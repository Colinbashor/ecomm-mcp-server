# TikTok Shop

Order ground truth (core), plus seven standalone scripts (and a one-off video-products backfill) for video
performance, LIVE-shopping, creator identity and Marketplace profiles, sales-source attribution,
settlement/fee data, and listing-quality diagnostics.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/tiktok_shop.py` | core, via `run_sync.py --only tiktok` | orders into `orders` |
| `tiktok_videos_sync.py` | standalone | video performance + every product each video tags |
| `tiktok_video_products_backfill.py` | standalone, one-off | month-by-month history of every product each video tags |
| `tiktok_live_sync.py` | standalone | LIVE-shopping broadcast + product funnel |
| `tiktok_creators_sync.py` | standalone | handle ↔ display-name ↔ user-id creator/affiliate identity bridge |
| `tiktok_creator_marketplace_sync.py` | standalone | per-creator Marketplace profile (whole-shop category split, GMV/units bands, rating, commission) for handles you supply |
| `tiktok_affiliate_sync.py` | standalone | seller Affiliate API: per-SKU-line creator commission, sample applications, and a `creator_open_id` ↔ `orders.creator_id` crosswalk built through sample orders |
| `tiktok_analytics_sync.py` | standalone | true mutually-exclusive LIVE/VIDEO/PRODUCT_CARD sales-source split |
| `tiktok_finance_sync.py` | standalone | settlement statements + per-order fee decomposition |
| `tiktok_listing_quality_sync.py` | standalone | per-product content-quality tier + issues (Listings Diagnosis API) |
| `tiktok_promotions.py` | **WRITE-capable**, dry run unless `--apply` | fixed-price promotion activities: list/show/export, create, set product prices — see [Promotions](#promotions-tiktok_promotionspy) |

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

All the extras reuse the core `TIKTOK_*` credentials — nothing new to
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

python tiktok_video_products_backfill.py           # video x product history back to the API's lookback floor
python tiktok_video_products_backfill.py --status  # coverage per window (writes no data; creates the tables if absent)
python tiktok_video_products_backfill.py --start 2026-05-01 --end 2026-07-01   # explicit range (--end is exclusive)
python tiktok_video_products_backfill.py --account-types AFFILIATES --force    # re-pull windows already `complete`

python tiktok_live_sync.py                         # both lives + per-product funnel, trailing 30 days
python tiktok_live_sync.py --only lives             # or `products` -- skip the other half
python tiktok_live_sync.py --account-types ALL      # default OFFICIAL_ACCOUNTS,AFFILIATE_ACCOUNTS,MARKETING_ACCOUNTS
python tiktok_live_sync.py --dates 2026-01-05,2026-01-06   # force specific days for the products crawl

python tiktok_creators_sync.py api                  # or `import path/to/export.csv` for the manual path
python tiktok_creators_sync.py import --dir exports/   # import every .xlsx/.csv in a folder
python tiktok_creators_sync.py api --dry-run        # fetch + report, write nothing (also on `import`)

python tiktok_creator_marketplace_sync.py --handles creator_one,creator_two
python tiktok_creator_marketplace_sync.py --handles-file handles.txt --limit 200   # cap this run at 200 lookups
python tiktok_creator_marketplace_sync.py --from-table tiktok_creators --handle-column handle --group footwear=601352 --group bags=824584 --max-age-days 14

python tiktok_analytics_sync.py                     # trailing 30 days (default)
python tiktok_analytics_sync.py --start 2026-01-01 --end 2026-01-31
python tiktok_analytics_sync.py --dry-run           # fetch and report, write nothing

python tiktok_affiliate_sync.py orders --days 14            # trailing re-pull of affiliate order lines
python tiktok_affiliate_sync.py orders --start 2024-01-01   # backfill; skips windows already complete
python tiktok_affiliate_sync.py samples --list --lookup     # application statuses + per-order lookups
python tiktok_affiliate_sync.py samples --by-product --since 2024-01-01   # needs catalog_tiktok, or --product-ids
python tiktok_affiliate_sync.py samples --by-product --product-ids 1729000000001,1729000000002
python tiktok_affiliate_sync.py samples --lookup --since 2025-01-01 --max-lookups 500   # capped backfill chunk
python tiktok_affiliate_sync.py xwalk                       # rebuild the crosswalk only
python tiktok_affiliate_sync.py report --split 2025-01-01   # read-only coverage numbers

python tiktok_finance_sync.py                 # settlements, last 30 days
python tiktok_finance_sync.py --backfill      # 365-day window
python tiktok_finance_sync.py --no-components # statements only, fast
python tiktok_finance_sync.py --no-orders     # skip retaining per-order fee rows (statements + components only)

python tiktok_listing_quality_sync.py --product-ids 1234567890,2345678901
python tiktok_listing_quality_sync.py --product-ids-file product_ids.txt
python tiktok_listing_quality_sync.py --product-ids-file product_ids.txt --limit 400   # smoke test: first 400 ids
```

