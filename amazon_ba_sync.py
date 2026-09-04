r"""
Amazon Brand Analytics ingestion (the four reports beyond SQP) -> warehouse.

Runs on the shared warehouse/brand_analytics.py runner (Sun-Sat weeks,
FATAL-reason-from-document, 15-25+ min processing, poll >= 45 min). Each grain
logs its OWN sync_log platform and a failure in one does NOT kill the batch.
Requires SP-API Brand Analytics access (brand registry), same as
amazon_sqp_sync.py.

  (a) GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT -> amazon_ba_search_catalog
      One row per ASIN in your active catalog for the week, per-ASIN search
      funnel. The interesting column is search_sales (SEARCH-driven revenue)
      vs your total ordered sales elsewhere.
  (b) GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT -> amazon_ba_search_terms
      MARKET-WIDE (every seller, every search term — this can be a very large
      report). NEVER ingested wholesale — filtered on ingest to terms that
      (1) have one of YOUR ASINs in the top-3 clicks ['ours'], (2) optionally
      match a brand-name watchlist ['brand'] (see brand_watchlist.yaml),
      (3) rank <= rank_flag_max ['rank'], or (4) optionally match a
      configured TOPIC regex ['topic:<name>'] (see brand_watchlist.yaml) —
      the only one of the four rules that can keep a term because of what it
      *is*, regardless of whether you sell anything matching it, which is
      what makes market-research questions ("what's the whole market
      searching for in this product area") answerable at all. A term's FULL
      top-3 rows are kept together. Run this grain ALONE (see --only below)
      given its size.
  (c) GET_BRAND_ANALYTICS_MARKET_BASKET_REPORT -> amazon_ba_market_basket
      What else customers buy alongside your ASINs (co-purchase pairs).
  (d) GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT -> amazon_ba_repeat_purchase
      MONTHLY (weekly grain is too noisy to be useful). --month mode.

UNIT TRAP (probe-verified against the live API): these four reports return
shares/rates as FRACTIONS (0.0751) while SQP (amazon_sqp_sync.py) returns
PERCENT (14.29). Everything here is normalized to PERCENT (x100) at ingest and
named *_pct. Getting this backwards makes every share look 100x off.

WHICH ASINs ARE "OURS": grains (b) and (c) need to know which ASINs are yours
(to flag a term/pair as involving your own catalog). Like amazon_sqp_sync.py,
this scaffold has no product/catalog table of its own to source that from, so
you pass it explicitly via `--asins`/`--asins-file`; as a weak fallback,
amazon_rank_sync.fallback_asins() is used when neither is given (see that
module's docstring for its limitations).

OPTIONAL BRAND WATCHLIST: grain (b) can also flag a search term because it
contains one of your own brand names or a named competitor's, even when
neither of the other two rules apply. This reads an OPTIONAL
`brand_watchlist.yaml` in the project root (see that file for the format and
a placeholder example) — if it's missing, this feature is silently skipped and
grain (b) still runs on the 'ours'/'rank' rules alone.

OPTIONAL TOPIC CAPTURE (also in `brand_watchlist.yaml`): the three rules
above are all scoped to what's already yours — your ASINs, your watchlist, or
the marketplace's overall top-N. That's the right filter for tracking your
own position, but it makes demand for something you don't sell at all
invisible, since nothing about such a term matches you. `term_topics` is a
`{name: [regex, ...]}` map — any term matching one of a topic's regexes
(case-insensitive, OR'd together) is kept with `match_reason = 'topic:<name>'`
regardless of who sells it, and stores its full top-3 clicked ASINs — which,
for a topic term, names the *competitors* currently winning it. Matching is
counted per unique TERM (not per row) against `topic_max_rows_per_week`
(default 20,000 — the source report can run into the millions of rows
uncapped, so this cap is what keeps the ingest runnable at all), since a term
brings its whole top-3 with it and splitting the cap mid-term would store a
partial, misleading result.

USAGE:
  python amazon_ba_sync.py --asins-file asins.txt                     # prior BA week: (a)(b)(c)
  python amazon_ba_sync.py --asins-file asins.txt --week 2026-06-28
  python amazon_ba_sync.py --asins-file asins.txt --only search_catalog,market_basket
  python amazon_ba_sync.py --asins-file asins.txt --weeks 13 --only search_catalog,market_basket
  python amazon_ba_sync.py --month 2026-06                            # (d) repeat purchase
  python amazon_ba_sync.py --asins-file asins.txt --fallback-weeks 1  # weekly Monday-lag guard
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from amazon_rank_sync import fallback_asins
from warehouse import db as warehouse_db
from warehouse.brand_analytics import (
    run_ba_report, BAReportCancelled, BAReportFatal, CREATE_SPACING_SEC)

load_dotenv()
HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("WAREHOUSE_DB", HERE / "warehouse.db"))

# Default rank cutoff for keeping a Top Search Terms row purely on popularity
# (see sync_search_terms / _rank_max). Overridable via brand_watchlist.yaml.
DEFAULT_RANK_FLAG_MAX = 2500
# Default per-topic-per-week cap for the optional topic-capture rule (see
# sync_search_terms / _topic_cap). Overridable via brand_watchlist.yaml.
DEFAULT_TOPIC_MAX_ROWS_PER_WEEK = 20000

DDL = """
-- Brand Analytics per-ASIN search funnel (whole active catalog, one row/ASIN/wk).
-- *_pct columns are PERCENT (Amazon returns FRACTIONS; normalized x100 at ingest).
CREATE TABLE IF NOT EXISTS amazon_ba_search_catalog (
    week_start             TEXT NOT NULL,   -- SUNDAY of the Sun-Sat BA week
    asin                   TEXT NOT NULL,
    impressions            INTEGER,
    impression_median_price REAL,
    clicks                 INTEGER,
    click_rate_pct         REAL,            -- PERCENT
    click_median_price     REAL,
    cart_adds              INTEGER,
    purchases              INTEGER,
    search_sales           REAL,            -- SEARCH-driven revenue (searchTrafficSales)
    conversion_rate_pct    REAL,            -- PERCENT
    purchase_median_price  REAL,
    synced_at              TEXT NOT NULL,
    PRIMARY KEY (week_start, asin)
);
-- Market-wide top search terms, FILTERED on ingest (see module docstring).
-- clickShare/conversionShare are PERCENT here (Amazon FRACTIONS; x100 at ingest).
CREATE TABLE IF NOT EXISTS amazon_ba_search_terms (
    week_start            TEXT NOT NULL,   -- SUNDAY
    department            TEXT NOT NULL,
    search_term           TEXT NOT NULL,
    search_frequency_rank INTEGER,         -- RANK: 1 = most searched (lower is better)
    clicked_asin          TEXT NOT NULL,
    clicked_item_name     TEXT,
    click_share_rank      INTEGER NOT NULL, -- 1..3 within the term
    click_share_pct       REAL,            -- PERCENT
    conversion_share_pct  REAL,            -- PERCENT
    match_reason          TEXT,            -- 'ours' | 'brand' | 'rank' | 'topic:<name>'
    synced_at             TEXT NOT NULL,
    PRIMARY KEY (week_start, department, search_term, click_share_rank)
);
-- What else customers bought alongside an ASIN.
-- combination_pct is PERCENT (Amazon FRACTION; x100 at ingest).
CREATE TABLE IF NOT EXISTS amazon_ba_market_basket (
    week_start          TEXT NOT NULL,   -- SUNDAY
    asin                TEXT NOT NULL,
    purchased_with_asin TEXT NOT NULL,
    purchased_with_rank INTEGER,         -- 1..3
    combination_pct     REAL,            -- PERCENT
    is_ours             INTEGER,         -- 1 if purchased_with_asin is one of ours
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (week_start, asin, purchased_with_asin)
);
-- Repeat purchase behaviour, MONTHLY grain (weekly is noise).
-- *_pct columns are PERCENT (Amazon FRACTIONS; x100 at ingest).
CREATE TABLE IF NOT EXISTS amazon_ba_repeat_purchase (
    period_start         TEXT NOT NULL,
    period_end           TEXT,
    period_type          TEXT NOT NULL,   -- 'MONTH' (never mix period_types in one query)
    asin                 TEXT NOT NULL,
    orders               INTEGER,
    unique_customers     INTEGER,
    repeat_customers_pct REAL,            -- PERCENT
    repeat_revenue       REAL,
    repeat_revenue_pct   REAL,            -- PERCENT
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (period_start, period_type, asin)
);
"""


# ---- value helpers ---------------------------------------------------------

def _g(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _i(v):
    return 0 if v in (None, "") else int(v)


def _amt(d, *keys):
    """A {amount, currencyCode} money block -> float (None/absent -> 0.0)."""
    m = _g(d, *keys) if keys else d
    if isinstance(m, dict):
        return 0.0 if m.get("amount") in (None, "") else float(m["amount"])
    return 0.0 if m in (None, "") else float(m)


def _pct(frac):
    """FRACTION (0.0751) -> PERCENT (7.51), rounded. None-safe."""
    return None if frac in (None, "") else round(float(frac) * 100.0, 4)


# ---- optional brand watchlist (brand_watchlist.yaml) -----------------------

def _watchlist_config() -> dict:
    """Load brand_watchlist.yaml if present; {} if missing or PyYAML isn't
    installed. This feature is entirely optional — callers must tolerate {}."""
    try:
        import yaml
    except ImportError:
        return {}
    path = HERE / "brand_watchlist.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, ValueError):
        return {}


def _watchlist() -> list[str]:
    cfg = _watchlist_config()
    terms = list(cfg.get("house_brands", []) or []) + list(cfg.get("competitor_brands", []) or [])
    return [str(t).strip().lower() for t in terms if str(t).strip()]


def _rank_max() -> int:
    cfg = _watchlist_config()
    return int(cfg.get("rank_flag_max", DEFAULT_RANK_FLAG_MAX))


def _topics() -> dict[str, list[str]]:
    """Optional topic capture: {name: [regex, ...]} from brand_watchlist.yaml's
    `term_topics` section. Empty (feature off) if the key is absent or the
    file is missing — see the module docstring's "OPTIONAL TOPIC CAPTURE"."""
    raw = _watchlist_config().get("term_topics") or {}
    return {str(k): [str(p) for p in (v or [])] for k, v in raw.items()}


