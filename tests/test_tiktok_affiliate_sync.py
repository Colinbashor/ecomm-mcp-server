"""tiktok_affiliate_sync: seller-side Affiliate orders + sample applications + creator crosswalk.

Hermetic: no network, no warehouse.db (an in-memory SQLite per test). Pinned
here are the decisions that, if silently reversed, would produce a plausible
WRONG number rather than an error:

  * stopping pagination on a SHORT page (the live orders endpoint returns 97 of
    100 with a next token, so short != finished);
  * a stored `{}` money object turning into 0 instead of NULL;
  * the hundredths-of-a-percent commission unit (800 = 8%) vs the sample
    endpoint's fraction (0.10 = 10%);
  * a renamed creator leaving their old-handle row behind (double GMV);
  * positional INSERTs after an ALTER TABLE;
  * a sample order that TikTok cannot find being re-asked forever, or never
    re-asked;
  * an empty pull logging `ok`;
  * orders.creator_id being treated as creator_open_id (it is not).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import unittest
from unittest import mock

import tiktok_affiliate_sync as tas

UTC = dt.timezone.utc


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(tas.DDL)
    conn.execute("""CREATE TABLE orders (platform TEXT, order_id TEXT, order_date TEXT, sku TEXT,
                    is_sample INTEGER, creator TEXT, creator_id TEXT,
                    PRIMARY KEY (platform, order_id, sku))""")
    return conn


def _sku(**kw) -> dict:
    s = {"sku_id": "S1", "product_id": "P1", "quantity": 1, "content_type": "VIDEO",
         "content_id": "C1", "creator_username": "alice",
         "price": {"amount": "20.00", "currency": "USD"},
         "estimated_commission_base": {"amount": "25", "currency": "USD"},
         "estimated_paid_commission": {"amount": "2.5", "currency": "USD"},
         "estimated_paid_shop_ads_commission": {}, "actual_paid_commission": {},
         "commission_rate": "1000", "shop_ads_commission_rate": "800"}
    s.update(kw)
    return s


def _order(oid="O1", ts=1760000000, skus=None) -> dict:
    return {"id": oid, "create_time": ts, "skus": skus if skus is not None else [_sku()]}


def _app(aid="A1", order_id="SO1", open_id="OPEN1", username="alice", nickname="Alice",
         status="COMPLETED", product="P1") -> dict:
    return {"id": aid, "order_id": order_id, "status": status, "commission_rate": "0.10",
            "product": {"id": product, "sku_id": "K1", "title": "Dress"},
            "creator": {"creator_open_id": open_id, "username": username, "nickname": nickname,
                        "avatar_url": "https://signed.example/expires", "gmv": {"amount": "999"},
                        "follower_count": 1234}}


class ScriptedCall:
    """Stands in for tas._call: returns one scripted `data` dict per call."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, path, query, body, pace=0.0):
        self.calls.append((path, dict(query), dict(body)))
        return self.pages.pop(0) if self.pages else {}


