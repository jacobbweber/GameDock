"""Plain data types shared by the service, backends, web and (later) MCP layers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class Status(str, Enum):
    NOT_CREATED = "not_created"  # defined in catalog, no container yet
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
    host: str  # relative paths are placed under <data_dir>/<game id>/
    container: str


@dataclass(frozen=True)
class GameDefinition:
    id: str
    name: str
    image: str
    ports: tuple[PortMapping, ...] = ()
    volumes: tuple[VolumeMapping, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class ContainerSpec:
    """Everything a backend needs to create a container (paths already resolved)."""

    name: str
    image: str
    ports: tuple[PortMapping, ...]
    volumes: tuple[VolumeMapping, ...]
    env: dict[str, str]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerInfo:
    id: str
    name: str
    image: str
    description: str
    status: Status
    ports: tuple[PortMapping, ...]
    detail: str = ""  # e.g. an error message
