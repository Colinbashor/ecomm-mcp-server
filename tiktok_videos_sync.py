r"""
TikTok Shop video performance -> warehouse.

Endpoint: GET /analytics/202409/shop_videos/performance
Scope needed: data.shop_analytics.public.read

Pulls per-video shop-attributed performance (GMV, orders, units, views, click-
through rate) for a date window, plus which product(s) each video tagged.
`account_type` splits AFFILIATES (creators promoting your products) from your
own LINKED_ACCOUNTS videos, so organic/affiliate UGC can be compared against
your own brand posts.

AUTH SETUP
Reuses the TikTok Shop app credentials and access/refresh-token machinery from
`warehouse/connectors/tiktok_shop.py` (see that file's docstring for the full
OAuth setup). No new credentials are needed for this script -- if TikTok
orders sync already works, this does too.

GENERIC GOTCHAS (all verified against the live API)
* SCOPE RE-AUTH TRAP: a TikTok Shop refresh token belongs to ONE authorization
  grant with a FIXED set of scopes. Re-authorizing the app later for a
  *different* or narrower scope selection (e.g. testing analytics access in
  isolation) silently REPLACES the whole grant -- including scopes you already
  depended on, like order access. Nothing breaks immediately: the current
  access token keeps working until it expires, then every call that needed the
  dropped scope starts failing with business error code 105005 ("no
  permission"). FIX: always re-authorize with the FULL set of scopes your
  integration needs, never a subset "just to test one thing".
* WINDOW CAP + RETENTION: the analytics window is capped (roughly a month per
  request) and TikTok only retains a few months of video-analytics history. A
  full historical backfill therefore has to walk BACKWARD in bounded windows
  rather than requesting one huge range; requesting too old or too wide a span
  returns a "parameter invalid" business error, not an empty result -- don't
  mistake that error for "no data that month."
* RESULTS ARE GMV-SORTED DESCENDING. If you only care about actual sellers,
  you can stop paging at the first zero-GMV video. This script keeps paging
  by default (so post-rate and the true dud-rate stay measurable), but exposes
  --gmv-positive-only to restore the cheaper stop-early behavior.
* A video that tagged a product is captured here; a creator's post that never
  tagged anything is invisible to this endpoint no matter how much organic
  interest it drove -- that needs a separate affiliate/creator-content feed.

EVERY TAGGED PRODUCT: tiktok_shop_videos keeps only the FIRST tagged product
(products[0]) per video x window, but a video can tag up to ~10 products.
Every tagged product also goes to `tiktok_shop_video_products` (video x
account_type x product, cumulative: first_seen / last_seen are the earliest
window_start / latest window_end that returned the tag), so a creator's video
can be linked to its 2nd..Nth product too. The API's product entries carry
only id + name -- GMV / units / views are VIDEO-level, so NEVER sum a video's
metrics across its tagged products (a 3-product video would triple-count).
`tiktok_shop_video_products_coverage` records every window ASKED for and what
came back (complete / short / empty / out_of_retention / failed), which
tiktok_video_products_backfill.py resumes from -- never from absent rows,
since a window can legitimately hold no multi-product video.

USAGE
  python tiktok_videos_sync.py --days 30
  python tiktok_videos_sync.py --start 2026-01-01 --end 2026-02-01
  python tiktok_videos_sync.py --current-window        # trailing 30d, convenient for a daily cron
  python tiktok_videos_sync.py --account-types AFFILIATES
  python tiktok_videos_sync.py --gmv-positive-only
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import time
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors.tiktok_shop import TOKEN_EXPIRED_CODES, _refresh_access_token

load_dotenv()

BASE = "https://open-api.tiktokglobalshop.com"
PATH = "/analytics/202409/shop_videos/performance"
PLATFORM = "tiktok_videos"

REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_shop_videos (
    video_id           TEXT NOT NULL,
    account_type       TEXT NOT NULL,   -- AFFILIATES | LINKED_ACCOUNTS | ALL
    title              TEXT,
    username           TEXT,            -- creator @handle (only id this endpoint exposes)
    gmv                REAL DEFAULT 0,
    currency           TEXT,
    sku_orders         INTEGER DEFAULT 0,
    units_sold         INTEGER DEFAULT 0,
    views              INTEGER DEFAULT 0,
    click_through_rate REAL DEFAULT 0,
    video_post_time    TEXT,
    product_id         TEXT,            -- first tagged product
    product_name       TEXT,
    product_count      INTEGER DEFAULT 0,
    window_start       TEXT NOT NULL,
    window_end         TEXT NOT NULL,
    synced_at          TEXT NOT NULL,
    PRIMARY KEY (video_id, account_type, window_start, window_end)
);
"""


