# Operations

Two standalone utilities that aren't connectors — no platform credentials,
no data pulled from anywhere. Both are optional.

## Notifications — `warehouse/notify.py`

A small utility any script (or your own code) can call to push a plain-text
or lightly-formatted message to Slack, Google Chat, and/or email. Useful for
a "sync finished, here's a summary" ping, or turning any sync script into an
alert when something needs attention.

```python
from warehouse import notify
notify.send("*Sync complete*\n- 1,204 rows written\nFull report: https://...", dest="daily_sync")
```

Targets are configured per named `dest` in `.env`:

| Variable pattern | Notes |
|---|---|
| `<DEST>_SLACK_WEBHOOK` | e.g. `DAILY_SYNC_SLACK_WEBHOOK` |
| `<DEST>_GCHAT_WEBHOOK` | e.g. `DAILY_SYNC_GCHAT_WEBHOOK` |
| `<DEST>_EMAIL_TO` | e.g. `DAILY_SYNC_EMAIL_TO` |
| `SMTP_HOST` / `SMTP_PORT` | global, only needed if any `dest` sets `_EMAIL_TO`. Defaults assume Gmail's SMTP relay. |
| `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | global. If the account has 2-Step Verification on, `SMTP_PASSWORD` must be a Google **app password** (myaccount.google.com/apppasswords), not the normal account password. |

Different callers can point at different channels/spaces/inboxes with no code
change, and a `dest` with nothing configured is silently skipped — `send()`
never raises, so a notification failure can't take down whatever pipeline
called it. See the module docstring for full setup (webhook creation, SMTP
setup for email).

## Backups — `backup_db.py`

Makes a same-disk rotating copy of `warehouse.db` using SQLite's online
backup API, which is safe to run against a live WAL database — a concurrent
sync can keep writing while the backup runs. Useful once your warehouse holds
history that's aged out of your source platforms' own API retention and so
can't simply be re-pulled.

```bash
python backup_db.py
```

| Variable | Purpose | Default |
|---|---|---|
| `WAREHOUSE_DB` | path to the SQLite file (used throughout the repo, not just here) | `warehouse.db` beside the code |
| `WAREHOUSE_BACKUP_DIR` | where copies are written | `warehouse-backups/` next to (not inside) the project directory |
| `WAREHOUSE_BACKUP_KEEP` | how many copies to keep | `3` |

A backup that fails to verify (can't open, no tables) is discarded rather
than kept, so a corrupt copy never silently replaces a good one. Wire it into
your OS's scheduler (Task Scheduler, cron, launchd) to run before your main
sync job.

## Tests

`tests/test_notify.py`, `tests/test_backup_db.py`
