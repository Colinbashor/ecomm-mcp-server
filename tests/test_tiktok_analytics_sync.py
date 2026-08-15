"""Hermetic tests for tiktok_analytics_sync.py -- no network, no real warehouse.db.

Covers: schema creation, row-shaping of the content-type breakdown response
(the three per-type rows + the shop-wide TOTAL row), the missing-credentials
guard, the token-refresh-and-retry request flow, chunking of a long date
range, dropping an unsettled trailing day, and treating the retention/
too-wide-span error code as skip-and-continue rather than fatal.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tiktok_analytics_sync as ta
from warehouse import db


def _resp(code=0, data=None, message="ok"):
    class _R:
        status_code = 200

        def json(self):
            return {"code": code, "message": message, "data": data or {}}
    return _R()


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        ta.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tiktok_shop_performance)")}
        for expected in ("date", "content_type", "gmv", "buyers", "orders",
                         "units_sold", "refunds", "synced_at"):
            self.assertIn(expected, cols)
        # PRIMARY KEY (date, content_type)
        pk_cols = {r[1] for r in conn.execute("PRAGMA table_info(tiktok_shop_performance)") if r[5] > 0}
        self.assertEqual(pk_cols, {"date", "content_type"})
        conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        ta.ensure_schema(conn)
        ta.ensure_schema(conn)  # must not raise
        conn.close()


class CheckRequiredEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                ta.check_required_env()
        self.assertIn("TIKTOK_APP_KEY", str(cm.exception))


class FetchRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _interval(self, day: str) -> dict:
        return {
            "start_date": day,
            "gmv": {"amount": "1000.00", "currency": "USD"},
            "buyers": "40",
            "product_impressions": "5000",
            "product_page_views": "800",
            "avg_product_page_visitors": "120",
            "orders": "50",
            "sku_orders": "55",
            "units_sold": "60",
            "avg_order_value": {"amount": "20.00", "currency": "USD"},
            "refunds": {"amount": "30.00", "currency": "USD"},
            "cancellations_and_returns": "2",
            "gmv_breakdowns": [
                {"type": "LIVE", "amount": "100.00"},
                {"type": "VIDEO", "amount": "600.00"},
                {"type": "PRODUCT_CARD", "amount": "300.00"},
            ],
            "buyer_breakdowns": [
                {"type": "LIVE", "amount": "5"},
                {"type": "VIDEO", "amount": "20"},
                {"type": "PRODUCT_CARD", "amount": "15"},
            ],
            "product_impression_breakdowns": [
                {"type": "LIVE", "amount": "500"},
                {"type": "VIDEO", "amount": "3000"},
                {"type": "PRODUCT_CARD", "amount": "1500"},
            ],
            "product_page_view_breakdowns": [
                {"type": "LIVE", "amount": "80"},
                {"type": "VIDEO", "amount": "500"},
                {"type": "PRODUCT_CARD", "amount": "220"},
            ],
            "avg_product_page_visitor_breakdowns": [
                {"type": "LIVE", "amount": "10"},
                {"type": "VIDEO", "amount": "70"},
                {"type": "PRODUCT_CARD", "amount": "40"},
            ],
        }

    def test_shapes_three_type_rows_plus_total_row(self) -> None:
        payload = {
            "latest_available_date": "2026-01-02",
            "performance": {"intervals": [self._interval("2026-01-01")]},
        }
        with patch.object(ta.requests, "get", return_value=_resp(data=payload)):
            rows, latest = ta.fetch_range("2026-01-01", "2026-01-02")

        self.assertEqual(latest, "2026-01-02")
        self.assertEqual(len(rows), 4)  # LIVE, VIDEO, PRODUCT_CARD, TOTAL

        by_type = {r[1]: r for r in rows}
        self.assertEqual(set(by_type), {"LIVE", "VIDEO", "PRODUCT_CARD", "TOTAL"})

        live = by_type["LIVE"]
        self.assertEqual(live[0], "2026-01-01")   # date
        self.assertEqual(live[2], 100.0)          # gmv
        self.assertEqual(live[3], 5.0)             # buyers
        # shop-only scalars are NULL on a per-type row
        self.assertIsNone(live[7])   # orders
        self.assertIsNone(live[11])  # refunds

        total = by_type["TOTAL"]
        self.assertEqual(total[2], 1000.0)  # gmv
        self.assertEqual(total[7], 50)       # orders
        self.assertEqual(total[11], 30.0)    # refunds

        # the three type rows sum back to TOTAL's gmv -- mutually exclusive split
        type_sum = sum(by_type[t][2] for t in ta.TYPES)
        self.assertEqual(type_sum, total[2])

    def test_empty_intervals_yields_no_rows(self) -> None:
        payload = {"latest_available_date": "2026-01-02", "performance": {"intervals": []}}
        with patch.object(ta.requests, "get", return_value=_resp(data=payload)):
            rows, latest = ta.fetch_range("2026-01-01", "2026-01-02")
        self.assertEqual(rows, [])
        self.assertEqual(latest, "2026-01-02")


class RequestRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_expired_token_triggers_one_refresh_and_retry(self) -> None:
        expired = _resp(code=next(iter(ta.TOKEN_EXPIRED_CODES)), message="token expired")
        ok = _resp(code=0, data={"performance": {"intervals": []}})
        with patch.object(ta.requests, "get", side_effect=[expired, ok]) as get, \
             patch.object(ta, "_refresh_access_token") as refresh:
            data = ta._request({"start_date_ge": "2026-01-01"})
        self.assertEqual(get.call_count, 2)
        refresh.assert_called_once()
        self.assertEqual(data["code"], 0)

    def test_non_zero_code_raises_with_code_attached(self) -> None:
        bad = _resp(code=28001022, message="invalid parameter")
        with patch.object(ta.requests, "get", return_value=bad):
            with self.assertRaises(RuntimeError) as cm:
                ta._request({"start_date_ge": "2026-01-01"})
        self.assertEqual(cm.exception.args[1], 28001022)


class ChunksTests(unittest.TestCase):
    def test_splits_long_range_at_chunk_days(self) -> None:
        from datetime import date
        spans = list(ta.chunks(date(2026, 1, 1), date(2026, 4, 1)))
        # 90-day range with a 60-day chunk size -> two spans
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0][0], "2026-01-01")
        self.assertEqual(spans[-1][1], "2026-04-01")
        # spans are contiguous and non-overlapping
        for (s0, e0), (s1, e1) in zip(spans, spans[1:]):
            self.assertEqual(e0, s1)

    def test_short_range_is_a_single_chunk(self) -> None:
        from datetime import date
        spans = list(ta.chunks(date(2026, 1, 1), date(2026, 1, 10)))
        self.assertEqual(spans, [("2026-01-01", "2026-01-10")])


class FetchWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _interval(self, day: str, gmv: str = "100.00") -> dict:
        return {
            "start_date": day, "gmv": {"amount": gmv, "currency": "USD"},
            "buyers": "1", "product_impressions": "1", "product_page_views": "1",
            "avg_product_page_visitors": "1", "orders": "1", "sku_orders": "1",
            "units_sold": "1", "avg_order_value": {"amount": "1"}, "refunds": {"amount": "0"},
            "cancellations_and_returns": "0",
            "gmv_breakdowns": [{"type": t, "amount": "1"} for t in ta.TYPES],
        }

    def test_drops_rows_after_latest_available_date(self) -> None:
        payload = {
            "latest_available_date": "2026-01-01",
            "performance": {"intervals": [
                self._interval("2026-01-01"), self._interval("2026-01-02"),
            ]},
        }
        with patch.object(ta.requests, "get", return_value=_resp(data=payload)), \
             patch("time.sleep"):
            rows, skipped = ta.fetch_window("2026-01-01", "2026-01-03")
        self.assertEqual(skipped, [])
        self.assertTrue(all(r[0] <= "2026-01-01" for r in rows))
        self.assertTrue(any(r[0] == "2026-01-01" for r in rows))

    def test_retention_error_is_skipped_not_fatal(self) -> None:
        retention_error = _resp(code=ta.RETENTION_CODE, message="invalid parameter")
        with patch.object(ta.requests, "get", return_value=retention_error), \
             patch("time.sleep"):
            rows, skipped = ta.fetch_window("2020-01-01", "2020-02-01")
        self.assertEqual(rows, [])
        self.assertEqual(len(skipped), 1)

    def test_non_retention_error_propagates(self) -> None:
        other_error = _resp(code=99999, message="some other failure")
        with patch.object(ta.requests, "get", return_value=other_error), \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                ta.fetch_window("2026-01-01", "2026-01-02")


class SyncWritePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "warehouse.db"
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path

    def test_sync_writes_rows_and_reupsert_is_idempotent(self) -> None:
        payload = {
            "latest_available_date": "2026-01-01",
            "performance": {"intervals": [{
                "start_date": "2026-01-01", "gmv": {"amount": "100.00", "currency": "USD"},
                "buyers": "1", "product_impressions": "1", "product_page_views": "1",
                "avg_product_page_visitors": "1", "orders": "1", "sku_orders": "1",
                "units_sold": "1", "avg_order_value": {"amount": "1"}, "refunds": {"amount": "0"},
                "cancellations_and_returns": "0",
                "gmv_breakdowns": [{"type": t, "amount": "1"} for t in ta.TYPES],
            }]},
        }
        with patch.object(ta.requests, "get", return_value=_resp(data=payload)), \
             patch("time.sleep"):
            n1 = ta.sync("2026-01-01", "2026-01-02")
            n2 = ta.sync("2026-01-01", "2026-01-02")  # re-run: same PK, no duplication

        self.assertEqual(n1, 4)
        self.assertEqual(n2, 4)
        conn = sqlite3.connect(db.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM tiktok_shop_performance").fetchone()[0]
        conn.close()
        self.assertEqual(count, 4)

    def test_sync_returns_zero_when_no_rows(self) -> None:
        payload = {"latest_available_date": "2026-01-02", "performance": {"intervals": []}}
        with patch.object(ta.requests, "get", return_value=_resp(data=payload)), \
             patch("time.sleep"):
            n = ta.sync("2026-01-01", "2026-01-02")
        self.assertEqual(n, 0)
        self.assertFalse(Path(db.DB_PATH).exists())


if __name__ == "__main__":
    unittest.main()
