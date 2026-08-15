r"""
Google Merchant Center (Merchant API v1) -> warehouse sync.

Standalone script: creates its own tables in warehouse.db via ensure_schema().
Pulls five report families, each independently selectable via --only:
  performance  — product_performance_view: ORGANIC vs ADS clicks/impressions/
                 conversions per product per day, PLUS non_product_performance_view
                 (account-wide, non-Shopping surfaces — e.g. Discovery/Demand
                 Gen style placements that don't map to one product) as a
                 second table under the same family. Most ad-platform
                 connectors in this repo (e.g. google_ads.py) only ever see
                 the PAID side of Shopping; this is the only place free/
                 organic listing clicks show up at all.
  status       — product_view: current feed-eligibility state per product
                 (ELIGIBLE / ELIGIBLE_LIMITED / NOT_ELIGIBLE_OR_DISAPPROVED /
                 PENDING) plus the disapproval/warning issues attached to it.
  pricing      — price_competitiveness_product_view: your price vs Google's
                 crowd-sourced benchmark price for the same product.
  bestsellers  — best_sellers_product_cluster_view: market-wide demand
                 ranking for a category (NOT your own sales — see "BEST
                 SELLERS IS A MARKET RANKING" below), plus
                 best_sellers_brand_view for tracking specific brands
                 (yours and/or competitors') across the rank curve via
                 --brand.
  visibility   — competitive_visibility_competitor_view: your rank vs named
                 competitor domains for a category, plus a benchmark trend.
                 See "VISIBILITY PUBLISHES A FEW DAYS LATE" below.

SETUP (once):
  1. Reuse or create a Google Cloud service account and download its JSON
     key (the same one used for ga4_sync.py works fine if you enable the
     Content API scope for it too — no new file required).
  2. Enable "merchantapi.googleapis.com" for that Cloud project.
  3. THE STEP EVERYONE MISSES: enabling the API is not enough. The Cloud
     project must also be REGISTERED against your merchant account with a
     one-time call — there is NO Merchant Center UI for this at all:
       POST https://merchantapi.googleapis.com/accounts/v1/accounts/{merchantId}/developerRegistration:registerGcp
       {"developerEmail": "<a human Google account with admin on the merchant account>"}
     Until this runs, EVERY endpoint returns 401 GCP_NOT_REGISTERED, which
     reads exactly like a permissions problem and is not one. The registering
     identity only needs merchant-account admin to make this ONE call —
     registration survives being downgraded afterward, and a project can be
     registered against exactly one merchant account at a time.
  4. Grant the service account access to the merchant account (Merchant
     Center -> Settings -> Account access -> add the service account email
     as a Standard user is enough; you do not need Admin for day-to-day
     reporting).
  5. Add to .env:
       GMC_MERCHANT_ID=1234567
       GMC_CREDENTIALS_FILE=C:\path\to\service-account.json

USAGE:
  python merchant_center_sync.py                                   # daily: everything, 3-day perf window
  python merchant_center_sync.py --days 30
  python merchant_center_sync.py --start 2025-01-01 --end 2025-12-31 --only performance
  python merchant_center_sync.py --only performance|status|pricing|bestsellers|visibility
  python merchant_center_sync.py --category 166:"Apparel & Accessories" --country US --country GB
  python merchant_center_sync.py --brand "Your Brand" --brand "Competitor Brand"
  python merchant_center_sync.py --backfill                          # walk performance back until it runs dry

GOTCHAS WORTH KNOWING BEFORE YOU MODIFY THIS FILE
--------------------------------------------------
QUERY LANGUAGE IS NOT THE OLD CONTENT API'S (GAQL). It is a flat snake_case
dialect of its own:
  * Field names are flat: `date`, `clicks`, `offer_id` — not `segments.date` /
    `metrics.clicks`. The old GAQL dotted names are simply rejected.
  * Omitting a dimension AGGREGATES the metrics across it automatically —
    there is no GROUP BY.
  * ORDER BY genuinely sorts and LIMIT genuinely caps (and suppresses
    nextPageToken on the last page), which is what makes an affordable
    top-N best-sellers pull possible at all.
  * Some views force certain fields into the SELECT clause as well as WHERE —
    a 400 names the exact missing field, so iterate on the error message
    rather than guessing the schema up front.
  * pageSize accepts up to 10,000 and latency is roughly FLAT with page size,
    so there is no reason ever to ask for less than the maximum.

THE conversion_value TRAP. Do not add a money-valued conversion field (e.g.
`conversion_value`) to the performance query unless you have specifically
verified your account's behavior. Selecting a money field implicitly
segments results by CURRENCY — which silently splits what should be one
logical (date, product) row into two or more, one per currency, with the
non-selected currency's numeric fields reading as zero on each split. If you
only take clicks/impressions/conversions (no money field), this cannot
happen. Keep conversion *value* in whichever ads-platform connector already
reports attributed revenue, and use this feed for clicks/impressions only.

THROTTLING IS REAL BUT ONLY UNDER SUSTAINED LOAD. A normal daily run (a
handful of sequential requests) sees no throttling at all. A long backfill
that pages through months of history at 10,000 rows/page can and will hit
429 ("Service temporarily unavailable") or 408 after a sustained run — these
are TRANSIENT despite being 4xx status codes and get their own longer retry
budget (see THROTTLE_BACKOFF_SECONDS) so a rate-limit storm doesn't burn
through the short budget meant for ordinary network blips. Every OTHER 4xx
(bad field, bad view name, bad enum value, malformed date) is PERMANENT —
retrying one just wastes time, so those raise immediately as GmcQueryError.
An out-of-range date window is HTTP 200 with zero rows, not an error, so
"empty" is always distinguishable from "broken".

A LONG BACKFILL WILL EVENTUALLY BE INTERRUPTED — design for it rather than
fighting it. `_backfill_months()` commits one calendar month at a time and,
on a transient failure it can't ride out, stops cleanly and reports exactly
which month to resume from (exit code 75, same "pause don't fail" convention
used elsewhere in this repo for long-running jobs) rather than losing
whatever it already wrote.

THE ID BRIDGE (Shopify stores only). If your feed comes from Shopify's
Google & YouTube sales channel app, `offer_id` is shaped like
`shopify_<locale>_<productId>_<variantId>` — parse_offer_id() extracts the
two numeric ids so they can join back to your product catalog. If you're on
a different platform, offer_id is whatever you set it to at feed-submission
time; adjust or drop this parsing entirely for your own id scheme.

BEST SELLERS IS A MARKET RANKING, NOT YOUR OWN SALES. It answers "what's in
demand in this category across all of Google Shopping", not "what do we
sell". Two things follow from that:
  * SNAPSHOT-ONLY, NO BACKFILL. Exactly one report_date is retrievable per
    granularity at any given time, and the API rejects a >= or BETWEEN filter
    on report_date. History exists only because each run stores a new
    snapshot — there is no way to ask for last month's report once this
    month's has replaced it.
  * A PLAIN TOP-N CUT CAN MISS EVERYTHING YOU ACTUALLY STOCK. Categories can
    hold tens of thousands of ranked products, and most retailers carry only
    a sliver of any one category's total catalog — so a bare "top 1,000"
    pull is market context, not a useful demand list for your own assortment.
    `sync_best_sellers()` therefore unions two queries per category: the top
    N by rank (market context) and every row where `inventory_status`
    indicates you carry that product, regardless of its rank. Each stored
    row records which query produced it in `pull_reason`, so a gap in the
    rank sequence is never mistaken for missing data.

COMPETITIVE VISIBILITY VARIES BY traffic_source. The `ALL` / `ADS` / `ORGANIC`
values return genuinely different competitor rank orderings for the same
category and date — store and compare within one traffic_source, never mix
them into a single ranking.

VISIBILITY PUBLISHES A FEW DAYS LATE. `competitive_visibility_*` data tends to
lag by several days relative to when it's queried — verify the actual lag for
your own account once (query a wide recent range and see where real rows stop
appearing), then set VISIBILITY_LAG_DAYS accordingly. The default `--days`
window (today minus a few days, through today) will otherwise sit entirely
inside the unpublished gap and silently return zero rows every single run —
not an error, just an empty result that looks like "nothing changed" instead
of "asked for a date that doesn't exist yet."

TRACKING SPECIFIC BRANDS ACROSS THE RANK CURVE (`--brand`). The plain top-N
best-sellers cut (see above) only shows the highest-ranked products in a
category — fine for market context, useless for checking where one specific
brand (yours, or a competitor's) sits if it's not near the top. `--brand`
issues an explicit `brand IN (...)` query against `best_sellers_brand_view`
so a brand ranked in the thousands is still retrievable directly, stored in
`gmc_best_seller_brands` with `is_tracked_brand` marking which rows matched
your list. Brand matching is exact (case-insensitive) — a substring match
would risk one brand name accidentally matching an unrelated brand that
happens to contain the same word.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time
from typing import Any, Iterator

import requests
from google.oauth2 import service_account
import google.auth.transport.requests

from warehouse import db

SCOPE = "https://www.googleapis.com/auth/content"
BASE = "https://merchantapi.googleapis.com"

# Latency is roughly flat with page size, so always ask for the ceiling.
PAGE_SIZE = 10_000

# Transient = 5xx / network / 429 / 408. Every OTHER 4xx is a permanent query
# error (bad field, bad view, bad enum, bad date) and retrying one just burns
# time — see "THROTTLING IS REAL BUT ONLY UNDER SUSTAINED LOAD" above.
MAX_TRIES = 5
BACKOFF_SECONDS = 5
THROTTLE_STATUS = (408, 429)
THROTTLE_BACKOFF_SECONDS = (30, 60, 120, 240, 300)

# A conservative default; --backfill walks back from today and stops on two
# consecutive empty months rather than assuming any particular floor, since
# retention varies by account.
BACKFILL_FLOOR = dt.date(2018, 1, 1)

# Google's own published product taxonomy id for a broad top-level category —
# see https://www.google.com/basepages/producttype/taxonomy-with-ids.en-US.txt
# for the full list. Override with --category for your own catalog.
DEFAULT_CATEGORIES = ({"id": 166, "name": "Apparel & Accessories"},)
DEFAULT_COUNTRIES = ("US",)
DEFAULT_TOP_N = 200

_OFFER_SHOPIFY = re.compile(r"^shopify_[A-Za-z0-9]+_(\d+)_(\d+)$")

FAMILIES = ("performance", "status", "pricing", "bestsellers", "visibility")

DDL = """
CREATE TABLE IF NOT EXISTS gmc_product_performance (
    date                  TEXT NOT NULL,
    marketing_method      TEXT NOT NULL,  -- 'ORGANIC' | 'ADS'
    customer_country_code TEXT NOT NULL,
    offer_id              TEXT NOT NULL,
    product_id            TEXT,
    variant_id            TEXT,
    title                 TEXT,
    brand                 TEXT,
    clicks                INTEGER,
    impressions           INTEGER,
    click_through_rate    REAL,
    conversions           REAL,
    synced_at             TEXT NOT NULL,
    PRIMARY KEY (date, marketing_method, customer_country_code, offer_id)
);
CREATE INDEX IF NOT EXISTS idx_gmc_perf_date ON gmc_product_performance(date);

