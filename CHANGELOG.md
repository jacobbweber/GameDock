# Changelog

All notable changes. Dates are 2026.

## 0.2.0: progress, honest readiness, logs and a settings form

Driven by the first real test (deploying Palworld): the deploy button greyed out with no feedback for 30 seconds, the dashboard said "Running" while the game was still downloading about 5 GB (so joining timed out), there was no way to see logs, and the config file was one huge line.

### Added

- **Progress everywhere.** Clicking Deploy shows a "Deploying" tile at once (a fast validation error still stays in the form). Start, stop, restart, update, backup, restore, wipe and delete show a spinner, a bar and a plain sentence on the card and in their dialogs. The "Working" badge is larger, animated and has a coloured border. The tile updates in place, so animations do not restart on every refresh. The server also reports what it is doing (`activity`), so a page reload or an MCP client sees it too.
- **Readiness: Starting / Installing NN% / Ready to join.** Based on Docker's `State.Health.Status`, and for images without a health check on two optional catalog fields, `ready_log_pattern` and `progress_log_pattern` (one capture group = the percentage). With neither, behaviour is as before (Running = ready), except that a visible download below 100% still counts as installing. A download in progress outranks an `unhealthy` health check (Palworld's check goes unhealthy during a long first download). Exposed as `ready`, `phase`, `progress`, `phase_detail`, `activity` in the API, in the MCP `list_servers` / `get_server_status`, on the dashboard tile and in the log viewer header. Log reads are throttled (2 s per server) and skipped for healthy containers.
- **Log viewer.** **Logs** button on each server: 100 to 1000 lines (200 default), Follow (3 s), Refresh, Copy, ANSI colours removed, carriage-return progress lines collapsed, secret values masked. `GET /api/servers/{id}/logs`, backend method `get_logs` (Docker and fake), MCP tool `get_server_logs` (read-only; in `read_only`, `standard` and `full` modes; 100 lines default, 500 max, 40 000 characters max). Tool counts are now 11 (`read_only`), 19 (`standard`), 23 (`full`).
- **Settings form for config files.** Form view with labelled fields, switches for True/False, number and text inputs, password inputs for `sensitive_keys`, search, "Only changed" filter, highlighted changes with `was:` and Undo, a review list, and a toggle to the raw text. Parsers for Palworld-style `OptionSettings=(...)` (commas and escaped quotes inside quoted values, nested lists), `key=value` ini / properties (sections, comments) and flat JSON. Edits replace only the changed value's characters, so the rest of the file is preserved byte for byte. New read-only endpoint `POST /api/servers/{id}/config/form`. Saving uses the existing path, so `.bak` copies, traversal protection, secret masking and `hot_edit` are unchanged.
- The raw editor wraps long lines (it used to be one endless line).
- Catalog: `ready_log_pattern`, `progress_log_pattern` (validated at load time) and `note` on config files (warning above the editor). Palworld and Enshrouded entries use them. Palworld also gets a `DISABLE_GENERATE_SETTINGS` field ("Keep hand-edited settings file"), because that image rewrites `PalWorldSettings.ini` from environment variables on every start.
- A missing settings file now says: "This game has not created its settings file yet; it appears after the first full start."
- `scripts/make_screenshots.py`: browser smoke test and screenshots on the fake backend.
- Screenshots: `docs/gamedock-020-*.png`.

### Changed

- Version 0.2.0 (`pyproject.toml`, `/api/health`).
- Backend `inspect` format also returns health and start time; `ContainerBackend` has a new `get_logs` method (a custom backend must add it).
- The dashboard keeps refreshing while an action runs or a dialog is open.

### Known gaps

- The ready / progress patterns are written from the images' documentation, not yet checked against real logs on a real machine. The Enshrouded pattern in particular is unverified.
- The real Docker backend (health, `docker logs --since`) is covered by command tests and fake `docker` scripts, not a real engine.
- A server deployed before this version keeps its old template: redeploy (or recreate it) to get new catalog fields such as `DISABLE_GENERATE_SETTINGS`. Readiness patterns are read from the current catalog, so they work for existing servers straight away.
- The MCP `write_config_file` tool is still text based; only the web page has the form view.
- Patterns are regular expressions written by whoever authors a catalog entry (trusted input, like the rest of the catalog).

## 0.1.x: review and polish pass