# Every tagged product of a video (tiktok_shop_videos holds only the first).
# Cumulative across windows, never deleted: a tag seen once stays, and
# first_seen/last_seen bound the windows that returned it.
PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS tiktok_shop_video_products (
    video_id      TEXT NOT NULL,
    account_type  TEXT NOT NULL,   -- AFFILIATES | LINKED_ACCOUNTS | ALL
    product_id    TEXT NOT NULL,   -- TikTok Shop product (SPU) id
    product_name  TEXT,
    position      INTEGER,         -- 1 = products[0] = tiktok_shop_videos.product_id, in the LATEST window seen
    first_seen    TEXT NOT NULL,   -- earliest window_start that returned this tag
    last_seen     TEXT NOT NULL,   -- latest window_end (exclusive) that returned this tag
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (video_id, account_type, product_id)
);
CREATE INDEX IF NOT EXISTS idx_tsvp_product ON tiktok_shop_video_products(product_id);

CREATE TABLE IF NOT EXISTS tiktok_shop_video_products_coverage (
    account_type     TEXT NOT NULL,
    window_start     TEXT NOT NULL,
    window_end       TEXT NOT NULL,   -- exclusive
    status           TEXT NOT NULL,   -- complete | short | empty | out_of_retention | failed
    api_total        INTEGER,         -- total_count the API reported for the window
    rows_returned    INTEGER,         -- video rows across all pages (incl. any repeats)
    videos_returned  INTEGER,         -- distinct video ids
    multi_product_videos INTEGER,     -- videos tagging more than one product
    product_rows     INTEGER,         -- (video, product) tags written
    attempts         INTEGER NOT NULL DEFAULT 0,
    source           TEXT,            -- sync | backfill
    error            TEXT,
    first_attempt_at TEXT NOT NULL,
    last_attempt_at  TEXT NOT NULL,
    PRIMARY KEY (account_type, window_start, window_end)
);
"""

# Named columns throughout, so a migrated table's physical column order can
# never shift values. The ON CONFLICT keeps the widest seen range, and takes
# name/position from whichever window is LATEST, so an older backfill window
# can never overwrite what a newer pull saw.
_UPSERT_PRODUCT = """
INSERT INTO tiktok_shop_video_products
    (video_id, account_type, product_id, product_name, position, first_seen, last_seen, synced_at)
VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT (video_id, account_type, product_id) DO UPDATE SET
    product_name = CASE WHEN excluded.last_seen >= last_seen
                        THEN COALESCE(excluded.product_name, product_name) ELSE product_name END,
    position     = CASE WHEN excluded.last_seen >= last_seen THEN excluded.position ELSE position END,
    first_seen   = MIN(first_seen, excluded.first_seen),
    last_seen    = MAX(last_seen, excluded.last_seen),
    synced_at    = excluded.synced_at
"""

_UPSERT_COVERAGE = """
INSERT INTO tiktok_shop_video_products_coverage
    (account_type, window_start, window_end, status, api_total, rows_returned, videos_returned,
     multi_product_videos, product_rows, attempts, source, error, first_attempt_at, last_attempt_at)
VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?)
ON CONFLICT (account_type, window_start, window_end) DO UPDATE SET
    status = excluded.status, api_total = excluded.api_total,
    rows_returned = excluded.rows_returned, videos_returned = excluded.videos_returned,
    multi_product_videos = excluded.multi_product_videos, product_rows = excluded.product_rows,
    attempts = attempts + 1, source = excluded.source, error = excluded.error,
    last_attempt_at = excluded.last_attempt_at
"""

# Business error for a window that starts before the API's lookback (roughly
# six months; the floor advances a day per day).
OUT_OF_RETENTION_CODE = 28001022


def ensure_schema(conn) -> None:
    conn.executescript(DDL)
    ensure_products_tables(conn)


def ensure_products_tables(conn) -> None:
    conn.executescript(PRODUCTS_DDL)


def check_required_env() -> None:
    """Raise a clear SystemExit (not a KeyError deep in a request) when
    credentials are missing, so a misconfigured .env fails fast and legibly."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


