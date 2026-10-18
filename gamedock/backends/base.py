"""The interface between GameDock and a container engine.

The service layer only talks to this interface, so the real Docker backend and
the in-memory fake are interchangeable (and tests need no Docker).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import ContainerSpec, ContainerStats, Status


@dataclass(frozen=True)
class ContainerState:
    status: Status
    detail: str = ""
    health: str = ""      # Docker health check result: healthy | unhealthy | starting | "" (image has no health check)
    started_at: str = ""  # when the current run began (RFC 3339 text); changes on every restart


@dataclass(frozen=True)
class DockerDiagnosis:
    """Answer to "can GameDock use the container engine?" (never raises; used by the doctor and the UI)."""

    installed: bool
    path: str | None
    client_version: str | None
    reachable: bool
    server_version: str | None
    kind: str  # ok | not_installed | daemon_down | permission | error | fake
    message: str  # plain language, includes the fix
    fix: str = ""


class ContainerBackend(Protocol):
    def get_state(self, name: str) -> ContainerState:
        """State of the named container; NOT_CREATED if it does not exist."""

    def create(self, spec: ContainerSpec) -> None:
        """Create (but do not start) a container. No-op if it already exists."""

    def start(self, name: str) -> None: ...

    def stop(self, name: str) -> None: ...

    def remove(self, name: str) -> None:
        """Remove the container (not its data volumes). No-op if missing."""

    def pull_image(self, image: str) -> None:
        """Download the newest version of an image."""

    def get_image_id(self, image: str) -> str | None:
        """Id (digest) of the locally stored image, or None if it is not present."""

    def get_container_image_id(self, name: str) -> str | None:
        """Id of the image the container was created from, or None if no such container."""

    def get_logs(self, name: str, tail: int = 200, since: str | None = None) -> str:
        """The last `tail` lines the container wrote (stdout and stderr merged, oldest first), optionally only those
        since `since` (an RFC 3339 time as in ContainerState.started_at). Empty text if there is nothing yet."""

    def remote_image_digest(self, image: str) -> str | None:
        """Digest the registry currently serves for `image` (looked up WITHOUT downloading the image), or None if the
        registry did not say. Raises BackendError, with a plain sentence, when the registry cannot be asked."""

    def local_image_digests(self, ref: str) -> tuple[str, ...]:
        """Registry digests recorded for a locally stored image (`ref` is an image name or an image id). Empty if
        none are known (for example an image built on this machine)."""

    def exec_in(self, name: str, argv: list[str], timeout: int = 60) -> str:
        """Run a command (no shell) inside the running container and return what it printed. Raises BackendError."""

    def diagnose(self) -> DockerDiagnosis:
        """Check that the engine is installed and reachable. Must not raise."""

    def get_stats(self, names: list[str]) -> dict[str, ContainerStats]:
        """Resource numbers for the named containers, using as few engine calls as possible (not one per
        container). Containers that do not exist are left out; stopped ones come back with running=False."""
