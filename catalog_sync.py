r"""
Cross-channel product catalog + identity layer -> warehouse.

Pulls each sales channel's own product catalog and builds one table that lets
a product identifier TRANSLATE between channels:

  catalog_shopify               Shopify Admin GraphQL: one row per VARIANT
                                (ids, sku, titles, status, price/compare-at,
                                unit cost, handle, images, tags, metafields)
  catalog_shopify_market_price  optional Shopify Markets contextual prices,
                                one row per variant per configured country
  shopify_price_history         change-log of price / compare-at moves
  catalog_tiktok                TikTok Shop products search: product_id <->
                                sku_id <-> seller_sku
  catalog_amazon                SP-API merchant listings report: seller_sku
                                <-> ASIN
  product_xref                  every (channel, id, sku, title) sighting,
                                keyed by sku_key (exact) and match_key (fuzzy,
                                title-derived) so channels join even when
                                their own ids differ. Fully derived: rebuilt
                                from the tables above (+ ga_products and
                                orders, when present) on every run.

server.py's `product_lookup` tool reads product_xref; see docs/catalog.md.

Each channel is optional and SKIPPED (not failed) when its credentials are
absent, the same as every other connector here. A channel that runs and
returns nothing logs `degraded`, never `ok`.

USAGE
  python catalog_sync.py                       # every configured channel, full pull
  python catalog_sync.py --days 3              # incremental: products UPDATED in the
                                               # last 3 days (Shopify + TikTok; the
                                               # Amazon listings report is always full)
  python catalog_sync.py --only-shopify --days 3
  python catalog_sync.py --only-tiktok --only-amazon
  python catalog_sync.py --only-shopify --product-ids 1000000000001,1000000000002
  python catalog_sync.py --xref-only           # rebuild product_xref, no API calls
  python catalog_sync.py --reconcile-status    # re-read every Shopify product's status
  python catalog_sync.py --probe               # one cheap request per channel, writes nothing

EXIT CODES: 0 ok, 75 degraded (an empty channel, a skipped over-cap window,
targeted ids that did not come back, an upstream report that never finished),
1 error.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import io
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors import shopify, tiktok_shop
from warehouse.connectors.amazon_orders import HOSTS as SPAPI_HOSTS
from warehouse.connectors.amazon_orders import _access_token as spapi_token
from warehouse.product_keys import norm_sku, norm_style

load_dotenv()

PLATFORM = "catalog"
EXIT_PAUSED = 75  # deliberate pause on an upstream fault (matches the rest of the repo)
CHANNELS = ("shopify", "tiktok", "amazon")

REQUIRED_ENV = {
    "tiktok": ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER"),
    "amazon": ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN",
               "SPAPI_MARKETPLACE_ID"),
}


class UpstreamPause(RuntimeError):
    """The platform, not this code, failed to deliver (report cancelled / never
    finished). Logged `degraded` with exit 75 rather than `error`."""


def missing_env(channel: str) -> list[str]:
    """Env vars a channel still needs; empty list = configured."""
    if channel == "shopify":
        missing = [] if os.environ.get("SHOPIFY_SHOP") else ["SHOPIFY_SHOP"]
        # Same gate as run_sync.py: a legacy static token, or the client-
        # credentials pair the shared connector exchanges itself.
        if not os.environ.get("SHOPIFY_ADMIN_TOKEN"):
            missing += [v for v in ("SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET")
                        if not os.environ.get(v)]
        return missing
    return [v for v in REQUIRED_ENV[channel] if not os.environ.get(v)]


DDL = """
CREATE TABLE IF NOT EXISTS catalog_shopify (
    variant_id        TEXT NOT NULL PRIMARY KEY,
    product_id        TEXT NOT NULL,
    sku               TEXT,     -- NOT UNIQUE: one SKU can sit on several product_ids
                                -- (archived duplicates, re-listings). Filter
                                -- status != 'ARCHIVED' before any SKU-keyed lookup.
    title             TEXT,
    variant_title     TEXT,
    unit_cost         REAL,     -- inventoryItem.unitCost (needs read_inventory)
    status            TEXT,     -- ACTIVE | DRAFT | ARCHIVED (product-grain)
    vendor            TEXT,
    price             REAL,     -- current selling price, store currency
    compare_at_price  REAL,     -- MSRP anchor; > price means the variant is marked down
    handle            TEXT,     -- URL slug: product page = <store>/products/<handle>
    image_url         TEXT,     -- variant image, falling back to the product's featured image
    description       TEXT,     -- plain-text product description (product-grain)
    material          TEXT,     -- optional metafields, mapped by SHOPIFY_CATALOG_METAFIELDS
    pattern           TEXT,
    category          TEXT,
    tags              TEXT,     -- raw comma-joined product tags, unfiltered (product-grain)
    launch_date_epoch INTEGER,  -- optional metafield (unix seconds)
    additional_images TEXT,     -- comma-joined gallery URLs (up to IMAGE_GALLERY_CAP),
                                -- EXCLUDING this row's own image_url
    synced_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_shopify_sku ON catalog_shopify(sku);
CREATE INDEX IF NOT EXISTS idx_catalog_shopify_product ON catalog_shopify(product_id);

-- Shopify Markets contextual pricing: what a shopper in `country` actually
-- sees, already rounded by the store's own market pricing rules. NOT an FX
-- conversion done here. Only populated when SHOPIFY_CATALOG_MARKETS is set.
CREATE TABLE IF NOT EXISTS catalog_shopify_market_price (
    variant_id       TEXT NOT NULL,
    country          TEXT NOT NULL,   -- ISO 3166-1 alpha-2, as configured
    currency         TEXT,            -- currencyCode Shopify returned
    price            REAL,
    compare_at_price REAL,
    synced_at        TEXT NOT NULL,
    PRIMARY KEY (variant_id, country)
);

-- Price CHANGE-LOG, not a daily dump: one row per variant per observed change
-- of price or compare_at_price. catalog_shopify is overwritten each run and
-- Shopify has no price-history API, so this table is the only place markdown
-- history accrues. It starts the day you first run this and can never be
-- backfilled -- never delete its rows.
CREATE TABLE IF NOT EXISTS shopify_price_history (
    variant_id       TEXT NOT NULL,
    observed_date    TEXT NOT NULL,   -- date the new value was first seen
    price            REAL,
    compare_at_price REAL,
    synced_at        TEXT NOT NULL,
    PRIMARY KEY (variant_id, observed_date)
);
CREATE INDEX IF NOT EXISTS idx_sph_variant ON shopify_price_history(variant_id);

CREATE TABLE IF NOT EXISTS catalog_tiktok (
    product_id TEXT NOT NULL,
    sku_id     TEXT NOT NULL,
    seller_sku TEXT,
    title      TEXT,
    status     TEXT,
    price      REAL,
    currency   TEXT,
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (product_id, sku_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_tiktok_sku ON catalog_tiktok(seller_sku);

-- One marketplace (SPAPI_MARKETPLACE_ID); price is in that marketplace's currency.
CREATE TABLE IF NOT EXISTS catalog_amazon (
    seller_sku TEXT NOT NULL PRIMARY KEY,
    asin       TEXT,
    title      TEXT,
    price      REAL,
    quantity   INTEGER,
    status     TEXT,
    synced_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_amazon_asin ON catalog_amazon(asin);

-- One row per identifier sighting; sku_key / match_key join across channels.
CREATE TABLE IF NOT EXISTS product_xref (
    match_key   TEXT NOT NULL,   -- normalized style name (fuzzy, title-derived)
    sku_key     TEXT NOT NULL,   -- normalized sku (exact); '' when the sighting has none
    channel     TEXT NOT NULL,   -- shopify | ga4 | tiktok | tiktok_order | amazon
    channel_id  TEXT NOT NULL,   -- the channel's own product id ('' if sku-only)
    channel_sku TEXT NOT NULL,   -- sku AS THE CHANNEL HAS IT (raw)
    title       TEXT,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (channel, channel_id, channel_sku)
);
CREATE INDEX IF NOT EXISTS idx_xref_key ON product_xref(match_key);
CREATE INDEX IF NOT EXISTS idx_xref_skukey ON product_xref(sku_key);
CREATE INDEX IF NOT EXISTS idx_xref_channel_id ON product_xref(channel_id);
"""

# Columns written to catalog_shopify, by NAME. Inserts are never positional:
# a column added later via ALTER TABLE lands at the table's physical end, not
# where the DDL lists it, so VALUES (?,?,...) would map onto the wrong columns.
CATALOG_SHOPIFY_COLS = (
    "variant_id", "product_id", "sku", "title", "variant_title", "unit_cost",
    "status", "vendor", "price", "compare_at_price", "handle", "image_url",
    "description", "material", "pattern", "category", "tags",
    "launch_date_epoch", "additional_images",
)
_SHOPIFY_COL_TYPES = {"unit_cost": "REAL", "price": "REAL", "compare_at_price": "REAL",
                      "launch_date_epoch": "INTEGER"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    # Idempotent migration so a database created by an older version of this
    # script picks up columns added since.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(catalog_shopify)")}
    for col in CATALOG_SHOPIFY_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE catalog_shopify ADD COLUMN {col} "
                         f"{_SHOPIFY_COL_TYPES.get(col, 'TEXT')}")
    conn.commit()


def _has_source(conn: sqlite3.Connection, name: str) -> bool:
    """A source table may legitimately not exist (its sync was never run). Views
    count too -- a type='table'-only check would silently blank view-backed data."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
        (name,)).fetchone() is not None


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- TikTok ----

TIKTOK_PRODUCTS_PATH = "/product/202309/products/search"
TIKTOK_OVER_CAP = 12052180  # "search results can not exceed 10000"
# Floor for a FULL pull (by create_time). A too-early floor costs one empty
# request; a too-late one silently drops every older product. TikTok Shop's
# earliest markets opened in 2021.
TIKTOK_CATALOG_FLOOR = "2021-01-01"
_MIN_WINDOW_SECONDS = 3600


def _fetch_tiktok_window(lo: int, hi: int, field: str = "create_time") -> list[tuple]:
    """Every product with `field` in [lo, hi], one row per SKU.

    Requests go through the shared tiktok_shop._fetch_page, which already
    refreshes an expired token (105001/105002) ONCE and REBUILDS + re-signs the
    request with a fresh timestamp -- replaying the old params would carry the
    expired token and a stale signed timestamp. The access token expires about
    weekly, so a long catalog walk WILL hit this; a direct caller without the
    retry dies on the same weekday every week while every other TikTok feed
    rides straight through. Transient upstream codes are retried there too."""
    body = json.dumps({f"{field}_ge": lo, f"{field}_le": hi}, separators=(",", ":"))
    shop_cipher = os.environ["TIKTOK_SHOP_CIPHER"]
    rows: list[tuple] = []
    page_token = ""
    while True:
        data = tiktok_shop._fetch_page(shop_cipher, TIKTOK_PRODUCTS_PATH, body, page_token)
        for p in data.get("products") or []:
            # A product with no SKUs still gets one row (sku_id '') so its
            # product id is known.
            for s in p.get("skus") or [{}]:
                price = s.get("price") or {}
                rows.append((
                    str(p.get("id")), str(s.get("id") or ""), s.get("seller_sku"),
                    p.get("title"), p.get("status"),
                    _f(price.get("tax_exclusive_price") or price.get("sale_price")),
                    price.get("currency"),
                ))
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break
    return rows


def fetch_tiktok_catalog(days: int | None = None, on_window=None,
                         now: int | None = None) -> tuple[list[tuple], list[tuple[int, int]]]:
    """Walk the catalog in time windows. Search caps at 10,000 results, so a
    window that exceeds it is split in half until it fits.

    days=None -> full catalog by create_time since TIKTOK_CATALOG_FLOOR.
    days=N    -> incremental: products UPDATED in the last N days.

    on_window(rows) is called per completed window, so the caller can commit as
    it goes and a crash late in a long walk loses nothing already paged.

    Returns (rows, skipped). rows is empty when on_window is given. skipped
    lists windows that still exceeded the cap at one hour wide (a bulk-listing
    hour with >10k products) -- unlistable through this API, so they are
    REPORTED, never silently dropped."""
    rows: list[tuple] = []
    skipped: list[tuple[int, int]] = []
    now = int(time.time()) if now is None else now
    if days:
        field, lo = "update_time", now - days * 86400
    else:
        field = "create_time"
        lo = calendar.timegm(time.strptime(TIKTOK_CATALOG_FLOOR, "%Y-%m-%d"))
    stack = [(lo, now)]
    while stack:
        a, b = stack.pop()
        try:
            got = _fetch_tiktok_window(a, b, field)
        except RuntimeError as e:
            if str(TIKTOK_OVER_CAP) not in str(e):
                raise
            if b - a > _MIN_WINDOW_SECONDS:
                mid = (a + b) // 2
                stack += [(a, mid), (mid, b)]
            else:
                skipped.append((a, b))
                print(f"    tiktok window {time.strftime('%Y-%m-%d %H:%M', time.gmtime(a))}"
                      f" exceeds the 10k cap even at 1h -- skipped", flush=True)
            continue
        if on_window:
            on_window(got)
        else:
            rows.extend(got)
        if got:
            print(f"    tiktok {field} {time.strftime('%Y-%m-%d', time.gmtime(a))}"
                  f"..{time.strftime('%Y-%m-%d', time.gmtime(b))}: {len(got)} sku rows",
                  flush=True)
    return rows, skipped


def store_tiktok_rows(conn: sqlite3.Connection, rows: list[tuple], stamp: str) -> int:
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO catalog_tiktok "
            "(product_id, sku_id, seller_sku, title, status, price, currency, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [r + (stamp,) for r in rows])
    return len(rows)


# ---------------------------------------------------------------- Amazon ----

AMAZON_REPORT_POLLS = 60          # x 15s: listings reports usually take a minute or two
AMAZON_POLL_SECONDS = 15


def _decode_report(raw: bytes) -> str:
    # Flat-file report encoding varies by marketplace; cp1252 has been observed
    # for the US. Try UTF-8 first and fall back rather than guess one.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_listings_report(text: str) -> list[tuple]:
    """GET_MERCHANT_LISTINGS_ALL_DATA (tab-separated) ->
    (seller_sku, asin, title, price, quantity, status)."""
    lines = text.splitlines()
    if not lines:
        return []
    idx = {c: i for i, c in enumerate(lines[0].split("\t"))}

    def col(parts, *names):
        for n in names:
            if n in idx and idx[n] < len(parts):
                return parts[idx[n]]
        return ""

    rows = []
    for line in lines[1:]:
        parts = line.split("\t")
        sku = col(parts, "seller-sku")
        if not sku:
            continue
        qty = _f(col(parts, "quantity"))
        rows.append((sku, col(parts, "asin1", "product-id"), col(parts, "item-name"),
                     _f(col(parts, "price")), int(qty) if qty is not None else None,
                     col(parts, "status")))
    return rows


def fetch_amazon_catalog() -> list[tuple]:
    host = SPAPI_HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]
    headers = {"x-amz-access-token": spapi_token(), "Content-Type": "application/json"}
    r = requests.post(f"{host}/reports/2021-06-30/reports", headers=headers, json={
        "reportType": "GET_MERCHANT_LISTINGS_ALL_DATA",
        "marketplaceIds": [os.environ["SPAPI_MARKETPLACE_ID"]],
    }, timeout=60)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Amazon listings report request {r.status_code}: {r.text[:200]}")
    report_id = r.json()["reportId"]

    doc_id = None
    for _ in range(AMAZON_REPORT_POLLS):
        time.sleep(AMAZON_POLL_SECONDS)
        headers["x-amz-access-token"] = spapi_token()
        st = requests.get(f"{host}/reports/2021-06-30/reports/{report_id}",
                          headers=headers, timeout=60).json()
        status = st.get("processingStatus")
        if status == "DONE":
            doc_id = st["reportDocumentId"]
            break
        if status == "CANCELLED":
            raise UpstreamPause(f"Amazon cancelled the listings report {report_id}")
        if status == "FATAL":
            raise RuntimeError(f"Amazon listings report failed: {st}")
    if not doc_id:
        raise UpstreamPause("Amazon listings report did not finish in time")

    doc = requests.get(f"{host}/reports/2021-06-30/documents/{doc_id}",
                       headers=headers, timeout=60).json()
    raw = requests.get(doc["url"], timeout=120).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return parse_listings_report(_decode_report(raw))


def store_amazon_rows(conn: sqlite3.Connection, rows: list[tuple], stamp: str) -> int:
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO catalog_amazon "
            "(seller_sku, asin, title, price, quantity, status, synced_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [r + (stamp,) for r in rows])
    return len(rows)


# --------------------------------------------------------------- Shopify ----

# OUR cap on the images() connection -- not Shopify's limit. 10 silently
# dropped every image past the 10th (no error, no log) for every product with
# a bigger gallery; 25 is a deliberate middle ground rather than paginating
# every gallery (some products run 50-250+ images, and storing all of them is
# a cost decision, not just a completeness one). A gallery that comes back
# exactly this long may have been cut: a short page under a cap you set does
# NOT mean "that is everything".
IMAGE_GALLERY_CAP = 25

# Bound on the joined additional_images string. A full 25-image gallery of
# ~150-230-char CDN URLs joins to ~4-6k chars, so this is headroom. It is
# applied by WHOLE URL: a flat `[:N]` slice of the joined string cuts the last
# URL in half and stores a corrupt link, rather than merely a shorter list.
_ADDITIONAL_IMAGES_MAX_CHARS = 8000

PAGE_SIZE = 50                 # products per page; lower it on MAX_COST_EXCEEDED
VARIANTS_PER_PAGE = 100
PRODUCT_IDS_PER_QUERY = 50     # ids OR-ed into one search query string

METAFIELD_COLUMNS = ("material", "pattern", "category", "launch_date_epoch")
_IDENT = re.compile(r"^[A-Za-z0-9_\-]+$")


def _join_gallery(urls: Iterable[str]) -> str | None:
    """Comma-join URLs, dropping WHOLE urls off the end once the bound would be
    exceeded -- never truncating one mid-string."""
    kept: list[str] = []
    total = 0
    for u in urls:
        total += len(u) + (1 if kept else 0)  # +1 for the joining comma
        if total > _ADDITIONAL_IMAGES_MAX_CHARS:
            break
        kept.append(u)
    return ",".join(kept) or None


def market_countries() -> tuple[str, ...]:
    """SHOPIFY_CATALOG_MARKETS, e.g. 'CA,GB,AU'. Empty = no market pricing (and
    no extra query cost)."""
    raw = os.environ.get("SHOPIFY_CATALOG_MARKETS", "")
    out = tuple(c.strip().upper() for c in raw.split(",") if c.strip())
    bad = [c for c in out if not re.fullmatch(r"[A-Z]{2}", c)]
    if bad:
        raise ValueError(f"SHOPIFY_CATALOG_MARKETS: not ISO country codes: {bad}")
    return out


def metafield_map() -> dict[str, tuple[str, str]]:
    """SHOPIFY_CATALOG_METAFIELDS, e.g.
    'material=custom.material,launch_date_epoch=custom.launch_date'.
    Maps a catalog_shopify column onto a product metafield namespace.key.
    Unmapped columns are not requested and stay NULL."""
    raw = os.environ.get("SHOPIFY_CATALOG_METAFIELDS", "")
    out: dict[str, tuple[str, str]] = {}
    for item in (s.strip() for s in raw.split(",") if s.strip()):
        col, _, ref = item.partition("=")
        ns, _, key = ref.strip().partition(".")
        col = col.strip()
        if col not in METAFIELD_COLUMNS or not _IDENT.match(ns) or not _IDENT.match(key):
            raise ValueError(f"SHOPIFY_CATALOG_METAFIELDS: bad entry {item!r} (want "
                             f"<{'|'.join(METAFIELD_COLUMNS)}>=<namespace>.<key>)")
        out[col] = (ns, key)
    return out


def _variant_fields(markets: tuple[str, ...]) -> str:
    # One contextualPricing selection per market inside the SAME query -- no
    # extra round-trip per market. Codes are validated in market_countries().
    mkt = "\n".join(
        f"market_{c}: contextualPricing(context: {{country: {c}}}) "
        f"{{ price {{ amount currencyCode }} compareAtPrice {{ amount currencyCode }} }}"
        for c in markets)
    return f"""
          id sku title price compareAtPrice
          image {{ url }}
          inventoryItem {{ unitCost {{ amount }} }}
          {mkt}"""


def build_catalog_query(markets: tuple[str, ...] = (),
                        metafields: dict[str, tuple[str, str]] | None = None) -> str:
    mfs = "\n".join(f'mf_{col}: metafield(namespace: "{ns}", key: "{key}") {{ value }}'
                    for col, (ns, key) in (metafields or {}).items())
    return f"""
query($n: Int!, $cursor: String, $query: String, $imageCap: Int!) {{
  products(first: $n, after: $cursor, query: $query, sortKey: UPDATED_AT, reverse: true) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      id title status vendor tags handle description
      {mfs}
      featuredImage {{ url }}
      images(first: $imageCap) {{ nodes {{ url }} }}
      variants(first: {VARIANTS_PER_PAGE}) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {_variant_fields(markets)} }}
      }}
    }}
  }}
}}"""


def build_variants_query(markets: tuple[str, ...] = ()) -> str:
    """Follow-up page of one product's variants, for products with more than
    VARIANTS_PER_PAGE. Paginated, never warn-and-truncate."""
    return f"""
query($id: ID!, $cursor: String) {{
  product(id: $id) {{
    variants(first: {VARIANTS_PER_PAGE}, after: $cursor) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ {_variant_fields(markets)} }}
    }}
  }}
}}"""


def _bare(gid: str | None) -> str:
    """gid://shopify/Product/123 -> '123'."""
    return (gid or "").rsplit("/", 1)[-1]


def shape_product(p: dict, variants: list[dict], markets: tuple[str, ...] = (),
                  metafields: dict[str, tuple[str, str]] | None = None,
                  ) -> tuple[list[dict], list[dict]]:
    """One product node + all its variants -> (catalog rows, market-price rows)."""
    pid = _bare(p.get("id"))
    mf = {col: (p.get(f"mf_{col}") or {}).get("value") for col in (metafields or {})}
    launch = mf.get("launch_date_epoch")
    try:
        # Metafield values arrive as untyped strings.
        launch = int(launch) if launch not in (None, "") else None
    except (TypeError, ValueError):
        launch = None
    product_image = (p.get("featuredImage") or {}).get("url")
    gallery = [n.get("url") for n in (p.get("images") or {}).get("nodes") or [] if n.get("url")]
    tags = p.get("tags") or []
    tags_joined = ", ".join(tags) if tags else None

    catalog, market_rows = [], []
    for v in variants:
        vid = _bare(v.get("id"))
        image_url = (v.get("image") or {}).get("url") or product_image
        uc = ((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount")
        catalog.append({
            "variant_id": vid, "product_id": pid, "sku": v.get("sku") or "",
            "title": p.get("title"), "variant_title": v.get("title"),
            "unit_cost": _f(uc), "status": p.get("status"), "vendor": p.get("vendor"),
            "price": _f(v.get("price")), "compare_at_price": _f(v.get("compareAtPrice")),
            "handle": p.get("handle"), "image_url": image_url,
            "description": p.get("description"),
            "material": mf.get("material"), "pattern": mf.get("pattern"),
            "category": mf.get("category"), "tags": tags_joined,
            "launch_date_epoch": launch,
            # Excluded PER VARIANT, because which image is primary is decided
            # per variant. Google Merchant Center rejects a row whose
            # image_link also appears in additional_image_link.
            "additional_images": _join_gallery(u for u in gallery if u != image_url),
        })
        for c in markets:
            cp = v.get(f"market_{c}") or {}
            price = cp.get("price") or {}
            cap = cp.get("compareAtPrice") or {}
            if not price and not cap:
                continue  # nothing observed for this market -- not a zero
            market_rows.append({
                "variant_id": vid, "country": c,
                "currency": price.get("currencyCode") or cap.get("currencyCode"),
                "price": _f(price.get("amount")), "compare_at_price": _f(cap.get("amount")),
            })
    return catalog, market_rows


def _product_id_queries(product_ids: list[str]) -> list[str]:
    ids = [_bare(str(i).strip()) for i in product_ids if str(i).strip()]
    bad = [i for i in ids if not i.isdigit()]
    if bad:
        raise ValueError(f"--product-ids must be numeric Shopify product ids: {bad[:5]}")
    return [" OR ".join(f"id:{i}" for i in ids[k:k + PRODUCT_IDS_PER_QUERY])
            for k in range(0, len(ids), PRODUCT_IDS_PER_QUERY)]


def iter_shopify_catalog(days: int | None = None, product_ids: list[str] | None = None,
                         ) -> Iterator[tuple[list[dict], list[dict]]]:
    """Yield (catalog rows, market rows) one Shopify page at a time.

    days=N          only products whose updated_at is in the last N days.
    product_ids=[]  a targeted re-sync of exactly these products: for a known,
                    bounded set (say, rows that predate a new column) that
                    should not wait for updated_at to move and should not cost
                    a full catalog crawl. Mutually exclusive with days.
    neither         the whole catalog."""
    if days is not None and product_ids is not None:
        raise ValueError("days and product_ids are mutually exclusive")
    markets, metafields = market_countries(), metafield_map()
    query, vquery = build_catalog_query(markets, metafields), build_variants_query(markets)
    if product_ids is not None:
        searches: list[str | None] = _product_id_queries(product_ids)
    elif days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        searches = [f"updated_at:>='{cutoff.isoformat(timespec='seconds')}'"]
    else:
        searches = [None]

    for search in searches:
        cursor = None
        while True:
            data = shopify._post({"n": PAGE_SIZE, "cursor": cursor, "query": search,
                                  "imageCap": IMAGE_GALLERY_CAP}, query)
            block = data["products"]
            catalog, market_rows = [], []
            for p in block["nodes"]:
                vblock = p.get("variants") or {}
                variants = list(vblock.get("nodes") or [])
                vinfo = vblock.get("pageInfo") or {}
                while vinfo.get("hasNextPage"):
                    more = shopify._post({"id": p["id"], "cursor": vinfo.get("endCursor")},
                                         vquery)["product"]["variants"]
                    variants += more.get("nodes") or []
                    vinfo = more.get("pageInfo") or {}
                c, m = shape_product(p, variants, markets, metafields)
                catalog += c
                market_rows += m
            yield catalog, market_rows
            if not block["pageInfo"]["hasNextPage"]:
                break
            cursor = block["pageInfo"]["endCursor"]


def fetch_shopify_catalog(days: int | None = None, product_ids: list[str] | None = None,
                          ) -> tuple[list[dict], list[dict]]:
    """Whole result in memory -- convenient for small pulls and tests. main()
    streams via iter_shopify_catalog instead."""
    catalog, market_rows = [], []
    for c, m in iter_shopify_catalog(days=days, product_ids=product_ids):
        catalog += c
        market_rows += m
    return catalog, market_rows


def record_price_changes(conn: sqlite3.Connection, rows: list[dict], stamp: str) -> int:
    """Append to shopify_price_history where price/compare_at_price moved.

    Logged when the variant is new to us or either value differs from what
    catalog_shopify holds NOW -- so this MUST run before the upsert overwrites
    that basis, or the log is silently always empty. PK (variant_id,
    observed_date): several runs on one day collapse to that day's latest
    observation. Diffed SQL-side through a temp table rather than a second
    in-memory copy of the catalog."""
    rows = [r for r in rows if not (r["price"] is None and r["compare_at_price"] is None)]
    if not rows:
        return 0
    conn.execute("DROP TABLE IF EXISTS temp._price_incoming")
    conn.execute("CREATE TEMP TABLE _price_incoming "
                 "(variant_id TEXT PRIMARY KEY, price REAL, compare_at_price REAL)")
    conn.executemany(
        "INSERT OR REPLACE INTO _price_incoming (variant_id, price, compare_at_price) "
        "VALUES (:variant_id, :price, :compare_at_price)", rows)
    # Floats compared within half a cent; NULL vs a value counts as a change,
    # NULL vs NULL does not.
    n = conn.execute(
        """INSERT OR REPLACE INTO shopify_price_history
               (variant_id, observed_date, price, compare_at_price, synced_at)
           SELECT i.variant_id, ?, i.price, i.compare_at_price, ?
           FROM _price_incoming i
           LEFT JOIN catalog_shopify c ON c.variant_id = i.variant_id
           WHERE c.variant_id IS NULL
              OR NOT (   (c.price IS NULL AND i.price IS NULL
                          OR ABS(COALESCE(c.price,-1) - COALESCE(i.price,-1)) < 0.005)
                     AND (c.compare_at_price IS NULL AND i.compare_at_price IS NULL
                          OR ABS(COALESCE(c.compare_at_price,-1)
                                 - COALESCE(i.compare_at_price,-1)) < 0.005))""",
        (stamp[:10], stamp)).rowcount
    conn.execute("DROP TABLE _price_incoming")
    return n


def store_shopify_page(conn: sqlite3.Connection, catalog: list[dict],
                       market_rows: list[dict], stamp: str) -> int:
    """Price-history diff, then the catalog upsert, in ONE short transaction
    per page: the database-wide write lock is released between pages, and a
    crash mid-crawl keeps every page already stored.

    An UPSERT of whatever window was crawled, never a wholesale replace -- a
    `--days 3` run covers a fraction of the catalog, and replacing the table
    with it would discard every variant outside the window."""
    if not catalog and not market_rows:
        return 0
    cols = ", ".join(CATALOG_SHOPIFY_COLS)
    named = ", ".join(f":{c}" for c in CATALOG_SHOPIFY_COLS)
    with conn:
        changed = record_price_changes(conn, catalog, stamp)
        conn.executemany(
            f"INSERT OR REPLACE INTO catalog_shopify ({cols}, synced_at) "
            f"VALUES ({named}, :synced_at)",
            [{**r, "synced_at": stamp} for r in catalog])
        conn.executemany(
            "INSERT OR REPLACE INTO catalog_shopify_market_price "
            "(variant_id, country, currency, price, compare_at_price, synced_at) "
            "VALUES (:variant_id, :country, :currency, :price, :compare_at_price, :synced_at)",
            [{**r, "synced_at": stamp} for r in market_rows])
    if changed:
        print(f"    shopify_price_history: +{changed} price changes", flush=True)
    return len(catalog)


_SHOPIFY_STATUS_QUERY = """
query($n: Int!, $cursor: String) {
  products(first: $n, after: $cursor, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes { id status }
  }
}"""


def reconcile_shopify_status(conn: sqlite3.Connection, stamp: str) -> tuple[int, int]:
    """Re-read EVERY product's status and repair drifted catalog_shopify rows.

    The routine pass is incremental (updated_at window) and an upsert with no
    delete pass, so a product whose status changes outside that window -- a
    bulk archive, say -- keeps the last status we happened to see, forever.
    Anything trusting `status` then reads fiction. This asks for id+status
    only, so it stays cheap enough to schedule weekly, unlike a full re-crawl.

    Returns (products_seen, rows_repaired). Products deleted in Shopify are not
    seen and keep their last status."""
    seen = repaired = 0
    cursor = None
    while True:
        block = shopify._post({"n": 250, "cursor": cursor}, _SHOPIFY_STATUS_QUERY)["products"]
        rows = [(_bare(p["id"]), p["status"]) for p in block["nodes"]]
        seen += len(rows)
        with conn:
            for pid, status in rows:
                repaired += conn.execute(
                    "UPDATE catalog_shopify SET status=?, synced_at=? "
                    "WHERE product_id=? AND COALESCE(status,'') != ?",
                    (status, stamp, pid, status)).rowcount
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    return seen, repaired


# ------------------------------------------- rebuild without a long lock ----

REBUILD_CHUNK = 50_000


def rebuild_via_swap(conn: sqlite3.Connection, table: str, columns: str,
                     rows: list[tuple], chunk: int = REBUILD_CHUNK) -> int:
    """Replace `table`'s whole contents without holding the write lock for
    minutes, and without readers ever seeing a half-built table.

    SQLite's write lock is DATABASE-wide, so one DELETE + INSERT of ~1M rows
    blocks every other writer for as long as it runs. Chunking the REAL table
    instead would be a correctness bug: a reader landing mid-rebuild would see
    part of the table. So rows go into a shadow table in many short
    transactions, and the visible table is replaced by two metadata-only
    renames: readers see the old contents or the new, never a mixture.

    Schema and indexes are copied from sqlite_master, so adding a column or an
    index needs no change here. Indexes are recreated after the old table is
    dropped, because index names are database-global.

    legacy_alter_table is switched on around the renames: in modern mode a
    RENAME rewrites references inside VIEWS, which would silently re-point any
    view over `table` at `<table>__old` -- and then break it when that is
    dropped. In legacy mode views keep naming `table`, i.e. the new one."""
    new, old = f"{table}__new", f"{table}__old"
    tbl_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone()
    if not tbl_sql:
        raise RuntimeError(f"{table} does not exist -- DDL must run first")
    # sql IS NOT NULL skips the implicit PRIMARY KEY autoindex.
    idx_sqls = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,))]
    new_sql = re.sub(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["\'`\[]?' + re.escape(table) + r'["\'`\]]?',
        f'CREATE TABLE "{new}"', tbl_sql[0], count=1, flags=re.I)
    if new_sql == tbl_sql[0]:
        raise RuntimeError(f"could not derive shadow DDL for {table}")

    conn.executescript(f'DROP TABLE IF EXISTS "{new}"; DROP TABLE IF EXISTS "{old}";')
    conn.execute(new_sql)
    placeholders = ",".join("?" * (columns.count(",") + 1))
    insert = f'INSERT OR REPLACE INTO "{new}" ({columns}) VALUES ({placeholders})'
    for i in range(0, len(rows), chunk):
        with conn:                      # short txn -- other writers get a turn
            conn.executemany(insert, rows[i:i + chunk])
    written = conn.execute(f'SELECT COUNT(*) FROM "{new}"').fetchone()[0]

    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        with conn:                      # the ONLY visible change: metadata, ~ms
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
            conn.execute(f'ALTER TABLE "{new}" RENAME TO "{table}"')
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
    conn.executescript(f'DROP TABLE IF EXISTS "{old}";')
    for idx_sql in idx_sqls:
        conn.execute(idx_sql)
    conn.commit()
    return written


# ------------------------------------------------------------ xref build ----

XREF_COLS = "match_key, sku_key, channel, channel_id, channel_sku, title, synced_at"


def build_xref(conn: sqlite3.Connection, stamp: str) -> int:
    """Rebuild product_xref from whatever source tables exist. Returns the
    number of SIGHTINGS (before duplicate keys collapse)."""
    # (match_key, channel, channel_id, channel_sku, title)
    sightings: list[tuple] = []
    has_shopify = _has_source(conn, "catalog_shopify")

    if has_shopify:
        # ARCHIVED rows are left out: a SKU is not unique in catalog_shopify,
        # and an archived duplicate of a live product would otherwise sit
        # beside it under the same sku_key with stale data.
        for vid, sku, title in conn.execute(
                "SELECT variant_id, COALESCE(sku,''), title FROM catalog_shopify "
                "WHERE COALESCE(status,'') != 'ARCHIVED'"):
            sightings.append((norm_style(title), "shopify", vid, sku, title))

    if _has_source(conn, "ga_products"):
        # A GA4 item_id is whatever the site's tagging sends: often the SKU,
        # sometimes a product id. Keep it as a SKU only when it IS one in the
        # Shopify catalog; otherwise leave the sighting SKU-less rather than
        # invent a variant.
        exact = ("CASE WHEN EXISTS (SELECT 1 FROM catalog_shopify s WHERE s.sku = g.item_id) "
                 "THEN g.item_id ELSE '' END") if has_shopify else "''"
        for item_id, name, sku in conn.execute(
                f"SELECT g.item_id, MAX(g.item_name), {exact} FROM ga_products g "
                f"GROUP BY g.item_id"):
            sightings.append((norm_style(name), "ga4", item_id, sku, name))

    if _has_source(conn, "catalog_tiktok"):
        for pid, sku, title in conn.execute(
                "SELECT product_id, COALESCE(seller_sku,''), title FROM catalog_tiktok"):
            sightings.append((norm_style(title), "tiktok", pid, sku, title))

    if _has_source(conn, "orders"):
        # Order SKUs reach products the catalog no longer lists (delisted).
        for sku, name in conn.execute(
                "SELECT sku, MAX(product_name) FROM orders "
                "WHERE platform='tiktok' AND sku != '' GROUP BY sku"):
            sightings.append((norm_style(name), "tiktok_order", "", sku, name))

    if _has_source(conn, "catalog_amazon"):
        # Amazon titles are SEO strings, so match_key is near-useless here;
        # Amazon joins through sku_key (MSKU with channel suffixes stripped).
        for sku, asin, title in conn.execute(
                "SELECT seller_sku, COALESCE(asin,''), title FROM catalog_amazon"):
            sightings.append((norm_style(title), "amazon", asin, sku, title))

    rebuild_via_swap(conn, "product_xref", XREF_COLS,
                     [(s[0], norm_sku(s[3]), s[1], s[2] or "", s[3], s[4], stamp)
                      for s in sightings if s[0] or s[3]])
    return len(sightings)


# ------------------------------------------------------------------ main ----

def _probe(selected: list[str]) -> int:
    """One cheap request per configured channel. Writes nothing."""
    ok = True
    for ch in selected:
        try:
            if ch == "shopify":
                shopify._post({"n": 1, "cursor": None, "query": None, "imageCap": 1},
                              build_catalog_query(market_countries(), metafield_map()))
            elif ch == "tiktok":
                now = int(time.time())
                body = json.dumps({"update_time_ge": now - 86400, "update_time_le": now},
                                  separators=(",", ":"))
                tiktok_shop._fetch_page(os.environ["TIKTOK_SHOP_CIPHER"],
                                        TIKTOK_PRODUCTS_PATH, body, "")
            elif ch == "amazon":
                spapi_token()  # the report itself is too heavy for a probe
            print(f"  {ch}: ok")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  {ch}: FAILED -- {e}")
    return 0 if ok else 1


def run_channel(conn: sqlite3.Connection, ch: str, stamp: str, days: int | None,
                product_ids: list[str] | None) -> dict:
    """Pull one channel. Returns {"status", "rows", "note"}; raises on a real error."""
    if ch == "tiktok":
        written = [0]

        def _commit(rows):
            written[0] += store_tiktok_rows(conn, rows, stamp)

        _, skipped = fetch_tiktok_catalog(days=days, on_window=_commit)
        if skipped:
            return {"status": "degraded", "rows": written[0],
                    "note": f"{written[0]} sku rows; {len(skipped)} 1h window(s) over the "
                            f"10k search cap skipped"}
        return _counted(written[0], "sku rows")

    if ch == "amazon":
        return _counted(store_amazon_rows(conn, fetch_amazon_catalog(), stamp), "listings")

    # shopify
    written, seen = 0, set()
    for catalog, market_rows in iter_shopify_catalog(days=days, product_ids=product_ids):
        written += store_shopify_page(conn, catalog, market_rows, stamp)
        seen.update(r["product_id"] for r in catalog)
        if written:
            print(f"    shopify catalog +{written} variants so far", flush=True)
    if product_ids is not None:
        # Record what was ASKED next to what came back.
        asked = {_bare(str(i).strip()) for i in product_ids if str(i).strip()}
        missing = sorted(asked - seen)
        if missing:
            return {"status": "degraded", "rows": written,
                    "note": f"targeted resync: {len(asked) - len(missing)}/{len(asked)} "
                            f"product ids returned; missing e.g. {missing[:5]}"}
        return {"status": "ok", "rows": written,
                "note": f"targeted resync: {len(asked)} product ids, {written} variants"}
    return _counted(written, "variants")


def _counted(n: int, what: str) -> dict:
    if n == 0:
        # An empty pull and a broken one look identical: never call it ok.
        return {"status": "degraded", "rows": 0, "note": f"0 {what} returned"}
    return {"status": "ok", "rows": n, "note": f"{n} {what}"}


def _worst(statuses: Iterable[str]) -> str:
    statuses = set(statuses)
    for s in ("error", "degraded"):
        if s in statuses:
            return s
    return "ok"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Cross-channel product catalog + product_xref.")
    p.add_argument("--only-shopify", action="store_true", help="pull the Shopify catalog")
    p.add_argument("--only-tiktok", action="store_true", help="pull the TikTok Shop catalog")
    p.add_argument("--only-amazon", action="store_true", help="pull the Amazon listings report")
    p.add_argument("--days", type=int,
                   help="incremental: only Shopify/TikTok products updated in the last N "
                        "days (the Amazon listings report is always a full snapshot)")
    p.add_argument("--product-ids",
                   help="comma-separated numeric Shopify product ids to re-sync (requires "
                        "--only-shopify; not combinable with --days)")
    p.add_argument("--xref-only", action="store_true",
                   help="rebuild product_xref from tables already in the warehouse (no API calls)")
    p.add_argument("--reconcile-status", action="store_true",
                   help="re-read every Shopify product's status (id+status only) and repair "
                        "catalog_shopify rows the incremental window never revisited")
    p.add_argument("--probe", action="store_true",
                   help="one cheap request per configured channel; writes nothing")
    args = p.parse_args(argv)

    only = [ch for ch in CHANNELS if getattr(args, f"only_{ch}")]
    if args.product_ids and only != ["shopify"]:
        p.error("--product-ids requires --only-shopify (and no other --only-* flag)")
    if args.product_ids and args.days is not None:
        p.error("--product-ids and --days are mutually exclusive")
    product_ids = ([s.strip() for s in args.product_ids.split(",") if s.strip()]
                   if args.product_ids else None)
    if product_ids is not None:
        try:
            _product_id_queries(product_ids)
        except ValueError as e:
            p.error(str(e))

    # Explicitly asked-for channels must be configured; otherwise skip quietly.
    for ch in only + (["shopify"] if args.reconcile_status else []):
        missing = missing_env(ch)
        if missing:
            raise SystemExit(f"{ch}: missing required env var(s): {', '.join(missing)}. "
                             f"See .env.example.")
    selected = only or [ch for ch in CHANNELS if not missing_env(ch)]
    skipped = [] if only else [ch for ch in CHANNELS if missing_env(ch)]

    if args.probe:
        return _probe(selected)

    db.init_db()
    conn = db.connect()
    conn.row_factory = None
    ensure_schema(conn)
    stamp = db.now()
    started = stamp

    if args.reconcile_status:
        try:
            seen, repaired = reconcile_shopify_status(conn, stamp)
        except Exception as e:  # noqa: BLE001
            conn.close()
            db.log_sync(PLATFORM, started, 0, "error", f"status reconcile: {e}")
            raise
        conn.close()
        status = "ok" if seen else "degraded"
        msg = f"status reconcile: {seen} products checked, {repaired} rows repaired"
        db.log_sync(PLATFORM, started, repaired, status, msg)
        print(msg)
        return 0 if status == "ok" else EXIT_PAUSED

    results: dict[str, dict] = {}
    if not args.xref_only:
        for ch in selected:
            print(f"{ch}: pulling catalog", flush=True)
            try:
                results[ch] = run_channel(conn, ch, stamp, args.days,
                                          product_ids if ch == "shopify" else None)
            except UpstreamPause as e:
                results[ch] = {"status": "degraded", "rows": 0, "note": str(e)}
            except Exception as e:  # noqa: BLE001 -- isolate: other channels still run
                results[ch] = {"status": "error", "rows": 0, "note": str(e)[:200]}
            print(f"  {ch}: {results[ch]['status']} -- {results[ch]['note']}", flush=True)

    # The xref is derived from whatever the tables now hold, so it is rebuilt
    # even after a channel failed: a failed channel's previous rows still count.
    try:
        sightings = build_xref(conn, stamp)
    except Exception as e:  # noqa: BLE001
        results["xref"] = {"status": "error", "rows": 0, "note": f"xref build failed: {e}"}
        sightings = 0
    else:
        results["xref"] = ({"status": "ok", "rows": sightings,
                            "note": f"{sightings} identifier sightings"} if sightings else
                           {"status": "degraded", "rows": 0,
                            "note": "0 identifier sightings (no catalog or order data yet)"})
    conn.close()

    status = _worst(r["status"] for r in results.values())
    parts = [f"{k} {v['status']}: {v['note']}" for k, v in results.items()]
    parts += [f"{ch} skipped (not configured)" for ch in skipped]
    if args.xref_only:
        parts.insert(0, "xref only")
    msg = " | ".join(parts)
    db.log_sync(PLATFORM, started, sum(r["rows"] for r in results.values()), status, msg)
    print(f"catalog: {status} -- {msg}")
    return {"ok": 0, "degraded": EXIT_PAUSED}.get(status, 1)


if __name__ == "__main__":
    sys.exit(main())
