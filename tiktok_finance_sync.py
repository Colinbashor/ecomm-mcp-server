r"""
TikTok Shop settlement / fee feed -> `tiktok_settlements` + `tiktok_settlement_components`.

WHY THIS EXISTS AS A CONNECTOR rather than an API call inside a report. TikTok
Shop's marketplace fee is not one flat commission rate -- it's a mix of
referral/platform commission, affiliate creator commission, affiliate ad
commission, return-shipping and refund-admin lines, etc., and that mix shifts
with campaign strategy over time. Two shapes are tempting and both are wrong:

  * hardcoding a guessed flat commission rate, which silently goes stale as
    the mix shifts, and
  * calling the Finance API live inside a scheduled report, which puts a
    third-party outage in the report's critical path and re-fetches the same
    window on every single run.

Landing it in the warehouse fixes both: reports read the DB like every other
source, fee history accrues so the rate is comparable over time, and the
component split is resolved ONCE at ingest instead of per consumer.

THE DATA
  GET /finance/202309/statements                              statement-grain settlement records
  GET /finance/202309/statements/{id}/statement_transactions   per-order fee decomposition

  ILLUSTRATIVE EXAMPLE (not real data -- shape only): a shop might see an
  all-in take rate in the mid-to-high teens percent of revenue, split across
  referral/platform commission, affiliate creator commission, affiliate ads
  commission, plus smaller shipping/refund-admin lines and a residual that
  doesn't decompose cleanly (often payment processing, which this API
  doesn't itemize). Your own split will depend entirely on how much of your
  volume runs through affiliates vs. direct listings.

  SALES TAX IS A PASS-THROUGH and a seller-funded discount is the merchant's
  own markdown -- neither is a TikTok fee. Both are stored (they're real
  settlement lines, useful for reconciliation) but are tagged `is_fee=0` so a
  consumer summing fees can't accidentally include them and overstate the
  take rate by tens of points.

GOTCHA THAT COST A WRONG CONCLUSION DURING THIS CONNECTOR'S BUILD, WORTH
KNOWING BEFORE YOU DEBUG THE SAME THING: `/statements` 400s with "SortField
is a required field" if `sort_field` is omitted -- which reads exactly like
a permissions/scope error. An early probe concluded "no Finance API scope"
on that basis and was WRONG; the scope was present all along. (A genuine 403
"no schema found" scope gap does exist on the sibling
`/finance/202309/orders/settlements` and `/finance/202401/transactions`
paths, so the two failure modes need to be told apart by status code +
message, not assumed to mean the same thing.)

AUTH SETUP
Reuses the TikTok Shop app credentials and access/refresh-token machinery
from `warehouse/connectors/tiktok_shop.py` (see that file's docstring for the
full OAuth setup). No new credentials are needed -- if TikTok orders sync
already works, this does too.

NET SALES VIEW: `tiktok_weekly_product`
  `orders.total` for TikTok is the shopper-paid price, which nets out BOTH a
  seller-funded discount (your own markdown) and a platform-funded discount (a
  voucher TikTok funds and reimburses to you at settlement). Summing `total`
  as "net sales" therefore understates what you actually sold by the
  platform-funded piece. This connector rebuilds a view, on every run, that
  corrects it per order line, using the best source available:

    1. `orders.platform_discount` -- captured per line from the order API by
       warehouse/connectors/tiktok_shop.py. Exact, and available the day the
       order is placed. Wins whenever it is non-NULL (a measured 0 counts).
    2. Settlement pro rata -- `tiktok_settlement_orders.platform_discount_amount`
       is ORDER-level (no sku column), so it is SUMmed per order_id (an order
       can appear on several statements; MAX would undercount) and allocated
       across the order's lines by each line's share of the order total. Exact
       for single-sku orders, proportional otherwise. Used only for rows synced
       before the line-level column existed.
    3. Raw `total`, unmodified. An order with neither source has NOT been
       measured -- it is never treated as "zero platform discount".

  Columns: week_start (Monday; Mon-Sun weeks), sku, product_name, units,
  gross_sales, discounts (seller-funded only), net_sales, sample_units,
  sample_orders, platform_discount, shopper_paid (= raw total), and
  pd_known_sales (the part of net_sales whose platform discount was actually
  measured -- compare it to net_sales to see how much of a week is still on
  the raw-total fallback). Sample orders are excluded from the sales columns
  and counted separately. It is a live VIEW, not a snapshot: it self-corrects
  as more orders settle or history is re-pulled, with no extra step.

USAGE
  python tiktok_finance_sync.py                 # incremental, last 30 days
  python tiktok_finance_sync.py --days 180       # wider window
  python tiktok_finance_sync.py --backfill       # 365-day window
  python tiktok_finance_sync.py --no-components  # statements only (fast, no fee split)
  python tiktok_finance_sync.py --no-orders      # skip the per-order fee detail table
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors.tiktok_shop import TOKEN_EXPIRED_CODES, _refresh_access_token

load_dotenv()

BASE = "https://open-api.tiktokglobalshop.com"
STATEMENTS_PATH = "/finance/202309/statements"
PLATFORM = "tiktok_finance"

REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

MAX_STATEMENT_PAGES = 120   # runaway guard on the statements list
MAX_TRANSACTION_PAGES = 20  # runaway guard per statement's transaction breakdown

# Components that genuinely reduce your take. Anything else captured from the
# transaction payload is stored with is_fee=0 (pass-throughs, your own
# discounts, customer payments) so a naive SUM over the table can't inflate
# the fee rate.
FEE_FIELDS = {
    "referral_fee_amount": "Referral / platform commission",
    "affiliate_commission_amount": "Affiliate creator commission",
    "affiliate_ads_commission_amount": "Affiliate ads commission",
    "affiliate_partner_commission_amount": "Affiliate partner commission",
    "actual_return_shipping_fee_amount": "Return shipping",
    "refund_administration_fee_amount": "Refund administration",
    "shipping_fee_amount": "Other shipping fees",
    "retail_delivery_fee_amount": "Retail delivery fee",
    "fbm_shipping_cost_amount": "Fulfilled-by-merchant shipping cost",
    "fbt_fulfillment_fee_amount": "Fulfilled-by-TikTok fulfilment fee",
}
# Present on the transaction payload but NOT a fee -- kept for reconciliation.
CONTEXT_FIELDS = {
    "sales_tax_amount": "Sales tax (pass-through, NOT a fee)",
    "seller_discount_amount": "Seller discount (your own markdown, NOT a fee)",
    "platform_discount_amount": "Platform-funded discount",
}
# Column order for tiktok_settlement_orders, after the key/context columns.
ORDER_FIELDS = [
    "revenue_amount",
    "net_sales_amount",
    "gross_sales_amount",
    "customer_refund_amount",
    "fee_amount",
    "referral_fee_amount",
    "affiliate_commission_amount",
    "affiliate_ads_commission_amount",
    "affiliate_partner_commission_amount",
    "actual_return_shipping_fee_amount",
    "refund_administration_fee_amount",
    "shipping_fee_amount",
    "retail_delivery_fee_amount",
    "fbm_shipping_cost_amount",
    "fbt_fulfillment_fee_amount",
    "sales_tax_amount",
    "seller_discount_amount",
    "platform_discount_amount",
]

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_settlements (
    id                   TEXT PRIMARY KEY,
    statement_time       TEXT,
    payment_time         TEXT,
    payment_status       TEXT,
    currency             TEXT,
    revenue_amount       REAL,
    net_sales_amount     REAL,
    fee_amount           REAL,
    shipping_cost_amount REAL,
    adjustment_amount    REAL,
    settlement_amount    REAL,
    synced_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tiktok_settlements_time
    ON tiktok_settlements(statement_time);

CREATE TABLE IF NOT EXISTS tiktok_settlement_components (
    statement_id TEXT NOT NULL,
    field        TEXT NOT NULL,
    label        TEXT,
    is_fee       INTEGER NOT NULL DEFAULT 0,
    amount       REAL,
    synced_at    TEXT NOT NULL,
    PRIMARY KEY (statement_id, field)
);

-- Per-order fee decomposition, from /statements/{id}/statement_transactions.
-- Retained (not just aggregated into components) because order_id joins to
-- your own orders table -> sku -> product identity, which is what makes
-- true per-product fee/margin possible rather than only an account-wide rate.
CREATE TABLE IF NOT EXISTS tiktok_settlement_orders (
    statement_id           TEXT NOT NULL,
    order_id                TEXT NOT NULL,
    order_create_time       TEXT,
    currency                TEXT,
    revenue_amount          REAL,
    net_sales_amount        REAL,
    gross_sales_amount      REAL,
    customer_refund_amount  REAL,
    fee_amount              REAL,  -- total fees on this order (negative = cost to you)
    referral_fee_amount     REAL,
    affiliate_commission_amount REAL,
    affiliate_ads_commission_amount REAL,
    affiliate_partner_commission_amount REAL,
    actual_return_shipping_fee_amount REAL,
    refund_administration_fee_amount REAL,
    shipping_fee_amount     REAL,
    retail_delivery_fee_amount REAL,
    fbm_shipping_cost_amount REAL,
    fbt_fulfillment_fee_amount REAL,
    sales_tax_amount        REAL,  -- pass-through, not a fee
    seller_discount_amount  REAL,  -- your own markdown, not a fee
    platform_discount_amount REAL,
    synced_at               TEXT NOT NULL,
    PRIMARY KEY (statement_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_tso_order ON tiktok_settlement_orders(order_id);
CREATE INDEX IF NOT EXISTS idx_tso_time  ON tiktok_settlement_orders(order_create_time);
"""

