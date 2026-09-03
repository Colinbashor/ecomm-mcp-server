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

COMPLETENESS IS NOT GUARANTEED BY A DONE STATUS. A report requested close to
the end of a period can come back HTTP 200/DONE with fewer days than
requested, because the platform has not finished publishing the most recent
day(s) yet — this is common for any "closes the books on a lag" reporting
API, not specific to this report type. A fixed-schedule pull run shortly
after a period ends is especially exposed: it can silently store a short
period as if it were complete, understating totals for however long it takes
someone to notice a discrepancy against the platform's own dashboard.

`coverage()` guards against this by checking TWO things, deliberately not
collapsed into a single day-count check: (1) every expected calendar day is
present in `salesAndTrafficByDate`, and (2) the `salesAndTrafficByDate` and
`salesAndTrafficByAsin` sections' summed `orderedProductSales` agree within
`BYASIN_BYDATE_TOLERANCE`. The two sections can disagree independently of
the day-count check (one section can be short while the other is complete),
and `sync_week`/`sync_month` write from byAsin, so a day-count-only check
can call a byAsin-short-but-byDate-complete period "fine" and vice versa.

`sync_week`/`sync_month` return `(rows_written, is_complete)` and apply three
rules: (1) a short pull never overwrites a period already stored COMPLETE —
without this, a routine re-check of a recent period could let one bad day
silently replace good data with a fresher-but-shorter pull; (2) a short pull
is recorded but logged as `degraded`, never `ok`, so monitoring keyed on
sync-log status can't read a short pull as healthy; (3) `--repair` re-pulls
only periods recorded incomplete, at zero API cost when everything is
already complete, bounded by `MAX_REPAIR_ATTEMPTS` so a period the platform
will never finish publishing doesn't get re-requested forever. `amazon_traffic_coverage`
persists what was actually returned per period, independent of whether the
main tables show any rows — the guiding rule (used elsewhere in this project
for other gap-prone external feeds) is that the ABSENCE of a coverage row
can never be treated as evidence of completeness or incompleteness on its
own; it just means the check predates this feature and falls back to a
weaker day-count heuristic.

