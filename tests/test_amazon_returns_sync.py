"""Hermetic tests for amazon_returns_sync.py — no network, no real DB file.

Covers: schema creation, return-row shaping, the calendar-month window
splitter, and CANCELLED-window / per-window-failure handling in `sync()`
(a CANCELLED window means "no data past retention" and must not be treated
as an error).
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import amazon_returns_sync as returns


def _tsv(header: list[str], *data_rows: list[str]) -> str:
    lines = ["\t".join(header)]
    lines += ["\t".join(r) for r in data_rows]
    return "\n".join(lines) + "\n"


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        returns.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_returns)")}
        self.assertIn("disposition", cols)
        self.assertIn("license_plate_number", cols)


class ParseReturnsTests(unittest.TestCase):
    def test_row_shape(self) -> None:
        text = _tsv(
            ["return-date", "order-id", "sku", "asin", "fnsku", "product-name",
             "quantity", "fulfillment-center-id", "detailed-disposition", "reason",
             "status", "license-plate-number", "customer-comments"],
            ["2026-08-01", "112-1", "SKU1", "B1", "FN1", "Widget", "1",
             "FC1", "SELLABLE", "NOT_AS_DESCRIBED", "Processed", "LP1", "too small"],
        )
        conn = sqlite3.connect(":memory:")
        returns.ensure_schema(conn)
        n = returns.parse_returns(conn, text, "stamp")
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT order_id, sku, disposition, reason FROM amazon_returns"
        ).fetchone()
        self.assertEqual(row, ("112-1", "SKU1", "SELLABLE", "NOT_AS_DESCRIBED"))

    def test_missing_license_plate_defaults_to_empty_string(self) -> None:
        text = _tsv(["order-id", "sku"], ["112-1", "SKU1"])
        conn = sqlite3.connect(":memory:")
        returns.ensure_schema(conn)
        returns.parse_returns(conn, text, "stamp")
        row = conn.execute("SELECT license_plate_number FROM amazon_returns").fetchone()
        self.assertEqual(row[0], "")

    def test_rows_missing_order_id_or_sku_are_skipped(self) -> None:
        text = _tsv(["order-id", "sku"], ["", "SKU1"], ["112-1", ""])
        conn = sqlite3.connect(":memory:")
        returns.ensure_schema(conn)
        n = returns.parse_returns(conn, text, "stamp")
        self.assertEqual(n, 0)


class MonthWindowsTests(unittest.TestCase):
    def test_single_month_span(self) -> None:
        windows = list(returns._month_windows(date(2026, 8, 1), date(2026, 8, 31)))
        self.assertEqual(windows, [("2026-08-01", "2026-08-31")])

    def test_spans_multiple_months_clamped_to_range(self) -> None:
        windows = list(returns._month_windows(date(2026, 7, 15), date(2026, 9, 10)))
        self.assertEqual(windows, [
            ("2026-07-15", "2026-07-31"),
            ("2026-08-01", "2026-08-31"),
            ("2026-09-01", "2026-09-10"),
        ])

    def test_year_boundary_crossed(self) -> None:
        windows = list(returns._month_windows(date(2025, 12, 20), date(2026, 1, 5)))
        self.assertEqual(windows, [
            ("2025-12-20", "2025-12-31"),
            ("2026-01-01", "2026-01-05"),
        ])


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"

    def test_cancelled_window_counts_as_empty_not_error(self) -> None:
        conn = sqlite3.connect(":memory:")
        returns.ensure_schema(conn)
        with patch.object(returns, "_create_and_download", return_value=None):
            total, empty, errors = returns.sync(conn, date(2026, 8, 1), date(2026, 8, 31))
        self.assertEqual(total, 0)
        self.assertEqual(empty, 1)
        self.assertEqual(errors, [])

    def test_one_window_failure_does_not_stop_the_others(self) -> None:
        conn = sqlite3.connect(":memory:")
        returns.ensure_schema(conn)
        good_text = _tsv(["order-id", "sku"], ["112-1", "SKU1"])
        calls = []

        def fake(host, report_type, ws, we):
            calls.append(ws)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return good_text

        with patch.object(returns, "_create_and_download", side_effect=fake):
            total, empty, errors = returns.sync(conn, date(2026, 7, 15), date(2026, 8, 15))
        self.assertEqual(total, 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
