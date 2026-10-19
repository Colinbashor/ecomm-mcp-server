"""Notify on a CHANGE in warehouse health -- never on the standing state.

    python health_alerts.py              # diff, record, push if anything is new
    python health_alerts.py --dry-run    # print the decision; record and send nothing
    python health_alerts.py --force      # push the full current state once

`warehouse_health.py` decides what is wrong. This decides what is NEW, because
those are different questions and conflating them is what breaks an alarm: a
scheduler's "last result" is not a monitoring system, and a daily message that
always says something is wrong stops being read within a couple of weeks.

So this pushes ONLY:

  * an ERROR or DEGRADED check that was not there last run
  * an escalation -- a check whose severity went UP (the aging DEGRADED
    condition that has now become an ERROR is the case that matters)
  * a recovery, which is what keeps the channel worth reading
  * a weekly digest of standing DEGRADED conditions, so a long pause stays
    visible without being mentioned every morning

Silence is the expected output on a healthy day, and equally on a day when the
same known conditions are still true. That silence is the signal.

A brand-new WARN is recorded but not pushed: warnings describe historical
data-quality drift (gaps in old data) and are not worth waking anyone for.
They still appear in warehouse_health's own report.

SAME MODEL AS THE HEALTH REPORT: this imports `collect_issues` rather than
re-deriving anything, so the two surfaces can never disagree about whether the
warehouse is healthy.

STATE LIVES IN THE WAREHOUSE (`health_state` table, one row per open check)
and is written BEFORE the push. A chat outage must not lose the record,
because the record is what suppresses tomorrow's duplicate; re-running after a
failed push is then safe and silent.

DELIVERY goes through `warehouse/notify.py` to the destination named by
`--dest` (default `warehouse_health`), i.e. whichever of these env vars are
set: WAREHOUSE_HEALTH_SLACK_WEBHOOK, WAREHOUSE_HEALTH_GCHAT_WEBHOOK,
WAREHOUSE_HEALTH_EMAIL_TO (+ the global SMTP_* settings for email). None set
is not an error: the change is still recorded and printed.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

import warehouse_health as wh
from warehouse import db, notify

SYNC_PLATFORM = "health_alerts"
DEFAULT_DEST = "warehouse_health"

# Standing DEGRADED conditions are re-announced on this weekday (0=Monday) so
# a long upstream pause cannot fade out of view entirely. On any other day
# they are recorded silently. -1 disables the digest.
DIGEST_WEEKDAY = 0

DDL = """
CREATE TABLE IF NOT EXISTS health_state (
    check_name  TEXT NOT NULL PRIMARY KEY,
    severity    TEXT NOT NULL,
    message     TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    notified_on TEXT
);
"""


def load_state(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        row[0]: {"severity": row[1], "message": row[2], "first_seen": row[3],
                 "last_seen": row[4], "notified_on": row[5]}
        for row in conn.execute(
            "SELECT check_name, severity, message, first_seen, last_seen, "
            "notified_on FROM health_state")
    }


def classify(issues: Sequence[wh.Issue],
             previous: dict[str, dict]) -> dict[str, list]:
    """Split the current issue set into new / escalated / resolved / standing.

    A check is NEW when it was absent before, ESCALATED when it was present at
    a LOWER severity -- which is why severity is compared rather than mere
    presence.
    """
    current = {issue.check: issue for issue in issues}
    new: list = []
    escalated: list = []
    standing: list = []
    for check, issue in current.items():
        before = previous.get(check)
        if before is None:
            (standing if issue.severity == wh.WARN else new).append(issue)
            continue
        was = wh.SEVERITY_RANK.get(before["severity"], 0)
        now = wh.SEVERITY_RANK.get(issue.severity, 0)
        if now > was:
            escalated.append((issue, before["severity"], before["first_seen"]))
        else:
            standing.append(issue)

    # A check missing from the current set has recovered. Only worth saying
    # if it was announced before or had reached ERROR -- a WARN that quietly
    # came and went never needs a "recovered" line.
    resolved = [
        (check, before) for check, before in previous.items()
        if check not in current
        and (before["notified_on"] or before["severity"] == wh.ERROR)
    ]
    return {"new": new, "escalated": escalated, "resolved": resolved,
            "standing": standing}


def _age(first_seen: str, today: date) -> str:
    seen = wh._date_value(first_seen)
    if seen is None:
        return ""
    days = (today - seen).days
    return " (first seen today)" if days <= 0 else f" (standing {days}d)"


def build_message(buckets: dict[str, list], today: date,
                  digest: bool) -> str | None:
    """Render only what changed. Returns None when there is nothing to say."""
    lines: list[str] = []
    if buckets["new"]:
        lines.append("*NEW*")
        for issue in buckets["new"]:
            lines.append(f"- [{issue.severity}] {issue.check}: {issue.message}")
    if buckets["escalated"]:
        lines.append("*ESCALATED - a tolerated condition has aged out*")
        for issue, was, first_seen in buckets["escalated"]:
            lines.append(f"- [{was} -> {issue.severity}] {issue.check}: "
                         f"{issue.message}{_age(first_seen, today)}")
    if buckets["resolved"]:
        lines.append("*RECOVERED*")
        for check, before in buckets["resolved"]:
            lines.append(f"- {check} (was {before['severity']})")
    if digest:
        held = [i for i in buckets["standing"] if i.severity == wh.DEGRADED]
        if held:
            lines.append("*Still degraded, tolerated for now*")
            for issue in held:
                lines.append(f"- {issue.check}: {issue.message}")
    if not lines:
        return None
    return f"*Warehouse health - {today.isoformat()}*\n" + "\n".join(lines)


def save_state(conn: sqlite3.Connection, issues: Sequence[wh.Issue],
               previous: dict[str, dict], today: date, notified: bool) -> None:
    stamp = today.isoformat()
    current = {issue.check: issue for issue in issues}
    with conn:
        # A recovered check LEAVES the table, so a later recurrence is
        # genuinely NEW again and gets announced, rather than silently
        # matching a stale row and never being mentioned.
        for check in previous:
            if check not in current:
                conn.execute("DELETE FROM health_state WHERE check_name=?",
                             (check,))
        for check, issue in current.items():
            before = previous.get(check)
            notified_on = stamp if notified else (
                before["notified_on"] if before else None)
            conn.execute(
                "INSERT OR REPLACE INTO health_state "
                "(check_name, severity, message, first_seen, last_seen, "
                " notified_on) VALUES (?, ?, ?, ?, ?, ?)",
                (check, issue.severity, (issue.message or "")[:500],
                 before["first_seen"] if before else stamp, stamp,
                 notified_on),
            )


def run(issues: Sequence[wh.Issue], conn: sqlite3.Connection, today: date,
        dest: str = DEFAULT_DEST, dry_run: bool = False, force: bool = False,
        digest_weekday: int = DIGEST_WEEKDAY) -> str | None:
    """Diff `issues` against the stored state, record, and push if changed.

    Returns the message that was (or, with dry_run, would have been) sent,
    or None when there was nothing new to say.
    """
    conn.executescript(DDL)
    previous = load_state(conn)
    buckets = classify(issues, previous)
    counts = {key: len(value) for key, value in buckets.items()}
    print(f"health: {counts['new']} new, {counts['escalated']} escalated, "
          f"{counts['resolved']} recovered, {counts['standing']} standing")

    digest = today.weekday() == digest_weekday
    if force:
        buckets["new"] = [i for i in issues if i.severity != wh.WARN]
        digest = True
    message = build_message(buckets, today, digest)

    if message is None:
        print("Nothing new - staying silent (the normal outcome).")
        if not dry_run:
            save_state(conn, issues, previous, today, notified=False)
        return None

    print()
    print(message)
    if dry_run:
        print("\n[dry-run] nothing recorded, nothing sent")
        return message

    # RECORD FIRST: the record is what suppresses a duplicate tomorrow, so it
    # must survive a failed push.
    save_state(conn, issues, previous, today, notified=True)
    # notify.send is best-effort and never raises; it prints what it did.
    notify.send(message, dest=dest)
    return message


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Notify only when warehouse health changes.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the decision; record nothing, send nothing")
    parser.add_argument("--force", action="store_true",
                        help="send the full current state (e.g. after "
                             "configuring a webhook) and record it")
    parser.add_argument("--dest", default=DEFAULT_DEST,
                        help="notify.py destination name (default "
                             f"{DEFAULT_DEST!r} -> WAREHOUSE_HEALTH_* env vars)")
    parser.add_argument("--config", type=Path,
                        help="health config JSON (same as warehouse_health.py)")
    parser.add_argument("--digest-weekday", type=int, default=DIGEST_WEEKDAY,
                        help="weekday for the standing-DEGRADED digest, "
                             "0=Monday; -1 disables (default 0)")
    args = parser.parse_args(argv)

    started = db.now()
    try:
        config = wh.load_config(args.config)
    except wh.ConfigError as exc:
        print(f"health config error: {exc}", file=sys.stderr)
        return 2
    try:
        ro = wh.open_readonly()
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"[ERROR] warehouse: {exc}", file=sys.stderr)
        return 1
    try:
        issues = wh.collect_issues(ro, config=config)
    finally:
        ro.close()

    today = wh.today_local()
    conn = db.connect()
    try:
        message = run(issues, conn, today, dest=args.dest,
                      dry_run=args.dry_run, force=args.force,
                      digest_weekday=args.digest_weekday)
    finally:
        conn.close()
    if not args.dry_run:
        db.log_sync(SYNC_PLATFORM, started, len(issues), "ok",
                    "change announced" if message else "no change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
