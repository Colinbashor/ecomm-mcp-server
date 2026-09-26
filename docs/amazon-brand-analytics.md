# Amazon Brand Analytics

For brand-registered sellers: Search Query Performance, Search Catalog
Performance, Top Search Terms, Market Basket Analysis (frequently
co-purchased products), and Repeat Purchase Behavior.

**Scripts:** `amazon_sqp_sync.py`, `amazon_ba_sync.py`, `amazon_ba_backfill.py`
(standalone)

These reports queue for 15–25+ minutes on Amazon's side;
`warehouse/brand_analytics.py` is the shared create/poll/download runner all
three scripts build on (the backfill indirectly, by calling
`amazon_ba_sync.py`'s Top Search Terms grain).

## Setup

Requires a **brand-registered** seller account. Reuses the same `SPAPI_*`
credentials as [Amazon Seller](amazon-seller.md) — no new variables needed.

Optional: create `brand_watchlist.yaml` in the project root to flag search
terms containing your own or a competitor's brand name (used by
`amazon_ba_sync.py`'s Top Search Terms report) — see that file for the format.
The same file has a separate, also-optional `term_topics` section for
**topic capture**: unlike the `ours`/`brand` match rules, this one keeps a
term because of what it *is* (a regex match), not because it involves your
own ASINs or brand names. The `rank` rule also keeps terms that have nothing
to do with you, but only the market-wide head (rank ≤ `rank_flag_max`,
default 2500). Topic capture is the only Top Search Terms rule that can
surface a *niche* product area you don't currently sell at all, along with
the competitor ASINs currently winning it. Both `rank_flag_max` and
`topic_max_rows_per_week` (default 20,000, counted per unique term rather
than per row, so a term's full top-3 rows stay together) are set in the
same YAML file.

If PyYAML isn't installed, `brand_watchlist.yaml` is **silently ignored**:
no brand or topic matching, and the rank cutoff falls back to the default.

## Usage

All three scripts take your ASINs via `--asins` (comma-separated) or
`--asins-file` (one ASIN per line), but they use them differently:

- `amazon_sqp_sync.py` uses the list as **the set of ASINs to request**, in
  the order given, so put your most important ASINs first. It exits with an
  error if the list comes out empty.
- `amazon_ba_sync.py` and `amazon_ba_backfill.py` use it to **flag** a
  term or pair as involving your own catalog (`match_reason = 'ours'`).

If you omit both flags, they fall back to
`amazon_rank_sync.fallback_asins()`. That returns distinct **seller SKUs**
from `amazon_fulfilled_shipments`, not ASINs, so SQP requests will mostly
miss and `ours` matching will effectively find nothing. Treat it as a
smoke-test convenience only and pass real ASINs for any actual run.

```bash
python amazon_sqp_sync.py --asins-file asins.txt   # Search Query Performance
python amazon_ba_sync.py --asins-file asins.txt    # Search Catalog Performance, Top Search Terms,
                                                    # Market Basket Analysis
python amazon_ba_sync.py --month 2026-06           # Repeat Purchase Behavior (no ASINs needed)
```

Flags on both `amazon_sqp_sync.py` and `amazon_ba_sync.py`:

| Flag | Meaning |
|---|---|
| `--week YYYY-MM-DD` | The **Sunday** that starts the BA week (default: last completed Sun–Sat). Any other weekday is rejected. |
| `--weeks N` | Walk back N BA weeks from `--week` (default 1). |
| `--fallback-weeks N` | Step back up to N earlier weeks when the requested week yields nothing (default 0). See the caveat below. |

`--fallback-weeks` behaves differently in the two scripts:

- **`amazon_sqp_sync.py`** steps back when the week is detected as
  unpublished, or when it ran cleanly but returned zero rows. This is the
  real guard against the Monday availability lag on a weekly cron.
- **`amazon_ba_sync.py`** steps back **only when a report finishes with zero
  rows**. A too-recent week usually comes back `FATAL` instead, which is
  logged as an error for that grain and *not* retried on an earlier week. For
  a Monday cron, schedule the run late enough that the prior week is
  published, or re-run it later.

`amazon_ba_sync.py` only:

| Flag | Meaning |
|---|---|
| `--only search_catalog,search_terms,market_basket` | Run a subset of the weekly grains (default: all three). The module docstring recommends running `search_terms` **alone**, because it is market-wide and large. |
| `--month YYYY-MM` / `--last-month` | Run **only** Repeat Purchase for that month and exit. Weekly grains are skipped in this mode, and `--month` wins if both are given. |

`amazon_sqp_sync.py` only:

| Flag | Meaning |
|---|---|
| `--max-asins N` | Cap on ASINs requested per week. **Default 120**; `0` = all. |
| `--refresh` | Re-request ASINs already recorded in `amazon_sqp_coverage`. |
| `--max-minutes N` | Wall-clock budget so a scheduled run can't block indefinitely (0 = unlimited). |

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
python amazon_search_terms_monthly.py --asins-file asins.txt --month 2026-07 --months 3  # July + 3 earlier months
python amazon_search_terms_monthly.py --asins-file asins.txt --month 2026-07 --doc-id <reportDocumentId>  # re-bucket an existing report
```

Other flags: `--max-months N` (safety stop for `--backfill`, default 36) and
`--refresh` (re-run months already marked complete). `--doc-id` re-buckets an
already-generated report document for a single month instead of requesting a
new one.

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
because this one grain is both the most expensive of `amazon_ba_sync.py`'s
four reports to re-request and, if
you're using topic capture, the only one worth deep-backfilling for
market-research purposes.

```bash
python amazon_ba_backfill.py --asins-file asins.txt              # walk back until 3 empty weeks in a row, or 60 weeks
python amazon_ba_backfill.py --asins-file asins.txt --weeks 12   # bounded run
python amazon_ba_backfill.py --asins-file asins.txt --start 2025-09-07  # must be a Sunday
python amazon_ba_backfill.py --asins-file asins.txt --refresh    # re-pull weeks already stored
python amazon_ba_backfill.py --status                            # what's stored; no API calls
```

Amazon doesn't publish how far back this report actually answers for your
account, and it isn't guaranteed to signal "past retention" consistently —
some out-of-range weeks come back `FATAL` with the same generic message an
unpublished, too-recent week produces, rather than the cleaner `CANCELLED`.
This script stops after **3 consecutive weeks** that all yield zero rows
(`MAX_CONSECUTIVE_MISSES`), whatever the specific reason, or after `--weeks`
weeks (default 60), whichever comes first. It stops on empty weeks rather than waiting for a signal that isn't
guaranteed to arrive. See the module docstring and
`warehouse/brand_analytics.py`'s docstring for the full explanation, and pace
any concurrent probing of multiple candidate weeks conservatively — the
create-report throttle has been observed to bite well before the documented
burst limit in practice.

## Building on top of `warehouse/brand_analytics.py`

The shared runner exposes two ways to consume a report's records:

- `fetch_ba_records(doc_id)` — downloads and `json.loads()`s the whole
  document. Fine for catalog-scoped reports (Search Catalog, Market Basket,
  Repeat Purchase, SQP), which top out in the low thousands of rows. Note
  that `amazon_ba_sync.py`'s Top Search Terms grain, and therefore
  `amazon_ba_backfill.py`, currently goes through `run_ba_report`, which uses
  this function. It loads the full market-wide document into memory before
  filtering, so expect a large memory spike on that grain.
  `amazon_search_terms_monthly.py` is the in-repo example of the streaming
  path below.
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
