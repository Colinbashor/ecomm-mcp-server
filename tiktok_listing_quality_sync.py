r"""
TikTok Shop listing quality diagnosis -> warehouse.

Endpoint: GET /product/202405/products/diagnoses
Scope needed: seller.product.optimize — an existing TikTok Shop app grant
usually already has product-read scopes, but this specific optimize scope is
separate and worth confirming (Partner Center > your app > Scopes) before a
first run; if it's missing you'll see a permission-denied response rather
than data.

This is TikTok's own listing QUALITY tier (POOR/FAIR/GOOD), driven by
objective content checks (title, description, images, attributes, size
chart) — NOT the customer star rating. This is the TikTok-side analog to
amazon_listing_quality_sync.py, which does the same job against Amazon's
per-issue (rather than per-tier) content diagnostics. Docs:
https://partner.tiktokshop.com/docv2/page/66eb8f5c6f2da702e96a49dd

Max 200 product_ids per call, and every id must currently be an active,
listed product. A request-level failure (e.g. a stale id that's since been
deleted, code 12052260) fails the WHOLE batch of up to 200 — this connector
halves a failing batch (the same halving pattern used elsewhere in this
project for TikTok API cap errors) rather than dropping all 200 products'
data, and logs `degraded` (never `ok`) if any ids were never diagnosed after
halving down to singles.

WHICH PRODUCT IDS TO DIAGNOSE: like amazon_listing_quality_sync.py, this base
scaffold has no product/catalog table to source an active-product-id list
from (that depends on your own catalog pipeline), so you provide the list
explicitly: `--product-ids` (comma-separated) or `--product-ids-file` (one id
per line) — e.g. exported from Seller Center's product listing page, or
pulled from your own TikTok Shop product-search integration if you have one.

Snapshot semantics: latest diagnosis only. A product's issue list can shrink
as it's fixed, so `tiktok_listing_quality_issues` rows for a product are
deleted and re-inserted each time that product is diagnosed (not
accumulated) — an old code that no longer applies must not survive because a
listing already fixed it.

AUTH: same TikTok Shop app credentials as warehouse/connectors/tiktok_shop.py
— no new credentials, only the extra scope noted above.

USAGE:
  python tiktok_listing_quality_sync.py --product-ids 1234567890,2345678901
  python tiktok_listing_quality_sync.py --product-ids-file product_ids.txt
  python tiktok_listing_quality_sync.py --product-ids-file product_ids.txt --limit 400   # smoke test
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors.tiktok_shop import TOKEN_EXPIRED_CODES, _refresh_access_token

load_dotenv()

PLATFORM = "tiktok_listing_quality"
BASE = "https://open-api.tiktokglobalshop.com"
PATH = "/product/202405/products/diagnoses"
BATCH_SIZE = 200
REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_listing_quality (
    product_id              TEXT PRIMARY KEY,
    current_tier            TEXT,     -- POOR | FAIR | GOOD
    remaining_recommendations INTEGER,
    synced_at                TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiktok_listing_quality_issues (
    product_id     TEXT NOT NULL,
    field          TEXT NOT NULL,   -- TITLE | DESCRIPTION | IMAGE | ATTRIBUTE | SIZE_CHART
    code           TEXT NOT NULL,
    how_to_solve   TEXT,
    quality_tier   TEXT,            -- the tier reached by fixing this (FAIR | GOOD)
    suggestion_json TEXT,           -- raw `suggestion` object for this field, if any
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (product_id, field, code)
);
"""


def check_required_env() -> None:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


def ensure_schema(conn) -> None:
    conn.executescript(DDL)


