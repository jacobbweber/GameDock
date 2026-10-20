# Changelog

All notable changes. Dates are 2026.

## 0.6.2: root-owned files no longer block backups or deletes, and games run as GameDock's user

Built from a real case: deleting a Valheim server together with its data stopped with "Could not delete .../data/weber-brothers-2/config: permission denied (belongs to root)". The Valheim image runs the game as root unless told otherwise (its `PUID` defaults to 0), so the files it wrote into the mounted folder belonged to root, and GameDock (an ordinary user in the docker group, no sudo) could back them up only partly and not delete them. It was fixed by hand with a one-off `docker run ... chown`. GameDock now does that itself, and the bundled games no longer write root-owned files in the first place.

### Added

- **Automatic file owner repair (Docker backend).** When a backup, a restore, a wipe, a delete with data, or reading or saving a config file fails with "permission denied", GameDock runs a short-lived helper container that mounts **only that server's data folder** and gives every file in it to the user GameDock runs as (`chown -R -h <uid>:<gid>`), then tries the action **once** more. The helper runs with `--rm`, `--network none`, a read-only root file system, all capabilities dropped except the four chown needs, `no-new-privileges`, and never follows symbolic links. Before mounting anything GameDock checks that the folder is exactly `<data folder>/<server id>` (realpath, not a symbolic link, a plain server id); anything else is refused. The repair also runs **proactively** before a backup, restore, wipe or delete of a stopped server whose folder holds files of another user, and again right after the container is removed during a delete. The backup taken before a delete or wipe must still be complete (every file read) or nothing is deleted. If the helper itself fails, the old plain message is kept and now says what was tried and gives a command that works without sudo.
- **Helper image.** `GAMEDOCK_HELPER_IMAGE` (default `alpine:3`). An image already stored on the machine is used first (the configured one, else `alpine:3`, `alpine:latest`, `busybox:latest`, `busybox`); only if none is there is the configured image downloaded, and the result says so.
- **Check setup: File ownership row** (also in `verify_game_setup`). Passes when every file belongs to GameDock's user and says whether the game runs as that user; otherwise warns with the owner (for example "root (0)") and an example file, and the web page shows a **Fix ownership** button (non-destructive: only owners change). Web API `POST /api/servers/{id}/fix-ownership`.
- **MCP tool `fix_data_ownership`** (standard and full modes, non-destructive, recorded in the audit log). Tool counts: read_only 24, **standard 38, full 42**.
- **Catalog keys `user_env` and `run_as_user`.** `user_env: {PUID: uid, PGID: gid}` fills the image's own "run as this user" variables with GameDock's user and group id at deploy time; `run_as_user: true` starts the container with `docker --user <uid>:<gid>` (only for images that support any user). A `user_env` name may not also be in `env` or `fields`. `validate_catalog_entry`, the Review checks and `scripts/lint_catalog.py` warn when an entry writes fixed numbers into PUID/PGID/UID/GID-like variables, and when an inspected image has such variables that the entry does not map. Documented in ADDING_A_GAME (Step 4, traps 7 and 17, checklist), the agent prompt and `catalog/_template.yaml`.
- **Built-in problem `k_EResultGSLTExpired`**: "Steam says the login token has expired" with how to regenerate or recreate it, and that an account without a phone number or a limited account can make new tokens show as expired too. Also the first `fail_log_patterns` item of `docs/catalog-entries/aska.yaml`.
- `scripts/make_screenshots_ownership.py` and `docs/gamedock-062-*.png`.

### Changed

- **Bundled entries now run the game as GameDock's user** via `user_env` (each checked against the image's documentation): Valheim (`PUID`/`PGID`, image default 0 = root), Palworld (`PUID`/`PGID`, default 1000), Enshrouded (`PUID`/`PGID`, default 4711; the fixed `PUID: "1000"` / `PGID: "1000"` in `env` are replaced), Satisfactory (`PUID`/`PGID`, default 1000) and Minecraft Java (`UID`/`GID`, default 1000). The smoke-test entry is unchanged.
- **Palworld notes**: joining by address always shows an "Input password" popup whose OK button stays disabled while it is empty; with no server password, type a single space and press OK.
- The built-in "permission denied" problem now points to Fix ownership and `user_env`.
- Version 0.6.2 (only the version lines of `pyproject.toml` and `gamedock/__init__.py`; dependencies unchanged).

