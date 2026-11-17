"""feed_labels: label bands, velocity ranking, SKU ambiguity and feed ids.
Hermetic: in-memory SQLite built from the real DDL, no network."""
from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

import feed_labels as fl
from feed_fixtures import CLEAN_ENV, TODAY, add_order, add_variant, make_db
from feed_fixtures import epoch as _epoch


class BandTests(unittest.TestCase):
    def test_margin_bands_including_unknown_and_negative(self) -> None:
        self.assertEqual(fl.margin_band(100, 40, 0, 0), "Margin 50%+")       # 60%
        self.assertEqual(fl.margin_band(100, 40, 5, 0.03), "Margin 50%+")    # 52%
        self.assertEqual(fl.margin_band(100, 50, 5, 0.03), "Margin 40-50%")  # 42%
        self.assertEqual(fl.margin_band(100, 85, 5, 0.03), "Margin 0-10%")   # 7%
        self.assertEqual(fl.margin_band(100, 95, 8, 0.03), fl.NEGATIVE_MARGIN_LABEL)
        self.assertEqual(fl.margin_band(100, None, 5, 0.03), fl.UNKNOWN_MARGIN_LABEL)
        self.assertEqual(fl.margin_band(100, 0, 5, 0.03), fl.UNKNOWN_MARGIN_LABEL)
        self.assertEqual(fl.margin_band(None, 10, 5, 0.03), fl.UNKNOWN_MARGIN_LABEL)

    def test_an_implausible_cost_is_unknown_not_a_loss(self) -> None:
        self.assertEqual(fl.margin_band(20, 45, 0, 0), fl.UNKNOWN_MARGIN_LABEL)
        self.assertEqual(fl.margin_band(20, 30, 0, 0), fl.NEGATIVE_MARGIN_LABEL)

    def test_markdown_bands(self) -> None:
        self.assertEqual(fl.markdown_band(40, None), fl.FULL_PRICE_LABEL)
        self.assertEqual(fl.markdown_band(40, 40), fl.FULL_PRICE_LABEL)
        self.assertEqual(fl.markdown_band(40, 30), fl.FULL_PRICE_LABEL)     # compare-at below price
        self.assertEqual(fl.markdown_band(36, 40), fl.LIGHT_MARKDOWN_LABEL)  # 10% off
        self.assertEqual(fl.markdown_band(30, 40), fl.DEEP_MARKDOWN_LABEL)   # exactly 25% off

    def test_new_arrival_is_blank_when_unknown(self) -> None:
        self.assertEqual(fl.new_arrival_band(None, TODAY, 30), "")
        self.assertEqual(fl.new_arrival_band(_epoch(TODAY - dt.timedelta(days=30)), TODAY, 30),
                         fl.NEW_ARRIVAL_LABEL)
        self.assertEqual(fl.new_arrival_band(_epoch(TODAY - dt.timedelta(days=31)), TODAY, 30),
                         fl.ESTABLISHED_LABEL)

    def test_velocity_is_by_rank_and_ties_share_a_band(self) -> None:
        units = {f"SKU-{i}": float(u) for i, u in enumerate([50, 40, 30, 30, 20, 10, 5, 3, 2, 1])}
        th = fl.velocity_thresholds(units)
        self.assertEqual(th, (50.0, 20.0))
        self.assertEqual(fl.velocity_band(50, th), fl.TOP_SELLER_LABEL)
        self.assertEqual(fl.velocity_band(30, th), fl.STEADY_SELLER_LABEL)
        self.assertEqual(fl.velocity_band(1, th), fl.SLOW_SELLER_LABEL)
        self.assertEqual(fl.velocity_band(0, th), fl.NO_SALES_LABEL)
        tied = fl.velocity_thresholds({"A": 5.0, "B": 5.0, "C": 5.0})
        self.assertEqual({fl.velocity_band(5, tied)}, {fl.TOP_SELLER_LABEL})


