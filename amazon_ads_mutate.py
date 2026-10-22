r"""
Amazon Ads (Sponsored Products v3) MUTATE operations -- the Amazon counterpart to
google_ads_mutate.py and meta_ads_mutate.py. Everything else Amazon-Ads in this
repo only reads; this module changes the live account.

SAFETY MODEL
  * Every write command is a DRY RUN by default. It reads live state (read-only list
    calls), validates the plan against it, and prints what WOULD change plus a plan id.
  * The client itself refuses any non-list call until --execute flips it on, so a dry
    run cannot write even through a bug.
  * --execute additionally requires --plan-id <id printed by the dry run>, so what is
    pushed is exactly the plan that was reviewed. The id is a hash of the rows that
    would change, so a plan that moved under you (drift, a new row) gets a new id.
  * Every write saves an UNDO CSV (default reports/amazon_ads_undo/, gitignored)
    BEFORE the request is sent, then re-reads live state and verifies.
  * --revert FILE puts back what an undo CSV recorded. A revert is a write like any
    other: dry run first, --execute, its own undo file.

USAGE
  python amazon_ads_mutate.py set-bids --plan bid_plan.csv                    # dry run
  python amazon_ads_mutate.py set-bids --plan bid_plan.csv --execute --plan-id 0a1b2c3d4e
  python amazon_ads_mutate.py set-bids --revert reports/amazon_ads_undo/amazon_bids_undo_<ts>.csv
  python amazon_ads_mutate.py set-bids --revert <file> --execute --plan-id <id>
  python amazon_ads_mutate.py add-negatives --plan negatives.csv             # dry run
  python amazon_ads_mutate.py add-negatives --plan negatives.csv --execute --plan-id <id>
  python amazon_ads_mutate.py add-negatives --revert reports/amazon_ads_undo/amazon_negatives_undo_<ts>.csv
  python amazon_ads_mutate.py check --applied reports/amazon_ads_undo/amazon_bids_undo_<ts>.csv

PLAN CSV (set-bids), one row per keyword / targeting clause:
  kind         keyword | target
  id           keywordId / targetId
  campaign_id  needed to read the live bid back (the list API filters by campaign)
  before       the bid the plan was built against   (alias: bid_before)
  after        the bid to set                        (alias: bid_after)
  any other columns (campaign, target, spend, ...) are carried into the undo file;
  `spend` orders the preview, `campaign` / `target` label it.
  A row whose LIVE bid no longer equals `before` is skipped as drift (someone changed
  it since the plan was built) unless --force-drift. Raises (after > before) are
  refused unless --allow-raises. Bids below the marketplace minimum (AMAZON_ADS_MIN_BID,
  default 0.02, the US Sponsored Products floor) are a plan error.

PLAN CSV (add-negatives): campaign_id, ad_group_id, keyword_text, optional match_type
  (NEGATIVE_EXACT default | NEGATIVE_PHRASE). Created ad-group-level, ENABLED. Never created:
  a negative that already exists in that ad group or campaign, and a negative whose text
  equals a POSITIVE keyword in the ad group (it would silently switch your own keyword off).
  The undo CSV carries each negativeKeywordId; --revert deletes (archives) those ids and
  writes its own undo file, which is itself a valid add-negatives plan.

CHECK (read-only): `check --applied undo1.csv undo2.csv` compares the bids those files set
  with live and buckets them: unchanged / RAISED after / lowered further / now inherits /
  state change / gone. It cannot write (no --execute exists). Use it to see whether
  someone, or an automated bidding rule, has overridden a change since.

TRAPS
  * A target with no bid of its own inherits the ad group default. The plan sets an
    explicit bid on it (the default stays, so siblings are untouched); a revert restores
    the explicit old VALUE, not the inheritance.
  * Creating a negative is not idempotent, so a 5xx on create is NOT retried: the request
    may have committed. Re-run the dry run instead; it skips what already exists.
  * List calls paginate until `nextToken` is absent, never on a short page.

Credentials: the same AMAZON_ADS_* variables as the read connectors (docs/amazon-ads.md).
The LWA app needs no extra scope to write; the advertising login must have edit rights on
the profile, or every write returns 403.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UNDO_DIR = os.path.join(ROOT, "reports", "amazon_ads_undo")

# Sponsored Products keyword/target minimum. 0.02 is the US floor; other marketplaces
# differ (and use their own currency), so set AMAZON_ADS_MIN_BID for yours.
MIN_BID = float(os.environ.get("AMAZON_ADS_MIN_BID", "0.02"))
CHUNK = 500
BID_TOL = 0.005
BID_KINDS = ("keyword", "target")
REQUIRED_ENV = ("AMAZON_ADS_CLIENT_ID", "AMAZON_ADS_CLIENT_SECRET",
                "AMAZON_ADS_REFRESH_TOKEN", "AMAZON_ADS_PROFILE_ID")

# kind -> (content type, path, response key, id key)
MT = {
    "keyword": ("application/vnd.spKeyword.v3+json", "/sp/keywords", "keywords", "keywordId"),
    "target": ("application/vnd.spTargetingClause.v3+json", "/sp/targets", "targetingClauses", "targetId"),
    "adgroup": ("application/vnd.spAdGroup.v3+json", "/sp/adGroups", "adGroups", "adGroupId"),
}


class PlanError(Exception):
    """The plan file is not usable. Nothing has been sent."""


class WriteBlocked(RuntimeError):
    """A write was attempted on a client that has not been enabled with --execute."""


# --------------------------------------------------------------------------- client

class AdsClient:
    """Thin Amazon Ads v3 client. `allow_writes` is False until a command is run with
    --execute; anything that is not a `/list` read raises WriteBlocked before it touches
    the network. Tests subclass and override `_transport`."""

    def __init__(self, allow_writes=False, sleep=time.sleep, clock=time.time):
        self.allow_writes = allow_writes
        self._sleep = sleep
        self._clock = clock
        self._tok = None
        self._tok_at = 0.0
        self._host = None

    # -- real network ------------------------------------------------------------
    def _token(self):
        if self._tok is None or self._clock() - self._tok_at > 2400:
            from warehouse.connectors.amazon_ads import _access_token   # lazy: no creds at import
            self._tok, self._tok_at = _access_token(), self._clock()
        return self._tok

    def _invalidate_token(self):
        self._tok = None

    def _transport(self, method, kind, path, body):
        import requests
        from warehouse.connectors.amazon_ads import REGION_HOSTS
        host = self._host or REGION_HOSTS[os.environ.get("AMAZON_ADS_REGION", "NA")]
        ct = MT[kind][0]
        headers = {"Authorization": f"Bearer {self._token()}",
                   "Amazon-Advertising-API-ClientId": os.environ["AMAZON_ADS_CLIENT_ID"],
                   "Amazon-Advertising-API-Scope": os.environ["AMAZON_ADS_PROFILE_ID"],
                   "Content-Type": ct, "Accept": ct}
        r = requests.request(method, host + path, headers=headers, data=json.dumps(body), timeout=120)
        try:
            payload = r.json()
        except ValueError:
            payload = r.text
        return r.status_code, payload

    # -- retry / guard layer (the part under test) ---------------------------------
    def call(self, method, kind, path, body, retry_5xx=True):
        if not path.endswith("/list") and not self.allow_writes:
            raise WriteBlocked(f"{method} {path} refused: writes are disabled (dry run). Pass --execute.")
        for attempt in range(6):
            status, payload = self._transport(method, kind, path, body)
            if status == 429 or (retry_5xx and status in (500, 502, 503, 504)):
                self._sleep(min(60, 2 ** attempt * 2))
                continue
            if status == 401:
                self._invalidate_token()
                continue
            if status >= 400:
                raise RuntimeError(f"{method} {path} -> HTTP {status}: {str(payload)[:800]}")
            return payload
        raise RuntimeError(f"{method} {path} kept failing (throttle/5xx/401)")


def list_all(client, kind, campaign_ids):
    """Every ENABLED/PAUSED object of `kind` in the campaigns. Pagination stops when the
    token is absent -- never on a short page (CLAUDE.md rule 8)."""
    _, path, key, _ = MT[kind]
    out = []
    ids = sorted(str(c) for c in campaign_ids)
    for i in range(0, len(ids), 100):
        body = {"campaignIdFilter": {"include": ids[i:i + 100]}, "maxResults": 1000,
                "stateFilter": {"include": ["ENABLED", "PAUSED"]}}
        while True:
            j = client.call("POST", kind, path + "/list", body)
            out += j.get(key, [])
            if not j.get("nextToken"):
                break
            body["nextToken"] = j["nextToken"]
    return out


def default_client():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit("amazon_ads_mutate: missing " + ", ".join(missing)
                         + " (see docs/amazon-ads.md)")
    return AdsClient()


# --------------------------------------------------------------------------- helpers

def norm(s):
    return " ".join((s or "").lower().replace('"', "").split())


def plan_id(items):
    """Stable short id of (kind, id, value) triples: the first 10 hex chars of
    sha1(json([(kind, str(id), f"{value:.2f}"), ...]))."""
    return hashlib.sha1(json.dumps([(k, str(i), f"{float(b):.2f}") for k, i, b in items]).encode()).hexdigest()[:10]


def _stamp(ts=None):
    return ts or dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_csv(path, rows, first=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fields = list(first or [])
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())


def _money(x):
    return f"{float(x):.2f}"


# --------------------------------------------------------------------------- set-bids

def load_bid_plan(path):
    """Read and validate a bid plan. Raises PlanError listing EVERY bad row."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        raw = list(csv.DictReader(f))
    if not raw:
        raise PlanError(f"{path}: no rows")
    problems, rows, seen = [], [], set()
    for n, r in enumerate(raw, start=2):          # line 1 is the header
        kind = (r.get("kind") or "").strip().lower()
        rid = (r.get("id") or "").strip()
        cid = (r.get("campaign_id") or "").strip()
        b = r.get("before") if r.get("before") not in (None, "") else r.get("bid_before")
        a = r.get("after") if r.get("after") not in (None, "") else r.get("bid_after")
        where = f"line {n}"
        if kind not in BID_KINDS:
            problems.append(f"{where}: kind {kind!r} must be one of {BID_KINDS}")
            continue
        if not rid or not cid:
            problems.append(f"{where}: id and campaign_id are required")
            continue
        try:
            b, a = float(b), float(a)
        except (TypeError, ValueError):
            problems.append(f"{where}: before/after must be numbers (got {b!r}, {a!r})")
            continue
        if a < MIN_BID:
            problems.append(f"{where}: after {a:.2f} is below the {MIN_BID:.2f} minimum "
                            "(AMAZON_ADS_MIN_BID)")
            continue
        if (kind, rid) in seen:
            problems.append(f"{where}: duplicate {kind} {rid}")
            continue
        seen.add((kind, rid))
        row = dict(r)
        row.update(kind=kind, id=rid, campaign_id=cid, before=_money(b), after=_money(a))
        rows.append(row)
    if problems:
        raise PlanError(f"{path}: {len(problems)} bad row(s):\n  " + "\n  ".join(problems[:25]))
    return rows


