# Amazon Seller (Selling Partner API)

Retail order ground truth (core), plus seven standalone scripts covering FBA
inventory, AWD (bulk-storage) inventory, returns, rank tracking, fees, SKU
economics, and sales/traffic — and a manual CSV importer for Voice of the
Customer, which has no API.

This is a separate credential set (`SPAPI_*`) from [Amazon Advertising](amazon-ads.md).
For Brand Analytics (search query performance, market basket, etc.), see
[Amazon Brand Analytics](amazon-brand-analytics.md) — same credentials, but
requires brand registry.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/amazon_orders.py` | core, via `run_sync.py --only amazon_orders` | retail orders into `orders` |
| `amazon_inventory_sync.py` | standalone | FBA inventory snapshots |
| `amazon_awd_sync.py` | standalone | Amazon Warehousing & Distribution (AWD) bulk-storage inventory snapshots |
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

All seven extras reuse these same variables — nothing new to configure.
`voc_import.py` needs **no credentials at all**: download the export from
Seller Central (Performance → Voice of the Customer), drop it in a local
folder, and import it.

## Usage

```bash
python run_sync.py --only amazon_orders   # core retail orders, last 7 days
python amazon_inventory_sync.py
python amazon_awd_sync.py                           # or --probe / --date YYYY-MM-DD
python amazon_returns_sync.py --start 2026-01-01 --end 2026-01-31   # or no args for the default window
python amazon_rank_sync.py --asins-file asins.txt   # or --asins B0FOO,B0BAR
python amazon_fees_sync.py                          # or --week YYYY-MM-DD
python amazon_fees_sync.py --only fee_preview,storage,reimbursements,promotions,shipments
python amazon_economics_sync.py                     # or --week YYYY-MM-DD / --weeks N to backfill
python amazon_traffic_sync.py                       # or --week / --weeks N / --month YYYY-MM
python amazon_traffic_sync.py --repair               # re-pull only weeks recorded incomplete
python amazon_traffic_sync.py --allow-partial        # exit 0 on a short pull (early-pass schedule)
python voc_import.py path/to/export.csv --dry-run   # preview before writing
python voc_import.py path/to/export.csv
python voc_import.py --dir imports/voc               # import every *.csv in a folder
python voc_import.py --date 2025-07-20 imports/voc/export.csv   # force snapshot date
```

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `amazon_inventory`
- `amazon_awd_inventory`
- `amazon_returns`
- `amazon_sales_rank`
- `amazon_fee_preview`, `amazon_fba_storage_fees`, `amazon_fba_reimbursements`,
  `amazon_fba_promotions`, `amazon_fulfilled_shipments`
- `amazon_economics`
- `amazon_traffic_weekly`, `amazon_traffic_monthly`, `amazon_traffic_daily`,
  `amazon_traffic_monthly_account`, `amazon_traffic_coverage`
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

`amazon_awd_inventory` (Amazon Warehousing & Distribution — a bulk-storage
tier upstream of FBA, only relevant if you use it) is invisible to the FBA
Inventory API entirely, so it's a genuinely separate stock pool, not a
variant of `amazon_inventory`. It needs no new credentials — same `SPAPI_*`
app. **The one thing to get right:** of the four quantities the API returns
per SKU, only `available_distributable` is stock the FBA feed doesn't
already know about; `reserved_distributable` and `replenishment_qty` are
already committed to FBA and typically reappear as one of
`amazon_inventory`'s `inbound_*` columns for the same SKU, so summing them
into an FBA position double-counts real units. Read `amazon_awd.py` (a
shared, read-only reader with exactly one accessor, `available()`, for "how
much extra stock is there") rather than querying `amazon_awd_inventory`
directly — the full reasoning is in both modules' docstrings. Like
`amazon_inventory`, this is current-state-only (no history API), so a
missed day's snapshot is gone permanently; `amazon_awd.note(...)` renders a
one-line status footer distinguishing "no AWD stock" from "never synced" —
always show it alongside any AWD-derived number.

`amazon_economics_sync.py` pulls Data Kiosk's weekly grain, which is aligned
Sun–Sat. A query with a Mon–Sun (or any other) week boundary will silently
return zero rows rather than erroring — if `amazon_economics` looks empty for
a week you expect data for, check the boundary first.

`amazon_traffic_sync.py` **validates that a "DONE" report is actually
complete** before trusting it. A report requested close to the end of a
period can come back HTTP 200/DONE with fewer days than requested, because
Amazon hasn't finished publishing the most recent day(s) yet — a fixed
schedule run right after a period ends is especially exposed to silently
storing a short period as if it were whole. `coverage()` checks two things:
every expected calendar day is present in `salesAndTrafficByDate`, and the
`salesAndTrafficByDate`/`salesAndTrafficByAsin` sections' summed
`orderedProductSales` agree within 2% (`sync_week`/`sync_month` write from
byAsin, so a day-count-only check can miss a byAsin-short period). A short
pull never overwrites a period already stored complete, is recorded in the
new `amazon_traffic_coverage` table, and logs `degraded` rather than `ok` so
monitoring keyed on sync-log status doesn't read it as healthy. Run with
`--repair` on a later pass (after Amazon has had time to catch up) to
re-pull only the recorded-incomplete periods, bounded to 6 attempts per
period so a day Amazon will never actually finish publishing doesn't get
re-requested forever. `--allow-partial` is for the opposite case — an early
pass expected to be short — and exits 0 instead of failing the run.

`amazon_fulfilled_shipments` (written by `amazon_fees_sync.py`) carries
`sales_channel` and `shopify_order_name` columns, letting you join Amazon's
Multi-Channel Fulfillment (MCF) shipments back to the Shopify order they
fulfilled — useful for tracing an order that shipped from FBA inventory but
sold on your own site.

## Tests

`tests/test_amazon_inventory_sync.py`, `tests/test_amazon_awd_sync.py`,
`tests/test_amazon_awd.py`, `tests/test_amazon_returns_sync.py`,
`tests/test_amazon_rank_sync.py`, `tests/test_amazon_fees_sync.py`,
`tests/test_amazon_economics_sync.py`, `tests/test_amazon_traffic_sync.py`,
`tests/test_voc_import.py`
