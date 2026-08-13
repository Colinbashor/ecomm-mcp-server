r"""
Flexport Logistics (3PL) connector — product catalog + daily inventory snapshots.

Flexport (docs.logistics-api.flexport.com) is a third-party-logistics (3PL)
provider: if your warehouse/fulfillment runs through them, this pulls your
catalog and stock levels into two local tables.

  flexport_products   — the 3PL's view of your catalog. `logistics_sku` is
                         Flexport's own id for the item (the key the inventory
                         endpoint returns); `merchant_sku` is YOUR sku, so this
                         table is the translation between the two id spaces.
                         Also carries per-unit weight/dimensions when Flexport
                         has them (useful for case-pack / shipping-cost work).
  flexport_inventory   — one row per logistics_sku per snapshot date
                         (available / on_hand / unavailable). The API only
                         ever returns CURRENT levels — there is no historical
                         endpoint — so running this job daily is what BUILDS
                         history. Snapshots accumulate; nothing is overwritten.

Auth: bearer token in FLEXPORT_API_TOKEN (.env). Get one from the Flexport
portal (Settings > API). Merchant tokens expire after about a year — a 401
means it's expired or was revoked, not that anything else is wrong. Skips
cleanly (prints a message, exits 0) if the token is unset, so this is safe to
leave in a scheduled job before you've wired Flexport up.

GENERIC API GOTCHAS worth knowing before you touch this endpoint family:
  * Pagination is limit/offset, but the page size you ASK for is not
    necessarily the page size you GET — some endpoints (bulk inventory, in
    particular) silently cap the returned page below the requested `limit`.
    Advance the offset by len(batch) actually returned, never by the limit you
    requested, or you will skip rows.
  * "Updated after" style filters on the catalog endpoint are silently
    IGNORED rather than erroring — verify a filtered pull actually narrowed
    the result set before trusting it, don't assume a 200 means it worked.
  * A full catalog crawl can take a long time for a large merchant (offset
    paging through the whole product list, one page at a time) — that's why
    the daily job below only refreshes inventory plus a small on-demand
    gap-fill, and leaves the full catalog crawl (`--catalog`) to be run
    occasionally rather than every day.

USAGE:
  python flexport_sync.py              # inventory snapshot for today
  python flexport_sync.py --catalog    # + full catalog crawl (can take a while)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import date, datetime, timezone

import requests
from dotenv import load_dotenv

from warehouse import db

load_dotenv()

BASE = "https://logistics-api.flexport.com/logistics/api/2024-06"
PAGE_SIZE = 500

DDL = """
CREATE TABLE IF NOT EXISTS flexport_products (
    logistics_sku  TEXT NOT NULL,   -- Flexport's id for the item
    merchant_sku   TEXT,            -- your own SKU
    name           TEXT,
    barcodes       TEXT,            -- comma-joined
    created_at     TEXT,
    updated_at     TEXT,
    weight_oz      REAL,            -- per-unit weight, normalized to oz
    length_in      REAL,            -- per-unit dims, normalized to inches
    width_in       REAL,
    height_in      REAL,
    dims_locked    INTEGER,         -- 1 = vendor-confirmed measurement, 0 = estimated
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (logistics_sku)
);
CREATE INDEX IF NOT EXISTS idx_flexport_products_msku ON flexport_products(merchant_sku);

CREATE TABLE IF NOT EXISTS flexport_inventory (
    snapshot_date  TEXT NOT NULL,   -- the day this level was observed
    logistics_sku  TEXT NOT NULL,
    merchant_sku   TEXT,            -- denormalized from flexport_products
    available      INTEGER DEFAULT 0,
    on_hand        INTEGER DEFAULT 0,
    unavailable    INTEGER DEFAULT 0,
    units_per_pack INTEGER DEFAULT 1,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, logistics_sku)
);
CREATE INDEX IF NOT EXISTS idx_flexport_inventory_msku ON flexport_inventory(merchant_sku);

