"""Hermetic tests for shopify_customers_sync.py — no network, no live warehouse.db.

Covers: schema creation (and the NO-PII guarantee), pure parsing of bulk JSONL
customer/metafield nodes, the parent/child batching logic, the diff-before-
overwrite change-log behavior, and the Bulk Operations submit/poll flow (all
HTTP mocked).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unittest
from unittest.mock import patch

import shopify_customers_sync as sc

# Column names are split on '_' into whole tokens and checked for an EXACT
# match against these — a substring check would false-positive on innocuous
# columns like `namespace` (contains "name") or `email_consent` (contains
# "email" but stores a consent STATE, not an address). Whole-token matching
# still catches the columns that would actually matter: `email`, `first_name`,
# `last_name`, `phone`, `address`, `city`, `zip`, etc.
_PII_TOKENS = {"email", "name", "firstname", "lastname", "phone", "address",
              "street", "zip", "postal", "city"}
# Columns that legitimately contain a PII-shaped token but store a STATE, not
# contact information — documented in the module docstring as the reason this
# connector is still NO-PII despite the column name.
_ALLOWED_PII_SHAPED_COLUMNS = {"email_consent", "email_consent_at"}


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _full_customer(**overrides) -> dict:
    """A parse_customer-shaped row with every column present (store_customers
    binds all of _CUSTOMER_COLUMNS by name), defaulting anything not passed."""
    row = {c: None for c in sc._CUSTOMER_COLUMNS}
    row.update(overrides)
    return row


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_all_three_tables(self) -> None:
        conn = _make_conn()
        sc.ensure_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("shopify_customers", tables)
        self.assertIn("shopify_customer_metafields", tables)
        self.assertIn("shopify_customer_flag_history", tables)

    def test_ensure_schema_is_idempotent(self) -> None:
        conn = _make_conn()
        sc.ensure_schema(conn)
        sc.ensure_schema(conn)  # must not raise on a second call

    def test_no_pii_columns_anywhere_in_this_connectors_schema(self) -> None:
        """THE load-bearing privacy test. If a future edit ever adds an email,
        name, phone, or address column to any table this script owns, this
        must fail loudly rather than let PII slip into the schema quietly."""
        conn = _make_conn()
        sc.ensure_schema(conn)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertTrue(tables, "ensure_schema created no tables at all")
        offenders = []
        for table in tables:
            for col in conn.execute(f"PRAGMA table_info({table})"):
                col_name = col[1].lower()
                if col_name in _ALLOWED_PII_SHAPED_COLUMNS:
                    continue
                tokens = set(col_name.split("_"))
                hit = tokens & _PII_TOKENS
                if hit:
                    offenders.append(f"{table}.{col[1]} (matched {sorted(hit)})")
        self.assertEqual(offenders, [],
                         f"PII-shaped column(s) found: {offenders}")

    def test_query_doc_never_requests_pii_fields(self) -> None:
        """The GraphQL selection set itself must never ask for contact fields,
        even if the schema were somehow safe despite it. Tokenizes the doc
        (rather than a bare substring check) so `emailMarketingConsent` — a
        consent STATE field, not an address, and expected to be present —
        doesn't false-positive a check aimed at a standalone `email` field."""
        doc = sc._query_doc()
        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", doc))
        forbidden_fields = {"email", "firstName", "lastName", "phone", "address",
                           "addresses", "defaultAddress", "displayName"}
        hit = tokens & forbidden_fields
        self.assertEqual(hit, set())
        # emailMarketingConsent (a consent STATE, not an address) is fine and
        # expected to be present as its own single token.
        self.assertIn("emailMarketingConsent", tokens)


