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

## Claude Desktop

Copy `claude_desktop_config.example.json` into your Claude Desktop config and replace
`/ABSOLUTE/PATH/TO/ecomm-mcp-server` with the real path — Claude Desktop requires
absolute paths and does not expand `~`.

The interpreter path differs by platform:

- macOS / Linux — `.venv/bin/python`
- Windows — `.venv\Scripts\python.exe`

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
