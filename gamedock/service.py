"""GameDock service layer.

All management actions live here. The web API calls these functions, and a
future MCP endpoint will call exactly the same ones. This module knows nothing
about HTTP.

Vocabulary:
  catalog entry = a game template (catalog/*.yaml)
  server        = a deployed instance of a catalog entry (stored under data/)
"""
from __future__ import annotations

import logging
import math
import re
import shlex
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from threading import Lock, RLock
from typing import Callable, Iterable, Iterator, Mapping

from . import authoring as au
from . import backups as bk
from . import baselines as bl
from . import configfiles as cfg
from . import configforms as forms
from . import readiness as rd
from . import schedule as sch
from . import steam as stm
from .backends.base import ContainerBackend, DockerDiagnosis
from .catalog import entry_to_dict, load_catalog, parse_definition
from .hoststats import HostMeter
from .hostcheck import foreign_owners
from .authoring_service import AuthoringMixin
from .ops_service import OpsMixin
from .problem_service import ProblemsMixin
from .game_updates import GameUpdateMixin
from .errors import BackendError, BusyError, ConflictError, GameDockError, NotFoundError, ValidationError
from .models import (
    BackupResult, BaselineInfo, CatalogEntry, ConfigContent, ConfigFile, ConfigFileInfo, ConfigForm, ConfigFormResult, ConfigSaveResult,
    ContainerSpec, FormField, LogsResult,
    DataSummary, DeleteResult, EnvValue, HostStats, ImageInfo, Instance, PortMapping, RestoreResult, ServerInfo, ServerStats,
    StatsHistory, StatsPoint, StatsSnapshot, Status, UpdateResult, VolumeMapping, WipeResult,
)
from .store import InstanceStore
from .updates import UpdateChecker

log = logging.getLogger("gamedock.service")

CONTAINER_PREFIX = "gamedock-"
LABEL_MANAGED = "gamedock.managed"
LABEL_SERVER_ID = "gamedock.server_id"
LABEL_GAME_ID = "gamedock.game_id"
MAX_ENV_VALUE_LEN = 4096
BACKUPS_DIRNAME = "backups"          # <data>/backups/<server id>/ ; also reserved so no server uses it
DELETED_DIRNAME = "_deleted"         # <data>/backups/_deleted/ holds backups taken when data was deleted
RESERVED_IDS = {BACKUPS_DIRNAME, bl.DIRNAME}
MAX_HISTORY_POINTS = 2000            # hard cap per series, whatever the interval/history settings
READINESS_LOG_LINES = 400            # how much log is read to find the "ready" line or a download percentage
READINESS_TTL = 2.0                  # seconds a readiness answer is reused (the page polls every 3 s)
MAX_LOG_LINES = 1000                 # most lines one log request may return
DEFAULT_LOG_LINES = 200
MAX_LOG_CHARS = 200_000              # cap on the text of one log answer (the newest part is kept)
MIN_SECRET_LEN = 3                   # secret setting values shorter than this are not searched for in logs
IMAGE_CHECK_TTL = 6 * 3600           # a registry answer is reused this long
IMAGE_CHECK_RETRY = 30 * 60          # ...and a failed question is asked again after this long
IMAGE_CHECK_GAP = 30                 # seconds between two presses of "Check now" for one server
SCHEDULE_TICK_GRACE = 300            # a run more than this many seconds late is called a catch-up run
MAINTENANCE_CATCHUP_SECONDS = 6 * 3600   # a missed maintenance (it restarts the game) is skipped when later than this
BUSY_GIVE_UP_SECONDS = 2 * 3600      # a scheduled run that finds the server busy keeps trying this long, then skips
SAVE_COMMAND_TIMEOUT = 60
SAVE_COMMAND_PAUSE = 3.0             # seconds the game gets to write its files after the save command
ONLY_WHEN_EMPTY_REASON = ("GameDock cannot see how many players are online for this game, so 'only when nobody is "
                          "playing' is not available. Pick a quiet time instead.")


@dataclass
class ImageCheck:
    """The last answer to "does the registry have a newer image than the one this server runs?"."""

    checked_at: float                 # wall clock seconds
    available: bool | None            # None = could not tell
    message: str = ""
    failed: bool = False


def container_name(server_id: str) -> str:
    return f"{CONTAINER_PREFIX}{server_id}"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")


