# Sharing one warehouse with a team

`server.py` normally talks to Claude Desktop over stdio: one person, one
machine, one `warehouse.db`. If several people want to query the same
warehouse, run the server in `--http` mode on whichever machine holds the
database, and have everyone else connect to it remotely.

Custom connectors added via Claude's Settings → Connectors UI always connect
*from Anthropic's cloud*, so they can never reach a server that only exists on
your office network or a colleague's laptop. The supported path for a
LAN-only or self-hosted server is a **local MCP config** in each teammate's
own Claude Desktop, using [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)
as a thin bridge that connects out from their machine instead of in from
Anthropic's.

## 1. Start the server in HTTP mode

```bash
python server.py --http --port 8787
```

This requires `WAREHOUSE_MCP_TOKEN` in `.env` (any long random string —
`python -c "import secrets; print(secrets.token_urlsafe(32))"` works). Every
request must send it as `Authorization: Bearer <token>`.

By default this binds `0.0.0.0:8787` and serves plain HTTP. Claude's connector
UI and most browser-based MCP clients refuse plain `http://` URLs, so if you
want teammates to connect from anywhere other than a trusted local network,
put a real TLS terminator in front of it (a reverse proxy, a Cloudflare
Tunnel, etc.) rather than relying on the steps below, which are meant for a
small team on the same LAN.

For a LAN-only setup, generate a self-signed certificate once:

```bash
pip install cryptography
python make_cert.py
```

This writes `certs/warehouse-mcp.crt` (safe to share with teammates — no
secret material) and `certs/warehouse-mcp.key` (never share this one).
`server.py --http` automatically serves HTTPS once both files exist. Re-run
`make_cert.py` if the machine's LAN IP changes, or when adding extra names
(`python make_cert.py extra.hostname 10.1.2.3`).

On Windows, `serve_mcp.bat` keeps the server running across reboots and
restarts it if it crashes — point a Task Scheduler "At startup" trigger at it.

## 2. Lock down which hostnames may connect

`server.py` validates the incoming `Host` and `Origin` headers itself (see the
`HostGuard` class) rather than trusting the SDK's static allowlist, because a
snapshot of "current LAN IPs" goes stale the moment the host machine changes
networks. By default it accepts:

- `localhost` and any loopback address
- the machine's own hostname, `<hostname>.local`, and its resolved FQDN
- whatever IP address a request *actually arrived on* (so a moved network
  needs no restart, and a retired address stops working automatically)

To accept an additional name (a Cloudflare Tunnel hostname, a hosts-file
alias, a static DNS name teammates use), add it to `allowed_hosts.txt` next to
`server.py` (one per line, `#` comments allowed) or the
`WAREHOUSE_MCP_ALLOWED_HOSTS` env var (comma-separated). Both are re-read
automatically within ~10 seconds of a rejected request — no restart needed,
and removing a name revokes it just as fast.

Use `python server.py --check-host <value>` to test what a given Host header
would resolve to before a teammate hits it live.

## 3. Give each teammate a config snippet

They'll need:
- **Claude Desktop** (the app, not claude.ai in a browser)
- [Node.js](https://nodejs.org) installed (LTS is fine) — `mcp-remote` runs
  via `npx`
- Network access to the host machine (same LAN, VPN, or tunnel)
- From you: `warehouse-mcp.crt` (if you generated one) and the bearer token

Settings → Developer → **Edit Config** in Claude Desktop opens
`claude_desktop_config.json`. Add inside `mcpServers`:

```json
{
  "mcpServers": {
    "ecomm-warehouse": {
      "command": "npx",
      "args": ["-y", "mcp-remote",
               "https://YOUR-HOSTNAME:8787/mcp",
               "--header", "Authorization:${WAREHOUSE_AUTH_HEADER}"],
      "env": {
        "NODE_EXTRA_CA_CERTS": "C:\\path\\to\\warehouse-mcp.crt",
        "WAREHOUSE_AUTH_HEADER": "Bearer PASTE_THE_TOKEN_HERE"
      }
    }
  }
}
```

Notes:
- Keep the header argument exactly as shown — putting the full `Bearer ...`
  value directly in `args` is fragile on Windows, which can mangle arguments
  containing spaces.
- `NODE_EXTRA_CA_CERTS` only matters if you're using the self-signed cert from
  `make_cert.py`; drop it if you fronted the server with a properly trusted
  certificate instead. `mcp-remote` (via Node) ignores the OS certificate
  store, so installing the `.crt` into Windows' Trusted Root store does
  nothing for it — `NODE_EXTRA_CA_CERTS` is the setting that matters here.
- If `YOUR-HOSTNAME` doesn't resolve for a teammate, try the full FQDN, or
  connect by IP (`https://<ip>:8787/mcp`) — but regenerate the cert first if
  you go this route, since IPs aren't in the SAN list until `make_cert.py`
  sees them.
- An **HTTP 421** response means the server rejected the hostname in the URL:
  add it to `allowed_hosts.txt` on the host machine (no restart required) and
  have them retry.

Fully quit Claude Desktop (not just close the window) and reopen it. You
should then see the warehouse tools under the hammer icon: `spend_summary`,
`sales_summary`, `top_campaigns`, `run_sql`, `list_tables`,
`last_sync_status`.

## Notes

- The server is **read-only** — `run_sql` only permits `SELECT`/`WITH`, and
  the underlying connection is opened `mode=ro`, so nothing a client sends can
  write to the warehouse.
- Ask Claude to run `last_sync_status` to check data freshness for each
  platform.
- The bearer token is a secret. Don't post it in a public channel or embed it
  directly in a URL — keeping it in the local MCP config's environment (as
  shown above) keeps it out of web server access logs.
- If the server stops responding, check whether the host machine is powered
  on, reachable on the same network, and whether the token was rotated.
- Remote HTTP requests get one extra layer of protection: any column you list
  in `server.py`'s `_REMOTE_DENIED_COLUMNS` is unreadable over `--http` (even
  through joins, aliases, or subqueries) while remaining fully queryable over
  local stdio. Populate that set for your own schema before sharing anything
  containing personal data.
