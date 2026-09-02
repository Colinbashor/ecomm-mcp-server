r"""
Amazon Warehousing & Distribution (AWD) inventory -> daily snapshots.

AWD is Amazon's bulk-storage tier that sits UPSTREAM of FBA: a seller ships
inventory into an AWD facility and Amazon distributes it into FBA as the FBA
position draws down. The FBA Inventory API (`amazon_inventory_sync.py`) does
NOT report AWD stock at all — it is a genuinely separate pool, invisible to
every reorder/replenishment decision that only looks at FBA levels. If you
use AWD, this can be a large blind spot: on one seller's account it measured
at roughly 40% the size of their entire FBA fulfillable pool, and individual
SKUs read "critically low" in FBA while holding four figures of units one
transfer away.

Needs NO new credentials if you already have `amazon_inventory_sync.py`
working — the AWD endpoints answer on the same SP-API LWA app/refresh-token
(`SPAPI_*`). `GET /awd/2024-05-09/inventory` and `.../inboundShipments` are
reachable on ordinary seller credentials; only `.../inboundEligibility`
requires something more, and this connector never calls it.

!! THE DOUBLE-COUNTING TRAP — READ BEFORE ADDING AWD TO AN FBA POSITION !!
The API returns four quantities per SKU, and only ONE of them is genuinely
EXTRA stock the FBA feed can't already see:
  * `totalOnhandQuantity` — decomposes as available + reserved. NOT a safe
    number to add to an FBA position on its own.
  * `availableDistributableQuantity` — THE usable number: stock sitting in
    AWD with nothing else claimed against it yet.
  * `reservedDistributableQuantity` — already allocated to a distribution
    order (i.e. already committed to move to FBA), still physically in AWD.
  * `replenishmentQuantity` — already in transit AWD -> FBA, no longer part
    of on-hand.
The last two are already committed to FBA and typically reappear FBA-side as
one of `amazon_inventory`'s `inbound_*` columns for the same SKU — summing
them into an FBA position double-counts the same physical units mid-transfer.
Every consumer of this data should therefore read `available_distributable`
and never `total_onhand` when computing "how much MORE stock is there
somewhere". All four raw buckets are still stored so the decision stays
auditable — see `amazon_awd.py` for the shared reader that encodes this as
its one accessor rather than leaving every caller to get it right separately.

CURRENT-STATE ONLY, like `amazon_inventory_sync.py`: the API exposes today's
levels and nothing historical, so running this once a day is what builds a
time series (PK `snapshot_date, sku` — nothing is overwritten). There is no
backfill and a missed day's snapshot is gone permanently.

`sku` here is the same seller-SKU space as `amazon_inventory.sku` (whatever
alias/suffix convention your seller account uses), so the two tables join
directly on it. AWD reports no ASIN at all — bridge through
`amazon_inventory`'s own sku->asin mapping (see `amazon_awd.by_asin()`).

USAGE:
  python amazon_awd_sync.py               # snapshot today
  python amazon_awd_sync.py --probe       # check reachability, print a sample; writes nothing
  python amazon_awd_sync.py --date 2026-01-15
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS, _access_token

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

PLATFORM = "amazon_awd"
REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")

AWD_PATH = "/awd/2024-05-09/inventory"
PAGE_SIZE = 100          # API max; latency is per-request so always page at the ceiling
MAX_TRIES = 8
THROTTLE_SEC = 0.6

DDL = """
CREATE TABLE IF NOT EXISTS amazon_awd_inventory (
    snapshot_date           TEXT NOT NULL,  -- the day this level was observed
    sku                     TEXT NOT NULL,  -- seller SKU, same space as amazon_inventory.sku
    total_onhand            INTEGER DEFAULT 0,  -- = available + reserved; NOT extra stock on its own
    total_inbound           INTEGER DEFAULT 0,  -- on its way INTO AWD
    available_distributable INTEGER DEFAULT 0,  -- THE usable number (see module docstring)
    reserved_distributable  INTEGER DEFAULT 0,  -- committed to FBA; overlaps FBA inbound
    replenishment_qty       INTEGER DEFAULT 0,  -- in transit AWD->FBA; overlaps FBA inbound
    synced_at               TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, sku)
);
CREATE INDEX IF NOT EXISTS idx_amazon_awd_inventory_sku ON amazon_awd_inventory(sku);
"""

_COLUMNS = [
    "snapshot_date", "sku", "total_onhand", "total_inbound",
    "available_distributable", "reserved_distributable", "replenishment_qty",
    "synced_at",
]


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_awd_sync: missing required env var(s): "
            f"{', '.join(missing)}. Same SP-API credentials amazon_inventory_sync.py "
            "and amazon_orders.py use — copy .env.example to .env and fill them in."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _parse_item(item: dict, snapshot_date: str, stamp: str) -> dict:
    det = item.get("inventoryDetails") or {}
    return {
        "snapshot_date": snapshot_date,
        "sku": item.get("sku") or "",
        "total_onhand": int(item.get("totalOnhandQuantity", 0) or 0),
        "total_inbound": int(item.get("totalInboundQuantity", 0) or 0),
        "available_distributable": int(det.get("availableDistributableQuantity", 0) or 0),
        "reserved_distributable": int(det.get("reservedDistributableQuantity", 0) or 0),
        "replenishment_qty": int(det.get("replenishmentQuantity", 0) or 0),
        "synced_at": stamp,
    }


def _get(url: str, params: dict) -> requests.Response:
    """GET with the same retry posture as the other SP-API connectors."""
    for _ in range(MAX_TRIES):
        try:
            resp = requests.get(
                url, params=params,
                headers={"x-amz-access-token": _access_token(),
                         "Accept": "application/json"}, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(15)
            continue
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 5)))
            continue
        if resp.status_code >= 500:
            time.sleep(10)
            continue
        return resp
    raise RuntimeError("AWD inventory kept failing after retries.")


def fetch_awd(snapshot_date: str | None = None) -> list[dict]:
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    snap = snapshot_date or date.today().isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows: list[dict] = []
    token = None
    while True:
        params = {"maxResults": PAGE_SIZE, "details": "SHOW"}
        if token:
            params["nextToken"] = token
        resp = _get(f"{host}{AWD_PATH}", params)
        if resp.status_code != 200:
            raise RuntimeError(f"AWD inventory {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        for item in body.get("inventory", []) or []:
            rows.append(_parse_item(item, snap, stamp))
        token = body.get("nextToken")
        if not token:
            break
        time.sleep(THROTTLE_SEC)
    return rows


def write_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    placeholders = ",".join(f":{c}" for c in _COLUMNS)
    with conn:
        # Named columns, never positional — an ALTER-added column lands at the
        # table's PHYSICAL end and a positional INSERT would silently shift
        # every value one column over. Cheap to get right from the start.
        conn.executemany(
            f"INSERT OR REPLACE INTO amazon_awd_inventory ({','.join(_COLUMNS)}) "
            f"VALUES ({placeholders})", rows)
    return len(rows)


def probe() -> int:
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    resp = _get(f"{host}{AWD_PATH}", {"maxResults": 10, "details": "SHOW"})
    print(f"GET {AWD_PATH} -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return 1
    items = resp.json().get("inventory", []) or []
    print(f"reachable; first page returned {len(items)} SKUs")
    for it in items[:5]:
        d = it.get("inventoryDetails") or {}
        print(f"  {str(it.get('sku')):18} onhand={it.get('totalOnhandQuantity'):>6} "
              f"available={d.get('availableDistributableQuantity'):>6} "
              f"reserved={d.get('reservedDistributableQuantity'):>5} "
              f"replenishment={d.get('replenishmentQuantity'):>5}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                     help="check reachability and print a sample; writes nothing")
    ap.add_argument("--date", help="store under this snapshot_date (default: today)")
    args = ap.parse_args()

    if args.probe:
        return probe()

    require_env()
    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)
    try:
        rows = fetch_awd(args.date)
        n = write_rows(conn, rows)
    except Exception as e:  # noqa: BLE001
        conn.close()
        warehouse_db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    conn.close()

    avail = sum(r["available_distributable"] for r in rows)
    onhand = sum(r["total_onhand"] for r in rows)
    # An empty snapshot is NOT ok: downstream it is indistinguishable from "AWD
    # holds nothing", which would quietly recreate the exact blind spot this
    # connector exists to close.
    status = "ok" if rows else "degraded"
    msg = f"{n} SKUs, {avail:,} available-to-distribute (on-hand {onhand:,})"
    warehouse_db.log_sync(PLATFORM, started, n, status, msg)
    print(f"Amazon AWD: {msg} for {args.date or date.today().isoformat()}")
    if not rows:
        print("WARNING: zero rows — logged degraded, not ok.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
