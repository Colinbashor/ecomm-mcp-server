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
python server.py --http --host 0.0.0.0 --port 8787
```

This requires `WAREHOUSE_MCP_TOKEN` in `.env` (any long random string —
`python -c "import secrets; print(secrets.token_urlsafe(32))"` works). Every
request must send it as `Authorization: Bearer <token>`.

**`--host 0.0.0.0` is required and is not the default.** Plain `--http` binds
`127.0.0.1`, reachable only from the machine it runs on. Sharing with teammates
means deliberately widening that, which is why this page exists — and why the
server logs a warning when you do.

Prefer the narrowest address that works: if everyone is on one subnet, binding
that interface's own IP (`--host 192.168.1.20`) beats the `0.0.0.0` wildcard,
which also picks up any VPN, hotspot, or guest interface the machine happens to
have.

Plain HTTP also means the token crosses the network in cleartext. Claude's
connector UI and most browser-based MCP clients refuse plain `http://` URLs
anyway, so if you want teammates connecting from anywhere other than a trusted
local network, put a real TLS terminator in front (a reverse proxy, a
Cloudflare Tunnel, Tailscale) rather than relying on the steps below, which are
meant for a small team on the same LAN.

For a LAN-only setup, generate a self-signed certificate once (`cryptography`
is already in `requirements.txt`, so no separate install is needed if you
followed the README's Setup step):

```bash
python make_cert.py
```

This writes `certs/warehouse-mcp.crt` (safe to share with teammates — no
secret material) and `certs/warehouse-mcp.key` (never share this one).
`server.py --http` automatically serves HTTPS once both files exist. Re-run
`make_cert.py` if the machine's LAN IP changes, or when adding extra names
(`python make_cert.py extra.hostname 10.1.2.3`). The certificate's
Organization field is cosmetic (clients trust it by SAN + Trusted Root
install, not by this string) but defaults to "ecommerce-warehouse MCP" —
set `CERT_ORG_NAME` in `.env` before running it to put your own team or
company name there instead.

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

`--allow-any-host` skips this check entirely — useful for a quick local debug
session, not for anything reachable by a teammate. It logs a warning on
startup and re-warns every 6 hours for as long as it's set, specifically so an
`--allow-any-host` left on in `serve_mcp.bat` doesn't go unnoticed.

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
`last_sync_status`. See the README's [MCP tools](README.md#mcp-tools)
section for what each one does and its parameters.

## Migrating off the legacy token-in-URL scheme

An earlier version of this setup put the bearer token directly in the URL path
(`https://host:8787/<token>/mcp`) instead of the `Authorization: Bearer` header
shown in step 3 above. That style is deprecated — a token embedded in a URL
tends to end up in browser history, proxy access logs, and shell history, none
of which happens with a header — but a fleet of already-configured teammates
can't all switch their local `claude_desktop_config.json` in the same instant.

`--allow-legacy-token-path` bridges the gap: while it's set, the server accepts
**both** the new header style and the old `/<token>/mcp` path, so you can roll
the header-based config out to one teammate at a time without breaking
everyone else's connection mid-migration.

```bash
python server.py --http --host 0.0.0.0 --port 8787 --allow-legacy-token-path
```

Once every teammate's config has moved to the header style, remove the flag
(and update `serve_mcp.bat` if you run it as a Windows service) — leaving it on
indefinitely keeps the weaker scheme available with no offsetting benefit.

While the flag is set:
- Both URL styles authenticate identically; either one being valid is enough.
- The server scrubs the token out of the path before anything logs it (Uvicorn's
  access log included), so even the legacy style doesn't leave the secret sitting
  in plain text in a log file.
- If you rotate `WAREHOUSE_MCP_TOKEN`, a client still hitting the old
  `/<old-token>/mcp` URL gets a normal 401, exactly like a wrong header would —
  rotation isn't blocked by having the flag on.

## Notes

- The server is **read-only** — `run_sql` only permits `SELECT`/`WITH`, and
  the underlying connection is opened `mode=ro`, so nothing a client sends can
  write to the warehouse.
- `run_sql` also carries a wall-clock budget (`RUN_SQL_TIMEOUT_SEC` in
  `server.py`, 45s by default) — one unindexed scan or accidental cross join
  can't tie up a server that several people share. A query that hits the
  budget gets cancelled with a clear error instead of stalling everyone else.
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
