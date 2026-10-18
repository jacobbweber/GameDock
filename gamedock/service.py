"""GameDock service layer.

All management actions live here. The web API calls these functions, and a
future MCP endpoint will call exactly the same ones. This module knows nothing
about HTTP.
"""
from __future__ import annotations

from pathlib import Path

from .backends.base import ContainerBackend
from .catalog import load_catalog
from .errors import BackendError, NotFoundError
from .models import ContainerSpec, GameDefinition, ServerInfo, Status, VolumeMapping

CONTAINER_PREFIX = "gamedock-"
LABEL_MANAGED = "gamedock.managed"
LABEL_GAME_ID = "gamedock.game_id"


def container_name(game_id: str) -> str:
    return f"{CONTAINER_PREFIX}{game_id}"


class GameDockService:
    def __init__(self, backend: ContainerBackend, catalog_dir: Path, data_dir: Path) -> None:
        self.backend = backend
        self.catalog_dir = Path(catalog_dir)
        self.data_dir = Path(data_dir)

    # -- helpers -----------------------------------------------------------
    def _definitions(self) -> dict[str, GameDefinition]:
        # Reloaded on each call so catalog edits show up without a restart.
        return load_catalog(self.catalog_dir)

    def _definition(self, server_id: str) -> GameDefinition:
        try:
            return self._definitions()[server_id]
        except KeyError:
            raise NotFoundError(f"Unknown server '{server_id}'") from None

    def _spec(self, d: GameDefinition) -> ContainerSpec:
        base = (self.data_dir / d.id).resolve()
        volumes = []
        for v in d.volumes:
            host = Path(v.host)
            host_path = host if host.is_absolute() else (base / host).resolve()
            volumes.append(VolumeMapping(str(host_path), v.container))
        return ContainerSpec(
            name=container_name(d.id),
            image=d.image,
            ports=d.ports,
            volumes=tuple(volumes),
            env=dict(d.env),
            labels={LABEL_MANAGED: "true", LABEL_GAME_ID: d.id},
        )

    def _info(self, d: GameDefinition) -> ServerInfo:
        try:
            state = self.backend.get_state(container_name(d.id))
            status, detail = state.status, state.detail
        except BackendError as e:
            status, detail = Status.ERROR, str(e)
        return ServerInfo(d.id, d.name, d.image, d.description, status, d.ports, detail)

    # -- public API --------------------------------------------------------
    def list_servers(self) -> list[ServerInfo]:
        return [self._info(d) for d in self._definitions().values()]

    def get_status(self, server_id: str) -> ServerInfo:
        return self._info(self._definition(server_id))

    def start(self, server_id: str) -> ServerInfo:
        """Start a server, creating its container first if needed."""
        d = self._definition(server_id)
        spec = self._spec(d)
        for v in spec.volumes:  # make sure host folders exist before docker does it as root
            Path(v.host).mkdir(parents=True, exist_ok=True)
        if self.backend.get_state(spec.name).status == Status.RUNNING:
            return self._info(d)
        self.backend.create(spec)
        self.backend.start(spec.name)
        return self._info(d)

    def stop(self, server_id: str) -> ServerInfo:
        d = self._definition(server_id)
        name = container_name(d.id)
        if self.backend.get_state(name).status == Status.RUNNING:
            self.backend.stop(name)
        return self._info(d)

    def restart(self, server_id: str) -> ServerInfo:
        self.stop(server_id)
        return self.start(server_id)
