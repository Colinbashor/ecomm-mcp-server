# Google Sheets / Apps Script (opt-in MCP write tools)

Seven **write** tools that let an assistant connected to this MCP server build
a small internal tool on top of a Google Sheet — add tabs and header rows,
fill in seed values, turn a column into checkboxes, then create, push and
deploy a bound Apps Script web app (a voting form, an approval queue, a
lightweight data-entry page) without anyone pasting code into the script
editor.

This is **not a connector**: nothing here syncs into `warehouse.db`, and none
of these tools can touch the warehouse database (`run_sql` stays read-only at
the SQLite level whether or not they are enabled). They mutate a live Google
Sheet and can publish a public web app, so they are **off by default**.

**Scripts:** `google_sheets_script.py` (the tools + a CLI),
`google_sheets_auth.py` (one-time OAuth helper). Registration lives in
`server.py`'s `register_write_tools()`.

## Setup

1. **Pick an OAuth client.** In the Google Cloud project, reuse the existing
   "Desktop app" OAuth client you made for Google Ads, or create one
   (Console → APIs & Services → Credentials → Create Credentials → OAuth
   client ID → Desktop app). Put its values in `.env`:

   ```
   GOOGLE_SHEETS_CLIENT_ID=...
   GOOGLE_SHEETS_CLIENT_SECRET=...
   ```

2. **Enable the APIs** in that Cloud project: the Google Sheets API and the
   Apps Script API.
3. **Switch the Apps Script API on for the signing-in user** at
   <https://script.google.com/home/usersettings>. This is a per-user toggle,
   off by default. Without it, every `script_*` call fails with a 403 even
   with a valid token.
4. **Mint the refresh token** (needs `pip install google-auth-oauthlib`, the
   same dependency `google_auth.py` uses):

   ```bash
   python google_sheets_auth.py
   ```

   A browser opens. Sign in as the Google account that owns or edits the
   target Sheet and approve. `GOOGLE_SHEETS_REFRESH_TOKEN` is written to
   `.env`.
5. **Turn the tools on** for the MCP server by setting, at startup:

   ```
   WAREHOUSE_MCP_ENABLE_WRITES=1
   ```

   Only the exact string `1` counts (`true`, `yes`, `on` do not). The flag is
   read once when the server starts, so restart it after changing `.env`. The
   startup log records the decision either way: an `INFO` line
   `write tools NOT registered ...` or a `WARNING` line
   `write tools REGISTERED ...`.

| Variable | Notes |
|---|---|
| `GOOGLE_SHEETS_CLIENT_ID` / `GOOGLE_SHEETS_CLIENT_SECRET` | OAuth client. May be the same Desktop-app client as `GOOGLE_ADS_*`. |
| `GOOGLE_SHEETS_REFRESH_TOKEN` | Written by `google_sheets_auth.py`. A **separate** token from the Google Ads one. |
| `WAREHOUSE_MCP_ENABLE_WRITES` | Exactly `1` registers the seven tools. Unset by default. |

**Identity and scope.** The credential acts as the signed-in Google **user**,
not as a service account. It is a different credential from the Google Ads
OAuth token and from the GA4 / Merchant Center service account. The scopes
are `spreadsheets`, `script.projects`, `script.deployments` and `drive.file`.
`drive.file` is deliberately not full Drive access. The `spreadsheets` scope
still reaches **every Sheet that user can edit**, so treat the token as that
user's edit access.

