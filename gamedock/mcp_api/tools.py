"""The MCP tools. Every tool is a thin wrapper around a GameDockService method (the same ones the web
layer calls); this module only adds argument checking, plain-language summaries for the two-step
destructive flow, and audit logging.

Note: no `from __future__ import annotations` here on purpose. The MCP SDK builds each tool's input
schema from the real type annotations of the function.
"""
import functools
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from ..errors import GameDockError
from ..models import PortMapping, Status
from ..serialize import catalog_to_dict, delete_result_to_dict, server_to_dict, to_plain
from ..service import BACKUPS_DIRNAME, DELETED_DIRNAME, GameDockService
from .audit import AuditLog
from .confirm import ConfirmationError, ConfirmationStore

log = logging.getLogger("gamedock.mcp")

# Who is calling (set by the guard from the connection); recorded in the audit log.
current_client: ContextVar[str] = ContextVar("gamedock_mcp_client", default="")

READ, STANDARD, DESTRUCTIVE = "read", "standard", "destructive"
MODE_TIERS = {
    "read_only": (READ,),
    "standard": (READ, STANDARD),
    "full": (READ, STANDARD, DESTRUCTIVE),
}

ServerId = Annotated[str, Field(description="The server id, as shown by list_servers (for example 'valheim').")]
Result = dict[str, Any]


class PortSetting(BaseModel):
    protocol: Literal["tcp", "udp"]
    container: int = Field(description="Port inside the container")
    host: int = Field(description="Port on this machine")


class TokenRejected(ToolError):
    """A confirmation token was missing, expired, reused or for different arguments."""


@dataclass
class ToolDef:
    name: str
    tier: str
    fn: Callable
    annotations: ToolAnnotations


def _ports(items: list[PortSetting] | None) -> list[PortMapping] | None:
    if items is None:
        return None
    return [PortMapping(p.protocol, p.host, p.container) for p in items]


def _size(n: int) -> str:
    f = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{int(f)} bytes" if unit == "bytes" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} bytes"


def _ann(read: bool = False, destructive: bool = False, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=read, destructiveHint=False if read else destructive,
                           idempotentHint=idempotent or read, openWorldHint=False)


