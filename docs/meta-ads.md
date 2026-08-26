# Meta (Facebook / Instagram) Ads

Campaign-level spend/clicks/conversions from the core connector, plus an
optional standalone script for ad/adset/creative/video-level detail.

**Script:** `warehouse/connectors/meta_ads.py`, via `run_sync.py --only meta`

## Setup

Fill in `.env`:

| Variable | Notes |
|---|---|
| `META_ACCESS_TOKEN` | a long-lived system-user token |
| `META_AD_ACCOUNT_ID` | looks like `act_1234567890` |

No interactive auth helper — generate the long-lived token directly in
Meta Business Settings.

## Usage

```bash
python run_sync.py --only meta   # last 7 days
```

## Tables

- `ad_metrics` (core, shared across platforms — see the main [README](../README.md#mcp-tools))

## Standalone extra: ad/creative/video-level detail

**Script:** `meta_ads_detail_sync.py` (standalone — not wired into
`run_sync.py`; same `.env` credentials as the core connector, no extra
setup)

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

Three tables:

- `meta_ad_daily` — account x date x ad: impressions/clicks/spend/link_clicks
  + the same canonical purchase/add-to-cart/checkout action-type resolution
  the core connector uses (so purchases can't double-count Meta's overlapping
  `omni_*` and `pixel_*` action types).
- `meta_ad_creatives` — ad → creative → video, current state (Meta exposes no
  creative history, so this is an upsert, not a log).
- `meta_ad_videos` — the ad-account's video library.

### Notes

- **Video reads need a crawl, not a lookup.** Reading an individual video by
  id (`GET /{video_id}` or the `?ids=` batch form) commonly fails with a
  permissions error even when the account's own `/advideos` edge returns the
  same object fine — so ad/creative lookups use Meta's `?ids=` batch form
  (capped at 50/request), but the video library has to be walked page by
  page (`/advideos`, 50/page — 200 errors with "reduce the amount of data").
  `/advideos` ordering is only approximately newest-first, so the crawl stops
  after several consecutive empty pages rather than at the first
  already-known video.
- **A creative can carry two disagreeing video ids.** `creative.video_id` and
  `object_story_spec.video_data.video_id` aren't guaranteed to point at the
  same object, and in practice the latter (`story_video_id` here) is the one
  that reliably resolves to a real ad-account video. `video_id_any` coalesces
  `story_video_id` first — worth knowing if you ever touch this code, since
  the more obviously-named field silently resolves to nothing.
- **Bounded by spend, not by account history**, on every routine run: only
  ads that appear in the requested date range's insights get a creative
  lookup, and only the videos those creatives reference get pulled. Use
  `--full-video-crawl` to seed the whole library once (or periodically), not
  as part of a daily job.
- **Optional creator-platform bridge.** If you upload creator-marketing
  videos to the ad account through a tool that stamps a naming convention on
  the title (this ships an example parser for
  [Reacher's](reacher.md) convention,
  `<creator_handle>_<Mon><Year>_RCHR_<hex>`), the video's title can be parsed
  straight into `meta_ad_videos.creator_handle` with no extra API calls —
  bridging "did this creator's video work as a Meta ad" back to whichever
  platform you manage that creator relationship through. This is entirely
  optional: unmatched titles just leave those columns NULL, so skip it (or
  swap in your own pattern) if you don't use a matching convention. As with
  any creator identity join, match on the **handle**, not on a display name —
  see [docs/tiktok-shop.md](tiktok-shop.md) for the same trap on that side.

### Tests

`tests/test_meta_ads_detail_sync.py`
