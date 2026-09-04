"""
Google Search Console -> organic search performance.

WHY THIS EXISTS. Most of the connectors in this project cover PAID channels
(Google/Meta/Amazon Ads, TikTok). Nothing here covers organic search — what
people typed to find your site without you paying for the click — unless you
already have a paid-search connector's "paid vs. organic" view for the exact
same query, which typically rejects money metrics entirely and only exists at
all for queries that ALSO ran a paid ad. This connector is the only source of
plain organic search performance in this project: clicks, impressions, CTR,
and average position, at site, query, and landing-page grain.

AUTH NEEDS NO NEW CREDENTIAL TYPE if you already run the GA4 connector — Search
Console's API accepts the same kind of Google service-account JSON key, just
with a different OAuth scope. You can reuse the exact same key file GA4 uses,
provided you also complete the property grant below; a dedicated
`SEARCH_CONSOLE_CREDENTIALS_FILE` variable is used here for the same reason
Google Merchant Center gets its own `GMC_CREDENTIALS_FILE` instead of silently
reading `GA4_CREDENTIALS_FILE` — the two APIs are unrelated and shouldn't be
implicitly coupled just because it's convenient to point them at one file.

SETUP has one step with no API equivalent: the service account must be added
as a **user on the Search Console property itself** (Search Console > Settings
> Users and permissions > Add user; "Restricted" permission is enough for
read-only reporting). There is no API call that grants this — it's a one-time
UI action, same shape as Merchant Center's `registerGcp` step in
`merchant_center_sync.py`. If every request starts failing with
"not a verified Search Console site in this account" (401/403/404), that grant
was removed — re-add the service account's email, which prints when you run
`--probe`.

PROPERTY TYPE: a **domain property** (`sc-domain:example.com`, verified via DNS
in Search Console) already spans every subdomain and protocol variant
(http/https/www/non-www) as one property, so there's nothing to merge across
several properties the way you might need to with older URL-prefix properties.
Set `SEARCH_CONSOLE_SITE` to whatever property string Search Console shows you
under Settings — `--probe` lists every property the service account can see
and marks which one is configured.

=============================== THE TRAPS ===============================

(1) !! THE 5,000-ROW CAP IS PER DAY, NOT PER REQUEST !! `rowLimit` on the
    Search Analytics API tops out at 25,000, but the number of DISTINCT ROWS
    the query-dimension grain can return is separately capped at 5,000 **per
    calendar day**, regardless of `rowLimit`. Asking for `startRow=5000` on a
    single day returns zero rows even though `rowLimit` says 25,000 more
    should fit. Consequently, this connector issues query/page-grain requests
    **one day at a time** — a multi-day request would silently truncate every
    day inside it down to whatever fraction of the 5,000-row cap it happened
    to spend on that day.

(2) PAGINATION MUST STOP ON AN **EMPTY** PAGE, NEVER A SHORT ONE. A capped day
    legitimately returns exactly 5,000 rows against a `rowLimit` of 25,000 —
    "short means done" is a real bug class (it's silently truncated other
    high-volume daily feeds in this project before), so `query_rows()` keeps
    paging until a page comes back with zero rows, at the cost of one
    extra, empty-bodied request per pull.

(3) YOUR QUERY-GRAIN PULL IS A SUBSET OF THE SITE TOTAL, NEVER THE WHOLE THING
    — and no pull depth recovers the rest. Google anonymizes rare/personal
    search queries entirely; they never appear at query grain at all, however
    many rows you request or how many dimensions you add. Run `--probe` (or
    compare `search_console_queries` clicks against `search_console_daily`
    for the same day/site/search_type) to measure your own site's recovery
    rate before treating a query-level SUM as authoritative — use
    `search_console_daily` as the denominator for anything that needs to
    reconcile to a real site total. Adding a second dimension (this connector
    adds `device`) does not close that click gap, but is still worth doing:
    it recovers many more DISTINCT queries and impressions than `query` alone
    — the "we rank for it, but almost nobody clicks" tail, which is exactly
    the kind of query worth knowing about even at zero clicks.

(4) PAGE-GRAIN IMPRESSIONS DOUBLE-COUNT AND MUST NEVER BE SUMMED TO A SITE
    TOTAL. One search result that shows several of your URLs (e.g. sitelinks,
    or several products from the same query) is ONE site-level impression but
    N page-level impressions — summing `search_console_pages.impressions`
    across pages routinely runs well over 100% of the true site total. Clicks
    do NOT have this problem at page grain. `country` and `device` dimensions
    also reconcile cleanly to the site total — page is the one dimension to
    treat as "real at its own grain, a lie the moment you add it up."

(5) `dataState=all` RETURNS PARTIAL, NEVER-CORRECTED RECENT DAYS. Search
    Console has a finalization lag of roughly 1-3 days on `dataState=final`
    (the default and recommended choice); `all` additionally includes
    still-accumulating recent days, and if you store that partial number
    without ever re-pulling the day once it settles, it stays wrong forever
    — the same failure mode as any feed that gets pulled once and never
    revisited. `data_state` is recorded on every row here but **deliberately
    excluded from every table's primary key**, so a later `final` pull
    overwrites an earlier `all`/partial row in place instead of both rows
    coexisting and double-counting any SUM. If you ever need this, a test
    should pin that behavior explicitly — it's easy to mistake for a modeling
    choice rather than a correctness requirement.

(6) RETENTION IS A ROLLING WINDOW THAT SLIDES FORWARD EVERY DAY, AND THE LOSS
    IS AT THE **OLD** END. This is the opposite failure mode from most
    snapshot-style feeds in this project (an inventory level, a sales-rank
    reading), which lose history only if nobody runs the sync for a while.
    Google's Search Console UI/API has historically supported roughly 16
    months of history, but the exact floor can vary by property and has
    changed over time — verify it for your own property (`--probe`, or binary
    search a `--start` date until it returns zero rows) rather than trusting
    a hardcoded number. Requesting a date older than the floor is **not** an
    error: it returns HTTP 200 with zero rows, so an over-wide backfill window
    is harmless, but a *missed* window is a **permanent** loss — the old end
    ages out whether or not this connector ever runs. `missing_settled_days()`
    computes its own floor from the data already stored (`MIN(date)` with
    `impressions > 0`) rather than trusting a hardcoded retention constant, so
    a gap check doesn't alarm on the slack between the assumed and real floor,
    and also doesn't silently stop catching a genuine gap either.

(7) SEARCH TYPES OTHER THAN `web` ARE USUALLY NEAR-EMPTY BUT CHEAP TO CHECK.
    The API's implicit default is exactly `web`; `image`/`video`/`news`/
    `discover` traffic is typically a small fraction of `web` but can be
    worth having (image search, in particular, can carry meaningful
    impression volume even with few clicks) — this connector pulls all five
    at the cheap site-total grain and only pulls the expensive, paginated
    query/page grain for `web`.

(8) THE OPTIONAL `query_pages` GRAIN IS THE ONLY BRIDGE FROM A SEARCH TERM TO
    A SPECIFIC LANDING PAGE, AND ITS IMPRESSIONS DOUBLE-COUNT IN **BOTH**
    DIRECTIONS. Neither `queries` nor `pages` alone can answer "what did
    people search to land on this exact page" — `queries` carries no page,
    `pages` carries no query text. `query_pages` (dims `[date, query, page]`)
    closes that gap and, measured against a real site, recovered a noticeably
    BETTER share of the day's clicks than `query`+`device` alone (a third
    dimension gives the API another way to avoid the per-day row cap in trap
    (1)) — worth confirming for your own property with `--probe` plus a spot
    comparison against `search_console_daily`. The cost is that
    `impressions` on this grain double-counts worse than the page-grain trap
    in (4), and in both directions at once: summing across a query's pages
    inflates (one query landing on N URLs is one site impression but N
    query-page impressions), and summing across a page's queries inflates
    too. `clicks` stays safe to sum. Always use `search_console_daily` as the
    impressions denominator, never this table. `device` is deliberately NOT a
    fourth dimension here — it would multiply an already-large row count for
    a split `queries` already carries exactly on its own. This grain is
    noticeably more expensive than `queries`/`pages` (routinely several times
    the row count per day, since a query can land on many pages), so it is
    opt-in: run `--only query_pages` or include it via `--only` alongside the
    others rather than defaulting it into every run. It is worth the cost
    specifically if you need to join organic search demand to a product/
    landing-page catalog by URL — measured against a real product catalog,
    the URLs recovered here matched by exact handle/slug far more reliably
    than trying to resolve the same products through an analytics platform's
    own opaque item-id space.

Search queries can contain non-Latin/non-ASCII text — anything that prints
them needs a UTF-8-safe console/log, or a real query will eventually crash a
report script that assumes cp1252 or similar.

CRASH SAFETY — a killed run loses at most the day in flight, never the whole
walk. Each day commits its rows together with its own
`search_console_coverage` row in the SAME transaction, so a coverage row can
never claim a day whose rows failed to commit. A plain re-run resumes,
skipping any day already pulled `final`; absence of *rows* is never treated as
the resume signal on its own (a day can legitimately return zero rows if
unsettled or aged out of retention, and would otherwise be re-requested
forever) — only the coverage row's presence, with the right `data_state`, is.
`--refresh` forces a full re-fetch ignoring stored coverage. Coverage is also
SEEDED from any rows already present, so pointing this at a warehouse that
predates the coverage table does not re-fetch history it already holds.

USAGE:
    search_console_sync.py                       # rolling 5-day window (daily run)
    search_console_sync.py --days 30
    search_console_sync.py --start 2026-01-01 --end 2026-01-31
    search_console_sync.py --backfill            # retention floor -> yesterday
    search_console_sync.py --backfill --refresh  # ignore coverage, re-pull all
    search_console_sync.py --only queries --only pages
    search_console_sync.py --only query_pages     # opt-in, see trap (8)
    search_console_sync.py --probe               # reachability, writes nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db

load_dotenv()

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WAREHOUSE_DB", ROOT / "warehouse.db"))

CREDENTIALS_FILE = os.environ.get("SEARCH_CONSOLE_CREDENTIALS_FILE", "")
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# No sensible default — this identifies YOUR property. See the module
# docstring for the domain-property-vs-URL-prefix-property distinction.
SITE = os.environ.get("SEARCH_CONSOLE_SITE", "")
API_ROOT = "https://searchconsole.googleapis.com/webmasters/v3"

GRAINS = ("daily", "queries", "pages", "query_pages")

# Cheap grain — one request per type for the WHOLE window, so all five are
# affordable even though most are near-empty for a typical site.
DAILY_TYPES = ("web", "image", "news", "video", "discover")
# Expensive grains — per day, paginated. Only the type that usually carries
# real volume.
DETAIL_TYPES = ("web",)

QUERY_DIMS = ["date", "query", "device"]
PAGE_DIMS = ["date", "page"]
# The only dimension set that joins a search term to a specific landing page.
# See trap (8): opt-in, more expensive than the two grains above, deliberately
# no `device` split.
QUERY_PAGE_DIMS = ["date", "query", "page"]

ROW_LIMIT = 25000          # API maximum per request
MAX_PAGES = 40             # a page count this high would mean something
                           # structurally unusual is going on — see trap (2)

# A rolling window, commonly cited around 16 months but not guaranteed to
# stay that way — verify with --probe for your own property. Deliberately set
# a little above the expected floor: over-reaching costs nothing (an
# out-of-range request returns 200 with zero rows), while under-reaching
# silently abandons history that can never be re-fetched once it ages out.
RETENTION_DAYS = 510
# Days more recent than this are not expected to be `final` yet, so their
# absence must NOT be reported as a gap.
FINAL_LAG_DAYS = 3

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 6


class SearchConsoleError(RuntimeError):
    """Permanent failure — retrying is pure waste."""


class SearchConsoleTransient(RuntimeError):
    """Transient failure that survived the retry budget."""


DDL = """
-- AUTHORITATIVE site totals. One row per day per search type. These are the
-- ONLY numbers here that reconcile to 100% — use this table as the denominator
-- for anything computed off search_console_queries / _pages.
CREATE TABLE IF NOT EXISTS search_console_daily (
    date        TEXT NOT NULL,
    site        TEXT NOT NULL,
    search_type TEXT NOT NULL,      -- web | image | news | video | discover
    clicks      INTEGER,
    impressions INTEGER,
    ctr         REAL,               -- a RATIO: average it, never sum it
    position    REAL,               -- average position; ratio-like, never sum
    data_state  TEXT NOT NULL,      -- final | all ('all' = PARTIAL, see docstring)
    synced_at   TEXT NOT NULL,
    -- data_state is deliberately NOT in the PK: a later `final` pull must
    -- OVERWRITE an earlier partial `all` row rather than sit beside it.
    PRIMARY KEY (date, site, search_type)
);
CREATE INDEX IF NOT EXISTS idx_sc_daily_date ON search_console_daily(date);