def swap_for_revert(rows):
    """An undo file read back as a plan: restore `before`, expect `after` live."""
    out = []
    for r in rows:
        r = dict(r)
        r["before"], r["after"] = r["after"], r["before"]
        out.append(r)
    return out


def _live_bids(client, rows):
    camps = {r["campaign_id"] for r in rows}
    ags = {str(a["adGroupId"]): a for a in list_all(client, "adgroup", camps)}
    live = {}
    for kind in BID_KINDS:
        idk = MT[kind][3]
        for o in list_all(client, kind, camps):
            eff = o.get("bid")
            if eff is None:
                eff = (ags.get(str(o.get("adGroupId"))) or {}).get("defaultBid")
            live[(kind, str(o[idk]))] = (eff, o)
    return live


def classify_bids(rows, live, allow_raises=False, force_drift=False):
    """-> (apply, skipped) where skipped is [(row, reason)]."""
    apply, skipped = [], []
    for r in rows:
        before, after = float(r["before"]), float(r["after"])
        hit = live.get((r["kind"], r["id"]))
        if hit is None or hit[0] is None or hit[1].get("state") == "ARCHIVED":
            skipped.append((r, "missing/archived live"))
        elif abs(after - before) <= BID_TOL:
            skipped.append((r, "no-op (before == after)"))
        elif after > before and not allow_raises:
            skipped.append((r, "raise refused (use --allow-raises)"))
        elif abs(hit[0] - before) > BID_TOL and not force_drift:
            skipped.append((r, f"drift: live ${hit[0]:.2f} != plan before ${before:.2f}"))
        else:
            apply.append(r)
    return apply, skipped


