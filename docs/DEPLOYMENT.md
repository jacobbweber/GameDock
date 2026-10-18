# Running GameDock all the time

Two ways. **Use the first one** unless you have a reason not to.

| | A. systemd + virtualenv (recommended) | B. Docker Compose (optional) |
|---|---|---|
| How it runs | A normal Python program run by systemd | GameDock in a container |
| Docker access | The user's own `docker` command | The Docker socket mounted into the container |
| Extra moving parts | None | An image to build, a socket mount, same-path data folder |
| Files in the data folder belong to | The user running it | The uid you set in `.env` |
| Files | `deploy/gamedock-user.service`, `deploy/gamedock.service` | `Dockerfile`, `docker-compose.yml` |

Nothing in the repository installs any of these. You copy files yourself, as shown in the comments at the top of each file.

## A. systemd (recommended)

Pick one of two flavours:

- **User service** (`deploy/gamedock-user.service`): runs as you, lives in your home folder, no root needed except one `loginctl enable-linger` command so it starts at boot. Best for a single-person home server.
- **System service** (`deploy/gamedock.service`): runs as a dedicated `gamedock` user with extra sandboxing (`ProtectSystem=strict`, `NoNewPrivileges`, only the data folder writable). Settings go in `/etc/gamedock/gamedock.env` (start from `deploy/gamedock.env.example`).

Either way:

1. Do the manual install steps from the top of the unit file (clone, venv, `pip install`).
2. Run `python -m gamedock doctor` as that user and fix anything it reports.
3. Copy the unit file, `daemon-reload`, `enable --now`.
4. Read logs with `journalctl -u gamedock -f` (`--user` for the user flavour).

Good to know:

- Restarting or updating GameDock does **not** stop game servers; they are separate containers with restart policy `unless-stopped`, and they come back by themselves after a reboot (Docker starts them).
- To update GameDock by hand: `git pull --ff-only`, `pip install -e .` (only needed if the required packages changed), restart the service.
- From 0.3.0 the page can do this itself: the header shows the version, an **Update available** badge, and an **Update GameDock** button.

### Updating GameDock from the web page (0.3.0 and later)

The button runs `git pull --ff-only` in the install folder, then restarts GameDock only. Game servers are separate containers and keep running. It needs:

- the install folder to be a git checkout on a branch, with no local changes to tracked files, writable by the user GameDock runs as. That is true for the **user service** flavour. With the hardened **system service** (`ProtectSystem=strict`) the install folder is read-only for the service, so the button will report the git error: update by hand there (commands are shown in the dialog). The Docker image has no git checkout, so rebuild it instead (`git pull`, `docker compose up -d --build`).
- a service manager that restarts GameDock. **The shipped units need no change.** GameDock first asks systemd to restart it (`systemctl --user restart gamedock`, or `systemctl restart gamedock` when allowed). If that is not possible it exits with status 75, which makes `Restart=on-failure` (what both example units use, with `RestartSec=5`) start it again; `Restart=always` works too. Outside systemd it starts itself again in place. A unit with `Restart=no` or `Restart=on-abnormal` will leave GameDock stopped after an update: do not use those.
- GameDock finds its own systemd unit name when it restarts, so a different unit name works. The manual commands the dialog displays assume the name `gamedock`.

It refuses (and changes nothing) while a backup, update, restore, deploy or scheduled job is running, with local changes, when the pull is not a fast-forward, and when the update changes the required packages (run `.venv/bin/pip install -e .` by hand, then restart; a release that only changes the `version` line of `pyproject.toml` does not count, from 0.4.0 on). Turn the check off with `GAMEDOCK_UPDATE_CHECK=false` and only the button with `GAMEDOCK_SELF_UPDATE=false`.

### Moving an existing install from 0.3.0 to 0.4.0

0.4.0 changes only the version line in `pyproject.toml` and adds no packages, **but the 0.3.0 button refuses any change to `pyproject.toml`**, so update this one by hand (once). From 0.4.0 on the button looks at what changed in `pyproject.toml` and only refuses when the required packages changed, so ordinary version bumps no longer block it.

