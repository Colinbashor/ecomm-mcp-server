"""warehouse_health: three severities, an escalation clock, configurable lists.

Each escalation test pins one half of the design, and the halves pull in
opposite directions on purpose:
  * an expected pause must NOT go red     (or the signal is noise)
  * a pause that ages out MUST go red     (or the fix is just suppression)

Hermetic: in-memory or temp-file SQLite only.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import warehouse_health as wh
from warehouse import db

SYNC_LOG_DDL = (
    "CREATE TABLE sync_log "
    "(id INTEGER PRIMARY KEY, platform TEXT, started_at TEXT, "
    " finished_at TEXT, rows_written INTEGER, status TEXT, message TEXT)"
)


def _log(conn, rows):
    conn.executemany(
        "INSERT INTO sync_log "
        "(platform, started_at, finished_at, rows_written, status, message) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )


def _run(platform, day, status="ok", rows=100, message=""):
    return (platform, f"{day}T04:00:00Z", f"{day}T05:00:00Z", rows, status,
            message)


class _Conn(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(SYNC_LOG_DDL)

    def tearDown(self) -> None:
        self.conn.close()


class FreshnessTests(_Conn):
    def setUp(self) -> None:
        super().setUp()
        self.conn.execute("CREATE TABLE feed (day TEXT)")
        self.spec = (wh.FreshnessSpec("test", "SELECT MAX(day) FROM feed", 2),)

    def test_fresh_feed_has_no_issue(self) -> None:
        self.conn.execute("INSERT INTO feed VALUES ('2026-07-27')")
        self.assertEqual(
            wh.check_freshness(self.conn, date(2026, 7, 28), self.spec), [])

    def test_stale_feed_is_error(self) -> None:
        self.conn.execute("INSERT INTO feed VALUES ('2026-07-20')")
        issues = wh.check_freshness(self.conn, date(2026, 7, 28), self.spec)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, wh.ERROR)
        self.assertIn("8 days old", issues[0].message)

    def test_a_feed_that_never_produced_anything_says_so(self) -> None:
        issues = wh.check_freshness(self.conn, date(2026, 7, 28), self.spec)
        self.assertIn("never recorded", issues[0].message)

    def test_a_broken_query_is_an_error_not_a_crash(self) -> None:
        spec = (wh.FreshnessSpec("bad", "SELECT MAX(x) FROM no_such_table", 2),)
        issues = wh.check_freshness(self.conn, date(2026, 7, 28), spec)
        self.assertIn("query failed", issues[0].message)

    def test_unseen_platform_spec_is_skipped_only_when_asked(self) -> None:
        spec = (wh.FreshnessSpec("t", "SELECT MAX(day) FROM feed", 2,
                                 platform="google"),)
        self.assertEqual(wh.check_freshness(
            self.conn, date(2026, 7, 28), spec, seen=set()), [])
        self.assertEqual(len(wh.check_freshness(
            self.conn, date(2026, 7, 28), spec, seen=None)), 1)


class FlexportReturnsFreshnessTests(unittest.TestCase):
    """The returns spec must read return ACTIVITY, never synced_at: a walk
    that keeps re-reading one old page refreshes synced_at every day while no
    new return ever lands."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE flexport_returns (id INTEGER PRIMARY KEY, status TEXT, "
            "shipped_at TEXT, received_at TEXT, inspected_at TEXT, synced_at TEXT)")
        self.spec = tuple(s for s in wh.parse_config().freshness
                          if s.name == "flexport_returns")
        self.assertEqual(len(self.spec), 1)

    def tearDown(self) -> None:
        self.conn.close()

    def test_freshly_synced_but_stale_activity_is_flagged(self) -> None:
        self.conn.execute(
            "INSERT INTO flexport_returns VALUES (1, 'PROCESSED', NULL, "
            "'2026-07-23T14:56:12.000Z', '2026-07-23T18:54:04.000Z', "
            "'2026-10-14T12:01:16+00:00')")
        issues = wh.check_freshness(self.conn, date(2026, 10, 14), self.spec)
        self.assertEqual(issues[0].severity, wh.ERROR)
        self.assertIn("2026-07-23", issues[0].message)

    def test_placeholder_dates_never_count_as_activity(self) -> None:
        self.conn.execute(
            "INSERT INTO flexport_returns VALUES (1, 'CREATED', "
            "'0000-00-00 00:00:00', NULL, NULL, '2026-10-14T12:00:00+00:00')")
        issues = wh.check_freshness(self.conn, date(2026, 10, 14), self.spec)
        self.assertIn("never recorded", issues[0].message)


