"""
MCP server for the e-commerce warehouse.

This is what Claude Desktop talks to. It exposes a handful of safe,
read-only tools over your local SQLite warehouse so you can ask things like
"what did we spend on each platform last week?" in plain English.

Two ways to run it:
  python server.py           # stdio — Claude Desktop launches this locally
  python server.py --http    # HTTP on 0.0.0.0:8787 for coworkers on the
                             # network (see SHARING.md). Requires
                             # WAREHOUSE_MCP_TOKEN in .env and sends it in
                             # the Authorization Bearer header.

SDK PIN: this module uses the v1 SDK API (mcp.server.fastmcp.FastMCP), so
requirements.txt pins `mcp[cli]>=1.29,<2`. SDK 2.0.0 (2026-07-28) renames
FastMCP to MCPServer, so an unpinned install breaks the import above; v1.x is
maintenance-only, so plan that migration rather than floating the pin.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import dataclasses
import hmac
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Sequence

import anyio
import anyio.to_thread
from dotenv import dotenv_values, load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from warehouse import db

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
# Operator-editable extra names/origins, re-read on every policy refresh, so a
# new name (a Cloudflare tunnel hostname, a hosts-file alias) needs no restart.
ALLOWED_HOSTS_FILE = os.path.join(HERE, "allowed_hosts.txt")
ALLOWED_HOSTS_ENV = "WAREHOUSE_MCP_ALLOWED_HOSTS"

MAX_HOST_LEN = 263                    # 253-byte DNS name + ':' + 5-digit port
REFRESH_COOLDOWN_SECONDS = 10.0       # min gap between policy re-resolutions
REFRESH_TIMEOUT_SECONDS = 5.0         # getfqdn can stall during VPN transitions
REJECT_LOG_COOLDOWN_SECONDS = 300.0
REJECT_LOG_MAX_DISTINCT = 64
ACCEPT_LOG_MAX_DISTINCT = 32
ANY_HOST_REWARN_SECONDS = 21600.0     # 6h: a debug bypass must not go quiet

# Own logger, NOT the root one: FastMCP's configure_logging() installs a rich
# handler that hard-wraps at ~80 columns (visible in mcp_server_log.txt), which
# would shred these diagnostic lines mid-value and make them ungreppable.
host_log = logging.getLogger("warehouse.mcp.host")

_REMOTE_HTTP_REQUEST = contextvars.ContextVar(
    "warehouse_remote_http_request",
    default=False,
)

# Columns that remote SQL may never read. Remote callers can still AGGREGATE
# the tables containing them; SQLite's authorizer rejects reads of the listed
# columns specifically, and it does so even through CTEs, aliases, subqueries or
# quoting — which is why this is an authorizer rule and not a regex over the
# query text. Local stdio access is unchanged.
#
# POPULATE THIS FOR YOUR OWN SCHEMA. The principle that has held up in practice:
# deny anything that RESOLVES AN ID TO A PERSON OR PLACE — email, phone, name,
# free-text customer comments, shipment tracking numbers, street/city/postcode —
# and do NOT deny pseudonymous ids. A bare platform customer id identifies
# nobody without access to that platform, while denying it blocks every
# legitimate customer-grain question (LTV, repeat rate, cohorts, retention),
# which is usually the entire reason the id was ingested in the first place.
#
# The cheaper half of the strategy is upstream: if the connectors never ingest
# name/email/phone at all, this list barely has to grow. A denylist is a second
# line of defence, not the first.
_REMOTE_DENIED_COLUMNS: set[tuple[str, str]] = {
    # ("your_table", "email"),
    # ("your_table", "phone"),
    # ("your_shipments", "tracking_code"),
}


@dataclasses.dataclass(frozen=True)
class HostPolicy:
    """The NAME half of the accept rule. Immutable; refreshed by whole rebind.

    IP-literal Hosts are deliberately absent: they are checked against the
    address the connection actually arrived on, so there is nothing to keep
    fresh (see host_verdict).
    """

    names: frozenset[str] = frozenset()
    origins: frozenset[str] = frozenset()


_FORBIDDEN_HOST_CHARS = frozenset('\t\n\r /\\@?#\x7f' + '"' + "'" + " ")
# Everything a DNS name or IP literal can legitimately contain, and nothing else.
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._:")


def _sanitize(value: object, limit: int = 128) -> str:
    """Make an attacker-controlled value safe to append to the log file."""
    text = str(value)
    clean = "".join(c if 0x20 <= ord(c) < 0x7F else "?" for c in text)
    return clean[:limit] + ("..." if len(clean) > limit else "")


def split_host_header(raw: bytes | str) -> tuple[str, int | None] | None:
    """Normalize a Host/authority value to (name, port), or None if malformed.

    Lowercases, strips exactly one trailing dot, unwraps [IPv6], and collapses
    IPv4-mapped IPv6 to plain IPv4 so equality against a real address works.
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("latin-1")
        except Exception:  # noqa: BLE001  # pragma: no cover - latin-1 cannot fail
            return None
    else:
        text = raw
    # Strip only HTTP's optional whitespace (space/htab). A bare .strip() also
    # removes unicode whitespace such as NBSP, which is not something a real
    # client sends and not something worth silently forgiving.
    text = text.strip(" \t")
    if not text or len(text) > MAX_HOST_LEN:
        return None
    if any(c in _FORBIDDEN_HOST_CHARS or ord(c) < 0x20 for c in text):
        return None

    port_text = ""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        name, rest = text[1:end], text[end + 1:]
        if rest:
            if not rest.startswith(":") or rest == ":":
                return None            # a bare trailing ':' is malformed
            port_text = rest[1:]
    elif text.count(":") > 1:
        # Unbracketed multi-colon is only legitimate as a bare IPv6 literal
        # (some tooling emits it); anything else is malformed.
        try:
            ipaddress.ip_address(text)
        except ValueError:
            return None
        name = text
    elif ":" in text:
        name, port_text = text.split(":", 1)
        if not port_text:
            return None                # 'host:' with no port is malformed
    else:
        name = text

    port: int | None = None
    if port_text:
        if not port_text.isdigit() or len(port_text) > 5:
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None

    name = name.lower()
    if name.endswith("."):
        name = name[:-1]
    if not name or not set(name) <= _NAME_CHARS:
        # Positive charset, not a blacklist: this is what makes '*' a malformed
        # name rather than a value someone could mistake for a wildcard.
        return None
    try:
        parsed = ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        if getattr(parsed, "ipv4_mapped", None):
            name = str(parsed.ipv4_mapped)
    # PORT IS PARSED AND IGNORED for matching. The connection already arrived on
    # our listening socket, so a mismatched Host port detects no attack — and
    # requiring it would break port-forwarded clients and the tunnel case
    # (public hostname with no port in front of local 8787). Do not "fix" this.
    return name, port


