r"""
Shared read-only reader for Amazon Warehousing & Distribution (AWD) stock.

Written to be the ONE place that answers "how much extra AWD stock does this
SKU have" — any report that touches Amazon inventory should go through this
module rather than querying `amazon_awd_inventory` directly, so the
double-counting trap documented below only has to be gotten right once
instead of separately by every consumer.

No writes, no API calls, no writer lock. The feed is `amazon_awd_sync.py`.

WHAT COUNTS AS AWD STOCK — the one rule that matters:
    AWD units usable as EXTRA stock = `available_distributable`, NEVER
    `total_onhand`.
`reserved_distributable` and `replenishment_qty` are units already committed
to FBA (allocated to a distribution order, or already in transit AWD->FBA)
and typically reappear FBA-side as inbound quantity for the same SKU — adding
them to an FBA position double-counts the same physical units mid-transfer.
Full field semantics are in `amazon_awd_sync.py`'s module docstring.
`available()` is the only accessor most callers need; the other buckets are
exposed on the loaded pool for display/audit only.

WHERE AWD SITS IN A REPLENISHMENT DECISION. AWD is Amazon's OWN bulk-storage
tier, already inside Amazon's fulfillment network — so for an FBA stock-out
it is typically the closest, cheapest source to draw from, ahead of
transferring stock from an outside warehouse/3PL or placing a fresh factory
order. Before this feed exists, a SKU that's empty in FBA and empty in every
other pool a report knows about reads as "reorder now" even when AWD is
sitting on a four-figure quantity one transfer away.

MISSING IS NOT ZERO. The table doesn't exist until `amazon_awd_sync.py` has
run at least once, and this is a snapshot-only feed (the API exposes today's
levels and nothing historical), so a stalled sync goes stale silently. Every
loader therefore returns a `status` the caller MUST surface — `note()`
renders it as a one-line footer — because a bare zero is ambiguous between
"AWD holds nothing" and "we never checked", and this feed exists specifically
to close that kind of blind spot.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

TABLE = "amazon_awd_inventory"

# A snapshot older than this is reported as stale rather than quietly used.
STALE_DAYS = 3
# Week-over-week comparison: snapshot nearest cur-7d, within this tolerance.
WOW_TOLERANCE_DAYS = 2

STATUS_OK = "ok"
STATUS_MISSING = "missing"      # table absent, or present but empty
STATUS_STALE = "stale"          # newest snapshot older than STALE_DAYS


def table_exists(conn: sqlite3.Connection) -> bool:
    """True if the AWD table exists.

    Checks `sqlite_master` for BOTH `type='table'` and `type='view'` — a
    `type='table'`-only filter would silently treat a view-backed source as
    absent, which is an easy way to make a report think a feed never ran when
    it actually has, just fronted by a view.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')",
        (TABLE,)).fetchone() is not None


def latest_snapshot(conn: sqlite3.Connection, as_of: str | None = None) -> str | None:
    if not table_exists(conn):
        return None
    if as_of:
        return conn.execute(
            f"SELECT MAX(snapshot_date) FROM {TABLE} WHERE snapshot_date<=?",
            (as_of,)).fetchone()[0]
    return conn.execute(f"SELECT MAX(snapshot_date) FROM {TABLE}").fetchone()[0]


def prior_snapshot(conn: sqlite3.Connection, cur: str) -> str | None:
    """Snapshot nearest cur-7d within +/-WOW_TOLERANCE_DAYS, strictly < cur.

    Use the same rule and tolerance for any other inventory pool's
    week-over-week column (e.g. an FBA or 3PL snapshot table) so the deltas
    line up on the same report row instead of comparing mismatched dates.
    """
    if not cur or not table_exists(conn):
        return None
    target = (date.fromisoformat(cur) - timedelta(days=7)).isoformat()
    row = conn.execute(
        f"""SELECT snapshot_date FROM {TABLE}
            WHERE snapshot_date BETWEEN date(?, '-{WOW_TOLERANCE_DAYS} day')
                                    AND date(?, '+{WOW_TOLERANCE_DAYS} day')
              AND snapshot_date < ?
            GROUP BY snapshot_date
            ORDER BY ABS(julianday(snapshot_date) - julianday(?)) LIMIT 1""",
        (target, target, cur, target)).fetchone()
    return row[0] if row else None


def status(conn: sqlite3.Connection, snap: str | None, as_of: str | None = None) -> str:
    """STATUS_OK / STATUS_MISSING / STATUS_STALE for the given snapshot."""
    if not snap:
        return STATUS_MISSING
    ref = date.fromisoformat(as_of) if as_of else date.today()
    return STATUS_OK if (ref - date.fromisoformat(snap)).days <= STALE_DAYS else STATUS_STALE