CREATE TABLE IF NOT EXISTS gmc_product_status (
    gmc_id             TEXT PRIMARY KEY,
    offer_id           TEXT,
    feed_label         TEXT,
    product_id         TEXT,
    variant_id         TEXT,
    title              TEXT,
    brand              TEXT,
    price_micros       INTEGER,
    price_currency     TEXT,
    availability       TEXT,
    aggregated_status  TEXT,  -- ELIGIBLE / ELIGIBLE_LIMITED / NOT_ELIGIBLE_OR_DISAPPROVED / PENDING
    n_issues           INTEGER DEFAULT 0,
    synced_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmc_product_issues (
    gmc_id     TEXT NOT NULL,
    issue_code TEXT NOT NULL,
    severity   TEXT,
    resolution TEXT,
    offer_id   TEXT,
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (gmc_id, issue_code)
);

CREATE TABLE IF NOT EXISTS gmc_account_performance (
    date               TEXT NOT NULL,
    week_start         TEXT,
    clicks             INTEGER,
    impressions        INTEGER,
    click_through_rate REAL,
    synced_at          TEXT NOT NULL,
    PRIMARY KEY (date)
);

CREATE TABLE IF NOT EXISTS gmc_price_competitiveness (
    snapshot_date        TEXT NOT NULL,
    gmc_id               TEXT NOT NULL,
    offer_id             TEXT,
    product_id           TEXT,
    variant_id           TEXT,
    title                TEXT,
    brand                TEXT,
    report_country_code  TEXT NOT NULL,
    price_micros         INTEGER,
    price_currency       TEXT,
    benchmark_micros     INTEGER,
    benchmark_currency   TEXT,
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, gmc_id, report_country_code)
);

