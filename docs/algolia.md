# Algolia (on-site search/browse)

On-site collection/category-grid **placement** (which slot each product
occupies, per collection, per day) plus **engagement** analytics
(impressions, click-through, add-to-cart rate, top queries) from
[Algolia](https://www.algolia.com) — only relevant if your storefront's
collection pages are rendered **client-side** against an Algolia index (a
common pattern for Shopify themes using `react-instantsearch` or similar,
but not universal). See the "WHEN THIS APPLIES" section at the top of the
module docstring for how to check whether this applies to your storefront
before setting it up.

**Script:** `algolia_sync.py` (standalone — not wired into `run_sync.py`, so
schedule it separately)

## Why this exists

Nothing else in this repo records **where** a product sits on a
collection/category page. That's a real gap if your merchandising team can
hand-pin products into specific grid slots via rules that get edited
same-day — the placement a product enjoyed on a given day becomes
unrecoverable the moment those rules change, unless it's snapshotted daily.
When your storefront is Algolia-rendered, querying the same index with the
same filter your storefront uses returns the exact order a shopper saw —
not an estimate.

Algolia's Analytics API separately answers "how did shoppers engage with
what they saw" — impressions, clicks, and (see the module docstring's trap
on this) usually add-to-cart rate rather than purchase rate, since most
storefronts don't wire purchase events through to Algolia.

## Setup

