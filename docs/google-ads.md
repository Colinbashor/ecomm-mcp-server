# Google Ads

Campaign-level spend/clicks/conversions (core), plus three standalone scripts
for everything the campaign-level connector can't reach: search terms,
keyword-level performance, Shopping/Performance Max product demand, and
current-state campaign/asset/conversion-action configuration.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/google_ads.py` | core, via `run_sync.py --only google` | daily campaign spend/clicks/impressions/conversions/revenue into `ad_metrics` |
| `google_ads_detail_sync.py` | standalone | search terms, keywords + Quality Score, paid-vs-organic overlap, conversion-action attribution, device split, Shopping/PMax product demand, PMax search themes |
| `google_ads_structure_sync.py` | standalone | current-state snapshots: campaigns, asset groups + assets, listing-group filters, conversion-action setup |
| `google_ads_mutate.py` | standalone, **write-capable** | pause/remove a campaign or ad group, change bidding strategy or TIS bid ceiling, restrict a Shopping campaign to one feed label, edit a Performance Max *or* standard Shopping listing-group filter tree, add/remove keywords, add/remove/flip `user_list` audience criteria on a campaign, edit an Audience's segment membership, end a Campaign Experiment |

The structure connector in particular is aimed at "this campaign looks funded
but isn't serving" — a question spend/impression metrics alone usually can't
answer.

`google_ads_mutate.py` is the one script in this repo that changes anything
in your live ad account. Every mutate call defaults to `validate_only=True`
(full server-side validation, zero changes committed) — only `--execute`
actually applies a change. See its module docstring before using it.

## Setup

1. Create an OAuth **Desktop app** client in Google Cloud.
2. Run the interactive auth helper — it opens a browser and saves the refresh
   token for you:

   ```bash
   python google_auth.py
   ```
3. Fill in the `Google Ads` block in `.env`:

   | Variable | Notes |
   |---|---|
   | `GOOGLE_ADS_DEVELOPER_TOKEN` | from the API Center of your Ads manager account |
   | `GOOGLE_ADS_CLIENT_ID` / `GOOGLE_ADS_CLIENT_SECRET` | from the OAuth client above |
   | `GOOGLE_ADS_REFRESH_TOKEN` | written by `google_auth.py` |
   | `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | the manager (MCC) account, digits only |
   | `GOOGLE_ADS_CUSTOMER_ID` | the ad account to pull, digits only |

`google_ads_detail_sync.py` and `google_ads_structure_sync.py` reuse these
same variables — nothing new to configure.

## Usage

