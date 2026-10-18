# Changelog

All notable changes. Dates are 2026.

## Unreleased: review and polish pass

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
- No login or TLS; no `docker logs` viewer; SELinux relabeling is not handled; Linux hosts only.

## 0.1.0 (steps 1 to 5)

- Catalog of YAML game templates; deploy several servers per game on different ports.
- Start, stop, restart, edit settings, update image, delete with confirmations.
- Backups with retention, restore with safety backup, wipe, delete with data.
- Config file editor with masked secrets.
- Monitoring: live CPU, memory, network, host totals, optional Prometheus `/metrics`.
- Built-in MCP endpoint (`/mcp`) with modes, two-step confirmations, token, Host/Origin checks and an audit log.
