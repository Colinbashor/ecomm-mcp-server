r"""
Amazon top search terms BY CATEGORY, MONTHLY grain -> amazon_search_term_monthly.

`amazon_ba_sync.py`'s Top Search Terms grain is WEEKLY and keyed on terms that
touch your own ASINs / a brand watchlist / a flat marketplace-rank ceiling —
it carries no category. This script answers a different question: "what are
the top N search terms each month in the categories I actually sell in".

!! THE CONSTRAINT THAT SHAPES THE WHOLE DESIGN: THE SP-API SEARCH TERMS REPORT
HAS NO CATEGORY DIMENSION !!
Its `departmentName` field is the single literal value "Amazon.com" on every
row. The category filter you may remember from the Seller Central UI is a UI
control, not part of the report — reportOptions accepts only `reportPeriod`.
So category cannot be REQUESTED and must be DERIVED, and there is no way to
make Amazon send less than the whole marketplace's search terms.

CATEGORY IS DERIVED FROM THE CLICKED ASINs. Each term row carries up to three
top-clicked ASINs; an ASIN's browse-node category comes from the Catalog
Items API (`classificationRanks[].title`, the same field
`amazon_rank_sync.py` already reads). A category is a stable product
attribute, so every ASIN is resolved ONCE and cached forever in
`amazon_asin_category` — the lookup cost is front-loaded onto the first month
and near-zero afterward. Most clicked ASINs on a market-wide report will not
be your own, so joining the report to your own catalog first would
categorize almost nothing; the browse-node lookup has to run against
whichever ASIN actually appears in the report.

!! THE GRAIN IS A BROAD BUCKET, NOT A RAW BROWSE NODE — THIS MATTERS FOR HOW
YOU CONFIGURE search_term_categories.yaml !! Amazon's marketplace has many
thousands of narrow browse nodes; any one seller's catalog usually maps to a
small handful of them, and the head of the marketplace-wide search-term list
is dominated by whatever is popular across ALL sellers, not yours. Tracking
narrow nodes 1:1 tends to fill slowly or not at all, because each node
competes separately for its own quota against an overwhelming amount of
unrelated traffic. Rolling several related narrow nodes into one broad
bucket (e.g. treating "Casual Dresses", "Cocktail Dresses", and "Club Dresses"
all as one "Dresses" bucket) lifts the fill rate substantially, because the
bucket now accepts a hit from any of them instead of each node needing its
own 200-term quota filled independently. See `search_term_categories.yaml`
for the config format and a placeholder example.

A TERM CAN BELONG TO SEVERAL BUCKETS, deliberately. If a term sends clicks to
both a dress and a boot, it is a top term for both, and whoever is looking at
either bucket needs to see it. `category_click_share_pct` reports how much of
that term's top-3 click share landed specifically in THIS bucket, so a term
that is marginal in one bucket and dominant in another is distinguishable.

--- HOW THIS AVOIDS OVER-QUERYING --------------------------------------------
ONE report per month, not several weekly ones, and reportPeriod=MONTH is
native so there is no week-stitching. Beyond that the report body itself is
unavoidable (Amazon has no server-side filter for it), so the savings are
all on the second API call — the per-ASIN category lookups:

  * SCAN CEILING -- only terms at/above `scan_rank_ceiling` marketplace rank
    are considered at all. This is both the memory bound (a market-wide
    report can run to millions of records and cannot be held in RAM whole)
    and the honesty bound (see coverage below).
  * RANK ORDER + EARLY EXIT -- candidates are walked most-searched-first and
    the run STOPS the moment every configured category holds its quota.
    Walking in rank order is what makes the early exit CORRECT rather than
    merely cheap: the first N terms a category accepts ARE its top N.
  * PERMANENT CACHE -- `amazon_asin_category` caches misses too (a NULL
    category), so an ASIN Amazon has no classification for is never asked
    about twice.
  * HARD CAP -- `max_asin_lookups` bounds a runaway first month.
  * COST-AWARE STALL GUARD -- some categories will never fill within any
    affordable scan depth (a genuinely thin niche, or a seasonal category
    outside its season). The guard stops a scan once a window of recent
    chunks has both SPENT real lookup budget and GAINED few rows — it must
    check both, because a window served entirely from the permanent cache
    is free and stopping it wastes rows for no savings at all.

WE RECORD WHAT WE ASKED, NOT WHAT CAME BACK (`amazon_search_term_coverage`),
the same "coverage over presence" rule used elsewhere in this project (see
`amazon_sqp_sync.py`). A category that did not fill its quota within the rank
ceiling is marked incomplete with a reason; the absence of rows must never be
read as "this term doesn't exist" — a truncated scan and a genuinely thin
category look identical in the data table alone. A partial month is logged
as `degraded`, never `ok`.

RETENTION IS UNPROBED for this report at MONTH granularity. `--backfill`
walks BACKWARD a month at a time until Amazon CANCELLEDs (its no-data signal)
and treats that as the floor.

CONFIG: `search_term_categories.yaml` in the project root (see that file for
the format). The script refuses to run with no buckets configured.

WHICH ASINs ARE "OURS": same pattern as `amazon_ba_sync.py` and
`amazon_sqp_sync.py` — pass `--asins`/`--asins-file`, or fall back to
`amazon_rank_sync.fallback_asins()` (weak; pass your ASINs explicitly for
anything beyond a first smoke test).

USAGE:
  python amazon_search_terms_monthly.py --probe
  python amazon_search_terms_monthly.py --asins-file asins.txt --last-month
  python amazon_search_terms_monthly.py --asins-file asins.txt --month 2026-07
  python amazon_search_terms_monthly.py --asins-file asins.txt --backfill
  python amazon_search_terms_monthly.py --asins-file asins.txt --month 2026-07 --refresh
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from amazon_rank_sync import fallback_asins
from warehouse import db as warehouse_db
from warehouse.brand_analytics import (
    BAReportCancelled, BAReportFatal, CREATE_SPACING_SEC, await_ba_report,
    create_ba_report, stream_ba_records)
from warehouse.connectors.amazon_orders import HOSTS, _access_token

load_dotenv()
HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("WAREHOUSE_DB", HERE / "warehouse.db"))
CONFIG_FILE = HERE / "search_term_categories.yaml"

REPORT_TYPE = "GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT"
PLATFORM = "amazon_search_terms_monthly"
REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")

# Catalog Items pacing. amazon_rank_sync.py runs the same endpoint at 0.6s
# between calls (~2 req/sec, its documented rate) with no throttling observed;
# reuse that pace rather than discovering a new limit the hard way.
CATALOG_BATCH = 20          # searchCatalogItems identifiers cap
CATALOG_PACE_SEC = 0.6
# Terms are categorized in chunks so each catalog call carries a full 20
# ASINs. Resolving one term's 3 ASINs at a time would waste 85% of every call.
TERM_CHUNK = 400

DDL = """
-- Top search terms per month per browse-node category bucket (see
-- search_term_categories.yaml). ONE ROW PER (month, category, search_term);
-- a term legitimately appears under every category its top-3 clicked ASINs
-- reach (see module docstring).
-- Shares are PERCENT (Amazon returns FRACTIONS; normalized x100 at ingest) --
-- the same unit trap documented in amazon_ba_sync.py.
CREATE TABLE IF NOT EXISTS amazon_search_term_monthly (
    month                         TEXT NOT NULL,    -- 'YYYY-MM'
    category                      TEXT NOT NULL,    -- your configured bucket name
    search_term                   TEXT NOT NULL,
    category_term_rank            INTEGER NOT NULL, -- 1..N within (month, category)
    search_frequency_rank         INTEGER,          -- MARKETPLACE-wide; 1 = most searched
    category_click_share_pct      REAL,             -- summed over this term's top-3 ASINs in THIS bucket
    category_conversion_share_pct REAL,
    asins_in_category             INTEGER,          -- how many of the top-3 sit here (1..3)
    top_asin                      TEXT,             -- highest click share of those
    top_asin_title                TEXT,
    top_asin_node                 TEXT,             -- the RAW browse node behind the bucket
    top_asin_click_share_pct      REAL,
    top_asin_conversion_share_pct REAL,
    is_ours                       INTEGER NOT NULL DEFAULT 0,
    our_asin                      TEXT,
    synced_at                     TEXT NOT NULL,
    PRIMARY KEY (month, category, search_term)
);
CREATE INDEX IF NOT EXISTS idx_astm_month_rank
    ON amazon_search_term_monthly (month, category, category_term_rank);
