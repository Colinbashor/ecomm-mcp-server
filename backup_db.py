r"""
Warehouse.db backup with rotation.

warehouse.db is often the only copy of history reaching further back than
your source platforms' own API retention (many ad platforms only keep
detailed reports for 60-95 days; some connectors in this repo backfill
deeper history that can't be re-pulled once it ages out upstream). The copy
is one `VACUUM INTO` statement: a single consistent read snapshot of a live
WAL database, so a concurrent sync job can keep writing while the backup
runs. The database copied is `warehouse.db.DB_PATH` — the same file every
other script (and this script's own `sync_log` row) uses.

Backups go to BACKUP_DIR (default: a `warehouse-backups/` folder OUTSIDE the project
directory, so a botched project-folder operation — e.g. `git clean`, a bad
`rm -rf` — can't take the backups down with it), named
warehouse-YYYY-MM-DD.db, keeping the newest KEEP copies. This is same-disk
only: it protects against corruption or an accidental delete, NOT against
disk loss. If you need an offsite copy, add a step that uploads the rotated
file somewhere else (cloud storage, another machine) after this script runs.

Set WAREHOUSE_BACKUP_DIR / WAREHOUSE_BACKUP_KEEP in .env to change the
destination or retention count.

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
  python backup_db.py

WHY THE RUN SWEEPS, ROTATES AND CHECKS FREE SPACE *BEFORE* THE COPY.
A backup script can fill the disk it is meant to protect. Three things
compound into a ratchet if you get any of them wrong:
  1. Rotating only AFTER the copy means a failed copy never reaches the one
     step that frees space, so the next run has even less room.
  2. A FAILED copy has to take its `.partial` (and SQLite's `-wal`/`-shm`
     crumbs) with it. A rotation glob of `warehouse-*.db` does NOT match
     `warehouse-*.db.partial`, so if cleanup only runs on the success path,
     every failed night leaves dead weight behind permanently — and a
     bigger partial means less room for the NEXT attempt to succeed.
  3. If a skipped/failed backup isn't logged anywhere your monitoring reads,
     the failure is invisible until the disk is already full.
This script therefore sweeps stale partials first, rotates anything above
KEEP, checks free space against what the copy will read (the database PLUS
its live -wal, with a margin), drops to KEEP-1 rather than fail outright if
that's what it takes to fit, and logs the outcome to `sync_log`. The normal
path still never deletes a known-good backup until the new one has verified;
only the degraded (disk-tight) path trades one fewer backup for not taking
the rest of the pipeline down with it.

EVERY EXIT LEAVES A sync_log ROW. A run that dies without one is
indistinguishable from a run that never started, so main() runs the whole
job inside one handler that logs FIRST and cleans up second, and every file
operation that can fail on its own (a sweep, a rotation) is individually
guarded: a file that will not delete is reported, not raised. A rotation
that could not delete logs `degraded` — the backup is good, but retention is
not being enforced, which is the first step of the ratchet above.

ONE RUN AT A TIME, BY A REAL LOCK. The run holds an OS lock on
`backup_db.lock` in BACKUP_DIR for its whole life. "A .partial exists" is not
a usable signal: a crashed run's leftover satisfies it forever (wedging that
date until someone deletes the file by hand), and two runs launched in the
same instant can both see "no partial yet". An OS lock is granted to exactly
one process and is dropped by the OS when that process ends, however it ends.
"""
from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import sys
import zipfile
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Before warehouse.db is imported, so a WAREHOUSE_DB set only in .env reaches
# its DB_PATH too.
load_dotenv()

from warehouse import db as wdb  # noqa: E402

# The file every other script reads and writes, and the one log_sync() below
# writes this run's row to. Resolving WAREHOUSE_DB a second time here could
# disagree with warehouse/db.py — and then the script would copy one database
# while reporting on the backup in another.
DB = wdb.DB_PATH
BACKUP_DIR = Path(os.environ.get("WAREHOUSE_BACKUP_DIR") or
                  Path(__file__).resolve().parent.parent / "warehouse-backups")
KEEP = int(os.environ.get("WAREHOUSE_BACKUP_KEEP", "3"))
# Weekly compressed copies to keep; 0 disables the feature.
ZIPS = int(os.environ.get("WAREHOUSE_BACKUP_ZIPS", "0"))
ZIP_WEEKDAY = int(os.environ.get("WAREHOUSE_BACKUP_ZIP_WEEKDAY", "6"))
# Compressed size as a share of the database; drives the free-space reserve on
# zip days (and disk_probe.py's zip-night threshold). How well a database
# compresses depends on what is in it — mostly-numeric tables shrink far more
# than long text or URLs — so measure one of your own zips and set this a
# little above it rather than trusting the default.
ZIP_SIZE_FRACTION = float(os.environ.get("WAREHOUSE_BACKUP_ZIP_FRACTION", "0.30"))

