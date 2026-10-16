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


def _clean_adset_get(url, params=None, timeout=None):
    """A guard read: an ad set with plain targeting and no child ad sets."""
    r = mock.Mock()
    if url.endswith("/adsets"):
        r.json.return_value = {"data": []}
    else:
        r.json.return_value = {"name": "Some ad set", "effective_status": "PAUSED",
                               "campaign": {"name": "Some campaign"}, "targeting": {}}
    return r


class MetaMutateTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {
            "META_ACCESS_TOKEN": "test-token", "META_TARGETING_RULES": "no-such-rules.json"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, argv, response=None):
        args = mam.build_parser().parse_args(argv)
        with mock.patch.object(mam.requests, "post", return_value=response or _resp()) as post, \
             mock.patch.object(mam.requests, "get", side_effect=_clean_adset_get), \
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


class CopyAdsTests(unittest.TestCase):
    SRC = {"data": [{"id": "a1", "name": "One", "effective_status": "ACTIVE"},
                    {"id": "a2", "name": "Two", "effective_status": "PAUSED"},
                    {"id": "a3", "name": "Gone", "effective_status": "DELETED"}]}

    def setUp(self):
        env = mock.patch.dict("os.environ", {"META_ACCESS_TOKEN": "test-token"})
        env.start()
        self.addCleanup(env.stop)
        sl = mock.patch.object(mam.time, "sleep")
        sl.start()
        self.addCleanup(sl.stop)

    @staticmethod
    def _get(src, dst):
        def fake(url, params=None, timeout=None):
            m = mock.Mock()
            m.json.return_value = src if "/111/" in url else dst
            return m
        return fake

    def _run(self, argv, src, dst, post_side_effect=None):
        args = mam.build_parser().parse_args(argv)
        post_kw = ({"side_effect": post_side_effect} if post_side_effect
                   else {"return_value": _resp(body={"copied_ad_id": "9"})})
        with mock.patch.object(mam.requests, "get", side_effect=self._get(src, dst)), \
             mock.patch.object(mam.requests, "post", **post_kw) as post, \
             mock.patch("builtins.print"):
            args.func(args)
        return post

    def test_dry_run_posts_nothing(self):
        post = self._run(["copy-ads", "--source-adset-id", "111", "--dest-adset-id", "222"],
                         self.SRC, {"data": []})
        post.assert_not_called()

    def test_execute_copies_paused_and_skips_deleted_and_existing(self):
        post = self._run(["copy-ads", "--source-adset-id", "111", "--dest-adset-id", "222",
                          "--execute"], self.SRC, {"data": [{"id": "x", "name": "One"}]})
        self.assertEqual(post.call_count, 1)  # "One" exists, "Gone" is DELETED
        self.assertTrue(post.call_args.args[0].endswith("/a2/copies"))
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["adset_id"], "222")
        self.assertEqual(data["status_option"], "PAUSED")

    def test_limit_caps_copies(self):
        post = self._run(["copy-ads", "--source-adset-id", "111", "--dest-adset-id", "222",
                          "--limit", "1", "--execute"], self.SRC, {"data": []})
        self.assertEqual(post.call_count, 1)

    def test_list_error_aborts_instead_of_reading_as_empty(self):
        with self.assertRaises(SystemExit):
            self._run(["copy-ads", "--source-adset-id", "111", "--dest-adset-id", "222",
                       "--execute"], {"error": {"message": "nope"}}, {"data": []})

    def test_rate_limit_is_retried(self):
        limited = _resp(body={"error": {"code": 613, "message": "slow"}})
        post = self._run(["copy-ads", "--source-adset-id", "111", "--dest-adset-id", "222",
                          "--limit", "1", "--execute"], self.SRC, {"data": []},
                         post_side_effect=[limited, _resp(body={"copied_ad_id": "9"})])
        self.assertEqual(post.call_count, 2)


class PdpAudienceTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {
            "META_ACCESS_TOKEN": "t", "META_AD_ACCOUNT_ID": "123", "META_PIXEL_ID": "999"})
        env.start()
        self.addCleanup(env.stop)

    def _args(self, execute=False):
        return mam.build_parser().parse_args(
            ["create-pdp-audience", "--name", "X PDP Visitors", "--url-keyword", "x-handle"]
            + (["--execute"] if execute else []))

    def _listing(self, data):
        r = mock.Mock()
        r.json.return_value = {"data": data}
        return r

    def test_dry_run_posts_nothing(self):
        with mock.patch.object(mam.requests, "get", return_value=self._listing([])), \
                mock.patch.object(mam.requests, "post") as post:
            args = self._args()
            args.func(args)
        post.assert_not_called()

    def test_existing_name_is_not_recreated(self):
        with mock.patch.object(mam.requests, "get",
                               return_value=self._listing([{"id": "7", "name": "X PDP Visitors"}])), \
                mock.patch.object(mam.requests, "post") as post:
            args = self._args(execute=True)
            args.func(args)
        post.assert_not_called()

    def test_execute_posts_rule_with_pixel(self):
        created = mock.Mock()
        created.json.return_value = {"id": "55"}
        with mock.patch.object(mam.requests, "get", return_value=self._listing([])), \
                mock.patch.object(mam.requests, "post", return_value=created) as post:
            args = self._args(execute=True)
            args.func(args)
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["pixel_id"], "999")
        self.assertIn("x-handle", data["rule"])

    def test_missing_pixel_env_exits(self):
        with mock.patch.dict("os.environ", {"META_PIXEL_ID": ""}):
            args = self._args()
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