def _topic_cap() -> int:
    """Rows kept per topic per week. Top Search Terms can run into the
    millions of rows uncapped, so an unbounded topic filter is how this
    connector stops being runnable at all."""
    cfg = _watchlist_config()
    return int(cfg.get("topic_max_rows_per_week", DEFAULT_TOPIC_MAX_ROWS_PER_WEEK))


def _topic_regexes() -> dict[str, "re.Pattern"]:
    """name -> compiled regex. Each topic's patterns are OR'd together and
    matched case-insensitively against the normalized term."""
    out = {}
    for name, pats in _topics().items():
        pats = [p for p in pats if p]
        if pats:
            out[name] = re.compile("|".join(f"(?:{p})" for p in pats), re.I)
    return out


def _watch_regex(terms: list[str]):
    """Compile the watchlist into ONE word-boundary regex. Word boundaries are
    load-bearing: naive substring matching would mis-flag a short brand token
    that happens to sit inside an unrelated longer word. \\b + re.escape
    handles multi-word phrases. Returns None when the watchlist is empty."""
    terms = [t for t in terms if t]
    if not terms:
        return None
    # Longest-first so a multi-word phrase wins over a shorter substring of it.
    alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(r"\b(?:" + alt + r")\b")


def _target_asins(args: argparse.Namespace, conn: sqlite3.Connection) -> list[str]:
    """Resolve "our ASINs" for grains (b)/(c) — see the module docstring's
    "WHICH ASINs ARE 'OURS'" section. This scaffold has no product/catalog
    table to source that from on its own."""
    if args.asins:
        asins = [a.strip() for a in args.asins.split(",") if a.strip()]
    elif args.asins_file:
        asins = [ln.strip() for ln in Path(args.asins_file).read_text().splitlines() if ln.strip()]
    else:
        asins = fallback_asins(conn)
    return asins


