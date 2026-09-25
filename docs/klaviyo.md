# Klaviyo

Email campaign performance, flow (automation) performance across every send
channel, monthly segment growth, and daily attributed revenue by channel and
by flow.

**Script:** `klaviyo_sync.py` (standalone — not wired into `run_sync.py`).
It owns its four tables via `ensure_schema()` (`CREATE TABLE IF NOT EXISTS`),
so it's safe to run against a brand-new `warehouse.db`.

## Setup

Two auth modes — pick one. If both are configured, OAuth wins.

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
credentials land. Leave an empty gating variable with no inline `#` comment
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

Each section logs to `sync_log` under its own platform name:
`klaviyo_campaigns`, `klaviyo_flows`, `klaviyo_audience`, and two for
`attributed` (`klaviyo_attr_channel` and `klaviyo_attr_flow`). If one fails,
the others still run.

## Tables

| Table | Primary key | Window | What's in it |
|---|---|---|---|
| `klaviyo_campaigns` | `campaign_id` | campaign timeframe (default last 30 days) | **Email campaigns only.** The report filters on `send_channel = 'email'`, so SMS campaigns aren't stored. Recipients, delivered, unique opens and clicks, conversions, revenue, unsubscribes, bounces, spam complaints, and derived rates (open, click, conversion, click-to-open, revenue per recipient, AOV). |
| `klaviyo_flows` | `(flow_id, channel, month_start)` | prior calendar month and current month to date | Flow performance per flow **per send channel** (email, SMS, etc.), with the same metric set as campaigns minus spam complaints, plus `trigger_type`. |
| `klaviyo_audience_growth` | `(audience_id, month_start)` | prior calendar month and current month to date | Monthly **segment** membership: total members, members added, members removed, net change. `audience_type` is always `segment`; lists aren't pulled. All-zero months (before a segment existed) are skipped. |
| `klaviyo_attributed_daily` | `(date, dimension_type, dimension_id)` | last `--days` (default 35) | Daily Klaviyo-attributed conversions, unique conversions, and revenue for the conversion metric. `dimension_type` is `channel` (e.g. `email`, `sms`, `unattributed`) or `flow` (flow id, where an empty id is named `campaign/unattributed`). Days with no activity aren't stored. |

Every row carries `conversion_metric_id`, so changing
`KLAVIYO_CONVERSION_METRIC` later doesn't silently mix metrics. All writes are
`INSERT OR REPLACE` on the primary key, so re-running is idempotent.

Rates (`open_rate`, `click_rate`, `conversion_rate`) are **fractions from 0
to 1** of `delivered`, not percentages. `click_to_open_rate` is unique clicks
divided by unique opens, and `average_order_value` is revenue per conversion.
All of them are recomputed from the summed counts after the message/variant
roll-up, not averaged across rows. When you aggregate across campaigns or
months yourself, do the same: sum the counts, then divide.

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
- A 429 honours `Retry-After`. 5xx responses and dropped connections are
  retried with exponential backoff, up to 6 tries. Any other 4xx fails
  immediately with the response body, since that's almost always a
  request-shape problem.
- Klaviyo's API paths and versions have changed before (e.g. `-report` vs
  `-reports`, or a `revision` bump). A 404 or "unknown attribute" error on a
  previously working endpoint usually means the integration is out of date.
  Pin `KLAVIYO_API_REVISION` or check Klaviyo's current docs.

## Tests

`tests/test_klaviyo_sync.py`, `tests/test_klaviyo_auth.py`
