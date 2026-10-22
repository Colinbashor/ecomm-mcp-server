"""tiktok_promotions: a stateful fake Promotion API, no network, no .env.

Pinned: a dry run never writes, the add body carries the shapes the API
actually accepts, batches respect the 20-product cap, the rate-limit code backs
off, an expired token refreshes once, and the result is verified by re-reading.
"""
from __future__ import annotations

import argparse
import csv

import pytest

import tiktok_promotions as tp

ENV = {"TIKTOK_APP_KEY": "app-key", "TIKTOK_APP_SECRET": "app-secret",
       "TIKTOK_ACCESS_TOKEN": "access", "TIKTOK_SHOP_CIPHER": "cipher"}
ACT = "1000000000001"


class FakePromotions:
    def __init__(self):
        self.products: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.script: list[tuple[int, dict]] = []

    def __call__(self, method, url, params, raw, headers):
        import json
        path = url.split(".com", 1)[1]
        body = json.loads(raw) if raw else {}
        self.calls.append((method, path, body))
        assert params["sign"] and params["shop_cipher"] == "cipher"
        if self.script:
            return self.script.pop(0)
        if method == "GET" and path == f"{tp.BASE_PATH}/{ACT}":
            return 200, {"code": 0, "data": {"status": "ONGOING", "title": "Test sale",
                         "products": [{"id": p, "activity_price": {"amount": a, "currency": "USD"}}
                                      for p, a in self.products.items()]}}
        if method == "PUT" and path == f"{tp.BASE_PATH}/{ACT}/products":
            assert body["activity_id"] == ACT, "activity_id must be in the body too"
            assert len(body["products"]) <= tp.BATCH
            for p in body["products"]:
                assert set(p) == {"id", "activity_price_amount", "quantity_limit", "quantity_per_user"}
                assert isinstance(p["activity_price_amount"], str)
                self.products[p["id"]] = p["activity_price_amount"]
            return 200, {"code": 0, "message": "Success"}
        if method == "POST" and path == tp.BASE_PATH:
            return 200, {"code": 0, "message": "Success", "data": {"activity_id": ACT}}
        raise AssertionError(f"unexpected {method} {path}")

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET" and not c[1].endswith("/search")]


@pytest.fixture
def api(monkeypatch, tmp_path):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    fake = FakePromotions()
    monkeypatch.setattr(tp, "_send", fake)
    monkeypatch.setattr(tp, "LOG", tmp_path / "log.csv")
    monkeypatch.setattr(tp.time, "sleep", lambda s: None)
    return fake


def prices_file(tmp_path, rows):
    p = tmp_path / "prices.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "price"])
        w.writerows(rows)
    return str(p)


def add_args(path, apply=False):
    return argparse.Namespace(activity_id=ACT, prices_file=path, apply=apply, pause=0)


def test_add_dry_run_reads_but_never_writes(api, tmp_path, capsys):
    api.products = {"1000000000101": "20"}
    rc = tp.cmd_add(add_args(prices_file(tmp_path, [("1000000000101", "15"), ("1000000000102", "$9.50")])))
    assert rc == 0 and api.writes == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "2 new or repriced" in out
    assert api.products == {"1000000000101": "20"}


def test_add_apply_upserts_in_batches_and_verifies(api, tmp_path, capsys):
    rows = [(str(1000000000200 + i), "12") for i in range(45)]
    rc = tp.cmd_add(add_args(prices_file(tmp_path, rows), apply=True))
    assert rc == 0
    assert [len(c[2]["products"]) for c in api.writes] == [20, 20, 5]
    assert "VERIFIED" in capsys.readouterr().out
    assert len(api.products) == 45
    with (tmp_path / "log.csv").open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 45


def test_add_skips_products_already_at_the_price(api, tmp_path):
    api.products = {"1000000000101": "15"}
    tp.cmd_add(add_args(prices_file(tmp_path, [("1000000000101", "15.00"),
                                               ("1000000000102", "8")]), apply=True))
    assert [p["id"] for c in api.writes for p in c[2]["products"]] == ["1000000000102"]


