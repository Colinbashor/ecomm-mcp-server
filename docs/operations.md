# Operations

Three standalone utilities that aren't connectors — no platform credentials,
no data pulled from any platform. All are optional.

## Notifications — `warehouse/notify.py`

A small utility any script (or your own code) can call to push a plain-text
or lightly-formatted message to Slack, Google Chat, and/or email. Useful for
a "sync finished, here's a summary" ping, or turning any sync script into an
alert when something needs attention. It has no CLI; import it. The module
calls `load_dotenv()` on import, so values in `.env` are picked up.

```python
from warehouse import notify
notify.send("*Sync complete*\n- 1,204 rows written\nFull report: https://...", dest="daily_sync")
```

### Setup

Targets are configured per named `dest` in `.env`. The `dest` string is
upper-cased to form the prefix (`dest="weekly_digest"` →
`WEEKLY_DIGEST_SLACK_WEBHOOK`, …). `.env.example` shows the `DAILY_SYNC_*`
block (commented out) and the `SMTP_*` block.

| Variable | Scope | Notes |
|---|---|---|
| `<DEST>_SLACK_WEBHOOK` | per dest | Slack Incoming Webhook URL (Slack App settings → Incoming Webhooks). |
| `<DEST>_GCHAT_WEBHOOK` | per dest | Google Chat Space webhook URL (Space settings → Apps & integrations → Webhooks). |
| `<DEST>_EMAIL_TO` | per dest | Comma-separated recipient addresses. |
| `SMTP_HOST` | global | Default `smtp.gmail.com`. |
| `SMTP_PORT` | global | Default `587`. The connection always uses STARTTLS. |
| `SMTP_USER` / `SMTP_PASSWORD` | global | **Both** required for any email to be sent. If the account has 2-Step Verification on, `SMTP_PASSWORD` must be a Google **app password** (myaccount.google.com/apppasswords), not the normal account password — Gmail otherwise rejects the login with "5.7.9 Application-specific password required". |
| `SMTP_FROM` | global | Sender address; defaults to `SMTP_USER` when blank. |

The `SMTP_*` values are read at **call time**, not import time, so a caller
(or test) that sets them just before sending sees them take effect. Any SMTP
provider works — point `SMTP_HOST`/`SMTP_PORT` at it.

### `notify.send(text, dest)` — best-effort, never raises

For each target configured for `dest`:

