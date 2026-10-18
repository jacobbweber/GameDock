"""Plain data types shared by the service, backends, web and (later) MCP layers.

Two ideas are kept apart:
  * CatalogEntry - a game *template* from catalog/*.yaml (what can be deployed)
  * Instance     - a *deployed server* made from a template (stored under data/)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
if TYPE_CHECKING:
    from .backups import BackupInfo

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")  # name[:tag][@digest], may start with a registry host
# Setting values are handed to the docker command through its own environment, so names that would change how
# the docker command itself behaves (where it looks for programs, which daemon it talks to...) are not allowed.
RESERVED_ENV_RE = re.compile(
    r"^(PATH|HOME|USER|LOGNAME|SHELL|TMPDIR|PWD|XDG_[A-Z_]+|SSH_[A-Z_]+|DOCKER_[A-Z_]+|LD_[A-Z_]+|DYLD_[A-Z_]+|"
    r"(HTTP|HTTPS|ALL|NO)_PROXY|(http|https|all|no)_proxy)$")


def is_reserved_env_name(name: str) -> bool:
    return bool(RESERVED_ENV_RE.match(name))


class Status(str, Enum):
    NOT_CREATED = "not_created"  # instance is stored but has no container
    STOPPED = "stopped"
    RUNNING = "running"
    ERROR = "error"


@dataclass(frozen=True)
class PortMapping:
    protocol: str  # "tcp" or "udp"
    host: int
    container: int


@dataclass(frozen=True)
class VolumeMapping:
    host: str  # relative paths are placed under <data_dir>/<instance id>/
    container: str


@dataclass(frozen=True)
class EnvField:
    """An environment variable the user may set when deploying/editing."""

    name: str
    label: str = ""
    default: str = ""
    secret: bool = False  # rendered as a password input; never returned by the API or logged
    required: bool = False
    description: str = ""


CONFIG_FORMATS = ("ini", "json", "yaml", "text")
UPDATE_METHODS = ("pull_recreate", "none")


@dataclass(frozen=True)
class ConfigFile:
    """A config file a catalog entry lets users edit. `path` is relative to the server's data folder."""

    path: str
    label: str = ""
    format: str = "text"  # hint for the editor; "json" files are validated on save
    sensitive_keys: tuple[str, ...] = ()  # values of these keys are masked in the editor
    hot_edit: bool = False  # True = safe to edit while the server runs


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    image: str
    ports: tuple[PortMapping, ...] = ()  # default ports
    volumes: tuple[VolumeMapping, ...] = ()
    env: dict[str, str] = field(default_factory=dict)  # fixed values, not editable
    fields: tuple[EnvField, ...] = ()  # editable values
    description: str = ""
    notes: str = ""
    update_method: str = "pull_recreate"  # or "none" (the game updates itself inside the container)
    config_files: tuple[ConfigFile, ...] = ()


@dataclass
class Instance:
    """A deployed server. `template` is a snapshot of the catalog entry (in its YAML form)
    so later catalog edits cannot break existing servers."""

    id: str
    name: str
    game_id: str
    template: dict
    ports: tuple[PortMapping, ...]
    env: dict[str, str] = field(default_factory=dict, repr=False)  # may hold secrets
    created_at: str = ""


@dataclass(frozen=True)
class ContainerSpec:
    """Everything a backend needs to create a container (paths already resolved)."""

    name: str
    image: str
    ports: tuple[PortMapping, ...]
    volumes: tuple[VolumeMapping, ...]
    env: dict[str, str] = field(repr=False)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnvValue:
    """An editable env value as shown to users. Secret values are never included."""

    name: str
    label: str
    secret: bool
    required: bool
    description: str
    value: str | None  # None for secrets
    is_set: bool


@dataclass(frozen=True)
class ServerInfo:
    id: str
    name: str
    game_id: str
    game_name: str
    image: str
    description: str
    status: Status
    ports: tuple[PortMapping, ...]
    env: tuple[EnvValue, ...] = ()
    detail: str = ""  # e.g. an error message
    created_at: str = ""
    update_method: str = "pull_recreate"
    config_file_count: int = 0


