"""Docker backend that shells out to the `docker` CLI (no extra Python deps).

Environment variables are passed to `docker create` as `-e NAME` with the value
placed in the child process environment, so values never appear in the process
list (`ps`).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from typing import Callable

from ..errors import BackendError
from ..models import ContainerSpec, ContainerStats, Status
from .base import ContainerState
from .docker_stats import INSPECT_FORMAT, parse_inspect_lines, parse_stats_lines

STOP_TIMEOUT_SECONDS = 60  # give game servers time to save on SIGTERM
PULL_TIMEOUT_SECONDS = 3600  # game images can be many GB

Runner = Callable[..., subprocess.CompletedProcess]

_STATUS_MAP = {
    "running": Status.RUNNING,
    "restarting": Status.RUNNING,
    "paused": Status.STOPPED,
    "created": Status.STOPPED,
    "exited": Status.STOPPED,
    "dead": Status.ERROR,
}


class DockerCliBackend:
    def __init__(self, docker_bin: str = "docker", runner: Runner | None = None) -> None:
        self.docker_bin = docker_bin
        self._runner = runner or subprocess.run

    def _run(self, args: list[str], env: dict[str, str] | None = None, check: bool = True,
             timeout: int | None = None):
        full_env = {**os.environ, **env} if env else None
        try:
            proc = self._runner(
                [self.docker_bin, *args],
                capture_output=True,
                text=True,
                env=full_env,
                timeout=timeout or STOP_TIMEOUT_SECONDS + 30,
            )
        except FileNotFoundError as e:
            raise BackendError("docker command not found; is Docker installed and on PATH?") from e
        except subprocess.TimeoutExpired as e:
            raise BackendError(f"docker {args[0]} timed out") from e
        if check and proc.returncode != 0:
            raise BackendError(f"docker {args[0]} failed: {(proc.stderr or proc.stdout).strip()}")
        return proc

    def get_state(self, name: str) -> ContainerState:
        proc = self._run(["inspect", "--format", "{{.State.Status}}", name], check=False)
        if proc.returncode != 0:
            err = (proc.stderr or "").lower()
            if "no such" in err:
                return ContainerState(Status.NOT_CREATED)
            raise BackendError(f"docker inspect failed: {proc.stderr.strip()}")
        raw = proc.stdout.strip()
        return ContainerState(_STATUS_MAP.get(raw, Status.ERROR), detail=raw)

    @staticmethod
    def build_create_args(spec: ContainerSpec) -> list[str]:
        args = ["create", "--name", spec.name, "--restart", "unless-stopped"]
        for k, v in spec.labels.items():
            args += ["--label", f"{k}={v}"]
        for p in spec.ports:
            args += ["-p", f"{p.host}:{p.container}/{p.protocol}"]
        for v in spec.volumes:
            args += ["-v", f"{v.host}:{v.container}"]
        for k in spec.env:
            args += ["-e", k]
        args.append(spec.image)
        return args

    def create(self, spec: ContainerSpec) -> None:
        if self.get_state(spec.name).status != Status.NOT_CREATED:
            return
        self._run(self.build_create_args(spec), env=spec.env)

    def start(self, name: str) -> None:
        self._run(["start", name])

    def stop(self, name: str) -> None:
        self._run(["stop", "-t", str(STOP_TIMEOUT_SECONDS), name])

    def remove(self, name: str) -> None:
        if self.get_state(name).status == Status.NOT_CREATED:
            return
        self._run(["rm", "-f", name])

    def pull_image(self, image: str) -> None:
        self._run(["pull", image], timeout=PULL_TIMEOUT_SECONDS)

    def get_image_id(self, image: str) -> str | None:
        proc = self._run(["image", "inspect", "--format", "{{.Id}}", image], check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def get_container_image_id(self, name: str) -> str | None:
        proc = self._run(["inspect", "--format", "{{.Image}}", name], check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def get_stats(self, names: list[str]) -> dict[str, ContainerStats]:
        """One `docker inspect` for every name (state, start time, memory limit) and, only if something is
        running, one `docker stats --no-stream` for the running ones. Two engine calls at most, however many
        servers there are."""
        if not names:
            return {}
        # inspect exits non-zero if some name does not exist but still prints the others
        proc = self._run(["inspect", "--format", INSPECT_FORMAT, *names], check=False)
        info = parse_inspect_lines(proc.stdout)
        if not info and proc.returncode != 0 and "no such" not in (proc.stderr or "").lower():
            raise BackendError(f"docker inspect failed: {(proc.stderr or '').strip()}")
        running = [n for n, (status, _, _) in info.items() if status in ("running", "restarting")]
        live: dict[str, ContainerStats] = {}
        if running:
            sp = self._run(["stats", "--no-stream", "--format", "{{json .}}", *running])
            live = parse_stats_lines(sp.stdout)
        result: dict[str, ContainerStats] = {}
        for name, (status, started, limit) in info.items():
            if name in running:
                s = live.get(name, ContainerStats(running=True))
                result[name] = replace(s, started_at=started, mem_limit=limit or s.mem_limit)
            else:
                result[name] = ContainerStats(running=False, mem_limit=limit)
        return result