-- Resume bookmark for a full --catalog crawl (offset paging; this endpoint has
-- no cursor) so a crash/interrupt restarts from the last committed page
-- instead of from the beginning. Cleared on a clean full-crawl completion.
CREATE TABLE IF NOT EXISTS flexport_catalog_sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

CATALOG_OFFSET_KEY = "catalog_offset"

# Explicit column list for inserts. ALTER TABLE ADD COLUMN always appends new
# columns at a table's PHYSICAL end — if this table ever grows a column via
# migration after it already has rows, a positional "VALUES (?, ?, ...)"
# insert would silently start mapping values onto the wrong columns. Naming
# columns explicitly makes that impossible regardless of migration history.
_PRODUCT_COLS = ("logistics_sku, merchant_sku, name, barcodes, created_at, updated_at, "
                 "weight_oz, length_in, width_in, height_in, dims_locked, synced_at")
_PRODUCT_INSERT = (
    f"INSERT OR REPLACE INTO flexport_products ({_PRODUCT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")

_WEIGHT_TO_OZ = {"oz": 1.0, "lb": 16.0, "kg": 35.27396, "g": 0.035274}
_LENGTH_TO_IN = {"in": 1.0, "cm": 0.393701, "mm": 0.0393701, "m": 39.3701}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _to_oz(weight, unit: str | None) -> float | None:
    if weight is None:
        return None
    return round(float(weight) * _WEIGHT_TO_OZ.get((unit or "oz").lower(), 1.0), 3)


def _to_in(value, unit: str | None) -> float | None:
    if value is None:
        return None
    return round(float(value) * _LENGTH_TO_IN.get((unit or "in").lower(), 1.0), 3)


