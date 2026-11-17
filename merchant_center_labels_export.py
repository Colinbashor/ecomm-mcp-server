"""
Merchant Center custom labels -> a supplemental feed Google Sheet.

Writes `id, custom_label_0..4` for every ACTIVE Shopify variant (labels from
feed_labels.py) into the Google Sheet GMC_LABELS_SHEET_ID. In Merchant Center
that sheet is attached to your EXISTING primary feed as a supplemental data
source, so Performance Max / Shopping listing groups can split on margin,
markdown, newness and velocity without touching the primary feed.

WHY A SUPPLEMENTAL SHEET: a supplemental source can only add or override
attributes on products the primary feed already has. A bad push can mislabel
products but can never delist one, which is the failure a full primary-feed
rewrite risks.

GUARDS
  * Dry run by default; --execute is the only path that pushes.
  * A row floor (GMC_LABELS_MIN_ROWS): a build below it is refused and the
    sheet is left as it was. A broken upstream sync (an empty catalog table,
    an id format that matches nothing) produces a tiny build, and pushing it
    would replace every good label with nothing.
  * The id column must match the primary feed's ids exactly. GMC_FEED_ID_FORMAT
    has no default for that reason; see docs/merchant-center.md.

STATUS: every --execute run logs sync_log platform `gmc_labels_export`:
`ok` with the rows pushed, or `error` (refused, build failed, push failed),
and exits non-zero on error. A dry run logs nothing.

Usage:
  python merchant_center_labels_export.py                 # dry run: counts + samples
  python merchant_center_labels_export.py --csv out.csv   # dry run, also write the rows
  python merchant_center_labels_export.py --execute       # push the sheet
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import feed_labels  # noqa: E402
from warehouse import db, drive  # noqa: E402

PLATFORM = "gmc_labels_export"
HEADER = ("id",) + feed_labels.LABEL_COLUMNS
DEFAULT_MIN_ROWS = 100
SHEET_TAB = "labels"


def min_rows() -> int:
    return feed_labels._env_int("GMC_LABELS_MIN_ROWS", DEFAULT_MIN_ROWS)


def check_required_env(execute: bool) -> None:
    """Fail fast on a missing setting instead of deep inside the push."""
    feed_labels.feed_id_format()                       # raises on missing/invalid
    if execute:
        missing = [v for v in ("GMC_LABELS_SHEET_ID", "GMC_CREDENTIALS_FILE")
                   if not os.environ.get(v, "").strip()]
        if missing:
            raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. "
                             "See .env.example.")


def build_sheet_rows(conn: sqlite3.Connection) -> tuple[list[list[str]], dict]:
    rows = feed_labels.build_label_rows(conn)
    keyed, dropped = feed_labels.keyed_rows(rows, feed_labels.feed_id_format(),
                                            feed_labels.target_country())
    sheet = [[fid] + [r[c] for c in feed_labels.LABEL_COLUMNS] for fid, r in keyed]
    return sheet, {"variants": len(rows), "rows": len(sheet), "dropped": dropped,
                   "counts": feed_labels.label_counts([r for _, r in keyed])}


def _open_db() -> sqlite3.Connection:
    if not Path(db.DB_PATH).exists():
        raise RuntimeError(f"warehouse database not found at {db.DB_PATH}")
    return db.connect_readonly()


def run(execute: bool, csv_path: Path | None = None) -> int:
    started = db.now()

    def log(status: str, rows: int, msg: str) -> None:
        print(msg)
        if execute:
            db.log_sync(PLATFORM, started, rows, status, msg)

    try:
        conn = _open_db()
        try:
            sheet, stats = build_sheet_rows(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- logged, then re-raised
        log("error", 0, f"label build failed: {exc}")
        raise

    print(f"{stats['variants']:,} active variants -> {stats['rows']:,} feed rows "
          f"({stats['dropped']:,} dropped: no id in this format, or an ambiguous/duplicate id)")
    for col, counts in stats["counts"].items():
        top = ", ".join(f"{k or '(blank)'}: {n:,}" for k, n in counts.most_common(6))
        print(f"  {col}: {top}")
    for row in sheet[:5]:
        print("  sample:", row)
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(sheet)
        print(f"wrote {csv_path}")

    floor = min_rows()
    if len(sheet) < floor:
        log("error", 0, f"REFUSED: label build has {len(sheet):,} rows (< GMC_LABELS_MIN_ROWS "
                        f"{floor:,}); the sheet was left untouched")
        return 1
    if not execute:
        print(f"dry run: would push {len(sheet):,} rows. Re-run with --execute to push.")
        return 0

    try:
        drive.replace_with_rows(os.environ["GMC_LABELS_SHEET_ID"].strip(), SHEET_TAB,
                                HEADER, sheet, os.environ["GMC_CREDENTIALS_FILE"].strip())
    except Exception as exc:  # noqa: BLE001
        log("error", 0, f"push failed: {exc}")
        return 1
    log("ok", len(sheet), f"pushed {len(sheet):,} label rows "
                          f"({stats['dropped']:,} variants without a usable id)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    ap.add_argument("--execute", action="store_true",
                    help="push the sheet (default: dry run)")
    ap.add_argument("--csv", type=Path, default=None,
                    help="also write the rows to this CSV file")
    args = ap.parse_args(argv)
    check_required_env(args.execute)
    try:
        return run(args.execute, args.csv)
    except Exception as exc:  # noqa: BLE001 -- already logged by run()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
