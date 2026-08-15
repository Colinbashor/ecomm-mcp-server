"""Tests for google_ads_structure_sync.py.

Hermetic: no network, no real Google Ads credentials. Each grain's fetch
callable in FETCH is monkeypatched with a fake that returns canned rows, so
these exercise the connector's own logic — schema creation, per-grain insert
shaping, and partial-failure isolation — without touching the google-ads
library at all.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

import google_ads_structure_sync as gstruct


class _UnclosableConnection:
    """Delegates to a real sqlite3.Connection except close(), so run()'s
    `finally: conn.close()` can't take the connection away before the test
    inspects it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_five_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        gstruct.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {
            "google_campaigns", "google_asset_groups", "google_asset_group_assets",
            "google_asset_group_listing_filters", "google_conversion_actions",
        })
        conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        gstruct.ensure_schema(conn)
        gstruct.ensure_schema(conn)  # must not raise
        conn.close()


def _fake_campaigns():
    return [{
        "account_id": "111", "campaign_id": "c1", "campaign_name": "Shopping - US",
        "status": "ENABLED", "serving_status": "SERVING", "primary_status": "ELIGIBLE",
        "primary_status_reasons": "", "channel_type": "PERFORMANCE_MAX",
        "channel_sub_type": None, "bidding_strategy": "MAXIMIZE_CONVERSION_VALUE",
        "target_roas": 3.0, "target_cpa": None, "budget_amount": 100.0,
        "budget_delivery": "STANDARD", "budget_shared": 0,
    }]


def _fake_conversion_actions():
    return [{
        "account_id": "111", "conversion_action_id": "ca1", "name": "Purchase",
        "status": "ENABLED", "category": "PURCHASE", "action_type": "WEBPAGE",
        "primary_for_goal": 1, "attribution_model": "DATA_DRIVEN",
    }]


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self._real_connect = gstruct.db.connect
        gstruct.db.connect = lambda: _UnclosableConnection(self.conn)

    def tearDown(self) -> None:
        gstruct.db.connect = self._real_connect
        self.conn.close()

    def test_run_writes_rows_for_the_requested_grain_only(self) -> None:
        with patch.dict(gstruct.FETCH, {"campaigns": _fake_campaigns}):
            n, failures = gstruct.run("2026-08-05", only=["campaigns"])
        self.assertEqual(n, 1)
        self.assertEqual(failures, [])
        row = self.conn.execute(
            "SELECT campaign_name, primary_status, target_roas FROM google_campaigns"
        ).fetchone()
        self.assertEqual(row, ("Shopping - US", "ELIGIBLE", 3.0))
        # An unrequested grain must not have been touched.
        count = self.conn.execute(
            "SELECT COUNT(*) FROM google_conversion_actions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_run_stamps_every_row_with_the_same_snapshot_date(self) -> None:
        with patch.dict(gstruct.FETCH, {"conversion_actions": _fake_conversion_actions}):
            gstruct.run("2026-08-05", only=["conversion_actions"])
        row = self.conn.execute(
            "SELECT snapshot_date, primary_for_goal FROM google_conversion_actions"
        ).fetchone()
        self.assertEqual(row, ("2026-08-05", 1))

    def test_one_grain_failing_does_not_stop_the_others(self) -> None:
        def _broken():
            raise RuntimeError("simulated API failure")

        with patch.dict(gstruct.FETCH, {
            "campaigns": _broken,
            "conversion_actions": _fake_conversion_actions,
        }):
            n, failures = gstruct.run("2026-08-05", only=["campaigns", "conversion_actions"])
        self.assertEqual(n, 1)  # conversion_actions still landed
        self.assertEqual(len(failures), 1)
        self.assertIn("campaigns", failures[0])


class MainTests(unittest.TestCase):
    def test_unknown_grain_exits_with_a_clear_message(self) -> None:
        import sys
        with patch.object(sys, "argv", ["google_ads_structure_sync.py", "--only", "bogus"]):
            with self.assertRaises(SystemExit) as cm:
                gstruct.main()
        self.assertIn("bogus", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