def test_add_reports_a_mismatch_after_the_write(api, tmp_path, capsys, monkeypatch):
    real = api.__call__

    def lossy(method, url, params, raw, headers):
        status, data = real(method, url, params, raw, headers)
        if method == "PUT":
            api.products.pop("1000000000102", None)      # accepted but not applied
        return status, data
    monkeypatch.setattr(tp, "_send", lossy)
    rc = tp.cmd_add(add_args(prices_file(tmp_path, [("1000000000101", "5"),
                                                    ("1000000000102", "6")]), apply=True))
    assert rc == 1 and "MISMATCH" in capsys.readouterr().out


def test_rate_limit_code_backs_off_and_retries(api):
    api.script = [(200, {"code": tp.RATE_LIMIT_CODE}), (429, {}), (200, {"code": 0, "data": {}})]
    assert tp.call("POST", f"{tp.BASE_PATH}/search", {"page_size": 100})["code"] == 0
    assert len(api.calls) == 3


def test_expired_token_refreshes_once(api, monkeypatch):
    refreshed = []
    monkeypatch.setattr(tp.tts, "_refresh_access_token", lambda: refreshed.append(1))
    code = sorted(tp.tts.TOKEN_EXPIRED_CODES)[0]
    api.script = [(200, {"code": code}), (200, {"code": code})]
    data = tp.call("POST", f"{tp.BASE_PATH}/search", {})
    assert refreshed == [1] and data["code"] == code      # second expiry is returned, not looped


def test_gives_up_after_max_attempts(api):
    api.script = [(429, {})] * tp.MAX_ATTEMPTS
    with pytest.raises(RuntimeError, match="gave up"):
        tp.call("POST", f"{tp.BASE_PATH}/search", {})


def test_create_dry_run_and_apply(api, capsys):
    a = argparse.Namespace(title="Test sale", begin="2026-11-01T00:00:00-08:00",
                           end="2026-11-30T23:59:59-08:00", apply=False)
    assert tp.cmd_create(a) == 0 and api.writes == []
    a.apply = True
    assert tp.cmd_create(a) == 0
    (_, path, body), = api.writes
    assert body["activity_type"] == "FIXED_PRICE" and body["product_level"] == "PRODUCT"
    assert body["end_time"] > body["begin_time"]


def test_create_refuses_a_naive_timestamp_and_a_backwards_window():
    with pytest.raises(SystemExit, match="offset"):
        tp.epoch("2026-11-01T00:00:00")
    a = argparse.Namespace(title="x", begin="2026-11-02T00:00:00+00:00",
                           end="2026-11-01T00:00:00+00:00", apply=False)
    with pytest.raises(SystemExit, match="after"):
        tp.cmd_create(a)


@pytest.mark.parametrize("rows,match", [
    ([("1.00000000001E+12", "5")], "non-numeric"),
    ([("1000000000101", "abc")], "not a number"),
    ([("1000000000101", "0")], "positive"),
    ([("1000000000101", "5"), ("1000000000101", "6")], "duplicate"),
])
def test_prices_file_is_refused_whole_on_a_bad_row(tmp_path, rows, match):
    with pytest.raises(SystemExit, match=match):
        tp.read_prices(__import__("pathlib").Path(prices_file(tmp_path, rows)))


def test_export_writes_live_prices(api, tmp_path):
    api.products = {"1000000000101": "20", "1000000000102": "7.5"}
    out = tmp_path / "live.csv"
    tp.cmd_export(argparse.Namespace(activity_id=ACT, out=str(out)))
    assert tp.read_prices(out) == {"1000000000101": "20", "1000000000102": "7.5"}


def test_main_names_missing_credentials(monkeypatch, capsys):
    monkeypatch.setattr(tp, "load_dotenv", lambda *a, **k: None)
    for k in tp.REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    assert tp.main(["list"]) == 2
    assert "TIKTOK_SHOP_CIPHER" in capsys.readouterr().err


def test_apply_defaults_off():
    ns = tp.build_parser().parse_args(["add", ACT, "--prices-file", "p.csv"])
    assert ns.apply is False
