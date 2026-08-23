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

1. Run the PKCE auth helper in two steps, which mirrors the other
   `*_auth.py` scripts:

   ```bash
   python klaviyo_auth.py --url                     # prints a consent URL, stashes the PKCE verifier
   python klaviyo_auth.py PASTE_THE_CODE_HERE        # exchanges the code it redirects you to
   ```

   The `--url` step isn't optional — it's what stashes the PKCE verifier the
   exchange step needs; running the exchange without it first raises a
   `KlaviyoAuthError`. Together they write `KLAVIYO_CLIENT_ID`,
   `KLAVIYO_CLIENT_SECRET`, and `KLAVIYO_REFRESH_TOKEN`. `klaviyo_sync.py`
   prefers OAuth automatically whenever all three are set.

Either way, also set:

| Variable | Notes |
|---|---|
| `KLAVIYO_CONVERSION_METRIC` | your account's conversion-event metric id (e.g. a "Placed Order" metric) — find it via `GET /api/metrics` or Klaviyo's Analytics → Metrics UI. No default; required for every report. |
| `KLAVIYO_API_REVISION` | optional, sensible default if blank |
| `KLAVIYO_CAMPAIGN_TIMEFRAME` / `KLAVIYO_CAMPAIGN_META_LOOKBACK_DAYS` | optional, sensible defaults if blank |
| `KLAVIYO_TIMEZONE` | optional, sensible default if blank |

## Usage

```bash
python klaviyo_sync.py                                    # all reports, default lookback
python klaviyo_sync.py --days 30
python klaviyo_sync.py --only campaigns,flows
python klaviyo_sync.py --campaign-timeframe last_30_days
```

## Tables

- `klaviyo_campaigns` — email/SMS campaign performance
- `klaviyo_flows` — flow (automation) performance
- `klaviyo_audience_growth` — monthly audience/segment growth
- `klaviyo_attributed_daily` — daily attributed revenue by channel and flow

## Notes

Klaviyo's API has a handful of drift-prone edges that `klaviyo_sync.py`
works around — worth knowing about if a report comes back empty or you're
debugging a 4xx:

- The flow-values report requires grouping by `flow_message_id` — omit it
  and the request is rejected.
- Segment-series queries require **timezone-aware** datetimes; a naive
  datetime is rejected rather than silently assumed UTC.
- `metric-aggregates` can't group by product — don't expect per-SKU
  breakdowns from that endpoint.
- Klaviyo enforces roughly a 1-year cap on report timeframes.
- Campaign and flow rows are split across message/variant grain, not one
  row per campaign/flow — expect to aggregate before reporting a single
  number per campaign.
- 429s and 5xx responses are retried with backoff automatically.
- Klaviyo's API paths and versions have churned before; `KLAVIYO_API_REVISION`
  lets you pin a known-good version if a future change breaks a report.

## Tests

`tests/test_klaviyo_sync.py`, `tests/test_klaviyo_auth.py`
