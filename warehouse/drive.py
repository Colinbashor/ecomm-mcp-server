"""Replace a Google Drive file's content in place, riding out transient faults.

Several feeds in this repo publish the same way: build an .xlsx and PATCH it
onto a Drive file id that the consuming platform (Merchant Center, for
example) already fetches on its own schedule. Drive converts the upload into
the existing native Google Sheet because the file's mimeType is left alone,
so the platform's registration of that file id never has to change.

Why bounded retry: without it, one dropped connection or a Drive 503 at
publish time leaves a feed stale until the next scheduled run, and nothing
about the stale sheet looks wrong.

What is retried: connection errors, timeouts, 408, 429 and 5xx. What is not:
any other 4xx (a bad file id, a lost share, a revoked key). Retrying those
only delays an error a person has to fix. Replacing content is idempotent, so
a retry after a request that did in fact land is harmless.

Auth: a Google service-account JSON key, passed in by the caller (the
Merchant Center scripts read GMC_CREDENTIALS_FILE). Share the target Sheet
with the service account's email as an Editor; Drive returns 404, not 403,
for a file the account cannot see.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import requests

UPLOAD_URL = ("https://www.googleapis.com/upload/drive/v3/files/{file_id}"
              "?uploadType=multipart&supportsAllDrives=true")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
TIMEOUT_SECONDS = 180


def _backoff(attempt: int) -> float:
    """10s, 20s, 40s: long enough for a Drive brownout, short enough to finish
    well inside a scheduled task's time limit."""
    return min(10 * 2 ** (attempt - 1), 60)


def access_token(sa_file: str | Path) -> str:
    from google.oauth2 import service_account
    import google.auth.transport.requests

    creds = service_account.Credentials.from_service_account_file(
        str(sa_file), scopes=[DRIVE_SCOPE])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def replace_content(
    file_id: str,
    filename: str,
    content: bytes,
    token: str,
    mime: str = XLSX_MIME,
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """PATCH `content` onto Drive file `file_id`. Raises RuntimeError when it
    cannot, naming the last failure and how many attempts were made."""
    url = UPLOAD_URL.format(file_id=file_id)
    last = "no attempt made"
    for attempt in range(1, attempts + 1):
        files = {"metadata": ("metadata", json.dumps({}), "application/json"),
                 "file": (filename, content, mime)}
        try:
            r = requests.patch(url, headers={"Authorization": f"Bearer {token}"},
                               files=files, timeout=TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = f"transport: {type(exc).__name__}: {str(exc)[:200]}"
        else:
            if r.status_code == 200:
                if attempt > 1:
                    print(f"Drive update succeeded on attempt {attempt} (after: {last})")
                return
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code not in TRANSIENT_STATUS:
                raise RuntimeError(f"Drive update failed, not retried ({last})")
        if attempt < attempts:
            wait = _backoff(attempt)
            print(f"Drive update attempt {attempt}/{attempts} failed ({last}); "
                  f"retrying in {wait:.0f}s")
            sleep(wait)
    raise RuntimeError(f"Drive update failed after {attempts} attempts ({last})")


def replace_with_file(file_id: str, path: str | Path, sa_file: str | Path,
                      **kwargs) -> None:
    """replace_content() for a file on disk, authenticating with a service
    account key."""
    p = Path(path)
    replace_content(file_id, p.name, p.read_bytes(), access_token(sa_file), **kwargs)


def write_xlsx(path: str | Path, sheet_title: str, header: Sequence[str],
               rows: Iterable[Sequence]) -> None:
    """One-tab workbook: a header row, then `rows`. Empty strings are written
    as truly empty cells rather than as zero-length text."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]          # Excel's tab-name limit
    ws.append(list(header))
    for row in rows:
        ws.append([None if v == "" else v for v in row])
    wb.save(str(path))


def replace_with_rows(file_id: str, sheet_title: str, header: Sequence[str],
                      rows: Iterable[Sequence], sa_file: str | Path, **kwargs) -> None:
    """Replace the WHOLE content of Sheet `file_id` with `header` + `rows`.
    Whole-sheet replacement is the point: a row left out of this build (an
    expired promotion, a delisted product) drops out of the sheet as well."""
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        write_xlsx(tmp, sheet_title, header, rows)
        replace_with_file(file_id, tmp, sa_file, **kwargs)
    finally:
        Path(tmp).unlink(missing_ok=True)
