"""Hermetic tests for amazon_economics_sync.py — no network, no real DB file.

Covers: schema creation, the money/charge-total/fee-component helpers
(including the fees-vs-ads shape difference and list-vs-dict inputs), the
Mon-Sun day-grain aggregation that works around the API's Sun-Sat `date:WEEK`
gotcha, and the required-env-var guard.
"""
from __future__ import annotations

import os
import sqlite3
import unittest

import amazon_economics_sync as econ


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        econ.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_economics)")}
        self.assertIn("ordered_product_sales", cols)
        self.assertIn("net_proceeds_total", cols)
        self.assertIn("total_fees", cols)


class MoneyHelperTests(unittest.TestCase):
    def test_money_extracts_amount_and_currency(self) -> None:
        self.assertEqual(econ._money({"amount": "12.50", "currencyCode": "USD"}), (12.5, "USD"))

    def test_money_handles_non_dict(self) -> None:
        self.assertEqual(econ._money(None), (0.0, None))

    def test_money_handles_bad_amount(self) -> None:
        self.assertEqual(econ._money({"amount": "oops", "currencyCode": "USD"}), (0.0, "USD"))


class ChargeTotalTests(unittest.TestCase):
    def test_fees_shape_is_a_list_of_aggregated_detail(self) -> None:
        node = [{"charge": {"aggregatedDetail": {"totalAmount": {"amount": "1.50"}}}},
                {"charge": {"aggregatedDetail": {"totalAmount": {"amount": "2.50"}}}}]
        self.assertEqual(econ._charge_total(node), 4.0)

    def test_ads_shape_is_charge_total_amount_directly(self) -> None:
        node = [{"charge": {"totalAmount": {"amount": "3.00"}}}]
        self.assertEqual(econ._charge_total(node), 3.0)

    def test_single_dict_not_list_also_works(self) -> None:
        node = {"charge": {"totalAmount": {"amount": "1.00"}}}
        self.assertEqual(econ._charge_total(node), 1.0)

    def test_empty_or_none_is_zero(self) -> None:
        self.assertEqual(econ._charge_total(None), 0.0)
        self.assertEqual(econ._charge_total([]), 0.0)


class FeeComponentNamesTests(unittest.TestCase):
    def test_collects_names_across_list(self) -> None:
        node = [{"charge": {"components": [{"name": "ReferralFee"}, {"name": None}]}}]
        self.assertEqual(econ._fee_component_names(node), ["ReferralFee"])

    def test_none_node_returns_empty(self) -> None:
        self.assertEqual(econ._fee_component_names(None), [])


class AggregateWeekTests(unittest.TestCase):
    def _daily(self, asin: str, ops: float, units: int) -> dict:
        return {
            "childAsin": asin,
            "parentAsin": f"P-{asin}",
            "msku": f"MSKU-{asin}",
            "fnsku": f"FN-{asin}",
            "marketplaceId": "ATVPDKIKX0DER",
            "sales": {
                "orderedProductSales": {"amount": str(ops), "currencyCode": "USD"},
                "netProductSales": {"amount": str(ops * 0.9), "currencyCode": "USD"},
                "netUnitsSold": units,
                "unitsOrdered": units,
                "unitsRefunded": 0,
            },
            "fees": [{"charge": {"aggregatedDetail": {"totalAmount": {"amount": "1.00"}},
                                  "components": [{"name": "ReferralFee"}]}}],
            "ads": [{"charge": {"totalAmount": {"amount": "0.50"}}}],
            "netProceeds": {"total": {"amount": str(ops * 0.7), "currencyCode": "USD"}},
            "cost": {"costOfGoodsSold": None},
        }

    def test_sums_multiple_days_for_same_asin(self) -> None:
        rows = [self._daily("B1", 10.0, 1), self._daily("B1", 20.0, 2)]
        agg = econ.aggregate_week(rows)
        self.assertEqual(set(agg), {"B1"})
        a = agg["B1"]
        self.assertAlmostEqual(a["ops"], 30.0)
        self.assertEqual(a["units_ordered"], 3)
        self.assertAlmostEqual(a["fees"], 2.0)
        self.assertAlmostEqual(a["ads"], 1.0)
        self.assertEqual(a["fee_names"], {"ReferralFee"})

    def test_keeps_separate_asins_separate(self) -> None:
        rows = [self._daily("B1", 10.0, 1), self._daily("B2", 5.0, 1)]
        agg = econ.aggregate_week(rows)
        self.assertEqual(set(agg), {"B1", "B2"})

    def test_rows_without_child_asin_are_skipped(self) -> None:
        rows = [{"sales": {}}]
        self.assertEqual(econ.aggregate_week(rows), {})

    def test_row_shaping_computes_average_selling_price(self) -> None:
        rows = [self._daily("B1", 100.0, 4)]
        agg = econ.aggregate_week(rows)
        tup = econ._row("2026-08-03", "2026-08-09", "B1", agg["B1"], "stamp")
        # (week_start, asin, parent_asin, msku, fnsku, marketplace_id, range_start,
        #  range_end, ops, nps, net_units, units_ordered, units_refunded, asp, ...)
        self.assertEqual(tup[0], "2026-08-03")
        self.assertEqual(tup[1], "B1")
        self.assertEqual(tup[8], 100.0)   # ordered_product_sales
        self.assertEqual(tup[11], 4)      # units_ordered
        self.assertEqual(tup[13], 25.0)   # avg_selling_price = 100/4


class RequireEnvTests(unittest.TestCase):
    def test_missing_vars_raise_systemexit(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in econ.REQUIRED_ENV}
        try:
            with self.assertRaises(SystemExit) as cm:
                econ.require_env()
            for k in econ.REQUIRED_ENV:
                self.assertIn(k, str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