def _local_ip(local_addr: Sequence[Any] | None) -> ipaddress._BaseAddress | None:
    """The address THIS connection arrived on, from the ASGI scope."""
    if not isinstance(local_addr, (list, tuple)) or len(local_addr) < 1:
        return None
    try:
        return ipaddress.ip_address(str(local_addr[0]))
    except ValueError:
        return None


def host_verdict(
    name: str,
    local_addr: Sequence[Any] | None,
    policy: HostPolicy,
) -> tuple[bool, str]:
    """Accept/reject a normalized Host name. First match wins."""
    if name == "localhost":
        return True, "loopback-name"
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        if name in policy.names:
            return True, "configured-name"
        return False, "unknown-host"
    # Host is a literal IP.
    if addr.is_loopback:
        return True, "loopback-ip"
    local = _local_ip(local_addr)
    if local is not None and local == addr:
        # THE FIX: "is this the address you reached me on?" is answerable per
        # request and is never stale, so moving networks needs no restart and
        # no IP enumeration. A retired address (an old cert SAN) still fails.
        return True, "connected-ip"
    if name in policy.names:
        return True, "configured-ip"
    if local is None:
        return False, "no-local-addr"        # fail closed
    return False, "ip-not-this-connection"


def origin_verdict(
    raw: bytes | str | None,
    local_addr: Sequence[Any] | None,
    policy: HostPolicy,
) -> tuple[bool, str]:
    """Accept/reject an Origin header. Absent is allowed (mcp-remote sends none)."""
    if raw is None:
        return True, "no-origin"
    text = raw.decode("latin-1") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        return True, "no-origin"
    lowered = text.lower()
    if lowered in policy.origins:
        return True, "configured-origin"
    scheme, sep, rest = lowered.partition("://")
    if not sep or scheme not in ("http", "https"):
        return False, "origin-scheme"        # also rejects the literal "null"
    authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in authority:
        return False, "origin-userinfo"
    parts = split_host_header(authority)
    if parts is None:
        return False, "origin-malformed"
    ok, _ = host_verdict(parts[0], local_addr, policy)
    return (True, "origin-host") if ok else (False, "origin-host")


def _split_extra_entry(entry: str) -> tuple[str | None, str | None]:
    """Classify one operator-supplied entry as (name, origin)."""
    entry = entry.strip()
    if not entry or entry.startswith("#"):
        return None, None
    if "://" in entry:
        lowered = entry.lower()
        scheme = lowered.split("://", 1)[0]
        if scheme in ("http", "https"):
            return None, lowered
        host_log.warning("dropping allowed-host entry %s: unsupported scheme",
                         _sanitize(entry))
        return None, None
    parts = split_host_header(entry)
    if parts is None:
        # '*' has no meaning here: there is deliberately no wildcard.
        host_log.warning("dropping allowed-host entry %s: not a valid host name",
                         _sanitize(entry))
        return None, None
    return parts[0], None