def _write_bids(client, rows, undo_path):
    """PUT in chunks. The pre-image of every row goes to the undo file BEFORE its request."""
    attempted, ok, errors = [], [], []
    for kind in BID_KINDS:
        _, path, key, idk = MT[kind]
        sel = [r for r in rows if r["kind"] == kind]
        for i in range(0, len(sel), CHUNK):
            chunk = sel[i:i + CHUNK]
            attempted += chunk
            _write_csv(undo_path, attempted, first=["kind", "id", "campaign_id", "before", "after"])
            body = {key: [{idk: r["id"], "bid": float(r["after"])} for r in chunk]}
            j = client.call("PUT", kind, path, body)
            res = j.get(key, j)
            for s in res.get("success", []):
                ok.append(chunk[s["index"]])
            for e in res.get("error", []):
                errors.append((chunk[e["index"]], e.get("errors")))
    return ok, errors


def _verify_bids(client, ok):
    live = _live_bids(client, ok) if ok else {}
    return [r for r in ok
            if live.get((r["kind"], r["id"])) is None
            or live[(r["kind"], r["id"])][1].get("bid") is None
            or abs(float(live[(r["kind"], r["id"])][1]["bid"]) - float(r["after"])) > BID_TOL]


def run_set_bids(client, rows, *, execute=False, plan_id_wanted=None, undo_dir=DEFAULT_UNDO_DIR,
                 allow_raises=False, force_drift=False, label="bids", ts=None, out=print):
    """Dry-run or execute a list of normalised bid rows. Returns an exit code."""
    live = _live_bids(client, rows)
    apply, skipped = classify_bids(rows, live, allow_raises, force_drift)
    pid = plan_id((r["kind"], r["id"], r["after"]) for r in apply)
    out(f"{label}: {len(rows)} plan rows -> {len(apply)} to change, {len(skipped)} skipped   PLAN {pid}")
    reasons = {}
    for _, why in skipped:
        reasons[why.split(":")[0]] = reasons.get(why.split(":")[0], 0) + 1
    for why, n in sorted(reasons.items()):
        out(f"  skipped {n:5}  {why}")
    inh = sum(1 for r in apply if str(r.get("bid_inherited_from_adgroup", "")).lower() == "true")
    if inh:
        out(f"  {inh} of these inherit the ad-group default; a revert restores an explicit value, not the inheritance")
    for r in sorted(apply, key=lambda x: -float(x.get("spend") or 0))[:15]:
        out(f"   ${float(r['before']):.2f} -> ${float(r['after']):.2f} | {r['kind']} {r['id']} | "
            f"{(r.get('target') or '')[:30]:30} | {(r.get('campaign') or '')[:50]}")
    if not execute:
        out("DRY RUN: nothing written. To apply: re-run with --execute --plan-id " + pid)
        return 0
    if not apply:
        out("nothing to apply")
        return 0
    if plan_id_wanted != pid:
        out(f"REFUSED: --plan-id {plan_id_wanted!r} does not match this plan ({pid}). Review the dry run and pass its id.")
        return 2
    client.allow_writes = True
    undo_path = os.path.join(undo_dir, f"amazon_{label}_undo_{_stamp(ts)}.csv")
    try:
        ok, errors = _write_bids(client, apply, undo_path)
    finally:
        client.allow_writes = False
    out(f"applied {len(ok)} / {len(apply)}; errors {len(errors)}  -> {undo_path} (undo file)")
    for r, e in errors[:10]:
        out(f"  ERR {r['kind']} {r['id']} {json.dumps(e)[:300]}")
    bad = _verify_bids(client, ok)
    out(f"verify: {len(ok) - len(bad)} confirmed at new bid, {len(bad)} mismatched")
    for r in bad[:10]:
        out(f"  MISMATCH {r['kind']} {r['id']} expected {r['after']}")
    return 0 if not errors and not bad else 1


