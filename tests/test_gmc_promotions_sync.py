"""gmc_promotions_sync: which Shopify codes become Merchant Center promotions,
how a row is built, the eligible set, and what each run logs and pushes.
Hermetic: the Shopify connector, Drive and the warehouse are patched out."""
from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

import gmc_promotions_sync as gp
from feed_fixtures import CLEAN_ENV, add_variant, make_db

NOW = dt.datetime(2026, 6, 15, 12, tzinfo=dt.timezone.utc)
ENV = {**CLEAN_ENV, "GMC_FEED_ID_FORMAT": "variant_id", "SHOPIFY_SHOP": "my-store.myshopify.com",
       "SHOPIFY_ADMIN_TOKEN": "test-token", "GMC_PROMO_SHEET_ID": "YOUR_PROMO_SHEET_FILE_ID",
       "GMC_PROMO_INCLUSIONS_SHEET_ID": "YOUR_INCLUSIONS_SHEET_FILE_ID",
       "GMC_CREDENTIALS_FILE": "key.json", "GMC_PROMO_ELIGIBLE_TAG": "",
       "GMC_PROMO_COLLECTION_HANDLE": "", "GMC_PROMO_SKIP_TITLE_PREFIXES": "",
       "GMC_PROMO_CURRENCY": ""}


def _code(**over):
    d = {
        "__typename": "DiscountCodeBasic", "title": "Weekend sale", "status": "ACTIVE",
        "codesCount": {"count": 1}, "usageLimit": None, "appliesOncePerCustomer": False,
        "customerSelection": {"__typename": "DiscountCustomerAll"},
        "startsAt": "2026-06-13T08:00:00Z", "endsAt": "2026-06-18T08:00:00Z",
        "codes": {"nodes": [{"code": "SAVE20"}]},
        "customerGets": {"items": {"__typename": "AllDiscountItems"},
                         "value": {"__typename": "DiscountPercentage", "percentage": 0.2}},
        "minimumRequirement": None,
    }
    d.update(over)
    return d


def _on(*handles):
    return _code(customerGets={"items": {"__typename": "DiscountCollections",
                 "collections": {"nodes": [{"handle": h} for h in handles]}},
                 "value": {"__typename": "DiscountPercentage", "percentage": 0.1}})


class ClassifyTests(unittest.TestCase):
    def test_a_plain_public_code_is_kept(self) -> None:
        self.assertEqual(gp.classify(_code(), NOW, 0), ("ok", ""))

    def test_each_rule_skips_with_its_reason(self) -> None:
        cases = {
            "multi-code batch": _code(codesCount={"count": 500}),
            "usage-limited": _code(usageLimit=1),
            "customer-restricted": _code(customerSelection={"__typename": "DiscountCustomers"}),
            "no end date": _code(endsAt=None),
            "already ended": _code(endsAt="2026-06-14T08:00:00Z"),
            "BXGY not mapped yet": _code(__typename="DiscountCodeBxgy"),
        }
        for reason, d in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(gp.classify(d, NOW, 0), ("skip", reason))

    def test_a_window_shorter_than_the_minimum_is_skipped(self) -> None:
        short = _code(startsAt="2026-06-15T08:00:00Z", endsAt="2026-06-15T20:00:00Z")
        self.assertEqual(gp.classify(short, NOW, 24)[0], "skip")
        self.assertEqual(gp.classify(short, NOW, 6)[0], "ok")

    def test_a_collection_code_needs_the_configured_handle(self) -> None:
        self.assertEqual(gp.classify(_on("sale-eligible"), NOW, 0)[0], "skip")   # none configured
        self.assertEqual(gp.classify(_on("sale-eligible"), NOW, 0, "sale-eligible")[0], "ok")
        self.assertEqual(gp.classify(_on("other"), NOW, 0, "sale-eligible")[0], "skip")
        self.assertEqual(gp.classify(_on("sale-eligible", "other"), NOW, 0, "sale-eligible")[0],
                         "skip")

    def test_specific_product_codes_are_skipped(self) -> None:
        d = _code(customerGets={"items": {"__typename": "DiscountProducts"},
                                "value": {"__typename": "DiscountPercentage", "percentage": 0.1}})
        self.assertEqual(gp.classify(d, NOW, 0), ("skip", "DiscountProducts not mapped"))


