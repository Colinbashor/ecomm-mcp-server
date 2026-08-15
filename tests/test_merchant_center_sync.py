"""Tests for the Google Merchant Center connector (merchant_center_sync.py).

Hermetic: no network, no real service-account file. Two layers are tested
separately:
  * `Client.search()` — HTTP transport (retry/backoff/pagination), with
    `requests.post` monkeypatched, mirroring tests/test_shopify_connector.py.
  * `sync_*()` — row-shaping/parsing logic, given a fake object exposing only
    a `.search(query)` generator, so no HTTP layer is involved at all.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from unittest.mock import patch

import requests

import merchant_center_sync as gmc


# --------------------------------------------------------------------------- #
#  pure field coercion
# --------------------------------------------------------------------------- #
class FieldCoercionTests(unittest.TestCase):
    def test_as_int_parses_stringly_typed_counts(self) -> None:
        self.assertEqual(gmc.as_int("42"), 42)
        self.assertIsNone(gmc.as_int(""))
        self.assertIsNone(gmc.as_int(None))
        self.assertIsNone(gmc.as_int("not-a-number"))

    def test_as_float_guards_against_nan(self) -> None:
        self.assertEqual(gmc.as_float("3.5"), 3.5)
        nan = float("nan")
        self.assertIsNone(gmc.as_float(nan))
        self.assertIsNone(gmc.as_float(""))

    def test_as_date_from_year_month_day_dict(self) -> None:
        self.assertEqual(gmc.as_date({"year": 2026, "month": 7, "day": 1}), "2026-07-01")
        self.assertIsNone(gmc.as_date({}))
        self.assertIsNone(gmc.as_date(None))

    def test_as_money_keeps_micros_and_currency_separate(self) -> None:
        self.assertEqual(
            gmc.as_money({"amountMicros": "272000000", "currencyCode": "USD"}),
            (272000000, "USD"))
        self.assertEqual(gmc.as_money(None), (None, None))

    def test_parse_offer_id_extracts_shopify_ids(self) -> None:
        pid, vid = gmc.parse_offer_id("shopify_US_1234567890123_9876543210987")
        self.assertEqual((pid, vid), ("1234567890123", "9876543210987"))

    def test_parse_offer_id_returns_none_for_unrecognized_shape(self) -> None:
        self.assertEqual(gmc.parse_offer_id("some-other-sku-123"), (None, None))
        self.assertEqual(gmc.parse_offer_id(None), (None, None))


# --------------------------------------------------------------------------- #
#  Client.search() transport: retry / backoff / pagination / permanent errors
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status: int = 200, payload: object | None = None,
                 text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self) -> object:
        return self._payload


def _page(rows: list[dict], next_token: str | None = None) -> _FakeResp:
    return _FakeResp(200, {
        "results": [{"someView": r} for r in rows],
        **({"nextPageToken": next_token} if next_token else {}),
    })


class _TimeShim:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _make_client() -> gmc.Client:
    """A Client instance with no real service-account file: __init__ is
    bypassed and just enough state is set for search() to run."""
    c = gmc.Client.__new__(gmc.Client)
    c.merchant_id = "12345"
    c._reports_url = "https://merchantapi.googleapis.com/reports/v1/accounts/12345/reports:search"
    c._headers = {"Authorization": "Bearer tok1"}
    c._refresh = lambda: setattr(c, "_headers", {"Authorization": "Bearer tok2"})
    return c


class _TransportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _make_client()
        self._post = gmc.requests.post
        self._time = gmc.time
        self.clock = _TimeShim()
        gmc.time = self.clock
        self.calls: list = []

    def tearDown(self) -> None:
        gmc.requests.post = self._post
        gmc.time = self._time

    def _queue(self, *outcomes) -> None:
        def fake_post(url, **kw):
            outcome = outcomes[min(len(self.calls), len(outcomes) - 1)]
            self.calls.append(kw.get("json"))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        gmc.requests.post = fake_post


class TransportTests(_TransportCase):
    def test_single_page_no_token_yields_unwrapped_rows(self) -> None:
        self._queue(_page([{"a": 1}, {"a": 2}]))
        rows = list(self.client.search("SELECT a FROM some_view"))
        self.assertEqual(rows, [{"a": 1}, {"a": 2}])
        self.assertEqual(len(self.calls), 1)

    def test_pagination_follows_next_page_token(self) -> None:
        self._queue(_page([{"a": 1}], next_token="tok-2"), _page([{"a": 2}]))
        rows = list(self.client.search("SELECT a FROM some_view"))
        self.assertEqual(rows, [{"a": 1}, {"a": 2}])
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("pageToken", self.calls[0])
        self.assertEqual(self.calls[1]["pageToken"], "tok-2")

    def test_401_triggers_one_refresh_and_retries(self) -> None:
        self._queue(_FakeResp(401), _page([{"a": 1}]))
        rows = list(self.client.search("SELECT a FROM some_view"))
        self.assertEqual(rows, [{"a": 1}])
        self.assertEqual(self.client._headers["Authorization"], "Bearer tok2")

    def test_429_is_retried_with_throttle_backoff(self) -> None:
        self._queue(_FakeResp(429), _page([{"a": 1}]))
        rows = list(self.client.search("SELECT a FROM some_view"))
        self.assertEqual(rows, [{"a": 1}])
        self.assertEqual(self.clock.slept, [gmc.THROTTLE_BACKOFF_SECONDS[0]])

    def test_5xx_is_retried_then_succeeds(self) -> None:
        self._queue(_FakeResp(500, {}, "boom"), _page([{"a": 1}]))
        rows = list(self.client.search("SELECT a FROM some_view"))
        self.assertEqual(rows, [{"a": 1}])

    def test_network_error_is_retried(self) -> None:
        self._queue(requests.exceptions.ConnectionError("reset"), _page([{"a": 1}]))
        rows = list(self.client.search("SELECT a FROM some_view"))
        self.assertEqual(rows, [{"a": 1}])

    def test_permanent_4xx_raises_query_error_immediately(self) -> None:
        self._queue(_FakeResp(400, {"error": {"message": "Unknown field: bogus"}}))
        with self.assertRaises(gmc.GmcQueryError) as cm:
            list(self.client.search("SELECT bogus FROM some_view"))
        self.assertIn("Unknown field", str(cm.exception))
        self.assertEqual(len(self.calls), 1, "a permanent error must not be retried")

    def test_throttle_exhaustion_raises_transient(self) -> None:
        self._queue(*([_FakeResp(429)] * (len(gmc.THROTTLE_BACKOFF_SECONDS) + 1)))
        with self.assertRaises(gmc.GmcTransient):
            list(self.client.search("SELECT a FROM some_view"))


# --------------------------------------------------------------------------- #
#  row shaping (fake client exposes only .search(), no HTTP involved)
# --------------------------------------------------------------------------- #
class _FakeSearchClient:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def search(self, query: str, **kw):
        yield from self._rows


class RowShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        gmc.ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_ensure_schema_is_idempotent(self) -> None:
        gmc.ensure_schema(self.conn)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({
            "gmc_product_performance", "gmc_account_performance", "gmc_product_status",
            "gmc_product_issues", "gmc_price_competitiveness", "gmc_best_sellers",
            "gmc_best_seller_brands", "gmc_competitive_visibility",
        } <= tables)

    def test_sync_performance_writes_expected_row_shape(self) -> None:
        client = _FakeSearchClient([{
            "date": {"year": 2026, "month": 8, "day": 1},
            "marketingMethod": "ADS",
            "customerCountryCode": "US",
            "offerId": "shopify_US_111_222",
            "title": "Example Product",
            "brand": "Example Brand",
            "clicks": "10", "impressions": "500",
            "clickThroughRate": "0.02", "conversions": "1.5",
        }])
        import datetime as dt
        n = gmc.sync_performance(self.conn, client, dt.date(2026, 8, 1), dt.date(2026, 8, 1))
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT date, marketing_method, offer_id, product_id, variant_id, "
            "clicks, impressions, conversions FROM gmc_product_performance"
        ).fetchone()
        self.assertEqual(row, ("2026-08-01", "ADS", "shopify_US_111_222", "111", "222", 10, 500, 1.5))

    def test_sync_status_writes_status_and_issue_rows(self) -> None:
        client = _FakeSearchClient([{
            "id": "online:en:US:gmc1",
            "offerId": "shopify_US_111_222",
            "feedLabel": "US",
            "title": "Example Product",
            "brand": "Example Brand",
            "price": {"amountMicros": "19990000", "currencyCode": "USD"},
            "availability": "IN_STOCK",
            "aggregatedReportingContextStatus": "NOT_ELIGIBLE_OR_DISAPPROVED",
            "itemIssues": [{"type": {"code": "disapproved_price"},
                            "severity": {"aggregatedSeverity": "DISAPPROVED"},
                            "resolution": "MERCHANT_ACTION"}],
        }])
        n = gmc.sync_status(self.conn, client)
        self.assertEqual(n, 1)
        status_row = self.conn.execute(
            "SELECT gmc_id, offer_id, price_micros, price_currency, "
            "aggregated_status, n_issues FROM gmc_product_status"
        ).fetchone()
        self.assertEqual(status_row, ("online:en:US:gmc1", "shopify_US_111_222",
                                       19990000, "USD", "NOT_ELIGIBLE_OR_DISAPPROVED", 1))
        issue_row = self.conn.execute(
            "SELECT gmc_id, issue_code, severity FROM gmc_product_issues"
        ).fetchone()
        self.assertEqual(issue_row, ("online:en:US:gmc1", "disapproved_price", "DISAPPROVED"))

    def test_sync_status_prunes_rows_no_longer_in_the_feed(self) -> None:
        stamp_old = "2020-01-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO gmc_product_status (gmc_id, synced_at) VALUES ('gone', ?)",
            (stamp_old,))
        client = _FakeSearchClient([{"id": "still-here", "offerId": "x", "feedLabel": "US"}])
        gmc.sync_status(self.conn, client)
        remaining = {r[0] for r in self.conn.execute("SELECT gmc_id FROM gmc_product_status")}
        self.assertEqual(remaining, {"still-here"})

    def test_sync_pricing_writes_benchmark_row(self) -> None:
        client = _FakeSearchClient([{
            "id": "gmc1", "offerId": "shopify_US_111_222",
            "title": "Example Product", "brand": "Example Brand",
            "price": {"amountMicros": "20000000", "currencyCode": "USD"},
            "benchmarkPrice": {"amountMicros": "22000000", "currencyCode": "USD"},
            "reportCountryCode": "US",
        }])
        n = gmc.sync_pricing(self.conn, client)
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT gmc_id, product_id, variant_id, price_micros, benchmark_micros "
            "FROM gmc_price_competitiveness"
        ).fetchone()
        self.assertEqual(row, ("gmc1", "111", "222", 20000000, 22000000))

    def test_sync_best_sellers_prefers_stocked_over_top_n_for_same_rank(self) -> None:
        # Same (date, granularity, country, category, rank) key returned by
        # both the top_n and stocked queries -> "stocked" must win, since it
        # is the more actionable reason (see _REASON_RANK).
        shared = {
            "reportDate": {"year": 2026, "month": 7, "day": 13},
            "reportGranularity": "WEEKLY", "reportCountryCode": "US",
            "reportCategoryId": "166", "rank": "5", "previousRank": "7",
            "title": "Example Product", "brand": "Example Brand",
            "relativeDemand": "HIGH", "relativeDemandChange": "FLAT",
            "inventoryStatus": "IN_STOCK",
        }

        class _TwoQueryClient:
            def search(self, query, **kw):
                if "inventory_status !=" in query:
                    return iter([shared])
                return iter([shared])

        n = gmc.sync_best_sellers(self.conn, _TwoQueryClient(),
                                   [{"id": 166, "name": "Apparel"}], ["US"],
                                   top_n=10, granularities=("WEEKLY",))
        self.assertEqual(n, 1)
        reason = self.conn.execute("SELECT pull_reason FROM gmc_best_sellers").fetchone()[0]
        self.assertEqual(reason, "stocked")

    def test_sync_account_performance_writes_expected_row_shape(self) -> None:
        client = _FakeSearchClient([{
            "date": {"year": 2026, "month": 8, "day": 1},
            "week": {"year": 2026, "month": 7, "day": 27},
            "clicks": "40", "impressions": "900", "clickThroughRate": "0.044",
        }])
        import datetime as dt
        n = gmc.sync_account_performance(self.conn, client, dt.date(2026, 8, 1), dt.date(2026, 8, 1))
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT date, week_start, clicks, impressions, click_through_rate "
            "FROM gmc_account_performance"
        ).fetchone()
        self.assertEqual(row, ("2026-08-01", "2026-07-27", 40, 900, 0.044))

    def test_sync_best_sellers_includes_risers(self) -> None:
        riser_row = {
            "reportDate": {"year": 2026, "month": 7, "day": 13},
            "reportGranularity": "WEEKLY", "reportCountryCode": "US",
            "reportCategoryId": "166", "rank": "9000", "previousRank": "9500",
            "title": "Example Product", "brand": "Example Brand",
            "relativeDemand": "LOW", "relativeDemandChange": "RISER",
            "inventoryStatus": "NOT_IN_INVENTORY",
        }

        class _RiserClient:
            def search(self, query, **kw):
                if "relative_demand_change = 'RISER'" in query:
                    return iter([riser_row])
                return iter([])

        n = gmc.sync_best_sellers(self.conn, _RiserClient(),
                                   [{"id": 166, "name": "Apparel"}], ["US"],
                                   top_n=10, granularities=("WEEKLY",))
        self.assertEqual(n, 1)
        reason = self.conn.execute("SELECT pull_reason FROM gmc_best_sellers").fetchone()[0]
        self.assertEqual(reason, "riser")

    def test_sync_best_seller_brands_marks_tracked_brand(self) -> None:
        row = {
            "reportGranularity": "WEEKLY",
            "reportDate": {"year": 2026, "month": 7, "day": 13},
            "reportCountryCode": "US", "reportCategoryId": "166",
            "rank": "5243", "previousRank": "5300", "brand": "Example Brand",
            "relativeDemand": "VERY_LOW", "previousRelativeDemand": "VERY_LOW",
            "relativeDemandChange": "FLAT",
        }

        class _BrandClient:
            def search(self, query, **kw):
                if "brand IN" in query:
                    return iter([row])
                return iter([])

        n = gmc.sync_best_seller_brands(self.conn, _BrandClient(),
                                         [{"id": 166, "name": "Apparel"}], ["US"],
                                         brands=["Example Brand"], top_n=10,
                                         granularities=("WEEKLY",))
        self.assertEqual(n, 1)
        brand, is_tracked, reason = self.conn.execute(
            "SELECT brand, is_tracked_brand, pull_reason FROM gmc_best_seller_brands"
        ).fetchone()
        self.assertEqual((brand, is_tracked, reason), ("Example Brand", 1, "tracked_brand"))

    def test_sync_best_seller_brands_skips_the_tracked_query_with_no_brands(self) -> None:
        class _NoBrandQueryClient:
            def search(self, query, **kw):
                self_calls.append(query)
                return iter([])

        self_calls: list[str] = []
        gmc.sync_best_seller_brands(self.conn, _NoBrandQueryClient(),
                                     [{"id": 166, "name": "Apparel"}], ["US"],
                                     brands=[], top_n=10, granularities=("WEEKLY",))
        self.assertTrue(all("brand IN" not in q for q in self_calls))

    def test_sync_visibility_writes_competitor_row(self) -> None:
        # sync_visibility() queries once PER traffic_source (ALL/ADS/ORGANIC);
        # a real account returns different rows for each, so the fake mirrors
        # that by only answering the 'ALL' query, leaving ADS/ORGANIC empty.
        row = {
            "reportCategoryId": "166", "trafficSource": "ALL",
            "date": {"year": 2026, "month": 8, "day": 1},
            "domain": "example-competitor.com", "isYourDomain": False,
            "rank": "3", "adsOrganicRatio": "12.5", "relativeVisibility": "0.8",
            "reportCountryCode": "US",
        }

        class _VisibilityClient:
            def search(self, query, **kw):
                if "traffic_source = 'ALL'" in query:
                    yield row

        import datetime as dt
        n = gmc.sync_visibility(self.conn, _VisibilityClient(), [{"id": 166, "name": "Apparel"}],
                                 ["US"], dt.date(2026, 8, 1), dt.date(2026, 8, 1))
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT domain, is_your_domain, rank, ads_organic_ratio "
            "FROM gmc_competitive_visibility"
        ).fetchone()
        self.assertEqual(row, ("example-competitor.com", 0, 3, 12.5))


# --------------------------------------------------------------------------- #
#  CLI-level failure mode
# --------------------------------------------------------------------------- #
class MainEnvGuardTests(unittest.TestCase):
    def test_missing_env_vars_exit_cleanly_with_a_clear_message(self) -> None:
        import os
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["merchant_center_sync.py"]):
            with self.assertRaises(SystemExit) as cm:
                gmc.main()
        self.assertIn("GMC_MERCHANT_ID", str(cm.exception))
        self.assertIn("GMC_CREDENTIALS_FILE", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
