from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import run_sync


class RunSyncFailureTests(unittest.TestCase):
    @patch.dict(run_sync.os.environ, {"TEST_TOKEN": "set"}, clear=False)
    @patch.object(run_sync.db, "log_sync")
    @patch.object(run_sync.db, "init_db")
    def test_connector_error_is_returned_to_caller(
        self,
        init_db_mock,
        log_sync_mock,
    ) -> None:
        module = Mock()
        module.sync.side_effect = RuntimeError("API down")
        with (
            patch.dict(
                run_sync.CONNECTORS,
                {"test": (module, "ads")},
                clear=True,
            ),
            patch.dict(
                run_sync.REQUIRED_ENV,
                {"test": ("TEST_TOKEN",)},
                clear=True,
            ),
        ):
            failures = run_sync.run(
                ["test"],
                "2026-07-27",
                "2026-07-28",
            )

        self.assertEqual(failures, ["test"])
        init_db_mock.assert_called_once()
        self.assertEqual(log_sync_mock.call_args.args[3], "error")


class RunSyncEmptyPullTests(unittest.TestCase):
    def _run(self, rows: list[dict], written: int):
        module = Mock()
        module.sync.return_value = rows
        with (
            patch.dict(run_sync.os.environ, {"TEST_TOKEN": "set"}, clear=False),
            patch.object(run_sync.db, "init_db"),
            patch.object(run_sync.db, "upsert_ad_metrics", return_value=written),
            patch.object(run_sync.db, "log_sync") as log_sync_mock,
            patch.dict(run_sync.CONNECTORS, {"test": (module, "ads")}, clear=True),
            patch.dict(run_sync.REQUIRED_ENV, {"test": ("TEST_TOKEN",)}, clear=True),
        ):
            failures = run_sync.run(["test"], "2026-07-27", "2026-07-28")
        return failures, log_sync_mock.call_args.args

    def test_zero_rows_logs_degraded_not_ok(self) -> None:
        failures, args = self._run([], 0)
        self.assertEqual(failures, [])          # not an exception -> not a failure
        self.assertEqual(args[2], 0)
        self.assertEqual(args[3], "degraded")

    def test_rows_written_logs_ok(self) -> None:
        failures, args = self._run([{"x": 1}], 1)
        self.assertEqual(failures, [])
        self.assertEqual((args[2], args[3]), (1, "ok"))


if __name__ == "__main__":
    unittest.main()