class RowTests(unittest.TestCase):
    def test_promotion_id_is_stable_per_code_and_start(self) -> None:
        start = dt.datetime(2026, 6, 13, tzinfo=dt.timezone.utc)
        self.assertEqual(gp.promotion_id("save-20!", start), "SHOP_SAVE20_20260613")
        self.assertNotEqual(gp.promotion_id("SAVE20", start),
                            gp.promotion_id("SAVE20", start + dt.timedelta(days=7)))

    def test_percent_off_with_a_minimum(self) -> None:
        row = gp.to_row(_code(minimumRequirement={
            "__typename": "DiscountMinimumSubtotal",
            "greaterThanOrEqualToSubtotal": {"amount": "50.0"}}), "US", "USD", scoped=True)
        self.assertEqual(row["coupon_value_type"], "buy_m_get_percent_off")
        self.assertEqual(row["percent_off"], 20)
        self.assertEqual(row["minimum_purchase_amount"], "50.00 USD")
        self.assertIn("select items on 50 USD+", row["long_title"])
        self.assertLessEqual(len(row["long_title"]), gp.LONG_TITLE_MAX)
        self.assertEqual(row["promotion_effective_dates"],
                         "2026-06-13T08:00:00Z/2026-06-18T08:00:00Z")

    def test_money_off_free_shipping_and_settings(self) -> None:
        money = gp.to_row(_code(customerGets={"items": {"__typename": "AllDiscountItems"},
                          "value": {"__typename": "DiscountAmount", "amount": {"amount": "15"}}}),
                          "CA", "CAD", scoped=False)
        self.assertEqual((money["coupon_value_type"], money["money_off_amount"],
                          money["target_country"], money["long_title"]),
                         ("money_off", "15.00 CAD", "CA", "15 CAD off with code SAVE20"))
        ship = gp.to_row(_code(__typename="DiscountCodeFreeShipping"), "US", "USD", False)
        self.assertEqual(ship["coupon_value_type"], "free_shipping_standard")

    def test_search_query_carries_the_prefix_prefilter(self) -> None:
        q = gp.search_query(NOW, 14, ("WELCOME", "LOYALTY"))
        self.assertIn("-title:WELCOME* -title:LOYALTY*", q)
        self.assertIn("starts_at:<2026-06-29", q)
        self.assertNotIn("-title", gp.search_query(NOW, 14, ()))


@mock.patch.dict(os.environ, ENV)
class EligibleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        add_variant(self.conn, "1000000000101", sku="SKU-1001", tags="summer, Promo-OK")
        add_variant(self.conn, "1000000000102", sku="SKU-1002", tags="summer")
        add_variant(self.conn, "1000000000103", sku="SKU-1003", tags="promo-ok", status="ARCHIVED")

    def test_unset_tag_means_every_active_product(self) -> None:
        ids, _ = gp.eligible_feed_ids(self.conn, "")
        self.assertEqual(ids, ["1000000000101", "1000000000102"])

    def test_the_tag_scopes_the_set_case_insensitively(self) -> None:
        ids, _ = gp.eligible_feed_ids(self.conn, "promo-ok")
        self.assertEqual(ids, ["1000000000101"])

    def test_ids_follow_the_feed_id_format(self) -> None:
        with mock.patch.dict(os.environ, {"GMC_FEED_ID_FORMAT": "shopify",
                                          "GMC_TARGET_COUNTRY": "GB"}):
            ids, _ = gp.eligible_feed_ids(self.conn, "promo-ok")
        self.assertEqual(ids, ["shopify_GB_1000000000001_1000000000101"])

    def test_a_collection_handle_without_a_tag_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"GMC_PROMO_COLLECTION_HANDLE": "sale-eligible"}):
            with self.assertRaises(SystemExit):
                gp.check_required_env(False)