### Existing servers

A deployed server keeps the catalog entry it was deployed with, so **servers deployed before 0.6.2 keep running as before** (Valheim as root, Enshrouded as 1000) until they are deleted (data can be kept) and deployed again. The automatic repair and the Fix ownership button protect them in the meantime.

## 0.6.1: say why a server is stuck (known problems), a clearer ASKA entry, and an updater that copes with a file in the way

Built from a real case: Jacob's ASKA server installed but stayed Docker-"unhealthy", and GameDock only said "Docker's health check for this game is failing". The game's own log said `Failed to connect to Steam reason: k_EResultAccountNotFound` and `Disconnected with reason: CustomAuthenticationFailed`: Steam had refused the Game Server Login Token (a Steam Web API key looks the same and is refused). Now GameDock reads such lines and says so.

### Added

- **Known problems.** While a server runs but is not ready, GameDock scans the newest container log lines and the game's declared log files and shows the matching problem (plain title, next step and the one log line it saw, passwords and tokens hidden) on the card (badge **Problem**), in Check setup (a **Known problem** row at the top), in `get_server_diagnostics`, in the web API (`problem` on every server) and in the MCP tools `list_servers`, `get_server_status` and `verify_game_setup`. Ready servers are never scanned for a problem; a stopped server shows one only when Docker says it was killed for memory.
- **Catalog keys** `fail_log_patterns` (up to 12 of `pattern`, `title`, `advice`), `log_files` (up to 6 relative paths inside the data folder; only the last 64 KiB is read, never through a symbolic link, ignored when written before the current run started) and `builtin_fail_patterns` (default true). Validated by `validate_catalog_entry`, `add_catalog_entry`, the loader and `scripts/lint_catalog.py` (a nested repeat such as `(a+)+` is refused; lint warns about a pattern that matches almost everything, a log file outside the mounted folders, and a pattern without a hint). They are read-only hints, so **existing servers use the current catalog entry's list without being deployed again**.
- **Built-in patterns for every game** (conservative, plain language): killed for memory (OOM, exit 137 with the OOM flag), port already in use, permission denied on the data folder, disk full, SteamCMD (`state is 0x202/0x402/0x602`, `No subscription`, `Missing configuration`, `Failed to install`) and a refused Steam login or token. The entry's own patterns come first.
- **Field rules** `pattern` and `pattern_hint` on a setting: the whole value must match, checked at deploy and edit time with the hint as the message; the value is never repeated in an error. Also current for older servers.
- A clearer message for an unhealthy server without a known problem: it points to **Logs** and **Check setup**.
- `docs/catalog-entries/aska.yaml`: the corrected ASKA entry (see below). `scripts/make_screenshots_problems.py` and `docs/gamedock-061-*.png`.
- Docs: ADDING_A_GAME (new keys, two new traps), the agent prompt, `catalog/_template.yaml`, README, ARCHITECTURE.
- **Tool counts unchanged** (read_only 24, standard 37, full 41): `problem` was added to existing outputs.

### Changed

- **ASKA entry** (kept outside `catalog/`, see below): `steam_app_id` is now `3246670` (the dedicated server's app id, which is the id in `appmanifest_3246670.acf`; the token is still created for App ID 1898300, and the token field says so, with the page `https://steamcommunity.com/dev/managegameservers` and that the Steam Web API key from `/dev/apikey` is wrong); the token must be 32 hex characters; a `fail_log_patterns` entry for `CustomAuthenticationFailed` / `k_EResultAccountNotFound`; `log_files: [server/logs/aska_game_output.log]`; new settings `ASKA_MODE`, `ASKA_SEED`, `ASKA_KEEP_WORLD_ALIVE`, `ASKA_AUTOSAVE_STYLE`, `ASKA_SAVE_BACKUP` (default 1), `ASKA_KEEP_SAVES` and `TZ`. **Because the file changed, an approved AI-added ASKA entry asks for approval again** (the review gate compares the file's SHA-256); approve it again in Catalog.
- **The self-updater copes with an untracked catalog file in the way.** Before `git pull --ff-only` it moves a plain, untracked file directly in `catalog/` that the update is about to add (same name) to `<data>/catalog-displaced/<name>.<UTC time>` (an identical file is simply replaced), and puts it back if the pull fails. It never touches symlinks, tracked files or other paths, and the update result and dialog tell where the copy is. This only helps from 0.6.1 on: GameDock 0.6.0's updater still stops with "untracked working tree files would be overwritten".
- The unhealthy message now says the health check "keeps failing" and where to look. Docker marks a container unhealthy only after the image's start period plus its retries.
- Version 0.6.1 (only the version lines of `pyproject.toml` and `gamedock/__init__.py`; dependencies unchanged, so the header update button accepts it).

