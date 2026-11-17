"""Repo-wide conventions from AGENTS.md, enforced so they cannot quietly lapse."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(p for pattern in ("*.py", "warehouse/*.py", "warehouse/connectors/*.py")
                 for p in ROOT.glob(pattern))

POSITIONAL_INSERT = re.compile(r"INSERT\s+(?:OR\s+(?:REPLACE|IGNORE)\s+)?INTO\s+\w+\s+VALUES", re.I)
BARE_CONNECT = re.compile(r"sqlite3\.connect\(\s*[\w.]*DB(?:_PATH)?\s*\)")


def _hits(pattern: re.Pattern) -> list[str]:
    out = []
    for path in SOURCES:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                out.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    return out


def test_sources_were_found():
    assert len(SOURCES) > 50


def test_every_insert_names_its_columns():
    """AGENTS.md data rule 3: ALTER TABLE appends a column at the table's physical
    end, so a positional INSERT maps onto different columns on a migrated database
    than on a fresh one, and stores the shifted values without an error."""
    assert _hits(POSITIONAL_INSERT) == []


def test_the_warehouse_db_is_never_opened_without_a_busy_timeout():
    """SQLite's write lock is database-wide. The default 5 s timeout turns a
    wait behind another writer into a lost job."""
    assert _hits(BARE_CONNECT) == []
