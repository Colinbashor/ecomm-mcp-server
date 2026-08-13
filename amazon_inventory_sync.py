r"""
Amazon FBA (Fulfilled by Amazon) inventory -> daily snapshots.

Pulls the SP-API FBA Inventory API (`fba/inventory/v1/summaries`,
`details=true`) and writes one row per SKU per day into `amazon_inventory`.
The API only ever exposes the CURRENT inventory level — there is no
historical endpoint — so running this once a day (e.g. from a scheduled
task) is what builds a time series. Each day's pull is its own row
(PK snapshot_date+sku); nothing is overwritten, so history accumulates.

AUTH: reuses the SP-API LWA refresh-token flow already implemented in
warehouse/connectors/amazon_orders.py (SPAPI_CLIENT_ID/SECRET/REFRESH_TOKEN,
SPAPI_MARKETPLACE_ID, SPAPI_REGION — see .env.example). No new credentials
needed; this script fails fast with a clear message if they're unset.

GOTCHA — DO NOT SUM ROWS NAIVELY BY SKU: Amazon can report the exact same
physical stock pool under more than one merchant SKU "alias" — for example,
suffixed SKU variants registered for different fulfillment programs — that
all resolve to one underlying `fn_sku` with byte-identical quantities across
the aliases. If you sum `total` grouped by the raw `sku` column you will
over-count real stock. Group by `fn_sku` first (or dedupe on it) before
computing any aggregate; the raw `sku` value is worth keeping only so you can
tell the aliases apart, not for arithmetic.

GOTCHA — PAGINATION TOKEN LOCATION: the `nextToken` for this endpoint lives
in a top-level `pagination` object in the response, NOT inside `payload`
alongside the inventory rows. Easy to miss if you're used to APIs that nest
pagination next to the data.

USAGE:
  python amazon_inventory_sync.py             # snapshot today, all SKUs
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS, _access_token

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

PLATFORM = "amazon_inventory"

REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")

DDL = """
CREATE TABLE IF NOT EXISTS amazon_inventory (
    snapshot_date        TEXT NOT NULL,   -- the day this level was observed
    sku                  TEXT NOT NULL,   -- seller SKU (may be an alias, see module docstring)
    asin                 TEXT,
    fn_sku               TEXT,            -- the STABLE key for the physical stock pool
    condition             TEXT,
    fulfillable          INTEGER DEFAULT 0,
    inbound_working      INTEGER DEFAULT 0,
    inbound_shipped      INTEGER DEFAULT 0,
    inbound_receiving    INTEGER DEFAULT 0,
    reserved_orders      INTEGER DEFAULT 0,  -- pending customer orders
    reserved_transfer    INTEGER DEFAULT 0,  -- fulfillment-center transfers
    reserved_processing  INTEGER DEFAULT 0,  -- fulfillment-center processing
    unfulfillable        INTEGER DEFAULT 0,
    researching          INTEGER DEFAULT 0,
    total                INTEGER DEFAULT 0,
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, sku)
);
CREATE INDEX IF NOT EXISTS idx_amazon_inventory_sku    ON amazon_inventory(sku);
CREATE INDEX IF NOT EXISTS idx_amazon_inventory_fn_sku ON amazon_inventory(fn_sku);
"""

_COLUMNS = [
    "snapshot_date", "sku", "asin", "fn_sku", "condition", "fulfillable",
    "inbound_working", "inbound_shipped", "inbound_receiving",
    "reserved_orders", "reserved_transfer", "reserved_processing",
    "unfulfillable", "researching", "total", "synced_at",
]


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_inventory_sync: missing required env var(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill in the "
            "SP-API credentials (same ones amazon_orders.py uses)."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _parse_summary(s: dict, snapshot_date: str, stamp: str) -> dict:
    det = s.get("inventoryDetails") or {}
    res = det.get("reservedQuantity") or {}
    return {
        "snapshot_date": snapshot_date,
        "sku": s.get("sellerSku") or "",
        "asin": s.get("asin"),
        "fn_sku": s.get("fnSku"),
        "condition": s.get("condition"),
        "fulfillable": int(det.get("fulfillableQuantity", 0) or 0),
        "inbound_working": int(det.get("inboundWorkingQuantity", 0) or 0),
        "inbound_shipped": int(det.get("inboundShippedQuantity", 0) or 0),
        "inbound_receiving": int(det.get("inboundReceivingQuantity", 0) or 0),
        "reserved_orders": int(res.get("pendingCustomerOrderQuantity", 0) or 0),
        "reserved_transfer": int(res.get("pendingTransshipmentQuantity", 0) or 0),
        "reserved_processing": int(res.get("fcProcessingQuantity", 0) or 0),
        "unfulfillable": int((det.get("unfulfillableQuantity") or {}).get("totalUnfulfillableQuantity", 0) or 0),
        "researching": int((det.get("researchingQuantity") or {}).get("totalResearchingQuantity", 0) or 0),
        "total": int(s.get("totalQuantity", 0) or 0),
        "synced_at": stamp,
    }


def fetch_inventory() -> list[dict]:
    """Pull every FBA inventory summary for the configured marketplace."""
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    mkt = os.environ["SPAPI_MARKETPLACE_ID"]
    snapshot_date = date.today().isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows: list[dict] = []
    params = {
        "details": "true",
        "granularityType": "Marketplace",
        "granularityId": mkt,
        "marketplaceIds": mkt,
    }
    while True:
        for attempt in range(8):
            try:
                resp = requests.get(
                    f"{host}/fba/inventory/v1/summaries",
                    params=params,
                    headers={"x-amz-access-token": _access_token(), "Accept": "application/json"},
                    timeout=60,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                time.sleep(15)
                continue
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 5)))
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"FBA inventory {resp.status_code}: {resp.text[:200]}")
            break
        else:
            raise RuntimeError("FBA inventory kept failing after retries.")

        body = resp.json()
        payload = body.get("payload", {})
        for s in payload.get("inventorySummaries", []):
            rows.append(_parse_summary(s, snapshot_date, stamp))

        # Pagination token lives OUTSIDE payload — see module docstring.
        next_token = (body.get("pagination") or {}).get("nextToken")
        if not next_token:
            break
        params = {
            "details": "true", "granularityType": "Marketplace",
            "granularityId": mkt, "marketplaceIds": mkt, "nextToken": next_token,
        }
        time.sleep(0.6)  # stay comfortably under the ~2 req/s limit
    return rows


def write_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    placeholders = ",".join(f":{c}" for c in _COLUMNS)
    with conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO amazon_inventory ({','.join(_COLUMNS)}) VALUES ({placeholders})",
            rows,
        )
    return len(rows)


def main() -> int:
    require_env()
    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)
    try:
        rows = fetch_inventory()
        n = write_rows(conn, rows)
    except Exception as e:  # noqa: BLE001
        conn.close()
        warehouse_db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    conn.close()
    warehouse_db.log_sync(PLATFORM, started, n, "ok")
    print(f"Amazon inventory: snapshotted {n} SKUs for {date.today().isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
