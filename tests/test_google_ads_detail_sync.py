"""Tests for google_ads_detail_sync.py.

Hermetic: no network, no real Google Ads credentials. Each grain's `fetch`
callable in REPORTS is monkeypatched with a fake that returns canned rows, so
these exercise the connector's own logic — chunking, per-grain insert
shaping, partial-failure isolation, and the window-grain opt-in rule —
without touching the google-ads library at all.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

import google_ads_detail_sync as gads


class ChunkingTests(unittest.TestCase):
    def test_chunks_splits_into_at_most_30_day_windows(self) -> None:
        chunks = list(gads._chunks("2026-01-01", "2026-03-05"))
        self.assertEqual(chunks, [
            ("2026-01-01", "2026-01-30"),
            ("2026-01-31", "2026-03-01"),
            ("2026-03-02", "2026-03-05"),
        ])

    def test_chunks_handles_a_single_day_range(self) -> None:
        self.assertEqual(list(gads._chunks("2026-06-01", "2026-06-01")),
                          [("2026-06-01", "2026-06-01")])


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_seven_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        gads.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {
            "google_search_terms", "google_keywords", "google_shopping_products",
            "google_paid_organic", "google_conversion_actions_daily",
            "google_campaign_devices", "google_pmax_search_themes",
        })
        conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        gads.ensure_schema(conn)
        gads.ensure_schema(conn)  # must not raise
        conn.close()


def _fake_search_terms(start, end):
    return [{
        "account_id": "111", "date": start, "campaign_id": "c1",
        "campaign_name": "Search - Non-Brand", "ad_group_id": "ag1",
        "ad_group_name": "Shoes", "search_term": "running shoes",
        "status": "NONE", "impressions": 100, "clicks": 5,
        "spend": 12.5, "conversions": 1.0, "revenue": 89.99,
    }]


def _fake_pmax_themes(start, end):
    return [{
        "account_id": "111", "window_start": start, "window_end": end,
        "campaign_id": "c9", "insight_id": "i1", "search_theme": "hiking boots",
        "impressions": 40, "clicks": 3, "conversions": 0.0, "revenue": 0.0,
    }]


class _UnclosableConnection:
    """Delegates everything to a real sqlite3.Connection except close(), so
    `run()`'s `finally: conn.close()` can't take the connection away before
    the test gets to inspect it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self._real_connect = gads.db.connect
        gads.db.connect = lambda: _UnclosableConnection(self.conn)

    def tearDown(self) -> None:
        gads.db.connect = self._real_connect
        self.conn.close()

    def test_run_writes_rows_for_a_date_keyed_grain(self) -> None:
        with patch.dict(gads.REPORTS, {
            "google_search_terms": {**gads.REPORTS["google_search_terms"],
                                     "fetch": _fake_search_terms},
        }):
            n, failures = gads.run("2026-01-01", "2026-01-01",
                                    only=["google_search_terms"])
        self.assertEqual(n, 1)
        self.assertEqual(failures, [])
        row = self.conn.execute(
            "SELECT search_term, impressions, spend FROM google_search_terms"
        ).fetchone()
        self.assertEqual(row, ("running shoes", 100, 12.5))

    def test_window_grain_is_skipped_by_a_plain_run(self) -> None:
        # Every date-keyed grain gets a harmless empty fetch here — the point
        # of this test is only that the window grain's fetch is never called
        # at all on a plain (unfiltered) run, not the date-keyed grains'
        # behavior.
        patched = {name: {**cfg, "fetch": lambda s, e: []}
                   for name, cfg in gads.REPORTS.items()}
        patched["google_pmax_search_themes"] = {
            **gads.REPORTS["google_pmax_search_themes"], "fetch": _fake_pmax_themes}
        with patch.dict(gads.REPORTS, patched):
            n, failures = gads.run("2026-01-01", "2026-01-31", only=None)
        self.assertEqual(failures, [])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM google_pmax_search_themes").fetchone()[0]
        self.assertEqual(count, 0)

    def test_window_grain_runs_when_named_explicitly(self) -> None:
        with patch.dict(gads.REPORTS, {
            "google_pmax_search_themes": {**gads.REPORTS["google_pmax_search_themes"],
                                           "fetch": _fake_pmax_themes},
        }):
            n, failures = gads.run("2026-06-01", "2026-06-30",
                                    only=["google_pmax_search_themes"])
        self.assertEqual(n, 1)
        self.assertEqual(failures, [])
        row = self.conn.execute(
            "SELECT search_theme, window_start, window_end FROM google_pmax_search_themes"
        ).fetchone()
        self.assertEqual(row, ("hiking boots", "2026-06-01", "2026-06-30"))

    def test_one_grain_failing_does_not_stop_the_others(self) -> None:
        def _broken(start, end):
            raise RuntimeError("simulated API failure")

        with patch.dict(gads.REPORTS, {
            "google_search_terms": {**gads.REPORTS["google_search_terms"], "fetch": _broken},
            "google_keywords": {**gads.REPORTS["google_keywords"], "fetch": lambda s, e: [{
                "account_id": "111", "date": s, "campaign_id": "c1",
                "campaign_name": "Search", "ad_group_id": "ag1", "ad_group_name": "Shoes",
                "criterion_id": "kw1", "keyword": "running shoes", "match_type": "BROAD",
                "quality_score": 7, "status": "ENABLED", "impressions": 50, "clicks": 2,
                "spend": 6.0, "conversions": 0.0, "revenue": 0.0,
            }]},
        }):
            n, failures = gads.run("2026-01-01", "2026-01-01",
                                    only=["google_search_terms", "google_keywords"])
        self.assertEqual(n, 1)  # the keywords grain still landed
        self.assertEqual(len(failures), 1)
        self.assertIn("google_search_terms", failures[0])

    def test_run_raises_when_every_requested_grain_fails(self) -> None:
        def _broken(start, end):
            raise RuntimeError("simulated API failure")

        with patch.dict(gads.REPORTS, {
            "google_search_terms": {**gads.REPORTS["google_search_terms"], "fetch": _broken},
        }):
            with self.assertRaises(RuntimeError):
                gads.run("2026-01-01", "2026-01-01", only=["google_search_terms"])


if __name__ == "__main__":
    unittest.main()
