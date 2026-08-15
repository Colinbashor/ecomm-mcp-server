r"""
Import Amazon "Voice of the Customer" (VOC) CSV exports -> warehouse.amazon_voc.

VOC has no SP-API — Seller Central's dashboard is the only way to get it
(Performance > Voice of the Customer > Download). This script is a MANUAL-DROP
importer: someone downloads the CSV periodically and drops it into a local
folder (e.g. `imports/voc/`), and this loads it. Per ASIN/SKU it carries: CX
Health (Excellent..Very Poor), NCX rate (negative-customer-experience rate),
NCX orders / total orders, and the top NCX reason.

This is the same manual-CSV-import shape you'd reuse for any Seller Central
report that has no API: header-driven parsing (see "COLUMN NAMES DRIFT"
below) rather than fixed column positions, --dry-run to preview before
writing, and a snapshot_date grain so re-importing the same period is
idempotent (INSERT OR REPLACE) while re-importing a DIFFERENT period builds
history instead of overwriting it.

SNAPSHOT GRAIN (like the FBA-inventory / sales-rank connectors in this repo):
each file is one point-in-time snapshot; the recurring import builds history
over time as new exports land. snapshot_date comes from --date or the file's
modified-time date. INSERT OR REPLACE is keyed on (snapshot_date, sku), so
re-importing the same day's file refreshes it cleanly instead of duplicating.

COLUMN NAMES DRIFT between Seller Central export versions (observed across
report revisions of many Amazon reports, not just this one), so parsing is
header-driven: headers are normalized (lowercased, non-alphanumerics ->
underscore) and pulled by name across a list of known-candidate spellings —
add more candidates to the `_pick(...)` calls below if your export uses
different column names than the ones already listed. ALWAYS `--dry-run`
first: it prints the parsed columns + a sample row and writes nothing, so you
can confirm the column mapping actually matched before trusting it.

Usage:
    python voc_import.py imports/voc/voc_export.csv
    python voc_import.py --dir imports/voc
    python voc_import.py --dry-run --dir imports/voc
    python voc_import.py --date 2025-07-20 imports/voc/export.csv   # force snapshot date
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warehouse import db as warehouse_db  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = HERE / "warehouse.db"

DDL = """
CREATE TABLE IF NOT EXISTS amazon_voc (
    snapshot_date  TEXT NOT NULL,   -- --date or the file's mtime date (point-in-time)
    sku            TEXT NOT NULL,   -- seller SKU, or ASIN when the export omits SKU
    asin           TEXT,
    product_name   TEXT,
    cx_health      TEXT,            -- Excellent / Good / Fair / Poor / Very Poor
    ncx_rate       REAL,            -- negative-customer-experience rate (percent)
    ncx_orders     INTEGER,
    total_orders   INTEGER,
    top_ncx_reason TEXT,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, sku)
);
"""

_NULLISH = {"", "--", "-", "n/a", "na", "none", "null"}


def _norm(h: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(h).strip().lower()).strip("_")


def _pick(d: dict, *names: str) -> str:
    for n in names:
        if n in d and str(d[n]).strip().lower() not in _NULLISH:
            return str(d[n]).strip()
    return ""


def _num(v) -> float | None:
    s = str(v).strip().replace(",", "").rstrip("%")
    if s.lower() in _NULLISH:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(v) -> int | None:
    n = _num(v)
    return None if n is None else int(round(n))


def _read_rows(path: Path):
    """Yield dict rows keyed by normalized header, tolerating BOM + encoding drift."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = path.read_bytes().decode("latin-1", errors="replace")
    # Sniff delimiter (Seller Central emits comma; some locales use ';' or tab).
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    rdr = csv.reader(text.splitlines(), delimiter=delim)
    header = None
    for row in rdr:
        if not any(str(c).strip() for c in row):
            continue
        if header is None:
            header = [_norm(c) for c in row]
            continue
        yield {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}


def parse_file(path: Path, snapshot_date: str, stamp: str):
    rows, cols_seen = [], None
    for d in _read_rows(path):
        if cols_seen is None:
            cols_seen = list(d.keys())
        sku = _pick(d, "sku", "seller_sku", "msku")
        asin = _pick(d, "asin", "child_asin")
        if not sku and not asin:
            continue
        rows.append((
            snapshot_date,
            sku or asin,  # PK needs a sku; ASIN-only exports key on the ASIN
            asin,
            _pick(d, "product_name", "item_name", "title", "product_title"),
            _pick(d, "cx_health", "customer_experience_health", "cx_health_status"),
            _num(_pick(d, "ncx_rate", "negative_customer_experience_rate", "ncx_rate_percentage")),
            _int(_pick(d, "ncx_orders", "ncx", "negative_customer_experiences", "ncx_order_count")),
            _int(_pick(d, "total_orders", "orders", "order_count")),
            _pick(d, "top_ncx_reason", "top_reason", "top_negative_reason", "ncx_top_reason"),
            stamp,
        ))
    return rows, (cols_seen or [])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="*", help="VOC CSV file(s)")
    p.add_argument("--dir", help="import every *.csv in this folder")
    p.add_argument("--date", help="snapshot date YYYY-MM-DD (default: file mtime date)")
    p.add_argument("--dry-run", action="store_true", help="parse + print, write nothing")
    args = p.parse_args()

    paths: list[Path] = [Path(f) for f in args.files]
    if args.dir:
        paths += sorted(Path(args.dir).glob("*.csv"))
    if not paths:
        raise SystemExit("no files given (pass file paths or --dir imports/voc)")

    warehouse_db.init_db()
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(DDL)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    grand = 0
    for path in paths:
        if not path.exists():
            print(f"  SKIP {path}: not found")
            continue
        snap = args.date or datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc).date().isoformat()
        try:
            rows, cols = parse_file(path, snap, stamp)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED {path.name}: {e}")
            continue
        print(f"  {path.name}: {len(rows)} rows, snapshot {snap}")
        print(f"    columns seen: {', '.join(cols)}")
        if rows:
            s = rows[0]
            print(f"    sample: sku={s[1]} asin={s[2]} cx_health={s[4]!r} "
                  f"ncx_rate={s[5]} ncx_orders={s[6]} total_orders={s[7]} reason={s[8]!r}")
        if args.dry_run:
            continue
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO amazon_voc VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        grand += len(rows)

    conn.close()
    if args.dry_run:
        print("DRY RUN — nothing written.")
    else:
        warehouse_db.log_sync("amazon_voc", warehouse_db.now(), grand, "ok" if grand else "error")
        print(f"Amazon VOC: wrote {grand} rows")


if __name__ == "__main__":
    main()
