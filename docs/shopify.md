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
   | `SHOPIFY_ADMIN_TOKEN` | only for legacy pre-2026 static `shpat_` tokens. If set, it takes precedence over the client-credentials grant |
   | `SHOPIFY_CAPTURE_CUSTOMER` | optional tri-state override of the `read_customers` scope check (see below) |

   `run_sync.py` treats Shopify as configured when either
   `SHOPIFY_CLIENT_SECRET` or `SHOPIFY_ADMIN_TOKEN` is set, and prints
   `SKIPPED` otherwise.

**`SHOPIFY_CAPTURE_CUSTOMER` in detail.** Both scripts ask one function,
`customer_capture_enabled()`, whether the app holds `read_customers`:

| Value | Effect |
|---|---|
| unset / empty | Auto-probe `access_scopes.json`. The result is cached for 6 hours, and any error counts as "not granted", so the probe can never break the order sync. |
| `0`, `false`, `no`, `off` | Forced off. The core sync skips `shopify_order_customers`, and **`shopify_customers_sync.py` refuses to run** ("read_customers is not granted"). |
| any other non-empty value (e.g. `1`) | Forced on, with no probe. The core sync requests `customer { id }`, and `shopify_customers_sync.py` skips its own scope check. |

Because of the auto-probe, customer-id capture on orders switches itself on
the first time the app is released and reinstalled with `read_customers`. No
config change is needed.

For `shopify_customers_sync.py`, additionally add the **`read_customers`**
Admin API scope to the same app (a Shopify-side config change, not an env
var), release a new version, and reinstall the app.

## Usage

```bash
python run_sync.py --only shopify     # core orders: today and the 7 days before it (8 calendar days, inclusive)
python run_sync.py --only shopify --start 2026-01-01 --end 2026-01-31   # explicit window / backfill
python shopify_customers_sync.py --probe     # cheap scope/permission check — run this first
python shopify_customers_sync.py --dry-run   # full crawl + parse, no customer data written
python shopify_customers_sync.py --since 2026-01-01
python shopify_customers_sync.py --days 3    # same idea, as a rolling window — pair with a scheduler
python shopify_customers_sync.py             # full crawl, all customers
```

Start with `--probe` on a new store. It confirms the `read_customers` scope
and fetches a 3-customer sample before you kick off a full crawl, and it
writes nothing.

`--dry-run` is **not** a cheap preview. It still needs the scope, submits a
full bulk job, and creates the customer tables if they're missing. It may
also write a `sync_log` row if the crawl returns nothing or errors. What it
skips is writing customer rows.

`--since YYYY-MM-DD` filters on the customer's `updated_at`. `--days N` is
the same filter as a rolling window, and is ignored when `--since` is given.

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `shopify_order_discounts` (one row per order × SKU × discount kind × code,
  written as a side effect of `warehouse/connectors/shopify.py`'s `sync()`)
- `shopify_order_customers` (order → Shopify customer id, also a `sync()`
  side effect). This is populated **only when the app holds
  `read_customers`** (or `SHOPIFY_CAPTURE_CUSTOMER` forces it on). Without
  the scope the field isn't requested at all, and the table stays empty.
- `shopify_customers`, `shopify_customer_metafields`,
  `shopify_customer_flag_history` (customer dimension, created by
  `shopify_customers_sync.py`'s own `ensure_schema()` rather than
  `schema.sql`)

### What lands in `orders` (platform = `shopify`)

- **Grain:** one row per order + SKU. Duplicate-SKU line items within an
  order are summed. `order_id` is the human-facing order name (e.g.
  `#4519001`).
- **Line totals exclude shipping and tax**, so the sum will run a few
  percent below Shopify's own "total sales" figure.
- **Only the first 50 line items** of each order are fetched
  (`lineItems(first: 50)`). Larger orders are silently truncated.
- **No status filter.** The query filters only on `created_at`, so every
  financial status lands (paid, pending, refunded, voided, and so on),
  including cancelled orders. `status` holds `displayFinancialStatus`.
  Refunds are **not** netted out of `total`, so filter on `status` yourself.
- **Dates are UTC.** The window is `T00:00:00Z`–`T23:59:59Z`, and
  `order_date` is the first 10 characters of `createdAt`. That is the UTC
  calendar date, not the store's local date, so orders near midnight can land
  on the neighbouring day compared with Shopify's own reports.
- `source` holds Shopify's `sourceName` (e.g. `web`, `pos`, or an app's
  channel id).
- `original_total` is `originalTotalSet`, the line value before
  code/automatic/manual discounts, at the variant's listed price. See the
  discount note below.

## Notes

**`orders.total` is not net of discounts.** Shopify's `discountedTotalSet`
excludes several discount-allocation methods (`code/EACH/ENTITLED`,
`code/ACROSS/ALL`, `manual/ACROSS/ALL`), so summing `orders.total` alone
overstates realized revenue whenever any of those discount types are in
play. To get a true net figure, subtract `shopify_order_discounts.amount`
**joined per order + SKU**: `total - SUM(shopify_order_discounts.amount)`.

Caution before relying on that formula. The connector docstring also says
that `manual/ACROSS/EXPLICIT` and `automatic/EACH/ENTITLED` allocations *are*
already reflected in `total`, and every allocation (those included) is
written to `shopify_order_discounts`. Taken literally, subtracting all of
them would double-count those two types. Filtering the subtraction to the
unreflected `allocation_method`/`target_selection` combinations, or
computing `original_total - SUM(amount)`, avoids that. Check against a few
real orders in Shopify admin before you pick one. Promo-code discounts and
price markdowns are two distinct mechanisms with different visibility here
— see `warehouse/connectors/shopify.py`'s module docstring for the full
breakdown before building a revenue report on this table.

`shopify_customers_sync.py` deliberately never stores email, name, phone, or
address — see the module docstring for why.

`shopify_customers_sync.py` uses Shopify's Bulk Operations API, which only
allows **one bulk query per app at a time**. If another bulk query is
already running on the same app (for example, one left over from a killed
run), the script does not cancel it. It **waits for that job to finish**,
then resubmits its own. If the other job ends `FAILED` or `CANCELED`, this
run errors out. The bulk query itself has no pagination: results stream
back as JSONL, are regrouped by `__parentId`, and batches are only cut at a
root-line boundary.

**How the change log works.** Before `store_customers()` overwrites the
`shopify_customers` snapshot, `record_flag_changes()` compares each incoming
customer's `tags`/`state` against **that customer's previous snapshot row**.
It adds a row to `shopify_customer_flag_history` only when something
changed, or as a baseline the first time a customer is seen. The history
table is keyed on `(customer_id, observed_date)`, so two runs on the same
day leave one row per customer for that day, holding the later values. The
diff has to run before the snapshot upsert. Keep that ordering if you extend
the script, and see the module docstring for adding more tracked
attributes.

## Tests

- `tests/test_shopify_connector.py` covers the core connector's `_post()`
  HTTP layer: retries, throttling, and error handling. It does not yet
  cover row building (`_order_rows`, `_discount_rows`) or
  `customer_capture_enabled()`.
- `tests/test_shopify_customers_sync.py` covers schema, the no-PII
  guarantees, JSONL parsing and batching, `record_flag_changes()`, storage,
  env/scope checks, and the bulk submit/poll flow.
