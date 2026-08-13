r"""
Flexport Logistics (3PL) connector — customer returns received by the 3PL.

The returns counterpart to flexport_orders_sync.py: if Flexport is the 3PL
that receives returned items back from your customers, GET /returns is the
return-rate / disposition signal (why items came back, what condition they
arrived in, whether they went back into sellable stock).

Two tables (one row per return, one row per returned line):
  flexport_returns       — status (CREATED/SHIPPED/RECEIVED/INSPECTED/
                           PROCESSED), rma, external_return_id, carrier +
                           tracking, source address (where the customer
                           shipped from), fulfillment_order_id (joins
                           flexport_order_costs.order_id when the vendor
                           supplies it — often null), shipped/received/
                           inspected timestamps.
  flexport_return_lines  — the returned SKU, expected_quantity, and once
                           inspected: received/final condition + disposition
                           (e.g. RESTOCK vs scrap). A line can have more than
                           one inspected item; we keep the FIRST inspection's
                           disposition on the line plus a count.

PAGINATION: same cursor mechanism as flexport_orders_sync.py — follow the
`Link: <...page_info=...>; rel="next"` response header rather than an
`offset` parameter (see that file's docstring for why `offset` is a trap on
list endpoints like this). This endpoint's cursor happens to be a plain
incrementing id rather than a time-sortable token, but the pagination
mechanics are identical. `limit` maxes out around 100.

Auth: bearer token in FLEXPORT_API_TOKEN (.env), same token as the other
Flexport connectors. Skips cleanly if unset.

Incremental by default (resumes from the stored cursor to the feed tip);
--restart re-crawls from the floor; --pages caps a single run.

USAGE:
  python flexport_returns_sync.py             # incremental resume to the feed tip
  python flexport_returns_sync.py --restart   # full re-crawl from the floor
  python flexport_returns_sync.py --pages 50  # cap this run
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from warehouse import db

load_dotenv()

BASE = "https://logistics-api.flexport.com/logistics/api/2024-06"
RETURNS_PAGE = 100
DEFAULT_MAX_PAGES = 100_000
CURSOR_KEY = "returns_page_info"

DDL = """
CREATE TABLE IF NOT EXISTS flexport_returns (
    id                   INTEGER PRIMARY KEY,   -- Flexport return id
    status               TEXT,                  -- CREATED/SHIPPED/RECEIVED/INSPECTED/PROCESSED
    rma                  TEXT,
    external_return_id   TEXT,
    fulfillment_order_id INTEGER,               -- joins flexport_order_costs.order_id (often NULL)
    carrier              TEXT,
    tracking_code        TEXT,
    tracking_status      TEXT,
    source_name          TEXT,
    source_city          TEXT,
    source_state         TEXT,
    source_zip           TEXT,
    source_country       TEXT,
    shipped_at           TEXT,
    received_at          TEXT,
    inspected_at         TEXT,
    n_lines              INTEGER,
    synced_at            TEXT
);
CREATE TABLE IF NOT EXISTS flexport_return_lines (
    return_id           INTEGER NOT NULL,
    line_no             INTEGER NOT NULL,
    identifier          TEXT,                  -- returned SKU
    expected_quantity   INTEGER,
    n_inspected         INTEGER,               -- how many inspected items on this line
    received_condition  TEXT,                  -- first inspection's condition (may be blank)
    final_condition     TEXT,
    disposition         TEXT,                  -- first inspection's disposition (RESTOCK / ...)
    synced_at           TEXT,
    PRIMARY KEY (return_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_flexport_return_lines_sku ON flexport_return_lines(identifier);
CREATE INDEX IF NOT EXISTS idx_flexport_returns_status ON flexport_returns(status);

CREATE TABLE IF NOT EXISTS flexport_returns_sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

_NEXT_RE = re.compile(r'page_info=([^&>]+)[^>]*>\s*;\s*rel="next"')


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _request(path: str, params: dict) -> requests.Response:
    """GET with the same resilience pattern used across the Flexport
    connectors: retries on connection blips/429/5xx, 401 is hard-fatal.
    Returns the Response so callers can read the Link cursor header."""
    token = os.environ["FLEXPORT_API_TOKEN"]
    last_error = None
    for attempt in range(12):
        backoff = min(10 * (2 ** min(attempt, 5)), 90)
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
            raise RuntimeError("Flexport 401: token expired or revoked — get a new one from the portal.")
        if resp.status_code >= 500:
            last_error = f"{resp.status_code} server error"
            time.sleep(backoff)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Flexport {path} {resp.status_code}: {resp.text[:200]}")
        return resp
    raise RuntimeError(f"Flexport {path} kept failing after retries. Last error: {last_error}")


def next_page_info(resp: requests.Response) -> str | None:
    link = resp.headers.get("link") or resp.headers.get("Link") or ""
    m = _NEXT_RE.search(link)
    return m.group(1) if m else None


def _load_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM flexport_returns_sync_state WHERE key = ?", (CURSOR_KEY,)).fetchone()
    return row[0] if row else None


def _save_cursor(conn: sqlite3.Connection, page_info: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO flexport_returns_sync_state (key, value, updated_at) VALUES (?,?,?)",
            (CURSOR_KEY, page_info, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def rows_for_return(rec: dict, stamp: str) -> tuple[tuple, list[tuple]]:
    lab = rec.get("shippingLabel") or {}
    addr = rec.get("sourceAddress") or {}
    items = rec.get("returnItems") or []
    ret_row = (
        rec.get("id"), rec.get("status"), rec.get("rma"), rec.get("externalReturnId"),
        rec.get("fulfillmentOrderId"),
        lab.get("carrier") or None, lab.get("trackingCode"), lab.get("trackingStatus"),
        addr.get("name"), addr.get("city"), addr.get("state"), addr.get("zip"), addr.get("country"),
        rec.get("shippedAt"), rec.get("receivedAt"), rec.get("inspectedAt"),
        len(items), stamp,
    )
    line_rows: list[tuple] = []
    for i, it in enumerate(items):
        inspected = it.get("inspectedItems") or []
        first = inspected[0] if inspected else {}
        line_rows.append((
            rec.get("id"), i, it.get("identifier"), it.get("expectedQuantity"),
            len(inspected),
            first.get("receivedCondition") or None,
            first.get("finalCondition") or None,
            first.get("disposition") or None,
            stamp,
        ))
    return ret_row, line_rows


def iter_return_pages(start_page_info: str | None, max_pages: int):
    """Yield (records, next_page_info), walking the returns feed forward via
    the Link cursor. Stops at the tip, an empty page, or max_pages."""
    params = ({"limit": RETURNS_PAGE, "page_info": start_page_info}
              if start_page_info else {"limit": RETURNS_PAGE})
    for _ in range(max_pages):
        resp = _request("/returns", params)
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            return
        nxt = next_page_info(resp)
        yield batch, nxt
        if not nxt:
            return
        params = {"limit": RETURNS_PAGE, "page_info": nxt}
        time.sleep(0.1)


def run(conn: sqlite3.Connection, *, restart: bool, max_pages: int) -> tuple[int, int, int]:
    """Returns (n_returns, n_lines, n_pages)."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    start_cursor = None if restart else _load_cursor(conn)
    if restart:
        with conn:
            conn.execute("DELETE FROM flexport_returns_sync_state WHERE key = ?", (CURSOR_KEY,))
    print(f"Flexport returns — "
          f"{'restart from floor' if restart else ('resume from cursor' if start_cursor else 'fresh from floor')}.")

    ret_rows: list[tuple] = []
    line_rows: list[tuple] = []
    pending_cursor: str | None = None
    n_returns = n_lines = pages = 0

    def checkpoint() -> None:
        nonlocal ret_rows, line_rows
        with conn:
            if ret_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO flexport_returns VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ret_rows)
            if line_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO flexport_return_lines VALUES "
                    "(?,?,?,?,?,?,?,?,?)", line_rows)
        ret_rows, line_rows = [], []
        if pending_cursor:
            _save_cursor(conn, pending_cursor)

    for batch, nxt in iter_return_pages(start_cursor, max_pages):
        pages += 1
        for rec in batch:
            if not isinstance(rec, dict) or "id" not in rec:
                continue
            rrow, lrows = rows_for_return(rec, stamp)
            ret_rows.append(rrow)
            line_rows.extend(lrows)
            n_returns += 1
            n_lines += len(lrows)
        pending_cursor = nxt
        if len(ret_rows) >= 200:
            checkpoint()
            print(f"  ...checkpointed: {n_returns} returns, {pages} pages")
    checkpoint()
    return n_returns, n_lines, pages


def main() -> None:
    if not os.environ.get("FLEXPORT_API_TOKEN"):
        print("FLEXPORT_API_TOKEN not set — skipping Flexport returns sync.")
        return

    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES)
    p.add_argument("--restart", action="store_true")
    args = p.parse_args()

    conn = db.connect()
    ensure_schema(conn)
    started = db.now()
    try:
        n_returns, n_lines, pages = run(conn, restart=args.restart, max_pages=args.pages)
        conn.close()
    except Exception as e:  # noqa: BLE001
        db.log_sync("flexport_returns", started, 0, "error", str(e))
        raise

    db.log_sync("flexport_returns", started, n_returns, "ok")
    print(f"Flexport returns: {n_returns} returns, {n_lines} lines over {pages} pages.")


if __name__ == "__main__":
    main()
