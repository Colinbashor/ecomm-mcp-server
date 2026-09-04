r"""
Google Ads MUTATE operations — the write-capable counterpart to the
read-only connectors in this repo.

Every other Google Ads file here (`warehouse/connectors/google_ads.py`,
`google_ads_detail_sync.py`, `google_ads_structure_sync.py`) only issues
`search`/`searchStream` GAQL queries. This one sends `mutate` requests, so it
can actually change things in the live account: pause, remove, or retarget a
campaign or ad group, change a campaign's bidding strategy or Target
Impression Share ceiling, restrict a Shopping/PMax campaign to one Merchant
Center feed label, edit a Performance Max asset group's listing-group filter
tree (including building a fresh multi-tier subdivision, or the equivalent
for a *standard* Shopping campaign's `AdGroupCriterion.listing_group` tree —
a related but genuinely different object/service/enum from PMax's), add or
remove keywords, add or remove `user_list` (RLSA) audience criteria on a
campaign (including converting an existing positive criterion into a
negative/exclusion one), edit an `Audience` resource's segment membership, or
end a Campaign Experiment.

EVERY SUBCOMMAND HAS ITS OWN DOCSTRING on its function explaining what it
does and any API quirk it works around — read that before using an unfamiliar
one. Several of the listing-group-tree and audience functions in particular
exist because of specific, non-obvious API behavior discovered by trial and
error (an `IMMUTABLE_FIELD` error on flipping a criterion's `negative` flag
in place, a required-but-must-be-empty `oneof` selection for a subdivision's
catch-all node, two same-named-but-different enum types depending on which
object you're setting a custom attribute index on) — those comments are
worth reading verbatim rather than skimming past, since each was found by a
live 400 error, not documented anywhere by Google.

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
        "MutateAdGroupCriteriaRequest": "mutate_ad_group_criteria",
        "MutateAdGroupsRequest": "mutate_ad_groups",
        "MutateAudiencesRequest": "mutate_audiences",
        "MutateCampaignCriteriaRequest": "mutate_campaign_criteria",
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


def add_campaign_negative_user_list(args):
    """Exclude a `user_list` (existing customers, RLSA-style) from a
    Search/Shopping campaign via `CampaignCriterion.negative`.

    NOTE: this does NOT work for Performance Max — PMax has no negative
    audience/user-list targeting at all, only positive audience SIGNALS. This
    function will validate-fail if pointed at a PMax campaign; that failure is
    a platform limit, not a bug here."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = client.get_service("CampaignCriterionService")
    campaign_path = client.get_service("CampaignService").campaign_path(
        _customer_id(), args.campaign_id)
    ops = []
    for ul_id in args.user_list_id:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_path
        crit.negative = True
        crit.user_list.user_list = client.get_service("UserListService").user_list_path(
            _customer_id(), ul_id)
        ops.append(op)

    try:
        resp = _mutate(client, svc, "MutateCampaignCriteriaRequest",
                       _customer_id(), ops, args.execute)
        _report_result(resp, args.execute,
                       f"exclude {len(ops)} user list(s) from campaign {args.campaign_id}")
    except GoogleAdsException as ex:
        _report_failure(ex)