### Fixed (found in review of the real Docker backend, which has not yet run against a real engine)

- **High:** a container that already existed with the same name (`gamedock-<id>`) but was not made by GameDock would have been silently adopted, and then started, stopped or removed. GameDock now labels its containers (`gamedock.managed=true`) and refuses to touch any without the label.
- **High:** `docker create` had a 90 second limit, but it also downloads missing images (often several GB), so the first deploy of a big game would have been cut off. The limit is now one hour; quick checks keep short limits.
- **Medium:** plain `docker inspect` also matches images and volumes; now `docker container inspect`.
- **Medium:** catalog entries could set environment names such as `PATH`, `DOCKER_HOST` or `LD_PRELOAD`, which would change how the docker command itself runs. These names are now rejected (in the catalog and in the backend).
- **Medium:** the web page could be reached through a DNS-rebinding trick that defeated the same-origin check. It now only answers to known Host names (`GAMEDOCK_ALLOWED_HOSTS`), cannot be framed, and sends a content-security policy.
- **Medium:** game containers had no stop grace period of their own (host shutdown used Docker's 10 s). Added `--stop-timeout` and `GAMEDOCK_STOP_TIMEOUT` (default 60 s).
- **Medium:** `docker rm -f` could kill a running container. Now plain `rm` (only paused or dead containers are forced); paused containers are unpaused on start.
- **Medium:** backup archives were world-readable although they contain configs with passwords. Now 0600 in a 0700 folder; `instances.json.bak` is created 0600 as well.
- **Medium:** `-v host:container` could auto-create a missing host folder as root and breaks on `:` in paths. Now `--mount type=bind,...`.
- **Low:** raw Docker errors replaced by plain sentences with a fix (daemon down, permission, port in use, disk full, image not found, rate limit, wrong CPU type, name taken).
- **Low:** host ports are checked (by trying to bind them) before a server starts, so "port already in use" is reported clearly instead of as a half-created container.
- **Low:** two deploys at once could race on the name and port checks; deploys are now serialized.
- **Low:** one broken catalog file hid every game; broken files are now skipped and reported.
- **Low:** the server list was not flushed to disk and had no recovery copy; now fsync, atomic rename and an `instances.json.bak`, with an actionable message if it is damaged.
- **Low:** file-owner problems (game container writing as root) now produce a quoted `chown` command plus the `PUID` / `PGID` explanation; "disk full" is no longer reported as a "damaged backup"; restore warns when the old files had other owners.
- Smaller: UTF-8 made explicit everywhere; crash-looping servers still show as running so Stop works, and the card says why a server exited (exit code or out of memory).

### Added

- `python -m gamedock doctor` (and `--json`): checks Docker, the data folder, disk space, file owners, catalog files, host ports, web address and MCP, in plain language with fixes. Exit code 1 when something must be fixed.
- `GET /api/doctor` and `GET /api/status`; a **Health** page in the UI and a banner on every page when Docker is unreachable or a catalog file is broken.
- Deployment examples in `deploy/` (systemd user and system units, settings file) and an optional `Dockerfile` + `docker-compose.yml`, with the security implications documented in `docs/DEPLOYMENT.md`.
- `catalog/smoke-test.yaml`: a tiny harmless test container for the first run on a real machine.
- Settings `GAMEDOCK_STOP_TIMEOUT` and `GAMEDOCK_ALLOWED_HOSTS`.
- UI: loading and empty states, connection-lost banner, labelled buttons and dialogs, skip link, page titles, favicon, phone-sized layout.
- Docs: restructured README, `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/NIMO_FIRST_TEST.md`.

### Known limits

- The real Docker backend is covered by command-construction tests and fake `docker` scripts, not yet by a real engine.
- No login or TLS; SELinux relabeling is not handled; Linux hosts only.

## 0.1.0 (steps 1 to 5)

- Catalog of YAML game templates; deploy several servers per game on different ports.
- Start, stop, restart, edit settings, update image, delete with confirmations.
- Backups with retention, restore with safety backup, wipe, delete with data.
- Config file editor with masked secrets.
- Monitoring: live CPU, memory, network, host totals, optional Prometheus `/metrics`.
- Built-in MCP endpoint (`/mcp`) with modes, two-step confirmations, token, Host/Origin checks and an audit log.