> **Do not enable this on a shared `--http` server casually.** The flag is
> server-wide, not per client. Every teammate holding the bearer token could
> edit any Sheet the `GOOGLE_SHEETS_*` user can edit and redeploy that user's
> public web apps. If only you need the tools, run a separate stdio-only
> instance with the flag set. See [SHARING.md](../SHARING.md#notes).

## The tools

All seven are registered with `readOnlyHint=False`, `destructiveHint=True`,
`idempotentHint=False` and `openWorldHint=False`. A well-behaved client
therefore asks for confirmation before each call.

| Tool | Parameters | Returns | Safety net |
|---|---|---|---|
| `sheets_add_tabs` | `spreadsheet_id`, `tabs` | `{"created": [...], "already_existed": [...]}` | Idempotent: an existing tab is left untouched |
| `sheets_rename_tab` | `spreadsheet_id`, `old_title`, `new_title` | `{"renamed": old, "to": new}` | Errors (listing current tabs) if `old_title` doesn't exist |
| `sheets_write_values` | `spreadsheet_id`, `range_a1`, `values` | The Sheets API `values.update` response | None. Commits directly |
| `sheets_set_checkboxes` | `spreadsheet_id`, `range_a1` | The Sheets API `batchUpdate` response | None. Commits directly |
| `script_create_project` | `title`, `parent_spreadsheet_id` | The new `scriptId` (a string) | — |
| `script_push_content` | `script_id`, `files`, `confirm=False` | `{"added", "changed", "unchanged", "preserved_untouched", "diffs", "committed"}` | Diff-only unless `confirm=true`. Keeps remote files it wasn't given |
| `script_deploy` | `script_id`, `description`, `allow_new_deployment=False` | `{"action": "updated"\|"created", "url", "deployment"}` | Lists deployments first. Updates in place (same `/exec` URL) by default |

### `sheets_add_tabs(spreadsheet_id, tabs)`

`tabs` is a list like
`[{"title": "Settings", "headers": ["key", "value", "notes"]}, ...]`.
`headers` is optional. Tabs whose title already exists are skipped, never
re-titled or re-headered, and reported in `already_existed`. The new tabs
are created in one `batchUpdate`. Then each new tab's `headers` are written
to row 1 starting at `A1`, as plain `RAW` values with no formatting applied.

### `sheets_rename_tab(spreadsheet_id, old_title, new_title)`

Looks the tab up by its current title and renames it in one
`updateSheetProperties` request. It does not check whether `new_title` is
already taken. If it is, the Sheets API rejects the request with an error.

### `sheets_write_values(spreadsheet_id, range_a1, values)`

`values` is a list of rows, for example `[["round_name", "Spring review", ""]]`.
The block is written starting at `range_a1`'s top-left cell, for example
`"Settings!A2:C2"`. Input is always `RAW`: a value is stored exactly as
given. A string like `=SUM(A1:A3)` or `3/4` is stored as that literal text,
never parsed as a formula or a date. **This tool cannot write formulas.**

### `sheets_set_checkboxes(spreadsheet_id, range_a1)`

Applies strict `BOOLEAN` data validation (shown as checkboxes) to a range.
The range parser is deliberately narrow:

- The range **must include a tab name**: `Products!H2:H501`, or
  `'My Tab'!H2:H501` (surrounding single quotes are stripped).
- Both ends must be **full cell references**. Whole-column (`H:H`) and
  whole-row (`2:2`) forms are rejected with a `ValueError`. A single cell
  (`Products!H2`) is fine.
- The tab must already exist. If it doesn't, the error lists the current
  tabs.

### `script_create_project(title, parent_spreadsheet_id)`

Creates a new Apps Script project **bound** to the given spreadsheet and
returns its `scriptId`. Save that ID, because the other `script_*` tools
need it. The new project starts with Google's default `appsscript` manifest.

### `script_push_content(script_id, files, confirm=False)`

`files` uses the Apps Script API's file shape:

```json
[
  {"name": "Code",  "type": "SERVER_JS", "source": "function doGet() { ... }"},
  {"name": "Index", "type": "HTML",      "source": "<!doctype html>..."},
  {"name": "appsscript", "type": "JSON", "source": "{ \"webapp\": { ... } }"}
]
```

`name` has no extension. The manifest is always named `appsscript`.

How it behaves:

1. It always fetches the project's current remote content first and
   computes, for each file it was given, whether the file is `added`,
   `changed` (with a unified diff in `diffs`) or `unchanged`.
2. Without `confirm=true` it **stops there** and returns
   `"committed": false`. That is the dry run. Show the diff to the user
   before committing.
3. With `confirm=true` it writes back the **merged** file list: remote files
   plus the given files, with the given ones replacing same-named remote
   ones. Apps Script's `updateContent` replaces the whole file list, so
   pushing only `Code` + `Index` naively would silently delete the manifest
   and break the project. Files it wasn't given are listed in
   `preserved_untouched`.

A consequence of step 3: **this tool cannot delete a remote file.** Leaving
a file out of `files` keeps it. Change detection compares `source` only.

### `script_deploy(script_id, description, allow_new_deployment=False)`

Publishes the project's **current saved content** as a new numbered
version, then:

- **Default:** it updates the **first** existing versioned deployment in
  place, so the web app keeps the same `/exec` URL every link already points
  at. The project's always-present "HEAD" test deployment has no version
  number and is ignored.
- **If the project has no versioned deployment yet** (its first deploy), it
  creates one, even without `allow_new_deployment`.
- **With `allow_new_deployment=true`**, it always creates a brand-new
  deployment with a **new** URL, which breaks links already shared. Treat
  this as a deliberate, rare action.

`deployments.list` is always called first. Whether to create or update is
decided by looking at the project, never by assuming. `url` is the web-app
`/exec` URL, or `null` if the deployment has no web-app entry point. That
happens when the manifest has no `webapp` section. The manifest's `webapp`
block (`executeAs`, `access`) also controls who can open the app and whose
identity the app runs as, so check it before deploying anything that reads
or writes the Sheet.

Each call creates a new project version, even when only the description
changed, so versions accumulate.

## Typical flow

1. `sheets_add_tabs` creates the tabs and header rows. It is safe to re-run.
2. `sheets_write_values` fills seed or config rows. Then
   `sheets_set_checkboxes` turns a flag column into checkboxes.
3. `script_create_project` creates a project bound to the Sheet. Keep the
   `scriptId`.
4. `script_push_content` without `confirm` shows the diff. Review it, then
   call it again with `confirm=true`.
5. `script_deploy` publishes the app. Later redeploys update the same URL.

The order matters for 4 → 5. `script_deploy` publishes whatever is saved
remotely, so a push that was only dry-run is **not** what gets deployed.

## CLI

The same operations work without the MCP server. The CLI does **not**
require `WAREHOUSE_MCP_ENABLE_WRITES`, only the `GOOGLE_SHEETS_*`
credentials. Run `python google_sheets_script.py --help` for the full list.

```bash
python google_sheets_script.py add-tabs --spreadsheet-id ID \
    --tabs-json '[{"title":"Settings","headers":["key","value","notes"]}]'

python google_sheets_script.py rename-tab --spreadsheet-id ID \
    --old-title "Sheet1" --new-title "Products"

python google_sheets_script.py write-values --spreadsheet-id ID \
    --range "Settings!A2:C2" --values-json '[["round_name","Spring review",""]]'

python google_sheets_script.py set-checkboxes --spreadsheet-id ID \
    --range "Products!H2:H501"

python google_sheets_script.py create-project --title "Product voting" \
    --parent-spreadsheet-id ID                       # prints scriptId=...

python google_sheets_script.py push-content --script-id ID --dir my_app            # dry-run diff
python google_sheets_script.py push-content --script-id ID --dir my_app --confirm  # write

python google_sheets_script.py deploy --script-id ID --description "v2: wider pick cap"
python google_sheets_script.py deploy --script-id ID --description "v1" --allow-new-deployment
```

`push-content --dir` reads a local folder into the `files` shape by
extension: `.gs` → `SERVER_JS`, `.html` → `HTML`, `.json` → `JSON`.
`appsscript.json` becomes the manifest named `appsscript`. Every other file
is ignored, as are subfolders, because only the top level is read. If the
folder has no matching files, the command exits with an error.

## Notes

- **No server-side dry run for Sheet writes.** Unlike Google Ads, which has
  `validate_only`, the Sheets and Apps Script APIs have no dry-run mode. The
  safety nets are where they matter most: diff-then-confirm on push, and
  update-in-place on deploy. The four `sheets_*` tools commit immediately.
- **`sheets_add_tabs` is not atomic.** Tabs are created first and headers
  are written afterward, one tab at a time. If a header write fails, the tab
  already exists without headers. A re-run then skips that tab as
  "already existed" and won't add the headers. Fix it with
  `sheets_write_values` at `'<tab>'!A1`.
- **The credential is read at call time, but `.env` is read at startup.**
  `google_sheets_script.py` reads `GOOGLE_SHEETS_*` from the process
  environment on every call and refreshes the access token itself. It never
  runs the interactive consent flow. The server loads `.env` only once, when
  it starts. After running `google_sheets_auth.py`, restart the MCP server
  so it picks up the new refresh token.
- **Missing credentials currently call `sys.exit()`.** If any
  `GOOGLE_SHEETS_*` variable is unset, the helper raises `SystemExit`
  instead of an ordinary exception. That is fine for the CLI. Inside the MCP
  server, though, `SystemExit` is not caught by the MCP SDK's
  tool-error handling, so a call can stop the server instead of returning an
  error to the client. Finish Setup before setting
  `WAREHOUSE_MCP_ENABLE_WRITES=1`.
- **Lazy import.** `server.py` imports `google_sheets_script` only inside
  `register_write_tools()`, and only when the flag is on. A server running
  without writes never loads it. `google-auth` is imported inside the
  credential helper, at call time.
- **Raw `requests`, not `google-api-python-client`.** This is the same choice
  `merchant_center_sync.py` makes: one fewer heavy dependency, and request
  bodies map 1:1 onto the REST docs. Each HTTP call has a 60-second timeout.
  A non-2xx response raises a `RuntimeError` containing the status and the
  first 500 characters of the body.

## Tests

- `tests/test_server_write_tools.py`: the seven tools are absent unless
  `WAREHOUSE_MCP_ENABLE_WRITES` is exactly `1`. When registered they carry
  `readOnlyHint=False` / `destructiveHint=True`, they never alter the
  read-only tool set, and `register_write_tools()` reports its decision.
- `tests/test_google_sheets_script.py`: `script_push_content` writes nothing
  without `confirm` and preserves remote files it wasn't given.
  `script_deploy` always lists deployments first, updates in place by
  default, and creates a new deployment only with `allow_new_deployment`.
  Rename issues exactly one `updateSheetProperties` request and rejects an
  unknown title. Add-tabs skips existing tabs. The checkbox grid-range math
  is covered too.

Both files are hermetic, with HTTP mocked and no Google credentials needed.
