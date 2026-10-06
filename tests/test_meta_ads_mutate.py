"""Tests for meta_ads_mutate.py.

Hermetic: `requests.post` is faked, so no token or network is needed. Pins the
safety model the script rests on:

  * every field update sends `execution_options=["validate_only"]` unless
    `--execute` is passed,
  * `copy-adset` (whose `/copies` edge has no documented validate_only
    support) refuses to run at all without `--execute`, and creates the copy
    PAUSED unless `--go-live`,
  * an API error exits non-zero instead of printing success,
  * budgets convert major -> minor units by the currency offset.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

import meta_ads_mutate as mam


def _resp(status=200, body=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body if body is not None else {"success": True}
    return r


class MetaMutateTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {"META_ACCESS_TOKEN": "test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, argv, response=None):
        args = mam.build_parser().parse_args(argv)
        with mock.patch.object(mam.requests, "post", return_value=response or _resp()) as post, \
             mock.patch("builtins.print"):
            args.func(args)
        return post

    def test_status_update_is_validate_only_by_default(self):
        post = self._run(["pause-campaign", "--campaign-id", "111"])
        url = post.call_args.args[0]
        data = post.call_args.kwargs["data"]
        self.assertTrue(url.endswith("/111"))
        self.assertEqual(data["status"], "PAUSED")
        self.assertEqual(json.loads(data["execution_options"]), ["validate_only"])

    def test_execute_drops_validate_only(self):
        post = self._run(["resume-ad", "--ad-id", "222", "--execute"])
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["status"], "ACTIVE")
        self.assertNotIn("execution_options", data)

    def test_every_status_subcommand_defaults_to_dry_run(self):
        for cmd, flag in [("resume-campaign", "--campaign-id"), ("pause-campaign", "--campaign-id"),
                          ("resume-adset", "--adset-id"), ("pause-adset", "--adset-id"),
                          ("resume-ad", "--ad-id"), ("pause-ad", "--ad-id")]:
            post = self._run([cmd, flag, "1"])
            self.assertIn("execution_options", post.call_args.kwargs["data"], cmd)

    def test_copy_adset_refuses_without_execute(self):
        args = mam.build_parser().parse_args(
            ["copy-adset", "--adset-id", "1", "--dest-campaign-id", "2"])
        with mock.patch.object(mam.requests, "post") as post, mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
        self.assertNotEqual(cm.exception.code, 0)
        post.assert_not_called()

    def test_copy_adset_with_execute_is_created_paused(self):
        post = self._run(["copy-adset", "--adset-id", "1", "--dest-campaign-id", "2", "--execute"])
        self.assertTrue(post.call_args.args[0].endswith("/1/copies"))
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["status_option"], "PAUSED")
        self.assertEqual(data["campaign_id"], "2")
        self.assertEqual(data["deep_copy"], "true")

    def test_copy_adset_go_live_creates_active(self):
        post = self._run(["copy-adset", "--adset-id", "1", "--dest-campaign-id", "2",
                          "--execute", "--go-live"])
        self.assertEqual(post.call_args.kwargs["data"]["status_option"], "ACTIVE")

    def test_budget_converts_to_minor_units(self):
        post = self._run(["set-adset-budget", "--adset-id", "1", "--daily-amount", "50.25"])
        self.assertEqual(post.call_args.kwargs["data"]["daily_budget"], 5025)

    def test_budget_respects_zero_decimal_currency_offset(self):
        post = self._run(["set-adset-budget", "--adset-id", "1", "--daily-amount", "3000",
                          "--currency-offset", "1"])
        self.assertEqual(post.call_args.kwargs["data"]["daily_budget"], 3000)

    def test_api_error_exits_nonzero(self):
        err = _resp(400, {"error": {"message": "Permissions error", "code": 200}})
        args = mam.build_parser().parse_args(["pause-adset", "--adset-id", "1"])
        with mock.patch.object(mam.requests, "post", return_value=err), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
        self.assertEqual(cm.exception.code, 1)

    def test_error_body_on_http_200_still_fails(self):
        err = _resp(200, {"error": {"message": "Invalid parameter"}})
        args = mam.build_parser().parse_args(["pause-ad", "--ad-id", "1", "--execute"])
        with mock.patch.object(mam.requests, "post", return_value=err), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                args.func(args)


class RenameTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {"META_ACCESS_TOKEN": "test-token"})
        env.start()
        self.addCleanup(env.stop)

    def test_rename_is_validate_only_without_execute(self):
        args = mam.build_parser().parse_args(["rename", "--object-id", "1", "--name", "N"])
        with mock.patch.object(mam.requests, "post", return_value=_resp()) as post,              mock.patch("builtins.print"):
            args.func(args)
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["name"], "N")
        self.assertEqual(json.loads(sent["execution_options"]), ["validate_only"])

    def _csv(self, rows):
        import os, tempfile
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write("level,id,current_name,proposed_name\n")
            for r in rows:
                fh.write(",".join(r) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_csv_skips_when_live_name_changed_and_writes_when_matching(self):
        path = self._csv([("ad", "1", "old", "new"), ("ad", "2", "old", "new")])
        args = mam.build_parser().parse_args(["rename-from-csv", "--csv", path, "--delay", "0"])
        gets = [_resp(body={"name": "someone edited"}), _resp(body={"name": "old"})]
        with mock.patch.object(mam.requests, "get", side_effect=gets),              mock.patch.object(mam.requests, "post", return_value=_resp()) as post,              mock.patch("builtins.print"):
            args.func(args)
        self.assertEqual(post.call_count, 1)  # only id 2 written
        self.assertIn("/2", post.call_args.args[0])

    def test_post_quiet_retries_on_rate_limit(self):
        limited = _resp(body={"error": {"code": 613, "message": "rate"}})
        with mock.patch.object(mam.requests, "post", side_effect=[limited, _resp()]),              mock.patch.object(mam.time, "sleep") as sl:
            self.assertEqual(mam._post_quiet("1", {"name": "n"}, True), "ok")
        sl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