def make_tools(service: GameDockService, confirms: ConfirmationStore) -> list[ToolDef]:
    """All tool definitions, tagged by tier. Which ones are served depends on the mode."""
    defs: list[ToolDef] = []

    def tool(tier: str, annotations: ToolAnnotations):
        def deco(fn):
            defs.append(ToolDef(fn.__name__, tier, fn, annotations))
            return fn
        return deco

    # ------------------------------------------------------------------ read-only
    @tool(READ, _ann(read=True))
    def list_catalog() -> Result:
        """List the games GameDock can deploy (the catalog): id, name, image, default ports, the settings
        you can fill in (fields; secret ones are marked and have no value) and editable config files."""
        return {"games": [catalog_to_dict(e) for e in service.list_catalog()]}

    @tool(READ, _ann(read=True))
    def get_catalog_entry(game_id: Annotated[str, Field(description="Catalog id from list_catalog")]) -> Result:
        """Details of one catalog entry, including every setting (field) deploy_server accepts."""
        return catalog_to_dict(service.get_catalog_entry(game_id))

    @tool(READ, _ann(read=True))
    def list_servers() -> Result:
        """List deployed servers with their status (running, stopped, not_created, error), ports and
        settings. Secret settings are never shown, only whether they are set. `status: running` only means the
        container is up: check `ready` (true = the game can be joined) and `phase` (starting, installing with
        `progress` percent, ready, unhealthy). `activity` shows what GameDock is doing right now (e.g. deploying)."""
        return {"servers": [server_to_dict(s) for s in service.list_servers()]}

    @tool(READ, _ann(read=True))
    def get_server_status(server_id: ServerId) -> Result:
        """Current status and settings of one server. `status: running` only means the container is up; the game can
        be joined when `ready` is true. `phase` is starting, installing (with `progress` percent), ready or
        unhealthy; `phase_detail` explains it in plain words. `activity` is what GameDock is doing right now."""
        return server_to_dict(service.get_status(server_id))

    @tool(READ, _ann(read=True))
    def get_server_stats(server_id: ServerId) -> Result:
        """Live CPU, memory, network and disk numbers for one server (zeros if it is not running)."""
        return to_plain(service.get_server_stats(server_id))

    @tool(READ, _ann(read=True))
    def get_host_stats() -> Result:
        """CPU, memory, load and disk totals of the machine GameDock runs on."""
        return to_plain(service.get_host_stats())

    @tool(READ, _ann(read=True))
    def get_stats_history(
        server_id: ServerId,
        window_seconds: Annotated[int, Field(ge=10, description="How far back to look, in seconds")] = 600,
        max_points: Annotated[int, Field(ge=2, le=360, description="At most this many points are returned")] = 60,
    ) -> Result:
        """Recent CPU/memory/network samples for one server (oldest first), plus min/average/max.
        Kept in memory only, so it starts empty after GameDock restarts."""
        h = service.get_stats_history(server_id, window_seconds)
        pts = list(h.points)
        step = max(1, -(-len(pts) // max_points))  # ceiling division
        shown = pts[::step]
        out: dict[str, Any] = {"server_id": server_id, "interval_seconds": h.interval, "window_seconds": h.window,
                               "samples_available": len(pts), "points": to_plain(shown)}
        if pts:
            cpu = [p.cpu for p in pts]
            mem = [p.mem for p in pts]
            out["summary"] = {"cpu_percent": {"min": min(cpu), "avg": sum(cpu) / len(cpu), "max": max(cpu)},
                              "mem_bytes": {"min": min(mem), "avg": int(sum(mem) / len(mem)), "max": max(mem)}}
        return out

    @tool(READ, _ann(read=True))
    def list_backups(server_id: ServerId) -> Result:
        """Backups saved for a server (newest first). Names are what restore_backup and delete_backup take."""
        return {"retention": service.backup_retention,
                "backups": [to_plain(b) for b in service.list_backups(server_id)]}

    @tool(READ, _ann(read=True))
    def list_config_files(server_id: ServerId) -> Result:
        """Config files of a server that can be read and edited through GameDock."""
        return {"files": [to_plain(c) for c in service.list_config_files(server_id)]}

    @tool(READ, _ann(read=True))
    def read_config_file(server_id: ServerId,
                         path: Annotated[str, Field(description="Path exactly as listed by list_config_files")]) -> Result:
        """Read one config file. Values of sensitive keys are replaced by a placeholder. If the game has not
        created the file yet (first start not finished) `info.exists` is false and `content` is empty."""
        d = to_plain(service.read_config_file(server_id, path))
        d.pop("form", None)  # the structured view is for the web page; agents work with the text
        return d

    @tool(READ, _ann(read=True))
    def get_server_logs(
        server_id: ServerId,
        lines: Annotated[int, Field(ge=1, le=500, description="How many of the newest lines to return")] = 100,
    ) -> Result:
        """The newest lines of the game's console output (colour codes removed, passwords hidden). Use it to see
        why a server is not ready, what it is downloading, or why it stopped. Read-only."""
        return to_plain(service.get_logs(server_id, lines, max_chars=40_000))

    # ------------------------------------------------------------------ standard (changes things, not destructive)
    @tool(STANDARD, _ann())
    def deploy_server(
        game_id: Annotated[str, Field(description="Catalog id from list_catalog")],
        name: Annotated[str, Field(description="A display name for the new server")],
        ports: Annotated[list[PortSetting] | None, Field(description="Override the default ports (optional)")] = None,
        env: Annotated[dict[str, str] | None, Field(
            description="Settings by field name, see get_catalog_entry. Required secret fields must be given.")] = None,
        start: Annotated[bool, Field(description="Start the server right after creating it")] = False,
    ) -> Result:
        """Create a new server from a catalog entry (and optionally start it). Fails if a port is already used."""
        return server_to_dict(service.deploy(game_id, name, _ports(ports), env or {}, start))

    @tool(STANDARD, _ann(idempotent=True))
    def start_server(server_id: ServerId) -> Result:
        """Start a server."""
        return server_to_dict(service.start(server_id))

    @tool(STANDARD, _ann(idempotent=True))
    def stop_server(server_id: ServerId) -> Result:
        """Stop a running server (players are disconnected; data is kept)."""
        return server_to_dict(service.stop(server_id))

    @tool(STANDARD, _ann())
    def restart_server(server_id: ServerId) -> Result:
        """Restart a server (players are disconnected briefly)."""
        return server_to_dict(service.restart(server_id))

    @tool(STANDARD, _ann(idempotent=True))
    def update_server_settings(
        server_id: ServerId,
        name: Annotated[str | None, Field(description="New display name")] = None,
        ports: Annotated[list[PortSetting] | None, Field(description="Replace the port list")] = None,
        env: Annotated[dict[str, str] | None, Field(
            description="Settings to change, by field name. Leave a secret out to keep its current value.")] = None,
    ) -> Result:
        """Change a server's name, ports or settings. Changes apply the next time it starts."""
        return server_to_dict(service.update(server_id, name, _ports(ports), env))

    @tool(STANDARD, _ann())
    def update_server_image(server_id: ServerId) -> Result:
        """Pull the newest game image and recreate the container. A running server is stopped, backed up
        automatically first, then started again. Does nothing if the image has not changed."""
        return to_plain(service.update_server_image(server_id, confirm=True))

    @tool(STANDARD, _ann())
    def create_backup(server_id: ServerId,
                      live: Annotated[bool, Field(
                          description="Back up a running server without stopping it (may be inconsistent)")] = False
                      ) -> Result:
        """Save a backup of a server's data. A running server is stopped briefly for a consistent copy
        (and restarted) unless live=true."""
        return to_plain(service.create_backup(server_id, live=live))

    @tool(STANDARD, _ann(idempotent=True))
    def write_config_file(server_id: ServerId,
                          path: Annotated[str, Field(description="Path exactly as listed by list_config_files")],
                          content: Annotated[str, Field(description="The complete new file content")]) -> Result:
        """Replace a config file's content. The previous version is kept as an automatic .bak copy.
        Masked placeholders from read_config_file keep the existing secret values."""
        return to_plain(service.write_config_file(server_id, path, content))

    # ------------------------------------------------------------------ destructive (two steps)
    def two_step(action: str, args: dict[str, Any], token: str | None, describe: Callable[[], str],
                 run: Callable[[], Result]) -> Result:
        if token is None:
            summary = describe()  # raises ToolError when the action cannot be done right now
            issued = confirms.issue(action, args)
            return {
                "status": "confirmation_required",
                "action": action,
                "summary": summary,
                "confirmation_token": issued,
                "expires_in_seconds": int(confirms.ttl),
                "how_to_confirm": (f"Nothing has been changed yet. To go ahead, call {action} again with exactly "
                                   "the same arguments plus confirmation_token. The token works once, expires "
                                   "soon, and only for these exact arguments. Show the summary to the user first."),
            }
        try:
            confirms.consume(token, action, args)
        except ConfirmationError as e:
            raise TokenRejected(str(e)) from None
        result = run()
        result["status"] = "done"
        return result

    def stopped_server(server_id: str, what: str):
        info = service.get_status(server_id)
        if info.status == Status.RUNNING:
            raise ToolError(f"Server '{info.name}' ({info.id}) is running. Stop it first with stop_server, "
                            f"then {what}.")
        return info

    def find_backup(server_id: str, name: str):
        backups = service.list_backups(server_id)
        for b in backups:
            if b.name == name:
                return b
        have = ", ".join(b.name for b in backups[:10]) or "none"
        raise ToolError(f"Server '{server_id}' has no backup named '{name}'. Backups: {have}.")

    def data_phrase(server_id: str) -> str:
        d = service.get_data_summary(server_id)
        more = "at least " if d.truncated else ""
        return f"{more}{d.files} files, {_size(d.bytes)}"

    @tool(DESTRUCTIVE, _ann(destructive=True))
    def delete_server(
        server_id: ServerId,
        delete_data: Annotated[bool, Field(
            description="Also delete the server's data folder (saves, configs). Default false keeps it.")] = False,
        confirmation_token: Annotated[str | None, Field(
            description="Leave empty on the first call. On the second call pass the token you were given.")] = None,
    ) -> Result:
        """DESTRUCTIVE, two steps. Remove a server and its container (the server must be stopped). The first call
        changes nothing and returns a summary plus a confirmation_token; the second call with that token does it."""
        args = {"server_id": server_id, "delete_data": delete_data}

        def describe() -> str:
            info = stopped_server(server_id, "delete it")
            s = (f"Delete server '{info.name}' (id {info.id}, {info.game_name}): its container is removed and it "
                 "disappears from GameDock. ")
            if delete_data:
                s += (f"Its data folder ({data_phrase(server_id)}) will ALSO be deleted for good. Before that, "
                      "GameDock saves an automatic backup of the whole data folder (folder "
                      f"'{BACKUPS_DIRNAME}/{DELETED_DIRNAME}' inside the GameDock data folder); if that backup "
                      "fails, nothing is deleted. Other backups of this server are not touched.")
            else:
                s += f"Its data folder ({data_phrase(server_id)}) is KEPT on disk."
            return s

        return two_step("delete_server", args, confirmation_token, describe,
                        lambda: delete_result_to_dict(service.delete(
                            server_id, confirm=True, delete_data=delete_data, confirm_delete_data=delete_data)))

    @tool(DESTRUCTIVE, _ann(destructive=True))
    def restore_backup(
        server_id: ServerId,
        backup_name: Annotated[str, Field(description="Backup file name from list_backups")],
        confirmation_token: Annotated[str | None, Field(
            description="Leave empty on the first call. On the second call pass the token you were given.")] = None,
    ) -> Result:
        """DESTRUCTIVE, two steps. Replace a stopped server's data with a backup. The first call changes nothing
        and returns a summary plus a confirmation_token; the second call with that token does it."""
        args = {"server_id": server_id, "backup_name": backup_name}

        def describe() -> str:
            info = stopped_server(server_id, "restore a backup")
            b = find_backup(server_id, backup_name)
            return (f"Restore backup '{b.name}' ({b.kind}, made {b.created_at}, {_size(b.size)}) onto server "
                    f"'{info.name}' (id {info.id}). The server's CURRENT data ({data_phrase(server_id)}) will be "
                    "replaced by the backup's contents. First, GameDock saves an automatic 'pre-restore' backup of "
                    "the current data in this server's backups list; if that fails, nothing is restored.")

        return two_step("restore_backup", args, confirmation_token, describe,
                        lambda: to_plain(service.restore_backup(server_id, backup_name, confirm=True)))

    @tool(DESTRUCTIVE, _ann(destructive=True))
    def delete_backup(
        server_id: ServerId,
        backup_name: Annotated[str, Field(description="Backup file name from list_backups")],
        confirmation_token: Annotated[str | None, Field(
            description="Leave empty on the first call. On the second call pass the token you were given.")] = None,
    ) -> Result:
        """DESTRUCTIVE, two steps. Permanently delete one backup file. The first call changes nothing and returns
        a summary plus a confirmation_token; the second call with that token does it."""
        args = {"server_id": server_id, "backup_name": backup_name}

        def describe() -> str:
            b = find_backup(server_id, backup_name)
            return (f"Permanently delete backup '{b.name}' ({b.kind}, made {b.created_at}, {_size(b.size)}) of "
                    f"server '{server_id}'. No other copy is kept and this cannot be undone. The server and its "
                    "data are not touched.")

        def run() -> Result:
            service.delete_backup(server_id, backup_name, confirm=True)
            return {"deleted": backup_name}

        return two_step("delete_backup", args, confirmation_token, describe, run)

    @tool(DESTRUCTIVE, _ann(destructive=True))
    def wipe_server_data(
        server_id: ServerId,
        confirmation_token: Annotated[str | None, Field(
            description="Leave empty on the first call. On the second call pass the token you were given.")] = None,
    ) -> Result:
        """DESTRUCTIVE, two steps. Delete everything in a stopped server's data folder but keep the server (it
        starts fresh). The first call changes nothing and returns a summary plus a confirmation_token."""
        args = {"server_id": server_id}

        def describe() -> str:
            info = stopped_server(server_id, "wipe its data")
            return (f"Wipe ALL data of server '{info.name}' (id {info.id}): {data_phrase(server_id)} in its data "
                    "folder will be deleted, so it starts like a brand new server. The server itself, its settings "
                    "and its existing backups stay. First, GameDock saves an automatic 'pre-wipe' backup of the "
                    "data in this server's backups list; if that fails, nothing is wiped.")

        return two_step("wipe_server_data", args, confirmation_token, describe,
                        lambda: to_plain(service.wipe_data(server_id, confirm=True)))

    return defs


def wrap_with_audit(d: ToolDef, audit: AuditLog) -> Callable:
    """Wrap a tool so every call is recorded (arguments masked) and expected errors become clean tool errors."""
    fn = d.fn

    @functools.wraps(fn)
    def wrapper(**kwargs):
        started = time.monotonic()
        client = current_client.get()
        try:
            result = fn(**kwargs)
        except TokenRejected as e:
            audit.record(d.name, kwargs, "token_rejected", str(e), client)
            raise
        except ToolError as e:
            audit.record(d.name, kwargs, "error", str(e), client)
            raise
        except GameDockError as e:
            audit.record(d.name, kwargs, "error", str(e), client)
            raise ToolError(str(e)) from None
        except Exception:
            log.exception("MCP tool %s failed unexpectedly", d.name)  # message only; arguments are not logged
            audit.record(d.name, kwargs, "crashed", "unexpected internal error (see the GameDock log)", client)
            raise
        status = result.get("status") if isinstance(result, dict) and result.get("status") in (
            "confirmation_required",) else "ok"
        audit.record(d.name, kwargs, status, "", client)
        log.debug("MCP tool %s %s in %.0f ms", d.name, status, (time.monotonic() - started) * 1000)
        return result

    return wrapper


class AuditedMCPServer(MCPServer):
    """MCPServer that also audits calls rejected before a tool runs (unknown tool, arguments that do not
    fit the schema). Calls that reach a tool are audited by the tool wrapper."""

    def __init__(self, *args, audit: AuditLog, **kwargs):
        super().__init__(*args, **kwargs)
        self._audit = audit

    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as e:
            if isinstance(e.__cause__, ValidationError):
                fields = sorted({".".join(str(p) for p in err["loc"]) for err in e.__cause__.errors()})
                self._audit.record(name, arguments, "invalid_arguments", "rejected fields: " + ", ".join(fields),
                                   current_client.get())
            elif str(e).startswith("Unknown tool"):
                self._audit.record(name, arguments, "unknown_tool", "", current_client.get())
            raise


def register_tools(server: MCPServer, defs: list[ToolDef], mode: str, audit: AuditLog) -> list[str]:
    """Register the tools allowed in `mode` on `server`; others are not listed at all. Returns their names."""
    allowed = MODE_TIERS.get(mode, ())
    names = []
    for d in defs:
        if d.tier in allowed:
            server.tool(name=d.name, annotations=d.annotations)(wrap_with_audit(d, audit))
            names.append(d.name)
    return names
