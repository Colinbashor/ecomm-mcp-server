# ecomm-mcp-server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server over a local
SQLite warehouse of e-commerce data — plus the connectors that fill it.

Pull advertising and order data from Google Ads, Meta Ads, Amazon (Ads + SP-API), Shopify,
and TikTok Shop into one SQLite file, then query it in natural language from any MCP client.
A further set of optional standalone scripts covers GA4, Google Merchant Center, Flexport,
Klaviyo, Purple Dot, and more — see "Additional connectors" below.

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

## Getting credentials

Every connector reads its own block in `.env.example`, which documents where to get each
value. Three platforms need a one-time interactive OAuth consent before you have a refresh
token to put in `.env` — a helper script drives that flow and saves the result for you:

```bash
python google_auth.py                          # Google Ads: opens a browser, saves the refresh token
python amazon_auth.py --url                     # Amazon Ads: prints a consent URL
python amazon_auth.py PASTE_THE_CODE_HERE       # then exchanges the code it redirects you to
python tiktok_auth.py PASTE_THE_CODE_HERE       # TikTok Shop: same pattern, code from Partner Center
```

Amazon retail orders (SP-API) and Shopify don't need one of these: SP-API gives you a refresh
token directly in Seller Central when you authorize your own private app, and Shopify uses a
non-interactive client-credentials grant that the connector performs itself. See the comments
above each block in `.env.example` for the exact steps.

## Syncing real data

```bash
python run_sync.py                      # last 7 days
python run_sync.py --days 30            # a wider window
python run_sync.py --start 2026-01-01 --end 2026-01-31
python run_sync.py --only google,meta   # just these connectors
```

Connectors whose environment variables are absent are skipped automatically.

## Additional connectors (standalone scripts)

`run_sync.py` only handles platforms that fit its uniform ads/orders shape. A
number of other integrations don't — they have their own table shapes (a daily
inventory snapshot, a campaign report, a product feed) — so each lives as its
own standalone script instead, runnable directly (`python <script>.py`) and
schedule-able independently (cron, Task Scheduler, etc.). Every one of them is
optional: skip a script entirely if you don't use that platform, and each
manages its own tables via its own `ensure_schema(conn)` — nothing else in the
project depends on them.

- **Google Analytics 4** (`ga4_sync.py`) — daily channel-level funnel metrics,
  item-level product views/sales, landing-page performance, and a new-vs-
  returning split per Google Ads campaign (useful for a rough "is this
  campaign acquiring new customers?" read — see the module docstring for the
  cookie-scoping caveat on that last one). Handles GA4's 100k-row response
  cap with daily chunking and pagination.
- **Google Ads detail + structure** (`google_ads_detail_sync.py`,
  `google_ads_structure_sync.py`) — everything the campaign-level connector
  (`run_sync.py --only google`) doesn't reach: customer search terms,
  keyword-level performance + Quality Score, per-product Shopping/Performance
  Max demand, paid-vs-organic query overlap, which conversion actions feed
  the reported Conversions number, device split, Performance Max search
  themes, and CURRENT-STATE snapshots of campaign/asset-group/listing-group
  configuration and conversion-action setup. The structure connector in
  particular is aimed at "this campaign looks funded but isn't serving" —
  a question spend/impression metrics alone usually can't answer. Reuses the
  same GOOGLE_ADS_* credentials — no new setup.
