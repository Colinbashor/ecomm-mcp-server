"""
Amazon Advertising connector (Sponsored Products + Brands + Display daily
campaign reports).

Amazon Ads reporting is asynchronous: you (1) request a report, (2) poll
until it's ready, (3) download the gzipped JSON. This wires that flow for
each ad product; results are normalized into ad_metrics rows with
campaign_type distinguishing SP/SB/SD.

Access tokens last 1 hour; we refresh one from the refresh token per sync.
HTTP 425 = a report with the same config is already running — reuse its id.
v3 limits: <=31-day range per report, ~95-day lookback.

If one ad product's report fails, the others still sync (partial results
with a printed warning) — a bad SB column name must not kill SP data.

NOTE: Amazon DSP is NOT covered here — it uses a different reporting surface
and entity permissions, not the v3 /reporting/reports endpoint.

Docs: https://advertising.amazon.com/API/docs/en-us/guides/reporting/v3/get-started
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import time

import requests

PLATFORM = "amazon"

REGION_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}

# (adProduct, reportTypeId, conversions column, revenue column)
# SP uses attribution-windowed names; SB/SD use plain names in v3.
AD_PRODUCTS = [
    ("SPONSORED_PRODUCTS", "spCampaigns", "purchases14d", "sales14d"),
    ("SPONSORED_BRANDS", "sbCampaigns", "purchases", "sales"),
    ("SPONSORED_DISPLAY", "sdCampaigns", "purchases", "sales"),
]


def _access_token() -> str:
    for attempt in range(5):
        try:
            resp = requests.post(
                "https://api.amazon.com/auth/o2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": os.environ["AMAZON_ADS_REFRESH_TOKEN"],
                    "client_id": os.environ["AMAZON_ADS_CLIENT_ID"],
                    "client_secret": os.environ["AMAZON_ADS_CLIENT_SECRET"],
                },
                timeout=60,
            )
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            # api.amazon.com sheds/reset connections under load — retry, don't
            # let a token blip kill a whole report batch.
            if attempt == 4:
                raise
            time.sleep(30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _pull_report(host: str, headers: dict, start_date: str, end_date: str,
                 ad_product: str, report_type: str, conv_col: str, rev_col: str) -> list[dict]:
    profile_id = headers["Amazon-Advertising-API-Scope"]

    # 1) request a campaign-level daily report
    body = {
        "name": f"warehouse-{report_type}",
        "startDate": start_date,
        "endDate": end_date,
        "configuration": {
            "adProduct": ad_product,
            "groupBy": ["campaign"],
            "columns": ["date", "campaignId", "campaignName", "impressions",
                        "clicks", "cost", conv_col, rev_col],
            "reportTypeId": report_type,
            "timeUnit": "DAILY",
            "format": "GZIP_JSON",
        },
    }
    records = run_report(host, headers, body)

    rows: list[dict] = []
    for d in records:
        rows.append(
            {
                "platform": PLATFORM,
                "account_id": profile_id,
                "campaign_id": str(d.get("campaignId")),
                "campaign_name": d.get("campaignName"),
                "date": d.get("date"),
                "impressions": int(d.get("impressions", 0) or 0),
                "clicks": int(d.get("clicks", 0) or 0),
                "spend": float(d.get("cost", 0) or 0),
                "conversions": float(d.get(conv_col, 0) or 0),
                "revenue": float(d.get(rev_col, 0) or 0),
                "currency": None,  # Amazon reports in the profile's marketplace currency
                "campaign_type": ad_product,
            }
        )
    return rows


def run_report(host: str, headers: dict, body: dict) -> list[dict]:
    """Generic v3 report flow: request (dedupe-aware) -> poll -> download.
    Returns the raw record dicts. Shared by ad_metrics and the detail syncs."""
    report_type = body["configuration"]["reportTypeId"]
    report_id = None
    for attempt in range(8):
        try:
            r = requests.post(f"{host}/reporting/reports", headers=headers, json=body, timeout=60)
        except requests.exceptions.ConnectionError:
            time.sleep(20)  # transient drop — Amazon sheds connections when busy
            continue
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code == 401:
            # LWA access token expired mid-batch (callers loop many reports
            # through one headers dict) — refresh in place and retry.
            headers["Authorization"] = f"Bearer {_access_token()}"
            continue
        if r.status_code == 425:
            # duplicate request — Amazon returns the already-running report's id
            m = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27,}", r.text)
            if not m:
                raise RuntimeError(f"Amazon 425 without a report id: {r.text[:200]}")
            report_id = m.group(0)
            break
        if r.status_code not in (200, 202):
            raise RuntimeError(f"Amazon report request {r.status_code}: {r.text[:300]}")
        report_id = r.json()["reportId"]
        break
    if not report_id:
        raise RuntimeError(f"Amazon {report_type} report request kept throttling.")

    # 2) poll until COMPLETED. Reports regularly take several minutes, and when
    #    Amazon's queue is congested they can take 30-45+ (every morning run
    #    2026-07-03..06 blew a 15-minute cap) — so wait up to ~60 by default.
    timeout_min = float(os.environ.get("AMAZON_ADS_REPORT_TIMEOUT_MIN", "60"))
    deadline = time.time() + timeout_min * 60
    url = None
    wait = 15.0
    while time.time() < deadline:
        time.sleep(wait)
        wait = min(60.0, wait * 1.25)  # poll less often as the wait drags on
        try:
            poll = requests.get(f"{host}/reporting/reports/{report_id}", headers=headers, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            continue  # transient drop while waiting — the report keeps cooking
        if poll.status_code == 401:
            headers["Authorization"] = f"Bearer {_access_token()}"
            continue
        if poll.status_code == 429 or poll.status_code >= 500:
            continue
        poll.raise_for_status()
        status = poll.json()
        if status.get("status") == "COMPLETED":
            url = status.get("url")
            break
        if status.get("status") == "FAILED":
            raise RuntimeError(f"Amazon report failed: {status}")
    if not url:
        raise TimeoutError(f"Amazon {report_type} report did not finish within ~{timeout_min:.0f} minutes")

    # 3) download + unzip
    dl = requests.get(url, timeout=120)
    dl.raise_for_status()
    return json.loads(gzip.GzipFile(fileobj=io.BytesIO(dl.content)).read())


def sync(start_date: str, end_date: str) -> list[dict]:
    host = REGION_HOSTS[os.environ.get("AMAZON_ADS_REGION", "NA")]
    headers = {
        "Authorization": f"Bearer {_access_token()}",
        "Amazon-Advertising-API-ClientId": os.environ["AMAZON_ADS_CLIENT_ID"],
        "Amazon-Advertising-API-Scope": os.environ["AMAZON_ADS_PROFILE_ID"],
        "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
    }

    rows: list[dict] = []
    failures: list[str] = []
    for ad_product, report_type, conv_col, rev_col in AD_PRODUCTS:
        try:
            got = _pull_report(host, headers, start_date, end_date,
                               ad_product, report_type, conv_col, rev_col)
            rows.extend(got)
            print(f"    (amazon {report_type}: {len(got)} rows)")
        except Exception as e:  # noqa: BLE001 — one product must not kill the others
            failures.append(f"{report_type}: {e}")
            print(f"    (amazon {report_type} FAILED: {str(e)[:120]})")
    if failures and not rows:
        raise RuntimeError("; ".join(failures)[:400])
    return rows
