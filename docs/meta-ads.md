# Meta (Facebook / Instagram) Ads

Campaign-level spend/clicks/conversions from the core connector, plus an
optional standalone script for ad/adset/creative/video-level detail.

**Script:** `warehouse/connectors/meta_ads.py`, via `run_sync.py --only meta`
(plain HTTPS against the Graph/Marketing API `v23.0`, no Meta SDK)

## Setup

Fill in `.env` (both variables are listed in `.env.example`):

| Variable | Notes |
|---|---|
| `META_ACCESS_TOKEN` | a long-lived system-user token. This is the only variable `run_sync.py` checks before attempting `meta` — if it is empty, `meta` is printed as `SKIPPED (no credentials in .env)`. |
| `META_AD_ACCOUNT_ID` | looks like `act_1234567890` (include the `act_` prefix — it is used verbatim in the URL path). Not pre-checked: if the token is set but this is missing, the run fails with a `KeyError` and is logged as an error. |

Optional, shared with every script: `WAREHOUSE_DB` overrides the SQLite path
(default `warehouse.db` in the repo root).

No interactive auth helper — generate the long-lived token directly in
Meta Business Settings.

## Usage

```bash
python run_sync.py --only meta                               # default window: today minus 7 days -> today (inclusive)
python run_sync.py --only meta --days 30
python run_sync.py --only meta --start 2026-06-01 --end 2026-06-30
```

`run_sync.py` flags: `--days N` (default 7; start = end − N days, so the
window is N+1 calendar days inclusive), `--start` / `--end` (ISO dates;
`--end` defaults to today), `--only` (comma list), `--sample` (fake demo
data, no API calls — includes fake `meta` rows).

## What the core connector does

- One `GET /{account}/insights` with `level=campaign`, `time_increment=1`
  (one row per campaign per day), `limit=500`, following `paging.next`.
- Fields requested: `campaign_id, campaign_name, impressions, clicks, spend,
  actions, action_values, account_currency, reach, inline_link_clicks,
  objective`.
- **Big ranges split themselves.** If Meta persistently answers with error
  code 1 ("too much data"), the range is halved and each half retried,
  recursively. If even a single day is rejected, it raises
  `Meta rejected even a single day (...) as too large.`
- **Retries** (`_get_json`, also reused by the detail script), up to 8
  attempts per request:
  - connection error / timeout → wait 20 s, retry;
  - error code 1, or a 5xx with no error code → wait `15 × n` s; the 3rd
    code-1 response is treated as "range too large" (triggers the split);
  - rate-limit codes `4, 17, 32, 613, 80000, 80004` or HTTP 429 → wait
    `60 × (attempt+1)` s, printing `(meta: rate limited, waiting Ns)`;
  - any other error → `RuntimeError("Meta API <status>: <body>")` immediately;
  - still failing after 8 attempts → `RuntimeError("Meta API kept failing after retries.")`.
- **Canonical action types (no double counting).** Meta's `actions` list
  contains overlapping entries (`omni_*` already includes the pixel/onsite
  variants), so for each funnel step exactly one action type is taken — the
  first one present in this priority list:

  | Metric | Priority order |
  |---|---|
  | purchases / revenue | `omni_purchase` → `purchase` → `offsite_conversion.fb_pixel_purchase` |
  | add to carts | `omni_add_to_cart` → `add_to_cart` → `offsite_conversion.fb_pixel_add_to_cart` |
  | checkouts | `omni_initiated_checkout` → `initiate_checkout` → `offsite_conversion.fb_pixel_initiate_checkout` |
  | landing page views | `omni_landing_page_view` → `landing_page_view` |

  Missing action types (or no `actions` at all) yield `0`.

## Tables

