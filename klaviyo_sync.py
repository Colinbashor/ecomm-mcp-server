r"""
Klaviyo (email / SMS marketing) -> warehouse sync.

Standalone script, like the platform connectors under `warehouse/connectors/`,
but not wired into `run_sync.py`'s generic ads/orders loop: Klaviyo doesn't fill
either shared table, so it defines and owns four tables of its own via
`ensure_schema()`. Safe to run against a fresh `warehouse.db` — it creates what
it needs and reuses `warehouse.db`'s connection/log_sync helpers for everything
else.

  klaviyo_campaigns        — recent email (+ SMS, if you send it) campaign
                              performance. PK campaign_id.
  klaviyo_flows             — automation ("flow") performance for the current
                              and prior calendar month, grouped by
                              flow_id + send_channel. PK (flow_id, channel,
                              month_start).
  klaviyo_audience_growth   — monthly segment membership series (current +
                              prior month). PK (audience_id, month_start).
  klaviyo_attributed_daily  — DAILY Klaviyo-attributed conversions + revenue,
                              split by attribution dimension (channel or flow).
                              PK (date, dimension_type, dimension_id).

Each of the (up to) five sections below (campaigns / flows / audience /
attributed-by-channel / attributed-by-flow) logs to `sync_log` under its own
platform name, so one section failing doesn't take the others down with it —
the same "let one grain fail" convention used across this project's
connectors.

CREDENTIALS
  KLAVIYO_API_KEY   a Klaviyo PRIVATE API key (starts with `pk_`). Create one
                     in Klaviyo under Settings > API Keys > Create Private API
                     Key, with (at least) the campaigns:read, flows:read,
                     metrics:read and segments:read scopes.
  With that unset, the script prints a SKIPPED line and exits 0 rather than
  raising — nothing is logged as an error, so a scheduled job stays green
  until credentials land. Per this project's convention, an *empty* gating env
  var must have no inline `#` comment on its line — python-dotenv can read the
  comment text as the value and defeat the "unset" check.

  Klaviyo also supports an OAuth (authorization-code) grant, useful if you're
  building a multi-tenant integration rather than a single store's private
  key. `klaviyo_auth.py` runs that one-time PKCE consent flow and saves
  KLAVIYO_CLIENT_ID / KLAVIYO_CLIENT_SECRET / KLAVIYO_REFRESH_TOKEN to .env.
  `_auth_mode()` resolves which credential set is configured — OAuth if all
  three of those are present, else the private key — and `_session()` mints a
  fresh Bearer access token from the refresh token whenever OAuth is active.
  Both paths hit the same read-only scopes; pick whichever suits your setup,
  or leave OAuth unconfigured and the private key keeps working unchanged.

  KLAVIYO_CONVERSION_METRIC   the Klaviyo metric id used as "the" conversion
                     event for value reporting (usually whatever you treat as
                     a completed order/purchase, e.g. a "Placed Order" metric
                     synced from your storefront). THERE IS NO SENSIBLE
                     DEFAULT: metric ids are per-account. Find yours with
                     `GET /api/metrics` (or Klaviyo's Analytics > Metrics UI)
                     and match on the metric's `name`. Without this set, the
                     script skips cleanly with a message telling you so —
                     campaign/flow value reports and the attributed-revenue
                     pull all require a conversion metric id, so there's
                     nothing useful to do without it.

Optional .env overrides (sensible defaults):
  KLAVIYO_API_REVISION           dated API revision header (see Klaviyo's API
                                 versioning docs; default below)
  KLAVIYO_CAMPAIGN_TIMEFRAME     report window key for the nightly campaign
                                 pull, e.g. last_30_days / last_90_days
  KLAVIYO_CAMPAIGN_META_LOOKBACK_DAYS  how far back to look up campaign
                                 metadata (name/status/send_time) so every
                                 campaign id the values report returns has a
                                 match (default 120)
  KLAVIYO_TIMEZONE               IANA timezone Klaviyo should bucket "daily"
                                 attributed-revenue rows in (default UTC)

API GOTCHAS WORTH KNOWING BEFORE YOU DEBUG A 400 (Klaviyo's own docs are the
source of truth — its REST contract has drifted over time in ways an old
blog post or Stack Overflow answer won't reflect):
  * The flow-values-report endpoint has, in the past, required an EXPLICIT
    `flow_message_id` alongside `flow_id` in `group_by` — omit it and the
    request 400s. Verify the currently-required group_by fields against
    Klaviyo's reporting API docs rather than trusting an older example; if
    Klaviyo changes the requirement again this is the first place to look.
  * Some report endpoints have moved paths/versions across Klaviyo API
    releases (e.g. a `-report`/`-reports` pluralization, or a version bump in
    the required `revision` header). A 404 or "unknown attribute" error on an
    endpoint that used to work is more likely a stale integration than a
    broken account — check the current docs for the exact path/shape first.
  * Reporting timeframes are capped (at the time of writing, 1 year per
    request) — a too-wide custom timeframe 400s rather than truncating
    silently. This script only ever requests short recent windows, so it
    isn't a practical concern here, but it matters if you extend it into a
    historical backfill.
  * The segment-series report requires TIMEZONE-AWARE datetimes in its
    timeframe — a naive datetime is rejected.
  * `metric-aggregates` can group revenue by attribution dimension
    (`$attributed_channel` / `$attributed_flow`) but NOT by product. If you
    need attributed revenue broken out by product, you'll need a different
    approach (e.g. joining your own order feed's line items to Klaviyo
    campaign/flow ids via UTM parameters or a shared order id).
  * Campaign/flow REPORT ROWS can be split across message/variation grain
    (e.g. an A/B test) even when you group_by campaign_id/flow_id — always
    roll them up rather than assuming one row per campaign/flow.
  * Rate limiting is a 429 with a `Retry-After` header — honour it. Treat 5xx
    responses and dropped connections as transient and retry with backoff;
    don't let one blip fail an entire scheduled run.

USAGE:
  python klaviyo_sync.py
  python klaviyo_sync.py --campaign-timeframe last_90_days
  python klaviyo_sync.py --only campaigns,flows
  python klaviyo_sync.py --only attributed --days 90
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    ZoneInfo = None  # type: ignore

import requests
from dotenv import load_dotenv, set_key

from warehouse import db

load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

API_BASE = "https://a.klaviyo.com/api"
# Dated JSON:API revision. Bump via .env if Klaviyo deprecates this one -
# check https://developers.klaviyo.com/en/reference/api_overview for the
# current recommended revision before assuming an old one still works.
REVISION = os.environ.get("KLAVIYO_API_REVISION", "2025-07-15")
CONVERSION_METRIC = os.environ.get("KLAVIYO_CONVERSION_METRIC")  # no default: account-specific
CAMPAIGN_TIMEFRAME = os.environ.get("KLAVIYO_CAMPAIGN_TIMEFRAME", "last_30_days")
CAMPAIGN_META_LOOKBACK_DAYS = int(os.environ.get("KLAVIYO_CAMPAIGN_META_LOOKBACK_DAYS", "120"))
TIMEZONE = os.environ.get("KLAVIYO_TIMEZONE", "UTC")

_TZ = ZoneInfo(TIMEZONE) if ZoneInfo else timezone.utc

# Value stats (conversion_value) are requested in the SAME `statistics` array
# as the count stats for the values-report endpoints - the response returns
# them together in each result's `statistics` dict.
CAMPAIGN_STATS = [
    "recipients", "delivered", "opens_unique", "clicks_unique",
    "conversions", "conversion_uniques", "unsubscribes", "bounced",
    "spam_complaints", "conversion_value",
]
FLOW_STATS = [
    "recipients", "delivered", "opens_unique", "clicks_unique",
    "conversions", "conversion_uniques", "unsubscribes", "bounced",
    "conversion_value",
]
SEGMENT_STATS = ["total_members", "members_added", "members_removed", "net_members_changed"]
# metric-aggregates measurements: count=conversions, unique=unique converters,
# sum_value=attributed revenue.
ATTRIBUTED_MEASUREMENTS = ["count", "unique", "sum_value"]
ATTRIBUTED_MAX_DAYS = 365  # same 1-year/request cap as the value reports

DDL = """
CREATE TABLE IF NOT EXISTS klaviyo_campaigns (
  campaign_id TEXT PRIMARY KEY,
  name TEXT, channel TEXT, status TEXT, send_time TEXT,
  recipients INTEGER, delivered INTEGER, opens_unique INTEGER, clicks_unique INTEGER,
  conversions INTEGER, conversion_uniques INTEGER, revenue REAL,
  unsubscribes INTEGER, bounced INTEGER, spam_complaints INTEGER,
  open_rate REAL, click_rate REAL, conversion_rate REAL,
  revenue_per_recipient REAL, average_order_value REAL, click_to_open_rate REAL,
  conversion_metric_id TEXT, as_of TEXT, synced_at TEXT
);

