# SETUP_AGENT.md — instructions for an AI assistant setting this up for someone

You are helping a person set up **ecomm-mcp-server** on their own computer: a
local SQLite warehouse of their store's marketing and commerce data, filled by
sync scripts, and exposed read-only to an MCP client (usually Claude Desktop).
The person may not be technical. Your job is to get them to a working, healthy,
scheduled setup with as little friction and as little risk as possible.

The human-facing companion to this file is `docs/setup-with-ai.md`; the full
manual walkthrough is `docs/setup-manual.md`. Read `docs/setup-manual.md` before
you begin; it has the exact commands, config snippets and troubleshooting table
this file refers to. (If you are changing the code rather than setting it up,
read `AGENTS.md` instead.)

**Done means:**

1. Each platform the person chose syncs with status `ok`.
2. History is loaded to the depth they chose.
3. Their MCP client answers a question from real data.
4. A daily pipeline is scheduled, and `warehouse_health.py` reports no ERROR.
5. They have a short written summary of what you set up.

---

## Rules that override everything else

1. **Never ask for, accept, display or repeat a secret.** That covers API keys,
   access and refresh tokens, client secrets, passwords, webhook URLs and
   private keys. The person types them into `.env` in their own text editor.
   - If they paste one into the chat anyway: don't repeat it or use it. Tell
     them to revoke it in that platform's console, create a new one, and put
     the new one in `.env`.
   - To check `.env`, run `python env_check.py [platform words]`. It prints key
     names with `set` / `empty` / `missing` / `suspect` and never a value.
   - Never run `cat .env`, `type .env`, `Get-Content .env`, `printenv`, `env`,
     `set`, or anything else that prints `.env` contents or environment values.
     Never open `.env` with a file-reading tool.
   - Non-secret values are fine to discuss and, with permission, to write:
     `SHOPIFY_SHOP` (the store slug), regions, marketplace ids, account and
     property ids, and file paths.
2. **Sign-in helpers are run by the person, in their own terminal.**
   `google_auth.py`, `amazon_auth.py`, `tiktok_auth.py`, `klaviyo_auth.py` and
   `google_sheets_auth.py` involve a browser sign-in or a pasted authorization
   code, and they write tokens into `.env`. Give the person the exact command
   and let them run it. Don't ask them to paste the code to you.
3. **Read-only setup. Never change a live account.** Don't run any
   `*_mutate.py` script, `amazon_listing_quality_material_fix.py`,
   `reacher_sample_limits.py`, `tiktok_promotions.py`, or any command with
   `--execute`, `--yes` or `--apply`. Don't set `WAREHOUSE_MCP_ENABLE_WRITES`. If the
   person wants those features, finish setup first, point them to the
   platform's doc, and treat it as a separate task they drive.
4. **Don't open the server to the network on your own initiative.** Never use
   `--host 0.0.0.0` (or any non-loopback `--host`) or `--allow-any-host` unless
   the person explicitly asks to share with a team. Then follow `SHARING.md`
   in full (token, allowed hosts, TLS).
5. **Protect data that can't be recovered.** Once `warehouse.db` holds real
   data, don't delete, move or overwrite it without the person's explicit
   go-ahead after you've explained what's lost. Inventory, rank, price and
   placement snapshots have no backfill.
6. **Never commit or upload credentials or data.** Don't `git add` `.env`,
   `*.json` key files, `certs/` or `*.db`, and don't push anything anywhere
   unless asked.
7. **Ask before anything persistent or outward-facing:** creating a scheduled
   task, editing the MCP client's config file, a large backfill (it uses API
   quota and time), installing system software.
8. **Don't invent console steps.** Platform consoles change. Take the steps
   from `docs/<platform>.md` (Setup section) and quote them. If what the person
   sees doesn't match, say so plainly and work from what they describe; don't
   guess menu names.
9. **Verify instead of assuming.** After every step, run the check that proves
   it worked (listed in each phase below) and read the output before moving on.

## Decide your mode first

- **Mode A: you can run shell commands** on the person's machine (Claude Code
  or a similar agent). Run commands yourself, one logical step at a time, and
  explain each before running it. Prefer the repo's own scripts over ad-hoc
  code.
