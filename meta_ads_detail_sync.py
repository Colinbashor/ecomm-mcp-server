r"""
Meta AD-LEVEL and CREATIVE-LEVEL detail -> warehouse.

WHY THIS EXISTS
`ad_metrics` (platform='meta', from warehouse/connectors/meta_ads.py) is
CAMPAIGN grain, and if you also run meta_products_sync.py that's CATALOG-
PRODUCT grain. Neither carries an ad_id, an adset_id, or a creative id, so
neither can answer anything about a specific AD or the CREATIVE inside it.
That becomes a blocker the moment you want to know "did this specific video
work as a Meta ad" — e.g. when a business starts running videos originally
made for TikTok creator/affiliate marketing (via a tool like Reacher — see
docs/reacher.md) as Meta ads too, and wants to close the loop between the two
platforms.

AN OPTIONAL BRIDGE BACK TO A CREATOR PLATFORM
If you upload creator-marketing videos to the ad account through a tool that
stamps a consistent naming convention on the upload, that convention can
often be parsed straight out of Meta's own video title with no extra
instrumentation. This module ships one example — Reacher's convention:

    <creator_handle>_<Mon><Year>_RCHR_<8-hex>
    e.g. "creatorhandle_Sep2025_RCHR_68cc4beb"

`RCHR` is Reacher's own marker and the leading token is the creator's TikTok
HANDLE — the same space as `tiktok_shop_videos.username` (or, if you're
also running reacher_sync.py, `reacher_video_creative.creator_handle`).
`parse_reacher_title()` extracts it into `meta_ad_videos.creator_handle`, so
a video can be joined back to its TikTok creator without any extra API call.

This is entirely OPTIONAL and safe to ignore: `parse_reacher_title()` simply
returns `None` for any title that doesn't match the convention (which is most
of a typical account's library, especially before adopting the practice), and
the `is_reacher`/`creator_handle`/`period`/`reacher_hash` columns just stay
NULL. If you don't use Reacher or a similarly-named convention, this bridge
never fires and costs nothing — the ad/creative/video capture below is
useful entirely on its own. Swap in your own regex here if your upload tool
uses a different naming convention.

CREATOR IDENTITY WARNING if you do use this bridge: join on the HANDLE, not
on any display name a storefront or order feed might carry for the same
person — display names and handles are different identity spaces and rarely
match by string comparison (see docs/tiktok-shop.md's own identity-bridge
note). Handle match rates against your own creator roster will typically be
well under 100%: a residual of creators whose posts never tagged a shop
product, or who fall outside whatever window you've captured, is normal, not
a parsing failure.

WHAT IS CAPTURED
  meta_ad_daily      account x date x ad — the insight grain that's missing
                     from campaign-level ad_metrics. Impressions/clicks/spend/
                     link_clicks + the SAME canonical action types the base
                     connector uses (_PURCHASE_TYPES / _ATC_TYPES /
                     _CHECKOUT_TYPES from warehouse/connectors/meta_ads.py),
                     so purchases never double-count omni_* against pixel_*.
  meta_ad_creatives  ad -> creative -> video. CURRENT STATE, upserted, because
                     an ad's creative is an attribute and Meta exposes no
                     history for it. TWO video ids are stored per creative
                     because `creative.video_id` and
                     `object_story_spec.video_data.video_id` frequently
                     DISAGREE on the same creative, and only the latter is
                     reliably the actual ad-account video (see the trap below)
                     — `video_id_any` coalesces `story_video_id` first.
  meta_ad_videos     the ad-account video library, with the optional Reacher
                     convention parsed into creator_handle / period / hash
                     when it matches.

VOLUME: an active account can easily spend on 100+ ads in a single day, all
in one paginated insights request per day — cheap relative to a campaign-
level daily pull, comparable in row count to a per-product Shopping feed. The
creative lookup is bounded by the ads that actually spent in the requested
window; only the video library needs a real crawl, and only to seed it once.

THREE API TRAPS, all worth knowing before you build anything on top of this
 1. ADS can be read by id, VIDEOS generally cannot. `GET /{video_id}` and the
    `?ids=` batch form both return "(#10) Application does not have
    permission for this action" for a video even when the account's own
    `/advideos` EDGE returns the very same object without complaint. So
    creatives use a targeted id-batch read, and videos have to be crawled —
    see `crawl_videos`.
 2. `/advideos` errors (code 1, "Please reduce the amount of data you're
    asking for" — which `meta_ads._get_json` surfaces as `_RangeTooLarge`) at
    `limit=200`. Page it at 50.
 3. `/advideos` is only APPROXIMATELY newest-first, not strictly ordered (a
    real 50-row page has been observed running out of chronological sequence
    partway through), so "stop at the first already-known video" truncates
    the crawl early. The stop rule here is N consecutive barren PAGES
    instead of the first hit.

The insight and creative stages work BACKWARD from spend: only ad_ids that
actually appear in the requested window's insights get a creative lookup, and
only the videos those creatives reference are sought. Bounded by ads that
spent, not by whole-account history. `--full-video-crawl` exists separately
to seed the full video library beyond that routine bound.

Batch reads use Meta's `?ids=a,b,c` form, capped at BATCH_IDS (50) per
request.

USAGE
  meta_ads_detail_sync.py --days 3
  meta_ads_detail_sync.py --start 2026-08-01 --end 2026-08-24
  meta_ads_detail_sync.py --days 3 --only insights     # skip creative/video
  meta_ads_detail_sync.py --refresh-creatives          # re-read stored ads
  meta_ads_detail_sync.py --full-video-crawl           # seed the whole video library
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from warehouse import db as warehouse_db
from warehouse.connectors.meta_ads import (
    API_VERSION, _ATC_TYPES, _CHECKOUT_TYPES, _PURCHASE_TYPES, _RangeTooLarge,
    _action_total, _get_json,
)

load_dotenv()
DB = Path(os.environ.get("WAREHOUSE_DB", Path(__file__).resolve().parent / "warehouse.db"))

# Meta's ?ids= batch form. 50 is the documented ceiling and is also comfortably
# under the payload size that trips the code-1 "reduce the amount of data".
BATCH_IDS = 50
# /advideos and /adcreatives are heavy per row; 50 survives, 200 does not.
PAGE_LIMIT = 50
# Bounds on the /advideos crawl (see crawl_videos for why it must be a crawl).
MAX_VIDEO_PAGES = 80              # 80 x 50 = 4,000 for a routine incremental run
FULL_CRAWL_MAX_PAGES = 600        # --full-video-crawl; libraries can exceed 4,000
STOP_AFTER_BARREN_PAGES = 3       # ordering is only roughly newest-first

DDL = """
CREATE TABLE IF NOT EXISTS meta_ad_daily (
    account_id    TEXT NOT NULL,
    date          TEXT NOT NULL,
    ad_id         TEXT NOT NULL,
    ad_name       TEXT,
    adset_id      TEXT,
    adset_name    TEXT,
    campaign_id   TEXT,
    campaign_name TEXT,
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    link_clicks   INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,
    add_to_carts  REAL    DEFAULT 0,
    checkouts     REAL    DEFAULT 0,
    purchases     REAL    DEFAULT 0,
    revenue       REAL    DEFAULT 0,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, date, ad_id)
);
CREATE INDEX IF NOT EXISTS idx_meta_ad_daily_date ON meta_ad_daily(date);
CREATE INDEX IF NOT EXISTS idx_meta_ad_daily_ad   ON meta_ad_daily(ad_id);

