"""Tests for reacher_sample_limits.py -- the live write tool that caps/clears
per-product Reacher sample limits.

Hermetic: no network, no real Reacher credentials. `_call()` is the only
function that touches the network (via `requests.request`) and is mocked in
every test that needs it. State/log files are redirected to a temp directory
per test so runs never touch a real `reacher_sample_limit_overrides.json`.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import reacher_sample_limits as rsl


def _config(product_id: str, limit, sku: str = "", name: str = "", config_id: str = "cfg-1") -> dict:
    return {"id": config_id, "productId": product_id, "monthlySampleLimit": limit,
            "sku": sku, "productName": name}


class ResolveBySkuTests(unittest.TestCase):
    def test_matches_case_insensitively_against_reacher_sku_field(self) -> None:
        configs = [_config("111", 5, sku="sku-abc", name="Widget")]
        out = rsl.resolve_by_sku(["SKU-ABC"], configs)
        self.assertEqual(out, {"SKU-ABC": [("111", "Widget")]})

    def test_unmatched_sku_returns_empty_list_and_prints_a_reason(self) -> None:
        configs = [_config("111", 5, sku="sku-abc")]
        with mock.patch("builtins.print") as p:
            out = rsl.resolve_by_sku(["NOPE"], configs)
        self.assertEqual(out, {"NOPE": []})
        self.assertTrue(any("no Reacher product-config" in str(c) for c in p.call_args_list))

    def test_blank_sku_fields_are_never_matched(self) -> None:
        configs = [_config("111", 5, sku="")]
        out = rsl.resolve_by_sku([""], configs)
        self.assertEqual(out, {"": []})

    def test_first_config_wins_on_a_duplicate_sku(self) -> None:
        configs = [_config("111", 5, sku="dup", name="First"),
                   _config("222", 3, sku="dup", name="Second")]
        out = rsl.resolve_by_sku(["dup"], configs)
        self.assertEqual(out, {"dup": [("111", "First")]})


class FindConfigByPidTests(unittest.TestCase):
    def test_finds_by_string_or_int_product_id(self) -> None:
        configs = [_config("111", 5), _config(222, 0)]
        self.assertEqual(rsl.find_config_by_pid(configs, "111")["monthlySampleLimit"], 5)
        self.assertEqual(rsl.find_config_by_pid(configs, "222")["monthlySampleLimit"], 0)

    def test_returns_none_when_not_found(self) -> None:
        self.assertIsNone(rsl.find_config_by_pid([_config("111", 5)], "999"))


class CheckRequiredEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = {k: os.environ.get(k) for k in rsl.REQUIRED_ENV}
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_env_raises_system_exit(self) -> None:
        for k in rsl.REQUIRED_ENV:
            os.environ.pop(k, None)
        with self.assertRaises(SystemExit):
            rsl.check_required_env()

    def test_all_present_does_not_raise(self) -> None:
        for k in rsl.REQUIRED_ENV:
            os.environ[k] = "x"
        rsl.check_required_env()  # must not raise


class _StatePathsTestCase(unittest.TestCase):
    """Redirects STATE_PATH/LOG_PATH to a temp dir so writes never touch the
    real overrides file, and restores the module globals afterward."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_state, self._orig_log = rsl.STATE_PATH, rsl.LOG_PATH
        rsl.STATE_PATH = Path(self._tmp.name) / "overrides.json"
        rsl.LOG_PATH = Path(self._tmp.name) / "log.txt"
        self.addCleanup(self._restore_paths)

    def _restore_paths(self) -> None:
        rsl.STATE_PATH, rsl.LOG_PATH = self._orig_state, self._orig_log


class StateRoundTripTests(_StatePathsTestCase):
    def test_load_state_returns_empty_dict_when_no_file(self) -> None:
        self.assertEqual(rsl.load_state(), {})

    def test_save_then_load_round_trips(self) -> None:
        rsl.save_state({"111": {"override_limit": 0}})
        self.assertEqual(rsl.load_state(), {"111": {"override_limit": 0}})

    def test_log_appends_a_timestamped_line_and_also_prints(self) -> None:
        with mock.patch("builtins.print") as p:
            rsl.log("hello world")
        self.assertIn("hello world", rsl.LOG_PATH.read_text())
        p.assert_called_once_with("hello world")


