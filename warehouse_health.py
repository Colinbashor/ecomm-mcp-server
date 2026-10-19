"""Read-only operational health checks for the warehouse.

    python warehouse_health.py                   # human-readable report
    python warehouse_health.py --json            # machine-readable issues
    python warehouse_health.py --print-config    # dump the effective config
    python warehouse_health.py --config my_health.json

Run it last in your scheduled pipeline. `health_alerts.py` imports
`collect_issues` from here, so there is ONE definition of health rather than a
second that drifts from it.

THREE SEVERITIES, AND THE EXIT CODE IS DRIVEN BY `ERROR` ALONE.

  ERROR     ours, actionable now                              -> exit 1
  DEGRADED  a known upstream condition, on an aging clock      -> exit 0
  WARN      recoverable or historical data-quality drift       -> exit 0

A two-state version (any non-ok status = failure) cannot tell a connector's
deliberate graceful pause (exit 75, `degraded`) or a platform's normal
publishing lag from a crash. It then goes red every day, nobody reads it, and
a genuine outage sits behind the always-red light unnoticed. A light that is
always red is not a light.

DEGRADED IS CLASSIFICATION, NOT SUPPRESSION. A platform that has gone
`escalate_after_days` without a successful run is an ERROR again, whatever its
latest status says: "upstream is flaky this morning" and "nobody has looked at
this in five weeks" look identical in the data, and only the clock separates
them. Days since the last OK, never a consecutive-run counter -- an
intermittent feed resets a counter on one lucky run and would never escalate.

WHAT IS CHECKED (each list is configurable; see docs/health.md):

  sync_status:<platform>  latest sync_log row per platform: error -> ERROR;
                          degraded/paused -> DEGRADED until the escalation
                          clock runs out; no run within that platform's own
                          max_days_between_runs -> ERROR.
  freshness:<name>        the newest date in a table, against a per-table
                          limit. A table can go stale while its sync keeps
                          logging `ok`, which is exactly the failure that
                          status checks cannot see.
  volume:<platform>       consecutive successful runs that wrote 0 rows. A
                          connector answering "did the request succeed?"
                          instead of "did any data arrive?" logs `ok` on an
                          empty pull forever.
  snapshot_gaps:<name>    missing days in a current-state snapshot table.
                          These have no backfill: a missed day is gone (WARN,
                          because there is nothing left to do about it).
  historical_gaps:<name>  missing days inside a daily series (WARN).

UNCONFIGURED CONNECTORS STAY QUIET. Every connector in this repo is optional,
so the built-in lists name all of them. With `only_seen_platforms` (the
default), a declared platform that has never written a sync_log row is
skipped, as is any freshness/gap check tied to it -- someone running three
platforms must not see errors for the thirteen they never set up. Once a
platform HAS run, its going quiet is a finding. Set `only_seen_platforms` to
false once your config lists exactly the feeds you run, and absence from day
one becomes an ERROR too.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from warehouse import db

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "health.json"

ERROR = "ERROR"
DEGRADED = "DEGRADED"
WARN = "WARN"
SEVERITY_RANK = {ERROR: 3, DEGRADED: 2, WARN: 1}

# Statuses a connector writes for a DELIBERATE pause on an upstream fault (the
# exit-75 convention). Anything else that is not ok/success is an error.
TOLERATED_STATUSES = {"degraded", "paused"}
OK_STATUSES = {"ok", "success"}

# How long a platform may sit degraded before it is treated as ours. A week
# lets a self-healing upstream blip (a 5xx window, a late report) clear on its
# own, and still escalates a pause that needs a person -- a support ticket, a
# re-authorization -- before the gap becomes unrecoverable on a feed with a
# retention floor.
ESCALATE_AFTER_DAYS = 7

# Default max days between runs for a daily feed: a weekend plus one bad
# morning. Weekly and monthly feeds get their own values below.
STALE_RUN_DAYS = 3

# Only judge zero-row streaks this far back. Older than this and the platform
# has stopped running rather than running empty, which is sync_status's job.
VOLUME_WINDOW_DAYS = 14

# How far back to look for missing days in snapshot tables.
SNAPSHOT_LOOKBACK_DAYS = 21


@dataclass(frozen=True)
class Issue:
    severity: str
    check: str
    message: str


@dataclass(frozen=True)
class FreshnessSpec:
    name: str
    sql: str
    max_age_days: int
    # The sync_log platform that FEEDS this table, where one owns it. A table
    # cannot be fresh while its own writer is deliberately paused, so its
    # staleness is then a CONSEQUENCE of that platform's status, not a second
    # independent fault -- reporting both turns one upstream outage into
    # several red lines. While the owner is currently degraded (and inside its
    # escalation clock) this reports DEGRADED. While the owner reports `ok`
    # it stays ERROR: a sync that logs success while its table goes stale is
    # the exact failure this check exists for, so never set an owner whose
    # sync can log `ok` while writing nothing.
    owner_platform: str | None = None
    # Activation only: with only_seen_platforms, skip this spec until this
    # platform has logged at least one sync_log row. No effect on severity.
    platform: str | None = None


@dataclass(frozen=True)
class VolumeSpec:
    platform: str
    # Consecutive SUCCESSFUL runs allowed to write nothing before it is an
    # error. 2 tolerates a quiet weekend on a feed that is merely sparse.
    max_zero_streak: int = 2


@dataclass(frozen=True)
class SnapshotSpec:
    name: str
    table: str
    date_column: str = "snapshot_date"
    platform: str | None = None


@dataclass(frozen=True)
class SeriesSpec:
    name: str
    sql: str  # must return one date per row
    platform: str | None = None


# --------------------------------------------------------------------------
# Built-in defaults: the public connectors. Override any list in health.json.
# --------------------------------------------------------------------------

# platform -> max days between runs before "no run in N days" is an ERROR.
# Names are the sync_log platform each script writes. Daily feeds get
# STALE_RUN_DAYS; weekly feeds a week plus slack; monthly a month plus slack.
DEFAULT_STATUS_PLATFORMS: dict[str, int] = {
    # run_sync.py's six shared-table connectors
    "google": STALE_RUN_DAYS,
    "meta": STALE_RUN_DAYS,
    "amazon": STALE_RUN_DAYS,
    "amazon_orders": STALE_RUN_DAYS,
    "shopify": STALE_RUN_DAYS,
    "tiktok": STALE_RUN_DAYS,
    # standalone daily feeds
    "ga4": STALE_RUN_DAYS,
    "google_detail": STALE_RUN_DAYS,
    "google_structure": STALE_RUN_DAYS,
    "meta_ads_detail": STALE_RUN_DAYS,
    "amazon_ads_detail": STALE_RUN_DAYS,
    "amazon_inventory": STALE_RUN_DAYS,
    "amazon_awd": STALE_RUN_DAYS,
    "amazon_returns": STALE_RUN_DAYS,
    "flexport": STALE_RUN_DAYS,
    "flexport_orders": STALE_RUN_DAYS,
    "flexport_returns": STALE_RUN_DAYS,
    "flexport_inbounds": STALE_RUN_DAYS,
    "purple_dot": STALE_RUN_DAYS,
    "purple_dot_waitlists": STALE_RUN_DAYS,
    "gmc_performance": STALE_RUN_DAYS,
    "gmc_status": STALE_RUN_DAYS,
    "gmc_pricing": STALE_RUN_DAYS,
    "gmc_bestsellers": STALE_RUN_DAYS,
    "gmc_visibility": STALE_RUN_DAYS,
    # Search Console retention is a ROLLING ~16 months: a stall nobody
    # notices silently loses the OLD end of the history for good.
    "search_console": STALE_RUN_DAYS,
    "search_console_queries": STALE_RUN_DAYS,
    "search_console_pages": STALE_RUN_DAYS,
    "search_console_query_pages": STALE_RUN_DAYS,
    "shopify_customers": STALE_RUN_DAYS,
    "tiktok_finance": STALE_RUN_DAYS,
    "warehouse_backup": STALE_RUN_DAYS,
    # weekly
    "amazon_sqp": 9,
    # monthly
    "amazon_search_terms_monthly": 40,
}

_FRESHNESS_DEFAULTS: list[dict[str, Any]] = [
    {"name": "google_ads", "platform": "google", "max_age_days": 2,
     "sql": "SELECT MAX(date) FROM ad_metrics WHERE platform='google'"},
    {"name": "meta_ads", "platform": "meta", "max_age_days": 2,
     "sql": "SELECT MAX(date) FROM ad_metrics WHERE platform='meta'"},
    {"name": "amazon_ads", "platform": "amazon", "max_age_days": 3,
     "sql": "SELECT MAX(date) FROM ad_metrics WHERE platform='amazon'"},
    # Order feeds assume a store that takes orders every day; raise these for
    # a low-volume store or a quiet day reads as an outage.
    {"name": "shopify_orders", "platform": "shopify", "max_age_days": 2,
     "sql": "SELECT MAX(substr(order_date,1,10)) FROM orders "
            "WHERE platform='shopify'"},
    {"name": "tiktok_orders", "platform": "tiktok", "max_age_days": 2,
     "sql": "SELECT MAX(substr(order_date,1,10)) FROM orders "
            "WHERE platform='tiktok'"},
    {"name": "amazon_orders", "platform": "amazon_orders", "max_age_days": 5,
     "sql": "SELECT MAX(substr(order_date,1,10)) FROM orders "
            "WHERE platform='amazon'"},
    {"name": "ga4", "platform": "ga4", "max_age_days": 2,
     "sql": "SELECT MAX(date) FROM ga_metrics"},
    # Current-state snapshots with no backfill: alarm fast.
    {"name": "amazon_inventory", "platform": "amazon_inventory",
     "max_age_days": 2,
     "sql": "SELECT MAX(snapshot_date) FROM amazon_inventory"},
    {"name": "amazon_awd", "platform": "amazon_awd", "max_age_days": 2,
     "sql": "SELECT MAX(snapshot_date) FROM amazon_awd_inventory"},
    {"name": "flexport_inventory", "platform": "flexport", "max_age_days": 2,
     "sql": "SELECT MAX(snapshot_date) FROM flexport_inventory"},
    {"name": "purple_dot_waitlists", "platform": "purple_dot_waitlists",
     "max_age_days": 2,
     "sql": "SELECT MAX(snapshot_date) FROM purple_dot_waitlist_inventory"},
    # Owned: a paused crawl makes this stale as a consequence, not a 2nd fault.
    {"name": "flexport_order_costs", "platform": "flexport_orders",
     "owner_platform": "flexport_orders", "max_age_days": 4,
     "sql": "SELECT MAX(substr(created_at,1,10)) FROM flexport_order_costs"},
    # Newest return ACTIVITY, never synced_at: a walk that keeps re-reading the
    # same old page refreshes synced_at daily while nothing new ever lands.
    # The floor drops '0000-00-00' placeholders; the ceiling stops a
    # future-dated value holding the light green.
    {"name": "flexport_returns", "platform": "flexport_returns",
     "owner_platform": "flexport_returns", "max_age_days": 3,
     "sql": "SELECT MAX(d) FROM ("
            " SELECT MAX(substr(received_at,1,10)) AS d FROM flexport_returns"
            "  WHERE received_at >= '2000' AND received_at <= date('now','+1 day')"
            " UNION ALL SELECT MAX(substr(inspected_at,1,10)) FROM flexport_returns"
            "  WHERE inspected_at >= '2000' AND inspected_at <= date('now','+1 day')"
            " UNION ALL SELECT MAX(substr(shipped_at,1,10)) FROM flexport_returns"
            "  WHERE shipped_at >= '2000' AND shipped_at <= date('now','+1 day'))"},
    {"name": "gmc_performance", "platform": "gmc_performance",
     "max_age_days": 2,
     "sql": "SELECT MAX(date) FROM gmc_product_performance"},
    {"name": "gmc_pricing", "platform": "gmc_pricing", "max_age_days": 2,
     "sql": "SELECT MAX(snapshot_date) FROM gmc_price_competitiveness"},
    # Two questions, two specs: "did we pull it" (synced_at) and "did anything
    # new arrive" (report_date). A sync can re-write the same stale report
    # every day with a fresh synced_at; only the second spec sees that. The
    # report_date limit is loose because publication itself lags weeks.
    {"name": "gmc_bestsellers", "platform": "gmc_bestsellers",
     "max_age_days": 2,
     "sql": "SELECT MAX(substr(synced_at,1,10)) FROM gmc_best_sellers"},
    {"name": "gmc_bestsellers_report_date", "platform": "gmc_bestsellers",
     "max_age_days": 35,
     "sql": "SELECT MAX(report_date) FROM gmc_best_sellers"},
    # Visibility publishes with a lag of several days that moves. Deliberately
    # NOT owned by gmc_visibility: that sync can log `ok` while writing
    # nothing, so ownership would soften the very outage this catches.
    {"name": "gmc_visibility", "platform": "gmc_visibility",
     "max_age_days": 12,
     "sql": "SELECT MAX(date) FROM gmc_competitive_visibility"},
    # `final` Search Console data lags ~2-3 days.
    {"name": "search_console", "platform": "search_console",
     "max_age_days": 6,
     "sql": "SELECT MAX(date) FROM search_console_daily"},
]

# Feeds whose successful runs should always write rows. A zero streak on a
# feed where an empty answer is legitimate (an incremental window that found
# nothing new, a day with no live shows) would be noise, so those are left out.
# Pipeline platforms (`*_pipeline`) log 0 rows by construction: never list them.
DEFAULT_VOLUME_PLATFORMS = (
    "google", "meta", "amazon", "amazon_orders", "shopify", "tiktok",
    "ga4", "amazon_inventory", "amazon_awd", "flexport",
    "purple_dot_waitlists", "gmc_performance", "gmc_status", "gmc_pricing",
    "gmc_bestsellers", "gmc_visibility",
)

_SNAPSHOT_DEFAULTS: list[dict[str, Any]] = [
    {"name": "amazon_inventory", "table": "amazon_inventory",
     "platform": "amazon_inventory"},
    {"name": "amazon_awd", "table": "amazon_awd_inventory",
     "platform": "amazon_awd"},
    {"name": "flexport_inventory", "table": "flexport_inventory",
     "platform": "flexport"},
    {"name": "purple_dot_waitlists", "table": "purple_dot_waitlist_inventory",
     "platform": "purple_dot_waitlists"},
]

_SERIES_DEFAULTS: list[dict[str, Any]] = [
    {"name": "google_ads", "platform": "google",
     "sql": "SELECT DISTINCT date FROM ad_metrics WHERE platform='google' "
            "ORDER BY date"},
    {"name": "meta_ads", "platform": "meta",
     "sql": "SELECT DISTINCT date FROM ad_metrics WHERE platform='meta' "
            "ORDER BY date"},
    {"name": "ga4", "platform": "ga4",
     "sql": "SELECT DISTINCT date FROM ga_metrics ORDER BY date"},
]

DEFAULT_CONFIG: dict[str, Any] = {
    "escalate_after_days": ESCALATE_AFTER_DAYS,
    "volume_window_days": VOLUME_WINDOW_DAYS,
    "snapshot_lookback_days": SNAPSHOT_LOOKBACK_DAYS,
    "only_seen_platforms": True,
    "status_platforms": DEFAULT_STATUS_PLATFORMS,
    "freshness": _FRESHNESS_DEFAULTS,
    "volume": [{"platform": p, "max_zero_streak": 2}
               for p in DEFAULT_VOLUME_PLATFORMS],
    "snapshot_tables": _SNAPSHOT_DEFAULTS,
    "date_series": _SERIES_DEFAULTS,
    # Dates a series genuinely had no data (verified against the platform),
    # so the gap check stops reporting them: {"google_ads": ["2025-01-01"]}.
    "verified_empty_dates": {},
    # Check ids to drop, shell-style wildcards allowed:
    # ["freshness:gmc_visibility", "historical_gaps:*"]
    "disable": [],
}


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

class ConfigError(ValueError):
    """The health config cannot be used as written."""


@dataclass(frozen=True)
class HealthConfig:
    escalate_after_days: int = ESCALATE_AFTER_DAYS
    volume_window_days: int = VOLUME_WINDOW_DAYS
    snapshot_lookback_days: int = SNAPSHOT_LOOKBACK_DAYS
    only_seen_platforms: bool = True
    status_platforms: Mapping[str, int] = field(default_factory=dict)
    freshness: tuple[FreshnessSpec, ...] = ()
    volume: tuple[VolumeSpec, ...] = ()
    snapshot_tables: tuple[SnapshotSpec, ...] = ()
    date_series: tuple[SeriesSpec, ...] = ()
    verified_empty_dates: Mapping[str, frozenset[date]] = field(
        default_factory=dict)
    disable: tuple[str, ...] = ()


def _int(value: Any, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where}: must be an integer >= {minimum}, "
                          f"got {value!r}")
    return value


def _build(cls, raw: Any, where: str, ints: Iterable[str] = ()):
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be an object, got {raw!r}")
    allowed = set(cls.__dataclass_fields__) | {"description", "_comment"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}")
    kwargs = {k: v for k, v in raw.items()
              if k in cls.__dataclass_fields__}
    for key in ints:
        if key in kwargs:
            kwargs[key] = _int(kwargs[key], f"{where}.{key}")
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def parse_config(raw: Mapping[str, Any] | None = None) -> HealthConfig:
    """Built-in defaults, with each top-level key in `raw` REPLACING its
    default wholesale (lists are not merged -- use `--print-config` to start
    from the defaults, or `disable` to drop a single check)."""
    merged = copy.deepcopy(DEFAULT_CONFIG)
    if raw:
        if not isinstance(raw, Mapping):
            raise ConfigError("health config must be a JSON object")
        unknown = sorted(set(raw) - set(DEFAULT_CONFIG) - {"_comment"})
        if unknown:
            raise ConfigError(f"unknown key(s) {unknown}; allowed: "
                              f"{sorted(DEFAULT_CONFIG)}")
        merged.update({k: v for k, v in raw.items() if k != "_comment"})

    status = merged["status_platforms"]
    if isinstance(status, list):  # a bare list means "default cadence"
        status = {name: STALE_RUN_DAYS for name in status}
    if not isinstance(status, dict):
        raise ConfigError("status_platforms: must be an object of "
                          "{platform: max_days_between_runs}")
    status = {str(k): _int(v, f"status_platforms.{k}", 1)
              for k, v in status.items()}

    def specs(key, cls, ints=()):
        value = merged[key]
        if not isinstance(value, list):
            raise ConfigError(f"{key}: must be a list")
        return tuple(_build(cls, item, f"{key}[{i}]", ints)
                     for i, item in enumerate(value))

    empty = merged["verified_empty_dates"]
    if not isinstance(empty, dict):
        raise ConfigError("verified_empty_dates: must be an object")
    verified: dict[str, frozenset[date]] = {}
    for name, days in empty.items():
        try:
            verified[name] = frozenset(date.fromisoformat(d) for d in days)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"verified_empty_dates.{name}: {exc}") from exc

    disable = merged["disable"]
    if not isinstance(disable, list) or not all(isinstance(d, str)
                                                for d in disable):
        raise ConfigError("disable: must be a list of check ids")
    if not isinstance(merged["only_seen_platforms"], bool):
        raise ConfigError("only_seen_platforms: must be true or false")

    return HealthConfig(
        escalate_after_days=_int(merged["escalate_after_days"],
                                 "escalate_after_days", 1),
        volume_window_days=_int(merged["volume_window_days"],
                                "volume_window_days", 1),
        snapshot_lookback_days=_int(merged["snapshot_lookback_days"],
                                    "snapshot_lookback_days", 1),
        only_seen_platforms=merged["only_seen_platforms"],
        status_platforms=status,
        freshness=specs("freshness", FreshnessSpec, ("max_age_days",)),
        volume=specs("volume", VolumeSpec, ("max_zero_streak",)),
        snapshot_tables=specs("snapshot_tables", SnapshotSpec),
        date_series=specs("date_series", SeriesSpec),
        verified_empty_dates=verified,
        disable=tuple(disable),
    )


def config_path() -> Path:
    env = os.environ.get("WAREHOUSE_HEALTH_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> HealthConfig:
    """Read the health config; a missing file means "use the defaults"."""
    path = Path(path) if path else config_path()
    if not path.exists():
        return parse_config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    return parse_config(raw)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def today_local(now: datetime | None = None) -> date:
    """The warehouse's calendar day: WAREHOUSE_TIMEZONE if set, else this
    machine's local time."""
    if now is not None:
        return now.date()
    tz = os.environ.get("WAREHOUSE_TIMEZONE")
    return (datetime.now(ZoneInfo(tz)) if tz
            else datetime.now().astimezone()).date()


