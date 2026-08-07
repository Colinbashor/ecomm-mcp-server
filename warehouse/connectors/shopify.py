"""
Shopify orders connector (GraphQL Admin API).

Pulls orders into the shared `orders` table (platform='shopify'), one row per
order+SKU (line-item grain, like the TikTok connector) so product-level sales
analysis works. Line totals exclude shipping and tax, so they will NOT equal
Shopify's own "total sales" figure — expect to run a few percent below it.

This is the ground-truth sales feed: ad platforms report *claimed* revenue and
analytics reports *attributed* revenue, but these are the actual orders.

!! `orders.total` IS NOT NET OF DISCOUNT CODES — the sharpest trap in this API !!
`total` here is `discountedTotalSet`, which despite the name EXCLUDES most
discount allocations. Measured against the raw allocations, only
`manual/ACROSS/EXPLICIT` and `automatic/EACH/ENTITLED` are reflected;
`code/EACH/ENTITLED`, `code/ACROSS/ALL` and `manual/ACROSS/ALL` are NOT. So on a
store that discounts through codes, SUM(total) OVERSTATES net line revenue, and
it does so proportionally to promo intensity — meaning it distorts month-over-
month TRENDS, not just levels: a heavily promoted month reads as growth.

  TRUE NET LINE REVENUE = total - SUM(shopify_order_discounts.amount)
  for that order+sku, equivalently originalTotal - allocations.

This connector therefore writes every allocation to `shopify_order_discounts` as
a side effect, so the correction is always available. It deliberately does NOT
rewrite `total` itself: which number your reports use is a decision with
consequences for every historical comparison, so it should be explicit.

DISCOUNT MEASUREMENT — read this before attempting price or elasticity work.
There are TWO distinct discount mechanisms and `orders` only sees one:
  * PROMO discounts (codes, automatic and manual discounts) reduce the line
    below its listed price. Captured here as `original_total`
    (originalTotalSet) minus `total` (discountedTotalSet).
  * MARKDOWNS — where the variant's *price itself* is lowered — are INVISIBLE
    here: originalTotalSet reflects the already-marked-down price, so
    original == total and the discount leaves no trace on the order at all.
Which mechanism dominates is a per-merchant question, and getting it wrong
invalidates the analysis. Check before assuming: if most orders show
original == total, the discounting is happening in the price, not in codes.

Markdown depth can only come from the variant's compareAtPrice on the CATALOG
side. Note that Shopify exposes no price-history API, so that history exists
only if you have been recording it — start early, because it cannot be
backfilled.

Incremental syncs page the regular GraphQL API, which is fine for daily windows.
For a multi-year backfill use the Bulk Operations API instead: one server-side
query per month returning JSONL, rather than millions of paged requests.

Setup (once — Shopify killed admin-created custom apps on 2026-01-01; apps
are now made in the Dev Dashboard and auth is the OAuth client-credentials
grant, https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant):
  1. Someone with org developer access: https://dev.shopify.com > create an
     app, set Admin API scopes read_orders AND read_all_orders (without
     read_all_orders the API only returns the last 60 days), release a
     version, install it on the store.
  2. Copy the app's Client ID + Client Secret into .env:
       SHOPIFY_SHOP=your-store.myshopify.com     (the *.myshopify.com slug)
       SHOPIFY_CLIENT_ID=...
       SHOPIFY_CLIENT_SECRET=...
  The connector exchanges those for a 24h access token automatically and
  re-exchanges when it expires. (Legacy path: if SHOPIFY_ADMIN_TOKEN holds a
  static shpat_ token from a pre-2026 custom app, it's used directly.)

Rate limits: GraphQL is cost-based (bucket refills per second; Plus gets 2x).
On THROTTLED errors we wait and retry, so multi-month backfills complete
unattended:  python run_sync.py --only shopify --start 2022-06-01 --end ...

Docs: https://shopify.dev/docs/api/admin-graphql/latest/queries/orders
"""
from __future__ import annotations

import os
import time

import requests

PLATFORM = "shopify"
API_VERSION = "2025-07"

