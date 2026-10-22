"""health_alerts: notify on a CHANGE in health, never on the standing state.

Hermetic: temp SQLite files, notify.send patched out, no network.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import health_alerts
import warehouse_health as wh
from health_alerts import build_message, classify
from warehouse import db

TUESDAY = date(2026, 9, 8)  # not the digest day
MONDAY = date(2026, 9, 14)

# A destination with a target, so delivery tests reach the (patched) push.
REACHABLE = {"WAREHOUSE_HEALTH_SLACK_WEBHOOK": "https://hooks.example.com/health",
             "OPS_CHANNEL_SLACK_WEBHOOK": "https://hooks.example.com/ops"}


def _issue(severity=wh.ERROR, check="volume:gmc_visibility", message="0 rows"):
    return wh.Issue(severity, check, message)


def _prev(severity, first_seen="2026-09-01", notified_on="2026-09-01",
          message="x"):
    return {"severity": severity, "message": message, "first_seen": first_seen,
            "last_seen": first_seen, "notified_on": notified_on}


class ChangeDetectionTests(unittest.TestCase):
    """The pure classification half, adapted from the alarm rebuild."""

    def test_a_new_error_is_announced(self) -> None:
        buckets = classify([_issue()], previous={})
        self.assertEqual(len(buckets["new"]), 1)
        self.assertIsNotNone(build_message(buckets, TUESDAY, digest=False))

    def test_the_same_error_tomorrow_is_silent(self) -> None:
        """The whole point: a message that always fires stops being read."""
        buckets = classify([_issue()],
                           {"volume:gmc_visibility": _prev(wh.ERROR)})
        self.assertEqual(buckets["new"], [])
        self.assertIsNone(build_message(buckets, TUESDAY, digest=False))

    def test_degraded_ageing_into_error_is_announced(self) -> None:
        check = "sync_status:flexport_inbounds"
        buckets = classify([_issue(check=check)],
                           {check: _prev(wh.DEGRADED, notified_on=None)})
        self.assertEqual(len(buckets["escalated"]), 1)
        message = build_message(buckets, TUESDAY, digest=False)
        self.assertIn("ESCALATED", message)
        self.assertIn("[DEGRADED -> ERROR]", message)

    def test_a_severity_drop_is_not_an_escalation(self) -> None:
        check = "sync_status:x"
        buckets = classify([_issue(wh.DEGRADED, check)],
                           {check: _prev(wh.ERROR)})
        self.assertEqual(buckets["escalated"], [])
        self.assertIsNone(build_message(buckets, TUESDAY, digest=False))

    def test_recovery_is_announced(self) -> None:
        buckets = classify([], {"volume:gmc_visibility": _prev(wh.ERROR)})
        self.assertEqual(len(buckets["resolved"]), 1)
        self.assertIn("RECOVERED", build_message(buckets, TUESDAY, False))

    def test_a_quiet_warning_that_cleared_needs_no_recovery_line(self) -> None:
        buckets = classify([], {"historical_gaps:x": _prev(wh.WARN,
                                                           notified_on=None)})
        self.assertEqual(buckets["resolved"], [])

    def test_a_standing_degraded_condition_only_speaks_on_digest_day(self) -> None:
        check = "sync_status:flexport_orders"
        buckets = classify([_issue(wh.DEGRADED, check)],
                           {check: _prev(wh.DEGRADED)})
        self.assertIsNone(build_message(buckets, TUESDAY, digest=False))
        self.assertIn("Still degraded",
                      build_message(buckets, TUESDAY, digest=True))

    def test_a_new_warning_does_not_wake_anyone(self) -> None:
        buckets = classify([_issue(wh.WARN, "historical_gaps:x")], {})
        self.assertEqual(buckets["new"], [])
        self.assertIsNone(build_message(buckets, TUESDAY, False))


class ChangeOnlyDeliveryTests(unittest.TestCase):
    """End to end over a persisted state table: send exactly when it changed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(Path(self._tmp.name) / "w.db")
        self._send = patch.object(health_alerts.notify, "send", return_value=[])
        self.send = self._send.start()
        self._env = patch.dict(os.environ, REACHABLE)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._send.stop()
        self.conn.close()
        self._tmp.cleanup()

    def _run(self, issues, today=TUESDAY, **kw):
        return health_alerts.run(issues, self.conn, today, **kw)

    def test_same_state_twice_sends_once(self) -> None:
        self._run([_issue()])
        self._run([_issue()], TUESDAY + timedelta(days=1))
        self.assertEqual(self.send.call_count, 1)

    def test_full_lifecycle_new_silent_recovered_silent_recurring(self) -> None:
        day = TUESDAY
        self._run([_issue()], day)                         # new -> send
        self._run([_issue()], day + timedelta(days=1))     # same -> silent
        self._run([], day + timedelta(days=2))             # recovered -> send
        self._run([], day + timedelta(days=3))             # still fine -> silent
        self._run([_issue()], day + timedelta(days=4))     # recurrence -> send
        texts = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual(len(texts), 3)
        self.assertIn("*NEW*", texts[0])
        self.assertIn("*RECOVERED*", texts[1])
        self.assertIn("*NEW*", texts[2], "a recurrence must be NEW again")

    def test_escalation_is_sent_once(self) -> None:
        check = "sync_status:flexport_inbounds"
        self._run([_issue(wh.DEGRADED, check)])                 # new degraded
        self._run([_issue(wh.DEGRADED, check)],
                  TUESDAY + timedelta(days=1))                  # silent
        self._run([_issue(wh.ERROR, check)],
                  TUESDAY + timedelta(days=2))                  # escalated
        self._run([_issue(wh.ERROR, check)],
                  TUESDAY + timedelta(days=3))                  # silent
        texts = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn("ESCALATED", texts[1])
        self.assertIn("(standing 2d)", texts[1])

    def test_message_text_changing_alone_is_not_a_change(self) -> None:
        """A stale count ticking up daily ('8 days old' -> '9 days old') must
        not re-announce: identity is the check, change is the severity."""
        self._run([_issue(message="latest is 8 days old")])
        self._run([_issue(message="latest is 9 days old")],
                  TUESDAY + timedelta(days=1))
        self.assertEqual(self.send.call_count, 1)

    def test_standing_degraded_is_repeated_on_the_digest_day_only(self) -> None:
        held = [_issue(wh.DEGRADED, "sync_status:x")]
        self._run(held, MONDAY - timedelta(days=2))      # new -> send
        self._run(held, MONDAY - timedelta(days=1))      # silent
        self._run(held, MONDAY)                          # digest -> send
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("Still degraded", self.send.call_args.args[0])

    def test_digest_can_be_disabled(self) -> None:
        held = [_issue(wh.DEGRADED, "sync_status:x")]
        self._run(held, MONDAY - timedelta(days=1), digest_weekday=-1)
        self._run(held, MONDAY, digest_weekday=-1)
        self.assertEqual(self.send.call_count, 1)

    def test_dry_run_records_nothing_and_sends_nothing(self) -> None:
        message = self._run([_issue()], dry_run=True).message
        self.assertIsNotNone(message)
        self.send.assert_not_called()
        self.assertEqual(health_alerts.load_state(self.conn), {})
        self._run([_issue()])           # so the real run still announces it
        self.assertEqual(self.send.call_count, 1)

    def test_state_is_recorded_before_the_push(self) -> None:
        """A failed push must not lose the record, or tomorrow re-announces."""
        seen_state = {}

        def capture(text, dest):
            seen_state.update(health_alerts.load_state(self.conn))

        self.send.side_effect = capture
        self._run([_issue()])
        self.assertIn("volume:gmc_visibility", seen_state)
        self.assertEqual(seen_state["volume:gmc_visibility"]["notified_on"],
                         TUESDAY.isoformat())

    def test_sends_to_the_configured_dest(self) -> None:
        self._run([_issue()], dest="ops_channel")
        self.assertEqual(self.send.call_args.kwargs["dest"], "ops_channel")

    def test_force_resends_the_current_state(self) -> None:
        self._run([_issue()])
        self._run([_issue()], TUESDAY + timedelta(days=1), force=True)
        self.assertEqual(self.send.call_count, 2)