def _date_value(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _seen_platforms(conn: sqlite3.Connection) -> set[str]:
    try:
        return {row[0] for row in
                conn.execute("SELECT DISTINCT platform FROM sync_log")}
    except sqlite3.Error:
        return set()


def _active(platform: str | None, seen: set[str] | None) -> bool:
    """Whether a check tied to `platform` should run. `seen` is None in strict
    mode (everything declared is checked)."""
    return seen is None or platform is None or platform in seen


def _last_ok_age(conn: sqlite3.Connection, platform: str,
                 today: date) -> int | None:
    """Days since `platform` last logged a successful run.

    Falls back to the age of its OLDEST run when it has never succeeded, so a
    brand-new feed is not escalated on its first bad morning while one that has
    never worked is escalated eventually. None when the platform has no rows.
    """
    row = conn.execute(
        "SELECT MAX(started_at) FROM sync_log "
        "WHERE platform=? AND lower(status) IN ('ok','success')",
        (platform,),
    ).fetchone()
    last_ok = _date_value(row[0] if row else None)
    if last_ok is not None:
        return (today - last_ok).days
    row = conn.execute(
        "SELECT MIN(started_at) FROM sync_log WHERE platform=?", (platform,),
    ).fetchone()
    first_seen = _date_value(row[0] if row else None)
    return None if first_seen is None else (today - first_seen).days


def _ever_ok(conn: sqlite3.Connection, platform: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sync_log WHERE platform=? "
        "AND lower(status) IN ('ok','success') LIMIT 1", (platform,),
    ).fetchone() is not None


def _latest_status(conn: sqlite3.Connection, platform: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM sync_log WHERE platform=? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (platform,),
    ).fetchone()
    return str(row[0]).lower() if row and row[0] is not None else None


def _compress_dates(days: list[date]) -> str:
    if not days:
        return ""
    ranges: list[str] = []
    start = previous = days[0]

    def emit() -> None:
        ranges.append(start.isoformat() if start == previous
                      else f"{start.isoformat()}..{previous.isoformat()}")

    for current in days[1:]:
        if current == previous + timedelta(days=1):
            previous = current
            continue
        emit()
        start = previous = current
    emit()
    return ", ".join(ranges)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_sync_log_present(conn: sqlite3.Connection) -> list[Issue]:
    """An empty warehouse must not read as a healthy one. With
    only_seen_platforms every other check skips what has never run, so
    without this a fresh or wiped database would come back all-clear."""
    try:
        count = conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0]
    except sqlite3.Error as exc:
        return [Issue(ERROR, "sync_log", f"cannot read sync_log: {exc} "
                      "(has any sync ever run against this database?)")]
    if not count:
        return [Issue(ERROR, "sync_log",
                      "sync_log is empty - no sync has ever been recorded")]
    return []


