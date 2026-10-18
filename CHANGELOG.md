# Changelog

All notable changes. Dates are 2026.

## 0.4.1: Enshrouded fixes from the first real test

Found by deploying the bundled Enshrouded entry on a real host: the game asked for a password although the password box was empty, and the Config editor had no file to open.

### Fixed

- **Enshrouded password.** The image only applies `SERVER_PASSWORD` when it is not empty, so an empty box left the game's own generated 8 character password in place. The entry now uses the role variables the image really reads: the field is `SERVER_ROLE_0_PASSWORD` (label "Server password", secret, empty = no password) and fixed `SERVER_ROLE_0_NAME=Default` and the other `SERVER_ROLE_0_*` rights are always set. The image writes a role setting whenever its variable *exists*, so an empty password now means an empty password.
- **Empty values reach the container.** GameDock already passed every setting, empty ones too; the docker command now writes an empty value explicitly as `-e NAME=` (present but empty) instead of relying on docker copying it from its own environment. Non-empty values still never appear on the command line.
- **Enshrouded settings file.** `/opt/enshrouded` (the game install, about 7 GB, and `server/enshrouded_server.json`) is now a volume (`./game`), so the file can be edited from the Config page and a recreated container does not download the game again. Added `PUID`/`PGID` 1000, `SERVER_QUERYPORT`, a 90 second stop timeout and a note that the file only appears after the first start (several minutes while 7 GB download; the tile shows Installing).
- **Enshrouded ready line.** Ready is now the periodic `Session ----` block of the real log (`Config Parsed` appears before the game listens, so it is not used). The progress pattern is kept; the early "Missing configuration" updater message on first start is documented as harmless.

### Added