1. Find your Algolia app id and a **search-only** API key — these are
   commonly public by design (already shipped to every visitor's browser),
   visible in your storefront's own page source or Network tab requests to
   `*.algolia.net`.
2. Find the index that actually serves your storefront's default grid — a
   real Algolia application often has many indices (locale/sort-order
   variants, abandoned prior integrations) and usually only one has real
   traffic. `algolia_sync.py --probe` helps confirm which (see Usage).
3. (Optional, for the analytics grains) Get a privileged **Analytics** API
   key from your Algolia dashboard (API Keys section) — different from the
   search key above, and must stay out of any client-facing code.
4. Fill in `.env` (the four `ALGOLIA_*` variables are all present, blank, in
   `.env.example`):

   | Variable | Required | Notes |
   |---|---|---|
   | `ALGOLIA_APP_ID` | yes | your Algolia application id. The Search API host is derived from it: `<app id, lowercased>-dsn.algolia.net` |
   | `ALGOLIA_SEARCH_KEY` | yes | a search-only API key |
   | `ALGOLIA_INDEX` | yes | the index behind your storefront's default grid. Also sent as `index=` on every Analytics API call, so analytics are scoped to this one index |
   | `ALGOLIA_ANALYTICS_KEY` | no | privileged Analytics key (host `analytics.algolia.com`). Omit to skip the four analytics grains; placement still runs |
   | `WAREHOUSE_DB` | no | path to the SQLite warehouse (default `warehouse.db` in the repo root), shared with every other connector |

   The three required variables are checked before anything else runs,
   **including `--probe`**: if any is missing the script exits immediately
   (non-zero) with `Missing required env var(s): ... See .env.example.` and
   writes nothing — not even a `sync_log` row.

5. (Optional) Edit `algolia_collections.yaml` to list the collection/category
   handles you want tracked for placement, under `algolia.tracked_collections`:

   ```yaml
   algolia:
     tracked_collections:
       - "new-arrivals"
       - "best-sellers"
   ```

   It ships with those two placeholder examples — replace them with your
   own. For a Shopify-backed storefront a handle is usually the slug in the
   page URL (`/collections/new-arrivals` -> `new-arrivals`). The handle is
   matched against a facet attribute **hard-coded as `collections`**
   (`facetFilters: [["collections:<handle>"]]`), so your index must expose
   that attribute as a facet. If the file is missing or the list is empty,
   the placement grain has no collections to snapshot and prints a note
   instead of erroring (but see the A/B-test guard under Notes — the run can
   still be logged `degraded`).

## Usage

```bash
python algolia_sync.py --probe             # check reachability + traffic mix, no writes
python algolia_sync.py                     # daily: placement + last 3 days of analytics
python algolia_sync.py --days 30
python algolia_sync.py --start 2026-08-01 --end 2026-08-24
python algolia_sync.py --only placement
python algolia_sync.py --only hits --only positions
python algolia_sync.py --collections dresses,new-arrivals   # override the config file for one run
```

| Flag | Default | Effect |
|---|---|---|
| `--days N` | `3` | analytics window of N days **ending yesterday** (local date). Ignored when `--start` is given |
| `--start YYYY-MM-DD` | `end - (days - 1)` | analytics window start (inclusive) |
| `--end YYYY-MM-DD` | yesterday | analytics window end (inclusive) |
| `--only GRAIN` | all five | restrict to a grain; repeatable. Choices: `placement`, `hits`, `positions`, `searches`, `daily` |
| `--collections a,b,c` | `algolia_collections.yaml` | comma-separated handles for the placement grain, replacing the config file for this run |
| `--probe` | off | report reachability and write nothing (see below) |

Window behavior:

- `--days` / `--start` / `--end` only affect the four **analytics** grains.
  Placement always snapshots **today** (`snapshot_date` = the local date the
  script runs), whatever window you pass.
- A start older than 89 days before today is **clamped** to that floor with a
  `NOTE: start clamped to ...` line (Algolia's retention is ~90 days; the
  script stays a day inside it so a run straddling midnight UTC doesn't 400
  on its own start date). Older data is gone — there is no backfill.
- If start ends up after end, the script prints `Nothing to do: ...` and
  exits 0 without touching the warehouse.

`--probe` writes nothing and always exits 0 (failures are printed, not
raised). It reports:

- **Search API** — OK/FAILED, plus the product count of the unfiltered
  default grid of `ALGOLIA_INDEX`.
- **Analytics API** — `SKIPPED` if `ALGOLIA_ANALYTICS_KEY` is unset;
  otherwise the search count for the 7 days ending yesterday, the
  purchase-event count and revenue currencies, and an explicit warning when
  **no purchase events** are recorded (i.e. Algolia "conversion" ==
  add-to-cart, trap 1).
- **Tracked collections** — product count per handle from
  `algolia_collections.yaml`. `--probe` reads the config file only; it
  **ignores `--collections`**.

The probe only inspects the one index named in `ALGOLIA_INDEX`. To compare
candidate indices (trap 7), re-run it with `ALGOLIA_INDEX` set to each
candidate and compare the search counts.

## Tables

All five tables are created by the script's own DDL (`CREATE TABLE IF NOT
EXISTS`, run after `warehouse.db.init_db()` on every non-probe run); they
are not in the shared warehouse schema. Ratio columns (`*_rate`,
`average_click_position`) must be averaged, never summed.

### `collection_placement` — grain `placement` (Search API)

Daily snapshot: which position each product holds in each tracked
collection's default-sort grid. **Cannot be backfilled** — Algolia exposes
no ranking history.

- **PK** `(snapshot_date, collection, position)`. Indexes on
  `snapshot_date`, `product_id`, and `(collection, snapshot_date)`.
- **Write semantics: delete-rewrite per (day, collection).** For each
  collection that fetched successfully, today's rows for that collection are
  deleted and re-inserted in one transaction, so a same-day re-run replaces
  rather than appends, and a shrinking grid leaves no stale deep positions.
  A collection whose fetch fails keeps whatever rows it already had for today.
- The full collection is fetched (1000 hits per page, paging through
  `nbPages`), so large collections are not truncated.

| Column | Source / meaning |
|---|---|
| `snapshot_date` | local date of the run |
| `collection` | the tracked handle |
| `position` | 1-based grid rank = **enumeration order** of the result set, not any `position` field on the record (trap 5) |
| `object_id` | record `objectID`, as text (often a variant id — trap 4) |
| `product_id` | record `id`, as text, if present |
| `sku`, `handle`, `title`, `vendor` | record fields of the same name |
| `price`, `compare_at_price` | REAL, record fields |
| `inventory_available` | coerced to `1`/`0` (NULL if the record lacks it) |
| `published_at` | record field |
| `collection_size` | number of products in that grid that day — for bucketing position as a percentile |
| `synced_at` | UTC ISO timestamp of the run |

### `algolia_product_engagement` — grain `hits` (`/2/hits`)

Per-object (often per-variant, not per-product — trap 4) daily
impressions/clicks/add-to-cart. **Pooled** across every collection and query
the object appeared in; do not attribute it to one collection or sum it
across a product's placement rows (trap 2).

- **PK** `(date, object_id)`; index on `date`. **Upsert** (`INSERT OR
  REPLACE`), committed one day at a time.
- Paged at 1000 objects per request, up to 60 pages per day; hitting that
  guard prints `WARNING <day>: hit MAX_ANALYTICS_PAGES ... TRUNCATED` and
  stores the truncated day. Entries without a `hit` id are dropped.
- `revenueAnalytics=true` is only requested when a one-request check
  (`/2/conversions/purchaseRate` on the last day of the window) finds
  purchase events; otherwise it is skipped (the run log says which). If the
  check errors it fails closed (no revenue analytics).

Columns: `date`, `object_id`, `impressions` (Algolia `count`),
`tracked_impressions` (`trackedHitCount`), `click_count`,
`click_through_rate`, `add_to_cart_count`, `add_to_cart_rate`,
`purchase_count`, `purchase_rate` (zero/NULL until purchase events are
wired), `synced_at`. There is deliberately no column named `conversions`
(trap 1).

### `algolia_click_positions` — grain `positions` (`/2/clicks/positions`)

Empirical click-decay curve by grid slot, one row per position bucket per
day. A relative **shape**, not an absolute click total — it does not
cross-foot the CTR endpoint (trap 6).

- **PK** `(date, position_from)`. **Upsert** (`INSERT OR REPLACE`).
- Columns: `date`, `position_from`, `position_to` (`-1` = "and beyond", the
  deepest bucket; a single-value bucket gets `position_to = position_from`),
  `click_count`, `synced_at`.

### `algolia_searches` — grain `searches` (`/2/searches`)

Top queries per day, **capped at 2000 distinct queries per day** by volume
(the run log prints `(CAPPED at 2000)` when the cap is hit). The empty query
`''` is usually the browse grid itself, not a real search, and is kept
deliberately.

- **PK** `(date, query)`. **Upsert** (`INSERT OR REPLACE`).
- Offset paging on this endpoint can overlap between pages, so rows are
  **deduped by query before insert, first sighting wins** (the
  higher-volume page).
- Columns: `date`, `query`, `search_count`, `nb_hits`,
  `click_through_rate`, `average_click_position`, `conversion_rate`,
  `click_count`, `conversion_count`, `synced_at`. `conversion_*` are
  Algolia's raw fields and may really be add-to-cart (trap 1).

### `algolia_daily` — grain `daily`

Index-level daily series, zipped by date from eight endpoints —
`/2/searches/count`, `/2/users/count`, `/2/clicks/clickThroughRate`,
`/2/clicks/averageClickPosition`, `/2/conversions/addToCartRate`,
`/2/conversions/purchaseRate`, `/2/searches/noResultRate`,
`/2/searches/noClickRate` — one request each for the whole window, not per
day. The only place the site-wide no-result / no-click rates live.

- **PK** `(date)`. **Upsert** (`INSERT OR REPLACE`). One row is written for
  **every** date in the window; metrics an endpoint didn't return are NULL.
- Columns: `date`, `search_count`, `user_count`, `tracked_search_count`,
  `click_count`, `click_through_rate`, `average_click_position`,
  `add_to_cart_count`, `add_to_cart_rate`, `purchase_count`,
  `purchase_rate`, `no_result_count`, `no_result_rate`, `no_click_count`,
  `no_click_rate`, `synced_at`.

## Notes

The module docstring documents eight specific traps worth reading before you
build anything on top of this data — among them: Algolia's "conversion"
metric is frequently add-to-cart, not purchase; engagement can't be
attributed to a single collection when a product appears on several;
analytics retention is short (measured in weeks, not years) and hard-capped
server-side, with **no historical backfill possible**; a search-hit's own
`objectID` may be a variant id that the search index itself cannot reliably
resolve back to a product (resolve it via your own catalog data instead); a
record's own `position` field is not its grid rank; and A/B testing can
start splitting traffic across index variants at any time. The connector
reads nothing else from the warehouse, so variant-to-product resolution is
left to consumers.

**A/B-test guard.** Whenever the placement grain runs, it calls
`active_abtests()` (`/2/abtests?limit=50`, needs `ALGOLIA_ANALYTICS_KEY`).
Any test whose status is not `stopped`, `expired` or `failed` counts as
live. A live test logs the placement grain as `degraded` — not `ok` — with
the test names in the message, since a running test means the snapshot no
longer describes what every shopper saw. It also logs `degraded` (`A/B test
state UNKNOWN`) if the check itself can't run (no analytics key, or the API
call fails), because "unknown" must never be recorded the same way as
"confirmed clean." The check runs even when no collections are configured,
so a placement-only setup **without** an analytics key logs `degraded` and
exits 1 on every run.

**`sync_log` rows.** Each grain logs its own row, and a failure in one grain
does not stop the others:

| Grain | `sync_log.platform` |
|---|---|
| placement | `algolia_placement` |
| hits | `algolia_hits` |
| positions | `algolia_positions` |
| searches | `algolia_searches` |
| daily | `algolia_daily` |

Statuses: `ok`; `degraded` (placement only — one or more collections failed
to fetch, and/or an A/B test is live or its state is unknown; reasons are
joined with ` | ` in `message`); `error` (an Algolia call failed after
retries; `rows_written` is 0, though days already committed earlier in that
grain stay in the table). When `ALGOLIA_ANALYTICS_KEY` is unset, the
analytics grains are skipped with a printed note and log **no** `sync_log`
row at all.

**Exit code.** 0 when every planned grain logged `ok`; 1 (`Algolia sync
finished with N failed grain(s).`) if any grain logged `error` **or
`degraded`**. `--probe` and the "nothing to do" path always exit 0.

**Retries.** HTTP 408, 429, 500, 502, 503, 504 and network
errors/timeouts are retried, up to 5 attempts with exponential backoff (2s,
4s, 8s, 16s). Every other status (bad field, bad date, out-of-retention 400,
auth errors) is treated as permanent and fails immediately. Request timeout
is 120s. In the placement grain, a failed collection is caught and recorded
as a partial snapshot rather than failing the whole grain.

**Accrue-forward only.** Both placement and the analytics grains are
accrue-forward — a sync that stops running for an extended stretch loses
that window permanently, which is the whole argument for running this on a
real daily schedule rather than treating it as backfillable later.

## Tests

`tests/test_algolia_sync.py` — hermetic (no network, no credentials, no
`warehouse.db`). Covers: retention clamping; the purchase-event gate for
`revenueAnalytics` (including fail-closed); schema invariants (no
`conversions` column, purchase columns present, `collection_size`, the
placement PK, text ids); placement enumeration order, same-day
delete-rewrite, one failed collection not losing the others, and the
empty-config skip; retry classification (permanent 4xx vs transient);
config loading and the analytics key not being hard-coded; `/2/searches`
overlap dedupe (first sighting wins, empty query kept); the A/B-test guard
(`[]` vs live vs finished vs `None`); and the required-env `SystemExit`.
