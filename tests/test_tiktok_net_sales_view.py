"""Pins the tiktok_weekly_product net-sales correction (VIEW_DDL in
tiktok_finance_sync.py).

orders.total nets out BOTH the seller-funded and the platform-funded discount,
but the platform reimburses the seller for its own piece. The view adds it
back with this precedence: line-level orders.platform_discount (exact) ->
settlement platform_discount_amount pro rata across the order's lines ->
raw total. An order with neither source must NOT be treated as zero discount.
All figures below are illustrative, not real orders.
"""
from __future__ import annotations

import sqlite3

import tiktok_finance_sync as tf
from warehouse import db


def _conn():
    conn = sqlite3.connect(":memory:")
    # Build `orders` from the real schema + migrations so the test follows
    # any future column change instead of pinning a hand-copied DDL.
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, col_defs in db.MIGRATIONS.items():
        existing = {c[1] for c in conn.execute(f"PRAGMA table_info({table})")}
        for col_def in col_defs:
            if col_def.split()[0] not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    tf.ensure_schema(conn)
    tf.ensure_view(conn)
    return conn


def _order(conn, order_id, sku, total, original_total=None, is_sample=0,
           order_date="2026-01-07", status="DELIVERED", platform_discount=None):
    conn.execute(
        "INSERT INTO orders (platform, order_id, order_date, status, sku, product_name, "
        "quantity, total, currency, synced_at, original_total, is_sample, platform_discount) "
        "VALUES ('tiktok', ?, ?, ?, ?, 'p', 1, ?, 'USD', 'now', ?, ?, ?)",
        (order_id, order_date, status, sku, total,
         original_total if original_total is not None else total, is_sample, platform_discount))


def _settlement(conn, statement_id, order_id, platform_discount_amount, seller_discount_amount=0.0):
    conn.execute(
        "INSERT INTO tiktok_settlement_orders (statement_id, order_id, "
        "platform_discount_amount, seller_discount_amount, synced_at) VALUES (?, ?, ?, ?, 'now')",
        (statement_id, order_id, platform_discount_amount, seller_discount_amount))


def _one(conn, cols, sku):
    return conn.execute(f"SELECT {cols} FROM tiktok_weekly_product WHERE sku = ?", (sku,)).fetchone()


def test_settled_single_sku_order_adds_back_platform_discount():
    # gross 40, seller discount 10 -> platform net 30; shopper paid 26 (30 - 4 platform)
    conn = _conn()
    _order(conn, "O1", "A", total=26.0, original_total=40.0)
    _settlement(conn, "S1", "O1", platform_discount_amount=-4.0, seller_discount_amount=-10.0)
    assert _one(conn, "net_sales, discounts", "A") == (30.0, 10.0)


def test_unsettled_order_falls_back_to_total_unmodified():
    conn = _conn()
    _order(conn, "O2", "B", total=26.0, original_total=40.0)
    assert _one(conn, "net_sales, pd_known_sales", "B") == (26.0, 0.0)


def test_multi_sku_order_splits_settlement_proportionally():
    # 75 + 25 = 100 order total, -20 platform discount -> 90 / 30
    conn = _conn()
    _order(conn, "O3", "C", total=75.0)
    _order(conn, "O3", "D", total=25.0)
    _settlement(conn, "S3", "O3", platform_discount_amount=-20.0)
    rows = dict(conn.execute("SELECT sku, net_sales FROM tiktok_weekly_product WHERE sku IN ('C','D')"))
    assert rows == {"C": 90.0, "D": 30.0}


def test_multiple_settlement_rows_are_summed_not_maxed():
    conn = _conn()
    _order(conn, "O4", "E", total=26.0, original_total=40.0)
    _settlement(conn, "S4a", "O4", platform_discount_amount=-3.0)
    _settlement(conn, "S4b", "O4", platform_discount_amount=-1.0)
    assert _one(conn, "net_sales", "E") == (30.0,)


def test_sample_order_untouched_by_correction():
    conn = _conn()
    _order(conn, "O5", "F", total=0.0, original_total=0.0, is_sample=1)
    _settlement(conn, "S5", "O5", platform_discount_amount=-2.0)
    assert _one(conn, "net_sales, sample_units, sample_orders", "F") == (0.0, 1, 1)


def test_line_level_platform_discount_used_without_settlement():
    conn = _conn()
    _order(conn, "O6", "G", total=26.0, original_total=40.0, platform_discount=4.0)
    assert _one(conn, "net_sales, discounts, platform_discount, shopper_paid, pd_known_sales",
                "G") == (30.0, 10.0, 4.0, 26.0, 30.0)


def test_line_level_wins_over_settlement_pro_rata():
    # pro rata would give 99 / 11; the exact per-line split must win, and the
    # settlement must not be added on top of it (double count)
    conn = _conn()
    _order(conn, "O7", "H", total=90.0, platform_discount=10.0)
    _order(conn, "O7", "I", total=10.0, platform_discount=0.0)
    _settlement(conn, "S7", "O7", platform_discount_amount=-10.0)
    rows = dict(conn.execute("SELECT sku, net_sales FROM tiktok_weekly_product WHERE sku IN ('H','I')"))
    assert rows == {"H": 100.0, "I": 10.0}


def test_measured_zero_is_known_but_null_is_not():
    conn = _conn()
    _order(conn, "O8", "J", total=20.0, platform_discount=0.0)
    _order(conn, "O9", "J", total=30.0)  # NULL and unsettled
    assert _one(conn, "net_sales, platform_discount, pd_known_sales", "J") == (50.0, 0.0, 20.0)


def test_cancelled_orders_excluded_and_weeks_start_monday():
    conn = _conn()
    _order(conn, "O10", "K", total=10.0, order_date="2026-01-11")  # Sunday -> week of Mon 01-05
    _order(conn, "O11", "K", total=99.0, status="CANCELLED")
    assert conn.execute("SELECT week_start, net_sales FROM tiktok_weekly_product "
                        "WHERE sku='K'").fetchall() == [("2026-01-05", 10.0)]


def test_ensure_view_is_idempotent():
    conn = _conn()
    tf.ensure_view(conn)
    tf.ensure_view(conn)