_QUERY_TEMPLATE = """
query($n: Int!, $cursor: String, $q: String!) {
  orders(first: $n, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      name
      createdAt
      displayFinancialStatus
      sourceName
      %s
      lineItems(first: 50) {
        nodes {
          sku
          title
          quantity
          discountedTotalSet { shopMoney { amount currencyCode } }
          originalTotalSet { shopMoney { amount } }
          discountAllocations {
            allocatedAmountSet { shopMoney { amount } }
            discountApplication {
              __typename
              allocationMethod
              targetSelection
              ... on DiscountCodeApplication { code }
              ... on AutomaticDiscountApplication { title }
              ... on ManualDiscountApplication { title }
              ... on ScriptDiscountApplication { title }
            }
          }
        }
      }
    }
  }
}
"""
# 50 line items covers essentially every order; nested cost forces small pages.
PAGE_SIZE = 40

# The customer id is the join key for every customer-grain question (LTV, repeat
# rate, cohorts, retention) — `orders` has no customer column without it. Only the
# ID is ever requested: no email/name/phone, by explicit decision, so this stays
# Level 1 protected customer data and no PII lands in the warehouse.
_CUSTOMER_FIELD = "customer { id }"


def _build_query(include_customer: bool) -> str:
    return _QUERY_TEMPLATE % (_CUSTOMER_FIELD if include_customer else "")


# --- read_customers capability ----------------------------------------------
# Requesting `customer` WITHOUT the read_customers scope does not quietly return
# null: the response carries an ACCESS_DENIED error, and _post() raises on any
# error, which would take down the entire nightly order sync the whole team
# depends on. So the field is requested only when the scope is genuinely granted.
# The probe FAILS CLOSED — any uncertainty at all and we omit the field and keep
# syncing orders, because a false positive costs the nightly feed while a false
# negative costs only this run's customer ids (a later run backfills them).
# Effect: capture self-enables the moment someone releases + reinstalls the app
# with read_customers. Nothing here needs redeploying or editing.
_SCOPE_PROBE_TTL_SECONDS = 6 * 3600
_scope_cache: dict[str, object] = {"granted": None, "checked_at": 0.0}


def _capture_override() -> bool | None:
    """SHOPIFY_CAPTURE_CUSTOMER as a tri-state: True / False / None (unset).
    Separate from customer_capture_enabled() so callers that must not touch the
    network (a --status flag, say) can consult it alone."""
    raw = os.environ.get("SHOPIFY_CAPTURE_CUSTOMER")
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() not in ("0", "false", "no", "off")


def customer_capture_enabled() -> bool:
    """True iff the app holds read_customers, so `customer { id }` is selectable.

    Cached for _SCOPE_PROBE_TTL_SECONDS so a long backfill costs one probe, not
    one per page. SHOPIFY_CAPTURE_CUSTOMER=0 forces it off (kill switch), =1
    forces it on without probing (tests / a known-good grant)."""
    override = _capture_override()
    if override is not None:
        return override
    cached = _scope_cache["granted"]
    if cached is not None and time.time() - float(_scope_cache["checked_at"]) < _SCOPE_PROBE_TTL_SECONDS:
        return bool(cached)
    granted = False
    try:
        shop = os.environ["SHOPIFY_SHOP"]
        resp = requests.get(
            f"https://{shop}/admin/oauth/access_scopes.json",
            headers={"X-Shopify-Access-Token": _access_token(shop)},
            timeout=30,
        )
        if resp.status_code == 200:
            handles = {s.get("handle") for s in (resp.json().get("access_scopes") or [])}
            granted = "read_customers" in handles
    except Exception:
        granted = False   # deliberately broad: nothing about this probe may ever
                          # be allowed to break the order sync.
    _scope_cache["granted"] = granted
    _scope_cache["checked_at"] = time.time()
    return granted


def numeric_id(gid: str | None) -> str | None:
    """'gid://shopify/Customer/6650756268289' -> '6650756268289'.

    Stored numeric rather than as the gid so it joins the id space other systems
    use — most platforms that carry a Shopify customer id (email tools, loyalty
    apps) expose the bare number, not the gid."""
    if not gid:
        return None
    tail = str(gid).rstrip("/").rsplit("/", 1)[-1]
    return tail or None


