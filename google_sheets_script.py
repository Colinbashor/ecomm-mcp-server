r"""
Google Sheets + Apps Script write operations — mutates a live Sheet and can
deploy a public Apps Script web app. NOT a warehouse connector (nothing here
syncs INTO warehouse.db), so it lives at the repo root next to
`google_ads_mutate.py` rather than under `warehouse/connectors/`.

Typical use: an assistant connected to this repo's MCP server builds a small
internal tool on top of a Google Sheet — adds the tabs and header rows, fills
in seed values, turns a column into checkboxes, then pushes and deploys a
bound Apps Script web app (a voting form, an approval queue, a lightweight
data-entry page) without anyone copy-pasting code into the script editor.

WHY THIS NEEDS CARE, UNLIKE `google_ads_mutate.py`: the Sheets and Apps Script
APIs have no `validate_only` server-side dry-run mode the way Google Ads does,
so the safety net here is narrower and placed where it actually matters:
  - `script_push_content` DIFFS against the live remote content before writing
    anything, and only commits with `confirm=True`. It also preserves every
    remote file it was not given (`updateContent` REPLACES the whole file
    list, so pushing only `Code.gs` + `Index.html` would otherwise silently
    delete the `appsscript.json` manifest and break the project).
  - `script_deploy` ALWAYS calls `deployments.list` FIRST. Updating an EXISTING
    deployment in place (same `/exec` URL) is the default; creating a
    brand-new deployment — which changes the URL and breaks every link
    already shared — requires `allow_new_deployment=True`. When a project has
    several versioned deployments, the FIRST one listed is updated.
Sheet-level writes (add/rename tabs, write values, set checkboxes) have no
dry-run concept in the Sheets API itself (a batchUpdate either runs or 400s),
so those commit directly. They are still gated at the MCP layer by
`WAREHOUSE_MCP_ENABLE_WRITES=1` (see `server.py`) and by `destructiveHint` on
each registered tool, so a client prompts before calling one.

AUTH: `google_sheets_auth.py` runs the one-time OAuth consent flow (see that
file for scopes) and saves `GOOGLE_SHEETS_REFRESH_TOKEN` to `.env`. This module
reads `GOOGLE_SHEETS_CLIENT_ID` / `GOOGLE_SHEETS_CLIENT_SECRET` /
`GOOGLE_SHEETS_REFRESH_TOKEN` fresh on every call and refreshes the access
token itself (`google.oauth2.credentials.Credentials` + `Request()`); it never
runs the interactive flow. This is a DIFFERENT credential from the Google Ads
OAuth token and from the GA4 / Merchant Center service account — it acts as
the signed-in Google user, so it can only touch Sheets that user can edit.
The Apps Script API must also be switched on for that user once, at
https://script.google.com/home/usersettings, or every `script_*` call 403s.

Deliberately raw `requests` calls signed with a bearer token, not
google-api-python-client — the same choice `merchant_center_sync.py` makes:
one fewer heavy dependency, and the request bodies map 1:1 onto the REST docs.

USAGE
  python google_sheets_script.py add-tabs --spreadsheet-id ID \
      --tabs-json '[{"title":"Settings","headers":["key","value","notes"]}]'

  python google_sheets_script.py rename-tab --spreadsheet-id ID \
      --old-title "Sheet1" --new-title "Products"

  python google_sheets_script.py write-values --spreadsheet-id ID \
      --range "Settings!A2:C2" --values-json '[["round_name","Spring review",""]]'

  python google_sheets_script.py set-checkboxes --spreadsheet-id ID \
      --range "Products!H2:H501"

  python google_sheets_script.py create-project --title "Product voting" \
      --parent-spreadsheet-id ID

  python google_sheets_script.py push-content --script-id ID --dir my_app      # dry-run diff
  python google_sheets_script.py push-content --script-id ID --dir my_app --confirm

  python google_sheets_script.py deploy --script-id ID --description "v2: wider pick cap"
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCRIPT_API = "https://script.googleapis.com/v1/projects"
TIMEOUT = 60

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/drive.file",
]

# Apps Script project file extension -> API file `type`.
_FILE_TYPE_BY_EXT = {
    ".gs": "SERVER_JS",
    ".html": "HTML",
    ".json": "JSON",
}


def _credentials():
    """Build a user credential from the saved refresh token and refresh it
    in place -- never runs the interactive consent flow (see
    google_sheets_auth.py for that)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    for key in ("GOOGLE_SHEETS_CLIENT_ID", "GOOGLE_SHEETS_CLIENT_SECRET",
                "GOOGLE_SHEETS_REFRESH_TOKEN"):
        if not os.environ.get(key):
            sys.exit(f"{key} is not set in .env -- run google_sheets_auth.py first.")

    creds = Credentials(
        None,
        refresh_token=os.environ["GOOGLE_SHEETS_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_SHEETS_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_SHEETS_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_credentials().token}"}


def _raise_for_status(resp: requests.Response, what: str) -> None:
    if resp.status_code >= 300:
        raise RuntimeError(f"{what} failed [{resp.status_code}]: {resp.text[:500]}")


# --------------------------------------------------------------------------
# Sheets API
# --------------------------------------------------------------------------

def _get_spreadsheet(spreadsheet_id: str) -> dict:
    r = requests.get(f"{SHEETS_API}/{spreadsheet_id}", headers=_headers(),
                      params={"fields": "sheets.properties"}, timeout=TIMEOUT)
    _raise_for_status(r, "spreadsheets.get")
    return r.json()


def _sheet_titles(spreadsheet_id: str) -> dict[str, int]:
    """{title: sheetId} for every tab currently in the spreadsheet."""
    data = _get_spreadsheet(spreadsheet_id)
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in data.get("sheets", [])}


