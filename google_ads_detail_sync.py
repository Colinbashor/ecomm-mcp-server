r"""
Google Ads DETAIL reports -> warehouse. Fills in the grains that a plain
campaign-level daily sync (see warehouse/connectors/google_ads.py's `sync()`)
can't reach on its own: what customers actually searched for, how individual
keywords are performing, per-product Shopping/Performance Max demand, and a
handful of narrower diagnostics below.

Standalone script: creates its own tables in warehouse.db via ensure_schema(),
so nothing else in the repo needs to change to query them.

Seven independently-selectable grains (--only):
  google_search_terms          — search_term_view: the actual customer query
                                  x campaign x ad group x day (impressions,
                                  clicks, spend, conversions, revenue). The
                                  basis for negative-keyword and wasted-spend
                                  audits.
  google_keywords               — keyword_view: keyword x ad group x day,
                                  plus Quality Score.
  google_shopping_products       — shopping_performance_view: Shopping/PMax
                                  product x campaign x day, with product
                                  title. The per-product ad-demand signal —
                                  useful as an early inventory/reorder
                                  indicator alongside real sales data.
  google_paid_organic            — paid_organic_search_term_view: the SAME
                                  query's paid clicks next to its organic
                                  clicks — the direct measure of how much a
                                  paid campaign might be cannibalizing organic
                                  traffic it would have won anyway. This view
                                  REJECTS money metrics entirely (cost,
                                  conversions, and conversion value all error
                                  if requested) — join google_search_terms on
                                  the search term for spend.
  google_conversion_actions_daily — which named conversion ACTIONS feed the
                                  `conversions` column, split out daily.
                                  NEVER sum this alongside ad_metrics /
                                  google_ads.py's own conversions column: it's
                                  the same number broken out by action, so
                                  summing both together double-counts.
  google_campaign_devices         — the mobile/desktop/tablet split that a
                                  campaign-level daily pull can't see at all.
  google_pmax_search_themes       — campaign_search_term_insight: the ONLY
                                  query-level visibility Performance Max
                                  campaigns expose (search_term_view returns
                                  zero rows for PMax). See "WHY THIS GRAIN IS
                                  DIFFERENT" below — it is WINDOW-aggregated,
                                  not date-keyed, and is skipped by a plain
                                  run.

Same credentials as the campaign-level connector (GOOGLE_ADS_* in .env) — no
new setup required if you're already running that sync. Google Ads keeps deep
history (data back to roughly 2015 on most accounts), so backfills over wide
date ranges are possible; a scheduled run typically just needs `--days 3` or
so to stay current. One grain failing doesn't stop the others — each is
fetched and committed independently, and a partial run is logged as
"degraded" rather than silently reported clean.

WHY THIS GRAIN IS DIFFERENT (google_pmax_search_themes). Every other grain
here is keyed by a specific calendar date, so a daily run just adds new rows.
`campaign_search_term_insight` isn't like that: Google Ads only returns it as
an aggregate over whatever date range you request, with no daily breakdown —
the WINDOW itself is the only time dimension you get. A daily `--days 3` run
would therefore store a new overlapping window every night that can never be
summed against the others without double-counting, so this grain is excluded
from a plain run and only pulled when named explicitly with an aligned
window, e.g.:
    python google_ads_detail_sync.py --only google_pmax_search_themes --start 2026-06-01 --end 2026-06-30
The resource also requires an explicit `campaign_search_term_insight.campaign_id`
filter per Google Ads API rules, so this is one request per campaign rather
than one request for the whole account.

CONVERSION-ACTION HEALTH IS WORTH CHECKING BEFORE YOU TRUST ROAS. Which
actions count toward the account's `conversions` metric is an ACCOUNT-LEVEL
GOAL setting, not something visible on the action itself — an action can be
`ENABLED` and `primary_for_goal=true` and still never contribute in practice,
or the reverse. `google_conversion_actions_daily` makes that split auditable
day by day: if `all_conversions` runs far above `conversions` on the same
campaign-day, that's usually low-value micro-actions (page views, "view
item", engagement pings) getting folded into `all_conversions`, which is why
this repo treats `all_conversions` as diagnostic only, never as a business
metric to report.

USAGE:
  python google_ads_detail_sync.py --days 3
  python google_ads_detail_sync.py --start 2026-06-01 --end 2026-06-30
  python google_ads_detail_sync.py --only google_shopping_products --start 2026-06-01 --end 2026-06-30
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

from warehouse import db
from warehouse.connectors import google_ads

DDL = """
CREATE TABLE IF NOT EXISTS google_search_terms (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    campaign_name TEXT,
    ad_group_id   TEXT NOT NULL,
    ad_group_name TEXT,
    search_term   TEXT NOT NULL,
    status        TEXT,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,
    conversions   REAL    DEFAULT 0,
    revenue       REAL    DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, ad_group_id, search_term)
);
CREATE INDEX IF NOT EXISTS idx_gst_date ON google_search_terms(date);
CREATE INDEX IF NOT EXISTS idx_gst_term ON google_search_terms(search_term);

