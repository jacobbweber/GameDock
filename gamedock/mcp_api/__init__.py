"""Built-in MCP endpoint: lets AI agents use the same service functions as the web page.

    McpConfig  - settings (mode, token, allowed hosts/origins, where GameDock is bound)
    build_mcp  - builds everything for one app: the MCP server (only the tools the mode allows), the
                 confirmation store, the audit log, and the guard that is mounted at /mcp
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from .. import __version__
from ..config import Settings
from ..service import GameDockService
from .audit import AuditLog
from .confirm import ConfirmationStore
from .guard import McpGuard
from .guide import ADD_GAME_URI, GUIDE_URI, guide_text
from .tools import MODE_TIERS, AuditedMCPServer, make_tools, register_tools

log = logging.getLogger("gamedock.mcp")
AUDIT_FILENAME = "mcp-audit.jsonl"


@dataclass(frozen=True)
class McpConfig:
    mode: str = "off"  # the mode actually served (already adjusted by Settings.effective_mcp_mode)
    token: str | None = field(default=None, repr=False)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    bind_host: str = "127.0.0.1"
    configured_mode: str | None = None  # what the user asked for, if different
    disabled_reason: str = ""

    @classmethod
    def from_settings(cls, s: Settings) -> "McpConfig":
        mode, reason = s.effective_mcp_mode()
        return cls(mode, s.mcp_token, s.mcp_allowed_hosts, s.mcp_allowed_origins, s.host, s.mcp_mode, reason)


class _Asgi:
    """Route target. (A class instance, so Starlette treats it as an ASGI app and does not wrap it
    as a request/response function, which would limit it to GET.)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


class McpIntegration:
    def __init__(self, config: McpConfig, audit: AuditLog, confirms: ConfirmationStore,
                 factory: Callable[[], MCPServer] | None, tool_names: list[str], guard_args: dict | None):
        self.config = config
        self.audit = audit
        self.confirms = confirms
        self.tool_names = tool_names
        self._factory = factory
        self._server = factory() if factory else None
        self._server_started = False
        self.guard = McpGuard(_Asgi(self._dispatch), **guard_args) if guard_args is not None else None

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._server.session_manager.handle_request(scope, receive, send)

    @property
    def enabled(self) -> bool:
        return self.guard is not None

    @property
    def asgi(self) -> _Asgi | None:
        return _Asgi(self.guard) if self.guard else None

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """The SDK's session manager must be running while requests are served. (It can run only once per
        instance, so a second start of the same app, as in tests, gets a fresh server.)"""
        if self._factory is None:
            yield
            return
        if self._server_started:
            self._server = self._factory()
        self._server_started = True
        async with self._server.session_manager.run():
            yield

    def info(self, endpoint_url: str) -> dict[str, Any]:
        c = self.config
        return {
            "enabled": self.enabled,
            "mode": c.mode,
            "configured_mode": c.configured_mode or c.mode,
            "reason": c.disabled_reason,
            "token_set": bool(c.token),
            "endpoint_url": endpoint_url,
            "tools": self.tool_names,
            "confirmation_ttl_seconds": int(self.confirms.ttl),
            "guide_resource": GUIDE_URI,
            "add_game_resource": ADD_GAME_URI,
            "audit_available": True,
        }