def _batch_update(spreadsheet_id: str, requests_: list[dict]) -> dict:
    r = requests.post(f"{SHEETS_API}/{spreadsheet_id}:batchUpdate",
                       headers={**_headers(), "Content-Type": "application/json"},
                       data=json.dumps({"requests": requests_}), timeout=TIMEOUT)
    _raise_for_status(r, "spreadsheets.batchUpdate")
    return r.json()


def sheets_add_tabs(spreadsheet_id: str, tabs: list[dict]) -> dict:
    """Add one or more tabs, each optionally with a bold header row.

    tabs: [{"title": "Settings", "headers": ["key", "value", "notes"]}, ...]

    Idempotent: a tab whose title already
    exists is left untouched (never re-titled, never re-headered) rather than
    erroring, since this is meant to be safe to re-run after a partial
    failure.
    """
    existing = _sheet_titles(spreadsheet_id)
    to_create = [t for t in tabs if t["title"] not in existing]
    skipped = [t["title"] for t in tabs if t["title"] in existing]

    if to_create:
        _batch_update(spreadsheet_id, [
            {"addSheet": {"properties": {"title": t["title"]}}} for t in to_create
        ])
        for t in to_create:
            headers = t.get("headers") or []
            if headers:
                sheets_write_values(spreadsheet_id, f"'{t['title']}'!A1", [headers])

    return {"created": [t["title"] for t in to_create], "already_existed": skipped}


def sheets_rename_tab(spreadsheet_id: str, old_title: str, new_title: str) -> dict:
    existing = _sheet_titles(spreadsheet_id)
    if old_title not in existing:
        raise RuntimeError(
            f"No tab named {old_title!r} in this spreadsheet. "
            f"Current tabs: {sorted(existing)}")
    _batch_update(spreadsheet_id, [{
        "updateSheetProperties": {
            "properties": {"sheetId": existing[old_title], "title": new_title},
            "fields": "title",
        }
    }])
    return {"renamed": old_title, "to": new_title}


