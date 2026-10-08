# TikTok Shop

Order ground truth (core), plus seven standalone scripts for video
performance, LIVE-shopping, creator identity and Marketplace profiles, sales-source attribution,
settlement/fee data, and listing-quality diagnostics.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/tiktok_shop.py` | core, via `run_sync.py --only tiktok` | orders into `orders` |
| `tiktok_videos_sync.py` | standalone | video performance |
| `tiktok_live_sync.py` | standalone | LIVE-shopping broadcast + product funnel |
| `tiktok_creators_sync.py` | standalone | handle ↔ display-name ↔ user-id creator/affiliate identity bridge |
| `tiktok_creator_marketplace_sync.py` | standalone | per-creator Marketplace profile (whole-shop category split, GMV/units bands, rating, commission) for handles you supply |
| `tiktok_analytics_sync.py` | standalone | true mutually-exclusive LIVE/VIDEO/PRODUCT_CARD sales-source split |
| `tiktok_finance_sync.py` | standalone | settlement statements + per-order fee decomposition |
| `tiktok_listing_quality_sync.py` | standalone | per-product content-quality tier + issues (Listings Diagnosis API) |

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

All six extras reuse the core `TIKTOK_*` credentials — nothing new to
configure, except `tiktok_listing_quality_sync.py`, which additionally needs
the `seller.product.optimize` scope granted on your Partner Center app
(separate from the product-read scopes the other scripts use — check
Partner Center > your app > Scopes before a first run).

## Usage

```bash
python run_sync.py --only tiktok      # core orders, last 7 days

python tiktok_videos_sync.py                       # trailing 30 days (default)
python tiktok_videos_sync.py --days 60
python tiktok_videos_sync.py --start 2026-01-01 --end 2026-01-31
python tiktok_videos_sync.py --current-window      # trailing 30 days ending tomorrow -- for a daily cron
python tiktok_videos_sync.py --account-types AFFILIATES     # default is AFFILIATES,LINKED_ACCOUNTS; also accepts ALL
python tiktok_videos_sync.py --gmv-positive-only   # cheaper: stop paging at the first zero-GMV video

python tiktok_live_sync.py                         # both lives + per-product funnel, trailing 30 days
python tiktok_live_sync.py --only lives             # or `products` -- skip the other half
python tiktok_live_sync.py --account-types ALL      # default OFFICIAL_ACCOUNTS,AFFILIATE_ACCOUNTS,MARKETING_ACCOUNTS
python tiktok_live_sync.py --dates 2026-01-05,2026-01-06   # force specific days for the products crawl

python tiktok_creators_sync.py api                  # or `import path/to/export.csv` for the manual path
python tiktok_creators_sync.py import --dir exports/   # import every .xlsx/.csv in a folder
python tiktok_creators_sync.py api --dry-run        # fetch + report, write nothing (also on `import`)

python tiktok_creator_marketplace_sync.py --handles creator_one,creator_two
python tiktok_creator_marketplace_sync.py --from-table tiktok_creators --handle-column handle     --group footwear=601352 --group bags=824584 --max-age-days 14

python tiktok_analytics_sync.py                     # trailing 30 days (default)
python tiktok_analytics_sync.py --start 2026-01-01 --end 2026-01-31
python tiktok_analytics_sync.py --dry-run           # fetch and report, write nothing

python tiktok_finance_sync.py                 # settlements, last 30 days
python tiktok_finance_sync.py --backfill      # 365-day window
python tiktok_finance_sync.py --no-components # statements only, fast
python tiktok_finance_sync.py --no-orders     # skip retaining per-order fee rows (statements + components only)