def read_extra_entries(cli_extras: Iterable[str] = ()) -> list[str]:
    """CLI + env + file extras. Re-read on every refresh, so no restart."""
    entries: list[str] = [str(x) for x in cli_extras]
    env_value = os.environ.get(ALLOWED_HOSTS_ENV)
    if env_value is None:
        try:
            env_value = dotenv_values(os.path.join(HERE, ".env")).get(ALLOWED_HOSTS_ENV)
        except Exception:  # noqa: BLE001
            env_value = None
    if env_value:
        entries.extend(part for part in env_value.replace(";", ",").split(",") if part)
    try:
        with open(ALLOWED_HOSTS_FILE, encoding="utf-8") as handle:
            entries.extend(handle.read().splitlines())
    except FileNotFoundError:
        pass
    except OSError as exc:
        host_log.warning("could not read %s: %s", ALLOWED_HOSTS_FILE, exc)
    return entries


def extras_signature() -> tuple[int, int] | None:
    """(mtime, size) of the extras file, or None if absent.

    A stat — NOT name resolution — so it is safe on the happy path. This is what
    makes REVOCATION timely: a rejection-triggered refresh alone would keep
    honouring a name after the operator deleted it, since an accepted request
    never misses and so would never re-resolve.
    """
    try:
        info = os.stat(ALLOWED_HOSTS_FILE)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def resolve_policy(cli_extras: Iterable[str] = ()) -> HostPolicy:
    """Build the name policy. BLOCKING (getfqdn) — never call on the event loop."""
    hostname = socket.gethostname().lower()
    names = {hostname, f"{hostname}.local", "localhost"}
    try:
        fqdn = socket.getfqdn().lower().rstrip(".")
    except OSError as exc:  # pragma: no cover - defensive
        host_log.warning("getfqdn failed: %s", exc)
        fqdn = ""
    if fqdn and fqdn != hostname:
        # Accept the resolver's answer ONLY if it is our own name with a suffix.
        # A hostile/wildcard resolver answering 'evil.attacker.com' is dropped.
        if fqdn.split(".")[0] == hostname:
            names.add(fqdn)
        else:
            host_log.warning("ignoring getfqdn answer %s: first label is not %s",
                             _sanitize(fqdn), hostname)
    origins: set[str] = set()
    for entry in read_extra_entries(cli_extras):
        name, origin = _split_extra_entry(entry)
        if name:
            names.add(name)
        if origin:
            origins.add(origin)
    return HostPolicy(frozenset(names), frozenset(origins))