COLD_RULES = [
    {"name": "cold", "pattern": r"^\s*COLD\s*\|", "advantage_audience_off": True,
     "forbid_include": True, "required_exclusions": {"Purchasers": ["900", "901"]}},
    {"name": "warm", "pattern": r"^\s*WARM\s*\|", "advantage_audience_off": True,
     "require_include": True, "required_exclusions": {"Purchasers": ["900"]},
     "include_name_regex": "pdp"},
]


class TargetingRulesTests(unittest.TestCase):
    def test_stage_matches_adset_name_before_campaign(self):
        rule = mam.stage_from_names("WARM | Jacket", "COLD | Prospecting", COLD_RULES)
        self.assertEqual(rule["name"], "warm")
        self.assertEqual(mam.stage_from_names("x", "COLD | p", COLD_RULES)["name"], "cold")
        self.assertIsNone(mam.stage_from_names("x", "y", COLD_RULES))

    def test_warm_with_no_include_is_a_hard_break(self):
        tgt = {"targeting_automation": {"advantage_audience": 0},
               "excluded_custom_audiences": [{"id": "900"}]}
        br = mam.targeting_breaks(COLD_RULES[1], tgt, {})
        self.assertIn("HARD", [lvl for lvl, _ in br])
        self.assertTrue(any("runs broad" in m for _, m in br))

    def test_lookalike_include_does_not_count_as_real(self):
        tgt = {"custom_audiences": [{"id": "5"}], "excluded_custom_audiences": [{"id": "901"}],
               "targeting_automation": {"advantage_audience": 0}}
        aud = {"5": {"name": "LAL 1%", "subtype": "LOOKALIKE"}}
        self.assertEqual(mam.targeting_breaks(COLD_RULES[0], tgt, aud), [])
        self.assertTrue(mam.targeting_breaks(COLD_RULES[1], tgt, aud))

    def test_cold_missing_exclusion_and_advantage_on(self):
        br = mam.targeting_breaks(COLD_RULES[0], {"targeting_automation": {"advantage_audience": 1}}, {})
        msgs = " ".join(m for _, m in br)
        self.assertIn("Advantage+", msgs)
        self.assertIn("Purchasers", msgs)

    def test_overlap_is_hard_even_without_rules(self):
        tgt = {"custom_audiences": [{"id": "1"}], "excluded_custom_audiences": [{"id": "1"}]}
        br = mam.targeting_breaks(None, tgt, {})
        self.assertEqual([lvl for lvl, _ in br], ["HARD", "WARN"])

    def test_no_rule_alone_is_only_a_warning(self):
        self.assertEqual([lvl for lvl, _ in mam.targeting_breaks(None, {}, {})], ["WARN"])

    def test_load_rules_missing_file_is_empty_and_bad_file_exits(self):
        import os
        import tempfile
        self.assertEqual(mam.load_rules("definitely-missing.json"), [])
        fd, path = tempfile.mkstemp(suffix=".json")
        os.write(fd, b"{not json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with self.assertRaises(SystemExit):
            mam.load_rules(path)

    def test_example_rules_file_parses(self):
        from pathlib import Path
        rules = mam.load_rules(Path(mam.__file__).with_name("meta_targeting_rules.example.json"))
        self.assertEqual([r["name"] for r in rules], ["cold", "warm_product", "retargeting"])
        self.assertEqual(mam.stage_from_names("RETARGETING | x", "", rules)["name"], "retargeting")


class GuardAndEditorTests(unittest.TestCase):
    BROKEN_WARM = {"name": "WARM | Jacket", "effective_status": "PAUSED",
                   "campaign": {"name": "c"},
                   "targeting": {"targeting_automation": {"advantage_audience": 0},
                                 "excluded_custom_audiences": [{"id": "900"}]}}

    def setUp(self):
        env = mock.patch.dict("os.environ", {"META_ACCESS_TOKEN": "t"})
        env.start()
        self.addCleanup(env.stop)
        rules = mock.patch.object(mam, "load_rules", return_value=COLD_RULES)
        rules.start()
        self.addCleanup(rules.stop)

    def _get(self, adset):
        def fake(url, params=None, timeout=None):
            r = mock.Mock()
            if params and "ids" in params:
                r.json.return_value = {i: {"name": f"aud {i}", "subtype": "WEBSITE"}
                                       for i in params["ids"].split(",")}
            else:
                r.json.return_value = adset
            return r
        return fake

    def _run(self, argv, adset):
        args = mam.build_parser().parse_args(argv)
        with mock.patch.object(mam.requests, "get", side_effect=self._get(adset)), \
             mock.patch.object(mam.requests, "post", return_value=_resp()) as post, \
             mock.patch("builtins.print"):
            args.func(args)
        return post

    def test_resume_refuses_a_hard_break(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(["resume-adset", "--adset-id", "1"], self.BROKEN_WARM)
        self.assertEqual(cm.exception.code, 1)

    def test_force_targeting_overrides(self):
        post = self._run(["resume-adset", "--adset-id", "1", "--force-targeting"], self.BROKEN_WARM)
        self.assertEqual(post.call_args.kwargs["data"]["status"], "ACTIVE")

    def test_copy_adset_checks_the_source(self):
        with self.assertRaises(SystemExit):
            self._run(["copy-adset", "--adset-id", "1", "--dest-campaign-id", "2", "--execute"],
                      self.BROKEN_WARM)

    def test_check_targeting_exit_code(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(["check-targeting", "--adset-id", "1"], self.BROKEN_WARM)
        self.assertEqual(cm.exception.code, 1)

    def test_edit_keeps_other_targeting_keys_and_is_validate_only(self):
        adset = {"name": "WARM | Jacket", "effective_status": "ACTIVE", "campaign": {"name": "c"},
                 "targeting": {"geo_locations": {"countries": ["US"]}, "age_min": 18,
                               "age_range": [18, 65],
                               "excluded_custom_audiences": [{"id": "900"}],
                               "targeting_automation": {"advantage_audience": 1}}}
        post = self._run(["edit-adset-audiences", "--adset-id", "1", "--include", "77",
                          "--advantage-off"], adset)
        data = post.call_args.kwargs["data"]
        tgt = json.loads(data["targeting"])
        self.assertEqual(tgt["geo_locations"], {"countries": ["US"]})
        self.assertEqual(tgt["custom_audiences"], [{"id": "77"}])
        self.assertEqual(tgt["targeting_automation"]["advantage_audience"], 0)
        self.assertEqual(tgt["targeting_automation"]["individual_setting"], {"age": 1, "gender": 1})
        self.assertIn("execution_options", data)

    def test_edit_refuses_include_and_exclude_of_same_audience(self):
        with self.assertRaises(SystemExit):
            self._run(["edit-adset-audiences", "--adset-id", "1", "--include", "900"],
                      self.BROKEN_WARM)

    def test_edit_with_no_change_sends_nothing(self):
        post = self._run(["edit-adset-audiences", "--adset-id", "1", "--exclude", "900"],
                         self.BROKEN_WARM)
        post.assert_not_called()

    def test_remove_exclude(self):
        post = self._run(["edit-adset-audiences", "--adset-id", "1", "--remove-exclude", "900"],
                         self.BROKEN_WARM)
        tgt = json.loads(post.call_args.kwargs["data"]["targeting"])
        self.assertNotIn("excluded_custom_audiences", tgt)


class VideoViewersAudienceTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {
            "META_ACCESS_TOKEN": "t", "META_AD_ACCOUNT_ID": "123", "META_PAGE_ID": "444"})
        env.start()
        self.addCleanup(env.stop)

    def _listing(self, data):
        r = mock.Mock()
        r.json.return_value = {"data": data}
        return r

    def _args(self, *extra):
        return mam.build_parser().parse_args(
            ["create-vv-audience", "--name", "X VV - 25%", "--video-ids", "11", "22", "11", *extra])

    def test_dry_run_posts_nothing(self):
        with mock.patch.object(mam.requests, "get", return_value=self._listing([])), \
                mock.patch.object(mam.requests, "post") as post, mock.patch("builtins.print"):
            a = self._args()
            a.func(a)
        post.assert_not_called()

    def test_execute_builds_one_rule_per_unique_video_with_page_context(self):
        created = mock.Mock()
        created.json.return_value = {"id": "9"}
        with mock.patch.object(mam.requests, "get", return_value=self._listing([])), \
                mock.patch.object(mam.requests, "post", return_value=created) as post, \
                mock.patch("builtins.print"):
            a = self._args("--execute")
            a.func(a)
        data = post.call_args.kwargs["data"]
        rule = json.loads(data["rule"])
        self.assertEqual([r["object_id"] for r in rule], ["11", "22"])
        self.assertTrue(all(r["context_id"] == "444" for r in rule))
        self.assertEqual(data["subtype"], "ENGAGEMENT")

    def test_existing_name_is_not_recreated(self):
        with mock.patch.object(mam.requests, "get",
                               return_value=self._listing([{"id": "7", "name": "X VV - 25%"}])), \
                mock.patch.object(mam.requests, "post") as post, mock.patch("builtins.print"):
            a = self._args("--execute")
            a.func(a)
        post.assert_not_called()

    def test_non_numeric_id_refused_before_any_call(self):
        a = mam.build_parser().parse_args(["create-vv-audience", "--name", "n", "--video-ids", "abc"])
        with mock.patch.object(mam.requests, "get") as get, mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                a.func(a)
        get.assert_not_called()

    def test_missing_page_env_exits(self):
        with mock.patch.dict("os.environ", {"META_PAGE_ID": ""}):
            a = self._args()
            with self.assertRaises(SystemExit):
                a.func(a)


if __name__ == "__main__":
    unittest.main()
