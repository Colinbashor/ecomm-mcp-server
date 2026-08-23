# Purple Dot

Pre-order/waitlist bookings and their eventual export into real storefront
orders, plus daily waitlist inventory-allocation snapshots.

**Script:** `purple_dot_sync.py` (standalone — not wired into `run_sync.py`)

Kept in its own tables rather than the shared `orders` table, since a booking
and its export are different measures at different times.

## Setup

Set in `.env`:

| Variable | Notes |
|---|---|
| `PURPLE_DOT_ACCESS_TOKEN` | the **private** API access token from the Purple Dot merchant portal's API Keys page — not the public client-side key, which will not authenticate this API |

## Usage

```bash
python purple_dot_sync.py                        # default lookback, all grains
python purple_dot_sync.py --days 30
python purple_dot_sync.py --only preorders        # or --only waitlists
python purple_dot_sync.py --backfill --pages 10   # resumable full crawl from the feed start
python purple_dot_sync.py --backfill --restart    # discard the saved cursor and restart the backfill
python purple_dot_sync.py --snapshot-all          # snapshot every waitlist state, not just active
```

## Tables

- `purple_dot_preorders`, `purple_dot_preorder_lines` — bookings
- `purple_dot_preorder_exports` — eventual export into real storefront orders
- `purple_dot_waitlists`, `purple_dot_waitlist_inventory` — waitlist
  inventory-allocation snapshots
- `purple_dot_sync_state`

## Notes

A booking and its eventual export are recorded at different times — the lag
between "booked" and "exported to the storefront order" can be substantial
for a pre-order, so don't expect a booking to have a matching export row
right away.

**Currency is stored as-is, with no FX conversion.** Never sum `total_price`
(or any money column) across rows without checking currency first if you
sell pre-orders in more than one currency.

Cancellations and refunds are common and material for pre-order bookings,
and they need to be filtered at the **line** grain, not the order grain — a
partially-cancelled order still has an order-level row that looks intact.

Non-merchandise lines (gift cards, protection/insurance upsells) are stored
as-is alongside real product lines, with no filtering — exclude them
yourself if you're computing merchandise-only revenue.

Deliberately **no PII is stored** — no email, name, or phone — matching the
same policy as `shopify_customers_sync.py`.

`purple_dot` (preorders) and `purple_dot_waitlists` are logged as two
separate `sync_log` entries, so one grain failing doesn't mask a working
sync of the other — check `last_sync_status` for both platform names.

## Tests

`tests/test_purple_dot_sync.py`
