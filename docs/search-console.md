# Google Search Console (organic search)

Plain organic search performance — clicks, impressions, CTR, and average
position — at site, query, and landing-page grain, from
[Google Search Console](https://search.google.com/search-console/about).
This is the only source of organic (unpaid) search performance in this repo;
the paid-search connectors (Google Ads) can show a query's paid clicks next
to its organic clicks for the *same* query, but only for queries that also
ran a paid ad, and that view rejects money metrics entirely.

**Script:** `search_console_sync.py` (standalone — not wired into `run_sync.py`)

## Why this exists

Nothing else in this repo answers "what did people actually search to find
us, without us paying for the click." Search Console is Google's own
first-party answer to that question for your property.

## Setup

1. Use the same kind of service-account credential as [GA4](ga4.md) — you
   can reuse the same JSON key if you add the Search Console scope
   (`webmasters.readonly`) to it, or create a separate key. A dedicated
   `SEARCH_CONSOLE_CREDENTIALS_FILE` variable is used rather than silently
   reading `GA4_CREDENTIALS_FILE`, the same reasoning as Merchant Center
   getting its own `GMC_CREDENTIALS_FILE`.
2. You must **also** add the service account as a **user on the Search
   Console property itself**: Search Console → Settings → Users and
   permissions → Add user (Restricted permission is enough for read-only
   reporting). There is no API call for this — it's a one-time UI action,
   the same shape as Merchant Center's `registerGcp` step.
3. Find your property's exact identifier under Search Console → Settings —
   either a **domain property** (`sc-domain:example.com`, which already
   spans every subdomain/protocol variant as one property) or an older
   **URL-prefix property** (`https://www.example.com/`).
4. Fill in `.env`:

   | Variable | Notes |
   |---|---|
   | `SEARCH_CONSOLE_CREDENTIALS_FILE` | path to the service-account JSON key |
   | `SEARCH_CONSOLE_SITE` | your property string, exactly as Search Console shows it |

## Usage

```bash
python search_console_sync.py --probe              # reachability + property check, no writes
python search_console_sync.py                       # daily: rolling 5-day window
python search_console_sync.py --days 30
python search_console_sync.py --start 2026-01-01 --end 2026-01-31
python search_console_sync.py --backfill             # retention floor -> yesterday
python search_console_sync.py --backfill --refresh   # ignore stored coverage, re-pull all
python search_console_sync.py --only queries --only pages
```

`--only` accepts `daily`, `queries`, `pages` — repeatable. Run `--probe`
first on a new property: it lists every site the service account can see and
confirms which one is configured, without writing anything.

## Tables

- `search_console_daily` — **authoritative** site totals per day per search
  type (web/image/video/news/discover). Use this as the denominator for
  anything computed off the two tables below.
- `search_console_queries` — clicks/impressions/CTR/position per query per
  device per day. A **subset** of the site total — some queries are
  anonymized by Google and never appear here at any pull depth.
- `search_console_pages` — the same, per landing page per day.
  **Impressions here double-count and must never be summed to a site
  figure** — see the module docstring.

## Notes

The module docstring documents seven traps worth reading before building on
this data — among them: the 5,000-row cap on query/page-grain results is
**per calendar day**, not per request, so days are pulled one at a time;
pagination must stop on an *empty* page, never a short one, since a capped
day legitimately returns exactly 5,000 rows; `dataState=all` banks partial,
never-corrected recent days (the default, `final`, is recommended); and
retention is a **rolling window that slides forward daily**, with the loss
at the *old* end — a missed backfill window is gone permanently, the
opposite failure mode from most snapshot-style feeds in this repo.

`data_state` is stored on every row but deliberately **excluded from every
table's primary key**, so a later `final` pull overwrites an earlier
partial `all` row in place instead of both coexisting and double-counting
any SUM.

Crash safety: each day commits its rows together with its own coverage row
in the same transaction, so a killed run loses at most the day in flight. A
plain re-run resumes, skipping any day already pulled `final`; `--refresh`
forces a full re-fetch.

## Tests

`tests/test_search_console_sync.py`