### Why `aska.yaml` is not in `catalog/`

Files in `catalog/` that are in the repository are trusted and need no approval, which would bypass the review gate for a game that was added by an AI agent, and a tracked `catalog/aska.yaml` would make GameDock 0.6.0's updater fail on a machine that already holds an untracked file of that name. The corrected entry is therefore shipped as `docs/catalog-entries/aska.yaml`: copy it over the existing file in `catalog/` (or let an agent call `update_catalog_entry`), then approve it in Catalog.

### Known limits

- A Game Server Login Token and a Steam Web API key are both 32 hex characters; the pattern catches typos, stray spaces and a wrong length, and the log message catches the wrong kind of key.
- A server deployed before this release keeps `steam_app_id` from its snapshot: version tracking for the existing ASKA server stays "unknown" until it is deleted (keep its data) and deployed again. The problem messages and field rules do refresh.
- An exit code 137 without Docker's out-of-memory flag shows only in Check setup and diagnostics, not on the card (a slow `docker stop` looks the same).
- Log patterns need the text to appear in the newest 600 container lines or in the end of a declared log file.
- ASKA entry details that were rebuilt without access to the original file (name, description, labels, config file note) should be compared with the copy already on the machine.

## 0.6.0: Check setup, port-conflict warnings, diagnostics and a stricter add-a-game loop

Built from a real AI-agent review (AutoReiv adding ASKA through MCP) and from the questions a person asks when a new server does not behave: *is it set up the way the entry says, is the port free, what do I paste into a chat?* Everything new is read-only: no tool approves an entry or re-records a config baseline, and nothing destructive was added.

### Added

