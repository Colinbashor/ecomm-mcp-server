"""Hermetic tests for disk_probe.py: three states, thresholds that follow the
backup's need with the env values as floors, always exits 0, always logs.

Sizes are PATCHED, never written: nothing here creates a large file on the
disk the probe exists to protect.
"""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_db
import disk_probe

GB = 1024 ** 3  # the test's own constant, so a changed disk_probe.GB fails here


def _usage(free_gb: float):
    return lambda _p: types.SimpleNamespace(total=0, used=0, free=int(free_gb * GB))


def _no_backup_need(_path):
    return 0.0, 0.0, "no backup folder, fixed thresholds only"


class ClassifyTests(unittest.TestCase):
    def test_three_states(self) -> None:
        self.assertEqual(disk_probe.classify(50, 20, 10), "ok")
        self.assertEqual(disk_probe.classify(20, 20, 10), "ok")
        self.assertEqual(disk_probe.classify(15, 20, 10), "degraded")
        self.assertEqual(disk_probe.classify(9.9, 20, 10), "error")


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(disk_probe, "backup_need", _no_backup_need)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_thresholds_come_from_env(self) -> None:
        with patch.dict("os.environ", {"DISK_PROBE_DEGRADED_GB": "100",
                                       "DISK_PROBE_ERROR_GB": "50"}), \
                patch.object(disk_probe.shutil, "disk_usage", _usage(60)):
            status, _ = disk_probe.probe(Path("."))
        self.assertEqual(status, "degraded")

    def test_bad_threshold_is_rejected(self) -> None:
        with patch.dict("os.environ", {"DISK_PROBE_ERROR_GB": "lots"}):
            with self.assertRaises(SystemExit):
                disk_probe.probe(Path("."))

    def test_watched_path_walks_up_to_an_existing_folder(self) -> None:
        with patch.dict("os.environ", {"DISK_PROBE_PATH": str(
                Path(__file__).parent / "no" / "such" / "dir")}):
            self.assertTrue(disk_probe.watched_path().exists())


