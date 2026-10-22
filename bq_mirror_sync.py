r"""Mirror warehouse SQLite tables into a BigQuery dataset.

    python bq_mirror_sync.py --set facts
    python bq_mirror_sync.py --set all --dry-run
    python bq_mirror_sync.py --only orders --since 2026-01-01

WHAT / WHY: see bq_mirror_registry.py. In short: if the warehouse is also
served from BigQuery, every BigQuery table needs a scheduled writer, or it
silently freezes at whatever day it was last copied. This is that writer for
every table listed in `bq_mirror_registry.SETS`. It is optional — nothing else
in the repo needs BigQuery.

SETUP
  pip install google-cloud-bigquery
  .env:  BQ_PROJECT=<gcp project id>   BQ_DATASET=<dataset>   (BQ_LOCATION=US)
  Auth is Google Application Default Credentials: `gcloud auth
  application-default login` interactively, or GOOGLE_APPLICATION_CREDENTIALS
  pointing at a service-account key for an unattended scheduled task. The
  principal needs BigQuery Data Editor on the dataset and Job User on the
  project. The dataset must already exist; tables are created only for
  registry entries with `create_if_missing=True`.

HOW A TABLE IS WRITTEN
  The BigQuery table's LIVE schema is the contract (nothing is hand-declared, so
  a column added to SQLite cannot drift from a copy of its DDL). Columns SQLite
  has and BigQuery lacks are ADDED (nullable) before the load, typed TEXT ->
  STRING, INTEGER -> INTEGER, REAL -> FLOAT; columns BigQuery has and SQLite
  lacks load as NULL and are reported.
  full    one WRITE_TRUNCATE load job of the whole table -- atomic.
  window  rewrite a set of DAYS. Partitioned table: one WRITE_TRUNCATE load per
          day into `table$YYYYMMDD` (no DML). Unpartitioned table: load the rows
          into a staging table, then ONE transaction `DELETE days; INSERT staging`
          so readers never see the days missing. The days are the trailing
          `window_days`, plus any day in `reconcile_days` whose row count differs
          between SQLite and BigQuery, plus the gap since BigQuery's newest day.
          A missed night therefore heals itself on the next run (capped at
          MAX_CATCHUP_DAYS unless --since says otherwise). A partition that is
          empty on BOTH sides is skipped rather than truncated, so a weekly table
          costs one load per week, not seven.
  where   optional registry predicate: every SQLite read (export AND counts) goes
          through it, so a filtered-out row is simply absent from BigQuery.

CHECKS
  * every table is verified against what was exported (job output_rows / per-day
    counts after the load) and a mismatch is an `error`, never silently `ok`;
  * an empty SQLite table is `degraded` and the BigQuery copy is left alone,
    unless the registry marks it allow_empty (an empty pull is not success);
  * what gets rewritten is decided by COMPARING COUNTS, never by "absence of
    rows" — a day can legitimately have none;
  * one sync_log row per table, platform `mirror_<table>_bq`, so the warehouse's
    own freshness checks (the `last_sync_status` MCP tool) see the mirror too;
  * one table failing never stops the rest; the process exits 1 if any table
    is `error` (degraded alone exits 0).

READS ONLY from SQLite (a read-only connection): it never takes the writer lock,
so it cannot block, or lose a race with, a sync that is running at the same time.

SCHEDULING
  Run each set as its own scheduled job after the nightly syncs (e.g. `facts`
  then `log`). Window tables tolerate a skipped night; full tables are
  rewritten whole every run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

try:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
except ImportError as exc:  # pragma: no cover -- exercised only without the optional dep
    raise SystemExit("bq_mirror_sync.py needs the optional BigQuery client: "
                     "pip install google-cloud-bigquery") from exc

from bq_mirror_registry import SETS, Mirror, all_mirrors, platform_for
from warehouse import db as warehouse_db

load_dotenv()

REQUIRED_ENV = ("BQ_PROJECT", "BQ_DATASET")

# A table whose BigQuery copy is further behind than this is rewritten only this
# far back unless --since is given (a first-ever history load must be a deliberate act).
MAX_CATCHUP_DAYS = 60

_INT = {"INTEGER", "INT64"}
_FLOAT = {"FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}
_BOOL = {"BOOLEAN", "BOOL"}
_TS = {"TIMESTAMP", "DATETIME"}


class MirrorError(RuntimeError):
    pass


def check_required_env() -> None:
    """Fail fast and legibly on a misconfigured .env instead of a vague
    BigQuery 'project not found' deep inside the first load."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