def flip_campaign_user_list_to_negative(args):
    """Convert an EXISTING positive (observation) `user_list` campaign
    criterion into a negative (exclusion) one.

    Google rejects a plain create of a negative criterion for a user_list
    that already exists as a positive criterion on the same campaign
    (`IMMUTABLE_FIELD` on `negative` — it treats the campaign+user_list pair
    as the criterion's identity, and flipping the flag in place reads as an
    illegal update-via-create). The fix is remove the old criterion + create
    a fresh negative one, in ONE atomic batch — the same remove-then-create
    pattern `replace_filter` already uses for listing-group filters."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = client.get_service("CampaignCriterionService")
    campaign_path = client.get_service("CampaignService").campaign_path(
        _customer_id(), args.campaign_id)
    ops = []
    for old_id, ul_id in zip(args.old_criterion_id, args.user_list_id):
        remove_op = client.get_type("CampaignCriterionOperation")
        remove_op.remove = svc.campaign_criterion_path(_customer_id(), args.campaign_id, old_id)
        ops.append(remove_op)

        create_op = client.get_type("CampaignCriterionOperation")
        crit = create_op.create
        crit.campaign = campaign_path
        crit.negative = True
        crit.user_list.user_list = client.get_service("UserListService").user_list_path(
            _customer_id(), ul_id)
        ops.append(create_op)

    try:
        resp = _mutate(client, svc, "MutateCampaignCriteriaRequest",
                       _customer_id(), ops, args.execute)
        _report_result(resp, args.execute,
                       f"flip {len(args.old_criterion_id)} user list criteria to negative "
                       f"on campaign {args.campaign_id}")
    except GoogleAdsException as ex:
        _report_failure(ex)


def add_audience_user_lists(args):
    """Add one or more `user_list` segments to an EXISTING Audience's
    `audience_segments` dimension, preserving every dimension already on it.

    `Audience.dimensions` is a repeated field, so an update REPLACES the
    whole list wholesale under a field mask — there is no server-side
    "append". This reads the audience first, copies every existing dimension
    byte for byte, appends the new user_list segments to whichever dimension
    already holds `audience_segments` (or adds a fresh one if none exists),
    and writes the complete result back. Never touches age/gender/interest/
    custom-audience dimensions that are already there."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    ga_service = client.get_service("GoogleAdsService")
    customer_id = _customer_id()
    q = f"""SELECT audience.resource_name, audience.dimensions
            FROM audience WHERE audience.id = {args.audience_id}"""
    existing = None
    for batch in ga_service.search_stream(customer_id=customer_id, query=q):
        for r in batch.results:
            existing = r.audience
    if existing is None:
        print(f"Audience {args.audience_id} not found.")
        sys.exit(2)

    svc = client.get_service("AudienceService")
    op = client.get_type("AudienceOperation")
    aud = op.update
    aud.resource_name = existing.resource_name

    ul_paths = [client.get_service("UserListService").user_list_path(customer_id, i)
                for i in args.user_list_id]

    # proto-plus repeated MESSAGE fields don't support .add() -- each element
    # must be a fully-built standalone message appended via .append().
    found_segments_dim = False
    new_dims = []
    for dim in existing.dimensions:
        new_dim = client.get_type("AudienceDimension")
        new_dim._pb.CopyFrom(dim._pb)
        if "audience_segments" in dim:
            found_segments_dim = True
            for path in ul_paths:
                seg = client.get_type("AudienceSegment")
                seg.user_list.user_list = path
                new_dim.audience_segments.segments.append(seg)
        new_dims.append(new_dim)
    if not found_segments_dim:
        new_dim = client.get_type("AudienceDimension")
        for path in ul_paths:
            seg = client.get_type("AudienceSegment")
            seg.user_list.user_list = path
            new_dim.audience_segments.segments.append(seg)
        new_dims.append(new_dim)

    aud.dimensions.extend(new_dims)
    op.update_mask.paths.append("dimensions")

    try:
        resp = _mutate(client, svc, "MutateAudiencesRequest", customer_id, [op], args.execute)
        _report_result(resp, args.execute,
                       f"add {len(args.user_list_id)} user list(s) to audience {args.audience_id}")
    except GoogleAdsException as ex:
        _report_failure(ex)


