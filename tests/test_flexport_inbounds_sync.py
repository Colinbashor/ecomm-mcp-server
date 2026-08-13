"""Hermetic tests for flexport_inbounds_sync.py (supplier receiving feed)."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import Mock, patch

import flexport_inbounds_sync as fi


def _resp(body, status: int = 200) -> Mock:
    m = Mock()
    m.status_code = status
    m.json.return_value = body
    m.headers = {}
    m.text = ""
    return m


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_tables_and_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        fi.ensure_schema(conn)
        fi.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("flexport_inbounds", tables)
        self.assertIn("flexport_inbound_lines", tables)


class OffsetPaginationTests(unittest.TestCase):
    """Unlike the orders/returns endpoints, this one has no Link cursor at
    all — plain offset paging that stops on a short/empty page."""

    def test_walks_offset_until_short_page(self) -> None:
        full_page = [{"id": f"S{i}"} for i in range(fi.PAGE)]
        short_page = [{"id": "SLAST"}]
        calls = []

        def fake_request(path, params):
            calls.append(dict(params))
            return _resp(full_page if params["offset"] == 0 else short_page)

        with patch.object(fi, "_request", side_effect=fake_request), patch.object(fi.time, "sleep"):
            recs = list(fi.iter_shipments(max_pages=10))

        self.assertEqual(len(recs), fi.PAGE + 1)
        self.assertEqual([c["offset"] for c in calls], [0, fi.PAGE])

    def test_stops_immediately_on_empty_first_page(self) -> None:
        with patch.object(fi, "_request", return_value=_resp([])), patch.object(fi.time, "sleep"):
            self.assertEqual(list(fi.iter_shipments(max_pages=10)), [])

    def test_skips_records_without_an_id(self) -> None:
        page = [{"id": "OK"}, {"no": "id here"}, None]
        with patch.object(fi, "_request", return_value=_resp(page)), patch.object(fi.time, "sleep"):
            recs = list(fi.iter_shipments(max_pages=1))
        self.assertEqual(recs, [{"id": "OK"}])


class RowBuildingTests(unittest.TestCase):
    def test_rows_for_shipment_sums_line_counts(self) -> None:
        rec = {
            "id": "SHIP-1", "receivingId": "R1", "status": "ARRIVED",
            "shippingPlanId": "PLAN-1", "shippingPlanExternalId": "PO-100",
            "shippingPlanName": "Plan Name", "shippingOption": "FREIGHT_EXTERNAL",
            "shipmentDestination": "NETWORK", "bookingId": "BOOK-1",
            "addresses": {"from": {"name": "Supplier Co", "countryCode": "CN"},
                         "to": {"name": "Warehouse 1"}},
            "arrivedAt": "2024-02-01", "completedAt": "2024-02-10",
            "packages": [{}, {}],
            "items": [
                {"shipmentItemId": "I1", "merchantSku": "MSKU-1", "logisticsSku": "D1",
                 "packOfDsku": None, "lineItemId": "L1",
                 "counts": {"expected": 10, "sellable": 8, "damaged": 2}},
                {"shipmentItemId": "I2", "merchantSku": "MSKU-2", "logisticsSku": "D2",
                 "packOfDsku": None, "lineItemId": "L2",
                 "counts": {"expected": 5, "sellable": 5, "damaged": 0}},
            ],
        }
        ship_row, line_rows = fi.rows_for_shipment(rec, "stamp")
        self.assertEqual(ship_row[0], "SHIP-1")
        self.assertEqual(ship_row[4], "PO-100")   # shipping_plan_external_id
        self.assertEqual(ship_row[14], 2)         # n_lines
        self.assertEqual(ship_row[15], 2)         # n_packages
        self.assertEqual(ship_row[16], 15)        # expected_units summed
        self.assertEqual(ship_row[17], 13)        # sellable_units summed
        self.assertEqual(ship_row[18], 2)         # damaged_units summed
        self.assertEqual(len(line_rows), 2)
        self.assertEqual(line_rows[0][2], "MSKU-1")

    def test_missing_counts_default_to_zero(self) -> None:
        rec = {"id": "S1", "items": [{"shipmentItemId": "I1"}]}
        ship_row, line_rows = fi.rows_for_shipment(rec, "stamp")
        self.assertEqual(ship_row[16], 0)
        self.assertEqual(line_rows[0][6], 0)


class UpsertPageTests(unittest.TestCase):
    def test_replaces_lines_for_a_shipment_when_item_set_shrinks(self) -> None:
        conn = sqlite3.connect(":memory:")
        fi.ensure_schema(conn)
        first = {"id": "S1", "items": [
            {"shipmentItemId": "I1", "counts": {"expected": 1, "sellable": 1, "damaged": 0}},
            {"shipmentItemId": "I2", "counts": {"expected": 1, "sellable": 1, "damaged": 0}},
        ]}
        fi.upsert_page(conn, [first], "stamp1")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM flexport_inbound_lines WHERE inbound_id='S1'").fetchone()[0], 2)

        shrunk = {"id": "S1", "items": [
            {"shipmentItemId": "I1", "counts": {"expected": 1, "sellable": 1, "damaged": 0}},
        ]}
        fi.upsert_page(conn, [shrunk], "stamp2")
        remaining = conn.execute(
            "SELECT shipment_item_id FROM flexport_inbound_lines WHERE inbound_id='S1'").fetchall()
        self.assertEqual([r[0] for r in remaining], ["I1"])  # I2 must not linger


class RunPauseTests(unittest.TestCase):
    def test_transient_failure_pauses_but_keeps_already_committed_pages(self) -> None:
        conn = sqlite3.connect(":memory:")
        fi.ensure_schema(conn)

        def raising_iter(max_pages):
            yield {"id": "S1", "items": []}
            raise fi.FlexportTransient("degraded")

        with patch.object(fi, "iter_shipments", side_effect=lambda m: raising_iter(m)):
            n_ships, n_lines, paused = fi.run(conn, max_pages=10)

        self.assertTrue(paused)
        self.assertEqual(n_ships, 0)  # buffered page (<PAGE) never flushed before the raise
        # nothing committed yet since the buffered page never reached PAGE size
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM flexport_inbounds").fetchone()[0], 0)

    def test_clean_run_upserts_all_shipments(self) -> None:
        conn = sqlite3.connect(":memory:")
        fi.ensure_schema(conn)
        recs = [{"id": f"S{i}", "items": []} for i in range(3)]
        with patch.object(fi, "iter_shipments", return_value=iter(recs)):
            n_ships, n_lines, paused = fi.run(conn, max_pages=10)
        self.assertEqual(n_ships, 3)
        self.assertFalse(paused)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM flexport_inbounds").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
