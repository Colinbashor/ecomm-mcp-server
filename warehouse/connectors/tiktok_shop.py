"""
TikTok Shop connector. Pulls orders into the `orders` table.

Every request is signed with HMAC-SHA256 (app secret + path + sorted query
params + JSON body + app secret). Pagination follows next_page_token.

If the access token expires mid-run, the connector automatically refreshes it
using TIKTOK_REFRESH_TOKEN, saves the new token back to .env, and resumes the
same page — so paginating through thousands of orders never breaks.

Docs: https://partner.tiktokshop.com/docv2  (Orders > Get Order List)
Returns rows shaped for db.upsert_orders().
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import requests
from dotenv import set_key

PLATFORM = "tiktok"
BASE = "https://open-api.tiktokglobalshop.com"
AUTH_HOST = "https://auth.tiktok-shops.com"
PAGE_SIZE = 100  # max allowed
ENV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".env")

# TikTok business codes that mean "your access token is no longer valid"
TOKEN_EXPIRED_CODES = {105001, 105002}


def _sign(path: str, params: dict, body: str, secret: str) -> str:
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    base_string = f"{secret}{path}{ordered}{body}{secret}"
    return hmac.new(secret.encode(), base_string.encode(), hashlib.sha256).hexdigest()


def _refresh_access_token() -> str:
    """Swap the saved refresh token for a fresh access token; persist to .env."""
    refresh_token = os.environ.get("TIKTOK_REFRESH_TOKEN")
    if not refresh_token:
        raise RuntimeError(
            "Access token expired and no TIKTOK_REFRESH_TOKEN is set to renew it. "
            "Re-run tiktok_auth.py with a fresh auth code."
        )
    resp = requests.get(
        f"{AUTH_HOST}/api/v2/token/refresh",
        params={
            "app_key": os.environ["TIKTOK_APP_KEY"],
            "app_secret": os.environ["TIKTOK_APP_SECRET"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=60,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"TikTok token refresh failed: {data}")
    tok = data["data"]
    new_access = tok["access_token"]
    # persist so the MCP server and future runs use the renewed token
    os.environ["TIKTOK_ACCESS_TOKEN"] = new_access
    set_key(ENV_PATH, "TIKTOK_ACCESS_TOKEN", new_access)
    if tok.get("refresh_token"):
        os.environ["TIKTOK_REFRESH_TOKEN"] = tok["refresh_token"]
        set_key(ENV_PATH, "TIKTOK_REFRESH_TOKEN", tok["refresh_token"])
    print("    (tiktok: access token expired mid-run — refreshed and resumed)")
    return new_access


def _fetch_page(shop_cipher: str, path: str, body: str, page_token: str) -> dict:
    """One signed request. Refreshes the token once if it's expired, then retries."""
    app_key = os.environ["TIKTOK_APP_KEY"]
    secret = os.environ["TIKTOK_APP_SECRET"]

    for attempt in (1, 2):  # attempt 2 only happens after a token refresh
        access_token = os.environ["TIKTOK_ACCESS_TOKEN"]
        params = {
            "app_key": app_key,
            "timestamp": str(int(time.time())),
            "shop_cipher": shop_cipher,
            "page_size": str(PAGE_SIZE),
        }
        if page_token:
            params["page_token"] = page_token
        params["sign"] = _sign(path, params, body, secret)

        resp = requests.post(
            f"{BASE}{path}",
            params=params,
            data=body,
            headers={"Content-Type": "application/json", "x-tts-access-token": access_token},
            timeout=60,
        )
        try:
            payload = resp.json()
        except Exception:
            resp.raise_for_status()
            raise

        code = payload.get("code")
        if code in TOKEN_EXPIRED_CODES and attempt == 1:
            _refresh_access_token()
            continue  # retry the same page with the new token
        if code not in (0, None):
            raise RuntimeError(f"TikTok API error {code}: {payload.get('message')}")
        return payload.get("data", {}) or {}

    raise RuntimeError("TikTok request failed even after refreshing the access token.")


def sync(start_date: str, end_date: str) -> list[dict]:
    shop_cipher = os.environ["TIKTOK_SHOP_CIPHER"]
    path = "/order/202309/orders/search"
    since = int(time.mktime(time.strptime(start_date, "%Y-%m-%d")))
    until = int(time.mktime(time.strptime(end_date, "%Y-%m-%d"))) + 86399
    body = json.dumps({"create_time_ge": since, "create_time_lt": until}, separators=(",", ":"))

    rows: list[dict] = []
    page_token = ""
    while True:
        data = _fetch_page(shop_cipher, path, body, page_token)
        for o in data.get("orders", []):
            order_id = o.get("id")
            order_date = _to_date(o.get("create_time"))
            status = o.get("status")
            currency = (o.get("payment") or {}).get("currency")
            is_sample = 1 if o.get("is_sample_order") else 0
            # For sample orders the "buyer" is the creator. buyer_nickname is a
            # DISPLAY NAME; user_id is a STABLE id. Keep both (id is the future
            # join key once a handle<->user_id map exists). Only for samples —
            # don't retain buyer identity on ordinary customer orders.
            creator = (o.get("buyer_nickname") or None) if is_sample else None
            creator_id = (o.get("user_id") or None) if is_sample else None
            # TikTok emits one line_item per UNIT; the (order_id, sku) primary
            # key means duplicate SKUs would overwrite each other on upsert,
            # so aggregate units of the same SKU into one row first.
            by_sku: dict[str, dict] = {}
            for item in o.get("line_items", []) or [{}]:
                sku = item.get("seller_sku") or item.get("sku_id") or ""
                row = by_sku.get(sku)
                if row:
                    row["quantity"] += 1
                    row["total"] += float(item.get("sale_price", 0) or 0)
                    row["original_total"] += float(item.get("original_price", 0) or 0)
                else:
                    by_sku[sku] = {
                        "platform": PLATFORM,
                        "order_id": str(order_id),
                        "order_date": order_date,
                        "status": status,
                        "sku": sku,
                        "product_name": item.get("product_name"),
                        "quantity": 1,
                        "total": float(item.get("sale_price", 0) or 0),
                        "currency": item.get("currency") or currency,
                        "original_total": float(item.get("original_price", 0) or 0),
                        "is_sample": is_sample,
                        "creator": creator,
                        "creator_id": creator_id,
                    }
            rows.extend(by_sku.values())
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break
    return rows


def _to_date(unix_seconds) -> str:
    if not unix_seconds:
        return ""
    return time.strftime("%Y-%m-%d", time.gmtime(int(unix_seconds)))