def check_freshness(
    conn: sqlite3.Connection,
    today: date,
    specs: Sequence[FreshnessSpec],
    escalate_after_days: int = ESCALATE_AFTER_DAYS,
    seen: set[str] | None = None,
) -> list[Issue]:
    """Age-check each tracked table.

    A spec whose `owner_platform` is CURRENTLY paused reports DEGRADED rather
    than ERROR, and still escalates on the owner's clock.
    """
    issues: list[Issue] = []
    for spec in specs:
        if not _active(spec.platform, seen):
            continue
        check = f"freshness:{spec.name}"
        try:
            value = conn.execute(spec.sql).fetchone()[0]
        except sqlite3.Error as exc:
            issues.append(Issue(ERROR, check, f"query failed: {exc}"))
            continue
        latest = _date_value(value)
        if latest is None:
            # "Nothing has EVER arrived" is a different finding from "what
            # arrived is old", and wording it like a broken query hides it.
            issues.append(Issue(
                ERROR, check,
                "never recorded - this feed has produced nothing, ever "
                "(not stale: absent)"))
            continue
        age = (today - latest).days
        if age <= spec.max_age_days:
            continue
        detail = (f"latest {latest.isoformat()} is {age} days old "
                  f"(limit {spec.max_age_days})")
        severity = ERROR
        # The owner's clock alone is not enough: a writer that alternates
        # ok/paused resets "days since last ok" on every lucky run while the
        # table falls further behind. So the TABLE's own lag also caps the
        # softening: past limit + escalate_after_days it is an ERROR whatever
        # the owner says.
        if (spec.owner_platform
                and age <= spec.max_age_days + escalate_after_days):
            owner_state = _latest_status(conn, spec.owner_platform)
            owner_age = _last_ok_age(conn, spec.owner_platform, today)
            if owner_state in TOLERATED_STATUSES and (
                    owner_age is None or owner_age <= escalate_after_days):
                severity = DEGRADED
                detail += (f" - consequence of {spec.owner_platform} being "
                           f"{owner_state}, not an independent fault")
        issues.append(Issue(severity, check, detail))
    return issues