class StatusEscalationTests(_Conn):
    def test_latest_status_uses_start_time_for_overlapping_runs(self) -> None:
        _log(self.conn, [
            ("flexport", "2026-07-28T16:30:00-07:00", "2026-07-28T17:05:00-07:00",
             0, "error", "older run failed late"),
            ("flexport", "2026-07-28T16:41:00-07:00", "2026-07-28T16:48:00-07:00",
             10, "ok", ""),
        ])
        self.assertEqual(wh.check_recent_statuses(
            self.conn, date(2026, 7, 28), platforms=("flexport",)), [])

    def test_recent_pause_is_degraded_not_error(self) -> None:
        _log(self.conn, [_run("flexport_orders", "2026-09-08"),
                         _run("flexport_orders", "2026-09-10", "degraded", 0,
                              "upstream 500")])
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("flexport_orders",))
        self.assertEqual(issues[0].severity, wh.DEGRADED)

    def test_pause_past_the_clock_escalates_to_error(self) -> None:
        """The half that stops this being mere suppression."""
        _log(self.conn, [_run("flexport_inbounds", "2026-08-25"),
                         _run("flexport_inbounds", "2026-09-10", "degraded", 0,
                              "upstream 504")])
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("flexport_inbounds",))
        self.assertEqual(issues[0].severity, wh.ERROR)
        self.assertIn("16 days", issues[0].message)

    def test_escalation_clock_is_configurable(self) -> None:
        _log(self.conn, [_run("x", "2026-09-07"),
                         _run("x", "2026-09-10", "degraded", 0)])
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("x",),
            escalate_after_days=2)
        self.assertEqual(issues[0].severity, wh.ERROR)

    def test_never_successful_feed_escalates_from_its_first_run(self) -> None:
        _log(self.conn, [_run("new", "2026-09-01", "degraded", 0),
                         _run("new", "2026-09-10", "degraded", 0)])
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("new",))
        self.assertEqual(issues[0].severity, wh.ERROR)

    def test_error_status_is_never_tolerated(self) -> None:
        _log(self.conn, [_run("tiktok", "2026-09-10", "error", 0, "boom")])
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("tiktok",))
        self.assertEqual(issues[0].severity, wh.ERROR)

    def test_a_feed_that_stopped_running_is_reported(self) -> None:
        """A check that skips old platforms goes silent exactly when a feed
        dies."""
        _log(self.conn, [_run("gmc_pricing", "2026-09-01")])
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("gmc_pricing",))
        self.assertEqual(issues[0].severity, wh.ERROR)
        self.assertIn("no run in 9 days", issues[0].message)

    def test_staleness_limit_is_per_platform(self) -> None:
        """A monthly feed nine days after its run is fine; a daily one is not."""
        _log(self.conn, [_run("monthly_feed", "2026-09-01"),
                         _run("daily_feed", "2026-09-01")])
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10),
            platforms={"monthly_feed": 40, "daily_feed": 3})
        self.assertEqual([i.check for i in issues], ["sync_status:daily_feed"])

    def test_a_platform_that_vanished_entirely_is_reported(self) -> None:
        """Iterates the DECLARED list, so absence is a finding (strict)."""
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("ghost_feed",))
        self.assertEqual(issues[0].severity, wh.ERROR)
        self.assertIn("no sync_log row has ever been written",
                      issues[0].message)

    def test_unconfigured_platform_is_quiet_in_only_seen_mode(self) -> None:
        issues = wh.check_recent_statuses(
            self.conn, date(2026, 9, 10), platforms=("ghost_feed",),
            seen=set())
        self.assertEqual(issues, [])


