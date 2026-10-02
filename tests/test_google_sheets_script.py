"""Tests for google_sheets_script.py.

This module can mutate a live Google Sheet and deploy a public Apps Script
web app, so the tests pin exactly the guarantees the module's docstring
promises (hermetic: the HTTP layer and credentials are faked):
  - sheets_rename_tab issues one batchUpdate shaped as updateSheetProperties.
  - script_deploy ALWAYS calls deployments.list before deciding create vs
    update.
  - Nothing calls projects.updateContent or projects.deployments.create
    unless confirm= / allow_new_deployment= is explicitly True.
"""
from __future__ import annotations

import json

import pytest

import google_sheets_script as gs


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def no_real_auth(monkeypatch):
    """Every test replaces the HTTP layer, so credentials must never be built."""
    monkeypatch.setattr(gs, "_headers", lambda: {"Authorization": "Bearer fake"})


def _spreadsheet_payload(titles_and_ids: dict[str, int]) -> dict:
    return {"sheets": [{"properties": {"title": t, "sheetId": i}}
                        for t, i in titles_and_ids.items()]}


# ------------------------------------------------------------------ sheets --

def test_rename_tab_issues_exactly_one_update_sheet_properties_batch(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/SPREADSHEET_ID")
        return FakeResponse(200, _spreadsheet_payload({"Untitled": 0}))

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append((url, json.loads(data)))
        return FakeResponse(200, {"spreadsheetId": "SPREADSHEET_ID", "replies": [{}]})

    monkeypatch.setattr(gs.requests, "get", fake_get)
    monkeypatch.setattr(gs.requests, "post", fake_post)

    gs.sheets_rename_tab("SPREADSHEET_ID", "Untitled", "Products")

    assert len(calls) == 1
    url, body = calls[0]
    assert url == f"{gs.SHEETS_API}/SPREADSHEET_ID:batchUpdate"
    assert body == {"requests": [{
        "updateSheetProperties": {
            "properties": {"sheetId": 0, "title": "Products"},
            "fields": "title",
        }
    }]}


def test_rename_tab_rejects_an_unknown_title(monkeypatch):
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, _spreadsheet_payload({"Sheet1": 0})))
    with pytest.raises(RuntimeError, match="No tab named"):
        gs.sheets_rename_tab("SPREADSHEET_ID", "Untitled", "Products")


def test_add_tabs_skips_ones_that_already_exist(monkeypatch):
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, _spreadsheet_payload({"Products": 5})))
    batch_calls, value_calls = [], []
    monkeypatch.setattr(gs.requests, "post",
                         lambda url, headers=None, data=None, timeout=None:
                         batch_calls.append(json.loads(data)) or FakeResponse(200, {}))
    monkeypatch.setattr(gs.requests, "put",
                         lambda url, headers=None, params=None, data=None, timeout=None:
                         value_calls.append(json.loads(data)) or FakeResponse(200, {}))

    result = gs.sheets_add_tabs("SPREADSHEET_ID", [
        {"title": "Products", "headers": ["a"]},       # already exists -> skip
        {"title": "Settings", "headers": ["key", "value"]},  # new
    ])

    assert result["already_existed"] == ["Products"]
    assert result["created"] == ["Settings"]
    assert len(batch_calls) == 1
    assert batch_calls[0] == {"requests": [{"addSheet": {"properties": {"title": "Settings"}}}]}
    assert len(value_calls) == 1
    assert value_calls[0]["values"] == [["key", "value"]]


def test_set_checkboxes_builds_the_correct_grid_range(monkeypatch):
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, _spreadsheet_payload({"Products": 7})))
    calls = []
    monkeypatch.setattr(gs.requests, "post",
                         lambda url, headers=None, data=None, timeout=None:
                         calls.append(json.loads(data)) or FakeResponse(200, {}))

    gs.sheets_set_checkboxes("SPREADSHEET_ID", "Products!H2:H501")

    [body] = calls
    grid = body["requests"][0]["setDataValidation"]["range"]
    assert grid == {
        "sheetId": 7,
        "startRowIndex": 1, "endRowIndex": 501,
        "startColumnIndex": 7, "endColumnIndex": 8,
    }


# -------------------------------------------------------------- apps script -

def test_push_content_without_confirm_never_calls_update_content(monkeypatch):
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, {"files": [
                             {"name": "Code", "type": "SERVER_JS", "source": "old"},
                             {"name": "appsscript", "type": "JSON", "source": "{}"},
                         ]}))
    put_calls = []
    monkeypatch.setattr(gs.requests, "put",
                         lambda *a, **k: put_calls.append(1) or FakeResponse(200, {}))

    result = gs.script_push_content(
        "SCRIPT_ID", [{"name": "Code", "type": "SERVER_JS", "source": "new"}], confirm=False)

    assert put_calls == []
    assert result["committed"] is False
    assert result["changed"] == ["Code"]
    assert result["preserved_untouched"] == ["appsscript"]


