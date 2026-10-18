"""Troubleshooting and pre-flight service methods (mixed into GameDockService): port checks, the deploy preview, the setup
check (verify_game_setup) and the diagnostics bundle. Everything here is READ-ONLY: nothing is created, started, written
or changed. (Files and folders are only looked at.)"""
from __future__ import annotations

import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import checks as ck
from . import configfiles as cfg
from . import configforms as forms
from . import portcheck as pc
from . import readiness as rd
from . import steam as stm
from .backends.docker_cli import DockerCliBackend
from .catalog import entry_to_dict
from .errors import BackendError, ConflictError, GameDockError, NotFoundError, ValidationError
from .hostcheck import foreign_owners, free_space
from .models import CatalogEntry, ContainerSpec, Instance, PortMapping, Status, VolumeMapping

LOW_DISK_WARN = 2 * 1024**3
LOW_DISK_FAIL = 500 * 1024**2
READY_OVERDUE = 20 * 60          # seconds a started container may take before "ready line not seen" is a problem
RESTART_WARN = 3
LOG_SCAN_LINES = 1500
DIAG_MAX_LOG_LINES = 200
# Snapshot keys that only describe the game, not the container; a difference there needs no redeploy.
_DRIFT_SKIP = {"id", "name", "description", "notes", "update_note"}
_SNAPSHOT_OPTIONAL = {"update_note", "backup_stop_first", "save_command", "steam_app_id", "steam_branch", "steam_manifest_dir",
                      "version_log_pattern", "game_update_method", "installed_build_file", "ready_log_pattern",
                      "progress_log_pattern"}


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{seconds} seconds"


def _size(n: int) -> str:
    f = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} bytes" if unit == "bytes" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} bytes"


