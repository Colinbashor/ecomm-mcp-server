r"""
Purple Dot (pre-order / waitlist platform) -> pre-order bookings + waitlist
allocation.

Purple Dot lets customers book an unreleased or oversold product against a
"waitlist" (typically one per product, with a per-variant unit allocation),
and later EXPORTS the booking into your storefront (e.g. Shopify) as a real
order once stock lands. If your finance/ops team tracks pre-order demand as a
dashboard line, it's usually built from those exported storefront orders -
which is fine, but it is a LAGGING and INCOMPLETE view of demand (see BOOKED
VS EXPORTED below).

WHY THIS GETS ITS OWN TABLES (not the shared `orders` table):
An exported pre-order already shows up in your storefront order feed once it
exports - writing it into `orders` a second time here would double-count
every downstream consumer. More fundamentally the two rows are DIFFERENT
MEASURES: a pre-order row is a *booking*, dated at booking time; the
storefront order row appears only at *export* time, which can be days to
weeks later, and a meaningful share of bookings may cancel and never export
at all. If you want one number, decide which measure you mean and don't
silently swap between them.

FIVE tables:
  purple_dot_preorders        - PK id (Purple Dot's UUID). The booking:
                                order_number/reference, created_at/placed_at,
                                cancelled_at/cancel_reason, currency, money
                                totals (subtotal/discounts/tax/total/
                                refunded), a convenience link to the FIRST
                                storefront export (shopify_order_id,
                                exported_at, n_exported), rolled n_lines/
                                n_units, and ship_city/province/country for
                                demand geography.
  purple_dot_preorder_lines   - PK (preorder_id, line_id). The useful grain:
                                sku, product_id + variant_id (join keys into
                                your storefront's own catalog/order tables),
                                quantity + money, a per-line cancelled flag,
                                waitlist_id, shopify_line_item_id (a direct
                                line-level join into the storefront order,
                                where the API populates it), and
                                earliest/latest_ship_date - the promised
                                delivery window, which most order feeds don't
                                carry at all.
  purple_dot_preorder_exports - PK (preorder_id, shopify_order_id). One row
                                per storefront order a booking exported into.
                                This is a SEPARATE table (not just a column on
                                purple_dot_preorders) because a booking can
                                export in MULTIPLE PARTS as stock arrives in
                                waves (e.g. order_number 'PD1389447/1',
                                '/2', ...) - a single parent column would only
                                ever capture the first part. Join your order
                                table on shopify_order_id.
  purple_dot_waitlists        - PK id. CURRENT state per waitlist (e.g.
                                Live/Paused/Closed/Scheduled/Draft - exact
                                enum depends on your Purple Dot account),
                                ship-date window, launch/pause dates, labels,
                                storefront product_id, and product-level
                                buy_size/committed/available. Upserted -
                                latest state wins, like a typical inventory
                                snapshot table.
  purple_dot_waitlist_inventory - PK (snapshot_date, waitlist_id, variant_id).
                                DAILY SNAPSHOT of the per-variant pre-order
                                allocation (buy_size = units offered,
                                committed = units booked, available = units
                                left). The API only reports CURRENT levels, so
                                - exactly like an inventory-snapshot table -
                                running this daily is what builds sell-through
                                HISTORY; without it you can never ask "how
                                fast did this pre-order sell through".
                                SCOPED BY DEFAULT to states that can still
                                move (SNAPSHOT_STATES = live/scheduled) -
                                snapshotting every waitlist every day adds a
                                lot of dead rows for waitlists that are
                                permanently paused/closed and will never
                                change again. `--snapshot-all` overrides.
                                `purple_dot_waitlists` always holds current
                                state for every waitlist regardless.

BOOKED VS EXPORTED: if your warehouse's only view of pre-order demand today
comes from storefront orders, expect this feed to run noticeably AHEAD of
that number, because a booking only becomes a storefront order once Purple
Dot exports it - which can lag by anywhere from same-day to several weeks,
and a nonzero share of bookings cancel before ever exporting. So the
storefront-derived number isn't wrong in kind, it's just booking demand shown
late and undercounted by whatever never exports. Cross-check n_exported /
shopify_order_id on a row before treating either measure as "the" total, and
compute your own lag distribution from created_at vs exported_at rather than
assuming a fixed lag.

CURRENCY IS MIXED, AND THERE IS NO FX RATE IN THE PAYLOAD. Money fields are in
each order's OWN currency (`currency` column), and Purple Dot does not attach
an exchange rate anywhere in the response. NEVER SUM total_price (or any
other money column) across rows without first filtering to one currency or
converting yourself. This isn't just an imprecision risk: some currencies
(e.g. JPY, KRW) have no minor unit, so their "amount" is ~100x the numeric
magnitude of a dollar-equivalent order - a naive SUM across currencies can be
dominated by a handful of orders in one of those currencies even when they're
a small share of order COUNT. Filter `currency = 'USD'` (or whichever is your
base currency) unless you are deliberately converting.

CANCELLATIONS AND REFUNDS ARE MATERIAL, NOT AN EDGE CASE. Expect a real
fraction of bookings to cancel, and note that a LINE can cancel independently
of its parent order - order-grain filtering alone is not enough, filter
`cancelled = 0` at LINE grain. Also expect a real fraction of orders to carry
a partial or full refund; net `total_refunded` against `total_price` rather
than treating `total_price` as realized revenue.

NON-MERCHANDISE LINES: pre-order checkouts commonly push shipping-protection
upsells, gift cards, or membership/subscription add-ons harder than a normal
checkout flow (it's an extra touchpoint), so if your reporting already
excludes non-merchandise SKUs from other order feeds (protection upsells,
gift cards, etc.), apply the SAME exclusion list here before computing units
or revenue - this connector stores every line as-is and does not attempt any
such filtering itself. That curation is business-specific and belongs in your
reporting layer, not in the sync.

PII: customer email/name/phone are deliberately NOT stored. `customer_external_id`
(your storefront's customer id, if the API returns one) is kept because it's
useful for repeat-booking/cohort analysis without being contact information.
Full street address and phone are dropped; only city/province/country are
kept for demand geography. If you expose this warehouse over a shared/remote
interface with column-level access control, add the geography columns to
whatever denylist you use there.

API NOTES (Purple Dot's private admin API - verify current behavior against
https://docs.getpurpledot.com/docs/purple-dot-apis/private-api since any
third-party API can shift its contract over time):
  * Base https://www.purpledotprice.com/admin/api/v1, header
    X-Purple-Dot-Access-Token. This is the PRIVATE API token from the
    merchant portal - Purple Dot also has a separate PUBLIC API key (used
    client-side on your storefront) that will NOT authenticate here; a 401/403
    on every call usually means the wrong key was configured, not a revoked one.
  * Pre-orders sort OLDEST-FIRST by created_at and page via an opaque
    `starting_after` cursor (also echoed in a `Link: <...>; rel={next}` response
    header - note the non-standard curly braces around `next`, unlike the more
    common `rel="next"` convention). limit maxes at 200 for pre-orders, 100 for
    waitlists.
  * The pre-order object has no `updated_at` field, but `updated_at_min` IS a
    working query filter - and it's the only way to catch a cancellation,
    refund, or a new export landing on an OLDER booking, so incremental syncs
    should key off update time, not created_at. Passing `updated_at_min`
    changes the result ordering to update time as well.
  * `/pre-orders/count` accepts the same filters as `/pre-orders` - useful for
    a cheap expected-vs-fetched completeness check without paging everything.
  * `/fulfillment-orders` (hyphenated) is a real, documented endpoint - it will
    return an empty list if you fulfill orders outside Purple Dot (e.g.
    directly through your storefront or a 3PL), which is not an error. The
    underscore spelling `/fulfillment_orders` redirects to an admin login page
    instead of 404ing, which can be mistaken for an auth problem - it's just
    the wrong path.
  * `/inventory` is a POST endpoint that WRITES Purple Dot's available-units
    allocation. This connector is read-only and never calls it - don't wire it
    in without deciding you actually want to push allocation changes.
  * A full historical backfill of pre-orders is typically a same-sitting job
    (minutes, not a multi-day crawl), since latency is roughly one request per
    page rather than one request per record - always page at each endpoint's
    documented max. There is no fixed "floor" date to hardcode: the API simply
    stops returning records once you reach your account's actual history, so
    let the crawl run to exhaustion rather than assuming a specific start date.

USAGE:
  python purple_dot_sync.py                 # incremental (updated_at_min high-water mark)
  python purple_dot_sync.py --days 30       # re-pull anything updated in the last 30 days
  python purple_dot_sync.py --backfill      # resumable full crawl of all pre-orders
  python purple_dot_sync.py --backfill --restart
  python purple_dot_sync.py --only waitlists
  python purple_dot_sync.py --pages 50      # cap pages fetched this run

sync_log platforms: 'purple_dot' (pre-orders) and 'purple_dot_waitlists' -
logged independently so one failing does not hide the other.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from warehouse import db

load_dotenv()

BASE = "https://www.purpledotprice.com/admin/api/v1"
PREORDER_PAGE = 200          # documented max
WAITLIST_PAGE = 100          # documented max
DEFAULT_MAX_PAGES = 100_000
CHECKPOINT_EVERY = 500       # pre-orders buffered before a commit + cursor save

# An update (cancellation/refund/export) can land against an older booking,
# and the incremental window is keyed on update time, so re-read a little
# history each run. Cheap: every write here is an idempotent upsert.
OVERLAP_HOURS = 48
FIRST_RUN_DAYS = 7           # no high-water mark yet -> small window, then suggest --backfill

CURSOR_KEY = "preorders_cursor"                    # --backfill resume point
HIGH_WATER_KEY = "preorders_updated_high_water"

DDL = """
CREATE TABLE IF NOT EXISTS purple_dot_preorders (
    id                    TEXT PRIMARY KEY,   -- Purple Dot UUID
    order_number          TEXT,
    reference             TEXT,               -- customer-facing reference, e.g. #PD1731592
    created_at            TEXT,
    placed_at             TEXT,
    cancelled_at          TEXT,
    cancel_reason         TEXT,
    currency              TEXT,               -- MIXED across rows - never sum without filtering
    customer_external_id  TEXT,               -- your storefront's customer id (no email/name stored)
    subtotal_price        REAL,
    total_discounts       REAL,
    total_tax             REAL,
    total_price           REAL,
    total_refunded        REAL,
    tax_included          INTEGER,
    discount_codes        TEXT,               -- JSON array as returned by the API
    ship_city             TEXT,
    ship_province         TEXT,
    ship_country          TEXT,
    n_lines               INTEGER,
    n_units               INTEGER,
    shopify_order_id      TEXT,               -- FIRST exported storefront order (see _exports table)
    shopify_order_number  TEXT,
    exported_at           TEXT,               -- when it first became a storefront order
    n_exported            INTEGER,            -- >1 = split export across multiple storefront orders
    synced_at             TEXT
);
CREATE TABLE IF NOT EXISTS purple_dot_preorder_exports (
    preorder_id           TEXT NOT NULL,
    shopify_order_id      TEXT NOT NULL,      -- join your order/catalog tables
    export_no             INTEGER,            -- 0-based order of export
    shopify_order_number  TEXT,               -- e.g. PD1389447/2 on a split export
    export_type           TEXT,               -- e.g. SHOPIFY_ORDER
    exported_at           TEXT,
    n_lines               INTEGER,            -- lines carried in this particular export
    synced_at             TEXT,
    PRIMARY KEY (preorder_id, shopify_order_id)
);
CREATE TABLE IF NOT EXISTS purple_dot_preorder_lines (
    preorder_id           TEXT NOT NULL,
    line_id               TEXT NOT NULL,      -- Purple Dot line UUID
    line_no               INTEGER,
    sku                   TEXT,               -- join your catalog's SKU space
    product_id            TEXT,               -- storefront product id
    variant_id            TEXT,               -- storefront variant id
    name                  TEXT,
    quantity              INTEGER,
    unit_price            REAL,
    unit_total            REAL,
    price                 REAL,               -- pre-discount line total
    total_discount        REAL,
    total                 REAL,               -- post-discount, pre-tax
    taxable               INTEGER,
    earliest_ship_date    TEXT,               -- promised delivery window
    latest_ship_date      TEXT,
    waitlist_id           TEXT,
    cancelled             INTEGER,
    cancelled_at          TEXT,
    shopify_line_item_id  TEXT,               -- populated once this line has exported
    synced_at             TEXT,
    PRIMARY KEY (preorder_id, line_id)
);
CREATE TABLE IF NOT EXISTS purple_dot_waitlists (
    id                    TEXT PRIMARY KEY,
    created_at            TEXT,
    updated_at            TEXT,
    state                 TEXT,               -- e.g. Live/Paused/Closed/Scheduled/Draft
    earliest_ship_date    TEXT,
    latest_ship_date      TEXT,
    launch_date           TEXT,
    scheduled_pause_date  TEXT,
    labels                TEXT,               -- JSON array
    product_id            TEXT,               -- storefront product id
    buy_size              INTEGER,            -- product-level units offered
    committed             INTEGER,            -- product-level units booked
    available             INTEGER,
    n_variants            INTEGER,
    synced_at             TEXT
);
CREATE TABLE IF NOT EXISTS purple_dot_waitlist_inventory (
    snapshot_date         TEXT NOT NULL,      -- API is current-only; daily runs build history
    waitlist_id           TEXT NOT NULL,
    variant_id            TEXT NOT NULL,
    sku                   TEXT,
    state                 TEXT,               -- denormalized waitlist state
    buy_size              INTEGER,            -- units allocated to the pre-order
    committed             INTEGER,            -- units booked
    available             INTEGER,            -- units left (NULL on per-product-only waitlists)
    synced_at             TEXT,
    PRIMARY KEY (snapshot_date, waitlist_id, variant_id)
);
CREATE TABLE IF NOT EXISTS purple_dot_sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pd_lines_sku ON purple_dot_preorder_lines(sku);
CREATE INDEX IF NOT EXISTS idx_pd_lines_waitlist ON purple_dot_preorder_lines(waitlist_id);
CREATE INDEX IF NOT EXISTS idx_pd_pre_created ON purple_dot_preorders(created_at);
CREATE INDEX IF NOT EXISTS idx_pd_pre_shopify ON purple_dot_preorders(shopify_order_id);
CREATE INDEX IF NOT EXISTS idx_pd_wlinv_sku ON purple_dot_waitlist_inventory(sku);
CREATE INDEX IF NOT EXISTS idx_pd_exports_shopify ON purple_dot_preorder_exports(shopify_order_id);
"""

# Waitlist states whose allocation can still move, so a daily history row is
# worth keeping. Paused/closed/draft waitlists are static and are captured in
# purple_dot_waitlists' current-state row instead. --snapshot-all ignores this.
SNAPSHOT_STATES = {"live", "scheduled"}


def ensure_schema(conn) -> None:
    """Create every table this script owns. Safe to call repeatedly and safe
    to call on a brand-new warehouse.db - CREATE TABLE/INDEX IF NOT EXISTS
    only."""
    conn.executescript(DDL)
    conn.commit()


# ---- HTTP -------------------------------------------------------------- #
def _get(path: str, params: dict) -> dict:
    """GET with backoff. Purple Dot doesn't document rate limits, so 429/5xx
    are treated as transient and retried; 401/403 are treated as fatal (wrong
    token - the PUBLIC API key will not authenticate the private API)."""
    token = os.environ["PURPLE_DOT_ACCESS_TOKEN"]
    last_error = None
    for attempt in range(12):
        try:
            resp = requests.get(
                f"{BASE}{path}", params=params,
                headers={"X-Purple-Dot-Access-Token": token,
                         "Accept": "application/json"},
                timeout=90, allow_redirects=False)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = f"ConnectionError/Timeout: {e}"
            time.sleep(min(60, 5 * (attempt + 1)))
            continue
        if resp.status_code == 429:
            last_error = "429 rate limit"
            time.sleep(float(resp.headers.get("Retry-After", 30)))
            continue
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Purple Dot {resp.status_code} on {path}: access token rejected. "
                "Confirm PURPLE_DOT_ACCESS_TOKEN is the PRIVATE API access token "
                "(the public API key will not authenticate), or rotate it in the "
                "merchant portal's API Keys page.")
        if resp.status_code in (301, 302, 303, 307, 308):
            # A redirect to /admin/login means the path is wrong (e.g. the
            # underscore spelling of fulfillment-orders), not an auth problem.
            raise RuntimeError(
                f"Purple Dot {path} redirected to {resp.headers.get('location')!r} - "
                "wrong endpoint path for the private API.")
        if resp.status_code >= 500:
            last_error = f"{resp.status_code} server error"
            time.sleep(min(60, 10 * (attempt + 1)))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Purple Dot {path} {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if payload.get("meta", {}).get("result") not in (None, "success"):
            raise RuntimeError(f"Purple Dot {path} returned meta={payload.get('meta')}")
        return payload["data"]
    raise RuntimeError(f"Purple Dot {path} kept failing after retries. Last error: {last_error}")


def _count(**filters) -> int | None:
    try:
        return _get("/pre-orders/count", filters).get("count")
    except Exception as e:  # noqa: BLE001 - a count is nice-to-have, never fatal
        print(f"  (count unavailable: {e})")
        return None


# ---- state --------------------------------------------------------------- #
def _load_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM purple_dot_sync_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _save_state(conn, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO purple_dot_sync_state (key, value, updated_at) VALUES (?,?,?)",
            (key, value, datetime.now(timezone.utc).isoformat(timespec="seconds")))


# ---- row building ---------------------------------------------------------
def _num(v) -> float | None:
    return None if v is None else float(v)


def _preorder_rows(o: dict, stamp: str) -> tuple[tuple, list[tuple], list[tuple]]:
    items = o.get("line_items") or []
    addr = o.get("shipping_address") or {}
    exports = o.get("exported_orders") or []
    first_export = exports[0] if exports else {}
    order_row = (
        o.get("id"),
        o.get("order_number"),
        o.get("reference"),
        o.get("created_at"),
        o.get("placed_at"),
        o.get("cancelled_at"),
        o.get("cancel_reason"),
        o.get("currency"),
        (o.get("customer") or {}).get("external_id"),
        _num(o.get("subtotal_price")),
        _num(o.get("total_discounts")),
        _num(o.get("total_tax")),
        _num(o.get("total_price")),
        _num(o.get("total_refunded")),
        1 if o.get("tax_included") else 0,
        json.dumps(o.get("discount_codes") or []),
        addr.get("city"),
        addr.get("province_code"),
        addr.get("country_code"),
        len(items),
        sum(int(li.get("quantity") or 0) for li in items),
        # str() so this always matches purple_dot_preorder_exports.shopify_order_id
        # byte-for-byte, in case the API ever returns a number instead of a string.
        str(first_export["id"]) if first_export.get("id") is not None else None,
        first_export.get("order_number"),
        first_export.get("created_at"),
        len(exports),
        stamp,
    )
    line_rows = [(
        o.get("id"),
        li.get("id"),
        i,
        li.get("sku"),
        li.get("product_id"),
        li.get("variant_id"),
        li.get("name"),
        int(li.get("quantity") or 0),
        _num(li.get("unit_price")),
        _num(li.get("unit_total")),
        _num(li.get("price")),
        _num(li.get("total_discount")),
        _num(li.get("total")),
        1 if li.get("taxable") else 0,
        li.get("earliest_ship_date"),
        li.get("latest_ship_date"),
        li.get("waitlist_id"),
        1 if li.get("cancelled") else 0,
        li.get("cancelled_at"),
        li.get("shopify_line_item_id"),
        stamp,
    ) for i, li in enumerate(items) if li.get("id")]
    export_rows = [(
        o.get("id"),
        str(ex.get("id")),
        i,
        ex.get("order_number"),
        ex.get("type"),
        ex.get("created_at"),
        len(ex.get("line_items") or []),
        stamp,
    ) for i, ex in enumerate(exports) if ex.get("id") is not None]
    return order_row, line_rows, export_rows


def _waitlist_rows(w: dict, snapshot: str, stamp: str) -> tuple[tuple, list[tuple]]:
    avail = w.get("availability") or {}
    prod = avail.get("product") or {}
    variants = avail.get("variants") or []
    wl_row = (
        w.get("id"),
        w.get("created_at"),
        w.get("updated_at"),
        w.get("state"),
        w.get("earliest_ship_date"),
        w.get("latest_ship_date"),
        w.get("launch_date"),
        w.get("scheduled_pause_date"),
        json.dumps(w.get("labels") or []),
        prod.get("product_id"),
        prod.get("buy_size"),
        prod.get("committed"),
        prod.get("available"),
        len(variants),
        stamp,
    )
    inv_rows = [(
        snapshot,
        w.get("id"),
        str(v.get("variant_id")),
        v.get("sku"),
        w.get("state"),
        v.get("buy_size"),
        v.get("committed"),
        v.get("available"),
        stamp,
    ) for v in variants if v.get("variant_id") is not None]
    return wl_row, inv_rows


_PREORDER_SQL = f"""INSERT OR REPLACE INTO purple_dot_preorders VALUES ({','.join('?' * 26)})"""
_LINE_SQL = f"""INSERT OR REPLACE INTO purple_dot_preorder_lines VALUES ({','.join('?' * 21)})"""
_EXPORT_SQL = f"""INSERT OR REPLACE INTO purple_dot_preorder_exports VALUES ({','.join('?' * 8)})"""
_WAITLIST_SQL = f"""INSERT OR REPLACE INTO purple_dot_waitlists VALUES ({','.join('?' * 15)})"""
_WLINV_SQL = f"""INSERT OR REPLACE INTO purple_dot_waitlist_inventory VALUES ({','.join('?' * 9)})"""


# ---- paging ---------------------------------------------------------------
def _iter_pages(path: str, key: str, page_size: int, max_pages: int,
                start_cursor: str | None = None, **filters):
    """Yield (records, next_cursor) walking a Purple Dot list endpoint
    forward via its `starting_after` cursor."""
    cursor = start_cursor
    for _ in range(max_pages):
        params = {"limit": page_size, **filters}
        if cursor:
            params["starting_after"] = cursor
        data = _get(path, params)
        records = data.get(key) or []
        if not records:
            return
        nxt = data.get("starting_after") if data.get("has_more") else None
        yield records, nxt
        if not nxt:
            return
        cursor = nxt
        time.sleep(0.1)


# ---- pre-orders -----------------------------------------------------------
def sync_preorders(conn, *, backfill: bool, restart: bool,
                   days: int | None, max_pages: int) -> int:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    filters: dict = {}
    start_cursor = None
    mode = ""

    if backfill:
        if restart:
            with conn:
                conn.execute("DELETE FROM purple_dot_sync_state WHERE key=?", (CURSOR_KEY,))
        start_cursor = _load_state(conn, CURSOR_KEY)
        mode = f"BACKFILL {'resuming from stored cursor' if start_cursor else 'from feed start'}"
    else:
        if days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=days)
        else:
            high = _load_state(conn, HIGH_WATER_KEY)
            if high:
                since = datetime.fromisoformat(high) - timedelta(hours=OVERLAP_HOURS)
            else:
                since = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)
                print(f"  no high-water mark yet - first run covers {FIRST_RUN_DAYS}d. "
                      "Run with --backfill for full history.")
        filters["updated_at_min"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        mode = f"INCREMENTAL updated_at_min={filters['updated_at_min']}"

    expected = _count(**filters)
    print(f"Purple Dot pre-orders - {mode}"
          + (f" ({expected:,} expected)" if expected is not None else ""))

    order_buf: list[tuple] = []
    line_buf: list[tuple] = []
    export_buf: list[tuple] = []
    n_orders = n_lines = n_exports = pages = 0
    pending_cursor: str | None = None

    def flush() -> None:
        nonlocal order_buf, line_buf, export_buf
        with conn:
            if order_buf:
                conn.executemany(_PREORDER_SQL, order_buf)
            if line_buf:
                conn.executemany(_LINE_SQL, line_buf)
            if export_buf:
                conn.executemany(_EXPORT_SQL, export_buf)
        order_buf, line_buf, export_buf = [], [], []
        # Only the backfill cursor is resumable; the incremental run is
        # re-derived from the high-water mark instead.
        if backfill and pending_cursor:
            _save_state(conn, CURSOR_KEY, pending_cursor)

    try:
        for records, nxt in _iter_pages("/pre-orders", "orders", PREORDER_PAGE,
                                        max_pages, start_cursor, **filters):
            pages += 1
            for o in records:
                if not o.get("id"):
                    continue
                orow, lrows, erows = _preorder_rows(o, stamp)
                order_buf.append(orow)
                line_buf.extend(lrows)
                export_buf.extend(erows)
                n_orders += 1
                n_lines += len(lrows)
                n_exports += len(erows)
            pending_cursor = nxt
            if len(order_buf) >= CHECKPOINT_EVERY:
                flush()
                print(f"  ...{n_orders:,} pre-orders / {n_lines:,} lines over {pages:,} pages")
    finally:
        # Never lose work already fetched, even if a later page dies.
        flush()

    if not backfill:
        # High-water = when this run started, so the next run's overlap
        # window covers anything updated while it was running.
        _save_state(conn, HIGH_WATER_KEY, stamp)

    print(f"Purple Dot pre-orders: {n_orders:,} bookings, {n_lines:,} lines, "
          f"{n_exports:,} storefront exports, {pages:,} pages.")
    if expected is not None and n_orders < expected:
        note = f"  NOTE: fetched {n_orders:,} of {expected:,} reported"
        print(note + (" - re-run to continue (cursor saved)." if backfill else "."))
    return n_orders


# ---- waitlists ------------------------------------------------------------
def sync_waitlists(conn, max_pages: int, snapshot_all: bool = False) -> int:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot = datetime.now(timezone.utc).date().isoformat()
    print("Purple Dot waitlists - current state for all; allocation snapshot for "
          + ("ALL states" if snapshot_all else f"{'/'.join(sorted(SNAPSHOT_STATES))} only"))

    wl_buf: list[tuple] = []
    inv_buf: list[tuple] = []
    n_wl = n_inv = n_snap = pages = 0

    def flush() -> None:
        nonlocal wl_buf, inv_buf
        with conn:
            if wl_buf:
                conn.executemany(_WAITLIST_SQL, wl_buf)
            if inv_buf:
                conn.executemany(_WLINV_SQL, inv_buf)
        wl_buf, inv_buf = [], []

    try:
        for records, _nxt in _iter_pages("/waitlists", "waitlists", WAITLIST_PAGE, max_pages):
            pages += 1
            for w in records:
                if not w.get("id"):
                    continue
                wrow, irows = _waitlist_rows(w, snapshot, stamp)
                wl_buf.append(wrow)
                n_wl += 1
                if snapshot_all or (w.get("state") or "").lower() in SNAPSHOT_STATES:
                    inv_buf.extend(irows)
                    n_inv += len(irows)
                    n_snap += 1
            if len(wl_buf) >= CHECKPOINT_EVERY:
                flush()
                print(f"  ...{n_wl:,} waitlists over {pages:,} pages")
    finally:
        flush()

    print(f"Purple Dot waitlists: {n_wl:,} waitlists (current state), "
          f"{n_inv:,} variant allocations from {n_snap:,} active waitlists "
          f"(snapshot {snapshot}), {pages:,} pages.")
    return n_wl


# ---- main -----------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Sync Purple Dot pre-orders + waitlists.")
    p.add_argument("--backfill", action="store_true",
                   help="resumable full crawl of all pre-orders from the feed start")
    p.add_argument("--restart", action="store_true",
                   help="with --backfill: discard the stored cursor and start over")
    p.add_argument("--days", type=int,
                   help="incremental window: re-pull anything updated in the last N days")
    p.add_argument("--only", choices=("preorders", "waitlists"),
                   help="run just one grain (default: both)")
    p.add_argument("--snapshot-all", action="store_true",
                   help="snapshot allocation for every waitlist state, not just "
                        f"{'/'.join(sorted(SNAPSHOT_STATES))}")
    p.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES,
                   help="cap pages fetched per grain this run")
    args = p.parse_args()

    if not os.environ.get("PURPLE_DOT_ACCESS_TOKEN"):
        print("PURPLE_DOT_ACCESS_TOKEN not set - skipping Purple Dot sync.")
        return

    db.init_db()
    conn = db.connect()
    with conn:
        ensure_schema(conn)

    failures: list[str] = []

    if args.only != "waitlists":
        started = db.now()
        try:
            n = sync_preorders(conn, backfill=args.backfill, restart=args.restart,
                               days=args.days, max_pages=args.pages)
            db.log_sync("purple_dot", started, n, "ok")
        except Exception as e:  # noqa: BLE001
            db.log_sync("purple_dot", started, 0, "error", str(e))
            print(f"Purple Dot pre-orders ERROR: {e}")
            failures.append("preorders")

    if args.only != "preorders":
        started = db.now()
        try:
            n = sync_waitlists(conn, args.pages, snapshot_all=args.snapshot_all)
            db.log_sync("purple_dot_waitlists", started, n, "ok")
        except Exception as e:  # noqa: BLE001
            db.log_sync("purple_dot_waitlists", started, 0, "error", str(e))
            print(f"Purple Dot waitlists ERROR: {e}")
            failures.append("waitlists")

    conn.close()
    if failures:
        raise SystemExit("Purple Dot sync failures: " + ", ".join(failures))


if __name__ == "__main__":
    main()
