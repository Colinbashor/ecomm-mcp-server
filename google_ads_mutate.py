r"""
Google Ads MUTATE operations — the write-capable counterpart to the
read-only connectors in this repo.

Every other Google Ads file here (`warehouse/connectors/google_ads.py`,
`google_ads_detail_sync.py`, `google_ads_structure_sync.py`) only issues
`search`/`searchStream` GAQL queries. This one sends `mutate` requests, so it
can actually change things in the live account: pause or remove a campaign,
change its bidding strategy, edit a Performance Max asset group's listing-
group filter tree, or end a Campaign Experiment.

WHY THIS IS SAFE TO RUN EXPLORATORILY: every mutate call in this module
defaults to `validate_only=True` — the API runs FULL SERVER-SIDE VALIDATION
against the live account (real resource IDs, real policy checks) and returns
real errors, but commits NOTHING. Only `--execute` flips `validate_only=False`
and actually posts the change. Always run without `--execute` first, read the
result, THEN re-run with `--execute` once it validates clean.

OAuth scope is `https://www.googleapis.com/auth/adwords` (see `google_auth.py`)
— the Google Ads API has exactly one scope, covering both read and write, so
nothing about the credential itself needs to change to add write capability.
The account-level permission tier (Read-only / Standard / Admin — a
*different* axis from OAuth scope) does need to be Standard or Admin for
mutate calls to succeed even in `validate_only` mode.

USAGE
  python google_ads_mutate.py pause-campaign --campaign-id 18373650912
  python google_ads_mutate.py pause-campaign --campaign-id 18373650912 --execute

  python google_ads_mutate.py set-bidding --campaign-id 20593969582 --target-roas 2.5
  python google_ads_mutate.py set-bidding --campaign-id 20593969582 --maximize-conversion-value

  python google_ads_mutate.py replace-filter --campaign-id 20593969582 \
      --asset-group-id 6477859796 --remove-filter-id 11195896515 \
      --dimension custom_label_0 --value "Winter - Proven Seller" --parent-id 11195894994

  python google_ads_mutate.py build-tier-subdivision --campaign-id 22001500480 \
      --asset-group-id 6536885353 --remove-filter-id 12163354837 \
      --dimension custom_label_0 --include "A - Hero" --include "B - Scale up"
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

CUSTOM_LABEL_INDEX = {
    "custom_label_0": "INDEX0",
    "custom_label_1": "INDEX1",
    "custom_label_2": "INDEX2",
    "custom_label_3": "INDEX3",
    "custom_label_4": "INDEX4",
}


def _client():
    from google.ads.googleads.client import GoogleAdsClient

    return GoogleAdsClient.load_from_dict({
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        "use_proto_plus": True,
    })


def _customer_id() -> str:
    return os.environ["GOOGLE_ADS_CUSTOMER_ID"]


def _report_result(resp, execute: bool, service_name: str):
    if execute:
        print(f"EXECUTED — {service_name}. Resource(s) changed:")
        for result in resp.results:
            print(f"  {result.resource_name}")
    else:
        print(f"VALIDATE_ONLY passed — {service_name} would succeed with no errors. "
              f"Re-run with --execute to actually make this change.")


def _report_failure(ex):
    print("VALIDATION FAILED — the API rejected this request. Details:")
    for error in ex.failure.errors:
        print(f"  [{error.error_code}] {error.message}")
        if error.location and error.location.field_path_elements:
            path = " -> ".join(f.field_name for f in error.location.field_path_elements)
            print(f"      field: {path}")
    sys.exit(1)


def _mutate(client, service, request_type: str, customer_id: str, operations: list, execute: bool):
    """Build the request object explicitly and pass it via `request=` --
    `validate_only` is a REQUEST field, not an accepted kwarg on the service
    method itself in this client library version (verified: passing it as a
    kwarg raises TypeError)."""
    req = client.get_type(request_type)
    req.customer_id = customer_id
    req.operations = operations
    req.validate_only = not execute
    method = getattr(service, {
        "MutateCampaignsRequest": "mutate_campaigns",
        "MutateAssetGroupListingGroupFiltersRequest": "mutate_asset_group_listing_group_filters",
        "MutateAssetGroupAssetsRequest": "mutate_asset_group_assets",
        "MutateConversionActionsRequest": "mutate_conversion_actions",
    }[request_type])
    return method(request=req)


def pause_campaign(args):
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    campaign_service = client.get_service("CampaignService")
    campaign_op = client.get_type("CampaignOperation")
    campaign = campaign_op.update
    campaign.resource_name = campaign_service.campaign_path(_customer_id(), args.campaign_id)
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    from google.api_core.protobuf_helpers import field_mask
    campaign_op.update_mask.CopyFrom(field_mask(None, campaign._pb))

    try:
        resp = _mutate(client, campaign_service, "MutateCampaignsRequest",
                       _customer_id(), [campaign_op], args.execute)
        _report_result(resp, args.execute, "pause campaign")
    except GoogleAdsException as ex:
        _report_failure(ex)


def remove_campaigns(args):
    """Permanently remove one or more campaigns (CampaignOperation.remove).
    IRREVERSIBLE -- Ads has no "undo" for a removed campaign. All operations
    go in ONE mutate call so validate_only checks the whole batch atomically
    before anything executes."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    campaign_service = client.get_service("CampaignService")
    ops = []
    for cid in args.campaign_id:
        op = client.get_type("CampaignOperation")
        op.remove = campaign_service.campaign_path(_customer_id(), cid)
        ops.append(op)

    try:
        resp = _mutate(client, campaign_service, "MutateCampaignsRequest",
                       _customer_id(), ops, args.execute)
        _report_result(resp, args.execute, f"remove {len(ops)} campaign(s)")
    except GoogleAdsException as ex:
        _report_failure(ex)


