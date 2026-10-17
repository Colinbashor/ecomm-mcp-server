r"""
Meta Marketing API MUTATE operations — the Meta counterpart to
`google_ads_mutate.py`, and the only write-capable Meta file in this repo.

Every other Meta file here (`warehouse/connectors/meta_ads.py`,
`meta_ads_detail_sync.py`) only issues GET requests against the Graph API.
This one POSTs updates, so it can change a live ad account: pause or resume a
campaign / ad set / ad, change an ad set's daily budget, copy an ad set
(with its ads) into a different campaign, or copy the ads of one ad set into
another existing ad set.

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

`copy-ads` HAS THE SAME LIMITATION BUT IS DRY-RUNNABLE: its dry run (no
`--execute`) only READS — it lists the source ad set's ads and reports what
WOULD be copied — and sends no POST at all. `--execute` copies each ad
(`/{ad_id}/copies` with `adset_id`) PAUSED, skipping any ad whose name already
exists in the destination so a re-run never duplicates. Typical use: a
published ad set's `optimization_goal` cannot be edited and ads cannot be
moved, so changing (say) a conversions ad set to a value-optimised one means
building a new ad set and copying the ads across, which keeps each ad's
creative (caption, link, URL tags, partner/branded-content settings).

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

  python meta_ads_mutate.py rename --object-id 1234567890 --name "New name"
  python meta_ads_mutate.py rename-from-csv --csv rename_plan.csv            # dry run, whole plan
  python meta_ads_mutate.py rename-from-csv --csv rename_plan.csv --execute

  # no dry run exists for copies -- --execute is required; created PAUSED
  python meta_ads_mutate.py copy-adset --adset-id 1234567890 \
      --dest-campaign-id 9876543210 --execute

  # copy the ads of one ad set into another EXISTING ad set (dry run only lists)
  python meta_ads_mutate.py copy-ads --source-adset-id 1234567890 --dest-adset-id 1122334455
  python meta_ads_mutate.py copy-ads --source-adset-id 1234567890 --dest-adset-id 1122334455 \
      --limit 1 --execute        # canary: copy a single ad first

  # create a product-page-visitors website audience (URL contains a keyword,
  # last 180 days, your pixel). Needs META_PIXEL_ID. Idempotent by name.
  python meta_ads_mutate.py create-pdp-audience --name "Blue Jacket PDP Visitors" \
      --url-keyword blue-jacket
  python meta_ads_mutate.py create-pdp-audience --name "Blue Jacket PDP Visitors" \
      --url-keyword blue-jacket --execute

  # video-viewers engagement audience (watched >= 25% of any of these ad
  # videos, 365 days). Needs META_PAGE_ID. Idempotent by name.
  python meta_ads_mutate.py create-vv-audience --name "Blue Jacket VV - 25%" \
      --video-ids 1111111111 2222222222
  python meta_ads_mutate.py create-vv-audience --name "Blue Jacket VV - 25%" \
      --video-ids 1111111111 2222222222 --event video_view_50_percent --execute

  # add / remove include and exclude audiences on one ad set; turn Advantage+
  # audience off. Dry run (validate_only) by default.
  python meta_ads_mutate.py edit-adset-audiences --adset-id 1234567890 \
      --include 2345678901 --advantage-off
  python meta_ads_mutate.py edit-adset-audiences --adset-id 1234567890 \
      --exclude 3456789012 4567890123 --execute
  python meta_ads_mutate.py edit-adset-audiences --adset-id 1234567890 --remove-exclude 3456789012

  # read-only
  python meta_ads_mutate.py check-targeting --adset-id 1234567890 1234567891
  python meta_ads_mutate.py audience-info --ids 3456789012 4567890123

`create-pdp-audience` / `create-vv-audience` are pure audience definitions:
they deliver nothing and spend nothing until an ad set targets them.
`/customaudiences` has no validate_only, so without --execute they only print
what they would create, and an audience whose name already exists is never
created twice.

TARGETING GUARD: `resume-adset`, `resume-campaign` (every ACTIVE child ad set)
and `copy-adset` (the SOURCE ad set) first read the live targeting and check it
against your targeting rules; they REFUSE on a HARD break unless
`--force-targeting` is passed. Copying a broken ad set is exactly how a batch
of "warm" ad sets can end up with no include audience and quietly run broad.
`edit-adset-audiences` prints the same check for the edited targeting, and
`check-targeting` runs it read-only (exit 1 on any HARD break).

The rules live in a JSON file (META_TARGETING_RULES, default
`meta_targeting_rules.json` next to this script; see
`meta_targeting_rules.example.json`). Each rule names a funnel stage, a regex
matched against the ad set name and then the campaign name, and what that
stage requires (Advantage+ audience off, a real include audience, no
non-lookalike include, required exclusions). With no rules file only the
universal check runs (an audience both included and excluded is a HARD
break) and every ad set gets a WARN that no stage rule applies.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # audience and ad set names carry non-ASCII

API_VERSION = "v23.0"
BASE = f"https://graph.facebook.com/{API_VERSION}"
REQUEST_TIMEOUT_SECONDS = 60
COPY_SPACING_SECONDS = 31  # pause between ad copies (Meta write limit ~1 / 30s)
COPY_MAX_ATTEMPTS = 4
RATE_LIMIT_CODES = (613, 17)  # call-count rate limits worth sleeping through
RATE_LIMIT_SLEEP_SECONDS = 35  # Meta write limit (code 613) is roughly 1 write / 30s


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
        print(f"FAILED — {err.get('message', data)} {err.get('error_user_msg', '')}".rstrip())
        print(f"  type={err.get('type')} code={err.get('code')} subcode={err.get('error_subcode')}")
        sys.exit(1)

    mode = "EXECUTED" if execute else "VALIDATE_ONLY passed"
    print(f"{mode} — POST {object_id} {payload} -> {data}")
    if not execute:
        print("Re-run with --execute to actually make this change.")
    return data


def resume_campaign(args):
    """Resuming a campaign re-starts every ad set under it whose own status is
    ACTIVE, so each of those is checked first (see check_targeting)."""
    sets = _get(f"{args.campaign_id}/adsets", fields="id,status", limit=200).get("data", [])
    breaks = [s["id"] for s in sets if s.get("status") == "ACTIVE" and _guard_print(s["id"])]
    _refuse_on_breaks(breaks, args)
    _post(args.campaign_id, {"status": "ACTIVE"}, args.execute)


def pause_campaign(args):
    _post(args.campaign_id, {"status": "PAUSED"}, args.execute)


def resume_adset(args):
    _refuse_on_breaks([args.adset_id] if _guard_print(args.adset_id) else [], args)
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
    `--go-live` to create it ACTIVE immediately.

    The SOURCE targeting is checked first (see check_targeting): copying an ad
    set with broken targeting multiplies the break."""
    if not args.execute:
        print("REFUSED — copy-adset has no dry-run mode (the /copies edge does not "
              "document validate_only support). Re-run with --execute; the copy "
              "will be created PAUSED unless --go-live is also passed.")
        sys.exit(2)
    _refuse_on_breaks([args.adset_id] if _guard_print(args.adset_id) else [], args)
    payload = {
        "campaign_id": args.dest_campaign_id,
        "status_option": "ACTIVE" if args.go_live else "PAUSED",
        "deep_copy": "true",
    }
    _post(f"{args.adset_id}/copies", payload, args.execute, validate_only=False)


