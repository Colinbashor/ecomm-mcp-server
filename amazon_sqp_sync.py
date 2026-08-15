r"""
Amazon Brand Analytics Search Query Performance (SQP) -> warehouse.amazon_sqp.

"How do we win or lose the Amazon search shelf" at the query x ASIN grain:
per search query, your impression / click / cart-add / purchase SHARE vs the
whole query's totals across ALL sellers, plus your median click price vs the
market's. This is market-level data, not just your own sales — it's only
available to brand-registered sellers via Brand Analytics.

Requires SP-API Brand Analytics access (brand registry) in addition to the
regular SPAPI_* credentials used elsewhere in this project.

KEY TRAPS (probe-verified against the live API; re-verify if Amazon changes
report behavior):
  * reportOptions MUST include `asin` (space-delimited) AND `reportPeriod`.
    Omitting `asin` is FATAL, and the reason is only in the report DOCUMENT
    (warehouse/brand_analytics.py surfaces it).
  * BA weeks are SUNDAY-SATURDAY. --week takes the SUNDAY.
  * Shares/medians come back NULL when the underlying stage count is 0 —
    coalesce before you store them.
  * SQP shares are reported as PERCENT (e.g. 14.29), unlike the other four
    Brand Analytics reports (amazon_ba_sync.py), which report FRACTIONS
    (0.1429). Stored as-is here (percent). NEVER SUM a share across rows —
    average it, and only where that's meaningful.

COST CONTROL — SQP is easily the most expensive Brand Analytics report to run
at any real scale, and the four rules below exist to keep it from ballooning
into hundreds of report requests for one week:
  * ONE WEEK = MANY REPORTS. SQP is requested per-ASIN-batch. Empirically,
    batches of 25/50/100 ASINs each return 400 while batches of 12-13 ASINs
    succeed, so requesting even a modest ASIN list is several reports. Since
    each report can sit 15-25+ min in Amazon's queue, polling batches
    SEQUENTIALLY costs N x ~20 min. Batches here run CONCURRENTLY
    (MAX_IN_FLIGHT at a time, created CREATE_SPACING_SEC apart, kept under
    createReport's burst bucket), which pays the queue wait roughly once
    instead of once per batch.
  * PROBE THE WEEK BEFORE FANNING OUT. A BA week whose data hasn't published
    yet FATALs EVERY batch with a generic "A client error occurred" — which
    looks identical, per batch, to a genuinely bad payload. Naively halving
    and retrying every failed batch would turn one unpublished week into
    dozens to hundreds of doomed reports. Instead, the highest-priority batch
    is run alone first as a gate; if it AND one alternate/disjoint slice both
    fail, the week is declared unavailable after just two reports and
    --fallback-weeks can step back immediately.
  * HALVING IS BUDGETED. Splitting a failed batch 12->6->3->1 to isolate one
    bad ASIN can cost many reports on its own, so retries draw from a fixed
    MAX_RETRY_REPORTS budget per week; past that, a failing batch is skipped
    wholesale rather than split further. A week that produces NO rows after
    MAX_DEAD_BATCHES full-size batch failures is abandoned outright.
  * COVERAGE IS RECORDED, SO RUNS RESUME. `amazon_sqp_coverage` marks which
    (week, ASIN) pairs were actually attempted, so a killed or interrupted run
    continues instead of restarting from batch one. Note the report only ever
    returns ASINs that had search data for that query, so the ABSENCE of rows
    in `amazon_sqp` can never be used as the resume marker — coverage is a
    separate, deliberate record of what was asked, not what came back.
    `--refresh` ignores recorded coverage and re-requests everything.

WHICH ASINs TO REQUEST: like amazon_rank_sync.py, this scaffold has no
product/traffic table of its own to rank ASINs by recent sales or sessions
(that depends on your own catalog/sales data), so you provide the list
explicitly via `--asins` or `--asins-file`, in your own priority order —
`--max-asins` then caps how many of THAT list get requested per week (0 =
all). As a weak fallback when neither is given, this reuses
amazon_rank_sync.fallback_asins() (distinct SKUs seen in
amazon_fulfilled_shipments, if that table exists and has been populated by
amazon_fees_sync.py) — pass --asins/--asins-file for anything beyond a quick
smoke test. In practice, search-query yield across ASINs is often heavily
concentrated in a relatively small number of your best-selling products, so
prioritizing those first (rather than requesting your whole catalog) is
usually the efficient choice.

USAGE:
  python amazon_sqp_sync.py --asins B0EXAMPLE1,B0EXAMPLE2       # prior BA week (Sun-Sat)
  python amazon_sqp_sync.py --asins-file asins.txt --week 2026-06-28
  python amazon_sqp_sync.py --asins-file asins.txt --weeks 8    # backfill N BA weeks
  python amazon_sqp_sync.py --asins-file asins.txt --fallback-weeks 1  # step back if empty
  python amazon_sqp_sync.py --asins-file asins.txt --max-minutes 90    # wall-clock budget
  python amazon_sqp_sync.py --asins-file asins.txt --refresh            # ignore coverage
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from amazon_rank_sync import fallback_asins
from warehouse import db as warehouse_db
from warehouse.brand_analytics import (
    run_ba_report, check_ba_report, create_ba_report, fetch_ba_records,
    BAReportCancelled, BAReportFatal, CREATE_SPACING_SEC, DEFAULT_TIMEOUT_MIN,
    POLL_EVERY_SEC)

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

REPORT_TYPE = "GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT"
RECORDS_KEY = "dataByAsin"
# Empirically, requests of 25/50/100 ASINs consistently 400 while 12/13
# succeed. Start at the proven size and retain split-on-failure for edge cases.
INITIAL_BATCH = 12
# A reasonable default cap on how many of your given ASINs get requested per
# week — raise it if you want deeper coverage; concurrency makes that cheaper
# than it used to be, but it is still real API load.
DEFAULT_MAX_ASINS = 120
# Concurrency: kept well under brand_analytics.CREATE_BURST_LIMIT (15) so a
# full window of creates never drains createReport's burst bucket into 429s.
MAX_IN_FLIGHT = 8
# Floor on the halve-and-retry allowance per week (a full 12->1 isolation can
# cost over 20 reports on its own; without a cap a dead week fans out to
# hundreds). The effective budget is max(this, batch count) — see sync_ba_week.
MAX_RETRY_REPORTS = 12
# Full-size batch failures tolerated while ZERO rows have landed, before the
# week is declared dead. Only reachable if the probe passed but the week is
# partial.
MAX_DEAD_BATCHES = 3

# Week outcomes (drive the --fallback-weeks decision in main()).
STATE_OK = "ok"                    # the week ran; rows may still be 0
STATE_UNAVAILABLE = "unavailable"  # BA has not published this week — step back
STATE_ABORTED = "aborted"          # went dead mid-week
STATE_TIMEOUT = "timeout"          # wall-clock budget hit; resumable
STATE_DONE_ALREADY = "done_already"  # every in-scope ASIN already attempted

DDL = """
CREATE TABLE IF NOT EXISTS amazon_sqp (
    week_start              TEXT NOT NULL,   -- SUNDAY of the Brand Analytics (Sun-Sat) week
    asin                    TEXT NOT NULL,
    search_query            TEXT NOT NULL,
    query_score             INTEGER,
    query_volume            INTEGER,
    impressions_total       INTEGER,
    impressions_asin        INTEGER,
    impression_share        REAL,            -- PERCENT (SQP quirk), coalesced 0 at zero count
    clicks_total            INTEGER,
    clicks_asin             INTEGER,
    click_share             REAL,            -- PERCENT
    median_click_price_total REAL,
    median_click_price_asin  REAL,
    cart_adds_total         INTEGER,
    cart_adds_asin          INTEGER,
    cart_add_share          REAL,            -- PERCENT
    purchases_total         INTEGER,
    purchases_asin          INTEGER,
    purchase_share          REAL,            -- PERCENT
    synced_at               TEXT NOT NULL,
    PRIMARY KEY (week_start, asin, search_query)
);

