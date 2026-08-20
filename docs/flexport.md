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
python flexport_returns_sync.py   # customer returns
python flexport_inbounds_sync.py  # inbound supplier shipments
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

## Tests

`tests/test_flexport_sync.py`, `tests/test_flexport_orders_sync.py`,
`tests/test_flexport_returns_sync.py`, `tests/test_flexport_inbounds_sync.py`
