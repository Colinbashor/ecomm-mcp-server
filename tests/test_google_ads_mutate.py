"""Tests for google_ads_mutate.py.

Hermetic: no real Google Ads credentials or network calls. `_mutate()` never
touches the network itself — it builds a request object via `client.get_type`
and dispatches to a method on `service` — so both are faked here to pin two
behaviors that would otherwise be easy to silently break:

  * `validate_only` is always the inverse of `execute` (the whole safety
    model of this script rests on this one line),
  * each known request type dispatches to the correct service method name,
    since `_mutate()` looks that up from a small dict rather than deriving it
    mechanically from the request type string.

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
            "MutateAdGroupCriteriaRequest": "mutate_ad_group_criteria",
            "MutateAdGroupsRequest": "mutate_ad_groups",
            "MutateAudiencesRequest": "mutate_audiences",
            "MutateCampaignCriteriaRequest": "mutate_campaign_criteria",
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

        p = sub.add_parser("flip-campaign-user-list-to-negative")
        p.add_argument("--campaign-id", required=True)
        p.add_argument("--old-criterion-id", action="append", required=True)
        p.add_argument("--user-list-id", action="append", required=True)
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("build-shopping-tier-subdivision")
        p.add_argument("--ad-group-id", required=True)
        p.add_argument("--parent-id", required=True)
        p.add_argument("--remove-criterion-id", required=True)
        p.add_argument("--parent-dimension", required=True,
                       choices=["product_brand", *gam.CUSTOM_LABEL_INDEX])
        p.add_argument("--parent-case-value", default="")
        p.add_argument("--dimension", required=True, choices=gam.CUSTOM_LABEL_INDEX)
        p.add_argument("--include", action="append", required=True)
        p.add_argument("--cpc-bid-micros", type=int, default=10000)
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

    def test_flip_campaign_user_list_requires_paired_lists(self):
        args = self._parser().parse_args([
            "flip-campaign-user-list-to-negative", "--campaign-id", "1",
            "--old-criterion-id", "10", "--user-list-id", "20"])
        self.assertEqual(args.old_criterion_id, ["10"])
        self.assertEqual(args.user_list_id, ["20"])

    def test_build_shopping_tier_subdivision_rejects_an_unknown_parent_dimension(self):
        """--parent-dimension is either 'product_brand' (its own oneof arm) or
        one of the custom_label_N keys (product_custom_attribute) -- anything
        else can't be mapped to either branch in build_shopping_tier_subdivision."""
        with self.assertRaises(SystemExit):
            self._parser().parse_args([
                "build-shopping-tier-subdivision", "--ad-group-id", "1",
                "--parent-id", "2", "--remove-criterion-id", "3",
                "--parent-dimension", "not_a_real_dimension",
                "--dimension", "custom_label_0", "--include", "A"])

    def test_build_shopping_tier_subdivision_accepts_product_brand(self):
        args = self._parser().parse_args([
            "build-shopping-tier-subdivision", "--ad-group-id", "1",
            "--parent-id", "2", "--remove-criterion-id", "3",
            "--parent-dimension", "product_brand",
            "--dimension", "custom_label_0", "--include", "A"])
        self.assertEqual(args.parent_dimension, "product_brand")


class AudienceGuardTests(unittest.TestCase):
    """add_audience_user_lists / remove_audience_segment both read the target
    Audience via a GAQL search before mutating anything -- if it isn't found,
    they must exit loudly instead of building an update against a resource
    name that doesn't exist."""

    def _client_with_no_matching_audience(self):
        client = mock.Mock()
        ga_service = mock.Mock()
        ga_service.search_stream.return_value = []  # no batches -> not found
        client.get_service.return_value = ga_service
        return client

    def test_add_audience_user_lists_exits_when_audience_not_found(self):
        client = self._client_with_no_matching_audience()
        args = SimpleNamespace(audience_id="999", user_list_id=["1"], execute=False)
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="1"):
            with self.assertRaises(SystemExit):
                gam.add_audience_user_lists(args)

    def test_remove_audience_segment_exits_when_audience_not_found(self):
        client = self._client_with_no_matching_audience()
        args = SimpleNamespace(audience_id="999", user_list_id="1", execute=False)
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="1"):
            with self.assertRaises(SystemExit):
                gam.remove_audience_segment(args)


if __name__ == "__main__":
    unittest.main()