class HostGuard:
    """Validates Host/Origin per request, self-healing without a restart.

    NO PREFIX OR SUFFIX MATCHING. Names are exact set membership: accepting
    '<hostname>.<anything>' would wave through <hostname>.evil.com, which an
    attacker can register and rebind — defeating the very control this is.

    The happy path performs NO name resolution and NO socket work, so it can
    never stall the event loop. The only refresh trigger is a rejection, which
    re-resolves off-loop (cooldown-limited, single-flight) and re-checks once,
    so an operator adding a name to allowed_hosts.txt is live within ~10s.
    """

    def __init__(
        self,
        policy: HostPolicy,
        *,
        accept_any: bool = False,
        cli_extras: Iterable[str] = (),
        resolver: Callable[[], HostPolicy] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.policy = policy
        self.accept_any = accept_any
        self._cli_extras = tuple(cli_extras)
        self._resolver = resolver or (lambda: resolve_policy(self._cli_extras))
        self._clock = clock
        self._last_refresh = -1e9
        self._lock: asyncio.Lock | None = None
        self._lock_loop: Any = None
        self._extras_sig = extras_signature()
        self._seen_accepts: dict[str, float] = {}
        self._seen_rejects: dict[str, float] = {}
        self._last_any_warn = -1e9
        self._reject_overflow = 0

    # ---------------------------------------------------------------- checks
    def evaluate(self, scope: dict) -> tuple[int, str, str] | None:
        """Pure check against the CURRENT policy. None means allow."""
        raw_hosts = [v for k, v in scope.get("headers", ()) if k.lower() == b"host"]
        if not raw_hosts:
            return 421, "missing-host", ""
        if len(raw_hosts) > 1:
            # Never trust a dict-comprehension's last-wins here.
            return 421, "duplicate-host", ""
        parts = split_host_header(raw_hosts[0])
        if parts is None:
            return 421, "malformed-host", _sanitize(raw_hosts[0])
        name, _port = parts
        local_addr = scope.get("server")
        ok, reason = host_verdict(name, local_addr, self.policy)
        if not ok:
            return 421, reason, name
        origins = [v for k, v in scope.get("headers", ()) if k.lower() == b"origin"]
        if len(origins) > 1:
            return 403, "duplicate-origin", ""
        ok, reason = origin_verdict(origins[0] if origins else None,
                                    local_addr, self.policy)
        if not ok:
            return 403, reason, _sanitize(origins[0])
        return None

    async def validate(self, scope: dict) -> tuple[int, str, str] | None:
        """Check, refreshing the name policy once if the first look rejects."""
        if self.accept_any:
            self._warn_any_host()
            return None
        # An edited extras file is an explicit operator action, so it bypasses
        # the cooldown and applies to THIS request — additions take effect at
        # once, and removals actually take effect at all.
        signature = extras_signature()
        if signature != self._extras_sig:
            self._extras_sig = signature      # stamp first: one attempt per edit
            await self._refresh(force=True)
        verdict = self.evaluate(scope)
        if verdict is None:
            self._log_accept(scope)
            return None
        if verdict[1] in ("missing-host", "duplicate-host", "malformed-host",
                          "duplicate-origin"):
            self._log_reject(*verdict, scope=scope)   # never a policy-staleness issue
            return verdict
        if await self._refresh():
            verdict = self.evaluate(scope)
            if verdict is None:
                self._log_accept(scope)
                return None
        self._log_reject(*verdict, scope=scope)
        return verdict

    async def _refresh(self, force: bool = False) -> bool:
        """Re-resolve off the event loop. Returns True if the policy is fresh."""
        now = self._clock()
        if not force and now - self._last_refresh < REFRESH_COOLDOWN_SECONDS:
            return False
        # Bind the single-flight lock to the running loop: an asyncio.Lock reused
        # across loops raises, which would turn a refresh into a 500.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        async with self._lock:
            now = self._clock()
            if not force and now - self._last_refresh < REFRESH_COOLDOWN_SECONDS:
                return False
            self._last_refresh = now      # stamp BEFORE resolving: a slow
            previous = self.policy        # resolver must not be re-entered
            try:
                with anyio.fail_after(REFRESH_TIMEOUT_SECONDS):
                    policy = await anyio.to_thread.run_sync(
                        self._resolver, abandon_on_cancel=True)
            except Exception as exc:  # noqa: BLE001  (incl. anyio TimeoutError)
                host_log.warning("policy refresh failed (%s); keeping %d names",
                                 _sanitize(exc), len(previous.names))
                return False
            if policy != previous:
                added = sorted(policy.names - previous.names)
                removed = sorted(previous.names - policy.names)
                host_log.info("policy changed:%s%s (names=%d origins=%d)",
                              "".join(f" +{_sanitize(n, 64)}" for n in added),
                              "".join(f" -{_sanitize(n, 64)}" for n in removed),
                              len(policy.names), len(policy.origins))
                self.policy = policy
            return True

    # --------------------------------------------------------------- logging
    def _dedupe(self, table: dict[str, float], key: str, cooldown: float,
                limit: int) -> bool:
        now = self._clock()
        last = table.get(key)
        if last is not None and now - last < cooldown:
            return False
        if key not in table and len(table) >= limit:
            oldest = min(table, key=lambda k: table[k])
            del table[oldest]
            self._reject_overflow += 1
        table[key] = now
        return True

    def _log_accept(self, scope: dict) -> None:
        raw = [v for k, v in scope.get("headers", ()) if k.lower() == b"host"]
        if not raw:
            return
        parts = split_host_header(raw[0])
        if parts is None:
            return
        name = parts[0]
        if name in self._seen_accepts:
            return
        if len(self._seen_accepts) >= ACCEPT_LOG_MAX_DISTINCT:
            return
        self._seen_accepts[name] = self._clock()
        _ok, reason = host_verdict(name, scope.get("server"), self.policy)
        host_log.info("accept host=%s via=%s local=%s client=%s",
                      _sanitize(name), reason,
                      _sanitize((scope.get("server") or ["?"])[0]),
                      _sanitize((scope.get("client") or ["?"])[0]))

    def _log_reject(self, status: int, reason: str, value: str, *,
                    scope: dict) -> None:
        key = f"{status}:{reason}:{value}"
        before = self._reject_overflow
        if not self._dedupe(self._seen_rejects, key, REJECT_LOG_COOLDOWN_SECONDS,
                            REJECT_LOG_MAX_DISTINCT):
            return
        if self._reject_overflow > before:
            host_log.warning(
                "reject-log table full; %d distinct rejected values evicted "
                "(a scanner or a misconfigured client is retrying)",
                self._reject_overflow)
        local = scope.get("server") or ["?", "?"]
        host_log.warning(
            "REJECT %d host=%s reason=%s local=%s client=%s -> add the name to "
            "%s (live within %ds, no restart) or connect via https://%s:%s/mcp",
            status, _sanitize(value) or "(none)", reason,
            _sanitize(local[0]), _sanitize((scope.get("client") or ["?"])[0]),
            ALLOWED_HOSTS_FILE, int(REFRESH_COOLDOWN_SECONDS),
            self._suggested_name(), local[1] if len(local) > 1 else "?")

    def _suggested_name(self) -> str:
        """A working URL to suggest, from the policy — never a syscall.

        The reject path must stay free of name resolution; a unit test patches
        the socket module to prove it.
        """
        candidates = [n for n in self.policy.names
                      if n != "localhost" and not n.endswith(".local")]
        return min(candidates, key=len) if candidates else "localhost"

    def _warn_any_host(self) -> None:
        now = self._clock()
        if now - self._last_any_warn < ANY_HOST_REWARN_SECONDS:
            return
        self._last_any_warn = now
        host_log.warning(
            "Host/Origin validation is DISABLED (--allow-any-host); "
            "remove the flag from serve_mcp.bat when done debugging")

    def describe(self) -> str:
        return (f"names={sorted(self.policy.names)} "
                f"origins={sorted(self.policy.origins)} "
                f"(plus loopback and the IP each connection arrives on)")


def configure_host_logging(stream=None) -> None:
    """Give the host logger its own plain handler (see host_log comment)."""
    host_log.setLevel(logging.INFO)
    host_log.propagate = False
    if not host_log.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s host-policy %(message)s"))
        host_log.addHandler(handler)


class BearerTokenMiddleware:
    """Authenticate HTTP requests and optionally bridge the legacy token path.

    The compatibility path is migration-only: it lets existing coworkers keep
    working while Bearer configs roll out. Both modes mark the request remote,
    so the SQLite PII authorizer is active immediately.
    """

    def __init__(
        self,
        app: Any,
        token: str,
        guard: HostGuard,
        allow_legacy_path: bool = False,
    ):
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")
        self.legacy_path = f"/{token}/mcp" if allow_legacy_path else None
        # Required, not optional: there is no way to build the HTTP entry point
        # without deciding a Host policy. To skip the check you must construct
        # HostGuard(accept_any=True), which announces itself in the log.
        self.guard = guard

    async def __call__(self, scope, receive, send) -> None:
        scope_type = scope.get("type")
        if scope_type != "http":
            # Only the lifespan channel may pass unchecked. Anything else would
            # skip BOTH the token and the host check by construction, relying on
            # the inner app to refuse it — today Starlette does close websockets
            # (no ws routes exist), but that is its accident, not our guarantee.
            if scope_type == "lifespan":
                await self.app(scope, receive, send)
            elif scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = {
            key.lower(): value
            for key, value in scope.get("headers", ())
        }
        supplied = headers.get(b"authorization", b"")
        bearer_ok = hmac.compare_digest(supplied, self.expected)
        legacy_ok = (
            self.legacy_path is not None
            and hmac.compare_digest(
                str(scope.get("path", "")),
                self.legacy_path,
            )
        )
        # Scrub the token out of the path BEFORE any response is sent, so a
        # server's access-log middleware (Uvicorn's included) never persists it
        # verbatim. This matters most for the legacy `/<token>/mcp` path style:
        # once a token is rotated, every request against the OLD path 401s below
        # (the compare_digest checks above are already computed, so mutating the
        # scope here cannot affect them) and would otherwise write the retired
        # secret straight into a log file on every stale client's retry.
        #
        # Deliberately matched by SHAPE (first path segment of a two-segment
        # `/<x>/mcp` path), not by comparing against self.legacy_path — a
        # rotated-out token no longer equals the current one, but it still sits
        # in exactly this position and is exactly as sensitive.
        if self.legacy_path is not None:
            path_str = str(scope.get("path", ""))
            segments = path_str.split("/")
            if len(segments) == 3 and segments[0] == "" and segments[1] and segments[2] == "mcp":
                safe_path = "/<redacted-token>/mcp"
                scope["path"] = safe_path
                scope["raw_path"] = safe_path.encode("ascii", "replace")
        if not bearer_ok and not legacy_ok:
            body = b'{"error":"authentication required"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        # Host/Origin validation runs AFTER authentication on purpose: it is only
        # load-bearing when a token IS present (a leaked token driven from a
        # browser), an unauthenticated attacker is already 401'd, and this way
        # anonymous probes can neither enumerate valid host names by status code
        # nor fill the log — so every 421 here is a real client with a real
        # problem. Replaces the SDK's own check (see build_server).
        verdict = await self.guard.validate(scope)
        if verdict is not None:
            status, reason, value = verdict
            body = json.dumps({
                "error": "host not allowed" if status == 421 else "origin not allowed",
                "reason": reason,
                "value": value,
                "hint": f"add the name to {ALLOWED_HOSTS_FILE} (applies within "
                        f"{int(REFRESH_COOLDOWN_SECONDS)}s, no restart) or use the "
                        f"machine's own name/IP in the URL",
            }).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        if legacy_ok:
            # FastMCP is mounted at /mcp. Rewrite the deprecated capability
            # URL after authenticating it so unchanged clients still route.
            # Mutate the shared scope so Uvicorn's access logger also sees only
            # /mcp and does not persist the secret-bearing legacy path.
            scope["path"] = "/mcp"
            scope["raw_path"] = b"/mcp"

        marker = _REMOTE_HTTP_REQUEST.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            _REMOTE_HTTP_REQUEST.reset(marker)


def _protect_remote_connection(conn: sqlite3.Connection) -> None:
    """Deny sensitive column reads for remote run_sql calls."""
    if not _REMOTE_HTTP_REQUEST.get():
        return

    def authorizer(action, table, column, database, source):
        del database, source
        if (
            action == sqlite3.SQLITE_READ
            and (str(table).lower(), str(column).lower())
            in _REMOTE_DENIED_COLUMNS
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)


def _rows_to_json(rows) -> str:
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ---------------------------------------------------------------- tools -----
# Plain functions; register_tools() below binds them to a server instance with
# read-only annotations. Keeping them unbound is what lets build_server() pass
# host/port/security as CONSTRUCTOR arguments instead of reaching into private
# SDK state after the fact.

def list_tables(table_pattern: str | None = None,
                include_columns: bool = True) -> str:
    """
    List the warehouse's tables and their column names.

    On a mature warehouse the full dump gets large — at ~100 tables it costs
    several thousand tokens per call — so prefer narrowing it:
      table_pattern    substring match on the table name, case-insensitive, so
                       'shopify' finds every shopify_* table. A '%' anywhere
                       makes it a raw SQL LIKE pattern instead.
      include_columns  set false for a cheap name-only catalogue when you just
                       need to know what exists.
    """
    conn = db.connect_readonly()
    sql = "SELECT name FROM sqlite_master WHERE type='table'"
    params: tuple[Any, ...] = ()
    if table_pattern:
        # Only '%' signals a hand-written pattern. Underscores must NOT: LIKE
        # treats '_' as a single-char wildcard, and snake_case table names are
        # full of them — keying off '_' would leave a plain name like
        # 'order_items' unwrapped and matching nothing.
        pattern = table_pattern if "%" in table_pattern else f"%{table_pattern}%"
        sql += " AND lower(name) LIKE lower(?)"
        params = (pattern,)
    names = [row[0] for row in conn.execute(sql + " ORDER BY name", params)]
    if not names:
        return (f"No table name matches {table_pattern!r}. "
                "Call list_tables() with no arguments to see everything.")
    if not include_columns:
        conn.close()
        return json.dumps(names, indent=2)
    out = {
        name: [c[1] for c in conn.execute(f"PRAGMA table_info({name})").fetchall()]
        for name in names
    }
    conn.close()
    return json.dumps(out, indent=2)


# Wall-clock ceiling on a single ad-hoc run_sql() call. On a large warehouse an
# unindexed scan or an accidental cross join can run for minutes, and this
# server is often shared (--http mode) or running alongside a sync job, so one
# slow query souring the whole process for everyone else is a worse failure
# mode than cutting it off with a clear error. Tune to your database size and
# how many concurrent callers you expect.
RUN_SQL_TIMEOUT_SEC = 45.0


def run_sql(query: str) -> str:
    """
    Run a READ-ONLY SQL query against the warehouse and return rows as JSON.
    Only SELECT statements are allowed. Useful for ad-hoc analysis.
    Tables: ad_metrics, orders, sync_log (use list_tables for columns).
    """
    cleaned = query.strip().rstrip(";")
    if not cleaned.lower().startswith(("select", "with")):
        return "Error: only SELECT / WITH (read-only) queries are allowed."
    # The connection is opened mode=ro, so SQLite itself rejects any write —
    # the startswith check above is just a friendlier fast-fail.
    conn = db.connect_readonly()
    _protect_remote_connection(conn)
    # SQLite polls this callback every N virtual-machine instructions during
    # query execution; returning truthy aborts the query with an
    # OperationalError("interrupted"), which is how a wall-clock budget is
    # enforced without threads or a separate watchdog process.
    deadline = time.monotonic() + RUN_SQL_TIMEOUT_SEC
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 100_000)
    try:
        rows = conn.execute(cleaned).fetchmany(1000)  # cap without loading everything
    except sqlite3.OperationalError as e:  # noqa: BLE001
        if "interrupted" in str(e).lower():
            return (f"Error: query exceeded the {RUN_SQL_TIMEOUT_SEC:.0f}s time "
                    f"budget and was cancelled. Narrow it with a WHERE on an "
                    f"indexed column, add a LIMIT, or pre-aggregate.")
        return f"SQL error: {e}"
    except Exception as e:  # noqa: BLE001
        return f"SQL error: {e}"
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
    out = _rows_to_json(rows)
    if len(rows) == 1000:
        out += "\n(Result truncated at 1000 rows — add a LIMIT or aggregate.)"
    return out


