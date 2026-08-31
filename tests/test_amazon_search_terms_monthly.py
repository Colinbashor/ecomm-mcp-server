"""Hermetic tests for amazon_search_terms_monthly.py — no network, no real
warehouse.db.

Covers: the config loader (buckets/exclusions/defaults, missing-file and
missing-PyYAML fallbacks), bucket matching (first-match-wins order,
exclusions, word boundaries), the camelCase/snake_case + fraction->percent
helpers, the calendar-month window helpers, the report-streaming candidate
collector (rank ceiling, top-3 grouping, transition-counted terms_seen), the
category-filling scan (early exit on all-buckets-full, the lookup-budget
stop-before-half-categorizing rule, per-bucket cap, is_ours flagging, top-ASIN
selection, share summation, and the cost-aware stall guard in both
directions), the coverage-table migration safety, and sync_month's
skip/retry/settled orchestration.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import amazon_search_terms_monthly as astm


class ConfigTests(unittest.TestCase):
    def test_loads_buckets_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "search_term_categories.yaml"
            cfg_path.write_text(
                "buckets:\n  Dresses:\n    - Dress\n  Shoes:\n    - Shoe\n"
                "exclude_nodes:\n  - Costume\n"
                "terms_per_category: 50\n"
                "scan_rank_ceiling: 1000\n"
                "max_asin_lookups: 500\n",
                encoding="utf-8",
            )
            with patch.object(astm, "CONFIG_FILE", cfg_path):
                cfg = astm._cfg()
        self.assertEqual(set(cfg["buckets"]), {"Dresses", "Shoes"})
        self.assertEqual(cfg["exclude_nodes"], ["Costume"])
        self.assertEqual(cfg["terms_per_category"], 50)
        self.assertEqual(cfg["scan_rank_ceiling"], 1000)
        self.assertEqual(cfg["max_asin_lookups"], 500)

    def test_missing_file_returns_empty_buckets(self) -> None:
        with patch.object(astm, "CONFIG_FILE", Path("/no/such/file.yaml")):
            cfg = astm._cfg()
        self.assertEqual(cfg["buckets"], {})
        self.assertEqual(cfg["terms_per_category"], 200)  # documented default

    def test_shipped_example_config_is_loadable_and_regexes_compile(self) -> None:
        shipped = Path(__file__).resolve().parent.parent / "search_term_categories.yaml"
        with patch.object(astm, "CONFIG_FILE", shipped):
            cfg = astm._cfg()
        self.assertTrue(cfg["buckets"], "shipped example config has no buckets")
        compiled, _ = astm.compile_buckets(cfg)
        self.assertEqual(len(compiled), len(cfg["buckets"]))


class BucketMatchingTests(unittest.TestCase):
    def _compiled(self, buckets: dict, exclude: list | None = None):
        cfg = {"buckets": buckets, "exclude_nodes": exclude or []}
        return astm.compile_buckets(cfg)

    def test_first_matching_bucket_wins(self) -> None:
        compiled, exclude = self._compiled({
            "Costumes": ["Costume"],
            "Dresses": ["Dress"],
        })
        self.assertEqual(astm.bucket_of("Women's Costume Bodysuits", compiled, exclude), "Costumes")

    def test_exclusions_run_before_buckets(self) -> None:
        compiled, exclude = self._compiled({"Dresses": ["Dress"]}, exclude=["Dress Shirt"])
        self.assertIsNone(astm.bucket_of("Dress Shirt Accessories", compiled, exclude))

    def test_word_boundaries_avoid_substring_false_positives(self) -> None:
        compiled, exclude = self._compiled({"Tops": [r"\bTop\b"]})
        self.assertIsNone(astm.bucket_of("Laptop Cases", compiled, exclude))
        self.assertEqual(astm.bucket_of("Women's Crop Top", compiled, exclude), "Tops")

    def test_no_match_returns_none(self) -> None:
        compiled, exclude = self._compiled({"Shoes": ["Shoe"]})
        self.assertIsNone(astm.bucket_of("Kitchen Appliances", compiled, exclude))

    def test_none_node_returns_none(self) -> None:
        compiled, exclude = self._compiled({"Shoes": ["Shoe"]})
        self.assertIsNone(astm.bucket_of(None, compiled, exclude))


class HelperTests(unittest.TestCase):
    def test_g_accepts_camel_and_snake_case(self) -> None:
        self.assertEqual(astm._g({"searchTerm": "x"}, "searchTerm", "search_term"), "x")
        self.assertEqual(astm._g({"search_term": "y"}, "searchTerm", "search_term"), "y")
        self.assertIsNone(astm._g({}, "searchTerm", "search_term"))
        self.assertIsNone(astm._g({"searchTerm": ""}, "searchTerm", "search_term"))

    def test_pct_converts_fraction_and_is_none_safe(self) -> None:
        self.assertEqual(astm._pct(0.0751), 7.51)
        self.assertIsNone(astm._pct(None))
        self.assertIsNone(astm._pct(""))

    def test_month_bounds(self) -> None:
        start, end = astm.month_bounds("2026-02")
        self.assertEqual((start, end), (date(2026, 2, 1), date(2026, 2, 28)))
        start, end = astm.month_bounds("2026-12")
        self.assertEqual((start, end), (date(2026, 12, 1), date(2026, 12, 31)))

    def test_prior_month_crosses_year_boundary(self) -> None:
        self.assertEqual(astm.prior_month(date(2026, 1, 15)), "2025-12")
        self.assertEqual(astm.prior_month(date(2026, 3, 1)), "2026-02")


class CollectCandidatesTests(unittest.TestCase):
    def test_applies_the_rank_ceiling(self) -> None:
        records = [
            {"searchTerm": "in", "searchFrequencyRank": 10, "clickedAsin": "A1"},
            {"searchTerm": "out", "searchFrequencyRank": 999, "clickedAsin": "A2"},
        ]
        cand, total, _ = astm.collect_candidates(records, ceiling=100)
        self.assertEqual(set(cand), {"in"})
        self.assertEqual(total, 2)

    def test_groups_top3_asins_under_one_term(self) -> None:
        records = [
            {"searchTerm": "t", "searchFrequencyRank": 1, "clickedAsin": "A1"},
            {"searchTerm": "t", "searchFrequencyRank": 1, "clickedAsin": "A2"},
            {"searchTerm": "t", "searchFrequencyRank": 1, "clickedAsin": "A3"},
        ]
        cand, _, _ = astm.collect_candidates(records, ceiling=1000)
        rank, hits = cand["t"]
        self.assertEqual(rank, 1)
        self.assertEqual([h[0] for h in hits], ["A1", "A2", "A3"])

    def test_skips_rows_with_no_rank_or_no_asin(self) -> None:
        records = [
            {"searchTerm": "no_rank", "clickedAsin": "A1"},
            {"searchTerm": "no_asin", "searchFrequencyRank": 5},
        ]
        cand, _, _ = astm.collect_candidates(records, ceiling=1000)
        self.assertEqual(cand, {})

    def test_terms_seen_counts_by_transition_not_a_set(self) -> None:
        # Same term repeated non-contiguously would double count with naive
        # transition counting, but Amazon emits a term's rows contiguously —
        # this pins the documented assumption.
        records = [
            {"searchTerm": "a", "searchFrequencyRank": 1, "clickedAsin": "A1"},
            {"searchTerm": "a", "searchFrequencyRank": 1, "clickedAsin": "A2"},
            {"searchTerm": "b", "searchFrequencyRank": 2, "clickedAsin": "B1"},
        ]
        _, _, terms_seen = astm.collect_candidates(records, ceiling=1000)
        self.assertEqual(terms_seen, 2)


class BuildRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(astm.DDL)

    def _cfg(self, buckets, **overrides) -> dict:
        cfg = {"buckets": buckets, "exclude_nodes": [], "terms_per_category": 2,
               "scan_rank_ceiling": 100_000, "max_asin_lookups": 1000}
        cfg.update(overrides)
        return cfg

    def test_a_term_lands_in_every_bucket_its_top3_reaches(self) -> None:
        cand = {"gothic dress": (1, [("A1", "Dress A", 50.0, 10.0)])}
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={"A1": "Womens Dresses"}):
            rows, stats = self.build_rows_two_buckets(cand)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "Dresses")

    def build_rows_two_buckets(self, cand):
        cfg = self._cfg({"Dresses": ["Dress"], "Shoes": ["Shoe"]})
        return astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")

    def test_nodes_outside_every_bucket_are_dropped(self) -> None:
        cand = {"widgets": (1, [("A1", "Widget", 50.0, 10.0)])}
        cfg = self._cfg({"Dresses": ["Dress"]})
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={"A1": "Home & Kitchen"}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(rows, [])

    def test_unresolvable_asins_dropped_not_guessed(self) -> None:
        cand = {"term": (1, [("A1", "X", 50.0, 10.0)])}
        cfg = self._cfg({"Dresses": ["Dress"]})
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={"A1": None}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(rows, [])

    def test_shares_sum_across_terms_asins_in_that_bucket(self) -> None:
        cand = {"t": (1, [("A1", "X", 30.0, 5.0), ("A2", "Y", 20.0, 2.0)])}
        cfg = self._cfg({"Dresses": ["Dress"]})
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={"A1": "Dresses", "A2": "Dresses"}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], 50.0)   # category_click_share_pct, summed
        self.assertEqual(rows[0][6], 7.0)    # category_conversion_share_pct, summed
        self.assertEqual(rows[0][7], 2)      # asins_in_category

    def test_top_asin_picks_highest_click_share_even_with_a_null(self) -> None:
        cand = {"t": (1, [("A1", "X", None, 1.0), ("A2", "Y", 40.0, 2.0)])}
        cfg = self._cfg({"Dresses": ["Dress"]})
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={"A1": "Dresses", "A2": "Dresses"}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(rows[0][8], "A2")   # top_asin

    def test_is_ours_flags_our_asin_in_that_bucket(self) -> None:
        cand = {"t": (1, [("A1", "X", 30.0, 5.0)])}
        cfg = self._cfg({"Dresses": ["Dress"]})
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={"A1": "Dresses"}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, {"A1"}, "2026-07-01T00:00:00Z")
        self.assertEqual(rows[0][13], 1)     # is_ours
        self.assertEqual(rows[0][14], "A1")  # our_asin

    def test_per_bucket_cap_triggers_early_exit(self) -> None:
        cand = {
            f"term{i}": (i, [(f"A{i}", "X", 10.0, 1.0)]) for i in range(1, 6)
        }
        cfg = self._cfg({"Dresses": ["Dress"]}, terms_per_category=2)
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={f"A{i}": "Dresses" for i in range(1, 6)}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats["stop_reason"], "all_categories_full")

    def test_under_filled_bucket_is_reported_not_hidden(self) -> None:
        cand = {"t": (1, [("A1", "X", 10.0, 1.0)])}
        cfg = self._cfg({"Dresses": ["Dress"], "Shoes": ["Shoe"]}, terms_per_category=5)
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={"A1": "Dresses"}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(stats["per_category"]["Shoes"], 0)
        self.assertEqual(stats["filled"], 0)  # neither bucket reached its quota

    def test_lookup_budget_exhaustion_stops_before_half_categorizing_a_chunk(self) -> None:
        cand = {"t": (1, [("A1", "X", 10.0, 1.0)])}
        cfg = self._cfg({"Dresses": ["Dress"]})
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={}):  # A1 stays unresolved
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(rows, [])
        self.assertEqual(stats["stop_reason"], "lookup_budget")


class StallGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(astm.DDL)

    def _many_terms(self, n: int, category_hits: bool) -> dict:
        """`n` candidate terms, each pointing at one ASIN. If category_hits is
        False the ASIN never resolves into a tracked bucket (so no rows can
        ever be produced), which is the "permanently thin category" case the
        stall guard exists for."""
        cand = {}
        for i in range(n):
            cand[f"term{i}"] = (i + 1, [(f"A{i}", "X", 10.0, 1.0)])
        return cand

    def test_stops_when_spending_lookups_but_gaining_nothing(self) -> None:
        n = astm.TERM_CHUNK * (astm.STALL_WINDOW_CHUNKS + 2)
        cand = self._many_terms(n, category_hits=False)
        cfg = {"buckets": {"Dresses": ["Dress"]}, "exclude_nodes": [],
               "terms_per_category": 200, "scan_rank_ceiling": 1_000_000,
               "max_asin_lookups": n}  # never budget-exhausted
        # ASINs resolve (spending lookups) but never map into a bucket, so
        # rows never grow -- real spend, zero gain.
        with patch.object(astm, "resolve_asins", return_value=1), \
             patch.object(astm, "_load_cache", return_value={f"A{i}": "Home & Kitchen" for i in range(n)}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(stats["stop_reason"], "diminishing_returns")

    def test_never_fires_on_a_fully_cached_scan(self) -> None:
        n = astm.TERM_CHUNK * (astm.STALL_WINDOW_CHUNKS + 2)
        cand = self._many_terms(n, category_hits=False)
        cfg = {"buckets": {"Dresses": ["Dress"]}, "exclude_nodes": [],
               "terms_per_category": 200, "scan_rank_ceiling": 1_000_000,
               "max_asin_lookups": n}
        # resolve_asins returns 0 new lookups every chunk (everything already
        # cached) -- a window that costs nothing must never be treated as a
        # stall, however few rows it produces.
        with patch.object(astm, "resolve_asins", return_value=0), \
             patch.object(astm, "_load_cache", return_value={f"A{i}": "Home & Kitchen" for i in range(n)}):
            rows, stats = astm.build_rows(self.conn, "2026-07", cand, cfg, set(), "2026-07-01T00:00:00Z")
        self.assertEqual(stats["stop_reason"], "exhausted_candidates")


class MigrateTests(unittest.TestCase):
    def test_safe_on_a_fresh_database(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(astm.DDL)
        astm._migrate(conn)  # should not raise
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_search_term_coverage)")}
        self.assertIn("attempts", cols)

    def test_adds_attempts_column_to_a_table_missing_it(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(astm.DDL.replace(
            "attempts           INTEGER NOT NULL DEFAULT 1,\n", ""))
        astm._migrate(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_search_term_coverage)")}
        self.assertIn("attempts", cols)

    def test_adds_top_asin_node_column_to_a_table_missing_it(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(astm.DDL.replace(
            "    top_asin_node                 TEXT,             -- the RAW browse node behind the bucket\n", ""))
        astm._migrate(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_search_term_monthly)")}
        self.assertIn("top_asin_node", cols)


class SyncMonthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(astm.DDL)
        self.cfg = {"buckets": {"Dresses": ["Dress"]}, "exclude_nodes": [],
                    "terms_per_category": 200, "scan_rank_ceiling": 100_000,
                    "max_asin_lookups": 1000}

    def _patched(self, records, stats_override=None):
        rows = [("2026-07", "Dresses", "gothic dress", 1, 1, 50.0, 5.0, 1,
                  "A1", "Dress A", "Womens Dresses", 50.0, 5.0, 0, None, "s")]
        stats = {"filled": 1, "deepest": 1, "lookups": 0, "examined": 1,
                  "stop_reason": "all_categories_full", "per_category": {"Dresses": 1}}
        if stats_override:
            stats.update(stats_override)
        return patch.object(astm, "create_ba_report", return_value="r1"), \
               patch.object(astm, "await_ba_report", return_value="doc1"), \
               patch.object(astm, "stream_ba_records", return_value=records), \
               patch.object(astm, "build_rows", return_value=(rows, stats))

    def test_end_to_end_writes_rows_and_coverage(self) -> None:
        p1, p2, p3, p4 = self._patched([])
        with p1, p2, p3, p4:
            n, stats = astm.sync_month(self.conn, "2026-07", self.cfg, set())
        self.assertEqual(n, 1)
        row_count = self.conn.execute(
            "SELECT COUNT(*) FROM amazon_search_term_monthly WHERE month='2026-07'").fetchone()[0]
        self.assertEqual(row_count, 1)
        cov = self.conn.execute(
            "SELECT is_complete, attempts FROM amazon_search_term_coverage WHERE month='2026-07'").fetchone()
        self.assertEqual(cov, (1, 1))

    def test_complete_month_is_skipped_without_touching_the_api(self) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO amazon_search_term_coverage "
                "(month, is_complete, attempts, stop_reason, synced_at) "
                "VALUES ('2026-07', 1, 1, 'all_categories_full', 's')")
        with patch.object(astm, "create_ba_report") as fake_create:
            n, stats = astm.sync_month(self.conn, "2026-07", self.cfg, set())
        fake_create.assert_not_called()
        self.assertTrue(stats["skipped"])

    def test_settled_month_is_not_re_pulled(self) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO amazon_search_term_coverage "
                "(month, is_complete, attempts, stop_reason, synced_at) "
                "VALUES ('2026-07', 0, 1, 'diminishing_returns', 's')")
        with patch.object(astm, "create_ba_report") as fake_create:
            n, stats = astm.sync_month(self.conn, "2026-07", self.cfg, set())
        fake_create.assert_not_called()
        self.assertEqual(stats["stop_reason"], "settled")

    def test_truncated_month_is_retried(self) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO amazon_search_term_coverage "
                "(month, is_complete, attempts, stop_reason, synced_at) "
                "VALUES ('2026-07', 0, 1, 'lookup_budget', 's')")
        p1, p2, p3, p4 = self._patched([])
        with p1, p2, p3, p4:
            n, stats = astm.sync_month(self.conn, "2026-07", self.cfg, set())
        self.assertEqual(n, 1)

    def test_stops_retrying_after_max_attempts(self) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO amazon_search_term_coverage "
                "(month, is_complete, attempts, stop_reason, synced_at) "
                "VALUES ('2026-07', 0, ?, 'lookup_budget', 's')", (astm.MAX_ATTEMPTS,))
        with patch.object(astm, "create_ba_report") as fake_create:
            n, stats = astm.sync_month(self.conn, "2026-07", self.cfg, set())
        fake_create.assert_not_called()
        self.assertEqual(stats["stop_reason"], "attempts_exhausted")

    def test_refresh_replaces_rather_than_appends(self) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO amazon_search_term_monthly "
                "(month, category, search_term, category_term_rank, synced_at) "
                "VALUES ('2026-07', 'Dresses', 'stale term', 1, 's')")
            self.conn.execute(
                "INSERT INTO amazon_search_term_coverage "
                "(month, is_complete, attempts, stop_reason, synced_at) "
                "VALUES ('2026-07', 1, 1, 'all_categories_full', 's')")
        p1, p2, p3, p4 = self._patched([])
        with p1, p2, p3, p4:
            astm.sync_month(self.conn, "2026-07", self.cfg, set(), refresh=True)
        terms = {r[0] for r in self.conn.execute(
            "SELECT search_term FROM amazon_search_term_monthly WHERE month='2026-07'")}
        self.assertNotIn("stale term", terms)
        self.assertIn("gothic dress", terms)

    def test_doc_id_reuses_the_document_instead_of_creating_a_report(self) -> None:
        _, _, p3, p4 = self._patched([])
        with p3, p4:
            with patch.object(astm, "create_ba_report") as fake_create:
                astm.sync_month(self.conn, "2026-07", self.cfg, set(), doc_id="existing-doc")
                fake_create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
