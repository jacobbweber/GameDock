"""HTTP layer: a thin JSON API plus the static dashboard. No management logic here."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..errors import BackendError, CatalogError, GameDockError, NotFoundError
from ..models import ServerInfo
from ..service import GameDockService

STATIC_DIR = Path(__file__).parent / "static"


def server_to_dict(s: ServerInfo) -> dict:
    d = asdict(s)
    d["status"] = s.status.value
    return d


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