def cmd_set_bids(args, client=None, out=print):
    if bool(args.plan) == bool(args.revert):
        raise SystemExit("set-bids: pass exactly one of --plan FILE or --revert UNDO_FILE")
    try:
        if args.revert:
            rows = swap_for_revert(load_bid_plan(args.revert))
            label, allow_raises = "revert", True
        else:
            rows = load_bid_plan(args.plan)
            label, allow_raises = "bids", args.allow_raises
    except PlanError as e:
        raise SystemExit(str(e))
    return run_set_bids(client or default_client(), rows, execute=args.execute, plan_id_wanted=args.plan_id,
                        undo_dir=args.undo_dir, allow_raises=allow_raises, force_drift=args.force_drift,
                        label=label, out=out)


# --------------------------------------------------------------------------- add-negatives

NEG_PATH = "/sp/negativeKeywords"
NEG_MATCH = ("NEGATIVE_EXACT", "NEGATIVE_PHRASE")
MT["neg"] = ("application/vnd.spNegativeKeyword.v3+json", NEG_PATH, "negativeKeywords", "negativeKeywordId")
MT["cneg"] = ("application/vnd.spCampaignNegativeKeyword.v3+json", "/sp/campaignNegativeKeywords",
              "campaignNegativeKeywords", "keywordId")


