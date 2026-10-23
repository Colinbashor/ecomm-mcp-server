r"""
Warehouse.db backup with rotation.

warehouse.db is often the only copy of history reaching further back than
your source platforms' own API retention (many ad platforms only keep
detailed reports for 60-95 days; some connectors in this repo backfill
deeper history that can't be re-pulled once it ages out upstream). This
script uses SQLite's online backup API, which is safe against a live WAL
database — a concurrent sync job can keep writing while the backup runs.

Backups go to BACKUP_DIR (default: a `warehouse-backups/` folder OUTSIDE the project
directory, so a botched project-folder operation — e.g. `git clean`, a bad
`rm -rf` — can't take the backups down with it), named
warehouse-YYYY-MM-DD.db, keeping the newest KEEP copies. This is same-disk
only: it protects against corruption or an accidental delete, NOT against
disk loss. If you need an offsite copy, add a step that uploads the rotated
file somewhere else (cloud storage, another machine) after this script runs.

Set WAREHOUSE_BACKUP_DIR in .env (or edit BACKUP_DIR / KEEP below) to change
the destination or retention count.

OPTIONAL WEEKLY COMPRESSED COPY. Set WAREHOUSE_BACKUP_ZIPS=N (N >= 1) to also
write a compressed copy (warehouse-YYYY-MM-DD.db.zip) on one weekday
(WAREHOUSE_BACKUP_ZIP_WEEKDAY, 0=Monday .. 6=Sunday, default 6), keeping the
newest N zips. A zip is a cheap way to keep deeper history while lowering
KEEP. It is written from the backup that JUST verified, and is itself
verified (CRC + size) BEFORE the zip it replaces is freed. A zip failure logs
`degraded`, never `ok` (the retention you asked for is not intact) and never
`error` (the nightly .db backup did succeed). Disabled by default.

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
import zipfile
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
# Weekly compressed copies to keep; 0 disables the feature.
ZIPS = int(os.environ.get("WAREHOUSE_BACKUP_ZIPS", "0"))
ZIP_WEEKDAY = int(os.environ.get("WAREHOUSE_BACKUP_ZIP_WEEKDAY", "6"))
# Compressed size as a share of the database; drives the free-space reserve on
# zip days. 0.15 is deliberately generous for SQLite page images.
ZIP_SIZE_FRACTION = 0.15

# Headroom over the raw file size: the online backup API writes a fresh page
# image and the source can keep growing while the copy runs.
FREE_MARGIN = 1.10
PLATFORM = "warehouse_backup"


# The crumbs SQLite can leave beside a database or a `.partial` of one.
DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _siblings(p: Path):
    """A backup file plus the -wal/-shm/-journal crumbs SQLite can leave
    beside a database — a rotation or sweep that only deletes `p` itself
    leaves these behind forever."""
    yield p
    for suffix in DB_SIDECAR_SUFFIXES:
        yield p.with_name(p.name + suffix)


def _rm(p: Path) -> int:
    freed = 0
    for f in _siblings(p):
        if f.exists():
            freed += f.stat().st_size
            f.unlink()
    return freed


def _not_in_use(p: Path) -> bool:
    """Best-effort "nothing has this file open" probe. On Windows a byte-range
    lock cannot be taken while SQLite (or anything else) holds the file; on
    POSIX an advisory flock is tried."""
    try:
        f = open(p, "r+b")
    except OSError:
        return False
    try:
        try:
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            try:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:
                pass
        return True
    except OSError:
        return False
    finally:
        f.close()


def sweep_partials() -> int:
    """Delete every `.partial` left behind by a run that didn't finish, every
    sidecar crumb left beside one, and stale -wal/-shm beside COMPLETE backups.

    A partial is garbage by definition — it never passed verification, so
    it was never a backup. Only one backup process is meant to run at a
    time, so anything found here belongs to a run that's already over.

    Sidecars whose main file is gone are swept by NAME: no process can have a
    database open whose file no longer exists. Without this, the crumbs of a
    swept partial (`.db.partial-wal` ...) and of backups already rotated away
    stay behind forever.

    COMPLETE backups still present are different — they ARE backups. Their
    -wal/-shm go only when the -wal is zero-length (nothing uncheckpointed to
    lose) and neither file is in use. A -journal beside a complete backup is
    NEVER swept: a hot journal can hold unmerged pages, and deleting it can
    corrupt the backup."""
    freed = 0
    for pattern in ("warehouse-*.db.partial", "warehouse-*.db.zip.partial"):
        for p in sorted(BACKUP_DIR.glob(pattern)):
            try:
                freed += _rm(p)
                print(f"swept stale partial {p.name}")
            except OSError as e:
                # One locked litter file must not kill the nightly backup.
                print(f"could not sweep {p.name}: {e}")
    # `db*-*` rather than `db-*`: a partial's crumbs are `.db.partial-wal`,
    # where ".db" is not directly followed by the dash.
    for s in sorted(BACKUP_DIR.glob("warehouse-*.db*-*")):
        stem = next((s.name[:-len(suf)] for suf in DB_SIDECAR_SUFFIXES
                     if s.name.endswith(suf)), None)
        if stem is None or (BACKUP_DIR / stem).exists():
            continue  # not a sidecar we know, or a live file owns it
        try:
            freed += s.stat().st_size
            s.unlink()
            print(f"swept orphaned sidecar {s.name}")
        except OSError as e:
            print(f"could not sweep {s.name}: {e}")
    for db in complete_backups():
        wal = db.with_name(db.name + "-wal")
        if not wal.exists() or wal.stat().st_size != 0:
            continue
        crumbs = [wal] + [c for c in (db.with_name(db.name + "-shm"),)
                          if c.exists()]
        if not all(_not_in_use(c) for c in crumbs):
            continue
        for c in crumbs:
            freed += c.stat().st_size
            c.unlink()
            print(f"swept stale sidecar {c.name}")
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


def rotate_zips(keep: int) -> int:
    freed = 0
    zips = sorted(BACKUP_DIR.glob("warehouse-*.db.zip"))
    for f in zips[:-keep] if keep > 0 else zips:
        freed += f.stat().st_size
        f.unlink()
        print(f"rotated out {f.name}")
    return freed


def zips_enabled() -> bool:
    return ZIPS > 0


def is_zip_day(d: date) -> bool:
    return zips_enabled() and d.weekday() == ZIP_WEEKDAY


def _write_zip(source: Path, tmp: Path) -> None:
    # compresslevel=1: page images compress nearly as well at 1 as at 6 and
    # several times faster.
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        z.write(source, arcname=source.name)


def _verify_zip(tmp: Path, source: Path) -> None:
    with zipfile.ZipFile(tmp) as z:
        if z.testzip() is not None:
            raise RuntimeError("CRC verify failed")
        info = z.infolist()[0]
        if info.file_size != source.stat().st_size:
            raise RuntimeError(f"size verify failed: zip holds "
                               f"{info.file_size}, source is "
                               f"{source.stat().st_size}")


def compressed_copy(source: Path) -> tuple[str, str]:
    """The weekly compressed copy, made from the backup that JUST verified.

    Returns ("", note) on success or ("degraded", reason) on failure. The
    nightly .db backup has already succeeded by now, so a zip problem
    downgrades the run to `degraded` — never a silent `ok`, never `error`.
    The zip it replaces is freed ONLY after this one is on disk verified."""
    final = BACKUP_DIR / (source.name + ".zip")
    tmp = BACKUP_DIR / (source.name + ".zip.partial")
    try:
        _write_zip(source, tmp)
        _verify_zip(tmp, source)
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return "degraded", f"weekly zip FAILED: {type(e).__name__}: {e}"
    tmp.replace(final)  # atomic: last week's zip survives until this verifies
    rotate_zips(ZIPS)
    return "", f"weekly zip {final.name} ({final.stat().st_size / 1e9:.2f} GB)"


def main(today: date | None = None) -> None:
    today = today or date.today()
    started = wdb.now()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    size = DB.stat().st_size
    need = int(size * FREE_MARGIN)
    if is_zip_day(today):
        # Room for the fresh zip while last week's is still on disk.
        need += int(size * ZIP_SIZE_FRACTION)

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
        if zips_enabled():
            rotate_zips(max(ZIPS - 1, 1))
        free = shutil.disk_usage(BACKUP_DIR).free

    if free < need:
        msg = (f"insufficient disk: {free / 1e9:.1f} GB free, need "
               f"{need / 1e9:.1f} GB for a {size / 1e9:.1f} GB "
               f"database even after rotating down")
        wdb.log_sync(PLATFORM, started, 0, "error", msg)
        sys.exit(f"backup skipped — {msg}")

    dest = BACKUP_DIR / f"warehouse-{today.isoformat()}.db"
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

    zip_status, zip_note = "", ""
    if is_zip_day(today):
        zip_status, zip_note = compressed_copy(dest)

    # Rotate again now that today's copy exists — the pre-copy rotate above
    # only guarantees we never NEEDED more than KEEP+1 copies of headroom;
    # this is what actually brings the count back down to KEEP.
    rotate(KEEP)
    left_gb = shutil.disk_usage(BACKUP_DIR).free / 1e9
    msg = f"{size_gb:.2f} GB -> {dest.name} ({len(complete_backups())} kept, {left_gb:.1f} GB free)"
    if zip_note:
        msg = msg[:-1] + f"; {zip_note})"
    wdb.log_sync(PLATFORM, started, 1, zip_status or "ok", msg)


if __name__ == "__main__":
    main()