class PaginationTests(unittest.TestCase):
    def test_orders_keep_paging_past_a_short_page_with_a_token(self):
        # 97 of 100 with a token is what the live endpoint does mid-window.
        call = ScriptedCall([
            {"total_count": 4, "orders": [_order("O1"), _order("O2")], "next_page_token": "t1"},
            {"orders": [_order("O3")], "next_page_token": "t2"},
            {"orders": [_order("O4")], "next_page_token": ""},
        ])
        orders, meta = tas.page_orders(0, 10, call)
        self.assertEqual(sorted(o["id"] for o in orders), ["O1", "O2", "O3", "O4"])
        self.assertEqual(meta["pages"], 3)
        self.assertEqual(call.calls[1][1]["page_token"], "t1")

    def test_orders_stop_on_an_empty_page_even_with_a_token(self):
        call = ScriptedCall([{"orders": [_order("O1")], "next_page_token": "t1"},
                             {"orders": [], "next_page_token": "t2"}])
        orders, meta = tas.page_orders(0, 10, call)
        self.assertEqual(len(orders), 1)
        self.assertEqual(meta["pages"], 2)

    def test_orders_dedupe_by_id_across_pages(self):
        call = ScriptedCall([{"orders": [_order("O1")], "next_page_token": "t1"},
                             {"orders": [_order("O1")], "next_page_token": ""}])
        orders, _ = tas.page_orders(0, 10, call)
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(orders[0]["skus"]), 1)

    def test_order_split_across_pages_keeps_every_line(self):
        # page_size counts SKU LINES, so a 2-line order can straddle a page
        # boundary. Overwriting by id would silently drop the first half.
        call = ScriptedCall([{"orders": [_order("O1", skus=[_sku(sku_id="A")])], "next_page_token": "t"},
                             {"orders": [_order("O1", skus=[_sku(sku_id="B")])], "next_page_token": ""}])
        orders, _ = tas.page_orders(0, 10, call)
        self.assertEqual(sorted(s["sku_id"] for s in orders[0]["skus"]), ["A", "B"])

    def test_sample_list_keeps_paging_past_a_short_page(self):
        conn = _conn()
        call = ScriptedCall([
            {"total_count": 3, "sample_applications": [_app("A1")], "next_page_token": "t"},
            {"sample_applications": [_app("A2", order_id="SO2"), _app("A3", order_id="SO3")],
             "next_page_token": ""},
        ])
        with mock.patch.object(tas, "_call", call):
            r = tas.list_status(conn, "COMPLETED", call)
        self.assertEqual(r["rows"], 3)
        cov = conn.execute("SELECT total_count, rows_returned, pages, capped, status "
                           "FROM tiktok_affiliate_sample_coverage WHERE kind='status'").fetchone()
        self.assertEqual(cov, (3, 3, 2, 0, "complete"))

    def test_sample_list_flags_the_9999_ceiling(self):
        conn = _conn()
        call = ScriptedCall([{"total_count": 9999, "sample_applications": [_app()], "next_page_token": ""}])
        r = tas.list_status(conn, "COMPLETED", call)
        self.assertEqual(r["capped"], 1)
        st = conn.execute("SELECT status FROM tiktok_affiliate_sample_coverage").fetchone()[0]
        self.assertEqual(st, "short")

    def test_offset_ceiling_error_ends_the_slice_as_capped_not_failed(self):
        # Offset 10,000 answers 98001004. The rows before it are real and kept.
        conn = _conn()
        pages = [{"total_count": 9999, "sample_applications": [_app()], "next_page_token": "offset=50"}]

        def call(path, query, body, pace=0.0):
            if query.get("page_token"):
                raise tas.ApiError("Only the latest 10,000 ...", tas.LIST_DEPTH_CODE)
            return pages[0]

        r = tas.list_status(conn, "COMPLETED", call)
        self.assertEqual((r["rows"], r["capped"]), (1, 1))
        cov = conn.execute("SELECT status, capped FROM tiktok_affiliate_sample_coverage").fetchone()
        self.assertEqual(cov, ("short", 1))

    def test_other_api_errors_still_fail_the_slice(self):
        conn = _conn()

        def call(path, query, body, pace=0.0):
            raise tas.ApiError("bad param", 36009004)

        with self.assertRaises(tas.ApiError):
            tas.list_status(conn, "COMPLETED", call)
        self.assertEqual(conn.execute("SELECT status FROM tiktok_affiliate_sample_coverage").fetchone()[0],
                         "failed")

    def test_product_slices_cover_unresolved_orders_once(self):
        conn = _conn()
        conn.execute("CREATE TABLE catalog_tiktok (product_id TEXT, sku_id TEXT, seller_sku TEXT)")
        conn.executemany("INSERT INTO catalog_tiktok VALUES (?, ?, ?)",
                         [("P1", "K1", "S100"), ("P2", "K2", "S200"), ("P3", "K3", "S300")])
        conn.executemany("INSERT INTO orders VALUES ('tiktok', ?, ?, ?, 1, 'A', '7')",
                         [("SO1", "2024-03-01", "S100"), ("SO2", "2024-04-01", "S200"),
                          ("SO3", "2024-05-01", "S300")])
        tas.upsert_applications(conn, [_app("A3", "SO3")], "list:COMPLETED", "now")
        self.assertEqual(tas.products_to_partition(conn, "2023-09-01"), ["P2", "P1"])
        call = ScriptedCall([{"total_count": 1, "sample_applications": [_app("A2", "SO2", product="P2")],
                              "next_page_token": ""}])
        tas.list_product_completed(conn, "P2", call)
        self.assertEqual(call.calls[0][2], {"product_id": "P2", "status": "COMPLETED"})
        self.assertEqual(tas.products_to_partition(conn, "2023-09-01"), ["P1"])

    def test_product_slices_need_no_catalog_table(self):
        conn = _conn()
        conn.execute("INSERT INTO orders VALUES ('tiktok', 'SO1', '2024-03-01', 'S100', 1, 'A', '7')")
        self.assertEqual(tas.products_to_partition(conn, "2023-09-01"), [])

    def test_explicit_product_ids_bypass_the_catalog(self):
        conn = _conn()
        args = mock.Mock(list=False, by_product=True, lookup=False, product_ids="P9, P8",
                         since="2023-09-01", until=None, max_products=0)
        with mock.patch.object(tas, "list_product_completed",
                               return_value={"rows": 1, "capped": 0}) as lp, \
                mock.patch.object(tas.warehouse_db, "log_sync"):
            self.assertEqual(tas.cmd_samples(conn, args), 0)
        self.assertEqual([c.args[1] for c in lp.call_args_list], ["P9", "P8"])