-- Organic performance per query per device per day.
-- !! A SUBSET, NOT A TOTAL: some fraction of clicks/impressions, never all of
-- them. The rest sits on queries Google anonymizes and is UNAVAILABLE at any
-- pull depth. Never sum this into a site figure — join search_console_daily
-- for that. See the module docstring.
CREATE TABLE IF NOT EXISTS search_console_queries (
    date        TEXT NOT NULL,
    site        TEXT NOT NULL,
    query       TEXT NOT NULL,      -- may contain non-Latin text; UTF-8
    device      TEXT NOT NULL,      -- DESKTOP | MOBILE | TABLET
    search_type TEXT NOT NULL,
    clicks      INTEGER,
    impressions INTEGER,
    ctr         REAL,
    position    REAL,
    data_state  TEXT NOT NULL,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (date, site, query, device, search_type)
);
CREATE INDEX IF NOT EXISTS idx_sc_q_date  ON search_console_queries(date);
CREATE INDEX IF NOT EXISTS idx_sc_q_query ON search_console_queries(query);

-- Organic performance per landing page per day.
-- !! `impressions` HERE DOUBLE-COUNTS: one query showing several of your URLs
-- is one SITE impression but N PAGE impressions. Clicks are safe; impressions
-- are valid per-page and a lie when added up. See the module docstring.
CREATE TABLE IF NOT EXISTS search_console_pages (
    date        TEXT NOT NULL,
    site        TEXT NOT NULL,
    page        TEXT NOT NULL,
    search_type TEXT NOT NULL,
    clicks      INTEGER,
    impressions INTEGER,            -- NOT SUMMABLE across pages
    ctr         REAL,
    position    REAL,
    data_state  TEXT NOT NULL,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (date, site, page, search_type)
);
CREATE INDEX IF NOT EXISTS idx_sc_p_date ON search_console_pages(date);
CREATE INDEX IF NOT EXISTS idx_sc_p_page ON search_console_pages(page);

