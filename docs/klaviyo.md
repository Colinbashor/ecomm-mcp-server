# Klaviyo

Email campaign performance, flow (automation) performance across every send
channel, monthly segment growth, and daily attributed revenue by channel and
by flow.

**Script:** `klaviyo_sync.py` (standalone — not wired into `run_sync.py`).
It owns its four tables via `ensure_schema()` (`CREATE TABLE IF NOT EXISTS`),
so it's safe to run against a brand-new `warehouse.db`.

## Setup

Two auth modes — pick one. If both are configured, OAuth wins. OAuth only
counts as configured when **all three** of `KLAVIYO_CLIENT_ID`,
`KLAVIYO_CLIENT_SECRET`, and `KLAVIYO_REFRESH_TOKEN` are set. With only some
of them set, the script falls back to `KLAVIYO_API_KEY`, or skips if that's
unset too.

**Private API key** (simpler):

1. Klaviyo Settings → API Keys → Create Private API Key, scoped to
   `campaigns:read`, `flows:read`, `metrics:read`, `segments:read`.
2. Set `KLAVIYO_API_KEY` in `.env` (it starts with `pk_`).

**OAuth** (if you'd rather not hand out a long-lived key):

1. Create a Klaviyo app and put its Client ID and Secret in `.env` as
   `KLAVIYO_CLIENT_ID` / `KLAVIYO_CLIENT_SECRET`. The helper below reads
   these; it does not write them.
2. In the app settings, add an Allowed Redirect URI that exactly matches
   `KLAVIYO_REDIRECT_URI` (default `https://localhost`, no trailing slash;
   a trailing slash counts as a different URI). Enable the scopes the helper
   asks for: `accounts:read campaigns:read flows:read metrics:read
   segments:read`. Set `KLAVIYO_OAUTH_SCOPES` to request a different set.
3. Run the PKCE auth helper in two steps. This works the same way as the
   other `*_auth.py` scripts:

   ```bash
   python klaviyo_auth.py --url                     # prints a consent URL, stashes the PKCE verifier
   python klaviyo_auth.py PASTE_THE_CODE_HERE        # exchanges the ?code= from the redirect's address bar
   python klaviyo_auth.py --refresh                 # optional sanity check: mints one access token
   ```

   The browser lands on a dead `https://localhost` page after consent. That's
   expected; just copy the `?code=` value from the address bar. The `--url`
   step isn't optional: it writes the PKCE verifier to `.env` as
   `KLAVIYO_OAUTH_VERIFIER`, and the exchange step needs it. Running the
   exchange without `--url` first raises a `KlaviyoAuthError`. The exchange
   saves `KLAVIYO_REFRESH_TOKEN` and clears the verifier.
4. `klaviyo_sync.py` then mints a fresh ~1-hour Bearer token from the refresh
   token on every run. If Klaviyo returns a new refresh token, the script
   writes it back to `.env` for you.

With neither credential set, `klaviyo_sync.py` prints a `SKIPPED` line and
exits 0 without logging an error, so a scheduled job stays green until
credentials land. (The script exits 0 after a section *fails*, too — see
[Failures and exit codes](#failures-and-exit-codes).) Leave an empty gating variable with no inline `#` comment
on its line, since python-dotenv can read the comment as the value.

Either way, also set:

| Variable | Default | Notes |
|---|---|---|
| `KLAVIYO_CONVERSION_METRIC` | *(none)* | Your account's conversion-event metric id (e.g. a "Placed Order" metric). Find it via `GET /api/metrics` or Klaviyo's Analytics → Metrics UI. **Required:** every report needs it, and without it the script prints `SKIPPED` and exits 0. |
| `KLAVIYO_API_REVISION` | `2025-07-15` | Dated `revision` header. Pin a known-good one if a Klaviyo change breaks a report. |
| `KLAVIYO_CAMPAIGN_TIMEFRAME` | `last_30_days` | Klaviyo timeframe key for the campaign report (e.g. `last_90_days`). `--campaign-timeframe` overrides it per run. |
| `KLAVIYO_CAMPAIGN_META_LOOKBACK_DAYS` | `120` | How far back to list campaigns for name/status/send_time. Campaign ids the report returns that fall outside this window get looked up one at a time. |
| `KLAVIYO_TIMEZONE` | `UTC` | IANA timezone used to bucket `klaviyo_attributed_daily` days and convert campaign `send_time`. |

## Usage

```bash
python klaviyo_sync.py                                    # all four sections
python klaviyo_sync.py --only campaigns,flows             # a subset
python klaviyo_sync.py --campaign-timeframe last_90_days  # wider campaign window, this run only
python klaviyo_sync.py --only attributed --days 90        # longer attributed-revenue lookback
```

| Flag | Default | Effect |
|---|---|---|
| `--only` | all | Comma list of sections: `campaigns`, `flows`, `audience`, `attributed`. |
| `--campaign-timeframe` | `KLAVIYO_CAMPAIGN_TIMEFRAME` | Overrides the campaign report's timeframe key for this run. |
| `--days` | `35` | Lookback for the **attributed** section only, capped at 365. It has no effect on campaigns, flows, or audience. |

`--only` doesn't validate names. A typo (`--only campaign`) runs zero
sections and exits 0 without an error. `--days` has no lower bound, only the
365 cap.

### Failures and exit codes

Each section logs to `sync_log` under its own platform name:
`klaviyo_campaigns`, `klaviyo_flows`, `klaviyo_audience`, and two for
`attributed` (`klaviyo_attr_channel` and `klaviyo_attr_flow`). An exception
*inside* a section logs that section `error`, and the others still run.

Two caveats:

- **The script always exits 0**, even when a section logged `error`. A
  scheduler watching exit codes won't notice a failure. Check `sync_log` (or
  the `last_sync_status` MCP tool) instead.
- Some calls happen **before** the per-section loop, and a failure there
  crashes the whole run with a traceback and writes **no** `sync_log` rows:
  - opening/initialising the database;
  - building the HTTP session, which for OAuth means refreshing the access
    token (an expired or revoked refresh token fails here);
  - when `attributed` is selected, listing `/flows/` for flow names, which is
    shared by both attributed sub-sections. A failed flow listing aborts
    campaigns, flows, and audience as well.

## Tables

| Table | Primary key | Window | What's in it |
|---|---|---|---|
| `klaviyo_campaigns` | `campaign_id` | campaign timeframe (default last 30 days) | **Email campaigns only.** The report filters on `send_channel = 'email'`, so SMS campaigns aren't stored. Recipients, delivered, unique opens and clicks, conversions, revenue, unsubscribes, bounces, spam complaints, and derived rates (open, click, conversion, click-to-open, revenue per recipient, AOV). |
| `klaviyo_flows` | `(flow_id, channel, month_start)` | prior calendar month and current month to date | Flow performance per flow **per send channel** (email, SMS, etc.), with the same metric set as campaigns minus spam complaints, plus `trigger_type`. |
| `klaviyo_audience_growth` | `(audience_id, month_start)` | prior calendar month and current month to date | Monthly **segment** membership: total members, members added, members removed, net change. `audience_type` is always `segment`; lists aren't pulled. All-zero months (before a segment existed) are skipped. |
| `klaviyo_attributed_daily` | `(date, dimension_type, dimension_id)` | last `--days` (default 35) | Daily Klaviyo-attributed conversions, unique conversions, and revenue for the conversion metric. `dimension_type` is `channel` or `flow` — see [the dimension columns](#klaviyo_attributed_daily-dimension-columns) below. Day/dimension cells with no activity aren't stored. |

### Columns

- `klaviyo_campaigns`: `campaign_id`, `name`, `channel`, `status`,
  `send_time` (converted to `KLAVIYO_TIMEZONE`), `recipients`, `delivered`,
  `opens_unique`, `clicks_unique`, `conversions`, `conversion_uniques`,
  `revenue`, `unsubscribes`, `bounced`, `spam_complaints`, `open_rate`,
  `click_rate`, `conversion_rate`, `revenue_per_recipient`,
  `average_order_value`, `click_to_open_rate`, `conversion_metric_id`,
  `as_of`, `synced_at`.
- `klaviyo_flows`: `flow_id`, `name`, `channel`, `trigger_type`,
  `month_start`, then the same metric/rate columns as campaigns minus
  `spam_complaints`, plus `conversion_metric_id`, `as_of`, `synced_at`.
- `klaviyo_audience_growth`: `audience_id`, `audience_type`, `name`,
  `month_start`, `total_members`, `members_added`, `members_removed`,
  `net_members_changed`, `as_of`, `synced_at`. There's no
  `conversion_metric_id` here, since membership has nothing to do with the
  conversion metric.
- `klaviyo_attributed_daily`: `date`, `dimension_type`, `dimension_id`,
  `dimension_name`, `conversions`, `conversion_uniques`, `revenue`,
  `conversion_metric_id`, `synced_at` (no `as_of`).

### `klaviyo_attributed_daily` dimension columns

`dimension_id` holds Klaviyo's **raw** grouping value. Only `dimension_name`
holds the friendly label:

| `dimension_type` | `dimension_id` | `dimension_name` |
|---|---|---|
| `channel` | `$email_channel`, `$sms_channel`, … | `email`, `sms`, … (`$` and `_channel` stripped) |
| `channel` | `''` (unattributed) | `unattributed` |
| `flow` | the flow id | the flow's name, or `NULL` if the id isn't in the `/flows/` listing (e.g. a deleted flow) |
| `flow` | `''` (not from a flow) | `campaign/unattributed` |

So filter on `dimension_name = 'email'` (or `dimension_id = '$email_channel'`).
`WHERE dimension_id = 'email'` matches nothing.

### Semantics

Every row carries `conversion_metric_id`, so changing
`KLAVIYO_CONVERSION_METRIC` later doesn't silently mix metrics. All writes are
`INSERT OR REPLACE` on the primary key, so re-running is idempotent.

Rates (`open_rate`, `click_rate`, `conversion_rate`) are **fractions from 0
to 1** of `delivered`, not percentages. `click_to_open_rate` is unique clicks
divided by unique opens, and `average_order_value` is revenue per conversion.
`revenue_per_recipient` divides by `recipients`, not `delivered`.
`conversion_rate` uses `conversion_uniques`, but `average_order_value` divides
by `conversions` (not uniques). Any rate whose denominator is 0 is stored as
`0.0`, not `NULL`. All of them are recomputed from the summed counts after
the message/variant roll-up, not averaged across rows. When you aggregate across campaigns or
months yourself, do the same: sum the counts, then divide.

**Snapshots, never pruned.** Rows are upserted and nothing is deleted:
- A campaign that drops out of the report timeframe keeps its last-synced
  numbers. Use `as_of` to see how fresh a row is.
- A month's `klaviyo_flows` / `klaviyo_audience_growth` rows stop updating
  once that month is older than the prior calendar month.

**NULLs.**
- Campaign `name`/`status`/`send_time` are best-effort. If the one-off
  metadata lookup for a campaign fails, they're stored as `NULL` and the
  metrics are still written.
- Individual `klaviyo_audience_growth` stat columns can be `NULL` when Klaviyo
  omits that series.
- The "skip all-zero months" rule also drops a real segment's month that
  genuinely had zero members and zero change.

**Time zones.**
- `KLAVIYO_TIMEZONE` sets the daily buckets in `klaviyo_attributed_daily` and
  converts campaign `send_time`.
- The attributed window *starts* at midnight **UTC** `--days` back, so in a
  non-UTC zone the first local day can be partial.
- Month boundaries for flows and audience (`month_start`) are always UTC.

## Notes

Klaviyo's API has several edges that change over time. `klaviyo_sync.py`
works around the ones below. Check them first if a report comes back empty
or you're debugging a 4xx:

- The flow-values report is grouped by `flow_id`, `flow_message_id`, and
  `send_channel`. Klaviyo has required `flow_message_id` alongside `flow_id`,
  and omitting it gets a 400. Message and variant rows are rolled up to
  `(flow_id, send_channel)` before storing.
- Campaign report rows can also split across message or variant grain, for
  example in an A/B test. They're summed up to one row per `campaign_id`.
- The campaigns *list* endpoint requires a `messages.channel` filter, which is
  why metadata lookup (and the report) is email-scoped.
- Segment-series queries need **timezone-aware** datetimes. Klaviyo rejects a
  naive datetime rather than assuming UTC.
- `metric-aggregates` can group by `$attributed_channel` / `$attributed_flow`
  but not by product, so this endpoint can't give per-SKU attributed revenue.
- Klaviyo caps report timeframes at about 1 year per request, and a wider
  window returns a 400 rather than being truncated. That's why `--days` is
  capped at 365.
- Every request has a 60s timeout and up to 6 tries in total. A 429 honours
  `Retry-After` (or waits `2^attempt` seconds without it), capped at 60s, and
  429s use up tries too. 5xx responses and dropped connections back off
  `2^attempt` seconds, capped at 30s. Running out of tries raises
  `exhausted retries`. Any other 4xx fails
  immediately with the response body, since that's almost always a
  request-shape problem.
- Klaviyo's API paths and versions have changed before (e.g. `-report` vs
  `-reports`, or a `revision` bump). A 404 or "unknown attribute" error on a
  previously working endpoint usually means the integration is out of date.
  Pin `KLAVIYO_API_REVISION` or check Klaviyo's current docs.

## Tests

- `tests/test_klaviyo_sync.py` covers: schema creation, rate and derived-value
  maths (including zero denominators), channel naming, the campaign
  message-row roll-up and its idempotent upsert, attributed-dimension loading
  (empty cells skipped, flow names, the unattributed bucket, the 365-day cap),
  auth-mode precedence (including partial OAuth config falling back), retry
  behaviour (connection errors, 429 `Retry-After`, 5xx, hard 4xx), and the
  clean skips when there are no credentials or no conversion metric.
- `tests/test_klaviyo_auth.py` covers: the PKCE verifier and S256 challenge,
  redirect-URI and scope defaults and overrides, and required-variable
  errors.
