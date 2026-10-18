"""In-memory pretend container engine for development and tests."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from threading import Lock
from typing import Callable

from ..errors import BackendError
from ..models import ContainerSpec, ContainerStats, Status
from .base import ContainerState


class FakeBackend:
    def __init__(self) -> None:
        self._containers: dict[str, tuple[ContainerSpec, Status]] = {}
        self._container_image: dict[str, str] = {}
        self.local_images: dict[str, str] = {}   # image name -> id stored locally
        self.registry: dict[str, str] = {}       # image name -> newest id "on the internet"
        self._counter = 0
        self.clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._started: dict[str, datetime] = {}
        self._tick = 0
        self.stats_override: dict[str, ContainerStats] = {}  # tests can pin exact numbers
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
            if spec.name not in self._containers:
                self._containers[spec.name] = (spec, Status.STOPPED)
                self._container_image[spec.name] = self.local_images.setdefault(
                    spec.image, self.registry.setdefault(spec.image, self._new_id()))

    def start(self, name: str) -> None:
        with self._lock:
            self.calls.append(("start", name))
            self._maybe_fail("start")
            if name not in self._containers:
                raise BackendError(f"no such container: {name}")
            if self._containers[name][1] != Status.RUNNING:
                self._started[name] = self.clock()
            self._containers[name] = (self._containers[name][0], Status.RUNNING)

    def stop(self, name: str) -> None:
        with self._lock:
            self.calls.append(("stop", name))
            self._maybe_fail("stop")
            if name not in self._containers:
                raise BackendError(f"no such container: {name}")
            self._started.pop(name, None)
            self._containers[name] = (self._containers[name][0], Status.STOPPED)

    def remove(self, name: str) -> None:
        with self._lock:
            self.calls.append(("remove", name))
            self._containers.pop(name, None)
            self._container_image.pop(name, None)
            self._started.pop(name, None)

    # -- image simulation ------------------------------------------------
    def _new_id(self) -> str:
        self._counter += 1
        return f"sha256:{self._counter:012x}fakeimage"

    def publish_new_image(self, image: str) -> str:
        """Test/demo helper: pretend the image publisher released a new version."""
        self.registry[image] = self._new_id()
        return self.registry[image]

    def pull_image(self, image: str) -> None:
        with self._lock:
            self.calls.append(("pull", image))
            self._maybe_fail("pull")
            self.local_images[image] = self.registry.setdefault(image, self._new_id())

    def get_image_id(self, image: str) -> str | None:
        return self.local_images.get(image)

    def get_container_image_id(self, name: str) -> str | None:
        return self._container_image.get(name)

    def get_stats(self, names: list[str]) -> dict[str, ContainerStats]:
        """Pretend numbers that wander around a per-container base so graphs look alive."""
        with self._lock:
            self.calls.append(("stats", ",".join(names)))
            self._maybe_fail("stats")
            self._tick += 1
            t = self._tick
            out: dict[str, ContainerStats] = {}
            for name in names:
                entry = self._containers.get(name)
                if entry is None:
                    continue
                if entry[1] != Status.RUNNING:
                    out[name] = ContainerStats(running=False, mem_limit=8 * 1024**3)
                    continue
                if name in self.stats_override:
                    out[name] = self.stats_override[name]
                    continue
                seed = sum(ord(c) for c in name)
                cpu = max(0.2, 8 + seed % 45 + 14 * math.sin(t / 4 + seed) + 5 * math.sin(t / 1.3 + seed))
                mem = int((600 + seed % 25 * 90) * 1024**2 + t * 3 * 1024**2)
                out[name] = ContainerStats(
                    running=True, started_at=self._started.get(name), cpu_percent=round(cpu, 2),
                    mem_used=mem, mem_limit=8 * 1024**3,
                    net_rx=t * (20_000 + seed % 7 * 9_000), net_tx=t * (60_000 + seed % 5 * 31_000),
                    block_read=t * 4096, block_write=t * 150_000, pids=20 + seed % 30)
            return out
