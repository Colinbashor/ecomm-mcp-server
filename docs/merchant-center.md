# Google Merchant Center

Product feed performance (organic vs. paid, plus account-wide
non-product-specific performance), feed eligibility/issues, price
competitiveness, category best-sellers, and competitive visibility.

**Script:** `merchant_center_sync.py` (standalone — not wired into `run_sync.py`)

## Setup

1. Use the same kind of service-account credential as [GA4](ga4.md) — you can
   reuse the same JSON key if you enable the Content API scope for it.
2. You must **also** run a one-time `registerGcp` API call before anything
   works — there is no Merchant Center UI for this step; see the module
   docstring in `merchant_center_sync.py` for the exact call.
3. Grant the service account access to the merchant account: Merchant Center
   → Settings → Account access → add the service account's email as a
   Standard user (Admin isn't needed for day-to-day reporting).
4. Fill in `.env`:

   | Variable | Notes |
   |---|---|
   | `GMC_MERCHANT_ID` | your Merchant Center account id |
   | `GMC_CREDENTIALS_FILE` | path to the service-account JSON key |

## Usage

```bash
python merchant_center_sync.py                        # last 3 days (default), all grains
python merchant_center_sync.py --days 30
python merchant_center_sync.py --start 2026-01-01 --end 2026-01-31
python merchant_center_sync.py --only performance --only pricing
python merchant_center_sync.py --backfill              # walk performance history back until it runs dry
python merchant_center_sync.py --only bestsellers --category 1604 --country US --top-n 50
python merchant_center_sync.py --only bestsellers --brand "Your Brand" --brand "Competitor"
python merchant_center_sync.py --only bestsellers --report-date 2026-08-24   # manual backfill of one date
```

`--only` accepts `performance`, `status`, `pricing`, `bestsellers`,
`visibility` — pass it once per family to select more than one. `--category`
and `--country` are also repeatable and scope both the best-sellers and
visibility grains; `--brand` is repeatable and scopes best-sellers only.

## Tables

- `gmc_product_performance`, `gmc_account_performance` — organic vs. paid
  performance, product-specific and account-wide
- `gmc_product_status`, `gmc_product_issues` — feed eligibility and issues
- `gmc_price_competitiveness` — price vs. market
- `gmc_best_sellers`, `gmc_best_seller_brands` — category best-sellers,
  including a "riser" signal for products gaining demand outside the usual
  top-N cut, and `--brand` for tracking specific brands (yours or a
  competitor's) regardless of rank
- `gmc_best_seller_coverage` — records which best-sellers `report_date`s have
  actually been asked for and whether Google had published them yet; backs
  the automatic heal pass below, not meant for direct querying
- `gmc_competitive_visibility` — competitive visibility

## Notes

See the module docstring for a lag-in-publishing gotcha on the visibility
grain (`gmc_competitive_visibility`) — it doesn't update same-day.

`gmc_best_sellers`/`gmc_best_seller_brands` are a **market ranking**, not your
own sales data, and a plain (unfiltered) query only ever returns whichever
report is *current* — so a sync that misses a run can permanently lose
whichever `report_date` was current on exactly that day once a newer one
replaces it. Every normal `bestsellers` sync therefore also runs
`heal_best_sellers()`: it works out which recent WEEKLY (Monday) and MONTHLY
(1st-of-month) `report_date`s this database should already hold, checks
`gmc_best_seller_coverage`/`gmc_best_sellers` for gaps, and re-pulls only the
missing ones with an exact `report_date = '...'` filter — at zero extra API
cost when there's no gap to heal. A date Google hasn't published yet is
recorded as such (not left unrecorded, and not re-asked on every single run
forever — it ages out of the heal window eventually). For a manual, targeted
backfill of one known date, use `--report-date` directly. See "HEALING A
MISSED report_date" in the module docstring for the one hard rule this all
depends on: never batch more than one `report_date` into a single query — the
API silently accepts `report_date IN (...)` but applies the top-N `LIMIT`
across the *combined* result set, quietly halving each date's row count with
no error.

`gmc_product_performance`/`gmc_account_performance` deliberately carry
clicks/impressions/conversions only, no revenue column — see the module
docstring's `conversion_value` trap: selecting a money-valued conversion
field from this API implicitly segments results by currency, silently
splitting one logical (date, product) row into several with the
non-selected currency's numbers reading as zero. Get conversion *value* from
whichever ads-platform connector already reports attributed revenue instead.

A long `--backfill` run can pause partway (rate-limited or interrupted) and
resumes where it left off on re-run rather than restarting — it exits with
code `75` to signal "paused, not failed," which matters if you're wiring
this into a scheduler that treats a nonzero exit as an alert.

The sync distinguishes throttling (retry the same request) from a permanent
per-item error (skip and move on) rather than retrying everything uniformly
— see the module docstring if you're debugging a partial/`"degraded"` run.

**Every grain logs `"degraded"` (never `"ok"`) on a pull that returned zero
rows.** A grain that logs `"ok"` on any request that didn't raise — ignoring
the row count entirely — reads identically in `sync_log` whether nothing
changed today or the feed has silently stopped advancing; a run of "ok, 0
rows" looks exactly like "ok, quiet day" until someone happens to check the
table directly. This does *not* change the process exit code (an empty pull
isn't a resumable pause), it just makes the log line honest so a downstream
health check can tell the two cases apart — see `_log_grain()`.

## Tests

`tests/test_merchant_center_sync.py`