def _sign(path: str, params: dict, secret: str) -> str:
    """HMAC-SHA256 over app_secret + path + sorted(query k+v, excluding sign/
    access_token) + app_secret. Same recipe as the orders connector's
    `_sign`, minus the request body -- these are signed GETs with no JSON
    body, so the body slot is simply omitted rather than passed as ''."""
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    base = f"{secret}{path}{ordered}{secret}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def _request_page(params: dict) -> dict:
    """One signed GET. Refreshes an expired access token once and rides out
    transient connection resets, rate limits and 5xx errors with exponential
    backoff (1, 2, 4, 8, 16s; 6 attempts). A month of videos is hundreds of
    pages, so without this a single hiccup anywhere killed the whole pull."""
    secret = os.environ["TIKTOK_APP_SECRET"]
    refreshed = False
    for attempt in range(6):
        params["timestamp"] = str(int(time.time()))
        params.pop("sign", None)
        params["sign"] = _sign(PATH, params, secret)
        try:
            r = requests.get(
                f"{BASE}{PATH}", params=params,
                headers={"content-type": "application/json",
                         "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]},
                timeout=60,
            )
            data = r.json()
        except (requests.ConnectionError, requests.Timeout, ValueError):
            time.sleep(2 ** attempt)  # network hiccup or non-JSON body
            continue
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and not refreshed:
            _refresh_access_token()  # saves the new token to .env + os.environ
            refreshed = True
            continue
        if code in (105050, 105051, 429000) or r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)  # rate limited, or a transient server error
            continue
        if code != 0:
            raise RuntimeError(f"TikTok video API {r.status_code} code={code}: {data.get('message')}")
        return data
    raise RuntimeError("TikTok video API failed after retries (last attempt exhausted).")


def product_tags(video: dict) -> list[tuple]:
    """(product_id, product_name, position) for EVERY product the video tags,
    position 1 = products[0]. A product listed twice keeps its first position;
    an entry with no id is skipped (nothing to key it on)."""
    out, seen = [], set()
    for pos, p in enumerate(video.get("products") or [], start=1):
        pid = p.get("id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append((str(pid), p.get("name"), pos))
    return out


class FetchResult:
    """One account_type x window pull. `rows` are tiktok_shop_videos row dicts
    (the same shape fetch() has always returned); `tags` are (video_id,
    account_type, product_id, product_name, position) for every tagged product."""

    def __init__(self):
        self.rows: list[dict] = []
        self.tags: list[tuple] = []
        self.api_total: int | None = None
        self.video_ids: set[str] = set()
        self.multi_product_videos = 0
        self.stopped_early = False   # winners-only mode stops at the first $0 video

    def coverage_status(self) -> str:
        """empty = nothing came back (never 'complete'); short = fewer distinct
        videos than the API's own total_count, so paging lost some."""
        if not self.video_ids:
            return "empty"
        if self.stopped_early or self.api_total is None:
            return "complete"
        return "complete" if len(self.video_ids) >= self.api_total else "short"


def fetch(account_type: str, start: str, end: str, gmv_positive_only: bool = False) -> list[dict]:
    """tiktok_shop_videos rows only (unchanged shape, for existing callers)."""
    return fetch_full(account_type, start, end, gmv_positive_only).rows


def fetch_full(account_type: str, start: str, end: str, gmv_positive_only: bool = False) -> FetchResult:
    res = FetchResult()
    rows, tags = res.rows, res.tags
    page_token = ""
    while True:
        params = {
            "app_key": os.environ["TIKTOK_APP_KEY"],
            "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"],
            "start_date_ge": start,   # inclusive
            "end_date_lt": end,       # exclusive
            "page_size": "100",
            "currency": "USD",
            "account_type": account_type,
            "sort_field": "gmv",
            "sort_order": "DESC",
        }
        if page_token:
            params["page_token"] = page_token
        data = _request_page(params).get("data", {}) or {}
        if res.api_total is None and data.get("total_count") is not None:
            res.api_total = int(data["total_count"])
        for v in data.get("videos", []):
            gmv_block = v.get("gmv") or {}
            amount = float(gmv_block.get("amount", 0) or 0)
            # Sorted by GMV DESC: first $0 video => all remaining are $0. In
            # winners-only mode we stop here; by default we keep paging to
            # capture the zero-GMV tail (post-rate / dud visibility).
            if amount <= 0 and gmv_positive_only:
                res.stopped_early = True
                return res
            products = v.get("products") or []
            vtags = product_tags(v)
            if v.get("id"):
                res.video_ids.add(v["id"])
                tags.extend((v["id"], account_type, pid, name, pos) for pid, name, pos in vtags)
                res.multi_product_videos += len(vtags) > 1
            rows.append({
                "video_id": v.get("id"), "account_type": account_type,
                "title": v.get("title"), "username": v.get("username"),
                "gmv": amount, "currency": gmv_block.get("currency"),
                "sku_orders": int(v.get("sku_orders", 0) or 0),
                "units_sold": int(v.get("units_sold", 0) or 0),
                "views": int(v.get("views", 0) or 0),
                "click_through_rate": float(v.get("click_through_rate", 0) or 0),
                "video_post_time": v.get("video_post_time"),
                "product_id": products[0].get("id") if products else None,
                "product_name": products[0].get("name") if products else None,
                "product_count": len(products),
                "window_start": start, "window_end": end,
            })
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break
    return res


