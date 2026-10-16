#!/usr/bin/env python
"""TikTok Shop seller-side Affiliate API -> per-line creator commission + a creator id crosswalk.

WHAT IT GIVES YOU
  Two things a TikTok Shop seller usually cannot get from the Order API alone:

  1. Per-SKU-line affiliate attribution: which creator (handle), which piece
     of content (video / LIVE / showcase), the commission rate, and the
     estimated and actual commission TikTok computed for that line.
  2. A STABLE creator identity for your sample (free-product) orders. The
     Order API exposes the creator only as a display name plus a buyer
     `user_id`; display names change and rarely match handles, and the video
     / affiliate APIs key creators by handle or by `creator_open_id`. This
     connector bridges them through the sample order itself.

  Both come from two endpoints the normal SELLER token reaches (the
  `affiliate_creator/*` namespace needs a creator-type authorization and is a
  different thing — do not conflate them):

    POST /affiliate_seller/202410/orders/search
        Affiliate (commission-eligible) orders. Per SKU line: creator_username
        (the HANDLE), content_id, content_type, commission rate, estimated and
        actual paid commission, Shop Ads commission. -> tiktok_affiliate_order_lines
    POST /affiliate_seller/202508/sample_applications/search
        Sample applications. Maps each sample `order_id` to creator_open_id,
        username and nickname. -> tiktok_affiliate_sample_applications

API BEHAVIOUR (measured against the live API; encoded below)
  orders/search
    * The create_time window is capped at 3 MONTHS (code 2000011134
      "time_range exceeds the allowed range"). Newest first. page_size 1..100
      (36009004 above). This uses 7-day windows and splits any window whose
      total_count is >= SPLIT_AT before paging it.
    * page_size AND total_count COUNT SKU LINES, NOT ORDERS: a 100-line page
      can hold fewer than 100 orders. So (a) coverage compares total_count to
      LINES, and (b) an order can straddle two pages, which is why pages are
      merged per order id (_merge_order) instead of the second half
      overwriting the first. Paging stops on an EMPTY page or a MISSING
      token, never a short one. The token is cursor-shaped
      ([create_ms, order_id]).
    * The live payload carries fields the published OpenAPI spec does not
      list: sku_id, settlement_status, fully_return, is_carousel,
      *_paid_partner_commission. The order-level `status` the spec documents
      is ABSENT (always null).
    * Money objects are `{}` until TikTok has computed them (actual_* before
      settlement, the estimated commission on a Shop-Ads-only line). `{}` is
      stored NULL, never 0: a blank is not a measured zero.
    * commission_rate / shop_ads_commission_rate are HUNDREDTHS OF A PERCENT
      ("800" = 8.00%); stored as *_pct = raw / 100. The sample endpoint's
      commission_rate is a FRACTION ("0.10") instead.
  sample_applications/search
    * NO date filter and NO create time in the response. page_size 1..50.
    * THE LIST ONLY REACHES THE LATEST 10,000 PER QUERY. The page token is a
      plain offset ("offset=50"), offset 10,000 fails with 98001004 "Only the
      latest 10,000 sample applications can be queried", and total_count
      saturates at 9999 to match. That is recorded as `capped`, not as an
      error and not as complete. A shop with a long sampling history has two
      routes past it: `--by-product` slices COMPLETED per product (one call
      usually resolves every sample of a product), and `--lookup` filters by
      `order_id`, which returns the single application behind any sample
      order. The COMPLETED filter also returns OPS_COMPLETED rows.
    * order_id is the string "0" until the seller approves; stored NULL.
    * Rate limited (36009037) at roughly 35-45 calls/min; ~25/min runs clean,
      hence SAMPLES_PACE_SECONDS. The orders endpoint tolerated ~60/min. A
      limit that never clears pauses the run: exit 75, sync_log `degraded`.

IDENTITY (the reason for the crosswalk)
  `orders.creator_id` (the Order API buyer user_id on a sample order, a long
  integer) is NOT `creator_open_id` (an opaque per-app token string): they are
  different id spaces and never compare equal. The SAMPLE ORDER is the bridge:
  application -> order_id -> orders.creator_id. Handles are mutable, so
  everything is keyed on creator_open_id and handles are kept as a history
  with OUR observation times (TikTok does not timestamp a rename).

TABLES
  tiktok_affiliate_order_lines     one row per order SKU line, raw + named columns
  tiktok_affiliate_order_coverage  every orders window ASKED, and what came back
  tiktok_affiliate_sample_applications  one row per application id
  tiktok_affiliate_sample_coverage every sample partition / order_id ASKED
  tiktok_creator_handle_history    (creator_open_id, username) with first/last seen
  tiktok_sample_order_creator      sample order -> application -> open_id -> orders.creator_id
  tiktok_creator_xwalk             one row per creator_open_id (rebuilt every run)

  Coverage tables record what was ASKED, never inferred from missing rows: a
  window or a sample order can legitimately have nothing, and keying resume on
  absent rows would re-request it forever. Each unfound sample order is
  re-asked at most MAX_LOOKUP_ATTEMPTS times, LOOKUP_RETRY_DAYS apart.

DEPENDS ON
  * `orders` rows for platform 'tiktok' with `is_sample` / `creator_id`
    (written by warehouse/connectors/tiktok_shop.py via run_sync.py) for the
    sample-order bridge. Without them the order-line pull still works; the
    lookup and crosswalk just have nothing to resolve.
  * OPTIONAL `catalog_tiktok (seller_sku, product_id)` for `--by-product`:
    `orders.sku` is the seller SKU, and slicing needs the TikTok product id.
    If you have no such table, pass `--product-ids` explicitly, or rely on
    `--lookup` (slower, one call per order, but needs nothing else).
  * OPTIONAL `tiktok_creator_marketplace` (tiktok_creator_marketplace_sync.py):
    its exact-handle matches are folded into the handle history.

AUTH SETUP
  Reuses the TikTok Shop app credentials and token refresh of
  warehouse/connectors/tiktok_shop.py: `TIKTOK_APP_KEY`, `TIKTOK_APP_SECRET`,
  `TIKTOK_ACCESS_TOKEN`, `TIKTOK_SHOP_CIPHER` (plus `TIKTOK_REFRESH_TOKEN`
  for the refresh). The app needs the affiliate-seller scope; re-authorise
  with ALL scopes at once (a partial re-auth silently drops the others).
  `TIKTOK_SHOP_FLOOR` (optional, YYYY-MM-DD, default 2023-09-01) is the
  earliest date worth asking for and the anchor of the fixed 7-day window grid.

RECOMMENDED SCHEDULE
  daily    orders --days 14         re-pulls the last two to three grid weeks
  daily    samples --lookup         new sample orders (last 30 days) + crosswalk rebuild
  weekly   orders --days 60         commission / settlement keep changing for weeks
  weekly   samples --list --lookup  status refresh of every order-bearing status
  Run the lookup AFTER the order sync that writes `orders`, since it reads
  the sample orders from there. Treat exit 75 as a tolerated pause.

USAGE
  python tiktok_affiliate_sync.py orders --days 14                 # trailing re-pull
  python tiktok_affiliate_sync.py orders --start 2024-01-01        # backfill; skips complete windows
  python tiktok_affiliate_sync.py samples --list                   # statuses that carry orders
  python tiktok_affiliate_sync.py samples --list --all-statuses    # plus PENDING/REJECTED/...
  python tiktok_affiliate_sync.py samples --by-product --since 2024-01-01 [--max-products N]
  python tiktok_affiliate_sync.py samples --by-product --product-ids 1729000000001,1729000000002
  python tiktok_affiliate_sync.py samples --lookup --since 2025-01-01 [--max-lookups N]
  (a backfill runs them in that order: list, then product slices, then lookups
  for whatever is still unresolved; each step skips what is already resolved.
  Without --since, product slices and lookups cover the last 30 days only —
  a full-history lookup is hours at ~25 calls/min.)
  python tiktok_affiliate_sync.py xwalk                            # rebuild crosswalk only
  python tiktok_affiliate_sync.py report [--split YYYY-MM-DD]      # coverage numbers, read-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors import tiktok_shop as tts

load_dotenv()

ORDERS_PATH = "/affiliate_seller/202410/orders/search"
SAMPLES_PATH = "/affiliate_seller/202508/sample_applications/search"
ORDERS_PAGE_SIZE = 100      # API max; 101 -> 36009004
SAMPLES_PAGE_SIZE = 50      # API max; 51 -> 36009004
ORDER_WINDOW_DAYS = 7       # API max is ~90 (code 2000011134)
SPLIT_AT = 9000             # split a window before paging past a possible 10k depth cap
MAX_PAGES = 400             # per window / per partition: a runaway-token guard, not a limit we expect
SAMPLE_LIST_CAP = 9999      # total_count saturates here
LIST_DEPTH_CODE = 98001004  # "Only the latest 10,000 sample applications can be queried" (offset 10000)
# Earliest date worth asking for (TikTok Shop US opened in 2023-09) and the
# anchor of the fixed order-window grid. Override per shop with TIKTOK_SHOP_FLOOR.
SHOP_FLOOR = os.environ.get("TIKTOK_SHOP_FLOOR") or "2023-09-01"
GRID_ANCHOR = dt.datetime.strptime(SHOP_FLOOR, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)

# A sample order with NO application behind it is real (measured ~0.25% of
# sample orders in one shop -- most likely samples sent outside the
# application flow), and each is re-asked up to MAX_LOOKUP_ATTEMPTS times. So a
# few misses are the normal state; the run only reads `degraded` when misses
# exceed BOTH floors.
LOOKUP_MISS_TOLERANCE = 3
LOOKUP_MISS_SHARE = 0.10

RATE_LIMIT_CODE = 36009037
MAX_RATE_LIMIT_RETRIES = 8
ORDERS_PACE_SECONDS = 0.5
SAMPLES_PACE_SECONDS = 2.0  # ~25/min measured clean; 36009037 appears around 35-45/min

PLATFORM_ORDERS = "tiktok_affiliate_orders"
PLATFORM_SAMPLES = "tiktok_affiliate_samples"
PLATFORM_XWALK = "tiktok_creator_xwalk"
EXIT_PAUSED = 75  # deliberate pause on an upstream fault (matches the rest of the repo)
REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

# Statuses an application passes through AFTER the seller approved it, i.e. the
# ones that can carry a sample order_id. PENDING / REJECT / WITHDRAW / OVERDUE
# never had an order; --all-statuses adds them (handle history only).
ORDER_STATUSES = (
    "AWAITING_SHIPMENT", "SHIPPED", "CONTENT_PENDING", "COMPLETED",
    "UNFULFILL_CANCELLED", "UNFULFILLABLE_CANCELLED", "SELLER_NOT_SHIP_CANCELLED",
    "OPS_CANCELLED", "OPS_FAILED", "OPS_COMPLETED", "DEL_OPEN_COLLAB",
)
NO_ORDER_STATUSES = ("PENDING", "REJECT_CANCELLED", "OVERDUE_CANCELLED", "WITHDRAW_CANCELLED")

# A sample order looked up and NOT found is re-asked at most this many times,
# no more often than LOOKUP_RETRY_DAYS, so a missing row can never become an
# infinite re-request yet a transient miss still heals.
MAX_LOOKUP_ATTEMPTS = 3
LOOKUP_RETRY_DAYS = 7

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_affiliate_order_lines (
    order_id                 TEXT NOT NULL,
    line_no                  INTEGER NOT NULL,  -- position in the order's skus[]; the whole
                                                -- order is replaced on every pull, so this
                                                -- never goes stale
    order_create_time        TEXT,              -- ISO UTC
    order_date               TEXT,              -- UTC date, same convention as orders.order_date
    delivery_time            TEXT,              -- ISO UTC; NULL until delivered
    order_status             TEXT,              -- documented, but absent from the live payload
    sku_id                   TEXT,
    product_id               TEXT,
    quantity                 INTEGER,
    refunded_quantity        INTEGER,
    returned_quantity        INTEGER,
    fully_return             TEXT,
    settlement_status        TEXT,
    is_carousel              INTEGER,
    content_type             TEXT,              -- SHOP | VIDEO | LIVE | PRE_LIVE | PROMOTION_PAGE | LINKSHARE ...
    content_id               TEXT,
    creator_username         TEXT,              -- HANDLE at pull time; mutable
    price                    REAL,              -- SKU selling price (unit)
    currency                 TEXT,
    commission_model         TEXT,
    commission_rate_pct      REAL,              -- raw is hundredths of a percent
    commission_tier_setting  TEXT,
    shop_ads_commission_rate_pct REAL,
    campaign_id              TEXT,
    open_collaboration_id    TEXT,
    target_collaboration_id  TEXT,
    est_commission_base      REAL,              -- NULL = TikTok had not computed it, never 0
    est_paid_commission      REAL,
    est_shop_ads_commission  REAL,
    est_partner_commission   REAL,
    est_cofunded_bonus       REAL,
    act_commission_base      REAL,
    act_paid_commission      REAL,
    act_shop_ads_commission  REAL,
    act_partner_commission   REAL,
    act_cofunded_bonus       REAL,
    raw_json                 TEXT,
    synced_at                TEXT NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_tt_aff_lines_date ON tiktok_affiliate_order_lines (order_date);
CREATE INDEX IF NOT EXISTS idx_tt_aff_lines_creator ON tiktok_affiliate_order_lines (creator_username);
CREATE INDEX IF NOT EXISTS idx_tt_aff_lines_product ON tiktok_affiliate_order_lines (product_id);

CREATE TABLE IF NOT EXISTS tiktok_affiliate_order_coverage (
    window_start     TEXT NOT NULL,   -- ISO UTC, inclusive
    window_end       TEXT NOT NULL,   -- ISO UTC, exclusive
    total_count      INTEGER,         -- TikTok's count for the window: SKU LINES, not orders
    orders_returned  INTEGER,         -- distinct orders actually paged out
    lines_written    INTEGER,
    pages            INTEGER,
    status           TEXT NOT NULL,   -- complete | short | failed
    message          TEXT,
    asked_at         TEXT NOT NULL,
    PRIMARY KEY (window_start, window_end)
);

CREATE TABLE IF NOT EXISTS tiktok_affiliate_sample_applications (
    application_id           TEXT PRIMARY KEY,
    order_id                 TEXT,             -- NULL while unapproved (API sends "0")
    status                   TEXT,
    fulfillment_status       TEXT,
    product_id               TEXT,
    sku_id                   TEXT,
    product_title            TEXT,
    sku_name                 TEXT,
    creator_open_id          TEXT,
    creator_username         TEXT,             -- handle when last seen
    creator_nickname         TEXT,             -- display name when last seen (= orders.creator)
    follower_count           INTEGER,          -- creator-level snapshot when last seen
    commission_rate_frac     REAL,             -- FRACTION here ("0.10"), unlike order lines
    partner_name             TEXT,
    tracking_number          TEXT,
    approve_expiration_time  TEXT,
    shipment_expiration_time TEXT,
    source                   TEXT,             -- list:<STATUS> | order_lookup
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tt_aff_apps_order ON tiktok_affiliate_sample_applications (order_id);
CREATE INDEX IF NOT EXISTS idx_tt_aff_apps_creator ON tiktok_affiliate_sample_applications (creator_open_id);

CREATE TABLE IF NOT EXISTS tiktok_affiliate_sample_coverage (
    kind          TEXT NOT NULL,   -- status | product | order
    key           TEXT NOT NULL,   -- status name | '<product_id>|COMPLETED' | sample order_id
    total_count   INTEGER,         -- status/product: TikTok's count (saturates at 9999)
    rows_returned INTEGER,
    pages         INTEGER,
    capped        INTEGER,         -- 1 when the slice hit the 10k list ceiling (98001004 / 9999)
    found         INTEGER,         -- order: 1 when an application came back
    attempts      INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL,   -- complete | short | failed
    message       TEXT,
    asked_at      TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS tiktok_creator_handle_history (
    creator_open_id TEXT NOT NULL,
    username        TEXT NOT NULL,
    nickname        TEXT,           -- latest display name seen alongside this handle
    first_seen_at   TEXT NOT NULL,  -- OUR observation time; TikTok does not date a rename
    last_seen_at    TEXT NOT NULL,
    sources         TEXT,           -- comma list: sample_applications, marketplace
    PRIMARY KEY (creator_open_id, username)
);
CREATE INDEX IF NOT EXISTS idx_tt_handle_hist_user ON tiktok_creator_handle_history (username);

CREATE TABLE IF NOT EXISTS tiktok_sample_order_creator (
    order_id         TEXT NOT NULL,
    application_id   TEXT NOT NULL,
    creator_open_id  TEXT,
    creator_username TEXT,
    tiktok_user_id   TEXT,           -- orders.creator_id on that sample order (NULL: order not in `orders`)
    order_date       TEXT,
    product_id       TEXT,
    sku_id           TEXT,
    app_status       TEXT,
    built_at         TEXT NOT NULL,
    PRIMARY KEY (order_id, application_id)
);
CREATE INDEX IF NOT EXISTS idx_tt_soc_open_id ON tiktok_sample_order_creator (creator_open_id);
CREATE INDEX IF NOT EXISTS idx_tt_soc_user_id ON tiktok_sample_order_creator (tiktok_user_id);

CREATE TABLE IF NOT EXISTS tiktok_creator_xwalk (
    creator_open_id         TEXT PRIMARY KEY,
    username                TEXT,     -- latest handle seen
    nickname                TEXT,     -- latest display name seen
    tiktok_user_id          TEXT,     -- orders.creator_id via the creator's sample orders (modal value)
    tiktok_user_id_count    INTEGER,  -- distinct user ids seen; >1 is a conflict to investigate
    handle_count            INTEGER,  -- distinct handles in tiktok_creator_handle_history
    sample_applications     INTEGER,
    sample_orders           INTEGER,
    first_sample_order_date TEXT,
    last_sample_order_date  TEXT,
    built_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tt_xwalk_user_id ON tiktok_creator_xwalk (tiktok_user_id);
CREATE INDEX IF NOT EXISTS idx_tt_xwalk_username ON tiktok_creator_xwalk (username);
"""


