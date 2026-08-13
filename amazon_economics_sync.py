r"""
Amazon SKU Economics (ACTUAL per-ASIN-per-week fees + net proceeds) -> warehouse.

Uses the Data Kiosk GraphQL API, NOT the Reports API — a different surface
from amazon_fees_sync.py. Where amazon_fee_preview gives you an ESTIMATE of
referral/FBA fees, this gives you what you were ACTUALLY charged, plus net
proceeds, straight from Amazon's economics dataset. Same LWA credentials as
amazon_orders.py (SPAPI_*) — no new credentials needed.

FLOW (async): POST /dataKiosk/2023-11-15/queries {query} -> queryId; poll
GET .../queries/{id} until processingStatus is DONE; then
GET .../documents/{dataDocumentId} -> a download URL -> GZIP JSONL (one JSON
object per line). Queries can run long (tens of minutes on a big date range)
and submissions are QUOTA-limited (HTTP 429), so this submits one week at a
time and backs off on 429 rather than firing many queries at once.

GOTCHA — WEEK-ALIGNMENT: the economics API's `date:WEEK` aggregation grain is
SUNDAY-SATURDAY. If your own reporting convention uses Monday-Sunday weeks
(a common convention), a query with a Monday startDate and `date:WEEK`
VALIDATES and returns DONE, but with ZERO rows — silently, no error document.
The fix used here: query at `date:DAY` over your own Mon-Sun range and sum
the 7 daily per-ASIN rows into one weekly row yourself (`_aggregate_week`).
If you use Sunday-Saturday weeks natively, you can simplify this to request
`date:WEEK` directly — but always sanity-check row counts on your first run
against whatever other sales numbers you already trust.

SCHEMA (root `analytics_economics_2024_03_15`; introspection is disabled on
this API, so keys and shapes below were confirmed by hand against the docs
and a live query — expect to re-verify if Amazon changes the schema):
  economics(startDate, endDate, marketplaceIds:[..]!, aggregateBy:{date:DAY,
            productId:CHILD_ASIN})
    startDate endDate parentAsin childAsin msku fnsku marketplaceId
    sales { orderedProductSales{amount currencyCode} netProductSales{..}
            netUnitsSold unitsOrdered unitsRefunded averageSellingPrice{..} }
    fees  { charge { aggregatedDetail { totalAmount{amount currencyCode} }
                     components { name } } }
    ads   { charge { totalAmount{amount currencyCode} } }
    netProceeds { total{amount currencyCode} perUnit{amount currencyCode} }
    cost  { costOfGoodsSold{amount currencyCode} }
Money fields are always `{amount, currencyCode}` objects, never bare numbers.
NOTE: `fees` and `ads` are each a LIST of charge summaries (not a single
object) — sum across the list. `components.name` under `fees` typically comes
back NULL at this aggregation grain (no per-fee-type breakdown available),
so we store any component names we do see as a JSON list for reference only.
`cost.costOfGoodsSold` is usually NULL — Amazon does not hold your COGS, so
don't rely on this field for margin; use your own cost data.
`netProceeds.total` NETS refunds/adjustments, so it will NOT equal
sales-minus-fees in aggregate — treat it as a reference column, not the basis
for a margin calculation.

CAVEATS: data typically lags ~72h behind real time; retention is limited (no
deep historical backfill — this is a rolling-window feed like FBA inventory).
A week that comes back with zero rows can simply mean the data hasn't
settled yet — the run prints daily-row and ASIN counts so that's visible.

USAGE:
  python amazon_economics_sync.py --week 2026-06-22
  python amazon_economics_sync.py --weeks 4      # last 4 complete Mon-Sun weeks
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS, _access_token

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

PLATFORM = "amazon_economics"
POLL_TIMEOUT_MIN = int(os.environ.get("DATAKIOSK_TIMEOUT_MIN", "150"))  # econ weeks can run 1-2h

REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")

DDL = """
CREATE TABLE IF NOT EXISTS amazon_economics (
    week_start            TEXT NOT NULL,   -- Monday of the report week (query start)
    asin                  TEXT NOT NULL,   -- childAsin
    parent_asin           TEXT,
    msku                  TEXT,
    fnsku                 TEXT,
    marketplace_id        TEXT,
    range_start           TEXT,            -- economics row startDate (authoritative)
    range_end             TEXT,
    ordered_product_sales REAL DEFAULT 0,
    net_product_sales     REAL DEFAULT 0,
    net_units_sold        INTEGER DEFAULT 0,
    units_ordered         INTEGER DEFAULT 0,
    units_refunded        INTEGER DEFAULT 0,
    avg_selling_price     REAL DEFAULT 0,
    total_fees            REAL DEFAULT 0,  -- charge.aggregatedDetail.totalAmount (sum)
    fee_components        TEXT,            -- JSON list of component names (reference only)
    ads_charge            REAL DEFAULT 0,
    net_proceeds_total    REAL DEFAULT 0,
    net_proceeds_per_unit REAL DEFAULT 0,
    cogs                  REAL DEFAULT 0,
    currency              TEXT,
    synced_at             TEXT NOT NULL,
    PRIMARY KEY (week_start, asin)
);
"""

QUERY_TMPL = (
    'query { analytics_economics_2024_03_15 { economics('
    'startDate:"%(start)s", endDate:"%(end)s", marketplaceIds:["%(mp)s"], '
    'aggregateBy:{date:DAY, productId:CHILD_ASIN}) { '
    'startDate endDate parentAsin childAsin msku fnsku marketplaceId '
    'sales { orderedProductSales{amount currencyCode} netProductSales{amount currencyCode} '
    'netUnitsSold unitsOrdered unitsRefunded averageSellingPrice{amount currencyCode} } '
    'fees { charge { aggregatedDetail { totalAmount{amount currencyCode} } components { name } } } '
    'ads { charge { totalAmount{amount currencyCode} } } '
    'netProceeds { total{amount currencyCode} perUnit{amount currencyCode} } '
    'cost { costOfGoodsSold{amount currencyCode} } } } }'
)


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_economics_sync: missing required env var(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill in the "
            "SP-API credentials (same ones amazon_orders.py uses)."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _hdr() -> dict:
    return {"x-amz-access-token": _access_token(), "Content-Type": "application/json"}


def _money(obj) -> tuple[float, str | None]:
    if not isinstance(obj, dict):
        return 0.0, None
    try:
        return float(obj.get("amount") or 0), obj.get("currencyCode")
    except (TypeError, ValueError):
        return 0.0, obj.get("currencyCode")


def _charge_total(node) -> float:
    """Sum charge.aggregatedDetail.totalAmount across a fees/ads node that may
    be a dict or a list of summaries."""
    if not node:
        return 0.0
    items = node if isinstance(node, list) else [node]
    total = 0.0
    for it in items:
        ch = (it or {}).get("charge") or {}
        agg = ch.get("aggregatedDetail")
        if isinstance(agg, dict):          # fees shape: charge.aggregatedDetail.totalAmount
            total += _money(agg.get("totalAmount"))[0]
        else:                               # ads shape: charge.totalAmount directly
            total += _money(ch.get("totalAmount"))[0]
    return total


def _fee_component_names(node) -> list[str]:
    names = []
    for it in (node if isinstance(node, list) else [node]) if node else []:
        for c in ((it or {}).get("charge") or {}).get("components") or []:
            if c.get("name"):
                names.append(c["name"])
    return names


def _req(host: str, method: str, url: str, auth: bool = True, **kw):
    """HTTP with retry on transient connection resets/timeouts — api.amazon.com
    (and its Data Kiosk surface) sheds connections under load. auth=False for
    the pre-signed document-download URL, which must NOT get the LWA header."""
    for attempt in range(6):
        try:
            return requests.request(method, url, headers=_hdr() if auth else None,
                                    timeout=kw.pop("timeout", 60), **kw)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 5:
                raise
            time.sleep(min(60, 5 * (attempt + 1) + 10))


def _submit(host: str, query: str) -> str:
    for attempt in range(8):
        r = _req(host, "POST", f"{host}/dataKiosk/2023-11-15/queries", json={"query": query})
        if r.status_code in (200, 202):
            return r.json()["queryId"]
        if r.status_code == 429:  # quota/throttle — wait and retry
            wait = min(300, 30 * (attempt + 1))
            print(f"    quota/throttle on submit; waiting {wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Data Kiosk submit {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Data Kiosk submit kept hitting the quota after retries.")


def _await_document(host: str, query_id: str) -> str | None:
    """Poll until DONE; return dataDocumentId (None if the result set is empty).
    Rides out transient poll errors — only wall-clock timeout ends the wait."""
    deadline = time.time() + POLL_TIMEOUT_MIN * 60
    while time.time() < deadline:
        r = _req(host, "GET", f"{host}/dataKiosk/2023-11-15/queries/{query_id}")
        if r.status_code != 200:
            time.sleep(30)
            continue
        rec = r.json()
        st = rec.get("processingStatus")
        if st == "DONE":
            return rec.get("dataDocumentId")  # may be absent => empty result
        if st in ("FATAL", "CANCELLED"):
            raise RuntimeError(f"Data Kiosk query {query_id} {st}: {json.dumps(rec)[:300]}")
        time.sleep(30)
    raise RuntimeError(f"Data Kiosk query {query_id} still processing after {POLL_TIMEOUT_MIN} min.")


def _download_jsonl(host: str, document_id: str) -> list[dict]:
    d = _req(host, "GET", f"{host}/dataKiosk/2023-11-15/documents/{document_id}").json()
    blob = _req(host, "GET", d["documentUrl"], auth=False, timeout=180).content
    try:
        text = gzip.decompress(blob).decode("utf-8")
    except OSError:
        text = blob.decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def aggregate_week(daily_rows: list[dict]) -> dict[str, dict]:
    """Collapse per-ASIN-per-DAY economics rows into one accumulator per child
    ASIN. See the module docstring's week-alignment gotcha for why this is
    done in Python instead of asking the API for date:WEEK directly."""
    agg: dict[str, dict] = {}
    for e in daily_rows:
        asin = e.get("childAsin")
        if not asin:
            continue
        sales = e.get("sales") or {}
        np_ = e.get("netProceeds") or {}
        ops, cur = _money(sales.get("orderedProductSales"))
        nps, _ = _money(sales.get("netProductSales"))
        npt, npc = _money(np_.get("total"))
        cogs, _ = _money((e.get("cost") or {}).get("costOfGoodsSold"))
        a = agg.get(asin)
        if a is None:
            a = agg[asin] = {
                "parent_asin": None, "msku": None, "fnsku": None, "marketplace_id": None,
                "ops": 0.0, "nps": 0.0, "net_units": 0, "units_ordered": 0, "units_refunded": 0,
                "fees": 0.0, "ads": 0.0, "np_total": 0.0, "cogs": 0.0,
                "currency": None, "fee_names": set(),
            }
        # carry the latest non-null identity fields (stable across days)
        a["parent_asin"] = e.get("parentAsin") or a["parent_asin"]
        a["msku"] = e.get("msku") or a["msku"]
        a["fnsku"] = e.get("fnsku") or a["fnsku"]
        a["marketplace_id"] = e.get("marketplaceId") or a["marketplace_id"]
        a["currency"] = cur or npc or a["currency"]
        a["ops"] += ops
        a["nps"] += nps
        a["net_units"] += int(sales.get("netUnitsSold") or 0)
        a["units_ordered"] += int(sales.get("unitsOrdered") or 0)
        a["units_refunded"] += int(sales.get("unitsRefunded") or 0)
        a["fees"] += _charge_total(e.get("fees"))
        a["ads"] += _charge_total(e.get("ads"))
        a["np_total"] += npt
        a["cogs"] += cogs
        a["fee_names"].update(_fee_component_names(e.get("fees")))
    return agg


def _row(week_start: str, week_end: str, asin: str, a: dict, stamp: str) -> tuple:
    units = a["units_ordered"]
    asp = round(a["ops"] / units, 2) if units else 0.0
    npu = round(a["np_total"] / units, 2) if units else 0.0
    return (
        week_start, asin, a["parent_asin"], a["msku"], a["fnsku"], a["marketplace_id"],
        week_start, week_end,
        round(a["ops"], 2), round(a["nps"], 2), a["net_units"], units, a["units_refunded"], asp,
        round(a["fees"], 2), json.dumps(sorted(a["fee_names"])),
        round(a["ads"], 2), round(a["np_total"], 2), npu, round(a["cogs"], 2),
        a["currency"], stamp,
    )


def run(conn: sqlite3.Connection, week_starts: list[str]) -> int:
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    mp = os.environ.get("SPAPI_MARKETPLACE_ID", "ATVPDKIKX0DER")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    all_rows = []
    for ws in week_starts:
        sun = (date.fromisoformat(ws) + timedelta(days=6)).isoformat()
        print(f"  {ws}..{sun}: submitting Data Kiosk query (DAY grain, aggregating to week)")
        qid = _submit(host, QUERY_TMPL % {"start": ws, "end": sun, "mp": mp})
        doc = _await_document(host, qid)
        rows = _download_jsonl(host, doc) if doc else []
        agg = aggregate_week(rows)
        with_sales = sum(1 for a in agg.values() if a["ops"] > 0)
        print(f"    {ws}: {len(rows)} daily rows -> {len(agg)} ASINs ({with_sales} with sales)")
        all_rows += [_row(ws, sun, asin, a, stamp) for asin, a in agg.items()]

    with conn:
        for ws in week_starts:
            conn.execute("DELETE FROM amazon_economics WHERE week_start = ?", (ws,))
        conn.executemany(
            """INSERT OR REPLACE INTO amazon_economics
               (week_start, asin, parent_asin, msku, fnsku, marketplace_id,
                range_start, range_end, ordered_product_sales, net_product_sales,
                net_units_sold, units_ordered, units_refunded, avg_selling_price,
                total_fees, fee_components, ads_charge, net_proceeds_total,
                net_proceeds_per_unit, cogs, currency, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            all_rows,
        )
    return len(all_rows)


def _mondays(n: int) -> list[str]:
    """Last n complete Mon-Sun weeks (excludes the running week)."""
    today = date.today()
    last_mon = today - timedelta(days=today.weekday()) - timedelta(weeks=1)
    return [(last_mon - timedelta(weeks=i)).isoformat() for i in range(n)][::-1]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--week", help="Monday of the report week")
    p.add_argument("--weeks", type=int, help="last N complete Mon-Sun weeks")
    args = p.parse_args()

    require_env()

    if args.week:
        wk = date.fromisoformat(args.week)
        if wk.weekday() != 0:
            raise SystemExit("Report weeks start on Monday.")
        weeks = [wk.isoformat()]
    elif args.weeks:
        weeks = _mondays(args.weeks)
    else:
        weeks = _mondays(1)

    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)
    try:
        n = run(conn, weeks)
    except Exception as e:  # noqa: BLE001
        conn.close()
        warehouse_db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    conn.close()
    warehouse_db.log_sync(PLATFORM, started, n, "ok", f"{weeks[0]}..{weeks[-1]} ({len(weeks)}w)")
    print(f"Amazon economics: wrote {n} rows across {len(weeks)} week(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
