# Amazon Brand Analytics

For brand-registered sellers: Search Query Performance, Search Catalog
Performance, Top Search Terms, Market Basket Analysis (frequently
co-purchased products), and Repeat Purchase Behavior.

**Scripts:** `amazon_sqp_sync.py`, `amazon_ba_sync.py` (standalone)

These reports queue for 15–25+ minutes on Amazon's side;
`warehouse/brand_analytics.py` is the shared create/poll/download runner both
scripts build on.

## Setup

Requires a **brand-registered** seller account. Reuses the same `SPAPI_*`
credentials as [Amazon Seller](amazon-seller.md) — no new variables needed.

Optional: create `brand_watchlist.yaml` in the project root to flag search
terms containing your own or a competitor's brand name (used by
`amazon_ba_sync.py`'s Top Search Terms report) — see that file for the format.

## Usage

Both scripts need to know which ASINs are yours (to flag a query/term/pair as
involving your own catalog). Pass them with `--asins` (comma-separated) or
`--asins-file` (one ASIN per line); if you omit both, they fall back to
`amazon_rank_sync.fallback_asins()` — a weak proxy, not a real substitute, so
pass your ASINs explicitly for anything beyond a first smoke test.

```bash
python amazon_sqp_sync.py --asins-file asins.txt   # Search Query Performance
python amazon_ba_sync.py --asins-file asins.txt    # Search Catalog Performance, Top Search Terms,
                                                    # Market Basket Analysis
python amazon_ba_sync.py --month 2026-06           # Repeat Purchase Behavior (no ASINs needed)
```

Useful flags on both: `--week YYYY-MM-DD` (a specific BA week, default: last
completed Sun–Sat), `--weeks N` (backfill N weeks), `--fallback-weeks N` (if a
week comes back empty, step back further — guards against the Monday
availability lag on a weekly cron). `amazon_ba_sync.py` also takes `--only
search_catalog,search_terms,market_basket` to run a subset of grains, and
`--last-month`/`--month YYYY-MM` for Repeat Purchase. `amazon_sqp_sync.py`
also takes `--max-asins N` (cap per week; 0 = all), `--refresh` (re-request
ASINs already recorded in `amazon_sqp_coverage`), and `--max-minutes N` (wall-
clock budget so a scheduled run can't be blocked indefinitely).

## Tables

- `amazon_sqp`, `amazon_sqp_coverage` — query-level volume + your share of
  impressions/clicks/cart-adds/purchases vs. the whole market
- `amazon_ba_search_catalog` — Search Catalog Performance
- `amazon_ba_search_terms` — Top Search Terms (filterable — the raw report is
  market-wide and can be huge; see the module docstring)
- `amazon_ba_market_basket` — frequently co-purchased products
- `amazon_ba_repeat_purchase` — repeat purchase behavior

## Tests

`tests/test_amazon_sqp_sync.py`, `tests/test_amazon_ba_sync.py`,
`tests/test_brand_analytics.py`
