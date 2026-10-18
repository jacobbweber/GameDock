"""`python -m gamedock doctor`: check that this machine is ready to run GameDock, in plain language.

Every check returns a status and, when something is wrong, a concrete fix. The same report is served at
/api/doctor and shown on the Health page. Nothing here changes anything except a short-lived test file in
the data folder.
"""
from __future__ import annotations

import getpass
import os
import platform
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .backends import docker_errors
from .config import LOCAL_HOSTS, Settings
from .errors import GameDockError
from .hostcheck import foreign_owners, free_space, port_free
from .models import Status
from .service import GameDockService

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
GIB = 1024 ** 3


@dataclass
class Check:
    id: str
    title: str
    status: str  # ok | warn | fail | info
    message: str
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, id: str, title: str, status: str, message: str, fix: str = "") -> None:
        self.checks.append(Check(id, title, status, message, fix))

    @property
    def counts(self) -> dict[str, int]:
        return {s: sum(1 for c in self.checks if c.status == s) for s in (OK, WARN, FAIL, INFO)}

    @property
    def ok(self) -> bool:
        return self.counts[FAIL] == 0

    def summary(self) -> str:
        n = self.counts
        if n[FAIL]:
            return f"{n[FAIL]} problem(s) must be fixed before GameDock can manage servers, {n[WARN]} warning(s)."
        if n[WARN]:
            return f"GameDock can run. {n[WARN]} warning(s) worth a look."
        return "Everything looks good."

    def to_dict(self) -> dict:
        return {"ok": self.ok, "summary": self.summary(), "counts": self.counts,
                "checks": [c.__dict__ for c in self.checks]}

    def to_text(self, color: bool = False) -> str:
        tag = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}
        paint = {OK: "32", WARN: "33", FAIL: "31", INFO: "36"}
        lines = []
        for c in self.checks:
            t = tag[c.status]
            if color:
                t = f"\033[{paint[c.status]}m{t}\033[0m"
            lines.append(f"{t} {c.title}: {c.message}")
            if c.fix and c.status in (WARN, FAIL):
                lines.append(f"       How to fix: {c.fix}")
        lines += ["", self.summary()]
        return "\n".join(lines)


def _fmt_bytes(n: float) -> str:
    return f"{n / GIB:.1f} GB" if n >= GIB else f"{n / 1024 ** 2:.0f} MB"


# ----------------------------------------------------------------------------- the checks
def check_platform(r: DoctorReport) -> None:
    py = sys.version.split()[0]
    system = platform.system()
    if sys.version_info < (3, 10):
        r.add("python", "Python", FAIL, f"Python {py} is too old; GameDock needs 3.10 or newer.",
              "Install a newer Python and recreate the virtual environment.")
    elif system != "Linux":
        r.add("python", "Python and system", WARN,
              f"Python {py} on {system}. GameDock is built and tested for Linux hosts; on {system} things like file "
              "owners and the Docker socket behave differently.",
              "Run GameDock on the Linux machine that runs Docker (use the browser from here).")
    else:
        who = getpass.getuser() if _has_user() else "?"
        r.add("python", "Python and system", OK, f"Python {py} on Linux, running as user '{who}'"
              + (f" (id {os.getuid()})" if hasattr(os, "getuid") else "") + ".")
    if os.path.exists("/.dockerenv"):
        r.add("in_container", "Running inside a container", INFO,
              "GameDock itself is running in a container. Game servers are started by the host's Docker engine, so the "
              "data folder must have the same path on the host and in this container "
              f"(now: {os.environ.get('GAMEDOCK_DATA_DIR', './data')}).",
              "Mount the data folder at the identical path (see docker-compose.yml), not at a different one.")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        r.add("root", "Running as root", WARN, "GameDock is running as root. That works, but any bug or leaked "
              "token then has full control of this machine.", "Run it as a normal user that is in the docker group.")


def _has_user() -> bool:
    try:
        getpass.getuser()
        return True
    except Exception:
        return False


