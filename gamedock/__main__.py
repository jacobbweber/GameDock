"""Entry point: `python -m gamedock` or the `gamedock` command."""
from __future__ import annotations

import argparse
import json
import logging
import sys

import uvicorn

from .backends import make_backend
from .config import Settings, load_dotenv
from .doctor import run_doctor
from .hostcheck import port_free
from .mcp_api import McpConfig
from .mcp_api.guard import allowed_host_patterns
from .sampler import Sampler
from .scheduler import Scheduler
from .service import GameDockService
from .steam import SteamInfo
from .updates import UpdateChecker
from .web.app import create_app

log = logging.getLogger("gamedock")


def build_service(settings: Settings) -> GameDockService:
    return GameDockService(make_backend(settings.backend, settings.stop_timeout), settings.catalog_dir,
                           settings.data_dir,
                           backup_retention=settings.backup_retention,
                           stats_interval=settings.stats_interval,
                           stats_history_seconds=settings.stats_history_seconds,
                           port_probe=port_free if settings.backend == "docker" else None,
                           updater=UpdateChecker(repo=settings.update_repo, enabled=settings.update_check,
                                                 self_update=settings.self_update, data_dir=settings.data_dir),
                           steam_info=SteamInfo(settings.data_dir / "steam-latest.json", settings.steam_info_url,
                                                enabled=settings.update_check))


def build_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    service = build_service(settings)
    sampler = Sampler(service) if settings.stats_enabled else None
    scheduler = Scheduler(service, background_checks=settings.update_check) if settings.scheduler_enabled else None
    return create_app(service, sampler=sampler, scheduler=scheduler, settings=settings, metrics_enabled=settings.metrics_enabled,
                      metrics_allow_remote=settings.metrics_allow_remote,
                      mcp_config=McpConfig.from_settings(settings),
                      allowed_host_patterns=allowed_host_patterns(
                          settings.host, (*settings.allowed_hosts, *settings.mcp_allowed_hosts)))


def doctor_main(argv: list[str]) -> int:
    """`python -m gamedock doctor [--json]`: exit status 0 when nothing must be fixed, 1 otherwise."""
    parser = argparse.ArgumentParser(prog="gamedock doctor", description="Check that this machine is ready for GameDock.")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    load_dotenv()
    settings = Settings.from_env()
    report = run_doctor(build_service(settings), settings)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print("GameDock doctor\n")
        print(report.to_text(color=sys.stdout.isatty()))
    return 0 if report.ok else 1


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        sys.exit(doctor_main(sys.argv[2:]))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    settings = Settings.from_env()
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "Listening on %s: anyone who can reach this port can start/stop servers. "
            "GameDock has no login yet.", settings.host)
    if settings.metrics_enabled and settings.metrics_allow_remote:
        log.warning("/metrics is open to remote clients (GAMEDOCK_METRICS_ALLOW_REMOTE=true).")
    mode, reason = settings.effective_mcp_mode()
    if reason:
        log.warning(reason)
    elif mode != "off":
        log.info("MCP endpoint at /mcp, mode '%s', bearer token %s.", mode,
                 "required" if settings.mcp_token else "NOT set (anyone who can reach this port can use it)")
    if mode == "full":
        log.warning("MCP mode 'full': agents may delete servers and data (each behind a two-step confirmation).")
    if settings.backend == "fake":
        log.warning("Using the FAKE in-memory backend: no real containers will be touched.")
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
