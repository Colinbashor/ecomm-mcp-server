"""Hermetic tests for reacher_sync.py -- no network, no real warehouse.db.

Covers: schema creation, the missing-credentials guard, the read-only
transport guard (the single most safety-critical piece of this connector),
date/window helpers, the video-feed defect filter, the products/list
inert-page stopping rule, pagination termination, and the row-shaping /
ad_metrics-mirroring behavior of a representative sample of the sync_*
functions.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import reacher_sync as rs
from warehouse import db


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_every_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        rs.ensure_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in (
            "reacher_metrics_daily", "reacher_shop_gmv_daily", "reacher_gmv_max_campaign",
            "reacher_gmv_max_daily", "reacher_gmv_max_product_daily",
            "reacher_creator_product_weekly", "reacher_creator_video_weekly",
            "reacher_creator", "reacher_creator_weekly", "reacher_sample_request",
            "reacher_product_weekly", "reacher_sample_request_weekly",
            "reacher_automation_product", "reacher_sample_product_weekly",
            "reacher_video_creative", "reacher_shop_health_daily", "reacher_automation",
            "reacher_outreach_weekly", "reacher_sync_state",
        ):
            self.assertIn(t, tables)
        conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        rs.ensure_schema(conn)
        rs.ensure_schema(conn)  # must not raise
        conn.close()


class CheckRequiredEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                rs.check_required_env()
        self.assertIn("REACHER_API_KEY", str(cm.exception))
        self.assertIn("REACHER_SHOP_ID", str(cm.exception))


class ReadOnlyGuardTests(unittest.TestCase):
    """The single most important safety property of this connector: it must
    never be able to reach a write-capable surface, even if the API key it's
    given happens to have write scope."""

    def test_get_and_post_are_allowed(self) -> None:
        rs._assert_read_only("GET", "/creators/list")
        rs._assert_read_only("POST", "/metrics/timeseries")  # this API reads via POST bodies too

    def test_non_read_http_method_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            rs._assert_read_only("DELETE", "/automations/1")
        with self.assertRaises(RuntimeError):
            rs._assert_read_only("PUT", "/creators/list")

    def test_known_write_paths_are_refused_even_over_get_or_post(self) -> None:
        for path in ("/target-collabs/create", "/creator-messages/1/reply",
                     "/automations/5/archive", "/campaigns/payments/settle"):
            with self.assertRaises(RuntimeError):
                rs._assert_read_only("POST", path)

    def test_request_refuses_before_any_network_call(self) -> None:
        # _request must raise from the guard, not from a mocked HTTP layer --
        # proves the check runs first regardless of what requests.Session does.
        with patch.object(rs._SESSION, "request") as mock_request:
            with self.assertRaises(RuntimeError):
                rs._request("DELETE", "/automations/1")
            mock_request.assert_not_called()


class DateHelperTests(unittest.TestCase):
    def test_monday_rolls_back_to_the_start_of_the_week(self) -> None:
        # 2026-01-07 is a Wednesday
        self.assertEqual(rs.monday(date(2026, 1, 7)), date(2026, 1, 5))
        # Monday itself is unchanged
        self.assertEqual(rs.monday(date(2026, 1, 5)), date(2026, 1, 5))

    def test_week_starts_produces_consecutive_mondays(self) -> None:
        weeks = rs.week_starts(date(2026, 1, 1), date(2026, 1, 20))
        for w in weeks:
            self.assertEqual(w.weekday(), 0)
        for a, b in zip(weeks, weeks[1:]):
            self.assertEqual((b - a).days, 7)

    def test_day_chunks_splits_inclusive_range_and_covers_it_exactly(self) -> None:
        chunks = list(rs.day_chunks(date(2026, 1, 1), date(2026, 1, 10), size=4))
        self.assertEqual(chunks[0], (date(2026, 1, 1), date(2026, 1, 4)))
        self.assertEqual(chunks[-1][1], date(2026, 1, 10))
        # contiguous, non-overlapping
        for (_, e0), (s1, _) in zip(chunks, chunks[1:]):
            self.assertEqual((s1 - e0).days, 1)

    def test_day_chunks_short_range_is_one_chunk(self) -> None:
        chunks = list(rs.day_chunks(date(2026, 1, 1), date(2026, 1, 2), size=30))
        self.assertEqual(chunks, [(date(2026, 1, 1), date(2026, 1, 2))])


class BadPostedDateTests(unittest.TestCase):
    def test_epoch_zero_is_bad(self) -> None:
        self.assertTrue(rs._is_bad_posted_date("1970-01-01T00:00:00Z"))

    def test_far_future_is_bad(self) -> None:
        self.assertTrue(rs._is_bad_posted_date("2099-01-01T00:00:00Z"))

    def test_none_is_bad(self) -> None:
        self.assertTrue(rs._is_bad_posted_date(None))

    def test_recent_past_date_is_fine(self) -> None:
        self.assertFalse(rs._is_bad_posted_date("2024-06-15T00:00:00Z"))


class JsonifyTests(unittest.TestCase):
    def test_list_and_dict_are_json_encoded(self) -> None:
        self.assertEqual(rs._jsonify(["a", "b"]), '["a", "b"]')
        self.assertEqual(rs._jsonify({"k": 1}), '{"k": 1}')

    def test_none_passes_through(self) -> None:
        self.assertIsNone(rs._jsonify(None))

    def test_scalar_becomes_a_string(self) -> None:
        self.assertEqual(rs._jsonify(42), "42")


class ProductTransactedTests(unittest.TestCase):
    def test_all_zero_or_missing_fields_is_not_transacted(self) -> None:
        self.assertFalse(rs._product_transacted({"product_name": "Padding Row"}))
        self.assertFalse(rs._product_transacted({"gmv": 0, "units_sold": 0}))

    def test_any_nonzero_activity_field_counts(self) -> None:
        self.assertTrue(rs._product_transacted({"gmv": 12.5}))
        self.assertTrue(rs._product_transacted({"sample_count": 1}))
        self.assertTrue(rs._product_transacted({"sc_impressions": 500}))


class PaginateTests(unittest.TestCase):
    def test_walks_pages_until_total_pages_reached(self) -> None:
        pages = [
            {"data": [{"id": 1}], "pagination": {"total_pages": 2, "total_count": 2}},
            {"data": [{"id": 2}], "pagination": {"total_pages": 2, "total_count": 2}},
        ]
        with patch.object(rs, "_post", side_effect=pages):
            rows = list(rs._paginate("/creators/list", {}, "test"))
        self.assertEqual([r["id"] for r in rows], [1, 2])

    def test_stops_on_empty_page_even_if_total_pages_says_more(self) -> None:
        pages = [{"data": [], "pagination": {"total_pages": 5, "total_count": 0}}]
        with patch.object(rs, "_post", side_effect=pages):
            rows = list(rs._paginate("/creators/list", {}, "test"))
        self.assertEqual(rows, [])


class SyncShopGmvTests(unittest.TestCase):
    def test_shapes_affiliate_vs_seller_split(self) -> None:
        conn = sqlite3.connect(":memory:")
        rs.ensure_schema(conn)
        payload = {
            "currency_code": "USD",
            "series": [{
                "date": "2026-01-01", "gmv": 1000, "orders": 10, "items_sold": 12,
                "customers": 9, "aov": 100.0,
                "channels": {
                    "video": {"gmv": 600, "affiliate": 550, "seller": 50},
                    "live": {"gmv": 100, "affiliate": 80, "seller": 20},
                    "product_card": {"gmv": 300, "shop_tab": 200, "search": 100},
                },
                "traffic": {"product_impressions": 5000, "product_clicks": 400},
            }],
        }
        with patch.object(rs, "_post", return_value=payload):
            n = rs.sync_shop_gmv(conn)
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT gmv, video_affiliate_gmv, video_seller_gmv, product_card_search_gmv "
            "FROM reacher_shop_gmv_daily WHERE date='2026-01-01'").fetchone()
        self.assertEqual(row, (1000.0, 550.0, 50.0, 100.0))
        conn.close()


class SyncGmvMaxTests(unittest.TestCase):
    """The ad_metrics mirror is the highest-stakes behavior in this file: it
    feeds spend_summary/top_campaigns directly, and must only mirror days that
    actually transacted."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "warehouse.db"
        self.env = patch.dict(os.environ, {
            "REACHER_API_KEY": "key", "REACHER_SHOP_ID": "1234",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path

    def test_only_transacting_days_are_mirrored_to_ad_metrics(self) -> None:
        db.init_db()  # creates the core ad_metrics table this mirror writes into
        conn = db.connect()
        rs.ensure_schema(conn)
        campaigns_payload = {"data": [
            {"campaign_id": "C1", "campaign_name": "Full Catalog", "status": "ENABLE",
             "shopping_ads_type": "PRODUCT", "budget": 100, "roas_bid": 4, "currency": "USD"},
            {"campaign_id": "C2", "campaign_name": "Dead Husk", "status": "DISABLE",
             "shopping_ads_type": "PRODUCT", "budget": 0, "roas_bid": 0, "currency": "USD"},
        ]}
        metrics_by_campaign = {
            "C1": {"currency": "USD", "data": [
                {"date": "2026-01-01", "spend": 50.0, "impressions": 1000, "clicks": 20,
                 "orders": 5, "gross_revenue": 200.0, "cpc": 2.5, "cpm": 50, "ctr": 2.0,
                 "roas": 4.0, "ad_roi": 3.0},
                {"date": "2026-01-02", "spend": 0.0, "impressions": 0, "clicks": 0,
                 "orders": 0, "gross_revenue": 0.0, "cpc": 0, "cpm": 0, "ctr": 0,
                 "roas": 0, "ad_roi": 0},
            ]},
            "C2": {"currency": "USD", "data": []},
        }

        def fake_get(path, params=None):
            if path == "/gmv-max/campaigns":
                return campaigns_payload
            cid = path.split("/")[3]
            return metrics_by_campaign[cid]

        with patch.object(rs, "_get", side_effect=fake_get):
            n = rs.sync_gmv_max(conn, days=30)
        conn.close()

        self.assertEqual(n, 2)  # both campaign-days written to reacher_gmv_max_daily
        check = sqlite3.connect(db.DB_PATH)
        ad_rows = check.execute(
            "SELECT date, spend, campaign_id FROM ad_metrics WHERE platform='tiktok'").fetchall()
        check.close()
        # only the day that actually spent was mirrored -- the zero-spend/zero-revenue
        # day and the dead-husk campaign must NOT pad ad_metrics.
        self.assertEqual(len(ad_rows), 1)
        self.assertEqual(ad_rows[0], ("2026-01-01", 50.0, "C1"))

    def test_dry_run_never_writes_ad_metrics(self) -> None:
        db.init_db()  # creates ad_metrics so we have something to assert stayed empty
        rs.DRY_RUN = True
        self.addCleanup(setattr, rs, "DRY_RUN", False)
        conn = db.connect()
        rs.ensure_schema(conn)
        campaigns_payload = {"data": [
            {"campaign_id": "C1", "campaign_name": "Full Catalog", "status": "ENABLE",
             "shopping_ads_type": "PRODUCT", "budget": 100, "roas_bid": 4, "currency": "USD"},
        ]}
        metrics_payload = {"currency": "USD", "data": [
            {"date": "2026-01-01", "spend": 50.0, "impressions": 1000, "clicks": 20,
             "orders": 5, "gross_revenue": 200.0, "cpc": 2.5, "cpm": 50, "ctr": 2.0,
             "roas": 4.0, "ad_roi": 3.0},
        ]}

        def fake_get(path, params=None):
            return campaigns_payload if path == "/gmv-max/campaigns" else metrics_payload

        with patch.object(rs, "_get", side_effect=fake_get):
            rs.sync_gmv_max(conn, days=30)
        conn.close()
        check = sqlite3.connect(db.DB_PATH)
        count = check.execute("SELECT COUNT(*) FROM ad_metrics").fetchone()[0]
        gmv_max_count = check.execute("SELECT COUNT(*) FROM reacher_gmv_max_daily").fetchone()[0]
        check.close()
        self.assertEqual(count, 0)          # dry-run mirrored nothing to ad_metrics
        self.assertEqual(gmv_max_count, 0)  # dry-run wrote nothing to its own table either


class SyncCreatorWeeklyTests(unittest.TestCase):
    def test_filters_creators_below_min_gmv_at_the_request_layer(self) -> None:
        conn = sqlite3.connect(":memory:")
        rs.ensure_schema(conn)
        captured_bodies = []

        def fake_paginate(path, body, label):
            captured_bodies.append(body)
            return iter([
                {"creator_handle": "creator_a", "creator_id": "1", "gmv": 500.0,
                 "units_sold": 10, "order_count": 8, "est_commission": 45.0,
                 "follower_count": 1000},
            ])

        with patch.object(rs, "_paginate", side_effect=fake_paginate):
            n = rs.sync_creator_weekly(conn, [date(2026, 1, 5)], max_pages=0, min_gmv=0.01)
        self.assertEqual(n, 1)
        self.assertEqual(captured_bodies[0]["min_gmv"], 0.01)
        row = conn.execute(
            "SELECT creator_handle, gmv FROM reacher_creator_weekly").fetchone()
        self.assertEqual(row, ("creator_a", 500.0))
        conn.close()

    def test_min_gmv_zero_omits_the_filter_param(self) -> None:
        conn = sqlite3.connect(":memory:")
        rs.ensure_schema(conn)
        captured_bodies = []

        def fake_paginate(path, body, label):
            captured_bodies.append(body)
            return iter([])

        with patch.object(rs, "_paginate", side_effect=fake_paginate):
            rs.sync_creator_weekly(conn, [date(2026, 1, 5)], max_pages=0, min_gmv=0)
        self.assertNotIn("min_gmv", captured_bodies[0])
        conn.close()


class SyncSamplesTests(unittest.TestCase):
    def test_email_is_never_stored_even_if_the_api_returns_it(self) -> None:
        conn = sqlite3.connect(":memory:")
        rs.ensure_schema(conn)
        sample_row = {
            "creator_handle": "creator_a", "product_id": "P1", "product_title": "Widget",
            "status": "Sample Approved", "gmv": 10.0, "units_sold": 1, "sample_received": 1,
            "bio": "niche creator", "categories": ["fashion"], "updated_at": "2026-01-01",
            "email": "someone@example.com",  # must never end up in the table
        }
        with patch.object(rs, "_paginate", return_value=iter([sample_row])):
            n = rs.sync_samples(conn, max_pages=0, days=30)
        self.assertEqual(n, 1)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reacher_sample_request)")}
        self.assertNotIn("email", cols)
        conn.close()


class MainGuardTests(unittest.TestCase):
    def test_missing_api_key_skips_cleanly_without_raising(self) -> None:
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.argv", ["reacher_sync.py"]):
            rs.main()  # must not raise


if __name__ == "__main__":
    unittest.main()