class WindowTests(unittest.TestCase):
    def test_window_reporting_more_than_split_at_is_split_before_paging(self):
        conn = _conn()
        start = dt.datetime(2026, 9, 1, tzinfo=UTC)
        end = start + dt.timedelta(days=2)
        call = ScriptedCall([
            {"total_count": 50},                                   # probe whole window -> split
            {"total_count": 3},                                    # probe first half
            {"total_count": 1, "orders": [_order("O1")], "next_page_token": ""},
            {"total_count": 2},                                    # probe second half
            {"total_count": 1, "orders": [_order("O2")], "next_page_token": ""},
        ])
        r = tas.sync_order_window(conn, start, end, call, split_at=10)
        self.assertEqual(r["orders"], 2)
        rows = conn.execute("SELECT window_start, window_end, status, lines_written "
                            "FROM tiktok_affiliate_order_coverage ORDER BY window_start, window_end").fetchall()
        halves = [w for w in rows if (w[0], w[1]) != (start.isoformat(), end.isoformat())]
        self.assertEqual(len(halves), 2)
        self.assertEqual(halves[0][1], halves[1][0])
        # the PARENT is recorded too, so run_orders' skip check can find it
        parent = [w for w in rows if (w[0], w[1]) == (start.isoformat(), end.isoformat())]
        self.assertEqual([(p[2], p[3]) for p in parent], [("complete", 2)])

    def test_a_split_window_is_skipped_once_complete(self):
        conn = _conn()
        start = dt.datetime(2026, 9, 1, tzinfo=UTC)
        end = start + dt.timedelta(days=7)
        call = ScriptedCall([{"total_count": 50},
                             {"total_count": 1}, {"total_count": 1, "orders": [_order("O1")], "next_page_token": ""},
                             {"total_count": 1}, {"total_count": 1, "orders": [_order("O2")], "next_page_token": ""}])
        with mock.patch.object(tas, "SPLIT_AT", 10):
            tas.run_orders(conn, start, end, refresh_after=None, call=call)
            again = ScriptedCall([])
            stats = tas.run_orders(conn, start, end, refresh_after=None, call=again)
        self.assertEqual(stats.get("skipped"), 1)
        self.assertEqual(again.calls, [])

    def test_daily_windows_land_on_the_backfill_grid(self):
        # A trailing --days 14 run on any day must ask the SAME windows the
        # backfill asked, or coverage rows pile up and never match.
        backfill = set(tas.plan_windows(*tas.snap_to_grid(tas.GRID_ANCHOR,
                                                          dt.datetime(2026, 10, 16, tzinfo=UTC))))
        for day in (14, 15, 16, 20):
            end = dt.datetime(2026, 10, day, tzinfo=UTC)
            ws = tas.plan_windows(*tas.snap_to_grid(end - dt.timedelta(days=14), end))
            with self.subTest(day=day):
                self.assertTrue(all(b - a == dt.timedelta(days=7) for a, b in ws))
                self.assertLessEqual(ws[0][0], end - dt.timedelta(days=14))
                self.assertGreaterEqual(ws[-1][1], end)
                old = [w for w in ws if w[1] <= dt.datetime(2026, 10, 16, tzinfo=UTC)]
                self.assertTrue(set(old) <= backfill)

    def test_short_window_is_recorded_short(self):
        conn = _conn()
        start = dt.datetime(2026, 9, 1, tzinfo=UTC)
        call = ScriptedCall([{"total_count": 1},
                             {"total_count": 5, "orders": [_order("O1")], "next_page_token": ""}])
        r = tas.sync_order_window(conn, start, start + dt.timedelta(days=1), call)
        self.assertEqual(r["short"], 1)
        self.assertEqual(conn.execute("SELECT status FROM tiktok_affiliate_order_coverage").fetchone()[0], "short")

    def test_total_count_is_compared_to_lines_not_orders(self):
        # Live: total_count == lines written in every window; one 2-line order
        # makes orders < total_count without anything missing.
        conn = _conn()
        start = dt.datetime(2026, 9, 1, tzinfo=UTC)
        call = ScriptedCall([{"total_count": 1},
                             {"total_count": 3, "next_page_token": "",
                              "orders": [_order("O1", skus=[_sku(sku_id="A"), _sku(sku_id="B")]),
                                         _order("O2")]}])
        r = tas.sync_order_window(conn, start, start + dt.timedelta(days=1), call)
        self.assertEqual((r["orders"], r["lines"], r["short"]), (2, 3, 0))

    def test_complete_windows_are_skipped_unless_inside_the_refresh_period(self):
        conn = _conn()
        start = dt.datetime(2026, 9, 1, tzinfo=UTC)
        for w0, w1 in tas.plan_windows(start, start + dt.timedelta(days=14)):
            tas._record_order_window(conn, w0.isoformat(timespec="seconds"), w1.isoformat(timespec="seconds"),
                                     status="complete")
        pulled = []
        with mock.patch.object(tas, "sync_order_window",
                               side_effect=lambda c, a, b, call: pulled.append(a) or
                               {"orders": 0, "lines": 0, "short": 0}):
            stats = tas.run_orders(conn, start, start + dt.timedelta(days=14),
                                   refresh_after=start + dt.timedelta(days=7))
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(pulled, [start + dt.timedelta(days=7)])

    def test_windows_never_exceed_the_api_cap(self):
        ws = tas.plan_windows(dt.datetime(2023, 9, 1, tzinfo=UTC), dt.datetime(2026, 10, 15, tzinfo=UTC))
        self.assertTrue(all(b - a <= dt.timedelta(days=90) for a, b in ws))
        self.assertEqual(ws[0][0], dt.datetime(2023, 9, 1, tzinfo=UTC))
        self.assertTrue(all(ws[i][1] == ws[i + 1][0] for i in range(len(ws) - 1)))


