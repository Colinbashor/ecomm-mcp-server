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

`run_sync.py` is the only thing that writes. `server.py` only ever reads.

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
wiring up a single real API.

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

Platforms whose environment variables are absent are skipped automatically. Valid `--only`
names: `google`, `meta`, `amazon` (Ads), `amazon_orders` (SP-API retail orders), `shopify`,
`tiktok`.

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
| Google Ads | [docs/google-ads.md](docs/google-ads.md) | campaign spend/clicks/conversions | search terms, keywords, Shopping/PMax demand, campaign structure |
| Google Analytics 4 | [docs/ga4.md](docs/ga4.md) | — | funnel metrics, product performance, landing pages, new-vs-returning |
| Google Merchant Center | [docs/merchant-center.md](docs/merchant-center.md) | — | feed performance, price competitiveness, best-sellers, visibility |
| Google Search Console | [docs/search-console.md](docs/search-console.md) | — | organic search clicks/impressions/position by query and landing page |
| Meta Ads | [docs/meta-ads.md](docs/meta-ads.md) | campaign spend/clicks/conversions | ad/creative/video-level detail |
| Amazon Advertising | [docs/amazon-ads.md](docs/amazon-ads.md) | campaign spend/clicks/conversions | per-ASIN, keyword/target, search-term performance |
| Amazon Seller (SP-API) | [docs/amazon-seller.md](docs/amazon-seller.md) | retail orders | inventory, AWD (bulk-storage) inventory, returns, rank, fees, economics, traffic, Voice of the Customer |
| Amazon Brand Analytics | [docs/amazon-brand-analytics.md](docs/amazon-brand-analytics.md) | — | search query performance, market basket, repeat purchase, monthly search terms by category |
| Shopify | [docs/shopify.md](docs/shopify.md) | orders | customer dimension (tags, consent, metafields) |
| TikTok Shop | [docs/tiktok-shop.md](docs/tiktok-shop.md) | orders | videos, LIVE-shopping, creator identity, sales-source split, settlement/fee data |
| Klaviyo | [docs/klaviyo.md](docs/klaviyo.md) | — | campaign/flow performance, audience growth, attributed revenue |
| Flexport | [docs/flexport.md](docs/flexport.md) | — | catalog/inventory, order shipping cost, returns, inbounds |
| Purple Dot | [docs/purple-dot.md](docs/purple-dot.md) | — | pre-order/waitlist bookings, waitlist inventory |
| Reacher (TikTok Shop affiliate platform) | [docs/reacher.md](docs/reacher.md) | — | creator/sample/GMV Max ad-spend history, affiliate funnel metrics |
| Algolia (on-site search/browse) | [docs/algolia.md](docs/algolia.md) | — | collection-grid placement, search/browse engagement |

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

In HTTP mode, set `WAREHOUSE_MCP_TOKEN` — clients must then send it as a bearer token.
Host/Origin validation is on by default; `--allow-host` is repeatable, and the same list
can also come from `WAREHOUSE_MCP_ALLOWED_HOSTS` (comma- or semicolon-separated) or from
an `allowed_hosts.txt` file (one entry per line) beside `server.py` — all three are
re-read on every policy refresh, so adding a name needs no restart. Use
`--check-host <value>` to print the accept/reject verdict for a Host and exit.
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

`server.py` exposes six read-only tools — the same six over stdio or `--http`,
though `run_sql`'s column redaction only kicks in over HTTP (see below). All
annotate `readOnlyHint=True`/`destructiveHint=False`/`idempotentHint=True`, so
clients don't prompt for write-style approval.

| Tool | Signature | Returns |
|---|---|---|
| `list_tables` | `(table_pattern: str \| None = None, include_columns: bool = True)` | table/view names, optionally with column lists |
| `run_sql` | `(query: str)` | up to 1000 rows as JSON |
| `spend_summary` | `(start_date, end_date)` | spend/revenue/clicks/impressions/conversions/ROAS per platform |
| `top_campaigns` | `(start_date, end_date, limit: int = 15)` | top campaigns by spend across platforms |
| `sales_summary` | `(start_date, end_date)` | order count, units, and sales per platform |
| `last_sync_status` | `()` | most recent sync run per platform, for checking data freshness |

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
  up front, and enforced again by SQLite itself since the connection is
  opened `mode=ro`); anything else returns an error string instead of
  executing. Results are capped at 1000 rows — a truncation notice is
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
  `YYYY-MM-DD` strings. Anything these three don't answer, reach for
  `run_sql` — `list_tables` shows what else is available, including every
  table a platform page under [Connectors, by platform](#connectors-by-platform) adds.
- **`last_sync_status`** — one row per platform from `sync_log`: last run
  time, status, rows written, and any error message. The first thing to
  check if a summary tool looks stale or empty.

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
| `tests/test_list_tables.py` | `list_tables` surfaces SQL views alongside tables, in both column-listing and name-only mode, and `table_pattern` matches views too |
| `tests/test_run_sync.py` | One connector failing doesn't abort the rest of a sync run |
| `tests/test_db_journal_mode.py` | A fresh database comes up in WAL mode (not SQLite's default `delete` journal) and `init_db()` stays idempotent |
| `tests/test_shopify_connector.py` | Network-blip retry/backoff, honoring `Retry-After` on a 429, GraphQL throttling, and that a hard error (5xx, a real GraphQL error) fails immediately instead of retrying |
| `tests/test_notify.py` | Chat-markdown/HTML rendering, per-`dest` target resolution, and that a missing/unconfigured/failing target is skipped rather than raised |

Every standalone script under [Connectors, by platform](#connectors-by-platform) above has
its own `tests/test_<script>.py` — schema creation, row-shaping, and its own API's particular
gotchas, all hermetic (mocked HTTP, no network). Each platform's doc page links its own tests.

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
| `CERT_ORG_NAME` | Organization field on the self-signed cert `make_cert.py` generates | `ecommerce-warehouse MCP` |

Set `WAREHOUSE_DB` the same way for both `run_sync.py` and `server.py`. If they disagree,
the sync fills one database while the server reads an empty one.

## License

Apache 2.0 — see [LICENSE](LICENSE).
