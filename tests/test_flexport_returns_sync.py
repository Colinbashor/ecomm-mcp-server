"""Hermetic tests for flexport_returns_sync.py (DTC/customer returns feed)."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import Mock, patch

import flexport_returns_sync as fr


def _resp(body, link: str | None = None, status: int = 200) -> Mock:
    m = Mock()
    m.status_code = status
    m.json.return_value = body
    m.headers = {"Link": link} if link else {}
    m.text = ""
    return m


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_tables_and_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        fr.ensure_schema(conn)
        fr.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("flexport_returns", tables)
        self.assertIn("flexport_return_lines", tables)
        self.assertIn("flexport_returns_sync_state", tables)


class CursorTests(unittest.TestCase):
    def test_extracts_next_page_info(self) -> None:
        resp = _resp([], link='<https://api.example.com/returns?page_info=42>; rel="next"')
        self.assertEqual(fr.next_page_info(resp), "42")

    def test_none_when_no_next_relation(self) -> None:
        resp = _resp([], link='<https://api.example.com/returns?page_info=42>; rel="prev"')
        self.assertIsNone(fr.next_page_info(resp))


class RowParsingTests(unittest.TestCase):
    def test_rows_for_return_keeps_first_inspection_disposition(self) -> None:
        rec = {
            "id": 7, "status": "INSPECTED", "rma": "RMA-1", "externalReturnId": "EXT-7",
            "fulfillmentOrderId": 99,
            "shippingLabel": {"carrier": "UPS", "trackingCode": "TRK", "trackingStatus": "DELIVERED"},
            "sourceAddress": {"name": "Jane", "city": "Portland", "state": "OR",
                              "zip": "97201", "country": "US"},
            "shippedAt": "2024-01-01", "receivedAt": "2024-01-05", "inspectedAt": "2024-01-06",
            "returnItems": [
                {"identifier": "SKU-1", "expectedQuantity": 2,
                 "inspectedItems": [
                     {"receivedCondition": "GOOD", "finalCondition": "SELLABLE", "disposition": "RESTOCK"},
                     {"receivedCondition": "DAMAGED", "finalCondition": "SCRAP", "disposition": "SCRAP"},
                 ]},
                {"identifier": "SKU-2", "expectedQuantity": 1, "inspectedItems": []},
            ],
        }
        ret_row, line_rows = fr.rows_for_return(rec, "stamp")
        self.assertEqual(ret_row[0], 7)
        self.assertEqual(ret_row[16], 2)        # n_lines
        self.assertEqual(ret_row[17], "stamp")  # synced_at
        self.assertEqual(len(line_rows), 2)
        # first line: two inspected items, but disposition is the FIRST one's
        self.assertEqual(line_rows[0][2], "SKU-1")
        self.assertEqual(line_rows[0][4], 2)             # n_inspected
        self.assertEqual(line_rows[0][7], "RESTOCK")      # first inspection's disposition
        # second line: no inspections at all yet
        self.assertEqual(line_rows[1][4], 0)
        self.assertIsNone(line_rows[1][7])


class PaginationTests(unittest.TestCase):
    def test_walks_pages_until_no_next_cursor(self) -> None:
        responses = [
            _resp([{"id": 1}], link='<x?page_info=2>; rel="next"'),
            _resp([{"id": 2}]),  # tip
        ]
        with patch.object(fr, "_request", side_effect=responses), patch.object(fr.time, "sleep"):
            pages = list(fr.iter_return_pages(None, max_pages=10))
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0], ([{"id": 1}], "2"))
        self.assertEqual(pages[1], ([{"id": 2}], None))

    def test_stops_on_empty_page(self) -> None:
        with patch.object(fr, "_request", return_value=_resp([])), patch.object(fr.time, "sleep"):
            self.assertEqual(list(fr.iter_return_pages(None, 10)), [])


class RunTests(unittest.TestCase):
    def test_run_writes_returns_and_lines_and_saves_cursor(self) -> None:
        conn = sqlite3.connect(":memory:")
        fr.ensure_schema(conn)
        batch = [{
            "id": 1, "status": "RECEIVED", "returnItems": [
                {"identifier": "SKU-1", "expectedQuantity": 1},
            ],
        }]
        with patch.object(fr, "iter_return_pages", return_value=iter([(batch, "NEXT-CURSOR")])):
            n_returns, n_lines, pages = fr.run(conn, restart=False, max_pages=10)

        self.assertEqual(n_returns, 1)
        self.assertEqual(n_lines, 1)
        self.assertEqual(pages, 1)
        cursor = conn.execute(
            "SELECT value FROM flexport_returns_sync_state WHERE key='returns_page_info'").fetchone()
        self.assertEqual(cursor[0], "NEXT-CURSOR")

    def test_restart_clears_stored_cursor_before_crawling(self) -> None:
        conn = sqlite3.connect(":memory:")
        fr.ensure_schema(conn)
        fr._save_cursor(conn, "STALE")
        with patch.object(fr, "iter_return_pages", return_value=iter([])) as mocked:
            fr.run(conn, restart=True, max_pages=10)
        # restart must have passed None as the starting cursor, not the stale one
        mocked.assert_called_once_with(None, 10)
        self.assertIsNone(fr._load_cursor(conn))

    def test_malformed_record_without_id_is_skipped(self) -> None:
        conn = sqlite3.connect(":memory:")
        fr.ensure_schema(conn)
        batch = ["not-a-dict", {"status": "no id field"}, {"id": 5, "returnItems": []}]
        with patch.object(fr, "iter_return_pages", return_value=iter([(batch, None)])):
            n_returns, n_lines, pages = fr.run(conn, restart=False, max_pages=10)
        self.assertEqual(n_returns, 1)  # only the well-formed record counted


if __name__ == "__main__":
    unittest.main()
