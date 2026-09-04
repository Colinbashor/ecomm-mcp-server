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

!! DO NOT ASSUME A REPORT PAST ITS RETENTION WINDOW ALWAYS COMES BACK
CANCELLED — on at least one report type/account, an out-of-retention week
returned **FATAL** instead, with the exact same generic error message
("A client error occurred. Please double check that your parameters are
valid...") that an unpublished, too-*recent* week also produces. Those two
situations are indistinguishable from the response alone. A backfill walking
backward in time that stops only on `BAReportCancelled` can therefore run
forever against a report type/account where retention manifests as FATAL. A
robust walk-back should stop after a bounded number of consecutive
non-productive weeks (FATAL or CANCELLED or empty), not rely on one specific
exception type as "the" floor signal. See `create_ba_report`'s
`CREATE_BURST_LIMIT` note below for a related measured-vs-documented gap: the
create-report throttle can be noticeably tighter in practice than the
documented burst allowance implies, so a backfill that fires several reports
concurrently to find a retention floor quickly should pace off a conservative
sustained rate, not the burst limit.

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


# ---- streaming download ----------------------------------------------------
# Some BA reports (Top Search Terms in particular) are market-wide rather than
# scoped to your own catalog, and can run to millions of records over a wide
# window. `fetch_ba_records` calls `json.loads()` on the whole document, which
# materializes the entire thing as Python dicts at once -- fine for a
# few-thousand-row report, but a caller processing one of the large
# market-wide reports should walk the gzip stream and decode one record at a
# time instead, so memory stays flat regardless of how big the document is.

class _ChunkReader(io.RawIOBase):
    """Adapt a requests iter_content() generator to a readable binary stream."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._buf = b""

    def readable(self) -> bool:  # noqa: D102
        return True

    def readinto(self, dest) -> int:  # noqa: D102
        while not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(dest), len(self._buf))
        dest[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _iter_json_array_objects(text_fh, chunk_chars: int = 1 << 20):
    """Yield objects from the first TOP-LEVEL array-of-objects in a JSON stream.

    'Top-level' is load-bearing: a BA document is
    {"reportSpecification": {... "marketplaceIds": [...]}, "dataBy...": [...]}
    so the first '[' in the raw text belongs to marketplaceIds, at depth 2. We
    track nesting (string- and escape-aware) and take the first '[' seen while
    depth == 1, which is always the data array.
    """
    dec = json.JSONDecoder()
    buf = ""
    pos = 0
    # --- phase 1: locate the data array -----------------------------------
    depth = 0
    in_str = False
    esc = False
    found = False
    while not found:
        while pos < len(buf):
            ch = buf[pos]
            pos += 1
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == "[":
                if depth == 1:
                    found = True
                    break
                depth += 1
            elif ch == "]":
                depth -= 1
        if found:
            break
        more = text_fh.read(chunk_chars)
        if not more:
            return
        buf = buf[pos:]
        pos = 0
        buf += more

    # --- phase 2: decode elements one at a time ---------------------------
    # `pos` is an index rather than repeated buf slicing: slicing a 1 MB buffer
    # once per record would copy terabytes over a multi-million-record document.
    while True:
        while pos < len(buf) and buf[pos] in " \t\r\n,":
            pos += 1
        if pos < len(buf) and buf[pos] == "]":
            return
        if pos < len(buf):
            try:
                obj, end = dec.raw_decode(buf, pos)
            except ValueError:
                pass  # truncated record -- need more input
            else:
                yield obj
                pos = end
                if pos > (1 << 20):
                    buf = buf[pos:]
                    pos = 0
                continue
        more = text_fh.read(chunk_chars)
        if not more:
            return
        buf = buf[pos:]
        pos = 0
        buf += more


def stream_ba_records(doc_id: str):
    """Yield a finished report document's records one at a time (flat memory)."""
    host = _host()
    headers = {"x-amz-access-token": _access_token()}
    doc = requests.get(f"{host}/reports/2021-06-30/documents/{doc_id}",
                       headers=headers, timeout=60).json()
    r = requests.get(doc["url"], timeout=300, stream=True)
    r.raise_for_status()
    binary = io.BufferedReader(_ChunkReader(r.iter_content(1 << 16)))
    # Trust the magic bytes over the metadata: requests transparently inflates a
    # Content-Encoding: gzip body, in which case compressionAlgorithm still says
    # GZIP but the stream is already plain text.
    if binary.peek(2)[:2] == b"\x1f\x8b":
        binary = gzip.GzipFile(fileobj=binary)
    yield from _iter_json_array_objects(io.TextIOWrapper(binary, encoding="utf-8"))


def await_ba_report(report_id: str, timeout_min: int = DEFAULT_TIMEOUT_MIN) -> str:
    """Poll one already-created report to terminal and return its document id.

    Pairs with `create_ba_report` + `stream_ba_records` for a caller that wants
    the phased create/poll/stream split but only ever has one report in flight
    (e.g. a single large market-wide report), rather than the many-reports-at-once
    use case `check_ba_report` is designed for.
    """
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        time.sleep(POLL_EVERY_SEC)
        status, payload = check_ba_report(report_id)
        if status == "DONE":
            return payload
        if status == "CANCELLED":
            raise BAReportCancelled(f"{report_id} CANCELLED (no data for this window)")
        if status == "FATAL":
            raise BAReportFatal(f"{report_id} FATAL: {payload}")
    raise TimeoutError(f"{report_id} did not finish within {timeout_min} min")
