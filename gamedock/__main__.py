"""Entry point: `python -m gamedock` or the `gamedock` command."""
from __future__ import annotations

import logging

import uvicorn

from .backends import make_backend
from .config import Settings, load_dotenv
from .service import GameDockService
from .web.app import create_app

log = logging.getLogger("gamedock")


def build_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    service = GameDockService(make_backend(settings.backend), settings.catalog_dir, settings.data_dir,
                              backup_retention=settings.backup_retention)
    return create_app(service)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    settings = Settings.from_env()
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "Listening on %s: anyone who can reach this port can start/stop servers. "
            "GameDock has no login yet.", settings.host)
    if settings.backend == "fake":
        log.warning("Using the FAKE in-memory backend: no real containers will be touched.")
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
