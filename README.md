# ecomm-mcp-server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server over a local
SQLite warehouse of e-commerce data — plus the connectors that fill it.

Pull advertising and order data from Google Ads, Meta Ads, Amazon (Ads + SP-API), Shopify,
and TikTok Shop into one SQLite file, then query it in natural language from any MCP client.
A further set of optional standalone scripts covers GA4, Google Merchant Center, Flexport,
Klaviyo, Purple Dot, Reacher, and more — see [Connectors, by platform](#connectors-by-platform) below.

## How it fits together

```
run_sync.py  ──>  warehouse.db  ──>  server.py  ──>  MCP client
(connectors)      (SQLite)          (read-only)
```

The sync side writes: `run_sync.py` for the core `ad_metrics` / `orders` tables, and each
standalone `<platform>_sync.py` script for its own tables. `server.py` only ever reads —
every query goes through a `mode=ro` SQLite connection. (On startup it does call
`db.init_db()`, which creates any missing core tables and switches the file to WAL mode,
so pointing it at a fresh path yields an empty-but-valid warehouse rather than an error.)

## Requirements

- Python 3.11 or newer
- Credentials for whichever platforms you want to sync — every connector is optional and
  independent, so you can start with one and add others later

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in the platforms you use
```

`mcp[cli]` is pinned to `>=1.29,<2` on purpose. SDK 2.0.0 renames `FastMCP` to
`MCPServer`, so an unpinned install breaks the import in `server.py`.

## Try it without any credentials

```bash
python run_sync.py --sample
```

This loads synthetic rows so you can verify the schema and exercise the MCP tools before
wiring up a single real API: 14 days (ending today) of random `ad_metrics` rows for
`google`, `meta` and `amazon` (three demo campaigns each, `account_id` `DEMO`), a handful of
random `tiktok` `orders` per day, and one `sync_log` row with platform `sample`. Rows are
upserted on their primary keys, so re-running it overwrites the same keys with new random
values rather than piling up duplicates. `--sample` ignores every other flag.

## Syncing real data

The five **core** platforms below share a uniform ads/orders shape and run through one
CLI. Everything else — the platforms with their own table shapes — is a standalone script;
see [Connectors, by platform](#connectors-by-platform).

```bash
python run_sync.py                          # last 7 days, all configured platforms
python run_sync.py --days 30                # a wider window
python run_sync.py --start 2026-01-01 --end 2026-01-31
python run_sync.py --only google,meta       # just these platforms
```

| Flag | Meaning | Default |
|---|---|---|
| `--days N` | Window length when `--start` is omitted: start = end − N days | `7` |
| `--start YYYY-MM-DD` | First day of the window | end − `--days` |
| `--end YYYY-MM-DD` | Last day of the window | today |
| `--only a,b` | Comma-separated subset of connectors (whitespace around names is ignored) | all six |
| `--sample` | Load synthetic demo data instead of syncing (see above) | off |

Valid `--only` names — six connectors across the five core platforms: `google`, `meta`,
`amazon` (Ads), `amazon_orders` (SP-API retail orders), `shopify`, `tiktok`. An unknown name
aborts the run before anything is synced.

How a run behaves:

- **Skip, don't fail, on missing credentials.** A connector is attempted only when its gating
  env var is set (and non-empty) — `GOOGLE_ADS_DEVELOPER_TOKEN`, `META_ACCESS_TOKEN`,
  `AMAZON_ADS_REFRESH_TOKEN`, `SPAPI_REFRESH_TOKEN`, `SHOPIFY_CLIENT_SECRET` *or*
  `SHOPIFY_ADMIN_TOKEN`, `TIKTOK_ACCESS_TOKEN` respectively. Otherwise it prints `SKIPPED`
  and writes nothing, not even a `sync_log` row.
- **Upsert.** Ads connectors write `ad_metrics` (primary key
  `platform, account_id, campaign_id, date`); order connectors write `orders` (primary key
  `platform, order_id, sku`). Both use `INSERT OR REPLACE`, so re-syncing an overlapping
  window refreshes those rows in place.
- **One failure doesn't stop the rest.** Each connector's exception is caught, logged to
  `sync_log` with status `error` and the exception text as `message`, and the loop moves on.
  Successes log status `ok` with the row count.
- **Exit code.** If any connector failed, the process exits non-zero with
  `Connector failures: <names>` — so a scheduler can alert on it.

`.env.example` documents where to get every credential. Three platforms need a one-time
interactive OAuth consent before you have a refresh token — a helper script drives that flow
and saves the result for you:

```bash
python google_auth.py                          # Google Ads: opens a browser, saves the refresh token
python amazon_auth.py --url                     # Amazon Ads: prints a consent URL
python amazon_auth.py PASTE_THE_CODE_HERE       # then exchanges the code it redirects you to
python tiktok_auth.py PASTE_THE_CODE_HERE       # TikTok Shop: same pattern, code from Partner Center
```

Amazon retail orders (SP-API) and Shopify don't need one of these: SP-API gives you a refresh
token directly in Seller Central when you authorize your own private app, and Shopify uses a
non-interactive client-credentials grant that the connector performs itself. Full setup steps
for each core platform, including exact env vars, live on its own page — see the table below.

## Connectors, by platform

`run_sync.py` only handles the five core platforms above. A number of other
integrations don't fit its uniform shape — a daily inventory snapshot, a
campaign report, a product feed — so each lives as its own standalone script,
runnable directly (`python <script>.py`) and schedule-able independently
(cron, Task Scheduler, etc.). Every one is optional: skip a page entirely if
you don't use that platform.

Each page below is self-contained: setup/credentials, usage, the tables it
writes, and its test coverage.

| Platform | Doc | Core connector | Standalone extras |
|---|---|---|---|
| Google Ads | [docs/google-ads.md](docs/google-ads.md) | campaign spend/clicks/conversions | search terms, keywords, Shopping/PMax demand, campaign structure; **write-capable** `google_ads_mutate.py` for pausing/creating/editing live campaigns |
| Google Analytics 4 | [docs/ga4.md](docs/ga4.md) | — | funnel metrics, product performance, landing pages (per-URL and bucketed), Meta paid/organic traffic split, new-vs-returning |
| Google Merchant Center | [docs/merchant-center.md](docs/merchant-center.md) | — | feed performance, price competitiveness, best-sellers, visibility |
| Google Search Console | [docs/search-console.md](docs/search-console.md) | — | organic search clicks/impressions/position by query and landing page |
| Meta Ads | [docs/meta-ads.md](docs/meta-ads.md) | campaign spend/clicks/conversions | ad/creative/video-level detail, ad-set funnel stage (`meta_adset_funnel`, derived from targeting); **write-capable** `meta_ads_mutate.py` for pausing/resuming campaigns, ad sets and ads, ad-set budgets, ad-set copies and guarded renames (single or from a CSV plan) |
| Amazon Advertising | [docs/amazon-ads.md](docs/amazon-ads.md) | campaign spend/clicks/conversions | per-ASIN, keyword/target, search-term performance |
| Amazon Seller (SP-API) | [docs/amazon-seller.md](docs/amazon-seller.md) | retail orders | inventory, AWD (bulk-storage) inventory, returns, rank, fees, economics, traffic, Voice of the Customer, listing-quality diagnostics |
| Amazon Brand Analytics | [docs/amazon-brand-analytics.md](docs/amazon-brand-analytics.md) | — | search query performance, market basket, repeat purchase, monthly search terms by category |
| Shopify | [docs/shopify.md](docs/shopify.md) | orders | customer dimension (tags, consent, metafields) |
| TikTok Shop | [docs/tiktok-shop.md](docs/tiktok-shop.md) | orders (with per-line seller/platform discounts) | videos, LIVE-shopping, creator identity, sales-source split, settlement/fee data + net-sales view, listing-quality diagnostics |
| Klaviyo | [docs/klaviyo.md](docs/klaviyo.md) | — | email campaign + per-channel flow performance, segment growth, daily attributed revenue |
| Flexport | [docs/flexport.md](docs/flexport.md) | — | catalog/inventory, order shipping cost, returns, inbounds |
| Purple Dot | [docs/purple-dot.md](docs/purple-dot.md) | — | pre-order/waitlist bookings, waitlist inventory |
| Reacher (TikTok Shop affiliate platform) | [docs/reacher.md](docs/reacher.md) | — | creator/sample/GMV Max ad-spend history, affiliate funnel metrics; **write-capable** `reacher_sample_limits.py` for capping/clearing per-product sample auto-approval |
| Algolia (on-site search/browse) | [docs/algolia.md](docs/algolia.md) | — | collection-grid placement, search/browse engagement |
| Factory production tracking | [docs/factory-production.md](docs/factory-production.md) | — | manual-drop vendor spreadsheets, header-matched into one table |
| Google Sheets / Apps Script (opt-in MCP **write** tools, not a connector) | [docs/google-sheets.md](docs/google-sheets.md) | — | add tabs, write values, checkboxes; create/push/deploy a bound Apps Script web app — registered only with `WAREHOUSE_MCP_ENABLE_WRITES=1` |

Two more standalone utilities aren't connectors at all — no credentials, no
data pulled from anywhere: pushing a "sync finished" notification to
Slack/Chat/email, and rotating local backups of `warehouse.db`. See
[docs/operations.md](docs/operations.md).

## Running the server

Over stdio, which is what Claude Desktop and most MCP clients expect:

```bash
python server.py
```

Over HTTP, on this machine only (the default):

```bash
python server.py --http --port 8787
```

Over HTTP, to share one warehouse with several people on your network:

```bash
python server.py --http --host 0.0.0.0 --port 8787 --allow-host <hostname>
```

**`--http` binds `127.0.0.1` unless you pass `--host`.** This server answers
questions about your entire business, so reaching the network is a decision you
make on purpose rather than a default you inherit by following a README on
untrusted wifi. The server logs a warning when you widen it, and a second one if
you widen it without TLS — the bearer token travels in a header, so on a
cleartext bind anyone on the segment can read and replay it.

In HTTP mode `WAREHOUSE_MCP_TOKEN` is **required** — the server exits at startup without it —
and every request must send `Authorization: Bearer <token>` (compared in constant time);
anything else gets a `401`. The MCP endpoint is `/mcp`, served stateless (no per-client
session to lose across restarts). If `certs/warehouse-mcp.crt` and `certs/warehouse-mcp.key`
both exist at startup (see `make_cert.py` in [SHARING.md](SHARING.md)) it serves HTTPS,
otherwise plain HTTP; the startup line prints which.

Host/Origin validation is on by default and runs *after* authentication, so anonymous
probes only ever see `401`. A rejected `Host` gets `421`, a rejected `Origin` `403`, each
with a JSON body naming the reason. `--allow-host` is repeatable, and the same list can
also come from `WAREHOUSE_MCP_ALLOWED_HOSTS` (comma- or semicolon-separated) or from an
`allowed_hosts.txt` file (one entry per line, `#` comments allowed) beside `server.py`.
Entries may be bare host names/IPs or full `http(s)://` origins; there is no wildcard, and
`*` or junk entries are dropped with a warning. `allowed_hosts.txt` is the live-edit path:
a changed file is picked up on the very next request, additions and removals alike, with no
restart. (The env var is read from the process environment, which `server.py` populates
from `.env` at startup, so treat changes to it as needing a restart.) Use
`--check-host <value>` to print the accept/reject verdict for a Host value and the current
name policy, then exit `0` (accept) or `1` (reject/malformed) — an IP literal always shows
as `no-local-addr` there, because IPs are judged against the address a live connection
arrives on.
`--allow-any-host` disables Host/Origin validation entirely — a debug escape hatch, not
something to leave on; the server re-warns in the log every 6 hours while it's set.
`--allow-legacy-token-path` is a temporary migration switch: it makes the server also
accept the older `/<token>/mcp` URL-embedded-token style alongside the current
`Authorization: Bearer` header, so you can roll the header-based config out to coworkers
one at a time instead of breaking everyone's config in the same instant. Drop the flag
once every teammate's config has switched — see [SHARING.md](SHARING.md#migrating-off-the-legacy-token-in-url-scheme)
for the full rotation story.

See [SHARING.md](SHARING.md) for the full walkthrough: generating a self-signed
cert with `make_cert.py` so Claude's connector UI accepts the URL, keeping the
server running across reboots with `serve_mcp.bat` (Windows), and the
`mcp-remote` config snippet each teammate adds to their own Claude Desktop.

## MCP tools

`server.py` exposes six read-only tools (plus an opt-in write set, [below](#optional-write-tools-google-sheets--apps-script)) (MCP server name `ecommerce-warehouse`) — the
same six over stdio or `--http`, though `run_sql`'s column redaction only kicks in over
HTTP (see below). All annotate `readOnlyHint=True`/`destructiveHint=False`/
`idempotentHint=True`/`openWorldHint=False`, so clients don't prompt for write-style
approval. Every tool returns a JSON string (a list of row objects, except `list_tables`).

| Tool | Title | Signature | Returns |
|---|---|---|---|
| `list_tables` | List warehouse tables | `(table_pattern: str \| None = None, include_columns: bool = True)` | `{name: [columns]}`, or a name list when `include_columns=false` |
| `run_sql` | Run read-only SQL | `(query: str)` | up to 1000 rows as JSON |
| `spend_summary` | Ad spend by platform | `(start_date: str, end_date: str)` | per platform from `ad_metrics`: `spend`, `revenue`, `clicks`, `impressions`, `conversions`, `roas` (= revenue / spend, `NULL` at zero spend), ordered by spend |
| `top_campaigns` | Top campaigns by spend | `(start_date: str, end_date: str, limit: int = 15)` | `platform`, `campaign_name`, `spend`, `revenue`, `clicks`, grouped by platform + `campaign_id`, highest spend first |
| `sales_summary` | Sales by platform | `(start_date: str, end_date: str)` | per platform from `orders`: `orders` (distinct `order_id`), `units` (sum of `quantity`), `sales` (sum of `total`), filtered on `order_date` |
| `last_sync_status` | Data freshness by platform | `()` | latest `sync_log` row per platform: `platform`, `last_run` (its `finished_at`), `status`, `rows_written`, `message` |

- **`list_tables`** — the schema explorer. Narrow it with `table_pattern`
  (substring match, case-insensitive — `"shopify"` finds every `shopify_*`
  table; include a literal `%` to write a raw SQL `LIKE` pattern instead) and
  drop `include_columns` to `false` for a cheap name-only catalogue. On a
  mature warehouse the full dump costs several thousand tokens, so prefer
  narrowing over calling it bare. It lists **views as well as tables** — if
  your schema defines a SQL `VIEW` (a dedup view over a source table that can
  carry several disagreeing rows per key, say, exposed as the safe join
  target instead of the raw table), it needs to be just as discoverable here
  or a caller exploring the schema will join the raw table instead of the
  view built to protect against exactly that. A `table_pattern` that matches
  nothing returns a hint to call `list_tables()` bare rather than an error or
  an empty list.
- **`run_sql`** — ad-hoc analysis. Only `SELECT`/`WITH` are accepted (checked
  up front after trimming whitespace and a trailing `;`, and enforced again by
  SQLite itself since the connection is opened `mode=ro`); anything else
  returns an error string instead of executing, and a failing query returns
  `SQL error: <sqlite message>` rather than raising. Results are capped at 1000 rows — a truncation notice is
  appended if you hit it, so add a `LIMIT` or pre-aggregate. Each call also
  carries a wall-clock budget (`RUN_SQL_TIMEOUT_SEC` in `server.py`, 45s by
  default): a query that runs past it is cancelled with a clear error rather
  than tying up a server other people may be sharing. Over `--http`, any
  column listed in `_REMOTE_DENIED_COLUMNS` (in `server.py`) is unreadable —
  even through joins, aliases, subqueries, or quoting — while remaining fully
  queryable over local stdio; see [SHARING.md](SHARING.md). That set ships
  **empty** — no redaction happens until an operator edits `server.py` to
  list their own PII-bearing columns (email, phone, name, free-text notes,
  tracking numbers), so don't assume PII is protected out of the box.
- **`spend_summary`**, **`top_campaigns`**, **`sales_summary`** — the
  canonical rollups over `ad_metrics` and `orders`, the two tables
  `run_sync.py`'s connectors share a uniform shape for. Dates are inclusive
  `YYYY-MM-DD` strings (compared as text with `BETWEEN`, so pass exactly that
format). The amounts are plain `SUM`s with no currency conversion — see
the data rules in [AGENTS.md](AGENTS.md) before comparing ad-platform
revenue across rows. Anything these three don't answer, reach for
  `run_sql` — `list_tables` shows what else is available, including every
  table a platform page under [Connectors, by platform](#connectors-by-platform) adds.
- **`last_sync_status`** — one row per platform from `sync_log`: last run
  time, status, rows written, and any error message. The first thing to
  check if a summary tool looks stale or empty.

### Optional write tools (Google Sheets / Apps Script)

Off by default. With `WAREHOUSE_MCP_ENABLE_WRITES=1` in the environment at
startup (only the exact value `1` counts), `server.py` also registers seven
tools that let an assistant build a small internal tool on a Google Sheet —
tabs, seed values, checkboxes, and a bound Apps Script web app — without
anyone pasting code into the script editor. They come from
`google_sheets_script.py`, which is imported only when the flag is on, and
the startup log says which way the decision went.

| Tool | Does | Safety net |
|---|---|---|
| `sheets_add_tabs` | add tabs, each with an optional header row | idempotent: an existing tab is left untouched |
| `sheets_rename_tab` | rename a tab | errors if the old title doesn't exist |
| `sheets_write_values` | write a block of values (`RAW`, never parsed as formulas) | commits directly |
| `sheets_set_checkboxes` | turn a range into checkboxes | commits directly |
| `script_create_project` | create an Apps Script project bound to a Sheet | — |
| `script_push_content` | push `.gs` / `.html` / manifest files | returns a diff and writes nothing unless `confirm=true`; keeps remote files it wasn't given (e.g. the manifest) |
| `script_deploy` | publish a web app version | always lists deployments first and **updates the existing one in place** (same `/exec` URL); a new deployment (new URL) needs `allow_new_deployment=true` |

All seven carry `readOnlyHint=False` / `destructiveHint=True`, so a
well-behaved client asks before each call. They never touch the warehouse
database: `run_sql` stays read-only at the SQLite level either way. Setup:
put `GOOGLE_SHEETS_CLIENT_ID` / `GOOGLE_SHEETS_CLIENT_SECRET` in `.env`, run
`python google_sheets_auth.py` once (needs `google-auth-oauthlib`, same as
`google_auth.py`), enable the Sheets and Apps Script APIs in the Cloud
project, and switch the Apps Script API on for that user at
<https://script.google.com/home/usersettings>. The OAuth scopes are
`spreadsheets`, `script.projects`, `script.deployments` and `drive.file`
(deliberately not full Drive). The same operations are available as a CLI:
`python google_sheets_script.py --help`. Full reference — each tool's
parameters and return shape, the A1-range rules, the push → deploy order, and
the gotchas — is in [docs/google-sheets.md](docs/google-sheets.md).

> **Do not enable this on a shared `--http` server casually.** The flag is
> server-wide, not per client: every teammate holding the bearer token gets
> the ability to edit any Sheet the `GOOGLE_SHEETS_*` user can edit and to
> redeploy its public web apps. Run a separate stdio-only instance with the
> flag set if only you need it. See [SHARING.md](SHARING.md#notes).

## Claude Desktop

Copy `claude_desktop_config.example.json` into your Claude Desktop config and replace
`/ABSOLUTE/PATH/TO/ecomm-mcp-server` with the real path — Claude Desktop requires
absolute paths and does not expand `~`.

The interpreter path differs by platform:

- macOS / Linux — `.venv/bin/python`
- Windows — `.venv\Scripts\python.exe`

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Hermetic — no network access, no `warehouse.db` required — and runs in a couple of seconds.

| File | Covers |
|---|---|
| `tests/test_server_security.py` | `HostGuard`'s Host/Origin accept/reject rules, live policy refresh, the remote SQL column authorizer, the `run_sql` wall-clock timeout, legacy-token-path log scrubbing, the `--http` loopback-by-default bind and its warnings, and a source grep guarding against a hardcoded wildcard bind |
| `tests/test_server_write_tools.py` | The Sheets/Apps Script write tools are absent unless `WAREHOUSE_MCP_ENABLE_WRITES` is exactly `1`, carry `readOnlyHint=False`/`destructiveHint=True` when registered, and never alter the read-only tool set |
| `tests/test_google_sheets_script.py` | `script_push_content` writes nothing without `confirm` and preserves remote files it wasn't given; `script_deploy` always lists deployments first, updates in place by default, and creates a new deployment only with `allow_new_deployment`; tab add/rename and checkbox grid ranges |
| `tests/test_list_tables.py` | `list_tables` surfaces SQL views alongside tables, in both column-listing and name-only mode, and `table_pattern` matches views too |
| `tests/test_run_sync.py` | A connector that raises is returned in `run()`'s failure list (which drives the non-zero exit) and logged to `sync_log` as `error` |
| `tests/test_db_journal_mode.py` | A fresh database comes up in WAL mode (not SQLite's default `delete` journal) and `init_db()` stays idempotent |
| `tests/test_shopify_connector.py` | Network-blip retry/backoff, honoring `Retry-After` on a 429, GraphQL throttling, and that a hard error (5xx, a real GraphQL error) fails immediately instead of retrying |
| `tests/test_google_ads_connector.py` | `search_impression_share` and its lost-share siblings stay `NULL` only on non-auction campaign types, keep a real `0.0` on Search/Shopping, and a Google-side `0.0/0.0/0.0` placeholder response is detected and nulled rather than stored as a fabricated zero |
| `tests/test_notify.py` | Chat-markdown/HTML rendering, per-`dest` target resolution, that a missing/unconfigured/failing target is skipped rather than raised, that `send(dest=...)`'s email target calls `send_email()` (so it shares the same retry behavior rather than a separate weaker path), `_smtp_config()` reading `SMTP_*` from the environment at call time, and `send_email()`'s own retry-then-report-failure behavior for a standalone HTML report send |

Every standalone sync/import script under [Connectors, by platform](#connectors-by-platform)
above has its own `tests/test_<script>.py` — schema creation, row-shaping, and its own API's
particular gotchas, all hermetic (mocked HTTP, no network). Each platform's doc page links its
own tests. A few files don't follow the one-script naming: `tests/test_brand_analytics.py`
covers the shared `warehouse/brand_analytics.py` report runner, and
`tests/test_meta_ads_landing_page_views.py` covers the `landing_page_views` column in the core
Meta connector. The one-time OAuth helpers (`google_auth.py`, `google_sheets_auth.py`,
`amazon_auth.py`, `tiktok_auth.py`) and `make_cert.py` have no tests; `klaviyo_auth.py` does
(`tests/test_klaviyo_auth.py`).

## Configuration

`.env.example` lists every credential, grouped by platform, with setup notes in the comments
above each block. Platform-specific variables and their per-page docs are linked in
[Connectors, by platform](#connectors-by-platform). A few cross-cutting knobs that aren't
tied to any one platform:

| Variable | Purpose | Default |
|---|---|---|
| `WAREHOUSE_DB` | Path to the SQLite file | `warehouse.db` beside the code |
| `WAREHOUSE_MCP_TOKEN` | Bearer token required in `--http` mode | unset |
| `WAREHOUSE_MCP_ALLOWED_HOSTS` | Comma- or semicolon-separated Host/Origin allowlist for `--http` (see also `allowed_hosts.txt`) | unset |
| `WAREHOUSE_MCP_ENABLE_WRITES` | Exactly `1` registers the optional Google Sheets / Apps Script write tools (see [Optional write tools](#optional-write-tools-google-sheets--apps-script)); read once at startup | unset (off) |
| `CERT_ORG_NAME` | Organization field on the self-signed cert `make_cert.py` generates. `make_cert.py` does **not** load `.env`, so set this in the shell environment when you run it | `ecommerce-warehouse MCP` |

Set `WAREHOUSE_DB` the same way for both `run_sync.py` and `server.py`. If they disagree,
the sync fills one database while the server reads an empty one.

## License

Apache 2.0 — see [LICENSE](LICENSE).
