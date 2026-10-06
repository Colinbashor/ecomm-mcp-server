"""Hermetic tests for the Shopify-driven direct fetch in flexport_orders_sync.py.

No network, no real DB: Flexport requests and the Shopify GraphQL call are faked.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import flexport_orders_sync as fx


def _fo(fo_id, location="3PL Warehouse", status="CLOSED", request="ACCEPTED"):
    return {"id": f"gid://shopify/FulfillmentOrder/{fo_id}", "status": status,
            "requestStatus": request, "assignedLocation": {"name": location}}


def _order(name, *fos):
    return {"name": name, "fulfillmentOrders": {"nodes": list(fos)}}


class ExternalIdTests(unittest.TestCase):
    def test_builds_id_from_name_marker_and_numeric_fo_id(self):
        ids = fx.flexport_external_ids([_order("#1001", _fo(555))], "3PL Warehouse", "SPLIT")
        self.assertEqual(ids, [("#1001SPLIT555", "CLOSED")])

    def test_no_marker_uses_order_name_only(self):
        ids = fx.flexport_external_ids([_order("#1001", _fo(555))], "3PL Warehouse")
        self.assertEqual(ids, [("#1001", "CLOSED")])

    def test_skips_other_locations_cancelled_and_unaccepted(self):
        nodes = [_order("#1", _fo(1, location="Dropship"),
                        _fo(2, status="CANCELLED"),
                        _fo(3, request="UNSUBMITTED"),
                        _fo(4))]
        self.assertEqual([i for i, _ in fx.flexport_external_ids(nodes, "3PL Warehouse")],
                         ["#1"])

    def test_path_percent_encodes_slashes(self):
        self.assertEqual(fx.external_id_path("A/1 B"), "/orders/external_id/A%2F1%20B")


class NotFoundTests(unittest.TestCase):
    def test_404_raises_not_found_which_is_a_runtimeerror(self):
        resp = mock.Mock(status_code=404, text="nope", headers={})
        with mock.patch.dict(os.environ, {"FLEXPORT_API_TOKEN": "t"}), \
             mock.patch.object(fx.requests, "get", return_value=resp):
            with self.assertRaises(fx.FlexportNotFound):
                fx._request("/orders/external_id/x", {})
        self.assertTrue(issubclass(fx.FlexportNotFound, RuntimeError))

    def test_fetch_by_external_id_maps_outcomes(self):
        with mock.patch.object(fx, "_request", side_effect=fx.FlexportNotFound("x")):
            self.assertEqual(fx._fetch_by_external_id(("a", "CLOSED"))[2], "not_found")
        with mock.patch.object(fx, "_request", side_effect=RuntimeError("boom")):
            self.assertEqual(fx._fetch_by_external_id(("a", "CLOSED"))[2], "error")
        ok = mock.Mock()
        ok.json.return_value = {"id": 7}
        with mock.patch.object(fx, "_request", return_value=ok):
            self.assertEqual(fx._fetch_by_external_id(("a", "CLOSED"))[2:], ("ok", {"id": 7}))


class RunFromShopifyTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        self.logged = {}
        env = mock.patch.dict(os.environ, {"FLEXPORT_SHOPIFY_LOCATION": "3PL Warehouse"})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, pairs, n_shop, fetch):
        path = self.path
        with mock.patch.object(fx.db, "connect", lambda: sqlite3.connect(path)), \
             mock.patch.object(fx.db, "now", return_value="t"), \
             mock.patch.object(fx.db, "log_sync",
                               side_effect=lambda *a, **k: self.logged.update(status=a[3], msg=a[4])), \
             mock.patch.object(fx, "_shopify_flexport_ids", return_value=(n_shop, pairs)), \
             mock.patch.object(fx, "_fetch_by_external_id", side_effect=fetch):
            return fx.run_from_shopify(10, 2)

    def _order_json(self, oid, ext, cost):
        return {"id": oid, "externalOrderId": ext, "cost": cost, "state": {},
                "lineItems": [{"quantity": 1}], "shipments": []}

    def test_stores_fetched_orders_and_logs_ok(self):
        pairs = [("A", "CLOSED"), ("B", "CLOSED")]
        fetch = lambda item: (item[0], item[1], "ok",
                              self._order_json(1 if item[0] == "A" else 2, item[0], 4.5))
        self.assertEqual(self._run(pairs, 2, fetch), 0)
        self.assertEqual(self.logged["status"], "ok")
        c = sqlite3.connect(self.path)
        n = c.execute("SELECT COUNT(*) FROM flexport_order_costs").fetchone()[0]
        c.close()
        self.assertEqual(n, 2)

    def test_empty_pull_is_degraded_not_ok(self):
        self.assertEqual(self._run([], 0, lambda i: None), 75)
        self.assertEqual(self.logged["status"], "degraded")

    def test_high_404_rate_on_accepted_orders_is_degraded(self):
        pairs = [(f"X{i}", "CLOSED") for i in range(30)]
        fetch = lambda item: (item[0], item[1], "not_found", None)
        self.assertEqual(self._run(pairs, 30, fetch), 75)
        self.assertIn("RULE MAY HAVE CHANGED", self.logged["msg"])

    def test_missing_location_env_refuses(self):
        with mock.patch.dict(os.environ, {"FLEXPORT_SHOPIFY_LOCATION": ""}):
            self.assertEqual(fx.run_from_shopify(10, 2), 1)


if __name__ == "__main__":
    unittest.main()
