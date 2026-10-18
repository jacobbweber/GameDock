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
- To update GameDock: `git pull`, `pip install -e .` (or `pip install .`), restart the service.

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
