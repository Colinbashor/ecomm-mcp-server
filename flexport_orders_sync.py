r"""
Flexport Logistics (3PL) connector — per-order OUTBOUND SHIPPING COST.

Separate from flexport_sync.py (catalog + inventory). This one lands the
realized cost Flexport billed to fulfil each outbound order — useful for
landed-cost / margin work that only has an estimated shipping number today.

Two tables:
  flexport_order_costs    — one row per order. `cost` is the order's all-in
                            realized outbound shipping/fulfillment cost (a
                            single scalar covering the whole order, which may
                            span several shipments/packages/carriers).
  flexport_order_packages — one row per package: carrier, shipping method,
                            weight, dims, tracking code — raw material for
                            per-pound benchmarks or per-SKU cost allocation.

THE HARD PART OF THIS CONNECTOR IS DISCOVERY, NOT RETRIEVAL. There is no
list-all `/orders` endpoint. Order ids can only be discovered by walking an
event feed and reading them off delivery-related events, then each order's
detail (including cost) is fetched individually:

  1. GET /events?type=Shipment.Created&limit=100 — each event's payload
     carries an orderId (+ your own external/merchant order id). A single
     order commonly fans out into several Shipment.Created events (one per
     split shipment), so order ids must be de-duplicated as you go.
  2. GET /orders/{orderId} — returns cost, status, timestamps, line items,
     and shipments[] -> packages[] (carrier, shipping method, dimensions).

PAGINATION — USE THE CURSOR IN THE RESPONSE HEADER, NOT `offset`. This is the
single most important gotcha in this file and it generalizes past Flexport:
plenty of vendor list APIs advertise an `offset` parameter that LOOKS fine for
shallow paging but silently stops making progress once you page deep enough
(a frozen/repeating page, or a hard cap on how far offset can reach). Verify
deep pagination against a real cursor before trusting `offset` on any new
endpoint. Concretely, on this endpoint:
  * `offset` paging caps out after enough pages — past that point every
    request returns the same frozen slice of the oldest events, so an
    offset-based walk can only ever reach a few days of history no matter how
    far you page.
  * Date-range filters on this endpoint (start/end/since/after style
    parameters) are silently IGNORED — don't rely on them to window a pull.
  * The CORRECT mechanism is the `Link: <...page_info=...>; rel="next"`
    response header, a pattern you'll recognize from GitHub's and Shopify's
    REST APIs. `page_info` is an opaque cursor; following it walks forward
    through time with no depth cap. Extract just the cursor value and pass it
    back as a `page_info` query parameter on the next request.
  * `limit` maxes out around 100 on this endpoint (asking for more 400s).
    Latency per page is roughly flat regardless of page size, so always page
    at the max.

DISCOVERY HAS A RETENTION CEILING; RETRIEVAL DOES NOT — an important and
easy-to-miss distinction. The event feed used for DISCOVERING order ids only
retains roughly the trailing 12 months — craft a cursor pointed further back
and it silently snaps forward to the feed's actual floor instead of erroring.
But an order id you already know (because you fetched it before the floor
moved, or got it from an export/webhook/another system) can still be fetched
directly via GET /orders/{id} indefinitely — the floor is on *discovering*
ids via the event feed, not on the underlying order records themselves. If
you need cost data older than the event feed's retention window, you need the
order ids from somewhere else (a CSV export, a webhook log you kept, etc.) —
this connector's crawl alone cannot reach them.

The crawl is resumable: the last page_info cursor is persisted after each
batch of orders is flushed, so a killed run continues near where it left off
instead of restarting. `--restart` discards the stored cursor and starts the
walk over from the feed floor.

A CURSOR CAN GO PERMANENTLY BAD — SELF-HEAL BY TRACKING A TIMESTAMP TOO. In
production this has been observed more than once: a *stored* page_info value
starts 500ing on every retry while a *freshly issued* cursor 200s within the
same minute — the cursor value itself died, not the feed. An opaque cursor
that goes bad that way is unrecoverable on its own, so this connector tracks
position a second way, as a plain timestamp read off each page's own event
data (`events_last_time`, alongside the `page_info` cursor). On a
rejected/exhausted cursor it re-crafts a fresh one from that timestamp via
`page_info_at()` and keeps walking in the same run (bounded by
`MAX_RECRAFTS`), rather than surfacing a failure that needs a human to notice
and clear. This generalizes past Flexport: for any paginated feed whose only
recoverable position signal is a value embedded in the page contents, track
that value defensively even while the primary cursor is healthy — you only
find out you need it once the primary cursor is already dead.

Three refinements worth calling out because they were each found the hard
way, and the failure mode in each case is silent (the crawl looks fine, it
just makes little or no progress):
  * Re-crafting at the IDENTICAL timestamp on every retry assumes the
    badness is transient — but whatever makes a position bad is often
    sticky, so retrying the same spot can fail every attempt and burn the
    whole recraft budget for nothing. `RECRAFT_NUDGE_MINUTES` steps the
    re-craft position forward a little more on each attempt instead.
  * A resume seed sized for an initial historical backfill ("N days back")
    is usually wrong once that backfill is done and you're just keeping the
    tip current — it can land the crawl re-walking a long stretch of orders
    you already have. `frontier_seed()` derives the seed from wherever the
    stored data actually ends instead of a fixed window.
  * A checkpoint trigger keyed only on "found something new" can starve
    during a legitimate no-new-data phase (e.g. re-walking the overlap
    window above the frontier seed) — a kill mid-run then discards all
    cursor progress even though many pages were successfully walked.
    `CHECKPOINT_PAGES` adds a second, page-count-based trigger so position is
    still saved during a quiet stretch.

Auth: bearer token in FLEXPORT_API_TOKEN (.env), same token as flexport_sync.py.
Skips cleanly if unset.

USAGE:
  python flexport_orders_sync.py                  # resume: walk forward from the stored cursor to the feed tip
  python flexport_orders_sync.py --since-days 30   # seed a cursor ~30 days back and walk forward from there
  python flexport_orders_sync.py --pages 200       # cap this run at 200 event pages (safety bound)
  python flexport_orders_sync.py --restart         # ignore the stored cursor; crawl from the feed floor
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from warehouse import db

load_dotenv()

BASE = "https://logistics-api.flexport.com/logistics/api/2024-06"
EVENTS_PAGE = 100          # this endpoint 400s at >=250; 100 is a safe max
SHIPMENT_EVENT = "Shipment.Created"
DEFAULT_MAX_PAGES = 100_000   # runaway safety bound only, not a real expectation
ORDER_FETCH_WORKERS = 8    # per-order detail fetches are independent; run them concurrently
CURSOR_KEY = "events_page_info"
# Durable position as an event TIMESTAMP, kept alongside the opaque page_info
# cursor. See the self-healing note on iter_event_pages() for why this exists.
LAST_TIME_KEY = "events_last_time"
MAX_RECRAFTS = 3   # bound on same-run cursor re-crafts; each one pays a full retry budget
# How far to step forward per re-craft attempt when a cursor position is
# rejected, instead of re-crafting at the identical timestamp. Whatever makes
# a position bad tends to be sticky — retrying the same spot can fail every
# time, burning the whole recraft budget for zero progress. Stepping forward
# clears the bad region at the cost of a small, bounded gap; small enough to
# lose little, big enough to actually move past the problem.
RECRAFT_NUDGE_MINUTES = 15
# When resuming with no validated cursor, re-seed from wherever the STORED
# DATA actually ends (see frontier_seed()) rather than a fixed "N days ago"
# window. Size this in HOURS, not days: on a feed where a single page covers
# only a couple of minutes, a multi-day overlap can mean thousands of pages
# re-reading orders you already have before reaching anything new. A couple
# of hours of overlap is cheap insurance against a boundary gap; re-reads are
# harmless as long as writes are idempotent (INSERT OR REPLACE).
FRONTIER_OVERLAP_HOURS = 2
# Persist the cursor at least this often in PAGES, independent of how many
# new orders were found. A pure resume walk can spend long stretches
# re-reading pages with nothing new on them (see FRONTIER_OVERLAP_HOURS); a
# checkpoint trigger keyed only on "found something new" never fires during
# exactly that phase, so a kill mid-walk can discard all cursor progress.
CHECKPOINT_PAGES = 25
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # ULID base32 alphabet

DDL = """
CREATE TABLE IF NOT EXISTS flexport_order_costs (
    order_id           INTEGER NOT NULL,   -- Flexport's own order id
    external_order_id  TEXT,               -- your own order id, if the vendor echoes it back
    cost               REAL,               -- all-in realized outbound cost (NULL if unshipped)
    currency           TEXT,
    internal_status    TEXT,
    fulfillment_status TEXT,
    created_at         TEXT,
    shipped_at         TEXT,
    delivered_at       TEXT,
    units              INTEGER DEFAULT 0,  -- sum of line-item quantities
    n_shipments        INTEGER DEFAULT 0,
    n_packages         INTEGER DEFAULT 0,
    total_weight_oz    REAL DEFAULT 0,     -- sum of package weights, normalized to oz
    carriers           TEXT,               -- comma-joined distinct
    shipping_methods   TEXT,               -- comma-joined distinct
    is_international   INTEGER DEFAULT 0,
    synced_at          TEXT NOT NULL,
    PRIMARY KEY (order_id)
);
CREATE INDEX IF NOT EXISTS idx_flexport_order_costs_ext ON flexport_order_costs(external_order_id);

