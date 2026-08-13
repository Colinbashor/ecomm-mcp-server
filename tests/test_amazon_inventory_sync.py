"""Hermetic tests for amazon_inventory_sync.py — no network, no real DB file.

Covers: schema creation, per-summary row shaping (including the nested
inbound/reserved sub-objects), pagination token handling (nextToken lives
OUTSIDE `payload` — see module docstring), and the required-env-var guard.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import amazon_inventory_sync as inv


class _FakeResp:
    def __init__(self, status: int = 200, payload: object | None = None,
                 headers: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self) -> object:
        return self._payload


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        inv.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_inventory)")}
        self.assertIn("snapshot_date", cols)
        self.assertIn("fn_sku", cols)
        self.assertIn("fulfillable", cols)

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        inv.ensure_schema(conn)
        inv.ensure_schema(conn)  # must not raise


class ParseSummaryTests(unittest.TestCase):
    def test_full_summary_maps_every_field(self) -> None:
        summary = {
            "sellerSku": "SKU1-FBA",
            "asin": "B0EXAMPLE1",
            "fnSku": "X0000001",
            "condition": "NewItem",
            "totalQuantity": 42,
            "inventoryDetails": {
                "fulfillableQuantity": 30,
                "inboundWorkingQuantity": 1,
                "inboundShippedQuantity": 2,
                "inboundReceivingQuantity": 3,
                "reservedQuantity": {
                    "pendingCustomerOrderQuantity": 4,
                    "pendingTransshipmentQuantity": 5,
                    "fcProcessingQuantity": 6,
                },
                "unfulfillableQuantity": {"totalUnfulfillableQuantity": 7},
                "researchingQuantity": {"totalResearchingQuantity": 8},
            },
        }
        row = inv._parse_summary(summary, "2026-08-01", "2026-08-01T00:00:00+00:00")
        self.assertEqual(row["sku"], "SKU1-FBA")
        self.assertEqual(row["fn_sku"], "X0000001")
        self.assertEqual(row["fulfillable"], 30)
        self.assertEqual(row["inbound_working"], 1)
        self.assertEqual(row["reserved_orders"], 4)
        self.assertEqual(row["reserved_transfer"], 5)
        self.assertEqual(row["reserved_processing"], 6)
        self.assertEqual(row["unfulfillable"], 7)
        self.assertEqual(row["researching"], 8)
        self.assertEqual(row["total"], 42)
        self.assertEqual(row["snapshot_date"], "2026-08-01")

    def test_missing_nested_objects_default_to_zero(self) -> None:
        row = inv._parse_summary({"sellerSku": "S1"}, "2026-08-01", "stamp")
        self.assertEqual(row["fulfillable"], 0)
        self.assertEqual(row["reserved_orders"], 0)
        self.assertEqual(row["total"], 0)
        self.assertIsNone(row["asin"])


class FetchInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"
        self._token_patch = patch.object(inv, "_access_token", return_value="tok")
        self._token_patch.start()

    def tearDown(self) -> None:
        self._token_patch.stop()

    def test_pagination_token_read_from_top_level_not_payload(self) -> None:
        # The gotcha this test pins: nextToken lives OUTSIDE `payload`.
        page1 = _FakeResp(200, {
            "payload": {"inventorySummaries": [{"sellerSku": "A", "totalQuantity": 1}]},
            "pagination": {"nextToken": "tok2"},
        })
        page2 = _FakeResp(200, {
            "payload": {"inventorySummaries": [{"sellerSku": "B", "totalQuantity": 2}]},
        })
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(dict(params or {}))
            return page1 if len(calls) == 1 else page2

        with patch.object(inv, "requests") as fake_requests, patch.object(inv.time, "sleep"):
            fake_requests.get.side_effect = fake_get
            fake_requests.exceptions = __import__("requests").exceptions
            rows = inv.fetch_inventory()

        self.assertEqual(len(rows), 2)
        self.assertEqual({r["sku"] for r in rows}, {"A", "B"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["nextToken"], "tok2")

    def test_429_is_retried(self) -> None:
        throttled = _FakeResp(429, {}, {"Retry-After": "0"})
        ok = _FakeResp(200, {"payload": {"inventorySummaries": []}})
        with patch.object(inv, "requests") as fake_requests, patch.object(inv.time, "sleep"):
            fake_requests.get.side_effect = [throttled, ok]
            fake_requests.exceptions = __import__("requests").exceptions
            rows = inv.fetch_inventory()
        self.assertEqual(rows, [])

    def test_hard_error_raises(self) -> None:
        bad = _FakeResp(500, {}, {}, text="boom")
        with patch.object(inv, "requests") as fake_requests:
            fake_requests.get.return_value = bad
            fake_requests.exceptions = __import__("requests").exceptions
            with self.assertRaises(RuntimeError) as cm:
                inv.fetch_inventory()
        self.assertIn("500", str(cm.exception))


class WriteRowsTests(unittest.TestCase):
    def test_write_and_overwrite_same_key(self) -> None:
        conn = sqlite3.connect(":memory:")
        inv.ensure_schema(conn)
        row = {c: None for c in inv._COLUMNS}
        row.update(snapshot_date="2026-08-01", sku="S1", total=5, synced_at="t1")
        self.assertEqual(inv.write_rows(conn, [row]), 1)
        row2 = dict(row, total=9, synced_at="t2")
        inv.write_rows(conn, [row2])
        got = conn.execute("SELECT total FROM amazon_inventory WHERE snapshot_date=? AND sku=?",
                           ("2026-08-01", "S1")).fetchone()
        self.assertEqual(got[0], 9)

    def test_write_rows_empty_list_is_noop(self) -> None:
        conn = sqlite3.connect(":memory:")
        inv.ensure_schema(conn)
        self.assertEqual(inv.write_rows(conn, []), 0)


class RequireEnvTests(unittest.TestCase):
    def test_missing_vars_raise_systemexit_with_names(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in inv.REQUIRED_ENV}
        try:
            with self.assertRaises(SystemExit) as cm:
                inv.require_env()
            msg = str(cm.exception)
            for k in inv.REQUIRED_ENV:
                self.assertIn(k, msg)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_all_present_does_not_raise(self) -> None:
        saved = {k: os.environ.get(k) for k in inv.REQUIRED_ENV}
        try:
            for k in inv.REQUIRED_ENV:
                os.environ[k] = "x"
            inv.require_env()  # must not raise
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
