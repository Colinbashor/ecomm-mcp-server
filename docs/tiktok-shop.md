# TikTok Shop

Order ground truth (core), plus five standalone scripts for video
performance, LIVE-shopping, creator identity, sales-source attribution, and
settlement/fee data.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/tiktok_shop.py` | core, via `run_sync.py --only tiktok` | orders into `orders` |
| `tiktok_videos_sync.py` | standalone | video performance |
| `tiktok_live_sync.py` | standalone | LIVE-shopping broadcast + product funnel |
| `tiktok_creators_sync.py` | standalone | handle ↔ display-name ↔ user-id creator/affiliate identity bridge |
| `tiktok_analytics_sync.py` | standalone | true mutually-exclusive LIVE/VIDEO/PRODUCT_CARD sales-source split |
| `tiktok_finance_sync.py` | standalone | settlement statements + per-order fee decomposition |

## Setup

1. Authorize a Partner Center app with **all scopes at once** — a partial
   re-auth silently replaces the grant and orders fail with error 105005 about
   a week later.
2. Run the auth helper:

   ```bash
   python tiktok_auth.py PASTE_THE_CODE_HERE   # code from Partner Center
   ```

   This fills tokens plus the shop cipher/id for you.
3. Fill in the rest of `.env`:

   | Variable | Notes |
   |---|---|
   | `TIKTOK_APP_KEY` / `TIKTOK_APP_SECRET` | from the Partner Center app |
   | `TIKTOK_ACCESS_TOKEN` / `TIKTOK_REFRESH_TOKEN` / `TIKTOK_SHOP_CIPHER` / `TIKTOK_SHOP_ID` | written by `tiktok_auth.py` |
   | `TIKTOK_LIVE_OWN_ACCOUNT_TYPE` | which `tiktok_shop_lives.account_type` counts as your own broadcasts vs. affiliate/creator or paid-marketing lives (default `OFFICIAL_ACCOUNTS`) |
   | `TIKTOK_SHOP_TIMEZONE` | IANA timezone for bucketing LIVE broadcasts into calendar days (default `UTC`) |

All five extras reuse the core `TIKTOK_*` credentials — nothing new to
configure.

## Usage

```bash
python run_sync.py --only tiktok      # core orders, last 7 days
python tiktok_videos_sync.py
python tiktok_live_sync.py
python tiktok_creators_sync.py api      # or `import path/to/export.csv` for the manual path
python tiktok_analytics_sync.py
python tiktok_finance_sync.py                 # settlements, last 30 days
python tiktok_finance_sync.py --backfill      # 365-day window
python tiktok_finance_sync.py --no-components # statements only, fast
```

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `tiktok_shop_videos`
- `tiktok_shop_lives`, `tiktok_shop_live_products`
- `tiktok_creators`
- `tiktok_shop_performance`
- `tiktok_settlements`, `tiktok_settlement_components`, `tiktok_settlement_orders`

## Notes

`tiktok_creators_sync.py` exists because TikTok's video API and order API
expose different halves of a creator's identity with no shared join key — the
bridge closes that gap via the API plus an optional manual CSV import. The
`api` subcommand's crawl has a rolling window ceiling, and the write logic
switches behavior based on how far it got: a partial crawl **merges** into
existing rows, while a complete crawl **replaces** them outright — see the
module docstring before assuming every run behaves the same way.

`tiktok_shop_performance` (from `tiktok_analytics_sync.py`) is a cleaner
alternative to estimating "unattributed" sales by subtraction. It chunks
requests by a fixed window length (`CHUNK_DAYS`) and disambiguates a
"date range too wide" error from a genuine retention-window error (TikTok
returns similar-looking codes for both) rather than surfacing a confusing
raw error. It also drops the most recent day or two of unsettled data via
`latest_available_date`, so don't expect today's numbers to be final yet.

`tiktok_live_sync.py` has a two-endpoint design gotcha: the list endpoint
ignores a `live_id` filter, so filtering to one broadcast happens client-side
after fetching the list. `tiktok_shop_lives` is DATE-grain, not
session-grain — a broadcast spanning midnight splits across two rows — and
the script runs a reconciliation sanity-check against the funnel totals to
catch pagination gaps.

`tiktok_finance_sync.py` exists so a report can read a measured, up-to-date
fee/take-rate from the database instead of hardcoding a guessed commission
or calling the Finance API live at render time. The load-bearing detail is
`tiktok_settlement_components.is_fee`: TikTok's settlement transactions carry
real pass-through lines (sales tax) and your own markdowns (seller-funded
discounts) alongside genuine platform fees, and summing all of them together
overstates the take rate substantially. Only `is_fee=1` rows are fees;
everything else is stored for reconciliation. `tiktok_settlement_orders`
keeps the per-order breakdown (not just the aggregated component totals)
specifically so it can be joined to your own orders table by `order_id` —
that's what makes a true per-product fee/margin number possible instead of
only an account-wide rate. Also worth knowing before you debug the same
thing twice: the statements endpoint 400s with "SortField is a required
field" if `sort_field` is omitted, which reads exactly like a missing-scope
error but usually isn't — see the module docstring.

## Tests

`tests/test_tiktok_videos_sync.py`, `tests/test_tiktok_live_sync.py`,
`tests/test_tiktok_creators_sync.py`, `tests/test_tiktok_analytics_sync.py`,
`tests/test_tiktok_finance_sync.py`
