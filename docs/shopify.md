# Shopify

Order ground truth (core), plus a standalone connector for the customer
dimension: account state, tags, marketing-consent, and metafields.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/shopify.py` | core, via `run_sync.py --only shopify` | orders into `orders` |
| `shopify_customers_sync.py` | standalone | current customer state + change-log of tag/state transitions over time |

## Setup

1. Create a Dev Dashboard app (dev.shopify.com) with Admin API scopes
   `read_orders` **and** `read_all_orders` (without the latter, only the last
   60 days are visible). Install it on the store.
2. Fill in `.env`:

   | Variable | Notes |
   |---|---|
   | `SHOPIFY_SHOP` | the original `*.myshopify.com` slug, not the custom domain |
   | `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | from the Dev Dashboard app; used for the non-interactive client-credentials grant the connector performs itself |
   | `SHOPIFY_ADMIN_TOKEN` | only for legacy pre-2026 static `shpat_` tokens |
   | `SHOPIFY_CAPTURE_CUSTOMER` | optional tri-state: `1` forces storing the Shopify customer id on orders, `0` forces it off, unset auto-probes whether the app's token has the scope |

For `shopify_customers_sync.py`, additionally add the **`read_customers`**
Admin API scope to the same app (a Shopify-side config change, not an env
var), release a new version, and reinstall the app.

## Usage

```bash
python run_sync.py --only shopify     # core orders, last 7 days
python shopify_customers_sync.py --probe     # cheap scope/permission check — run this first
python shopify_customers_sync.py --dry-run   # preview without writing
python shopify_customers_sync.py --since 2026-01-01
python shopify_customers_sync.py             # full crawl, all customers
```

Start with `--probe` on a new store: it confirms the `read_customers` scope
and API access work before you kick off a full crawl.

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `shopify_order_customers`, `shopify_order_discounts` (order-side extras,
  written as a side effect of `warehouse/connectors/shopify.py`'s `sync()`)
- `shopify_customers`, `shopify_customer_metafields`,
  `shopify_customer_flag_history` (customer dimension)

## Notes

**`orders.total` is not net of discounts.** Shopify's `discountedTotalSet`
excludes several discount-allocation methods (`code/EACH/ENTITLED`,
`code/ACROSS/ALL`, `manual/ACROSS/ALL`), so summing `orders.total` alone
overstates realized revenue whenever any of those discount types are in
play. To get a true net figure, subtract `shopify_order_discounts.amount`:
`total - SUM(shopify_order_discounts.amount)`. Promo-code discounts and
price markdowns are two distinct mechanisms with different visibility here
— see `warehouse/connectors/shopify.py`'s module docstring for the full
breakdown before building a revenue report on this table.

`shopify_customers_sync.py` deliberately never stores email, name, phone, or
address — see the module docstring for why.

`shopify_customers_sync.py` uses Shopify's Bulk Operations API, which only
allows **one bulk query per app at a time** account-wide — a concurrent
sync elsewhere on the same app gets auto-drained and resubmitted rather than
failing outright. The bulk query itself has no pagination (results stream
back as JSONL, regrouped by `__parentId`, and batches only cut at a root
line boundary). Before overwriting a customer's tag/state history, the
script diffs against `shopify_customer_flag_history` rather than blindly
appending — see the module docstring if you're extending this script.

## Tests

`tests/test_shopify_connector.py`, `tests/test_shopify_customers_sync.py`
