r"""
Google Analytics 4 (GA4) -> warehouse sync.

Standalone script: creates its own tables in warehouse.db via ensure_schema(),
so nothing else in the repo needs to change to query them (the MCP server's
generic run_sql / list_tables tools work against any table automatically).

Four reports per run:
  ga_metrics       — daily full-funnel totals by default channel group
                     (sessions, users, conversions, revenue, ...), plus a
                     cookie-scoped first_time_purchasers count per channel
                     (see "FIRST-TIME PURCHASERS ARE COOKIE-SCOPED" below).
  ga_products      — daily item-level views/add-to-cart/purchases/revenue.
                     Pulled in DAILY chunks (see "THE 100K-ROW CAP" below).
  ga_landing_pages — daily landing-page sessions/conversions/revenue, limited
                     to pages with meaningful traffic that day.
  ga_campaign_ntb  — daily new-vs-returning split PER GOOGLE ADS CAMPAIGN (see
                     "NEW-TO-BRAND BY CAMPAIGN" below). Answers "is this
                     campaign acquiring new customers or just re-selling to
                     existing ones?" — a question the channel-level metrics
                     report can't reach, since it only has channel, not
                     campaign, granularity.

SETUP (once):
  1. Install the client library into your venv:
       pip install google-analytics-data
  2. In Google Cloud Console, create (or reuse) a service account and
     download its JSON key. Enable the "Google Analytics Data API" for that
     project.
  3. In GA4: Admin -> Property Access Management -> add the service account's
     email (looks like name@project.iam.gserviceaccount.com) as a Viewer on
     the property you want to sync. This is a server-to-server credential,
     not an interactive OAuth flow like google_auth.py's — there is no
     browser consent step, just a key file and a GA4 permission grant.
  4. Add to .env:
       GA4_PROPERTY_ID=123456789
       GA4_CREDENTIALS_FILE=C:\path\to\ga4-service-account.json

USAGE:
  python ga4_sync.py --days 90
  python ga4_sync.py --start 2025-01-01 --end 2025-12-31
  python ga4_sync.py --only products --days 7

GOTCHAS WORTH KNOWING BEFORE YOU MODIFY THIS FILE
--------------------------------------------------
THE 100K-ROW CAP. A single GA4 Data API report response is capped at 100,000
rows, and unlike most Google Data APIs there is no cursor token — you page it
with `limit` + `offset` instead, and `row_count` on the response tells you the
TOTAL matching rows (not the page size), so you know when to stop. `_run()`
below always follows that loop to completion rather than trusting one request.
An earlier, simpler version of this kind of connector can silently under-report
if it issues one capped request and never checks whether more rows existed —
that failure mode is invisible unless you specifically look for a response
that came back exactly at the cap.

WHY THE ITEM REPORT IS CHUNKED BY DAY, NOT BY WEEK OR MONTH. Channel-level and
landing-page reports have low cardinality (one row per channel or per landing
page per day) and comfortably fit a monthly request under the 100k cap.
Item-level reports do not: an item row exists per (date, item_id), and a
catalog of any real size multiplied by a week of dates can approach or exceed
the cap on its own, at which point pagination just means "more, slower"
requests instead of "one that fits". Chunking day-by-day keeps each request's
row count bounded by catalog size alone, which is the more predictable knob.
If your property's catalog is small, monthly chunks may work fine — raise
GA4_PRODUCT_CHUNK_DAYS or write your own chunker.

ITEM_MIN_VIEWS is a cardinality control, not a business rule. The item report
below keeps an (date, item) row if the item sold OR was viewed more than this
many times that day. Set it to 0 to keep every item with at least one view;
raise it if your catalog is large enough that even daily chunks are hitting
the row cap and you only care about items with meaningful traffic.

TRANSIENT VS PERMANENT ERRORS. A long backfill issues hundreds or thousands of
sequential requests, and at any nonzero per-request failure rate that makes
hitting at least one transient error (504/503/500/429/quota) close to
certain — a short daily sync can get away with no retry logic, a long one
cannot. `_with_retry()` retries exactly the transient google.api_core
exception types and lets everything else (bad metric name, bad property id,
no permission) surface immediately, so a genuine configuration mistake isn't
silently retried into a long wait before failing anyway.

THE keyEvents/conversions METRIC RENAME. GA4 renamed the "conversions" metric
to "keyEvents" in 2024. Which name your property's API accepts depends on when
it was set up / migrated, so this connector probes once per run (an
inexpensive one-row request) and uses whichever name actually works, rather
than hardcoding either and breaking on half of all properties.

STALE DIMENSION VALUES. GA4 can stop returning a dimension value for a day
that it returned before — most commonly its own "(other)" rollup bucket, which
shows up while attribution is still unsettled for recent dates and disappears
once it settles. `INSERT OR REPLACE` only overwrites rows whose primary key
still appears in the new response, so a value that stops being returned is
never removed by a plain upsert — it just sits there forever, quietly
included in every future SUM. `_purge_dates()` deletes the days about to be
rewritten before inserting, so a sync always leaves exactly what GA4 just
reported for that day, never a superset built up over multiple runs.

PROPERTY ID MIX-UPS. If a fresh setup returns oddly small session counts,
double-check GA4_PROPERTY_ID against Admin -> Property Settings — GA4
accounts commonly have more than one property (e.g. a web property and a
separate mobile-app property under the same account), and pointing at the
wrong one is a very easy, very quiet mistake: it doesn't error, it just
returns tiny real numbers for a different property entirely.

NEW-TO-BRAND BY CAMPAIGN. GA4's `sessionGoogleAdsCampaignName` dimension
returns the campaign name byte-for-byte identical to whatever your Google Ads
connector calls it (verify this once for your own account — it's a documented
GA4 behavior, not something that needs reverse-engineering), so if you already
have a naming convention that encodes campaign intent (a "Brand" vs
"Non-Brand" prefix, a "-Prospecting" / "-Retargeting" suffix, anything
parseable), that convention carries straight over to this table with no extra
mapping step. `ga_campaign_ntb` splits sessions/users/transactions/revenue by
`newVsReturning` (new / returning / "(not set)" — the last is real volume GA4
simply can't classify, typically a few percent of sessions; never assume
new + returning sums to the total) per campaign per day, so you can compare
new-customer acquisition efficiency across campaigns directly. GA4 reports all
non-Google-Ads traffic under a single "(not set)" pseudo-campaign that can
dwarf real campaign volume (organic + direct + email + everything else that
isn't a Google Ads click); this connector filters it out of
`ga_campaign_ntb` specifically so a naive SUM over the table can't
double-count it — the channel-level baseline (including that traffic) still
lives in `ga_metrics`, which is why `ga_metrics` carries its own
`first_time_purchasers` column for that comparison.

FIRST-TIME PURCHASERS ARE COOKIE-SCOPED, NOT CUSTOMER-IDENTITY-SCOPED. GA4's
`firstTimePurchasers` metric is scoped to the browser/device, not to a real
customer identity: a returning customer on a new device, or one who cleared
cookies, counts as "first-time." For a brand with meaningful repeat business
this metric commonly reads far higher (sometimes 70-90%+ of sessions) than any
real new-customer rate, on every channel — so don't hand an absolute count of
it to anyone making a business decision on true new-customer acquisition.
It IS trustworthy as a RELATIVE index for comparing channels or campaigns
against each other, since the same cookie-scoping bias applies everywhere, and
it still discriminates in the expected direction (channels built around
repeat engagement, like email/SMS, read meaningfully lower than pure
prospecting channels). A true new-to-brand count needs a real customer
identity join, which this connector — being GA4-only — cannot provide; if your
warehouse has an order-level customer id (e.g. from a commerce platform
connector), that's the more defensible source for an absolute NTB number.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from google.api_core import exceptions as gexc
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Filter, FilterExpression, FilterExpressionList,
    Metric, NumericValue, RunReportRequest,
)
from google.oauth2 import service_account

from warehouse import db

load_dotenv()

# Item report inclusion floor: keep an (date, item) row if the item sold OR
# was viewed MORE than this many times that day. 0 keeps every viewed item.
# See "ITEM_MIN_VIEWS is a cardinality control" in the module docstring.
ITEM_MIN_VIEWS = 0

# Landing pages report: only keep pages with more than this many sessions that
# day, so the report doesn't fill up with one-visit noise.
LANDING_PAGE_MIN_SESSIONS = 4

# GA4's own pseudo-campaign for every session that didn't arrive via a Google
# Ads click (organic, direct, email, everything else). Excluded from
# ga_campaign_ntb — see "NEW-TO-BRAND BY CAMPAIGN" in the module docstring.
NOT_SET_CAMPAIGN = "(not set)"

DDL = """
CREATE TABLE IF NOT EXISTS ga_metrics (
    property_id      TEXT NOT NULL,
    date             TEXT NOT NULL,
    channel          TEXT NOT NULL,
    sessions         INTEGER DEFAULT 0,
    users            INTEGER DEFAULT 0,
    conversions      REAL    DEFAULT 0,
    revenue          REAL    DEFAULT 0,
    new_users        INTEGER DEFAULT 0,
    engaged_sessions INTEGER DEFAULT 0,
    purchases        INTEGER DEFAULT 0,   -- transactions
    -- cookie/device-scoped, NOT customer-identity-scoped — see
    -- "FIRST-TIME PURCHASERS ARE COOKIE-SCOPED" in the module docstring.
    first_time_purchasers INTEGER DEFAULT 0,
    synced_at        TEXT NOT NULL,
    PRIMARY KEY (property_id, date, channel)
);
CREATE INDEX IF NOT EXISTS idx_ga_metrics_date ON ga_metrics(date);

