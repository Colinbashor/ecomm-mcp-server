r"""
TikTok Shop performance -- GMV split by content-type source -> warehouse.

Endpoint: GET /analytics/202405/shop/performance
Scope needed: data.shop_analytics.public.read

THIS IS THE SALES-SOURCE ANSWER. TikTok attributes every order to exactly one
of LIVE / VIDEO / PRODUCT_CARD, and this endpoint hands over that split
directly -- a true, mutually-exclusive breakdown of where GMV came from. That
beats the cheaper-but-wrong approach of estimating a "source mix" by
subtracting known video GMV from total order sales and calling the remainder
"unattributed" -- that remainder is really just PRODUCT_CARD (organic/browse
discovery) hiding under a vague label.

The breakdown is not GMV-only: buyers, product impressions, product page
views, and avg product page visitors all carry the same three-way split, so
this is a full mini-funnel by content type, not just a revenue split.

ILLUSTRATIVE EXAMPLE (not real data -- shape only): a shop that leans on
organic video content might see something like VIDEO ~55%, PRODUCT_CARD
~40%, LIVE ~5% of GMV in a given period, with LIVE's share climbing as the
shop invests more in live-shopping. Your own mix will differ completely by
category, content strategy, and how long you've been running LIVE.

Totals from this endpoint are expected to be CLOSE to but not identical to
your order-level revenue table -- typically within a few percent -- because
this is GMV (booked at time of sale) rather than net sales after refunds and
cancellations settle. Use your orders table for revenue truth and this
table for MIX (what fraction of sales came from where).

AUTH SETUP
Reuses the TikTok Shop app credentials and access/refresh-token machinery
from `warehouse/connectors/tiktok_shop.py` (see that file's docstring for the
full OAuth setup). No new credentials are needed for this script -- if
TikTok orders sync already works, this does too.

GRAIN: one row per (date, content_type). content_type is LIVE | VIDEO |
PRODUCT_CARD | TOTAL. The TOTAL row additionally carries shop-level metrics
that have NO per-type breakdown (orders, sku_orders, units_sold,
avg_order_value, refunds, cancellations_and_returns); those columns are NULL
on the three type rows. So: SUM the three type rows for a mix, read TOTAL for
shop-wide scalars -- never add TOTAL to the type rows, or you double everything.

GENERIC GOTCHAS (all verified against the live API)
* Re-running a date is harmless: rows upsert on (date, content_type), so a
  daily job that re-pulls a trailing window just refreshes it -- TikTok
  restates recent days as refunds and cancellations land.
* WINDOW CAP: `granularity` accepts only ALL or 1D, and a long single request
  eventually fails with a distinct "invalid parameter" business error code
  once the requested span gets too wide. Requests are therefore chunked
  (CHUNK_DAYS) to stay comfortably under that ceiling. That SAME error code
  is also what you get when a chunk reaches back further than TikTok's
  retention window for this report -- so on a deep historical backfill, a
  chunk failing with that code is reported and skipped (not treated as fatal)
  rather than killing the whole run, since it usually just means "this range
  predates what TikTok will serve here."
* NEVER STORE A PARTIAL TRAILING DAY: the response includes a
  `latest_available_date` field marking how far TikTok has actually finalized
  data; this script drops any fetched rows dated after that point before
  writing, so an in-progress "today" row (which will keep changing as the day
  goes on) never lands in the table looking like a settled number.
* SCOPE RE-AUTH TRAP (same lesson as the sibling TikTok connectors): a TikTok
  Shop refresh token belongs to ONE authorization grant with a fixed set of
  scopes. Re-authorizing the app later for a narrower scope selection
  silently replaces the whole grant, including scopes you already depended
  on. Nothing breaks immediately -- the current access token keeps working
  until it expires, then every call needing the dropped scope starts failing
  with business error code 105005 ("no permission"). Always re-authorize with
  the FULL set of scopes your integration needs.

USAGE
  python tiktok_analytics_sync.py                  # last 30 days
  python tiktok_analytics_sync.py --days 90
  python tiktok_analytics_sync.py --start 2026-01-01 --end 2026-04-01
  python tiktok_analytics_sync.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors.tiktok_shop import TOKEN_EXPIRED_CODES, _refresh_access_token

load_dotenv()

BASE = "https://open-api.tiktokglobalshop.com"
PATH = "/analytics/202405/shop/performance"
PLATFORM = "tiktok_analytics"

REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

CHUNK_DAYS = 60          # stays comfortably under the API's window-length ceiling
RETENTION_CODE = 28001022  # "invalid parameter" -- too-wide span OR older than retention
TYPES = ("LIVE", "VIDEO", "PRODUCT_CARD")

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_shop_performance (
    date                      TEXT NOT NULL,
    content_type              TEXT NOT NULL,   -- LIVE | VIDEO | PRODUCT_CARD | TOTAL
    gmv                       REAL,
    buyers                    INTEGER,
    product_impressions       INTEGER,
    product_page_views        INTEGER,
    avg_product_page_visitors INTEGER,
    -- shop-level only (content_type='TOTAL'); no per-type breakdown exists
    orders                    INTEGER,
    sku_orders                INTEGER,
    units_sold                INTEGER,
    avg_order_value           REAL,
    refunds                   REAL,
    cancellations_and_returns INTEGER,
    currency                  TEXT,
    synced_at                 TEXT NOT NULL,
    PRIMARY KEY (date, content_type)
);
"""
INDEX_DDL = ("CREATE INDEX IF NOT EXISTS idx_tsp_date ON tiktok_shop_performance(date)",)

