"""Short-lived, single-use confirmation tokens for destructive MCP tools.

A token is tied to ONE action and its EXACT arguments. Change a single argument and it no longer works.
Tokens live in memory only (a restart invalidates them), expire after about two minutes, and are
consumed the first time they are checked, whether or not the action then succeeds.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_TTL = 120.0
MAX_PENDING = 200


class ConfirmationError(Exception):
    """The token is missing, expired, already used, or for different arguments. Message is for the agent."""


@dataclass
class _Pending:
    action: str
    key: str
    expires: float


def _key(action: str, args: dict[str, Any]) -> str:
    return json.dumps({"action": action, "args": args}, sort_keys=True, separators=(",", ":"), default=str)


class ConfirmationStore:
    def __init__(self, ttl: float = DEFAULT_TTL, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def _purge(self, now: float) -> None:
        for t in [t for t, p in self._pending.items() if p.expires <= now]:
            del self._pending[t]
        while len(self._pending) >= MAX_PENDING:  # drop the oldest
            del self._pending[next(iter(self._pending))]

    def issue(self, action: str, args: dict[str, Any]) -> str:
        now = self._clock()
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._purge(now)
            self._pending[token] = _Pending(action, _key(action, args), now + self.ttl)
        return token

    def consume(self, token: str, action: str, args: dict[str, Any]) -> None:
        """Raise ConfirmationError unless `token` is valid for exactly this action and these arguments."""
        now = self._clock()
        with self._lock:
            p = self._pending.pop(token, None)  # single use: gone even if the check below fails
            self._purge(now)
        if p is None:
            raise ConfirmationError(
                "That confirmation token is unknown, already used, or expired. Call the tool again without a "
                "token to get a fresh summary and token.")
        if p.expires <= now:
            raise ConfirmationError("That confirmation token has expired (they last about 2 minutes). "
                                    "Call the tool again without a token to get a new one.")
        if p.action != action or p.key != _key(action, args):
            raise ConfirmationError(
                "That confirmation token was issued for different arguments or a different action, so it was "
                "discarded. Call the tool again without a token to get a new summary and token.")