@mock.patch.dict(os.environ, ENV)
class MainTests(unittest.TestCase):
    """Every --execute run leaves a sync_log row that says what happened."""

    def _main(self, discounts=None, fetch_error=None, push_error=None, argv=("--execute",),
              n_variants=2):
        conn = make_db()
        for i in range(n_variants):
            add_variant(conn, f"100000000010{i}", sku=f"SKU-100{i}")
        logged = []
        fetch = (mock.patch.object(gp, "fetch_discounts", side_effect=fetch_error)
                 if fetch_error else
                 mock.patch.object(gp, "fetch_discounts", return_value=discounts or []))
        with fetch, \
                mock.patch.object(gp, "_open_db", return_value=conn), \
                mock.patch.object(gp.drive, "replace_with_rows",
                                  side_effect=push_error) as push, \
                mock.patch.object(gp.db, "log_sync", side_effect=lambda *a: logged.append(a)), \
                mock.patch.object(gp, "_utcnow", return_value=NOW):
            rc = gp.main(list(argv))
        return rc, logged, push

    def test_no_live_codes_pushes_header_only_sheets_and_logs_ok(self) -> None:
        rc, logged, push = self._main([])
        self.assertEqual(rc, 0)
        self.assertEqual([c.args[0] for c in push.call_args_list],
                         ["YOUR_INCLUSIONS_SHEET_FILE_ID", "YOUR_PROMO_SHEET_FILE_ID"])
        self.assertEqual([c.args[3] for c in push.call_args_list], [[], []])
        self.assertEqual((logged[-1][0], logged[-1][3]), ("gmc_promotions_sync", "ok"))

    def test_a_live_code_pushes_inclusions_before_promotions(self) -> None:
        rc, logged, push = self._main([_code()])
        self.assertEqual(rc, 0)
        inclusions, promos = (c.args for c in push.call_args_list)
        self.assertEqual(inclusions[0], "YOUR_INCLUSIONS_SHEET_FILE_ID")
        self.assertEqual(inclusions[3], [["1000000000100", "SHOP_SAVE20_20260613"],
                                         ["1000000000101", "SHOP_SAVE20_20260613"]])
        self.assertEqual(promos[3][0][0], "SHOP_SAVE20_20260613")
        self.assertEqual((logged[-1][2], logged[-1][3]), (1, "ok"))

    def test_a_live_code_with_no_eligible_products_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"GMC_PROMO_ELIGIBLE_TAG": "no-such-tag"}):
            rc, logged, push = self._main([_code()])
        self.assertEqual((rc, push.call_count, logged[-1][3]), (1, 0, "error"))

    def test_a_shopify_failure_logs_error(self) -> None:
        rc, logged, push = self._main(fetch_error=RuntimeError("boom"))
        self.assertEqual((rc, logged[-1][3], push.call_count), (1, "error", 0))

    def test_a_push_failure_logs_error(self) -> None:
        rc, logged, _ = self._main([_code()], push_error=RuntimeError("Drive 403"))
        self.assertEqual(rc, 1)
        self.assertEqual(logged[-1][3], "error")
        self.assertIn("Drive 403", logged[-1][4])

    def test_a_dry_run_pushes_and_logs_nothing(self) -> None:
        rc, logged, push = self._main([_code()], argv=())
        self.assertEqual((rc, logged, push.call_count), (0, [], 0))

    def test_execute_requires_both_sheet_ids(self) -> None:
        with mock.patch.dict(os.environ, {"GMC_PROMO_SHEET_ID": ""}):
            gp.check_required_env(False)
            with self.assertRaises(SystemExit):
                gp.check_required_env(True)


if __name__ == "__main__":
    unittest.main()
