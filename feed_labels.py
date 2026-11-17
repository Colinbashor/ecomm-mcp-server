"""
Product custom labels for ad feeds, computed from the warehouse.

Pure label logic shared by merchant_center_labels_export.py (a Google Merchant
Center supplemental feed) and meta_catalog_labels_sync.py (the same labels
written onto a Meta catalog), so the two channels can never disagree about
what a label means. Nothing here talks to a network.

INPUTS: `catalog_shopify` (catalog_sync.py) for price, cost, markdown and
launch date, and the shared `orders` table for sales velocity. Only ACTIVE
Shopify products are labelled: archived duplicates of a live product share its
SKU, and a draft is not on sale.

THE FIVE LABELS

  custom_label_0  net margin band. Per unit:
                    (price - unit_cost - FEED_LABEL_SHIP_COST) / price
                    - FEED_LABEL_DEDUCTION_RATE
                  i.e. cost of goods and a flat per-unit shipping cost, then a
                  price-proportional deduction (card processing, average
                  discount-code leakage). Bands are MARGIN_BANDS. A missing or
                  implausible cost gets UNKNOWN_MARGIN_LABEL, never a band:
                  guessing would put a product with no cost record into the
                  best-margin band. A negative result gets its own label.
  custom_label_1  markdown band from price vs compare_at_price.
  custom_label_2  new arrival: launched within FEED_LABEL_NEW_ARRIVAL_DAYS.
                  BLANK when launch_date_epoch is unknown (a blank is not
                  "established"; we could not look).
  custom_label_3  sales velocity over the last FEED_LABEL_VELOCITY_DAYS of
                  `orders`, by RANK of units among SKUs that sold (see
                  velocity_band). BLANK for every product when the window
                  holds no orders at all, because that means the order feed is
                  not syncing, not that nothing sold.
  custom_label_4  left blank, reserved for your own use.

SKU IS NOT UNIQUE in catalog_shopify (archived duplicates, re-listings, and
sometimes two live variants sharing one SKU). Rows are keyed by variant_id; a
SKU held by more than one ACTIVE variant is flagged `sku_ambiguous`, and
feed_id() refuses to emit a SKU-format id for it, since a feed id that names
two products attaches labels to whichever one the platform happens to pick.

FEED IDS. The id column of a supplemental feed must match the PRIMARY feed's
ids exactly, or every row silently attaches to nothing. GMC_FEED_ID_FORMAT
picks the format (see FEED_ID_FORMATS); `shopify` is the format Shopify's own
Google & YouTube channel emits: shopify_<COUNTRY>_<product_id>_<variant_id>.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from collections import Counter

LABEL_COLUMNS = ("custom_label_0", "custom_label_1", "custom_label_2",
                 "custom_label_3", "custom_label_4")

# --------------------------------------------------------------------- margin
# (floor, label), checked top-down. Generic cut points; change them here if
# your bid strategy wants different breaks. Renaming a label is a breaking
# change for any ad filter that matches its TEXT.
MARGIN_BANDS = (
    (0.50, "Margin 50%+"),
    (0.40, "Margin 40-50%"),
    (0.30, "Margin 30-40%"),
    (0.20, "Margin 20-30%"),
    (0.10, "Margin 10-20%"),
    (0.00, "Margin 0-10%"),
)
NEGATIVE_MARGIN_LABEL = "Negative margin"
UNKNOWN_MARGIN_LABEL = "Unknown margin"
# A cost above this multiple of the price is treated as a broken cost record
# (a case cost entered as a unit cost, a currency slip), not a real loss.
SUSPECT_COST_MULTIPLE = 2.0

DEFAULT_SHIP_COST = 0.0
DEFAULT_DEDUCTION_RATE = 0.03

# ------------------------------------------------------------------- markdown
DEEP_MARKDOWN = 0.25            # >= 25% below compare-at = deep
FULL_PRICE_LABEL = "Full price"
LIGHT_MARKDOWN_LABEL = "Light markdown (<25% off)"
DEEP_MARKDOWN_LABEL = "Deep markdown (25%+ off)"

# ---------------------------------------------------------------- new arrival
DEFAULT_NEW_ARRIVAL_DAYS = 30
NEW_ARRIVAL_LABEL = "New arrival"
ESTABLISHED_LABEL = "Established"

# ------------------------------------------------------------------- velocity
DEFAULT_VELOCITY_DAYS = 30
DEFAULT_ORDER_PLATFORMS = ("shopify",)
TOP_SELLER_SHARE = 0.10         # top 10% of selling SKUs by units
STEADY_SELLER_SHARE = 0.50      # the next tier, down to the top 50%
TOP_SELLER_LABEL = "Top seller"
STEADY_SELLER_LABEL = "Steady seller"
SLOW_SELLER_LABEL = "Slow seller"
NO_SALES_LABEL = "No recent sales"

# -------------------------------------------------------------------- feed id
FEED_ID_FORMATS = ("sku", "variant_id", "shopify")
DEFAULT_COUNTRY = "US"


# ----------------------------------------------------------------- env access
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {raw!r}") from None


def ship_cost() -> float:
    return _env_float("FEED_LABEL_SHIP_COST", DEFAULT_SHIP_COST)


def deduction_rate() -> float:
    return _env_float("FEED_LABEL_DEDUCTION_RATE", DEFAULT_DEDUCTION_RATE)


def new_arrival_days() -> int:
    return _env_int("FEED_LABEL_NEW_ARRIVAL_DAYS", DEFAULT_NEW_ARRIVAL_DAYS)


def velocity_days() -> int:
    return _env_int("FEED_LABEL_VELOCITY_DAYS", DEFAULT_VELOCITY_DAYS)


def order_platforms() -> tuple[str, ...]:
    raw = os.environ.get("FEED_LABEL_ORDER_PLATFORMS", "").strip()
    return tuple(p.strip() for p in raw.split(",") if p.strip()) or DEFAULT_ORDER_PLATFORMS


def target_country() -> str:
    return (os.environ.get("GMC_TARGET_COUNTRY", "").strip() or DEFAULT_COUNTRY).upper()


def feed_id_format(var: str = "GMC_FEED_ID_FORMAT", default: str | None = None) -> str:
    """The configured id format. No default for Merchant Center on purpose: a
    wrong guess produces a feed whose rows match nothing, with no error."""
    raw = os.environ.get(var, "").strip().lower() or (default or "")
    if raw not in FEED_ID_FORMATS:
        raise SystemExit(f"{var} must be one of {', '.join(FEED_ID_FORMATS)} "
                         f"(got {raw!r}). See .env.example.")
    return raw


# --------------------------------------------------------------------- labels
def net_margin_rate(price, cost, ship: float, deduct: float) -> float | None:
    """Net margin as a share of price, or None when cost is missing/broken."""
    if not price or not cost or price <= 0 or cost <= 0:
        return None
    if cost > price * SUSPECT_COST_MULTIPLE:
        return None
    return 1.0 - (cost + (ship or 0.0)) / price - deduct


def margin_band(price, cost, ship: float, deduct: float) -> str:
    """custom_label_0. `ship` and `deduct` are required arguments on purpose:
    a caller that forgot them would emit net-labelled bands from gross math
    and nothing would look wrong."""
    rate = net_margin_rate(price, cost, ship, deduct)
    if rate is None:
        return UNKNOWN_MARGIN_LABEL
    if rate < 0:
        return NEGATIVE_MARGIN_LABEL
    for floor, label in MARGIN_BANDS:
        if rate >= floor:
            return label
    return NEGATIVE_MARGIN_LABEL          # unreachable: the last floor is 0


def markdown_band(price, compare_at_price) -> str:
    """custom_label_1. A compare-at at or below price is not a markdown."""
    if not price or not compare_at_price or compare_at_price <= 0:
        return FULL_PRICE_LABEL
    depth = 1.0 - price / compare_at_price
    if depth <= 0:
        return FULL_PRICE_LABEL
    return DEEP_MARKDOWN_LABEL if depth >= DEEP_MARKDOWN else LIGHT_MARKDOWN_LABEL


def new_arrival_band(launch_epoch, today: dt.date, days: int) -> str:
    """custom_label_2. Blank when the launch date is unknown."""
    if not launch_epoch:
        return ""
    launched = dt.datetime.fromtimestamp(int(launch_epoch), tz=dt.timezone.utc).date()
    return NEW_ARRIVAL_LABEL if (today - launched).days <= days else ESTABLISHED_LABEL


def velocity_thresholds(units_by_sku: dict[str, float]) -> tuple[float, float]:
    """(top_min_units, steady_min_units) from the RANK of selling SKUs.

    Rank rather than fixed unit counts so the bands mean the same thing on a
    store selling 50 units a month and one selling 50,000. The cut is taken as
    the unit count AT the cutoff rank, so SKUs tied on units always share a
    band instead of being split by sort order."""
    sold = sorted((u for u in units_by_sku.values() if u > 0), reverse=True)
    if not sold:
        return float("inf"), float("inf")
    top_i = max(1, round(len(sold) * TOP_SELLER_SHARE)) - 1
    steady_i = max(1, round(len(sold) * STEADY_SELLER_SHARE)) - 1
    return sold[top_i], sold[steady_i]


def velocity_band(units: float, thresholds: tuple[float, float]) -> str:
    """custom_label_3."""
    top, steady = thresholds
    if units <= 0:
        return NO_SALES_LABEL
    if units >= top:
        return TOP_SELLER_LABEL
    if units >= steady:
        return STEADY_SELLER_LABEL
    return SLOW_SELLER_LABEL


# ----------------------------------------------------------------------- data
def _norm_sku(sku) -> str:
    return str(sku or "").strip().upper()


def active_variants(conn: sqlite3.Connection) -> list[dict]:
    """Every ACTIVE catalog_shopify variant, with `sku_ambiguous` set when its
    SKU is shared by another ACTIVE variant."""
    cur = conn.execute(
        "SELECT variant_id, product_id, sku, price, compare_at_price, unit_cost, "
        "launch_date_epoch, tags FROM catalog_shopify "
        "WHERE UPPER(COALESCE(status, '')) = 'ACTIVE' AND variant_id IS NOT NULL "
        "ORDER BY product_id, variant_id")
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    counts = Counter(_norm_sku(r["sku"]) for r in rows if _norm_sku(r["sku"]))
    for r in rows:
        r["variant_id"] = str(r["variant_id"]).strip()
        r["product_id"] = str(r["product_id"] or "").strip()
        r["sku"] = str(r["sku"] or "").strip()
        r["sku_ambiguous"] = counts.get(_norm_sku(r["sku"]), 0) > 1
    return rows


def units_by_sku(conn: sqlite3.Connection, since: dt.date,
                 platforms: tuple[str, ...]) -> dict[str, float] | None:
    """Units per normalized SKU since `since`, or None when the window holds no
    orders at all for these platforms (feed not syncing: unknown, not zero).

    No status filter: what a status means differs by platform (AGENTS.md data
    rule 9), and cancelled lines are a small share of velocity. Filter here if
    your order feed says otherwise."""
    marks = ",".join("?" for _ in platforms)
    rows = conn.execute(
        f"SELECT sku, SUM(quantity) FROM orders WHERE platform IN ({marks}) "
        f"AND order_date >= ? GROUP BY sku",
        (*platforms, since.isoformat())).fetchall()
    if not rows:
        return None
    out: dict[str, float] = {}
    for sku, qty in rows:
        key = _norm_sku(sku)
        if key:
            out[key] = out.get(key, 0.0) + float(qty or 0)
    return out


def build_label_rows(conn: sqlite3.Connection, today: dt.date | None = None) -> list[dict]:
    """One dict per ACTIVE variant: variant_id, product_id, sku, sku_ambiguous,
    and custom_label_0..4. Settings come from the FEED_LABEL_* env vars."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    ship, deduct = ship_cost(), deduction_rate()
    arrival_days = new_arrival_days()
    units = units_by_sku(conn, today - dt.timedelta(days=velocity_days()), order_platforms())
    thresholds = velocity_thresholds(units or {})

    out = []
    for v in active_variants(conn):
        label3 = ("" if units is None
                  else velocity_band(units.get(_norm_sku(v["sku"]), 0.0), thresholds))
        out.append({
            "variant_id": v["variant_id"],
            "product_id": v["product_id"],
            "sku": v["sku"],
            "sku_ambiguous": v["sku_ambiguous"],
            "custom_label_0": margin_band(v["price"], v["unit_cost"], ship, deduct),
            "custom_label_1": markdown_band(v["price"], v["compare_at_price"]),
            "custom_label_2": new_arrival_band(v["launch_date_epoch"], today, arrival_days),
            "custom_label_3": label3,
            "custom_label_4": "",
        })
    return out


