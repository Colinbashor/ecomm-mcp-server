# Google Merchant Center

Product feed performance (organic vs. paid, plus account-wide
non-product-specific performance), feed eligibility/issues, price
competitiveness, category best-sellers, and competitive visibility — pulled
from the Merchant API v1 (`merchantapi.googleapis.com`, `reports:search`).

**Script:** `merchant_center_sync.py` (standalone — not wired into `run_sync.py`;
creates its own tables via `ensure_schema()` on every run)

Two write-capable companions publish Google Sheets that Merchant Center
fetches: `merchant_center_labels_export.py` (custom labels as a supplemental
feed) and `gmc_promotions_sync.py` (Shopify codes as promotions). See
[Write-capable: custom labels and promotions](#write-capable-custom-labels-and-promotions).

## Setup

1. Use the same kind of service-account credential as [GA4](ga4.md) — you can
   reuse the same JSON key if you enable the Content API scope for it (the
   script requests `https://www.googleapis.com/auth/content`).
2. Enable `merchantapi.googleapis.com` for that Cloud project.
3. You must **also** run a one-time `registerGcp` API call before anything
   works — there is no Merchant Center UI for this step:

   ```
   POST https://merchantapi.googleapis.com/accounts/v1/accounts/{merchantId}/developerRegistration:registerGcp
   {"developerEmail": "<a human Google account with admin on the merchant account>"}
   ```

   Until it runs, **every** endpoint returns `401 GCP_NOT_REGISTERED`, which
   reads exactly like a permissions problem and is not one. The registering
   identity needs merchant-account admin only for this one call (registration
   survives a later downgrade), and a Cloud project can be registered against
   exactly one merchant account at a time.
4. Grant the service account access to the merchant account: Merchant Center
   → Settings → Account access → add the service account's email as a
   Standard user (Admin isn't needed for day-to-day reporting).
5. Set the environment variables (both are listed in `.env.example`):

   | Variable | Notes |
   |---|---|
   | `GMC_MERCHANT_ID` | your Merchant Center account id (numeric) |
   | `GMC_CREDENTIALS_FILE` | path to the service-account JSON key |

   **Unlike most scripts in this repo, `merchant_center_sync.py` does not call
   `load_dotenv()` itself** — it reads `os.environ` directly. Putting the two
   values in `.env` is only enough if whatever launches the script (your
   scheduler, a wrapper, your shell profile) exports them; otherwise set them
   in the environment before running.

   If either is missing the script does **not** skip quietly like the
   `run_sync.py` connectors — it exits immediately (`SystemExit`, exit code 1)
   with a message naming both variables and pointing at the `registerGcp`
   step. Nothing is written to `sync_log` in that case.

`WAREHOUSE_DB` (optional, repo-wide) overrides the database path as usual.

## Usage

```bash
python merchant_center_sync.py                        # last 3 days (default), all five families
python merchant_center_sync.py --days 30
python merchant_center_sync.py --start 2026-01-01 --end 2026-01-31
python merchant_center_sync.py --only performance --only pricing
python merchant_center_sync.py --only performance --backfill              # walk performance history back until it runs dry
python merchant_center_sync.py --only performance --backfill --start 2025-03-01   # resume a paused backfill
python merchant_center_sync.py --only bestsellers --category 1604 --country US --top-n 50
python merchant_center_sync.py --category 166:"Apparel & Accessories" --country US --country GB
python merchant_center_sync.py --only bestsellers --brand "Your Brand" --brand "Competitor"
python merchant_center_sync.py --only bestsellers --report-date 2026-08-24   # manual backfill of one date
```

| Flag | Default | Meaning |
|---|---|---|
| `--days N` | `3` | Window length in days ending today (values below 1 are treated as 1). Used by `performance` and `visibility`. |
| `--start YYYY-MM-DD` | `end − (days − 1)` | Explicit window start. With `--backfill`, it is instead the month to start walking back **from** (the resume point). |
| `--end YYYY-MM-DD` | today | Explicit window end. Ignored by `--backfill`. |
| `--only FAMILY` | all five | One of `performance`, `status`, `pricing`, `bestsellers`, `visibility`. Repeatable — pass it once per family. |
| `--backfill` | off | Walk **product** performance back a calendar month at a time (see Notes). Only affects the `performance` family; other selected families still run normally, so combine with `--only performance` for a pure backfill. |
| `--category SPEC` | `166` (Apparel & Accessories) | Google product-taxonomy id, as `id` or `id:Name` (the name is cosmetic — it is not stored). Repeatable. Scopes `bestsellers` and `visibility`. |
| `--country CC` | `US` | ISO country code. Repeatable. Scopes `bestsellers` and `visibility`. |
| `--brand NAME` | none | Brand to fetch explicitly from the brand best-sellers view, even far outside the top-N. Repeatable. `bestsellers` only. |
| `--top-n N` | `200` | Size of the rank-ordered cut for product and brand best-sellers. |
| `--report-date YYYY-MM-DD` | none | `bestsellers` only: pull exactly this `report_date` (a Monday for WEEKLY, the 1st for MONTHLY) instead of the current report. **Skips the automatic heal pass** for that run. The same date is queried for both granularities, so a Monday that isn't a 1st simply returns nothing for MONTHLY. |

Family → what each one queries:

| Family | Merchant API view(s) | `sync_log` platform |
|---|---|---|
| `performance` | `product_performance_view` + `non_product_performance_view` | `gmc_performance` |
| `status` | `product_view` | `gmc_status` |
| `pricing` | `price_competitiveness_product_view` | `gmc_pricing` |
| `bestsellers` | `best_sellers_product_cluster_view` + `best_sellers_brand_view` | `gmc_bestsellers` |
| `visibility` | `competitive_visibility_competitor_view` (one query per country × category × traffic source `ALL`/`ADS`/`ORGANIC`) | `gmc_visibility` |

## Tables

Every table has a `synced_at` column (UTC timestamp of the run). All writes
are `INSERT OR REPLACE` on the primary key (upsert) unless noted; INSERT
columns are always named.

| Table | Primary key | Columns | Write semantics |
|---|---|---|---|
| `gmc_product_performance` | `(date, marketing_method, customer_country_code, offer_id)` | `date`, `marketing_method` (`ORGANIC` / `ADS`), `customer_country_code`, `offer_id`, `product_id`, `variant_id`, `title`, `brand`, `clicks`, `impressions`, `click_through_rate`, `conversions` | Upsert, committed in batches of 5,000. Indexed on `date`. Re-pullable history. |
| `gmc_account_performance` | `(date)` | `date`, `week_start`, `clicks`, `impressions`, `click_through_rate` | Upsert. Account-wide, non-product-specific surfaces. Not populated by `--backfill`. |
| `gmc_product_status` | `(gmc_id)` | `gmc_id`, `offer_id`, `feed_label`, `product_id`, `variant_id`, `title`, `brand`, `price_micros`, `price_currency`, `availability`, `aggregated_status` (`ELIGIBLE` / `ELIGIBLE_LIMITED` / `NOT_ELIGIBLE_OR_DISAPPROVED` / `PENDING`), `n_issues` | **Current state only**, not history: upserted in batches of 5,000, then — only after a complete pass — every row with an older `synced_at` is deleted, so delisted products drop out. |
| `gmc_product_issues` | `(gmc_id, issue_code)` | `gmc_id`, `issue_code` (`unknown` if absent), `severity`, `resolution`, `offer_id` | Same as `gmc_product_status`: current state, stale rows pruned after a complete pass (so a resolved issue disappears). |
| `gmc_price_competitiveness` | `(snapshot_date, gmc_id, report_country_code)` | `snapshot_date` (the run's local date), `gmc_id`, `offer_id`, `product_id`, `variant_id`, `title`, `brand`, `report_country_code`, `price_micros`, `price_currency`, `benchmark_micros`, `benchmark_currency` | Upsert, one snapshot per day. The view has no date of its own, so **history exists only because daily runs stored it — it cannot be backfilled.** |
| `gmc_best_sellers` | `(report_date, report_granularity, report_country_code, report_category_id, rank)` | those five plus `previous_rank`, `title`, `brand`, `relative_demand`, `relative_demand_change`, `inventory_status`, `pull_reason` (`top_n` / `stocked` / `riser`) | Upsert. Rows with no `rank` are dropped. |
| `gmc_best_seller_brands` | `(report_date, report_granularity, report_country_code, report_category_id, rank)` | those five plus `previous_rank`, `brand`, `is_tracked_brand` (1 if `brand` case-insensitively matches a `--brand`), `relative_demand`, `previous_relative_demand`, `relative_demand_change`, `pull_reason` (`top_n` / `tracked_brand`) | Upsert. Rows with no `rank` are dropped. |
| `gmc_best_seller_coverage` | `(report_granularity, report_country_code, report_date)` | those three plus `rows_returned`, `is_present` (0 if Google had nothing for that date yet), `attempts`, `first_asked_at`, `last_asked_at` | Written only by the heal pass: insert on first ask, then `rows_returned`/`is_present`/`last_asked_at` overwritten and `attempts` incremented on each re-ask. An audit trail of what was asked — not meant for direct querying. |
| `gmc_competitive_visibility` | `(date, report_country_code, report_category_id, traffic_source, domain)` | those five plus `is_your_domain` (0/1), `rank`, `ads_organic_ratio`, `relative_visibility` | Upsert. |

Money is always stored as integer micros plus a currency code, never a bare
float, since a multi-country feed can mix currencies.

`product_id` / `variant_id` are parsed from `offer_id` only when it has the
Shopify Google & YouTube channel shape `shopify_<locale>_<productId>_<variantId>`;
any other `offer_id` scheme leaves both `NULL` (see "THE ID BRIDGE" in the
module docstring).

## Notes

**Best-sellers rows are a union of three queries.** For each country ×
granularity (`WEEKLY` and `MONTHLY`) × category, `sync_best_sellers()` runs a
top-N-by-rank query, a "stocked" query (`inventory_status != 'NOT_IN_INVENTORY'`,
i.e. products you carry, at any rank), and a "riser" query
(`relative_demand_change = 'RISER'`, products gaining demand outside the usual
top-N cut). When the same rank comes back from more than one query,
`pull_reason` keeps the most specific: `stocked` > `riser` > `top_n`. A gap in
the rank sequence is therefore expected, not missing data. The brand view runs
a top-N query plus, when `--brand` is given, a `brand IN (...)` query with the
names exactly as you typed them; `tracked_brand` beats `top_n`.

`gmc_best_sellers`/`gmc_best_seller_brands` are a **market ranking**, not your
own sales data, and a plain (unfiltered) query only ever returns whichever
report is *current* — so a sync that misses a run can permanently lose
whichever `report_date` was current on exactly that day once a newer one
replaces it. The API rejects range filters (`>=`, `BETWEEN`) on `report_date`,
but an exact `report_date = '...'` filter works. Every normal `bestsellers`
sync (i.e. without `--report-date`) therefore also runs `heal_best_sellers()`:

1. For each country and granularity it lists candidate `report_date`s —
   WEEKLY Mondays up to `BEST_SELLER_HEAL_WEEKS` (8) back, MONTHLY
   1st-of-months up to `BEST_SELLER_HEAL_MONTHS` (3) back — keeping only
   dates at least `BEST_SELLER_DUE_DAYS` (14) old, since Google publishes
   these reports well in arrears.
2. It compares them with the distinct `report_date`s already in
   `gmc_best_sellers` for that country + granularity. No gap → zero extra API
   calls.
3. Each missing date is re-pulled on its own (products and brands, all
   categories), oldest first, and the ask is recorded in
   `gmc_best_seller_coverage` with its row count.

A date Google hasn't published yet comes back empty, is recorded with
`is_present = 0`, and — because "held" is judged from `gmc_best_sellers` —
is asked again on each later run (bumping `attempts`) until it either appears
or ages out of the heal window. The window is the give-up guard, so a date
Google never publishes is not re-asked forever. Tune the three constants in
`merchant_center_sync.py` if your account's publish lag differs. For a
manual, targeted backfill of one known date, use `--report-date` directly.

The one hard rule this all depends on: never batch more than one
`report_date` into a single query — the API silently accepts
`report_date IN (...)` but applies the top-N `LIMIT` across the *combined*
result set, quietly halving each date's row count with no error (see
"HEALING A MISSED report_date" in the module docstring).

**Healing is not per category.** "Already held" is decided per
`(country, granularity, report_date)` — if *any* category has rows for a
date, the date counts as held. If you add a new `--category` after the
database already holds some other category's rows for a date, the heal pass
will not backfill the new category's history for it — use `--report-date`
manually for each missing date.

**Visibility publishes several days late.** When you don't pass `--start` or
`--end`, the visibility window is shifted back by `VISIBILITY_LAG_DAYS` (4):
it ends 4 days ago and spans `--days` days. Without that shift the default
window would sit entirely inside the unpublished gap and return zero rows
every run. Verify the lag for your own account and adjust the constant. An
explicit `--start`/`--end` is used as-is (no shift). The `ALL` / `ADS` /
`ORGANIC` traffic sources give genuinely different competitor orderings —
compare within one `traffic_source`, never mix them.

**No revenue column, on purpose.**
`gmc_product_performance`/`gmc_account_performance` carry
clicks/impressions/conversions only — see the module docstring's
`conversion_value` trap: selecting a money-valued conversion field from this
API implicitly segments results by currency, silently splitting one logical
(date, product) row into several with the non-selected currency's numbers
reading as zero. Get conversion *value* from whichever ads-platform connector
already reports attributed revenue instead. `click_through_rate` is a ratio —
average it, never sum it.

**Status and pricing coverage.** `gmc_product_status` is a full-catalog
snapshot that is pruned to the latest pass; a partial run (exception
mid-pull) skips the prune, so it never wipes rows it simply didn't reach.
`gmc_price_competitiveness` only covers products Google has benchmark data
for — an absent product means "no benchmark", not "priced at market".

**Backfill.** `--backfill` calls `_backfill_months()`, which walks
`product_performance_view` backwards one calendar month at a time from the
current month (or from the month containing `--start`), committing as it
goes. It stops after two consecutive empty months or at `BACKFILL_FLOOR`
(2018-01-01). If a transient failure can't be ridden out, it stops cleanly,
prints the exact resume command (`--only performance --backfill --start
<month>`), logs `gmc_performance` as `degraded` with
`backfill paused (throttled); resume --start <date>`, and the process exits
with code **75** ("paused, not failed") — relevant if your scheduler treats
any nonzero exit as an alert. Account-level performance is not backfilled.

**Retries and errors** (`Client.search`):

- Pages always request `pageSize` 10,000; each HTTP call has a 300 s timeout.
- `401` → the access token is re-minted once and the request retried.
- `408` / `429` (throttling) → waits 30, 60, 120, 240, 300 s, then gives up
  with `GmcTransient`.
- `5xx` or a network error → up to 5 tries with a linear 5 s × attempt
  backoff, then `GmcTransient`.
- Any other `4xx` (bad field, view, enum or date) is permanent → raised
  immediately as `GmcQueryError`, no retry.
- An out-of-range date window is HTTP 200 with zero rows, not an error.

How those surface per family: in `bestsellers` and `visibility`, a
`GmcQueryError` on one query variant is printed as `skipped` and the rest of
the pull continues. In `performance`, `status` and `pricing` (and for any
`GmcTransient` outside a backfill) the exception fails that family: it logs
`error` with the message and the script moves on to the next family.

**`sync_log` and exit codes.** Each family logs one row under the platform
names in the Usage table. **Every family logs `"degraded"` (never `"ok"`) on
a pull that returned zero rows** (message: "request succeeded but returned NO
rows — an empty pull is not a healthy pull"), via `_log_grain()`. A run of
"ok, 0 rows" would read identically whether nothing changed today or the feed
has silently stopped advancing. This does *not* change the exit code — an
empty pull isn't a resumable pause; it's a downstream health check's job to
decide when a streak of empty pulls is an error. Exit codes: `1` if any
family raised (takes precedence), `75` if a backfill paused, otherwise `0`.

## Write-capable: custom labels and promotions

Two more scripts **write** to Merchant Center, indirectly: each replaces the
whole content of a Google Sheet that Merchant Center fetches on its own
schedule. Both are dry runs unless given `--execute`, both upload through
`warehouse/drive.py` (bounded retry on transient Drive faults; a permanent 4xx
fails at once), and both authenticate with `GMC_CREDENTIALS_FILE`.

**One-time Google setup for both:**

1. In the Cloud project of the `GMC_CREDENTIALS_FILE` key, enable the
   **Google Drive API**.
2. Create each Google Sheet, share it with the service account's email
   (`name@project.iam.gserviceaccount.com`) as an **Editor**, and copy its
   file id from the URL (`docs.google.com/spreadsheets/d/<file id>/edit`).
   Drive answers 404, not 403, for a file the account cannot see.
3. Set `GMC_FEED_ID_FORMAT` to match your **primary feed's** item ids. Open a
   product in Merchant Center (Products → All products) and read its id:

   | Primary feed id looks like | `GMC_FEED_ID_FORMAT` |
   |---|---|
   | `shopify_US_1000000000001_1000000000101` (Shopify's Google & YouTube channel) | `shopify` (set `GMC_TARGET_COUNTRY` to the country in the id) |
   | `1000000000101` (the Shopify variant id) | `variant_id` |
   | `SKU-1001` | `sku` |

   There is no default. A supplemental row whose id is not in the primary feed
   is ignored without an error, so a wrong format produces a feed that looks
   fine and does nothing. After the first push, check that the labels appear
   on a few products before relying on them.

### `merchant_center_labels_export.py` — custom labels supplemental feed

Writes `id, custom_label_0 … custom_label_4` for every ACTIVE Shopify variant
to the Sheet `GMC_LABELS_SHEET_ID`. Labels come from `feed_labels.py` (shared
with [Meta](meta-ads.md#write-capable-meta_catalog_labels_syncpy--custom-labels-on-a-catalog)):

| Label | Meaning | Values |
|---|---|---|
| `custom_label_0` | net margin: `(price − unit_cost − FEED_LABEL_SHIP_COST) / price − FEED_LABEL_DEDUCTION_RATE` | `Margin 50%+`, `Margin 40-50%`, `Margin 30-40%`, `Margin 20-30%`, `Margin 10-20%`, `Margin 0-10%`, `Negative margin`, `Unknown margin` (cost missing, or above 2× price, which is treated as a broken cost record) |
| `custom_label_1` | markdown: price vs `compare_at_price` | `Full price`, `Light markdown (<25% off)`, `Deep markdown (25%+ off)` |
| `custom_label_2` | launched within `FEED_LABEL_NEW_ARRIVAL_DAYS` (default 30), from `launch_date_epoch` | `New arrival`, `Established`, blank when the launch date is unknown |
| `custom_label_3` | units over the last `FEED_LABEL_VELOCITY_DAYS` (default 30) of `orders` for `FEED_LABEL_ORDER_PLATFORMS` (default `shopify`), ranked among SKUs that sold | `Top seller` (top 10% of selling SKUs), `Steady seller` (to the top 50%), `Slow seller`, `No recent sales`; blank for every product when the window has no orders at all (the order feed is not syncing, which is not the same as nothing selling) |
| `custom_label_4` | reserved, always blank | |

Velocity is ranked rather than cut at fixed unit counts so the bands mean the
same thing at any store size; SKUs tied on units always share a band. Orders
are matched to the catalog on SKU (trimmed, case-insensitive) with no status
filter. `launch_date_epoch` is filled only if you map it with
`SHOPIFY_CATALOG_METAFIELDS` (see [catalog.md](catalog.md)). Band cut points
are constants at the top of `feed_labels.py`; Google Ads listing groups match
the label **text**, so renaming a band breaks any filter that uses it.

**Merchant Center step (once):** Products → Feeds (or Settings → Data
sources) → *Add supplemental source* → Google Sheets → choose "existing
sheet" and pick this Sheet → attach it to your existing primary feed → set a
daily fetch time after your scheduled run. Do **not** add it as a primary
source.

```bash
python merchant_center_labels_export.py                 # dry run: label counts + samples
python merchant_center_labels_export.py --csv out.csv   # dry run, also write the rows
python merchant_center_labels_export.py --execute       # push the sheet
```

**Guards.** Only ACTIVE products are labelled (an archived duplicate shares
its live product's SKU). With `GMC_FEED_ID_FORMAT=sku`, a SKU held by more
than one ACTIVE variant is dropped rather than guessed, and the dry run
reports how many rows were dropped. A build with fewer than
`GMC_LABELS_MIN_ROWS` (default 100) rows is refused and the Sheet left as it
was: an empty catalog table or a mistyped setting produces a tiny build, and
pushing it would replace every good label with nothing. Set the floor to a
comfortable fraction of your normal row count.

**`sync_log`** (platform `gmc_labels_export`, `--execute` only): `ok` with the
rows pushed, or `error` (refused, build failed, push failed) with exit 1.

**Blank cells.** `custom_label_4` and unknown labels go out as empty cells.
How Merchant Center treats an empty supplemental value (ignore vs. clear) is
not verified here; if your primary feed already sets a custom label, check a
product after the first push.

### `gmc_promotions_sync.py` — Shopify codes as Merchant Center promotions

Reads live and scheduled Shopify code discounts and publishes the ones Google
accepts as promotions, so codes appear on Shopping ads and free listings
without being re-typed into Merchant Center.

- **Which codes:** one shared code, for all customers, no usage limit, not
  once-per-customer, ending in the future. Single-use, generated and
  segment-only codes are skipped and the dry run prints why per reason. Buy X
  get Y codes and codes scoped to specific products are skipped (not mapped).
  `--min-hours` (default 0) skips short windows; `--lookahead-days` (default 14)
  includes scheduled codes starting soon.
- **Which products:** every promotion is `specific_products`. Products are
  tied to it through a `promotion_id` column on a second, **supplemental**
  Sheet (`id, promotion_id`), so a bad push can never delist a product.
  Eligible = every ACTIVE product, or only those tagged
  `GMC_PROMO_ELIGIBLE_TAG` when set. Set the tag when checkout excludes some
  products from codes: Google disapproves a promotion whose code fails on a
  product it is shown with. A collection-scoped code is accepted only if its
  collection is exactly `GMC_PROMO_COLLECTION_HANDLE`, which must hold the same
  products as the tag (for example a smart collection on that tag). Google
  allows at most 10 `promotion_id` values per product; the newest codes win.
- **Order and clearing:** inclusions are pushed before promotions. With no
  live codes, both Sheets are pushed header-only; replacing the whole Sheet is
  how an expired promotion leaves Merchant Center.
- **Guard:** a live code with an empty eligible set is refused (`error`,
  nothing pushed).

**Shopify scopes:** `read_discounts` (older apps: `read_price_rules`) and
`read_products`, added to the app the [Shopify connector](shopify.md) uses;
release a new app version and reinstall after adding scopes.

**Merchant Center steps (once):**

1. Marketing → Promotions must be enabled for the account (Google may require
   accepting the promotions program terms).
2. Promotions → *Add promotions data source* → Google Sheets → this script's
   `GMC_PROMO_SHEET_ID` Sheet, daily fetch.
3. Add `GMC_PROMO_INCLUSIONS_SHEET_ID` as a **supplemental** product source on
   the primary feed, as for the labels Sheet above.
4. Schedule this script shortly before both fetch times.

```bash
python gmc_promotions_sync.py                                  # dry run (default)
python gmc_promotions_sync.py --out-dir reports/promotions     # dry run + both CSVs
python gmc_promotions_sync.py --execute                        # push both sheets
python gmc_promotions_sync.py --min-hours 6 --lookahead-days 7
```

Settings: `GMC_TARGET_COUNTRY` (default `US`), `GMC_PROMO_CURRENCY` (`USD`),
`GMC_PROMO_CONTENT_LANGUAGE` (`en`), `GMC_PROMO_DESTINATIONS`
(`Shopping_ads,Free_listings`), `GMC_PROMO_SKIP_TITLE_PREFIXES` (a search
pre-filter for generated code families; the rules above still apply). The
promotion id is `SHOP_<CODE>_<start date>`, so a re-run updates the same
promotion and a code reused later gets a new one.

**`sync_log`** (platform `gmc_promotions_sync`, `--execute` only): `ok` with
the promotion count (zero live codes is a measured result, still `ok`), or
`error` on a Shopify, build or push failure, with exit 1.

## Tests

`tests/test_merchant_center_labels_export.py`, `tests/test_gmc_promotions_sync.py`,
`tests/test_feed_labels.py` and `tests/test_drive.py` — label bands, rank-based
velocity, blank-vs-zero handling, SKU ambiguity and each feed-id format; the
row floor, dry-run default and `sync_log` status of the export; code
classification, promotion rows, the eligible set and push order of the
promotions sync; Drive retry and whole-sheet replacement.

`tests/test_merchant_center_sync.py` — field coercion (`as_int`, `as_float`,
`as_date`, `as_money`, `parse_offer_id`), transport retry/throttle/401
behaviour, row shapes written by each family, `_log_grain` ok-vs-degraded,
best-seller heal candidates / missing-date diff / `report_date` clause /
`heal_best_sellers()`, and the missing-env-var guard in `main()`.
