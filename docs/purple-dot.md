# Purple Dot

Pre-order/waitlist bookings and their eventual export into real storefront
orders, plus daily waitlist inventory-allocation snapshots.

**Script:** `purple_dot_sync.py` (standalone — not wired into `run_sync.py`).
It owns its six tables via `ensure_schema()` (`CREATE TABLE/INDEX IF NOT
EXISTS` only), so it's safe to run against a brand-new `warehouse.db`. It is
**read-only** against Purple Dot: it only issues GETs.

Kept in its own tables rather than the shared `orders` table. An exported
pre-order already shows up in your storefront order feed, so writing it into
`orders` again would double-count. The two rows are also different measures:
a booking is dated at booking time, and the storefront order appears only at
export time, which can be days to weeks later (or never, if the booking
cancels).

## Setup

Set in `.env` (the variable is already listed in `.env.example`):

| Variable | Notes |
|---|---|
| `PURPLE_DOT_ACCESS_TOKEN` | The **private** API access token from the Purple Dot merchant portal's API Keys page. Purple Dot's separate **public** client-side key will not authenticate this API. A 401/403 on every call usually means the wrong key was configured, not a revoked one. |

If `PURPLE_DOT_ACCESS_TOKEN` is unset or empty, the script prints
`PURPLE_DOT_ACCESS_TOKEN not set - skipping Purple Dot sync.` and exits 0
without touching the database or writing a `sync_log` row, so a scheduled job
stays green until credentials land.

API base: `https://www.purpledotprice.com/admin/api/v1`, authenticated with
the `X-Purple-Dot-Access-Token` header.

## Usage

```bash
python purple_dot_sync.py                        # incremental (updated_at_min high-water mark), both grains
python purple_dot_sync.py --days 30              # re-pull pre-orders updated in the last 30 days
python purple_dot_sync.py --only preorders       # or --only waitlists
python purple_dot_sync.py --backfill             # resumable full crawl of all pre-orders from the feed start
python purple_dot_sync.py --backfill --pages 10  # same, capped at 10 pages this run
python purple_dot_sync.py --backfill --restart   # discard the saved cursor and restart the backfill
python purple_dot_sync.py --snapshot-all         # snapshot every waitlist state, not just live/scheduled
```

| Flag | Default | Effect |
|---|---|---|
| `--backfill` | off | Pre-orders only: crawl the whole feed oldest-first (by `created_at`) with no `updated_at_min` filter, resuming from the saved cursor if one exists. `--days` is ignored in this mode. |
| `--restart` | off | With `--backfill`: delete the saved backfill cursor before the run, so the crawl starts at the feed start. It has no effect without `--backfill`. |
| `--days N` | *(none)* | Incremental window: re-pull pre-orders **updated** in the last N days, instead of using the stored high-water mark. Doesn't affect waitlists. |
| `--only` | both | `preorders` or `waitlists` — run just one grain. |
| `--snapshot-all` | off | Write `purple_dot_waitlist_inventory` rows for every waitlist state, not just `live`/`scheduled`. |
| `--pages N` | `100000` | Cap on pages fetched **per grain** this run (pre-orders and waitlists each get N). Useful for chunking a backfill. |

### How the pre-order window is chosen

- **Incremental (default).** Filters `/pre-orders` with `updated_at_min`,
  because the pre-order object has no `updated_at` field but the filter
  works. It's the only way to catch a cancellation, refund, or new export
  landing on an older booking. The window starts at the stored high-water
  mark minus a **48-hour overlap**. With no high-water mark yet (first run),
  it covers the last **7 days** and prints a hint to run `--backfill` for full
  history. On success the high-water mark is set to the time **this run
  started**, so the next run's overlap also covers updates made while this
  run was going. If the run raises, the high-water mark isn't advanced.
- **`--days N`.** Same as incremental, but the window is simply now minus N
  days. The high-water mark is still advanced afterwards.
- **`--backfill`.** No filters. The `starting_after` cursor is saved to
  `purple_dot_sync_state` at each checkpoint, so an interrupted or
  `--pages`-capped crawl resumes where it left off on the next `--backfill`
  run. A backfill never reads or writes the incremental high-water mark.

