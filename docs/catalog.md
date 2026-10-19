# Product catalog + cross-channel identity (`catalog_sync.py`)

Pulls each sales channel's own product catalog into `catalog_*` tables and
builds `product_xref`, one table that translates a product identifier between
channels: a Shopify variant id, a TikTok Shop product id, an Amazon ASIN, a GA4
item id, and the seller SKU that ties them together. The MCP tool
[`product_lookup`](#mcp-tool-product_lookup) reads it.

| Script | Type | Covers |
|---|---|---|
| `catalog_sync.py` | standalone | `catalog_shopify` (+ market prices, price change-log), `catalog_tiktok`, `catalog_amazon`, `product_xref` |
| `warehouse/product_keys.py` | shared library | `norm_sku()` / `norm_style()`, the two keys `product_xref` joins on (also used by `server.py`) |

## Setup

No new credentials: each channel reuses its core connector's.

| Channel | Credentials | Notes |
|---|---|---|
| Shopify | `SHOPIFY_SHOP` + `SHOPIFY_CLIENT_ID`/`SHOPIFY_CLIENT_SECRET` (or `SHOPIFY_ADMIN_TOKEN`) | needs the `read_products` Admin API scope, and `read_inventory` for `unit_cost` (from Shopify's docs; not verified against an app lacking it) |
| TikTok Shop | `TIKTOK_APP_KEY`, `TIKTOK_APP_SECRET`, `TIKTOK_ACCESS_TOKEN`, `TIKTOK_SHOP_CIPHER` (+ `TIKTOK_REFRESH_TOKEN` for auto-refresh) | needs a product-read scope on the Partner Center app |
| Amazon | `SPAPI_CLIENT_ID`, `SPAPI_CLIENT_SECRET`, `SPAPI_REFRESH_TOKEN`, `SPAPI_MARKETPLACE_ID`, `SPAPI_REGION` | the app needs the Reports role (listings report) |

A channel whose credentials are absent is **skipped, not failed**. Asking for
it explicitly (`--only-tiktok`) without credentials exits with the missing
variable names instead.

Optional knobs (all in `.env.example`):

| Variable | Default | Effect |
|---|---|---|
| `SHOPIFY_CATALOG_MARKETS` | empty | comma-separated ISO country codes (`CA,GB,AU`). Each adds a `contextualPricing` selection to the same query and a row per variant to `catalog_shopify_market_price`. Empty = no market pricing and no extra query cost |
| `SHOPIFY_CATALOG_METAFIELDS` | empty | maps the optional columns `material`, `pattern`, `category`, `launch_date_epoch` onto product metafields: `material=custom.material,launch_date_epoch=custom.launch_date`. Unmapped columns are not requested and stay NULL |
| `CATALOG_SKU_STRIP_SUFFIXES` | `FBA` | suffixes stripped from SKUs after a `-` to form `sku_key` (`SKU-1001-FBA` -> `SKU-1001`). Empty value = strip nothing |
| `CATALOG_TITLE_MARKERS` | empty | parenthesised markers removed from titles before keying (`Outlet` turns `Tote Bag (Outlet)` into `tote bag`) |
| `CATALOG_VARIANT_DELIMITER` | ` - ` | separator between style and variant in titles (`Mini Dress - Black`) |

Changing any of the last three changes every key. Rebuild afterwards with
`python catalog_sync.py --xref-only`, or old and new keys won't join.

## Usage

```bash
python catalog_sync.py --probe                 # one cheap request per configured channel; writes nothing
python catalog_sync.py                         # every configured channel, full pull, then product_xref
python catalog_sync.py --days 3                # incremental: Shopify/TikTok products UPDATED in the last 3 days
python catalog_sync.py --only-shopify --days 3
python catalog_sync.py --only-tiktok --only-amazon
python catalog_sync.py --only-shopify --product-ids 1000000000001,1000000000002
python catalog_sync.py --xref-only             # rebuild product_xref from tables already loaded; no API calls
python catalog_sync.py --reconcile-status      # re-read every Shopify product's status (id + status only)
```

A reasonable schedule: `--days 3` daily, a full pull weekly, and
`--reconcile-status` weekly.

### Flags

| Flag | Meaning |
|---|---|
| `--only-shopify` / `--only-tiktok` / `--only-amazon` | pull just these channels (combinable). Without any, every configured channel runs |
| `--days N` | incremental window on `updated_at` (Shopify) / `update_time` (TikTok). The Amazon listings report has no such filter and is always a full snapshot |
| `--product-ids A,B` | targeted Shopify re-sync of exactly these numeric product ids (gids accepted). Requires `--only-shopify` alone, not combinable with `--days`. Use it for a known, bounded set (rows that predate a new column, say) instead of waiting for `updated_at` to move or paying for a full crawl. Ids that don't come back are named in `sync_log` |
| `--xref-only` | rebuild `product_xref` only |
| `--reconcile-status` | repair `catalog_shopify.status` drift (see below) |
| `--probe` | Shopify: one 1-product page; TikTok: one products-search page; Amazon: an LWA token only (the report is too heavy for a probe) |

### Exit codes and `sync_log`

One `sync_log` row per run, platform `catalog`, with a per-channel breakdown
in `message`:

| Status | Exit | When |
|---|---|---|
| `ok` | 0 | every attempted channel returned rows and `product_xref` is non-empty |
| `degraded` | 75 | a channel returned **zero** rows (an empty pull and a broken one look identical, so it is never `ok`); a TikTok window over the 10k cap even at one hour wide; targeted `--product-ids` that didn't all come back; an Amazon report that Amazon cancelled or that never finished; an empty `product_xref` |
| `error` | 1 | a channel raised. The other channels still run, and `product_xref` is still rebuilt from whatever the tables hold |

An incremental `--days N` run on a quiet store can legitimately return zero
products and so reads `degraded`. If that's normal for your store, widen the
window rather than relaxing the rule.

## Tables

### `catalog_shopify` (one row per variant, PK `variant_id`)

`product_id`, `sku`, `title`, `variant_title`, `unit_cost`, `status`
(`ACTIVE`/`DRAFT`/`ARCHIVED`), `vendor`, `price`, `compare_at_price`, `handle`,
`image_url`, `description`, `material`, `pattern`, `category`, `tags`,
`launch_date_epoch`, `additional_images`, `synced_at`.

- **`sku` is NOT unique.** One SKU can sit on several `product_id`s: an
  archived duplicate of a live product, a re-listing, a product imported twice.
  After a bulk archive or channel migration this can be the norm, not the edge case.
  Any SKU-keyed lookup **must filter `status != 'ARCHIVED'` first**, or it can
  silently return a stale duplicate's price, images or title instead of the live
  product's. `variant_id` is the only safe variant-grain join key, and a naive
  `sku` join to order lines multiplies revenue. `product_xref` and
  `product_lookup` apply this filter for you.
- `image_url` is the variant's own image, falling back to the product's
  featured image (most variants that don't differ by colour have no image of
  their own).
- `additional_images` is the product gallery, up to `IMAGE_GALLERY_CAP` (25),
  comma-joined, **excluding the row's own `image_url`**. The exclusion is per
  variant, because which image is primary is decided per variant. Google
  Merchant Center rejects a row whose `image_link` also appears in
  `additional_image_link`.
- **Gallery cap rationale.** 25 is *our* cap on the `images()` connection, not
  Shopify's. A cap of 10 once silently dropped every image past the 10th, with
  no error and no log. Paginating every gallery fully (some products run
  50-250+ images) is a storage decision, not just a completeness one, so 25 is
  a deliberate middle ground. A gallery that comes back exactly 25 long may have
  been cut.
- **Never truncates a URL.** The joined string is bounded at 8,000 characters
  by `_join_gallery()`, which drops *whole* URLs off the end. A flat `[:N]`
  slice of the joined string would cut the last URL in half and store a corrupt
  link.
- `tags` is the raw tag list, joined with `", "` and unfiltered (stores often
  carry internal `key:value` tags and promo codes). NULL when the product has
  none. Filtering belongs in consumers.
- `material`/`pattern`/`category`/`launch_date_epoch` come from metafields only
  when `SHOPIFY_CATALOG_METAFIELDS` maps them. `launch_date_epoch` is parsed
  defensively (an unparseable value stores NULL).
- Product-grain fields (`tags`, `description`, metafields, gallery) are written
  onto every variant row.
- A column added later stays NULL until its product is re-fetched. `--days`
  only revisits recently updated products, so backfill a new column with a
  full pull or `--product-ids`.
- Products with more than 100 variants are paginated with a follow-up query.
  They are never truncated.

### `catalog_shopify_market_price` (PK `variant_id, country`)

`currency`, `price`, `compare_at_price`, `synced_at`. This is Shopify Markets'
own contextual price, already rounded by the store's market pricing rules. It
is **not** an FX conversion done here. A market that returned nothing for a
variant gets no row, rather than a zero. Mind the currency before summing
(data rule 10).

### `shopify_price_history` (PK `variant_id, observed_date`)

A change-log: one row whenever a variant is new to us, or its `price` /
`compare_at_price` moved by at least half a cent. The diff runs **before** the
`catalog_shopify` upsert overwrites its basis (engineering rule 4). Shopify has
no price-history API, so this table starts the day you first run the script
and **can never be backfilled**. Never delete its rows.

### `catalog_tiktok` (PK `product_id, sku_id`)

`seller_sku`, `title`, `status`, `price` (tax-exclusive price, else sale
price), `currency`, `synced_at`. A product with no SKUs still gets one row with
`sku_id = ''`. `tiktok_affiliate_sync.py samples --by-product` reads this table
when it exists.

### `catalog_amazon` (PK `seller_sku`)

`asin`, `title`, `price`, `quantity`, `status`, `synced_at`, from
`GET_MERCHANT_LISTINGS_ALL_DATA` for `SPAPI_MARKETPLACE_ID` (one marketplace;
prices are in its currency). It's an upsert, so a listing deleted on Amazon
keeps its last row.

### `product_xref` (PK `channel, channel_id, channel_sku`)

One row per identifier sighting, rebuilt in full on every run:

| Column | Meaning |
|---|---|
| `channel` | `shopify` (non-archived variants), `ga4` (`ga_products`), `tiktok` (`catalog_tiktok`), `tiktok_order` (TikTok order SKUs, which reach delisted products), `amazon` (`catalog_amazon`) |
| `channel_id` | the channel's own id: Shopify variant id, GA4 item id, TikTok product id, ASIN. `''` for `tiktok_order` |
| `channel_sku` | the SKU exactly as the channel has it |
| `sku_key` | `norm_sku(channel_sku)`. **Exact identity: join on this** |
| `match_key` | `norm_style(title)`. Fuzzy and title-derived: a hint for SKU-less sightings, never a substitute for `sku_key`. Near-useless for Amazon, whose titles are SEO strings |

- A GA4 `item_id` keeps a SKU only when it really is a SKU in
  `catalog_shopify`. Otherwise (a product id, say) the sighting stays SKU-less
  rather than inventing a variant.
- Source tables are optional. A missing `ga_products` or `orders` just
  contributes nothing.
- The rebuild never shows readers a half-built table: rows go into a shadow
  table in short transactions, then two metadata-only renames swap it in
  (`rebuild_via_swap`). SQLite's write lock is database-wide, so one giant
  `DELETE` + `INSERT` would block every other writer for its whole duration. A
  view you define over `product_xref` survives the swap.

```sql
-- every channel's ids for one SKU
SELECT channel, channel_id, channel_sku, title
FROM product_xref WHERE sku_key = 'SKU-1001';
```

### Not included

These were left out because they only make sense with one company's data, or
because nothing in this repo produces their input:

- **Meta Commerce Catalog** (`catalog_meta`). It resolves product ids seen in a
  Meta ad-by-product breakdown, and this repo has no such table to resolve.
- **A unit-cost resolver across sources.** Without a second cost source,
  `catalog_shopify.unit_cost` is the whole answer.
- **Merchandising flags** (dropship, no-discount badge) and their change-log.
  These are tag conventions specific to one store. Derive them from `tags` in
  your own queries.

## MCP tool: `product_lookup`

`product_lookup(identifier)` takes any SKU (case and configured suffixes are
normalized), ASIN, TikTok product id, Shopify product or variant id, or GA4
item id. It returns JSON:

- `matched`: the `product_xref` rows the identifier hit directly
- `sku_keys`: the SKU(s) they resolve to. A Shopify product id expands to its
  variants' SKUs, and an archived product id resolves through its SKU to the
  live product
- `by_channel`: every channel's ids sharing those SKUs
- `shopify_variants`: the live Shopify variants for those SKUs (ids, status,
  price, compare-at, unit cost). Archived duplicates are excluded
- `same_style_fuzzy`: only present when some rows share just the title-derived
  `match_key` (another colour of the same style, usually). A hint, not an
  identity match
- On no exact match: `candidates`, a title / style substring search, capped at
  25. LIKE wildcards in the input are matched literally

Read-only and parameter-bound like the other tools. Sections are capped (50
rows, 200 for `by_channel`), with a note when a cap is hit. A channel missing
from the answer usually means the product isn't listed there, or that
channel's catalog sync isn't configured.

## Tests

- `tests/test_catalog_sync.py`: Shopify query shapes (`--days` filter,
  full pull, chunked and validated `--product-ids`, `IMAGE_GALLERY_CAP`,
  market/metafield selections and their config validation); `_join_gallery`
  never cuts a URL; per-variant shaping (image fallback, per-variant gallery
  exclusion, tags/NULL, metafields, defensive launch date, market rows with
  currency); >100-variant pagination; the price change-log (new / unchanged /
  changed, no blank baseline, named insert onto a migrated column order); the
  TikTok window walk (10k-cap halving, a 1h overflow reported, update vs create
  time); token refresh that rebuilds and re-signs the request through the
  shared connector, and no retry on other errors; the Amazon listings parser
  and encoding fallback; `rebuild_via_swap` (wholesale replace, canonical index
  names, a view surviving the swap, never-partial visibility); `main()` status
  and exit codes (skips, empty channel -> degraded/75, channel isolation on
  error, Amazon pause, targeted ids missing, flag validation)
- `tests/test_product_xref.py`: `norm_sku` / `norm_style` and their env
  configuration; `build_xref` channel sightings, archived duplicates excluded,
  GA4 exact-SKU vs product-id handling, MSKU suffix joining, missing source
  tables
- `tests/test_product_lookup.py`: ASIN / SKU / Shopify product id / archived
  product id / TikTok product id resolution, archived duplicates excluded,
  fuzzy style rows kept separate, substring fallback, literal LIKE wildcards,
  missing `product_xref`, tool registration
