r"""
Algolia -> on-site collection-grid PLACEMENT + on-site search/browse ENGAGEMENT.

WHEN THIS APPLIES. Only relevant if your storefront's collection/category
pages are rendered CLIENT-SIDE against Algolia — e.g. a Shopify theme (or any
storefront) using react-instantsearch or a similar library to query Algolia
directly from the browser, rather than a server-rendered page. To check:
open a collection page's browser dev tools > Network tab and look for
requests to `*.algolia.net` or `*.algolianet.com`. If your storefront is
server-rendered instead, this connector has nothing to read — Algolia never
sees the request that actually built the page a shopper saw.

WHY THIS EXISTS. Most warehouses have no record of WHERE a product sits on a
collection/category page, so "this product sells well" and "this product
happens to sit at slot 3 of a heavily-trafficked collection" are
indistinguishable. That matters most when merchandising can hand-pin products
into specific slots via rules that get edited same-day — the placement a
product enjoyed on a given day is not recoverable after the fact unless it's
snapshotted daily.

When your storefront IS client-side-rendered against Algolia, Algolia turns
out to answer both halves of that question:
  * PLACEMENT (Search API)     — where each product sits, per tracked
    collection, per day.
  * ENGAGEMENT (Analytics API) — impressions/clicks/add-to-carts per product,
    plus the EMPIRICAL click-decay-by-position curve. That curve is the
    reason the analytics half is worth having: "expected engagement for slot
    N" can be MEASURED from your own account's data rather than assumed from
    a generic decay curve.

THE STOREFRONT IS ALGOLIA (when this applies). Querying the same index with
the same filter the storefront uses returns the same product order a shopper
actually saw — exact, not scraped or estimated. A large majority of an
Algolia index's traffic on a storefront like this is typically the EMPTY
query filtered by a `collections`-style facet attribute — i.e. the browse
grid, not a search box — which is what makes it the right instrument for a
placement question in the first place. Run `--probe` to see your own
account's query/traffic mix before trusting this connector's assumptions
about it.

=============================== THE TRAPS ===============================

(1) !! "CONVERSION" MAY MEAN ADD-TO-CART, NOT PURCHASE !! Many storefronts
    send Algolia click/add-to-cart events but never wire purchase events —
    check with `--probe`, which reports whether any purchases have actually
    been recorded. When purchase events are absent, Algolia's own
    `conversionCount`/`conversionRate` fields are, in practice, exactly the
    add-to-cart count/rate (Algolia's terminology conflates the two when only
    add-to-cart is instrumented). Consequence: THE PURCHASE SIDE MUST COME
    FROM YOUR OWN SALES DATA (an order feed, a GA4/analytics export) — Algolia
    can say a product was seen and clicked, but not that it sold. Columns
    here are therefore named `add_to_cart_*`, and there is deliberately NO
    column named "conversions" that would invite reading it as revenue.
    `purchase_count`/`purchase_rate` ARE captured (zeros, if unwired) so that
    the moment purchase events get wired on the storefront side, this
    connector's revenue capture self-enables with no code change — see
    `purchase_events_present()`.

(2) !! ENGAGEMENT CANNOT BE ATTRIBUTED TO A COLLECTION !! Unless your
    storefront explicitly tags searches with Algolia's `analyticsTags`
    feature, a product's impressions are POOLED across every context it
    appeared in (every collection it's shown on, plus text search), while its
    placement rows are recorded PER collection. A product on several
    collection pages has several placement rows and only ONE engagement row.
    Any join between the two is therefore many-to-one — do NOT sum engagement
    across a product's collections, that multiplies its real impressions by
    however many collections it's in. There is no per-collection engagement
    breakdown to go find unless you add analyticsTags on the storefront side.

(3) ANALYTICS RETENTION IS SHORT AND HARD-ENFORCED (90 days on Algolia's
    Analytics API as of this writing — verify against current Algolia docs,
    they've changed this before). A request for an older `startDate` 400s
    outright; there is no analytics backfill possible past that window. The
    placement side is worse: Algolia exposes no ranking history AT ALL, so
    placement can only ever accrue forward from whenever you start running
    this. Both are accrue-forward feeds — the same shape as an inventory or
    sales-rank snapshot table — so a sync that silently stops for an extended
    period loses that window PERMANENTLY. That's the whole argument for
    running this daily rather than backfilling it "later."

(4) THE ANALYTICS "hit" ID IS OFTEN A VARIANT ID, NOT A PRODUCT ID, AND THE
    SEARCH INDEX MAY NOT BE ABLE TO RESOLVE IT. Depending on how your catalog
    is indexed, each analytics `hit` objectID can be a per-variant id rather
    than a product id. Resolving it by re-querying the search index can be
    silently WRONG if the index has Algolia's `distinct` feature configured:
    only one representative variant per product is ever returned by a query,
    so a non-representative sibling variant looks like it doesn't exist at
    all, even though it has real impressions. RESOLVE VIA YOUR OWN PRODUCT
    CATALOG DATA (e.g. a Shopify product/variant table, if you sync one),
    never by re-querying the search index for an objectID. Because that
    resolution genuinely depends on what catalog data you have elsewhere,
    it's deliberately left to consumers rather than done at ingest — this
    connector reads NOTHING else from the warehouse, so it has no
    dependency on any other connector or table.

(5) A HIT'S `position` FIELD MAY NOT BE ITS GRID POSITION. On a Shopify-
    backed Algolia index in particular, records commonly carry a `position`
    attribute that means something else entirely — the variant's ordinal
    within its OWN product's variant list (values like 3, 7), not where it
    sits in a search result. Grid rank is the ENUMERATION ORDER of the
    result set you fetch, not any field on the record itself. Storing a
    record's own `position` field as if it were grid rank is an easy,
    plausible-looking mistake — check what your own account's schema means
    by that field before trusting it.

(6) `/2/clicks/positions` (the position-decay histogram) is NOT guaranteed to
    cross-foot `/2/clicks/clickThroughRate` (the account's overall click
    total) — the two endpoints can measure different underlying click
    populations (e.g. the histogram may not be restricted to tracked
    searches the way the CTR endpoint is). Treat the position curve as a
    SHAPE — the relative decay across slots — never as an absolute click
    total, and don't reconcile it against a different endpoint's number.

(7) USUALLY ONLY ONE INDEX ACTUALLY HAS TRAFFIC. A real Algolia application
    can have dozens of indices — locale variants, "Sort by" replicas
    (`_price_asc`, `_price_desc`, etc.), legacy/abandoned generations from a
    prior integration — and typically only one of them (the plain,
    unsuffixed index behind the default "Featured" sort) is what the grid
    actually shows by default and is worth tracking. `--probe` reports
    search volume per index it's told about; check that before assuming a
    plausible-looking index name is the right one.

WHAT THIS IS NOT. This connector captures the DECLARED default-sort
placement. A shopper who applies a filter, picks a different sort, or is
served a personalized or A/B-tested variant sees a different order than what
gets recorded here. Whether your storefront runs personalization or A/B
testing on this grid is something you need to confirm independently — nothing
observed by this connector proves either way. Treat placement as a baseline,
not per-session truth.

CREDENTIALS. The Algolia app id and a SEARCH-only API key are commonly public
by design on Algolia-backed storefronts — the storefront ships them to every
visitor's browser already, so there's usually nothing sensitive about
`ALGOLIA_APP_ID`/`ALGOLIA_SEARCH_KEY` beyond identifying your own
application (see your storefront's own network requests to find them). The
ANALYTICS key is different: it's a privileged dashboard key that reads all
analytics for the whole application, and belongs ONLY in `.env`, never in any
client-facing code. If `ALGOLIA_ANALYTICS_KEY` is unset, the analytics grains
skip cleanly and placement capture still runs.

USAGE:
  algolia_sync.py                            # daily: placement + 3 days analytics
  algolia_sync.py --days 30
  algolia_sync.py --start 2026-08-01 --end 2026-08-24
  algolia_sync.py --only placement
  algolia_sync.py --only hits --only positions
  algolia_sync.py --collections dresses,halloween
  algolia_sync.py --probe                    # report reachability, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from collections.abc import Callable
from dotenv import load_dotenv

from warehouse import db as warehouse_db

load_dotenv()

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WAREHOUSE_DB", ROOT / "warehouse.db"))
CONFIG_FILE = ROOT / "algolia_collections.yaml"

# The app id and search key are commonly public-by-construction on an
# Algolia-backed storefront (shipped to every visitor's browser), but there
# is no safe universal default to fall back to — they identify YOUR Algolia
# application. See .env.example for how to find your own.
APP_ID = os.environ.get("ALGOLIA_APP_ID", "")
SEARCH_KEY = os.environ.get("ALGOLIA_SEARCH_KEY", "")
INDEX = os.environ.get("ALGOLIA_INDEX", "")
# NOT public. Unset -> analytics grains skip, placement still runs.
ANALYTICS_KEY = os.environ.get("ALGOLIA_ANALYTICS_KEY", "")

REQUIRED_ENV = ("ALGOLIA_APP_ID", "ALGOLIA_SEARCH_KEY", "ALGOLIA_INDEX")


def check_required_env() -> None:
    """Raise a clear SystemExit (not a KeyError deep in a request) when
    credentials are missing, so a misconfigured .env fails fast and legibly."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


