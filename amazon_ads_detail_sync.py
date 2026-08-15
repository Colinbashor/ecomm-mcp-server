r"""
Amazon Ads (Sponsored Products) detail report grains -> warehouse.

The campaign-level `ad_metrics` table (see warehouse/connectors/amazon_ads.py)
tells you what a campaign spent and sold, but not WHICH ASIN, keyword, or
customer search query drove it. This pulls the three v3 reporting grains that
answer those questions, DAILY, into three tables:

  amazon_ad_products     — spAdvertisedProduct: per advertised ASIN/SKU per day
  amazon_ad_targeting    — spTargeting: per keyword/target per day
  amazon_ad_search_terms — spSearchTerm: per customer search query per day

Sponsored Products only for now (Sponsored Brands/Display use different
report shapes at this grain). Retention follows the v3 reporting API, which
is much shorter than campaign-level data — expect roughly the trailing
~95 days to be available, not deep history. One failed report type never
kills the others; a run that got SOME rows from SOME grains logs as
'degraded', not 'error' — only a run that wrote nothing at all is a hard
failure (see main()).

Reuses warehouse/connectors/amazon_ads.py's `run_report` (the shared
request -> poll -> download v3 flow, including its 401-refresh-in-place and
425-duplicate-report handling) and `REGION_HOSTS` / `_access_token` — same
AMAZON_ADS_* credentials as the campaign-level connector, no new creds.

USAGE:
  python amazon_ads_detail_sync.py --days 3
  python amazon_ads_detail_sync.py --start 2026-04-01 --end 2026-07-01
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_ads import REGION_HOSTS, _access_token, run_report

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

PLATFORM = "amazon_ads_detail"
REQUIRED_ENV = ("AMAZON_ADS_CLIENT_ID", "AMAZON_ADS_CLIENT_SECRET",
                "AMAZON_ADS_REFRESH_TOKEN", "AMAZON_ADS_PROFILE_ID")

DDL = """
CREATE TABLE IF NOT EXISTS amazon_ad_products (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    ad_group_id   TEXT NOT NULL,
    asin          TEXT NOT NULL,
    sku           TEXT,
    campaign_name TEXT,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,
    purchases     REAL    DEFAULT 0,
    sales         REAL    DEFAULT 0,
    units         INTEGER DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, ad_group_id, asin)
);
CREATE INDEX IF NOT EXISTS idx_azadp_date ON amazon_ad_products(date);
CREATE INDEX IF NOT EXISTS idx_azadp_asin ON amazon_ad_products(asin);

CREATE TABLE IF NOT EXISTS amazon_ad_targeting (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    ad_group_id   TEXT NOT NULL,
    targeting     TEXT NOT NULL,
    match_type    TEXT,
    campaign_name TEXT,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,
    purchases     REAL    DEFAULT 0,
    sales         REAL    DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, ad_group_id, targeting)
);
CREATE INDEX IF NOT EXISTS idx_azadt_date ON amazon_ad_targeting(date);