CREATE TABLE IF NOT EXISTS meta_ad_creatives (
    ad_id            TEXT PRIMARY KEY,
    ad_name          TEXT,
    campaign_id      TEXT,
    adset_id         TEXT,
    status           TEXT,
    creative_id      TEXT,
    creative_name    TEXT,
    -- creative.video_id and object_story_spec.video_data.video_id are NOT
    -- guaranteed to be the same object -- they can disagree on the same
    -- creative -- and only one of them reliably resolves to the actual
    -- ad-account video. story_video_id has been the reliable one in
    -- practice; creative.video_id has not. So video_id_any coalesces
    -- story_video_id FIRST. Preferring creative.video_id (the more
    -- obviously-named field) can silently resolve nothing while looking
    -- correct -- don't "simplify" this back to that field.
    video_id         TEXT,
    story_video_id   TEXT,
    video_id_any     TEXT,
    instagram_media_id TEXT,
    thumbnail_url    TEXT,
    synced_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meta_creatives_video ON meta_ad_creatives(video_id_any);

CREATE TABLE IF NOT EXISTS meta_ad_videos (
    video_id       TEXT PRIMARY KEY,
    title          TEXT,
    created_time   TEXT,
    length_seconds REAL,
    -- parsed from the optional Reacher upload convention; NULL when the
    -- title doesn't follow it (the common case for most of a library).
    is_reacher     INTEGER DEFAULT 0,
    creator_handle TEXT,
    period         TEXT,
    reacher_hash   TEXT,
    synced_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meta_videos_handle ON meta_ad_videos(creator_handle);
"""

# <creator_handle>_<Mon><Year>_RCHR_<hex> -- Reacher's own upload-naming
# convention (see the module docstring). The handle itself may contain
# underscores, so the leading group is non-greedy and the literal _RCHR_
# anchors the split unambiguously. Swap this pattern out if your own
# creator-video upload tool uses a different convention.
REACHER_TITLE = re.compile(
    r"^(?P<handle>.+?)_(?P<period>[A-Za-z]{3}\d{4})_RCHR_(?P<hash>[0-9a-fA-F]+)$")


def parse_reacher_title(title: str | None) -> dict | None:
    """Split a Reacher-convention upload title into handle / period / hash.

    Returns None for any title that doesn't follow the convention — which is
    normal for most of an account's video library, not an error. See the
    module docstring: this bridge is entirely optional.
    """
    if not title:
        return None
    m = REACHER_TITLE.match(title.strip())
    if not m:
        return None
    return {"creator_handle": m.group("handle").lower(),
            "period": m.group("period"),
            "reacher_hash": m.group("hash").lower()}


def _account() -> str:
    return os.environ["META_AD_ACCOUNT_ID"]


def _token() -> str:
    return os.environ["META_ACCESS_TOKEN"]


def _batch_get(ids: list[str], fields: str) -> dict:
    """Meta's ?ids= multi-read. Returns {id: object}, skipping ids Meta drops."""
    out: dict = {}
    for i in range(0, len(ids), BATCH_IDS):
        chunk = ids[i:i + BATCH_IDS]
        payload = _get_json(f"https://graph.facebook.com/{API_VERSION}/", {
            "access_token": _token(),
            "ids": ",".join(chunk),
            "fields": fields,
        })
        for k, v in (payload or {}).items():
            if isinstance(v, dict):
                out[k] = v
    return out


# ---------------------------------------------------------------- insights

def fetch_day(day: str) -> list[tuple]:
    """One day of ad-level insights, paginated."""
    account = _account()
    url = f"https://graph.facebook.com/{API_VERSION}/{account}/insights"
    params = {
        "access_token": _token(),
        "level": "ad",
        "time_increment": 1,
        "time_range": f'{{"since":"{day}","until":"{day}"}}',
        "fields": ("ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,"
                   "impressions,clicks,spend,inline_link_clicks,actions,action_values"),
        "limit": 500,
    }
    rows: list[tuple] = []
    while url:
        payload = _get_json(url, params)
        for d in payload.get("data", []):
            actions = d.get("actions")
            rows.append((
                account, d.get("date_start", day), str(d.get("ad_id")),
                d.get("ad_name"), d.get("adset_id"), d.get("adset_name"),
                d.get("campaign_id"), d.get("campaign_name"),
                int(d.get("impressions", 0) or 0),
                int(d.get("clicks", 0) or 0),
                int(d.get("inline_link_clicks", 0) or 0),
                float(d.get("spend", 0) or 0),
                _action_total(actions, _ATC_TYPES),
                _action_total(actions, _CHECKOUT_TYPES),
                _action_total(actions, _PURCHASE_TYPES),
                _action_total(d.get("action_values"), _PURCHASE_TYPES),
            ))
        url = payload.get("paging", {}).get("next")
        params = None          # the next url already carries every param
    return rows


# ---------------------------------------------------------------- creatives

_CREATIVE_FIELDS = ("id,name,status,campaign_id,adset_id,"
                    "creative{id,name,video_id,thumbnail_url,"
                    "source_instagram_media_id,object_story_spec}")


def fetch_creatives(ad_ids: list[str]) -> list[tuple]:
    """ad -> creative -> video, for the given ads only."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for ad_id, ad in _batch_get(ad_ids, _CREATIVE_FIELDS).items():
        c = ad.get("creative") or {}
        story = ((c.get("object_story_spec") or {}).get("video_data") or {})
        vid = c.get("video_id")
        svid = story.get("video_id")
        rows.append((
            str(ad_id), ad.get("name"), ad.get("campaign_id"), ad.get("adset_id"),
            ad.get("status"), c.get("id"), c.get("name"),
            str(vid) if vid else None,
            str(svid) if svid else None,
            # story_video_id FIRST -- see the video_id_any note in the DDL.
            str(svid or vid) if (svid or vid) else None,
            c.get("source_instagram_media_id"), c.get("thumbnail_url"), stamp,
        ))
    return rows


def crawl_videos(known: set[str], want: set[str], max_pages: int = MAX_VIDEO_PAGES,
                 stop_after: int = STOP_AFTER_BARREN_PAGES) -> tuple[list[tuple], set[str]]:
    """Walk /advideos, storing every video not already known. Reacher-parsed.

    THIS IS A COLLECTION CRAWL BY NECESSITY, not by choice. Reading a video by
    id -- GET /{video_id} or the ?ids= batch form -- typically fails with
    "(#10) Application does not have permission for this action", while the
    account's /advideos EDGE returns the same objects happily. So the
    id-targeted read used for creatives is unavailable here and the library
    has to be walked instead.

    The walk is bounded three ways, since a real ad-account video library can
    run into the thousands while only a small fraction is tagged by any one
    naming convention:
      * it stops once every `want` id (the videos our spending ads actually
        reference) has been resolved,
      * it stops after `stop_after` consecutive pages containing nothing new,
      * and it never exceeds `max_pages`.

    Ordering is APPROXIMATELY newest-first but NOT strictly monotonic
    (observed live: a 50-row page ran out of chronological order partway
    through), so "stop at the first known id" would truncate early. Hence the
    consecutive-barren-pages rule rather than a single-row test.

    Returns (rows, unresolved) -- unresolved is the `want` ids never found, so
    the caller can report a gap instead of silently under-covering.
    """
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    url = f"https://graph.facebook.com/{API_VERSION}/{_account()}/advideos"
    params = {"access_token": _token(), "limit": PAGE_LIMIT,
              "fields": "id,title,created_time,length"}
    rows: list[tuple] = []
    found: set[str] = set()
    barren = 0
    for _ in range(max_pages):
        payload = _get_json(url, params)
        data = payload.get("data", [])
        if not data:
            break
        new_here = 0
        for v in data:
            vid = str(v.get("id"))
            found.add(vid)
            if vid in known:
                continue
            new_here += 1
            known.add(vid)
            p = parse_reacher_title(v.get("title")) or {}
            rows.append((
                vid, v.get("title"), v.get("created_time"),
                float(v.get("length") or 0) or None,
                1 if p else 0, p.get("creator_handle"), p.get("period"),
                p.get("reacher_hash"), stamp,
            ))
        barren = barren + 1 if new_here == 0 else 0
        if want and want <= found:
            break                      # every wanted video resolved
        if barren >= stop_after:
            break
        url = payload.get("paging", {}).get("next")
        params = None                  # the next url already carries every param
        if not url:
            break
    return rows, (want - found)


# ---------------------------------------------------------------- driver

def run(start: str, end: str, only: str | None = None,
        refresh_creatives: bool = False, full_video_crawl: bool = False) -> dict:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(DB, timeout=warehouse_db.BUSY_TIMEOUT_SECONDS)
    conn.executescript(DDL)
    stats = {"insight_rows": 0, "days_skipped": 0, "creatives": 0, "videos": 0,
             "reacher_videos": 0, "videos_unresolved": 0}

    seen_ads: set[str] = set()
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    while day <= last:
        try:
            rows = fetch_day(day.isoformat())
        except (_RangeTooLarge, RuntimeError, requests.RequestException) as exc:
            print(f"    meta ad detail {day}: skipped ({str(exc)[:80]})")
            stats["days_skipped"] += 1
            day += timedelta(days=1)
            continue
        # Commit per day so a long backfill never holds the write lock; SQLite
        # allows one writer and the nightly pipeline would starve behind it.
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta_ad_daily VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [r + (stamp,) for r in rows])
        stats["insight_rows"] += len(rows)
        seen_ads.update(r[2] for r in rows)
        day += timedelta(days=1)

    if only == "insights":
        conn.close()
        return stats

    # Work BACKWARD from spend rather than crawling the account: only ads that
    # actually appear in the window get a creative lookup.
    targets = sorted(seen_ads)
    if refresh_creatives:
        targets = sorted({r[0] for r in conn.execute("SELECT ad_id FROM meta_ad_creatives")}
                         | seen_ads)
    if targets:
        crows = fetch_creatives(targets)
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta_ad_creatives VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", crows)
        stats["creatives"] = len(crows)

        # Videos referenced by the creatives we just read. The title never
        # changes, so anything already stored needs no re-read.
        known = {r[0] for r in conn.execute("SELECT video_id FROM meta_ad_videos")}
        want = {r[9] for r in crows if r[9]} - known
        if want or full_video_crawl:
            vrows, unresolved = crawl_videos(
                known, set() if full_video_crawl else want,
                max_pages=FULL_CRAWL_MAX_PAGES if full_video_crawl else MAX_VIDEO_PAGES,
                stop_after=10**9 if full_video_crawl else STOP_AFTER_BARREN_PAGES)
            if vrows:
                with conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO meta_ad_videos "
                        "VALUES (?,?,?,?,?,?,?,?,?)", vrows)
            stats["videos"] = len(vrows)
            stats["reacher_videos"] = sum(1 for r in vrows if r[4])
            stats["videos_unresolved"] = len(unresolved)
            if unresolved:
                # Not fatal: an ad can reference a video buried deeper than the
                # crawl bound. Say so rather than under-cover in silence.
                print(f"    ({len(unresolved)} referenced video(s) not found in "
                      "this crawl — run --full-video-crawl to seed the library)")
    conn.close()
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--only", choices=["insights"],
                   help="insights only; skip creative + video lookups")
    p.add_argument("--refresh-creatives", action="store_true",
                   help="also re-read creatives for every ad already stored")
    p.add_argument("--full-video-crawl", action="store_true",
                   help="walk the whole /advideos library, not just what ads reference")
    args = p.parse_args()

    end = args.end or date.today().isoformat()
    start = args.start or (date.fromisoformat(end) - timedelta(days=args.days)).isoformat()

    warehouse_db.init_db()
    started = warehouse_db.now()
    try:
        s = run(start, end, args.only, args.refresh_creatives,
                args.full_video_crawl)
    except Exception as exc:                                    # noqa: BLE001
        warehouse_db.log_sync("meta_ads_detail", started, 0, "error", str(exc))
        raise
    status = "degraded" if s["days_skipped"] else "ok"
    msg = (f"{start} -> {end}; {s['creatives']} creatives, {s['videos']} new videos "
           f"({s['reacher_videos']} Reacher)")
    if s["days_skipped"]:
        msg += f"; {s['days_skipped']} day(s) skipped"
    warehouse_db.log_sync("meta_ads_detail", started, s["insight_rows"], status, msg)
    print(f"Meta ad detail: {s['insight_rows']} ad-day rows, {s['creatives']} creatives, "
          f"{s['videos']} new videos ({s['reacher_videos']} Reacher-tagged) "
          f"[{start} -> {end}]")
    return 1 if s["days_skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
