"""Tests for algolia_sync.py.

Hermetic: no network, no real Algolia credentials, no warehouse.db. The tests
that matter here are the ones pinning the traps documented in the module
docstring, because every one of them is a mistake that would produce a
plausible-looking wrong number rather than a loud error.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import unittest
from unittest import mock

import algolia_sync as alg


class RetentionTests(unittest.TestCase):
    """Analytics hard-400s past the retention window (trap 3), so the window
    must be clamped before the request rather than after the failure."""

    def test_start_inside_retention_is_untouched(self):
        end = dt.date.today() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=2)
        got, clamped = alg.clamp_to_retention(start, end)
        self.assertEqual(got, start)
        self.assertFalse(clamped)

    def test_start_beyond_retention_is_clamped_and_reported(self):
        end = dt.date.today()
        start = end - dt.timedelta(days=400)
        got, clamped = alg.clamp_to_retention(start, end)
        self.assertTrue(clamped, "a silently unclamped start would just 400")
        self.assertGreater(got, start)
        self.assertLessEqual((dt.date.today() - got).days, alg.RETENTION_DAYS)

    def test_clamp_stays_strictly_inside_the_retention_wall(self):
        """One day of slack — a run straddling midnight UTC must not 400 on
        its own start date."""
        self.assertLess(alg.RETENTION_DAYS, 90)


class RevenueAnalyticsGateTests(unittest.TestCase):
    """trap 1 + the cost of ignoring it. revenueAnalytics roughly doubles
    page latency to return columns that are guaranteed zero on a storefront
    that sends no purchase events."""

    def test_gate_is_false_when_no_purchase_events(self):
        with mock.patch.object(alg, "analytics_get", return_value={"purchaseCount": 0}):
            self.assertFalse(alg.purchase_events_present([dt.date(2026, 8, 23)]))

    def test_gate_self_enables_when_purchases_appear(self):
        """The whole point of a gate rather than a hardcoded False: capture
        must start on its own the day purchase events get wired."""
        with mock.patch.object(alg, "analytics_get", return_value={"purchaseCount": 42}):
            self.assertTrue(alg.purchase_events_present([dt.date(2026, 8, 23)]))

    def test_gate_fails_closed_on_error(self):
        with mock.patch.object(alg, "analytics_get", side_effect=alg.AlgoliaError("boom")):
            self.assertFalse(alg.purchase_events_present([dt.date(2026, 8, 23)]))

    def test_revenue_param_omitted_when_gate_is_false(self):
        seen = {}

        def fake(path, params):
            seen.update(params)
            return {"hits": []}

        with mock.patch.object(alg, "analytics_get", side_effect=fake):
            alg.fetch_hits(dt.date(2026, 8, 23), revenue=False)
        self.assertNotIn("revenueAnalytics", seen)
        self.assertEqual(seen.get("clickAnalytics"), "true")

    def test_revenue_param_sent_when_gate_is_true(self):
        seen = {}

        def fake(path, params):
            seen.update(params)
            return {"hits": []}

        with mock.patch.object(alg, "analytics_get", side_effect=fake):
            alg.fetch_hits(dt.date(2026, 8, 23), revenue=True)
        self.assertEqual(seen.get("revenueAnalytics"), "true")


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(alg.DDL)

    def tearDown(self):
        self.conn.close()

    def _cols(self, table: str) -> list[str]:
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def test_no_column_is_called_conversions(self):
        """trap 1. Algolia's 'conversion' can be an add-to-cart in disguise.
        A column named `conversions` invites someone to read it as purchases,
        which is the single most expensive mistake available here."""
        for table in ("algolia_product_engagement", "algolia_daily"):
            cols = self._cols(table)
            self.assertNotIn("conversions", cols)
            self.assertNotIn("conversion_count", cols)
            self.assertTrue(any(c.startswith("add_to_cart") for c in cols),
                            f"{table} should name the measure add_to_cart_*")

    def test_purchase_columns_exist_even_though_often_zero(self):
        """So capture self-enables rather than needing a migration later."""
        cols = self._cols("algolia_product_engagement")
        self.assertIn("purchase_count", cols)
        self.assertIn("purchase_rate", cols)

    def test_placement_carries_collection_size(self):
        """Position alone is meaningless across collections of very different
        sizes — the percentile bucket needs the denominator recorded at
        snapshot time, because the grid length changes day to day."""
        self.assertIn("collection_size", self._cols("collection_placement"))

    def test_placement_pk_prevents_duplicate_positions(self):
        self.conn.execute(
            "INSERT INTO collection_placement (snapshot_date,collection,position,synced_at) "
            "VALUES ('2026-08-25','dresses',1,'x')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO collection_placement (snapshot_date,collection,position,synced_at) "
                "VALUES ('2026-08-25','dresses',1,'y')")

    def test_ids_are_text_not_integer(self):
        """A big product/variant id can silently overflow or mismatch across
        a TEXT/INTEGER comparison depending on how a consumer's own catalog
        table stores it — TEXT throughout keeps the join predictable."""
        info = {r[1]: r[2] for r in self.conn.execute(
            "PRAGMA table_info(collection_placement)")}
        self.assertEqual(info["product_id"], "TEXT")
        self.assertEqual(info["object_id"], "TEXT")


class PlacementOrderTests(unittest.TestCase):
    """trap 5: a record's own `position` field can be an attribute (e.g. a
    variant's ordinal within its product), NOT its grid rank. Storing it
    would look entirely plausible and be wrong."""

    def test_grid_position_is_enumeration_order_not_the_record_field(self):
        hits = [
            {"objectID": "111", "id": "9001", "title": "first",  "position": 7},
            {"objectID": "222", "id": "9002", "title": "second", "position": 3},
            {"objectID": "333", "id": "9003", "title": "third",  "position": 3},
        ]
        conn = sqlite3.connect(":memory:")
        conn.executescript(alg.DDL)
        with mock.patch.object(alg, "fetch_collection", return_value=hits):
            total, failed = alg.store_placement(conn, ["dresses"], "stamp")
        self.assertEqual(total, 3)
        self.assertEqual(failed, [])
        rows = conn.execute(
            "SELECT position, title, collection_size FROM collection_placement "
            "ORDER BY position").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 2, 3])
        self.assertEqual([r[1] for r in rows], ["first", "second", "third"])
        self.assertEqual({r[2] for r in rows}, {3}, "collection_size = grid length")
        conn.close()

    def test_rerun_replaces_rather_than_appends_and_shrinks_cleanly(self):
        """The grid shrinks as well as grows; stale deep positions must go or
        a delisted product keeps a slot it no longer holds."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(alg.DDL)
        big = [{"objectID": str(i), "id": str(i), "title": f"t{i}"} for i in range(5)]
        small = [{"objectID": "0", "id": "0", "title": "t0"}]
        with mock.patch.object(alg, "fetch_collection", return_value=big):
            alg.store_placement(conn, ["dresses"], "s")
        with mock.patch.object(alg, "fetch_collection", return_value=small):
            alg.store_placement(conn, ["dresses"], "s")
        n = conn.execute("SELECT COUNT(*) FROM collection_placement").fetchone()[0]
        self.assertEqual(n, 1)
        conn.close()

    def test_one_failed_collection_does_not_lose_the_others(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(alg.DDL)

        def flaky(handle):
            if handle == "bad":
                raise alg.AlgoliaTransient("down")
            return [{"objectID": "1", "id": "1", "title": "ok"}]

        with mock.patch.object(alg, "fetch_collection", side_effect=flaky):
            total, failed = alg.store_placement(conn, ["good", "bad", "also_good"], "s")
        self.assertEqual(total, 2)
        self.assertEqual(failed, ["bad"])
        conn.close()

    def test_no_tracked_collections_skips_cleanly_rather_than_erroring(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(alg.DDL)
        total, failed = alg.store_placement(conn, [], "s")
        self.assertEqual((total, failed), (0, []))
        conn.close()


class RetryClassificationTests(unittest.TestCase):
    def test_permanent_4xx_is_not_retried(self):
        """A bad date or bad index must fail immediately, not burn several
        backoffs — an out-of-retention 400 is the common case."""
        self.assertNotIn(400, alg.TRANSIENT_STATUS)
        self.assertNotIn(403, alg.TRANSIENT_STATUS)
        self.assertNotIn(422, alg.TRANSIENT_STATUS)

    def test_throttle_and_gateway_errors_are_retried(self):
        for code in (429, 500, 502, 503, 504):
            self.assertIn(code, alg.TRANSIENT_STATUS)


class ConfigTests(unittest.TestCase):
    def test_tracked_collections_come_from_the_config_file(self):
        handles = alg.load_collections()
        self.assertTrue(handles, "algolia_collections.yaml ships with placeholder examples")

    def test_missing_config_file_returns_default_not_an_error(self):
        with mock.patch.object(alg, "CONFIG_FILE", alg.ROOT / "does-not-exist.yaml"):
            self.assertEqual(alg.load_collections(), alg.DEFAULT_COLLECTIONS)

    def test_analytics_key_is_not_hardcoded(self):
        """The search key is commonly public (many storefronts ship it to
        every browser); the analytics key is a privileged dashboard key and
        must live only in .env.

        The live value is read from the environment rather than written here
        — this test file is committed, so pasting a real key in to assert its
        absence would put it in the repo, which is the exact thing being
        guarded against.
        """
        key = os.environ.get("ALGOLIA_ANALYTICS_KEY")
        if not key:
            self.skipTest("ALGOLIA_ANALYTICS_KEY not set in this environment")
        source = (alg.__file__).replace(".pyc", ".py")
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(key, text,
                         "the analytics key must never be committed to source")


class SearchesPagingTests(unittest.TestCase):
    """/2/searches offset paging is not guaranteed to be a clean partition —
    consecutive pages can share query strings. Without a dedupe the same
    query is inserted twice and the reported count overstates what landed."""

    def test_overlapping_pages_are_deduped(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(alg.DDL)
        page0 = [{"search": f"q{i}", "count": 100 - i} for i in range(alg.ANALYTICS_PAGE_LIMIT)]
        # page 1 repeats a few of page 0's queries, as a real API can.
        page1 = [{"search": "q0", "count": 1}, {"search": "q1", "count": 1},
                 {"search": "q2", "count": 1}, {"search": "zzz", "count": 1}]
        calls = iter([page0, page1])

        def fake(path, params):
            return {"searches": next(calls, [])}

        with mock.patch.object(alg, "analytics_get", side_effect=fake):
            total = alg.store_searches(conn, [dt.date(2026, 8, 24)], "s")
        stored = conn.execute("SELECT COUNT(*) FROM algolia_searches").fetchone()[0]
        conn.close()
        self.assertEqual(total, stored,
                         "reported row count must equal what actually landed")
        self.assertEqual(stored, alg.ANALYTICS_PAGE_LIMIT + 1)

    def test_first_sighting_wins(self):
        """Results are volume-ordered, so the first page carries the real count."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(alg.DDL)
        pages = iter([[{"search": "boots", "count": 5204}],
                      [{"search": "boots", "count": 2}]])

        def fake(path, params):
            return {"searches": next(pages, [])}

        with mock.patch.object(alg, "analytics_get", side_effect=fake):
            alg.store_searches(conn, [dt.date(2026, 8, 24)], "s")
        got = conn.execute(
            "SELECT search_count FROM algolia_searches WHERE query='boots'").fetchone()[0]
        conn.close()
        self.assertEqual(got, 5204)

    def test_empty_query_is_kept(self):
        """The empty query is typically the browse grid itself, not nothing —
        dropping it as falsy would discard the single largest row."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(alg.DDL)
        with mock.patch.object(alg, "analytics_get",
                               return_value={"searches": [{"search": "", "count": 1400170}]}):
            alg.store_searches(conn, [dt.date(2026, 8, 24)], "s")
        got = conn.execute(
            "SELECT search_count FROM algolia_searches WHERE query=''").fetchone()
        conn.close()
        self.assertIsNotNone(got, "the browse-grid row must not be dropped as falsy")
        self.assertEqual(got[0], 1400170)


class RequireEnvTests(unittest.TestCase):
    def test_missing_vars_raise_systemexit(self):
        saved = {k: os.environ.pop(k, None) for k in alg.REQUIRED_ENV}
        try:
            with self.assertRaises(SystemExit) as cm:
                alg.check_required_env()
            for k in alg.REQUIRED_ENV:
                self.assertIn(k, str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