# -------------------------------------------------------------------- feed id
def feed_id(row: dict, fmt: str, country: str) -> str | None:
    """The id this variant carries in the primary feed, or None when it cannot
    be named unambiguously in that format."""
    if fmt == "sku":
        if not row.get("sku") or row.get("sku_ambiguous"):
            return None
        return row["sku"]
    if fmt == "variant_id":
        return row.get("variant_id") or None
    if fmt == "shopify":
        if not (row.get("product_id") and row.get("variant_id")):
            return None
        return f"shopify_{country.upper()}_{row['product_id']}_{row['variant_id']}"
    raise ValueError(f"unknown feed id format {fmt!r}")


def keyed_rows(rows: list[dict], fmt: str, country: str) -> tuple[list[tuple[str, dict]], int]:
    """[(feed_id, row)] with unnameable and duplicate ids dropped, plus how
    many rows were dropped. Ids are compared case-insensitively because
    Merchant Center ids are."""
    seen: set[str] = set()
    out, dropped = [], 0
    for r in rows:
        fid = feed_id(r, fmt, country)
        if fid is None or fid.upper() in seen:
            dropped += 1
            continue
        seen.add(fid.upper())
        out.append((fid, r))
    return out, dropped


def label_counts(rows: list[dict]) -> dict[str, Counter]:
    """Per label column, how many rows carry each value: a quick sanity print."""
    return {c: Counter(r[c] for r in rows) for c in LABEL_COLUMNS}
