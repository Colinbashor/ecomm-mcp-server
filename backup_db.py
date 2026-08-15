r"""
Warehouse.db backup with rotation.

warehouse.db is often the only copy of history reaching further back than
your source platforms' own API retention (many ad platforms only keep
detailed reports for 60-95 days; some connectors in this repo backfill
deeper history that can't be re-pulled once it ages out upstream). This
script uses SQLite's online backup API, which is safe against a live WAL
database — a concurrent sync job can keep writing while the backup runs.

Backups go to BACKUP_DIR (default: a `backups/` folder OUTSIDE the project
directory, so a botched project-folder operation — e.g. `git clean`, a bad
`rm -rf` — can't take the backups down with it), named
warehouse-YYYY-MM-DD.db, keeping the newest KEEP copies. This is same-disk
only: it protects against corruption or an accidental delete, NOT against
disk loss. If you need an offsite copy, add a step that uploads the rotated
file somewhere else (cloud storage, another machine) after this script runs.

Set WAREHOUSE_BACKUP_DIR in .env (or edit BACKUP_DIR / KEEP below) to change
the destination or retention count.

Run manually, or wire into your OS's task scheduler (Windows Task Scheduler,
cron, launchd) to run daily before your main sync job:
  .\.venv\Scripts\python.exe backup_db.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB = Path(os.environ.get("WAREHOUSE_DB", "warehouse.db"))
if not DB.is_absolute():
    DB = Path(__file__).resolve().parent / DB
BACKUP_DIR = Path(os.environ.get("WAREHOUSE_BACKUP_DIR") or
                  Path(__file__).resolve().parent.parent / "warehouse-backups")
KEEP = int(os.environ.get("WAREHOUSE_BACKUP_KEEP", "3"))


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"warehouse-{date.today().isoformat()}.db"
    tmp = dest.with_suffix(".db.partial")

    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst, pages=4096)  # chunked so we never hold long locks
    finally:
        dst.close()
        src.close()

    # A backup that can't answer a trivial query is not a backup — verify
    # before we trust it enough to rotate an older, known-good copy out.
    check = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        tables = check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone()
    finally:
        check.close()
    if not tables:
        tmp.unlink(missing_ok=True)
        sys.exit("backup verification failed (no tables found) — kept nothing")

    tmp.replace(dest)  # atomic: yesterday's file survives until today's verifies
    print(f"backed up {DB.stat().st_size / 1e9:.2f} GB -> {dest}")

    old = sorted(BACKUP_DIR.glob("warehouse-*.db"))[:-KEEP] if KEEP > 0 else []
    for f in old:
        f.unlink()
        print(f"rotated out {f.name}")


if __name__ == "__main__":
    main()
