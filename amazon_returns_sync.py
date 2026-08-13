r"""
Amazon SP-API FBA customer returns -> warehouse.

Returned UNITS (not reimbursements — `amazon_fba_reimbursements`, written by
amazon_fees_sync.py, only covers inventory Amazon pays you back for after a
loss/damage claim). This is the separate feed of actual customer returns,
with a return reason + disposition (sellable/unsellable) per ASIN/SKU/date.

Report: GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA (Reports API, tab-delimited
flat file). Reuses the request/poll/download + header-driven parse helpers
from amazon_fees_sync.py — same SP-API flat-file machinery and auth, no need
to duplicate it.

RETENTION: like the other Reports-API feeds, history is bounded — Amazon
CANCELS (returns no data) for windows past retention, so a deep backfill only
reaches so far back, and the recurring run is what accrues history going
forward. A CANCELLED window is a normal, non-fatal outcome (skipped), not an
error — don't treat it as one.

AUTH: same SPAPI_* LWA credentials as amazon_orders.py — no new creds.

USAGE:
  python amazon_returns_sync.py                      # last 30 days
  python amazon_returns_sync.py --start 2025-07-01 --end 2026-07-06
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS
# Reuse the SP-API flat-file report machinery + parse helpers.
from amazon_fees_sync import _create_and_download, _rows, _pick, _num, _int, require_env

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

PLATFORM = "amazon_returns"
REPORT_TYPE = "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA"

DDL = """
CREATE TABLE IF NOT EXISTS amazon_returns (
    return_date          TEXT NOT NULL,
    order_id             TEXT NOT NULL,
    sku                  TEXT NOT NULL,
    asin                 TEXT,
    fnsku                TEXT,
    product_name         TEXT,
    quantity             INTEGER,
    fulfillment_center   TEXT,
    disposition          TEXT,
    reason               TEXT,
    status               TEXT,
    license_plate_number TEXT NOT NULL DEFAULT '',
    customer_comments    TEXT,
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (order_id, sku, return_date, license_plate_number)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def parse_returns(conn: sqlite3.Connection, text: str, stamp: str) -> int:
    out = []
    for d in _rows(text):
        order_id = _pick(d, "order-id", "amazon-order-id")
        sku = _pick(d, "sku", "seller-sku")
        rdate = _pick(d, "return-date")
        if not order_id or not sku:
            continue
        out.append((
            rdate, order_id, sku,
            _pick(d, "asin"), _pick(d, "fnsku"), _pick(d, "product-name"),
            _int(_pick(d, "quantity")),
            _pick(d, "fulfillment-center-id", "fulfillment-center"),
            _pick(d, "detailed-disposition", "disposition"),
            _pick(d, "reason"),
            _pick(d, "status"),
            _pick(d, "license-plate-number") or "",
            _pick(d, "customer-comments"),
            stamp,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO amazon_returns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def _month_windows(start: date, end: date):
    """Yield (start, end) calendar-month windows spanning [start, end] inclusive."""
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        first = max(date(y, m, 1), start)
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        last = min(nxt - timedelta(days=1), end)
        yield first.isoformat(), last.isoformat()
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def sync(conn: sqlite3.Connection, start: date, end: date) -> tuple[int, int, list[str]]:
    """Pull returns for every calendar-month window spanning [start, end].
    Returns (rows_written, empty_window_count, error_messages)."""
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total = 0
    empty = 0
    errors: list[str] = []
    for ws, we in _month_windows(start, end):
        try:
            text = _create_and_download(host, REPORT_TYPE, ws, we)
            if text is None:
                empty += 1
                print(f"    returns {ws}..{we}: CANCELLED (no data / past retention)", flush=True)
                continue
            with conn:
                n = parse_returns(conn, text, stamp)
            total += n
            print(f"    returns {ws}..{we}: {n} rows", flush=True)
        except Exception as e:  # noqa: BLE001 — one window must not kill the backfill
            errors.append(f"{ws}..{we}: {str(e)[:100]}")
            print(f"    returns {ws}..{we}: FAILED {str(e)[:150]}", flush=True)
    return total, empty, errors


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="YYYY-MM-DD (default: 30 days ago)")
    p.add_argument("--end", help="YYYY-MM-DD (default: today)")
    args = p.parse_args()

    require_env()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=30)
    if start > end:
        raise SystemExit("--start must be <= --end")

    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)

    total, empty, errors = sync(conn, start, end)
    conn.close()

    status = "ok" if not errors else ("error" if total == 0 else "degraded")
    msg = f"{start}..{end}"
    if empty:
        msg += f" ({empty} empty/cancelled window(s))"
    if errors:
        msg += "; " + "; ".join(errors)
    warehouse_db.log_sync(PLATFORM, started, total, status, msg)
    print(f"Amazon returns: wrote {total} rows for {start}..{end}"
          + (f" ({len(errors)} window(s) failed)" if errors else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