def bq_target() -> tuple[str, str, str]:
    """(project, dataset, location), read from the environment at call time."""
    return (os.environ.get("BQ_PROJECT", ""), os.environ.get("BQ_DATASET", ""),
            os.environ.get("BQ_LOCATION") or "US")


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------
def convert(value, bq_type: str, column: str = "?"):
    """One SQLite value -> the JSON value BigQuery's loader expects for `bq_type`."""
    if value is None:
        return None
    t = bq_type.upper()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if t in _INT:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, float):
            if not value.is_integer():
                raise MirrorError(f"{column}: {value!r} is not integral for {t}")
            return int(value)
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                return None
            try:
                return int(s)
            except ValueError:
                f = float(s)
                if not f.is_integer():
                    raise MirrorError(f"{column}: {value!r} is not integral for {t}") from None
                return int(f)
        return int(value)
    if t in _FLOAT:
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if t in _BOOL:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "t", "yes")
        return bool(value)
    if t == "DATE":
        s = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}", s):      # month-grain tables ('2026-08') -> first of month
            return s + "-01"
        return s[:10] if s else None
    if t in _TS:
        s = str(value).strip()
        return s if s else None
    return value if isinstance(value, str) else str(value)


def infer_bq_type(sqlite_type: str) -> str:
    """BigQuery type for a SQLite column BigQuery does not have yet
    (TEXT -> STRING, INTEGER -> INTEGER, REAL -> FLOAT; anything else STRING)."""
    t = (sqlite_type or "").upper()
    if "INT" in t:
        return "INTEGER"
    if any(x in t for x in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "FLOAT"
    return "STRING"


def source_sql(m: Mirror) -> str:
    """What every SQLite read of `m` selects FROM: the table, or the table narrowed by
    the registry's `where`. Exports and the counts they are verified against must use
    the same source, or a filtered table would read as permanent count drift."""
    return m.table if not m.where else f"(SELECT * FROM {m.table} WHERE {m.where})"


def date_expr_sqlite(column: str) -> str:
    return f"substr({column}, 1, 10)"


def date_expr_bq(column: str) -> str:
    return f"SUBSTR(CAST({column} AS STRING), 1, 10)"


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def plan_days(*, today: str, sqlite_max: str | None, window_days: int, reconcile_days: int,
              bq_max: str | None, sqlite_counts: dict[str, int], bq_counts: dict[str, int],
              since: str | None = None) -> list[str]:
    """The days to rewrite this run, ascending. Pure so the catch-up logic is testable.

    sqlite_counts / bq_counts: per-day row counts over the reconcile range.
    """
    if sqlite_max is None:
        return []
    end = max(sqlite_max, today)
    t = date.fromisoformat(today)
    window_start = t - timedelta(days=window_days - 1)
    start = window_start
    if since:
        start = min(start, date.fromisoformat(since))
    elif bq_max is not None:
        b = date.fromisoformat(bq_max)
        if b < start:
            floor = t - timedelta(days=MAX_CATCHUP_DAYS)
            start = max(b, floor)
    days: set[str] = set()
    d = start
    while d <= date.fromisoformat(end):
        days.add(d.isoformat())
        d += timedelta(days=1)
    recon_start = (t - timedelta(days=reconcile_days)).isoformat()
    for day in set(sqlite_counts) | set(bq_counts):
        if day >= recon_start and day < start.isoformat() \
                and sqlite_counts.get(day, 0) != bq_counts.get(day, 0):
            days.add(day)
    return sorted(days)


@dataclass
class Result:
    table: str
    status: str = "ok"            # ok | degraded | error
    rows: int = 0
    message: str = ""
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# SQLite / BigQuery access
# --------------------------------------------------------------------------
def open_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(warehouse_db.DB_PATH).as_posix()}?mode=ro", uri=True,
                           timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.execute("PRAGMA query_only = ON")
    return conn


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise MirrorError(f"{table} does not exist in SQLite")
    return [(r[1], r[2]) for r in rows]


