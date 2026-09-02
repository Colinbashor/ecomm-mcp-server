"""Hermetic tests for amazon_awd_sync.py — no network, no real DB file.

Covers: schema creation, per-item row shaping (the field names the API uses
vs. the columns we store them under), the named-column write path, pagination,
retry/backoff behavior, and the required-env-var guard.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import amazon_awd_sync as awd_sync


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
        awd_sync.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_awd_inventory)")}
        self.assertIn("snapshot_date", cols)
        self.assertIn("sku", cols)
        self.assertIn("available_distributable", cols)

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        awd_sync.ensure_schema(conn)
        awd_sync.ensure_schema(conn)  # must not raise


class ParseItemTests(unittest.TestCase):
    def test_full_item_maps_every_field(self) -> None:
        item = {
            "sku": "WIDGET-A-FBA",
            "totalOnhandQuantity": 150,
            "totalInboundQuantity": 7,
            "inventoryDetails": {
                "availableDistributableQuantity": 90,
                "reservedDistributableQuantity": 60,
                "replenishmentQuantity": 30,
            },
        }
        row = awd_sync._parse_item(item, "2026-01-15", "2026-01-15T00:00:00+00:00")
        self.assertEqual(row["sku"], "WIDGET-A-FBA")
        self.assertEqual(row["total_onhand"], 150)
        self.assertEqual(row["total_inbound"], 7)
        self.assertEqual(row["available_distributable"], 90)
        self.assertEqual(row["reserved_distributable"], 60)
        self.assertEqual(row["replenishment_qty"], 30)
        self.assertEqual(row["snapshot_date"], "2026-01-15")

    def test_missing_inventory_details_defaults_to_zero(self) -> None:
        row = awd_sync._parse_item({"sku": "S1"}, "2026-01-15", "stamp")
        self.assertEqual(row["total_onhand"], 0)
        self.assertEqual(row["available_distributable"], 0)
        self.assertEqual(row["reserved_distributable"], 0)
        self.assertEqual(row["replenishment_qty"], 0)

    def test_missing_sku_defaults_to_empty_string(self) -> None:
        row = awd_sync._parse_item({}, "2026-01-15", "stamp")
        self.assertEqual(row["sku"], "")


class FetchAwdTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_REGION"] = "NA"
        self._token_patch = patch.object(awd_sync, "_access_token", return_value="tok")
        self._token_patch.start()

    def tearDown(self) -> None:
        self._token_patch.stop()

    def test_pagination_follows_next_token(self) -> None:
        page1 = _FakeResp(200, {
            "inventory": [{"sku": "A", "totalOnhandQuantity": 1}],
            "nextToken": "tok2",
        })
        page2 = _FakeResp(200, {"inventory": [{"sku": "B", "totalOnhandQuantity": 2}]})
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(dict(params or {}))
            return page1 if len(calls) == 1 else page2

        with patch.object(awd_sync, "requests") as fake_requests, \
                patch.object(awd_sync.time, "sleep"):
            fake_requests.get.side_effect = fake_get
            fake_requests.exceptions = __import__("requests").exceptions
            rows = awd_sync.fetch_awd("2026-01-15")

        self.assertEqual(len(rows), 2)
        self.assertEqual({r["sku"] for r in rows}, {"A", "B"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["nextToken"], "tok2")

    def test_429_is_retried_using_retry_after(self) -> None:
        throttled = _FakeResp(429, {}, {"Retry-After": "0"})
        ok = _FakeResp(200, {"inventory": []})
        with patch.object(awd_sync, "requests") as fake_requests, \
                patch.object(awd_sync.time, "sleep"):
            fake_requests.get.side_effect = [throttled, ok]
            fake_requests.exceptions = __import__("requests").exceptions
            rows = awd_sync.fetch_awd("2026-01-15")
        self.assertEqual(rows, [])

    def test_hard_error_raises(self) -> None:
        bad = _FakeResp(400, {}, {}, text="bad request")
        with patch.object(awd_sync, "requests") as fake_requests:
            fake_requests.get.return_value = bad
            fake_requests.exceptions = __import__("requests").exceptions
            with self.assertRaises(RuntimeError) as cm:
                awd_sync.fetch_awd("2026-01-15")
        self.assertIn("400", str(cm.exception))


class WriteRowsTests(unittest.TestCase):
    def test_write_uses_named_columns_not_positional(self) -> None:
        # A positional INSERT silently shifts every value after an ALTER TABLE
        # appends a column at the table's physical end. Named columns are
        # immune to that class of bug; pin that the query stays named.
        import inspect
        src = inspect.getsource(awd_sync.write_rows)
        self.assertIn("INSERT OR REPLACE INTO amazon_awd_inventory (", src)

    def test_write_and_overwrite_same_key(self) -> None:
        conn = sqlite3.connect(":memory:")
        awd_sync.ensure_schema(conn)
        row = {c: 0 for c in awd_sync._COLUMNS}
        row.update(snapshot_date="2026-01-15", sku="S1", available_distributable=5,
                   synced_at="t1")
        self.assertEqual(awd_sync.write_rows(conn, [row]), 1)
        row2 = dict(row, available_distributable=9, synced_at="t2")
        awd_sync.write_rows(conn, [row2])
        got = conn.execute(
            "SELECT available_distributable FROM amazon_awd_inventory "
            "WHERE snapshot_date=? AND sku=?", ("2026-01-15", "S1")).fetchone()
        self.assertEqual(got[0], 9)

    def test_write_rows_empty_list_is_noop(self) -> None:
        conn = sqlite3.connect(":memory:")
        awd_sync.ensure_schema(conn)
        self.assertEqual(awd_sync.write_rows(conn, []), 0)


class RequireEnvTests(unittest.TestCase):
    def test_missing_vars_raise_systemexit_with_names(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in awd_sync.REQUIRED_ENV}
        try:
            with self.assertRaises(SystemExit) as cm:
                awd_sync.require_env()
            msg = str(cm.exception)
            for k in awd_sync.REQUIRED_ENV:
                self.assertIn(k, msg)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_all_present_does_not_raise(self) -> None:
        saved = {k: os.environ.get(k) for k in awd_sync.REQUIRED_ENV}
        try:
            for k in awd_sync.REQUIRED_ENV:
                os.environ[k] = "x"
            awd_sync.require_env()  # must not raise
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
