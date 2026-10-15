"""Tests for Shopify order-tag capture (shopify_order_tags + _coverage).

Why it exists: order tags are the merchant's own labels (gift, gift,
wholesale, test, resend ...), often the only way to segment or exclude orders.
Before this the connector read no tags at all.

The load-bearing properties:
  * REPLACE semantics. A tag removed in Shopify must disappear on the next fetch,
    which an INSERT OR REPLACE upsert alone would never do.
  * A measured zero is not a blank. An untagged order still gets a
    coverage row; an order whose tags were never read gets none.
  * Named columns. The write must survive a table whose physical column
    order differs from the DDL.

Hermetic: no network, and every DB test runs against a throwaway SQLite file.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from warehouse import db
from warehouse.connectors import shopify


class _TempDbCase(unittest.TestCase):
    """Points warehouse.db at a fresh file with the real schema applied."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "t.db"
        self._patch = patch.object(db, "DB_PATH", self.path)
        self._patch.start()
        self.pre_init()
        db.init_db()

    def pre_init(self) -> None:
        """Hook: create tables BEFORE the schema runs (see NamedColumnTests)."""

    def tearDown(self) -> None:
        self._patch.stop()
        self._dir.cleanup()

    def q(self, sql: str, *args) -> list[tuple]:
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def tags_of(self, order_id: str) -> list[str]:
        return [r[0] for r in self.q(
            "SELECT tag FROM shopify_order_tags WHERE order_id=? ORDER BY tag", order_id)]

    def coverage(self, order_id: str) -> tuple | None:
        rows = self.q("SELECT tag_count, order_date FROM shopify_order_tag_coverage "
                      "WHERE order_id=?", order_id)
        return rows[0] if rows else None


def _order(order_id: str, tags: list[str], day: str = "2026-10-13") -> dict:
    return {"order_id": order_id, "order_date": day, "tags": tags}


# --- parsing -----------------------------------------------------------------

class TagRowTests(unittest.TestCase):
    def test_list_is_kept_in_order_and_trimmed(self) -> None:
        r = shopify._tag_row("#101", "2026-10-13",
                             ["Wholesale", " vip ", "gift"])
        self.assertEqual(r, {"order_id": "#101", "order_date": "2026-10-13",
                             "tags": ["Wholesale", "vip", "gift"]})

    def test_untagged_order_is_an_empty_list_not_none(self) -> None:
        # Still a row: it becomes a tag_count=0 coverage row (a measured zero).
        self.assertEqual(shopify._tag_row("#101", "2026-10-13", [])["tags"], [])
        self.assertEqual(shopify._tag_row("#101", "2026-10-13", None)["tags"], [])

    def test_comma_joined_string_is_split_not_stored_whole(self) -> None:
        r = shopify._tag_row("#101", "2026-10-13", "gift, gift-note,resend")
        self.assertEqual(r["tags"], ["gift", "gift-note", "resend"])

    def test_blank_tags_are_dropped(self) -> None:
        r = shopify._tag_row("#101", "2026-10-13", ["a", "", "  ", "b"])
        self.assertEqual(r["tags"], ["a", "b"])

    def test_case_duplicates_collapse_to_first_spelling(self) -> None:
        # The column is COLLATE NOCASE, so these would collide on the PK anyway.
        r = shopify._tag_row("#101", "2026-10-13", ["Gift", "gift", "GIFT"])
        self.assertEqual(r["tags"], ["Gift"])


class QueryShapeTests(unittest.TestCase):
    def test_incremental_query_requests_tags_with_and_without_customer(self) -> None:
        # tags needs only read_orders, so it must NOT be gated on the customer scope.
        for include_customer in (False, True):
            doc = shopify._build_query(include_customer)
            self.assertRegex(doc, r"\btags\b")
            self.assertEqual(doc.count("{"), doc.count("}"))

    def test_no_custom_attributes_are_requested(self) -> None:
        # Deliberate: they carry visitor/session ids,
        # which the warehouse's no-PII rule keeps out. See schema.sql.
        self.assertNotIn("customAttributes", shopify._build_query(True))
        self.assertNotIn("note", shopify._build_query(True).split())


