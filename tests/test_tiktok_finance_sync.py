"""Hermetic tests for tiktok_finance_sync.py -- no network, no real warehouse.db.

Covers: schema creation, the missing-credentials guard, the token-refresh-
and-retry request flow, statement-list pagination, the per-order transaction
fee decomposition (and the load-bearing is_fee split -- sales tax / seller
discount must never be tagged as fees), and the end-to-end sync/upsert
behavior.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tiktok_finance_sync as tf
from warehouse import db


def _resp(code=0, data=None, message="ok"):
    class _R:
        status_code = 200

        def json(self):
            return {"code": code, "message": message, "data": data or {}}
    return _R()


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_three_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        tf.ensure_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tiktok_settlements", tables)
        self.assertIn("tiktok_settlement_components", tables)
        self.assertIn("tiktok_settlement_orders", tables)
        conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        tf.ensure_schema(conn)
        tf.ensure_schema(conn)  # must not raise
        conn.close()


class CheckRequiredEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                tf.check_required_env()
        self.assertIn("TIKTOK_APP_KEY", str(cm.exception))


class SignTests(unittest.TestCase):
    def test_sign_is_deterministic_and_excludes_sign_and_token(self) -> None:
        params = {"app_key": "k", "timestamp": "1", "sign": "stale", "access_token": "ignored"}
        s1 = tf._sign("/path", params, "secret")
        s2 = tf._sign("/path", {**params, "sign": "different-stale-value"}, "secret")
        self.assertEqual(s1, s2)  # sign/access_token don't affect the signature


class RequestRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_expired_token_triggers_one_refresh_and_retry(self) -> None:
        expired = _resp(code=next(iter(tf.TOKEN_EXPIRED_CODES)), message="token expired")
        ok = _resp(code=0, data={"statements": []})
        with patch.object(tf.requests, "get", side_effect=[expired, ok]) as get, \
             patch.object(tf, "_refresh_access_token") as refresh:
            data = tf._get(tf.STATEMENTS_PATH, {}, None)
        self.assertEqual(get.call_count, 2)
        refresh.assert_called_once()
        self.assertEqual(data["code"], 0)

    def test_non_zero_code_raises_with_code_attached(self) -> None:
        bad = _resp(code=12345, message="SortField is a required field")
        with patch.object(tf.requests, "get", return_value=bad):
            with self.assertRaises(RuntimeError) as cm:
                tf._get(tf.STATEMENTS_PATH, {}, None)
        self.assertEqual(cm.exception.args[1], 12345)


class FetchStatementsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_follows_next_page_token_and_stops_at_end(self) -> None:
        page1 = _resp(data={"statements": [{"id": 1}], "next_page_token": "P2"})
        page2 = _resp(data={"statements": [{"id": 2}], "next_page_token": None})
        with patch.object(tf.requests, "get", side_effect=[page1, page2]):
            rows = tf.fetch_statements(30)
        self.assertEqual([r["id"] for r in rows], [1, 2])

    def test_stops_on_empty_batch_even_with_a_token(self) -> None:
        empty = _resp(data={"statements": [], "next_page_token": "SOMETHING"})
        with patch.object(tf.requests, "get", return_value=empty):
            rows = tf.fetch_statements(30)
        self.assertEqual(rows, [])


class FetchStatementTransactionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_aggregates_fee_and_context_fields_and_builds_order_rows(self) -> None:
        tx = {
            "order_id": "ORD-1", "order_create_time": "1700000000", "currency": "USD",
            "revenue_amount": "50.00", "referral_fee_amount": "-3.00",
            "affiliate_commission_amount": "-2.00",
            "sales_tax_amount": "-4.00", "seller_discount_amount": "-1.00",
        }
        payload = _resp(data={"statement_transactions": [tx], "next_page_token": None})
        with patch.object(tf.requests, "get", return_value=payload):
            agg, order_rows = tf.fetch_statement_transactions("STMT-1")

        self.assertEqual(agg["referral_fee_amount"], -3.00)
        self.assertEqual(agg["affiliate_commission_amount"], -2.00)
        # pass-through / self-inflicted fields are captured too...
        self.assertEqual(agg["sales_tax_amount"], -4.00)
        self.assertEqual(agg["seller_discount_amount"], -1.00)
        # ...but the fee/context split (is_fee) only happens downstream in sync().
        self.assertNotIn("sales_tax_amount", tf.FEE_FIELDS)
        self.assertNotIn("seller_discount_amount", tf.FEE_FIELDS)

        self.assertEqual(len(order_rows), 1)
        self.assertEqual(order_rows[0][0], "STMT-1")
        self.assertEqual(order_rows[0][1], "ORD-1")

    def test_missing_order_id_is_skipped_from_order_rows_but_still_aggregated(self) -> None:
        tx = {"referral_fee_amount": "-1.50"}
        payload = _resp(data={"statement_transactions": [tx], "next_page_token": None})
        with patch.object(tf.requests, "get", return_value=payload):
            agg, order_rows = tf.fetch_statement_transactions("STMT-1")
        self.assertEqual(agg["referral_fee_amount"], -1.50)
        self.assertEqual(order_rows, [])


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

    def _statement_page(self, fee_amount="-10.00"):
        return _resp(data={
            "statements": [{"id": 1, "statement_time": "1700000000", "payment_time": "1700000100",
                            "payment_status": "PAID", "currency": "USD", "revenue_amount": "100.00",
                            "net_sales_amount": "95.00", "fee_amount": fee_amount,
                            "shipping_cost_amount": "0", "adjustment_amount": "0",
                            "settlement_amount": "90.00"}],
            "next_page_token": None,
        })

    def _transactions_page(self):
        return _resp(data={
            "statement_transactions": [{
                "order_id": "ORD-1", "order_create_time": "1700000000", "currency": "USD",
                "revenue_amount": "100.00", "referral_fee_amount": "-6.00",
                "sales_tax_amount": "-2.00",
            }],
            "next_page_token": None,
        })

    def test_sync_writes_statement_and_is_idempotent(self) -> None:
        with patch.object(tf.requests, "get", return_value=self._statement_page(fee_amount="0")):
            summary1 = tf.sync(30, with_components=False)
            summary2 = tf.sync(30, with_components=False)
        self.assertEqual(summary1["statements"], 1)
        self.assertEqual(summary2["statements"], 1)
        conn = sqlite3.connect(db.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM tiktok_settlements").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)  # re-run didn't duplicate the PK'd row

    def test_sync_with_components_tags_is_fee_correctly(self) -> None:
        responses = [self._statement_page(), self._transactions_page()]
        with patch.object(tf.requests, "get", side_effect=responses):
            summary = tf.sync(30, with_components=True, with_orders=True)

        self.assertEqual(summary["components"], 2)  # referral fee + sales tax
        conn = sqlite3.connect(db.DB_PATH)
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT field, is_fee FROM tiktok_settlement_components")}
        conn.close()
        self.assertEqual(rows["referral_fee_amount"], 1)
        self.assertEqual(rows["sales_tax_amount"], 0)  # pass-through, never a fee

    def test_no_components_flag_skips_the_breakdown_call(self) -> None:
        with patch.object(tf.requests, "get", return_value=self._statement_page()) as get:
            summary = tf.sync(30, with_components=False)
        self.assertEqual(get.call_count, 1)  # only the statements list, no transactions call
        self.assertEqual(summary["components"], 0)

    def test_no_orders_flag_skips_order_table_but_keeps_components(self) -> None:
        responses = [self._statement_page(), self._transactions_page()]
        with patch.object(tf.requests, "get", side_effect=responses):
            summary = tf.sync(30, with_components=True, with_orders=False)
        self.assertGreater(summary["components"], 0)
        self.assertEqual(summary["order_rows"], 0)
        conn = sqlite3.connect(db.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM tiktok_settlement_orders").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_fee_rate_computed_from_revenue_and_fees(self) -> None:
        with patch.object(tf.requests, "get", return_value=self._statement_page(fee_amount="-20.00")):
            summary = tf.sync(30, with_components=False)
        self.assertAlmostEqual(summary["rate"], 20.00 / 100.00)


if __name__ == "__main__":
    unittest.main()
