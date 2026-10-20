"""
Amazon listing-quality priority audit -> CSV.

Reads the tables amazon_listing_quality_sync.py writes plus amazon_traffic_weekly
(amazon_traffic_sync.py) and ranks penalized ASINs by volume-at-risk: units x
severity, plus a "quick wins" cut of low-effort listings that already sell.

ROLL UP TO ASIN, NOT SELLER SKU. Units and sessions live at ASIN grain, issues
are diagnosed per seller SKU, and an FBA offer and an MFN offer of one product
are separate SKUs. Joining traffic to every sibling SKU row would count that
ASIN's demand once per SKU. Each ASIN's WORST offer (severity, then issue
count) is the representative row, and that offer's issues are what the CSV
reports.

MIRROR / SECONDARY-STOREFRONT SKUs. If some of your SKUs are mirrors of a
primary offer (a global-store programme, say), set AMAZON_MIRROR_SKU_PREFIX to
their prefix. A primary offer then always wins the representative pick when one
exists, so a defect that only a mirror carries cannot inflate a clean primary
listing's severity. Unset (the default) means no SKU is treated as a mirror.

SUPPRESSED LISTINGS NEED A DIFFERENT VOLUME BASIS. A listing that lost
DISCOVERABLE status cannot be selling now, so its trailing units understate
what is at risk. The score basis is MAX(units, units in the prior window) for
every row, which protects a just-suppressed listing without a special case.

FIXABILITY is a judgement from the SP-API contract and the issue message text,
not from a live write test. `Yes` = every issue code has a documented
PATCH-able fix; `Partial` = some need a hosting/image change or an appeal;
`No (Seller Central only)` = only the Seller Central UI can fix it;
`Unknown` = a code this script has not classified. Treat it as a triage hint.

Code 101265 (the "switch to FBA" nudge) is excluded: not a content defect.

USAGE:
  python amazon_listing_quality_priority.py
  python amazon_listing_quality_priority.py --days 90 --out-dir reports
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from warehouse import db as warehouse_db

load_dotenv()

MAIN_NAME = "amazon_listing_quality_priority_audit.csv"
QUICK_NAME = "amazon_listing_quality_quick_wins.csv"

SEVERITY_WEIGHT = {"SUPPRESSED": 3, "ERROR": 2, "WARNING": 1}
TIER_NAMES = {3: "SUPPRESSED", 2: "ERROR", 1: "WARNING"}
NON_DEFECT_CODES = ("101265",)

# Amazon issue codes whose message text names a fix. Codes are Amazon's, not
# yours, so the lists are shared by every seller.
API_FIXABLE_CODES = {
    "18448",     # missing attribute (material, theme, ...): plain value fill-in
    "99022",     # apparel_size sub-attribute missing a value
    "90226",     # 'Style' value shorter than the allowed minimum
    "90004401",  # apparel_size.size_class missing a value
    "90004201", "90004200", "99016",  # fabric type wrong format / duplicated
    "90220",     # 'Are batteries required?' missing
    "90225",     # item highlight over the max length
    "90244",     # invalid enum value for 'Is Used When'
    "100476",    # item name over the max length
    "5009",      # a price schedule with no price value
    "100893",    # child ASIN missing 'size' to link to its parent variation
    "100095",    # invalid enum value for an attribute
}
PARTIAL_CODES = {
    "8541",      # item_name conflicts with the catalog value: PATCH or appeal
    "101067",    # product type does not match the ASIN's
    "300060", "20000", "300403",  # image URL unreachable/blocked by its host
    "100232",    # image flagged explicit content / child model
    "100586", "100587", "18625", "18947",  # image has props or a model
    "18320", "20017", "100582", "20015",   # main image missing/cropped/blocked
}
UI_ONLY_CODES = {
    "101058",  # size chart defect: "size chart self-serve tool" in Seller Central
    "100390",  # missing compliance documents: Seller Central upload
}


def fixability(codes: set[str]) -> str:
    if not codes:
        return ""
    if codes <= UI_ONLY_CODES:
        return "No (Seller Central only)"
    if codes & UI_ONLY_CODES or codes & PARTIAL_CODES:
        return "Partial"
    if codes <= API_FIXABLE_CODES:
        return "Yes"
    return "Unknown"


def tier_rank(row: sqlite3.Row) -> int:
    if not row["is_discoverable"]:
        return 3
    if row["max_severity"] == "ERROR":
        return 2
    if row["max_severity"] == "WARNING":
        return 1
    return 0


def pick_representatives(listings: list[sqlite3.Row], mirror_prefix: str) -> dict[str, sqlite3.Row]:
    """Worst offer per ASIN; a primary (non-mirror) offer always beats a mirror."""
    def is_mirror(row: sqlite3.Row) -> bool:
        return bool(mirror_prefix) and row["seller_sku"].startswith(mirror_prefix)

    def rank(row: sqlite3.Row) -> tuple[int, int, int]:
        return (0 if is_mirror(row) else 1, tier_rank(row), row["issue_count"] or 0)

    best: dict[str, sqlite3.Row] = {}
    for row in listings:
        if row["asin"] not in best or rank(row) > rank(best[row["asin"]]):
            best[row["asin"]] = row
    return best


def _traffic(conn: sqlite3.Connection, asins: list[str], start: str, end: str) -> dict[str, dict]:
    marks = ",".join("?" * len(asins))
    rows = conn.execute(
        f"SELECT asin, SUM(units_ordered) units, SUM(ordered_sales) gmv, "
        f"SUM(sessions) sessions, AVG(buy_box_pct) buy_box_pct "
        f"FROM amazon_traffic_weekly "
        f"WHERE asin IN ({marks}) AND week_start >= ? AND week_start < ? "
        f"GROUP BY asin", asins + [start, end]).fetchall()
    return {r["asin"]: dict(r) for r in rows}


def build(conn: sqlite3.Connection, out_dir: Path, days: int = 90,
          mirror_prefix: str = "") -> tuple[int, int]:
    """Write the two CSVs. Returns (penalized ASINs, quick wins)."""
    conn.row_factory = sqlite3.Row
    today = date.today()
    cur_start = (today - timedelta(days=days)).isoformat()
    prior_start = (today - timedelta(days=2 * days)).isoformat()

    listings = conn.execute(
        "SELECT seller_sku, asin, item_name, issue_count, max_severity, "
        "is_discoverable, is_buyable FROM amazon_listing_quality "
        "WHERE asin IS NOT NULL AND asin <> ''").fetchall()
    best = pick_representatives(listings, mirror_prefix)
    penalized = {a: r for a, r in best.items() if tier_rank(r) > 0}
    if not penalized:
        return 0, 0

    asins = list(penalized)
    cur = _traffic(conn, asins, cur_start, today.isoformat())
    prior = _traffic(conn, asins, prior_start, cur_start)
    skip = ",".join("?" * len(NON_DEFECT_CODES))

    out_rows = []
    for asin, row in penalized.items():
        tier = TIER_NAMES[tier_rank(row)]
        issues = conn.execute(
            f"SELECT code, COALESCE(NULLIF(attribute_names,''), categories) AS field "
            f"FROM amazon_listing_quality_issues "
            f"WHERE seller_sku = ? AND code NOT IN ({skip})",
            (row["seller_sku"], *NON_DEFECT_CODES)).fetchall()
        c, p = cur.get(asin, {}), prior.get(asin, {})
        units = c.get("units") or 0
        sessions = c.get("sessions") or 0
        units_prior = p.get("units") or 0
        buy_box = c.get("buy_box_pct")
        out_rows.append({
            "asin": asin,
            "title": row["item_name"] or "",
            "representative_sku": row["seller_sku"],
            "tier": tier,
            "is_discoverable": row["is_discoverable"],
            "is_buyable": row["is_buyable"],
            "units": units,
            "sales": round(c.get("gmv") or 0.0, 2),
            "sessions": sessions,
            # blank, not 0, when there were no sessions to divide by
            "conversion_rate": round(units / sessions, 4) if sessions else "",
            "avg_buy_box_pct": round(buy_box, 4) if buy_box is not None else "",
            "units_prior": units_prior,
            "sales_prior": round(p.get("gmv") or 0.0, 2),
            "issue_count": row["issue_count"] or 0,
            "fields_with_issues": ",".join(sorted({i["field"] for i in issues if i["field"]})),
            "api_fixable": fixability({i["code"] for i in issues}),
            "priority_score": round(max(units, units_prior) * SEVERITY_WEIGHT[tier], 1),
        })
    out_rows.sort(key=lambda r: r["priority_score"], reverse=True)

    # Quick wins: <=2 issues AND above the median volume among penalized ASINs,
    # a data-driven cutoff rather than a fixed unit count.
    volumes = sorted(max(r["units"], r["units_prior"]) for r in out_rows)
    median = volumes[len(volumes) // 2]
    quick = [r for r in out_rows
             if r["issue_count"] <= 2 and max(r["units"], r["units_prior"]) > median
             and r["tier"] != "SUPPRESSED"]

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in ((MAIN_NAME, out_rows), (QUICK_NAME, quick)):
        with open(out_dir / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
            w.writeheader()
            w.writerows(rows)
    return len(out_rows), len(quick)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90, help="trailing window size (default 90)")
    p.add_argument("--out-dir", default=".", help="where to write the CSVs (default: cwd)")
    args = p.parse_args()

    conn = warehouse_db.connect()
    try:
        total, quick = build(conn, Path(args.out_dir), args.days,
                             os.environ.get("AMAZON_MIRROR_SKU_PREFIX", ""))
    except sqlite3.OperationalError as e:
        raise SystemExit(f"{e}. Run amazon_listing_quality_sync.py and "
                         "amazon_traffic_sync.py first.")
    finally:
        conn.close()
    print(f"{total} penalized ASINs -> {MAIN_NAME}; {quick} quick wins -> {QUICK_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
