r"""
Reacher per-product sample-limit override -- a live WRITE tool, deliberately
separate from reacher_sync.py's read-only-guarded transport.

WHY THIS IS A SEPARATE SCRIPT: reacher_sync.py's own docstring explains that
Reacher API keys are commonly provisioned with read/write scope even for
read-only use, and its `_assert_read_only()` guard refuses any write so an
unattended nightly job can never use that half of the key by accident. This
script is the deliberate, human-invoked exception -- kept in its own file so
the guard in reacher_sync.py stays absolute and nothing has to weaken it to
add a write capability.

WHAT IT'S FOR: capping (or clearing) how many free samples Reacher will
auto-approve for creators per product per month -- useful when a product is
selling out and you want to stop new samples going out for it without
touching your other automations, then remove the cap again later.

THE LEVER -- verified against Reacher's own OpenAPI spec
(https://api.reacherapp.com/public/v1/openapi.json -- more trustworthy than a
scraped docs page):
    GET  /samples/product-config             -> {"data": [ProductConfigItem]}
    POST /samples/product-config              body {productId, monthlySampleLimit, ...}
    PUT  /samples/product-config/{config_id}  body {monthlySampleLimit: N|null}  (partial update)
`monthlySampleLimit` is an integer >= 0, or explicit JSON `null` to remove the
cap entirely -- **omitting the field leaves it unchanged**, so a reset must
send an explicit null, not just skip the field. There is no per-product GET
filter; the endpoint returns every product config for the whole shop and this
script filters client-side.

!! THIS DOES NOT FULLY STOP SAMPLING -- READ BEFORE RELYING ON IT !!
`monthlySampleLimit` only caps Reacher's own *automated* sample-approval
funnel for that product. Most TikTok Shop sellers can also approve sample
requests manually in TikTok Seller Center, and this Reacher endpoint has no
reach into that approval path at all -- a human reviewer can still approve a
request regardless of what this script sets. This tool only closes the
automated half of the funnel; flag the product for manual reviewers too if a
hard stop is actually required.

IDENTITY: a Reacher `productId` is the TikTok Shop SPU (item_group_id), the
same identifier your TikTok product-catalog data would use if you're capturing
one. This script needs NO warehouse table to resolve a product -- pass
`--product-id` directly, or `--sku` to resolve against the `sku` field Reacher
itself returns on each ProductConfigItem (whatever SKU value you've given
Reacher for that product, independent of any catalog table in your own
warehouse). If your deployment has its own SKU -> TikTok-product-id mapping
(e.g. a catalog connector you've built), extend `resolve_by_sku()` to check it
first -- Reacher's own `sku` field is a reasonable fallback but reflects
whatever Reacher was told at listing time, not necessarily your source of
truth, and it can be blank for a product Reacher's account was never given a
SKU for.

STATE: active overrides live in `reacher_sample_limit_overrides.json` (next
to this script, NOT the warehouse database -- this is operational state about
a live external system, not warehouse data, so it needs no schema migration).
`reset --all` reads this file, so restoring everything zeroed for a season
needs no memorized product list. Every actual write is also appended to
`reacher_sample_limits_log.txt` as a plain-text audit trail.

SAFETY: every command defaults to a DRY RUN that prints what it would do.
Pass --yes to actually call the API.

USAGE
  reacher_sample_limits.py zero   --sku SKU123 [--sku SKU...] [--limit 0] --reason "selling out" [--yes]
  reacher_sample_limits.py zero   --product-id 1729401428505563994 [--yes]
  reacher_sample_limits.py reset  --sku SKU123 [--yes]
  reacher_sample_limits.py reset  --all [--yes]           # e.g. a seasonal reset
  reacher_sample_limits.py status                          # what's tracked + live drift check
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Constants only -- never reacher_sync's guarded _request/_assert_read_only.
from reacher_sync import BASE, USER_AGENT, REQUIRED_ENV

load_dotenv()

STATE_PATH = Path(__file__).resolve().parent / "reacher_sample_limit_overrides.json"
LOG_PATH = Path(__file__).resolve().parent / "reacher_sample_limits_log.txt"

MANUAL_APPROVAL_NOTE = (
    "NOTE: this only caps Reacher's auto-approval automation for the product.\n"
    "Most TikTok Shop sellers can also approve samples manually in Seller\n"
    "Center -- pause or flag manual approval there too if a hard stop is\n"
    "actually required."
)


def check_required_env() -> None:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


# ---------------------------------------------------------------- HTTP -----
def _headers() -> dict:
    return {
        "x-api-key": os.environ["REACHER_API_KEY"],
        "x-shop-id": os.environ["REACHER_SHOP_ID"],
        "Accept": "application/json",
        "Content-Type": "application/json",
        # Cloudflare 1010 bans the default UA outright -- see reacher_sync.py.
        "User-Agent": USER_AGENT,
    }


def _call(method: str, path: str, body: dict | None = None) -> dict:
    last_error = None
    for attempt in range(5):
        try:
            resp = requests.request(method, f"{BASE}{path}", json=body,
                                     headers=_headers(), timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = f"{type(e).__name__}: {e}"
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 30)))
            continue
        if resp.status_code >= 500:
            last_error = f"{resp.status_code} server error"
            time.sleep(10 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Reacher {method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}
    raise RuntimeError(f"Reacher {method} {path} failed after retries: {last_error}")


def get_all_configs() -> list[dict]:
    return _call("GET", "/samples/product-config").get("data", [])


def find_config_by_pid(configs: list[dict], product_id: str) -> dict | None:
    for c in configs:
        if str(c.get("productId")) == str(product_id):
            return c
    return None


def fetch_usage() -> dict[str, int]:
    data = _call("GET", "/samples/product-usage").get("data", [])
    return {str(u.get("productId")): u.get("usedThisMonth", 0) for u in data}


# --------------------------------------------------------- sku resolution --
def resolve_by_sku(skus: list[str], configs: list[dict]) -> dict[str, list[tuple[str, str]]]:
    """sku (as given) -> [(reacher_product_id, product_name), ...], matched
    against the `sku` field Reacher itself returns on each existing
    product-config row. This needs no warehouse table -- it's whatever SKU
    value your Reacher account already has on file for that product, so it
    only finds a product Reacher has *already* been told the SKU for (e.g.
    one that's had a sample-config touched before, or was synced with SKUs
    at catalog-connection time). A product with no matching config yet, or
    whose SKU was never set in Reacher, won't resolve here -- pass
    `--product-id` directly for those.
    """
    by_sku: dict[str, tuple[str, str]] = {}
    for c in configs:
        sku = (c.get("sku") or "").strip().upper()
        if sku:
            by_sku.setdefault(sku, (str(c.get("productId")), c.get("productName") or ""))

    out: dict[str, list[tuple[str, str]]] = {}
    for raw in skus:
        key = raw.strip().upper()
        match = by_sku.get(key)
        if match:
            out[raw] = [match]
        else:
            out[raw] = []
            print(f"  ! {raw}: no Reacher product-config carries this SKU -- skipping. "
                  f"Pass --product-id directly if you already know Reacher's product id, "
                  f"or extend resolve_by_sku() to check your own catalog mapping first.")
    return out


# -------------------------------------------------------- state file -------
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def log(line: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{stamp}  {line}\n")
    print(line)


# ------------------------------------------------------------ commands -----
def cmd_zero(args: argparse.Namespace) -> None:
    if not args.sku and not args.product_id:
        print("Nothing to do -- pass --sku and/or --product-id.")
        return

    configs = get_all_configs()

    by_pid: dict[str, dict] = {}
    if args.sku:
        resolved = resolve_by_sku(args.sku, configs)
        for sku, matches in resolved.items():
            if not matches:
                # resolve_by_sku already printed the reason.
                continue
            for pid, title in matches:
                rec = by_pid.setdefault(pid, {"skus": set(), "title": title})
                rec["skus"].add(sku)
                if title and not rec["title"]:
                    rec["title"] = title
    for pid in args.product_id:
        by_pid.setdefault(pid, {"skus": set(), "title": ""})

    if not by_pid:
        print("Nothing resolved -- no products to update.")
        return

    print(MANUAL_APPROVAL_NOTE)
    state = load_state()
    for pid, rec in sorted(by_pid.items()):
        existing = find_config_by_pid(configs, pid)
        prev_limit = existing.get("monthlySampleLimit") if existing else None
        label = rec["title"] or (existing or {}).get("productName") or ", ".join(sorted(rec["skus"])) or pid
        sku_note = ", ".join(sorted(rec["skus"])) or "(given directly)"
        print(f"\n{label}")
        print(f"  product_id={pid}  sku(s)={sku_note}")
        print(f"  monthlySampleLimit: {prev_limit!r} -> {args.limit}")
        if not args.yes:
            print("  [dry run -- pass --yes to apply]")
            continue

        try:
            if existing:
                data = _call("PUT", f"/samples/product-config/{existing['id']}",
                             {"monthlySampleLimit": args.limit}).get("data", {})
            else:
                try:
                    data = _call("POST", "/samples/product-config",
                                 {"productId": pid, "monthlySampleLimit": args.limit}).get("data", {})
                except RuntimeError as e:
                    if "409" not in str(e):
                        raise
                    # A config appeared since our GET -- refetch once and update it instead.
                    fresh = find_config_by_pid(get_all_configs(), pid)
                    if not fresh:
                        raise
                    data = _call("PUT", f"/samples/product-config/{fresh['id']}",
                                 {"monthlySampleLimit": args.limit}).get("data", {})
        except RuntimeError as e:
            print(f"  ! FAILED: {e}")
            log(f"ZERO FAILED product_id={pid} sku(s)={sku_note} title={label!r}: {e}")
            continue

        state[pid] = {
            "skus": sorted(rec["skus"]),
            "title": label,
            "config_id": data.get("id"),
            "previous_limit": prev_limit,
            "override_limit": args.limit,
            "reason": args.reason,
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
        log(f"ZERO product_id={pid} sku(s)={sku_note} title={label!r} "
            f"prev_limit={prev_limit} new_limit={args.limit} reason={args.reason!r}")

    if args.yes:
        save_state(state)
        print(f"\nState saved to {STATE_PATH.name} ({len(state)} active override(s)).")


def cmd_reset(args: argparse.Namespace) -> None:
    if not (args.all or args.sku or args.product_id):
        print("Nothing to do -- pass --sku, --product-id, or --all.")
        return

    state = load_state()
    configs = get_all_configs()

    target_pids: set[str] = set()
    if args.all:
        target_pids |= set(state.keys())
    if args.sku:
        resolved = resolve_by_sku(args.sku, configs)
        for sku, matches in resolved.items():
            for pid, _ in matches:
                target_pids.add(pid)
    target_pids |= set(args.product_id)

    if not target_pids:
        print("Nothing resolved -- no products to reset.")
        return

    for pid in sorted(target_pids):
        rec = state.get(pid, {})
        existing = find_config_by_pid(configs, pid)
        live_limit = existing.get("monthlySampleLimit") if existing else None
        label = rec.get("title") or (existing or {}).get("productName") or pid
        print(f"\n{label}  (product_id={pid})")
        if rec:
            print(f"  tracked override: {rec.get('override_limit')} "
                  f"(pre-override value was {rec.get('previous_limit')!r}, "
                  f"reason={rec.get('reason')!r})")
        else:
            print("  (not tracked by this tool -- clearing whatever live limit exists, if any)")
        print(f"  live monthlySampleLimit: {live_limit!r} -> null (uncapped)")

        if not args.yes:
            print("  [dry run -- pass --yes to apply]")
            continue

        try:
            if existing and live_limit is not None:
                _call("PUT", f"/samples/product-config/{existing['id']}",
                      {"monthlySampleLimit": None})
                log(f"RESET product_id={pid} sku(s)={','.join(rec.get('skus', []))} "
                    f"title={label!r} was_override={rec.get('override_limit')} "
                    f"pre_override_value={rec.get('previous_limit')}")
            else:
                log(f"RESET product_id={pid} title={label!r} (no live cap to clear)")
        except RuntimeError as e:
            print(f"  ! FAILED: {e}")
            log(f"RESET FAILED product_id={pid} title={label!r}: {e}")
            continue
        state.pop(pid, None)

    if args.yes:
        save_state(state)
        print(f"\nState saved. {len(state)} override(s) still tracked.")


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state()
    if not state:
        print("No active sample-limit overrides tracked.")
        return
    configs = get_all_configs()
    usage = fetch_usage()
    print(f"{len(state)} active override(s):\n")
    for pid, rec in state.items():
        live = find_config_by_pid(configs, pid)
        live_limit = live.get("monthlySampleLimit") if live else None
        used = usage.get(pid, 0)
        label = rec.get("title") or pid
        drift = ""
        if live_limit != rec.get("override_limit"):
            drift = "  ** DRIFTED -- live value no longer matches what this tool set **"
        print(f"- {label}  (product_id={pid}, sku(s)={','.join(rec.get('skus', [])) or '?'})")
        print(f"    set to {rec.get('override_limit')} on {rec.get('set_at')}  "
              f"reason={rec.get('reason')!r}")
        print(f"    live monthlySampleLimit={live_limit!r}  used_this_month={used}{drift}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Set or clear a per-product Reacher sample limit "
                     "(TikTok Shop creator sampling auto-approval cap).")
    sub = p.add_subparsers(dest="command", required=True)

    z = sub.add_parser("zero", help="cap a product's sample limit (default 0)")
    z.add_argument("--sku", action="append", default=[],
                    help="matched against Reacher's own product-config sku field; repeatable")
    z.add_argument("--product-id", action="append", default=[], dest="product_id",
                   help="Reacher/TikTok product id directly; repeatable")
    z.add_argument("--limit", type=int, default=0)
    z.add_argument("--reason", default="selling out")
    z.add_argument("--yes", action="store_true", help="actually write (default: dry run)")
    z.set_defaults(func=cmd_zero)

    r = sub.add_parser("reset", help="remove a product's sample-limit cap (unlimited)")
    r.add_argument("--sku", action="append", default=[])
    r.add_argument("--product-id", action="append", default=[], dest="product_id")
    r.add_argument("--all", action="store_true", help="reset every currently tracked override")
    r.add_argument("--yes", action="store_true", help="actually write (default: dry run)")
    r.set_defaults(func=cmd_reset)

    s = sub.add_parser("status", help="show tracked overrides + live drift check")
    s.set_defaults(func=cmd_status)

    args = p.parse_args()
    check_required_env()
    args.func(args)


if __name__ == "__main__":
    main()
