r"""
TikTok Shop LIVE-shopping performance -> warehouse.

Two related feeds, both standalone (own tables, no schema.sql edits):

1. Per-LIVE-SESSION performance (`tiktok_shop_lives`).
   Endpoint: GET /analytics/202509/shop_lives/performance
   One row per broadcast: GMV, viewers, engagement, click-through and
   click-to-order rates. `account_type` splits your own account's broadcasts
   from affiliate/creator lives and paid-marketing lives.

2. Per-PRODUCT, per-day LIVE-attributed funnel (`tiktok_shop_live_products`).
   Needs TWO endpoints together -- the reason why is the interesting gotcha,
   see below.

AUTH SETUP
Reuses the TikTok Shop app credentials + access/refresh-token handling in
`warehouse/connectors/tiktok_shop.py`. No new credentials needed.

GENERIC GOTCHAS
* SCOPE RE-AUTH TRAP (same lesson as the orders/video connectors): a partial
  re-authorization silently drops previously-granted scopes from the one live
  refresh token. The old access token keeps working until it expires, then
  everything needing the dropped scope fails with business error 105005.
  Always re-authorize with the FULL scope list you depend on, never a subset.
* WHY THE PRODUCT FUNNEL NEEDS TWO ENDPOINTS (verified live): the shop-wide
  "list today's top sellers" endpoint (.../shop_products/performance) has NO
  content-type breakdown and SILENTLY IGNORES a live_id filter -- it is only
  good for finding which product ids sold *something* that day. The
  per-product detail endpoint (.../shop_products/{id}/performance) is the one
  that actually splits GMV/impressions/units by content_type
  (LIVE | VIDEO | PRODUCT_CARD), but it has no "list all products" mode -- you
  must already know the id. So the flow is: list a day's top sellers (cheap,
  GMV-sorted so you can stop at the first $0 product), then call the
  per-product detail endpoint once per candidate id and keep only the LIVE
  slice. That fan-out of per-product calls is also why this feed needs its
  own backoff: a burst of rapid per-id calls trips TikTok's connection
  shedding far more readily than the single-call session endpoint does.
* DATE GRAIN, NOT SESSION GRAIN: the per-product detail endpoint buckets by
  calendar DAY, not by individual broadcast, so multiple same-day broadcasts
  collapse into one row -- you cannot split per-broadcast product performance
  back out of this feed alone.
* SHOP-LOCAL DAY BUCKETING: a broadcast's start/end times are UNIX
  timestamps. Bucketing them into a calendar day using UTC (rather than your
  shop's own local timezone) can push a late-night broadcast into the *next*
  calendar day from the seller's point of view. Set TIKTOK_SHOP_TIMEZONE in
  .env to your shop's IANA timezone name (e.g. "America/New_York") if you
  want "which days did we go live" to match your own business calendar
  instead of UTC.
* Because the per-product crawl is expensive (one call per candidate
  product, per day), this script by default only crawls product detail for
  days your OWN account (TIKTOK_LIVE_OWN_ACCOUNT_TYPE, default
  OFFICIAL_ACCOUNTS) is recorded as having gone live in `tiktok_shop_lives`
  -- not every calendar day in the window. Pass --dates to force specific
  days regardless.
* A rough cross-check is worth keeping once both tables exist: a day's
  summed per-product LIVE gmv should land in the same ballpark as that day's
  session-level GMV from `tiktok_shop_lives` (not exact -- content
  attribution and account-type scope differ slightly between the two
  endpoints); a ratio far outside a sane band usually signals a partial
  pull, not proof of an exact reconciliation.

USAGE
  python tiktok_live_sync.py --days 30
  python tiktok_live_sync.py --start 2026-01-01 --end 2026-02-01
  python tiktok_live_sync.py --only lives --account-types OFFICIAL_ACCOUNTS
  python tiktok_live_sync.py --only products --dates 2026-01-05,2026-01-12
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors.tiktok_shop import TOKEN_EXPIRED_CODES, _refresh_access_token

load_dotenv()

BASE = "https://open-api.tiktokglobalshop.com"
LIVES_PATH = "/analytics/202509/shop_lives/performance"
LIST_PATH = "/analytics/202509/shop_products/performance"
DETAIL_TMPL = "/analytics/202405/shop_products/{pid}/performance"
PLATFORM = "tiktok_live"

REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

# Which account_type in tiktok_shop_lives represents broadcasts you run
# yourself (as opposed to affiliate/creator or paid-marketing lives). Override
# via .env if your shop labels its own broadcasts differently.
OWN_ACCOUNT_TYPE = os.environ.get("TIKTOK_LIVE_OWN_ACCOUNT_TYPE", "OFFICIAL_ACCOUNTS")
# Your shop's own local timezone, for bucketing broadcasts into calendar days
# the way your business actually thinks about "which day did we go live".
SHOP_TZ = ZoneInfo(os.environ.get("TIKTOK_SHOP_TIMEZONE", "UTC"))

# How many of a day's top-GMV sellers to inspect for LIVE activity before
# giving up. The catalog tail is zero-GMV and live-driven products cluster
# near the top of the GMV-sorted list, so this is a safety ceiling, not the
# typical count actually scanned.
MAX_SELLERS_PER_DAY = 300

LIVES_DDL = """
CREATE TABLE IF NOT EXISTS tiktok_shop_lives (
    live_id                 TEXT NOT NULL,
    account_type            TEXT NOT NULL,  -- ALL | OFFICIAL_ACCOUNTS | AFFILIATE_ACCOUNTS | MARKETING_ACCOUNTS
    title                   TEXT,
    username                TEXT,
    start_time              TEXT,
    end_time                TEXT,
    views                   INTEGER DEFAULT 0,
    viewers                 INTEGER DEFAULT 0,
    avg_viewing_duration    REAL DEFAULT 0,   -- seconds
    product_impressions     INTEGER DEFAULT 0,
    product_clicks          INTEGER DEFAULT 0,
    click_through_rate      REAL DEFAULT 0,   -- percent, e.g. 2.10 means 2.10%
    likes                   INTEGER DEFAULT 0,
    comments                INTEGER DEFAULT 0,
    shares                  INTEGER DEFAULT 0,
    new_followers           INTEGER DEFAULT 0,
    gmv                     REAL DEFAULT 0,
    currency                TEXT,
    live_gmv_24h            REAL DEFAULT 0,   -- API key "24h_live_gmv"
    items_sold              INTEGER DEFAULT 0,
    sku_orders              INTEGER DEFAULT 0,
    created_sku_orders      INTEGER DEFAULT 0,
    customers               INTEGER DEFAULT 0,
    different_products_sold INTEGER DEFAULT 0,
    avg_price               REAL DEFAULT 0,
    click_to_order_rate     REAL DEFAULT 0,   -- percent
    window_start            TEXT NOT NULL,
    window_end              TEXT NOT NULL,
    synced_at               TEXT NOT NULL,
    PRIMARY KEY (live_id, account_type, window_start, window_end)
);
"""

LIVE_PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS tiktok_shop_live_products (
    product_id        TEXT NOT NULL,
    date              TEXT NOT NULL,      -- YYYY-MM-DD, shop-local calendar day
    live_impressions  INTEGER DEFAULT 0,
    live_clicks       INTEGER DEFAULT 0,  -- product-page views; CTR = clicks/impressions
    live_units        INTEGER DEFAULT 0,
    live_gmv          REAL DEFAULT 0,
    currency          TEXT,
    live_avg_visitors INTEGER DEFAULT 0,
    synced_at         TEXT NOT NULL,
    PRIMARY KEY (product_id, date)
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(LIVES_DDL)
    conn.executescript(LIVE_PRODUCTS_DDL)


def check_required_env() -> None:
    """Raise a clear SystemExit (not a KeyError deep in a request) when
    credentials are missing, so a misconfigured .env fails fast and legibly."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