CREATE TABLE IF NOT EXISTS flexport_order_packages (
    order_id        INTEGER NOT NULL,
    shipment_id     INTEGER,
    package_id      TEXT NOT NULL,
    warehouse_id    TEXT,
    carrier         TEXT,
    shipping_method TEXT,
    tracking_code   TEXT,
    weight_oz       REAL,
    length_in       REAL,
    width_in        REAL,
    height_in       REAL,
    logistics_skus  TEXT,                  -- comma-joined skus in this package
    synced_at       TEXT NOT NULL,
    PRIMARY KEY (order_id, package_id)
);
CREATE INDEX IF NOT EXISTS idx_flexport_order_packages_order ON flexport_order_packages(order_id);

-- Resumable-crawl bookmark: the last page_info cursor whose orders are fully
-- flushed, so an interrupted crawl continues where it stopped.
CREATE TABLE IF NOT EXISTS flexport_order_sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


class FlexportBadCursor(RuntimeError):
    """The event feed rejected the page_info cursor (HTTP 400)."""


class FlexportTransient(RuntimeError):
    """Transient (5xx/connection/429) failures exhausted every retry. Distinct
    from a hard error (401/other 4xx) so the caller can pause gracefully —
    checkpoint what's committed and exit — rather than crash losing progress.
    A resumable crawl's whole point is that a vendor-side degradation window
    is a pause, not a failure."""


