"""tiktok_fbt_sync: parsing, storage and the signed request loop. Hermetic."""
from __future__ import annotations

import sqlite3

import pytest

import tiktok_fbt_sync as sync

STAMP = "2026-01-15T00:00:00+00:00"
SNAP = "2026-01-15"


def _inv(wh="W1", gid="G1", ref="SKU-1001", avail=0, res=0, total=1, unf=1, transit=0):
    return {"fbt_warehouse_id": wh,
            "goods": {"id": gid, "name": "Example Tee - M", "reference_code": ref,
                      "skus": [{"id": "1700000000001"}]},
            "in_transit_quantity": transit,
            "on_hand_detail": {"available_quantity": avail, "reserved_quantity": res,
                               "total_quantity": total, "unfulfillable_quantity": unf}}


def _rec(ct=1768000000, gid="G1", init=2, final=1, order="ORDER-1"):
    return {"changed_quantity": final - init, "create_time": ct, "fbt_warehouse_id": "W1",
            "final_on_hand_quantity": final, "initial_on_hand_quantity": init,
            "goods": {"id": gid, "name": "Example Tee", "reference_code": "SKU-1002"},
            "inventory_goods_type": "NORMAL", "order": {"id": order, "type": "CONSIGN_ORDER"}}


def _goods(gid="G1", ref="SKU-1001"):
    return {"id": gid, "reference_code": ref, "name": "Example Tee - M",
            "skus": [{"id": "1700000000001", "product": {"id": "1729000000001"}}],
            "barcodes": [{"code": ref, "type": "MERCHANT SKU ID"}]}


@pytest.fixture(autouse=True)
def _no_pattern(monkeypatch):
    monkeypatch.delenv("TIKTOK_FBT_SKU_PATTERN", raising=False)


# ---- parsing --------------------------------------------------------------

@pytest.mark.parametrize("ref,key", [("SKU-1001", "SKU-1001"), (" SKU-1 ", "SKU-1"),
                                     ("", None), (None, None)])
def test_sku_key_without_a_pattern_keeps_the_code(ref, key):
    assert sync.sku_key_for(ref) == key


@pytest.mark.parametrize("ref,key", [("SKU-1001", "SKU-1001"), ("BUNDLE-7", None),
                                     ("XSKU-1001", None)])
def test_sku_key_pattern_must_match_the_whole_code(monkeypatch, ref, key):
    monkeypatch.setenv("TIKTOK_FBT_SKU_PATTERN", r"SKU-\d+")
    assert sync.sku_key_for(ref) == key


def test_status_empty_census_is_degraded():
    assert sync.status_for(0) == "degraded"
    assert sync.status_for(3) == "ok"


def test_inventory_rows_keep_buckets_apart():
    rows = sync.inventory_rows([_inv(avail=3, res=1, total=4, unf=2, transit=5)],
                               {"W1": "Warehouse A"}, SNAP, STAMP)
    (r,) = rows
    assert r[:7] == (SNAP, "W1", "Warehouse A", "G1", "SKU-1001", "SKU-1001", "Example Tee - M")
    assert r[8:13] == (3, 1, 4, 2, 5)
    assert sync.summarize(rows) == {"available": 3, "reserved": 1, "total_onhand": 4,
                                    "unfulfillable": 2, "in_transit": 5}


def test_record_rows_use_the_utc_date_and_blank_a_missing_order():
    rec = _rec()
    rec["order"] = None
    (r,) = sync.record_rows([rec], STAMP)
    assert r[1] == "2026-01-09"            # 1768000000 = 2026-01-09 23:06 UTC
    assert r[7] == "" and r[11] == -1


# ---- storage --------------------------------------------------------------

def _store(conn, inv, recs, goods=None, snap=SNAP):
    sync.store(conn, snap, {"W1": "Warehouse A"},
               sync.goods_rows(goods or [_goods()], STAMP),
               sync.inventory_rows(inv, {"W1": "Warehouse A"}, snap, STAMP),
               sync.record_rows(recs, STAMP), STAMP)


def test_empty_inventory_is_logged_as_a_measured_zero():
    conn = sqlite3.connect(":memory:")
    _store(conn, [], [])
    assert conn.execute("SELECT goods_registered, inventory_rows, available "
                        "FROM tiktok_fbt_snapshot_log").fetchone() == (1, 0, 0)


def test_rerun_replaces_the_day_and_records_are_idempotent():
    conn = sqlite3.connect(":memory:")
    _store(conn, [_inv(gid="G1"), _inv(gid="G2", ref="SKU-1003")], [_rec(), _rec(ct=1)])
    _store(conn, [_inv(gid="G1", avail=4)], [_rec(), _rec(ct=1)])
    assert conn.execute("SELECT goods_id, available FROM tiktok_fbt_inventory").fetchall() \
        == [("G1", 4)]
    assert conn.execute("SELECT COUNT(*) FROM tiktok_fbt_inventory_records").fetchone()[0] == 2


def test_goods_rows_join_product_and_sku_ids():
    (g,) = sync.goods_rows([_goods()], STAMP)
    assert g[4] == "1729000000001" and g[5] == "1700000000001"


# ---- API plumbing ---------------------------------------------------------

def test_paged_stops_on_empty_token_not_short_page(monkeypatch):
    pages = iter([{"goods": [1], "next_page_token": "t2"},
                  {"goods": [2], "next_page_token": ""}])
    calls = []
    monkeypatch.setattr(sync, "_call", lambda *a: calls.append(a) or next(pages))
    assert sync._paged(sync.GOODS_PATH, {}, "goods") == [1, 2]
    assert calls[1][3]["page_token"] == "t2"


def test_inventory_is_queried_in_id_chunks(monkeypatch):
    bodies = []
    monkeypatch.setattr(sync, "_paged", lambda path, body, key: bodies.append(body) or [])
    sync.fetch_inventory([str(i) for i in range(250)])
    assert [len(b["goods_ids"]) for b in bodies] == [100, 100, 50]


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


@pytest.fixture
def api_env(monkeypatch):
    for k in sync.REQUIRED_ENV:
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)


def test_call_retries_intermittent_access_denied(monkeypatch, api_env):
    answers = iter([{"code": 105005}, {"code": 105005}, {"code": 0, "data": {"ok": 1}}])
    monkeypatch.setattr(sync.net, "request", lambda *a, **k: _Resp(next(answers)))
    assert sync._call("POST", sync.GOODS_PATH, {}) == {"ok": 1}


def test_call_raises_access_denied_when_it_persists(monkeypatch, api_env):
    monkeypatch.setattr(sync.net, "request", lambda *a, **k: _Resp({"code": 105005}))
    with pytest.raises(sync.AccessDenied):
        sync._call("POST", sync.GOODS_PATH, {})


def test_call_refreshes_an_expired_token_once(monkeypatch, api_env):
    answers = iter([{"code": 105002}, {"code": 0, "data": {"ok": 1}}])
    refreshed = []
    monkeypatch.setattr(sync.net, "request", lambda *a, **k: _Resp(next(answers)))
    monkeypatch.setattr(sync, "_refresh_access_token", lambda: refreshed.append(1))
    assert sync._call("GET", sync.WAREHOUSES_PATH) == {"ok": 1}
    assert refreshed == [1]


def test_call_fails_fast_on_other_codes(monkeypatch, api_env):
    monkeypatch.setattr(sync.net, "request", lambda *a, **k: _Resp({"code": 36009004,
                                                                    "message": "bad"}))
    with pytest.raises(RuntimeError, match="36009004"):
        sync._call("POST", sync.GOODS_PATH, {})