```bash
python run_sync.py --only google         # core campaign metrics, last 7 days
python google_ads_detail_sync.py          # search terms, keywords, product demand; default 3-day lookback
python google_ads_detail_sync.py --days 30
python google_ads_detail_sync.py --start 2026-01-01 --end 2026-01-31
python google_ads_detail_sync.py --only google_pmax_search_themes --start 2026-01-01 --end 2026-01-31
python google_ads_structure_sync.py       # current-state config snapshot, as of today
python google_ads_structure_sync.py --date 2026-01-15   # or --only campaigns,asset_groups

# google_ads_mutate.py — every subcommand below defaults to validate_only=True
# (server-side validation, zero changes committed); add --execute to apply it
python google_ads_mutate.py pause-campaign --campaign-id 18373650912
python google_ads_mutate.py pause-campaign --campaign-id 18373650912 --execute
python google_ads_mutate.py remove-campaigns --campaign-id 18373650912 --campaign-id 20593969582 --execute
python google_ads_mutate.py end-experiment --experiment-id 6477859796 --execute
python google_ads_mutate.py set-bidding --campaign-id 20593969582 --target-roas 2.5
python google_ads_mutate.py set-bidding --campaign-id 20593969582 --maximize-conversion-value --execute
python google_ads_mutate.py replace-filter --campaign-id 20593969582 \
    --asset-group-id 6477859796 --remove-filter-id 11195896515 \
    --dimension custom_label_0 --value "Winter - Proven Seller" --parent-id 11195894994 --execute
python google_ads_mutate.py build-tier-subdivision --campaign-id 22001500480 \
    --asset-group-id 6536885353 --remove-filter-id 12163354837 \
    --dimension custom_label_0 --include "A - Hero" --include "B - Scale up" --execute
python google_ads_mutate.py build-tier-subdivision --campaign-id 22001500480 \
    --asset-group-id 6536885353 --remove-filter-id 12163354837 \
    --parent-id 12163354800 --parent-dimension custom_label_0 --parent-case-value "A - Hero" \
    --dimension custom_label_2 --include "Clearance" --execute   # nest instead of root

python google_ads_mutate.py set-tis-ceiling --campaign-id 20593969582 --ceiling 1.25 --execute
python google_ads_mutate.py set-shopping-feed-label --campaign-id 20593969582 --feed-label US --execute

python google_ads_mutate.py add-keywords --ad-group-id 118472772345 --file keywords.txt --execute
python google_ads_mutate.py remove-ad-group-criterion --ad-group-id 118472772345 --criterion-id 987654 --execute
python google_ads_mutate.py remove-ad-group --ad-group-id 118472772345 --execute

# user_list (RLSA) audience criteria on a campaign
python google_ads_mutate.py add-campaign-negative-user-list --campaign-id 20593969582 \
    --user-list-id 1234567890 --execute
python google_ads_mutate.py flip-campaign-user-list-to-negative --campaign-id 20593969582 \
    --old-criterion-id 555 --user-list-id 1234567890 --execute

# an Audience resource's own segment membership (distinct from campaign criteria above)
python google_ads_mutate.py add-audience-user-lists --audience-id 111222333 \
    --user-list-id 1234567890 --execute
python google_ads_mutate.py remove-audience-segment --audience-id 111222333 \
    --user-list-id 1234567890 --execute

# standard Shopping's counterpart to build-tier-subdivision (different object/service/enum — see Notes)
python google_ads_mutate.py build-shopping-tier-subdivision --ad-group-id 118472772345 \
    --parent-id 987650 --remove-criterion-id 987651 \
    --parent-dimension product_brand --parent-case-value "" \
    --dimension custom_label_0 --include "A - Hero" --include "B - Scale up" --execute
python google_ads_mutate.py add-pmax-tier-include --asset-group-id 6536885353 \
    --parent-id 12163354837 --dimension custom_label_0 --value "C - Maintain" --execute
python google_ads_mutate.py add-shopping-tier-include --ad-group-id 118472772345 \
    --parent-id 987651 --dimension custom_label_0 --value "C - Maintain" --execute
```

`remove-campaigns` accepts repeated `--campaign-id` to remove several in one
mutate request. Always run a subcommand without `--execute` first, read the
validation result, then re-run with `--execute` once it validates clean.

`end-experiment` has no `validate_only` mode at all — the API call itself
isn't dry-runnable, so `--execute` is the *only* thing standing between a
bare invocation and actually ending a real experiment. Confirm the
experiment id/status with a GAQL read first.

Both scripts write each grain independently and mark the run `"degraded"`
(not `"ok"`) in `sync_log` if one grain fails while others succeed — check
`last_sync_status` for `"degraded"` rather than assuming a run either fully
succeeded or fully failed.

## Tables

- `ad_metrics` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `google_search_terms`, `google_keywords`, `google_paid_organic`,
  `google_conversion_actions_daily`, `google_campaign_devices`,
  `google_shopping_products`, `google_pmax_search_themes` (detail)
- `google_campaigns`, `google_asset_groups`, `google_asset_group_assets`,
  `google_asset_group_listing_filters`, `google_conversion_actions` (structure)

## Notes

A few grains carry real gotchas — check these before writing a query or a
cross-table rollup:

- **`google_pmax_search_themes` isn't populated by a default run.** It's
  window-aggregated rather than date-keyed, so `google_ads_detail_sync.py`
  only fetches it when named explicitly via `--only` together with an
  aligned `--start`/`--end` window — running the script bare skips it
  silently.
- **`google_paid_organic` has no money columns.** `cost_micros`,
  `conversions`, and `conversions_value` all error against this view; it's
  clicks/impressions only, for paid-vs-organic overlap.
- **Never sum `google_conversion_actions_daily.conversions` with
  `ad_metrics`** — the two attribute the same conversions differently, and
  adding them double-counts.
- **`all_conversions` (wherever it appears) is diagnostic-only** — it
  includes view-through and cross-device attribution well outside your
  actual conversion actions. Don't report it as a business metric; use the
  named conversion-action columns instead.