_MAX_RETRIES = 12
_BACKOFF_CAP = 90


def _request(path: str, params: dict) -> requests.Response:
    """GET with resilience: connection blips and 429/5xx are retried with
    exponential backoff; 401/other 4xx are hard-fatal; retry exhaustion raises
    FlexportTransient so the caller can pause-and-resume instead of crashing.
    Returns the Response so callers can read the Link cursor header."""
    token = os.environ["FLEXPORT_API_TOKEN"]
    last_error = None
    bad_cursor = False
    for attempt in range(_MAX_RETRIES):
        backoff = min(10 * (2 ** min(attempt, 5)), _BACKOFF_CAP)
        try:
            resp = requests.get(f"{BASE}{path}", params=params,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/json"}, timeout=90)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = f"connection error: {e}"
            time.sleep(backoff)
            continue
        if resp.status_code == 429:
            last_error = "429 rate limited"
            time.sleep(float(resp.headers.get("Retry-After", backoff)))
            continue
        if resp.status_code == 401:
            raise RuntimeError("Flexport 401: token expired or revoked — "
                               "merchant tokens last about a year; get a new one from the portal.")
        if resp.status_code >= 500:
            last_error = f"{resp.status_code} server error"
            time.sleep(backoff)
            continue
        if resp.status_code == 400 and path == "/events":
            # A 400 here can be transient load-shedding rather than a genuinely
            # bad cursor (a request that fails now may succeed moments later
            # with the SAME cursor). Back off like a 5xx; only treat it as a
            # bad cursor once the whole retry budget is exhausted below.
            last_error = f"400 (rejected cursor or load-shedding): {resp.text[:120]}"
            bad_cursor = True
            time.sleep(backoff)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Flexport {path} {resp.status_code}: {resp.text[:200]}")
        return resp
    if bad_cursor:
        raise FlexportBadCursor(f"Flexport {path} kept 400ing. Last error: {last_error}")
    raise FlexportTransient(f"Flexport {path} kept failing after retries. Last error: {last_error}")


_NEXT_RE = re.compile(r'page_info=([^&>]+)[^>]*>\s*;\s*rel="next"')


def next_page_info(resp: requests.Response) -> str | None:
    """Extract the `page_info` cursor from the `Link: <...>; rel="next"`
    header. Returns None once the feed has no further pages (tip reached)."""
    link = resp.headers.get("link") or resp.headers.get("Link") or ""
    m = _NEXT_RE.search(link)
    return m.group(1) if m else None


def ulid_at(dt: datetime, tail: str = "0") -> str:
    """A 26-char ULID whose leading bits encode `dt` as milliseconds since the
    epoch, with the trailing randomness filled by `tail`. Some vendor cursor
    schemes (this one included) are time-sortable ULIDs under the hood, which
    means you can hand-craft a cursor at an arbitrary point in time instead of
    only ever being able to resume from a cursor the API gave you — handy for
    seeding a crawl at "N days ago" without grinding forward from the floor."""
    ms = int(dt.timestamp() * 1000)
    head = ""
    for _ in range(10):
        head = _CROCKFORD[ms & 31] + head
        ms >>= 5
    return head + tail * 16


def page_info_at(dt: datetime) -> str:
    """Best-effort crafted page_info cursor seeded at `dt`. The exact cursor
    envelope (a JSON blob with a type/cursor/direction, base64'd) is specific
    to this API; adapt the shape to whatever your vendor's Link header
    actually emits if you reuse this technique elsewhere."""
    import base64
    import json
    raw = json.dumps({"type": SHIPMENT_EVENT, "cursor": ulid_at(dt), "direction": "forward"})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def iter_event_pages(start_page_info: str | None, max_pages: int,
                     last_time: str | None = None):
    """Yield (order_ids_in_page, next_page_info, last_event_time), walking
    Shipment.Created events forward via the Link cursor. `order_ids_in_page`
    is de-duplicated within the page. Stops at the feed tip (no next cursor),
    an empty page, or max_pages (a runaway guard).

    SELF-HEALS A DEAD CURSOR. A specific page_info value can go permanently
    bad on the vendor's side even though the feed itself is healthy — this
    connector has hit it more than once: a *stored* cursor returning 500 on
    every retry while a *freshly issued* cursor 200'd within the same minute.
    `_request` already retries transient failures, but retrying the same dead
    cursor forever can't recover — the value itself is the problem.

    The fix generalizes past Flexport: track position a SECOND way, as a
    timestamp pulled from the page contents (here, each event's `time`
    field), not just via the opaque cursor. An opaque cursor that goes bad is
    unrecoverable on its own, but a timestamp can always be re-minted into a
    fresh cursor via `page_info_at()`. So on a rejected/exhausted cursor this
    re-crafts one from the last known timestamp and keeps walking in the same
    run, instead of surfacing the failure and waiting for the next scheduled
    run (or a human) to notice and clear it. Bounded by MAX_RECRAFTS, and only
    attempted when a timestamp is already known — on a cold start with no
    prior position in either dimension, a bad cursor still raises, because
    re-crafting from nothing would silently restart the walk at an arbitrary
    point rather than a genuine resume."""
    params = ({"limit": EVENTS_PAGE, "page_info": start_page_info}
              if start_page_info else
              {"limit": EVENTS_PAGE, "type": SHIPMENT_EVENT})
    recrafts = 0
    for _ in range(max_pages):
        try:
            resp = _request("/events", params)
        except (FlexportBadCursor, FlexportTransient):
            if not last_time or recrafts >= MAX_RECRAFTS:
                raise
            recrafts += 1
            # NUDGE the position forward each attempt rather than re-crafting
            # at the identical timestamp. Retrying the exact same spot assumes
            # the badness is transient when it may not be — a cursor that just
            # died there will die there again, spending the whole recraft
            # budget for zero progress before the run gives up and pauses.
            nudge = RECRAFT_NUDGE_MINUTES * recrafts
            pos = (datetime.strptime(last_time[:19], "%Y-%m-%dT%H:%M:%S")
                   .replace(tzinfo=timezone.utc) + timedelta(minutes=nudge))
            print(f"  cursor rejected — re-crafting at {last_time} +{nudge}min "
                  f"(recraft {recrafts}/{MAX_RECRAFTS})", flush=True)
            params = {"limit": EVENTS_PAGE, "page_info": page_info_at(pos)}
            continue
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            return
        ids: list[int] = []
        seen: set[int] = set()
        for e in batch:
            if not isinstance(e, dict):
                continue
            t = e.get("time") or ""
            if t:
                last_time = t[:19]
            oid = (e.get("payload") or {}).get("orderId")
            if isinstance(oid, int) and oid not in seen:
                seen.add(oid)
                ids.append(oid)
        nxt = next_page_info(resp)
        yield ids, nxt, last_time
        if not nxt:
            return
        params = {"limit": EVENTS_PAGE, "page_info": nxt}
        time.sleep(0.1)


def _load_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM flexport_order_sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _save_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO flexport_order_sync_state (key, value, updated_at) VALUES (?,?,?)",
            (key, value, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def _clear_state(conn: sqlite3.Connection, key: str) -> None:
    with conn:
        conn.execute("DELETE FROM flexport_order_sync_state WHERE key = ?", (key,))


def _load_cursor(conn: sqlite3.Connection) -> str | None:
    return _load_state(conn, CURSOR_KEY)


def _save_cursor(conn: sqlite3.Connection, page_info: str) -> None:
    _save_state(conn, CURSOR_KEY, page_info)


def _clear_cursor(conn: sqlite3.Connection) -> None:
    _clear_state(conn, CURSOR_KEY)


def frontier_time(conn: sqlite3.Connection,
                  overlap_hours: int = FRONTIER_OVERLAP_HOURS) -> str | None:
    """The frontier position as a naive-ISO string, for the last_time bookmark.
    None if the table is empty (nothing to derive a frontier from yet)."""
    row = conn.execute("SELECT MAX(created_at) FROM flexport_order_costs").fetchone()
    if not row or not row[0]:
        return None
    try:
        newest = datetime.strptime(row[0][:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (newest - timedelta(hours=overlap_hours)).strftime("%Y-%m-%dT%H:%M:%S")


def frontier_seed(conn: sqlite3.Connection,
                  overlap_hours: int = FRONTIER_OVERLAP_HOURS) -> str | None:
    """A crafted cursor positioned just before the newest order already stored.

    Used when there is no validated cursor to resume from (a fresh deployment
    past its first run, or a cursor the poison guard just discarded). A fixed
    "N days ago" seed is right for an initial historical backfill but wrong
    for keeping the tip current once that backfill is done — it can land the
    crawl walking pages of orders you already have for a long stretch before
    reaching anything new. Deriving the seed from wherever the DATA actually
    ends (whatever put it there — a prior crawl, an out-of-band import, a long
    outage) always resumes in the right place. A small overlap re-reads a
    little rather than risking a boundary gap; already-stored ids are skipped
    on resume anyway."""
    row = conn.execute("SELECT MAX(created_at) FROM flexport_order_costs").fetchone()
    if not row or not row[0]:
        return None
    try:
        newest = datetime.strptime(row[0][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return page_info_at(newest - timedelta(hours=overlap_hours))


def existing_order_ids(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT order_id FROM flexport_order_costs")}


def fetch_order(oid: int) -> tuple[int, dict | None]:
    """Fetch one order's detail for the parallel pool. Never raises — a failed
    fetch returns (oid, None) so the page's other orders still land."""
    try:
        o = _request(f"/orders/{oid}", {}).json()
    except RuntimeError as e:
        print(f"  order {oid} failed: {e}")
        return oid, None
    return oid, (o if isinstance(o, dict) and "id" in o else None)


def _to_oz(weight, unit: str | None) -> float:
    if not weight:
        return 0.0
    return float(weight) if (unit or "oz").lower() == "oz" else float(weight) * 16.0


def rows_for_order(o: dict, stamp: str) -> tuple[tuple, list[tuple]]:
    state = o.get("state") or {}
    units = sum(int(li.get("quantity", 0) or 0) for li in (o.get("lineItems") or []))
    carriers: dict[str, None] = {}
    methods: dict[str, None] = {}
    total_oz = 0.0
    n_pkgs = 0
    intl = 0
    pkg_rows: list[tuple] = []
    shipments = o.get("shipments") or []
    for s in shipments:
        for pk in (s.get("packages") or []):
            n_pkgs += 1
            lab = pk.get("label") or {}
            dims = lab.get("packageDimensions") or {}
            carrier = lab.get("carrier")
            method = lab.get("shippingMethod")
            if carrier:
                carriers.setdefault(carrier, None)
            if method:
                methods.setdefault(method, None)
            m = (method or "").upper()
            if (carrier or "").upper() == "PASSPORT" or "DDU" in m or "DDP" in m:
                intl = 1
            oz = _to_oz(dims.get("weight"), dims.get("weightUnit"))
            total_oz += oz
            skus = ",".join(li.get("logisticsSku", "") for li in (pk.get("lineItems") or []))
            pkg_rows.append((
                o.get("id"), s.get("id"), str(pk.get("id") or ""), s.get("warehouseId"),
                carrier, method, lab.get("trackingCode"),
                round(oz, 2) if oz else None,
                dims.get("length"), dims.get("width"), dims.get("height"),
                skus, stamp,
            ))
    cost = o.get("cost")
    order_row = (
        o.get("id"), o.get("externalOrderId"),
        float(cost) if isinstance(cost, (int, float)) else None,
        o.get("currency") or ("USD" if cost is not None else None),
        state.get("internalStatus"), state.get("fulfillmentStatus"),
        o.get("createdAt"), o.get("shippedAt"), o.get("deliveredAt"),
        units, len(shipments), n_pkgs, round(total_oz, 2),
        ",".join(carriers) or None, ",".join(methods) or None, intl, stamp,
    )
    return order_row, pkg_rows


def _flush(conn: sqlite3.Connection, order_rows: list[tuple], pkg_rows: list[tuple]) -> None:
    with conn:
        if order_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO flexport_order_costs VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", order_rows)
        if pkg_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO flexport_order_packages VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", pkg_rows)


def run(conn: sqlite3.Connection, *, restart: bool, since_days: int | None,
        max_pages: int) -> tuple[int, int, bool]:
    """Crawl forward from the resume point, writing orders as they're found.
    Returns (n_new_orders, n_pages_walked, paused). `paused=True` means a
    transient failure stopped the run early but everything found so far is
    committed and the cursor is saved for a later resume."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if since_days is not None:
        start_cursor = page_info_at(datetime.now(timezone.utc) - timedelta(days=since_days))
        _clear_cursor(conn)
        mode = f"seed at ~{since_days} days ago"
    elif restart:
        _clear_cursor(conn)
        start_cursor = None
        mode = "restart from feed floor"
    else:
        start_cursor = _load_cursor(conn)
        if start_cursor:
            mode = "resume from stored cursor"
        elif (frontier := frontier_seed(conn)) is not None:
            # No validated cursor, but we already hold some orders — resume at
            # the DATA FRONTIER instead of walking the whole feed from the floor.
            start_cursor = frontier
            mode = "no stored cursor — re-seed at the data frontier"
        else:
            mode = "fresh from feed floor"
    print(f"Flexport orders crawl — {mode}.")

    already = existing_order_ids(conn)
    order_rows: list[tuple] = []
    pkg_rows: list[tuple] = []
    pending_cursor: str | None = None
    last_time = _load_state(conn, LAST_TIME_KEY)
    if start_cursor and not last_time:
        # A frontier-seeded cursor needs its OWN last_time anchor: the stored
        # bookmark (if any) can be far older than the frontier, and re-crafting
        # off a stale value on the first rejected page would rewind the crawl.
        last_time = frontier_time(conn)
    n_orders = pages = 0

    def checkpoint() -> None:
        nonlocal order_rows, pkg_rows
        _flush(conn, order_rows, pkg_rows)
        order_rows, pkg_rows = [], []
        if pending_cursor:
            _save_cursor(conn, pending_cursor)
        if last_time:
            _save_state(conn, LAST_TIME_KEY, last_time)

    pool = ThreadPoolExecutor(max_workers=ORDER_FETCH_WORKERS)
    try:
        for ids, nxt, last_time in iter_event_pages(start_cursor, max_pages, last_time):
            pages += 1
            new_ids = [oid for oid in ids if oid not in already]
            already.update(new_ids)
            for oid, o in pool.map(fetch_order, new_ids):
                if o is None:
                    continue
                orow, prows = rows_for_order(o, stamp)
                order_rows.append(orow)
                pkg_rows.extend(prows)
                n_orders += 1
            pending_cursor = nxt
            # Checkpoint on EITHER 100 new orders OR every CHECKPOINT_PAGES
            # pages, whichever comes first. The page trigger matters when
            # resuming near the frontier: that walk can spend many pages
            # re-reading orders already stored (see FRONTIER_OVERLAP_HOURS),
            # and an order-count-only trigger never fires during that phase —
            # a kill mid-walk would then discard all cursor progress.
            if len(order_rows) >= 100 or pages % CHECKPOINT_PAGES == 0:
                checkpoint()
                print(f"  ...checkpointed: {n_orders} new orders, {pages} pages")
        checkpoint()
        return n_orders, pages, False
    except FlexportTransient as e:
        checkpoint()
        print(f"Flexport orders PAUSED after {n_orders} new orders / {pages} pages "
              f"— vendor backend degraded ({e}). Cursor saved; re-run to resume.")
        return n_orders, pages, True
    finally:
        pool.shutdown(wait=True)


def main() -> int:
    if not os.environ.get("FLEXPORT_API_TOKEN"):
        print("FLEXPORT_API_TOKEN not set — skipping Flexport order-cost sync.")
        return 0

    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES,
                   help="cap this run at N event pages")
    p.add_argument("--since-days", type=int, default=None,
                   help="seed the crawl ~N days back instead of resuming the stored cursor")
    p.add_argument("--restart", action="store_true",
                   help="ignore the stored cursor; crawl from the feed floor")
    args = p.parse_args()

    conn = db.connect()
    ensure_schema(conn)
    started = db.now()
    try:
        n_orders, pages, paused = run(conn, restart=args.restart,
                                      since_days=args.since_days, max_pages=args.pages)
        conn.close()
    except Exception as e:  # noqa: BLE001
        db.log_sync("flexport_orders", started, 0, "error", str(e))
        raise

    status = "degraded" if paused else "ok"
    db.log_sync("flexport_orders", started, n_orders, status,
               "paused, resumable" if paused else "")
    print(f"Flexport orders: {n_orders} new orders over {pages} event pages.")
    return 75 if paused else 0


if __name__ == "__main__":
    raise SystemExit(main())
