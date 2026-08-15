"""Hermetic tests for amazon_ads_detail_sync.py — no network, no real DB file.

Covers: schema creation, the <=31-day chunk splitter, each report grain's
row-shaping lambda, `run()` writing through a patched `run_report` (including
one grain failing without killing the others), and main()'s
ok/degraded/error status logic.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import amazon_ads_detail_sync as detail


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_three_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        detail.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("amazon_ad_products", "amazon_ad_targeting", "amazon_ad_search_terms"):
            self.assertIn(t, tables)


class ChunksTests(unittest.TestCase):
    def test_short_range_is_a_single_chunk(self) -> None:
        chunks = list(detail._chunks("2026-06-01", "2026-06-10"))
        self.assertEqual(chunks, [("2026-06-01", "2026-06-10")])

    def test_range_over_31_days_is_split(self) -> None:
        chunks = list(detail._chunks("2026-01-01", "2026-03-15"))
        self.assertEqual(chunks[0], ("2026-01-01", "2026-01-31"))
        # windows never exceed 31 days and never overlap
        for lo, hi in chunks:
            self.assertLessEqual(
                (__import__("datetime").date.fromisoformat(hi)
                 - __import__("datetime").date.fromisoformat(lo)).days, 30)
        self.assertEqual(chunks[-1][1], "2026-03-15")

    def test_single_day_range(self) -> None:
        self.assertEqual(list(detail._chunks("2026-06-01", "2026-06-01")),
                         [("2026-06-01", "2026-06-01")])


class RowLambdaTests(unittest.TestCase):
    def test_products_row_shape(self) -> None:
        d = {"date": "2026-06-01", "campaignId": 111, "adGroupId": 222,
             "advertisedAsin": "B0001", "advertisedSku": "SKU1",
             "campaignName": "Campaign A", "impressions": 1000, "clicks": 20,
             "cost": "15.50", "purchases14d": 3, "sales14d": "89.97",
             "unitsSoldClicks14d": 3}
        row = detail.REPORTS["amazon_ad_products"]["row"](d, "acct1", "stamp")
        self.assertEqual(row, ("acct1", "2026-06-01", "111", "222", "B0001", "SKU1",
                               "Campaign A", 1000, 20, 15.50, 3.0, 89.97, 3, "stamp"))

    def test_targeting_row_shape(self) -> None:
        d = {"date": "2026-06-01", "campaignId": 111, "adGroupId": 222,
             "targeting": "close-match", "matchType": "CLOSE_MATCH",
             "campaignName": "Campaign A", "impressions": 500, "clicks": 10,
             "cost": 5.0, "purchases14d": 1, "sales14d": 29.99}
        row = detail.REPORTS["amazon_ad_targeting"]["row"](d, "acct1", "stamp")
        self.assertEqual(row, ("acct1", "2026-06-01", "111", "222", "close-match",
                               "CLOSE_MATCH", "Campaign A", 500, 10, 5.0, 1.0, 29.99, "stamp"))

    def test_search_terms_row_shape(self) -> None:
        d = {"date": "2026-06-01", "campaignId": 111, "adGroupId": 222,
             "searchTerm": "wireless mouse", "campaignName": "Campaign A",
             "impressions": 300, "clicks": 5, "cost": 2.5,
             "purchases14d": 0, "sales14d": 0}
        row = detail.REPORTS["amazon_ad_search_terms"]["row"](d, "acct1", "stamp")
        self.assertEqual(row, ("acct1", "2026-06-01", "111", "222", "wireless mouse",
                               "Campaign A", 300, 5, 2.5, 0.0, 0.0, "stamp"))

    def test_missing_numeric_fields_default_to_zero(self) -> None:
        row = detail.REPORTS["amazon_ad_products"]["row"](
            {"campaignId": 1, "adGroupId": 2}, "acct1", "stamp")
        self.assertEqual(row[4], "")     # asin defaults to ""
        self.assertEqual(row[7:9], (0, 0))  # impressions, clicks
        self.assertEqual(row[9], 0.0)    # spend


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["AMAZON_ADS_PROFILE_ID"] = "profile1"
        os.environ["AMAZON_ADS_CLIENT_ID"] = "client1"
        os.environ["AMAZON_ADS_REGION"] = "NA"

    def _row(self, campaign_id=1, ad_group_id=2, **extra) -> dict:
        base = {"date": "2026-06-01", "campaignId": campaign_id, "adGroupId": ad_group_id,
                "impressions": 10, "clicks": 1, "cost": 1.0,
                "purchases14d": 0, "sales14d": 0}
        base.update(extra)
        return base

    def test_writes_rows_for_all_three_grains(self) -> None:
        conn = sqlite3.connect(":memory:")
        detail.ensure_schema(conn)

        def fake_run_report(host, headers, body):
            rtype = body["configuration"]["reportTypeId"]
            if rtype == "spAdvertisedProduct":
                return [self._row(advertisedAsin="B0001")]
            if rtype == "spTargeting":
                return [self._row(targeting="close-match")]
            return [self._row(searchTerm="widget")]

        with patch.object(detail, "run_report", side_effect=fake_run_report), \
             patch.object(detail, "_access_token", return_value="tok"):
            total, failures = detail.run(conn, "2026-06-01", "2026-06-01")

        self.assertEqual(total, 3)
        self.assertEqual(failures, [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM amazon_ad_products").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM amazon_ad_targeting").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM amazon_ad_search_terms").fetchone()[0], 1)

    def test_one_failing_grain_does_not_kill_the_others(self) -> None:
        conn = sqlite3.connect(":memory:")
        detail.ensure_schema(conn)

        def fake_run_report(host, headers, body):
            rtype = body["configuration"]["reportTypeId"]
            if rtype == "spTargeting":
                raise RuntimeError("boom")
            return [self._row()]

        with patch.object(detail, "run_report", side_effect=fake_run_report), \
             patch.object(detail, "_access_token", return_value="tok"):
            total, failures = detail.run(conn, "2026-06-01", "2026-06-01")

        self.assertEqual(total, 2)  # products + search_terms still landed
        self.assertEqual(len(failures), 1)
        self.assertIn("spTargeting", failures[0])

    def test_all_grains_failing_yields_zero_rows_and_three_failures(self) -> None:
        conn = sqlite3.connect(":memory:")
        detail.ensure_schema(conn)
        with patch.object(detail, "run_report", side_effect=RuntimeError("boom")), \
             patch.object(detail, "_access_token", return_value="tok"):
            total, failures = detail.run(conn, "2026-06-01", "2026-06-01")
        self.assertEqual(total, 0)
        self.assertEqual(len(failures), 3)


class RequireEnvTests(unittest.TestCase):
    def test_missing_vars_raise_systemexit(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in detail.REQUIRED_ENV}
        try:
            with self.assertRaises(SystemExit) as cm:
                detail.require_env()
            for k in detail.REQUIRED_ENV:
                self.assertIn(k, str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class MainStatusTests(unittest.TestCase):
    def _run_main(self, run_return):
        saved = {k: os.environ.get(k) for k in detail.REQUIRED_ENV}
        logged = {}

        def fake_log_sync(platform, started, rows, status, message=""):
            logged.update(platform=platform, rows=rows, status=status, message=message)

        try:
            for k in detail.REQUIRED_ENV:
                os.environ[k] = "x"
            fake_conn = sqlite3.connect(":memory:")
            with patch("sys.argv", ["amazon_ads_detail_sync.py", "--days", "1"]), \
                 patch.object(detail.warehouse_db, "init_db"), \
                 patch.object(detail.warehouse_db, "now", return_value="stamp"), \
                 patch.object(detail.warehouse_db, "log_sync", side_effect=fake_log_sync), \
                 patch("sqlite3.connect", return_value=fake_conn), \
                 patch.object(detail, "run", return_value=run_return):
                rc = detail.main()
            return rc, logged
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_all_ok_when_no_failures(self) -> None:
        rc, logged = self._run_main((10, []))
        self.assertEqual(rc, 0)
        self.assertEqual(logged["status"], "ok")

    def test_degraded_when_some_rows_landed_despite_failures(self) -> None:
        rc, logged = self._run_main((5, ["spTargeting 2026-06-01..2026-06-01: boom"]))
        self.assertEqual(rc, 0)
        self.assertEqual(logged["status"], "degraded")

    def test_error_when_nothing_landed_and_something_failed(self) -> None:
        rc, logged = self._run_main((0, ["spAdvertisedProduct: boom",
                                          "spTargeting: boom", "spSearchTerm: boom"]))
        self.assertEqual(rc, 1)
        self.assertEqual(logged["status"], "error")


if __name__ == "__main__":
    unittest.main()
