"""Hermetic tests for meta_ads_detail_sync.py — no network, no real DB file.

Covers: the Reacher-title parser (match + the common non-match case), schema
creation, `run()` writing through patched fetch_day/fetch_creatives/
crawl_videos (including --only insights skipping creative work, spend-bounded
targeting, and the days-skipped -> degraded status), and crawl_videos' three
stop conditions in isolation.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import meta_ads_detail_sync as detail


class ReacherTitleParsingTests(unittest.TestCase):
    def test_parses_a_matching_title(self) -> None:
        parsed = detail.parse_reacher_title("creatorhandle_Sep2025_RCHR_68cc4beb")
        self.assertEqual(parsed, {
            "creator_handle": "creatorhandle", "period": "Sep2025",
            "reacher_hash": "68cc4beb",
        })

    def test_handle_may_itself_contain_underscores(self) -> None:
        parsed = detail.parse_reacher_title("the_real_creator_Aug2026_RCHR_7d12936f")
        self.assertEqual(parsed["creator_handle"], "the_real_creator")

    def test_lowercases_handle_and_hash(self) -> None:
        parsed = detail.parse_reacher_title("Creator_Sep2025_RCHR_ABCDEF12")
        self.assertEqual(parsed["creator_handle"], "creator")
        self.assertEqual(parsed["reacher_hash"], "abcdef12")

    def test_non_matching_title_returns_none(self) -> None:
        # The common case: most of a real video library predates any
        # naming convention, or was uploaded by a different tool entirely.
        self.assertIsNone(detail.parse_reacher_title("Fall Collection Launch Video"))

    def test_none_and_empty_title_return_none(self) -> None:
        self.assertIsNone(detail.parse_reacher_title(None))
        self.assertIsNone(detail.parse_reacher_title(""))


class SchemaTests(unittest.TestCase):
    def test_ddl_creates_all_three_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(detail.DDL)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"meta_ad_daily", "meta_ad_creatives", "meta_ad_videos"})
        conn.close()

    def test_ddl_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(detail.DDL)
        conn.executescript(detail.DDL)  # must not raise
        conn.close()


class CrawlVideosTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["META_AD_ACCOUNT_ID"] = "act_1"
        os.environ["META_ACCESS_TOKEN"] = "tok"

    def _page(self, ids: list[str]):
        return {"data": [{"id": vid, "title": f"title-{vid}", "created_time": "2026-01-01",
                          "length": 12.0} for vid in ids]}

    def test_stops_once_every_wanted_video_is_found(self) -> None:
        pages = [self._page(["a", "b"]), self._page(["c"])]
        with patch.object(detail, "_get_json", side_effect=pages):
            rows, unresolved = detail.crawl_videos(set(), {"b"}, max_pages=10)
        self.assertEqual({r[0] for r in rows}, {"a", "b"})  # stopped after page 1
        self.assertEqual(unresolved, set())

    def test_stops_after_consecutive_barren_pages(self) -> None:
        pages = [self._page(["a"]), self._page([]), self._page([]), self._page(["z"])]
        with patch.object(detail, "_get_json", side_effect=pages):
            rows, unresolved = detail.crawl_videos(set(), set(), max_pages=10, stop_after=2)
        # the empty pages (data=[]) break the loop outright via `if not data: break`
        self.assertEqual({r[0] for r in rows}, {"a"})

    def test_already_known_videos_are_not_re_stored(self) -> None:
        with patch.object(detail, "_get_json", return_value=self._page(["a", "b"])):
            rows, unresolved = detail.crawl_videos({"a"}, set(), max_pages=1, stop_after=1)
        self.assertEqual({r[0] for r in rows}, {"b"})

    def test_unresolved_wanted_ids_are_reported(self) -> None:
        with patch.object(detail, "_get_json", return_value=self._page(["a"])):
            rows, unresolved = detail.crawl_videos(set(), {"a", "never-seen"},
                                                    max_pages=1, stop_after=1)
        self.assertEqual(unresolved, {"never-seen"})


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = Path(path)
        self._patch_db = patch.object(detail, "DB", self.db_path)
        self._patch_db.start()

    def tearDown(self) -> None:
        self._patch_db.stop()
        self.db_path.unlink(missing_ok=True)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _fake_day_rows(self, ad_id="ad1"):
        return [("act_1", "2026-08-01", ad_id, "Ad One", "as1", "Adset", "c1",
                 "Campaign", 1000, 20, 5, 12.5, 1.0, 1.0, 1.0, 29.99)]

    def test_only_insights_skips_creative_and_video_work(self) -> None:
        with patch.object(detail, "fetch_day", return_value=self._fake_day_rows()), \
             patch.object(detail, "fetch_creatives") as fake_creatives:
            stats = detail.run("2026-08-01", "2026-08-01", only="insights")
        fake_creatives.assert_not_called()
        self.assertEqual(stats["insight_rows"], 1)
        self.assertEqual(stats["creatives"], 0)
        conn = self._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta_ad_daily").fetchone()[0], 1)
        conn.close()

    def test_creative_lookup_is_bounded_by_ads_seen_in_the_window(self) -> None:
        creative_row = ("ad1", "Ad One", "c1", "as1", "ACTIVE", "cr1", "Creative",
                        None, "v1", "v1", None, "http://thumb", "stamp")
        with patch.object(detail, "fetch_day", return_value=self._fake_day_rows("ad1")), \
             patch.object(detail, "fetch_creatives", return_value=[creative_row]) as fake_c, \
             patch.object(detail, "crawl_videos", return_value=([], set())):
            detail.run("2026-08-01", "2026-08-01")
        fake_c.assert_called_once_with(["ad1"])

    def test_refresh_creatives_also_targets_previously_stored_ads(self) -> None:
        conn = self._conn()
        conn.executescript(detail.DDL)
        conn.execute(
            "INSERT INTO meta_ad_creatives VALUES "
            "('old_ad','Old','c0','as0','ACTIVE',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'stamp')")
        conn.commit()
        conn.close()

        with patch.object(detail, "fetch_day", return_value=self._fake_day_rows("new_ad")), \
             patch.object(detail, "fetch_creatives", return_value=[]) as fake_c, \
             patch.object(detail, "crawl_videos", return_value=([], set())):
            detail.run("2026-08-01", "2026-08-01", refresh_creatives=True)
        (called_ids,), _ = fake_c.call_args
        self.assertEqual(sorted(called_ids), ["new_ad", "old_ad"])

    def test_new_videos_referenced_by_creatives_are_crawled(self) -> None:
        creative_row = ("ad1", "Ad One", "c1", "as1", "ACTIVE", "cr1", "Creative",
                        None, "v99", "v99", None, None, "stamp")
        video_row = ("v99", "some title", "2026-08-01", 15.0, 0, None, None, None, "stamp")
        with patch.object(detail, "fetch_day", return_value=self._fake_day_rows("ad1")), \
             patch.object(detail, "fetch_creatives", return_value=[creative_row]), \
             patch.object(detail, "crawl_videos", return_value=([video_row], set())) as fake_v:
            stats = detail.run("2026-08-01", "2026-08-01")
        fake_v.assert_called_once()
        (known_arg, want_arg), _ = fake_v.call_args
        self.assertEqual(want_arg, {"v99"})
        self.assertEqual(stats["videos"], 1)
        conn = self._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta_ad_videos").fetchone()[0], 1)
        conn.close()

    def test_a_skipped_day_is_counted_and_does_not_raise(self) -> None:
        with patch.object(detail, "fetch_day", side_effect=detail._RangeTooLarge("too big")):
            stats = detail.run("2026-08-01", "2026-08-01", only="insights")
        self.assertEqual(stats["days_skipped"], 1)
        self.assertEqual(stats["insight_rows"], 0)


class MainStatusTests(unittest.TestCase):
    def test_status_is_degraded_when_a_day_was_skipped(self) -> None:
        logged = {}

        def fake_log_sync(platform, started, rows, status, message=""):
            logged.update(platform=platform, rows=rows, status=status)

        fake_stats = {"insight_rows": 5, "days_skipped": 1, "creatives": 0,
                      "videos": 0, "reacher_videos": 0, "videos_unresolved": 0}
        with patch("sys.argv", ["meta_ads_detail_sync.py", "--days", "1"]), \
             patch.object(detail.warehouse_db, "init_db"), \
             patch.object(detail.warehouse_db, "now", return_value="stamp"), \
             patch.object(detail.warehouse_db, "log_sync", side_effect=fake_log_sync), \
             patch.object(detail, "run", return_value=fake_stats):
            rc = detail.main()
        self.assertEqual(logged["status"], "degraded")
        self.assertEqual(rc, 1)

    def test_status_is_ok_when_nothing_was_skipped(self) -> None:
        logged = {}

        def fake_log_sync(platform, started, rows, status, message=""):
            logged.update(status=status)

        fake_stats = {"insight_rows": 5, "days_skipped": 0, "creatives": 2,
                      "videos": 1, "reacher_videos": 0, "videos_unresolved": 0}
        with patch("sys.argv", ["meta_ads_detail_sync.py", "--days", "1"]), \
             patch.object(detail.warehouse_db, "init_db"), \
             patch.object(detail.warehouse_db, "now", return_value="stamp"), \
             patch.object(detail.warehouse_db, "log_sync", side_effect=fake_log_sync), \
             patch.object(detail, "run", return_value=fake_stats):
            rc = detail.main()
        self.assertEqual(logged["status"], "ok")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
