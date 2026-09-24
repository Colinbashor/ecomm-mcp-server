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
python search_console_sync.py --only query_pages    # just the query x page bridge, see Notes
python search_console_sync.py --only daily --only queries --only pages   # everything except query_pages
python search_console_sync.py --site sc-domain:other.example  # override SEARCH_CONSOLE_SITE for one run
python search_console_sync.py --data-state all      # include partial, still-settling days (not recommended)
```

| Flag | Default | Meaning |
|---|---|---|
| `--days N` | `5` | rolling window of N days **ending yesterday**; the overlap deliberately re-pulls recent days as Google finalizes them |
| `--start` / `--end` | — / yesterday | explicit `YYYY-MM-DD` window (`--start` overrides `--days`) |
| `--backfill` | off | start at the retention floor (~510 days, ~16 months back) instead |
| `--only GRAIN` | all four grains | repeatable; one of `daily`, `queries`, `pages`, `query_pages` |
| `--refresh` | off | ignore stored coverage and re-fetch every day in the window (default is to resume, skipping days already settled) |
| `--data-state` | `final` | `final` or `all`; `all` returns partial, never-corrected recent days (trap 5) |
| `--site` | `SEARCH_CONSOLE_SITE` | property to pull, for a one-off run against a different property |
| `--probe` | off | reachability + property check; writes nothing |

> **Heads-up — `query_pages` currently runs by default.** The module
> docstring (trap 8) and the grain's design describe `query_pages` as
> opt-in, but `main()` falls back to the full `GRAINS` tuple — which
> includes `query_pages` — whenever `--only` is omitted, so a plain
> `python search_console_sync.py` pulls it too. If you want the lighter
> daily job the docstring intends, pass the three other grains explicitly
> (`--only daily --only queries --only pages`, shown above).

If neither `SEARCH_CONSOLE_CREDENTIALS_FILE` nor `SEARCH_CONSOLE_SITE` is set
(or the key file doesn't exist), the script prints which one is missing and
exits cleanly without touching the database — safe to leave in a scheduled
job before the property is configured. A `--start` below the retention floor
prints a note but is harmless: out-of-range days return zero rows.

Run `--probe` first on a new property: it lists every site the service
account can see, confirms which one is configured, and prints the last 7
settled days of site totals, without writing anything.

## Tables

- `search_console_daily` — **authoritative** site totals per day per search
  type (web/image/video/news/discover). Use this as the denominator for
  anything computed off the tables below.
- `search_console_queries` — clicks/impressions/CTR/position per query per
  device per day. A **subset** of the site total — some queries are
  anonymized by Google and never appear here at any pull depth.
- `search_console_pages` — the same, per landing page per day.
  **Impressions here double-count and must never be summed to a site
  figure** — see the module docstring.
- `search_console_query_pages` — clicks/impressions/CTR/position per query
  **and** landing page per day (designed as opt-in, but see the heads-up
  under Usage — a run without `--only` currently includes it). The only
  grain that bridges a search term to a specific page, useful when your own
  product/page titles don't share vocabulary with what people actually
  search. Bigger and slower than the two grains above, and its impressions
  double-count in **both** directions (across a query's pages, and across a
  page's queries) — see the module docstring's trap (8).
- `search_console_coverage` — bookkeeping, one row per date x site x
  detail grain (`queries`/`pages`/`query_pages`): `rows_stored`,
  `data_state`, `was_settled` (1 = the day was already past Google's
  finalization lag when fetched), `fetched_at`. This is what makes a plain
  re-run resume: a day only counts as done when it was pulled `final` **and**
  settled. Not analytics data — don't join against it except to answer "did
  we pull this day yet."

## Notes

The module docstring documents eight traps worth reading before building on
this data — among them: the 5,000-row cap on query/page-grain results is
**per calendar day**, not per request, so days are pulled one at a time;
pagination must stop on an *empty* page, never a short one, since a capped
day legitimately returns exactly 5,000 rows; `dataState=all` banks partial,
never-corrected recent days (the default, `final`, is recommended);
retention is a **rolling window that slides forward daily**, with the loss
at the *old* end — a missed backfill window is gone permanently, the
opposite failure mode from most snapshot-style feeds in this repo; and the
optional `query_pages` grain is the only way to join a search term to a
landing page, at the cost of a noticeably bigger, double-counting-prone
table.

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