- **Google Merchant Center** (`merchant_center_sync.py`) — product feed
  performance (organic vs. paid, plus account-wide non-product-specific
  performance), feed eligibility/issues, price competitiveness, category
  best-sellers (including a "riser" signal for products gaining demand
  outside the usual top-N cut, and `--brand` for tracking specific brands —
  yours or a competitor's — regardless of rank), and competitive visibility.
  Requires a one-time `registerGcp` API call before anything works — see the
  module docstring; there's no Merchant Center UI for that step. Also see the
  module docstring for a lag-in-publishing gotcha on the visibility grain.
- **Flexport** (`flexport_sync.py`, `flexport_orders_sync.py`,
  `flexport_returns_sync.py`, `flexport_inbounds_sync.py`) — 3PL fulfillment:
  catalog + daily inventory snapshots, per-order shipping cost (via a
  resumable event-cursor crawl), customer returns, and inbound supplier
  shipments.
- **Klaviyo** (`klaviyo_sync.py`) — email/SMS campaign performance, flow
  (automation) performance, monthly audience/segment growth, and daily
  attributed revenue by channel and flow. Supports either a private API key
  or OAuth (`klaviyo_auth.py`, a PKCE helper matching the other `*_auth.py`
  scripts' shape) — useful if you'd rather not hand out a long-lived key.
- **Purple Dot** (`purple_dot_sync.py`) — pre-order/waitlist bookings and
  their eventual export into real storefront orders, plus daily waitlist
  inventory-allocation snapshots. Kept in its own tables rather than the
  shared `orders` table, since a booking and its export are different
  measures at different times.
- **Shopify customer dimension** (`shopify_customers_sync.py`) — current
  account state, tags, marketing-consent state, and custom metafields for
  every customer, plus a change-log of tag/state transitions over time.
  Reuses the Shopify connector's credentials but needs the additional
  `read_customers` scope. Deliberately never stores email, name, phone, or
  address — see the module docstring for why.
- **TikTok Shop extras** (`tiktok_videos_sync.py`, `tiktok_live_sync.py`,
  `tiktok_creators_sync.py`, `tiktok_analytics_sync.py`) — video performance,
  LIVE-shopping broadcast + product funnel, a handle ↔ display-name ↔ user-id
  creator/affiliate identity bridge (TikTok's video API and order API expose
  different halves of a creator's identity with no shared join key — the
  bridge closes that gap via the API plus an optional manual CSV import), and
  a true mutually-exclusive LIVE/VIDEO/PRODUCT_CARD sales-source split (a
  cleaner alternative to estimating "unattributed" sales by subtraction).
- **Amazon Seller extras** (`amazon_inventory_sync.py`,
  `amazon_returns_sync.py`, `amazon_rank_sync.py`, `amazon_fees_sync.py`,
  `amazon_economics_sync.py`, `amazon_traffic_sync.py`) — FBA inventory
  snapshots, customer returns, Best-Seller-Rank tracking, SP-API fee reports
  (previews, storage, reimbursements, promotions, fulfilled shipments/MCF),
  Data Kiosk SKU economics (actual fees + net proceeds, as opposed to the
  fee-preview estimate), and per-ASIN Sales & Traffic (sessions, page views,
  Buy Box %, units/sales, both weekly and monthly grain). All six reuse the
  SP-API credentials already set up for Amazon retail orders.
- **Amazon Ads detail** (`amazon_ads_detail_sync.py`) — the grains the
  campaign-level connector (`run_sync.py --only amazon_ads`) can't reach:
  per-ASIN advertised-product performance, keyword/target-level performance,
  and customer search-term performance (Sponsored Products). Reuses the same
  Ads API credentials — no new setup.
- **Amazon Brand Analytics** (`amazon_sqp_sync.py`, `amazon_ba_sync.py`,
  `warehouse/brand_analytics.py`) — for brand-registered sellers: Search
  Query Performance (query-level volume + your share of impressions/clicks/
  cart-adds/purchases vs. the whole market), Search Catalog Performance,
  Top Search Terms (filterable — see the module docstring, since the raw
  report is market-wide and can be huge), Market Basket Analysis (frequently
  co-purchased products), and Repeat Purchase Behavior. These reports queue
  for 15-25+ minutes on Amazon's side; `warehouse/brand_analytics.py` is the
  shared create/poll/download runner both scripts build on. An optional
  `brand_watchlist.yaml` (see that file) flags search terms containing your
  own or a competitor's brand name. Reuses the SP-API credentials above —
  requires brand registry, no new setup otherwise.
- **Amazon Voice of the Customer** (`voc_import.py`) — there's no API for
  this report, so it's a manual-drop CSV importer: download the export from
  Seller Central (Performance > Voice of the Customer), drop it in a local
  folder, and this loads per-ASIN/SKU customer-experience health and
  negative-experience rate. A useful template if you need to import any
  other Seller-Central-only report that has no API — header-driven column
  matching (spellings drift between export versions) and `--dry-run` to
  preview before writing.

## Notifications (optional)

`warehouse/notify.py` is a small standalone utility — not a connector, no
tables of its own — for pushing a plain-text or lightly-formatted message to
Slack, Google Chat, and/or email from any script in this repo (or your own).
Useful for a "sync finished, here's a summary" ping, or turning any of the
sync scripts above into an alert when something needs attention.

```python
from warehouse import notify
notify.send("*Sync complete*\n- 1,204 rows written\nFull report: https://...", dest="daily_sync")
```

Targets are configured per named `dest` in `.env` (`<DEST>_SLACK_WEBHOOK`,
`<DEST>_GCHAT_WEBHOOK`, `<DEST>_EMAIL_TO`), so different callers can point at
different channels/spaces/inboxes with no code change, and a `dest` with
nothing configured is silently skipped — `send()` never raises, so a
notification failure can't take down whatever pipeline called it. See the
module docstring for full setup (webhook creation, SMTP/app-password setup
for email).

## Backups (optional)

`backup_db.py` makes a same-disk rotating copy of `warehouse.db` using
SQLite's online backup API, which is safe to run against a live WAL database
— a concurrent sync can keep writing while the backup runs. Useful once your
warehouse holds history that's aged out of your source platforms' own API
retention and so can't simply be re-pulled.

```
python backup_db.py
```

Defaults to a `warehouse-backups/` folder next to (not inside) the project
directory, keeping the newest 3 copies; override with `WAREHOUSE_BACKUP_DIR`
/ `WAREHOUSE_BACKUP_KEEP` in `.env`. A backup that fails to verify (can't
open, no tables) is discarded rather than kept, so a corrupt copy never
silently replaces a good one. Wire it into your OS's scheduler (Task
Scheduler, cron, launchd) to run before your main sync job.

## Running the server

Over stdio, which is what Claude Desktop and most MCP clients expect:

```bash
python server.py
```

Over HTTP, to share one warehouse with several people:

```bash
python server.py --http --port 8787 --allow-host <hostname>
```

In HTTP mode, set `WAREHOUSE_MCP_TOKEN` — clients must then send it as a bearer token.
Host/Origin validation is on by default; `--allow-host` is repeatable, and the same list
can live in `WAREHOUSE_MCP_ALLOWED_HOSTS` (re-read without a restart). Use
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

`server.py` exposes six read-only tools — identical set and behavior whether
you're connected over stdio or `--http`. All annotate `readOnlyHint=True`, so
clients don't prompt for write-style approval.

| Tool | Signature | Returns |
|---|---|---|
| `list_tables` | `(table_pattern: str = None, include_columns: bool = True)` | table/view names, optionally with column lists |
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
  view built to protect against exactly that.
- **`run_sql`** — ad-hoc analysis. Only `SELECT`/`WITH` are accepted (checked
  up front, and enforced again by SQLite itself since the connection is
  opened `mode=ro`); anything else returns an error string instead of
  executing. Results are capped at 1000 rows — a truncation notice is
  appended if you hit it, so add a `LIMIT` or pre-aggregate. Each call also
  carries a wall-clock budget (`RUN_SQL_TIMEOUT_SEC` in `server.py`, 45s by
  default): a query that runs past it is cancelled with a clear error rather
  than tying up a server other people may be sharing. Over `--http`, any
  column listed in `_REMOTE_DENIED_COLUMNS` is unreadable — even through
  joins, aliases, subqueries, or quoting — while remaining fully queryable
  over local stdio; see [SHARING.md](SHARING.md).
- **`spend_summary`**, **`top_campaigns`**, **`sales_summary`** — the
  canonical rollups over `ad_metrics` and `orders`, the two tables
  `run_sync.py`'s connectors share a uniform shape for. Dates are inclusive
  `YYYY-MM-DD` strings. Anything these three don't answer, reach for
  `run_sql` — `list_tables` shows what else is available, including every
  table an "Additional connectors" script above adds.
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
| `tests/test_server_security.py` | `HostGuard`'s Host/Origin accept/reject rules, live policy refresh, the remote SQL column authorizer, the `run_sql` wall-clock timeout, and legacy-token-path log scrubbing |
| `tests/test_list_tables.py` | `list_tables` surfaces SQL views alongside tables, in both column-listing and name-only mode, and `table_pattern` matches views too |
| `tests/test_run_sync.py` | One connector failing doesn't abort the rest of a sync run |
| `tests/test_shopify_connector.py` | Network-blip retry/backoff, honoring `Retry-After` on a 429, GraphQL throttling, and that a hard error (5xx, a real GraphQL error) fails immediately instead of retrying |
| `tests/test_notify.py` | Chat-markdown/HTML rendering, per-`dest` target resolution, and that a missing/unconfigured/failing target is skipped rather than raised |

Every standalone script under "Additional connectors" above has its own
`tests/test_<script>.py` — schema creation, row-shaping, and its own API's
particular gotchas, all hermetic (mocked HTTP, no network).

## Configuration

`.env.example` lists every credential. A few non-obvious knobs:

| Variable | Purpose | Default |
|---|---|---|
| `WAREHOUSE_DB` | Path to the SQLite file | `warehouse.db` beside the code |
| `WAREHOUSE_MCP_TOKEN` | Bearer token required in `--http` mode | unset |
| `WAREHOUSE_MCP_ALLOWED_HOSTS` | Comma-separated Host/Origin allowlist for `--http` | unset |
| `SHOPIFY_CAPTURE_CUSTOMER` | Whether to store Shopify customer ids | auto-probes scope |
| `AMAZON_ADS_REPORT_TIMEOUT_MIN` | Amazon report poll timeout, minutes | `60` |
| `DATAKIOSK_TIMEOUT_MIN` | Amazon Data Kiosk query poll timeout, minutes (`amazon_economics_sync.py`) | `150` |
| `TIKTOK_LIVE_OWN_ACCOUNT_TYPE` | Which `tiktok_shop_lives.account_type` counts as your own broadcasts | `OFFICIAL_ACCOUNTS` |
| `TIKTOK_SHOP_TIMEZONE` | IANA timezone for bucketing LIVE broadcasts into calendar days | `UTC` |
| `CERT_ORG_NAME` | Organization field on the self-signed cert `make_cert.py` generates | `ecommerce-warehouse MCP` |

Set `WAREHOUSE_DB` the same way for both `run_sync.py` and `server.py`. If they disagree,
the sync fills one database while the server reads an empty one.

## License

Apache 2.0 — see [LICENSE](LICENSE).
