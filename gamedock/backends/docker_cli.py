"""Docker backend that shells out to the `docker` CLI (no extra Python deps).

Safety rules followed here:
  * every command is an argument list (never a shell string), so nothing in a name or value can be
    interpreted by a shell;
  * setting values are passed as `-e NAME` with the value in the child process environment, so they never
    appear in the process list (`ps`); names that could change how the docker command itself behaves
    (PATH, DOCKER_HOST, LD_PRELOAD...) are refused;
  * GameDock only touches containers it created (label gamedock.managed=true). A container that merely has
    the same name is reported as an error and left alone;
  * raw docker errors are turned into plain-language messages (see docker_errors.py).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import Callable

from ..errors import BackendError, DockerUnavailableError, ForeignContainerError
from ..models import IMAGE_RE, ContainerSpec, ContainerStats, Status, is_reserved_env_name
from . import docker_errors
from .base import ContainerState, DockerDiagnosis
from .container_inspect import parse_container_details
from .image_inspect import parse_imagetools, parse_local_inspect, parse_manifest
from .docker_stats import INSPECT_FORMAT, parse_inspect_lines, parse_stats_lines

STOP_TIMEOUT_SECONDS = 60  # default grace period for game servers to save on SIGTERM
PULL_TIMEOUT_SECONDS = 3600  # game images can be many GB; `docker create` also pulls a missing image
QUICK_TIMEOUT_SECONDS = 20  # looking at things must never hang the web page
STATS_TIMEOUT_SECONDS = 45
START_TIMEOUT_SECONDS = 120
LABEL_MANAGED = "gamedock.managed"

Runner = Callable[..., subprocess.CompletedProcess]

_STATUS_MAP = {
    "running": Status.RUNNING,
    "restarting": Status.RUNNING,  # still "running" so that Stop works on a crash-looping server
    "paused": Status.STOPPED,
    "created": Status.STOPPED,
    "exited": Status.STOPPED,
    "dead": Status.ERROR,
}

STATE_FORMAT = ("{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|"
                '{{index .Config.Labels "' + LABEL_MANAGED + '"}}|'
                "{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.State.StartedAt}}")
MAX_LOG_LINES = 5000
SINCE_RE = re.compile(r"^[0-9T:.+\-Z]{10,48}$")  # an RFC 3339 time; never anything that could look like an option


@dataclass(frozen=True)
class _Found:
    raw: str
    exit_code: int
    oom: bool
    managed: bool
    health: str = ""
    started_at: str = ""


def foreign_message(name: str) -> str:
    return (f"A container named '{name}' exists but was not created by GameDock, so GameDock will not touch it. "
            "Rename or remove that container with the docker command, or give this server a different name.")


def describe_state(f: _Found) -> str:
    if f.raw == "exited" and (f.exit_code or f.oom):
        why = "it ran out of memory" if f.oom else f"exit code {f.exit_code}"
        return f"exited unexpectedly ({why}); check 'docker logs' for this container"
    if f.raw == "restarting":
        return "restarting (the game keeps stopping; check 'docker logs' for this container)"
    return f.raw


def _mount_arg(host: str, container: str) -> str:
    """`--mount` value. Unlike -v it has no ':' ambiguity and Docker refuses to invent a missing host folder
    (which it would create owned by root). Fields are CSV-quoted so a ',' in a path is safe."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(["type=bind", f"source={host}", f"target={container}"])
    return buf.getvalue()


