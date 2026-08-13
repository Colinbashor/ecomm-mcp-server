r"""
Flexport Logistics (3PL) connector — INBOUND SHIPMENTS (supplier receiving feed).

The stock-ARRIVING side of the 3PL: purchase orders / supplier shipments
flowing INTO the warehouse network (as opposed to flexport_orders_sync.py,
which is stock going OUT to customers, and flexport_returns_sync.py, stock
coming BACK from customers). This is the "what's inbound and when will it
land" signal for replenishment/reorder planning.

Two tables (one row per inbound shipment, one row per shipment line):
  flexport_inbounds       — status (WORKING/READY_TO_SHIP/IN_TRANSIT/ARRIVED/
                            COMPLETED/...), the purchase-order reference via
                            shipping_plan_external_id + shipping_plan_id/name,
                            shipping_option, shipment_destination, booking_id,
                            supplier ship_from (name/country), destination
                            ship_to_name (the receiving warehouse),
                            arrived_at / completed_at, package count, and
                            rolled-up expected/sellable/damaged unit counts.
  flexport_inbound_lines  — merchant_sku (your own SKU — this payload carries
                            it directly, no id-translation hop needed),
                            logistics_sku (the vendor's own id for the item),
                            pack_of_dsku, line_item_id, and per-line
                            expected/sellable/damaged counts.

PAGINATION: unlike the orders/returns endpoints, THIS one has NO Link cursor
at all — it's plain offset + limit, newest-first, and (verified) offset
actually advances cleanly here with no freeze or cap. Not every list endpoint
on the same API needs the same pagination trick; check each one rather than
assuming the whole API shares one pattern. We walk offset 0, 100, 200, ...
until a short/empty page comes back.

UPSERT-PER-PAGE + GRACEFUL PAUSE: a shipment's status and counts evolve as
it's received (expected -> sellable), so this behaves like a live status feed
rather than an immutable event log. Each run re-walks from offset 0
(newest-first) and commits each page as it arrives — a shipment's lines are
replaced within the same transaction so stale line rows never linger if the
item set shrinks. If the vendor API throws a sustained 5xx window, the run
pauses (already-committed pages survive) rather than crashing, and the next
run re-walks from the top and converges the snapshot rather than needing an
all-or-nothing retry. Aged-out completed shipments are left in place, not
pruned — they're historical receiving records.

Auth: bearer token in FLEXPORT_API_TOKEN (.env), same token as the other
Flexport connectors. This is a read-only GET endpoint. Skips cleanly if unset.

USAGE:
  python flexport_inbounds_sync.py                # full snapshot refresh
  python flexport_inbounds_sync.py --max-pages 5   # cap this run (debug)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from warehouse import db

load_dotenv()

BASE = "https://logistics-api.flexport.com/logistics/api/2024-06"
PAGE = 100                 # this endpoint caps at 100 per page
DEFAULT_MAX_PAGES = 1000   # runaway guard, well past any realistic shipment count
_MAX_RETRIES = 12
_BACKOFF_CAP = 90

DDL = """
CREATE TABLE IF NOT EXISTS flexport_inbounds (
    id                        TEXT PRIMARY KEY,   -- Flexport's own inbound shipment id
    receiving_id              TEXT,
    status                    TEXT,               -- WORKING/READY_TO_SHIP/IN_TRANSIT/ARRIVED/COMPLETED/...
    shipping_plan_id          TEXT,
    shipping_plan_external_id TEXT,               -- the PO reference (e.g. your own PO number)
    shipping_plan_name        TEXT,
    shipping_option           TEXT,               -- FREIGHT_EXTERNAL/...
    shipment_destination      TEXT,
    booking_id                TEXT,
    ship_from_name            TEXT,               -- supplier
    ship_from_country         TEXT,
    ship_to_name              TEXT,               -- destination warehouse
    arrived_at                TEXT,
    completed_at              TEXT,
    n_lines                   INTEGER,
    n_packages                INTEGER,
    expected_units            INTEGER,
    sellable_units            INTEGER,
    damaged_units             INTEGER,
    synced_at                 TEXT
);
CREATE TABLE IF NOT EXISTS flexport_inbound_lines (
    inbound_id        TEXT NOT NULL,
    shipment_item_id  TEXT NOT NULL,
    merchant_sku      TEXT,              -- your own SKU
    logistics_sku     TEXT,              -- the vendor's own id for the item
    pack_of_dsku      TEXT,
    line_item_id      TEXT,
    expected          INTEGER,
    sellable          INTEGER,
    damaged           INTEGER,
    synced_at         TEXT,
    PRIMARY KEY (inbound_id, shipment_item_id)
);
CREATE INDEX IF NOT EXISTS idx_flexport_inbound_lines_sku ON flexport_inbound_lines(merchant_sku);
CREATE INDEX IF NOT EXISTS idx_flexport_inbounds_status ON flexport_inbounds(status);
CREATE INDEX IF NOT EXISTS idx_flexport_inbounds_po ON flexport_inbounds(shipping_plan_external_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


class FlexportTransient(RuntimeError):
    """Transient (5xx/429/connection) failures exhausted every retry. Distinct
    from a hard 401/4xx so the caller can pause (already-committed pages
    survive) and a re-run converges the snapshot, rather than crashing."""


def _request(path: str, params: dict) -> requests.Response:
    """GET with exponential backoff on transient failures; 401/other-4xx are
    hard-fatal; retry exhaustion raises FlexportTransient so the caller can
    pause-and-resume."""
    token = os.environ["FLEXPORT_API_TOKEN"]
    last_error = None
    for attempt in range(_MAX_RETRIES):
        backoff = min(10 * (2 ** min(attempt, 5)), _BACKOFF_CAP)
        try:
            resp = requests.get(f"{BASE}{path}", params=params,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/json"}, timeout=90)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = f"connection error: {e}"
            time.sleep(backoff)
            continue
        if resp.status_code == 429:
            last_error = "429 rate limited"
            time.sleep(float(resp.headers.get("Retry-After", backoff)))
            continue
        if resp.status_code == 401:
            raise RuntimeError("Flexport 401: token expired or revoked — get a new one from the portal.")
        if resp.status_code >= 500:
            last_error = f"{resp.status_code} server error"
            time.sleep(backoff)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Flexport {path} {resp.status_code}: {resp.text[:200]}")
        return resp
    raise FlexportTransient(f"Flexport {path} kept failing after retries. Last error: {last_error}")


def iter_shipments(max_pages: int):
    """Yield inbound-shipment dicts, walking offset 0, 100, ... newest-first
    until a short/empty page (no Link cursor on this endpoint)."""
    offset = 0
    for _ in range(max_pages):
        resp = _request("/inbounds/shipments", {"limit": PAGE, "offset": offset})
        batch = resp.json()
        recs = batch if isinstance(batch, list) else (batch.get("data") or [])
        if not recs:
            return
        for rec in recs:
            if isinstance(rec, dict) and rec.get("id"):
                yield rec
        if len(recs) < PAGE:
            return
        offset += PAGE
        time.sleep(0.1)


def rows_for_shipment(rec: dict, stamp: str) -> tuple[tuple, list[tuple]]:
    addrs = rec.get("addresses") or {}
    frm = addrs.get("from") or {}
    to = addrs.get("to") or {}
    items = rec.get("items") or []
    exp = sel = dmg = 0
    line_rows: list[tuple] = []
    for it in items:
        c = it.get("counts") or {}
        e, s, d = int(c.get("expected") or 0), int(c.get("sellable") or 0), int(c.get("damaged") or 0)
        exp += e
        sel += s
        dmg += d
        line_rows.append((
            rec.get("id"), str(it.get("shipmentItemId") or ""),
            it.get("merchantSku"), it.get("logisticsSku"), it.get("packOfDsku"),
            str(it.get("lineItemId") or "") or None,
            e, s, d, stamp,
        ))
    ship_row = (
        rec.get("id"), rec.get("receivingId"), rec.get("status"),
        str(rec.get("shippingPlanId") or "") or None,
        rec.get("shippingPlanExternalId"), rec.get("shippingPlanName"),
        rec.get("shippingOption"), rec.get("shipmentDestination"),
        str(rec.get("bookingId") or "") or None,
        frm.get("name"), frm.get("countryCode"),
        to.get("name"),
        rec.get("arrivedAt"), rec.get("completedAt"),
        len(items), len(rec.get("packages") or []),
        exp, sel, dmg, stamp,
    )
    return ship_row, line_rows


def upsert_page(conn: sqlite3.Connection, recs: list[dict], stamp: str) -> tuple[int, int]:
    """Commit one page's shipments + lines immediately (per-shipment: replace
    its lines so a shrunk item set leaves no stale rows), so progress survives
    a later transient failure. Returns (n_shipments, n_lines) written."""
    n_ships = n_lines = 0
    with conn:
        for rec in recs:
            srow, lrows = rows_for_shipment(rec, stamp)
            conn.execute("DELETE FROM flexport_inbound_lines WHERE inbound_id = ?", (rec.get("id"),))
            conn.execute(
                "INSERT OR REPLACE INTO flexport_inbounds VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", srow)
            conn.executemany(
                "INSERT OR REPLACE INTO flexport_inbound_lines VALUES "
                "(?,?,?,?,?,?,?,?,?,?)", lrows)
            n_ships += 1
            n_lines += len(lrows)
    return n_ships, n_lines


def run(conn: sqlite3.Connection, max_pages: int) -> tuple[int, int, bool]:
    """Returns (n_shipments, n_lines, paused)."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_ships = n_lines = 0
    page: list[dict] = []
    try:
        for rec in iter_shipments(max_pages):
            page.append(rec)
            if len(page) >= PAGE:
                s, l = upsert_page(conn, page, stamp)
                n_ships += s
                n_lines += l
                page = []
        if page:
            s, l = upsert_page(conn, page, stamp)
            n_ships += s
            n_lines += l
        return n_ships, n_lines, False
    except FlexportTransient as e:
        print(f"Flexport inbounds PAUSED after {n_ships} shipments this run — "
              f"vendor backend degraded ({e}). Committed pages saved; re-run to converge.")
        return n_ships, n_lines, True


def main() -> int:
    if not os.environ.get("FLEXPORT_API_TOKEN"):
        print("FLEXPORT_API_TOKEN not set — skipping Flexport inbounds sync.")
        return 0

    p = argparse.ArgumentParser()
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = p.parse_args()

    conn = db.connect()
    ensure_schema(conn)
    started = db.now()
    try:
        n_ships, n_lines, paused = run(conn, args.max_pages)
        conn.close()
    except Exception as e:  # noqa: BLE001
        db.log_sync("flexport_inbounds", started, 0, "error", str(e))
        raise

    if paused:
        db.log_sync("flexport_inbounds", started, n_ships, "degraded", "paused (transient)")
        return 75

    db.log_sync("flexport_inbounds", started, n_ships, "ok")
    print(f"Flexport inbounds: {n_ships} shipments, {n_lines} lines upserted this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