def spend_summary(start_date: str, end_date: str) -> str:
    """
    Total spend, revenue, clicks, and conversions per platform between two
    dates (inclusive). Dates are 'YYYY-MM-DD'. This is the cross-platform
    rollup most people want.
    """
    conn = db.connect_readonly()
    rows = conn.execute(
        """
        SELECT platform,
               ROUND(SUM(spend), 2)       AS spend,
               ROUND(SUM(revenue), 2)     AS revenue,
               SUM(clicks)                AS clicks,
               SUM(impressions)           AS impressions,
               ROUND(SUM(conversions), 2) AS conversions,
               ROUND(SUM(revenue) / NULLIF(SUM(spend), 0), 2) AS roas
        FROM ad_metrics
        WHERE date BETWEEN ? AND ?
        GROUP BY platform
        ORDER BY spend DESC
        """,
        (start_date, end_date),
    ).fetchall()
    conn.close()
    return _rows_to_json(rows)


def top_campaigns(start_date: str, end_date: str, limit: int = 15) -> str:
    """Top campaigns by spend across all platforms in a date range."""
    conn = db.connect_readonly()
    rows = conn.execute(
        """
        SELECT platform, campaign_name,
               ROUND(SUM(spend), 2)   AS spend,
               ROUND(SUM(revenue), 2) AS revenue,
               SUM(clicks)            AS clicks
        FROM ad_metrics
        WHERE date BETWEEN ? AND ?
        GROUP BY platform, campaign_id
        ORDER BY spend DESC
        LIMIT ?
        """,
        (start_date, end_date, int(limit)),
    ).fetchall()
    conn.close()
    return _rows_to_json(rows)


