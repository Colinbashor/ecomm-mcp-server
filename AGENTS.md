# AGENTS.md — orientation for AI coding agents

You are working on a **local e-commerce data warehouse**: a set of connectors
that pull marketing and commerce data from ~12 platforms into one SQLite file,
plus an MCP server that exposes it read-only so an assistant can answer
questions about the business in plain English.

This file exists because the connectors are the easy half. The hard half is the
accumulated knowledge of how each API misleads you — and most items below were
learned by shipping a wrong number first. Read the relevant section before
touching a connector, and add to it when you learn something new.

If you only read one thing: **an empty result and a broken pull look identical,
and so do a partial period and a genuinely bad week.** Most rules here exist to
tell those apart.

---

## Layout

```
run_sync.py            drives the 6 shared-table connectors (see below)
<platform>_sync.py     standalone syncs, each owning its own tables
warehouse/
  db.py                connection helpers, WAL, migrations
  schema.sql           the shared core tables
  connectors/          the 6 that write shared `orders` / `ad_metrics`
  brand_analytics.py   shared Amazon report runner
  notify.py            optional Slack / Google Chat webhooks
server.py              MCP server (6 read-only tools)
docs/<platform>.md     per-platform setup + traps  <- READ BEFORE EDITING
tests/                 hermetic; no network, no database file
```

`run_sync.py` drives `google`, `meta`, `amazon` (ads), `amazon_orders`,
`shopify`, `tiktok` — the ones sharing `orders` and `ad_metrics`. Everything
else is a standalone script owning its own tables, so one failure stays
isolated.

**A connector is skipped, not failed, when its credentials are absent.** That is
deliberate: someone running three platforms should not see errors for the nine
they never configured.

---

## Setup and commands

```bash
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # then fill in only the platforms you want
python -m pytest -q           # hermetic, seconds, no credentials needed
python server.py              # stdio MCP server
```

Most syncs support `--dry-run` or `--probe`. **Use them first.** A probe costs
one request and usually tells you a scope is missing, which is the real problem
far more often than the error message suggests.

---

## The data rules

These are cross-cutting. Violating one produces a plausible number, which is
worse than a crash.

**1. Average ratios; never sum them.** Impression share, conversion rate, CTR,
per-unit averages. Summing seven days of a rate yields a meaningless number that
still looks approximately reasonable.

**2. Never add revenue across ad platforms.** Each platform reports what it
claims credit for, on its own attribution window, and those claims overlap each
other and the order feed. Ad-platform revenue belongs inside that platform's own
ROAS and nowhere else. Totals come from order feeds.

**3. Know which tables can never be rebuilt.** Many endpoints return only
*current* state — inventory, search rank, price, catalog flags, on-site
placement, best-seller rank. For those, history exists *only* because a
scheduled run stored it, and there is no backfill, ever. Two consequences: never
delete those rows, and if a daily run silently stops, that window is gone
permanently. Back them up on a different schedule from tables you can re-pull.

**4. Every API has a retention floor, and out-of-range is not an error.** Ad
platforms cap lookback (commonly 90 days to ~3 years); report APIs cancel
requests for periods they no longer hold. An empty response usually means
"outside retention", not "broken". Find the floor by walking backwards until the
API refuses, then record it in `docs/<platform>.md`.

**5. A period that just closed is often not published yet.** Pull a week the
morning after it ends and you may silently get six days. The report succeeds,
the number lands 10–20% light, and nothing looks wrong. Record what you *asked
for* next to what you received, and re-pull later rather than trusting one
attempt.

**6. Absence of rows can never be your resume marker.** Reports legitimately
omit entities with no activity, so "no rows for X" is indistinguishable from
"never requested X" — and a resume built on it re-requests those entities
forever. Keep a coverage table recording what was requested and what returned.

**7. Percent vs fraction differs between sibling reports on one API.** One
report returns `14.29` where its neighbour returns `0.1429` for the same kind of
quantity. Normalize at ingest and name the column for its unit.

**8. Deduplicate before counting.** The same physical thing is often reported
under several identifiers — one stock pool under multiple SKU aliases, one
product under multiple variant ids. Summing rows overstates by tens of percent.
Group by the identifier for the *physical* thing.

**9. A status column may not mean what its name suggests.** Settlement status is
not order validity; payment status is not fulfillment status. A filter that
looks right can select ~100% of an old period and ~0% of a current one, which
manufactures a fake trend. Check the distribution across both old and recent
data before filtering on any status.

**10. Money needs its currency.** Multi-currency feeds report in the order's own
currency, often with no FX rate in the payload. Currencies whose units sit ~100x
apart will dominate a naive `SUM`. Filter to one currency or convert explicitly.

---

## The engineering rules

