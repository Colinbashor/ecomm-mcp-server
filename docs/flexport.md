# Flexport

3PL fulfillment: daily inventory snapshots (with an on-demand catalog
gap-fill, or a full catalog crawl via `--catalog`), per-order shipping cost,
customer returns, and inbound supplier shipments.

**Scripts:** `flexport_sync.py`, `flexport_orders_sync.py`,
`flexport_returns_sync.py`, `flexport_inbounds_sync.py` (standalone — not
wired into `run_sync.py`, since 3PL data doesn't fit its ads/orders shape)

## Setup

1. Get a bearer token from the Flexport portal (Settings → API). One token
   is shared by all four scripts.
2. Set in `.env`:

   | Variable | Notes |
   |---|---|
   | `FLEXPORT_API_TOKEN` | merchant tokens expire after about a year; a 401 means it needs rotating |

## Usage

```bash
python flexport_sync.py           # daily inventory snapshot + a small catalog gap-fill
python flexport_sync.py --catalog # + a full catalog crawl (slow; run occasionally, not daily)
python flexport_orders_sync.py    # per-order shipping cost (resumable event-cursor crawl)
python flexport_orders_sync.py --since-days 30 --pages 5
python flexport_orders_sync.py --restart   # discard the saved cursor and re-walk from scratch
python flexport_returns_sync.py   # customer returns
python flexport_returns_sync.py --pages 5 --restart
python flexport_inbounds_sync.py  # inbound supplier shipments
python flexport_inbounds_sync.py --max-pages 5
```

A bare run does **not** do a full catalog crawl — only inventory plus enough
catalog gap-filling to resolve any new SKUs it sees. Pass `--catalog`
periodically (it pages through your entire product list, which can take a
while for a large merchant) to fully refresh `flexport_products`.

## Tables

- `flexport_products`, `flexport_inventory`, `flexport_catalog_sync_state`
- `flexport_order_costs`, `flexport_order_packages`, `flexport_order_sync_state`
- `flexport_returns`, `flexport_return_lines`, `flexport_returns_sync_state`
- `flexport_inbounds`, `flexport_inbound_lines`

## Notes

`flexport_orders_sync.py`'s event-cursor crawl **self-heals a dead cursor**.
Flexport's `page_info` cursor can go permanently bad on their side even while
the feed itself is healthy — a *stored* cursor 500s on every retry while a
*freshly issued* one 200s within the same minute. Since an opaque cursor that
goes bad that way can't recover on its own, the connector also tracks
position a second way, as a plain timestamp read off each page's own event
data (`flexport_order_sync_state` key `events_last_time`, alongside the
`events_page_info` cursor). On a rejected/exhausted cursor it re-crafts a
fresh one from that timestamp and keeps walking in the same run — bounded to
3 re-crafts (`MAX_RECRAFTS`) — instead of surfacing a failure that needs a
human to notice and clear. On a cold start with no prior timestamp, a bad
cursor still raises rather than silently restarting the walk from an
arbitrary point.

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
  idempotent (`INSERT OR REPLACE`).
- **Checkpointing also triggers on page count, not just new-order count.** A
  resume walk near the frontier can spend a long stretch re-reading pages
  with nothing new on them, and a checkpoint trigger keyed only on "found 100
  new orders" never fires during that stretch — a kill mid-walk would then
  discard all cursor progress. `CHECKPOINT_PAGES` (25) adds a second trigger
  so position is saved during a quiet stretch too.

`flexport_orders_sync.py` and `flexport_inbounds_sync.py` both exit with
code `75` and log status `"degraded"` when a transient failure pauses the
run partway through — this is a resumable pause, not a hard error, and
matters if you're wiring either script into a scheduler that treats any
nonzero exit as an alert. `flexport_returns_sync.py` doesn't have this
pause/resume handling — a transient failure there fails the run outright
rather than pausing, so don't assume identical resilience across all three
crawlers.

## Tests

`tests/test_flexport_sync.py`, `tests/test_flexport_orders_sync.py`,
`tests/test_flexport_returns_sync.py`, `tests/test_flexport_inbounds_sync.py`