def remove_audience_segment(args):
    """Remove ONE segment (matched by its user_list id) from an Audience's
    `audience_segments` dimension, preserving everything else — the
    counterpart to `add_audience_user_lists`, for dropping a dead/orphaned
    seed list."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    ga_service = client.get_service("GoogleAdsService")
    customer_id = _customer_id()
    q = f"""SELECT audience.resource_name, audience.dimensions
            FROM audience WHERE audience.id = {args.audience_id}"""
    existing = None
    for batch in ga_service.search_stream(customer_id=customer_id, query=q):
        for r in batch.results:
            existing = r.audience
    if existing is None:
        print(f"Audience {args.audience_id} not found.")
        sys.exit(2)

    target_path = client.get_service("UserListService").user_list_path(
        customer_id, args.user_list_id)

    svc = client.get_service("AudienceService")
    op = client.get_type("AudienceOperation")
    aud = op.update
    aud.resource_name = existing.resource_name

    removed = 0
    new_dims = []
    for dim in existing.dimensions:
        new_dim = client.get_type("AudienceDimension")
        new_dim._pb.CopyFrom(dim._pb)
        if "audience_segments" in dim:
            kept = [s for s in dim.audience_segments.segments
                    if not ("user_list" in s and s.user_list.user_list == target_path)]
            removed += len(dim.audience_segments.segments) - len(kept)
            del new_dim.audience_segments.segments[:]
            for s in kept:
                copy = client.get_type("AudienceSegment")
                copy._pb.CopyFrom(s._pb)
                new_dim.audience_segments.segments.append(copy)
        new_dims.append(new_dim)

    if not removed:
        print(f"user_list {args.user_list_id} was not found on audience "
              f"{args.audience_id} -- nothing to remove.")
        sys.exit(2)

    aud.dimensions.extend(new_dims)
    op.update_mask.paths.append("dimensions")

    try:
        resp = _mutate(client, svc, "MutateAudiencesRequest", customer_id, [op], args.execute)
        _report_result(resp, args.execute,
                       f"remove user list {args.user_list_id} from audience {args.audience_id}")
    except GoogleAdsException as ex:
        _report_failure(ex)


def set_shopping_feed_label(args):
    """Lock a Shopping/PMax campaign to ONE Merchant Center `feed_label`, so
    it can only ever source products from that specific data source — matters
    when the Merchant Center account has multiple overlapping product feeds
    (e.g. more than one connector/app feeding the same account) and an
    unrestricted campaign could otherwise draw from any of them, silently
    bypassing whatever custom-attribute-based listing-group rules you've
    built against a specific feed."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException
    from google.api_core.protobuf_helpers import field_mask

    campaign_service = client.get_service("CampaignService")
    campaign_op = client.get_type("CampaignOperation")
    campaign = campaign_op.update
    campaign.resource_name = campaign_service.campaign_path(_customer_id(), args.campaign_id)
    campaign.shopping_setting.feed_label = args.feed_label
    campaign_op.update_mask.CopyFrom(field_mask(None, campaign._pb))

    try:
        resp = _mutate(client, campaign_service, "MutateCampaignsRequest",
                       _customer_id(), [campaign_op], args.execute)
        _report_result(resp, args.execute, "set Shopping feed_label")
    except GoogleAdsException as ex:
        _report_failure(ex)