def _list_ads(adset_id: str, fields: str) -> list[dict]:
    """GET the ads of an ad set (first 200). An API error raises: an error is
    not "zero ads", and treating it as such would make `copy-ads` believe the
    destination is empty and duplicate everything."""
    data = requests.get(f"{BASE}/{adset_id}/ads",
                        params={"fields": fields, "limit": 200, "access_token": _token()},
                        timeout=REQUEST_TIMEOUT_SECONDS).json()
    if "error" in data:
        raise SystemExit(f"could not list ads of {adset_id}: {data['error'].get('message')}")
    return data.get("data", [])


def copy_ads(args):
    """Copy the ads of one ad set into ANOTHER EXISTING ad set via
    `POST /{ad_id}/copies` with `adset_id`, keeping each ad's creative. Copies
    are created PAUSED.

    `/copies` has no `validate_only`, so without `--execute` this only lists
    what WOULD be copied (reads only). Ads in the source that are DELETED or
    ARCHIVED are ignored, and ads whose name already exists in the destination
    are skipped, so a re-run never duplicates. `--limit N` caps the number
    copied per run (canary). Writes are spaced and rate-limit errors retried,
    since Meta allows roughly one write every 30 seconds. Needs publish
    permission on the Page/Instagram account behind each ad's creative."""
    skip_status = {"DELETED", "ARCHIVED"}
    src = [a for a in _list_ads(args.source_adset_id, "id,name,effective_status")
           if a.get("effective_status") not in skip_status]
    have = {a["name"] for a in _list_ads(args.dest_adset_id, "id,name")}
    todo = [a for a in src if a["name"] not in have]
    if args.limit:
        todo = todo[:args.limit]
    print(f"source {args.source_adset_id}: {len(src)} ads | already in "
          f"{args.dest_adset_id}: {sum(a['name'] in have for a in src)} | to copy: {len(todo)}")
    for a in todo:
        if not args.execute:
            print(f"  would copy {a['id']}  {a['name']}")
            continue
        err = None
        for _ in range(COPY_MAX_ATTEMPTS):
            data = requests.post(f"{BASE}/{a['id']}/copies", data={
                "adset_id": args.dest_adset_id, "status_option": "PAUSED",
                "rename_options": json.dumps({"rename_strategy": "NO_RENAME"}),
                "access_token": _token()}, timeout=REQUEST_TIMEOUT_SECONDS).json()
            err = data.get("error")
            if err and err.get("code") in RATE_LIMIT_CODES:
                time.sleep(RATE_LIMIT_SLEEP_SECONDS)
                continue
            break
        if err:
            print(f"  FAILED {a['id']}  {a['name']}: {err.get('message')}")
        else:
            print(f"  COPIED {a['id']} -> {data.get('copied_ad_id') or data}  {a['name']}")
        time.sleep(COPY_SPACING_SECONDS)
    if not args.execute:
        print("(dry run: nothing copied; pass --execute)")


