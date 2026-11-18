#!/usr/bin/env python
"""Fulfilled by TikTok (FBT) inventory -> daily snapshots + stock-movement history.

FBT is TikTok Shop's own fulfillment network: you send stock into TikTok's
warehouses and TikTok picks and packs your TikTok Shop orders from it. Those
units have left your own 3PL and are in no other channel's inventory feed, so
without this script the warehouse cannot see them.

ENDPOINTS (all signed like every other TikTok Shop call)
  GET  /fbt/202408/warehouses                  FBT warehouses (id -> name)
  POST /fbt/202409/goods/search                the goods census: every FBT
                                               "goods" (one size-level SKU)
                                               TikTok knows for the shop
  POST /fbt/202408/inventory/search            per-warehouse stock levels
  POST /fbt/202410/inventory_records/search    every on-hand change (inbound,
                                               sale, return, disposal ...)
  The three searches page at 100 and accept a `goods_ids` filter of at most
  100 ids, so inventory and records are queried BY ID in chunks of 100. That
  also makes the request provably cover the whole census. The records search
  answered "access denied" when sent with no goods filter while the filtered
  call succeeded, so it is always filtered.

TABLES
  tiktok_fbt_snapshot_log       one row per snapshot_date: what was ASKED and
                                what came back. THIS is the "we looked" marker.
                                An FBT pool can legitimately be empty (everything
                                sold through), so the absence of inventory rows
                                must never be read as "not synced". Health checks
                                read this table, not tiktok_fbt_inventory.
  tiktok_fbt_inventory          (snapshot_date, fbt_warehouse_id, goods_id) levels.
                                Snapshot-only: TikTok returns current state, so a
                                missed day is lost for good. A re-run on the same
                                day replaces that day.
  tiktok_fbt_goods              the goods census (goods -> reference code, TikTok
                                product / SKU ids, barcodes).
  tiktok_fbt_inventory_records  on-hand movements with before/after quantities.
                                These DO have history: each run re-pulls a
                                trailing window (idempotent), and --records-since
                                reaches further back.

WHAT COUNTS AS STOCK: `available` only (sellable now). `reserved` is already
allocated to customer orders, `unfulfillable` cannot be sold, and in-transit
units are still on their way IN. Those are stored in their own columns and
must never be added to `available`.

SKU KEY: each goods carries your merchant `reference_code`. It is stored as-is,
and copied to `sku_key` for joining to your own SKU tables. Set
TIKTOK_FBT_SKU_PATTERN (a regex) if only codes of a known shape are real SKUs;
a code that does not fully match is then stored with sku_key NULL and counted
in the run message, never guessed.

STATUS: `ok` when the goods census answered. An empty census logs `degraded`:
with nothing registered there is no way to tell "FBT holds nothing" from "the
API returned nothing". An empty INVENTORY with a non-empty census is a measured
zero (sold through) and is `ok`.

ACCESS: the Partner Center app needs the FBT scope. Re-authorise with ALL
scopes at once; a partial re-authorisation silently drops the others. For some
minutes after a re-authorisation the FBT endpoints have been seen to answer
105005 ("access denied") intermittently for a call that succeeds moments later,
so 105005 is retried a few times before it is treated as a real scope loss.
Expired-token and transient codes reuse warehouse/connectors/tiktok_shop.py.

USAGE
  python tiktok_fbt_sync.py                              # snapshot today + 7 days of movements
  python tiktok_fbt_sync.py --records-since 2026-01-01   # deeper movement pull
  python tiktok_fbt_sync.py --probe                      # reachability + a sample; writes nothing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

from warehouse import db, net
from warehouse.connectors.tiktok_shop import (
    BASE, TOKEN_EXPIRED_CODES, TRANSIENT_CODES, _refresh_access_token, _sign)

load_dotenv()

PLATFORM = "tiktok_fbt"
WAREHOUSES_PATH = "/fbt/202408/warehouses"
GOODS_PATH = "/fbt/202409/goods/search"
INVENTORY_PATH = "/fbt/202408/inventory/search"
RECORDS_PATH = "/fbt/202410/inventory_records/search"
PAGE_SIZE = 100          # API max for the three search endpoints
ID_CHUNK = 100           # max length of the goods_ids filter
RECORDS_DAYS = 7         # trailing movement window per run; re-pulls are idempotent
ACCESS_DENIED = 105005
DENIED_RETRIES = 4       # see ACCESS in the module docstring
DENIED_SLEEP = 15
MAX_TRANSIENT = 5
REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

# Inventory row layout (see inventory_rows); summarize() reads these positions.
_QTY_COLUMNS = (("available", 8), ("reserved", 9), ("total_onhand", 10),
                ("unfulfillable", 11), ("in_transit", 12))

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_fbt_snapshot_log (
    snapshot_date       TEXT PRIMARY KEY,
    warehouses          INTEGER,   -- FBT warehouses listed
    goods_registered    INTEGER,   -- the census: goods TikTok knows for the shop
    inventory_rows      INTEGER,   -- (warehouse, goods) rows returned
    available           INTEGER,
    reserved            INTEGER,
    total_onhand        INTEGER,
    unfulfillable       INTEGER,
    in_transit          INTEGER,
    synced_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiktok_fbt_inventory (
    snapshot_date       TEXT NOT NULL,
    fbt_warehouse_id    TEXT NOT NULL,
    warehouse_name      TEXT,
    goods_id            TEXT NOT NULL,
    reference_code      TEXT,          -- your merchant code, as TikTok returns it
    sku_key             TEXT,          -- reference_code, or NULL if it fails TIKTOK_FBT_SKU_PATTERN
    goods_name          TEXT,
    tiktok_sku_ids      TEXT,          -- comma-joined TikTok Shop SKU ids
    available           INTEGER DEFAULT 0,  -- THE usable number
    reserved            INTEGER DEFAULT 0,  -- allocated to customer orders
    total_onhand        INTEGER DEFAULT 0,  -- physically in the warehouse
    unfulfillable       INTEGER DEFAULT 0,  -- cannot be sold
    in_transit          INTEGER DEFAULT 0,  -- on its way INTO FBT
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, fbt_warehouse_id, goods_id)
);
CREATE INDEX IF NOT EXISTS idx_fbt_inv_sku_key ON tiktok_fbt_inventory(sku_key);
CREATE TABLE IF NOT EXISTS tiktok_fbt_goods (
    goods_id            TEXT PRIMARY KEY,
    reference_code      TEXT,
    sku_key             TEXT,
    name                TEXT,
    tiktok_product_id   TEXT,
    tiktok_sku_ids      TEXT,
    barcodes_json       TEXT,
    synced_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiktok_fbt_inventory_records (
    create_time         INTEGER NOT NULL,  -- unix seconds
    record_date         TEXT NOT NULL,     -- UTC date of create_time
    fbt_warehouse_id    TEXT NOT NULL,
    goods_id            TEXT NOT NULL,
    reference_code      TEXT,
    sku_key             TEXT,
    goods_type          TEXT NOT NULL,     -- NORMAL / DEFECTIVE
    order_id            TEXT NOT NULL,     -- '' when the record carries none
    order_type          TEXT,              -- INBOUND_ORDER, CONSIGN_ORDER, ...
    initial_on_hand     INTEGER NOT NULL,
    final_on_hand       INTEGER NOT NULL,
    changed_quantity    INTEGER,
    synced_at           TEXT NOT NULL,
    -- the API gives no record id; this tuple is unique per movement, so
    -- re-pulling an overlapping window replaces rather than duplicates
    PRIMARY KEY (goods_id, fbt_warehouse_id, create_time, order_id, goods_type,
                 initial_on_hand, final_on_hand)
);
CREATE INDEX IF NOT EXISTS idx_fbt_rec_date ON tiktok_fbt_inventory_records(record_date);
"""