def check_recent_statuses(
    conn: sqlite3.Connection,
    today: date,
    platforms: Mapping[str, int] | Sequence[str],
    escalate_after_days: int = ESCALATE_AFTER_DAYS,
    seen: set[str] | None = None,
) -> list[Issue]:
    """Classify each declared platform's most recent run.

      ok / success           -> silent.
      error (or anything     -> ERROR. Ours.
        unrecognised)
      degraded / paused      -> DEGRADED until the platform has gone
                                escalate_after_days with no successful run,
                                then ERROR naming the age.
      no run within the      -> ERROR. A check that skips old platforms goes
        platform's limit        SILENT exactly when a feed dies.
      no row at all          -> ERROR in strict mode (seen=None): this
                                iterates the DECLARED list, not the query
                                results, so a platform that vanished from
                                sync_log (renamed, dropped from the pipeline)
                                is a finding. Skipped when `seen` is given and
                                the platform has never run (unconfigured).
    """
    if not isinstance(platforms, Mapping):
        platforms = {name: STALE_RUN_DAYS for name in platforms}
    names = list(platforms)
    if not names:
        return []
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT platform, started_at, finished_at, status, message,
                 ROW_NUMBER() OVER (
                   PARTITION BY platform ORDER BY started_at DESC, id DESC
                 ) AS rn
          FROM sync_log
          WHERE platform IN ({placeholders})
        )
        SELECT platform, started_at, finished_at, status, message
        FROM ranked WHERE rn=1
        """,
        tuple(names),
    ).fetchall()
    latest = {row[0]: row[1:] for row in rows}

    issues: list[Issue] = []
    for platform, max_days in platforms.items():
        check = f"sync_status:{platform}"
        record = latest.get(platform)
        if record is None:
            if seen is not None:
                continue
            issues.append(Issue(
                ERROR, check,
                "no sync_log row has ever been written for this platform "
                "(renamed, dropped from the schedule, or never configured?)"))
            continue
        started_at, finished_at, status, message = record
        # finished_at falls back to started_at: a run killed mid-flight may
        # never write a finish, and that is the run most worth seeing.
        last = _date_value(finished_at) or _date_value(started_at)
        if last is None:
            issues.append(Issue(ERROR, check, "latest run has no usable timestamp"))
            continue
        age = (today - last).days
        if age > max_days:
            issues.append(Issue(
                ERROR, check,
                f"no run in {age} days (last {last.isoformat()}, status "
                f"{status}, limit {max_days})"))
            continue
        state = str(status).lower()
        if state in OK_STATUSES:
            continue
        detail = message or "no message"
        if state not in TOLERATED_STATUSES:
            issues.append(Issue(ERROR, check,
                                f"latest status is {status}: {detail}"))
            continue
        since = _last_ok_age(conn, platform, today)
        if since is not None and since > escalate_after_days:
            issues.append(Issue(
                ERROR, check,
                f"{status} for {since} days with no successful run "
                f"(escalated past {escalate_after_days}d - this needs a "
                f"person, not another retry): {detail}"))
            continue
        if since is None:
            window = "age unknown"
        elif _ever_ok(conn, platform):
            window = f"{since}d since last ok"
        else:
            window = f"never ok, first run {since}d ago"
        issues.append(Issue(
            DEGRADED, check,
            f"{status} ({window}, tolerated to {escalate_after_days}d): "
            f"{detail}"))
    return issues


def check_volume(
    conn: sqlite3.Connection,
    today: date,
    specs: Sequence[VolumeSpec],
    window_days: int = VOLUME_WINDOW_DAYS,
) -> list[Issue]:
    """Catch a feed that reports success while writing nothing.

    A STREAK, NOT A FLOOR. Healthy feeds legitimately write 0 rows on some
    days, and a broken one may have averaged only a few hundred rows before it
    broke -- so a `min_rows` threshold both false-positives on the healthy and
    misses the broken.
    """
    window = (today - timedelta(days=window_days)).isoformat()
    issues: list[Issue] = []
    for spec in specs:
        rows = conn.execute(
            """
            SELECT started_at, rows_written FROM sync_log
            WHERE platform=? AND lower(status) IN ('ok','success')
              AND substr(started_at,1,10) >= ?
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (spec.platform, window, spec.max_zero_streak + 1),
        ).fetchall()
        # Not enough recent successes to judge. A platform that stopped running
        # is sync_status's finding; reporting it twice is noise.
        if len(rows) <= spec.max_zero_streak:
            continue
        if any((written or 0) > 0 for _, written in rows):
            continue
        oldest = _date_value(rows[-1][0])
        since = f" since {oldest.isoformat()}" if oldest else ""
        issues.append(Issue(
            ERROR, f"volume:{spec.platform}",
            f"last {len(rows)} successful runs wrote 0 rows{since} - "
            f"reporting success while delivering no data"))
    return issues


