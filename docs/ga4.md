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
python ga4_sync.py                                  # last 30 days, all grains
python ga4_sync.py --days 7
python ga4_sync.py --start 2026-01-01 --end 2026-01-31
python ga4_sync.py --only metrics,landing_pages      # grains: metrics, products, landing_pages, campaign_ntb
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

A few other behaviors worth knowing about before you rely on this connector
in production:

- **Retries with backoff** on transient GA4 API errors — a single flaky
  call doesn't fail the whole run.
- **`conversions` → `keyEvents` metric rename**: GA4 renamed this metric
  server-side; the script probes for whichever name the property responds
  to, so you don't need to track which properties migrated.
- **Stale dimension values are deleted, not just overwritten**, before each
  day's re-insert — so a landing page or campaign that stops appearing in
  GA4 also stops appearing in `ga_landing_pages`/`ga_campaign_ntb`, rather
  than lingering with stale numbers.
- **Double check `GA4_PROPERTY_ID`.** A GA4 *account* ID and *property* ID
  look similar but are different values in the same Admin UI — pulling the
  account ID by mistake fails cleanly, but it's an easy mix-up worth
  confirming up front.

## Tests

`tests/test_ga4_sync.py`
