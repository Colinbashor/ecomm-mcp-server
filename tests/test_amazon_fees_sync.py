"""Hermetic tests for amazon_fees_sync.py — no network, no real DB file.

Covers: schema creation, the header-driven TSV parser (`_rows`, including BOM
and quoted headers), the money/int coercion helpers ("--" and thousands
separators), each report's row-shaping parser, the MCF composite
merchant-order-id split, and the CANCELLED-window / per-report-failure
handling in `sync()`.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import amazon_fees_sync as fees


def _tsv(header: list[str], *data_rows: list[str]) -> str:
    lines = ["\t".join(header)]
    lines += ["\t".join(r) for r in data_rows]
    return "\n".join(lines) + "\n"


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_five_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        fees.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("amazon_fee_preview", "amazon_fba_storage_fees",
                  "amazon_fba_reimbursements", "amazon_fba_promotions",
                  "amazon_fulfilled_shipments"):
            self.assertIn(t, tables)


class RowsParserTests(unittest.TestCase):
    def test_bom_and_quoted_header_normalized(self) -> None:
        text = "﻿\"SKU\"\t\"ASIN\"\nabc\tB000001\n"
        rows = list(fees._rows(text))
        self.assertEqual(rows, [{"sku": "abc", "asin": "B000001"}])

    def test_blank_rows_skipped(self) -> None:
        text = "sku\tasin\nabc\tB1\n\t\n"
        rows = list(fees._rows(text))
        self.assertEqual(len(rows), 1)

    def test_empty_text_yields_nothing(self) -> None:
        self.assertEqual(list(fees._rows("")), [])


class NumericHelperTests(unittest.TestCase):
    def test_double_dash_is_zero(self) -> None:
        self.assertEqual(fees._num("--"), 0.0)

    def test_currency_and_thousands_separators_stripped(self) -> None:
        self.assertEqual(fees._num("$1,234.56"), 1234.56)

    def test_none_and_empty_are_zero(self) -> None:
        self.assertEqual(fees._num(None), 0.0)
        self.assertEqual(fees._num(""), 0.0)

    def test_garbage_string_is_zero_not_a_crash(self) -> None:
        self.assertEqual(fees._num("N/A"), 0.0)

    def test_int_rounds(self) -> None:
        self.assertEqual(fees._int("3.7"), 4)


class ParseFeePreviewTests(unittest.TestCase):
    def test_row_shape(self) -> None:
        text = _tsv(
            ["sku", "fnsku", "asin", "product-name", "your-price", "sales-price",
             "estimated-fee-total", "estimated-referral-fee-per-unit",
             "expected-fulfillment-fee-per-unit", "currency"],
            ["SKU1", "FN1", "B1", "Widget", "19.99", "19.99", "5.25", "3.00", "2.25", "USD"],
        )
        conn = sqlite3.connect(":memory:")
        fees.ensure_schema(conn)
        n = fees.parse_fee_preview(conn, text, "stamp1", snapshot_date="2026-08-01")
        self.assertEqual(n, 1)
        row = conn.execute("SELECT sku, estimated_fee_total FROM amazon_fee_preview").fetchone()
        self.assertEqual(row, ("SKU1", 5.25))

    def test_rows_without_sku_are_skipped(self) -> None:
        text = _tsv(["sku", "asin"], ["", "B1"])
        conn = sqlite3.connect(":memory:")
        fees.ensure_schema(conn)
        n = fees.parse_fee_preview(conn, text, "s", snapshot_date="2026-08-01")
        self.assertEqual(n, 0)


class ParseShipmentsTests(unittest.TestCase):
    def test_mcf_order_name_parsed_from_composite_id(self) -> None:
        self.assertEqual(fees._mcf_order_name("Shopify DK1001 8486373196033"), "DK1001")

    def test_non_mcf_id_returns_none(self) -> None:
        self.assertIsNone(fees._mcf_order_name("112-1234567-1234567"))

    def test_shipment_row_written_with_named_columns(self) -> None:
        text = _tsv(
            ["shipment-item-id", "shipment-date", "purchase-date", "amazon-order-id",
             "shipment-id", "sku", "quantity-shipped", "item-price",
             "item-promotion-discount", "ship-promotion-discount", "currency",
             "sales-channel", "merchant-order-id"],
            ["SII1", "2026-08-01", "2026-07-30", "112-1", "SHIP1", "SKU1", "2",
             "39.98", "0", "0", "USD", "Non-Amazon", "Shopify DK1001 8486373196033"],
        )
        conn = sqlite3.connect(":memory:")
        fees.ensure_schema(conn)
        n = fees.parse_shipments(conn, text, "stamp", snapshot_date="2026-08-01")
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT sales_channel, shopify_order_name, quantity FROM amazon_fulfilled_shipments"
        ).fetchone()
        self.assertEqual(row, ("Non-Amazon", "DK1001", 2))


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"

    def test_cancelled_window_is_not_an_error(self) -> None:
        conn = sqlite3.connect(":memory:")
        fees.ensure_schema(conn)
        with patch.object(fees, "_create_and_download", return_value=None):
            total, errors = fees.sync(conn, date(2026, 8, 3), ["fee_preview"])
        self.assertEqual(total, 0)
        self.assertEqual(errors, [])

    def test_one_failing_report_does_not_kill_the_others(self) -> None:
        conn = sqlite3.connect(":memory:")
        fees.ensure_schema(conn)
        good_text = _tsv(["sku"], ["SKU1"])

        def fake(host, report_type, start, end):
            if report_type == fees.REPORTS["storage"][0]:
                raise RuntimeError("boom")
            return good_text

        with patch.object(fees, "_create_and_download", side_effect=fake):
            total, errors = fees.sync(conn, date(2026, 8, 3), ["fee_preview", "storage"])
        self.assertEqual(total, 1)  # fee_preview still wrote its row
        self.assertEqual(len(errors), 1)
        self.assertIn("storage", errors[0])

    def test_require_env_reports_missing_vars(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in fees.REQUIRED_ENV}
        try:
            with self.assertRaises(SystemExit) as cm:
                fees.require_env()
            self.assertIn("SPAPI_REFRESH_TOKEN", str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