def sales_summary(start_date: str, end_date: str) -> str:
    """Order count and total sales per platform from the orders table."""
    conn = db.connect_readonly()
    rows = conn.execute(
        """
        SELECT platform,
               COUNT(DISTINCT order_id) AS orders,
               SUM(quantity)            AS units,
               ROUND(SUM(total), 2)     AS sales
        FROM orders
        WHERE order_date BETWEEN ? AND ?
        GROUP BY platform
        ORDER BY sales DESC
        """,
        (start_date, end_date),
    ).fetchall()
    conn.close()
    return _rows_to_json(rows)


def last_sync_status() -> str:
    """Show the most recent sync run for each platform (to check freshness)."""
    conn = db.connect_readonly()
    rows = conn.execute(
        """
        WITH ranked AS (
          SELECT platform, started_at, finished_at AS last_run, status,
                 rows_written, message,
                 ROW_NUMBER() OVER (
                   PARTITION BY platform ORDER BY started_at DESC, id DESC
                 ) AS rn
          FROM sync_log
        )
        SELECT platform, last_run, status, rows_written, message
        FROM ranked
        WHERE rn=1
        ORDER BY platform
        """
    ).fetchall()
    conn.close()
    return _rows_to_json(rows)


# Every tool here reads; none mutates the warehouse (connections are mode=ro).
# Declaring that lets clients skip write-style approval prompts.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_TOOLS = (
    (list_tables, "List warehouse tables"),
    (run_sql, "Run read-only SQL"),
    (spend_summary, "Ad spend by platform"),
    (top_campaigns, "Top campaigns by spend"),
    (sales_summary, "Sales by platform"),
    (last_sync_status, "Data freshness by platform"),
)


