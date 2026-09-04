"""Backfill every dated period folder in a production-tracking archive into
`factory_production_status`.

Walks <archive_root>/<year folder>/<month folder>/<dated period folder>/*.xlsx|*.xls,
reusing `factory_status_import.process_folder()` per period on one shared
connection. A bad file or a bad period is recorded and skipped rather than
aborting the whole run -- across a real multi-year archive spanning many
vendors and hundreds of files, some file being corrupted or mid-edit
somewhere is a near-certainty, and one bad file shouldn't cost you the rest
of the backfill.

FOLDER NAMING TOLERANCE: the dated period folder is parsed two ways, and
real archives commonly mix both across different years -- an older
convention that names a folder like "March 15" with no year at all (the
year then has to come from the grandparent year folder), and a newer
convention that spells the full date numerically (e.g. "03.15.2026"). Adjust
`NUMERIC_DATE_RE` / `MONTH_NAME_RE` / `YEAR_IN_PARENT_RE` below if your own
archive uses a different naming convention.

Usage:
    factory_status_backfill.py <archive_root> [--dry-run] [--since YYYY-MM-DD] [--limit N]
"""

import argparse
import os
import re
import sqlite3
import sys
import time

import factory_status_import as fsi

# A dated period folder is matched two ways, since real archives commonly mix
# both across different years: a fully-numeric date (e.g. "03.15.2026") or a
# bare month name + day with the year coming from the grandparent year folder
# (e.g. a "March 15" folder inside a "2024" folder).
NUMERIC_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
MONTH_NAME_RE = re.compile(
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
YEAR_IN_PARENT_RE = re.compile(r"(20\d{2})")


def parse_snapshot_date(date_dir_name, year_folder_name):
    m = NUMERIC_DATE_RE.search(date_dir_name)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    m = MONTH_NAME_RE.search(date_dir_name)
    if m:
        month_key = m.group(1)[:3].lower()
        mm = MONTH_NUM.get(month_key)
        dd = int(m.group(2))
        ym = YEAR_IN_PARENT_RE.search(year_folder_name)
        if mm and ym:
            return f"{ym.group(1)}-{mm:02d}-{dd:02d}"
    return None


def find_period_folders(archive_root):
    """Return [(snapshot_date, folder_path), ...] sorted oldest-first."""
    out = []
    unparsed = []
    for year_dir in sorted(os.listdir(archive_root)):
        year_path = os.path.join(archive_root, year_dir)
        if not os.path.isdir(year_path):
            continue
        for month_dir in sorted(os.listdir(year_path)):
            month_path = os.path.join(year_path, month_dir)
            if not os.path.isdir(month_path):
                continue
            for date_dir in sorted(os.listdir(month_path)):
                date_path = os.path.join(month_path, date_dir)
                if not os.path.isdir(date_path):
                    continue
                snapshot_date = parse_snapshot_date(date_dir.strip(), year_dir)
                if not snapshot_date:
                    unparsed.append(date_path)
                    continue
                out.append((snapshot_date, date_path))
    if unparsed:
        print(f"WARNING: {len(unparsed)} folders had no parseable date and were skipped:", file=sys.stderr)
        for u in unparsed:
            print(f"  {u}", file=sys.stderr)
    out.sort(key=lambda t: t[0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archive_root", help="the outer archive folder containing the year subfolders")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", help="only process periods with snapshot_date >= this (YYYY-MM-DD)")
    ap.add_argument("--until", help="only process periods with snapshot_date <= this (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="stop after N periods (for testing)")
    args = ap.parse_args()

    periods = find_period_folders(args.archive_root)
    if args.since:
        periods = [p for p in periods if p[0] >= args.since]
    if args.until:
        periods = [p for p in periods if p[0] <= args.until]
    if args.limit:
        periods = periods[:args.limit]

    print(f"Found {len(periods)} period folders to process")

    conn = sqlite3.connect(fsi.DB_PATH)
    conn.execute(fsi.DDL)
    conn.commit()

    totals = {"periods_ok": 0, "periods_failed": 0, "rows": 0, "matched": 0, "file_errors": 0}
    period_summaries = []
    t0 = time.time()

    for i, (snapshot_date, folder) in enumerate(periods, start=1):
        t_period = time.time()
        try:
            stats = fsi.process_folder(folder, conn, snapshot_date=snapshot_date,
                                       dry_run=args.dry_run, verbose=False)
        except Exception as e:
            print(f"[{i}/{len(periods)}] {snapshot_date}  FOLDER FAILED: {e}", file=sys.stderr)
            totals["periods_failed"] += 1
            period_summaries.append({"snapshot_date": snapshot_date, "folder": folder, "error": str(e)})
            continue

        totals["periods_ok"] += 1
        totals["rows"] += stats["n_rows"]
        totals["matched"] += stats["n_matched"]
        totals["file_errors"] += stats["n_file_errors"]
        period_summaries.append(stats)

        elapsed = time.time() - t_period
        pct = (stats["n_matched"] / stats["n_rows"] * 100) if stats["n_rows"] else 0.0
        print(f"[{i}/{len(periods)}] {snapshot_date}  files={stats['n_files']} "
              f"(err={stats['n_file_errors']})  rows={stats['n_rows']}  "
              f"matched={stats['n_matched']} ({pct:.0f}%)  {elapsed:.1f}s")
        if stats["file_errors"]:
            for fn, err in stats["file_errors"].items():
                print(f"    ! {fn}: {err}", file=sys.stderr)

    conn.close()
    total_elapsed = time.time() - t0
    print("\n=== SUMMARY ===")
    print(f"Periods processed OK: {totals['periods_ok']} / {len(periods)}")
    print(f"Periods failed entirely: {totals['periods_failed']}")
    print(f"Individual file errors: {totals['file_errors']}")
    print(f"Total rows: {totals['rows']}")
    pct = (totals["matched"] / totals["rows"] * 100) if totals["rows"] else 0.0
    print(f"Total matched to product master: {totals['matched']} ({pct:.1f}%)")
    print(f"Elapsed: {total_elapsed / 60:.1f} min")


if __name__ == "__main__":
    main()