- **Mode B: chat only.** Give **one** command (or one small group) at a time in
  a code block, say what output to expect, and ask the person to paste the
  output back. Write commands for their OS and shell: PowerShell on Windows,
  bash/zsh on macOS and Linux. Remind them to activate the virtual environment
  in every new terminal. Keep explanations short. They are switching between
  windows.

In both modes, the person does the platform-website steps, the `.env` editing
and the sign-in helpers.

---

## Phase 0: Interview (a few short questions)

Ask, briefly:

- Operating system (and, on Windows, PowerShell or cmd).
- Which platforms they want, in priority order. Show the effort table from
  `docs/setup-manual.md` §2 and suggest starting any slow approvals now (Google
  Ads developer token, Amazon Ads scope) while you do the rest.
- Which MCP client they'll query from. Default to Claude Desktop.
- Whether this computer stays on overnight. This shapes scheduling.
- Whether they already have any credentials, or a previous install.

Keep the answers; you'll need them in the hand-off summary.

## Phase 1: Install

1. Check the tools: `python --version` (or `python3`) must be **3.11+**, and
   `git --version`. If either is missing, give the official download link
   (python.org, git-scm.com) and wait. On Windows, they need "Add python.exe
   to PATH" ticked.
2. Clone the repo (or use the folder they already have), create `.venv`,
   activate it, run `pip install -r requirements.txt`, and copy `.env.example`
   to `.env`. The commands are in `docs/setup-manual.md` §3.
3. **Check:** `pip install -r requirements-dev.txt` then `pytest -q`. Expect
   all tests to pass, with at most a skip or two, in about 20 seconds. A
   failure here is an install problem; fix it before going on.

## Phase 2: Sample data and the MCP client

1. `python run_sync.py --sample`
2. **Check:**
   `python -c "import server; print(server.spend_summary('2000-01-01', '2100-01-01'))"`
   prints rows for `google`, `meta` and `amazon`.
3. Connect the MCP client (with permission, since this edits their config):
   - Claude Desktop config: macOS
     `~/Library/Application Support/Claude/claude_desktop_config.json`,
     Windows `%APPDATA%\Claude\claude_desktop_config.json`.
   - **Merge** an `ecomm-warehouse` entry into `mcpServers`. Never replace the
     whole file; it may hold other servers. Use absolute paths to the
     `.venv` Python, `server.py` and `warehouse.db` (snippets in
     `docs/setup-manual.md` §5; on Windows, JSON backslashes must be doubled).
   - The person must fully quit and reopen Claude Desktop.
4. **Check:** the person asks Claude *"What was ad spend by platform over the
   last two weeks?"* and gets an answer from the sample data.
5. **Delete the sample data** before any real platform: remove `warehouse.db`,
   `warehouse.db-wal` and `warehouse.db-shm`. It shares tables and platform
   names with real data and would contaminate it. This is the one deletion
   that's always right, because the file holds only sample rows at this point.
   Confirm that's still true first.

## Phase 3: One platform at a time

Repeat for each platform, easiest or most important first:

1. Read `docs/<platform>.md`, Setup section. Tell the person what to create on
   the platform's website, step by step, quoting the doc. Mention any approval
   wait up front.
2. They fill in that platform's block in `.env`. Remind them: no quotes needed,
   and no comment on the same line as a key.
3. If the platform has a sign-in helper, give them the exact command to run
   themselves (Rule 2).
4. **Check:** `python env_check.py <platform word>`. Every required key should
   be `set`. `suspect` means an inline comment was read as the value; `missing`
   usually means a typo in the key name.
5. Probe first where the script supports it (`--probe` or `--dry-run`; the list
   is in `AGENTS.md` → Setup and commands). Then a small pull:
   - The six core connectors: `python run_sync.py --only <name> --days 3`.
     Valid names: `google`, `meta`, `amazon`, `amazon_orders`, `shopify`,
     `tiktok`.
   - A standalone script: use the Usage section of its doc, with the smallest
     window it offers.