python tiktok_listing_quality_sync.py --product-ids 1234567890,2345678901
python tiktok_listing_quality_sync.py --product-ids-file product_ids.txt
python tiktok_listing_quality_sync.py --product-ids-file product_ids.txt --limit 400   # smoke test: first 400 ids
```

`tiktok_videos_sync.py` retries each page up to 6 times: it refreshes an expired access token once, and backs off exponentially (1, 2, 4, 8, 16s) on connection resets, timeouts, non-JSON bodies, HTTP 429/5xx and API codes 105050 / 105051 / 429000. A month of videos is hundreds of pages, so without this one transient failure would abort the whole pull; any other non-zero API code still fails immediately.

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `tiktok_shop_videos`
- `tiktok_shop_lives`, `tiktok_shop_live_products`
- `tiktok_creators`
- `tiktok_creator_marketplace`, `tiktok_category_top`
- `tiktok_shop_performance`
- `tiktok_settlements`, `tiktok_settlement_components`, `tiktok_settlement_orders`
- `tiktok_weekly_product` (VIEW, rebuilt by `tiktok_finance_sync.py`) — weekly
  (Mon–Sun) per-sku TikTok sales with the platform-funded discount added back;
  see the net-sales note below
- `tiktok_listing_quality` — one row per product (PK `product_id`):
  `current_tier` (`POOR`/`FAIR`/`GOOD`), `remaining_recommendations`,
  `synced_at`
- `tiktok_listing_quality_issues` — one row per issue (PK `product_id`,
  `field`, `code`): `field` (`TITLE`/`DESCRIPTION`/`IMAGE`/`ATTRIBUTE`/
  `SIZE_CHART`), `how_to_solve`, `quality_tier` (the tier the product
  **reaches if you fix this issue**, not its current tier), and
  `suggestion_json` (TikTok's raw `suggestion` object for that field, if it
  sent one)

## Notes

`tiktok_creators_sync.py` exists because TikTok's video API and order API
expose different halves of a creator's identity with no shared join key — the
bridge closes that gap via the API plus an optional manual CSV import. The
`api` subcommand's crawl has a rolling window ceiling, and the write logic
switches behavior based on how far it got: a partial crawl **merges** into
existing rows, while a complete crawl **replaces** them outright — see the
module docstring before assuming every run behaves the same way.

`tiktok_creator_marketplace_sync.py` answers "is this creator a category fit?"
BEFORE you spend on a sample. Your own orders only show what a creator sold for
you; TikTok's Marketplace profile carries their whole-shop
`category_gmv_distribution`. Handles come from `--handles`, `--handles-file` or
a read-only `--from-table`/`--handle-column` (for example the `handle` column
of `tiktok_creators`). It uses the `affiliate_seller` endpoints with the
ordinary seller token (the `affiliate_creator` namespace needs a creator-type
authorisation and is not used). Things to know before trusting the numbers:

- Search is fuzzy, so only an **exact case-insensitive username match** is
  accepted; anything else is stored as `found = 0`.
- Coverage is "what was asked": every looked-up handle gets a row (found or
  not) with `fetched_at`, and `--max-age-days` resume is keyed on that
  timestamp, never on missing rows.
- `category_gmv_distribution` values are **fractions** ("0.85" = 85%), and
  category id `-1` is the uncategorised bucket (`uncategorised_share`).
  `--group NAME=ID,ID` sums chosen top-level category ids into
  `group_pct_json`; the ids to use are in `tiktok_category_top`, refreshed each
  run. TikTok has no top-level category for niche segments, so a group share
  cannot isolate one that sits inside a broader category.
- `gmv_band` / `units_band` are bands (`$150K+`), not numbers, and an empty
  category list usually means the creator has not authorised data sharing —
  not that they are a poor fit.
- Five consecutive request errors stop the run with exit code 75 and a
  `degraded` `sync_log` row (platform `tiktok_creator_marketplace`); a run
  where no handle matched at all is also `degraded`, never `ok`.

`tiktok_shop_performance` (from `tiktok_analytics_sync.py`) is a cleaner
alternative to estimating "unattributed" sales by subtraction. It chunks
requests by a fixed window length (`CHUNK_DAYS`) to stay under the
"date range too wide" error, and treats that same business error code as a
retention-window miss when it shows up mid-backfill — reporting and skipping
the chunk rather than failing the whole run. It also drops the most recent
day or two of unsettled data via `latest_available_date`, so don't expect
today's numbers to be final yet.

`tiktok_live_sync.py`'s per-product funnel needs two endpoints: the cheap
"list today's top sellers" endpoint has no content-type breakdown and
silently ignores a `live_id` filter, so it's only used to find which product
ids sold anything that day (GMV-sorted, stopping at the first $0 product);
the per-product detail endpoint is then called once per candidate id to get
the actual LIVE/VIDEO/PRODUCT_CARD split. `tiktok_shop_live_products` is
DATE-grain, not session-grain — the detail endpoint buckets by calendar day,
so multiple same-day broadcasts collapse into one row and per-broadcast
product performance can't be split back out of this feed alone. By default
the products crawl only covers days your own account
(`TIKTOK_LIVE_OWN_ACCOUNT_TYPE`) is recorded as having gone live in
`tiktok_shop_lives`, not every day in the window — pass `--dates` to force
specific days regardless. The script also runs a reconciliation sanity-check
(`reconcile()`) comparing each day's summed per-product LIVE gmv against that
day's session-level GMV, flagging ratios outside a 0.70–1.20 band rather than
expecting an exact match.

The per-product LIVE crawl in `sync_live_products` commits **one day at a
time** as soon as that day's product scan finishes, and wraps each
individual product lookup in its own retry/skip — a product that exhausts
its retries is logged and skipped rather than aborting the whole window. An
earlier version buffered every day's rows in memory and wrote them all at
the very end, so one flaky call late in a multi-day backfill discarded
everything already fetched. The retry predicate also treats any bare HTTP
`>= 500` from the per-product detail endpoint as transient, not just
TikTok's documented rate-limit codes.

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

**TikTok `orders.total` is not net sales.** Each TikTok line's `sale_price`
(stored as `total`) nets out two discounts: `seller_discount` (your own coupon
or flash sale) and `platform_discount` (a voucher TikTok funds and pays you
back at settlement), so `sale_price = original_price - seller_discount -
platform_discount`. The orders connector stores both per line in the
`orders.seller_discount` / `orders.platform_discount` columns (summed across
unit-lines of the same sku; NULL on other platforms and on rows synced before
the columns existed — re-pull TikTok order history to populate them). Net
sales = `total + platform_discount`. The `tiktok_weekly_product` view applies
that per line, falls back to the settlement feed's order-level
`platform_discount_amount` split pro rata across the order's lines for rows
without the line-level figure, and otherwise leaves `total` unmodified —
never assuming zero. Its `pd_known_sales` column shows how much of `net_sales`
was actually measured, so a coverage gap can't masquerade as a demand change.

`tiktok_listing_quality_sync.py` has no catalog table to source an
active-product-id list from — pass `--product-ids`/`--product-ids-file`
explicitly. The diagnosis endpoint takes up to 200 ids per call, and one bad
id (e.g. a deleted product) fails the whole batch — this script halves a
failing batch recursively down to singles so one bad id doesn't discard the
other ~199 products' results, and logs `degraded` (never `ok`) if any id
still failed after halving all the way down.

This is TikTok's own listing-*quality* tier, based on content checks (title,
description, images, attributes, size chart). It is **not** the customer
star rating. Each diagnosed product is a snapshot of the latest result: its
rows in `tiktok_listing_quality_issues` are deleted and rewritten on every
diagnosis, so a fixed issue drops out. A product that fails even on its own
keeps whatever rows an earlier run wrote, so check `synced_at` before you
trust a row as current. If the `seller.product.optimize` scope is missing,
every call fails with permission denied. The halving logic can't tell a
missing scope from a bad id, so it keeps splitting every batch down to
single ids, which is about 2× as many requests as you have ids, before
giving up. Use `--limit` for a first run so a missing scope fails fast. When
nothing gets written, the run logs `error` in `sync_log` (platform
`tiktok_listing_quality`) and exits 1.
Running without `--product-ids`/`--product-ids-file` exits right away with a
message and writes nothing to `sync_log`. Unlike the Amazon counterpart,
there is no fallback id list.

## Tests

`tests/test_tiktok_videos_sync.py`, `tests/test_tiktok_live_sync.py`,
`tests/test_tiktok_creators_sync.py`, `tests/test_tiktok_analytics_sync.py`,
`tests/test_tiktok_finance_sync.py`, `tests/test_tiktok_listing_quality_sync.py`,
`tests/test_tiktok_shop_connector.py` (per-sku summing of `seller_discount`/
`platform_discount` across unit-lines, and that `upsert_orders()` stores them
while leaving them `NULL` for connectors that never set them),
`tests/test_tiktok_net_sales_view.py` (the `tiktok_weekly_product` precedence:
line-level `platform_discount` wins, then the settlement pro-rata split, then
raw `total` — never zero by default)