def check_docker(r: DoctorReport, service: GameDockService, settings: Settings | None) -> bool:
    """Returns True when the container engine can be used."""
    if settings is not None and settings.backend == "fake":
        r.add("docker", "Container engine", INFO, "Using the FAKE in-memory engine (GAMEDOCK_BACKEND=fake): "
              "nothing real is started. Fine for trying the UI, not for hosting games.")
        return True
    d = service.docker_status(max_age=0)
    if d.reachable:
        r.add("docker_binary", "Docker command", OK, f"Found {d.path or 'docker'}"
              + (f" ({d.client_version})" if d.client_version else "") + ".")
        r.add("docker_daemon", "Docker service", OK, f"Docker is running (engine version {d.server_version}).")
        r.add("docker_access", "Permission to use Docker", OK, "This user can talk to Docker.")
        return True
    problem, fix = docker_errors.SHORT.get(d.kind, docker_errors.SHORT["error"])
    if d.kind == "not_installed":
        r.add("docker_binary", "Docker command", FAIL, problem, fix)
        r.add("docker_daemon", "Docker service", INFO, "Cannot tell until Docker is installed.")
        r.add("docker_access", "Permission to use Docker", INFO, "Cannot tell until Docker is installed.")
        return False
    r.add("docker_binary", "Docker command", OK, f"Found {d.path or 'docker'}."
          + (f" ({d.client_version})" if d.client_version else ""))
    if d.kind == "permission":
        r.add("docker_daemon", "Docker service", INFO, "Cannot tell yet (no permission to ask).")
        r.add("docker_access", "Permission to use Docker", FAIL, problem, fix)
    else:
        r.add("docker_daemon", "Docker service", FAIL, problem + (f" ({d.message})" if d.kind == "error" else ""), fix)
        r.add("docker_access", "Permission to use Docker", INFO, "Cannot tell until Docker is running.")
    return False


def check_data_folder(r: DoctorReport, service: GameDockService) -> None:
    data = service.data_dir
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        r.add("data_dir", "Data folder", FAIL, f"{data} cannot be created: {e.strerror or e}.",
              f"Create it yourself and give this user write access, or set GAMEDOCK_DATA_DIR elsewhere.")
        return
    try:
        with tempfile.NamedTemporaryFile(dir=data, prefix=".doctor-", delete=True):
            pass
    except OSError as e:
        r.add("data_dir", "Data folder", FAIL, f"{data} is not writable: {e.strerror or e}.",
              f"sudo chown -R $(id -u):$(id -g) '{data}'")
        return
    free, total = free_space(data)
    if free < GIB // 2:
        r.add("data_dir", "Data folder", FAIL, f"{data} is writable but only {_fmt_bytes(free)} are free.",
              "Free up disk space or point GAMEDOCK_DATA_DIR at a bigger disk. Game servers and backups need room.")
    elif free < 5 * GIB:
        r.add("data_dir", "Data folder", WARN, f"{data} is writable, {_fmt_bytes(free)} free of {_fmt_bytes(total)}. "
              "That is tight for game saves plus backups.", "Free up space or move GAMEDOCK_DATA_DIR to a bigger disk.")
    else:
        r.add("data_dir", "Data folder", OK, f"{data} is writable, {_fmt_bytes(free)} free of {_fmt_bytes(total)}.")


def check_state_file(r: DoctorReport, service: GameDockService) -> None:
    path = service.store.path
    try:
        n = len(service.store.all())
    except GameDockError as e:
        r.add("state", "List of deployed servers", FAIL, str(e), "Follow the recovery steps in the message above.")
        return
    if path.exists() and hasattr(os, "getuid") and path.stat().st_mode & 0o077:
        r.add("state", "List of deployed servers", WARN,
              f"{n} server(s) in {path}, but the file can be read by other users and it holds passwords.",
              f"chmod 600 '{path}'")
        return
    r.add("state", "List of deployed servers", OK, f"{n} server(s) known"
          + ("." if path.exists() else " (no file yet: nothing deployed)."))


def check_ownership(r: DoctorReport, service: GameDockService) -> None:
    if not hasattr(os, "getuid"):
        return
    owners, example = foreign_owners(service.data_dir)
    if owners:
        ids = ", ".join(str(u) for u in sorted(owners))
        r.add("ownership", "File ownership in the data folder", WARN,
              f"Some files belong to other user ids ({ids}), for example {example}. Backups and config editing "
              "cannot read or change those files.",
              f"sudo chown -R $(id -u):$(id -g) '{service.data_dir}'. To stop it recurring, run the game container "
              "as your user (many images take PUID and PGID settings).")
    else:
        r.add("ownership", "File ownership in the data folder", OK, "All files belong to this user.")