class BackupRelativeThresholdTests(unittest.TestCase):
    """The thresholds follow what backup_db.py needs on the watched volume.
    A fixed number of GB reads `ok` while the backup can no longer fit."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._set(backup_db, "BACKUP_DIR", self.dir)   # exists, same volume
        self._set(backup_db, "ZIPS", 1)
        env = patch.dict("os.environ", {"DISK_PROBE_DEGRADED_GB": "",
                                        "DISK_PROBE_ERROR_GB": ""})
        self.addCleanup(env.stop)
        env.start()

    def _set(self, target, attr, value) -> None:
        patcher = patch.object(target, attr, value)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _db(self, gb: float) -> None:
        self._set(backup_db, "db_bytes", lambda: int(gb * GB))

    def _status(self, free_gb: float) -> tuple[str, str]:
        with patch.object(disk_probe.shutil, "disk_usage", _usage(free_gb)):
            return disk_probe.probe(self.dir)

    def test_free_space_over_the_floors_but_under_one_backup_is_an_error(self) -> None:
        self._db(200)
        status, msg = self._status(100)   # five times the fixed degraded floor
        self.assertEqual(status, "error")
        self.assertIn(f"error under {200 * backup_db.FREE_MARGIN:.1f}", msg)
        self.assertIn("database + WAL 200.0 GB", msg)

    def test_room_for_a_plain_copy_but_not_a_zip_night_is_degraded(self) -> None:
        self._db(200)
        error_gb, degraded_gb, _ = disk_probe.thresholds(self.dir)
        self.assertAlmostEqual(error_gb, 200 * backup_db.FREE_MARGIN)
        self.assertAlmostEqual(
            degraded_gb, 200 * (backup_db.FREE_MARGIN + backup_db.ZIP_SIZE_FRACTION))
        self.assertEqual(self._status((error_gb + degraded_gb) / 2)[0], "degraded")
        self.assertEqual(self._status(degraded_gb + 1)[0], "ok")

    def test_without_weekly_zips_there_is_no_zip_night_to_reserve_for(self) -> None:
        self._db(200)
        self._set(backup_db, "ZIPS", 0)
        error_gb, degraded_gb, _ = disk_probe.thresholds(self.dir)
        self.assertAlmostEqual(degraded_gb, error_gb)
        self.assertEqual(self._status(error_gb + 1)[0], "ok")

    def test_the_probe_sizes_on_what_the_backup_copies(self) -> None:
        """Database + live -wal, through backup_db's own definition."""
        db = self.dir / "warehouse.db"
        db.write_bytes(b"x" * 600)
        (self.dir / "warehouse.db-wal").write_bytes(b"w" * 400)
        self._set(backup_db, "DB", db)
        plain, _, _ = disk_probe.backup_need(self.dir)
        self.assertAlmostEqual(plain, 1000 * backup_db.FREE_MARGIN / GB)

    def test_the_env_values_survive_as_floors(self) -> None:
        """A small database never relaxes the probe below them."""
        self._db(1)
        self.assertEqual(self._status(15)[0], "degraded")
        self.assertEqual(self._status(5)[0], "error")
        self.assertEqual(self._status(25)[0], "ok")

    def test_an_unreadable_database_falls_back_to_the_floors_and_says_so(self) -> None:
        self._set(backup_db, "DB", self.dir / "missing.db")
        status, msg = self._status(15)
        self.assertEqual(status, "degraded")
        self.assertIn("fixed thresholds only", msg)
        self.assertIn("unreadable", msg)

    def test_no_backup_folder_means_fixed_thresholds(self) -> None:
        """An install that never runs backup_db.py must not be told its disk
        is too small for a backup it does not make."""
        self._db(200)
        self._set(backup_db, "BACKUP_DIR", self.dir / "never-created")
        status, msg = self._status(25)
        self.assertEqual(status, "ok")
        self.assertIn("no backup folder", msg)

    def test_backups_on_another_volume_mean_fixed_thresholds(self) -> None:
        self._db(200)

        def stat(p, *a, **k):
            return types.SimpleNamespace(
                st_dev=1 if Path(p) == backup_db.BACKUP_DIR else 2)
        with patch.object(disk_probe.os, "stat", stat):
            plain, zip_night, note = disk_probe.backup_need(self.dir / "watched")
        self.assertEqual((plain, zip_night), (0.0, 0.0))
        self.assertIn("another volume", note)


class MainTests(unittest.TestCase):
    def _run(self, free_gb: float):
        with patch.object(disk_probe, "backup_need", _no_backup_need), \
                patch.object(disk_probe.shutil, "disk_usage", _usage(free_gb)), \
                patch.object(disk_probe.wdb, "log_sync") as log:
            rc = disk_probe.main()
        return rc, log.call_args.args

    def test_error_still_exits_zero_and_logs(self) -> None:
        rc, args = self._run(1)
        self.assertEqual(rc, 0)
        self.assertEqual(args[0], "disk_space")
        self.assertEqual(args[3], "error")
        self.assertEqual(args[2], 0)

    def test_ok_logs_one_row(self) -> None:
        rc, args = self._run(500)
        self.assertEqual((rc, args[3], args[2]), (0, "ok", 1))

    def test_a_probe_that_raises_still_logs_a_row(self) -> None:
        """Never a silent zero: the row is the whole signal."""
        def boom(_p):
            raise OSError("volume gone")
        with patch.object(disk_probe, "backup_need", _no_backup_need), \
                patch.object(disk_probe.shutil, "disk_usage", boom), \
                patch.object(disk_probe.wdb, "log_sync") as log:
            self.assertEqual(disk_probe.main(), 0)
        args = log.call_args.args
        self.assertEqual((args[0], args[3]), ("disk_space", "error"))
        self.assertIn("volume gone", args[4])


if __name__ == "__main__":
    unittest.main()
