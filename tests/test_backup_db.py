"""Hermetic tests for backup_db.py — no network, tmp_path only, and every
test patches `backup_db.wdb.log_sync` so nothing ever touches a real
warehouse.db's sync_log table.

Covers: a good backup verifies + rotates old copies out, a from-scratch (no
tables) source database is correctly rejected rather than silently "backed
up" as an empty, useless file, and the disk-fill failure mode this script is
built to avoid — see the module docstring's "WHY ROTATION HAPPENS *BEFORE*
THE COPY" section. That failure mode is a ratchet of three defects compounding
together (rotate-after-copy needs KEEP+1 headroom; a failed copy's `.partial`
sits outside the rotation glob and lingers forever; a skipped/failed backup
that isn't logged anywhere is invisible until the disk is already full), so
each gets its own test rather than one combined "it doesn't crash" check.
"""
from __future__ import annotations

import contextlib
import errno
import importlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


class BackupDbTests(unittest.TestCase):
    def _reload(self, db_path: Path, backup_dir: Path, keep: str = "3"):
        with patch.dict("os.environ", {
            "WAREHOUSE_BACKUP_DIR": str(backup_dir),
            "WAREHOUSE_BACKUP_KEEP": keep,
        }):
            import backup_db
            mod = importlib.reload(backup_db)
        # DB is warehouse.db.DB_PATH, fixed when that module was first
        # imported, so the test database is patched in rather than set by env.
        db_patcher = patch.object(mod, "DB", db_path)
        self.addCleanup(db_patcher.stop)
        db_patcher.start()
        # Every test drives main() end-to-end; log_sync must never touch a
        # real database file regardless of which path (success/failure) runs.
        patcher = patch.object(mod.wdb, "log_sync")
        self.addCleanup(patcher.stop)
        self.logged = patcher.start()
        return mod

    def test_backs_up_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "warehouse.db"
            backup_dir = tmp_path / "backups"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE sync_log (platform TEXT)")
            conn.execute("INSERT INTO sync_log VALUES ('test')")
            conn.commit()
            conn.close()

            mod = self._reload(db_path, backup_dir)
            mod.main()

            backups = list(backup_dir.glob("warehouse-*.db"))
            self.assertEqual(len(backups), 1)
            check = sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True)
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0], 1)
            check.close()

            self.logged.assert_called_once()
            self.assertEqual(self.logged.call_args.args[0], mod.PLATFORM)
            self.assertEqual(self.logged.call_args.args[3], "ok")

    def test_rejects_empty_source_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "warehouse.db"
            backup_dir = tmp_path / "backups"
            sqlite3.connect(db_path).close()  # file exists, zero tables

            mod = self._reload(db_path, backup_dir)
            with self.assertRaises(SystemExit):
                mod.main()

            self.assertEqual(list(backup_dir.glob("warehouse-*.db*")), [],
                              "a failed verification must leave no partial behind")
            self.logged.assert_called_once()
            self.assertEqual(self.logged.call_args.args[3], "error")

    def test_rotation_keeps_only_newest_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "warehouse.db"
            backup_dir = tmp_path / "backups"
            backup_dir.mkdir()
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.close()

            # Pre-seed 3 fake older backups so rotation has something to drop.
            for name in ("warehouse-2025-01-01.db", "warehouse-2025-01-02.db",
                         "warehouse-2025-01-03.db"):
                (backup_dir / name).write_bytes(b"x")

            mod = self._reload(db_path, backup_dir, keep="2")
            mod.main()

            remaining = sorted(p.name for p in backup_dir.glob("warehouse-*.db"))
            # 3 old + 1 new = 4, keep=2 -> newest 2 survive (today's + the
            # newest-dated pre-seeded stub).
            self.assertEqual(len(remaining), 2)
            self.assertIn("warehouse-2025-01-03.db", remaining)