class ParsingTests(unittest.TestCase):
    def test_empty_money_object_is_null_not_zero(self):
        row = tas.order_line_rows(_order(), "now")[0]
        self.assertIsNone(row["act_paid_commission"])
        self.assertIsNone(row["est_shop_ads_commission"])
        self.assertEqual(row["est_paid_commission"], 2.5)

    def test_commission_rate_is_hundredths_of_a_percent(self):
        row = tas.order_line_rows(_order(skus=[_sku(commission_rate="1550", shop_ads_commission_rate="800")]),
                                  "now")[0]
        self.assertAlmostEqual(row["commission_rate_pct"], 15.5)
        self.assertAlmostEqual(row["shop_ads_commission_rate_pct"], 8.0)

    def test_order_date_is_utc(self):
        # 2025-10-15 00:14 UTC is still 10-14 in Pacific; orders.order_date is UTC too.
        row = tas.order_line_rows(_order(ts=1760487240), "now")[0]
        self.assertEqual(row["order_date"], "2025-10-15")
        self.assertTrue(row["order_create_time"].endswith("+00:00"))

    def test_one_row_per_sku_line(self):
        rows = tas.order_line_rows(_order(skus=[_sku(sku_id="A"), _sku(sku_id="B")]), "now")
        self.assertEqual([(r["line_no"], r["sku_id"]) for r in rows], [(0, "A"), (1, "B")])
        self.assertEqual(json.loads(rows[1]["raw_json"])["sku_id"], "B")

    def test_application_keeps_fraction_and_drops_creator_level_snapshots(self):
        r = tas.application_row(_app(), "list:COMPLETED", "now")
        self.assertAlmostEqual(r["commission_rate_frac"], 0.10)
        self.assertNotIn("avatar_url", r)
        self.assertNotIn("gmv", r)

    def test_unapproved_application_order_id_zero_is_null(self):
        self.assertIsNone(tas.application_row(_app(order_id="0"), "x", "now")["order_id"])