def write_product_tags(conn, tags: list[tuple], start: str, end: str, stamp: str) -> int:
    """Upsert (video_id, account_type, product_id, product_name, position) tags
    seen in the window [start, end). Caller owns the transaction."""
    conn.executemany(_UPSERT_PRODUCT,
                     [(vid, at, pid, name, pos, start, end, stamp) for vid, at, pid, name, pos in tags])
    return len(tags)


def record_coverage(conn, account_type: str, start: str, end: str, status: str, source: str,
                    res: FetchResult | None = None, error: str | None = None,
                    stamp: str | None = None) -> None:
    """One row per window ASKED for, whatever came back: a window that returned
    nothing, was refused, or failed is recorded as such, never left absent for
    a resume to misread. Caller owns the transaction."""
    stamp = stamp or db.now()
    conn.execute(_UPSERT_COVERAGE, (
        account_type, start, end, status,
        res.api_total if res else None, len(res.rows) if res else None,
        len(res.video_ids) if res else None, res.multi_product_videos if res else None,
        len(res.tags) if res else None, source, error, stamp, stamp))


def sync(start: str, end: str, account_types: list[str], gmv_positive_only: bool = False) -> int:
    stamp = db.now()
    all_rows: list[dict] = []
    fetched: list[tuple[str, FetchResult]] = []
    for at in account_types:
        res = fetch_full(at, start, end, gmv_positive_only)
        fetched.append((at, res))
        all_rows += res.rows
    for r in all_rows:
        r["synced_at"] = stamp

    conn = db.connect()
    with conn:
        ensure_schema(conn)
        conn.executemany(
            """
            INSERT OR REPLACE INTO tiktok_shop_videos
              (video_id, account_type, title, username, gmv, currency, sku_orders,
               units_sold, views, click_through_rate, video_post_time,
               product_id, product_name, product_count, window_start, window_end, synced_at)
            VALUES
              (:video_id, :account_type, :title, :username, :gmv, :currency, :sku_orders,
               :units_sold, :views, :click_through_rate, :video_post_time,
               :product_id, :product_name, :product_count, :window_start, :window_end, :synced_at)
            """,
            all_rows,
        )
        for at, res in fetched:
            write_product_tags(conn, res.tags, start, end, stamp)
            record_coverage(conn, at, start, end, res.coverage_status(), "sync", res, stamp=stamp)
    conn.close()
    return len(all_rows)


def _current_window() -> tuple[str, str]:
    """Trailing 30 days ending tomorrow (exclusive) -- a rolling window
    convenient to re-run daily, since recent days' GMV/views keep climbing
    until TikTok finalizes them."""
    today = date.today()
    return (today - timedelta(days=30)).isoformat(), (today + timedelta(days=1)).isoformat()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--current-window", action="store_true",
                    help="trailing 30 days ending tomorrow -- convenient for a daily cron")
    p.add_argument("--account-types", default="AFFILIATES,LINKED_ACCOUNTS",
                    help="comma list: AFFILIATES, LINKED_ACCOUNTS, ALL")
    p.add_argument("--gmv-positive-only", action="store_true",
                    help="stop paging at the first zero-GMV video (cheaper, hides post-rate/dud data)")
    args = p.parse_args()

    check_required_env()

    if args.current_window:
        start, end = _current_window()
    else:
        end = args.end or date.today().isoformat()
        start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()

    types = [t.strip() for t in args.account_types.split(",") if t.strip()]

    db.init_db()
    started = db.now()
    try:
        n = sync(start, end, types, gmv_positive_only=args.gmv_positive_only)
    except Exception as e:  # noqa: BLE001
        db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    db.log_sync(PLATFORM, started, n, "ok", f"{start} -> {end}")
    print(f"TikTok videos: wrote {n} rows to tiktok_shop_videos ({start} -> {end}) [{', '.join(types)}]")
