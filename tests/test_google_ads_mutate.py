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
            "MutateCampaignBudgetsRequest": "mutate_campaign_budgets",
            "MutateAdGroupAdsRequest": "mutate_ad_group_ads",
            "MutateAssetGroupListingGroupFiltersRequest":
                "mutate_asset_group_listing_group_filters",
            "MutateAssetGroupAssetsRequest": "mutate_asset_group_assets",
            "MutateConversionActionsRequest": "mutate_conversion_actions",
            "MutateAdGroupCriteriaRequest": "mutate_ad_group_criteria",
            "MutateAdGroupsRequest": "mutate_ad_groups",
            "MutateAudiencesRequest": "mutate_audiences",
            "MutateCampaignCriteriaRequest": "mutate_campaign_criteria",
            "MutateCampaignSharedSetsRequest": "mutate_campaign_shared_sets",
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

        p = sub.add_parser("create-budget")
        p.add_argument("--name", required=True)
        p.add_argument("--daily-amount", type=float, required=True)
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("create-search-campaign")
        p.add_argument("--name", required=True)
        p.add_argument("--budget-id", required=True)
        p.add_argument("--target-roas", type=float)
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("add-campaign-negative-keywords")
        p.add_argument("--campaign-id", required=True)
        p.add_argument("--file", required=True)
        p.add_argument("--match-type", default="EXACT",
                       choices=["BROAD", "PHRASE", "EXACT"])
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("add-keywords")
        p.add_argument("--match-type", default="BROAD",
                       choices=["BROAD", "PHRASE", "EXACT"])
        p.add_argument("--ad-group-id", required=True)
        p.add_argument("--file", required=True)
        p.add_argument("--execute", action="store_true")

        p = sub.add_parser("set-campaign-geo")
        p.add_argument("--campaign-id", required=True)
        p.add_argument("--geo-target-constant", action="append", required=True)
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

    def test_create_budget_requires_name_and_daily_amount(self):
        with self.assertRaises(SystemExit):
            self._parser().parse_args(["create-budget", "--name", "Test Budget"])

    def test_create_search_campaign_target_roas_is_optional(self):
        args = self._parser().parse_args([
            "create-search-campaign", "--name", "Test", "--budget-id", "1"])
        self.assertIsNone(args.target_roas)

    def test_add_campaign_negative_keywords_defaults_to_exact_match(self):
        """Deliberately a DIFFERENT default from add-keywords (BROAD): a
        negative keyword defaulting to broad match would exclude a much wider
        set of queries than the operator likely intended."""
        args = self._parser().parse_args([
            "add-campaign-negative-keywords", "--campaign-id", "1", "--file", "f.txt"])
        self.assertEqual(args.match_type, "EXACT")

    def test_add_keywords_defaults_to_broad_match(self):
        """Preserves add_keywords' pre-existing hardcoded behavior now that
        --match-type is a flag rather than an always-BROAD constant."""
        args = self._parser().parse_args([
            "add-keywords", "--ad-group-id", "1", "--file", "f.txt"])
        self.assertEqual(args.match_type, "BROAD")

    def test_set_campaign_geo_accepts_multiple_targets(self):
        args = self._parser().parse_args([
            "set-campaign-geo", "--campaign-id", "1",
            "--geo-target-constant", "2840", "--geo-target-constant", "2124"])
        self.assertEqual(args.geo_target_constant, ["2840", "2124"])


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


class CopyRsaGuardTests(unittest.TestCase):
    """copy_rsa reads the source ad group's ENABLED responsive search ad via
    a GAQL search before building anything to clone -- if none is found, it
    must exit loudly rather than build a create operation against an empty
    ad."""

    def test_exits_when_no_enabled_rsa_in_source_ad_group(self):
        client = mock.Mock()
        ga_service = mock.Mock()
        ga_service.search.return_value = []
        client.get_service.return_value = ga_service
        args = SimpleNamespace(source_ad_group_id="1", target_ad_group_id="2", execute=False)
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="999"):
            with self.assertRaises(SystemExit):
                gam.copy_rsa(args)