# ----------------------------------------------------------------------- operation results
@dataclass(frozen=True)
class BackupResult:
    backup: "BackupInfo"
    was_running: bool
    stopped_for_backup: bool  # server was stopped so the files were consistent
    restarted: bool
    live: bool  # True when a running server was backed up without stopping (may be inconsistent)
    removed_by_retention: tuple[str, ...]
    note: str  # plain-language account of what happened
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestoreResult:
    restored: str
    safety_backup: "BackupInfo"
    note: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WipeResult:
    safety_backup: "BackupInfo"
    note: str


@dataclass(frozen=True)
class DeleteResult:
    deleted: str
    data_deleted: bool
    backup: "BackupInfo | None"  # backup taken because data was deleted (kept outside the data folder)
    note: str


@dataclass(frozen=True)
class DataSummary:
    """How much a server keeps in its data folder (used to describe destructive actions)."""

    path: str
    files: int
    bytes: int
    truncated: bool  # True if the folder was too big to count fully (numbers are then a lower bound)


@dataclass(frozen=True)
class ImageInfo:
    image: str
    image_id: str | None  # id of the image the container was created from
    latest_local_id: str | None


@dataclass(frozen=True)
class UpdateResult:
    image: str
    before: str | None
    after: str | None
    changed: bool
    was_running: bool
    restarted: bool
    backup: "BackupInfo | None"
    note: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigFileInfo:
    path: str
    label: str
    format: str
    sensitive_keys: tuple[str, ...]
    hot_edit: bool
    exists: bool
    size: int
    backups: tuple[str, ...] = ()  # names of automatic .bak copies, newest first


@dataclass(frozen=True)
class ConfigContent:
    info: ConfigFileInfo
    content: str  # sensitive values are replaced by a placeholder
    masked: bool
    running: bool
    editable_now: bool
    note: str


@dataclass(frozen=True)
class ConfigSaveResult:
    path: str
    size: int
    backup: str | None
    running: bool
    note: str




# ----------------------------------------------------------------------- monitoring
@dataclass(frozen=True)
class ContainerStats:
    """Raw numbers for one container as reported by the container engine."""

    running: bool
    started_at: "datetime | None" = None
    cpu_percent: float = 0.0  # Docker style: 100 = one full CPU core
    mem_used: int = 0
    mem_limit: int = 0
    net_rx: int = 0  # cumulative bytes
    net_tx: int = 0
    block_read: int = 0  # cumulative bytes
    block_write: int = 0
    pids: int = 0


@dataclass(frozen=True)
class HostStats:
    cpu_percent: float | None  # whole machine, 0-100
    cpu_count: int
    mem_total: int | None
    mem_used: int | None
    mem_available: int | None
    load1: float | None
    load5: float | None
    load15: float | None
    disk_total: int
    disk_used: int
    disk_free: int  # for the volume holding the data folder
    uptime_seconds: float | None


@dataclass(frozen=True)
class ServerStats:
    server_id: str
    name: str
    game_id: str
    running: bool
    cpu_percent: float  # Docker style, 100 = one core
    cpu_of_host_percent: float  # share of the whole machine's CPU, 0-100
    mem_used: int
    mem_limit: int
    mem_of_host_percent: float  # share of the host's memory, 0-100
    net_rx: int
    net_tx: int
    net_rx_rate: float  # bytes/second since the previous sample
    net_tx_rate: float
    block_read: int
    block_write: int
    pids: int
    uptime_seconds: float | None
    started_at: str | None


@dataclass(frozen=True)
class StatsSnapshot:
    sampled_at: str  # ISO 8601 UTC
    interval: float
    host: HostStats
    servers: tuple[ServerStats, ...]
    error: str = ""  # set if the container engine could not be queried


@dataclass(frozen=True)
class StatsPoint:
    t: float  # unix seconds
    cpu: float
    mem: int
    rx_rate: float
    tx_rate: float


@dataclass(frozen=True)
class StatsHistory:
    server_id: str
    interval: float
    window: int
    points: tuple[StatsPoint, ...]