class Plan:
    """Column mapping between a SQLite table and its (possibly extended) BigQuery schema."""

    def __init__(self, bq_schema: list[bigquery.SchemaField],
                 sqlite_cols: list[tuple[str, str]]):
        lite = {n: t for n, t in sqlite_cols}
        bq_names = {f.name for f in bq_schema}
        self.added = [bigquery.SchemaField(n, infer_bq_type(t)) for n, t in sqlite_cols
                      if n not in bq_names]
        self.schema = list(bq_schema) + self.added
        self.missing_in_sqlite = [f.name for f in self.schema if f.name not in lite]
        # (sqlite column, bq type) in schema order, only columns SQLite has
        self.columns = [(f.name, f.field_type) for f in self.schema if f.name in lite]


def _ndjson(rows, columns: list[tuple[str, str]], fh) -> int:
    n = 0
    names = [c for c, _ in columns]
    types = [t for _, t in columns]
    dumps = json.dumps
    for r in rows:
        rec = {}
        for i, name in enumerate(names):
            try:
                rec[name] = convert(r[i], types[i], name)
            except MirrorError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise MirrorError(f"{name}: cannot convert {r[i]!r} to {types[i]}: {exc}") from exc
        fh.write(dumps(rec, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        fh.write(b"\n")
        n += 1
    return n


def _load_file(client, path: Path, dest: str, schema, location: str, truncate: bool = True):
    cfg = bigquery.LoadJobConfig(
        schema=schema,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=(bigquery.WriteDisposition.WRITE_TRUNCATE if truncate
                           else bigquery.WriteDisposition.WRITE_APPEND),
    )
    with open(path, "rb") as fh:
        job = client.load_table_from_file(fh, dest, job_config=cfg, location=location)
    job.result()
    return job


def _apply_new_columns(client, table_ref: str, bq_table, plan: Plan, dry_run: bool) -> None:
    if not plan.added or dry_run:
        return
    bq_table.schema = plan.schema
    client.update_table(bq_table, ["schema"])


def _scalar_rows(client, sql: str, params=None, location: str = "US"):
    cfg = bigquery.QueryJobConfig(query_parameters=params) if params else None
    job = client.query(sql, job_config=cfg, location=location) if cfg \
        else client.query(sql, location=location)
    return list(job.result())


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def run_full(client, conn, m: Mirror, bq_table, plan: Plan, table_ref: str,
             tmp: Path, location: str, dry_run: bool) -> Result:
    res = Result(m.table)
    cols = ", ".join(c for c, _ in plan.columns)
    src = source_sql(m)
    src_n = conn.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
    path = tmp / f"{m.table}.ndjson"
    with open(path, "wb") as fh:
        n = _ndjson(conn.execute(f"SELECT {cols} FROM {src}"), plan.columns, fh)
    res.rows = n
    if n == 0 and not m.allow_empty:
        res.status = "degraded"
        res.message = "SQLite table is empty -- BigQuery copy NOT replaced (empty pull is not ok)"
        return res
    if dry_run:
        res.message = f"dry-run: would replace table with {n} rows ({src_n} counted)"
        return res
    _apply_new_columns(client, table_ref, bq_table, plan, dry_run)
    job = _load_file(client, path, table_ref, plan.schema, location)
    loaded = int(job.output_rows if job.output_rows is not None else n)
    if loaded != n:
        res.status = "error"
        res.message = f"loaded {loaded} rows but exported {n}"
    else:
        res.message = f"replaced with {n} rows"
    return res


def _sqlite_day_counts(conn, m: Mirror, since: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT {date_expr_sqlite(m.column)} d, COUNT(*) FROM {source_sql(m)} "
        f"WHERE {m.column} >= ? GROUP BY d", (since,)).fetchall()
    return {d: n for d, n in rows if d}


def _bq_day_counts(client, table_ref: str, m: Mirror, since: str, location: str) -> dict[str, int]:
    expr = date_expr_bq(m.column)
    rows = _scalar_rows(
        client, f"SELECT {expr} AS d, COUNT(*) AS n FROM `{table_ref}` "
                f"WHERE {expr} >= @since GROUP BY d",
        [bigquery.ScalarQueryParameter("since", "STRING", since)], location)
    return {r["d"]: int(r["n"]) for r in rows if r["d"]}


def run_window(client, conn, m: Mirror, bq_table, plan: Plan, table_ref: str,
               tmp: Path, location: str, dry_run: bool, today: str,
               since: str | None) -> Result:
    res = Result(m.table)
    tp = bq_table.time_partitioning
    partitioned = tp is not None and tp.field is not None
    if partitioned and tp.field != m.column:
        raise MirrorError(f"{m.table} is partitioned on {tp.field!r} but the registry "
                          f"windows on {m.column!r}")

    src = source_sql(m)
    sqlite_max = conn.execute(f"SELECT {date_expr_sqlite(m.column)} FROM {src} "
                              f"WHERE {m.column} IS NOT NULL ORDER BY {m.column} DESC LIMIT 1"
                              ).fetchone()
    sqlite_max = sqlite_max[0] if sqlite_max else None
    if sqlite_max is None:
        res.status = "degraded" if not m.allow_empty else "ok"
        res.message = "SQLite table has no dated rows -- nothing to mirror"
        return res

    expr_bq = date_expr_bq(m.column)
    bq_max_row = _scalar_rows(client, f"SELECT MAX({expr_bq}) AS d FROM `{table_ref}`",
                              None, location)
    bq_max = bq_max_row[0]["d"] if bq_max_row and bq_max_row[0]["d"] else None
    recon_start = (date.fromisoformat(today) - timedelta(days=m.reconcile_days)).isoformat()
    if bq_max is None and not since:
        res.notes.append("BigQuery table has no rows: loading the trailing window plus any "
                         "reconcile-range drift (pass --since for older history)")
    s_counts = _sqlite_day_counts(conn, m, recon_start)
    b_counts = _bq_day_counts(client, table_ref, m, recon_start, location)
    days = plan_days(today=today, sqlite_max=sqlite_max, window_days=m.window_days,
                     reconcile_days=m.reconcile_days, bq_max=bq_max, sqlite_counts=s_counts,
                     bq_counts=b_counts, since=since)
    drift = sorted(d for d in days if s_counts.get(d, 0) != b_counts.get(d, 0))
    res.notes.append(f"{len(days)} day(s) {days[0]}..{days[-1]}; bq_max={bq_max}; "
                     f"{len(drift)} with count drift before")

    cols = ", ".join(c for c, _ in plan.columns)
    per_day: dict[str, int] = {}
    files: dict[str, Path] = {}
    for d in days:
        p = tmp / f"{m.table}_{d}.ndjson"
        with open(p, "wb") as fh:
            per_day[d] = _ndjson(conn.execute(
                f"SELECT {cols} FROM {src} WHERE {m.column} >= ? AND {m.column} < ?",
                (d, next_day(d))), plan.columns, fh)
        files[d] = p
    res.rows = sum(per_day.values())
    if res.rows == 0 and not m.allow_empty:
        res.status = "degraded"
        res.message = "window held no rows in SQLite -- nothing written (empty pull is not ok)"
        return res
    if dry_run:
        res.message = f"dry-run: would rewrite {len(days)} day(s), {res.rows} rows"
        return res

    _apply_new_columns(client, table_ref, bq_table, plan, dry_run)
    if partitioned:
        for d in days:
            dest = f"{table_ref}${d.replace('-', '')}"
            # Empty on both sides: nothing to truncate. Only trusted inside the
            # counted range -- a catch-up day older than recon_start has no BigQuery
            # count, so it is still truncated rather than assumed empty.
            if per_day[d] == 0 and d >= recon_start and b_counts.get(d, 0) == 0:
                continue
            if per_day[d] == 0:
                client.load_table_from_json([], dest, job_config=bigquery.LoadJobConfig(
                    schema=plan.schema, write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON),
                    location=location).result()
            else:
                _load_file(client, files[d], dest, plan.schema, location)
    else:
        _replace_days_transaction(client, table_ref, m, plan, days, files, per_day, tmp, location)

    after = _bq_day_counts(client, table_ref, m, days[0], location)
    bad = [f"{d}: sqlite {per_day[d]} vs bq {after.get(d, 0)}" for d in days
           if after.get(d, 0) != per_day[d]]
    if bad:
        res.status = "error"
        res.message = "post-load count mismatch " + "; ".join(bad[:5])
    else:
        res.message = f"rewrote {len(days)} day(s), {res.rows} rows"
    return res


def _replace_days_transaction(client, table_ref, m: Mirror, plan: Plan, days, files, per_day,
                              tmp: Path, location: str) -> None:
    stg = f"{table_ref.rsplit('.', 1)[0]}._stg_mirror_{m.table}_{uuid.uuid4().hex[:12]}"
    merged = tmp / f"{m.table}_window.ndjson"
    with open(merged, "wb") as out:
        for d in days:
            if per_day[d]:
                out.write(files[d].read_bytes())
    try:
        t = bigquery.Table(stg, schema=plan.schema)
        t.expires = datetime.now(timezone.utc) + timedelta(hours=2)
        client.create_table(t, exists_ok=True)
        if sum(per_day.values()):
            _load_file(client, merged, stg, plan.schema, location)
        cols = ", ".join(f"`{f.name}`" for f in plan.schema)
        sql = (
            "BEGIN TRANSACTION;\n"
            f"DELETE FROM `{table_ref}` WHERE {date_expr_bq(m.column)} IN UNNEST(@days);\n"
            f"INSERT INTO `{table_ref}` ({cols}) SELECT {cols} FROM `{stg}`;\n"
            "COMMIT TRANSACTION;"
        )
        cfg = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("days", "STRING", list(days))])
        client.query(sql, job_config=cfg, location=location).result()
    finally:
        client.delete_table(stg, not_found_ok=True)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def create_from_sqlite(client, conn, m: Mirror, table_ref: str) -> bigquery.Table:
    """Create a missing BigQuery table from the SQLite schema (same type mapping
    as a column SQLite gains later: TEXT->STRING, INTEGER->INTEGER, REAL->FLOAT).
    Unpartitioned, so a window mirror uses the one-transaction day replace."""
    schema = [bigquery.SchemaField(n, infer_bq_type(t)) for n, t in sqlite_columns(conn, m.table)]
    return client.create_table(bigquery.Table(table_ref, schema=schema), exists_ok=True)


