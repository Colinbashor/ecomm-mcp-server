r"""
Meta Marketing API MUTATE operations — the Meta counterpart to
`google_ads_mutate.py`, and the only write-capable Meta file in this repo.

Every other Meta file here (`warehouse/connectors/meta_ads.py`,
`meta_ads_detail_sync.py`) only issues GET requests against the Graph API.
This one POSTs updates, so it can change a live ad account: pause or resume a
campaign / ad set / ad, change an ad set's daily budget, or copy an ad set
(with its ads) into a different campaign.

SAME SAFETY DISCIPLINE AS THE GOOGLE ADS SCRIPT: every field update defaults to
a dry run. Meta's equivalent of Google's `validate_only` is the
`execution_options=["validate_only"]` POST param, which runs full server-side
validation (real object ids, real permission and policy checks) without
committing anything. Only `--execute` drops that param and commits. Always run
without `--execute` first, read the result, THEN re-run with `--execute` once it
validates clean.

Meta returns `{"success": true}` on BOTH a real commit and a validate-only pass
— there is no distinct "would succeed" response — so `_post()` prints the mode
explicitly (`EXECUTED` vs `VALIDATE_ONLY passed`) to avoid any ambiguity about
what just happened.

`copy-adset` IS THE EXCEPTION: the `/{adset_id}/copies` edge is not documented
to honour `validate_only`, so a "dry run" could still create a real copy.
Rather than trust an undocumented behaviour, that subcommand REFUSES to run
without `--execute`, and the copy is created PAUSED unless `--go-live` is also
passed — so even an executed copy is held for inspection before it can spend.

AUTH — TWO INDEPENDENT PERMISSION AXES (same shape as Google Ads):
  1. The access token's OAuth scopes must include `ads_management`
     (`ads_read` alone is enough for the read-only connectors, not for this).
     Check with `GET /debug_token`.
  2. The token's user/system user must ALSO hold an Advertiser-or-higher role
     on the ad account in Business Manager. A token with `ads_management` but
     only an analyst/read-level role fails every write — including
     validate-only ones — with a permissions error. Fix the role, not the
     scope.

Env: `META_ACCESS_TOKEN` (shared with the read-only connectors). The Graph API
version is pinned to match `warehouse/connectors/meta_ads.py`; bump both
together.

USAGE
  python meta_ads_mutate.py pause-campaign --campaign-id 1234567890
  python meta_ads_mutate.py pause-campaign --campaign-id 1234567890 --execute

  python meta_ads_mutate.py resume-adset --adset-id 1234567890
  python meta_ads_mutate.py resume-ad --ad-id 1234567890 --execute

  python meta_ads_mutate.py set-adset-budget --adset-id 1234567890 --daily-amount 50
  python meta_ads_mutate.py set-adset-budget --adset-id 1234567890 --daily-amount 50 --execute

  # no dry run exists for copies -- --execute is required; created PAUSED
  python meta_ads_mutate.py copy-adset --adset-id 1234567890 \
      --dest-campaign-id 9876543210 --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

API_VERSION = "v23.0"
BASE = f"https://graph.facebook.com/{API_VERSION}"
REQUEST_TIMEOUT_SECONDS = 60


def _token() -> str:
    return os.environ["META_ACCESS_TOKEN"]


def _post(object_id: str, payload: dict, execute: bool, *, validate_only: bool = True) -> dict:
    """POST an update to a Meta object.

    Adds `execution_options=["validate_only"]` unless `execute` is true (or the
    caller passes `validate_only=False` for an edge that does not support it —
    only `copy_adset` does, and it guards `execute` itself). Exits 1 on any API
    error, printing Meta's message/type/code/subcode."""
    params = dict(payload)
    params["access_token"] = _token()
    if not execute and validate_only:
        params["execution_options"] = json.dumps(["validate_only"])

    r = requests.post(f"{BASE}/{object_id}", data=params, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        data = r.json()
    except ValueError:
        data = {"error": {"message": r.text[:500]}}

    if r.status_code != 200 or "error" in data:
        err = data.get("error", {})
        print(f"FAILED — {err.get('message', data)}")
        print(f"  type={err.get('type')} code={err.get('code')} subcode={err.get('error_subcode')}")
        sys.exit(1)

    mode = "EXECUTED" if execute else "VALIDATE_ONLY passed"
    print(f"{mode} — POST {object_id} {payload} -> {data}")
    if not execute:
        print("Re-run with --execute to actually make this change.")
    return data


def resume_campaign(args):
    _post(args.campaign_id, {"status": "ACTIVE"}, args.execute)


def pause_campaign(args):
    _post(args.campaign_id, {"status": "PAUSED"}, args.execute)


def resume_adset(args):
    _post(args.adset_id, {"status": "ACTIVE"}, args.execute)


def pause_adset(args):
    _post(args.adset_id, {"status": "PAUSED"}, args.execute)


def resume_ad(args):
    """Resume a single ad. Useful when an ad set reads ACTIVE but is not
    delivering: an ACTIVE ad set whose ads are all PAUSED spends nothing, and
    Ads Manager's ad-set row does not make that obvious. Check
    `effective_status` on the ads, not just the ad set's `status`."""
    _post(args.ad_id, {"status": "ACTIVE"}, args.execute)


def pause_ad(args):
    _post(args.ad_id, {"status": "PAUSED"}, args.execute)


def copy_adset(args):
    """Copy an ad set (and its ads — `deep_copy=true`) into a DIFFERENT
    campaign. Meta has no native ad-set move; this is the closest equivalent,
    but it creates a genuinely NEW ad set with its own fresh learning phase —
    none of the source's delivery or conversion history carries over.

    The `/copies` edge is not documented to support `validate_only`, so this
    refuses to run without `--execute` instead of sending a "dry run" that
    might really create a copy. The safety net is `status_option=PAUSED` by
    default: the copy is created but held paused for inspection. Pass
    `--go-live` to create it ACTIVE immediately."""
    if not args.execute:
        print("REFUSED — copy-adset has no dry-run mode (the /copies edge does not "
              "document validate_only support). Re-run with --execute; the copy "
              "will be created PAUSED unless --go-live is also passed.")
        sys.exit(2)
    payload = {
        "campaign_id": args.dest_campaign_id,
        "status_option": "ACTIVE" if args.go_live else "PAUSED",
        "deep_copy": "true",
    }
    _post(f"{args.adset_id}/copies", payload, args.execute, validate_only=False)


def to_minor_units(amount: float, offset: int) -> int:
    """Meta budgets are integers in the account currency's minor unit, scaled
    by the currency's 'offset' — 100 for two-decimal currencies (USD, EUR,
    GBP: 50.00 -> 5000), 1 for zero-decimal ones (JPY, KRW). Compare against
    an existing ad set's `daily_budget` before writing if unsure."""
    return int(round(amount * offset))


def set_adset_budget(args):
    """Set an ad set's `daily_budget`. `--daily-amount` is in MAJOR units of
    the account currency (e.g. dollars); it is converted with
    `--currency-offset` (default 100). Only valid on ad sets that own their
    budget — a campaign using Advantage campaign budget (CBO) rejects ad-set
    budgets, and its budget lives on the campaign instead."""
    _post(args.adset_id, {"daily_budget": to_minor_units(args.daily_amount, args.currency_offset)},
          args.execute)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, flag, func in [
        ("resume-campaign", "--campaign-id", resume_campaign),
        ("pause-campaign", "--campaign-id", pause_campaign),
        ("resume-adset", "--adset-id", resume_adset),
        ("pause-adset", "--adset-id", pause_adset),
        ("resume-ad", "--ad-id", resume_ad),
        ("pause-ad", "--ad-id", pause_ad),
    ]:
        p = sub.add_parser(name)
        p.add_argument(flag, required=True)
        p.add_argument("--execute", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("copy-adset")
    p.add_argument("--adset-id", required=True)
    p.add_argument("--dest-campaign-id", required=True)
    p.add_argument("--go-live", action="store_true", help="create ACTIVE instead of PAUSED")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=copy_adset)

    p = sub.add_parser("set-adset-budget")
    p.add_argument("--adset-id", required=True)
    p.add_argument("--daily-amount", type=float, required=True,
                   help="in MAJOR units of the account currency (e.g. 50 = 50.00)")
    p.add_argument("--currency-offset", type=int, default=100,
                   help="minor units per major unit: 100 for USD/EUR/GBP, 1 for JPY/KRW")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=set_adset_budget)
    return ap


def main():
    args = build_parser().parse_args()
    if not args.execute:
        print("(validate_only mode — no changes will be made; pass --execute to apply)\n")
    args.func(args)


if __name__ == "__main__":
    main()
