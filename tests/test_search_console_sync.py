"""Google Search Console connector.

Hermetic: no network, no warehouse.db. What is pinned here is deliberately not
"does it parse JSON" but the handful of decisions that, if silently reversed,
would produce a plausible-looking WRONG number instead of an error:

  * stopping pagination on a SHORT page (the per-day cap returns exactly 5,000
    against a rowLimit of 25,000, so short != finished),
  * letting `data_state` into the primary key, which would leave a partial
    `all` row sitting beside the `final` one that should have replaced it,
  * alarming on days inside the finalization lag, or conversely NOT alarming
    on a settled day that came back empty.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import unittest
from unittest import mock

import search_console_sync as scs


def _row(keys, clicks=1, impressions=10, ctr=0.1, position=5.0):
    return {"keys": list(keys), "clicks": clicks, "impressions": impressions,
            "ctr": ctr, "position": position}


class FakeSession:
    """Returns a scripted list of pages, one per _post call."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - mirrors requests
        self.calls.append(json)
        body = {"rows": self.pages.pop(0)} if self.pages else {}
        return mock.Mock(status_code=200, json=lambda: body, text="")


class PaginationTests(unittest.TestCase):
    """The 5,000-row per-DAY cap is why short-page-means-done is wrong here."""

    def test_stops_only_on_an_empty_page_not_a_short_one(self):
        # 5,000 rows against a rowLimit of 25,000 is exactly what a capped day
        # looks like. Treating that as the end would silently truncate it.
        session = FakeSession([
            [_row(("2026-08-25", f"q{i}", "MOBILE")) for i in range(5000)],
            [_row(("2026-08-25", "extra", "DESKTOP"))],
            [],
        ])
        rows = scs.query_rows(session, start=dt.date(2026, 8, 25),
                              end=dt.date(2026, 8, 25), dimensions=scs.QUERY_DIMS,
                              search_type="web", data_state="final")
        self.assertEqual(len(rows), 5001,
                         "a short page must not end pagination")
        self.assertEqual(len(session.calls), 3)

    def test_start_row_advances_by_rows_received(self):
        session = FakeSession([[_row(("d", "q", "MOBILE"))] * 3, []])
        scs.query_rows(session, start=dt.date(2026, 8, 25), end=dt.date(2026, 8, 25),
                       dimensions=scs.QUERY_DIMS, search_type="web",
                       data_state="final")
        self.assertEqual([c["startRow"] for c in session.calls], [0, 3])

    def test_empty_first_page_returns_nothing_without_extra_calls(self):
        session = FakeSession([[]])
        rows = scs.query_rows(session, start=dt.date(2026, 8, 25),
                              end=dt.date(2026, 8, 25), dimensions=["date"],
                              search_type="web", data_state="final")
        self.assertEqual(rows, [])
        self.assertEqual(len(session.calls), 1)

    def test_request_carries_type_and_data_state(self):
        session = FakeSession([[]])
        scs.query_rows(session, start=dt.date(2026, 8, 25), end=dt.date(2026, 8, 25),
                       dimensions=["date"], search_type="image", data_state="final")
        sent = session.calls[0]
        self.assertEqual(sent["type"], "image")
        self.assertEqual(sent["dataState"], "final")
        self.assertEqual(sent["rowLimit"], scs.ROW_LIMIT)


