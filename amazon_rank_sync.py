r"""
Amazon Best Sellers Rank (BSR) snapshots -> warehouse.

Sales rank moves faster than order/sales data, so it is often the earliest
signal that a product is taking off (or falling off) — worth tracking
alongside your sales numbers rather than only checking it by hand.

Source: SP-API Catalog Items API (2022-04-01), `searchCatalogItems` by ASIN
(up to 20 identifiers per call, `includedData=salesRanks`). One dated
snapshot row per ASIN; run this daily/weekly to build a rank HISTORY — the
API only ever returns the CURRENT rank (same "snapshot table" pattern as
amazon_inventory_sync.py), so the recurring run is what builds a trend.

We store two ranks per ASIN:
  display_rank  — the broad top-level category rank Amazon shows as the
                  headline BSR (e.g. "Clothing, Shoes & Jewelry" — a big
                  number, whole-store position)
  category_rank — the BEST (lowest) specific sub-category rank, e.g.
                  "#27 in Women's Novelty Dresses" — usually the more
                  actionable "is this taking off" signal

WHICH ASINs TO TRACK: this base scaffold has no product/traffic table to
source a target-ASIN list from (that depends on your own catalog or sales
data), so you provide the list explicitly: `--asins` (comma-separated) or
`--asins-file` (one ASIN per line). As a convenience, if neither is given
this script will also look for distinct ASINs in `amazon_fulfilled_shipments`
(written by amazon_fees_sync.py's "shipments" report) as a proxy for
"ASINs that recently sold" — but that table is optional and this is just a
fallback, not the intended primary input.

GOTCHA — pageSize DEFAULTS TO 10, NOT YOUR BATCH SIZE: `searchCatalogItems`
silently returns only the first 10 items of a 20-ASIN batch unless you
explicitly pass `pageSize=20` — the rest sit behind a `nextToken` this script
does not follow. Always set pageSize to (at least) your batch size.

AUTH: same SPAPI_* LWA credentials as amazon_orders.py — no new creds.

USAGE:
  python amazon_rank_sync.py --asins B0EXAMPLE1,B0EXAMPLE2
  python amazon_rank_sync.py --asins-file asins.txt
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS, _access_token

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

PLATFORM = "amazon_rank"
BATCH_SIZE = 20  # Catalog Items API max identifiers per call

REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")

DDL = """
CREATE TABLE IF NOT EXISTS amazon_sales_rank (
    snapshot_date  TEXT NOT NULL,
    asin           TEXT NOT NULL,
    display_rank   INTEGER,
    display_title  TEXT,
    category_rank  INTEGER,
    category_title TEXT,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, asin)
);
"""


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_rank_sync: missing required env var(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill in the "
            "SP-API credentials (same ones amazon_orders.py uses)."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def fallback_asins(conn: sqlite3.Connection) -> list[str]:
    """Best-effort ASIN list when the caller doesn't pass --asins/--asins-file:
    whatever recently showed up in amazon_fulfilled_shipments (if that table
    exists and has been populated by amazon_fees_sync.py). Returns [] if the
    table is missing or empty — the caller decides what to do about that."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT sku FROM amazon_fulfilled_shipments "
            "WHERE sku IS NOT NULL AND sku <> ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    # amazon_fulfilled_shipments is keyed by seller SKU, not ASIN, in this
    # scaffold — this fallback is intentionally weak; pass --asins for a real run.
    return [r[0] for r in rows if r[0]]


def _fetch_batch(host: str, mk: str, asins: list[str]) -> dict:
    """searchCatalogItems for up to 20 ASINs; returns {asin: item}. Retries 429."""
    # pageSize defaults to 10 — without it a 20-ASIN batch silently returns
    # only the first 10 (see module docstring). Set it to the batch size.
    params = {
        "marketplaceIds": mk, "identifiers": ",".join(asins),
        "identifiersType": "ASIN", "includedData": "salesRanks",
        "pageSize": BATCH_SIZE,
    }
    for attempt in range(8):
        try:
            r = requests.get(
                f"{host}/catalog/2022-04-01/items",
                headers={"x-amz-access-token": _access_token(), "Accept": "application/json"},
                params=params, timeout=60,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(15)
            continue
        if r.status_code == 429:
            time.sleep(min(60.0, 5 * (attempt + 1)))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"catalog items {r.status_code}: {r.text[:200]}")
        return {it.get("asin"): it for it in r.json().get("items", [])}
    raise RuntimeError("catalog items kept throttling after retries.")


def _extract(item: dict, mk: str) -> tuple[int | None, str | None, int | None, str | None]:
    display_rank = display_title = category_rank = category_title = None
    for sr in item.get("salesRanks", []):
        if sr.get("marketplaceId") not in (mk, None):
            continue
        for d in sr.get("displayGroupRanks", []):
            if d.get("rank") is not None and (display_rank is None or d["rank"] < display_rank):
                display_rank, display_title = d["rank"], d.get("title")
        for c in sr.get("classificationRanks", []):
            # best (lowest) specific-category rank = the headline sub-category BSR
            if c.get("rank") is not None and (category_rank is None or c["rank"] < category_rank):
                category_rank, category_title = c["rank"], c.get("title")
    return display_rank, display_title, category_rank, category_title


def fetch_ranks(asins: list[str]) -> list[tuple]:
    """Fetch sales-rank snapshots for the given ASINs, batched at BATCH_SIZE."""
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    mk = os.environ["SPAPI_MARKETPLACE_ID"]
    snapshot_date = date.today().isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows: list[tuple] = []
    for i in range(0, len(asins), BATCH_SIZE):
        batch = asins[i:i + BATCH_SIZE]
        try:
            found = _fetch_batch(host, mk, batch)
        except Exception as e:  # noqa: BLE001 — one batch must not kill the run
            print(f"    batch {i // BATCH_SIZE}: FAILED {str(e)[:100]}", flush=True)
            continue
        for a in batch:
            it = found.get(a)
            if not it:
                continue
            dr, dt, cr, ct = _extract(it, mk)
            rows.append((snapshot_date, a, dr, dt, cr, ct, stamp))
        time.sleep(0.6)  # stay comfortably under the ~2 req/s limit
    return rows


def write_rows(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    with conn:
        conn.executemany("INSERT OR REPLACE INTO amazon_sales_rank VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--asins", help="comma-separated list of ASINs to snapshot")
    p.add_argument("--asins-file", help="path to a file with one ASIN per line")
    args = p.parse_args()

    require_env()
    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)

    if args.asins:
        asins = [a.strip() for a in args.asins.split(",") if a.strip()]
    elif args.asins_file:
        asins = [ln.strip() for ln in Path(args.asins_file).read_text().splitlines() if ln.strip()]
    else:
        asins = fallback_asins(conn)

    if not asins:
        conn.close()
        warehouse_db.log_sync(PLATFORM, started, 0, "error", "no target ASINs")
        raise SystemExit(
            "No ASINs to rank. Pass --asins A,B,C or --asins-file path.txt — "
            "this scaffold has no product table to source a default list from."
        )

    try:
        rows = fetch_ranks(asins)
        n = write_rows(conn, rows)
    finally:
        conn.close()

    warehouse_db.log_sync(PLATFORM, started, n, "ok" if n else "error")
    print(f"Amazon sales rank: {n}/{len(asins)} ASINs snapshotted for {date.today().isoformat()}")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
