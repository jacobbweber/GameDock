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
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Callable, Iterable, Iterator, Mapping

from . import backups as bk
from . import configfiles as cfg
from .backends.base import ContainerBackend
from .catalog import entry_to_dict, load_catalog, parse_definition
from .hoststats import HostMeter
from .errors import BackendError, ConflictError, GameDockError, NotFoundError, ValidationError
from .models import (
    BackupResult, CatalogEntry, ConfigContent, ConfigFile, ConfigFileInfo, ConfigSaveResult, ContainerSpec,
    DeleteResult, EnvValue, HostStats, ImageInfo, Instance, PortMapping, RestoreResult, ServerInfo, ServerStats,
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


def container_name(server_id: str) -> str:
    return f"{CONTAINER_PREFIX}{server_id}"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")


class GameDockService:
    def __init__(self, backend: ContainerBackend, catalog_dir: Path, data_dir: Path,
                 backup_retention: int = 10, clock: Callable[[], datetime] | None = None,
                 stats_interval: float = 10.0, stats_history_seconds: int = 3600) -> None:
        self.backend = backend
        self.catalog_dir = Path(catalog_dir)
        self.data_dir = Path(data_dir)
        self.store = InstanceStore(self.data_dir / "instances.json")
        self.backup_retention = max(1, int(backup_retention))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
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
    def _operation(self, server_id: str) -> Iterator[None]:
        """Only one state-changing operation per server at a time (e.g. no start during a backup)."""
        with self._locks_guard:
            lock = self._locks.setdefault(server_id, Lock())
        if not lock.acquire(blocking=False):
            raise ConflictError("Another operation is already running on this server; wait for it to finish")
        try:
            yield
        finally:
            lock.release()

    # ------------------------------------------------------------------ catalog
    def _catalog(self) -> dict[str, CatalogEntry]:
        # Reloaded on each call so catalog edits show up without a restart.
        return load_catalog(self.catalog_dir)

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

    def _info(self, inst: Instance) -> ServerInfo:
        t = self._template(inst)
        try:
            state = self.backend.get_state(container_name(inst.id))
            status, detail = state.status, state.detail
        except BackendError as e:
            status, detail = Status.ERROR, str(e)
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
                          status, inst.ports, env, detail, inst.created_at, t.update_method, len(t.config_files))

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
        return [self._info(i) for i in self.store.all()]

    def get_status(self, server_id: str) -> ServerInfo:
        return self._info(self._instance(server_id))

    def deploy(self, game_id: str, name: str, ports: Iterable[PortMapping] | None = None,
               env: Mapping[str, str] | None = None, start: bool = False) -> ServerInfo:
        """Create a server from a catalog entry: store it and create its container (stopped)."""
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
        self.backend.create(spec)  # if this fails nothing is stored
        self.store.put(inst)
        if start:
            try:
                self.backend.start(spec.name)
            except BackendError as e:
                raise BackendError(f"Server '{name}' was deployed but could not be started: {e}") from e
        return self._info(inst)

    def update(self, server_id: str, name: str | None = None, ports: Iterable[PortMapping] | None = None,
               env: Mapping[str, str] | None = None) -> ServerInfo:
        """Change a stopped server's name, host ports or settings (its container is recreated)."""
        with self._operation(server_id):
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
        self.backend.start(spec.name)

    def _stop(self, inst: Instance) -> None:
        name = container_name(inst.id)
        if self.backend.get_state(name).status == Status.RUNNING:
            self.backend.stop(name)

    def start(self, server_id: str) -> ServerInfo:
        """Start a server (re-creating its container if it is missing)."""
        with self._operation(server_id):
            inst = self._instance(server_id)
            self._start(inst)
            return self._info(inst)

    def stop(self, server_id: str) -> ServerInfo:
        with self._operation(server_id):
            inst = self._instance(server_id)
            self._stop(inst)
            return self._info(inst)

    def restart(self, server_id: str) -> ServerInfo:
        with self._operation(server_id):
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
        with self._operation(server_id):
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
        with self._operation(server_id):
            inst = self._instance(server_id)
            if not confirm:
                raise ValidationError("Restoring replaces the current data and needs explicit confirmation (confirm=true)")
            archive = bk.backup_path(self._backup_dir(inst), name)
            self._require_stopped(inst, "restoring a backup")
            safety = self._safety_backup(inst, "pre-restore", protect={name})  # aborts the restore if it fails
            warnings = bk.restore_archive(archive, self._data_dir_of(inst))
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
        with self._operation(server_id):
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
        with self._operation(server_id):
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
        with self._operation(server_id):
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
        return ConfigFileInfo(c.path, c.label, c.format, c.sensitive_keys, c.hot_edit, exists, size, baks)

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
            note = "This file does not exist yet (the game usually creates it on first start). Saving creates it."
        elif c.hot_edit:
            note = "This file is safe to edit while the server runs, but the game may need a restart to notice."
        else:
            note = "Changes take effect after the server is restarted."
        if running and not c.hot_edit:
            note = "Stop the server to edit this file. " + note
        if r.masked:
            note += f" Values for {', '.join(c.sensitive_keys)} are hidden; leave the placeholder to keep them."
        return ConfigContent(self._config_info(inst, c), r.content, r.masked, running, editable, note)

    def write_config_file(self, server_id: str, path: str, content: str) -> ConfigSaveResult:
        with self._operation(server_id):
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
