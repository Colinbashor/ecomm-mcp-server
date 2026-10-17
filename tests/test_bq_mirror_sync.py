"""Hermetic tests for bq_mirror_sync.py (the SQLite -> BigQuery mirror).

No network, no warehouse.db: an in-memory SQLite plus a fake BigQuery client
(tests/bq_fakes.py). They pin the failure modes that would otherwise produce a
plausible-looking but stale or wrong BigQuery table: a silently skipped gap
day, a column SQLite gained after the table was created, an empty pull that
logs ok, a half-applied window.

Skipped when the optional google-cloud-bigquery package is not installed.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("google.cloud.bigquery")
from google.cloud import bigquery  # noqa: E402

import bq_mirror_registry as reg  # noqa: E402
import bq_mirror_sync as ms  # noqa: E402
from bq_fakes import FakeBigQueryClient, FakeJob  # noqa: E402

SF = bigquery.SchemaField
PROJECT, DATASET = "p", "ds"
TODAY = "2026-10-08"


# ---------------------------------------------------------------- fake client
class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class MirrorClient(FakeBigQueryClient):
    """FakeBigQueryClient plus the calls the mirror makes."""

    def __init__(self):
        super().__init__(PROJECT)
        self.updated: list[tuple[str, list[str]]] = []
        self.output_rows_override: int | None = None
        self.date_col: dict[str, str] = {}

    def load_table_from_file(self, fh, destination, job_config=None, location=None, **kw):
        rows = [json.loads(line) for line in fh.read().decode("utf-8").splitlines() if line]
        job = self.load_table_from_json(rows, destination, job_config=job_config, location=location)
        out = FakeJob()
        out.output_rows = self.output_rows_override if self.output_rows_override is not None else len(rows)
        return out

    def load_table_from_json(self, rows, destination, job_config=None, location=None):
        job = super().load_table_from_json(rows, destination, job_config, location)
        job.output_rows = len(list(rows))
        return job

    def update_table(self, table, fields):
        self.updated.append((table.table_id, [f.name for f in table.schema]))
        self.tables[f"{table.project}.{table.dataset_id}.{table.table_id}"] = table
        return table

    def query(self, sql, job_config=None, location=None):
        self.queries.append(sql)
        params = {p.name: p.values if hasattr(p, "values") else p.value
                  for p in (getattr(job_config, "query_parameters", None) or [])}
        table = re.search(r"`([^`]+)`", sql).group(1)
        col = self.date_col.get(table, "date")
        if sql.lstrip().startswith("BEGIN TRANSACTION"):
            return self._transaction(sql, table, col, params)
        if "MAX(" in sql:
            days = [str(r[col])[:10] for r in self.rows_in(table) if r.get(col)]
            return _Rows([{"d": max(days) if days else None}])
        if "GROUP BY d" in sql:
            counts: dict[str, int] = {}
            for r in self.rows_in(table):
                d = str(r[col])[:10]
                if d >= params["since"]:
                    counts[d] = counts.get(d, 0) + 1
            return _Rows([{"d": d, "n": n} for d, n in counts.items()])
        return _Rows([])

    def _transaction(self, sql, table, col, params):
        days = set(params["days"])
        stg = re.findall(r"`([^`]+)`", sql)[-1]
        kept = [r for r in self.rows_in(table) if str(r[col])[:10] not in days]
        self._data[table] = {None: kept + list(self.rows_in(stg))}
        return _Rows([])


def make_sqlite():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders (order_id TEXT, order_date TEXT, total REAL, qty INTEGER,"
                 " platform_discount REAL)")
    conn.execute("CREATE TABLE ad_metrics (campaign TEXT, date TEXT, spend REAL)")
    conn.execute("CREATE TABLE snap (sku TEXT, snapshot_date TEXT, n INTEGER)")
    return conn


def table_ref(name):
    return f"{PROJECT}.{DATASET}.{name}"


def seed_orders(client, partitioned=False):
    client.date_col[table_ref("orders")] = "order_date"
    schema = [SF("order_id", "STRING"), SF("order_date", "STRING"), SF("total", "FLOAT"),
              SF("qty", "INTEGER")]  # NB: no platform_discount -- SQLite gained it later
    client.seed_table(table_ref("orders"), schema)


def run_one(client, conn, m, **kw):
    return ms.mirror_one(client, conn, m, PROJECT, DATASET, "US", kw.pop("today", TODAY),
                         kw.pop("since", None), kw.pop("dry_run", False))


# ------------------------------------------------------------------- convert
@pytest.mark.parametrize("value,typ,expected", [
    (None, "STRING", None),
    (5, "STRING", "5"),
    ("12", "INTEGER", 12),
    (3.0, "INT64", 3),
    ("", "INTEGER", None),
    (True, "INTEGER", 1),
    ("2.50", "FLOAT", 2.5),
    (float("nan"), "FLOAT", None),
    (float("inf"), "FLOAT64", None),
    (1, "BOOLEAN", True),
    ("0", "BOOL", False),
    ("2026-09-14T10:00:00+00:00", "DATE", "2026-09-14"),
    ("2026-08", "DATE", "2026-08-01"),            # month-grain tables (amazon_search_term_*)
    ("2026-09-14 10:00:00", "TIMESTAMP", "2026-09-14 10:00:00"),
    ("", "TIMESTAMP", None),
    (b"abc", "STRING", "abc"),
])
def test_convert(value, typ, expected):
    assert ms.convert(value, typ) == expected


def test_convert_refuses_to_truncate_a_fractional_integer():
    with pytest.raises(ms.MirrorError):
        ms.convert(3.5, "INTEGER", "qty")
    with pytest.raises(ms.MirrorError):
        ms.convert("3.5", "INTEGER", "qty")


@pytest.mark.parametrize("sqlite_type,bq", [("INTEGER", "INTEGER"), ("REAL", "FLOAT"),
                                            ("TEXT", "STRING"), ("", "STRING"), ("NUMERIC", "FLOAT")])
def test_infer_bq_type(sqlite_type, bq):
    assert ms.infer_bq_type(sqlite_type) == bq


# ---------------------------------------------------------------- plan_days
def plan(**kw):
    base = dict(today=TODAY, sqlite_max=TODAY, window_days=3, reconcile_days=30,
                bq_max=TODAY, sqlite_counts={}, bq_counts={})
    base.update(kw)
    return ms.plan_days(**base)


def test_plan_trailing_window_only_when_bq_is_current():
    assert plan() == ["2026-10-06", "2026-10-07", "2026-10-08"]


def test_plan_catches_up_the_gap_since_bigquerys_newest_day():
    days = plan(bq_max="2026-10-01")
    assert days[0] == "2026-10-01" and days[-1] == TODAY and len(days) == 8


def test_plan_caps_the_catch_up_unless_since_is_given():
    capped = plan(bq_max="2026-01-01")
    assert capped[0] == "2026-08-09"          # MAX_CATCHUP_DAYS = 60 before today
    assert plan(bq_max="2026-01-01", since="2026-09-20")[0] == "2026-09-20"


def test_plan_empty_bigquery_uses_the_window_not_history():
    assert plan(bq_max=None)[0] == "2026-10-06"


def test_plan_adds_days_outside_the_window_whose_counts_differ():
    days = plan(sqlite_counts={"2026-09-20": 5, "2026-09-21": 5},
                bq_counts={"2026-09-20": 5, "2026-09-21": 3})
    assert "2026-09-21" in days and "2026-09-20" not in days


def test_plan_ignores_drift_older_than_the_reconcile_range():
    assert "2026-01-01" not in plan(sqlite_counts={"2026-01-01": 9}, bq_counts={})


def test_plan_includes_a_day_sqlite_is_ahead_of_local_today():
    assert plan(sqlite_max="2026-10-09")[-1] == "2026-10-09"


def test_plan_nothing_when_sqlite_has_no_dated_rows():
    assert plan(sqlite_max=None) == []


# ------------------------------------------------------------------ full mode
def test_full_replaces_the_table_and_adds_a_column_sqlite_gained():
    conn, client = make_sqlite(), MirrorClient()
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)",
                     [("A", "2026-10-08T01:00:00Z", 10.0, 1, 2.5), ("B", "2026-10-08", 5.0, 2, None)])
    seed_orders(client)
    client.rows_in  # noqa: B018
    client._data[table_ref("orders")] = {None: [{"order_id": "STALE", "order_date": "2026-09-14"}]}
    res = run_one(client, conn, reg.Mirror("orders"))
    assert res.status == "ok" and res.rows == 2
    rows = client.rows_in(table_ref("orders"))
    assert {r["order_id"] for r in rows} == {"A", "B"}          # STALE gone: atomic replace
    assert rows[0]["platform_discount"] == 2.5                   # new column carried
    assert client.updated and "platform_discount" in client.updated[0][1]
    assert "added BQ column(s): platform_discount" in " ".join(res.notes)


def test_full_empty_source_is_degraded_and_does_not_wipe_bigquery():
    conn, client = make_sqlite(), MirrorClient()
    seed_orders(client)
    client._data[table_ref("orders")] = {None: [{"order_id": "KEEP"}]}
    res = run_one(client, conn, reg.Mirror("orders"))
    assert res.status == "degraded"
    assert [r["order_id"] for r in client.rows_in(table_ref("orders"))] == ["KEEP"]


def test_full_empty_source_is_ok_when_the_registry_says_so():
    conn, client = make_sqlite(), MirrorClient()
    seed_orders(client)
    res = run_one(client, conn, reg.Mirror("orders", allow_empty=True))
    assert res.status == "ok" and res.rows == 0


def test_full_row_count_mismatch_is_an_error():
    conn, client = make_sqlite(), MirrorClient()
    conn.execute("INSERT INTO orders VALUES ('A','2026-10-08',1,1,0)")
    seed_orders(client)
    client.output_rows_override = 0
    assert run_one(client, conn, reg.Mirror("orders")).status == "error"


def test_full_dry_run_writes_nothing():
    conn, client = make_sqlite(), MirrorClient()
    conn.execute("INSERT INTO orders VALUES ('A','2026-10-08',1,1,0)")
    seed_orders(client)
    res = run_one(client, conn, reg.Mirror("orders"), dry_run=True)
    assert res.status == "ok" and not client.loads and not client.updated


def test_a_failing_table_is_an_error_result_not_an_exception():
    conn, client = make_sqlite(), MirrorClient()
    res = run_one(client, conn, reg.Mirror("orders"))       # BQ table not seeded
    assert res.status == "error" and "Not found" in res.message


# ------------------------------------------------------- create_if_missing
class NotFoundClient(MirrorClient):
    """The real client raises google.api_core NotFound, not the fake's RuntimeError."""

    def get_table(self, ref):
        from google.api_core.exceptions import NotFound
        if ms.bigquery.Table(ref).reference.__str__() not in self.tables and                 str(ref) not in self.tables:
            raise NotFound(f"Not found: Table {ref}")
        return super().get_table(ref)


