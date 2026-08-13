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


def ensure_schema(conn) -> None:
    conn.executescript(DDL)


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
    """One signed GET. Refreshes an expired access token once, then retries."""
    secret = os.environ["TIKTOK_APP_SECRET"]
    for attempt in (1, 2):  # attempt 2 only happens after a token refresh
        params["timestamp"] = str(int(time.time()))
        params.pop("sign", None)
        params["sign"] = _sign(PATH, params, secret)
        r = requests.get(
            f"{BASE}{PATH}", params=params,
            headers={"content-type": "application/json",
                     "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]},
            timeout=60,
        )
        data = r.json()
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and attempt == 1:
            _refresh_access_token()  # saves the new token to .env + os.environ
            continue
        if code != 0:
            raise RuntimeError(f"TikTok video API {r.status_code} code={code}: {data.get('message')}")
        return data
    raise RuntimeError("TikTok request failed even after refreshing the access token.")


def fetch(account_type: str, start: str, end: str, gmv_positive_only: bool = False) -> list[dict]:
    rows: list[dict] = []
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
        for v in data.get("videos", []):
            gmv_block = v.get("gmv") or {}
            amount = float(gmv_block.get("amount", 0) or 0)
            # Sorted by GMV DESC: first $0 video => all remaining are $0. In
            # winners-only mode we stop here; by default we keep paging to
            # capture the zero-GMV tail (post-rate / dud visibility).
            if amount <= 0 and gmv_positive_only:
                return rows
            products = v.get("products") or []
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
    return rows


def sync(start: str, end: str, account_types: list[str], gmv_positive_only: bool = False) -> int:
    stamp = db.now()
    all_rows: list[dict] = []
    for at in account_types:
        all_rows += fetch(at, start, end, gmv_positive_only)
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
