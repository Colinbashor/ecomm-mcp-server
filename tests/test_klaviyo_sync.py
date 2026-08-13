"""Hermetic tests for klaviyo_sync.py - no network, no real warehouse.db.

Covers: schema creation, the rate-derivation helpers, campaign report-row
aggregation (multiple message/variation rows rolling up to one campaign row),
the conversion-metric gating, and the HTTP retry contract (connection drop /
429 Retry-After / hard 4xx), mirroring the retry tests already written for
the Shopify connector.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import klaviyo_sync as ks


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        ks.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(
            {"klaviyo_campaigns", "klaviyo_flows", "klaviyo_audience_growth",
             "klaviyo_attributed_daily"} <= tables)

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        ks.ensure_schema(conn)
        ks.ensure_schema(conn)  # must not raise on a second call


class RateHelperTests(unittest.TestCase):
    def test_rates_handles_zero_delivered(self) -> None:
        self.assertEqual(ks._rates(0, 10, 5, 2), (0.0, 0.0, 0.0))

    def test_rates_computes_expected_ratios(self) -> None:
        open_r, click_r, conv_r = ks._rates(100, 20, 10, 5)
        self.assertAlmostEqual(open_r, 0.2)
        self.assertAlmostEqual(click_r, 0.1)
        self.assertAlmostEqual(conv_r, 0.05)

    def test_derived_handles_zero_recipients_and_conversions(self) -> None:
        rpr, aov, ctor = ks._derived(0.0, 0, 0, 0, 0)
        self.assertEqual((rpr, aov, ctor), (0.0, 0.0, 0.0))

    def test_derived_computes_expected_values(self) -> None:
        rpr, aov, ctor = ks._derived(500.0, 1000, 25, 200, 50)
        self.assertAlmostEqual(rpr, 0.5)
        self.assertAlmostEqual(aov, 20.0)
        self.assertAlmostEqual(ctor, 0.25)

    def test_channel_name_strips_dollar_sign_and_suffix(self) -> None:
        self.assertEqual(ks._channel_name("$email_channel"), "email")
        self.assertEqual(ks._channel_name("$sms_channel"), "sms")

    def test_channel_name_defaults_to_unattributed(self) -> None:
        self.assertEqual(ks._channel_name(""), "unattributed")
        self.assertEqual(ks._channel_name(None), "unattributed")


class ConversionMetricGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original = ks.CONVERSION_METRIC

    def tearDown(self) -> None:
        ks.CONVERSION_METRIC = self._original

    def test_missing_metric_id_is_reported_and_returns_false(self) -> None:
        ks.CONVERSION_METRIC = None
        self.assertFalse(ks._require_conversion_metric())

    def test_present_metric_id_returns_true(self) -> None:
        ks.CONVERSION_METRIC = "TEST_METRIC_ID"
        self.assertTrue(ks._require_conversion_metric())


class LoadCampaignsAggregationTests(unittest.TestCase):
    """The campaign-values report can split one campaign across several
    message/variation rows; load_campaigns must roll them up to one row
    keyed on campaign_id, with rates/derived stats recomputed post-rollup."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        ks.ensure_schema(self.conn)
        self._metric = ks.CONVERSION_METRIC
        ks.CONVERSION_METRIC = "TEST_METRIC"

    def tearDown(self) -> None:
        ks.CONVERSION_METRIC = self._metric
        self.conn.close()

    @patch.object(ks, "_request")
    def test_two_message_rows_roll_up_to_one_campaign_row(self, mock_request) -> None:
        mock_request.return_value = {"data": {"attributes": {"results": [
            {"groupings": {"campaign_id": "C1", "send_channel": "email"},
             "statistics": {"recipients": 100, "delivered": 90, "opens_unique": 40,
                            "clicks_unique": 10, "conversions": 3, "conversion_uniques": 3,
                            "unsubscribes": 1, "bounced": 2, "spam_complaints": 0,
                            "conversion_value": 150.0}},
            {"groupings": {"campaign_id": "C1", "send_channel": "email"},
             "statistics": {"recipients": 50, "delivered": 45, "opens_unique": 20,
                            "clicks_unique": 5, "conversions": 1, "conversion_uniques": 1,
                            "unsubscribes": 0, "bounced": 0, "spam_complaints": 0,
                            "conversion_value": 50.0}},
        ]}}}
        n = ks.load_campaigns(
            MagicMock(), self.conn, {"key": "last_30_days"},
            meta={"C1": {"name": "Test Campaign", "status": "Sent", "send_time": None}})
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT recipients, delivered, conversions, revenue, name "
            "FROM klaviyo_campaigns WHERE campaign_id='C1'").fetchone()
        self.assertEqual(row, (150, 135, 4, 200.0, "Test Campaign"))

    @patch.object(ks, "_request")
    def test_rows_missing_a_campaign_id_are_skipped(self, mock_request) -> None:
        mock_request.return_value = {"data": {"attributes": {"results": [
            {"groupings": {}, "statistics": {}},
        ]}}}
        n = ks.load_campaigns(MagicMock(), self.conn, {"key": "last_30_days"}, meta={})
        self.assertEqual(n, 0)

    @patch.object(ks, "_request")
    def test_upsert_is_idempotent_on_campaign_id(self, mock_request) -> None:
        payload = {"data": {"attributes": {"results": [
            {"groupings": {"campaign_id": "C1", "send_channel": "email"},
             "statistics": {"recipients": 10, "delivered": 10, "opens_unique": 5,
                            "clicks_unique": 2, "conversions": 1, "conversion_uniques": 1,
                            "unsubscribes": 0, "bounced": 0, "spam_complaints": 0,
                            "conversion_value": 20.0}},
        ]}}}
        mock_request.return_value = payload
        ks.load_campaigns(MagicMock(), self.conn, {"key": "last_30_days"}, meta={})
        ks.load_campaigns(MagicMock(), self.conn, {"key": "last_30_days"}, meta={})
        count = self.conn.execute(
            "SELECT COUNT(*) FROM klaviyo_campaigns WHERE campaign_id='C1'").fetchone()[0]
        self.assertEqual(count, 1)


