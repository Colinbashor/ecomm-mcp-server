"""Connectors that call out through warehouse.net ride out a connection reset.

Each test raises a reset once and checks the connector gets through on the
next attempt. Hermetic: no network, in-memory database.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

import requests

from warehouse import net


def _reset() -> requests.ConnectionError:
    return requests.ConnectionError(
        ("Connection aborted.", ConnectionResetError(10054, "forcibly closed")))


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class GoogleAdsStructureTests(unittest.TestCase):
    def _run(self, fetch):
        import google_ads_structure_sync as gs
        with mock.patch.object(gs.db, "connect",
                               side_effect=lambda *a, **k: sqlite3.connect(":memory:")), \
                mock.patch.dict(gs.FETCH, {"campaigns": fetch}), \
                mock.patch.object(net, "backoff", return_value=0):
            return gs.run("2026-09-15", only=["campaigns"])

    def test_a_grain_whose_fetch_resets_once_still_lands(self) -> None:
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _reset()
            return []

        _, failures = self._run(flaky)
        self.assertEqual((calls["n"], failures), (2, []))

    def test_a_real_error_is_still_a_grain_failure(self) -> None:
        def broken():
            raise ValueError("invalid query")

        _, failures = self._run(broken)
        self.assertEqual(len(failures), 1)
        self.assertIn("invalid query", failures[0])


class TikTokFinanceTests(unittest.TestCase):
    def test_get_rides_out_a_connection_reset(self) -> None:
        import tiktok_finance_sync as tf
        payload = {"code": 0, "data": {"statements": []}}
        env = {"TIKTOK_APP_KEY": "k", "TIKTOK_APP_SECRET": "s",
               "TIKTOK_SHOP_CIPHER": "c", "TIKTOK_ACCESS_TOKEN": "t"}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(net.requests, "request",
                                  side_effect=[_reset(), _Resp(payload)]) as req, \
                mock.patch.object(net, "backoff", return_value=0):
            out = tf._get("/finance/202309/statements", {}, None)
        self.assertEqual(out, payload)
        self.assertEqual(req.call_count, 2)


if __name__ == "__main__":
    unittest.main()