-- Which (week, ASIN) pairs we have actually ASKED Amazon about. Distinct from
-- amazon_sqp, which only holds ASINs that HAD search data — so this is the only
-- valid resume marker (see the module docstring).
CREATE TABLE IF NOT EXISTS amazon_sqp_coverage (
    week_start   TEXT NOT NULL,   -- SUNDAY of the BA week
    asin         TEXT NOT NULL,
    status       TEXT NOT NULL,   -- 'done' (report returned) | 'skipped' (gave up)
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (week_start, asin)
);
"""


def _num(v):
    return 0.0 if v in (None, "") else float(v)


def _int(v):
    return 0 if v in (None, "") else int(v)


def _amount(d):
    """A {amount, currencyCode} money block -> float (NULL/absent at zero count)."""
    return _num((d or {}).get("amount"))


def _rows_from_report(records: list[dict], week_start: str, stamp: str) -> list[tuple]:
    out = []
    for rec in records:
        sq = rec.get("searchQueryData") or {}
        imp = rec.get("impressionData") or {}
        clk = rec.get("clickData") or {}
        cart = rec.get("cartAddData") or {}
        pur = rec.get("purchaseData") or {}
        asin = rec.get("asin") or imp.get("asin") or ""
        query = sq.get("searchQuery") or ""
        if not asin or not query:
            continue
        out.append((
            week_start, asin, query,
            _int(sq.get("searchQueryScore")), _int(sq.get("searchQueryVolume")),
            _int(imp.get("totalQueryImpressionCount")), _int(imp.get("asinImpressionCount")),
            _num(imp.get("asinImpressionShare")),
            _int(clk.get("totalClickCount")), _int(clk.get("asinClickCount")),
            _num(clk.get("asinClickShare")),
            _amount(clk.get("totalMedianClickPrice")), _amount(clk.get("asinMedianClickPrice")),
            _int(cart.get("totalCartAddCount")), _int(cart.get("asinCartAddCount")),
            _num(cart.get("asinCartAddShare")),
            _int(pur.get("totalPurchaseCount")), _int(pur.get("asinPurchaseCount")),
            _num(pur.get("asinPurchaseShare")),
            stamp,
        ))
    return out


def _attempted_asins(conn: sqlite3.Connection, week_start: str) -> set[str]:
    """ASINs already successfully requested for this week (the resume marker)."""
    return {
        r[0] for r in conn.execute(
            "SELECT asin FROM amazon_sqp_coverage WHERE week_start=? AND status='done'",
            (week_start,),
        )
    }


def _record_coverage(conn: sqlite3.Connection, week_start: str, asins: list[str],
                     status: str, stamp: str) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO amazon_sqp_coverage VALUES (?,?,?,?)",
            [(week_start, a, status, stamp) for a in asins],
        )


def _store_batch(conn: sqlite3.Connection, week_start: str, batch: list[str],
                 records: list[dict], stamp: str) -> int:
    """Persist one finished batch's rows AND its coverage, in that order."""
    rows = _rows_from_report(records, week_start, stamp)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO amazon_sqp VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    _record_coverage(conn, week_start, batch, "done", stamp)
    return len(rows)