class ErrorClassificationTests(unittest.TestCase):
    """A revoked property grant must fail LOUDLY and immediately, not burn the
    retry budget looking like a flaky backend."""

    def _session(self, status, text="boom"):
        session = mock.Mock()
        session.post.return_value = mock.Mock(status_code=status, text=text,
                                              json=lambda: {})
        return session

    def test_403_is_permanent_and_names_the_property_grant(self):
        session = self._session(403, "not a verified Search Console site")
        with self.assertRaises(scs.SearchConsoleError) as ctx:
            scs._post(session, "/searchAnalytics/query", {})
        self.assertIn("Users and permissions", str(ctx.exception))
        self.assertEqual(session.post.call_count, 1, "must not retry a 403")

    def test_404_is_permanent(self):
        session = self._session(404)
        with self.assertRaises(scs.SearchConsoleError):
            scs._post(session, "/searchAnalytics/query", {})
        self.assertEqual(session.post.call_count, 1)

    def test_400_is_permanent_not_retried(self):
        # A bad dimension/date is never going to succeed on attempt six.
        session = self._session(400)
        with self.assertRaises(scs.SearchConsoleError):
            scs._post(session, "/searchAnalytics/query", {})
        self.assertEqual(session.post.call_count, 1)

    def test_503_is_transient_and_exhausts_the_budget(self):
        session = self._session(503)
        with mock.patch.object(scs.time, "sleep"):
            with self.assertRaises(scs.SearchConsoleTransient):
                scs._post(session, "/searchAnalytics/query", {})
        self.assertEqual(session.post.call_count, scs.MAX_RETRIES)

    def test_transient_then_success_returns_the_body(self):
        session = mock.Mock()
        session.post.side_effect = [
            mock.Mock(status_code=503, text="", json=lambda: {}),
            mock.Mock(status_code=200, text="", json=lambda: {"rows": [_row(("d",))]}),
        ]
        with mock.patch.object(scs.time, "sleep"):
            body = scs._post(session, "/searchAnalytics/query", {})
        self.assertEqual(len(body["rows"]), 1)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(scs.DDL)

    def tearDown(self):
        self.conn.close()

    def test_data_state_is_not_in_any_primary_key(self):
        """A partial `all` row must be REPLACED by the later `final` one. If
        data_state were part of the key they would coexist and every SUM would
        double-count the fresh day."""
        for table in ("search_console_daily", "search_console_queries",
                      "search_console_pages"):
            pk = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")
                  if r[5]]
            self.assertNotIn("data_state", pk, f"{table} must not key on data_state")

    def test_final_pull_overwrites_a_partial_row(self):
        ins = ("INSERT OR REPLACE INTO search_console_daily "
               "(date, site, search_type, clicks, impressions, ctr, position, "
               " data_state, synced_at) VALUES (?,?,?,?,?,?,?,?,?)")
        self.conn.execute(ins, ("2026-09-02", "s", "web", 2079, 30689, 0.06, 8.0,
                                "all", "t1"))
        self.conn.execute(ins, ("2026-09-02", "s", "web", 14124, 165136, 0.08, 8.0,
                                "final", "t2"))
        rows = self.conn.execute(
            "SELECT clicks, data_state FROM search_console_daily").fetchall()
        self.assertEqual(rows, [(14124, "final")],
                         "the partial row must be replaced, not kept alongside")

    def test_query_grain_keys_include_device(self):
        """Device is part of the key because the same query on two devices is
        two legitimate rows; collapsing them would drop real tail volume."""
        pk = [r[1] for r in self.conn.execute("PRAGMA table_info(search_console_queries)")
              if r[5]]
        self.assertIn("device", pk)
        self.assertIn("query", pk)