def set_tis_ceiling(args):
    """Raise/lower the max CPC bid ceiling on a Target Impression Share
    campaign. This does NOT increase spend on its own — the campaign's daily
    budget is a separate, still-enforced cap. It only lets the bid strategy
    compete more aggressively for the SAME already-allocated budget, which
    matters when a campaign is under-spending its own budget purely because
    its ceiling is set too low to win auctions."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException
    from google.api_core.protobuf_helpers import field_mask

    campaign_service = client.get_service("CampaignService")
    campaign_op = client.get_type("CampaignOperation")
    campaign = campaign_op.update
    campaign.resource_name = campaign_service.campaign_path(_customer_id(), args.campaign_id)
    campaign.target_impression_share.cpc_bid_ceiling_micros = int(args.ceiling * 1_000_000)
    campaign_op.update_mask.CopyFrom(field_mask(None, campaign._pb))

    try:
        resp = _mutate(client, campaign_service, "MutateCampaignsRequest",
                       _customer_id(), [campaign_op], args.execute)
        _report_result(resp, args.execute, "update Target Impression Share CPC ceiling")
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
    """Remove one existing filter and build in its place:
        SUBDIVISION (same slot as the removed filter)
        ├── dimension = include[0]  -> UNIT_INCLUDED
        ├── dimension = include[1]  -> UNIT_INCLUDED
        ├── ...
        └── (no value, "everything else") -> UNIT_EXCLUDED
    All in ONE mutate call so the parent-child temp IDs resolve correctly —
    the Ads UI would need this as two passes (create the subdivision, then
    read back its new ID for the children); the API lets you reference a
    not-yet-created parent via a negative temporary resource id in the SAME
    request.

    By default the new SUBDIVISION has no parent (it becomes the tree's
    ROOT — the original use case, replacing a flat top-level "everything"
    filter). Pass `--parent-id` (+ `--parent-dimension`/`--parent-case-value`)
    to instead NEST it under an existing node — e.g. converting one tier's
    UNIT_INCLUDED leaf into a further subdivision on a SECOND dimension. The
    new node then carries the SAME case_value the removed leaf had, so it
    occupies the identical slot under ITS parent, and only what happens BELOW
    it changes."""
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
    if getattr(args, "parent_id", None):
        root.parent_listing_group_filter = _agf_path(
            client, customer_id, args.asset_group_id, args.parent_id)
        parent_dim = getattr(client.enums.ListingGroupFilterCustomAttributeIndexEnum,
                              CUSTOM_LABEL_INDEX[args.parent_dimension])
        root.case_value.product_custom_attribute.index = parent_dim
        if args.parent_case_value:
            root.case_value.product_custom_attribute.value = args.parent_case_value
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


def build_shopping_tier_subdivision(args):
    """Standard Shopping campaigns use `AdGroupCriterion.listing_group`
    (`ListingGroupInfo`), a DIFFERENT object from PMax's
    `AssetGroupListingGroupFilter` used by `build_tier_subdivision` — a
    different service, a different type enum (`SUBDIVISION`/`UNIT` only, no
    `UNIT_INCLUDED`/`UNIT_EXCLUDED`), and exclusion is expressed via
    `ad_group_criterion.negative = true` on a UNIT leaf rather than a
    dedicated type.

    Removes one existing UNIT leaf (typically a catch-all with no case
    value — e.g. `brand=''` under a `product_brand` subdivision) and replaces
    it with a nested SUBDIVISION on `--dimension`:
        SUBDIVISION (new, same slot as the removed leaf)
        ├── dimension = include[0] -> UNIT (bid on, not negative)
        ├── dimension = include[1] -> UNIT
        ├── ...
        └── (no value, "everything else") -> UNIT, negative=true (excluded)

    This is deliberately SURGICAL: it only touches the one leaf named by
    `--remove-criterion-id`, leaving every sibling under `--parent-id` (e.g.
    an existing brand-specific carve-out) untouched. Building a whole new
    root tree would risk destroying an existing carve-out whose purpose
    isn't obvious from the API alone."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = client.get_service("AdGroupCriterionService")
    ag_svc = client.get_service("AdGroupService")
    customer_id = _customer_id()
    ad_group_path = ag_svc.ad_group_path(customer_id, args.ad_group_id)
    ops = []

    remove_op = client.get_type("AdGroupCriterionOperation")
    remove_op.remove = svc.ad_group_criterion_path(
        customer_id, args.ad_group_id, args.remove_criterion_id)
    ops.append(remove_op)

    TEMP_ROOT_ID = "-1"
    root_op = client.get_type("AdGroupCriterionOperation")
    root = root_op.create
    root.ad_group = ad_group_path
    root.resource_name = svc.ad_group_criterion_path(customer_id, args.ad_group_id, TEMP_ROOT_ID)
    root.listing_group.type_ = client.enums.ListingGroupTypeEnum.SUBDIVISION
    root.listing_group.parent_ad_group_criterion = svc.ad_group_criterion_path(
        customer_id, args.ad_group_id, args.parent_id)
    # NOTE: standard Shopping's ListingGroupInfo.case_value.product_custom_attribute
    # uses ProductCustomAttributeIndexEnum -- a DIFFERENT enum TYPE from PMax's
    # AssetGroupListingGroupFilter, which uses ListingGroupFilterCustomAttributeIndexEnum.
    # Same member names (INDEX0..INDEX4), different underlying type -- verified
    # live via a 400 INVALID_ENUM_VALUE when the PMax enum name was tried on
    # this object.
    dim = getattr(client.enums.ProductCustomAttributeIndexEnum,
                  CUSTOM_LABEL_INDEX[args.dimension])
    # The new subdivision must carry the SAME case_value the removed leaf had
    # under ITS parent, so it sits in the identical tree slot -- but that is
    # whatever dimension the PARENT subdivision splits on (e.g. product_brand
    # for a brand carve-out), which is a DIFFERENT dimension from --dimension
    # (the tier the new CHILDREN below split on).
    if args.parent_dimension == "product_brand":
        if args.parent_case_value:
            root.listing_group.case_value.product_brand.value = args.parent_case_value
        else:
            # An EMPTY product_brand.value is REJECTED (TOO_SHORT -- Google
            # validates it as a real brand-name string), but leaving
            # case_value entirely untouched is ALSO rejected (REQUIRED -- the
            # oneof must be explicitly selected even for "no case"). The fix
            # is the raw-protobuf SetInParent() idiom (the same `._pb` escape
            # hatch this project already uses elsewhere for field_mask
            # construction): it selects the product_brand oneof arm as
            # present WITHOUT setting its value field, which is exactly what
            # "everything else under this product_brand subdivision" means
            # structurally. Verified live: HasField('case_value') becomes
            # True with a bare `product_brand {}` and no `value`.
            root.listing_group.case_value.product_brand._pb.SetInParent()
    else:
        parent_dim = getattr(client.enums.ProductCustomAttributeIndexEnum,
                              CUSTOM_LABEL_INDEX[args.parent_dimension])
        root.listing_group.case_value.product_custom_attribute.index = parent_dim
        if args.parent_case_value:
            root.listing_group.case_value.product_custom_attribute.value = args.parent_case_value
    ops.append(root_op)

    for value in args.include:
        child_op = client.get_type("AdGroupCriterionOperation")
        child = child_op.create
        child.ad_group = ad_group_path
        child.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
        child.listing_group.parent_ad_group_criterion = svc.ad_group_criterion_path(
            customer_id, args.ad_group_id, TEMP_ROOT_ID)
        child.listing_group.case_value.product_custom_attribute.index = dim
        child.listing_group.case_value.product_custom_attribute.value = value
        # REQUIRED even under an automated (e.g. Target ROAS) bid strategy,
        # which ignores it in practice -- verified live.
        child.cpc_bid_micros = args.cpc_bid_micros
        ops.append(child_op)

    other_op = client.get_type("AdGroupCriterionOperation")
    other = other_op.create
    other.ad_group = ad_group_path
    other.negative = True   # the "excluded" mechanism for AdGroupCriterion listing groups
    other.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
    other.listing_group.parent_ad_group_criterion = svc.ad_group_criterion_path(
        customer_id, args.ad_group_id, TEMP_ROOT_ID)
    other.listing_group.case_value.product_custom_attribute.index = dim
    # no .value -> "everything else" under this subdivision
    ops.append(other_op)

    try:
        resp = _mutate(client, svc, "MutateAdGroupCriteriaRequest", customer_id, ops, args.execute)
        _report_result(resp, args.execute, "build Shopping tier subdivision")
    except GoogleAdsException as ex:
        _report_failure(ex)


