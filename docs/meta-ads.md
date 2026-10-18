# Meta (Facebook / Instagram) Ads

Campaign-level spend/clicks/conversions from the core connector, plus an
optional standalone script for ad/adset/creative/video-level detail, and a
write-capable `meta_ads_mutate.py` for pausing/resuming, budgets and ad-set
copies (see the last section).

**Script:** `warehouse/connectors/meta_ads.py`, via `run_sync.py --only meta`
(plain HTTPS against the Graph/Marketing API `v23.0`, no Meta SDK)

## Setup

Fill in `.env` (both variables are listed in `.env.example`):

| Variable | Notes |
|---|---|
| `META_ACCESS_TOKEN` | a long-lived system-user token. This is the only variable `run_sync.py` checks before attempting `meta` — if it is empty, `meta` is printed as `SKIPPED (no credentials in .env)`. |
| `META_AD_ACCOUNT_ID` | looks like `act_1234567890` (include the `act_` prefix — it is used verbatim in the URL path). Not pre-checked: if the token is set but this is missing, the run fails with a `KeyError` and is logged as an error. |
| `META_PIXEL_ID` | optional; only `meta_ads_mutate.py create-pdp-audience` needs it (the site pixel the audience is built from). |
| `META_PAGE_ID` | optional; only `meta_ads_mutate.py create-vv-audience` needs it (the Facebook Page the ad videos belong to). |
| `META_TARGETING_RULES` | optional; path to the targeting-rules JSON for `meta_ads_mutate.py`'s guard (default `meta_targeting_rules.json` in the repo root — see [Targeting guard](#targeting-guard)). |

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

All four are created by the script itself (`CREATE TABLE IF NOT EXISTS`,
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

- `meta_adset_funnel` — PK `(adset_id, observed_date)`. The funnel stage of
  each ad set that spent in the window, read from its **targeting** (the
  custom audiences it includes) by `meta_funnel.classify_targeting`, never
  from its hand-typed name. Columns: `adset_id, observed_date, campaign_id,
  adset_name, stage, synced_at`. `stage` is one of `retention` (customer-list
  audience), `retargeting` (site-visitor / engager audiences), `warm_product`
  (product-page-visitor / video-viewer audiences), `cold` (no custom audience
  included) or `unknown` (no targeting, or an included audience that matches
  no pattern — stored and counted, never guessed). One row per observation
  date because audiences can be edited: join a spend row to the latest
  `observed_date <=` its date. Only ad sets seen in the window's insights are
  read, so dead ad sets under paused campaigns cost nothing.

  The audience-name patterns are a convention you adapt to your own naming
  scheme; override them with `META_FUNNEL_RETENTION_PATTERN`,
  `META_FUNNEL_RETARGETING_PATTERN` and `META_FUNNEL_WARM_PRODUCT_PATTERN`
  (case-insensitive regexes; defaults are in the `meta_funnel.py` docstring).
  `python meta_funnel.py` lists every active ad set with its stage — a quick way
  to see which audiences need a pattern.

### sync_log and exit code

One `sync_log` row per run with `platform='meta_ads_detail'` and
`rows_written` = number of ad-day insight rows:

- `status='ok'` — every day fetched; message like
  `<start> -> <end>; N creatives, M new videos (K Reacher)`.
- `status='degraded'` — one or more days were skipped (message gets
  `; N day(s) skipped` appended, **exit code 1**), and/or one or more ad sets
  have an unrecognised audience (stage `unknown`; message names the count and
  points at `meta_funnel.py`; exit code stays 0 — the data is stored, the
  classifier just needs a pattern).
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
  `creatorhandle_Sep2025_RCHR_0a1b2c3d`), the video's title can be parsed
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
`degraded` vs `ok` status), plus the funnel side: only ad sets that spent in
the window are looked up, their stages land in `meta_adset_funnel` with
`unknown` ones counted (`stage_unknown`), and `fetch_adset_funnel` classifies
from targeting (no custom audience → `cold`, a lookalike → `unknown`).