class ParseCustomerTests(unittest.TestCase):
    def test_parse_customer_basic_fields(self) -> None:
        node = {
            "id": "gid://shopify/Customer/6650756268289",
            "state": "ENABLED",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-02-01T00:00:00Z",
            "tags": ["vip", "newsletter"],
            "numberOfOrders": 5,
            "amountSpent": {"amount": "123.45", "currencyCode": "USD"},
            "emailMarketingConsent": {"marketingState": "SUBSCRIBED",
                                      "consentUpdatedAt": "2024-01-15T00:00:00Z"},
            "smsMarketingConsent": {"marketingState": "NOT_SUBSCRIBED",
                                    "consentUpdatedAt": None},
        }
        row = sc.parse_customer(node)
        self.assertEqual(row["customer_id"], "6650756268289")
        self.assertEqual(row["state"], "ENABLED")
        self.assertEqual(row["tags"], "vip, newsletter")
        self.assertEqual(row["number_of_orders"], 5)
        self.assertEqual(row["amount_spent"], 123.45)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["email_consent"], "SUBSCRIBED")
        self.assertEqual(row["sms_consent"], "NOT_SUBSCRIBED")
        # No PII key present in the parsed row at all.
        for forbidden_key in ("email", "phone", "first_name", "last_name", "address"):
            self.assertNotIn(forbidden_key, row)

    def test_parse_customer_handles_missing_optional_blocks(self) -> None:
        node = {"id": "gid://shopify/Customer/1", "tags": None}
        row = sc.parse_customer(node)
        self.assertIsNone(row["tags"])
        self.assertIsNone(row["amount_spent"])
        self.assertIsNone(row["email_consent"])

    def test_parse_customer_zero_amount_spent_is_not_none(self) -> None:
        node = {"id": "gid://shopify/Customer/1",
                "amountSpent": {"amount": "0.00", "currencyCode": "USD"}}
        row = sc.parse_customer(node)
        self.assertEqual(row["amount_spent"], 0.0)


class ParseMetafieldTests(unittest.TestCase):
    def test_parse_metafield_is_unfiltered(self) -> None:
        node = {"namespace": "custom", "key": "favorite_color", "value": "blue",
                "type": "single_line_text_field", "updatedAt": "2024-03-01T00:00:00Z"}
        row = sc.parse_metafield(node, "gid://shopify/Customer/42")
        self.assertEqual(row["customer_id"], "42")
        self.assertEqual(row["namespace"], "custom")
        self.assertEqual(row["key"], "favorite_color")
        self.assertEqual(row["value"], "blue")


class IterCustomerBatchesTests(unittest.TestCase):
    """Mocks requests.get to feed fixed JSONL, since bulk results are streamed."""

    def _fake_stream(self, lines: list[dict]):
        text_lines = [json.dumps(o) for o in lines]

        class _FakeResp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def raise_for_status(self_inner):
                pass

            def iter_lines(self_inner, decode_unicode=True):
                return iter(text_lines)

        return lambda *a, **kw: _FakeResp()

    def test_customer_and_its_metafields_grouped_together(self) -> None:
        lines = [
            {"id": "gid://shopify/Customer/1", "state": "ENABLED", "tags": ["a"]},
            {"__parentId": "gid://shopify/Customer/1", "namespace": "custom",
             "key": "k1", "value": "v1", "type": "single_line_text_field"},
            {"id": "gid://shopify/Customer/2", "state": "DISABLED", "tags": []},
        ]
        with patch.object(sc.requests, "get", self._fake_stream(lines)):
            customers, metafields = sc.parse_jsonl("https://example.test/bulk.jsonl")
        self.assertEqual(len(customers), 2)
        self.assertEqual(len(metafields), 1)
        self.assertEqual(metafields[0]["customer_id"], "1")

    def test_batches_never_split_a_customer_from_its_metafields(self) -> None:
        # batch_size=1 forces a cut after every root line; the metafield line
        # immediately following its parent must still land in the SAME batch.
        lines = [
            {"id": "gid://shopify/Customer/1", "state": "ENABLED"},
            {"__parentId": "gid://shopify/Customer/1", "namespace": "ns", "key": "k",
             "value": "v"},
            {"id": "gid://shopify/Customer/2", "state": "ENABLED"},
            {"__parentId": "gid://shopify/Customer/2", "namespace": "ns", "key": "k",
             "value": "v2"},
        ]
        with patch.object(sc.requests, "get", self._fake_stream(lines)):
            batches = list(sc.iter_customer_batches("https://example.test/bulk.jsonl",
                                                     batch_size=1))
        # 2 customers, batch_size=1 -> 2 batches, each with its own metafield.
        self.assertEqual(len(batches), 2)
        for customers, metafields in batches:
            self.assertEqual(len(customers), 1)
            self.assertEqual(len(metafields), 1)
            self.assertEqual(customers[0]["customer_id"], metafields[0]["customer_id"])

    def test_non_customer_non_child_lines_are_ignored(self) -> None:
        lines = [{"id": "gid://shopify/SomethingElse/9"}]
        with patch.object(sc.requests, "get", self._fake_stream(lines)):
            customers, metafields = sc.parse_jsonl("https://example.test/bulk.jsonl")
        self.assertEqual(customers, [])
        self.assertEqual(metafields, [])

    def test_empty_stream_yields_nothing(self) -> None:
        with patch.object(sc.requests, "get", self._fake_stream([])):
            batches = list(sc.iter_customer_batches("https://example.test/bulk.jsonl"))
        self.assertEqual(batches, [])


class RecordFlagChangesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_conn()
        sc.ensure_schema(self.conn)

    def test_first_seen_customer_logs_a_baseline_row(self) -> None:
        customers = [{"customer_id": "1", "tags": "vip", "state": "ENABLED"}]
        n = sc.record_flag_changes(self.conn, customers, "2026-01-01")
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT tags, state FROM shopify_customer_flag_history WHERE customer_id='1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("vip", "ENABLED"))

    def test_no_change_logs_nothing_on_second_run(self) -> None:
        customers = [_full_customer(customer_id="1", tags="vip", state="ENABLED")]
        sc.record_flag_changes(self.conn, customers, "2026-01-01")
        sc.store_customers(self.conn, customers)
        # identical snapshot on a later run -> no new change-log row
        n = sc.record_flag_changes(self.conn, customers, "2026-01-08")
        self.assertEqual(n, 0)

    def test_tag_change_is_detected(self) -> None:
        before = [_full_customer(customer_id="1", tags="vip", state="ENABLED")]
        sc.record_flag_changes(self.conn, before, "2026-01-01")
        sc.store_customers(self.conn, before)
        after = [_full_customer(customer_id="1", tags="vip, lapsed", state="ENABLED")]
        n = sc.record_flag_changes(self.conn, after, "2026-01-08")
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT tags FROM shopify_customer_flag_history "
            "WHERE customer_id='1' AND observed_date='2026-01-08'").fetchone()
        self.assertEqual(row[0], "vip, lapsed")

    def test_state_change_is_detected(self) -> None:
        before = [_full_customer(customer_id="1", tags="vip", state="ENABLED")]
        sc.record_flag_changes(self.conn, before, "2026-01-01")
        sc.store_customers(self.conn, before)
        after = [_full_customer(customer_id="1", tags="vip", state="DISABLED")]
        n = sc.record_flag_changes(self.conn, after, "2026-01-08")
        self.assertEqual(n, 1)

    def test_must_run_before_store_customers_or_it_sees_no_diff(self) -> None:
        """Pins the ordering constraint documented in the module: diffing
        AFTER the overwrite compares a snapshot to itself."""
        before = [_full_customer(customer_id="1", tags="vip", state="ENABLED")]
        sc.record_flag_changes(self.conn, before, "2026-01-01")
        sc.store_customers(self.conn, before)

        after = [_full_customer(customer_id="1", tags="vip, lapsed", state="ENABLED")]
        sc.store_customers(self.conn, after)          # overwrite FIRST (wrong order)
        n = sc.record_flag_changes(self.conn, after, "2026-01-08")
        self.assertEqual(n, 0, "diffing after the overwrite must see no change")

    def test_customer_without_id_is_skipped(self) -> None:
        customers = [{"customer_id": None, "tags": "x", "state": "ENABLED"}]
        n = sc.record_flag_changes(self.conn, customers, "2026-01-01")
        self.assertEqual(n, 0)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_conn()
        sc.ensure_schema(self.conn)

    def test_store_customers_round_trip(self) -> None:
        rows = [sc.parse_customer({"id": "gid://shopify/Customer/1", "state": "ENABLED",
                                   "tags": ["a", "b"]})]
        n = sc.store_customers(self.conn, rows)
        self.assertEqual(n, 1)
        got = self.conn.execute(
            "SELECT customer_id, state, tags FROM shopify_customers").fetchone()
        self.assertEqual(tuple(got), ("1", "ENABLED", "a, b"))

    def test_store_customers_upserts_on_conflict(self) -> None:
        rows = [{"customer_id": "1", "state": "ENABLED", "created_at": None,
                "updated_at": None, "tags": "a", "email_consent": None,
                "email_consent_at": None, "sms_consent": None, "sms_consent_at": None,
                "number_of_orders": 1, "amount_spent": 1.0, "currency": "USD"}]
        sc.store_customers(self.conn, rows)
        rows[0] = {**rows[0], "state": "DISABLED"}
        sc.store_customers(self.conn, rows)
        count = self.conn.execute("SELECT COUNT(*) FROM shopify_customers").fetchone()[0]
        state = self.conn.execute("SELECT state FROM shopify_customers").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(state, "DISABLED")

    def test_store_metafields_round_trip(self) -> None:
        rows = [sc.parse_metafield({"namespace": "custom", "key": "k", "value": "v",
                                    "type": "single_line_text_field"},
                                   "gid://shopify/Customer/1")]
        n = sc.store_metafields(self.conn, rows)
        self.assertEqual(n, 1)
        got = self.conn.execute(
            "SELECT customer_id, namespace, key, value FROM shopify_customer_metafields"
        ).fetchone()
        self.assertEqual(tuple(got), ("1", "custom", "k", "v"))

    def test_store_empty_rows_is_a_no_op(self) -> None:
        self.assertEqual(sc.store_customers(self.conn, []), 0)
        self.assertEqual(sc.store_metafields(self.conn, []), 0)