class DeliveryOutcomeTests(unittest.TestCase):
    """An alarm that reaches no one must never log `ok`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(Path(self._tmp.name) / "w.db")
        self._send = patch.object(health_alerts.notify, "send", return_value=[])
        self.send = self._send.start()

    def tearDown(self) -> None:
        self._send.stop()
        self.conn.close()
        self._tmp.cleanup()

    def _run(self, issues, env, today=TUESDAY):
        with patch.dict(os.environ, env, clear=True):
            return health_alerts.run(issues, self.conn, today)

    def test_unreachable_dest_with_news_is_degraded_and_not_pushed(self) -> None:
        result = self._run([_issue()], {})
        self.assertEqual(result.status, "degraded")
        self.assertIn("NOT DELIVERED", result.note)
        self.assertIn("WAREHOUSE_HEALTH_SLACK_WEBHOOK", result.note)
        self.send.assert_not_called()
        state = health_alerts.load_state(self.conn)
        self.assertIsNone(state["volume:gmc_visibility"]["notified_on"])

    def test_unreachable_dest_on_a_quiet_day_is_still_degraded(self) -> None:
        # An `ok` on the quiet path would reset the escalation clock.
        result = self._run([], {})
        self.assertIsNone(result.message)
        self.assertEqual(result.status, "degraded")

    def test_reachable_quiet_day_is_ok(self) -> None:
        result = self._run([], REACHABLE)
        self.assertEqual((result.status, result.note), ("ok", "no change"))

    def test_a_failed_push_is_degraded_and_keeps_the_record(self) -> None:
        self.send.return_value = ["slack: ConnectionError"]
        result = self._run([_issue()], REACHABLE)
        self.assertEqual(result.status, "degraded")
        self.assertIn("push failed - slack: ConnectionError", result.note)
        self.assertIn("volume:gmc_visibility", health_alerts.load_state(self.conn))

    def test_a_successful_push_is_ok(self) -> None:
        result = self._run([_issue()], REACHABLE)
        self.assertEqual((result.status, result.note), ("ok", "change announced"))

    def test_undeliverable_names_the_keys_for_the_dest(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            why = health_alerts.undeliverable("ops_channel")
        self.assertIn("OPS_CHANNEL_GCHAT_WEBHOOK", why)
        with patch.dict(os.environ, {"OPS_CHANNEL_EMAIL_TO": "ops@example.com"},
                        clear=True):
            self.assertIsNone(health_alerts.undeliverable("ops_channel"))

    def test_main_logs_degraded_when_nothing_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(db, "DB_PATH", Path(folder) / "w.db"), \
                patch.dict(os.environ, {}, clear=True):
            db.init_db()
            db.log_sync("meta", db.now(), 0, "error", "token expired")
            self.assertEqual(health_alerts.main(
                ["--config", str(Path(folder) / "none.json")]), 0)
            conn = sqlite3.connect(db.DB_PATH)
            try:
                status, = conn.execute(
                    "SELECT status FROM sync_log WHERE platform='health_alerts'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "degraded")


class MainTests(unittest.TestCase):
    """main() over a temp warehouse: health is computed, then diffed."""

    def test_main_announces_once_then_goes_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(db, "DB_PATH", Path(folder) / "w.db"), \
                patch.dict(os.environ, REACHABLE), \
                patch.object(health_alerts.notify, "send",
                             return_value=[]) as send:
            db.init_db()
            db.log_sync("meta", db.now(), 0, "error", "token expired")
            cfg = ["--config", str(Path(folder) / "none.json")]

            self.assertEqual(health_alerts.main(cfg), 0)
            self.assertEqual(health_alerts.main(cfg), 0)

            self.assertEqual(send.call_count, 1)
            self.assertIn("sync_status:meta", send.call_args.args[0])
            self.assertEqual(send.call_args.kwargs["dest"], "warehouse_health")
            conn = sqlite3.connect(db.DB_PATH)
            try:
                rows = conn.execute(
                    "SELECT status, message FROM sync_log "
                    "WHERE platform='health_alerts' ORDER BY id").fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [("ok", "change announced"),
                                    ("ok", "no change")])

    def test_env_example_documents_the_default_dest(self) -> None:
        text = (wh.HERE / ".env.example").read_text(encoding="utf-8")
        for suffix in ("SLACK_WEBHOOK", "GCHAT_WEBHOOK", "EMAIL_TO"):
            with self.subTest(suffix=suffix):
                self.assertIn(f"WAREHOUSE_HEALTH_{suffix}", text)


if __name__ == "__main__":
    unittest.main()
