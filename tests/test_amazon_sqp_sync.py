"""Hermetic tests for amazon_sqp_sync.py — no network, no real DB file.

Covers: report-row shaping, the ASIN-sourcing helper (explicit --asins /
--asins-file / fallback, capped by --max-asins), and the cost-control behavior
in sync_ba_week (probe-before-fanout, budgeted halving, bounded concurrency,
coverage/resume, wall-clock budget) — the same four rules the module docstring
describes as load-bearing for keeping one Brand Analytics week from costing
hundreds of report requests.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import amazon_sqp_sync
from warehouse.brand_analytics import BAReportFatal

WEEK = date(2026, 7, 19)
STAMP = "2026-07-28T00:00:00+00:00"


def _asins(n: int, prefix: str = "A") -> list[str]:
    return [f"{prefix}{i:04d}" for i in range(n)]


def _record(asin: str) -> dict:
    """Minimal SQP record — enough for _rows_from_report to emit one row."""
    return {
        "asin": asin,
        "searchQueryData": {"searchQuery": "example search term", "searchQueryVolume": 10},
        "impressionData": {"totalQueryImpressionCount": 100, "asinImpressionCount": 10,
                           "asinImpressionShare": 10.0},
        "clickData": {}, "cartAddData": {}, "purchaseData": {},
    }


class RowShapingTests(unittest.TestCase):
    def test_row_shape_from_a_full_record(self) -> None:
        rec = {
            "asin": "B1",
            "searchQueryData": {"searchQuery": "widget", "searchQueryScore": 5,
                                "searchQueryVolume": 1000},
            "impressionData": {"totalQueryImpressionCount": 500, "asinImpressionCount": 50,
                               "asinImpressionShare": 10.0},
            "clickData": {"totalClickCount": 40, "asinClickCount": 4, "asinClickShare": 10.0,
                         "totalMedianClickPrice": {"amount": "19.99"},
                         "asinMedianClickPrice": {"amount": "18.00"}},
            "cartAddData": {"totalCartAddCount": 8, "asinCartAddCount": 1, "asinCartAddShare": 12.5},
            "purchaseData": {"totalPurchaseCount": 5, "asinPurchaseCount": 1, "asinPurchaseShare": 20.0},
        }
        rows = amazon_sqp_sync._rows_from_report([rec], "2026-07-19", "stamp")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[0:3], ("2026-07-19", "B1", "widget"))
        self.assertEqual(row[3], 5)      # query_score
        self.assertEqual(row[4], 1000)   # query_volume
        self.assertEqual(row[7], 10.0)   # impression_share (percent, as-is)
        self.assertEqual(row[11], 19.99)  # median_click_price_total

    def test_rows_missing_asin_or_query_are_skipped(self) -> None:
        rows = amazon_sqp_sync._rows_from_report(
            [{"searchQueryData": {"searchQuery": "x"}}, {"asin": "B1"}], "2026-07-19", "stamp")
        self.assertEqual(rows, [])

    def test_null_share_coalesces_to_zero(self) -> None:
        rec = {"asin": "B1", "searchQueryData": {"searchQuery": "x"},
              "impressionData": {}, "clickData": {}, "cartAddData": {}, "purchaseData": {}}
        row = amazon_sqp_sync._rows_from_report([rec], "2026-07-19", "stamp")[0]
        self.assertEqual(row[7], 0.0)


class TargetAsinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_explicit_asins_flag_is_parsed_and_capped(self) -> None:
        args = argparse.Namespace(asins="B1, B2, B3", asins_file=None, max_asins=2)
        self.assertEqual(amazon_sqp_sync._target_asins(args, self.conn), ["B1", "B2"])

    def test_asins_file_is_read_in_order(self) -> None:
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "w") as f:
                f.write("B1\nB2\n\nB3\n")
            args = argparse.Namespace(asins=None, asins_file=path, max_asins=0)
            self.assertEqual(amazon_sqp_sync._target_asins(args, self.conn), ["B1", "B2", "B3"])
        finally:
            os.remove(path)

    def test_falls_back_when_neither_flag_given(self) -> None:
        with patch.object(amazon_sqp_sync, "fallback_asins", return_value=["FB1", "FB2"]):
            args = argparse.Namespace(asins=None, asins_file=None, max_asins=0)
            self.assertEqual(amazon_sqp_sync._target_asins(args, self.conn), ["FB1", "FB2"])

    def test_zero_max_asins_means_no_cap(self) -> None:
        args = argparse.Namespace(asins="B1,B2,B3", asins_file=None, max_asins=0)
        self.assertEqual(len(amazon_sqp_sync._target_asins(args, self.conn)), 3)


class SyncBaWeekBasicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(amazon_sqp_sync.DDL)

    def tearDown(self) -> None:
        self.conn.close()

    def test_no_asins_is_a_clean_ok_noop(self) -> None:
        written, skipped, state = amazon_sqp_sync.sync_ba_week(self.conn, WEEK, STAMP, [])
        self.assertEqual((written, skipped, state), (0, 0, amazon_sqp_sync.STATE_OK))

    def test_irrecoverable_single_asin_is_reported_as_skipped(self) -> None:
        """A lone bad ASIN still isolates: with only one batch there is no
        fan-out to gate, so it goes through the normal halving path."""
        with (
            patch.object(amazon_sqp_sync, "create_ba_report", return_value="rid"),
            patch.object(
                amazon_sqp_sync,
                "check_ba_report",
                return_value=("FATAL", "bad payload"),
            ),
            patch.object(amazon_sqp_sync.time, "sleep"),
        ):
            written, skipped, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, ["BAD-ASIN"],
            )

        self.assertEqual(written, 0)
        self.assertEqual(skipped, 1)
        self.assertEqual(state, amazon_sqp_sync.STATE_OK)


class AmazonSqpCostControlTests(unittest.TestCase):
    """The four cost controls that keep one week from costing hundreds of
    reports (see the module docstring)."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(amazon_sqp_sync.DDL)
        self.sleep = patch.object(amazon_sqp_sync.time, "sleep").start()
        self.addCleanup(patch.stopall)
        self._ids = iter(f"rid{i}" for i in range(10_000))

    def _unique_id(self, *a, **kw) -> str:
        """Amazon returns a distinct reportId per create; never reuse one in a
        test or every batch collapses onto a single in-flight entry."""
        return next(self._ids)

    def tearDown(self) -> None:
        self.conn.close()

    def test_unpublished_week_costs_two_reports_not_a_fan_out(self) -> None:
        """An unpublished BA week FATALs every batch. The probe must condemn the
        week after 2 reports instead of halving 10 batches into ~230."""
        calls = []

        def fatal(*a, **kw):
            calls.append(a)
            raise BAReportFatal("A client error occurred")

        with (
            patch.object(amazon_sqp_sync, "run_ba_report", side_effect=fatal),
            patch.object(amazon_sqp_sync, "create_ba_report") as create,
        ):
            written, skipped, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(120),
            )

        self.assertEqual(state, amazon_sqp_sync.STATE_UNAVAILABLE)
        self.assertEqual(written, 0)
        self.assertEqual(len(calls), 2, "probe must stop after two disjoint slices")
        create.assert_not_called()  # no batch reports were ever submitted

    def test_probe_recovers_when_only_the_lead_batch_is_poisoned(self) -> None:
        """One bad ASIN in the highest-priority batch must not be mistaken for an
        unpublished week — the second, disjoint probe slice proves it published."""
        outcomes = [BAReportFatal("poison ASIN"), [_record("A0060")]]

        def probe(*a, **kw):
            got = outcomes.pop(0)
            if isinstance(got, Exception):
                raise got
            return got

        with (
            patch.object(amazon_sqp_sync, "run_ba_report", side_effect=probe),
            patch.object(amazon_sqp_sync, "create_ba_report", return_value="rid"),
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("CANCELLED", None),
            ),
        ):
            written, _, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(120),
            )

        self.assertEqual(state, amazon_sqp_sync.STATE_OK)
        self.assertEqual(written, 1)

    def test_halving_is_capped_by_the_retry_budget(self) -> None:
        """Past MAX_RETRY_REPORTS a failing batch is skipped wholesale rather than
        split, so total reports for a week stay bounded."""
        with (
            patch.object(amazon_sqp_sync, "run_ba_report", return_value=[_record("A0060")]),
            patch.object(
                amazon_sqp_sync, "create_ba_report", side_effect=self._unique_id,
            ) as create,
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("FATAL", "nope"),
            ),
        ):
            _, skipped, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(120),
            )

        # 9 remaining batches + at most MAX_RETRY_REPORTS retry reports. The old
        # unbounded halving would have been ~9 x 23 = 207.
        self.assertLessEqual(create.call_count, 9 + amazon_sqp_sync.MAX_RETRY_REPORTS)
        self.assertEqual(skipped, 108, "every un-probed ASIN accounted for")
        self.assertEqual(state, amazon_sqp_sync.STATE_OK)

    def test_retry_budget_scales_with_batch_count(self) -> None:
        """A deep run over your whole ASIN list must not be starved by a budget
        sized for a small weekly cohort, but must still stay ~2x its batch count."""
        with (
            patch.object(amazon_sqp_sync, "run_ba_report", return_value=[_record("A0600")]),
            patch.object(
                amazon_sqp_sync, "create_ba_report", side_effect=self._unique_id,
            ) as create,
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("FATAL", "nope"),
            ),
        ):
            amazon_sqp_sync.sync_ba_week(self.conn, WEEK, STAMP, _asins(1200))

        batches = 100
        self.assertGreater(create.call_count, batches, "budget scaled up")
        self.assertLessEqual(create.call_count, 2 * batches)

    def test_dead_week_is_abandoned_after_max_dead_batches(self) -> None:
        """Probe passed but nothing else lands: bail instead of grinding through
        every remaining batch."""
        with (
            patch.object(amazon_sqp_sync, "run_ba_report", return_value=[]),
            patch.object(amazon_sqp_sync, "create_ba_report", side_effect=self._unique_id),
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("FATAL", "nope"),
            ),
        ):
            written, _, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(600),
            )

        self.assertEqual(written, 0)
        self.assertEqual(state, amazon_sqp_sync.STATE_ABORTED)

    def test_in_flight_reports_are_capped(self) -> None:
        """Concurrency must stay under createReport's burst bucket."""
        peak = 0
        live: set[str] = set()
        counter = iter(range(1000))

        def create(*a, **kw):
            nonlocal peak
            rid = f"rid{next(counter)}"
            live.add(rid)
            peak = max(peak, len(live))
            return rid

        def check(rid):
            live.discard(rid)
            return "DONE", f"doc-{rid}"

        with (
            patch.object(amazon_sqp_sync, "run_ba_report", return_value=[]),
            patch.object(amazon_sqp_sync, "create_ba_report", side_effect=create),
            patch.object(amazon_sqp_sync, "check_ba_report", side_effect=check),
            patch.object(amazon_sqp_sync, "fetch_ba_records", return_value=[]),
        ):
            amazon_sqp_sync.sync_ba_week(self.conn, WEEK, STAMP, _asins(240))

        self.assertGreater(peak, 1, "batches must actually overlap")
        self.assertLessEqual(peak, amazon_sqp_sync.MAX_IN_FLIGHT)

    def test_coverage_lets_a_killed_run_resume(self) -> None:
        """Attempted (week, ASIN) pairs are skipped on the next run — and the
        marker is coverage, NOT the presence of amazon_sqp rows, because the
        report omits ASINs that had no search data."""
        amazon_sqp_sync._record_coverage(
            self.conn, WEEK.isoformat(), _asins(108), "done", STAMP)

        with (
            patch.object(amazon_sqp_sync, "run_ba_report") as probe,
            patch.object(
                amazon_sqp_sync, "create_ba_report", side_effect=self._unique_id,
            ) as create,
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("CANCELLED", None),
            ),
        ):
            _, _, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(120),
            )

        self.assertEqual(state, amazon_sqp_sync.STATE_OK)
        # Only the 12 un-attempted ASINs are re-requested: one batch, and with a
        # single batch there is no fan-out to gate so the probe is skipped.
        self.assertEqual(create.call_count, 1)
        probe.assert_not_called()

    def test_fully_covered_week_does_no_work(self) -> None:
        amazon_sqp_sync._record_coverage(
            self.conn, WEEK.isoformat(), _asins(120), "done", STAMP)

        with patch.object(amazon_sqp_sync, "run_ba_report") as run:
            written, _, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(120),
            )

        self.assertEqual(state, amazon_sqp_sync.STATE_DONE_ALREADY)
        self.assertEqual(written, 0)
        run.assert_not_called()

    def test_refresh_ignores_coverage(self) -> None:
        amazon_sqp_sync._record_coverage(
            self.conn, WEEK.isoformat(), _asins(120), "done", STAMP)

        with (
            patch.object(amazon_sqp_sync, "run_ba_report", return_value=[]) as run,
            patch.object(amazon_sqp_sync, "create_ba_report", return_value="rid"),
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("CANCELLED", None),
            ),
        ):
            _, _, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(120), refresh=True,
            )

        self.assertEqual(state, amazon_sqp_sync.STATE_OK)
        run.assert_called()

    def test_wall_clock_budget_stops_cleanly(self) -> None:
        with (
            patch.object(amazon_sqp_sync, "run_ba_report", return_value=[]),
            patch.object(amazon_sqp_sync, "create_ba_report", return_value="rid") as create,
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("PENDING", None),
            ),
        ):
            written, _, state = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(240),
                deadline=amazon_sqp_sync.time.time() - 1,  # already expired
            )

        self.assertEqual(state, amazon_sqp_sync.STATE_TIMEOUT)
        self.assertEqual(written, 0)
        create.assert_not_called()

    def test_cancelled_batch_counts_as_covered_not_skipped(self) -> None:
        """CANCELLED means 'no data for these ASINs', which is an answer — it must
        not be retried forever."""
        with (
            patch.object(amazon_sqp_sync, "run_ba_report", return_value=[]),
            patch.object(amazon_sqp_sync, "create_ba_report", return_value="rid"),
            patch.object(
                amazon_sqp_sync, "check_ba_report", return_value=("CANCELLED", None),
            ),
        ):
            _, skipped, _ = amazon_sqp_sync.sync_ba_week(
                self.conn, WEEK, STAMP, _asins(24),
            )

        self.assertEqual(skipped, 0)
        covered = amazon_sqp_sync._attempted_asins(self.conn, WEEK.isoformat())
        self.assertEqual(len(covered), 24)


if __name__ == "__main__":
    unittest.main()
