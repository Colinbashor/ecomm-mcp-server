# Setting it up yourself

This guide takes you from nothing to an AI assistant answering questions about
your store from your own data, refreshed every night. It assumes you are
comfortable running commands in a terminal and editing a text file. You do not
need to know Python.

If you would rather have an AI assistant walk you through it, see
[setup-with-ai.md](setup-with-ai.md) instead.

**Contents**

1. [What you are building](#1-what-you-are-building)
2. [Before you start](#2-before-you-start)
3. [Install](#3-install)
4. [Try it with sample data](#4-try-it-with-sample-data)
5. [Connect Claude Desktop](#5-connect-claude-desktop)
6. [Add your first real platform](#6-add-your-first-real-platform)
7. [Load history](#7-load-history)
8. [Run it every day](#8-run-it-every-day)
9. [Optional extras](#9-optional-extras)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What you are building

```
platform APIs ──> sync scripts ──> warehouse.db ──> server.py ──> Claude (or another MCP client)
(Shopify, Meta,   (run nightly)    (one SQLite     (read-only)
 Google, ...)                       file)
```

- **Sync scripts** pull data from each platform you connect into a single
  SQLite file, `warehouse.db`, on your machine. Nothing is sent anywhere else.
- **`server.py`** lets an MCP client such as Claude Desktop read that file. It
  opens the database read-only, so asking questions can never change your data.
- Every platform is optional. Start with one and add others later.

## 2. Before you start

**You need:**

- A computer that is on when you want the nightly refresh to run. A laptop is
  fine to start; an always-on machine is better once you schedule it.
- **Python 3.11 or newer.** Check with `python --version` (on macOS/Linux it may
  be `python3 --version`). Get it from [python.org](https://www.python.org/downloads/)
  if needed. On Windows, tick "Add python.exe to PATH" in the installer.
- **Git**, or download the repository as a ZIP from GitHub.
- An MCP client. This guide uses [Claude Desktop](https://claude.ai/download).
- Admin access to each platform you want to connect.

**Choose your platforms.** Credentials are the slow part, and some platforms
make you wait for approval, so start those first. Each row links to the page
with the exact steps.

| Platform | What you create | Effort |
|---|---|---|
| [Shopify](shopify.md) | A Dev Dashboard app with order scopes, installed on your store | Easy, ~15 min |
| [Meta Ads](meta-ads.md) | A long-lived system-user token in Business Settings | Easy to medium |
| [Klaviyo](klaviyo.md) | A read-only private API key | Easy, ~5 min |
| [Google Analytics 4](ga4.md) | A Google Cloud service account added to GA4 as Viewer | Medium |
| [Google Ads](google-ads.md) | A developer token, an OAuth desktop client, then a browser sign-in (`google_auth.py`) | Medium to hard: the developer token needs Google's approval |
| [Amazon Ads](amazon-ads.md) | An LWA app with the Ads API scope, then a sign-in (`amazon_auth.py`) | Hard: scope approval can take ~72 hours |
| [Amazon Seller (SP-API)](amazon-seller.md) | A private developer app in Seller Central, self-authorized | Medium, if your account can register a developer app |
| [TikTok Shop](tiktok-shop.md) | A Partner Center app authorized with all scopes, then `tiktok_auth.py` | Medium to hard |

The full list (Merchant Center, Search Console, Flexport, Purple Dot, Reacher,
Algolia, and more) is in the README under
[Connectors, by platform](../README.md#connectors-by-platform).

## 3. Install

```bash
git clone https://github.com/Colinbashor/ecomm-mcp-server.git
cd ecomm-mcp-server
python -m venv .venv
```

Activate the virtual environment. You need to do this in every new terminal
before running the scripts:

```bash
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows (PowerShell or cmd)
```

Then install the dependencies and create your settings file:

```bash
pip install -r requirements.txt
cp .env.example .env               # Windows cmd: copy .env.example .env
```

`.env` holds your credentials. It is listed in `.gitignore`, so git will not
commit it. Keep it that way, and never share it.

Optional, but a good check that the install worked:

```bash
pip install -r requirements-dev.txt
pytest -q                          # ~20 seconds, no network, no credentials
```

## 4. Try it with sample data

Load two weeks of made-up ad and order data:

```bash
python run_sync.py --sample
```

Check it from the command line:

```bash
python -c "import server; print(server.spend_summary('2000-01-01', '2100-01-01'))"
```

You should see spend, revenue and ROAS for `google`, `meta` and `amazon`.

> **Delete the sample data before you connect a real platform.** It is written
> into the same `warehouse.db` that real syncs fill, under the platform names
> `google`, `meta`, `amazon` and `tiktok`, so it would mix with your real
> numbers. When you're ready for step 6, delete `warehouse.db` (and any
> `warehouse.db-wal` / `warehouse.db-shm` beside it).

## 5. Connect Claude Desktop

1. Open Claude Desktop's config file. In Claude Desktop, go to
   **Settings → Developer → Edit Config**, or open it directly:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Add an entry under `mcpServers`, using **absolute paths**. Claude Desktop
   does not expand `~` or relative paths. If the file already has other
   servers, add this one beside them rather than replacing the file.

   macOS / Linux:

   ```json
   {
     "mcpServers": {
       "ecomm-warehouse": {
         "command": "/Users/you/ecomm-mcp-server/.venv/bin/python",
         "args": ["/Users/you/ecomm-mcp-server/server.py"],
         "env": { "WAREHOUSE_DB": "/Users/you/ecomm-mcp-server/warehouse.db" }
       }
     }
   }
   ```

   Windows (note the doubled backslashes):

   ```json
   {
     "mcpServers": {
       "ecomm-warehouse": {
         "command": "C:\\Users\\you\\ecomm-mcp-server\\.venv\\Scripts\\python.exe",
         "args": ["C:\\Users\\you\\ecomm-mcp-server\\server.py"],
         "env": { "WAREHOUSE_DB": "C:\\Users\\you\\ecomm-mcp-server\\warehouse.db" }
       }
     }
   }
   ```

3. Quit Claude Desktop completely (not just close the window) and reopen it.
4. Ask: *"Using the warehouse, what was ad spend by platform over the last two
   weeks?"* Claude should call `spend_summary` and answer from the sample data.

If the tools don't appear, see [Troubleshooting](#10-troubleshooting).

## 6. Add your first real platform

Delete the sample `warehouse.db` first (see step 4). Then, for each platform:

1. **Read its page** (linked in the table in step 2). Each page has a Setup
   section listing exactly which credentials to create and which `.env`
   variables they go in.
2. **Create the credentials** in that platform's console.
3. **Fill in its block in `.env`** with a plain text editor. Two rules:
   - No quotes are needed: `SHOPIFY_SHOP=my-store`.
   - Never put a comment on the same line as an empty key (`META_ACCESS_TOKEN=  # todo`).
     The comment can be read as the value, and the platform then looks
     configured when it isn't. Put comments on their own line.
4. **Run the sign-in helper**, if the platform has one (Google Ads, Amazon Ads,
   TikTok Shop, Klaviyo OAuth). It opens a browser or prints a URL, and writes
   the resulting token into `.env` for you. For example:

   ```bash
   python google_auth.py
   ```

5. **Check what's filled in.** This lists each key as `set`, `empty`, `missing`
   or `suspect`, and never prints a value:

   ```bash
   python env_check.py shopify
   ```

6. **Do a small test pull.** Some scripts have `--probe` or `--dry-run`, which
   make one cheap request and write nothing; use them first where they exist.
   Then pull a few days:

   ```bash
   python run_sync.py --only shopify --days 3
   ```

   For a standalone script, use the command from its page's Usage section.

7. **Read the result.**

   | You see | It means |
   |---|---|
   | `OK  1234 rows` | Working |
   | `SKIPPED (no credentials in .env)` | The gating key for that platform is empty. Run `env_check.py` for it |
   | `DEGRADED  0 rows returned` | It connected but got nothing back: usually a missing scope, the wrong account id, or a window with genuinely no activity |
   | `ERROR ...` | Read the message. Its page's Notes section covers the common ones |

   You can ask Claude *"when did each platform last sync?"* at any time. That
   calls `last_sync_status`.

The six core connectors, which share one ads/orders shape (Google Ads, Meta,
Amazon Ads, Amazon orders, Shopify, TikTok Shop orders), all run through
`run_sync.py`. Everything else has its own `<platform>_sync.py` script.

## 7. Load history

Once a platform works, pull more history:

```bash
python run_sync.py --only shopify --days 365
python run_sync.py --only meta --start 2025-01-01 --end 2025-12-31
```

- **Each platform has a limit on how far back it goes.** Asking for more is not
  an error; you just get nothing older than the limit. Each page notes its
  limit where it's known.
- **Run one large pull at a time.** SQLite allows one writer at a time, so two
  big jobs mostly wait on each other.
- **Some data can't be pulled for past dates at all.** Inventory snapshots,
  search rank and prices only exist from the day you start collecting them,
  which is the strongest reason to schedule them soon (step 8).
- Large backfills can take hours. Keep the terminal open, and on a laptop stop
  it from sleeping.

## 8. Run it every day

**1. Make your pipeline file.** Copy the example and trim it to the platforms
you actually use:

```bash
cp pipeline.example.json pipeline.json
```

Delete every step for a platform you haven't configured. `run_sync.py` skips
missing credentials, but a standalone script without credentials fails, and a
step that fails every night is noise that hides real problems.

**2. Check the plan, then run it once by hand:**

```bash
python pipeline_runner.py daily --dry-run
python pipeline_runner.py daily
```

**3. Schedule it.** Use Task Scheduler on Windows, cron on Linux, or launchd on
macOS. [health.md → Scheduling](health.md#scheduling) has a ready entry for each.

**4. Check health.** The example pipeline ends with two steps:

- `warehouse_health.py` checks for stale tables, feeds that stopped running,
  and runs that keep returning nothing. It reports ERROR, DEGRADED or WARN.
- `health_alerts.py` sends a message only when that picture changes.

To get those messages, fill in one of `WAREHOUSE_HEALTH_SLACK_WEBHOOK`,
`WAREHOUSE_HEALTH_GCHAT_WEBHOOK` or `WAREHOUSE_HEALTH_EMAIL_TO` (email also
needs the `SMTP_*` block) in `.env`.

```bash
python warehouse_health.py            # see the current state any time
python health_alerts.py --dry-run     # see what would be sent
```

**5. Back it up.** `backup_db.py` (included in the example pipeline) keeps
rotating local copies. Also copy `warehouse.db` somewhere off this machine now
and then, because some of what it holds can't be downloaded again.

## 9. Optional extras

- **Share with your team** over your network: [SHARING.md](../SHARING.md).
  This means binding the server beyond your own machine, with a token and
  ideally TLS. Read the whole page first.
- **Write-capable scripts.** The `*_mutate.py` scripts,
  `amazon_listing_quality_material_fix.py`, `reacher_sample_limits.py` and
  `tiktok_promotions.py` change live accounts. They do a dry run unless you
  pass `--execute` (`--yes`, `--apply`). You don't need them for setup.
- **Google Sheets write tools** for Claude: [google-sheets.md](google-sheets.md).
  They are off unless `WAREHOUSE_MCP_ENABLE_WRITES=1`.
- **Mirror into BigQuery**: [operations.md](operations.md).
- **Cross-channel product lookup**: run [catalog_sync.py](catalog.md) and Claude
  can translate a SKU, ASIN or TikTok product id across channels.

## 10. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `python` not found, or the wrong version | On macOS/Linux try `python3`. On Windows, reinstall with "Add to PATH" ticked |
| `ModuleNotFoundError` | The virtual environment isn't active. Run the activate command from step 3 |
| A platform says `SKIPPED` though you filled it in | Run `python env_check.py <platform>`. Look for `empty`, `missing` (a typo in the key name), or `suspect` (an inline comment) |
| Claude Desktop shows no warehouse tools | Paths in the config must be absolute and must exist; on Windows, backslashes must be doubled; quit Claude fully and reopen. Logs are in `~/Library/Logs/Claude/` (macOS) or `%APPDATA%\Claude\logs\` (Windows) |
| Claude answers but every number is empty | Claude Desktop and your syncs are using different databases. Make `WAREHOUSE_DB` in the Claude config point at the same file the syncs write (by default `warehouse.db` in the repo folder) |
| `DEGRADED 0 rows` | Connected but empty. Check the scopes, the account or property id, and that the window had activity |
| `database is locked` | Two writers at once. Let one finish; don't run backfills during the scheduled pipeline |
| Amazon sign-in redirect fails | `AMAZON_ADS_REDIRECT_URI` must match the Allowed Return URL exactly, including any trailing slash |
| TikTok orders fail with `105005` about a week after setup | The app was re-authorized with only some scopes. Authorize again with all scopes and rerun `tiktok_auth.py` |

Each platform page has a Notes section with that platform's known traps, and
[health.md](health.md) explains every health state.