`tiktok_videos_sync.py` makes up to 6 attempts per page. One of them may be
spent refreshing an expired access token (once per page; a second expired-token
code fails immediately). Connection resets, timeouts, non-JSON bodies, HTTP
429/5xx and API codes 105050 / 105051 / 429000 sleep `2**attempt` seconds
(1, 2, 4, 8, 16, 32s; the last sleep happens even though no attempt follows)
and try again. A month of videos is hundreds of pages, so without this one
transient failure would abort the whole pull; any other non-zero API code still
fails immediately.

The orders connector (`warehouse/connectors/tiktok_shop.py`, driven by
`run_sync.py`) keeps two independent retry budgets per page request: the access
token is refreshed at most once (codes 105001 / 105002), and transient upstream
faults are retried up to 5 times with a growing sleep (15s, 30s, 45s, 60s, 60s).
Transient means business codes 98001021 / 36009003 (returned inside an HTTP 200),
connection resets, timeouts and non-JSON bodies. Every attempt rebuilds and
re-signs the request so the timestamp is fresh. Any other non-zero code raises
immediately, so a bad request is never masked as a hiccup. The retry keys on
the **body**, not the HTTP status: an HTTP 429/5xx that still returns a JSON
body is retried only if that body carries one of the transient codes above.

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `tiktok_shop_videos` — one row per video x account_type x window, holding
  only the FIRST tagged product (`product_id`) plus `product_count`. Written
  with `INSERT OR REPLACE`, so re-running the same window overwrites that
  window's metrics. That is intended: GMV and views for recent days keep
  climbing until TikTok finalizes them, which is why `--current-window` exists.
  Only `tiktok_videos_sync.py` writes this table; the backfill never does.
- `tiktok_shop_video_products` — EVERY product a video tags (PK `video_id`,
  `account_type`, `product_id`; `position` 1 = first; `first_seen` /
  `last_seen` = the earliest window_start / latest window_end that returned
  the tag). Cumulative, never deleted; name/position come from the LATEST
  window seen, so an older backfill window never overwrites a newer pull.
  Product entries carry only id + name: GMV / units / views are VIDEO-level,
  so never sum a video's metrics across its tagged products.
- `tiktok_shop_video_products_coverage` — one row per account_type x window
  ASKED for: `status` (`complete` / `short` = fewer distinct videos than the
  API's `total_count` / `empty` / `out_of_retention` = business code 28001022
  / `failed`), counts, `attempts`, `source` (`sync` / `backfill`). Written by
  both the daily sync and the backfill, but the daily sync only ever records
  `complete` / `short` / `empty`; `failed` and `out_of_retention` rows come
  from the backfill alone. The backfill resumes from this table, never from
  absent tag rows (a window can legitimately have no multi-product video).
- `tiktok_shop_lives`, `tiktok_shop_live_products`
- `tiktok_creators`
- `tiktok_creator_marketplace`, `tiktok_category_top`
- `tiktok_affiliate_order_lines` (PK `order_id`, `line_no`) — one row per
  affiliate order SKU line: `creator_username` (handle at pull time),
  `content_type` / `content_id`, `commission_rate_pct`,
  `shop_ads_commission_rate_pct`, `est_*` / `act_*` commission base, paid
  commission, Shop Ads, partner and co-funded bonus amounts (NULL until
  TikTok computes them, never 0), `settlement_status`, `fully_return`,
  `raw_json`. Each order is replaced whole on every pull.