def test_missing_table_is_created_from_sqlite_when_opted_in_then_loaded():
    conn, client = make_sqlite(), NotFoundClient()
    conn.execute("INSERT INTO orders VALUES ('A','2026-10-08',1.5,2,0.25)")
    res = run_one(client, conn, reg.Mirror("orders", create_if_missing=True))
    assert res.status == "ok" and res.rows == 1, res.message
    t = client.tables[table_ref("orders")]
    assert [(f.name, f.field_type) for f in t.schema] == [
        ("order_id", "STRING"), ("order_date", "STRING"), ("total", "FLOAT"),
        ("qty", "INTEGER"), ("platform_discount", "FLOAT")]
    assert t.time_partitioning is None
    assert "created the BigQuery table from the SQLite schema" in res.notes
    assert client.rows_in(table_ref("orders"))[0]["platform_discount"] == 0.25


def test_missing_table_without_the_flag_is_still_an_error():
    conn, client = make_sqlite(), NotFoundClient()
    conn.execute("INSERT INTO orders VALUES ('A','2026-10-08',1,1,0)")
    res = run_one(client, conn, reg.Mirror("orders"))
    assert res.status == "error" and "Not found" in res.message
    assert table_ref("orders") not in client.tables


def test_dry_run_never_creates_a_table():
    conn, client = make_sqlite(), NotFoundClient()
    conn.execute("INSERT INTO orders VALUES ('A','2026-10-08',1,1,0)")
    res = run_one(client, conn, reg.Mirror("orders", create_if_missing=True), dry_run=True)
    assert res.status == "ok" and "would create" in res.message
    assert table_ref("orders") not in client.tables and not client.loads