- **Slack / Google Chat:** a single `POST {"text": ...}` to the webhook with a
  10 s timeout (`TIMEOUT_SECONDS`). No retry. Text longer than `MAX_CHARS`
  (3,800 — under Google Chat's ~4,096 cap) is cut and suffixed with
  `…(truncated)`. Both platforms render the same minimal markdown subset
  (single-asterisk `*bold*`, no headers).
- **Email:** the **full, untruncated** text is sent via `send_email()` (so it
  gets the same retry), with the subject taken from the first line of `text`
  stripped of `*`, `-`, `•` markup (fallback: `Warehouse notification`), the
  plain text as the text/plain part, and `to_email_html(text)` as the HTML
  part. A `_EMAIL_TO` that contains no valid address after splitting on
  commas is reported and skipped.

A `dest` with nothing configured prints
`[notify] no target configured for dest=... — skipped` and returns. A target
that fails prints `[notify] <platform> (dest=...) failed: ...` and the
remaining targets still run — `send()` never raises, so a notification
failure can't take down whatever pipeline called it. It returns `None`; if
you need to know whether delivery worked, call `send_email()` directly.

Formatting helpers:

- `to_chat_markdown(md)` converts `#`/`##` headers to a bold line and
  `**bold**` to `*bold*`. `send()` does **not** call it for you — run your
  text through it first if it was written as normal Markdown.
- `to_email_html(text)` converts chat-markdown to simple HTML: `*bold*` →
  `<b>`, consecutive `-`/`•` bullet lines → one `<ul>`, blank line → spacer,
  a whole `Label: https://...` line → one hyperlink labelled `Label`, any other
  bare URL → linkified in place. HTML special characters are escaped first.

### Sending a fully custom HTML email — `notify.send_email()`

`send()`'s email target auto-converts the same short chat-markdown text sent
to Slack/Chat into HTML. For a real report — a formatted table, embedded or
hotlinked images, its own layout — build the HTML yourself and call
`send_email()` directly instead:

```python
from warehouse import notify
ok = notify.send_email(
    subject="Weekly Top Sellers",
    html_body="<table>...</table>",
    to=["team@example.com"],
    plaintext_body="Weekly Top Sellers (see HTML version)",  # optional
    cc=["manager@example.com"],                              # optional
    attachments=["reports/top_sellers.csv"],                 # optional file paths
)
if not ok:
    ...  # the email is usually the only copy of this report — handle the failure
```

The message is `multipart/alternative` (plain text first if given, then the
HTML, both UTF-8); `cc` addresses get a `Cc:` header and receive the message.

**Attachments.** `attachments` is an optional list of file paths (str or
`Path`). When given, the message becomes `multipart/mixed`: the
text/html alternative part first, then each file as a base64
`application/octet-stream` part whose filename is the file's basename. Every
file is read before any SMTP connection is opened, so an unreadable or
missing path **raises** (`OSError`) rather than returning `False`. The
exception is when SMTP isn't configured or `to` is empty: those checks run
first and return `False` before any attachment is read. No MIME
type guessing or size limit is applied — keep attachments within your SMTP
provider's message-size cap (about 25 MB for Gmail). Without `attachments`
the message shape is unchanged.

Unlike `send()`, a missing SMTP config (`SMTP_USER` or `SMTP_PASSWORD`
unset), an empty `to` list, or an exhausted retry loop is **not** swallowed —
it's reported back via the `False` return value (and printed), since an email
built this way is typically the deliverable itself rather than a side
notification of a report that exists elsewhere. It returns `True` on success.

Retries: up to `SMTP_SEND_RETRIES` (3) attempts, sleeping
`SMTP_RETRY_BACKOFF_SECONDS × attempt` (15 s, then 30 s) between them. Any
exception is retried — including a permanent one such as a bad password, so a
misconfigured login takes ~45 s to report `False`. The SMTP socket timeout is
`SMTP_TIMEOUT_SECONDS` (60 s), longer than the webhook path, since a large
image-heavy HTML body can take longer to send. These three are module
constants, not environment variables.

Uses the same `SMTP_*` variables as above — no separate configuration needed.

Sending through the Gmail API's own draft/send path is deliberately avoided
for this: Gmail's compose sanitizer strips remote `<img src="...">` tags on
save, which silently breaks any report built around hotlinked product images.
Sending a hand-built MIME message directly over SMTP bypasses that rewrite,
and also works from an unattended scheduled job (no interactive OAuth).

## Backups — `backup_db.py`

Makes a same-disk rotating copy of `warehouse.db` using SQLite's online
backup API, which is safe to run against a live WAL database — a concurrent
sync can keep writing while the backup runs. Useful once your warehouse holds
history that's aged out of your source platforms' own API retention (or
current-state tables that can never be re-pulled).

```bash
python backup_db.py
```

There are no command-line flags; configuration is by environment (the script
calls `load_dotenv()`, so `.env` works; all three are listed, commented out,
in `.env.example`):

| Variable | Purpose | Default |
|---|---|---|
| `WAREHOUSE_DB` | path to the SQLite file (used throughout the repo, not just here). A relative value is resolved against the **script's directory** (the project root), not the current working directory. | `warehouse.db` in the project root |
| `WAREHOUSE_BACKUP_DIR` | where copies are written (created if missing) | `warehouse-backups/` next to (not inside) the project directory |
| `WAREHOUSE_BACKUP_KEEP` | how many complete copies to keep. Use `1` or more — `0` makes rotation delete every copy, including the one just made. | `3` |

Copies are named `warehouse-YYYY-MM-DD.db` (local date). Running twice on
the same day replaces that day's file rather than adding another. Rotation
sorts by filename, i.e. by date, and deletes the oldest; each deletion also
removes the file's `-wal`/`-shm`/`-journal` siblings. This protects against
corruption or an accidental delete, **not** disk loss — add your own offsite
upload step after it if you need one.

The copy is written to `warehouse-YYYY-MM-DD.db.partial` (source opened
read-only, 4,096 pages per step so no long lock is held), then verified by
opening it read-only and checking that at least one table exists. Only then
is it atomically renamed over the final name, so yesterday's file survives
until today's verifies. A backup that fails to verify (can't open, no
tables) is discarded rather than kept, so a corrupt copy never silently
replaces a good one. Wire it into your OS's scheduler (Task Scheduler, cron,
launchd) to run before your main sync job.

