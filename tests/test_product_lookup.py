"""Hermetic tests for server.product_lookup over a temp warehouse built by
catalog_sync.build_xref -- no network."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog_sync
import server
from warehouse import db


class ProductLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.dict(os.environ, {})
        p.start()
        self.addCleanup(p.stop)
        for k in ("CATALOG_SKU_STRIP_SUFFIXES", "CATALOG_TITLE_MARKERS",
                  "CATALOG_VARIANT_DELIMITER"):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        orig = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "warehouse.db"
        self.addCleanup(setattr, db, "DB_PATH", orig)

        conn = sqlite3.connect(db.DB_PATH)
        catalog_sync.ensure_schema(conn)
        conn.executemany(
            "INSERT INTO catalog_shopify (variant_id, product_id, sku, title, variant_title, "
            "status, price, unit_cost, synced_at) VALUES (?,?,?,?,?,?,?,?, 'now')", [
                ("2000000000001", "1000000000001", "SKU-1001", "Hearts Mini Dress - Black",
                 "S", "ACTIVE", 40.0, 12.5),
                ("2000000000002", "1000000000001", "SKU-1002", "Hearts Mini Dress - Black",
                 "M", "ACTIVE", 40.0, 12.5),
                # archived duplicate of SKU-1001 under another product id
                ("2000000000099", "1000000000099", "SKU-1001", "Hearts Mini Dress (old)",
                 "S", "ARCHIVED", 55.0, 20.0),
                ("2000000000003", "1000000000003", "SKU-50%_OFF", "Plain Tee",
                 "S", "ACTIVE", 10.0, 3.0),
            ])
        conn.executemany(
            "INSERT INTO catalog_tiktok (product_id, sku_id, seller_sku, title, synced_at) "
            "VALUES (?,?,?,?, 'now')", [
                ("3000000000001", "91", "SKU-1001", "Hearts Mini Dress - Black"),
                ("3000000000001", "92", "SKU-1002", "Hearts Mini Dress - Black"),
                ("3000000000007", "93", "SKU-7007", "Hearts Mini Dress - Red"),
            ])
        conn.execute(
            "INSERT INTO catalog_amazon (seller_sku, asin, title, synced_at) VALUES "
            "('sku-1001-fba', 'B000000001', 'Hearts Mini Dress Black Party Dress', 'now')")
        conn.commit()
        catalog_sync.build_xref(conn, "now")
        conn.close()

    def _lookup(self, ident: str) -> dict:
        out = server.product_lookup(ident)
        return json.loads(out)

    def test_asin_resolves_to_shopify_and_tiktok_ids(self) -> None:
        out = self._lookup("B000000001")
        self.assertEqual(out["sku_keys"], ["SKU-1001"])
        self.assertEqual([r["channel_id"] for r in out["by_channel"]["tiktok"]],
                         ["3000000000001"])
        self.assertEqual([r["channel_id"] for r in out["by_channel"]["shopify"]],
                         ["2000000000001"])
        self.assertEqual([v["product_id"] for v in out["shopify_variants"]], ["1000000000001"])

    def test_archived_duplicate_is_excluded_from_shopify_variants(self) -> None:
        out = self._lookup("SKU-1001")
        self.assertEqual([v["variant_id"] for v in out["shopify_variants"]], ["2000000000001"])
        self.assertTrue(all(v["status"] != "ARCHIVED" for v in out["shopify_variants"]))

    def test_sku_is_normalized_before_matching(self) -> None:
        out = self._lookup("sku-1001-FBA")
        self.assertEqual(out["sku_keys"], ["SKU-1001"])
        self.assertIn("amazon", out["by_channel"])

    def test_shopify_product_id_expands_to_its_variants(self) -> None:
        out = self._lookup("1000000000001")
        self.assertEqual(out["sku_keys"], ["SKU-1001", "SKU-1002"])
        self.assertEqual(sorted(v["variant_id"] for v in out["shopify_variants"]),
                         ["2000000000001", "2000000000002"])

    def test_archived_product_id_resolves_to_the_live_product(self) -> None:
        out = self._lookup("1000000000099")
        self.assertEqual(out["sku_keys"], ["SKU-1001"])
        self.assertEqual([v["variant_id"] for v in out["shopify_variants"]], ["2000000000001"])

    def test_tiktok_product_id_returns_every_sku_and_the_other_channels(self) -> None:
        out = self._lookup("3000000000001")
        self.assertEqual(out["sku_keys"], ["SKU-1001", "SKU-1002"])
        self.assertEqual(len(out["matched"]), 2)
        self.assertIn("amazon", out["by_channel"])

    def test_same_style_rows_are_flagged_fuzzy_not_merged(self) -> None:
        out = self._lookup("3000000000001")
        fuzzy_ids = [r["channel_id"] for r in out["same_style_fuzzy"]]
        self.assertIn("3000000000007", fuzzy_ids)          # same style, other colour/sku
        self.assertNotIn("3000000000007",
                         [r["channel_id"] for r in out["by_channel"]["tiktok"]])

    def test_no_match_falls_back_to_title_substring(self) -> None:
        out = self._lookup("plain te")
        self.assertEqual(out["matched"], [])
        self.assertEqual([c["channel_id"] for c in out["candidates"]], ["2000000000003"])

    def test_like_wildcards_in_the_identifier_are_literal(self) -> None:
        self.assertIn("No product_xref row matches", server.product_lookup("%%%"))

    def test_unknown_and_empty_identifiers(self) -> None:
        self.assertIn("No product_xref row matches", server.product_lookup("ZZZZ-NOPE"))
        self.assertTrue(server.product_lookup("  ").startswith("Error"))

    def test_missing_xref_table_gives_a_build_hint(self) -> None:
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DROP TABLE product_xref")
        conn.commit()
        conn.close()
        self.assertIn("catalog_sync.py", server.product_lookup("SKU-1001"))

    def test_registered_read_only(self) -> None:
        names = [f.__name__ for f, _ in server._TOOLS]
        self.assertIn("product_lookup", names)


if __name__ == "__main__":
    unittest.main()