def check_snapshot_gaps(
    conn: sqlite3.Connection,
    today: date,
    specs: Sequence[SnapshotSpec],
    lookback_days: int = SNAPSHOT_LOOKBACK_DAYS,
    seen: set[str] | None = None,
) -> list[Issue]:
    start = today - timedelta(days=lookback_days)
    end = today - timedelta(days=1)
    expected = {(start + timedelta(days=offset)).isoformat()
                for offset in range((end - start).days + 1)}
    issues: list[Issue] = []
    for spec in specs:
        if not _active(spec.platform, seen):
            continue
        check = f"snapshot_gaps:{spec.name}"
        col, table = spec.date_column, spec.table
        try:
            present = {row[0][:10] for row in conn.execute(
                f'SELECT DISTINCT "{col}" FROM "{table}" '
                f'WHERE "{col}" BETWEEN ? AND ?',
                (start.isoformat(), end.isoformat() + "￿"),
            ) if row[0]}
            first_seen = conn.execute(
                f'SELECT MIN("{col}") FROM "{table}"').fetchone()[0]
        except sqlite3.OperationalError:
            issues.append(Issue(WARN, check,
                                f"table {table} does not exist - the feed has "
                                f"never run"))
            continue
        # A snapshot cannot exist from before the feed's first run, so those
        # days are not gaps. Without this a newly-added feed warns every day
        # for the whole lookback window and trains people to ignore it.
        expected_days = expected
        if first_seen:
            expected_days = {d for d in expected if d >= str(first_seen)[:10]}
        missing = sorted(expected_days - present)
        if missing:
            issues.append(Issue(
                WARN, check,
                f"{len(missing)} missing day(s): "
                f"{_compress_dates([date.fromisoformat(d) for d in missing])}"))
    return issues


