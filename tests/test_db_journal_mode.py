"""WAL is a correctness property here, not a performance tweak.

The MCP server reads the warehouse while a sync writes it. In SQLite's default
`delete` journal a reader and a writer block each other, so a question asked
during the nightly sync would stall. `connect_readonly()` deliberately sets no
busy timeout *because* it assumes WAL, so if WAL is ever lost that reader has
no timeout to fall back on.

This was a real defect: db.py's comments described WAL as though it were in
force while nothing in the repo ever set it, so a fresh clone silently ran in
`delete` mode.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class JournalModeTests(unittest.TestCase):
    def _fresh_db(self) -> str:
        tmp = tempfile.mkdtemp()
        return str(Path(tmp) / "warehouse.db")

    def _init(self, path: str):
        """Import db bound to a throwaway path, in a clean module state."""
        import importlib
        prev = os.environ.get("WAREHOUSE_DB")
        os.environ["WAREHOUSE_DB"] = path
        try:
            from warehouse import db as _db
            importlib.reload(_db)
            _db.init_db()
            return _db
        finally:
            if prev is None:
                os.environ.pop("WAREHOUSE_DB", None)
            else:
                os.environ["WAREHOUSE_DB"] = prev

    def test_a_fresh_database_comes_up_in_wal(self) -> None:
        path = self._fresh_db()
        self._init(path)
        conn = sqlite3.connect(path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_init_db_is_still_idempotent(self) -> None:
        """journal_mode cannot be set inside a transaction, so the pragma sits
        outside init_db's `with conn:` block. Running twice must stay clean."""
        path = self._fresh_db()
        db = self._init(path)
        db.init_db()
        db.init_db()
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            conn.close()

    # NOTE: there is deliberately no "a reader is not blocked by a writer"
    # test here. The obvious version -- hold BEGIN IMMEDIATE open and read from
    # a second connection -- PASSES IN BOTH JOURNAL MODES and so proves nothing:
    # BEGIN IMMEDIATE takes only a RESERVED lock, which still admits readers.
    # Rollback-journal mode blocks readers later, at EXCLUSIVE, during the
    # commit itself, which is not reachable deterministically in-process
    # without timing games that would make this suite flaky. The pragma
    # assertions above are the real guard; the concurrency benefit follows from
    # WAL by construction.


if __name__ == "__main__":
    unittest.main()
