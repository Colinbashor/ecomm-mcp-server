"""flexport_returns_sync: newest-first walk, refresh, and the no-progress signal.

Hermetic: an in-memory SQLite DB and a fake /returns feed shaped like the live
one (ids strictly DESCENDING, page_info = base64url {"cursor": <last id on the
page>, "direction": "forward"} meaning "ids below this", no next link on the
final page). No network, no warehouse.db.
"""
from __future__ import annotations

import base64
import copy
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import flexport_returns_sync as fx

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
# REFRESH_DAYS before T0 is 2026-04-17: activity in January is "old", in July
# is inside the window.
OLD = "2026-01-10T10:00:00.000Z"
RECENT = "2026-07-20T10:00:00.000Z"


def _rec(rid: int, status: str = "CREATED", *, inspected: str | None = None,
         disposition: str | None = None, n_items: int = 1) -> dict:
    inspected_items = ([{"receivedCondition": "NEW", "finalCondition": "NEW",
                         "disposition": disposition}] if disposition else [])
    return {
        "id": rid, "status": status, "rma": f"RMA{rid}", "externalReturnId": None,
        "fulfillmentOrderId": None, "shippingLabel": {}, "sourceAddress": {},
        "shippedAt": None, "receivedAt": None, "inspectedAt": inspected,
        "returnItems": [{"identifier": f"S{rid}-{i}", "expectedQuantity": 1,
                         "inspectedItems": inspected_items} for i in range(n_items)],
    }


def _decode(cursor: str) -> int:
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["cursor"]


class FakeFeed:
    """GET /returns as measured live: newest-first, cursor = 'ids below N',
    no next link on the last page."""

    def __init__(self, records: list[dict], page_size: int = 3) -> None:
        self.records = {r["id"]: r for r in records}
        self.page_size = page_size
        self.calls: list[str | None] = []

    def put(self, *records: dict) -> None:
        for r in records:
            self.records[r["id"]] = r

    def __call__(self, cursor: str | None) -> tuple[list, str | None]:
        self.calls.append(cursor)
        below = _decode(cursor) if cursor else None
        rows = sorted((r for rid, r in self.records.items() if below is None or rid < below),
                      key=lambda r: r["id"], reverse=True)
        page = rows[: self.page_size]
        nxt = fx._cursor_below(page[-1]["id"]) if len(rows) > self.page_size else None
        return copy.deepcopy(page), nxt

    def ids_requested_below(self) -> list[int | None]:
        return [_decode(c) if c else None for c in self.calls]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    fx.ensure_schema(conn)
    return conn


def _run(conn, feed, now, **kw) -> fx.RunResult:
    with mock.patch("builtins.print"):
        return fx.run(conn, fetch=feed, now=now, **kw)


def _seed(conn) -> FakeFeed:
    """Ten returns, four of them old, already walked in full once."""
    feed = FakeFeed([_rec(i, "PROCESSED", inspected=OLD, disposition="RESTOCK") for i in range(1, 5)]
                    + [_rec(i, "CREATED") for i in range(5, 8)]
                    + [_rec(i, "PROCESSED", inspected=RECENT, disposition="RESTOCK")
                       for i in range(8, 11)])
    r = _run(conn, feed, T0 - timedelta(days=1))
    assert r.walk.mode == "full" and r.walk.complete and r.new == 10, r
    return feed


class CursorShapeTests(unittest.TestCase):
    def test_crafted_cursor_is_byte_identical_to_flexport_issued(self) -> None:
        # Same shape as a Flexport-issued page_info: compact JSON, unpadded base64url.
        self.assertEqual(fx._cursor_below(123456),
                         "eyJjdXJzb3IiOjEyMzQ1NiwiZGlyZWN0aW9uIjoiZm9yd2FyZCJ9")


class NewestFirstWalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_new_returns_at_the_head_are_ingested(self) -> None:
        # THE REGRESSION: new returns are prepended at the head. The old
        # resume-to-tip cursor sat on the oldest page and never saw them.
        feed = _seed(self.conn)
        feed.put(*(_rec(i) for i in range(11, 16)))
        feed.calls.clear()

        r = _run(self.conn, feed, T0)

        self.assertEqual(r.walk.mode, "window")
        self.assertIsNone(feed.calls[0], "every walk must start at the head")
        self.assertEqual(r.new, 5)
        self.assertEqual(r.status, "ok")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM flexport_returns WHERE id > 10").fetchone()[0], 5)

    def test_window_walk_stops_at_its_floor(self) -> None:
        feed = _seed(self.conn)
        feed.put(_rec(11))
        feed.calls.clear()

        r = _run(self.conn, feed, T0)

        # floor = highest id with activity before the cutoff = 4 (bootstrap).
        self.assertEqual(r.walk.stop_id, 4)
        self.assertTrue(r.walk.complete)
        # pages 11-9, 8-6, 5-3: stops on the page that reaches 4, never asks below 3.
        self.assertEqual(feed.ids_requested_below(), [None, 9, 6])
        self.assertEqual(r.seen, 9)
        untouched = self.conn.execute(
            "SELECT synced_at FROM flexport_returns WHERE id IN (1, 2) ORDER BY id").fetchall()
        self.assertTrue(all(s == (T0 - timedelta(days=1)).isoformat(timespec="seconds")
                            for (s,) in untouched))

    def test_non_terminal_return_is_refreshed_with_its_lines(self) -> None:
        feed = _seed(self.conn)
        feed.put(_rec(6, "PROCESSED", inspected="2026-10-13T09:00:00.000Z",
                      disposition="RESTOCK", n_items=1))
        feed.put(_rec(7, "CREATED", n_items=1))   # was 1 line, still 1: unchanged

        r = _run(self.conn, feed, T0)

        self.assertEqual((r.new, r.changed), (0, 1))
        self.assertEqual(r.status, "ok")
        row = self.conn.execute(
            "SELECT status, inspected_at FROM flexport_returns WHERE id = 6").fetchone()
        self.assertEqual(row, ("PROCESSED", "2026-10-13T09:00:00.000Z"))
        self.assertEqual(self.conn.execute(
            "SELECT disposition FROM flexport_return_lines WHERE return_id = 6").fetchall(),
            [("RESTOCK",)])

    def test_lines_are_replaced_wholesale(self) -> None:
        feed = _seed(self.conn)
        feed.put(_rec(9, "PROCESSED", inspected=RECENT, disposition="RESTOCK", n_items=3))
        _run(self.conn, feed, T0)
        feed.put(_rec(9, "PROCESSED", inspected=RECENT, disposition="RESTOCK", n_items=1))

        r = _run(self.conn, feed, T0 + timedelta(hours=1))

        self.assertEqual(r.changed, 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM flexport_return_lines WHERE return_id = 9").fetchone()[0], 1)

    def test_run_that_finds_nothing_new_or_changed_is_degraded(self) -> None:
        # A walk that changes nothing must not log `ok` (that is how the old bug hid).
        feed = _seed(self.conn)

        r = _run(self.conn, feed, T0)

        self.assertTrue(r.walk.complete)
        self.assertEqual(r.rows_written, 0)
        self.assertEqual((r.status, r.exit_code), ("degraded", fx.GRACEFUL_PAUSE_EXIT))
        self.assertIn("NO PROGRESS", r.message)

    def test_rows_seen_are_not_rows_written(self) -> None:
        feed = _seed(self.conn)
        feed.put(_rec(11))

        r = _run(self.conn, feed, T0)

        self.assertGreater(r.seen, 1)
        self.assertEqual(r.rows_written, 1)


class EndOfFeedTests(unittest.TestCase):
    def test_short_page_without_next_link_is_not_the_end(self) -> None:
        # A page that comes back short and link-less must be confirmed
        # by an EMPTY page before the walk believes the feed is over.
        feed = FakeFeed([_rec(i) for i in range(1, 8)], page_size=3)
        real = feed.__call__

        def short_first_page(cursor):
            page, nxt = real(cursor)
            return (page[:2], None) if cursor is None else (page, nxt)

        walk = fx.Walk("full", None)
        seen = [r["id"] for batch in fx.walk_pages(walk, 100, short_first_page) for r in batch]

        self.assertEqual(seen, [7, 6, 5, 4, 3, 2, 1])
        self.assertTrue(walk.complete)

    def test_end_is_an_empty_page(self) -> None:
        feed = FakeFeed([_rec(i) for i in range(1, 7)], page_size=3)
        walk = fx.Walk("full", None)
        list(fx.walk_pages(walk, 100, feed))
        self.assertTrue(walk.complete)
        self.assertEqual(feed.ids_requested_below(), [None, 4, 1])

    def test_page_cap_leaves_walk_incomplete_and_degraded(self) -> None:
        conn = _db()
        feed = FakeFeed([_rec(i) for i in range(1, 11)], page_size=3)
        r = _run(conn, feed, T0, max_pages=2)
        self.assertFalse(r.walk.complete)
        self.assertEqual((r.status, r.exit_code), ("degraded", fx.GRACEFUL_PAUSE_EXIT))
        self.assertIn("INCOMPLETE", r.message)
        self.assertIsNone(fx._high_water_mark(conn))
        conn.close()

    def test_feed_that_stops_being_newest_first_fails_loudly(self) -> None:
        def ignores_cursor(cursor):
            return [_rec(3), _rec(2)], fx._cursor_below(2)

        with self.assertRaises(fx.FeedOrderError):
            list(fx.walk_pages(fx.Walk("full", None), 100, ignores_cursor))

    def test_non_list_body_is_an_error_not_end_of_feed(self) -> None:
        resp = mock.Mock()
        resp.json.return_value = {"message": "Endpoint request timed out"}
        resp.headers = {}
        with mock.patch.object(fx, "_request", return_value=resp):
            with self.assertRaises(RuntimeError):
                fx._fetch_page(None)


class InterruptionTests(unittest.TestCase):
    def test_transient_outage_keeps_pages_and_holds_the_high_water_mark(self) -> None:
        conn = _db()
        feed = _seed(conn)
        feed.put(*(_rec(i) for i in range(11, 17)))
        calls = {"n": 0}

        def flaky(cursor):
            calls["n"] += 1
            if calls["n"] == 2:
                raise fx.FlexportTransient("504 x16")
            return feed(cursor)

        r = _run(conn, flaky, T0)

        self.assertEqual((r.status, r.exit_code), ("degraded", fx.GRACEFUL_PAUSE_EXIT))
        self.assertEqual(r.new, 3)   # page 16-14 committed before the outage
        self.assertEqual(fx._high_water_mark(conn), 10)
        self.assertEqual(conn.execute(
            "SELECT complete, status FROM flexport_returns_walks ORDER BY run_id DESC LIMIT 1"
        ).fetchone(), (0, "degraded"))

        r = _run(conn, feed, T0 + timedelta(hours=1))   # next run fills the gap

        self.assertEqual(r.new, 3)   # 13-11
        self.assertEqual(fx._high_water_mark(conn), 16)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM flexport_returns").fetchone()[0], 16)
        conn.close()

    def test_error_is_recorded_on_the_walk_and_raised(self) -> None:
        conn = _db()

        def broken(cursor):
            raise RuntimeError("Flexport 401")

        with self.assertRaises(RuntimeError):
            _run(conn, broken, T0)
        self.assertEqual(conn.execute(
            "SELECT status, complete FROM flexport_returns_walks").fetchone(), ("error", 0))
        conn.close()


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _db()

    def tearDown(self) -> None:
        self.conn.close()

    def _walk(self, started: datetime, mode: str, head: int, complete: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO flexport_returns_walks (started_at, mode, head_id, complete, status) "
            "VALUES (?, ?, ?, ?, 'ok')",
            (started.isoformat(timespec="seconds"), mode, head, complete))

    def test_first_run_is_a_full_walk(self) -> None:
        self.assertEqual(fx.plan_walk(self.conn, T0).mode, "full")

    def test_full_walk_recurs_weekly(self) -> None:
        self._walk(T0, "full", 100)
        self.conn.execute("INSERT INTO flexport_returns (id, inspected_at) VALUES (50, ?)", (OLD,))
        self.assertEqual(fx.plan_walk(self.conn, T0 + timedelta(days=3)).mode, "window")
        self.assertEqual(fx.plan_walk(self.conn, T0 + timedelta(days=8)).mode, "full")

    def test_incomplete_full_walk_does_not_count(self) -> None:
        self._walk(T0, "full", 100, complete=0)
        self.assertEqual(fx.plan_walk(self.conn, T0 + timedelta(days=1)).mode, "full")

    def test_force_full(self) -> None:
        self._walk(T0, "full", 100)
        self.assertEqual(fx.plan_walk(self.conn, T0, force_full=True).mode, "full")

    def test_window_floor_uses_walk_log_once_it_is_old_enough(self) -> None:
        self._walk(T0 - timedelta(days=200), "window", 1000)
        self._walk(T0 - timedelta(days=185), "window", 1500)
        self._walk(T0 - timedelta(days=170), "window", 2000)
        self.assertEqual(fx._window_floor_id(self.conn, T0), 1500)

    def test_bootstrap_floor_ignores_placeholder_dates(self) -> None:
        self.conn.executemany(
            "INSERT INTO flexport_returns (id, shipped_at, inspected_at) VALUES (?, ?, ?)",
            [(10, None, OLD), (20, None, RECENT),
             (30, "0000-00-00 00:00:00", None)])   # placeholder on a newer id
        self.assertEqual(fx._window_floor_id(self.conn, T0), 10)

    def test_no_floor_basis_means_full_walk(self) -> None:
        self._walk(T0 - timedelta(days=1), "full", 100)
        self.assertEqual(fx.plan_walk(self.conn, T0).mode, "full")

    def test_window_never_stops_above_the_high_water_mark(self) -> None:
        # Whenever the floor lands above the mark (no complete walk for longer
        # than the window), stopping at it would skip returns nobody has seen.
        self._walk(T0 - timedelta(days=6), "full", 100)
        self.conn.execute("INSERT INTO flexport_returns (id, inspected_at) VALUES (150, ?)", (OLD,))
        self.assertEqual(fx.plan_walk(self.conn, T0).stop_id, 100)


class FullWalkTests(unittest.TestCase):
    def test_vanished_returns_are_counted_not_deleted(self) -> None:
        conn = _db()
        feed = _seed(conn)
        del feed.records[5]
        feed.put(_rec(11))
        r = _run(conn, feed, T0, force_full=True)
        self.assertEqual(r.vanished, 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM flexport_returns WHERE id = 5").fetchone()[0], 1)
        conn.close()

    def test_upsert_names_its_columns(self) -> None:
        # Named columns survive a table whose physical column order differs from the DDL.
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE flexport_returns (synced_at TEXT, id INTEGER PRIMARY KEY)")
        for col in fx.RETURN_COLUMNS:
            if col not in ("id", "synced_at"):
                conn.execute(f"ALTER TABLE flexport_returns ADD COLUMN {col}")
        conn.executescript(fx.DDL)
        fx._upsert_page(conn, [_rec(1, "SHIPPED")], "stamp")
        self.assertEqual(conn.execute(
            "SELECT status, synced_at, rma FROM flexport_returns").fetchone(),
            ("SHIPPED", "stamp", "RMA1"))
        conn.close()


class CliAndWiringTests(unittest.TestCase):
    def test_help_exits_without_syncing(self) -> None:
        with mock.patch.object(fx, "_request", side_effect=AssertionError("live call")), \
                mock.patch("sys.stdout"):
            with self.assertRaises(SystemExit) as ctx:
                fx.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


class LegacyHelperTests(unittest.TestCase):
    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        fx.ensure_schema(conn)
        fx.ensure_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"flexport_returns", "flexport_return_lines",
                         "flexport_returns_walks"} <= tables)

    def test_extracts_next_page_info(self) -> None:
        resp = mock.Mock(headers={"Link": '<https://api.example.com/returns?page_info=42>; rel="next"'})
        self.assertEqual(fx.next_page_info(resp), "42")
        resp = mock.Mock(headers={"Link": '<https://api.example.com/returns?page_info=42>; rel="prev"'})
        self.assertIsNone(fx.next_page_info(resp))

    def test_rows_for_return_keeps_first_inspection_disposition(self) -> None:
        rec = _rec(7, "PROCESSED", disposition="RESTOCK", n_items=2)
        rec["returnItems"][0]["inspectedItems"].append(
            {"receivedCondition": "DAMAGED", "finalCondition": "SCRAP", "disposition": "SCRAP"})
        ret_row, line_rows = fx.rows_for_return(rec, "stamp")
        self.assertEqual(ret_row[fx.RETURN_COLUMNS.index("n_lines")], 2)
        self.assertEqual(line_rows[0][fx.LINE_COLUMNS.index("n_inspected")], 2)
        self.assertEqual(line_rows[0][fx.LINE_COLUMNS.index("disposition")], "RESTOCK")

    def test_old_cursor_is_retired(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE flexport_returns_sync_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO flexport_returns_sync_state VALUES ('returns_page_info', 'x', 'y')")
        fx._retire_old_cursor(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM flexport_returns_sync_state").fetchone()[0], 0)
        fx._retire_old_cursor(sqlite3.connect(":memory:"))   # absent table is fine


if __name__ == "__main__":
    unittest.main()