def to_minor_units(amount: float, offset: int) -> int:
    """Meta budgets are integers in the account currency's minor unit, scaled
    by the currency's 'offset' — 100 for two-decimal currencies (USD, EUR,
    GBP: 50.00 -> 5000), 1 for zero-decimal ones (JPY, KRW). Compare against
    an existing ad set's `daily_budget` before writing if unsure."""
    return int(round(amount * offset))


def create_pdp_audience(args):
    """Create a product-page-visitors website custom audience: URL contains
    `--url-keyword`, 180-day retention, the pixel in META_PIXEL_ID. Dry run by
    default; an audience with the same name is reported as EXISTS, never duplicated."""
    pixel_id = os.environ.get("META_PIXEL_ID")
    if not pixel_id:
        raise SystemExit("META_PIXEL_ID is not set in .env")
    account = os.environ["META_AD_ACCOUNT_ID"]
    account = account if account.startswith("act_") else "act_" + account
    ex = requests.get(f"{BASE}/{account}/customaudiences", params={
        "fields": "id,name", "limit": 500, "access_token": _token()}).json()
    if "error" in ex:
        raise SystemExit(f"could not list audiences: {ex['error'].get('message')}")
    for a in ex.get("data", []):
        if a["name"] == args.name:
            print(f"EXISTS {a['id']}  {args.name}")
            return
    rule = {"inclusions": {"operator": "or", "rules": [{
        "event_sources": [{"type": "pixel", "id": pixel_id}], "retention_seconds": 180 * 86400,
        "filter": {"operator": "and", "filters": [
            {"operator": "or", "filters": [{"field": "url", "operator": "i_contains", "value": args.url_keyword}]},
            {"field": "url", "operator": "i_contains", "value": ""}]},
        "template": "VISITORS_BY_URL"}]}}
    print(f"  create audience {args.name!r}: visited a URL containing {args.url_keyword!r}, 180d")
    if not args.execute:
        print("(dry run: nothing created; pass --execute)")
        return
    d = requests.post(f"{BASE}/{account}/customaudiences", data={
        "name": args.name, "pixel_id": pixel_id, "retention_days": 180, "rule": json.dumps(rule),
        "prefill": "true", "access_token": _token()}).json()
    print("EXECUTED" if "id" in d else "FAILED", json.dumps(d)[:300])


VV_EVENTS = ("video_watched", "video_view_10s", "video_view_15s", "video_view_25_percent",
             "video_view_50_percent", "video_view_75_percent", "video_completed")


