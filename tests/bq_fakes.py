"""In-memory stand-in for `google.cloud.bigquery.Client`, for hermetic tests.

Code under test accepts a `client=` argument; tests pass a
`FakeBigQueryClient` and then inspect what would have been written -- no
network, no credentials, no `warehouse.db`. Emulates just enough of the real
client:

  create_table(table, exists_ok)     -> stores the bigquery.Table object
  get_table(ref)                     -> returns it (schema + time_partitioning)
  load_table_from_json(rows, dest, job_config, location)
                                     -> `dest` may carry a `$YYYYMMDD` partition
                                        decorator; WRITE_TRUNCATE replaces that
                                        partition (or the whole table), else appends
  query(sql, location)               -> records the SQL; applies a simple
                                        `MERGE INTO ... USING ... ON T.k = S.k` upsert
  delete_table(ref, not_found_ok)    -> removes it

Assertion helpers: `rows_in(table)`, `partition_rows(table, day)`,
`partitions_written(table)`, and the raw `loads` / `queries` logs.

Seed a pre-existing table with `seed_table(ref, schema, partition_field=None)`.
"""
from __future__ import annotations

import re
from collections import defaultdict

from google.cloud import bigquery


class FakeJob:
    def __init__(self, error: Exception | None = None):
        self._error = error

    def result(self):
        if self._error:
            raise self._error
        return self


def _ref_str(table_or_ref) -> str:
    if isinstance(table_or_ref, str):
        return table_or_ref
    return f"{table_or_ref.project}.{table_or_ref.dataset_id}.{table_or_ref.table_id}"


_MERGE_RE = re.compile(
    r"MERGE INTO `(?P<target>[^`]+)` T\s+USING `(?P<staging>[^`]+)` S\s+ON (?P<on>.+?)\n",
    re.DOTALL,
)


class FakeBigQueryClient:
    def __init__(self, project: str = "fake-project"):
        self.project = project
        self.tables: dict[str, bigquery.Table] = {}
        # base table ref -> {partition_day_or_None: [rows]}
        self._data: dict[str, dict[str | None, list[dict]]] = defaultdict(dict)
        self.loads: list[dict] = []
        self.queries: list[str] = []
        self.deleted: list[str] = []
        self.fail_next_load: Exception | None = None

    # -- seeding ---------------------------------------------------------
    def seed_table(self, ref: str, schema: list[bigquery.SchemaField],
                   partition_field: str | None = None,
                   partition_type: str = bigquery.TimePartitioningType.DAY) -> bigquery.Table:
        t = bigquery.Table(ref, schema=schema)
        if partition_field:
            t.time_partitioning = bigquery.TimePartitioning(type_=partition_type, field=partition_field)
        self.tables[ref] = t
        return t

    # -- client surface ----------------------------------------------------
    def create_table(self, table: bigquery.Table, exists_ok: bool = False) -> bigquery.Table:
        ref = _ref_str(table)
        if ref in self.tables:
            if not exists_ok:
                raise RuntimeError(f"Already Exists: {ref}")
            return self.tables[ref]
        self.tables[ref] = table
        return table

    def get_table(self, ref) -> bigquery.Table:
        ref = _ref_str(ref)
        if ref not in self.tables:
            raise RuntimeError(f"Not found: {ref}")
        return self.tables[ref]

    def delete_table(self, ref, not_found_ok: bool = False) -> None:
        ref = _ref_str(ref)
        self.deleted.append(ref)
        if ref not in self.tables and not not_found_ok:
            raise RuntimeError(f"Not found: {ref}")
        self.tables.pop(ref, None)
        self._data.pop(ref, None)

    def load_table_from_json(self, rows, destination: str, job_config=None, location=None) -> FakeJob:
        rows = [dict(r) for r in rows]  # materialise generators, copy dicts
        base, _, decorator = destination.partition("$")
        day = f"{decorator[:4]}-{decorator[4:6]}-{decorator[6:8]}" if decorator else None
        disposition = getattr(job_config, "write_disposition", None) or bigquery.WriteDisposition.WRITE_APPEND
        self.loads.append({"destination": destination, "table": base, "partition": day,
                           "rows": rows, "write_disposition": disposition, "location": location})
        if self.fail_next_load:
            err, self.fail_next_load = self.fail_next_load, None
            return FakeJob(error=err)
        bucket = self._data[base]
        if disposition == bigquery.WriteDisposition.WRITE_TRUNCATE:
            if day is None:
                bucket.clear()
            bucket[day] = rows
        else:
            bucket.setdefault(day, []).extend(rows)
        return FakeJob()

    def query(self, sql: str, location=None) -> FakeJob:
        self.queries.append(sql)
        m = _MERGE_RE.search(sql)
        if m:
            self._apply_merge(m.group("target"), m.group("staging"), m.group("on"))
        return FakeJob()

    # -- MERGE emulation -----------------------------------------------------
    def _apply_merge(self, target: str, staging: str, on_clause: str) -> None:
        keys = re.findall(r"T\.(\w+) = S\.\1", on_clause)
        existing = {tuple(r[k] for k in keys): r for r in self.rows_in(target)}
        for r in self.rows_in(staging):
            existing[tuple(r[k] for k in keys)] = {**existing.get(tuple(r[k] for k in keys), {}), **r}
        self._data[target] = {None: list(existing.values())}

    # -- assertion helpers -----------------------------------------------------
    def rows_in(self, table: str) -> list[dict]:
        out: list[dict] = []
        for day in sorted(self._data.get(table, {}), key=lambda d: d or ""):
            out.extend(self._data[table][day])
        return out

    def partition_rows(self, table: str, day: str) -> list[dict]:
        return list(self._data.get(table, {}).get(day, []))

    def partitions_written(self, table: str) -> list[str]:
        return sorted(ld["partition"] for ld in self.loads if ld["table"] == table and ld["partition"])
