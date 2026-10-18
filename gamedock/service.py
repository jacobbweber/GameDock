"""GameDock service layer.

All management actions live here. The web API calls these functions, and a
future MCP endpoint will call exactly the same ones. This module knows nothing
about HTTP.

Vocabulary:
  catalog entry = a game template (catalog/*.yaml)
  server        = a deployed instance of a catalog entry (stored under data/)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .backends.base import ContainerBackend
from .catalog import entry_to_dict, load_catalog, parse_definition
from .errors import BackendError, ConflictError, NotFoundError, ValidationError
from .models import (
    CatalogEntry, ContainerSpec, EnvValue, Instance, PortMapping, ServerInfo, Status, VolumeMapping,
)
from .store import InstanceStore

CONTAINER_PREFIX = "gamedock-"
LABEL_MANAGED = "gamedock.managed"
LABEL_SERVER_ID = "gamedock.server_id"
LABEL_GAME_ID = "gamedock.game_id"
MAX_ENV_VALUE_LEN = 4096


def container_name(server_id: str) -> str:
    return f"{CONTAINER_PREFIX}{server_id}"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")


class GameDockService:
    def __init__(self, backend: ContainerBackend, catalog_dir: Path, data_dir: Path) -> None:
        self.backend = backend
        self.catalog_dir = Path(catalog_dir)
        self.data_dir = Path(data_dir)
        self.store = InstanceStore(self.data_dir / "instances.json")

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
                          status, inst.ports, env, detail, inst.created_at)

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
        existing = {i.id for i in self.store.all()}
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

    def start(self, server_id: str) -> ServerInfo:
        """Start a server (re-creating its container if it is missing)."""
        inst = self._instance(server_id)
        spec = self._spec(inst)
        if self.backend.get_state(spec.name).status == Status.RUNNING:
            return self._info(inst)
        self._prepare_dirs(spec)
        self.backend.create(spec)
        self.backend.start(spec.name)
        return self._info(inst)

    def stop(self, server_id: str) -> ServerInfo:
        inst = self._instance(server_id)
        name = container_name(inst.id)
        if self.backend.get_state(name).status == Status.RUNNING:
            self.backend.stop(name)
        return self._info(inst)

    def restart(self, server_id: str) -> ServerInfo:
        self.stop(server_id)
        return self.start(server_id)

    def delete(self, server_id: str, confirm: bool = False, delete_data: bool = False,
               confirm_delete_data: bool = False) -> None:
        """Remove a server and its container. Its data folder is kept."""
        inst = self._instance(server_id)
        if not confirm:
            raise ValidationError(f"Deleting '{inst.name}' needs explicit confirmation (confirm=true)")
        if delete_data:
            if not confirm_delete_data:
                raise ValidationError(
                    "Deleting data needs a second explicit confirmation (confirm_delete_data=true)")
            raise ValidationError(
                "Deleting server data is not available yet: backups arrive in step 3. "
                "Delete the server without its data and remove the folder by hand if you are sure.")
        cname = container_name(inst.id)
        if self.backend.get_state(cname).status == Status.RUNNING:
            raise ConflictError("Stop the server before deleting it")
        self.backend.remove(cname)
        self.store.remove(inst.id)
