# Amazon Seller (Selling Partner API)

Retail order ground truth (core), plus six standalone scripts covering FBA
inventory, returns, rank tracking, fees, SKU economics, and sales/traffic —
and a manual CSV importer for Voice of the Customer, which has no API.

This is a separate credential set (`SPAPI_*`) from [Amazon Advertising](amazon-ads.md).
For Brand Analytics (search query performance, market basket, etc.), see
[Amazon Brand Analytics](amazon-brand-analytics.md) — same credentials, but
requires brand registry.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/amazon_orders.py` | core, via `run_sync.py --only amazon_orders` | retail orders into `orders` |
| `amazon_inventory_sync.py` | standalone | FBA inventory snapshots |
| `amazon_returns_sync.py` | standalone | customer returns |
| `amazon_rank_sync.py` | standalone | Best-Seller-Rank tracking |
| `amazon_fees_sync.py` | standalone | SP-API fee reports: previews, storage, reimbursements, promotions, fulfilled shipments/MCF |
| `amazon_economics_sync.py` | standalone | Data Kiosk SKU economics — actual fees + net proceeds, vs. the fee-preview estimate |
| `amazon_traffic_sync.py` | standalone | per-ASIN Sales & Traffic: sessions, page views, Buy Box %, units/sales, weekly and monthly grain |
| `voc_import.py` | standalone, manual CSV | per-ASIN/SKU Voice of the Customer health |

## Setup

Seller **self-authorization** — no OAuth consent screen:

1. Seller Central → Apps & Services → Develop Apps → your private app →
   **Authorize app** shows the refresh token directly.
2. Fill in `.env`:

   | Variable | Notes |
   |---|---|
   | `SPAPI_CLIENT_ID` / `SPAPI_CLIENT_SECRET` | from the private app |
   | `SPAPI_REFRESH_TOKEN` | from the Authorize app step above |
   | `SPAPI_MARKETPLACE_ID` | required — only `amazon_economics_sync.py` falls back to `ATVPDKIKX0DER` (US) if unset; every other script raises a `KeyError` without it, so set it explicitly |
   | `SPAPI_REGION` | default `NA` |
   | `DATAKIOSK_TIMEOUT_MIN` | optional, how long `amazon_economics_sync.py` waits for a Data Kiosk query (default `150` — these can run 1–2h for a full week) |

All six extras reuse these same variables — nothing new to configure.
`voc_import.py` needs **no credentials at all**: download the export from
Seller Central (Performance → Voice of the Customer), drop it in a local
folder, and import it.

## Usage

```bash
python run_sync.py --only amazon_orders   # core retail orders, last 7 days
python amazon_inventory_sync.py
python amazon_returns_sync.py --start 2026-01-01 --end 2026-01-31   # or no args for the default window
python amazon_rank_sync.py --asins-file asins.txt   # or --asins B0FOO,B0BAR
python amazon_fees_sync.py                          # or --week YYYY-MM-DD
python amazon_fees_sync.py --only fee_preview,storage,reimbursements,promotions,shipments
python amazon_economics_sync.py                     # or --week YYYY-MM-DD / --weeks N to backfill
python amazon_traffic_sync.py                       # or --week / --weeks N / --month YYYY-MM
python voc_import.py path/to/export.csv --dry-run   # preview before writing
python voc_import.py path/to/export.csv
```

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `amazon_inventory`
- `amazon_returns`
- `amazon_sales_rank`
- `amazon_fee_preview`, `amazon_fba_storage_fees`, `amazon_fba_reimbursements`,
  `amazon_fba_promotions`, `amazon_fulfilled_shipments`
- `amazon_economics`
- `amazon_traffic_weekly`, `amazon_traffic_monthly`, `amazon_traffic_daily`,
  `amazon_traffic_monthly_account`
- `amazon_voc`

## Notes

`voc_import.py` uses header-driven column matching (spellings drift between
Seller Central export versions) — a useful template if you need to import
any other Seller-Central-only report that has no API.

`amazon_rank_sync.py` needs to know which ASINs to track — pass `--asins`
(comma-separated) or `--asins-file` (one per line). Omit both and it falls
back to scanning `amazon_fulfilled_shipments` (written by
`amazon_fees_sync.py`'s shipments report) as a proxy "recently sold" list —
a weak fallback, not the intended input, and it produces nothing on a first
run before fees data exists. Pass an explicit ASIN list for anything beyond
a smoke test.

`amazon_inventory`: **don't sum rows naively by SKU.** Amazon aliases FBA
inventory under multiple `fn_sku` values for the same seller SKU (bundle
components, marketplace-specific aliasing), so a plain `SUM(quantity) GROUP
BY seller_sku` can double-count. Check the module docstring in
`amazon_inventory_sync.py` before writing aggregate queries against this
table.

`amazon_economics_sync.py` pulls Data Kiosk's weekly grain, which is aligned
Sun–Sat. A query with a Mon–Sun (or any other) week boundary will silently
return zero rows rather than erroring — if `amazon_economics` looks empty for
a week you expect data for, check the boundary first.

`amazon_fulfilled_shipments` (written by `amazon_fees_sync.py`) carries
`sales_channel` and `shopify_order_name` columns, letting you join Amazon's
Multi-Channel Fulfillment (MCF) shipments back to the Shopify order they
fulfilled — useful for tracing an order that shipped from FBA inventory but
sold on your own site.

## Tests

`tests/test_amazon_inventory_sync.py`, `tests/test_amazon_returns_sync.py`,
`tests/test_amazon_rank_sync.py`, `tests/test_amazon_fees_sync.py`,
`tests/test_amazon_economics_sync.py`, `tests/test_amazon_traffic_sync.py`,
`tests/test_voc_import.py`
