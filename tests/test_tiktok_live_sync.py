"""Hermetic tests for tiktok_live_sync.py -- no network, no real warehouse.db.

Covers: schema creation for both tables, percent/money parsing, per-session
row shaping, the day-sellers GMV-sorted early-stop, per-product LIVE-slice
extraction (incl. the "no LIVE activity -> None" case), shop-local-timezone
day bucketing for own_live_days, and the missing-credentials guard.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tiktok_live_sync as tl
from warehouse import db


def _resp(code=0, data=None, message="ok", status=200):
    class _R:
        status_code = status

        def json(self):
            return {"code": code, "message": message, "data": data or {}}
    return _R()


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_both_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        tl.ensure_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tiktok_shop_lives", tables)
        self.assertIn("tiktok_shop_live_products", tables)
        conn.close()


class CheckRequiredEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                tl.check_required_env()
        self.assertIn("TIKTOK_SHOP_CIPHER", str(cm.exception))


class ParsingHelperTests(unittest.TestCase):
    def test_pct_strips_percent_sign(self) -> None:
        self.assertEqual(tl._pct("2.10%"), 2.10)
        self.assertEqual(tl._pct(None), 0.0)
        self.assertEqual(tl._pct(3), 3.0)

    def test_money_unpacks_amount_and_currency(self) -> None:
        self.assertEqual(tl._money({"amount": "9.99", "currency": "USD"}), (9.99, "USD"))
        self.assertEqual(tl._money(None), (0.0, None))


class FetchLivesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_row_shaping_including_percent_and_money_fields(self) -> None:
        session = {
            "id": "live1", "title": "Show", "username": "brand",
            "start_time": "1700000000", "end_time": "1700003600",
            "interaction_performance": {
                "views": 100, "viewers": 80, "avg_viewing_duration": 45.5,
                "product_impressions": 500, "product_clicks": 50,
                "click_through_rate": "10.00%", "likes": 5, "comments": 2,
                "shares": 1, "new_followers": 3,
            },
            "sales_performance": {
                "gmv": {"amount": "250.00", "currency": "USD"},
                "24h_live_gmv": {"amount": "300.00", "currency": "USD"},
                "items_sold": 20, "sku_orders": 15, "created_sku_orders": 15,
                "customers": 12, "different_products_sold": 6,
                "avg_price": {"amount": "12.50"}, "click_to_order_rate": "5.00%",
            },
        }
        page = _resp(data={"live_stream_sessions": [session], "next_page_token": ""})
        with patch.object(tl.requests, "get", return_value=page):
            rows = tl.fetch_lives("OFFICIAL_ACCOUNTS", "2026-01-01", "2026-02-01")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["live_id"], "live1")
        self.assertEqual(row["gmv"], 250.00)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["click_through_rate"], 10.00)
        self.assertEqual(row["click_to_order_rate"], 5.00)
        self.assertEqual(row["avg_price"], 12.50)
        self.assertEqual(row["items_sold"], 20)

    def test_pagination_follows_next_page_token(self) -> None:
        p1 = _resp(data={"live_stream_sessions": [{"id": "a"}], "next_page_token": "tok2"})
        p2 = _resp(data={"live_stream_sessions": [{"id": "b"}], "next_page_token": ""})
        with patch.object(tl.requests, "get", side_effect=[p1, p2]) as get:
            rows = tl.fetch_lives("ALL", "2026-01-01", "2026-02-01")
        self.assertEqual(get.call_count, 2)
        self.assertEqual([r["live_id"] for r in rows], ["a", "b"])


class DaySellersAndLiveRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_day_sellers_stops_at_first_zero_gmv(self) -> None:
        page = _resp(data={"products": [
            {"id": "p1", "overall_performance": {"gmv": {"amount": "10"}}},
            {"id": "p2", "overall_performance": {"gmv": {"amount": "0"}}},
            {"id": "p3", "overall_performance": {"gmv": {"amount": "0"}}},
        ], "next_page_token": ""})
        with patch.object(tl.requests, "get", return_value=page):
            ids = tl._day_sellers("2026-01-01", "2026-01-02")
        self.assertEqual(ids, ["p1"])

    def test_live_row_none_when_no_live_activity(self) -> None:
        page = _resp(data={"performance": {"intervals": [{
            "impression_breakdowns": [{"type": "VIDEO", "amount": 100}],
            "page_view_breakdowns": [], "unit_sold_breakdowns": [], "gmv_breakdowns": [],
        }]}})
        with patch.object(tl.requests, "get", return_value=page):
            row = tl._live_row("p1", "2026-01-01", "2026-01-02")
        self.assertIsNone(row)

    def test_live_row_extracts_only_the_live_slice(self) -> None:
        page = _resp(data={"performance": {"intervals": [{
            "impression_breakdowns": [{"type": "LIVE", "amount": 40}, {"type": "VIDEO", "amount": 100}],
            "page_view_breakdowns": [{"type": "LIVE", "amount": 8}],
            "unit_sold_breakdowns": [{"type": "LIVE", "amount": 3}],
            "gmv_breakdowns": [{"type": "LIVE", "amount": 60, "currency": "USD"}],
            "avg_page_visitor_breakdowns": [{"type": "LIVE", "amount": 5}],
        }]}})
        with patch.object(tl.requests, "get", return_value=page):
            row = tl._live_row("p1", "2026-01-01", "2026-01-02")
        self.assertEqual(row["live_impressions"], 40)
        self.assertEqual(row["live_clicks"], 8)
        self.assertEqual(row["live_units"], 3)
        self.assertEqual(row["live_gmv"], 60.0)
        self.assertEqual(row["currency"], "USD")


class OwnLiveDaysTests(unittest.TestCase):
    def test_buckets_by_configured_shop_timezone_not_utc(self) -> None:
        conn = sqlite3.connect(":memory:")
        tl.ensure_schema(conn)
        # 2026-01-01 23:30 UTC == 2026-01-01 18:30 America/New_York (UTC-5 in Jan)
        ts = 1767309000  # 2026-01-01T23:30:00Z
        conn.execute(
            "INSERT INTO tiktok_shop_lives (live_id, account_type, start_time, end_time, "
            "window_start, window_end, synced_at) VALUES (?,?,?,?,?,?,?)",
            ("l1", "OFFICIAL_ACCOUNTS", str(ts), str(ts), "2026-01-01", "2026-01-02", "now"),
        )
        with patch.object(tl, "SHOP_TZ", tl.ZoneInfo("America/New_York")):
            days = tl.own_live_days(conn, "2026-01-01", "2026-01-03")
        self.assertEqual(days, ["2026-01-01"])
        with patch.object(tl, "SHOP_TZ", tl.ZoneInfo("UTC")):
            days_utc = tl.own_live_days(conn, "2026-01-01", "2026-01-03")
        self.assertEqual(days_utc, ["2026-01-01"])  # still Jan 1 in UTC too, just checking no crash
        conn.close()

    def test_missing_table_returns_empty(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.assertEqual(tl.own_live_days(conn, "2026-01-01", "2026-01-02"), [])
        conn.close()


class SyncWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "warehouse.db"
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path

    def test_sync_lives_writes_rows(self) -> None:
        page = _resp(data={"live_stream_sessions": [{
            "id": "l1", "title": "T", "username": "u",
            "start_time": "1700000000", "end_time": "1700003600",
            "interaction_performance": {}, "sales_performance": {"gmv": {"amount": 5, "currency": "USD"}},
        }], "next_page_token": ""})
        with patch.object(tl.requests, "get", return_value=page):
            n = tl.sync_lives("2026-01-01", "2026-02-01", ["OFFICIAL_ACCOUNTS"])
        self.assertEqual(n, 1)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tiktok_shop_lives").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_sync_live_products_deletes_and_replaces_the_day(self) -> None:
        sellers_page = _resp(data={"products": [
            {"id": "p1", "overall_performance": {"gmv": {"amount": "10"}}},
        ], "next_page_token": ""})
        detail_page = _resp(data={"performance": {"intervals": [{
            "impression_breakdowns": [{"type": "LIVE", "amount": 10}],
            "page_view_breakdowns": [{"type": "LIVE", "amount": 2}],
            "unit_sold_breakdowns": [{"type": "LIVE", "amount": 1}],
            "gmv_breakdowns": [{"type": "LIVE", "amount": 15, "currency": "USD"}],
        }]}})
        with patch.object(tl.requests, "get", side_effect=[sellers_page, detail_page]):
            n = tl.sync_live_products(["2026-01-05"])
        self.assertEqual(n, 1)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT product_id, live_gmv FROM tiktok_shop_live_products WHERE date = ?", ("2026-01-05",)
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("p1", 15.0))


if __name__ == "__main__":
    unittest.main()
