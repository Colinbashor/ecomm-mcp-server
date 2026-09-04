#!/usr/bin/env python
"""Backfill the Top Search Terms grain of amazon_ba_sync.py, week by week.

WHY THIS SCRIPT EXISTS SEPARATELY FROM amazon_ba_sync.py's own --weeks FLAG.
amazon_ba_sync.py's `--weeks N` loop is meant for routine catch-up (a handful
of weeks) and paces every grain identically. The Top Search Terms report (b)
is different in two ways that make a dedicated backfill driver worth having:
it is by far the most expensive of the four Brand Analytics reports to
re-request (a market-wide document that can run into the millions of
records), and — if you're using the optional `term_topics` capture described
in `brand_watchlist.yaml` and `amazon_ba_sync.py`'s docstring — it is the
*only* Brand Analytics grain that can answer a market-research question
("what terms is the whole market searching in some product area, regardless
of who sells them") rather than an own-performance one. That makes a deep,
patient backfill of this one grain worth running as a standalone job, the
same way this project treats other slow, resumable backfills (see e.g. any
connector whose docstring describes a multi-hour or multi-day catch-up run).

RETENTION IS UNDOCUMENTED BY AMAZON AND MUST BE DISCOVERED EMPIRICALLY.
Amazon does not publish how far back a given Brand Analytics report type
will actually answer for your account — it varies by report type and can
change over time. Probing a handful of candidate weeks at increasing
historical depth (a month back, six months, a year, eighteen months, and so
on) and seeing which ones return data is the practical way to find your
account's real floor; expect something on the order of several months to a
little over a year, not the full history of your Seller Central account.

!! A WEEK PAST RETENTION IS NOT GUARANTEED TO COME BACK **CANCELLED** — IT CAN
COME BACK **FATAL**, WITH THE SAME GENERIC MESSAGE AN UNPUBLISHED, TOO-RECENT
WEEK ALSO PRODUCES. See the "retention window" note in
`warehouse/brand_analytics.py`'s module docstring for the full explanation.
Practically, this means a naive walk-back that stops only when it sees
`BAReportCancelled` can run forever against a report/account where the
retention floor manifests as FATAL instead. This driver stops after
`MAX_CONSECUTIVE_MISSES` weeks in a row that yield zero rows for ANY reason
(FATAL, CANCELLED, or a genuinely empty week), and reports which weeks those
were rather than asserting a specific cause — you may want to spot-check a
couple of the "missed" weeks by hand if the exact reason matters to you.

THE CREATE-REPORT THROTTLE CAN BE TIGHTER THAN THE DOCUMENTED BURST ALLOWANCE
IMPLIES. `warehouse/brand_analytics.py`'s `CREATE_BURST_LIMIT` documents the
burst bucket, but the *sustained* rate observed in practice on at least one
account was closer to roughly one createReport call per minute — a handful of
reports fired a few seconds apart can 429 well before the documented burst
limit is reached. This driver paces sequential requests at `SPACING_SEC`
(conservatively above one minute) rather than trying to fan multiple weeks'
reports out concurrently — if you want to probe several candidate depths at
once to find your retention floor faster, keep any concurrent fan-out well
under one request/minute per in-flight report, and expect some 429s anyway.

RESUME IS FREE AND IS THE POINT. A week that already holds rows is skipped
on a re-run, so a killed run just continues where it left off and costs
nothing extra. Each week commits independently. Expect each week to take
roughly 15-25+ minutes of Amazon's own report-queue time (see the shared
runner's docstring) — this is a babysitter-style job meant to run for a long
time unattended, not something to invoke inline from another script.

USAGE
  python amazon_ba_backfill.py --asins-file asins.txt              # walk back to the floor
  python amazon_ba_backfill.py --asins-file asins.txt --weeks 12   # bounded run
  python amazon_ba_backfill.py --asins-file asins.txt --start 2025-09-07
  python amazon_ba_backfill.py --status                            # what is stored; no API calls
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

import amazon_ba_sync as BA
from warehouse import db as warehouse_db

DB = BA.DB
# Roughly 1 create/minute sustained is the safe assumption — see the module
# docstring. A few seconds of spacing has been observed to 429 well before
# the documented burst limit is reached.
SPACING_SEC = 65
# Consecutive weeks that must yield nothing before this driver calls it the
# retention floor. More than one, because a single empty week can be a
# transient FATAL rather than the true edge of retention.
MAX_CONSECUTIVE_MISSES = 3
# Do not walk past this many weeks even if Amazon keeps answering — a sane
# ceiling on how deep any account's retention is likely to reach.
MAX_WEEKS = 60


def stored_weeks(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT week_start FROM amazon_ba_search_terms")}


def topic_rows(conn) -> int:
    """Rows kept via the optional topic-capture rule (see brand_watchlist.yaml)
    rather than because one of your own ASINs or brand names matched — the
    market-research signal this backfill is usually run to build up."""
    return conn.execute(
        "SELECT COUNT(*) FROM amazon_ba_search_terms "
        "WHERE match_reason LIKE 'topic:%'").fetchone()[0]


def status(conn) -> None:
    weeks = sorted(stored_weeks(conn))
    total = conn.execute(
        "SELECT COUNT(*) FROM amazon_ba_search_terms").fetchone()[0]
    print(f"amazon_ba_search_terms: {total:,} rows across {len(weeks)} weeks")
    if weeks:
        print(f"  span: {weeks[0]} .. {weeks[-1]}")
    print(f"  topic-capture rows: {topic_rows(conn):,}")
    by_reason = conn.execute(
        "SELECT match_reason, COUNT(*) FROM amazon_ba_search_terms "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    for reason, n in by_reason:
        print(f"    {reason or '(null)':16s} {n:>9,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--asins", help="comma-separated ASINs that are 'ours' (see amazon_ba_sync.py)")
    ap.add_argument("--asins-file", help="path to a file with one ASIN per line")
    ap.add_argument("--weeks", type=int, default=MAX_WEEKS,
                    help=f"max weeks to walk back (default {MAX_WEEKS})")
    ap.add_argument("--start", help="SUNDAY to start from (default: last complete BA week)")
    ap.add_argument("--status", action="store_true",
                    help="print what is stored and exit; makes no API calls")
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull weeks already stored (default: skip them)")
    args = ap.parse_args()

    warehouse_db.init_db()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(BA.DDL)

    if args.status:
        status(conn)
        conn.close()
        return 0

    base = (date.fromisoformat(args.start) if args.start
            else BA._prior_ba_sunday())
    if base.weekday() != 6:
        raise SystemExit("--start must be a SUNDAY (BA weeks are Sun-Sat).")

    our_asins = set(BA._target_asins(args, conn))

    have = set() if args.refresh else stored_weeks(conn)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    before_topic = topic_rows(conn)

    misses = 0
    landed = 0
    done = 0
    print(f"walking back from {base}, max {args.weeks} weeks; "
          f"{len(have)} already stored", flush=True)

    for i in range(args.weeks):
        wk = base - timedelta(weeks=i)
        key = wk.isoformat()
        if key in have:
            print(f"  {key}  skip (already stored)", flush=True)
            continue
        if done:
            time.sleep(SPACING_SEC)
        try:
            n = BA.sync_search_terms(conn, wk, stamp, our_asins)
        except Exception as exc:                              # noqa: BLE001
            n = 0
            print(f"  {key}  ERROR {str(exc)[:120]}", flush=True)
        done += 1
        landed += n
        if n:
            misses = 0
            print(f"  {key}  {n:,} rows", flush=True)
        else:
            misses += 1
            print(f"  {key}  no rows ({misses}/{MAX_CONSECUTIVE_MISSES})",
                  flush=True)
            if misses >= MAX_CONSECUTIVE_MISSES:
                print(f"\nstopping: {MAX_CONSECUTIVE_MISSES} consecutive weeks "
                      f"returned nothing -- treating that as the retention "
                      f"floor.\nNOTE an out-of-retention week can answer FATAL "
                      f"instead of CANCELLED (see the module docstring), so "
                      f"this counts misses rather than waiting for a signal "
                      f"that isn't guaranteed to come.", flush=True)
                break

    gained = topic_rows(conn) - before_topic
    print(f"\n[done] {done} week(s) requested, {landed:,} rows stored, "
          f"{gained:,} of them via topic capture", flush=True)
    status(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
