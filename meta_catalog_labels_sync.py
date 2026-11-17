"""
Write feed_labels.py's custom labels straight onto a Meta product catalog.

Uses the Catalog Batch API (POST /{catalog_id}/items_batch) so Meta product
sets and Advantage+ catalog ads can filter on the same margin / markdown /
newness / velocity labels as Merchant Center. Labels come from feed_labels.py,
the same code merchant_center_labels_export.py uses, so the two channels can
never disagree about what a label means.

WHY THE API, NOT A SUPPLEMENTARY FEED: a catalog filled by Shopify's Facebook &
Instagram channel often has no feeds at all to attach a supplement to. The
API needs no UI setup, reports per-item errors, and is verifiable per item.

WHAT IT WRITES: custom_label_0..4 only. items_batch UPDATE is a partial update,
so price, inventory, images and everything else the catalog's own source
maintains are untouched. allow_upsert=false, so an id that is not already in
the catalog can never CREATE a product.

KEYED ON retailer_id. For a Shopify-synced catalog that is the variant id;
META_CATALOG_RETAILER_ID_FORMAT selects sku / variant_id / shopify if your
catalog differs. Check one item in Commerce Manager before the first run.

DIFF, NOT BLIND OVERWRITE: every catalog item's current labels are read first
(paging until there is no `next` link, never stopping on a short page) and
only items whose labels differ are sent, so a run with nothing to change makes
no writes. Catalog items absent from the label build (archived, drafts, items
from another source) get their labels CLEARED, so a product that left the
build cannot keep advertising itself under a stale label.

GUARDS (refuse, log `error`, exit 1, catalog untouched):
  * the label build is below META_LABELS_MIN_ROWS rows, or
  * the run would CLEAR labels on more than META_LABELS_MAX_CLEAR_SHARE of the
    catalog.
Both are what a broken upstream build looks like (an empty catalog_shopify,
the wrong id format), and pushing it would wipe the labels live product sets
filter on.

RETRY: every Graph call, GET and POST, retries transient faults (network
errors, Graph codes 1/2 and the rate-limit codes). The POST is retried too:
it SETS label values, so repeating one that did land is harmless.

If the catalog's own source (e.g. the Shopify channel) later resets these
fields, the nightly diff simply re-sends them; a large, steady `changes` count
in sync_log every night is the sign that it does.

STATUS (sync_log platform `meta_catalog_labels`, --execute only): `ok`,
`degraded` when some items errored or a batch had not finished when polling
stopped (the next run's diff re-sends whatever did not land), `error` when
refused, when the build or fetch failed, or when every item errored.

Usage:
  python meta_catalog_labels_sync.py                       # dry run: counts + samples
  python meta_catalog_labels_sync.py --limit 20 --execute  # small live test
  python meta_catalog_labels_sync.py --execute             # full run
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

import feed_labels  # noqa: E402
from warehouse import db  # noqa: E402
from warehouse.connectors.meta_ads import API_VERSION  # noqa: E402

GRAPH = f"https://graph.facebook.com/{API_VERSION}"
PLATFORM = "meta_catalog_labels"
LABELS = feed_labels.LABEL_COLUMNS
EMPTY = ("",) * len(LABELS)

BATCH_SIZE = 3000              # Meta caps items_batch at 5000 requests
PAGE_SIZE = 500
DEFAULT_MIN_ROWS = 100
DEFAULT_MAX_CLEAR_SHARE = 0.30
STATUS_POLL_SECONDS = 10
STATUS_POLL_MAX = 90           # ~15 min per batch before calling it unfinished
MAX_ATTEMPTS = 4
# Graph error codes that clear on their own: unknown/temporary (1, 2) and the
# app/user/page rate limits (4, 17, 32, 613).
TRANSIENT_GRAPH_CODES = frozenset({1, 2, 4, 17, 32, 613})


# ------------------------------------------------------------------ settings
def catalog_id() -> str:
    return os.environ["META_CATALOG_ID"].strip()


def catalog_token() -> str:
    """A dedicated catalog token if set, else the ads system-user token."""
    return (os.environ.get("META_CATALOG_ACCESS_TOKEN", "").strip()
            or os.environ["META_ACCESS_TOKEN"].strip())


def retailer_id_format() -> str:
    return feed_labels.feed_id_format("META_CATALOG_RETAILER_ID_FORMAT", default="variant_id")


def min_rows() -> int:
    return feed_labels._env_int("META_LABELS_MIN_ROWS", DEFAULT_MIN_ROWS)


def max_clear_share() -> float:
    return feed_labels._env_float("META_LABELS_MAX_CLEAR_SHARE", DEFAULT_MAX_CLEAR_SHARE)


def check_required_env() -> None:
    missing = [] if os.environ.get("META_CATALOG_ID", "").strip() else ["META_CATALOG_ID"]
    if not (os.environ.get("META_CATALOG_ACCESS_TOKEN", "").strip()
            or os.environ.get("META_ACCESS_TOKEN", "").strip()):
        missing.append("META_CATALOG_ACCESS_TOKEN (or META_ACCESS_TOKEN)")
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")
    retailer_id_format()                               # raises on an invalid value


# --------------------------------------------------------------------- build
def build_desired(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """retailer_id -> the five label values."""
    rows = feed_labels.build_label_rows(conn)
    keyed, _ = feed_labels.keyed_rows(rows, retailer_id_format(), feed_labels.target_country())
    return {rid: tuple(r[c] or "" for c in LABELS) for rid, r in keyed}


# ------------------------------------------------------------------ Meta I/O
def _call(method: str, url: str, params: dict | None = None, data: dict | None = None,
          sleep: Callable[[float], None] = time.sleep) -> dict:
    """One Graph request with bounded retry on transient faults; a permanent
    Graph error (bad token, missing permission, bad field) fails at once."""
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = requests.request(method, url, params=params, data=data, timeout=180)
            d = r.json()
            if "error" not in d:
                return d
            last = d["error"].get("message")
            if d["error"].get("code") not in TRANSIENT_GRAPH_CODES:
                break
        except (requests.RequestException, ValueError) as exc:
            last = f"transport: {type(exc).__name__}: {exc}"
        if attempt < MAX_ATTEMPTS:
            sleep(5 * attempt)
    raise RuntimeError(f"{method} {url.split('?')[0]} failed: {last}")


def _get(url: str, params: dict | None = None) -> dict:
    return _call("GET", url, params=params)


def _post(url: str, data: dict) -> dict:
    return _call("POST", url, data=data)


def fetch_current(cat_id: str, token: str) -> dict[str, tuple[str, ...]]:
    """Every catalog item's retailer_id -> its current labels. Follows
    paging.next until there is none: a short page only means that request
    carried less, never that the catalog is exhausted."""
    out: dict[str, tuple[str, ...]] = {}
    url: str | None = f"{GRAPH}/{cat_id}/products"
    params: dict | None = {"fields": "retailer_id," + ",".join(LABELS),
                           "limit": PAGE_SIZE, "access_token": token}
    while url:
        d = _get(url, params)
        for it in d.get("data", []):
            rid = str(it.get("retailer_id") or "").strip()
            if rid:
                out[rid] = tuple((it.get(k) or "") for k in LABELS)
        url = (d.get("paging") or {}).get("next")
        params = None            # `next` already carries every param, token included
    return out


def plan(desired: dict[str, tuple[str, ...]], current: dict[str, tuple[str, ...]]
         ) -> tuple[list[tuple[str, tuple[str, ...]]], dict]:
    """(changes, stats). Only catalog items whose labels differ are changed;
    catalog items absent from the build are cleared to blank; build rows
    absent from the catalog are counted and skipped (never created)."""
    changes = []
    n_clear = 0
    for rid, have in current.items():
        want = desired.get(rid, EMPTY)
        if want != have:
            changes.append((rid, want))
            if want == EMPTY:
                n_clear += 1
    stats = dict(catalog=len(current), desired=len(desired),
                 matched=sum(1 for r in desired if r in current),
                 not_in_catalog=sum(1 for r in desired if r not in current),
                 changes=len(changes), clears=n_clear)
    return changes, stats


def guard(stats: dict, floor: int, clear_share: float) -> str | None:
    """A reason to refuse, or None. See GUARDS in the module docstring."""
    if stats["desired"] < floor:
        return f"label build has only {stats['desired']:,} rows (< {floor:,})"
    if stats["catalog"] and stats["clears"] / stats["catalog"] > clear_share:
        return (f"would clear labels on {stats['clears']:,} of {stats['catalog']:,} catalog "
                f"items (> {clear_share:.0%})")
    return None


def batch_requests(changes) -> list[list[dict]]:
    reqs = [{"method": "UPDATE", "data": {"id": rid, **dict(zip(LABELS, labels))}}
            for rid, labels in changes]
    return [reqs[i:i + BATCH_SIZE] for i in range(0, len(reqs), BATCH_SIZE)]


def send_batch(cat_id: str, token: str, reqs: list[dict],
               sleep: Callable[[float], None] = time.sleep) -> dict:
    """POST one items_batch and poll it to completion. Returns
    {handle, status, errors, sample_errors}."""
    d = _post(f"{GRAPH}/{cat_id}/items_batch", data={
        "item_type": "PRODUCT_ITEM", "allow_upsert": "false",
        "requests": json.dumps(reqs), "access_token": token})
    handle = (d.get("handles") or [None])[0]
    if not handle:
        raise RuntimeError(f"items_batch returned no handle: {str(d)[:200]}")
    st: dict = {}
    for _ in range(STATUS_POLL_MAX):
        sleep(STATUS_POLL_SECONDS)
        st = (_get(f"{GRAPH}/{cat_id}/check_batch_request_status",
                   {"handle": handle, "access_token": token}).get("data") or [{}])[0]
        if st.get("status") in ("finished", "error"):
            break
    errs = st.get("errors") or []
    return dict(handle=handle, status=st.get("status") or "unknown",
                errors=int(st.get("errors_total_count") or len(errs)),
                sample_errors=errs[:3])


# ---------------------------------------------------------------------- main
def _open_db() -> sqlite3.Connection:
    if not Path(db.DB_PATH).exists():
        raise RuntimeError(f"warehouse database not found at {db.DB_PATH}")
    return db.connect_readonly()


def run(execute: bool, limit: int | None = None,
        sleep: Callable[[float], None] = time.sleep) -> int:
    started = db.now()
    cat_id, token = catalog_id(), catalog_token()

    def log(status: str, rows: int, msg: str) -> None:
        print(msg)
        if execute:
            db.log_sync(PLATFORM, started, rows, status, msg)

    try:
        conn = _open_db()
        try:
            desired = build_desired(conn)
        finally:
            conn.close()
        current = fetch_current(cat_id, token)
    except Exception as exc:  # noqa: BLE001 -- logged, then reported by main()
        log("error", 0, f"build/fetch failed: {exc}")
        raise

    changes, stats = plan(desired, current)
    print(f"catalog {stats['catalog']:,} items | label build {stats['desired']:,} rows "
          f"({stats['matched']:,} in catalog, {stats['not_in_catalog']:,} not) | "
          f"{stats['changes']:,} need an update ({stats['clears']:,} clears)")
    why = guard(stats, min_rows(), max_clear_share())
    if why:
        log("error", 0, f"REFUSED: {why}; catalog left untouched")
        return 1

    if limit is not None:
        changes = changes[:limit]
    if not execute:
        for rid, labels in changes[:5]:
            print(f"  {rid}: {current.get(rid)} -> {labels}")
        print(f"dry run: would send {len(changes):,} updates. Re-run with --execute.")
        return 0
    if not changes:
        # A measured "nothing to change", not an empty pull: the whole catalog
        # was read and compared.
        log("ok", 0, f"0 changes needed; {stats['catalog']:,} catalog items already current")
        return 0

    sent = errors = unfinished = 0
    samples: list = []
    for reqs in batch_requests(changes):
        res = send_batch(cat_id, token, reqs, sleep=sleep)
        sent += len(reqs)
        errors += res["errors"]
        unfinished += res["status"] != "finished"
        samples += res["sample_errors"]
        print(f"  batch {res['handle']}: {len(reqs):,} sent, status {res['status']}, "
              f"{res['errors']} errors")
    if samples:
        print("  sample errors:", json.dumps(samples[:3])[:600])

    msg = (f"{sent:,} label updates sent ({stats['clears']:,} clears in plan), {errors:,} item "
           f"errors, {unfinished} batch(es) unfinished; catalog {stats['catalog']:,}"
           + (f"; LIMITED to {limit}" if limit is not None else ""))
    if errors >= sent:
        log("error", sent, msg)
        return 1
    log("degraded" if (errors or unfinished) else "ok", sent, msg)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    ap.add_argument("--execute", action="store_true",
                    help="actually write to the catalog (default: dry run)")
    ap.add_argument("--limit", type=int, default=None,
                    help="send only the first N changes (for a live test batch)")
    args = ap.parse_args(argv)
    check_required_env()
    try:
        return run(args.execute, args.limit)
    except Exception as exc:  # noqa: BLE001 -- already logged by run()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