# Weekly per-sku TikTok sales with the platform-funded discount added back.
# See "NET SALES VIEW" in the module docstring for the precedence rules.
# Sign conventions differ by source and both are handled here: the settlement
# feed reports platform_discount_amount as NEGATIVE (a deduction), the order
# API's line-level platform_discount as POSITIVE. Dropped and recreated on
# every run so a fresh database or a definition change never leaves a stale
# view behind. Downstream readers should select columns by NAME -- new
# columns are appended at the end.
VIEW_DDL = """
DROP VIEW IF EXISTS tiktok_weekly_product;
CREATE VIEW tiktok_weekly_product AS
WITH settle AS (
    SELECT order_id, SUM(platform_discount_amount) AS platform_discount_amount
    FROM tiktok_settlement_orders
    GROUP BY order_id
),
order_totals AS (
    SELECT order_id, SUM(CASE WHEN is_sample = 1 THEN 0 ELSE total END) AS order_total
    FROM orders
    WHERE platform = 'tiktok' AND status != 'CANCELLED'
    GROUP BY order_id
),
lines AS (
    SELECT o.*,
           CASE
               WHEN o.is_sample = 1 THEN o.total
               WHEN o.platform_discount IS NOT NULL THEN o.total + o.platform_discount
               WHEN s.platform_discount_amount IS NULL THEN o.total
               WHEN ot.order_total IS NULL OR ot.order_total = 0 THEN o.total
               ELSE o.total - s.platform_discount_amount * (o.total / ot.order_total)
           END AS net_total,
           CASE
               WHEN o.platform_discount IS NOT NULL THEN 1
               WHEN s.platform_discount_amount IS NOT NULL
                    AND ot.order_total IS NOT NULL AND ot.order_total <> 0 THEN 1
               ELSE 0
           END AS pd_known
    FROM orders o
    LEFT JOIN settle s ON s.order_id = o.order_id
    LEFT JOIN order_totals ot ON ot.order_id = o.order_id
    WHERE o.platform = 'tiktok' AND o.status != 'CANCELLED'
)
SELECT date(order_date, 'weekday 0', '-6 days') AS week_start,
       sku,
       MAX(product_name) AS product_name,
       SUM(CASE WHEN is_sample = 1 THEN 0 ELSE quantity END) AS units,
       ROUND(SUM(CASE WHEN is_sample = 1 THEN 0 ELSE original_total END), 2) AS gross_sales,
       ROUND(SUM(CASE WHEN is_sample = 1 THEN 0 ELSE original_total - net_total END), 2) AS discounts,
       ROUND(SUM(CASE WHEN is_sample = 1 THEN 0 ELSE net_total END), 2) AS net_sales,
       SUM(CASE WHEN is_sample = 1 THEN quantity ELSE 0 END) AS sample_units,
       COUNT(DISTINCT CASE WHEN is_sample = 1 THEN order_id END) AS sample_orders,
       ROUND(SUM(CASE WHEN is_sample = 1 THEN 0 ELSE net_total - total END), 2) AS platform_discount,
       ROUND(SUM(CASE WHEN is_sample = 1 THEN 0 ELSE total END), 2) AS shopper_paid,
       ROUND(SUM(CASE WHEN is_sample = 1 OR pd_known = 0 THEN 0 ELSE net_total END), 2) AS pd_known_sales
FROM lines
GROUP BY week_start, sku;
"""


