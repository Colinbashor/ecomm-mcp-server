"""Tests for the GA4 connector (ga4_sync.py).

Hermetic: no network, no real service-account file. The GA4 client is faked
at the `run_report(request)` boundary (a `_FakeClient`/`_ScriptedClient` below
stand in for `BetaAnalyticsDataClient`), so these exercise the connector's own
logic — pagination past the 100k-row cap, transient-error retry, row shaping,
and stale-row purging — without touching Google's API.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from unittest.mock import patch

from google.api_core import exceptions as gexc

import ga4_sync


# --------------------------------------------------------------------------- #
#  fakes standing in for the google-analytics-data response shape
# --------------------------------------------------------------------------- #
class _Val:
    def __init__(self, value: str) -> None:
        self.value = value


class _Row:
    def __init__(self, dims: list[str], mets: list[str]) -> None:
        self.dimension_values = [_Val(d) for d in dims]
        self.metric_values = [_Val(m) for m in mets]


class _Resp:
    def __init__(self, rows: list[_Row], row_count: int | None = None) -> None:
        self.rows = rows
        self.row_count = row_count if row_count is not None else len(rows)


class _FakeClient:
    """Returns one canned response per call, in order."""

    def __init__(self, pages: list) -> None:
        self._pages = list(pages)
        self.calls: list = []

    def run_report(self, request):
        self.calls.append(request)
        if not self._pages:
            raise AssertionError("no more fake pages queued")
        return self._pages.pop(0)


class _ScriptedClient:
    """Queue of outcomes (a response or an exception instance) per call. The
    last outcome repeats if more calls happen than outcomes were queued."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def run_report(self, request):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _TimeShim:
    """Stands in for the `time` module so retry backoff costs no wall-clock."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _ClockPatchedCase(unittest.TestCase):
    def setUp(self) -> None:
        self._real_time = ga4_sync.time
        self.clock = _TimeShim()
        ga4_sync.time = self.clock

    def tearDown(self) -> None:
        ga4_sync.time = self._real_time


# --------------------------------------------------------------------------- #
#  pure helpers
# --------------------------------------------------------------------------- #
class HelperTests(unittest.TestCase):
    def test_iso_splits_yyyymmdd(self) -> None:
        self.assertEqual(ga4_sync._iso("20260813"), "2026-08-13")

    def test_month_chunks_splits_on_calendar_boundaries(self) -> None:
        chunks = list(ga4_sync._month_chunks("2026-01-15", "2026-03-05"))
        self.assertEqual(chunks, [
            ("2026-01-15", "2026-01-31"),
            ("2026-02-01", "2026-02-28"),
            ("2026-03-01", "2026-03-05"),
        ])

    def test_day_chunks_yields_one_pair_per_day(self) -> None:
        chunks = list(ga4_sync._day_chunks("2026-01-30", "2026-02-01"))
        self.assertEqual(chunks, [
            ("2026-01-30", "2026-01-30"),
            ("2026-01-31", "2026-01-31"),
            ("2026-02-01", "2026-02-01"),
        ])


# --------------------------------------------------------------------------- #
#  pagination + retry (the tricky GA4-specific behavior)
# --------------------------------------------------------------------------- #
class PaginationTests(unittest.TestCase):
    def test_run_pages_past_a_short_first_response(self) -> None:
        # row_count (3) exceeds the first page's row count (2) -> a second
        # request must follow, offset by the rows already seen.
        page1 = _Resp([_Row(["20260101", "A"], ["1"]), _Row(["20260101", "B"], ["2"])], row_count=3)
        page2 = _Resp([_Row(["20260101", "C"], ["3"])], row_count=3)
        client = _FakeClient([page1, page2])

        rows = ga4_sync._run(client, "123", "2026-01-01", "2026-01-01", ["date", "channel"], ["sessions"])

        self.assertEqual(len(rows), 3)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0].offset, 0)
        self.assertEqual(client.calls[1].offset, 2)

    def test_run_stops_on_empty_page(self) -> None:
        page1 = _Resp([_Row(["20260101", "A"], ["1"])], row_count=5)
        page2 = _Resp([], row_count=5)  # GA4 can return short of its own count
        client = _FakeClient([page1, page2])

        rows = ga4_sync._run(client, "123", "2026-01-01", "2026-01-01", ["date", "channel"], ["sessions"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(client.calls), 2)


class RetryTests(_ClockPatchedCase):
    def test_transient_error_is_retried_then_succeeds(self) -> None:
        resp = _Resp([_Row(["20260101", "A"], ["1"])])
        client = _ScriptedClient(gexc.DeadlineExceeded("timeout"), resp)

        rows = ga4_sync._run(client, "123", "2026-01-01", "2026-01-01", ["date", "channel"], ["sessions"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(client.calls, 2)
        self.assertEqual(self.clock.slept, [ga4_sync.RETRY_BASE_SECONDS])

    def test_non_transient_error_is_not_retried(self) -> None:
        client = _ScriptedClient(ValueError("bad metric name"))
        with self.assertRaises(ValueError):
            ga4_sync._run(client, "123", "2026-01-01", "2026-01-01", ["date"], ["sessions"])
        self.assertEqual(client.calls, 1)

    def test_exhausted_retries_raise_the_underlying_error(self) -> None:
        client = _ScriptedClient(gexc.ServiceUnavailable("down"))
        with self.assertRaises(gexc.ServiceUnavailable):
            ga4_sync._run(client, "123", "2026-01-01", "2026-01-01", ["date"], ["sessions"])
        self.assertEqual(client.calls, ga4_sync.RETRY_TRIES)


class ConversionMetricResolutionTests(unittest.TestCase):
    def test_keyevents_used_when_the_probe_succeeds(self) -> None:
        client = _FakeClient([_Resp([_Row(["20260101"], ["1"])])])
        self.assertEqual(
            ga4_sync._resolve_conversion_metric(client, "123", "2026-01-01"), "keyEvents")

    def test_falls_back_to_conversions_when_the_probe_fails(self) -> None:
        client = _ScriptedClient(RuntimeError("keyEvents not supported"))
        self.assertEqual(
            ga4_sync._resolve_conversion_metric(client, "123", "2026-01-01"), "conversions")


# --------------------------------------------------------------------------- #
#  schema + row shaping (against a real in-memory sqlite3 connection)
# --------------------------------------------------------------------------- #
class SchemaAndRowShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        ga4_sync.ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        ga4_sync.ensure_schema(self.conn)  # must not raise on a second call
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"ga_metrics", "ga_products", "ga_landing_pages"} <= tables)

    def test_sync_metrics_writes_expected_row_shape(self) -> None:
        # sessions, totalUsers, conversions, totalRevenue, newUsers,
        # engagedSessions, transactions
        page = _Resp([_Row(["20260101", "Organic Search"],
                            ["120", "100", "5", "250.5", "30", "80", "10"])])
        client = _FakeClient([page])

        n = ga4_sync.sync_metrics(client, self.conn, "999", "2026-01-01", "2026-01-01",
                                   "conversions", "2026-01-01T00:00:00+00:00")
        self.assertEqual(n, 1)

        row = self.conn.execute(
            "SELECT property_id, date, channel, sessions, users, conversions, "
            "revenue, new_users, engaged_sessions, purchases FROM ga_metrics"
        ).fetchone()
        self.assertEqual(row, ("999", "2026-01-01", "Organic Search", 120, 100, 5.0, 250.5, 30, 80, 10))

    def test_sync_products_writes_expected_row_shape(self) -> None:
        # itemsViewed, itemsAddedToCart, itemsPurchased, itemRevenue
        page = _Resp([_Row(["20260101", "ITEM1", "Item One"], ["10", "3", "2", "49.98"])])
        client = _FakeClient([page])

        n = ga4_sync.sync_products(client, self.conn, "999", "2026-01-01", "2026-01-01",
                                    "2026-01-01T00:00:00+00:00")
        self.assertEqual(n, 1)

        row = self.conn.execute(
            "SELECT item_id, item_name, items_viewed, items_added_to_cart, "
            "items_purchased, item_revenue FROM ga_products"
        ).fetchone()
        self.assertEqual(row, ("ITEM1", "Item One", 10, 3, 2, 49.98))

    def test_sync_landing_pages_writes_expected_row_shape(self) -> None:
        # sessions, engagedSessions, conversions, transactions, totalRevenue
        page = _Resp([_Row(["20260101", "/products/foo"], ["50", "40", "2", "3", "89.97"])])
        client = _FakeClient([page])

        n = ga4_sync.sync_landing_pages(client, self.conn, "999", "2026-01-01", "2026-01-01",
                                         "2026-01-01T00:00:00+00:00")
        self.assertEqual(n, 1)

        row = self.conn.execute(
            "SELECT landing_page, sessions, engaged_sessions, conversions, "
            "purchases, revenue FROM ga_landing_pages"
        ).fetchone()
        self.assertEqual(row, ("/products/foo", 50, 40, 2.0, 3, 89.97))

    def test_purge_dates_removes_rows_ga4_stopped_returning(self) -> None:
        # Simulate a prior sync's orphan row (e.g. GA4's '(other)' bucket that
        # later disappeared), then re-sync the same date with a different
        # channel set — the orphan must not survive.
        self.conn.execute(
            "INSERT INTO ga_metrics (property_id, date, channel, synced_at) "
            "VALUES ('999', '2026-01-01', '(other)', 'old')")
        rows = [("999", "2026-01-01", "Organic Search")]  # only date_index matters here
        ga4_sync._purge_dates(self.conn, "ga_metrics", "999", rows, date_index=1)
        remaining = self.conn.execute(
            "SELECT channel FROM ga_metrics WHERE property_id='999' AND date='2026-01-01'"
        ).fetchall()
        self.assertEqual(remaining, [])


# --------------------------------------------------------------------------- #
#  CLI-level failure mode
# --------------------------------------------------------------------------- #
class MainEnvGuardTests(unittest.TestCase):
    def test_missing_env_vars_exit_cleanly_with_a_clear_message(self) -> None:
        with patch.dict(ga4_sync.os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["ga4_sync.py"]):
            with self.assertRaises(SystemExit) as cm:
                ga4_sync.main()
        self.assertIn("GA4_PROPERTY_ID", str(cm.exception))
        self.assertIn("GA4_CREDENTIALS_FILE", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
