r"""
Reacher (TikTok Shop affiliate/creator-marketing platform) -> creator, sample,
ad-spend and affiliate-funnel history.

Reacher is a third-party platform many TikTok Shop sellers run their creator
program through (outreach automations, Target Collab invites, sample
approvals, GMV Max ad campaigns). Its Data API re-exposes a lot of TikTok
Seller Center / Affiliate Center state that TikTok's own Shop API either
doesn't expose at all or doesn't retain history for. If your store's TikTok
creator/affiliate program runs through Reacher, this connector is the only
practical way to get that state into a warehouse instead of living only in
Reacher's own dashboard.

Base https://api.reacherapp.com/public/v1, headers `x-api-key` + `x-shop-id`.
Docs https://portal.reacherapp.com/docs/api  Spec /api/public-api/openapi.json

================================================================================
WHAT THIS UNIQUELY ADDS
================================================================================
1. **TikTok AD SPEND, if you have no other source for it.** Many sellers can't
   get TikTok Ads API access (dev-portal registration for that product is a
   separate, sometimes-rejected approval path from TikTok Shop access), which
   leaves a real gap: no `ad_metrics` rows for TikTok at all. Reacher's GMV Max
   endpoints carry that spend even without TikTok Ads API access, since GMV Max
   is a TikTok Shop advertising product Reacher manages on your behalf. This
   connector mirrors it into the shared `ad_metrics` table as `platform='tiktok'`
   so `spend_summary` / `top_campaigns` and any blended-ROAS/MER math pick it up
   automatically — see the ATTRIBUTION TRAP section below before you trust the
   *revenue* side of that mirror, though; the spend side is solid.

2. **Creator-level GMV WITH a date dimension.** A one-off creator export is
   typically a lifetime snapshot with no dates, so "which creators drove this
   week" is unanswerable from it. Reacher's `/creators/performance` endpoint is
   date-filtered, giving you a real per-week creator earnings series.

3. **Sample OUTCOMES, not just the approval queue.** Most sample-management
   surfaces show you status/expiry/approvability — whether a sample WAS SENT.
   Reacher's sample data includes what that sample went on to sell
   (gmv/units_sold per sample), which is what an actual sample-ROI calculation
   needs.

4. **AI creative analysis of top videos.** Hook text and its classification,
   derived sell points, shot style, videography notes — a description of WHY a
   video worked, not just how many views it got.

Plus two cheap extras: a daily affiliate-funnel metric series, and TikTok
Shop's own Performance Score.

================================================================================
THE GMV MAX ATTRIBUTION TRAP — READ BEFORE USING gross_revenue
================================================================================
A common GMV Max setup is ONE full-catalog "shop the whole store" campaign
plus a long tail of narrower, often-paused campaigns. When that's the shape of
your account, the full-catalog campaign's *attributed* GMV can end up close to
— or even exceeding — 100% of your entire shop's actual GMV for the period,
and its order count can approach 100% of every order the shop took, paid or
organic. That is expected behavior of a full-catalog placement, not a data
error, but it means:

  * `spend` is real, incremental, and belongs to you alone — use it freely.
  * `gross_revenue` / `roas` / `ad_roi` on that campaign are NOT incremental
    ad-driven revenue. A ROAS computed that way is functionally
    (whole-shop GMV) / (TikTok ad spend) — a blended MER wearing a ROAS
    costume. Never add it to affiliate GMV, never add it to your own
    site-wide GMV total, and never present it as "ads drove $X" without that
    caveat attached.
  * This connector still stores that revenue, in `ad_metrics.revenue`,
    because that column's existing contract elsewhere in this project is
    already "platform-attributed, do NOT sum across platforms" — it's the
    correct home for a number like this, it just needs to never be summed.

Check your own account's campaign mix (spend per campaign, and whether one
campaign targets the full catalog) before trusting `roas` on this feed.

================================================================================
HISTORY DEPTH — VARIES SHARPLY BY ENDPOINT, AND SOME CAPS ARE PERMANENT
================================================================================
Reacher OMITS days/weeks with no data rather than returning zeros for them, so
asking for a wider window than your real data floor is cheap (a few extra
requests) and never stores anything false. That means you don't need to know
your exact per-endpoint floor in advance — just ask wide and let empty
responses tell you where your data starts.

What's worth knowing before you plan a backfill:
  * `metrics/timeseries` — by far the deepest history of any endpoint here;
    posting-volume-shaped metrics (e.g. `videos_posted`) tend to reach back
    years further than revenue metrics (`gmv`), because affiliate GMV
    tracking is often turned on well after creators start posting.
  * `shop-gmv/timeseries` and `gmv-max/{id}/metrics` — both HARD-CAP at 90
    days (400 INVALID_REQUEST past that). This is a real, permanent API
    limit, not a bug: history on these two endpoints can only ever accrue
    FORWARD from whenever you start running this connector. **This is the
    entire argument for running the GMV Max sync daily** — miss 90
    consecutive days and that ad-spend history is gone for good, with no way
    to recover it later.
  * `shop-health/timeseries` — starts accruing from whenever Reacher turned
    on Shop Performance Score tracking for your account; not backfillable
    before that.
  * `samples/by-product` — has its own, typically-later floor than
    `samples/list`; requesting earlier weeks just returns empty pages.
  * `videos` / `videos/creative` — real post history can reach back several
    years, but `videos/creative`'s top-N ranking is only meaningful once GMV
    tracking exists (ranking by GMV before GMV exists ranks nothing).

**A known defect worth checking against your own account before trusting
older data:** `/creators/performance`'s `total_count` (and the matching
`creators` metric in the timeseries) has been observed to report MORE
distinct creators in a single month than the account's entire lifetime
creator population — a single month literally cannot contain more distinct
creators than exist, so that's either double-counting or a collection change
somewhere in Reacher's pipeline. Recent history has checked out consistently
(a trailing-30-day `total_count` matching `metrics/summary`'s
`active_creators` exactly), so treat any large `reacher_metrics_daily`
`creators` value as a KPI to spot-check against your own creator population
before reporting it, especially on older history. `reacher_metrics_daily` in
general should never be summed for this metric — it's distinct-PER-DAY, so
summing double-counts by design regardless of the defect above.

The creator WEEKLY grain (`reacher_creator_weekly`) is safe even over
questionable history, because it's filtered to earners (`min_gmv`) rather than
reporting a raw distinct count — a filtered week has been verified to line up
sensibly with known GMV floors even in months where the unfiltered
`total_count` looks inflated.

================================================================================
COMMISSION — what this feed gives you, and what it does NOT
================================================================================
Affiliate commission is a real cost line most warehouses have no clean TikTok
source for. This feed carries it at two useful grains, both in the money-typed
`est_commission` field:
  * creator x week   `reacher_creator_weekly.est_commission`
  * product x week   `reacher_product_weekly.est_commission`

**IT IS AN ESTIMATE, NOT A SETTLED PAYOUT.** Every relevant field in Reacher's
own schema is literally named `est_commission` / `estimate_commission` — there
is no settled-commission endpoint in the API. Actual paid commission lives in
TikTok Shop's own settlement reports, which this API does not expose (if your
shop settlement data matters, see whether your TikTok Shop connector exposes a
Finance/settlement API directly). Use `est_commission` for margin MODELLING,
not for reconciling against finance.

**DO NOT compute commission from `commission_rate`.** That field has been
observed to arrive at inconsistent scales in different responses from the same
account within the same session (e.g. one product returning `10.0` and another
`1000.0` for what should be a comparable percentage, and a different endpoint
returning `100`). Treat any `commission_rate` value from this API as an
untyped magnitude you cannot safely do arithmetic on. `est_commission` (a
money value) is the only field this connector trusts for anything numeric.

With `reacher_product_weekly` (GMV, commission, refunds) and
`reacher_gmv_max_product_daily` (ad spend, per product per day), a rough
TikTok-specific per-product contribution margin becomes computable:
`gmv - your own COGS - est_commission - ad cost - refunds`. Refunds in
particular are worth including at this grain, not treating as a rounding
error — they can run well into double-digit percent of a product's GMV.

================================================================================
OPEN COLLAB vs TARGET COLLAB — how to split sample requests
================================================================================
An individual sample request row carries no field distinguishing "creator
requested it via open collab" from "creator accepted a Target Collab invite
that included a free sample." Every field across the sample endpoints was
checked for this at build time; there is no per-request origin anywhere.

The split IS recoverable shop-wide, from `/automations/list`'s `aggregate`
block: **`accepted_tc_count` is creator-level accepted Target Collabs, and
because a TC offering free samples makes the acceptance itself a sample
request, it is effectively the TC-sourced slice of `sample_request`** — open
collab is the residual (`sample_request - accepted_tc_count`). Trust this at
weekly/monthly grain, not per-day: an acceptance can be stamped on a different
day than the request it satisfies, so a handful of days can show
`accepted_tc_count` exceeding that same day's `sample_request` without that
meaning anything is wrong.

**The unit trap that makes these counters look broken at first glance:**
`tc_invites` counts invitation BATCHES, while `tc_invites_creator_count`
counts CREATORS invited. The `tc_invites_sent` metric in the timeseries is
also a batch count. Comparing `accepted_tc_count` (creators) against
`tc_invites_sent` (batches) will read as "more accepted than sent," which is a
units mismatch, not a data defect — always pair `accepted_tc_count` with the
CREATOR count.

Two fields not worth reaching for: `open_collabs` in the aggregate answers a
different question (affiliates added via open collaboration, not sample
requests) and is commonly near-zero even on accounts with heavy open-collab
sample activity; `tc_acceptance_rate` from the API is computed as the mean of
daily rates rather than a proper window ratio, so it can return values well
outside a sane 0-100% range — derive the rate yourself as
`accepted_tc_count / tc_invites_creator_count`.

Per-automation attribution also works at product-SET grain via
`/automations/{id}`'s `details.target_collab.products[]`, alongside that
automation's own sample/acceptance counts — useful for tying a request spike
on a specific product to a specific outreach push. Note the per-automation
stats view ages out idle automations, so the shop-wide `aggregate` (used
above) will not equal the sum of individual automation rows, and should be
treated as the authoritative total.

================================================================================
KNOWN DATA DEFECTS IN THE VIDEO FEED
================================================================================
  * A meaningful slice of videos can carry `posted_date = 1970-01-01T00:00:00Z`
    (Unix epoch zero) — every one observed with zero views and zero GMV. These
    sort to the front of an ascending date order and must be filtered, never
    trusted as a genuine series start. This connector nulls `posted_date`
    rather than storing the epoch value (`_is_bad_posted_date`).
  * Future-dated posts can appear (placeholder/scheduled entries), also with
    zero engagement — filtered the same way.
  * Most videos carry zero GMV; a mean-per-video metric over the unfiltered
    set will be dominated by that, not by the videos that actually sold
    anything.
  * `/products/{id}/creators` returns the creator roster for a product but its
    `gmv` field is NULL on every row observed — it answers "who is
    affiliated," not "who sold." Use `reacher_creator_product_weekly` (from
    `/videos/leaderboard`) for actual attribution.

================================================================================
RATE LIMITS / TRANSPORT GOTCHAS
================================================================================
  * 3,000 requests/hour AND 60/minute per key; either one trips HTTP 429. The
    hourly ceiling is what actually binds on a long backfill (3,000/hr ≈
    50/min sustained), so `_throttle()` self-paces below both limits and
    tracks a rolling hourly window, rather than relying on catching 429s.
  * **Cloudflare error 1010 blocks a default/missing User-Agent.** A request
    with no explicit UA (or a bare interpreter-default one) gets 403 "browser
    signature banned" on EVERY endpoint — indistinguishable from a dead API
    key at a glance. `requests`' own default UA has been observed to work,
    but this connector sets an explicit one so that can never silently
    resurface if the HTTP library ever changes.
  * Some endpoints are genuinely slow, not hung: creative-analysis and bulk
    list endpoints can take tens of seconds per call. The timeout here is set
    generously on purpose.
  * Occasional Cloudflare 502s are treated as transient and retried like any
    5xx.
  * A "Social Intelligence" tier (if Reacher exposes one on your account)
    returns 404 when it isn't enabled on your key — not an error to chase,
    just an unavailable feature tier.

================================================================================
TABLES (all `reacher_*`, all created here — nothing added to shared schema.sql)
================================================================================
  reacher_metrics_daily          (date, metric) long-format daily affiliate
                                 funnel series. SIX metrics are RATIOS —
                                 gmv_per_video, gmv_per_sample, aov, ctr,
                                 conversion_rate, reply_rate. AVERAGE them,
                                 never SUM, same rule as any impression-share
                                 metric elsewhere in this project.
  reacher_shop_gmv_daily         Seller Center daily GMV, 90-day rolling, WITH
                                 the affiliate-vs-seller split inside video and
                                 live GMV (and shop_tab-vs-search inside
                                 product_card GMV).
  reacher_gmv_max_campaign       current GMV Max campaign state (budget,
                                 target ROAS, status).
  reacher_gmv_max_daily          campaign x date spend/impressions/clicks/
                                 orders/revenue. Non-zero days ALSO mirrored to
                                 `ad_metrics` as platform='tiktok'.
  reacher_gmv_max_product_daily  product x date TikTok AD COST — ad spend
                                 attributed to an individual product/style,
                                 which most ad platforms don't expose below
                                 campaign grain.
  reacher_creator_product_weekly creator x product x week video GMV/units/
                                 distinct-video-count — which creator sells
                                 which product.
  reacher_creator_video_weekly   the creator totals from the same leaderboard
                                 call (total video GMV, units, qualifying
                                 videos).
  reacher_creator                current-state creator snapshot: level,
                                 fulfillment rate, lifetime shop + overall GMV.
  reacher_creator_weekly         creator x Monday-week GMV/units/orders/
                                 commission. EARNERS ONLY by default — the vast
                                 majority of creator-weeks have zero GMV, so
                                 zero-GMV rows are filtered out unless you pass
                                 `--creator-min-gmv 0`. NEVER count rows here to
                                 get "active creators" — use
                                 `reacher_metrics_daily`'s `creators` metric
                                 (with the caveats above).
  reacher_product_weekly         per-product weekly GMV/commission/refunds and
                                 Seller Center funnel.
  reacher_sample_request         current-state sample rows with gmv/units +
                                 creator bio/categories for niche-fit triage.
                                 EMAIL IS NOT STORED.
  reacher_sample_request_weekly  sample requests dated by the week they were
                                 CREATED (recovered from the query window, not
                                 a field on the row — see the DDL comment).
  reacher_automation_product     which products each Target Collab automation
                                 offers, for attributing request spikes to a
                                 specific outreach push.
  reacher_sample_product_weekly  product x week requests/approved/sample_gmv/
                                 videos_from_samples — the sample-ROI grain.
  reacher_video_creative         weekly top-N videos + AI hook/sell-points/
                                 shot-style/videography breakdown.
  reacher_shop_health_daily      TikTok Shop Performance Score (0-5) + its
                                 component dimensions.
  reacher_automation             outreach automations: reach, reply rate, GMV.
  reacher_outreach_weekly        shop-wide outreach aggregate per week — the
                                 only place the open-vs-target-collab sample
                                 split exists (see above).
  reacher_sync_state             high-water marks / incremental bookkeeping.

PII: creator `email` (available from `/samples/list`) is deliberately DROPPED.
Public profile fields (bio/categories) are kept, since they're what makes
sample-request niche-fit triage possible. If you add PII redaction to
`server.py`'s remote column denylist elsewhere in your deployment, treat any
future email/phone field the same way.

DELIBERATELY NOT SYNCED here, worth reconsidering only if your account differs:
  * basic video view/like/comment metrics and LIVE-shopping sessions, if your
    TikTok Shop connector's own `tiktok_shop_videos`/`tiktok_shop_lives`
    tables already cover them more completely — Reacher's video/live coverage
    has been observed to be materially thinner than TikTok's own Shop API for
    these.
  * Creator Community campaigns, if your shop doesn't run any.
  * creator DM inbox contents — operational, not analytical, and PII-heavy.
  * CRM groups/lists — program configuration that changes constantly and adds
    little analytical value.
  * Social Intelligence — only sync it if that tier is actually enabled on
    your key.

USAGE:
  python reacher_sync.py                       # nightly incremental (a few minutes)
  python reacher_sync.py --backfill            # deepest available history for every grain
  python reacher_sync.py --only gmv_max        # one grain (repeatable)
  python reacher_sync.py --skip video_creative # skip the slow one
  python reacher_sync.py --only metrics --days 540
  python reacher_sync.py --weeks 12            # deeper creator/sample weeks
  python reacher_sync.py --creator-min-gmv 0   # keep zero-GMV creator-weeks too
  python reacher_sync.py --backfill-start 2024-01-01
  python reacher_sync.py --dry-run             # probe, write nothing

The nightly run deliberately re-pulls overlapping windows (metrics, creator/
sample/creative weeks, sample rows) because Reacher restates recent data: GMV
and video metrics keep moving for a few days after the fact, and sample status
changes can land long after the original request. Every write here is an
upsert, so re-pulling an overlapping window is free (no duplication, and it's
exactly what keeps restated numbers current).

sync_log platforms, logged independently so one grain's failure can't mask
another's: 'reacher_metrics', 'reacher_shop_gmv', 'reacher_gmv_max',
'reacher_gmv_max_products', 'reacher_creators', 'reacher_creator_weekly',
'reacher_creator_products', 'reacher_product_weekly', 'reacher_samples',
'reacher_sample_requests_weekly', 'reacher_automation_products',
'reacher_sample_products', 'reacher_video_creative', 'reacher_shop_health',
'reacher_automations', 'reacher_outreach_weekly'.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from warehouse import db

load_dotenv()

BASE = "https://api.reacherapp.com/public/v1"

# Cloudflare 1010 bans a default/missing UA outright (403 on every endpoint,
# indistinguishable from a revoked key at a glance). Always send our own.
USER_AGENT = "ecomm-mcp-server-reacher-sync/1.0"

REQUIRED_ENV = ("REACHER_API_KEY", "REACHER_SHOP_ID")

PAGE_SIZE = 100          # documented max on every paginated POST body
CREATIVE_LIMIT = 50      # documented max on /videos/creative
WINDOW_CAP_DAYS = 90     # shop-gmv and gmv-max both hard-fail past this
METRICS_CHUNK_DAYS = 180  # no documented cap on metrics/timeseries; chunk anyway

# 3,000/hr AND 60/min. The hourly ceiling is what actually binds a backfill
# (50/min sustained), so pace below it rather than discovering 429s.
MIN_INTERVAL = 1.3       # seconds between requests -> ~46/min
HOURLY_CAP = 2_900       # leave headroom under 3,000

# Generic backfill horizon. Reacher omits days/weeks with no data rather than
# erroring, so asking wider than your account's real floor just costs a few
# harmless extra requests -- there's no need to know your exact per-endpoint
# floor before running --backfill. Override with --backfill-start if you've
# already discovered your account's actual data floor and want a tighter run.
DEFAULT_BACKFILL_YEARS = 2

DEFAULT_WEEKS = 2                     # current (incomplete) week + last week's restatements
INCREMENTAL_METRIC_DAYS = 30          # daily run re-pulls a month (restatements)

# Most creator-weeks earn nothing on a typical account, so the weekly grain
# defaults to earners-only to avoid storing a large volume of all-zero rows.
# Consequence to remember: you CANNOT count rows in reacher_creator_weekly to
# get "active creators" -- that number lives in reacher_metrics_daily (and
# metrics/summary active_creators). Pass --creator-min-gmv 0 to keep every row.
DEFAULT_CREATOR_MIN_GMV = 0.01

# Full-table snapshots are wasteful on a nightly run, and both endpoints
# accept a recency window (only creators/samples touched in the last N days).
# --backfill drops the window and takes everything.
DEFAULT_SAMPLE_DAYS = 90
DEFAULT_CREATOR_SNAPSHOT_DAYS = 30

ALL_METRICS = [
    # money / volume
    "gmv", "total_gmv", "live_gmv", "orders", "units_sold",
    # creator + content
    "creators", "videos_posted", "video_views", "gmv_driving_videos",
    "new_creators_posting",
    # samples
    "sample_requests", "samples_approved",
    # outreach funnel
    "creators_reached", "creators_messaged", "tc_invites_sent",
    "accepted_tc_count", "open_collabs", "emails_sent", "dm_responses",
    # RATIOS — average, never sum
    "gmv_per_video", "gmv_per_sample", "aov", "ctr", "conversion_rate",
    "reply_rate",
]
RATIO_METRICS = {"gmv_per_video", "gmv_per_sample", "aov", "ctr",
                 "conversion_rate", "reply_rate"}

DDL = """
CREATE TABLE IF NOT EXISTS reacher_metrics_daily (
    date        TEXT NOT NULL,
    metric      TEXT NOT NULL,       -- see ALL_METRICS
    value       REAL,
    synced_at   TEXT,
    PRIMARY KEY (date, metric)
);
CREATE TABLE IF NOT EXISTS reacher_shop_gmv_daily (
    date                       TEXT PRIMARY KEY,
    currency                   TEXT,
    gmv                        REAL,   -- whole shop, all channels
    orders                     INTEGER,
    items_sold                 INTEGER,
    customers                  INTEGER,  -- unique PER DAY; summing double-counts
    aov                        REAL,     -- RATIO
    video_gmv                  REAL,
    video_affiliate_gmv        REAL,   -- creator-driven
    video_seller_gmv           REAL,   -- your own brand-account videos
    live_gmv                   REAL,
    live_affiliate_gmv         REAL,
    live_seller_gmv            REAL,
    product_card_gmv           REAL,
    product_card_shop_tab_gmv  REAL,
    product_card_search_gmv    REAL,
    product_impressions        INTEGER,
    product_clicks             INTEGER,
    synced_at                  TEXT
);
CREATE TABLE IF NOT EXISTS reacher_gmv_max_campaign (
    campaign_id       TEXT PRIMARY KEY,
    campaign_name     TEXT,
    status            TEXT,   -- ENABLE / DISABLE
    shopping_ads_type TEXT,   -- PRODUCT / LIVE
    budget            REAL,
    roas_bid          REAL,   -- target ROAS
    currency          TEXT,
    last_synced_at    TEXT,   -- Reacher's own sync stamp, not ours
    synced_at         TEXT
);
CREATE TABLE IF NOT EXISTS reacher_gmv_max_daily (
    campaign_id   TEXT NOT NULL,
    date          TEXT NOT NULL,
    spend         REAL,      -- real and unique to this feed
    impressions   INTEGER,
    clicks        INTEGER,
    orders        INTEGER,
    gross_revenue REAL,      -- NOT reliably incremental. See the attribution-trap note.
    cpc           REAL,
    cpm           REAL,
    ctr           REAL,      -- RATIO
    roas          REAL,      -- RATIO, and often a blended MER in practice
    ad_roi        REAL,      -- RATIO
    currency      TEXT,
    synced_at     TEXT,
    PRIMARY KEY (campaign_id, date)
);
CREATE TABLE IF NOT EXISTS reacher_gmv_max_product_daily (
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    product_id    TEXT NOT NULL,   -- TikTok SPU / item_group_id
    cost          REAL,            -- per-product TikTok ad spend
    gross_revenue REAL,            -- TikTok-attributed; same caveat as campaign level
    orders        INTEGER,         -- SKU orders
    roi           REAL,            -- RATIO (TikTok's stored daily value)
    ad_roi        REAL,            -- RATIO (gross_revenue / cost, recomputed)
    currency      TEXT,
    synced_at     TEXT,
    PRIMARY KEY (date, campaign_id, product_id)
);
CREATE TABLE IF NOT EXISTS reacher_creator_product_weekly (
    week_start     TEXT NOT NULL,
    creator_handle TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    product_name   TEXT,
    video_gmv      REAL,
    units_sold     INTEGER,
    video_count    INTEGER,   -- DISTINCT videos for this creator+product
    synced_at      TEXT,
    PRIMARY KEY (week_start, creator_handle, product_id)
);
CREATE TABLE IF NOT EXISTS reacher_creator_video_weekly (
    week_start             TEXT NOT NULL,
    creator_handle         TEXT NOT NULL,
    total_video_gmv        REAL,
    total_units_sold       INTEGER,
    qualifying_video_count INTEGER,
    product_count          INTEGER,
    synced_at              TEXT,
    PRIMARY KEY (week_start, creator_handle)
);
CREATE TABLE IF NOT EXISTS reacher_creator (
    creator_handle            TEXT PRIMARY KEY,
    creator_id                TEXT,   -- only /creators/performance returns it
    follower_count            INTEGER,
    status                    TEXT,
    creator_level             TEXT,   -- e.g. an L0-L7 GMV tier
    shop_gmv                  REAL,   -- lifetime, this shop
    overall_gmv               REAL,   -- lifetime, all of TikTok (USD-normalized)
    shop_units_sold           INTEGER,
    shop_video_count          INTEGER,
    video_views               INTEGER,
    sample_received           INTEGER,
    commission_rate           REAL,
    est_commission            REAL,
    fulfillment_rate          REAL,
    overall_fulfillment_rate  REAL,
    tags                      TEXT,   -- JSON array
    product_id                TEXT,
    product_title             TEXT,
    created_at                TEXT,
    updated_at                TEXT,
    synced_at                 TEXT
);
CREATE TABLE IF NOT EXISTS reacher_creator_weekly (
    week_start      TEXT NOT NULL,   -- Monday
    creator_handle  TEXT NOT NULL,
    creator_id      TEXT,
    gmv             REAL,
    units_sold      INTEGER,
    order_count     INTEGER,
    est_commission  REAL,
    follower_count  INTEGER,
    synced_at       TEXT,
    PRIMARY KEY (week_start, creator_handle)
);
CREATE TABLE IF NOT EXISTS reacher_sample_request (
    creator_handle  TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    product_title   TEXT,
    status          TEXT,
    gmv             REAL,      -- what the sample went on to earn
    units_sold      INTEGER,
    sample_received INTEGER,
    bio             TEXT,      -- creator profile text (niche fit triage)
    categories      TEXT,      -- JSON array
    updated_at      TEXT,
    first_seen_at   TEXT,      -- ours: when this connector first saw the row
    synced_at       TEXT,
    -- The feed exposes no row id, so this is the best available key. A creator
    -- re-requesting the SAME product collapses onto one row (rare; accepted).
    PRIMARY KEY (creator_handle, product_id)
);
CREATE TABLE IF NOT EXISTS reacher_product_weekly (
    -- Per-product weekly economics. `est_commission` and `sc_refunds` are the
    -- two lines nothing else typically has at product grain for TikTok; with
    -- reacher_gmv_max_product_daily.cost (ad spend per product) and your own
    -- COGS source, that's enough for per-style contribution margin:
    --   gmv - COGS - commission - ad cost - refunds.
    week_start           TEXT NOT NULL,
    product_id           TEXT NOT NULL,
    product_name         TEXT,
    product_status       TEXT,
    -- affiliate view (has been observed to track sc_affiliate_gmv closely,
    -- i.e. `gmv` here IS essentially the affiliate slice of the product)
    gmv                  REAL,
    est_commission       REAL,   -- ESTIMATE, not a settled payout. See docstring.
    units_sold           INTEGER,
    refund_units         INTEGER,
    video_count          INTEGER,
    sample_count         INTEGER,
    live_count           INTEGER,
    -- whole-shop Seller Center view of the same product
    sc_total_gmv         REAL,
    sc_affiliate_gmv     REAL,
    sc_seller_video_gmv  REAL,
    sc_seller_live_gmv   REAL,
    sc_product_card_gmv  REAL,
    sc_shop_tab_gmv      REAL,
    sc_refunds           REAL,
    sc_orders            REAL,
    sc_units_sold        REAL,
    sc_customers         REAL,   -- unique PER WINDOW; do not sum across weeks
    sc_impressions       REAL,
    sc_clicks            REAL,
    sc_add_to_cart       REAL,
    -- RATIOS. AVERAGE, NEVER SUM (same rule as any impression-share metric).
    sc_aov               REAL,
    sc_ctr               REAL,
    sc_conversion        REAL,
    sc_gmv_per_1k_impr   REAL,
    synced_at            TEXT,
    PRIMARY KEY (week_start, product_id)
);
CREATE TABLE IF NOT EXISTS reacher_sample_request_weekly (
    -- DATED sample requests at creator x product grain. The plain
    -- reacher_sample_request table cannot answer "when was this requested":
    -- the row carries only `updated_at` (a recent re-stamp, not a request
    -- date). /samples/list's start_date/end_date filter keys on CREATED_AT
    -- instead, so the request week is recovered from the WINDOW WE ASKED FOR,
    -- not from any field in the row -- that's why this table exists separately
    -- from reacher_sample_request.
    week_start      TEXT NOT NULL,   -- Monday of the created_at week
    creator_handle  TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    product_title   TEXT,
    status          TEXT,            -- fulfilment lifecycle, NOT collab source
    gmv             REAL,
    units_sold      INTEGER,
    sample_received INTEGER,
    synced_at       TEXT,
    PRIMARY KEY (week_start, creator_handle, product_id)
);
CREATE TABLE IF NOT EXISTS reacher_automation_product (
    -- Which products each Target Collab automation offered. Lets a request
    -- spike on a product be attributed to the automations that were live
    -- that week.
    automation_id            INTEGER NOT NULL,
    product_id               TEXT NOT NULL,
    product_name             TEXT,
    commission_rate          REAL,   -- see docstring: scale is NOT trustworthy
    shop_ads_commission_rate REAL,
    automation_name          TEXT,
    automation_type          TEXT,
    automation_status        TEXT,
    created_at               TEXT,
    completed_at             TEXT,
    valid_until              TEXT,   -- TC card expiry
    offer_free_samples       INTEGER,
    auto_approve_samples     INTEGER,
    synced_at                TEXT,
    PRIMARY KEY (automation_id, product_id)
);
CREATE TABLE IF NOT EXISTS reacher_sample_product_weekly (
    week_start          TEXT NOT NULL,
    product_id          TEXT NOT NULL,
    product_name        TEXT,
    total_requests      INTEGER,
    approved            INTEGER,
    sample_gmv          REAL,
    videos_from_samples INTEGER,
    synced_at           TEXT,
    PRIMARY KEY (week_start, product_id)
);
CREATE TABLE IF NOT EXISTS reacher_video_creative (
    week_start                    TEXT NOT NULL,
    video_id                      TEXT NOT NULL,
    rank                          INTEGER,
    title                         TEXT,
    creator_handle                TEXT,
    tiktok_url                    TEXT,
    posted_date                   TEXT,   -- epoch/future-dated rows nulled here
    video_gmv                     REAL,
    views                         INTEGER,
    like_count                    INTEGER,
    comment_count                 INTEGER,
    order_count                   INTEGER,
    analyzed                      INTEGER, -- 0 = no AI breakdown yet
    hook_text                     TEXT,
    hook_classification           TEXT,
    hook_reasoning                TEXT,
    sell_points                   TEXT,   -- JSON array
    product_niche                 TEXT,
    shot_style                    TEXT,   -- JSON array
    videography_locations         TEXT,   -- JSON array
    videography_lighting          TEXT,   -- JSON array
    videography_product_showcase  TEXT,   -- JSON array
    videography_notes             TEXT,
    missing_fields                TEXT,   -- JSON array; render as "not analyzed"
    synced_at                     TEXT,
    PRIMARY KEY (week_start, video_id)
);
CREATE TABLE IF NOT EXISTS reacher_shop_health_daily (
    date                     TEXT PRIMARY KEY,
    sps_score                REAL,   -- 0-5
    sps_tier                 TEXT,   -- e.g. EXCELLENT/GOOD/POOR/CRITICAL
    peer_percentile          REAL,
    product_satisfaction     REAL,
    fulfillment_logistics    REAL,
    customer_service         REAL,
    dimensions_json          TEXT,   -- full payload; dimension set may change
    synced_at                TEXT
);
CREATE TABLE IF NOT EXISTS reacher_automation (
    automation_id      INTEGER PRIMARY KEY,
    automation_name    TEXT,
    automation_type    TEXT,
    status             TEXT,
    status_message     TEXT,
    gmv                REAL,
    sample_requests    INTEGER,
    accepted_requests  INTEGER,
    videos_posted      INTEGER,
    videos_converted   INTEGER,
    creators_reached   INTEGER,
    dm_response_count  INTEGER,
    reply_rate         REAL,   -- RATIO
    skipped            INTEGER,
    total_creators     INTEGER,
    creators_remaining INTEGER,
    created_at         TEXT,
    completed_at       TEXT,
    created_via        TEXT,
    synced_at          TEXT
);
CREATE TABLE IF NOT EXISTS reacher_outreach_weekly (
    -- Shop-wide outreach aggregate from /automations/list `aggregate`. NOT the
    -- sum of the per-automation rows in reacher_automation -- those come from
    -- a stats view that ages out idle automations, so this aggregate is the
    -- authoritative shop-wide total.
    week_start                TEXT PRIMARY KEY,
    sample_request            INTEGER,  -- shop-wide sample requests in the window
    sample_approved           INTEGER,
    -- !! THE OPEN-vs-TARGET-COLLAB SPLIT LIVES HERE !! See docstring.
    accepted_tc_count         INTEGER,
    -- tc_invites counts invitation BATCHES; tc_invites_creator_count counts
    -- CREATORS. Pair accepted_tc_count with the CREATOR count, never with
    -- tc_invites / the metrics `tc_invites_sent` (also batches) -- see docstring.
    tc_invites                INTEGER,  -- batches
    tc_invites_creator_count  INTEGER,  -- creators invited
    creators_reached          INTEGER,
    creators_messaged         INTEGER,
    emails_sent               INTEGER,
    videos_posted             INTEGER,
    videos_converted          INTEGER,
    spark_codes               INTEGER,
    added_showcase_affiliates INTEGER,
    tc_showcase_creator_count INTEGER,
    tc_content_creator_count  INTEGER,
    -- DO NOT USE: computed by the API as the mean of daily rates, so it is not
    -- window-correct and can return values well outside 0-100%. Derive the
    -- rate yourself as accepted_tc_count / tc_invites_creator_count.
    tc_acceptance_rate_raw    REAL,
    synced_at                 TEXT
);
CREATE TABLE IF NOT EXISTS reacher_sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reacher_gmpd_product ON reacher_gmv_max_product_daily(product_id);
CREATE INDEX IF NOT EXISTS idx_reacher_gmpd_date    ON reacher_gmv_max_product_daily(date);
CREATE INDEX IF NOT EXISTS idx_reacher_cpw_product  ON reacher_creator_product_weekly(product_id);
CREATE INDEX IF NOT EXISTS idx_reacher_cpw_handle   ON reacher_creator_product_weekly(creator_handle);
CREATE INDEX IF NOT EXISTS idx_reacher_cw_handle  ON reacher_creator_weekly(creator_handle);
CREATE INDEX IF NOT EXISTS idx_reacher_spw_product ON reacher_sample_product_weekly(product_id);
CREATE INDEX IF NOT EXISTS idx_reacher_sr_product ON reacher_sample_request(product_id);
CREATE INDEX IF NOT EXISTS idx_reacher_sr_status  ON reacher_sample_request(status);
CREATE INDEX IF NOT EXISTS idx_reacher_gmd_date   ON reacher_gmv_max_daily(date);
CREATE INDEX IF NOT EXISTS idx_reacher_vc_creator ON reacher_video_creative(creator_handle);
CREATE INDEX IF NOT EXISTS idx_reacher_creator_lvl ON reacher_creator(creator_level);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def check_required_env() -> None:
    """Raise a clear SystemExit (not a KeyError deep in a request) when
    credentials are missing, so a misconfigured .env fails fast and legibly."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


# ---- read-only guard ------------------------------------------------------
# Reacher API keys are commonly provisioned with read_write scope even when a
# deployment only intends to read. Nothing in this connector needs write
# access, and an unattended scheduled job must never be able to reach the
# surfaces that scope unlocks: creating Target Collab invites sends REAL
# invites to creators, replying to creator messages sends REAL DMs, archiving
# an automation destroys real program config, and any payment/settlement
# endpoint can trigger a REAL money transfer. So the transport refuses
# anything that isn't a known read.
#
# This is belt-and-braces on top of using a read-only key if you can get one:
# if a write example is ever copied out of the Reacher docs into this file, it
# fails loudly here instead of silently messaging your entire creator roster.
_READ_METHODS = {"GET", "POST"}          # this API does reads over POST bodies
_WRITE_PATH_MARKERS = (
    "/target-collabs", "/reply", "/draft", "/start", "/stop", "/settle",
    "/deposit-intent", "/accept", "/reject", "/reactivate", "/remove",
    "/add-to-campaign", "/archive", "/request-more", "/spark-codes/sync",
    "/from-segment", "/retone", "/product-blocks", "/export",
    "/automations/dm", "/automations/email", "/automations/target-collab",
    "/automations/tc-cleanup", "/automations/sample-request",
    "/support-contact-default",
)


def _assert_read_only(method: str, path: str) -> None:
    if method.upper() not in _READ_METHODS:
        raise RuntimeError(
            f"reacher_sync is READ-ONLY: refusing {method} {path}. "
            "This connector must never use write scope even if the API key has it.")
    low = path.lower()
    for marker in _WRITE_PATH_MARKERS:
        if marker in low:
            raise RuntimeError(
                f"reacher_sync is READ-ONLY: refusing {method} {path} "
                f"(matched write-surface marker {marker!r}). If you genuinely need "
                "this, it does not belong in a nightly sync.")


DRY_RUN = False
_SESSION = requests.Session()
_call_times: deque[float] = deque()   # rolling hour, for the 3,000/hr ceiling
_last_call = 0.0


# ---- HTTP -----------------------------------------------------------------
def _throttle() -> None:
    """Respect BOTH documented limits without relying on catching 429s."""
    global _last_call
    now_t = time.monotonic()
    gap = now_t - _last_call
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    while _call_times and time.monotonic() - _call_times[0] > 3600:
        _call_times.popleft()
    if len(_call_times) >= HOURLY_CAP:
        wait = 3600 - (time.monotonic() - _call_times[0]) + 5
        print(f"  [rate] hourly cap reached ({HOURLY_CAP}); sleeping {wait/60:.1f} min")
        time.sleep(max(0, wait))
        while _call_times and time.monotonic() - _call_times[0] > 3600:
            _call_times.popleft()
    _last_call = time.monotonic()
    _call_times.append(_last_call)


def _request(method: str, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
    _assert_read_only(method, path)
    key = os.environ["REACHER_API_KEY"]
    shop = os.environ["REACHER_SHOP_ID"]
    headers = {
        "x-api-key": key,
        "x-shop-id": shop,
        "Accept": "application/json",
        # Do not remove: without an explicit UA, Cloudflare 1010 bans the
        # default one and every call 403s like a dead key.
        "User-Agent": USER_AGENT,
    }
    last_error = None
    for attempt in range(8):
        _throttle()
        try:
            resp = _SESSION.request(
                method, f"{BASE}{path}", json=body, params=params,
                headers=headers, timeout=180)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = f"{type(e).__name__}: {e}"
            time.sleep(min(60, 5 * (attempt + 1)))
            continue
        if resp.status_code == 429:
            last_error = "429 rate limit"
            time.sleep(float(resp.headers.get("Retry-After", 30)))
            continue
        if resp.status_code in (401, 403):
            hint = ""
            if "1010" in resp.text or "browser_signature" in resp.text:
                hint = (" — this is Cloudflare error 1010 (banned User-Agent), NOT a bad "
                        "key. Something stripped the User-Agent header.")
            raise RuntimeError(
                f"Reacher {resp.status_code} on {path}: {resp.text[:200]}{hint}")
        if resp.status_code == 404:
            # An unenabled tier (e.g. Social Intelligence) reads as absent.
            raise LookupError(f"Reacher 404 on {path}")
        if resp.status_code >= 500:
            last_error = f"{resp.status_code} server error"
            time.sleep(min(90, 10 * (attempt + 1)))
            continue
        if resp.status_code == 400:
            raise RuntimeError(f"Reacher 400 on {path}: {resp.text[:300]}")
        if resp.status_code != 200:
            raise RuntimeError(f"Reacher {path} {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise RuntimeError(f"Reacher {path} failed after retries. Last error: {last_error}")


def _post(path: str, body: dict) -> dict:
    return _request("POST", path, body=body)


def _get(path: str, params: dict | None = None) -> dict:
    return _request("GET", path, params=params)


def _paginate(path: str, body: dict, label: str):
    """Yield rows from a paginated POST endpoint, page_size maxed at 100."""
    page = 1
    while True:
        payload = _post(path, {**body, "page": page, "page_size": PAGE_SIZE})
        rows = payload.get("data") or []
        pg = payload.get("pagination") or {}
        total_pages = pg.get("total_pages") or 1
        if page == 1 and (pg.get("total_count") or 0):
            print(f"  {label}: {pg['total_count']:,} rows over {total_pages:,} pages")
        yield from rows
        if page >= total_pages or not rows:
            return
        page += 1


# ---- helpers --------------------------------------------------------------
def _num(v):
    return None if v is None else float(v)


def _int(v):
    return None if v is None else int(v)


def _jsonify(v):
    """JSON-encode lists/dicts; pass scalars through."""
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return str(v)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_starts(start: date, end: date) -> list[date]:
    w = monday(start)
    out = []
    while w <= end:
        out.append(w)
        w += timedelta(days=7)
    return out


def day_chunks(start: date, end: date, size: int):
    """Inclusive [start, end] split into <= `size`-day windows."""
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=size - 1))
        yield cur, stop
        cur = stop + timedelta(days=1)


def _load_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM reacher_sync_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _save_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    if DRY_RUN:
        return
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO reacher_sync_state (key, value, updated_at) VALUES (?,?,?)",
            (key, value, db.now()))


def _write(conn: sqlite3.Connection, sql: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    if DRY_RUN:
        print(f"  [dry-run] would write {len(rows):,} rows")
        return len(rows)
    with conn:
        conn.executemany(sql, rows)
    return len(rows)


def _is_bad_posted_date(pd: str | None) -> bool:
    """Epoch-zero and future-dated posted_date values are feed defects, not
    real dates -- see the docstring's VIDEO FEED DEFECTS section."""
    if not pd:
        return True
    d = pd[:10]
    return d.startswith("1970") or d > (date.today() + timedelta(days=1)).isoformat()


# ---- 1. daily affiliate-funnel metrics ------------------------------------
_METRICS_SQL = """INSERT OR REPLACE INTO reacher_metrics_daily
    (date, metric, value, synced_at) VALUES (?,?,?,?)"""


def sync_metrics(conn: sqlite3.Connection, days: int | None,
                 start_floor: str | None = None) -> int:
    stamp = db.now()
    end = date.today()
    start = (date.fromisoformat(start_floor) if start_floor
             else end - timedelta(days=days or INCREMENTAL_METRIC_DAYS))
    print(f"Reacher metrics: {len(ALL_METRICS)} metrics, {start} .. {end} (daily)")
    rows: list[tuple] = []
    for a, b in day_chunks(start, end, METRICS_CHUNK_DAYS):
        payload = _post("/metrics/timeseries", {
            "metrics": ALL_METRICS,
            "start_date": a.isoformat(),
            "end_date": b.isoformat(),
            "granularity": "day",
        })
        data = payload.get("data") or {}
        for metric, points in data.items():
            for p in points:
                rows.append((p["date"], metric, _num(p.get("value")), stamp))
        print(f"  {a} .. {b}: {sum(len(v) for v in data.values()):,} points")
    n = _write(conn, _METRICS_SQL, rows)
    print(f"Reacher metrics: {n:,} metric-days written.")
    return n


# ---- 2. Seller Center shop GMV (90d rolling, with affiliate/seller split) --
_SHOPGMV_SQL = """INSERT OR REPLACE INTO reacher_shop_gmv_daily
    (date, currency, gmv, orders, items_sold, customers, aov,
     video_gmv, video_affiliate_gmv, video_seller_gmv,
     live_gmv, live_affiliate_gmv, live_seller_gmv,
     product_card_gmv, product_card_shop_tab_gmv, product_card_search_gmv,
     product_impressions, product_clicks, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def sync_shop_gmv(conn: sqlite3.Connection) -> int:
    stamp = db.now()
    end = date.today() - timedelta(days=1)          # today's row is incomplete
    start = end - timedelta(days=WINDOW_CAP_DAYS - 1)
    print(f"Reacher shop GMV (Seller Center): {start} .. {end} (90d hard cap)")
    payload = _post("/shop-gmv/timeseries",
                    {"start_date": start.isoformat(), "end_date": end.isoformat()})
    cur = payload.get("currency_code")
    rows = []
    for p in payload.get("series") or []:
        ch = p.get("channels") or {}
        vid = ch.get("video") or {}
        liv = ch.get("live") or {}
        pc = ch.get("product_card") or {}
        tr = p.get("traffic") or {}
        rows.append((
            p.get("date"), cur, _num(p.get("gmv")), _int(p.get("orders")),
            _int(p.get("items_sold")), _int(p.get("customers")), _num(p.get("aov")),
            _num(vid.get("gmv")), _num(vid.get("affiliate")), _num(vid.get("seller")),
            _num(liv.get("gmv")), _num(liv.get("affiliate")), _num(liv.get("seller")),
            _num(pc.get("gmv")), _num(pc.get("shop_tab")), _num(pc.get("search")),
            _int(tr.get("product_impressions")), _int(tr.get("product_clicks")), stamp,
        ))
    n = _write(conn, _SHOPGMV_SQL, rows)
    print(f"Reacher shop GMV: {n:,} days written.")
    return n


# ---- 3. GMV Max ads (-> also ad_metrics platform='tiktok') ----------------
_GMVMAX_CAMP_SQL = """INSERT OR REPLACE INTO reacher_gmv_max_campaign
    (campaign_id, campaign_name, status, shopping_ads_type, budget, roas_bid,
     currency, last_synced_at, synced_at) VALUES (?,?,?,?,?,?,?,?,?)"""
_GMVMAX_DAILY_SQL = """INSERT OR REPLACE INTO reacher_gmv_max_daily
    (campaign_id, date, spend, impressions, clicks, orders, gross_revenue,
     cpc, cpm, ctr, roas, ad_roi, currency, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def sync_gmv_max(conn: sqlite3.Connection, days: int | None) -> int:
    """Campaign state + daily metrics. Mirrors spend into ad_metrics.

    90-day hard cap means history only accrues forward — a gap longer than 90
    days is permanently unrecoverable, which is the whole argument for running
    this daily.
    """
    stamp = db.now()
    shop = os.environ["REACHER_SHOP_ID"]
    end = date.today()
    # days is None on a plain --backfill (no --days): take the full 90-day cap.
    start = end - timedelta(days=min(days or WINDOW_CAP_DAYS, WINDOW_CAP_DAYS) - 1)

    camps = (_get("/gmv-max/campaigns").get("data") or [])
    print(f"Reacher GMV Max: {len(camps)} campaigns, metrics {start} .. {end}")
    crows = [(
        str(c.get("campaign_id")), c.get("campaign_name"), c.get("status"),
        c.get("shopping_ads_type"), _num(c.get("budget")), _num(c.get("roas_bid")),
        c.get("currency"), c.get("last_synced_at"), stamp,
    ) for c in camps if c.get("campaign_id")]
    _write(conn, _GMVMAX_CAMP_SQL, crows)

    drows: list[tuple] = []
    ad_rows: list[dict] = []
    spending = 0
    for c in camps:
        cid = str(c.get("campaign_id") or "")
        if not cid:
            continue
        payload = _get(f"/gmv-max/campaigns/{cid}/metrics",
                       {"start_date": start.isoformat(), "end_date": end.isoformat()})
        cur = payload.get("currency") or c.get("currency") or "USD"
        got = payload.get("data") or []
        camp_spend = 0.0
        for m in got:
            spend = _num(m.get("spend")) or 0.0
            camp_spend += spend
            drows.append((
                cid, m.get("date"), _num(m.get("spend")), _int(m.get("impressions")),
                _int(m.get("clicks")), _int(m.get("orders")), _num(m.get("gross_revenue")),
                _num(m.get("cpc")), _num(m.get("cpm")), _num(m.get("ctr")),
                _num(m.get("roas")), _num(m.get("ad_roi")), cur, stamp,
            ))
            # Only mirror days that actually transacted — most GMV Max
            # campaigns on a typical account are inactive, and would
            # otherwise pad ad_metrics with a large volume of zero rows.
            if spend or (_num(m.get("gross_revenue")) or 0):
                ad_rows.append({
                    "platform": "tiktok",
                    "account_id": shop,
                    "campaign_id": cid,
                    "campaign_name": c.get("campaign_name"),
                    "date": m.get("date"),
                    "impressions": _int(m.get("impressions")),
                    "clicks": _int(m.get("clicks")),
                    "spend": spend,
                    "conversions": _num(m.get("orders")),
                    "revenue": _num(m.get("gross_revenue")),
                    "currency": cur,
                    "campaign_type": c.get("shopping_ads_type"),
                })
        if camp_spend:
            spending += 1
            print(f"    {cid} {str(c.get('campaign_name'))[:44]:44} spend={camp_spend:,.0f}")

    n = _write(conn, _GMVMAX_DAILY_SQL, drows)
    if DRY_RUN:
        print(f"  [dry-run] would mirror {len(ad_rows):,} rows into ad_metrics")
    else:
        db.upsert_ad_metrics(ad_rows)
    print(f"Reacher GMV Max: {n:,} campaign-days ({spending} campaign(s) with spend), "
          f"{len(ad_rows):,} rows mirrored to ad_metrics as platform='tiktok'.")
    return n


# ---- 3b. PER-PRODUCT daily ad spend --------------------------------------
_GMVMAX_PROD_SQL = """INSERT OR REPLACE INTO reacher_gmv_max_product_daily
    (date, campaign_id, product_id, cost, gross_revenue, orders, roi, ad_roi,
     currency, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)"""

GMVMAX_PRODUCT_CHUNK_DAYS = 30   # keeps each response's JSON payload manageable


def sync_gmv_max_products(conn: sqlite3.Connection, days: int | None) -> int:
    """TikTok ad spend attributed PER PRODUCT PER DAY.

    This is the highest-value grain in the whole feed on most accounts: many
    ad platforms stop at campaign-level spend, so a real per-product ad cost
    lets you compute contribution margin per style/product, not just per
    channel.

    Same attribution caveat as the campaign grain: `cost` is real,
    `gross_revenue` is TikTok-attributed and must not be summed against
    affiliate GMV.
    """
    stamp = db.now()
    end = date.today()
    start = end - timedelta(days=min(days or WINDOW_CAP_DAYS, WINDOW_CAP_DAYS) - 1)
    print(f"Reacher GMV Max per-product: {start} .. {end}")
    total = 0
    for a, b in day_chunks(start, end, GMVMAX_PRODUCT_CHUNK_DAYS):
        payload = _post("/gmv-max/products/timeseries",
                        {"start_date": a.isoformat(), "end_date": b.isoformat()})
        cur = payload.get("currency") or "USD"
        rows = []
        for m in payload.get("data") or []:
            pid = m.get("product_id") or m.get("item_group_id")
            if not pid or not m.get("date"):
                continue
            rows.append((
                m.get("date"), str(m.get("campaign_id") or ""), str(pid),
                _num(m.get("cost")), _num(m.get("gross_revenue")), _int(m.get("orders")),
                _num(m.get("roi")), _num(m.get("ad_roi")), cur, stamp,
            ))
        total += _write(conn, _GMVMAX_PROD_SQL, rows)
        print(f"  {a} .. {b}: {len(rows):,} product-days")
    print(f"Reacher GMV Max per-product: {total:,} product-days written.")
    return total


# ---- 3c. creator x product video GMV -------------------------------------
_CREATOR_PROD_SQL = """INSERT OR REPLACE INTO reacher_creator_product_weekly
    (week_start, creator_handle, product_id, product_name, video_gmv, units_sold,
     video_count, synced_at) VALUES (?,?,?,?,?,?,?,?)"""
_CREATOR_VID_SQL = """INSERT OR REPLACE INTO reacher_creator_video_weekly
    (week_start, creator_handle, total_video_gmv, total_units_sold,
     qualifying_video_count, product_count, synced_at) VALUES (?,?,?,?,?,?,?)"""


def sync_creator_products(conn: sqlite3.Connection, weeks: list[date],
                          max_pages: int) -> int:
    """WHICH CREATOR SELLS WHICH PRODUCT, per week.

    `/videos/leaderboard` returns one row per creator with a nested
    `products[]` breakdown (video_gmv, units_sold, distinct video_count per
    product), so a single paginated pass gives both the creator total and the
    creator x product grain — a join most warehouses have no other path to,
    since basic video-metrics tables typically have video->product but no
    creator rollup, and a creator table typically has no product dimension at
    all.
    """
    stamp = db.now()
    print(f"Reacher creator x product: {len(weeks)} week(s)")
    total_pairs = 0
    for w in weeks:
        w_end = w + timedelta(days=6)
        prod_rows, cre_rows, n = [], [], 0
        for c in _paginate("/videos/leaderboard",
                           {"start_date": w.isoformat(), "end_date": w_end.isoformat(),
                            "sort_by": "total_video_gmv", "sort_dir": "desc"},
                           f"week {w}"):
            h = c.get("creator_handle")
            if not h:
                continue
            prods = c.get("products") or []
            cre_rows.append((
                w.isoformat(), h, _num(c.get("total_video_gmv")),
                _int(c.get("total_units_sold")), _int(c.get("qualifying_video_count")),
                len(prods), stamp,
            ))
            for p in prods:
                pid = p.get("product_id")
                if not pid:
                    continue
                prod_rows.append((
                    w.isoformat(), h, str(pid), p.get("product_name"),
                    _num(p.get("video_gmv")), _int(p.get("units_sold")),
                    _int(p.get("video_count")), stamp,
                ))
            n += 1
            if max_pages and n >= max_pages * PAGE_SIZE:
                break
        _write(conn, _CREATOR_VID_SQL, cre_rows)
        total_pairs += _write(conn, _CREATOR_PROD_SQL, prod_rows)
        print(f"  {w}: {n:,} creators, {len(prod_rows):,} creator-product pairs")
    print(f"Reacher creator x product: {total_pairs:,} pairs written.")
    return total_pairs


# ---- 4. creators: current-state snapshot ---------------------------------
_CREATOR_SQL = """INSERT OR REPLACE INTO reacher_creator
    (creator_handle, creator_id, follower_count, status, creator_level,
     shop_gmv, overall_gmv, shop_units_sold, shop_video_count, video_views,
     sample_received, commission_rate, est_commission, fulfillment_rate,
     overall_fulfillment_rate, tags, product_id, product_title,
     created_at, updated_at, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def sync_creators(conn: sqlite3.Connection, max_pages: int,
                  days: int | None) -> int:
    """Current-state creator snapshot.

    `days` windows the pull to recently-touched creators. Rows for creators
    outside the window stay in place at their last known state — normal
    snapshot-table behaviour, same pattern as any other current-state feed
    that has no delta/webhook mechanism.
    """
    stamp = db.now()
    body: dict = {"sort_by": "shop_gmv", "sort_dir": "desc"}
    if days:
        end = date.today()
        body["start_date"] = (end - timedelta(days=days)).isoformat()
        body["end_date"] = end.isoformat()
        print(f"Reacher creators: snapshot, touched since {body['start_date']}")
    else:
        print("Reacher creators: snapshot, FULL population")
    rows, seen = [], 0
    for c in _paginate("/creators/list", body, "creators"):
        h = c.get("creator_handle")
        if not h:
            continue
        rows.append((
            h, c.get("creator_id"), _int(c.get("follower_count")), c.get("status"),
            c.get("creator_level"), _num(c.get("shop_gmv")), _num(c.get("overall_gmv")),
            _int(c.get("shop_units_sold")), _int(c.get("shop_video_count")),
            _int(c.get("video_views")), _int(c.get("sample_received")),
            _num(c.get("commission_rate")), _num(c.get("est_commission")),
            _num(c.get("fulfillment_rate")), _num(c.get("overall_fulfillment_rate")),
            _jsonify(c.get("tags")), c.get("product_id"), c.get("product_title"),
            c.get("created_at"), c.get("updated_at"), stamp,
        ))
        seen += 1
        if len(rows) >= 5_000:
            _write(conn, _CREATOR_SQL, rows)
            rows = []
            print(f"  …{seen:,} creators")
        if max_pages and seen >= max_pages * PAGE_SIZE:
            break
    _write(conn, _CREATOR_SQL, rows)
    print(f"Reacher creators: {seen:,} creators written.")
    return seen


# ---- 5. creator x week performance --------------------------------------
_CREATOR_WEEK_SQL = """INSERT OR REPLACE INTO reacher_creator_weekly
    (week_start, creator_handle, creator_id, gmv, units_sold, order_count,
     est_commission, follower_count, synced_at) VALUES (?,?,?,?,?,?,?,?,?)"""


def sync_creator_weekly(conn: sqlite3.Connection, weeks: list[date],
                        max_pages: int, min_gmv: float) -> int:
    """Creator x Monday-week earnings.

    Filtered to earners by default (see DEFAULT_CREATOR_MIN_GMV) — do not
    count rows here to get active creators.
    """
    stamp = db.now()
    print(f"Reacher creator weekly: {len(weeks)} week(s) "
          f"{weeks[0] if weeks else '-'} .. {weeks[-1] if weeks else '-'}"
          + (f", min_gmv={min_gmv}" if min_gmv else ", ALL rows"))
    total = 0
    for w in weeks:
        w_end = w + timedelta(days=6)
        body = {"start_date": w.isoformat(), "end_date": w_end.isoformat(),
                "sort_by": "gmv", "sort_dir": "desc"}
        if min_gmv:
            body["min_gmv"] = min_gmv
        rows, n = [], 0
        for c in _paginate("/creators/performance", body, f"week {w}"):
            h = c.get("creator_handle")
            if not h:
                continue
            rows.append((
                w.isoformat(), h, c.get("creator_id"), _num(c.get("gmv")),
                _int(c.get("units_sold")), _int(c.get("order_count")),
                _num(c.get("est_commission")), _int(c.get("follower_count")), stamp,
            ))
            n += 1
            if max_pages and n >= max_pages * PAGE_SIZE:
                break
        total += _write(conn, _CREATOR_WEEK_SQL, rows)
        print(f"  {w}: {n:,} creators")
    print(f"Reacher creator weekly: {total:,} creator-weeks written.")
    return total


# ---- 6. sample requests --------------------------------------------------
_SAMPLE_SQL = """INSERT INTO reacher_sample_request
    (creator_handle, product_id, product_title, status, gmv, units_sold,
     sample_received, bio, categories, updated_at, first_seen_at, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(creator_handle, product_id) DO UPDATE SET
      product_title=excluded.product_title, status=excluded.status,
      gmv=excluded.gmv, units_sold=excluded.units_sold,
      sample_received=excluded.sample_received, bio=excluded.bio,
      categories=excluded.categories, updated_at=excluded.updated_at,
      synced_at=excluded.synced_at"""


def sync_samples(conn: sqlite3.Connection, max_pages: int,
                 days: int | None) -> int:
    """Sample rows. EMAIL IS DELIBERATELY NOT STORED (see docstring PII note).

    `days` windows the pull to a recent slice instead of the full history.
    """
    stamp = db.now()
    body: dict = {"sort_by": "updated_at", "sort_dir": "desc"}
    if days:
        end = date.today()
        body["start_date"] = (end - timedelta(days=days)).isoformat()
        body["end_date"] = end.isoformat()
        # NOTE the window is CREATED_AT, not updated_at (see the
        # reacher_sample_request_weekly DDL comment). So an incremental run
        # picks up newly-CREATED requests and will NOT re-read an old request
        # whose status later changed. Status churn tends to be slow (a
        # fulfilment-expiry status can land weeks later), which is why
        # DEFAULT_SAMPLE_DAYS is wide rather than a couple of weeks.
        print(f"Reacher samples: rows CREATED since {body['start_date']} "
              "(email dropped by design)")
    else:
        print("Reacher samples: FULL history (email dropped by design)")
    rows, seen = [], 0
    for s in _paginate("/samples/list", body, "samples"):
        h, pid = s.get("creator_handle"), s.get("product_id")
        if not h or not pid:
            continue
        rows.append((
            h, pid, s.get("product_title"), s.get("status"), _num(s.get("gmv")),
            _int(s.get("units_sold")), _int(s.get("sample_received")),
            s.get("bio"), _jsonify(s.get("categories")), s.get("updated_at"),
            stamp, stamp,
        ))
        seen += 1
        if len(rows) >= 5_000:
            _write(conn, _SAMPLE_SQL, rows)
            rows = []
            print(f"  …{seen:,} sample rows")
        if max_pages and seen >= max_pages * PAGE_SIZE:
            break
    _write(conn, _SAMPLE_SQL, rows)
    print(f"Reacher samples: {seen:,} sample rows written.")
    return seen


# ---- 5b. per-product weekly economics (commission + refunds) --------------
_PRODUCT_WEEK_SQL = """INSERT OR REPLACE INTO reacher_product_weekly
    (week_start, product_id, product_name, product_status, gmv, est_commission,
     units_sold, refund_units, video_count, sample_count, live_count,
     sc_total_gmv, sc_affiliate_gmv, sc_seller_video_gmv, sc_seller_live_gmv,
     sc_product_card_gmv, sc_shop_tab_gmv, sc_refunds, sc_orders, sc_units_sold,
     sc_customers, sc_impressions, sc_clicks, sc_add_to_cart, sc_aov, sc_ctr,
     sc_conversion, sc_gmv_per_1k_impr, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

PRODUCT_WEEK_PAGE_CAP = 40   # a generous ceiling; most weeks stop far earlier


def _product_transacted(p: dict) -> bool:
    """Did this product do ANYTHING in the window?

    Used to stop paging (rows are sorted gmv DESC, so once a page is entirely
    inert nothing below it matters) and to keep zero-activity padding rows out
    of the table. Deliberately generous: a product with no GMV but a sample
    request or a posted video is still real activity worth a row.

    Also a defense against a real defect this endpoint has been observed to
    have: on some weeks its reported `total_count` can be wildly larger than
    the account's actual catalog size, padded out with all-zero rows (usually
    with an empty product_name). Paging that honestly would mean walking
    thousands of pages for one week. Sorting by GMV descending and stopping at
    the first fully-inert page turns a pathological week into a couple of
    requests regardless of whether `total_count` can be trusted — which is
    the right thing to do on every week anyway, since an inert product row
    carries no information either way.
    """
    for f in ("gmv", "est_commission", "units_sold", "refund_units",
              "video_count", "sample_count", "live_count",
              "sc_total_gmv", "sc_orders", "sc_units_sold",
              "sc_impressions", "sc_clicks", "sc_add_to_cart"):
        v = p.get(f)
        if v:
            return True
    return False


def sync_product_weekly(conn: sqlite3.Connection, weeks: list[date],
                        max_pages: int) -> int:
    """Per-product weekly GMV, COMMISSION, refunds and Seller Center funnel.

    The commission and refund fields are the reason this grain exists — see
    the DDL comment. See `_product_transacted` for why paging stops early.
    """
    stamp = db.now()
    print(f"Reacher product weekly: {len(weeks)} week(s)")
    total = 0
    for w in weeks:
        w_end = w + timedelta(days=6)
        rows, n, page, empty_pages = [], 0, 1, 0
        cap = max_pages or PRODUCT_WEEK_PAGE_CAP
        while page <= cap:
            payload = _post("/products/list",
                            {"start_date": w.isoformat(), "end_date": w_end.isoformat(),
                             "sort_by": "gmv", "sort_dir": "desc",
                             "page": page, "page_size": PAGE_SIZE})
            batch = payload.get("data") or []
            pg = payload.get("pagination") or {}
            if not batch:
                break
            live = [p for p in batch if _product_transacted(p)]
            if not live:
                empty_pages += 1
                if empty_pages >= 1:      # desc sort ⇒ nothing below this matters
                    break
            for p in live:
                pid = p.get("product_id")
                if not pid:
                    continue
                rows.append((
                    w.isoformat(), str(pid), p.get("product_name"),
                    p.get("product_status"),
                    _num(p.get("gmv")), _num(p.get("est_commission")),
                    _int(p.get("units_sold")), _int(p.get("refund_units")),
                    _int(p.get("video_count")), _int(p.get("sample_count")),
                    _int(p.get("live_count")),
                    _num(p.get("sc_total_gmv")), _num(p.get("sc_affiliate_gmv")),
                    _num(p.get("sc_seller_video_gmv")), _num(p.get("sc_seller_live_gmv")),
                    _num(p.get("sc_product_card_gmv")), _num(p.get("sc_shop_tab_gmv")),
                    _num(p.get("sc_refunds")), _num(p.get("sc_orders")),
                    _num(p.get("sc_units_sold")), _num(p.get("sc_customers")),
                    _num(p.get("sc_impressions")), _num(p.get("sc_clicks")),
                    _num(p.get("sc_add_to_cart")), _num(p.get("sc_aov")),
                    _num(p.get("sc_ctr")), _num(p.get("sc_conversion")),
                    _num(p.get("sc_gmv_per_1k_impr")), stamp,
                ))
                n += 1
            if len(rows) >= 5_000:
                total += _write(conn, _PRODUCT_WEEK_SQL, rows)
                rows = []
            if page >= (pg.get("total_pages") or 1):
                break
            page += 1
        total += _write(conn, _PRODUCT_WEEK_SQL, rows)
        print(f"  {w}: {n:,} products that transacted ({page} page(s))")
    print(f"Reacher product weekly: {total:,} product-weeks written.")
    return total


# ---- 6b. DATED sample requests (creator x product x created-week) --------
_SAMPLE_WEEK_SQL = """INSERT OR REPLACE INTO reacher_sample_request_weekly
    (week_start, creator_handle, product_id, product_title, status, gmv,
     units_sold, sample_received, synced_at) VALUES (?,?,?,?,?,?,?,?,?)"""


def sync_sample_requests_weekly(conn: sqlite3.Connection, weeks: list[date],
                                max_pages: int) -> int:
    """Sample requests dated by the week they were CREATED.

    The request date is recovered from the query window, because no field in
    the row carries it (see the DDL comment).
    """
    stamp = db.now()
    print(f"Reacher dated sample requests: {len(weeks)} week(s)")
    total = 0
    for w in weeks:
        w_end = w + timedelta(days=6)
        rows, n = [], 0
        for s in _paginate("/samples/list",
                           {"start_date": w.isoformat(), "end_date": w_end.isoformat(),
                            "sort_by": "created_at", "sort_dir": "asc"},
                           f"week {w}"):
            h, pid = s.get("creator_handle"), s.get("product_id")
            if not h or not pid:
                continue
            rows.append((
                w.isoformat(), h, str(pid), s.get("product_title"), s.get("status"),
                _num(s.get("gmv")), _int(s.get("units_sold")),
                _int(s.get("sample_received")), stamp,
            ))
            n += 1
            if len(rows) >= 5_000:
                total += _write(conn, _SAMPLE_WEEK_SQL, rows)
                rows = []
            if max_pages and n >= max_pages * PAGE_SIZE:
                break
        total += _write(conn, _SAMPLE_WEEK_SQL, rows)
        print(f"  {w}: {n:,} requests")
    print(f"Reacher dated sample requests: {total:,} rows written.")
    return total


# ---- 6c. automation -> product (for attributing request spikes to TC) ----
_AUTO_PROD_SQL = """INSERT OR REPLACE INTO reacher_automation_product
    (automation_id, product_id, product_name, commission_rate,
     shop_ads_commission_rate, automation_name, automation_type,
     automation_status, created_at, completed_at, valid_until,
     offer_free_samples, auto_approve_samples, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def sync_automation_products(conn: sqlite3.Connection) -> int:
    """The product list attached to every Target Collab / TC-cleanup automation.

    Requires one GET /automations/{id} per automation (the list response does
    not carry products), so this costs roughly one call per automation.
    """
    stamp = db.now()
    autos = list(_paginate("/automations/list",
                           {"sort_by": "created_at", "sort_dir": "desc"}, "automations"))
    print(f"Reacher automation products: inspecting {len(autos)} automation(s)")
    rows = []
    for a in autos:
        aid = a.get("automation_id")
        if aid is None:
            continue
        atype = a.get("automation_type") or ""
        # Only TC-shaped automations carry a product list; skip the rest to save calls.
        if "Target Collab" not in atype and "TC" not in atype:
            continue
        try:
            det = _get(f"/automations/{aid}")
        except (RuntimeError, LookupError) as e:  # noqa: BLE001
            print(f"    automation {aid}: detail unavailable ({e})")
            continue
        tc = ((det.get("details") or {}).get("target_collab") or {})
        for p in (tc.get("products") or []):
            pid = p.get("product_id")
            if not pid:
                continue
            rows.append((
                int(aid), str(pid), p.get("product_name"), _num(p.get("commission_rate")),
                _num(p.get("shop_ads_commission_rate")), a.get("automation_name"),
                atype, a.get("status"), a.get("created_at"), a.get("completed_at"),
                tc.get("valid_until"),
                1 if tc.get("offer_free_samples") else 0,
                1 if tc.get("auto_approve_samples") else 0,
                stamp,
            ))
    n = _write(conn, _AUTO_PROD_SQL, rows)
    print(f"Reacher automation products: {n:,} automation-product rows written.")
    return n


# ---- 7. sample ROI by product x week ------------------------------------
_SAMPLE_PROD_SQL = """INSERT OR REPLACE INTO reacher_sample_product_weekly
    (week_start, product_id, product_name, total_requests, approved,
     sample_gmv, videos_from_samples, synced_at) VALUES (?,?,?,?,?,?,?,?)"""


def sync_sample_products(conn: sqlite3.Connection, weeks: list[date],
                         max_pages: int) -> int:
    stamp = db.now()
    print(f"Reacher sample ROI by product: {len(weeks)} week(s)")
    total = 0
    for w in weeks:
        w_end = w + timedelta(days=6)
        rows, n = [], 0
        for p in _paginate("/samples/by-product",
                           {"start_date": w.isoformat(), "end_date": w_end.isoformat(),
                            "sort_by": "sample_gmv", "sort_dir": "desc"},
                           f"week {w}"):
            pid = p.get("product_id")
            if not pid:
                continue
            rows.append((
                w.isoformat(), pid, p.get("product_name"), _int(p.get("total_requests")),
                _int(p.get("approved")), _num(p.get("sample_gmv")),
                _int(p.get("videos_from_samples")), stamp,
            ))
            n += 1
            if max_pages and n >= max_pages * PAGE_SIZE:
                break
        total += _write(conn, _SAMPLE_PROD_SQL, rows)
        print(f"  {w}: {n:,} products")
    print(f"Reacher sample ROI: {total:,} product-weeks written.")
    return total


# ---- 8. video creative breakdown ----------------------------------------
_VIDEO_CREATIVE_SQL = """INSERT OR REPLACE INTO reacher_video_creative
    (week_start, video_id, rank, title, creator_handle, tiktok_url, posted_date,
     video_gmv, views, like_count, comment_count, order_count, analyzed,
     hook_text, hook_classification, hook_reasoning, sell_points, product_niche,
     shot_style, videography_locations, videography_lighting,
     videography_product_showcase, videography_notes, missing_fields, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def sync_video_creative(conn: sqlite3.Connection, weeks: list[date]) -> int:
    """Top-N videos per week + the AI creative breakdown.

    This endpoint is genuinely slow (tens of seconds per call regardless of
    how few rows you ask for), so it's the one grain most worth skipping on a
    hurried run (`--skip video_creative`).
    """
    stamp = db.now()
    print(f"Reacher video creative: top {CREATIVE_LIMIT}/week over {len(weeks)} week(s) "
          "(slow endpoint)")
    total = 0
    for w in weeks:
        w_end = w + timedelta(days=6)
        payload = _post("/videos/creative", {
            "limit": CREATIVE_LIMIT, "sort_by": "video_gmv", "sort_dir": "desc",
            "start_date": w.isoformat(), "end_date": w_end.isoformat(),
        })
        rows, skipped = [], 0
        for v in payload.get("data") or []:
            vid = v.get("video_id")
            if not vid:
                continue
            pd = v.get("posted_date")
            if _is_bad_posted_date(pd):
                pd = None          # 1970 epoch / future-dated: store NULL, not a lie
                skipped += 1
            cr = v.get("creative") or {}
            hook = cr.get("hook") or {}
            vg = cr.get("videography") or {}
            rows.append((
                w.isoformat(), vid, _int(v.get("rank")), v.get("title"),
                v.get("creator_handle"), v.get("tiktok_url"), pd,
                _num(v.get("video_gmv")), _int(v.get("views")), _int(v.get("like_count")),
                _int(v.get("comment_count")), _int(v.get("order_count")),
                1 if cr.get("analyzed") else 0,
                hook.get("text"), hook.get("classification"), hook.get("reasoning"),
                _jsonify(cr.get("sell_points")), cr.get("product_niche"),
                _jsonify(cr.get("shot_style")), _jsonify(vg.get("locations")),
                _jsonify(vg.get("lighting")), _jsonify(vg.get("product_showcase")),
                vg.get("notes"), _jsonify(cr.get("missing_fields")), stamp,
            ))
        cov = payload.get("coverage") or {}
        total += _write(conn, _VIDEO_CREATIVE_SQL, rows)
        print(f"  {w}: {len(rows)} videos, {cov.get('with_creative_analysis', '?')} analyzed"
              + (f", {skipped} bad posted_date nulled" if skipped else ""))
    print(f"Reacher video creative: {total:,} video-weeks written.")
    return total


# ---- 9. shop performance score ------------------------------------------
_HEALTH_SQL = """INSERT OR REPLACE INTO reacher_shop_health_daily
    (date, sps_score, sps_tier, peer_percentile, product_satisfaction,
     fulfillment_logistics, customer_service, dimensions_json, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?)"""

# Dimension names as returned by the API. Matched case-insensitively on a
# prefix so a rename ("Customer service score") still lands; the raw JSON is
# kept too in case the dimension set changes shape.
_DIM_MAP = {
    "product satisfaction": "product_satisfaction",
    "fulfillment": "fulfillment_logistics",
    "customer service": "customer_service",
}


def sync_shop_health(conn: sqlite3.Connection) -> int:
    stamp = db.now()
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=WINDOW_CAP_DAYS - 1)
    print(f"Reacher shop health (SPS): {start} .. {end}")
    payload = _post("/shop-health/timeseries",
                    {"start_date": start.isoformat(), "end_date": end.isoformat()})
    rows = []
    for p in payload.get("series") or []:
        dims = p.get("dimensions") or []
        flat = {v: None for v in _DIM_MAP.values()}
        for d in dims:
            name = (d.get("name") or "").lower()
            for prefix, col in _DIM_MAP.items():
                if name.startswith(prefix):
                    flat[col] = _num(d.get("score"))
                    break
        rows.append((
            p.get("date"), _num(p.get("sps_score")), p.get("sps_tier"),
            _num(p.get("peer_percentile")), flat["product_satisfaction"],
            flat["fulfillment_logistics"], flat["customer_service"],
            json.dumps(dims), stamp,
        ))
    n = _write(conn, _HEALTH_SQL, rows)
    print(f"Reacher shop health: {n:,} days written.")
    return n


# ---- 10. outreach automations -------------------------------------------
_AUTOMATION_SQL = """INSERT OR REPLACE INTO reacher_automation
    (automation_id, automation_name, automation_type, status, status_message,
     gmv, sample_requests, accepted_requests, videos_posted, videos_converted,
     creators_reached, dm_response_count, reply_rate, skipped, total_creators,
     creators_remaining, created_at, completed_at, created_via, synced_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def sync_automations(conn: sqlite3.Connection) -> int:
    stamp = db.now()
    print("Reacher automations: outreach program state")
    rows = []
    for a in _paginate("/automations/list", {"sort_by": "created_at", "sort_dir": "desc"},
                       "automations"):
        aid = a.get("automation_id")
        if aid is None:
            continue
        rows.append((
            int(aid), a.get("automation_name"), a.get("automation_type"),
            a.get("status"), a.get("status_message"), _num(a.get("gmv")),
            _int(a.get("sample_requests")), _int(a.get("accepted_requests")),
            _int(a.get("videos_posted")), _int(a.get("videos_converted")),
            _int(a.get("creators_reached")), _int(a.get("dm_response_count")),
            _num(a.get("reply_rate")), _int(a.get("skipped")),
            _int(a.get("total_creators")), _int(a.get("creators_remaining")),
            a.get("created_at"), a.get("completed_at"), a.get("created_via"), stamp,
        ))
    n = _write(conn, _AUTOMATION_SQL, rows)
    print(f"Reacher automations: {n:,} automations written.")
    return n


# ---- 10b. shop-wide outreach aggregate (the open-vs-TC split) -----------
_OUTREACH_SQL = """INSERT OR REPLACE INTO reacher_outreach_weekly
    (week_start, sample_request, sample_approved, accepted_tc_count, tc_invites,
     tc_invites_creator_count, creators_reached, creators_messaged, emails_sent,
     videos_posted, videos_converted, spark_codes, added_showcase_affiliates,
     tc_showcase_creator_count, tc_content_creator_count, tc_acceptance_rate_raw,
     synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def sync_outreach_weekly(conn: sqlite3.Connection, weeks: list[date]) -> int:
    """Shop-wide outreach aggregate per week.

    This is the only place the OPEN-vs-TARGET-COLLAB sample split is
    available: `accepted_tc_count` is the TC-sourced slice of
    `sample_request` (a TC offering free samples makes an acceptance BE a
    sample request), so open-collab is the residual. See the DDL comment for
    the unit trap on `tc_invites`.

    One request per week; the aggregate only populates when a date filter is
    supplied, which is why this can't be folded into `sync_automations`.
    """
    stamp = db.now()
    print(f"Reacher outreach aggregate: {len(weeks)} week(s)")
    rows = []
    for w in weeks:
        w_end = w + timedelta(days=6)
        payload = _post("/automations/list",
                        {"page": 1, "page_size": 1,
                         "start_date": w.isoformat(), "end_date": w_end.isoformat()})
        a = payload.get("aggregate") or {}
        if not a:
            continue
        rows.append((
            w.isoformat(), _int(a.get("sample_request")), _int(a.get("sample_approved")),
            _int(a.get("accepted_tc_count")), _int(a.get("tc_invites")),
            _int(a.get("tc_invites_creator_count")), _int(a.get("creators_reached")),
            _int(a.get("creators_messaged")), _int(a.get("emails_sent")),
            _int(a.get("videos_posted")), _int(a.get("videos_converted")),
            _int(a.get("spark_codes")), _int(a.get("added_showcase_affiliates")),
            _int(a.get("tc_showcase_creator_count")), _int(a.get("tc_content_creator_count")),
            _num(a.get("tc_acceptance_rate")), stamp,
        ))
    n = _write(conn, _OUTREACH_SQL, rows)
    print(f"Reacher outreach aggregate: {n:,} weeks written.")
    return n


# ---- main ---------------------------------------------------------------
GRAINS = ("metrics", "shop_gmv", "gmv_max", "gmv_max_products", "creators",
          "creator_weekly", "creator_products", "product_weekly",
          "samples", "sample_requests_weekly",
          "automation_products", "sample_products",
          "video_creative", "shop_health", "automations", "outreach_weekly")


def main() -> None:
    global DRY_RUN
    p = argparse.ArgumentParser(
        description="Sync Reacher (TikTok Shop affiliate/creator platform) into the warehouse.")
    p.add_argument("--backfill", action="store_true",
                   help="deepest available history for every grain")
    p.add_argument("--days", type=int,
                   help="override the metrics window in days")
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                   help=f"weeks of creator/sample/creative history (default {DEFAULT_WEEKS})")
    p.add_argument("--backfill-start", default=None,
                   help="floor (YYYY-MM-DD) for weekly grains on --backfill "
                        f"(default {DEFAULT_BACKFILL_YEARS} years back; Reacher "
                        "returns empty windows before your real data floor, so "
                        "there's no need to know it exactly)")
    p.add_argument("--creator-min-gmv", type=float, default=DEFAULT_CREATOR_MIN_GMV,
                   help="min GMV for a creator-week row (default %(default)s; "
                        "0 keeps every row, including creators who earned nothing)")
    p.add_argument("--sample-days", type=int, default=DEFAULT_SAMPLE_DAYS,
                   help="sample rows updated in the last N days "
                        "(default %(default)s; ignored with --backfill)")
    p.add_argument("--creator-days", type=int, default=DEFAULT_CREATOR_SNAPSHOT_DAYS,
                   help="refresh creator snapshot for creators touched in the last "
                        "N days (default %(default)s; ignored with --backfill)")
    p.add_argument("--only", choices=GRAINS, action="append",
                   help="run only these grains (repeatable)")
    p.add_argument("--skip", choices=GRAINS, action="append",
                   help="skip these grains (repeatable)")
    p.add_argument("--pages", type=int, default=0,
                   help="cap pages per paginated grain (0 = no cap)")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch and report, write nothing")
    args = p.parse_args()

    if not os.environ.get("REACHER_API_KEY"):
        print("REACHER_API_KEY not set — skipping Reacher sync.")
        return
    check_required_env()

    DRY_RUN = args.dry_run
    if DRY_RUN:
        print("*** DRY RUN — no writes ***")

    db.init_db()
    conn = db.connect()
    conn.executescript(DDL)   # in dry-run mode, tables only -- rows still suppressed by _write

    wanted = set(args.only) if args.only else set(GRAINS)
    wanted -= set(args.skip or ())

    metric_days = args.days
    backfill_floor = (date.fromisoformat(args.backfill_start) if args.backfill_start
                      else date.today() - timedelta(days=365 * DEFAULT_BACKFILL_YEARS))
    metric_floor = backfill_floor.isoformat() if (args.backfill and not args.days) else None
    if args.backfill:
        weeks = week_starts(backfill_floor, date.today())
    else:
        weeks = week_starts(date.today() - timedelta(weeks=args.weeks - 1), date.today())

    # --backfill drops the incremental windows and takes the whole population.
    sample_days = None if args.backfill else args.sample_days
    creator_days = None if args.backfill else args.creator_days

    jobs = [
        ("metrics",         "reacher_metrics",        lambda: sync_metrics(conn, metric_days, metric_floor)),
        ("shop_gmv",        "reacher_shop_gmv",       lambda: sync_shop_gmv(conn)),
        ("gmv_max",         "reacher_gmv_max",        lambda: sync_gmv_max(conn, metric_days)),
        ("gmv_max_products", "reacher_gmv_max_products", lambda: sync_gmv_max_products(conn, metric_days or WINDOW_CAP_DAYS)),
        ("creators",        "reacher_creators",       lambda: sync_creators(conn, args.pages, creator_days)),
        ("creator_weekly",  "reacher_creator_weekly", lambda: sync_creator_weekly(conn, weeks, args.pages, args.creator_min_gmv)),
        ("creator_products", "reacher_creator_products", lambda: sync_creator_products(conn, weeks, args.pages)),
        ("product_weekly",  "reacher_product_weekly", lambda: sync_product_weekly(conn, weeks, args.pages)),
        ("samples",         "reacher_samples",        lambda: sync_samples(conn, args.pages, sample_days)),
        ("sample_requests_weekly", "reacher_sample_requests_weekly", lambda: sync_sample_requests_weekly(conn, weeks, args.pages)),
        ("automation_products", "reacher_automation_products", lambda: sync_automation_products(conn)),
        ("sample_products", "reacher_sample_products", lambda: sync_sample_products(conn, weeks, args.pages)),
        ("video_creative",  "reacher_video_creative", lambda: sync_video_creative(conn, weeks)),
        ("shop_health",     "reacher_shop_health",    lambda: sync_shop_health(conn)),
        ("automations",     "reacher_automations",    lambda: sync_automations(conn)),
        ("outreach_weekly", "reacher_outreach_weekly", lambda: sync_outreach_weekly(conn, weeks)),
    ]

    failures: list[str] = []
    for grain, platform, fn in jobs:
        if grain not in wanted:
            continue
        started = db.now()
        try:
            n = fn()
            if not DRY_RUN:
                db.log_sync(platform, started, n, "ok")
        except Exception as e:  # noqa: BLE001
            if not DRY_RUN:
                db.log_sync(platform, started, 0, "error", str(e))
            print(f"Reacher {grain} ERROR: {e}")
            failures.append(grain)
        print()

    conn.close()
    if failures:
        raise SystemExit("Reacher sync failures: " + ", ".join(failures))


if __name__ == "__main__":
    main()