class DockerCliBackend:
    def __init__(self, docker_bin: str = "docker", runner: Runner | None = None,
                 stop_timeout: int = STOP_TIMEOUT_SECONDS) -> None:
        self.docker_bin = docker_bin
        self._runner = runner or subprocess.run
        self.stop_timeout = int(stop_timeout)

    # ------------------------------------------------------------------ plumbing
    def _run(self, args: list[str], env: dict[str, str] | None = None, check: bool = True,
             timeout: int | None = None, merge_stderr: bool = False):
        full_env = {**os.environ, **env} if env else None
        # merge_stderr: one stream with stdout and stderr interleaved in the order the game wrote them (logs)
        streams = ({"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT} if merge_stderr
                   else {"capture_output": True})
        try:
            proc = self._runner(
                [self.docker_bin, *args],
                **streams,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=full_env,
                timeout=timeout or START_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as e:
            raise DockerUnavailableError(docker_errors.NOT_INSTALLED, "not_installed") from e
        except PermissionError as e:
            raise DockerUnavailableError(
                f"The docker command could not be run (permission denied for '{self.docker_bin}').",
                "permission") from e
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                f"docker {args[0]} did not finish in time. Docker may be overloaded or stuck; "
                "check 'systemctl status docker'.") from e
        if check and proc.returncode != 0:
            raise docker_errors.friendly(args[0], proc.stderr or proc.stdout)
        return proc

    def _inspect(self, name: str) -> _Found | None:
        """What we know about the named container, or None if it does not exist."""
        proc = self._run(["container", "inspect", "--format", STATE_FORMAT, name], check=False,
                         timeout=QUICK_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            err = proc.stderr or ""
            if "no such" in err.lower() and not docker_errors.classify(err):
                return None
            raise docker_errors.friendly("inspect", err or proc.stdout)
        parts = proc.stdout.strip().split("|")
        if len(parts) not in (4, 6):  # older/newer answers without the health and start time are still understood
            raise BackendError(f"Unexpected answer from docker inspect: {proc.stdout.strip()!r}")
        try:
            code = int(parts[1])
        except ValueError:
            code = 0
        health, started = (parts[4], parts[5]) if len(parts) == 6 else ("", "")
        return _Found(parts[0], code, parts[2] == "true", parts[3] == "true", health, started)

    def _ours(self, name: str) -> _Found | None:
        found = self._inspect(name)
        if found is not None and not found.managed:
            raise ForeignContainerError(foreign_message(name))
        return found

    # ------------------------------------------------------------------ interface
    def get_state(self, name: str) -> ContainerState:
        found = self._inspect(name)
        if found is None:
            return ContainerState(Status.NOT_CREATED)
        if not found.managed:
            return ContainerState(Status.ERROR, detail=foreign_message(name))
        status = _STATUS_MAP.get(found.raw, Status.ERROR)
        running = found.raw in ("running", "restarting")
        return ContainerState(status, detail=describe_state(found), health=found.health if running else "",
                              started_at=found.started_at if running else "")

    def get_logs(self, name: str, tail: int = 200, since: str | None = None) -> str:
        found = self._ours(name)
        if found is None:
            raise BackendError(f"The container '{name}' does not exist yet, so there are no logs.")
        args = ["logs", "--tail", str(max(1, min(int(tail), MAX_LOG_LINES)))]
        if since:
            if not SINCE_RE.match(since):
                raise BackendError("Invalid 'since' time for the log request")
            args += ["--since", since]
        proc = self._run([*args, name], timeout=QUICK_TIMEOUT_SECONDS, merge_stderr=True)
        return proc.stdout or ""

    @staticmethod
    def check_image_name(image: str) -> None:
        if not IMAGE_RE.match(image or ""):
            raise BackendError(f"'{image}' is not a valid image name")

    @staticmethod
    def build_create_args(spec: ContainerSpec, stop_timeout: int = STOP_TIMEOUT_SECONDS) -> list[str]:
        DockerCliBackend.check_image_name(spec.image)
        for k in spec.env:
            if is_reserved_env_name(k):
                raise BackendError(f"The setting name '{k}' is reserved and cannot be passed to a container")
        # restart policy: come back after a crash or a reboot, but stay stopped once the user stopped it.
        # stop-timeout: the same grace period applies when Docker itself stops the container (host shutdown).
        args = ["create", "--name", spec.name, "--restart", "unless-stopped", "--stop-timeout", str(stop_timeout)]
        for k, v in spec.labels.items():
            args += ["--label", f"{k}={v}"]
        for p in spec.ports:
            args += ["-p", f"{p.host}:{p.container}/{p.protocol}"]
        for v in spec.volumes:
            args += ["--mount", _mount_arg(v.host, v.container)]
        for k, v in spec.env.items():
            # A value is never put on the command line (it could be a password): "-e NAME" makes docker read it from
            # its own environment. An EMPTY value carries no secret, so it is written out as "-e NAME=", which every
            # docker version reads as "present but empty" (images such as Enshrouded's treat that differently from
            # a missing variable).
            args += ["-e", f"{k}=" if v == "" else k]
        args += ["--", spec.image]
        return args

    def create(self, spec: ContainerSpec) -> None:
        if self._ours(spec.name) is not None:
            return
        args = self.build_create_args(spec, spec.stop_timeout or self.stop_timeout)
        self._run(args, env=spec.env, timeout=PULL_TIMEOUT_SECONDS)  # also downloads the image if it is missing

    def start(self, name: str) -> None:
        found = self._ours(name)
        if found is None:
            raise BackendError(f"The container '{name}' does not exist. Deploy the server again or press Start to recreate it.")
        if found.raw == "paused":
            self._run(["unpause", name], timeout=QUICK_TIMEOUT_SECONDS)
            return
        self._run(["start", name], timeout=START_TIMEOUT_SECONDS)

    def stop(self, name: str, timeout: int | None = None) -> None:
        self._ours(name)
        grace = int(timeout or self.stop_timeout)
        self._run(["stop", "-t", str(grace), name], timeout=grace + 30)

    def remove(self, name: str) -> None:
        found = self._ours(name)
        if found is None:
            return
        # No -f: a running container is refused instead of being killed. (Paused/dead ones need -f.)
        force = ["-f"] if found.raw in ("paused", "dead") else []
        self._run(["rm", *force, name], timeout=START_TIMEOUT_SECONDS)

    def pull_image(self, image: str) -> None:
        self.check_image_name(image)
        self._run(["pull", "--", image], timeout=PULL_TIMEOUT_SECONDS)

    def get_image_id(self, image: str) -> str | None:
        self.check_image_name(image)
        proc = self._run(["image", "inspect", "--format", "{{.Id}}", "--", image], check=False,
                         timeout=QUICK_TIMEOUT_SECONDS)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def remote_image_digest(self, image: str) -> str | None:
        """Ask the registry which digest `image` has now, without pulling it. Tries `docker buildx imagetools
        inspect` (prints the digest of the whole multi-platform image, which is what a pull records), then the
        older `docker manifest inspect`."""
        self.check_image_name(image)
        digest_re = re.compile(r"sha256:[0-9a-f]{64}")
        last = ""
        proc = self._run(["buildx", "imagetools", "inspect", image, "--format", "{{.Manifest.Digest}}"], check=False,
                         timeout=QUICK_TIMEOUT_SECONDS * 2)
        if proc.returncode == 0 and digest_re.fullmatch(proc.stdout.strip()):
            return proc.stdout.strip()
        last = proc.stderr or proc.stdout or last
        proc = self._run(["buildx", "imagetools", "inspect", image], check=False, timeout=QUICK_TIMEOUT_SECONDS * 2)
        if proc.returncode == 0:
            m = re.search(r"^Digest:\s*(sha256:[0-9a-f]{64})\s*$", proc.stdout, re.M)
            if m:
                return m.group(1)
        last = proc.stderr or proc.stdout or last
        proc = self._run(["manifest", "inspect", "-v", image], check=False, timeout=QUICK_TIMEOUT_SECONDS * 2)
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                data = None
            if isinstance(data, dict):  # a single-platform image: its manifest digest is what a pull records
                d = (data.get("Descriptor") or {}).get("digest", "")
                if digest_re.fullmatch(d):
                    return d
            return None  # a multi-platform list: this command cannot give the digest of the list
        last = proc.stderr or proc.stdout or last
        first = (last.strip().splitlines() or ["no answer"])[0][:200]
        raise BackendError(f"Could not ask the registry about {image} ({first}). Are you online, and is the image "
                           "name right?")

    def inspect_image(self, image: str) -> dict:
        """Read-only look at an image, for people (and AI agents) writing a catalog entry. NEVER pulls or runs the
        image: `docker image inspect` only reads images that are already stored here, and the registry is asked
        with `buildx imagetools inspect` (then `manifest inspect`), which read metadata only.
        Returns {"image", "local", "local_details", "remote", "remote_error"}."""
        self.check_image_name(image)
        out: dict = {"image": image, "local": False, "local_details": None, "remote": None, "remote_error": ""}
        proc = self._run(["image", "inspect", "--", image], check=False, timeout=QUICK_TIMEOUT_SECONDS)
        if proc.returncode == 0:
            details = parse_local_inspect(proc.stdout)
            if details is not None:
                out["local"], out["local_details"] = True, details
        else:
            err = proc.stderr or proc.stdout or ""
            kind = docker_errors.classify(err)
            if kind:  # docker itself is unusable: say so instead of "not local"
                raise docker_errors.friendly("inspect", err)
        last = ""
        proc = self._run(["buildx", "imagetools", "inspect", image], check=False, timeout=QUICK_TIMEOUT_SECONDS * 2)
        if proc.returncode == 0:
            r = parse_imagetools(proc.stdout)
            if r["digest"] or r["platforms"]:
                out["remote"] = r
        else:
            last = proc.stderr or proc.stdout or ""
        if out["remote"] is None:
            proc = self._run(["manifest", "inspect", image], check=False, timeout=QUICK_TIMEOUT_SECONDS * 2)
            if proc.returncode == 0:
                out["remote"] = parse_manifest(proc.stdout)
            else:
                last = proc.stderr or proc.stdout or last
            if out["remote"] is None:
                first = (last.strip().splitlines() or ["no answer"])[0][:200]
                out["remote_error"] = (f"The registry could not be asked ({first}). Is the machine online and is the "
                                       "image name right?")
                out["remote_error_kind"] = docker_errors.classify_registry_error(last)
                out["failure"] = docker_errors.registry_failure(out["remote_error_kind"], image, last)
        return out

    def local_image_digests(self, ref: str) -> tuple[str, ...]:
        self.check_image_name(ref)
        proc = self._run(["image", "inspect", "--format", "{{json .RepoDigests}}", "--", ref], check=False,
                         timeout=QUICK_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            return ()
        try:
            items = json.loads(proc.stdout.strip() or "[]") or []
        except ValueError:
            return ()
        return tuple(sorted({s.split("@", 1)[1] for s in items if isinstance(s, str) and "@sha256:" in s}))

    def exec_in(self, name: str, argv: list[str], timeout: int = 60) -> str:
        found = self._ours(name)
        if found is None or found.raw != "running":
            raise BackendError(f"The container '{name}' is not running, so a command cannot be run in it.")
        if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
            raise BackendError("Invalid command.")
        proc = self._run(["exec", name, *argv], check=False, timeout=timeout, merge_stderr=True)
        if proc.returncode != 0:
            tail = " ".join((proc.stdout or "").split())[-300:]
            raise BackendError(f"The command exited with status {proc.returncode}: {tail or 'no output'}")
        return proc.stdout or ""

    def get_container_image_id(self, name: str) -> str | None:
        proc = self._run(["container", "inspect", "--format", "{{.Image}}", name], check=False,
                         timeout=QUICK_TIMEOUT_SECONDS)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def container_details(self, name: str) -> dict | None:
        """Read-only facts about one of our containers for troubleshooting (state, exit code, out-of-memory flag,
        restart count, published ports, mounts). None if it does not exist. Never reads its environment."""
        proc = self._run(["container", "inspect", name], check=False, timeout=QUICK_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            err = proc.stderr or proc.stdout or ""
            if "no such" in err.lower() and not docker_errors.classify(err):
                return None
            raise docker_errors.friendly("inspect", err)
        try:
            doc = json.loads(proc.stdout)
            labels = ((doc[0] if isinstance(doc, list) else doc).get("Config") or {}).get("Labels") or {}
        except (ValueError, AttributeError, IndexError, TypeError):
            labels = {}
        if str(labels.get(LABEL_MANAGED, "")).lower() != "true":   # only ever describe containers GameDock made
            raise ForeignContainerError(foreign_message(name))
        return parse_container_details(proc.stdout)

    def get_stats(self, names: list[str]) -> dict[str, ContainerStats]:
        """One `docker container inspect` for every name (state, start time, memory limit) and, only if
        something is running, one `docker stats --no-stream` for the running ones. Two engine calls at most,
        however many servers there are."""
        if not names:
            return {}
        # inspect exits non-zero if some name does not exist but still prints the others
        proc = self._run(["container", "inspect", "--format", INSPECT_FORMAT, *names], check=False,
                         timeout=STATS_TIMEOUT_SECONDS)
        info = parse_inspect_lines(proc.stdout)
        if not info and proc.returncode != 0 and "no such" not in (proc.stderr or "").lower():
            raise docker_errors.friendly("inspect", proc.stderr or "")
        running = [n for n, (status, _, _) in info.items() if status in ("running", "restarting")]
        live: dict[str, ContainerStats] = {}
        if running:
            sp = self._run(["stats", "--no-stream", "--format", "{{json .}}", *running],
                           timeout=STATS_TIMEOUT_SECONDS)
            live = parse_stats_lines(sp.stdout)
        result: dict[str, ContainerStats] = {}
        for name, (status, started, limit) in info.items():
            if name in running:
                s = live.get(name, ContainerStats(running=True))
                result[name] = replace(s, started_at=started, mem_limit=limit or s.mem_limit)
            else:
                result[name] = ContainerStats(running=False, mem_limit=limit)
        return result

    # ------------------------------------------------------------------ health
    def diagnose(self) -> DockerDiagnosis:
        """Is Docker installed, is the daemon reachable, may this user use it? Never raises."""
        path = shutil.which(self.docker_bin) if self._runner is subprocess.run else self.docker_bin
        if path is None:
            return DockerDiagnosis(False, None, None, False, None, "not_installed", docker_errors.NOT_INSTALLED, "")
        client = None
        try:
            cp = self._run(["--version"], check=False, timeout=QUICK_TIMEOUT_SECONDS)
            client = cp.stdout.strip() or None if cp.returncode == 0 else None
            sp = self._run(["version", "--format", "{{.Server.Version}}"], check=False, timeout=QUICK_TIMEOUT_SECONDS)
        except DockerUnavailableError as e:
            return DockerDiagnosis(e.kind != "not_installed", path, client, False, None, e.kind, str(e), "")
        except BackendError as e:
            return DockerDiagnosis(True, path, client, False, None, "error", str(e), "")
        if sp.returncode != 0:
            err = docker_errors.friendly("version", sp.stderr or sp.stdout)
            kind = getattr(err, "kind", "error")
            return DockerDiagnosis(True, path, client, False, None, kind, str(err), "")
        return DockerDiagnosis(True, path, client, True, sp.stdout.strip() or None, "ok", "Docker is running.", "")
