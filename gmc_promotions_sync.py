"""
Shopify code discounts -> Google Merchant Center promotions (two Google Sheets).

Reads the store's live and scheduled code discounts through the Shopify Admin
GraphQL API and publishes the ones Google can accept as Merchant Center
promotions, so a code shows on Shopping ads and free listings without someone
re-typing it into Merchant Center for every sale.

WHICH CODES. A store holds many codes Google will not accept: single-use
loyalty or welcome codes, generated batches, codes for one customer segment.
Google requires a code anyone can redeem, so a code qualifies only if it is
ONE shared code, for ALL customers, with no usage limit and not
once-per-customer, and it has an end date in the future (classify()).
GMC_PROMO_SKIP_TITLE_PREFIXES is only a pre-filter that saves paging through
thousands of generated codes; classify() enforces the real rules either way.
Short windows are sent too (--min-hours defaults to 0): Google reviews every
promotion before serving it, and only serves inside its effective dates, so a
late approval costs nothing.

PRODUCT SCOPE. Every promotion goes out as specific_products, and products are
tied to it by a `promotion_id` column on a SUPPLEMENTAL product sheet (id +
promotion_id). A supplemental source can only add attributes, so a bad push
cannot delist anything. The eligible set is:
  * every ACTIVE product in catalog_shopify, or
  * only products carrying GMC_PROMO_ELIGIBLE_TAG, if set. Use this when your
    codes exclude some products at checkout: Google disapproves a promotion
    whose code fails on an item it is shown with.
A code scoped to one collection is accepted only when its collection handle
equals GMC_PROMO_COLLECTION_HANDLE, which must describe the SAME products as
the eligible tag (e.g. a smart collection on that tag). A code scoped to any
other collection, or to specific products, is reported and skipped. Buy X get
Y codes are skipped (not mapped).

Inclusion ids use GMC_FEED_ID_FORMAT, exactly like
merchant_center_labels_export.py, so they match the primary feed.

ORDER AND CLEARING. Inclusions are pushed before promotions, so products
already carry the promotion_id when Merchant Center fetches the promotion.
With no live codes, both sheets go out header-only: whole-sheet replacement is
how an expired promotion leaves Merchant Center.

GUARDS: dry run by default; a live code with an EMPTY eligible set is refused
(log `error`, nothing pushed), since that promotion could attach to nothing.

STATUS (sync_log platform `gmc_promotions_sync`, --execute only): `ok` with
the number of promotions (zero is a measured "no live codes", still ok), or
`error` on a Shopify, build or push failure; exit non-zero on error.

Shopify scopes: read_discounts (codeDiscountNodes; older apps call it
read_price_rules) and read_products (collection handles in a discount's
scope), on the same app run_sync.py's Shopify connector uses.

Usage:
  python gmc_promotions_sync.py                       # dry run (default)
  python gmc_promotions_sync.py --out-dir reports/promotions   # dry run + CSVs
  python gmc_promotions_sync.py --execute             # push both sheets
  python gmc_promotions_sync.py --min-hours 6 --lookahead-days 7
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import feed_labels  # noqa: E402
from warehouse import db, drive  # noqa: E402
from warehouse.connectors import shopify  # noqa: E402

PLATFORM = "gmc_promotions_sync"
MIN_HOURS = 0
LOOKAHEAD_DAYS = 14       # scheduled codes further out than this wait for a later run
MAX_PROMOS_PER_PRODUCT = 10   # Google's cap on promotion_id values per product
LONG_TITLE_MAX = 60

DEFAULT_CURRENCY = "USD"
DEFAULT_LANGUAGE = "en"
DEFAULT_DESTINATIONS = "Shopping_ads,Free_listings"

PROMO_HEADER = [
    "promotion_id", "product_applicability", "offer_type", "generic_redemption_code",
    "long_title", "promotion_effective_dates", "redemption_channel",
    "promotion_destination", "target_country", "content_language",
    "coupon_value_type", "percent_off", "money_off_amount", "minimum_purchase_amount",
]
INCLUSION_HEADER = ["id", "promotion_id"]

DISCOUNT_QUERY = """
query($after: String, $q: String) {
  codeDiscountNodes(first: 100, after: $after, query: $q, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      codeDiscount {
        __typename
        ... on DiscountCodeBasic {
          title status startsAt endsAt usageLimit appliesOncePerCustomer
          codesCount { count } codes(first: 1) { nodes { code } }
          customerSelection { __typename }
          customerGets {
            value { __typename
              ... on DiscountPercentage { percentage }
              ... on DiscountAmount { amount { amount } } }
            items { __typename
              ... on DiscountCollections { collections(first: 10) { nodes { handle title } } }
              ... on DiscountProducts { products(first: 1) { nodes { id } } } }
          }
          minimumRequirement { __typename
            ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount } } }
        }
        ... on DiscountCodeFreeShipping {
          title status startsAt endsAt usageLimit appliesOncePerCustomer
          codesCount { count } codes(first: 1) { nodes { code } }
          customerSelection { __typename }
          minimumRequirement { __typename
            ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount } } }
        }
        ... on DiscountCodeBxgy {
          title status startsAt endsAt usageLimit appliesOncePerCustomer
          codesCount { count } codes(first: 1) { nodes { code } }
          customerSelection { __typename }
        }
      }
    }
  }
}
"""


# ------------------------------------------------------------------ settings
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def eligible_tag() -> str:
    return _env("GMC_PROMO_ELIGIBLE_TAG").lower()


def collection_handle() -> str:
    return _env("GMC_PROMO_COLLECTION_HANDLE")


def skip_title_prefixes() -> tuple[str, ...]:
    return tuple(p.strip() for p in _env("GMC_PROMO_SKIP_TITLE_PREFIXES").split(",") if p.strip())


def currency() -> str:
    return _env("GMC_PROMO_CURRENCY", DEFAULT_CURRENCY).upper()


def check_required_env(execute: bool) -> None:
    missing = [] if _env("SHOPIFY_SHOP") else ["SHOPIFY_SHOP"]
    if not _env("SHOPIFY_ADMIN_TOKEN"):
        missing += [v for v in ("SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET") if not _env(v)]
    if execute:
        missing += [v for v in ("GMC_PROMO_SHEET_ID", "GMC_PROMO_INCLUSIONS_SHEET_ID",
                                "GMC_CREDENTIALS_FILE") if not _env(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")
    if collection_handle() and not eligible_tag():
        raise SystemExit("GMC_PROMO_COLLECTION_HANDLE is set without GMC_PROMO_ELIGIBLE_TAG: "
                         "a collection-scoped code needs the tag that describes that "
                         "collection's products, or its promotion would attach to every product.")
    feed_labels.feed_id_format()                       # raises on missing/invalid


# ------------------------------------------------------------------- Shopify
def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ts(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def search_query(now: dt.datetime, lookahead_days: int, prefixes: tuple[str, ...]) -> str:
    """Active + scheduled codes starting within the lookahead. Shopify's search
    cannot express "one shared code", so classify() does that client-side."""
    noise = " ".join(f"-title:{p}*" for p in prefixes)
    horizon = (now + dt.timedelta(days=lookahead_days)).date().isoformat()
    return " ".join(x for x in ("(status:active OR status:scheduled)", noise,
                                f"starts_at:<{horizon}") if x)


def fetch_discounts(now: dt.datetime, lookahead_days: int) -> list[dict]:
    q = search_query(now, lookahead_days, skip_title_prefixes())
    out, after = [], None
    while True:
        data = shopify._post({"after": after, "q": q}, DISCOUNT_QUERY)
        page = data["codeDiscountNodes"]
        out += [dict(n["codeDiscount"], node_id=n["id"]) for n in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            return out
        after = page["pageInfo"]["endCursor"]


def classify(d: dict, now: dt.datetime, min_hours: float,
             collection: str = "") -> tuple[str, str]:
    """('ok', '') or ('skip', reason). Cheapest and most common tests first, so
    generated single-use codes fall out on the first one."""
    if (d.get("codesCount") or {}).get("count", 0) != 1:
        return "skip", "multi-code batch"
    if d.get("usageLimit") is not None or d.get("appliesOncePerCustomer"):
        return "skip", "usage-limited"
    if (d.get("customerSelection") or {}).get("__typename") != "DiscountCustomerAll":
        return "skip", "customer-restricted"
    start, end = _ts(d.get("startsAt")), _ts(d.get("endsAt"))
    if end is None:
        return "skip", "no end date"
    if end <= now:
        return "skip", "already ended"
    if start is not None and (end - start).total_seconds() / 3600 < min_hours:
        return "skip", f"window < {min_hours:g}h"
    if d["__typename"] == "DiscountCodeBxgy":
        return "skip", "BXGY not mapped yet"
    if d["__typename"] == "DiscountCodeBasic":
        items = d["customerGets"]["items"]
        if items["__typename"] == "DiscountCollections":
            handles = {c["handle"] for c in items["collections"]["nodes"]}
            if not collection or handles != {collection}:
                return "skip", f"collections {sorted(handles)} not mapped"
        elif items["__typename"] != "AllDiscountItems":
            return "skip", f"{items['__typename']} not mapped"
    return "ok", ""


def promotion_id(code: str, start: dt.datetime) -> str:
    """Stable per code + start date, so a re-run updates the same promotion
    instead of creating a new one, and a code reused later gets a fresh id."""
    return f"SHOP_{re.sub(r'[^A-Z0-9]', '', code.upper())[:30]}_{start:%Y%m%d}"


def to_row(d: dict, country: str, cur: str, scoped: bool,
           destinations: str = DEFAULT_DESTINATIONS, language: str = DEFAULT_LANGUAGE) -> dict:
    """One promotions-sheet row. `scoped` = only some products are eligible,
    which the title says so a shopper is not promised a store-wide discount."""
    code = d["codes"]["nodes"][0]["code"]
    start, end = _ts(d["startsAt"]), _ts(d["endsAt"])
    minimum = None
    mr = d.get("minimumRequirement") or {}
    if mr.get("__typename") == "DiscountMinimumSubtotal":
        minimum = float(mr["greaterThanOrEqualToSubtotal"]["amount"])

    pct = money = None
    where = " select items" if scoped else ""
    if d["__typename"] == "DiscountCodeFreeShipping":
        value_type = "free_shipping_standard"
        title = f"Free shipping with code {code}"
    else:
        v = d["customerGets"]["value"]
        if v["__typename"] == "DiscountPercentage":
            pct = round(v["percentage"] * 100)
            value_type = "buy_m_get_percent_off" if minimum else "percent_off"
            title = f"{pct}% off{where} with code {code}"
        else:
            money = float(v["amount"]["amount"])
            value_type = "buy_m_get_money_off" if minimum else "money_off"
            title = f"{money:g} {cur} off{where} with code {code}"
    if minimum:
        title = title.replace(" with code", f" on {minimum:g} {cur}+ with code")

    return {
        "promotion_id": promotion_id(code, start),
        "product_applicability": "specific_products",
        "offer_type": "generic_code",
        "generic_redemption_code": code,
        "long_title": title[:LONG_TITLE_MAX],
        "promotion_effective_dates": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        "redemption_channel": "online",
        "promotion_destination": destinations,
        "target_country": country,
        "content_language": language,
        "coupon_value_type": value_type,
        "percent_off": pct if pct is not None else "",
        "money_off_amount": f"{money:.2f} {cur}" if money is not None else "",
        "minimum_purchase_amount": f"{minimum:.2f} {cur}" if minimum else "",
        "_title": d.get("title", ""),
        "_status": d.get("status", ""),
        "_hours": round((end - start).total_seconds() / 3600, 1),
    }


# --------------------------------------------------------------- eligibility
def eligible_feed_ids(conn: sqlite3.Connection, tag: str) -> tuple[list[str], dict]:
    """Primary-feed ids of the ACTIVE products a promotion may attach to: all
    of them, or those tagged `tag` (case-insensitive, exact tag match)."""
    rows = feed_labels.active_variants(conn)
    if tag:
        rows = [r for r in rows
                if tag in {t.strip().lower() for t in str(r.get("tags") or "").split(",")}]
    keyed, dropped = feed_labels.keyed_rows(rows, feed_labels.feed_id_format(),
                                            feed_labels.target_country())
    return [fid for fid, _ in keyed], {"eligible_variants": len(rows), "dropped": dropped}


def _open_db() -> sqlite3.Connection:
    if not Path(db.DB_PATH).exists():
        raise RuntimeError(f"warehouse database not found at {db.DB_PATH}")
    return db.connect_readonly()


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def push_sheet(sheet_id: str, title: str, header: list[str], rows: list[list]) -> None:
    """Whole-sheet replace, so a promotion that is no longer live drops out."""
    drive.replace_with_rows(sheet_id, title, header, rows, _env("GMC_CREDENTIALS_FILE"))
    print(f"pushed {len(rows):,} rows to sheet {title!r}")


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    ap.add_argument("--execute", action="store_true", help="push both sheets (default: dry run)")
    ap.add_argument("--min-hours", type=float, default=MIN_HOURS,
                    help="skip codes whose window is shorter than this")
    ap.add_argument("--lookahead-days", type=int, default=LOOKAHEAD_DAYS,
                    help="include scheduled codes starting within this many days")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="also write both sheets as CSVs into this folder")
    args = ap.parse_args(argv)
    check_required_env(args.execute)

    now = _utcnow()
    started = db.now()

    def fail(msg: str) -> int:
        print(f"ERROR: {msg}", file=sys.stderr)
        if args.execute:
            db.log_sync(PLATFORM, started, 0, "error", msg)
        return 1

    try:
        discounts = fetch_discounts(now, args.lookahead_days)
    except Exception as exc:  # noqa: BLE001
        return fail(f"Shopify fetch failed: {exc}")

    tag, coll = eligible_tag(), collection_handle()
    country, cur = feed_labels.target_country(), currency()
    destinations = _env("GMC_PROMO_DESTINATIONS", DEFAULT_DESTINATIONS)
    language = _env("GMC_PROMO_CONTENT_LANGUAGE", DEFAULT_LANGUAGE)

    kept, skipped = [], {}
    for d in discounts:
        verdict, why = classify(d, now, args.min_hours, coll)
        if verdict == "ok":
            kept.append(to_row(d, country, cur, bool(tag), destinations, language))
        else:
            skipped.setdefault(why, []).append(d.get("title", "?"))

    print(f"Shopify code discounts (active/scheduled, start within {args.lookahead_days}d): "
          f"{len(discounts)}")
    for why, titles in sorted(skipped.items(), key=lambda kv: -len(kv[1])):
        print(f"  skip {len(titles):>6}  {why:<32} e.g. {', '.join(titles[:4])}")
    print(f"  KEEP {len(kept):>6}")
    for r in kept:
        print(f"    {r['promotion_id']:<32} {r['_status']:<9} {r['_hours']:>6}h  "
              f"{r['promotion_effective_dates']}  {r['long_title']}")

    inclusion_rows: list[list[str]] = []
    if kept:
        try:
            conn = _open_db()
            try:
                ids, stats = eligible_feed_ids(conn, tag)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return fail(f"eligible-product build failed: {exc}")
        if not ids:
            return fail(f"{len(kept)} live code(s) but the eligible product set is empty "
                        f"(tag {tag or '(none)'!r}); nothing pushed")
        # Google caps promotion_id values per product; the newest codes win
        # (Shopify returns them newest-created first).
        promo_ids = ",".join(r["promotion_id"] for r in kept[:MAX_PROMOS_PER_PRODUCT])
        inclusion_rows = [[i, promo_ids] for i in ids]
        print(f"supplemental inclusions: {len(ids):,} ids from {stats['eligible_variants']:,} "
              f"eligible variants ({stats['dropped']:,} without a usable id)")

    promo_rows = [[r[h] for h in PROMO_HEADER] for r in kept]
    if args.out_dir:
        stamp = now.strftime("%Y-%m-%d_%H%M")
        write_csv(args.out_dir / f"{stamp}_promotions.csv", PROMO_HEADER, promo_rows)
        write_csv(args.out_dir / f"{stamp}_inclusions.csv", INCLUSION_HEADER, inclusion_rows)
        print(f"wrote {stamp}_promotions.csv and {stamp}_inclusions.csv to {args.out_dir}")

    if not args.execute:
        print("DRY RUN: nothing pushed. Re-run with --execute to push both sheets.")
        return 0
    try:
        push_sheet(_env("GMC_PROMO_INCLUSIONS_SHEET_ID"), "inclusions",
                   INCLUSION_HEADER, inclusion_rows)
        push_sheet(_env("GMC_PROMO_SHEET_ID"), "promotions", PROMO_HEADER, promo_rows)
    except Exception as exc:  # noqa: BLE001
        return fail(f"push failed: {exc}")
    codes = ", ".join(r["generic_redemption_code"] for r in kept) or "none live"
    db.log_sync(PLATFORM, started, len(promo_rows), "ok",
                f"{len(promo_rows)} promotions ({codes}), {len(inclusion_rows):,} inclusion rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
