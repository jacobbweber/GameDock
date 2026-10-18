"""HTTP layer: a thin JSON API plus the static dashboard. No management logic here."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..errors import (
    AccessError, BackendError, CatalogError, ConflictError, GameDockError, NotFoundError, OperationError,
    TooLargeError,
)
from ..models import CatalogEntry, PortMapping, ServerInfo
from ..service import GameDockService

STATIC_DIR = Path(__file__).parent / "static"


def server_to_dict(s: ServerInfo) -> dict:
    d = asdict(s)
    d["status"] = s.status.value
    return d


def catalog_to_dict(e: CatalogEntry) -> dict:
    return {
        "id": e.id, "name": e.name, "image": e.image, "description": e.description, "notes": e.notes,
        "ports": [asdict(p) for p in e.ports],
        "update_method": e.update_method,
        "config_files": [{"path": c.path, "label": c.label, "format": c.format, "hot_edit": c.hot_edit}
                         for c in e.config_files],
        "fields": [asdict(f) for f in e.fields],  # defaults of secret fields are not secret values
    }


class PortIn(BaseModel):
    protocol: str = Field(pattern="^(tcp|udp)$")
    container: int
    host: int


class DeployIn(BaseModel):
    game_id: str
    name: str
    ports: list[PortIn] = []
    env: dict[str, str] = {}
    start: bool = False


class BackupIn(BaseModel):
    live: bool = False  # back up a running server without stopping it (may be inconsistent)


class ConfigWriteIn(BaseModel):
    path: str
    content: str


class UpdateIn(BaseModel):
    name: str | None = None
    ports: list[PortIn] | None = None
    env: dict[str, str] | None = None


def _ports(items: list[PortIn] | None) -> list[PortMapping] | None:
    if items is None:
        return None
    return [PortMapping(p.protocol, p.host, p.container) for p in items]


def create_app(service: GameDockService) -> FastAPI:
    app = FastAPI(title="GameDock", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def same_origin_for_writes(request: Request, call_next):
        # Basic CSRF guard: browsers send Origin on cross-site POSTs; refuse mismatches.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(NotFoundError)
    async def _nf(_: Request, e: NotFoundError):
        return JSONResponse({"error": str(e)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def _co(_: Request, e: ConflictError):
        return JSONResponse({"error": str(e)}, status_code=409)

    @app.exception_handler(AccessError)
    async def _ae(_: Request, e: AccessError):
        return JSONResponse({"error": str(e)}, status_code=403)

    @app.exception_handler(TooLargeError)
    async def _tl(_: Request, e: TooLargeError):
        return JSONResponse({"error": str(e)}, status_code=413)

    @app.exception_handler(OperationError)
    async def _oe(_: Request, e: OperationError):
        return JSONResponse({"error": str(e)}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def _rv(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return JSONResponse({"error": f"Invalid request: {where}: {first.get('msg', 'bad value')}"},
                            status_code=422)

    @app.exception_handler(BackendError)
    async def _be(_: Request, e: BackendError):
        return JSONResponse({"error": str(e)}, status_code=502)

    @app.exception_handler(CatalogError)
    async def _ce(_: Request, e: CatalogError):
        return JSONResponse({"error": f"catalog problem: {e}"}, status_code=500)

    @app.exception_handler(GameDockError)
    async def _ge(_: Request, e: GameDockError):
        return JSONResponse({"error": str(e)}, status_code=400)

    # Plain `def` handlers run in a thread pool, so slow docker calls don't block the server.
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/servers")
    def list_servers():
        return [server_to_dict(s) for s in service.list_servers()]

    @app.get("/api/catalog")
    def list_catalog():
        return [catalog_to_dict(e) for e in service.list_catalog()]

    @app.get("/api/catalog/{game_id}")
    def get_catalog_entry(game_id: str):
        return catalog_to_dict(service.get_catalog_entry(game_id))

    @app.post("/api/servers", status_code=201)
    def deploy_server(body: DeployIn):
        return server_to_dict(service.deploy(body.game_id, body.name, _ports(body.ports), body.env, body.start))

    @app.patch("/api/servers/{server_id}")
    def update_server(server_id: str, body: UpdateIn):
        return server_to_dict(service.update(server_id, body.name, _ports(body.ports), body.env))

    @app.delete("/api/servers/{server_id}")
    def delete_server(server_id: str, confirm: bool = False, delete_data: bool = False,
                      confirm_delete_data: bool = False):
        r = service.delete(server_id, confirm, delete_data, confirm_delete_data)
        return {"deleted": r.deleted, "data_kept": not r.data_deleted, "data_deleted": r.data_deleted,
                "backup": r.backup.name if r.backup else None,
                "backup_location": r.backup.path if r.backup else None, "note": r.note}

    # ---- backups
    @app.get("/api/servers/{server_id}/backups")
    def list_backups(server_id: str):
        return {"retention": service.backup_retention,
                "backups": [asdict(b) for b in service.list_backups(server_id)]}

    @app.post("/api/servers/{server_id}/backups", status_code=201)
    def create_backup(server_id: str, body: BackupIn | None = None):
        return asdict(service.create_backup(server_id, live=body.live if body else False))

    @app.post("/api/servers/{server_id}/backups/{name}/restore")
    def restore_backup(server_id: str, name: str, confirm: bool = False):
        return asdict(service.restore_backup(server_id, name, confirm))

    @app.delete("/api/servers/{server_id}/backups/{name}")
    def delete_backup(server_id: str, name: str, confirm: bool = False):
        service.delete_backup(server_id, name, confirm)
        return {"deleted": name}

    @app.post("/api/servers/{server_id}/wipe-data")
    def wipe_data(server_id: str, confirm: bool = False):
        return asdict(service.wipe_data(server_id, confirm))

    # ---- image updates
    @app.get("/api/servers/{server_id}/image")
    def image_info(server_id: str):
        return asdict(service.get_image_info(server_id))

    @app.post("/api/servers/{server_id}/update-image")
    def update_image(server_id: str, confirm: bool = False):
        return asdict(service.update_server_image(server_id, confirm))

    # ---- config files
    @app.get("/api/servers/{server_id}/config")
    def list_config(server_id: str):
        return [asdict(c) for c in service.list_config_files(server_id)]

    @app.get("/api/servers/{server_id}/config/file")
    def read_config(server_id: str, path: str):
        return asdict(service.read_config_file(server_id, path))

    @app.put("/api/servers/{server_id}/config/file")
    def write_config(server_id: str, body: ConfigWriteIn):
        return asdict(service.write_config_file(server_id, body.path, body.content))

    @app.get("/api/servers/{server_id}")
    def get_server(server_id: str):
        return server_to_dict(service.get_status(server_id))

    @app.post("/api/servers/{server_id}/start")
    def start_server(server_id: str):
        return server_to_dict(service.start(server_id))

    @app.post("/api/servers/{server_id}/stop")
    def stop_server(server_id: str):
        return server_to_dict(service.stop(server_id))

    @app.post("/api/servers/{server_id}/restart")
    def restart_server(server_id: str):
        return server_to_dict(service.restart(server_id))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
