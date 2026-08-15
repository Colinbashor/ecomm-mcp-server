r"""
Shared Amazon Brand Analytics report runner (SP-API Reports, JSON payloads).

Brand Analytics (BA) is a family of SP-API reports available to brand-
registered sellers that go beyond your own sales data — they show MARKET-LEVEL
search and shopping behavior: which search queries people use, how your ASINs
perform on those queries relative to the whole market, what else shoppers buy
alongside your products, and repeat-purchase behavior. See amazon_sqp_sync.py
(Search Query Performance) and amazon_ba_sync.py (Search Catalog Performance,
Top Search Terms, Market Basket Analysis, Repeat Purchase Behavior).

All of these reports share the same create -> poll -> download -> gunzip ->
json.loads machinery, plus three BA-specific traps that a copy-paste-per-report
implementation would likely get wrong:

  * BA weeks are SUNDAY-SATURDAY, not whatever weekly convention you use
    elsewhere (many shops report Monday-Sunday). run_ba_report validates that
    the requested week starts on a Sunday.
  * A FAILED report's reason is NOT in the status poll — it is in the report
    DOCUMENT's `errorDetails`. On FATAL we download that document and raise
    with the real reason (e.g. a missing required `asin` reportOption), so
    callers do not have to guess-and-retry blind.
  * Reports commonly take 15-25+ minutes to process; we poll for up to ~45 min.
    A 202 at create time is NOT success — it just means the request queued.

CANCELLED (Amazon returns no data — e.g. a week past this report's retention
window) raises `BAReportCancelled`, which callers should treat as a normal,
non-fatal outcome (e.g. the floor of a backfill walk-back), not an error.

TWO CALLING STYLES:
  * `run_ba_report` — create, poll to terminal, return records. One report at a
    time; the whole 15-25 min queue wait is inline. Fine for a script that only
    ever runs one report per report type (amazon_ba_sync.py).
  * `create_ba_report` / `check_ba_report` / `fetch_ba_records` — the same flow
    split into phases so a caller with MANY reports in flight (e.g. one BA week
    fanned out over several ASIN-batch reports, as amazon_sqp_sync.py does) can
    hold several open at once and pay the queue latency roughly ONCE instead of
    once per report. run_ba_report is implemented on top of these, so both
    calling styles share one code path.

Auth/host/marketplace come from the same SPAPI_* env vars and access-token
helper used by warehouse/connectors/amazon_orders.py and the other Amazon
connectors in this project.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import time
from datetime import date, timedelta

import requests

from warehouse.connectors.amazon_orders import HOSTS, _access_token

# Poll cadence. BA reports commonly sit 15-25 min in queue; poll well past that
# before declaring a timeout — Amazon's report queue can run 30-45+ min when
# congested.
POLL_EVERY_SEC = 20
DEFAULT_TIMEOUT_MIN = 45
# Courtesy spacing callers should sleep BETWEEN sequential createReport calls
# (the Reports API throttles createReport). Exposed so batch loops stay honest.
CREATE_SPACING_SEC = 5
# createReport is documented at roughly 1 request/minute sustained with a
# burst of ~15, so a concurrent caller must keep its in-flight count well
# under the burst bucket or it trades queue waits for 429s. Exposed as
# guidance for batch callers.
CREATE_BURST_LIMIT = 15


class BAReportCancelled(Exception):
    """Amazon CANCELLED the report — no data for this window (e.g. past this
    report's retention). Backfill callers can use this as a walk-back floor;
    it is a normal, non-fatal outcome, not a failure."""


class BAReportFatal(RuntimeError):
    """Report finished FATAL. The message carries the document errorDetails."""


def _host() -> str:
    return HOSTS[os.environ.get("SPAPI_REGION", "NA").upper()]


def _marketplace_ids() -> list[str]:
    return [os.environ["SPAPI_MARKETPLACE_ID"]]


def sunday_saturday(week_sunday: date) -> tuple[str, str]:
    """(dataStartTime, dataEndTime) for the Sun-Sat BA week starting week_sunday."""
    if week_sunday.weekday() != 6:  # Python: Monday=0 .. Sunday=6
        raise ValueError(
            f"Brand Analytics weeks are Sunday-Saturday; {week_sunday} is a "
            f"{week_sunday.strftime('%A')}, not a Sunday.")
    saturday = week_sunday + timedelta(days=6)
    return f"{week_sunday.isoformat()}T00:00:00Z", f"{saturday.isoformat()}T23:59:59Z"


def _download_json(host: str, doc_id: str) -> dict:
    headers = {"x-amz-access-token": _access_token()}
    doc = requests.get(f"{host}/reports/2021-06-30/documents/{doc_id}",
                       headers=headers, timeout=60).json()
    raw = requests.get(doc["url"], timeout=300).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return json.loads(raw)


def _fatal_reason(host: str, status: dict) -> str:
    """Pull the human reason for a FATAL report out of its document, if any."""
    doc_id = status.get("reportDocumentId")
    if not doc_id:
        return f"FATAL with no document ({status})"
    try:
        body = _download_json(host, doc_id)
    except Exception as e:  # noqa: BLE001 — best-effort reason extraction
        return f"FATAL; could not read error document: {e}"
    # BA error documents surface reasons under errorDetails / messages.
    detail = body.get("errorDetails") or body.get("messages") or body
    return json.dumps(detail)[:500]


def _window(period: str, week_sunday: date | None,
            month_start: date | None, month_end: date | None) -> tuple[str, str]:
    if period == "MONTH":
        if not (month_start and month_end):
            raise ValueError("period='MONTH' needs month_start and month_end")
        return (f"{month_start.isoformat()}T00:00:00Z",
                f"{month_end.isoformat()}T23:59:59Z")
    if week_sunday is None:
        raise ValueError("period='WEEK' needs week_sunday")
    return sunday_saturday(week_sunday)


def create_ba_report(report_type: str, week_sunday: date | None,
                     options: dict | None = None, *,
                     period: str = "WEEK",
                     month_start: date | None = None,
                     month_end: date | None = None) -> str:
    """Submit one BA report and return its reportId WITHOUT waiting for it.

    Retries a 429 (createReport's throttle bucket) rather than raising, so a
    batch caller that outruns the throttle waits instead of losing the batch.
    """
    host = _host()
    start, end = _window(period, week_sunday, month_start, month_end)
    body: dict = {
        "reportType": report_type,
        "marketplaceIds": _marketplace_ids(),
        "dataStartTime": start,
        "dataEndTime": end,
    }
    if options:
        body["reportOptions"] = options

    for attempt in range(4):
        headers = {"x-amz-access-token": _access_token(),
                   "Content-Type": "application/json"}
        r = requests.post(f"{host}/reports/2021-06-30/reports", headers=headers,
                          json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(60)
            continue
        if r.status_code not in (200, 202):
            raise RuntimeError(f"{report_type} create {r.status_code}: {r.text[:200]}")
        return r.json()["reportId"]
    raise RuntimeError(f"{report_type} create throttled (429) after {attempt + 1} attempts")


def check_ba_report(report_id: str) -> tuple[str, str | None]:
    """One non-blocking status poll -> (status, doc_id_or_reason).

    status is 'PENDING' (not terminal yet, including a transient poll error),
    'DONE' (doc id returned), 'CANCELLED', or 'FATAL' (reason returned).
    """
    host = _host()
    headers = {"x-amz-access-token": _access_token()}
    try:
        st = requests.get(f"{host}/reports/2021-06-30/reports/{report_id}",
                          headers=headers, timeout=60).json()
    except requests.RequestException:
        return "PENDING", None  # transient — Amazon sheds connections under load
    status = st.get("processingStatus")
    if status == "DONE":
        return "DONE", st["reportDocumentId"]
    if status == "CANCELLED":
        return "CANCELLED", None
    if status == "FATAL":
        return "FATAL", _fatal_reason(host, st)
    return "PENDING", None


def fetch_ba_records(doc_id: str, records_key: str | None = None) -> list[dict]:
    """Download + gunzip a finished report document and return its record list."""
    data = _download_json(_host(), doc_id)
    if records_key is not None:
        return data.get(records_key) or []
    # Auto-detect: BA docs are {reportSpecification: {...}, <dataArrayKey>: [...]}.
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


def run_ba_report(report_type: str, week_sunday: date,
                  options: dict | None = None, *,
                  period: str = "WEEK", records_key: str | None = None,
                  month_start: date | None = None, month_end: date | None = None,
                  timeout_min: int = DEFAULT_TIMEOUT_MIN) -> list[dict]:
    """Create, poll to terminal, and return the record list of a BA report.

    period='WEEK' uses the Sun-Sat week starting `week_sunday` (validated).
    period='MONTH' uses [month_start, month_end] verbatim (for Repeat Purchase).
    `options` are merged into reportOptions (e.g. {'asin': 'B0.. B0..',
    'reportPeriod': 'WEEK'}).
    `records_key` overrides which top-level array is the data (default: the
    first list-valued top-level key — BA docs have exactly one).

    Raises BAReportCancelled on CANCELLED (no data / past retention),
    BAReportFatal on FATAL (message = document errorDetails),
    TimeoutError if the report never reaches a terminal state.

    Blocks for the report's whole queue wait. A caller running MANY reports
    should use create_ba_report + check_ba_report + fetch_ba_records instead so
    the waits overlap.
    """
    start, end = _window(period, week_sunday, month_start, month_end)
    report_id = create_ba_report(report_type, week_sunday, options, period=period,
                                 month_start=month_start, month_end=month_end)

    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        time.sleep(POLL_EVERY_SEC)
        status, payload = check_ba_report(report_id)
        if status == "DONE":
            return fetch_ba_records(payload, records_key)
        if status == "CANCELLED":
            raise BAReportCancelled(f"{report_type} {start[:10]}..{end[:10]} CANCELLED (no data)")
        if status == "FATAL":
            raise BAReportFatal(f"{report_type} FATAL: {payload}")
    raise TimeoutError(f"{report_type} did not finish within {timeout_min} min")