Order of operations, and why:

1. Sweep any stale `warehouse-*.db.partial` (plus crumbs) left by an earlier
   interrupted run — the rotation glob `warehouse-*.db` doesn't match them.
2. Rotate down to `KEEP` *before* copying, so a run never needs more than
   `KEEP+1` copies of headroom at once.
3. Check free space in the backup directory against the source database's
   size × 1.10 (`FREE_MARGIN`). If that's tight and `KEEP > 1`, print a
   warning and rotate to `KEEP-1` — one fewer backup beats a full disk, which
   can stop every other writer on the machine. (This is the one path that
   deletes a known-good backup before the new one has verified.)
4. If free space is still short — including when `KEEP` is already `1` and
   there's no lower rung — the run is skipped with nothing copied.
5. Copy, verify, rename.
6. Rotate a *second* time, bringing the count back from the temporary
   `KEEP+1` down to `KEEP`.

`sync_log` (platform `warehouse_backup`):

| Outcome | `status` | `rows_written` | Exit |
|---|---|---|---|
| Backup made and verified | `ok` | `1`; message gives size, file name, copies kept, GB free | 0 |
| Not enough disk even after rotating down | `error` (`insufficient disk: ...`) | 0 | 1, prints `backup skipped — ...` |
| Verification failed | `error` | 0 | 1, prints `backup verification failed ... — kept nothing` |
| Any other exception during copy (including Ctrl-C) | `error` (`<ExceptionType>: ...`) | 0 | re-raised (traceback) |

The partial file is removed on every failure path. One gap: if the source
database file doesn't exist, the script fails on its size check before
anything is logged, so that case shows only as a traceback / nonzero exit.

## BigQuery mirror — `bq_mirror_sync.py`

Copies warehouse tables from SQLite into a BigQuery dataset on a schedule.
Use it if something reads the warehouse from BigQuery as well as (or instead
of) the MCP server: a BI tool, a hosted MCP connector, a notebook. Nothing
else in the repo needs it.

Why a job and not a one-time export: a BigQuery table with no scheduled
writer looks healthy for weeks. It answers queries; it has just stopped at
the day it was copied, and nothing errors. `bq_mirror_registry.py` is the
explicit list of the tables this job owns, so "who keeps this table fresh?"
always has an answer.

```bash
python bq_mirror_sync.py --set facts               # one set
python bq_mirror_sync.py --set all --dry-run       # export + plan, write nothing
python bq_mirror_sync.py --only orders --since 2026-01-01   # backfill a window table
```

### Setup

```bash
pip install google-cloud-bigquery
gcloud auth application-default login     # or GOOGLE_APPLICATION_CREDENTIALS=<service-account key>
```

| Variable | Notes |
|---|---|
| `BQ_PROJECT` | GCP project id. Required. |
| `BQ_DATASET` | Destination dataset. Required; must already exist. |
| `BQ_LOCATION` | Dataset location for load/query jobs. Default `US`. |

Auth is Google Application Default Credentials. For an unattended scheduled
task, point `GOOGLE_APPLICATION_CREDENTIALS` at a service-account key. The
principal needs **BigQuery Data Editor** on the dataset and **BigQuery Job
User** on the project.

### The registry — `bq_mirror_registry.py`

`SETS` maps a set name to a list of `Mirror(...)` entries. The defaults cover
the core tables from `warehouse/schema.sql` (`facts`: `orders`,
`shopify_order_discounts`, `shopify_order_customers`, `ad_metrics`; `log`:
`sync_log`). Add any standalone connector's tables you want in BigQuery.
`--set all` runs every set in order; keep `log` last so it carries the rows
the other sets just logged.

| Field | Default | Meaning |
|---|---|---|
| `table` | — | Same name in SQLite and BigQuery. |
| `mode` | `full` | `full` or `window` (below). |
| `column` | — | `window` only: the date column (TEXT starting `YYYY-MM-DD`). |
| `window_days` | `7` | `window`: trailing days rewritten every run. |
| `reconcile_days` | `90` | `window`: how far back to compare per-day counts. |
| `allow_empty` | `False` | An empty SQLite table is legitimate (`ok`), not `degraded`. |
| `create_if_missing` | `False` | Create a missing BigQuery table from the SQLite schema. Off by default so a registry typo cannot create junk tables. |

