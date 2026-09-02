"""Tests for amazon_awd.py, the shared read-only AWD reader.

What these pin, in order of how expensive the mistake would be:
  * `available_distributable` is the ONLY bucket that counts as extra stock —
    `reserved_distributable` and `replenishment_qty` are already committed to
    FBA and would double-count a stock position if summed in.
  * MISSING must never read as ZERO — a report printing 0 AWD units before
    the feed has ever run would recreate the exact blind spot it exists to
    close.
  * The ASIN bridge only trusts SKUs it can actually map through
    `amazon_inventory` — it never guesses.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import amazon_awd as awd  # noqa: E402
import amazon_awd_sync as awd_sync  # noqa: E402

TODAY = "2026-01-15"


def _conn(with_table: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    if with_table:
        conn.executescript(awd_sync.DDL)
    return conn


def _add(conn, sku, snapshot=TODAY, onhand=0, inbound=0, available=0,
         reserved=0, replenishment=0):
    conn.execute(
        "INSERT OR REPLACE INTO amazon_awd_inventory "
        "(snapshot_date, sku, total_onhand, total_inbound, "
        " available_distributable, reserved_distributable, replenishment_qty, "
        " synced_at) VALUES (?,?,?,?,?,?,?,?)",
        (snapshot, sku, onhand, inbound, available, reserved, replenishment,
         snapshot + "T00:00:00Z"))
    conn.commit()


class AvailabilityTests(unittest.TestCase):
    """The double-count rule: only available_distributable is extra stock."""

    def test_available_excludes_reserved_and_replenishment(self) -> None:
        conn = _conn()
        # Shape taken from the API: on-hand = available + reserved, with a
        # separate in-transit-to-FBA quantity that isn't part of on-hand.
        _add(conn, "S1-FBA", onhand=150, available=90, reserved=60,
             replenishment=30)
        pool = awd.load_latest(conn, as_of=TODAY)
        self.assertEqual(pool["available"], 90)
        self.assertEqual(pool["onhand"], 150)
        self.assertEqual(pool["reserved"], 60)
        self.assertEqual(pool["replenishment"], 30)
        # THE assertion: the accessor callers use returns 90, not 150.
        self.assertEqual(awd.available(pool, "S1-FBA"), 90)

    def test_available_for_skus_dedupes_before_summing(self) -> None:
        conn = _conn()
        _add(conn, "S1-FBA", onhand=100, available=100)
        _add(conn, "S2-FBA", onhand=40, available=40)
        pool = awd.load_latest(conn, as_of=TODAY)
        self.assertEqual(
            awd.available_for_skus(pool, ["S1-FBA", "S2-FBA", "S1-FBA"]), 140)

    def test_available_for_skus_tolerates_none_and_unknown(self) -> None:
        conn = _conn()
        _add(conn, "S1-FBA", onhand=10, available=10)
        pool = awd.load_latest(conn, as_of=TODAY)
        self.assertEqual(awd.available_for_skus(pool, [None, "NOPE", "S1-FBA"]), 10)

    def test_available_for_unknown_sku_is_zero(self) -> None:
        conn = _conn()
        _add(conn, "S1-FBA", onhand=10, available=10)
        pool = awd.load_latest(conn, as_of=TODAY)
        self.assertEqual(awd.available(pool, "NOPE"), 0)
        self.assertEqual(awd.available(pool, None), 0)


class MissingIsNotZeroTests(unittest.TestCase):
    """A blank AWD column must be distinguishable from 'AWD holds nothing'."""

    def test_absent_table_returns_missing_status_not_a_crash(self) -> None:
        conn = _conn(with_table=False)
        pool = awd.load_latest(conn)
        self.assertEqual(pool["status"], awd.STATUS_MISSING)
        self.assertEqual(pool["available"], 0)
        self.assertEqual(pool["by_sku"], {})

    def test_absent_table_note_says_no_data_not_zero(self) -> None:
        conn = _conn(with_table=False)
        note = awd.note(awd.load_latest(conn))
        self.assertIn("NO DATA", note)
        self.assertIn("NOT zero", note)

    def test_empty_table_is_missing_not_ok(self) -> None:
        pool = awd.load_latest(_conn())
        self.assertEqual(pool["status"], awd.STATUS_MISSING)

    def test_stale_snapshot_is_flagged_loudly(self) -> None:
        conn = _conn()
        old = (date.fromisoformat(TODAY) - timedelta(days=awd.STALE_DAYS + 4)).isoformat()
        _add(conn, "S1-FBA", snapshot=old, onhand=10, available=10)
        pool = awd.load(conn, old, as_of=TODAY)
        self.assertEqual(pool["status"], awd.STATUS_STALE)
        self.assertIn("STALE", awd.note(pool))

    def test_fresh_snapshot_is_ok_and_note_states_the_exclusions(self) -> None:
        conn = _conn()
        _add(conn, "S1-FBA", onhand=150, available=90, reserved=60, replenishment=30)
        pool = awd.load_latest(conn, as_of=TODAY)
        self.assertEqual(pool["status"], awd.STATUS_OK)
        note = awd.note(pool)
        self.assertIn("EXCLUDED", note)
        self.assertNotIn("STALE", note)


class SnapshotSelectionTests(unittest.TestCase):
    def test_latest_snapshot_respects_as_of(self) -> None:
        conn = _conn()
        _add(conn, "S1-FBA", snapshot="2026-01-01", onhand=5, available=5)
        _add(conn, "S1-FBA", snapshot=TODAY, onhand=99, available=99)
        self.assertEqual(awd.latest_snapshot(conn), TODAY)
        self.assertEqual(awd.latest_snapshot(conn, "2026-01-05"), "2026-01-01")
        self.assertEqual(awd.available(awd.load_latest(conn, "2026-01-05"), "S1-FBA"), 5)

    def test_prior_snapshot_matches_the_wow_rule(self) -> None:
        conn = _conn()
        _add(conn, "S1-FBA", snapshot="2026-01-08", onhand=5, available=5)
        _add(conn, "S1-FBA", snapshot=TODAY, onhand=9, available=9)
        self.assertEqual(awd.prior_snapshot(conn, TODAY), "2026-01-08")

    def test_prior_snapshot_none_when_nothing_in_tolerance(self) -> None:
        conn = _conn()
        _add(conn, "S1-FBA", snapshot="2025-12-01", onhand=5, available=5)
        _add(conn, "S1-FBA", snapshot=TODAY, onhand=9, available=9)
        self.assertIsNone(awd.prior_snapshot(conn, TODAY))


class AsinBridgeTests(unittest.TestCase):
    """AWD carries no ASIN; amazon_inventory is the bridge."""

    def _with_inventory(self):
        conn = _conn()
        conn.executescript("""
            CREATE TABLE amazon_inventory (
                snapshot_date TEXT, sku TEXT, asin TEXT, fn_sku TEXT);
        """)
        conn.execute("INSERT INTO amazon_inventory VALUES (?,?,?,?)",
                     (TODAY, "S1-FBA", "B001", "X1"))
        conn.execute("INSERT INTO amazon_inventory VALUES (?,?,?,?)",
                     (TODAY, "S2-FBA", "B002", "X2"))
        conn.commit()
        return conn

    def test_maps_available_units_to_asins(self) -> None:
        conn = self._with_inventory()
        _add(conn, "S1-FBA", onhand=100, available=80, reserved=20)
        _add(conn, "S2-FBA", onhand=40, available=40)
        got = awd.by_asin(conn, awd.load_latest(conn, as_of=TODAY))
        # 80 not 100 — reserved units are already committed to FBA.
        self.assertEqual(got, {"B001": 80, "B002": 40})

    def test_sku_with_no_inventory_row_is_dropped_not_guessed(self) -> None:
        conn = self._with_inventory()
        _add(conn, "S9-FBA", onhand=10, available=10)
        self.assertEqual(awd.by_asin(conn, awd.load_latest(conn, as_of=TODAY)), {})

    def test_empty_pool_short_circuits(self) -> None:
        conn = self._with_inventory()
        self.assertEqual(awd.by_asin(conn, awd.empty()), {})

    def test_two_skus_mapping_to_the_same_asin_are_summed(self) -> None:
        conn = self._with_inventory()
        conn.execute("INSERT INTO amazon_inventory VALUES (?,?,?,?)",
                     (TODAY, "S1-UPC", "B001", "X1b"))
        conn.commit()
        _add(conn, "S1-FBA", onhand=10, available=10)
        _add(conn, "S1-UPC", onhand=5, available=5)
        got = awd.by_asin(conn, awd.load_latest(conn, as_of=TODAY))
        self.assertEqual(got, {"B001": 15})


class TableExistsTests(unittest.TestCase):
    def test_view_counts_as_present(self) -> None:
        # A type='table'-only filter would wrongly treat a view-backed source
        # as absent — this project has been bitten by that class of bug
        # before on a different table.
        conn = _conn(with_table=False)
        conn.executescript(
            "CREATE TABLE _real (snapshot_date TEXT, sku TEXT, total_onhand INT, "
            "total_inbound INT, available_distributable INT, "
            "reserved_distributable INT, replenishment_qty INT, synced_at TEXT); "
            "CREATE VIEW amazon_awd_inventory AS SELECT * FROM _real;"
        )
        self.assertTrue(awd.table_exists(conn))


if __name__ == "__main__":
    unittest.main()
