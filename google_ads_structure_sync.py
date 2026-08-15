r"""
Google Ads STRUCTURE + SETTINGS -> warehouse (current-state snapshots).

Everything captured here is a campaign/asset ATTRIBUTE rather than a daily
metric, so neither a campaign-level daily sync nor the date-keyed detail
grains in google_ads_detail_sync.py have anywhere to put it. Without this,
a warehouse can see that a campaign performed badly but not WHY it was
configured that way — and specifically can't answer "is this campaign funded
and eligible to serve, or is it quietly misconfigured?", which is often the
real question behind a sudden spend or impression drop.

WHY THIS EXISTS. A Performance Max campaign can look completely healthy on
every metric you'd normally check — budget available, no impression-share
loss reported, asset group ELIGIBLE — and still deliver almost nothing,
because the actual cause is pure configuration: a listing-group filter that
now matches an empty (or near-empty) product segment, a campaign stuck in a
LEARNING bidding state months after it should have exited, or a stale
end-date-adjacent status. None of those are visible from spend/impressions
alone; you have to pull campaign, asset group, and listing-group state
directly from the API. This connector makes that a single query instead of a
manual investigation each time.

FIVE TABLES, all snapshot_date-keyed (current state as of when the sync ran,
not a metric for that day):
  google_campaigns          — status/primary_status + REASONS, bid strategy
                              and its target (tROAS / tCPA), budget + delivery
  google_asset_groups       — PMax asset groups: primary_status + REASONS,
                              ad_strength
  google_asset_group_assets — asset COUNTS per asset_group x field_type x
                              status. Deliberately aggregated: PMax enforces
                              hard per-type minimums (a business name, a 1:1
                              logo, at least one landscape and one square
                              image, several headlines and descriptions), so
                              "how many ENABLED assets of each required type"
                              is the question that matters — storing every
                              individual asset daily would be a lot of rows
                              for no additional insight.
  google_asset_group_listing_filters — the listing-group tree (SUBDIVISION /
                              UNIT_INCLUDED / UNIT_EXCLUDED) with whichever
                              case dimension each node splits on. This is
                              what tells you a PMax campaign's product
                              targeting points at an empty or shrinking
                              segment.
  google_conversion_actions — every conversion action + primary_for_goal, so
                              a change to what the Conversions column counts
                              is DETECTABLE instead of silently repricing
                              every ROAS number downstream with no error
                              anywhere.

CONVERSION-ACTION HEALTH — READ THIS BEFORE TRUSTING ROAS. Which actions
actually feed the account's `conversions` metric is an ACCOUNT-LEVEL GOAL
setting, not something you can read off primary_for_goal alone: an action can
be ENABLED and primary_for_goal=true and still contribute nothing in
practice, and the reverse is just as possible. It's common for several
non-purchase actions (page views, click-to-call, store visits, engagement
pings) to sit alongside a real purchase action all marked primary_for_goal.
Cross-check what actually shows up as `conversions` on a campaign-day against
this table's action list before assuming ROAS reflects purchases specifically
— and if you see all_conversions running far above conversions on the same
campaign-day, that's almost always page-view-style micro-actions inflating
all_conversions, which is why this repo treats all_conversions as diagnostic
only, never as a business metric.

API NOTES (Google Ads API v24-era, `google-ads` Python library):
  - `campaign.start_date` / `campaign.end_date` do not exist as queryable
    fields in current API versions ("Unrecognized field" if you try). Don't
    reintroduce them from an older code sample.
  - asset_group / asset_group_asset / asset_group_listing_group_filter all
    read fine with the standard reporting scopes — no extra access needed
    beyond what the campaign-level connector already uses.
  - primary_status_reasons is a REPEATED enum; stored as a comma-joined
    string because it's meant to be read by a human, not joined on.
  - The bid target lives on a DIFFERENT field per strategy
    (maximize_conversion_value.target_roas vs target_roas.target_roas vs
    target_cpa.target_cpa_micros vs maximize_conversions.target_cpa_micros),
    so all four are selected and coalesced into a single target_roas /
    target_cpa pair.

USAGE:
  python google_ads_structure_sync.py
  python google_ads_structure_sync.py --only campaigns
  python google_ads_structure_sync.py --date 2026-08-05
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

from warehouse import db
from warehouse.connectors import google_ads

GRAINS = ("campaigns", "asset_groups", "assets", "listing_filters", "conversion_actions")

DDL = """
CREATE TABLE IF NOT EXISTS google_campaigns (
    snapshot_date       TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    campaign_id         TEXT NOT NULL,
    campaign_name       TEXT,
    status              TEXT,
    serving_status      TEXT,
    primary_status      TEXT,
    primary_status_reasons TEXT,      -- comma-joined enum names
    channel_type        TEXT,
    channel_sub_type    TEXT,
    bidding_strategy    TEXT,
    target_roas         REAL,         -- ratio (2.5 = 250%), NULL if not tROAS
    target_cpa          REAL,         -- currency units, NULL if not tCPA
    budget_amount       REAL,         -- daily budget, currency units
    budget_delivery     TEXT,
    budget_shared       INTEGER,
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, account_id, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_gcamp_date ON google_campaigns(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_gcamp_id ON google_campaigns(campaign_id);

CREATE TABLE IF NOT EXISTS google_asset_groups (
    snapshot_date       TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    campaign_id         TEXT NOT NULL,
    asset_group_id      TEXT NOT NULL,
    asset_group_name    TEXT,
    status              TEXT,
    primary_status      TEXT,
    primary_status_reasons TEXT,
    ad_strength         TEXT,
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, account_id, asset_group_id)
);
CREATE INDEX IF NOT EXISTS idx_gag_date ON google_asset_groups(snapshot_date);

-- Aggregated on purpose: the question is always "does this asset group have
-- enough ENABLED assets of each required type", never "which asset id".
CREATE TABLE IF NOT EXISTS google_asset_group_assets (
    snapshot_date       TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    campaign_id         TEXT NOT NULL,
    asset_group_id      TEXT NOT NULL,
    field_type          TEXT NOT NULL,
    status              TEXT NOT NULL,
    n_assets            INTEGER NOT NULL,
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, account_id, asset_group_id, field_type, status)
);
CREATE INDEX IF NOT EXISTS idx_gaga_date ON google_asset_group_assets(snapshot_date);

