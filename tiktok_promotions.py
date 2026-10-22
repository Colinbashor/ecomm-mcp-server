"""Create and fill TikTok Shop fixed-price promotions (Promotion API 202309).

    python tiktok_promotions.py list [--status ONGOING]                     # read-only
    python tiktok_promotions.py show ACTIVITY_ID [-v]                       # read-only
    python tiktok_promotions.py export ACTIVITY_ID --out live.csv           # snapshot live prices -> csv
    python tiktok_promotions.py create --title T --begin ISO --end ISO      # dry run
    python tiktok_promotions.py create --title T --begin ISO --end ISO --apply
    python tiktok_promotions.py add ACTIVITY_ID --prices-file prices.csv    # dry run
    python tiktok_promotions.py add ACTIVITY_ID --prices-file prices.csv --apply

WRITE-CAPABLE. Nothing is written without --apply; a dry run only reads.

--begin/--end are ISO timestamps WITH a UTC offset (e.g. 2026-11-30T23:59:59-08:00).
--prices-file is a CSV with columns product_id,price.

WHAT THIS IS: a FIXED_PRICE activity at PRODUCT level. An activity is created
EMPTY, then products are attached with PUT .../activities/{id}/products. At
product level the id is the PRODUCT id (not a SKU id) and one price applies to
every SKU of that product.

API TRAPS (each one an error the API returned):
  * The add-products call needs activity_id in the BODY as well as the path
    ("ActivityId is a required field" otherwise), and it is a PUT -- POST there
    answers "no schema found".
  * The price is the flat field `activity_price_amount` (a string), not the
    nested activity_price {amount, currency} that the READ returns.
  * quantity_limit and quantity_per_user are both required; -1 = no limit.
  * Passing SKU ids as the product id fails ("product IDs cannot be found").
  * At most 20 products per add call; business code 36009037 is the rate limit
    (back off and retry).
  * A product already in the activity is OVERWRITTEN by a second add (an
    upsert) -- that is how a price is changed. Others editing the same activity
    in Seller Center are overwritten too, so `export` and diff first.
  * Adding to an ONGOING activity changes live prices immediately.
  * Search takes page_size as an INTEGER in the JSON body, not a query param.

NOT BUILT (do not assume): removing a product from an activity and
deactivating an activity. Both exist in the API; verify them on a throwaway
activity before relying on them.

Every write is appended to reports/tiktok_promotions_log.csv (gitignored).
Auth: the same TikTok Shop app credentials as warehouse/connectors/tiktok_shop.py
(TIKTOK_APP_KEY, TIKTOK_APP_SECRET, TIKTOK_ACCESS_TOKEN, TIKTOK_REFRESH_TOKEN,
TIKTOK_SHOP_CIPHER); the app needs the promotion read/write scope.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse.connectors import tiktok_shop as tts

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "reports" / "tiktok_promotions_log.csv"
BASE_PATH = "/promotion/202309/activities"
BATCH = 20                  # API max per add call
PAUSE_S = 2.0
RATE_LIMIT_CODE = 36009037
MAX_ATTEMPTS = 5
REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN",
                "TIKTOK_SHOP_CIPHER")


def check_required_env() -> list[str]:
    return [k for k in REQUIRED_ENV if not os.environ.get(k)]


def _send(method: str, url: str, params: dict, raw: str, headers: dict) -> tuple[int, dict]:
    """The one network call; tests replace it."""
    r = requests.request(method, url, params=params, data=raw or None, timeout=60,
                         headers=headers)
    return r.status_code, r.json()


def call(method: str, path: str, body: dict | None = None, query: dict | None = None,
         sleep=None) -> dict:
    """One signed request. Re-signs on every attempt (fresh timestamp), refreshes the
    token at most once, and backs off on the rate-limit code / HTTP 429 / transport
    errors. Returns the response envelope; the caller checks `code`."""
    sleep = sleep or time.sleep
    raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
    refreshed = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        params = {"app_key": os.environ["TIKTOK_APP_KEY"],
                  "timestamp": str(int(time.time())),
                  "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"], **(query or {})}
        params["sign"] = tts._sign(path, params, raw, os.environ["TIKTOK_APP_SECRET"])
        headers = {"Content-Type": "application/json",
                   "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]}
        try:
            status, data = _send(method, f"{tts.BASE}{path}", params, raw, headers)
        except (requests.RequestException, ValueError) as e:
            print(f"    transport error ({type(e).__name__}); retry {attempt}", flush=True)
            sleep(5 * attempt)
            continue
        if data.get("code") in tts.TOKEN_EXPIRED_CODES and not refreshed:
            tts._refresh_access_token()
            refreshed = True
            continue
        if data.get("code") == RATE_LIMIT_CODE or status == 429:
            sleep(5 * attempt)
            continue
        return data
    raise RuntimeError(f"{method} {path}: gave up after {MAX_ATTEMPTS} attempts")


def log_row(action: str, ref: str, ok: bool, data: dict, log: Path | None = None) -> None:
    log = log or LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "action", "ref", "ok", "code", "message", "request_id"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), action, ref,
                    int(ok), data.get("code"), data.get("message"), data.get("request_id")])


def epoch(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        raise SystemExit(f"{iso!r} has no UTC offset -- add one, e.g. -08:00")
    return int(dt.timestamp())


def read_prices(path: Path) -> dict[str, str]:
    """product_id -> price string. Refuses the whole file on any bad row."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows or "product_id" not in rows[0] or "price" not in rows[0]:
        raise SystemExit(f"{path}: need product_id and price columns")
    out: dict[str, str] = {}
    for n, r in enumerate(rows, start=2):
        pid = (r["product_id"] or "").strip()
        price = (r["price"] or "").replace("$", "").replace(",", "").strip()
        if not pid.isdigit():
            # a spreadsheet round-trip turns long ids into 1.23E+18
            raise SystemExit(f"{path} line {n}: non-numeric product id {pid!r}")
        try:
            value = float(price)
        except ValueError:
            raise SystemExit(f"{path} line {n}: price {price!r} is not a number") from None
        if value <= 0:
            raise SystemExit(f"{path} line {n}: price must be positive")
        if pid in out:
            raise SystemExit(f"{path} line {n}: duplicate product id {pid}")
        out[pid] = f"{value:g}"
    return out