def load_negative_plan(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        raw = list(csv.DictReader(f))
    if not raw:
        raise PlanError(f"{path}: no rows")
    problems, rows, seen = [], [], set()
    for n, r in enumerate(raw, start=2):
        cid = (r.get("campaign_id") or "").strip()
        ag = (r.get("ad_group_id") or "").strip()
        text = (r.get("keyword_text") or "").strip()
        match = (r.get("match_type") or "NEGATIVE_EXACT").strip().upper()
        if not (cid and ag and text):
            problems.append(f"line {n}: campaign_id, ad_group_id and keyword_text are required")
        elif match not in NEG_MATCH:
            problems.append(f"line {n}: match_type {match!r} must be one of {NEG_MATCH}")
        elif (cid, ag, norm(text), match) in seen:
            problems.append(f"line {n}: duplicate negative {text!r}")
        else:
            seen.add((cid, ag, norm(text), match))
            rows.append(dict(r, campaign_id=cid, ad_group_id=ag, keyword_text=text, match_type=match))
    if problems:
        raise PlanError(f"{path}: {len(problems)} bad row(s):\n  " + "\n  ".join(problems[:25]))
    return rows


def classify_negatives(client, rows):
    """-> (create, dup, conflict). Reads live state only."""
    camps = {r["campaign_id"] for r in rows}
    have_neg = {(str(n["adGroupId"]), norm(n["keywordText"])) for n in list_all(client, "neg", camps)}
    have_cneg = {(str(n["campaignId"]), norm(n["keywordText"])) for n in list_all(client, "cneg", camps)}
    pos = {(str(k["adGroupId"]), norm(k["keywordText"])) for k in list_all(client, "keyword", camps)}
    create, dup, conflict = [], [], []
    for r in rows:
        t = norm(r["keyword_text"])
        if (r["ad_group_id"], t) in have_neg or (r["campaign_id"], t) in have_cneg:
            dup.append(r)
        elif (r["ad_group_id"], t) in pos:
            conflict.append(r)
        else:
            create.append(r)
    return create, dup, conflict


def _neg_plan_id(rows):
    return plan_id((f"neg:{r['campaign_id']}:{r['ad_group_id']}:{r['match_type']}", norm(r["keyword_text"]), 0)
                   for r in rows)


def run_add_negatives(client, rows, *, execute=False, plan_id_wanted=None, undo_dir=DEFAULT_UNDO_DIR,
                      ts=None, out=print):
    create, dup, conflict = classify_negatives(client, rows)
    pid = _neg_plan_id(create)
    out(f"negatives: {len(rows)} plan rows -> {len(create)} to create   PLAN {pid}")
    out(f"  skipped {len(dup):5}  already negated (ad group or campaign level)")
    out(f"  skipped {len(conflict):5}  would block our own positive keyword")
    for r in create[:15]:
        out(f"   + {r['match_type']} {r['keyword_text'][:40]:40} | ag {r['ad_group_id']} | {(r.get('campaign') or '')[:50]}")
    if not execute:
        out("DRY RUN: nothing written. To apply: re-run with --execute --plan-id " + pid)
        return 0
    if not create:
        out("nothing to create")
        return 0
    if plan_id_wanted != pid:
        out(f"REFUSED: --plan-id {plan_id_wanted!r} does not match this plan ({pid}). Review the dry run and pass its id.")
        return 2
    client.allow_writes = True
    undo_path = os.path.join(undo_dir, f"amazon_negatives_undo_{_stamp(ts)}.csv")
    created, errors = [], []
    try:
        for i in range(0, len(create), CHUNK):
            chunk = create[i:i + CHUNK]
            body = {"negativeKeywords": [{"campaignId": r["campaign_id"], "adGroupId": r["ad_group_id"],
                                          "keywordText": r["keyword_text"], "matchType": r["match_type"],
                                          "state": "ENABLED"} for r in chunk]}
            # a create is not idempotent: never blind-retry it on a 5xx that may have committed
            j = client.call("POST", "neg", NEG_PATH, body, retry_5xx=False)
            res = j.get("negativeKeywords", j)
            for s_ in res.get("success", []):
                created.append(dict(chunk[s_["index"]], negativeKeywordId=s_.get("negativeKeywordId")))
            for e in res.get("error", []):
                errors.append((chunk[e["index"]], e.get("errors")))
            _write_csv(undo_path, created, first=["negativeKeywordId", "campaign_id", "ad_group_id",
                                                  "keyword_text", "match_type"])
    finally:
        client.allow_writes = False
    out(f"created {len(created)} / {len(create)}; errors {len(errors)}  -> {undo_path} (undo file)")
    for r, e in errors[:10]:
        out(f"  ERR {r['keyword_text'][:40]} {json.dumps(e)[:300]}")
    live = ({str(n["negativeKeywordId"]): n for n in list_all(client, "neg", {r["campaign_id"] for r in created})}
            if created else {})
    ok = [r for r in created if live.get(str(r["negativeKeywordId"]), {}).get("state") == "ENABLED"
          and live[str(r["negativeKeywordId"])].get("matchType") == r["match_type"]]
    out(f"verify: {len(ok)} confirmed ENABLED, {len(created) - len(ok)} not confirmed")
    return 0 if not errors and len(ok) == len(created) else 1


def run_revert_negatives(client, rows, *, execute=False, plan_id_wanted=None, undo_dir=DEFAULT_UNDO_DIR,
                         ts=None, out=print):
    rows = [r for r in rows if r.get("negativeKeywordId")]
    live = ({str(n["negativeKeywordId"]): n for n in list_all(client, "neg", {r["campaign_id"] for r in rows})}
            if rows else {})
    todo = [r for r in rows if str(r["negativeKeywordId"]) in live]
    pid = plan_id((f"del:{r['campaign_id']}", r["negativeKeywordId"], 0) for r in todo)
    out(f"revert negatives: {len(rows)} in file -> {len(todo)} still live to delete, "
        f"{len(rows) - len(todo)} already gone   PLAN {pid}")
    if not execute:
        out("DRY RUN: nothing written. To apply: re-run with --execute --plan-id " + pid)
        return 0
    if not todo:
        out("nothing to delete")
        return 0
    if plan_id_wanted != pid:
        out(f"REFUSED: --plan-id {plan_id_wanted!r} does not match this plan ({pid}). Review the dry run and pass its id.")
        return 2
    undo_path = os.path.join(undo_dir, f"amazon_negatives_revert_undo_{_stamp(ts)}.csv")
    # a valid add-negatives plan that re-creates exactly what is about to be deleted
    _write_csv(undo_path, [{k: r.get(k, "") for k in ("campaign_id", "ad_group_id", "keyword_text", "match_type")}
                           for r in todo], first=["campaign_id", "ad_group_id", "keyword_text", "match_type"])
    client.allow_writes = True
    done, errors = 0, 0
    try:
        ids = [str(r["negativeKeywordId"]) for r in todo]
        for i in range(0, len(ids), CHUNK):
            j = client.call("POST", "neg", NEG_PATH + "/delete",
                            {"negativeKeywordIdFilter": {"include": ids[i:i + CHUNK]}})
            res = j.get("negativeKeywords", j)
            done += len(res.get("success", []))
            errors += len(res.get("error", []))
    finally:
        client.allow_writes = False
    still = {str(n["negativeKeywordId"]) for n in list_all(client, "neg", {r["campaign_id"] for r in todo})}
    left = [r for r in todo if str(r["negativeKeywordId"]) in still]
    out(f"deleted {done}, errors {errors}  -> {undo_path} (re-create plan)")
    out(f"verify: {len(todo) - len(left)} confirmed gone, {len(left)} still live")
    return 0 if not errors and not left else 1


def cmd_add_negatives(args, client=None, out=print):
    if bool(args.plan) == bool(args.revert):
        raise SystemExit("add-negatives: pass exactly one of --plan FILE or --revert UNDO_FILE")
    kw = dict(execute=args.execute, plan_id_wanted=args.plan_id, undo_dir=args.undo_dir, out=out)
    if args.revert:
        with open(args.revert, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows or "negativeKeywordId" not in rows[0] or "campaign_id" not in rows[0]:
            raise SystemExit(f"{args.revert}: not a negatives undo file (needs negativeKeywordId and campaign_id columns)")
        return run_revert_negatives(client or default_client(), rows, **kw)
    try:
        rows = load_negative_plan(args.plan)
    except PlanError as e:
        raise SystemExit(str(e))
    return run_add_negatives(client or default_client(), rows, **kw)


# --------------------------------------------------------------------------- check (read-only)

def check_bids(client, rows):
    """Compare what we set (`after`) with what is live now. Later files win per (kind, id).
    Returns ({bucket: count}, examples for the buckets that mean someone changed it)."""
    last = {(r["kind"], r["id"]): r for r in rows}
    live = _live_bids(client, list(last.values()))
    counts, examples = {}, []

    def bump(k):
        counts[k] = counts.get(k, 0) + 1
    for key, r in last.items():
        hit = live.get(key)
        if hit is None:
            bump("gone/archived")
            continue
        o, want = hit[1], float(r["after"])
        if r.get("state") and o.get("state") != r["state"]:
            bump(f"state {r['state']}->{o.get('state')}")
        if o.get("bid") is None:
            bump("now inherits ad-group default")
        elif abs(o["bid"] - want) <= BID_TOL:
            bump("unchanged since our change")
        elif o["bid"] > want:
            bump("RAISED after our change")
            examples.append((r, o["bid"]))
        else:
            bump("lowered further")
            examples.append((r, o["bid"]))
    return counts, examples


def cmd_check(args, client=None, out=print):
    rows = []
    for path in args.applied:
        try:
            rows += load_bid_plan(path)
        except PlanError as e:
            raise SystemExit(str(e))
    client = client or default_client()
    client.allow_writes = False                    # read-only by construction
    counts, examples = check_bids(client, rows)
    out(f"{len({(r['kind'], r['id']) for r in rows})} bids from {len(args.applied)} file(s), now: {counts}")
    for r, b in sorted(examples, key=lambda x: -float(x[0].get("spend") or 0))[:10]:
        out(f"   {(r.get('target') or r['id'])[:26]:26} ours ${float(r['after']):.2f} (was ${float(r['before']):.2f}) now ${b:.2f}"
            f" | {(r.get('campaign') or '')[:60]}")
    return 0


# --------------------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--execute", action="store_true", help="actually write (default is a dry run)")
        p.add_argument("--plan-id", help="id printed by the dry run; required with --execute")
        p.add_argument("--undo-dir", default=DEFAULT_UNDO_DIR)

    p = sub.add_parser("set-bids", help="set keyword / targeting-clause bids from a plan CSV, or revert an undo CSV")
    p.add_argument("--plan")
    p.add_argument("--revert", metavar="UNDO_CSV")
    p.add_argument("--allow-raises", action="store_true")
    p.add_argument("--force-drift", action="store_true", help="apply even where the live bid differs from `before`")
    common(p)
    p.set_defaults(fn=cmd_set_bids)

    p = sub.add_parser("add-negatives", help="create ad-group negative keywords from a plan CSV, or delete those in an undo CSV")
    p.add_argument("--plan")
    p.add_argument("--revert", metavar="UNDO_CSV")
    common(p)
    p.set_defaults(fn=cmd_add_negatives)

    p = sub.add_parser("check", help="READ-ONLY: did anyone change bids we set? (undo CSVs vs live)")
    p.add_argument("--applied", nargs="+", required=True, metavar="CSV")
    p.set_defaults(fn=cmd_check)
    return ap


def main(argv=None, client=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return args.fn(args, client=client)


if __name__ == "__main__":
    sys.exit(main())