def add_pmax_tier_include(args):
    """Add ONE more UNIT_INCLUDED sibling to an EXISTING PMax tier
    subdivision — additive only, does not touch the subdivision's other
    children or its catch-all. Used to widen an already-built tier tree (see
    `build_tier_subdivision`) once you're ready to add another value without
    rebuilding the whole thing."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = _listing_group_service(client)
    customer_id = _customer_id()
    op = client.get_type("AssetGroupListingGroupFilterOperation")
    node = op.create
    node.asset_group = client.get_service("AssetGroupService").asset_group_path(
        customer_id, args.asset_group_id)
    node.type_ = client.enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
    node.listing_source = client.enums.ListingGroupFilterListingSourceEnum.SHOPPING
    node.parent_listing_group_filter = _agf_path(client, customer_id, args.asset_group_id, args.parent_id)
    dim = node.case_value.product_custom_attribute
    dim.index = getattr(client.enums.ListingGroupFilterCustomAttributeIndexEnum,
                         CUSTOM_LABEL_INDEX[args.dimension])
    dim.value = args.value

    try:
        resp = _mutate(client, svc, "MutateAssetGroupListingGroupFiltersRequest",
                       customer_id, [op], args.execute)
        _report_result(resp, args.execute, "add PMax tier include")
    except GoogleAdsException as ex:
        _report_failure(ex)


def add_shopping_tier_include(args):
    """Add ONE more UNIT sibling (ENABLED, not negative) to an EXISTING
    Shopping tier subdivision built by `build_shopping_tier_subdivision` —
    additive only, same rationale as `add_pmax_tier_include`."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = client.get_service("AdGroupCriterionService")
    ag_svc = client.get_service("AdGroupService")
    customer_id = _customer_id()
    op = client.get_type("AdGroupCriterionOperation")
    node = op.create
    node.ad_group = ag_svc.ad_group_path(customer_id, args.ad_group_id)
    node.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
    node.listing_group.parent_ad_group_criterion = svc.ad_group_criterion_path(
        customer_id, args.ad_group_id, args.parent_id)
    dim = getattr(client.enums.ProductCustomAttributeIndexEnum, CUSTOM_LABEL_INDEX[args.dimension])
    node.listing_group.case_value.product_custom_attribute.index = dim
    node.listing_group.case_value.product_custom_attribute.value = args.value
    node.cpc_bid_micros = args.cpc_bid_micros

    try:
        resp = _mutate(client, svc, "MutateAdGroupCriteriaRequest", customer_id, [op], args.execute)
        _report_result(resp, args.execute, "add Shopping tier include")
    except GoogleAdsException as ex:
        _report_failure(ex)