def test_push_content_with_confirm_preserves_untouched_remote_files(monkeypatch):
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, {"files": [
                             {"name": "Code", "type": "SERVER_JS", "source": "old"},
                             {"name": "appsscript", "type": "JSON", "source": "{\"manifest\":true}"},
                         ]}))
    put_calls = []

    def fake_put(url, headers=None, data=None, timeout=None):
        put_calls.append(json.loads(data))
        return FakeResponse(200, {"scriptId": "SCRIPT_ID"})

    monkeypatch.setattr(gs.requests, "put", fake_put)

    result = gs.script_push_content(
        "SCRIPT_ID", [{"name": "Code", "type": "SERVER_JS", "source": "new"}], confirm=True)

    assert result["committed"] is True
    [body] = put_calls
    names = {f["name"] for f in body["files"]}
    assert names == {"Code", "appsscript"}
    manifest = next(f for f in body["files"] if f["name"] == "appsscript")
    assert manifest["source"] == "{\"manifest\":true}"
    code = next(f for f in body["files"] if f["name"] == "Code")
    assert code["source"] == "new"


def test_deploy_always_lists_deployments_first(monkeypatch):
    call_order = []

    def fake_get(url, headers=None, timeout=None):
        call_order.append("list")
        return FakeResponse(200, {"deployments": []})

    def fake_post(url, headers=None, data=None, timeout=None):
        call_order.append("create" if "deployments" in url else "version")
        if url.endswith("/versions"):
            return FakeResponse(200, {"versionNumber": 1})
        return FakeResponse(200, {"deploymentId": "D1", "entryPoints": [
            {"entryPointType": "WEB_APP", "webApp": {"url": "https://example.com/exec"}}]})

    monkeypatch.setattr(gs.requests, "get", fake_get)
    monkeypatch.setattr(gs.requests, "post", fake_post)

    gs.script_deploy("SCRIPT_ID", "first deploy")

    assert call_order[0] == "list"


def test_deploy_updates_the_existing_deployment_by_default(monkeypatch):
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, {"deployments": [
                             {"deploymentId": "D1", "deploymentConfig": {"versionNumber": 3}},
                         ]}))

    create_deployment_calls = []

    def fake_post(url, headers=None, data=None, timeout=None):
        assert url.endswith("/versions"), "only a new VERSION may be created, never a new deployment"
        return FakeResponse(200, {"versionNumber": 4})

    def fake_put(url, headers=None, data=None, timeout=None):
        assert url.endswith("/deployments/D1")
        return FakeResponse(200, {"deploymentId": "D1", "entryPoints": [
            {"entryPointType": "WEB_APP", "webApp": {"url": "https://example.com/exec"}}]})

    monkeypatch.setattr(gs.requests, "post", fake_post)
    monkeypatch.setattr(gs.requests, "put", fake_put)

    result = gs.script_deploy("SCRIPT_ID", "v2", allow_new_deployment=False)

    assert result["action"] == "updated"
    assert result["url"] == "https://example.com/exec"


def test_deploy_never_creates_a_new_deployment_unless_explicitly_allowed(monkeypatch):
    """An existing deployment + allow_new_deployment=False (the default) must
    never hit projects.deployments.create -- that changes the public /exec URL."""
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, {"deployments": [
                             {"deploymentId": "D1", "deploymentConfig": {"versionNumber": 3}},
                         ]}))
    monkeypatch.setattr(gs.requests, "post",
                         lambda url, **k: FakeResponse(200, {"versionNumber": 9})
                         if url.endswith("/versions")
                         else pytest.fail("deployments.create was called without allow_new_deployment"))
    monkeypatch.setattr(gs.requests, "put",
                         lambda *a, **k: FakeResponse(200, {"deploymentId": "D1", "entryPoints": []}))

    gs.script_deploy("SCRIPT_ID", "v2")  # allow_new_deployment defaults False


def test_deploy_creates_new_when_requested(monkeypatch):
    monkeypatch.setattr(gs.requests, "get",
                         lambda *a, **k: FakeResponse(200, {"deployments": [
                             {"deploymentId": "D1", "deploymentConfig": {"versionNumber": 3}},
                         ]}))

    def fake_post(url, headers=None, data=None, timeout=None):
        if url.endswith("/versions"):
            return FakeResponse(200, {"versionNumber": 4})
        assert url.endswith("/deployments")
        return FakeResponse(200, {"deploymentId": "D2", "entryPoints": [
            {"entryPointType": "WEB_APP", "webApp": {"url": "https://example.com/new/exec"}}]})

    monkeypatch.setattr(gs.requests, "post", fake_post)
    put_calls = []
    monkeypatch.setattr(gs.requests, "put", lambda *a, **k: put_calls.append(1) or FakeResponse(200, {}))

    result = gs.script_deploy("SCRIPT_ID", "v2", allow_new_deployment=True)

    assert result["action"] == "created"
    assert put_calls == [], "creating a new deployment must never touch the old one via PUT"
