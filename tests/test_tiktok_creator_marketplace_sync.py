"""Hermetic tests for tiktok_creator_marketplace_sync.py -- no network, no warehouse.db.

Covers: schema, handle sources (file/table/dedupe/identifier validation),
group parsing, fraction-share maths, exact-username matching (fuzzy hits are
rejected), found=0 rows for asked-but-absent handles, fetched_at-keyed resume,
the consecutive-error stop rule, and the missing-credentials guard.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import tiktok_creator_marketplace_sync as m


def _conn():
    conn = sqlite3.connect(":memory:")
    m.ensure_schema(conn)
    return conn


class SchemaAndInputs(unittest.TestCase):
    def test_schema_has_resume_and_coverage_columns(self):
        cols = {r[1] for r in _conn().execute("PRAGMA table_info(tiktok_creator_marketplace)")}
        for c in ("creator_handle", "found", "fetched_at", "category_gmv_json", "group_pct_json"):
            self.assertIn(c, cols)

    def test_parse_groups(self):
        self.assertEqual(m.parse_groups(["a=1, 2", "b=3"]), {"a": {"1", "2"}, "b": {"3"}})
        with self.assertRaises(SystemExit):
            m.parse_groups(["nonsense"])

    def test_handles_file_skips_comments_and_at_signs(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write("# roster\n@alpha\n\nbeta\n")
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(m.read_handles_file(fh.name), ["alpha", "beta"])

    def test_dedupe_is_case_insensitive_and_ordered(self):
        self.assertEqual(m.dedupe_handles(["Alpha", "alpha", "@Beta", " "]), ["Alpha", "Beta"])

    def test_table_source_validates_identifiers(self):
        conn = _conn()
        conn.execute("CREATE TABLE r (handle TEXT)")
        conn.executemany("INSERT INTO r VALUES (?)", [("x",), ("x",), (None,), ("y",)])
        self.assertEqual(sorted(m.read_handles_table(conn, "r", "handle")), ["x", "y"])
        with self.assertRaises(SystemExit):
            m.read_handles_table(conn, "r; DROP TABLE r", "handle")

    def test_check_required_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                m.check_required_env()


class ShareMaths(unittest.TestCase):
    def test_share_sums_fractions_and_ignores_blanks(self):
        dist = [{"category_id": "1", "value": "0.25"}, {"category_id": "2", "value": "0.5"},
                {"category_id": "1", "value": ""}, {"category_id": "-1", "value": "0.1"}]
        self.assertEqual(m.share(dist, {"1", "2"}), 0.75)
        self.assertEqual(m.share(dist, {"-1"}), 0.1)
        self.assertEqual(m.share([], {"1"}), 0.0)


class FetchAndWrite(unittest.TestCase):
    def test_fuzzy_search_hit_is_rejected(self):
        with mock.patch.object(m, "call", return_value={"creators": [{"username": "someone_else"}]}):
            self.assertIsNone(m.fetch_one("wanted"))

    def test_exact_match_case_insensitive_fetches_detail(self):
        responses = [
            {"creators": [{"username": "Wanted", "creator_open_id": "oid", "follower_count": 5}]},
            {"creator": {"category_gmv_distribution": [{"category_id": "1", "value": "0.4"}],
                         "gmv_range": {"formatted_range": "$10K+"}, "rating": "4.8"}},
        ]
        with mock.patch.object(m, "call", side_effect=responses):
            d = m.fetch_one("wanted")
        self.assertEqual(d["open_id"], "oid")
        self.assertEqual(d["followers"], 5)  # falls back to the search hit
        self.assertEqual(d["gmv_band"], "$10K+")

    def test_not_found_row_is_still_written(self):
        conn = _conn()
        m.write_row(conn, "ghost", None, {})
        row = conn.execute("SELECT found, fetched_at FROM tiktok_creator_marketplace").fetchone()
        self.assertEqual(row[0], 0)
        self.assertIsNotNone(row[1])

    def test_found_row_stores_group_shares(self):
        conn = _conn()
        d = {"open_id": "o", "followers": 1, "ids": ["1"],
             "dist": [{"category_id": "1", "value": "0.6"}, {"category_id": "-1", "value": "0.4"}],
             "gmv_band": None, "units_band": None, "post_rate": None, "pps": None,
             "rating": None, "commission": None, "collabs": None}
        m.write_row(conn, "h", d, {"shoes": {"1"}})
        grp, unc = conn.execute(
            "SELECT group_pct_json, uncategorised_share FROM tiktok_creator_marketplace").fetchone()
        self.assertEqual(json.loads(grp), {"shoes": 0.6})
        self.assertEqual(unc, 0.4)

    def test_no_groups_leaves_column_null(self):
        conn = _conn()
        d = {"open_id": "o", "followers": 1, "ids": [], "dist": [], "gmv_band": None,
             "units_band": None, "post_rate": None, "pps": None, "rating": None,
             "commission": None, "collabs": None}
        m.write_row(conn, "h", d, {})
        self.assertIsNone(conn.execute("SELECT group_pct_json FROM tiktok_creator_marketplace").fetchone()[0])


class ResumeAndStop(unittest.TestCase):
    def test_resume_skips_recently_asked_including_not_found(self):
        conn = _conn()
        m.write_row(conn, "Seen", None, {})  # asked, absent -> must NOT be re-asked
        conn.execute("""INSERT INTO tiktok_creator_marketplace (creator_handle, found, fetched_at)
                        VALUES ('old', 1, datetime('now', '-90 day'))""")
        todo = m.select_todo(conn, ["seen", "old", "new"], max_age_days=30)
        self.assertEqual(todo, ["old", "new"])
        self.assertEqual(m.select_todo(conn, ["old", "new"], 30, limit=1), ["old"])

    def test_consecutive_errors_stop_the_run(self):
        conn = _conn()
        with mock.patch.object(m, "fetch_one", side_effect=RuntimeError("rate limited")), \
             mock.patch.object(m.time, "sleep"), mock.patch("builtins.print"):
            found, missing, stopped = m.run(conn, [f"h{i}" for i in range(10)], {})
        self.assertTrue(stopped)
        self.assertEqual((found, missing), (0, 0))

    def test_success_resets_error_streak(self):
        conn = _conn()
        seq = [RuntimeError("x")] * (m.MAX_CONSECUTIVE_ERRORS - 1) + [None] \
            + [RuntimeError("x")] * (m.MAX_CONSECUTIVE_ERRORS - 1)
        with mock.patch.object(m, "fetch_one", side_effect=seq), \
             mock.patch.object(m.time, "sleep"), mock.patch("builtins.print"):
            _, missing, stopped = m.run(conn, [f"h{i}" for i in range(len(seq))], {}, pace=0)
        self.assertFalse(stopped)
        self.assertEqual(missing, 1)


if __name__ == "__main__":
    unittest.main()