def build_mcp(service: GameDockService, config: McpConfig | None = None,
              confirmations: ConfirmationStore | None = None) -> McpIntegration:
    config = config or McpConfig()
    audit = AuditLog(Path(service.data_dir) / AUDIT_FILENAME, secret_names=service.secret_field_names)
    confirms = confirmations or ConfirmationStore()
    if config.mode == "off" or config.mode not in MODE_TIERS:
        return McpIntegration(config, audit, confirms, None, [], None)
    ttl = int(confirms.ttl)
    tool_defs = make_tools(service, confirms, audit)
    names = [d.name for d in tool_defs if d.tier in MODE_TIERS[config.mode]]

    def factory() -> MCPServer:
        server = AuditedMCPServer("gamedock", audit=audit, title="GameDock", version=__version__,
                           instructions=("GameDock manages game servers running in Docker. Read the resource "
                                         f"{GUIDE_URI} first. Destructive tools need a two-step confirmation."))
        register_tools(server, tool_defs, config.mode, audit)

        @server.resource(GUIDE_URI, name="gamedock_guide", title="GameDock guide for AI agents",
                         description="How GameDock works, which tools you have, and the confirmation flow",
                         mime_type="text/markdown")
        def guide() -> str:
            return guide_text(config.mode, names, ttl)

        @server.resource(ADD_GAME_URI, name="gamedock_add_a_game", title="How to add a game to GameDock",
                         description="The written procedure for adding a new game to the catalog (10 steps, common "
                                     "traps, verification checklist)",
                         mime_type="text/markdown")
        def add_a_game_resource() -> str:
            return service.add_game_procedure()

        @server.prompt(name="add_a_game", title="Add a new game to the catalog",
                       description="Step-by-step instructions for researching and adding a game's dedicated server")
        def add_a_game(game_name: str) -> str:
            can_add = "add_catalog_entry" in names
            return (f"Add the game '{game_name}' to GameDock's catalog:\n"
                    "1. Call get_add_game_guide (or read the resource gamedock://add-a-game) and follow its 10 steps.\n"
                    "2. Research a maintained Docker image for the dedicated server, its ports and protocols, the save and "
                    "settings folders, the environment variables and the Steam app id of the dedicated server. NEVER invent "
                    "any of these; write what you could not confirm in the entry's notes.\n"
                    "3. Call inspect_image on the EXACT image tag you will use (it never downloads or runs the image; if the "
                    "tag is not found it lists the tags that exist).\n"
                    "4. Write the entry from the template (expose every documented player-facing setting as a field, or say "
                    "in notes why one was left out) and call validate_catalog_entry until valid is true and every warning is "
                    "explained or fixed.\n"
                    + ("5. Call add_catalog_entry, then tell the person the entry is waiting for their approval in the web "
                       "page (Catalog -> Review). You cannot approve it and it cannot be deployed before. Poll "
                       "get_catalog_review_status to see when it is approved.\n"
                       if can_add else
                       "5. This connection is read-only: give the validated entry to the person to add.\n")
                    + "6. After approval, with the person's agreement: preview_deploy and check_ports, then deploy a test copy "
                    "on spare ports, then verify_game_setup. Read get_server_logs / get_server_diagnostics for anything it "
                    "flags, fix the entry with update_catalog_entry (it needs approval again), redeploy the test copy "
                    "(a deployed server keeps a snapshot of the entry) and report the result.")

        @server.prompt(name="gamedock_overview", title="GameDock overview",
                       description="How GameDock works and how to use its tools safely")
        def overview() -> str:
            return guide_text(config.mode, names, ttl)

        @server.prompt(name="deploy_game_server", title="Deploy a game server",
                       description="Step-by-step instructions to deploy a game from the catalog")
        def deploy_game_server(game_id: str) -> str:
            return (f"Deploy the game '{game_id}' with GameDock:\n"
                    f"1. Call get_catalog_entry with game_id='{game_id}' and read its fields and default ports.\n"
                    "2. Ask the human for any required setting you do not know (never invent passwords).\n"
                    "3. Call list_servers to see which ports are already in use.\n"
                    "4. Call deploy_server with a clear name, the settings, and start=true if they want it running.\n"
                    "5. Call get_server_status to confirm it is running and report the ports to the human.")

        # Host/Origin/token checks are done by McpGuard (it knows about the bearer token and wildcard binds),
        # so the SDK's own DNS-rebinding check is switched off to avoid two rule sets disagreeing.
        server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                   transport_security=TransportSecuritySettings(
                                       enable_dns_rebinding_protection=False))
        return server

    guard_args = dict(token=config.token, bind_host=config.bind_host, allowed_hosts=config.allowed_hosts,
                      allowed_origins=config.allowed_origins, audit=audit)
    return McpIntegration(config, audit, confirms, factory, names, guard_args)
