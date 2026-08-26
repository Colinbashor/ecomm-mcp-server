# Algolia (on-site search/browse)

On-site collection/category-grid **placement** (which slot each product
occupies, per collection, per day) plus **engagement** analytics
(impressions, click-through, add-to-cart rate, top queries) from
[Algolia](https://www.algolia.com) — only relevant if your storefront's
collection pages are rendered **client-side** against an Algolia index (a
common pattern for Shopify themes using `react-instantsearch` or similar,
but not universal). See the "WHEN THIS APPLIES" section at the top of the
module docstring for how to check whether this applies to your storefront
before setting it up.

**Script:** `algolia_sync.py` (standalone — not wired into `run_sync.py`)

## Why this exists

Nothing else in this repo records **where** a product sits on a
collection/category page. That's a real gap if your merchandising team can
hand-pin products into specific grid slots via rules that get edited
same-day — the placement a product enjoyed on a given day becomes
unrecoverable the moment those rules change, unless it's snapshotted daily.
When your storefront is Algolia-rendered, querying the same index with the
same filter your storefront uses returns the exact order a shopper saw —
not an estimate.

Algolia's Analytics API separately answers "how did shoppers engage with
what they saw" — impressions, clicks, and (see the module docstring's trap
on this) usually add-to-cart rate rather than purchase rate, since most
storefronts don't wire purchase events through to Algolia.

## Setup

1. Find your Algolia app id and a **search-only** API key — these are
   commonly public by design (already shipped to every visitor's browser),
   visible in your storefront's own page source or Network tab requests to
   `*.algolia.net`.
2. Find the index that actually serves your storefront's default grid — a
   real Algolia application often has many indices (locale/sort-order
   variants, abandoned prior integrations) and usually only one has real
   traffic. `algolia_sync.py --probe` helps confirm which.
3. (Optional, for the analytics grains) Get a privileged **Analytics** API
   key from your Algolia dashboard (API Keys section) — different from the
   search key above, and must stay out of any client-facing code.
4. Fill in `.env`:

   | Variable | Notes |
   |---|---|
   | `ALGOLIA_APP_ID` | your Algolia application id |
   | `ALGOLIA_SEARCH_KEY` | a search-only API key |
   | `ALGOLIA_INDEX` | the index behind your storefront's default grid |
   | `ALGOLIA_ANALYTICS_KEY` | optional — omit to skip analytics grains, placement still works |

5. (Optional) Edit `algolia_collections.yaml` to list the collection/category
   handles you want tracked for placement. Ships with placeholder examples —
   replace them with your own. If left unedited or missing, the placement
   grain has nothing to track and skips cleanly.

## Usage

```bash
python algolia_sync.py --probe             # check reachability + traffic mix, no writes
python algolia_sync.py                     # daily: placement + last 3 days of analytics
python algolia_sync.py --days 30
python algolia_sync.py --start 2026-08-01 --end 2026-08-24
python algolia_sync.py --only placement
python algolia_sync.py --only hits --only positions
python algolia_sync.py --collections dresses,new-arrivals   # override the config file for one run
```

`--only` accepts `placement`, `hits`, `positions`, `searches`, `daily` —
repeatable to select more than one grain. Run `--probe` first on a new
account: it reports whether the Search/Analytics APIs are reachable, whether
purchase events are wired, and the product count for each configured
collection, without writing anything.

## Tables

- `collection_placement` — daily snapshot: which position each product holds
  in each tracked collection's default-sort grid. Cannot be backfilled —
  Algolia exposes no ranking history.
- `algolia_product_engagement` — per-object (often per-variant, not
  per-product — see the docstring) daily impressions/clicks/add-to-cart.
  Pooled across every collection and query the object appeared in; do not
  attribute it to one collection.
- `algolia_click_positions` — empirical click-decay curve by grid slot. A
  relative shape, not an absolute click total.
- `algolia_searches` — top queries per day (capped by volume; the empty
  query is usually the browse grid itself, not a real search).
- `algolia_daily` — account-level daily series (search/click/no-result/
  no-click rates).

## Notes

The module docstring documents seven specific traps worth reading before you
build anything on top of this data — among them: Algolia's "conversion"
metric is frequently add-to-cart, not purchase; engagement can't be
attributed to a single collection when a product appears on several;
analytics retention is short (measured in weeks, not years) and hard-capped
server-side, with **no historical backfill possible**; and a search-hit's own
`objectID` may be a variant id that the search index itself cannot reliably
resolve back to a product (resolve it via your own catalog data instead).

Both placement and the analytics grains are **accrue-forward only** — a
sync that stops running for an extended stretch loses that window
permanently, which is the whole argument for running this on a real daily
schedule rather than treating it as backfillable later.

## Tests

`tests/test_algolia_sync.py`
