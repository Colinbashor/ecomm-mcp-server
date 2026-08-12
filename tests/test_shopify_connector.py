"""Tests for the Shopify connector's HTTP transport (`shopify._post`).

THE load-bearing property is that a dropped connection is RETRIED, not raised.
Observed live 2026-08-04: `run_sync.py --only shopify --days 1` failed outright
with ConnectionReset(10054) and an immediate identical re-run succeeded (7,461
rows) — so one transient blip was taking down the whole nightly Shopify step in
pipeline_runner wave 1. Carriers shedding connections under load is expected
behaviour (CLAUDE.md documents the same for api.amazon.com), and
shopify_bulk_backfill._gql already handled it; these tests pin the same
behaviour into the incremental path so it cannot regress back.

The negative cases matter just as much: the except clause must NOT swallow a
real API/GraphQL failure into eight pointless retries.
"""
from __future__ import annotations

import os
import time as _real_time
import unittest

import requests

from warehouse.connectors import shopify

_ORDERS = {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}
_THROTTLED = {"errors": [{"extensions": {"code": "THROTTLED"}}]}


class _FakeResp:
    def __init__(self, status: int = 200, payload: object | None = None,
                 headers: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self) -> object:
        return self._payload


def _ok(data: dict | None = None) -> _FakeResp:
    return _FakeResp(200, {"data": data if data is not None else _ORDERS})


def _reset() -> requests.exceptions.ConnectionError:
    """The exact live failure from 2026-08-04."""
    return requests.exceptions.ConnectionError(
        "('Connection aborted.', ConnectionResetError(10054, "
        "'An existing connection was forcibly closed by the remote host'))")


class _TimeShim:
    """Stands in for the `time` module so retry backoff costs no wall-clock.

    A shim rather than patching `time.sleep` itself — that is stdlib state shared
    with every other module in the suite.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def time(self) -> float:
        return _real_time.time()


class _PostCase(unittest.TestCase):
    """Base that isolates env, the token helper, `requests.post` and the clock."""

    def setUp(self) -> None:
        self._shop = os.environ.get("SHOPIFY_SHOP")
        os.environ["SHOPIFY_SHOP"] = "test.myshopify.com"
        self._post_fn = shopify.requests.post
        self._token = shopify._access_token
        self._time = shopify.time
        shopify._access_token = lambda shop: "tok"  # noqa: SLF001
        self.clock = _TimeShim()
        shopify.time = self.clock
        self.calls: list[str] = []

    def tearDown(self) -> None:
        shopify.requests.post = self._post_fn
        shopify._access_token = self._token  # noqa: SLF001
        shopify.time = self._time
        if self._shop is None:
            os.environ.pop("SHOPIFY_SHOP", None)
        else:
            os.environ["SHOPIFY_SHOP"] = self._shop

    def _queue(self, *outcomes) -> None:
        """Queue one outcome per attempt; an exception instance gets raised.
        The last outcome repeats, so a single item means "always this"."""
        def fake_post(url, **kw):
            outcome = outcomes[min(len(self.calls), len(outcomes) - 1)]
            self.calls.append(url)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        shopify.requests.post = fake_post

    def _call(self) -> dict:
        return shopify._post({"n": 1, "cursor": None, "q": "created_at:>='x'"})


class NetworkRetryTests(_PostCase):
    def test_connection_error_is_retried_and_second_payload_returned(self) -> None:
        # The regression this whole file exists for.
        self._queue(_reset(), _ok())
        self.assertEqual(self._call(), _ORDERS)
        self.assertEqual(len(self.calls), 2, "the blip must be retried, not raised")

    def test_timeout_is_retried_too(self) -> None:
        self._queue(requests.exceptions.Timeout("read timed out"), _ok())
        self.assertEqual(self._call(), _ORDERS)
        self.assertEqual(len(self.calls), 2)

    def test_backoff_matches_the_sibling_ladder(self) -> None:
        # min(60, 3 * (attempt + 1)) — same ladder as shopify_bulk_backfill._gql.
        self._queue(_reset(), _reset(), _ok())
        self._call()
        self.assertEqual(self.clock.slept, [3, 6])

    def test_repeated_blips_within_the_attempt_budget_still_succeed(self) -> None:
        self._queue(*([_reset()] * 7 + [_ok()]))
        self.assertEqual(self._call(), _ORDERS)
        self.assertEqual(len(self.calls), 8)

    def test_exhausted_network_retries_name_the_underlying_error(self) -> None:
        # "kept throttling" would be a lie here, and 10054 is the searchable clue.
        self._queue(_reset())
        with self.assertRaises(RuntimeError) as cm:
            self._call()
        self.assertIn("10054", str(cm.exception))
        self.assertEqual(len(self.calls), 8, "the 8-attempt budget must be unchanged")

    def test_token_exchange_blip_is_retried(self) -> None:
        # _access_token posts to the same host and drops the same way, which is
        # why it sits inside the try rather than above the loop.
        attempts = []

        def flaky_token(shop):
            attempts.append(shop)
            if len(attempts) == 1:
                raise _reset()
            return "tok"

        shopify._access_token = flaky_token  # noqa: SLF001
        self._queue(_ok())
        self.assertEqual(self._call(), _ORDERS)
        self.assertEqual(len(attempts), 2)


class ExistingBranchesPreservedTests(_PostCase):
    """The 429 / THROTTLED / hard-error branches must be untouched by the port."""

    def test_429_is_still_retried_honouring_retry_after(self) -> None:
        self._queue(_FakeResp(429, {}, {"Retry-After": "5"}), _ok())
        self.assertEqual(self._call(), _ORDERS)
        self.assertEqual(self.clock.slept, [5.0])

    def test_graphql_throttled_is_still_retried(self) -> None:
        self._queue(_FakeResp(200, _THROTTLED), _ok())
        self.assertEqual(self._call(), _ORDERS)
        self.assertEqual(len(self.calls), 2)

    def test_throttle_exhaustion_still_reports_throttling(self) -> None:
        self._queue(_FakeResp(200, _THROTTLED))
        with self.assertRaises(RuntimeError) as cm:
            self._call()
        self.assertIn("kept throttling", str(cm.exception))

    def test_hard_http_error_raises_immediately(self) -> None:
        # Proves the new except is not over-broad: a 500 is not a blip to retry.
        self._queue(_FakeResp(500, {}, {}, "boom"))
        with self.assertRaises(RuntimeError) as cm:
            self._call()
        self.assertIn("500", str(cm.exception))
        self.assertEqual(len(self.calls), 1)

    def test_real_graphql_error_raises_immediately(self) -> None:
        self._queue(_FakeResp(200, {"errors": [{"message": "ACCESS_DENIED"}]}))
        with self.assertRaises(RuntimeError) as cm:
            self._call()
        self.assertIn("ACCESS_DENIED", str(cm.exception))
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
