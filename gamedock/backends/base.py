"""The interface between GameDock and a container engine.

The service layer only talks to this interface, so the real Docker backend and
the in-memory fake are interchangeable (and tests need no Docker).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import ContainerSpec, Status


@dataclass(frozen=True)
class ContainerState:
    status: Status
    detail: str = ""


class ContainerBackend(Protocol):
    def get_state(self, name: str) -> ContainerState:
        """State of the named container; NOT_CREATED if it does not exist."""

    def create(self, spec: ContainerSpec) -> None:
        """Create (but do not start) a container. No-op if it already exists."""

    def start(self, name: str) -> None: ...

    def stop(self, name: str) -> None: ...

    def remove(self, name: str) -> None:
        """Remove the container (not its data volumes). No-op if missing."""