CREATE TABLE IF NOT EXISTS ga_products (
    property_id          TEXT NOT NULL,
    date                 TEXT NOT NULL,
    item_id               TEXT NOT NULL,
    item_name             TEXT,
    items_viewed          INTEGER DEFAULT 0,
    items_added_to_cart   INTEGER DEFAULT 0,
    items_purchased       INTEGER DEFAULT 0,
    item_revenue          REAL    DEFAULT 0,
    synced_at             TEXT NOT NULL,
    PRIMARY KEY (property_id, date, item_id)
);
CREATE INDEX IF NOT EXISTS idx_ga_products_date ON ga_products(date);

CREATE TABLE IF NOT EXISTS ga_landing_pages (
    property_id      TEXT NOT NULL,
    date             TEXT NOT NULL,
    landing_page     TEXT NOT NULL,
    sessions         INTEGER DEFAULT 0,
    engaged_sessions INTEGER DEFAULT 0,
    conversions      REAL    DEFAULT 0,
    purchases        INTEGER DEFAULT 0,
    revenue          REAL    DEFAULT 0,
    synced_at        TEXT NOT NULL,
    PRIMARY KEY (property_id, date, landing_page)
);
CREATE INDEX IF NOT EXISTS idx_ga_landing_date ON ga_landing_pages(date);