# --------------------------------------------------------------------------- #
#  the disk-fill ratchet this script is built to prevent — see the module
#  docstring's "WHY ROTATION HAPPENS *BEFORE* THE COPY" section
# --------------------------------------------------------------------------- #
class SweepPartialsTests(unittest.TestCase):
    """A `.partial` never passed verification, so it was never a backup —
    and the rotation glob (`warehouse-*.db`) does not match it, so nothing
    else in this script would ever clean one up."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name)
        import backup_db
        self.mod = backup_db
        patcher = patch.object(self.mod, "BACKUP_DIR", self.dest)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _mk(self, name: str, size: int = 16) -> Path:
        p = self.dest / name
        p.write_bytes(b"x" * size)
        return p

    def test_stale_partials_and_their_crumbs_are_swept(self) -> None:
        self._mk("warehouse-2025-08-06.db.partial", 32)
        self._mk("warehouse-2025-09-11.db.partial", 64)
        self._mk("warehouse-2025-09-11.db.partial-journal", 8)
        keeper = self._mk("warehouse-2025-09-10.db", 16)

        freed = self.mod.sweep_partials()

        self.assertEqual(freed, 32 + 64 + 8)
        self.assertEqual(list(self.dest.glob("*.partial")), [])
        self.assertEqual(list(self.dest.glob("*.partial-journal")), [])
        self.assertTrue(keeper.exists(), "a COMPLETE backup must never be swept")

    def test_the_rotation_glob_does_not_see_partials(self) -> None:
        """Pins the exact mismatch that lets a partial linger forever, so
        nobody 'simplifies' sweep_partials() away believing rotate() already
        covers it."""
        self._mk("warehouse-2025-09-11.db.partial")
        self.assertEqual(self.mod.complete_backups(), [])


class RotateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name)
        import backup_db
        self.mod = backup_db
        patcher = patch.object(self.mod, "BACKUP_DIR", self.dest)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _mk(self, name: str, size: int = 16) -> Path:
        p = self.dest / name
        p.write_bytes(b"x" * size)
        return p

    def test_keeps_the_newest_n(self) -> None:
        for d in ("07", "08", "10", "12"):
            self._mk(f"warehouse-2025-09-{d}.db")
        self.mod.rotate(2)
        self.assertEqual(
            [p.name for p in self.mod.complete_backups()],
            ["warehouse-2025-09-10.db", "warehouse-2025-09-12.db"])

    def test_takes_the_wal_and_shm_crumbs_with_it(self) -> None:
        """Those crumbs are exactly what a naive `Path.unlink()` on just the
        `.db` name leaves behind — 0-byte stragglers that accumulate
        silently for as long as the script has been running."""
        self._mk("warehouse-2025-09-01.db")
        self._mk("warehouse-2025-09-01.db-wal", 8)
        self._mk("warehouse-2025-09-01.db-shm", 8)
        self._mk("warehouse-2025-09-12.db")
        self.mod.rotate(1)
        self.assertEqual([p.name for p in self.dest.iterdir()], ["warehouse-2025-09-12.db"])


class DiskGuardTests(unittest.TestCase):
    """DEFECT: the old script copied first and rotated afterward, so it
    needed room for KEEP+1 copies at the moment the disk was already
    tightest, and would happily write until the volume was full — taking
    every other writer on the machine down with it, not just this script."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.dest = self.tmp_path / "backups"
        self.dest.mkdir()
        import backup_db
        self.mod = backup_db
        for target, value in (("BACKUP_DIR", self.dest), ("KEEP", 3)):
            patcher = patch.object(self.mod, target, value)
            self.addCleanup(patcher.stop)
            patcher.start()
        self.logged = []
        log_patcher = patch.object(self.mod.wdb, "log_sync",
                                    side_effect=lambda *a, **k: self.logged.append(a))
        self.addCleanup(log_patcher.stop)
        log_patcher.start()

    def _drive(self, *, free: int, db_size: int = 1000):
        db = self.tmp_path / "warehouse.db"
        db.write_bytes(b"x" * db_size)
        db_patcher = patch.object(self.mod, "DB", db)
        self.addCleanup(db_patcher.stop)
        db_patcher.start()
        disk_patcher = patch.object(
            self.mod.shutil, "disk_usage",
            lambda _p: types.SimpleNamespace(total=0, used=0, free=free))
        self.addCleanup(disk_patcher.stop)
        disk_patcher.start()

    def test_refuses_to_start_rather_than_fill_the_disk(self) -> None:
        self._drive(free=10, db_size=1000)
        with self.assertRaises(SystemExit) as cm:
            self.mod.main()
        self.assertIn("insufficient disk", str(cm.exception))
        self.assertTrue(self.logged)
        self.assertEqual(self.logged[0][0], self.mod.PLATFORM)
        self.assertEqual(self.logged[0][3], "error", "a skipped backup must not log ok")
        self.assertEqual(list(self.dest.glob("*.partial")), [],
                          "a refused run must not leave a partial behind")

    def test_a_tight_disk_drops_one_backup_instead_of_the_pipeline(self) -> None:
        """One fewer backup beats a full disk: a full disk stops every
        writer on the box, which is the actual failure this guards against."""
        for d in ("07", "08", "10"):
            (self.dest / f"warehouse-2025-09-{d}.db").write_bytes(b"x")

        def free():
            return 10_000 if len(self.mod.complete_backups()) < self.mod.KEEP else 10

        self._drive(free=free(), db_size=1000)
        # Exercise the building blocks directly (deterministic), the same
        # sequence main() runs: sweep, rotate to KEEP, rotate to KEEP-1 only
        # if that's what it takes.
        self.mod.sweep_partials()
        self.mod.rotate(self.mod.KEEP)
        self.assertEqual(len(self.mod.complete_backups()), 3)
        self.mod.rotate(self.mod.KEEP - 1)
        self.assertEqual(len(self.mod.complete_backups()), self.mod.KEEP - 1)

    def test_rotation_is_reached_before_any_copy(self) -> None:
        """THE RATCHET, in one assertion. Rotation used to sit AFTER the
        copy, so a failing copy meant rotation — the one thing that frees
        space — was never reached, and the next night had even less room.
        Read the source: the first rotate() call must precede the copy
        STATEMENT (matched as code, `execute("VACUUM INTO`, so prose in a
        comment can never satisfy it)."""
        src = Path(self.mod.__file__).read_text(encoding="utf-8")
        body = src.split("def _backup(")[1].split("\ndef ")[0]
        copy = 'execute("VACUUM INTO'
        self.assertIn("rotate(", body, "the run no longer rotates at all")
        self.assertIn("sweep_partials(", body, "the run no longer sweeps partials")
        self.assertIn(copy, body, "the run no longer copies")
        self.assertLess(body.index("rotate("), body.index(copy),
                        "rotate() must run BEFORE the copy, or a tight disk can never recover")
        self.assertLess(body.index("sweep_partials("), body.index(copy),
                        "partials must be swept BEFORE the copy")


