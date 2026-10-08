r"""
TikTok Shop creator MARKETPLACE profile enrichment -> `tiktok_creator_marketplace`.

WHY THIS EXISTS
Your own order/video data only tells you what a creator sold FOR YOU. TikTok's
Marketplace profile carries the creator's WHOLE-SHOP category split
(`category_gmv_distribution`), so a creator you have never sent a sample to can
still be assessed for category fit BEFORE you spend on a sample. This script
looks up a list of creator handles you supply and stores each one's Marketplace
profile: follower count, category split, GMV/units bands, post rate, rating,
average commission, brand-collaboration count.

ENDPOINTS (seller namespace `affiliate_seller` — works with the ordinary
seller token; the `affiliate_creator` namespace needs a creator-type
authorisation and is NOT used here):
    POST /affiliate_seller/202508/marketplace_creators/search   body {"keyword": handle}
    GET  /affiliate_seller/202508/marketplace_creators/{creator_open_id}
Search is keyword-FUZZY, so only an EXACT case-insensitive username match is
accepted; anything else is recorded as `found = 0`. Categories use the V2 tree
(`category_version=v2` is mandatory for all-region shops).

WHERE THE HANDLES COME FROM (you choose — the script has no opinion on how you
build your roster)
    --handles a,b,c                   inline list
    --handles-file handles.txt        one handle per line (blank lines and
                                      `#` comments ignored)
    --from-table T --handle-column C  read-only `SELECT DISTINCT C FROM T`
                                      against the warehouse, e.g. the
                                      `handle` column of `tiktok_creators`
                                      (see tiktok_creators_sync.py). Table and
                                      column names are validated as plain
                                      identifiers before being interpolated.
Sources can be combined; handles are de-duplicated case-insensitively.

CATEGORY GROUPS (optional, so the table suits any catalogue)
`category_gmv_json` always keeps TikTok's full distribution untouched. To get
a quick "how much of this creator's GMV is in the categories I sell" number,
pass one or more `--group NAME=ID,ID,...` flags with top-level category ids
(the ids come from `tiktok_category_top`, which this script refreshes on every
run). The summed shares are stored as JSON in `group_pct_json`, e.g.
`{"footwear": 0.31, "bags": 0.02}`. Without any `--group` flag the column is
simply NULL — nothing else depends on it.

COVERAGE RULE ("record what was ASKED"): a row is written for EVERY handle
looked up, found or not, with `fetched_at`. A missing row therefore means "never
asked", never "asked and absent". Resume is keyed on `fetched_at` — handles
asked within `--max-age-days` are skipped — NEVER on the absence of rows.

TRAPS
* `category_gmv_distribution` values are FRACTIONS of the creator's whole-shop
  GMV ("0.8554" = 85.5%), not percentages. Category id `-1` is TikTok's
  "uncategorised" bucket and is stored separately as `uncategorised_share`.
* TikTok has no top-level category for niche segments you may care about
  (e.g. costumes sit inside a broader apparel category), so a group share can
  not separate them — join to your own sales data for that.
* `gmv_band` / `units_band` are BANDS ("$150K+"), not numbers.
* An EMPTY category list is common for creators who have not authorised data
  sharing. It is NOT evidence of poor fit — a blank is not a zero.
* A run that cannot make progress says so: repeated consecutive errors
  (rate limit, dropped scope) stop the run with exit code 75 and a `degraded`
  sync_log row, rather than looking like a clean finish. An empty lookup
  (zero handles found at all) is also logged `degraded`, never `ok`.

AUTH SETUP
Reuses the TikTok Shop app credentials and token handling already used by the
rest of this repo (`warehouse/connectors/tiktok_shop.py`); no new variables.
Needs `TIKTOK_APP_KEY`, `TIKTOK_APP_SECRET`, `TIKTOK_ACCESS_TOKEN`,
`TIKTOK_SHOP_CIPHER`. An expired access token is refreshed once per request.
The authorising app needs the affiliate-seller scope; re-authorise with ALL
scopes at once (a partial re-auth silently drops the others).

USAGE
  python tiktok_creator_marketplace_sync.py --handles creator_one,creator_two
  python tiktok_creator_marketplace_sync.py --handles-file handles.txt --max-age-days 14
  python tiktok_creator_marketplace_sync.py --from-table tiktok_creators --handle-column handle \
      --group footwear=601352 --group bags=824584 --limit 50
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors import tiktok_shop as t

load_dotenv()

SEARCH_PATH = "/affiliate_seller/202508/marketplace_creators/search"
DETAIL_PATH = "/affiliate_seller/202508/marketplace_creators/{}"
CATEGORY_PATH = "/product/202309/categories"
PLATFORM = "tiktok_creator_marketplace"
PACE_SECONDS = 0.25
MAX_CONSECUTIVE_ERRORS = 5
UNCATEGORISED_ID = "-1"
EXIT_PAUSED = 75  # deliberate pause on an upstream fault (matches the rest of the repo)
REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_creator_marketplace (
    creator_handle      TEXT PRIMARY KEY,
    found               INTEGER NOT NULL,   -- 1 = exact username match, 0 = asked but not found
    creator_open_id     TEXT,
    follower_count      INTEGER,
    category_ids        TEXT,               -- JSON list of top-level category ids
    category_gmv_json   TEXT,               -- TikTok's full distribution (values are FRACTIONS)
    group_pct_json      TEXT,               -- JSON {group_name: summed share} from --group flags, or NULL
    uncategorised_share REAL,               -- share of GMV in TikTok's uncategorised bucket (-1)
    gmv_band            TEXT,               -- a BAND such as '$150K+', not a number
    units_band          TEXT,
    post_rate           TEXT,
    pps                 TEXT,
    rating              TEXT,
    avg_commission_rate INTEGER,
    brand_collab_count  INTEGER,
    fetched_at          TEXT NOT NULL       -- when we ASKED; the resume marker
);
CREATE TABLE IF NOT EXISTS tiktok_category_top (
    category_id TEXT PRIMARY KEY,
    name        TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def check_required_env() -> None:
    """Fail fast and legibly on a misconfigured .env instead of a KeyError
    deep inside a signed request."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


