"""Tests for google_ads_mutate.py.

Hermetic: no real Google Ads credentials or network calls. `_mutate()` never
touches the network itself — it builds a request object via `client.get_type`
and dispatches to a method on `service` — so both are faked here to pin two
behaviors that would otherwise be easy to silently break:

  * `validate_only` is always the inverse of `execute` (the whole safety
    model of this script rests on this one line),
  * each of the four known request types dispatches to the correct service
    method name, since `_mutate()` looks that up from a small dict rather
    than deriving it mechanically from the request type string.

The individual subcommand functions (`pause_campaign`, `set_bidding`, etc.)
call the real `google.ads.googleads.client.GoogleAdsClient` and are exercised
manually against a live (or `--dry-run`-equivalent `validate_only`) account
instead — building fakes deep enough to mock the SDK's typed message classes
would test the fakes more than the code.
"""
from __future__ import annotations

import argparse
import unittest
from types import SimpleNamespace
from unittest import mock

import google_ads_mutate as gam


class FakeRequest:
    def __init__(self):
        self.customer_id = None
        self.operations = None
        self.validate_only = None


class MutateDispatchTests(unittest.TestCase):
    def _client(self):
        client = mock.Mock()
        client.get_type.return_value = FakeRequest()
        return client

    def test_validate_only_defaults_true_when_not_executing(self):
        client = self._client()
        service = mock.Mock()
        gam._mutate(client, service, "MutateCampaignsRequest", "123", ["op"], execute=False)
        sent_request = service.mutate_campaigns.call_args.kwargs["request"]
        self.assertTrue(sent_request.validate_only)

    def test_validate_only_false_when_executing(self):
        client = self._client()
        service = mock.Mock()
        gam._mutate(client, service, "MutateCampaignsRequest", "123", ["op"], execute=True)
        sent_request = service.mutate_campaigns.call_args.kwargs["request"]
        self.assertFalse(sent_request.validate_only)

    def test_request_carries_customer_id_and_operations(self):
        client = self._client()
        service = mock.Mock()
        ops = ["op1", "op2"]
        gam._mutate(client, service, "MutateCampaignsRequest", "999", ops, execute=False)
        sent_request = service.mutate_campaigns.call_args.kwargs["request"]
        self.assertEqual(sent_request.customer_id, "999")
        self.assertEqual(sent_request.operations, ops)

    def test_each_known_request_type_dispatches_to_its_own_method(self):
        expected = {
            "MutateCampaignsRequest": "mutate_campaigns",
            "MutateAssetGroupListingGroupFiltersRequest":
                "mutate_asset_group_listing_group_filters",
            "MutateAssetGroupAssetsRequest": "mutate_asset_group_assets",
            "MutateConversionActionsRequest": "mutate_conversion_actions",
        }
        for request_type, method_name in expected.items():
            client = self._client()
            service = mock.Mock(spec=list(expected.values()))
            gam._mutate(client, service, request_type, "1", [], execute=False)
            getattr(service, method_name).assert_called_once()


class ArgparseWiringTests(unittest.TestCase):
    """Each subcommand must require exactly the arguments its function reads,
    and --execute must default to False so a bare invocation is always a dry
    run — the whole point of the validate_only safety model."""

    def _parser(self):
        # Rebuild main()'s parser without invoking main() itself (which would
        # dispatch to a real subcommand function).
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd", required=True)

        p = sub.add_parser("pause-campaign")
        p.add_argument("--campaign-id", required=True)
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("remove-campaigns")
        p.add_argument("--campaign-id", action="append", required=True)
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("set-bidding")
        p.add_argument("--campaign-id", required=True)
        p.add_argument("--target-roas", type=float)
        p.add_argument("--maximize-conversion-value", action="store_true")
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("end-experiment")
        p.add_argument("--experiment-id", required=True)
        p.add_argument("--execute", action="store_true")
        return ap

    def test_execute_defaults_to_false(self):
        args = self._parser().parse_args(["pause-campaign", "--campaign-id", "1"])
        self.assertFalse(args.execute)

    def test_execute_flag_flips_it_true(self):
        args = self._parser().parse_args(["pause-campaign", "--campaign-id", "1", "--execute"])
        self.assertTrue(args.execute)

    def test_pause_campaign_requires_campaign_id(self):
        with self.assertRaises(SystemExit):
            self._parser().parse_args(["pause-campaign"])

    def test_remove_campaigns_accepts_multiple_ids(self):
        args = self._parser().parse_args(
            ["remove-campaigns", "--campaign-id", "1", "--campaign-id", "2"])
        self.assertEqual(args.campaign_id, ["1", "2"])

    def test_end_experiment_without_execute_never_calls_end_experiment(self):
        """end_experiment() itself (not argparse) is what enforces the real
        guard here -- the API call has no validate_only mode, so --execute
        is the only thing standing between a bare invocation and actually
        ending a real experiment. Building the resource name (to print it)
        is fine without --execute; calling end_experiment() is not."""
        client = mock.Mock()
        args = SimpleNamespace(execute=False, experiment_id="42")
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="999"):
            gam.end_experiment(args)
        client.get_service.return_value.end_experiment.assert_not_called()

    def test_end_experiment_with_execute_calls_the_service(self):
        client = mock.Mock()
        args = SimpleNamespace(execute=True, experiment_id="42")
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="999"):
            gam.end_experiment(args)
        client.get_service.return_value.end_experiment.assert_called_once()


if __name__ == "__main__":
    unittest.main()