CREATE INDEX IF NOT EXISTS idx_astm_term ON amazon_search_term_monthly (search_term);

-- Permanent ASIN -> browse-node category cache. category_title IS NULL means
-- RESOLVED AND AMAZON HAS NO CLASSIFICATION -- a negative cache entry, so the
-- ASIN is never looked up again. An ASIN simply absent has never been asked.
CREATE TABLE IF NOT EXISTS amazon_asin_category (
    asin           TEXT PRIMARY KEY,
    category_title TEXT,
    display_title  TEXT,
    resolved_at    TEXT NOT NULL
);

-- What the scan actually ASKED for. Absence of term rows can never be the
-- resume marker: a category that is genuinely thin and one the scan never
-- reached look identical in the data table.
CREATE TABLE IF NOT EXISTS amazon_search_term_coverage (
    month              TEXT PRIMARY KEY,
    report_records     INTEGER,   -- rows in Amazon's document
    terms_seen         INTEGER,   -- distinct terms in the document
    candidates         INTEGER,   -- terms within scan_rank_ceiling
    terms_examined     INTEGER,   -- candidates whose ASINs we actually categorized
    deepest_rank       INTEGER,   -- deepest marketplace rank examined
    asin_lookups       INTEGER,   -- NEW ASINs resolved this run
    categories_total   INTEGER,
    categories_filled  INTEGER,   -- reached terms_per_category
    rows_written       INTEGER,
    stop_reason        TEXT,      -- all_categories_full | lookup_budget | exhausted_candidates | diminishing_returns
    scan_rank_ceiling  INTEGER,
    terms_per_category INTEGER,
    is_complete        INTEGER,
    attempts           INTEGER NOT NULL DEFAULT 1,
    synced_at          TEXT NOT NULL
);
"""

# A month can be permanently incomplete for a legitimate reason -- a thin
# category may simply never have N distinct terms inside the rank ceiling.
# Without a cap, a self-healing monthly re-run would re-pull that month's
# whole report forever. Same poison guard as amazon_sqp_sync's
# MAX_RETRY_REPORTS / amazon_traffic_sync's MAX_REPAIR_ATTEMPTS.
MAX_ATTEMPTS = 3
# Stop reasons meaning "the scan ran dry", not "the scan was cut off". A month
# that ended this way returns identical rows on a re-run with the same config,
# so re-pulling its whole report would be pure waste; only `lookup_budget` is
# a real truncation worth retrying.
SETTLED_REASONS = ("all_categories_full", "diminishing_returns",
                   "exhausted_candidates")

# STALL GUARD. It exists because `all_categories_full` only fires when EVERY
# bucket reaches its quota, so one bucket that can never fill (or is out of
# season) keeps the scan running to the hard lookup cap every single month
# even though the other buckets finished long ago.
#
# !! IT GUARDS COST, NOT DEPTH -- a window only counts as stalled if it
# actually SPENT lookups. A purely row-based guard would fire on a re-run
# whose ASINs were all already cached, stopping a scan that was costing
# nothing and throwing away real rows for no saving whatsoever. Scanning
# cached candidates is free, so the only thing worth stopping is spend that
# has stopped buying rows.
STALL_WINDOW_CHUNKS = 8
STALL_MIN_ROWS = 5


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_search_terms_monthly: missing required env var(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill in the "
            "SP-API credentials (same ones amazon_orders.py uses)."
        )


# ---- config ----------------------------------------------------------------

def _migrate(conn) -> None:
    """Additive column migrations. CREATE TABLE IF NOT EXISTS is a no-op on an
    existing table, so a widened schema needs explicit ALTERs. Columns are
    always named in the coverage INSERT for exactly this reason: ALTER
    appends the new column at the table's PHYSICAL end (after synced_at)
    while the DDL declares it before synced_at, so a positional INSERT could
    silently write into the wrong column on a migrated (but not fresh)
    database."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(amazon_search_term_coverage)")}
    if have and "attempts" not in have:
        with conn:
            conn.execute("ALTER TABLE amazon_search_term_coverage "
                         "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
    have = {r[1] for r in conn.execute("PRAGMA table_info(amazon_search_term_monthly)")}
    if have and "top_asin_node" not in have:
        with conn:
            conn.execute("ALTER TABLE amazon_search_term_monthly "
                         "ADD COLUMN top_asin_node TEXT")


def _cfg() -> dict:
    try:
        import yaml
    except ImportError:
        return {"buckets": {}, "exclude_nodes": [], "terms_per_category": 200,
                "scan_rank_ceiling": 250_000, "max_asin_lookups": 60_000}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            c = yaml.safe_load(f) or {}
    except FileNotFoundError:
        c = {}
    buckets = c.get("buckets") or {}
    return {
        "buckets": {str(k): [str(p) for p in (v or [])] for k, v in buckets.items()},
        "exclude_nodes": [str(p) for p in (c.get("exclude_nodes") or [])],
        "terms_per_category": int(c.get("terms_per_category", 200)),
        "scan_rank_ceiling": int(c.get("scan_rank_ceiling", 250_000)),
        "max_asin_lookups": int(c.get("max_asin_lookups", 60_000)),
    }


def compile_buckets(cfg: dict):
    """(ordered [(bucket, regex)], exclude_regex_or_None).

    Order is load-bearing and comes from the YAML: first match wins, so put
    more specific bucket patterns before more general ones that would
    otherwise swallow them (see search_term_categories.yaml).
    """
    compiled = [(name, re.compile("|".join(pats), re.I))
                for name, pats in cfg["buckets"].items() if pats]
    ex = cfg.get("exclude_nodes") or []
    return compiled, (re.compile("|".join(ex), re.I) if ex else None)


def bucket_of(node: str | None, compiled, exclude) -> str | None:
    """Map an Amazon browse-node title to a configured bucket, or None."""
    if not node:
        return None
    if exclude is not None and exclude.search(node):
        return None
    for name, rx in compiled:
        if rx.search(node):
            return name
    return None


def _g(rec: dict, *names):
    """BA documents have flipped between camelCase and snake_case; accept both."""
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return None


def _pct(frac):
    """FRACTION (0.0751) -> PERCENT (7.51). None-safe. Same trap as amazon_ba_sync."""
    return None if frac in (None, "") else round(float(frac) * 100.0, 4)


def month_bounds(ym: str) -> tuple[date, date]:
    y, m = (int(x) for x in ym.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def prior_month(today: date | None = None) -> str:
    t = today or date.today()
    return (t.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


# ---- ASIN -> category ------------------------------------------------------

def _load_cache(conn) -> dict[str, str | None]:
    return {a: c for a, c in conn.execute(
        "SELECT asin, category_title FROM amazon_asin_category")}


def _fetch_categories(asins: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """searchCatalogItems for <=20 ASINs -> {asin: (category_title, display_title)}.

    pageSize=20 is mandatory: it defaults to 10, so a 20-ASIN batch silently
    returns only the first half behind an unpaged nextToken (the same trap
    amazon_rank_sync.py documents).
    """
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    mk = os.environ["SPAPI_MARKETPLACE_ID"]
    params = {"marketplaceIds": mk, "identifiers": ",".join(asins),
              "identifiersType": "ASIN", "includedData": "salesRanks", "pageSize": 20}
    for attempt in range(8):
        try:
            r = requests.get(f"{host}/catalog/2022-04-01/items",
                             headers={"x-amz-access-token": _access_token(),
                                      "Accept": "application/json"},
                             params=params, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(15)
            continue
        if r.status_code == 429:
            time.sleep(min(60.0, 5 * (attempt + 1)))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"catalog items {r.status_code}: {r.text[:200]}")
        out: dict[str, tuple[str | None, str | None]] = {}
        for it in r.json().get("items", []):
            catt = disp = best = None
            for sr in it.get("salesRanks", []):
                if sr.get("marketplaceId") not in (mk, None):
                    continue
                for c in sr.get("classificationRanks", []):
                    # best (lowest) rank = the most specific / headline category,
                    # matching how amazon_sales_rank.category_title is chosen.
                    if c.get("rank") is not None and (best is None or c["rank"] < best):
                        best, catt = c["rank"], c.get("title")
                for d in sr.get("displayGroupRanks", []):
                    if disp is None:
                        disp = d.get("title")
            out[it.get("asin")] = (catt, disp)
        return out
    raise RuntimeError("catalog items kept throttling after retries")


def resolve_asins(conn, cache: dict, asins: set[str], budget: list[int],
                  stamp: str) -> int:
    """Resolve+cache every ASIN not already known, respecting the lookup budget.

    `budget` is a one-element list so the caller sees the decrement. Returns
    the number of NEW ASINs resolved. Negative results are cached (category
    NULL) so an unclassifiable ASIN costs exactly one lookup, ever.
    """
    todo = sorted(a for a in asins if a and a not in cache)
    if not todo or budget[0] <= 0:
        return 0
    todo = todo[:budget[0]]
    done = 0
    for i in range(0, len(todo), CATALOG_BATCH):
        batch = todo[i:i + CATALOG_BATCH]
        got = _fetch_categories(batch)
        rows = []
        for a in batch:
            cat, disp = got.get(a, (None, None))
            cache[a] = cat            # None = negative cache entry, never re-asked
            rows.append((a, cat, disp, stamp))
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO amazon_asin_category "
                "(asin, category_title, display_title, resolved_at) "
                "VALUES (?,?,?,?)", rows)
        done += len(batch)
        budget[0] -= len(batch)
        time.sleep(CATALOG_PACE_SEC)
    return done


# ---- the scan --------------------------------------------------------------

def collect_candidates(records, ceiling: int) -> tuple[dict, int, int]:
    """Stream the report -> {term: (rank, [(asin, name, click%, conv%), ...])}.

    Only terms at/above `ceiling` are retained; everything else is discarded
    as it streams past, which is what keeps a multi-million-record document
    inside memory. Returns (candidates, total_records, distinct_terms_seen).
    """
    cand: dict[str, list] = {}
    total = 0
    # terms_seen is counted by TRANSITION, not with a set. A set of every
    # distinct term in a market-wide document could be millions of strings —
    # it would dwarf the candidate dict this function exists to bound. Amazon
    # emits a term's up-to-3 clicked-ASIN rows contiguously, so counting
    # changes of `term` is exact in practice; if that ever stopped holding,
    # the count would over-report, never silently drop a candidate.
    blocks = 0
    prev_term = None
    for r in records:
        total += 1
        term = _g(r, "searchTerm", "search_term")
        if not term:
            continue
        if term != prev_term:
            blocks += 1
            prev_term = term
        rank = _g(r, "searchFrequencyRank", "search_frequency_rank")
        if rank is None:
            continue
        rank = int(rank)
        if rank > ceiling:
            continue
        asin = _g(r, "clickedAsin", "clicked_asin")
        if not asin:
            continue
        entry = cand.get(term)
        if entry is None:
            entry = cand[term] = [rank, []]
        entry[1].append((
            asin,
            _g(r, "clickedItemName", "clicked_item_name"),
            _pct(_g(r, "clickShare", "click_share")),
            _pct(_g(r, "conversionShare", "conversion_share")),
        ))
    return cand, total, blocks


def build_rows(conn, month: str, cand: dict, cfg: dict, our_asins: set[str],
               stamp: str) -> tuple[list, dict]:
    """Walk candidates most-searched-first, filling each category's top-N.

    Rank order is what makes the early exit CORRECT: the first N terms a
    category accepts are, by construction, its N most-searched terms.
    """
    compiled, exclude = compile_buckets(cfg)
    want = cfg["terms_per_category"]
    cache = _load_cache(conn)
    budget = [cfg["max_asin_lookups"]]
    # Browse-node -> bucket is pure string work over a few thousand distinct
    # node titles, so memoize it rather than re-running the regex
    # alternation for every one of possibly hundreds of thousands of
    # clicked-ASIN hits.
    node_bucket: dict[str | None, str | None] = {}

    filled: dict[str, list] = {name: [] for name, _ in compiled}
    stats = {"examined": 0, "deepest": 0, "lookups": 0,
             "stop_reason": "exhausted_candidates"}

    def all_full() -> bool:
        return all(len(v) >= want for v in filled.values())

    ordered = sorted(cand.items(), key=lambda kv: kv[1][0])
    stopped = False
    # (rows_gained, lookups_spent) per chunk, for the cost-aware stall guard.
    recent: list[tuple[int, int]] = []
    for i in range(0, len(ordered), TERM_CHUNK):
        rows_before = sum(len(v) for v in filled.values())
        spent_before = stats["lookups"]
        chunk = ordered[i:i + TERM_CHUNK]
        # Resolve this chunk's unknown ASINs in one pass so every catalog
        # call carries a full 20 identifiers rather than one term's 3.
        need = {a for _, (_, hits) in chunk for a, *_ in hits}
        stats["lookups"] += resolve_asins(conn, cache, need, budget, stamp)
        if any(a not in cache for a in need):
            # The budget ran out with ASINs still unknown. Stop BEFORE
            # processing the chunk: a half-resolved term would silently lose
            # the categories whose ASINs were never looked up, which would
            # read downstream as "this term doesn't belong to that category"
            # rather than "we didn't check".
            stats["stop_reason"] = "lookup_budget"
            break

        for term, (rank, hits) in chunk:
            stats["examined"] += 1
            stats["deepest"] = max(stats["deepest"], rank)
            by_cat: dict[str, list] = {}
            for asin, name, click, conv in hits:
                node = cache.get(asin)
                if node not in node_bucket:
                    node_bucket[node] = bucket_of(node, compiled, exclude)
                b = node_bucket[node]
                if b is not None:
                    by_cat.setdefault(b, []).append((asin, name, click, conv, node))
            for cat, hs in by_cat.items():
                if len(filled[cat]) >= want:
                    continue
                hs.sort(key=lambda h: (h[2] is None, -(h[2] or 0)))
                top = hs[0]
                mine = next((h[0] for h in hs if h[0] in our_asins), None)
                filled[cat].append((
                    month, cat, term, len(filled[cat]) + 1, rank,
                    round(sum(h[2] or 0 for h in hs), 4),
                    round(sum(h[3] or 0 for h in hs), 4),
                    len(hs), top[0], top[1], top[4], top[2], top[3],
                    1 if mine else 0, mine, stamp,
                ))
            if all_full():
                stats["stop_reason"] = "all_categories_full"
                stopped = True
                break
        if stopped:
            break

        recent.append((sum(len(v) for v in filled.values()) - rows_before,
                       stats["lookups"] - spent_before))
        if len(recent) > STALL_WINDOW_CHUNKS:
            recent.pop(0)
        if len(recent) == STALL_WINDOW_CHUNKS:
            gained = sum(r for r, _ in recent)
            spent = sum(k for _, k in recent)
            # Only stop if the window actually COST something. A window
            # served entirely from cache is free, so continuing can only
            # add rows.
            if spent > 0 and gained < STALL_MIN_ROWS:
                stats["stop_reason"] = "diminishing_returns"
                break

    stats["filled"] = sum(1 for v in filled.values() if len(v) >= want)
    stats["per_category"] = {c: len(v) for c, v in filled.items()}
    rows = [r for v in filled.values() for r in v]
    return rows, stats


# ---- orchestration ---------------------------------------------------------

def sync_month(conn, ym: str, cfg: dict, our_asins: set[str], *,
               refresh: bool = False, doc_id: str | None = None) -> tuple[int, dict]:
    """Run one calendar month end-to-end. Returns (rows_written, stats)."""
    prior = conn.execute(
        "SELECT is_complete, attempts, stop_reason FROM amazon_search_term_coverage "
        "WHERE month=?", (ym,)).fetchone()
    attempt = (prior[1] if prior else 0) + 1
    if not refresh and prior:
        if prior[0]:
            print(f"  {ym}: already complete (coverage) — skipping. --refresh to redo.")
            return 0, {"stop_reason": "already_complete", "skipped": True}
        if prior[2] in SETTLED_REASONS:
            # The scan ran dry rather than being cut off, so the same config
            # would return exactly the same rows. Only a real truncation
            # (lookup_budget) is worth another attempt.
            print(f"  {ym}: settled at {prior[2]} — nothing deeper to get with "
                  f"this config. Raise scan_rank_ceiling/max_asin_lookups and "
                  f"use --refresh.")
            return 0, {"stop_reason": "settled", "skipped": True}
        if prior[1] >= MAX_ATTEMPTS:
            # Not a failure: some categories are genuinely thinner than the cap.
            print(f"  {ym}: incomplete after {prior[1]} attempts — not retrying "
                  f"(raise scan_rank_ceiling and use --refresh to go deeper).")
            return 0, {"stop_reason": "attempts_exhausted", "skipped": True}

    start, end = month_bounds(ym)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if doc_id:
        # Re-bucket an ALREADY-GENERATED report document instead of asking
        # Amazon for a new one. Changing the bucket rules is a local
        # decision; the month's underlying data has not moved, so
        # re-running the report would waste a 15-25 min queue slot.
        print(f"  {ym}: reusing report document {doc_id} (no new report)", flush=True)
    else:
        print(f"  {ym}: creating {REPORT_TYPE} (reportPeriod=MONTH {start}..{end})",
              flush=True)
        rid = create_ba_report(REPORT_TYPE, None, {"reportPeriod": "MONTH"},
                               period="MONTH", month_start=start, month_end=end)
        print(f"  {ym}: report {rid} queued — BA reports commonly sit 15-25 min",
              flush=True)
        doc_id = await_ba_report(rid, timeout_min=90)

    print(f"  {ym}: streaming document {doc_id}", flush=True)
    t0 = time.time()
    cand, total, terms_seen = collect_candidates(
        stream_ba_records(doc_id), cfg["scan_rank_ceiling"])
    print(f"  {ym}: {total:,} records / {terms_seen:,} terms -> "
          f"{len(cand):,} candidates within rank {cfg['scan_rank_ceiling']:,} "
          f"({time.time() - t0:.0f}s)", flush=True)

    rows, stats = build_rows(conn, ym, cand, cfg, our_asins, stamp)
    with conn:
        conn.execute("DELETE FROM amazon_search_term_monthly WHERE month=?", (ym,))
        conn.executemany(
            "INSERT OR REPLACE INTO amazon_search_term_monthly "
            "(month, category, search_term, category_term_rank, "
            " search_frequency_rank, category_click_share_pct, "
            " category_conversion_share_pct, asins_in_category, top_asin, "
            " top_asin_title, top_asin_node, top_asin_click_share_pct, "
            " top_asin_conversion_share_pct, is_ours, our_asin, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    n_cat = len(cfg["buckets"])
    complete = 1 if stats["filled"] == n_cat else 0
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO amazon_search_term_coverage "
            "(month, report_records, terms_seen, candidates, terms_examined, "
            " deepest_rank, asin_lookups, categories_total, categories_filled, "
            " rows_written, stop_reason, scan_rank_ceiling, terms_per_category, "
            " is_complete, attempts, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ym, total, terms_seen, len(cand), stats["examined"], stats["deepest"],
             stats["lookups"], n_cat, stats["filled"], len(rows),
             stats["stop_reason"], cfg["scan_rank_ceiling"],
             cfg["terms_per_category"], complete, attempt, stamp))

    print(f"  {ym}: {len(rows):,} rows | {stats['filled']}/{n_cat} buckets full "
          f"| {stats['lookups']:,} new ASIN lookups | deepest rank "
          f"{stats['deepest']:,} | stop={stats['stop_reason']}", flush=True)
    short = {c: n for c, n in stats["per_category"].items()
             if n < cfg["terms_per_category"]}
    if short:
        print(f"  {ym}: UNDER-FILLED (raise scan_rank_ceiling to deepen): {short}",
              flush=True)
    return len(rows), stats


def probe(conn, cfg: dict) -> int:
    """Report readiness without creating a single report."""
    print("Config")
    print(f"  buckets           : {len(cfg['buckets'])}")
    for c, pats in cfg["buckets"].items():
        print(f"                      - {c}  ({len(pats)} node patterns)")
    print(f"  exclude_nodes     : {len(cfg['exclude_nodes'])}")
    print(f"  terms_per_category: {cfg['terms_per_category']}")
    print(f"  scan_rank_ceiling : {cfg['scan_rank_ceiling']:,}")
    print(f"  max_asin_lookups  : {cfg['max_asin_lookups']:,}")
    cached = conn.execute("SELECT COUNT(*), COUNT(category_title) FROM amazon_asin_category").fetchone()
    print(f"\nASIN category cache : {cached[0]:,} resolved ({cached[1]:,} with a category)")
    print("Coverage recorded   :")
    rows = conn.execute(
        "SELECT month, rows_written, categories_filled, categories_total, "
        "deepest_rank, stop_reason, is_complete FROM amazon_search_term_coverage "
        "ORDER BY month DESC LIMIT 24").fetchall()
    if not rows:
        print("  (none yet)")
    for m, n, f, t, d, sr, ok in rows:
        print(f"  {m}  {n:>6,} rows  {f}/{t} cats  deepest {d:>8,}  "
              f"{'complete' if ok else 'PARTIAL '}  {sr}")
    print(f"\nMarketplace {os.environ.get('SPAPI_MARKETPLACE_ID')} / region "
          f"{os.environ.get('SPAPI_REGION', 'NA')}")
    print("NOTE: the report has NO category dimension (departmentName is always "
          "'Amazon.com'); category is derived from clicked-ASIN browse nodes.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--asins", help="comma-separated ASINs that are 'ours'")
    p.add_argument("--asins-file", help="path to a file with one ASIN per line")
    p.add_argument("--month", help="YYYY-MM to sync")
    p.add_argument("--months", type=int, default=1,
                   help="with --month, also sync this many EARLIER months")
    p.add_argument("--last-month", action="store_true", help="sync the previous calendar month")
    p.add_argument("--backfill", action="store_true",
                   help="walk BACKWARD month by month until Amazon CANCELLEDs (retention floor)")
    p.add_argument("--max-months", type=int, default=36, help="backfill safety stop")
    p.add_argument("--refresh", action="store_true", help="re-run months already marked complete")
    p.add_argument("--doc-id", help="re-bucket an already-generated report document "
                                    "instead of creating a new report (single month)")
    p.add_argument("--probe", action="store_true", help="print readiness; no API calls")
    args = p.parse_args()

    require_env()
    warehouse_db.init_db()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(DDL)
    _migrate(conn)
    cfg = _cfg()
    if not cfg["buckets"]:
        print(f"No buckets configured in {CONFIG_FILE.name} — nothing to do. "
              f"See that file (or its example in the repo) for the format.")
        return 1

    if args.probe:
        return probe(conn, cfg)

    if args.asins:
        our_asins = {a.strip() for a in args.asins.split(",") if a.strip()}
    elif args.asins_file:
        our_asins = {ln.strip() for ln in Path(args.asins_file).read_text().splitlines() if ln.strip()}
    else:
        our_asins = set(fallback_asins(conn))

    # One walk-back rule for every mode: start at the requested month (default
    # last month) and step backwards. --months applies to --last-month too, so
    # a monthly scheduled run can re-attempt the prior month for free.
    count = args.max_months if args.backfill else max(1, args.months)
    months = []
    cur = args.month or prior_month()
    for _ in range(count):
        months.append(cur)
        cur = prior_month(month_bounds(cur)[0])

    total = 0
    partial = 0
    for i, ym in enumerate(months):
        started = warehouse_db.now()
        try:
            n, stats = sync_month(conn, ym, cfg, our_asins, refresh=args.refresh,
                                  doc_id=args.doc_id if len(months) == 1 else None)
        except BAReportCancelled as e:
            # CANCELLED means "Amazon has no data for this window". Walking
            # back in --backfill that IS the expected retention floor and is
            # ok; asked for a specific month it means the month is not
            # published yet (or is past retention), and a run that produced
            # nothing must never log ok.
            warehouse_db.log_sync(
                PLATFORM, started, 0, "ok" if args.backfill else "degraded",
                f"{ym} CANCELLED (no data): {str(e)[:120]}")
            print(f"  {ym}: CANCELLED — no data for this month."
                  + (" Treating as the retention floor." if args.backfill
                     else " Not yet published, or past retention."))
            break
        except (BAReportFatal, TimeoutError, Exception) as e:  # noqa: BLE001
            warehouse_db.log_sync(PLATFORM, started, 0, "error", f"{ym}: {str(e)[:150]}")
            print(f"  {ym}: FAILED — {str(e)[:200]}")
            if args.backfill:
                break
            continue
        total += n
        if stats.get("skipped"):
            continue
        # A run that could not fill every category is NOT ok. Logging `ok`
        # on a partial scan is exactly how a truncated pull can go unnoticed
        # for a long time — see the note on coverage tables in the module
        # docstring.
        ok = (stats["filled"] == len(cfg["buckets"])
              or stats["stop_reason"] in SETTLED_REASONS)
        partial += 0 if ok else 1
        warehouse_db.log_sync(
            PLATFORM, started, n, "ok" if ok else "degraded",
            f"{ym}: {stats['filled']}/{len(cfg['buckets'])} buckets full, "
            f"deepest rank {stats['deepest']}, stop={stats['stop_reason']}")
        if i + 1 < len(months):
            time.sleep(CREATE_SPACING_SEC)

    print(f"\nDone: {total:,} rows across {len(months)} month(s) attempted"
          + (f"; {partial} partial" if partial else ""))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
