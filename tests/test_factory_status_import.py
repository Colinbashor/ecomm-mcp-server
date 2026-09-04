"""Hermetic tests for factory_status_import.py — no real spreadsheet files,
no network. Covers the pieces most likely to silently misbehave: ordered
keyword header matching (a more-specific pattern must win over a looser one
listed after it), the non-Latin-suffix stripping that keeps a vendor's
translated header text from defeating a match, Excel-serial-date handling,
and the optional product-master join-back degrading cleanly when the target
table is absent or has an unexpected shape.
"""
from __future__ import annotations

import sqlite3
import unittest

import factory_status_import as fsi


class NormalizeHeaderTests(unittest.TestCase):
    def test_lowercases_and_collapses_whitespace(self):
        self.assertEqual(fsi.normalize_header("  Style   No  "), "style no")

    def test_strips_trailing_non_latin_translation_text(self):
        # A vendor template appending a CJK translation in the same cell must
        # not defeat matching against the English portion.
        self.assertEqual(fsi.normalize_header("Style No 款号"), "style no")

    def test_none_becomes_empty_string(self):
        self.assertEqual(fsi.normalize_header(None), "")

    def test_newlines_become_spaces(self):
        self.assertEqual(fsi.normalize_header("Actual\nCut Date"), "actual cut date")


class MatchFieldOrderingTests(unittest.TestCase):
    def test_more_specific_pattern_wins_over_a_looser_one_listed_later(self):
        # "actual cut date" must resolve to actual_cut_date, not the bare
        # "cut date" pattern under planned_cut_date/cancel_date_on_po -- this
        # is exactly the ordering FIELD_RULES depends on.
        self.assertEqual(fsi.match_field("actual cut date"), "actual_cut_date")
        self.assertEqual(fsi.match_field("planned cut date"), "planned_cut_date")

    def test_style_number_variants_all_resolve_to_style_no(self):
        for header in ("style no", "style#", "style number", "mpn"):
            self.assertEqual(fsi.match_field(header), "style_no")

    def test_unrecognized_header_returns_none(self):
        self.assertIsNone(fsi.match_field("some totally unrelated column"))


class BuildColumnMapTests(unittest.TestCase):
    def test_first_matching_column_wins_for_a_given_field(self):
        # Two headers that both resolve to style_no -- only the first column
        # index should claim the field; a second "hit" must not overwrite it.
        headers = ["Style No", "Description", "Style#"]
        col_map = fsi.build_column_map(headers)
        self.assertEqual(col_map[0], "style_no")
        self.assertNotIn(2, col_map, "a second style_no-like header must be left unmapped")

    def test_unmatched_headers_are_absent_from_the_map(self):
        headers = ["Style No", "Some Random Column"]
        col_map = fsi.build_column_map(headers)
        self.assertEqual(set(col_map.values()), {"style_no"})


class ExcelSerialDateTests(unittest.TestCase):
    def test_converts_a_plausible_serial_to_iso(self):
        # 45000 -> 2023-03-15 under the 1899-12-30 epoch Excel uses.
        self.assertEqual(fsi.excel_serial_to_iso(45000), "2023-03-15")

    def test_implausible_serial_is_returned_unchanged(self):
        # A serial resolving outside the sanity window (e.g. a stray small
        # number that's actually a unit count, not a date) must not be
        # silently reinterpreted as a date.
        self.assertEqual(fsi.excel_serial_to_iso(3), 3)

    def test_non_numeric_value_passes_through(self):
        self.assertEqual(fsi.excel_serial_to_iso("N/A"), "N/A")


class CleanValueTests(unittest.TestCase):
    def test_nullish_tokens_become_none(self):
        for v in (None, "", "#VALUE!", "#N/A", "#REF!"):
            self.assertIsNone(fsi.clean_value("description", v))

    def test_date_field_converts_serial_number(self):
        self.assertEqual(fsi.clean_value("actual_cut_date", 45000), "2023-03-15")

    def test_non_date_field_is_not_reinterpreted_as_a_date(self):
        self.assertEqual(fsi.clean_value("units", 45000), 45000)

    def test_whitespace_only_string_becomes_none(self):
        self.assertIsNone(fsi.clean_value("description", "   "))

    def test_ordinary_string_is_stripped(self):
        self.assertEqual(fsi.clean_value("description", "  Blue Jacket  "), "Blue Jacket")


class FindHeaderRowTests(unittest.TestCase):
    def test_picks_the_row_with_the_most_string_like_cells(self):
        rows = [
            (None, None, None),                       # junk/title row
            ("Style No", "Description", "Vendor"),     # real header row
            ("S1001", "Blue Jacket", "Vendor A"),
        ]
        self.assertEqual(fsi.find_header_row(rows), 1)


class VendorFromFilenameTests(unittest.TestCase):
    def test_matches_a_configured_pattern(self):
        self.assertEqual(fsi.vendor_from_filename("VENDOR_A_week12.xlsx"), "Vendor A")

    def test_unmatched_filename_falls_back_to_its_stem(self):
        self.assertEqual(fsi.vendor_from_filename("mystery_supplier.xlsx"), "mystery_supplier")


class ResolveMatchesTests(unittest.TestCase):
    """The optional product-master join-back must degrade cleanly: this repo
    ships no such table by default, so absence must never raise."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.records = [{"style_no": "S1001"}, {"style_no": "S1002"}]

    def tearDown(self) -> None:
        self.conn.close()

    def test_missing_table_returns_empty_dict(self):
        matches = fsi.resolve_matches(self.conn, self.records)
        self.assertEqual(matches, {})

    def test_disabled_via_empty_table_name(self):
        old = fsi.PRODUCT_MASTER_TABLE
        fsi.PRODUCT_MASTER_TABLE = ""
        try:
            self.assertEqual(fsi.resolve_matches(self.conn, self.records), {})
        finally:
            fsi.PRODUCT_MASTER_TABLE = old

    def test_matches_when_a_compatible_table_exists(self):
        self.conn.execute(
            "CREATE TABLE product_master (style_no TEXT, sku TEXT, "
            "product_group TEXT, product_id TEXT, snapshot_date TEXT)")
        self.conn.execute(
            "INSERT INTO product_master VALUES ('S1001', 'SKU-1', 'GROUP-1', 'PID-1', '2026-01-01')")
        self.conn.commit()
        matches = fsi.resolve_matches(self.conn, self.records)
        self.assertEqual(matches["S1001"], ("SKU-1", "GROUP-1", "PID-1"))
        self.assertNotIn("S1002", matches)

    def test_incompatible_table_shape_degrades_to_no_matches(self):
        # A table happens to exist under the configured name but doesn't
        # have the expected columns -- must not crash the import.
        self.conn.execute("CREATE TABLE product_master (totally_different_column TEXT)")
        self.conn.commit()
        self.assertEqual(fsi.resolve_matches(self.conn, self.records), {})

    def test_no_style_numbers_short_circuits_without_querying(self):
        self.conn.execute("CREATE TABLE product_master (style_no TEXT)")
        self.conn.commit()
        self.assertEqual(fsi.resolve_matches(self.conn, [{"style_no": None}]), {})


class ProcessFolderDryRunTests(unittest.TestCase):
    """process_folder without real files: an empty folder must report zero
    rows and never touch the database (dry_run and "nothing found" both take
    this path)."""

    def test_empty_records_list_writes_nothing_even_without_dry_run(self):
        conn = sqlite3.connect(":memory:")
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                stats = fsi.process_folder(d, conn, snapshot_date="2026-01-01")
            self.assertEqual(stats["n_rows"], 0)
            self.assertEqual(stats["n_files"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
