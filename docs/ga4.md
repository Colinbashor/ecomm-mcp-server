# Google Analytics 4

Web analytics: daily channel-level funnel metrics, item-level product
views/sales, landing-page performance (per-URL and bucketed into page types),
a Meta paid/organic traffic split on those page-type buckets and on
individual collection pages, and a new-vs-returning split per Google Ads
campaign.

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
python ga4_sync.py --only metrics,landing_pages      # grains: metrics, products, landing_pages,
                                                      #   landing_buckets, landing_bucket_meta,
                                                      #   collection_meta, campaign_ntb
```

Handles GA4's 100k-row response cap with daily chunking and pagination.

Rough API cost per grain, so you can pick a cheaper `--only` set:
`products` is the expensive one (pulled in daily chunks). `metrics`,
`landing_pages`, and `campaign_ntb` each make about one request per calendar
month in the range. The three landing-bucket grains are also chunked by
month, with more requests per month:

| Grain | Requests per month chunk | Why |
|---|---|---|
| `landing_buckets` | 1 + number of buckets (5 with the default four buckets) | one site-total request, plus one filtered request per bucket |
| `landing_bucket_meta` | 3 × number of buckets (12 by default) | per bucket: total, Meta paid, Meta organic |
| `collection_meta` | 3 | total, Meta paid, and Meta organic, each grouped by collection URL; rows scale with your number of collections |

## Tables

- `ga_metrics` — daily channel-level funnel metrics
- `ga_products` — item-level product views/sales
- `ga_landing_pages` — landing-page performance, per URL
- `ga_landing_buckets` — the same traffic collapsed into coarse page types
  (product/collection/content/home/other)
- `ga_landing_bucket_meta` — `ga_landing_buckets`, further split into Meta
  paid/organic/other traffic
- `ga_collection_meta` — per-collection-URL performance x the same Meta
  paid/organic/other split
- `ga_campaign_ntb` — new-vs-returning split per Google Ads campaign

The three landing-bucket tables share the same metric columns: `sessions`,
`engaged_sessions`, `conversions` (from whichever of `keyEvents` or
`conversions` the property supports; see Notes), `purchases` (GA4
`transactions`), and `revenue` (GA4 `totalRevenue`), plus `property_id`,
`date` (ISO `YYYY-MM-DD`), and `synced_at`. Their keys are:

| Table | Primary key | Dimension values |
|---|---|---|
| `ga_landing_buckets` | `property_id, date, bucket` | each `LANDING_BUCKETS` label, plus `Other / uncategorised` |
| `ga_landing_bucket_meta` | `property_id, date, bucket, meta_class` | the named buckets only (**no** `Other / uncategorised` row) × `meta_paid` / `meta_organic` / `other` |
| `ga_collection_meta` | `property_id, date, landing_page, meta_class` | every `/collections/…` landing page × `meta_paid` / `meta_organic` / `other` |

## Notes

The new-vs-returning split (`ga_campaign_ntb`) is a rough "is this campaign
acquiring new customers?" read, not a precise one — see the module docstring
for the cookie-scoping caveat before treating it as exact.

`ga_landing_buckets` groups landing-page URLs into `LANDING_BUCKETS` (a
constant you edit for your own site's URL structure — the shipped default
assumes `/products/`, `/collections/`, `/pages/` and `/` conventions common
to Shopify-style storefronts). Each bucket is its own filtered request rather
than a `GROUP BY`, so it returns an exact per-bucket total instead of letting
GA4's own `(other)` long-tail rollup absorb low-traffic URLs; an "Other /
uncategorised" row is derived (whole-property total minus every named
bucket) so the buckets always reconcile to the same-day site total.

`ga_landing_bucket_meta` and `ga_collection_meta` split that same traffic
into `meta_paid` / `meta_organic` / `other` using two editable constants,
`META_SOURCES` (an allowlist of exact `sessionSource` values GA4 reports for
Facebook/Instagram traffic) and `PAID_MEDIUM_MARKERS` (a `sessionMedium`
contains-check for anything that looks like a paid placement). Both are
necessarily heuristic — advertiser UTM tagging is inconsistent in practice —
so treat the split as directional, not reconciled to what your ad platform
itself reports for spend/revenue. `ga_collection_meta` scopes to
`/collections/`-prefixed URLs only, so its per-URL cardinality stays bounded
to your own collection count rather than the whole site.

Before you edit those constants or rely on the totals, note these details:

- **The "Other" rows are clamped at zero.** The derived `Other /
  uncategorised` bucket and each `other` meta class are computed as a total
  minus the named slices, floored at 0. They reconcile exactly as long as
  the slices don't add up to more than the total. If GA4 ever returns
  filtered numbers larger than the unfiltered one, the rows can sum to
  slightly more than the site total.
- **Buckets must not overlap.** Each bucket is its own request, so the order
  of `LANDING_BUCKETS` doesn't matter. The downside: if two entries match
  the same URLs (for example `/collections/` and `/collections/sale`), that
  traffic is counted in both buckets, and the clamp hides it by shrinking
  `Other / uncategorised`.
- **Match kinds are `begins` and `exact`.** The default home-page bucket is
  an exact match on `/`, so it catches only the bare root.
- **`ga_collection_meta` ignores `LANDING_BUCKETS`.** Its `/collections/`
  prefix is hardcoded in `sync_collection_meta()`. If your storefront uses a
  different collection path and you change the bucket, change it there too.
  Unlike `ga_landing_pages`, it has no `LANDING_PAGE_MIN_SESSIONS` floor:
  every collection URL with traffic that day gets three rows.
- **Rows exist only for days with traffic.** A bucket (or collection page)
  with no sessions on a day gets no rows for that day. `Other /
  uncategorised` in `ga_landing_buckets` is written for every day the
  property had traffic, even when its value is 0.

Example: Meta paid vs. organic conversion rate by page type over the last
30 days:

```sql
SELECT bucket, meta_class,
       SUM(sessions) AS sessions,
       ROUND(1.0 * SUM(purchases) / NULLIF(SUM(sessions), 0), 4) AS purchase_rate,
       SUM(revenue) AS revenue
FROM ga_landing_bucket_meta
WHERE date >= date('now', '-30 day')
GROUP BY bucket, meta_class
ORDER BY bucket, meta_class;
```

A few other behaviors worth knowing about before you rely on this connector
in production:

- **Retries with backoff** on transient GA4 API errors — a single flaky
  call doesn't fail the whole run.
- **`conversions` → `keyEvents` metric rename**: GA4 renamed this metric
  server-side; the script probes for whichever name the property responds
  to, so you don't need to track which properties migrated.
- **Stale dimension values are deleted, not just overwritten**, before each
  day's re-insert — so a landing page or campaign that stops appearing in
  GA4 also stops appearing in `ga_landing_pages`/`ga_campaign_ntb` (and
  the three landing-bucket tables, which use the same purge), rather than
  lingering with stale numbers.
- **Double check `GA4_PROPERTY_ID`.** A GA4 *account* ID and *property* ID
  look similar but are different values in the same Admin UI — pulling the
  account ID by mistake fails cleanly, but it's an easy mix-up worth
  confirming up front.

## Tests

`tests/test_ga4_sync.py`