CREATE TABLE IF NOT EXISTS gmc_best_sellers (
    report_date          TEXT NOT NULL,
    report_granularity   TEXT NOT NULL,
    report_country_code  TEXT NOT NULL,
    report_category_id   TEXT NOT NULL,
    rank                 INTEGER NOT NULL,
    previous_rank        INTEGER,
    title                TEXT,
    brand                TEXT,
    relative_demand      TEXT,
    relative_demand_change TEXT,
    inventory_status     TEXT,
    pull_reason          TEXT,  -- 'top_n' | 'stocked' | 'riser'
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (report_date, report_granularity, report_country_code, report_category_id, rank)
);

CREATE TABLE IF NOT EXISTS gmc_best_seller_brands (
    report_date          TEXT NOT NULL,
    report_granularity   TEXT NOT NULL,
    report_country_code  TEXT NOT NULL,
    report_category_id   TEXT NOT NULL,
    rank                 INTEGER NOT NULL,
    previous_rank        INTEGER,
    brand                TEXT,
    is_tracked_brand     INTEGER DEFAULT 0,  -- 1 if `brand` matched --brand
    relative_demand      TEXT,
    previous_relative_demand TEXT,
    relative_demand_change   TEXT,
    pull_reason          TEXT,  -- 'top_n' | 'tracked_brand'
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (report_date, report_granularity, report_country_code, report_category_id, rank)
);

CREATE TABLE IF NOT EXISTS gmc_competitive_visibility (
    date                  TEXT NOT NULL,
    report_country_code   TEXT NOT NULL,
    report_category_id    TEXT NOT NULL,
    traffic_source        TEXT NOT NULL,  -- 'ALL' | 'ADS' | 'ORGANIC'
    domain                TEXT NOT NULL,
    is_your_domain        INTEGER,
    rank                  INTEGER,
    ads_organic_ratio     REAL,
    relative_visibility   REAL,
    synced_at             TEXT NOT NULL,
    PRIMARY KEY (date, report_country_code, report_category_id, traffic_source, domain)
);
"""


def ensure_schema(conn) -> None:
    """Create this connector's tables if they don't exist yet. Safe to call
    every run."""
    conn.executescript(DDL)


class GmcQueryError(RuntimeError):
    """A permanent 4xx query error — do not retry."""


class GmcTransient(RuntimeError):
    """Repeated 5xx/network/throttle failure; the caller should pause the
    current step rather than fail the whole run."""


# --------------------------------------------------------------------------- #
#  field coercion
# --------------------------------------------------------------------------- #
def as_int(v: Any) -> int | None:
    """Counts and ranks arrive as STRINGS in this API."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # guard against literal NaN on zero-impression rows


def as_date(v: Any) -> str | None:
    """{'year':2026,'month':7,'day':1} -> '2026-07-01'."""
    if not isinstance(v, dict) or not v.get("year"):
        return None
    return f"{int(v['year']):04d}-{int(v.get('month') or 1):02d}-{int(v.get('day') or 1):02d}"


def as_money(v: Any) -> tuple[int | None, str | None]:
    """{'amountMicros':'272000000','currencyCode':'USD'} -> (272000000, 'USD').

    Kept as micros + currency, never a bare float, since a multi-country feed
    can mix currencies and a plain number would be silently unsummable across
    them.
    """
    if not isinstance(v, dict):
        return None, None
    return as_int(v.get("amountMicros")), (v.get("currencyCode") or None)


def parse_offer_id(offer_id: str | None) -> tuple[str | None, str | None]:
    """offer_id -> (product_id, variant_id) for a Shopify-shaped offer_id.
    See "THE ID BRIDGE" in the module docstring. Returns (None, None) for any
    offer_id that doesn't match the expected pattern."""
    if not offer_id:
        return None, None
    m = _OFFER_SHOPIFY.match(offer_id)
    if m:
        return m.group(1), m.group(2)
    return None, None


