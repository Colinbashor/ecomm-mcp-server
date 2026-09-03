"""Hermetic tests for amazon_traffic_sync.py — no network, no real DB file.

Covers: schema creation, the local week/month period-math helpers (no shared
report_period module in this scaffold), row-shaping from a sample
GET_SALES_AND_TRAFFIC_REPORT payload, sync_week/sync_month writing through a
patched `_report`, and the --week Monday-only guard.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import amazon_traffic_sync as traffic


def _sample_report() -> dict:
    return {
        "salesAndTrafficByAsin": [
            {
                "childAsin": "B0001",
                "parentAsin": "B0000",
                "trafficByAsin": {"sessions": 100, "pageViews": 150, "buyBoxPercentage": 87.5},
                "salesByAsin": {"unitsOrdered": 10, "orderedProductSales": {"amount": "199.90"}},
            },
            {
                "childAsin": "B0002",
                "trafficByAsin": {"sessions": 5, "pageViews": 5},
                "salesByAsin": {},
            },
        ],
        "salesAndTrafficByDate": [
            {
                "date": "2026-06-22",
                "trafficByDate": {"sessions": 60, "pageViews": 90},
                "salesByDate": {"unitsOrdered": 6, "orderedProductSales": {"amount": "120.00"},
                                "totalOrderItems": 5},
            },
            {
                "date": "2026-06-23",
                "trafficByDate": {"sessions": 45, "pageViews": 60},
                "salesByDate": {"unitsOrdered": 4, "orderedProductSales": {"amount": "79.90"},
                                "totalOrderItems": 3},
            },
        ],
    }


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_four_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("amazon_traffic_weekly", "amazon_traffic_daily",
                  "amazon_traffic_monthly", "amazon_traffic_monthly_account",
                  "amazon_traffic_coverage"):
            self.assertIn(t, tables)


class PeriodHelperTests(unittest.TestCase):
    def test_week_bounds_is_monday_to_sunday(self) -> None:
        start, end = traffic._week_bounds(date(2026, 6, 22))
        self.assertEqual((start, end), ("2026-06-22", "2026-06-28"))

    def test_month_bounds_normal_month(self) -> None:
        start, end = traffic._month_bounds("2026-06")
        self.assertEqual((start, end), ("2026-06-01", "2026-06-30"))

    def test_month_bounds_february_non_leap(self) -> None:
        start, end = traffic._month_bounds("2026-02")
        self.assertEqual((start, end), ("2026-02-01", "2026-02-28"))

    def test_month_bounds_december_rolls_to_next_year(self) -> None:
        start, end = traffic._month_bounds("2025-12")
        self.assertEqual((start, end), ("2025-12-01", "2025-12-31"))

    def test_recent_mondays_returns_n_mondays_most_recent_first(self) -> None:
        mondays = traffic._recent_mondays(3)
        self.assertEqual(len(mondays), 3)
        for m in mondays:
            self.assertEqual(m.weekday(), 0)
        self.assertGreater(mondays[0], mondays[1])
        self.assertGreater(mondays[1], mondays[2])
        # excludes the currently-running week
        today = date.today()
        this_monday = today - __import__("datetime").timedelta(days=today.weekday())
        self.assertLess(mondays[0], this_monday)


class RowShapingTests(unittest.TestCase):
    def test_weekly_rows_shape_and_defaults(self) -> None:
        rows = traffic._weekly_rows(_sample_report(), "2026-06-22", "stamp")
        self.assertEqual(len(rows), 2)
        r1, r2 = rows
        self.assertEqual(r1[:3], ("2026-06-22", "B0001", "B0000"))
        self.assertEqual(r1[3:5], (100, 150))
        self.assertAlmostEqual(r1[5], 87.5)
        self.assertEqual(r1[6], 10)
        self.assertAlmostEqual(r1[7], 199.90)
        # second ASIN has an empty salesByAsin -> defaults to zero, no crash
        self.assertEqual(r2[1], "B0002")
        self.assertEqual(r2[6], 0)
        self.assertEqual(r2[7], 0.0)

    def test_daily_rows_shape(self) -> None:
        rows = traffic._daily_rows(_sample_report(), "stamp")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("2026-06-22", 60, 90, 6, 120.00, 5, "stamp"))

    def test_monthly_account_row_sums_across_days(self) -> None:
        row = traffic._monthly_account_row(_sample_report(), "2026-06", "stamp")
        self.assertEqual(row, ("2026-06", 105, 150, 10, 199.90, 8, "stamp"))

    def test_empty_report_yields_no_rows(self) -> None:
        self.assertEqual(traffic._weekly_rows({}, "2026-06-22", "stamp"), [])
        self.assertEqual(traffic._daily_rows({}, "stamp"), [])
        row = traffic._monthly_account_row({}, "2026-06", "stamp")
        self.assertEqual(row, ("2026-06", 0, 0, 0, 0.0, 0, "stamp"))


class SyncWeekTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"

    def test_writes_weekly_and_daily_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        with patch.object(traffic, "_report", return_value=_sample_report()):
            n, complete = traffic.sync_week(conn, date(2026, 6, 22), "stamp")
        self.assertEqual(n, 4)  # 2 asins + 2 days
        # The sample fixture only covers 2 of the week's 7 days, so this
        # period is (correctly) reported incomplete even though rows wrote.
        self.assertFalse(complete)
        weekly = conn.execute(
            "SELECT asin, units_ordered FROM amazon_traffic_weekly ORDER BY asin").fetchall()
        self.assertEqual(weekly, [("B0001", 10), ("B0002", 0)])
        daily = conn.execute("SELECT COUNT(*) FROM amazon_traffic_daily").fetchone()[0]
        self.assertEqual(daily, 2)

    def test_rerun_replaces_rather_than_duplicates(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        with patch.object(traffic, "_report", return_value=_sample_report()):
            traffic.sync_week(conn, date(2026, 6, 22), "stamp1")
            traffic.sync_week(conn, date(2026, 6, 22), "stamp2")
        count = conn.execute("SELECT COUNT(*) FROM amazon_traffic_weekly").fetchone()[0]
        self.assertEqual(count, 2)  # still 2 ASINs, not 4


class SyncMonthTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"

    def test_writes_monthly_and_account_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        with patch.object(traffic, "_report", return_value=_sample_report()):
            n, _complete = traffic.sync_month(conn, "2026-06", "stamp")
        self.assertEqual(n, 3)  # 2 asins + 1 account row
        monthly = conn.execute(
            "SELECT asin, units_ordered FROM amazon_traffic_monthly ORDER BY asin").fetchall()
        self.assertEqual(monthly, [("B0001", 10), ("B0002", 0)])
        acct = conn.execute(
            "SELECT sessions, units_ordered FROM amazon_traffic_monthly_account "
            "WHERE month='2026-06'").fetchone()
        self.assertEqual(acct, (105, 10))

    def test_rerun_deletes_stale_month_rows_first(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        with patch.object(traffic, "_report", return_value=_sample_report()):
            traffic.sync_month(conn, "2026-06", "stamp1")
        with patch.object(traffic, "_report", return_value={"salesAndTrafficByAsin": [
            {"childAsin": "B0003", "trafficByAsin": {}, "salesByAsin": {}},
        ], "salesAndTrafficByDate": []}):
            traffic.sync_month(conn, "2026-06", "stamp2")
        asins = {r[0] for r in conn.execute(
            "SELECT asin FROM amazon_traffic_monthly WHERE month='2026-06'")}
        self.assertEqual(asins, {"B0003"})  # old B0001/B0002 rows gone, not accumulated


class CoverageTests(unittest.TestCase):
    def _full_week_report(self) -> dict:
        """A report covering all 7 days of the 2026-06-22..28 week, sections
        agreeing exactly — the baseline complete case."""
        by_date = []
        total = 0.0
        for i in range(7):
            d = (date(2026, 6, 22) + traffic.timedelta(days=i)).isoformat()
            by_date.append({"date": d, "trafficByDate": {"sessions": 10, "pageViews": 15},
                            "salesByDate": {"unitsOrdered": 1,
                                            "orderedProductSales": {"amount": "10.00"},
                                            "totalOrderItems": 1}})
            total += 10.00
        return {
            "salesAndTrafficByAsin": [
                {"childAsin": "B0001", "trafficByAsin": {"sessions": 70, "pageViews": 105},
                 "salesByAsin": {"unitsOrdered": 7,
                                 "orderedProductSales": {"amount": f"{total:.2f}"}}},
            ],
            "salesAndTrafficByDate": by_date,
        }

    def test_full_week_is_complete(self) -> None:
        cov = traffic.coverage(self._full_week_report(), "2026-06-22", "2026-06-28")
        self.assertTrue(cov["is_complete"])
        self.assertEqual(cov["missing_days"], [])

    def test_missing_trailing_day_is_incomplete(self) -> None:
        data = self._full_week_report()
        data["salesAndTrafficByDate"] = data["salesAndTrafficByDate"][:6]  # drop Sunday
        cov = traffic.coverage(data, "2026-06-22", "2026-06-28")
        self.assertFalse(cov["is_complete"])
        self.assertEqual(cov["missing_days"], ["2026-06-28"])

    def test_all_days_present_but_sections_disagree_is_incomplete(self) -> None:
        # Every day present in byDate, but byAsin reports much more revenue —
        # the two-section check must catch this even with a full day count.
        data = self._full_week_report()
        data["salesAndTrafficByAsin"][0]["salesByAsin"]["orderedProductSales"]["amount"] = "999.00"
        cov = traffic.coverage(data, "2026-06-22", "2026-06-28")
        self.assertEqual(cov["missing_days"], [])
        self.assertFalse(cov["is_complete"])

    def test_a_short_pull_never_overwrites_a_stored_complete_week(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        with patch.object(traffic, "_report", return_value=self._full_week_report()):
            n1, complete1 = traffic.sync_week(conn, date(2026, 6, 22), "stamp1")
        self.assertTrue(complete1)

        short = self._full_week_report()
        short["salesAndTrafficByDate"] = short["salesAndTrafficByDate"][:6]
        with patch.object(traffic, "_report", return_value=short):
            n2, complete2 = traffic.sync_week(conn, date(2026, 6, 22), "stamp2")
        self.assertEqual(n2, 0)          # nothing written on top of the good data
        self.assertFalse(complete2)
        weekly = conn.execute(
            "SELECT synced_at FROM amazon_traffic_weekly WHERE asin='B0001'").fetchone()
        self.assertEqual(weekly[0], "stamp1")  # untouched by the short re-pull

    def test_repair_finds_incomplete_weeks_and_skips_complete_ones(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        good_monday = date(2026, 6, 22)
        bad_monday = date(2026, 6, 29)
        with patch.object(traffic, "_report", return_value=self._full_week_report()):
            traffic.sync_week(conn, good_monday, "stamp")
        short = self._full_week_report()
        short["salesAndTrafficByDate"] = short["salesAndTrafficByDate"][:6]
        with patch.object(traffic, "_report", return_value=short):
            traffic.sync_week(conn, bad_monday, "stamp")

        wanted = traffic.weeks_needing_repair(conn, [good_monday, bad_monday])
        self.assertEqual(wanted, [bad_monday])

    def test_repair_gives_up_after_max_attempts(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        conn.execute(
            "INSERT INTO amazon_traffic_coverage VALUES "
            "('week','2026-06-29','2026-06-29','2026-07-05',7,6,'2026-07-05',10,10,0,?,'x')",
            (traffic.MAX_REPAIR_ATTEMPTS,))
        wanted = traffic.weeks_needing_repair(conn, [date(2026, 6, 29)])
        self.assertEqual(wanted, [])

    def test_missing_coverage_row_falls_back_to_day_count_heuristic(self) -> None:
        conn = sqlite3.connect(":memory:")
        traffic.ensure_schema(conn)
        monday = date(2026, 6, 22)
        with conn:
            conn.execute(
                "INSERT INTO amazon_traffic_weekly VALUES ('2026-06-22','B0001',NULL,0,0,0,0,0,'x')")
            conn.execute("INSERT INTO amazon_traffic_daily VALUES ('2026-06-22',0,0,0,0,0,'x')")
        # Only 1 of 7 days stored, and no coverage row at all (pre-dates the
        # feature) -> still flagged for repair via the day-count fallback.
        self.assertEqual(traffic.weeks_needing_repair(conn, [monday]), [monday])


class RunStatusTests(unittest.TestCase):
    def test_failed_takes_priority(self) -> None:
        status, msg = traffic.run_status(5, ["2026-06-22"], ["2026-06-29"], repair=False)
        self.assertEqual(status, "error")
        self.assertIn("2026-06-29", msg)

    def test_partial_without_failure_is_degraded(self) -> None:
        status, msg = traffic.run_status(5, ["2026-06-22"], [], repair=False)
        self.assertEqual(status, "degraded")

    def test_zero_rows_on_a_plain_run_is_an_error(self) -> None:
        status, _ = traffic.run_status(0, [], [], repair=False)
        self.assertEqual(status, "error")

    def test_zero_rows_on_a_repair_run_is_ok(self) -> None:
        # Nothing needed fixing — that is success, not failure.
        status, msg = traffic.run_status(0, [], [], repair=True)
        self.assertEqual(status, "ok")

    def test_all_complete_is_ok(self) -> None:
        status, msg = traffic.run_status(10, [], [], repair=False)
        self.assertEqual((status, msg), ("ok", ""))


class ExitCodeTests(unittest.TestCase):
    """An expected upstream publishing lag must not fail the pipeline."""

    def test_repair_partial_exits_zero(self) -> None:
        """The platform not having published the final day yet is EXPECTED.
        It logs degraded (already visible via sync_log) but must not fail a
        multi-step pipeline that has already rendered/uploaded everything
        that depends on this step."""
        self.assertEqual(traffic.exit_code("degraded", repair=True, allow_partial=False), 0)

    def test_early_pass_partial_still_exits_zero(self) -> None:
        self.assertEqual(traffic.exit_code("degraded", repair=False, allow_partial=True), 0)

    def test_plain_partial_run_is_still_loud(self) -> None:
        """A fresh non-repair pull that came back short with no --allow-partial
        is still a nonzero exit -- that asymmetry is deliberate."""
        self.assertEqual(traffic.exit_code("degraded", repair=False, allow_partial=False), 1)

    def test_a_failed_week_always_exits_nonzero(self) -> None:
        """An exception is a real error and stays loud in every mode."""
        for repair in (True, False):
            for allow in (True, False):
                self.assertEqual(
                    traffic.exit_code("error", repair=repair, allow_partial=allow), 1)

    def test_ok_exits_zero(self) -> None:
        self.assertEqual(traffic.exit_code("ok", repair=True, allow_partial=False), 0)


class RequireEnvTests(unittest.TestCase):
    def test_missing_vars_raise_systemexit(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in traffic.REQUIRED_ENV}
        try:
            with self.assertRaises(SystemExit) as cm:
                traffic.require_env()
            for k in traffic.REQUIRED_ENV:
                self.assertIn(k, str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class MainWeekGuardTests(unittest.TestCase):
    def test_week_flag_must_be_a_monday(self) -> None:
        saved = {k: os.environ.get(k) for k in traffic.REQUIRED_ENV}
        try:
            for k in traffic.REQUIRED_ENV:
                os.environ[k] = "x"
            os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
            fake_conn = sqlite3.connect(":memory:")
            with patch("sys.argv", ["amazon_traffic_sync.py", "--week", "2026-06-23"]), \
                 patch.object(traffic.warehouse_db, "init_db"), \
                 patch.object(traffic.warehouse_db, "now", return_value="stamp"), \
                 patch.object(traffic.warehouse_db, "log_sync"), \
                 patch("sqlite3.connect", return_value=fake_conn):
                with self.assertRaises(SystemExit) as cm:
                    traffic.main()
            self.assertIn("Monday", str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class MainMonthFailureTests(unittest.TestCase):
    def test_month_failure_logs_error_and_exits_nonzero(self) -> None:
        saved = {k: os.environ.get(k) for k in traffic.REQUIRED_ENV}
        try:
            for k in traffic.REQUIRED_ENV:
                os.environ[k] = "x"
            os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
            fake_conn = sqlite3.connect(":memory:")
            logged = {}

            def fake_log_sync(platform, started, rows, status, message=""):
                logged.update(platform=platform, rows=rows, status=status)

            with patch("sys.argv", ["amazon_traffic_sync.py", "--month", "2026-06"]), \
                 patch.object(traffic.warehouse_db, "init_db"), \
                 patch.object(traffic.warehouse_db, "now", return_value="stamp"), \
                 patch.object(traffic.warehouse_db, "log_sync", side_effect=fake_log_sync), \
                 patch("sqlite3.connect", return_value=fake_conn), \
                 patch.object(traffic, "sync_month", side_effect=RuntimeError("boom")):
                rc = traffic.main()
            self.assertEqual(rc, 1)
            self.assertEqual(logged["status"], "error")
            self.assertEqual(logged["platform"], "amazon_traffic_monthly")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
