"""Hermetic tests for flexport_orders_sync.py (per-order shipping cost).

No network — the HTTP layer is mocked. Focus is on the two load-bearing
behaviors: the Link-header cursor pagination (not offset) and the resumable
crawl/checkpoint logic.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import flexport_orders_sync as fo


def _resp(body, link: str | None = None, status: int = 200) -> Mock:
    m = Mock()
    m.status_code = status
    m.json.return_value = body
    m.headers = {"Link": link} if link else {}
    m.text = ""
    return m


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_tables_and_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        fo.ensure_schema(conn)
        fo.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("flexport_order_costs", tables)
        self.assertIn("flexport_order_packages", tables)
        self.assertIn("flexport_order_sync_state", tables)


class CursorExtractionTests(unittest.TestCase):
    def test_extracts_page_info_from_link_header(self) -> None:
        resp = _resp([], link='<https://api.example.com/events?page_info=abc123>; rel="next"')
        self.assertEqual(fo.next_page_info(resp), "abc123")

    def test_returns_none_when_no_next_link(self) -> None:
        resp = _resp([], link='<https://api.example.com/events?page_info=abc>; rel="prev"')
        self.assertIsNone(fo.next_page_info(resp))

    def test_returns_none_when_no_link_header_at_all(self) -> None:
        resp = _resp([])
        self.assertIsNone(fo.next_page_info(resp))


class UlidCraftingTests(unittest.TestCase):
    def test_ulid_is_26_chars_and_deterministic_for_same_input(self) -> None:
        dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        u1 = fo.ulid_at(dt)
        u2 = fo.ulid_at(dt)
        self.assertEqual(len(u1), 26)
        self.assertEqual(u1, u2)

    def test_later_timestamp_sorts_after_earlier_one(self) -> None:
        # The whole point of a ULID cursor is that lexical order == time order.
        early = fo.ulid_at(datetime(2024, 1, 1, tzinfo=timezone.utc))
        later = fo.ulid_at(datetime(2024, 6, 1, tzinfo=timezone.utc))
        self.assertLess(early, later)

    def test_page_info_at_produces_a_decodable_cursor(self) -> None:
        import base64
        import json
        dt = datetime(2024, 3, 1, tzinfo=timezone.utc)
        token = fo.page_info_at(dt)
        pad = token + "=" * (-len(token) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(pad))
        self.assertEqual(decoded["type"], fo.SHIPMENT_EVENT)
        self.assertEqual(decoded["direction"], "forward")
        self.assertEqual(len(decoded["cursor"]), 26)


class IterEventPagesTests(unittest.TestCase):
    def test_dedupes_order_ids_within_a_page_and_follows_cursor(self) -> None:
        page1 = [
            {"payload": {"orderId": 1}}, {"payload": {"orderId": 1}},  # dup within page
            {"payload": {"orderId": 2}},
        ]
        page2 = [{"payload": {"orderId": 3}}]
        responses = [
            _resp(page1, link='<x?page_info=CUR2>; rel="next"'),
            _resp(page2),  # no next link -> feed tip
        ]

        with patch.object(fo, "_request", side_effect=responses), patch.object(fo.time, "sleep"):
            pages = list(fo.iter_event_pages(None, max_pages=10))

        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0], ([1, 2], "CUR2", None))
        self.assertEqual(pages[1], ([3], None, None))

    def test_stops_on_empty_page(self) -> None:
        with patch.object(fo, "_request", return_value=_resp([])), patch.object(fo.time, "sleep"):
            pages = list(fo.iter_event_pages(None, max_pages=10))
        self.assertEqual(pages, [])

    def test_respects_max_pages_runaway_guard(self) -> None:
        infinite_page = _resp([{"payload": {"orderId": 1}}],
                              link='<x?page_info=SAME>; rel="next"')
        with patch.object(fo, "_request", return_value=infinite_page), patch.object(fo.time, "sleep"):
            pages = list(fo.iter_event_pages(None, max_pages=3))
        self.assertEqual(len(pages), 3)

    def test_tracks_last_event_time_across_pages(self) -> None:
        page1 = [{"payload": {"orderId": 1}, "time": "2024-06-01T00:00:00Z"}]
        page2 = [{"payload": {"orderId": 2}, "time": "2024-06-02T00:00:00Z"}]
        responses = [
            _resp(page1, link='<x?page_info=CUR2>; rel="next"'),
            _resp(page2),
        ]
        with patch.object(fo, "_request", side_effect=responses), patch.object(fo.time, "sleep"):
            pages = list(fo.iter_event_pages(None, max_pages=10))
        self.assertEqual(pages[0][2], "2024-06-01T00:00:00")
        self.assertEqual(pages[1][2], "2024-06-02T00:00:00")


class SelfHealingCursorTests(unittest.TestCase):
    """A page_info cursor can go permanently bad on the vendor's side even
    while the feed is healthy. iter_event_pages must recover by re-crafting a
    fresh cursor from the last known event timestamp, but only when it has
    one — a cold start with no prior position must still raise."""

    def test_recrafts_from_last_time_after_bad_cursor(self) -> None:
        good_page = [{"payload": {"orderId": 9}, "time": "2024-06-05T12:00:00Z"}]
        responses = [fo.FlexportBadCursor("500"), _resp(good_page)]

        def fake_request(path, params):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(fo, "_request", side_effect=fake_request), \
             patch.object(fo.time, "sleep"):
            pages = list(fo.iter_event_pages(
                "DEAD-CURSOR", max_pages=10, last_time="2024-06-05T11:00:00"))

        self.assertEqual(pages[0][0], [9])  # recovered and yielded the order past the dead cursor

    def test_gives_up_after_max_recrafts(self) -> None:
        with patch.object(fo, "_request", side_effect=fo.FlexportBadCursor("500")), \
             patch.object(fo.time, "sleep"):
            with self.assertRaises(fo.FlexportBadCursor):
                list(fo.iter_event_pages(
                    "DEAD-CURSOR", max_pages=10, last_time="2024-06-05T11:00:00"))

    def test_cold_start_with_no_last_time_still_raises(self) -> None:
        # No prior position in either dimension -> recrafting would restart
        # the walk at an arbitrary point, so a bad cursor must propagate.
        with patch.object(fo, "_request", side_effect=fo.FlexportBadCursor("500")), \
             patch.object(fo.time, "sleep"):
            with self.assertRaises(fo.FlexportBadCursor):
                list(fo.iter_event_pages("DEAD-CURSOR", max_pages=10, last_time=None))


class RecraftNudgeTests(unittest.TestCase):
    """A rejected cursor must not be re-crafted at the identical timestamp —
    whatever made that position bad is often sticky, so retrying the same
    spot can fail every attempt. Each recraft should step forward."""

    def test_recraft_steps_forward_past_the_bad_position(self) -> None:
        seen_dts: list[datetime] = []
        real_page_info_at = fo.page_info_at

        def spy_page_info_at(dt):
            seen_dts.append(dt)
            return real_page_info_at(dt)

        good_page = [{"payload": {"orderId": 9}, "time": "2024-06-05T12:00:00Z"}]
        responses = [fo.FlexportBadCursor("500"), fo.FlexportBadCursor("500"), _resp(good_page)]

        def fake_request(path, params):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(fo, "_request", side_effect=fake_request), \
             patch.object(fo, "page_info_at", side_effect=spy_page_info_at), \
             patch.object(fo.time, "sleep"):
            list(fo.iter_event_pages(
                "DEAD-CURSOR", max_pages=10, last_time="2024-06-05T11:00:00"))

        # Two recrafts, each nudged further forward than the last, and both
        # strictly past the original last_time.
        self.assertEqual(len(seen_dts), 2)
        base = datetime(2024, 6, 5, 11, 0, 0, tzinfo=timezone.utc)
        self.assertGreater(seen_dts[0], base)
        self.assertGreater(seen_dts[1], seen_dts[0])


class FrontierSeedTests(unittest.TestCase):
    """Resuming with no validated cursor should re-seed from wherever the
    stored data actually ends, not a fixed lookback window."""

    def test_returns_none_on_empty_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        fo.ensure_schema(conn)
        self.assertIsNone(fo.frontier_seed(conn))
        self.assertIsNone(fo.frontier_time(conn))

    def test_seeds_before_the_newest_stored_order(self) -> None:
        conn = sqlite3.connect(":memory:")
        fo.ensure_schema(conn)
        with conn:
            conn.execute(
                "INSERT INTO flexport_order_costs "
                "(order_id, external_order_id, cost, currency, internal_status, fulfillment_status, "
                " created_at, shipped_at, delivered_at, units, n_shipments, n_packages, "
                " total_weight_oz, carriers, shipping_methods, is_international, synced_at) "
                "VALUES (1,'E1',5.0,'USD',NULL,NULL,'2024-06-05T12:00:00',NULL,NULL,0,0,0,0,"
                "NULL,NULL,0,'x')")

        cursor = fo.frontier_seed(conn, overlap_hours=2)
        self.assertIsNotNone(cursor)
        import base64
        import json
        pad = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(pad))
        # A ULID's leading characters are monotonic with time; a 2h-earlier seed
        # must sort strictly before a ULID crafted at the stored created_at.
        at_created = fo.ulid_at(datetime(2024, 6, 5, 12, 0, 0, tzinfo=timezone.utc))
        self.assertLess(decoded["cursor"], at_created)

        bookmark = fo.frontier_time(conn, overlap_hours=2)
        self.assertEqual(bookmark, "2024-06-05T10:00:00")


class CheckpointPagesTests(unittest.TestCase):
    """A quiet stretch with no new orders must still checkpoint periodically,
    or a kill mid-walk discards all cursor progress."""

    def test_checkpoints_on_page_count_even_with_zero_new_orders(self) -> None:
        conn = sqlite3.connect(":memory:")
        fo.ensure_schema(conn)

        # CHECKPOINT_PAGES pages, each with an order id already "seen" so
        # nothing new accumulates in order_rows — only the page-count trigger
        # can cause a checkpoint here.
        pages = [([], f"CUR-{i}", None) for i in range(1, fo.CHECKPOINT_PAGES + 1)]

        with patch.object(fo, "iter_event_pages", return_value=iter(pages)), \
             patch.object(fo, "fetch_order"):
            fo.run(conn, restart=False, since_days=None, max_pages=100)

        cursor = conn.execute(
            "SELECT value FROM flexport_order_sync_state WHERE key='events_page_info'"
        ).fetchone()
        self.assertEqual(cursor[0], f"CUR-{fo.CHECKPOINT_PAGES}")


class RequestRetryTests(unittest.TestCase):
    def test_401_is_hard_fatal_no_retry(self) -> None:
        with patch.object(fo.requests, "get", return_value=_resp({}, status=401)), \
             patch.dict(fo.os.environ, {"FLEXPORT_API_TOKEN": "tok"}):
            with self.assertRaises(RuntimeError) as cm:
                fo._request("/orders/1", {})
        self.assertIn("401", str(cm.exception))

    def test_500_is_retried_then_succeeds(self) -> None:
        calls = [_resp({}, status=500), _resp({"ok": True})]

        def fake_get(url, params, headers, timeout):
            return calls.pop(0)

        with patch.object(fo.requests, "get", side_effect=fake_get), \
             patch.object(fo.time, "sleep"), \
             patch.dict(fo.os.environ, {"FLEXPORT_API_TOKEN": "tok"}):
            resp = fo._request("/orders/1", {})
        self.assertEqual(resp.json(), {"ok": True})

    def test_exhausted_5xx_raises_transient_not_generic_error(self) -> None:
        with patch.object(fo.requests, "get", return_value=_resp({}, status=503)), \
             patch.object(fo.time, "sleep"), \
             patch.dict(fo.os.environ, {"FLEXPORT_API_TOKEN": "tok"}):
            with self.assertRaises(fo.FlexportTransient):
                fo._request("/events", {})


class RowBuildingTests(unittest.TestCase):
    def test_rows_for_order_flags_international_and_sums_weight(self) -> None:
        order = {
            "id": 42, "externalOrderId": "EXT-1", "cost": 12.5, "currency": "USD",
            "state": {"internalStatus": "SHIPPED", "fulfillmentStatus": "COMPLETE"},
            "createdAt": "2024-01-01", "shippedAt": "2024-01-02", "deliveredAt": "2024-01-05",
            "lineItems": [{"quantity": 2}, {"quantity": 1}],
            "shipments": [{
                "id": 100, "warehouseId": "WH1",
                "packages": [{
                    "id": 1,
                    "label": {"carrier": "PASSPORT", "shippingMethod": "STANDARD",
                             "trackingCode": "TRK1",
                             "packageDimensions": {"weight": 1, "weightUnit": "lb",
                                                   "length": 10, "width": 5, "height": 3}},
                    "lineItems": [{"logisticsSku": "D1"}],
                }],
            }],
        }
        order_row, pkg_rows = fo.rows_for_order(order, "stamp")
        self.assertEqual(order_row[0], 42)
        self.assertEqual(order_row[2], 12.5)   # cost
        self.assertEqual(order_row[9], 3)      # units
        self.assertEqual(order_row[15], 1)     # is_international (PASSPORT carrier)
        self.assertEqual(len(pkg_rows), 1)
        self.assertEqual(pkg_rows[0][7], 16.0)  # weight_oz normalized from 1 lb

    def test_null_cost_stays_null_not_zero(self) -> None:
        order_row, _ = fo.rows_for_order({"id": 1}, "stamp")
        self.assertIsNone(order_row[2])


class ResumableCrawlTests(unittest.TestCase):
    """The crawl must persist its cursor so a killed run can resume, and must
    never re-fetch an order id already stored."""

    def test_run_skips_already_stored_orders_and_saves_cursor(self) -> None:
        conn = sqlite3.connect(":memory:")
        fo.ensure_schema(conn)
        with conn:
            conn.execute(
                "INSERT INTO flexport_order_costs "
                "(order_id, external_order_id, cost, currency, internal_status, fulfillment_status, "
                " created_at, shipped_at, delivered_at, units, n_shipments, n_packages, "
                " total_weight_oz, carriers, shipping_methods, is_international, synced_at) "
                "VALUES (1,'E1',5.0,'USD',NULL,NULL,NULL,NULL,NULL,0,0,0,0,NULL,NULL,0,'x')")

        pages = [([1, 2], None, None)]  # order 1 already stored; order 2 is new

        def fake_fetch(oid):
            return oid, {"id": oid, "cost": 9.99, "shipments": []}

        with patch.object(fo, "iter_event_pages", return_value=iter(pages)), \
             patch.object(fo, "fetch_order", side_effect=fake_fetch):
            n_orders, pages_walked, paused = fo.run(
                conn, restart=False, since_days=None, max_pages=10)

        self.assertEqual(n_orders, 1)   # only the new order was fetched/written
        self.assertFalse(paused)
        stored = {r[0] for r in conn.execute("SELECT order_id FROM flexport_order_costs")}
        self.assertEqual(stored, {1, 2})

    def test_transient_failure_pauses_and_preserves_progress(self) -> None:
        conn = sqlite3.connect(":memory:")
        fo.ensure_schema(conn)

        def raising_pages(start, max_pages, last_time=None):
            yield [1], "CURSOR-A", None
            raise fo.FlexportTransient("backend degraded")

        def fake_fetch(oid):
            return oid, {"id": oid, "cost": 1.0, "shipments": []}

        with patch.object(fo, "iter_event_pages",
                          side_effect=lambda s, m, t=None: raising_pages(s, m, t)), \
             patch.object(fo, "fetch_order", side_effect=fake_fetch):
            n_orders, pages_walked, paused = fo.run(
                conn, restart=False, since_days=None, max_pages=10)

        self.assertTrue(paused)
        self.assertEqual(n_orders, 1)  # the order found before the failure is kept
        cursor = conn.execute(
            "SELECT value FROM flexport_order_sync_state WHERE key='events_page_info'").fetchone()
        self.assertEqual(cursor[0], "CURSOR-A")  # cursor checkpointed before the pause

    def test_restart_clears_stored_cursor(self) -> None:
        conn = sqlite3.connect(":memory:")
        fo.ensure_schema(conn)
        fo._save_cursor(conn, "OLD-CURSOR")

        with patch.object(fo, "iter_event_pages", return_value=iter([])):
            fo.run(conn, restart=True, since_days=None, max_pages=10)

        self.assertIsNone(fo._load_cursor(conn))


if __name__ == "__main__":
    unittest.main()
