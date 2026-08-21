# Reacher (TikTok Shop affiliate/creator platform)

Creator, sample, GMV Max ad-spend, and affiliate-funnel history from
[Reacher](https://www.reacherapp.com) — a third-party platform many TikTok
Shop sellers run their creator/affiliate program through. Only relevant if
your shop's outreach automations, Target Collab invites, sample approvals,
and/or GMV Max ad campaigns are managed via Reacher rather than (or alongside)
TikTok's own Shop API.

**Script:** `reacher_sync.py` (standalone — not wired into `run_sync.py`)

## Why this exists alongside the core TikTok Shop connector

TikTok's own Shop API (the core `tiktok` platform, plus the video/live/
creator/analytics extras — see [tiktok-shop.md](tiktok-shop.md)) doesn't
expose everything a creator-marketing program needs:

- **TikTok ad spend, if you have no other source for it.** TikTok Ads API
  access is a separate approval path from TikTok Shop access and isn't
  always available. If your GMV Max campaigns run through Reacher, this
  connector gets that spend into the warehouse regardless, mirroring it into
  the shared `ad_metrics` table as `platform='tiktok'` — see the module
  docstring's ATTRIBUTION TRAP section before trusting the *revenue* side of
  that mirror; the spend side is solid.
- **Creator-level GMV with a date dimension** (most creator exports are
  lifetime-only snapshots with no dates).
- **Sample OUTCOMES**, not just the approval queue — what a sample actually
  went on to sell.
- **AI creative analysis** of top-performing videos (hook text/
  classification, sell points, shot style).

Deliberately does NOT re-sync things TikTok's own Shop API already covers
well on most accounts (basic video/live metrics) — see the module docstring's
"DELIBERATELY NOT SYNCED" section.

## Setup

1. Get an API key from your Reacher portal (Settings > API).
2. Find your shop id (visible in the portal URL, or via `GET /shops` with your
   key).
3. Set in `.env`:

   | Variable | Notes |
   |---|---|
   | `REACHER_API_KEY` | from the Reacher portal |
   | `REACHER_SHOP_ID` | your shop's numeric id in Reacher |

## Usage

```bash
python reacher_sync.py                       # nightly incremental (a few minutes)
python reacher_sync.py --backfill            # deepest available history for every grain
python reacher_sync.py --only gmv_max        # one grain (repeatable)
python reacher_sync.py --skip video_creative # skip the slowest grain
python reacher_sync.py --dry-run             # probe, write nothing
```

Run `python reacher_sync.py --help` for the full flag list (window overrides,
`--creator-min-gmv`, `--backfill-start`, `--pages`).

**Run the GMV Max grain (or the whole script) on a real schedule if you use
GMV Max ads at all.** `shop-gmv` and `gmv-max/*` both hard-cap at 90 days of
history on Reacher's side — miss 90 consecutive days and that ad-spend
history is permanently unrecoverable, not just delayed.

## Tables

All `reacher_*`, created by this script (nothing added to shared
`schema.sql`):

- `reacher_metrics_daily` — long-format daily affiliate-funnel metrics
- `reacher_shop_gmv_daily` — Seller Center daily GMV with affiliate/seller split
- `reacher_gmv_max_campaign`, `reacher_gmv_max_daily` — GMV Max ad campaign
  state + daily spend/performance (also mirrored into `ad_metrics`)
- `reacher_gmv_max_product_daily` — TikTok ad spend per product per day
- `reacher_creator`, `reacher_creator_weekly` — creator snapshot + weekly earnings
- `reacher_creator_product_weekly`, `reacher_creator_video_weekly` — creator x
  product video attribution
- `reacher_sample_request`, `reacher_sample_request_weekly` — sample rows
  (current-state and dated-by-created-week)
- `reacher_product_weekly` — per-product weekly GMV/commission/refunds
- `reacher_sample_product_weekly` — sample ROI by product
- `reacher_automation`, `reacher_automation_product`, `reacher_outreach_weekly`
  — outreach program state and the open-vs-Target-Collab sample split
- `reacher_video_creative` — top-N weekly videos + AI creative breakdown
- `reacher_shop_health_daily` — TikTok Shop Performance Score
- `reacher_sync_state` — internal bookkeeping

## Notes

- **Read-only by design, defensively enforced.** Reacher API keys are
  commonly provisioned with read/write scope even for read-only use cases.
  This connector refuses any HTTP method other than GET/POST-as-read, and
  additionally refuses any path matching a known write-surface pattern
  (creating invites, replying to creator messages, archiving automations,
  settling payments) — so a write example accidentally copied out of the
  Reacher docs into this file fails loudly instead of silently taking a real
  action against your creator roster.
- **Cloudflare 1010 blocks a default/missing User-Agent** on every endpoint,
  which reads exactly like a dead API key. The connector sends an explicit UA
  defensively.
- **`commission_rate` is not a safe field to compute from** — it has been
  observed at inconsistent scales across responses. Only `est_commission` (a
  money value, and explicitly an *estimate*, not a settled payout) is used
  for anything numeric.
- **Email is never stored**, even though the sample-requests endpoint returns
  it. Public profile fields (bio, categories) are kept for niche-fit triage.
- Rate limits (3,000/hr and 60/min) are self-paced proactively rather than
  handled by retrying 429s reactively.

## Tests

`tests/test_reacher_sync.py`