`tests/test_meta_funnel.py` — hermetic `classify_targeting` cases: no
included audience is `cold` even when exclusions exist, stacked site-visitor
and engager audiences are `retargeting`, PDP / video-viewer audiences are
`warm_product`, a customer list is `retention` and outranks retargeting, an
unrecognised included audience (e.g. a lookalike) or missing targeting is
`unknown`, and a `META_FUNNEL_*_PATTERN` env override replaces the default.

## Standalone extra: write-capable `meta_ads_mutate.py`

**Script:** `meta_ads_mutate.py` — the Meta counterpart to
[`google_ads_mutate.py`](google-ads.md), and the only file in this repo that
changes anything in a live Meta ad account. Plain `requests` POSTs against the
same pinned Graph API version as the core connector; it writes nothing to
`warehouse.db`.

| Subcommand | Does |
|---|---|
| `pause-campaign` / `resume-campaign --campaign-id ID` | set campaign `status` |
| `pause-adset` / `resume-adset --adset-id ID` | set ad set `status` |
| `pause-ad` / `resume-ad --ad-id ID` | set ad `status` |
| `set-adset-budget --adset-id ID --daily-amount N [--currency-offset 100]` | set `daily_budget`, converting major → minor currency units |
| `copy-adset --adset-id ID --dest-campaign-id ID [--go-live] --execute` | deep-copy an ad set and its ads into another campaign |
| `create-pdp-audience --name NAME --url-keyword HANDLE [--execute]` | create a product-page-visitors website audience (URL contains the keyword, 180 days, pixel from `META_PIXEL_ID`); idempotent by name, dry run by default |
| `copy-ads --source-adset-id ID --dest-adset-id ID [--limit N] [--execute]` | copy the ads of one ad set into another **existing** ad set, created `PAUSED`; the dry run only lists |
| `create-vv-audience --name NAME --video-ids ID... [--event video_view_25_percent] [--retention-days 365] [--execute]` | create a video-viewers engagement audience (reached `--event` on any of the ad videos); page from `META_PAGE_ID`; idempotent by name, dry run by default |
| `edit-adset-audiences --adset-id ID [--include ID...] [--exclude ID...] [--remove-include ID...] [--remove-exclude ID...] [--advantage-off] [--execute]` | change only an ad set's include/exclude custom audiences and Advantage+ audience; validate-only by default (alias `adset-audience-edit`) |
| `check-targeting --adset-id ID...` | read-only: print each ad set's targeting and its rule breaks; exit 1 on any HARD break |
| `audience-info --ids ID...` | read-only: custom audience name, subtype, retention, size bounds, last update / content refresh, delivery status |
| `rename --object-id ID --name NAME [--execute]` | rename one campaign / ad set / ad (validate-only by default) |
| `rename-from-csv --csv PLAN.csv [--delay 2] [--execute]` | apply a rename plan, skipping rows whose live name drifted (see [Usage](#usage)) |

### Safety model

- Every field update sends `execution_options=["validate_only"]` unless you
  pass `--execute`: Meta runs full server-side validation (real ids, real
  permission checks) and commits nothing. Meta answers `{"success": true}`
  for both a dry run and a real commit, so the script prints
  `VALIDATE_ONLY passed` or `EXECUTED` explicitly.
- `copy-adset` is the exception. The `/{adset_id}/copies` edge is not
  documented to honour `validate_only`, so instead of sending a "dry run"
  that might really create a copy, the subcommand **refuses to run without
  `--execute`** (exit code 2, versus 1 for an API error). The copy is created `PAUSED` unless `--go-live` is also
  passed. It is a genuinely new ad set with a fresh learning phase — none of
  the source's delivery history carries over (Meta has no native "move").
- `copy-ads` also has no server-side validation, but its dry run is purely
  read-only: it lists the source ad set's ads (ignoring `DELETED`/`ARCHIVED`)
  and reports which would be copied. Ads whose name already exists in the
  destination are skipped, so re-running after a partial failure is safe;
  `--limit N` allows a canary run of a single ad first. Use it to rebuild an
  ad set whose `optimization_goal` cannot be edited after publish (create the
  new ad set, then copy the ads across — each ad keeps its creative, caption,
  link and URL tags). Writes are spaced ~31s apart and rate-limit errors
  (codes 613/17) are retried up to 4 times, because Meta allows roughly one
  write per 30 seconds. Execution needs publish permission on the Page /
  Instagram account behind each ad's creative; a failed list call aborts
  rather than being read as "no ads".
- `create-pdp-audience` has no server-side validation either; its dry run
  makes one read-only call (it lists the account's custom audiences) and
  posts nothing. If an audience with exactly `--name` already exists it
  prints `EXISTS <id>` and stops, so re-runs never create duplicates. With
  `--execute` it creates a pixel-based website audience (template
  `VISITORS_BY_URL`): anyone who visited a URL containing `--url-keyword`
  (case-insensitive — pass the product handle, e.g. `ruffle-mini-dress`) in
  the last 180 days, prefilled from existing pixel history. It prints
  `EXECUTED` or `FAILED` with Meta's response; a failed create does **not**
  exit non-zero, so check the output in scripts. It exits early if
  `META_PIXEL_ID` is unset or the audience list call errors.
- `create-vv-audience` follows the same pattern as `create-pdp-audience`
  (one read-only list call, `EXISTS` on a duplicate name, nothing posted
  without `--execute`). Its rule is one video-engagement entry per unique
  video id with `META_PAGE_ID` as context. Pass the Facebook video ids (not
  Instagram media ids) of the copy **owned by your Page** —
  `GET <video_id>?fields=from` must return `META_PAGE_ID`. The ad account's
  library copy (`act_<id>/advideos`) is rejected with (#2654) "video isn't
  associated with a Page" (subcode 1713216), and videos from creator /
  partnership ads live on the creator's Page and fail with (#2654) subcode
  1713080. The token's user or system user needs a role on the Page with the
  **Advertise** task; `pages_read_engagement` alone still gets (#10) on the
  Page. Non-numeric ids are refused before any call is made, but ownership is
  **not** checked here (Meta checks it on create): filter the list once
  yourself with `?fields=from`, and do not list the whole `advideos` library
  per call — that trips the account-wide read limit and blocks every command,
  writes included, for a while. `--event`
  is one of `video_watched`, `video_view_10s`, `video_view_15s`,
  `video_view_25_percent` (default), `video_view_50_percent`,
  `video_view_75_percent`, `video_completed`. The audience is created with
  `prefill=true`, so it back-fills viewers within the retention window
  rather than starting empty. Like `create-pdp-audience`, a failed create
  prints `FAILED` and still exits 0.
- Both audience creators check for an existing name with a single
  `customaudiences?limit=500` call that does not follow paging. On an
  account with more than 500 custom audiences an existing name can be
  missed and a duplicate created — check Ads Manager first on large
  accounts.
- `edit-adset-audiences` reads the live targeting and sends it back with
  ONLY `custom_audiences`, `excluded_custom_audiences` and
  `targeting_automation` changed — Meta replaces the whole `targeting`
  object on write, so a partial payload would wipe geo, age and
  placements. It prints before → after with audience names (resolved in
  one `?ids=` call), refuses an audience that would be both included and
  excluded, and prints `NO CHANGE` and sends nothing when the live
  targeting already matches. With `--advantage-off` on an ad set that was
  created with Advantage+ audience on, Meta rejects
  `advantage_audience=0` (subcode 1487079, "targeting_automation must be
  enabled to use age_range") unless `individual_setting` is present, so
  the editor adds `individual_setting={"age":1,"gender":1}` when the
  targeting has `age_range` and no setting.
- The editor only edits the top-level `custom_audiences` /
  `excluded_custom_audiences` lists. Includes nested inside
  `flexible_spec` are counted by the targeting guard (and shown as
  includes) but cannot be added or removed here — change those in Ads
  Manager. `--remove-*` of an id that is not in the list prints a `NOTE`
  and is otherwise ignored.
- The editor's "after the edit" rule check is informational: it refuses
  only on an include/exclude overlap, never on another HARD break, so you
  can use it to *fix* a broken ad set one step at a time.
- Every guard and editor read is fail-closed: if Meta returns an error
  reading the ad set or its audiences, the command prints `FAILED reading`
  and exits 1 before any write.
- Any API error (HTTP non-200, or an `error` object in a 200 body) exits 1
  with Meta's message, type, code, subcode and `error_user_msg`.

### Targeting guard

`resume-adset`, `resume-campaign` (every ACTIVE ad set under it) and
`copy-adset` (the SOURCE ad set) first read the live targeting and check
it; on a HARD break they print the reason and exit 1 without writing,
unless `--force-targeting` is passed. The failure this catches is quiet:
a "warm" ad set that lost (or never had) its include audience delivers
broad, and copying it multiplies the problem. `check-targeting` runs the
same check read-only, and `edit-adset-audiences` prints it for the
edited targeting.

The rules are yours, in a JSON file (`META_TARGETING_RULES`, default
`meta_targeting_rules.json`, git-ignored because it holds real audience
ids). Start from `meta_targeting_rules.example.json`. Each entry in
`stages` has a `name`, a case-insensitive `pattern` matched against the
ad set name and then the campaign name (first match wins, so a warm ad
set inside a cold campaign is judged as warm), and any of:

| Key | Break |
|---|---|
| `advantage_audience_off: true` | HARD if `targeting_automation.advantage_audience` is not 0 |
| `require_include: true` | HARD if there is no non-lookalike include audience ("runs broad") |
| `forbid_include: true` | HARD if there is a non-lookalike include (prospecting should be broad or lookalike) |
| `required_exclusions: {label: [ids]}` | HARD if none of a label's ids is excluded |
| `recommended_inclusions: {label: [ids]}` | WARN if none of a label's ids is included |
| `include_name_regex: "pdp"` | WARN if no included audience name matches |

One check needs no rules: an audience that is both included and excluded
is always HARD. An ad set no stage matches (or every ad set, with no rules
file) gets a WARN, never a refusal. A rules file that exists but does not
parse exits rather than silently guarding nothing.

A useful prospecting rule: exclude your customer-list audiences (recent
purchasers, lapsed customers) from cold ad sets. Without them a
meaningful share of "cold" spend reaches existing customers and
inflates cold ROAS. `audience-info` shows whether such a list is still
refreshing (`content=` date) — a stale list is not a real exclusion.

### Permissions — two independent axes

1. The token's OAuth scopes must include `ads_management` (`ads_read` is
   enough for the read-only connectors, not here). Check with
   `GET /debug_token`.
2. The token's user or system user must **also** hold an Advertiser-level
   (or higher) role on the ad account in Business Manager. A token with
   `ads_management` but a read-level role fails every write, validate-only
   ones included. Fix the role, not the scope.
3. `create-vv-audience` only: the same user or system user also needs a role
   on the Facebook **Page** (`META_PAGE_ID`) with the **Advertise** task, and
   the video ids must be the Page-owned copies (see the subcommand above).
   `pages_read_engagement` on the token is not a substitute — without the Page
   task the create fails with (#10).

Environment: every subcommand reuses `META_ACCESS_TOKEN`.
`create-pdp-audience` additionally reads `META_AD_ACCOUNT_ID` (the `act_`
prefix is optional) and `META_PIXEL_ID` (the site pixel the audience is
built from); `create-vv-audience` reads `META_AD_ACCOUNT_ID` and
`META_PAGE_ID`; the targeting guard reads `META_TARGETING_RULES` (optional).

### Usage

```bash
python meta_ads_mutate.py pause-campaign --campaign-id 1234567890           # dry run
python meta_ads_mutate.py pause-campaign --campaign-id 1234567890 --execute
python meta_ads_mutate.py set-adset-budget --adset-id 1234567890 --daily-amount 50
python meta_ads_mutate.py set-adset-budget --adset-id 1234567890 --daily-amount 3000 \
    --currency-offset 1 --execute                                           # zero-decimal currency (JPY/KRW)
python meta_ads_mutate.py copy-adset --adset-id 1234567890 --dest-campaign-id 9876543210 --execute
python meta_ads_mutate.py copy-ads --source-adset-id 1234567890 --dest-adset-id 1122334455            # lists only
python meta_ads_mutate.py copy-ads --source-adset-id 1234567890 --dest-adset-id 1122334455 --limit 1 --execute
python meta_ads_mutate.py create-pdp-audience --name "PDP - Ruffle Mini Dress" --url-keyword ruffle-mini-dress            # dry run
python meta_ads_mutate.py create-pdp-audience --name "PDP - Ruffle Mini Dress" --url-keyword ruffle-mini-dress --execute
python meta_ads_mutate.py create-vv-audience --name "VV25 - Spring UGC" --video-ids 1111111111 2222222222             # dry run
python meta_ads_mutate.py create-vv-audience --name "VV50 - Spring UGC" --video-ids 1111111111 2222222222 \
    --event video_view_50_percent --retention-days 180 --execute
python meta_ads_mutate.py check-targeting --adset-id 1234567890 1122334455   # read-only; exit 1 on a HARD break
python meta_ads_mutate.py audience-info --ids 2222222222 3333333333          # read-only: size, refresh dates
python meta_ads_mutate.py edit-adset-audiences --adset-id 1234567890 --include 4444444444 \
    --exclude 3333333333 --advantage-off                                    # validate-only; add --execute
python meta_ads_mutate.py edit-adset-audiences --adset-id 1234567890 --remove-include 4444444444 --execute
python meta_ads_mutate.py resume-adset --adset-id 1234567890 --execute      # refuses on a HARD targeting break
python meta_ads_mutate.py resume-adset --adset-id 1234567890 --force-targeting --execute   # override the guard
python meta_ads_mutate.py rename --object-id 1234567890 --name "New name"   # dry run; add --execute
python meta_ads_mutate.py rename-from-csv --csv rename_plan.csv             # dry run of the whole plan
python meta_ads_mutate.py rename-from-csv --csv rename_plan.csv --execute --delay 2
```

`rename` changes a campaign / ad set / ad name only (no delivery, budget or
learning impact). `rename-from-csv` applies a plan with columns
`level,id,current_name,proposed_name`. Before each write it reads the object's
**live** name and compares it with `current_name`: a mismatch is skipped and
reported (`SKIP`), never overwritten, so a name someone changed after the plan
was drafted is not clobbered; a row already at `proposed_name` is reported
`SAME`. Writes ride out Meta's write rate limit (error code 613, roughly one
write per 30 seconds) by sleeping and retrying instead of aborting the batch,
and the run ends with `executed|validated: N  skipped: N  failed: N`. Keep real
rename plans out of version control — they are account data.

### Notes

- `daily_budget` is an integer in the account currency's minor unit:
  `--currency-offset` is 100 for two-decimal currencies (50 → 5000) and 1 for
  zero-decimal ones. Compare against an existing ad set's `daily_budget`
  before writing if unsure. Ad sets under Advantage campaign budget (CBO)
  have no budget of their own and reject this write.
- An ACTIVE ad set whose ads are all PAUSED spends nothing, and the ad-set
  row in Ads Manager doesn't make that obvious. Check the ads'
  `effective_status` and use `resume-ad` when an ad set "looks live" but
  isn't delivering.

### Tests

`tests/test_meta_ads_mutate.py` — hermetic (fake `requests.post`): every
status subcommand defaults to `validate_only`, `--execute` drops it,
`copy-adset` refuses without `--execute` and defaults to `PAUSED`, budget
minor-unit conversion (including a zero-decimal offset), and non-zero exit on
an API error (including an error body on HTTP 200), `rename` validate-only,
`rename-from-csv` skipping a drifted live name, and the rate-limit retry.
`CopyAdsTests` cover the read-only dry run, `PAUSED` copies that skip
deleted/already-present ads, `--limit`, aborting on a list error and the
rate-limit retry. `PdpAudienceTests` cover the post-free dry run, an existing
name not being recreated, the executed rule carrying the pixel id, and the
exit when `META_PIXEL_ID` is missing. `TargetingRulesTests` cover stage
matching (ad set name before campaign), each rule key, lookalikes not
counting as a real include, the rule-free overlap check, and loading
(missing file = no rules, malformed file exits, the example parses).
`GuardAndEditorTests` cover resume/copy refusing on a HARD break,
`--force-targeting`, `check-targeting`'s exit code, the editor keeping
every other targeting key, the `individual_setting` fix, the overlap
refusal and the no-change short-circuit. `VideoViewersAudienceTests`
cover the dry run, de-duplicated rule entries with the page context,
the duplicate-name check, non-numeric ids and a missing `META_PAGE_ID`.