- `tiktok_affiliate_order_coverage` — every 7-day orders window asked:
  `total_count` (SKU lines), `orders_returned`, `lines_written`, `status`
  `complete` / `short` / `failed`
- `tiktok_affiliate_sample_applications` (PK `application_id`) — sample
  `order_id` (NULL until approved), `status`, product / SKU, creator
  `creator_open_id` / `username` / `nickname`, `commission_rate_frac`
  (a FRACTION here), `source` (`list:<STATUS>` / `order_lookup`)
- `tiktok_affiliate_sample_coverage` (PK `kind`, `key`) — every status,
  product slice or order id asked, with `capped` (hit the 10k list
  ceiling), `found` and `attempts`
- `tiktok_creator_handle_history` — every (creator_open_id, handle) pair
  seen, with our own first/last-seen times
- `tiktok_sample_order_creator`, `tiktok_creator_xwalk` — derived, rebuilt
  every `samples` / `xwalk` run (see the note below)
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

## Promotions (`tiktok_promotions.py`)

Writes to the live shop; nothing is written without `--apply`, and `add`
re-reads the activity afterwards to verify every price. The app needs the
promotion read/write scope; credentials are the same as the order connector.

```bash
python tiktok_promotions.py list --status ONGOING
python tiktok_promotions.py export ACTIVITY_ID --out live.csv     # snapshot before editing
python tiktok_promotions.py create --title "Winter sale"     --begin 2026-12-01T00:00:00-08:00 --end 2026-12-07T23:59:59-08:00 --apply
python tiktok_promotions.py add ACTIVITY_ID --prices-file prices.csv            # dry run: diff vs live
python tiktok_promotions.py add ACTIVITY_ID --prices-file prices.csv --apply
```

`prices.csv` has `product_id,price`; the whole file is refused on any bad row
(a non-numeric id, which is what a spreadsheet makes of a long id, a
non-positive price, a duplicate). Only new or repriced products are sent.
Every write is appended to `reports/tiktok_promotions_log.csv`.

Traps (each one an error the API returned):

- An activity is created **empty**; products are attached with
  `PUT .../activities/{id}/products`, which needs `activity_id` in the body as
  well as the path. POST there answers "no schema found".
- At `product_level: PRODUCT` the id is the **product** id; a SKU id fails
  ("product IDs cannot be found"), and one price covers every SKU.
- The write takes the flat string `activity_price_amount`, not the nested
  `activity_price {amount, currency}` the read returns.
- `quantity_limit` and `quantity_per_user` are both required (`-1` = none).
- At most 20 products per call; code `36009037` is the rate limit (backed off).
- Re-adding a product **overwrites** it. That is how a price changes, and it
  also overwrites an edit someone made in Seller Center: `export` first.
- Adding to an `ONGOING` activity changes live prices immediately.
- Removing a product and deactivating an activity are not built; verify them
  on a throwaway activity before relying on them.

## Notes

### Video sync and video-products backfill: run behaviour

`tiktok_videos_sync.py`:

- Fetches **every** account type first and writes only afterwards, in one
  transaction. A failure in any account type therefore writes nothing (no
  video rows, no tag rows, not even a `failed` coverage row), logs `sync_log`
  `status='error'` (platform `tiktok_videos`) and exits `1` with the traceback.
