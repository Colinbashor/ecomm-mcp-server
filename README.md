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
  item-level product views/sales, and landing-page performance. Handles GA4's
  100k-row response cap with daily chunking and pagination.
- **Google Merchant Center** (`merchant_center_sync.py`) — product feed
  performance (organic vs. paid), feed eligibility/issues, price
  competitiveness, category best-sellers, and competitive visibility. Requires
  a one-time `registerGcp` API call before anything works — see the module
  docstring; there's no Merchant Center UI for that step.
- **Flexport** (`flexport_sync.py`, `flexport_orders_sync.py`,
  `flexport_returns_sync.py`, `flexport_inbounds_sync.py`) — 3PL fulfillment:
  catalog + daily inventory snapshots, per-order shipping cost (via a
  resumable event-cursor crawl), customer returns, and inbound supplier
  shipments.
- **Klaviyo** (`klaviyo_sync.py`) — email/SMS campaign performance, flow
  (automation) performance, monthly audience/segment growth, and daily
  attributed revenue by channel and flow.
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
  `tiktok_creators_sync.py`) — video performance, LIVE-shopping broadcast +
  product funnel, and a handle ↔ display-name ↔ user-id creator/affiliate
  identity bridge (TikTok's video API and order API expose different halves
  of a creator's identity with no shared join key — the bridge closes that
  gap via the API plus an optional manual CSV import).
- **Amazon Seller extras** (`amazon_inventory_sync.py`,
  `amazon_returns_sync.py`, `amazon_rank_sync.py`, `amazon_fees_sync.py`,
  `amazon_economics_sync.py`) — FBA inventory snapshots, customer returns,
  Best-Seller-Rank tracking, SP-API fee reports (previews, storage,
  reimbursements, promotions, fulfilled shipments/MCF), and Data Kiosk SKU
  economics (actual fees + net proceeds, as opposed to the fee-preview
  estimate). All five reuse the SP-API credentials already set up for Amazon
  retail orders.

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

See [SHARING.md](SHARING.md) for the full walkthrough: generating a self-signed
cert with `make_cert.py` so Claude's connector UI accepts the URL, keeping the
server running across reboots with `serve_mcp.bat` (Windows), and the
`mcp-remote` config snippet each teammate adds to their own Claude Desktop.

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
| `tests/test_run_sync.py` | One connector failing doesn't abort the rest of a sync run |
| `tests/test_shopify_connector.py` | Network-blip retry/backoff, honoring `Retry-After` on a 429, GraphQL throttling, and that a hard error (5xx, a real GraphQL error) fails immediately instead of retrying |

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
