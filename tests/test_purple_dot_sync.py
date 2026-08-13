"""Hermetic tests for purple_dot_sync.py - no network, no real warehouse.db.

Covers: schema creation, row-shaping for a pre-order (with its nested lines
and export links) and a waitlist (with its nested variant allocations), the
HTTP retry/backoff contract for `_get` (connection drop, 429, hard auth
errors, wrong-path redirects), and that `sync_preorders`/`sync_waitlists`
write the expected rows and cursor/high-water state given a mocked page
iterator.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import purple_dot_sync as pds

SAMPLE_ORDER = {
    "id": "pd_1",
    "order_number": "PD1000001",
    "reference": "#PD1000001",
    "created_at": "2026-01-01T00:00:00Z",
    "placed_at": "2026-01-01T00:00:00Z",
    "cancelled_at": None,
    "cancel_reason": None,
    "currency": "USD",
    "customer": {"external_id": "cust_1"},
    "subtotal_price": "100.00",
    "total_discounts": "10.00",
    "total_tax": "5.00",
    "total_price": "95.00",
    "total_refunded": "0.00",
    "tax_included": False,
    "discount_codes": ["WELCOME10"],
    "shipping_address": {"city": "Portland", "province_code": "OR", "country_code": "US"},
    "line_items": [
        {"id": "line_1", "sku": "SKU1", "product_id": "prod_1", "variant_id": "var_1",
         "name": "Widget", "quantity": 2, "unit_price": "50.00", "unit_total": "100.00",
         "price": "100.00", "total_discount": "10.00", "total": "90.00", "taxable": True,
         "earliest_ship_date": "2026-03-01", "latest_ship_date": "2026-03-15",
         "waitlist_id": "wl_1", "cancelled": False, "cancelled_at": None,
         "shopify_line_item_id": "9999"},
    ],
    "exported_orders": [
        {"id": 555, "order_number": "PD1000001/1", "type": "SHOPIFY_ORDER",
         "created_at": "2026-01-10T00:00:00Z", "line_items": [{"id": "li_1"}]},
    ],
}

SAMPLE_WAITLIST = {
    "id": "wl_1",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-05T00:00:00Z",
    "state": "Live",
    "earliest_ship_date": "2026-03-01",
    "latest_ship_date": "2026-03-15",
    "launch_date": "2026-01-01",
    "scheduled_pause_date": None,
    "labels": ["restock"],
    "availability": {
        "product": {"product_id": "prod_1", "buy_size": 100, "committed": 40, "available": 60},
        "variants": [
            {"variant_id": 111, "sku": "SKU1", "buy_size": 50, "committed": 20, "available": 30},
        ],
    },
}


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        pds.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(
            {"purple_dot_preorders", "purple_dot_preorder_lines",
             "purple_dot_preorder_exports", "purple_dot_waitlists",
             "purple_dot_waitlist_inventory", "purple_dot_sync_state"} <= tables)

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        pds.ensure_schema(conn)
        pds.ensure_schema(conn)  # must not raise on a second call


class PreorderRowShapeTests(unittest.TestCase):
    def test_order_row_matches_column_order(self) -> None:
        stamp = "2026-01-15T00:00:00+00:00"
        order_row, line_rows, export_rows = pds._preorder_rows(SAMPLE_ORDER, stamp)

        # index positions follow purple_dot_preorders' CREATE TABLE column order
        self.assertEqual(order_row[0], "pd_1")               # id
        self.assertEqual(order_row[7], "USD")                 # currency
        self.assertEqual(order_row[8], "cust_1")               # customer_external_id
        self.assertEqual(order_row[9], 100.0)                  # subtotal_price
        self.assertEqual(order_row[19], 1)                     # n_lines
        self.assertEqual(order_row[20], 2)                     # n_units (sum of quantities)
        self.assertEqual(order_row[21], "555")                 # shopify_order_id, cast to str
        self.assertEqual(order_row[24], 1)                     # n_exported
        self.assertEqual(order_row[25], stamp)                 # synced_at
        self.assertEqual(json.loads(order_row[15]), ["WELCOME10"])  # discount_codes JSON

    def test_line_rows_carry_sku_and_quantity(self) -> None:
        stamp = "2026-01-15T00:00:00+00:00"
        _order_row, line_rows, _export_rows = pds._preorder_rows(SAMPLE_ORDER, stamp)
        self.assertEqual(len(line_rows), 1)
        line = line_rows[0]
        self.assertEqual(line[0], "pd_1")     # preorder_id
        self.assertEqual(line[1], "line_1")   # line_id
        self.assertEqual(line[3], "SKU1")     # sku
        self.assertEqual(line[7], 2)          # quantity

    def test_export_rows_use_string_shopify_order_id(self) -> None:
        stamp = "2026-01-15T00:00:00+00:00"
        _order_row, _line_rows, export_rows = pds._preorder_rows(SAMPLE_ORDER, stamp)
        self.assertEqual(len(export_rows), 1)
        self.assertEqual(export_rows[0][0], "pd_1")
        self.assertEqual(export_rows[0][1], "555")  # matches order_row[21] byte-for-byte

    def test_lines_without_an_id_are_dropped(self) -> None:
        order = dict(SAMPLE_ORDER, line_items=[{"sku": "NOID", "quantity": 1}])
        _order_row, line_rows, _export_rows = pds._preorder_rows(order, "stamp")
        self.assertEqual(line_rows, [])

    def test_cancelled_order_with_no_exports_has_null_shopify_fields(self) -> None:
        order = dict(SAMPLE_ORDER, exported_orders=[], cancelled_at="2026-01-02T00:00:00Z")
        order_row, _line_rows, export_rows = pds._preorder_rows(order, "stamp")
        self.assertIsNone(order_row[21])   # shopify_order_id
        self.assertEqual(order_row[24], 0)  # n_exported
        self.assertEqual(export_rows, [])


class WaitlistRowShapeTests(unittest.TestCase):
    def test_waitlist_row_matches_column_order(self) -> None:
        wl_row, inv_rows = pds._waitlist_rows(SAMPLE_WAITLIST, "2026-01-15", "stamp")
        self.assertEqual(wl_row[0], "wl_1")     # id
        self.assertEqual(wl_row[3], "Live")     # state
        self.assertEqual(wl_row[9], "prod_1")   # product_id
        self.assertEqual(wl_row[10], 100)       # buy_size (product-level)
        self.assertEqual(json.loads(wl_row[8]), ["restock"])  # labels JSON

    def test_variant_inventory_rows_cast_variant_id_to_string(self) -> None:
        _wl_row, inv_rows = pds._waitlist_rows(SAMPLE_WAITLIST, "2026-01-15", "stamp")
        self.assertEqual(len(inv_rows), 1)
        row = inv_rows[0]
        self.assertEqual(row[0], "2026-01-15")  # snapshot_date
        self.assertEqual(row[1], "wl_1")        # waitlist_id
        self.assertEqual(row[2], "111")         # variant_id cast to str
        self.assertEqual(row[3], "SKU1")        # sku
        self.assertEqual(row[5], 50)            # buy_size (variant-level)

    def test_variants_without_a_variant_id_are_dropped(self) -> None:
        wl = dict(SAMPLE_WAITLIST)
        wl["availability"] = {"product": {}, "variants": [{"sku": "NOID"}]}
        _wl_row, inv_rows = pds._waitlist_rows(wl, "2026-01-15", "stamp")
        self.assertEqual(inv_rows, [])


class _TimeShim:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class GetRetryTests(unittest.TestCase):
    """Same contract style as the other connectors' transport tests: network
    blips and 5xx/429 are retried with backoff; a bad token or wrong path
    fails immediately with a message that says why."""

    def setUp(self) -> None:
        self._time = pds.time
        pds.time = _TimeShim()
        self._env_patch = patch.dict(pds.os.environ, {"PURPLE_DOT_ACCESS_TOKEN": "test-token"})
        self._env_patch.start()

    def tearDown(self) -> None:
        pds.time = self._time
        self._env_patch.stop()

    def test_connection_error_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def fake_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise pds.requests.exceptions.ConnectionError("boom")
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"meta": {"result": "success"}, "data": {"orders": []}}
            return resp

        with patch.object(pds.requests, "get", side_effect=fake_get):
            result = pds._get("/pre-orders", {})
        self.assertEqual(result, {"orders": []})
        self.assertEqual(calls["n"], 2)

    def test_429_is_retried_after_backoff(self) -> None:
        resp_429 = MagicMock(status_code=429, headers={"Retry-After": "2"})
        resp_ok = MagicMock(status_code=200)
        resp_ok.json.return_value = {"meta": {"result": "success"}, "data": {"waitlists": []}}
        with patch.object(pds.requests, "get", side_effect=[resp_429, resp_ok]):
            result = pds._get("/waitlists", {})
        self.assertEqual(result, {"waitlists": []})
        self.assertEqual(pds.time.slept, [2.0])

    def test_401_raises_immediately_naming_the_token(self) -> None:
        resp = MagicMock(status_code=401, text="unauthorized")
        with patch.object(pds.requests, "get", return_value=resp):
            with self.assertRaises(RuntimeError) as cm:
                pds._get("/pre-orders", {})
        self.assertIn("access token rejected", str(cm.exception))

    def test_wrong_path_redirect_raises_immediately(self) -> None:
        resp = MagicMock(status_code=302, headers={"location": "/admin/login"})
        with patch.object(pds.requests, "get", return_value=resp):
            with self.assertRaises(RuntimeError) as cm:
                pds._get("/fulfillment_orders", {})
        self.assertIn("redirected", str(cm.exception))

    def test_5xx_is_retried_then_succeeds(self) -> None:
        resp_500 = MagicMock(status_code=500)
        resp_ok = MagicMock(status_code=200)
        resp_ok.json.return_value = {"meta": {"result": "success"}, "data": {"orders": []}}
        with patch.object(pds.requests, "get", side_effect=[resp_500, resp_ok]):
            result = pds._get("/pre-orders", {})
        self.assertEqual(result, {"orders": []})


class SyncPreordersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        pds.ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    @patch.object(pds, "_count", return_value=None)
    def test_incremental_run_writes_order_line_and_export_rows(self, _mock_count) -> None:
        with patch.object(pds, "_iter_pages", return_value=iter([([SAMPLE_ORDER], None)])):
            n = pds.sync_preorders(self.conn, backfill=False, restart=False,
                                   days=30, max_pages=10)
        self.assertEqual(n, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM purple_dot_preorders").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM purple_dot_preorder_lines").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM purple_dot_preorder_exports").fetchone()[0], 1)
        # incremental runs save a high-water mark, not a resumable cursor
        self.assertIsNotNone(pds._load_state(self.conn, pds.HIGH_WATER_KEY))
        self.assertIsNone(pds._load_state(self.conn, pds.CURSOR_KEY))

    @patch.object(pds, "_count", return_value=None)
    def test_backfill_run_persists_the_page_cursor(self, _mock_count) -> None:
        with patch.object(pds, "_iter_pages",
                          return_value=iter([([SAMPLE_ORDER], "cursor-A")])):
            pds.sync_preorders(self.conn, backfill=True, restart=False,
                               days=None, max_pages=10)
        self.assertEqual(pds._load_state(self.conn, pds.CURSOR_KEY), "cursor-A")
        # backfill runs don't touch the incremental high-water mark
        self.assertIsNone(pds._load_state(self.conn, pds.HIGH_WATER_KEY))

    @patch.object(pds, "_count", return_value=None)
    def test_restart_discards_the_stored_cursor_before_the_run(self, _mock_count) -> None:
        pds._save_state(self.conn, pds.CURSOR_KEY, "stale-cursor")
        with patch.object(pds, "_iter_pages", return_value=iter([])) as mock_pages:
            pds.sync_preorders(self.conn, backfill=True, restart=True,
                               days=None, max_pages=10)
        # the resumed cursor passed to _iter_pages must be None, not "stale-cursor"
        self.assertIsNone(mock_pages.call_args.args[4])

    @patch.object(pds, "_count", return_value=None)
    def test_upsert_is_idempotent_on_preorder_id(self, _mock_count) -> None:
        for _ in range(2):
            with patch.object(pds, "_iter_pages", return_value=iter([([SAMPLE_ORDER], None)])):
                pds.sync_preorders(self.conn, backfill=False, restart=False,
                                   days=30, max_pages=10)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM purple_dot_preorders WHERE id='pd_1'").fetchone()[0]
        self.assertEqual(count, 1)


class SyncWaitlistsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        pds.ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_live_waitlist_gets_an_inventory_snapshot_row(self) -> None:
        with patch.object(pds, "_iter_pages", return_value=iter([([SAMPLE_WAITLIST], None)])):
            n = pds.sync_waitlists(self.conn, max_pages=10)
        self.assertEqual(n, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM purple_dot_waitlists").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM purple_dot_waitlist_inventory").fetchone()[0], 1)

    def test_paused_waitlist_is_not_snapshotted_by_default(self) -> None:
        paused = dict(SAMPLE_WAITLIST, state="Paused")
        with patch.object(pds, "_iter_pages", return_value=iter([([paused], None)])):
            pds.sync_waitlists(self.conn, max_pages=10)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM purple_dot_waitlists").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM purple_dot_waitlist_inventory").fetchone()[0], 0)

    def test_snapshot_all_overrides_the_state_filter(self) -> None:
        paused = dict(SAMPLE_WAITLIST, state="Paused")
        with patch.object(pds, "_iter_pages", return_value=iter([([paused], None)])):
            pds.sync_waitlists(self.conn, max_pages=10, snapshot_all=True)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM purple_dot_waitlist_inventory").fetchone()[0], 1)


class MainSkipTests(unittest.TestCase):
    """main() must degrade to a clean, non-raising skip when the access token
    is missing - never crash a scheduled job for want of credentials."""

    def test_skips_without_access_token(self) -> None:
        with patch.dict(pds.os.environ, {}, clear=False), \
             patch.object(pds.db, "init_db") as mock_init_db:
            pds.os.environ.pop("PURPLE_DOT_ACCESS_TOKEN", None)
            with patch("sys.argv", ["purple_dot_sync.py"]):
                pds.main()
        mock_init_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
