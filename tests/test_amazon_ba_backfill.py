"""Hermetic tests for amazon_ba_backfill.py — no network, no real DB file.

Covers: the status-reporting helpers (stored_weeks / topic_rows), and the
consecutive-miss stopping logic in main() — the part of this script that
exists specifically because an out-of-retention week can come back FATAL
rather than the more obviously-terminal CANCELLED (see the module docstring
and warehouse/brand_analytics.py), so a walk-back can't simply wait for one
specific exception type to know it has reached the floor.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import amazon_ba_backfill as backfill
import amazon_ba_sync as ba


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(ba.DDL)
    return conn


def _insert_week(conn, week_start: str, match_reason: str, n: int = 1) -> None:
    for i in range(n):
        conn.execute(
            "INSERT OR REPLACE INTO amazon_ba_search_terms VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (week_start, "Apparel", f"term{i}", 1, f"ASIN{i}", "name", 1,
             10.0, 5.0, match_reason, "stamp"),
        )
    conn.commit()


class StatusHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_stored_weeks_is_empty_on_a_fresh_db(self) -> None:
        self.assertEqual(backfill.stored_weeks(self.conn), set())

    def test_stored_weeks_reflects_distinct_week_starts(self) -> None:
        _insert_week(self.conn, "2026-07-19", "ours")
        _insert_week(self.conn, "2026-07-26", "rank")
        self.assertEqual(backfill.stored_weeks(self.conn),
                         {"2026-07-19", "2026-07-26"})

    def test_topic_rows_counts_only_topic_prefixed_reasons(self) -> None:
        _insert_week(self.conn, "2026-07-19", "ours", n=2)
        _insert_week(self.conn, "2026-07-26", "topic:widgets", n=3)
        self.assertEqual(backfill.topic_rows(self.conn), 3)

    def test_status_does_not_raise_on_an_empty_table(self) -> None:
        backfill.status(self.conn)  # must not raise


class WalkBackStoppingTests(unittest.TestCase):
    """The consecutive-miss counter is the whole reason this script isn't
    just a thin wrapper around amazon_ba_sync.sync_search_terms in a loop."""

    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _run(self, weekly_returns, weeks=10, start="2026-07-19"):
        """weekly_returns: list of row-counts sync_search_terms should return,
        oldest-call-last (walk-back order), reused/extended with 0s if the
        walk asks for more weeks than provided."""
        calls = {"n": 0}

        def fake_sync(conn, wk, stamp, our_asins):
            i = calls["n"]
            calls["n"] += 1
            n = weekly_returns[i] if i < len(weekly_returns) else 0
            if n:
                _insert_week(conn, wk.isoformat(), "ours", n=n)
            return n

        argv = ["amazon_ba_backfill.py", "--start", start, "--weeks", str(weeks),
                "--asins-file", "does-not-matter.txt"]
        with patch("sys.argv", argv), \
             patch.object(backfill.time, "sleep"), \
             patch.object(ba, "run_ba_report"), \
             patch.object(backfill.BA, "_target_asins", return_value=[]), \
             patch.object(backfill.BA, "sync_search_terms", side_effect=fake_sync), \
             patch.object(backfill, "sqlite3") as sqlite3_mod:
            sqlite3_mod.connect.return_value = self.conn
            with patch.object(backfill.warehouse_db, "init_db"):
                backfill.main()
        return calls["n"]

    def test_stops_after_max_consecutive_misses(self) -> None:
        # Two productive weeks, then nothing but misses -- must stop at
        # MAX_CONSECUTIVE_MISSES rather than walking all the way to --weeks.
        returns = [5, 5] + [0] * backfill.MAX_CONSECUTIVE_MISSES
        calls = self._run(returns, weeks=30)
        self.assertEqual(calls, len(returns),
                         "must not keep walking past MAX_CONSECUTIVE_MISSES "
                         "consecutive empty weeks")

    def test_a_hit_between_misses_resets_the_counter(self) -> None:
        # One miss, then a hit, then MAX_CONSECUTIVE_MISSES misses -- the
        # intervening hit must reset the counter, not merely pause it.
        returns = ([0] * (backfill.MAX_CONSECUTIVE_MISSES - 1) + [3]
                   + [0] * backfill.MAX_CONSECUTIVE_MISSES)
        calls = self._run(returns, weeks=30)
        self.assertEqual(calls, len(returns))

    def test_already_stored_weeks_are_skipped_without_calling_sync(self) -> None:
        _insert_week(self.conn, "2026-07-19", "ours")
        calls = self._run([5], weeks=1, start="2026-07-19")
        self.assertEqual(calls, 0, "a week already stored must cost zero API calls")


if __name__ == "__main__":
    unittest.main()
