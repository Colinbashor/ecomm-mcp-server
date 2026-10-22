"""Hermetic tests for amazon_ads_mutate -- a stateful FAKE Amazon Ads API, no network,
no credentials, no .env. The properties pinned here are the safety ones: a dry run cannot
write, --execute needs the reviewed plan id, the undo file exists BEFORE the write, drift
and raises are refused, and a revert restores exactly what was changed."""
import csv
import os

import pytest

import amazon_ads_mutate as m


class FakeAds(m.AdsClient):
    """In-memory Sponsored Products v3. Overrides only the transport, so the real
    guard / retry / pagination code runs."""

    PAGE = 2          # tiny page size so pagination is exercised

    def __init__(self, **kw):
        super().__init__(sleep=lambda s: None, **kw)
        self.keywords, self.targets, self.adgroups = {}, {}, {}
        self.negatives = {}            # id -> dict
        self.campaign_negatives = {}
        self.calls = []                # (method, path)
        self.script = []               # queued (status, payload) overrides
        self.fail_put = False
        self.reject = set()            # ids the fake API rejects on PUT
        self._neg_seq = 1000

    # -- seeding -----------------------------------------------------------------
    def add_keyword(self, kid, bid, camp="c1", ag="ag1", text="kw", match="EXACT", state="ENABLED"):
        self.keywords[str(kid)] = {"keywordId": str(kid), "campaignId": camp, "adGroupId": ag,
                                   "keywordText": text, "matchType": match, "state": state, "bid": bid}

    def add_target(self, tid, bid, camp="c1", ag="ag1", state="ENABLED"):
        o = {"targetId": str(tid), "campaignId": camp, "adGroupId": ag, "state": state,
             "expression": [{"type": "QUERY_HIGH_REL_MATCHES"}]}
        if bid is not None:
            o["bid"] = bid
        self.targets[str(tid)] = o

    def add_adgroup(self, ag="ag1", camp="c1", default=1.00):
        self.adgroups[ag] = {"adGroupId": ag, "campaignId": camp, "defaultBid": default}

    # -- the transport -------------------------------------------------------------
    def _transport(self, method, kind, path, body):
        self.calls.append((method, path))
        if self.script:
            return self.script.pop(0)
        if path.endswith("/list"):
            return 200, self._list(path[:-5], body)
        if method == "PUT" and path in ("/sp/keywords", "/sp/targets"):
            if self.fail_put:
                raise RuntimeError("boom")
            return 200, self._put(path, body)
        if path.startswith("/sp/negativeKeywords"):
            return 200, self._neg(method, path, body)
        raise AssertionError(f"unexpected {method} {path}")

    def _store(self, path):
        return {"/sp/keywords": ("keywords", self.keywords), "/sp/targets": ("targetingClauses", self.targets),
                "/sp/adGroups": ("adGroups", self.adgroups),
                "/sp/negativeKeywords": ("negativeKeywords", self.negatives),
                "/sp/campaignNegativeKeywords": ("campaignNegativeKeywords", self.campaign_negatives)}[path]

    def _list(self, path, body):
        key, store = self._store(path)
        camps = set(body["campaignIdFilter"]["include"])
        objs = [o for o in store.values() if o["campaignId"] in camps and o.get("state", "ENABLED") in ("ENABLED", "PAUSED")]
        start = int(body.get("nextToken", 0))
        page = objs[start:start + self.PAGE]
        out = {key: page}
        if start + self.PAGE < len(objs):
            out["nextToken"] = str(start + self.PAGE)
        return out

    def _put(self, path, body):
        key, store = self._store(path)
        idk = m.MT["keyword" if path == "/sp/keywords" else "target"][3]
        res = {"success": [], "error": []}
        for i, item in enumerate(body[key]):
            if item[idk] in store and item[idk] not in self.reject:
                store[item[idk]]["bid"] = item["bid"]
                res["success"].append({"index": i, idk: item[idk]})
            else:
                res["error"].append({"index": i, "errors": [{"errorType": "NOT_FOUND"}]})
        return {key: res}

    def _neg(self, method, path, body):
        res = {"success": [], "error": []}
        if path.endswith("/delete"):
            for i in body["negativeKeywordIdFilter"]["include"]:
                if i in self.negatives:
                    self.negatives[i]["state"] = "ARCHIVED"
                    res["success"].append({"index": 0, "negativeKeywordId": i})
                else:
                    res["error"].append({"index": 0, "errors": [{"errorType": "NOT_FOUND"}]})
            return {"negativeKeywords": res}
        for i, item in enumerate(body["negativeKeywords"]):
            self._neg_seq += 1
            nid = str(self._neg_seq)
            self.negatives[nid] = dict(item, negativeKeywordId=nid)
            res["success"].append({"index": i, "negativeKeywordId": nid})
        return {"negativeKeywords": res}

    @property
    def writes(self):
        return [c for c in self.calls if not c[1].endswith("/list")]


