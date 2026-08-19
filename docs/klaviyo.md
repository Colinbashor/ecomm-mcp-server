# Klaviyo

Email/SMS campaign performance, flow (automation) performance, monthly
audience/segment growth, and daily attributed revenue by channel and flow.

**Script:** `klaviyo_sync.py` (standalone — not wired into `run_sync.py`)

## Setup

Two auth modes — pick one:

**Private API key** (simpler):

1. Klaviyo Settings → API Keys → Create Private API Key, scoped to
   `campaigns:read`, `flows:read`, `metrics:read`, `segments:read`.
2. Set `KLAVIYO_API_KEY` in `.env`.

**OAuth** (if you'd rather not hand out a long-lived key):

1. Run the PKCE auth helper, which mirrors the other `*_auth.py` scripts:

   ```bash
   python klaviyo_auth.py PASTE_THE_CODE_HERE
   ```

   This writes `KLAVIYO_CLIENT_ID`, `KLAVIYO_CLIENT_SECRET`, and
   `KLAVIYO_REFRESH_TOKEN`. `klaviyo_sync.py` prefers OAuth automatically
   whenever all three are set.

Either way, also set:

| Variable | Notes |
|---|---|
| `KLAVIYO_CONVERSION_METRIC` | your account's conversion-event metric id (e.g. a "Placed Order" metric) — find it via `GET /api/metrics` or Klaviyo's Analytics → Metrics UI. No default; required for every report. |
| `KLAVIYO_API_REVISION` | optional, sensible default if blank |
| `KLAVIYO_CAMPAIGN_TIMEFRAME` / `KLAVIYO_CAMPAIGN_META_LOOKBACK_DAYS` | optional, sensible defaults if blank |
| `KLAVIYO_TIMEZONE` | optional, sensible default if blank |

## Usage

```bash
python klaviyo_sync.py
```

## Tables

- `klaviyo_campaigns` — email/SMS campaign performance
- `klaviyo_flows` — flow (automation) performance
- `klaviyo_audience_growth` — monthly audience/segment growth
- `klaviyo_attributed_daily` — daily attributed revenue by channel and flow

## Tests

`tests/test_klaviyo_sync.py`, `tests/test_klaviyo_auth.py`
