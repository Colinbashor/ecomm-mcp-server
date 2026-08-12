# ecomm-mcp-server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server over a local
SQLite warehouse of e-commerce data — plus the connectors that fill it.

Pull advertising and order data from Google Ads, Meta Ads, Amazon (Ads + SP-API), Shopify,
and TikTok Shop into one SQLite file, then query it in natural language from any MCP client.

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
Covers the two things a well-meaning edit is most likely to break: the Host/Origin validation
rules in `server.py` and the remote SQL column authorizer.

## Configuration

`.env.example` lists every credential. A few non-obvious knobs:

| Variable | Purpose | Default |
|---|---|---|
| `WAREHOUSE_DB` | Path to the SQLite file | `warehouse.db` beside the code |
| `WAREHOUSE_MCP_TOKEN` | Bearer token required in `--http` mode | unset |
| `WAREHOUSE_MCP_ALLOWED_HOSTS` | Comma-separated Host/Origin allowlist for `--http` | unset |
| `SHOPIFY_CAPTURE_CUSTOMER` | Whether to store Shopify customer ids | auto-probes scope |
| `AMAZON_ADS_REPORT_TIMEOUT_MIN` | Amazon report poll timeout, minutes | `60` |

Set `WAREHOUSE_DB` the same way for both `run_sync.py` and `server.py`. If they disagree,
the sync fills one database while the server reads an empty one.

## License

Apache 2.0 — see [LICENSE](LICENSE).