def create_vv_audience(args):
    """Create a video-viewers engagement audience: people who reached at least
    `--event` (default 25% watched) on any of the given ad videos in the last
    `--retention-days` (default 365). The rule is Meta's video engagement rule,
    one entry per video with your Facebook Page (META_PAGE_ID) as context.

    Video ids are the Facebook video ids on the product's ad creatives
    (`video_id` / `video_data.video_id` / `asset_feed_spec` videos) — NOT
    Instagram media ids — and must be the copy OWNED BY YOUR PAGE
    (`GET <video_id>?fields=from` returns META_PAGE_ID). Meta rejects:
      - the ad account's library copy (`act_<id>/advideos`) with (#2654)
        "video isn't associated with a Page" (subcode 1713216);
      - videos from creator/partnership ads, which live on the creator's
        Page, with (#2654) subcode 1713080 ("no permission to advertise for
        the engagement object").
    The token's user (or system user) also needs a role on the Page with the
    Advertise task; `pages_read_engagement` alone is not enough.

    Ownership is not checked here — Meta checks it on create. Filter the id
    list once when building it (`?fields=from`); do not list the whole
    `advideos` library per call, which trips the account-wide read rate limit
    and blocks every other command, writes included, for a while.

    Dry run by default (prints the plan, posts nothing); an audience with the
    same name is reported as EXISTS, never duplicated."""
    page_id = os.environ.get("META_PAGE_ID")
    if not page_id:
        raise SystemExit("META_PAGE_ID is not set in .env")
    account = os.environ["META_AD_ACCOUNT_ID"]
    account = account if account.startswith("act_") else "act_" + account
    vids = list(dict.fromkeys(v.strip() for v in args.video_ids if v.strip()))
    if not vids:
        raise SystemExit("REFUSED — no video ids")
    bad = [v for v in vids if not v.isdigit()]
    if bad:
        print(f"REFUSED — not video ids: {' '.join(bad)}")
        sys.exit(1)
    ex = requests.get(f"{BASE}/{account}/customaudiences", params={
        "fields": "id,name", "limit": 500, "access_token": _token()},
        timeout=REQUEST_TIMEOUT_SECONDS).json()
    if "error" in ex:
        raise SystemExit(f"could not list audiences: {ex['error'].get('message')}")
    for a in ex.get("data", []):
        if a["name"] == args.name:
            print(f"EXISTS {a['id']}  {args.name}")
            return
    rule = [{"event_name": args.event, "object_id": v, "context_id": page_id} for v in vids]
    print(f"  create audience {args.name!r}: {args.event} on {len(vids)} videos "
          f"({', '.join(vids[:5])}{' ...' if len(vids) > 5 else ''}), {args.retention_days}d")
    if not args.execute:
        print("(dry run: nothing created; pass --execute)")
        return
    d = requests.post(f"{BASE}/{account}/customaudiences", data={
        "name": args.name, "subtype": "ENGAGEMENT", "retention_days": args.retention_days,
        "rule": json.dumps(rule), "prefill": "true", "access_token": _token()},
        timeout=REQUEST_TIMEOUT_SECONDS).json()
    print("EXECUTED" if "id" in d else "FAILED", json.dumps(d)[:300])


def set_adset_budget(args):
    """Set an ad set's `daily_budget`. `--daily-amount` is in MAJOR units of
    the account currency (e.g. dollars); it is converted with
    `--currency-offset` (default 100). Only valid on ad sets that own their
    budget — a campaign using Advantage campaign budget (CBO) rejects ad-set
    budgets, and its budget lives on the campaign instead."""
    _post(args.adset_id, {"daily_budget": to_minor_units(args.daily_amount, args.currency_offset)},
          args.execute)


def rename(args):
    """Rename a campaign / ad set / ad. A name write only: it does not touch
    delivery, budget or learning."""
    _post(args.object_id, {"name": args.name}, args.execute)