def ensure_schema(conn) -> None:
    conn.executescript(DDL)


def ensure_view(conn) -> None:
    """(Re)build tiktok_weekly_product. Kept separate from ensure_schema()
    because it reads the shared `orders` table (created by db.init_db()), not
    only this connector's own tables. SQLite resolves a view's tables lazily,
    so creating it never fails -- querying it needs `orders` with the
    platform_discount column, i.e. db.init_db() must have run."""
    conn.executescript(VIEW_DDL)


def check_required_env() -> None:
    """Raise a clear SystemExit (not a KeyError deep in a request) when
    credentials are missing, so a misconfigured .env fails fast and legibly."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


def _sign(path: str, params: dict, secret: str) -> str:
    """Same HMAC-SHA256 recipe as the other TikTok Shop connectors: app_secret
    + path + sorted(query k+v, excluding sign/access_token) + app_secret."""
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    return hmac.new(secret.encode(), f"{secret}{path}{ordered}{secret}".encode(),
                    hashlib.sha256).hexdigest()


def _get(path: str, extra: dict, page_token: str | None) -> dict:
    """One signed GET. Refreshes an expired access token once, then retries —
    same pattern as tiktok_analytics_sync.py's _request."""
    app_key = os.environ["TIKTOK_APP_KEY"]
    secret = os.environ["TIKTOK_APP_SECRET"]
    for attempt in (1, 2):
        params = {"app_key": app_key, "timestamp": str(int(time.time())),
                  "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"]}
        params.update(extra)
        if page_token:
            params["page_token"] = page_token
        params["sign"] = _sign(path, params, secret)
        r = requests.get(BASE + path, params=params,
                         headers={"x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"],
                                  "Content-Type": "application/json"}, timeout=90)
        data = r.json()
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and attempt == 1:
            _refresh_access_token()
            continue
        if code != 0:
            raise RuntimeError(f"TikTok finance API {r.status_code} code={code}: "
                                f"{data.get('message')}", code)
        return data
    raise RuntimeError("TikTok request failed even after refreshing the access token.")


