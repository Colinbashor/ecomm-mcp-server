"""
Meta (Facebook / Instagram) Ads connector.

Pulls daily campaign insights from the Marketing API and normalizes them
into ad_metrics rows. Uses a plain HTTPS call so there's no heavy SDK.

Big date ranges are handled automatically: when Meta rejects a query for
requesting too much data (error code 1, "unknown error"), the range is split
in half and each half retried, recursing until the pieces fit. Rate-limit
errors back off and retry, so long backfills complete unattended.

Docs: https://developers.facebook.com/docs/marketing-api/insights
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta

import requests

PLATFORM = "meta"
API_VERSION = "v23.0"

# Meta error codes that mean "you're being throttled — wait and retry"
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80004}


class _RangeTooLarge(Exception):
    """Meta refused the query because the date range asks for too much data."""


def sync(start_date: str, end_date: str) -> list[dict]:
    try:
        return _fetch_range(start_date, end_date)
    except _RangeTooLarge:
        lo = date.fromisoformat(start_date)
        hi = date.fromisoformat(end_date)
        if lo >= hi:
            raise RuntimeError(f"Meta rejected even a single day ({start_date}) as too large.")
        mid = lo + (hi - lo) // 2
        print(f"    (meta: {start_date}..{end_date} too large — splitting)")
        return (sync(start_date, mid.isoformat())
                + sync((mid + timedelta(days=1)).isoformat(), end_date))


def _fetch_range(start_date: str, end_date: str) -> list[dict]:
    token = os.environ["META_ACCESS_TOKEN"]
    account = os.environ["META_AD_ACCOUNT_ID"]  # e.g. act_1234567890

    url = f"https://graph.facebook.com/{API_VERSION}/{account}/insights"
    params = {
        "access_token": token,
        "level": "campaign",
        "time_increment": 1,  # one row per day
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "fields": ("campaign_id,campaign_name,impressions,clicks,spend,actions,"
                   "action_values,account_currency,reach,inline_link_clicks,objective"),
        "limit": 500,
    }

    rows: list[dict] = []
    while url:
        payload = _get_json(url, params)
        for d in payload.get("data", []):
            actions = d.get("actions")
            rows.append(
                {
                    "platform": PLATFORM,
                    "account_id": account,
                    "campaign_id": d.get("campaign_id"),
                    "campaign_name": d.get("campaign_name"),
                    "date": d.get("date_start"),
                    "impressions": int(d.get("impressions", 0) or 0),
                    "clicks": int(d.get("clicks", 0) or 0),
                    "spend": float(d.get("spend", 0) or 0),
                    "conversions": _action_total(actions, _PURCHASE_TYPES),
                    "revenue": _action_total(d.get("action_values"), _PURCHASE_TYPES),
                    "currency": d.get("account_currency"),
                    "campaign_type": d.get("objective"),
                    "reach": int(d.get("reach", 0) or 0),
                    "link_clicks": int(d.get("inline_link_clicks", 0) or 0),
                    "add_to_carts": _action_total(actions, _ATC_TYPES),
                    "checkouts": _action_total(actions, _CHECKOUT_TYPES),
                }
            )
        # follow pagination
        url = payload.get("paging", {}).get("next")
        params = None  # the "next" url already includes all params
    return rows


def _get_json(url: str, params: dict | None) -> dict:
    """One GET with Meta error handling: throttle -> wait, too-big -> split.

    Meta uses error code 1 BOTH for "too much data" and for transient flakes
    (empty-body 500s too), so code-1/5xx responses are retried with backoff
    first; only a persistent code 1 is treated as range-too-large.
    """
    flaky = 0
    for attempt in range(8):
        try:
            resp = requests.get(url, params=params, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(20)  # transient network drop — retry
            continue
        if resp.status_code == 200:
            return resp.json()
        try:
            err = (resp.json() or {}).get("error", {})
        except json.JSONDecodeError:
            err = {}
        code = err.get("code")
        if code == 1 or (resp.status_code >= 500 and code is None):
            flaky += 1
            if flaky >= 3 and code == 1:
                # consistent code 1 = Meta's too-much-data response for insights
                raise _RangeTooLarge(resp.text[:200])
            time.sleep(15 * flaky)
            continue
        if code in RATE_LIMIT_CODES or resp.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"    (meta: rate limited, waiting {wait}s)")
            time.sleep(wait)
            continue
        # anything else: surface Meta's error JSON — raise_for_status hides it
        raise RuntimeError(f"Meta API {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError("Meta API kept failing after retries.")


# Meta's action lists contain OVERLAPPING entries (omni_* already includes the
# pixel/onsite variants). Summing substring matches double-counts 3-4x, so for
# each funnel step take exactly one canonical action_type, with fallbacks.
_PURCHASE_TYPES = ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase")
_ATC_TYPES = ("omni_add_to_cart", "add_to_cart", "offsite_conversion.fb_pixel_add_to_cart")
_CHECKOUT_TYPES = ("omni_initiated_checkout", "initiate_checkout",
                   "offsite_conversion.fb_pixel_initiate_checkout")


def _action_total(actions, candidates: tuple[str, ...]) -> float:
    """First matching action_type from candidates wins (they overlap)."""
    if not actions:
        return 0.0
    by_type = {a.get("action_type"): float(a.get("value", 0) or 0) for a in actions}
    for action_type in candidates:
        if action_type in by_type:
            return by_type[action_type]
    return 0.0