class CoverageTests(unittest.TestCase):
    """`degraded` must fire on a real gap and stay silent during the lag."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(scs.DDL)

    def tearDown(self):
        self.conn.close()

    def _store(self, day, impressions=100000):
        self.conn.execute(
            "INSERT OR REPLACE INTO search_console_daily "
            "(date, site, search_type, clicks, impressions, ctr, position, "
            " data_state, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (day.isoformat(), scs.SITE, "web", 1, impressions, 0.1, 8.0,
             "final", "t"))

    def test_recent_days_inside_the_lag_are_never_reported_missing(self):
        """`final` lags a few days, so today and yesterday being empty is
        NORMAL. Alarming on them would make the status meaningless within a
        week."""
        days = [dt.date.today() - dt.timedelta(days=n) for n in (0, 1)]
        self.assertEqual(scs.missing_settled_days(self.conn, days), [])

    def test_settled_day_with_no_data_is_reported(self):
        day = dt.date.today() - dt.timedelta(days=10)
        self.assertEqual(scs.missing_settled_days(self.conn, [day]),
                         [day.isoformat()])

    def test_settled_day_with_data_is_clean(self):
        day = dt.date.today() - dt.timedelta(days=10)
        self._store(day)
        self.assertEqual(scs.missing_settled_days(self.conn, [day]), [])

    def test_a_stored_but_empty_day_still_counts_as_missing(self):
        """impressions=0 on a site with meaningful organic volume is not a
        quiet day, it is a failed pull that happened to write a row."""
        day = dt.date.today() - dt.timedelta(days=10)
        self._store(day, impressions=0)
        self.assertEqual(scs.missing_settled_days(self.conn, [day]),
                         [day.isoformat()])

    def test_days_below_retention_are_not_reported_missing(self):
        """Aged-out history is permanently gone; reporting it every night would
        turn the status permanently red for something nobody can fix."""
        day = scs.retention_floor() - dt.timedelta(days=30)
        self.assertEqual(scs.missing_settled_days(self.conn, [day]), [])

    def test_days_below_the_OBSERVED_floor_are_not_reported_missing(self):
        """RETENTION_DAYS over-reaches the real floor on purpose, so the slack
        region returns nothing forever. Flagging it would make a real backfill
        log `degraded` for aged-out days that can never come back."""
        oldest = dt.date.today() - dt.timedelta(days=470)
        self._store(oldest)
        aged_out = oldest - dt.timedelta(days=5)   # inside the assumed floor
        self.assertGreaterEqual(aged_out, scs.retention_floor(),
                                "test only meaningful inside the assumed window")
        self.assertEqual(scs.missing_settled_days(self.conn, [aged_out]), [])

    def test_a_real_gap_above_the_observed_floor_is_still_reported(self):
        """The exclusion must not swallow genuine holes in the middle."""
        oldest = dt.date.today() - dt.timedelta(days=470)
        self._store(oldest)
        hole = oldest + dt.timedelta(days=30)
        self.assertEqual(scs.missing_settled_days(self.conn, [hole]),
                         [hole.isoformat()])


class ResumeTests(unittest.TestCase):
    """A killed backfill must resume, not re-fetch hundreds of days of API
    calls, and must never skip a day whose rows did not actually commit."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(scs.DDL)
        self.settled = dt.date.today() - dt.timedelta(days=20)

    def tearDown(self):
        self.conn.close()

    def _run(self, days, pages_per_day, refresh=False):
        session = FakeSession(pages_per_day)
        written = scs.store_detail(self.conn, session, days, "queries", "final",
                                   "stamp", refresh=refresh)
        return written, session

    def test_completed_day_is_skipped_on_a_second_run(self):
        day = self.settled
        self._run([day], [[_row((day.isoformat(), "q", "MOBILE"))], []])
        _, session = self._run([day], [[_row((day.isoformat(), "q", "MOBILE"))], []])
        self.assertEqual(session.calls, [],
                         "a covered day must cost zero API calls on re-run")

    def test_refresh_ignores_coverage(self):
        day = self.settled
        self._run([day], [[_row((day.isoformat(), "q", "MOBILE"))], []])
        _, session = self._run([day], [[_row((day.isoformat(), "q", "MOBILE"))], []],
                               refresh=True)
        self.assertTrue(session.calls, "--refresh must re-fetch")

    def test_unsettled_day_is_never_treated_as_complete(self):
        """Today's data is still finalizing, so it must be re-pulled tomorrow
        even though a row was already stored for it."""
        today = dt.date.today()
        self._run([today], [[_row((today.isoformat(), "q", "MOBILE"))], []])
        row = self.conn.execute(
            "SELECT was_settled FROM search_console_coverage").fetchone()
        self.assertEqual(row[0], 0)
        _, session = self._run([today], [[_row((today.isoformat(), "q", "MOBILE"))], []])
        self.assertTrue(session.calls, "an unsettled day must be re-pulled")

    def test_empty_settled_day_is_recorded_so_it_is_not_re_asked_forever(self):
        day = self.settled
        self._run([day], [[]])
        cov = self.conn.execute(
            "SELECT rows_stored, was_settled FROM search_console_coverage").fetchone()
        self.assertEqual(cov, (0, 1))
        _, session = self._run([day], [[]])
        self.assertEqual(session.calls, [])

    def test_coverage_is_seeded_from_data_that_predates_the_table(self):
        """A warehouse holding rows but no coverage must not re-fetch them."""
        day = self.settled
        self.conn.execute(
            "INSERT INTO search_console_queries (date, site, query, device, "
            "search_type, clicks, impressions, ctr, position, data_state, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (day.isoformat(), scs.SITE, "q", "MOBILE", "web", 1, 2, 0.5, 3.0,
             "final", "t"))
        self.conn.commit()
        _, session = self._run([day], [[_row((day.isoformat(), "q", "MOBILE"))], []])
        self.assertEqual(session.calls, [],
                         "already-stored history must seed coverage, not re-fetch")

    def test_a_partial_day_row_is_not_seeded_as_complete(self):
        """dataState='all' rows are PARTIAL -- seeding them would freeze partial
        numbers in place permanently."""
        day = self.settled
        self.conn.execute(
            "INSERT INTO search_console_queries (date, site, query, device, "
            "search_type, clicks, impressions, ctr, position, data_state, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (day.isoformat(), scs.SITE, "q", "MOBILE", "web", 1, 2, 0.5, 3.0,
             "all", "t"))
        self.conn.commit()
        seeded = scs.seed_coverage_from_data(self.conn, "queries")
        self.assertEqual(seeded, 0)

    def test_coverage_and_rows_commit_together(self):
        """If the day's rows are there, its coverage row must be too -- they
        share one transaction precisely so this can never diverge."""
        days = [self.settled, self.settled + dt.timedelta(days=1)]
        self._run(days, [[_row((days[0].isoformat(), "a", "MOBILE"))], [],
                         [_row((days[1].isoformat(), "b", "MOBILE"))], []])
        stored = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT date FROM search_console_queries")}
        covered = {r[0] for r in self.conn.execute(
            "SELECT date FROM search_console_coverage")}
        self.assertEqual(stored, covered)

    def test_grains_have_independent_coverage(self):
        """Pages completing must never mark queries done for that day."""
        day = self.settled
        session = FakeSession([[_row((day.isoformat(), "/p"))], []])
        scs.store_detail(self.conn, session, [day], "pages", "final", "stamp")
        self.assertEqual(scs.covered_days(self.conn, "queries"), set())
        self.assertEqual(scs.covered_days(self.conn, "pages"), {day.isoformat()})