- **Check setup** (button on every server card; `GET /api/servers/{id}/check-setup`; MCP `verify_game_setup`). A read-only checklist (OK / Check / Problem / Skipped, each with a plain "How to fix"): container (running, restarts, exit code, out of memory), Docker health check, the game's ready line, installed game version and Steam record (a wrong `steam_app_id` names the id that is really there), newest Steam build, version log line, each config file (missing, with a look-alike path suggested, empty, not parsing), each port (published, bound, taken by another program), each folder (exists, writable, mounted in the running container), file ownership, free disk, required settings, and whether the catalog entry changed after the server was deployed (a server keeps a snapshot).
- **Diagnostics** (`GET /api/servers/{id}/diagnostics?lines=`, MCP `get_server_diagnostics`, **Copy diagnostics** button in the Check setup dialog): status, container facts from `docker container inspect`, ports, settings (secrets only as set / not set), data size, disk, backups, update checks and the newest log lines, with known secret values, `password=` / `token=` shapes, bearer tokens and URL credentials replaced by `********`.
- **Port conflicts.** `check_ports` (MCP, `POST /api/ports/check`): a port used by another GameDock server (conflict), by another program on the machine (conflict, real Docker backend only) or by another catalog game's default (information), duplicate and invalid ports, and a free port to use instead. The Deploy and Edit dialogs show it as you type with a **Use N** button. `preview_deploy` (MCP, `POST /api/catalog/{id}/deploy-preview`) is a dry run of a deploy: image, ports, folders and the container command with secrets hidden, with every problem listed at once (unapproved entry, name taken, missing required setting, port conflict). It creates nothing.
- **Review status.** `get_catalog_review_status` (MCP, `GET /api/catalog/{id}/review-status`): `waiting_for_approval`, `approved`, `changed_since_approval` or `repository`, fingerprints, timestamps, whether it can be deployed, what to do next, and which deployed servers still run an older snapshot of the entry. The documented loop is add, wait for approval, deploy a test copy, verify, fix, re-review. Deploying an unapproved entry is still refused everywhere.
- **`inspect_image` for a wrong tag.** `tag_status: not_found` with a plain reason (not found, private or denied, rate limit, offline), the tags that exist for Docker Hub images (and the closest ones), `ok` and `failure_reason`. What it saw is remembered per exact tag in `<data>/inspected-images.json` (variable names only, never values).
- **`validate_catalog_entry` warnings:** the tag was never inspected; the inspection failed and the tag is `latest`; the image declares variables, ports or volumes the entry ignores; no ready pattern and no health check; `steam_app_id` that can never be confirmed (no mounted folder); fewer than 5 settings without a note saying what was left out; a default host port that another catalog game also uses.
- **`get_mcp_audit`** (MCP) and an **AI agents** page section with "Last N calls" and "Only problems"; `GET /api/mcp/audit` takes `tool` and `status`.
- **`scripts/lint_catalog.py`** (`--strict`, `--notes`) and `gamedock/lint.py`; a test runs it on the repository catalog.
- **Clearer deploy errors:** the required-setting message names the label and variable, a wrong container port lists the valid ones, an unknown setting lists the valid ones, a port in use suggests a free one, and a failed create says that nothing was saved or started.
- Docs: ADDING_A_GAME (exact-tag rule, expose every documented setting, the loop, the new tools), the agent prompt, README, ARCHITECTURE. Screenshots `docs/gamedock-060-*.png` from `scripts/make_screenshots_checks.py`. `scripts/check_dialog_layout.py` now also covers Check setup (with the diagnostics open) and the deploy dialog with a port note.
- **Tool counts:** read_only 24, standard 37, full 41 (six new read-only tools: `get_catalog_review_status`, `verify_game_setup`, `check_ports`, `preview_deploy`, `get_server_diagnostics`, `get_mcp_audit`).

### Changed

- Version 0.6.0 (only the version lines of `pyproject.toml` and `gamedock/__init__.py`; dependencies are unchanged, so the header update button accepts it).

### Known limits

- `steam_app_id` can only be checked as far as the entry allows before a first deploy; the full confirmation is `verify_game_setup` on a running test copy.
- Tag lists exist only for Docker Hub images and only when update checks are on (`GAMEDOCK_UPDATE_CHECK`); other registries get a note.
- Comparing the image's variables, ports and volumes with the entry needs the image to be stored on the machine (`docker pull` it once); otherwise `inspect_image` says so.
- "Taken by another program" needs the real Docker backend (it tries to bind the port); the fake backend reports "not checked".
- The inspection record is keyed by the tag text; a tag that is re-published under the same name is not noticed.

## 0.5.2: the Config form remembers the game's defaults and marks what differs

Reported from Jacob's real use of the Config form (Enshrouded `gameSettings.*`, Palworld `OptionSettings`): the "These changes will be saved" list and the **Only changed** filter only knew what changed since the dialog was opened. After saving, or after a refresh and coming back later, nothing was marked and no setting showed what it had been changed from.

### Added

