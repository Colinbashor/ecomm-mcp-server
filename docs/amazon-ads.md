# Amazon Advertising

Campaign-level spend/clicks/conversions (core), plus a standalone script for
the grains the campaign-level connector can't reach: per-ASIN advertised-product
performance, keyword/target-level performance, and customer search-term
performance (Sponsored Products).

For Amazon **retail orders / SP-API** (a separate credential set), see
[Amazon Seller](amazon-seller.md). For **Brand Analytics**, see
[Amazon Brand Analytics](amazon-brand-analytics.md).

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/amazon_ads.py` | core, via `run_sync.py --only amazon` | daily campaign spend/clicks/impressions/conversions/revenue into `ad_metrics` |
| `amazon_ads_detail_sync.py` | standalone | per-ASIN advertised-product performance, keyword/target-level performance, search-term performance |

## Setup

1. In the Advanced Tools Center, make sure your LWA app has the Ads API
   scope **assigned** (approval can take ~72h).
2. Run the two-step auth helper:

   ```bash
   python amazon_auth.py --url                     # prints a consent URL
   python amazon_auth.py PASTE_THE_CODE_HERE        # exchanges the code it redirects you to
   ```

   This fills `AMAZON_ADS_REFRESH_TOKEN` and `AMAZON_ADS_PROFILE_ID` for you.
3. Fill in the rest of `.env`:

   | Variable | Notes |
   |---|---|
   | `AMAZON_ADS_CLIENT_ID` / `AMAZON_ADS_CLIENT_SECRET` | from the LWA app |
   | `AMAZON_ADS_REGION` | default `NA` |
   | `AMAZON_ADS_REDIRECT_URI` | must byte-match an Allowed Return URL on the security profile — trailing slash matters |
   | `AMAZON_ADS_REPORT_TIMEOUT_MIN` | optional, how long to wait for an async report before giving up (default `60`) |

`amazon_ads_detail_sync.py` reuses these same variables — nothing new to
configure.

## Usage

```bash
python run_sync.py --only amazon          # core campaign metrics, last 7 days
python amazon_ads_detail_sync.py          # ASIN/keyword/search-term detail
```

## Tables

- `ad_metrics` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `amazon_ad_products`, `amazon_ad_targeting`, `amazon_ad_search_terms` (detail)

## Tests

`tests/test_amazon_ads_detail_sync.py`