class VolumeTests(_Conn):
    """rows_written is recorded on every run; reading it is what catches a
    sync that logs `ok` on an empty pull forever."""

    spec = (wh.VolumeSpec("gmc_visibility", max_zero_streak=2),)

    def test_successful_but_empty_runs_are_an_error(self) -> None:
        _log(self.conn, [_run("gmc_visibility", f"2026-09-0{d}", rows=0)
                         for d in (6, 7, 8)])
        issues = wh.check_volume(self.conn, date(2026, 9, 8), self.spec)
        self.assertEqual(issues[0].severity, wh.ERROR)
        self.assertIn("0 rows", issues[0].message)

    def test_one_non_empty_run_in_the_streak_clears_it(self) -> None:
        """A STREAK, not a floor."""
        _log(self.conn, [_run("gmc_visibility", "2026-09-06", rows=0),
                         _run("gmc_visibility", "2026-09-07", rows=186),
                         _run("gmc_visibility", "2026-09-08", rows=0)])
        self.assertEqual(wh.check_volume(self.conn, date(2026, 9, 8),
                                         self.spec), [])

    def test_too_few_recent_runs_to_judge_stays_silent(self) -> None:
        _log(self.conn, [_run("gmc_visibility", "2026-09-08", rows=0)])
        self.assertEqual(wh.check_volume(self.conn, date(2026, 9, 8),
                                         self.spec), [])

    def test_pipeline_platforms_carry_no_volume_spec(self) -> None:
        """Pipelines log 0 rows by construction."""
        for spec in wh.parse_config().volume:
            with self.subTest(platform=spec.platform):
                self.assertNotIn("pipeline", spec.platform)


class FreshnessOwnershipTests(_Conn):
    """A table cannot be fresh while its own writer is paused."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.execute("CREATE TABLE feed (day TEXT)")
        self.conn.execute("INSERT INTO feed VALUES ('2026-09-01')")
        self.spec = (wh.FreshnessSpec("owned", "SELECT MAX(day) FROM feed", 4,
                                      owner_platform="flexport_orders"),)

    def test_staleness_under_a_paused_owner_is_degraded(self) -> None:
        _log(self.conn, [_run("flexport_orders", "2026-09-07"),
                         _run("flexport_orders", "2026-09-10", "degraded", 0)])
        issues = wh.check_freshness(self.conn, date(2026, 9, 10), self.spec)
        self.assertEqual(issues[0].severity, wh.DEGRADED)
        self.assertIn("consequence of flexport_orders", issues[0].message)

    def test_a_paused_owner_cannot_soften_staleness_forever(self) -> None:
        """An owner that logs `ok` every few days never passes its own clock
        while the table falls further behind; the table's lag caps it."""
        _log(self.conn, [_run("flexport_orders", "2026-09-11"),
                         _run("flexport_orders", "2026-09-12", "degraded")])
        issues = wh.check_freshness(self.conn, date(2026, 9, 12), self.spec)
        self.assertEqual(issues[0].severity, wh.DEGRADED)  # 11d = 4 + 7

        _log(self.conn, [_run("flexport_orders", "2026-09-19"),
                         _run("flexport_orders", "2026-09-20", "degraded")])
        issues = wh.check_freshness(self.conn, date(2026, 9, 20), self.spec)
        self.assertEqual(issues[0].severity, wh.ERROR)

    def test_staleness_under_a_healthy_owner_stays_an_error(self) -> None:
        """A sync that says `ok` while its table goes stale must NOT be
        softened."""
        _log(self.conn, [_run("flexport_orders", "2026-09-10")])
        issues = wh.check_freshness(self.conn, date(2026, 9, 10), self.spec)
        self.assertEqual(issues[0].severity, wh.ERROR)

    def test_gmc_visibility_is_not_owned_by_its_own_sync(self) -> None:
        """It can log `ok` while writing nothing, so ownership would re-hide
        the outage this check exists for."""
        spec = next(s for s in wh.parse_config().freshness
                    if s.name == "gmc_visibility")
        self.assertIsNone(spec.owner_platform)