# --------------------------------------------------------------------------- #
#  transport
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, merchant_id: str, credentials_file: str) -> None:
        self.merchant_id = merchant_id
        self._reports_url = f"{BASE}/reports/v1/accounts/{merchant_id}/reports:search"
        self._creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=[SCOPE])
        self._refresh()

    def _refresh(self) -> None:
        self._creds.refresh(google.auth.transport.requests.Request())
        self._headers = {"Authorization": f"Bearer {self._creds.token}",
                          "Content-Type": "application/json"}

    def search(self, query: str, *, page_size: int = PAGE_SIZE) -> Iterator[dict]:
        """Page a reports:search query to exhaustion, yielding UNWRAPPED rows.

        Each API result looks like {"someView": {...fields...}}; the single
        wrapper key is stripped so callers see the field dict directly.
        Streams page-by-page rather than materializing a list — a full
        catalog pull can be hundreds of thousands of rows.
        """
        one_line = " ".join(query.split())
        token = None
        refreshed = False
        while True:
            body: dict[str, Any] = {"query": one_line, "pageSize": page_size}
            if token:
                body["pageToken"] = token
            payload = None
            attempt = throttled = 0
            while payload is None:
                try:
                    r = requests.post(self._reports_url, headers=self._headers,
                                       json=body, timeout=300)
                except requests.RequestException as exc:
                    attempt += 1
                    if attempt >= MAX_TRIES:
                        raise GmcTransient(f"network: {exc}") from exc
                    time.sleep(BACKOFF_SECONDS * attempt)
                    continue
                if r.status_code == 200:
                    payload = r.json()
                    break
                if r.status_code == 401 and not refreshed:
                    # Access token aged out mid-run; re-mint once and retry.
                    refreshed = True
                    self._refresh()
                    continue
                if r.status_code in THROTTLE_STATUS:
                    if throttled >= len(THROTTLE_BACKOFF_SECONDS):
                        raise GmcTransient(
                            f"HTTP {r.status_code} still throttled after "
                            f"{throttled} waits")
                    wait = THROTTLE_BACKOFF_SECONDS[throttled]
                    throttled += 1
                    time.sleep(wait)
                    continue
                if 400 <= r.status_code < 500:
                    msg = r.json().get("error", {}).get("message", r.text[:300])
                    raise GmcQueryError(f"HTTP {r.status_code}: {msg}\nquery: {one_line}")
                attempt += 1
                if attempt >= MAX_TRIES:
                    raise GmcTransient(f"HTTP {r.status_code} after {MAX_TRIES} tries")
                time.sleep(BACKOFF_SECONDS * attempt)
            if payload is None:
                raise GmcTransient("no payload")
            for row in payload.get("results", []):
                if not row:
                    continue
                yield next(iter(row.values()))
            token = payload.get("nextPageToken")
            if not token:
                return


# --------------------------------------------------------------------------- #
#  performance
# --------------------------------------------------------------------------- #
_PERF_FIELDS = ("date, marketing_method, customer_country_code, offer_id, "
                "title, brand, clicks, impressions, click_through_rate, conversions")

_PERF_INSERT = """
INSERT OR REPLACE INTO gmc_product_performance
 (date, marketing_method, customer_country_code, offer_id, product_id,
  variant_id, title, brand, clicks, impressions, click_through_rate,
  conversions, synced_at)
VALUES (:date, :marketing_method, :customer_country_code, :offer_id, :product_id,
        :variant_id, :title, :brand, :clicks, :impressions,
        :click_through_rate, :conversions, :synced_at)
"""


def sync_performance(conn, client: Client, start: dt.date, end: dt.date) -> int:
    """The ORGANIC vs ADS product-level split."""
    now = db.now()
    query = (f"SELECT {_PERF_FIELDS} FROM product_performance_view "
             f"WHERE date BETWEEN '{start:%Y-%m-%d}' AND '{end:%Y-%m-%d}'")
    batch, total = [], 0
    for v in client.search(query):
        pid, vid = parse_offer_id(v.get("offerId"))
        batch.append({
            "date": as_date(v.get("date")),
            "marketing_method": v.get("marketingMethod"),
            "customer_country_code": v.get("customerCountryCode") or "",
            "offer_id": v.get("offerId") or "",
            "product_id": pid, "variant_id": vid,
            "title": v.get("title") or None,
            "brand": v.get("brand") or None,
            "clicks": as_int(v.get("clicks")),
            "impressions": as_int(v.get("impressions")),
            "click_through_rate": as_float(v.get("clickThroughRate")),
            "conversions": as_float(v.get("conversions")),
            "synced_at": now,
        })
        if len(batch) >= 5000:
            with conn:
                conn.executemany(_PERF_INSERT, batch)
            total += len(batch)
            batch.clear()
    if batch:
        with conn:
            conn.executemany(_PERF_INSERT, batch)
        total += len(batch)
    return total


def sync_account_performance(conn, client: Client, start: dt.date, end: dt.date) -> int:
    """Account-wide clicks/impressions on non-product-specific surfaces
    (non_product_performance_view) — Shopping surfaces that don't map to one
    product, so they can't appear in sync_performance's per-product rows.
    Same account, same date range, different scope."""
    now = db.now()
    rows = []
    for v in client.search(
            "SELECT date, week, clicks, impressions, click_through_rate "
            "FROM non_product_performance_view "
            f"WHERE date BETWEEN '{start:%Y-%m-%d}' AND '{end:%Y-%m-%d}'"):
        rows.append({
            "date": as_date(v.get("date")), "week_start": as_date(v.get("week")),
            "clicks": as_int(v.get("clicks")),
            "impressions": as_int(v.get("impressions")),
            "click_through_rate": as_float(v.get("clickThroughRate")),
            "synced_at": now,
        })
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO gmc_account_performance "
            "(date, week_start, clicks, impressions, click_through_rate, synced_at) "
            "VALUES (:date, :week_start, :clicks, :impressions, "
            ":click_through_rate, :synced_at)", rows)
    return len(rows)