# ---- (a) Search Catalog ----------------------------------------------------

def sync_search_catalog(conn, ba_sunday: date, stamp: str, our_asins: set[str]) -> int:
    week = ba_sunday.isoformat()
    recs = run_ba_report("GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT", ba_sunday,
                         options={"reportPeriod": "WEEK"})
    out = []
    for r in recs:
        asin = _g(r, "asin", "childAsin")
        if not asin:
            continue
        imp = r.get("impressionData") or {}
        clk = r.get("clickData") or {}
        cart = r.get("cartAddData") or {}
        pur = r.get("purchaseData") or {}
        out.append((
            week, asin,
            _i(_g(imp, "impressionCount", "count")),
            _amt(imp, "impressionMedianPrice", "medianPrice"),
            _i(_g(clk, "clickCount", "count")),
            _pct(_g(clk, "clickRate", "rate")),
            _amt(clk, "clickedMedianPrice", "clickMedianPrice", "medianPrice"),
            _i(_g(cart, "cartAddCount", "count")),
            _i(_g(pur, "purchaseCount", "count")),
            _amt(pur, "searchTrafficSales", "sales"),
            _pct(_g(pur, "conversionRate", "rate")),
            _amt(pur, "purchaseMedianPrice", "medianPrice"),
            stamp,
        ))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO amazon_ba_search_catalog VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    print(f"    ba_search_catalog {week}: {len(out)} ASIN rows", flush=True)
    return len(out)


