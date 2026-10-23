"""Hermetic tests for disk_probe.py: three states, always exits 0, always logs."""
from __future__ import annotations

import types
import unittest
from pathlib import Path
from unittest.mock import patch

import disk_probe


def _usage(free_gb: float):
    return lambda _p: types.SimpleNamespace(
        total=0, used=0, free=int(free_gb * disk_probe.GB))


class ClassifyTests(unittest.TestCase):
    def test_three_states(self) -> None:
        self.assertEqual(disk_probe.classify(50, 20, 10), "ok")
        self.assertEqual(disk_probe.classify(20, 20, 10), "ok")
        self.assertEqual(disk_probe.classify(15, 20, 10), "degraded")
        self.assertEqual(disk_probe.classify(9.9, 20, 10), "error")


class ProbeTests(unittest.TestCase):
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


class MainTests(unittest.TestCase):
    def _run(self, free_gb: float):
        with patch.object(disk_probe.shutil, "disk_usage", _usage(free_gb)), \
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


if __name__ == "__main__":
    unittest.main()