def test_created_window_table_loads_history_inside_the_reconcile_range():
    # A window mirror whose reconcile range spans the table's whole history:
    # the first run needs no manual --since.
    conn, client = make_sqlite(), NotFoundClient()
    fill_orders(conn)
    conn.execute("INSERT INTO orders VALUES ('OLD','2026-08-01T00:00:00Z',1,1,0)")
    client.date_col[table_ref("orders")] = "order_date"
    m = reg.Mirror("orders", "window", column="order_date", window_days=3, reconcile_days=120,
                   create_if_missing=True)
    res = run_one(client, conn, m)
    assert res.status == "ok", res.message
    assert len(client.rows_in(table_ref("orders"))) == 10


# ---------------------------------------------------------------- window mode
WINDOW = reg.Mirror("orders", "window", column="order_date", window_days=3, reconcile_days=30)


def fill_orders(conn):
    rows = []
    for day, n in (("2026-10-04", 2), ("2026-10-05", 1), ("2026-10-06", 2),
                   ("2026-10-07", 3), ("2026-10-08", 1)):
        for i in range(n):
            rows.append((f"{day}-{i}", f"{day}T0{i}:00:00Z", 1.0, 1, 0.0))
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", rows)


def test_window_unpartitioned_replaces_days_in_one_transaction_and_keeps_the_rest():
    conn, client = make_sqlite(), MirrorClient()
    fill_orders(conn)
    seed_orders(client)
    client._data[table_ref("orders")] = {None: [
        {"order_id": "OLD", "order_date": "2026-09-01"},               # outside window: kept
        {"order_id": "2026-10-04-0", "order_date": "2026-10-04T00:00:00Z"},   # still current
        {"order_id": "2026-10-04-1", "order_date": "2026-10-04T01:00:00Z"},
        {"order_id": "2026-10-05-0", "order_date": "2026-10-05T00:00:00Z"},
        {"order_id": "GHOST", "order_date": "2026-10-07T09:00:00Z"},           # in window, not in SQLite
    ]}
    res = run_one(client, conn, WINDOW)
    assert res.status == "ok", res.message
    ids = {r["order_id"] for r in client.rows_in(table_ref("orders"))}
    assert "OLD" in ids and "GHOST" not in ids
    assert {"2026-10-06-0", "2026-10-06-1", "2026-10-07-0", "2026-10-08-0"} <= ids
    tx = [q for q in client.queries if q.lstrip().startswith("BEGIN TRANSACTION")]
    assert len(tx) == 1 and "DELETE FROM" in tx[0] and "INSERT INTO" in tx[0] and "COMMIT" in tx[0]
    assert any(d.startswith(f"{PROJECT}.{DATASET}._stg_mirror_orders_") for d in client.deleted)