class AccessDenied(RuntimeError):
    """105005 that survived the retries: the FBT scope is really missing."""


def check_required_env() -> None:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def sku_key_for(reference_code: str | None, pattern: str | None = None) -> str | None:
    """The reference code as a join key. With a pattern (default:
    TIKTOK_FBT_SKU_PATTERN), only a code that fully matches it is a key."""
    code = (reference_code or "").strip()
    if not code:
        return None
    if pattern is None:
        pattern = os.environ.get("TIKTOK_FBT_SKU_PATTERN") or ""
    if pattern and not re.fullmatch(pattern, code):
        return None
    return code


def _call(method: str, path: str, body: dict | None = None,
          query: dict | None = None) -> dict:
    """One signed request. Three independent budgets: one token refresh,
    MAX_TRANSIENT retries of a transient code or a non-JSON body, and
    DENIED_RETRIES retries of 105005. Every pass is re-signed with a fresh
    timestamp. Transport errors and HTTP 5xx are retried inside net.request."""
    payload = "" if body is None else json.dumps(body)
    refreshed, transient, denied = False, 0, 0
    while True:
        params = {"app_key": os.environ["TIKTOK_APP_KEY"],
                  "timestamp": str(int(time.time())),
                  "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"]}
        params.update(query or {})
        params["sign"] = _sign(path, params, payload, os.environ["TIKTOK_APP_SECRET"])
        headers = {"Content-Type": "application/json",
                   "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]}
        resp = net.request(method, f"{BASE}{path}", params=params,
                           data=payload if method != "GET" else None,
                           headers=headers, timeout=60)
        try:
            data = resp.json()
        except ValueError:
            if transient >= MAX_TRANSIENT:
                raise
            transient += 1
            time.sleep(min(15 * transient, 60))
            continue
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and not refreshed:
            _refresh_access_token()
            refreshed = True
            continue
        if code in TRANSIENT_CODES and transient < MAX_TRANSIENT:
            transient += 1
            time.sleep(min(15 * transient, 60))
            continue
        if code == ACCESS_DENIED:
            if denied < DENIED_RETRIES:
                denied += 1
                time.sleep(DENIED_SLEEP)
                continue
            raise AccessDenied(f"{path}: 105005 access denied after {denied} retries; "
                               "check that the app still holds the FBT scope")
        if code not in (0, None):
            raise RuntimeError(f"TikTok API {path} {code}: {data.get('message')}")
        return data.get("data") or {}


def _paged(path: str, body: dict, key: str) -> list[dict]:
    """Every page of a POST search. Stops on an EMPTY next_page_token, never
    on a short page: a short page only means that request carried less."""
    out: list[dict] = []
    token = ""
    while True:
        query = {"page_size": str(PAGE_SIZE)}
        if token:
            query["page_token"] = token
        data = _call("POST", path, body, query)
        out.extend(data.get(key) or [])
        token = data.get("next_page_token") or ""
        if not token:
            return out


def _chunks(ids: list[str], n: int = ID_CHUNK):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def fetch_warehouses() -> dict[str, str]:
    data = _call("GET", WAREHOUSES_PATH)
    return {str(w["fbt_warehouse_id"]): w.get("name") or ""
            for w in data.get("warehouses") or []}


def fetch_goods() -> list[dict]:
    return _paged(GOODS_PATH, {}, "goods")


def fetch_inventory(goods_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for chunk in _chunks(goods_ids):
        out.extend(_paged(INVENTORY_PATH, {"goods_ids": chunk}, "inventory"))
    return out


def fetch_records(goods_ids: list[str], since: date, until: datetime) -> list[dict]:
    lo = int(datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp())
    hi = int(until.timestamp())
    out: list[dict] = []
    for chunk in _chunks(goods_ids):
        out.extend(_paged(RECORDS_PATH, {"goods_ids": chunk, "create_time_ge": lo,
                                         "create_time_le": hi}, "inventory_records"))
    return out


def _sku_ids(skus: list[dict]) -> str | None:
    return ",".join(str(s["id"]) for s in skus if s.get("id")) or None


def goods_rows(goods: list[dict], stamp: str) -> list[tuple]:
    rows = []
    for g in goods:
        skus = g.get("skus") or []
        product_ids = {(s.get("product") or {}).get("id") for s in skus} - {None, ""}
        rows.append((
            str(g["id"]), g.get("reference_code"), sku_key_for(g.get("reference_code")),
            g.get("name"), ",".join(sorted(str(p) for p in product_ids)) or None,
            _sku_ids(skus), json.dumps(g.get("barcodes") or []), stamp))
    return rows


def inventory_rows(inv: list[dict], warehouses: dict[str, str],
                   snapshot: str, stamp: str) -> list[tuple]:
    rows = []
    for r in inv:
        g = r.get("goods") or {}
        o = r.get("on_hand_detail") or {}
        wh = str(r["fbt_warehouse_id"])
        rows.append((
            snapshot, wh, warehouses.get(wh), str(g.get("id")), g.get("reference_code"),
            sku_key_for(g.get("reference_code")), g.get("name"), _sku_ids(g.get("skus") or []),
            int(o.get("available_quantity") or 0), int(o.get("reserved_quantity") or 0),
            int(o.get("total_quantity") or 0), int(o.get("unfulfillable_quantity") or 0),
            int(r.get("in_transit_quantity") or 0), stamp))
    return rows


def record_rows(recs: list[dict], stamp: str) -> list[tuple]:
    rows = []
    for r in recs:
        g = r.get("goods") or {}
        o = r.get("order") or {}
        ct = int(r["create_time"])
        rows.append((
            ct, datetime.fromtimestamp(ct, timezone.utc).date().isoformat(),
            str(r["fbt_warehouse_id"]), str(g.get("id")), g.get("reference_code"),
            sku_key_for(g.get("reference_code")), r.get("inventory_goods_type") or "",
            o.get("id") or "", o.get("type"),
            int(r.get("initial_on_hand_quantity") or 0),
            int(r.get("final_on_hand_quantity") or 0),
            r.get("changed_quantity"), stamp))
    return rows


def summarize(inv_rows: list[tuple]) -> dict[str, int]:
    return {name: sum(r[i] for r in inv_rows) for name, i in _QTY_COLUMNS}


def store(conn: sqlite3.Connection, snapshot: str, warehouses: dict[str, str],
          g_rows: list[tuple], inv_rows: list[tuple], rec_rows: list[tuple],
          stamp: str) -> None:
    """One transaction, so the snapshot log row is only ever written together
    with the inventory rows it describes."""
    tot = summarize(inv_rows)
    ensure_schema(conn)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tiktok_fbt_goods (goods_id, reference_code, sku_key, "
            "name, tiktok_product_id, tiktok_sku_ids, barcodes_json, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?)", g_rows)
        # replace the whole day: a goods that dropped out of TikTok's answer
        # since an earlier run today must not linger
        conn.execute("DELETE FROM tiktok_fbt_inventory WHERE snapshot_date=?", (snapshot,))
        conn.executemany(
            "INSERT INTO tiktok_fbt_inventory (snapshot_date, fbt_warehouse_id, "
            "warehouse_name, goods_id, reference_code, sku_key, goods_name, tiktok_sku_ids, "
            "available, reserved, total_onhand, unfulfillable, in_transit, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", inv_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO tiktok_fbt_inventory_records (create_time, record_date, "
            "fbt_warehouse_id, goods_id, reference_code, sku_key, goods_type, order_id, "
            "order_type, initial_on_hand, final_on_hand, changed_quantity, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rec_rows)
        conn.execute(
            "INSERT OR REPLACE INTO tiktok_fbt_snapshot_log (snapshot_date, warehouses, "
            "goods_registered, inventory_rows, available, reserved, total_onhand, "
            "unfulfillable, in_transit, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (snapshot, len(warehouses), len(g_rows), len(inv_rows), tot["available"],
             tot["reserved"], tot["total_onhand"], tot["unfulfillable"],
             tot["in_transit"], stamp))


def status_for(goods_count: int) -> str:
    """`ok` when the census answered; `degraded` when it came back empty."""
    return "ok" if goods_count else "degraded"


def probe() -> int:
    try:
        wh = fetch_warehouses()
        goods = fetch_goods()
        inv = fetch_inventory([str(g["id"]) for g in goods]) if goods else []
    except AccessDenied as e:
        print(f"DENIED: {e}")
        return 1
    print(f"warehouses {len(wh)}, goods {len(goods)}, inventory rows {len(inv)}")
    for r in inv[:10]:
        g, o = r.get("goods") or {}, r.get("on_hand_detail") or {}
        name = wh.get(str(r["fbt_warehouse_id"]), r["fbt_warehouse_id"])
        print(f"  {name!s:12} {g.get('reference_code')!s:14} {o} "
              f"in_transit={r.get('in_transit_quantity')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="check reachability and print a sample; writes nothing")
    ap.add_argument("--records-since",
                    help=f"YYYY-MM-DD start of the movement window (default: {RECORDS_DAYS} days ago)")
    args = ap.parse_args()

    check_required_env()
    if args.probe:
        return probe()

    db.init_db()
    started = db.now()
    snapshot = date.today().isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    since = (date.fromisoformat(args.records_since) if args.records_since
             else date.today() - timedelta(days=RECORDS_DAYS))
    try:
        warehouses = fetch_warehouses()
        goods = fetch_goods()
        ids = [str(g["id"]) for g in goods]
        inv = fetch_inventory(ids) if ids else []
        recs = fetch_records(ids, since, datetime.now(timezone.utc)) if ids else []
        g_rows = goods_rows(goods, stamp)
        inv_rows = inventory_rows(inv, warehouses, snapshot, stamp)
        rec_rows = record_rows(recs, stamp)
        conn = db.connect()
        try:
            store(conn, snapshot, warehouses, g_rows, inv_rows, rec_rows, stamp)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        db.log_sync(PLATFORM, started, 0, "error", str(e)[:500])
        raise

    tot = summarize(inv_rows)
    unkeyed = sorted({r[1] for r in g_rows if r[1] and not r[2]})
    status = status_for(len(g_rows))
    msg = (f"{len(g_rows)} goods, {len(inv_rows)} inventory rows: {tot['available']:,} "
           f"available, {tot['reserved']:,} reserved, {tot['unfulfillable']:,} unfulfillable, "
           f"{tot['in_transit']:,} in transit; {len(rec_rows)} movement records since "
           f"{since.isoformat()}; {len(unkeyed)} reference codes outside TIKTOK_FBT_SKU_PATTERN")
    # rows = everything written (census + levels + movements), so a sold-through
    # pool still reports the census it measured rather than 0
    db.log_sync(PLATFORM, started, len(g_rows) + len(inv_rows) + len(rec_rows), status, msg)
    print(f"TikTok FBT {snapshot}: {msg}")
    if unkeyed:
        print(f"  sku_key left NULL for: {', '.join(unkeyed[:20])}")
    if status != "ok":
        print("WARNING: empty FBT goods census; logged degraded, not ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