class GapTests(_Conn):
    def test_snapshot_gaps_ignore_days_before_the_first_snapshot(self) -> None:
        self.conn.execute("CREATE TABLE snap (snapshot_date TEXT)")
        self.conn.executemany("INSERT INTO snap VALUES (?)", [
            ("2026-09-05",), ("2026-09-06",), ("2026-09-08",), ("2026-09-09",)])
        issues = wh.check_snapshot_gaps(
            self.conn, date(2026, 9, 10),
            (wh.SnapshotSpec("snap", "snap"),), lookback_days=21)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, wh.WARN)
        self.assertIn("1 missing day(s): 2026-09-07", issues[0].message)

    def test_missing_snapshot_table_is_a_warning(self) -> None:
        issues = wh.check_snapshot_gaps(
            self.conn, date(2026, 9, 10), (wh.SnapshotSpec("x", "nope"),))
        self.assertEqual(issues[0].severity, wh.WARN)

    def test_historical_gaps_respect_verified_empty_dates(self) -> None:
        self.conn.execute("CREATE TABLE s (d TEXT)")
        self.conn.executemany("INSERT INTO s VALUES (?)", [
            ("2026-01-01",), ("2026-01-03",), ("2026-01-06",)])
        spec = (wh.SeriesSpec("s", "SELECT d FROM s"),)
        issues = wh.check_historical_date_gaps(
            self.conn, spec, {"s": [date(2026, 1, 2)]})
        self.assertIn("2026-01-04..2026-01-05", issues[0].message)
        self.assertNotIn("2026-01-02", issues[0].message)

    def test_date_ranges_are_compacted(self) -> None:
        self.assertEqual(
            wh._compress_dates([date(2026, 7, 1), date(2026, 7, 2),
                                date(2026, 7, 4)]),
            "2026-07-01..2026-07-02, 2026-07-04")


class ConfigTests(unittest.TestCase):
    def test_defaults_parse(self) -> None:
        config = wh.parse_config()
        self.assertTrue(config.only_seen_platforms)
        self.assertIn("shopify", config.status_platforms)
        self.assertTrue(config.freshness)

    def test_print_config_output_round_trips(self) -> None:
        """--print-config is the documented starting point for health.json,
        so its output must be a valid config as-is."""
        dumped = json.loads(json.dumps(wh.DEFAULT_CONFIG))
        self.assertEqual(wh.parse_config(dumped), wh.parse_config())

    def test_example_config_loads(self) -> None:
        config = wh.load_config(wh.HERE / "health.example.json")
        self.assertFalse(config.only_seen_platforms)
        self.assertIn("historical_gaps:ga4", config.disable)

    def test_a_key_replaces_its_default_wholesale(self) -> None:
        config = wh.parse_config({"status_platforms": {"only_me": 5}})
        self.assertEqual(dict(config.status_platforms), {"only_me": 5})

    def test_status_platforms_may_be_a_plain_list(self) -> None:
        config = wh.parse_config({"status_platforms": ["a", "b"]})
        self.assertEqual(dict(config.status_platforms),
                         {"a": wh.STALE_RUN_DAYS, "b": wh.STALE_RUN_DAYS})

    def test_bad_configs_are_rejected(self) -> None:
        for raw in ({"escalate_afterdays": 7},
                    {"escalate_after_days": "7"},
                    {"freshness": [{"name": "x", "sql": "SELECT 1",
                                    "max_age_days": 2, "typo": 1}]},
                    {"freshness": [{"name": "x"}]},
                    {"verified_empty_dates": {"x": ["not-a-date"]}},
                    {"only_seen_platforms": "yes"}):
            with self.subTest(raw=raw), self.assertRaises(wh.ConfigError):
                wh.parse_config(raw)

    def test_missing_config_file_means_defaults(self) -> None:
        self.assertEqual(wh.load_config(Path("no/such/health.json")),
                         wh.parse_config())

    def test_default_freshness_tables_belong_to_the_public_schema(self) -> None:
        """Every default spec queries a table some script in this repo creates
        -- no leftovers from a private schema."""
        sources = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                            for p in list(wh.HERE.glob("*.py"))
                            + list((wh.HERE / "warehouse").glob("*.sql")))
        import re
        for spec in wh.parse_config().freshness:
            for table in re.findall(r"FROM\s+([a-z_]+)", spec.sql):
                with self.subTest(spec=spec.name, table=table):
                    self.assertRegex(
                        sources,
                        rf"CREATE TABLE IF NOT EXISTS {table}\b")