def live_prices(activity_id: str) -> tuple[dict, dict[str, str]]:
    data = call("GET", f"{BASE_PATH}/{activity_id}")
    if data.get("code") != 0:
        raise SystemExit(f"read failed: {data.get('code')} {data.get('message')}")
    d = data.get("data") or {}
    return d, {p["id"]: p["activity_price"]["amount"] for p in d.get("products", [])}


def cmd_list(a) -> int:
    body = {"page_size": 100, **({"status": a.status} if a.status else {})}
    data = call("POST", f"{BASE_PATH}/search", body)
    if data.get("code") != 0:
        raise SystemExit(f"search failed: {data.get('code')} {data.get('message')}")
    for act in (data.get("data") or {}).get("activities", []):
        print(act["id"], act["status"], act["activity_type"], act.get("product_level"),
              datetime.fromtimestamp(act["begin_time"], timezone.utc).date(),
              datetime.fromtimestamp(act["end_time"], timezone.utc).date(), act["title"])
    return 0


def cmd_show(a) -> int:
    d, prods = live_prices(a.activity_id)
    print(d.get("status"), d.get("title"), "products:", len(prods))
    if a.verbose:
        for pid, amt in prods.items():
            print(" ", pid, amt)
    return 0


def cmd_export(a) -> int:
    """Write the LIVE activity to a prices CSV (product_id,price). Edit it, then `add`."""
    _, prods = live_prices(a.activity_id)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "price"])
        w.writerows(prods.items())
    print(f"wrote {len(prods)} products to {out}")
    return 0


def cmd_create(a) -> int:
    begin, end = epoch(a.begin), epoch(a.end)
    if end <= begin:
        raise SystemExit("--end must be after --begin")
    body = {"title": a.title, "activity_type": "FIXED_PRICE", "product_level": "PRODUCT",
            "begin_time": begin, "end_time": end}
    print("create:", json.dumps(body))
    if not a.apply:
        print("DRY RUN -- add --apply to create")
        return 0
    data = call("POST", BASE_PATH, body)
    ok = data.get("code") == 0
    log_row("create", a.title, ok, data)
    print(data.get("code"), data.get("message"), (data.get("data") or {}).get("activity_id"))
    return 0 if ok else 1


def cmd_add(a) -> int:
    prices = read_prices(Path(a.prices_file))
    d, before = live_prices(a.activity_id)
    changed = {p: amt for p, amt in prices.items() if before.get(p) is None
               or float(before[p]) != float(amt)}
    print(f"{len(prices)} products in file; {sum(p in before for p in prices)} already in the "
          f"activity ({d.get('status')}); {len(changed)} new or repriced")
    if not a.apply:
        for pid, amt in changed.items():
            print(" ", pid, before.get(pid, "-"), "->", amt)
        print("DRY RUN -- add --apply (adding to an ONGOING activity changes live prices)")
        return 0
    items = [{"id": p, "activity_price_amount": amt, "quantity_limit": -1, "quantity_per_user": -1}
             for p, amt in changed.items()]
    failed = 0
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        data = call("PUT", f"{BASE_PATH}/{a.activity_id}/products",
                    {"activity_id": a.activity_id, "products": batch})
        ok = data.get("code") == 0
        failed += 0 if ok else len(batch)
        for it in batch:
            log_row("add", f"{a.activity_id}:{it['id']}={it['activity_price_amount']}", ok, data)
        print(f"  batch {i // BATCH + 1}: {data.get('code')} {data.get('message')}", flush=True)
        if i + BATCH < len(items):
            time.sleep(a.pause)
    _, live = live_prices(a.activity_id)
    bad = {p: (amt, live.get(p)) for p, amt in changed.items()
           if live.get(p) is None or float(live[p]) != float(amt)}
    print("VERIFIED -- every price matches" if not bad else f"MISMATCH/MISSING: {bad}")
    return 0 if not bad and not failed else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="read-only")
    p.add_argument("--status")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show", help="read-only")
    p.add_argument("activity_id")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("export", help="read-only: live prices to a CSV")
    p.add_argument("activity_id")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("create", help="WRITE with --apply: an empty FIXED_PRICE activity")
    p.add_argument("--title", required=True)
    p.add_argument("--begin", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_create)
    p = sub.add_parser("add", help="WRITE with --apply: set product prices in an activity")
    p.add_argument("activity_id")
    p.add_argument("--prices-file", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE_S, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_add)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    load_dotenv(ROOT / ".env")
    missing = check_required_env()
    if missing:
        print(f"tiktok_promotions: missing {', '.join(missing)} (see docs/tiktok-shop.md)",
              file=sys.stderr)
        return 2
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
