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

All flags:

| Flag | Default | Effect |
|---|---|---|
| `--backfill` | off | Pulls the deepest history available for every grain. Weekly grains start at `--backfill-start`, and the `--sample-days`/`--creator-days` recency windows are dropped, so the full population is fetched. |
| `--backfill-start YYYY-MM-DD` | 2 years back | Earliest week for weekly grains under `--backfill`. Reacher returns empty windows before your real data floor, so you don't need the exact date. |
| `--days N` | metrics: 30; GMV Max: 90 | Overrides the daily-metrics window. It also narrows the `gmv_max`/`gmv_max_products` window, which otherwise always pulls the full 90-day cap and can never exceed it. |
| `--weeks N` | `2` | Weeks of weekly-grain history on an incremental run: the current partial week plus last week's restatements. |
| `--creator-min-gmv X` | `0.01` | Minimum GMV for a `reacher_creator_weekly` row. Use `0` to keep zero-earning creator-weeks too. Because of this filter, row counts there are **not** "active creators"; get that from `reacher_metrics_daily`. |
| `--sample-days N` | `90` | Refreshes only samples updated in the last N days. Ignored with `--backfill`. |
| `--creator-days N` | `30` | Refreshes the creator snapshot only for creators touched in the last N days. Ignored with `--backfill`. |
| `--only GRAIN` / `--skip GRAIN` | all | Repeatable. Grains: `metrics`, `shop_gmv`, `gmv_max`, `gmv_max_products`, `creators`, `creator_weekly`, `creator_products`, `product_weekly`, `samples`, `sample_requests_weekly`, `automation_products`, `sample_products`, `video_creative`, `shop_health`, `automations`, `outreach_weekly`. |
| `--pages N` | `0` (no cap) | Caps pages per paginated grain. Useful for a quick probe. |
| `--dry-run` | off | Fetches and reports without writing rows. Tables are still created. |

With `REACHER_API_KEY` unset, the script prints a skip line and exits
cleanly.

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
- **The video feed has known defects.** Some `posted_date` values are Unix
  epoch zero or future-dated placeholders (this connector nulls them rather
  than storing a lie), and `/products/{id}/creators`'s `gmv` field is NULL on
  every row observed — use `reacher_creator_product_weekly` (from
  `/videos/leaderboard`) for real creator-x-product attribution instead.
- **`tc_invites` counts invitation batches; `accepted_tc_count` and
  `tc_invites_creator_count` count creators.** Pair `accepted_tc_count` with
  the creator count, not `tc_invites` — and only trust the open-vs-Target-
  Collab split at weekly/monthly grain, not per-day.
- **`/creators/performance`'s `total_count` (and `reacher_metrics_daily`'s
  `creators` metric) has been observed over-reporting** distinct creators on
  older history — sometimes more in a single month than the account's entire
  lifetime population. Spot-check before reporting; `reacher_creator_weekly`
  (filtered to earners) has checked out reliably even when the raw count
  looks inflated.

## Writing back to Reacher — `reacher_sample_limits.py`

Everything above is read-only. This is a separate, deliberately human-invoked
script that can cap (or clear) how many free samples Reacher will
**auto-approve** for creators, per product, per month — useful when a product
is selling out and you want to stop new automated samples for it without
touching anything else, then remove the cap again later.

It's a distinct file rather than an addition to `reacher_sync.py` on purpose:
that connector's `_assert_read_only()` guard refuses any write outright, so
an unattended sync can never use the write half of a `read_write`-scoped API
key by accident. This script is the deliberate exception, kept separate so
that guard stays absolute.

```bash
python reacher_sample_limits.py zero --product-id 1729401428505563994             # dry run
python reacher_sample_limits.py zero --product-id 1729401428505563994 --yes       # actually cap it to 0
python reacher_sample_limits.py zero --sku YOUR-SKU --reason "selling out" --yes
python reacher_sample_limits.py zero --product-id 1729401428505563994 --limit 5 --yes  # cap to 5, not 0
python reacher_sample_limits.py reset --product-id 1729401428505563994 --yes      # clear the cap
python reacher_sample_limits.py reset --all --yes                                 # clear every override this tool set
python reacher_sample_limits.py status                                            # tracked overrides + live drift check
```

**Every command defaults to a dry run** that prints what it would do; pass
`--yes` to actually call the API. `zero` defaults to a cap of `0`; pass
`--limit N` to cap at some other positive number instead (the subcommand name
is a bit of a misnomer once you use `--limit`).

**Identity:** a Reacher `productId` is your TikTok Shop SPU (item_group_id).
This script needs no warehouse table to resolve one — pass `--product-id`
directly (most reliable), or `--sku`, which matches against the `sku` field
Reacher itself returns on each existing product-config row (i.e. whatever SKU
value your Reacher account already has on file for that product — independent
of any catalog data in your own warehouse). A product Reacher was never given
a SKU for won't resolve by `--sku`; use `--product-id` for those. If you've
built your own SKU → TikTok-product-id mapping, extend `resolve_by_sku()` to
check it first.

**This does not fully stop sampling.** `monthlySampleLimit` only caps
Reacher's own automated approval funnel for that product. Most TikTok Shop
sellers can also approve sample requests manually in Seller Center, and this
endpoint has no reach into that path — a human reviewer can still approve a
request regardless of what this script sets.

**State:** active overrides are tracked in a local
`reacher_sample_limit_overrides.json` (next to the script, not the warehouse
database — this is operational state about a live external system, not
warehouse data), so `reset --all` needs no memorized product list. Every
actual write is also appended to `reacher_sample_limits_log.txt` as a
plain-text audit trail.

## Tests

`tests/test_reacher_sync.py`, `tests/test_reacher_sample_limits.py`
