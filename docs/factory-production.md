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
to "no matches" rather than erroring. The shipped defaults are
`PRODUCT_MASTER_TABLE = "product_master"` with columns `style_no`
(`PRODUCT_MASTER_STYLE_COLUMN`), `snapshot_date`
(`PRODUCT_MASTER_SNAPSHOT_COLUMN` — set to `""` if your table isn't
snapshot-keyed; otherwise only its latest `snapshot_date` is used), `sku`,
`product_group` and `product_id`. No such table ships with this repo.

**Database path.** Both scripts open `WAREHOUSE_DB` if it is set in the
process environment, else `warehouse.db` next to `factory_status_import.py`.
Neither script calls `load_dotenv()`, so a `WAREHOUSE_DB` that exists only in
`.env` is **not** picked up — export it in the shell if you need a
non-default path. They connect with plain `sqlite3.connect()` (SQLite's
default 5 s busy timeout, not the repo's long `db.BUSY_TIMEOUT_SECONDS`), so
avoid running them while a long sync is writing.

## Usage

```bash
python factory_status_import.py imports/production/03.15.2026            # one period's folder
python factory_status_import.py imports/production/03.15.2026 --dry-run  # preview, no writes
python factory_status_import.py imports/production/03.15.2026 --snapshot-date 2026-03-16  # override

# backfill an entire dated-folder archive: <root>/<year>/<month>/<dated folder>/*.xlsx
python factory_status_backfill.py "path/to/Production Archive"
python factory_status_backfill.py "path/to/Production Archive" --dry-run
python factory_status_backfill.py "path/to/Production Archive" --since 2025-01-01 --limit 10
python factory_status_backfill.py "path/to/Production Archive" --since 2025-01-01 --until 2025-06-30
```

`factory_status_import.py` arguments:

| Argument | Meaning |
|---|---|
| `folder` (positional) | One period's folder. Walked **recursively** for `*.xlsx` and `*.xls` files (case-insensitive extension; `.xlsm`/`.csv` are ignored). |
| `--snapshot-date YYYY-MM-DD` | Override the period date (default: parsed from the folder name, else today — see below). |
| `--dry-run` | Parse and print the summary; writes nothing (the table isn't even created). |

`factory_status_backfill.py` arguments:

| Argument | Meaning |
|---|---|
| `archive_root` (positional) | Outer archive folder. Must be laid out exactly `<archive_root>/<year folder>/<month folder>/<dated period folder>/`; files at any other depth are not seen (inside a period folder the recursive walk above applies). |
| `--since YYYY-MM-DD` | Only periods with `snapshot_date >=` this (string compare on the ISO date). |
| `--until YYYY-MM-DD` | Only periods with `snapshot_date <=` this. |
| `--limit N` | Stop after the first N periods (oldest first, applied after `--since`/`--until`). |
| `--dry-run` | Parse every period, write no rows. It still runs `CREATE TABLE IF NOT EXISTS` for the table. |

The backfill processes periods oldest-first on one shared connection by
calling `factory_status_import.process_folder()` for each, prints one
progress line per period (files, file errors, rows, matched %, seconds), and
ends with a summary (periods OK / failed, file errors, total rows, total
matched, elapsed minutes). A period whose processing raises is reported as
`FOLDER FAILED` on stderr and skipped; the run continues.

`snapshot_date` (the period this folder represents) is parsed from the
folder name. The two scripts support different conventions, and this is not
symmetric:

- `factory_status_import.py` run directly only recognizes a folder name that
  **starts with** a two-digit `MM.DD.YYYY` date (`03.15.2026`, or
  `03.15.2026 final`) via `snapshot_date_from_folder()`. Point it at a
  folder named some other way (e.g. `March 15`, or single-digit `3.15.2026`)
  without `--snapshot-date` and it silently falls back to **today's date**,
  not the folder's actual period — always pass `--snapshot-date` explicitly
  for anything but a numeric folder name. The chosen date is printed as
  `Snapshot date:` at the top of the run; check it.
- `factory_status_backfill.py` is looser on the numeric form (one- or
  two-digit month/day, found anywhere in the name: `3.5.2026`,
  `Week of 03.15.2026`) and additionally recognizes a month name or
  abbreviation + day (`March 15`, `Sept 3`, `Mar. 15`, case-insensitive)
  with the year taken from the first `20xx` in the grandparent (year)
  folder's name (`March 15` inside a `2024` folder), via its own
  `parse_snapshot_date()` (patterns `NUMERIC_DATE_RE` / `MONTH_NAME_RE` /
  `YEAR_IN_PARENT_RE`). A period folder matching neither is listed in a
  `WARNING` on stderr and skipped, not raised. This logic
  lives only in the backfill script, not in `factory_status_import.py`. It
  tolerates both conventions coexisting across different years of one
  archive, which is common if the naming scheme changed at some point.

`--snapshot-date` on `factory_status_import.py` always overrides whatever
would otherwise be parsed (or defaulted to today).

A bad/corrupted file never aborts the whole import — it's recorded in the
returned stats' `file_errors` (printed to stderr — as it happens by the
importer, after each period by the backfill — and listed again in the
importer's final summary) and everything else still loads. A legacy `.xls` with
`xlrd` not installed is reported the same way. Excel's own `~$...xlsx` lock
files (left while a workbook is open) match the file glob and will show up
as file errors; close the workbook or ignore them.

Re-importing the same `snapshot_date` replaces that period's rows wholesale:
one `DELETE ... WHERE snapshot_date = ?` for the **whole period** (every
vendor and file, including files no longer in the folder), then `INSERT`,
committed together. So re-running an import is safe, but re-running with
only some of a period's files present drops the others' rows for that
period. If a run parses **zero** rows (e.g. every file failed), nothing is
deleted or written — the previous rows for that date are left intact.

Neither script writes to `sync_log`, and both exit `0` even when files or
whole periods failed — read the printed summary (or the `file_errors` /
`periods_failed` counts) rather than relying on the exit code.

## Tables

`factory_production_status` — **primary key
`(snapshot_date, source_file, source_sheet, row_num)`**; delete-and-rewrite
per `snapshot_date` (see above). Rows from every period are kept, so the
table is a period-by-period history of each style's milestones. Created on
the first real write (`CREATE TABLE IF NOT EXISTS`).

| Column(s) | Meaning |
|---|---|
| `snapshot_date` | The period (ISO date) the folder represents. |
| `source_vendor` | From `vendor_from_filename()`: the first `VENDOR_FROM_FILENAME` pattern matching the lowercased filename, else the filename without extension. |
| `source_file` | Path of the file **relative to the period folder**, including any subfolder (e.g. `footwear/VendorB.xlsx`). |
| `source_sheet` | Worksheet name. |
| `row_num` | 1-based counter of *kept* product rows within (`source_file`, `source_sheet`) — not the spreadsheet's own row number (header, blank and skipped rows aren't counted). |
| `style_no` | Style/model number, with a trailing colour suffix like ` - RED` stripped. |
| `description`, `brand`, `collection_month`, `designer`, `buyer`, `colour`, `sizes`, `supplier`, `category`, `subcategory` | Product identity. `category` falls back to the file's subfolder path when the sheet has no category value. |
| `tech_pack_received_date`, `original_sample_received_date`, `artwork_received_date`, `sample_send_date`, `fit_comments_rcvd_date`, `revised_fit_sample_send_date`, `final_fit_approved`, `color_approved`, `print_embellishment_approved`, `branding_packaging`, `top_send_date_awb` | Development / approval milestones (treated as date fields). |
| `po_number`, `po_issue_date`, `cancel_date_on_po`, `planned_wh_date`, `planned_cut_date`, `actual_cut_date`, `planned_packing_date`, `actual_packing_date`, `original_ex_factory_date`, `actual_ex_factory_date`, `xf_date`, `planned_vessel_book_date`, `actual_vessel_book_date` | PO and production/shipping milestones (all but `po_number` are date fields; `collection_month` is one too). |
| `length_in`, `width_in`, `height_in`, `weight_lb`, `coo`, `hts` | Dimensions, country of origin, tariff code. |
| `fabric_type`, `fabric_content`, `fabric_cost_usd`, `yy_yards` | Fabric. |
| `units`, `actual_shipped`, `price_usd`, `total_amount` | Quantities and cost. `price_usd` is `TEXT`; the numeric columns are declared `REAL`, but values are not coerced, so a vendor's text lands as text. |
| `vendor`, `bulk_factory_name`, `fob_port`, `ship_mode`, `shipping_status`, `drop_flag`, `vendor_comments`, `internal_comments`, `sku_number`, `photo` | Logistics, status and free-text columns as the vendor filled them. |
| `extra_json` | JSON object of `{normalized header: value}` for every non-empty cell whose column didn't map to a field above (`NULL` if none). |
| `matched_sku_key`, `matched_product_group`, `matched_product_id` | From the optional product-master join (`MIN()` of each per style); `NULL` when unmatched or unconfigured. |
| `match_method` | `style_exact` when matched, else `NULL`. |

Value handling: blank cells and the Excel error tokens `#VALUE!`, `#N/A`,
`#REF!` become `NULL`; strings are trimmed. In the date fields an Excel serial
number is converted to `YYYY-MM-DD` when it falls in 1990–2035 (otherwise the
raw number is kept), and a real date cell is stored via `isoformat()` — so a
datetime cell reads like `2026-03-15T00:00:00`. Non-date text a vendor typed
into a date column (`TBD`, `approved`, …) is kept as text. Compare dates with
`date(col)` or `substr(col, 1, 10)` rather than raw string equality.

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
- **How a sheet is read.** Sheets with fewer than two rows are skipped. The
  header row is whichever of the first six rows has the most text cells
  longer than two characters. A sheet is only trusted as product data if a
  header maps to `style_no` or `description`; otherwise the whole sheet is
  skipped silently (a typical cause of "0 rows" for a new vendor — add a
  pattern to `FIELD_RULES`). Fully blank rows, and rows with neither a
  `style_no` nor a `description` value, are dropped. Headers are normalized
  first: CJK / Hangul characters removed, newlines to spaces, whitespace
  collapsed, lowercased.
- **Each canonical field maps to at most one column** — the first
  (left-most) header that matches it. A second column matching the same
  field lands in `extra_json` instead. Chartsheets are ignored, and `.xlsx`
  formulas are read as their cached values (`data_only=True`).
- **Product-master matching is exact** on the cleaned `style_no`, queried in
  chunks of 400. If the configured table exists but its columns don't fit,
  the `OperationalError` is swallowed and every row is simply unmatched.

## Tests

- `tests/test_factory_status_import.py` — header normalization,
  `FIELD_RULES` ordering, `build_column_map` (first column wins), Excel
  serial dates, `clean_value`, header-row detection, `vendor_from_filename`,
  `resolve_matches` (missing / disabled / compatible / incompatible table,
  no styles), and `process_folder` writing nothing when no records parse.
- `tests/test_factory_status_backfill.py` — `parse_snapshot_date` (numeric,
  numeric with trailing text, month name + parent year, abbreviations,
  unparseable) and `find_period_folders` (year/month/date walk, oldest-first
  sort, unparseable folders skipped).