def set_bidding(args):
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException
    from google.api_core.protobuf_helpers import field_mask

    campaign_service = client.get_service("CampaignService")
    campaign_op = client.get_type("CampaignOperation")
    campaign = campaign_op.update
    campaign.resource_name = campaign_service.campaign_path(_customer_id(), args.campaign_id)

    if args.target_roas is not None:
        campaign.maximize_conversion_value.target_roas = args.target_roas
    elif args.maximize_conversion_value:
        # Assigning an empty MaximizeConversionValue selects that bidding
        # strategy with target_roas left UNSET -> runs uncapped, per Google's
        # own semantics for this field (no target = no ROAS ceiling).
        campaign.maximize_conversion_value = client.get_type("MaximizeConversionValue")()
    else:
        print("Specify --target-roas X or --maximize-conversion-value")
        sys.exit(2)

    campaign_op.update_mask.CopyFrom(field_mask(None, campaign._pb))

    try:
        resp = _mutate(client, campaign_service, "MutateCampaignsRequest",
                       _customer_id(), [campaign_op], args.execute)
        _report_result(resp, args.execute, "update bidding strategy")
    except GoogleAdsException as ex:
        _report_failure(ex)


def _listing_group_service(client):
    return client.get_service("AssetGroupListingGroupFilterService")


def _agf_path(client, customer_id, asset_group_id, filter_id):
    return client.get_service("AssetGroupListingGroupFilterService").asset_group_listing_group_filter_path(
        customer_id, asset_group_id, filter_id)