# --------------------------------------------------------------------------- #
#  feed health / status
# --------------------------------------------------------------------------- #
_STATUS_FIELDS = ("id, offer_id, feed_label, title, brand, price, "
                   "availability, aggregated_reporting_context_status, item_issues")

_STATUS_INSERT = """
INSERT OR REPLACE INTO gmc_product_status
 (gmc_id, offer_id, feed_label, product_id, variant_id, title, brand,
  price_micros, price_currency, availability, aggregated_status, n_issues,
  synced_at)
VALUES (:gmc_id, :offer_id, :feed_label, :product_id, :variant_id, :title,
        :brand, :price_micros, :price_currency, :availability,
        :aggregated_status, :n_issues, :synced_at)
"""

_ISSUE_INSERT = """
INSERT OR REPLACE INTO gmc_product_issues
 (gmc_id, issue_code, severity, resolution, offer_id, synced_at)
VALUES (:gmc_id, :issue_code, :severity, :resolution, :offer_id, :synced_at)
"""


def _flatten_issues(gmc_id: str, offer_id: str, issues: list, now: str) -> list[dict]:
    """item_issues -> one row per issue code.

    Shape: [{type: {code}, severity: {aggregatedSeverity}, resolution}].
    """
    out = []
    for iss in issues or []:
        code = ((iss.get("type") or {}).get("code")) or "unknown"
        sev = (iss.get("severity") or {}).get("aggregatedSeverity")
        out.append({
            "gmc_id": gmc_id, "issue_code": code, "severity": sev,
            "resolution": iss.get("resolution"), "offer_id": offer_id,
            "synced_at": now,
        })
    return out


def sync_status(conn, client: Client) -> int:
    """Full catalog eligibility state + flattened issues.

    gmc_product_status is UPSERTED current state, not per-day history — a
    product's feed status can change hour to hour and there is no value in
    storing every intermediate state, only the latest one. Rows not seen in
    THIS pass are pruned afterward so a delisted product doesn't sit here
    reading "disapproved" forever.
    """
    now = db.now()
    query = f"SELECT {_STATUS_FIELDS} FROM product_view"

    sbatch: list[dict] = []
    ibatch: list[dict] = []
    total = 0

    def flush() -> None:
        nonlocal total
        if sbatch:
            with conn:
                conn.executemany(_STATUS_INSERT, sbatch)
                if ibatch:
                    conn.executemany(_ISSUE_INSERT, ibatch)
            total += len(sbatch)
            sbatch.clear()
            ibatch.clear()

    for v in client.search(query):
        gmc_id = v.get("id") or ""
        offer_id = v.get("offerId") or ""
        pid, vid = parse_offer_id(offer_id)
        micros, cur = as_money(v.get("price"))
        issues = v.get("itemIssues") or []
        sbatch.append({
            "gmc_id": gmc_id, "offer_id": offer_id,
            "feed_label": v.get("feedLabel") or "",
            "product_id": pid, "variant_id": vid,
            "title": v.get("title") or None, "brand": v.get("brand") or None,
            "price_micros": micros, "price_currency": cur,
            "availability": v.get("availability"),
            "aggregated_status": v.get("aggregatedReportingContextStatus"),
            "n_issues": len(issues), "synced_at": now,
        })
        ibatch.extend(_flatten_issues(gmc_id, offer_id, issues, now))
        if len(sbatch) >= 5000:
            flush()
    flush()

    # Prune what the feed no longer contains. Safe because it only runs after
    # a COMPLETE pass: an exception above propagates and skips this, so a
    # partial run can never wipe rows that simply weren't reached yet.
    with conn:
        conn.execute("DELETE FROM gmc_product_status WHERE synced_at < ?", (now,))
        conn.execute("DELETE FROM gmc_product_issues WHERE synced_at < ?", (now,))
    return total


# --------------------------------------------------------------------------- #
#  pricing
# --------------------------------------------------------------------------- #
_PRICE_INSERT = """
INSERT OR REPLACE INTO gmc_price_competitiveness
 (snapshot_date, gmc_id, offer_id, product_id, variant_id, title, brand,
  report_country_code, price_micros, price_currency, benchmark_micros,
  benchmark_currency, synced_at)
VALUES (:snapshot_date, :gmc_id, :offer_id, :product_id, :variant_id, :title,
        :brand, :report_country_code, :price_micros, :price_currency,
        :benchmark_micros, :benchmark_currency, :synced_at)
"""


def sync_pricing(conn, client: Client) -> int:
    """Your price vs Google's crowd-sourced benchmark price.

    Coverage is typically narrow — this view only has a benchmark for
    products Google has enough market data on, so an absent product does NOT
    mean "priced at market", it means no benchmark exists for it. This is a
    snapshot view with no date of its own, so the daily run is what builds
    history (same pattern as any other current-state-only API in this repo).
    """
    now = db.now()
    today = dt.date.today().isoformat()
    rows = []
    for v in client.search(
            "SELECT id, offer_id, title, brand, price, benchmark_price, "
            "report_country_code FROM price_competitiveness_product_view"):
        pid, vid = parse_offer_id(v.get("offerId"))
        pm, pc = as_money(v.get("price"))
        bm, bc = as_money(v.get("benchmarkPrice"))
        rows.append({
            "snapshot_date": today, "gmc_id": v.get("id") or "",
            "offer_id": v.get("offerId"), "product_id": pid, "variant_id": vid,
            "title": v.get("title") or None, "brand": v.get("brand") or None,
            "report_country_code": v.get("reportCountryCode") or "",
            "price_micros": pm, "price_currency": pc,
            "benchmark_micros": bm, "benchmark_currency": bc,
            "synced_at": now,
        })
    with conn:
        conn.executemany(_PRICE_INSERT, rows)
    return len(rows)


