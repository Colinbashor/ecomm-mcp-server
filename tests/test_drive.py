"""warehouse/drive.py: in-place Drive replace with bounded retry. Hermetic."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from warehouse import drive


class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


class ReplaceContentTests(unittest.TestCase):
    def _run(self, side_effect, attempts=drive.MAX_ATTEMPTS):
        sleeps: list[float] = []
        with mock.patch.object(drive.requests, "patch", side_effect=side_effect) as p:
            try:
                drive.replace_content("YOUR_SHEET_FILE_ID", "feed.xlsx", b"x", "tok",
                                      attempts=attempts, sleep=sleeps.append)
                err = None
            except RuntimeError as exc:
                err = exc
        return p, sleeps, err

    def test_success_first_time(self) -> None:
        p, sleeps, err = self._run([_Resp(200)])
        self.assertIsNone(err)
        self.assertEqual((p.call_count, sleeps), (1, []))
        self.assertIn("YOUR_SHEET_FILE_ID", p.call_args.args[0])
        self.assertEqual(p.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})

    def test_transient_status_and_transport_errors_are_retried(self) -> None:
        p, sleeps, err = self._run([_Resp(503), requests.ConnectionError("reset"),
                                    _Resp(429), _Resp(200)])
        self.assertIsNone(err)
        self.assertEqual(p.call_count, 4)
        self.assertEqual(sleeps, [10, 20, 40])

    def test_a_permanent_4xx_fails_at_once(self) -> None:
        p, sleeps, err = self._run([_Resp(404, "File not found")])
        self.assertIn("not retried", str(err))
        self.assertIn("404", str(err))
        self.assertEqual((p.call_count, sleeps), (1, []))

    def test_retries_are_bounded_and_the_last_failure_is_named(self) -> None:
        p, sleeps, err = self._run([_Resp(500, "backend error")] * 10, attempts=3)
        self.assertEqual(p.call_count, 3)
        self.assertIn("after 3 attempts", str(err))
        self.assertIn("backend error", str(err))

    def test_every_attempt_resends_the_whole_body(self) -> None:
        p, _, _ = self._run([_Resp(502), _Resp(200)])
        for call in p.call_args_list:
            self.assertEqual(call.kwargs["files"]["file"][1], b"x")


class RowsTests(unittest.TestCase):
    def test_write_xlsx_header_rows_and_blank_cells(self) -> None:
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.xlsx"
            drive.write_xlsx(path, "labels", ["id", "custom_label_0"],
                             [["SKU-1001", "Margin 40-50%"], ["SKU-1002", ""]])
            ws = load_workbook(path).active
            self.assertEqual(ws.title, "labels")
            self.assertEqual([list(r) for r in ws.iter_rows(values_only=True)],
                             [["id", "custom_label_0"], ["SKU-1001", "Margin 40-50%"],
                              ["SKU-1002", None]])

    def test_replace_with_rows_uploads_and_removes_its_temp_file(self) -> None:
        seen = {}

        def fake_replace(file_id, path, sa_file, **kw):
            seen.update(file_id=file_id, path=Path(path), exists=Path(path).exists(),
                        sa_file=sa_file)

        with mock.patch.object(drive, "replace_with_file", side_effect=fake_replace):
            drive.replace_with_rows("YOUR_SHEET_FILE_ID", "t", ["id"], [["SKU-1001"]],
                                    "key.json")
        self.assertEqual((seen["file_id"], seen["sa_file"], seen["exists"]),
                         ("YOUR_SHEET_FILE_ID", "key.json", True))
        self.assertFalse(seen["path"].exists())


if __name__ == "__main__":
    unittest.main()
