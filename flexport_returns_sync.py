r"""
Flexport Logistics (3PL) connector — customer returns received by the 3PL.

The returns counterpart to flexport_orders_sync.py: if Flexport is the 3PL
that receives returned items back from your customers, GET /returns is the
return-rate / disposition signal (why items came back, what condition they
arrived in, whether they went back into sellable stock).

Two data tables (one row per return, one row per returned line) + a walk log:
  flexport_returns       — PK id. status (CREATED/SHIPPED/RECEIVED_FOR_PROCESSING/
                           PROCESSED/CANCELLED), rma, external_return_id,
                           carrier + tracking, source address (where the
                           customer shipped from), fulfillment_order_id (joins
                           flexport_order_costs.order_id when the vendor
                           supplies it — often null), shipped/received/inspected
                           timestamps. synced_at = the last time a walk SAW the
                           return, not when it last changed.
  flexport_return_lines  — PK (return_id, line_no). The returned SKU,
                           expected_quantity, and once inspected: received/final
                           condition + disposition (e.g. RESTOCK vs scrap). A
                           line can have more than one inspected item; we keep
                           the FIRST inspection's disposition plus a count.
  flexport_returns_walks — one row per run: what the walk ASKED for (mode,
                           stop_id) and what it actually reached (head_id,
                           lowest_id, complete), with seen/new/changed counts.
                           This is the resume marker; which rows exist never is.

THE FEED IS NEWEST-FIRST (id DESCENDING). The cursor in the
`Link: <...page_info=...>; rel="next"` header is base64url of
{"cursor": <last id on the page>, "direction": "forward"}, meaning "ids below
this one", and the FINAL page is the OLDEST end of the feed. An earlier
version of this connector assumed oldest-first: it stored the last page's
cursor and "resumed to the tip" from it, which re-read the same oldest page
every run, never saw a new return (they are prepended at the head) and never
saw a stored return change status — while logging `ok` every day.

SO EVERY WALK STARTS AT THE HEAD AND WALKS DOWN. Two depths:
  window (default) — down to a stop id: the LOWER of the high-water mark (the
                     head of the latest COMPLETE walk, so nothing new is
                     skipped) and the id that was the head REFRESH_DAYS ago.
                     The window exists to re-read returns that can still
                     change: a return can sit CREATED for weeks before the
                     customer ships it, and ship -> inspect can take months at
                     the tail. Size REFRESH_DAYS from your own lifecycle (the
                     default 180 covers a slow 3PL's p99).
  full             — to the EMPTY page (a short page is never the end). Runs
                     automatically when no complete full walk is younger than
                     FULL_WALK_EVERY_DAYS, so abandoned RMAs far down the feed
                     are still re-checked weekly rather than daily.
A walk that is interrupted (page cap, Flexport outage) does not advance the
high-water mark, so the next run re-walks from the head — the window is small
enough that resuming mid-walk is not worth a second cursor to keep correct.

END OF FEED: a page with no next link is confirmed by asking once more below
its lowest id with a crafted cursor (`_cursor_below`, byte-identical to the
ones Flexport issues); the walk counts as finished only on an EMPTY page. A
page whose ids are not all below the previous page's raises FeedOrderError
rather than walking to a wrong stop — if the feed order ever changes (or
differs on another API version), this fails loudly instead of silently.

A RUN THAT CHANGES NOTHING IS `degraded`, NOT `ok`. A completed walk that
finds no new return and no changed one logs `degraded` and exits 75 (this
repo's "deliberate pause on an upstream fault" convention), as do a page cap
and a Flexport outage (pages already written are kept). For a store with
steady return volume, a feed that changes nothing is a fault, not a quiet
day; if your volume is low enough that whole days pass with no return
activity, expect the occasional `degraded`. rows_written is new + changed,
never rows merely seen; changes are detected by reading the stored rows back
through SQLite before and after the upsert, so type-affinity coercion cannot
fake one.

Auth: bearer token in FLEXPORT_API_TOKEN (.env), same token as the other
Flexport connectors. Skips cleanly if unset. sync_log platform
'flexport_returns'. argparse, so --help prints usage and exits.

USAGE:
  python flexport_returns_sync.py             # window walk (full when one is due)
  python flexport_returns_sync.py --full      # walk the whole feed now (--restart = alias)
  python flexport_returns_sync.py --pages 50  # cap this run (leaves the walk incomplete)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

import requests
from dotenv import load_dotenv

from warehouse import db

load_dotenv()

BASE = "https://logistics-api.flexport.com/logistics/api/2024-06"
RETURNS_PAGE = 100        # /returns caps at 100 like the other list endpoints
DEFAULT_MAX_PAGES = 100_000
PLATFORM = "flexport_returns"
GRACEFUL_PAUSE_EXIT = 75  # deliberate pause on an upstream fault (matches the rest of the repo)

# How far below the head the window walk re-reads, in days of feed. Size it
# from your returns lifecycle (creation -> ship, plus ship -> inspect at the
# tail). The weekly full walk catches anything older.
REFRESH_DAYS = 180
FULL_WALK_EVERY_DAYS = 7
# Activity dates below this are placeholders ('0000-00-00 00:00:00' is stored
# on some CREATED returns) and must not count as "old activity".
VALID_DATE_FLOOR = "2000-01-01"
# The old oldest-first resume cursor. Nothing reads it any more; it is deleted
# on startup so nobody mistakes it for live state.
RETIRED_CURSOR_KEY = "returns_page_info"

DDL = """
CREATE TABLE IF NOT EXISTS flexport_returns (
    id                   INTEGER PRIMARY KEY,   -- Flexport return id
    status               TEXT,                  -- CREATED/SHIPPED/RECEIVED_FOR_PROCESSING/PROCESSED/CANCELLED
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
    synced_at            TEXT                   -- last time a walk saw it, not last change
);
CREATE TABLE IF NOT EXISTS flexport_return_lines (
    return_id           INTEGER NOT NULL,
    line_no             INTEGER NOT NULL,
    identifier          TEXT,                  -- returned SKU
    expected_quantity   INTEGER,
    n_inspected         INTEGER,               -- how many inspected items on this line
    received_condition  TEXT,                  -- first inspection's condition (API sends '' -> NULL)
    final_condition     TEXT,
    disposition         TEXT,                  -- first inspection's disposition (RESTOCK / ...)
    synced_at           TEXT,
    PRIMARY KEY (return_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_flexport_return_lines_sku ON flexport_return_lines(identifier);
CREATE INDEX IF NOT EXISTS idx_flexport_returns_status ON flexport_returns(status);
CREATE TABLE IF NOT EXISTS flexport_returns_walks (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    mode          TEXT NOT NULL,              -- 'window' | 'full'
    stop_id       INTEGER,                    -- the floor ASKED for; NULL = to the empty page
    head_id       INTEGER,                    -- newest id seen (top of page 1)
    lowest_id     INTEGER,                    -- deepest id actually reached
    pages         INTEGER,
    returns_seen  INTEGER,
    new_returns   INTEGER,
    changed       INTEGER,                    -- existing returns whose fields or lines changed
    complete      INTEGER NOT NULL DEFAULT 0, -- 1 = reached stop_id (or the empty page)
    status        TEXT                        -- running / ok / degraded / error
);
"""

RETURN_COLUMNS = (
    "id", "status", "rma", "external_return_id", "fulfillment_order_id",
    "carrier", "tracking_code", "tracking_status", "source_name",
    "source_city", "source_state", "source_zip", "source_country",
    "shipped_at", "received_at", "inspected_at", "n_lines", "synced_at",
)
LINE_COLUMNS = (
    "return_id", "line_no", "identifier", "expected_quantity", "n_inspected",
    "received_condition", "final_condition", "disposition", "synced_at",
)
# Named columns, never positional VALUES: SQLite appends an ALTER-added column
# at the table's PHYSICAL end, so `VALUES (?,?,...)` would map onto the wrong
# columns on a migrated table. An upsert, so the row is updated in place.
_RETURN_UPSERT = (
    f"INSERT INTO flexport_returns ({', '.join(RETURN_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(RETURN_COLUMNS))}) "
    "ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in RETURN_COLUMNS[1:])
)
_LINE_INSERT = (
    f"INSERT INTO flexport_return_lines ({', '.join(LINE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(LINE_COLUMNS))})"
)
# What "changed" compares: every stored field except synced_at.
_SNAPSHOT_RETURN_COLS = RETURN_COLUMNS[:-1]
_SNAPSHOT_LINE_COLS = LINE_COLUMNS[1:-1]

_NEXT_RE = re.compile(r'page_info=([^&>]+)[^>]*>\s*;\s*rel="next"')

Fetch = Callable[[str | None], tuple[list, str | None]]


class FlexportTransient(RuntimeError):
    """Transient (5xx/429/connection) failures exhausted all retries. Distinct
    from a hard 401/4xx so main() can PAUSE (pages already committed survive)
    and exit 75 rather than crash."""


class FeedOrderError(RuntimeError):
    """A page carried an id at or above the previous page's lowest id: the feed
    stopped being newest-first. Every stop rule here depends on that order, so
    this fails loudly instead of walking to a wrong stop."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _request(path: str, params: dict) -> requests.Response:
    """GET with the same resilience pattern used across the Flexport
    connectors: retries on connection blips/429/5xx, 401 is hard-fatal.
    Returns the Response so callers can read the Link cursor header. Raises
    FlexportTransient once the retries run out."""
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
    raise FlexportTransient(f"Flexport {path} kept failing after retries. Last error: {last_error}")


def next_page_info(resp: requests.Response) -> str | None:
    link = resp.headers.get("link") or resp.headers.get("Link") or ""
    m = _NEXT_RE.search(link)
    return m.group(1) if m else None


def _cursor_below(return_id: int) -> str:
    """A page_info meaning "returns with id below return_id" — byte-identical to
    the cursor Flexport issues ({"cursor": <last id on the page>, "direction":
    "forward"}, compact JSON, unpadded base64url). Used only when a page comes
    back WITHOUT a next link, to confirm the end of the feed on an empty page."""
    raw = json.dumps({"cursor": return_id, "direction": "forward"}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _fetch_page(cursor: str | None) -> tuple[list, str | None]:
    """One live page: (records, Flexport-issued next cursor or None)."""
    params: dict = {"limit": RETURNS_PAGE}
    if cursor:
        params["page_info"] = cursor
    resp = _request("/returns", params)
    body = resp.json()
    if not isinstance(body, list):
        # An error body (e.g. a gateway-timeout message) is NOT end of feed.
        raise RuntimeError(
            f"Flexport /returns returned {type(body).__name__}, not a list: {str(body)[:200]}")
    time.sleep(0.1)
    return body, next_page_info(resp)


def rows_for_return(rec: dict, stamp: str) -> tuple[tuple, list[tuple]]:
    """(return row in RETURN_COLUMNS order, line rows in LINE_COLUMNS order)."""
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


def _return_ids(batch: list) -> list[int]:
    return [rec["id"] for rec in batch
            if isinstance(rec, dict) and isinstance(rec.get("id"), int)]


@dataclass
class Walk:
    mode: str                      # 'window' | 'full'
    stop_id: int | None            # floor ASKED for; None = walk to the empty page
    head_id: int | None = None     # newest id seen
    lowest_id: int | None = None   # deepest id reached
    pages: int = 0
    complete: bool = False


def walk_pages(walk: Walk, max_pages: int, fetch: Fetch = _fetch_page) -> Iterator[list]:
    """Yield pages newest-first from the HEAD, updating `walk` as it goes.

    Complete when a page reaches walk.stop_id, or on an EMPTY page. A page with
    no next link is NOT the end: the walk asks once more below that page's
    lowest id and stops only if that comes back empty. Hitting max_pages leaves
    the walk incomplete.
    """
    cursor: str | None = None
    while walk.pages < max_pages:
        batch, nxt = fetch(cursor)
        if not batch:
            walk.complete = True
            return
        ids = _return_ids(batch)
        if not ids:
            raise RuntimeError(f"Flexport /returns page carried no return ids: {str(batch)[:200]}")
        if walk.lowest_id is not None and max(ids) >= walk.lowest_id:
            raise FeedOrderError(
                f"page holds id {max(ids)} at/above the previous page's lowest "
                f"{walk.lowest_id} — /returns is no longer newest-first")
        walk.pages += 1
        if walk.head_id is None:
            walk.head_id = max(ids)
        walk.lowest_id = min(ids)
        yield batch
        if walk.stop_id is not None and walk.lowest_id <= walk.stop_id:
            walk.complete = True
            return
        cursor = nxt or _cursor_below(walk.lowest_id)


def _last_complete_full(conn: sqlite3.Connection) -> str | None:
    return conn.execute(
        "SELECT MAX(started_at) FROM flexport_returns_walks "
        "WHERE mode = 'full' AND complete = 1").fetchone()[0]


def _high_water_mark(conn: sqlite3.Connection) -> int | None:
    """The head of the latest COMPLETE walk. Every walk starts at the head and a
    window walk never stops above the previous mark, so every return at or
    below this id has been captured. Taken from what was walked, never from
    which rows exist (a report can legitimately omit rows, so absence is not
    a resume marker)."""
    return conn.execute(
        "SELECT MAX(head_id) FROM flexport_returns_walks WHERE complete = 1").fetchone()[0]


def _window_floor_id(conn: sqlite3.Connection, now: datetime) -> int | None:
    """The id that was the feed's head REFRESH_DAYS ago.

    Exact once the walk log is that old: the head recorded by the latest walk
    started on or before the cutoff. Until then it is bounded from the data: a
    return with any activity date before the cutoff existed at the cutoff, so
    the highest such id is at or below the head then — a floor that can only
    make the walk deeper, never shallower. Placeholder dates ('0000-00-00 ...')
    are excluded, since one on a recent return would lift the floor. None = no
    basis, caller walks it all.
    """
    cutoff = now - timedelta(days=REFRESH_DAYS)
    row = conn.execute(
        "SELECT head_id FROM flexport_returns_walks "
        "WHERE head_id IS NOT NULL AND started_at <= ? "
        "ORDER BY started_at DESC LIMIT 1",
        (cutoff.isoformat(timespec="seconds"),)).fetchone()
    if row:
        return row[0]
    params = {"floor": VALID_DATE_FLOOR, "cut": cutoff.date().isoformat()}
    return conn.execute(
        "SELECT MAX(id) FROM flexport_returns WHERE "
        "(shipped_at >= :floor AND shipped_at < :cut) OR "
        "(received_at >= :floor AND received_at < :cut) OR "
        "(inspected_at >= :floor AND inspected_at < :cut)", params).fetchone()[0]


def plan_walk(conn: sqlite3.Connection, now: datetime, force_full: bool = False) -> Walk:
    """Full when asked, when no complete full walk is younger than
    FULL_WALK_EVERY_DAYS (including the very first run, which doubles as the
    initial backfill), or when no window floor can be established; otherwise
    a window walk to min(window floor, high-water mark)."""
    if force_full:
        return Walk("full", None)
    last_full = _last_complete_full(conn)
    due = (now - timedelta(days=FULL_WALK_EVERY_DAYS)).isoformat(timespec="seconds")
    if last_full is None or last_full < due:
        return Walk("full", None)
    hwm = _high_water_mark(conn)
    floor = _window_floor_id(conn, now)
    if hwm is None or floor is None:
        return Walk("full", None)
    return Walk("window", min(floor, hwm))


def _snapshot(conn: sqlite3.Connection, ids: list[int]) -> dict[int, tuple]:
    """Stored state of these returns (fields + lines, minus synced_at), read
    back THROUGH SQLite so a type coerced by column affinity (an id sent as a
    string, say) can never register as a change on every run."""
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    fields = {row[0]: tuple(row) for row in conn.execute(
        f"SELECT {', '.join(_SNAPSHOT_RETURN_COLS)} FROM flexport_returns "
        f"WHERE id IN ({qs})", ids)}
    lines: dict[int, list[tuple]] = {}
    for row in conn.execute(
            f"SELECT return_id, {', '.join(_SNAPSHOT_LINE_COLS)} FROM flexport_return_lines "
            f"WHERE return_id IN ({qs}) ORDER BY return_id, line_no", ids):
        lines.setdefault(row[0], []).append(tuple(row)[1:])
    return {rid: (f, tuple(lines.get(rid, ()))) for rid, f in fields.items()}


def _upsert_page(conn: sqlite3.Connection, batch: list, stamp: str) -> tuple[int, int, int]:
    """Write one page in one transaction; returns (seen, new, changed). A
    return's lines are replaced wholesale, so a shrunk line set leaves no stale
    rows behind."""
    recs = [rec for rec in batch if isinstance(rec, dict) and isinstance(rec.get("id"), int)]
    ids = [rec["id"] for rec in recs]
    with conn:
        before = _snapshot(conn, ids)
        for rec in recs:
            ret_row, line_rows = rows_for_return(rec, stamp)
            conn.execute(_RETURN_UPSERT, ret_row)
            conn.execute("DELETE FROM flexport_return_lines WHERE return_id = ?", (rec["id"],))
            if line_rows:
                conn.executemany(_LINE_INSERT, line_rows)
        after = _snapshot(conn, ids)
    new = sum(1 for rid in ids if rid not in before)
    changed = sum(1 for rid in ids if rid in before and before[rid] != after.get(rid))
    return len(ids), new, changed


@dataclass
class RunResult:
    walk: Walk | None = None
    seen: int = 0
    new: int = 0
    changed: int = 0
    vanished: int | None = None    # stored ids a complete FULL walk did not see
    status: str = "running"
    exit_code: int = 0
    message: str = ""
    seen_ids: set = field(default_factory=set, repr=False)

    @property
    def rows_written(self) -> int:
        return self.new + self.changed


def _summary(r: RunResult) -> str:
    w = r.walk
    stop = "end of feed" if w.stop_id is None else f"stop {w.stop_id}"
    text = (f"{w.mode} walk: {w.pages:,} pages, ids {w.head_id} -> {w.lowest_id} "
            f"({stop}, {'complete' if w.complete else 'INCOMPLETE'}); "
            f"{r.seen:,} seen, {r.new:,} new, {r.changed:,} changed")
    if r.vanished is not None:
        text += f", {r.vanished:,} stored ids absent from the feed"
    return text


def outcome(r: RunResult, max_pages: int) -> tuple[str, int, str]:
    """(sync_log status, exit code, message) for a walk that raised nothing."""
    summary = _summary(r)
    if not r.walk.complete:
        return ("degraded", GRACEFUL_PAUSE_EXIT,
                f"INCOMPLETE: hit the {max_pages:,}-page cap before the stop id, so "
                f"the high-water mark did not advance. {summary}")
    if r.rows_written == 0:
        return ("degraded", GRACEFUL_PAUSE_EXIT,
                "NO PROGRESS: the walk completed but found no new return and no "
                "changed one. A live returns feed normally changes daily, so this "
                f"is likely a fault, not a quiet day. {summary}")
    return "ok", 0, summary


def _record_walk_start(conn: sqlite3.Connection, walk: Walk, started_at: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO flexport_returns_walks (started_at, mode, stop_id, complete, status) "
            "VALUES (?, ?, ?, 0, 'running')", (started_at, walk.mode, walk.stop_id))
    return cur.lastrowid


def _record_walk_end(conn: sqlite3.Connection, run_id: int, r: RunResult) -> None:
    w = r.walk
    with conn:
        conn.execute(
            "UPDATE flexport_returns_walks SET finished_at = ?, head_id = ?, lowest_id = ?, "
            "pages = ?, returns_seen = ?, new_returns = ?, changed = ?, complete = ?, status = ? "
            "WHERE run_id = ?",
            (db.now(), w.head_id, w.lowest_id, w.pages, r.seen, r.new, r.changed,
             int(w.complete), r.status, run_id))


def _count_vanished(conn: sqlite3.Connection, r: RunResult) -> int:
    """Stored returns at or below the head that a complete full walk never saw
    — reported, not deleted: whether a cancelled return leaves the feed or just
    changes status is not guaranteed by the API."""
    stored = {row[0] for row in conn.execute(
        "SELECT id FROM flexport_returns WHERE id <= ?", (r.walk.head_id,))}
    return len(stored - r.seen_ids)


def run(conn: sqlite3.Connection, *, force_full: bool = False,
        max_pages: int = DEFAULT_MAX_PAGES, fetch: Fetch = _fetch_page,
        now: datetime | None = None, result: RunResult | None = None) -> RunResult:
    """Plan and execute one walk against `conn` (schema already ensured). Fills
    `result` in place when given, so a caller still has the partial counts if
    this raises. `fetch` is injectable for tests."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    r = result if result is not None else RunResult()
    r.walk = plan_walk(conn, now, force_full)
    run_id = _record_walk_start(conn, r.walk, stamp)
    print(f"Flexport returns — {r.walk.mode} walk from the head"
          + (f" down to id {r.walk.stop_id}." if r.walk.stop_id is not None else " to the end of the feed."))
    try:
        for batch in walk_pages(r.walk, max_pages, fetch):
            seen, new, changed = _upsert_page(conn, batch, stamp)
            r.seen += seen
            r.new += new
            r.changed += changed
            r.seen_ids.update(_return_ids(batch))
            if r.walk.pages % 50 == 0:
                print(f"  ...{r.walk.pages:,} pages, down to id {r.walk.lowest_id}: "
                      f"{r.new:,} new, {r.changed:,} changed")
    except FlexportTransient as e:
        r.status, r.exit_code = "degraded", GRACEFUL_PAUSE_EXIT
        r.message = f"paused (transient): {e}. Pages written so far are kept. {_summary(r)}"
        _record_walk_end(conn, run_id, r)
        return r
    except Exception as e:  # noqa: BLE001
        r.status, r.message = "error", f"{e}. {_summary(r)}"
        _record_walk_end(conn, run_id, r)
        raise
    if r.walk.mode == "full" and r.walk.complete and r.walk.head_id is not None:
        r.vanished = _count_vanished(conn, r)
    r.status, r.exit_code, r.message = outcome(r, max_pages)
    _record_walk_end(conn, run_id, r)
    return r


def _retire_old_cursor(conn: sqlite3.Connection) -> None:
    """Delete the old oldest-first resume cursor so nobody mistakes it for live
    state. An absent state table (fresh DB) is fine."""
    try:
        with conn:
            conn.execute("DELETE FROM flexport_returns_sync_state WHERE key = ?",
                         (RETIRED_CURSOR_KEY,))
    except sqlite3.OperationalError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flexport customer returns -> flexport_returns / flexport_return_lines.")
    ap.add_argument("--full", action="store_true",
                    help="walk the whole feed now (otherwise automatic every "
                         f"{FULL_WALK_EVERY_DAYS} days)")
    ap.add_argument("--restart", action="store_true", help="alias of --full")
    ap.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES,
                    help="cap pages this run (an unfinished walk logs degraded)")
    args = ap.parse_args(argv)

    if not os.environ.get("FLEXPORT_API_TOKEN"):
        print("FLEXPORT_API_TOKEN not set — skipping Flexport returns sync.")
        return 0

    started = db.now()
    result = RunResult()
    conn = db.connect()
    try:
        ensure_schema(conn)
        _retire_old_cursor(conn)
        run(conn, force_full=args.full or args.restart, max_pages=args.pages, result=result)
    except Exception as e:  # noqa: BLE001
        db.log_sync(PLATFORM, started, result.rows_written, "error", result.message or str(e))
        raise
    finally:
        conn.close()

    db.log_sync(PLATFORM, started, result.rows_written, result.status, result.message)
    print(f"Flexport returns [{result.status}]: {result.message}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