-- New-vs-returning split per Google Ads campaign — see "NEW-TO-BRAND BY
-- CAMPAIGN" in the module docstring. visitor_type is GA4's newVsReturning:
-- 'new' | 'returning' | '(not set)' (real, unclassifiable volume — keep it).
CREATE TABLE IF NOT EXISTS ga_campaign_ntb (
    property_id           TEXT NOT NULL,
    date                  TEXT NOT NULL,
    campaign_name         TEXT NOT NULL,
    visitor_type          TEXT NOT NULL,
    sessions              INTEGER DEFAULT 0,
    users                 INTEGER DEFAULT 0,
    new_users             INTEGER DEFAULT 0,
    transactions          INTEGER DEFAULT 0,
    purchase_revenue      REAL    DEFAULT 0,
    first_time_purchasers INTEGER DEFAULT 0,
    synced_at             TEXT NOT NULL,
    PRIMARY KEY (property_id, date, campaign_name, visitor_type)
);
CREATE INDEX IF NOT EXISTS idx_ga_ntb_date ON ga_campaign_ntb(date);
CREATE INDEX IF NOT EXISTS idx_ga_ntb_campaign ON ga_campaign_ntb(campaign_name);
"""

# Columns added after a table may already exist from an older version of this
# script — applied on the fly so an existing warehouse.db doesn't need to be
# dropped to pick up a new column.
MIGRATE_COLUMNS = ("first_time_purchasers INTEGER DEFAULT 0",)

# Grain names accepted by --only. "products" is the expensive one (daily
# chunks); "metrics" and "landing_pages" are one request per month of range;
# "campaign_ntb" is cheap (low cardinality: campaigns x 3 visitor types x days).
GRAINS = ("metrics", "products", "landing_pages", "campaign_ntb")

# GA4 caps a single report response at 100k rows; _run() pages past it with
# offset/limit rather than trusting one request.
PAGE_LIMIT = 100_000

# TRANSIENT GA4 failures — retry these; anything else (bad metric/dimension
# name, bad property id, no permission) is a real error and must surface
# immediately rather than being retried into a long silence.
TRANSIENT_ERRORS = (
    gexc.DeadlineExceeded,      # 504
    gexc.ServiceUnavailable,    # 503
    gexc.InternalServerError,   # 500
    gexc.TooManyRequests,       # 429
    gexc.ResourceExhausted,     # quota, usually momentary
    gexc.Aborted,
)
RETRY_TRIES = 6
RETRY_BASE_SECONDS = 5  # 5, 10, 20, 40, 80, 160 seconds of backoff


def ensure_schema(conn) -> None:
    """Create this connector's tables if they don't exist yet, and add any
    columns introduced after a table may already have been created (see
    MIGRATE_COLUMNS) — safe to call every run."""
    conn.executescript(DDL)
    existing = {c[1] for c in conn.execute("PRAGMA table_info(ga_metrics)")}
    for col_def in MIGRATE_COLUMNS:
        if col_def.split()[0] not in existing:
            conn.execute(f"ALTER TABLE ga_metrics ADD COLUMN {col_def}")


def _client() -> BetaAnalyticsDataClient:
    cred_file = os.environ.get("GA4_CREDENTIALS_FILE")
    if cred_file:
        creds = service_account.Credentials.from_service_account_file(cred_file)
        return BetaAnalyticsDataClient(credentials=creds)
    # Falls back to GOOGLE_APPLICATION_CREDENTIALS if that's how you prefer
    # to supply the key (e.g. already used by another Google connector).
    return BetaAnalyticsDataClient()


def _gt_filter(metric: str, threshold: int) -> FilterExpression:
    """metric > threshold."""
    return FilterExpression(filter=Filter(
        field_name=metric,
        numeric_filter=Filter.NumericFilter(
            operation=Filter.NumericFilter.Operation.GREATER_THAN,
            value=NumericValue(int64_value=threshold),
        ),
    ))


def _not_dimension(dimension: str, value: str) -> FilterExpression:
    """dimension != value — used to drop GA4's "(not set)" pseudo-campaign
    from ga_campaign_ntb (see NOT_SET_CAMPAIGN)."""
    return FilterExpression(not_expression=FilterExpression(filter=Filter(
        field_name=dimension,
        string_filter=Filter.StringFilter(
            value=value, match_type=Filter.StringFilter.MatchType.EXACT),
    )))


def _with_retry(fn, what: str):
    """Call fn(), retrying TRANSIENT_ERRORS with exponential backoff."""
    for attempt in range(RETRY_TRIES):
        try:
            return fn()
        except TRANSIENT_ERRORS as exc:
            if attempt == RETRY_TRIES - 1:
                raise
            wait = RETRY_BASE_SECONDS * (2 ** attempt)
            print(f"    ga4 transient {type(exc).__name__} on {what}; "
                  f"retry {attempt + 1}/{RETRY_TRIES - 1} in {wait}s")
            time.sleep(wait)


def _run(client, prop: str, start: str, end: str, dimensions: list[str],
         metrics: list[str], metric_filter: FilterExpression | None = None,
         dimension_filter: FilterExpression | None = None):
    """Run one report, following GA4's offset pagination to completion.

    A naive version of this would issue one request capped at 100k rows and
    just warn when the response came back short — silently storing partial
    data whenever a chunk's true row count exceeds the cap. This instead pages
    with offset/limit until `len(rows) >= resp.row_count` (the API's own count
    of total matching rows, not the page size), so a chunk that turns out
    bigger than expected is still captured in full.
    """
    rows = []
    offset = 0
    while True:
        resp = _with_retry(lambda off=offset: client.run_report(RunReportRequest(
            property=f"properties/{prop}",
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            metric_filter=metric_filter,
            dimension_filter=dimension_filter,
            limit=PAGE_LIMIT,
            offset=off,
        )), f"{dimensions} {start}..{end}")
        rows.extend(resp.rows)
        if not resp.rows or len(rows) >= resp.row_count:
            break
        offset += len(resp.rows)
    return rows


def _resolve_conversion_metric(client, prop: str, probe_date: str) -> str:
    """GA4 renamed "conversions" -> "keyEvents" in 2024. Probe once per run
    (a single cheap request) and use whichever name this property's API
    version actually accepts."""
    try:
        _run(client, prop, probe_date, probe_date, ["date"], ["keyEvents"])
        return "keyEvents"
    except Exception:  # noqa: BLE001 — any failure just means "use the old name"
        return "conversions"


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _month_chunks(start: str, end: str):
    """Yield (start, end) ISO pairs, split on calendar month boundaries."""
    lo = date.fromisoformat(start)
    hi = date.fromisoformat(end)
    while lo <= hi:
        nxt = (lo.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield lo.isoformat(), min(hi, nxt - timedelta(days=1)).isoformat()
        lo = nxt


def _day_chunks(start: str, end: str):
    """Yield one (day, day) ISO pair per calendar day — see "WHY THE ITEM
    REPORT IS CHUNKED BY DAY" in the module docstring."""
    lo = date.fromisoformat(start)
    hi = date.fromisoformat(end)
    while lo <= hi:
        yield lo.isoformat(), lo.isoformat()
        lo += timedelta(days=1)