def empty() -> dict:
    """The shape load() returns when there is no AWD data.

    `by_sku` is an empty dict rather than absent so callers can `.get(sku, 0)`
    without branching first; `status` is what tells them the difference
    between "AWD holds nothing" and "we have never looked".
    """
    return {"by_sku": {}, "available": 0, "onhand": 0, "inbound": 0,
            "reserved": 0, "replenishment": 0, "skus": 0,
            "skus_with_stock": 0, "snapshot": None, "status": STATUS_MISSING}


def load(conn: sqlite3.Connection, snap: str | None, as_of: str | None = None) -> dict:
    """SKU-keyed AWD position for one snapshot.

    `by_sku`: sku -> dict(available, onhand, inbound, reserved, replenishment).
    """
    if not snap or not table_exists(conn):
        return empty()

    by_sku: dict[str, dict] = {}
    for sku, onhand, inbound, avail, reserved, repl in conn.execute(
            f"""SELECT sku, total_onhand, total_inbound,
                       available_distributable, reserved_distributable,
                       replenishment_qty
                FROM {TABLE} WHERE snapshot_date=?""", (snap,)):
        by_sku[sku] = {
            "available": avail or 0, "onhand": onhand or 0,
            "inbound": inbound or 0, "reserved": reserved or 0,
            "replenishment": repl or 0,
        }

    return {
        "by_sku": by_sku,
        "available": sum(v["available"] for v in by_sku.values()),
        "onhand": sum(v["onhand"] for v in by_sku.values()),
        "inbound": sum(v["inbound"] for v in by_sku.values()),
        "reserved": sum(v["reserved"] for v in by_sku.values()),
        "replenishment": sum(v["replenishment"] for v in by_sku.values()),
        "skus": len(by_sku),
        "skus_with_stock": sum(1 for v in by_sku.values() if v["available"] > 0),
        "snapshot": snap,
        "status": status(conn, snap, as_of),
    }


def load_latest(conn: sqlite3.Connection, as_of: str | None = None) -> dict:
    """Convenience: newest snapshot on or before `as_of` (or overall)."""
    return load(conn, latest_snapshot(conn, as_of), as_of)


def available(awd: dict, sku: str | None) -> int:
    """AWD units usable as extra stock for one SKU. THE accessor to call."""
    if not sku:
        return 0
    return (awd.get("by_sku", {}).get(sku) or {}).get("available", 0)


def available_for_skus(awd: dict, skus) -> int:
    """Summed AWD availability over an iterable of SKUs.

    Dedupes first: a caller rolling several SKU variants up to one product
    may hand the same SKU in twice, and double-counting stock is the one
    error this whole module exists to avoid.
    """
    return sum(available(awd, s) for s in set(filter(None, skus)))


def by_asin(conn: sqlite3.Connection, awd: dict, inv_snapshot: str | None = None) -> dict:
    """{asin: available AWD units}, bridged through `amazon_inventory`.

    AWD's own API reports no ASIN at all — only the seller SKU — so the
    bridge is your FBA inventory table's own sku->asin mapping (this project's
    `amazon_inventory_sync.py`, if you're using it) on its latest snapshot.
    Values are summed per ASIN in case more than one AWD SKU maps to the same
    ASIN on your account; verify that assumption holds for your catalog
    before trusting a naive per-ASIN total at scale.
    """
    if not awd.get("by_sku"):
        return {}
    snap = inv_snapshot or conn.execute(
        "SELECT MAX(snapshot_date) FROM amazon_inventory").fetchone()[0]
    if not snap:
        return {}
    sku_asin = dict(conn.execute(
        "SELECT sku, asin FROM amazon_inventory "
        "WHERE snapshot_date=? AND asin IS NOT NULL AND asin!=''", (snap,)))
    out: dict[str, int] = {}
    for sku, vals in awd["by_sku"].items():
        asin = sku_asin.get(sku)
        if asin:
            out[asin] = out.get(asin, 0) + vals["available"]
    return out


def note(awd: dict) -> str:
    """One-line footer describing AWD coverage — ALWAYS render this.

    States plainly when the feed is missing or stale, because a zero that
    means "never synced" and a zero that means "no AWD stock" lead to
    opposite inventory decisions.
    """
    st = awd.get("status")
    if st == STATUS_MISSING:
        return ("Amazon AWD (bulk storage upstream of FBA): NO DATA — "
                "amazon_awd_sync.py has not run, so AWD columns are blank, NOT zero. "
                "Any 'out of stock' read below may be wrong.")
    base = (f"Amazon AWD snapshot {awd['snapshot']}: {awd['available']:,} units "
            f"available-to-distribute across {awd['skus_with_stock']:,} SKUs "
            f"(on-hand {awd['onhand']:,}; {awd['reserved']:,} reserved and "
            f"{awd['replenishment']:,} in transit to FBA are EXCLUDED — they are "
            "already committed and typically counted again in FBA inbound).")
    if st == STATUS_STALE:
        base += (f" !! STALE — snapshot is more than {STALE_DAYS} days old; "
                 "the AWD sync may have stopped.")
    return base