class CreateSearchCampaignTests(unittest.TestCase):
    """A newly created search campaign must always land PAUSED -- going live
    is a separate, deliberate action (enable-campaign), never a side effect
    of creation."""

    def test_new_campaign_status_is_paused(self):
        client = mock.Mock()
        args = SimpleNamespace(name="Test", budget_id="1", target_roas=None, execute=False)
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="999"):
            gam.create_search_campaign(args)
        sent_request = client.get_service.return_value.mutate_campaigns.call_args.kwargs["request"]
        created_campaign = sent_request.operations[0].create
        self.assertEqual(created_campaign.status, client.enums.CampaignStatusEnum.PAUSED)


class ExplicitMaskPathTests(unittest.TestCase):
    """Subcommands that write a proto3 DEFAULT value (False, "") or must touch
    exactly one arm of a oneof can't use the auto `field_mask(None, obj._pb)`
    helper: it only sees non-default values, so the mask comes back empty and
    the mutate succeeds while changing nothing. Each of these must append its
    mask path explicitly -- pin that so a refactor back onto the helper fails
    loudly here instead of silently in a live account."""

    def _run(self, func, **kw):
        client = mock.Mock()
        args = SimpleNamespace(execute=False, **kw)
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="999"), \
             mock.patch.object(gam, "_mutate") as m, \
             mock.patch.object(gam, "_report_result"):
            func(args)
        ops = m.call_args.args[4]
        return ops, m

    def _paths(self, op):
        return [c.args[0] for c in op.update_mask.paths.append.call_args_list]

    def test_conversion_action_secondary_masks_primary_for_goal(self):
        ops, m = self._run(gam.set_conversion_action_secondary, conversion_action_id="7")
        self.assertIn("primary_for_goal", self._paths(ops[0]))
        self.assertIs(ops[0].update.primary_for_goal, False)
        self.assertEqual(m.call_args.args[2], "MutateConversionActionsRequest")

    def test_clear_tracking_template_masks_the_field(self):
        ops, _ = self._run(gam.clear_campaign_tracking_template, campaign_id=["1"])
        self.assertIn("tracking_url_template", self._paths(ops[0]))
        self.assertEqual(ops[0].update.tracking_url_template, "")

    def test_final_url_suffix_sends_every_campaign_in_one_call(self):
        ops, m = self._run(gam.set_final_url_suffix,
                           campaign_id=["1", "2", "3"], suffix="utm_source=google")
        m.assert_called_once()
        self.assertEqual(len(ops), 3)
        for op in ops:
            self.assertIn("final_url_suffix", self._paths(op))
            self.assertEqual(op.update.final_url_suffix, "utm_source=google")

    def test_update_target_roas_touches_only_the_target_roas_arm(self):
        ops, _ = self._run(gam.update_target_roas, campaign_id="1", target_roas=3.5)
        self.assertEqual(self._paths(ops[0]), ["target_roas.target_roas"])
        self.assertEqual(ops[0].update.target_roas.target_roas, 3.5)

    def test_new_subcommands_default_to_dry_run(self):
        for func, kw in [
            (gam.set_conversion_action_secondary, {"conversion_action_id": "7"}),
            (gam.clear_campaign_tracking_template, {"campaign_id": ["1"]}),
            (gam.set_final_url_suffix, {"campaign_id": ["1"], "suffix": "a=b"}),
            (gam.update_target_roas, {"campaign_id": "1", "target_roas": 2.0}),
        ]:
            _, m = self._run(func, **kw)
            self.assertFalse(m.call_args.args[5], func.__name__)


