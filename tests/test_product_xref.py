"""Hermetic tests for product_xref assembly (catalog_sync.build_xref) and the
shared key normalization in warehouse/product_keys.py."""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import catalog_sync
from warehouse.product_keys import norm_sku, norm_style


def _conn(with_sources: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    catalog_sync.ensure_schema(conn)
    if with_sources:
        conn.executescript("""
            CREATE TABLE ga_products (property_id TEXT, date TEXT, item_id TEXT, item_name TEXT);
            CREATE TABLE orders (platform TEXT, order_id TEXT, sku TEXT, product_name TEXT);
        """)
    conn.execute(
        "INSERT INTO catalog_shopify (variant_id, product_id, sku, title, variant_title, "
        "status, synced_at) VALUES "
        "('2000000000001', '1000000000001', 'SKU-1001', 'Exact Dress - Black', 'S', "
        "'ACTIVE', 'now')")
    conn.commit()
    return conn


def _xref(conn, where: str = "1=1", params=()):
    return conn.execute(
        f"SELECT channel, channel_id, channel_sku, sku_key, match_key FROM product_xref "
        f"WHERE {where} ORDER BY channel, channel_id, channel_sku", params).fetchall()


class KeyNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        knobs = ("CATALOG_SKU_STRIP_SUFFIXES", "CATALOG_TITLE_MARKERS",
                 "CATALOG_VARIANT_DELIMITER")
        p = patch.dict(os.environ, {})
        p.start()
        self.addCleanup(p.stop)
        for k in knobs:
            os.environ.pop(k, None)

    def test_norm_sku_default_strips_fba_and_uppercases(self) -> None:
        self.assertEqual(norm_sku(" sku-1001-fba "), "SKU-1001")
        self.assertEqual(norm_sku("SKU-1001-UPC"), "SKU-1001-UPC")   # not configured
        self.assertEqual(norm_sku(None), "")

    def test_norm_sku_suffixes_are_configurable(self) -> None:
        os.environ["CATALOG_SKU_STRIP_SUFFIXES"] = "FBA, UPC"
        self.assertEqual(norm_sku("SKU-1001-UPC"), "SKU-1001")
        os.environ["CATALOG_SKU_STRIP_SUFFIXES"] = ""
        self.assertEqual(norm_sku("SKU-1001-FBA"), "SKU-1001-FBA")

    def test_norm_style_drops_variant_and_punctuation(self) -> None:
        self.assertEqual(norm_style("Hearts+Stars Mini Dress - Black"), "hearts stars mini dress")
        self.assertEqual(norm_style(None), "")

    def test_markers_strip_before_the_variant_split(self) -> None:
        os.environ["CATALOG_TITLE_MARKERS"] = "Outlet,Clearance"
        self.assertEqual(norm_style("Tote Bag (Outlet)"), "tote bag")
        self.assertEqual(norm_style("Halter Top - White (Outlet)"), "halter top")
        os.environ["CATALOG_VARIANT_DELIMITER"] = " / "
        self.assertEqual(norm_style("Halter Top / White"), "halter top")


class ProductXrefTests(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.dict(os.environ, {})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CATALOG_SKU_STRIP_SUFFIXES", None)
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_shopify_variants_are_sighted_by_variant_id(self) -> None:
        catalog_sync.build_xref(self.conn, "now")
        self.assertEqual(_xref(self.conn, "channel='shopify'"),
                         [("shopify", "2000000000001", "SKU-1001", "SKU-1001", "exact dress")])

    def test_archived_shopify_duplicates_are_left_out(self) -> None:
        """A SKU is not unique in catalog_shopify; an archived duplicate must not
        sit beside the live product under the same sku_key."""
        self.conn.execute(
            "INSERT INTO catalog_shopify (variant_id, product_id, sku, title, status, synced_at) "
            "VALUES ('2000000000099', '1000000000099', 'SKU-1001', 'Exact Dress (old)', "
            "'ARCHIVED', 'now')")
        catalog_sync.build_xref(self.conn, "now")
        self.assertEqual([r[1] for r in _xref(self.conn, "sku_key='SKU-1001'")],
                         ["2000000000001"])

    def test_ga4_sku_item_id_keeps_exact_sku(self) -> None:
        self.conn.execute("INSERT INTO ga_products VALUES ('p', 'd', 'SKU-1001', 'Exact Dress')")
        catalog_sync.build_xref(self.conn, "now")
        self.assertEqual(_xref(self.conn, "channel='ga4'"),
                         [("ga4", "SKU-1001", "SKU-1001", "SKU-1001", "exact dress")])

    def test_ga4_product_id_does_not_invent_variant_sku(self) -> None:
        self.conn.execute(
            "INSERT INTO ga_products VALUES ('p', 'd', '1000000000001', 'Exact Dress')")
        catalog_sync.build_xref(self.conn, "now")
        self.assertEqual(_xref(self.conn, "channel='ga4'"),
                         [("ga4", "1000000000001", "", "", "exact dress")])

    def test_tiktok_catalog_and_order_skus(self) -> None:
        self.conn.execute(
            "INSERT INTO catalog_tiktok (product_id, sku_id, seller_sku, title, synced_at) "
            "VALUES ('3000000000001', '9', 'SKU-1001', 'Exact Dress - Black', 'now')")
        self.conn.execute("INSERT INTO orders VALUES ('tiktok', 'O1', 'SKU-2002', 'Delisted Top')")
        self.conn.execute("INSERT INTO orders VALUES ('shopify', 'O2', 'SKU-3003', 'Ignored')")
        catalog_sync.build_xref(self.conn, "now")
        self.assertEqual(_xref(self.conn, "channel LIKE 'tiktok%'"), [
            ("tiktok", "3000000000001", "SKU-1001", "SKU-1001", "exact dress"),
            ("tiktok_order", "", "SKU-2002", "SKU-2002", "delisted top"),
        ])

    def test_amazon_msku_suffix_joins_the_base_sku(self) -> None:
        self.conn.execute(
            "INSERT INTO catalog_amazon (seller_sku, asin, title, synced_at) "
            "VALUES ('SKU-1001-FBA', 'B000000001', 'Exact Dress Black Womens Party', 'now')")
        catalog_sync.build_xref(self.conn, "now")
        self.assertEqual(_xref(self.conn, "channel='amazon'")[0][:4],
                         ("amazon", "B000000001", "SKU-1001-FBA", "SKU-1001"))

    def test_missing_source_tables_are_tolerated(self) -> None:
        conn = _conn(with_sources=False)   # no ga_products, no orders
        self.assertEqual(catalog_sync.build_xref(conn, "now"), 1)
        conn.close()

    def test_sightings_with_neither_key_nor_sku_are_dropped(self) -> None:
        self.conn.execute("INSERT INTO ga_products VALUES ('p', 'd', 'x', '')")
        catalog_sync.build_xref(self.conn, "now")
        self.assertEqual(_xref(self.conn, "channel='ga4'"), [])


if __name__ == "__main__":
    unittest.main()
