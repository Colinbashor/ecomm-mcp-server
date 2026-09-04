# Amazon Brand Analytics

For brand-registered sellers: Search Query Performance, Search Catalog
Performance, Top Search Terms, Market Basket Analysis (frequently
co-purchased products), and Repeat Purchase Behavior.

**Scripts:** `amazon_sqp_sync.py`, `amazon_ba_sync.py`, `amazon_ba_backfill.py`
(standalone)

These reports queue for 15–25+ minutes on Amazon's side;
`warehouse/brand_analytics.py` is the shared create/poll/download runner both
scripts build on.

## Setup

Requires a **brand-registered** seller account. Reuses the same `SPAPI_*`
credentials as [Amazon Seller](amazon-seller.md) — no new variables needed.

Optional: create `brand_watchlist.yaml` in the project root to flag search
terms containing your own or a competitor's brand name (used by
`amazon_ba_sync.py`'s Top Search Terms report) — see that file for the format.
The same file has a separate, also-optional `term_topics` section for
**topic capture**: unlike every other Top Search Terms match rule, this one
keeps a term because of what it *is* (a regex match), not because it's
already tied to your own ASINs or brand names — the only way to surface
market demand for a product area you don't currently sell at all, along with
the competitor ASINs currently winning it.

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
  market-wide and can be huge; see the module docstring). `match_reason`
  records why each row was kept: `ours`/`brand`/`rank` from the base three
  rules, or `topic:<name>` from the optional topic-capture rule above.
- `amazon_ba_market_basket` — frequently co-purchased products
- `amazon_ba_repeat_purchase` — repeat purchase behavior

## Top search terms by category (monthly)

`amazon_search_terms_monthly.py` (standalone) answers a different question
than the Top Search Terms grain above: "what are the top N search terms each
month in the categories I sell in" — grouped by category, not filtered down
to terms that happen to touch your own catalog.

The SP-API Search Terms report carries **no category dimension** at all
(`departmentName` is always the literal string `"Amazon.com"`), so category
is derived from the Catalog Items browse-node classification of each term's
top-3 clicked ASINs, then rolled up into broad buckets you define — see
`search_term_categories.yaml` in the project root for the config format and
a placeholder example, and the module docstring for why broad buckets beat
narrow browse nodes.

```bash
python amazon_search_terms_monthly.py --probe                              # readiness check, no API calls
python amazon_search_terms_monthly.py --asins-file asins.txt --last-month
python amazon_search_terms_monthly.py --asins-file asins.txt --month 2026-07
python amazon_search_terms_monthly.py --asins-file asins.txt --backfill     # walk back to the retention floor
```

**Table:** `amazon_search_term_monthly` (month × category × search_term),
plus `amazon_asin_category` (a permanent ASIN → browse-node cache) and
`amazon_search_term_coverage` (what a given month's scan actually asked for
— read this before trusting an empty or short category; see the module
docstring's "coverage over presence" rule).

**Test:** `tests/test_amazon_search_terms_monthly.py`

## Deep backfill of Top Search Terms

`amazon_ba_backfill.py` (standalone) walks the Top Search Terms grain
backward week by week, resuming automatically on a re-run (a week already
stored is skipped). It's a separate script from `amazon_ba_sync.py --weeks N`
because this one grain is both the most expensive to re-request and, if
you're using topic capture, the only one worth deep-backfilling for
market-research purposes.

```bash
python amazon_ba_backfill.py --asins-file asins.txt              # walk back to the retention floor
python amazon_ba_backfill.py --asins-file asins.txt --weeks 12   # bounded run
python amazon_ba_backfill.py --asins-file asins.txt --start 2025-09-07
python amazon_ba_backfill.py --status                            # what's stored; no API calls
```

Amazon doesn't publish how far back this report actually answers for your
account, and it isn't guaranteed to signal "past retention" consistently —
some out-of-range weeks come back `FATAL` with the same generic message an
unpublished, too-recent week produces, rather than the cleaner `CANCELLED`.
This script stops after a run of consecutive weeks that all yield zero rows,
whatever the specific reason, rather than waiting for a signal that isn't
guaranteed to arrive. See the module docstring and
`warehouse/brand_analytics.py`'s docstring for the full explanation, and pace
any concurrent probing of multiple candidate weeks conservatively — the
create-report throttle has been observed to bite well before the documented
burst limit in practice.

## Building on top of `warehouse/brand_analytics.py`

The shared runner exposes two ways to consume a report's records:

- `fetch_ba_records(doc_id)` — downloads and `json.loads()`s the whole
  document. Fine for the reports above, which top out in the low thousands of
  rows.
- `stream_ba_records(doc_id)` — walks the gzip response stream and yields one
  record at a time, so memory stays flat no matter how large the document is.
  Some Brand Analytics reports (Top Search Terms in particular) are
  market-wide rather than scoped to your own catalog and can run to millions
  of records over a wide window — a plain `json.loads()` on one of those
  materializes the entire thing as Python dicts at once. If you're building a
  connector on a report you expect to be that large, use `stream_ba_records`
  (paired with `create_ba_report` + `await_ba_report` for the phased
  create/poll/stream split) instead of `run_ba_report`/`fetch_ba_records`.

## Notes

**Unit trap across these two tables**: `amazon_sqp` stores shares
(impression/click/cart-add/purchase share) as raw **percent** values, while
the four `amazon_ba_search_catalog`/`amazon_ba_search_terms`/
`amazon_ba_market_basket`/`amazon_ba_repeat_purchase` reports source
**fractions** from Amazon that `amazon_ba_sync.py` normalizes ×100 at ingest
to match. If you're pulling both tables into the same query or dashboard,
don't assume a shared scale without checking — an easy 100x mistake if you
copy a formula from one table to the other.

## Tests

`tests/test_amazon_sqp_sync.py`, `tests/test_amazon_ba_sync.py`,
`tests/test_amazon_ba_backfill.py`, `tests/test_brand_analytics.py`