class DateTests(unittest.TestCase):
    def test_date_range_is_inclusive_at_both_ends(self):
        got = scs.date_range(dt.date(2026, 8, 30), dt.date(2026, 9, 1))
        self.assertEqual(got, [dt.date(2026, 8, 30), dt.date(2026, 8, 31),
                               dt.date(2026, 9, 1)])

    def test_single_day_range(self):
        d = dt.date(2026, 8, 30)
        self.assertEqual(scs.date_range(d, d), [d])

    def test_retention_floor_is_inside_the_slack_window(self):
        """Over-reaching is free (200 with zero rows); under-reaching silently
        abandons history that can never be re-fetched."""
        days = (dt.date.today() - scs.retention_floor()).days
        self.assertGreaterEqual(days, 490)
        self.assertLessEqual(days, 550)


class RowParsingTests(unittest.TestCase):
    def test_keys_are_padded_not_index_errored(self):
        self.assertEqual(scs._keys({"keys": ["a"]}, 3), ["a", "", ""])

    def test_keys_are_truncated_to_the_requested_width(self):
        self.assertEqual(scs._keys({"keys": ["a", "b", "c", "d"]}, 2), ["a", "b"])

    def test_missing_metrics_default_to_zero(self):
        self.assertEqual(scs._metrics({}), (0, 0, 0.0, 0.0))

    def test_metric_order_matches_the_insert_columns(self):
        got = scs._metrics(_row(("d",), clicks=3, impressions=40, ctr=0.075,
                                position=6.5))
        self.assertEqual(got, (3, 40, 0.075, 6.5))


class QueryDimTests(unittest.TestCase):
    def test_query_dims_lead_with_date(self):
        """Requests go one day at a time because the 5,000 cap is per DAY; the
        date dimension is what keeps each stored row attributable to its day."""
        self.assertEqual(scs.QUERY_DIMS[0], "date")
        self.assertEqual(scs.PAGE_DIMS[0], "date")

    def test_detail_types_are_a_subset_of_daily_types(self):
        self.assertTrue(set(scs.DETAIL_TYPES) <= set(scs.DAILY_TYPES))


if __name__ == "__main__":
    unittest.main()
