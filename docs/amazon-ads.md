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
   Only **one** advertising profile is synced: if the login can see several
   (multiple marketplaces/accounts), the helper writes the **first** one to
   `.env` and just prints the rest — swap `AMAZON_ADS_PROFILE_ID` by hand to
   sync a different one.
   Already have a refresh token but need the profile ID (new marketplace,
   lost the value)? `python amazon_auth.py --profiles` looks it up without
   a fresh consent round-trip.
3. Fill in the rest of `.env`:

   | Variable | Notes |
   |---|---|
   | `AMAZON_ADS_CLIENT_ID` / `AMAZON_ADS_CLIENT_SECRET` | from the LWA app |
   | `AMAZON_ADS_REGION` | `NA`, `EU`, or `FE`, uppercase (default `NA`; the connectors look it up case-sensitively) — picks the Ads API host for both the auth helper and the report pulls |
   | `AMAZON_ADS_REDIRECT_URI` | must byte-match an Allowed Return URL on the security profile — trailing slash matters. The helper falls back to `https://localhost` only when the variable is **absent**; `.env.example` ships it as an empty `AMAZON_ADS_REDIRECT_URI=` line, which loads as an empty string, so fill it in (e.g. `https://localhost`) rather than leaving it blank |
   | `AMAZON_ADS_REPORT_TIMEOUT_MIN` | optional, how long to wait for an async report before giving up (default `60`) |

`amazon_ads_detail_sync.py` reuses these same variables — nothing new to
configure.

## Usage

```bash
python run_sync.py --only amazon          # core campaign metrics, --days 7 back from today
python amazon_ads_detail_sync.py          # ASIN/keyword/search-term detail, --days 3 back from today
python amazon_ads_detail_sync.py --days 30
python amazon_ads_detail_sync.py --start 2026-01-01 --end 2026-01-31
```

Both windows are `[end - days, end]` **inclusive**, and `--end` defaults to
**today** (a partial day). So the detail default `--days 3` covers 4 calendar
dates, and `run_sync.py`'s default `--days 7` covers 8. `--start` overrides
`--days` when given.

The detail script splits long windows into ≤31-day report requests (Amazon's
per-report limit). The **core** connector does not — it requests the whole
`start..end` range as one report per ad product — so keep core pulls at
`--days 30` or less (backfill in pieces with `--start`/`--end`).

## Tables

- `ad_metrics` (core, shared across platforms — see the main [README](../README.md#mcp-tools)).
  Amazon rows have `platform = 'amazon'`, `account_id` = `AMAZON_ADS_PROFILE_ID`,
  `campaign_type` = `SPONSORED_PRODUCTS` / `SPONSORED_BRANDS` / `SPONSORED_DISPLAY`,
  and **`currency` always `NULL`** (values are in the profile's marketplace
  currency, which the report doesn't state).
- Detail tables (Sponsored Products only), all written with `INSERT OR REPLACE`:

  | Table | Report | Primary key | Extra columns |
  |---|---|---|---|
  | `amazon_ad_products` | `spAdvertisedProduct` | `(account_id, date, campaign_id, ad_group_id, asin)` | `sku` (nullable), `units` (`unitsSoldClicks14d`) |
  | `amazon_ad_targeting` | `spTargeting` | `(account_id, date, campaign_id, ad_group_id, targeting)` | `match_type` (nullable) |
  | `amazon_ad_search_terms` | `spSearchTerm` | `(account_id, date, campaign_id, ad_group_id, search_term)` | — |

  Every detail table also carries `campaign_name`, `impressions`, `clicks`,
  `spend` (Amazon's `cost`), `purchases` (`purchases14d`), `sales`
  (`sales14d`), and `synced_at`.

## Notes

**Coverage.** `ad_metrics` covers Sponsored Products, Sponsored Brands, and
Sponsored Display (`campaign_type` distinguishes them); `amazon_ads_detail_sync.py`'s
three grains are Sponsored Products only — SB/SD use different report shapes
at that level of detail. Amazon DSP is not covered by either script — it uses
a different reporting surface and entity permissions, not the v3
`/reporting/reports` endpoint.

**Attribution columns differ by ad type.** In `ad_metrics`, Sponsored Products
`conversions`/`revenue` come from `purchases14d`/`sales14d` (14-day
attribution); Sponsored Brands and Sponsored Display use the v3 report's plain
`purchases`/`sales` columns. All three detail tables use the 14-day SP columns.
Keep this in mind before summing conversions across `campaign_type`s.

**Old days freeze.** Rows are overwritten only while their date is inside the
lookback window of some run. Attributed purchases keep arriving for up to 14
days after the click, so with the 3-day detail default a row stops updating
about 3 days after the fact and will understate late-attributed sales. Run a
wider `--days 14` periodically if you need settled attribution.

**Detail-table quirks.**
- A missing `asin`, `targeting`, or `search_term` is stored as `''`, not
  `NULL` (they're primary-key columns). Missing numeric metrics become `0`.
- `amazon_ad_targeting.targeting` holds both keyword text and product/
  category targeting expressions; `match_type` tells them apart.
- `amazon_ad_search_terms` has no keyword or match-type column, so a search
  term can't be tied back to the specific keyword that triggered it — only to
  its campaign and ad group.

**Retention.** The detail grains follow the v3 reporting API's retention,
which is much shorter than the campaign-level history in `ad_metrics` — expect
roughly the trailing ~95 days to be available, not deep history.

**Report queueing and retries.** Report generation is asynchronous and can
take 30-45+ minutes when Amazon's queue is congested; both scripts wait up to
`AMAZON_ADS_REPORT_TIMEOUT_MIN` (default 60) before giving up. The shared
`run_report()` flow in `warehouse/connectors/amazon_ads.py`:
- token refresh: up to 5 tries, 30s apart, on connection errors/timeouts only;
- report request: up to 8 attempts — `429` waits `30 × (attempt + 1)` seconds,
  a dropped connection waits 20s, `401` refreshes the token and retries, `425`
  (duplicate) reuses the report already queued; any other non-`200/202` fails
  at once;
- polling: starts at 15s and backs off ×1.25 up to 60s, riding out `401`,
  `429`, `5xx`, and connection drops; a `FAILED` report raises.

**Partial failures and `sync_log`.**
- **Core** (`sync_log.platform = 'amazon'`): one ad product failing doesn't
  stop the others, and its rows are simply missing — the run still logs
  **`ok`** with the rows that landed. The failure only shows in console output
  (`amazon sbCampaigns FAILED: ...`). Only when *every* ad product fails does
  the run log `error`. So check row counts per `campaign_type`, not just
  `last_sync_status`, when SB/SD numbers look absent.
- **Detail** (`sync_log.platform = 'amazon_ads_detail'`): commits after each
  report, so a crash only loses the report in flight. One grain/window failing
  logs the run **`degraded`** (exit code 0); only a run that wrote nothing at
  all logs `error` (exit code 1). If any of `AMAZON_ADS_CLIENT_ID`,
  `AMAZON_ADS_CLIENT_SECRET`, `AMAZON_ADS_REFRESH_TOKEN`,
  `AMAZON_ADS_PROFILE_ID` is unset it exits with a message and writes nothing
  to `sync_log`.
- `run_sync.py` skips the core connector only when `AMAZON_ADS_REFRESH_TOKEN`
  is unset; other missing variables surface as an `error` row.

## Tests

`tests/test_amazon_ads_detail_sync.py` (detail script). The core connector
(`warehouse/connectors/amazon_ads.py`) and `amazon_auth.py` have no dedicated tests.
