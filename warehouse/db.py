"""Tiny SQLite helper shared by the sync jobs and the MCP server."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# The database file lives next to this project. Override with env var if you like.
DB_PATH = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent.parent / "warehouse.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# How long a writer waits for the single WAL write lock before giving up with
# "database is locked". SQLite locks the whole DATABASE, not a table, so two
# writers ALWAYS contend no matter how disjoint their tables are.
#
# Size this to clear your LONGEST single write transaction, not your typical
# one. A bulk rebuild that replaces ~1M rows in one transaction can hold the
# lock for minutes, and any other job that starts during it dies — losing hours
# of work seconds before the rebuild would have committed. Lowering this makes
# nothing faster; it only converts waits into lost jobs. The real fix is
# keeping big rebuilds' exclusive window short.
#
# Read connections do not need it: in WAL mode a reader never blocks on a
# writer, which is why connect_readonly() below leaves the default alone.
BUSY_TIMEOUT_SECONDS = 300


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


def connect_readonly() -> sqlite3.Connection:
    """Open the DB so SQLite itself rejects writes — used by the MCP server."""
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# Columns added after the first release; applied to older tables on the fly.
MIGRATIONS = {
    "ad_metrics": [
        "campaign_type TEXT", "reach INTEGER", "link_clicks INTEGER",
        "add_to_carts REAL", "checkouts REAL", "all_conversions REAL",
        # Meta landing_page_view: link taps that actually rendered the page.
        # Always <= link_clicks; the gap is people who tapped and bailed
        # during load. Use THIS, not link_clicks, to compare against a
        # separate analytics tool's session count -- link_clicks alone
        # overstates real arrivals.
        "landing_page_views REAL",
        "search_impression_share REAL",
        # Google auction diagnostics, added 2026-08-05. ALL FIVE ARE RATIOS
        # (0-1) — AVG them, never SUM, same rule as search_impression_share.
        # The two lost-IS columns are the point: impression share alone says
        # you're missing impressions but not WHY, so nobody could tell a
        # budget-capped campaign from a bid/quality-capped one. budget + rank
        # lost IS sum with IS to ~1.0, which is also a useful sanity check.
        "search_budget_lost_impression_share REAL",
        "search_rank_lost_impression_share REAL",
        "search_click_share REAL",
        "absolute_top_impression_pct REAL",
        "top_impression_pct REAL",
    ],
    "orders": [
        "original_total REAL",  # pre-discount list price total (discount = original - total)
        "is_sample INTEGER",    # tiktok is_sample_order — affiliate sample sendouts
        "creator TEXT",         # tiktok buyer_nickname on sample orders (DISPLAY NAME)
        "creator_id TEXT",      # tiktok user_id on sample orders (STABLE id; join key
                                # once a handle<->user_id map exists — video feed has
                                # only the handle, so these don't bridge yet)
        "source TEXT",          # shopify source_name (e.g. web, tiktok, amazon-us, pos, ...)
    ],
}


def init_db() -> None:
    """Create tables if they don't exist yet. Safe to run repeatedly."""
    conn = connect()
    # WAL is load-bearing, not a tuning knob: the MCP server reads this file
    # while a sync writes it, and in SQLite's default `delete` journal a reader
    # and a writer block each other, so a query would stall behind the nightly
    # sync. connect_readonly() deliberately sets no busy timeout because it
    # assumes WAL -- without this line that assumption is simply false.
    #
    # Set OUTSIDE the `with conn:` block below: journal_mode cannot change
    # inside a transaction. It is a persistent property of the database file,
    # so this runs once and sticks; running it again is a cheap no-op.
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if mode.lower() != "wal":
        # Network filesystems reject WAL and report the mode they kept rather
        # than raising, which would otherwise be a silent, invisible downgrade.
        print(f"WARNING: could not enable WAL (journal_mode={mode}); readers "
              "and writers will block each other. Is the database on a network "
              "share?")
    with conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, col_defs in MIGRATIONS.items():
            existing = {c[1] for c in conn.execute(f"PRAGMA table_info({table})")}
            for col_def in col_defs:
                if col_def.split()[0] not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    conn.close()


