# Amazon Seller (Selling Partner API)

Retail order ground truth (core), plus eight standalone scripts covering FBA
inventory, AWD (bulk-storage) inventory, returns, rank tracking, fees, SKU
economics, sales/traffic, and listing-quality diagnostics — and a manual CSV
importer for Voice of the Customer, which has no API.

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
| `amazon_event_pull.py` | standalone | Sales & Traffic for an arbitrary date window (a sale event inside the current week), stored apart from the weekly tables with its own coverage record |
| `amazon_listing_quality_sync.py` | standalone | per-SKU content-quality issues (Listings Items API) — the same checks behind Seller Central's Listing Quality Dashboard |
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
   | `SPAPI_SELLER_ID` | only `amazon_listing_quality_sync.py` — Amazon's "Merchant Token", required in the Listings Items URL path (see below) |

Six of the eight extras reuse these same variables — nothing new to
configure. `amazon_listing_quality_sync.py` additionally needs
`SPAPI_SELLER_ID` (Amazon's "Merchant Token"), and it refuses to start
without it. No documented SP-API call returns this value for the account
you're authorized as. One *undocumented* way to recover it: call
`GET /sellers/v1/marketplaceParticipations` and look for an "Amazon.com
Invoicing Shadow Marketplace" entry whose `storeName` looks like
`Invoicing_<accountId>_<sellerId>`. The seller ID is the second
underscore-delimited segment. Amazon doesn't guarantee this behavior, so
test the value before relying on it: a real SKU should return 200 with
issues, and a made-up SKU should return "SKU not found" for that same
account, not an auth error. Once confirmed, save it in `.env`. If you ever
re-authorize under a different seller account, find it again.
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
python amazon_event_pull.py --event-id spring_sale --name "Spring Sale" --start 2026-03-10 --end 2026-03-11
python amazon_listing_quality_sync.py --skus SKU1,SKU2
python amazon_listing_quality_sync.py --skus-file skus.txt
python amazon_listing_quality_sync.py --skus-file skus.txt --limit 100   # smoke test: first 100 SKUs only
python amazon_listing_quality_sync.py --skus-file skus.txt --resume      # finish an interrupted pass (see Notes)
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
- `amazon_event_asin`, `amazon_event_pull`, `dim_event_calendar` — written by
  `amazon_event_pull.py` (see [Event window pulls](#event-window-pulls)).
  `amazon_traffic_daily` is also written by it.
- `amazon_listing_quality` — one row per seller SKU (PK `seller_sku`): `asin`,
  `item_name`, `product_type`, `is_discoverable` / `is_buyable` (0/1, from
  the listing summary's status), `issue_count` and `max_severity`
  (`ERROR`/`WARNING`/`INFO`; NULL means no defects; both leave out
  non-defect code `101265`), `generic_keyword` (backend Search Terms),
  `item_type_keyword`, `synced_at`
- `amazon_listing_quality_issues` — one row per issue (PK `seller_sku`,
  `issue_seq`, where `issue_seq` is 0-based because the same code can appear
  twice on one SKU): `code`, `severity`, `message`, and `attribute_names` /
  `categories` (comma-separated)
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

The exit code is decided by a small pure function, `exit_code(status, repair,
allow_partial)`: `degraded` (a short pull) exits `0` when either `--repair` or
`--allow-partial` was passed, and `1` otherwise; a real `error` status
(an exception) always exits `1`, in every mode. If you wire this into a
scheduler that treats any nonzero exit as a pipeline-wide failure, a
`--repair` pass that still comes back short (Amazon simply hasn't published
yet, which is already recorded in `amazon_traffic_coverage` and will retry
next run) should not be allowed to fail steps that already succeeded and
don't depend on it — that asymmetry (loud on a plain short pull, quiet on an
expected repair/early-pass one) is the point of `exit_code()` existing as its
own function rather than inline logic in `main()`.

`amazon_fulfilled_shipments` (written by `amazon_fees_sync.py`) carries
`sales_channel` and `shopify_order_name` columns, letting you join Amazon's
Multi-Channel Fulfillment (MCF) shipments back to the Shopify order they
fulfilled — useful for tracing an order that shipped from FBA inventory but
sold on your own site.

`amazon_listing_quality_sync.py` has no catalog table to source an
active-SKU list from — pass `--skus`/`--skus-file` explicitly, same pattern
as `amazon_rank_sync.py`'s `--asins`/`--asins-file` (and the same weak
fallback to `amazon_fulfilled_shipments` if you pass neither). It's keyed by
**seller SKU**, not ASIN: an FBA and a non-FBA (MFN) offer of the same
product are separate SKUs that can carry different issues, so if you join
this to ASIN-grain sales/traffic data, pick one representative SKU per ASIN
first (e.g. worst severity, then most issues) or you'll double-count that
ASIN's demand. Issue code `101265` ("switch this SKU to FBA") is excluded
from `issue_count`/`max_severity` — it's a merchandising nudge, not a
content defect — but it's kept in `amazon_listing_quality_issues` so nothing
observed is silently dropped. There's no bulk report for this data, so it's
a synchronous per-SKU call; the endpoint rate-limits at 5 req/sec, which this
script paces itself under. Pass `--resume` to finish an interrupted full
pass without redoing SKUs already written. It isn't the default, because a
normal run should re-diagnose every SKU (issues, and the search terms below,
can change between runs). **Watch out:** `--resume` doesn't know which run
wrote a row. It skips *every* SKU that already has a row in
`amazon_listing_quality`, from any earlier run. So once one full pass has
finished, `--resume` skips everything on your list. Only use it right after
an interrupted run. The script prints how many SKUs it skipped and how many
remain.

Each diagnosed SKU is a snapshot of the latest result. Its rows in
`amazon_listing_quality_issues` are deleted and rewritten every time, so a
fixed issue drops out. A SKU you remove from your list is never deleted,
though; its old row stays with an older `synced_at`. Filter on `synced_at`
if you only want the current pass.

A SKU that returns 404 (delisted since you built your list), or any other
non-200 response, counts as failed. The run then logs `degraded` in
`sync_log` (platform `amazon_listing_quality`) and prints the first 10
failures. If no SKU was written, it logs `error` and exits 1. If there are
no target SKUs at all (no flags and nothing in the fallback table), it logs
`error` and exits with a message. On an older `warehouse.db`, the two
search-term columns are added automatically with `ALTER TABLE` on the next
run; you don't need to drop anything.

The same GET also captures `generic_keyword` — the real, otherwise-invisible
backend "Search Terms" field from Seller Central's edit-listing page (no
separate API or report exposes it) — plus `item_type_keyword` (Amazon's own
category classifier), at no extra API cost. A NULL here means the field is
genuinely empty on that listing, a real content gap worth surfacing.

### Event window pulls

`amazon_traffic_sync.py` only pulls whole Mon–Sun weeks, so a sale event that
falls inside the **current** week is invisible until the week closes.
`amazon_event_pull.py` pulls an arbitrary `--start`/`--end` window directly
and stores it **apart from the weekly tables** (and their coverage table), so
a partial week can never be mistaken for a finished one. It reuses
`amazon_traffic_sync`'s report request/poll/download helpers and `coverage()`
validation, so it needs the same SP-API credentials (`SPAPI_CLIENT_ID`,
`SPAPI_CLIENT_SECRET`, `SPAPI_REFRESH_TOKEN`, optional `SPAPI_REGION`) and
Sales & Traffic report access.

```bash
python amazon_event_pull.py --event-id spring_sale_2026 --name "Spring Sale"     --start 2026-03-10 --end 2026-03-11
```

| Flag | Default | Meaning |
|---|---|---|
| `--event-id` | required | Stable key; re-running with the same id **replaces** that event's rows |
| `--name` | required | Human label stored in `dim_event_calendar.event_name` |
| `--start` / `--end` | required | Inclusive `YYYY-MM-DD` window |
| `--channel` | `amazon` | Stored in `dim_event_calendar.channel` |
| `--pad-days` | `1` | Extra pre-event days of account-daily totals stored as a baseline |
| `--notes` | empty | Free text for `dim_event_calendar.notes` (defaults to the completeness note) |

Two reports are requested concurrently: the event window alone (per-ASIN
totals aggregate the whole requested range, so they must never include the
baseline pad) and, when `--pad-days` > 0, a padded window used only for the
daily rows.

Tables written (all in one transaction):

- `amazon_event_asin` — PK (`event_id`, `asin`): `parent_asin`, `sessions`,
  `page_views`, `buy_box_pct`, `units_ordered`, `ordered_sales`, `range_start`,
  `range_end`, `synced_at`. Totals over the event window only.
- `amazon_traffic_daily` — account daily totals for `[start − pad, end]`, the
  same rows the weekly pull writes (the next weekly pull overwrites them
  identically).
- `amazon_event_pull` — what was **asked** and what Amazon **returned**:
  `days_expected`, `days_returned`, `missing_days`, `bydate_sales`,
  `byasin_sales`, `sections_gap`, `is_complete` (0/1), `note`. As with the
  weekly coverage table, recorded intent — not absence of rows — is the
  completeness marker.
- `dim_event_calendar` — one row per event (`event_type='platform'`,
  `mechanic='deal'`) so event windows can be joined to any channel's data.

Completeness and exit codes: the same two checks as the weekly pull apply
(every day present; byDate vs byAsin sales within 2%). A complete pull logs
`ok` and exits `0`; a short pull logs `degraded` (sync log platform
`amazon_event`) and exits `1`. Amazon publishes the latest day late, so a pull
made while the event is still current is often short and the final day may
restate upward — re-run until `is_complete = 1`. The weekly pull after the
week closes is authoritative.

## Tests

`tests/test_amazon_inventory_sync.py`, `tests/test_amazon_awd_sync.py`,
`tests/test_amazon_awd.py`, `tests/test_amazon_returns_sync.py`,
`tests/test_amazon_rank_sync.py`, `tests/test_amazon_fees_sync.py`,
`tests/test_amazon_economics_sync.py`, `tests/test_amazon_traffic_sync.py`, `tests/test_amazon_event_pull.py`,
`tests/test_amazon_listing_quality_sync.py`, `tests/test_voc_import.py`
