"""Hermetic tests for amazon_event_pull.py -- no network, in-memory DB."""
from __future__ import annotations

import sqlite3
import unittest

import amazon_event_pull as ev
import amazon_traffic_sync as t


def _report(days=("2026-03-10", "2026-03-11")) -> dict:
    return {
        "salesAndTrafficByAsin": [
            {"childAsin": "B1", "parentAsin": "B0",
             "trafficByAsin": {"sessions": 10, "pageViews": 12, "buyBoxPercentage": 90.5},
             "salesByAsin": {"unitsOrdered": 3, "orderedProductSales": {"amount": "30.00"}}},
            {"childAsin": "B2", "trafficByAsin": {}, "salesByAsin": {}},
        ],
        "salesAndTrafficByDate": [
            {"date": d, "trafficByDate": {"sessions": 5, "pageViews": 6},
             "salesByDate": {"unitsOrdered": 1, "orderedProductSales": {"amount": "15.00"},
                             "totalOrderItems": 1}} for d in days],
    }


class EventPullTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ev.ensure_schema(self.conn)

    def _store(self, data, event_id="e1", notes=""):
        cov = t.coverage(data, "2026-03-10", "2026-03-11")
        note = ev.store_event(
            self.conn, event_id=event_id, name="Sale", start="2026-03-10", end="2026-03-11",
            channel="amazon", notes=notes, cov=cov,
            asins=ev.asin_rows(event_id, data, "2026-03-10", "2026-03-11", "s"),
            daily=ev.daily_rows(data, "s"), stamp="s")
        return cov, note

    def test_row_shaping_handles_missing_sections(self):
        rows = ev.asin_rows("e1", _report(), "a", "b", "s")
        self.assertEqual(rows[0][3:8], (10, 12, 90.5, 3, 30.0))
        self.assertEqual(rows[1][3:8], (0, 0, 0.0, 0, 0.0))

    def test_store_writes_event_tables_without_weekly_coverage(self):
        cov, _ = self._store(_report())
        c = self.conn
        self.assertEqual(c.execute("SELECT COUNT(*) FROM amazon_event_asin").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM amazon_traffic_daily").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT is_complete FROM amazon_event_pull").fetchone()[0],
                         int(cov["is_complete"]))
        self.assertEqual(c.execute("SELECT channel FROM dim_event_calendar").fetchone()[0], "amazon")

    def test_incomplete_window_is_recorded_not_hidden(self):
        cov, note = self._store(_report(days=("2026-03-10",)))
        self.assertFalse(cov["is_complete"])
        self.assertTrue(note.startswith("INCOMPLETE"))
        row = self.conn.execute(
            "SELECT days_returned, days_expected, missing_days FROM amazon_event_pull").fetchone()
        self.assertEqual(row, (1, 2, "2026-03-11"))

    def test_rerun_replaces_event_rows(self):
        self._store(_report())
        self._store(_report())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM amazon_event_asin").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