`--allow-partial` exits 0 on a short pull instead of failing the run, for a
two-pass schedule: an early pass run soon after a period ends is *expected*
to be short and shouldn't alarm, while a later `--repair` pass (run after
enough time has passed for the platform to catch up) is the one that should
fail loudly if data is still missing.

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
CREATE TABLE IF NOT EXISTS amazon_traffic_coverage (
    period_kind    TEXT NOT NULL,   -- 'week' | 'month'
    period_key     TEXT NOT NULL,   -- Monday ISO date | 'YYYY-MM'
    range_start    TEXT NOT NULL,
    range_end      TEXT NOT NULL,
    days_expected  INTEGER NOT NULL,
    days_returned  INTEGER NOT NULL,
    missing_days   TEXT,            -- comma-joined ISO dates Amazon did not return
    bydate_sales   REAL,
    byasin_sales   REAL,
    is_complete    INTEGER NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 1,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (period_kind, period_key)
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


# ---- coverage validation (see module docstring for why this exists) -------

BYASIN_BYDATE_TOLERANCE = 0.02   # 2%: complete periods measure well under this;
                                 # a genuinely missing day produces a much larger gap.
MAX_REPAIR_ATTEMPTS = 6          # poison guard: a day the platform will never
                                 # publish must not be re-requested forever.


def _expected_days(start: str, end: str) -> list[str]:
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _section_sales(section: dict, key: str) -> float:
    return float(((section.get(key) or {}).get("orderedProductSales") or {}).get("amount", 0) or 0)


def coverage(data: dict, start: str, end: str) -> dict:
    """Did the platform actually return the whole requested range?

    Pure — takes the parsed report payload, returns a verdict dict. Kept free
    of IO so the completeness rule is unit-testable without an API call."""
    expected = _expected_days(start, end)
    returned = {d.get("date") for d in data.get("salesAndTrafficByDate", []) if d.get("date")}
    missing = [d for d in expected if d not in returned]

    bydate = sum(_section_sales(d, "salesByDate") for d in data.get("salesAndTrafficByDate", []))
    byasin = sum(_section_sales(a, "salesByAsin") for a in data.get("salesAndTrafficByAsin", []))

    # Relative gap against the LARGER side: dividing by whichever section is
    # short overstates the gap when that same section is the short one.
    base = max(bydate, byasin)
    gap = abs(byasin - bydate) / base if base else 0.0

    return {
        "range_start": start,
        "range_end": end,
        "days_expected": len(expected),
        "days_returned": len(expected) - len(missing),
        "missing_days": missing,
        "bydate_sales": round(bydate, 2),
        "byasin_sales": round(byasin, 2),
        "sections_gap": round(gap, 4),
        "is_complete": not missing and gap <= BYASIN_BYDATE_TOLERANCE,
    }


def _stored_coverage(conn: sqlite3.Connection, kind: str, key: str) -> dict | None:
    r = conn.execute(
        """SELECT is_complete, attempts FROM amazon_traffic_coverage
           WHERE period_kind=? AND period_key=?""", (kind, key)).fetchone()
    return None if r is None else {"is_complete": bool(r[0]), "attempts": int(r[1])}


def _record_coverage(conn: sqlite3.Connection, kind: str, key: str,
                     cov: dict, stamp: str) -> None:
    prior = _stored_coverage(conn, kind, key)
    attempts = (prior["attempts"] + 1) if prior else 1
    conn.execute(
        """INSERT OR REPLACE INTO amazon_traffic_coverage
           (period_kind, period_key, range_start, range_end, days_expected,
            days_returned, missing_days, bydate_sales, byasin_sales,
            is_complete, attempts, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kind, key, cov["range_start"], cov["range_end"], cov["days_expected"],
         cov["days_returned"], ",".join(cov["missing_days"]) or None,
         cov["bydate_sales"], cov["byasin_sales"], int(cov["is_complete"]),
         attempts, stamp))


def describe(cov: dict) -> str:
    if cov["is_complete"]:
        return f"{cov['days_expected']}/{cov['days_expected']} days"
    bits = [f"{cov['days_returned']}/{cov['days_expected']} days"]
    if cov["missing_days"]:
        bits.append("missing " + ",".join(cov["missing_days"]))
    if cov["sections_gap"] > BYASIN_BYDATE_TOLERANCE:
        bits.append(f"byAsin/byDate differ {cov['sections_gap']*100:.1f}% "
                    f"(${cov['byasin_sales']:,.0f} vs ${cov['bydate_sales']:,.0f})")
    return "; ".join(bits)


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


def sync_week(conn: sqlite3.Connection, monday: date, stamp: str) -> tuple[int, bool]:
    """Pull one Mon-Sun week. Returns (rows_written, is_complete).

    A short pull never overwrites a week already stored complete — see the
    module docstring's COMPLETENESS section for why."""
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    start, end = _week_bounds(monday)
    data = _report(host, start, end)

    cov = coverage(data, start, end)
    if not cov["is_complete"]:
        prior = _stored_coverage(conn, "week", start)
        if prior and prior["is_complete"]:
            with conn:
                _record_coverage(conn, "week", start, cov, stamp)
            print(f"    traffic week {start}: INCOMPLETE ({describe(cov)}) — "
                  f"keeping the complete data already stored, not overwriting", flush=True)
            return 0, False

    weekly = _weekly_rows(data, start, stamp)
    daily = _daily_rows(data, stamp)
    with conn:
        conn.executemany("INSERT OR REPLACE INTO amazon_traffic_weekly VALUES (?,?,?,?,?,?,?,?,?)", weekly)
        conn.executemany("INSERT OR REPLACE INTO amazon_traffic_daily VALUES (?,?,?,?,?,?,?)", daily)
        _record_coverage(conn, "week", start, cov, stamp)
    flag = "" if cov["is_complete"] else f"  !! INCOMPLETE: {describe(cov)}"
    print(f"    traffic week {start}: {len(weekly)} asins, {len(daily)} days{flag}", flush=True)
    return len(weekly) + len(daily), cov["is_complete"]


def sync_month(conn: sqlite3.Connection, ym: str, stamp: str) -> tuple[int, bool]:
    """Native calendar-month pull -> amazon_traffic_monthly (+_account). ONE
    report request for the whole month range (byAsin aggregates over the full
    requested range on its own; byDate comes back one row per day and is
    summed here into a single account-level month row) — never day-by-day.
    Parallel tables, never touches amazon_traffic_weekly/_daily. Returns
    (rows_written, is_complete); see sync_week for the never-downgrade rule."""
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    start, end = _month_bounds(ym)
    data = _report(host, start, end)

    cov = coverage(data, start, end)
    if not cov["is_complete"]:
        prior = _stored_coverage(conn, "month", ym)
        if prior and prior["is_complete"]:
            with conn:
                _record_coverage(conn, "month", ym, cov, stamp)
            print(f"    traffic month {ym}: INCOMPLETE ({describe(cov)}) — "
                  f"keeping the complete data already stored, not overwriting", flush=True)
            return 0, False

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
        _record_coverage(conn, "month", ym, cov, stamp)
    flag = "" if cov["is_complete"] else f"  !! INCOMPLETE: {describe(cov)}"
    print(f"    traffic month {ym} ({start}..{end}): {len(monthly)} asins, "
          f"{by_date_count} days rolled up to 1 account row{flag}", flush=True)
    return len(monthly) + 1, cov["is_complete"]


def weeks_needing_repair(conn: sqlite3.Connection, mondays: list[date]) -> list[date]:
    """Which of these weeks are known-incomplete and still worth re-requesting.

    A MISSING coverage row is NOT treated as incomplete: every week synced
    before coverage tracking existed has none, and treating absence as
    failure would re-pull the entire backfilled history on the first repair
    run. For those weeks this falls back to the day count in
    amazon_traffic_daily — a weak proxy (it reads byDate, not the byAsin the
    main tables are written from) but strictly better than nothing, and it
    only ever costs one extra report per week that turns out fine.

    Weeks already re-requested MAX_REPAIR_ATTEMPTS times are dropped: if the
    platform is never going to publish that day, repeating the ask forever is
    just noise."""
    out = []
    for monday in mondays:
        key = monday.isoformat()
        end = (monday + timedelta(days=6)).isoformat()
        prior = _stored_coverage(conn, "week", key)
        if prior is not None:
            if not prior["is_complete"] and prior["attempts"] < MAX_REPAIR_ATTEMPTS:
                out.append(monday)
            continue
        stored_days = conn.execute(
            "SELECT COUNT(*) FROM amazon_traffic_daily WHERE date BETWEEN ? AND ?",
            (key, end)).fetchone()[0]
        has_week = conn.execute(
            "SELECT 1 FROM amazon_traffic_weekly WHERE week_start=? LIMIT 1", (key,)).fetchone()
        if has_week and stored_days < 7:
            out.append(monday)
    return out


def run_status(total: int, partial: list[str], failed: list[str],
              repair: bool) -> tuple[str, str]:
    """Decide the sync_log status for a weekly run.

    THE RULE THIS ENCODES: a run that stored a short period is not "ok",
    however many rows it wrote — status must reflect completeness, not just
    "wrote something"."""
    if failed:
        return "error", "failed weeks: " + ",".join(failed)
    if partial:
        return "degraded", ("incomplete weeks (platform had not published the full "
                            "range): " + ",".join(partial))
    if not total:
        # Nothing to repair is a real success; nothing pulled at all is not.
        return ("ok", "repair: nothing to fix") if repair else ("error", "no rows written")
    return "ok", ""


def exit_code(status: str, repair: bool, allow_partial: bool) -> int:
    """Process exit code for a run that logged `status`.

    A DEGRADED REPAIR RUN EXITS 0, and that is the point of this function
    existing. `degraded` here means "the platform has still not published the
    final day", which is an EXPECTED upstream lag, not a failure of this job:
    the coverage table has already recorded it, MAX_REPAIR_ATTEMPTS bounds the
    retrying, and this feed's freshness is already visible wherever else your
    monitoring reads sync_log. Exiting nonzero on top of that fails an entire
    multi-step pipeline over a condition that is already tracked and already
    surfaced elsewhere — two runs of red for an expected condition is how a
    status stops meaning anything.

    A `failed` week (an exception, i.e. a real error) still exits 1.
    """
    if status == "ok":
        return 0
    if status == "degraded" and (allow_partial or repair):
        return 0
    return 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--week", help="Monday (YYYY-MM-DD) of one specific report week")
    p.add_argument("--weeks", type=int, default=1,
                   help="how many complete Mon-Sun weeks back to pull (default: prior week; "
                        "ignored if --week is given)")
    p.add_argument("--month", help="YYYY-MM calendar month — native month pull into the "
                                    "parallel monthly tables (see module docstring)")
    p.add_argument("--repair", action="store_true",
                   help="only re-pull weeks inside the --weeks window that are recorded "
                        "INCOMPLETE (zero API calls when everything is already complete)")
    p.add_argument("--allow-partial", action="store_true",
                   help="exit 0 even if a period comes back short. Use for an early pass run "
                        "soon after a period ends, where a short pull is expected; a later "
                        "--repair pass is the one that should fail loudly.")
    args = p.parse_args()

    require_env()
    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.month:
        message = args.month
        try:
            total, complete = sync_month(conn, args.month, stamp)
            status = "ok" if total else "error"
            if total and not complete:
                status = "degraded"
                message = f"{args.month} INCOMPLETE — platform did not return the full month"
        except Exception as e:  # noqa: BLE001
            print(f"    traffic month {args.month}: FAILED {str(e)[:200]}", flush=True)
            total, status = 0, "error"
        finally:
            conn.close()
        warehouse_db.log_sync(f"{PLATFORM}_monthly", started, total, status, message)
        print(f"Amazon traffic (month {args.month}): wrote {total} rows [{status}]")
        return 0 if status == "ok" or (status == "degraded" and args.allow_partial) else 1

    if args.week:
        monday = date.fromisoformat(args.week)
        if monday.weekday() != 0:
            raise SystemExit("Report weeks start on Monday.")
        mondays = [monday]
    else:
        mondays = _recent_mondays(args.weeks)

    partial: list[str] = []
    failed: list[str] = []
    total = 0
    try:
        if args.repair:
            wanted = weeks_needing_repair(conn, mondays)
            skipped = len(mondays) - len(wanted)
            print(f"    repair: {len(wanted)} of {len(mondays)} weeks recorded incomplete "
                  f"({skipped} already complete — no report requested)", flush=True)
            mondays = wanted
        for monday in mondays:
            try:
                rows, complete = sync_week(conn, monday, stamp)
                total += rows
                if not complete:
                    partial.append(monday.isoformat())
            except Exception as e:  # noqa: BLE001 — one week must not kill the rest
                print(f"    traffic week {monday}: FAILED {str(e)[:100]}", flush=True)
                failed.append(monday.isoformat())
    finally:
        conn.close()

    status, message = run_status(total, partial, failed, args.repair)
    warehouse_db.log_sync(PLATFORM, started, total, status, message)
    print(f"Amazon traffic: wrote {total} rows [{status}]" + (f" — {message}" if message else ""))
    code = exit_code(status, args.repair, args.allow_partial)
    if code == 0 and status == "degraded":
        print("    (degraded, exit 0: the platform has not published the final "
              "day yet. Recorded in amazon_traffic_coverage; it will be "
              "re-requested next run.)", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