def add_keywords(args):
    """Bulk-add BROAD match keyword criteria to an ad group, one operation
    per line in `--file` (blank lines skipped). All in ONE mutate call so
    `validate_only` checks the whole batch atomically."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    with open(args.file, encoding="utf-8") as f:
        terms = [line.strip() for line in f if line.strip()]

    svc = client.get_service("AdGroupCriterionService")
    ag_path = client.get_service("AdGroupService").ad_group_path(_customer_id(), args.ad_group_id)
    ops = []
    for term in terms:
        op = client.get_type("AdGroupCriterionOperation")
        crit = op.create
        crit.ad_group = ag_path
        crit.keyword.text = term
        crit.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
        ops.append(op)

    try:
        resp = _mutate(client, svc, "MutateAdGroupCriteriaRequest", _customer_id(), ops, args.execute)
        _report_result(resp, args.execute, f"add {len(ops)} keyword(s)")
    except GoogleAdsException as ex:
        _report_failure(ex)


def remove_ad_group_criterion(args):
    """Remove a single ad_group_criterion (e.g. a dead brand carve-out leaf
    in a listing-group tree) without touching its siblings."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = client.get_service("AdGroupCriterionService")
    op = client.get_type("AdGroupCriterionOperation")
    op.remove = svc.ad_group_criterion_path(_customer_id(), args.ad_group_id, args.criterion_id)

    try:
        resp = _mutate(client, svc, "MutateAdGroupCriteriaRequest", _customer_id(), [op], args.execute)
        _report_result(resp, args.execute, "remove ad group criterion")
    except GoogleAdsException as ex:
        _report_failure(ex)