def write_plan(path, rows, fields=("kind", "id", "campaign_id", "before", "after")):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return str(path)


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def api():
    a = FakeAds()
    a.add_adgroup("ag1", "c1", default=1.00)
    a.add_keyword(1, 2.00, text="alpha")
    a.add_keyword(2, 1.00, text="beta")
    a.add_keyword(3, 0.80, text="gamma")
    a.add_target(10, None)                       # inherits the ad group default $1.00
    return a


def bid_rows():
    return [dict(kind="keyword", id="1", campaign_id="c1", before="2.00", after="0.50"),
            dict(kind="keyword", id="2", campaign_id="c1", before="1.00", after="0.25"),
            dict(kind="target", id="10", campaign_id="c1", before="1.00", after="0.10")]


def run(api, rows, tmp_path, **kw):
    lines = []
    kw.setdefault("undo_dir", str(tmp_path / "undo"))
    code = m.run_set_bids(api, [dict(r) for r in rows], out=lines.append, ts="B1", **kw)
    return code, "\n".join(lines)


# ----------------------------------------------------------------------------- client guard

def test_client_refuses_writes_unless_enabled():
    a = FakeAds()
    with pytest.raises(m.WriteBlocked):
        a.call("PUT", "keyword", "/sp/keywords", {"keywords": []})
    assert a.calls == []                          # never reached the transport
    a.allow_writes = True
    a.add_keyword(1, 1.0)
    a.call("PUT", "keyword", "/sp/keywords", {"keywords": [{"keywordId": "1", "bid": 0.5}]})
    assert a.keywords["1"]["bid"] == 0.5


def test_list_is_always_allowed_and_paginates_past_short_page(api):
    got = m.list_all(api, "keyword", ["c1"])      # 3 keywords, page size 2 -> needs the token
    assert sorted(k["keywordId"] for k in got) == ["1", "2", "3"]


def test_retries_throttle_then_succeeds():
    a = FakeAds()
    a.script = [(429, "slow down"), (503, "x"), (200, {"keywords": []})]
    assert a.call("POST", "keyword", "/sp/keywords/list", {}) == {"keywords": []}


def test_non_retryable_5xx_raises_immediately_when_disabled():
    a = FakeAds()
    a.script = [(503, "x")]
    with pytest.raises(RuntimeError):
        a.call("POST", "keyword", "/sp/keywords/list", {}, retry_5xx=False)
    assert len(a.calls) == 1


def test_401_refreshes_token_and_retries():
    a = FakeAds()
    a.script = [(401, "expired"), (200, {"keywords": []})]
    a.call("POST", "keyword", "/sp/keywords/list", {})
    assert len(a.calls) == 2


def test_http_error_raises():
    a = FakeAds()
    a.script = [(400, "bad")]
    with pytest.raises(RuntimeError, match="HTTP 400"):
        a.call("POST", "keyword", "/sp/keywords/list", {})


# ----------------------------------------------------------------------------- plan loading