def replace_filter(args):
    """Remove one existing UNIT_INCLUDED filter and add a new one under the
    same parent SUBDIVISION — e.g. swapping which product attribute value a
    Performance Max listing group targets."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = _listing_group_service(client)
    customer_id = _customer_id()
    ops = []

    remove_op = client.get_type("AssetGroupListingGroupFilterOperation")
    remove_op.remove = _agf_path(client, customer_id, args.asset_group_id, args.remove_filter_id)
    ops.append(remove_op)

    add_op = client.get_type("AssetGroupListingGroupFilterOperation")
    node = add_op.create
    node.asset_group = client.get_service("AssetGroupService").asset_group_path(
        customer_id, args.asset_group_id)
    node.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
    node.listing_source = client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
    node.parent_listing_group_filter = _agf_path(client, customer_id, args.asset_group_id, args.parent_id)
    dim = node.case_value.product_custom_attribute
    dim.index = getattr(client.enums.ListingGroupFilterCustomAttributeIndexEnum,
                         CUSTOM_LABEL_INDEX[args.dimension])
    dim.value = args.value
    ops.append(add_op)

    try:
        resp = _mutate(client, svc, "MutateAssetGroupListingGroupFiltersRequest",
                       customer_id, ops, args.execute)
        _report_result(resp, args.execute, "replace listing group filter")
    except GoogleAdsException as ex:
        _report_failure(ex)


def build_tier_subdivision(args):
    """Remove the old flat UNIT_INCLUDED('everything') filter and build:
        SUBDIVISION (root)
        ├── dimension = include[0]  -> UNIT_INCLUDED
        ├── dimension = include[1]  -> UNIT_INCLUDED
        ├── ...
        └── (no value, "everything else") -> UNIT_EXCLUDED
    All in ONE mutate call so the parent-child temp IDs resolve correctly —
    the Ads UI would need this as two passes (create the subdivision, then
    read back its new ID for the children); the API lets you reference a
    not-yet-created parent via a negative temporary resource id in the SAME
    request.
    """
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = _listing_group_service(client)
    customer_id = _customer_id()
    asset_group_path = client.get_service("AssetGroupService").asset_group_path(
        customer_id, args.asset_group_id)
    ops = []

    remove_op = client.get_type("AssetGroupListingGroupFilterOperation")
    remove_op.remove = _agf_path(client, customer_id, args.asset_group_id, args.remove_filter_id)
    ops.append(remove_op)

    TEMP_ROOT_ID = "-1"
    root_op = client.get_type("AssetGroupListingGroupFilterOperation")
    root = root_op.create
    root.asset_group = asset_group_path
    root.type_ = client.enums.ListingGroupFilterTypeEnum.SUBDIVISION
    root.listing_source = client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
    root.resource_name = svc.asset_group_listing_group_filter_path(
        customer_id, args.asset_group_id, TEMP_ROOT_ID)
    ops.append(root_op)

    for value in args.include:
        child_op = client.get_type("AssetGroupListingGroupFilterOperation")
        child = child_op.create
        child.asset_group = asset_group_path
        child.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
        child.listing_source = client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
        child.parent_listing_group_filter = svc.asset_group_listing_group_filter_path(
            customer_id, args.asset_group_id, TEMP_ROOT_ID)
        dim = child.case_value.product_custom_attribute
        dim.index = getattr(client.enums.ListingGroupFilterCustomAttributeIndexEnum,
                             CUSTOM_LABEL_INDEX[args.dimension])
        dim.value = value
        ops.append(child_op)

    other_op = client.get_type("AssetGroupListingGroupFilterOperation")
    other = other_op.create
    other.asset_group = asset_group_path
    other.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
    other.listing_source = client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
    other.parent_listing_group_filter = svc.asset_group_listing_group_filter_path(
        customer_id, args.asset_group_id, TEMP_ROOT_ID)
    # The catch-all still needs the dimension's INDEX set (verified,
    # REQUIRED) -- just no .value, which is what makes it "everything else".
    other.case_value.product_custom_attribute.index = getattr(
        client.enums.ListingGroupFilterCustomAttributeIndexEnum, CUSTOM_LABEL_INDEX[args.dimension])
    ops.append(other_op)

    try:
        resp = _mutate(client, svc, "MutateAssetGroupListingGroupFiltersRequest",
                       customer_id, ops, args.execute)
        _report_result(resp, args.execute, "build tier subdivision")
    except GoogleAdsException as ex:
        _report_failure(ex)


def end_experiment(args):
    """End a Campaign Experiment (`ExperimentService.end_experiment`) — the
    correct way to conclude a trial campaign; you cannot edit a trial
    campaign's status/budget/dates directly via CampaignService (verified:
    it 400s with CANNOT_MODIFY_FOR_TRIAL_CAMPAIGN).

    NOTE: end_experiment does not accept validate_only -- this call is NOT
    dry-runnable through the API itself. It DOES require --execute here as an
    extra guard, but the first real signal you'll get is the call actually
    ending it. Confirm the experiment id/status via a GAQL read first (see
    the `experiment` resource) before running with --execute."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = client.get_service("ExperimentService")
    resource_name = svc.experiment_path(_customer_id(), args.experiment_id)

    if not args.execute:
        print(f"Would call ExperimentService.end_experiment on {resource_name}.")
        print("This call has NO validate_only mode -- re-run with --execute to actually end it.")
        return

    try:
        svc.end_experiment(experiment=resource_name)
        print(f"EXECUTED — ended experiment {resource_name}.")
    except GoogleAdsException as ex:
        _report_failure(ex)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pause-campaign")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=pause_campaign)

    p = sub.add_parser("remove-campaigns")
    p.add_argument("--campaign-id", action="append", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=remove_campaigns)

    p = sub.add_parser("end-experiment")
    p.add_argument("--experiment-id", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=end_experiment)

    p = sub.add_parser("set-bidding")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--target-roas", type=float)
    p.add_argument("--maximize-conversion-value", action="store_true")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=set_bidding)

    p = sub.add_parser("replace-filter")
    p.add_argument("--campaign-id", required=True)  # unused directly but kept for clarity/logging
    p.add_argument("--asset-group-id", required=True)
    p.add_argument("--remove-filter-id", required=True)
    p.add_argument("--parent-id", required=True)
    p.add_argument("--dimension", required=True, choices=CUSTOM_LABEL_INDEX)
    p.add_argument("--value", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=replace_filter)

    p = sub.add_parser("build-tier-subdivision")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--asset-group-id", required=True)
    p.add_argument("--remove-filter-id", required=True)
    p.add_argument("--dimension", required=True, choices=CUSTOM_LABEL_INDEX)
    p.add_argument("--include", action="append", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=build_tier_subdivision)

    args = ap.parse_args()
    if not args.execute:
        print("(validate_only mode — no changes will be made; pass --execute to apply)\n")
    args.func(args)


if __name__ == "__main__":
    main()
