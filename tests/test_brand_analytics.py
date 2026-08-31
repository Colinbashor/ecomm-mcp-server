"""Hermetic tests for warehouse/brand_analytics.py — no network, no real DB.

Covers: the Sun-Sat week validator/window builder, the FATAL-reason-from-
document lookup, the create/poll/download phases (create's 429 retry,
check_ba_report's PENDING/DONE/CANCELLED/FATAL mapping, fetch_ba_records'
records_key handling), run_ba_report's terminal-state dispatch
(DONE/CANCELLED/FATAL/timeout), the flat-memory streaming JSON reader
(top-level-array detection, chunk-boundary splitting, gzip vs. plain-text
bodies), and await_ba_report's terminal-state dispatch.
"""
from __future__ import annotations

import gzip
import io
import os
import unittest
from datetime import date
from unittest.mock import patch

from warehouse import brand_analytics as ba


class _FakeResp:
    def __init__(self, status: int = 200, payload: object | None = None, text: str = "",
                 content: bytes = b"") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = content

    def json(self) -> object:
        return self._payload


class SundaySaturdayTests(unittest.TestCase):
    def test_valid_sunday_returns_full_week_window(self) -> None:
        start, end = ba.sunday_saturday(date(2026, 7, 19))  # a Sunday
        self.assertEqual(start, "2026-07-19T00:00:00Z")
        self.assertEqual(end, "2026-07-25T23:59:59Z")

    def test_non_sunday_raises(self) -> None:
        with self.assertRaises(ValueError):
            ba.sunday_saturday(date(2026, 7, 20))  # a Monday


