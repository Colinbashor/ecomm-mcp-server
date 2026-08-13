r"""
Amazon SP-API flat-file (TSV) fee reports -> warehouse.

Pulls five asynchronous Reports API report types and lands each one into its
own table with a natural primary key, so a P&L can subtract real
referral/FBA/storage fees instead of guessing at them.

Reuses the same async request -> poll -> download SHAPE as
warehouse/connectors/amazon_ads.py, but these reports are flat TSV text, not
gzipped JSON, so parsing is header-driven: the header row is normalized
(lowercased, BOM/quotes stripped) and columns are looked up by name. That
makes the parser robust to Amazon reordering, renaming (hyphens vs
underscores), or adding columns between report versions — it will not break
just because a column moved.

Tables (one per report type):
  amazon_fee_preview          GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA — a snapshot
                              of ESTIMATED per-unit referral + fulfillment
                              fees. This is an ESTIMATE, not what you were
                              actually charged; see amazon_economics_sync.py
                              for actuals via a different API.
  amazon_fba_storage_fees     GET_FBA_STORAGE_FEE_CHARGES_DATA — monthly,
                              per ASIN/fulfillment-center.
  amazon_fba_reimbursements   GET_FBA_REIMBURSEMENTS_DATA — Amazon paying you
                              back for lost/damaged inventory.
  amazon_fba_promotions       GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_PROMOTION_DATA
  amazon_fulfilled_shipments  GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL —
                              per-shipment sales + promo discounts. Also
                              carries `sales_channel` ('amazon.com' vs
                              'Non-Amazon' — the signal that a row is a
                              Multi-Channel Fulfillment, i.e. Amazon shipped
                              an order that was PLACED somewhere other than
                              Amazon; `fulfillment-channel` is always AFN on
                              every row here and can't tell you that) and,
                              when the order came from an integrated storefront
                              like Shopify, `shopify_order_name` parsed out of
                              the flat file's composite `merchant-order-id`
                              field (format 'Shopify <order-name> <order-id>' —
                              split on whitespace and take the middle token),
                              which lets you join an MCF shipment back to the
                              order in your own commerce `orders` table.

NOT INGESTED: the FBA inbound placement service fee has no Reports API report
type at all as of this writing (Seller Central UI download only).

GOTCHAS:
  - A report can come back CANCELLED instead of DONE/FAILED. For these
    reports that means "no data for the requested window" (e.g. a month that
    hasn't posted yet) — a normal, non-fatal empty result, not an error.
    `_create_and_download` returns None in that case.
  - Flat files are usually cp1252/latin-1, sometimes with a UTF-8 BOM —
    decode defensively (try utf-8-sig, then cp1252, then latin-1 with
    character replacement as a last resort).
  - Money/quantity fields can contain "--" for "no value" and thousands
    separators or a currency symbol — don't just `float()` them directly.
  - The header row is sometimes quoted and its casing/separator (hyphen vs
    underscore) can drift between report versions — always parse by
    normalized header name, never by column position.

AUTH: same SPAPI_* LWA credentials as amazon_orders.py — no new creds.

USAGE:
  python amazon_fees_sync.py                       # prior full Mon-Sun week
  python amazon_fees_sync.py --week 2026-06-22
  python amazon_fees_sync.py --only fee_preview,storage
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
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

PLATFORM = "amazon_fees"

REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")

DDL = """
CREATE TABLE IF NOT EXISTS amazon_fee_preview (
    snapshot_date TEXT NOT NULL,
    sku           TEXT NOT NULL,
    fnsku         TEXT,
    asin          TEXT,
    product_name  TEXT,
    your_price    REAL,
    sales_price   REAL,
    estimated_fee_total               REAL,
    estimated_referral_fee_per_unit   REAL,
    expected_fulfillment_fee_per_unit REAL,
    currency      TEXT,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, sku)
);
CREATE TABLE IF NOT EXISTS amazon_fba_storage_fees (
    month_of_charge    TEXT NOT NULL,
    fnsku              TEXT NOT NULL,
    fulfillment_center TEXT NOT NULL,
    asin               TEXT,
    product_name       TEXT,
    avg_qty_on_hand    REAL,
    estimated_monthly_storage_fee REAL,
    currency           TEXT,
    synced_at          TEXT NOT NULL,
    PRIMARY KEY (month_of_charge, fnsku, fulfillment_center)
);
CREATE TABLE IF NOT EXISTS amazon_fba_reimbursements (
    reimbursement_id TEXT NOT NULL,
    sku              TEXT NOT NULL,
    amazon_order_id  TEXT NOT NULL,
    approval_date    TEXT,
    reason           TEXT,
    fnsku            TEXT,
    asin             TEXT,
    amount_total     REAL,
    quantity_total   INTEGER,
    currency         TEXT,
    synced_at        TEXT NOT NULL,
    PRIMARY KEY (reimbursement_id, sku, amazon_order_id)
);
CREATE TABLE IF NOT EXISTS amazon_fba_promotions (
    shipment_item_id  TEXT NOT NULL,
    item_promotion_id TEXT NOT NULL,
    shipment_date     TEXT,
    amazon_order_id   TEXT,
    shipment_id       TEXT,
    description       TEXT,
    item_promotion_discount REAL,
    currency          TEXT,
    synced_at         TEXT NOT NULL,
    PRIMARY KEY (shipment_item_id, item_promotion_id)
);
CREATE TABLE IF NOT EXISTS amazon_fulfilled_shipments (
    shipment_item_id TEXT NOT NULL PRIMARY KEY,
    shipment_date    TEXT,
    purchase_date    TEXT,
    amazon_order_id  TEXT,
    shipment_id      TEXT,
    sku              TEXT,
    quantity         INTEGER,
    item_price       REAL,
    item_promo_discount     REAL,
    shipment_promo_discount REAL,
    currency         TEXT,
    sales_channel      TEXT,  -- 'amazon.com' vs 'Non-Amazon' — the MCF discriminator
                               -- (fulfillment-channel is AFN on every row and useless
                               -- for this — see module docstring)
    shopify_order_name TEXT,  -- parsed middle token of merchant-order-id when it's
                               -- the MCF composite 'Shopify <order-name> <order-id>';
                               -- joins your `orders` table's order_id. NULL otherwise.
    synced_at        TEXT NOT NULL
);
"""


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_fees_sync: missing required env var(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill in the "
            "SP-API credentials (same ones amazon_orders.py uses)."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


# ---- report request / poll / download (TSV flat files) --------------------

def _create_and_download(host: str, report_type: str, start: str | None,
                          end: str | None) -> str | None:
    """Submit a report, wait out the queue, return the decoded flat-file text.

    Returns None if Amazon CANCELS the report — for these fee reports that
    means "no data for the requested window", which is a normal, non-fatal
    outcome the caller decides how to handle (e.g. try an earlier window).
    """
    headers = {"x-amz-access-token": _access_token(), "Content-Type": "application/json"}
    body: dict = {"reportType": report_type, "marketplaceIds": [os.environ["SPAPI_MARKETPLACE_ID"]]}
    if start and end:
        body["dataStartTime"] = f"{start}T00:00:00Z"
        body["dataEndTime"] = f"{end}T23:59:59Z"

    r = requests.post(f"{host}/reports/2021-06-30/reports", headers=headers, json=body, timeout=60)
    if r.status_code == 429:
        time.sleep(60)
        return _create_and_download(host, report_type, start, end)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"{report_type} request {r.status_code}: {r.text[:200]}")
    report_id = r.json()["reportId"]

    doc_id = None
    for _ in range(80):
        time.sleep(15)
        headers["x-amz-access-token"] = _access_token()
        st = requests.get(f"{host}/reports/2021-06-30/reports/{report_id}",
                          headers=headers, timeout=60).json()
        status = st.get("processingStatus")
        if status == "DONE":
            doc_id = st["reportDocumentId"]
            break
        if status == "CANCELLED":
            return None  # no data for this window — caller decides
        if status == "FATAL":
            raise RuntimeError(f"{report_type} failed: FATAL")
    if not doc_id:
        raise TimeoutError(f"{report_type} did not finish in time")

    doc = requests.get(f"{host}/reports/2021-06-30/documents/{doc_id}",
                       headers=headers, timeout=60).json()
    raw = requests.get(doc["url"], timeout=180).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    # Amazon flat files are usually cp1252/latin-1 with a UTF-8 BOM on some.
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _rows(text: str):
    """Yield dict rows keyed by normalized header (lowercased, BOM/quotes stripped)."""
    rdr = csv.reader(io.StringIO(text), delimiter="\t")
    try:
        header = next(rdr)
    except StopIteration:
        return
    norm = [h.replace("﻿", "").strip().strip('"').strip().lower() for h in header]
    for row in rdr:
        if not any(c.strip() for c in row):
            continue
        yield {norm[i]: (row[i] if i < len(row) else "") for i in range(len(norm))}


def _pick(d: dict, *names: str) -> str:
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return ""


def _num(v) -> float:
    if v in (None, "", "--"):
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def _int(v) -> int:
    return int(round(_num(v)))


# ---- per-report parsers ----------------------------------------------------

def parse_fee_preview(conn, text, stamp, snapshot_date, **_):
    out = []
    for d in _rows(text):
        sku = _pick(d, "sku", "seller-sku")
        if not sku:
            continue
        out.append((
            snapshot_date, sku, _pick(d, "fnsku"), _pick(d, "asin"),
            _pick(d, "product-name"),
            _num(_pick(d, "your-price")), _num(_pick(d, "sales-price")),
            _num(_pick(d, "estimated-fee-total")),
            _num(_pick(d, "estimated-referral-fee-per-unit")),
            _num(_pick(d, "expected-fulfillment-fee-per-unit")),
            _pick(d, "currency") or "USD", stamp,
        ))
    conn.executemany("INSERT OR REPLACE INTO amazon_fee_preview VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def parse_storage(conn, text, stamp, **_):
    out = []
    for d in _rows(text):
        fnsku = _pick(d, "fnsku")
        moc = _pick(d, "month_of_charge", "month-of-charge")
        fc = _pick(d, "fulfillment_center", "fulfillment-center")
        if not fnsku or not moc:
            continue
        out.append((
            moc, fnsku, fc, _pick(d, "asin"), _pick(d, "product_name", "product-name"),
            _num(_pick(d, "average_quantity_on_hand", "average-quantity-on-hand")),
            _num(_pick(d, "estimated_monthly_storage_fee", "estimated-monthly-storage-fee")),
            _pick(d, "currency") or "USD", stamp,
        ))
    conn.executemany("INSERT OR REPLACE INTO amazon_fba_storage_fees VALUES (?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def parse_reimbursements(conn, text, stamp, **_):
    out = []
    for d in _rows(text):
        rid = _pick(d, "reimbursement-id")
        if not rid:
            continue
        out.append((
            rid, _pick(d, "sku"), _pick(d, "amazon-order-id"),
            _pick(d, "approval-date"), _pick(d, "reason"),
            _pick(d, "fnsku"), _pick(d, "asin"),
            _num(_pick(d, "amount-total")),
            _int(_pick(d, "quantity-reimbursed-total")),
            _pick(d, "currency-unit", "currency") or "USD", stamp,
        ))
    conn.executemany("INSERT OR REPLACE INTO amazon_fba_reimbursements VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def parse_promotions(conn, text, stamp, **_):
    out = []
    for d in _rows(text):
        sii = _pick(d, "shipment-item-id")
        pid = _pick(d, "item-promotion-id")
        if not sii or not pid:
            continue
        out.append((
            sii, pid, _pick(d, "shipment-date"), _pick(d, "amazon-order-id"),
            _pick(d, "shipment-id"), _pick(d, "description"),
            _num(_pick(d, "item-promotion-discount")),
            _pick(d, "currency") or "USD", stamp,
        ))
    conn.executemany("INSERT OR REPLACE INTO amazon_fba_promotions VALUES (?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def _mcf_order_name(merchant_order_id: str) -> str | None:
    """MCF rows carry a composite 'Shopify <order-name> <order-id>' in
    merchant-order-id, not a plain id. Split on whitespace and take the
    middle token — the order NAME that joins your commerce `orders` table
    directly. Non-MCF rows carry a plain id (not 3 tokens) and return None."""
    parts = merchant_order_id.split()
    return parts[1] if len(parts) == 3 else None


_SHIPMENT_COLUMNS = [
    "shipment_item_id", "shipment_date", "purchase_date", "amazon_order_id",
    "shipment_id", "sku", "quantity", "item_price", "item_promo_discount",
    "shipment_promo_discount", "currency", "sales_channel",
    "shopify_order_name", "synced_at",
]


def parse_shipments(conn, text, stamp, **_):
    out = []
    for d in _rows(text):
        sii = _pick(d, "shipment-item-id")
        if not sii:
            continue
        row = {
            "shipment_item_id": sii,
            "shipment_date": _pick(d, "shipment-date"),
            "purchase_date": _pick(d, "purchase-date"),
            "amazon_order_id": _pick(d, "amazon-order-id"),
            "shipment_id": _pick(d, "shipment-id"),
            "sku": _pick(d, "sku"),
            "quantity": _int(_pick(d, "quantity-shipped", "quantity")),
            "item_price": _num(_pick(d, "item-price")),
            "item_promo_discount": _num(_pick(d, "item-promotion-discount")),
            "shipment_promo_discount": _num(_pick(d, "ship-promotion-discount")),
            "currency": _pick(d, "currency") or "USD",
            "sales_channel": _pick(d, "sales-channel"),
            "shopify_order_name": _mcf_order_name(_pick(d, "merchant-order-id")),
            "synced_at": stamp,
        }
        out.append(tuple(row[c] for c in _SHIPMENT_COLUMNS))
    placeholders = ",".join("?" * len(_SHIPMENT_COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO amazon_fulfilled_shipments ({','.join(_SHIPMENT_COLUMNS)}) "
        f"VALUES ({placeholders})",
        out,
    )
    return len(out)


# report_type, mode, parser.  mode: "snapshot" (no dates), "week" (report week
# window), "monthly" (calendar-month window; storage fees post mid-month for
# the prior month, so we walk back until a month has posted).
REPORTS = {
    "fee_preview":    ("GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA", "snapshot", parse_fee_preview),
    "storage":        ("GET_FBA_STORAGE_FEE_CHARGES_DATA", "monthly", parse_storage),
    "reimbursements": ("GET_FBA_REIMBURSEMENTS_DATA", "week", parse_reimbursements),
    "promotions":     ("GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_PROMOTION_DATA", "week", parse_promotions),
    "shipments":      ("GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL", "week", parse_shipments),
}


def _month_windows(monday: date, back: int = 3) -> list[tuple[str, str]]:
    """Calendar-month (start, end) windows: the report week's month, then earlier."""
    windows = []
    y, m = monday.year, monday.month
    for _ in range(back):
        first = date(y, m, 1)
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        last = nxt - timedelta(days=1)
        windows.append((first.isoformat(), last.isoformat()))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return windows