def test_window_heals_a_gap_since_bigquerys_newest_day():
    conn, client = make_sqlite(), MirrorClient()
    fill_orders(conn)
    seed_orders(client)
    client._data[table_ref("orders")] = {None: [
        {"order_id": "2026-10-04-0", "order_date": "2026-10-04T00:00:00Z"}]}
    run_one(client, conn, WINDOW)
    days = {r["order_date"][:10] for r in client.rows_in(table_ref("orders"))}
    assert days == {"2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"}
    got = [r["order_id"] for r in client.rows_in(table_ref("orders"))
           if r["order_date"].startswith("2026-10-04")]
    assert sorted(got) == ["2026-10-04-0", "2026-10-04-1"]   # bq_max day itself is rewritten


def test_window_rewrites_an_old_day_whose_count_drifted():
    conn, client = make_sqlite(), MirrorClient()
    fill_orders(conn)
    seed_orders(client)
    # bq is current to today, but 10-04 is missing a row
    full = [{"order_id": f"X{i}", "order_date": d} for i, d in enumerate(
        ["2026-10-04T00:00:00Z", "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z",
         "2026-10-06T01:00:00Z", "2026-10-07T00:00:00Z", "2026-10-07T01:00:00Z",
         "2026-10-07T02:00:00Z", "2026-10-08T00:00:00Z"])]
    client._data[table_ref("orders")] = {None: full}
    res = run_one(client, conn, WINDOW)
    assert res.status == "ok"
    d4 = [r for r in client.rows_in(table_ref("orders")) if r["order_date"].startswith("2026-10-04")]
    assert len(d4) == 2