@mock.patch.dict(os.environ, CLEAN_ENV)
class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        add_variant(self.conn, "1000000000101", sku="SKU-1001", price=40, cost=10,
                    launch=_epoch(TODAY - dt.timedelta(days=3)))
        add_variant(self.conn, "1000000000102", sku="SKU-1002", price=30, compare_at=40, cost=None)
        add_variant(self.conn, "1000000000103", sku="SKU-1003", status="ARCHIVED")
        add_variant(self.conn, "1000000000104", product_id="1000000000002", sku="SKU-1004")
        add_variant(self.conn, "1000000000105", product_id="1000000000003", sku="sku-1004 ")
        add_order(self.conn, "#1001", "SKU-1001", 5)
        add_order(self.conn, "#1002", "SKU-1004", 1)
        add_order(self.conn, "#1003", "SKU-1002", 9, day=TODAY - dt.timedelta(days=90))
        add_order(self.conn, "TT-1", "SKU-1002", 99, platform="tiktok")

    def _rows(self) -> dict[str, dict]:
        return {r["variant_id"]: r for r in fl.build_label_rows(self.conn, today=TODAY)}

    def test_only_active_variants_are_labelled(self) -> None:
        self.assertNotIn("1000000000103", self._rows())
        self.assertEqual(len(self._rows()), 4)

    def test_labels_per_variant(self) -> None:
        rows = self._rows()
        a = rows["1000000000101"]
        self.assertEqual([a[c] for c in fl.LABEL_COLUMNS],
                         ["Margin 50%+", fl.FULL_PRICE_LABEL, fl.NEW_ARRIVAL_LABEL,
                          fl.TOP_SELLER_LABEL, ""])
        b = rows["1000000000102"]
        self.assertEqual(b["custom_label_0"], fl.UNKNOWN_MARGIN_LABEL)
        self.assertEqual(b["custom_label_1"], fl.DEEP_MARKDOWN_LABEL)
        self.assertEqual(b["custom_label_2"], "")
        # sold only outside the window, and on a platform not configured
        self.assertEqual(b["custom_label_3"], fl.NO_SALES_LABEL)

    def test_env_settings_change_the_math(self) -> None:
        with mock.patch.dict(os.environ, {"FEED_LABEL_SHIP_COST": "10",
                                          "FEED_LABEL_DEDUCTION_RATE": "0.05",
                                          "FEED_LABEL_ORDER_PLATFORMS": "shopify,tiktok"}):
            rows = self._rows()
        self.assertEqual(rows["1000000000101"]["custom_label_0"], "Margin 40-50%")  # 45%
        self.assertEqual(rows["1000000000102"]["custom_label_3"], fl.TOP_SELLER_LABEL)

    def test_a_shared_sku_is_flagged_ambiguous(self) -> None:
        rows = self._rows()
        self.assertTrue(rows["1000000000104"]["sku_ambiguous"])
        self.assertTrue(rows["1000000000105"]["sku_ambiguous"])
        self.assertFalse(rows["1000000000101"]["sku_ambiguous"])

    def test_no_orders_in_the_window_means_blank_velocity_not_zero(self) -> None:
        self.conn.execute("DELETE FROM orders")
        self.assertEqual({r["custom_label_3"] for r in self._rows().values()}, {""})


class FeedIdTests(unittest.TestCase):
    ROW = {"variant_id": "1000000000101", "product_id": "1000000000001", "sku": "SKU-1001",
           "sku_ambiguous": False}

    def test_each_format(self) -> None:
        self.assertEqual(fl.feed_id(self.ROW, "sku", "US"), "SKU-1001")
        self.assertEqual(fl.feed_id(self.ROW, "variant_id", "US"), "1000000000101")
        self.assertEqual(fl.feed_id(self.ROW, "shopify", "ca"),
                         "shopify_CA_1000000000001_1000000000101")

    def test_an_ambiguous_or_blank_sku_has_no_sku_id(self) -> None:
        self.assertIsNone(fl.feed_id({**self.ROW, "sku_ambiguous": True}, "sku", "US"))
        self.assertIsNone(fl.feed_id({**self.ROW, "sku": ""}, "sku", "US"))
        self.assertEqual(fl.feed_id({**self.ROW, "sku_ambiguous": True}, "variant_id", "US"),
                         "1000000000101")

    def test_keyed_rows_drops_unnameable_and_duplicate_ids(self) -> None:
        rows = [self.ROW, dict(self.ROW, variant_id="1000000000102", sku="sku-1001"),
                dict(self.ROW, variant_id="1000000000103", sku="")]
        keyed, dropped = fl.keyed_rows(rows, "sku", "US")
        self.assertEqual(([k for k, _ in keyed], dropped), (["SKU-1001"], 2))

    def test_the_format_must_be_configured(self) -> None:
        with mock.patch.dict(os.environ, {"GMC_FEED_ID_FORMAT": ""}):
            with self.assertRaises(SystemExit):
                fl.feed_id_format()
        with mock.patch.dict(os.environ, {"GMC_FEED_ID_FORMAT": "offer"}):
            with self.assertRaises(SystemExit):
                fl.feed_id_format()
        with mock.patch.dict(os.environ, {"GMC_FEED_ID_FORMAT": "Shopify"}):
            self.assertEqual(fl.feed_id_format(), "shopify")
        with mock.patch.dict(os.environ, {"META_CATALOG_RETAILER_ID_FORMAT": ""}):
            self.assertEqual(fl.feed_id_format("META_CATALOG_RETAILER_ID_FORMAT", "variant_id"),
                             "variant_id")


if __name__ == "__main__":
    unittest.main()