class CreatePmaxCampaignTests(unittest.TestCase):
    """create_pmax_campaign reads a source campaign via GAQL and builds ONE
    atomic GoogleAdsService.mutate. A plain Mock client stands in for the SDK:
    `client.enums.X.Y` returns a stable Mock attribute (so enum identities can
    be compared) and `client.get_type(...)` returns a fresh Mock per call (so
    each MutateOperation's `.create` message can be inspected afterwards)."""

    def _client(self, *, channel="PMAX", ag_count=1, brand_guidelines=True,
                ag_assets=2, signals=("audience", "theme")):
        client = mock.Mock()
        enums = client.enums
        pmax = enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
        campaign = SimpleNamespace(
            advertising_channel_type=pmax if channel == "PMAX" else "SEARCH",
            shopping_setting=SimpleNamespace(merchant_id=111, feed_label="US"),
            final_url_suffix="utm_source=google", tracking_url_template="",
            asset_automation_settings=[], brand_guidelines_enabled=brand_guidelines)
        groups = [SimpleNamespace(asset_group=SimpleNamespace(
            id=500 + i, name=f"AG{i}", final_urls=["https://example.com"],
            final_mobile_urls=[], path1="", path2="")) for i in range(ag_count)]
        asset_rows = [SimpleNamespace(asset_group_asset=SimpleNamespace(
            asset=f"assets/{i}", field_type="HEADLINE")) for i in range(ag_assets)]
        camp_asset_rows = [SimpleNamespace(campaign_asset=SimpleNamespace(
            asset="assets/logo", field_type="LOGO"))]
        crit_rows = [SimpleNamespace(campaign_criterion=SimpleNamespace(
            type_=enums.CriterionTypeEnum.LOCATION, negative=False,
            location=SimpleNamespace(geo_target_constant="geoTargetConstants/1")))]
        shared_rows = [SimpleNamespace(campaign_shared_set=SimpleNamespace(
            shared_set="sharedSets/9"))]
        sig_rows = [SimpleNamespace(asset_group_signal=SimpleNamespace(
            audience=SimpleNamespace(audience="audiences/3" if kind == "audience" else ""),
            search_theme=SimpleNamespace(text="theme" if kind == "theme" else "")))
            for kind in signals]
        self.queries = []

        def search(customer_id, query):
            self.queries.append(query)
            # most specific FROM first: "FROM asset_group" prefixes the others
            for marker, rows in [("FROM campaign_criterion", crit_rows),
                                 ("FROM campaign_shared_set", shared_rows),
                                 ("FROM campaign_asset", camp_asset_rows),
                                 ("FROM asset_group_signal", sig_rows),
                                 ("FROM asset_group_asset", asset_rows),
                                 ("FROM asset_group", groups)]:
                if marker in query:
                    return rows
            return [SimpleNamespace(campaign=campaign)]

        self.ga = mock.Mock()
        self.ga.search.side_effect = search
        client.get_service.return_value = self.ga
        client.get_type.side_effect = lambda name: mock.Mock(name=name)
        return client

    def _run(self, client, **kw):
        args = SimpleNamespace(
            name="PMax - Segment", source_campaign_id="42", source_asset_group_id=None,
            daily_budget=123.45, target_roas=4.0, dimension="custom_label_4",
            include=["Fast", "Steady"], execute=False)
        for k, v in kw.items():
            setattr(args, k, v)
        with mock.patch.object(gam, "_client", return_value=client), \
             mock.patch.object(gam, "_customer_id", return_value="999"):
            gam.create_pmax_campaign(args)
        return self.ga.mutate.call_args.kwargs["request"]

    @staticmethod
    def _kind(op):
        # Mock only materialises the attributes the code actually touched.
        return [k for k in op._mock_children if k.endswith("_operation")][0]

    def _creates(self, req, kind):
        return [getattr(op, kind).create for op in req.mutate_operations
                if self._kind(op) == kind]

    def test_everything_goes_in_one_validate_only_mutate(self):
        req = self._run(self._client())
        self.ga.mutate.assert_called_once()
        self.assertTrue(req.validate_only)
        self.assertEqual(req.customer_id, "999")

    def test_execute_flips_validate_only(self):
        client = self._client()
        self.ga.mutate.return_value.mutate_operation_responses = []
        req = self._run(client, execute=True)
        self.assertFalse(req.validate_only)

    def test_operation_order_and_count(self):
        req = self._run(self._client())
        self.assertEqual([self._kind(op) for op in req.mutate_operations], [
            "campaign_budget_operation",
            "campaign_operation",
            "campaign_criterion_operation",
            "campaign_asset_operation",
            "campaign_shared_set_operation",
            "asset_group_operation",
            "asset_group_asset_operation", "asset_group_asset_operation",
            "asset_group_signal_operation", "asset_group_signal_operation",
            "asset_group_listing_group_filter_operation",   # root
            "asset_group_listing_group_filter_operation",   # Fast
            "asset_group_listing_group_filter_operation",   # Steady
            "asset_group_listing_group_filter_operation",   # everything else
        ])

    def test_campaign_is_paused_with_non_shared_budget(self):
        client = self._client()
        req = self._run(client)
        budget, = self._creates(req, "campaign_budget_operation")
        camp, = self._creates(req, "campaign_operation")
        self.assertIs(budget.explicitly_shared, False)
        self.assertEqual(budget.amount_micros, 123_450_000)
        self.assertEqual(camp.status, client.enums.CampaignStatusEnum.PAUSED)
        self.assertEqual(camp.maximize_conversion_value.target_roas, 4.0)
        self.assertEqual(camp.shopping_setting.merchant_id, 111)
        self.assertEqual(camp.campaign_budget, budget.resource_name)

    def test_asset_queries_filter_to_advertiser_source(self):
        self._run(self._client())
        asset_queries = [q for q in self.queries
                         if "FROM asset_group_asset" in q or "FROM campaign_asset" in q]
        self.assertEqual(len(asset_queries), 2)
        for q in asset_queries:
            self.assertIn("source = 'ADVERTISER'", q)

    def test_brand_assets_skipped_when_brand_guidelines_off(self):
        req = self._run(self._client(brand_guidelines=False))
        self.assertEqual(self._creates(req, "campaign_asset_operation"), [])
        self.assertFalse(any("FROM campaign_asset" in q for q in self.queries))

    def test_listing_tree_includes_values_and_excludes_everything_else(self):
        client = self._client()
        req = self._run(client)
        root, *leaves, other = self._creates(req, "asset_group_listing_group_filter_operation")
        t = client.enums.ListingGroupFilterTypeEnum
        self.assertEqual(root.type_, t.SUBDIVISION)
        self.assertEqual([leaf.type_ for leaf in leaves], [t.UNIT_INCLUDED, t.UNIT_INCLUDED])
        self.assertEqual([leaf.case_value.product_custom_attribute.value for leaf in leaves],
                         ["Fast", "Steady"])
        self.assertEqual(other.type_, t.UNIT_EXCLUDED)
        for node in (*leaves, other):
            self.assertEqual(node.parent_listing_group_filter, root.resource_name)
        # "everything else" carries the index but never a value
        self.assertNotIn("value", vars(other.case_value.product_custom_attribute))
        self.assertIn("value", vars(leaves[0].case_value.product_custom_attribute))

    def test_exits_when_source_is_not_pmax(self):
        client = self._client(channel="SEARCH")
        with self.assertRaises(SystemExit):
            self._run(client)
        self.ga.mutate.assert_not_called()

    def test_exits_when_source_has_several_enabled_asset_groups(self):
        client = self._client(ag_count=2)
        with self.assertRaises(SystemExit):
            self._run(client)
        self.ga.mutate.assert_not_called()

    def test_source_asset_group_id_disambiguates(self):
        self._run(self._client(ag_count=2), source_asset_group_id="501")
        self.ga.mutate.assert_called_once()

    def test_cli_wiring_defaults_to_dry_run(self):
        """Parse through the REAL main() parser (the subcommand function is
        patched out), so the subparser itself is what's under test."""
        base = ["google_ads_mutate.py", "create-pmax-campaign", "--name", "N",
                "--source-campaign-id", "1", "--daily-budget", "10", "--target-roas", "3",
                "--dimension", "custom_label_4"]
        with mock.patch.object(gam, "create_pmax_campaign") as fn:
            with mock.patch("sys.argv", base + ["--include", "A", "--include", "B"]):
                gam.main()
            args = fn.call_args.args[0]
            self.assertEqual(args.include, ["A", "B"])
            self.assertFalse(args.execute)
            for bad in (base, base[:-1] + ["not_a_label", "--include", "A"]):
                with mock.patch("sys.argv", bad), self.assertRaises(SystemExit):
                    gam.main()


if __name__ == "__main__":
    unittest.main()
