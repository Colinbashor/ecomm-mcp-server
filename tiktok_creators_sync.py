r"""
TikTok Shop creator/affiliate identity bridge -> `tiktok_creators`.

WHY THIS EXISTS
TikTok Shop's various APIs each expose a DIFFERENT identifier for the same
creator, and none of them share a join key with each other:
    * the order API (see warehouse/connectors/tiktok_shop.py) exposes a
      creator's numeric user_id and DISPLAY NAME on sample/affiliate orders.
    * the video-analytics API (tiktok_videos_sync.py) exposes only a
      creator's @HANDLE.
Joining "creator sent a sample" to "creator posted a video" on
display-name-vs-handle is unreliable -- people don't spell their own display
name and handle the same way, and display names collide. This script builds
one table, `tiktok_creators`, keyed on handle, that carries all three
identifiers (handle, display_name, creator_id) plus whatever profile stats
are available, so other tables can join through it.

TWO WAYS TO POPULATE IT, because TikTok doesn't hand this over cleanly either
way on its own:

1. API sync (`api` subcommand) -- POST /affiliate_seller/.../sample_applications/search.
   Each application record's `creator` object happens to carry username,
   nickname AND user_id together for any creator who has ever applied for a
   product sample -- exactly the population an affiliate/sample program cares
   about. This is the automated, no-file-needed path, but it only covers
   creators who applied for a sample through your program.

2. Manual CSV/XLSX import (`import` subcommand) -- for a Seller Center /
   Affiliate Center creator or affiliate roster EXPORT. Platforms like this
   often have no equivalent "list all my affiliate creators" read API, only a
   dashboard download, so a human-exported spreadsheet is sometimes the only
   route to a broader roster than the sample-application population above.
   Both paths write the SAME table, so anything joining on it benefits from
   whichever source you actually have.

AUTH SETUP (api subcommand only -- import needs no credentials at all)
Reuses the TikTok Shop app credentials + token handling in
`warehouse/connectors/tiktok_shop.py`. No new credentials needed.

GENERIC GOTCHAS
* SCOPE RE-AUTH TRAP (same lesson as the other TikTok scripts here): a
  partial re-authorization silently drops previously-granted scopes from the
  one live refresh token. The current access token keeps working until it
  expires, then calls needing the dropped scope fail with business error
  105005. Always re-authorize with the FULL scope list you depend on.
* ROLLING WINDOW, NOT FULL HISTORY: the sample-applications endpoint is a
  bounded, most-recent-N window -- TikTok returns a specific "only the most
  recent N applications can be queried" business error once you page past
  it, NOT a complete historical export, and there is no way to page further
  back. Treat the result as "creators visible in the current rolling
  window," never as "every creator who ever applied."
* PARTIAL CRAWL != FAILURE. This endpoint also rate-limits considerably
  faster than the order/video endpoints, so a full paginated walk commonly
  gets cut off by a rate limit or a dropped connection partway through. A
  half-finished map of creators is still far more useful than zero, so a
  partial run is treated as a successful, resumable-next-time result -- but
  see the next point for why that changes how it is WRITTEN.
* MERGE ON PARTIAL, REPLACE ON COMPLETE: because this table should reflect
  "who's currently in view," a run that finishes a COMPLETE pass replaces the
  API-sourced rows wholesale (so a creator who dropped out of the window
  disappears rather than lingering forever with stale data). A run that gets
  cut off PARTWAY THROUGH instead MERGES into the existing rows -- replacing
  wholesale on a partial run would let a short, unlucky run silently erase a
  longer, complete one from a prior day.
* CSV COLUMN NAMES ARE A GUESS. Exported header names vary by platform
  version/locale/report type. This script auto-detects a header row by
  looking for any known alias of the "handle" column, then maps the rest of
  COLUMN_ALIASES by name; run --dry-run first and extend COLUMN_ALIASES to
  match your actual export before trusting a real import.
* The importer skips the export's own grand-total/summary row (matched
  against TOTAL_MARKERS) rather than importing it as a fake "creator."

USAGE
  python tiktok_creators_sync.py api --dry-run
  python tiktok_creators_sync.py api
  python tiktok_creators_sync.py import creators_export.xlsx --dry-run
  python tiktok_creators_sync.py import creators_export.xlsx
  python tiktok_creators_sync.py import --dir imports/tiktok
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors.tiktok_shop import TOKEN_EXPIRED_CODES, _refresh_access_token

load_dotenv()

BASE = "https://open-api.tiktokglobalshop.com"
PATH = "/affiliate_seller/202409/sample_applications/search"
API_SOURCE = "api:sample_applications"
PAGE_SIZE = 50
REQUIRED_ENV = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER")

DDL = """
CREATE TABLE IF NOT EXISTS tiktok_creators (
    handle          TEXT,          -- @handle; join key to the video feed's `username`
    display_name    TEXT,          -- join key to the order feed's display name
    creator_id      TEXT,          -- join key to the order feed's stable user id
    followers       INTEGER,
    engagement_rate REAL,
    gmv             REAL,
    units           INTEGER,
    videos          INTEGER,
    category        TEXT,
    region          TEXT,
    source_file     TEXT NOT NULL,  -- 'api:sample_applications' or the imported filename
    synced_at       TEXT NOT NULL,
    PRIMARY KEY (handle, source_file)
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(DDL)


def check_required_env() -> None:
    """Raise a clear SystemExit (not a KeyError deep in a request) when
    credentials are missing, so a misconfigured .env fails fast and legibly.
    Only the `api` subcommand needs these -- `import` works on a local file
    with no TikTok credentials at all."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s): {', '.join(missing)}. See .env.example.")


# ---------------------------------------------------------------------------
# API sync
# ---------------------------------------------------------------------------

# App-level rate limit, a separate user-level rate limit (trips later in a
# long crawl), and a transient 500 -- all worth a backoff-and-retry rather
# than aborting the whole crawl.
TRANSIENT = {36009002, 36009037, 36009003}
ROLLING_WINDOW_EXCEEDED = 98001004  # "only the most recent N applications can be queried"


def _sign(params: dict, body: str, secret: str) -> str:
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    return hmac.new(secret.encode(), f"{secret}{PATH}{ordered}{body}{secret}".encode(), hashlib.sha256).hexdigest()


def _request(page_token: str) -> dict:
    """One signed page. Refreshes an expired token once; backs off and
    retries on rate limits and transient errors, and on a bare dropped
    connection (this endpoint has been observed to reset the connection
    outright on long paginated pulls, which is a transport failure, not an
    API error code, so it needs its own except clause or a multi-hundred-
    creator pull can be silently discarded by one late-page network blip)."""
    secret = os.environ["TIKTOK_APP_SECRET"]
    body = "{}"
    refreshed = False
    code = None
    for attempt in range(6):
        params = {
            "app_key": os.environ["TIKTOK_APP_KEY"],
            "shop_cipher": os.environ["TIKTOK_SHOP_CIPHER"],
            "timestamp": str(int(time.time())),
            "page_size": str(PAGE_SIZE),
        }
        if page_token:
            params["page_token"] = page_token
        params["sign"] = _sign(params, body, secret)
        try:
            r = requests.post(
                f"{BASE}{PATH}", params=params, data=body,
                headers={"Content-Type": "application/json",
                         "x-tts-access-token": os.environ["TIKTOK_ACCESS_TOKEN"]},
                timeout=60,
            )
            data = r.json()
        except requests.exceptions.RequestException as e:
            wait = 5 * (attempt + 1)
            print(f"    {type(e).__name__} -- waiting {wait}s")
            time.sleep(wait)
            continue
        code = data.get("code")
        if code in TOKEN_EXPIRED_CODES and not refreshed:
            _refresh_access_token()
            refreshed = True
            continue
        if code in TRANSIENT:
            wait = 5 * (attempt + 1)
            print(f"    rate limited / transient (code={code}) -- waiting {wait}s")
            time.sleep(wait)
            continue
        if code == ROLLING_WINDOW_EXCEEDED:
            return {"_rolling_window_exceeded": True}
        if code != 0:
            raise RuntimeError(f"sample_applications {r.status_code} code={code}: {data.get('message')}")
        return data.get("data", {}) or {}
    raise RuntimeError(f"sample_applications still failing after 6 attempts (last code={code}).")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_creators() -> tuple[dict[str, tuple], bool]:
    """Walk sample_applications/search once. Returns (handle -> row, complete).
    `complete` is False if the crawl was cut off by the rolling-window
    ceiling or gave up after a repeated failure -- see the module docstring
    for why that changes how the caller writes the result."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    creators: dict[str, tuple] = {}
    complete = True
    token, pages = "", 0
    while True:
        try:
            d = _request(token)
        except (RuntimeError, requests.exceptions.RequestException) as e:
            if not creators:
                raise
            print(f"  WARNING: stopped early at page {pages + 1}: {e}")
            print(f"  keeping the {len(creators)} creators collected so far (re-run to extend)")
            complete = False
            break
        if d.get("_rolling_window_exceeded"):
            print(f"  hit the API's rolling-window ceiling at page {pages}; "
                  f"treating the crawl as PARTIAL (this is expected, not a bug)")
            complete = False
            break
        rows = d.get("sample_applications") or []
        pages += 1
        for a in rows:
            c = a.get("creator") or {}
            handle = c.get("username")
            if not handle:
                continue
            gmv = (c.get("gmv") or {}).get("amount")
            creators[handle] = (
                handle, c.get("nickname"), c.get("user_id"),
                _int(c.get("follower_count")), None,
                float(gmv) if gmv is not None else None,
                None, _int(c.get("content_count")), None, None, API_SOURCE, now,
            )
        token = d.get("next_page_token") or ""
        if not token:
            break
        if pages % 10 == 0:
            print(f"    ...{pages} pages, {len(creators)} creators so far")
        time.sleep(1.0)  # this endpoint rate-limits fast; pace the paging
    print(f"  {pages} page(s), {len(creators)} distinct creators" + ("" if complete else "  [PARTIAL]"))
    return creators, complete


def sync_api(dry_run: bool = False) -> int:
    creators, complete = fetch_creators()
    if not creators:
        print("no creators returned -- nothing to do")
        return 0
    if dry_run:
        print("\nDRY RUN -- nothing written. Sample creators:")
        for r in list(creators.values())[:5]:
            print(f"    handle={r[0]:<24} id={r[2]:<20} followers={r[3]} name={r[1]!r}")
        return 0

    conn = db.connect()
    try:
        ensure_schema(conn)
        if complete:
            # Full pull: replace wholesale so a creator who's left the rolling
            # window (no longer applying / dropped from the program) disappears
            # rather than lingering forever with stale data.
            conn.execute("DELETE FROM tiktok_creators WHERE source_file = ?", (API_SOURCE,))
        else:
            print("  partial pull: merging into the existing map instead of replacing it")
        conn.executemany(
            """
            INSERT OR REPLACE INTO tiktok_creators
              (handle, display_name, creator_id, followers, engagement_rate,
               gmv, units, videos, category, region, source_file, synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            list(creators.values()),
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM tiktok_creators").fetchone()[0]
    finally:
        conn.close()
    print(f"wrote {len(creators)} creators ({total} total rows in tiktok_creators)")
    return len(creators)


# ---------------------------------------------------------------------------
# CSV/XLSX import (Seller Center / Affiliate Center roster export)
# ---------------------------------------------------------------------------

# target field -> accepted header names (lower-cased, trimmed). Extend to
# match your actual export after inspecting the --dry-run header dump.
COLUMN_ALIASES = {
    "handle":          ["creator username", "username", "handle", "creator handle", "tiktok handle"],
    "display_name":    ["creator name", "creator nickname", "nickname", "display name", "name"],
    "creator_id":      ["creator id", "user id", "creator_id", "uid", "user_id"],
    "followers":       ["followers", "follower count", "fans", "follower"],
    "engagement_rate": ["engagement rate", "engagement", "engagement %", "avg engagement"],
    "gmv":             ["gmv", "revenue", "sales", "total gmv"],
    "units":           ["units", "units sold", "items sold", "orders"],
    "videos":          ["videos", "video count", "posts", "content", "video posts"],
    "category":        ["category", "niche", "content category"],
    "region":          ["region", "country", "market"],
}
NUMERIC = {"followers", "engagement_rate", "gmv", "units", "videos"}
TOTAL_MARKERS = {"total", "grand total", "totals", "summary", "-"}


def _rows_from_file(path: Path) -> list[list]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            return [row for row in csv.reader(f)]
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _norm(s) -> str:
    return str(s).strip().lower() if s is not None else ""


def _find_header(rows: list[list]) -> int:
    """Header = the first row (scanning the top 10) that contains a handle alias."""
    wanted = set(COLUMN_ALIASES["handle"])
    for i, row in enumerate(rows[:10]):
        if any(_norm(c) in wanted for c in row):
            return i
    raise ValueError(
        "No header row with a recognizable creator-handle column found -- "
        "inspect the file and extend COLUMN_ALIASES['handle']."
    )


def _num(v):
    if v is None or v == "":
        return None
    s = str(v).replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse(path: Path) -> tuple[list[dict], dict]:
    rows = _rows_from_file(path)
    if not rows:
        return [], {}
    h = _find_header(rows)
    headers = [_norm(c) for c in rows[h]]
    idx = {}
    for field, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in headers:
                idx[field] = headers.index(a)
                break
    out = []
    for row in rows[h + 1:]:
        if not any(c not in (None, "") for c in row):
            continue
        handle = row[idx["handle"]] if "handle" in idx and idx["handle"] < len(row) else None
        if handle is None or _norm(handle) in TOTAL_MARKERS:  # skip grand-total row
            continue
        rec = {"handle": str(handle).lstrip("@").strip()}
        for field, i in idx.items():
            if field == "handle" or i >= len(row):
                continue
            val = row[i]
            rec[field] = _num(val) if field in NUMERIC else (str(val).strip() if val not in (None, "") else None)
        out.append(rec)
    return out, {f: (f in idx) for f in COLUMN_ALIASES}


def import_file(path: Path, dry_run: bool) -> int:
    records, mapped = parse(path)
    found = [f for f, ok in mapped.items() if ok]
    missing = [f for f, ok in mapped.items() if not ok]
    print(f"\n{path.name}: {len(records)} creator rows")
    print(f"  mapped columns : {', '.join(found) or '(none!)'}")
    print(f"  NOT found      : {', '.join(missing) or '(all mapped)'}")
    if records:
        s = records[0]
        print(f"  sample         : "
              f"{ {k: s.get(k) for k in ('handle', 'display_name', 'creator_id', 'followers', 'gmv')} }")
    if dry_run:
        print("  [dry-run] nothing written.")
        return 0
    if not records:
        return 0

    stamp = db.now()
    cols = ["handle", "display_name", "creator_id", "followers", "engagement_rate",
            "gmv", "units", "videos", "category", "region"]
    conn = db.connect()
    with conn:
        ensure_schema(conn)
        conn.execute("DELETE FROM tiktok_creators WHERE source_file = ?", (path.name,))
        conn.executemany(
            f"""
            INSERT OR REPLACE INTO tiktok_creators
              ({', '.join(cols)}, source_file, synced_at)
            VALUES ({', '.join(':' + c for c in cols)}, :source_file, :synced_at)
            """,
            [{**{c: r.get(c) for c in cols}, "source_file": path.name, "synced_at": stamp} for r in records],
        )
    conn.close()
    print(f"  wrote {len(records)} rows to tiktok_creators.")
    return len(records)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    api_p = sub.add_parser("api", help="pull creator identity from the sample-applications API")
    api_p.add_argument("--dry-run", action="store_true", help="fetch + report, write nothing")

    import_p = sub.add_parser("import", help="import a Seller/Affiliate Center creator export (.xlsx/.csv)")
    import_p.add_argument("files", nargs="*", help="export file(s)")
    import_p.add_argument("--dir", help="import every .xlsx/.csv in this folder")
    import_p.add_argument("--dry-run", action="store_true", help="preview + column mapping only, write nothing")

    args = p.parse_args()
    db.init_db()

    if args.cmd == "api":
        check_required_env()
        started = db.now()
        try:
            n = sync_api(dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            db.log_sync("tiktok_creators_api", started, 0, "error", str(e))
            raise
        if not args.dry_run:
            db.log_sync("tiktok_creators_api", started, n, "ok", "")
        return 0

    # cmd == "import"
    paths = [Path(f) for f in args.files]
    if args.dir:
        paths += sorted(Path(args.dir).glob("*.xlsx")) + sorted(Path(args.dir).glob("*.csv"))
    if not paths:
        raise SystemExit("Give a file or --dir. Use --dry-run first to check column mapping.")

    started = db.now()
    total = 0
    try:
        for path in paths:
            if not path.exists():
                print(f"skip (missing): {path}")
                continue
            total += import_file(path, args.dry_run)
    except Exception as e:  # noqa: BLE001
        db.log_sync("tiktok_creators_import", started, 0, "error", str(e))
        raise
    if not args.dry_run:
        db.log_sync("tiktok_creators_import", started, total, "ok", f"{len(paths)} file(s)")
    print(f"\nDone. {total} rows{' (dry-run)' if args.dry_run else ''}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
