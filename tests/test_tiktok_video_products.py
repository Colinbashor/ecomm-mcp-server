"""Every product a TikTok video tags: tiktok_videos_sync's fan-out into
tiktok_shop_video_products, its coverage table, and the month-by-month
backfill that resumes from that coverage.

Hermetic: the API is mocked at tvs._request_page / tvs.fetch_full and every
database is a temp file, so no network and no warehouse.db.
"""
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tiktok_videos_sync as tvs  # noqa: E402
import tiktok_video_products_backfill as bf  # noqa: E402
from warehouse import db as wdb  # noqa: E402


def _video(vid, gmv, *pids, user="creator"):
    return {"id": vid, "username": user, "title": f"t{vid}", "gmv": {"amount": str(gmv), "currency": "USD"},
            "sku_orders": 1, "units_sold": 1, "views": 10, "click_through_rate": "0.01",
            "video_post_time": "2026-09-01 10:00:00",
            "products": [{"id": p, "name": f"name {p}"} for p in pids]}


def _page(videos, token="", total=None):
    d = {"videos": videos, "next_page_token": token}
    if total is not None:
        d["total_count"] = total
    return {"code": 0, "data": d}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("TIKTOK_APP_KEY", "TIKTOK_SHOP_CIPHER"):
        monkeypatch.setenv(k, "x")


# ---- product fan-out ----------------------------------------------------------

def test_product_tags_positions_dedupe_and_missing_ids():
    v = {"products": [{"id": "A", "name": "a"}, {"id": "B", "name": "b"}, {"name": "no id"},
                      {"id": "A", "name": "a again"}, {"id": "C"}]}
    assert tvs.product_tags(v) == [("A", "a", 1), ("B", "b", 2), ("C", None, 5)]
    assert tvs.product_tags({"products": None}) == []
    assert tvs.product_tags({}) == []


def test_fetch_full_fans_out_every_product_and_keeps_parent_rows_unchanged():
    pages = [_page([_video("v1", 50, "A", "B"), _video("v2", 20, "C")], token="p2", total=3),
             _page([_video("v3", 0, "D", "E", "F")])]
    with mock.patch.object(tvs, "_request_page", side_effect=pages):
        res = tvs.fetch_full("AFFILIATES", "2026-09-01", "2026-10-01")
    # parent rows: still ONLY the first product, plus the product count
    assert [(r["video_id"], r["product_id"], r["product_name"], r["product_count"]) for r in res.rows] == [
        ("v1", "A", "name A", 2), ("v2", "C", "name C", 1), ("v3", "D", "name D", 3)]
    assert res.tags == [("v1", "AFFILIATES", "A", "name A", 1), ("v1", "AFFILIATES", "B", "name B", 2),
                        ("v2", "AFFILIATES", "C", "name C", 1), ("v3", "AFFILIATES", "D", "name D", 1),
                        ("v3", "AFFILIATES", "E", "name E", 2), ("v3", "AFFILIATES", "F", "name F", 3)]
    assert (res.api_total, len(res.video_ids), res.multi_product_videos) == (3, 3, 2)
    assert res.coverage_status() == "complete"
    # fetch() (the pre-existing entry point) returns the same row dicts
    with mock.patch.object(tvs, "_request_page", side_effect=pages):
        assert tvs.fetch("AFFILIATES", "2026-09-01", "2026-10-01") == res.rows


def test_coverage_status_short_empty_and_winners_only():
    with mock.patch.object(tvs, "_request_page", side_effect=[_page([_video("v1", 5, "A")], total=2)]):
        assert tvs.fetch_full("AFFILIATES", "a", "b").coverage_status() == "short"
    with mock.patch.object(tvs, "_request_page", side_effect=[_page([], total=0)]):
        assert tvs.fetch_full("AFFILIATES", "a", "b").coverage_status() == "empty"
    # winners-only stops at the first $0 video by design: not "short"
    with mock.patch.object(tvs, "_request_page",
                           side_effect=[_page([_video("v1", 5, "A"), _video("v2", 0, "B")], token="t", total=9)]):
        res = tvs.fetch_full("AFFILIATES", "a", "b", gmv_positive_only=True)
    assert res.coverage_status() == "complete" and [t[0] for t in res.tags] == ["v1"]


def _fake_fetch(tags_by_video, api_total=None):
    res = tvs.FetchResult()
    for vid, pids in tags_by_video.items():
        res.video_ids.add(vid)
        res.rows.append({
            "video_id": vid, "account_type": "AFFILIATES", "title": "t", "username": "u",
            "gmv": 1.0, "currency": "USD", "sku_orders": 1, "units_sold": 1, "views": 1,
            "click_through_rate": 0.0, "video_post_time": "2026-09-01",
            "product_id": pids[0] if pids else None, "product_name": None,
            "product_count": len(pids), "window_start": "?", "window_end": "?"})
        res.tags += [(vid, "AFFILIATES", p, f"name {p}", i) for i, p in enumerate(pids, 1)]
        res.multi_product_videos += len(pids) > 1
    res.api_total = len(tags_by_video) if api_total is None else api_total
    return res