class CmdZeroTests(_StatePathsTestCase):
    def test_dry_run_by_default_writes_no_state_and_makes_no_call(self) -> None:
        with mock.patch.object(rsl, "get_all_configs", return_value=[_config("111", None)]):
            rsl.cmd_zero(Namespace(sku=[], product_id=["111"], limit=0,
                                    reason="selling out", yes=False))
        self.assertFalse(rsl.STATE_PATH.exists())

    @mock.patch("reacher_sample_limits._call")
    def test_yes_creates_a_new_config_when_none_exists(self, call: mock.Mock) -> None:
        call.return_value = {"data": {"id": "new-cfg"}}
        with mock.patch.object(rsl, "get_all_configs", return_value=[]):
            rsl.cmd_zero(Namespace(sku=[], product_id=["111"], limit=0,
                                    reason="selling out", yes=True))
        call.assert_called_once_with("POST", "/samples/product-config",
                                      {"productId": "111", "monthlySampleLimit": 0})
        state = rsl.load_state()
        self.assertEqual(state["111"]["override_limit"], 0)
        self.assertEqual(state["111"]["reason"], "selling out")
        self.assertEqual(state["111"]["config_id"], "new-cfg")

    @mock.patch("reacher_sample_limits._call")
    def test_yes_updates_an_existing_config_via_put(self, call: mock.Mock) -> None:
        existing = _config("111", 100, config_id="cfg-existing")
        call.return_value = {"data": {"id": "cfg-existing"}}
        with mock.patch.object(rsl, "get_all_configs", return_value=[existing]):
            rsl.cmd_zero(Namespace(sku=[], product_id=["111"], limit=0,
                                    reason="selling out", yes=True))
        call.assert_called_once_with("PUT", "/samples/product-config/cfg-existing",
                                      {"monthlySampleLimit": 0})
        self.assertEqual(rsl.load_state()["111"]["previous_limit"], 100)

    def test_no_targets_given_prints_and_does_nothing(self) -> None:
        with mock.patch.object(rsl, "get_all_configs") as get_all:
            rsl.cmd_zero(Namespace(sku=[], product_id=[], limit=0, reason="x", yes=True))
        get_all.assert_not_called()

    @mock.patch("reacher_sample_limits._call")
    def test_a_409_on_create_falls_back_to_updating_the_now_existing_config(self, call: mock.Mock) -> None:
        # First GET (in cmd_zero, via get_all_configs()) sees nothing; POST
        # 409s (a config appeared concurrently); the refetch-and-PUT fallback
        # (another get_all_configs() call, then a PUT) picks it up. Only
        # _call() is mocked here so get_all_configs() runs for real and
        # exercises both of its calls.
        appeared = _config("111", 50, config_id="cfg-race")
        call.side_effect = [
            {"data": []},                                                        # get_all_configs() #1
            RuntimeError("Reacher POST /samples/product-config -> 409: conflict"),  # the POST
            {"data": [appeared]},                                                # get_all_configs() #2
            {"data": {"id": "cfg-race"}},                                        # the fallback PUT
        ]
        rsl.cmd_zero(Namespace(sku=[], product_id=["111"], limit=0,
                                reason="race", yes=True))
        self.assertEqual(rsl.load_state()["111"]["config_id"], "cfg-race")


class CmdResetTests(_StatePathsTestCase):
    def test_all_resets_every_tracked_override(self) -> None:
        rsl.save_state({"111": {"override_limit": 0, "previous_limit": 20, "skus": ["A"]}})
        existing = _config("111", 0, config_id="cfg-1")
        with mock.patch.object(rsl, "get_all_configs", return_value=[existing]), \
             mock.patch("reacher_sample_limits._call") as call:
            rsl.cmd_reset(Namespace(all=True, sku=[], product_id=[], yes=True))
        call.assert_called_once_with("PUT", "/samples/product-config/cfg-1",
                                      {"monthlySampleLimit": None})
        self.assertEqual(rsl.load_state(), {})

    def test_dry_run_does_not_clear_state(self) -> None:
        rsl.save_state({"111": {"override_limit": 0, "previous_limit": 20}})
        existing = _config("111", 0, config_id="cfg-1")
        with mock.patch.object(rsl, "get_all_configs", return_value=[existing]):
            rsl.cmd_reset(Namespace(all=True, sku=[], product_id=[], yes=False))
        self.assertIn("111", rsl.load_state())

    def test_untracked_product_id_still_clears_a_live_cap(self) -> None:
        existing = _config("999", 5, config_id="cfg-untracked")
        with mock.patch.object(rsl, "get_all_configs", return_value=[existing]), \
             mock.patch("reacher_sample_limits._call") as call:
            rsl.cmd_reset(Namespace(all=False, sku=[], product_id=["999"], yes=True))
        call.assert_called_once_with("PUT", "/samples/product-config/cfg-untracked",
                                      {"monthlySampleLimit": None})

    def test_nothing_to_reset_makes_no_call(self) -> None:
        with mock.patch.object(rsl, "get_all_configs") as get_all:
            rsl.cmd_reset(Namespace(all=False, sku=[], product_id=[], yes=True))
        get_all.assert_not_called()


class CmdStatusTests(_StatePathsTestCase):
    def test_empty_state_prints_and_makes_no_call(self) -> None:
        with mock.patch.object(rsl, "get_all_configs") as get_all, \
             mock.patch("builtins.print") as p:
            rsl.cmd_status(Namespace())
        get_all.assert_not_called()
        self.assertTrue(any("No active" in str(c) for c in p.call_args_list))

    def test_drift_is_flagged_when_live_value_no_longer_matches(self) -> None:
        rsl.save_state({"111": {"override_limit": 0, "title": "Widget", "set_at": "t", "reason": "x"}})
        drifted = _config("111", 5)  # live is 5, tool set it to 0
        with mock.patch.object(rsl, "get_all_configs", return_value=[drifted]), \
             mock.patch.object(rsl, "fetch_usage", return_value={}), \
             mock.patch("builtins.print") as p:
            rsl.cmd_status(Namespace())
        self.assertTrue(any("DRIFTED" in str(c) for c in p.call_args_list))


if __name__ == "__main__":
    unittest.main()
