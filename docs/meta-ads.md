# Meta (Facebook / Instagram) Ads

Campaign-level spend/clicks/conversions. Core connector only — no standalone
extras exist for Meta yet.

**Script:** `warehouse/connectors/meta_ads.py`, via `run_sync.py --only meta`

## Setup

Fill in `.env`:

| Variable | Notes |
|---|---|
| `META_ACCESS_TOKEN` | a long-lived system-user token |
| `META_AD_ACCOUNT_ID` | looks like `act_1234567890` |

No interactive auth helper — generate the long-lived token directly in
Meta Business Settings.

## Usage

```bash
python run_sync.py --only meta   # last 7 days
```

## Tables

- `ad_metrics` (core, shared across platforms — see the main [README](../README.md#mcp-tools))
