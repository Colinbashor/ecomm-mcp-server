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

The structure connector in particular is aimed at "this campaign looks funded
but isn't serving" — a question spend/impression metrics alone usually can't
answer.

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
python google_ads_detail_sync.py --only pmax_search_themes --start 2026-01-01 --end 2026-01-31
python google_ads_structure_sync.py       # current-state config snapshot, as of today
python google_ads_structure_sync.py --date 2026-01-15   # or --only campaigns,asset_groups
```

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

## Tests

`tests/test_google_ads_detail_sync.py`, `tests/test_google_ads_structure_sync.py`
