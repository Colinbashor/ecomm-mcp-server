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
python purple_dot_sync.py
```

## Tables

- `purple_dot_preorders`, `purple_dot_preorder_lines` — bookings
- `purple_dot_preorder_exports` — eventual export into real storefront orders
- `purple_dot_waitlists`, `purple_dot_waitlist_inventory` — waitlist
  inventory-allocation snapshots
- `purple_dot_sync_state`

## Tests

`tests/test_purple_dot_sync.py`
