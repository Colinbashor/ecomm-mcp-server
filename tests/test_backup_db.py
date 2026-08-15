"""Hermetic tests for backup_db.py — no network, tmp_path only.

Covers: a good backup verifies + rotates old copies out, and a from-scratch
(no tables) source database is correctly rejected rather than silently
"backed up" as an empty, useless file.
"""
from __future__ import annotations

import importlib
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch


class BackupDbTests(unittest.TestCase):
    def _reload(self, db_path: Path, backup_dir: Path, keep: str = "3"):
        with patch.dict("os.environ", {
            "WAREHOUSE_DB": str(db_path),
            "WAREHOUSE_BACKUP_DIR": str(backup_dir),
            "WAREHOUSE_BACKUP_KEEP": keep,
        }):
            import backup_db
            return importlib.reload(backup_db)

    def test_backs_up_and_verifies(self) -> None:
        with unittest.mock.patch("sys.exit") as _:
            pass
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "warehouse.db"
            backup_dir = tmp_path / "backups"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE sync_log (platform TEXT)")
            conn.execute("INSERT INTO sync_log VALUES ('test')")
            conn.commit()
            conn.close()

            mod = self._reload(db_path, backup_dir)
            mod.main()

            backups = list(backup_dir.glob("warehouse-*.db"))
            self.assertEqual(len(backups), 1)
            check = sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True)
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0], 1)
            check.close()

    def test_rejects_empty_source_database(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "warehouse.db"
            backup_dir = tmp_path / "backups"
            sqlite3.connect(db_path).close()  # file exists, zero tables

            mod = self._reload(db_path, backup_dir)
            with self.assertRaises(SystemExit):
                mod.main()

            self.assertEqual(list(backup_dir.glob("warehouse-*.db*")), [])

    def test_rotation_keeps_only_newest_n(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "warehouse.db"
            backup_dir = tmp_path / "backups"
            backup_dir.mkdir()
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.close()

            # Pre-seed 3 fake older backups so rotation has something to drop.
            for name in ("warehouse-2025-01-01.db", "warehouse-2025-01-02.db",
                         "warehouse-2025-01-03.db"):
                (backup_dir / name).write_bytes(b"x")

            mod = self._reload(db_path, backup_dir, keep="2")
            mod.main()

            remaining = sorted(p.name for p in backup_dir.glob("warehouse-*.db"))
            # 3 old + 1 new = 4, keep=2 -> newest 2 survive (today's + the
            # newest-dated pre-seeded stub).
            self.assertEqual(len(remaining), 2)
            self.assertIn(f"warehouse-2025-01-03.db", remaining)


if __name__ == "__main__":
    unittest.main()
