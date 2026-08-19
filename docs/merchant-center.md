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
3. Fill in `.env`:

   | Variable | Notes |
   |---|---|
   | `GMC_MERCHANT_ID` | your Merchant Center account id |
   | `GMC_CREDENTIALS_FILE` | path to the service-account JSON key |

## Usage

```bash
python merchant_center_sync.py
```

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

## Tests

`tests/test_merchant_center_sync.py`