-- Which search query landed on which page — the only grain here that bridges
-- organic demand to a specific landing page/product. See trap (8): opt-in
-- (not in every default run), noticeably higher row count than the two
-- tables above, and its `impressions` double-counts in BOTH directions (sum
-- across a query's pages, or across a page's queries, and either inflates).
-- `clicks` is safe to sum; `impressions` never is — use search_console_daily
-- for that. No `device` dimension — see trap (8) for why.
CREATE TABLE IF NOT EXISTS search_console_query_pages (
    date        TEXT NOT NULL,
    site        TEXT NOT NULL,
    query       TEXT NOT NULL,      -- may contain non-Latin text; UTF-8
    page        TEXT NOT NULL,      -- full URL
    search_type TEXT NOT NULL,
    clicks      INTEGER,
    impressions INTEGER,            -- NOT SUMMABLE in either direction
    ctr         REAL,
    position    REAL,
    data_state  TEXT NOT NULL,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (date, site, query, page, search_type)
);
CREATE INDEX IF NOT EXISTS idx_sc_qp_date  ON search_console_query_pages(date);
CREATE INDEX IF NOT EXISTS idx_sc_qp_page  ON search_console_query_pages(page);
CREATE INDEX IF NOT EXISTS idx_sc_qp_query ON search_console_query_pages(query);