CREATE TABLE IF NOT EXISTS klaviyo_flows (
  flow_id TEXT, name TEXT, channel TEXT, trigger_type TEXT, month_start TEXT,
  recipients INTEGER, delivered INTEGER, opens_unique INTEGER, clicks_unique INTEGER,
  conversions INTEGER, conversion_uniques INTEGER, revenue REAL,
  unsubscribes INTEGER, bounced INTEGER,
  open_rate REAL, click_rate REAL, conversion_rate REAL,
  revenue_per_recipient REAL, average_order_value REAL, click_to_open_rate REAL,
  conversion_metric_id TEXT, as_of TEXT, synced_at TEXT,
  PRIMARY KEY (flow_id, channel, month_start)
);

CREATE TABLE IF NOT EXISTS klaviyo_audience_growth (
  audience_id TEXT, audience_type TEXT, name TEXT, month_start TEXT,
  total_members INTEGER, members_added INTEGER, members_removed INTEGER, net_members_changed INTEGER,
  as_of TEXT, synced_at TEXT,
  PRIMARY KEY (audience_id, month_start)
);

CREATE TABLE IF NOT EXISTS klaviyo_attributed_daily (
  date TEXT, dimension_type TEXT, dimension_id TEXT, dimension_name TEXT,
  conversions INTEGER, conversion_uniques INTEGER, revenue REAL,
  conversion_metric_id TEXT, synced_at TEXT,
  PRIMARY KEY (date, dimension_type, dimension_id)
);
"""


def ensure_schema(conn) -> None:
    """Create every table this script owns. Safe to call repeatedly and safe
    to call on a brand-new warehouse.db - CREATE TABLE IF NOT EXISTS only."""
    conn.executescript(DDL)
    conn.commit()


# --------------------------------------------------------------------------- #
#  HTTP
# --------------------------------------------------------------------------- #
_COMMON_HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "revision": REVISION,
}


def _auth_mode() -> str | None:
    """Which credential set is configured: 'oauth' if KLAVIYO_CLIENT_ID/
    _SECRET + KLAVIYO_REFRESH_TOKEN (see klaviyo_auth.py) are all present,
    else 'private_key' if KLAVIYO_API_KEY is set, else None (unconfigured -
    callers should skip cleanly, same convention as _require_conversion_metric).
    OAuth takes precedence when both happen to be configured."""
    if (os.environ.get("KLAVIYO_CLIENT_ID") and os.environ.get("KLAVIYO_CLIENT_SECRET")
            and os.environ.get("KLAVIYO_REFRESH_TOKEN")):
        return "oauth"
    if os.environ.get("KLAVIYO_API_KEY"):
        return "private_key"
    return None


def _oauth_access_token() -> str:
    """Mint a fresh Bearer access token from KLAVIYO_REFRESH_TOKEN. A minimal
    inline refresh call rather than importing klaviyo_auth.py as a module -
    same convention this scaffold already uses elsewhere (e.g. the TikTok
    Shop connector re-implements its own small token-refresh request instead
    of importing tiktok_auth.py). klaviyo_auth.py is what puts the refresh
    token in .env in the first place; this just spends it."""
    resp = requests.post(
        "https://a.klaviyo.com/oauth/token",
        data={"grant_type": "refresh_token",
              "refresh_token": os.environ["KLAVIYO_REFRESH_TOKEN"]},
        auth=(os.environ["KLAVIYO_CLIENT_ID"], os.environ["KLAVIYO_CLIENT_SECRET"]),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"Klaviyo OAuth token refresh failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Klaviyo OAuth refresh returned no access_token: {data}")
    new_rt = data.get("refresh_token")
    if new_rt and new_rt != os.environ.get("KLAVIYO_REFRESH_TOKEN"):
        os.environ["KLAVIYO_REFRESH_TOKEN"] = new_rt
        set_key(ENV_PATH, "KLAVIYO_REFRESH_TOKEN", new_rt)
    return data["access_token"]


def _session(mode: str | None = None) -> requests.Session:
    """Build an authenticated session. `mode` defaults to whatever
    `_auth_mode()` resolves - OAuth if configured (see klaviyo_auth.py), else
    the private API key. Callers should gate on `_auth_mode()` first (see
    `run()`); with neither configured this raises a KeyError."""
    mode = mode or _auth_mode()
    s = requests.Session()
    s.headers.update(_COMMON_HEADERS)
    if mode == "oauth":
        s.headers["Authorization"] = f"Bearer {_oauth_access_token()}"
    else:
        s.headers["Authorization"] = f"Klaviyo-API-Key {os.environ['KLAVIYO_API_KEY']}"
    return s


def _request(session: requests.Session, method: str, path: str,
             json=None, params=None, max_tries: int = 6):
    """One request with retries: honour 429 Retry-After, ride out 5xx and
    connection drops, surface other 4xx immediately with the body (that's
    almost always a request-shape problem worth seeing, not a transient one)."""
    url = path if path.startswith("http") else API_BASE + path
    for attempt in range(max_tries):
        try:
            resp = session.request(method, url, json=json, params=params, timeout=60)
        except requests.RequestException:
            if attempt == max_tries - 1:
                raise
            time.sleep(min(2 ** attempt, 30))
            continue
        if resp.status_code == 429:
            wait = resp.headers.get("Retry-After")
            time.sleep(min(float(wait) if wait else 2 ** attempt, 60))
            continue
        if resp.status_code >= 500:
            if attempt == max_tries - 1:
                resp.raise_for_status()
            time.sleep(min(2 ** attempt, 30))
            continue
        if not resp.ok:
            raise RuntimeError(
                f"Klaviyo {method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()
    raise RuntimeError(f"Klaviyo {method} {path}: exhausted retries")


def _paginate(session: requests.Session, path: str, params: dict):
    """Yield every `data` item across cursor-paginated JSON:API pages."""
    nxt = None
    while True:
        payload = _request(session, "GET", nxt or path, params=None if nxt else params)
        for item in payload.get("data", []):
            yield item
        nxt = payload.get("links", {}).get("next")
        if not nxt:
            break


# --------------------------------------------------------------------------- #
#  small helpers
# --------------------------------------------------------------------------- #
def _to_local(iso_utc: str | None) -> str | None:
    """Normalize a UTC ISO timestamp to KLAVIYO_TIMEZONE for display."""
    if not iso_utc:
        return None
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_TZ).isoformat()
    except ValueError:
        return iso_utc


def _dt(d) -> str:
    """A date -> a tz-aware midnight-UTC ISO string, for Klaviyo's custom
    timeframe shape."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def _month_starts():
    """(prior_month_first, current_month_first) as date objects."""
    today = datetime.now(timezone.utc).date()
    cur = today.replace(day=1)
    prior = (cur - timedelta(days=1)).replace(day=1)
    return prior, cur