# --- the write -----------------------------------------------------------------

class ReplaceWriteTests(_TempDbCase):
    def test_tags_and_coverage_are_written(self) -> None:
        n = db.replace_shopify_order_tags([_order("#101", ["gift", "gift-note"])])
        self.assertEqual(n, 2)
        self.assertEqual(self.tags_of("#101"), ["gift", "gift-note"])
        self.assertEqual(self.coverage("#101"), (2, "2026-10-13"))

    def test_untagged_order_is_a_measured_zero(self) -> None:
        db.replace_shopify_order_tags([_order("#102", [])])
        self.assertEqual(self.tags_of("#102"), [])
        self.assertEqual(self.coverage("#102"), (0, "2026-10-13"))

    def test_never_read_order_has_no_coverage_row(self) -> None:
        db.replace_shopify_order_tags([_order("#101", ["x"])])
        self.assertIsNone(self.coverage("#never-synced"))

    def test_removed_tag_disappears_on_refetch(self) -> None:
        db.replace_shopify_order_tags([_order("#101", ["gift", "resend"])])
        db.replace_shopify_order_tags([_order("#101", ["gift"])])
        self.assertEqual(self.tags_of("#101"), ["gift"])
        self.assertEqual(self.coverage("#101")[0], 1)

    def test_all_tags_removed_leaves_a_zero_not_stale_rows(self) -> None:
        db.replace_shopify_order_tags([_order("#101", ["gift"])])
        db.replace_shopify_order_tags([_order("#101", [])])
        self.assertEqual(self.tags_of("#101"), [])
        self.assertEqual(self.coverage("#101")[0], 0)

    def test_replacing_one_order_leaves_others_alone(self) -> None:
        db.replace_shopify_order_tags([_order("#101", ["a"]), _order("#102", ["b"])])
        db.replace_shopify_order_tags([_order("#101", ["c"])])
        self.assertEqual(self.tags_of("#101"), ["c"])
        self.assertEqual(self.tags_of("#102"), ["b"])

    def test_tag_match_is_case_insensitive(self) -> None:
        db.replace_shopify_order_tags([_order("#101", ["Test-Order"])])
        rows = self.q("SELECT order_id FROM shopify_order_tags WHERE tag = 'test-order'")
        self.assertEqual(rows, [("#101",)])

    def test_shared_display_name_in_one_batch_unions_tags(self) -> None:
        # order_id is the display NAME, which is not guaranteed unique. The
        # second must not erase the first's tags, and coverage must match the rows.
        db.replace_shopify_order_tags([_order("#1001", ["a", "b"], "2022-06-01"),
                                       _order("#1001", ["B", "c"], "2023-01-01")])
        self.assertEqual(self.tags_of("#1001"), ["a", "b", "c"])
        self.assertEqual(self.coverage("#1001")[0], 3)

    def test_coverage_counts_agree_with_rows(self) -> None:
        db.replace_shopify_order_tags([_order("#101", ["a", "b"]), _order("#102", []),
                                       _order("#103", ["c"])])
        (cov,), = self.q("SELECT SUM(tag_count) FROM shopify_order_tag_coverage")
        (rows,), = self.q("SELECT COUNT(*) FROM shopify_order_tags")
        self.assertEqual(cov, rows)

    def test_empty_input_writes_nothing(self) -> None:
        self.assertEqual(db.replace_shopify_order_tags([]), 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM shopify_order_tag_coverage"), [(0,)])


class NamedColumnTests(_TempDbCase):
    """A table migrated with ALTER TABLE has its columns in a different
    PHYSICAL order than the DDL. A positional VALUES(...) would store every
    value in the wrong column without raising. Pre-create both tables with a
    scrambled order (the schema's CREATE IF NOT EXISTS then leaves them alone)
    and check every value still lands in its own column."""

    def pre_init(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE shopify_order_tags (
                synced_at TEXT NOT NULL, order_date TEXT,
                tag TEXT NOT NULL COLLATE NOCASE, order_id TEXT NOT NULL,
                PRIMARY KEY (order_id, tag));
            CREATE TABLE shopify_order_tag_coverage (
                synced_at TEXT NOT NULL, tag_count INTEGER NOT NULL,
                order_date TEXT, order_id TEXT PRIMARY KEY);
        """)
        conn.close()

    def test_values_land_in_their_named_columns(self) -> None:
        db.replace_shopify_order_tags([_order("#109", ["gift"], "2026-10-01")])
        self.assertEqual(
            self.q("SELECT order_id, tag, order_date FROM shopify_order_tags"),
            [("#109", "gift", "2026-10-01")])
        self.assertEqual(
            self.q("SELECT order_id, order_date, tag_count FROM shopify_order_tag_coverage"),
            [("#109", "2026-10-01", 1)])


# --- end to end through the two entry points --------------------------------------

def _gql_order(name: str, **extra) -> dict:
    node = {"name": name, "createdAt": "2026-10-13T12:00:00Z",
            "displayFinancialStatus": "PAID", "sourceName": "web",
            "lineItems": {"nodes": [{
                "sku": "S1", "title": "T", "quantity": 1,
                "discountedTotalSet": {"shopMoney": {"amount": "10.00", "currencyCode": "USD"}},
                "originalTotalSet": {"shopMoney": {"amount": "10.00"}},
                "discountAllocations": []}]}}
    node.update(extra)
    return node


class IncrementalSyncTests(_TempDbCase):
    def setUp(self) -> None:
        super().setUp()
        self._env = os.environ.get("SHOPIFY_CAPTURE_CUSTOMER")
        os.environ["SHOPIFY_CAPTURE_CUSTOMER"] = "0"   # no scope probe, no network
        self.docs: list[str] = []

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("SHOPIFY_CAPTURE_CUSTOMER", None)
        else:
            os.environ["SHOPIFY_CAPTURE_CUSTOMER"] = self._env
        super().tearDown()

    def _sync(self, nodes: list[dict]) -> list[dict]:
        def fake_post(variables, query=None):
            self.docs.append(query)
            return {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                               "nodes": nodes}}
        with patch.object(shopify, "_post", fake_post):
            return shopify.sync("2026-10-13", "2026-10-13")

    def test_sync_writes_tags_and_measured_zeros(self) -> None:
        rows = self._sync([
            _gql_order("#1002", tags=["Wholesale", "vip", "gift"]),
            _gql_order("#1003", tags=[]),
        ])
        self.assertEqual([r["order_id"] for r in rows], ["#1002", "#1003"])
        self.assertIn("tags", self.docs[0])
        self.assertEqual(self.tags_of("#1002"),
                         ["gift", "vip", "Wholesale"])
        self.assertEqual(self.coverage("#1002"), (3, "2026-10-13"))
        self.assertEqual(self.coverage("#1003"), (0, "2026-10-13"))

    def test_order_rows_are_unchanged_by_tags(self) -> None:
        # The task is to make the tag available, not to change revenue: the
        # `orders` rows must not carry anything tag-derived.
        rows = self._sync([_gql_order("#101", tags=["gift"])])
        self.assertEqual(len(rows), 1)
        self.assertNotIn("tags", rows[0])
        self.assertEqual(rows[0]["total"], 10.0)

    def test_payload_without_tags_key_records_nothing(self) -> None:
        # Absent = not read. Writing a tag_count=0 row would claim we looked.
        self._sync([_gql_order("#101")])
        self.assertIsNone(self.coverage("#101"))
