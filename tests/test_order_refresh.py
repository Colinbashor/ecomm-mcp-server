"""Orders change after they are created; both order connectors now re-read them.

Shopify: a second pass reads orders UPDATED since the window start but CREATED
before it (refunds, cancellations, voids). Amazon: stored orders still open
(Pending / Unshipped ...) are re-read by id. Hermetic: no network.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date
from unittest import mock

from warehouse.connectors import amazon_orders, shopify


def _order(name, created, status="PAID"):
    return {"name": name, "createdAt": f"{created}T10:00:00Z", "displayFinancialStatus": status,
            "sourceName": "web", "tags": [],
            "lineItems": {"nodes": [{"sku": "SKU-1001", "title": "Tee", "quantity": 1,
                                     "discountedTotalSet": {"shopMoney": {"amount": "20.0",
                                                                          "currencyCode": "USD"}},
                                     "originalTotalSet": {"shopMoney": {"amount": "20.0"}},
                                     "discountAllocations": []}]}}


class ShopifyUpdatedPassTests(unittest.TestCase):
    def test_updated_since_is_the_window_start_capped_at_the_lookback(self) -> None:
        today = date(2026, 11, 17)
        self.assertEqual(shopify.updated_since("2026-11-14", today), "2026-11-14")
        self.assertEqual(shopify.updated_since("2026-08-01", today), "2026-11-10")

    def test_the_updated_pass_excludes_what_the_created_pass_read(self) -> None:
        q = shopify.updated_query("2026-11-14", "2026-11-14")
        self.assertIn("updated_at:>='2026-11-14T00:00:00Z'", q)
        self.assertIn("created_at:<'2026-11-14T00:00:00Z'", q)

    def test_a_refund_on_an_older_order_lands(self) -> None:
        asked = []

        def fake_post(variables, query=None):
            asked.append(variables["q"])
            nodes = ([_order("#1002", "2026-11-15")] if variables["q"].startswith("created_at")
                     else [_order("#0950", "2026-10-30", status="REFUNDED")])
            return {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                               "nodes": nodes}}

        with mock.patch.object(shopify, "_post", fake_post), \
                mock.patch.object(shopify, "customer_capture_enabled", return_value=False), \
                mock.patch("warehouse.db.upsert_shopify_discounts"), \
                mock.patch("warehouse.db.replace_shopify_order_tags"):
            rows = shopify.sync("2026-11-14", "2026-11-16")
        self.assertEqual(len(asked), 2)
        by_order = {r["order_id"]: r for r in rows}
        self.assertEqual(by_order["#0950"]["status"], "REFUNDED")
        self.assertEqual(by_order["#0950"]["order_date"], "2026-10-30")   # original date kept
        self.assertEqual(by_order["#1002"]["status"], "PAID")


class AmazonOpenOrderRefreshTests(unittest.TestCase):
    def _db(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE orders (platform TEXT, order_id TEXT, order_date TEXT, "
                     "status TEXT, sku TEXT, total REAL)")
        conn.executemany("INSERT INTO orders (platform, order_id, order_date, status, sku, total) "
                         "VALUES ('amazon', ?, ?, ?, '', 0)", rows)
        return conn

    def test_only_open_orders_before_the_window_and_inside_the_lookback(self) -> None:
        conn = self._db([
            ("111-OLD-PENDING", "2026-10-01", "Pending"),
            ("111-UNSHIPPED", "2026-11-01", "Unshipped"),
            ("111-SHIPPED", "2026-10-02", "Shipped"),          # settled
            ("111-IN-WINDOW", "2026-11-15", "Pending"),        # created pass covers it
            ("111-ANCIENT", "2026-05-01", "Pending"),          # beyond the lookback
        ])
        ids = amazon_orders.open_order_ids(conn, "2026-11-14", today=date(2026, 11, 17))
        self.assertEqual(ids, ["111-UNSHIPPED", "111-OLD-PENDING"])           # newest first

    def test_open_orders_are_reread_by_id_in_chunks_of_50(self) -> None:
        conn = self._db([(f"111-{i:04d}", "2026-11-01", "Pending") for i in range(120)])
        calls = []

        def fake_get(host, path, params):
            ids = params["AmazonOrderIds"].split(",")
            calls.append(len(ids))
            return {"payload": {"Orders": [
                {"AmazonOrderId": i, "PurchaseDate": "2026-11-01T08:00:00Z",
                 "OrderStatus": "Shipped", "NumberOfItemsShipped": 1,
                 "OrderTotal": {"Amount": "40.00", "CurrencyCode": "USD"}} for i in ids]}}

        with mock.patch.object(amazon_orders, "_get", fake_get), \
                mock.patch.object(amazon_orders.time, "sleep"):
            rows = amazon_orders.refresh_open_orders("https://host", "MKT", "2026-11-14",
                                                     conn=conn)
        self.assertEqual(calls, [50, 50, 20])
        self.assertEqual(len(rows), 120)
        self.assertEqual((rows[0]["status"], rows[0]["total"]), ("Shipped", 40.0))

    def test_a_failure_returns_what_was_gathered_and_never_raises(self) -> None:
        conn = self._db([(f"111-{i:04d}", "2026-11-01", "Pending") for i in range(60)])
        first = {"payload": {"Orders": [{"AmazonOrderId": "111-0000",
                                         "OrderStatus": "Canceled"}]}}
        with mock.patch.object(amazon_orders, "_get",
                               side_effect=[first, RuntimeError("SP-API 500")]), \
                mock.patch.object(amazon_orders.time, "sleep"):
            rows = amazon_orders.refresh_open_orders("https://host", "MKT", "2026-11-14",
                                                     conn=conn)
        self.assertEqual([r["status"] for r in rows], ["Canceled"])


if __name__ == "__main__":
    unittest.main()
