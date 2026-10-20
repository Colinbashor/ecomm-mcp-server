"""Hermetic tests for amazon_listing_quality_priority.py and
amazon_listing_quality_material_fix.py -- no network, in-memory SQLite.

Covers: the material parser (primary component, synonyms, prefix match,
unmapped words dropped), the SKU join (suffix stripping, archived duplicates
ignored), the priority ranking (mirror SKUs never win the representative pick,
fixability labels, blank-not-zero conversion rate) and the dry-run default.
"""
from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import amazon_listing_quality_material_fix as fix
import amazon_listing_quality_priority as prio
import amazon_listing_quality_sync as alq


class ParseMaterialTests(unittest.TestCase):
    def test_highest_percentage_wins_and_synonyms_map(self) -> None:
        self.assertEqual(fix.parse_primary_material("95% Polyester, 5% Elastane"), "Polyester")
        self.assertEqual(fix.parse_primary_material("30% Cotton 70% Viscose"), "Rayon")

    def test_multipart_string_does_not_leak_part_labels(self) -> None:
        self.assertEqual(
            fix.parse_primary_material("100% POLYESTER Headgear 100% POLYESTER"), "Polyester")

    def test_longest_key_first(self) -> None:
        self.assertEqual(fix.parse_primary_material("100% Faux Leather"), "Faux Leather")

    def test_unmapped_or_empty_is_dropped(self) -> None:
        self.assertIsNone(fix.parse_primary_material("100% POLYESTYER"))
        self.assertIsNone(fix.parse_primary_material("soft and stretchy"))
        self.assertIsNone(fix.parse_primary_material(""))
        self.assertIsNone(fix.parse_primary_material(None))  # type: ignore[arg-type]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    alq.ensure_schema(conn)
    conn.execute("CREATE TABLE catalog_shopify (sku TEXT, material TEXT, status TEXT, "
                 "synced_at TEXT)")
    conn.execute("CREATE TABLE amazon_traffic_weekly (asin TEXT, week_start TEXT, "
                 "units_ordered INT, ordered_sales REAL, sessions INT, buy_box_pct REAL)")
    return conn


def _listing(conn, sku, asin, issues=1, sev="ERROR", disc=1) -> None:
    conn.execute(
        "INSERT INTO amazon_listing_quality (seller_sku, asin, item_name, issue_count, "
        "max_severity, is_discoverable, is_buyable, synced_at) VALUES (?,?,?,?,?,?,1,'t')",
        (sku, asin, f"Item {asin}", issues, sev, disc))


def _issue(conn, sku, code, attrs="material", seq=0) -> None:
    conn.execute(
        "INSERT INTO amazon_listing_quality_issues (seller_sku, issue_seq, code, severity, "
        "attribute_names, synced_at) VALUES (?,?,?,'ERROR',?,'t')", (sku, seq, code, attrs))


class CandidatesTests(unittest.TestCase):
    def test_join_strips_suffix_and_ignores_archived(self) -> None:
        conn = _conn()
        _issue(conn, "SKU-1001-FBA", "18448")
        _issue(conn, "SKU-1002", "18448", attrs="color")  # not a material issue
        conn.executemany("INSERT INTO catalog_shopify VALUES (?,?,?,?)", [
            ("SKU-1001", "100% Cotton", "ACTIVE", "2"),
            ("SKU-1001", "100% Nylon", "ARCHIVED", "3"),   # stale duplicate
            ("SKU-1002", "100% Cotton", "ACTIVE", "2"),
        ])
        rows = fix.candidates(conn, ("-FBA",))
        self.assertEqual([(r["seller_sku"], r["proposed_value"]) for r in rows],
                         [("SKU-1001-FBA", "Cotton")])

    def test_no_suffix_config_means_no_match(self) -> None:
        conn = _conn()
        _issue(conn, "SKU-1001-FBA", "18448")
        conn.execute("INSERT INTO catalog_shopify VALUES ('SKU-1001','100% Cotton','ACTIVE','2')")
        self.assertEqual(fix.candidates(conn), [])


