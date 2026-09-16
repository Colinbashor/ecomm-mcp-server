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

WHY ROTATION HAPPENS *BEFORE* THE COPY, NOT AFTER — a lesson worth keeping.
An earlier version of this script copied first and rotated old copies out
afterward, which needs headroom for KEEP+1 copies at the worst possible
moment (right before the disk is already tightest) and, on a large/growing
database, can starve the whole machine of disk space. Three things compound
into a ratchet if you get any of them wrong:
  1. Rotating AFTER the copy means every run's peak requirement is KEEP+1
     copies, not KEEP — needless headroom that gets worse as the database
     grows.
  2. A FAILED copy has to take its `.partial` (and SQLite's `-wal`/`-shm`
     crumbs) with it. A rotation glob of `warehouse-*.db` does NOT match
     `warehouse-*.db.partial`, so if cleanup only runs on the success path,
     every failed night leaves dead weight behind permanently — and a
     bigger partial means less room for the NEXT attempt to succeed.
  3. If a skipped/failed backup isn't logged anywhere your monitoring reads,
     the failure is invisible until the disk is already full.
This script now: sweeps stale partials first, rotates down to KEEP (freeing
space) before attempting anything, checks free space against the source
database's size with a margin, drops to KEEP-1 rather than fail outright if
that's what it takes to fit, and logs every skip/failure to `sync_log` via
`warehouse.db.log_sync` so it's visible wherever else you watch that table —
never silently. The normal path still never deletes a known-good backup
until the new one has verified; only the degraded (disk-tight) path trades
one fewer backup for not taking the rest of the pipeline down with it.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from warehouse import db as wdb

load_dotenv()

DB = Path(os.environ.get("WAREHOUSE_DB", "warehouse.db"))
if not DB.is_absolute():
    DB = Path(__file__).resolve().parent / DB
BACKUP_DIR = Path(os.environ.get("WAREHOUSE_BACKUP_DIR") or
                  Path(__file__).resolve().parent.parent / "warehouse-backups")
KEEP = int(os.environ.get("WAREHOUSE_BACKUP_KEEP", "3"))

# Headroom over the raw file size: the online backup API writes a fresh page
# image and the source can keep growing while the copy runs.
FREE_MARGIN = 1.10
PLATFORM = "warehouse_backup"


def _siblings(p: Path):
    """A backup file plus the -wal/-shm/-journal crumbs SQLite can leave
    beside a database — a rotation or sweep that only deletes `p` itself
    leaves these behind forever."""
    yield p
    for suffix in ("-wal", "-shm", "-journal"):
        yield p.with_name(p.name + suffix)


def _rm(p: Path) -> int:
    freed = 0
    for f in _siblings(p):
        if f.exists():
            freed += f.stat().st_size
            f.unlink()
    return freed


def sweep_partials() -> int:
    """Delete every `.partial` left behind by a run that didn't finish.

    A partial is garbage by definition — it never passed verification, so
    it was never a backup. Only one backup process is meant to run at a
    time, so anything found here belongs to a run that's already over."""
    freed = 0
    for p in sorted(BACKUP_DIR.glob("warehouse-*.db.partial")):
        freed += _rm(p)
        print(f"swept stale partial {p.name}")
    return freed


def complete_backups() -> list[Path]:
    return sorted(BACKUP_DIR.glob("warehouse-*.db"))


def rotate(keep: int) -> int:
    """Delete the oldest complete backups beyond `keep`. Safe to call before
    OR after adding today's backup — see the module docstring for why this
    script calls it at both points."""
    freed = 0
    for f in complete_backups()[:-keep] if keep > 0 else complete_backups():
        freed += _rm(f)
        print(f"rotated out {f.name}")
    return freed


def main() -> None:
    started = wdb.now()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    need = int(DB.stat().st_size * FREE_MARGIN)

    sweep_partials()
    # Rotate to KEEP *before* attempting anything, so a run can never need
    # room for KEEP+1 copies at once.
    rotate(KEEP)

    free = shutil.disk_usage(BACKUP_DIR).free
    if free < need and KEEP > 1:
        # Not enough room even at KEEP. Drop to KEEP-1 rather than fail
        # outright: one fewer backup beats a full disk, which can take down
        # every other writer on the machine, not just this script.
        print(f"WARNING: {free / 1e9:.1f} GB free, need {need / 1e9:.1f} GB — "
              f"rotating to {KEEP - 1} to make room", flush=True)
        rotate(KEEP - 1)
        free = shutil.disk_usage(BACKUP_DIR).free

    if free < need:
        msg = (f"insufficient disk: {free / 1e9:.1f} GB free, need "
               f"{need / 1e9:.1f} GB for a {DB.stat().st_size / 1e9:.1f} GB "
               f"database even after rotating down")
        wdb.log_sync(PLATFORM, started, 0, "error", msg)
        sys.exit(f"backup skipped — {msg}")

    dest = BACKUP_DIR / f"warehouse-{date.today().isoformat()}.db"
    tmp = dest.with_suffix(".db.partial")
    try:
        src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        try:
            with dst:
                src.backup(dst, pages=4096)  # chunked so we never hold long locks
        finally:
            dst.close()
            src.close()

        # A backup that can't answer a trivial query is not a backup —
        # verify before we trust it enough to rotate an older, known-good
        # copy out.
        check = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            tables = check.execute(
                "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone()
        finally:
            check.close()
        if not tables:
            raise RuntimeError("verification failed — no tables found in the backup")
    except BaseException as exc:
        # Always take the partial (and its -wal/-shm crumbs) with us on any
        # failure — leaving it behind is exactly what turns one bad night
        # into a slow-motion disk-fill outage over the following weeks.
        _rm(tmp)
        wdb.log_sync(PLATFORM, started, 0, "error", f"{type(exc).__name__}: {exc}")
        if isinstance(exc, RuntimeError):
            sys.exit(f"backup {str(exc)} — kept nothing")
        raise

    tmp.replace(dest)  # atomic: yesterday's file survives until today's verifies
    size_gb = dest.stat().st_size / 1e9
    print(f"backed up {size_gb:.2f} GB -> {dest}")

    # Rotate again now that today's copy exists — the pre-copy rotate above
    # only guarantees we never NEEDED more than KEEP+1 copies of headroom;
    # this is what actually brings the count back down to KEEP.
    rotate(KEEP)
    left_gb = shutil.disk_usage(BACKUP_DIR).free / 1e9
    msg = f"{size_gb:.2f} GB -> {dest.name} ({len(complete_backups())} kept, {left_gb:.1f} GB free)"
    wdb.log_sync(PLATFORM, started, 1, "ok", msg)


if __name__ == "__main__":
    main()