def _next_month(first):
    return (first + timedelta(days=32)).replace(day=1)


def _rates(delivered, opens_u, clicks_u, conv_u):
    d = delivered or 0
    if not d:
        return 0.0, 0.0, 0.0
    return (round(opens_u / d, 6), round(clicks_u / d, 6), round(conv_u / d, 6))


def _derived(revenue, recipients, conversions, opens_u, clicks_u):
    """Revenue-per-recipient / AOV / click-to-open - DERIVED from the summed
    components (ratios can't be summed across a campaign's message rows, so
    recompute post-aggregation, same reasoning as `_rates`). AOV = revenue per
    conversion."""
    rpr = round(revenue / recipients, 6) if recipients else 0.0
    aov = round(revenue / conversions, 4) if conversions else 0.0
    ctor = round(clicks_u / opens_u, 6) if opens_u else 0.0
    return rpr, aov, ctor


def _require_conversion_metric() -> bool:
    if not CONVERSION_METRIC:
        print("  - klaviyo  SKIPPED (KLAVIYO_CONVERSION_METRIC not set - see "
              "the module docstring for how to find your account's metric id)")
        return False
    return True


# --------------------------------------------------------------------------- #
#  campaigns
# --------------------------------------------------------------------------- #
def _email_campaign_meta(session) -> dict:
    since = (datetime.now(timezone.utc)
             - timedelta(days=CAMPAIGN_META_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # the campaigns list endpoint requires a messages.channel filter
    flt = f"and(equals(messages.channel,'email'),greater-or-equal(created_at,{since}))"
    meta = {}
    for c in _paginate(session, "/campaigns/",
                       {"filter": flt, "fields[campaign]": "name,status,send_time",
                        "sort": "-created_at"}):
        a = c.get("attributes", {})
        meta[c["id"]] = {"name": a.get("name"), "status": a.get("status"),
                         "send_time": a.get("send_time")}
    return meta


def _campaign_meta_one(session, cid: str) -> dict:
    try:
        r = _request(session, "GET", f"/campaigns/{cid}/",
                     params={"fields[campaign]": "name,status,send_time"})
        a = r["data"]["attributes"]
        return {"name": a.get("name"), "status": a.get("status"),
                "send_time": a.get("send_time")}
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return {"name": None, "status": None, "send_time": None}


def load_campaigns(session, conn, timeframe: dict, meta: dict | None = None) -> int:
    """Pull + store the email campaign-values report for one `timeframe`
    ({'key': ...} for the scheduled run, or {'start': iso, 'end': iso} for a
    custom window). Pass a prebuilt `meta` map (campaign_id -> name/status/
    send_time) to skip the metadata lookup when you already have one handy.
    Idempotent (PK campaign_id)."""
    body = {"data": {"type": "campaign-values-report", "attributes": {
        "timeframe": timeframe,
        "conversion_metric_id": CONVERSION_METRIC,
        "statistics": CAMPAIGN_STATS,
        "filter": "equals(send_channel,'email')",
    }}}
    results = _request(session, "POST", "/campaign-values-reports/",
                       json=body)["data"]["attributes"]["results"]

    # aggregate report rows up to campaign_id (a campaign may split into
    # several message/variation rows)
    agg: dict[str, dict] = {}
    for res in results:
        g = res.get("groupings", {})
        cid = g.get("campaign_id")
        if not cid:
            continue
        s = res.get("statistics", {})
        d = agg.setdefault(cid, {k: 0 for k in
                                 ("recipients", "delivered", "opens_unique", "clicks_unique",
                                  "conversions", "conversion_uniques", "unsubscribes",
                                  "bounced", "spam_complaints")})
        d.setdefault("revenue", 0.0)
        d.setdefault("channel", g.get("send_channel") or "email")
        for k in ("recipients", "delivered", "opens_unique", "clicks_unique",
                  "conversions", "conversion_uniques", "unsubscribes",
                  "bounced", "spam_complaints"):
            d[k] += int(s.get(k, 0) or 0)
        d["revenue"] += float(s.get("conversion_value", 0) or 0)

    if meta is None:
        meta = _email_campaign_meta(session) if agg else {}
    stamp = db.now()
    n = 0
    for cid, d in agg.items():
        m = meta.get(cid) or _campaign_meta_one(session, cid)
        open_rate, click_rate, conv_rate = _rates(
            d["delivered"], d["opens_unique"], d["clicks_unique"], d["conversion_uniques"])
        rpr, aov, ctor = _derived(
            d["revenue"], d["recipients"], d["conversions"], d["opens_unique"], d["clicks_unique"])
        conn.execute("""INSERT OR REPLACE INTO klaviyo_campaigns
          (campaign_id,name,channel,status,send_time,recipients,delivered,opens_unique,
           clicks_unique,conversions,conversion_uniques,revenue,unsubscribes,bounced,
           spam_complaints,open_rate,click_rate,conversion_rate,
           revenue_per_recipient,average_order_value,click_to_open_rate,conversion_metric_id,
           as_of,synced_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (cid, m.get("name"), d["channel"], m.get("status"), _to_local(m.get("send_time")),
           d["recipients"], d["delivered"], d["opens_unique"], d["clicks_unique"],
           d["conversions"], d["conversion_uniques"], round(d["revenue"], 4),
           d["unsubscribes"], d["bounced"], d["spam_complaints"],
           open_rate, click_rate, conv_rate, rpr, aov, ctor, CONVERSION_METRIC, stamp, stamp))
        n += 1
    conn.commit()
    return n


def sync_campaigns(session, conn) -> int:
    return load_campaigns(session, conn, {"key": CAMPAIGN_TIMEFRAME})


# --------------------------------------------------------------------------- #
#  flows
# --------------------------------------------------------------------------- #
def _flow_meta(session) -> dict:
    meta = {}
    for f in _paginate(session, "/flows/", {"fields[flow]": "name,trigger_type"}):
        a = f.get("attributes", {})
        meta[f["id"]] = {"name": a.get("name"), "trigger_type": a.get("trigger_type")}
    return meta


def _flow_report(session, start_iso: str, end_iso: str):
    body = {"data": {"type": "flow-values-report", "attributes": {
        "timeframe": {"start": start_iso, "end": end_iso},
        "conversion_metric_id": CONVERSION_METRIC,
        "statistics": FLOW_STATS,
        # flow_message_id + flow_id are both required group_bys as of this
        # writing (see the module docstring's API-drift note) - message/
        # variation rows are rolled up to (flow_id, send_channel) below.
        "group_by": ["flow_id", "flow_message_id", "send_channel"],
    }}}
    return _request(session, "POST", "/flow-values-reports/",
                    json=body)["data"]["attributes"]["results"]


def load_flows_window(session, conn, start_iso: str, end_iso: str,
                      month_start: str, meta: dict) -> int:
    """Pull + store one month's flow-values report, rolled up to
    (flow_id, send_channel). Idempotent (PK flow_id+channel+month_start)."""
    stamp = db.now()
    agg: dict[tuple, dict] = {}
    for res in _flow_report(session, start_iso, end_iso):
        g = res.get("groupings", {})
        fid, ch = g.get("flow_id"), g.get("send_channel")
        if not fid:
            continue
        s = res.get("statistics", {})
        key = (fid, ch)
        d = agg.setdefault(key, {k: 0 for k in
                                 ("recipients", "delivered", "opens_unique", "clicks_unique",
                                  "conversions", "conversion_uniques", "unsubscribes",
                                  "bounced")})
        d.setdefault("revenue", 0.0)
        for k in ("recipients", "delivered", "opens_unique", "clicks_unique",
                  "conversions", "conversion_uniques", "unsubscribes", "bounced"):
            d[k] += int(s.get(k, 0) or 0)
        d["revenue"] += float(s.get("conversion_value", 0) or 0)

    n = 0
    for (fid, ch), d in agg.items():
        open_rate, click_rate, conv_rate = _rates(
            d["delivered"], d["opens_unique"], d["clicks_unique"], d["conversion_uniques"])
        rpr, aov, ctor = _derived(
            d["revenue"], d["recipients"], d["conversions"], d["opens_unique"], d["clicks_unique"])
        fm = meta.get(fid, {})
        conn.execute("""INSERT OR REPLACE INTO klaviyo_flows
          (flow_id,name,channel,trigger_type,month_start,recipients,delivered,opens_unique,
           clicks_unique,conversions,conversion_uniques,revenue,unsubscribes,bounced,
           open_rate,click_rate,conversion_rate,
           revenue_per_recipient,average_order_value,click_to_open_rate,conversion_metric_id,
           as_of,synced_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (fid, fm.get("name"), ch, fm.get("trigger_type"), month_start,
           d["recipients"], d["delivered"], d["opens_unique"], d["clicks_unique"],
           d["conversions"], d["conversion_uniques"], round(d["revenue"], 4),
           d["unsubscribes"], d["bounced"],
           open_rate, click_rate, conv_rate, rpr, aov, ctor, CONVERSION_METRIC, stamp, stamp))
        n += 1
    conn.commit()
    return n


def sync_flows(session, conn) -> int:
    """Current + prior calendar month, same window the scheduled run uses."""
    meta = _flow_meta(session)
    prior, cur = _month_starts()
    now = datetime.now(timezone.utc)
    n = 0
    for first in (prior, cur):
        nxt = _next_month(first)
        start_iso = _dt(first)
        # for the current month, stop at "now"; a completed month runs to its end
        end_iso = _dt(nxt) if nxt <= now.date() else now.isoformat()
        n += load_flows_window(session, conn, start_iso, end_iso, first.isoformat(), meta)
    return n


# --------------------------------------------------------------------------- #
#  audience growth (segments)
# --------------------------------------------------------------------------- #
def _segment_meta(session) -> dict:
    meta = {}
    for s in _paginate(session, "/segments/", {"fields[segment]": "name"}):
        meta[s["id"]] = s.get("attributes", {}).get("name")
    return meta


def load_segments_window(session, conn, start_iso: str, end_iso: str,
                         name_map: dict) -> int:
    """Pull + store the monthly segment-series report for [start,end]. One
    call returns every segment's series across the window's months; all-zero
    months (before a segment existed) are skipped. Idempotent (PK
    segment+month)."""
    body = {"data": {"type": "segment-series-report", "attributes": {
        "statistics": SEGMENT_STATS,
        # tz-aware datetimes are required by the segment-series endpoint
        "timeframe": {"start": start_iso, "end": end_iso},
        "interval": "monthly",
    }}}
    attrs = _request(session, "POST", "/segment-series-reports/",
                     json=body)["data"]["attributes"]

    dts = attrs.get("date_times", [])
    months = [dt[:7] + "-01" for dt in dts]
    stamp = db.now()
    n = 0
    for res in attrs.get("results", []):
        sid = res.get("groupings", {}).get("segment_id")
        if not sid:
            continue
        st = res.get("statistics", {})
        for i, ms in enumerate(months):
            tot = (st.get("total_members") or [None] * len(months))[i]
            add = (st.get("members_added") or [None] * len(months))[i]
            rem = (st.get("members_removed") or [None] * len(months))[i]
            net = (st.get("net_members_changed") or [None] * len(months))[i]
            # skip months before the segment existed / had no computed data
            if not any([tot, add, rem, net]):
                continue
            conn.execute("""INSERT OR REPLACE INTO klaviyo_audience_growth
              (audience_id,audience_type,name,month_start,total_members,members_added,
               members_removed,net_members_changed,as_of,synced_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)""",
              (sid, "segment", name_map.get(sid), ms, tot, add, rem, net, stamp, stamp))
            n += 1
    conn.commit()
    return n


def sync_audience(session, conn) -> int:
    name_map = _segment_meta(session)
    prior, _cur = _month_starts()
    now = datetime.now(timezone.utc)
    return load_segments_window(session, conn, _dt(prior), now.isoformat(), name_map)


# --------------------------------------------------------------------------- #
#  daily attributed revenue (channel / flow)
# --------------------------------------------------------------------------- #
def _channel_name(val: str) -> str:
    if not val:
        return "unattributed"
    return val.lstrip("$").replace("_channel", "")  # $email_channel -> email


def _attributed_aggregate(session, by: str, start_iso: str, end_iso: str) -> dict:
    body = {"data": {"type": "metric-aggregate", "attributes": {
        "metric_id": CONVERSION_METRIC,
        "measurements": ATTRIBUTED_MEASUREMENTS,
        "interval": "day",
        "by": [by],
        "filter": [f"greater-or-equal(datetime,{start_iso})",
                   f"less-than(datetime,{end_iso})"],
        "timezone": TIMEZONE,
    }}}
    return _request(session, "POST", "/metric-aggregates/", json=body)["data"]["attributes"]


def _load_dimension(session, conn, dim_type: str, by: str, start_iso: str,
                    end_iso: str, name_of) -> int:
    attrs = _attributed_aggregate(session, by, start_iso, end_iso)
    dates = [d[:10] for d in attrs.get("dates", [])]
    stamp = db.now()
    n = 0
    for grp in attrs.get("data", []):
        dim_id = (grp.get("dimensions") or [""])[0]
        meas = grp.get("measurements", {})
        cnt = meas.get("count") or []
        uniq = meas.get("unique") or []
        val = meas.get("sum_value") or []
        for i, day in enumerate(dates):
            c = int(cnt[i]) if i < len(cnt) and cnt[i] is not None else 0
            u = int(uniq[i]) if i < len(uniq) and uniq[i] is not None else 0
            r = float(val[i]) if i < len(val) and val[i] is not None else 0.0
            if not (c or u or r):
                continue  # don't store empty day/dimension cells
            conn.execute("""INSERT OR REPLACE INTO klaviyo_attributed_daily
              (date,dimension_type,dimension_id,dimension_name,conversions,
               conversion_uniques,revenue,conversion_metric_id,synced_at)
              VALUES (?,?,?,?,?,?,?,?,?)""",
              (day, dim_type, dim_id or "", name_of(dim_id), c, u, round(r, 4),
               CONVERSION_METRIC, stamp))
            n += 1
    conn.commit()
    return n


def sync_attributed_channel(session, conn, days: int) -> int:
    start_iso, end_iso = _attributed_window(days)
    return _load_dimension(session, conn, "channel", "$attributed_channel",
                           start_iso, end_iso, _channel_name)


def sync_attributed_flow(session, conn, days: int, flow_meta: dict | None = None) -> int:
    start_iso, end_iso = _attributed_window(days)
    flow_meta = flow_meta if flow_meta is not None else _flow_meta(session)
    return _load_dimension(
        session, conn, "flow", "$attributed_flow", start_iso, end_iso,
        lambda fid: (flow_meta.get(fid, {}) or {}).get("name") if fid else "campaign/unattributed")


def _attributed_window(days: int) -> tuple[str, str]:
    days = min(days, ATTRIBUTED_MAX_DAYS)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), now.isoformat()


# --------------------------------------------------------------------------- #
#  orchestration
# --------------------------------------------------------------------------- #
def run(only: set[str] | None = None, campaign_timeframe: str | None = None,
        attributed_days: int = 35) -> int:
    """Run whichever sections are selected (default: all four grains).

    `only` is a set drawn from {"campaigns", "flows", "audience", "attributed"};
    None means run everything. Each section logs to sync_log under its own
    platform name and a failure there does not stop the others."""
    if _auth_mode() is None:
        print("  - klaviyo  SKIPPED (no Klaviyo credentials in .env - set "
              "KLAVIYO_API_KEY, or configure OAuth via klaviyo_auth.py)")
        return 0
    if not _require_conversion_metric():
        return 0

    timeframe = campaign_timeframe or CAMPAIGN_TIMEFRAME

    db.init_db()
    conn = db.connect()
    with conn:
        ensure_schema(conn)

    session = _session()
    want = (lambda name: only is None or name in only)

    sections: list[tuple[str, object]] = []
    if want("campaigns"):
        sections.append(("klaviyo_campaigns", lambda: load_campaigns(session, conn, {"key": timeframe})))
    if want("flows"):
        sections.append(("klaviyo_flows", lambda: sync_flows(session, conn)))
    if want("audience"):
        sections.append(("klaviyo_audience", lambda: sync_audience(session, conn)))
    if want("attributed"):
        flow_meta = _flow_meta(session)
        sections.append(("klaviyo_attr_channel", lambda: sync_attributed_channel(session, conn, attributed_days)))
        sections.append(("klaviyo_attr_flow", lambda: sync_attributed_flow(session, conn, attributed_days, flow_meta)))

    total = 0
    for platform, fn in sections:
        started = db.now()
        try:
            n = fn()
            db.log_sync(platform, started, n, "ok")
            print(f"  - {platform:18s} OK     {n} rows")
            total += n
        except Exception as e:  # noqa: BLE001 - one section failing must not kill the rest
            db.log_sync(platform, started, 0, "error", str(e))
            print(f"  - {platform:18s} ERROR  {e}")
    conn.close()
    return total


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Sync Klaviyo campaigns/flows/segments/attributed-revenue into the warehouse.")
    p.add_argument("--campaign-timeframe",
                   help="override the campaign report window key (e.g. last_30_days, last_90_days)")
    p.add_argument("--days", type=int, default=35,
                   help="attributed-daily lookback window in days (default 35; capped at 365/request)")
    p.add_argument("--only",
                   help="comma list of sections to run: campaigns,flows,audience,attributed "
                        "(default: all four)")
    args = p.parse_args()
    only_set = {s.strip() for s in args.only.split(",")} if args.only else None
    run(only=only_set, campaign_timeframe=args.campaign_timeframe, attributed_days=args.days)