def test_window_partitioned_table_gets_one_truncate_load_per_day_including_empty_days():
    conn, client = make_sqlite(), MirrorClient()
    conn.executemany("INSERT INTO snap VALUES (?,?,?)",
                     [("a", "2026-10-06", 1), ("b", "2026-10-06", 2), ("a", "2026-10-08", 3)])
    ref = table_ref("snap")
    client.date_col[ref] = "snapshot_date"
    client.seed_table(ref, [SF("sku", "STRING"), SF("snapshot_date", "DATE"), SF("n", "INTEGER")],
                      partition_field="snapshot_date")
    client._data[ref] = {"2026-10-07": [{"sku": "GHOST", "snapshot_date": "2026-10-07", "n": 9}]}
    m = reg.Mirror("snap", "window", column="snapshot_date", window_days=3, reconcile_days=30)
    res = run_one(client, conn, m)
    assert res.status == "ok", res.message
    assert client.partitions_written(ref) == ["2026-10-06", "2026-10-07", "2026-10-08"]
    assert client.partition_rows(ref, "2026-10-07") == []          # empty day truncated
    assert len(client.partition_rows(ref, "2026-10-06")) == 2
    assert all(ld["write_disposition"] == bigquery.WriteDisposition.WRITE_TRUNCATE
               for ld in client.loads if ld["table"] == ref)
    assert not [q for q in client.queries if "BEGIN TRANSACTION" in q]   # no DML on partitioned


def test_window_refuses_a_partition_column_mismatch():
    conn, client = make_sqlite(), MirrorClient()
    conn.execute("INSERT INTO snap VALUES ('a','2026-10-08',1)")
    ref = table_ref("snap")
    client.seed_table(ref, [SF("sku", "STRING"), SF("snapshot_date", "DATE"), SF("n", "INTEGER")],
                      partition_field="n")
    m = reg.Mirror("snap", "window", column="snapshot_date", window_days=3)
    res = run_one(client, conn, m)
    assert res.status == "error" and "partitioned on" in res.message


def test_window_with_no_rows_in_sqlite_is_degraded():
    conn, client = make_sqlite(), MirrorClient()
    seed_orders(client)
    assert run_one(client, conn, WINDOW).status == "degraded"


def test_window_detects_a_post_load_count_mismatch():
    conn, client = make_sqlite(), MirrorClient()
    fill_orders(conn)
    seed_orders(client)
    orig = client._transaction

    def drop_one(sql, table, col, params):
        out = orig(sql, table, col, params)
        client._data[table][None] = client._data[table][None][:-1]
        return out

    client._transaction = drop_one
    assert run_one(client, conn, WINDOW).status == "error"


# ------------------------------------------------------------------- driver
def test_run_logs_one_sync_log_row_per_table_and_never_stops_on_an_error(monkeypatch):
    monkeypatch.setenv("BQ_PROJECT", PROJECT)
    monkeypatch.setenv("BQ_DATASET", DATASET)
    conn, client = make_sqlite(), MirrorClient()
    conn.execute("INSERT INTO orders VALUES ('A','2026-10-08',1,1,0)")
    seed_orders(client)
    logged = []
    monkeypatch.setattr(ms.warehouse_db, "log_sync", lambda *a: logged.append(a))
    results = ms.run([reg.Mirror("missing_table"), reg.Mirror("orders")],
                     client=client, conn=conn, today=TODAY)
    assert [r.status for r in results] == ["error", "ok"]
    assert [a[0] for a in logged] == ["mirror_missing_table_bq", "mirror_orders_bq"]
    assert logged[0][3] == "error" and logged[1][3] == "ok"
    assert ms.exit_code(results) == 1


