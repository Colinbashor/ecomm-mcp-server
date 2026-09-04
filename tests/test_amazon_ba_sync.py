"""Hermetic tests for amazon_ba_sync.py — no network, no real DB file.

Covers: schema creation, the money/percent/lookup helpers (including the
FRACTION -> PERCENT normalization that's the module's core unit trap), the
optional brand_watchlist.yaml loader (missing file, present file, rank
override) and its word-boundary regex, the "our ASINs" sourcing helper, each
report grain's row shaping (including the Search Terms 'ours'/'brand'/'rank'
filter — the report is otherwise market-wide and would be huge if stored
unfiltered), and the calendar-month window helper used by Repeat Purchase.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import amazon_ba_sync as ba


class SchemaTests(unittest.TestCase):
    def test_all_four_tables_are_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(ba.DDL)
        for table in ("amazon_ba_search_catalog", "amazon_ba_search_terms",
                      "amazon_ba_market_basket", "amazon_ba_repeat_purchase"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            self.assertTrue(cols, f"{table} was not created")
        conn.close()


class ValueHelperTests(unittest.TestCase):
    def test_g_returns_first_present_key(self) -> None:
        self.assertEqual(ba._g({"b": "x"}, "a", "b"), "x")

    def test_g_returns_none_when_nothing_matches(self) -> None:
        self.assertIsNone(ba._g({"a": None, "b": ""}, "a", "b"))

    def test_i_none_safe(self) -> None:
        self.assertEqual(ba._i(None), 0)
        self.assertEqual(ba._i("4"), 4)

    def test_amt_from_money_block(self) -> None:
        self.assertEqual(ba._amt({"amount": "12.5"}), 12.5)

    def test_amt_from_named_key_lookup(self) -> None:
        self.assertEqual(ba._amt({"price": {"amount": "9.99"}}, "price"), 9.99)

    def test_amt_missing_is_zero(self) -> None:
        self.assertEqual(ba._amt({}, "missing"), 0.0)

    def test_pct_converts_fraction_to_percent(self) -> None:
        # This is the module's core unit trap: these four reports return
        # FRACTIONS, unlike SQP which returns PERCENT already.
        self.assertEqual(ba._pct(0.0751), 7.51)

    def test_pct_none_safe(self) -> None:
        self.assertIsNone(ba._pct(None))
        self.assertIsNone(ba._pct(""))


class BrandWatchlistTests(unittest.TestCase):
    def test_missing_file_gives_empty_watchlist_and_default_rank(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.object(ba, "HERE", Path(d)):
                self.assertEqual(ba._watchlist(), [])
                self.assertEqual(ba._rank_max(), ba.DEFAULT_RANK_FLAG_MAX)

    def test_present_file_is_parsed_lowercased(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "brand_watchlist.yaml"
            path.write_text(
                "house_brands:\n  - Acme\ncompetitor_brands:\n  - Widgetco\n"
                "rank_flag_max: 999\n",
                encoding="utf-8",
            )
            with patch.object(ba, "HERE", Path(d)):
                self.assertEqual(sorted(ba._watchlist()), ["acme", "widgetco"])
                self.assertEqual(ba._rank_max(), 999)

    def test_empty_file_behaves_like_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "brand_watchlist.yaml"
            path.write_text("", encoding="utf-8")
            with patch.object(ba, "HERE", Path(d)):
                self.assertEqual(ba._watchlist(), [])

    def test_yaml_not_installed_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "brand_watchlist.yaml"
            path.write_text("house_brands:\n  - Acme\n", encoding="utf-8")
            with patch.object(ba, "HERE", Path(d)), \
                 patch.dict("sys.modules", {"yaml": None}):
                self.assertEqual(ba._watchlist_config(), {})

    def test_missing_term_topics_gives_empty_topics_and_default_cap(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.object(ba, "HERE", Path(d)):
                self.assertEqual(ba._topics(), {})
                self.assertEqual(ba._topic_cap(), ba.DEFAULT_TOPIC_MAX_ROWS_PER_WEEK)

    def test_term_topics_parsed_with_cap_override(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "brand_watchlist.yaml"
            path.write_text(
                "term_topics:\n"
                "  widgets:\n"
                "    - '\\bwidget\\b'\n"
                "topic_max_rows_per_week: 5\n",
                encoding="utf-8",
            )
            with patch.object(ba, "HERE", Path(d)):
                self.assertEqual(ba._topics(), {"widgets": [r"\bwidget\b"]})
                self.assertEqual(ba._topic_cap(), 5)

    def test_topic_regexes_are_case_insensitive_and_OR_within_a_topic(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "brand_watchlist.yaml"
            path.write_text(
                "term_topics:\n"
                "  widgets:\n"
                "    - '\\bwidget\\b'\n"
                "    - '\\bgadget\\b'\n",
                encoding="utf-8",
            )
            with patch.object(ba, "HERE", Path(d)):
                rx = ba._topic_regexes()["widgets"]
                self.assertIsNotNone(rx.search("blue WIDGET large"))
                self.assertIsNotNone(rx.search("a gadget for sale"))
                self.assertIsNone(rx.search("gizmo only"))


class WatchRegexTests(unittest.TestCase):
    def test_empty_list_returns_none(self) -> None:
        self.assertIsNone(ba._watch_regex([]))

    def test_word_boundary_prevents_substring_false_positive(self) -> None:
        rx = ba._watch_regex(["acme"])
        self.assertIsNone(rx.search("academic"))
        self.assertIsNotNone(rx.search("acme boots"))

    def test_multi_word_phrase_matches(self) -> None:
        rx = ba._watch_regex(["super brand co"])
        self.assertIsNotNone(rx.search("buy super brand co shoes"))


class TargetAsinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_explicit_asins_flag(self) -> None:
        args = argparse.Namespace(asins="B1,B2", asins_file=None)
        self.assertEqual(ba._target_asins(args, self.conn), ["B1", "B2"])

    def test_falls_back_when_neither_flag_given(self) -> None:
        with patch.object(ba, "fallback_asins", return_value=["FB1"]):
            args = argparse.Namespace(asins=None, asins_file=None)
            self.assertEqual(ba._target_asins(args, self.conn), ["FB1"])


class SearchCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ba.DDL)

    def tearDown(self) -> None:
        self.conn.close()

    def test_row_shape_and_percent_normalization(self) -> None:
        rec = {
            "asin": "B1",
            "impressionData": {"impressionCount": 100, "impressionMedianPrice": {"amount": "20.00"}},
            "clickData": {"clickCount": 10, "clickRate": 0.10,
                         "clickedMedianPrice": {"amount": "19.50"}},
            "cartAddData": {"cartAddCount": 2},
            "purchaseData": {"purchaseCount": 1, "searchTrafficSales": {"amount": "19.50"},
                            "conversionRate": 0.10, "purchaseMedianPrice": {"amount": "19.50"}},
        }
        with patch.object(ba, "run_ba_report", return_value=[rec]):
            n = ba.sync_search_catalog(self.conn, date(2026, 7, 19), "stamp", set())
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT asin, impressions, click_rate_pct, conversion_rate_pct "
            "FROM amazon_ba_search_catalog"
        ).fetchone()
        self.assertEqual(row, ("B1", 100, 10.0, 10.0))

    def test_records_without_asin_are_skipped(self) -> None:
        with patch.object(ba, "run_ba_report", return_value=[{"impressionData": {}}]):
            n = ba.sync_search_catalog(self.conn, date(2026, 7, 19), "stamp", set())
        self.assertEqual(n, 0)


class SearchTermsFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ba.DDL)

    def tearDown(self) -> None:
        self.conn.close()

    def _rec(self, term: str, clicked_asin: str, rank: int) -> dict:
        return {
            "departmentName": "Apparel",
            "searchTerm": term,
            "searchFrequencyRank": rank,
            "clickedAsin": clicked_asin,
            "clickedItemName": "Some Item",
            "clickShareRank": 1,
            "clickShare": 0.5,
            "conversionShare": 0.25,
        }

    def test_kept_when_our_asin_in_top3(self) -> None:
        recs = [self._rec("term one", "OURS1", 999999)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=1):
            n = ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp", {"OURS1"})
        self.assertEqual(n, 1)
        reason = self.conn.execute("SELECT match_reason FROM amazon_ba_search_terms").fetchone()[0]
        self.assertEqual(reason, "ours")

    def test_kept_when_rank_within_threshold(self) -> None:
        recs = [self._rec("term two", "SOMEONE_ELSE", 5)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=2500):
            n = ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp", set())
        self.assertEqual(n, 1)
        reason = self.conn.execute("SELECT match_reason FROM amazon_ba_search_terms").fetchone()[0]
        self.assertEqual(reason, "rank")

    def test_dropped_when_no_rule_matches(self) -> None:
        recs = [self._rec("term three", "SOMEONE_ELSE", 999999)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=1):
            n = ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp", set())
        self.assertEqual(n, 0)

    def test_percent_normalization_on_shares(self) -> None:
        recs = [self._rec("term four", "OURS1", 999999)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=1):
            ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp", {"OURS1"})
        click_pct, conv_pct = self.conn.execute(
            "SELECT click_share_pct, conversion_share_pct FROM amazon_ba_search_terms"
        ).fetchone()
        self.assertEqual((click_pct, conv_pct), (50.0, 25.0))

    def test_topic_capture_keeps_a_term_nobody_here_sells(self) -> None:
        # No 'ours'/'brand'/'rank' rule can fire (rank_max=0), so only topic
        # capture can be responsible for keeping this row -- this is the
        # whole point of the feature: surfacing demand for something the
        # account doesn't sell at all.
        recs = [self._rec("blue widget", "COMPETITOR1", 999999)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=0), \
             patch.object(ba, "_topic_regexes",
                          return_value={"widgets": ba.re.compile(r"widget", ba.re.I)}), \
             patch.object(ba, "_topic_cap", return_value=100):
            n = ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp", set())
        self.assertEqual(n, 1)
        asin, reason = self.conn.execute(
            "SELECT clicked_asin, match_reason FROM amazon_ba_search_terms").fetchone()
        self.assertEqual(reason, "topic:widgets")
        self.assertEqual(asin, "COMPETITOR1",
                         "the competitor ASIN must be kept -- that's the point")

    def test_topic_capture_never_overrides_a_stronger_match_reason(self) -> None:
        # A term that's already 'ours' must stay 'ours', not get relabeled by
        # a topic regex that happens to also match it.
        recs = [self._rec("blue widget", "OURS1", 999999)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=0), \
             patch.object(ba, "_topic_regexes",
                          return_value={"widgets": ba.re.compile(r"widget", ba.re.I)}), \
             patch.object(ba, "_topic_cap", return_value=100):
            ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp", {"OURS1"})
        reason = self.conn.execute(
            "SELECT match_reason FROM amazon_ba_search_terms").fetchone()[0]
        self.assertEqual(reason, "ours")

    def test_topic_capture_respects_the_per_topic_weekly_cap(self) -> None:
        # Counted per TERM, not per row -- so a cap of 1 keeps exactly the
        # first matching term and drops the second, never a partial term.
        recs = [self._rec("blue widget", "C1", 999999),
                self._rec("red widget", "C2", 999999)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=0), \
             patch.object(ba, "_topic_regexes",
                          return_value={"widgets": ba.re.compile(r"widget", ba.re.I)}), \
             patch.object(ba, "_topic_cap", return_value=1):
            n = ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp", set())
        self.assertEqual(n, 1, "the cap must stop at whole terms, not rows")

    def test_a_prior_weeks_rows_are_replaced_not_appended(self) -> None:
        recs = [self._rec("term five", "OURS1", 999999)]
        with patch.object(ba, "run_ba_report", return_value=recs), \
             patch.object(ba, "_watch_regex", return_value=None), \
             patch.object(ba, "_rank_max", return_value=1):
            ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp1", {"OURS1"})
            ba.sync_search_terms(self.conn, date(2026, 7, 19), "stamp2", {"OURS1"})
        n = self.conn.execute(
            "SELECT COUNT(*) FROM amazon_ba_search_terms WHERE week_start='2026-07-19'"
        ).fetchone()[0]
        self.assertEqual(n, 1)


class MarketBasketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ba.DDL)

    def tearDown(self) -> None:
        self.conn.close()

    def test_is_ours_flag_set_correctly(self) -> None:
        recs = [
            {"asin": "B1", "purchasedWithAsin": "OURS1", "purchasedWithRank": 1,
             "combination": 0.20},
            {"asin": "B1", "purchasedWithAsin": "OTHER1", "purchasedWithRank": 2,
             "combination": 0.05},
        ]
        with patch.object(ba, "run_ba_report", return_value=recs):
            n = ba.sync_market_basket(self.conn, date(2026, 7, 19), "stamp", {"OURS1"})
        self.assertEqual(n, 2)
        rows = dict(self.conn.execute(
            "SELECT purchased_with_asin, is_ours FROM amazon_ba_market_basket"
        ).fetchall())
        self.assertEqual(rows, {"OURS1": 1, "OTHER1": 0})

    def test_records_missing_either_asin_are_skipped(self) -> None:
        with patch.object(ba, "run_ba_report", return_value=[{"asin": "B1"}]):
            n = ba.sync_market_basket(self.conn, date(2026, 7, 19), "stamp", set())
        self.assertEqual(n, 0)


class RepeatPurchaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ba.DDL)

    def tearDown(self) -> None:
        self.conn.close()

    def test_row_shape_and_percent_normalization(self) -> None:
        rec = {
            "asin": "B1", "orders": 100, "uniqueCustomers": 80,
            "repeatCustomersPctTotal": 0.15,
            "repeatPurchaseRevenue": {"amount": "500.00"},
            "repeatPurchaseRevenuePctTotal": 0.30,
        }
        with patch.object(ba, "run_ba_report", return_value=[rec]):
            n = ba.sync_repeat_purchase(self.conn, date(2026, 6, 1), date(2026, 6, 30), "stamp")
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT period_type, orders, repeat_customers_pct, repeat_revenue_pct "
            "FROM amazon_ba_repeat_purchase"
        ).fetchone()
        self.assertEqual(row, ("MONTH", 100, 15.0, 30.0))


class MonthBoundsTests(unittest.TestCase):
    def test_mid_year_month(self) -> None:
        first, last = ba._month_bounds("2026-06")
        self.assertEqual((first, last), (date(2026, 6, 1), date(2026, 6, 30)))

    def test_december_rolls_into_next_year(self) -> None:
        first, last = ba._month_bounds("2026-12")
        self.assertEqual((first, last), (date(2026, 12, 1), date(2026, 12, 31)))


class PriorBaSundayTests(unittest.TestCase):
    def test_returns_a_completed_sunday_saturday_week(self) -> None:
        # A Wednesday: the most recently COMPLETED Sun-Sat week ended the
        # Saturday before last Sunday.
        wk = ba._prior_ba_sunday(date(2026, 7, 22))
        self.assertEqual(wk.weekday(), 6)
        self.assertLess(wk + __import__("datetime").timedelta(days=6), date(2026, 7, 22))


class RunGrainFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ba.DDL)
        self._log_patch = patch.object(ba.warehouse_db, "log_sync")
        self._log_patch.start()
        self._now_patch = patch.object(ba.warehouse_db, "now", return_value="stamp")
        self._now_patch.start()
        self.addCleanup(self._log_patch.stop)
        self.addCleanup(self._now_patch.stop)

    def tearDown(self) -> None:
        self.conn.close()

    def test_falls_back_on_empty_week_until_a_week_yields_rows(self) -> None:
        calls = []

        def fake_sync(conn, wk, stamp, our_asins):
            calls.append(wk)
            return 5 if len(calls) == 2 else 0

        with patch.dict(ba.WEEKLY_GRAINS, {"search_catalog": ("ba_search_catalog", fake_sync)}):
            n = ba._run_grain(self.conn, "search_catalog", date(2026, 7, 19), "stamp",
                              fallback_weeks=2, our_asins=set())
        self.assertEqual(n, 5)
        self.assertEqual(len(calls), 2)

    def test_cancelled_is_caught_and_logged_not_raised(self) -> None:
        def fake_sync(conn, wk, stamp, our_asins):
            raise ba.BAReportCancelled("no data")

        with patch.dict(ba.WEEKLY_GRAINS, {"search_catalog": ("ba_search_catalog", fake_sync)}):
            n = ba._run_grain(self.conn, "search_catalog", date(2026, 7, 19), "stamp",
                              fallback_weeks=0, our_asins=set())
        self.assertEqual(n, 0)

    def test_unexpected_exception_is_caught_not_raised(self) -> None:
        def fake_sync(conn, wk, stamp, our_asins):
            raise RuntimeError("boom")

        with patch.dict(ba.WEEKLY_GRAINS, {"search_catalog": ("ba_search_catalog", fake_sync)}):
            n = ba._run_grain(self.conn, "search_catalog", date(2026, 7, 19), "stamp",
                              fallback_weeks=0, our_asins=set())
        self.assertEqual(n, 0)


class MainGrainValidationTests(unittest.TestCase):
    def test_unknown_grain_in_only_flag_exits(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        with patch("sys.argv", ["amazon_ba_sync.py", "--only", "not_a_real_grain"]), \
             patch.object(ba.warehouse_db, "init_db"), \
             patch("sqlite3.connect", return_value=sqlite3.connect(":memory:")):
            with self.assertRaises(SystemExit):
                ba.main()

    def test_week_must_be_a_sunday(self) -> None:
        with patch("sys.argv", ["amazon_ba_sync.py", "--week", "2026-07-20"]), \
             patch.object(ba.warehouse_db, "init_db"), \
             patch("sqlite3.connect", return_value=sqlite3.connect(":memory:")):
            with self.assertRaises(SystemExit):
                ba.main()


if __name__ == "__main__":
    unittest.main()
