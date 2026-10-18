"""Gatekeeper in front of the MCP endpoint: Host/Origin checks (DNS-rebinding and cross-site protection)
and the optional bearer token. Written as plain ASGI so it sits in front of the SDK's session manager."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import LOCAL_HOSTS
from .audit import AuditLog
from .tools import current_client

MAX_BODY = 4 * 1024 * 1024
WILDCARD_BINDS = ("0.0.0.0", "::", "")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
DENY_AUDIT_EVERY = 5.0  # seconds; keeps a flood of bad requests from filling the audit log


def host_part(value: str) -> str:
    """'host:port' -> 'host' (IPv6 in brackets is kept as '[::1]')."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[: value.find("]") + 1] if "]" in value else value
    return value.rsplit(":", 1)[0] if ":" in value else value


def allowed_host_patterns(bind_host: str, extra: tuple[str, ...]) -> list[str] | None:
    """Host header values accepted. None = do not restrict (public-style bind with nothing configured;
    the bearer token, which is mandatory in that situation, is then the protection)."""
    names = list(LOOPBACK_HOSTS)
    if bind_host not in LOCAL_HOSTS and bind_host not in WILDCARD_BINDS:
        names.append(bind_host.lower() if ":" not in bind_host else f"[{bind_host}]")
    if bind_host in WILDCARD_BINDS and not extra:
        return None
    pats = []
    for n in names:
        pats += [n, f"{n}:*"]
    pats += [e.lower() for e in extra]
    return pats


def _host_ok(host: str, patterns: list[str] | None) -> bool:
    if patterns is None:
        return True
    host = host.lower()
    for p in patterns:
        if p.endswith(":*"):
            prefix = p[:-2] + ":"
            if host.startswith(prefix) and host[len(prefix):].isdigit():
                return True
        elif p == host:
            return True
    return False


def _too_big(length: str | None) -> bool:
    try:
        return int(length or 0) > MAX_BODY
    except ValueError:
        return True


class McpGuard:
    def __init__(self, inner: ASGIApp, *, token: str | None, bind_host: str, allowed_hosts: tuple[str, ...],
                 allowed_origins: tuple[str, ...], audit: AuditLog, clock=time.monotonic):
        self.inner = inner
        self._token_digest = hashlib.sha256(token.encode()).digest() if token else None
        self.host_patterns = allowed_host_patterns(bind_host, allowed_hosts)
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins}
        self.audit = audit
        self._clock = clock
        self._last_denial: dict[tuple[str, str], float] = {}

    # -- checks
    def _token_ok(self, header: str | None) -> bool:
        if self._token_digest is None:
            return True
        scheme, _, cred = (header or "").partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(hashlib.sha256(cred.strip().encode()).digest(), self._token_digest)

    def _origin_ok(self, origin: str, host: str) -> bool:
        if origin.rstrip("/").lower() in self.allowed_origins:
            return True
        return urlparse(origin).netloc.lower() == host.lower() and bool(host)  # same-origin only

    def _deny(self, scope: Scope, status: int, reason: str, public_msg: str, headers: dict | None = None):
        client = (scope.get("client") or ("", 0))[0]
        now = self._clock()
        key = (reason, client)
        if now - self._last_denial.get(key, -1e9) >= DENY_AUDIT_EVERY:
            self._last_denial[key] = now
            if len(self._last_denial) > 500:
                self._last_denial.clear()
            self.audit.record("(connection)", {}, "denied", reason, client)
        return JSONResponse({"error": public_msg}, status_code=status, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        h = Headers(scope=scope)
        host = h.get("host", "")
        resp = None
        if not _host_ok(host, self.host_patterns):
            resp = self._deny(scope, 421, "host header not allowed", "Host header not allowed for /mcp. "
                              "Set GAMEDOCK_MCP_ALLOWED_HOSTS if you reach GameDock by another name.")
        elif (origin := h.get("origin")) is not None and not self._origin_ok(origin, host):
            resp = self._deny(scope, 403, "origin not allowed", "Origin not allowed for /mcp. "
                              "Set GAMEDOCK_MCP_ALLOWED_ORIGINS to allow a browser-based client.")
        elif not self._token_ok(h.get("authorization")):
            resp = self._deny(scope, 401, "missing or wrong bearer token",
                              "Missing or invalid bearer token.", {"WWW-Authenticate": 'Bearer realm="gamedock-mcp"'})
        elif _too_big(h.get("content-length")):
            resp = self._deny(scope, 413, "request too large", "Request too large.")
        if resp is not None:
            await resp(scope, receive, send)
            return
        reset = current_client.set((scope.get("client") or ("", 0))[0] or "")
        try:
            await self.inner(scope, receive, send)
        finally:
            current_client.reset(reset)
