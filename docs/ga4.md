# Google Analytics 4

Web analytics: daily channel-level funnel metrics, item-level product
views/sales, landing-page performance, and a new-vs-returning split per
Google Ads campaign.

**Script:** `ga4_sync.py` (standalone — not wired into `run_sync.py`)

## Setup

Service-account flow — server-to-server, no browser consent step:

1. Create or reuse a GCP service account and download its JSON key.
2. Enable the "Google Analytics Data API" for that project.
3. In GA4 Admin → Property Access Management, add the service account's
   email (`name@project.iam.gserviceaccount.com`) as a **Viewer**.
4. Fill in `.env`:

   | Variable | Notes |
   |---|---|
   | `GA4_PROPERTY_ID` | the GA4 property to pull |
   | `GA4_CREDENTIALS_FILE` | path to the service-account JSON key |

## Usage

```bash
python ga4_sync.py
```

Handles GA4's 100k-row response cap with daily chunking and pagination.

## Tables

- `ga_metrics` — daily channel-level funnel metrics
- `ga_products` — item-level product views/sales
- `ga_landing_pages` — landing-page performance
- `ga_campaign_ntb` — new-vs-returning split per Google Ads campaign

## Notes

The new-vs-returning split (`ga_campaign_ntb`) is a rough "is this campaign
acquiring new customers?" read, not a precise one — see the module docstring
for the cookie-scoping caveat before treating it as exact.

## Tests

`tests/test_ga4_sync.py`