COLS = ("date", "content_type", "gmv", "buyers", "product_impressions",
        "product_page_views", "avg_product_page_visitors", "orders", "sku_orders",
        "units_sold", "avg_order_value", "refunds", "cancellations_and_returns",
        "currency", "synced_at")
UPSERT = (f"INSERT OR REPLACE INTO tiktok_shop_performance ({','.join(COLS)}) "
          f"VALUES ({','.join('?' * len(COLS))})")


def ensure_schema(conn) -> None:
    conn.executescript(DDL)
    for ddl in INDEX_DDL:
        conn.execute(ddl)


def check_required_env() -> None:
    """Raise a clear SystemExit (not a KeyError deep in a request) when
    credentials are missing, so a misconfigured .env fails fast and legibly."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


def _sign(params: dict, secret: str) -> str:
    """HMAC-SHA256 over app_secret + path + sorted(query k+v, excluding sign/
    access_token) + app_secret. Same recipe as the sibling connectors' _sign."""
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    return hmac.new(secret.encode(), f"{secret}{PATH}{ordered}{secret}".encode(),
                     hashlib.sha256).hexdigest()


def _request(params: dict) -> dict:
    """One signed GET. Refreshes an expired access token once, then retries."""
    secret = os.environ["TIKTOK_APP_SECRET"]
    for attempt in (1, 2):
        params["timestamp"] = str(int(time.time()))
        params.pop("sign", None)
        params["sign"] = _sign(params, secret)
        r = requests.get(f"{BASE}{PATH}", params=params,
                          headers={"content-type": "application/json",
                                   "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]},
                          timeout=60)
        data = r.json()
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and attempt == 1:
            _refresh_access_token()
            continue
        if code != 0:
            raise RuntimeError(f"TikTok analytics API {r.status_code} code={code}: "
                                f"{data.get('message')}", code)
        return data
    raise RuntimeError("TikTok request failed even after refreshing the access token.")


def _num(x, cast=float):
    return None if x is None else cast(x)


def _amount(block):
    return _num((block or {}).get("amount")) if block else None


def _bd(interval, key):
    """breakdown array -> {content_type: amount}"""
    out = {}
    for b in interval.get(key) or []:
        v = b.get("amount")
        out[b.get("type")] = float(v) if isinstance(v, str) else v
    return out


def fetch_range(start: str, end: str) -> tuple[list[tuple], str | None]:
    """Rows for [start, end). Returns (rows, latest_available_date)."""
    params = {
        "app_key": os.environ["TIKTOK_APP_KEY"],
        "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"],
        "start_date_ge": start,
        "end_date_lt": end,
        "granularity": "1D",
        "currency": "USD",
    }
    data = _request(params).get("data", {}) or {}
    latest = data.get("latest_available_date")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[tuple] = []
    for iv in (data.get("performance", {}) or {}).get("intervals", []) or []:
        day = iv.get("start_date")
        gmv_block = iv.get("gmv") or {}
        currency = gmv_block.get("currency")
        gmv_bd = _bd(iv, "gmv_breakdowns")
        buy_bd = _bd(iv, "buyer_breakdowns")
        imp_bd = _bd(iv, "product_impression_breakdowns")
        pv_bd = _bd(iv, "product_page_view_breakdowns")
        vis_bd = _bd(iv, "avg_product_page_visitor_breakdowns")
        for t in TYPES:
            rows.append((day, t, gmv_bd.get(t), buy_bd.get(t), imp_bd.get(t),
                         pv_bd.get(t), vis_bd.get(t),
                         None, None, None, None, None, None, currency, now))
        rows.append((
            day, "TOTAL", _amount(gmv_block), _num(iv.get("buyers"), int),
            _num(iv.get("product_impressions"), int), _num(iv.get("product_page_views"), int),
            _num(iv.get("avg_product_page_visitors"), int),
            _num(iv.get("orders"), int), _num(iv.get("sku_orders"), int),
            _num(iv.get("units_sold"), int), _amount(iv.get("avg_order_value")),
            _amount(iv.get("refunds")), _num(iv.get("cancellations_and_returns"), int),
            currency, now))
    return rows, latest