def mirror_one(client, conn, m: Mirror, project: str, dataset: str, location: str,
               today: str, since: str | None, dry_run: bool) -> Result:
    t0 = time.monotonic()
    res = Result(m.table)
    table_ref = f"{project}.{dataset}.{m.table}"
    try:
        created = False
        try:
            bq_table = client.get_table(table_ref)
        except NotFound:
            if not m.create_if_missing:
                raise
            if dry_run:
                res.message = "dry-run: BigQuery table missing; would create it from the SQLite schema"
                res.seconds = time.monotonic() - t0
                return res
            bq_table = create_from_sqlite(client, conn, m, table_ref)
            created = True
        plan = Plan(bq_table.schema, sqlite_columns(conn, m.table))
        with tempfile.TemporaryDirectory(prefix="bq_mirror_") as td:
            tmp = Path(td)
            if m.mode == "full":
                res = run_full(client, conn, m, bq_table, plan, table_ref, tmp, location, dry_run)
            else:
                res = run_window(client, conn, m, bq_table, plan, table_ref, tmp, location,
                                 dry_run, today, since)
        if created:
            res.notes.append("created the BigQuery table from the SQLite schema")
        if plan.added:
            res.notes.append("added BQ column(s): " + ", ".join(f.name for f in plan.added))
        if plan.missing_in_sqlite:
            res.notes.append("NULL (not in SQLite): " + ", ".join(plan.missing_in_sqlite))
    except Exception as exc:  # noqa: BLE001 -- one table must never stop the rest
        res.status = "error"
        res.message = f"{type(exc).__name__}: {exc}"[:300]
    res.seconds = time.monotonic() - t0
    return res