def _probe_week(conn: sqlite3.Connection, ba_sunday: date, stamp: str,
                batches: list[list[str]],
                timeout_min: int = DEFAULT_TIMEOUT_MIN) -> tuple[bool, int, list[list[str]]]:
    """Gate on the highest-priority batch before fanning the week out.

    An unpublished BA week FATALs every batch with a generic client error, so
    running one batch alone distinguishes "week not ready" from "bad payload" at
    a cost of 1-2 reports instead of a full halving fan-out. A second, disjoint
    slice is tried before condemning the week, so one poison ASIN in the lead
    batch cannot mask a week that is genuinely available.

    Returns (week_available, rows_written, remaining_batches).
    """
    week_start = ba_sunday.isoformat()
    rows = 0
    for attempt, idx in enumerate([0, len(batches) // 2]):
        if idx >= len(batches):
            continue
        batch = batches[idx]
        if attempt:
            time.sleep(CREATE_SPACING_SEC)
        try:
            records = run_ba_report(REPORT_TYPE, ba_sunday,
                                    {"reportPeriod": "WEEK", "asin": " ".join(batch)},
                                    records_key=RECORDS_KEY, timeout_min=timeout_min)
        except BAReportCancelled:
            print(f"    sqp {week_start}: probe batch CANCELLED — no data for this week",
                  flush=True)
            return False, rows, []
        except (BAReportFatal, RuntimeError, TimeoutError) as e:
            print(f"    sqp {week_start}: probe batch {attempt + 1} failed "
                  f"({str(e)[:90]})", flush=True)
            continue
        rows += _store_batch(conn, week_start, batch, records, stamp)
        print(f"    sqp {week_start}: probe OK, +{rows} query rows "
              f"({len(batch)} ASINs) — week is published", flush=True)
        remaining = [b for i, b in enumerate(batches) if i != idx]
        return True, rows, remaining
    print(f"    sqp {week_start}: both probe batches failed — treating the BA week as "
          f"UNPUBLISHED (no fan-out)", flush=True)
    return False, rows, []


def sync_ba_week(
    conn: sqlite3.Connection,
    ba_sunday: date,
    stamp: str,
    asins: list[str],
    *,
    refresh: bool = False,
    deadline: float | None = None,
    timeout_min: int = DEFAULT_TIMEOUT_MIN,
) -> tuple[int, int, str]:
    """Pull SQP for one Sun-Sat BA week across the given ASINs.

    `asins` is the caller's already-decided target list (see the module
    docstring's "WHICH ASINs TO REQUEST" section) — this function does no
    ranking or selection of its own.

    Probes the week first (see _probe_week), then holds up to MAX_IN_FLIGHT
    batch reports open at once so their queue waits overlap. A batch that FATALs
    is halved and retried while the MAX_RETRY_REPORTS budget lasts, then skipped
    wholesale; the week is abandoned if MAX_DEAD_BATCHES full-size batches fail
    with nothing landed. Every attempted ASIN is recorded in amazon_sqp_coverage
    so an interrupted run resumes.

    Returns (rows_written, skipped_asins, state) — see the STATE_* constants.
    """
    week_start = ba_sunday.isoformat()
    if not asins:
        print(f"    sqp {week_start}: no ASINs given", flush=True)
        return 0, 0, STATE_OK

    scope = len(asins)
    if not refresh:
        already = _attempted_asins(conn, week_start)
        asins = [a for a in asins if a not in already]
        if not asins:
            print(f"    sqp {week_start}: all {scope} in-scope ASINs already pulled "
                  f"(use --refresh to re-request)", flush=True)
            return 0, 0, STATE_DONE_ALREADY
        if len(asins) < scope:
            print(f"    sqp {week_start}: resuming — {scope - len(asins)} of {scope} "
                  f"ASINs already pulled", flush=True)

    batches = [asins[i:i + INITIAL_BATCH] for i in range(0, len(asins), INITIAL_BATCH)]
    print(f"    sqp {week_start}: {len(asins)} ASINs in {len(batches)} batches of "
          f"{INITIAL_BATCH}, up to {MAX_IN_FLIGHT} reports in flight", flush=True)

    if len(batches) > 1:
        # The probe exists to stop a dead week fanning out across MANY batches;
        # with a single batch there is no fan-out to prevent, so skip the gate and
        # let the normal halving path isolate a bad ASIN.
        available, total, queue = _probe_week(conn, ba_sunday, stamp, batches, timeout_min)
        if not available:
            return total, 0, STATE_UNAVAILABLE
    else:
        total, queue = 0, list(batches)

    skipped_asins = 0
    # Scale the retry allowance with the legitimate work so a deep run over
    # your whole ASIN list is not starved by a budget sized for a small weekly
    # cohort, while still capping the week at roughly 2x its batch count
    # instead of the much larger cost an unbounded 12->1 isolation would incur.
    retry_budget = max(MAX_RETRY_REPORTS, len(batches))
    dead_batches = 0
    state = STATE_OK
    # A LIST, not a dict keyed by reportId: two batches must stay independent even
    # if Amazon ever returns a duplicate id, and the create-window guard below
    # depends on this growing by exactly one per create.
    in_flight: list[tuple[str, list[str], float]] = []  # (report_id, batch, created)

    def _fail(batch: list[str], reason: str) -> None:
        """Halve a failed batch if the retry budget allows, else skip it."""
        nonlocal retry_budget, skipped_asins
        if len(batch) > 1 and retry_budget >= 2:
            mid = len(batch) // 2
            queue[:0] = [batch[:mid], batch[mid:]]
            retry_budget -= 2
            print(f"    sqp {week_start}: batch of {len(batch)} failed, halving "
                  f"({retry_budget} retries left) ({reason[:70]})", flush=True)
        else:
            skipped_asins += len(batch)
            _record_coverage(conn, week_start, batch, "skipped", stamp)
            note = "retry budget spent" if len(batch) > 1 else "irrecoverable"
            print(f"    sqp {week_start}: skipping {len(batch)} ASIN(s) — {note} "
                  f"({reason[:70]})", flush=True)

    while queue or in_flight:
        if deadline and time.time() > deadline:
            pending = sum(len(b) for _, b, _ in in_flight) + sum(len(b) for b in queue)
            print(f"    sqp {week_start}: wall-clock budget reached — stopping with "
                  f"{pending} ASIN(s) unresolved (a re-run resumes)", flush=True)
            state = STATE_TIMEOUT
            break

        while queue and len(in_flight) < MAX_IN_FLIGHT:
            batch = queue.pop(0)
            try:
                rid = create_ba_report(REPORT_TYPE, ba_sunday,
                                       {"reportPeriod": "WEEK", "asin": " ".join(batch)})
            except RuntimeError as e:
                _fail(batch, str(e))
                continue
            in_flight.append((rid, batch, time.time()))
            time.sleep(CREATE_SPACING_SEC)  # stay inside createReport's burst bucket

        if not in_flight:
            continue
        time.sleep(POLL_EVERY_SEC)

        still_open: list[tuple[str, list[str], float]] = []
        for rid, batch, created in in_flight:
            status, payload = check_ba_report(rid)
            if status == "PENDING":
                if time.time() - created > timeout_min * 60:
                    _fail(batch, f"no terminal status within {timeout_min} min")
                else:
                    still_open.append((rid, batch, created))
                continue
            if status == "DONE":
                n = _store_batch(conn, week_start, batch,
                                 fetch_ba_records(payload, RECORDS_KEY), stamp)
                total += n
                print(f"    sqp {week_start}: +{n} query rows ({len(batch)} ASINs)",
                      flush=True)
                continue
            if status == "CANCELLED":
                # No data for these ASINs this week — a real answer, not a failure.
                _record_coverage(conn, week_start, batch, "done", stamp)
                print(f"    sqp {week_start}: batch of {len(batch)} CANCELLED (no data)",
                      flush=True)
                continue
            if len(batch) == INITIAL_BATCH and total == 0:
                dead_batches += 1
            _fail(batch, payload or "FATAL")
        in_flight = still_open

        if dead_batches >= MAX_DEAD_BATCHES and total == 0:
            pending = sum(len(b) for _, b, _ in in_flight) + sum(len(b) for b in queue)
            print(f"    sqp {week_start}: {dead_batches} full batches failed with zero rows "
                  f"— abandoning the week ({pending} ASIN(s) not attempted)", flush=True)
            state = STATE_ABORTED
            break

    return total, skipped_asins, state


def _prior_ba_sunday(today: date | None = None) -> date:
    """The most recently COMPLETED Brand Analytics week's Sunday."""
    today = today or date.today()
    # Most recent Sunday strictly in the past that starts a completed Sun-Sat week.
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    if last_sunday + timedelta(days=6) >= today:  # this week not finished yet
        last_sunday -= timedelta(days=7)
    return last_sunday


def _target_asins(args: argparse.Namespace, conn: sqlite3.Connection) -> list[str]:
    """Resolve the ASIN list to request, in the caller's priority order.

    See the module docstring's "WHICH ASINs TO REQUEST" section — this
    scaffold has no product/traffic table to rank ASINs by itself.
    """
    if args.asins:
        asins = [a.strip() for a in args.asins.split(",") if a.strip()]
    elif args.asins_file:
        asins = [ln.strip() for ln in Path(args.asins_file).read_text().splitlines() if ln.strip()]
    else:
        asins = fallback_asins(conn)
    if args.max_asins and args.max_asins > 0:
        asins = asins[:args.max_asins]
    return asins


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--asins", help="comma-separated ASINs to request, highest priority first")
    p.add_argument("--asins-file", help="path to a file with one ASIN per line, priority order")
    p.add_argument("--week", help="SUNDAY of the BA week (default: last completed Sun-Sat week)")
    p.add_argument("--weeks", type=int, default=1, help="backfill this many BA weeks back")
    p.add_argument("--fallback-weeks", type=int, default=0,
                   help="if a week returns 0 rows, step back up to this many BA weeks "
                        "(useful for a weekly run to guard the Monday-availability lag)")
    p.add_argument(
        "--max-asins",
        type=int,
        default=DEFAULT_MAX_ASINS,
        help=f"cap on how many of the given ASINs to request per week "
             f"(0 = all; default: {DEFAULT_MAX_ASINS})",
    )
    p.add_argument("--refresh", action="store_true",
                   help="re-request ASINs already recorded in amazon_sqp_coverage")
    p.add_argument("--max-minutes", type=int, default=0,
                   help="overall wall-clock budget; stops cleanly and resumes next run "
                        "(0 = unlimited). Useful in a scheduled run so SQP cannot block it.")
    args = p.parse_args()

    if args.week:
        base = date.fromisoformat(args.week)
        if base.weekday() != 6:
            raise SystemExit("--week must be a SUNDAY (Brand Analytics weeks are Sun-Sat).")
    else:
        base = _prior_ba_sunday()

    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(DDL)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    asins = _target_asins(args, conn)
    if not asins:
        conn.close()
        warehouse_db.log_sync("amazon_sqp", started, 0, "error", "no target ASINs")
        raise SystemExit(
            "No ASINs to request. Pass --asins A,B,C or --asins-file path.txt — "
            "this scaffold has no product/traffic table to default a list from."
        )

    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None

    total = 0
    skipped_asins = 0
    states: list[str] = []
    try:
        for i in range(args.weeks):
            wk = base - timedelta(weeks=i)
            n, skipped, state = sync_ba_week(
                conn, wk, stamp, asins,
                refresh=args.refresh,
                deadline=deadline,
            )
            # Monday-availability fallback (useful in a scheduled weekly run): the
            # just-closed BA week is often not queryable yet. Step back on
            # UNAVAILABLE (the probe said so in ~2 reports) or on a week that ran
            # clean but yielded nothing — never on DONE_ALREADY, which means the
            # week is complete.
            fb = 0
            while (state == STATE_UNAVAILABLE or (state == STATE_OK and n == 0)) \
                    and fb < args.fallback_weeks:
                fb += 1
                wk = wk - timedelta(weeks=1)
                print(f"    sqp: falling back to prior BA week {wk.isoformat()}", flush=True)
                n, skipped, state = sync_ba_week(
                    conn, wk, stamp, asins,
                    refresh=args.refresh,
                    deadline=deadline,
                )
            total += n
            skipped_asins += skipped
            states.append(state)
            if state == STATE_TIMEOUT:
                break  # budget spent; remaining weeks resume on the next run
    finally:
        conn.close()

    notes = []
    if skipped_asins:
        notes.append(f"{skipped_asins} ASIN(s) skipped after retries")
    for st, label in ((STATE_TIMEOUT, "wall-clock budget reached"),
                      (STATE_ABORTED, "week abandoned (no rows)"),
                      (STATE_UNAVAILABLE, "BA week not published")):
        if st in states:
            notes.append(label)
    # DONE_ALREADY with no new rows is a success: the week is fully covered.
    covered = total > 0 or STATE_DONE_ALREADY in states
    if not covered:
        status = "error"
    elif notes:
        status = "degraded"
    else:
        status = "ok"
    warehouse_db.log_sync("amazon_sqp", started, total, status, "; ".join(notes))
    print(f"Amazon SQP: wrote {total} rows [{status}]"
          + (f" — {'; '.join(notes)}" if notes else ""))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