def check_historical_date_gaps(
    conn: sqlite3.Connection,
    specs: Sequence[SeriesSpec],
    verified_empty: Mapping[str, Iterable[date]] | None = None,
    seen: set[str] | None = None,
) -> list[Issue]:
    verified_empty = verified_empty or {}
    issues: list[Issue] = []
    for spec in specs:
        if not _active(spec.platform, seen):
            continue
        check = f"historical_gaps:{spec.name}"
        try:
            present = sorted({parsed for value, in conn.execute(spec.sql)
                              if (parsed := _date_value(value)) is not None})
        except sqlite3.Error as exc:
            issues.append(Issue(WARN, check, f"query failed: {exc}"))
            continue
        if len(present) < 2:
            continue
        expected = {present[0] + timedelta(days=offset)
                    for offset in range((present[-1] - present[0]).days + 1)}
        missing = sorted(expected - set(present)
                         - set(verified_empty.get(spec.name, ())))
        if missing:
            issues.append(Issue(
                WARN, check,
                f"{len(missing)} missing day(s): {_compress_dates(missing)}"))
    return issues


def _disabled(check: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(check, p) for p in patterns)


def collect_issues(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    config: HealthConfig | None = None,
) -> list[Issue]:
    config = config or parse_config()
    today = today_local(now)
    seen = _seen_platforms(conn) if config.only_seen_platforms else None

    issues = check_sync_log_present(conn)
    if issues:
        # Every other check reads sync_log or depends on it for activation.
        return issues
    issues.extend(check_recent_statuses(
        conn, today, config.status_platforms, config.escalate_after_days, seen))
    issues.extend(check_freshness(
        conn, today, config.freshness, config.escalate_after_days, seen))
    issues.extend(check_volume(conn, today, config.volume,
                               config.volume_window_days))
    issues.extend(check_snapshot_gaps(conn, today, config.snapshot_tables,
                                      config.snapshot_lookback_days, seen))
    issues.extend(check_historical_date_gaps(
        conn, config.date_series, config.verified_empty_dates, seen))
    issues = [i for i in issues if not _disabled(i.check, config.disable)]
    # Most serious first: the actionable lines must not sit below a standing
    # DEGRADED condition someone has already decided to live with.
    issues.sort(key=lambda i: (-SEVERITY_RANK.get(i.severity, 0), i.check))
    return issues


