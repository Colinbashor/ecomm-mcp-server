r"""
Amazon SP-API listing quality (Listings Items issues) -> warehouse.

Endpoint: GET /listings/2021-08-01/items/{sellerId}/{sku}?includedData=issues,summaries,attributes

Scope needed: NONE beyond the existing SPAPI_* LWA credentials amazon_orders.py
already uses.

This is the programmatic equivalent of Seller Central's Listing Quality
Dashboard / the "Issues" column in Manage All Listings: Amazon has no single
content-quality TIER the way TikTok Shop does (see tiktok_listing_quality_sync.py
for that side), but the `issues` array on each listing is driven by the same
kind of objective, fixable checks — missing/incomplete attributes, image
problems, size-chart defects, catalog-data conflicts — each carrying a
severity (ERROR/WARNING/INFO).

BACKEND SEARCH TERMS COME ALONG FOR FREE. Adding `attributes` to the same
`includedData` list costs no extra call and no extra scope, and it surfaces
the `generic_keyword` attribute — this is the actual hidden "Search Terms"
field from Seller Central's edit-listing page, not a guess or a derived
value. It's worth capturing here because it's invisible everywhere else:
it never appears on the storefront, and Amazon gives sellers no report or
search-terms API that lists what's currently saved per SKU — the only way
to see it is one listing at a time in the UI, or here. `item_type_keyword`
(Amazon's own category classifier for the listing, not free text) is
captured alongside it since it's the same attribute call. Both are commonly
NULL/empty for a given SKU — that's a real content gap worth surfacing, not
a parsing bug — and a keyword you want ranked on but can't fit in the
visible title/bullets is exactly what this backend field is for.

!! SELLER ID GOTCHA !!
The Listings Items API path needs {sellerId} (Amazon calls it the "Merchant
Token"), and unlike the Ads API's profile id there is no straightforward SP-API
call that returns it for the account you're authorized as: GET
/sellers/v1/marketplaceParticipations (the closest thing to a "who am I"
endpoint) omits it from its documented response schema entirely. One
undocumented way to recover it: that same response's marketplace list
sometimes includes an "Amazon.com Invoicing Shadow Marketplace" entry whose
`storeName` is shaped `Invoicing_<accountId>_<sellerId>` — the sellerId is the
second underscore-delimited segment. This is unverified/undocumented Amazon
behavior, not a stable contract, so confirm it against a real SKU (a valid SKU
should 200 with real issues; a bogus one should come back a same-account "SKU
not found" rather than an auth error) before trusting it, and re-derive it the
same way if you ever re-authorize under a different seller account. Once
confirmed, save it as `SPAPI_SELLER_ID` in `.env` rather than re-deriving it
every run.

!! NO BULK PATH EXISTS !!
There's no Reports API flat file that carries `issues` data (unlike
GET_MERCHANT_LISTINGS_ALL_DATA, which covers status/price/qty but not content
diagnostics) — this evaluation is real-time against the live catalog, which is
presumably why it's a synchronous per-SKU GET rather than an async report.
That's fine at typical catalog sizes: the endpoint rate-limits at 5 req/sec
(confirmed via the `x-amzn-RateLimit-Limit` response header), so even a few
thousand SKUs clear in well under an hour with no batching needed — unlike
TikTok's 200-per-call diagnosis endpoint.

WHICH SKUs TO DIAGNOSE: like amazon_rank_sync.py, this base scaffold has no
product/catalog table to source an active-SKU list from (that depends on your
own catalog pipeline), so you provide the list explicitly: `--skus`
(comma-separated) or `--skus-file` (one seller SKU per line). As a
convenience, if neither is given this script also looks for distinct SKUs in
`amazon_fulfilled_shipments` (written by amazon_fees_sync.py's "shipments"
report) — the same weak "recently sold" fallback amazon_rank_sync.py uses, not
the intended primary input.

MANY SELLER_SKUs CAN SHARE ONE ASIN — an FBA and a non-FBA (MFN) offer of the
same product are separate SKUs, separately diagnosed, and their attribute
completeness can differ. This table is keyed by seller_sku (the real unit the
API evaluates); if you join this to ASIN-grain data (units/GMV/sessions from
your own sales/traffic feed), roll up to ASIN first — pick one representative
offer per ASIN (e.g. worst severity, then most issues) — or you will
double-count that ASIN's demand across its sibling SKUs.

A non-defect code is filtered out of `issue_count`/`max_severity` (but NOT out
of the raw issues table — nothing observed is silently dropped): code 101265
is Amazon's own "switch this SKU to FBA, here's the modeled sales lift"
merchandising nudge, not a content-quality defect, and would otherwise inflate
every MFN-fulfilled SKU's issue count for no defect reason.

Snapshot semantics, same as tiktok_listing_quality_sync.py: latest diagnosis
only. `amazon_listing_quality_issues` rows for a SKU are deleted and
re-inserted each time that SKU is diagnosed, not accumulated.

`--resume` skips any seller SKU already present in `amazon_listing_quality`,
to finish an interrupted full pass without redoing already-synced work.
Deliberately NOT the default: a normal run always re-diagnoses every SKU on
the list, since a listing's issues (and its backend search terms) can change
between runs and a stale row should refresh.

AUTH: same SPAPI_* LWA credentials as amazon_orders.py, plus SPAPI_SELLER_ID
(see the gotcha above) — no new app registration.

USAGE:
  python amazon_listing_quality_sync.py --skus SKU1,SKU2
  python amazon_listing_quality_sync.py --skus-file skus.txt
  python amazon_listing_quality_sync.py --skus-file skus.txt --limit 100   # smoke test
  python amazon_listing_quality_sync.py --skus-file skus.txt --resume      # finish an interrupted pass
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.amazon_orders import HOSTS, _access_token, _token_cache

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))
PLATFORM = "amazon_listing_quality"
PATH_TMPL = "/listings/2021-08-01/items/{seller_id}/{sku}"

REQUIRED_ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN",
                 "SPAPI_MARKETPLACE_ID", "SPAPI_SELLER_ID")

# code 101265 = "estimated sales lift if you switch to FBA" -- a merchandising
# nudge, not a content-quality defect. Excluded from issue_count/max_severity
# (kept in the raw issues table, see module docstring).
NON_DEFECT_CODES = {"101265"}
SEVERITY_RANK = {"ERROR": 2, "WARNING": 1, "INFO": 0}

DDL = """
CREATE TABLE IF NOT EXISTS amazon_listing_quality (
    seller_sku        TEXT PRIMARY KEY,
    asin              TEXT,
    item_name         TEXT,
    product_type      TEXT,
    is_discoverable   INTEGER,   -- 0/1, from summaries[0].status
    is_buyable        INTEGER,   -- 0/1
    issue_count       INTEGER,   -- excludes NON_DEFECT_CODES
    max_severity      TEXT,      -- 'ERROR' | 'WARNING' | 'INFO' | NULL (clean)
    generic_keyword   TEXT,      -- the real backend "Search Terms" field; NULL = empty
    item_type_keyword TEXT,      -- Amazon's own category classifier, not free-text
    synced_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amazon_listing_quality_issues (
    seller_sku      TEXT NOT NULL,
    issue_seq       INTEGER NOT NULL,  -- 0-based; a code can repeat per SKU
                                        -- (e.g. two unreachable-image errors)
    code            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    message         TEXT,
    attribute_names TEXT,              -- comma-joined
    categories      TEXT,              -- comma-joined
    synced_at       TEXT NOT NULL,
    PRIMARY KEY (seller_sku, issue_seq)
);
"""

# Columns added after a table may already exist from an older version of this
# script — applied on the fly so an existing warehouse.db doesn't need to be
# dropped to pick up a new column.
MIGRATE_COLUMNS = ("generic_keyword TEXT", "item_type_keyword TEXT")


def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "amazon_listing_quality_sync: missing required env var(s): "
            f"{', '.join(missing)}. SPAPI_SELLER_ID needs the derivation "
            "described in this script's module docstring — it isn't returned "
            "by any documented SP-API call."
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create this connector's tables if they don't exist yet, and add any
    columns introduced after a table may already have been created (see
    MIGRATE_COLUMNS) — safe to call every run."""
    conn.executescript(DDL)
    existing = {c[1] for c in conn.execute("PRAGMA table_info(amazon_listing_quality)")}
    for col_def in MIGRATE_COLUMNS:
        if col_def.split()[0] not in existing:
            conn.execute(f"ALTER TABLE amazon_listing_quality ADD COLUMN {col_def}")


def fallback_skus(conn: sqlite3.Connection) -> list[str]:
    """Best-effort SKU list when the caller doesn't pass --skus/--skus-file:
    whatever recently showed up in amazon_fulfilled_shipments (if that table
    exists and has been populated by amazon_fees_sync.py). Returns [] if the
    table is missing or empty — the caller decides what to do about that.
    Intentionally weak, same as amazon_rank_sync.py's fallback_asins(); pass
    --skus/--skus-file for a real run."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT sku FROM amazon_fulfilled_shipments "
            "WHERE sku IS NOT NULL AND sku <> ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows if r[0]]


def _force_token_refresh() -> None:
    _token_cache["expires_at"] = 0.0


def _get_listing(host: str, seller_id: str, sku: str, marketplace_id: str) -> tuple[int, dict]:
    """One GET for a single SKU's issues+summaries+attributes. Refreshes the
    LWA token once on 401/403, backs off on 429. Returns (status_code, json_body)."""
    path = PATH_TMPL.format(seller_id=seller_id, sku=requests.utils.quote(sku, safe=""))
    for attempt in range(6):
        try:
            resp = requests.get(
                f"{host}{path}",
                params={"marketplaceIds": marketplace_id,
                        "includedData": "issues,summaries,attributes"},
                headers={"x-amz-access-token": _access_token(), "Accept": "application/json"},
                timeout=30,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(10)
            continue
        if resp.status_code in (401, 403) and attempt == 0:
            _force_token_refresh()
            continue
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 0) or 0) or min(30.0, 3 * (attempt + 1))
            time.sleep(wait)
            continue
        return resp.status_code, (resp.json() if resp.content else {})
    raise RuntimeError(f"SP-API listings GET for {sku!r} kept failing after retries.")


def run(conn: sqlite3.Connection, skus: list[str],
        resume: bool = False) -> tuple[int, int, list[tuple[str, str]]]:
    region = os.environ.get("SPAPI_REGION", "NA").upper()
    host = HOSTS[region]
    marketplace_id = os.environ["SPAPI_MARKETPLACE_ID"]
    seller_id = os.environ["SPAPI_SELLER_ID"]

    if resume:
        # Skip SKUs a prior (possibly interrupted) run already wrote. There's
        # no per-run id, so "already present at all" is the resume signal --
        # fine for finishing an interrupted full pass; see the module
        # docstring for why this isn't the default.
        already = {r[0] for r in conn.execute(
            "SELECT seller_sku FROM amazon_listing_quality").fetchall()}
        skus = [s for s in skus if s not in already]
        print(f"    --resume: {len(already)} already synced, {len(skus)} remaining", flush=True)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    failed: list[tuple[str, str]] = []
    written = 0

    for i, sku in enumerate(skus):
        status_code, data = _get_listing(host, seller_id, sku, marketplace_id)
        if status_code == 404:
            failed.append((sku, "not found (delisted since your SKU list was built)"))
            time.sleep(0.22)
            continue
        if status_code != 200:
            errs = data.get("errors") or [{}]
            failed.append((sku, f"{status_code}: {errs[0].get('message', '')}"))
            time.sleep(0.22)
            continue

        summ = (data.get("summaries") or [{}])[0]
        issues = data.get("issues") or []
        attrs = data.get("attributes") or {}
        status_list = summ.get("status") or []
        defect_issues = [iss for iss in issues if iss.get("code") not in NON_DEFECT_CODES]
        max_sev = None
        if defect_issues:
            max_sev = max((iss.get("severity") for iss in defect_issues),
                           key=lambda s: SEVERITY_RANK.get(s, 0))

        # Both are lists-of-{"value": ...} per the Listings Items attribute
        # shape; take the first (sellers set at most one of each in practice).
        generic_keyword = ((attrs.get("generic_keyword") or [{}])[0] or {}).get("value")
        item_type_keyword = ((attrs.get("item_type_keyword") or [{}])[0] or {}).get("value")

        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO amazon_listing_quality
                   (seller_sku, asin, item_name, product_type, is_discoverable,
                    is_buyable, issue_count, max_severity, generic_keyword,
                    item_type_keyword, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (sku, summ.get("asin"), summ.get("itemName"), summ.get("productType"),
                 int("DISCOVERABLE" in status_list), int("BUYABLE" in status_list),
                 len(defect_issues), max_sev, generic_keyword, item_type_keyword, stamp),
            )
            conn.execute("DELETE FROM amazon_listing_quality_issues WHERE seller_sku = ?", (sku,))
            if issues:
                conn.executemany(
                    """INSERT INTO amazon_listing_quality_issues
                       (seller_sku, issue_seq, code, severity, message, attribute_names,
                        categories, synced_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [(sku, seq, iss.get("code"), iss.get("severity"), iss.get("message"),
                      ",".join(iss.get("attributeNames") or []),
                      ",".join(iss.get("categories") or []), stamp)
                     for seq, iss in enumerate(issues)],
                )
        written += 1
        if (i + 1) % 500 == 0:
            print(f"    {i + 1}/{len(skus)} SKUs diagnosed", flush=True)
        time.sleep(0.22)  # ~4.5 req/sec, under the observed 5 req/sec ceiling

    return len(skus), written, failed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skus", help="comma-separated list of seller SKUs to diagnose")
    p.add_argument("--skus-file", help="path to a file with one seller SKU per line")
    p.add_argument("--limit", type=int, default=None,
                   help="only diagnose the first N SKUs (smoke test)")
    p.add_argument("--resume", action="store_true",
                   help="skip SKUs already present in amazon_listing_quality "
                        "(finish an interrupted run)")
    args = p.parse_args()

    require_env()
    warehouse_db.init_db()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    ensure_schema(conn)

    if args.skus:
        skus = [s.strip() for s in args.skus.split(",") if s.strip()]
    elif args.skus_file:
        skus = [ln.strip() for ln in Path(args.skus_file).read_text().splitlines() if ln.strip()]
    else:
        skus = fallback_skus(conn)
    if args.limit:
        skus = skus[:args.limit]

    if not skus:
        conn.close()
        warehouse_db.log_sync(PLATFORM, warehouse_db.now(), 0, "error", "no target SKUs")
        raise SystemExit(
            "No SKUs to diagnose. Pass --skus A,B,C or --skus-file path.txt — "
            "this scaffold has no product/catalog table to source a default list from."
        )

    started = warehouse_db.now()
    try:
        requested, written, failed = run(conn, skus, resume=args.resume)
    except Exception as e:  # noqa: BLE001
        conn.close()
        warehouse_db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    conn.close()

    if written == 0:
        status, msg = "error", f"0 of {requested} SKUs diagnosed"
    elif failed:
        status = "degraded"
        msg = f"{written}/{requested} diagnosed; {len(failed)} SKUs failed (see log for detail)"
    else:
        status, msg = "ok", f"{written}/{requested} diagnosed"
    warehouse_db.log_sync(PLATFORM, started, written, status, msg)
    print(f"Amazon listing quality: {msg}")
    if failed:
        print(f"    failed SKUs (first 10): {failed[:10]}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