# --------------------------------------------------------------------------- #
#  best sellers
# --------------------------------------------------------------------------- #
_BS_SELECT = ("report_granularity, report_date, report_country_code, "
              "report_category_id, rank, previous_rank, title, brand, "
              "relative_demand, relative_demand_change, inventory_status")

_REASON_RANK = {"stocked": 3, "riser": 2, "top_n": 1, "tracked_brand": 3}

_BS_INSERT = """
INSERT OR REPLACE INTO gmc_best_sellers
 (report_date, report_granularity, report_country_code, report_category_id,
  rank, previous_rank, title, brand, relative_demand, relative_demand_change,
  inventory_status, pull_reason, synced_at)
VALUES (:report_date, :report_granularity, :report_country_code,
        :report_category_id, :rank, :previous_rank, :title, :brand,
        :relative_demand, :relative_demand_change, :inventory_status,
        :pull_reason, :synced_at)
"""


def sync_best_sellers(conn, client: Client, categories, countries,
                       top_n: int = DEFAULT_TOP_N,
                       granularities: tuple[str, ...] = ("WEEKLY", "MONTHLY")) -> int:
    """Market best sellers, as the UNION of three targeted queries per
    category — see "A PLAIN TOP-N CUT CAN MISS EVERYTHING YOU ACTUALLY STOCK"
    above. The third variant (rows with rising relative demand) surfaces
    products gaining momentum even if they're not yet ranked near the top or
    in your own inventory — an early signal worth watching."""
    now = db.now()
    total = 0
    for country in countries:
        for gran in granularities:
            for cat in categories:
                cid = cat["id"]
                base = (f"report_country_code = '{country}' "
                        f"AND report_granularity = '{gran}' "
                        f"AND report_category_id = {cid}")
                variants = (
                    ("top_n", f"SELECT {_BS_SELECT} FROM "
                              f"best_sellers_product_cluster_view WHERE {base} "
                              f"ORDER BY rank ASC LIMIT {top_n}"),
                    ("stocked", f"SELECT {_BS_SELECT} FROM "
                                f"best_sellers_product_cluster_view WHERE {base} "
                                f"AND inventory_status != 'NOT_IN_INVENTORY'"),
                    ("riser", f"SELECT {_BS_SELECT} FROM "
                              f"best_sellers_product_cluster_view WHERE {base} "
                              f"AND relative_demand_change = 'RISER'"),
                )
                merged: dict[tuple, dict] = {}
                for reason, query in variants:
                    try:
                        for v in client.search(query):
                            key = (as_date(v.get("reportDate")),
                                   v.get("reportGranularity"),
                                   v.get("reportCountryCode"),
                                   str(v.get("reportCategoryId")),
                                   as_int(v.get("rank")))
                            prev = merged.get(key)
                            if prev and _REASON_RANK[prev["pull_reason"]] >= _REASON_RANK[reason]:
                                continue
                            merged[key] = {
                                "report_date": key[0], "report_granularity": key[1],
                                "report_country_code": key[2],
                                "report_category_id": key[3], "rank": key[4],
                                "previous_rank": as_int(v.get("previousRank")),
                                "title": v.get("title") or None,
                                "brand": v.get("brand") or None,
                                "relative_demand": v.get("relativeDemand"),
                                "relative_demand_change": v.get("relativeDemandChange"),
                                "inventory_status": v.get("inventoryStatus"),
                                "pull_reason": reason, "synced_at": now,
                            }
                    except GmcQueryError as exc:
                        print(f"    [{gran} {country} cat {cid} {reason}] "
                              f"skipped: {exc}".replace("\n", " ")[:200])
                rows = [r for r in merged.values() if r["rank"] is not None]
                if rows:
                    with conn:
                        conn.executemany(_BS_INSERT, rows)
                    total += len(rows)
    return total


def _quoted_list(values: list[str]) -> str:
    """['A', "O'Brien"] -> "'A','O''Brien'" for a SQL IN (...) clause."""
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


_BSB_INSERT = """
INSERT OR REPLACE INTO gmc_best_seller_brands
 (report_date, report_granularity, report_country_code, report_category_id,
  rank, previous_rank, brand, is_tracked_brand, relative_demand,
  previous_relative_demand, relative_demand_change, pull_reason, synced_at)
VALUES (:report_date, :report_granularity, :report_country_code,
        :report_category_id, :rank, :previous_rank, :brand, :is_tracked_brand,
        :relative_demand, :previous_relative_demand, :relative_demand_change,
        :pull_reason, :synced_at)
"""


