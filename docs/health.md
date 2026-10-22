# Keeping it healthy — pipeline, health checks, alerts

Three scripts turn a set of independent syncs into something you can leave
running unattended and trust:

| Script | Job | Writes |
|---|---|---|
| `pipeline_runner.py` | Runs your configured steps as child processes, in order, with per-step timeouts and one pipeline at a time | one `sync_log` row per step and one per run |
| `warehouse_health.py` | Read-only checks: is every feed running, fresh, and actually delivering rows? Exits 1 on any ERROR | nothing |
| `health_alerts.py` | Pushes a message through `warehouse/notify.py` **only when health changes** | `health_state` table, one `sync_log` row |

None needs credentials of its own, and none touches the network except
`health_alerts.py`'s optional webhook/email push.

The design premise, learned the expensive way: **an alarm that is always red
is not an alarm.** A two-state check that calls every non-`ok` status a
failure goes red on the first upstream hiccup, stays red, gets ignored — and a
real outage then sits behind it unnoticed. Everything below is built to keep
the signal readable: three states instead of two, an aging clock instead of
permanent tolerance, and notifications on change instead of on state.

---

## The three statuses

Every connector writes one of three statuses to `sync_log`, and
`pipeline_runner.py` uses the same three for steps and runs:

| `sync_log.status` | Meaning | Exit code of the script |
|---|---|---|
| `ok` | It ran and delivered data | 0 |
| `degraded` | A deliberate pause on an upstream fault (a platform 5xx window that outlasted the retries, a report not yet published, a rate limit that never cleared) **or** a run that achieved nothing (an empty pull). State is checkpointed; the next run resumes | 75 for a pause |
| `error` | Ours: a bug, bad credentials, a schema problem | non-zero |

Exit **75** is the repo-wide convention for "paused, not failed". It only means
that for a script designed around it (checkpoint + resume); from anything else
it is a failure, which is why the pipeline tolerates it per step.

## The three health severities

`warehouse_health.py` reads `sync_log` and the tables and reports issues at
three severities. **Only ERROR drives the exit code.**

| Severity | Meaning | Exit | Pushed by `health_alerts.py` |
|---|---|---|---|
| **ERROR** | Ours and actionable now: a sync erroring, a feed that stopped running, a table gone stale while its sync says `ok`, successful runs writing 0 rows, or a DEGRADED condition that has aged past the escalation clock | 1 | when new, when escalated |
| **DEGRADED** | A known upstream condition still inside its clock: a sync currently `degraded`/`paused`, or a table stale *because* its owning sync is paused | 0 | when new; again in the weekly digest |
| **WARN** | Historical or unrecoverable data-quality drift: missing days in a snapshot table (no backfill exists) or inside a daily series | 0 | never (report only) |

### Escalation: classification, not suppression