def _post_quiet(object_id: str, payload: dict, execute: bool, retries: int = 4) -> str:
    """Like `_post` but returns a status string ("ok" or "FAILED ...") instead of
    exiting, and rides out Meta's write rate limit (code 613) by sleeping, so a
    batch can finish. Dry-run unless `execute`."""
    params = dict(payload, access_token=_token())
    if not execute:
        params["execution_options"] = json.dumps(["validate_only"])
    for _ in range(retries):
        data = requests.post(f"{BASE}/{object_id}", data=params,
                             timeout=REQUEST_TIMEOUT_SECONDS).json()
        err = data.get("error")
        if not err:
            return "ok"
        if err.get("code") == 613:
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)
            continue
        return f"FAILED {err.get('message')}"
    return "FAILED rate limit"


def rename_from_csv(args):
    """Apply a rename plan CSV (columns: level,id,current_name,proposed_name).

    Before each write the LIVE name is read and compared with `current_name`; a
    mismatch is skipped and reported, never overwritten, so a name someone
    changed since the plan was drafted is not clobbered. Rows whose live name
    already equals `proposed_name` are reported SAME and not written. Without
    `--execute` every write is a server-side validate-only pass."""
    norm = lambda n: (n or "").strip()
    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    ok = skipped = failed = 0
    for r in rows:
        oid = r["id"]
        live = requests.get(f"{BASE}/{oid}", params={"fields": "name", "access_token": _token()},
                            timeout=REQUEST_TIMEOUT_SECONDS).json().get("name")
        if norm(live) != norm(r["current_name"]):
            print(f"SKIP  {oid}  live name differs: {live!r}")
            skipped += 1
            continue
        if norm(live) == r["proposed_name"]:
            print(f"SAME  {oid}")
            continue
        res = _post_quiet(oid, {"name": r["proposed_name"]}, args.execute)
        tag = "FAIL " if res != "ok" else "DONE " if args.execute else "CHECK"
        print(f"{tag} {r['level']:<8}{oid}  {norm(live)[:50]!r} -> {r['proposed_name']!r}"
              + ("" if res == "ok" else f"  [{res}]"))
        ok += res == "ok"
        failed += res != "ok"
        time.sleep(args.delay)
    mode = "executed" if args.execute else "validated"
    print(f"\n{mode}: {ok}  skipped: {skipped}  failed: {failed}")


# ---- targeting guard + audience editor -------------------------------------

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "meta_targeting_rules.json"


def load_rules(path: str | os.PathLike | None = None) -> list[dict]:
    """The stage rules from META_TARGETING_RULES (or `meta_targeting_rules.json`
    next to this script). A missing file means no stage rules: only the
    universal include/exclude overlap check runs. A file that exists but does
    not parse exits, rather than silently guarding nothing."""
    path = Path(path or os.environ.get("META_TARGETING_RULES") or DEFAULT_RULES_PATH)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data["stages"] if isinstance(data, dict) else data
        for r in rules:
            r["_re"] = re.compile(r["pattern"], re.I)
    except (ValueError, KeyError, TypeError, re.error) as e:
        raise SystemExit(f"could not read targeting rules {path}: {e}")
    return rules