class _TimeShim:
    """Stands in for the `time` module inside klaviyo_sync so retry backoff
    costs no wall-clock, mirroring the shim used for the Shopify connector's
    tests. Rebinding `ks.time` only affects name lookups inside this module,
    not the real stdlib `time` module other tests may rely on."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class RequestRetryTests(unittest.TestCase):
    """Same contract as the Shopify connector's transport tests: a dropped
    connection or 5xx is retried, a 429 honours Retry-After, and a genuine
    4xx surfaces immediately with the response body rather than retrying."""

    def setUp(self) -> None:
        self._time = ks.time
        ks.time = _TimeShim()

    def tearDown(self) -> None:
        ks.time = self._time

    def test_connection_error_is_retried_then_succeeds(self) -> None:
        session = MagicMock()
        calls = {"n": 0}

        def fake_request(method, url, json=None, params=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ks.requests.exceptions.ConnectionError("boom")
            resp = MagicMock(status_code=200, ok=True)
            resp.json.return_value = {"data": []}
            return resp

        session.request.side_effect = fake_request
        result = ks._request(session, "GET", "/campaigns/")
        self.assertEqual(result, {"data": []})
        self.assertEqual(calls["n"], 2)

    def test_429_honours_retry_after_header(self) -> None:
        session = MagicMock()
        resp_429 = MagicMock(status_code=429, headers={"Retry-After": "3"})
        resp_ok = MagicMock(status_code=200, ok=True)
        resp_ok.json.return_value = {"data": []}
        session.request.side_effect = [resp_429, resp_ok]
        result = ks._request(session, "GET", "/campaigns/")
        self.assertEqual(result, {"data": []})
        self.assertEqual(ks.time.slept, [3.0])

    def test_hard_4xx_raises_immediately_with_response_body(self) -> None:
        session = MagicMock()
        resp = MagicMock(status_code=400, ok=False, text="missing flow_message_id in group_by")
        session.request.return_value = resp
        with self.assertRaises(RuntimeError) as cm:
            ks._request(session, "POST", "/flow-values-reports/")
        self.assertIn("missing flow_message_id", str(cm.exception))
        self.assertEqual(session.request.call_count, 1)

    def test_5xx_is_retried_then_succeeds(self) -> None:
        session = MagicMock()
        resp_500 = MagicMock(status_code=500)
        resp_ok = MagicMock(status_code=200, ok=True)
        resp_ok.json.return_value = {"data": []}
        session.request.side_effect = [resp_500, resp_ok]
        result = ks._request(session, "GET", "/campaigns/")
        self.assertEqual(result, {"data": []})


class RunSkipTests(unittest.TestCase):
    """run() must degrade to a clean, non-raising SKIPPED when credentials or
    the conversion-metric id are missing - never crash a scheduled job."""

    def test_skips_without_api_key(self) -> None:
        with patch.dict(ks.os.environ, {}, clear=False):
            ks.os.environ.pop("KLAVIYO_API_KEY", None)
            result = ks.run()
        self.assertEqual(result, 0)

    def test_skips_without_conversion_metric(self) -> None:
        original_metric = ks.CONVERSION_METRIC
        ks.CONVERSION_METRIC = None
        try:
            with patch.dict(ks.os.environ, {"KLAVIYO_API_KEY": "pk_test"}, clear=False):
                result = ks.run()
        finally:
            ks.CONVERSION_METRIC = original_metric
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
