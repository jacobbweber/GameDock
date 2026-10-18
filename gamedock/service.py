"""GameDock service layer.

All management actions live here. The web API calls these functions, and a
future MCP endpoint will call exactly the same ones. This module knows nothing
about HTTP.

Vocabulary:
  catalog entry = a game template (catalog/*.yaml)
  server        = a deployed instance of a catalog entry (stored under data/)
"""
from __future__ import annotations

import math
import re
import shlex
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Callable, Iterable, Iterator, Mapping

from . import backups as bk
from . import configfiles as cfg
from . import configforms as forms
from . import readiness as rd
from .backends.base import ContainerBackend, DockerDiagnosis
from .catalog import entry_to_dict, load_catalog, parse_definition
from .hoststats import HostMeter
from .hostcheck import foreign_owners
from .errors import BackendError, ConflictError, GameDockError, NotFoundError, ValidationError
from .models import (
    BackupResult, CatalogEntry, ConfigContent, ConfigFile, ConfigFileInfo, ConfigForm, ConfigFormResult, ConfigSaveResult,
    ContainerSpec, FormField, LogsResult,
    DataSummary, DeleteResult, EnvValue, HostStats, ImageInfo, Instance, PortMapping, RestoreResult, ServerInfo, ServerStats,
    StatsHistory, StatsPoint, StatsSnapshot, Status, UpdateResult, VolumeMapping, WipeResult,
)
from .store import InstanceStore

CONTAINER_PREFIX = "gamedock-"
LABEL_MANAGED = "gamedock.managed"
LABEL_SERVER_ID = "gamedock.server_id"
LABEL_GAME_ID = "gamedock.game_id"
MAX_ENV_VALUE_LEN = 4096
BACKUPS_DIRNAME = "backups"          # <data>/backups/<server id>/ ; also reserved so no server uses it
DELETED_DIRNAME = "_deleted"         # <data>/backups/_deleted/ holds backups taken when data was deleted
RESERVED_IDS = {BACKUPS_DIRNAME}
MAX_HISTORY_POINTS = 2000            # hard cap per series, whatever the interval/history settings
READINESS_LOG_LINES = 400            # how much log is read to find the "ready" line or a download percentage
READINESS_TTL = 2.0                  # seconds a readiness answer is reused (the page polls every 3 s)
MAX_LOG_LINES = 1000                 # most lines one log request may return
DEFAULT_LOG_LINES = 200
MAX_LOG_CHARS = 200_000              # cap on the text of one log answer (the newest part is kept)
MIN_SECRET_LEN = 3                   # secret setting values shorter than this are not searched for in logs


def container_name(server_id: str) -> str:
    return f"{CONTAINER_PREFIX}{server_id}"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")