# ---------------------------------------------------------------------------
# shared signed-GET request helper
# ---------------------------------------------------------------------------

def _sign(path: str, params: dict, secret: str) -> str:
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    base = f"{secret}{path}{ordered}{secret}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def _request(path: str, params: dict, max_attempts: int = 6) -> dict:
    """One signed GET. Refreshes an expired access token once and rides out
    transient connection resets / rate limits with exponential backoff -- a
    burst of rapid per-product calls (the live-products detail fetch) trips
    TikTok's connection shedding far more readily than a single call does."""
    secret = os.environ["TIKTOK_APP_SECRET"]
    refreshed = False
    code = None
    for attempt in range(max_attempts):
        params["timestamp"] = str(int(time.time()))
        params.pop("sign", None)
        params["sign"] = _sign(path, params, secret)
        try:
            r = requests.get(
                f"{BASE}{path}", params=params,
                headers={"content-type": "application/json",
                         "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]},
                timeout=60,
            )
            data = r.json()
        except (requests.ConnectionError, requests.Timeout, ValueError):
            time.sleep(2 ** attempt)  # 1,2,4,8,16,32s backoff on network hiccups
            continue
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and not refreshed:
            _refresh_access_token()
            refreshed = True
            continue
        if code in (105050, 105051, 429000) or r.status_code == 429:  # rate limited
            time.sleep(2 ** attempt)
            continue
        if code != 0:
            raise RuntimeError(f"TikTok {path} {r.status_code} code={code}: {data.get('message')}")
        return data
    raise RuntimeError(f"TikTok {path} failed after {max_attempts} attempts (last code={code}).")


def _pct(v) -> float:
    """'2.10%' -> 2.10; also tolerates a plain number or None."""
    if v is None:
        return 0.0
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip() or "0"
    return float(v)


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, str):
        v = v.replace(",", "").strip() or "0"
    return float(v)