class CheckEnvTests(unittest.TestCase):
    def test_missing_shop_fails_clearly_not_a_crash(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPIFY_")}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                sc._check_env()
            self.assertIn("SHOPIFY_SHOP", str(cm.exception))

    def test_missing_credentials_fails_clearly(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPIFY_")}
        env["SHOPIFY_SHOP"] = "test.myshopify.com"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                sc._check_env()
            self.assertIn("CLIENT_ID", str(cm.exception))

    def test_client_credentials_present_passes(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPIFY_")}
        env.update({"SHOPIFY_SHOP": "test.myshopify.com",
                   "SHOPIFY_CLIENT_ID": "id", "SHOPIFY_CLIENT_SECRET": "secret"})
        with patch.dict(os.environ, env, clear=True):
            sc._check_env()  # must not raise

    def test_legacy_static_token_passes(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPIFY_")}
        env.update({"SHOPIFY_SHOP": "test.myshopify.com",
                   "SHOPIFY_ADMIN_TOKEN": "shpat_x"})
        with patch.dict(os.environ, env, clear=True):
            sc._check_env()  # must not raise


class RequireScopeTests(unittest.TestCase):
    def test_scope_not_granted_raises_clear_systemexit(self) -> None:
        with patch.object(sc.shopify, "customer_capture_enabled", return_value=False):
            with self.assertRaises(SystemExit) as cm:
                sc.require_scope()
            self.assertIn("read_customers", str(cm.exception))

    def test_scope_granted_does_not_raise(self) -> None:
        with patch.object(sc.shopify, "customer_capture_enabled", return_value=True):
            sc.require_scope()  # must not raise


class _FakeHttpResp:
    def __init__(self, status: int = 200, payload: object | None = None,
                headers: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self) -> object:
        return self._payload


class BulkOperationsFlowTests(unittest.TestCase):
    """Mocks the Bulk Operations submit/poll HTTP flow end to end."""

    def setUp(self) -> None:
        self._shop_env = os.environ.get("SHOPIFY_SHOP")
        os.environ["SHOPIFY_SHOP"] = "test.myshopify.com"
        self._token = sc.shopify._access_token
        sc.shopify._access_token = lambda shop: "tok"
        self._sleep = sc.time.sleep
        sc.time.sleep = lambda *_: None  # no real waiting in tests

    def tearDown(self) -> None:
        sc.shopify._access_token = self._token
        sc.time.sleep = self._sleep
        if self._shop_env is None:
            os.environ.pop("SHOPIFY_SHOP", None)
        else:
            os.environ["SHOPIFY_SHOP"] = self._shop_env

    def test_submit_success_returns_url_and_object_count(self) -> None:
        responses = [
            _FakeHttpResp(200, {"data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"},
                "userErrors": []}}}),
            _FakeHttpResp(200, {"data": {"currentBulkOperation": {
                "id": "gid://shopify/BulkOperation/1", "status": "COMPLETED",
                "errorCode": None, "objectCount": "42",
                "url": "https://example.test/bulk-result.jsonl"}}}),
        ]
        calls = iter(responses)
        with patch.object(sc.requests, "post", lambda *a, **kw: next(calls)):
            url, count = sc._submit(sc._query_doc())
        self.assertEqual(url, "https://example.test/bulk-result.jsonl")
        self.assertEqual(count, 42)

    def test_submit_waits_out_a_running_op_then_polls(self) -> None:
        responses = [
            _FakeHttpResp(200, {"data": {"currentBulkOperation": {
                "id": "gid://shopify/BulkOperation/1", "status": "RUNNING",
                "errorCode": None, "objectCount": "0", "url": None}}}),
            _FakeHttpResp(200, {"data": {"currentBulkOperation": {
                "id": "gid://shopify/BulkOperation/1", "status": "COMPLETED",
                "errorCode": None, "objectCount": "1", "url": "https://example.test/x.jsonl"}}}),
        ]
        calls = iter(responses)
        with patch.object(sc.requests, "post", lambda *a, **kw: next(calls)):
            result = sc._wait_for("gid://shopify/BulkOperation/1")
        self.assertEqual(result["status"], "COMPLETED")

    def test_current_bulk_returns_none_gracefully_when_absent(self) -> None:
        resp = _FakeHttpResp(200, {"data": {"currentBulkOperation": None}})
        with patch.object(sc.requests, "post", lambda *a, **kw: resp):
            self.assertIsNone(sc._current_bulk())

    def test_bulk_op_failed_status_raises(self) -> None:
        resp = _FakeHttpResp(200, {"data": {"currentBulkOperation": {
            "id": "gid://shopify/BulkOperation/1", "status": "FAILED",
            "errorCode": "INTERNAL_SERVER_ERROR", "objectCount": "0", "url": None}}})
        with patch.object(sc.requests, "post", lambda *a, **kw: resp):
            with self.assertRaises(RuntimeError) as cm:
                sc._wait_for("gid://shopify/BulkOperation/1")
        self.assertIn("FAILED", str(cm.exception))

    def test_hard_graphql_error_raises_immediately(self) -> None:
        resp = _FakeHttpResp(200, {"errors": [{"message": "Field does not exist"}]})
        with patch.object(sc.requests, "post", lambda *a, **kw: resp):
            with self.assertRaises(RuntimeError) as cm:
                sc._gql("{ bogus }")
        self.assertIn("does not exist", str(cm.exception))

    def test_throttled_graphql_error_is_retried(self) -> None:
        responses = [
            _FakeHttpResp(200, {"errors": [{"extensions": {"code": "THROTTLED"}}]}),
            _FakeHttpResp(200, {"data": {"ok": True}}),
        ]
        calls = iter(responses)
        with patch.object(sc.requests, "post", lambda *a, **kw: next(calls)):
            data = sc._gql("{ ok }")
        self.assertEqual(data, {"ok": True})


if __name__ == "__main__":
    unittest.main()
