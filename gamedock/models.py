"""Plain data types shared by the service, backends, web and (later) MCP layers.

Two ideas are kept apart:
  * CatalogEntry - a game *template* from catalog/*.yaml (what can be deployed)
  * Instance     - a *deployed server* made from a template (stored under data/)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
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