CREATE TABLE IF NOT EXISTS google_asset_group_listing_filters (
    snapshot_date       TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    campaign_id         TEXT NOT NULL,
    asset_group_id      TEXT NOT NULL,
    filter_id           TEXT NOT NULL,
    parent_filter_id    TEXT,
    filter_type         TEXT,          -- SUBDIVISION | UNIT_INCLUDED | UNIT_EXCLUDED
    case_dimension      TEXT,          -- which dimension this node splits on
    case_value          TEXT,          -- the value it matches (EXACT match in Google Ads)
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, account_id, asset_group_id, filter_id)
);
CREATE INDEX IF NOT EXISTS idx_gaglf_date ON google_asset_group_listing_filters(snapshot_date);

CREATE TABLE IF NOT EXISTS google_conversion_actions (
    snapshot_date       TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    conversion_action_id TEXT NOT NULL,
    name                TEXT,
    status              TEXT,
    category            TEXT,
    action_type         TEXT,
    primary_for_goal    INTEGER,
    attribution_model   TEXT,
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, account_id, conversion_action_id)
);
CREATE INDEX IF NOT EXISTS idx_gca_date ON google_conversion_actions(snapshot_date);
"""

INSERTS = {
    "campaigns": """INSERT OR REPLACE INTO google_campaigns
        (snapshot_date, account_id, campaign_id, campaign_name, status, serving_status,
         primary_status, primary_status_reasons, channel_type, channel_sub_type,
         bidding_strategy, target_roas, target_cpa, budget_amount, budget_delivery,
         budget_shared, synced_at)
        VALUES (:snapshot_date,:account_id,:campaign_id,:campaign_name,:status,:serving_status,
         :primary_status,:primary_status_reasons,:channel_type,:channel_sub_type,
         :bidding_strategy,:target_roas,:target_cpa,:budget_amount,:budget_delivery,
         :budget_shared,:synced_at)""",
    "asset_groups": """INSERT OR REPLACE INTO google_asset_groups
        (snapshot_date, account_id, campaign_id, asset_group_id, asset_group_name,
         status, primary_status, primary_status_reasons, ad_strength, synced_at)
        VALUES (:snapshot_date,:account_id,:campaign_id,:asset_group_id,:asset_group_name,
         :status,:primary_status,:primary_status_reasons,:ad_strength,:synced_at)""",
    "assets": """INSERT OR REPLACE INTO google_asset_group_assets
        (snapshot_date, account_id, campaign_id, asset_group_id, field_type, status,
         n_assets, synced_at)
        VALUES (:snapshot_date,:account_id,:campaign_id,:asset_group_id,:field_type,:status,
         :n_assets,:synced_at)""",
    "listing_filters": """INSERT OR REPLACE INTO google_asset_group_listing_filters
        (snapshot_date, account_id, campaign_id, asset_group_id, filter_id,
         parent_filter_id, filter_type, case_dimension, case_value, synced_at)
        VALUES (:snapshot_date,:account_id,:campaign_id,:asset_group_id,:filter_id,
         :parent_filter_id,:filter_type,:case_dimension,:case_value,:synced_at)""",
    "conversion_actions": """INSERT OR REPLACE INTO google_conversion_actions
        (snapshot_date, account_id, conversion_action_id, name, status, category,
         action_type, primary_for_goal, attribution_model, synced_at)
        VALUES (:snapshot_date,:account_id,:conversion_action_id,:name,:status,:category,
         :action_type,:primary_for_goal,:attribution_model,:synced_at)""",
}

FETCH = {
    "campaigns": google_ads.fetch_campaign_settings,
    "asset_groups": google_ads.fetch_asset_groups,
    "assets": google_ads.fetch_asset_group_assets,
    "listing_filters": google_ads.fetch_asset_group_listing_filters,
    "conversion_actions": google_ads.fetch_conversion_actions,
}


def ensure_schema(conn) -> None:
    """Create this connector's tables if they don't exist yet. Safe to call
    every run — matches the CREATE TABLE IF NOT EXISTS pattern used across
    the rest of the warehouse."""
    conn.executescript(DDL)


def run(snapshot: str, only: list[str] | None = None) -> tuple[int, list[str]]:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db.connect()
    ensure_schema(conn)
    total, failures = 0, []
    try:
        for grain in GRAINS:
            if only and grain not in only:
                continue
            try:
                rows = FETCH[grain]()
            except Exception as e:  # noqa: BLE001 — one grain must not kill the rest
                failures.append(f"{grain}: {str(e)[:150]}")
                print(f"    {grain} FAILED: {str(e)[:150]}")
                continue
            with conn:  # commit per grain — never hold the write lock long
                conn.executemany(INSERTS[grain],
                                 [{**r, "snapshot_date": snapshot, "synced_at": stamp}
                                  for r in rows])
            total += len(rows)
            print(f"    {grain}: {len(rows)} rows")
    finally:
        conn.close()
    return total, failures


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", help="snapshot date (default: today)")
    p.add_argument("--only", help="comma-separated grain subset: " + ", ".join(GRAINS))
    args = p.parse_args()
    snapshot = args.date or date.today().isoformat()
    only = [g.strip() for g in args.only.split(",")] if args.only else None
    if only:
        unknown = set(only) - set(GRAINS)
        if unknown:
            raise SystemExit(f"unknown grain(s) {sorted(unknown)}; valid: {', '.join(GRAINS)}")

    db.init_db()
    started = db.now()
    try:
        n, failures = run(snapshot, only)
    except Exception as e:  # noqa: BLE001
        db.log_sync("google_structure", started, 0, "error", str(e))
        raise
    grains = ",".join(only) if only else "all grains"
    status = "degraded" if failures else "ok"
    msg = f"{snapshot} ({grains})"
    if failures:
        msg = f"PARTIAL {len(failures)} failed: " + "; ".join(failures) + " | " + msg
    db.log_sync("google_structure", started, n, status, msg)
    print(f"Google structure: wrote {n} rows [{grains}] ({snapshot})")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
