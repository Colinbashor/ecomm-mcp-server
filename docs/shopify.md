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
python shopify_customers_sync.py       # customer dimension
```

## Tables

- `orders`, `shopify_order_customers`, `shopify_order_discounts` (core, shared
  across platforms — see the main [README](../README.md#mcp-tools))
- `shopify_customers`, `shopify_customer_metafields`,
  `shopify_customer_flag_history` (customer dimension)

## Notes

`shopify_customers_sync.py` deliberately never stores email, name, phone, or
address — see the module docstring for why.

## Tests

`tests/test_shopify_connector.py`, `tests/test_shopify_customers_sync.py`
