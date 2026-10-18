"""Plain data types shared by the service, backends, web and (later) MCP layers.

Two ideas are kept apart:
  * CatalogEntry - a game *template* from catalog/*.yaml (what can be deployed)
  * Instance     - a *deployed server* made from a template (stored under data/)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
if TYPE_CHECKING:
    from .backups import BackupInfo

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


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