class DryRunTests(unittest.TestCase):
    def test_default_never_calls_amazon(self) -> None:
        with tempfile.TemporaryDirectory() as d, \
                patch.object(fix.warehouse_db, "connect_readonly", return_value=_conn()), \
                patch.object(fix.requests, "patch") as patch_mock, \
                patch.object(fix.requests, "get") as get_mock, \
                patch("sys.argv", ["x", "--out-dir", d]):
            self.assertEqual(fix.main(), 0)
            self.assertTrue((Path(d) / fix.OUT_CSV).exists())
        patch_mock.assert_not_called()
        get_mock.assert_not_called()


class PriorityTests(unittest.TestCase):
    def test_fixability_labels(self) -> None:
        self.assertEqual(prio.fixability(set()), "")
        self.assertEqual(prio.fixability({"18448"}), "Yes")
        self.assertEqual(prio.fixability({"101058"}), "No (Seller Central only)")
        self.assertEqual(prio.fixability({"18448", "300060"}), "Partial")
        self.assertEqual(prio.fixability({"18448", "999999"}), "Unknown")

    def test_mirror_sku_never_beats_primary(self) -> None:
        conn = _conn()
        _listing(conn, "mirror.SKU-1001", "B000000001", issues=5, sev="ERROR")
        _listing(conn, "SKU-1001", "B000000001", issues=0, sev=None)
        rows = conn.execute("SELECT * FROM amazon_listing_quality").fetchall()
        best = prio.pick_representatives(rows, "mirror.")
        self.assertEqual(best["B000000001"]["seller_sku"], "SKU-1001")
        # without a prefix configured the worse offer is the representative
        best = prio.pick_representatives(rows, "")
        self.assertEqual(best["B000000001"]["seller_sku"], "mirror.SKU-1001")

    def test_build_scores_and_leaves_missing_conversion_blank(self) -> None:
        conn = _conn()
        _listing(conn, "SKU-1001", "B000000001", issues=1, sev="ERROR")
        _issue(conn, "SKU-1001", "18448")
        _issue(conn, "SKU-1001", "101265", attrs="", seq=1)  # non-defect, excluded
        _listing(conn, "SKU-1002", "B000000002", issues=1, sev="WARNING")
        _issue(conn, "SKU-1002", "101058", attrs="size_chart")
        week = (date.today() - timedelta(days=14)).isoformat()
        conn.execute("INSERT INTO amazon_traffic_weekly VALUES ('B000000001',?,10,100.0,50,0.9)",
                     (week,))
        conn.execute("INSERT INTO amazon_traffic_weekly VALUES ('B000000002',?,4,40.0,0,0.5)",
                     (week,))
        with tempfile.TemporaryDirectory() as d:
            total, _ = prio.build(conn, Path(d))
            with open(Path(d) / prio.MAIN_NAME, newline="", encoding="utf-8") as f:
                out = {r["asin"]: r for r in csv.DictReader(f)}
        self.assertEqual(total, 2)
        self.assertEqual(float(out["B000000001"]["priority_score"]), 20.0)  # 10 units x ERROR(2)
        self.assertEqual(out["B000000001"]["api_fixable"], "Yes")
        self.assertEqual(out["B000000002"]["conversion_rate"], "")     # no sessions: blank
        self.assertEqual(out["B000000002"]["api_fixable"], "No (Seller Central only)")

    def test_clean_catalog_writes_nothing(self) -> None:
        conn = _conn()
        _listing(conn, "SKU-1001", "B000000001", issues=0, sev=None)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(prio.build(conn, Path(d)), (0, 0))
            self.assertFalse((Path(d) / prio.MAIN_NAME).exists())


if __name__ == "__main__":
    unittest.main()