class ApiError(RuntimeError):
    """A real TikTok API error (bad parameter, scope, ...): fail the run."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class RateLimited(RuntimeError):
    """36009037 persisted through every backoff: pause the run (exit 75), do not fail it."""


# --------------------------------------------------------------------------- HTTP

def _call(path: str, query: dict, body: dict, pace: float = 0.0) -> dict:
    """One signed POST. Reuses tiktok_shop's signer and token refresh.

    Token expiry -> refresh once and re-sign. Transient upstream codes and
    connection drops -> backoff, re-sign (a fresh timestamp each pass).
    36009037 -> longer backoff; RateLimited if it never clears.
    Anything else nonzero -> ApiError, immediately."""
    if pace:
        time.sleep(pace)
    raw = json.dumps(body, separators=(",", ":"))
    refreshed = False
    transient = 0
    limited = 0
    while True:
        params = {"app_key": os.environ["TIKTOK_APP_KEY"], "timestamp": str(int(time.time())),
                  "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"]}
        params.update({k: str(v) for k, v in query.items()})
        params["sign"] = tts._sign(path, params, raw, os.environ["TIKTOK_APP_SECRET"])
        try:
            resp = requests.post(tts.BASE + path, params=params, data=raw, timeout=60,
                                 headers={"Content-Type": "application/json",
                                          "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]})
            payload = resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ValueError):
            if transient >= tts.MAX_TRANSIENT_RETRIES:
                raise
            transient += 1
            time.sleep(min(15 * transient, 60))
            continue
        code = payload.get("code")
        if code in tts.TOKEN_EXPIRED_CODES and not refreshed:
            tts._refresh_access_token()
            refreshed = True
            continue
        if code in tts.TRANSIENT_CODES and transient < tts.MAX_TRANSIENT_RETRIES:
            transient += 1
            time.sleep(min(15 * transient, 60))
            continue
        if code == RATE_LIMIT_CODE:
            if limited >= MAX_RATE_LIMIT_RETRIES:
                raise RateLimited(f"{path}: still rate limited after {limited} backoffs")
            limited += 1
            time.sleep(min(10 * limited, 60))
            continue
        if code not in (0, None):
            raise ApiError(f"TikTok API error {code} on {path}: {payload.get('message')}", code)
        return payload.get("data") or {}


# --------------------------------------------------------------------------- parsing

def _iso(unix_seconds) -> str | None:
    if not unix_seconds:
        return None
    return dt.datetime.fromtimestamp(int(unix_seconds), dt.timezone.utc).isoformat(timespec="seconds")


def _utc_date(unix_seconds) -> str | None:
    if not unix_seconds:
        return None
    return dt.datetime.fromtimestamp(int(unix_seconds), dt.timezone.utc).date().isoformat()


def _money(obj) -> float | None:
    """`{}` / missing amount -> None. TikTok sends `{}` until it has computed a
    figure, so it is a BLANK, not a measured zero."""
    if not isinstance(obj, dict):
        return None
    amt = obj.get("amount")
    if amt in (None, ""):
        return None
    return float(amt)


def _pct_from_hundredths(v) -> float | None:
    """'1550' -> 15.5 (the order API's unit is hundredths of a percent)."""
    if v in (None, "", "-"):
        return None
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _blank_none(v):
    return v if v not in (None, "", "0") else None


def order_line_rows(order: dict, synced_at: str) -> list[dict]:
    """One row per element of order['skus'], every column named."""
    oid = str(order.get("id") or "")
    rows = []
    for i, s in enumerate(order.get("skus") or []):
        price = s.get("price") or {}
        currency = (price.get("currency") or (s.get("estimated_commission_base") or {}).get("currency")
                    or (s.get("estimated_paid_shop_ads_commission") or {}).get("currency"))
        carousel = s.get("is_carousel")
        rows.append({
            "order_id": oid,
            "line_no": i,
            "order_create_time": _iso(order.get("create_time")),
            "order_date": _utc_date(order.get("create_time")),
            "delivery_time": _iso(order.get("delivery_time")),
            "order_status": order.get("status") or None,
            "sku_id": s.get("sku_id") or None,
            "product_id": s.get("product_id") or None,
            "quantity": _int(s.get("quantity")),
            "refunded_quantity": _int(s.get("refunded_quantity")),
            "returned_quantity": _int(s.get("returned_quantity")),
            "fully_return": s.get("fully_return") or None,
            "settlement_status": s.get("settlement_status") or None,
            "is_carousel": None if carousel is None else int(bool(carousel)),
            "content_type": s.get("content_type") or None,
            "content_id": s.get("content_id") or None,
            "creator_username": s.get("creator_username") or None,
            "price": _float(price.get("amount")),
            "currency": currency,
            "commission_model": s.get("commission_model") or None,
            "commission_rate_pct": _pct_from_hundredths(s.get("commission_rate")),
            "commission_tier_setting": s.get("commission_tier_setting") or None,
            "shop_ads_commission_rate_pct": _pct_from_hundredths(s.get("shop_ads_commission_rate")),
            "campaign_id": s.get("campaign_id") or None,
            "open_collaboration_id": s.get("open_collaboration_id") or None,
            "target_collaboration_id": s.get("target_collaboration_id") or None,
            "est_commission_base": _money(s.get("estimated_commission_base")),
            "est_paid_commission": _money(s.get("estimated_paid_commission")),
            "est_shop_ads_commission": _money(s.get("estimated_paid_shop_ads_commission")),
            "est_partner_commission": _money(s.get("estimated_paid_partner_commission")),
            "est_cofunded_bonus": _money(s.get("estimated_cofunded_creator_bonus_amount")),
            "act_commission_base": _money(s.get("actual_commission_base")),
            "act_paid_commission": _money(s.get("actual_paid_commission")),
            "act_shop_ads_commission": _money(s.get("actual_paid_shop_ads_commission")),
            "act_partner_commission": _money(s.get("actual_paid_partner_commission")),
            "act_cofunded_bonus": _money(s.get("actual_cofunded_creator_bonus_amount")),
            "raw_json": json.dumps(s, separators=(",", ":"), sort_keys=True),
            "synced_at": synced_at,
        })
    return rows


def application_row(app: dict, source: str, seen_at: str) -> dict:
    """Named columns for one sample application. Deliberately NOT kept: the
    signed avatar URL (expires) and the creator's rolling 30-day gmv /
    ec_video_view / content_count / fulfillment_percentage -- those are
    CREATOR-level snapshots repeated on every application; summing them per
    sample would count the creator's whole-shop figure once per sample."""
    c = app.get("creator") or {}
    p = app.get("product") or {}
    return {
        "application_id": str(app.get("id")),
        "order_id": _blank_none(app.get("order_id")),
        "status": app.get("status") or None,
        "fulfillment_status": app.get("fulfillment_status") or None,
        "product_id": p.get("id") or None,
        "sku_id": p.get("sku_id") or None,
        "product_title": p.get("title") or None,
        "sku_name": p.get("sku_name") or None,
        "creator_open_id": c.get("creator_open_id") or None,
        "creator_username": c.get("username") or None,
        "creator_nickname": c.get("nickname") or None,
        "follower_count": _int(c.get("follower_count")),
        "commission_rate_frac": _float(app.get("commission_rate")),
        "partner_name": app.get("partner_name") or None,
        "tracking_number": app.get("tracking_number") or None,
        "approve_expiration_time": _iso(app.get("approve_expiration_time")),
        "shipment_expiration_time": _iso(app.get("shipment_expiration_time")),
        "source": source,
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
    }


# --------------------------------------------------------------------------- storage

LINE_COLS = (
    "order_id", "line_no", "order_create_time", "order_date", "delivery_time", "order_status",
    "sku_id", "product_id", "quantity", "refunded_quantity", "returned_quantity", "fully_return",
    "settlement_status", "is_carousel", "content_type", "content_id", "creator_username", "price",
    "currency", "commission_model", "commission_rate_pct", "commission_tier_setting",
    "shop_ads_commission_rate_pct", "campaign_id", "open_collaboration_id", "target_collaboration_id",
    "est_commission_base", "est_paid_commission", "est_shop_ads_commission", "est_partner_commission",
    "est_cofunded_bonus", "act_commission_base", "act_paid_commission", "act_shop_ads_commission",
    "act_partner_commission", "act_cofunded_bonus", "raw_json", "synced_at",
)
APP_COLS = (
    "application_id", "order_id", "status", "fulfillment_status", "product_id", "sku_id",
    "product_title", "sku_name", "creator_open_id", "creator_username", "creator_nickname",
    "follower_count", "commission_rate_frac", "partner_name", "tracking_number",
    "approve_expiration_time", "shipment_expiration_time", "source", "first_seen_at", "last_seen_at",
)

_LINE_INSERT = (f"INSERT INTO tiktok_affiliate_order_lines ({', '.join(LINE_COLS)}) "
                f"VALUES ({', '.join(':' + c for c in LINE_COLS)})")
# first_seen_at survives an update; a lookup hit never downgrades a list source.
_APP_UPSERT = (
    f"INSERT INTO tiktok_affiliate_sample_applications ({', '.join(APP_COLS)}) "
    f"VALUES ({', '.join(':' + c for c in APP_COLS)}) "
    "ON CONFLICT(application_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in APP_COLS
                if c not in ("application_id", "first_seen_at", "source"))
    + ", source = CASE WHEN tiktok_affiliate_sample_applications.source LIKE 'list:%' "
      "AND excluded.source NOT LIKE 'list:%' THEN tiktok_affiliate_sample_applications.source "
      "ELSE excluded.source END"
)
_HANDLE_UPSERT = (
    "INSERT INTO tiktok_creator_handle_history "
    "(creator_open_id, username, nickname, first_seen_at, last_seen_at, sources) "
    "VALUES (:creator_open_id, :username, :nickname, :seen_at, :seen_at, :source) "
    "ON CONFLICT(creator_open_id, username) DO UPDATE SET "
    "nickname = COALESCE(excluded.nickname, tiktok_creator_handle_history.nickname), "
    "first_seen_at = MIN(tiktok_creator_handle_history.first_seen_at, excluded.first_seen_at), "
    "last_seen_at = MAX(tiktok_creator_handle_history.last_seen_at, excluded.last_seen_at), "
    "sources = CASE WHEN instr(',' || tiktok_creator_handle_history.sources || ',', "
    "',' || excluded.sources || ',') > 0 THEN tiktok_creator_handle_history.sources "
    "ELSE tiktok_creator_handle_history.sources || ',' || excluded.sources END"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def check_required_env() -> None:
    """Fail fast and legibly on a misconfigured .env instead of a KeyError
    deep inside a signed request."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


def connect(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or warehouse_db.DB_PATH), timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)
    return conn


def replace_order_lines(conn: sqlite3.Connection, orders: list[dict], synced_at: str) -> int:
    """Replace every line of each order in ONE transaction. Whole-order replace
    is what makes line_no a safe key AND what drops a stale handle after a
    creator renames (a PK containing the handle would leave both rows behind
    and double the GMV)."""
    rows = [r for o in orders for r in order_line_rows(o, synced_at)]
    with conn:
        conn.executemany("DELETE FROM tiktok_affiliate_order_lines WHERE order_id = ?",
                         [(str(o.get("id")),) for o in orders])
        conn.executemany(_LINE_INSERT, rows)
    return len(rows)


def upsert_applications(conn: sqlite3.Connection, apps: list[dict], source: str, seen_at: str) -> int:
    rows = [application_row(a, source, seen_at) for a in apps if a.get("id")]
    handles = [{"creator_open_id": r["creator_open_id"], "username": r["creator_username"],
                "nickname": r["creator_nickname"], "seen_at": seen_at, "source": "sample_applications"}
               for r in rows if r["creator_open_id"] and r["creator_username"]]
    with conn:
        conn.executemany(_APP_UPSERT, rows)
        conn.executemany(_HANDLE_UPSERT, handles)
    return len(rows)


def _record_order_window(conn, start: str, end: str, **kw) -> None:
    cols = ("window_start", "window_end", "total_count", "orders_returned", "lines_written",
            "pages", "status", "message", "asked_at")
    row = {"window_start": start, "window_end": end, "total_count": None, "orders_returned": None,
           "lines_written": None, "pages": None, "message": None, "asked_at": warehouse_db.now(), **kw}
    with conn:
        conn.execute(f"INSERT OR REPLACE INTO tiktok_affiliate_order_coverage ({', '.join(cols)}) "
                     f"VALUES ({', '.join(':' + c for c in cols)})", row)


def _record_sample_ask(conn, kind: str, key: str, **kw) -> None:
    """Upsert one coverage row; `attempts` counts every ask of the same key."""
    cols = ("kind", "key", "total_count", "rows_returned", "pages", "capped", "found",
            "status", "message", "asked_at")
    row = {"kind": kind, "key": key, "total_count": None, "rows_returned": None, "pages": None,
           "capped": None, "found": None, "message": None, "asked_at": warehouse_db.now(), **kw}
    with conn:
        conn.execute(
            f"INSERT INTO tiktok_affiliate_sample_coverage ({', '.join(cols)}, attempts) "
            f"VALUES ({', '.join(':' + c for c in cols)}, 1) "
            "ON CONFLICT(kind, key) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("kind", "key"))
            + ", attempts = tiktok_affiliate_sample_coverage.attempts + 1", row)


# --------------------------------------------------------------------------- orders

def _merge_order(by_id: dict[str, dict], order: dict) -> None:
    """page_size and total_count count SKU LINES, not orders (a 100-line page
    held 97 orders), so a multi-line order can be split across two pages. Union
    its lines instead of letting the second page overwrite the first; an
    identical line seen twice is kept once."""
    oid = str(order.get("id"))
    if oid not in by_id:
        by_id[oid] = {**order, "skus": list(order.get("skus") or [])}
        return
    have = by_id[oid]["skus"]
    seen = {json.dumps(s, sort_keys=True) for s in have}
    for s in order.get("skus") or []:
        if json.dumps(s, sort_keys=True) not in seen:
            have.append(s)


def page_orders(start_ts: int, end_ts: int, call=_call) -> tuple[list[dict], dict]:
    """Every order created in [start_ts, end_ts). Stops on an EMPTY page or a
    MISSING next token -- never on a short page. Orders are merged by id across
    pages (see _merge_order)."""
    body = {"create_time_ge": int(start_ts), "create_time_lt": int(end_ts)}
    by_id: dict[str, dict] = {}
    token, pages, total = "", 0, None
    while True:
        query = {"page_size": ORDERS_PAGE_SIZE}
        if token:
            query["page_token"] = token
        data = call(ORDERS_PATH, query, body, ORDERS_PACE_SECONDS)
        pages += 1
        if total is None:
            total = data.get("total_count")
        batch = data.get("orders") or []
        for o in batch:
            _merge_order(by_id, o)
        token = data.get("next_page_token") or ""
        if not batch or not token:
            break
        if pages >= MAX_PAGES:
            raise ApiError(f"orders window {start_ts}-{end_ts}: still paging after {MAX_PAGES} pages")
    return list(by_id.values()), {"pages": pages, "total_count": total}


def _probe_total(start_ts: int, end_ts: int, call=_call) -> int | None:
    data = call(ORDERS_PATH, {"page_size": 1}, {"create_time_ge": int(start_ts), "create_time_lt": int(end_ts)},
                ORDERS_PACE_SECONDS)
    return data.get("total_count")


def snap_to_grid(start: dt.datetime, end: dt.datetime,
                 days: int = ORDER_WINDOW_DAYS) -> tuple[dt.datetime, dt.datetime]:
    """Widen [start, end) to whole windows of ONE fixed grid anchored at
    GRID_ANCHOR (TIKTOK_SHOP_FLOOR). Without this a daily `--days 14` run plans windows ending at a
    different instant every day, so its coverage rows never coincide with the
    backfill's (skip logic cannot match them) and pile up one partial row per
    day. The grid's last window may end in the future; TikTok accepts that."""
    anchor = GRID_ANCHOR
    step = dt.timedelta(days=days)
    lo = anchor + ((start - anchor) // step) * step
    hi = anchor + (-((anchor - end) // step)) * step   # ceiling
    return lo, hi


def plan_windows(start: dt.datetime, end: dt.datetime, days: int = ORDER_WINDOW_DAYS) -> list[tuple[dt.datetime, dt.datetime]]:
    out, cur = [], start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def sync_order_window(conn, start: dt.datetime, end: dt.datetime, call=_call,
                      split_at: int | None = None) -> dict:
    """Pull one window (splitting it first if TikTok reports >= split_at lines,
    default SPLIT_AT), write its lines, record what was asked. Returns
    per-window stats."""
    split_at = SPLIT_AT if split_at is None else split_at
    s_ts, e_ts = int(start.timestamp()), int(end.timestamp())
    if end - start > dt.timedelta(hours=6):
        total = _probe_total(s_ts, e_ts, call)
        if total is not None and total >= split_at:
            mid = start + (end - start) / 2
            a = sync_order_window(conn, start, mid, call, split_at)
            b = sync_order_window(conn, mid, end, call, split_at)
            out = {k: a[k] + b[k] for k in ("orders", "lines", "short")}
            # Record the PARENT too: run_orders' skip check looks up the planned
            # window's own key, and only the halves would otherwise exist.
            _record_order_window(conn, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"),
                                 total_count=total, orders_returned=out["orders"], lines_written=out["lines"],
                                 status="short" if out["short"] else "complete",
                                 message=f"split in two at {total} lines")
            return out
    w0, w1 = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
    try:
        orders, meta = page_orders(s_ts, e_ts, call)
    except Exception as e:
        _record_order_window(conn, w0, w1, status="failed", message=str(e)[:500])
        raise
    lines = replace_order_lines(conn, orders, warehouse_db.now())
    total = meta["total_count"]   # counts SKU LINES, so compare it to lines, not orders
    short = total is not None and lines < total
    _record_order_window(conn, w0, w1, total_count=total, orders_returned=len(orders), lines_written=lines,
                         pages=meta["pages"], status="short" if short else "complete",
                         message=(f"returned {lines} of {total} lines" if short else None))
    return {"orders": len(orders), "lines": lines, "short": int(short)}


def completed_windows(conn) -> set[tuple[str, str]]:
    return {(a, b) for a, b in conn.execute(
        "SELECT window_start, window_end FROM tiktok_affiliate_order_coverage WHERE status = 'complete'")}


def run_orders(conn, start: dt.datetime, end: dt.datetime, refresh_after: dt.datetime | None,
               call=_call, window_days: int = ORDER_WINDOW_DAYS) -> dict:
    """Pull every window in [start, end). A window already recorded `complete`
    is skipped UNLESS it ends after `refresh_after` -- affiliate lines keep
    changing for weeks (settlement, returns, actual commission), so the
    trailing window is always re-pulled."""
    done = completed_windows(conn)
    stats = Counter()
    for w0, w1 in plan_windows(start, end, window_days):
        key = (w0.isoformat(timespec="seconds"), w1.isoformat(timespec="seconds"))
        if key in done and (refresh_after is None or w1 <= refresh_after):
            stats["skipped"] += 1
            continue
        r = sync_order_window(conn, w0, w1, call)
        stats.update(r)
        stats["windows"] += 1
        print(f"  {key[0][:10]} -> {key[1][:10]}: orders={r['orders']} lines={r['lines']}"
              + ("  SHORT" if r["short"] else ""), flush=True)
    return dict(stats)


# --------------------------------------------------------------------------- samples

def list_partition(conn, kind: str, key: str, body: dict, source: str, call=_call) -> dict:
    """Page one filtered slice of sample applications to its end and upsert
    every application. The endpoint pages by OFFSET and refuses offset 10,000
    (98001004): that is the ceiling of what the list can reach, recorded as
    capped -- not an error, and not "complete"."""
    seen_at = warehouse_db.now()
    token, pages, rows, total, hit_ceiling = "", 0, 0, None, False
    try:
        while True:
            query = {"page_size": SAMPLES_PAGE_SIZE}
            if token:
                query["page_token"] = token
            try:
                data = call(SAMPLES_PATH, query, body, SAMPLES_PACE_SECONDS)
            except ApiError as e:
                if e.code == LIST_DEPTH_CODE:
                    hit_ceiling = True
                    break
                raise
            pages += 1
            if total is None:
                total = data.get("total_count")
            apps = data.get("sample_applications") or []
            rows += upsert_applications(conn, apps, source, seen_at)
            token = data.get("next_page_token") or ""
            if not apps or not token:
                break
            if pages >= MAX_PAGES:
                raise ApiError(f"samples {key}: still paging after {MAX_PAGES} pages")
    except Exception as e:
        _record_sample_ask(conn, kind, key, total_count=total, rows_returned=rows, pages=pages,
                           status="failed", message=str(e)[:500])
        raise
    capped = int(hit_ceiling or (total is not None and total >= SAMPLE_LIST_CAP))
    short = capped or (total is not None and rows < total)
    _record_sample_ask(conn, kind, key, total_count=total, rows_returned=rows, pages=pages,
                       capped=capped, status="short" if short else "complete",
                       message=(f"returned {rows} of {total}" + (" (10k list ceiling)" if capped else "")
                                if short else None))
    return {"key": key, "total": total, "rows": rows, "pages": pages, "capped": capped}


def list_status(conn, status: str, call=_call) -> dict:
    """One status partition. Note the API's COMPLETED filter also returns
    OPS_COMPLETED rows."""
    return list_partition(conn, "status", status, {"status": status}, f"list:{status}", call)


def products_to_partition(conn, since: str, until: str | None = None) -> list[str]:
    """TikTok product ids behind sample orders that still have no application,
    and whose product x COMPLETED slice has not been fetched yet. COMPLETED is
    the only status that both carries orders AND hits the 10k list ceiling, so
    slicing it per product reaches the history the plain list cannot; one call
    usually resolves every sample of that product at once. orders.sku is the
    seller SKU -> catalog_tiktok.seller_sku (a SKU can sit on several listings,
    so every matching product id is asked).

    catalog_tiktok is OPTIONAL: without it (or without its seller_sku /
    product_id columns) this returns [] and --by-product has nothing to slice
    unless --product-ids is passed. A large catalog usually has no index on
    seller_sku, and a plain JOIN would scan it once per sample line (minutes
    on a catalog of a few hundred thousand rows). So: collect the unresolved
    SKUs first, then resolve them in ONE pass over the catalog against a TEMP
    set -- rather than adding an index to another connector's table."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(catalog_tiktok)")}
    if not {"seller_sku", "product_id"} <= cols:
        return []
    latest = dict(conn.execute("""
        SELECT o.sku, MAX(o.order_date)
        FROM orders o
        WHERE o.platform = 'tiktok' AND o.is_sample = 1 AND o.order_date >= ?
          AND (? IS NULL OR o.order_date < ?) AND o.sku IS NOT NULL AND o.sku <> ''
          AND NOT EXISTS (SELECT 1 FROM tiktok_affiliate_sample_applications a WHERE a.order_id = o.order_id)
        GROUP BY o.sku""", (since, until, until)).fetchall())
    if not latest:
        return []
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _tas_unresolved_skus (sku TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _tas_unresolved_skus")
    conn.executemany("INSERT OR IGNORE INTO _tas_unresolved_skus (sku) VALUES (?)", [(s,) for s in latest])
    asked = {k.split("|")[0] for (k,) in conn.execute(
        "SELECT key FROM tiktok_affiliate_sample_coverage WHERE kind = 'product'")}
    newest: dict[str, str] = {}
    for sku, pid in conn.execute("SELECT seller_sku, product_id FROM catalog_tiktok "
                                 "WHERE seller_sku IN (SELECT sku FROM _tas_unresolved_skus)"):
        if pid in asked:
            continue
        if latest[sku] > newest.get(pid, ""):
            newest[pid] = latest[sku]
    conn.execute("DELETE FROM _tas_unresolved_skus")
    return [p for p, _ in sorted(newest.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)]


def list_product_completed(conn, product_id: str, call=_call) -> dict:
    return list_partition(conn, "product", f"{product_id}|COMPLETED",
                          {"product_id": product_id, "status": "COMPLETED"}, "list:product", call)


def orders_to_look_up(conn, since: str, until: str | None = None, now: dt.datetime | None = None) -> list[str]:
    """Sample orders in `orders` with no application stored for them that are
    due an ask: never asked, or asked-and-missing fewer than
    MAX_LOOKUP_ATTEMPTS times and not within LOOKUP_RETRY_DAYS. Newest first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=LOOKUP_RETRY_DAYS)).isoformat(timespec="seconds")
    q = """
        SELECT o.order_id, MIN(o.order_date) d
        FROM orders o
        WHERE o.platform = 'tiktok' AND o.is_sample = 1 AND o.order_date >= ?
          AND (? IS NULL OR o.order_date < ?)
          AND NOT EXISTS (SELECT 1 FROM tiktok_affiliate_sample_applications a WHERE a.order_id = o.order_id)
          AND NOT EXISTS (SELECT 1 FROM tiktok_affiliate_sample_coverage c
                          WHERE c.kind = 'order' AND c.key = o.order_id
                            AND (c.found = 1 OR c.attempts >= ? OR c.asked_at > ?))
        GROUP BY o.order_id
        ORDER BY d DESC, o.order_id DESC
    """
    return [r[0] for r in conn.execute(q, (since, until, until, MAX_LOOKUP_ATTEMPTS, cutoff))]


def lookup_order(conn, order_id: str, call=_call) -> int:
    """Ask TikTok which application produced one sample order; record the ask."""
    try:
        data = call(SAMPLES_PATH, {"page_size": SAMPLES_PAGE_SIZE}, {"order_id": order_id}, SAMPLES_PACE_SECONDS)
    except ApiError as e:
        _record_sample_ask(conn, "order", order_id, found=0, status="failed", message=str(e)[:500])
        raise
    apps = data.get("sample_applications") or []
    n = upsert_applications(conn, apps, "order_lookup", warehouse_db.now())
    _record_sample_ask(conn, "order", order_id, rows_returned=n, pages=1, found=int(n > 0),
                       status="complete")
    return n


# --------------------------------------------------------------------------- crosswalk

def ingest_marketplace_handles(conn) -> int:
    """Fold tiktok_creator_marketplace's exact-handle resolutions into the
    handle history (read-only use of that table)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='tiktok_creator_marketplace'").fetchone():
        return 0
    rows = [{"creator_open_id": oid, "username": h, "nickname": None, "seen_at": ts or warehouse_db.now(),
             "source": "marketplace"}
            for h, oid, ts in conn.execute(
                "SELECT creator_handle, creator_open_id, fetched_at FROM tiktok_creator_marketplace "
                "WHERE found = 1 AND creator_open_id IS NOT NULL AND creator_handle IS NOT NULL")]
    with conn:
        conn.executemany(_HANDLE_UPSERT, rows)
    return len(rows)


def build_crosswalk(conn) -> dict:
    """Rebuild tiktok_sample_order_creator and tiktok_creator_xwalk from the raw
    tables in ONE transaction. Deterministic: run it as often as you like."""
    built = warehouse_db.now()
    ingest_marketplace_handles(conn)
    soc = conn.execute("""
        SELECT a.order_id, a.application_id, a.creator_open_id, a.creator_username,
               (SELECT MAX(o.creator_id) FROM orders o
                 WHERE o.platform = 'tiktok' AND o.order_id = a.order_id) AS user_id,
               (SELECT MIN(o.order_date) FROM orders o
                 WHERE o.platform = 'tiktok' AND o.order_id = a.order_id) AS order_date,
               a.product_id, a.sku_id, a.status
        FROM tiktok_affiliate_sample_applications a
        WHERE a.order_id IS NOT NULL
    """).fetchall()

    per = defaultdict(lambda: {"apps": 0, "orders": set(), "dates": [], "uids": Counter()})
    for oid, app_id, open_id, _u, uid, odate, *_ in soc:
        if not open_id:
            continue
        p = per[open_id]
        p["orders"].add(oid)
        if odate:
            p["dates"].append(odate)
        if uid:
            p["uids"][uid] += 1
    for open_id, n in conn.execute("SELECT creator_open_id, COUNT(*) FROM tiktok_affiliate_sample_applications "
                                   "WHERE creator_open_id IS NOT NULL GROUP BY 1"):
        per[open_id]["apps"] = n

    latest = {}
    for open_id, username, nickname, _seen in conn.execute(
            "SELECT creator_open_id, creator_username, creator_nickname, last_seen_at "
            "FROM tiktok_affiliate_sample_applications WHERE creator_open_id IS NOT NULL "
            "ORDER BY last_seen_at, application_id"):
        latest[open_id] = (username, nickname)   # last write wins = most recently seen
    handles = dict(conn.execute("SELECT creator_open_id, COUNT(DISTINCT username) "
                                "FROM tiktok_creator_handle_history GROUP BY 1"))

    xw = []
    for open_id, p in per.items():
        uid, _ = p["uids"].most_common(1)[0] if p["uids"] else (None, 0)
        username, nickname = latest.get(open_id, (None, None))
        xw.append((open_id, username, nickname, uid, len(p["uids"]), handles.get(open_id, 0),
                   p["apps"], len(p["orders"]), min(p["dates"]) if p["dates"] else None,
                   max(p["dates"]) if p["dates"] else None, built))
    with conn:
        conn.execute("DELETE FROM tiktok_sample_order_creator")
        conn.executemany(
            "INSERT INTO tiktok_sample_order_creator (order_id, application_id, creator_open_id, "
            "creator_username, tiktok_user_id, order_date, product_id, sku_id, app_status, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", [(*r, built) for r in soc])
        conn.execute("DELETE FROM tiktok_creator_xwalk")
        conn.executemany(
            "INSERT INTO tiktok_creator_xwalk (creator_open_id, username, nickname, tiktok_user_id, "
            "tiktok_user_id_count, handle_count, sample_applications, sample_orders, "
            "first_sample_order_date, last_sample_order_date, built_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", xw)
    return {"sample_orders": len(soc), "creators": len(xw),
            "user_id_conflicts": sum(1 for r in xw if r[4] > 1)}


# --------------------------------------------------------------------------- measurement

def coverage_report(conn, split: str) -> dict:
    """How much of the sample / affiliate data the crosswalk resolves. Read-only.

    sample_line_open_id_share_*: share of sample-order lines in `orders` whose
    order has an application with a creator_open_id, split at `split` (pick
    the date your order data started carrying creator ids, or any cut you
    want to compare). affiliate_gmv_*: affiliate line GMV, and the share that
    has a handle at all / a handle the history knows. user_id_eq_open_id
    should always be 0 -- a nonzero value means the two id spaces were mixed."""
    out = {}
    for label, cond in (("since_" + split, "o.order_date >= :s"), ("before_" + split, "o.order_date < :s"),
                        ("all", "1=1")):
        r = conn.execute(f"""
            SELECT COUNT(*),
                   SUM(EXISTS (SELECT 1 FROM tiktok_affiliate_sample_applications a
                               WHERE a.order_id = o.order_id AND a.creator_open_id IS NOT NULL)),
                   COUNT(DISTINCT o.order_id)
            FROM orders o WHERE o.platform = 'tiktok' AND o.is_sample = 1 AND {cond}""",
                         {"s": split}).fetchone()
        lines, resolved, orders = r[0] or 0, r[1] or 0, r[2] or 0
        out[f"sample_lines_{label}"] = lines
        out[f"sample_orders_{label}"] = orders
        out[f"sample_lines_resolved_{label}"] = resolved
        out[f"sample_line_open_id_share_{label}"] = round(resolved / lines, 4) if lines else None
    r = conn.execute("""SELECT COUNT(*), SUM(tiktok_user_id = creator_open_id), SUM(tiktok_user_id IS NOT NULL)
                        FROM tiktok_sample_order_creator""").fetchone()
    out["xwalk_sample_orders"] = r[0]
    out["user_id_eq_open_id"] = r[1]
    out["user_id_present"] = r[2]
    out["open_ids_per_user_id_gt1"] = conn.execute(
        "SELECT COUNT(*) FROM (SELECT tiktok_user_id FROM tiktok_sample_order_creator "
        "WHERE tiktok_user_id IS NOT NULL GROUP BY 1 HAVING COUNT(DISTINCT creator_open_id) > 1)").fetchone()[0]
    out["user_ids_per_open_id_gt1"] = conn.execute(
        "SELECT COUNT(*) FROM tiktok_creator_xwalk WHERE tiktok_user_id_count > 1").fetchone()[0]
    for basis in ("est_commission_base", "price * quantity"):
        r = conn.execute(f"""
            SELECT SUM({basis}), SUM(CASE WHEN creator_username IS NOT NULL THEN {basis} END),
                   SUM(CASE WHEN creator_username IN (SELECT username FROM tiktok_creator_handle_history)
                            THEN {basis} END)
            FROM tiktok_affiliate_order_lines""").fetchone()
        tag = "base" if basis.startswith("est") else "price_x_qty"
        total = r[0] or 0
        out[f"affiliate_gmv_{tag}"] = round(total, 2)
        out[f"affiliate_gmv_with_handle_share_{tag}"] = round((r[1] or 0) / total, 4) if total else None
        out[f"affiliate_gmv_handle_resolves_share_{tag}"] = round((r[2] or 0) / total, 4) if total else None
    return out


# --------------------------------------------------------------------------- CLI

def _parse_date(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)


def lookup_misses_are_a_problem(missing: int, asked: int) -> bool:
    return missing > max(LOOKUP_MISS_TOLERANCE, LOOKUP_MISS_SHARE * asked)


def _status_for(rows: int, problems: int) -> str:
    """A pull that wrote nothing is `degraded`, never `ok`."""
    if problems or rows == 0:
        return "degraded"
    return "ok"


def cmd_orders(conn, a) -> int:
    started = warehouse_db.now()
    today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = _parse_date(a.end) + dt.timedelta(days=1) if a.end else today + dt.timedelta(days=1)
    start = _parse_date(a.start) if a.start else end - dt.timedelta(days=a.days)
    start, end = snap_to_grid(start, end)
    refresh_after = today - dt.timedelta(days=a.refresh_days)
    if a.force:
        refresh_after = start
    print(f"orders {start.date()} -> {end.date()} (re-pull windows ending after {refresh_after.date()})")
    try:
        stats = run_orders(conn, start, end, refresh_after)
    except RateLimited as e:
        warehouse_db.log_sync(PLATFORM_ORDERS, started, 0, "degraded", f"paused: {e}")
        print("PAUSED (rate limited); re-run resumes from the coverage table")
        return EXIT_PAUSED
    except Exception as e:  # noqa: BLE001
        warehouse_db.log_sync(PLATFORM_ORDERS, started, 0, "error", str(e))
        raise
    msg = (f"{start.date()}->{end.date()} windows={stats.get('windows', 0)} skipped={stats.get('skipped', 0)} "
           f"orders={stats.get('orders', 0)} lines={stats.get('lines', 0)} short={stats.get('short', 0)}")
    rows = stats.get("lines", 0)
    status = _status_for(rows, stats.get("short", 0))
    if rows == 0 and stats.get("windows", 0) == 0:
        msg += " (nothing due: every window already complete)"
    warehouse_db.log_sync(PLATFORM_ORDERS, started, rows, status, msg)
    print(f"{status}: {msg}")
    return 0


def cmd_samples(conn, a) -> int:
    started = warehouse_db.now()
    rows = problems = 0
    parts = []
    try:
        if a.list:
            statuses = ORDER_STATUSES + (NO_ORDER_STATUSES if a.all_statuses else ())
            for st in statuses:
                r = list_status(conn, st)
                rows += r["rows"]
                parts.append(f"{st}={r['rows']}/{r['total']}" + ("(capped)" if r["capped"] else ""))
                print(f"  list {st}: {r['rows']} of {r['total']}" + ("  CAPPED" if r["capped"] else ""), flush=True)
        if a.by_product:
            prods = ([p.strip() for p in a.product_ids.split(",") if p.strip()]
                     if a.product_ids else products_to_partition(conn, a.since, a.until))
            if a.max_products:
                prods = prods[:a.max_products]
            print(f"  product x COMPLETED slices due: {len(prods)}", flush=True)
            got = capped = 0
            for i, pid in enumerate(prods, 1):
                r = list_product_completed(conn, pid)
                got += r["rows"]
                capped += r["capped"]
                if i % 100 == 0:
                    print(f"    {i}/{len(prods)} applications={got}", flush=True)
            rows += got
            problems += capped
            parts.append(f"products={len(prods)} applications={got}" + (f" capped={capped}" if capped else ""))
        if a.lookup:
            todo = orders_to_look_up(conn, a.since, a.until)
            if a.max_lookups:
                todo = todo[:a.max_lookups]
            print(f"  lookups due: {len(todo)}", flush=True)
            found = 0
            for i, oid in enumerate(todo, 1):
                n = lookup_order(conn, oid)
                found += int(n > 0)
                rows += n
                if i % 100 == 0:
                    print(f"    {i}/{len(todo)} found={found}", flush=True)
            missing = len(todo) - found
            parts.append(f"lookups={len(todo)} found={found} missing={missing}")
            problems += int(lookup_misses_are_a_problem(missing, len(todo)))
    except RateLimited as e:
        warehouse_db.log_sync(PLATFORM_SAMPLES, started, rows, "degraded", f"paused: {e}; " + " ".join(parts))
        print("PAUSED (rate limited); re-run resumes from the coverage table")
        return EXIT_PAUSED
    except Exception as e:  # noqa: BLE001
        warehouse_db.log_sync(PLATFORM_SAMPLES, started, rows, "error", f"{e}; " + " ".join(parts))
        raise
    xw = build_crosswalk(conn)
    msg = " ".join(parts) + f" | xwalk creators={xw['creators']} sample_orders={xw['sample_orders']}"
    status = _status_for(rows, problems)
    warehouse_db.log_sync(PLATFORM_SAMPLES, started, rows, status, msg)
    print(f"{status}: {msg}")
    return 0


def cmd_xwalk(conn, a) -> int:
    started = warehouse_db.now()
    xw = build_crosswalk(conn)
    status = _status_for(xw["creators"], 0)
    msg = f"creators={xw['creators']} sample_orders={xw['sample_orders']} user_id_conflicts={xw['user_id_conflicts']}"
    warehouse_db.log_sync(PLATFORM_XWALK, started, xw["creators"], status, msg)
    print(f"{status}: {msg}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("orders")
    o.add_argument("--days", type=int, default=30)
    o.add_argument("--start")
    o.add_argument("--end")
    o.add_argument("--refresh-days", type=int, default=60,
                   help="always re-pull windows ending within this many days (lines mutate until settled)")
    o.add_argument("--force", action="store_true", help="re-pull every window in range")
    s = sub.add_parser("samples")
    s.add_argument("--list", action="store_true")
    s.add_argument("--all-statuses", action="store_true")
    s.add_argument("--by-product", action="store_true",
                   help="product x COMPLETED slices for products of unresolved sample orders")
    s.add_argument("--lookup", action="store_true", help="per-order_id lookups for what is still unresolved")
    s.add_argument("--since", help="sample-order floor for --by-product/--lookup (default: 30 days ago; "
                                   f"a backfill passes {SHOP_FLOOR} -- hours at ~25 calls/min)")
    s.add_argument("--until")
    s.add_argument("--product-ids", help="comma list of TikTok product ids to slice with --by-product "
                                         "(instead of resolving them from catalog_tiktok)")
    s.add_argument("--max-products", type=int, default=0)
    s.add_argument("--max-lookups", type=int, default=0)
    sub.add_parser("xwalk")
    r = sub.add_parser("report")
    r.add_argument("--split", help="date to split the coverage shares at (default: 90 days ago)")
    a = ap.parse_args(argv)
    if a.cmd == "samples" and not (a.list or a.by_product or a.lookup):
        ap.error("samples needs --list, --by-product and/or --lookup")
    if a.cmd == "samples" and not a.since:
        a.since = (dt.date.today() - dt.timedelta(days=30)).isoformat()

    if a.cmd in ("orders", "samples"):
        check_required_env()
    warehouse_db.init_db()
    conn = connect()
    if a.cmd == "orders":
        return cmd_orders(conn, a)
    if a.cmd == "samples":
        return cmd_samples(conn, a)
    if a.cmd == "xwalk":
        return cmd_xwalk(conn, a)
    split = a.split or (dt.date.today() - dt.timedelta(days=90)).isoformat()
    print(json.dumps(coverage_report(conn, split), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
