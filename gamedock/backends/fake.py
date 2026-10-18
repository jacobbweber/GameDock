"""In-memory pretend container engine for development and tests."""
from __future__ import annotations

from threading import Lock

from ..errors import BackendError
from ..models import ContainerSpec, Status
from .base import ContainerState


class FakeBackend:
    def __init__(self) -> None:
        self._containers: dict[str, tuple[ContainerSpec, Status]] = {}
        self._lock = Lock()
        self.calls: list[tuple[str, str]] = []  # (action, name), handy in tests
        self.fail_next: str | None = None  # set to an action name to make it raise once

    def _maybe_fail(self, action: str) -> None:
        if self.fail_next == action:
            self.fail_next = None
            raise BackendError(f"simulated failure during {action}")

    def get_state(self, name: str) -> ContainerState:
        with self._lock:
            entry = self._containers.get(name)
            return ContainerState(entry[1] if entry else Status.NOT_CREATED)

    def create(self, spec: ContainerSpec) -> None:
        with self._lock:
            self.calls.append(("create", spec.name))
            self._maybe_fail("create")
            self._containers.setdefault(spec.name, (spec, Status.STOPPED))

    def start(self, name: str) -> None:
        with self._lock:
            self.calls.append(("start", name))
            self._maybe_fail("start")
            if name not in self._containers:
                raise BackendError(f"no such container: {name}")
            self._containers[name] = (self._containers[name][0], Status.RUNNING)

    def stop(self, name: str) -> None:
        with self._lock:
            self.calls.append(("stop", name))
            self._maybe_fail("stop")
            if name not in self._containers:
                raise BackendError(f"no such container: {name}")
            self._containers[name] = (self._containers[name][0], Status.STOPPED)

    def remove(self, name: str) -> None:
        with self._lock:
            self.calls.append(("remove", name))
            self._containers.pop(name, None)
