"""The short guide AI agents can read (an MCP resource and prompts)."""
from __future__ import annotations

GUIDE_URI = "gamedock://guide"

MODE_TEXT = {
    "read_only": "Read-only: you can look at things (catalog, servers, stats, backups, config files) but not change them.",
    "standard": "Standard: you can deploy, start, stop, restart, change settings, update images, make backups and "
                "edit config files. Destructive tools (delete, restore, wipe, delete backup) are NOT available.",
    "full": "Full: everything in Standard, plus the destructive tools, each behind a two-step confirmation.",
}


def guide_text(mode: str, tool_names: list[str], ttl_seconds: int) -> str:
    return f"""# GameDock guide for AI agents

GameDock is a small self-hosted manager for game servers. Each server runs in its own Docker container,
and GameDock works for any game: a game is just an entry in the catalog.

## Ideas
- **Catalog entry**: a template for a game (image, default ports, settings). See `list_catalog`.
- **Server**: something deployed from a catalog entry. It has an id (use it in every tool), a name, ports,
  settings and a status: `running`, `stopped`, `not_created` or `error`.
- **Ready vs running**: `running` only means the container is up. A game that downloads files on first start
  (several GB) cannot be joined yet. `ready` is true once the game can be joined; `phase` says `starting`,
  `installing` (with a `progress` percent) or `ready`. Wait for `ready` before telling anyone to connect.
- **Settings (env)**: values you fill in when deploying (see `get_catalog_entry`). Settings marked `secret`
  (passwords) are accepted but never shown back: you only see whether they are set. Never ask for or repeat them.
- **Backups**: archives of a server's data. GameDock makes automatic safety backups before risky actions
  (restore, wipe, delete with data, image update).
- **Config files**: only files listed by `list_config_files` can be read or written. Sensitive values appear
  as placeholders; writing the placeholder back keeps the existing value.

## Current mode: {mode}
{MODE_TEXT.get(mode, "")}
Tools you have: {", ".join(tool_names)}.

## Typical flows
- Deploy: `list_catalog` -> `get_catalog_entry` -> `deploy_server` (give required settings, `start=true` to start).
- Check on things: `list_servers`, `get_server_status`, `get_server_stats`, `get_stats_history`, `get_host_stats`.
- Something wrong or slow to start? `get_server_logs` shows the newest console lines.
- Before risky changes: `create_backup`.
- Port conflicts and other problems come back as plain error messages: read them and adjust.

## Destructive tools: the two-step confirmation (full mode only)
`delete_server`, `restore_backup`, `delete_backup` and `wipe_server_data` never act on the first call.
1. Call the tool normally. It changes nothing and returns `status: confirmation_required`, a plain-language
   `summary` of exactly what would happen (including the automatic backup) and a `confirmation_token`.
2. **Show the summary to the human and get their agreement.** Do not confirm on your own.
3. Call the same tool again with exactly the same arguments plus `confirmation_token`. Only then it happens.
The token works once, expires after about {ttl_seconds // 60} minutes, and only fits those exact arguments.
Changing any argument, reusing it or waiting too long gives an error; just start again from step 1.
A running server must be stopped (`stop_server`) before delete, restore or wipe.

## Good manners
- Prefer the least destructive option. Take a backup first when unsure.
- Every call is written to an audit log the owner can read.
"""
