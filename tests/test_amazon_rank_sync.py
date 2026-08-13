"""Hermetic tests for amazon_rank_sync.py — no network, no real DB file.

Covers: schema creation, the pageSize=20 batch construction, best-(lowest)-
rank extraction across multiple sales-rank entries, the fallback ASIN
source, and the "no ASINs given" failure mode (this scaffold has no
product/traffic table to default from).
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import amazon_rank_sync as rank


class _FakeResp:
    def __init__(self, status: int = 200, payload: object | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self) -> object:
        return self._payload


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        rank.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_sales_rank)")}
        self.assertEqual(
            cols, {"snapshot_date", "asin", "display_rank", "display_title",
                   "category_rank", "category_title", "synced_at"})


class ExtractTests(unittest.TestCase):
    def test_picks_best_lowest_rank_across_multiple_entries(self) -> None:
        item = {
            "salesRanks": [
                {
                    "marketplaceId": "ATVPDKIKX0DER",
                    "displayGroupRanks": [{"rank": 500, "title": "Clothing"}],
                    "classificationRanks": [
                        {"rank": 40, "title": "Dresses"},
                        {"rank": 12, "title": "Novelty Dresses"},  # lower = better
                    ],
                }
            ]
        }
        dr, dt, cr, ct = rank._extract(item, "ATVPDKIKX0DER")
        self.assertEqual((dr, dt), (500, "Clothing"))
        self.assertEqual((cr, ct), (12, "Novelty Dresses"))

    def test_ignores_ranks_for_a_different_marketplace(self) -> None:
        item = {"salesRanks": [{"marketplaceId": "OTHER_MP", "displayGroupRanks": [{"rank": 1}]}]}
        dr, _, cr, _ = rank._extract(item, "ATVPDKIKX0DER")
        self.assertIsNone(dr)
        self.assertIsNone(cr)

    def test_no_sales_ranks_returns_all_none(self) -> None:
        dr, dt, cr, ct = rank._extract({}, "ATVPDKIKX0DER")
        self.assertEqual((dr, dt, cr, ct), (None, None, None, None))


class FetchBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        os.environ["SPAPI_REGION"] = "NA"
        self._token_patch = patch.object(rank, "_access_token", return_value="tok")
        self._token_patch.start()

    def tearDown(self) -> None:
        self._token_patch.stop()

    def test_page_size_is_always_set_to_batch_size(self) -> None:
        # The gotcha this test pins: pageSize defaults to 10 server-side and
        # must be set explicitly or a 20-ASIN batch silently truncates.
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured.update(params or {})
            return _FakeResp(200, {"items": []})

        with patch.object(rank, "requests") as fake_requests:
            fake_requests.get.side_effect = fake_get
            fake_requests.exceptions = __import__("requests").exceptions
            rank._fetch_batch("https://host", "ATVPDKIKX0DER", ["A", "B"])

        self.assertEqual(captured["pageSize"], rank.BATCH_SIZE)
        self.assertEqual(captured["identifiers"], "A,B")

    def test_429_is_retried(self) -> None:
        with patch.object(rank, "requests") as fake_requests, patch.object(rank.time, "sleep"):
            fake_requests.get.side_effect = [_FakeResp(429), _FakeResp(200, {"items": []})]
            fake_requests.exceptions = __import__("requests").exceptions
            result = rank._fetch_batch("https://host", "ATVPDKIKX0DER", ["A"])
        self.assertEqual(result, {})


class FallbackAsinsTests(unittest.TestCase):
    def test_missing_table_returns_empty_list(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.assertEqual(rank.fallback_asins(conn), [])

    def test_reads_distinct_skus_when_table_present(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE amazon_fulfilled_shipments (sku TEXT)")
        conn.executemany("INSERT INTO amazon_fulfilled_shipments VALUES (?)",
                         [("A",), ("A",), ("B",), (None,), ("",)])
        self.assertEqual(sorted(rank.fallback_asins(conn)), ["A", "B"])


class MainNoAsinsTests(unittest.TestCase):
    def test_main_exits_when_no_asins_available(self) -> None:
        # Fully mock the warehouse_db side so this never touches a real DB
        # file on disk — only the argv-parsing / no-ASINs-available logic in
        # main() itself is under test here.
        os.environ["SPAPI_MARKETPLACE_ID"] = "ATVPDKIKX0DER"
        saved = {k: os.environ.get(k) for k in rank.REQUIRED_ENV}
        try:
            for k in rank.REQUIRED_ENV:
                os.environ[k] = "x"
            fake_conn = sqlite3.connect(":memory:")
            with patch("sys.argv", ["amazon_rank_sync.py"]), \
                 patch.object(rank.warehouse_db, "init_db"), \
                 patch.object(rank.warehouse_db, "now", return_value="stamp"), \
                 patch.object(rank.warehouse_db, "log_sync"), \
                 patch("sqlite3.connect", return_value=fake_conn):
                with self.assertRaises(SystemExit) as cm:
                    rank.main()
            self.assertIn("No ASINs", str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