class GameDockService:
    def __init__(self, backend: ContainerBackend, catalog_dir: Path, data_dir: Path,
                 backup_retention: int = 10, clock: Callable[[], datetime] | None = None,
                 stats_interval: float = 10.0, stats_history_seconds: int = 3600,
                 port_probe: Callable[[str, int], bool | None] | None = None) -> None:
        """`port_probe(protocol, port)` returns False when the host port is taken by another program (checked
        before a server starts); None/True mean "free or unknown". Tests leave it out."""
        self.backend = backend
        self.catalog_dir = Path(catalog_dir)
        self.data_dir = Path(data_dir)
        self.store = InstanceStore(self.data_dir / "instances.json")
        self.backup_retention = max(1, int(backup_retention))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.catalog_problems: list[tuple[str, str]] = []  # (file, problem) from the last catalog load
        self._port_probe = port_probe
        self._deploy_lock = Lock()  # deploys check names/ports and then store: do that one at a time
        self._docker_cache: tuple[float, DockerDiagnosis] | None = None
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._activity: dict[str, str] = {}               # server id -> what GameDock is doing to it right now
        self._pending: dict[str, ServerInfo] = {}         # deploys in progress (not stored yet)
        self._ready_seen: dict[str, str] = {}             # server id -> start time of the run that printed its ready line
        self._phase_cache: dict[str, tuple[float, tuple, rd.Readiness]] = {}
        self._entries_cache: tuple[float, dict[str, CatalogEntry]] | None = None
        # monitoring (all in memory)
        self.stats_interval = float(stats_interval)
        self.stats_history_seconds = int(stats_history_seconds)
        self._history_len = max(2, min(MAX_HISTORY_POINTS, math.ceil(self.stats_history_seconds / self.stats_interval)))
        self._meter = HostMeter()
        self._stats_lock = RLock()
        self._sample_lock = Lock()
        self._latest: StatsSnapshot | None = None
        self._latest_ts: datetime | None = None
        self._history: dict[str, deque] = {}
        self._host_history: deque = deque(maxlen=self._history_len)
        self._prev_net: dict[str, tuple[float, int, int]] = {}

    @contextmanager
    def _operation(self, server_id: str, activity: str = "") -> Iterator[None]:
        """Only one state-changing operation per server at a time (e.g. no start during a backup).
        `activity` is shown to everyone who looks at the server meanwhile ("stopping", "backing up", ...)."""
        with self._locks_guard:
            lock = self._locks.setdefault(server_id, Lock())
        if not lock.acquire(blocking=False):
            raise ConflictError("Another operation is already running on this server; wait for it to finish")
        if activity:
            self._activity[server_id] = activity
        try:
            yield
        finally:
            self._activity.pop(server_id, None)
            lock.release()
            self._latest_ts = None  # state may have changed: the next stats request reads fresh numbers

    # ------------------------------------------------------------------ catalog
    def _catalog(self) -> dict[str, CatalogEntry]:
        # Reloaded on each call so catalog edits show up without a restart. A broken file is skipped (and
        # remembered in catalog_problems) so one typo does not hide every other game.
        problems: list[tuple[str, str]] = []
        entries = load_catalog(self.catalog_dir, errors=problems)
        self.catalog_problems = problems
        return entries

    def list_catalog(self) -> list[CatalogEntry]:
        return list(self._catalog().values())

    def get_catalog_entry(self, game_id: str) -> CatalogEntry:
        try:
            return self._catalog()[game_id]
        except KeyError:
            raise NotFoundError(f"Unknown catalog entry '{game_id}'") from None

    # ------------------------------------------------------------------ helpers
    def _instance(self, server_id: str) -> Instance:
        inst = self.store.get(server_id)
        if inst is None:
            raise NotFoundError(f"Unknown server '{server_id}'")
        return inst

    @staticmethod
    def _template(inst: Instance) -> CatalogEntry:
        return parse_definition(inst.template, f"template of {inst.id}")

    def _spec(self, inst: Instance) -> ContainerSpec:
        t = self._template(inst)
        base = (self.data_dir / inst.id).resolve()
        volumes = []
        for v in t.volumes:
            host = Path(v.host)
            host_path = host if host.is_absolute() else (base / host).resolve()
            volumes.append(VolumeMapping(str(host_path), v.container))
        return ContainerSpec(
            name=container_name(inst.id),
            image=t.image,
            ports=inst.ports,
            volumes=tuple(volumes),
            env={**t.env, **inst.env},
            labels={LABEL_MANAGED: "true", LABEL_SERVER_ID: inst.id, LABEL_GAME_ID: inst.game_id},
        )

    def _prepare_dirs(self, spec: ContainerSpec) -> None:
        # Make host folders ourselves so Docker doesn't create them owned by root.
        for v in spec.volumes:
            Path(v.host).mkdir(parents=True, exist_ok=True)

    def _info(self, inst: Instance, show_activity: bool = False) -> ServerInfo:
        """`show_activity`: include what GameDock is doing to the server right now. Left off for the answer to an
        action itself (which is returned when the action is already over)."""
        t = self._template(inst)
        health, started = "", ""
        try:
            state = self.backend.get_state(container_name(inst.id))
            status, detail = state.status, state.detail
            health, started = state.health, state.started_at
        except BackendError as e:
            status, detail = Status.ERROR, str(e)
        r = self._readiness(inst, t, health, started) if status == Status.RUNNING else None
        if status != Status.RUNNING:
            self._forget_readiness(inst.id)
        env = tuple(
            EnvValue(
                name=f.name, label=f.label, secret=f.secret, required=f.required,
                description=f.description,
                value=None if f.secret else inst.env.get(f.name, f.default),
                is_set=bool(inst.env.get(f.name, f.default)),
            )
            for f in t.fields
        )
        return ServerInfo(inst.id, inst.name, inst.game_id, t.name, t.image, t.description,
                          status, inst.ports, env, detail, inst.created_at, t.update_method, len(t.config_files),
                          activity=self._activity.get(inst.id, "") if show_activity else "",
                          phase=r.phase if r else "", ready=bool(r and r.ready),
                          progress=r.progress if r else None, phase_detail=r.detail if r else "")

    # --------------------------------------------------------------- readiness
    def _current_entries(self) -> dict[str, CatalogEntry]:
        """The catalog, re-read at most every 30 s (this is on the polling path)."""
        now = time.monotonic()
        cached = self._entries_cache
        if cached and now - cached[0] < 30.0:
            return cached[1]
        entries = self._catalog()
        self._entries_cache = (now, entries)
        return entries

    def _with_current_patterns(self, inst: Instance, t: CatalogEntry) -> CatalogEntry:
        """A server keeps a snapshot of its template, so one deployed before a catalog entry gained readiness patterns
        has none. Those two fields only describe how to read the game's log (they change no container), so take them
        from the current catalog entry of the same game when the snapshot has neither."""
        if t.ready_log_pattern or t.progress_log_pattern:
            return t
        try:
            cur = self._current_entries().get(inst.game_id)
        except Exception:  # noqa: BLE001 - a broken catalog must never break the server list
            return t
        if cur is None or not (cur.ready_log_pattern or cur.progress_log_pattern):
            return t
        return replace(t, ready_log_pattern=cur.ready_log_pattern, progress_log_pattern=cur.progress_log_pattern)

    def _forget_readiness(self, server_id: str) -> None:
        self._ready_seen.pop(server_id, None)
        self._phase_cache.pop(server_id, None)

    def _readiness(self, inst: Instance, t: CatalogEntry, health: str, started: str) -> rd.Readiness:
        """Is the running game joinable? Cheap: no extra engine call once the answer is "ready" (or when the image
        has a health check that says healthy); otherwise at most one `docker logs` call every READINESS_TTL seconds."""
        if health == "healthy":
            return rd.decide(health, bool(t.ready_log_pattern), False, None)
        t = self._with_current_patterns(inst, t)
        if self._ready_seen.get(inst.id) == started and t.ready_log_pattern:
            return rd.decide(health, True, True, None)  # the ready line was seen during this run; logs may have scrolled
        key = (health, started)
        cached = self._phase_cache.get(inst.id)
        now = time.monotonic()
        if cached and cached[1] == key and now - cached[0] < READINESS_TTL:
            return cached[2]
        text = ""
        if t.ready_log_pattern or t.progress_log_pattern:
            try:
                text = self.backend.get_logs(container_name(inst.id), READINESS_LOG_LINES, started or None)
            except BackendError:
                text = ""  # logs are only a hint; never break the server list because of them
        seen = rd.pattern_seen(text, t.ready_log_pattern)
        if seen:
            self._ready_seen[inst.id] = started
        result = rd.decide(health, bool(t.ready_log_pattern), seen, rd.last_progress(text, t.progress_log_pattern))
        self._phase_cache[inst.id] = (now, key, result)
        return result

    # --------------------------------------------------------------- validation
    @staticmethod
    def _resolve_ports(t: CatalogEntry, requested: Iterable[PortMapping] | None,
                       current: Iterable[PortMapping] | None = None) -> tuple[PortMapping, ...]:
        """Start from `current` (or the template defaults) and apply requested host-port overrides."""
        base = {(p.protocol, p.container): p.host for p in (current if current is not None else t.ports)}
        for r in requested or ():
            key = (r.protocol, r.container)
            if key not in base:
                raise ValidationError(f"{t.name} has no container port {r.container}/{r.protocol}")
            base[key] = r.host
        result = []
        for (proto, cont), host in base.items():
            if not isinstance(host, int) or not (1 <= host <= 65535):
                raise ValidationError(f"Host port for {cont}/{proto} must be 1-65535")
            result.append(PortMapping(proto, host, cont))
        seen: dict[tuple[str, int], int] = {}
        for p in result:
            if (p.protocol, p.host) in seen:
                raise ValidationError(f"Host port {p.host}/{p.protocol} is used twice in this server")
            seen[(p.protocol, p.host)] = p.container
        return tuple(result)

    def _check_port_conflicts(self, ports: Iterable[PortMapping], ignore_id: str | None = None) -> None:
        for other in self.store.all():
            if other.id == ignore_id:
                continue
            used = {(p.protocol, p.host) for p in other.ports}
            for p in ports:
                if (p.protocol, p.host) in used:
                    raise ConflictError(
                        f"Host port {p.host}/{p.protocol} is already used by server '{other.name}'")

    @staticmethod
    def _merge_env(t: CatalogEntry, provided: Mapping[str, str] | None,
                   current: Mapping[str, str] | None = None) -> dict[str, str]:
        """Apply provided values over current (or defaults). Blank secrets keep the existing value."""
        by_name = {f.name: f for f in t.fields}
        values = {f.name: f.default for f in t.fields} if current is None else dict(current)
        for name, value in (provided or {}).items():
            f = by_name.get(name)
            if f is None:
                raise ValidationError(f"Unknown setting '{name}' for {t.name}")
            value = "" if value is None else str(value)
            if "\n" in value or "\r" in value or "\0" in value or len(value) > MAX_ENV_VALUE_LEN:
                raise ValidationError(f"Invalid value for '{f.label}'")
            if f.secret and value == "" and current is not None:
                continue  # blank on edit = keep what is stored
            values[name] = value
        for f in t.fields:
            if f.required and not values.get(f.name, "").strip():
                raise ValidationError(f"'{f.label}' is required")
        return values

    def _unique_id(self, name: str) -> str:
        base = slugify(name)
        if not base:
            raise ValidationError("Name must contain at least one letter or digit")
        existing = {i.id for i in self.store.all()} | RESERVED_IDS
        candidate, n = base, 2
        while candidate in existing:
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def _check_name(self, name: str, ignore_id: str | None = None) -> str:
        name = (name or "").strip()
        if not (1 <= len(name) <= 64):
            raise ValidationError("Name must be 1 to 64 characters")
        if any(ord(c) < 32 for c in name):
            raise ValidationError("Name contains invalid characters")
        for i in self.store.all():
            if i.id != ignore_id and i.name.lower() == name.lower():
                raise ConflictError(f"A server named '{i.name}' already exists")
        return name

    # --------------------------------------------------------------- public API
    def list_servers(self) -> list[ServerInfo]:
        return [self._info(i, True) for i in self.store.all()] + list(self._pending.values())

    def get_status(self, server_id: str) -> ServerInfo:
        pending = self._pending.get(server_id)
        if pending is not None:
            return pending
        return self._info(self._instance(server_id), True)

    def deploy(self, game_id: str, name: str, ports: Iterable[PortMapping] | None = None,
               env: Mapping[str, str] | None = None, start: bool = False) -> ServerInfo:
        """Create a server from a catalog entry: store it and create its container (stopped)."""
        with self._deploy_lock:
            return self._deploy(game_id, name, ports, env, start)

    def _deploy(self, game_id: str, name: str, ports: Iterable[PortMapping] | None,
                env: Mapping[str, str] | None, start: bool) -> ServerInfo:
        t = self.get_catalog_entry(game_id)
        name = self._check_name(name)
        resolved = self._resolve_ports(t, ports)
        self._check_port_conflicts(resolved)
        values = self._merge_env(t, env)
        inst = Instance(
            id=self._unique_id(name), name=name, game_id=game_id, template=entry_to_dict(t),
            ports=resolved, env=values,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        spec = self._spec(inst)
        self._prepare_dirs(spec)
        # While the image downloads and the container is created (can take minutes) the server is visible as a
        # "deploying" entry to the web page and to MCP clients. Only one deploy runs at a time (see deploy()).
        self._pending[inst.id] = ServerInfo(
            inst.id, inst.name, inst.game_id, t.name, t.image, t.description, Status.NOT_CREATED, resolved,
            created_at=inst.created_at, update_method=t.update_method, config_file_count=len(t.config_files),
            activity="deploying",
            phase_detail="Downloading the game image and creating the server. This can take a few minutes.")
        try:
            self.backend.create(spec)  # if this fails nothing is stored
            self.store.put(inst)
            if start:
                self._pending[inst.id] = replace(self._pending[inst.id], activity="starting")
                try:
                    self._check_host_ports(inst)
                    self.backend.start(spec.name)
                except ConflictError as e:
                    raise ConflictError(f"Server '{name}' was deployed but could not be started: {e}") from e
                except BackendError as e:
                    raise BackendError(f"Server '{name}' was deployed but could not be started: {e}") from e
        finally:
            self._pending.pop(inst.id, None)
        return self._info(inst)

    def update(self, server_id: str, name: str | None = None, ports: Iterable[PortMapping] | None = None,
               env: Mapping[str, str] | None = None) -> ServerInfo:
        """Change a stopped server's name, host ports or settings (its container is recreated)."""
        with self._operation(server_id, "saving settings"):
            inst = self._instance(server_id)
            t = self._template(inst)
            cname = container_name(inst.id)
            if self.backend.get_state(cname).status == Status.RUNNING:
                raise ConflictError("Stop the server before changing its settings")
            new_name = self._check_name(name, ignore_id=inst.id) if name is not None else inst.name
            new_ports = self._resolve_ports(t, ports, current=inst.ports)
            self._check_port_conflicts(new_ports, ignore_id=inst.id)
            new_env = self._merge_env(t, env, current=inst.env)
            updated = Instance(inst.id, new_name, inst.game_id, inst.template, new_ports, new_env, inst.created_at)
            spec = self._spec(updated)
            self._prepare_dirs(spec)
            self.backend.remove(cname)  # container settings are fixed at creation, so recreate it
            self.backend.create(spec)
            self.store.put(updated)
            return self._info(updated)

    # ------------------------------------------------------------ lifecycle
    def _start(self, inst: Instance) -> None:
        spec = self._spec(inst)
        if self.backend.get_state(spec.name).status == Status.RUNNING:
            return
        self._prepare_dirs(spec)
        self.backend.create(spec)
        self._check_host_ports(inst)
        self.backend.start(spec.name)

    def _check_host_ports(self, inst: Instance) -> None:
        """Refuse to start if another program on this machine already holds one of the host ports."""
        if self._port_probe is None:
            return
        for p in inst.ports:
            if self._port_probe(p.protocol, p.host) is False:
                raise ConflictError(
                    f"Host port {p.host}/{p.protocol} is already in use on this machine by another program or "
                    f"container, so '{inst.name}' cannot start. Edit the server to pick another port, or stop "
                    "whatever uses it (find it with: sudo ss -lntup | grep :" + str(p.host) + ").")

    def docker_status(self, max_age: float = 10.0) -> DockerDiagnosis:
        """Can GameDock use the container engine right now? Cached briefly; never raises."""
        import time
        now = time.monotonic()
        cached = self._docker_cache
        if cached and now - cached[0] < max_age:
            return cached[1]
        try:
            d = self.backend.diagnose()
        except Exception as e:  # a broken backend must not break the status page
            d = DockerDiagnosis(False, None, None, False, None, "error", f"Could not check Docker: {e}")
        self._docker_cache = (now, d)
        return d

    def _stop(self, inst: Instance) -> None:
        name = container_name(inst.id)
        if self.backend.get_state(name).status == Status.RUNNING:
            self.backend.stop(name)

    def start(self, server_id: str) -> ServerInfo:
        """Start a server (re-creating its container if it is missing)."""
        with self._operation(server_id, "starting"):
            inst = self._instance(server_id)
            self._start(inst)
            return self._info(inst)

    def stop(self, server_id: str) -> ServerInfo:
        with self._operation(server_id, "stopping"):
            inst = self._instance(server_id)
            self._stop(inst)
            return self._info(inst)

    def restart(self, server_id: str) -> ServerInfo:
        with self._operation(server_id, "restarting"):
            inst = self._instance(server_id)
            self._stop(inst)
            self._start(inst)
            return self._info(inst)

    # ------------------------------------------------------------ paths
    def _data_dir_of(self, inst: Instance) -> Path:
        return (self.data_dir / inst.id).resolve()

    def _backup_dir(self, inst: Instance) -> Path:
        return self.data_dir / BACKUPS_DIRNAME / inst.id

    def _deleted_dir(self) -> Path:
        return self.data_dir / BACKUPS_DIRNAME / DELETED_DIRNAME

    def _is_running(self, inst: Instance) -> bool:
        return self.backend.get_state(container_name(inst.id)).status == Status.RUNNING

    def _require_stopped(self, inst: Instance, why: str) -> None:
        if self._is_running(inst):
            raise ConflictError(f"Stop the server before {why}")

    def get_data_summary(self, server_id: str) -> DataSummary:
        """File count and size of a server's data folder (read-only)."""
        inst = self._instance(server_id)
        path = self._data_dir_of(inst)
        files, size, truncated = bk.folder_usage(path)
        return DataSummary(str(path), files, size, truncated)

    def secret_field_names(self) -> set[str]:
        """Names of every field marked secret in the catalog or in a deployed server (used to mask audit logs)."""
        names: set[str] = set()
        try:
            entries = list(self._catalog().values())
        except GameDockError:
            entries = []  # a broken catalog file must not break auditing
        for inst in self.store.all():
            try:
                entries.append(self._template(inst))
            except GameDockError:
                continue
        for e in entries:
            names.update(f.name for f in e.fields if f.secret)
        return names

    # ------------------------------------------------------------ backups
    def list_backups(self, server_id: str) -> list[bk.BackupInfo]:
        inst = self._instance(server_id)
        return bk.list_backups(self._backup_dir(inst))

    def _safety_backup(self, inst: Instance, kind: str, protect: set[str] | None = None) -> bk.BackupInfo:
        """Back up a stopped server's data into its backups folder (retention applied)."""
        info = bk.create_archive(self._data_dir_of(inst), self._backup_dir(inst), inst.id, self._clock(), kind)
        bk.prune(self._backup_dir(inst), self.backup_retention, protect=(protect or set()) | {info.name})
        return info

    def create_backup(self, server_id: str, live: bool = False) -> BackupResult:
        """Back up a server's data folder.

        Safe by default: a running server is stopped, backed up, and started again so the files are
        consistent. With live=True a running server is backed up as it runs (files may be mid-write).
        """
        with self._operation(server_id, "backing up"):
            inst = self._instance(server_id)
            was_running = self._is_running(inst)
            stop_first = was_running and not live
            if stop_first:
                self._stop(inst)
            try:
                info = bk.create_archive(self._data_dir_of(inst), self._backup_dir(inst), inst.id, self._clock())
            except BaseException:
                if stop_first:
                    try:
                        self._start(inst)  # the backup failed; do not leave the server down
                    except GameDockError:
                        pass
                raise
            warnings: list[str] = []
            restarted = False
            if stop_first:
                try:
                    self._start(inst)
                    restarted = True
                except GameDockError as e:
                    warnings.append(f"The backup succeeded but the server could not be started again: {e}")
            removed = bk.prune(self._backup_dir(inst), self.backup_retention, protect={info.name})
            if not was_running:
                note = "The server was stopped, so the backup is consistent."
            elif stop_first:
                note = ("The server was running, so it was stopped for a consistent backup"
                        + (" and has been started again." if restarted else "."))
            else:
                note = ("The server was running and was NOT stopped (live backup). Files being written during "
                        "the backup may be inconsistent; prefer a stopped backup for anything important.")
            if removed:
                note += f" Removed {len(removed)} old backup(s) to keep the newest {self.backup_retention}."
            return BackupResult(info, was_running, stop_first, restarted, was_running and live,
                                tuple(removed), note, tuple(warnings))

    def restore_backup(self, server_id: str, name: str, confirm: bool = False) -> RestoreResult:
        """Replace a stopped server's data with a backup, after taking a safety backup of the current data."""
        with self._operation(server_id, "restoring"):
            inst = self._instance(server_id)
            if not confirm:
                raise ValidationError("Restoring replaces the current data and needs explicit confirmation (confirm=true)")
            archive = bk.backup_path(self._backup_dir(inst), name)
            self._require_stopped(inst, "restoring a backup")
            safety = self._safety_backup(inst, "pre-restore", protect={name})  # aborts the restore if it fails
            owners, example = foreign_owners(self._data_dir_of(inst))
            warnings = bk.restore_archive(archive, self._data_dir_of(inst))
            if owners:
                ids = ", ".join(str(u) for u in sorted(owners))
                warnings.append(
                    f"Some of the old files were owned by user id {ids} (for example {example}); the restored "
                    f"files are owned by the user running GameDock. If the game runs as another user it may not be "
                    f"able to write to them. Fix with: sudo chown -R {sorted(owners)[0]} "
                    f"{shlex.quote(str(self._data_dir_of(inst)))}")
            self._prepare_dirs(self._spec(inst))
            return RestoreResult(
                name, safety,
                f"Restored '{name}'. Your previous data was saved first as '{safety.name}'.", tuple(warnings))

    def delete_backup(self, server_id: str, name: str, confirm: bool = False) -> None:
        inst = self._instance(server_id)
        if not confirm:
            raise ValidationError("Deleting a backup needs explicit confirmation (confirm=true)")
        path = bk.backup_path(self._backup_dir(inst), name)
        try:
            path.unlink()
        except OSError as e:
            from .errors import OperationError
            raise OperationError(f"Could not delete the backup: {e.strerror or e}") from e

    def wipe_data(self, server_id: str, confirm: bool = False) -> WipeResult:
        """Empty a stopped server's data folder (a safety backup is taken first; if it fails, nothing is wiped)."""
        with self._operation(server_id, "wiping data"):
            inst = self._instance(server_id)
            if not confirm:
                raise ValidationError("Wiping data needs explicit confirmation (confirm=true)")
            self._require_stopped(inst, "wiping its data")
            safety = self._safety_backup(inst, "pre-wipe")
            bk.empty_directory(self._data_dir_of(inst))
            self._prepare_dirs(self._spec(inst))
            return WipeResult(safety, f"All data was deleted. A backup was saved first as '{safety.name}'.")

    # ------------------------------------------------------------ delete
    def delete(self, server_id: str, confirm: bool = False, delete_data: bool = False,
               confirm_delete_data: bool = False) -> DeleteResult:
        """Remove a server and its container.

        By default the data folder is kept. With delete_data (plus confirm_delete_data) the data folder is
        deleted too, but only after a backup has been saved outside it; if that backup fails nothing is deleted.
        """
        with self._operation(server_id, "deleting"):
            inst = self._instance(server_id)
            if not confirm:
                raise ValidationError(f"Deleting '{inst.name}' needs explicit confirmation (confirm=true)")
            if delete_data and not confirm_delete_data:
                raise ValidationError(
                    "Deleting data needs a second explicit confirmation (confirm_delete_data=true)")
            cname = container_name(inst.id)
            self._require_stopped(inst, "deleting it")
            backup = None
            if delete_data:
                data = self._data_dir_of(inst)
                if data.is_dir() and any(data.iterdir()):
                    backup = bk.create_archive(data, self._deleted_dir(), inst.id, self._clock(), "pre-delete")
                # (an empty or missing data folder has nothing worth backing up)
            self.backend.remove(cname)
            if delete_data:
                bk.remove_tree(self._data_dir_of(inst))  # on failure the server entry is kept so you can retry
            self.store.remove(inst.id)
            if delete_data:
                note = (f"Server deleted together with its data. A backup was saved at {backup.path}." if backup
                        else "Server deleted together with its (empty) data folder.")
            else:
                note = "Server deleted. Its data folder was kept."
            return DeleteResult(inst.id, delete_data, backup, note)

    # ------------------------------------------------------------ image updates
    def get_image_info(self, server_id: str) -> ImageInfo:
        inst = self._instance(server_id)
        t = self._template(inst)
        return ImageInfo(t.image, self.backend.get_container_image_id(container_name(inst.id)),
                         self.backend.get_image_id(t.image))

    def update_server_image(self, server_id: str, confirm: bool = False) -> UpdateResult:
        """Update a server to the newest version of its image, keeping data and settings.

        The newest image is downloaded first. If it is the one already in use nothing else happens. Otherwise: stop
        the server if it runs (with the usual grace period), back up its data, recreate the container from the new
        image, and start it again if it had been running.
        """
        with self._operation(server_id, "updating"):
            inst = self._instance(server_id)
            t = self._template(inst)
            if not confirm:
                raise ValidationError("Updating needs explicit confirmation (confirm=true)")
            if t.update_method == "none":
                raise ValidationError(f"{t.name} does not support image updates from GameDock")
            cname = container_name(inst.id)
            was_running = self._is_running(inst)
            before = self.backend.get_container_image_id(cname)
            self.backend.pull_image(t.image)  # nothing has been touched yet if this fails
            newest = self.backend.get_image_id(t.image)
            if before is not None and newest is not None and before == newest:
                return UpdateResult(t.image, before, before, False, was_running, False, None,
                                    "Already up to date. Nothing was changed.")
            stopped = False
            if was_running:
                self._stop(inst)
                stopped = True
            try:
                backup = self._safety_backup(inst, "pre-update")
            except BaseException:
                if stopped:
                    try:
                        self._start(inst)
                    except GameDockError:
                        pass
                raise  # no backup, no update
            self.backend.remove(cname)
            self.backend.create(self._spec(inst))
            after = self.backend.get_container_image_id(cname)
            warnings: list[str] = []
            restarted = False
            if was_running:
                try:
                    self.backend.start(cname)
                    restarted = True
                except GameDockError as e:
                    warnings.append(f"Updated, but the server could not be started again: {e}")
            note = (f"Updated to the newest image. Data was backed up first as '{backup.name}'."
                    + (" The server was restarted." if restarted else ""))
            return UpdateResult(t.image, before, after, True, was_running, restarted, backup, note, tuple(warnings))

    # ------------------------------------------------------------ config files
    def _config_file(self, inst: Instance, path: str) -> ConfigFile:
        for c in self._template(inst).config_files:
            if c.path == path:
                return c
        raise NotFoundError(f"'{path}' is not an editable config file for this server")

    def _config_info(self, inst: Instance, c: ConfigFile) -> ConfigFileInfo:
        root = self._data_dir_of(inst)
        try:
            p = cfg.safe_path(root, c.path)
            exists = p.is_file()
            size = p.stat().st_size if exists else 0
            baks = tuple(cfg.list_baks(p))
        except GameDockError:
            exists, size, baks = False, 0, ()
        return ConfigFileInfo(c.path, c.label, c.format, c.sensitive_keys, c.hot_edit, exists, size, baks, c.note)

    def list_config_files(self, server_id: str) -> list[ConfigFileInfo]:
        inst = self._instance(server_id)
        return [self._config_info(inst, c) for c in self._template(inst).config_files]

    def read_config_file(self, server_id: str, path: str) -> ConfigContent:
        inst = self._instance(server_id)
        c = self._config_file(inst, path)
        r = cfg.read_config(self._data_dir_of(inst), c)
        running = self._is_running(inst)
        editable = c.hot_edit or not running
        if not r.exists:
            note = "This game has not created its settings file yet; it appears after the first full start."
        elif c.hot_edit:
            note = "This file is safe to edit while the server runs, but the game may need a restart to notice."
        else:
            note = "Changes take effect after the server is restarted."
        if running and not c.hot_edit:
            note = "Stop the server to edit this file. " + note
        if r.masked:
            note += f" Values for {', '.join(c.sensitive_keys)} are hidden; leave the placeholder to keep them."
        form = None
        if r.exists:
            try:
                form = forms.build_form(r.content, c)
            except Exception:  # the structured view is a convenience; the raw text must always be readable
                form = None
        return ConfigContent(self._config_info(inst, c), r.content, r.masked, running, editable, note, form)

    def config_form(self, server_id: str, path: str, content: str | None = None,
                    changes: dict[str, str] | None = None) -> ConfigFormResult:
        """Structured view helper (read-only, writes nothing). Parses `content` (the masked text the editor holds;
        default: the file as it is on disk), applies `changes` ({field id: new value}) to it and returns the fields
        plus the resulting text. Used to switch between the form and the raw text, and to prepare a save."""
        inst = self._instance(server_id)
        c = self._config_file(inst, path)
        if content is None:
            r = cfg.read_config(self._data_dir_of(inst), c)
            if not r.exists:
                raise ValidationError("This game has not created its settings file yet; it appears after the first full start.")
            content = r.content
        if len(content.encode("utf-8")) > cfg.MAX_CONFIG_BYTES:
            raise ValidationError(f"Content is larger than the editor limit of {cfg.MAX_CONFIG_BYTES} bytes")
        text, changed = forms.apply_changes(content, c, changes) if changes else (content, [])
        return ConfigFormResult(forms.build_form(text, c), text, tuple(changed))

    def write_config_file(self, server_id: str, path: str, content: str) -> ConfigSaveResult:
        with self._operation(server_id, "saving config"):
            inst = self._instance(server_id)
            c = self._config_file(inst, path)
            running = self._is_running(inst)
            if running and not c.hot_edit:
                raise ConflictError("Stop the server before editing this file (it is not marked safe to edit while running)")
            w = cfg.write_config(self._data_dir_of(inst), c, content, self._clock())
            note = "Saved." + (f" The previous version was kept as '{w.backup}'." if w.backup else "")
            note += (" Restart the server for the change to take effect." if running
                     else " The change applies the next time the server starts.")
            return ConfigSaveResult(c.path, w.size, w.backup, running, note)

    # ------------------------------------------------------------ logs
    def get_logs(self, server_id: str, lines: int | None = None, max_chars: int = MAX_LOG_CHARS) -> LogsResult:
        """The newest `lines` lines of the game's console output. Colour codes are removed and the values of the
        server's secret settings (passwords) are replaced by a placeholder in case the game prints them."""
        inst = self._instance(server_id)
        n = DEFAULT_LOG_LINES if lines is None else int(lines)
        if not 1 <= n <= MAX_LOG_LINES:
            raise ValidationError(f"lines must be between 1 and {MAX_LOG_LINES}")
        cname = container_name(inst.id)
        state = self.backend.get_state(cname)
        if state.status == Status.NOT_CREATED:
            return LogsResult(server_id, state.status.value, 0, False, "",
                              "This server has no container yet, so there are no logs. Start it first.")
        raw = self.backend.get_logs(cname, n + 1)  # one extra line tells us whether older lines were cut
        text = self._redact(inst, rd.clean_log_text(raw)).rstrip("\n")
        all_lines = text.split("\n") if text else []
        truncated = len(all_lines) > n
        all_lines = all_lines[-n:]
        text = "\n".join(all_lines)
        if len(text) > max_chars:
            text = text[-max_chars:]
            text = text[text.find("\n") + 1:] if "\n" in text else text  # do not start in the middle of a line
            truncated = True
        count = len(text.split("\n")) if text else 0
        note = "" if text else "The game has not written anything yet."
        return LogsResult(server_id, state.status.value, count, truncated, text, note)

    def _redact(self, inst: Instance, text: str) -> str:
        try:
            secrets = {f.name for f in self._template(inst).fields if f.secret}
        except GameDockError:
            return text
        values = sorted({inst.env.get(n, "") for n in secrets if len(inst.env.get(n, "")) >= MIN_SECRET_LEN},
                        key=len, reverse=True)
        for v in values:
            text = text.replace(v, cfg.MASK)
        return text

    # ------------------------------------------------------------ monitoring
    def sample_stats(self, record: bool = True) -> StatsSnapshot:
        """Take one reading of every server plus the host and remember it as the latest snapshot.

        Costs one container-engine call when nothing runs (two when something does), however many servers
        exist. With record=True, and only if at least one server is running, the reading is also added to the
        in-memory history used for sparklines.
        """
        if not self._sample_lock.acquire(blocking=False):
            return self._latest or self._empty_snapshot()  # another sample is in progress
        try:
            now = self._clock()
            insts = self.store.all()
            error = ""
            raw = {}
            if insts:
                try:
                    raw = self.backend.get_stats([container_name(i.id) for i in insts])
                except BackendError as e:
                    error = str(e)
            host = self._meter.read(self.data_dir)
            servers = tuple(self._server_stats(i, raw.get(container_name(i.id)), host, now) for i in insts)
            snap = StatsSnapshot(now.isoformat(timespec="seconds").replace("+00:00", "Z"), self.stats_interval,
                                 host, servers, error)
            with self._stats_lock:
                self._latest, self._latest_ts = snap, now
                if record and any(s.running for s in servers):
                    t = now.timestamp()
                    for s in servers:
                        if s.running:
                            ring = self._history.setdefault(s.server_id, deque(maxlen=self._history_len))
                            ring.append((t, s.cpu_percent, s.mem_used, s.net_rx_rate, s.net_tx_rate))
                    self._host_history.append((t, host.cpu_percent or 0.0, host.mem_used or 0))
                ids = {i.id for i in insts}
                for gone in [k for k in self._history if k not in ids]:
                    del self._history[gone]
                for gone in [k for k in self._prev_net if k not in ids]:
                    del self._prev_net[gone]
            return snap
        finally:
            self._sample_lock.release()

    def _empty_snapshot(self) -> StatsSnapshot:
        host = self._meter.read(self.data_dir)
        return StatsSnapshot(self._clock().isoformat(timespec="seconds").replace("+00:00", "Z"),
                             self.stats_interval, host, ())

    def _server_stats(self, inst: Instance, c, host: HostStats, now: datetime) -> ServerStats:
        t = self._template(inst)
        running = bool(c and c.running)
        if not running:
            self._prev_net.pop(inst.id, None)
            return ServerStats(inst.id, inst.name, inst.game_id, False, 0.0, 0.0, 0, c.mem_limit if c else 0, 0.0,
                               0, 0, 0.0, 0.0, 0, 0, 0, None, None)
        ts = now.timestamp()
        prev = self._prev_net.get(inst.id)
        rx_rate = tx_rate = 0.0
        if prev and ts > prev[0] and c.net_rx >= prev[1] and c.net_tx >= prev[2]:  # counters reset on restart
            rx_rate = (c.net_rx - prev[1]) / (ts - prev[0])
            tx_rate = (c.net_tx - prev[2]) / (ts - prev[0])
        self._prev_net[inst.id] = (ts, c.net_rx, c.net_tx)
        uptime = max(0.0, (now - c.started_at).total_seconds()) if c.started_at else None
        mem_pct = 100.0 * c.mem_used / host.mem_total if host.mem_total else 0.0
        return ServerStats(
            inst.id, inst.name, inst.game_id, True, round(c.cpu_percent, 2),
            round(min(100.0, c.cpu_percent / max(1, host.cpu_count)), 2), c.mem_used, c.mem_limit, round(mem_pct, 2),
            c.net_rx, c.net_tx, round(rx_rate, 1), round(tx_rate, 1), c.block_read, c.block_write, c.pids,
            round(uptime, 1) if uptime is not None else None,
            c.started_at.isoformat(timespec="seconds").replace("+00:00", "Z") if c.started_at else None)

    def get_all_stats(self, sort_by: str | None = None) -> StatsSnapshot:
        """Latest stats for every server and the host. Uses the sampler's snapshot when it is recent;
        otherwise reads once now (without adding to the history). sort_by: 'cpu', 'memory' or 'name'."""
        with self._stats_lock:
            snap, ts = self._latest, self._latest_ts
        fresh = snap is not None and ts is not None and (self._clock() - ts).total_seconds() < self.stats_interval * 3
        if not fresh:
            snap = self.sample_stats(record=False)
        if sort_by:
            keys = {"cpu": lambda s: -s.cpu_percent, "memory": lambda s: -s.mem_used, "name": lambda s: s.name.lower()}
            if sort_by not in keys:
                raise ValidationError("sort must be one of: cpu, memory, name")
            snap = StatsSnapshot(snap.sampled_at, snap.interval, snap.host, tuple(sorted(snap.servers, key=keys[sort_by])),
                                 snap.error)
        return snap

    def get_server_stats(self, server_id: str) -> ServerStats:
        self._instance(server_id)
        for s in self.get_all_stats().servers:
            if s.server_id == server_id:
                return s
        raise NotFoundError(f"No stats for server '{server_id}'")

    def get_host_stats(self) -> HostStats:
        """Host totals, read live from /proc and the disk (cheap, no container-engine call)."""
        return self._meter.read(self.data_dir)

    def _window(self, window: int | None) -> int:
        w = 600 if window is None else int(window)
        if w < 10:
            raise ValidationError("window must be at least 10 seconds")
        return min(w, self.stats_history_seconds)

    def _points(self, ring, window: int):
        cutoff = self._clock().timestamp() - window
        return tuple(StatsPoint(*p) for p in list(ring) if p[0] >= cutoff)

    def get_stats_history(self, server_id: str, window: int | None = 600) -> StatsHistory:
        """Recent samples for one server (oldest first). window = seconds back, capped at the history kept."""
        self._instance(server_id)
        w = self._window(window)
        with self._stats_lock:
            ring = list(self._history.get(server_id, ()))
        return StatsHistory(server_id, self.stats_interval, w, self._points(ring, w))

    def get_all_stats_history(self, window: int | None = 600) -> tuple[dict[str, StatsHistory], list[tuple]]:
        """History for every server plus host (t, cpu%, mem_used) tuples, in one call."""
        w = self._window(window)
        with self._stats_lock:
            rings = {k: list(v) for k, v in self._history.items()}
            host = list(self._host_history)
        cutoff = self._clock().timestamp() - w
        return ({k: StatsHistory(k, self.stats_interval, w, self._points(v, w)) for k, v in rings.items()},
                [p for p in host if p[0] >= cutoff])