def test_dry_run_logs_nothing(monkeypatch):
    monkeypatch.setenv("BQ_PROJECT", PROJECT)
    monkeypatch.setenv("BQ_DATASET", DATASET)
    conn, client = make_sqlite(), MirrorClient()
    conn.execute("INSERT INTO orders VALUES ('A','2026-10-08',1,1,0)")
    seed_orders(client)
    logged = []
    monkeypatch.setattr(ms.warehouse_db, "log_sync", lambda *a: logged.append(a))
    ms.run([reg.Mirror("orders")], client=client, conn=conn, today=TODAY, dry_run=True)
    assert logged == [] and not client.loads


def test_degraded_does_not_fail_the_exit_code():
    assert ms.exit_code([ms.Result("a", "degraded"), ms.Result("b", "ok")]) == 0


def test_select_set_only_and_unknown():
    assert [m.table for m in ms.select("log", None)] == ["sync_log"]
    assert [m.table for m in ms.select(None, ["orders"])] == ["orders"]
    with pytest.raises(SystemExit):
        ms.select("nope", None)
    with pytest.raises(SystemExit):
        ms.select(None, ["not_a_table"])


# ----------------------------------------------------------------- registry
def test_no_table_is_listed_twice():
    names = [m.table for m in reg.all_mirrors()]
    assert len(names) == len(set(names))


def test_sync_log_is_mirrored_last_so_it_contains_the_other_sets_rows():
    assert list(reg.SETS)[-1] == "log"
    assert [m.table for m in reg.SETS["log"]] == ["sync_log"]


def test_window_mirrors_name_a_date_column_and_full_ones_do_not_need_one():
    for m in reg.all_mirrors():
        assert (m.mode == "window") == (m.column is not None), m.table


def test_default_registry_matches_the_shipped_schema():
    """Every default mirror names a table (and window column) warehouse/schema.sql creates."""
    conn = sqlite3.connect(":memory:")
    conn.executescript((Path(ms.__file__).parent / "warehouse" / "schema.sql").read_text())
    for m in reg.all_mirrors():
        cols = {c for c, _ in ms.sqlite_columns(conn, m.table)}
        if m.column:
            assert m.column in cols, m.table


def test_mirror_rejects_a_bad_mode_and_a_window_without_a_column():
    with pytest.raises(ValueError):
        reg.Mirror("t", "sometimes")
    with pytest.raises(ValueError):
        reg.Mirror("t", "window")


def test_platform_names_end_in_bq():
    assert reg.platform_for("orders") == "mirror_orders_bq"


# ---------------------------------------------------------------- env / CLI
def test_check_required_env_names_what_is_missing(monkeypatch):
    monkeypatch.delenv("BQ_PROJECT", raising=False)
    monkeypatch.setenv("BQ_DATASET", "ds")
    with pytest.raises(SystemExit, match="BQ_PROJECT"):
        ms.check_required_env()


def test_bq_target_defaults_location_to_us(monkeypatch):
    monkeypatch.setenv("BQ_PROJECT", "p1")
    monkeypatch.setenv("BQ_DATASET", "d1")
    monkeypatch.delenv("BQ_LOCATION", raising=False)
    assert ms.bq_target() == ("p1", "d1", "US")


def test_main_refuses_without_a_set_or_table():
    with pytest.raises(SystemExit):
        ms.main([])


def test_main_checks_env_before_touching_bigquery(monkeypatch):
    monkeypatch.delenv("BQ_PROJECT", raising=False)
    monkeypatch.delenv("BQ_DATASET", raising=False)
    monkeypatch.setattr(ms, "run", lambda *a, **k: pytest.fail("run() must not be reached"))
    with pytest.raises(SystemExit, match="BQ_PROJECT"):
        ms.main(["--set", "facts"])