**1. A run that achieved nothing must never log `ok`.** This is the most
expensive lesson in this codebase. A "graceful pause" that logged success once
hid 29 consecutive no-op runs across 13 days, because the health check stayed
green. If a run cannot make progress, log `degraded` and exit non-zero. A
resumable design without this fails silently forever.

**2. Never persist an unproven cursor.** Save a pagination cursor only after a
page actually returns data. A seeded or crafted cursor that turns out to be dead
becomes permanent if you store it first. Add a poison guard: after N consecutive
zero-progress runs, discard the cursor and re-seed.

**3. Name your INSERT columns.** `ALTER TABLE ADD COLUMN` appends physically at
the end of the table, which is *not* where `CREATE TABLE` lists it on a fresh
database. Positional `INSERT ... VALUES (?,?,?)` therefore maps onto different
columns depending on whether the table was migrated or created fresh. Always
`INSERT INTO t (a, b, c) VALUES (?,?,?)`.

**4. Diff history rows before the snapshot upsert.** Change-log tables compare
new values against the stored snapshot, so the diff must run *before* the upsert
overwrites the basis. Reverse the order and the log is silently always empty.

**5. Retry only transient failures.** 429, 500, 502, 503, 504 and explicit
rate-limit codes deserve exponential backoff. A malformed request or a missing
scope is permanent — retrying burns minutes to reach the same error. Keep the
transient set explicit.

**6. SQLite locks the whole database, not a table.** Two writers always contend
however unrelated their tables. Writers get a long busy timeout
(`db.BUSY_TIMEOUT_SECONDS`); readers use `connect_readonly()`, which sets no
timeout *because* the database is in WAL mode, where a reader never blocks on a
writer. `init_db()` sets `journal_mode=WAL` — if you ever remove that, the
read path loses its only protection and MCP queries will stall behind a running
sync. Run writers one at a time.

**7. Paginate; never warn-and-truncate.** If a response can hit a row cap, loop
until exhausted. Code that logs a warning and stores a partial page will quietly
store partial data for months.

**8. Latency is usually per-request, not per-row.** Fetching 100 items often
costs what fetching 1 costs, so page at the maximum the API allows.

---

## The MCP server

Six read-only tools: `list_tables`, `run_sql`, `spend_summary`,
`sales_summary`, `top_campaigns`, `last_sync_status`.

Security properties, all deliberate — **do not weaken these without saying so
explicitly in the commit message**:

- The database is opened `mode=ro`. Writes are impossible at the driver level,
  not filtered by keyword matching.
- Remote `run_sql` denies sensitive columns through a SQLite authorizer, so the
  denial happens inside the engine rather than in a query parser you can trick.
- `--http` binds `127.0.0.1` unless `--host` widens it, and warns when widened —
  louder still without TLS, because the bearer token is replayable by anyone who
  can read it. A test greps for a hardcoded wildcard bind; keep it passing.
- `HostGuard` validates Host/Origin itself rather than using the SDK's static
  list, so a changing network address needs no restart. Never add a
  prefix/suffix hostname rule: `<yourhost>.evil.com` is registrable by anyone.

Static bearer auth is **not** OAuth 2.1. It suits loopback, a tunnel, or a small
trusted LAN. Managed or hosted connectors that expect OAuth will not work with
it — front it with an identity-aware proxy rather than hand-rolling an
authorization server.

---

## Adding a connector

Follow an existing standalone sync; they share a shape.

1. Read `docs/` for a platform with similar auth and copy its structure.
2. Own your tables. Join the shared `orders` / `ad_metrics` only if your data is
   genuinely the same grain as what is already there.
3. Create tables and apply migrations idempotently on every run, so a fresh
   clone and an existing database both work.
4. Log to `sync_log` with `ok` / `degraded` and a message naming what was
   actually covered — especially for a partial run.
5. Support `--dry-run` (or `--probe`), a date window, and resume.
6. Write `docs/<platform>.md`: auth steps, retention floor, pagination style,
   rate limits, and every trap you hit. **This is a deliverable, not an
   afterthought** — the next agent has none of your context.
7. Add hermetic tests. Parsing, filtering and unit conversion are all testable
   against fixtures with no network.

---

## Working agreements

- **Run `python -m pytest -q` before and after.** It is hermetic and takes
  seconds. CI runs it on Linux, macOS and Windows.
- **Measure; do not assume.** When a number looks wrong, query the database and
  compare against the platform's own UI before changing code. Many "bugs" here
  turn out to be real definitional differences worth documenting instead.
- **Never commit credentials.** `.env` is gitignored; `.env.example` carries key
  names only. CI runs gitleaks over full history and blocks credential-shaped
  paths.
- **Prefer a comment explaining *why* over one restating *what*.** The code
  already says what it does. It cannot say which of three plausible readings of
  an API is correct, or which one already produced a wrong number.
- **State uncertainty.** "Verified live on <date>" and "assumed, not measured"
  are different claims, and a later reader cannot tell them apart. Say which.
