"""
Bulk-fix Amazon's "missing material" listing-quality issue (code 18448) from
material data already in catalog_shopify.material (catalog_sync.py).

WRITE-CAPABLE. The default is a dry run: it only writes a review CSV. Nothing
is sent to Amazon without --execute, and --limit bounds a batch (do one SKU
first, confirm with amazon_listing_quality_sync.py, then the rest).

NOT A RAW COPY. Amazon's `material` attribute is a constrained array whose
`value` is a plain string or a per-product-type enum of material names
("Cotton", "Polyester Blend", "Spandex"), with 1+ items (cardinality varies by
product type; some allow only one) and each item needing {value, language_tag,
marketplace_id}. A store's own material field usually holds COMPOSITION
STRINGS ("95% Polyester, 5% Elastane", sometimes multi-part:
"Body: 75% Polyester 25% Viscose; Sleeve: 100% Cotton"), which is a different
shape. So this parses out the PRIMARY (highest-percentage) material name and
maps common synonyms (Elastane/Lycra -> Spandex, Viscose -> Rayon,
Polyamide -> Nylon, ...) toward Amazon's vocabulary.

DROP, DON'T GUESS. It submits ONE item per SKU (the primary material), which is
at or under every product type's minimum cardinality, so it never needs each
SKU's own maximum. A word that is not in the synonym table is dropped: a wrong
enum value fails the whole PATCH, and a typo pushed live as free text is worse
than a skipped SKU.

JOIN. Seller SKUs are matched to catalog_shopify.sku. If your Amazon SKUs carry
fulfilment suffixes the store SKU lacks, list them in AMAZON_SKU_SUFFIXES
(comma-separated, e.g. `-FBA,-FBM`); they are stripped before the join. Only
non-ARCHIVED catalog rows are used, because catalog_shopify.sku is not unique
(archived duplicates share it) and a stale duplicate would feed a wrong value.

AUTH: the SPAPI_* credentials amazon_listing_quality_sync.py uses, including
SPAPI_SELLER_ID.

USAGE:
  python amazon_listing_quality_material_fix.py                    # dry run (default)
  python amazon_listing_quality_material_fix.py --execute --limit 1   # one live test
  python amazon_listing_quality_material_fix.py --execute             # full batch
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS, _access_token, _token_cache

load_dotenv()
OUT_CSV = "amazon_listing_quality_material_fix_review.csv"
REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN",
                "SPAPI_MARKETPLACE_ID", "SPAPI_SELLER_ID")

# Synonym -> Amazon's common material vocabulary. Deliberately conservative:
# only genuinely equivalent fibre names. Keys are letters only, upper-case, and
# are matched as PREFIXES, longest first (see parse_primary_material).
SYNONYMS = {
    "ELASTANE": "Spandex", "ELASTHANE": "Spandex", "LYCRA": "Spandex", "SPANDEX": "Spandex",
    "VISCOSE": "Rayon", "RAYON": "Rayon",
    "POLYAMIDE": "Nylon", "NYLON": "Nylon",
    "POLYESTER": "Polyester", "COTTON": "Cotton", "ACRYLIC": "Acrylic",
    "WOOL": "Wool", "SILK": "Silk", "LINEN": "Linen", "JUTE": "Jute",
    "LEATHER": "Leather", "SUEDE": "Suede", "NEOPRENE": "Neoprene",
    "POLYCOTTON": "Polycotton", "CASHMERE": "Cashmere",
    "FAUXLEATHER": "Faux Leather", "VEGANLEATHER": "Faux Leather", "PULEATHER": "Faux Leather",
    "PLASTIC": "Plastic", "PVC": "Polyvinyl Chloride (PVC)",
    "POLYURETHANE": "Polyurethane", "PU": "Polyurethane",
}
_KEYS_LONGEST_FIRST = sorted(SYNONYMS, key=len, reverse=True)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*([A-Za-z][A-Za-z\s\-]{0,30})")


def parse_primary_material(raw: str) -> str | None:
    """The highest-percentage component of a composition string, mapped to
    Amazon's vocabulary, or None when nothing maps.

    The text after a "%" is not reliably just the material: chained components
    can run together ("100% POLYESTER Headgear 100% POLYESTER"). So the span is
    compacted (letters only, upper-case) and a KNOWN material word must be its
    PREFIX, longest key first so "Faux Leather" is not cut to "Leather".
    "POLYESTERHEADGEAR" matches POLYESTER and drops HEADGEAR; a typo like
    "POLYESTYER" matches nothing and is left out rather than guessed.
    """
    found = []  # (percentage, mapped value)
    for pct, span in _PCT_RE.findall(raw or ""):
        compact = re.sub(r"[^A-Z]", "", span.upper())
        for key in _KEYS_LONGEST_FIRST:
            if compact.startswith(key):
                found.append((float(pct), SYNONYMS[key]))
                break
    return max(found, key=lambda f: f[0])[1] if found else None


def _strip_suffixes(sku: str, suffixes: tuple[str, ...]) -> str:
    for s in suffixes:
        sku = sku.replace(s, "")
    return sku


def candidates(conn: sqlite3.Connection, suffixes: tuple[str, ...] = ()) -> list[dict]:
    """SKUs with an open 'missing material' issue and a mappable catalog value."""
    issues = conn.execute(
        "SELECT DISTINCT seller_sku FROM amazon_listing_quality_issues "
        "WHERE code = '18448' AND (',' || attribute_names || ',') LIKE '%,material,%'"
    ).fetchall()
    materials: dict[str, str] = {}
    for sku, material in conn.execute(
            "SELECT sku, material FROM catalog_shopify "
            "WHERE status != 'ARCHIVED' AND material IS NOT NULL AND material != '' "
            "ORDER BY synced_at"):
        materials[sku] = material  # newest synced_at wins
    out = []
    for (seller_sku,) in issues:
        raw = materials.get(_strip_suffixes(seller_sku, suffixes))
        value = parse_primary_material(raw) if raw else None
        if value:
            out.append({"seller_sku": seller_sku, "catalog_material_raw": raw,
                        "proposed_value": value})
    return out


def _listing_url(host: str, seller_id: str, sku: str) -> str:
    return f"{host}/listings/2021-08-01/items/{seller_id}/{requests.utils.quote(sku, safe='')}"


def _patch_material(host: str, seller_id: str, sku: str, marketplace_id: str,
                    product_type: str, value: str) -> tuple[int, dict]:
    body = {
        "productType": product_type,
        "patches": [{
            "op": "replace",
            "path": "/attributes/material",
            "value": [{"value": value, "language_tag": "en_US", "marketplace_id": marketplace_id}],
        }],
    }
    resp = None
    for attempt in range(3):
        resp = requests.patch(
            _listing_url(host, seller_id, sku), params={"marketplaceIds": marketplace_id},
            json=body, timeout=30,
            headers={"x-amz-access-token": _access_token(), "Accept": "application/json"})
        if resp.status_code in (401, 403) and attempt == 0:
            _token_cache["expires_at"] = 0.0  # force an LWA refresh, retry once
            continue
        if resp.status_code == 429:
            time.sleep(5)
            continue
        break
    return resp.status_code, (resp.json() if resp.content else {})


def _product_type(host: str, seller_id: str, sku: str, marketplace_id: str) -> str | None:
    resp = requests.get(
        _listing_url(host, seller_id, sku),
        params={"marketplaceIds": marketplace_id, "includedData": "summaries"},
        headers={"x-amz-access-token": _access_token(), "Accept": "application/json"},
        timeout=30)
    if resp.status_code != 200:
        return None
    summaries = resp.json().get("summaries") or []
    return summaries[0].get("productType") if summaries else None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true",
                   help="actually PATCH Amazon (default: dry run, review CSV only)")
    p.add_argument("--limit", type=int, default=None, help="only apply to the first N rows")
    p.add_argument("--out-dir", default=".", help="where to write the review CSV (default: cwd)")
    args = p.parse_args()

    suffixes = tuple(s.strip() for s in os.environ.get("AMAZON_SKU_SUFFIXES", "").split(",")
                     if s.strip())
    conn = warehouse_db.connect_readonly()
    try:
        rows = candidates(conn, suffixes)
    except sqlite3.OperationalError as e:
        raise SystemExit(f"{e}. Run amazon_listing_quality_sync.py and catalog_sync.py first.")
    finally:
        conn.close()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["seller_sku", "catalog_material_raw", "proposed_value"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} SKUs have a mappable material value -> {OUT_CSV}")

    if not args.execute:
        print("Dry run only. Review the CSV, then --execute --limit 1 for a live test.")
        return 0

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing required env var(s): {', '.join(missing)}")
    host = HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    marketplace_id = os.environ["SPAPI_MARKETPLACE_ID"]
    seller_id = os.environ["SPAPI_SELLER_ID"]

    todo = rows[: args.limit] if args.limit else rows
    started = warehouse_db.now()
    ok, failed = 0, []
    for r in todo:
        sku = r["seller_sku"]
        ptype = _product_type(host, seller_id, sku, marketplace_id)
        if not ptype:
            failed.append((sku, "could not resolve productType"))
            continue
        status, data = _patch_material(host, seller_id, sku, marketplace_id, ptype,
                                       r["proposed_value"])
        result = data.get("status")
        if status in (200, 202) and result != "INVALID":
            ok += 1
            print(f"  OK   {sku} ({ptype}) material={r['proposed_value']!r} status={result}")
        else:
            failed.append((sku, f"{status} {result}: {(data.get('issues') or [])[:2]}"))
            print(f"  FAIL {sku}: {failed[-1][1]}")
        time.sleep(0.3)  # stay under the Listings Items rate limit

    print(f"Applied {ok}/{len(todo)}; {len(failed)} failed.")
    for sku, err in failed[:10]:
        print(f"    {sku}: {err}")
    if not todo:
        status, msg = "degraded", "nothing to apply"
    elif ok == 0:
        status, msg = "error", f"0 of {len(todo)} PATCHes accepted"
    elif failed:
        status, msg = "degraded", f"{ok}/{len(todo)} applied; {len(failed)} failed"
    else:
        status, msg = "ok", f"{ok}/{len(todo)} applied"
    warehouse_db.log_sync("amazon_listing_material_fix", started, ok, status, msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
