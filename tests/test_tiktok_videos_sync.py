"""Hermetic tests for tiktok_videos_sync.py -- no network, no real warehouse.db.

Covers: schema creation, row shaping (incl. tagged-product extraction),
pagination, the gmv_positive_only early-stop, the token-refresh retry path,
and the "missing credentials fail loudly" guard.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tiktok_videos_sync as tv
from warehouse import db


def _resp(code=0, data=None, message="ok"):
    class _R:
        status_code = 200

        def json(self):
            return {"code": code, "message": message, "data": data or {}}
    return _R()


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_table_with_expected_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        tv.ensure_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tiktok_shop_videos)")}
        for expected in ("video_id", "account_type", "gmv", "product_id", "product_count", "window_start"):
            self.assertIn(expected, cols)
        # idempotent
        tv.ensure_schema(conn)
        conn.close()


class CheckRequiredEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                tv.check_required_env()
        self.assertIn("TIKTOK_APP_KEY", str(cm.exception))

    def test_passes_when_all_present(self) -> None:
        env = {v: "x" for v in tv.REQUIRED_ENV}
        with patch.dict(os.environ, env, clear=True):
            tv.check_required_env()  # should not raise


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_row_shaping_and_tagged_product_extraction(self) -> None:
        page = _resp(data={"videos": [{
            "id": "v1", "title": "Unboxing", "username": "creator1",
            "gmv": {"amount": "12.50", "currency": "USD"},
            "sku_orders": 3, "units_sold": 4, "views": 1000,
            "click_through_rate": 0.05, "video_post_time": "2026-01-01T00:00:00Z",
            "products": [{"id": "p1", "name": "Widget"}, {"id": "p2", "name": "Gadget"}],
        }], "next_page_token": ""})
        with patch.object(tv.requests, "get", return_value=page) as get:
            rows = tv.fetch("LINKED_ACCOUNTS", "2026-01-01", "2026-02-01")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["video_id"], "v1")
        self.assertEqual(row["gmv"], 12.50)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["product_id"], "p1")
        self.assertEqual(row["product_name"], "Widget")
        self.assertEqual(row["product_count"], 2)
        self.assertEqual(row["account_type"], "LINKED_ACCOUNTS")

    def test_pagination_follows_next_page_token(self) -> None:
        page1 = _resp(data={"videos": [{"id": "v1", "gmv": {"amount": 1}}], "next_page_token": "abc"})
        page2 = _resp(data={"videos": [{"id": "v2", "gmv": {"amount": 2}}], "next_page_token": ""})
        with patch.object(tv.requests, "get", side_effect=[page1, page2]) as get:
            rows = tv.fetch("AFFILIATES", "2026-01-01", "2026-02-01")
        self.assertEqual(get.call_count, 2)
        self.assertEqual([r["video_id"] for r in rows], ["v1", "v2"])
        # second call must have carried the page token forward
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page_token"], "abc")

    def test_gmv_positive_only_stops_at_first_zero(self) -> None:
        # GMV-sorted descending: a $0 row means everything after it is also $0.
        page = _resp(data={"videos": [
            {"id": "v1", "gmv": {"amount": 5}},
            {"id": "v2", "gmv": {"amount": 0}},
            {"id": "v3", "gmv": {"amount": 0}},
        ], "next_page_token": ""})
        with patch.object(tv.requests, "get", return_value=page):
            rows = tv.fetch("AFFILIATES", "2026-01-01", "2026-02-01", gmv_positive_only=True)
        self.assertEqual([r["video_id"] for r in rows], ["v1"])

    def test_gmv_positive_only_false_keeps_zero_gmv_rows(self) -> None:
        page = _resp(data={"videos": [
            {"id": "v1", "gmv": {"amount": 5}},
            {"id": "v2", "gmv": {"amount": 0}},
        ], "next_page_token": ""})
        with patch.object(tv.requests, "get", return_value=page):
            rows = tv.fetch("AFFILIATES", "2026-01-01", "2026-02-01", gmv_positive_only=False)
        self.assertEqual([r["video_id"] for r in rows], ["v1", "v2"])

    def test_expired_token_is_refreshed_once_then_retried(self) -> None:
        expired = _resp(code=next(iter(tv.TOKEN_EXPIRED_CODES)), message="token expired")
        ok = _resp(data={"videos": [], "next_page_token": ""})
        with (
            patch.object(tv.requests, "get", side_effect=[expired, ok]) as get,
            patch.object(tv, "_refresh_access_token") as refresh,
        ):
            rows = tv.fetch("AFFILIATES", "2026-01-01", "2026-02-01")
        refresh.assert_called_once()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(rows, [])

    def test_hard_api_error_raises(self) -> None:
        err = _resp(code=99999, message="boom")
        with patch.object(tv.requests, "get", return_value=err):
            with self.assertRaises(RuntimeError) as cm:
                tv.fetch("AFFILIATES", "2026-01-01", "2026-02-01")
        self.assertIn("boom", str(cm.exception))


class SyncWriteTests(unittest.TestCase):
    """End-to-end against a real (temp, throwaway) SQLite file."""

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

    def test_sync_writes_rows_and_is_idempotent_via_upsert(self) -> None:
        page = _resp(data={"videos": [{
            "id": "v1", "title": "T", "username": "u1", "gmv": {"amount": 9, "currency": "USD"},
            "sku_orders": 1, "units_sold": 1, "views": 10, "click_through_rate": 0.1,
            "video_post_time": None, "products": [],
        }], "next_page_token": ""})
        with patch.object(tv.requests, "get", return_value=page):
            n1 = tv.sync("2026-01-01", "2026-02-01", ["LINKED_ACCOUNTS"])
            n2 = tv.sync("2026-01-01", "2026-02-01", ["LINKED_ACCOUNTS"])  # re-run: upsert, not duplicate
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tiktok_shop_videos").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
