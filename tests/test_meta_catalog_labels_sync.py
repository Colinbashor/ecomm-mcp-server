"""meta_catalog_labels_sync: paging, diff, guards, batching, retry and status.
Hermetic: requests and the warehouse are patched out; no network, no sleep."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import requests

import meta_catalog_labels_sync as mc
from feed_fixtures import CLEAN_ENV, add_variant, make_db

ENV = {**CLEAN_ENV, "META_CATALOG_ID": "1000000000009", "META_CATALOG_ACCESS_TOKEN": "tok",
       "META_ACCESS_TOKEN": "", "META_LABELS_MIN_ROWS": "1", "META_LABELS_MAX_CLEAR_SHARE": ""}
L = ("Margin 50%+", "Full price", "", "", "")


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class CallTests(unittest.TestCase):
    def _call(self, bodies, method="GET"):
        sleeps = []
        side = [b if isinstance(b, Exception) else _Resp(b) for b in bodies]
        with mock.patch.object(mc.requests, "request", side_effect=side) as req:
            try:
                out = mc._call(method, "https://graph.example/x", sleep=sleeps.append)
            except RuntimeError as exc:
                out = exc
        return out, req, sleeps

    def test_transient_graph_codes_and_transport_errors_are_retried(self) -> None:
        out, req, sleeps = self._call([{"error": {"code": 2, "message": "temporary"}},
                                       requests.ConnectionError("reset"),
                                       {"error": {"code": 613, "message": "rate"}},
                                       {"ok": True}], method="POST")
        self.assertEqual(out, {"ok": True})
        self.assertEqual((req.call_count, sleeps), (4, [5, 10, 15]))

    def test_a_permanent_error_fails_at_once(self) -> None:
        out, req, sleeps = self._call([{"error": {"code": 190, "message": "bad token"}}])
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("bad token", str(out))
        self.assertEqual((req.call_count, sleeps), (1, []))

    def test_retries_are_bounded(self) -> None:
        out, req, _ = self._call([{"error": {"code": 1, "message": "unknown"}}] * 9)
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual(req.call_count, mc.MAX_ATTEMPTS)


class FetchTests(unittest.TestCase):
    def test_paging_follows_next_even_after_a_short_page(self) -> None:
        pages = [
            {"data": [{"retailer_id": "1000000000101", "custom_label_0": "Margin 50%+"}],
             "paging": {"next": "https://graph.example/page2"}},          # short page
            {"data": [{"retailer_id": "1000000000102"}], "paging": {}},
        ]
        with mock.patch.object(mc, "_get", side_effect=pages) as get:
            cur = mc.fetch_current("1000000000009", "tok")
        self.assertEqual(get.call_count, 2)
        self.assertIsNone(get.call_args_list[1].args[1])     # `next` carries the params
        self.assertEqual(cur["1000000000101"][0], "Margin 50%+")
        self.assertEqual(cur["1000000000102"], mc.EMPTY)


class PlanGuardTests(unittest.TestCase):
    def test_plan_changes_only_differences_and_clears_strays(self) -> None:
        desired = {"A": L, "B": L, "NEW": L}
        current = {"A": L, "B": mc.EMPTY, "STRAY": L}
        changes, stats = mc.plan(desired, current)
        self.assertEqual(sorted(changes), [("B", L), ("STRAY", mc.EMPTY)])
        self.assertEqual((stats["clears"], stats["not_in_catalog"], stats["matched"]), (1, 1, 2))

    def test_guards(self) -> None:
        base = dict(catalog=100, desired=100, clears=10)
        self.assertIsNone(mc.guard(base, 50, 0.3))
        self.assertIn("only 10 rows", mc.guard(dict(base, desired=10), 50, 0.3))
        self.assertIn("would clear", mc.guard(dict(base, clears=31), 50, 0.3))

    def test_batches_are_capped(self) -> None:
        changes = [(str(i), L) for i in range(mc.BATCH_SIZE + 1)]
        batches = mc.batch_requests(changes)
        self.assertEqual([len(b) for b in batches], [mc.BATCH_SIZE, 1])
        self.assertEqual(batches[0][0], {"method": "UPDATE",
                                         "data": {"id": "0", **dict(zip(mc.LABELS, L))}})


class SendBatchTests(unittest.TestCase):
    def test_posts_without_upsert_and_polls_to_finished(self) -> None:
        polls = [{"data": [{"status": "in_progress"}]},
                 {"data": [{"status": "finished", "errors_total_count": 1,
                            "errors": [{"message": "x"}]}]}]
        with mock.patch.object(mc, "_post", return_value={"handles": ["h1"]}) as post, \
                mock.patch.object(mc, "_get", side_effect=polls):
            res = mc.send_batch("1000000000009", "tok", [{"method": "UPDATE"}],
                                sleep=lambda s: None)
        data = post.call_args.kwargs["data"]
        self.assertEqual((data["allow_upsert"], json.loads(data["requests"])),
                         ("false", [{"method": "UPDATE"}]))
        self.assertEqual((res["status"], res["errors"]), ("finished", 1))


@mock.patch.dict(os.environ, ENV)
class RunTests(unittest.TestCase):
    def _run(self, current, execute=True, batch=None, limit=None, n_variants=2):
        conn = make_db()
        for i in range(n_variants):
            add_variant(conn, f"100000000010{i}", sku=f"SKU-100{i}", price=40, cost=10)
        logged = []
        batch = batch or {"handle": "h", "status": "finished", "errors": 0, "sample_errors": []}
        with mock.patch.object(mc, "_open_db", return_value=conn), \
                mock.patch.object(mc, "fetch_current", return_value=current), \
                mock.patch.object(mc, "send_batch", return_value=batch) as send, \
                mock.patch.object(mc.db, "log_sync", side_effect=lambda *a: logged.append(a)):
            rc = mc.run(execute, limit)
        return rc, logged, send

    def test_retailer_ids_default_to_the_variant_id(self) -> None:
        conn = make_db()
        add_variant(conn, "1000000000101")
        self.assertEqual(list(mc.build_desired(conn)), ["1000000000101"])

    def test_dry_run_sends_and_logs_nothing(self) -> None:
        rc, logged, send = self._run({"1000000000100": mc.EMPTY}, execute=False)
        self.assertEqual((rc, logged, send.call_count), (0, [], 0))

    def test_execute_sends_the_diff_and_logs_ok(self) -> None:
        rc, logged, send = self._run({"1000000000100": mc.EMPTY, "1000000000101": mc.EMPTY})
        self.assertEqual((rc, send.call_count), (0, 1))
        self.assertEqual(len(send.call_args.args[2]), 2)
        self.assertEqual((logged[-1][0], logged[-1][2], logged[-1][3]),
                         ("meta_catalog_labels", 2, "ok"))

    def test_nothing_to_change_is_ok_with_no_writes(self) -> None:
        conn = make_db()
        for i in range(2):
            add_variant(conn, f"100000000010{i}", sku=f"SKU-100{i}", price=40, cost=10)
        with mock.patch.object(mc, "_open_db", return_value=conn):
            current = mc.build_desired(conn)
        rc, logged, send = self._run(current)
        self.assertEqual((rc, send.call_count, logged[-1][3]), (0, 0, "ok"))

    def test_mass_clear_is_refused(self) -> None:
        current = {f"STRAY-{i}": L for i in range(10)}
        rc, logged, send = self._run(current)
        self.assertEqual((rc, send.call_count, logged[-1][3]), (1, 0, "error"))
        self.assertIn("would clear", logged[-1][4])

    def test_a_tiny_build_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"META_LABELS_MIN_ROWS": "50"}):
            rc, logged, send = self._run({"1000000000100": mc.EMPTY})
        self.assertEqual((rc, send.call_count, logged[-1][3]), (1, 0, "error"))

    def test_item_errors_or_an_unfinished_batch_are_degraded(self) -> None:
        rc, logged, _ = self._run({"1000000000100": mc.EMPTY, "1000000000101": mc.EMPTY},
                                  batch={"handle": "h", "status": "in_progress", "errors": 1,
                                         "sample_errors": []})
        self.assertEqual((rc, logged[-1][3]), (0, "degraded"))

    def test_every_item_failing_is_an_error(self) -> None:
        rc, logged, _ = self._run({"1000000000100": mc.EMPTY, "1000000000101": mc.EMPTY},
                                  batch={"handle": "h", "status": "finished", "errors": 2,
                                         "sample_errors": []})
        self.assertEqual((rc, logged[-1][3]), (1, "error"))

    def test_limit_caps_the_changes_sent(self) -> None:
        rc, logged, send = self._run({"1000000000100": mc.EMPTY, "1000000000101": mc.EMPTY},
                                     limit=1)
        self.assertEqual(len(send.call_args.args[2]), 1)
        self.assertIn("LIMITED to 1", logged[-1][4])

    def test_a_fetch_failure_logs_error(self) -> None:
        conn = make_db()
        with mock.patch.object(mc, "_open_db", return_value=conn), \
                mock.patch.object(mc, "fetch_current", side_effect=RuntimeError("GET failed")), \
                mock.patch.object(mc.db, "log_sync") as log:
            with self.assertRaises(RuntimeError):
                mc.run(True)
        self.assertEqual(log.call_args.args[3], "error")

    def test_required_env(self) -> None:
        with mock.patch.dict(os.environ, {"META_CATALOG_ID": ""}):
            with self.assertRaises(SystemExit):
                mc.check_required_env()
        with mock.patch.dict(os.environ, {"META_CATALOG_ACCESS_TOKEN": "",
                                          "META_ACCESS_TOKEN": "fallback"}):
            mc.check_required_env()
            self.assertEqual(mc.catalog_token(), "fallback")


if __name__ == "__main__":
    unittest.main()