def _get(path: str, params: dict) -> list[dict]:
    token = os.environ["FLEXPORT_API_TOKEN"]
    for attempt in range(8):
        try:
            resp = requests.get(f"{BASE}{path}", params=params,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/json"}, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(15)
            continue
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 10)))
            continue
        if resp.status_code == 401:
            raise RuntimeError("Flexport 401: token expired or revoked — "
                               "merchant tokens last about a year; get a new one from the portal.")
        if resp.status_code >= 500:
            time.sleep(10)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Flexport {path} {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    raise RuntimeError(f"Flexport {path} kept failing after retries.")


def _paged(path: str) -> list[dict]:
    # Advance by what the page ACTUALLY contained, not by the limit you asked
    # for — some list endpoints silently cap the returned page size below the
    # requested limit, and advancing by the requested limit would skip rows.
    out: list[dict] = []
    offset = 0
    while True:
        batch = _get(path, {"limit": PAGE_SIZE, "offset": offset})
        if not batch:
            return out
        out.extend(batch)
        offset += len(batch)
        time.sleep(0.2)


def _product_row(p: dict, stamp: str) -> tuple:
    dims = p.get("dimensions") or {}
    locked = p.get("dimsLocked")
    return (
        p.get("logisticsSku") or "", p.get("merchantSku"), p.get("name"),
        ",".join(p.get("barcodes") or []), p.get("createdAt"), p.get("updatedAt"),
        _to_oz(dims.get("weight"), dims.get("weightUnit")),
        _to_in(dims.get("length"), dims.get("lengthUnit")),
        _to_in(dims.get("width"), dims.get("lengthUnit")),
        _to_in(dims.get("height"), dims.get("lengthUnit")),
        int(bool(locked)) if locked is not None else None,
        stamp,
    )


def _commit_page(conn: sqlite3.Connection, rows: list[tuple], offset: int) -> None:
    """Other jobs may write the same SQLite file concurrently, so a transient
    'database is locked' here is expected, not a bug — retry the same page
    with backoff rather than letting one collision kill a long crawl."""
    for attempt in range(10):
        try:
            with conn:
                conn.executemany(_PRODUCT_INSERT, rows)
                conn.execute(
                    "INSERT OR REPLACE INTO flexport_catalog_sync_state VALUES (?, ?)",
                    (CATALOG_OFFSET_KEY, str(offset)))
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 9:
                raise
            wait = min(2 ** attempt, 60)
            print(f"  DB locked (another sync writing?) — retrying in {wait}s")
            time.sleep(wait)


def crawl_catalog(conn: sqlite3.Connection, stamp: str) -> int:
    """Full /products crawl, committing + checkpointing every page so a crash
    resumes from the last page instead of restarting."""
    row = conn.execute(
        "SELECT value FROM flexport_catalog_sync_state WHERE key = ?",
        (CATALOG_OFFSET_KEY,)).fetchone()
    offset = int(row[0]) if row else 0
    if offset:
        print(f"  resuming catalog crawl from offset {offset}")
    total = offset
    pages = 0
    while True:
        batch = _get("/products", {"limit": PAGE_SIZE, "offset": offset})
        if not batch:
            break
        rows = [_product_row(p, stamp) for p in batch]
        offset += len(batch)
        total += len(batch)
        _commit_page(conn, rows, offset)
        pages += 1
        if pages % 50 == 0:
            print(f"  catalog crawl: {total} products so far (offset {offset})")
        time.sleep(0.2)
    with conn:
        conn.execute("DELETE FROM flexport_catalog_sync_state WHERE key = ?", (CATALOG_OFFSET_KEY,))
    return total


def sync_inventory(conn: sqlite3.Connection, stamp: str, today: str) -> tuple[int, int]:
    """Pull the current inventory snapshot, gap-filling the catalog for any
    logistics_sku we haven't mapped yet (so merchant_sku stays complete
    without needing the full --catalog crawl every day). Returns
    (inventory_rows_written, catalog_gap_fills)."""
    inventory = _paged("/products/inventory/all")

    known = {r[0] for r in conn.execute("SELECT logistics_sku FROM flexport_products")}
    gaps = [i["logisticsSku"] for i in inventory
            if i.get("logisticsSku") and i["logisticsSku"] not in known]
    gap_rows = []
    for lsku in gaps:
        try:
            gap_rows.append(_product_row(_get(f"/products/{lsku}", {}), stamp))
        except RuntimeError as e:
            print(f"  gap-fill {lsku} failed: {e}")
        time.sleep(0.1)
    if gap_rows:
        with conn:
            conn.executemany(_PRODUCT_INSERT, gap_rows)

    sku_map = dict(conn.execute("SELECT logistics_sku, merchant_sku FROM flexport_products"))
    inv_rows = [(
        today, i.get("logisticsSku") or "", sku_map.get(i.get("logisticsSku")),
        int(i.get("available", 0) or 0), int(i.get("onHand", 0) or 0),
        int(i.get("unavailable", 0) or 0), int(i.get("unitsPerPack", 1) or 1),
        stamp,
    ) for i in inventory]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO flexport_inventory VALUES (?,?,?,?,?,?,?,?)",
            inv_rows)
    return len(inv_rows), len(gap_rows)


def main() -> None:
    if not os.environ.get("FLEXPORT_API_TOKEN"):
        print("FLEXPORT_API_TOKEN not set — skipping Flexport catalog/inventory sync.")
        return
    full_catalog = "--catalog" in sys.argv

    conn = db.connect()
    ensure_schema(conn)
    started = db.now()
    today = date.today().isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        prod_count = 0
        if full_catalog:
            prod_count = crawl_catalog(conn, stamp)
        n_inventory, n_gaps = sync_inventory(conn, stamp, today)
        conn.close()
    except Exception as e:  # noqa: BLE001
        db.log_sync("flexport", started, 0, "error", str(e))
        raise

    db.log_sync("flexport", started, n_inventory, "ok")
    print(f"Flexport: {n_inventory} inventory rows snapshotted for {today}"
          + (f", {prod_count} catalog products (full crawl)" if full_catalog else
             f", {n_gaps} catalog gap-fills"))


if __name__ == "__main__":
    main()
