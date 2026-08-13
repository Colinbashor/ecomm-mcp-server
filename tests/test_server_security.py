from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import unittest
from unittest import mock

import server

HOSTNAME = "warehouse-host"
FQDN = f"{HOSTNAME}.corp.example.com"
LAN = "10.0.0.42"
DEFAULT_NAMES = frozenset({HOSTNAME, FQDN, f"{HOSTNAME}.local", "localhost"})


def _policy(names=DEFAULT_NAMES, origins=()) -> server.HostPolicy:
    return server.HostPolicy(frozenset(names), frozenset(origins))


class _Clock:
    """Injectable monotonic clock so no test ever sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _guard(names=DEFAULT_NAMES, origins=(), *, accept_any=False,
           resolver=None, clock=None) -> server.HostGuard:
    return server.HostGuard(
        _policy(names, origins),
        accept_any=accept_any,
        # Default resolver would hit DNS; tests must never touch the network.
        resolver=resolver or (lambda: _policy(names, origins)),
        clock=clock or _Clock(),
    )


def _scope(host=None, *, origin=None, local=(LAN, 8787), client=("10.0.0.7", 5001),
           token=b"Bearer secret", extra_headers=()):
    headers = []
    if token is not None:
        headers.append((b"authorization", token))
    if host is not None:
        headers.append((b"host", host if isinstance(host, bytes) else host.encode()))
    if origin is not None:
        headers.append((b"origin", origin if isinstance(origin, bytes) else origin.encode()))
    headers.extend(extra_headers)
    scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp", "headers": headers}
    if local is not None:
        scope["server"] = list(local)
    if client is not None:
        scope["client"] = list(client)
    return scope


def _drive(guard: server.HostGuard, scope: dict, loop=None) -> int:
    """Run one request through the real middleware; return the HTTP status.

    `loop` lets a caller build the event loop BEFORE patching socket internals —
    asyncio's proactor loop creates a socketpair, so patching socket.socket
    first would break loop construction rather than testing the request path.
    """
    sent: list[dict] = []

    async def app(scope_, receive_, send):
        del scope_, receive_
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    middleware = server.BearerTokenMiddleware(app, "secret", guard)
    if loop is None:
        asyncio.run(middleware(scope, receive, send))
    else:
        loop.run_until_complete(middleware(scope, receive, send))
    return sent[0]["status"]


class HostPolicyNormalizationTests(unittest.TestCase):
    def test_valid_values_normalize(self) -> None:
        cases = {
            "warehouse-host": (HOSTNAME, None),
            "WAREHOUSE-HOST:8787": (HOSTNAME, 8787),
            "Warehouse-Host": (HOSTNAME, None),        # mixed case
            "warehouse-host.": (HOSTNAME, None),       # one trailing dot
            "warehouse-host.:8787": (HOSTNAME, 8787),
            "10.0.0.42:8787": (LAN, 8787),
            "[::1]:8787": ("::1", 8787),
            "[::1]": ("::1", None),
            "::1": ("::1", None),
            "[::ffff:10.0.0.42]": (LAN, None),         # v4-mapped collapses
            b"warehouse-host:443": (HOSTNAME, 443),
        }
        for raw, expected in cases.items():
            self.assertEqual(server.split_host_header(raw), expected, raw)

    def test_malformed_values_are_rejected(self) -> None:
        for raw in ("", "   ", "a:b", "a:0", "a:99999", "a:123456", "a b", "a/b",
                    "a:1:2", "[::1", "user@a", "a\r\nb", "a\x00b", "host?x",
                    "host#x", "x" * 300, ".", "a\\b",
                    "host:", "[::1]:",                 # empty port
                    "\xa0host\xa0", "host\xa0evil",    # unicode whitespace
                    "fe80::1%eth0", "*", "*.example.com", "host%2eevil.com"):
            # NB alternate IP spellings (2130706433, 0x7f000001, 127.1) are legal
            # NAME shapes, so they normalize here and are rejected by
            # host_verdict instead — see the test below.
            self.assertIsNone(server.split_host_header(raw), repr(raw))

    def test_alternate_ip_spellings_are_not_treated_as_addresses(self) -> None:
        """Integer/octal/hex forms must not smuggle in loopback or a stale IP."""
        for raw in ("2130706433", "0x7f000001", "0177.0.0.1", "127.1",
                    "010.000.091.055"):
            parsed = server.split_host_header(raw)
            if parsed is None:
                continue
            self.assertFalse(server.host_verdict(parsed[0], (LAN, 8787), _policy())[0],
                             raw)


class HostVerdictTests(unittest.TestCase):
    def test_connected_ip_is_accepted_and_other_ips_are_not(self) -> None:
        self.assertEqual(server.host_verdict(LAN, (LAN, 8787), _policy()),
                         (True, "connected-ip"))
        self.assertEqual(server.host_verdict("10.0.0.43", (LAN, 8787), _policy()),
                         (False, "ip-not-this-connection"))

    def test_moved_network_needs_no_refresh_or_restart(self) -> None:
        """THE REGRESSION TEST: same policy object, new interface, still works."""
        guard = _guard()
        self.assertEqual(_drive(guard, _scope(f"{LAN}:8787", local=(LAN, 8787))), 200)
        moved = "192.168.4.9"
        self.assertEqual(
            _drive(guard, _scope(f"{moved}:8787", local=(moved, 8787))), 200)
        # ...and the old address is not honoured once it is no longer ours.
        self.assertEqual(
            _drive(guard, _scope(f"{LAN}:8787", local=(moved, 8787))), 421)

    def test_stale_cert_san_ip_is_denied(self) -> None:
        self.assertEqual(
            server.host_verdict("10.0.0.9", (LAN, 8787), _policy()),
            (False, "ip-not-this-connection"))

    def test_loopback_is_always_accepted(self) -> None:
        for name in ("localhost", "127.0.0.1", "::1", "127.0.0.2"):
            ok, reason = server.host_verdict(name, None, _policy())
            self.assertTrue(ok, name)
            self.assertIn(reason, ("loopback-name", "loopback-ip"))

    def test_exact_match_only_no_prefix_or_suffix_rule(self) -> None:
        """Guards against reintroducing a '<hostname>.<anything>' rule."""
        for name in (f"{HOSTNAME}.evil.com", f"{HOSTNAME}x", f"x{HOSTNAME}",
                     f"{HOSTNAME}-evil.com", f"not{HOSTNAME}.com",
                     "corp.example.com", "evil.com",
                     # ...and the other direction: nothing may be accepted just
                     # because it sits UNDER one of our names either.
                     f"sub.{HOSTNAME}", f"evil.{HOSTNAME}", f"x.{FQDN}",
                     "evil.localhost", f"sub.{HOSTNAME}.local"):
            self.assertEqual(server.host_verdict(name, (LAN, 8787), _policy()),
                             (False, "unknown-host"), name)
        for name in (HOSTNAME, FQDN, f"{HOSTNAME}.local"):
            self.assertTrue(server.host_verdict(name, (LAN, 8787), _policy())[0], name)

    def test_missing_local_address_fails_closed_for_ip_hosts(self) -> None:
        self.assertEqual(server.host_verdict(LAN, None, _policy()),
                         (False, "no-local-addr"))
        # A NAME host on that same scope still works — names need no local addr.
        self.assertTrue(server.host_verdict(HOSTNAME, None, _policy())[0])

    def test_configured_ip_is_accepted_even_when_not_the_arrival_address(self) -> None:
        policy = _policy(set(DEFAULT_NAMES) | {"10.9.9.9"})
        self.assertEqual(server.host_verdict("10.9.9.9", (LAN, 8787), policy),
                         (True, "configured-ip"))


class HeaderHandlingTests(unittest.TestCase):
    def test_missing_and_duplicate_host_headers(self) -> None:
        self.assertEqual(_drive(_guard(), _scope(None)), 421)
        self.assertEqual(
            _drive(_guard(), _scope(HOSTNAME,
                                    extra_headers=((b"host", b"evil.com"),))),
            421)

    def test_unauthenticated_hostile_host_is_401_and_logs_nothing(self) -> None:
        guard = _guard()
        with self.assertNoLogs("warehouse.mcp.host"):
            status = _drive(guard, _scope("evil.com", token=None))
        self.assertEqual(status, 401)

    def test_non_http_scopes_do_not_bypass_the_checks(self) -> None:
        """Only lifespan may pass unchecked; a websocket must never reach the app."""
        reached = []

        async def app(scope, receive, send):
            reached.append(scope["type"])
            if scope["type"] == "lifespan":
                return
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return {"type": "websocket.connect"}

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        middleware = server.BearerTokenMiddleware(app, "secret", _guard())
        asyncio.run(middleware(
            {"type": "websocket", "headers": [(b"host", b"evil.com")],
             "server": [LAN, 8787]}, receive, send))
        self.assertEqual(reached, [])
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])

        asyncio.run(middleware({"type": "lifespan", "headers": []}, receive, send))
        self.assertEqual(reached, ["lifespan"])

    def test_x_forwarded_host_cannot_satisfy_the_check(self) -> None:
        status = _drive(_guard(), _scope(
            "evil.com", extra_headers=((b"x-forwarded-host", HOSTNAME.encode()),)))
        self.assertEqual(status, 421)

    def test_bad_token_with_good_host_and_good_token_with_bad_host(self) -> None:
        self.assertEqual(_drive(_guard(), _scope(HOSTNAME, token=b"Bearer nope")), 401)
        self.assertEqual(_drive(_guard(), _scope("evil.com")), 421)


class OriginTests(unittest.TestCase):
    def test_origin_matrix(self) -> None:
        cases = [
            (None, 200),
            (f"https://{HOSTNAME}:8787", 200),
            (f"http://{LAN}:8787", 200),
            (f"https://{FQDN}", 200),
            ("https://evil.example", 403),
            ("null", 403),
            ("file:///x", 403),
            (f"https://user@{HOSTNAME}", 403),
        ]
        for origin, expected in cases:
            self.assertEqual(_drive(_guard(), _scope(HOSTNAME, origin=origin)),
                             expected, origin)

    def test_configured_origin_is_accepted(self) -> None:
        guard = _guard(origins={"https://claude.ai"})
        self.assertEqual(
            _drive(guard, _scope(HOSTNAME, origin="https://claude.ai")), 200)


class PolicyResolutionTests(unittest.TestCase):
    def test_fqdn_accepted_only_when_first_label_matches(self) -> None:
        original = (socket.gethostname, socket.getfqdn)
        try:
            socket.gethostname = lambda: "WAREHOUSE-HOST"
            socket.getfqdn = lambda: FQDN.upper()
            self.assertIn(FQDN, server.resolve_policy().names)
            socket.getfqdn = lambda: "evil.attacker.com"
            with self.assertLogs("warehouse.mcp.host", level="WARNING"):
                policy = server.resolve_policy()
            self.assertNotIn("evil.attacker.com", policy.names)

            def boom():
                raise OSError("no resolver")

            socket.getfqdn = boom
            with self.assertLogs("warehouse.mcp.host", level="WARNING"):
                policy = server.resolve_policy()
            self.assertEqual(policy.names, frozenset({HOSTNAME, f"{HOSTNAME}.local",
                                                      "localhost"}))
        finally:
            socket.gethostname, socket.getfqdn = original

    def test_wildcard_and_junk_entries_are_dropped(self) -> None:
        for entry in ("*", "a/b", "has space", "ftp://x"):
            with self.assertLogs("warehouse.mcp.host", level="WARNING"):
                name, origin = server._split_extra_entry(entry)
            self.assertIsNone(name, entry)
            self.assertIsNone(origin, entry)
        self.assertEqual(server._split_extra_entry("# comment"), (None, None))
        self.assertEqual(server._split_extra_entry("Warehouse.Corp.Local"),
                         ("warehouse.corp.local", None))
        self.assertEqual(server._split_extra_entry("https://claude.ai"),
                         (None, "https://claude.ai"))


class RefreshTests(unittest.TestCase):
    def test_miss_triggers_one_refresh_and_self_heals(self) -> None:
        clock = _Clock()
        calls = []
        names = set(DEFAULT_NAMES)

        def resolver():
            calls.append(clock.now)
            return _policy(names)

        guard = _guard(resolver=resolver, clock=clock)
        self.assertEqual(_drive(guard, _scope("warehouse.corp.local")), 421)
        self.assertEqual(len(calls), 1)
        # Operator adds the name; cooldown must elapse before the next attempt.
        names.add("warehouse.corp.local")
        self.assertEqual(_drive(guard, _scope("warehouse.corp.local")), 421)
        self.assertEqual(len(calls), 1, "cooldown must suppress the second resolve")
        clock.advance(server.REFRESH_COOLDOWN_SECONDS + 1)
        self.assertEqual(_drive(guard, _scope("warehouse.corp.local")), 200)
        self.assertEqual(len(calls), 2)

    def test_accepted_requests_never_resolve(self) -> None:
        calls = []

        def resolver():
            calls.append(1)
            return _policy()

        guard = _guard(resolver=resolver)
        for _ in range(20):
            self.assertEqual(_drive(guard, _scope(HOSTNAME)), 200)
        self.assertEqual(calls, [])

    def test_many_rejects_in_one_window_resolve_once(self) -> None:
        calls = []

        def resolver():
            calls.append(1)
            return _policy()

        guard = _guard(resolver=resolver, clock=_Clock())
        for _ in range(50):
            self.assertEqual(_drive(guard, _scope("evil.com")), 421)
        self.assertEqual(len(calls), 1)

    def test_concurrent_misses_are_single_flight(self) -> None:
        calls = []

        def resolver():
            calls.append(1)
            return _policy()

        guard = _guard(resolver=resolver, clock=_Clock())

        async def main():
            async def one():
                return await guard.validate(_scope("evil.com"))

            return await asyncio.gather(*[one() for _ in range(10)])

        results = asyncio.run(main())
        self.assertTrue(all(r is not None for r in results))
        self.assertEqual(len(calls), 1)

    def test_edited_extras_file_applies_at_once_in_both_directions(self) -> None:
        """Adding a name works immediately; REMOVING one actually revokes it."""
        import tempfile

        original_path = server.ALLOWED_HOSTS_FILE
        temp_dir = tempfile.mkdtemp()
        path = f"{temp_dir}/allowed_hosts.txt"
        server.ALLOWED_HOSTS_FILE = path
        try:
            hostname_only = frozenset({HOSTNAME, "localhost"})

            def resolver():
                # Mirrors resolve_policy: built-ins plus whatever the file holds.
                names = set(hostname_only)
                for entry in server.read_extra_entries():
                    name, _origin = server._split_extra_entry(entry)
                    if name:
                        names.add(name)
                return _policy(names)

            guard = _guard(hostname_only, resolver=resolver, clock=_Clock())
            self.assertEqual(_drive(guard, _scope("tunnel.example.com")), 421)

            with open(path, "w", encoding="utf-8") as handle:
                handle.write("tunnel.example.com\n")
            # No clock advance: an operator edit must not wait out the cooldown.
            self.assertEqual(_drive(guard, _scope("tunnel.example.com")), 200)

            os.remove(path)
            self.assertEqual(_drive(guard, _scope("tunnel.example.com")), 421,
                             "deleting the extras file must revoke the name")
            self.assertEqual(_drive(guard, _scope(HOSTNAME)), 200)
        finally:
            server.ALLOWED_HOSTS_FILE = original_path
            if os.path.exists(path):
                os.remove(path)
            os.rmdir(temp_dir)

    def test_unchanged_extras_file_does_not_resolve(self) -> None:
        calls = []

        def resolver():
            calls.append(1)
            return _policy()

        guard = _guard(resolver=resolver)
        for _ in range(10):
            self.assertEqual(_drive(guard, _scope(HOSTNAME)), 200)
        self.assertEqual(calls, [], "a stable extras file must not trigger work")

    def test_refresh_failure_keeps_previous_policy(self) -> None:
        def resolver():
            raise OSError("resolver down")

        guard = _guard(resolver=resolver, clock=_Clock())
        with self.assertLogs("warehouse.mcp.host", level="WARNING"):
            self.assertEqual(_drive(guard, _scope("evil.com")), 421)
        self.assertEqual(guard.policy.names, DEFAULT_NAMES)
        self.assertEqual(_drive(guard, _scope(HOSTNAME)), 200)

    def test_request_path_does_no_dns_or_socket_work(self) -> None:
        # Build the loop BEFORE patching: the proactor loop itself opens a
        # socketpair, so patching socket.socket first would fail loop setup and
        # prove nothing about the request path.
        loop = asyncio.new_event_loop()
        saved = (socket.getaddrinfo, socket.gethostbyname,
                 socket.gethostbyname_ex, socket.getfqdn, socket.gethostname,
                 socket.create_connection)

        def boom(*args, **kwargs):
            raise AssertionError("request path must not touch the network")

        try:
            (socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex,
             socket.getfqdn, socket.gethostname,
             socket.create_connection) = (boom,) * 6
            guard = _guard()
            self.assertEqual(_drive(guard, _scope(HOSTNAME), loop), 200)
            self.assertEqual(_drive(guard, _scope("evil.com"), loop), 421)
        finally:
            (socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex,
             socket.getfqdn, socket.gethostname,
             socket.create_connection) = saved
            loop.close()


class LoggingTests(unittest.TestCase):
    def test_repeated_rejections_log_once_per_window(self) -> None:
        clock = _Clock()
        guard = _guard(clock=clock)
        with self.assertLogs("warehouse.mcp.host", level="WARNING") as captured:
            for _ in range(100):
                _drive(guard, _scope("evil.com"))
            _drive(guard, _scope("other.evil.com"))
        rejects = [r for r in captured.records if "REJECT" in r.getMessage()]
        self.assertEqual(len(rejects), 2)

    def test_reject_table_is_bounded(self) -> None:
        guard = _guard(clock=_Clock())
        with self.assertLogs("warehouse.mcp.host", level="WARNING"):
            for i in range(300):
                _drive(guard, _scope(f"evil{i}.com"))
        self.assertLessEqual(len(guard._seen_rejects), server.REJECT_LOG_MAX_DISTINCT)

    def test_accepted_host_logs_once_per_distinct_value(self) -> None:
        guard = _guard(clock=_Clock())
        with self.assertLogs("warehouse.mcp.host", level="INFO") as captured:
            for _ in range(50):
                _drive(guard, _scope(HOSTNAME))
            _drive(guard, _scope(FQDN))
        accepts = [r for r in captured.records if r.getMessage().startswith("accept ")]
        self.assertEqual(len(accepts), 2)

    def test_sanitize_strips_control_characters_and_truncates(self) -> None:
        # Asserted directly on _sanitize: driving a BYTES Host through the
        # middleware cannot prove this, because str(bytes) escapes CR/LF into
        # backslash sequences on its own — that made an earlier version of this
        # test pass even with sanitization removed entirely.
        self.assertEqual(server._sanitize("a\r\nb\x01c\x7f"), "a??b?c?")
        self.assertEqual(server._sanitize("héllo"), "h?llo")
        long_value = server._sanitize("x" * 500)
        self.assertTrue(long_value.endswith("..."))
        self.assertLessEqual(len(long_value), 131)

    def test_logged_values_carry_no_line_breaks(self) -> None:
        guard = _guard(clock=_Clock())
        with self.assertLogs("warehouse.mcp.host", level="WARNING") as captured:
            _drive(guard, _scope(b"evil\r\ninjected: yes"))
        for record in captured.records:
            message = record.getMessage()
            self.assertNotIn("\r", message)
            self.assertNotIn("\n", message)

    def test_host_logger_is_isolated_from_the_rich_root_handler(self) -> None:
        import io

        server.configure_host_logging(io.StringIO())   # keep the suite quiet
        self.assertFalse(server.host_log.propagate)
        self.assertTrue(server.host_log.handlers)
        self.assertIn("asctime", server.host_log.handlers[0].formatter._fmt)

    def test_allow_any_host_accepts_anything_and_re_warns(self) -> None:
        clock = _Clock()
        guard = _guard(accept_any=True, clock=clock)
        with self.assertLogs("warehouse.mcp.host", level="WARNING") as captured:
            self.assertEqual(_drive(guard, _scope("literally.anything")), 200)
            self.assertEqual(_drive(guard, _scope("literally.anything")), 200)
            clock.advance(server.ANY_HOST_REWARN_SECONDS + 1)
            self.assertEqual(_drive(guard, _scope("literally.anything")), 200)
        warnings = [r for r in captured.records if "DISABLED" in r.getMessage()]
        self.assertEqual(len(warnings), 2)


class SdkIntegrationTests(unittest.TestCase):
    def test_sdk_protection_is_replaced_but_content_type_still_enforced(self) -> None:
        from mcp.server.transport_security import TransportSecurityMiddleware

        settings = server.replaced_transport_security()
        self.assertFalse(settings.enable_dns_rebinding_protection)
        middleware = TransportSecurityMiddleware(settings)
        self.assertTrue(middleware._validate_content_type("application/json"))
        self.assertFalse(middleware._validate_content_type("text/plain"))

    def test_transport_security_is_deep_copied_by_the_sdk(self) -> None:
        """Documents WHY the policy is not kept inside the SDK's settings.

        Mutating the object handed to build_server is a silent no-op, so a
        refreshable allowlist cannot live there.
        """
        from mcp.server.transport_security import TransportSecuritySettings

        passed = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=["a"])
        srv = server.build_server(transport_security=passed)
        live = srv.settings.transport_security
        self.assertIsNot(live, passed)
        passed.allowed_hosts.append("mutated")
        self.assertNotIn("mutated", live.allowed_hosts)


class ServerSecurityTests(unittest.TestCase):
    """The remote SQL column authorizer.

    server._REMOTE_DENIED_COLUMNS ships EMPTY, because which columns are
    sensitive is a property of your schema and there is nothing sensible to
    default it to. These tests install a representative denylist and prove the
    MECHANISM instead: that denial survives aliasing and CTEs, that it does not
    break aggregation over the same table, and that it applies to remote HTTP
    callers only.
    """

    DENY = frozenset({
        ("customers", "email"),
        ("customers", "phone"),
        ("shipments", "tracking_code"),
        ("shipments", "ship_city"),
    })

    def setUp(self) -> None:
        saved = server._REMOTE_DENIED_COLUMNS
        server._REMOTE_DENIED_COLUMNS = set(self.DENY)
        self.addCleanup(setattr, server, "_REMOTE_DENIED_COLUMNS", saved)

    @staticmethod
    def _as_remote(conn: sqlite3.Connection) -> None:
        """Install the authorizer the way an inbound HTTP request would."""
        marker = server._REMOTE_HTTP_REQUEST.set(True)
        try:
            server._protect_remote_connection(conn)
        finally:
            server._REMOTE_HTTP_REQUEST.reset(marker)

    @staticmethod
    def _fixture() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE customers "
            "(customer_id TEXT, email TEXT, phone TEXT, lifetime_value REAL)"
        )
        conn.execute(
            "INSERT INTO customers VALUES "
            "('c1', 'person@example.com', '+15555550100', 25.0)"
        )
        conn.execute(
            "CREATE TABLE shipments "
            "(id TEXT, tracking_code TEXT, ship_city TEXT, n_parcels INTEGER)"
        )
        conn.execute("INSERT INTO shipments VALUES ('s1', '1Z999', 'Osaka', 1)")
        return conn

    def test_remote_authorizer_denies_pii_but_allows_aggregates(self) -> None:
        conn = self._fixture()
        self._as_remote(conn)

        # The whole point: a denied column does not quarantine its table. Team
        # members can still answer aggregate questions over it.
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*), SUM(lifetime_value) FROM customers"
            ).fetchone(),
            (1, 25.0),
        )
        for sql in (
            "SELECT email FROM customers",
            "SELECT phone FROM customers",
            "SELECT tracking_code FROM shipments",
            "SELECT ship_city FROM shipments",
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(sql).fetchall()
        conn.close()

    def test_denial_survives_aliasing_and_cte_indirection(self) -> None:
        """Why this is an authorizer and not a regex over the query text."""
        conn = self._fixture()
        self._as_remote(conn)

        for sql in (
            'SELECT "email" FROM customers',
            "SELECT [email] FROM customers",
            "SELECT c.email AS anything FROM customers c",
            "WITH x AS (SELECT email FROM customers) SELECT * FROM x",
            "SELECT (SELECT email FROM customers LIMIT 1)",
        ):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                conn.execute(sql).fetchall()
        conn.close()

    def test_pseudonymous_id_is_readable(self) -> None:
        """A bare platform id resolves to nobody without access to that platform.

        Denying it would block every legitimate customer-grain question — LTV,
        repeat rate, cohorts, retention — which is usually the entire reason the
        id was ingested. So ids stay readable and only the columns that RESOLVE
        an id to a person are denied.
        """
        conn = self._fixture()
        self._as_remote(conn)

        self.assertEqual(
            conn.execute("SELECT customer_id FROM customers").fetchall(),
            [("c1",)],
        )
        conn.close()

    def test_local_authorizer_is_not_installed(self) -> None:
        """Local stdio is unrestricted — the denylist guards the network edge."""
        conn = self._fixture()

        server._protect_remote_connection(conn)  # no remote marker set

        self.assertEqual(
            conn.execute("SELECT email FROM customers").fetchone()[0],
            "person@example.com",
        )
        conn.close()

    def test_bearer_middleware_rejects_missing_token_and_marks_valid_request(self) -> None:
        seen = []

        async def app(scope, receive, send):
            del scope, receive
            seen.append(server._REMOTE_HTTP_REQUEST.get())
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        async def request(headers):
            sent = []

            async def receive():
                return {"type": "http.request", "body": b""}

            async def send(message):
                sent.append(message)

            middleware = server.BearerTokenMiddleware(
                app, "secret", _guard(),
            )
            await middleware(
                {"type": "http", "headers": headers, "server": ["10.0.0.42", 8787]},
                receive,
                send,
            )
            return sent

        denied = asyncio.run(request([]))
        allowed = asyncio.run(
            request([(b"authorization", b"Bearer secret"),
                     (b"host", b"warehouse-host:8787")])
        )
        # A valid token with NO Host header is now rejected, not passed through.
        no_host = asyncio.run(request([(b"authorization", b"Bearer secret")]))

        self.assertEqual(denied[0]["status"], 401)
        self.assertEqual(allowed[0]["status"], 200)
        self.assertEqual(no_host[0]["status"], 421)
        self.assertEqual(seen, [True])

    def test_stale_legacy_token_path_is_redacted_before_the_401(self) -> None:
        """A rotated-out token must never reach an access log verbatim.

        The stored `legacy_path` reflects the CURRENT token, so a request built
        against the OLD one fails `legacy_ok` — but the secret-shaped segment is
        still sitting in `scope['path']`, and that scope is what a real ASGI
        server's access-log middleware reads when the response completes. The
        redaction has to fire on this failure path, not just the success path
        the existing rewrite already covered.
        """
        async def app(scope, receive, send):
            del scope, receive, send
            raise AssertionError("a 401 must never reach the inner app")

        async def request():
            sent = []

            async def receive():
                return {"type": "http.request", "body": b""}

            async def send(message):
                sent.append(message)

            middleware = server.BearerTokenMiddleware(
                app, "current-secret", _guard(), allow_legacy_path=True,
            )
            scope = {
                "type": "http",
                "path": "/rotated-out-old-secret/mcp",
                "raw_path": b"/rotated-out-old-secret/mcp",
                "headers": [(b"host", b"warehouse-host:8787")],
                "server": ["10.0.0.42", 8787],
            }
            await middleware(scope, receive, send)
            return sent, scope

        sent, scope = asyncio.run(request())

        self.assertEqual(sent[0]["status"], 401)
        self.assertNotIn("rotated-out-old-secret", scope["path"])
        self.assertNotIn(b"rotated-out-old-secret", scope["raw_path"])
        self.assertEqual(scope["path"], "/<redacted-token>/mcp")

    def test_legacy_path_bridge_authenticates_and_rewrites_path(self) -> None:
        seen = []

        async def app(scope, receive, send):
            del receive
            seen.append((scope["path"], server._REMOTE_HTTP_REQUEST.get()))
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        async def request():
            sent = []
            scope = {
                "type": "http",
                "path": "/secret/mcp",
                "raw_path": b"/secret/mcp",
                "headers": [(b"host", b"warehouse-host:8787")],
                "server": ["10.0.0.42", 8787],
            }

            async def receive():
                return {"type": "http.request", "body": b""}

            async def send(message):
                sent.append(message)

            middleware = server.BearerTokenMiddleware(
                app,
                "secret",
                _guard(),
                allow_legacy_path=True,
            )
            await middleware(
                scope,
                receive,
                send,
            )
            return sent, scope

        response, scope = asyncio.run(request())

        self.assertEqual(response[0]["status"], 200)
        self.assertEqual(seen, [("/mcp", True)])
        self.assertEqual(scope["path"], "/mcp")
        self.assertEqual(scope["raw_path"], b"/mcp")


class RunSqlTimeoutTests(unittest.TestCase):
    """run_sql()'s wall-clock budget, enforced via sqlite3's progress handler."""

    @staticmethod
    def _slow_connection() -> sqlite3.Connection:
        """A connection whose queries can generate plenty of VM instructions
        without needing a real database file or actually running long — a
        recursive CTE counting to a few million is enough to guarantee at
        least one progress-handler poll fires. row_factory matches
        db.connect_readonly() so _rows_to_json's dict(row) conversion works."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn

    def test_query_past_the_deadline_is_cancelled_with_a_clear_message(self) -> None:
        conn = self._slow_connection()
        with (
            mock.patch.object(server.db, "connect_readonly", return_value=conn),
            mock.patch.object(server, "RUN_SQL_TIMEOUT_SEC", -1.0),
        ):
            result = server.run_sql(
                "WITH RECURSIVE cnt(x) AS "
                "(SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 5000000) "
                "SELECT COUNT(*) FROM cnt"
            )
        self.assertIn("time budget", result)
        self.assertIn("-1s", result)

    def test_ordinary_query_is_unaffected_by_a_generous_budget(self) -> None:
        conn = self._slow_connection()
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1), (2), (3)")
        with mock.patch.object(server.db, "connect_readonly", return_value=conn):
            result = server.run_sql("SELECT SUM(x) FROM t")
        self.assertIn("6", result)


if __name__ == "__main__":
    unittest.main()