- **Nested JSON in the settings form.** Settings inside objects and arrays of objects are fields with dotted labels (`gameSettings.playerHealthFactor`, `userGroups[0].password`), grouped under their parent; empty containers and arrays of plain values stay one raw-JSON field. Edits replace only the changed value text, so tabs and layout stay byte for byte (round trip tested with a file shaped like Enshrouded's). Sensitive keys match at any depth, so every group's password is masked.
- **Catalog `installed_build_file`** (optional): a small version file inside the server's data folder to use as the installed version for images that keep no Steam `appmanifest` (Enshrouded keeps `current_version`). A number is the Steam build id and is compared; other text is shown but "cannot be compared", and the dialog says so. Read first, falls back to the Steam record; symlinks are not followed.
- **Catalog `stop_timeout`** (optional, 1 to 600 seconds): how long this game gets to save when stopped, instead of `GAMEDOCK_STOP_TIMEOUT`. Used for the container's stop timeout and for `docker stop`.

### Changed

- Version 0.4.1 (only the version line of `pyproject.toml`; no dependency changes, so the header update button accepts it).

### Decisions and known gaps

- **Existing servers keep their snapshot.** A server deployed before this release keeps its old fixed `env`, fields, volumes and stop timeout; only the read-only hints (including `installed_build_file`) come from the current entry. To get the fix, delete the Enshrouded server in GameDock (it takes the safety backup) and deploy it again.
- The catalog has no field for the time zone, so `TZ` is not set (log times are UTC). The new `./game` folder is created by GameDock's own user and the image runs as uid 1000: that matches only when GameDock itself runs as uid 1000.
- What `game/current_version` holds is not confirmed. If it is not a Steam build number the tile shows the text and "Could not check".
- On an edit, a blank secret keeps the stored value (by design), so a password that was set cannot be cleared to "none" from the form; redeploy or set it to a new value.

## 0.4.0: each game's own version, and updating the game

Asked for: per game, show the game's version and whether a newer one is out, with an Update now button (scheduled maintenance already applies updates). Before this, GameDock only compared container image digests, which for SteamCMD games says nothing about the game files (the image downloads those at every start).

### Added

- **Game version panel per Steam server.** Installed Steam build id and its date (from `steamapps/appmanifest_<app id>.acf`, found by a bounded search of the server's volumes or the entry's `steam_manifest_dir`), the game's own version text from the log (`version_log_pattern`, last match, remembered with the build it belongs to in `<data>/game-versions.json`), the newest public build id and its date, and one status: **Up to date**, **Update available**, **Could not check**, **Unknown (no Steam id for this game)**.
- **Newest build lookup.** `GET https://api.steamcmd.net/v1/info/<app id>` (a public third-party mirror of Steam data; `data.<appid>.depots.branches.<branch>.{buildid,timeupdated}`). Address from `GAMEDOCK_STEAM_INFO_URL` (`{appid}` placeholder). Cached 1 hour in memory and in `<data>/steam-latest.json` (so a restart does not ask again), failures retried after 10 minutes, one **Check now** per 20 seconds, failures and rate limits are quiet ("Could not check"). Showing a tile never uses the network; the scheduler refreshes one stale game per tick. `GAMEDOCK_UPDATE_CHECK=false` switches it off.
- **Tile and Updates dialog.** A tile line such as "Game version v1.0.5.102999, up to date" and an **Update available** badge for a game update (purple), distinct from the image update badge (blue); with both, one badge and the dialog has the details. The **Update** button is now **Updates**: the dialog shows the game section (status, facts, Check now, how it works, **Update game now**) and an *image* section that opens the existing image update.
- **Update game now** (`game_update_method: restart`): stop, safety backup, start, then wait and re-read the installed build id. Reports plainly when the build did not change ("the game did not update on restart; check the logs"), when the server stopped, or when the wait ran out. Needs a running server (a stopped one updates when started). Shows an "Updating game" activity on the tile.
- **Scheduled maintenance** with *apply updates*: asks for the game status; if a newer game version exists the restart installs it (backup first, even when *restart* is not ticked) and the run result records "updated from build A to build B" or that it did not update (a failed run). With no update it just restarts when *restart* is ticked. A failed check does not stop the run.
- **Catalog fields** (all optional, validated when the catalog loads): `steam_app_id`, `steam_branch`, `steam_manifest_dir`, `version_log_pattern` (one capture group), `game_update_method` (`restart`, the default when a `steam_app_id` is set, or `none`). Added to Palworld (2394010, with the version pattern and `UPDATE_ON_BOOT=true`), Enshrouded (2278520, **verify before use**: its install folder must be a volume for the installed build to be readable), Valheim (896660) and Satisfactory (1690800, **verify before use**: files live in `/config/gamefiles`). Minecraft is not a Steam game and has none. Servers deployed by older versions pick the new settings up from the current catalog entry.
- **API and MCP.** Server status gains `steam_app_id`, `game_version`, `installed_build`, `latest_build`, `game_update_status`, `game_update_method`. Endpoints `GET /api/servers/{id}/game-update`, `POST .../game-update/check`, `POST .../update-game?confirm=true`. MCP tools `check_game_update` (read-only and above) and `update_game` (standard and full; the safe flow, nothing destructive). Tool counts are now 14 (`read_only`), 25 (`standard`), 29 (`full`). The guide resource explains game versus image updates. Calls are in the audit log as before.
- Settings: `GAMEDOCK_STEAM_INFO_URL`.
- The fake backend can pretend a self-updating Steam game (`simulate_steam_game`). Screenshots: `docs/gamedock-040-*.png` from `scripts/make_screenshots_game.py`.

### Changed

- Version 0.4.0. The catalog `update_note` texts of the Steam games now describe both kinds of update. The maintenance option *apply updates* is available for games that have either an image update or a game update.
- **GameDock's own update button** now refuses a `pyproject.toml` change only when the dependency lines changed, not when only the `version` line moved. (The 0.3.0 button still refuses this release, see below.)

### Decisions and known gaps

- **Updating to 0.4.0 has to be done by hand** (`git pull --ff-only`, restart): `pyproject.toml` changed (version line) and the 0.3.0 button refuses any such change. No new packages. See docs/DEPLOYMENT.md.
- The newest build comes from a third-party mirror of Steam's data, not Valve; if it is down or changes shape the status says "Could not check". The public branch is compared unless `steam_branch` says otherwise. Builds are compared as numbers: an installed build newer than the public one (a beta) counts as up to date.
- The installed build is only readable when the game's `steamapps` folder is inside one of the server's volumes. The Enshrouded entry's current volumes do not include the install folder, so its installed build will show "could not find the Steam record" until the folder is added as a volume; the entry is marked *verify before use*. GameDock does not read files inside the container.
- The game version text depends on a log line: only Palworld has a pattern; the others show the build id (add `version_log_pattern` after checking the game's log).
- *Update game now* relies on the image updating the game at start. It checks the result and says when that did not happen; it cannot install a game update by itself. Players are disconnected; "only when nobody is playing" still does not exist.
- The call waits up to 20 minutes for the new files (a first big download); the web page and MCP call stay open meanwhile and the tile shows "Updating game". If the wait runs out the result says so and the game's status updates later by itself.
- Tested with the fake engine, real Palworld manifest and Steam JSON samples, not with a real Steam update or Docker.

## 0.3.0: schedules, update indicators and self-update

Asked for: backups and maintenance on a schedule, an update indicator, and a version display with an update button.

### Added

- **Version and update indicator in the header.** `Version 0.3.0 (abc1234)` and, when GitHub is ahead of this copy, an **Update available** badge. The version dialog shows the commit, branch, install folder, when it last checked, and a **Check now** button. Checks read `api.github.com/repos/<GAMEDOCK_UPDATE_REPO>` at most every 6 hours (30 minutes after a failure), are cached, work offline (the dialog says it could not check) and are switched off by `GAMEDOCK_UPDATE_CHECK=false`.
- **Update GameDock button** (web only, after a confirmation): `git pull --ff-only` in the install folder, then a restart of GameDock only (game servers keep running); the page reconnects and shows the new version. Refuses, changing nothing, when the folder is not a git checkout on a branch, has local changes, would not fast-forward, an action or scheduled job is running, or `pyproject.toml` changed (needs `pip install -e .` by hand). New actions are blocked for the few seconds of the update. Every refusal shows the exact manual commands. `GAMEDOCK_SELF_UPDATE=false` hides the button.
- **Image update indicator per server.** The tile shows **Update available** and an **Update now** button (the existing safe update flow). Detection compares the registry digest (`docker buildx imagetools inspect`, falling back to `docker manifest inspect`) with the digest of the image the container runs, without pulling. Cached for hours, one registry question per server per 30 seconds, memory only. The Update dialog has a per-server **Check now**, plain help text for SteamCMD images, and the catalog `update_note`. `update_available` is in the API and the MCP status tools.
- **Scheduled backups** per server: every N hours, daily, or weekly on chosen days at a time, with retention. Honour `save_command` and `backup_stop_first`. Shown as *scheduled* in the backups list; last run, next run and last result are shown; failures are visible. A missed backup runs once at the next start.
- **Scheduled maintenance** per server: daily or weekly, with checkboxes for backup first (`pre-maintenance` backup), apply image update, and restart. **Run maintenance now** with a confirmation. "Only when nobody is playing" is shown as unavailable with the reason. One action per server at a time: a job that finds the server busy retries at the next check and is recorded as skipped after two hours.
- **Schedules** page and a per-server panel (with the machine's timezone and recent runs), a schedule summary line on each tile ("Backups: daily at 03:00 | Maintenance: Sunday at 04:00 | next in 5 hours"), and a *Maintenance* activity label.
- A small in-process scheduler thread (30 s tick). No cron, no new dependencies. Schedules persist in `data/schedules.json` (atomic write, 0600, strict validation, a damaged file is set aside and reported on the Schedules page). Daylight-saving aware.
- **MCP:** `get_gamedock_version` (read-only and above), `list_schedules` (read-only and above), `set_backup_schedule` and `set_maintenance_schedule` (standard and full). Tool counts are now 13 (`read_only`), 23 (`standard`), 27 (`full`). Self-update is intentionally not an MCP tool. Scheduled runs and their results are written to the audit log; the guide resource describes updates and schedules.
- Endpoints: `GET /api/version`, `POST /api/gamedock/update-check`, `POST /api/gamedock/update`, `GET /api/servers/{id}/image-update`, `POST /api/servers/{id}/image-update/check`, `GET /api/schedules`, `GET /api/servers/{id}/schedules`, `PUT .../schedules/backup`, `PUT .../schedules/maintenance`, `POST .../schedules/{kind}/run`.
- Settings: `GAMEDOCK_SCHEDULER`, `GAMEDOCK_UPDATE_CHECK`, `GAMEDOCK_UPDATE_REPO`, `GAMEDOCK_SELF_UPDATE`.
- Catalog: `update_note`, `save_command`, `backup_stop_first`.
- Backend interface: `remote_image_digest`, `local_image_digests`, `exec_in` (a custom backend must add them).
- Screenshots: `docs/gamedock-030-*.png`; `scripts/make_screenshots.py` now also exercises the update badges, schedules and maintenance dialog.

### Changed

- Version 0.3.0. Deleting a server also removes its schedules. The MCP page's activity heading now says "AI agents and scheduled runs".

### Decisions and known gaps

- **Systemd unit files are unchanged.** The restart uses `systemctl restart`, or exit code 75, which both `Restart=on-failure` (as shipped) and `Restart=always` act on. See docs/DEPLOYMENT.md. The first update to 0.3.0 has to be done by hand because 0.2.0 has no button.
- A missed *maintenance* more than 6 hours late is skipped (so a restart never happens at a surprise time); a missed backup always runs once.
- "Only when nobody is playing" is not available: there is no generic, game-agnostic way to count players.
- The self-update does not install packages. If `pyproject.toml` changes it refuses and says to run `pip install -e .`.
- With the hardened system unit (`ProtectSystem=strict`) the install folder is read-only, so the button will report the git error; update by hand there.
- The real Docker paths for registry digests (`buildx imagetools`, `manifest inspect`) and `docker exec` are covered by fake-runner tests, not a real engine or registry. The `update_note` and `save_command` texts in the catalog are written from the images' documentation and are unverified.
- Image update checks are kept in memory only and start empty after a restart.

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