```bash
cd ~/GameDock
git pull --ff-only
.venv/bin/pip install -e .      # harmless; 0.4.0 adds no new packages
systemctl --user restart gamedock
journalctl --user -u gamedock -n 20 --no-pager     # should say it started
```

Game containers keep running. Servers you deployed earlier keep working and pick up the Steam settings (app id, version pattern) from the current catalog entry of the same game, so Palworld shows its game version without a redeploy. (A new Palworld deploy also gets `UPDATE_ON_BOOT=true`; the image already defaults to it, so an existing one does not need it.) Outbound questions go to `api.steamcmd.net` (change with `GAMEDOCK_STEAM_INFO_URL`, or switch everything off with `GAMEDOCK_UPDATE_CHECK=false`).

### Moving an existing install from 0.2.0 to 0.3.0

0.2.0 has no button, so the first update is by hand (user service shown; for the system service add `sudo` and drop `--user`):

```bash
cd ~/GameDock
git pull --ff-only
.venv/bin/pip install -e .      # harmless; 0.3.0 adds no new packages
systemctl --user restart gamedock
journalctl --user -u gamedock -n 20 --no-pager     # should say it started
```

Your `data/` folder, servers and backups are untouched. Nothing else needs to change. The scheduler starts at once; there are no schedules until you create them on the **Schedules** page. After this, future updates can use the button.

### Schedules and the host timezone

Times of day follow the timezone of the machine (the `TZ` variable, or `/etc/localtime`); the Schedules page shows which. In a Docker image without timezone data they are UTC: set `TZ` and mount `/etc/localtime` read-only if you want local time. GameDock must be running at the scheduled time; a missed backup runs once at the next start, a missed maintenance only if it is under 6 hours late.

## B. Docker Compose (optional)

Only if you cannot or do not want the systemd route.

### Security implications. Read before using.

GameDock starts containers by talking to the Docker engine. In this setup the engine's socket (`/var/run/docker.sock`) is mounted into the GameDock container. Whoever can use that socket can ask Docker to start a container that mounts the host's whole disk as root. Therefore:

- **The GameDock container is root-equivalent on the host.** `cap_drop: ALL`, `no-new-privileges` and a non-root user inside the container do not change that; the socket is the powerful part.
- **A bug or break-in in GameDock (or its Python packages) becomes full control of the machine.** GameDock has no login, so its only protection is that it is reachable from this machine only. `docker-compose.yml` publishes the port as `127.0.0.1:8080:8080`; do not remove the `127.0.0.1`.
- **Never** change that to `8080:8080` or `0.0.0.0:8080:8080`. It would put a root-equivalent control panel with no password on your network.
- The systemd route has the same basic power (the user is in the `docker` group, which is root-equivalent as well) but fewer layers and nothing extra to build, which is why it is the default advice. A rootless Docker or Podman setup reduces the risk but is not covered here.

### Things that are easy to get wrong

- **The data folder must have the same path inside and outside the container.** GameDock tells the Docker engine (running on the host) which host folder to mount into each game. If GameDock saw the data as `/data` while it really lives at `/srv/gamedock/data` on the host, the game would be given the wrong (probably empty, root-owned) folder. `docker-compose.yml` mounts it at the identical path and sets `GAMEDOCK_DATA_DIR` to match.
- **Run as your own user** (`GAMEDOCK_UID`) and add the host's docker group id (`DOCKER_GID`) so the socket is usable without running as root.
- **The image has not been built on the project's test machine** (it has no Docker). Treat the first build as part of testing, and run `docker compose exec gamedock python -m gamedock doctor`.

### Steps

```bash
cd GameDock
mkdir -p /srv/gamedock/data
cat > .env <<EOT
GAMEDOCK_UID=$(id -u)
DOCKER_GID=$(getent group docker | cut -d: -f3)
GAMEDOCK_DATA_HOST=/srv/gamedock/data
EOT
docker compose up -d --build
docker compose exec gamedock python -m gamedock doctor
```

Then open `http://127.0.0.1:8080` (from another computer, through an SSH tunnel). Stop with `docker compose down`; your game servers keep running.
