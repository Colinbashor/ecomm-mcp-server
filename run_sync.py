"""
Sync runner. This is the job you schedule (e.g. Windows Task Scheduler) to
refresh the warehouse. It runs every connector that has credentials set,
writes the results into SQLite, and records each run in sync_log.

Usage:
    python run_sync.py                # last 7 days, all configured platforms
    python run_sync.py --days 30      # last 30 days
    python run_sync.py --start 2026-06-01 --end 2026-06-30
    python run_sync.py --only meta,google
    python run_sync.py --only amazon,amazon_orders   # Amazon Ads + retail orders
    python run_sync.py --sample       # load fake demo data (no API keys needed)
"""
from __future__ import annotations

import argparse
import os
import random
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors import amazon_ads, amazon_orders, google_ads, meta_ads, shopify, tiktok_shop

load_dotenv()

# platform name -> (module, kind). kind tells us which table it fills.
CONNECTORS = {
    "google": (google_ads, "ads"),
    "meta": (meta_ads, "ads"),
    "amazon": (amazon_ads, "ads"),
    "amazon_orders": (amazon_orders, "orders"),
    "shopify": (shopify, "orders"),
    "tiktok": (tiktok_shop, "orders"),
}

# Which env var must be present for us to attempt a platform (any of the
# tuple's alternatives satisfies it).
REQUIRED_ENV = {
    "google": ("GOOGLE_ADS_DEVELOPER_TOKEN",),
    "meta": ("META_ACCESS_TOKEN",),
    "amazon": ("AMAZON_ADS_REFRESH_TOKEN",),
    "amazon_orders": ("SPAPI_REFRESH_TOKEN",),
    "shopify": ("SHOPIFY_CLIENT_SECRET", "SHOPIFY_ADMIN_TOKEN"),
    "tiktok": ("TIKTOK_ACCESS_TOKEN",),
}


def run(platforms: list[str], start_date: str, end_date: str) -> list[str]:
    unknown = [p for p in platforms if p not in CONNECTORS]
    if unknown:
        raise SystemExit(
            f"Unknown platform(s): {', '.join(unknown)}. "
            f"Valid choices: {', '.join(CONNECTORS)}"
        )
    db.init_db()
    print(f"Syncing {start_date} -> {end_date}")
    failures: list[str] = []
    for name in platforms:
        module, kind = CONNECTORS[name]
        if not any(os.environ.get(k) for k in REQUIRED_ENV[name]):
            print(f"  - {name:7s}  SKIPPED (no credentials in .env)")
            continue
        started = db.now()
        try:
            rows = module.sync(start_date, end_date)
            written = db.upsert_ad_metrics(rows) if kind == "ads" else db.upsert_orders(rows)
            db.log_sync(name, started, written, "ok")
            print(f"  - {name:7s}  OK     {written} rows")
        except Exception as e:  # noqa: BLE001
            failures.append(name)
            db.log_sync(name, started, 0, "error", str(e))
            print(f"  - {name:7s}  ERROR  {e}")
    return failures


def load_sample() -> None:
    """Populate the warehouse with fake data so you can test end-to-end."""
    db.init_db()
    today = date.today()
    ad_rows, order_rows = [], []
    for d in range(14):
        day = (today - timedelta(days=d)).isoformat()
        for plat in ("google", "meta", "amazon"):
            for c in range(3):
                spend = round(random.uniform(50, 500), 2)
                ad_rows.append({
                    "platform": plat, "account_id": "DEMO", "campaign_id": f"{plat}-{c}",
                    "campaign_name": f"{plat.title()} Campaign {c}", "date": day,
                    "impressions": random.randint(1000, 50000), "clicks": random.randint(20, 1500),
                    "spend": spend, "conversions": round(random.uniform(1, 40), 1),
                    "revenue": round(spend * random.uniform(1.5, 5), 2), "currency": "USD",
                })
        for o in range(random.randint(3, 8)):
            total = round(random.uniform(20, 180), 2)
            order_rows.append({
                "platform": "tiktok", "order_id": f"{day}-{o}", "order_date": day,
                "status": "COMPLETED", "sku": f"SKU{random.randint(100,999)}",
                "product_name": "Demo Product", "quantity": random.randint(1, 3),
                "total": total, "currency": "USD",
            })
    db.upsert_ad_metrics(ad_rows)
    db.upsert_orders(order_rows)
    db.log_sync("sample", db.now(), len(ad_rows) + len(order_rows), "ok", "demo data")
    print(f"Loaded {len(ad_rows)} ad rows and {len(order_rows)} order rows of sample data.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--only", help="comma list: google,meta,amazon,amazon_orders,tiktok")
    p.add_argument("--sample", action="store_true", help="load fake demo data")
    args = p.parse_args()

    if args.sample:
        load_sample()
    else:
        end = args.end or date.today().isoformat()
        start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()
        chosen = args.only.split(",") if args.only else list(CONNECTORS)
        failures = run([c.strip() for c in chosen], start, end)
        if failures:
            raise SystemExit("Connector failures: " + ", ".join(failures))
