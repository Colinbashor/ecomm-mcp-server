"""Hermetic tests for voc_import.py — no network, tmp_path CSVs, tmp SQLite.

Covers: schema creation, header-driven column matching across a few known
spelling variants, numeric/percent cleanup, the SKU-vs-ASIN-only fallback
key, and that --dry-run never writes.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import voc_import as voc


class SchemaTests(unittest.TestCase):
    def test_ddl_creates_expected_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(voc.DDL)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(amazon_voc)")}
        self.assertEqual(cols, {
            "snapshot_date", "sku", "asin", "product_name", "cx_health",
            "ncx_rate", "ncx_orders", "total_orders", "top_ncx_reason",
            "synced_at",
        })


class ParseFileTests(unittest.TestCase):
    def _write(self, tmp: Path, text: str) -> Path:
        p = tmp / "export.csv"
        p.write_text(text, encoding="utf-8")
        return p

    def test_parses_known_header_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), (
                "SKU,ASIN,Product Name,CX Health,NCX Rate,NCX Orders,Total Orders,Top NCX Reason\n"
                "ABC-1,B000000001,Widget,Good,3.5%,7,200,Late delivery\n"
            ))
            rows, cols = voc.parse_file(path, "2025-01-01", "2025-01-01T00:00:00")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row[0], "2025-01-01")   # snapshot_date
            self.assertEqual(row[1], "ABC-1")         # sku
            self.assertEqual(row[2], "B000000001")    # asin
            self.assertEqual(row[3], "Widget")        # product_name
            self.assertEqual(row[4], "Good")          # cx_health
            self.assertEqual(row[5], 3.5)             # ncx_rate (% stripped)
            self.assertEqual(row[6], 7)                # ncx_orders
            self.assertEqual(row[7], 200)               # total_orders
            self.assertEqual(row[8], "Late delivery")  # top_ncx_reason

    def test_parses_alternate_header_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), (
                "Seller SKU,Child ASIN,Item Name,Customer Experience Health,"
                "Negative Customer Experience Rate,NCX,Orders,Top Reason\n"
                "XYZ-9,B000000002,Gadget,Poor,12,3,25,Damaged\n"
            ))
            rows, _ = voc.parse_file(path, "2025-01-02", "stamp")
            self.assertEqual(rows[0][1], "XYZ-9")
            self.assertEqual(rows[0][4], "Poor")
            self.assertEqual(rows[0][5], 12.0)

    def test_asin_only_row_keys_on_asin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), (
                "ASIN,CX Health\nB000000003,Fair\n"
            ))
            rows, _ = voc.parse_file(path, "2025-01-03", "stamp")
            self.assertEqual(rows[0][1], "B000000003")  # sku falls back to asin

    def test_row_with_no_sku_or_asin_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), (
                "Product Name,CX Health\nMystery Item,Good\n"
            ))
            rows, _ = voc.parse_file(path, "2025-01-04", "stamp")
            self.assertEqual(rows, [])

    def test_nullish_values_become_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), (
                "SKU,NCX Rate,NCX Orders\nABC-2,N/A,--\n"
            ))
            rows, _ = voc.parse_file(path, "2025-01-05", "stamp")
            self.assertIsNone(rows[0][5])  # ncx_rate
            self.assertIsNone(rows[0][6])  # ncx_orders


class MainDryRunTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "export.csv"
            csv_path.write_text("SKU,CX Health\nABC-1,Good\n", encoding="utf-8")
            db_path = tmp_path / "warehouse.db"

            with patch.object(voc, "DB", db_path), \
                 patch.object(voc.warehouse_db, "DB_PATH", db_path), \
                 patch("sys.argv", ["voc_import.py", "--dry-run", str(csv_path)]):
                voc.main()

            # init_db() may create the file/schema, but --dry-run must never
            # write a row or a sync_log entry.
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                n = conn.execute("SELECT COUNT(*) FROM amazon_voc").fetchone()[0]
                self.assertEqual(n, 0)
                conn.close()

    def test_real_run_writes_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "export.csv"
            csv_path.write_text("SKU,CX Health\nABC-1,Good\n", encoding="utf-8")
            db_path = tmp_path / "warehouse.db"

            with patch.object(voc, "DB", db_path), \
                 patch.object(voc.warehouse_db, "DB_PATH", db_path), \
                 patch("sys.argv", ["voc_import.py", "--date", "2025-01-01", str(csv_path)]):
                voc.main()

            conn = sqlite3.connect(db_path)
            n = conn.execute("SELECT COUNT(*) FROM amazon_voc").fetchone()[0]
            self.assertEqual(n, 1)
            log_n = conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE platform='amazon_voc'").fetchone()[0]
            self.assertEqual(log_n, 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
