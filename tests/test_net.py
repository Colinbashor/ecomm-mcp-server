"""warehouse/http.py: the shared definition of a transient failure and its retry."""
from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

import requests

from warehouse import net as http


class _Resp:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


def _reset() -> requests.ConnectionError:
    """What a 10054 looks like by the time requests raises it."""
    sock = ConnectionResetError(10054, "An existing connection was forcibly closed")
    return requests.ConnectionError(("Connection aborted.", sock))


class IsTransientTests(unittest.TestCase):
    def test_network_drops_and_timeouts(self) -> None:
        for exc in (_reset(), requests.Timeout("read timed out"), ConnectionResetError(),
                    TimeoutError(), urllib.error.URLError("reset")):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(http.is_transient(exc))

    def test_a_reset_wrapped_by_a_client_library_is_found_in_the_chain(self) -> None:
        class RefreshError(Exception):
            pass
        try:
            try:
                raise _reset()
            except requests.ConnectionError as inner:
                raise RefreshError("token refresh failed") from inner
        except RefreshError as outer:
            self.assertTrue(http.is_transient(outer))

    def test_google_server_errors_by_name(self) -> None:
        ServiceUnavailable = type("ServiceUnavailable", (Exception,), {})
        self.assertTrue(http.is_transient(ServiceUnavailable("503")))

    def test_permanent_errors_are_not_transient(self) -> None:
        for exc in (ValueError("bad"), KeyError("x"), PermissionError("denied"),
                    urllib.error.HTTPError("u", 403, "Forbidden", {}, None)):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(http.is_transient(exc))
        self.assertTrue(http.is_transient(urllib.error.HTTPError("u", 503, "x", {}, None)))


class RetryCallTests(unittest.TestCase):
    def test_retries_a_transient_failure_then_returns(self) -> None:
        fn = mock.Mock(side_effect=[_reset(), _reset(), ["row"]], __name__="fetch")
        sleeps: list[float] = []
        self.assertEqual(http.retry_call(fn, 1, sleep=sleeps.append), ["row"])
        self.assertEqual(fn.call_count, 3)
        self.assertEqual(len(sleeps), 2)
        fn.assert_called_with(1)

    def test_a_permanent_error_is_raised_at_once(self) -> None:
        fn = mock.Mock(side_effect=ValueError("bad query"), __name__="fetch")
        with self.assertRaises(ValueError):
            http.retry_call(fn, sleep=lambda s: None)
        self.assertEqual(fn.call_count, 1)

    def test_attempts_are_bounded_and_the_last_error_propagates(self) -> None:
        fn = mock.Mock(side_effect=[_reset()] * 9, __name__="fetch")
        with self.assertRaises(requests.ConnectionError):
            http.retry_call(fn, attempts=3, sleep=lambda s: None)
        self.assertEqual(fn.call_count, 3)


class RequestTests(unittest.TestCase):
    def _run(self, side_effect, **kw):
        sleeps: list[float] = []
        with mock.patch.object(http.requests, "request", side_effect=side_effect) as req:
            try:
                out = http.request("GET", "https://api.example/v1/x?token=s",
                                   sleep=sleeps.append, **kw)
            except Exception as exc:  # noqa: BLE001
                out = exc
        return req, sleeps, out

    def test_transport_errors_and_transient_statuses_are_retried(self) -> None:
        req, sleeps, out = self._run([_reset(), _Resp(503), _Resp(200)])
        self.assertEqual(out.status_code, 200)
        self.assertEqual((req.call_count, len(sleeps)), (3, 2))

    def test_retry_after_is_honoured_and_capped(self) -> None:
        _, sleeps, _ = self._run([_Resp(429, {"Retry-After": "7"}), _Resp(200)])
        self.assertEqual(sleeps, [7.0])
        _, sleeps, _ = self._run([_Resp(429, {"Retry-After": "9999"}), _Resp(200)])
        self.assertEqual(sleeps, [http.MAX_RETRY_AFTER])

    def test_a_permanent_status_is_returned_for_the_caller_to_judge(self) -> None:
        req, sleeps, out = self._run([_Resp(400)])
        self.assertEqual((out.status_code, req.call_count, sleeps), (400, 1, []))

    def test_the_last_transient_status_is_returned_when_attempts_run_out(self) -> None:
        req, _, out = self._run([_Resp(502)] * 9, attempts=3)
        self.assertEqual((out.status_code, req.call_count), (502, 3))

    def test_persistent_transport_failure_raises(self) -> None:
        req, _, out = self._run([_reset()] * 9, attempts=2)
        self.assertIsInstance(out, requests.ConnectionError)
        self.assertEqual(req.call_count, 2)

    def test_a_timeout_is_always_set(self) -> None:
        req, _, _ = self._run([_Resp(200)])
        self.assertEqual(req.call_args.kwargs["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
