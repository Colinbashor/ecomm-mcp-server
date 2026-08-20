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
   | `SPAPI_MARKETPLACE_ID` | default `ATVPDKIKX0DER` (US) |
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
python amazon_returns_sync.py
python amazon_rank_sync.py --asins-file asins.txt   # or --asins B0FOO,B0BAR
python amazon_fees_sync.py
python amazon_economics_sync.py
python amazon_traffic_sync.py
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

## Tests

`tests/test_amazon_inventory_sync.py`, `tests/test_amazon_returns_sync.py`,
`tests/test_amazon_rank_sync.py`, `tests/test_amazon_fees_sync.py`,
`tests/test_amazon_economics_sync.py`, `tests/test_amazon_traffic_sync.py`,
`tests/test_voc_import.py`