class OpsMixin:
    """Uses from GameDockService: backend, store, data_dir, _catalog, _instance, _template, _spec, _with_current_hints,
    _with_current_patterns, _resolve_ports, _merge_env, _check_name, _unique_id, _port_probe, _ready_seen, _clock, steam,
    _read_game_installed, _data_dir_of, _redact, list_backups, get_data_summary, get_logs, _info, _schedule_overview."""

    # ------------------------------------------------------------------ snapshot drift
    def template_drift(self, inst: Instance, current: CatalogEntry) -> list[str]:
        """Which parts of the catalog entry changed since this server was deployed (it keeps a snapshot)."""
        try:
            snap = entry_to_dict(self._template(inst))
        except GameDockError:
            return ["(the stored snapshot cannot be read)"]
        now = entry_to_dict(current)
        out = []
        for k, v in now.items():
            if k in _DRIFT_SKIP or (k in _SNAPSHOT_OPTIONAL and k not in inst.template):
                continue
            if snap.get(k) != v:
                out.append(k)
        return sorted(out)

    # ------------------------------------------------------------------ ports
    def free_port_hint(self, protocol: str, host: int, ignore_id: str | None = None) -> str:
        """" For example 8212." (with a leading space) for use in an error message, or "" if none was found."""
        try:
            used = {(p.protocol, p.host) for i in self.store.all() if i.id != ignore_id for p in i.ports}
            probe = self._port_probe
            alt = pc.suggest_free(protocol, host, lambda pr, po: (pr, po) in used or bool(probe and probe(pr, po) is False))
        except Exception:  # noqa: BLE001 - a hint must never hide the real error
            return ""
        return f" For example {alt} is free." if alt else ""

    def _server_port_sets(self, ignore_id: str | None):
        servers = []
        for other in self.store.all():
            if other.id == ignore_id:
                continue
            try:
                running = self.backend.get_state(f"gamedock-{other.id}").status == Status.RUNNING
            except BackendError:
                running = False
            servers.append((other.id, other.name, {(p.protocol, p.host) for p in other.ports}, running))
        return servers

    def check_ports(self, ports: Iterable[PortMapping | tuple[str, int]], game_id: str | None = None,
                    ignore_server_id: str | None = None) -> dict:
        """Would these host ports collide with another GameDock server, a program listening on this machine, or another
        catalog game's default? Read-only. `game_id`: the catalog game the ports are for (its own defaults are not a
        clash); `ignore_server_id`: a server being edited (its own ports are not a clash)."""
        wanted = []
        for p in ports:
            proto, host = (p.protocol, p.host) if isinstance(p, PortMapping) else (p[0], p[1])
            if proto not in ("tcp", "udp"):
                raise ValidationError(f"Protocol '{proto}' is not tcp or udp.")
            wanted.append(pc.Wanted(proto, host))
        if not wanted:
            raise ValidationError("Give at least one port to check.")
        if len(wanted) > 64:
            raise ValidationError("Check at most 64 ports at a time.")
        catalog = []
        try:
            for e in self._catalog().values():
                if e.id != game_id:
                    catalog.append((e.id, e.name, {(p.protocol, p.host) for p in e.ports}))
        except GameDockError:
            catalog = []
        own: set[tuple[str, int]] = set()
        if ignore_server_id:
            inst = self._instance(ignore_server_id)
            if self.backend.get_state(f"gamedock-{inst.id}").status == Status.RUNNING:
                own = {(p.protocol, p.host) for p in inst.ports}
        report = pc.find_conflicts(wanted, servers=self._server_port_sets(ignore_server_id), catalog_defaults=catalog,
                                   probe=self._port_probe, own_running=own)
        out = report.to_dict(wanted)
        out["read_only"] = True
        return out

    # ------------------------------------------------------------------ deploy preview
    def preview_deploy(self, game_id: str, name: str, ports: Iterable[PortMapping] | None = None,
                       env: dict[str, str] | None = None) -> dict:
        """Dry run of deploy: what WOULD be created, and every problem that would stop it, without creating anything (no
        folders, no container, no stored server). Secret values are never shown, only whether they are set."""
        t = self.get_catalog_entry(game_id)              # unknown game: NotFoundError with the list of games
        problems: list[str] = []
        warnings: list[str] = []
        if not t.reviewed:
            problems.append(
                f"'{t.name}' was added by an AI agent and a person has not approved it yet, so it cannot be deployed. "
                "get_catalog_review_status says where it stands; a person approves it in the web page (Catalog -> Review).")

        def attempt(fn):
            try:
                return fn()
            except GameDockError as e:
                problems.append(str(e))
                return None

        clean_name = attempt(lambda: self._check_name(name)) or (name or "").strip()
        resolved = attempt(lambda: self._resolve_ports(t, ports)) or tuple(t.ports)
        values = attempt(lambda: self._merge_env(t, env)) or {f.name: f.default for f in t.fields}
        port_report = self.check_ports(resolved, game_id=game_id)
        for f in port_report["findings"]:
            if f["level"] == "conflict":
                alt = f" Try host port {f['suggested_host_port']}." if f["suggested_host_port"] else ""
                problems.append(f["message"] + alt)
            elif f["level"] == "warn":
                warnings.append(f["message"])
        for f in port_report["findings"]:
            if f["level"] == "info":
                warnings.append(f["message"])
        try:
            sid = self._unique_id(clean_name) if clean_name else ""
        except GameDockError as e:
            problems.append(str(e))
            sid = ""
        base = (Path(self.data_dir) / (sid or "<server id>")).resolve()
        volumes = []
        for v in t.volumes:
            host = Path(v.host)
            hp = host if host.is_absolute() else (base / host).resolve()
            volumes.append({"host_path": str(hp), "container": v.container, "exists": hp.exists(),
                            "note": "" if hp.exists() else "will be created when the server is deployed"})
        spec = ContainerSpec(name=f"gamedock-{sid or 'server'}", image=t.image, ports=tuple(resolved),
                             volumes=tuple(VolumeMapping(x["host_path"], x["container"]) for x in volumes),
                             env={**t.env, **values}, stop_timeout=t.stop_timeout,
                             labels={"gamedock.managed": "true", "gamedock.server_id": sid, "gamedock.game_id": t.id})
        try:
            argv = DockerCliBackend.build_create_args(spec, spec.stop_timeout or 60)
            command = "docker " + " ".join(shlex.quote(a) for a in argv)
        except BackendError as e:
            command = ""
            problems.append(str(e))
        shown_env = []
        secret_names = {f.name for f in t.fields if f.secret}
        for k, v in t.env.items():
            shown_env.append({"name": k, "source": "fixed by the catalog entry", "value": v})
        for f in t.fields:
            v = values.get(f.name, "")
            shown_env.append({"name": f.name, "source": "setting", "secret": f.secret, "required": f.required,
                              "value": ("(set, hidden)" if v else "(not set)") if f.secret else v})
        image_state = "unknown"
        try:
            image_state = "stored on this machine" if self.backend.get_image_id(t.image) else \
                "not stored here: it will be downloaded when the server is deployed (can be many GB and take minutes)"
        except BackendError:
            pass
        out = {
            "ok": not problems, "can_deploy": t.reviewed and not problems, "game_id": t.id, "game_name": t.name,
            "problems": problems, "warnings": warnings, "server_id": sid, "name": clean_name, "image": t.image,
            "image_state": image_state,
            "ports": [{"protocol": p.protocol, "host": p.host, "container": p.container} for p in resolved],
            "port_check": port_report, "volumes": volumes, "env": shown_env,
            "container_command": command,
            "secret_settings": sorted(secret_names),
            "reviewed": t.reviewed,
            "note": "Nothing was created. Values of secret settings are never shown; the real command passes each value "
                    "through the environment, not on the command line.",
            "read_only": True,
        }
        return out

    # ------------------------------------------------------------------ setup check
    def _scan_text(self, inst: Instance, lines: int = LOG_SCAN_LINES) -> str:
        try:
            raw = self.backend.get_logs(f"gamedock-{inst.id}", lines)
        except BackendError:
            return ""
        return self._redact(inst, rd.clean_log_text(raw))

    def verify_game_setup(self, server_id: str) -> dict:
        """Is this deployed server set up the way its catalog entry claims? Read-only checklist (pass / warn / fail) with
        concrete fixes for the catalog entry. See the individual checks below."""
        inst = self._instance(server_id)
        if server_id in self._pending:
            c = [ck.Check("deploying", "Deploy in progress", ck.WARN, "The server is still being deployed (the image may "
                          "be downloading). Run the check again when it is finished.")]
            return self._verify_result(inst, c, None)
        t = self._template(inst)
        hints = self._with_current_hints(inst, t)
        t_read = self._with_current_patterns(inst, hints)
        cname = f"gamedock-{inst.id}"
        checks: list[ck.Check] = []
        try:
            state = self.backend.get_state(cname)
        except BackendError as e:
            checks.append(ck.Check("container", "Container", ck.FAIL, f"Docker could not be asked: {e}",
                                   "Fix Docker first (see the Health page), then run the check again."))
            return self._verify_result(inst, checks, None)
        details = None
        try:
            details = self.backend.container_details(cname)
        except BackendError:
            details = None
        running = state.status == Status.RUNNING
        text = self._scan_text(inst) if running else ""
        ready_now = self._check_container(checks, inst, state, details)
        game_ready = self._check_ready(checks, inst, t_read, state, running, text)
        if hints.steam_app_id or hints.installed_build_file:
            self._check_steam(checks, inst, hints, running, text, ready_now and game_ready)
        self._check_config_files(checks, inst, t, running)
        self._check_ports_published(checks, inst, state, details)
        self._check_volumes(checks, inst, t, running, details)
        self._check_settings(checks, inst, t)
        self._check_catalog_snapshot(checks, inst)
        return self._verify_result(inst, checks, details)

    def _verify_result(self, inst: Instance, checks: list[ck.Check], details) -> dict:
        out = {"server_id": inst.id, "name": inst.name, "game_id": inst.game_id,
               "checked_at": self._now().isoformat(timespec="seconds"), "read_only": True,
               "checks": [c.to_dict() for c in checks]}
        out.update(ck.summarize(checks))
        out["how_to_read"] = ("pass = fine, warn = worth a look (often just 'not finished starting yet'), fail = a problem. "
                              "`suggestions` are concrete fixes for the catalog entry or the server.")
        return out

    # -- container + health + restarts
    def _check_container(self, checks, inst, state, details) -> bool:
        """Adds the container check; returns True when the container runs and has not been crash-looping."""
        if state.status == Status.NOT_CREATED:
            checks.append(ck.Check("container", "Container", ck.FAIL, "The server has no container yet.",
                                   "Start the server (it creates the container), or deploy it again."))
            return False
        if state.status == Status.ERROR:
            checks.append(ck.Check("container", "Container", ck.FAIL, state.detail or "The container is in an error state."))
            return False
        if state.status == Status.STOPPED:
            why = ""
            if details and details.get("exit_code"):
                why = (f" It ran out of memory (exit code {details['exit_code']})." if details.get("oom_killed") else
                       f" It last exited with code {details['exit_code']}: read the logs (get_server_logs).")
            fix = "Start the server, then run this check again: most checks need it running."
            if details and details.get("oom_killed"):
                fix = "The game ran out of memory: give the machine or the container more memory, or lower the player count."
            checks.append(ck.Check("container", "Container", ck.FAIL if why else ck.WARN,
                                   "The container exists but is stopped." + why, fix))
            return False
        n = (details or {}).get("restart_count", 0)
        if n >= RESTART_WARN:
            checks.append(ck.Check("container", "Container", ck.WARN,
                                   f"The container is running but Docker has restarted it {n} times, which usually means the "
                                   "game keeps crashing.", "Read get_server_logs for the error just before each restart."))
            return False
        checks.append(ck.Check("container", "Container", ck.PASS,
                               "The container is running" + (f" (restarted {n} time(s))." if n else ".")))
        return True

    # -- health check and ready line
    def _check_ready(self, checks, inst, t, state, running, text) -> bool:
        """Adds the ready check; returns True when the game counts as ready (ready line seen, or no pattern to wait for)."""
        if not running:
            checks.append(ck.Check("ready", "Game is ready to join", ck.SKIP, "The container is not running."))
            return False
        if state.health:
            st = {"healthy": ck.PASS, "starting": ck.WARN, "unhealthy": ck.FAIL}.get(state.health, ck.WARN)
            checks.append(ck.Check("health", "Docker health check", st, f"The image's health check says: {state.health}.",
                                   "" if st == ck.PASS else "Read get_server_logs: the health check fails while the game is "
                                   "not working or not finished starting."))
        else:
            checks.append(ck.Check("health", "Docker health check", ck.SKIP,
                                   "This image has no health check (that is fine: GameDock uses the ready log line)."))
        pattern = t.ready_log_pattern
        started = ck.parse_started(state.started_at)
        up_for = (self._now() - started).total_seconds() if started else None
        if not pattern:
            cands = ck.ready_line_candidates(text)
            fix = ("Set ready_log_pattern in the catalog entry to the log line the game prints when players can join."
                   + (f" Lines in the log that may be it: {' | '.join(cands)}" if cands else ""))
            checks.append(ck.Check("ready", "Game is ready to join", ck.WARN if not state.health else ck.PASS,
                                   "The catalog entry has no ready_log_pattern, so GameDock says Ready as soon as the "
                                   "container runs." if not state.health else "Ready is decided by the health check.", fix
                                   if not state.health else ""))
            return True
        try:
            seen = rd.pattern_seen(text, pattern) or self._ready_seen.get(inst.id) == state.started_at
        except Exception:  # noqa: BLE001
            seen = False
        if seen:
            checks.append(ck.Check("ready", "Game is ready to join", ck.PASS,
                                   f"The ready line (pattern {pattern!r}) was found in the log."))
            return True
        prog = rd.last_progress(text, t.progress_log_pattern)
        if prog is not None:
            checks.append(ck.Check("ready", "Game is ready to join", ck.WARN,
                                   f"Still installing ({prog:g}%). The ready line (pattern {pattern!r}) has not appeared yet.",
                                   "Wait for the download to finish, then run the check again."))
            return False
        cands = ck.ready_line_candidates(text)
        overdue = up_for is not None and up_for > READY_OVERDUE
        detail = (f"The ready line (pattern {pattern!r}) was not found in the last {LOG_SCAN_LINES} log lines"
                  + (f", although the server has been running for {_ago(up_for)}." if overdue else
                     (f" (the server started {_ago(up_for)} ago)." if up_for is not None else ".")))
        fix = ("Compare the pattern with the real log (get_server_logs). " +
               (f"Lines that may be the ready line: {' | '.join(cands)}. " if cands else "") +
               "Fix ready_log_pattern in the catalog entry if the game's wording differs.") if overdue else \
            "If it stays like this after the game has finished starting, fix ready_log_pattern in the catalog entry."
        checks.append(ck.Check("ready", "Game is ready to join", ck.FAIL if overdue else ck.WARN, detail, fix))
        return False

    # -- Steam tracking
    def _check_steam(self, checks, inst, t, running, text, ready) -> None:
        app = t.steam_app_id
        base = self._data_dir_of(inst)
        roots = [Path(v.host) for v in self._spec(inst).volumes]
        if t.installed_build_file:
            try:
                target = cfg.safe_path(base, t.installed_build_file)
            except ValidationError as e:
                checks.append(ck.Check("version", "Installed game version", ck.FAIL,
                                       f"installed_build_file {t.installed_build_file!r} is not allowed ({e}).",
                                       "Use a relative path inside the server's data folder."))
                target = None
            if target is not None:
                vf = stm.read_version_file(target)
                if vf is not None:
                    checks.append(ck.Check("version", "Installed game version", ck.PASS,
                                           f"Read version {vf.build!r} from {t.installed_build_file}."
                                           + ("" if vf.comparable else " (Not a Steam build number, so it cannot be compared "
                                                                       "with Steam's newest build.)")))
                    return
                if not app:
                    checks.append(ck.Check("version", "Installed game version", ck.WARN if running else ck.SKIP,
                                           f"{t.installed_build_file} was not found or is not a short version text yet.",
                                           "Check the path (find_by_name suggestions appear under the config check), or wait "
                                           "for the first start to write it."))
                    return
        found = None
        installed, problem = self._read_game_installed(inst, t, fresh=True)
        manifests = stm.list_manifests(roots)
        if installed is not None:
            rel = self._rel(base, installed.path)
            checks.append(ck.Check("version", "Installed game version", ck.PASS,
                                   f"Steam record appmanifest_{app}.acf found ({rel}): installed build {installed.build}"
                                   + (f" ({installed.name})" if installed.name else "") + "."))
            self._check_latest(checks, t, app)
            self._check_version_line(checks, t, running, text)
            return
        others = [m for m in manifests if m["app_id"] != app]
        if others:
            descr = ", ".join(f"appmanifest_{m['app_id']}.acf ({m['name'] or 'no name'}, build {m['build'] or '?'}) in "
                              f"{self._rel(base, os.path.dirname(m['path']))}" for m in others[:4])
            if len(others) == 1:
                fix = (f"appmanifest_{others[0]['app_id']}.acf was found with id {others[0]['app_id']}, not your steam_app_id "
                       f"{app}. If that is the dedicated server's id, change steam_app_id to {others[0]['app_id']} in the "
                       "catalog entry.")
            else:
                fix = (f"Several Steam records exist ({', '.join(m['app_id'] for m in others[:6])}), none is {app}. Set "
                       "steam_app_id to the dedicated server's id (the one that is the game server, not a tool).")
            checks.append(ck.Check("version", "Installed game version", ck.FAIL,
                                   f"No Steam record for steam_app_id {app}, but other records exist: {descr}.", fix))
            return
        if running and ready:
            checks.append(ck.Check(
                "version", "Installed game version", ck.FAIL,
                f"The server is ready, but no Steam record (appmanifest_{app}.acf) exists in any of its mounted folders "
                f"({', '.join(v.host for v in self._template(inst).volumes) or 'no folders are mounted'}). "
                + "Version tracking will say unknown.",
                "The image probably installs the game outside the mounted folders: mount the game's install folder "
                "(inspect_image shows the image's volumes), or set steam_manifest_dir / installed_build_file, or remove "
                "steam_app_id if the image does not use SteamCMD."))
        else:
            checks.append(ck.Check("version", "Installed game version", ck.WARN if running else ck.SKIP,
                                   problem or f"No Steam record (appmanifest_{app}.acf) yet.",
                                   "The first download may still be running: run the check again once the server is ready."
                                   if running else "Start the server and run the check again after the first download."))

    def _check_latest(self, checks, t, app) -> None:
        latest = self.steam.cached(app, t.steam_branch or "public")
        if latest is not None and latest.ok:
            checks.append(ck.Check("steam_latest", "Newest Steam build known", ck.PASS,
                                   f"Newest public build {latest.build} (App {app}); use check_game_update for the comparison."))
        elif not self.steam.enabled:
            checks.append(ck.Check("steam_latest", "Newest Steam build known", ck.SKIP,
                                   "Asking Steam is switched off (GAMEDOCK_UPDATE_CHECK=false)."))
        else:
            checks.append(ck.Check("steam_latest", "Newest Steam build known", ck.WARN,
                                   (latest.error if latest and latest.error else "The newest build has not been looked up yet."),
                                   f"Run check_game_update. If Steam does not know app {app}, steam_app_id is wrong."))

    def _check_version_line(self, checks, t, running, text) -> None:
        if not t.version_log_pattern or not running:
            return
        v = stm.extract_version(text, t.version_log_pattern)
        checks.append(ck.Check("version_line", "Game version line in the log", ck.PASS if v else ck.WARN,
                               f"Found the game version {v!r}." if v else
                               f"version_log_pattern {t.version_log_pattern!r} matched nothing in the log.",
                               "" if v else "Compare version_log_pattern with the real log lines (get_server_logs)."))

    @staticmethod
    def _rel(base: Path, p: str) -> str:
        try:
            return os.path.relpath(p, base)
        except ValueError:
            return p

    # -- config files
    def _check_config_files(self, checks, inst, t, running) -> None:
        base = self._data_dir_of(inst)
        for c in t.config_files:
            cid, title = f"config:{c.path}", f"Config file {c.path}"
            try:
                p = cfg.safe_path(base, c.path)
            except ValidationError as e:
                checks.append(ck.Check(cid, title, ck.FAIL, f"The path is not allowed: {e}"))
                continue
            if not p.is_file():
                similar = ck.find_by_name(base, Path(c.path).name) if base.is_dir() else []
                similar = [s for s in similar if s != c.path]
                fix = (f"A file with that name exists at {', '.join(similar)}: set this config file's path to it in the "
                       "catalog entry." if similar else
                       "Check the path in the catalog entry against the game's real settings file, or wait until the game "
                       "has finished its first start (many games create it then).")
                st = ck.FAIL if (similar or (running and not self._first_start(inst))) else ck.WARN
                checks.append(ck.Check(cid, title, st, "The file does not exist in the server's data folder.", fix))
                continue
            try:
                text = cfg._read_text(p)
            except GameDockError as e:
                checks.append(ck.Check(cid, title, ck.FAIL, str(e),
                                       "If the file is owned by another user, fix the owner (see the ownership note in "
                                       "the Backups dialog)."))
                continue
            except OSError as e:
                checks.append(ck.Check(cid, title, ck.FAIL, f"The file cannot be read ({e.strerror or e})."))
                continue
            form = None
            try:
                form = forms.build_form(text, c)
            except Exception:  # noqa: BLE001 - only informational
                form = None
            st, detail, fix = ck.check_config_text(c.format, text, form.kind if form else None, len(form.fields) if form else 0)
            checks.append(ck.Check(cid, title, st, detail, fix))

    def _first_start(self, inst: Instance) -> bool:
        """True while the server is young (the game may not have written its files yet)."""
        try:
            created = datetime.fromisoformat(inst.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return (self._now() - created).total_seconds() < 15 * 60
        except (ValueError, TypeError):
            return False

    # -- ports
    def _check_ports_published(self, checks, inst, state, details) -> None:
        running = state.status == Status.RUNNING
        published = {(p["protocol"], p["container"]): p for p in (details or {}).get("ports", [])}
        for p in inst.ports:
            cid, title = f"port:{p.protocol}:{p.host}", f"Port {p.host}/{p.protocol}"
            bound = self._port_probe(p.protocol, p.host) if self._port_probe is not None else None
            if not running:
                if bound is False:
                    checks.append(ck.Check(cid, title, ck.FAIL, "Another program on this machine already uses this host port, "
                                           "so the server cannot start.", f"Edit the server to use another host port, or stop "
                                           f"what uses it (sudo ss -lntup | grep :{p.host})."))
                else:
                    checks.append(ck.Check(cid, title, ck.SKIP, "The server is not running."))
                continue
            got = published.get((p.protocol, p.container))
            if details is not None and got is None:
                checks.append(ck.Check(cid, title, ck.FAIL, f"Docker has not published container port {p.container}/{p.protocol} "
                                       "on the host.", "Edit the server (or delete and deploy again) so the port is published; "
                                       "check that the catalog entry's container port is right."))
            elif got is not None and got["host_port"] != p.host:
                checks.append(ck.Check(cid, title, ck.WARN, f"Docker publishes it on host port {got['host_port']}, not {p.host}.",
                                       "The container was created with other ports: edit the server and save, or delete "
                                       "and deploy again."))
            elif bound is True:
                checks.append(ck.Check(cid, title, ck.WARN, "Docker lists the port as published, but nothing is bound to it on "
                                       "this machine's address.", "Check 'docker ps' for the container's port column and any "
                                       "firewall; some Docker setups publish differently."))
            else:
                checks.append(ck.Check(cid, title, ck.PASS,
                                       f"Published: host {p.host}/{p.protocol} -> container {p.container}"
                                       + (" and bound on this machine." if bound is False else ".")))

    # -- volumes
    def _check_volumes(self, checks, inst, t, running, details) -> None:
        spec = self._spec(inst)
        mounted = {m["target"] for m in (details or {}).get("mounts", [])}
        for v in spec.volumes:
            cid, title = f"volume:{v.container}", f"Folder {v.container}"
            p = Path(v.host)
            if not p.exists():
                checks.append(ck.Check(cid, title, ck.WARN, f"The host folder {v.host} does not exist yet.",
                                       "It is created when the server starts."))
                continue
            if not p.is_dir():
                checks.append(ck.Check(cid, title, ck.FAIL, f"{v.host} is not a folder."))
                continue
            problems = []
            if not os.access(p, os.R_OK | os.W_OK | os.X_OK):
                problems.append("GameDock cannot read and write it (check the owner)")
            try:
                n = sum(1 for _ in os.scandir(p))
            except OSError:
                n = -1
            if details is not None and running and v.container not in mounted:
                problems.append("the running container does not have it mounted (it was created before the catalog changed)")
            if running and n == 0 and not self._first_start(inst):
                problems.append("it is empty although the server runs: the game may save somewhere else in the container")
            if problems:
                checks.append(ck.Check(cid, title, ck.WARN, f"{v.host}: " + "; ".join(problems) + ".",
                                       "Check the container path of this volume against the image's documentation."))
            else:
                checks.append(ck.Check(cid, title, ck.PASS, f"{v.host} exists ({n} item(s) at the top level)."))
        # ownership and disk space (one look at the whole data folder)
        base = self._data_dir_of(inst)
        if base.is_dir():
            owners, example = foreign_owners(base, cap=3000)
            if owners:
                checks.append(ck.Check("owner", "File ownership", ck.WARN,
                                       f"Some files belong to another user (for example {self._rel(base, example)}), so "
                                       "backups and the config editor may fail.",
                                       "Fix with: sudo chown -R $(id -u):$(id -g) <data folder>, and make the game run as "
                                       "that user (PUID/PGID if the image has them)."))
        try:
            free, total = free_space(Path(self.data_dir))
            st = ck.FAIL if free < LOW_DISK_FAIL else ck.WARN if free < LOW_DISK_WARN else ck.PASS
            checks.append(ck.Check("disk", "Free disk space", st, f"{_size(free)} free of {_size(total)} where GameDock keeps data.",
                                   "Free some space: game downloads and backups need room." if st != ck.PASS else ""))
        except OSError:
            pass

    # -- required settings
    def _check_settings(self, checks, inst, t) -> None:
        missing = [f for f in t.fields if f.required and not inst.env.get(f.name, "").strip()]
        secrets = [(f.name, bool(inst.env.get(f.name))) for f in t.fields if f.secret]
        sec = "; ".join(f"{n}: {'set' if s else 'not set'}" for n, s in secrets)
        if missing:
            checks.append(ck.Check("settings", "Required settings", ck.FAIL,
                                   "Required settings with no value: " + ", ".join(f.name for f in missing) + ".",
                                   "Stop the server, edit it and fill them in."))
        else:
            checks.append(ck.Check("settings", "Required settings", ck.PASS,
                                   "Every required setting has a value." + (f" Secrets ({sec})." if sec else "")))

    # -- catalog snapshot vs the current entry
    def _check_catalog_snapshot(self, checks, inst) -> None:
        try:
            entry = self._catalog().get(inst.game_id)
        except GameDockError:
            entry = None
        if entry is None:
            checks.append(ck.Check("catalog", "Catalog entry", ck.WARN,
                                   f"The catalog no longer has an entry '{inst.game_id}'; this server keeps its stored copy."))
            return
        drift = self.template_drift(inst, entry)
        extra = "" if entry.reviewed else " The current entry is not approved yet."
        if drift:
            checks.append(ck.Check("catalog", "Catalog entry", ck.WARN,
                                   "The catalog entry changed after this server was deployed (" + ", ".join(drift) + "). A "
                                   "server keeps a snapshot of the entry, so it does not use the changes." + extra,
                                   "To test the changed entry: delete this test server (data can be kept) and deploy it "
                                   "again after the entry is approved."))
        else:
            checks.append(ck.Check("catalog", "Catalog entry", ck.PASS,
                                   "This server matches the current catalog entry." + extra))

    # ------------------------------------------------------------------ diagnostics
    def get_server_diagnostics(self, server_id: str, log_lines: int = 60) -> dict:
        """One bundle for troubleshooting (read-only): status, container facts, ports, disk use, backups, game update
        check, GameDock's version and the newest log lines. Secret setting values are never included and the log lines are
        redacted (known secrets, password=... shapes, tokens)."""
        if not 1 <= int(log_lines) <= DIAG_MAX_LOG_LINES:
            raise ValidationError(f"log_lines must be between 1 and {DIAG_MAX_LOG_LINES}")
        inst = self._instance(server_id)
        info = self.get_status(server_id)
        t = self._template(inst)
        hints = self._with_current_hints(inst, t)
        cname = f"gamedock-{inst.id}"
        notes: list[str] = []
        try:
            details = self.backend.container_details(cname)
        except BackendError as e:
            details = None
            notes.append(f"Container details unavailable: {e}")
        secret_values = tuple(inst.env.get(f.name, "") for f in t.fields if f.secret)
        try:
            logs = self.get_logs(server_id, int(log_lines))
            log_text = ck.redact_text(logs.text, secret_values)
            log_note = logs.note
        except GameDockError as e:
            log_text, log_note = "", str(e)
        try:
            ds = self.get_data_summary(server_id)
            data = {"path": ds.path, "files": ds.files, "bytes": ds.bytes, "size": _size(ds.bytes), "truncated": ds.truncated}
        except GameDockError as e:
            data = {"error": str(e)}
        try:
            free, total = free_space(Path(self.data_dir))
            disk = {"free_bytes": free, "total_bytes": total, "free": _size(free)}
        except OSError:
            disk = {}
        backups = self.list_backups(server_id)
        chk = getattr(self, "_image_checks", {}).get(inst.id)
        game_chk = self.steam.cached(hints.steam_app_id, hints.steam_branch or "public") if hints.steam_app_id else None
        version = self.updater.status()
        d = {
            "generated_at": self._now().isoformat(timespec="seconds"), "read_only": True,
            "gamedock": {"version": version["version"], "commit": version["commit_short"]},
            "server": {"id": inst.id, "name": inst.name, "game_id": inst.game_id, "game": t.name, "image": t.image,
                       "created_at": inst.created_at, "status": info.status.value, "phase": info.phase, "ready": info.ready,
                       "phase_detail": info.phase_detail, "detail": info.detail, "activity": info.activity},
            "container": ({"status": details["status"], "health": details["health"] or "none",
                           "exit_code": details["exit_code"], "out_of_memory": details["oom_killed"],
                           "restart_count": details["restart_count"], "started_at": details["started_at"],
                           "finished_at": details["finished_at"], "error": details["error"]} if details else None),
            "ports": {"configured": [{"protocol": p.protocol, "host": p.host, "container": p.container} for p in inst.ports],
                      "published": (details or {}).get("ports", []), "not_published": (details or {}).get("unpublished", [])},
            "settings": [{"name": f.name, "secret": f.secret, "required": f.required,
                          "value": ("set" if inst.env.get(f.name) else "not set") if f.secret else ck.redact_text(
                              f"{f.name}={inst.env.get(f.name, '')}", secret_values).partition("=")[2][:80]}
                         for f in t.fields],
            "data_folder": data, "disk": disk,
            "backups": {"count": len(backups), "newest": backups[0].created_at if backups else None,
                        "newest_name": backups[0].name if backups else None},
            "image_update_check": ({"checked_at": datetime.fromtimestamp(chk.checked_at, timezone.utc).isoformat(
                timespec="seconds"), "update_available": chk.available, "message": chk.message} if chk else None),
            "game_update_check": ({"steam_app_id": hints.steam_app_id, "installed_build": info.installed_build or None,
                                   "latest_build": info.latest_build or None, "status": info.game_update_status,
                                   "checked_at": datetime.fromtimestamp(game_chk.checked_at, timezone.utc).isoformat(
                                       timespec="seconds") if game_chk and game_chk.checked_at else None}
                                  if hints.steam_app_id else None),
            "schedule": info.schedule,
            "logs": {"lines": log_text.count("\n") + 1 if log_text else 0, "text": log_text, "note": log_note},
            "notes": notes,
        }
        return d

    def diagnostics_text(self, d: dict) -> str:
        """The bundle as plain text for pasting into a chat or an issue (already redacted)."""
        s, c = d["server"], d.get("container")
        L = [f"GameDock diagnostics for '{s['name']}' ({s['id']}), made {d['generated_at']}",
             f"GameDock {d['gamedock']['version']} ({d['gamedock']['commit']})", "",
             f"Game: {s['game']} ({s['game_id']}), image {s['image']}",
             f"Status: {s['status']}" + (f", phase {s['phase']}" if s["phase"] else "") + (", ready" if s["ready"] else ""),
             ]
        if s["phase_detail"]:
            L.append(f"  {s['phase_detail']}")
        if s["detail"]:
            L.append(f"Detail: {s['detail']}")
        if c:
            L.append(f"Container: {c['status']}, health {c['health']}, restarts {c['restart_count']}, exit code "
                     f"{c['exit_code'] if c['exit_code'] is not None else '-'}, out of memory: {'yes' if c['out_of_memory'] else 'no'}, started {c['started_at'] or '-'}")
            if c["error"]:
                L.append(f"  Docker error: {c['error']}")
        pt = d["ports"]
        L.append("Ports configured: " + (", ".join(f"{p['host']}/{p['protocol']}->{p['container']}" for p in pt["configured"]) or "none"))
        L.append("Ports published by Docker: " + (", ".join(f"{p['host_port']}/{p['protocol']}->{p['container']}" for p in pt["published"]) or "none"))
        if pt["not_published"]:
            L.append("Declared but not published: " + ", ".join(pt["not_published"]))
        L.append("Settings: " + ", ".join(f"{x['name']}={x['value']}" for x in d["settings"]))
        df = d["data_folder"]
        L.append("Data folder: " + (df.get("error") or f"{df['size']} in {df['files']} files" + (" (at least)" if df["truncated"] else "")))
        if d["disk"]:
            L.append(f"Disk free: {d['disk']['free']}")
        b = d["backups"]
        L.append(f"Backups: {b['count']}" + (f", newest {b['newest']}" if b["newest"] else ""))
        g = d.get("game_update_check")
        if g:
            L.append(f"Game build: installed {g['installed_build'] or 'unknown'}, newest {g['latest_build'] or 'unknown'} "
                     f"({g['status']}), last checked {g['checked_at'] or 'never'}")
        iu = d.get("image_update_check")
        if iu:
            L.append(f"Image update check: {iu['checked_at']}, update available: {iu['update_available']}")
        for n in d["notes"]:
            L.append(f"Note: {n}")
        L += ["", f"--- last {d['logs']['lines']} log lines (passwords hidden) ---", d["logs"]["text"] or d["logs"]["note"] or "(no log output)"]
        return "\n".join(L)