def _sign(path: str, params: dict, secret: str) -> str:
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    base = f"{secret}{path}{ordered}{secret}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def _request(product_ids: list[str]) -> dict:
    """One signed GET for up to 200 product ids. Refreshes an expired access
    token once, then retries."""
    secret = os.environ["TIKTOK_APP_SECRET"]
    for attempt in (1, 2):  # attempt 2 only happens after a token refresh
        params = {
            "app_key": os.environ["TIKTOK_APP_KEY"],
            "timestamp": str(int(time.time())),
            "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"],
            "product_ids": ",".join(product_ids),
        }
        params["sign"] = _sign(PATH, params, secret)
        r = requests.get(
            f"{BASE}{PATH}",
            params=params,
            headers={"content-type": "application/json",
                     "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]},
            timeout=60,
        )
        data = r.json()
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and attempt == 1:
            _refresh_access_token()
            continue
        if code != 0:
            raise RuntimeError(f"TikTok diagnosis API {r.status_code} code={code}: {data.get('message')}")
        return data
    raise RuntimeError("TikTok request failed even after refreshing the access token.")


def _fetch_batch(product_ids: list[str], on_fail_report) -> list[dict]:
    """Diagnose a batch, halving on failure so one bad id doesn't lose the
    other ~199 products' data."""
    try:
        data = _request(product_ids)
        return (data.get("data") or {}).get("products", [])
    except RuntimeError as e:
        if len(product_ids) == 1:
            on_fail_report(product_ids[0], str(e))
            return []
        mid = len(product_ids) // 2
        return (_fetch_batch(product_ids[:mid], on_fail_report)
                + _fetch_batch(product_ids[mid:], on_fail_report))


def run(conn, product_ids: list[str]) -> tuple[int, int, list[tuple[str, str]]]:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    failed: list[tuple[str, str]] = []
    products_written = 0

    for i in range(0, len(product_ids), BATCH_SIZE):
        batch = product_ids[i:i + BATCH_SIZE]
        products = _fetch_batch(batch, lambda pid, err: failed.append((pid, err)))
        if not products:
            continue

        tier_rows = []
        issue_rows = []
        for p in products:
            pid = p.get("id")
            lq = p.get("listing_quality") or {}
            tier_rows.append((pid, lq.get("current_tier"), lq.get("remaining_recommendations"), stamp))
            for field_block in p.get("diagnoses", []) or []:
                field = field_block.get("field")
                suggestion = field_block.get("suggestion")
                suggestion_json = json.dumps(suggestion) if suggestion else None
                for res in field_block.get("diagnosis_results", []) or []:
                    issue_rows.append((
                        pid, field, res.get("code"), res.get("how_to_solve"),
                        res.get("quality_tier"), suggestion_json, stamp,
                    ))

        batch_pids = [p.get("id") for p in products]
        with conn:
            conn.executemany(
                """INSERT OR REPLACE INTO tiktok_listing_quality
                   (product_id, current_tier, remaining_recommendations, synced_at)
                   VALUES (?,?,?,?)""",
                tier_rows,
            )
            conn.execute(
                f"DELETE FROM tiktok_listing_quality_issues WHERE product_id IN "
                f"({','.join('?' * len(batch_pids))})",
                batch_pids,
            )
            if issue_rows:
                conn.executemany(
                    """INSERT OR REPLACE INTO tiktok_listing_quality_issues
                       (product_id, field, code, how_to_solve, quality_tier, suggestion_json, synced_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    issue_rows,
                )
        products_written += len(products)
        print(f"    batch {i // BATCH_SIZE + 1}/{-(-len(product_ids) // BATCH_SIZE)}: "
              f"{len(products)} products diagnosed", flush=True)
        time.sleep(0.3)  # light pacing; no documented rate limit for this endpoint

    return len(product_ids), products_written, failed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--product-ids", help="comma-separated list of TikTok Shop product ids to diagnose")
    p.add_argument("--product-ids-file", help="path to a file with one product id per line")
    p.add_argument("--limit", type=int, default=None,
                   help="only diagnose the first N product ids (smoke test)")
    args = p.parse_args()

    check_required_env()

    if args.product_ids:
        product_ids = [s.strip() for s in args.product_ids.split(",") if s.strip()]
    elif args.product_ids_file:
        product_ids = [ln.strip() for ln in Path(args.product_ids_file).read_text().splitlines() if ln.strip()]
    else:
        raise SystemExit(
            "No product ids to diagnose. Pass --product-ids A,B,C or "
            "--product-ids-file path.txt — this scaffold has no product/catalog "
            "table to source a default list from."
        )
    if args.limit:
        product_ids = product_ids[:args.limit]

    db.init_db()
    conn = db.connect()
    ensure_schema(conn)

    started = db.now()
    try:
        requested, written, failed = run(conn, product_ids)
    except Exception as e:  # noqa: BLE001
        conn.close()
        db.log_sync(PLATFORM, started, 0, "error", str(e))
        raise
    conn.close()

    if written == 0:
        status, msg = "error", f"0 of {requested} products diagnosed"
    elif failed:
        status = "degraded"
        msg = f"{written}/{requested} diagnosed; {len(failed)} ids failed even as singles"
    else:
        status, msg = "ok", f"{written}/{requested} diagnosed"
    db.log_sync(PLATFORM, started, written, status, msg)
    print(f"TikTok listing quality: {msg}")
    if failed:
        print(f"    failed ids (first 10): {failed[:10]}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