def sheets_write_values(spreadsheet_id: str, range_a1: str, values: list[list[Any]]) -> dict:
    """Write a block of values starting at range_a1's top-left cell.
    RAW input -- a value is stored exactly as given, never re-interpreted as
    a formula or auto-formatted the way USER_ENTERED would (the one case that
    matters here is a header row, which must round-trip byte for byte)."""
    r = requests.put(
        f"{SHEETS_API}/{spreadsheet_id}/values/{quote(range_a1, safe='')}",
        headers={**_headers(), "Content-Type": "application/json"},
        params={"valueInputOption": "RAW"},
        data=json.dumps({"range": range_a1, "values": values}),
        timeout=TIMEOUT,
    )
    _raise_for_status(r, "spreadsheets.values.update")
    return r.json()


_A1_CELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _col_to_index(letters: str) -> int:
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26 ..."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _parse_a1_range(spreadsheet_id: str, range_a1: str) -> dict:
    """'Products!H2:H501' -> a Sheets API GridRange. Requires a sheet name and
    fully-qualified cell references (no bare column like 'H:H') -- enough for
    how this module is actually used (a header-relative data range), not a
    general A1 parser."""
    if "!" not in range_a1:
        raise ValueError(f"range must include a sheet name, e.g. 'Products!H2:H501' (got {range_a1!r})")
    sheet_title, cells = range_a1.split("!", 1)
    sheet_title = sheet_title.strip("'")
    start_cell, _, end_cell = cells.partition(":")
    end_cell = end_cell or start_cell

    start_m, end_m = _A1_CELL_RE.match(start_cell), _A1_CELL_RE.match(end_cell)
    if not start_m or not end_m:
        raise ValueError(f"only fully-qualified cell ranges are supported (got {range_a1!r})")

    existing = _sheet_titles(spreadsheet_id)
    if sheet_title not in existing:
        raise RuntimeError(f"No tab named {sheet_title!r}. Current tabs: {sorted(existing)}")

    start_col, start_row = _col_to_index(start_m.group(1)), int(start_m.group(2))
    end_col, end_row = _col_to_index(end_m.group(1)), int(end_m.group(2))
    return {
        "sheetId": existing[sheet_title],
        "startRowIndex": start_row - 1,
        "endRowIndex": end_row,                  # exclusive; numerically == end_row
        "startColumnIndex": start_col,
        "endColumnIndex": end_col + 1,            # exclusive
    }


def sheets_set_checkboxes(spreadsheet_id: str, range_a1: str) -> dict:
    """Turn a cell range into checkboxes (BOOLEAN data validation) -- the
    usual way to give each row an on/off flag."""
    grid_range = _parse_a1_range(spreadsheet_id, range_a1)
    return _batch_update(spreadsheet_id, [{
        "setDataValidation": {
            "range": grid_range,
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True},
        }
    }])


# --------------------------------------------------------------------------
# Apps Script API
# --------------------------------------------------------------------------

def script_create_project(title: str, parent_spreadsheet_id: str) -> str:
    """Create a new Apps Script project bound to an existing spreadsheet.
    Returns the new scriptId."""
    r = requests.post(SCRIPT_API, headers={**_headers(), "Content-Type": "application/json"},
                       data=json.dumps({"title": title, "parentId": parent_spreadsheet_id}),
                       timeout=TIMEOUT)
    _raise_for_status(r, "projects.create")
    return r.json()["scriptId"]


def load_project_files(dir_path: str | Path) -> list[dict]:
    """Read a local project directory (e.g. my_app/) into the Apps Script
    API's file list shape: [{"name", "type", "source"}, ...]. The file's
    extension picks its `type` (.gs -> SERVER_JS, .html -> HTML, .json ->
    JSON for an appsscript.json manifest); `name` drops the extension, as the
    API itself does."""
    out = []
    for path in sorted(Path(dir_path).iterdir()):
        file_type = _FILE_TYPE_BY_EXT.get(path.suffix.lower())
        if file_type is None:
            continue
        out.append({
            "name": path.stem if path.name != "appsscript.json" else "appsscript",
            "type": file_type,
            "source": path.read_text(encoding="utf-8"),
        })
    return out


def _get_content(script_id: str) -> dict:
    r = requests.get(f"{SCRIPT_API}/{script_id}/content", headers=_headers(), timeout=TIMEOUT)
    _raise_for_status(r, "projects.getContent")
    return r.json()


