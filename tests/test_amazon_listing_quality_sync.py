"""Hermetic tests for amazon_listing_quality_sync.py -- no network, no real
warehouse.db.

Covers: schema creation (plus the ALTER-TABLE migration for a pre-existing
table missing the newer columns), the missing-credentials guard
(SPAPI_SELLER_ID has no documented source, so it must fail loudly when
unset), the recently-sold-SKUs fallback, the non-defect-code (101265)
exclusion from issue_count/max_severity while it's still kept in the raw
issues table, the delete-then-reinsert snapshot semantics for a SKU's
issues, the generic_keyword/item_type_keyword attribute capture, and
--resume's already-synced-SKU filtering.
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

    def test_ensure_schema_migrates_a_table_missing_the_newer_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE amazon_listing_quality (
                seller_sku TEXT PRIMARY KEY, asin TEXT, item_name TEXT,
                product_type TEXT, is_discoverable INTEGER, is_buyable INTEGER,
                issue_count INTEGER, max_severity TEXT, synced_at TEXT NOT NULL
            );
        """)
        alq.ensure_schema(conn)  # must not raise on the already-existing table
        cols = {c[1] for c in conn.execute("PRAGMA table_info(amazon_listing_quality)")}
        self.assertIn("generic_keyword", cols)
        self.assertIn("item_type_keyword", cols)
        conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        alq.ensure_schema(conn)
        alq.ensure_schema(conn)  # must not raise (duplicate ALTER TABLE)
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


def _listing(asin="B1", issues=None, discoverable=True, buyable=True, attributes=None):
    return 200, {
        "summaries": [{
            "asin": asin, "itemName": "Widget", "productType": "SHOES",
            "status": (["DISCOVERABLE"] if discoverable else []) + (["BUYABLE"] if buyable else []),
        }],
        "issues": issues or [],
        "attributes": attributes or {},
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

    def test_generic_keyword_and_item_type_keyword_captured(self) -> None:
        attrs = {
            "generic_keyword": [{"value": "chunky boots edgy footwear"}],
            "item_type_keyword": [{"value": "boots"}],
        }
        with patch.object(alq, "_get_listing", return_value=_listing(attributes=attrs)):
            alq.run(self.conn, ["SKU1"])
        row = self.conn.execute(
            "SELECT generic_keyword, item_type_keyword FROM amazon_listing_quality "
            "WHERE seller_sku='SKU1'"
        ).fetchone()
        self.assertEqual(row, ("chunky boots edgy footwear", "boots"))

    def test_missing_attributes_leave_keyword_columns_null(self) -> None:
        with patch.object(alq, "_get_listing", return_value=_listing()):
            alq.run(self.conn, ["SKU1"])
        row = self.conn.execute(
            "SELECT generic_keyword, item_type_keyword FROM amazon_listing_quality "
            "WHERE seller_sku='SKU1'"
        ).fetchone()
        self.assertEqual(row, (None, None))

    def test_resume_skips_already_synced_skus(self) -> None:
        with patch.object(alq, "_get_listing", return_value=_listing()) as get_listing:
            alq.run(self.conn, ["SKU1"])
            get_listing.reset_mock()
            requested, written, failed = alq.run(self.conn, ["SKU1", "SKU2"], resume=True)
        self.assertEqual(requested, 1)  # SKU1 already present, only SKU2 attempted
        get_listing.assert_called_once()
        self.assertEqual(get_listing.call_args.args[2], "SKU2")  # (host, seller_id, sku, marketplace_id)

    def test_resume_false_by_default_reprocesses_everything(self) -> None:
        with patch.object(alq, "_get_listing", return_value=_listing()) as get_listing:
            alq.run(self.conn, ["SKU1"])
            get_listing.reset_mock()
            requested, written, failed = alq.run(self.conn, ["SKU1"])
        self.assertEqual(requested, 1)
        get_listing.assert_called_once()  # re-diagnosed, not skipped


if __name__ == "__main__":
    unittest.main()