# Client-credentials tokens last 24h; cache and re-exchange a bit early.
_token_cache: dict[str, float | str] = {"value": "", "expires_at": 0.0}


def _access_token(shop: str) -> str:
    static = os.environ.get("SHOPIFY_ADMIN_TOKEN")
    if static:  # legacy pre-2026 custom-app token
        return static
    if _token_cache["value"] and time.time() < float(_token_cache["expires_at"]):
        return str(_token_cache["value"])
    resp = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["SHOPIFY_CLIENT_ID"],
            "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"],
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Shopify token exchange failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _token_cache["value"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 86400)) - 300
    return data["access_token"]


def _post(variables: dict, query: str | None = None) -> dict:
    """One GraphQL call. Waits out THROTTLED responses instead of failing.
    Token is resolved per call (cached) so multi-day backfills outlive the
    24h client-credentials token.

    A dropped connection is retried like a throttle, not raised. Observed live
    2026-08-04: `--only shopify --days 1` died on a ConnectionReset (10054) and
    an immediate identical re-run succeeded, so one transient blip was failing a
    whole nightly sync. Endpoints shedding connections under load is expected
    behaviour, not an error worth aborting a run for."""
    shop = os.environ["SHOPIFY_SHOP"]          # e.g. your-store.myshopify.com
    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    doc = query if query is not None else _build_query(False)
    last_err = None
    for attempt in range(8):
        try:
            headers = {"X-Shopify-Access-Token": _access_token(shop),
                       "Content-Type": "application/json"}
            resp = requests.post(url, headers=headers,
                                 json={"query": doc, "variables": variables}, timeout=90)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # Token exchange is inside the try deliberately: it posts to the same
            # host and drops the same way.
            last_err = e
            time.sleep(min(60, 3 * (attempt + 1)))
            continue
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 2)))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Shopify API {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        errors = payload.get("errors") or []
        if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors):
            # wait for the cost bucket to refill enough for one more page
            cost = payload.get("extensions", {}).get("cost", {})
            status = cost.get("throttleStatus", {})
            need = cost.get("requestedQueryCost", 500)
            rate = status.get("restoreRate", 50)
            time.sleep(max(1.0, need / rate))
            continue
        if errors:
            raise RuntimeError(f"Shopify GraphQL error: {errors[:2]}")
        return payload["data"]
    # Distinguish the two exhaustion modes — "kept throttling" would be a lie if
    # every attempt actually died on the network.
    raise RuntimeError(f"Shopify API failed after retries: {last_err}" if last_err
                       else "Shopify API kept throttling after retries.")


def sync(start_date: str, end_date: str) -> list[dict]:
    """Return `orders` rows for the window.

    SIDE EFFECT: promo discount attribution is written to
    shopify_order_discounts here rather than returned, because run_sync.py
    drives every connector generically (`rows = module.sync(...)` ->
    upsert_orders) and has no second channel. Writing it alongside the parse
    keeps the two streams from drifting apart.

    SECOND SIDE EFFECT, same reason: the Shopify customer id goes to
    shopify_order_customers, but ONLY when the app holds read_customers (see
    customer_capture_enabled). Without the scope the field is not even requested,
    so this stays a no-op and the order sync is untouched."""
    q = f"created_at:>='{start_date}T00:00:00Z' AND created_at:<='{end_date}T23:59:59Z'"
    want_customer = customer_capture_enabled()
    doc = _build_query(want_customer)

    rows: list[dict] = []
    disc: list[dict] = []
    cust: list[dict] = []
    cursor = None
    while True:
        data = _post({"n": PAGE_SIZE, "cursor": cursor, "q": q}, doc)
        block = data["orders"]
        for o in block["nodes"]:
            name = o.get("name")
            day = (o.get("createdAt") or "")[:10]
            items = (o.get("lineItems") or {}).get("nodes") or []
            rows.extend(_order_rows(
                name, day, o.get("displayFinancialStatus"), items, o.get("sourceName")))
            disc.extend(_discount_rows(name, day, items))
            if want_customer:
                cust.append(_customer_row(name, day, o.get("customer")))
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]

    from warehouse import db  # local import: avoids a circular import at module load
    if disc:
        db.upsert_shopify_discounts(disc)
    if cust:
        db.upsert_shopify_order_customers(cust)
    return rows


