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
- `gmc_competitive_visibility` — competitive visibility

## Notes

See the module docstring for a lag-in-publishing gotcha on the visibility
grain (`gmc_competitive_visibility`) — it doesn't update same-day.

`gmc_best_sellers`/`gmc_best_seller_brands` are a **market ranking snapshot**,
not your own sales data, and Google only exposes the current snapshot — there
is no historical backfill for this grain regardless of `--backfill`.

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

## Tests

`tests/test_merchant_center_sync.py`