def test_plan_accepts_bid_before_after_alias_columns(tmp_path):
    p = write_plan(tmp_path / "p.csv", [dict(kind="keyword", id="1", campaign_id="c1", bid_before="2", bid_after="0.5")],
                   fields=("kind", "id", "campaign_id", "bid_before", "bid_after"))
    row = m.load_bid_plan(p)[0]
    assert (row["before"], row["after"]) == ("2.00", "0.50")


def test_plan_reports_every_bad_row(tmp_path):
    p = write_plan(tmp_path / "p.csv", [
        dict(kind="keyword", id="1", campaign_id="c1", before="2", after="0.01"),   # below minimum
        dict(kind="adgroup", id="2", campaign_id="c1", before="2", after="1"),       # bad kind
        dict(kind="keyword", id="3", campaign_id="c1", before="x", after="1"),       # not a number
        dict(kind="keyword", id="4", campaign_id="", before="2", after="1"),         # no campaign
    ])
    with pytest.raises(m.PlanError) as e:
        m.load_bid_plan(p)
    msg = str(e.value)
    assert "4 bad row" in msg and "minimum" in msg and "kind" in msg and "numbers" in msg and "campaign_id" in msg


def test_plan_rejects_duplicate_rows(tmp_path):
    r = dict(kind="keyword", id="1", campaign_id="c1", before="2", after="1")
    with pytest.raises(m.PlanError, match="duplicate"):
        m.load_bid_plan(write_plan(tmp_path / "p.csv", [r, r]))


def test_plan_id_is_the_documented_formula():
    # sha1(json([(kind, str(id), f'{after:.2f}')])).hexdigest()[:10]
    import hashlib, json
    want = hashlib.sha1(json.dumps([("keyword", "1", "0.50")]).encode()).hexdigest()[:10]
    assert m.plan_id([("keyword", 1, 0.5)]) == want


# ----------------------------------------------------------------------------- dry run

def test_dry_run_writes_nothing_and_prints_plan_id(api, tmp_path):
    code, text = run(api, bid_rows(), tmp_path)
    assert code == 0
    assert api.writes == []                                       # only /list calls
    assert api.keywords["1"]["bid"] == 2.00
    assert not os.path.exists(tmp_path / "undo")                  # no undo file for a dry run
    assert "DRY RUN" in text and "--plan-id" in text


def test_execute_without_plan_id_is_refused(api, tmp_path):
    code, text = run(api, bid_rows(), tmp_path, execute=True)
    assert code == 2 and "REFUSED" in text
    assert api.writes == [] and api.keywords["1"]["bid"] == 2.00


def test_execute_with_wrong_plan_id_is_refused(api, tmp_path):
    code, _ = run(api, bid_rows(), tmp_path, execute=True, plan_id_wanted="deadbeef00")
    assert code == 2 and api.writes == []


# ----------------------------------------------------------------------------- execute

def reviewed_id(api, tmp_path, rows=None, **kw):
    _, text = run(api, rows or bid_rows(), tmp_path, **kw)
    return text.split("PLAN ")[1].split()[0]