class SidecarSweepTests(unittest.TestCase):
    """Sidecars of a swept partial or a rotated-away backup used to pile up
    forever because the sweep only matched bare `.partial` files."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name)
        import backup_db
        self.mod = backup_db
        patcher = patch.object(self.mod, "BACKUP_DIR", self.dest)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _mk(self, name: str, size: int = 8) -> Path:
        p = self.dest / name
        p.write_bytes(b"x" * size)
        return p

    def test_orphaned_sidecars_are_swept(self) -> None:
        for n in ("warehouse-2025-01-01.db.partial-wal",
                  "warehouse-2025-01-01.db.partial-shm",
                  "warehouse-2025-01-02.db-journal"):
            self._mk(n)
        self.mod.sweep_partials()
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_sidecars_of_a_live_backup_are_kept_unless_wal_is_empty(self) -> None:
        self._mk("warehouse-2025-01-01.db")
        self._mk("warehouse-2025-01-01.db-wal", 8)
        self._mk("warehouse-2025-01-01.db-shm", 8)
        self._mk("warehouse-2025-01-01.db-journal", 8)
        self.mod.sweep_partials()
        self.assertEqual(len(list(self.dest.iterdir())), 4, "non-empty wal: keep all")

        (self.dest / "warehouse-2025-01-01.db-wal").write_bytes(b"")
        with patch.object(self.mod, "_not_in_use", return_value=False):
            self.mod.sweep_partials()
        self.assertEqual(len(list(self.dest.iterdir())), 4,
                         "empty wal but not proven idle: keep all")
        with patch.object(self.mod, "_not_in_use", return_value=True):
            self.mod.sweep_partials()
        left = sorted(p.name for p in self.dest.iterdir())
        self.assertEqual(left, ["warehouse-2025-01-01.db",
                                "warehouse-2025-01-01.db-journal"],
                         "a journal beside a complete backup is never swept")


class WeeklyZipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.dest = self.tmp_path / "backups"
        self.dest.mkdir()
        db = self.tmp_path / "warehouse.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()
        import backup_db
        self.mod = backup_db
        for target, value in (("BACKUP_DIR", self.dest), ("DB", db),
                              ("KEEP", 2), ("ZIPS", 2), ("ZIP_WEEKDAY", 6)):
            patcher = patch.object(self.mod, target, value)
            self.addCleanup(patcher.stop)
            patcher.start()
        self.logged = []
        lp = patch.object(self.mod.wdb, "log_sync",
                          side_effect=lambda *a, **k: self.logged.append(a))
        self.addCleanup(lp.stop)
        lp.start()

    def test_disabled_by_default(self) -> None:
        with patch.object(self.mod, "ZIPS", 0):
            self.assertFalse(self.mod.is_zip_day(date(2025, 1, 5)))

    def test_zip_written_only_on_zip_day_and_rotated(self) -> None:
        for d in ("2024-12-22", "2024-12-29"):
            (self.dest / f"warehouse-{d}.db.zip").write_bytes(b"z")
        self.mod.main(today=date(2025, 1, 5))  # a Sunday
        zips = sorted(p.name for p in self.dest.glob("*.zip"))
        self.assertEqual(zips, ["warehouse-2024-12-29.db.zip",
                                "warehouse-2025-01-05.db.zip"])
        self.assertEqual(self.logged[-1][3], "ok")

        self.mod.main(today=date(2025, 1, 6))  # Monday: no new zip
        self.assertEqual(len(list(self.dest.glob("*.zip"))), 2)

    def test_zip_failure_is_degraded_and_keeps_the_old_zip(self) -> None:
        old = self.dest / "warehouse-2024-12-29.db.zip"
        old.write_bytes(b"z")
        with patch.object(self.mod, "_write_zip", side_effect=OSError("boom")):
            self.mod.main(today=date(2025, 1, 5))
        self.assertEqual(self.logged[-1][3], "degraded")
        self.assertTrue(old.exists())
        self.assertEqual(list(self.dest.glob("*.zip.partial")), [])
        self.assertTrue((self.dest / "warehouse-2025-01-05.db").exists(),
                        "the nightly backup itself still succeeded")


class _RealRun(unittest.TestCase):
    """main() end to end against a tiny real database, with free space and
    log_sync faked. `self.logged` holds each log_sync call's positional args:
    (platform, started, rows, status, message)."""

    KEEP = 2
    ZIPS = 0

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.dest = self.tmp_path / "backups"
        self.dest.mkdir()
        self.db = self.tmp_path / "warehouse.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t (x) VALUES (1)")
        conn.commit()
        conn.close()
        import backup_db
        self.mod = backup_db
        self.free = 10 ** 9
        self.logged = []
        for target, attr, value in (
                (self.mod, "BACKUP_DIR", self.dest), (self.mod, "DB", self.db),
                (self.mod, "KEEP", self.KEEP), (self.mod, "ZIPS", self.ZIPS),
                (self.mod, "ZIP_WEEKDAY", 6),
                (self.mod.shutil, "disk_usage",
                 lambda _p: types.SimpleNamespace(total=0, used=0, free=self.free)),
                (self.mod.wdb, "log_sync",
                 lambda *a, **k: self.logged.append(a))):
            self._patch(target, attr, value)

    def _patch(self, target, attr, value) -> None:
        patcher = patch.object(target, attr, value)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _mk(self, name: str, size: int = 16) -> Path:
        p = self.dest / name
        p.write_bytes(b"x" * size)
        return p

    def _unlink_fails_for(self, predicate) -> None:
        real = Path.unlink

        def unlink(path, *a, **k):
            if predicate(path):
                raise PermissionError(13, "in use", str(path))
            return real(path, *a, **k)
        self._patch(Path, "unlink", unlink)


class ConsistentCopyTests(_RealRun):
    def test_the_copy_includes_rows_still_in_the_wal_of_a_live_writer(self) -> None:
        """The copy is one read snapshot of a database another connection has
        open and is writing to: committed rows that exist only in the -wal
        must be in the backup, and the writer is never blocked."""
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executemany("INSERT INTO t (x) VALUES (?)", [(i,) for i in range(50)])
        writer.commit()
        self.assertGreater(
            self.db.with_name(self.db.name + "-wal").stat().st_size, 0)

        self.mod.main(today=date(2025, 1, 8))

        writer.execute("INSERT INTO t (x) VALUES (999)")  # still writable
        writer.commit()
        backup = self.dest / "warehouse-2025-01-08.db"
        check = sqlite3.connect(backup)
        try:
            self.assertEqual(check.execute("SELECT COUNT(*) FROM t").fetchone()[0], 51)
        finally:
            check.close()
        self.assertEqual(self.logged[-1][3], "ok")
        self.assertEqual(list(self.dest.glob("*.partial")), [])


class DatedNameTests(_RealRun):
    """Rotation only ever counts and deletes `warehouse-YYYY-MM-DD` files."""

    def test_a_hand_made_copy_is_neither_counted_nor_rotated(self) -> None:
        stray = self._mk("warehouse-before-upgrade.db")
        stray_zip = self._mk("warehouse-before-upgrade.db.zip")
        for d in ("01", "02", "03"):
            self._mk(f"warehouse-2025-01-{d}.db")
            self._mk(f"warehouse-2025-01-{d}.db.zip")
        self.mod.rotate(1)
        self.mod.rotate_zips(1)
        self.assertEqual([p.name for p in self.mod.complete_backups()],
                         ["warehouse-2025-01-03.db"],
                         "the undated file sorts last; it must not pass as the newest backup")
        self.assertTrue((self.dest / "warehouse-2025-01-03.db.zip").exists())
        self.assertTrue(stray.exists() and stray_zip.exists())
        self.mod.rotate(0)
        self.assertTrue(stray.exists(), "even keep=0 leaves an undated file alone")


class RunLockTests(_RealRun):
    def test_a_run_holding_the_lock_blocks_the_start(self) -> None:
        """The refusal must come BEFORE sweep_partials(), which deletes every
        .partial it sees: a second run would otherwise delete the first run's
        live copy. The lock is a real OS lock, refused to a second handle
        even in-process, so this is the contention two processes see."""
        foreign = self._mk("warehouse-2025-01-08.db.partial", 32)
        held = self.mod.acquire_lock()
        self.assertIsNotNone(held)
        try:
            with self.assertRaises(SystemExit):
                self.mod.main(today=date(2025, 1, 8))
        finally:
            self.mod.release_lock(held)
        self.assertEqual(self.logged[-1][3], "error")
        self.assertIn("another backup run holds", self.logged[-1][4])
        self.assertEqual(foreign.read_bytes(), b"x" * 32,
                         "the other run's copy must survive")
        self.assertEqual(self.mod.complete_backups(), [], "no copy was started")

    def test_a_dead_runs_partial_does_not_wedge_the_date(self) -> None:
        """With nobody holding the lock, a same-day partial is litter from a
        run that died: swept, and today's backup proceeds."""
        self._mk("warehouse-2025-01-08.db.partial", 32)
        self.mod.main(today=date(2025, 1, 8))
        self.assertEqual(self.logged[-1][3], "ok")
        self.assertTrue((self.dest / "warehouse-2025-01-08.db").exists())
        self.assertEqual(list(self.dest.glob("*.partial")), [])

    def test_the_lock_is_released_after_every_kind_of_exit(self) -> None:
        self.free = 0
        with self.assertRaises(SystemExit):
            self.mod.main(today=date(2025, 1, 8))            # refused: no room
        self.free = 10 ** 9
        with patch.object(self.mod, "_backup", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                self.mod.main(today=date(2025, 1, 8))        # crashed
        self.mod.main(today=date(2025, 1, 8))                # succeeded
        again = self.mod.acquire_lock()
        self.assertIsNotNone(again, "a finished run must not leave the lock held")
        self.mod.release_lock(again)
        self.assertEqual([row[3] for row in self.logged], ["error", "error", "ok"])

    def test_the_lock_file_is_never_mistaken_for_a_backup_or_litter(self) -> None:
        self.mod.main(today=date(2025, 1, 8))
        self.mod.sweep_partials()
        self.mod.rotate(0)
        self.assertEqual([p.name for p in self.dest.iterdir()], [self.mod.LOCK_NAME])

    def test_a_lock_error_that_is_not_contention_is_not_called_contention(self) -> None:
        """A filesystem that cannot lock at all must surface as its own
        error, not as a permanent 'another run is in progress'."""
        with patch.object(self.mod, "_lock_file",
                          side_effect=OSError(errno.ENOLCK, "no locks available")):
            with self.assertRaises(OSError):
                self.mod.main(today=date(2025, 1, 8))
        self.assertEqual(self.logged[-1][3], "error")
        self.assertIn("no locks available", self.logged[-1][4])
        self.assertNotIn("another backup run", self.logged[-1][4])


class EveryExitLogsTests(_RealRun):
    """Each test breaks one file operation and asserts a sync_log row was
    still written: a run with no row looks like a run that never started."""

    def test_a_sidecar_that_will_not_delete_does_not_stop_the_backup(self) -> None:
        self._mk("warehouse-2025-01-06.db")
        self._mk("warehouse-2025-01-06.db-wal", 0)
        shm = self._mk("warehouse-2025-01-06.db-shm", 64)
        self._patch(self.mod, "_not_in_use", lambda p: True)
        self._unlink_fails_for(lambda p: p.name.endswith("-shm"))
        self.mod.main(today=date(2025, 1, 8))
        self.assertEqual(self.logged[-1][3], "ok")
        self.assertTrue(shm.exists())

    def test_a_rotation_that_cannot_delete_is_degraded(self) -> None:
        """The backup is good, but retention is not being enforced: neither
        `ok` nor `error` is true."""
        old = [self._mk(f"warehouse-2025-01-0{d}.db") for d in (4, 5, 6)]
        self._unlink_fails_for(lambda p: p.name == "warehouse-2025-01-04.db")
        self.mod.main(today=date(2025, 1, 8))
        self.assertEqual(self.logged[-1][3], "degraded")
        self.assertIn("rotation FAILED", self.logged[-1][4])
        self.assertIn("warehouse-2025-01-04.db", self.logged[-1][4])
        self.assertTrue(old[0].exists())
        self.assertTrue((self.dest / "warehouse-2025-01-08.db").exists())

    def test_a_failed_copy_logs_before_cleaning_up(self) -> None:
        """A partial that will not delete must not cost the row, and the
        cleanup failure must not mask the original error."""
        order = []
        self._patch(self.mod.wdb, "log_sync",
                    lambda *a, **k: (order.append("log"), self.logged.append(a)))

        def stuck_rm(p):
            order.append("rm")
            raise PermissionError(13, "in use", str(p))
        self._patch(self.mod, "_rm", stuck_rm)

        class FakeConn:
            def execute(self, sql, params=()):
                Path(params[0]).write_bytes(b"half a copy")
                raise sqlite3.OperationalError("database or disk is full")

            def close(self):
                pass
        self._patch(self.mod.sqlite3, "connect", lambda *a, **k: FakeConn())
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk is full"):
            self.mod.main(today=date(2025, 1, 8))
        self.assertEqual(order[:2], ["log", "rm"],
                         "the row must be written before cleanup")
        self.assertEqual(self.logged[-1][3], "error")
        self.assertIn("disk is full", self.logged[-1][4])

    def test_a_failed_copy_takes_its_partial_with_it(self) -> None:
        class Dying:
            def execute(self, sql, params=()):
                Path(params[0]).write_bytes(b"half a copy")
                raise sqlite3.OperationalError("interrupted")

            def close(self):
                pass
        self._patch(self.mod.sqlite3, "connect", lambda *a, **k: Dying())
        with self.assertRaises(sqlite3.OperationalError):
            self.mod.main(today=date(2025, 1, 8))
        self.assertEqual(list(self.dest.glob("warehouse-*")), [])
        self.assertEqual(self.logged[-1][3], "error")

    def test_a_missing_database_still_logs(self) -> None:
        self._patch(self.mod, "DB", self.tmp_path / "gone.db")
        with self.assertRaises(FileNotFoundError):
            self.mod.main(today=date(2025, 1, 8))
        self.assertEqual(self.logged[-1][3], "error")
        self.assertEqual(list(self.dest.glob("*.partial")), [])

    def test_an_unwritable_sync_log_still_fails_the_run(self) -> None:
        """When even log_sync fails, the exit code is the only signal left:
        a good copy must not exit 0 silently, and the reason goes to stderr."""
        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        self._patch(self.mod.wdb, "log_sync", locked)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.mod.main(today=date(2025, 1, 8))
        self.assertTrue(cm.exception.code)
        self.assertIn("COULD NOT WRITE sync_log", err.getvalue())
        self.assertTrue((self.dest / "warehouse-2025-01-08.db").exists())


class ZipRotationFailureTests(_RealRun):
    ZIPS = 2

    def test_a_zip_rotation_that_cannot_delete_is_degraded(self) -> None:
        for d in ("2024-12-22", "2024-12-29"):
            self._mk(f"warehouse-{d}.db.zip")
        self._unlink_fails_for(lambda p: p.name.endswith("12-22.db.zip"))
        self.mod.main(today=date(2025, 1, 5))  # a Sunday
        self.assertEqual(self.logged[-1][3], "degraded")
        self.assertIn("zip rotation FAILED", self.logged[-1][4])
        self.assertTrue((self.dest / "warehouse-2025-01-05.db.zip").exists())

    def test_zip_days_reserve_room_for_the_zip(self) -> None:
        """A volume that fits the copy but not the zip refuses on the zip
        day and proceeds on any other day."""
        size = self.mod.db_bytes()
        self.free = int(size * self.mod.FREE_MARGIN) + 5
        with self.assertRaises(SystemExit):
            self.mod.main(today=date(2025, 1, 5))   # Sunday
        self.assertEqual(self.logged[-1][3], "error")
        self.mod.main(today=date(2025, 1, 8))       # Wednesday
        self.assertEqual(self.logged[-1][3], "ok")


class FreeSpaceNeedTests(_RealRun):
    def test_need_counts_the_live_wal(self) -> None:
        """The copy reads the snapshot, WAL pages included; sizing the need
        on the main file alone under-reserves by however big the WAL is."""
        self.db.write_bytes(b"x" * 1000)
        day = date(2025, 1, 8)
        self.assertEqual(self.mod.required_bytes(day),
                         (1000, int(1000 * self.mod.FREE_MARGIN)))
        self.db.with_name(self.db.name + "-wal").write_bytes(b"w" * 500)
        self.assertEqual(self.mod.required_bytes(day),
                         (1500, int(1500 * self.mod.FREE_MARGIN)))
        with patch.object(self.mod, "ZIPS", 1):
            self.assertEqual(
                self.mod.required_bytes(date(2025, 1, 5))[1],
                int(1500 * self.mod.FREE_MARGIN)
                + int(1500 * self.mod.ZIP_SIZE_FRACTION))

    def test_a_disk_that_fits_the_db_but_not_its_wal_refuses(self) -> None:
        self.db.write_bytes(b"x" * 1000)
        self.db.with_name(self.db.name + "-wal").write_bytes(b"w" * 500)
        self.free = 1200
        with self.assertRaises(SystemExit) as cm:
            self.mod.main(today=date(2025, 1, 8))
        self.assertIn("insufficient disk", str(cm.exception))
        self.assertIn("-wal", str(cm.exception))
        self.assertEqual(self.logged[-1][3], "error")


class DatabasePathTests(unittest.TestCase):
    def test_the_backup_copies_the_database_log_sync_writes_to(self) -> None:
        """One definition of WAREHOUSE_DB (warehouse/db.py). A second parse
        here could copy one file while the run's row lands in another."""
        with tempfile.TemporaryDirectory() as tmp:
            override = str(Path(tmp) / "elsewhere" / "other.db")
            code = ("import backup_db; from warehouse import db; "
                    "print(backup_db.DB == db.DB_PATH); print(backup_db.DB)")
            out = subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True, text=True, check=True,
                env={**os.environ, "WAREHOUSE_DB": override})
        self.assertEqual(out.stdout.splitlines(), ["True", override])


