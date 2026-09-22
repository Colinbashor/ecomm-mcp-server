"""Hermetic tests for amazon_listing_quality_sync.py -- no network, no real
warehouse.db.

Covers: schema creation, the missing-credentials guard (SPAPI_SELLER_ID has
no documented source, so it must fail loudly when unset), the
recently-sold-SKUs fallback, the non-defect-code (101265) exclusion from
issue_count/max_severity while it's still kept in the raw issues table, and
the delete-then-reinsert snapshot semantics for a SKU's issues.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import amazon_listing_quality_sync as alq


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_both_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        alq.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("amazon_listing_quality", tables)
        self.assertIn("amazon_listing_quality_issues", tables)
        conn.close()


class RequireEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                alq.require_env()
        self.assertIn("SPAPI_SELLER_ID", str(cm.exception))


class FallbackSkusTests(unittest.TestCase):
    def test_missing_table_returns_empty(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.assertEqual(alq.fallback_skus(conn), [])
        conn.close()

    def test_distinct_nonblank_skus_from_fulfilled_shipments(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE amazon_fulfilled_shipments (sku TEXT)")
        conn.executemany("INSERT INTO amazon_fulfilled_shipments VALUES (?)",
                          [("SKU1",), ("SKU1",), ("SKU2",), ("",), (None,)])
        self.assertEqual(sorted(alq.fallback_skus(conn)), ["SKU1", "SKU2"])
        conn.close()


def _listing(asin="B1", issues=None, discoverable=True, buyable=True):
    return 200, {
        "summaries": [{
            "asin": asin, "itemName": "Widget", "productType": "SHOES",
            "status": (["DISCOVERABLE"] if discoverable else []) + (["BUYABLE"] if buyable else []),
        }],
        "issues": issues or [],
    }


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_REGION"] = "NA"
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_SELLER_ID"] = "A1EXAMPLE"
        self.conn = sqlite3.connect(":memory:")
        alq.ensure_schema(self.conn)
        sleep_patch = patch.object(alq.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_clean_listing_has_no_severity_and_no_issue_rows(self) -> None:
        with patch.object(alq, "_get_listing", return_value=_listing()):
            requested, written, failed = alq.run(self.conn, ["SKU1"])
        self.assertEqual((requested, written, failed), (1, 1, []))
        row = self.conn.execute(
            "SELECT issue_count, max_severity FROM amazon_listing_quality WHERE seller_sku='SKU1'"
        ).fetchone()
        self.assertEqual(row, (0, None))

    def test_non_defect_code_excluded_from_count_but_kept_in_issues_table(self) -> None:
        issues = [
            {"code": "101265", "severity": "WARNING", "attributeNames": [], "categories": []},
            {"code": "88888", "severity": "ERROR", "attributeNames": ["title"], "categories": ["content"]},
        ]
        with patch.object(alq, "_get_listing", return_value=_listing(issues=issues)):
            alq.run(self.conn, ["SKU1"])
        row = self.conn.execute(
            "SELECT issue_count, max_severity FROM amazon_listing_quality WHERE seller_sku='SKU1'"
        ).fetchone()
        self.assertEqual(row, (1, "ERROR"))  # only the real defect counts
        codes = {r[0] for r in self.conn.execute(
            "SELECT code FROM amazon_listing_quality_issues WHERE seller_sku='SKU1'")}
        self.assertEqual(codes, {"101265", "88888"})  # both rows still present

    def test_reinsert_replaces_prior_issues_for_the_same_sku(self) -> None:
        first = [{"code": "A", "severity": "ERROR", "attributeNames": [], "categories": []},
                 {"code": "B", "severity": "WARNING", "attributeNames": [], "categories": []}]
        with patch.object(alq, "_get_listing", return_value=_listing(issues=first)):
            alq.run(self.conn, ["SKU1"])
        second = [{"code": "C", "severity": "WARNING", "attributeNames": [], "categories": []}]
        with patch.object(alq, "_get_listing", return_value=_listing(issues=second)):
            alq.run(self.conn, ["SKU1"])
        codes = {r[0] for r in self.conn.execute(
            "SELECT code FROM amazon_listing_quality_issues WHERE seller_sku='SKU1'")}
        self.assertEqual(codes, {"C"})  # A/B gone, not accumulated

    def test_404_is_reported_as_failed_and_not_written(self) -> None:
        with patch.object(alq, "_get_listing", return_value=(404, {})):
            requested, written, failed = alq.run(self.conn, ["GONE"])
        self.assertEqual((requested, written), (1, 0))
        self.assertEqual(failed[0][0], "GONE")

    def test_non_200_non_404_is_reported_as_failed(self) -> None:
        with patch.object(alq, "_get_listing",
                           return_value=(500, {"errors": [{"message": "boom"}]})):
            requested, written, failed = alq.run(self.conn, ["SKU1"])
        self.assertEqual(written, 0)
        self.assertIn("boom", failed[0][1])


if __name__ == "__main__":
    unittest.main()