- **`search_impression_share` and its two lost-share columns are `NULL`,
  never `0`, on campaign types that run no search auction at all** (Performance
  Max, Display, Video) — Google returns a meaningless `0` for those, and
  `warehouse/connectors/google_ads.py` maps it to `NULL` so it can't be
  averaged in as a real zero. On Search/Shopping campaigns, which DO run a
  search auction, a real `0.0` is stored as `0.0`, not `NULL` — don't
  reintroduce a blanket "any 0 means not applicable" check, that silently
  discards genuine zero-share days on exactly the campaign types where the
  metric matters. Some accounts have also been observed getting a hard
  `0.0/0.0/0.0` triple back from Google on a day with real impressions,
  which the connector detects (impression share + budget-lost + rank-lost
  should sum to ~1.0 on any real day) and stores as `NULL` rather than as a
  fabricated collapse.
- **`google_ads_mutate.py` needs the account's permission tier — not the
  OAuth scope — raised to Standard or Admin.** The Google Ads API has exactly
  one OAuth scope (`https://www.googleapis.com/auth/adwords`) covering both
  read and write, so the same refresh token used for every read-only script
  above also works here with no re-auth. But a Read-only permission tier on
  the account itself rejects mutate calls even with `validate_only=True` —
  validation happens server-side against the live account, so the tier check
  runs before anything is validated, not just before anything is committed.
- **Standard Shopping and Performance Max use two *different* objects for a
  listing-group tree, and a case-value index enum with the SAME member names
  but a DIFFERENT underlying type on each.** PMax uses
  `AssetGroupListingGroupFilter` (`AssetGroupListingGroupFilterService`,
  `ListingGroupFilterCustomAttributeIndexEnum`, filter types
  `SUBDIVISION`/`UNIT_INCLUDED`/`UNIT_EXCLUDED`) — that's what
  `build-tier-subdivision`/`add-pmax-tier-include` operate on. Standard
  Shopping uses `AdGroupCriterion.listing_group`
  (`AdGroupCriterionService`, `ProductCustomAttributeIndexEnum`, types
  `SUBDIVISION`/`UNIT` only, with exclusion expressed as
  `ad_group_criterion.negative = true` on a `UNIT` leaf rather than a
  dedicated type) — that's `build-shopping-tier-subdivision`/
  `add-shopping-tier-include`. Passing the PMax enum's member name into the
  standard-Shopping object (or vice versa) 400s with `INVALID_ENUM_VALUE`
  even though both enums expose identical-looking `INDEX0`..`INDEX4` members.
  Don't try to share one enum lookup between the two object families.
- **A subdivision's "everything else" catch-all node needs its `case_value`
  oneof explicitly selected with NO value set — neither leaving it untouched
  nor setting an empty string works.** Leaving `case_value` completely
  untouched is rejected as `REQUIRED` (Google needs the oneof arm chosen even
  for "no case"); for the `product_brand` oneof arm specifically, setting an
  *empty* `.value` is separately rejected as `TOO_SHORT` (it validates as a
  real brand-name string). The working fix, used in
  `build_shopping_tier_subdivision`, is the raw-protobuf escape hatch
  `root.listing_group.case_value.product_brand._pb.SetInParent()` — it
  selects the oneof arm as present without populating its value field. This
  is the same class of `._pb` escape hatch this repo already uses elsewhere
  for `field_mask` construction; it's not intuitive from the proto-plus
  Python API surface alone.
- **`Audience.dimensions` (used by `add-audience-user-lists`/
  `remove-audience-segment`) is a repeated field with no server-side
  append/remove — an update always REPLACES the whole list.** Both
  subcommands read the existing audience first, deep-copy every dimension
  the API returns via `new_dim._pb.CopyFrom(dim._pb)` (proto-plus repeated
  *message* fields have no `.add()`; each element must be built standalone
  and appended via `.append()`), and only then mutate the
  `audience_segments` dimension before writing the complete result back —
  age/gender/interest/custom-audience dimensions already on the audience are
  preserved untouched.
- **Flipping an existing `CampaignCriterion`'s `negative` flag in place
  fails with `IMMUTABLE_FIELD`.** Google treats a campaign+`user_list` pair
  as the criterion's identity, so a plain update that only changes
  `negative` on an already-existing positive criterion is rejected as an
  illegal update-via-create. `flip-campaign-user-list-to-negative` works
  around this the same way `replace-filter` handles listing-group filters:
  remove the old criterion and create a fresh negative one, in ONE atomic
  `mutate` batch.

## Tests

`tests/test_google_ads_detail_sync.py`, `tests/test_google_ads_structure_sync.py`,
`tests/test_google_ads_connector.py`, `tests/test_google_ads_mutate.py`