# ---- (b) Search Terms (FILTERED) -------------------------------------------

def sync_search_terms(conn, ba_sunday: date, stamp: str, our_asins: set[str]) -> int:
    week = ba_sunday.isoformat()
    watch_re = _watch_regex(_watchlist())
    rank_max = _rank_max()
    topic_res = _topic_regexes()
    topic_cap = _topic_cap()
    topic_terms: dict[str, set] = {}
    topic_kept: dict[str, int] = {}
    topic_dropped: dict[str, int] = {}
    # Full market document (potentially very large). Parse once, filter in two
    # passes over the already-loaded list — run this grain ALONE (see --only).
    recs = run_ba_report("GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT", ba_sunday,
                         options={"reportPeriod": "WEEK"})
    print(f"    ba_search_terms {week}: {len(recs)} market records -> filtering", flush=True)

    def term_key(r):
        return (_g(r, "departmentName", "department") or "",
                _g(r, "searchTerm", "search_term") or "")

    # Pass 1: which terms have one of OUR ASINs in the top-3?
    ours_terms = set()
    for r in recs:
        if (_g(r, "clickedAsin", "clicked_asin") or "") in our_asins:
            ours_terms.add(term_key(r))

    # Pass 2: keep every row of a term matched by any rule.
    out = []
    for r in recs:
        dept, term = term_key(r)
        if not term:
            continue
        tk = (dept, term)
        reason = None
        if tk in ours_terms:
            reason = "ours"
        else:
            if watch_re is not None and watch_re.search(term.lower()):
                reason = "brand"
            else:
                rank = _g(r, "searchFrequencyRank", "search_frequency_rank")
                if rank is not None and int(rank) <= rank_max:
                    reason = "rank"
        if reason is None and topic_res:
            # TOPIC CAPTURE -- kept for WHAT THE TERM IS, not who sells it.
            # This is the only rule that can surface demand nobody here sells.
            low = term.lower()
            for name, rx in topic_res.items():
                if not rx.search(low):
                    continue
                # Counted per TERM, not per row: a term brings its whole
                # top-3 with it, and splitting those across the cap would
                # leave a term half-stored.
                if tk in topic_terms.setdefault(name, set()):
                    reason = f"topic:{name}"
                    break
                seen = topic_kept.get(name, 0)
                if seen >= topic_cap:
                    topic_dropped[name] = topic_dropped.get(name, 0) + 1
                    break
                topic_terms[name].add(tk)
                topic_kept[name] = seen + 1
                reason = f"topic:{name}"
                break
        if reason is None:
            continue
        casin = _g(r, "clickedAsin", "clicked_asin") or ""
        out.append((
            week, dept, term,
            None if _g(r, "searchFrequencyRank", "search_frequency_rank") is None
            else int(_g(r, "searchFrequencyRank", "search_frequency_rank")),
            casin, _g(r, "clickedItemName", "clicked_item_name"),
            _i(_g(r, "clickShareRank", "click_share_rank")),
            _pct(_g(r, "clickShare", "click_share")),
            _pct(_g(r, "conversionShare", "conversion_share")),
            reason, stamp,
        ))
    with conn:
        conn.execute("DELETE FROM amazon_ba_search_terms WHERE week_start=?", (week,))
        conn.executemany(
            "INSERT OR REPLACE INTO amazon_ba_search_terms VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    kept_terms = len({(o[1], o[2]) for o in out})
    print(f"    ba_search_terms {week}: kept {len(out)} rows ({kept_terms} terms)", flush=True)
    if topic_kept:
        for name, n in topic_kept.items():
            dropped = topic_dropped.get(name, 0)
            capped = f", {dropped} more term(s) dropped at the cap" if dropped else ""
            print(f"      topic:{name}: {n} term(s) kept{capped}", flush=True)
    return len(out)


# ---- (c) Market Basket -----------------------------------------------------

def sync_market_basket(conn, ba_sunday: date, stamp: str, our_asins: set[str]) -> int:
    week = ba_sunday.isoformat()
    recs = run_ba_report("GET_BRAND_ANALYTICS_MARKET_BASKET_REPORT", ba_sunday,
                         options={"reportPeriod": "WEEK"})
    out = []
    for r in recs:
        asin = _g(r, "asin")
        pw = _g(r, "purchasedWithAsin", "purchased_with_asin")
        if not asin or not pw:
            continue
        out.append((
            week, asin, pw,
            _i(_g(r, "purchasedWithRank", "purchased_with_rank", "rank")),
            _pct(_g(r, "combination", "combinationPct", "combinationPercentage",
                    "purchasedWithCombinationPercentage")),
            1 if pw in our_asins else 0,
            stamp,
        ))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO amazon_ba_market_basket VALUES (?,?,?,?,?,?,?)", out)
    print(f"    ba_market_basket {week}: {len(out)} pairs", flush=True)
    return len(out)


# ---- (d) Repeat Purchase (MONTHLY) -----------------------------------------

def sync_repeat_purchase(conn, month_start: date, month_end: date, stamp: str) -> int:
    recs = run_ba_report("GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT", month_start,
                         options={"reportPeriod": "MONTH"}, period="MONTH",
                         month_start=month_start, month_end=month_end)
    out = []
    for r in recs:
        asin = _g(r, "asin")
        if not asin:
            continue
        out.append((
            month_start.isoformat(), month_end.isoformat(), "MONTH", asin,
            _i(_g(r, "orders", "orderCount")),
            _i(_g(r, "uniqueCustomers", "unique_customers")),
            _pct(_g(r, "repeatCustomersPctTotal", "repeatCustomerPercentage")),
            _amt(r, "repeatPurchaseRevenue"),
            _pct(_g(r, "repeatPurchaseRevenuePctTotal", "repeatPurchaseRevenuePercentage")),
            stamp,
        ))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO amazon_ba_repeat_purchase VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    print(f"    ba_repeat {month_start}..{month_end}: {len(out)} ASIN rows", flush=True)
    return len(out)


# ---- orchestration ---------------------------------------------------------

WEEKLY_GRAINS = {
    "search_catalog": ("ba_search_catalog", sync_search_catalog),
    "search_terms": ("ba_search_terms", sync_search_terms),
    "market_basket": ("ba_market_basket", sync_market_basket),
}


def _prior_ba_sunday(today: date | None = None) -> date:
    today = today or date.today()
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    if last_sunday + timedelta(days=6) >= today:
        last_sunday -= timedelta(days=7)
    return last_sunday


def _month_bounds(ym: str) -> tuple[date, date]:
    y, m = (int(x) for x in ym.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def _run_grain(conn, name: str, ba_sunday: date, stamp: str,
               fallback_weeks: int, our_asins: set[str]) -> int:
    platform, fn = WEEKLY_GRAINS[name]
    started = warehouse_db.now()
    wk = ba_sunday
    try:
        n = fn(conn, wk, stamp, our_asins)
        fb = 0
        while n == 0 and fb < fallback_weeks:
            fb += 1
            wk = wk - timedelta(weeks=1)
            print(f"    {platform}: falling back to prior BA week {wk}", flush=True)
            n = fn(conn, wk, stamp, our_asins)
        warehouse_db.log_sync(platform, started, n, "ok" if n else "error")
        return n
    except BAReportCancelled as e:
        warehouse_db.log_sync(platform, started, 0, "error", str(e)[:150])
        print(f"    {platform}: CANCELLED — {e}", flush=True)
    except (BAReportFatal, Exception) as e:  # noqa: BLE001 — one grain must not kill the batch
        warehouse_db.log_sync(platform, started, 0, "error", str(e)[:150])
        print(f"    {platform}: FAILED — {str(e)[:150]}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--asins", help="comma-separated ASINs that are 'ours' (for grains b/c)")
    p.add_argument("--asins-file", help="path to a file with one ASIN per line")
    p.add_argument("--week", help="SUNDAY of the BA week (default: last completed Sun-Sat)")
    p.add_argument("--weeks", type=int, default=1, help="backfill this many BA weeks")
    p.add_argument("--only", help="comma list of grains: search_catalog,search_terms,market_basket")
    p.add_argument("--month", help="YYYY-MM: run Repeat Purchase for this month (grain d)")
    p.add_argument("--last-month", action="store_true",
                   help="run Repeat Purchase for the PREVIOUS calendar month (monthly cron)")
    p.add_argument("--fallback-weeks", type=int, default=0,
                   help="if a grain returns 0 rows, step back up to this many BA weeks")
    args = p.parse_args()

    warehouse_db.init_db()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(DDL)
    conn.row_factory = None
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.last_month and not args.month:
        first_this = date.today().replace(day=1)
        last_prev = first_this - timedelta(days=1)
        args.month = f"{last_prev.year}-{last_prev.month:02d}"
    if args.month:
        ms, me = _month_bounds(args.month)
        started = warehouse_db.now()
        failed = False
        try:
            n = sync_repeat_purchase(conn, ms, me, stamp)
            warehouse_db.log_sync("ba_repeat", started, n, "ok" if n else "error")
            failed = n == 0
        except Exception as e:  # noqa: BLE001
            failed = True
            warehouse_db.log_sync("ba_repeat", started, 0, "error", str(e)[:150])
            print(f"    ba_repeat: FAILED — {str(e)[:150]}", flush=True)
        conn.close()
        return 1 if failed else 0

    grains = [g.strip() for g in args.only.split(",")] if args.only else list(WEEKLY_GRAINS)
    for g in grains:
        if g not in WEEKLY_GRAINS:
            raise SystemExit(f"unknown grain {g!r}; choose from {list(WEEKLY_GRAINS)}")

    base = date.fromisoformat(args.week) if args.week else _prior_ba_sunday()
    if base.weekday() != 6:
        raise SystemExit("--week must be a SUNDAY (Brand Analytics weeks are Sun-Sat).")

    our_asins = set(_target_asins(args, conn))

    failures = 0
    landed = 0
    for i in range(args.weeks):
        ba_sunday = base - timedelta(weeks=i)
        for j, g in enumerate(grains):
            if i or j:
                time.sleep(CREATE_SPACING_SEC)  # space sequential createReport calls
            n = _run_grain(conn, g, ba_sunday, stamp, args.fallback_weeks, our_asins)
            failures += n == 0
            landed += n
    conn.close()
    if failures and landed:
        print(f"    ba: DEGRADED - {failures} grain(s) returned no rows, "
              f"{landed} row(s) landed overall", flush=True)
    # Nonzero only when NOTHING landed across every grain and week. One flaky
    # grain must not mark the whole run failed -- per-grain errors are already
    # in sync_log.
    return 1 if not landed else 0


if __name__ == "__main__":
    raise SystemExit(main())
