r"""
Amazon Sales & Traffic (Business Report) -> warehouse.

Organic demand data that lives nowhere else in the warehouse: sessions, page
views, Buy Box %, and units ordered per ASIN — the traffic side of the
picture that a pure orders/ads feed can't give you (e.g. "this ASIN gets
lots of sessions but a low Buy Box % / low conversion").

Two tables for the Mon-Sun weekly path (one report request per week; the
byAsin section aggregates over whatever range you request, so a single
report call gives you every child ASIN's totals for the week):
  amazon_traffic_weekly — week_start x child ASIN: sessions, page views,
                          buy box %, units ordered, ordered sales
  amazon_traffic_daily  — account-level daily totals (for trend lines)

A separate --month path pulls ONE report for a whole calendar-month range
(never day-by-day) into PARALLEL tables, so the weekly path above is never
touched by a monthly run:
  amazon_traffic_monthly         — month x child ASIN (same measures as weekly)
  amazon_traffic_monthly_account — month account totals, rolled up from the
                                    report's per-day salesAndTrafficByDate rows

Source: SP-API Reports v2021-06-30, report type GET_SALES_AND_TRAFFIC_REPORT.
Like other async Reports-API feeds, this is request -> poll -> download; the
document is usually (not always) gzip-compressed, so check
`compressionAlgorithm` rather than assuming.

Unlike several other flat-file Reports-API feeds (see amazon_fees_sync.py),
this report type does not appear to have a short retention floor in practice
— multi-year backfills of `--weeks` generally work, subject to Amazon's own
report-queue throttling (expect 429s under sustained load; this backs off
and retries).

AUTH: same SPAPI_* LWA credentials as amazon_orders.py — no new creds. This
script is standalone (not wired into run_sync.py), like its sibling
amazon_*_sync.py scripts.

USAGE:
  python amazon_traffic_sync.py                        # prior complete week
  python amazon_traffic_sync.py --week 2026-06-22       # one specific Monday
  python amazon_traffic_sync.py --weeks 8               # backfill N weeks
  python amazon_traffic_sync.py --month 2026-06         # native calendar month
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS, _access_token

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

PLATFORM = "amazon_traffic"
REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")

DDL = """
CREATE TABLE IF NOT EXISTS amazon_traffic_weekly (
    week_start     TEXT NOT NULL,   -- Monday
    asin           TEXT NOT NULL,   -- child ASIN
    parent_asin    TEXT,
    sessions       INTEGER DEFAULT 0,
    page_views     INTEGER DEFAULT 0,
    buy_box_pct    REAL    DEFAULT 0,
    units_ordered  INTEGER DEFAULT 0,
    ordered_sales  REAL    DEFAULT 0,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (week_start, asin)
);
CREATE TABLE IF NOT EXISTS amazon_traffic_daily (
    date           TEXT NOT NULL PRIMARY KEY,
    sessions       INTEGER DEFAULT 0,
    page_views     INTEGER DEFAULT 0,
    units_ordered  INTEGER DEFAULT 0,
    ordered_sales  REAL    DEFAULT 0,
    total_orders   INTEGER DEFAULT 0,
    synced_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amazon_traffic_monthly (
    month          TEXT NOT NULL,   -- 'YYYY-MM'
    asin           TEXT NOT NULL,   -- child ASIN
    parent_asin    TEXT,
    sessions       INTEGER DEFAULT 0,
    page_views     INTEGER DEFAULT 0,
    buy_box_pct    REAL    DEFAULT 0,
    units_ordered  INTEGER DEFAULT 0,
    ordered_sales  REAL    DEFAULT 0,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (month, asin)
);
CREATE TABLE IF NOT EXISTS amazon_traffic_monthly_account (
    month          TEXT NOT NULL PRIMARY KEY,   -- 'YYYY-MM'
    sessions       INTEGER DEFAULT 0,
    page_views     INTEGER DEFAULT 0,
    units_ordered  INTEGER DEFAULT 0,
    ordered_sales  REAL    DEFAULT 0,
    total_orders   INTEGER DEFAULT 0,
    synced_at      TEXT NOT NULL
);
"""


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_traffic_sync: missing required env var(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill in the "
            "SP-API credentials (same ones amazon_orders.py uses)."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


# ---- small local period helpers (no shared "report_period" module here) ---

def _week_bounds(monday: date) -> tuple[str, str]:
    """Monday -> (start, end) ISO date strings for that Mon-Sun week."""
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _month_bounds(ym: str) -> tuple[str, str]:
    """'YYYY-MM' -> (first_day, last_day) ISO date strings for that month."""
    y, m = (int(x) for x in ym.split("-"))
    first = date(y, m, 1)
    last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _recent_mondays(n: int) -> list[date]:
    """Last n complete Mon-Sun weeks (excludes the currently-running week),
    most recent first — matches the original --weeks backfill order."""
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    return [this_monday - timedelta(weeks=i) for i in range(1, n + 1)]


# ---- report request / poll / download --------------------------------------

def _report(host: str, start: str, end: str) -> dict:
    headers = {"x-amz-access-token": _access_token(), "Content-Type": "application/json"}
    r = requests.post(f"{host}/reports/2021-06-30/reports", headers=headers, json={
        "reportType": "GET_SALES_AND_TRAFFIC_REPORT",
        "marketplaceIds": [os.environ["SPAPI_MARKETPLACE_ID"]],
        "dataStartTime": f"{start}T00:00:00Z",
        "dataEndTime": f"{end}T23:59:59Z",
        "reportOptions": {"dateGranularity": "DAY", "asinGranularity": "CHILD"},
    }, timeout=60)
    if r.status_code == 429:
        time.sleep(60)
        return _report(host, start, end)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"traffic report request {r.status_code}: {r.text[:200]}")
    report_id = r.json()["reportId"]

    doc_id = None
    for _ in range(60):
        time.sleep(15)
        headers["x-amz-access-token"] = _access_token()
        st = requests.get(f"{host}/reports/2021-06-30/reports/{report_id}",
                          headers=headers, timeout=60).json()
        if st.get("processingStatus") == "DONE":
            doc_id = st["reportDocumentId"]
            break
        if st.get("processingStatus") in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"traffic report failed: {st}")
    if not doc_id:
        raise TimeoutError("traffic report did not finish in time")

    doc = requests.get(f"{host}/reports/2021-06-30/documents/{doc_id}",
                       headers=headers, timeout=60).json()
    raw = requests.get(doc["url"], timeout=120).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return json.loads(raw)


# ---- row shaping (kept as free functions so tests can exercise them without
# any network / DB access) ---------------------------------------------------

def _weekly_rows(data: dict, week_start: str, stamp: str) -> list[tuple]:
    rows = []
    for a in data.get("salesAndTrafficByAsin", []):
        t, s = a.get("trafficByAsin") or {}, a.get("salesByAsin") or {}
        rows.append((
            week_start, a.get("childAsin") or "", a.get("parentAsin"),
            int(t.get("sessions", 0) or 0), int(t.get("pageViews", 0) or 0),
            float(t.get("buyBoxPercentage", 0) or 0),
            int(s.get("unitsOrdered", 0) or 0),
            float((s.get("orderedProductSales") or {}).get("amount", 0) or 0),
            stamp,
        ))
    return rows


def _daily_rows(data: dict, stamp: str) -> list[tuple]:
    rows = []
    for d in data.get("salesAndTrafficByDate", []):
        t, s = d.get("trafficByDate") or {}, d.get("salesByDate") or {}
        rows.append((
            d.get("date"),
            int(t.get("sessions", 0) or 0), int(t.get("pageViews", 0) or 0),
            int(s.get("unitsOrdered", 0) or 0),
            float((s.get("orderedProductSales") or {}).get("amount", 0) or 0),
            int(s.get("totalOrderItems", 0) or 0),
            stamp,
        ))
    return rows


def _monthly_account_row(data: dict, ym: str, stamp: str) -> tuple:
    """Roll salesAndTrafficByDate up into a single account-level month row."""
    sessions = pageviews = units = orders = 0
    sales = 0.0
    for d in data.get("salesAndTrafficByDate", []):
        t, s = d.get("trafficByDate") or {}, d.get("salesByDate") or {}
        sessions += int(t.get("sessions", 0) or 0)
        pageviews += int(t.get("pageViews", 0) or 0)
        units += int(s.get("unitsOrdered", 0) or 0)
        sales += float((s.get("orderedProductSales") or {}).get("amount", 0) or 0)
        orders += int(s.get("totalOrderItems", 0) or 0)
    return (ym, sessions, pageviews, units, round(sales, 2), orders, stamp)


def sync_week(conn: sqlite3.Connection, monday: date, stamp: str) -> int:
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    start, end = _week_bounds(monday)
    data = _report(host, start, end)

    weekly = _weekly_rows(data, start, stamp)
    daily = _daily_rows(data, stamp)
    with conn:
        conn.executemany("INSERT OR REPLACE INTO amazon_traffic_weekly VALUES (?,?,?,?,?,?,?,?,?)", weekly)
        conn.executemany("INSERT OR REPLACE INTO amazon_traffic_daily VALUES (?,?,?,?,?,?,?)", daily)
    print(f"    traffic week {start}: {len(weekly)} asins, {len(daily)} days", flush=True)
    return len(weekly) + len(daily)


def sync_month(conn: sqlite3.Connection, ym: str, stamp: str) -> int:
    """Native calendar-month pull -> amazon_traffic_monthly (+_account). ONE
    report request for the whole month range (byAsin aggregates over the full
    requested range on its own; byDate comes back one row per day and is
    summed here into a single account-level month row) — never day-by-day.
    Parallel tables, never touches amazon_traffic_weekly/_daily."""
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    start, end = _month_bounds(ym)
    data = _report(host, start, end)

    monthly = _weekly_rows(data, ym, stamp)  # same per-ASIN shape, month key instead of week key
    acct_row = _monthly_account_row(data, ym, stamp)
    by_date_count = len(data.get("salesAndTrafficByDate", []))

    with conn:
        conn.execute("DELETE FROM amazon_traffic_monthly WHERE month = ?", (ym,))
        conn.executemany("INSERT OR REPLACE INTO amazon_traffic_monthly VALUES (?,?,?,?,?,?,?,?,?)", monthly)
        conn.execute(
            "INSERT OR REPLACE INTO amazon_traffic_monthly_account VALUES (?,?,?,?,?,?,?)",
            acct_row,
        )
    print(f"    traffic month {ym} ({start}..{end}): {len(monthly)} asins, "
          f"{by_date_count} days rolled up to 1 account row", flush=True)
    return len(monthly) + 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--week", help="Monday (YYYY-MM-DD) of one specific report week")
    p.add_argument("--weeks", type=int, default=1,
                   help="how many complete Mon-Sun weeks back to pull (default: prior week; "
                        "ignored if --week is given)")
    p.add_argument("--month", help="YYYY-MM calendar month — native month pull into the "
                                    "parallel monthly tables (see module docstring)")
    args = p.parse_args()

    require_env()
    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.month:
        try:
            total = sync_month(conn, args.month, stamp)
            status = "ok" if total else "error"
        except Exception as e:  # noqa: BLE001
            print(f"    traffic month {args.month}: FAILED {str(e)[:200]}", flush=True)
            total, status = 0, "error"
        finally:
            conn.close()
        warehouse_db.log_sync(f"{PLATFORM}_monthly", started, total, status, args.month)
        print(f"Amazon traffic (month {args.month}): wrote {total} rows")
        return 0 if status == "ok" else 1

    if args.week:
        monday = date.fromisoformat(args.week)
        if monday.weekday() != 0:
            raise SystemExit("Report weeks start on Monday.")
        mondays = [monday]
    else:
        mondays = _recent_mondays(args.weeks)

    total = 0
    try:
        for monday in mondays:
            try:
                total += sync_week(conn, monday, stamp)
            except Exception as e:  # noqa: BLE001 — one week must not kill the rest
                print(f"    traffic week {monday}: FAILED {str(e)[:100]}", flush=True)
    finally:
        conn.close()
    warehouse_db.log_sync(PLATFORM, started, total, "ok" if total else "error")
    print(f"Amazon traffic: wrote {total} rows")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