# ---------------------------------------------------------------------------
# Handle sources
# ---------------------------------------------------------------------------

def parse_groups(specs: list[str]) -> dict[str, set[str]]:
    """`['footwear=601352,601353', 'bags=824584']` -> {name: {ids}}."""
    groups: dict[str, set[str]] = {}
    for spec in specs or []:
        name, sep, ids = spec.partition("=")
        name = name.strip()
        id_set = {i.strip() for i in ids.split(",") if i.strip()}
        if not sep or not name or not id_set:
            raise SystemExit(f"--group must look like NAME=ID,ID (got {spec!r})")
        groups[name] = id_set
    return groups


def read_handles_file(path: str) -> list[str]:
    with open(path, encoding="utf-8-sig") as fh:
        lines = (ln.strip() for ln in fh)
        return [ln.lstrip("@") for ln in lines if ln and not ln.startswith("#")]


def read_handles_table(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    """Read distinct handles from a warehouse table. Identifiers cannot be
    bound as parameters, so they are validated as plain identifiers first."""
    if not (_IDENT.match(table) and _IDENT.match(column)):
        raise SystemExit("--from-table / --handle-column must be plain identifiers")
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
    ).fetchall()
    return [str(r[0]).lstrip("@") for r in rows]


def dedupe_handles(handles: list[str]) -> list[str]:
    """Case-insensitive de-dup preserving first-seen order and spelling."""
    seen: set[str] = set()
    out: list[str] = []
    for h in handles:
        h = h.strip().lstrip("@")
        if h and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def call(method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
    """One signed request. Refreshes an expired access token once; raises on
    any other non-zero API code. The timestamp is re-stamped and re-signed on
    the retry, so a slow refresh cannot leave a stale timestamp in the
    signature."""
    for attempt in (1, 2):
        params = {"app_key": os.environ["TIKTOK_APP_KEY"], "timestamp": str(int(time.time())),
                  "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"]}
        params.update(query or {})
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        params["sign"] = t._sign(path, params, payload, os.environ["TIKTOK_APP_SECRET"])
        r = requests.request(method, t.BASE + path, params=params, data=payload or None, timeout=60,
                             headers={"Content-Type": "application/json",
                                      "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]})
        p = r.json()
        if p.get("code") in t.TOKEN_EXPIRED_CODES and attempt == 1:
            t._refresh_access_token()
            continue
        if p.get("code") not in (0, None):
            raise RuntimeError(f"TikTok API error {p.get('code')}: {p.get('message')}")
        return p.get("data") or {}
    raise RuntimeError("request failed after token refresh")


def refresh_category_names(conn: sqlite3.Connection) -> int:
    """Store the V2 top-level category id -> name map, so `--group` ids can be
    looked up in SQL. Top-level = parent_id 0."""
    cats = call("GET", CATEGORY_PATH, {"locale": "en-US", "category_version": "v2"}).get("categories", [])
    top = [(c["id"], c.get("local_name")) for c in cats if str(c.get("parent_id")) == "0"]
    conn.executemany("INSERT OR REPLACE INTO tiktok_category_top (category_id, name) VALUES (?,?)", top)
    conn.commit()
    return len(top)


def share(distribution: list[dict], ids: set[str]) -> float:
    """Sum the FRACTIONAL GMV shares of the given category ids."""
    return round(sum(float(d["value"]) for d in distribution
                     if str(d.get("category_id")) in ids and d.get("value") not in (None, "")), 4)


def fetch_one(handle: str) -> dict | None:
    """Search then fetch one creator. Returns None unless the search returns an
    exact (case-insensitive) username match — fuzzy hits are never trusted."""
    creators = call("POST", SEARCH_PATH, {"page_size": "12"}, {"keyword": handle}).get("creators") or []
    hit = next((c for c in creators if (c.get("username") or "").lower() == handle.lower()), None)
    if not hit:
        return None
    detail = (call("GET", DETAIL_PATH.format(hit["creator_open_id"])).get("creator")) or {}

    def pick(key):
        return detail.get(key) if detail.get(key) is not None else hit.get(key)

    return {
        "open_id": hit["creator_open_id"],
        "followers": pick("follower_count"),
        "ids": pick("category_ids") or [],
        "dist": detail.get("category_gmv_distribution") or [],
        "gmv_band": (pick("gmv_range") or {}).get("formatted_range"),
        "units_band": (pick("units_sold_range") or {}).get("formatted_range"),
        "post_rate": detail.get("post_rate"), "pps": detail.get("pps"), "rating": detail.get("rating"),
        "commission": detail.get("avg_commission_rate"),
        "collabs": detail.get("brand_collaboration_count"),
    }


def write_row(conn: sqlite3.Connection, handle: str, d: dict | None, groups: dict[str, set[str]]) -> None:
    """Upsert one ASKED handle. Columns are named (never positional) so a later
    ALTER TABLE cannot silently shift values."""
    if d is None:
        conn.execute("""INSERT OR REPLACE INTO tiktok_creator_marketplace
                        (creator_handle, found, fetched_at) VALUES (?, 0, datetime('now'))""", (handle,))
        return
    dist = d["dist"]
    group_json = json.dumps({n: share(dist, ids) for n, ids in groups.items()}) if groups else None
    conn.execute("""INSERT OR REPLACE INTO tiktok_creator_marketplace
        (creator_handle, found, creator_open_id, follower_count, category_ids, category_gmv_json,
         group_pct_json, uncategorised_share, gmv_band, units_band, post_rate, pps, rating,
         avg_commission_rate, brand_collab_count, fetched_at)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (handle, d["open_id"], d["followers"], json.dumps(d["ids"]), json.dumps(dist),
         group_json, share(dist, {UNCATEGORISED_ID}), d["gmv_band"], d["units_band"],
         d["post_rate"], d["pps"], d["rating"], d["commission"], d["collabs"]))


def select_todo(conn: sqlite3.Connection, handles: list[str], max_age_days: int, limit: int = 0) -> list[str]:
    """Handles never asked, or asked longer ago than `max_age_days`. Keyed on
    fetched_at — a missing row means 'never asked', which is itself a reason to
    ask, and an asked-but-absent creator keeps its row (found=0)."""
    fresh = {r[0].lower() for r in conn.execute(
        "SELECT creator_handle FROM tiktok_creator_marketplace WHERE fetched_at >= datetime('now', ?)",
        (f"-{max_age_days} day",))}
    todo = [h for h in handles if h.lower() not in fresh]
    return todo[:limit] if limit else todo


def run(conn: sqlite3.Connection, todo: list[str], groups: dict[str, set[str]], pace: float = PACE_SECONDS):
    """Look up each handle. Returns (found, not_found, stopped_early)."""
    errors = found = missing = 0
    for i, h in enumerate(todo, 1):
        try:
            d = fetch_one(h)
            errors = 0
        except Exception as e:  # noqa: BLE001 - any upstream fault counts toward the stop rule
            errors += 1
            print(f"  {h}: {e}")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                conn.commit()
                return found, missing, True
            time.sleep(5)
            continue
        write_row(conn, h, d, groups)
        found += d is not None
        missing += d is None
        if i % 25 == 0:
            conn.commit()
            print(f"  {i}/{len(todo)} found={found} not_found={missing}")
        time.sleep(pace)
    conn.commit()
    return found, missing, False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--handles", help="comma-separated creator handles")
    ap.add_argument("--handles-file", help="text file, one handle per line")
    ap.add_argument("--from-table", help="warehouse table to read handles from")
    ap.add_argument("--handle-column", default="handle", help="column in --from-table (default: handle)")
    ap.add_argument("--group", action="append", default=[], metavar="NAME=ID,ID",
                    help="sum these top-level category ids into group_pct_json (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="look up at most N handles this run")
    ap.add_argument("--max-age-days", type=int, default=30,
                    help="skip handles asked within this many days (default 30)")
    a = ap.parse_args()

    check_required_env()
    groups = parse_groups(a.group)
    db.init_db()
    started = db.now()
    conn = sqlite3.connect(db.DB_PATH, timeout=db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)

    handles: list[str] = []
    if a.handles:
        handles += a.handles.split(",")
    if a.handles_file:
        handles += read_handles_file(a.handles_file)
    if a.from_table:
        handles += read_handles_table(conn, a.from_table, a.handle_column)
    handles = dedupe_handles(handles)
    if not handles:
        raise SystemExit("No handles supplied: use --handles, --handles-file or --from-table.")

    print("top-level categories:", refresh_category_names(conn))
    todo = select_todo(conn, handles, a.max_age_days, a.limit)
    print("to fetch:", len(todo))
    found, missing, stopped = run(conn, todo, groups)
    conn.close()
    print(f"done: found={found} not_found={missing}")

    if stopped:
        print("stopping: repeated errors (likely rate limit or scope); resume later")
        db.log_sync(PLATFORM, started, found + missing, "degraded",
                    f"stopped after {MAX_CONSECUTIVE_ERRORS} consecutive errors")
        return EXIT_PAUSED
    if todo and found == 0:
        # Nothing matched at all: 'ok' would be a lie (a wrong scope or a bad handle list).
        db.log_sync(PLATFORM, started, missing, "degraded", "no handle matched an exact username")
    else:
        db.log_sync(PLATFORM, started, found + missing, "ok",
                    f"found={found} not_found={missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
