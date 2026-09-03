"""Meta Ads `landing_page_views` column.

`link_clicks` (an inline link click) overstates how many people actually
arrived on the page — some tap and bail during load. `landing_page_view` is
the action that fires only once the page actually rendered, so it's always
<= link_clicks and is the right number to compare against a separate
analytics tool's session count.

Two things are pinned here: the connector picks exactly one of Meta's
overlapping `omni_landing_page_view`/`landing_page_view` action types (the
same "take one canonical type, not a substring sum" rule the purchase/
add-to-cart/checkout columns already follow), and `warehouse/db.py` carries
the column all the way through migration, default, and the named INSERT --
a column added in one of those three places but not the others fails
silently (either the ALTER never runs, or the value never reaches storage).
"""
from __future__ import annotations

import sqlite3
import unittest

from warehouse.connectors.meta_ads import _action_total, _LPV_TYPES


class ActionTotalTests(unittest.TestCase):
    def test_landing_page_view_type_is_picked_up(self):
        actions = [{"action_type": "landing_page_view", "value": "42"}]
        self.assertEqual(_action_total(actions, _LPV_TYPES), 42.0)

    def test_omni_variant_is_preferred_when_both_present(self):
        # Meta returns both variants with identical values for a given
        # account; the omni_ one is listed first and wins, same convention
        # as the purchase/ATC/checkout tuples elsewhere in this connector.
        actions = [
            {"action_type": "omni_landing_page_view", "value": "42"},
            {"action_type": "landing_page_view", "value": "999"},
        ]
        self.assertEqual(_action_total(actions, _LPV_TYPES), 42.0)

    def test_missing_action_type_defaults_to_zero(self):
        self.assertEqual(_action_total([{"action_type": "purchase", "value": "10"}],
                                       _LPV_TYPES), 0.0)

    def test_no_actions_at_all_defaults_to_zero(self):
        self.assertEqual(_action_total(None, _LPV_TYPES), 0.0)


class SchemaAndUpsertTests(unittest.TestCase):
    """Column must exist end-to-end: migration, default, and named insert."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE ad_metrics (
                platform TEXT, account_id TEXT, campaign_id TEXT,
                campaign_name TEXT, date TEXT, impressions INTEGER,
                clicks INTEGER, spend REAL, conversions REAL, revenue REAL,
                currency TEXT, synced_at TEXT,
                PRIMARY KEY (platform, account_id, campaign_id, date)
            )
            """
        )
        return conn

    def test_migration_adds_the_column_to_an_older_table(self):
        from warehouse.db import MIGRATIONS

        conn = self._conn()
        existing = {c[1] for c in conn.execute("PRAGMA table_info(ad_metrics)")}
        self.assertNotIn("landing_page_views", existing)
        for col_def in MIGRATIONS["ad_metrics"]:
            if col_def.split()[0] not in existing:
                conn.execute(f"ALTER TABLE ad_metrics ADD COLUMN {col_def}")
        existing = {c[1] for c in conn.execute("PRAGMA table_info(ad_metrics)")}
        self.assertIn("landing_page_views", existing)

    def test_default_and_insert_columns_both_carry_it(self):
        from warehouse.db import _AD_EXTENDED_DEFAULTS

        self.assertIn("landing_page_views", _AD_EXTENDED_DEFAULTS)
        self.assertIsNone(_AD_EXTENDED_DEFAULTS["landing_page_views"])

    def test_upsert_ad_metrics_persists_a_real_value(self):
        import os
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp()) / "warehouse.db"
        prev = os.environ.get("WAREHOUSE_DB")
        os.environ["WAREHOUSE_DB"] = str(tmp)
        try:
            import importlib
            from warehouse import db as _db
            importlib.reload(_db)
            _db.init_db()
            _db.upsert_ad_metrics([{
                "platform": "meta", "account_id": "act_1", "campaign_id": "c1",
                "campaign_name": "test", "date": "2026-01-01", "impressions": 100,
                "clicks": 10, "spend": 5.0, "conversions": 1.0, "revenue": 20.0,
                "currency": "USD", "landing_page_views": 8.0,
            }])
            conn = sqlite3.connect(str(tmp))
            row = conn.execute(
                "SELECT landing_page_views FROM ad_metrics WHERE campaign_id='c1'"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], 8.0)
        finally:
            if prev is None:
                os.environ.pop("WAREHOUSE_DB", None)
            else:
                os.environ["WAREHOUSE_DB"] = prev


if __name__ == "__main__":
    unittest.main()
