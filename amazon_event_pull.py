r"""
Amazon Sales & Traffic for an ARBITRARY date window (a promotional event) -> warehouse.

Why this exists: amazon_traffic_sync.py only pulls whole Mon-Sun weeks, so a
tentpole sale event that falls inside the CURRENT week is invisible to every
consumer until the week closes. This pulls the event window directly and stores
it WITHOUT touching the weekly tables or their coverage table, so a partial
week can never be mistaken for a finished one.

It reuses the helpers in amazon_traffic_sync (report request/poll/download,
`coverage()` completeness validation), so it needs the same SP-API credentials
(SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET / SPAPI_REFRESH_TOKEN, optional
SPAPI_REGION) and the same Sales & Traffic report access.

WRITES
  amazon_traffic_daily
        Daily account totals for [start - pad_days, end] -- the same rows the
        weekly pull writes; the next weekly pull overwrites them identically.
  amazon_event_asin
        Per-ASIN totals over [start, end] ONLY. byAsin aggregates the whole
        range requested, so the event report is requested on its own, never
        with the baseline pad.
  amazon_event_pull
        What was ASKED and what Amazon actually returned (days expected vs
        returned, both section totals, completeness). Same "record what was
        asked; absence of rows is never the resume marker" rule as the weekly
        coverage table.
  dim_event_calendar
        One row describing the event (INSERT OR REPLACE), so downstream
        analysis can join event windows to any channel's data.

CAVEATS
  * Amazon publishes the most recent day late. A pull made while the event is
    still current may be short; the run logs `degraded` and exits 1 until the
    window is complete, and the final event day may restate upward on re-pull.
    The weekly pull after the week closes is authoritative.
  * Re-running with the same --event-id replaces that event's rows.

USAGE
  python amazon_event_pull.py --event-id spring_sale_2026 \
      --name "Spring Sale" --start 2026-03-10 --end 2026-03-11
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import amazon_traffic_sync as t
from warehouse import db as warehouse_db

PLATFORM = "amazon_event"

DDL = """
CREATE TABLE IF NOT EXISTS amazon_event_asin (
    event_id       TEXT NOT NULL,
    asin           TEXT NOT NULL,
    parent_asin    TEXT,
    sessions       INTEGER DEFAULT 0,
    page_views     INTEGER DEFAULT 0,
    buy_box_pct    REAL,
    units_ordered  INTEGER DEFAULT 0,
    ordered_sales  REAL DEFAULT 0,
    range_start    TEXT NOT NULL,
    range_end      TEXT NOT NULL,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (event_id, asin)
);
CREATE TABLE IF NOT EXISTS amazon_event_pull (
    event_id       TEXT PRIMARY KEY,
    range_start    TEXT NOT NULL,
    range_end      TEXT NOT NULL,
    days_expected  INTEGER NOT NULL,
    days_returned  INTEGER NOT NULL,
    missing_days   TEXT,
    bydate_sales   REAL,
    byasin_sales   REAL,
    sections_gap   REAL,
    is_complete    INTEGER NOT NULL,
    note           TEXT,
    synced_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dim_event_calendar (
    event_id       TEXT PRIMARY KEY,
    event_name     TEXT,
    event_type     TEXT,
    channel        TEXT,
    start_date     TEXT,
    end_date       TEXT,
    discount_pct   REAL,
    mechanic       TEXT,
    notes          TEXT,
    synced_at      TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    t.ensure_schema(conn)          # amazon_traffic_daily lives in the weekly module
    conn.executescript(DDL)


def _num(d: dict | None, key: str, cast=int):
    return cast((d or {}).get(key, 0) or 0)


def _money(section: dict | None) -> float:
    return float(((section or {}).get("orderedProductSales") or {}).get("amount", 0) or 0)


def asin_rows(event_id: str, data: dict, start: str, end: str, stamp: str) -> list[tuple]:
    rows = []
    for x in data.get("salesAndTrafficByAsin", []):
        tr, sa = x.get("trafficByAsin") or {}, x.get("salesByAsin") or {}
        rows.append((
            event_id, x.get("childAsin") or "", x.get("parentAsin"),
            _num(tr, "sessions"), _num(tr, "pageViews"), _num(tr, "buyBoxPercentage", float),
            _num(sa, "unitsOrdered"), _money(sa), start, end, stamp))
    return rows


def daily_rows(data: dict, stamp: str) -> list[tuple]:
    rows = []
    for d in data.get("salesAndTrafficByDate", []):
        tr, sa = d.get("trafficByDate") or {}, d.get("salesByDate") or {}
        rows.append((
            d.get("date"), _num(tr, "sessions"), _num(tr, "pageViews"),
            _num(sa, "unitsOrdered"), _money(sa), _num(sa, "totalOrderItems"), stamp))
    return rows


def store_event(conn: sqlite3.Connection, *, event_id: str, name: str, start: str,
                end: str, channel: str, notes: str, cov: dict, asins: list[tuple],
                daily: list[tuple], stamp: str) -> str:
    """Write one event's rows in a single transaction; returns the note."""
    note = ("final event day may restate upward; weekly pull after the week closes is authoritative"
            if cov["is_complete"] else f"INCOMPLETE: {t.describe(cov)}")
    with conn:
        conn.execute("DELETE FROM amazon_event_asin WHERE event_id=?", (event_id,))
        conn.executemany(
            """INSERT INTO amazon_event_asin
               (event_id, asin, parent_asin, sessions, page_views, buy_box_pct,
                units_ordered, ordered_sales, range_start, range_end, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""", asins)
        conn.executemany(
            """INSERT OR REPLACE INTO amazon_traffic_daily
               (date, sessions, page_views, units_ordered, ordered_sales,
                total_orders, synced_at)
               VALUES (?,?,?,?,?,?,?)""", daily)
        conn.execute(
            """INSERT OR REPLACE INTO amazon_event_pull
               (event_id, range_start, range_end, days_expected, days_returned, missing_days,
                bydate_sales, byasin_sales, sections_gap, is_complete, note, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, start, end, cov["days_expected"], cov["days_returned"],
             ",".join(cov["missing_days"]), cov["bydate_sales"], cov["byasin_sales"],
             cov["sections_gap"], int(cov["is_complete"]), note, stamp))
        conn.execute(
            """INSERT OR REPLACE INTO dim_event_calendar
               (event_id, event_name, event_type, channel, start_date, end_date,
                discount_pct, mechanic, notes, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (event_id, name, "platform", channel, start, end, None, "deal",
             notes or note, stamp))
    return note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--event-id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--channel", default="amazon")
    ap.add_argument("--pad-days", type=int, default=1,
                    help="extra pre-event days of account-daily to store as a baseline")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()

    t.require_env()
    host = t.HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    pad_start = (date.fromisoformat(a.start) - timedelta(days=a.pad_days)).isoformat()

    # Two reports: byAsin must cover the event window ONLY; daily rows may carry a pad.
    with ThreadPoolExecutor(2) as ex:
        f_event = ex.submit(t._report, host, a.start, a.end)
        f_daily = ex.submit(t._report, host, pad_start, a.end) if pad_start != a.start else None
        ev = f_event.result()
        dl = f_daily.result() if f_daily else ev

    cov = t.coverage(ev, a.start, a.end)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    asins = asin_rows(a.event_id, ev, a.start, a.end, stamp)
    daily = daily_rows(dl, stamp)

    started = warehouse_db.now()
    conn = sqlite3.connect(t.DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    try:
        ensure_schema(conn)
        note = store_event(conn, event_id=a.event_id, name=a.name, start=a.start, end=a.end,
                           channel=a.channel, notes=a.notes, cov=cov, asins=asins,
                           daily=daily, stamp=stamp)
    finally:
        conn.close()
    print(f"event {a.event_id}: {len(asins)} asins, {len(daily)} daily rows; "
          f"{'COMPLETE' if cov['is_complete'] else 'INCOMPLETE'} "
          f"(byDate {cov['bydate_sales']:,.0f} / byAsin {cov['byasin_sales']:,.0f})")
    # An empty or short pull is never `ok` -- see the weekly module's rationale.
    status = "ok" if cov["is_complete"] and (asins or daily) else "degraded"
    warehouse_db.log_sync(PLATFORM, started, len(asins) + len(daily), status, note)
    return 0 if cov["is_complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
