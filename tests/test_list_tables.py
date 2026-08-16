"""list_tables() must surface SQL views alongside tables.

Regression test for a real bug: the original query filtered to
`type='table'`, so any SQL VIEW in your schema (for example a dedup view you
add over a source table that can carry several disagreeing rows per key, so
callers have a safe join target instead of the raw table) was invisible to
anyone exploring the schema over MCP — they would find the raw table, join
on it directly, and get a silently wrong answer instead of using the view
built to prevent that.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import server
from warehouse import db


class ListTablesIncludesViewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "warehouse.db"
        conn = sqlite3.connect(self._db_path)
        conn.execute("CREATE TABLE widgets (id INTEGER, name TEXT)")
        conn.executemany("INSERT INTO widgets VALUES (?, ?)", [(1, "a"), (1, "a-dup"), (2, "b")])
        # A dedup view, same shape as the real-world case that prompted this
        # fix: the source table has more than one row per id, and the view is
        # the safe "one row per id" join target.
        conn.execute(
            "CREATE VIEW widgets_by_id AS "
            "SELECT id, name FROM widgets GROUP BY id HAVING rowid = MIN(rowid)"
        )
        conn.commit()
        conn.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self._db_path

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        self._tmp.cleanup()

    def test_view_appears_alongside_table(self) -> None:
        out = json.loads(server.list_tables())
        self.assertIn("widgets", out)
        self.assertIn("widgets_by_id", out)

    def test_view_columns_are_listed_correctly(self) -> None:
        out = json.loads(server.list_tables())
        self.assertEqual(out["widgets_by_id"], ["id", "name"])

    def test_name_only_mode_also_includes_views(self) -> None:
        out = json.loads(server.list_tables(include_columns=False))
        self.assertIn("widgets", out)
        self.assertIn("widgets_by_id", out)

    def test_table_pattern_matches_views_too(self) -> None:
        out = json.loads(server.list_tables(table_pattern="by_id"))
        self.assertEqual(list(out), ["widgets_by_id"])


if __name__ == "__main__":
    unittest.main()