class InUseProbeTests(unittest.TestCase):
    """`_not_in_use` must only say True when that is proven."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        import backup_db
        self.mod = backup_db

    def _open_wal_db(self, path: Path) -> sqlite3.Connection:
        """A live connection on a WAL database whose -wal is zero-length
        (checkpointed and truncated): exactly what the sweep looks for."""
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t (x) VALUES (1)")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(path.with_name(path.name + "-wal").stat().st_size, 0)
        return con

    def test_where_idleness_cannot_be_proven_the_answer_is_no(self) -> None:
        idle = self.dir / "x-wal"
        idle.write_bytes(b"")
        with patch.object(self.mod.sys, "platform", "linux"):
            self.assertFalse(self.mod._not_in_use(idle))

    @unittest.skipUnless(sys.platform == "win32", "share-mode probe is Windows-only")
    def test_it_sees_sqlites_open_wal_and_shm(self) -> None:
        """SQLite shares these files and locks only the bytes it uses, so a
        byte-range lock probe calls them idle; the share-nothing open does not."""
        db = self.dir / "warehouse-2025-01-06.db"
        self._open_wal_db(db)
        for suffix in ("-wal", "-shm"):
            self.assertFalse(self.mod._not_in_use(db.with_name(db.name + suffix)), suffix)
        idle = self.dir / "idle-wal"
        idle.write_bytes(b"")
        self.assertTrue(self.mod._not_in_use(idle), "a closed file is not in use")
        self.assertFalse(self.mod._not_in_use(self.dir / "missing-wal"))

    def test_a_backup_someone_has_open_keeps_its_sidecars(self) -> None:
        """End to end on every OS: a kept backup is open in WAL mode with a
        zero-length -wal. The sweep must leave both sidecars and the open
        connection must keep working."""
        db = self.dir / "warehouse-2025-01-06.db"
        con = self._open_wal_db(db)
        with patch.object(self.mod, "BACKUP_DIR", self.dir):
            self.assertEqual(self.mod.sweep_partials(), 0)
        self.assertTrue(db.with_name(db.name + "-wal").exists())
        self.assertTrue(db.with_name(db.name + "-shm").exists())
        self.assertEqual(con.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    @unittest.skipUnless(sys.platform == "win32", "share-mode probe is Windows-only")
    def test_idle_sidecars_of_a_kept_backup_are_swept(self) -> None:
        db = self.dir / "warehouse-2025-01-06.db"
        db.write_bytes(b"x")
        wal = self.dir / (db.name + "-wal")
        shm = self.dir / (db.name + "-shm")
        wal.write_bytes(b"")
        shm.write_bytes(b"x" * 64)
        with patch.object(self.mod, "BACKUP_DIR", self.dir):
            self.assertEqual(self.mod.sweep_partials(), 64)
        self.assertFalse(wal.exists() or shm.exists())
        self.assertTrue(db.exists())


if __name__ == "__main__":
    unittest.main()