# Extended ad_metrics columns default to NULL for connectors that don't set them.
_AD_EXTENDED_DEFAULTS = {
    "campaign_type": None, "reach": None, "link_clicks": None,
    "add_to_carts": None, "checkouts": None, "all_conversions": None,
    "landing_page_views": None,
    "search_impression_share": None,
    "search_budget_lost_impression_share": None,
    "search_rank_lost_impression_share": None,
    "search_click_share": None,
    "absolute_top_impression_pct": None,
    "top_impression_pct": None,
}


def upsert_ad_metrics(rows: list[dict]) -> int:
    """Insert or replace daily ad-performance rows. Returns count written."""
    if not rows:
        return 0
    stamp = now()
    conn = connect()
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO ad_metrics
              (platform, account_id, campaign_id, campaign_name, date,
               impressions, clicks, spend, conversions, revenue, currency, synced_at,
               campaign_type, reach, link_clicks, add_to_carts, checkouts,
               all_conversions, landing_page_views, search_impression_share,
               search_budget_lost_impression_share, search_rank_lost_impression_share,
               search_click_share, absolute_top_impression_pct, top_impression_pct)
            VALUES
              (:platform, :account_id, :campaign_id, :campaign_name, :date,
               :impressions, :clicks, :spend, :conversions, :revenue, :currency, :synced_at,
               :campaign_type, :reach, :link_clicks, :add_to_carts, :checkouts,
               :all_conversions, :landing_page_views, :search_impression_share,
               :search_budget_lost_impression_share, :search_rank_lost_impression_share,
               :search_click_share, :absolute_top_impression_pct, :top_impression_pct)
            """,
            [{**_AD_EXTENDED_DEFAULTS, **r, "synced_at": stamp} for r in rows],
        )
    conn.close()
    return len(rows)


def upsert_orders(rows: list[dict]) -> int:
    if not rows:
        return 0
    stamp = now()
    conn = connect()
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO orders
              (platform, order_id, order_date, status, sku, product_name,
               quantity, total, currency, synced_at, original_total, is_sample, creator, creator_id, source)
            VALUES
              (:platform, :order_id, :order_date, :status, :sku, :product_name,
               :quantity, :total, :currency, :synced_at, :original_total, :is_sample, :creator, :creator_id, :source)
            """,
            [{"original_total": None, "is_sample": None, "creator": None, "creator_id": None, "source": None,
              **r, "synced_at": stamp} for r in rows],
        )
    conn.close()
    return len(rows)


def upsert_shopify_discounts(rows: list[dict]) -> int:
    """Insert or replace per-line promo discount attribution rows.

    Written as a side effect by the Shopify connector (both the incremental
    sync and the bulk-backfill parse) so the discount stream can never drift
    out of step with the `orders` rows it explains."""
    if not rows:
        return 0
    stamp = now()
    conn = connect()
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO shopify_order_discounts
              (order_id, sku, order_date, kind, code, amount,
               allocation_method, target_selection, synced_at)
            VALUES
              (:order_id, :sku, :order_date, :kind, :code, :amount,
               :allocation_method, :target_selection, :synced_at)
            """,
            [{**r, "synced_at": stamp} for r in rows],
        )
    conn.close()
    return len(rows)


def upsert_shopify_order_customers(rows: list[dict]) -> int:
    """Insert or replace the Shopify customer id for each order.

    Written as a side effect by the Shopify connector (both the incremental sync
    and the bulk-backfill parse), same as upsert_shopify_discounts, so the
    customer stream can never drift out of step with the `orders` rows it keys.

    Only ever called when the app holds read_customers; see
    warehouse/connectors/shopify.py customer_capture_enabled()."""
    if not rows:
        return 0
    stamp = now()
    conn = connect()
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO shopify_order_customers
              (order_id, customer_id, order_date, synced_at)
            VALUES
              (:order_id, :customer_id, :order_date, :synced_at)
            """,
            [{**r, "synced_at": stamp} for r in rows],
        )
    conn.close()
    return len(rows)


def log_sync(platform: str, started_at: str, rows_written: int, status: str, message: str = "") -> None:
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO sync_log (platform, started_at, finished_at, rows_written, status, message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (platform, started_at, now(), rows_written, status, message[:500]),
        )
    conn.close()