Before paging, the script calls `/pre-orders/count` with the same filters and
prints the expected total. If fewer bookings were fetched than reported, it
prints a `NOTE: fetched X of Y reported` line (with "re-run to continue
(cursor saved)" in backfill mode). If the count call fails, the script prints
`(count unavailable: ...)` and carries on. The count is never fatal.

### Writes, checkpoints, and resume

- Pre-orders are buffered and committed every **500 bookings** (checked after
  each 200-record page, so in practice about every 3 pages). The backfill
  cursor is saved after each commit.
- A final flush runs in a `finally` block, so rows already fetched are never
  lost when a later page fails.
- When a backfill reaches the end of the feed, the last page has no next
  cursor, so the stored cursor stays at the one before it. A later
  `--backfill` (without `--restart`) resumes near the end and picks up newer
  bookings instead of recrawling everything. Use `--restart` for a genuinely
  fresh crawl.
- Waitlists aren't cursor-resumable. Every run pages the full `/waitlists`
  list, committing every 500 waitlists.

### Retries and errors

Each GET (`timeout=90`, redirects **not** followed) is tried up to 12 times:

| Response | Behavior |
|---|---|
| Connection error / timeout | retried, sleeping `min(60, 5 × attempt)` s |
| `429` | retried after `Retry-After` seconds (default 30) |
| `5xx` | retried, sleeping `min(60, 10 × attempt)` s |
| `401` / `403` | fatal right away, with a message saying the token was rejected and to check it's the private token |
| `301/302/303/307/308` | fatal right away: a redirect (typically to `/admin/login`) means a **wrong endpoint path**, not an auth problem |
| any other non-200, or `meta.result` not `success` | fatal right away |

When all 12 attempts are used up, the script raises `kept failing after retries`
with the last error. Pages are fetched with a 0.1 s pause between them.

### sync_log and exit status

The two grains are logged as **separate** `sync_log` entries, so one failing
doesn't hide a working sync of the other:

| Platform | `rows_written` |
|---|---|
| `purple_dot` | bookings (pre-orders) fetched this run |
| `purple_dot_waitlists` | waitlists fetched this run |

A failure logs `status = 'error'` with the exception message (truncated to
500 chars) and `rows_written = 0`, then the other grain still runs. If either
grain failed, the script exits non-zero with `Purple Dot sync failures:
preorders, waitlists` (listing whichever failed). Check `last_sync_status`
for both platform names.

## Tables

Every write is `INSERT OR REPLACE` on the primary key (an idempotent upsert).
Nothing is ever deleted, and every row carries a `synced_at` UTC timestamp.

| Table | Primary key | What's in it |
|---|---|---|
| `purple_dot_preorders` | `id` (Purple Dot UUID) | One row per booking (see columns below). |
| `purple_dot_preorder_lines` | `(preorder_id, line_id)` | One row per booking line: **the useful grain**. Lines with no `id` in the payload are dropped. |
| `purple_dot_preorder_exports` | `(preorder_id, shopify_order_id)` | One row per storefront order a booking exported into. |
| `purple_dot_waitlists` | `id` | **Current** state per waitlist, latest state wins. Written for every waitlist regardless of state. |
| `purple_dot_waitlist_inventory` | `(snapshot_date, waitlist_id, variant_id)` | **Daily snapshot** of per-variant allocation. |
| `purple_dot_sync_state` | `key` | `preorders_cursor` (backfill resume point) and `preorders_updated_high_water` (incremental mark), plus `updated_at`. |

Indexes: lines on `sku` and `waitlist_id`, preorders on `created_at` and
`shopify_order_id`, waitlist inventory on `sku`, and exports on
`shopify_order_id`.

**`purple_dot_preorders`**: `order_number`, `reference` (customer-facing,
e.g. `#PD1731592`), `created_at`, `placed_at`, `cancelled_at`,
`cancel_reason`, `currency`, `customer_external_id` (your storefront's
customer id), money as REAL: `subtotal_price`, `total_discounts`,
`total_tax`, `total_price`, `total_refunded`. Also `tax_included` (0/1),
`discount_codes` (JSON array as returned), `ship_city`, `ship_province` (the
province **code**), `ship_country` (the country **code**), rolled-up `n_lines`
and `n_units`, and a convenience link to the **first** export:
`shopify_order_id` (always stored as a string), `shopify_order_number`,
`exported_at`, and `n_exported` (>1 means a split export across several
storefront orders). The first-export fields are NULL for a booking that
hasn't exported.

**`purple_dot_preorder_lines`**: `line_no` (0-based position), `sku`,
`product_id` and `variant_id` (storefront ids, the join keys into your catalog
and order tables), `name`, `quantity`, `unit_price`, `unit_total`, `price`
(pre-discount line total), `total_discount`, `total` (post-discount,
pre-tax), `taxable` (0/1), `earliest_ship_date`/`latest_ship_date` (the
promised delivery window), `waitlist_id`, `cancelled` (0/1), `cancelled_at`,
and `shopify_line_item_id` (a direct line-level join into the storefront
order, populated once the line exports, where the API provides it).

**`purple_dot_preorder_exports`**: `export_no` (0-based export order),
`shopify_order_number` (e.g. `PD1389447/2` on a split export), `export_type`
(e.g. `SHOPIFY_ORDER`), `exported_at`, and `n_lines` (lines carried in this
particular export). This is a separate table because a booking can export in
**multiple parts** as stock arrives in waves, and a single parent column
would only ever capture the first. Join your order table on
`shopify_order_id`.

**`purple_dot_waitlists`**: `created_at`, `updated_at`, `state` (e.g.
Live/Paused/Closed/Scheduled/Draft; the exact enum depends on your account),
`earliest_ship_date`, `latest_ship_date`, `launch_date`,
`scheduled_pause_date`, `labels` (JSON array), `product_id` (storefront), the
product-level `buy_size` (units offered), `committed` (units booked), and
`available`, plus `n_variants`.

**`purple_dot_waitlist_inventory`**: `snapshot_date` (UTC date of the run),
`waitlist_id`, `variant_id` (stored as a string), `sku`, `state`
(denormalized from the waitlist), `buy_size` (units allocated to the
pre-order), `committed` (units booked), and `available` (units left; NULL on
per-product-only waitlists). Variants with no `variant_id` are dropped. The
API only reports **current** levels, so running this daily is what builds
sell-through history. Re-running on the same UTC day overwrites that day's
rows. It's **scoped by default** to waitlists whose state (case-insensitive)
is `live` or `scheduled`, since paused, closed, or draft waitlists don't move
and would just add dead rows. `--snapshot-all` overrides this.

## Notes

**Booked vs exported.** If your only view of pre-order demand today comes from
storefront orders, expect this feed to run noticeably **ahead** of that
number. A booking only becomes a storefront order once Purple Dot exports it,
which can lag from same-day to several weeks, and some bookings cancel
before they ever export. Don't expect a booking to have a matching export row
right away. Work out your own lag distribution from `created_at` vs
`exported_at` rather than assuming a fixed lag, and decide which measure you
mean before quoting "the" total.

**Currency is mixed and stored as-is, with no FX conversion.** The payload
has no exchange rate. Money is in each order's own `currency`. Never sum
`total_price` (or any money column) across rows without filtering to one
currency first. Zero-minor-unit currencies (e.g. JPY, KRW) are about 100×
the numeric size of a dollar-equivalent order, so a naive SUM can be
dominated by a handful of them.

**Cancellations and refunds are material, not an edge case.** A line can
cancel independently of its parent order, so filter `cancelled = 0` at the
**line** grain. A partially cancelled order still has an order-level row that
looks intact. Net `total_refunded` against `total_price` rather than treating
`total_price` as realized revenue.

**Non-merchandise lines** (shipping-protection upsells, gift cards,
membership add-ons) are stored as-is alongside real product lines, with no
filtering. Apply the same exclusion list you use for other order feeds if
you're computing merchandise-only units or revenue.

**PII is deliberately not stored**: no customer email, name, phone, or street
address, matching `shopify_customers_sync.py`. `customer_external_id` is kept
for repeat-booking and cohort analysis, and `ship_city`/`ship_province`/
`ship_country` are kept for demand geography. If you expose the warehouse
over a shared interface with column-level access control, consider adding
the geography columns to its denylist.

**API quirks worth knowing** (Purple Dot's private admin API; verify against
their docs since the contract can shift):

- Pre-orders sort oldest-first by `created_at` and page via an opaque
  `starting_after` cursor. Passing `updated_at_min` switches ordering to
  update time. Page sizes max out at 200 for pre-orders and 100 for
  waitlists, and the script always uses the max.
- The `Link` header uses a non-standard `rel={next}` (curly braces). The
  script ignores it and reads `has_more`/`starting_after` from the response
  body instead.
- `/fulfillment-orders` (hyphenated) is the real endpoint. It returns an empty
  list if you fulfill outside Purple Dot. The underscore spelling redirects to
  an admin login page, which looks like an auth problem but is just a wrong
  path. This script doesn't call it.
- `/inventory` is a POST that **writes** Purple Dot's allocation. This
  connector never calls it.
- A full backfill is usually a same-sitting job (about one request per 200
  bookings). There's no fixed floor date: the crawl runs until the API stops
  returning records.

## Tests

`tests/test_purple_dot_sync.py` is hermetic (no network, no real
`warehouse.db`). It covers:

- schema creation and idempotency
- column-order parity of the pre-order, line, export, waitlist, and inventory
  row builders with the DDL
- string-cast export and variant ids, and dropping id-less lines and variants
- NULL export fields on an unexported cancelled booking
- `_get` retry behavior: connection errors, 429s, and 5xx are retried, while
  401s and redirects fail immediately
- incremental writes, backfill cursor persistence, `--restart` clearing the
  cursor, and upsert idempotency
- the live/scheduled snapshot scoping and the `--snapshot-all` override
- the clean skip when `PURPLE_DOT_ACCESS_TOKEN` is missing