class StorageTests(unittest.TestCase):
    def test_renamed_handle_replaces_the_line_instead_of_duplicating_it(self):
        conn = _conn()
        tas.replace_order_lines(conn, [_order(skus=[_sku(creator_username="old_handle")])], "t1")
        tas.replace_order_lines(conn, [_order(skus=[_sku(creator_username="new_handle")])], "t2")
        rows = conn.execute("SELECT creator_username, price FROM tiktok_affiliate_order_lines").fetchall()
        self.assertEqual(rows, [("new_handle", 20.0)])

    def test_order_that_lost_a_line_loses_the_row(self):
        conn = _conn()
        tas.replace_order_lines(conn, [_order(skus=[_sku(sku_id="A"), _sku(sku_id="B")])], "t1")
        tas.replace_order_lines(conn, [_order(skus=[_sku(sku_id="A")])], "t2")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tiktok_affiliate_order_lines").fetchone()[0], 1)

    def test_named_columns_survive_a_migrated_physical_order(self):
        # Rule 9: build the table with two columns appended by ALTER, i.e. at the
        # PHYSICAL end, then check every value still lands in its own column.
        conn = sqlite3.connect(":memory:")
        cols = [c for c in tas.LINE_COLS if c not in ("price", "creator_username")]
        defs = ", ".join(f"{c} TEXT" for c in cols)
        conn.execute(f"CREATE TABLE tiktok_affiliate_order_lines ({defs}, PRIMARY KEY (order_id, line_no))")
        conn.execute("ALTER TABLE tiktok_affiliate_order_lines ADD COLUMN price REAL")
        conn.execute("ALTER TABLE tiktok_affiliate_order_lines ADD COLUMN creator_username TEXT")
        tas.replace_order_lines(conn, [_order()], "t1")
        got = conn.execute("SELECT price, creator_username, content_id FROM tiktok_affiliate_order_lines").fetchone()
        self.assertEqual(got, (20.0, "alice", "C1"))

    def test_application_upsert_keeps_first_seen_and_list_source(self):
        conn = _conn()
        tas.upsert_applications(conn, [_app(status="SHIPPED")], "list:SHIPPED", "2026-10-01")
        tas.upsert_applications(conn, [_app(status="COMPLETED")], "order_lookup", "2026-10-14")
        r = conn.execute("SELECT status, source, first_seen_at, last_seen_at "
                         "FROM tiktok_affiliate_sample_applications").fetchone()
        self.assertEqual(r, ("COMPLETED", "list:SHIPPED", "2026-10-01", "2026-10-14"))

    def test_handle_history_keeps_every_handle_a_creator_used(self):
        conn = _conn()
        tas.upsert_applications(conn, [_app(username="old")], "list:COMPLETED", "2026-01-01")
        tas.upsert_applications(conn, [_app(aid="A2", order_id="SO2", username="new")], "list:COMPLETED",
                                "2026-06-01")
        tas.upsert_applications(conn, [_app(aid="A3", order_id="SO3", username="new")], "order_lookup",
                                "2026-07-01")
        rows = conn.execute("SELECT username, first_seen_at, last_seen_at FROM tiktok_creator_handle_history "
                            "ORDER BY first_seen_at").fetchall()
        self.assertEqual(rows, [("old", "2026-01-01", "2026-01-01"), ("new", "2026-06-01", "2026-07-01")])


class LookupCoverageTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 14, tzinfo=UTC)

    def _seed(self, conn):
        conn.executemany("INSERT INTO orders VALUES ('tiktok', ?, ?, 'S', 1, 'Nick', ?)",
                         [("SO1", "2026-01-05", "7494"), ("SO2", "2026-02-05", "7495"),
                          ("SO3", "2024-02-05", "7496")])
        conn.execute("INSERT INTO orders VALUES ('tiktok', 'X1', '2026-02-05', 'S', 0, NULL, NULL)")

    def test_only_unresolved_sample_orders_since_the_floor_are_due(self):
        conn = _conn()
        self._seed(conn)
        tas.upsert_applications(conn, [_app(order_id="SO1")], "list:COMPLETED", "now")
        self.assertEqual(tas.orders_to_look_up(conn, "2025-12-01", now=self.NOW), ["SO2"])

    def test_not_found_is_recorded_and_not_reasked_until_retry_window(self):
        conn = _conn()
        self._seed(conn)
        call = ScriptedCall([{"sample_applications": [], "total_count": 0}])
        self.assertEqual(tas.lookup_order(conn, "SO2", call), 0)
        found, attempts = conn.execute("SELECT found, attempts FROM tiktok_affiliate_sample_coverage "
                                       "WHERE kind='order' AND key='SO2'").fetchone()
        self.assertEqual((found, attempts), (0, 1))
        asked = dt.datetime.fromisoformat(
            conn.execute("SELECT asked_at FROM tiktok_affiliate_sample_coverage").fetchone()[0])
        self.assertNotIn("SO2", tas.orders_to_look_up(conn, "2025-12-01", now=asked + dt.timedelta(days=1)))
        self.assertIn("SO2", tas.orders_to_look_up(conn, "2025-12-01", now=asked + dt.timedelta(days=8)))

    def test_gives_up_after_max_attempts(self):
        conn = _conn()
        self._seed(conn)
        for _ in range(tas.MAX_LOOKUP_ATTEMPTS):
            tas.lookup_order(conn, "SO2", ScriptedCall([{"sample_applications": []}]))
        far = dt.datetime(2030, 1, 1, tzinfo=UTC)
        self.assertNotIn("SO2", tas.orders_to_look_up(conn, "2025-12-01", now=far))

    def test_found_lookup_stores_the_application(self):
        conn = _conn()
        self._seed(conn)
        tas.lookup_order(conn, "SO2", ScriptedCall([{"sample_applications": [_app(order_id="SO2")]}]))
        self.assertEqual(conn.execute("SELECT order_id, source FROM tiktok_affiliate_sample_applications")
                         .fetchone(), ("SO2", "order_lookup"))
        self.assertNotIn("SO2", tas.orders_to_look_up(conn, "2025-12-01", now=self.NOW))