def open_readonly() -> sqlite3.Connection:
    """The warehouse, opened so SQLite itself rejects writes."""
    if not db.DB_PATH.exists():
        raise FileNotFoundError(f"warehouse database not found: {db.DB_PATH} "
                                "(set WAREHOUSE_DB, or run a sync first)")
    return db.connect_readonly()


def exit_code(issues: Sequence[Issue], warnings_as_errors: bool = False) -> int:
    # ERROR ALONE decides the exit code. DEGRADED is a known upstream
    # condition with a clock on it; returning nonzero for one is what turns a
    # health signal into permanent noise.
    if any(issue.severity == ERROR for issue in issues):
        return 1
    if warnings_as_errors and issues:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="print issues as JSON")
    parser.add_argument("--config", type=Path,
                        help="health config JSON (default: $WAREHOUSE_HEALTH_"
                             "CONFIG, else health.json; missing = defaults)")
    parser.add_argument("--print-config", action="store_true",
                        help="print the built-in defaults as JSON (a starting "
                             "point for health.json) and exit")
    parser.add_argument("--warnings-as-errors", action="store_true",
                        help="exit 1 on any issue, not just ERROR")
    args = parser.parse_args(argv)

    if args.print_config:
        print(json.dumps(DEFAULT_CONFIG, indent=2))
        return 0
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"health config error: {exc}", file=sys.stderr)
        return 2
    try:
        conn = open_readonly()
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"[ERROR] warehouse: {exc}", file=sys.stderr)
        return 1
    try:
        issues = collect_issues(conn, config=config)
    finally:
        conn.close()

    if args.json:
        print(json.dumps([asdict(issue) for issue in issues], indent=2))
    else:
        for issue in issues:
            print(f"[{issue.severity}] {issue.check}: {issue.message}",
                  flush=True)
        counts = {s: sum(i.severity == s for i in issues)
                  for s in (ERROR, DEGRADED, WARN)}
        print(f"Warehouse health: {counts[ERROR]} error(s), "
              f"{counts[DEGRADED]} degraded, {counts[WARN]} warning(s)",
              flush=True)
    return exit_code(issues, args.warnings_as_errors)


if __name__ == "__main__":
    raise SystemExit(main())
