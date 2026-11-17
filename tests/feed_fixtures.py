"""Shared fixtures for the feed-label tests: an in-memory warehouse built from
the real DDL (warehouse/schema.sql + catalog_sync.py), and row helpers."""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog_sync  # noqa: E402
from warehouse import db  # noqa: E402

TODAY = dt.date(2026, 6, 15)
SYNCED = "2026-06-15T00:00:00+00:00"

# Every setting feed_labels reads, blanked so a developer's own .env cannot
# change a test's outcome.
CLEAN_ENV = {k: "" for k in ("FEED_LABEL_SHIP_COST", "FEED_LABEL_DEDUCTION_RATE",
                             "FEED_LABEL_NEW_ARRIVAL_DAYS", "FEED_LABEL_VELOCITY_DAYS",
                             "FEED_LABEL_ORDER_PLATFORMS", "GMC_TARGET_COUNTRY",
                             "GMC_FEED_ID_FORMAT", "META_CATALOG_RETAILER_ID_FORMAT")}


def epoch(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    catalog_sync.ensure_schema(conn)
    return conn


def add_variant(conn, variant_id, product_id="1000000000001", sku="SKU-1001", status="ACTIVE",
                price=40.0, compare_at=None, cost=10.0, launch=None, tags="") -> None:
    conn.execute(
        "INSERT INTO catalog_shopify (variant_id, product_id, sku, title, unit_cost, status, "
        "price, compare_at_price, tags, launch_date_epoch, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (variant_id, product_id, sku, "Example Tee", cost, status, price, compare_at, tags,
         launch, SYNCED))


def add_order(conn, order_id, sku, qty, day=None, platform="shopify") -> None:
    day = day or TODAY
    conn.execute(
        "INSERT INTO orders (platform, order_id, order_date, sku, quantity, total, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (platform, order_id, day.isoformat(), sku, qty, 10.0 * qty, SYNCED))