class CollectIssuesTests(_Conn):
    NOW = datetime(2026, 9, 10, 9, 0)

    def test_empty_sync_log_is_an_error_not_all_clear(self) -> None:
        issues = wh.collect_issues(self.conn, self.NOW)
        self.assertEqual([(i.severity, i.check) for i in issues],
                         [(wh.ERROR, "sync_log")])

    def test_unconfigured_connectors_stay_quiet_by_default(self) -> None:
        """Someone running one platform must not see errors for the rest."""
        self.conn.execute("CREATE TABLE ad_metrics (platform TEXT, date TEXT)")
        self.conn.execute("INSERT INTO ad_metrics VALUES ('meta','2026-09-09')")
        _log(self.conn, [_run("meta", "2026-09-10")])
        config = wh.parse_config({"date_series": [], "volume": []})
        self.assertEqual(wh.collect_issues(self.conn, self.NOW, config), [])

    def test_strict_mode_reports_every_declared_platform(self) -> None:
        _log(self.conn, [_run("meta", "2026-09-10")])
        config = wh.parse_config({
            "only_seen_platforms": False, "status_platforms": ["meta", "google"],
            "freshness": [], "volume": [], "snapshot_tables": [],
            "date_series": []})
        issues = wh.collect_issues(self.conn, self.NOW, config)
        self.assertEqual([i.check for i in issues], ["sync_status:google"])

    def test_disable_drops_matching_checks(self) -> None:
        _log(self.conn, [_run("a", "2026-09-10", "error"),
                         _run("b", "2026-09-10", "error")])
        config = wh.parse_config({
            "status_platforms": ["a", "b"], "freshness": [], "volume": [],
            "snapshot_tables": [], "date_series": [],
            "disable": ["sync_status:a*"]})
        issues = wh.collect_issues(self.conn, self.NOW, config)
        self.assertEqual([i.check for i in issues], ["sync_status:b"])

    def test_issues_are_sorted_most_serious_first(self) -> None:
        _log(self.conn, [_run("a", "2026-09-09"),
                         _run("a", "2026-09-10", "degraded"),
                         _run("b", "2026-09-10", "error")])
        config = wh.parse_config({
            "status_platforms": ["a", "b"], "freshness": [], "volume": [],
            "snapshot_tables": [], "date_series": []})
        issues = wh.collect_issues(self.conn, self.NOW, config)
        self.assertEqual([i.severity for i in issues], [wh.ERROR, wh.DEGRADED])