def script_push_content(script_id: str, files: list[dict], confirm: bool = False) -> dict:
    """Diff `files` against the project's CURRENT remote content and, only
    with confirm=True, write the merged result back.

    Any remote file NOT named in `files` (typically appsscript.json, the
    manifest every Apps Script project is created with) is preserved
    untouched rather than dropped -- updateContent REPLACES the whole file
    list, so naively pushing just Code.gs + Index.html would silently delete
    the manifest and break the project.
    """
    current = _get_content(script_id)
    existing_by_name = {f["name"]: f for f in current.get("files", [])}
    new_by_name = {f["name"]: f for f in files}

    diffs: dict[str, str] = {}
    added, changed, unchanged = [], [], []
    for name, new_file in new_by_name.items():
        old_source = existing_by_name.get(name, {}).get("source")
        if old_source is None:
            added.append(name)
            diffs[name] = f"(new file, {len(new_file['source'])} chars)"
        elif old_source != new_file["source"]:
            changed.append(name)
            diffs[name] = "\n".join(difflib.unified_diff(
                old_source.splitlines(), new_file["source"].splitlines(),
                fromfile=f"{name} (remote)", tofile=f"{name} (local)", lineterm=""))
        else:
            unchanged.append(name)
    preserved = sorted(set(existing_by_name) - set(new_by_name))

    result = {
        "added": added, "changed": changed, "unchanged": unchanged,
        "preserved_untouched": preserved, "diffs": diffs, "committed": False,
    }
    if not confirm:
        return result

    merged = {**existing_by_name, **new_by_name}
    r = requests.put(f"{SCRIPT_API}/{script_id}/content",
                      headers={**_headers(), "Content-Type": "application/json"},
                      data=json.dumps({"files": list(merged.values())}), timeout=TIMEOUT)
    _raise_for_status(r, "projects.updateContent")
    result["committed"] = True
    return result


def _list_deployments(script_id: str) -> list[dict]:
    r = requests.get(f"{SCRIPT_API}/{script_id}/deployments", headers=_headers(), timeout=TIMEOUT)
    _raise_for_status(r, "projects.deployments.list")
    return r.json().get("deployments", [])


def _create_version(script_id: str, description: str) -> int:
    r = requests.post(f"{SCRIPT_API}/{script_id}/versions",
                       headers={**_headers(), "Content-Type": "application/json"},
                       data=json.dumps({"description": description}), timeout=TIMEOUT)
    _raise_for_status(r, "projects.versions.create")
    return r.json()["versionNumber"]


def _deployment_url(deployment: dict) -> str | None:
    for ep in deployment.get("entryPoints", []):
        if ep.get("entryPointType") == "WEB_APP":
            return ep.get("webApp", {}).get("url")
    return None


def script_deploy(script_id: str, description: str, allow_new_deployment: bool = False) -> dict:
    """Publish the project's current content as (by default) an UPDATE to its
    existing deployment, keeping the same /exec URL every link already points
    at. Pass allow_new_deployment=True to instead create a brand-new
    deployment with a NEW URL -- a deliberate, rare action (see the module
    docstring).

    ALWAYS calls deployments.list first, per the safety rule this whole
    module is built around: deciding create-vs-update by looking, never by
    assuming.
    """
    existing = [d for d in _list_deployments(script_id)
                if d.get("deploymentConfig", {}).get("versionNumber") is not None]

    if not existing or allow_new_deployment:
        version = _create_version(script_id, description)
        r = requests.post(f"{SCRIPT_API}/{script_id}/deployments",
                           headers={**_headers(), "Content-Type": "application/json"},
                           data=json.dumps({
                               "versionNumber": version,
                               "manifestFileName": "appsscript",
                               "description": description,
                           }), timeout=TIMEOUT)
        _raise_for_status(r, "projects.deployments.create")
        deployment = r.json()
        return {"action": "created", "url": _deployment_url(deployment), "deployment": deployment}

    target = existing[0]
    version = _create_version(script_id, description)
    r = requests.put(f"{SCRIPT_API}/{script_id}/deployments/{target['deploymentId']}",
                      headers={**_headers(), "Content-Type": "application/json"},
                      data=json.dumps({"deploymentConfig": {
                          "scriptId": script_id,
                          "versionNumber": version,
                          "manifestFileName": "appsscript",
                          "description": description,
                      }}), timeout=TIMEOUT)
    _raise_for_status(r, "projects.deployments.update")
    deployment = r.json()
    return {"action": "updated", "url": _deployment_url(deployment), "deployment": deployment}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_add_tabs(args):
    tabs = json.loads(args.tabs_json)
    print(json.dumps(sheets_add_tabs(args.spreadsheet_id, tabs), indent=2))