class GameDockService(GameUpdateMixin, AuthoringMixin, OpsMixin, ProblemsMixin):
    def __init__(self, backend: ContainerBackend, catalog_dir: Path, data_dir: Path,
                 backup_retention: int = 10, clock: Callable[[], datetime] | None = None,
                 stats_interval: float = 10.0, stats_history_seconds: int = 3600,
                 port_probe: Callable[[str, int], bool | None] | None = None,
                 tz: tzinfo | None = None, tz_name: str | None = None,
                 updater: UpdateChecker | None = None, steam_info: stm.SteamInfo | None = None,
                 game_update_wait: float = 1200.0, game_update_poll: float = 5.0,
                 game_update_settle: float = 120.0) -> None:
        """`port_probe(protocol, port)` returns False when the host port is taken by another program (checked
        before a server starts); None/True mean "free or unknown". Tests leave it out."""
        self.backend = backend
        self.catalog_dir = Path(catalog_dir)
        self.data_dir = Path(data_dir)
        self.store = InstanceStore(self.data_dir / "instances.json")
        self.backup_retention = max(1, int(backup_retention))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if tz is None and tz_name is None:
            tz, tz_name = sch.host_timezone()
        self.tz, self.tz_name = tz, tz_name or getattr(tz, "key", None) or str(tz)
        self.schedules = sch.ScheduleStore(self.data_dir / "schedules.json")
        self.baselines = bl.BaselineStore(self.data_dir / bl.DIRNAME)
        self._baseline_tried: dict[str, float] = {}       # server id -> monotonic time of the last deploy-time attempt
        self.updater = updater or UpdateChecker(enabled=False, self_update=False)
        self._init_game_updates(steam_info, game_update_wait, game_update_poll, game_update_settle)
        self._init_authoring()
        self._init_problems()
        self.audit_hook: Callable[[str, dict, str, str], None] | None = None  # set by the web app (writes the audit file)
        self._frozen = ""                                  # non-empty while GameDock is about to restart for an update
        self._image_checks: dict[str, ImageCheck] = {}
        self._image_check_lock = Lock()
        self._jobs_running: set[tuple[str, str]] = set()
        self._jobs_lock = Lock()
        self._waiting_since: dict[tuple[str, str], float] = {}
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
            if self._frozen:
                raise BusyError(f"{self._frozen}: please wait a moment and try again")
            lock = self._locks.setdefault(server_id, Lock())
        if not lock.acquire(blocking=False):
            raise BusyError("Another operation is already running on this server; wait for it to finish")
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
        records = self.authoring.records()
        if self.authoring.store.problem:
            problems.append((au.REVIEW_FILENAME, self.authoring.store.problem))
        entries = load_catalog(self.catalog_dir, errors=problems, added_by_ai=records)
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
            stop_timeout=t.stop_timeout,
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
        exit_code, oom_killed = None, False
        try:
            state = self.backend.get_state(container_name(inst.id))
            status, detail = state.status, state.detail
            health, started = state.health, state.started_at
            exit_code, oom_killed = state.exit_code, state.oom_killed
        except BackendError as e:
            status, detail = Status.ERROR, str(e)
        r = self._readiness(inst, t, health, started) if status == Status.RUNNING else None
        if status != Status.RUNNING:
            self._forget_readiness(inst.id)
        elif r is not None and r.ready:
            self._capture_deploy_baselines(inst)
        env = tuple(
            EnvValue(
                name=f.name, label=f.label, secret=f.secret, required=f.required,
                description=f.description,
                value=None if f.secret else inst.env.get(f.name, f.default),
                is_set=bool(inst.env.get(f.name, f.default)),
            )
            for f in t.fields
        )
        problem = self._card_problem(inst, t, status, health, started, exit_code, oom_killed, bool(r and r.ready)) \
            if status != Status.ERROR or oom_killed else {}
        phase_detail = r.detail if r else ""
        if problem and r is not None and r.phase == rd.PHASE_UNHEALTHY:
            phase_detail = f"{problem['title']} {problem['advice']}"
        hints = self._with_current_hints(inst, t)
        chk = self._image_checks.get(inst.id)
        game = self._game_report(inst, hints, status == Status.RUNNING, started) if hints.steam_app_id else None
        return ServerInfo(inst.id, inst.name, inst.game_id, t.name, t.image, t.description,
                          status, inst.ports, env, detail, inst.created_at, t.update_method, len(t.config_files),
                          activity=self._activity.get(inst.id, "") if show_activity else "",
                          phase=r.phase if r else "", ready=bool(r and r.ready),
                          progress=r.progress if r else None, phase_detail=phase_detail,
                          update_available=chk.available if chk and t.update_method != "none" else None,
                          update_checked_at=sch.iso(datetime.fromtimestamp(chk.checked_at, timezone.utc)) or "" if chk else "",
                          update_note=hints.update_note, schedule=self._schedule_overview(inst.id),
                          steam_app_id=hints.steam_app_id,
                          game_version=(game["game_version"] or "") if game else "",
                          installed_build=(game["installed"]["build"] or "") if game else "",
                          latest_build=(game["latest"]["build"] or "") if game else "",
                          game_update_status=game["status"] if game else stm.UNKNOWN,
                          game_update_method=hints.game_update_method, problem=problem)

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

    _HINT_KEYS = ("update_note", "backup_stop_first", "save_command", "steam_app_id", "steam_branch",
                  "steam_manifest_dir", "version_log_pattern", "game_update_method", "installed_build_file")

    def _with_current_hints(self, inst: Instance, t: CatalogEntry) -> CatalogEntry:
        """Servers keep a snapshot of their template. Optional hints (update note, stop-first flag, save command, the
        Steam game version settings) did not exist in snapshots made by older versions; for those, take the current catalog entry's values. Hints
        that the snapshot does have are never overridden."""
        if all(k in inst.template for k in self._HINT_KEYS):
            return t
        try:
            cur = self._current_entries().get(inst.game_id)
        except Exception:  # noqa: BLE001 - a broken catalog must never break the server list
            return t
        if cur is None:
            return t
        return replace(t, **{k: getattr(cur, k) for k in self._HINT_KEYS if k not in inst.template})

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
            self._readiness_text[inst.id] = (now, started, text)   # the problem finder reuses it (no second `docker logs`)
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
                have = ", ".join(f"{c}/{pr}" for (pr, c) in base) or "none"
                raise ValidationError(f"{t.name} has no container port {r.container}/{r.protocol}. Its container ports "
                                      f"are: {have}. Only the host port can be changed, not the container port.")
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
                        f"Host port {p.host}/{p.protocol} is already used by server '{other.name}' (another GameDock "
                        f"server). Two servers cannot share a host port: choose a different host port."
                        + self.free_port_hint(p.protocol, p.host, ignore_id))

    @staticmethod
    def _merge_env(t: CatalogEntry, provided: Mapping[str, str] | None,
                   current: Mapping[str, str] | None = None) -> dict[str, str]:
        """Apply provided values over current (or defaults). Blank secrets keep the existing value."""
        by_name = {f.name: f for f in t.fields}
        values = {f.name: f.default for f in t.fields} if current is None else dict(current)
        for name, value in (provided or {}).items():
            f = by_name.get(name)
            if f is None:
                known = ", ".join(by_name) or "none"
                raise ValidationError(f"Unknown setting '{name}' for {t.name}. The settings of this game are: {known}.")
            value = "" if value is None else str(value)
            if "\n" in value or "\r" in value or "\0" in value or len(value) > MAX_ENV_VALUE_LEN:
                raise ValidationError(f"Invalid value for '{f.label}'")
            if f.secret and value == "" and current is not None:
                continue  # blank on edit = keep what is stored
            if f.pattern and value != "":
                if re.fullmatch(f.pattern, value) is None and re.fullmatch(f.pattern, value.strip()) is not None:
                    value = value.strip()      # a stray space or line break from copy and paste
                if re.fullmatch(f.pattern, value) is None:
                    # Never repeat the value: it may be a password or token.
                    raise ValidationError(f"The setting '{f.label}' ({f.name}) does not look right: "
                                          f"{f.pattern_hint or 'it does not have the format this game expects'}. "
                                          "The value you typed is not shown here; check it and try again.")
            values[name] = value
        for f in t.fields:
            if f.required and not values.get(f.name, "").strip():
                raise ValidationError(f"The setting '{f.label}' ({f.name}) is required: fill it in and try again.")
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
        if self._frozen:
            raise BusyError(f"{self._frozen}: please wait a moment and try again")
        with self._deploy_lock:
            return self._deploy(game_id, name, ports, env, start)

    def _deploy(self, game_id: str, name: str, ports: Iterable[PortMapping] | None,
                env: Mapping[str, str] | None, start: bool) -> ServerInfo:
        t = self.get_catalog_entry(game_id)
        self.require_reviewed(t)
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
            try:
                self.backend.create(spec)  # if this fails nothing is stored
            except BackendError as e:
                if type(e) is not BackendError:
                    raise   # Docker itself is unusable: that message already says what to do
                raise BackendError(f"Could not create the server '{name}' (image {t.image}): {e} Nothing was saved or "
                                   "started, so there is nothing to clean up: fix the cause and deploy again.") from e
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
            new_env = self._merge_env(self._with_current_field_rules(inst, t), env, current=inst.env)
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
                    "whatever uses it (find it with: sudo ss -lntup | grep :" + str(p.host) + ")."
                    + self.free_port_hint(p.protocol, p.host, inst.id))

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
            self.backend.stop(name, self._template(inst).stop_timeout or None)

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

    def _retention_for(self, inst: Instance) -> int:
        """How many backups to keep for this server: its schedule's own number, else GameDock's normal setting."""
        r = self.schedules.get(inst.id).backup.retention
        return r if r else self.backup_retention

    def _safety_backup(self, inst: Instance, kind: str, protect: set[str] | None = None) -> bk.BackupInfo:
        """Back up a stopped server's data into its backups folder (retention applied)."""
        info = bk.create_archive(self._data_dir_of(inst), self._backup_dir(inst), inst.id, self._clock(), kind)
        bk.prune(self._backup_dir(inst), self._retention_for(inst), protect=(protect or set()) | {info.name})
        return info

    def create_backup(self, server_id: str, live: bool = False) -> BackupResult:
        """Back up a server's data folder.

        Safe by default: a running server is stopped, backed up, and started again so the files are
        consistent. With live=True a running server is backed up as it runs (files may be mid-write).
        """
        with self._operation(server_id, "backing up"):
            return self._backup_locked(self._instance(server_id), live)

    def _run_save_command(self, inst: Instance, t: CatalogEntry) -> str:
        """Ask a running game to save (the catalog's optional save_command) before a live copy. A failure is only
        reported: the backup still goes ahead. Returns a plain sentence about it, or ''."""
        if not t.save_command:
            return ""
        try:
            self.backend.exec_in(container_name(inst.id), list(t.save_command), SAVE_COMMAND_TIMEOUT)
        except GameDockError as e:
            return f"The game's save command did not work ({e}); the backup was taken anyway."
        if SAVE_COMMAND_PAUSE:
            time.sleep(SAVE_COMMAND_PAUSE)
        return "The game was asked to save first."

    def _backup_locked(self, inst: Instance, live: bool, kind: str | None = None) -> BackupResult:
        """The backup itself; the caller holds the server's operation lock."""
        t = self._with_current_hints(inst, self._template(inst))
        was_running = self._is_running(inst)
        stop_first = was_running and not live
        save_note = ""
        if was_running and live:
            save_note = self._run_save_command(inst, t)
        if stop_first:
            self._stop(inst)
        try:
            info = bk.create_archive(self._data_dir_of(inst), self._backup_dir(inst), inst.id, self._clock(), kind)
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
        keep = self._retention_for(inst)
        removed = bk.prune(self._backup_dir(inst), keep, protect={info.name})
        if not was_running:
            note = "The server was stopped, so the backup is consistent."
        elif stop_first:
            note = ("The server was running, so it was stopped for a consistent backup"
                    + (" and has been started again." if restarted else "."))
        else:
            note = ("The server was running and was NOT stopped (live backup). Files being written during "
                    "the backup may be inconsistent; prefer a stopped backup for anything important.")
            if save_note:
                note += " " + save_note
        if removed:
            note += f" Removed {len(removed)} old backup(s) to keep the newest {keep}."
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
            self.baselines.remove_server(inst.id)  # the game writes new files (and new defaults) on its next start
            self._baseline_tried.pop(inst.id, None)
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
                self.baselines.remove_server(inst.id)    # the recorded defaults belong to those files
            self._baseline_tried.pop(inst.id, None)
            self.store.remove(inst.id)
            self.schedules.remove(inst.id)
            self._image_checks.pop(inst.id, None)
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
            result = self._update_image_locked(inst, t)
            self._image_checks[inst.id] = ImageCheck(time.time(), False, "Up to date.")
            return result

    def _update_image_locked(self, inst: Instance, t: CatalogEntry) -> UpdateResult:
        """The image update itself; the caller holds the server's operation lock."""
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

    # ------------------------------------------------------------ recorded defaults (baselines)
    BASELINE_DEPLOY_WINDOW = 6 * 3600     # seconds after deploy in which a ready server's files count as "its defaults"
    BASELINE_DEPLOY_RETRY = 20.0          # seconds between two looks for settings files that do not exist yet

    def _capture_baseline(self, inst: Instance, c: ConfigFile, r: cfg.ConfigRead | None, origin: str) -> bool:
        """Record the file's current settings as its defaults if there is no baseline yet. Never overwrites. Returns
        True if one was written. Never raises: the defaults are a convenience and must not break reading or saving."""
        try:
            if self.baselines.load(inst.id, c.path) is not None:
                return False
            root = self._data_dir_of(inst)
            if r is None:
                r = cfg.read_config(root, c)
            if not r.exists:
                return False
            form = forms.build_form(r.content, c)
            if form is None or not form.fields:  # not a settings file (yet): an empty list is no baseline
                return False
            st = cfg.safe_path(root, c.path).stat()
            snap = bl.make_snapshot(form, c.sensitive_keys, path=c.path, captured_at=sch.iso(self._clock()) or "",
                                    origin=origin, source_mtime=st.st_mtime, source_size=st.st_size)
            return self.baselines.create_if_absent(inst.id, c.path, snap)
        except Exception:  # noqa: BLE001
            log.warning("could not record the defaults of %s for %s", c.path, inst.id, exc_info=True)
            return False

    def _capture_deploy_baselines(self, inst: Instance) -> None:
        """Called when a running server is ready: a freshly deployed server's files are its defaults. Servers that
        already existed before this was added get theirs when someone first opens or saves the file instead."""
        now = time.monotonic()
        if now - self._baseline_tried.get(inst.id, -1e9) < self.BASELINE_DEPLOY_RETRY:
            return
        self._baseline_tried[inst.id] = now
        try:
            created = datetime.fromisoformat((inst.created_at or "").replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created).total_seconds() > self.BASELINE_DEPLOY_WINDOW:
                self._baseline_tried[inst.id] = now + 10 ** 9  # an older server: do not look again
                return
            files = self._template(inst).config_files
        except Exception:  # noqa: BLE001
            return
        missing = 0
        for c in files:
            if self.baselines.load(inst.id, c.path) is None and not self._capture_baseline(inst, c, None, bl.ORIGIN_DEPLOY):
                missing += 1
        if not missing:
            self._baseline_tried[inst.id] = now + 10 ** 9  # everything is recorded

    def _baseline_info(self, inst: Instance, c: ConfigFile, snap: dict | None, just: bool) -> BaselineInfo:
        if snap is None:
            return BaselineInfo(False)
        return BaselineInfo(True, snap.get("captured_at", ""), snap.get("origin", ""), just,
                            self.baselines.has_previous(inst.id, c.path))

    def _baseline_for(self, inst: Instance, c: ConfigFile, r: cfg.ConfigRead | None = None,
                      origin: str = bl.ORIGIN_FIRST_SEEN) -> tuple[dict | None, bool]:
        """(baseline, recorded just now). Takes the baseline from the file on disk if there is none yet."""
        snap = self.baselines.load(inst.id, c.path)
        if snap is not None:
            return snap, False
        if not self._capture_baseline(inst, c, r, origin):
            return None, False
        return self.baselines.load(inst.id, c.path), True

    def _compared_form(self, inst: Instance, c: ConfigFile, form: ConfigForm | None, snap: dict | None) -> ConfigForm | None:
        if form is None or snap is None:
            return form
        try:
            return bl.compare(form, snap)
        except Exception:  # noqa: BLE001 - a damaged baseline must never hide the settings
            log.warning("could not compare %s of %s with its defaults", c.path, inst.id, exc_info=True)
            return form

    def record_config_baseline(self, server_id: str, path: str, confirm: bool = False) -> ConfigContent:
        """Deliberately make the settings as they are now the recorded defaults. The old baseline is kept once as
        <name>.prev.json. Only GameDock's own record changes, never the game's file."""
        inst = self._instance(server_id)
        c = self._config_file(inst, path)
        if not confirm:
            raise ValidationError("Recording new defaults needs explicit confirmation (confirm=true)")
        r = cfg.read_config(self._data_dir_of(inst), c)
        if not r.exists:
            raise ValidationError("This game has not created its settings file yet; there is nothing to record.")
        form = forms.build_form(r.content, c)
        if form is None or not form.fields:
            raise ValidationError("This file has no settings form, so no defaults can be recorded for it.")
        st = cfg.safe_path(self._data_dir_of(inst), c.path).stat()
        snap = bl.make_snapshot(form, c.sensitive_keys, path=c.path, captured_at=sch.iso(self._clock()) or "",
                                origin=bl.ORIGIN_MANUAL, source_mtime=st.st_mtime, source_size=st.st_size)
        self.baselines.save(inst.id, c.path, snap, keep_previous=True)
        return self.read_config_file(server_id, path)

    def config_changes(self, server_id: str, path: str) -> dict:
        """The settings of a file that differ from the recorded defaults, with default and current value. Values of
        secret settings are never included, only "set" / "not set"."""
        inst = self._instance(server_id)
        c = self._config_file(inst, path)
        r = cfg.read_config(self._data_dir_of(inst), c)
        out: dict = {"server_id": inst.id, "path": c.path, "file_exists": r.exists, "baseline": None, "count": 0,
                     "changes": [], "note": ""}
        if not r.exists:
            out["note"] = "This game has not created its settings file yet."
            return out
        form = forms.build_form(r.content, c)
        if form is None or not form.fields:
            out["note"] = "This file has no structured settings view, so settings cannot be compared with defaults."
            return out
        snap, just = self._baseline_for(inst, c, r)
        info = self._baseline_info(inst, c, snap, just)
        out["baseline"] = {"available": info.available, "captured_at": info.captured_at, "origin": info.origin,
                           "just_recorded": info.just_recorded}
        if snap is None:
            out["note"] = "No defaults could be recorded for this file."
            return out
        cmp = bl.compare(form, snap)

        def shown(secret: bool, value: str | None, is_set: bool | None) -> str | None:
            if secret:
                return "set" if is_set else "not set"
            return value

        changes = []
        for f in cmp.fields:
            if not f.differs:
                continue
            changes.append({"id": f.id, "key": f.key, "label": f.label, "group": f.group, "status": f.status,
                            "sensitive": f.sensitive,
                            "default": shown(f.sensitive, f.default, f.default_set) if f.status != "new" else None,
                            "current": shown(f.sensitive, f.value, f.value != "")})
        for m in cmp.missing:
            changes.append({"id": m.id, "key": m.key, "label": m.label, "group": m.group, "status": "missing",
                            "sensitive": m.sensitive, "default": shown(m.sensitive, m.default, m.default_set),
                            "current": None})
        out["changes"], out["count"] = changes, len(changes)
        if info.just_recorded:
            out["note"] = ("The defaults were recorded just now, from the file as it is, so nothing can differ and "
                           "earlier changes cannot be shown.")
        elif info.origin in (bl.ORIGIN_FIRST_SEEN, bl.ORIGIN_PRE_SAVE):
            out["note"] = (f"The defaults were recorded at {info.captured_at} when GameDock first looked at this file, "
                           "so they already include any change made before then.")
        elif info.origin == bl.ORIGIN_MANUAL:
            out["note"] = f"The defaults were recorded by hand at {info.captured_at}."
        return out

    # ------------------------------------------------------------ config files (read, form, write)
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
        baseline = BaselineInfo(False)
        if r.exists:
            try:
                form = forms.build_form(r.content, c)
            except Exception:  # the structured view is a convenience; the raw text must always be readable
                form = None
            if form is not None:
                snap, just = self._baseline_for(inst, c, r)
                baseline = self._baseline_info(inst, c, snap, just)
                form = self._compared_form(inst, c, form, snap)
        return ConfigContent(self._config_info(inst, c), r.content, r.masked, running, editable, note, form, baseline)

    def config_form(self, server_id: str, path: str, content: str | None = None,
                    changes: dict[str, str] | None = None) -> ConfigFormResult:
        """Structured view helper (read-only for the game's files). Parses `content` (the masked text the editor
        holds; default: the file as it is on disk), applies `changes` ({field id: new value}) to it and returns the
        fields plus the resulting text. Used to switch between the form and the raw text, and to prepare a save.
        The fields carry how they compare with the recorded defaults (the defaults are recorded from the file on disk
        if that has not happened yet, never from `content`)."""
        inst = self._instance(server_id)
        c = self._config_file(inst, path)
        r = None
        if content is None:
            r = cfg.read_config(self._data_dir_of(inst), c)
            if not r.exists:
                raise ValidationError("This game has not created its settings file yet; it appears after the first full start.")
            content = r.content
        if len(content.encode("utf-8")) > cfg.MAX_CONFIG_BYTES:
            raise ValidationError(f"Content is larger than the editor limit of {cfg.MAX_CONFIG_BYTES} bytes")
        text, changed = forms.apply_changes(content, c, changes) if changes else (content, [])
        form = forms.build_form(text, c)
        baseline = BaselineInfo(False)
        if form is not None:
            snap, just = self._baseline_for(inst, c, r)
            baseline = self._baseline_info(inst, c, snap, just)
            form = self._compared_form(inst, c, form, snap)
        return ConfigFormResult(form, text, tuple(changed), baseline)

    def write_config_file(self, server_id: str, path: str, content: str) -> ConfigSaveResult:
        with self._operation(server_id, "saving config"):
            inst = self._instance(server_id)
            c = self._config_file(inst, path)
            running = self._is_running(inst)
            if running and not c.hot_edit:
                raise ConflictError("Stop the server before editing this file (it is not marked safe to edit while running)")
            self._capture_baseline(inst, c, None, bl.ORIGIN_PRE_SAVE)  # the file as it is before our first edit
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

    # ------------------------------------------------------------ freezing (self-update)
    def freeze(self, reason: str) -> None:
        """Refuse new actions and scheduled runs, but only if nothing is running right now (raises BusyError with the
        list of what is). Used by the GameDock self-update just before it restarts GameDock."""
        with self._locks_guard:
            work = self._busy_work_unlocked()
            if work:
                raise BusyError("GameDock cannot update while it is busy (" + "; ".join(work) + "). "
                                "Wait for that to finish and try again.")
            self._frozen = reason

    def unfreeze(self) -> None:
        with self._locks_guard:
            self._frozen = ""

    def _busy_work_unlocked(self) -> list[str]:
        work = [f"{sid}: {self._activity.get(sid) or 'working'}" for sid, lk in self._locks.items() if lk.locked()]
        work += [f"{sid}: deploying" for sid in self._pending if not any(w.startswith(sid + ":") for w in work)]
        with self._jobs_lock:
            work += [f"{sid}: scheduled {kind}" for sid, kind in sorted(self._jobs_running)
                     if not any(w.startswith(sid + ":") for w in work)]
        return work

    def busy_work(self) -> list[str]:
        with self._locks_guard:
            return self._busy_work_unlocked()

    # ------------------------------------------------------------ image update check
    def get_image_update_status(self, server_id: str) -> dict:
        """What is known about a newer image, without asking the registry (cached answer, may be empty)."""
        inst = self._instance(server_id)
        return self._image_status(inst, self._image_checks.get(inst.id))

    def _image_status(self, inst: Instance, chk: ImageCheck | None, cached: bool = True) -> dict:
        t = self._with_current_hints(inst, self._template(inst))
        if t.update_method == "none":
            return {"server_id": inst.id, "supported": False, "update_available": None, "checked_at": None,
                    "message": f"{t.name} updates its game files inside the container, so there is no image update to "
                               "check for.", "update_note": t.update_note, "cached": cached}
        return {"server_id": inst.id, "supported": True,
                "update_available": chk.available if chk else None,
                "checked_at": sch.iso(datetime.fromtimestamp(chk.checked_at, timezone.utc)) if chk else None,
                "message": chk.message if chk else "Not checked yet.", "failed": bool(chk and chk.failed),
                "update_note": t.update_note, "cached": cached}

    def check_image_update(self, server_id: str, force: bool = False) -> dict:
        """Ask the registry whether the image has a newer version than the one this server's container was made from.
        Nothing is downloaded. The answer is kept for hours; `force` (the Check now button) asks again, but not more
        than once every 30 seconds per server. Never raises for network trouble: the answer says it could not tell."""
        inst = self._instance(server_id)
        t = self._template(inst)
        if t.update_method == "none":
            return self._image_status(inst, None, cached=False)
        with self._image_check_lock:
            chk = self._image_checks.get(inst.id)
            now = time.time()
            if chk:
                age = now - chk.checked_at
                if age < (IMAGE_CHECK_GAP if force else (IMAGE_CHECK_RETRY if chk.failed else IMAGE_CHECK_TTL)):
                    return self._image_status(inst, chk, cached=True)
            chk = self._ask_registry(inst, t, now)
            self._image_checks[inst.id] = chk
            return self._image_status(inst, chk, cached=False)

    def _ask_registry(self, inst: Instance, t: CatalogEntry, now: float) -> ImageCheck:
        try:
            remote = self.backend.remote_image_digest(t.image)
        except BackendError as e:
            return ImageCheck(now, None, str(e), failed=True)
        if not remote:
            return ImageCheck(now, None, "The registry did not report a version for this image, so GameDock cannot tell.")
        try:
            ref = self.backend.get_container_image_id(container_name(inst.id)) or t.image
            local = self.backend.local_image_digests(ref)
        except BackendError as e:
            return ImageCheck(now, None, str(e), failed=True)
        if not local:
            return ImageCheck(now, None, "The image on this machine has no registry record (for example it was built "
                                         "here), so GameDock cannot compare it.")
        if remote in local:
            return ImageCheck(now, False, "This server runs the newest image.")
        return ImageCheck(now, True, "A newer image is available in the registry.")

    def check_image_updates_due(self, limit: int = 1) -> int:
        """Background helper: ask the registry about at most `limit` servers whose answer is missing or old."""
        done = 0
        for inst in self.store.all():
            if done >= limit:
                break
            try:
                if self._template(inst).update_method == "none":
                    continue
                chk = self._image_checks.get(inst.id)
                wait = IMAGE_CHECK_RETRY if chk and chk.failed else IMAGE_CHECK_TTL
                if chk and time.time() - chk.checked_at < wait:
                    continue
                self.check_image_update(inst.id)
                done += 1
            except GameDockError:
                continue
        return done

    # ------------------------------------------------------------ schedules (view and change)
    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def _next_of(self, rule: sch.Recurrence, enabled: bool, state: sch.RunState) -> datetime | None:
        return sch.next_due(rule, state, self.tz) if enabled else None

    def _schedule_overview(self, server_id: str) -> dict:
        s = self.schedules.get(server_id)
        nb = self._next_of(s.backup.rule, s.backup.enabled, s.backup.state)
        nm = self._next_of(s.maintenance.rule, s.maintenance.enabled, s.maintenance.state)
        nxt, kind = (nb, "backup") if nb and (not nm or nb <= nm) else ((nm, "maintenance") if nm else (None, ""))
        return {"backup": sch.describe(s.backup.rule) if s.backup.enabled else "",
                "maintenance": sch.describe(s.maintenance.rule) if s.maintenance.enabled else "",
                "next_run": sch.iso(nxt), "next_kind": kind,
                "last_problem": bool(s.history and s.history[0].result == "failed")}

    def timezone_info(self) -> dict:
        off = self._now().astimezone(self.tz) if self.tz else self._now().astimezone()
        delta = off.utcoffset() or timedelta(0)
        mins = int(delta.total_seconds() // 60)
        sign = "+" if mins >= 0 else "-"
        return {"name": self.tz_name, "utc_offset": f"UTC{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"}

    def _rule_view(self, rule: sch.Recurrence) -> dict:
        return {"frequency": rule.kind, "every_hours": rule.every_hours or None,
                "time": rule.time or None, "days": list(rule.days)}

    def get_server_schedules(self, server_id: str) -> dict:
        inst = self._instance(server_id)
        t = self._with_current_hints(inst, self._template(inst))
        s = self.schedules.get(inst.id)
        b, m = s.backup, s.maintenance

        def run_info(state: sch.RunState, nxt: datetime | None) -> dict:
            return {"last_run": state.last_run or None, "last_result": state.last_result or None,
                    "last_message": state.last_message, "next_run": sch.iso(nxt)}

        return {
            "server_id": inst.id, "server_name": inst.name, "timezone": self.timezone_info(),
            "backup": {
                "enabled": b.enabled, **self._rule_view(b.rule), "retention": b.retention,
                "retention_in_use": b.retention or self.backup_retention,
                "summary": sch.describe(b.rule) if b.enabled else "off",
                **run_info(b.state, self._next_of(b.rule, b.enabled, b.state)),
                "how": ("The game is stopped briefly for each scheduled backup." if t.backup_stop_first else
                        "A running server is copied while it runs (players stay connected)"
                        + (", after asking the game to save." if t.save_command else ".")),
            },
            "maintenance": {
                "enabled": m.enabled, **self._rule_view(m.rule), "backup_first": m.backup_first,
                "update_image": m.update_image, "restart": m.restart,
                "summary": sch.describe(m.rule) if m.enabled else "off",
                "image_update_supported": t.update_method != "none",
                "game_update_supported": bool(t.steam_app_id) and t.game_update_method == "restart",
                "only_when_empty": {"available": False, "reason": ONLY_WHEN_EMPTY_REASON},
                **run_info(m.state, self._next_of(m.rule, m.enabled, m.state)),
            },
            "history": [h.__dict__ for h in s.history],
            "overview": self._schedule_overview(inst.id),
        }

    def list_schedules(self) -> dict:
        return {"timezone": self.timezone_info(), "problem": self.schedules.problem,
                "servers": [self.get_server_schedules(i.id) for i in self.store.all()]}

    def set_backup_schedule(self, server_id: str, enabled: bool, frequency: str | None = None,
                            every_hours: int | None = None, time_of_day: str | None = None,
                            days: list[str] | None = None, retention: int | None = None) -> dict:
        """Create or change a server's backup schedule. Switching it off keeps the settings for later. Does not run
        anything now and cannot lose data, so it is allowed to AI agents in standard mode."""
        inst = self._instance(server_id)
        if not isinstance(enabled, bool):
            raise ValidationError("'enabled' must be true or false")
        current = self.schedules.get(inst.id).backup
        if enabled and frequency is None and not current.state.since:
            raise ValidationError("Choose how often to back up: frequency 'interval' (every N hours), 'daily' or 'weekly'.")
        if frequency is None and enabled:
            rule, keep = current.rule, current.retention if retention is None else sch.validate_retention(retention)
        elif frequency is None:   # switching off, nothing else changes
            rule, keep = current.rule, current.retention
        else:
            rule = sch.validate_recurrence(frequency, every_hours, time_of_day, days, sch.BACKUP_KINDS)
            keep = sch.validate_retention(retention)
        now = sch.iso(self._now()) or ""

        def change(s: sch.ServerSchedules) -> sch.ServerSchedules:
            b = s.backup
            restart_clock = enabled and (not b.enabled or rule != b.rule or not b.state.since)
            state = replace(b.state, since=now) if restart_clock else b.state
            return replace(s, backup=sch.BackupSchedule(enabled, rule, keep, state))

        self.schedules.update(inst.id, change)
        return self.get_server_schedules(inst.id)

    def set_maintenance_schedule(self, server_id: str, enabled: bool, frequency: str | None = None,
                                 time_of_day: str | None = None, days: list[str] | None = None,
                                 backup_first: bool = True, update_image: bool = True, restart: bool = True) -> dict:
        """Create or change a server's maintenance schedule (back up, apply an image update, restart). Switching it off
        keeps the settings. Only the schedule changes; nothing runs now."""
        inst = self._instance(server_id)
        t = self._with_current_hints(inst, self._template(inst))
        if not isinstance(enabled, bool):
            raise ValidationError("'enabled' must be true or false")
        for name, v in (("backup_first", backup_first), ("update_image", update_image), ("restart", restart)):
            if not isinstance(v, bool):
                raise ValidationError(f"'{name}' must be true or false")
        current = self.schedules.get(inst.id).maintenance
        if enabled and frequency is None and not current.state.since:
            raise ValidationError("Choose when to run maintenance: frequency 'daily' or 'weekly' with a time.")
        if frequency is None:
            # no new frequency: keep the stored rule and options (switching on or off, or reusing the old settings)
            rule = current.rule
            backup_first, update_image, restart = current.backup_first, current.update_image, current.restart
        else:
            rule = sch.validate_recurrence(frequency, None, time_of_day, days, sch.MAINTENANCE_KINDS)
        if enabled:
            if not (backup_first or update_image or restart):
                raise ValidationError("Choose at least one thing for maintenance to do: back up, update the image or restart.")
            if update_image and t.update_method == "none" and not (t.steam_app_id and t.game_update_method == "restart"):
                raise ValidationError(f"{t.name} updates its game files inside the container, so there is no image update "
                                      "to apply. Turn 'apply updates' off (restarting is how this game updates).")
        now = sch.iso(self._now()) or ""

        def change(s: sch.ServerSchedules) -> sch.ServerSchedules:
            m = s.maintenance
            restart_clock = enabled and (not m.enabled or rule != m.rule or not m.state.since)
            state = replace(m.state, since=now) if restart_clock else m.state
            return replace(s, maintenance=sch.MaintenanceSchedule(enabled, rule, backup_first, update_image, restart, state))

        self.schedules.update(inst.id, change)
        return self.get_server_schedules(inst.id)

    # ------------------------------------------------------------ schedules (running)
    def _audit(self, tool: str, args: dict, status: str, detail: str) -> None:
        hook = self.audit_hook
        if hook:
            try:
                hook(tool, args, status, detail)
            except Exception:  # noqa: BLE001 - auditing must never break a job
                log.exception("could not write the audit entry for %s", tool)

    def _record_run(self, server_id: str, kind: str, trigger: str, result: str, message: str, started: datetime) -> None:
        """Remember how a run went: state of the schedule, the history list, the log and the audit file."""
        message = " ".join(message.split())[:500]
        entry = sch.HistoryEntry(sch.iso(started) or "", kind, trigger, result, message)

        def change(s: sch.ServerSchedules) -> sch.ServerSchedules:
            state_of = s.backup if kind == "backup" else s.maintenance
            st = replace(state_of.state, last_run=sch.iso(started) or "", last_result=result, last_message=message)
            s = replace(s, **{kind: replace(state_of, state=st)})
            return sch.with_history(s, entry)

        self.schedules.update(server_id, change)
        (log.error if result == "failed" else log.info)("Scheduled %s of '%s' (%s): %s - %s", kind, server_id, trigger, result, message)
        self._audit(f"scheduled_{kind}" if trigger != "manual" else f"{kind}_run_now",
                    {"server_id": server_id, "trigger": trigger}, "ok" if result in ("ok", "skipped") else "error", message)

    def run_schedule_now(self, server_id: str, kind: str, confirm: bool = False) -> dict:
        """Run a server's scheduled backup or maintenance right now (the "Run now" button). Needs confirm=true because
        maintenance restarts the game. Uses the settings stored for the schedule, switched on or not."""
        inst = self._instance(server_id)
        if kind not in ("backup", "maintenance"):
            raise ValidationError("kind must be 'backup' or 'maintenance'")
        if not confirm:
            raise ValidationError("Running it now needs explicit confirmation (confirm=true)")
        if kind == "maintenance":
            m = self.schedules.get(inst.id).maintenance
            if not (m.backup_first or m.update_image or m.restart):
                raise ValidationError("Maintenance has nothing to do: choose back up, update the image or restart first.")
        result, message = self._execute(inst, kind, "manual", self._now(), raise_failures=True)
        return {"server_id": inst.id, "kind": kind, "result": result, "message": message}

    def _execute(self, inst: Instance, kind: str, trigger: str, started: datetime, raise_failures: bool = False,
                 ) -> tuple[str, str]:
        """Run one job. A server that is busy raises BusyError (nothing is recorded). Anything else is recorded."""
        try:
            if kind == "backup":
                result, message = self._job_backup(inst)
            else:
                result, message = self._job_maintenance(inst)
        except BusyError:
            raise
        except GameDockError as e:
            self._record_run(inst.id, kind, trigger, "failed", str(e), started)
            if raise_failures:
                raise
            return "failed", str(e)
        except Exception as e:  # noqa: BLE001 - a scheduled job must never kill the scheduler
            log.exception("scheduled %s of '%s' crashed", kind, inst.id)
            msg = f"Unexpected error: {e}"
            self._record_run(inst.id, kind, trigger, "failed", msg, started)
            if raise_failures:
                raise
            return "failed", msg
        self._record_run(inst.id, kind, trigger, result, message, started)
        return result, message

    def _job_backup(self, inst: Instance) -> tuple[str, str]:
        with self._operation(inst.id, "backing up"):
            t = self._with_current_hints(inst, self._template(inst))
            files, _, _ = bk.folder_usage(self._data_dir_of(inst))
            if files == 0:
                return "skipped", "Nothing to back up yet: the server's data folder has no files."
            r = self._backup_locked(inst, live=not t.backup_stop_first, kind="scheduled")
            extra = (" " + " ".join(r.warnings)) if r.warnings else ""
            return ("failed" if r.warnings else "ok"), f"Saved backup {r.backup.name}. {r.note}{extra}"

    def _job_maintenance(self, inst: Instance) -> tuple[str, str]:
        with self._operation(inst.id, "maintenance"):
            t = self._with_current_hints(inst, self._template(inst))
            m = self.schedules.get(inst.id).maintenance
            cname = container_name(inst.id)
            was_running = self._is_running(inst)
            notes: list[str] = []
            problems: list[str] = []
            changed = False
            if m.update_image and t.update_method != "none":
                try:
                    before = self.backend.get_container_image_id(cname)
                    self.backend.pull_image(t.image)
                    newest = self.backend.get_image_id(t.image)
                    changed = before is not None and newest is not None and before != newest
                    notes.append("A newer image was downloaded." if changed else "The image was already the newest.")
                except GameDockError as e:
                    problems.append(f"The image update could not be checked ({e}); the other steps were still done.")
            # The game itself (SteamCMD games): "apply updates" asks whether a newer game version exists and, if so, the
            # restart below is what installs it (the image downloads new game files when the container starts).
            game_check = bool(m.update_image and t.steam_app_id and t.game_update_method == "restart")
            game_needed, game_before = False, None
            if game_check:
                latest = self.steam.get(t.steam_app_id, t.steam_branch or "public", force=True)
                installed, why = self._read_game_installed(inst, t, fresh=True)
                game_before = installed.build if installed else None
                gstatus, gtext = stm.compute_status(True, installed, latest, why)
                game_needed = gstatus == stm.UPDATE_AVAILABLE
                if game_needed:
                    notes.append(f"A newer game version is available (build {game_before} -> {latest.build}).")
                elif gstatus == stm.UP_TO_DATE:
                    notes.append(f"The game is up to date (build {game_before}).")
                else:
                    notes.append(f"Could not check for a game update ({gtext.rstrip('.')}).")
                if game_needed and not was_running:
                    notes.append("The server is stopped; the game will update the next time it starts.")
            game_needed = game_needed and was_running
            will_stop = was_running and (m.restart or changed or game_needed)
            stopped = False
            backed_up = False
            try:
                if m.backup_first and was_running and not will_stop:
                    r = self._backup_locked(inst, live=not t.backup_stop_first, kind="pre-maintenance")
                    backed_up = True
                    notes.append(f"Backed up ({r.backup.name}).")
                    problems.extend(r.warnings)
                if will_stop:
                    self._stop(inst)
                    stopped = True
                if (m.backup_first or changed or game_needed) and not backed_up:
                    if self._is_running(inst):
                        raise ConflictError("The server is still running, so a consistent backup cannot be made.")
                    files, _, _ = bk.folder_usage(self._data_dir_of(inst))
                    if files:
                        info = self._safety_backup(inst, "pre-maintenance" if m.backup_first else "pre-update")
                        notes.append(f"Backed up ({info.name}).")
                    else:
                        notes.append("No files to back up yet.")
                if changed:
                    self.backend.remove(cname)
                    self.backend.create(self._spec(inst))
                    notes.append("The server now uses the new image.")
            except GameDockError as e:
                problems.append(f"{e} Maintenance stopped before changing anything further.")
                changed = False
                game_needed = False
            if stopped:
                try:
                    self._start(inst)
                    notes.append("The server was restarted.")
                except GameDockError as e:
                    problems.append(f"The server could not be started again: {e}")
                    game_needed = False
                if game_needed:
                    outcome, after = self._wait_for_game_build(inst, t, game_before)
                    sentence, is_problem = self._describe_outcome(outcome, game_before, after, None)
                    (problems if is_problem else notes).append(sentence)
            elif m.restart and not was_running:
                notes.append("The server was not running, so it was left stopped.")
            if m.update_image and t.update_method != "none":
                self._image_checks.pop(inst.id, None)  # the badge is worked out afresh by the next check
            msg = " ".join([*notes, *problems]) or "Nothing to do."
            return ("failed" if problems else "ok"), msg

    def due_jobs(self, now: datetime | None = None) -> list[tuple[str, str, datetime]]:
        """(server id, kind, the time it was due) for every enabled schedule that is due at `now`."""
        now = now or self._now()
        out = []
        ids = {i.id for i in self.store.all()}
        for sid, s in self.schedules.all().items():
            if sid not in ids:
                continue
            for kind, sched in (("backup", s.backup), ("maintenance", s.maintenance)):
                if not sched.enabled:
                    continue
                due = sch.next_due(sched.rule, sched.state, self.tz)
                if due and due <= now:
                    out.append((sid, kind, due))
        return out

    def process_schedules(self, now: datetime | None = None, spawn: Callable[[Callable[[], None]], None] | None = None) -> int:
        """One scheduler tick: start every due job (each in its own thread unless `spawn` says otherwise). A job whose
        server is busy waits and is tried again at the next tick. Returns how many jobs were started."""
        now = now or self._now()
        if self._frozen:
            return 0
        spawn = spawn or (lambda fn: threading.Thread(target=fn, name="gamedock-job", daemon=True).start())
        started = 0
        for sid, kind, due in self.due_jobs(now):
            key = (sid, kind)
            with self._jobs_lock:
                if key in self._jobs_running:
                    continue
                self._jobs_running.add(key)
            started += 1
            spawn(lambda sid=sid, kind=kind, due=due: self._scheduled_job(sid, kind, due, now))
        return started

    def _scheduled_job(self, sid: str, kind: str, due: datetime, now: datetime) -> None:
        key = (sid, kind)
        try:
            inst = self.store.get(sid)
            if inst is None:
                return
            late = (now - due).total_seconds()
            if kind == "maintenance" and late > MAINTENANCE_CATCHUP_SECONDS:
                hours = int(late // 3600)
                self._waiting_since.pop(key, None)
                self._record_run(sid, kind, "catch-up", "skipped",
                                 f"Maintenance was due {hours} hours ago while GameDock was not running. It was skipped "
                                 "so the game is not restarted at an unexpected time. The next one runs as scheduled.", now)
                return
            trigger = "catch-up" if late > SCHEDULE_TICK_GRACE else "schedule"
            try:
                self._execute(inst, kind, trigger, now)
                self._waiting_since.pop(key, None)
            except BusyError:
                first = self._waiting_since.setdefault(key, time.monotonic())
                if time.monotonic() - first > BUSY_GIVE_UP_SECONDS:
                    self._waiting_since.pop(key, None)
                    self._record_run(sid, kind, trigger, "skipped",
                                     "The server stayed busy for two hours, so this run was skipped.", now)
                else:
                    log.info("Scheduled %s of '%s' waits: the server is busy", kind, sid)
        except Exception:  # noqa: BLE001
            log.exception("scheduled %s of '%s' failed", kind, sid)
        finally:
            with self._jobs_lock:
                self._jobs_running.discard(key)