def chunks(start: date, end: date):
    """[start, end) split into <= CHUNK_DAYS spans."""
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=CHUNK_DAYS), end)
        yield cur.isoformat(), nxt.isoformat()
        cur = nxt


def fetch_window(start: str, end: str) -> tuple[list[tuple], list[str]]:
    """Fetch [start, end) in chunks, dropping any unsettled trailing day and
    skipping (rather than failing on) chunks outside TikTok's retention.
    Returns (rows, skipped_chunk_labels)."""
    all_rows: list[tuple] = []
    latest = None
    skipped: list[str] = []
    for c0, c1 in chunks(date.fromisoformat(start), date.fromisoformat(end)):
        try:
            rows, lat = fetch_range(c0, c1)
        except RuntimeError as e:
            code = e.args[1] if len(e.args) > 1 else None
            if code == RETENTION_CODE:
                # outside TikTok's retention (or an over-long span) -- expected on
                # deep backfills, so report and keep going rather than abort.
                skipped.append(f"{c0}->{c1}")
                print(f"  skip {c0} -> {c1}: outside TikTok retention ({RETENTION_CODE})")
                continue
            raise
        latest = lat or latest
        all_rows += rows
        days = len({r[0] for r in rows})
        print(f"  {c0} -> {c1}: {days} days, {len(rows)} rows")
        time.sleep(0.3)

    if latest:
        # Never store a partial trailing day: TikTok restates until the day closes.
        before = len(all_rows)
        all_rows = [r for r in all_rows if r[0] <= latest]
        if before != len(all_rows):
            print(f"  dropped {before - len(all_rows)} rows after "
                  f"latest_available_date={latest}")
    return all_rows, skipped


def sync(start: str, end: str) -> int:
    rows, _skipped = fetch_window(start, end)
    if not rows:
        return 0
    conn = db.connect()
    with conn:
        ensure_schema(conn)
        conn.executemany(UPSERT, rows)
    conn.close()
    return len(rows)


def _mix_summary(rows: list[tuple]) -> str:
    mix: dict[str, float] = {}
    for r in rows:
        if r[1] in TYPES and r[2]:
            mix[r[1]] = mix.get(r[1], 0) + r[2]
    tot = sum(mix.values()) or 1
    lines = [f"  {t:<13} ${mix.get(t, 0):>12,.0f}  {mix.get(t, 0) / tot * 100:>5.1f}%" for t in TYPES]
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30, help="trailing days (default 30)")
    p.add_argument("--start", help="YYYY-MM-DD inclusive (overrides --days)")
    p.add_argument("--end", help="YYYY-MM-DD EXCLUSIVE (default: today)")
    p.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = p.parse_args()

    check_required_env()

    end = args.end or date.today().isoformat()
    start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()
    if start >= end:
        p.error("--start must be before --end")

    db.init_db()
    started = db.now()

    if args.dry_run:
        rows, skipped = fetch_window(start, end)
        days = sorted({r[0] for r in rows})
        print(f"\n{len(rows)} rows across {len(days)} days"
              + (f" ({days[0]} .. {days[-1]})" if days else ""))
        if skipped:
            print(f"skipped {len(skipped)} chunk(s) outside retention: {', '.join(skipped)}")
        print("DRY RUN -- nothing written. GMV mix over the window:")
        print(_mix_summary(rows))
        raise SystemExit(0)

    try:
        n = sync(start, end)
    except Exception as e:  # noqa: BLE001
        db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    db.log_sync(PLATFORM, started, n, "ok", f"{start} -> {end}")
    print(f"TikTok analytics: wrote {n} rows to tiktok_shop_performance ({start} -> {end})")
