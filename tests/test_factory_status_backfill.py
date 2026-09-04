"""Hermetic tests for factory_status_backfill.py's date-parsing and folder-
walk logic -- no real spreadsheet files.

Real production-tracking archives tend to mix TWO folder-naming conventions
across different years (a fully-numeric date, and a bare month-name + day
with the year only available from the grandparent folder), so both forms
need direct coverage.
"""
from __future__ import annotations

import os
import tempfile
import unittest

import factory_status_backfill as fsb


class ParseSnapshotDateTests(unittest.TestCase):
    def test_numeric_date_folder(self):
        self.assertEqual(fsb.parse_snapshot_date("03.15.2026", "2026"), "2026-03-15")

    def test_numeric_date_folder_with_trailing_text(self):
        self.assertEqual(fsb.parse_snapshot_date("03.15.2026 Archive", "2026"), "2026-03-15")

    def test_month_name_folder_borrows_year_from_parent(self):
        self.assertEqual(fsb.parse_snapshot_date("March 15", "2024 Archive"), "2024-03-15")

    def test_abbreviated_month_name(self):
        self.assertEqual(fsb.parse_snapshot_date("Mar 15", "2024"), "2024-03-15")

    def test_unparseable_folder_name_returns_none(self):
        self.assertIsNone(fsb.parse_snapshot_date("misc notes", "2024"))

    def test_month_name_without_a_year_anywhere_returns_none(self):
        self.assertIsNone(fsb.parse_snapshot_date("March 15", "no year here"))


class FindPeriodFoldersTests(unittest.TestCase):
    def test_walks_year_month_date_structure_and_sorts_oldest_first(self):
        with tempfile.TemporaryDirectory() as root:
            for rel in ("2026/03/03.15.2026", "2026/01/01.05.2026", "2025/12/Dec 20"):
                os.makedirs(os.path.join(root, rel), exist_ok=True)
            found = fsb.find_period_folders(root)
            dates = [d for d, _ in found]
            self.assertEqual(dates, sorted(dates))
            self.assertIn("2026-03-15", dates)
            self.assertIn("2026-01-05", dates)
            self.assertIn("2025-12-20", dates)

    def test_unparseable_folder_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "2026/03/not-a-date"), exist_ok=True)
            found = fsb.find_period_folders(root)
            self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