A `degraded` platform reads DEGRADED only until it has gone
`escalate_after_days` (default **7**) **without a successful run**, then it is
an ERROR again, naming the age ("degraded for 9 days with no successful run —
this needs a person, not another retry"). "Upstream is flaky this morning" and
"nobody has looked at this in five weeks" look identical in the data; only the
clock separates them.

Two details matter:

- The clock is **days since the last `ok`**, never a consecutive-failure count.
  A feed that alternates `ok`/`degraded` would reset a counter on every lucky
  run and never escalate.
- A platform that has **never** succeeded is aged from its first run, so a new
  feed is not escalated on its first bad morning, but one that has never worked
  is escalated eventually.

A stale table whose owning sync is paused is DEGRADED (one upstream outage
should be one line, not three) — but only while the owner is inside its clock
**and** the table is less than `max_age_days + escalate_after_days` behind. An
owner that logs `ok` every few days while advancing slowly cannot keep its
table soft forever.

### What is checked

| Check id | Finds | Severity |
|---|---|---|
| `sync_log` | `sync_log` missing or empty — an empty warehouse must not read as healthy | ERROR |
| `sync_status:<platform>` | latest run is `error`; no run within the platform's own `max_days_between_runs`; `degraded` past the clock (ERROR) or inside it (DEGRADED); in strict mode, a declared platform with no row at all | ERROR / DEGRADED |
| `sync_status:<job>` (scheduled jobs) | a declared non-daily job never ran; its latest run is older than its own `max_age_days`; or its latest status is outside `expected_statuses` | ERROR |
| `freshness:<name>` | the newest date in a table is older than `max_age_days`; or the table has never produced anything | ERROR / DEGRADED (owned) |
| `volume:<platform>` | the last *N*+1 **successful** runs (inside 14 days) all wrote 0 rows — a sync answering "did the request succeed?" instead of "did any data arrive?" | ERROR |
| `snapshot_gaps:<name>` | missing days in a current-state snapshot table over the last 21 days (days before the table's first snapshot don't count) | WARN |
| `historical_gaps:<name>` | missing days inside a daily series, minus `verified_empty_dates` | WARN |

Staleness is checked **per platform**: daily feeds default to 3 days between
runs (a weekend plus one bad morning), weekly feeds 9, monthly 40. A check that
skipped platforms whose last run was old would go silent exactly when a feed
died; this one reports it.

**Non-daily jobs** (a weekly replenishment run, a weekdays-only report) go in
`scheduled_jobs` instead. Each is judged on its own cadence and is either
silent or ERROR, with no DEGRADED tier: a job whose healthy runs log `degraded`
by design ("a proposal is waiting for review") lists it in
`expected_statuses`, because the escalation clock would otherwise go red
waiting for an `ok` the job never writes. A declared job that has never run is
an ERROR regardless of `only_seen_platforms`: declaring it says it runs. Don't
list a job in both `status_platforms` and `scheduled_jobs`.

### Unconfigured connectors stay quiet

Every connector here is optional, so the built-in lists name all of them. With
`only_seen_platforms: true` (the default) a declared platform that has **never
written a sync_log row** is skipped, along with every freshness/gap check tied
to it — someone running three platforms sees nothing about the other thirteen.
Once a platform *has* run, its going quiet is a finding.

When your config lists exactly the feeds you run, set
`only_seen_platforms: false`: then a declared platform with no row at all is an
ERROR from day one (renamed, dropped from the schedule, or never set up).

### Usage

```bash
python warehouse_health.py                  # report; exit 1 if any ERROR
python warehouse_health.py --json           # issues as JSON
python warehouse_health.py --warnings-as-errors
python warehouse_health.py --print-config > health.json   # start a config
```

It opens the database read-only (`mode=ro`) and never creates it; a missing
database is reported as an error. "Today" is the machine's local date, or
`WAREHOUSE_TIMEZONE` (an IANA name) if set.

### Configuration — `health.json`

Optional. Looked up at `WAREHOUSE_HEALTH_CONFIG`, else `health.json` beside the
code (gitignored); a missing file means the built-in defaults.
`health.example.json` shows a small override. **Each top-level key replaces its
default wholesale** — lists are not merged — so start from `--print-config` if
you want to tweak one entry, or use `disable` to drop a single check.

| Key | Default | Meaning |
|---|---|---|
| `escalate_after_days` | `7` | days a platform may go without an `ok` before DEGRADED becomes ERROR |
| `only_seen_platforms` | `true` | skip declared platforms (and checks tied to them) that have never logged a row |
| `status_platforms` | the public connectors | `{platform: max_days_between_runs}`; a plain list means 3 days each |
| `freshness` | ad/order/snapshot tables of the public connectors | list of `{name, sql, max_age_days, owner_platform?, platform?}`; `sql` returns one date; `platform` only gates activation, `owner_platform` enables the paused-owner softening |
| `volume` | feeds that should always write rows | list of `{platform, max_zero_streak}` (default streak 2) |
| `volume_window_days` | `14` | how far back zero-row streaks are judged |
| `snapshot_tables` | inventory / waitlist snapshots | list of `{name, table, date_column?, platform?}` |
| `snapshot_lookback_days` | `21` | window for snapshot gaps |
| `date_series` | Google/Meta ad days, GA4 days | list of `{name, sql, platform?}`; `sql` returns one date per row |
| `scheduled_jobs` | `[]` | list of `{platform, max_age_days, expected_statuses?}`; `max_age_days` = the longest normal gap between runs plus slack; `expected_statuses` defaults to `["ok", "success"]` |
| `verified_empty_dates` | `{}` | `{series_name: ["YYYY-MM-DD", ...]}` days verified to have no data |
| `disable` | `[]` | check ids to drop, shell wildcards allowed: `["freshness:gmc_visibility", "historical_gaps:*"]` |

Unknown keys and wrongly typed values are rejected rather than ignored — a
typo silently falling back to a default is the kind of plausible-but-wrong
outcome this tooling exists to prevent. Every spec accepts a `description`
field for your own notes.

Two rules when writing your own specs:

- **Never give a freshness spec an `owner_platform` whose sync can log `ok`
  while writing nothing.** Ownership softens staleness while the owner is
  paused; a sync that reports success on empty pulls would hide exactly the
  outage the spec is for.
- **Freshness should read the data's own date, not `synced_at`.** A sync that
  re-reads the same old page every day keeps `synced_at` current while nothing
  new arrives. Where "did we pull it" and "did anything new arrive" are both
  worth asking, use two specs.

Order feeds in the defaults assume a store that takes orders every day; raise
`max_age_days` for a low-volume store or a quiet weekend will read as an
outage.

---

## Alerts — `health_alerts.py`

`warehouse_health.py` decides what is wrong; `health_alerts.py` decides what is
**new**. It pushes only:

- an ERROR or DEGRADED check that was not there last run;
- an **escalation** — a check whose severity went up (typically DEGRADED aging
  into ERROR);
- a **recovery** of anything that was announced or had reached ERROR;
- on Mondays, a digest of standing DEGRADED conditions, so a long pause stays
  visible without being repeated every morning (`--digest-weekday N`, 0 =
  Monday, `-1` to disable).

Silence is the normal output, both on a healthy day and on a day when the same
known problems are still true. Identity is the check id and change is the
severity, so a message whose text ticks from "8 days old" to "9 days old" does
not re-announce. A brand-new WARN is recorded but never pushed.

State lives in the `health_state` table of the warehouse (one row per open
check: severity, first/last seen, last notified). It is written **before** the
push, so a failed send cannot produce a duplicate tomorrow, and a recovered
check leaves the table so a recurrence is announced as new again.

### Setup

Recipients come only from environment variables (`.env`), through
`warehouse/notify.py`'s per-destination scheme. The default destination is
`warehouse_health`:

| Variable | Notes |
|---|---|
| `WAREHOUSE_HEALTH_SLACK_WEBHOOK` | Slack Incoming Webhook URL |
| `WAREHOUSE_HEALTH_GCHAT_WEBHOOK` | Google Chat space webhook URL |
| `WAREHOUSE_HEALTH_EMAIL_TO` | comma-separated addresses; needs the global `SMTP_*` settings |

Set any subset. With none set, changes are still recorded and printed, just not
pushed. `--dest NAME` sends to `NAME_*` variables instead. See
[operations.md](operations.md#notifications--warehousenotifypy) for webhook and
SMTP details.

```bash
python health_alerts.py --dry-run   # show what would be sent; records nothing
python health_alerts.py             # diff, record, push if anything changed
python health_alerts.py --force     # push the full current state once
```

Because state is recorded even when no target is configured, use `--force`
once after adding a webhook if you want the current standing issues delivered.

---

## The pipeline — `pipeline_runner.py`

```bash
python pipeline_runner.py --list              # pipelines in the config
python pipeline_runner.py daily --dry-run     # print the plan; run nothing
python pipeline_runner.py daily               # run it
python pipeline_runner.py daily --config my_pipeline.json
```

Steps come from `pipeline.json` (yours, gitignored) or, if absent, the
checked-in `pipeline.example.json`. Copy the example and delete the steps for
platforms you don't use: `run_sync.py` skips connectors without credentials,
but a standalone script without credentials fails.

```json
{
  "pipelines": {
    "daily": {
      "lock_wait_minutes": 360,
      "max_concurrent": 4,
      "waves": [
        {"description": "independent pulls",
         "steps": [
           {"script": "run_sync.py", "args": ["--days", "3"], "timeout_minutes": 240},
           {"script": "flexport_orders_sync.py", "tolerate_pause": true}
         ]},
        [{"script": "backup_db.py"}],
        [{"script": "warehouse_health.py"}, {"script": "health_alerts.py"}]
      ]
    }
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `waves` | required | ordered list; each wave is a list of steps (or `{"description", "steps"}`). Steps in a wave run **concurrently**; waves run in order |
| `script` | required | a `.py` file, relative to the repo root (absolute allowed), run with the same interpreter as the runner — never through a shell |
| `args` | `[]` | a **list** of strings: `["--days", "3"]`, not `"--days 3"` |
| `timeout_minutes` | `180` | per-step budget; on expiry the step's whole process tree is killed |
| `tolerate_pause` | `false` | exit 75 from this step counts as `degraded`, not `error`. Only for scripts that checkpoint and resume |
| `name` | script + args | label in logs and in the step's `sync_log` platform |
| `lock_wait_minutes` | `0` | how long to wait for another running pipeline before giving up |
| `max_concurrent` | `4` | cap on simultaneous steps in a wave |
| `launch_stagger_seconds` | `5` | gap between launches within a wave |

The config is validated before anything runs; unknown keys, `args` given as a
string, duplicate steps and missing scripts are errors (exit 2).

**Waves are about read-after-write order, not table ownership.** Put a step in
a later wave when it reads what an earlier step writes. Steps in one wave can
safely share the database because writers queue behind
`db.BUSY_TIMEOUT_SECONDS` — SQLite's write lock is database-wide, so concurrent
writers always contend however disjoint their tables. Keep heavy writers in
separate waves if contention gets long.

**Outcomes.** Every step writes a `sync_log` row (platform
`<pipeline>_pipeline:<name>`, `rows_written` 0) and the run writes one
(`<pipeline>_pipeline`):

| Run status | When | Exit |
|---|---|---|
| `ok` | every step exited 0 | 0 |
| `degraded` | the only non-zero exits were tolerated pauses | 0 |
| `error` | any other non-zero exit, or a timeout | 1 |
| `degraded` ("skipped") | another pipeline held the lock past `lock_wait_minutes` | 75 |

A failing step never stops the run — the remaining independent feeds still
refresh. A timeout is never tolerated, whatever the step declares: a hang is
not a pause.

**Timeouts kill the tree.** On Windows the runner uses `taskkill /T /F`; on
Linux and macOS each step runs in its own process group (`start_new_session`)
and the group gets SIGTERM, then SIGKILL after 10 s. Killing only the direct
child is not enough: a virtualenv's `python.exe` on Windows is a launcher stub
whose real interpreter is a child process, and a step may spawn workers of its
own. Size each budget from the step's **slowest healthy run** in `sync_log`,
never its typical one — a budget set from the median kills a healthy slow day.

**One pipeline at a time.** An OS file lock (`warehouse.db.pipeline.lock`,
beside the database) serialises pipelines, including two checkouts pointed at
one database. The OS releases it if the runner dies, so it cannot go stale.

---

## Scheduling

Run the pipeline once a day, early enough that the platforms have published
the previous day. Use the virtualenv's interpreter by absolute path and run
from the repo directory so `.env` is found. Long pipelines must run as a
scheduled job, **not** from an SSH session: closing the session kills the whole
process tree, leaving a truncated log that looks exactly like a crash.

### Windows — Task Scheduler

```powershell
$repo = "C:\path\to\ecomm-mcp-server"
$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\python.exe" `
    -Argument "pipeline_runner.py daily" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At 4:30am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName "warehouse daily pipeline" `
    -Action $action -Trigger $trigger -Settings $settings
```

- Registering a task usually needs an **elevated** PowerShell, and
  `Get-ScheduledTask` only lists tasks your token may see — a task missing from
  an unelevated listing is not proof it is gone.
- `-StartWhenAvailable` runs a missed start after a reboot or sleep.
- Run it as the user whose credentials the syncs need (for example a
  credential store tied to that user), with "run whether user is logged on or
  not" if the machine is unattended.
- To keep a log, wrap the command: `-Execute "cmd.exe" -Argument "/c
  .venv\Scripts\python.exe pipeline_runner.py daily >> pipeline_log.txt 2>&1"`.

### Linux — cron

```cron
# m  h  dom mon dow  command
30   4  *   *   *    cd /path/to/ecomm-mcp-server && .venv/bin/python pipeline_runner.py daily >> pipeline_log.txt 2>&1
```

cron runs with a minimal environment and its own working directory, hence the
`cd` and the absolute interpreter. A systemd timer works equally well.

### macOS — launchd

Save as `~/Library/LaunchAgents/local.warehouse.pipeline.plist`, then
`launchctl load ~/Library/LaunchAgents/local.warehouse.pipeline.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.warehouse.pipeline</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/ecomm-mcp-server/.venv/bin/python</string>
    <string>pipeline_runner.py</string>
    <string>daily</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/ecomm-mcp-server</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/path/to/ecomm-mcp-server/pipeline_log.txt</string>
  <key>StandardErrorPath</key><string>/path/to/ecomm-mcp-server/pipeline_log.txt</string>
</dict>
</plist>
```

launchd runs a missed calendar job when the Mac wakes.

### Without the pipeline

If you schedule syncs individually instead, schedule `warehouse_health.py` and
then `health_alerts.py` after the last of them. The health check needs nothing
from the runner; it only reads `sync_log` and the tables.

### Watching the pipeline itself

The health defaults do not list `<pipeline>_pipeline` platforms. A pipeline
that stops firing is still caught — every feed it runs goes stale — and a
pipeline whose last step is `warehouse_health.py` already logs `error` whenever
health does, so alarming on it too would repeat every ERROR twice. Add
`"daily_pipeline": 2` to `status_platforms` if you want "the scheduler never
fired" reported under its own name.

---

## Tests

`tests/test_pipeline_runner.py`, `tests/test_warehouse_health.py` and
`tests/test_health_alerts.py` — hermetic: temp SQLite files, tiny throwaway
step scripts, `notify.send` patched out. They cover:

- tolerated pauses vs. failures vs. timeouts, run/step `sync_log` rows, a busy
  lock logging `degraded` with exit 75, a real cross-platform file lock, strict
  config validation, and that the example config only references scripts in
  this repo;
- a real tree kill: a step that spawns a worker and sleeps past its budget,
  whose worker must stop when the step is killed;
- DEGRADED inside the clock, ERROR past it (and per-platform clocks), stopped
  and vanished platforms, zero-row streaks, owner softening and its cap, gap
  checks, `only_seen_platforms`, `disable`, and that every default freshness
  table is one this repo creates;
- change-only delivery: the same state twice sends once; new → silent →
  recovered → silent → recurring sends exactly three messages; an escalation
  sends once; the digest repeats standing DEGRADED only on its weekday; state is
  written before the push.