def sync_best_seller_brands(conn, client: Client, categories, countries, brands,
                             top_n: int = DEFAULT_TOP_N,
                             granularities: tuple[str, ...] = ("WEEKLY", "MONTHLY")) -> int:
    """Brand-level market demand. `brands` (from --brand, case-insensitive) is
    fetched with an EXPLICIT `brand IN (...)` query, because a specific brand
    you care about can sit far outside any reasonable top-N cutoff (a small
    or niche brand can rank in the thousands within a broad category)."""
    now = db.now()
    tracked = brands or []
    tracked_lower = {b.lower() for b in tracked}
    sel = ("report_granularity, report_date, report_country_code, "
           "report_category_id, rank, previous_rank, brand, relative_demand, "
           "previous_relative_demand, relative_demand_change")
    total = 0
    for country in countries:
        for gran in granularities:
            for cat in categories:
                cid = cat["id"]
                base = (f"report_country_code = '{country}' "
                        f"AND report_granularity = '{gran}' "
                        f"AND report_category_id = {cid}")
                variants = [
                    ("top_n", f"SELECT {sel} FROM best_sellers_brand_view "
                              f"WHERE {base} ORDER BY rank ASC LIMIT {top_n}"),
                ]
                if tracked:
                    variants.append(
                        ("tracked_brand",
                         f"SELECT {sel} FROM best_sellers_brand_view "
                         f"WHERE {base} AND brand IN ({_quoted_list(tracked)})"))
                merged: dict[tuple, dict] = {}
                for reason, query in variants:
                    try:
                        for v in client.search(query):
                            key = (as_date(v.get("reportDate")),
                                   v.get("reportGranularity"),
                                   v.get("reportCountryCode"),
                                   str(v.get("reportCategoryId")),
                                   as_int(v.get("rank")))
                            prev = merged.get(key)
                            if prev and _REASON_RANK[prev["pull_reason"]] >= \
                                    _REASON_RANK[reason]:
                                continue
                            brand = v.get("brand") or None
                            merged[key] = {
                                "report_date": key[0],
                                "report_granularity": key[1],
                                "report_country_code": key[2],
                                "report_category_id": key[3], "rank": key[4],
                                "previous_rank": as_int(v.get("previousRank")),
                                "brand": brand,
                                "is_tracked_brand": int(
                                    (brand or "").lower() in tracked_lower),
                                "relative_demand": v.get("relativeDemand"),
                                "previous_relative_demand":
                                    v.get("previousRelativeDemand"),
                                "relative_demand_change":
                                    v.get("relativeDemandChange"),
                                "pull_reason": reason, "synced_at": now,
                            }
                    except GmcQueryError as exc:
                        print(f"    [brands {gran} cat {cid} {reason}] "
                              f"skipped: {exc}".replace("\n", " ")[:200])
                rows = [r for r in merged.values() if r["rank"] is not None]
                if rows:
                    with conn:
                        conn.executemany(_BSB_INSERT, rows)
                    total += len(rows)
    return total


def _backfill_months(client: Client, conn, start_from: dt.date | None = None
                      ) -> tuple[int, dt.date | None]:
    """Walk performance history backwards a month at a time until it runs
    dry or is interrupted. Returns (rows, resume_from)."""
    total, empty = 0, 0
    cursor = (start_from or dt.date.today()).replace(day=1)
    while cursor >= BACKFILL_FLOOR and empty < 2:
        nxt = (cursor + dt.timedelta(days=31)).replace(day=1)
        end = min(nxt - dt.timedelta(days=1), dt.date.today())
        try:
            n = sync_performance(conn, client, cursor, end)
        except GmcTransient as exc:
            print(f"  backfill PAUSED at {cursor:%Y-%m} ({exc})")
            print(f"  resume with: merchant_center_sync.py --only performance "
                  f"--backfill --start {cursor:%Y-%m-%d}")
            return total, cursor
        total += n
        empty = empty + 1 if n == 0 else 0
        cursor = (cursor - dt.timedelta(days=1)).replace(day=1)
    return total, None


# --------------------------------------------------------------------------- #
#  competitive visibility
# --------------------------------------------------------------------------- #
TRAFFIC_SOURCES = ("ALL", "ADS", "ORGANIC")

# competitive_visibility_*_view publishes several days late (see "VISIBILITY
# PUBLISHES A FEW DAYS LATE" in the module docstring — verify the actual lag
# for your own account and adjust). The default --days window otherwise sits
# entirely inside the unpublished gap and silently returns zero rows forever.
VISIBILITY_LAG_DAYS = 4