# Headroom over the raw size: the copy is a fresh page image and the source
# can keep growing while it runs.
FREE_MARGIN = 1.10
PLATFORM = "warehouse_backup"
# Held (an OS lock, not mere existence) for the whole run.
LOCK_NAME = "backup_db.lock"

# The crumbs SQLite can leave beside a database or a `.partial` of one.
DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# DATED names only. A hand-made copy such as `warehouse-before-upgrade.db`
# sorts AFTER every date, so a `warehouse-*.db` glob would treat it as the
# newest backup and rotate real dated copies out to keep it.
_DATED = "warehouse-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"


class BackupRefused(Exception):
    """A deliberate refusal before anything was copied (no room, another run
    in progress): logged `error`, exit 1."""


class VerificationFailed(Exception):
    """The copy finished but is not a usable database: logged `error`, the
    copy is discarded, exit 1."""


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


def _quiet_rm(p: Path) -> None:
    try:
        _rm(p)
    except OSError as e:
        # Left for the next run's sweep_partials(); the row is already logged.
        print(f"could not remove {p.name}: {e}", file=sys.stderr, flush=True)


def _note(problems: list[str] | None, note: str) -> None:
    print(note)
    if problems is not None:
        problems.append(note)


def _not_in_use(p: Path) -> bool:
    """True only when it is PROVEN that nothing has `p` open.

    Windows: open the file sharing nothing. Any other open handle, whatever
    share mode it asked for, makes that open fail with a sharing violation,
    and unlike a rename probe it changes nothing on disk. A byte-range lock
    probe is NOT equivalent: SQLite opens its -wal/-shm shared and locks only
    the specific bytes it uses, so locking some other byte succeeds on a file
    a live connection is using.

    Everywhere else there is no share mode to test, and advisory locks say
    nothing about who merely has a file open (SQLite's own are a different
    lock family from flock), so the answer is False: a sidecar that cannot be
    proven idle is never deleted. The cost is a few small stale -wal/-shm
    files beside kept backups, which leave when that backup rotates out.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, share_none, open_existing, normal = 0x80000000, 0, 3, 0x80
    h = k32.CreateFileW(str(p), generic_read, share_none, None,
                        open_existing, normal, None)
    if h is None or h == wintypes.HANDLE(-1).value:
        return False  # sharing violation, gone, or no access: hands off
    k32.CloseHandle(h)
    return True


# What "someone else holds the lock" looks like; anything else (a filesystem
# with no lock support, say) is a real error and must not be reported as
# "another run is in progress".
_LOCK_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}


def _lock_file(f, take: bool) -> None:
    if sys.platform == "win32":
        import msvcrt

        f.seek(0)  # the lock covers one byte from the current position
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if take else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if take
                    else fcntl.LOCK_UN)


def acquire_lock():
    """The open lock file, or None when another backup run holds it.

    Both branches lock per open handle, so a second attempt is refused even
    from inside the same process, and both are released by the OS when the
    holder exits — a crashed run can never wedge the next one."""
    f = open(BACKUP_DIR / LOCK_NAME, "a+b")
    try:
        _lock_file(f, True)
    except OSError as e:
        f.close()
        if e.errno in _LOCK_BUSY:
            return None
        raise
    return f


def release_lock(f) -> None:
    try:
        _lock_file(f, False)
    except OSError:
        pass  # closing the handle releases it anyway
    finally:
        f.close()


def sweep_partials() -> int:
    """Delete every `.partial` left behind by a run that didn't finish, every
    sidecar crumb left beside one, and stale -wal/-shm beside COMPLETE backups.

    A partial is garbage by definition — it never passed verification, so
    it was never a backup. main() holds the run lock before calling this, so
    anything found here belongs to a run that's already over.

    Sidecars whose main file is gone are swept by NAME: no process can have a
    database open whose file no longer exists. Without this, the crumbs of a
    swept partial (`.db.partial-wal` ...) and of backups already rotated away
    stay behind forever.

    COMPLETE backups still present are different — they ARE backups. Their
    -wal/-shm go only when the -wal is zero-length (nothing uncheckpointed to
    lose) and neither file is in use (see _not_in_use). A -journal beside a
    complete backup is NEVER swept: a hot journal can hold unmerged pages, and
    deleting it can corrupt the backup.

    Nothing here raises: one file that will not delete must not end the run
    before it has logged anything."""
    freed = 0
    for pattern in ("warehouse-*.db.partial", "warehouse-*.db.zip.partial"):
        for p in sorted(BACKUP_DIR.glob(pattern)):
            try:
                freed += _rm(p)
                print(f"swept stale partial {p.name}")
            except OSError as e:
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
        try:
            if not wal.exists() or wal.stat().st_size != 0:
                continue
        except OSError as e:
            print(f"could not inspect {wal.name}: {e}")
            continue
        crumbs = [wal] + [c for c in (db.with_name(db.name + "-shm"),)
                          if c.exists()]
        if not all(_not_in_use(c) for c in crumbs):
            continue
        for c in crumbs:
            # Guarded: a sidecar opened between the probe and the unlink.
            try:
                size = c.stat().st_size
                c.unlink()
            except OSError as e:
                print(f"could not sweep {c.name}: {e}")
                continue
            freed += size
            print(f"swept stale sidecar {c.name}")
    return freed


def complete_backups() -> list[Path]:
    return sorted(BACKUP_DIR.glob(_DATED + ".db"))


def rotate(keep: int, problems: list[str] | None = None) -> int:
    """Delete the oldest complete backups beyond `keep`. A file that will not
    delete is noted in `problems` and skipped, never raised."""
    freed = 0
    for f in complete_backups()[:-keep] if keep > 0 else complete_backups():
        try:
            freed += _rm(f)
        except OSError as e:
            _note(problems, f"could not rotate out {f.name}: {e}")
            continue
        print(f"rotated out {f.name}")
    return freed


def rotate_zips(keep: int, problems: list[str] | None = None) -> int:
    freed = 0
    zips = sorted(BACKUP_DIR.glob(_DATED + ".db.zip"))
    for f in zips[:-keep] if keep > 0 else zips:
        try:
            size = f.stat().st_size
            f.unlink()
        except OSError as e:
            _note(problems, f"could not rotate out {f.name}: {e}")
            continue
        freed += size
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
        tmp.replace(final)  # atomic: last week's zip survives until this verifies
        size_gb = final.stat().st_size / 1e9
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass  # the next run's sweep_partials() takes it
        return "degraded", f"weekly zip FAILED: {type(e).__name__}: {e}"
    problems: list[str] = []
    rotate_zips(ZIPS, problems)
    note = f"weekly zip {final.name} ({size_gb:.2f} GB)"
    if problems:
        return "degraded", f"{note}; zip rotation FAILED: {'; '.join(problems)}"
    return "", note


def db_bytes() -> int:
    """What the copy reads: the database PLUS its live -wal. VACUUM INTO
    copies the read snapshot, which includes every committed page still
    sitting in the WAL, and after a heavy sync the WAL can be a large share
    of the total that the main file's size does not show. disk_probe.py sizes
    its thresholds on this too."""
    size = DB.stat().st_size
    try:
        size += DB.with_name(DB.name + "-wal").stat().st_size
    except FileNotFoundError:
        pass  # no WAL right now: nothing uncheckpointed to copy
    return size


def required_bytes(today: date) -> tuple[int, int]:
    """(source bytes, free bytes needed) for a copy made on `today`."""
    size = db_bytes()
    need = int(size * FREE_MARGIN)
    if is_zip_day(today):
        # Room for the fresh zip while the one it replaces is still on disk.
        need += int(size * ZIP_SIZE_FRACTION)
    return size, need


def _log(started: str, rows: int, status: str, message: str) -> bool:
    """log_sync that cannot take the run down with it. If even this fails (a
    locked or full database), say so on stderr and return False: the process
    exit code is then the only signal left, so the caller exits non-zero."""
    try:
        wdb.log_sync(PLATFORM, started, rows, status, message)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"COULD NOT WRITE sync_log ({type(e).__name__}: {e}) for: "
              f"{status} {message}", file=sys.stderr, flush=True)
        return False


def _backup(today: date, tmp: Path, dest: Path) -> tuple[str, str]:
    """The run proper: (status, message), or an exception. Everything here
    runs inside main()'s single handler, so no failure can skip the sync_log
    row."""
    size, need = required_bytes(today)
    problems: list[str] = []

    sweep_partials()
    # This does NOT cap the peak at KEEP copies: the new copy is written
    # beside the KEEP already kept, so the peak is KEEP+1 for as long as the
    # copy runs. It only clears anything ABOVE KEEP, left by a run that died
    # before its own closing rotation. The real guard is the `need` check
    # below, which drops to KEEP-1 (a peak of KEEP) when KEEP+1 will not fit.
    rotate(KEEP, problems)

    free = shutil.disk_usage(BACKUP_DIR).free
    if free < need and KEEP > 1:
        # Not enough room even at KEEP. Drop to KEEP-1 rather than fail
        # outright: one fewer backup beats a full disk, which can take down
        # every other writer on the machine, not just this script.
        print(f"WARNING: {free / 1e9:.1f} GB free, need {need / 1e9:.1f} GB — "
              f"rotating to {KEEP - 1} to make room", flush=True)
        rotate(KEEP - 1, problems)
        if zips_enabled():
            rotate_zips(max(ZIPS - 1, 1), problems)
        free = shutil.disk_usage(BACKUP_DIR).free

    if free < need:
        raise BackupRefused(
            f"insufficient disk: {free / 1e9:.1f} GB free, need "
            f"{need / 1e9:.1f} GB for a {size / 1e9:.1f} GB database "
            f"(incl. -wal) even after rotating down"
            + (f"; {'; '.join(problems)}" if problems else ""))

    src = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        # VACUUM INTO: one statement, one consistent read snapshot for the
        # whole copy. Concurrent writers can neither block it (WAL readers
        # never do) nor restart it. The chunked online-backup API, by
        # contrast, starts over from the first page whenever another
        # connection commits mid-copy, so against a busy database it can
        # run for hours without finishing.
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()

    # A backup that can't answer a trivial query is not a backup — verify
    # before we trust it enough to rotate an older, known-good copy out.
    check = sqlite3.connect(f"file:{tmp.as_posix()}?mode=ro", uri=True)
    try:
        tables = check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone()
    finally:
        check.close()
    if not tables:
        raise VerificationFailed("verification failed — no tables found in the backup")

    tmp.replace(dest)  # atomic: yesterday's file survives until today's verifies
    size_gb = dest.stat().st_size / 1e9
    print(f"backed up {size_gb:.2f} GB -> {dest}")

    status, note = "", ""
    if is_zip_day(today):
        status, note = compressed_copy(dest)

    # Bring the count back from KEEP+1 to KEEP now that today's copy exists.
    post: list[str] = []
    rotate(KEEP, post)
    if post:
        # The backup is good but retention is not being enforced, so neither
        # `ok` nor `error` is true.
        status = "degraded"
        note = "; ".join(x for x in (note, "rotation FAILED: " + "; ".join(post)) if x)
    left_gb = shutil.disk_usage(BACKUP_DIR).free / 1e9
    msg = (f"{size_gb:.2f} GB -> {dest.name} ({len(complete_backups())} kept, "
           f"{left_gb:.1f} GB free" + (f"; {note}" if note else "") + ")")
    return status or "ok", msg


def main(today: date | None = None) -> None:
    today = today or date.today()
    started = wdb.now()
    dest = BACKUP_DIR / f"warehouse-{today.isoformat()}.db"
    tmp = dest.with_name(dest.name + ".partial")
    lock = None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        # Must be held BEFORE sweep_partials(), which deletes every .partial
        # it sees — including a concurrent run's live copy. A same-day partial
        # found once we hold the lock is a dead run's, and the sweep takes it.
        lock = acquire_lock()
        if lock is None:
            raise BackupRefused(f"refusing to start: another backup run holds "
                                f"{BACKUP_DIR / LOCK_NAME}")
        status, msg = _backup(today, tmp, dest)
    except BackupRefused as e:
        _log(started, 0, "error", str(e))
        if lock is not None:
            release_lock(lock)
        sys.exit(f"backup skipped — {e}")
    except BaseException as e:
        # LOG FIRST, clean up second: a partial that will not delete (still
        # held by the dying copy, a virus scanner) must not cost the row.
        _log(started, 0, "error", f"{type(e).__name__}: {e}")
        # Take our own partial with us — never another run's: without the
        # lock, the file at this name is not ours to touch.
        if lock is not None:
            _quiet_rm(tmp)
            release_lock(lock)
        if isinstance(e, VerificationFailed):
            sys.exit(f"backup {e} — kept nothing")
        raise
    release_lock(lock)
    if not _log(started, 1, status, msg):
        sys.exit("backup succeeded but its sync_log row could not be written")


if __name__ == "__main__":
    main()
