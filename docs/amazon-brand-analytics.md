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

```bash
python amazon_sqp_sync.py     # Search Query Performance
python amazon_ba_sync.py      # Search Catalog Performance, Top Search Terms,
                               # Market Basket Analysis, Repeat Purchase Behavior
```

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
