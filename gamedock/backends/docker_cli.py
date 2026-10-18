"""Docker backend that shells out to the `docker` CLI (no extra Python deps).

Environment variables are passed to `docker create` as `-e NAME` with the value
placed in the child process environment, so values never appear in the process
list (`ps`).
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable

from ..errors import BackendError
from ..models import ContainerSpec, Status
from .base import ContainerState

STOP_TIMEOUT_SECONDS = 60  # give game servers time to save on SIGTERM

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

    def _run(self, args: list[str], env: dict[str, str] | None = None, check: bool = True):
        full_env = {**os.environ, **env} if env else None
        try:
            proc = self._runner(
                [self.docker_bin, *args],
                capture_output=True,
                text=True,
                env=full_env,
                timeout=STOP_TIMEOUT_SECONDS + 30,
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