def run(mirrors: list[Mirror], *, dry_run: bool = False, since: str | None = None,
        client=None, conn=None, today: str | None = None) -> list[Result]:
    project, dataset, location = bq_target()
    client = client or bigquery.Client(project=project)
    own_conn = conn is None
    conn = conn or open_sqlite()
    today = today or date.today().isoformat()
    results: list[Result] = []
    try:
        for m in mirrors:
            started = warehouse_db.now()
            r = mirror_one(client, conn, m, project, dataset, location, today, since, dry_run)
            results.append(r)
            note = f" [{'; '.join(r.notes)}]" if r.notes else ""
            line = f"{m.table:34s} {r.status:8s} {r.rows:>10,} rows {r.seconds:6.1f}s  {r.message}{note}"
            print(line, flush=True)
            if not dry_run:
                try:
                    warehouse_db.log_sync(platform_for(m.table), started, r.rows, r.status,
                                          (r.message + note)[:480])
                except Exception as exc:  # noqa: BLE001 -- the log must not kill the mirror
                    print(f"  (sync_log write failed: {exc})", flush=True)
    finally:
        if own_conn:
            conn.close()
    return results


def select(set_name: str | None, only: list[str] | None) -> list[Mirror]:
    if only:
        by = {m.table: m for m in all_mirrors()}
        unknown = [t for t in only if t not in by]
        if unknown:
            raise SystemExit(f"not in the mirror registry: {unknown}")
        return [by[t] for t in only]
    if not set_name or set_name == "all":
        return all_mirrors()
    if set_name not in SETS:
        raise SystemExit(f"unknown set {set_name!r}; choose from {sorted(SETS)} or 'all'")
    return list(SETS[set_name])