class WindowTests(unittest.TestCase):
    def test_week_period_delegates_to_sunday_saturday(self) -> None:
        start, end = ba._window("WEEK", date(2026, 7, 19), None, None)
        self.assertEqual(start, "2026-07-19T00:00:00Z")

    def test_week_period_without_sunday_raises(self) -> None:
        with self.assertRaises(ValueError):
            ba._window("WEEK", None, None, None)

    def test_month_period_uses_given_bounds(self) -> None:
        start, end = ba._window("MONTH", None, date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(start, "2026-06-01T00:00:00Z")
        self.assertEqual(end, "2026-06-30T23:59:59Z")

    def test_month_period_missing_bounds_raises(self) -> None:
        with self.assertRaises(ValueError):
            ba._window("MONTH", None, None, None)


class FatalReasonTests(unittest.TestCase):
    def test_no_document_id_gives_generic_message(self) -> None:
        msg = ba._fatal_reason("https://host", {"processingStatus": "FATAL"})
        self.assertIn("FATAL with no document", msg)

    def test_downloads_document_and_surfaces_error_details(self) -> None:
        with patch.object(ba, "_download_json", return_value={"errorDetails": [{"message": "bad asin"}]}):
            msg = ba._fatal_reason("https://host", {"reportDocumentId": "doc1"})
        self.assertIn("bad asin", msg)

    def test_download_failure_is_reported_not_raised(self) -> None:
        with patch.object(ba, "_download_json", side_effect=RuntimeError("boom")):
            msg = ba._fatal_reason("https://host", {"reportDocumentId": "doc1"})
        self.assertIn("could not read error document", msg)


class CreateBaReportTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"
        self._token_patch = patch.object(ba, "_access_token", return_value="tok")
        self._token_patch.start()

    def tearDown(self) -> None:
        self._token_patch.stop()

    def test_returns_report_id_on_success(self) -> None:
        with patch.object(ba, "requests") as fake_requests:
            fake_requests.post.return_value = _FakeResp(200, {"reportId": "r1"})
            rid = ba.create_ba_report("SOME_REPORT", date(2026, 7, 19), {"reportPeriod": "WEEK"})
        self.assertEqual(rid, "r1")

    def test_retries_on_429_then_succeeds(self) -> None:
        with patch.object(ba, "requests") as fake_requests, patch.object(ba.time, "sleep"):
            fake_requests.post.side_effect = [
                _FakeResp(429), _FakeResp(200, {"reportId": "r2"}),
            ]
            rid = ba.create_ba_report("SOME_REPORT", date(2026, 7, 19))
        self.assertEqual(rid, "r2")

    def test_non_200_202_raises(self) -> None:
        with patch.object(ba, "requests") as fake_requests:
            fake_requests.post.return_value = _FakeResp(400, text="bad request")
            with self.assertRaises(RuntimeError):
                ba.create_ba_report("SOME_REPORT", date(2026, 7, 19))


class CheckBaReportTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"
        self._token_patch = patch.object(ba, "_access_token", return_value="tok")
        self._token_patch.start()

    def tearDown(self) -> None:
        self._token_patch.stop()

    def test_done_returns_document_id(self) -> None:
        with patch.object(ba, "requests") as fake_requests:
            fake_requests.get.return_value = _FakeResp(
                200, {"processingStatus": "DONE", "reportDocumentId": "doc1"})
            status, payload = ba.check_ba_report("r1")
        self.assertEqual((status, payload), ("DONE", "doc1"))

    def test_cancelled_returns_none_payload(self) -> None:
        with patch.object(ba, "requests") as fake_requests:
            fake_requests.get.return_value = _FakeResp(200, {"processingStatus": "CANCELLED"})
            status, payload = ba.check_ba_report("r1")
        self.assertEqual((status, payload), ("CANCELLED", None))

    def test_fatal_returns_reason(self) -> None:
        with patch.object(ba, "requests") as fake_requests, \
             patch.object(ba, "_fatal_reason", return_value="asin required"):
            fake_requests.get.return_value = _FakeResp(200, {"processingStatus": "FATAL"})
            status, payload = ba.check_ba_report("r1")
        self.assertEqual((status, payload), ("FATAL", "asin required"))

    def test_in_progress_is_pending(self) -> None:
        with patch.object(ba, "requests") as fake_requests:
            fake_requests.get.return_value = _FakeResp(200, {"processingStatus": "IN_PROGRESS"})
            status, payload = ba.check_ba_report("r1")
        self.assertEqual((status, payload), ("PENDING", None))

    def test_transient_request_exception_is_pending(self) -> None:
        import requests as real_requests
        with patch.object(ba, "requests") as fake_requests:
            fake_requests.RequestException = real_requests.RequestException
            fake_requests.get.side_effect = real_requests.RequestException("boom")
            status, payload = ba.check_ba_report("r1")
        self.assertEqual((status, payload), ("PENDING", None))


class FetchBaRecordsTests(unittest.TestCase):
    def test_explicit_records_key(self) -> None:
        with patch.object(ba, "_download_json", return_value={"dataByAsin": [1, 2], "other": "x"}):
            self.assertEqual(ba.fetch_ba_records("doc1", "dataByAsin"), [1, 2])

    def test_missing_records_key_returns_empty_list(self) -> None:
        with patch.object(ba, "_download_json", return_value={"dataByAsin": None}):
            self.assertEqual(ba.fetch_ba_records("doc1", "dataByAsin"), [])

    def test_auto_detect_first_list_valued_key(self) -> None:
        with patch.object(ba, "_download_json",
                          return_value={"reportSpecification": {}, "dataByAsin": [{"a": 1}]}):
            self.assertEqual(ba.fetch_ba_records("doc1"), [{"a": 1}])

    def test_auto_detect_no_list_returns_empty(self) -> None:
        with patch.object(ba, "_download_json", return_value={"reportSpecification": {}}):
            self.assertEqual(ba.fetch_ba_records("doc1"), [])


class RunBaReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleep_patch = patch.object(ba.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_done_returns_records(self) -> None:
        with patch.object(ba, "create_ba_report", return_value="r1"), \
             patch.object(ba, "check_ba_report", return_value=("DONE", "doc1")), \
             patch.object(ba, "fetch_ba_records", return_value=[{"asin": "B1"}]):
            recs = ba.run_ba_report("SOME_REPORT", date(2026, 7, 19), {"reportPeriod": "WEEK"})
        self.assertEqual(recs, [{"asin": "B1"}])

    def test_cancelled_raises_ba_report_cancelled(self) -> None:
        with patch.object(ba, "create_ba_report", return_value="r1"), \
             patch.object(ba, "check_ba_report", return_value=("CANCELLED", None)):
            with self.assertRaises(ba.BAReportCancelled):
                ba.run_ba_report("SOME_REPORT", date(2026, 7, 19))

    def test_fatal_raises_ba_report_fatal_with_reason(self) -> None:
        with patch.object(ba, "create_ba_report", return_value="r1"), \
             patch.object(ba, "check_ba_report", return_value=("FATAL", "asin required")):
            with self.assertRaises(ba.BAReportFatal) as cm:
                ba.run_ba_report("SOME_REPORT", date(2026, 7, 19))
        self.assertIn("asin required", str(cm.exception))

    def test_pending_forever_times_out(self) -> None:
        # Force the deadline to already be in the past so the loop body never
        # executes and we hit the TimeoutError immediately (no real sleeping).
        with patch.object(ba, "create_ba_report", return_value="r1"), \
             patch.object(ba, "check_ba_report", return_value=("PENDING", None)), \
             patch.object(ba.time, "time", side_effect=[0.0, 100.0]):
            with self.assertRaises(TimeoutError):
                ba.run_ba_report("SOME_REPORT", date(2026, 7, 19), timeout_min=1)


class IterJsonArrayObjectsTests(unittest.TestCase):
    def test_skips_a_nested_array_and_yields_the_top_level_one(self) -> None:
        # marketplaceIds is a nested array at depth 2 -- the parser must not
        # mistake its '[' for the top-level data array's.
        doc = io.StringIO(
            '{"reportSpecification": {"marketplaceIds": ["A", "B"]}, '
            '"dataByAsin": [{"asin": "X"}, {"asin": "Y"}]}'
        )
        self.assertEqual(list(ba._iter_json_array_objects(doc)), [{"asin": "X"}, {"asin": "Y"}])

    def test_handles_records_split_across_read_chunks(self) -> None:
        doc = io.StringIO('{"data": [{"a": 1}, {"a": 2}, {"a": 3}]}')
        # A tiny chunk size forces many partial reads mid-record.
        self.assertEqual(
            list(ba._iter_json_array_objects(doc, chunk_chars=5)),
            [{"a": 1}, {"a": 2}, {"a": 3}],
        )

    def test_empty_array_yields_nothing(self) -> None:
        doc = io.StringIO('{"data": []}')
        self.assertEqual(list(ba._iter_json_array_objects(doc)), [])

    def test_no_array_at_all_yields_nothing(self) -> None:
        doc = io.StringIO('{"reportSpecification": {"marketplaceIds": ["A"]}}')
        self.assertEqual(list(ba._iter_json_array_objects(doc)), [])


class StreamBaRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._token_patch = patch.object(ba, "_access_token", return_value="tok")
        self._token_patch.start()
        self.addCleanup(self._token_patch.stop)

    def test_streams_a_plain_text_document(self) -> None:
        body = b'{"data": [{"asin": "X"}, {"asin": "Y"}]}'

        class _StreamResp:
            status_code = 200

            def json(self):
                return {"url": "https://example.com/doc.json"}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield body

        with patch.object(ba, "_host", return_value="https://host"), \
             patch.object(ba, "requests") as fake_requests:
            fake_requests.get.side_effect = [_StreamResp(), _StreamResp()]
            records = list(ba.stream_ba_records("doc1"))
        self.assertEqual(records, [{"asin": "X"}, {"asin": "Y"}])

    def test_streams_a_gzip_compressed_document(self) -> None:
        raw = b'{"data": [{"asin": "Z"}]}'
        compressed = gzip.compress(raw)

        class _MetaResp:
            def json(self):
                return {"url": "https://example.com/doc.json.gz"}

        class _StreamResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                # Split the gzip payload across two chunks to exercise the
                # buffered-reader chunk boundary.
                yield compressed[:len(compressed) // 2]
                yield compressed[len(compressed) // 2:]

        with patch.object(ba, "_host", return_value="https://host"), \
             patch.object(ba, "requests") as fake_requests:
            fake_requests.get.side_effect = [_MetaResp(), _StreamResp()]
            records = list(ba.stream_ba_records("doc1"))
        self.assertEqual(records, [{"asin": "Z"}])


class AwaitBaReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleep_patch = patch.object(ba.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_done_returns_document_id(self) -> None:
        with patch.object(ba, "check_ba_report", return_value=("DONE", "doc1")):
            self.assertEqual(ba.await_ba_report("r1"), "doc1")

    def test_cancelled_raises(self) -> None:
        with patch.object(ba, "check_ba_report", return_value=("CANCELLED", None)):
            with self.assertRaises(ba.BAReportCancelled):
                ba.await_ba_report("r1")

    def test_fatal_raises_with_reason(self) -> None:
        with patch.object(ba, "check_ba_report", return_value=("FATAL", "boom")):
            with self.assertRaises(ba.BAReportFatal) as cm:
                ba.await_ba_report("r1")
        self.assertIn("boom", str(cm.exception))

    def test_pending_forever_times_out(self) -> None:
        with patch.object(ba, "check_ba_report", return_value=("PENDING", None)), \
             patch.object(ba.time, "time", side_effect=[0.0, 100.0]):
            with self.assertRaises(TimeoutError):
                ba.await_ba_report("r1", timeout_min=1)


if __name__ == "__main__":
    unittest.main()