def _money(v) -> tuple[float, str | None]:
    """{amount, currency} object (or a bare number) -> (amount, currency)."""
    if isinstance(v, dict):
        return _num(v.get("amount")), v.get("currency")
    return _num(v), None


# ---------------------------------------------------------------------------
# (1) per-session performance
# ---------------------------------------------------------------------------

def fetch_lives(account_type: str, start: str, end: str) -> list[dict]:
    rows: list[dict] = []
    page_token = ""
    while True:
        params = {
            "app_key": os.environ["TIKTOK_APP_KEY"],
            "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"],
            "start_date_ge": start, "end_date_lt": end,
            "page_size": "100", "currency": "USD",
            "account_type": account_type,
            "sort_field": "gmv", "sort_order": "DESC",
        }
        if page_token:
            params["page_token"] = page_token
        data = _request(LIVES_PATH, params).get("data", {}) or {}
        for s in data.get("live_stream_sessions", []):
            ip = s.get("interaction_performance") or {}
            sp = s.get("sales_performance") or {}
            gmv, currency = _money(sp.get("gmv"))
            gmv_24h, _ = _money(sp.get("24h_live_gmv"))
            avg_price, _ = _money(sp.get("avg_price"))
            rows.append({
                "live_id": s.get("id"), "account_type": account_type,
                "title": s.get("title"), "username": s.get("username"),
                "start_time": s.get("start_time"), "end_time": s.get("end_time"),
                "views": int(_num(ip.get("views"))), "viewers": int(_num(ip.get("viewers"))),
                "avg_viewing_duration": _num(ip.get("avg_viewing_duration")),
                "product_impressions": int(_num(ip.get("product_impressions"))),
                "product_clicks": int(_num(ip.get("product_clicks"))),
                "click_through_rate": _pct(ip.get("click_through_rate")),
                "likes": int(_num(ip.get("likes"))), "comments": int(_num(ip.get("comments"))),
                "shares": int(_num(ip.get("shares"))), "new_followers": int(_num(ip.get("new_followers"))),
                "gmv": gmv, "currency": currency, "live_gmv_24h": gmv_24h,
                "items_sold": int(_num(sp.get("items_sold"))),
                "sku_orders": int(_num(sp.get("sku_orders"))),
                "created_sku_orders": int(_num(sp.get("created_sku_orders"))),
                "customers": int(_num(sp.get("customers"))),
                "different_products_sold": int(_num(sp.get("different_products_sold"))),
                "avg_price": avg_price, "click_to_order_rate": _pct(sp.get("click_to_order_rate")),
                "window_start": start, "window_end": end,
            })
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break
    return rows


def sync_lives(start: str, end: str, account_types: list[str]) -> int:
    stamp = db.now()
    all_rows: list[dict] = []
    for at in account_types:
        all_rows += fetch_lives(at, start, end)
    for r in all_rows:
        r["synced_at"] = stamp

    conn = db.connect()
    with conn:
        ensure_schema(conn)
        conn.executemany(
            """
            INSERT OR REPLACE INTO tiktok_shop_lives
              (live_id, account_type, title, username, start_time, end_time,
               views, viewers, avg_viewing_duration, product_impressions,
               product_clicks, click_through_rate, likes, comments, shares,
               new_followers, gmv, currency, live_gmv_24h, items_sold,
               sku_orders, created_sku_orders, customers,
               different_products_sold, avg_price, click_to_order_rate,
               window_start, window_end, synced_at)
            VALUES
              (:live_id, :account_type, :title, :username, :start_time, :end_time,
               :views, :viewers, :avg_viewing_duration, :product_impressions,
               :product_clicks, :click_through_rate, :likes, :comments, :shares,
               :new_followers, :gmv, :currency, :live_gmv_24h, :items_sold,
               :sku_orders, :created_sku_orders, :customers,
               :different_products_sold, :avg_price, :click_to_order_rate,
               :window_start, :window_end, :synced_at)
            """,
            all_rows,
        )
    conn.close()
    return len(all_rows)