- **Recorded defaults (baseline).** The first time GameDock sees a config file that exists and has a form (when a newly deployed server becomes ready, otherwise the first time the file is opened in the form or raw view, or just before GameDock's first save) it records the parsed list of settings, not the file text, in `<data>/config-baselines/<server id>/<sha1 of the file path>.json` (atomic write, mode 0600 in a 0700 folder, outside the game's own folders, with the source file's mtime and size, the time, how it was taken and a format version). Values of `sensitive_keys` are never stored, only a set / not set marker (a test greps the file). The record is never overwritten automatically; if the file does not exist yet, or has no settings, nothing is recorded until it does. It is keyed by the server id, so renaming is harmless.
- **Persistent markers in the form.** Every setting that differs from the recorded default has a blue **Not default** pill, a muted `Default: <value>` line and a **Reset to default** button (it only fills the field, Save is still needed). A setting that was not in the defaults is labelled **New**; one that is gone from the file is listed under **Missing from file**. The bar shows `N settings differ from the default`. The markers are computed on the server against the record, so they stay after Save, closing, refreshing and coming back later; the session list ("These changes will be saved") still clears after Save. They also update live while typing.
- **Two filters.** **Only changed in this session** (the old one) and **Only differing from default** (new, persistent). They can be combined.
- **Reset all to defaults…** asks first, then only fills the form (nothing is written until Save; passwords are not touched).
- **Comparison by meaning.** `1`, `1.0` and `1.000000` (and `1.8e12` and `1800000000000`) are equal; `True` equals `true`; surrounding spaces are ignored; arrays and objects are compared as JSON (`true` is not `1`). So a game that rewrites its file in another style does not make every setting "different".
- **Honest notice when there is no history.** When the defaults are recorded at the moment the file is first opened, the dialog says "Defaults were first recorded just now, so earlier changes cannot be shown". Later the muted line at the top of the list says when and how they were taken. **Record current settings as the default…** (asks first) makes the saved file the new default on purpose; the previous record is kept once as `<sha1>.prev.json`.
- **API.** `GET .../config/file` and `POST .../config/form` return per field `default`, `default_set` (passwords), `differs` and `status` (`same`, `changed`, `new`), `form.missing`, `form.differing` and a top-level `baseline` (`available`, `captured_at`, `origin`, `just_recorded`, `has_previous`). New `POST /api/servers/{id}/config/baseline` (`{path, confirm: true}`) to re-record.
- **MCP.** New read-only tool `get_config_changes(server_id, path)` (all modes except off): the settings that differ from the recorded defaults with default and current value, secrets only as "set" / "not set", plus the baseline's origin and a plain note about what it cannot know. There is deliberately no MCP tool to re-record the defaults. The guide explains it. Tool counts: read_only 18, standard 31, full 35.
- `scripts/make_screenshots_defaults.py` and the screenshots `docs/gamedock-052-config-*.png`; `scripts/check_dialog_layout.py` now also saves, reopens, uses the new filter, Reset all and the record confirmation and asserts the 0.5.1 frame rules still hold (same dialog size, footer visible, one scroll area).

### Changed

- Cleanup: the baseline folder is removed when a server is deleted together with its data or its data is wiped; it is kept when the server is deleted but its data folder is kept (the files and their defaults are both still there). It is GameDock's own record, so it is not part of backups. `config-baselines` is a reserved name, like `backups`.
- Layout: the Config form's top bar was rearranged so the extra controls fit the fixed dialog frame: the session count now lives in the review box title ("These changes will be saved (N):") (the filters and the default count share one wrapping row), and the review box is capped lower (15% of the window height; 13% and 10% in small windows). Short labels are used in windows under 700 px wide or 650 px high.
- Version 0.5.2 (only the version lines of `pyproject.toml` and `gamedock/__init__.py`, so the header update button accepts it).

### Known limits

- **A baseline can only know what the file looked like when GameDock first read it.** A server whose file was edited by hand, or saved with 0.5.x, before 0.5.2 first reads it gets a baseline equal to that file: earlier changes are not shown (the notice says so). To get real game defaults, deploy a new server (its baseline is taken when it becomes ready) or compare with the game's own default file by hand.
- Passwords are not compared by value (only set / not set); a changed but still-set password is not marked.
- If a game update changes the game's own defaults, the record stays as it was (use **Record current settings as the default…**). Palworld regenerates its file from the deploy-form values on each start, so a deploy-time baseline already contains those values.
- Array entries are compared by position (`userGroups[0]`), so re-ordering a list shows as changes.

## 0.5.1: dialogs keep one fixed frame (Config editor layout fix)

Reported from Jacob's browser (about 1000 x 880): in the Config editor's settings form, changing a few settings made the "These changes will be saved" box appear and the dialog broke: it shrank, it had its own scrollbar plus a second one around the field list, and the Close and Save buttons were cut off at the bottom.

### Fixed

- **Config editor.** The dialog now has one fixed size (85% of the window height, set with `dvh` units, never changed by the review box appearing or going away). The title, the file selector, the search box, the "Only changed" filter, the changed count, the review box and the Close/Save buttons always stay visible; **only the field list scrolls** (one scrollbar). The review box is capped (about 21% of the window height, less in short windows) and scrolls by itself when many settings are changed, so it can never push the buttons out. Save and error messages now sit above the buttons, where they are always seen. The raw text view fills the same frame and is its only scroll area.
- **Root causes** (all in `style.css`): (1) the dialog, the panel inside it and the field list each had their own `max-height` and `overflow: auto`, in `vh` units that did not add up (92vh dialog, 90vh panel, 50vh list), so three nested scroll areas fought each other and the footer scrolled out of sight; (2) nothing told the flex children they may shrink (`min-height: 0`), so the content pushed the buttons down instead of the list giving way; (3) the hidden checkbox inside every on/off switch is `position: absolute` and had no positioned ancestor except the dialog itself, so it escaped the field list's clipping and made the whole dialog taller than its box (that is why the dialog could be scrolled by hand and jumped when a switch was toggled); (4) the "Settings form / Raw text" selector was squashed to a thin line in a short window.
- **Same frame for every dialog.** `openDialog()` now builds one structure for all of them: title on top, one scrolling body, and a footer with messages and buttons that never scrolls away. This fixes the same flaw (buttons or messages only reachable by scrolling, two scrollbars, or buttons cut off) in **Logs, Backups, Updates, Image update, Schedules, Review (catalog entry), Deploy/Edit and the confirmation dialogs**, at 1000 x 880, 700 x 500 and 390 x 760. Logs also has a fixed height now, with the log text filling it. The Review dialog's file text no longer has a second scrollbar of its own, and the Schedules dialog no longer overflows the window edge on a phone (its width belongs to the dialog, not the panel).
- Small windows: a long game note above the settings is shortened to two lines (the full text is in its tooltip), and the file path and restart note are hidden when the window is under 650 px high, so the field list always has room.

### Added

- `scripts/check_dialog_layout.py`: a browser check (Playwright and Chrome) that opens the Config editor on a nested Enshrouded-style settings file with 40+ fields, changes four settings and asserts that (a) the dialog has exactly the same size and position before and after, (b) the footer buttons are fully inside the window and nothing covers them, (c) only one element in the dialog scrolls, and that nothing sticks out of the dialog box. It repeats this for 16 changes, the raw view, and all the other dialogs, at three window sizes. `tests/test_browser_dialog_layout.py` runs it under pytest; it is skipped when Playwright or Chrome is missing or when `GAMEDOCK_SKIP_BROWSER_TESTS=1`.
- Screenshots before and after in `docs/` (`gamedock-051-config-before.png`, `gamedock-051-config-fixed.png`, and the other dialogs).

### Changed

- Version 0.5.1 (only the version lines of `pyproject.toml` and `gamedock/__init__.py`, so the header update button accepts it).

## 0.5.0: adding a game is now a repeatable procedure, also for AI agents

Asked for: make adding new games (the first is ASKA) easy to repeat, by a written procedure in the repository and through the MCP endpoint, so a local AI harness can research a game and add it, while Jacob keeps control.

### Added

- **`docs/ADDING_A_GAME.md`**: what a catalog entry is, the ten steps (image, ports and protocols, save and settings folders, environment variables and secrets, Steam app id and update behaviour, log lines for ready, version and progress, writing, validating, a test copy on spare ports, verifying), a complete field reference of every catalog key (generated from the code and kept equal to it by a test; `scripts/update_docs.py` rewrites it), the **common traps** learned from Palworld and Enshrouded, a verification checklist and the MCP section.
- **`catalog/_template.yaml`**: a commented starting point. Files whose name starts with `_` (or `.`) are now ignored by the catalog loader.
- **`docs/PROMPT_ADD_A_GAME.md`**: a ready-to-paste task for an AI harness (how to connect to `http://192.168.1.29:8100/mcp` with the bearer token without ever printing it, how to research, inspect, validate, add, test on spare ports and report in a fixed structure). The game is a placeholder.
- **MCP tools** `get_add_game_guide`, `validate_catalog_entry` and `inspect_image` (all modes except off), and `add_catalog_entry` and `update_catalog_entry` (standard and full). Tool counts: read_only 17, standard 30, full 34. New resource `gamedock://add-a-game` and prompt `add_a_game`; `gamedock://guide` now explains the authoring tools and the review rule.
  - `validate_catalog_entry` is a dry run with exactly the checks of adding; it returns errors, warnings, notes, a plain summary and whether the id exists. `inspect_image` never pulls or runs an image (see Safety below).
  - `add_catalog_entry` writes `catalog/<id>.yaml` atomically, only in the catalog folder, with a file name from the validated id, and never overwrites. `update_catalog_entry` only replaces entries that MCP added, never entries from the repository (also refused when git tracks the file).
- **Review step in the web page.** Entries added through MCP are recorded in `<data>/catalog-review.json` (not in the repository) as `reviewed: false` with the file's SHA-256. The Catalog page shows **Added by AI, needs your review**, disables Deploy with the reason, and the dashboard shows a banner. The **Review** dialog shows a plain summary (image, ports, folders it will mount, secret fields, anything unusual flagged) and the full YAML. **Approve** records the reviewed fingerprint; **Reject** deletes the file and record after a confirmation. `POST /api/servers` and `deploy_server` both refuse an unreviewed entry. If the file changes after approval, by anyone, it is unreviewed again. Approve and reject are web only.
- **Audit.** Every authoring call is in the audit log (the YAML text is never logged, only its size, and for add and update a short fingerprint); approvals and rejections in the web page are logged with client `web`.

### Safety rules (validation)

- For **every** entry: privileged mode, host networking, device mappings, extra capabilities, `security_opt`, shared process or memory spaces, a mount of the Docker socket, a malformed image reference (`..`, `//`, spaces) and non-list `ports`, `volumes`, `fields`, `config_files` are refused with a plain message; description, notes and update note are limited to 2000 characters.
- For entries **added by an AI agent**, additionally: unknown keys (also inside ports, volumes, fields and config files), absolute or `~` or `$` host folders, host ports below 1024, host ports listed twice, YAML anchors and aliases, duplicated keys, ids starting with `_`, and text over 20 KB.
- `validate_catalog_entry` also warns about unusual but legal things: no fixed image version, a registry other than Docker Hub, no ports or folders, password-like values written in the file, a password field not marked secret, no ready pattern, no notes.

### Changed

- Version 0.5.0 (only the version lines of `pyproject.toml` and `gamedock/__init__.py`; dependency lines are unchanged, so the header update button accepts it).
- The catalog API (`/api/catalog`, MCP `list_catalog`) now also returns `origin`, `reviewed`, `deployable` and `review_note`. `/api/status` returns `catalog_review_pending`.
- Existing catalog entries are unaffected (they are repository entries and load as before). A new test loads the bundled ones.

### Decisions and known gaps

- **Approval is a convention for MCP, not a login.** GameDock has no login (LAN trust, by Jacob's choice), so anything that can reach the web port can press Approve through `POST /api/catalog/<id>/approve`. An agent that only speaks MCP cannot, and the harness prompt tells it never to use the web API. Adding a login later would close this.
- `catalog-review.json` is the record of which entries came from an AI. If it is deleted or damaged, those entries are treated as repository entries (GameDock reports a damaged file on the Health page and refuses to add entries while it is unreadable). Keep the data folder backed up.
- `inspect_image` can only list ports, folders and settings for an image that is already on the machine; otherwise a person must run `docker pull`. It trusts what the image declares: many images declare nothing, so the documentation remains the main source.
- An AI-added entry is an untracked file in the git checkout's `catalog/` folder. It does not block the Update GameDock button (untracked files are ignored by its clean-tree check), but a later `git pull` that brings a tracked file of the same name would stop until the untracked one is removed. Commit approved entries you want to keep.
- A hand-edit of an approved AI entry makes it unreviewed (by design): approve it again.
- The harness's own judgement decides the image, ports and app id; GameDock checks the shape, not the truth. That is why the review dialog and the first test deploy exist.

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
