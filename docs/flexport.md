# Flexport

3PL fulfillment: daily inventory snapshots (with an on-demand catalog
gap-fill, or a full catalog crawl via `--catalog`), per-order shipping cost,
customer returns, and inbound supplier shipments.

**Scripts:** `flexport_sync.py`, `flexport_orders_sync.py`,
`flexport_returns_sync.py`, `flexport_inbounds_sync.py` (standalone — not
wired into `run_sync.py`, since 3PL data doesn't fit its ads/orders shape)

| Script | What it pulls | Endpoint(s) | Pagination | `sync_log` platform |
|---|---|---|---|---|
| `flexport_sync.py` | catalog + current inventory levels | `/products`, `/products/inventory/all`, `/products/{logisticsSku}` | limit/offset (500) | `flexport` |
| `flexport_orders_sync.py` | per-order realized outbound cost + packages | `/events?type=Shipment.Created`, `/orders/{orderId}` | `Link` header `page_info` cursor (100) | `flexport_orders` |
| `flexport_returns_sync.py` | customer returns + returned lines | `/returns` | `Link` header `page_info` cursor (100) | `flexport_returns` |
| `flexport_inbounds_sync.py` | inbound supplier shipments + lines | `/inbounds/shipments` | limit/offset (100, shrinks on 504) | `flexport_inbounds` |

All four call `https://logistics-api.flexport.com/logistics/api/2024-06` with
a bearer token, and all are read-only against Flexport.

## Setup

1. Get a bearer token from the Flexport portal (Settings → API). One token
   is shared by all four scripts.
2. Set in `.env` (it is listed in `.env.example`, Flexport section):

   | Variable | Notes |
   |---|---|
   | `FLEXPORT_API_TOKEN` | merchant tokens expire after about a year; a 401 means it needs rotating |

   This is the only environment variable any of the four scripts reads. If it
   is unset, each script prints a "not set — skipping" message and exits `0`
   without touching the database or writing a `sync_log` row, so it is safe
   to leave them in a scheduled job before Flexport is wired up.

## Usage

```bash
python flexport_sync.py           # daily inventory snapshot + a small catalog gap-fill
python flexport_sync.py --catalog # + a full catalog crawl (slow; run occasionally, not daily)
python flexport_orders_sync.py    # per-order shipping cost (resumable event-cursor crawl)
python flexport_orders_sync.py --since-days 30 --pages 5
python flexport_orders_sync.py --restart   # discard the saved cursor and re-walk from scratch
python flexport_orders_sync.py --from-shopify --days 10   # fetch by id, enumerated from Shopify
python flexport_returns_sync.py   # customer returns
python flexport_returns_sync.py --pages 5 --restart
python flexport_inbounds_sync.py  # inbound supplier shipments
python flexport_inbounds_sync.py --max-pages 5
```

### Flags

| Script | Flag | Default | Effect |
|---|---|---|---|
| `flexport_sync.py` | `--catalog` | off | Also run a full `/products` crawl before the inventory snapshot. Not argparse — the script just checks `sys.argv` for this string, so unknown flags are silently ignored. |
| `flexport_orders_sync.py` | `--pages N` | `100000` | Cap on event pages walked this run (a runaway guard, not an expected size). |
| | `--since-days N` | none | Clear the stored cursor and seed a hand-crafted cursor ~N days back. Takes precedence over `--restart`. |
| | `--restart` | off | Clear the stored cursor and walk from the event feed's floor (~12 months back). |
| | `--from-shopify` | off | Skip the `/events` crawl: enumerate Flexport external order ids from Shopify and fetch each with `GET /orders/external_id/{id}`. See below. |
| | `--days N` | `10` | With `--from-shopify`: how many days of Shopify orders to scan. |
| | `--workers N` | `8` | With `--from-shopify`: concurrent order fetches. |
| `flexport_returns_sync.py` | `--pages N` | `100000` | Cap on `/returns` pages walked this run. |
| | `--restart` | off | Delete the stored cursor and re-crawl from the floor. |
| `flexport_inbounds_sync.py` | `--max-pages N` | `1000` | Cap on `/inbounds/shipments` pages walked this run. |

A bare `flexport_sync.py` run does **not** do a full catalog crawl — only
inventory plus enough catalog gap-filling to resolve any new SKUs it sees.
Pass `--catalog` periodically (it pages through your entire product list,
which can take a while for a large merchant) to fully refresh
`flexport_products`.

`flexport_orders_sync.py` picks its start position in this order:
`--since-days` → `--restart` (feed floor) → the stored `events_page_info`
cursor → a cursor re-seeded at the data frontier (`frontier_seed()`, see
Notes) → the feed floor on a truly fresh database.

### Shopify-driven direct fetch (`--from-shopify`)

The `/events` crawl is bounded by that endpoint's per-page latency, which on a
busy store can fall days behind. If your Flexport `externalOrderId` is
derivable from Shopify, this mode is much faster and needs no cursor:

1. Page Shopify `orders` created in the last `--days` days (needs the
   `read_merchant_managed_fulfillment_orders` and
   `read_third_party_fulfillment_orders` scopes plus the usual Shopify
   credentials) and read each order's `fulfillmentOrders`.
2. Keep fulfillment orders assigned to the Shopify location named by
   `FLEXPORT_SHOPIFY_LOCATION` whose `requestStatus` is `ACCEPTED`, `CLOSED` or
   `CANCELLATION_*` (Flexport only knows about accepted ones) and whose
   `status` is not `CANCELLED`.
3. Build the external id as `<order name><FLEXPORT_EXTERNAL_ID_SPLIT_MARKER><numeric
   fulfillment-order id>` — leave the marker unset to use the bare order name.
   Mirror whatever your Shopify→Flexport integration actually sends.
4. Fetch each id (percent-encoded in the path, since order names can contain
   `/`) in a thread pool and upsert into the same two tables, using named
   columns. Ids already stored **with a cost** are skipped; ones stored before
   they shipped are re-fetched so the cost lands once Flexport posts it.

| Variable | Notes |
|---|---|
| `FLEXPORT_SHOPIFY_LOCATION` | required for this mode — name of the Shopify location that maps to Flexport; unset → exits `1` |
| `FLEXPORT_EXTERNAL_ID_SPLIT_MARKER` | optional literal between order name and fulfillment-order id |

A 404 is an *expected* answer (Flexport hasn't received it yet) and is counted,
not raised (`FlexportNotFound`). But an accepted fulfillment order must exist on
Flexport's side, so if more than 5% of ≥20 fetched ids 404 the run logs
`degraded` with `EXTERNAL-ID RULE MAY HAVE CHANGED` — the id rule no longer
matches your integration, which would otherwise look like a quiet zero. An empty
pull (no Shopify orders, or no Flexport ids) is also `degraded`, and more than
`max(5, 1%)` fetch errors is `degraded`. Degraded exits `75`. Logged under
platform `flexport_orders_direct`. Run it ahead of the crawl, which stays useful
as a backstop for orders whose external id has some other shape.

### Exit codes and `sync_log`

| Outcome | Exit | `sync_log.status` | `rows_written` |
|---|---|---|---|
| Token unset | `0` | (no row) | — |
| Clean run | `0` | `ok` | inventory rows / new orders / returns / shipments |
| Transient pause (orders, inbounds only) | `75` | `degraded` (message `paused, resumable` / `paused (transient)`) | rows committed before the pause |
| Any other exception | non-zero (traceback re-raised) | `error` (message = exception text, truncated to 500 chars) | `0` |

`flexport_sync.py` and `flexport_returns_sync.py` have no degraded/75 path:
retry exhaustion there raises `RuntimeError` and the run is logged `error`.

## Tables

All writes are `INSERT OR REPLACE` on the primary key (idempotent upserts)
unless noted. `synced_at` is the run's UTC ISO timestamp.

### `flexport_sync.py`

**`flexport_products`** — PK `logistics_sku`. Flexport's view of your
catalog and the translation between Flexport's id space and yours.

| Column | Notes |
|---|---|
| `logistics_sku` | Flexport's id for the item |
| `merchant_sku` | your own SKU (indexed) |
| `name` | |
| `barcodes` | comma-joined |
| `created_at`, `updated_at` | as returned by Flexport |
| `weight_oz` | per-unit weight normalized to oz (from oz/lb/kg/g; unknown unit treated as oz) |
| `length_in`, `width_in`, `height_in` | per-unit dims normalized to inches (from in/cm/mm/m) |
| `dims_locked` | `1` vendor-confirmed measurement, `0` estimated, `NULL` if Flexport didn't say |
| `synced_at` | |

Written by the full `--catalog` crawl (committed page by page) and by the
daily gap-fill (one `GET /products/{logisticsSku}` for each inventory SKU not
already in the table; a failed gap-fill is printed and skipped, not fatal).

**`flexport_inventory`** — PK `(snapshot_date, logistics_sku)`. One row per
SKU per day. The API only returns *current* levels, so running daily is what
builds history; earlier days are never overwritten, but a second run on the
same day replaces that day's rows.

| Column | Notes |
|---|---|
| `snapshot_date` | local `date.today()` of the run |
| `logistics_sku` | |
| `merchant_sku` | denormalized from `flexport_products` (indexed); `NULL` if unmapped |
| `available`, `on_hand`, `unavailable` | integers, default `0` |
| `units_per_pack` | default `1` |
| `synced_at` | |

**`flexport_catalog_sync_state`** — `key` / `value`. Holds `catalog_offset`,
the resume offset for an interrupted `--catalog` crawl; saved in the same
transaction as each page and deleted when a full crawl completes.

### `flexport_orders_sync.py`

**`flexport_order_costs`** — PK `order_id`. One row per outbound order.

| Column | Notes |
|---|---|
| `order_id` | Flexport's order id |
| `external_order_id` | your own order id, if echoed back (indexed) |
| `cost` | all-in realized outbound cost for the whole order; `NULL` (not 0) when absent |
| `currency` | as returned, else `USD` when a cost exists |
| `internal_status`, `fulfillment_status` | from the order's `state` |
| `created_at`, `shipped_at`, `delivered_at` | |
| `units` | sum of line-item quantities |
| `n_shipments`, `n_packages` | |
| `total_weight_oz` | sum of package weights (oz as-is; any other unit is treated as lb ×16) |
| `carriers`, `shipping_methods` | comma-joined distinct values |
| `is_international` | `1` if any package's carrier is `PASSPORT` or its method contains `DDU`/`DDP` |
| `synced_at` | |

**`flexport_order_packages`** — PK `(order_id, package_id)`, indexed on
`order_id`. Columns: `order_id`, `shipment_id`, `package_id`,
`warehouse_id`, `carrier`, `shipping_method`, `tracking_code`, `weight_oz`
(rounded, `NULL` if zero), `length_in`/`width_in`/`height_in`,
`logistics_skus` (comma-joined SKUs in the package), `synced_at`. Note the
dimension columns are stored exactly as the label reports them — unlike
`flexport_products`, they are **not** unit-converted despite the `_in` suffix.

**`flexport_order_sync_state`** — `key` / `value` / `updated_at`. Keys:
`events_page_info` (last cursor whose orders are fully flushed) and
`events_last_time` (the last event timestamp seen, used to self-heal a dead
cursor).

Orders already present in `flexport_order_costs` are **skipped, not
re-fetched**, so an existing row is never refreshed by later crawls (e.g. a
`delivered_at` that was null at first fetch stays null). Order detail is
fetched with 8 concurrent workers; a single order whose fetch fails is
printed and dropped for this run rather than aborting the page.

### `flexport_returns_sync.py`

**`flexport_returns`** — PK `id` (Flexport return id), indexed on `status`.
Columns: `id`, `status` (CREATED/SHIPPED/RECEIVED/INSPECTED/PROCESSED),
`rma`, `external_return_id`, `fulfillment_order_id` (joins
`flexport_order_costs.order_id` when supplied — often `NULL`), `carrier`,
`tracking_code`, `tracking_status`, `source_name`, `source_city`,
`source_state`, `source_zip`, `source_country` (where the customer shipped
from), `shipped_at`, `received_at`, `inspected_at`, `n_lines`, `synced_at`.

**`flexport_return_lines`** — PK `(return_id, line_no)` (`line_no` is the
0-based position in the return's item list), indexed on `identifier`.
Columns: `return_id`, `line_no`, `identifier` (the returned SKU),
`expected_quantity`, `n_inspected`, `received_condition`,
`final_condition`, `disposition` (e.g. `RESTOCK`), `synced_at`. A line can
have several inspected items; only the **first** inspection's
condition/disposition is kept, plus the count in `n_inspected`.

**`flexport_returns_sync_state`** — `key` / `value` / `updated_at`; key
`returns_page_info`. Rows and the cursor are committed every 200 returns and
at the end of the run. Lines are upserted, not delete-rewritten, so if a
return's item list ever shrank, the old higher-numbered lines would remain.

### `flexport_inbounds_sync.py`

**`flexport_inbounds`** — PK `id` (Flexport inbound shipment id), indexed on
`status` and `shipping_plan_external_id`. Columns: `id`, `receiving_id`,
`status` (WORKING/READY_TO_SHIP/IN_TRANSIT/ARRIVED/COMPLETED/...),
`shipping_plan_id`, `shipping_plan_external_id` (the PO reference, e.g. your
own PO number), `shipping_plan_name`, `shipping_option`,
`shipment_destination`, `booking_id`, `ship_from_name` (supplier),
`ship_from_country`, `ship_to_name` (receiving warehouse), `arrived_at`,
`completed_at`, `n_lines`, `n_packages`, `expected_units`,
`sellable_units`, `damaged_units` (rolled up from the lines), `synced_at`.

**`flexport_inbound_lines`** — PK `(inbound_id, shipment_item_id)`, indexed
on `merchant_sku`. Columns: `inbound_id`, `shipment_item_id`,
`merchant_sku` (your SKU, carried directly by this payload), `logistics_sku`,
`pack_of_dsku`, `line_item_id`, `expected`, `sellable`, `damaged` (missing
counts default to 0), `synced_at`.

Write semantics: each run re-walks the whole feed newest-first from offset 0
and commits every 100 shipments. Per shipment, its lines are **deleted and
rewritten** in the same transaction (so a shrunk item set leaves no stale
lines) and the shipment row is upserted. Shipments that drop out of the feed
are never pruned. There is no sync-state table — nothing to resume, the next
run just converges the snapshot.

## Notes

### Retries and error handling

| Script | Attempts | Connection error / timeout | 429 | 5xx | 504 | 401 | other non-200 |
|---|---|---|---|---|---|---|---|
| `flexport_sync.py` | 8 | sleep 15s | sleep `Retry-After` (default 10s) | sleep 10s | same as 5xx | fatal | fatal |
| `flexport_orders_sync.py` | 12 | exp. backoff 10s→90s cap | `Retry-After` or backoff | backoff | halve `limit` (min 10), retry immediately; at min, backoff | fatal | fatal (except `/events` 400, below) |
| `flexport_returns_sync.py` | 12 | backoff 10s→90s | `Retry-After` or backoff | backoff | same as 5xx | fatal | fatal |
| `flexport_inbounds_sync.py` | 12 | backoff 10s→90s | `Retry-After` or backoff | backoff | raise `FlexportGatewayTimeout` → caller halves `limit` (min 10) | fatal | fatal |

Request timeout is 60s in `flexport_sync.py` and 90s in the other three.
In `flexport_orders_sync.py` a 400 from `/events` is treated as possible
load-shedding and retried with backoff; only once the whole budget is spent
does it raise `FlexportBadCursor`. Retry exhaustion in the orders and
inbounds scripts raises `FlexportTransient`, which triggers the graceful
pause (exit 75); in the other two it is a plain `RuntimeError`.

`flexport_sync.py`'s `--catalog` crawl also retries a `database is locked`
error on a page commit up to 10 times with exponential backoff (capped at
60s), since other syncs may be writing the same SQLite file.

### Orders: discovery vs retrieval

There is no list-all orders endpoint: order ids are discovered by walking
`Shipment.Created` events (de-duplicated within a page and against orders
already stored), then each order is fetched via `GET /orders/{id}`. The event
feed only retains roughly the trailing 12 months — a cursor crafted further
back silently snaps forward to the floor — so this crawl cannot reach older
orders even though `GET /orders/{id}` would still return them. Date-range
filters and `offset` on `/events` are unreliable (ignored / frozen past a
depth), which is why the crawl follows the `Link: <...page_info=...>;
rel="next"` header instead. `page_info` cursors are base64'd JSON wrapping a
time-sortable ULID, which is what lets `--since-days` and the self-heal below
hand-craft a cursor at an arbitrary time (`page_info_at()`).

### Orders: dead-cursor self-heal

`flexport_orders_sync.py`'s event-cursor crawl **self-heals a dead cursor**.
Flexport's `page_info` cursor can go permanently bad on their side even while
the feed itself is healthy — a *stored* cursor 500s on every retry while a
*freshly issued* one 200s within the same minute. Since an opaque cursor that
goes bad that way can't recover on its own, the connector also tracks
position a second way, as a plain timestamp read off each page's own event
data (`flexport_order_sync_state` key `events_last_time`, alongside the
`events_page_info` cursor). On a rejected/exhausted cursor (`FlexportBadCursor`
or `FlexportTransient` from `/events`) it re-crafts a fresh one from that
timestamp and keeps walking in the same run — bounded to 3 re-crafts
(`MAX_RECRAFTS`) — instead of surfacing a failure that needs a human to notice
and clear. On a cold start with no prior timestamp, a bad cursor still raises
rather than silently restarting the walk from an arbitrary point.

Three further refinements, each found from a failure mode where the crawl
looked fine but made little or no progress:

- **Re-craft nudges forward instead of retrying the identical spot.**
  Whatever makes a cursor position bad tends to be sticky, so re-crafting at
  the exact same timestamp on every attempt can fail every time and burn the
  whole recraft budget for nothing. Each retry steps the position forward by
  `RECRAFT_NUDGE_MINUTES * recrafts` (15 min, then 30, then 45).
- **No stored cursor re-seeds from the data frontier, not a fixed lookback.**
  On resume with no validated cursor (fresh deployment past its first run, or
  a cursor the poison guard just discarded), `frontier_seed()` derives the
  resume position from `MAX(created_at)` in `flexport_order_costs` — the data
  actually on hand — rather than a fixed "N days back" window sized for an
  initial backfill. That fixed window is wrong once the backfill is done and
  the crawl is just keeping the tip current: it can re-walk a long stretch of
  orders already stored. `FRONTIER_OVERLAP_HOURS` (2h) is re-read as cheap
  insurance against a boundary gap; re-reads are harmless since writes are
  idempotent (`INSERT OR REPLACE`). If `events_last_time` is also missing,
  it is anchored to the same frontier time so a first re-craft can't rewind
  the crawl.
- **Checkpointing also triggers on page count, not just new-order count.** A
  resume walk near the frontier can spend a long stretch re-reading pages
  with nothing new on them, and a checkpoint trigger keyed only on "found 100
  new orders" never fires during that stretch — a kill mid-walk would then
  discard all cursor progress. `CHECKPOINT_PAGES` (25) adds a second trigger
  so position is saved during a quiet stretch too.

### Graceful pause (exit 75)

`flexport_orders_sync.py` and `flexport_inbounds_sync.py` both exit with
code `75` and log status `"degraded"` when a transient failure pauses the
run partway through — this is a resumable pause, not a hard error, and
matters if you're wiring either script into a scheduler that treats any
nonzero exit as an alert. On pause the orders crawl flushes what it has and
saves its cursor; the inbounds crawl keeps already-committed pages and the
next run re-walks from the top. `flexport_returns_sync.py` doesn't have this
pause/resume handling — a transient failure there fails the run outright
(logged `error`) rather than pausing; returns committed at the last
200-return checkpoint, and the cursor saved with them, survive. Don't assume
identical resilience across all three crawlers.

### Page-size shrink on 504

**The orders and inbounds crawlers shrink their page size on a 504, rather
than retrying the identical request** (`flexport_sync.py` and
`flexport_returns_sync.py` treat a 504 like any other 5xx). Flexport sits
behind AWS API Gateway (look for an `x-amz-apigw-id` response header), which
enforces a hard ~29-second integration timeout. Some endpoints' backend cost
scales with page size (and, for offset-paginated endpoints, with offset too),
so a deep crawl at a fixed page size can hit a wall well before the feed
actually ends — past a certain point every request at that page size
consistently exceeds 29s and comes back as
`504 {"message": "Endpoint request timed out"}`, while the identical request
at a smaller page size succeeds in a few seconds. This is deterministic, not
transient: retrying the identical request never works.

The two crawlers implement it differently:

- `flexport_inbounds_sync.py`: a 504 raises `FlexportGatewayTimeout` (a
  `FlexportTransient` subclass) immediately; the offset loop halves the limit
  (100 → 50 → 25 → 12 → 10) and retries at the same offset with no backoff.
  The shrunk limit **stays in effect for the rest of the run** (it does not
  grow back). A 504 at the floor of 10 re-raises and lands in the graceful
  pause.
- `flexport_orders_sync.py`: the shrink happens inside `_request`, on a local
  copy of the params, so it applies only to that one request — the next page
  goes back to `limit=100`. `/events` has a mostly-fixed per-request cost
  close to the ceiling regardless of page size, so it keeps the larger default
  and just tolerates the occasional shrink-and-retry. A 504 already at the
  floor falls back to ordinary backoff retries.

If you add another paginated Flexport caller, measure its own per-page
latency at a few page sizes before assuming one page size is safe everywhere.

For offset-based pagination specifically (`flexport_inbounds_sync.py`), the
walk must also stop **only on a genuinely empty page, never a short one** — a
short page under an adaptively-shrunk limit is expected and does not mean the
feed has ended, and the offset must advance by the number of rows *actually*
returned rather than the requested page size, so a shrunk page can't skip
records. `flexport_sync.py`'s offset paging follows the same
advance-by-actual-rows / stop-on-empty rule, because the bulk inventory
endpoint can silently return fewer rows than the requested `limit`.
Cursor-based pagination doesn't have this trap: a shorter page still returns
its own valid next-cursor with no call-site change needed.

### Other gotchas

- The `/products` catalog endpoint silently ignores "updated after" style
  filters, which is why there is no incremental catalog pull.
- Inventory rows whose SKU could not be gap-filled are still written, with
  `merchant_sku` `NULL`; a later `--catalog` or gap-fill run maps them for
  future snapshots but does not backfill past days.
- Records with no `id` are skipped by the returns and inbounds crawlers.

## Tests

- `tests/test_flexport_sync.py` — schema, oz/inch conversion, product-row
  normalization (`dims_locked` null vs false), offset paging by actual batch
  size, gap-fill (including a failed gap-fill not aborting), skip when the
  token is unset.
- `tests/test_flexport_orders_sync.py` — schema, `Link` header parsing, ULID
  / `page_info_at` crafting, event-page dedupe and `max_pages`, last-time
  tracking, re-craft (bounded, nudged forward, cold-start raises),
  `frontier_seed`, page-count checkpointing, retry policy (401 fatal, 5xx
  retried / exhausted → transient, 504 shrink without mutating caller params,
  504 at min page backs off), order-row mapping, skipping stored orders,
  transient pause, `--restart`.
- `tests/test_flexport_returns_sync.py` — schema, cursor parsing,
  first-inspection disposition, page walk, cursor save, `--restart`,
  malformed records skipped.
- `tests/test_flexport_inbounds_sync.py` — schema, walking past short pages
  until empty, 504 shrink and re-raise at min page, line-count roll-ups,
  line delete-rewrite when an item set shrinks, transient pause keeping
  committed pages.
