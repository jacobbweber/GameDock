"""HTTP layer: a thin JSON API plus the static dashboard. No management logic here."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route
from pydantic import BaseModel, Field, StrictBool, StrictInt
from typing import Literal

from .. import __version__
from ..errors import (
    AccessError, BackendError, CatalogError, ConflictError, GameDockError, NotFoundError, OperationError,
    TooLargeError,
)
from ..models import PortMapping
from ..serialize import catalog_to_dict, delete_result_to_dict, server_to_dict
from ..config import Settings
from ..doctor import run_doctor
from ..mcp_api import McpConfig, build_mcp
from ..mcp_api.guard import host_allowed
from ..mcp_api.confirm import ConfirmationStore
from ..metrics import render_metrics
from ..service import GameDockService

STATIC_DIR = Path(__file__).parent / "static"


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


class ApproveIn(BaseModel):
    yaml_sha256: str   # the fingerprint of the text the person was shown (the review dialog sends it back)


class BackupIn(BaseModel):
    live: bool = False  # back up a running server without stopping it (may be inconsistent)


class ConfigWriteIn(BaseModel):
    path: str
    content: str


class ConfigFormIn(BaseModel):
    path: str
    content: str | None = None            # the text the editor holds (default: the file on disk)
    changes: dict[str, str] | None = None  # {field id: new value}


class UpdateIn(BaseModel):
    name: str | None = None
    ports: list[PortIn] | None = None
    env: dict[str, str] | None = None


class BackupScheduleIn(BaseModel):
    enabled: StrictBool
    frequency: Literal["interval", "daily", "weekly"] | None = None
    every_hours: StrictInt | None = None
    time: str | None = None
    days: list[str] | None = None
    retention: StrictInt | None = None


class MaintenanceScheduleIn(BaseModel):
    enabled: StrictBool
    frequency: Literal["daily", "weekly"] | None = None
    time: str | None = None
    days: list[str] | None = None
    backup_first: StrictBool = True
    update_image: StrictBool = True
    restart: StrictBool = True


def _ports(items: list[PortIn] | None) -> list[PortMapping] | None:
    if items is None:
        return None
    return [PortMapping(p.protocol, p.host, p.container) for p in items]


def _is_loopback(host: str | None) -> bool:
    return bool(host) and (host in ("127.0.0.1", "::1", "localhost") or host.startswith("::ffff:127."))


def create_app(service: GameDockService, sampler=None, metrics_enabled: bool = False,
               metrics_allow_remote: bool = False, mcp_config: McpConfig | None = None,
               mcp_confirmations: ConfirmationStore | None = None,
               allowed_host_patterns: list[str] | None = None, settings: Settings | None = None,
               scheduler=None) -> FastAPI:
    """`sampler` (optional) is started and stopped with the app. /metrics exists only if metrics_enabled.
    /mcp is served only if mcp_config asks for a mode other than "off".
    `allowed_host_patterns` (optional) restricts the Host header of web/API requests, which stops DNS-rebinding
    attacks from web pages (None = no restriction; /mcp has its own check)."""
    mcp = build_mcp(service, mcp_config, mcp_confirmations)
    # scheduled runs are written to the same audit file as MCP calls (the "client" says why it ran)
    service.audit_hook = lambda tool, args, status, detail: mcp.audit.record(tool, args, status, detail,
                                                                          str(args.get("trigger") or "schedule"))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if sampler:
            sampler.start()
        if scheduler:
            scheduler.start()
        try:
            async with mcp.lifespan():
                yield
        finally:
            if scheduler:
                scheduler.stop()
            if sampler:
                sampler.stop()

    app = FastAPI(title="GameDock", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)

    @app.middleware("http")
    async def browser_safety(request: Request, call_next):
        # DNS rebinding: a web page can make its own name point at 127.0.0.1 and then pass the Origin check
        # below (Origin == Host). Refusing unknown Host names closes that hole.
        if (allowed_host_patterns is not None and not request.url.path.startswith("/mcp")
                and not host_allowed(request.headers.get("host", ""), allowed_host_patterns)):
            return JSONResponse({"error": "This Host name is not allowed. If you reach GameDock by another name, "
                                          "add it to GAMEDOCK_ALLOWED_HOSTS."}, status_code=421)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")  # nobody can frame the UI to trick a click
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path in ("/", "/index.html") or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # always revalidate, so a new version shows after an update
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.middleware("http")
    async def same_origin_for_writes(request: Request, call_next):
        # Basic CSRF guard: browsers send Origin on cross-site POSTs; refuse mismatches.
        # (/mcp has its own Origin/Host/token checks in the MCP guard, because its clients are not browsers.)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith("/mcp"):
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

    # ---- GameDock's own version and update
    @app.get("/api/version")
    def version_info():
        """Version, commit and what is known about a newer GameDock (never contacts the network)."""
        return service.updater.status()

    @app.post("/api/gamedock/update-check")
    def check_for_gamedock_update():
        """The Check now button: asks GitHub (read-only). Offline is not an error; the answer says so."""
        return service.updater.check(force=True)

    @app.post("/api/gamedock/update")
    def update_gamedock(confirm: bool = False):
        """Pull the newest GameDock (git pull --ff-only) and restart GameDock only. Web only, never an MCP tool."""
        if not confirm:
            raise GameDockError("Updating GameDock needs explicit confirmation (confirm=true)")
        return service.updater.apply(service.freeze, service.unfreeze)

    @app.get("/api/status")
    def status():
        """Cheap summary for the banner at the top of the UI (Docker reachable? catalog problems?)."""
        d = service.docker_status()
        fake = settings is not None and settings.backend == "fake"
        service.list_catalog()  # refreshes service.catalog_problems
        return {"version": __version__, "update_available": service.updater.status()["update_available"],
                "catalog_review_pending": [{"id": e.id, "name": e.name} for e in service.list_catalog() if not e.reviewed],
                "docker": {"ok": d.reachable, "kind": d.kind, "message": d.message,
                                                   "version": d.server_version, "fake": fake},
                "catalog_problems": [{"file": f, "problem": p} for f, p in service.catalog_problems]}

    @app.get("/api/doctor")
    def doctor():
        """The full health check (same as `python -m gamedock doctor`). Does a few quick local checks."""
        return run_doctor(service, settings).to_dict()

    @app.get("/api/servers")
    def list_servers():
        return [server_to_dict(s) for s in service.list_servers()]

    @app.get("/api/catalog")
    def list_catalog():
        return [catalog_to_dict(e) for e in service.list_catalog()]

    @app.get("/api/catalog/{game_id}")
    def get_catalog_entry(game_id: str):
        return catalog_to_dict(service.get_catalog_entry(game_id))

    # Reviewing entries that an AI agent added through MCP. These actions exist only here (never as MCP tools).
    @app.get("/api/catalog/{game_id}/review")
    def catalog_review(game_id: str):
        return service.catalog_review(game_id)

    @app.post("/api/catalog/{game_id}/approve")
    def catalog_approve(game_id: str, body: ApproveIn):
        return service.approve_catalog_entry(game_id, body.yaml_sha256)

    @app.post("/api/catalog/{game_id}/reject")
    def catalog_reject(game_id: str, confirm: bool = False):
        return service.reject_catalog_entry(game_id, confirm)

    @app.post("/api/servers", status_code=201)
    def deploy_server(body: DeployIn):
        return server_to_dict(service.deploy(body.game_id, body.name, _ports(body.ports), body.env, body.start))

    @app.patch("/api/servers/{server_id}")
    def update_server(server_id: str, body: UpdateIn):
        return server_to_dict(service.update(server_id, body.name, _ports(body.ports), body.env))

    @app.delete("/api/servers/{server_id}")
    def delete_server(server_id: str, confirm: bool = False, delete_data: bool = False,
                      confirm_delete_data: bool = False):
        return delete_result_to_dict(service.delete(server_id, confirm, delete_data, confirm_delete_data))

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

    @app.get("/api/servers/{server_id}/image-update")
    def image_update_status(server_id: str):
        """What is known about a newer game image (cached; asks nothing)."""
        return service.get_image_update_status(server_id)

    @app.post("/api/servers/{server_id}/image-update/check")
    def image_update_check(server_id: str):
        """The server's Check now button: asks the registry (nothing is downloaded)."""
        return service.check_image_update(server_id, force=True)

    # ---- game version (SteamCMD games)
    @app.get("/api/servers/{server_id}/game-update")
    def game_update_status(server_id: str):
        """Installed and newest game version (cached; asks nothing)."""
        return service.get_game_update_status(server_id)

    @app.post("/api/servers/{server_id}/game-update/check")
    def game_update_check(server_id: str):
        """The Check now button: asks the Steam data service for the newest build (read-only)."""
        return service.check_game_update(server_id, force=True)

    @app.post("/api/servers/{server_id}/update-game")
    def update_game(server_id: str, confirm: bool = False):
        """Safety backup, restart so the game files update, then verify the installed build changed."""
        return service.update_game(server_id, confirm)

    # ---- schedules
    @app.get("/api/schedules")
    def all_schedules():
        return service.list_schedules()

    @app.get("/api/servers/{server_id}/schedules")
    def server_schedules(server_id: str):
        return service.get_server_schedules(server_id)

    @app.put("/api/servers/{server_id}/schedules/backup")
    def put_backup_schedule(server_id: str, body: BackupScheduleIn):
        return service.set_backup_schedule(server_id, body.enabled, body.frequency, body.every_hours, body.time,
                                           body.days, body.retention)

    @app.put("/api/servers/{server_id}/schedules/maintenance")
    def put_maintenance_schedule(server_id: str, body: MaintenanceScheduleIn):
        return service.set_maintenance_schedule(server_id, body.enabled, body.frequency, body.time, body.days,
                                                body.backup_first, body.update_image, body.restart)

    @app.post("/api/servers/{server_id}/schedules/{kind}/run")
    def run_schedule(server_id: str, kind: str, confirm: bool = False):
        """The Run now button for a schedule (kind: backup or maintenance)."""
        return service.run_schedule_now(server_id, kind, confirm)

    # ---- logs
    @app.get("/api/servers/{server_id}/logs")
    def server_logs(server_id: str, lines: int = 200):
        return asdict(service.get_logs(server_id, lines))

    # ---- config files
    @app.get("/api/servers/{server_id}/config")
    def list_config(server_id: str):
        return [asdict(c) for c in service.list_config_files(server_id)]

    @app.post("/api/servers/{server_id}/config/form")
    def config_form(server_id: str, body: ConfigFormIn):
        """Structured view helper: parse text into fields and/or apply field changes to it. Writes nothing."""
        return asdict(service.config_form(server_id, body.path, body.content, body.changes))

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

    # ---- monitoring
    @app.get("/api/stats")
    def all_stats(sort: str | None = None):
        snap = service.get_all_stats(sort)
        d = asdict(snap)
        return d

    @app.get("/api/stats/host")
    def host_stats():
        return asdict(service.get_host_stats())

    @app.get("/api/stats/history")
    def all_stats_history(window: int = 600):
        servers, host = service.get_all_stats_history(window)
        return {"interval": service.stats_interval,
                "servers": {k: asdict(v) for k, v in servers.items()},
                "host": [{"t": t, "cpu": cpu, "mem": mem} for t, cpu, mem in host]}

    @app.get("/api/servers/{server_id}/stats")
    def server_stats(server_id: str):
        return asdict(service.get_server_stats(server_id))

    @app.get("/api/servers/{server_id}/stats/history")
    def server_stats_history(server_id: str, window: int = 600):
        return asdict(service.get_stats_history(server_id, window))

    # ---- MCP (the endpoint itself is mounted below; these two routes feed the web UI)
    @app.get("/api/mcp/info")
    def mcp_info(request: Request):
        return mcp.info(str(request.base_url).rstrip("/") + "/mcp")

    @app.get("/api/mcp/audit")
    def mcp_audit(limit: int = 50):
        return {"entries": mcp.audit.recent(limit)}

    if mcp.enabled:
        # Raw routes for both spellings so /mcp does not redirect (clients may not follow a 307 on POST).
        app.router.routes.append(Route("/mcp", endpoint=mcp.asgi))
        app.router.routes.append(Route("/mcp/", endpoint=mcp.asgi))
    else:
        async def mcp_off(request: Request):
            return JSONResponse({"error": "The MCP endpoint is switched off. " + (
                mcp.config.disabled_reason or "Set GAMEDOCK_MCP_MODE to read_only, standard or full.")},
                status_code=404)

        app.router.routes.append(Route("/mcp", endpoint=mcp_off, methods=["GET", "POST", "DELETE"]))
        app.router.routes.append(Route("/mcp/", endpoint=mcp_off, methods=["GET", "POST", "DELETE"]))

    if metrics_enabled:
        @app.get("/metrics", include_in_schema=False)
        def metrics(request: Request):
            if not metrics_allow_remote and not _is_loopback(request.client.host if request.client else None):
                return PlainTextResponse("metrics are only served to localhost\n", status_code=403)
            return PlainTextResponse(render_metrics(service.get_all_stats()),
                                     media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