CREATE TABLE IF NOT EXISTS google_keywords (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    campaign_name TEXT,
    ad_group_id   TEXT NOT NULL,
    ad_group_name TEXT,
    criterion_id  TEXT NOT NULL,
    keyword       TEXT,
    match_type    TEXT,
    quality_score INTEGER,
    status        TEXT,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,
    conversions   REAL    DEFAULT 0,
    revenue       REAL    DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, ad_group_id, criterion_id)
);
CREATE INDEX IF NOT EXISTS idx_gkw_date ON google_keywords(date);
CREATE INDEX IF NOT EXISTS idx_gkw_qs ON google_keywords(quality_score);

CREATE TABLE IF NOT EXISTS google_shopping_products (
    account_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    campaign_id     TEXT NOT NULL,
    campaign_name   TEXT,
    product_item_id TEXT NOT NULL,
    product_title   TEXT,
    impressions     INTEGER DEFAULT 0,
    clicks          INTEGER DEFAULT 0,
    spend           REAL    DEFAULT 0,
    conversions     REAL    DEFAULT 0,
    revenue         REAL    DEFAULT 0,
    synced_at       TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, product_item_id)
);
CREATE INDEX IF NOT EXISTS idx_gsp_date ON google_shopping_products(date);
CREATE INDEX IF NOT EXISTS idx_gsp_pid ON google_shopping_products(product_item_id);

-- Paid clicks next to ORGANIC clicks for the same query: the direct
-- cannibalization measure. NO COST COLUMN — the view rejects money metrics
-- (see module docstring); join google_search_terms on the term for spend.
CREATE TABLE IF NOT EXISTS google_paid_organic (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    campaign_name TEXT,
    ad_group_id   TEXT NOT NULL,
    search_term   TEXT NOT NULL,
    paid_impressions    INTEGER DEFAULT 0,
    paid_clicks         INTEGER DEFAULT 0,
    organic_impressions INTEGER DEFAULT 0,
    organic_clicks      INTEGER DEFAULT 0,
    organic_impressions_per_query REAL DEFAULT 0,
    organic_clicks_per_query      REAL DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, ad_group_id, search_term)
);
CREATE INDEX IF NOT EXISTS idx_gpo_date ON google_paid_organic(date);
CREATE INDEX IF NOT EXISTS idx_gpo_term ON google_paid_organic(search_term);

-- Which conversion ACTIONS feed the Conversions column. NEVER SUM `conversions`
-- here alongside ad_metrics/google_ads.py's own conversions column — this is
-- the same number split by action, so summing across actions reproduces the
-- campaign total, and summing both together double-counts.
CREATE TABLE IF NOT EXISTS google_conversion_actions_daily (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    campaign_name TEXT,
    conversion_action   TEXT NOT NULL,
    conversion_category TEXT,
    conversions           REAL DEFAULT 0,
    conversions_value     REAL DEFAULT 0,
    all_conversions       REAL DEFAULT 0,
    all_conversions_value REAL DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, conversion_action)
);
CREATE INDEX IF NOT EXISTS idx_gcad_date ON google_conversion_actions_daily(date);

CREATE TABLE IF NOT EXISTS google_campaign_devices (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    campaign_name TEXT,
    campaign_type TEXT,
    device        TEXT NOT NULL,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,
    conversions   REAL    DEFAULT 0,
    revenue       REAL    DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, device)
);
CREATE INDEX IF NOT EXISTS idx_gcd_date ON google_campaign_devices(date);