def _purge_dates(conn, table: str, prop: str, rows: list, date_index: int = 1) -> None:
    """Delete the days about to be rewritten, before rewriting them.

    See "STALE DIMENSION VALUES" in the module docstring for why this matters:
    INSERT OR REPLACE alone can never remove a row whose dimension value GA4
    has stopped returning, so without this a sync only ever adds rows and
    never corrects one that used to exist but shouldn't anymore.
    """
    days = sorted({r[date_index] for r in rows})
    if not days:
        return
    placeholders = ",".join("?" * len(days))
    conn.execute(
        f"DELETE FROM {table} WHERE property_id = ? AND date IN ({placeholders})",
        (prop, *days),
    )


def sync_metrics(client, conn, prop: str, start: str, end: str,
                  conversion_metric: str, stamp: str) -> int:
    """Daily full-funnel totals by default channel group."""
    rows = []
    for r in _run(client, prop, start, end,
                  ["date", "sessionDefaultChannelGroup"],
                  ["sessions", "totalUsers", conversion_metric, "totalRevenue",
                   "newUsers", "engagedSessions", "transactions",
                   "firstTimePurchasers"]):
        m = [v.value for v in r.metric_values]
        rows.append((
            prop, _iso(r.dimension_values[0].value), r.dimension_values[1].value,
            int(float(m[0] or 0)), int(float(m[1] or 0)),
            float(m[2] or 0), float(m[3] or 0),
            int(float(m[4] or 0)), int(float(m[5] or 0)), int(float(m[6] or 0)),
            int(float(m[7] or 0)),
            stamp,
        ))
    _purge_dates(conn, "ga_metrics", prop, rows)
    conn.executemany(
        """INSERT OR REPLACE INTO ga_metrics
           (property_id, date, channel, sessions, users, conversions, revenue,
            new_users, engaged_sessions, purchases, first_time_purchasers, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def sync_campaign_ntb(client, conn, prop: str, start: str, end: str, stamp: str) -> int:
    """New-vs-returning split per Google Ads campaign — see "NEW-TO-BRAND BY
    CAMPAIGN" in the module docstring. Filters out GA4's "(not set)"
    pseudo-campaign so a plain SUM over this table can't double-count the
    non-Google-Ads traffic that lives in ga_metrics instead."""
    rows = []
    for r in _run(client, prop, start, end,
                  ["date", "sessionGoogleAdsCampaignName", "newVsReturning"],
                  ["sessions", "totalUsers", "newUsers", "transactions",
                   "purchaseRevenue", "firstTimePurchasers"],
                  dimension_filter=_not_dimension(
                      "sessionGoogleAdsCampaignName", NOT_SET_CAMPAIGN)):
        m = [v.value for v in r.metric_values]
        rows.append((
            prop, _iso(r.dimension_values[0].value),
            r.dimension_values[1].value, r.dimension_values[2].value,
            int(float(m[0] or 0)), int(float(m[1] or 0)), int(float(m[2] or 0)),
            int(float(m[3] or 0)), float(m[4] or 0), int(float(m[5] or 0)),
            stamp,
        ))
    _purge_dates(conn, "ga_campaign_ntb", prop, rows)
    conn.executemany(
        """INSERT OR REPLACE INTO ga_campaign_ntb
           (property_id, date, campaign_name, visitor_type, sessions, users,
            new_users, transactions, purchase_revenue, first_time_purchasers,
            synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def sync_products(client, conn, prop: str, start: str, end: str, stamp: str) -> int:
    """Daily item-level views/add-to-cart/purchases/revenue, for every item
    that sold OR got at least ITEM_MIN_VIEWS+1 views that day."""
    item_filter = FilterExpression(or_group=FilterExpressionList(expressions=[
        _gt_filter("itemsPurchased", 0),
        _gt_filter("itemsViewed", ITEM_MIN_VIEWS),
    ]))
    rows = []
    for r in _run(client, prop, start, end,
                  ["date", "itemId", "itemName"],
                  ["itemsViewed", "itemsAddedToCart", "itemsPurchased", "itemRevenue"],
                  metric_filter=item_filter):
        m = [v.value for v in r.metric_values]
        rows.append((
            prop, _iso(r.dimension_values[0].value),
            r.dimension_values[1].value, r.dimension_values[2].value,
            int(float(m[0] or 0)), int(float(m[1] or 0)),
            int(float(m[2] or 0)), float(m[3] or 0), stamp,
        ))
    _purge_dates(conn, "ga_products", prop, rows)
    conn.executemany(
        """INSERT OR REPLACE INTO ga_products
           (property_id, date, item_id, item_name, items_viewed,
            items_added_to_cart, items_purchased, item_revenue, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def sync_landing_pages(client, conn, prop: str, start: str, end: str, stamp: str) -> int:
    """Daily landing-page performance, limited to pages with meaningful
    traffic that day (see LANDING_PAGE_MIN_SESSIONS)."""
    rows = []
    for r in _run(client, prop, start, end,
                  ["date", "landingPage"],
                  ["sessions", "engagedSessions", "conversions",
                   "transactions", "totalRevenue"],
                  metric_filter=_gt_filter("sessions", LANDING_PAGE_MIN_SESSIONS)):
        m = [v.value for v in r.metric_values]
        rows.append((
            prop, _iso(r.dimension_values[0].value), r.dimension_values[1].value,
            int(float(m[0] or 0)), int(float(m[1] or 0)), float(m[2] or 0),
            int(float(m[3] or 0)), float(m[4] or 0), stamp,
        ))
    _purge_dates(conn, "ga_landing_pages", prop, rows)
    conn.executemany(
        """INSERT OR REPLACE INTO ga_landing_pages
           (property_id, date, landing_page, sessions, engaged_sessions,
            conversions, purchases, revenue, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def run(start: str, end: str, only: frozenset[str] | None = None) -> int:
    prop = os.environ["GA4_PROPERTY_ID"]
    client = _client()
    conversion_metric = _resolve_conversion_metric(client, prop, end)
    grains = only or frozenset(GRAINS)

    stamp = db.now()
    conn = db.connect()
    ensure_schema(conn)
    total = 0
    try:
        if "metrics" in grains:
            for lo, hi in _month_chunks(start, end):
                with conn:
                    total += sync_metrics(client, conn, prop, lo, hi, conversion_metric, stamp)
        if "products" in grains:
            for lo, hi in _day_chunks(start, end):
                with conn:
                    total += sync_products(client, conn, prop, lo, hi, stamp)
        if "landing_pages" in grains:
            for lo, hi in _month_chunks(start, end):
                with conn:
                    total += sync_landing_pages(client, conn, prop, lo, hi, stamp)
        if "campaign_ntb" in grains:
            for lo, hi in _month_chunks(start, end):
                with conn:
                    total += sync_campaign_ntb(client, conn, prop, lo, hi, stamp)
    finally:
        conn.close()
    return total


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--only", help="comma-separated grain subset: "
                   + ", ".join(GRAINS) + " (default: all)")
    args = p.parse_args()

    if not os.environ.get("GA4_PROPERTY_ID") or not os.environ.get("GA4_CREDENTIALS_FILE"):
        raise SystemExit(
            "ga4_sync: set GA4_PROPERTY_ID and GA4_CREDENTIALS_FILE in .env first "
            "(see the module docstring for the one-time service-account setup)."
        )

    end = args.end or date.today().isoformat()
    start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()

    only = None
    if args.only:
        only = frozenset(g.strip() for g in args.only.split(","))
        unknown = only - set(GRAINS)
        if unknown:
            raise SystemExit(f"unknown grain(s) {sorted(unknown)}; valid: {', '.join(GRAINS)}")

    db.init_db()
    started = db.now()
    try:
        n = run(start, end, only)
    except Exception as e:  # noqa: BLE001
        db.log_sync("ga4", started, 0, "error", str(e))
        raise
    grains_label = ",".join(sorted(only)) if only else "all grains"
    db.log_sync("ga4", started, n, "ok", f"{start} -> {end} ({grains_label})")
    print(f"GA4: wrote {n} rows [{grains_label}] ({start} -> {end})")


if __name__ == "__main__":
    main()