- `ad_metrics` (core, shared across platforms — see the main [README](../README.md#mcp-tools)).
  Primary key `(platform, account_id, campaign_id, date)`; written with
  `INSERT OR REPLACE` (upsert — rows for campaigns that no longer appear in
  a re-synced window are not deleted). Meta rows have `platform='meta'`,
  `account_id` = `META_AD_ACCOUNT_ID`, and fill:

  | Column | Source |
  |---|---|
  | `impressions`, `clicks`, `spend` | same-named insight fields |
  | `conversions` | purchase count (canonical type above) |
  | `revenue` | purchase value from `action_values` (canonical type above) |
  | `currency` | `account_currency` |
  | `campaign_type` | campaign `objective` |
  | `reach` | `reach` (Meta only) |
  | `link_clicks` | `inline_link_clicks` |
  | `add_to_carts`, `checkouts` | canonical action types above |
  | `landing_page_views` | canonical action type above |

  Google-only columns (`all_conversions`, `search_impression_share`, etc.)
  stay NULL for Meta.

  About `landing_page_views`: Meta's `landing_page_view` action, i.e. link
  taps that actually finished rendering the page, always `<=` `link_clicks`.
  If you have a separate analytics tool tracking sessions landing from this
  traffic, compare it against `landing_page_views`, not `link_clicks` —
  `link_clicks` counts every tap, including ones that bailed during load, and
  can meaningfully overstate real arrivals. The column is added to older
  databases by the `init_db()` migration.

- `sync_log`: one row per run with `platform='meta'`, `status='ok'` (rows
  written) or `'error'` (message = the exception text). A failed connector
  makes `run_sync.py` exit non-zero with `Connector failures: meta`.

Core-connector tests: `tests/test_meta_ads_landing_page_views.py` (the
landing-page-view action resolution, the `landing_page_views` column
migration, and `upsert_ad_metrics` persisting it).

## Standalone extra: ad/creative/video-level detail

**Script:** `meta_ads_detail_sync.py` (standalone — not wired into
`run_sync.py`; same `.env` credentials as the core connector, no extra
setup; reuses the core connector's `_get_json` retry logic, API version and
action-type lists)

Neither `ad_metrics` (campaign grain) nor a catalog-product feed carries an
`ad_id`, `adset_id`, or creative id, so neither can answer "how is this
*specific ad* or the *creative inside it* performing" — useful once you want
to compare individual videos/creatives against each other rather than whole
campaigns, or (see the optional bridge below) tie a specific video back to
whichever tool originally produced it.

```bash
python meta_ads_detail_sync.py --days 3
python meta_ads_detail_sync.py --start 2026-08-01 --end 2026-08-24
python meta_ads_detail_sync.py --days 3 --only insights     # skip creative/video lookups
python meta_ads_detail_sync.py --refresh-creatives          # re-read creatives for every stored ad
python meta_ads_detail_sync.py --full-video-crawl           # seed the whole account video library
```

| Flag | Default | Meaning |
|---|---|---|
| `--days N` | `3` | start = end − N days (so N+1 calendar days, inclusive). Ignored when `--start` is given. |
| `--start YYYY-MM-DD` | end − `--days` | first day to pull |
| `--end YYYY-MM-DD` | today | last day to pull (inclusive) |
| `--only insights` | off | only choice is `insights`: write `meta_ad_daily`, skip the creative and video stages |
| `--refresh-creatives` | off | creative lookup targets every `ad_id` already in `meta_ad_creatives` **plus** the ads seen in this window (normally only the latter) |
| `--full-video-crawl` | off | walk the whole `/advideos` library (up to 600 pages × 50) instead of stopping once the referenced videos are found |

### How a run works

1. **Insights, one request chain per day.** For each day in the window,
   `GET /{account}/insights` with `level=ad`, `time_increment=1`,
   `limit=500`, paginated. Fields: `ad_id, ad_name, adset_id, adset_name,
   campaign_id, campaign_name, impressions, clicks, spend,
   inline_link_clicks, actions, action_values`. Each day is committed on its
   own, so a long backfill never holds SQLite's single write lock for long.
   If a day fails (`_RangeTooLarge`, `RuntimeError`, or a `requests` error
   after the retries above) it is printed as
   `meta ad detail <day>: skipped (...)`, counted, and the run continues.
2. **Creatives, bounded by spend.** Unless `--only insights`, every `ad_id`
   that appeared in step 1 (plus stored ads with `--refresh-creatives`) is
   read via Meta's `?ids=` batch form, 50 ids per request, fields `id, name,
   status, campaign_id, adset_id, creative{id, name, video_id,
   thumbnail_url, source_instagram_media_id, object_story_spec}`. Ids Meta
   drops from the response are silently skipped.
3. **Videos, by crawl.** The `video_id_any` values from step 2 that aren't
   already in `meta_ad_videos` are sought by walking `/advideos`
   (fields `id, title, created_time, length`, 50/page). A routine crawl
   stops when every wanted video is found, after 3 consecutive pages with
   nothing new, at the end of pagination, or at 80 pages. Wanted videos not
   found are reported (`N referenced video(s) not found in this crawl — run
   --full-video-crawl to seed the library`) but are not fatal. Already-stored
   videos are never re-read (titles don't change).

   Note: the video stage only runs inside the creative stage, so
   `--full-video-crawl` does nothing when the window had no ad insights
   (and `--refresh-creatives` isn't supplying stored ads), or with
   `--only insights`.

### Tables

All three are created by the script itself (`CREATE TABLE IF NOT EXISTS`,
not in `warehouse/schema.sql`) and written with `INSERT OR REPLACE`
(upsert; nothing is deleted). `synced_at` is a UTC ISO timestamp of the run.

- `meta_ad_daily` — PK `(account_id, date, ad_id)`; indexes on `date` and
  `ad_id`. Columns: `account_id, date, ad_id, ad_name, adset_id,
  adset_name, campaign_id, campaign_name, impressions, clicks, link_clicks`
  (`inline_link_clicks`), `spend, add_to_carts, checkouts, purchases,
  revenue` (purchase value), `synced_at`. Funnel metrics use the same
  canonical purchase/add-to-cart/checkout action-type resolution the core
  connector uses (so purchases can't double-count Meta's overlapping
  `omni_*` and `pixel_*` action types). No `landing_page_views`, `reach` or
  `currency` at this grain.
- `meta_ad_creatives` — PK `ad_id`; index on `video_id_any`. Ad → creative →
  video, current state (Meta exposes no creative history, so this is an
  upsert, not a log). Columns: `ad_id, ad_name, campaign_id, adset_id,
  status, creative_id, creative_name, video_id` (`creative.video_id`),
  `story_video_id` (`object_story_spec.video_data.video_id`),
  `video_id_any` (`story_video_id`, else `video_id`),
  `instagram_media_id` (`source_instagram_media_id`), `thumbnail_url`,
  `synced_at`.
- `meta_ad_videos` — PK `video_id`; index on `creator_handle`. The
  ad-account's video library. Columns: `video_id, title, created_time,
  length_seconds` (NULL when Meta reports 0/none), `is_reacher` (1/0),
  `creator_handle, period, reacher_hash` (NULL unless the title matches the
  optional convention below), `synced_at`.

### sync_log and exit code

One `sync_log` row per run with `platform='meta_ads_detail'` and
`rows_written` = number of ad-day insight rows:

- `status='ok'` — every day fetched; message like
  `<start> -> <end>; N creatives, M new videos (K Reacher)`.
- `status='degraded'` — one or more days were skipped; message gets
  `; N day(s) skipped` appended. **Exit code 1** in this case.
- `status='error'` — an uncaught exception (e.g. missing env var, a failure
  in the creative or video stage); the exception text is logged and
  re-raised.

Otherwise exit code 0. Final console line:
`Meta ad detail: N ad-day rows, N creatives, N new videos (N Reacher-tagged) [start -> end]`.

### Notes

- **Video reads need a crawl, not a lookup.** Reading an individual video by
  id (`GET /{video_id}` or the `?ids=` batch form) commonly fails with a
  `(#10) Application does not have permission` error even when the account's
  own `/advideos` edge returns the same object fine — so ad/creative lookups
  use Meta's `?ids=` batch form (capped at 50/request), but the video
  library has to be walked page by page (`/advideos`, 50/page — 200 errors
  with "reduce the amount of data"). `/advideos` ordering is only
  approximately newest-first, so the crawl stops after several (3)
  consecutive pages with nothing new rather than at the first already-known
  video.
- **A creative can carry two disagreeing video ids.** `creative.video_id` and
  `object_story_spec.video_data.video_id` aren't guaranteed to point at the
  same object, and in practice the latter (`story_video_id` here) is the one
  that reliably resolves to a real ad-account video. `video_id_any` coalesces
  `story_video_id` first — worth knowing if you ever touch this code, since
  the more obviously-named field silently resolves to nothing. Join
  `meta_ad_creatives.video_id_any = meta_ad_videos.video_id`.
- **Bounded by spend, not by account history**, on every routine run: only
  ads that appear in the requested date range's insights get a creative
  lookup, and only the videos those creatives reference get pulled. Use
  `--full-video-crawl` to seed the whole library once (or periodically), not
  as part of a daily job.
- **Re-syncing a window upserts, it doesn't clear.** An ad row stored for a
  day stays even if a later pull of that day no longer returns it.
- **Optional creator-platform bridge.** If you upload creator-marketing
  videos to the ad account through a tool that stamps a naming convention on
  the title (this ships an example parser for
  [Reacher's](reacher.md) convention,
  `<creator_handle>_<Mon><Year>_RCHR_<hex>`, e.g.
  `creatorhandle_Sep2025_RCHR_68cc4beb`), the video's title can be parsed
  straight into `meta_ad_videos.creator_handle` with no extra API calls —
  bridging "did this creator's video work as a Meta ad" back to whichever
  platform you manage that creator relationship through. The handle may
  itself contain underscores (the literal `_RCHR_` anchors the split);
  handle and hash are lower-cased; `period` is kept as written (e.g.
  `Sep2025`). This is entirely optional: unmatched titles leave those
  columns NULL (`is_reacher=0`), so skip it (or swap in your own pattern via
  `REACHER_TITLE`) if you don't use a matching convention. As with any
  creator identity join, match on the **handle**, not on a display name —
  see [docs/tiktok-shop.md](tiktok-shop.md) for the same trap on that side.

### Tests

`tests/test_meta_ads_detail_sync.py` — hermetic (no network, temp DB):
the Reacher title parser, DDL creation/idempotency, `crawl_videos`' stop
conditions (all wanted found, consecutive barren pages, known videos not
re-stored, unresolved ids reported), and `run()` / `main()` behaviour
(`--only insights`, spend-bounded creative targeting,
`--refresh-creatives`, video crawl of new references, skipped days and the
`degraded` vs `ok` status).