-- What was actually ASKED FOR, so a killed backfill resumes instead of
-- re-fetching hundreds of days. Written in the SAME transaction as that
-- day's rows, so coverage can never claim a day whose data did not commit.
-- A day is only recorded as settled coverage when it was pulled `final` AND
-- was already past the finalization lag — otherwise it must be re-pulled as
-- it settles, which is the whole point of the rolling window.
CREATE TABLE IF NOT EXISTS search_console_coverage (
    date        TEXT    NOT NULL,
    site        TEXT    NOT NULL,
    grain       TEXT    NOT NULL,   -- queries | pages | query_pages
    rows_stored INTEGER NOT NULL,
    data_state  TEXT    NOT NULL,
    was_settled INTEGER NOT NULL,   -- 1 = past the finalization lag when fetched
    fetched_at  TEXT    NOT NULL,
    PRIMARY KEY (date, site, grain)
);
"""


# --------------------------------------------------------------------------
# Auth / transport
# --------------------------------------------------------------------------
def build_session():
    """AuthorizedSession re-mints the ~1h token on its own — required for a
    multi-hour backfill, where a manually-held bearer would expire mid-walk."""
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise SearchConsoleError(f"google-auth is not installed: {exc}") from exc

    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    return AuthorizedSession(creds)


def _post(session, path: str, payload: dict, label: str = "") -> dict:
    url = f"{API_ROOT}/sites/{urllib.parse.quote(SITE, safe='')}{path}"
    delay = 5.0
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(url, json=payload, timeout=180)
        except (requests.RequestException, TimeoutError, OSError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise SearchConsoleTransient(f"{label or path}: {exc}") from exc
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            return resp.json()

        detail = resp.text[:400]
        if resp.status_code in (401, 403, 404):
            # Almost always the property grant, not a transient — say so
            # plainly instead of burning the retry budget on it.
            raise SearchConsoleError(
                f"{label or path}: HTTP {resp.status_code} {detail}\n"
                f"  Check that the service account is still a user on {SITE} "
                f"in Search Console (Settings > Users and permissions)."
            )
        if resp.status_code not in TRANSIENT_STATUS:
            raise SearchConsoleError(f"{label or path}: HTTP {resp.status_code} {detail}")
        if attempt == MAX_RETRIES - 1:
            raise SearchConsoleTransient(
                f"{label or path}: HTTP {resp.status_code} {detail}"
            )
        time.sleep(delay)
        delay *= 2
    raise SearchConsoleTransient(label or path)


def query_rows(session, *, start: dt.date, end: dt.date, dimensions: list[str],
               search_type: str, data_state: str, label: str = "") -> list[dict]:
    """Paginate one searchAnalytics query to exhaustion.

    STOPS ON AN EMPTY PAGE, not on a short one. A short page is NOT proof of the
    end here: the per-day query cap returns exactly 5,000 against a rowLimit of
    25,000 (see trap (1)/(2) in the module docstring), so treating short as
    final would silently truncate high-volume days.
    """
    rows: list[dict] = []
    start_row = 0
    for _ in range(MAX_PAGES):
        payload = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dimensions,
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "type": search_type,
            "dataState": data_state,
        }
        batch = _post(session, "/searchAnalytics/query", payload, label=label).get("rows", [])
        if not batch:
            return rows
        rows.extend(batch)
        start_row += len(batch)
    print(f"    WARNING: {label} hit MAX_PAGES ({MAX_PAGES}) at {start_row} rows.")
    return rows


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def _metrics(r: dict) -> tuple:
    return (r.get("clicks", 0), r.get("impressions", 0),
            r.get("ctr", 0.0), r.get("position", 0.0))


def _keys(r: dict, n: int) -> list:
    keys = list(r.get("keys") or [])
    return (keys + [""] * n)[:n]


def store_daily(conn, session, start: dt.date, end: dt.date, data_state: str,
                stamp: str) -> int:
    """One request per search type for the WHOLE window — `date` as a dimension
    returns every day at once, so this grain is cheap regardless of span."""
    written = 0
    for stype in DAILY_TYPES:
        rows = query_rows(session, start=start, end=end, dimensions=["date"],
                          search_type=stype, data_state=data_state,
                          label=f"daily/{stype}")
        payload = [(_keys(r, 1)[0], SITE, stype, *_metrics(r), data_state, stamp)
                   for r in rows]
        if payload:
            conn.executemany(
                "INSERT OR REPLACE INTO search_console_daily "
                "(date, site, search_type, clicks, impressions, ctr, position, "
                " data_state, synced_at) VALUES (?,?,?,?,?,?,?,?,?)", payload)
            conn.commit()
        written += len(payload)
        print(f"    {stype:<9} {len(payload):>5} day-rows", flush=True)
    return written


TABLE_FOR_GRAIN = {
    "queries": "search_console_queries",
    "pages": "search_console_pages",
    "query_pages": "search_console_query_pages",
}


def seed_coverage_from_data(conn, grain: str) -> int:
    """Back-fill the coverage table from days already stored.

    Makes resume self-healing: a run started before the coverage table existed
    (or one whose coverage was lost) does not re-fetch history it already holds.
    Only days that are BOTH settled and non-empty are seeded — an unsettled day
    still needs re-pulling as it finalizes, and an empty one was never proof of
    anything.
    """
    table = TABLE_FOR_GRAIN[grain]
    settled = _settled_through().isoformat()
    return conn.execute(
        f"""
        INSERT OR IGNORE INTO search_console_coverage
            (date, site, grain, rows_stored, data_state, was_settled, fetched_at)
        SELECT date, site, ?, COUNT(*), 'final', 1, ?
          FROM {table}
         WHERE site = ? AND date <= ? AND data_state = 'final'
         GROUP BY date, site
        """,
        (grain, warehouse_db.now(), SITE, settled),
    ).rowcount


def covered_days(conn, grain: str) -> set[str]:
    """Days that need no re-fetch: pulled `final`, after they had settled."""
    return {
        r[0] for r in conn.execute(
            "SELECT date FROM search_console_coverage "
            "WHERE site = ? AND grain = ? AND data_state = 'final' AND was_settled = 1",
            (SITE, grain),
        )
    }


def store_detail(conn, session, days: list[dt.date], grain: str, data_state: str,
                 stamp: str, *, refresh: bool = False) -> int:
    """Queries and pages, ONE DAY PER REQUEST (the 5,000 cap is per day), each
    day committed on its own — WITH its coverage row in the same transaction —
    so a crash costs one day, never the walk, and a re-run resumes."""
    if grain == "queries":
        table = "search_console_queries"
        dims, n_keys = QUERY_DIMS, 3
        cols = ("(date, site, query, device, search_type, clicks, impressions, "
                "ctr, position, data_state, synced_at)")
        placeholders = "?,?,?,?,?,?,?,?,?,?,?"
    elif grain == "query_pages":
        table = "search_console_query_pages"
        dims, n_keys = QUERY_PAGE_DIMS, 3
        cols = ("(date, site, query, page, search_type, clicks, impressions, "
                "ctr, position, data_state, synced_at)")
        placeholders = "?,?,?,?,?,?,?,?,?,?,?"
    else:
        table = "search_console_pages"
        dims, n_keys = PAGE_DIMS, 2
        cols = ("(date, site, page, search_type, clicks, impressions, "
                "ctr, position, data_state, synced_at)")
        placeholders = "?,?,?,?,?,?,?,?,?,?"

    settled_through = _settled_through()
    if not refresh:
        seeded = seed_coverage_from_data(conn, grain)
        conn.commit()
        if seeded:
            print(f"    (seeded {seeded} coverage rows from data already stored)",
                  flush=True)
    done = set() if refresh else covered_days(conn, grain)
    if done:
        skipping = sum(1 for d in days if d.isoformat() in done)
        if skipping:
            print(f"    resuming: {skipping} of {len(days)} day(s) already "
                  f"complete, skipping", flush=True)

    written = 0
    for day in days:
        if day.isoformat() in done:
            continue
        for stype in DETAIL_TYPES:
            rows = query_rows(session, start=day, end=day, dimensions=dims,
                              search_type=stype, data_state=data_state,
                              label=f"{grain}/{stype}/{day}")
            was_settled = 1 if day <= settled_through else 0
            payload = []
            for r in rows:
                k = _keys(r, n_keys)
                if grain == "queries":
                    # keys = date, query, device
                    payload.append((k[0], SITE, k[1], k[2], stype,
                                    *_metrics(r), data_state, stamp))
                elif grain == "query_pages":
                    # keys = date, query, page
                    payload.append((k[0], SITE, k[1], k[2], stype,
                                    *_metrics(r), data_state, stamp))
                else:
                    # keys = date, page
                    payload.append((k[0], SITE, k[1], stype,
                                    *_metrics(r), data_state, stamp))
            if payload:
                conn.executemany(
                    f"INSERT OR REPLACE INTO {table} {cols} VALUES ({placeholders})",
                    payload)
            # Coverage rides the SAME transaction as the rows: it can never
            # claim a day whose data failed to commit. Unsettled days are
            # recorded too (was_settled=0) but never suppress a re-pull.
            conn.execute(
                "INSERT OR REPLACE INTO search_console_coverage "
                "(date, site, grain, rows_stored, data_state, was_settled, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (day.isoformat(), SITE, grain, len(payload), data_state,
                 was_settled, stamp))
            conn.commit()
            written += len(payload)
            pending = "  (not final yet)" if not was_settled and not payload else ""
            print(f"    {day} {stype:<5} {grain:<7} {len(payload):>6} rows{pending}",
                  flush=True)
    return written


# --------------------------------------------------------------------------
# Dates / coverage
# --------------------------------------------------------------------------
def _settled_through() -> dt.date:
    return dt.date.today() - dt.timedelta(days=FINAL_LAG_DAYS)


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    out, day = [], start
    while day <= end:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


def retention_floor() -> dt.date:
    return dt.date.today() - dt.timedelta(days=RETENTION_DAYS)


def missing_settled_days(conn, days: list[dt.date]) -> list[str]:
    """Days that MUST be final by now yet stored nothing.

    A real zero-traffic day is implausible for a site with any meaningful
    organic volume, so absence here usually means a genuine gap — and a gap
    should log `degraded`, never `ok`. Two classes of day are excluded, and
    both matter:

      * Days inside the finalization lag. They are expected to be empty, and
        alarming on them would make the status meaningless within a week.
      * Days below the OBSERVED floor — the earliest date the API has ever
        actually returned data for. RETENTION_DAYS deliberately over-reaches
        the real floor for slack, so the assumed floor always sits a few days
        below the real one; flagging that slack would make every backfill log
        `degraded` for history that has simply aged out and can never be
        fetched again. The real floor also slides FORWARD daily, so it has to
        be read from the data rather than hardcoded.
    """
    floor = retention_floor()
    settled = [d for d in days if floor <= d <= _settled_through()]
    if not settled:
        return []
    observed = conn.execute(
        "SELECT MIN(date) FROM search_console_daily WHERE site = ? AND impressions > 0",
        (SITE,),
    ).fetchone()[0]
    if observed:
        settled = [d for d in settled if d.isoformat() >= observed]
        if not settled:
            return []
    rows = conn.execute(
        "SELECT date FROM search_console_daily "
        "WHERE site = ? AND search_type = 'web' AND date BETWEEN ? AND ? "
        "AND impressions > 0",
        (SITE, settled[0].isoformat(), settled[-1].isoformat()),
    ).fetchall()
    have = {r[0] for r in rows}
    return [d.isoformat() for d in settled if d.isoformat() not in have]


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------
def probe() -> None:
    print(f"credentials : {CREDENTIALS_FILE}")
    if not CREDENTIALS_FILE or not Path(CREDENTIALS_FILE).exists():
        print("  MISSING -- connector would skip cleanly.")
        return
    info = json.loads(Path(CREDENTIALS_FILE).read_text(encoding="utf-8"))
    print(f"  service account: {info.get('client_email')}")
    session = build_session()

    resp = session.get(f"{API_ROOT}/sites", timeout=60)
    print(f"\nlist sites -> HTTP {resp.status_code}")
    entries = resp.json().get("siteEntry", []) if resp.status_code == 200 else []
    for s in entries:
        marker = "  <-- configured" if s["siteUrl"] == SITE else ""
        print(f"   {s['permissionLevel']:>22}  {s['siteUrl']}{marker}")
    if not any(s["siteUrl"] == SITE for s in entries):
        print(f"\n  !! {SITE!r} is NOT accessible to this service account.")
        print(f"     Add {info.get('client_email')} under Search Console > "
              f"Settings > Users and permissions.")
        return

    end = _settled_through()
    rows = query_rows(session, start=end - dt.timedelta(days=6), end=end,
                      dimensions=["date"], search_type="web", data_state="final",
                      label="probe")
    print("\nlast 7 settled days (type=web, dataState=final):")
    for r in rows:
        print(f"   {r['keys'][0]}  clicks={r['clicks']:>7}  impr={r['impressions']:>9}"
              f"  pos={r.get('position', 0):.1f}")
    print(f"\nretention floor (assumed): {retention_floor()}  "
          f"({RETENTION_DAYS} days of slack over a commonly-cited ~16-month "
          f"window — verify against your own property; it slides daily)")
    print("Probe complete -- nothing written.")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> None:
    global SITE

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--days", type=int, default=5,
                        help="rolling window ending yesterday (default 5; the "
                             "overlap re-pulls days as they finalize)")
    parser.add_argument("--start", help="window start YYYY-MM-DD")
    parser.add_argument("--end", help="window end YYYY-MM-DD")
    parser.add_argument("--backfill", action="store_true",
                        help="retention floor -> yesterday (~16 months)")
    parser.add_argument("--only", choices=GRAINS, action="append",
                        help="limit to these grains (repeatable)")
    parser.add_argument("--data-state", choices=("final", "all"), default="final",
                        help="'all' includes TODAY as a PARTIAL day -- see docstring")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore stored coverage and re-fetch every day in "
                             "the window (default: resume, skipping complete days)")
    parser.add_argument("--site", help=f"Search Console property (default {SITE!r})")
    parser.add_argument("--probe", action="store_true",
                        help="report reachability and write nothing")
    args = parser.parse_args()

    if args.site:
        SITE = args.site

    if not CREDENTIALS_FILE or not SITE:
        missing = [n for n, v in (("SEARCH_CONSOLE_CREDENTIALS_FILE", CREDENTIALS_FILE),
                                   ("SEARCH_CONSOLE_SITE", SITE)) if not v]
        print(f"{', '.join(missing)} not set -- skipping Search Console sync. "
              f"See .env.example.")
        return

    if not Path(CREDENTIALS_FILE).exists():
        print(f"{CREDENTIALS_FILE} not found -- skipping Search Console sync.")
        return

    if args.probe:
        probe()
        return

    grains = tuple(args.only) if args.only else GRAINS

    end = (dt.date.fromisoformat(args.end) if args.end
           else dt.date.today() - dt.timedelta(days=1))
    if args.backfill:
        start = retention_floor()
    elif args.start:
        start = dt.date.fromisoformat(args.start)
    else:
        start = end - dt.timedelta(days=args.days - 1)

    if start < retention_floor():
        print(f"NOTE: {start} is below the ~{RETENTION_DAYS}-day retention floor "
              f"({retention_floor()}). Out-of-range days return 200 with zero rows, "
              f"so this is harmless -- but that history is permanently gone.")
    if start > end:
        print(f"Nothing to do: start {start} is after end {end}.")
        return

    days = date_range(start, end)
    print(f"Search Console {SITE}: {start} -> {end} ({len(days)} days), "
          f"grains={','.join(grains)}, dataState={args.data_state}")

    session = build_session()
    warehouse_db.init_db()
    conn = sqlite3.connect(DB_PATH, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(DDL)
    stamp = warehouse_db.now()

    plan = []
    if "daily" in grains:
        plan.append(("search_console", "daily",
                     lambda: store_daily(conn, session, start, end,
                                         args.data_state, stamp)))
    if "queries" in grains:
        plan.append(("search_console_queries", "queries",
                     lambda: store_detail(conn, session, days, "queries",
                                          args.data_state, stamp,
                                          refresh=args.refresh)))
    if "pages" in grains:
        plan.append(("search_console_pages", "pages",
                     lambda: store_detail(conn, session, days, "pages",
                                          args.data_state, stamp,
                                          refresh=args.refresh)))
    if "query_pages" in grains:
        plan.append(("search_console_query_pages", "query_pages",
                     lambda: store_detail(conn, session, days, "query_pages",
                                          args.data_state, stamp,
                                          refresh=args.refresh)))

    failures = 0
    try:
        for platform, label, fn in plan:
            print(f"  {label}:", flush=True)
            started = warehouse_db.now()
            try:
                rows = fn()
                notes = ""
                status = "ok"
                if label == "daily":
                    gaps = missing_settled_days(conn, days)
                    if gaps:
                        shown = ",".join(gaps[:8]) + ("..." if len(gaps) > 8 else "")
                        notes = f"{len(gaps)} settled day(s) returned no data: {shown}"
                        status = "degraded"
                        print(f"    !! {notes}", flush=True)
                if args.data_state == "all":
                    notes = (notes + " | " if notes else "") + \
                            "dataState=all: recent days may be PARTIAL"
                warehouse_db.log_sync(platform, started, rows, status, notes)
                if status != "ok":
                    failures += 1
            except (SearchConsoleError, SearchConsoleTransient) as exc:
                print(f"    FAILED: {exc}", flush=True)
                warehouse_db.log_sync(platform, started, 0, "error", str(exc)[:500])
                failures += 1
    finally:
        conn.close()

    if failures:
        print(f"Search Console sync finished with {failures} problem grain(s).")
        sys.exit(1)
    print("Search Console sync complete.")


if __name__ == "__main__":
    main()