def _cmd_rename_tab(args):
    print(json.dumps(sheets_rename_tab(args.spreadsheet_id, args.old_title, args.new_title), indent=2))


def _cmd_write_values(args):
    values = json.loads(args.values_json)
    print(json.dumps(sheets_write_values(args.spreadsheet_id, args.range, values), indent=2))


def _cmd_set_checkboxes(args):
    print(json.dumps(sheets_set_checkboxes(args.spreadsheet_id, args.range), indent=2))


def _cmd_create_project(args):
    script_id = script_create_project(args.title, args.parent_spreadsheet_id)
    print(f"scriptId={script_id}")


def _cmd_push_content(args):
    files = load_project_files(args.dir)
    if not files:
        sys.exit(f"No .gs/.html/.json files found in {args.dir}")
    result = script_push_content(args.script_id, files, confirm=args.confirm)
    for name, diff in result["diffs"].items():
        print(f"--- {name} ---\n{diff}\n")
    print(json.dumps({k: v for k, v in result.items() if k != "diffs"}, indent=2))
    if not args.confirm:
        print("\n(dry run -- nothing was written; re-run with --confirm to push)")


def _cmd_deploy(args):
    print(json.dumps(script_deploy(args.script_id, args.description,
                                    allow_new_deployment=args.allow_new_deployment), indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-tabs")
    p.add_argument("--spreadsheet-id", required=True)
    p.add_argument("--tabs-json", required=True,
                   help='JSON list, e.g. \'[{"title":"Settings","headers":["key","value"]}]\'')
    p.set_defaults(func=_cmd_add_tabs)

    p = sub.add_parser("rename-tab")
    p.add_argument("--spreadsheet-id", required=True)
    p.add_argument("--old-title", required=True)
    p.add_argument("--new-title", required=True)
    p.set_defaults(func=_cmd_rename_tab)

    p = sub.add_parser("write-values")
    p.add_argument("--spreadsheet-id", required=True)
    p.add_argument("--range", required=True, help="e.g. \"Settings!A2:C2\"")
    p.add_argument("--values-json", required=True, help='JSON list of rows, e.g. \'[["a","b"]]\'')
    p.set_defaults(func=_cmd_write_values)

    p = sub.add_parser("set-checkboxes")
    p.add_argument("--spreadsheet-id", required=True)
    p.add_argument("--range", required=True, help="e.g. \"Products!H2:H501\"")
    p.set_defaults(func=_cmd_set_checkboxes)

    p = sub.add_parser("create-project")
    p.add_argument("--title", required=True)
    p.add_argument("--parent-spreadsheet-id", required=True)
    p.set_defaults(func=_cmd_create_project)

    p = sub.add_parser("push-content")
    p.add_argument("--script-id", required=True)
    p.add_argument("--dir", required=True, help="directory of .gs/.html/appsscript.json files")
    p.add_argument("--confirm", action="store_true", help="actually write; default is dry-run diff")
    p.set_defaults(func=_cmd_push_content)

    p = sub.add_parser("deploy")
    p.add_argument("--script-id", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--allow-new-deployment", action="store_true",
                   help="create a NEW deployment (new /exec URL) instead of updating the existing one")
    p.set_defaults(func=_cmd_deploy)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
