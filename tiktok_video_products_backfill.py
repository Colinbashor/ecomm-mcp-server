r"""Backfill tiktok_shop_video_products (every product a TikTok video tags)
across the window TikTok's video-analytics API still serves.

WHY: tiktok_shop_videos stores only the FIRST tagged product of a video, so a
creator's video for its 2nd..Nth product could not be linked to that product.
tiktok_videos_sync.py writes every tag going forward; this pulls the history
the API still has (roughly six months -- see RETENTION_DAYS).

WHAT IT WRITES: tiktok_shop_video_products + tiktok_shop_video_products_coverage
ONLY. It never touches tiktok_shop_videos: re-pulled windows would restate
per-window metrics that reports may already read, and a window clipped at the
retention floor would overwrite complete rows with partial ones.

HOW:
  * Calendar-month windows, OLDEST FIRST (the oldest is the one TikTok drops
    next). The first starts at the retention floor and the last ends tomorrow.
  * A window refused with business code 28001022 (starts before the lookback)
    is recorded `out_of_retention`; for the FIRST window the start steps
    forward a day and retries, since the floor moves daily in the shop's
    timezone.
  * Resume is driven by the coverage table, never by absent tag rows: a window
    is skipped when a `complete` coverage row (from this script or the daily
    sync) already spans it -- a window can legitimately contain no
    multi-product video. short / empty / failed windows are retried up to
    MAX_ATTEMPTS, then left alone (poison guard).
  * Each window is fetched with NO database lock held, then written in one
    short transaction.

Exit codes: 0 ok, 75 degraded (a short/empty window, or nothing completed),
1 error (a window failed).

Long-running (an hour or more for both account types on a busy shop): run it
detached (a scheduled task / nohup), not from an SSH session that might close.

USAGE:
  python tiktok_video_products_backfill.py
  python tiktok_video_products_backfill.py --start 2026-04-01 --account-types AFFILIATES
  python tiktok_video_products_backfill.py --status      # print coverage, read-only
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

import tiktok_videos_sync as tvs
from warehouse import db

PLATFORM = "tiktok_video_products_backfill"
RETENTION_DAYS = 178        # approx. earliest accepted start_date_ge, in days back from today
FLOOR_STEP_LIMIT = 10       # days the first window may step forward on 28001022
MAX_ATTEMPTS = 4            # poison guard for a window that keeps coming back short/empty/failed
RETRYABLE = ("short", "empty", "failed")


def month_windows(start: date, end: date) -> list[tuple[str, str]]:
    """[start, end) cut at calendar-month boundaries, oldest first."""
    out, s = [], start
    while s < end:
        nxt = (s.replace(day=1) + timedelta(days=32)).replace(day=1)
        e = min(nxt, end)
        out.append((s.isoformat(), e.isoformat()))
        s = e
    return out


def covered(conn, account_type: str, start: str, end: str) -> bool:
    """A `complete` pull already spans [start, end) -- by this script or the
    daily sync. Coverage rows, not tag rows: a window can legitimately hold no
    multi-product video."""
    return conn.execute(
        "SELECT 1 FROM tiktok_shop_video_products_coverage WHERE account_type = ? "
        "AND status = 'complete' AND window_start <= ? AND window_end >= ? LIMIT 1",
        (account_type, start, end)).fetchone() is not None


def prior(conn, account_type: str, start: str, end: str):
    return conn.execute(
        "SELECT status, attempts FROM tiktok_shop_video_products_coverage "
        "WHERE account_type = ? AND window_start = ? AND window_end = ?",
        (account_type, start, end)).fetchone()


def should_pull(conn, account_type: str, start: str, end: str, force: bool) -> tuple[bool, str]:
    if force:
        return True, "forced"
    if covered(conn, account_type, start, end):
        return False, "already complete"
    p = prior(conn, account_type, start, end)
    if p and p[0] == "out_of_retention":
        return False, "out of retention (final)"
    if p and p[0] in RETRYABLE and p[1] >= MAX_ATTEMPTS:
        return False, f"{p[0]} after {p[1]} attempts (poison guard)"
    return True, "pending"


def pull_window(conn, account_type: str, start: str, end: str) -> tvs.FetchResult | str:
    """Fetch one window (no lock held) and write its tags + coverage. Returns the
    FetchResult, or 'out_of_retention' / 'failed'."""
    try:
        res = tvs.fetch_full(account_type, start, end)
    except RuntimeError as e:
        status = "out_of_retention" if str(tvs.OUT_OF_RETENTION_CODE) in str(e) else "failed"
        with conn:
            tvs.record_coverage(conn, account_type, start, end, status, "backfill", error=str(e)[:500])
        return status
    stamp = db.now()
    with conn:
        tvs.write_product_tags(conn, res.tags, start, end, stamp)
        tvs.record_coverage(conn, account_type, start, end, res.coverage_status(), "backfill", res,
                            stamp=stamp)
    return res


def run(conn, start: date, end: date, account_types: list[str], force: bool = False) -> dict:
    """Pull every pending window; returns {status: count} over the windows asked."""
    tally: dict[str, int] = {}
    windows = month_windows(start, end)
    for i, (s, e) in enumerate(windows):
        for at in account_types:
            ws, steps = s, 0
            if i == 0 and not force:
                # the floor moves daily: walk past starts TikTok already refused
                while steps < FLOOR_STEP_LIMIT and (p := prior(conn, at, ws, e)) and p[0] == "out_of_retention":
                    ws = (date.fromisoformat(ws) + timedelta(days=1)).isoformat()
                    steps += 1
                if ws >= e:
                    continue
            ok, why = should_pull(conn, at, ws, e, force)
            if not ok:
                print(f"  {at:15s} {ws} -> {e}: skip ({why})", flush=True)
                tally["skipped"] = tally.get("skipped", 0) + 1
                continue
            while True:
                t0 = time.monotonic()
                out = pull_window(conn, at, ws, e)
                secs = time.monotonic() - t0
                if out == "out_of_retention" and i == 0 and steps < FLOOR_STEP_LIMIT and ws < e:
                    # the floor moved (it advances daily): step forward and retry
                    print(f"  {at:15s} {ws} -> {e}: before TikTok's lookback, stepping forward a day",
                          flush=True)
                    ws = (date.fromisoformat(ws) + timedelta(days=1)).isoformat()
                    steps += 1
                    if ws >= e:
                        break
                    continue
                break
            status = out if isinstance(out, str) else out.coverage_status()
            tally[status] = tally.get(status, 0) + 1
            if isinstance(out, str):
                print(f"  {at:15s} {ws} -> {e}: {status} ({secs:.0f}s)", flush=True)
            else:
                print(f"  {at:15s} {ws} -> {e}: {status}  videos={len(out.video_ids):,} "
                      f"api_total={out.api_total} multi={out.multi_product_videos:,} "
                      f"tags={len(out.tags):,} ({secs:.0f}s)", flush=True)
    return tally


def run_status(tally: dict) -> str:
    """failed -> error; anything short / empty, or nothing complete -> degraded."""
    if tally.get("failed"):
        return "error"
    if tally.get("short") or tally.get("empty"):
        return "degraded"
    if not tally.get("complete") and not tally.get("skipped"):
        return "degraded"
    return "ok"


def print_status(conn) -> None:
    for r in conn.execute("""
        SELECT account_type, window_start, window_end, status, videos_returned, api_total,
               multi_product_videos, product_rows, attempts, source, last_attempt_at
        FROM tiktok_shop_video_products_coverage ORDER BY account_type, window_start, window_end"""):
        print(" | ".join("" if v is None else str(v) for v in r))
    for r in conn.execute("""
        SELECT account_type, COUNT(DISTINCT video_id), COUNT(*),
               COUNT(DISTINCT CASE WHEN position > 1 THEN video_id END), MIN(first_seen), MAX(last_seen)
        FROM tiktok_shop_video_products GROUP BY 1"""):
        print(f"tags {r[0]}: {r[1]:,} videos, {r[2]:,} video x product rows, {r[3]:,} videos with a "
              f"2nd+ product, seen {r[4]} -> {r[5]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", help=f"default: today - {RETENTION_DAYS} days (the retention floor)")
    ap.add_argument("--end", help="exclusive; default tomorrow")
    ap.add_argument("--account-types", default="AFFILIATES,LINKED_ACCOUNTS")
    ap.add_argument("--force", action="store_true", help="re-pull windows already complete")
    ap.add_argument("--status", action="store_true", help="print coverage and exit (read-only)")
    args = ap.parse_args(argv)

    if not args.status:
        tvs.check_required_env()
    db.init_db()
    conn = db.connect()
    try:
        with conn:
            tvs.ensure_products_tables(conn)
        if args.status:
            print_status(conn)
            return 0
        today = date.today()
        start = date.fromisoformat(args.start) if args.start else today - timedelta(days=RETENTION_DAYS)
        end = date.fromisoformat(args.end) if args.end else today + timedelta(days=1)
        types = [t.strip() for t in args.account_types.split(",") if t.strip()]
        started = db.now()
        print(f"TikTok video products backfill {start} -> {end} [{', '.join(types)}]", flush=True)
        try:
            tally = run(conn, start, end, types, force=args.force)
        except Exception as e:  # noqa: BLE001
            db.log_sync(PLATFORM, started, 0, "error", f"{type(e).__name__}: {e}")
            raise
        status = run_status(tally)
        n_tags = conn.execute("SELECT COUNT(*) FROM tiktok_shop_video_products").fetchone()[0]
        db.log_sync(PLATFORM, started, n_tags, status, f"{start} -> {end} {tally}")
        print(f"done: {tally} -> {status}; tiktok_shop_video_products now {n_tags:,} rows", flush=True)
        print_status(conn)
        return {"ok": 0, "degraded": 75}.get(status, 1)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