SEARCH_HOST = f"{APP_ID.lower()}-dsn.algolia.net" if APP_ID else ""
ANALYTICS_HOST = "analytics.algolia.com"

HITS_PER_PAGE = 1000          # Algolia's per-request maximum for the Search API
ANALYTICS_PAGE_LIMIT = 1000   # 422 "limit must be at most 1000" above this
MAX_ANALYTICS_PAGES = 60      # a guard against an unexpectedly deep result set
SEARCH_QUERY_CAP = 2000       # per day; see store_searches() for why it is capped

# Retention is enforced server-side (90 days as of this writing — verify
# against current Algolia docs). Stay a day inside it so a run that straddles
# midnight UTC does not 400 on its own start date.
RETENTION_DAYS = 89

GRAINS = ("placement", "hits", "positions", "searches", "daily")

# No universal default makes sense here — collection handles are entirely
# storefront-specific. See algolia_collections.yaml (optional, ships with
# placeholder examples) or pass --collections explicitly.
DEFAULT_COLLECTIONS: list[str] = []

# Transient per Algolia's own guidance plus general HTTP convention; every
# other 4xx (bad field, bad date, out of retention) is PERMANENT and must not
# burn a backoff.
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 5

DDL = """
-- Daily on-site collection-grid placement. One row per product per collection
-- per day. `position` is the 1-based grid rank as a shopper sees it on the
-- default "Featured" sort -- NOT any `position` field a record itself might
-- carry (see trap (5) in the module docstring). CANNOT be backfilled:
-- Algolia exposes no ranking history.
CREATE TABLE IF NOT EXISTS collection_placement (
    snapshot_date       TEXT    NOT NULL,
    collection          TEXT    NOT NULL,
    position            INTEGER NOT NULL,
    object_id           TEXT,              -- the index's objectID (often a
                                           -- per-variant id -- see trap (4))
    product_id          TEXT,              -- product id, if the record carries one
    sku                 TEXT,              -- SKU, if the record carries one
    handle              TEXT,
    title               TEXT,
    vendor              TEXT,
    price                REAL,
    compare_at_price    REAL,
    inventory_available INTEGER,
    published_at        TEXT,
    collection_size     INTEGER,           -- grid length that day; needed to
                                           -- bucket position as a percentile
    synced_at           TEXT    NOT NULL,
    PRIMARY KEY (snapshot_date, collection, position)
);
CREATE INDEX IF NOT EXISTS idx_colplace_date    ON collection_placement(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_colplace_product ON collection_placement(product_id);
CREATE INDEX IF NOT EXISTS idx_colplace_coll    ON collection_placement(collection, snapshot_date);

-- Per-object per-day on-site engagement. object_id is whatever the Algolia
-- record's objectID is (often a variant id -- see trap (4); resolve it via
-- your own product catalog data, never by re-querying the search index).
-- POOLED across every collection and query the object appeared in (trap 2).
-- add_to_cart_* is what Algolia may call "conversion" when purchase events
-- aren't wired (trap 1); purchase_* is 0 until they are.
CREATE TABLE IF NOT EXISTS algolia_product_engagement (
    date                TEXT NOT NULL,
    object_id           TEXT NOT NULL,
    impressions         INTEGER,   -- times returned in a result set
    tracked_impressions INTEGER,   -- subset on click-analytics-tracked searches
    click_count         INTEGER,
    click_through_rate  REAL,      -- RATIO: average it, never SUM
    add_to_cart_count   INTEGER,
    add_to_cart_rate    REAL,      -- RATIO
    purchase_count      INTEGER,   -- 0 until purchase events are wired
    purchase_rate       REAL,      -- RATIO
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (date, object_id)
);
CREATE INDEX IF NOT EXISTS idx_algeng_date ON algolia_product_engagement(date);

-- Empirical click-decay curve: how many clicks landed at each grid slot.
-- A SHAPE, not an absolute total -- does not necessarily cross-foot the CTR
-- endpoint (trap 6). position_to = -1 means "and beyond" (the deepest bucket).
CREATE TABLE IF NOT EXISTS algolia_click_positions (
    date          TEXT    NOT NULL,
    position_from INTEGER NOT NULL,
    position_to   INTEGER NOT NULL,
    click_count   INTEGER,
    synced_at     TEXT    NOT NULL,
    PRIMARY KEY (date, position_from)
);

-- Top queries per day. TRUNCATED to the top SEARCH_QUERY_CAP by volume --
-- the tail on any real account is typically thousands of single-count typos.
-- The empty query is usually the browse grid and is by far the largest row;
-- it is kept deliberately.
CREATE TABLE IF NOT EXISTS algolia_searches (
    date                   TEXT NOT NULL,
    query                  TEXT NOT NULL,   -- '' = the browse grid, on most accounts
    search_count           INTEGER,
    nb_hits                INTEGER,
    click_through_rate     REAL,            -- RATIO
    average_click_position REAL,            -- RATIO
    conversion_rate        REAL,            -- RATIO; may be add-to-cart rate (trap 1)
    click_count            INTEGER,
    conversion_count       INTEGER,         -- may be add-to-cart count (trap 1)
    synced_at              TEXT NOT NULL,
    PRIMARY KEY (date, query)
);

-- Account-level daily series. Cheap, and the only place the site-wide
-- no-result / no-click rates live.
CREATE TABLE IF NOT EXISTS algolia_daily (
    date                   TEXT NOT NULL,
    search_count           INTEGER,
    user_count             INTEGER,
    tracked_search_count   INTEGER,
    click_count            INTEGER,
    click_through_rate     REAL,   -- RATIO
    average_click_position REAL,   -- RATIO
    add_to_cart_count      INTEGER,
    add_to_cart_rate       REAL,   -- RATIO
    purchase_count         INTEGER,
    purchase_rate          REAL,   -- RATIO
    no_result_count        INTEGER,
    no_result_rate         REAL,   -- RATIO
    no_click_count         INTEGER,
    no_click_rate          REAL,   -- RATIO
    synced_at              TEXT NOT NULL,
    PRIMARY KEY (date)
);
"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class AlgoliaError(RuntimeError):
    """Permanent failure -- do not retry."""


class AlgoliaTransient(RuntimeError):
    """Transient failure that survived the retry budget."""


def _request(url: str, key: str, *, payload: dict | None = None, label: str = "") -> dict:
    """One Algolia call with retry on transient status only."""
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "X-Algolia-Application-Id": APP_ID,
        "X-Algolia-API-Key": key,
        # Algolia's edge rejects some default agents; send an explicit one.
        "User-Agent": "ecomm-mcp-server-algolia-sync/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    delay = 2.0
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code not in TRANSIENT_STATUS:
                # Bad field / bad date / out of retention -- retrying is pure waste.
                raise AlgoliaError(f"{label or url}: HTTP {exc.code} {detail}") from exc
            if attempt == MAX_RETRIES - 1:
                raise AlgoliaTransient(f"{label or url}: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise AlgoliaTransient(f"{label or url}: {exc}") from exc
        time.sleep(delay)
        delay *= 2
    raise AlgoliaTransient(label or url)


def search_query(payload: dict) -> dict:
    url = f"https://{SEARCH_HOST}/1/indexes/{urllib.parse.quote(INDEX)}/query"
    return _request(url, SEARCH_KEY, payload=payload, label="search")


def analytics_get(path: str, params: dict) -> dict:
    qs = urllib.parse.urlencode({"index": INDEX, **params})
    return _request(f"https://{ANALYTICS_HOST}{path}?{qs}", ANALYTICS_KEY,
                    label=f"analytics {path}")


# --------------------------------------------------------------------------
# Config / dates
# --------------------------------------------------------------------------
def load_collections() -> list[str]:
    """Tracked collection handles. Editable in algolia_collections.yaml, no
    code change. Returns an empty list (not an error) if the file is missing
    or has nothing configured -- see the module docstring's note on how
    placement handles that."""
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return list(DEFAULT_COLLECTIONS)
    section = cfg.get("algolia") or {}
    handles = section.get("tracked_collections")
    return [str(h).strip() for h in handles if str(h).strip()] if handles else list(DEFAULT_COLLECTIONS)


def clamp_to_retention(start: dt.date, end: dt.date) -> tuple[dt.date, bool]:
    """Analytics hard-400s past the retention window. Clamp and tell the caller."""
    floor = dt.date.today() - dt.timedelta(days=RETENTION_DAYS)
    if start < floor:
        return floor, True
    return start, False


def date_range(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


# --------------------------------------------------------------------------
# Grain: placement (Search API)
# --------------------------------------------------------------------------
def fetch_collection(handle: str) -> list[dict]:
    """Every product in one collection, in the order the grid shows them.

    Pages past the 1000-hit-per-page maximum when a collection is larger than
    that, so a large collection is captured in full rather than truncated at
    the first page.
    """
    hits: list[dict] = []
    page = 0
    while True:
        data = search_query({
            "query": "",
            "hitsPerPage": HITS_PER_PAGE,
            "page": page,
            "facetFilters": [[f"collections:{handle}"]],
            # Ask only for what we store. Records on a real catalog often
            # carry large html/meta blobs that would otherwise be downloaded
            # for every product of every collection.
            "attributesToRetrieve": [
                "objectID", "id", "sku", "handle", "title", "vendor", "price",
                "compare_at_price", "inventory_available", "published_at",
            ],
            "attributesToHighlight": [],
        })
        batch = data.get("hits") or []
        hits.extend(batch)
        n_pages = data.get("nbPages") or 1
        page += 1
        if page >= n_pages or not batch:
            break
    return hits


def store_placement(conn: sqlite3.Connection, handles: list[str], stamp: str) -> tuple[int, list[str]]:
    if not handles:
        print("    no tracked collections configured -- see algolia_collections.yaml "
              "or pass --collections", flush=True)
        return 0, []
    snapshot_date = dt.date.today().isoformat()
    total = 0
    failed: list[str] = []
    for handle in handles:
        try:
            hits = fetch_collection(handle)
        except (AlgoliaError, AlgoliaTransient) as exc:
            # One bad collection must not cost the whole snapshot.
            print(f"    {handle}: FAILED {exc}", flush=True)
            failed.append(handle)
            continue
        size = len(hits)
        rows = [
            (
                snapshot_date, handle, i + 1,
                str(h.get("objectID")) if h.get("objectID") is not None else None,
                str(h.get("id")) if h.get("id") is not None else None,
                h.get("sku"), h.get("handle"), h.get("title"), h.get("vendor"),
                h.get("price"), h.get("compare_at_price"),
                None if h.get("inventory_available") is None else int(bool(h.get("inventory_available"))),
                h.get("published_at"), size, stamp,
            )
            for i, h in enumerate(hits)
        ]
        with conn:
            # Re-running the same day must replace, not append -- and the grid
            # shrinks as well as grows, so stale deep positions have to go.
            conn.execute(
                "DELETE FROM collection_placement WHERE snapshot_date=? AND collection=?",
                (snapshot_date, handle),
            )
            conn.executemany(
                "INSERT INTO collection_placement VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        total += len(rows)
        print(f"    {handle}: {len(rows)} products", flush=True)
    return total, failed


# --------------------------------------------------------------------------
# Grain: hits (Analytics API, per object per day)
# --------------------------------------------------------------------------
def purchase_events_present(days: list[dt.date]) -> bool:
    """Does the storefront send purchase events yet? (trap 1)

    This gates `revenueAnalytics`, which roughly doubles the cost of an
    already-paginated `/2/hits` crawl. If purchase events aren't wired, the
    only fields it would buy (purchaseCount/purchaseRate/currencies) are
    guaranteed zero/empty, so paying that cost is pure waste.

    Rather than hardcode that away permanently (which would mean this
    connector silently keeps ignoring purchase events even after your
    storefront starts sending them), it's a cheap ONE-REQUEST check each run:
    the moment real purchases appear, revenue capture self-enables with no
    code change.
    """
    probe_day = days[-1]
    try:
        res = analytics_get("/2/conversions/purchaseRate", {
            "startDate": probe_day.isoformat(), "endDate": probe_day.isoformat(),
        })
    except (AlgoliaError, AlgoliaTransient):
        return False  # fail closed: cheap+incomplete beats slow+failed
    return bool(res.get("purchaseCount"))


def fetch_hits(day: dt.date, revenue: bool) -> list[dict]:
    """Every object with engagement on one day.

    Latency is per-REQUEST and roughly flat in page size, so always page at
    the 1000 maximum.
    """
    out: list[dict] = []
    for page in range(MAX_ANALYTICS_PAGES):
        params = {
            "startDate": day.isoformat(), "endDate": day.isoformat(),
            "clickAnalytics": "true",
            "limit": ANALYTICS_PAGE_LIMIT, "offset": page * ANALYTICS_PAGE_LIMIT,
        }
        if revenue:
            params["revenueAnalytics"] = "true"
        data = analytics_get("/2/hits", params)
        batch = data.get("hits") or []
        out.extend(batch)
        if len(batch) < ANALYTICS_PAGE_LIMIT:
            return out
    # Hitting the guard means the result set is deeper than expected; say so
    # rather than silently storing a truncated day.
    print(f"    WARNING {day}: hit MAX_ANALYTICS_PAGES ({MAX_ANALYTICS_PAGES}) "
          f"-- day is TRUNCATED at {len(out)} objects", flush=True)
    return out


def store_hits(conn: sqlite3.Connection, days: list[dt.date], stamp: str) -> int:
    revenue = purchase_events_present(days)
    print(f"    purchase events {'PRESENT -- capturing revenue analytics' if revenue else 'absent -- skipping revenueAnalytics (2x faster)'}",
          flush=True)
    total = 0
    for day in days:
        hits = fetch_hits(day, revenue)
        rows = [
            (
                day.isoformat(), str(h.get("hit")),
                h.get("count"), h.get("trackedHitCount"),
                h.get("clickCount"), h.get("clickThroughRate"),
                # Algolia may call this "conversion"; on an account with no
                # purchase events wired it is add-to-cart and nothing else
                # (trap 1).
                h.get("addToCartCount"), h.get("addToCartRate"),
                h.get("purchaseCount"), h.get("purchaseRate"),
                stamp,
            )
            for h in hits if h.get("hit") is not None
        ]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO algolia_product_engagement "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
            )
        total += len(rows)
        print(f"    {day}: {len(rows)} objects", flush=True)
    return total


# --------------------------------------------------------------------------
# Grain: positions (the click-decay curve)
# --------------------------------------------------------------------------
def store_positions(conn: sqlite3.Connection, days: list[dt.date], stamp: str) -> int:
    total = 0
    for day in days:
        data = analytics_get("/2/clicks/positions", {
            "startDate": day.isoformat(), "endDate": day.isoformat(),
        })
        rows = []
        for entry in data.get("positions") or []:
            span = entry.get("position") or []
            if not span:
                continue
            lo = span[0]
            hi = span[1] if len(span) > 1 else span[0]
            rows.append((day.isoformat(), lo, hi, entry.get("clickCount"), stamp))
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO algolia_click_positions VALUES (?,?,?,?,?)", rows)
        total += len(rows)
    print(f"    click positions: {total} buckets over {len(days)} day(s)", flush=True)
    return total


# --------------------------------------------------------------------------
# Grain: searches (top queries per day)
# --------------------------------------------------------------------------
def store_searches(conn: sqlite3.Connection, days: list[dt.date], stamp: str) -> int:
    """Top queries by volume, CAPPED and DEDUPED.

    A busy account can hold tens of thousands of distinct queries in a single
    day and the tail is typically single-count typos, so storing every one
    would add a large number of low-value rows over time. The cap is
    announced in the run log rather than applied silently.

    !! OFFSET PAGING ON THIS ENDPOINT CAN OVERLAP -- IT IS NOT GUARANTEED TO
    BE A CLEAN PARTITION !! Consecutive pages have been observed sharing a
    handful of query strings between them, so rows must be deduped by query
    before insert, or the reported row count can overstate what was actually
    stored and the same query can be inserted twice with whichever page's
    numbers happened to land last. Dedupe keeps the FIRST sighting, which is
    the higher-volume page (results are volume-ordered).
    """
    total = 0
    for day in days:
        collected: dict[str, dict] = {}
        for page in range(MAX_ANALYTICS_PAGES):
            if len(collected) >= SEARCH_QUERY_CAP:
                break
            data = analytics_get("/2/searches", {
                "startDate": day.isoformat(), "endDate": day.isoformat(),
                "clickAnalytics": "true",
                "limit": ANALYTICS_PAGE_LIMIT,
                "offset": page * ANALYTICS_PAGE_LIMIT,
            })
            batch = data.get("searches") or []
            for s in batch:
                # Results are volume-ordered, so first sighting wins.
                collected.setdefault(s.get("search") or "", s)
            if len(batch) < ANALYTICS_PAGE_LIMIT:
                break
        rows = [
            (
                day.isoformat(), query,
                s.get("count"), s.get("nbHits"),
                s.get("clickThroughRate"), s.get("averageClickPosition"),
                s.get("conversionRate"), s.get("clickCount"), s.get("conversionCount"),
                stamp,
            )
            for query, s in list(collected.items())[:SEARCH_QUERY_CAP]
        ]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO algolia_searches VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        total += len(rows)
        note = f" (CAPPED at {SEARCH_QUERY_CAP})" if len(collected) >= SEARCH_QUERY_CAP else ""
        print(f"    {day}: {len(rows)} distinct queries{note}", flush=True)
    return total


# --------------------------------------------------------------------------
# Grain: daily (account-level series)
# --------------------------------------------------------------------------
def _series(path: str, start: dt.date, end: dt.date) -> dict[str, dict]:
    """Fetch one daily-series endpoint, keyed by date."""
    data = analytics_get(path, {"startDate": start.isoformat(), "endDate": end.isoformat()})
    return {d["date"]: d for d in (data.get("dates") or []) if d.get("date")}


def store_daily(conn: sqlite3.Connection, start: dt.date, end: dt.date, stamp: str) -> int:
    # Each endpoint returns its own daily series; zip them by date. Cheaper and
    # far less code than one request per metric per day.
    searches = _series("/2/searches/count", start, end)
    users = _series("/2/users/count", start, end)
    ctr = _series("/2/clicks/clickThroughRate", start, end)
    pos = _series("/2/clicks/averageClickPosition", start, end)
    atc = _series("/2/conversions/addToCartRate", start, end)
    purch = _series("/2/conversions/purchaseRate", start, end)
    nores = _series("/2/searches/noResultRate", start, end)
    noclick = _series("/2/searches/noClickRate", start, end)

    rows = []
    for day in date_range(start, end):
        key = day.isoformat()
        c = ctr.get(key, {})
        rows.append((
            key,
            searches.get(key, {}).get("count"),
            users.get(key, {}).get("count"),
            c.get("trackedSearchCount"),
            c.get("clickCount"),
            c.get("rate"),
            pos.get(key, {}).get("average"),
            atc.get(key, {}).get("addToCartCount"),
            atc.get(key, {}).get("rate"),
            purch.get(key, {}).get("purchaseCount"),
            purch.get(key, {}).get("rate"),
            nores.get(key, {}).get("noResultCount"),
            nores.get(key, {}).get("rate"),
            noclick.get(key, {}).get("noClickCount"),
            noclick.get(key, {}).get("rate"),
            stamp,
        ))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO algolia_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"    daily series: {len(rows)} day(s)", flush=True)
    return len(rows)


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------
def probe() -> None:
    """Report what is reachable and what the account actually sends. No writes."""
    print(f"App {APP_ID}  index {INDEX}")
    try:
        res = search_query({"query": "", "hitsPerPage": 1})
        print(f"  Search API      OK  ({res.get('nbHits')} products on the default grid)")
    except Exception as exc:  # noqa: BLE001 -- a probe reports, never raises
        print(f"  Search API      FAILED  {exc}")

    if not ANALYTICS_KEY:
        print("  Analytics API   SKIPPED (ALGOLIA_ANALYTICS_KEY unset)")
    else:
        end = dt.date.today() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=6)
        try:
            cnt = analytics_get("/2/searches/count",
                                {"startDate": start.isoformat(), "endDate": end.isoformat()})
            print(f"  Analytics API   OK  ({cnt.get('count'):,} searches {start}..{end})")
            pr = analytics_get("/2/conversions/purchaseRate",
                               {"startDate": start.isoformat(), "endDate": end.isoformat()})
            rev = analytics_get("/2/conversions/revenue",
                                {"startDate": start.isoformat(), "endDate": end.isoformat()})
            print(f"  purchase events {pr.get('purchaseCount')} "
                  f"| revenue currencies {rev.get('currencies')}")
            if not pr.get("purchaseCount"):
                print("     -> storefront sends NO purchase events: Algolia "
                      "'conversion' == add-to-cart (trap 1). Purchases must "
                      "come from your own order/analytics data.")
        except Exception as exc:  # noqa: BLE001
            print(f"  Analytics API   FAILED  {exc}")

    print("\n  Tracked collections:")
    handles = load_collections()
    if not handles:
        print("    (none configured -- see algolia_collections.yaml)")
    for handle in handles:
        try:
            res = search_query({"query": "", "hitsPerPage": 1,
                                "facetFilters": [[f"collections:{handle}"]]})
            print(f"    {handle:18s} {res.get('nbHits'):>6} products")
        except Exception as exc:  # noqa: BLE001
            print(f"    {handle:18s} FAILED {exc}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--days", type=int, default=3,
                        help="analytics window ending yesterday (default 3)")
    parser.add_argument("--start", help="analytics window start YYYY-MM-DD")
    parser.add_argument("--end", help="analytics window end YYYY-MM-DD")
    parser.add_argument("--only", choices=GRAINS, action="append",
                        help="limit to these grains (repeatable)")
    parser.add_argument("--collections",
                        help="comma-separated collection handles (default: algolia_collections.yaml)")
    parser.add_argument("--probe", action="store_true",
                        help="report reachability and write nothing")
    args = parser.parse_args()

    check_required_env()

    if args.probe:
        probe()
        return

    grains = tuple(args.only) if args.only else GRAINS

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=1)
    start = (dt.date.fromisoformat(args.start) if args.start
             else end - dt.timedelta(days=args.days - 1))
    start, clamped = clamp_to_retention(start, end)
    if clamped:
        print(f"NOTE: start clamped to {start} -- Algolia analytics retention is "
              f"{RETENTION_DAYS + 1} days and older data is permanently gone.")
    if start > end:
        print(f"Nothing to do: start {start} is after end {end}.")
        return

    days = list(date_range(start, end))
    handles = ([h.strip() for h in args.collections.split(",") if h.strip()]
               if args.collections else load_collections())

    warehouse_db.init_db()
    conn = sqlite3.connect(DB_PATH, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(DDL)
    stamp = warehouse_db.now()

    analytics_grains = [g for g in grains if g != "placement"]
    if analytics_grains and not ANALYTICS_KEY:
        print("ALGOLIA_ANALYTICS_KEY unset -- skipping analytics grains "
              f"({', '.join(analytics_grains)}); placement is unaffected.")
        analytics_grains = []

    # Each grain logs its own sync_log platform and a failure in one does NOT
    # kill the others.
    plan: list[tuple[str, str, Callable[[], object]]] = []
    if "placement" in grains:
        plan.append(("algolia_placement", "placement",
                     lambda: store_placement(conn, handles, stamp)))
    if "hits" in analytics_grains:
        plan.append(("algolia_hits", "hits", lambda: store_hits(conn, days, stamp)))
    if "positions" in analytics_grains:
        plan.append(("algolia_positions", "positions",
                     lambda: store_positions(conn, days, stamp)))
    if "searches" in analytics_grains:
        plan.append(("algolia_searches", "searches",
                     lambda: store_searches(conn, days, stamp)))
    if "daily" in analytics_grains:
        plan.append(("algolia_daily", "daily", lambda: store_daily(conn, start, end, stamp)))

    failures = 0
    try:
        for platform, label, fn in plan:
            print(f"  {label}:", flush=True)
            started = warehouse_db.now()
            try:
                result = fn()
                if label == "placement":
                    rows, failed_handles = result
                    if failed_handles:
                        # A partial snapshot is NOT ok -- a run that quietly
                        # drops a collection reads as "that grid was empty".
                        warehouse_db.log_sync(
                            platform, started, rows, "degraded",
                            f"{len(failed_handles)} collection(s) failed: "
                            f"{','.join(failed_handles)}")
                        failures += 1
                        continue
                else:
                    rows = result
                warehouse_db.log_sync(platform, started, rows, "ok")
            except (AlgoliaError, AlgoliaTransient) as exc:
                print(f"    FAILED: {exc}", flush=True)
                warehouse_db.log_sync(platform, started, 0, "error", str(exc))
                failures += 1
    finally:
        conn.close()

    if failures:
        print(f"Algolia sync finished with {failures} failed grain(s).")
        sys.exit(1)
    print("Algolia sync complete.")


if __name__ == "__main__":
    main()
