"""Hermetic tests for flexport_sync.py (catalog + inventory connector).

No network, no real warehouse.db — HTTP is mocked and schema/parsing
functions are exercised against an in-memory SQLite connection.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

import flexport_sync as fx


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_tables_and_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        fx.ensure_schema(conn)
        fx.ensure_schema(conn)  # must not raise on a second call
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("flexport_products", tables)
        self.assertIn("flexport_inventory", tables)
        self.assertIn("flexport_catalog_sync_state", tables)


class UnitConversionTests(unittest.TestCase):
    def test_weight_converted_to_ounces(self) -> None:
        self.assertEqual(fx._to_oz(1, "lb"), 16.0)
        self.assertAlmostEqual(fx._to_oz(1, "kg"), 35.274, places=2)
        self.assertEqual(fx._to_oz(5, None), 5.0)  # defaults to oz
        self.assertIsNone(fx._to_oz(None, "lb"))

    def test_length_converted_to_inches(self) -> None:
        self.assertAlmostEqual(fx._to_in(10, "cm"), 3.937, places=2)
        self.assertEqual(fx._to_in(2, "in"), 2.0)
        self.assertIsNone(fx._to_in(None, "cm"))


class ProductRowTests(unittest.TestCase):
    def test_product_row_normalizes_dims_and_locked_flag(self) -> None:
        p = {
            "logisticsSku": "D123", "merchantSku": "SKU-1", "name": "Widget",
            "barcodes": ["111", "222"], "createdAt": "2024-01-01", "updatedAt": "2024-02-01",
            "dimensions": {"weight": 1, "weightUnit": "lb",
                           "length": 10, "width": 5, "height": 2, "lengthUnit": "in"},
            "dimsLocked": True,
        }
        row = fx._product_row(p, "2024-03-01T00:00:00")
        self.assertEqual(row[0], "D123")
        self.assertEqual(row[1], "SKU-1")
        self.assertEqual(row[3], "111,222")
        self.assertEqual(row[6], 16.0)   # weight_oz
        self.assertEqual(row[7], 10.0)   # length_in
        self.assertEqual(row[10], 1)     # dims_locked

    def test_missing_locked_flag_stays_null_not_false(self) -> None:
        row = fx._product_row({"logisticsSku": "D1"}, "stamp")
        self.assertIsNone(row[10])


class PagedFetchTests(unittest.TestCase):
    """The endpoint under test silently caps its page size below the
    requested limit — _paged must advance by what actually came back, not by
    the limit it asked for, or it will skip rows."""

    def test_advances_by_actual_batch_size_not_requested_limit(self) -> None:
        pages = [
            [{"id": i} for i in range(3)],   # asked for PAGE_SIZE, vendor gave 3
            [{"id": i} for i in range(3, 5)],
            [],
        ]
        calls = []

        def fake_get(path, params):
            calls.append(dict(params))
            return pages.pop(0)

        with patch.object(fx, "_get", side_effect=fake_get), \
             patch.object(fx.time, "sleep"):
            out = fx._paged("/products/inventory/all")

        self.assertEqual(len(out), 5)
        # second call's offset must reflect the 3 rows actually returned, not
        # the PAGE_SIZE that was requested
        self.assertEqual(calls[1]["offset"], 3)
        self.assertEqual(calls[2]["offset"], 5)

    def test_stops_on_empty_page(self) -> None:
        with patch.object(fx, "_get", return_value=[]), patch.object(fx.time, "sleep"):
            self.assertEqual(fx._paged("/anything"), [])


class SyncInventoryTests(unittest.TestCase):
    def test_gap_fills_catalog_for_unknown_skus_and_writes_inventory(self) -> None:
        conn = sqlite3.connect(":memory:")
        fx.ensure_schema(conn)
        # D1 already known; D2 is not, and must be gap-filled via /products/{id}
        conn.execute(fx._PRODUCT_INSERT,
                    ("D1", "MSKU-1", "Known", "", None, None, None, None, None, None, None, "stamp0"))
        conn.commit()

        inventory = [
            {"logisticsSku": "D1", "available": 5, "onHand": 5, "unavailable": 0, "unitsPerPack": 1},
            {"logisticsSku": "D2", "available": 2, "onHand": 3, "unavailable": 1, "unitsPerPack": 1},
        ]

        def fake_get(path, params):
            self.assertEqual(path, "/products/D2")
            return {"logisticsSku": "D2", "merchantSku": "MSKU-2", "name": "Gap"}

        with patch.object(fx, "_paged", return_value=inventory), \
             patch.object(fx, "_get", side_effect=fake_get), \
             patch.object(fx.time, "sleep"):
            n_inv, n_gaps = fx.sync_inventory(conn, "stamp1", "2024-05-01")

        self.assertEqual(n_inv, 2)
        self.assertEqual(n_gaps, 1)
        msku = dict(conn.execute("SELECT logistics_sku, merchant_sku FROM flexport_products"))
        self.assertEqual(msku["D2"], "MSKU-2")
        rows = dict(conn.execute(
            "SELECT logistics_sku, merchant_sku FROM flexport_inventory WHERE snapshot_date='2024-05-01'"))
        self.assertEqual(rows["D2"], "MSKU-2")

    def test_gap_fill_failure_is_logged_and_does_not_abort_the_run(self) -> None:
        conn = sqlite3.connect(":memory:")
        fx.ensure_schema(conn)
        inventory = [{"logisticsSku": "DX", "available": 1, "onHand": 1,
                     "unavailable": 0, "unitsPerPack": 1}]

        def fake_get(path, params):
            raise RuntimeError("Flexport /products/DX 500: boom")

        with patch.object(fx, "_paged", return_value=inventory), \
             patch.object(fx, "_get", side_effect=fake_get), \
             patch.object(fx.time, "sleep"):
            n_inv, n_gaps = fx.sync_inventory(conn, "stamp", "2024-05-01")

        self.assertEqual(n_inv, 1)
        self.assertEqual(n_gaps, 0)  # the failed gap-fill contributed no row


class MainSkipsWithoutTokenTests(unittest.TestCase):
    def test_main_skips_cleanly_when_token_unset(self) -> None:
        with patch.dict(fx.os.environ, {}, clear=True), \
             patch.object(fx.db, "connect") as connect_mock:
            fx.main()
        connect_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
