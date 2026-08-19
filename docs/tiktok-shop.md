# TikTok Shop

Order ground truth (core), plus four standalone scripts for video
performance, LIVE-shopping, creator identity, and sales-source attribution.

## Scripts

| Script | Type | Covers |
|---|---|---|
| `warehouse/connectors/tiktok_shop.py` | core, via `run_sync.py --only tiktok` | orders into `orders` |
| `tiktok_videos_sync.py` | standalone | video performance |
| `tiktok_live_sync.py` | standalone | LIVE-shopping broadcast + product funnel |
| `tiktok_creators_sync.py` | standalone | handle ↔ display-name ↔ user-id creator/affiliate identity bridge |
| `tiktok_analytics_sync.py` | standalone | true mutually-exclusive LIVE/VIDEO/PRODUCT_CARD sales-source split |

## Setup

1. Authorize a Partner Center app with **all scopes at once** — a partial
   re-auth silently replaces the grant and orders fail with error 105005 about
   a week later.
2. Run the auth helper:

   ```bash
   python tiktok_auth.py PASTE_THE_CODE_HERE   # code from Partner Center
   ```

   This fills tokens plus the shop cipher/id for you.
3. Fill in the rest of `.env`:

   | Variable | Notes |
   |---|---|
   | `TIKTOK_APP_KEY` / `TIKTOK_APP_SECRET` | from the Partner Center app |
   | `TIKTOK_ACCESS_TOKEN` / `TIKTOK_REFRESH_TOKEN` / `TIKTOK_SHOP_CIPHER` / `TIKTOK_SHOP_ID` | written by `tiktok_auth.py` |
   | `TIKTOK_LIVE_OWN_ACCOUNT_TYPE` | which `tiktok_shop_lives.account_type` counts as your own broadcasts vs. affiliate/creator or paid-marketing lives (default `OFFICIAL_ACCOUNTS`) |
   | `TIKTOK_SHOP_TIMEZONE` | IANA timezone for bucketing LIVE broadcasts into calendar days (default `UTC`) |

All four extras reuse the core `TIKTOK_*` credentials — nothing new to
configure.

## Usage

```bash
python run_sync.py --only tiktok      # core orders, last 7 days
python tiktok_videos_sync.py
python tiktok_live_sync.py
python tiktok_creators_sync.py
python tiktok_analytics_sync.py
```

## Tables

- `orders` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
- `tiktok_shop_videos`
- `tiktok_shop_lives`, `tiktok_shop_live_products`
- `tiktok_creators`
- `tiktok_shop_performance`

## Notes

`tiktok_creators_sync.py` exists because TikTok's video API and order API
expose different halves of a creator's identity with no shared join key — the
bridge closes that gap via the API plus an optional manual CSV import.

`tiktok_shop_performance` (from `tiktok_analytics_sync.py`) is a cleaner
alternative to estimating "unattributed" sales by subtraction.

## Tests

`tests/test_tiktok_videos_sync.py`, `tests/test_tiktok_live_sync.py`,
`tests/test_tiktok_creators_sync.py`, `tests/test_tiktok_analytics_sync.py`