Give every BigQuery table exactly **one** writer. If another job (a per-feed
export, a scheduled query) already writes a table, leave it out of the
registry; two writers on one table end up overwriting each other.

### How a table is written

The BigQuery table's **live schema is the contract**. Columns SQLite has and
BigQuery lacks are added (nullable) before the load, typed TEXT → STRING,
INTEGER → INTEGER, REAL → FLOAT. Columns BigQuery has and SQLite lacks load as
NULL and are reported in the run note. Nothing is hand-declared, so a column
added to SQLite later cannot drift from a copied DDL.

- **`full`** — one `WRITE_TRUNCATE` load of the whole table. Atomic, and
  self-healing: a row deleted in SQLite disappears from BigQuery too. Right
  for tables up to a few million rows.
- **`window`** — rewrites a set of days:
  - the trailing `window_days`;
  - any day within `reconcile_days` whose row count differs between SQLite and
    BigQuery (a historical re-pull or a deleted day heals on its own);
  - the gap since BigQuery's newest day, so a missed night heals on the next
    run. The catch-up is capped at 60 days (`MAX_CATCHUP_DAYS`) unless
    `--since` is given — loading older history is a deliberate act.

  A day-**partitioned** BigQuery table (partitioned on the same column) gets one
  `WRITE_TRUNCATE` load per day into `table$YYYYMMDD`, including an empty load
  for a day SQLite no longer has, and no DML. An **unpartitioned** table gets
  the rows loaded into a temporary staging table (expires after 2 h, deleted
  in any case) and then one transaction, `DELETE <days>; INSERT <staging>`, so
  readers never see the days missing. A table partitioned on a different
  column than the registry's `column` is refused.

  Tip: setting `reconcile_days` to span a table's whole history (e.g. `1200`)
  makes the first load and any later historical re-pull heal through the
  count-drift pass, with no manual `--since`. A table created by
  `create_if_missing` is unpartitioned.

SQLite is opened **read-only**, so the mirror never takes the writer lock and
can run while a sync is writing.

### Outcomes and `sync_log`

One `sync_log` row per table, platform `mirror_<table>_bq` (none on
`--dry-run`), so `last_sync_status` shows the mirror's freshness too.

| Outcome | `status` |
|---|---|
| Loaded, and BigQuery's count (job `output_rows`, or per-day counts after a window load) matches what was exported | `ok` |
| SQLite table (or the window) is empty and the entry is not `allow_empty` — BigQuery is left untouched | `degraded` |
| Count mismatch after the load, BigQuery table missing without `create_if_missing`, a value that cannot convert (e.g. `3.5` into an INTEGER column), or any other exception | `error` |

One table failing never stops the rest. The process exits `1` if any table is
`error`; `degraded` alone exits `0`. Run each set as its own scheduled job
after the nightly syncs.

## Tests

- `tests/test_notify.py` — `to_chat_markdown`, per-dest target lookup,
  email subject derivation, `to_email_html`, SMTP config defaults,
  `send_email()` (multipart shape, attachments turning the message
  `multipart/mixed` while no attachments keeps it `multipart/alternative`,
  cc, retry, exhausted retries return `False`), and `send()` (skips
  unconfigured dests, never raises, one failing
  platform doesn't block others, truncation).
- `tests/test_backup_db.py` — backup + verification, rejecting an empty
  source database, rotation (keeps newest N, removes `-wal`/`-shm` crumbs),
  partial sweeping, and the disk guard (refuses to start, drops to `KEEP-1`,
  rotation happens before any copy).
- `tests/test_bq_mirror_sync.py` (skipped without `google-cloud-bigquery`;
  uses the in-memory fake in `tests/bq_fakes.py`) — value conversion, the
  day planner (window, gap catch-up and its cap, `--since`, count drift and
  the reconcile range), full replace with a column SQLite gained, empty
  sources staying `degraded` without wiping BigQuery, count mismatches as
  `error`, `create_if_missing` (and that it stays opt-in and never fires on a
  dry run), the one-transaction window replace, per-day partition loads,
  partition-column mismatch, one `sync_log` row per table, the default
  registry against `warehouse/schema.sql`, and the env checks.