def _ts(unix) -> str | None:
    try:
        return datetime.fromtimestamp(int(unix), timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return None


def fetch_statements(days: int) -> list[dict]:
    now = int(time.time())
    since = now - days * 86400
    rows: list[dict] = []
    token = None
    for _ in range(MAX_STATEMENT_PAGES):
        payload = _get(STATEMENTS_PATH, {
            "page_size": "100", "sort_field": "statement_time", "sort_order": "DESC",
            "statement_time_ge": str(since), "statement_time_lt": str(now),
        }, token)
        data = payload.get("data") or {}
        batch = data.get("statements") or []
        rows += batch
        token = data.get("next_page_token")
        if not token or not batch:
            break
    return rows


def fetch_statement_transactions(statement_id: str) -> tuple[dict, list[tuple]]:
    """Aggregate one statement's per-order transactions into component
    totals, and return the per-order rows too (see tiktok_settlement_orders).
    Returns (field -> summed amount, order_rows)."""
    agg: dict[str, float] = defaultdict(float)
    order_rows: list[tuple] = []
    token = None
    for _ in range(MAX_TRANSACTION_PAGES):
        d = _get(f"{STATEMENTS_PATH}/{statement_id}/statement_transactions",
                 {"page_size": "100", "sort_field": "order_create_time"}, token)
        data = d.get("data") or {}
        txs = data.get("statement_transactions") or []

        def _f(t: dict, key: str) -> float:
            try:
                return float(t.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        for t in txs:
            oid = t.get("order_id")
            if oid:
                order_rows.append((
                    statement_id, str(oid), _ts(t.get("order_create_time")), t.get("currency"),
                    *[_f(t, k) for k in ORDER_FIELDS],
                ))
            for field in list(FEE_FIELDS) + list(CONTEXT_FIELDS):
                v = _f(t, field)
                if v:
                    agg[field] += v
        token = data.get("next_page_token")
        if not token or not txs:
            break
    return agg, order_rows


def sync(days: int, *, with_components: bool = True, with_orders: bool = True) -> dict:
    """Fetch statements (and optionally their fee breakdown) for the trailing
    `days` and write them. Returns a summary dict for logging/printing."""
    conn = db.connect()
    ensure_schema(conn)
    ensure_view(conn)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    statements = fetch_statements(days)
    with conn:
        for s in statements:
            conn.execute(
                "INSERT OR REPLACE INTO tiktok_settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(s.get("id")), _ts(s.get("statement_time")), _ts(s.get("payment_time")),
                 s.get("payment_status"), s.get("currency"),
                 float(s.get("revenue_amount") or 0), float(s.get("net_sales_amount") or 0),
                 float(s.get("fee_amount") or 0), float(s.get("shipping_cost_amount") or 0),
                 float(s.get("adjustment_amount") or 0), float(s.get("settlement_amount") or 0),
                 stamp))

    n_components = n_orders = 0
    if with_components:
        # Only statements that actually carry fees, largest first — a
        # reasonable prioritization if a run ever needs its own progress
        # tracking (it doesn't yet; a normal window is small enough to
        # finish in one pass).
        todo = sorted([s for s in statements if abs(float(s.get("fee_amount") or 0)) > 0],
                      key=lambda r: -abs(float(r.get("fee_amount") or 0)))
        for s in todo:
            sid = str(s.get("id"))
            agg, order_rows = fetch_statement_transactions(sid)
            if order_rows and with_orders:
                with conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO tiktok_settlement_orders VALUES ("
                        + ",".join("?" * (4 + len(ORDER_FIELDS) + 1)) + ")",
                        [(*r, stamp) for r in order_rows])
                n_orders += len(order_rows)
            if not agg:
                continue
            with conn:
                for field, amount in agg.items():
                    is_fee = 1 if field in FEE_FIELDS else 0
                    label = FEE_FIELDS.get(field) or CONTEXT_FIELDS.get(field) or field
                    conn.execute(
                        "INSERT OR REPLACE INTO tiktok_settlement_components VALUES (?,?,?,?,?,?)",
                        (sid, field, label, is_fee, amount, stamp))
                    n_components += 1

    conn.close()
    revenue = sum(float(s.get("revenue_amount") or 0) for s in statements)
    fees = sum(float(s.get("fee_amount") or 0) for s in statements)
    return {
        "statements": len(statements), "components": n_components, "order_rows": n_orders,
        "revenue": revenue, "fees": abs(fees),
        "rate": (abs(fees) / revenue) if revenue else 0.0,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30, help="trailing days (default 30)")
    p.add_argument("--backfill", action="store_true", help="365-day window")
    p.add_argument("--no-components", action="store_true", help="statements only, skip the fee breakdown (fast)")
    p.add_argument("--no-orders", action="store_true", help="skip retaining per-order fee rows")
    args = p.parse_args()
    window_days = 365 if args.backfill else args.days

    check_required_env()
    db.init_db()
    started = db.now()

    try:
        summary = sync(window_days, with_components=not args.no_components,
                       with_orders=not args.no_orders)
    except Exception as e:  # noqa: BLE001
        db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise

    msg = (f"{summary['statements']} statements / {summary['components']} components "
           f"over {window_days}d; revenue {summary['revenue']:,.0f}, "
           f"fees {summary['fees']:,.0f}, rate {summary['rate']:.2%}")
    db.log_sync(PLATFORM, started, summary["statements"] + summary["components"], "ok", msg)
    print(f"TikTok finance: {msg}")
