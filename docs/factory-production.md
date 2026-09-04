# Factory production tracking (manual-drop spreadsheets)

Normalizes an arbitrary mix of supplier/vendor production-tracking
spreadsheets — cut date, sample approval, ex-factory date, shipping status,
and similar milestones per style — into one table, even when every vendor
uses a different column layout (and some vary their layout per *sheet*
within one file).

**Scripts:** `factory_status_import.py`, `factory_status_backfill.py`
(standalone, manual-drop)

## Why this exists

If you manufacture physical goods, your suppliers likely send you a
periodic (often weekly) spreadsheet tracking each style through production.
Different vendors virtually never agree on a column layout, and header
wording drifts week to week even from the *same* vendor (line breaks,
trailing translated text, minor rewording). This connector matches columns
by keyword pattern rather than fixed position, so it tolerates that drift
instead of breaking on it.

There is no API here — production-tracking sheets essentially never have
one. This is a manual-drop importer, the same shape as `voc_import.py`: you
download or receive the files, drop them in a folder, and run the script
yourself (or on your own schedule).

## Setup

No credentials needed. Requires `openpyxl` (already in `requirements.txt`)
for `.xlsx` files; `xlrd` (optional, in `requirements.txt`) for legacy
`.xls` files.

**Before your first real import**, open `factory_status_import.py` and edit
two things for your own setup:

1. `VENDOR_FROM_FILENAME` — a list of (filename-pattern, vendor-name) pairs
   used to identify which vendor a file came from. The shipped list is
   placeholder examples ("Vendor A", "Vendor B") — replace them with
   patterns matching your own suppliers' filenames.
2. `FIELD_RULES` — the ordered, most-specific-first list of (canonical
   field, [regex, ...]) pairs used to match spreadsheet headers. The shipped
   list covers common apparel/production-tracking terminology and should
   work as a starting point, but expect to add patterns as you encounter new
   vendors or column wording it doesn't already recognize.

Optionally, if your warehouse has some other table mapping a style/model
number to your own internal product identity (e.g. an ERP export, a
hand-maintained product master), set `PRODUCT_MASTER_TABLE` and the
`PRODUCT_MASTER_*_COLUMN` constants to join this connector's `style_no`
column against it. Leave `PRODUCT_MASTER_TABLE` as its default (or set it to
`""`) to disable the join — `resolve_matches()` checks the table actually
exists before querying it, so an unconfigured or mismatched table degrades
to "no matches" rather than erroring.

## Usage

```bash
python factory_status_import.py imports/production/03.15.2026            # one period's folder
python factory_status_import.py imports/production/03.15.2026 --dry-run  # preview, no writes
python factory_status_import.py imports/production/03.15.2026 --snapshot-date 2026-03-16  # override

# backfill an entire dated-folder archive: <root>/<year>/<month>/<dated folder>/*.xlsx
python factory_status_backfill.py "path/to/Production Archive"
python factory_status_backfill.py "path/to/Production Archive" --dry-run
python factory_status_backfill.py "path/to/Production Archive" --since 2025-01-01 --limit 10
```

`snapshot_date` (the period this folder represents) is parsed from the
folder name by default — either a fully-numeric date (`03.15.2026`) or a
bare month name + day with the year taken from the grandparent folder
(`March 15` inside a `2024` folder); pass `--snapshot-date` to override it
directly. `factory_status_backfill.py` tolerates both conventions
coexisting across different years of one archive, which is common if the
naming scheme changed at some point.

A bad/corrupted file never aborts the whole import — it's recorded in the
returned stats (and printed to stderr) and everything else still loads.
Re-importing the same `snapshot_date` replaces that period's rows wholesale
(`DELETE` + re-`INSERT`), so re-running an import is always safe.

## Tables

- `factory_production_status` — one row per (snapshot_date, source_file,
  source_sheet, row_num). Canonical production-tracking columns (dates,
  vendor, category, cost, shipping status, ...) plus `extra_json` for any
  source column that didn't match a known field, and `matched_sku_key` /
  `matched_product_group` / `matched_product_id` / `match_method` for the
  optional product-master join-back (`NULL` when unmatched or unconfigured).

## Notes

- **Unmatched rows are kept, not dropped.** A style still in production and
  not yet in your product-master table is expected not to match on this
  run — `matched_*` columns stay `NULL` rather than the row being discarded,
  since a later re-run naturally re-links it once your product-master table
  picks the style up.
- **Any column that doesn't match a `FIELD_RULES` pattern is preserved
  verbatim** in the row's `extra_json` sidecar rather than silently dropped —
  useful both for auditing what a vendor's sheet actually contained and for
  spotting a header pattern worth adding to `FIELD_RULES`.
- **Header matching is *ordered* on purpose.** `FIELD_RULES` lists more
  specific patterns before looser ones that would otherwise also match (e.g.
  "actual cut date" is checked before the bare "cut date" pattern) — adding
  a new rule to the wrong place in the list can silently steal matches from
  an existing, more specific one.
- **A folder can nest files under category subfolders** (e.g. one vendor
  splitting apparel/footwear/accessories into separate files or sheets) —
  `factory_status_import.py` walks recursively and uses the path relative to
  the period folder as each file's identity, so two subfolders reusing the
  same filename for different categories don't collide.

## Tests

`tests/test_factory_status_import.py`, `tests/test_factory_status_backfill.py`