def remove_ad_group(args):
    """Permanently remove an ad group (`AdGroupOperation.remove`) —
    IRREVERSIBLE, same caution as `remove_campaigns`."""
    client = _client()
    from google.ads.googleads.errors import GoogleAdsException

    svc = client.get_service("AdGroupService")
    op = client.get_type("AdGroupOperation")
    op.remove = svc.ad_group_path(_customer_id(), args.ad_group_id)

    try:
        resp = _mutate(client, svc, "MutateAdGroupsRequest", _customer_id(), [op], args.execute)
        _report_result(resp, args.execute, "remove ad group")
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

    p = sub.add_parser("add-campaign-negative-user-list")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--user-list-id", action="append", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=add_campaign_negative_user_list)

    p = sub.add_parser("flip-campaign-user-list-to-negative")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--old-criterion-id", action="append", required=True)
    p.add_argument("--user-list-id", action="append", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=flip_campaign_user_list_to_negative)

    p = sub.add_parser("add-audience-user-lists")
    p.add_argument("--audience-id", required=True)
    p.add_argument("--user-list-id", action="append", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=add_audience_user_lists)

    p = sub.add_parser("remove-audience-segment")
    p.add_argument("--audience-id", required=True)
    p.add_argument("--user-list-id", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=remove_audience_segment)

    p = sub.add_parser("set-shopping-feed-label")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--feed-label", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=set_shopping_feed_label)

    p = sub.add_parser("set-tis-ceiling")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--ceiling", type=float, required=True, help="new max CPC bid ceiling in dollars")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=set_tis_ceiling)

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
    p.add_argument("--parent-id",
                   help="nest the new subdivision under an existing node instead of "
                        "making it root")
    p.add_argument("--parent-dimension", choices=CUSTOM_LABEL_INDEX,
                   help="dimension the PARENT node's own case_value uses "
                        "(required if --parent-id given)")
    p.add_argument("--parent-case-value", default="",
                   help="case value the removed leaf had under its own parent, so "
                        "the new node keeps its slot")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=build_tier_subdivision)

    p = sub.add_parser("build-shopping-tier-subdivision")
    p.add_argument("--ad-group-id", required=True)
    p.add_argument("--parent-id", required=True)
    p.add_argument("--remove-criterion-id", required=True)
    p.add_argument("--parent-dimension", required=True,
                   choices=["product_brand", *CUSTOM_LABEL_INDEX])
    p.add_argument("--parent-case-value", default="",
                   help="case value the removed leaf had under its own parent "
                        "(e.g. a brand name); omit/blank for an unset catch-all")
    p.add_argument("--dimension", required=True, choices=CUSTOM_LABEL_INDEX)
    p.add_argument("--include", action="append", required=True)
    p.add_argument("--cpc-bid-micros", type=int, default=10000,
                   help="vestigial CPC bid required on each UNIT leaf even under "
                        "an automated bid strategy (default 10000 = $0.01)")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=build_shopping_tier_subdivision)

    p = sub.add_parser("add-pmax-tier-include")
    p.add_argument("--asset-group-id", required=True)
    p.add_argument("--parent-id", required=True)
    p.add_argument("--dimension", required=True, choices=CUSTOM_LABEL_INDEX)
    p.add_argument("--value", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=add_pmax_tier_include)

    p = sub.add_parser("add-shopping-tier-include")
    p.add_argument("--ad-group-id", required=True)
    p.add_argument("--parent-id", required=True)
    p.add_argument("--dimension", required=True, choices=CUSTOM_LABEL_INDEX)
    p.add_argument("--value", required=True)
    p.add_argument("--cpc-bid-micros", type=int, default=10000)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=add_shopping_tier_include)

    p = sub.add_parser("add-keywords")
    p.add_argument("--ad-group-id", required=True)
    p.add_argument("--file", required=True, help="one keyword term per line")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=add_keywords)

    p = sub.add_parser("remove-ad-group-criterion")
    p.add_argument("--ad-group-id", required=True)
    p.add_argument("--criterion-id", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=remove_ad_group_criterion)

    p = sub.add_parser("remove-ad-group")
    p.add_argument("--ad-group-id", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=remove_ad_group)

    args = ap.parse_args()
    if not args.execute:
        print("(validate_only mode — no changes will be made; pass --execute to apply)\n")
    args.func(args)


if __name__ == "__main__":
    main()