def _get(path: str, **params) -> dict:
    """Read-only GET; exits 1 on an API error (a guard that cannot read the
    live targeting must not let the write through)."""
    params["access_token"] = _token()
    data = requests.get(f"{BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS).json()
    if "error" in data:
        print(f"FAILED reading {path} — {data['error'].get('message')}")
        sys.exit(1)
    return data


def _audiences(ids) -> dict:
    """id -> {name, subtype} for custom audiences, in ONE `?ids=` request."""
    ids = sorted(set(ids))
    return _get("", ids=",".join(ids), fields="name,subtype") if ids else {}


def stage_from_names(adset_name: str, campaign_name: str, rules: list[dict]) -> dict | None:
    """The first rule whose pattern matches the ad set name, else the campaign
    name (ad set first: a warm ad set can sit inside a cold campaign)."""
    for name in (adset_name, campaign_name):
        for rule in rules:
            pat = rule.get("_re") or re.compile(rule["pattern"], re.I)
            if pat.search(name or ""):
                return rule
    return None


def _included_ids(tgt: dict) -> list[str]:
    inc = [a["id"] for a in tgt.get("custom_audiences", []) or []]
    for fs in tgt.get("flexible_spec", []) or []:
        inc += [a["id"] for a in fs.get("custom_audiences", []) or []]
    return inc


def targeting_breaks(rule: dict | None, tgt: dict, aud: dict) -> list[tuple[str, str]]:
    """[(HARD|WARN, message)] for this targeting against `rule` (one stage from
    the rules file, or None). `aud` maps audience id -> {name, subtype} for at
    least the included audiences; lookalikes do not count as a "real" include.

    Rule keys (all optional):
      advantage_audience_off   true -> Advantage+ audience must be 0
      require_include          true -> at least one non-lookalike include
      forbid_include           true -> no non-lookalike include (prospecting)
      required_exclusions      {label: [ids]} -> one of the ids must be excluded
      recommended_inclusions   {label: [ids]} -> WARN when none is included
      include_name_regex       WARN when no included audience name matches
    """
    inc = _included_ids(tgt)
    exc = {a["id"] for a in tgt.get("excluded_custom_audiences", []) or []}
    real_inc = [i for i in inc if aud.get(i, {}).get("subtype") != "LOOKALIKE"]
    adv = (tgt.get("targeting_automation") or {}).get("advantage_audience")
    out: list[tuple[str, str]] = []
    if set(inc) & exc:
        out.append(("HARD", f"audience both included and excluded: {sorted(set(inc) & exc)}"))
    if rule is None:
        out.append(("WARN", "no targeting rule matches this ad set or campaign name: check it by hand"))
        return out
    stage = rule.get("name", "?")
    if rule.get("advantage_audience_off") and adv != 0:
        out.append(("HARD", f"Advantage+ audience is {adv!r}, must be 0 (off) for {stage}"))
    if rule.get("forbid_include") and real_inc:
        out.append(("HARD", f"{stage} with a non-lookalike include: "
                            f"{[aud.get(i, {}).get('name', i) for i in real_inc]}"))
    if rule.get("require_include") and not real_inc:
        out.append(("HARD", f"{stage} with no include audience: it runs broad"))
    missing = [label for label, ids in (rule.get("required_exclusions") or {}).items()
               if not exc & set(ids)]
    if missing:
        out.append(("HARD", f"{stage} missing exclusions: {missing}"))
    miss_inc = [label for label, ids in (rule.get("recommended_inclusions") or {}).items()
                if not set(real_inc) & set(ids)]
    if miss_inc:
        out.append(("WARN", f"{stage} include is missing {miss_inc}"))
    rx = rule.get("include_name_regex")
    if rx and real_inc:
        names = [aud.get(i, {}).get("name", "") for i in real_inc]
        if not any(re.search(rx, n, re.I) for n in names):
            out.append(("WARN", f"{stage} include has no audience matching /{rx}/: {names}"))
    return out


def _print_targeting(tgt: dict, aud: dict) -> None:
    def nm(lst):
        return [aud.get(a["id"], {}).get("name") or a.get("name") or a["id"] for a in lst or []]
    print(f"  include: {nm(tgt.get('custom_audiences')) or '-'}")
    for fs in tgt.get("flexible_spec", []) or []:
        if fs.get("custom_audiences"):
            print(f"  include (flexible_spec): {nm(fs['custom_audiences'])}")
    print(f"  exclude: {nm(tgt.get('excluded_custom_audiences')) or '-'}")
    print(f"  advantage_audience: {(tgt.get('targeting_automation') or {}).get('advantage_audience')!r}")


def _guard_print(adset_id: str, rules: list[dict] | None = None) -> bool:
    """Read the live ad set, print its targeting and every rule break.
    Returns True when there is a HARD break."""
    rules = load_rules() if rules is None else rules
    a = _get(adset_id, fields="name,effective_status,campaign{name},targeting")
    tgt = a.get("targeting") or {}
    aud = _audiences(_included_ids(tgt))
    rule = stage_from_names(a.get("name", ""), a.get("campaign", {}).get("name", ""), rules)
    stage = rule.get("name") if rule else "?"
    print(f"{adset_id}  {a.get('name')!r}  [{a.get('effective_status')}]  stage by name: {stage}")
    _print_targeting(tgt, aud)
    br = targeting_breaks(rule, tgt, aud)
    for level, msg in br:
        print(f"  {level}: {msg}")
    if not br:
        print("  OK: targeting matches the rules")
    return any(level == "HARD" for level, _ in br)


def _refuse_on_breaks(breaks: list, args) -> None:
    if breaks and not getattr(args, "force_targeting", False):
        print(f"REFUSED — targeting breaks a hard rule on {breaks}. Fix it first "
              "(edit-adset-audiences), or pass --force-targeting to proceed anyway.")
        sys.exit(1)


def check_targeting(args):
    """Read-only: print each ad set's targeting and its rule breaks. Exit 1 if
    any HARD break."""
    rules = load_rules()
    hard = [i for i in args.adset_id if _guard_print(i, rules)]
    sys.exit(1 if hard else 0)


def edit_adset_audiences(args):
    """Add / remove include and exclude custom audiences on one ad set, and
    optionally turn Advantage+ audience off.

    Reads the live targeting and changes ONLY `custom_audiences`,
    `excluded_custom_audiences` and `targeting_automation` — geo, age,
    placements and every other key are sent back exactly as read (Meta
    replaces the whole `targeting` object on write, so a partial payload would
    wipe them). Prints before -> after with audience names, then the rule
    check of the edited targeting. Refuses an audience that would be both
    included and excluded, and sends nothing when the live targeting already
    matches."""
    live = _get(args.adset_id, fields="name,effective_status,campaign{name},targeting")
    tgt = dict(live.get("targeting") or {})
    ids = set(args.include + args.exclude + args.remove_include + args.remove_exclude)
    ids |= {x["id"] for k in ("custom_audiences", "excluded_custom_audiences")
            for x in tgt.get(k, []) or []}
    ids |= set(_included_ids(tgt))
    aud = _audiences(ids)
    print(f"{args.adset_id}  {live.get('name')!r}  [{live.get('effective_status')}]")

    def nm(lst):
        return [aud.get(x["id"], {}).get("name") or x["id"] for x in lst] or "-"

    changed = False
    for key, add, drop in (("custom_audiences", args.include, args.remove_include),
                           ("excluded_custom_audiences", args.exclude, args.remove_exclude)):
        old = [{"id": x["id"]} for x in tgt.get(key, []) or []]
        have = {x["id"] for x in old}
        for d in drop:
            if d not in have:
                print(f"  NOTE {d} is not in {key}; nothing to remove")
        new = [x for x in old if x["id"] not in drop] + \
              [{"id": a} for a in dict.fromkeys(add) if a not in have]
        print(f"  {key}: {nm(old)}  ->  {nm(new)}")
        changed |= new != old
        if new:
            tgt[key] = new
        else:
            tgt.pop(key, None)
    if args.advantage_off:
        ta = dict(tgt.get("targeting_automation") or {})
        print(f"  advantage_audience: {ta.get('advantage_audience')!r}  ->  0")
        changed |= ta.get("advantage_audience") != 0
        ta["advantage_audience"] = 0
        # An ad set created with Advantage+ audience on carries age_range but
        # no individual_setting; Meta then rejects advantage_audience=0
        # ("targeting_automation must be enabled to use age_range", subcode
        # 1487079). Supplying individual_setting satisfies it.
        if "age_range" in tgt and "individual_setting" not in ta:
            ta["individual_setting"] = {"age": 1, "gender": 1}
            print(f"  individual_setting: None  ->  {ta['individual_setting']} (needed for age_range)")
        tgt["targeting_automation"] = ta
    inc = {x["id"] for x in tgt.get("custom_audiences", [])}
    exc = {x["id"] for x in tgt.get("excluded_custom_audiences", [])}
    if inc & exc:
        print(f"REFUSED — audience both included and excluded: {sorted(inc & exc)}")
        sys.exit(1)
    rule = stage_from_names(live.get("name", ""), live.get("campaign", {}).get("name", ""),
                            load_rules())
    print(f"  after the edit (stage by name: {rule.get('name') if rule else '?'}):")
    for level, msg in targeting_breaks(rule, tgt, aud) or [("OK", "targeting matches the rules")]:
        print(f"    {level}: {msg}")
    if not changed:
        print("NO CHANGE — the live targeting already has this; nothing sent")
        return
    _post(args.adset_id, {"targeting": json.dumps(tgt)}, args.execute)


def audience_info(args):
    """Read-only: name, type, size, retention and freshness of custom
    audiences. A customer-list audience whose content has not refreshed in
    months is not a real exclusion — `content=` shows when it last did."""
    from datetime import datetime, timezone

    def day(ts):
        return datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat() if ts else "-"

    data = _get("", ids=",".join(args.ids),
                fields="name,subtype,retention_days,time_updated,time_content_updated,"
                       "approximate_count_lower_bound,approximate_count_upper_bound,delivery_status")
    for i in args.ids:
        a = data.get(i, {})
        ds = a.get("delivery_status") or {}
        print(f"{i}  {a.get('name')!r}  {a.get('subtype')}  retention={a.get('retention_days')}d  "
              f"size={a.get('approximate_count_lower_bound')}-{a.get('approximate_count_upper_bound')}  "
              f"updated={day(a.get('time_updated'))}  content={day(a.get('time_content_updated'))}  "
              f"delivery={ds.get('code')} {ds.get('description', '')}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    force = dict(action="store_true",
                 help="resume / copy even though the targeting check finds a HARD break")
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
        if name in ("resume-campaign", "resume-adset"):
            p.add_argument("--force-targeting", **force)
        p.add_argument("--execute", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("copy-adset")
    p.add_argument("--adset-id", required=True)
    p.add_argument("--dest-campaign-id", required=True)
    p.add_argument("--go-live", action="store_true", help="create ACTIVE instead of PAUSED")
    p.add_argument("--force-targeting", **force)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=copy_adset)

    p = sub.add_parser("copy-ads")
    p.add_argument("--source-adset-id", required=True)
    p.add_argument("--dest-adset-id", required=True)
    p.add_argument("--limit", type=int, default=0, help="copy at most N ads this run (0 = all)")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=copy_ads)

    p = sub.add_parser("create-pdp-audience")
    p.add_argument("--name", required=True)
    p.add_argument("--url-keyword", required=True, help="product handle that appears in the page URL")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=create_pdp_audience)

    p = sub.add_parser("create-vv-audience", help="video-viewers engagement audience from ad video ids")
    p.add_argument("--name", required=True)
    p.add_argument("--video-ids", nargs="+", required=True,
                   help="Facebook video ids from the ad account's video library")
    p.add_argument("--event", default="video_view_25_percent", choices=VV_EVENTS)
    p.add_argument("--retention-days", type=int, default=365)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=create_vv_audience)

    p = sub.add_parser("set-adset-budget")
    p.add_argument("--adset-id", required=True)
    p.add_argument("--daily-amount", type=float, required=True,
                   help="in MAJOR units of the account currency (e.g. 50 = 50.00)")
    p.add_argument("--currency-offset", type=int, default=100,
                   help="minor units per major unit: 100 for USD/EUR/GBP, 1 for JPY/KRW")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=set_adset_budget)

    p = sub.add_parser("rename")
    p.add_argument("--object-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=rename)

    p = sub.add_parser("rename-from-csv")
    p.add_argument("--csv", required=True)
    p.add_argument("--delay", type=float, default=2.0, help="seconds between writes")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=rename_from_csv)

    p = sub.add_parser("edit-adset-audiences", aliases=["adset-audience-edit"],
                       help="add/remove include + exclude audiences, optionally Advantage+ audience off")
    p.add_argument("--adset-id", required=True)
    p.add_argument("--include", nargs="+", default=[], metavar="AUDIENCE_ID")
    p.add_argument("--exclude", nargs="+", default=[], metavar="AUDIENCE_ID")
    p.add_argument("--remove-include", nargs="+", default=[], metavar="AUDIENCE_ID")
    p.add_argument("--remove-exclude", nargs="+", default=[], metavar="AUDIENCE_ID")
    p.add_argument("--advantage-off", action="store_true",
                   help="set targeting_automation.advantage_audience to 0")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=edit_adset_audiences)

    p = sub.add_parser("check-targeting", help="read-only: ad set targeting vs the rules file")
    p.add_argument("--adset-id", nargs="+", required=True)
    p.set_defaults(func=check_targeting, execute=False)

    p = sub.add_parser("audience-info", help="read-only: custom audience size / type / freshness")
    p.add_argument("--ids", nargs="+", required=True)
    p.set_defaults(func=audience_info, execute=False)
    return ap


def main():
    args = build_parser().parse_args()
    if args.func in (check_targeting, audience_info):  # read-only: no validate_only banner
        args.func(args)
        return
    if not args.execute:
        print("(validate_only mode — no changes will be made; pass --execute to apply)\n")
    args.func(args)


if __name__ == "__main__":
    main()