def register_tools(server: FastMCP) -> None:
    """Attach the read-only tool set to a server instance."""
    for func, title in _TOOLS:
        server.tool(title=title, annotations=_READ_ONLY)(func)


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    stateless_http: bool = False,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    """Construct the MCP server. All transport settings are constructor args."""
    server = FastMCP(
        "ecommerce-warehouse",
        host=host,
        port=port,
        # Keep secrets out of the URL and access logs; mcp-remote supplies the
        # Authorization header from the coworker's local environment.
        streamable_http_path="/mcp",
        stateless_http=stateless_http,
        transport_security=transport_security,
    )
    register_tools(server)
    return server


def replaced_transport_security() -> TransportSecuritySettings:
    """The SDK's Host/Origin check, explicitly REPLACED — not removed.

    HostGuard (above) does this job instead, because the SDK can only match a
    STATIC list, and a list of local IPs snapshotted at startup goes stale the
    moment this laptop changes network — the regression this design exists to
    avoid. Its list is also not refreshable in place: pydantic-settings
    deep-copies the nested model, so mutating the object passed to build_server
    is a SILENT no-op (verified; see the unit test that pins this).

    Note POST Content-Type validation is checked BEFORE this flag is consulted,
    so disabling it keeps that enforcement.
    """
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--http", action="store_true",
                   help="serve over HTTP for the team instead of stdio")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument(
        "--allow-legacy-token-path",
        action="store_true",
        help="temporarily accept /<token>/mcp while coworkers migrate",
    )
    p.add_argument(
        "--allow-host", action="append", default=[], metavar="HOST",
        help="extra Host value or http(s) Origin to accept (repeatable). The "
             f"same list can live in {os.path.basename(ALLOWED_HOSTS_FILE)} or "
             f"{ALLOWED_HOSTS_ENV}, which are re-read without a restart",
    )
    p.add_argument(
        "--allow-any-host", action="store_true",
        help="disable Host/Origin validation entirely (debug only)",
    )
    p.add_argument(
        "--check-host", metavar="VALUE",
        help="print the accept/reject verdict for a Host value and exit",
    )
    args = p.parse_args()

    configure_host_logging()

    if args.check_host:
        policy = resolve_policy(args.allow_host)
        parts = split_host_header(args.check_host)
        if parts is None:
            print(f"{args.check_host!r}: REJECT (malformed-host)")
            sys.exit(1)
        # Show both cases: an IP is judged against the arrival interface, which
        # only exists on a live connection, so report it as conditional.
        ok, reason = host_verdict(parts[0], None, policy)
        print(f"{args.check_host!r} -> name={parts[0]!r} port={parts[1]}")
        print(f"  verdict: {'ACCEPT' if ok else 'REJECT'} ({reason})")
        if reason == "no-local-addr":
            print("  (an IP literal is accepted iff it is the address the "
                  "connection arrives on — always true for the current LAN IP)")
        print(f"  policy: names={sorted(policy.names)} "
              f"origins={sorted(policy.origins)}")
        sys.exit(0 if ok else 1)

    db.init_db()           # make sure tables exist before serving

    if args.http:
        token = os.environ.get("WAREHOUSE_MCP_TOKEN")
        if not token:
            sys.exit("Set WAREHOUSE_MCP_TOKEN in .env first (any long random string).")
        # Resolve once here, synchronously: blocking calls are safe before the
        # event loop exists. Afterwards the policy only refreshes on a rejection.
        guard = HostGuard(
            resolve_policy(args.allow_host),
            accept_any=args.allow_any_host,
            cli_extras=args.allow_host,
        )
        srv = build_server(
            host="0.0.0.0",
            port=args.port,
            stateless_http=True,   # no per-client session state to lose
            transport_security=replaced_transport_security(),
        )
        # If make_cert.py has been run, serve https — Claude's connector UI
        # refuses plain http URLs. Teammates trust the .crt once (see
        # SHARING.md). Without certs, falls back to plain http.
        here = os.path.dirname(os.path.abspath(__file__))
        cert = os.path.join(here, "certs", "warehouse-mcp.crt")
        key = os.path.join(here, "certs", "warehouse-mcp.key")
        tls = os.path.exists(cert) and os.path.exists(key)
        ssl_args = {"ssl_certfile": cert, "ssl_keyfile": key} if tls else {}
        if tls:
            print(f"Serving warehouse MCP on https://0.0.0.0:{args.port}/mcp (TLS)")
        else:
            print(f"Serving warehouse MCP on http://0.0.0.0:{args.port}/mcp"
                  " (no certs/ — run make_cert.py to enable https)")
        if args.allow_any_host:
            host_log.warning("Host/Origin validation DISABLED (--allow-any-host)")
        else:
            host_log.info("host policy: %s | extras=%s (re-read within %ds of a "
                          "rejection, no restart) | SDK dns-rebinding check "
                          "replaced by this guard",
                          guard.describe(), ALLOWED_HOSTS_FILE,
                          int(REFRESH_COOLDOWN_SECONDS))
        if args.allow_legacy_token_path:
            print("Legacy token-path compatibility is ENABLED temporarily")

        import uvicorn
        app = BearerTokenMiddleware(
            srv.streamable_http_app(),
            token,
            guard,
            allow_legacy_path=args.allow_legacy_token_path,
        )
        uvicorn.run(app, host="0.0.0.0", port=args.port, **ssl_args)
    else:
        build_server().run()   # speaks MCP over stdio; Claude Desktop manages it