def test_execute_applies_writes_undo_and_verifies(api, tmp_path):
    pid = reviewed_id(api, tmp_path)
    code, text = run(api, bid_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    assert code == 0, text
    assert api.keywords["1"]["bid"] == 0.50 and api.keywords["2"]["bid"] == 0.25
    assert api.targets["10"]["bid"] == 0.10                      # explicit bid set on the inheriting target
    assert api.adgroups["ag1"]["defaultBid"] == 1.00             # ad-group default untouched
    assert api.keywords["3"]["bid"] == 0.80                      # unrelated keyword untouched
    undo = read_csv(tmp_path / "undo" / "amazon_bids_undo_B1.csv")
    assert {(r["id"], r["before"], r["after"]) for r in undo} == {("1", "2.00", "0.50"), ("2", "1.00", "0.25"), ("10", "1.00", "0.10")}
    assert "3 confirmed" in text
    assert api.allow_writes is False                             # re-locked after the write


def test_undo_file_is_on_disk_before_the_write_is_sent(api, tmp_path):
    pid = reviewed_id(api, tmp_path)
    api.fail_put = True                                          # the PUT blows up mid-flight
    with pytest.raises(RuntimeError):
        run(api, bid_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    undo = read_csv(tmp_path / "undo" / "amazon_bids_undo_B1.csv")
    assert {r["id"] for r in undo} == {"1", "2"}                 # pre-image of the chunk that failed
    assert api.allow_writes is False                             # and the lock is restored


def test_partial_api_errors_are_reported_and_exit_nonzero(api, tmp_path):
    pid = reviewed_id(api, tmp_path)
    api.reject.add("2")                                          # Amazon rejects one item in the batch
    code, text = run(api, bid_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    assert code == 1 and "errors 1" in text and "ERR keyword 2" in text
    assert api.keywords["1"]["bid"] == 0.50 and api.keywords["2"]["bid"] == 1.00


def test_drift_is_skipped_not_overwritten(api, tmp_path):
    api.keywords["1"]["bid"] = 1.40                              # someone changed it after the plan
    code, text = run(api, bid_rows(), tmp_path)
    assert "drift" in text and "2 to change" in text
    pid = text.split("PLAN ")[1].split()[0]
    run(api, bid_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    assert api.keywords["1"]["bid"] == 1.40                      # left alone


def test_force_drift_applies_anyway(api, tmp_path):
    api.keywords["1"]["bid"] = 1.40
    _, text = run(api, bid_rows(), tmp_path, force_drift=True)
    assert "3 to change" in text


def test_raises_refused_unless_allowed(api, tmp_path):
    rows = [dict(kind="keyword", id="3", campaign_id="c1", before="0.80", after="1.20")]
    _, text = run(api, rows, tmp_path)
    assert "raise refused" in text and "0 to change" in text
    _, text = run(api, rows, tmp_path, allow_raises=True)
    assert "1 to change" in text


def test_noop_and_missing_rows_are_skipped(api, tmp_path):
    rows = [dict(kind="keyword", id="3", campaign_id="c1", before="0.80", after="0.80"),
            dict(kind="keyword", id="404", campaign_id="c1", before="1.00", after="0.50")]
    _, text = run(api, rows, tmp_path)
    assert "0 to change, 2 skipped" in text


def test_archived_live_object_is_skipped(api, tmp_path):
    api.keywords["1"]["state"] = "ARCHIVED"                      # list_all hides it -> missing
    _, text = run(api, bid_rows(), tmp_path)
    assert "2 to change" in text


# ----------------------------------------------------------------------------- revert

def test_revert_restores_exactly_what_was_changed(api, tmp_path):
    pid = reviewed_id(api, tmp_path)
    run(api, bid_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    undo = str(tmp_path / "undo" / "amazon_bids_undo_B1.csv")
    rows = m.swap_for_revert(m.load_bid_plan(undo))

    _, text = run(api, rows, tmp_path, label="revert", allow_raises=True)           # dry run first
    assert api.keywords["1"]["bid"] == 0.50 and "DRY RUN" in text
    rid = text.split("PLAN ")[1].split()[0]
    code, text = run(api, rows, tmp_path, label="revert", allow_raises=True, execute=True, plan_id_wanted=rid)
    assert code == 0, text
    assert (api.keywords["1"]["bid"], api.keywords["2"]["bid"], api.targets["10"]["bid"]) == (2.00, 1.00, 1.00)
    assert os.path.exists(tmp_path / "undo" / "amazon_revert_undo_B1.csv")           # the revert is undoable too


def test_revert_skips_bids_changed_since(api, tmp_path):
    pid = reviewed_id(api, tmp_path)
    run(api, bid_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    api.keywords["1"]["bid"] = 0.75                                                  # a human touched it
    rows = m.swap_for_revert(m.load_bid_plan(str(tmp_path / "undo" / "amazon_bids_undo_B1.csv")))
    _, text = run(api, rows, tmp_path, label="revert", allow_raises=True)
    assert "drift" in text and "2 to change" in text


def test_revert_reads_a_file_with_alias_columns_and_extras(api, tmp_path):
    # a hand-kept applied log may carry bid_before/bid_after plus extras, no `before`/`after`
    p = write_plan(tmp_path / "applied.csv",
                   [dict(kind="keyword", id="3", campaign_id="c1", bid_before="1.60", bid_after="0.80", target="gamma")],
                   fields=("kind", "id", "campaign_id", "bid_before", "bid_after", "target"))
    rows = m.swap_for_revert(m.load_bid_plan(p))
    assert (rows[0]["before"], rows[0]["after"]) == ("0.80", "1.60")


# ----------------------------------------------------------------------------- add-negatives

def neg_rows():
    return [dict(campaign_id="c1", ad_group_id="ag1", keyword_text="free shipping", match_type="NEGATIVE_EXACT"),
            dict(campaign_id="c1", ad_group_id="ag1", keyword_text="Cheap  Knockoff", match_type="NEGATIVE_EXACT"),
            dict(campaign_id="c1", ad_group_id="ag1", keyword_text="alpha", match_type="NEGATIVE_EXACT"),     # = positive
            dict(campaign_id="c1", ad_group_id="ag1", keyword_text="already", match_type="NEGATIVE_EXACT"),   # exists
            dict(campaign_id="c1", ad_group_id="ag1", keyword_text="campwide", match_type="NEGATIVE_EXACT")]  # campaign-level


def run_neg(api, rows, tmp_path, fn=m.run_add_negatives, **kw):
    lines = []
    kw.setdefault("undo_dir", str(tmp_path / "undo"))
    code = fn(api, [dict(r) for r in rows], out=lines.append, ts="N2", **kw)
    return code, "\n".join(lines)


@pytest.fixture
def napi(api):
    api.negatives["1"] = {"negativeKeywordId": "1", "campaignId": "c1", "adGroupId": "ag1", "keywordText": "Already",
                          "matchType": "NEGATIVE_EXACT", "state": "ENABLED"}
    api.campaign_negatives["9"] = {"keywordId": "9", "campaignId": "c1", "keywordText": "campwide", "state": "ENABLED"}
    return api


def test_negatives_dry_run_filters_and_writes_nothing(napi, tmp_path):
    code, text = run_neg(napi, neg_rows(), tmp_path)
    assert code == 0 and napi.writes == []
    assert "2 to create" in text and "skipped     2  already negated" in text and "skipped     1  would block" in text
    assert not os.path.exists(tmp_path / "undo")


def test_negatives_execute_needs_reviewed_plan_id(napi, tmp_path):
    code, text = run_neg(napi, neg_rows(), tmp_path, execute=True)
    assert code == 2 and napi.writes == [] and len(napi.negatives) == 1
    code, _ = run_neg(napi, neg_rows(), tmp_path, execute=True, plan_id_wanted="0000000000")
    assert code == 2 and napi.writes == []


def test_negatives_execute_creates_saves_undo_with_ids_and_verifies(napi, tmp_path):
    pid = run_neg(napi, neg_rows(), tmp_path)[1].split("PLAN ")[1].split()[0]
    code, text = run_neg(napi, neg_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    assert code == 0, text
    made = [n for n in napi.negatives.values() if n["negativeKeywordId"] != "1"]
    assert sorted(n["keywordText"] for n in made) == ["Cheap  Knockoff", "free shipping"]
    assert all(n["state"] == "ENABLED" and n["matchType"] == "NEGATIVE_EXACT" for n in made)
    undo = read_csv(tmp_path / "undo" / "amazon_negatives_undo_N2.csv")
    assert {r["negativeKeywordId"] for r in undo} == {n["negativeKeywordId"] for n in made}
    assert "2 confirmed ENABLED" in text and napi.allow_writes is False


def test_negatives_create_is_not_retried_on_5xx(napi, tmp_path):
    pid = run_neg(napi, neg_rows(), tmp_path)[1].split("PLAN ")[1].split()[0]
    real = napi._transport
    seen = []

    def flaky(method, kind, path, body):
        if method == "POST" and path == "/sp/negativeKeywords":
            seen.append(1)
            return 503, "maybe committed"
        return real(method, kind, path, body)
    napi._transport = flaky
    with pytest.raises(RuntimeError, match="503"):
        run_neg(napi, neg_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    assert len(seen) == 1                                   # exactly one attempt, no duplicate-create risk
    assert napi.allow_writes is False


def test_negatives_revert_deletes_exactly_the_created_ids_and_leaves_an_undo(napi, tmp_path):
    pid = run_neg(napi, neg_rows(), tmp_path)[1].split("PLAN ")[1].split()[0]
    run_neg(napi, neg_rows(), tmp_path, execute=True, plan_id_wanted=pid)
    undo = read_csv(tmp_path / "undo" / "amazon_negatives_undo_N2.csv")

    _, text = run_neg(napi, undo, tmp_path, fn=m.run_revert_negatives)
    assert "2 still live" in text and "DRY RUN" in text
    assert sum(n["state"] == "ENABLED" for n in napi.negatives.values()) == 3
    rid = text.split("PLAN ")[1].split()[0]
    code, text = run_neg(napi, undo, tmp_path, fn=m.run_revert_negatives, execute=True, plan_id_wanted=rid)
    assert code == 0, text
    assert napi.negatives["1"]["state"] == "ENABLED"          # the pre-existing negative is untouched
    assert sum(n["state"] == "ENABLED" for n in napi.negatives.values()) == 1
    again = m.load_negative_plan(str(tmp_path / "undo" / "amazon_negatives_revert_undo_N2.csv"))
    assert sorted(r["keyword_text"] for r in again) == ["Cheap  Knockoff", "free shipping"]     # valid re-create plan


def test_negatives_revert_dry_run_writes_nothing(napi, tmp_path):
    rows = [dict(campaign_id="c1", negativeKeywordId="1")]
    code, text = run_neg(napi, rows, tmp_path, fn=m.run_revert_negatives)
    assert code == 0 and napi.writes == [] and "1 still live" in text


def test_negative_plan_validation(tmp_path):
    p = write_plan(tmp_path / "n.csv", [dict(campaign_id="c1", ad_group_id="", keyword_text="x"),
                                        dict(campaign_id="c1", ad_group_id="a", keyword_text="y", match_type="BROAD")],
                   fields=("campaign_id", "ad_group_id", "keyword_text", "match_type"))
    with pytest.raises(m.PlanError) as e:
        m.load_negative_plan(p)
    assert "2 bad row" in str(e.value)
    ok = write_plan(tmp_path / "ok.csv", [dict(campaign_id="c1", ad_group_id="a", keyword_text="y")],
                    fields=("campaign_id", "ad_group_id", "keyword_text"))
    assert m.load_negative_plan(ok)[0]["match_type"] == "NEGATIVE_EXACT"


def test_cli_add_negatives_dry_run(napi, tmp_path, capsys):
    p = write_plan(tmp_path / "n.csv", neg_rows(), fields=("campaign_id", "ad_group_id", "keyword_text", "match_type"))
    assert m.main(["add-negatives", "--plan", p, "--undo-dir", str(tmp_path / "u")], client=napi) == 0
    assert "DRY RUN" in capsys.readouterr().out and napi.writes == []
    bad = write_plan(tmp_path / "b.csv", [dict(a="1")], fields=("a",))
    with pytest.raises(SystemExit, match="not a negatives undo file"):
        m.main(["add-negatives", "--revert", bad], client=napi)


# ----------------------------------------------------------------------------- check

def applied_file(tmp_path):
    return write_plan(tmp_path / "applied.csv", [
        dict(kind="keyword", id="1", campaign_id="c1", before="2.00", after="0.50", state="ENABLED"),
        dict(kind="keyword", id="2", campaign_id="c1", before="1.00", after="0.25", state="ENABLED"),
        dict(kind="keyword", id="3", campaign_id="c1", before="0.80", after="0.40", state="ENABLED"),
        dict(kind="target", id="10", campaign_id="c1", before="1.00", after="0.10", state="ENABLED"),
        dict(kind="keyword", id="77", campaign_id="c1", before="1.00", after="0.30", state="ENABLED"),
    ], fields=("kind", "id", "campaign_id", "before", "after", "state"))


def test_check_buckets_live_drift_and_never_writes(api, tmp_path, capsys):
    api.keywords["1"]["bid"] = 0.50          # unchanged
    api.keywords["2"]["bid"] = 0.90          # raised after our change
    api.keywords["3"]["bid"] = 0.20          # lowered further
    api.targets["10"].pop("bid", None)       # back to inheriting
    rc = m.main(["check", "--applied", applied_file(tmp_path)], client=api)
    out = capsys.readouterr().out
    assert rc == 0 and api.writes == []
    for want in ("unchanged since our change': 1", "RAISED after our change': 1", "lowered further': 1",
                 "now inherits ad-group default': 1", "gone/archived': 1"):
        assert want in out, want


def test_check_later_file_wins_per_bid(api, tmp_path):
    a = write_plan(tmp_path / "a.csv", [dict(kind="keyword", id="1", campaign_id="c1", before="2.00", after="1.00")])
    b = write_plan(tmp_path / "b.csv", [dict(kind="keyword", id="1", campaign_id="c1", before="1.00", after="0.50")])
    api.keywords["1"]["bid"] = 0.50
    counts, _ = m.check_bids(api, m.load_bid_plan(a) + m.load_bid_plan(b))
    assert counts == {"unchanged since our change": 1}


def test_check_has_no_execute_flag():
    with pytest.raises(SystemExit):
        m.build_parser().parse_args(["check", "--applied", "x.csv", "--execute"])


# ----------------------------------------------------------------------------- CLI

def test_cli_set_bids_dry_run_and_argument_checks(api, tmp_path, capsys):
    p = write_plan(tmp_path / "p.csv", bid_rows())
    assert m.main(["set-bids", "--plan", p, "--undo-dir", str(tmp_path / "u")], client=api) == 0
    assert "DRY RUN" in capsys.readouterr().out and api.writes == []
    with pytest.raises(SystemExit):                      # neither --plan nor --revert
        m.main(["set-bids"], client=api)
    with pytest.raises(SystemExit):                      # both
        m.main(["set-bids", "--plan", p, "--revert", p], client=api)


def test_cli_execute_defaults_off():
    ns = m.build_parser().parse_args(["set-bids", "--plan", "x.csv"])
    assert ns.execute is False and ns.plan_id is None


def test_client_construction_needs_no_credentials(monkeypatch):
    for k in ("AMAZON_ADS_CLIENT_ID", "AMAZON_ADS_PROFILE_ID", "AMAZON_ADS_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert m.AdsClient().allow_writes is False


def test_min_bid_is_configurable_per_marketplace(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("AMAZON_ADS_MIN_BID", "0.10")
    mod = importlib.reload(m)
    try:
        p = write_plan(tmp_path / "p.csv", [dict(kind="keyword", id="1", campaign_id="c1",
                                                  before="2", after="0.05")])
        with pytest.raises(mod.PlanError, match="AMAZON_ADS_MIN_BID"):
            mod.load_bid_plan(p)
    finally:
        monkeypatch.delenv("AMAZON_ADS_MIN_BID")
        importlib.reload(m)


def test_default_client_names_missing_credentials(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    for k in m.REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="AMAZON_ADS_PROFILE_ID"):
        m.default_client()
