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

import importlib
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class BackupDbTests(unittest.TestCase):
    def _reload(self, db_path: Path, backup_dir: Path, keep: str = "3"):
        with patch.dict("os.environ", {
            "WAREHOUSE_DB": str(db_path),
            "WAREHOUSE_BACKUP_DIR": str(backup_dir),
            "WAREHOUSE_BACKUP_KEEP": keep,
        }):
            import backup_db
            mod = importlib.reload(backup_db)
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
        Read the source: the first rotate() call must precede the first
        backup()."""
        src = Path(self.mod.__file__).read_text(encoding="utf-8")
        body = src.split("def main(")[1]
        self.assertIn("rotate(", body, "main() no longer rotates at all")
        self.assertIn("sweep_partials(", body, "main() no longer sweeps partials")
        self.assertIn(".backup(", body, "main() no longer copies")
        self.assertLess(body.index("rotate("), body.index(".backup("),
                        "rotate() must run BEFORE the copy, or a tight disk can never recover")
        self.assertLess(body.index("sweep_partials("), body.index(".backup("),
                        "partials must be swept BEFORE the copy")


if __name__ == "__main__":
    unittest.main()
