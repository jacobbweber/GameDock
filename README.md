# GameDock

A small, self-hosted game hosting manager for home use. Each game server runs in its own Docker container (SteamCMD and Wine based images work well). GameDock gives you a simple web dashboard to see and control them, and is built so a built-in MCP endpoint for AI agents can be added on top of the same service layer.

GameDock is not tied to any particular machine or game: games are described by small YAML files in `catalog/`.

![Dashboard](docs/gamedock-step2-dashboard.png)
![Catalog](docs/gamedock-step2-catalog.png)

**Status:** step 2 of the plan. Browse a catalog of game templates, deploy servers from them (several of the same game can coexist on different ports), edit their settings, start / stop / restart, and delete. Planned next: backups and updates (step 3), resource monitoring, MCP endpoint.

## Concepts

- **Catalog entry**: a game template in `catalog/*.yaml` (image, default ports, volumes, the settings you can fill in).
- **Server**: a deployed instance made from a catalog entry. It has its own name, host ports and settings, and is stored in `data/instances.json` (created with mode 0600 because it can contain passwords; `data/` is git-ignored). A server keeps a snapshot of its template, so later catalog edits never break it.

## Run it

Requires Python 3.10+ and, for real use, Docker (the `docker` command must work for the user running GameDock).

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt
cp .env.example .env        # optional; edit as needed
python -m gamedock          # then open http://127.0.0.1:8080
```

Try it without Docker using the pretend in-memory engine:

```bash
GAMEDOCK_BACKEND=fake python -m gamedock
```

### Settings (environment variables or `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `GAMEDOCK_HOST` | `127.0.0.1` | Listen address. Local only by default. Only set `0.0.0.0` if you accept that anyone who can reach the port can control your servers (no login yet). |
| `GAMEDOCK_PORT` | `8080` | Web port |
| `GAMEDOCK_CATALOG_DIR` | `./catalog` | Folder of game definition files |
| `GAMEDOCK_DATA_DIR` | `./data` | Server data and `instances.json`. Relative volume paths go under `<data>/<server id>/` |
| `GAMEDOCK_BACKEND` | `docker` | `docker` or `fake` |

## Using it

1. Open **Catalog**, press **Deploy** on a game.
2. Pick a name, check the host ports (prefilled with the defaults; a port already used by another server is refused) and fill in the settings. Password-type settings use password inputs and are never shown again, logged, or returned by the API.
3. Tick **Start after deploy** if you want it to run right away. Deploy creates the container either way.
4. On the **Dashboard**: Start / Stop / Restart, **Edit** (name, ports, settings; server must be stopped, its container is recreated; leave a password blank to keep the stored one) and **Delete**.

**Delete** needs explicit confirmation (UI dialog, and `confirm=true` in the API). It removes the container and keeps the data folder. Deleting the data folder too needs a second confirmation and is refused for now with a message that backups arrive in step 3. A running server must be stopped first.

## Adding a game

Drop a `.yaml` (or `.json`) file in `catalog/`; it appears in the Catalog page without a restart.

```yaml
id: my-game              # lowercase letters, digits, - and _
name: My Game Server
image: some/image:tag
description: One line shown on the card.
notes: Shown in amber on the card (use it for warnings).
ports:                   # defaults; each server can choose its own host ports
  - { protocol: udp, host: 27015, container: 27015 }
volumes:
  - { host: ./saves, container: /saves }   # relative = under <data>/<server id>/
env:                     # fixed values, not editable in the deploy form
  SOME_FLAG: "1"
fields:                  # settings shown in the deploy/edit form
  - { name: SERVER_NAME, label: Server name, default: "My Server", required: true }
  - { name: SERVER_PASSWORD, label: Password, secret: true }   # secret = password input
```

Never put real passwords in catalog files (they are committed to git); users type them into the deploy form.

**The bundled entries (Enshrouded, Palworld, Valheim, Minecraft Java, Satisfactory) are examples.** Their images, ports and variables are written from general knowledge of popular community images and have not been tested here. Check each against the image's own documentation before real use.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/catalog`, `/api/catalog/{id}` | game templates |
| GET | `/api/servers`, `/api/servers/{id}` | deployed servers with status |
| POST | `/api/servers` | deploy: `{game_id, name, ports:[{protocol,container,host}], env:{}, start}` |
| PATCH | `/api/servers/{id}` | edit `name`, `ports`, `env` (stopped servers only) |
| POST | `/api/servers/{id}/start` `/stop` `/restart` | lifecycle |
| DELETE | `/api/servers/{id}?confirm=true` | delete; `delete_data=true&confirm_delete_data=true` is refused until step 3 |
| GET | `/api/health` | liveness |

Errors are `{"error": "..."}` with 400 (invalid), 404 (unknown), 409 (conflict, e.g. port in use or server running), 422 (malformed request) or 502 (Docker problem). Interactive docs at `/api/docs`.

## Layout

```
gamedock/
  models.py        plain data types (catalog entry vs server instance)
  catalog.py       YAML template loader + validation
  store.py         persistent JSON store of deployed servers
  service.py       all management logic (web and future MCP call this)
  backends/        container engine interface: docker_cli.py (real), fake.py (in-memory)
  web/             FastAPI app + static dashboard (vanilla JS, no build step)
catalog/           game templates (examples)
tests/             pytest suite (no Docker needed)
```

## Tests

```bash
pip install -e '.[dev]'
pytest
```

## Safety notes

- Binds to `127.0.0.1` unless you configure otherwise.
- Environment values are handed to Docker through the process environment, not on the command line.
- State-changing requests from a different web origin are refused.
- `.env`, `data/` (including `instances.json`, mode 0600) and `backups/` are git-ignored.
- Secret settings are never returned by the API, shown in the UI after saving, or included in logs/`repr`.