def check_catalog(r: DoctorReport, service: GameDockService) -> None:
    d = service.catalog_dir
    if not d.is_dir():
        r.add("catalog", "Game catalog", FAIL, f"The catalog folder {d} does not exist.",
              "Run GameDock from the project folder, or set GAMEDOCK_CATALOG_DIR.")
        return
    try:
        entries = service.list_catalog()
    except GameDockError as e:
        r.add("catalog", "Game catalog", FAIL, str(e))
        return
    for fname, problem in service.catalog_problems:
        r.add(f"catalog_{fname}", f"Catalog file {fname}", WARN, problem + " This game is skipped.",
              f"Edit {d / fname} (see 'Adding a game' in the README) and reload the page.")
    if not entries:
        r.add("catalog", "Game catalog", FAIL, f"No valid games found in {d}.",
              "Copy the example *.yaml files into that folder.")
    else:
        r.add("catalog", "Game catalog", OK, f"{len(entries)} game(s) available: "
              + ", ".join(e.id for e in entries[:8]) + ("..." if len(entries) > 8 else "") + ".")


def check_ports(r: DoctorReport, service: GameDockService, docker_ok: bool) -> None:
    try:
        instances = service.store.all()
    except GameDockError:
        r.add("ports", "Host ports of deployed servers", INFO,
              "Skipped: the list of servers cannot be read (see the check above).")
        return
    if not instances:
        r.add("ports", "Host ports of deployed servers", INFO, "No servers deployed yet, nothing to check.")
        return
    problems = 0
    for inst in instances:
        try:
            running = service.get_status(inst.id).status == Status.RUNNING
        except GameDockError:
            running = False
        if running:
            continue  # its own ports are of course busy
        for p in inst.ports:
            if port_free(p.protocol, p.host) is False:
                problems += 1
                r.add(f"port_{inst.id}_{p.protocol}_{p.host}", f"Port {p.host}/{p.protocol} for '{inst.name}'", FAIL,
                      "Already in use by another program or container, so this server cannot start.",
                      f"Find the owner: sudo ss -lntup | grep :{p.host}   Then Edit the server to use another port.")
    if not problems:
        r.add("ports", "Host ports of deployed servers", OK,
              f"All host ports of the {len(instances)} deployed server(s) that are not running are free.")


def check_web_and_mcp(r: DoctorReport, settings: Settings) -> None:
    if settings.host in LOCAL_HOSTS:
        r.add("bind", "Web address", OK, f"Listening on {settings.host}:{settings.port} only (this machine). "
              "From another computer use an SSH tunnel.")
    else:
        r.add("bind", "Web address", WARN,
              f"Listening on {settings.host}:{settings.port}: anyone who can reach that port can start, stop and "
              "delete servers (there is no login).",
              "Use GAMEDOCK_HOST=127.0.0.1 and an SSH tunnel, or put a reverse proxy with a password in front.")
    mode, reason = settings.effective_mcp_mode()
    token = bool(settings.mcp_token)
    if mode == "off" and reason:
        r.add("mcp", "MCP (AI agent) endpoint", INFO, reason)
    elif mode == "off":
        r.add("mcp", "MCP (AI agent) endpoint", INFO, "Switched off (GAMEDOCK_MCP_MODE=off).")
    elif mode == "full" and not token:
        r.add("mcp", "MCP (AI agent) endpoint", WARN,
              "Mode 'full' (agents may delete servers and data, behind a two-step confirmation) with no token: any "
              "program on this machine can use it.",
              "Set GAMEDOCK_MCP_TOKEN to a long random value: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    else:
        r.add("mcp", "MCP (AI agent) endpoint", OK,
              f"Mode '{mode}', bearer token {'set' if token else 'not set (local use only)'}. URL path: /mcp.")


def _guarded(r: DoctorReport, id: str, title: str, fn, *args) -> object:
    """One broken check must never stop the others (a doctor that crashes is no use)."""
    try:
        return fn(r, *args)
    except Exception as e:  # noqa: BLE001
        r.add(id, title, FAIL if isinstance(e, GameDockError) else WARN,
              f"Could not run this check: {e}", "Fix the problem above, then run the doctor again.")
        return None


def run_doctor(service: GameDockService, settings: Settings | None = None) -> DoctorReport:
    r = DoctorReport()
    _guarded(r, "python", "Python and system", check_platform)
    docker_ok = _guarded(r, "docker", "Container engine", check_docker, service, settings)
    _guarded(r, "data_dir", "Data folder", check_data_folder, service)
    _guarded(r, "state", "List of deployed servers", check_state_file, service)
    _guarded(r, "ownership", "File ownership", check_ownership, service)
    _guarded(r, "catalog", "Game catalog", check_catalog, service)
    _guarded(r, "ports", "Host ports", check_ports, service, bool(docker_ok))
    if settings is not None:
        _guarded(r, "bind", "Web address and MCP", check_web_and_mcp, settings)
    return r