def exit_code(results: list[Result]) -> int:
    return 1 if any(r.status == "error" for r in results) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", dest="set_name",
                   help=f"one of {', '.join(SETS)}, or all (see bq_mirror_registry.py)")
    p.add_argument("--only", help="comma-separated table subset (overrides --set)")
    p.add_argument("--since", help="YYYY-MM-DD: rewrite window tables from this day (backfill)")
    p.add_argument("--dry-run", action="store_true", help="export + plan, write nothing to BigQuery")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not args.set_name and not args.only:
        p.error("give --set or --only")
    check_required_env()
    warehouse_db.init_db()
    mirrors = select(args.set_name, args.only.split(",") if args.only else None)
    project, dataset, location = bq_target()
    print(f"bq_mirror_sync: {len(mirrors)} table(s) -> {project}.{dataset} ({location})", flush=True)
    results = run(mirrors, dry_run=args.dry_run, since=args.since)
    errs = [r.table for r in results if r.status == "error"]
    degr = [r.table for r in results if r.status == "degraded"]
    print(f"done: {len(results) - len(errs) - len(degr)} ok, {len(degr)} degraded, "
          f"{len(errs)} error{' -> ' + ', '.join(errs) if errs else ''}", flush=True)
    return exit_code(results)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    sys.exit(main())