CREATE TABLE IF NOT EXISTS amazon_ad_search_terms (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    ad_group_id   TEXT NOT NULL,
    search_term   TEXT NOT NULL,
    campaign_name TEXT,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,
    purchases     REAL    DEFAULT 0,
    sales         REAL    DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, campaign_id, ad_group_id, search_term)
);
CREATE INDEX IF NOT EXISTS idx_azads_date ON amazon_ad_search_terms(date);
CREATE INDEX IF NOT EXISTS idx_azads_term ON amazon_ad_search_terms(search_term);
"""

REPORTS = {
    "amazon_ad_products": {
        "reportTypeId": "spAdvertisedProduct",
        "groupBy": ["advertiser"],
        "columns": ["date", "campaignId", "campaignName", "adGroupId",
                    "advertisedAsin", "advertisedSku", "impressions", "clicks",
                    "cost", "purchases14d", "sales14d", "unitsSoldClicks14d"],
        "insert": """INSERT OR REPLACE INTO amazon_ad_products
                     (account_id, date, campaign_id, ad_group_id, asin, sku,
                      campaign_name, impressions, clicks, spend, purchases,
                      sales, units, synced_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        "row": lambda d, acct, stamp: (
            acct, d.get("date"), str(d.get("campaignId")), str(d.get("adGroupId")),
            d.get("advertisedAsin") or "", d.get("advertisedSku"),
            d.get("campaignName"),
            int(d.get("impressions", 0) or 0), int(d.get("clicks", 0) or 0),
            float(d.get("cost", 0) or 0), float(d.get("purchases14d", 0) or 0),
            float(d.get("sales14d", 0) or 0),
            int(d.get("unitsSoldClicks14d", 0) or 0), stamp),
    },
    "amazon_ad_targeting": {
        "reportTypeId": "spTargeting",
        "groupBy": ["targeting"],
        "columns": ["date", "campaignId", "campaignName", "adGroupId",
                    "targeting", "matchType", "impressions", "clicks",
                    "cost", "purchases14d", "sales14d"],
        "insert": """INSERT OR REPLACE INTO amazon_ad_targeting
                     (account_id, date, campaign_id, ad_group_id, targeting,
                      match_type, campaign_name, impressions, clicks, spend,
                      purchases, sales, synced_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        "row": lambda d, acct, stamp: (
            acct, d.get("date"), str(d.get("campaignId")), str(d.get("adGroupId")),
            d.get("targeting") or "", d.get("matchType"), d.get("campaignName"),
            int(d.get("impressions", 0) or 0), int(d.get("clicks", 0) or 0),
            float(d.get("cost", 0) or 0), float(d.get("purchases14d", 0) or 0),
            float(d.get("sales14d", 0) or 0), stamp),
    },
    "amazon_ad_search_terms": {
        "reportTypeId": "spSearchTerm",
        "groupBy": ["searchTerm"],
        "columns": ["date", "campaignId", "campaignName", "adGroupId",
                    "searchTerm", "impressions", "clicks", "cost",
                    "purchases14d", "sales14d"],
        "insert": """INSERT OR REPLACE INTO amazon_ad_search_terms
                     (account_id, date, campaign_id, ad_group_id, search_term,
                      campaign_name, impressions, clicks, spend, purchases,
                      sales, synced_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        "row": lambda d, acct, stamp: (
            acct, d.get("date"), str(d.get("campaignId")), str(d.get("adGroupId")),
            d.get("searchTerm") or "", d.get("campaignName"),
            int(d.get("impressions", 0) or 0), int(d.get("clicks", 0) or 0),
            float(d.get("cost", 0) or 0), float(d.get("purchases14d", 0) or 0),
            float(d.get("sales14d", 0) or 0), stamp),
    },
}


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_ads_detail_sync: missing required env var(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill in the "
            "Amazon Ads credentials (same ones the campaign-level connector uses)."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _chunks(start: str, end: str):
    """<=31-day windows (the v3 per-report limit)."""
    lo = date.fromisoformat(start)
    hi = date.fromisoformat(end)
    while lo <= hi:
        nxt = min(hi, lo + timedelta(days=30))
        yield lo.isoformat(), nxt.isoformat()
        lo = nxt + timedelta(days=1)


def run(conn: sqlite3.Connection, start: str, end: str) -> tuple[int, list[str]]:
    """Pull all three grains over [start, end], chunked into <=31-day windows.
    Returns (rows_written, error_messages) — one failing grain/window is
    logged and skipped, never fatal to the others."""
    host = REGION_HOSTS[os.environ.get("AMAZON_ADS_REGION", "NA")]
    acct = os.environ["AMAZON_ADS_PROFILE_ID"]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total = 0
    failures: list[str] = []
    for lo, hi in _chunks(start, end):
        headers = {
            "Authorization": f"Bearer {_access_token()}",
            "Amazon-Advertising-API-ClientId": os.environ["AMAZON_ADS_CLIENT_ID"],
            "Amazon-Advertising-API-Scope": acct,
            "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
        }
        for table, cfg in REPORTS.items():
            body = {
                "name": f"warehouse-{cfg['reportTypeId']}",
                "startDate": lo, "endDate": hi,
                "configuration": {
                    "adProduct": "SPONSORED_PRODUCTS",
                    "groupBy": cfg["groupBy"],
                    "columns": cfg["columns"],
                    "reportTypeId": cfg["reportTypeId"],
                    "timeUnit": "DAILY",
                    "format": "GZIP_JSON",
                },
            }
            try:
                records = run_report(host, headers, body)
            except Exception as e:  # noqa: BLE001 — one grain must not kill the rest
                failures.append(f"{cfg['reportTypeId']} {lo}..{hi}: {str(e)[:100]}")
                print(f"    {cfg['reportTypeId']} {lo}..{hi} FAILED: {str(e)[:100]}", flush=True)
                continue
            with conn:  # commit per report — never hold the write lock long
                conn.executemany(cfg["insert"],
                                 [cfg["row"](d, acct, stamp) for d in records])
            total += len(records)
            print(f"    {cfg['reportTypeId']} {lo}..{hi}: {len(records)} rows", flush=True)
    return total, failures


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--start")
    p.add_argument("--end")
    args = p.parse_args()
    end = args.end or date.today().isoformat()
    start = args.start or (date.fromisoformat(end) - timedelta(days=args.days)).isoformat()

    require_env()
    warehouse_db.init_db()
    started = warehouse_db.now()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)
    try:
        total, failures = run(conn, start, end)
    except Exception as e:  # noqa: BLE001
        conn.close()
        warehouse_db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    conn.close()

    # A run where SOME grain/window failed but others still wrote rows is
    # 'degraded', not 'error' — a broken grain must not read green to
    # last_sync_status, but it also shouldn't mask data that did land. Only a
    # run that wrote NOTHING at all is a hard failure.
    status = "ok" if not failures else ("error" if total == 0 else "degraded")
    msg = f"{start} -> {end}"
    if failures:
        msg = f"PARTIAL {len(failures)} failed: " + "; ".join(failures) + " | " + msg
    warehouse_db.log_sync(PLATFORM, started, total, status, msg)
    print(f"Amazon ads detail: wrote {total} rows across products/targeting/search terms "
          f"({start} -> {end})" + (f" — {len(failures)} report(s) FAILED" if failures else ""))
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