-- PMax search THEMES (clustered, not raw queries) — the only query visibility
-- PMax has. WINDOW-aggregated, not date-keyed: never sum overlapping windows.
CREATE TABLE IF NOT EXISTS google_pmax_search_themes (
    account_id    TEXT NOT NULL,
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    insight_id    TEXT NOT NULL,
    search_theme  TEXT,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    conversions   REAL    DEFAULT 0,
    revenue       REAL    DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, window_start, window_end, campaign_id, insight_id)
);
CREATE INDEX IF NOT EXISTS idx_gpst_window ON google_pmax_search_themes(window_end);
"""

REPORTS = {
    "google_search_terms": {
        "fetch": google_ads.sync_search_terms,
        "insert": """INSERT OR REPLACE INTO google_search_terms
                     (account_id, date, campaign_id, campaign_name, ad_group_id,
                      ad_group_name, search_term, status, impressions, clicks,
                      spend, conversions, revenue, synced_at)
                     VALUES (:account_id,:date,:campaign_id,:campaign_name,:ad_group_id,
                      :ad_group_name,:search_term,:status,:impressions,:clicks,
                      :spend,:conversions,:revenue,:synced_at)""",
    },
    "google_keywords": {
        "fetch": google_ads.sync_keywords,
        "insert": """INSERT OR REPLACE INTO google_keywords
                     (account_id, date, campaign_id, campaign_name, ad_group_id,
                      ad_group_name, criterion_id, keyword, match_type, quality_score,
                      status, impressions, clicks, spend, conversions, revenue, synced_at)
                     VALUES (:account_id,:date,:campaign_id,:campaign_name,:ad_group_id,
                      :ad_group_name,:criterion_id,:keyword,:match_type,:quality_score,
                      :status,:impressions,:clicks,:spend,:conversions,:revenue,:synced_at)""",
    },
    "google_shopping_products": {
        "fetch": google_ads.sync_shopping_products,
        "insert": """INSERT OR REPLACE INTO google_shopping_products
                     (account_id, date, campaign_id, campaign_name, product_item_id,
                      product_title, impressions, clicks, spend, conversions, revenue, synced_at)
                     VALUES (:account_id,:date,:campaign_id,:campaign_name,:product_item_id,
                      :product_title,:impressions,:clicks,:spend,:conversions,:revenue,:synced_at)""",
    },
    "google_paid_organic": {
        "fetch": google_ads.sync_paid_organic,
        "insert": """INSERT OR REPLACE INTO google_paid_organic
                     (account_id, date, campaign_id, campaign_name, ad_group_id,
                      search_term, paid_impressions, paid_clicks, organic_impressions,
                      organic_clicks, organic_impressions_per_query,
                      organic_clicks_per_query, synced_at)
                     VALUES (:account_id,:date,:campaign_id,:campaign_name,:ad_group_id,
                      :search_term,:paid_impressions,:paid_clicks,:organic_impressions,
                      :organic_clicks,:organic_impressions_per_query,
                      :organic_clicks_per_query,:synced_at)""",
    },
    "google_conversion_actions_daily": {
        "fetch": google_ads.sync_conversion_action_daily,
        "insert": """INSERT OR REPLACE INTO google_conversion_actions_daily
                     (account_id, date, campaign_id, campaign_name, conversion_action,
                      conversion_category, conversions, conversions_value,
                      all_conversions, all_conversions_value, synced_at)
                     VALUES (:account_id,:date,:campaign_id,:campaign_name,:conversion_action,
                      :conversion_category,:conversions,:conversions_value,
                      :all_conversions,:all_conversions_value,:synced_at)""",
    },
    "google_campaign_devices": {
        "fetch": google_ads.sync_campaign_devices,
        "insert": """INSERT OR REPLACE INTO google_campaign_devices
                     (account_id, date, campaign_id, campaign_name, campaign_type,
                      device, impressions, clicks, spend, conversions, revenue, synced_at)
                     VALUES (:account_id,:date,:campaign_id,:campaign_name,:campaign_type,
                      :device,:impressions,:clicks,:spend,:conversions,:revenue,:synced_at)""",
    },
    # Window-aggregated, so it does NOT take the per-chunk date pair the way
    # the others do — the window IS the key. Chunking still works: each
    # 30-day chunk stores its own window rather than overwriting the last one.
    "google_pmax_search_themes": {
        "fetch": google_ads.sync_pmax_search_themes,
        "insert": """INSERT OR REPLACE INTO google_pmax_search_themes
                     (account_id, window_start, window_end, campaign_id, insight_id,
                      search_theme, impressions, clicks, conversions, revenue, synced_at)
                     VALUES (:account_id,:window_start,:window_end,:campaign_id,:insight_id,
                      :search_theme,:impressions,:clicks,:conversions,:revenue,:synced_at)""",
    },
}

# Grains whose rows are keyed by the WINDOW rather than a date. A daily run
# would store a fresh overlapping window every night (near-duplicate rows
# piling up with no way to sum them safely), so these are skipped unless asked
# for by name. Pull them on a reporting-aligned window instead, e.g.:
#   --only google_pmax_search_themes --start 2026-06-01 --end 2026-06-30
WINDOW_GRAINS = frozenset({"google_pmax_search_themes"})


def ensure_schema(conn) -> None:
    """Create this connector's tables if they don't exist yet. Safe to call
    every run — matches the CREATE TABLE IF NOT EXISTS pattern used across
    the rest of the warehouse."""
    conn.executescript(DDL)


def _chunks(start: str, end: str):
    """<=30-day windows so a big backfill commits in pieces rather than
    holding one long-lived write transaction."""
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    while lo <= hi:
        nxt = min(hi, lo + timedelta(days=29))
        yield lo.isoformat(), nxt.isoformat()
        lo = nxt + timedelta(days=1)


def run(start: str, end: str, only: list[str] | None = None) -> tuple[int, list[str]]:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db.connect()
    ensure_schema(conn)
    # Window-keyed grains are opt-in by name; see WINDOW_GRAINS.
    tables = [t for t in REPORTS
              if (t in only if only else t not in WINDOW_GRAINS)]

    total = 0
    failures: list[str] = []
    try:
        for lo, hi in _chunks(start, end):
            for table in tables:
                cfg = REPORTS[table]
                try:
                    rows = cfg["fetch"](lo, hi)
                except Exception as e:  # noqa: BLE001 — one grain must not kill the rest
                    failures.append(f"{table} {lo}..{hi}: {str(e)[:120]}")
                    print(f"    {table} {lo}..{hi} FAILED: {str(e)[:120]}")
                    continue
                with conn:  # commit per report — never hold the write lock long
                    conn.executemany(cfg["insert"], [{**r, "synced_at": stamp} for r in rows])
                total += len(rows)
                print(f"    {table} {lo}..{hi}: {len(rows)} rows")
    finally:
        conn.close()
    if failures and not total:
        raise RuntimeError("; ".join(failures)[:400])
    return total, failures


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--only", help="comma-separated table subset: "
                   + ", ".join(REPORTS))
    args = p.parse_args()
    end = args.end or date.today().isoformat()
    start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()
    only = [t.strip() for t in args.only.split(",")] if args.only else None
    if only:
        unknown = set(only) - set(REPORTS)
        if unknown:
            raise SystemExit(f"unknown grain(s) {sorted(unknown)}; valid: {', '.join(REPORTS)}")

    db.init_db()
    started = db.now()
    try:
        n, failures = run(start, end, only)
    except Exception as e:  # noqa: BLE001
        db.log_sync("google_detail", started, 0, "error", str(e))
        raise
    grains = ",".join(only) if only else "all grains"
    # Some grains landed but others failed => 'degraded' (not 'ok'): a broken
    # grain must not read green to last_sync_status. Marker goes first so it
    # shows in a truncated message preview too.
    status = "degraded" if failures else "ok"
    msg = f"{start} -> {end} ({grains})"
    if failures:
        msg = f"PARTIAL {len(failures)} failed: " + "; ".join(failures) + " | " + msg
    db.log_sync("google_detail", started, n, status, msg)
    if failures:
        print(f"Google detail DEGRADED: {len(failures)} grain-window(s) failed")
    print(f"Google detail: wrote {n} rows [{grains}] ({start} -> {end})"
          + (f" — {len(failures)} grain-window(s) FAILED" if failures else ""))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
