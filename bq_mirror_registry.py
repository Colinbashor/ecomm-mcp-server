"""Which SQLite tables `bq_mirror_sync.py` copies into BigQuery, and how.

WHY THIS EXISTS
  If you also serve the warehouse from BigQuery (a BI tool, a hosted MCP
  connector, a coworker's notebook), every BigQuery table needs a SCHEDULED
  WRITER. A one-time bulk copy looks perfectly healthy for weeks: the tables
  answer queries, they just stop at the copy date, and nothing errors. This
  registry is the explicit list of tables the mirror job owns, so "who keeps
  this table fresh?" always has an answer.

  Edit SETS below for your deployment. The defaults cover the core tables in
  warehouse/schema.sql; add any standalone connector's tables you want in
  BigQuery.

TWO MODES (see bq_mirror_sync.py for the mechanics)
  full    WRITE_TRUNCATE the whole table from SQLite every run. Right for
          anything up to a few million rows. Atomic (one load job), and it
          self-heals: a row deleted in SQLite disappears from BigQuery too.
  window  Replace only the trailing `window_days` of a DATE-grain table, plus
          (a) any day in the last `reconcile_days` whose row count disagrees
          with SQLite, and (b) any gap since BigQuery's newest day. For tables
          too big to resend nightly (an `orders` table with millions of lines).
          `column` is the date column; SQLite holds it as TEXT starting
          YYYY-MM-DD, so every comparison uses the first 10 characters.

          Tip: set `reconcile_days` to span a table's whole history (e.g. 1200)
          when its connector re-pulls a long trailing window. The first load,
          and any later historical re-pull, then heal through the count-drift
          pass with no manual `--since`.

ONE WRITER PER TABLE
  If some other job also writes a BigQuery table (a dedicated per-feed export,
  a scheduled query), do NOT list it here too. Two writers on one table end up
  overwriting each other's rows. Give each table exactly one owner.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mirror:
    table: str                       # same name in SQLite and BigQuery
    mode: str = "full"               # "full" | "window"
    column: str | None = None        # window mode: date column (TEXT, YYYY-MM-DD...)
    window_days: int = 7             # window mode: trailing days rewritten every run
    reconcile_days: int = 90         # window mode: look back this far for count drift
    allow_empty: bool = False        # True: an empty SQLite table is legitimate, not degraded
    # True: a table missing from BigQuery is CREATED from the SQLite schema on
    # the first run instead of erroring. Opt-in per table: everywhere else a
    # missing table stays an error, so a typo in this file cannot quietly
    # create junk tables in your dataset.
    create_if_missing: bool = False
    # SQL predicate over the SQLite table: only matching rows are mirrored, and
    # every count the mirror compares is taken over the same filtered rows, so a
    # row the filter drops is ABSENT from BigQuery (a blank), never a stale copy.
    # Use it to hold back a period the source has not finished publishing, e.g.
    #   Mirror("weekly_sales", "window", column="week_start", window_days=35,
    #          where="week_start NOT IN (SELECT week_start FROM weekly_sales_coverage"
    #                " WHERE is_complete = 0)")
    # The predicate is trusted SQL from this file, never user input.
    where: str | None = None

    def __post_init__(self):
        if self.mode not in ("full", "window"):
            raise ValueError(f"{self.table}: unknown mode {self.mode!r}")
        if self.mode == "window" and not self.column:
            raise ValueError(f"{self.table}: window mode needs a date column")


def _full(*names: str, empty_ok: tuple[str, ...] = (), create: bool = False) -> list[Mirror]:
    return [Mirror(n, allow_empty=n in empty_ok, create_if_missing=create) for n in names]


# Run order inside a set is the order listed. Sets are meant to be scheduled
# separately, so one slow table cannot starve the others and each set can get
# its own timeout.
SETS: dict[str, list[Mirror]] = {
    # The two core fact tables plus the Shopify order side tables.
    "facts": [
        Mirror("orders", "window", column="order_date", window_days=14, create_if_missing=True),
        Mirror("shopify_order_discounts", "window", column="order_date", window_days=14,
               create_if_missing=True),
        Mirror("shopify_order_customers", "window", column="order_date", window_days=14,
               create_if_missing=True),
        *_full("ad_metrics", create=True),
    ],
    # LAST on purpose: it then contains the mirror rows the sets above just logged.
    "log": [Mirror("sync_log", create_if_missing=True)],
}


def all_mirrors() -> list[Mirror]:
    return [m for ms in SETS.values() for m in ms]


def platform_for(table: str) -> str:
    """sync_log platform for one table's mirror run."""
    return f"mirror_{table}_bq"
