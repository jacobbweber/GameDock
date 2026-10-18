"""In-memory pretend container engine for development and tests."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Callable

from ..errors import BackendError
from ..models import ContainerSpec, ContainerStats, Status
from . import docker_errors
from .base import ContainerState, DockerDiagnosis


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
        self.health: dict[str, str] = {}   # container name -> "" | starting | healthy | unhealthy (pretend health check)
        self._logs: dict[str, list[str]] = {}
        self.offline = False                # True = the pretend registry cannot be reached
        self.exec_log: list[tuple[str, list[str]]] = []  # (container, command) of every exec_in
        self.delays: dict[str, float] = {}  # action name -> seconds to wait (demos: make create/stop look slow)
        self.image_facts: dict[str, dict] = {}   # image name -> what `docker image inspect` would show (only for local images)
        self.remote_facts: dict[str, dict] = {}  # image name -> {"digest", "platforms"} the pretend registry shows
        self._start_hooks: dict[str, Callable[[], None]] = {}  # container name -> called each time it starts
        self.restart_counts: dict[str, int] = {}              # container name -> pretend restart count
        self.exit_info: dict[str, tuple[int, bool]] = {}      # container name -> (exit code, out of memory) while stopped
        self.unpublished_ports: dict[str, set[str]] = {}      # container name -> {"27015/udp"} pretend not published

    def _pause(self, action: str) -> None:
        d = self.delays.get(action, 0)
        if d:
            time.sleep(d)  # outside the lock so other requests keep working, like a real slow docker call

    # -- SteamCMD game pretend ---------------------------------------------
    def simulate_steam_game(self, name: str, app_id: str, build: Callable[[], tuple[str, int | None]],
                            version: Callable[[str], str] | None = None) -> None:
        """Pretend the container holds a SteamCMD game that updates itself every time it starts, like the real images
        do (UPDATE_ON_BOOT). Each start writes Steam's appmanifest_<app id>.acf into the first volume's host folder
        with the build id given by `build()` -> (build id, unix time of that build), then logs a version line from
        `version(build id)` (default: ``Game version is v1.<build id>``). Make `build()` return an older id to pretend
        that the game did not update on restart."""
        from pathlib import Path

        from ..steam import render_manifest

        def write() -> None:
            entry = self._containers.get(name)
            if entry is None or not entry[0].volumes:
                return
            folder = Path(entry[0].volumes[0].host) / "steamapps"
            folder.mkdir(parents=True, exist_ok=True)
            b, when = build()
            (folder / f"appmanifest_{app_id}.acf").write_text(render_manifest(app_id, b, when), encoding="utf-8")
            self._logs.setdefault(name, []).append(
                "[fake engine] " + (version(b) if version else f"Game version is v1.{b}"))

        self._start_hooks[name] = write

    # -- logs (pretend) ----------------------------------------------------
    def set_logs(self, name: str, text: str) -> None:
        self._logs[name] = text.splitlines()

    def append_log(self, name: str, *lines: str) -> None:
        self._logs.setdefault(name, []).extend(lines)

    def get_logs(self, name: str, tail: int = 200, since: str | None = None) -> str:
        with self._lock:
            self.calls.append(("logs", name))
            self._maybe_fail("logs")
            if name not in self._containers:
                raise BackendError(f"The container '{name}' does not exist yet, so there are no logs.")
            lines = self._logs.get(name, [])
            return "\n".join(lines[-max(1, tail):]) + ("\n" if lines else "")

    def diagnose(self) -> DockerDiagnosis:
        return DockerDiagnosis(True, None, None, True, "fake", "fake", "Using the built-in pretend engine (no real containers).")

    def _maybe_fail(self, action: str) -> None:
        if self.fail_next == action:
            self.fail_next = None
            raise BackendError(f"simulated failure during {action}")

    def get_state(self, name: str) -> ContainerState:
        with self._lock:
            entry = self._containers.get(name)
            if entry is None:
                return ContainerState(Status.NOT_CREATED)
            running = entry[1] == Status.RUNNING
            started = self._started.get(name)
            return ContainerState(entry[1], health=self.health.get(name, "") if running else "",
                                  started_at=started.isoformat() if running and started else "")

    def create(self, spec: ContainerSpec) -> None:
        self._pause("create")
        with self._lock:
            self.calls.append(("create", spec.name))
            self._maybe_fail("create")
            if spec.name not in self._containers:
                self._containers[spec.name] = (spec, Status.STOPPED)
                self._container_image[spec.name] = self.local_images.setdefault(
                    spec.image, self.registry.setdefault(spec.image, self._new_id()))

    def start(self, name: str) -> None:
        self._pause("start")
        with self._lock:
            self.calls.append(("start", name))
            self._maybe_fail("start")
            if name not in self._containers:
                raise BackendError(f"no such container: {name}")
            if self._containers[name][1] != Status.RUNNING:
                self._started[name] = self.clock()
                self._logs.setdefault(name, []).append("[fake engine] container started")
                hook = self._start_hooks.get(name)
                if hook:
                    hook()
            self._containers[name] = (self._containers[name][0], Status.RUNNING)

    def stop(self, name: str, timeout: int | None = None) -> None:
        self._pause("stop")
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
            self._logs.pop(name, None)
            self.health.pop(name, None)

    # -- image simulation ------------------------------------------------
    def _new_id(self) -> str:
        self._counter += 1
        return f"sha256:{self._counter:012x}fakeimage"

    def publish_new_image(self, image: str) -> str:
        """Test/demo helper: pretend the image publisher released a new version."""
        self.registry[image] = self._new_id()
        return self.registry[image]

    def pull_image(self, image: str) -> None:
        self._pause("pull")
        with self._lock:
            self.calls.append(("pull", image))
            self._maybe_fail("pull")
            self.local_images[image] = self.registry.setdefault(image, self._new_id())

    def remote_image_digest(self, image: str) -> str | None:
        with self._lock:
            self.calls.append(("remote_digest", image))
            self._maybe_fail("remote_digest")
            if self.offline:
                raise BackendError(f"Could not ask the registry about {image} (no internet). Are you online?")
            return self.registry.get(image)

    def inspect_image(self, image: str) -> dict:
        with self._lock:
            self.calls.append(("inspect_image", image))   # note: never "pull" or "create"
            local = image in self.local_images
            remote = self.remote_facts.get(image)
            out = {"image": image, "local": local, "local_details": self.image_facts.get(image) if local else None,
                   "remote": None if self.offline else remote, "remote_error": ""}
            if remote is None or self.offline:
                out["remote"] = None
                out["remote_error"] = ("The registry could not be asked (no internet)." if self.offline else
                                       "The registry could not be asked (the image was not found there).")
                out["remote_error_kind"] = "offline" if self.offline else "not_found"
                out["failure"] = (docker_errors.registry_failure(out["remote_error_kind"], image))
            return out

    def local_image_digests(self, ref: str) -> tuple[str, ...]:
        with self._lock:
            known = set(self.local_images.values()) | set(self._container_image.values())
            if ref in known:
                return (ref,)
            got = self.local_images.get(ref)
            return (got,) if got else ()

    def exec_in(self, name: str, argv: list[str], timeout: int = 60) -> str:
        with self._lock:
            self.calls.append(("exec", name))
            self._maybe_fail("exec")
            entry = self._containers.get(name)
            if entry is None or entry[1] != Status.RUNNING:
                raise BackendError(f"The container '{name}' is not running, so a command cannot be run in it.")
            self.exec_log.append((name, list(argv)))
            return "ok\n"

    def container_details(self, name: str) -> dict | None:
        with self._lock:
            self.calls.append(("container_details", name))
            entry = self._containers.get(name)
            if entry is None:
                return None
            spec, status = entry
            running = status == Status.RUNNING
            hidden = self.unpublished_ports.get(name, set())
            started = self._started.get(name)
            code, oom = self.exit_info.get(name, (0, False))
            return {
                "status": "running" if running else "exited" if name in self.exit_info else "created",
                "exit_code": None if running else code, "oom_killed": False if running else oom,
                "restart_count": self.restart_counts.get(name, 0),
                "started_at": started.isoformat() if running and started else "", "finished_at": "", "error": "",
                "health": self.health.get(name, "") if running else "",
                "ports": [{"container": p.container, "protocol": p.protocol, "host_ip": "0.0.0.0", "host_port": p.host}
                          for p in spec.ports if f"{p.container}/{p.protocol}" not in hidden],
                "unpublished": sorted(hidden),
                "mounts": [{"source": v.host, "target": v.container, "writable": True, "type": "bind"}
                           for v in spec.volumes],
                "image": spec.image,
            }

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
