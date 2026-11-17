"""Shared retry for outbound calls: one definition of "transient", one backoff.

Left to themselves, connectors each decide what a retryable failure is, and
some decide nothing at all. The commonest real-world failure is the same on
every host: a connection reset by the remote end (on Windows,
``ConnectionResetError(10054)``), a timeout, or a brief 5xx, any of which the
next attempt usually gets through.

Two entry points:

* ``request(method, url, **kwargs)`` - a ``requests`` call that retries
  transport errors and transient HTTP statuses (408, 425, 429, 5xx), honouring
  ``Retry-After``. It RETURNS the final response, whatever its status: the
  caller still decides what a 400 or a 403 means for its API.
* ``retry_call(fn, *args, **kwargs)`` - for a client library (google-ads,
  google-analytics-data, ...) that raises its own exception types. Retries
  when ``is_transient(exc)``; the call must be safe to repeat, i.e. a read or
  a write that SETS a value.

What is never retried: a permanent HTTP status (bad request, auth, not found)
or an exception that is not recognisably transient. Retrying those only delays
an error a person has to fix.
"""
from __future__ import annotations

import random
import time
import urllib.error
from collections.abc import Callable
from typing import Any, TypeVar

import requests

T = TypeVar("T")

TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
ATTEMPTS = 4
BASE_DELAY = 10.0      # seconds; doubles each attempt: 10, 20, 40
MAX_DELAY = 60.0
MAX_RETRY_AFTER = 120.0

# Exception class names from optional client libraries, matched by name so this
# module imports none of them. google.api_core: the gRPC/REST status errors a
# Google client raises on a server-side blip. google.auth: a token refresh that
# could not reach the token endpoint.
_TRANSIENT_NAMES = frozenset({
    "ServiceUnavailable", "DeadlineExceeded", "InternalServerError",
    "TooManyRequests", "ResourceExhausted", "BadGateway", "GatewayTimeout",
    "TransportError", "RetryError",
})


def is_transient(exc: BaseException) -> bool:
    """True if `exc`, or anything in its cause/context chain, is a network
    drop, a timeout, or a known server-side transient error."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (requests.ConnectionError, requests.Timeout,
                            ConnectionError, TimeoutError)):
            return True
        if isinstance(cur, urllib.error.URLError) and not isinstance(cur, urllib.error.HTTPError):
            return True
        if isinstance(cur, urllib.error.HTTPError) and cur.code in TRANSIENT_STATUS:
            return True
        if type(cur).__name__ in _TRANSIENT_NAMES:
            return True
        # requests wraps urllib3's error, which wraps the socket error, in its
        # args rather than its __cause__.
        for arg in getattr(cur, "args", ()):
            if isinstance(arg, BaseException) and id(arg) not in seen:
                if is_transient(arg):
                    return True
        cur = cur.__cause__ or cur.__context__
    return False


def backoff(attempt: int, base: float = BASE_DELAY, cap: float = MAX_DELAY) -> float:
    """Exponential, capped, with a little jitter so parallel steps that failed
    together do not all retry in the same second."""
    return min(base * 2 ** (attempt - 1), cap) * random.uniform(0.9, 1.1)


def retry_call(
    fn: Callable[..., T],
    *args: Any,
    attempts: int = ATTEMPTS,
    is_retryable: Callable[[BaseException], bool] = is_transient,
    label: str = "",
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> T:
    """Call fn(*args, **kwargs); on a retryable exception wait and call again,
    up to `attempts` calls in all. The last exception propagates unchanged."""
    name = label or getattr(fn, "__name__", "call")
    for attempt in range(1, attempts + 1):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- classified just below
            if attempt == attempts or not is_retryable(exc):
                raise
            wait = backoff(attempt)
            print(f"    {name}: attempt {attempt}/{attempts} failed "
                  f"({type(exc).__name__}: {str(exc)[:120]}); retrying in {wait:.0f}s",
                  flush=True)
            sleep(wait)
            continue
        if attempt > 1:
            print(f"    {name}: succeeded on attempt {attempt}", flush=True)
        return result
    raise AssertionError("unreachable")


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After") if resp is not None else None
    try:
        return min(float(value), MAX_RETRY_AFTER) if value else None
    except ValueError:
        return None          # an HTTP-date; fall back to our own backoff


def request(
    method: str,
    url: str,
    *,
    attempts: int = ATTEMPTS,
    retry_status: frozenset[int] = TRANSIENT_STATUS,
    sleep: Callable[[float], None] = time.sleep,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Response:
    """requests.request() with retry on transport errors and transient
    statuses. Returns the last response; raises only if every attempt failed
    in transport. Pass `timeout=`; without one requests can wait forever."""
    kwargs.setdefault("timeout", 120)
    send = session.request if session is not None else requests.request
    short = url.split("?")[0]
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = send(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            wait = backoff(attempt)
            print(f"    {method} {short}: attempt {attempt}/{attempts} "
                  f"{type(exc).__name__}; retrying in {wait:.0f}s", flush=True)
            sleep(wait)
            continue
        if resp.status_code in retry_status and attempt < attempts:
            wait = _retry_after(resp) or backoff(attempt)
            print(f"    {method} {short}: attempt {attempt}/{attempts} HTTP "
                  f"{resp.status_code}; retrying in {wait:.0f}s", flush=True)
            sleep(wait)
            continue
        return resp
    raise last_exc or AssertionError("unreachable")