6. **Interpret the result:**
   - `OK n rows`: working.
   - `SKIPPED`: the gating key is empty. Back to step 4.
   - `DEGRADED 0 rows` (or exit 75 from a standalone script): it connected but
     got nothing, or paused on an upstream fault. Check scopes, account or
     property ids, and whether the window had activity. Read the doc's Notes.
   - `ERROR`: read the message, then the doc's Notes and the troubleshooting
     table. Don't retry blindly; a missing scope never fixes itself.
7. Optionally confirm from the client: *"When did each platform last sync?"*
   (calls `last_sync_status`).

Don't move to the next platform while this one is in an unexplained state.

## Phase 4: Load history

1. Ask how far back they want for each platform. Note that each API has its own
   lookback limit (see its doc); asking for more just returns nothing older.
2. With their go-ahead, run the pulls **one at a time** (SQLite has a single
   writer). For example `python run_sync.py --only shopify --days 365`, or
   `--start/--end` windows for very long ranges.
3. Large pulls can take hours. In mode A, run them where they won't be killed
   when your session ends. In mode B, tell the person to keep the terminal open
   and stop a laptop from sleeping.
4. **Check:** each run ends `ok`, and a client question over the full range
   returns plausible totals. If a number looks wrong, compare it with the
   platform's own dashboard before assuming a bug; `AGENTS.md` → The data rules
   explains the usual reasons (summed ratios, attributed revenue, currencies).

## Phase 5: Schedule and health

1. Copy `pipeline.example.json` to `pipeline.json` and **remove every step for
   a platform that isn't configured**. Standalone scripts fail without
   credentials, and a step that fails every night trains people to ignore the
   alarm. Keep `backup_db.py`, `warehouse_health.py` and `health_alerts.py`.
2. **Check:** `python pipeline_runner.py daily --dry-run`, then a real
   `python pipeline_runner.py daily`. Read the per-step results.
3. With permission, schedule it using the matching entry in `docs/health.md` →
   Scheduling (Task Scheduler, cron or launchd), at a time the machine is on.
   - On Windows, creating a task may need an elevated terminal. If the task
     doesn't show up in `Get-ScheduledTask`, check from an elevated terminal
     before concluding it failed.
   - Tasks must run from the repo folder with the `.venv` Python.
4. Alerts (optional): the person sets one of `WAREHOUSE_HEALTH_SLACK_WEBHOOK`,
   `WAREHOUSE_HEALTH_GCHAT_WEBHOOK` or `WAREHOUSE_HEALTH_EMAIL_TO` (email also
   needs `SMTP_*`). These are secrets: they type them, you check with
   `python env_check.py health notifications`.
   **Check:** `python health_alerts.py --dry-run`.
5. **Check:** `python warehouse_health.py` reports no ERROR. DEGRADED is a
   known upstream pause that escalates to ERROR after 7 days; explain any you
   see.

## Phase 6: Hand-off

Write the person a short summary:

- Platforms connected, and how much history each has.
- Where things live: the repo folder, `warehouse.db`, `.env` (and "never share
  it"), `pipeline.json`.
- What runs when (the schedule), and how to check health:
  `python warehouse_health.py`, or ask Claude when each platform last synced.
- Backups: `backup_db.py` keeps local copies, and they should also copy
  `warehouse.db` off the machine now and then.
- Anything left undone: pending approvals, platforms deferred, warnings.
- Three or four example questions they can ask, matched to their platforms.
  For example: "What was ROAS by platform last week?", "Top 10 products by
  revenue this month?", "Which campaigns spent the most with the lowest ROAS?"

## Resuming a session

If the person comes back mid-setup, don't start over. Establish the state:
`python env_check.py --set-only` (what's configured), `python warehouse_health.py`
(what's running and healthy), the `last_sync_status` tool or the
`sync_log` table (what has synced), and whether `pipeline.json` exists. Then
continue from the first phase that isn't done.

## Reference

| Need | Where |
|---|---|
| Exact commands, client config snippets, troubleshooting | `docs/setup-manual.md` |
| A platform's credentials, usage, tables and traps | `docs/<platform>.md` (index in `README.md` → Connectors, by platform) |
| Statuses, health severities, pipeline config, scheduling | `docs/health.md` |
| Team / network sharing | `SHARING.md` |
| Every environment variable, with comments | `.env.example` |
| Data-correctness rules (why a number can look wrong) | `AGENTS.md` → The data rules |
