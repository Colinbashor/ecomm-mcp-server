"""merchant_center_labels_export: dry-run default, the row floor, logging and the
push. Hermetic: in-memory SQLite, Drive patched out."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import merchant_center_labels_export as mx
from feed_fixtures import CLEAN_ENV, add_order, add_variant, make_db

ENV = {**CLEAN_ENV, "GMC_FEED_ID_FORMAT": "shopify", "GMC_LABELS_MIN_ROWS": "2",
       "GMC_LABELS_SHEET_ID": "YOUR_SHEET_FILE_ID", "GMC_CREDENTIALS_FILE": "key.json"}


def _db(n=3):
    conn = make_db()
    for i in range(n):
        add_variant(conn, f"100000000010{i}", sku=f"SKU-100{i}")
    add_order(conn, "#1001", "SKU-1000", 2)
    return conn


@mock.patch.dict(os.environ, ENV)
class ExportTests(unittest.TestCase):
    def _run(self, execute, conn=None, push_error=None):
        logged = []
        with mock.patch.object(mx, "_open_db", return_value=conn or _db()), \
                mock.patch.object(mx.drive, "replace_with_rows",
                                  side_effect=push_error) as push, \
                mock.patch.object(mx.db, "log_sync",
                                  side_effect=lambda *a: logged.append(a)):
            rc = mx.run(execute)
        return rc, logged, push

    def test_rows_use_the_configured_id_format(self) -> None:
        sheet, stats = mx.build_sheet_rows(_db())
        self.assertEqual(sheet[0][0], "shopify_US_1000000000001_1000000000100")
        self.assertEqual(len(sheet[0]), len(mx.HEADER))
        self.assertEqual(stats["rows"], 3)

    def test_dry_run_pushes_and_logs_nothing(self) -> None:
        rc, logged, push = self._run(False)
        self.assertEqual((rc, logged, push.call_count), (0, [], 0))

    def test_execute_pushes_the_whole_build_and_logs_ok(self) -> None:
        rc, logged, push = self._run(True)
        self.assertEqual(rc, 0)
        args = push.call_args.args
        self.assertEqual((args[0], args[2], len(args[3]), args[4]),
                         ("YOUR_SHEET_FILE_ID", mx.HEADER, 3, "key.json"))
        self.assertEqual((logged[-1][0], logged[-1][2], logged[-1][3]),
                         ("gmc_labels_export", 3, "ok"))

    def test_a_build_below_the_floor_is_refused(self) -> None:
        rc, logged, push = self._run(True, conn=_db(n=1))
        self.assertEqual((rc, push.call_count), (1, 0))
        self.assertEqual(logged[-1][3], "error")
        self.assertIn("REFUSED", logged[-1][4])

    def test_a_push_failure_logs_error_and_exits_nonzero(self) -> None:
        rc, logged, _ = self._run(True, push_error=RuntimeError("Drive update failed"))
        self.assertEqual(rc, 1)
        self.assertEqual(logged[-1][3], "error")
        self.assertIn("Drive update failed", logged[-1][4])

    def test_a_build_failure_logs_error(self) -> None:
        with mock.patch.object(mx, "_open_db", side_effect=RuntimeError("no database")), \
                mock.patch.object(mx.db, "log_sync") as log:
            with self.assertRaises(RuntimeError):
                mx.run(True)
        self.assertEqual(log.call_args.args[3], "error")

    def test_execute_requires_the_sheet_and_key(self) -> None:
        with mock.patch.dict(os.environ, {"GMC_LABELS_SHEET_ID": ""}):
            mx.check_required_env(False)               # a dry run does not need them
            with self.assertRaises(SystemExit):
                mx.check_required_env(True)


if __name__ == "__main__":
    unittest.main()
