r"""
Funnel stage for a Meta ad set, derived from its TARGETING, never from its name.

WHY: campaign and ad set names are typed by hand and drift ("Prospecting -
Product Audiences" may really be product-level retargeting; a product-warm ad
set can sit in a cold campaign). A tier read off the name yields a plausible
number, not an error, so the stage comes from the custom audiences the ad set
actually includes.

STAGES (first match wins, most specific audience first)
  retention     includes a customer-list audience
  retargeting   includes site-visitor or page/social-engager audiences
  warm_product  includes product-page-visitor or video-viewer audiences
  cold          includes no custom audience (exclusions are not read)
  unknown       has no targeting to read, or includes an audience whose name
                matches none of the patterns -- reported as such, never guessed

CONFIGURING THE PATTERNS
Audience matching is by custom-audience NAME (audience ids differ per product
and per account), so the regexes below are a convention you adapt to your own
audience naming scheme. Override any of them without editing code via env vars
holding a case-insensitive regular expression:

  META_FUNNEL_RETENTION_PATTERN      default: customer
  META_FUNNEL_RETARGETING_PATTERN    default: website visitors|engagers|site visitors
  META_FUNNEL_WARM_PRODUCT_PATTERN   default: pdp|product page|video viewers|\bvv\b

An audience that none of them recognises yields `unknown`; the syncs that call
this log `degraded` when any ad set is `unknown`, which is the cue to add a
pattern.

Read-only: this module only issues GET requests.

USAGE
  python meta_funnel.py        # table of every active ad set and its stage
Env: META_ACCESS_TOKEN, META_AD_ACCOUNT_ID (standalone listing only).
"""
from __future__ import annotations

import json
import os
import re

STAGES = ("retention", "retargeting", "warm_product", "cold", "unknown")

_DEFAULTS = {
    "META_FUNNEL_RETENTION_PATTERN": r"customer",
    "META_FUNNEL_RETARGETING_PATTERN": r"website visitors|engagers|site visitors",
    "META_FUNNEL_WARM_PRODUCT_PATTERN": r"pdp|product page|video viewers|\bvv\b",
}


def _pattern(var: str) -> re.Pattern:
    return re.compile(os.environ.get(var) or _DEFAULTS[var], re.I)


def classify_targeting(targeting: dict | None) -> str:
    """Return one of STAGES for a Graph API ad set `targeting` object."""
    if not targeting:
        return "unknown"
    names = [a.get("name", "") for a in targeting.get("custom_audiences", [])]
    if not names:
        return "cold"
    for stage, var in (("retention", "META_FUNNEL_RETENTION_PATTERN"),
                       ("retargeting", "META_FUNNEL_RETARGETING_PATTERN"),
                       ("warm_product", "META_FUNNEL_WARM_PRODUCT_PATTERN")):
        pat = _pattern(var)
        if any(pat.search(n) for n in names):
            return stage
    return "unknown"  # an included audience we do not recognise: say so, never guess


def _fetch(path: str, params: dict) -> list[dict]:
    import requests
    from dotenv import load_dotenv
    load_dotenv()
    from warehouse.connectors.meta_ads import API_VERSION
    base = f"https://graph.facebook.com/{API_VERSION}/"
    out: list[dict] = []
    url = base + path
    p = dict(params, access_token=os.environ["META_ACCESS_TOKEN"])
    while url:
        r = requests.get(url, params=p, timeout=(10, 30)).json()
        if "error" in r:
            raise RuntimeError(r["error"])
        out += r.get("data", [])
        url = r.get("paging", {}).get("next")
        p = None  # the next url already carries every param
    return out


def fetch_adset_stages() -> list[dict]:
    """Every ACTIVE / WITH_ISSUES ad set in the account with its stage."""
    from dotenv import load_dotenv
    load_dotenv()
    acct = os.environ["META_AD_ACCOUNT_ID"]
    acct = acct if acct.startswith("act_") else "act_" + acct
    sets = _fetch(acct + "/adsets", {
        "fields": "id,name,campaign_id,effective_status,targeting", "limit": 100,
        "filtering": json.dumps([{"field": "effective_status", "operator": "IN",
                                  "value": ["ACTIVE", "WITH_ISSUES"]}])})
    return [dict(adset_id=s["id"], name=s["name"], campaign_id=s["campaign_id"],
                 status=s["effective_status"], stage=classify_targeting(s.get("targeting")))
            for s in sets]


if __name__ == "__main__":
    rows = fetch_adset_stages()
    live = [r for r in rows if r["status"] == "ACTIVE"]
    print(f"{len(live)} ACTIVE ad sets ({len(rows) - len(live)} WITH_ISSUES skipped)")
    for r in live:
        print(f"{r['stage']:<13}{r['status']:<12}{r['adset_id']}  {r['name'][:70]}")