def sync(conn: sqlite3.Connection, monday: date, which: list[str]) -> tuple[int, list[str]]:
    """Run the chosen report(s) for the Mon-Sun week starting `monday`.
    Returns (rows_written, error_messages)."""
    start, end = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    snapshot_date = date.today().isoformat()
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total = 0
    errors: list[str] = []
    for k in which:
        report_type, mode, parser = REPORTS[k]
        try:
            if mode == "snapshot":
                windows = [(None, None)]
            elif mode == "monthly":
                windows = _month_windows(monday)
            else:
                windows = [(start, end)]

            n, used = 0, None
            for ws, we in windows:
                text = _create_and_download(host, report_type, ws, we)
                if text is None:
                    continue  # CANCELLED = no data for this window; try the next
                with conn:
                    n = parser(conn, text, stamp, snapshot_date=snapshot_date)
                used = f"{ws}..{we}" if ws else "snapshot"
                break

            total += n
            if used is None:
                print(f"    {k} ({report_type}): no data in any window", flush=True)
            else:
                print(f"    {k} ({report_type}): {n} rows [{used}]", flush=True)
        except Exception as e:  # noqa: BLE001 — one report must not kill the batch
            errors.append(f"{k}: {str(e)[:120]}")
            print(f"    {k} ({report_type}): FAILED {str(e)[:150]}", flush=True)
    return total, errors


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--week", help="Monday of the report week (default: prior complete week)")
    p.add_argument("--only", help="comma list: " + ",".join(REPORTS))
    args = p.parse_args()

    require_env()

    if args.week:
        monday = date.fromisoformat(args.week)
        if monday.weekday() != 0:
            raise SystemExit("Report weeks start on Monday.")
    else:
        today = date.today()
        monday = today - timedelta(days=today.weekday()) - timedelta(weeks=1)

    which = [k.strip() for k in args.only.split(",")] if args.only else list(REPORTS)
    for k in which:
        if k not in REPORTS:
            raise SystemExit(f"unknown report {k!r}; choose from {list(REPORTS)}")

    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)

    total, errors = sync(conn, monday, which)
    conn.close()

    status = "ok" if not errors else ("error" if total == 0 else "degraded")
    warehouse_db.log_sync(PLATFORM, started, total, status, "; ".join(errors))
    print(f"Amazon fees: wrote {total} rows for week {monday.isoformat()} [{status}]"
          + (f" ({len(errors)} report(s) failed)" if errors else ""))
    # A "degraded" run (some reports failed, some rows still landed) is a
    # success for automation purposes — the failing report names are in
    # sync_log. Only a run that wrote NOTHING is a hard failure.
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