def test_run_writes_parent_child_and_coverage(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    monkeypatch.setattr(wdb, "DB_PATH", db)
    with mock.patch.object(tvs, "fetch_full", return_value=_fake_fetch({"v1": ["A", "B"], "v2": ["C"]})):
        assert tvs.sync("2026-09-07", "2026-09-14", ["AFFILIATES"]) == 2
    c = sqlite3.connect(db)
    assert c.execute("SELECT video_id, product_id, product_count FROM tiktok_shop_videos ORDER BY 1").fetchall() \
        == [("v1", "A", 2), ("v2", "C", 1)]
    assert c.execute("SELECT video_id, product_id, position, first_seen, last_seen "
                     "FROM tiktok_shop_video_products ORDER BY 1, 3").fetchall() == [
        ("v1", "A", 1, "2026-09-07", "2026-09-14"), ("v1", "B", 2, "2026-09-07", "2026-09-14"),
        ("v2", "C", 1, "2026-09-07", "2026-09-14")]
    assert c.execute("SELECT status, videos_returned, multi_product_videos, product_rows, source, attempts "
                     "FROM tiktok_shop_video_products_coverage").fetchall() == [("complete", 2, 1, 3, "sync", 1)]


def test_older_window_never_overwrites_newer_position_but_widens_range(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    monkeypatch.setattr(wdb, "DB_PATH", db)
    with mock.patch.object(tvs, "fetch_full", return_value=_fake_fetch({"v1": ["A", "B"]})):
        tvs.sync("2026-09-07", "2026-09-14", ["AFFILIATES"])
    # an older backfill window saw B first
    with mock.patch.object(tvs, "fetch_full", return_value=_fake_fetch({"v1": ["B", "A"]})):
        tvs.sync("2026-05-01", "2026-06-01", ["AFFILIATES"])
    c = sqlite3.connect(db)
    got = dict((p, (pos, f, l)) for p, pos, f, l in c.execute(
        "SELECT product_id, position, first_seen, last_seen FROM tiktok_shop_video_products"))
    assert got == {"A": (1, "2026-05-01", "2026-09-14"), "B": (2, "2026-05-01", "2026-09-14")}
    # a NEWER window that reorders does update it
    with mock.patch.object(tvs, "fetch_full", return_value=_fake_fetch({"v1": ["B", "A"]})):
        tvs.sync("2026-09-14", "2026-09-21", ["AFFILIATES"])
    assert dict(c.execute("SELECT product_id, position FROM tiktok_shop_video_products")) == {"A": 2, "B": 1}


def test_upsert_names_columns_so_physical_order_cannot_shift_values(tmp_path):
    # named columns: a table whose physical column order differs (e.g. after an ALTER)
    c = sqlite3.connect(tmp_path / "w.db")
    c.execute("""CREATE TABLE tiktok_shop_video_products (synced_at TEXT NOT NULL, last_seen TEXT NOT NULL,
                 position INTEGER, product_id TEXT NOT NULL, first_seen TEXT NOT NULL, video_id TEXT NOT NULL,
                 account_type TEXT NOT NULL, product_name TEXT,
                 PRIMARY KEY (video_id, account_type, product_id))""")
    tvs.ensure_products_tables(c)
    tvs.write_product_tags(c, [("v1", "AFFILIATES", "A", "name A", 2)], "2026-09-01", "2026-10-01", "stamp")
    row = c.execute("SELECT video_id, account_type, product_id, product_name, position, first_seen, last_seen, "
                    "synced_at FROM tiktok_shop_video_products").fetchone()
    assert row == ("v1", "AFFILIATES", "A", "name A", 2, "2026-09-01", "2026-10-01", "stamp")


# ---- backfill -------------------------------------------------------------------

def test_month_windows_cut_at_month_boundaries():
    assert bf.month_windows(date(2026, 4, 19), date(2026, 7, 15)) == [
        ("2026-04-19", "2026-05-01"), ("2026-05-01", "2026-06-01"),
        ("2026-06-01", "2026-07-01"), ("2026-07-01", "2026-07-15")]
    assert bf.month_windows(date(2025, 12, 20), date(2026, 1, 2)) == [
        ("2025-12-20", "2026-01-01"), ("2026-01-01", "2026-01-02")]
    assert bf.month_windows(date(2026, 5, 1), date(2026, 5, 1)) == []


class FakeApi:
    """Refuses windows that start before `floor` (28001022), like the real API."""

    def __init__(self, floor, fail=()):
        self.floor, self.fail, self.calls = floor, set(fail), []

    def __call__(self, at, start, end, gmv_positive_only=False):
        self.calls.append((at, start, end))
        if start < self.floor:
            raise RuntimeError(f"TikTok video API 200 code={tvs.OUT_OF_RETENTION_CODE}: Invalid Parameter")
        if (start, end) in self.fail:
            raise RuntimeError("TikTok video API failed after retries (last attempt exhausted).")
        # a window with only single-product videos writes ONE tag row each
        return _fake_fetch({f"{at}-{start}": ["A"]})


def _db(tmp_path):
    c = sqlite3.connect(tmp_path / "w.db")
    tvs.ensure_products_tables(c)
    return c


def test_backfill_steps_past_the_floor_and_records_what_was_asked(tmp_path):
    c, api = _db(tmp_path), FakeApi(floor="2026-04-19")
    with mock.patch.object(tvs, "fetch_full", api):
        tally = bf.run(c, date(2026, 4, 17), date(2026, 6, 10), ["AFFILIATES"])
    assert tally == {"complete": 3}
    cov = c.execute("SELECT window_start, window_end, status FROM tiktok_shop_video_products_coverage "
                    "ORDER BY 1").fetchall()
    assert cov == [("2026-04-17", "2026-05-01", "out_of_retention"),
                   ("2026-04-18", "2026-05-01", "out_of_retention"),
                   ("2026-04-19", "2026-05-01", "complete"),
                   ("2026-05-01", "2026-06-01", "complete"),
                   ("2026-06-01", "2026-06-10", "complete")]
    assert bf.run_status(tally) == "ok"


def test_backfill_resumes_from_coverage_not_from_tag_rows(tmp_path):
    c, api = _db(tmp_path), FakeApi(floor="2026-04-19")
    with mock.patch.object(tvs, "fetch_full", api):
        bf.run(c, date(2026, 4, 17), date(2026, 6, 10), ["AFFILIATES"])
    # wipe every tag row: absence of rows must NOT make the windows look unpulled
    c.execute("DELETE FROM tiktok_shop_video_products")
    c.commit()
    api2 = FakeApi(floor="2026-04-19")
    with mock.patch.object(tvs, "fetch_full", api2):
        tally = bf.run(c, date(2026, 4, 17), date(2026, 6, 10), ["AFFILIATES"])
    assert api2.calls == [] and tally == {"skipped": 3}
    # the next day the default start moves up a day; the complete window still spans it
    with mock.patch.object(tvs, "fetch_full", api2):
        bf.run(c, date(2026, 4, 20), date(2026, 6, 10), ["AFFILIATES"])
    assert api2.calls == []


def test_backfill_failed_window_retried_until_the_poison_guard(tmp_path):
    c = _db(tmp_path)
    api = FakeApi(floor="2026-01-01", fail={("2026-05-01", "2026-06-01")})
    with mock.patch.object(tvs, "fetch_full", api):
        for _ in range(bf.MAX_ATTEMPTS + 2):
            tally = bf.run(c, date(2026, 5, 1), date(2026, 6, 1), ["AFFILIATES"])
    assert len(api.calls) == bf.MAX_ATTEMPTS
    assert c.execute("SELECT status, attempts FROM tiktok_shop_video_products_coverage").fetchone() == \
        ("failed", bf.MAX_ATTEMPTS)
    assert tally == {"skipped": 1}


def test_backfill_out_of_retention_after_the_first_window_is_final(tmp_path):
    c, api = _db(tmp_path), FakeApi(floor="2026-06-01")
    with mock.patch.object(tvs, "fetch_full", api):
        # starting INSIDE the first month: only the first window steps, the rest are final
        bf.run(c, date(2026, 5, 31), date(2026, 6, 2), ["AFFILIATES"])
    st = c.execute("SELECT window_start, window_end, status FROM tiktok_shop_video_products_coverage "
                   "ORDER BY 1").fetchall()
    assert st == [("2026-05-31", "2026-06-01", "out_of_retention"), ("2026-06-01", "2026-06-02", "complete")]


def test_backfill_run_status():
    assert bf.run_status({"complete": 4}) == "ok"
    assert bf.run_status({"skipped": 4}) == "ok"
    assert bf.run_status({"complete": 3, "short": 1}) == "degraded"
    assert bf.run_status({"complete": 3, "empty": 1}) == "degraded"
    assert bf.run_status({}) == "degraded"                  # asked for nothing, got nothing
    assert bf.run_status({"complete": 3, "failed": 1}) == "error"


def test_backfill_never_writes_tiktok_shop_videos(tmp_path):
    c, api = _db(tmp_path), FakeApi(floor="2026-01-01")
    with mock.patch.object(tvs, "fetch_full", api):
        bf.run(c, date(2026, 9, 1), date(2026, 10, 1), ["AFFILIATES"])
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'tiktok_shop_videos'").fetchone() is None
