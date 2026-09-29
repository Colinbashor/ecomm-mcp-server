"""Hermetic tests for warehouse/connectors/tiktok_shop.py -- no network.

Pins the per-sku aggregation of TikTok's per-UNIT line items, in particular
that the seller-funded and platform-funded discounts are summed alongside
quantity/total/original_total, and that db.upsert_orders() stores them while
leaving them NULL (not captured) for connectors that never set them.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from warehouse import db
from warehouse.connectors import tiktok_shop


def _unit(sku, sale, original, seller=0.0, platform=0.0):
    return {"seller_sku": sku, "product_name": f"Product {sku}", "currency": "USD",
            "sale_price": str(sale), "original_price": str(original),
            "seller_discount": str(seller), "platform_discount": str(platform)}


def _page(orders):
    return {"orders": orders, "next_page_token": ""}


class DiscountAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"TIKTOK_SHOP_CIPHER": "cipher"})
        env.start()
        self.addCleanup(env.stop)

    def _sync(self, orders):
        with patch.object(tiktok_shop, "_fetch_page", return_value=_page(orders)):
            return tiktok_shop.sync("2026-01-01", "2026-01-02")

    def test_multi_unit_same_sku_sums_both_discounts(self) -> None:
        rows = self._sync([{
            "id": "O1", "create_time": 1767225600, "status": "DELIVERED",
            "line_items": [_unit("A", 30.0, 50.0, seller=15.0, platform=5.0),
                           _unit("A", 30.0, 50.0, seller=15.0, platform=5.0)],
        }])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["quantity"], 2)
        self.assertEqual(r["total"], 60.0)
        self.assertEqual(r["original_total"], 100.0)
        self.assertEqual(r["seller_discount"], 30.0)
        self.assertEqual(r["platform_discount"], 10.0)
        # the identity the columns exist to preserve
        self.assertAlmostEqual(r["original_total"] - r["seller_discount"] - r["platform_discount"],
                               r["total"])

    def test_distinct_skus_keep_their_own_discounts(self) -> None:
        rows = self._sync([{
            "id": "O2", "create_time": 1767225600, "status": "DELIVERED",
            "line_items": [_unit("A", 20.0, 25.0, platform=5.0), _unit("B", 10.0, 10.0)],
        }])
        by_sku = {r["sku"]: r for r in rows}
        self.assertEqual(by_sku["A"]["platform_discount"], 5.0)
        self.assertEqual(by_sku["B"]["platform_discount"], 0.0)

    def test_missing_discount_fields_read_as_zero(self) -> None:
        item = _unit("C", 10.0, 10.0)
        del item["seller_discount"], item["platform_discount"]
        rows = self._sync([{"id": "O3", "create_time": 1767225600, "status": "DELIVERED",
                            "line_items": [item]}])
        self.assertEqual(rows[0]["seller_discount"], 0.0)
        self.assertEqual(rows[0]["platform_discount"], 0.0)


class UpsertOrdersDiscountColumnsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "warehouse.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig

    def _base(self, **kw):
        return {"platform": "tiktok", "order_id": "O1", "order_date": "2026-01-01",
                "status": "DELIVERED", "sku": "A", "product_name": "p", "quantity": 1,
                "total": 30.0, "currency": "USD", **kw}

    def test_discounts_round_trip_and_default_to_null(self) -> None:
        db.upsert_orders([self._base(seller_discount=15.0, platform_discount=5.0),
                          self._base(platform="shopify", order_id="#1001")])
        conn = sqlite3.connect(db.DB_PATH)
        got = dict((p, (s, d)) for p, s, d in conn.execute(
            "SELECT platform, seller_discount, platform_discount FROM orders"))
        conn.close()
        self.assertEqual(got["tiktok"], (15.0, 5.0))
        self.assertEqual(got["shopify"], (None, None))  # not captured, never 0

    def test_init_db_adds_discount_columns_to_orders(self) -> None:
        conn = sqlite3.connect(db.DB_PATH)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        conn.close()
        self.assertTrue({"seller_discount", "platform_discount"} <= cols)


if __name__ == "__main__":
    unittest.main()