def sync_visibility(conn, client: Client, categories, countries,
                     start: dt.date, end: dt.date) -> int:
    """Competitor ranks + our own visibility trend for each category."""
    now = db.now()
    window = f"date BETWEEN '{start:%Y-%m-%d}' AND '{end:%Y-%m-%d}'"
    total = 0
    for country in countries:
        for cat in categories:
            cid = cat["id"]
            for src in TRAFFIC_SOURCES:
                base = (f"report_country_code = '{country}' "
                        f"AND report_category_id = {cid} "
                        f"AND traffic_source = '{src}' AND {window}")
                rows = []
                try:
                    for v in client.search(
                            "SELECT report_category_id, traffic_source, date, "
                            "domain, is_your_domain, rank, ads_organic_ratio, "
                            "relative_visibility, report_country_code "
                            f"FROM competitive_visibility_competitor_view WHERE {base}"):
                        rows.append({
                            "date": as_date(v.get("date")),
                            "report_country_code": v.get("reportCountryCode") or "",
                            "report_category_id": str(v.get("reportCategoryId")),
                            "traffic_source": v.get("trafficSource") or "",
                            "domain": v.get("domain") or "",
                            "is_your_domain": int(bool(v.get("isYourDomain"))),
                            "rank": as_int(v.get("rank")),
                            "ads_organic_ratio": as_float(v.get("adsOrganicRatio")),
                            "relative_visibility": as_float(v.get("relativeVisibility")),
                            "synced_at": now,
                        })
                except GmcQueryError as exc:
                    print(f"    [vis {country} cat {cid} {src}] skipped: "
                          f"{exc}".replace("\n", " ")[:160])
                if rows:
                    with conn:
                        conn.executemany(
                            "INSERT OR REPLACE INTO gmc_competitive_visibility "
                            "(date, report_country_code, report_category_id, "
                            " traffic_source, domain, is_your_domain, rank, "
                            " ads_organic_ratio, relative_visibility, synced_at) "
                            "VALUES (:date, :report_country_code, "
                            ":report_category_id, :traffic_source, :domain, "
                            ":is_your_domain, :rank, :ads_organic_ratio, "
                            ":relative_visibility, :synced_at)", rows)
                    total += len(rows)
    return total


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def _parse_category(spec: str) -> dict:
    """'166' or '166:Apparel & Accessories' -> {"id": 166, "name": "..."}."""
    if ":" in spec:
        cid, name = spec.split(":", 1)
    else:
        cid, name = spec, spec
    return {"id": int(cid), "name": name}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=3,
                    help="performance/visibility window, days back from today")
    p.add_argument("--start", help="explicit window start YYYY-MM-DD")
    p.add_argument("--end", help="explicit window end YYYY-MM-DD")
    p.add_argument("--only", choices=FAMILIES, action="append",
                    help="limit to one or more families (repeatable)")
    p.add_argument("--backfill", action="store_true",
                    help="walk performance history back until it runs dry")
    p.add_argument("--category", action="append",
                    help="'id' or 'id:Name' (repeatable; default: %s)" % (DEFAULT_CATEGORIES[0]["id"]))
    p.add_argument("--country", action="append",
                    help="ISO country code (repeatable; default: %s)" % (DEFAULT_COUNTRIES[0]))
    p.add_argument("--brand", action="append",
                    help="brand name to track explicitly in bestsellers, even "
                         "outside the top-n cutoff (repeatable)")
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    args = p.parse_args()

    merchant_id = os.environ.get("GMC_MERCHANT_ID")
    credentials_file = os.environ.get("GMC_CREDENTIALS_FILE")
    if not merchant_id or not credentials_file:
        raise SystemExit(
            "merchant_center_sync: set GMC_MERCHANT_ID and GMC_CREDENTIALS_FILE "
            "in .env first (see the module docstring for the one-time "
            "registerGcp setup step — this is the part everyone misses)."
        )

    today = dt.date.today()
    end = dt.date.fromisoformat(args.end) if args.end else today
    start = (dt.date.fromisoformat(args.start) if args.start
             else end - dt.timedelta(days=max(args.days, 1) - 1))
    families = tuple(args.only) if args.only else FAMILIES
    categories = [_parse_category(c) for c in args.category] if args.category else list(DEFAULT_CATEGORIES)
    countries = args.country or list(DEFAULT_COUNTRIES)
    brands = args.brand or []

    db.init_db()
    conn = db.connect()
    ensure_schema(conn)
    client = Client(merchant_id, credentials_file)
    print(f"merchant_center: account {merchant_id}  window {start} .. {end}")
    failed = paused = False

    if "performance" in families:
        started = db.now()
        try:
            resume = None
            if args.backfill:
                n, resume = _backfill_months(
                    client, conn, dt.date.fromisoformat(args.start) if args.start else None)
            else:
                n = sync_performance(conn, client, start, end)
                n += sync_account_performance(conn, client, start, end)
            if resume:
                paused = True
                db.log_sync("gmc_performance", started, n, "degraded",
                            f"backfill paused (throttled); resume --start {resume}")
            else:
                db.log_sync("gmc_performance", started, n, "ok")
            print(f"  performance: {n:,} rows")
        except Exception as exc:  # noqa: BLE001 — logged and continued
            failed = True
            print(f"  performance FAILED: {exc}")
            db.log_sync("gmc_performance", started, 0, "error", str(exc))

    if "status" in families:
        started = db.now()
        try:
            n = sync_status(conn, client)
            db.log_sync("gmc_status", started, n, "ok")
            print(f"  status: {n:,} rows")
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"  status FAILED: {exc}")
            db.log_sync("gmc_status", started, 0, "error", str(exc))

    if "pricing" in families:
        started = db.now()
        try:
            n = sync_pricing(conn, client)
            db.log_sync("gmc_pricing", started, n, "ok")
            print(f"  pricing: {n:,} rows")
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"  pricing FAILED: {exc}")
            db.log_sync("gmc_pricing", started, 0, "error", str(exc))

    if "bestsellers" in families:
        started = db.now()
        try:
            n = sync_best_sellers(conn, client, categories, countries, args.top_n)
            n += sync_best_seller_brands(conn, client, categories, countries, brands, args.top_n)
            db.log_sync("gmc_bestsellers", started, n, "ok")
            print(f"  bestsellers: {n:,} rows")
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"  bestsellers FAILED: {exc}")
            db.log_sync("gmc_bestsellers", started, 0, "error", str(exc))

    if "visibility" in families:
        started = db.now()
        try:
            if args.start or args.end:
                vis_start, vis_end = start, end
            else:
                vis_end = today - dt.timedelta(days=VISIBILITY_LAG_DAYS)
                vis_start = vis_end - dt.timedelta(days=max(args.days, 1) - 1)
                print(f"  visibility window {vis_start} .. {vis_end} "
                      f"(lag-shifted {VISIBILITY_LAG_DAYS}d — see VISIBILITY_LAG_DAYS)")
            n = sync_visibility(conn, client, categories, countries, vis_start, vis_end)
            db.log_sync("gmc_visibility", started, n, "ok")
            print(f"  visibility: {n:,} rows")
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"  visibility FAILED: {exc}")
            db.log_sync("gmc_visibility", started, 0, "error", str(exc))

    conn.close()
    if failed:
        sys.exit(1)
    if paused:
        sys.exit(75)  # pause, not fail — see _backfill_months()


if __name__ == "__main__":
    main()
