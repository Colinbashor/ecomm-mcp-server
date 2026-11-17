"""
Amazon retail orders connector (Selling Partner API, Orders v0).

Pulls your Amazon Seller Central orders into the `orders` table — this is the
"regular Amazon" commerce side, separate from Amazon Advertising (amazon_ads.py).

Auth is Login with Amazon (LWA): we swap a refresh token for a 1-hour access
token and send it as x-amz-access-token. As of 2023 the SP-API no longer needs
AWS SigV4 signing, so LWA alone is enough.

We store one row per order (order-level totals, sku left blank). SKU-level
detail is available via getOrderItems but is heavily throttled, so we skip it
to keep syncs fast and within rate limits.

Docs: https://developer-docs.amazon.com/sp-api/docs/orders-api-v0-reference
Returns rows shaped for db.upsert_orders().
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import requests

PLATFORM = "amazon"

# Regional SP-API endpoints. LWA token endpoint is global (api.amazon.com).
HOSTS = {
    "NA": "https://sellingpartnerapi-na.amazon.com",
    "EU": "https://sellingpartnerapi-eu.amazon.com",
    "FE": "https://sellingpartnerapi-fe.amazon.com",
}
TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# LWA access tokens last ~1 hour; refresh a little early to be safe on long pulls.
_token_cache: dict[str, float | str] = {"value": "", "expires_at": 0.0}


def _access_token() -> str:
    if _token_cache["value"] and time.time() < float(_token_cache["expires_at"]):
        return str(_token_cache["value"])
    for attempt in range(5):
        try:
            resp = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": os.environ["SPAPI_REFRESH_TOKEN"],
                    "client_id": os.environ["SPAPI_CLIENT_ID"],
                    "client_secret": os.environ["SPAPI_CLIENT_SECRET"],
                },
                timeout=60,
            )
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == 4:
                raise
            time.sleep(30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["value"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 120
    return data["access_token"]


def _get(host: str, path: str, params: dict) -> dict:
    """One GET with LWA auth, waiting out throttling.

    getOrders allows a burst of ~20 requests, then refills at ~1/minute — so
    past the burst, every page needs a ~60s wait. Patience, not failure.
    """
    for attempt in range(10):
        try:
            resp = requests.get(
                f"{host}{path}",
                params=params,
                headers={
                    "x-amz-access-token": _access_token(),
                    "Accept": "application/json",
                },
                timeout=60,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(30)  # transient network drop — retry, don't kill a long drip
            continue
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 0) or 0) or min(65.0, 5 * (attempt + 1) + 20)
            time.sleep(min(wait, 120))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"SP-API {path} error {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise RuntimeError(f"SP-API {path} kept throttling after retries.")


def sync(start_date: str, end_date: str) -> list[dict]:
    region = os.environ.get("SPAPI_REGION", "NA").upper()
    host = HOSTS.get(region)
    if not host:
        raise RuntimeError(f"SPAPI_REGION must be NA/EU/FE (got {region!r}).")
    marketplace_id = os.environ["SPAPI_MARKETPLACE_ID"]
    path = "/orders/v0/orders"

    # getOrders 400s (InvalidInput) if CreatedBefore is less than ~2 minutes in
    # the past — an end_date of today would put it hours in the future.
    created_before = f"{end_date}T23:59:59Z"
    now_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_params = {
        "MarketplaceIds": marketplace_id,
        "CreatedAfter": f"{start_date}T00:00:00Z",
        "CreatedBefore": min(created_before, now_cutoff),
        "MaxResultsPerPage": 100,
    }

    rows: list[dict] = []
    next_token = ""
    while True:
        # When paginating, the API wants NextToken (+ MarketplaceIds) only.
        if next_token:
            params = {"MarketplaceIds": marketplace_id,
                      "NextToken": next_token}
        else:
            params = dict(base_params)

        payload = _get(host, path, params).get("payload", {})
        rows.extend(_row(o) for o in payload.get("Orders", []))
        next_token = payload.get("NextToken") or ""
        if not next_token:
            break
        # getOrders next-page rate limit is ~1 req/sec; pace ourselves.
        time.sleep(1)

    fresh = refresh_open_orders(host, marketplace_id, start_date,
                                skip={r["order_id"] for r in rows})
    if fresh:
        print(f"    amazon_orders: re-read {len(fresh)} older open order(s)", flush=True)
    return rows + fresh


def _row(o: dict) -> dict:
    total = o.get("OrderTotal") or {}
    shipped = int(o.get("NumberOfItemsShipped", 0) or 0)
    unshipped = int(o.get("NumberOfItemsUnshipped", 0) or 0)
    return {
        "platform": PLATFORM,
        "order_id": o.get("AmazonOrderId"),
        "order_date": (o.get("PurchaseDate") or "")[:10],
        "status": o.get("OrderStatus"),
        "sku": "",  # order-level row; see module docstring
        "product_name": None,
        "quantity": shipped + unshipped,
        "total": float(total.get("Amount", 0) or 0),
        "currency": total.get("CurrencyCode"),
    }


# ORDERS CHANGE AFTER THEY ARE CREATED. A Pending order has no OrderTotal yet
# (stored as 0) and becomes Shipped or Canceled later; a pull keyed on creation
# date never looks again, so its revenue stays at $0 in the warehouse for good.
# So every sync re-reads stored OPEN orders by id (getOrders takes up to 50
# AmazonOrderIds per call), newest first, capped so it fits inside getOrders'
# ~20-request burst; a backlog drains over successive runs.
OPEN_STATUSES = ("Pending", "Unshipped", "PartiallyShipped", "PendingAvailability")
OPEN_LOOKBACK_DAYS = 120
REFRESH_OPEN_LIMIT = 600
IDS_PER_CALL = 50


def open_order_ids(conn, start_date: str, limit: int = REFRESH_OPEN_LIMIT,
                   today=None) -> list[str]:
    """Stored orders still open, created before the window, newest first."""
    today = today or datetime.now(timezone.utc).date()
    floor = (today - timedelta(days=OPEN_LOOKBACK_DAYS)).isoformat()
    marks = ",".join("?" * len(OPEN_STATUSES))
    return [r[0] for r in conn.execute(
        f"SELECT order_id FROM orders WHERE platform=? AND status IN ({marks}) "
        f"AND order_date >= ? AND order_date < ? ORDER BY order_date DESC LIMIT ?",
        (PLATFORM, *OPEN_STATUSES, floor, start_date, limit))]


def refresh_open_orders(host: str, marketplace_id: str, start_date: str,
                        skip: set[str] = frozenset(), conn=None,
                        limit: int = REFRESH_OPEN_LIMIT) -> list[dict]:
    """Current rows for stored open orders. Best-effort: a failure here is
    printed and returns what was gathered, never failing the main pull.

    `limit` above the default is for a one-off backlog repair: past getOrders'
    ~20-request burst each call waits about a minute, so 10,000 ids take ~3h."""
    own = conn is None
    if own:
        from warehouse import db
        conn = db.connect_readonly()
    try:
        ids = [i for i in open_order_ids(conn, start_date, limit=limit) if i not in skip]
    finally:
        if own:
            conn.close()
    out: list[dict] = []
    for i in range(0, len(ids), IDS_PER_CALL):
        chunk = ids[i:i + IDS_PER_CALL]
        try:
            payload = _get(host, "/orders/v0/orders", {
                "MarketplaceIds": marketplace_id,
                "AmazonOrderIds": ",".join(chunk)}).get("payload", {})
        except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
            print(f"    amazon_orders: open-order refresh stopped after {len(out)} "
                  f"rows: {str(exc)[:150]}", flush=True)
            break
        out.extend(_row(o) for o in payload.get("Orders", []))
        time.sleep(1)
    return out