# ---------------------------------------------------------------------------
# (2) per-product, per-day LIVE-attributed funnel
# ---------------------------------------------------------------------------

def _day_sellers(day: str, nxt: str) -> list[str]:
    """Product ids that sold anything on `day`, top-GMV first."""
    ids: list[str] = []
    page_token = ""
    while len(ids) < MAX_SELLERS_PER_DAY:
        params = {
            "app_key": os.environ["TIKTOK_APP_KEY"],
            "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"],
            "start_date_ge": day, "end_date_lt": nxt,
            "page_size": "100", "currency": "USD",
            "sort_field": "gmv", "sort_order": "DESC",
        }
        if page_token:
            params["page_token"] = page_token
        d = _request(LIST_PATH, params).get("data", {}) or {}
        for p in d.get("products", []):
            gmv = _num(((p.get("overall_performance") or {}).get("gmv") or {}).get("amount"))
            if gmv <= 0:
                return ids  # GMV-sorted: first zero => the rest are zero
            ids.append(p["id"])
            if len(ids) >= MAX_SELLERS_PER_DAY:
                break
        page_token = d.get("next_page_token") or ""
        if not page_token:
            break
    return ids


def _live_slice(breakdowns, key: str = "amount") -> float:
    for b in breakdowns or []:
        if b.get("type") == "LIVE":
            return _num(b.get(key))
    return 0.0


def _live_row(pid: str, day: str, nxt: str) -> dict | None:
    """This product's LIVE-attributed funnel for `day`; None if there was no
    LIVE activity at all that day (the endpoint returns an interval regardless
    of whether anything actually happened)."""
    params = {
        "app_key": os.environ["TIKTOK_APP_KEY"],
        "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"],
        "start_date_ge": day, "end_date_lt": nxt, "currency": "USD",
    }
    d = _request(DETAIL_TMPL.format(pid=pid), params).get("data", {}) or {}
    intervals = (d.get("performance") or {}).get("intervals") or []
    if not intervals:
        return None
    iv = intervals[0]
    impr = int(_live_slice(iv.get("impression_breakdowns")))
    clicks = int(_live_slice(iv.get("page_view_breakdowns")))
    units = int(_live_slice(iv.get("unit_sold_breakdowns")))
    gmv = _live_slice(iv.get("gmv_breakdowns"))
    visitors = int(_live_slice(iv.get("avg_page_visitor_breakdowns")))
    if impr == 0 and clicks == 0 and units == 0 and gmv == 0:
        return None
    currency = next((b.get("currency") for b in iv.get("gmv_breakdowns") or [] if b.get("type") == "LIVE"), None)
    return {
        "product_id": pid, "date": day, "live_impressions": impr, "live_clicks": clicks,
        "live_units": units, "live_gmv": round(gmv, 2), "currency": currency,
        "live_avg_visitors": visitors,
    }


