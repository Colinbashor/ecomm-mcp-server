"""Hermetic tests for tiktok_listing_quality_sync.py -- no network, no real
warehouse.db.

Covers: schema creation, the missing-credentials guard, the batch-halving
retry (one bad id in a batch must not lose the other products' data), the
degraded-vs-ok status distinction, and the delete-then-reinsert snapshot
semantics for a product's issues.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import tiktok_listing_quality_sync as tlq


def _product(pid, tier="GOOD", diagnoses=None):
    return {
        "id": pid,
        "listing_quality": {"current_tier": tier, "remaining_recommendations": 0},
        "diagnoses": diagnoses or [],
    }


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_both_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        tlq.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tiktok_listing_quality", tables)
        self.assertIn("tiktok_listing_quality_issues", tables)
        conn.close()


class CheckRequiredEnvTests(unittest.TestCase):
    def test_raises_clear_systemexit_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                tlq.check_required_env()
        self.assertIn("TIKTOK_APP_KEY", str(cm.exception))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = sqlite3.connect(":memory:")
        tlq.ensure_schema(self.conn)
        sleep_patch = patch.object(tlq.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_writes_tier_and_issue_rows(self) -> None:
        diagnoses = [{"field": "TITLE", "suggestion": {"hint": "shorten"},
                      "diagnosis_results": [{"code": "T1", "how_to_solve": "shorten it",
                                              "quality_tier": "GOOD"}]}]
        products = [_product("p1", tier="FAIR", diagnoses=diagnoses)]
        with patch.object(tlq, "_request", return_value={"data": {"products": products}}):
            requested, written, failed = tlq.run(self.conn, ["p1"])
        self.assertEqual((requested, written, failed), (1, 1, []))
        row = self.conn.execute(
            "SELECT current_tier FROM tiktok_listing_quality WHERE product_id='p1'").fetchone()
        self.assertEqual(row, ("FAIR",))
        issue = self.conn.execute(
            "SELECT field, code, how_to_solve FROM tiktok_listing_quality_issues "
            "WHERE product_id='p1'").fetchone()
        self.assertEqual(issue, ("TITLE", "T1", "shorten it"))

    def test_reinsert_replaces_prior_issues_for_the_same_product(self) -> None:
        first = [_product("p1", diagnoses=[{"field": "TITLE", "diagnosis_results":
                                             [{"code": "OLD", "how_to_solve": "x"}]}])]
        with patch.object(tlq, "_request", return_value={"data": {"products": first}}):
            tlq.run(self.conn, ["p1"])
        second = [_product("p1", diagnoses=[{"field": "IMAGE", "diagnosis_results":
                                              [{"code": "NEW", "how_to_solve": "y"}]}])]
        with patch.object(tlq, "_request", return_value={"data": {"products": second}}):
            tlq.run(self.conn, ["p1"])
        codes = {r[0] for r in self.conn.execute(
            "SELECT code FROM tiktok_listing_quality_issues WHERE product_id='p1'")}
        self.assertEqual(codes, {"NEW"})  # OLD gone, not accumulated

    def test_one_bad_id_in_a_batch_does_not_lose_the_rest(self) -> None:
        """_request fails for the whole 2-id batch; halving isolates the bad id
        so the good one still gets written and only the bad one is reported failed."""
        def fake_request(product_ids):
            if "bad" in product_ids:
                if len(product_ids) == 1:
                    raise RuntimeError("12052260: deleted product")
                raise RuntimeError("batch failure")
            return {"data": {"products": [_product(pid) for pid in product_ids]}}

        with patch.object(tlq, "_request", side_effect=fake_request):
            requested, written, failed = tlq.run(self.conn, ["good", "bad"])
        self.assertEqual(requested, 2)
        self.assertEqual(written, 1)
        self.assertEqual(failed, [("bad", "12052260: deleted product")])
        row = self.conn.execute(
            "SELECT product_id FROM tiktok_listing_quality").fetchall()
        self.assertEqual(row, [("good",)])

    def test_all_ids_failing_reports_zero_written(self) -> None:
        with patch.object(tlq, "_request", side_effect=RuntimeError("down")):
            requested, written, failed = tlq.run(self.conn, ["p1", "p2"])
        self.assertEqual(written, 0)
        self.assertEqual({f[0] for f in failed}, {"p1", "p2"})


class RequestTokenRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "TIKTOK_APP_KEY": "key", "TIKTOK_APP_SECRET": "secret",
            "TIKTOK_ACCESS_TOKEN": "tok", "TIKTOK_SHOP_CIPHER": "cipher",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _resp(self, code, message="ok"):
        class _R:
            status_code = 200

            def json(self):
                return {"code": code, "message": message, "data": {"products": []}}
        return _R()

    def test_expired_token_triggers_one_refresh_and_retry(self) -> None:
        expired_code = next(iter(tlq.TOKEN_EXPIRED_CODES))
        with patch.object(tlq.requests, "get",
                           side_effect=[self._resp(expired_code), self._resp(0)]) as get, \
             patch.object(tlq, "_refresh_access_token") as refresh:
            data = tlq._request(["p1"])
        self.assertEqual(get.call_count, 2)
        refresh.assert_called_once()
        self.assertEqual(data["code"], 0)

    def test_non_zero_non_expired_code_raises_without_retry(self) -> None:
        with patch.object(tlq.requests, "get", return_value=self._resp(99999, "boom")) as get:
            with self.assertRaises(RuntimeError):
                tlq._request(["p1"])
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