class ScheduledJobTests(_Conn):
    TODAY = date(2026, 7, 30)  # a Thursday
    WEEKLY = wh.ScheduledJobSpec("weekly_job", 8)
    DESIGNED_DEGRADED = wh.ScheduledJobSpec(
        "review_job", 4, frozenset({"ok", "degraded"}))

    def check(self, *specs):
        return wh.check_scheduled_jobs(self.conn, self.TODAY, specs)

    def test_never_ran_is_error(self) -> None:
        issues = self.check(self.WEEKLY)
        self.assertEqual([i.severity for i in issues], [wh.ERROR])
        self.assertIn("ever been written", issues[0].message)

    def test_recent_ok_is_silent_even_days_after_the_run(self) -> None:
        # Six days without a run would be a daily feed's outage, not a weekly one.
        _log(self.conn, [_run("weekly_job", "2026-07-24")])
        self.assertEqual(self.check(self.WEEKLY), [])

    def test_too_old_is_error(self) -> None:
        _log(self.conn, [_run("weekly_job", "2026-07-20")])
        issues = self.check(self.WEEKLY)
        self.assertEqual([i.severity for i in issues], [wh.ERROR])
        self.assertIn("no run in 10 days", issues[0].message)

    def test_unexpected_status_is_error(self) -> None:
        _log(self.conn, [_run("weekly_job", "2026-07-24", "error",
                              message="boom")])
        issues = self.check(self.WEEKLY)
        self.assertEqual([i.severity for i in issues], [wh.ERROR])
        self.assertIn("boom", issues[0].message)

    def test_degraded_is_error_unless_it_is_the_designed_output(self) -> None:
        _log(self.conn, [_run("weekly_job", "2026-07-28", "degraded"),
                         _run("review_job", "2026-07-28", "degraded")])
        issues = self.check(self.WEEKLY, self.DESIGNED_DEGRADED)
        self.assertEqual([i.check for i in issues], ["sync_status:weekly_job"])

    def test_designed_degraded_never_escalates_with_age(self) -> None:
        # Weeks of degraded-only history: the escalation clock would go red
        # waiting for an `ok` this job never writes.
        _log(self.conn, [_run("review_job", d, "degraded")
                         for d in ("2026-07-02", "2026-07-16", "2026-07-29")])
        self.assertEqual(self.check(self.DESIGNED_DEGRADED), [])

    def test_judged_on_started_at_for_a_run_that_never_finished(self) -> None:
        _log(self.conn, [("weekly_job", "2026-07-27T04:00:00Z", None, 0,
                          "running", "")])
        issues = self.check(wh.ScheduledJobSpec(
            "weekly_job", 8, frozenset({"ok", "running"})))
        self.assertEqual(issues, [])

    def test_config_parses_scheduled_jobs(self) -> None:
        cfg = wh.parse_config({"scheduled_jobs": [
            {"platform": "weekly_job", "max_age_days": 8,
             "expected_statuses": ["OK", "degraded"]}]})
        self.assertEqual(cfg.scheduled_jobs, (wh.ScheduledJobSpec(
            "weekly_job", 8, frozenset({"ok", "degraded"})),))
        self.assertEqual(wh.parse_config().scheduled_jobs, ())

    def test_config_rejects_a_bare_string_of_statuses(self) -> None:
        with self.assertRaises(wh.ConfigError):
            wh.parse_config({"scheduled_jobs": [
                {"platform": "weekly_job", "max_age_days": 8,
                 "expected_statuses": "ok"}]})

    def test_collect_issues_runs_the_scheduled_tier(self) -> None:
        _log(self.conn, [_run("google", "2026-07-30")])
        cfg = wh.parse_config({
            "status_platforms": {}, "freshness": [], "volume": [],
            "snapshot_tables": [], "date_series": [],
            "scheduled_jobs": [{"platform": "weekly_job", "max_age_days": 8}]})
        issues = wh.collect_issues(self.conn, datetime(2026, 7, 30, 9), cfg)
        self.assertEqual([i.check for i in issues], ["sync_status:weekly_job"])

    def test_health_alerts_is_watched_by_default(self) -> None:
        self.assertIn("health_alerts", wh.DEFAULT_STATUS_PLATFORMS)


class ExitCodeTests(unittest.TestCase):
    def test_only_error_returns_nonzero(self) -> None:
        """DEGRADED must not drive the exit code."""
        self.assertEqual(wh.exit_code([wh.Issue(wh.DEGRADED, "x", "")]), 0)
        self.assertEqual(wh.exit_code([wh.Issue(wh.WARN, "x", "")]), 0)
        self.assertEqual(wh.exit_code([wh.Issue(wh.ERROR, "x", "")]), 1)
        self.assertEqual(wh.exit_code([wh.Issue(wh.WARN, "x", "")],
                                      warnings_as_errors=True), 1)
        self.assertGreater(wh.SEVERITY_RANK[wh.ERROR],
                           wh.SEVERITY_RANK[wh.DEGRADED])
        self.assertGreater(wh.SEVERITY_RANK[wh.DEGRADED],
                           wh.SEVERITY_RANK[wh.WARN])

    def test_main_against_a_temp_warehouse(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(db, "DB_PATH", Path(folder) / "w.db"):
            db.init_db()
            db.log_sync("meta", db.now(), 0, "error", "token expired")
            missing = Path(folder) / "none.json"
            self.assertEqual(wh.main(["--config", str(missing)]), 1)

    def test_main_on_a_missing_database_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(db, "DB_PATH", Path(folder) / "absent.db"):
            self.assertEqual(wh.main([]), 1)
            self.assertFalse((Path(folder) / "absent.db").exists(),
                             "a health check must never create the database")


if __name__ == "__main__":
    unittest.main()