def own_live_days(conn, start: str, end: str) -> list[str]:
    """Shop-local calendar days within [start, end) that OWN_ACCOUNT_TYPE has a
    recorded broadcast for in `tiktok_shop_lives`. Used to scope the
    (expensive, one-call-per-candidate-product) live-products crawl to days
    something actually happened, instead of every day in the window."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tiktok_shop_lives'"
    ).fetchone()
    if not has_table:
        return []
    rows = conn.execute(
        "SELECT DISTINCT start_time FROM tiktok_shop_lives "
        "WHERE account_type = ? AND start_time IS NOT NULL", (OWN_ACCOUNT_TYPE,)
    ).fetchall()
    days = set()
    for (st,) in rows:
        try:
            d = datetime.fromtimestamp(int(float(st)), SHOP_TZ).date().isoformat()
        except (ValueError, OverflowError, OSError, TypeError):
            continue
        if start <= d < end:
            days.add(d)
    return sorted(days)


def sync_live_products(days: list[str]) -> int:
    stamp = db.now()
    rows: list[dict] = []
    for day in days:
        nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        sellers = _day_sellers(day, nxt)
        kept = 0
        for pid in sellers:
            row = _live_row(pid, day, nxt)
            if row:
                row["synced_at"] = stamp
                rows.append(row)
                kept += 1
            time.sleep(0.05)  # pace per-product calls; a tight burst gets connection-reset
        print(f"  {day}: {len(sellers)} sellers scanned, {kept} with LIVE activity")

    conn = db.connect()
    with conn:
        ensure_schema(conn)
        for day in days:
            conn.execute("DELETE FROM tiktok_shop_live_products WHERE date = ?", (day,))
        conn.executemany(
            """
            INSERT OR REPLACE INTO tiktok_shop_live_products
              (product_id, date, live_impressions, live_clicks, live_units,
               live_gmv, currency, live_avg_visitors, synced_at)
            VALUES
              (:product_id, :date, :live_impressions, :live_clicks, :live_units,
               :live_gmv, :currency, :live_avg_visitors, :synced_at)
            """,
            rows,
        )
    conn.close()
    return len(rows)


def reconcile(days: list[str]) -> None:
    """Sanity check, not an exact reconciliation: sum this day's per-product
    LIVE gmv and compare it to the day's OWN_ACCOUNT_TYPE session GMV. They
    won't match exactly (attribution rules and account-type scope differ
    between the two endpoints), but a ratio far outside a sane band usually
    means a partial pull worth investigating."""
    if not days:
        return
    conn = db.connect()
    try:
        best: dict[str, tuple] = {}
        for live_id, gmv, start_time, synced_at in conn.execute(
            "SELECT live_id, gmv, start_time, synced_at FROM tiktok_shop_lives "
            "WHERE account_type = ? AND start_time IS NOT NULL", (OWN_ACCOUNT_TYPE,)
        ):
            prev = best.get(live_id)
            if prev is None or (synced_at or "") > (prev[2] or ""):
                best[live_id] = (gmv, start_time, synced_at)
        own_gmv_by_day: dict[str, float] = {}
        for gmv, start_time, _ in best.values():
            try:
                d = datetime.fromtimestamp(int(float(start_time)), SHOP_TZ).date().isoformat()
            except (ValueError, OverflowError, OSError, TypeError):
                continue
            if d in days:
                own_gmv_by_day[d] = own_gmv_by_day.get(d, 0.0) + float(gmv or 0)
        for day in days:
            product_gmv = conn.execute(
                "SELECT COALESCE(SUM(live_gmv), 0) FROM tiktok_shop_live_products WHERE date = ?", (day,)
            ).fetchone()[0]
            session_gmv = own_gmv_by_day.get(day, 0.0)
            if session_gmv > 0:
                ratio = product_gmv / session_gmv
                flag = "  ** CHECK **" if (ratio < 0.70 or ratio > 1.20) else ""
                print(f"  reconcile {day}: product-LIVE ${product_gmv:,.0f} vs "
                      f"session ${session_gmv:,.0f} ({ratio:.0%}){flag}")
    finally:
        conn.close()


def _daterange(start: str, end: str) -> list[str]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)  # end exclusive
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days)]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start")
    p.add_argument("--end", help="exclusive")
    p.add_argument("--dates", help="explicit comma list of YYYY-MM-DD for the products crawl "
                                    "(overrides the own-live-days auto-detection)")
    p.add_argument("--account-types", default="OFFICIAL_ACCOUNTS,AFFILIATE_ACCOUNTS,MARKETING_ACCOUNTS",
                    help="comma list: ALL, OFFICIAL_ACCOUNTS, AFFILIATE_ACCOUNTS, MARKETING_ACCOUNTS")
    p.add_argument("--only", default="lives,products",
                    help="comma list: lives, products (default both)")
    args = p.parse_args()

    check_required_env()

    end = args.end or date.today().isoformat()
    start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()
    account_types = [t.strip() for t in args.account_types.split(",") if t.strip()]
    steps = {s.strip() for s in args.only.split(",") if s.strip()}

    db.init_db()

    if "lives" in steps:
        started = db.now()
        try:
            n = sync_lives(start, end, account_types)
        except Exception as e:  # noqa: BLE001
            db.log_sync(f"{PLATFORM}_sessions", started, 0, "error", str(e))
            raise
        db.log_sync(f"{PLATFORM}_sessions", started, n, "ok", f"{start} -> {end}")
        print(f"TikTok live sessions: wrote {n} rows ({start} -> {end}) [{', '.join(account_types)}]")

    if "products" in steps:
        if args.dates:
            days = [d.strip() for d in args.dates.split(",") if d.strip()]
        else:
            conn = db.connect()
            try:
                days = own_live_days(conn, start, end)
            finally:
                conn.close()
        started = db.now()
        if not days:
            db.log_sync(f"{PLATFORM}_products", started, 0, "ok", "no own-account live days in window")
            print("TikTok live products: no own-account live days in window -- nothing to pull.")
        else:
            try:
                n = sync_live_products(days)
            except Exception as e:  # noqa: BLE001
                db.log_sync(f"{PLATFORM}_products", started, 0, "error", str(e))
                raise
            reconcile(days)
            db.log_sync(f"{PLATFORM}_products", started, n, "ok", f"{days[0]}..{days[-1]} ({len(days)}d)")
            print(f"TikTok live products: wrote {n} rows across {len(days)} day(s)")