class CrosswalkTests(unittest.TestCase):
    def test_sample_order_bridges_open_id_to_orders_creator_id(self):
        conn = _conn()
        conn.executemany("INSERT INTO orders VALUES ('tiktok', ?, ?, ?, 1, 'Alice', ?)",
                         [("SO1", "2026-01-05", "S1", "7494"), ("SO1", "2026-01-05", "S2", "7494"),
                          ("SO2", "2026-03-01", "S1", "7494")])
        tas.upsert_applications(conn, [_app("A1", "SO1"), _app("A2", "SO2", username="alice_new"),
                                       _app("A3", "0", status="PENDING")], "list:COMPLETED", "now")
        r = tas.build_crosswalk(conn)
        self.assertEqual(r["sample_orders"], 2)
        x = conn.execute("SELECT creator_open_id, tiktok_user_id, tiktok_user_id_count, sample_applications, "
                         "sample_orders, first_sample_order_date, last_sample_order_date, handle_count "
                         "FROM tiktok_creator_xwalk").fetchone()
        self.assertEqual(x, ("OPEN1", "7494", 1, 3, 2, "2026-01-05", "2026-03-01", 2))
        # the two ids are DIFFERENT namespaces; the bridge is the order, not equality
        rep = tas.coverage_report(conn, "2025-12-01")
        self.assertEqual(rep["user_id_eq_open_id"], 0)
        self.assertEqual(rep["user_id_present"], 2)

    def test_conflicting_user_ids_are_counted_not_hidden(self):
        conn = _conn()
        conn.executemany("INSERT INTO orders VALUES ('tiktok', ?, '2026-01-05', 'S', 1, 'A', ?)",
                         [("SO1", "111"), ("SO2", "222"), ("SO3", "222")])
        tas.upsert_applications(conn, [_app("A1", "SO1"), _app("A2", "SO2"), _app("A3", "SO3")], "x", "now")
        r = tas.build_crosswalk(conn)
        self.assertEqual(r["user_id_conflicts"], 1)
        self.assertEqual(conn.execute("SELECT tiktok_user_id FROM tiktok_creator_xwalk").fetchone()[0], "222")

    def test_rebuild_is_idempotent(self):
        conn = _conn()
        conn.execute("INSERT INTO orders VALUES ('tiktok', 'SO1', '2026-01-05', 'S', 1, 'A', '7')")
        tas.upsert_applications(conn, [_app()], "x", "now")
        tas.build_crosswalk(conn)
        tas.build_crosswalk(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tiktok_sample_order_creator").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tiktok_creator_xwalk").fetchone()[0], 1)

    def test_report_shares(self):
        conn = _conn()
        conn.executemany("INSERT INTO orders VALUES ('tiktok', ?, ?, 'S', 1, 'A', '7')",
                         [("SO1", "2026-01-05"), ("SO2", "2026-02-05"), ("SO3", "2025-06-01")])
        tas.upsert_applications(conn, [_app("A1", "SO1")], "x", "now")
        tas.replace_order_lines(conn, [_order("O1", skus=[_sku(creator_username="alice")]),
                                       _order("O2", skus=[_sku(creator_username=None,
                                                               content_type="SHOP")])], "now")
        tas.build_crosswalk(conn)
        rep = tas.coverage_report(conn, "2025-12-01")
        self.assertEqual(rep["sample_line_open_id_share_since_2025-12-01"], 0.5)
        self.assertEqual(rep["sample_line_open_id_share_before_2025-12-01"], 0.0)
        self.assertEqual(rep["affiliate_gmv_with_handle_share_base"], 0.5)
        self.assertEqual(rep["affiliate_gmv_handle_resolves_share_base"], 0.5)


class StatusTests(unittest.TestCase):
    def test_empty_pull_is_degraded_not_ok(self):
        self.assertEqual(tas._status_for(0, 0), "degraded")
        self.assertEqual(tas._status_for(10, 1), "degraded")
        self.assertEqual(tas._status_for(10, 0), "ok")

    def test_a_few_unfindable_sample_orders_do_not_turn_the_run_amber(self):
        # ~0.25% of sample orders have no application, and each is re-asked up
        # to 3 times, so 1-3 misses are the steady state, not a fault.
        self.assertFalse(tas.lookup_misses_are_a_problem(2, 2))
        self.assertFalse(tas.lookup_misses_are_a_problem(3, 40))
        self.assertTrue(tas.lookup_misses_are_a_problem(5, 40))
        self.assertFalse(tas.lookup_misses_are_a_problem(30, 400))
        self.assertTrue(tas.lookup_misses_are_a_problem(41, 400))

    def test_orders_command_logs_degraded_when_nothing_written(self):
        conn = _conn()
        args = mock.Mock(start="2026-10-01", end="2026-10-02", days=30, refresh_days=60, force=False)
        with mock.patch.object(tas, "run_orders", return_value={"windows": 1, "orders": 0, "lines": 0}), \
                mock.patch.object(tas.warehouse_db, "log_sync") as log:
            self.assertEqual(tas.cmd_orders(conn, args), 0)
        self.assertEqual(log.call_args[0][3], "degraded")

    def test_rate_limit_pauses_with_exit_75(self):
        conn = _conn()
        args = mock.Mock(start="2026-10-01", end="2026-10-02", days=30, refresh_days=60, force=False)
        with mock.patch.object(tas, "run_orders", side_effect=tas.RateLimited("x")), \
                mock.patch.object(tas.warehouse_db, "log_sync") as log:
            self.assertEqual(tas.cmd_orders(conn, args), 75)
        self.assertEqual(log.call_args[0][3], "degraded")


