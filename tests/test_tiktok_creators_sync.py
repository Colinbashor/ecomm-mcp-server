"""Hermetic tests for tiktok_creators_sync.py -- no network, no real warehouse.db.

Covers: schema creation, CSV import parsing (header aliasing + total-row
skip), the API crawl's pagination/partial-vs-complete detection (rolling
window ceiling + a bare dropped connection preserving partial results), the
merge-on-partial/replace-on-complete write policy, and the missing-
credentials guard (import needs none; api does).
"""
from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import tiktok_creators_sync as tc
from warehouse import db


def _resp(code=0, data=None, message="ok"):
    class _R:
        status_code = 200

        def json(self):
            return {"code": code, "message": message, "data": data or {}}
    return _R()


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        tc.ensure_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tiktok_creators)")}
        for expected in ("handle", "display_name", "creator_id", "source_file"):
            self.assertIn(expected, cols)
        conn.close()


class CheckRequiredEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                tc.check_required_env()
        self.assertIn("TIKTOK_APP_KEY", str(cm.exception))


class CsvImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()

    def _write_csv(self, name: str, rows: list[list]) -> Path:
        path = Path(self.tmpdir) / name
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        return path

    def test_parse_maps_aliased_headers_and_skips_total_row(self) -> None:
        path = self._write_csv("creators.csv", [
            ["Creator Username", "Creator Nickname", "Followers", "Total GMV"],
            ["@handle1", "Handle One", "1000", "500.50"],
            ["handle2", "Handle Two", "2,000", "$1,200.00"],
            ["Total", "", "3000", "1700.50"],
        ])
        records, mapped = tc.parse(path)
        self.assertTrue(mapped["handle"])
        self.assertTrue(mapped["display_name"])
        self.assertTrue(mapped["followers"])
        self.assertTrue(mapped["gmv"])
        self.assertFalse(mapped["region"])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["handle"], "handle1")  # leading @ stripped
        self.assertEqual(records[0]["followers"], 1000.0)
        self.assertEqual(records[1]["gmv"], 1200.0)  # $ and , stripped

    def test_parse_raises_when_no_handle_column_found(self) -> None:
        path = self._write_csv("bad.csv", [["Foo", "Bar"], ["1", "2"]])
        with self.assertRaises(ValueError):
            tc.parse(path)

    def test_import_file_writes_rows_and_replaces_by_source_file(self) -> None:
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "warehouse.db"
        try:
            path = self._write_csv("creators.csv", [
                ["handle", "gmv"],
                ["h1", "10"],
            ])
            n1 = tc.import_file(path, dry_run=False)
            n2 = tc.import_file(path, dry_run=False)  # re-import: replace, not duplicate
            self.assertEqual(n1, 1)
            self.assertEqual(n2, 1)
            conn = sqlite3.connect(db.DB_PATH)
            count = conn.execute(
                "SELECT COUNT(*) FROM tiktok_creators WHERE source_file = ?", (path.name,)
            ).fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)
        finally:
            db.DB_PATH = self._orig_db_path

    def test_dry_run_writes_nothing(self) -> None:
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "warehouse.db"
        try:
            path = self._write_csv("creators.csv", [["handle", "gmv"], ["h1", "10"]])
            n = tc.import_file(path, dry_run=True)
            self.assertEqual(n, 0)
            self.assertFalse(db.DB_PATH.exists())
        finally:
            db.DB_PATH = self._orig_db_path


class ApiCrawlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _application(self, handle, user_id="u1"):
        return {"id": f"app-{handle}", "creator": {
            "username": handle, "nickname": f"{handle} nick", "user_id": user_id,
            "follower_count": 100, "content_count": 5, "gmv": {"amount": "50"},
        }}

    def test_pagination_and_complete_flag(self) -> None:
        p1 = _resp(data={"sample_applications": [self._application("h1")], "next_page_token": "n2"})
        p2 = _resp(data={"sample_applications": [self._application("h2")], "next_page_token": ""})
        with patch.object(tc.requests, "post", side_effect=[p1, p2]) as post, \
             patch("time.sleep"):
            creators, complete = tc.fetch_creators()
        self.assertEqual(post.call_count, 2)
        self.assertTrue(complete)
        self.assertEqual(set(creators), {"h1", "h2"})
        self.assertEqual(creators["h1"][2], "u1")  # creator_id preserved

    def test_rolling_window_ceiling_marks_partial(self) -> None:
        p1 = _resp(data={"sample_applications": [self._application("h1")], "next_page_token": "n2"})
        ceiling = _resp(code=tc.ROLLING_WINDOW_EXCEEDED, message="only the most recent N applications")
        with patch.object(tc.requests, "post", side_effect=[p1, ceiling]), patch("time.sleep"):
            creators, complete = tc.fetch_creators()
        self.assertFalse(complete)
        self.assertEqual(set(creators), {"h1"})

    def test_dropped_connection_after_some_pages_preserves_partial_result(self) -> None:
        p1 = _resp(data={"sample_applications": [self._application("h1")], "next_page_token": "n2"})
        with patch.object(
            tc.requests, "post",
            side_effect=[p1] + [requests.exceptions.ConnectionError("reset")] * 6,
        ), patch("time.sleep"):
            creators, complete = tc.fetch_creators()
        self.assertFalse(complete)
        self.assertEqual(set(creators), {"h1"})

    def test_dropped_connection_on_first_page_raises(self) -> None:
        # _request exhausts its own 6-attempt retry budget and raises RuntimeError
        # (not the raw ConnectionError) once every attempt has failed; with zero
        # creators collected yet, fetch_creators has nothing worth keeping and
        # re-raises rather than returning an empty "partial" result.
        with patch.object(
            tc.requests, "post",
            side_effect=[requests.exceptions.ConnectionError("reset")] * 6,
        ), patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                tc.fetch_creators()


class SyncApiWritePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "warehouse.db"
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path

    def _seed(self, handle: str) -> None:
        conn = db.connect()
        with conn:
            tc.ensure_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO tiktok_creators "
                "(handle, display_name, creator_id, source_file, synced_at) VALUES (?,?,?,?,?)",
                (handle, "old", "old-id", tc.API_SOURCE, "before"),
            )
        conn.close()

    def test_complete_crawl_replaces_prior_api_rows(self) -> None:
        self._seed("stale_handle")
        page = _resp(data={"sample_applications": [{
            "id": "a1", "creator": {"username": "fresh_handle", "nickname": "n", "user_id": "u9"},
        }], "next_page_token": ""})
        with patch.object(tc.requests, "post", return_value=page), patch("time.sleep"):
            tc.sync_api(dry_run=False)
        conn = sqlite3.connect(db.DB_PATH)
        handles = {r[0] for r in conn.execute(
            "SELECT handle FROM tiktok_creators WHERE source_file = ?", (tc.API_SOURCE,))}
        conn.close()
        self.assertEqual(handles, {"fresh_handle"})  # stale row gone -- complete pull replaces

    def test_partial_crawl_merges_instead_of_replacing(self) -> None:
        self._seed("existing_handle")
        p1 = _resp(data={"sample_applications": [{
            "id": "a1", "creator": {"username": "new_handle", "nickname": "n", "user_id": "u9"},
        }], "next_page_token": "n2"})
        ceiling = _resp(code=tc.ROLLING_WINDOW_EXCEEDED, message="ceiling")
        with patch.object(tc.requests, "post", side_effect=[p1, ceiling]), patch("time.sleep"):
            tc.sync_api(dry_run=False)
        conn = sqlite3.connect(db.DB_PATH)
        handles = {r[0] for r in conn.execute(
            "SELECT handle FROM tiktok_creators WHERE source_file = ?", (tc.API_SOURCE,))}
        conn.close()
        self.assertEqual(handles, {"existing_handle", "new_handle"})  # both present -- merged


if __name__ == "__main__":
    unittest.main()