def _customer_row(order_id: str, order_date: str, customer: dict | None) -> dict:
    """One shopify_order_customers row. A guest checkout has no customer, which is
    recorded as a NULL customer_id rather than dropped — otherwise a missing row
    would be ambiguous between "guest order" and "never crawled"."""
    return {
        "order_id": order_id,
        "customer_id": numeric_id((customer or {}).get("id")),
        "order_date": order_date,
    }


# DiscountApplication is an interface; __typename is the only unambiguous way to
# tell automatic from manual (both expose `title` and nothing else distinguishing).
_DISCOUNT_KIND = {
    "DiscountCodeApplication": "code",
    "AutomaticDiscountApplication": "automatic",
    "ManualDiscountApplication": "manual",
    "ScriptDiscountApplication": "script",
}


def _discount_rows(order_id: str, order_date: str, items: list[dict]) -> list[dict]:
    """Per-line promo discount attribution -> shopify_order_discounts rows.

    Grain is order_id + sku + kind + code (matching `orders`' order+sku grain),
    so the same discount landing on several line items of one sku is SUMMED.
    Returns [] for the ~98% of lines that carry no allocation."""
    agg: dict[tuple, dict] = {}
    for item in items:
        sku = item.get("sku") or ""
        for alloc in item.get("discountAllocations") or []:
            app = alloc.get("discountApplication") or {}
            kind = _DISCOUNT_KIND.get(app.get("__typename"), "other")
            # codes expose `code`, automatic/manual/script expose `title`
            code = app.get("code") or app.get("title") or "(unnamed)"
            money = (alloc.get("allocatedAmountSet") or {}).get("shopMoney") or {}
            raw = money.get("amount")
            amount = float(raw) if raw is not None else 0.0
            key = (sku, kind, code)
            row = agg.get(key)
            if row:
                row["amount"] += amount
            else:
                agg[key] = {
                    "order_id": order_id,
                    "sku": sku,
                    "order_date": order_date,
                    "kind": kind,
                    "code": code,
                    "amount": amount,
                    "allocation_method": app.get("allocationMethod"),
                    "target_selection": app.get("targetSelection"),
                }
    return list(agg.values())


def _order_rows(order_id: str, order_date: str, status: str, items: list[dict],
                source: str | None = None) -> list[dict]:
    """One row per SKU within an order (duplicate-SKU line items aggregated,
    since the table's primary key is order_id + sku)."""
    by_sku: dict[str, dict] = {}
    for item in items:
        sku = item.get("sku") or ""
        money = (item.get("discountedTotalSet") or {}).get("shopMoney") or {}
        orig = (item.get("originalTotalSet") or {}).get("shopMoney") or {}
        # originalTotal = line value BEFORE code/automatic discounts, at whatever
        # price the variant was listed at. It does NOT see markdowns (a lowered
        # variant price makes original == discounted) — for markdown depth you
        # need the variant's compareAtPrice from the catalog side.
        # NB: keep a genuine $0.00 original as 0.0 — `float(x) or None` would turn
        # free/gift lines into NULL and conflate "no value" with "zero".
        _raw_orig = orig.get("amount")
        orig_amt = float(_raw_orig) if _raw_orig is not None else None
        row = by_sku.get(sku)
        if row:
            row["quantity"] += int(item.get("quantity", 0) or 0)
            row["total"] += float(money.get("amount", 0) or 0)
            if orig_amt is not None:
                row["original_total"] = (row.get("original_total") or 0) + orig_amt
        else:
            by_sku[sku] = {
                "platform": PLATFORM,
                "order_id": order_id,               # e.g. #4519001 — human-stable id
                "order_date": order_date,
                "status": status,
                "sku": sku,
                "product_name": item.get("title"),
                "quantity": int(item.get("quantity", 0) or 0),
                "total": float(money.get("amount", 0) or 0),
                "currency": money.get("currencyCode"),
                "source": source,
                "original_total": orig_amt,
            }
    return list(by_sku.values())