- A successful run always logs `ok` with `rows_written` = video rows, even if a
  window's coverage came back `short` or `empty`. Check
  `tiktok_shop_video_products_coverage` (or the backfill's `--status`), not
  `sync_log`, to see whether a window was really complete.
- **`--gmv-positive-only` gotcha.** A winners-only run stops paging at the
  first zero-GMV video and still records that window as `complete`. The
  backfill treats any `complete` window as done, so it will never fill in that
  window's zero-GMV tail unless you pass `--force`. Don't use
  `--gmv-positive-only` on windows you later want full product-tag history for.

`tiktok_video_products_backfill.py`:

| Flag | Default | Meaning |
|---|---|---|
| `--start` | today − 178 days (the API's approximate lookback floor) | first day pulled |
| `--end` | tomorrow | **exclusive** end |
| `--account-types` | `AFFILIATES,LINKED_ACCOUNTS` | comma list |
| `--force` | off | re-pull windows already `complete` (also skips the floor stepping below) |
| `--status` | off | print the coverage table and per-account tag totals, then exit `0`. Needs no TikTok credentials. Writes no data, but does run `init_db` and creates the tables if they are missing. |

- The range is cut into calendar-month windows, oldest first, one pull per
  window x account type. A window is skipped when a `complete` coverage row
  (from either script) already spans it.
- **Retention floor.** TikTok refuses start dates older than its lookback
  (business code 28001022, stored as `out_of_retention`). Only the **first**
  window handles this: it steps its start forward one day at a time, at most
  10 days (`FLOOR_STEP_LIMIT`), and on later runs it starts past days already
  refused. An `out_of_retention` on any later window is final and never
  retried.
- **Poison guard.** A window that keeps coming back `short`, `empty` or
  `failed` is retried on later runs, up to 4 attempts (`MAX_ATTEMPTS`). After
  that it is skipped until you `--force` it.
- **Status and exit code** (`sync_log` platform
  `tiktok_video_products_backfill`): any `failed` window → `error` (exit `1`);
  otherwise any `short` / `empty` window → `degraded` (exit `75`); a run that
  neither completed nor skipped anything → `degraded`; else `ok` (exit `0`).
  Windows blocked by the poison guard count as "skipped", so a run where every
  remaining window is poisoned reports `ok`; read `--status` to spot them.
  `rows_written` is the **total** row count of `tiktok_shop_video_products`
  after the run, not the rows this run added. An uncaught exception logs
  `error` and re-raises.

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
- `--max-age-days` (default 30) skips handles asked within that many days,
  found or not. `--limit N` caps the lookups in one run (0 = no cap).
- The top-level category list (`tiktok_category_top`) is refreshed **before**
  any lookup. If that call fails the script raises (exit `1`) and writes no
  `sync_log` row.
- There is no dedicated 429/transient retry. Any exception on a handle counts
  as an error: the script sleeps 5s and moves to the next handle, and one
  success resets the count. Five consecutive errors stop the run with exit
  code `75` and a `degraded` `sync_log` row (platform
  `tiktok_creator_marketplace`). Rerun later; handles already asked are
  skipped by `--max-age-days`.
- A run where no handle matched at all is logged `degraded`, never `ok`, but
  still exits `0`. Only the error stop exits `75`.
- Rows are written with `INSERT OR REPLACE` on the handle, so a later lookup
  that finds nothing (`found = 0`) **replaces** an earlier `found = 1` profile
  for that handle. Rows are committed every 25 handles.

`tiktok_affiliate_sync.py` fills two gaps the Order API leaves. First,
per-line affiliate attribution: which creator handle and which content
earned each SKU line, at what commission rate, and the estimated and
actual commission. Second, a stable creator id for sample orders. The
Order API gives a sample order's creator only as a display name plus a
buyer `user_id` (`orders.creator_id`), while the affiliate endpoints key
creators by an opaque per-app `creator_open_id` — a different id space
that never compares equal. The sample order is the bridge: each sample
application carries the `order_id` it produced, so
`tiktok_sample_order_creator` maps order → application → `creator_open_id`
→ `orders.creator_id`, and `tiktok_creator_xwalk` keeps one row per
`creator_open_id` with the latest handle, the modal user id
(`tiktok_user_id_count > 1` is a conflict to investigate, not hidden),
handle count and sample-order dates. Things to know:

- Orders: TikTok caps a request at a 3-month `create_time` window, so the
  script tiles time into 7-day windows on ONE fixed grid anchored at
  `TIKTOK_SHOP_FLOOR` (default 2023-09-01). A daily `--days 14` run lands
  on the same windows as a backfill, so its coverage rows match and
  completed windows are skipped; windows ending within `--refresh-days`
  (60) are always re-pulled because commission and settlement keep
  changing for weeks (`--force` re-pulls everything). A window reporting
  at least `SPLIT_AT` lines is split in half before paging, and the parent
  window still gets its own coverage row.
- `page_size` and `total_count` count **SKU lines, not orders**, so one
  order can straddle two pages (lines are merged per order id) and
  coverage compares `total_count` with lines written. Paging stops on an
  empty page or a missing token, never on a short page.
- Units: the orders endpoint's commission rates are hundredths of a
  percent (`"800"` = 8%, stored as `*_pct`); the samples endpoint's is a
  fraction (`"0.10"`, stored as `commission_rate_frac`).
- Samples: the list has no date filter and only reaches the newest 10,000
  applications per query (offset 10,000 errors with 98001004; that is
  recorded as `capped`). `--list` pages each order-bearing status
  (`--all-statuses` adds the ones that never had an order). For older
  history, `--by-product` slices COMPLETED per product — it needs an
  optional `catalog_tiktok (seller_sku, product_id)` table (built by
  `catalog_sync.py`, see [catalog.md](catalog.md)) to turn the
  sample orders' seller SKUs into product ids, or explicit
  `--product-ids` — and `--lookup` asks for the application behind each
  unresolved sample order by `order_id` (one call each, newest first).
  Without `--since` both cover the last 30 days.
- An unfound sample order is re-asked at most 3 times, 7 days apart; a few
  genuinely have no application (samples sent outside the flow), so a run
  only turns `degraded` when misses exceed both 3 and 10% of lookups.
- The samples endpoint rate-limits (36009037) at roughly 35-45 calls/min,
  so calls are paced to ~25/min. A limit that survives every backoff
  pauses the run with exit `75` and a `degraded` `sync_log` row
  (`tiktok_affiliate_orders` / `tiktok_affiliate_samples`); re-running
  resumes from the coverage tables. A run that writes nothing is
  `degraded`, never `ok`.
- Suggested schedule: daily `orders --days 14` and `samples --lookup`
  (after the order sync that writes `orders`); weekly `orders --days 60`
  and `samples --list --lookup`.

`tiktok_affiliate_sync.py` flag reference (subcommand first, then flags):

| Subcommand / flag | Default | Meaning |
|---|---|---|
| `orders --days N` | 30 | trailing range ending today, used when `--start` is absent |
| `orders --start` / `--end` | — / today | `YYYY-MM-DD`; `--end` is inclusive. Both are widened outward to whole 7-day grid windows, so the range actually pulled is usually a little wider than asked |
| `orders --refresh-days N` | 60 | windows ending within the last N days are re-pulled even if `complete` |
| `orders --force` | off | re-pull every window in range |
| `samples --list` | — | page every order-bearing status (ignores `--since` / `--until`) |
| `samples --all-statuses` | off | with `--list`, also page `PENDING` and the three `*_CANCELLED` statuses |
| `samples --by-product` | — | COMPLETED slice per product id behind unresolved sample orders; a product already sliced once is never sliced again |
| `samples --product-ids A,B` | — | slice exactly these product ids instead of resolving them from `catalog_tiktok` |
| `samples --lookup` | — | one `order_id` lookup per unresolved sample order, newest first |
| `samples --since` / `--until` | 30 days ago / none | sample-order date range for `--by-product` / `--lookup`; `--until` is exclusive |
| `samples --max-products N` / `--max-lookups N` | 0 (no cap) | cap this run's slices / lookups (resume next run) |
| `xwalk` | — | rebuild `tiktok_sample_order_creator` + `tiktok_creator_xwalk` only; no API calls, no credentials needed |
| `report --split DATE` | 90 days ago | print coverage numbers as JSON; read-only, no credentials needed |

`samples` needs at least one of `--list`, `--by-product`, `--lookup`
(argparse error otherwise); when several are given they run in that order,
and every `samples` run ends by rebuilding the crosswalk. Products for
`--by-product` are asked newest unresolved sample order first. Optional
`tiktok_creator_marketplace` rows (exact-handle matches from
`tiktok_creator_marketplace_sync.py`) are folded into
`tiktok_creator_handle_history` on every rebuild.

Run behaviour, for schedulers and `sync_log` readers:

- Missing credentials: `orders` and `samples` exit immediately with
  "Missing required env var(s): ..." (exit 1) instead of being skipped
  quietly like the `run_sync.py` connectors. `xwalk` and `report` need none.
- Exit codes: `0` for a finished run, **including** `degraded` ones; `75`
  for a rate-limit pause; any other exception is logged `error` and
  re-raised (non-zero, traceback).
- `sync_log` platforms: `tiktok_affiliate_orders` (rows = lines written;
  `degraded` when any window came back `short` or nothing was written —
  including a run where every window was already complete, whose message
  says "nothing due"), `tiktok_affiliate_samples` (`degraded` when nothing
  was written, a `--by-product` slice hit the 10k cap, or lookup misses
  exceed the tolerance — a capped `--list` status alone does **not**
  degrade the run), and `tiktok_creator_xwalk` from `xwalk` (`degraded`
  when the crosswalk has zero creators).
- Window splitting only applies to windows longer than 6 hours that report
  at least `SPLIT_AT` (9,000) lines; halves are split recursively.
- `report` keys: `sample_lines_*`, `sample_orders_*`,
  `sample_lines_resolved_*` and `sample_line_open_id_share_*` (split
  `since_<date>` / `before_<date>` / `all`); `xwalk_sample_orders`,
  `user_id_present`, `user_id_eq_open_id` (must be 0 — non-zero means the
  two id spaces were mixed), `open_ids_per_user_id_gt1`,
  `user_ids_per_open_id_gt1`; and `affiliate_gmv_*` with the share that has
  a handle and the share whose handle the history knows, on both an
  `est_commission_base` and a `price * quantity` basis.

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

`tests/test_tiktok_videos_sync.py` (token refresh and a hard API error; the
transient-retry path has no dedicated test), `tests/test_tiktok_video_products.py` (every
tagged product fanned out with positions, coverage statuses, an older window
never overwriting a newer position, named-column upsert, month windows, the
backfill stepping past the retention floor, `out_of_retention` after the first
window being final, resuming from coverage not tag rows, the poison guard,
`run_status`, and never touching `tiktok_shop_videos`),
`tests/test_tiktok_live_sync.py`,
`tests/test_tiktok_creators_sync.py`, `tests/test_tiktok_analytics_sync.py`,
`tests/test_tiktok_finance_sync.py`, `tests/test_tiktok_listing_quality_sync.py`,
`tests/test_tiktok_promotions.py` (a dry run never writes, the add body shapes,
20-product batches, rate-limit backoff, one token refresh, verify-after-write),
`tests/test_tiktok_affiliate_sync.py` (paging past a short page and stopping
on an empty one, an order split across pages keeping every line, the 10k
sample ceiling recorded as capped, window splitting and the fixed grid,
lines-not-orders coverage, `{}` money stored NULL, the two commission
units, a renamed handle replacing rather than duplicating a line, lookup
retry/give-up, the crosswalk bridge and its conflicts, product slices
with and without `catalog_tiktok`, status/exit-75 handling, and the
signed request path),
`tests/test_tiktok_creator_marketplace_sync.py` (handle parsing/dedupe, exact-match
only lookup rejecting fuzzy hits, not-found rows still written, group-share sums,
resume by `--max-age-days`, consecutive-error stop, a success resetting the
error streak),
`tests/test_tiktok_shop_connector.py` (per-sku summing of `seller_discount`/
`platform_discount` across unit-lines, and that `upsert_orders()` stores them
while leaving them `NULL` for connectors that never set them; `FetchPageRetryTests`:
a transient code or a connection error retried then succeeding, the transient
budget exhausting and raising, a non-transient code failing fast, and the token
refreshed only once),
`tests/test_tiktok_net_sales_view.py` (the `tiktok_weekly_product` precedence:
line-level `platform_discount` wins, then the settlement pro-rata split, then
raw `total` — never zero by default)