class EnvTests(unittest.TestCase):
    def test_missing_env_fails_fast_for_api_commands(self):
        with mock.patch.dict("os.environ", {k: "" for k in tas.REQUIRED_ENV}):
            with self.assertRaises(SystemExit) as cm:
                tas.main(["orders", "--days", "1"])
        self.assertIn("TIKTOK_APP_KEY", str(cm.exception))


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@mock.patch.dict("os.environ", {"TIKTOK_APP_KEY": "k", "TIKTOK_APP_SECRET": "s",
                                "TIKTOK_ACCESS_TOKEN": "t", "TIKTOK_SHOP_CIPHER": "c"})
@mock.patch.object(tas.time, "sleep", lambda s: None)
class CallTests(unittest.TestCase):
    OK = _Resp({"code": 0, "data": {"orders": []}})
    LIMITED = _Resp({"code": tas.RATE_LIMIT_CODE, "message": "Too many requests"})

    def test_rate_limit_backs_off_then_succeeds(self):
        with mock.patch.object(tas.requests, "post", side_effect=[self.LIMITED, self.LIMITED, self.OK]) as p:
            self.assertEqual(tas._call(tas.ORDERS_PATH, {}, {}), {"orders": []})
        self.assertEqual(p.call_count, 3)

    def test_persistent_rate_limit_raises_ratelimited(self):
        with mock.patch.object(tas.requests, "post", side_effect=[self.LIMITED] * 20):
            with self.assertRaises(tas.RateLimited):
                tas._call(tas.ORDERS_PATH, {}, {})

    def test_real_api_error_fails_fast(self):
        bad = _Resp({"code": 2000011134, "message": "time_range exceeds the allowed range"})
        with mock.patch.object(tas.requests, "post", side_effect=[bad]) as p:
            with self.assertRaises(tas.ApiError):
                tas._call(tas.ORDERS_PATH, {}, {})
        self.assertEqual(p.call_count, 1)

    def test_token_refreshed_once_then_resigned(self):
        expired = _Resp({"code": next(iter(tas.tts.TOKEN_EXPIRED_CODES))})
        with mock.patch.object(tas.tts, "_refresh_access_token") as refresh, \
                mock.patch.object(tas.requests, "post", side_effect=[expired, self.OK]):
            tas._call(tas.ORDERS_PATH, {}, {})
        self.assertEqual(refresh.call_count, 1)

    def test_request_is_signed_with_the_shared_signer(self):
        with mock.patch.object(tas.tts, "_sign", return_value="SIG") as sign, \
                mock.patch.object(tas.requests, "post", return_value=self.OK) as p:
            tas._call(tas.SAMPLES_PATH, {"page_size": 50}, {"order_id": "1"})
        self.assertEqual(sign.call_args[0][0], tas.SAMPLES_PATH)
        self.assertEqual(sign.call_args[0][2], '{"order_id":"1"}')
        self.assertEqual(p.call_args.kwargs["params"]["sign"], "SIG")
        self.assertEqual(p.call_args.kwargs["params"]["page_size"], "50")


if __name__ == "__main__":
    unittest.main()
