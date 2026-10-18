# Adding a game to GameDock

This page is for people and for AI agents. It explains how to add a new game, such as a SteamCMD or Wine dedicated server, to GameDock's catalog so that it can be deployed, started, stopped, backed up and managed like the others. Follow the ten steps in order. Do not skip the verification at the end.

There are two ways to add a game, and the procedure is the same for both:

- **By hand.** Copy `catalog/_template.yaml` to `catalog/<id>.yaml`, fill it in, and reload the Catalog page.
- **By an AI agent through GameDock's MCP endpoint.** The agent calls `get_add_game_guide`, `inspect_image`, `validate_catalog_entry` and `add_catalog_entry`. Entries added this way are marked **Added by AI, needs your review** and **cannot be deployed until a person approves them** in the web page. See [Adding a game through MCP](#adding-a-game-through-mcp).

## 1. What a catalog entry is

A catalog entry is one small text file in the `catalog/` folder, written in a format called YAML (lines of `key: value`). It is a **template** for one game: which Docker image to run, which ports to open, which folders to keep, which settings to ask for, and a few hints that make GameDock friendlier (log lines that mean "ready", the game's Steam id, which config files can be edited).

An entry is **not** a running server. When someone presses Deploy, GameDock copies the template into a new server with its own folder, ports and settings. That server keeps its own copy of the template, so editing the catalog file later never changes servers that already exist.

GameDock never runs anything except a Docker container made from the image named in the entry, with exactly the ports, folders and settings the entry lists. It always creates plain, unprivileged containers.

Files whose names start with an underscore (for example `_template.yaml`) are ignored by the catalog.

## 2. The ten steps

For every step, write down **where you found the answer** (a link). You will put the important sources in the entry's `notes`. If you cannot confirm something, do not guess: leave it out, and say in `notes` that it is unverified.

### Step 1. Find a maintained Docker image for the dedicated server, and read its documentation

- A *dedicated server* is the program that hosts the game for others. It is not the game you play. Search for "<game name> dedicated server docker" on Docker Hub (hub.docker.com) and GitHub.
- Prefer an image that is updated recently, has a documentation page with a table of ports, folders and environment variables, and has many users or stars. A good image's page describes `docker run` or `docker-compose` examples; those examples are the best source for the next steps.
- Note the exact image name and a **specific tag** (for example `1.2.3` or `stable`). Use `latest` only if the image has no other tag.
- Make sure the image supports a normal PC or server processor (`linux/amd64`). On a machine with Docker, `inspect_image` shows what the registry says without downloading anything.
- Never invent an image name. If you cannot find a maintained image, stop and say so.

### Step 2. Confirm the ports and their protocols

- Write down every port the documentation says to open, and whether each is **UDP** or **TCP**. Many games use UDP for play and TCP for queries or remote control. They are different ports even if the number is the same.
- In the entry, each port has a `container` number (what the image listens on, from the documentation) and a `host` number (what the machine opens; the default is usually the same number). People can pick other host numbers when deploying.
- Some games must be reachable on the same number inside and outside because they announce their own port. If the documentation says so, keep `host` equal to `container` and say it in `notes`.

### Step 3. Confirm which folders hold the saves and the settings, and mount them

- Find the folder inside the container where the game writes its **world or save files**, and where its **settings file** lives. Both go in `volumes`.
- Anything not in a volume disappears when the container is removed, and GameDock cannot back it up.
- If the game downloads many gigabytes on first start (most SteamCMD games do), also mount the **game install folder**. Otherwise every re-created container downloads it all again.
- Use relative host folders such as `./saves`. GameDock keeps them under its own data folder for that server (`<data folder>/<server id>/saves`).
- Settings files you want people to edit go in `config_files`; their `path` is relative to the server's folder, so it starts with the volume's host folder (for example `saves/Settings.ini`). The Config form also remembers what the file looked like when GameDock first saw it (the game's own defaults) and marks settings that later differ; that needs no catalog key. Declare `sensitive_keys` so passwords are never stored in that record.

### Step 4. Identify the environment variables, and which are secret

- Environment variables are how most images are configured. List the ones the documentation says matter: server name, number of player slots, password, admin password, world name, and so on.
- Values people choose go in `fields`. Fixed values that should always be set go in `env`.
- **Every password or token must be a field with `secret: true`.** Never write a real password in the file: catalog files are public in git.
- Read what an empty value means. Some images treat "empty" as "no password"; others keep a generated one. See the common traps below.
- If the image runs as a chosen user id (`PUID` and `PGID` variables), set them in `env` to match the user GameDock runs as, so backups can read the files.

### Step 5. Find the Steam app id of the dedicated server, and how the image updates the game

- Open SteamDB (steamdb.info) or the image's documentation and find the **app id of the dedicated server**. The server usually has its own number, different from the game you buy. Searching "<game name> Dedicated Server" on SteamDB shows it. Never guess a number.
- The id goes in `steam_app_id`. It turns on the game's version display, "Update available" and the Update game now button.
- Find out **when the image updates the game**. Most images run SteamCMD every time the container starts (often switched on by a variable such as `UPDATE_ON_BOOT=true`). If so, set `game_update_method: restart`, and write one plain sentence in `update_note`. If the game is not on Steam, leave `steam_app_id` out.
- Find out where the image keeps the Steam record `steamapps/appmanifest_<app id>.acf` (usually inside the install folder). GameDock searches the mounted folders for it. If the image keeps the installed version somewhere else, `installed_build_file` can name a small file with it.

### Step 6. Find the log lines for "ready", "version" and "download progress"

- Run the image once (see step 9) and press **Logs** on the tile, or read the image's documentation, and find:
  - a line printed once players **can join** (`ready_log_pattern`). Pick a line that appears *late*, after the game is listening; some images print "config loaded" long before.
  - the percentage line printed while SteamCMD downloads (`progress_log_pattern`), such as `Update state (0x61) downloading, progress: 51.68`. The pattern needs exactly one group in round brackets around the number.
  - a line that shows the game's own version (`version_log_pattern`), if any.
- These are regular expressions (patterns for text). Keep them simple and test them against real log text. GameDock checks that they are valid when the file loads.
- If the image has a Docker health check, GameDock uses it; the patterns still help while the health check lags.

### Step 7. Write the entry

- Copy `catalog/_template.yaml` and replace every value marked REPLACE. Use the field reference below for what each key means.
- Write `description` (one line), and `notes` (sources and what is unverified).
- Keep it short. A normal entry is 30 to 90 lines. The limit for entries added by an AI agent is 20 KB.

### Step 8. Validate

- By hand: put the file in `catalog/`, open the Catalog page (or run `python -m gamedock doctor`). A mistake is reported in plain words, and the Health page lists files that could not be read.
- Through MCP: call `validate_catalog_entry` with the text until `valid` is true. It saves nothing. Read the `warnings` too: they point at things that are legal but suspicious, such as a password with a default value or no ready line.

### Step 9. Deploy a test copy on spare ports

- Deploy the entry with a name such as `test-<game>`, and choose **spare host ports** (for example 20000 and up) so a real server on the default ports is not disturbed.
- Do not delete or change any existing server to test a new game.
- The first start downloads the game and can take many minutes. The tile shows Installing with a percentage. Wait for **Ready to join**.
- An entry added by an AI agent must be approved by a person before this step is possible (the Deploy button is disabled and the deploy call is refused until then).

### Step 10. Verify, then mark as reviewed

Work through the [verification checklist](#verification-checklist). When everything passes, update the entry's `notes` to say what was verified and on which date, remove any "unverified" remarks that are now confirmed, and (for entries added by an AI agent) ask the person to review and approve it in the web page. Delete the test server when you are done, if the person agrees.

## 3. Field reference

Every key a catalog entry understands. This table is generated from the program (`gamedock/authoring.py`); the MCP tool `get_add_game_guide` returns the same table in machine-readable form. A key that is not in this table is rejected in entries added by an AI agent.

<!-- field-reference:start -->
| Key | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `id` | text | yes | (none) | The short name of the game. Lowercase letters, digits, - and _, starting with a letter or digit, at most 63 characters. Becomes the file name <id>.yaml. Ids that start with _ are reserved for templates. |
| `name` | text | yes | (none) | The name people see on the card, for example 'Example Game Dedicated Server'. |
| `image` | text | yes | (none) | The Docker image, as name[:tag][@digest], for example 'someuser/some-server:1.2'. Letters, digits and . _ - / : @ only. Prefer a pinned tag over 'latest'. |
| `description` | text | no | empty | One line shown on the card. At most 2000 characters. |
| `notes` | text | no | empty | Shown in amber on the card. Use it for warnings, where the information came from, and what has NOT been verified yet. |
| `ports` | list of ports | no | none | The default ports the game needs. Each server can choose different host ports when deployed. |
| `ports[].protocol` | tcp or udp | yes | (none) | The protocol. Many games use UDP for play and TCP for queries or remote control: list each one. |
| `ports[].host` | whole number 1-65535 | yes | (none) | The default port on this machine. Entries added by an AI agent must use 1024 or higher. The same host port and protocol may not appear twice. |
| `ports[].container` | whole number 1-65535 | yes | (none) | The port inside the container (what the image's documentation lists). The same container port and protocol may not appear twice. |
| `volumes` | list of folders | no | none | Folders from this machine that appear inside the container. Anything not in a volume is lost when the container is removed and is not backed up. Mount the saves, the settings and (for games that download many GB) the game's install folder. |
| `volumes[].host` | relative folder | yes | (none) | A relative folder such as ./saves, kept under <data folder>/<server id>/. No '..'. Absolute paths are only accepted in entries a person put in the catalog folder; never the Docker socket. |
| `volumes[].container` | absolute path | yes | (none) | Where the folder appears inside the container. Must start with / and contain no '..'. |
| `env` | mapping name: value | no | none | Fixed environment variables that are always set and are not shown in the deploy form (quote numbers and yes/no so YAML keeps them as text). Never put passwords here: the file is public. A name may not also be listed in fields. |
| `fields` | list of settings | no | none | Environment variables the person deploying fills in (server name, slots, password...). |
| `fields[].name` | environment variable name | yes | (none) | Letters, digits and _, not starting with a digit. Names such as PATH, HOME or DOCKER_* are reserved. Each name once. |
| `fields[].label` | text | no | the name | The label in the form. |
| `fields[].default` | text | no | empty | The value pre-filled in the form. An empty value is still passed to the container as NAME= (present but empty), because some images treat that differently from a missing variable. |
| `fields[].secret` | true or false | no | false | true = a password box whose value is never shown again, never written to logs and never returned by the API. Use it for every password. |
| `fields[].required` | true or false | no | false | true = the person must fill it in when deploying. |
| `fields[].description` | text | no | empty | Help text under the box. Say what an empty value means (for example 'leave empty for no password'). |
| `config_files` | list of files | no | none | Settings files people may read and edit with the Config button. Each must be inside a mounted folder. |
| `config_files[].path` | relative file path | yes | (none) | Relative to the server's data folder, so it starts with the volume's host folder, for example saves/settings.ini. No '..', no absolute path. Each path once. |
| `config_files[].label` | text | no | the path | The name shown above the editor. |
| `config_files[].format` | ini, json, yaml or text | no | text | A hint for the editor. json files are checked for validity when saved. Files shaped like Palworld's OptionSettings, KEY=value lines, or JSON also get a form with one box per setting. |
| `config_files[].sensitive_keys` | list of names | no | none | Settings whose values are hidden in the editor and in the form (passwords). Writing the placeholder back keeps the stored value. Names: letters, digits, _ . - |
| `config_files[].hot_edit` | true or false | no | false | true = safe to edit while the server runs. false = GameDock asks for the server to be stopped first (the game would overwrite the file when it exits). |
| `config_files[].note` | text | no | empty | A warning shown above the editor, for example 'the image rewrites this file on every start'. |
| `update_method` | pull_recreate or none | no | pull_recreate | pull_recreate = the Update button downloads the newest image and recreates the container. none = the game updates itself inside the container and image updates are switched off. |
| `update_note` | text | no | empty | Plain words shown with the update button: how this particular image really updates the game. |
| `backup_stop_first` | true or false | no | false | true = backups stop the game briefly instead of copying it while it runs. Use it for games that keep their save in memory or write it in pieces. |
| `save_command` | list of words (or one quoted string) | no | none | A command, run inside the container without a shell, that makes the game write its save, run before a backup of a running server. 1 to 12 words, each at most 300 characters. Example: [rcon-cli, save]. A failure only adds a note to the backup. |
| `ready_log_pattern` | regular expression | no | empty | A line the game prints once players can join. Until it appears the tile says Starting or Installing, not Ready to join. One line, at most 300 characters, checked when the catalog loads. |
| `progress_log_pattern` | regular expression with exactly one group | no | empty | Matches a download progress line; the one group in brackets is the percentage, for example 'progress: *([0-9.]+)'. Gives the tile its Installing 52% bar. |
| `steam_app_id` | number (as text) | no | empty | The Steam app id of the DEDICATED SERVER (not the game you play). Turns on game version tracking: installed build, newest build, Update available. |
| `steam_branch` | text | no | empty (the public branch) | The Steam branch to compare with, for example beta. Needs steam_app_id. |
| `steam_manifest_dir` | relative folder | no | empty (search the volumes) | The folder, inside the server's data folder, holding appmanifest_<app id>.acf, when the automatic search does not find it. Needs steam_app_id. |
| `version_log_pattern` | regular expression with exactly one group | no | empty | A log line that shows the game's own version, with the version in the one group, for example 'Game version is (v?[0-9.]+)'. The last match is used. |
| `game_update_method` | restart or none | no | restart when steam_app_id is set, otherwise none | restart = the image updates the game when the container starts, so Update game now restarts the server and checks the build changed. none = GameDock cannot update the game itself. restart needs steam_app_id. |
| `installed_build_file` | relative file path | no | empty | A small file, inside the server's data folder, whose text is the installed version or build, for images that keep no Steam appmanifest. A plain number is compared with Steam's newest build. |
| `stop_timeout` | whole number of seconds, 1-600 | no | 0 (GameDock's usual stop time, 60 seconds) | How long this game gets to save and exit when stopped, before Docker kills it. Give slow-saving games more time. |
<!-- field-reference:end -->

### Rules that are always checked

These apply to every entry, whoever wrote it:

- The image must be a normal image reference (`name`, `name:tag` or `name@sha256:...`, no spaces).
- Ports must be between 1 and 65535, and the protocol must be `tcp` or `udp`.
- Environment variable names must be valid, and names that would change how Docker itself behaves (such as `PATH` or `DOCKER_HOST`) are refused.
- Regular expressions must be valid, one line, and at most 300 characters.
- Privileged mode, host networking, device mappings, extra capabilities (and similar keys such as `security_opt`) are refused, and so is any mount of the Docker socket (`docker.sock`).

Additionally, for entries **added by an AI agent**:

- Only the keys in the table above are accepted (an unknown key is an error).
- Host folders must be relative, such as `./saves`, so they stay inside the server's own data folder.
- Host ports must be 1024 or higher.
- The whole entry must be at most 20 KB, with no YAML anchors or aliases and no key written twice.

## 4. Common traps

These were all learned on real servers (Palworld and Enshrouded). Check each one.

1. **An empty password may not mean "no password".** The Enshrouded image only clears the game's password when it is told through a role variable (`SERVER_ROLE_0_PASSWORD`); the ordinary `SERVER_PASSWORD` was ignored when empty, and the game kept a password it generated itself. Read how the image treats an empty value, and use the variable that really clears it. GameDock always passes an empty field as `NAME=` (present but empty), which is what such images need.
2. **The settings file lives inside the container unless its folder is mounted.** If the folder is not in `volumes`, the Config editor finds nothing, and a re-created container loses the file. Mount the folder that holds it.
3. **Mount the game install folder.** SteamCMD games are several gigabytes (Enshrouded 7 GB, Palworld 5 GB). If the install folder is not a volume, every re-created container downloads everything again.
4. **The first start downloads many gigabytes.** The container is "running" long before the game can be joined. Give the entry a `progress_log_pattern` and a late `ready_log_pattern`, so the tile says Installing and Starting instead of Ready to join. Joining too early looks like a connection timeout.
5. **Health checks can go unhealthy during the first download.** Some images have a health check that fails until the game is installed. A tile that says "unhealthy" in the first minutes is not necessarily broken: check the logs.
6. **Settings may be regenerated from environment variables on every start.** The Palworld image rewrites its settings file from its variables each time it starts, so edits made in the Config editor are lost unless a switch such as `DISABLE_GENERATE_SETTINGS=true` is set. Say so in the file's `note`, or set the switch.
7. **Images run as a specific user id.** If the image runs as user 1000 but the folders were created by another user, the game cannot write its saves. Use the image's `PUID` and `PGID` variables (set them in `env`) to match the user that runs GameDock.
8. **Ports differ per protocol.** The game port is usually UDP and the query or admin port may be TCP or UDP. List every one with the right protocol; a port opened as TCP does not work for UDP traffic.
9. **Do not hard-code to one machine.** No host names, no absolute host paths, no IP addresses, no user names, no passwords. Use relative folders and fields, so anyone can use the entry.
10. **Pin the image version when you can.** `latest` can change under you and break a working server.
11. **Do not trust a log line that appears early.** Pick the line printed when the game is really listening.
12. **Wine games are slow to start** and print many warnings. Warnings are normal; look for the real ready line.

## 5. Verification checklist

After the test copy is deployed on spare ports, check each item and write down the result:

- [ ] The tile shows Installing with a percentage (or Starting) and then **Ready to join**, not Ready too early.
- [ ] The game can be joined from another computer on the spare port (UDP and TCP as listed).
- [ ] The Logs button shows the game's output, and the ready and version lines match the patterns.
- [ ] The Config button opens each listed file; the form or text matches the game; after saving and restarting, the change took effect (note if the image rewrites the file).
- [ ] An empty password really means no password (and a set password is required to join).
- [ ] A backup can be created, listed, and restored on the test server (the restore takes a safety backup first).
- [ ] Stop, start and restart work, and the world is still there afterwards.
- [ ] Game version: the tile shows the installed build, and "Check now" in the Updates dialog works (or the entry says it cannot).
- [ ] The test server is stopped or deleted, and the entry's `notes` say what was verified and when.

## 6. Adding a game through MCP

An AI agent connected to GameDock's `/mcp` endpoint (modes `read_only`, `standard` or `full`) has these tools for this job:

| Tool | Modes | What it does |
|---|---|---|
| `get_add_game_guide` | all | The procedure, the field reference, a template and the rules |
| `inspect_image` | all | Read-only look at a Docker image. Never downloads or runs it. If the image is already on the machine it shows declared ports, volumes, user, environment variable names and health check; if not, it says so and how to pull it |
| `validate_catalog_entry` | all | Dry run of the same checks as adding. Saves nothing. Returns errors, warnings and a plain summary |
| `add_catalog_entry` | `standard`, `full` | Saves a new entry as `catalog/<id>.yaml`. Refuses an existing id |
| `update_catalog_entry` | `standard`, `full` | Replaces an entry that was added through MCP. Never an entry from the repository |

The same text is the resource `gamedock://add-a-game`, and the prompt `add_a_game` gives the steps as a ready-made message. A ready-to-paste task for an agent is in [PROMPT_ADD_A_GAME.md](PROMPT_ADD_A_GAME.md).

**The review rule.**

- An entry added through MCP is recorded (in `<data folder>/catalog-review.json`, not in the repository) as added by MCP, not reviewed, with the SHA-256 fingerprint of the file.
- The Catalog page shows it with the badge **Added by AI, needs your review**, its Deploy button is disabled, and `deploy_server` and the web API refuse to deploy it.
- A person presses **Review**, reads the full YAML and a plain summary (image, ports, folders, secret fields, anything unusual), and presses **Approve** or **Reject**. Approve records the fingerprint of exactly what was shown. If the file changes afterwards, by anyone, it is unreviewed again. Reject deletes the file after a confirmation.
- Approving and rejecting exist only in the web page. There is no MCP tool for them.
- Entries that came from the repository (git-tracked files) are never overwritten or changed through MCP. `update_catalog_entry` works only on entries the agent itself added, and it resets them to unreviewed.
- An entry added through MCP is a new, untracked file in your `catalog/` folder (it does not block the header Update GameDock button). To keep it permanently, review it, then `git add` and commit it yourself; from then on it is a repository file and MCP can no longer change it. If you later pull a repository file with the same name, delete the untracked one first.
- Every authoring call is written to the audit log (the YAML text itself is not logged, only its size and fingerprint).

## 7. Tips for good entries

- Look at `catalog/enshrouded.yaml` (a Wine image that was tested on a real server) and `catalog/palworld.yaml` for finished examples, with comments saying where each value came from.
- Describe the first start in `notes`: how long it takes and what the tile will show.
- Keep comments in the file: they are kept when an entry is saved through MCP.
