# First test of GameDock on Nimo

A short checklist. Do the steps in order and stop at the first thing that looks wrong (see "If something goes wrong" at the bottom). Total time: about 20 minutes.

**What this test does:** runs GameDock for the first time against the real Docker on Nimo, using a tiny harmless web server (the "TEST" entry) instead of a game.

## Read this first: what must not be touched

Ports already in use on Nimo:

| Port | Used by |
|---|---|
| 8211 (UDP) | Palworld server |
| 15637 (UDP) | Enshrouded server |
| 11434 | Ollama |
| 9090 | something already listening |
| 3001 | something already listening |
| 9256 | something already listening |

- **The existing Palworld and Enshrouded servers must not be touched.** GameDock only manages containers that *it* created (they carry the label `gamedock.managed=true` and are named `gamedock-...`). Your existing containers will not show up in GameDock, and GameDock will not start, stop, change or delete them.
- **Do not deploy Palworld or Enshrouded from the GameDock catalog during this test.** Their default ports (8211, 27015, 15637) are the ones already in use.
- The test uses **port 18080** (TCP) for the test container and **port 8080** for GameDock's own page. Step 3 checks both are free. If 8080 is taken, use 8081 everywhere below.
- Never run `docker rm $(docker ps -aq)`, `docker system prune`, or `docker stop` on things you did not start for this test.

## Steps

### 1. Write down what is running now (so you can compare at the end)

```bash
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | tee ~/docker-before.txt
```

### 2. Clone, create the virtual environment, install

```bash
cd ~
git clone https://github.com/jacobbweber/GameDock.git
cd GameDock
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

You should see the install finish without errors. (If `python3 -m venv` complains, install the venv package for your system, for example `sudo apt install python3-venv`.)

### 3. Check that the two ports are free

```bash
ss -ltn | grep -E ':(8080|18080)\b' || echo "both free"
```

You want to see `both free`. If something is listed, pick other numbers (set `GAMEDOCK_PORT` in step 5; for the test container you can change the host port in the deploy form in step 8).

### 4. Run the doctor

```bash
python -m gamedock doctor
```

It prints one line per check: `[ OK ]`, `[WARN]`, `[FAIL]` or `[INFO]`, and a "How to fix" under each problem. Expected on a good Nimo: Docker found, running and usable, data folder writable, catalog valid.

- Any `[FAIL]` line: fix it as the message says, run the doctor again, and do not go on until there are none. The usual one is "permission denied" on Docker: run `sudo usermod -aG docker $USER`, then log out and back in.
- `[WARN]` lines: read them; they are usually fine for a first test.

Nothing has been created or started yet; the doctor only looks.

### 5. Start GameDock with the real Docker engine, bound to this machine only

```bash
printf 'GAMEDOCK_BACKEND=docker\nGAMEDOCK_HOST=127.0.0.1\nGAMEDOCK_PORT=8080\n' > .env
python -m gamedock
```

Leave this window open: GameDock runs in it, and its messages appear here. (Use a second SSH window for the rest, or run it inside `tmux`.) You should see a line saying it is listening on `127.0.0.1:8080`. It is **not** reachable from the network, only from Nimo itself.

### 6. Open the page from your Windows PC with an SSH tunnel

On the Windows PC, open PowerShell and run (replace the two placeholders):

```powershell
ssh -L 8080:127.0.0.1:8080 <your-user>@<nimo-address>
```

Keep that window open; it is the tunnel. Then open **http://127.0.0.1:8080** in the Windows browser. (If port 8080 is already used on the Windows PC, use another number on the left, for example `ssh -L 18081:127.0.0.1:8080 <your-user>@<nimo-address>` and open `http://127.0.0.1:18081`.)

You should see the GameDock dashboard with "No servers yet" and a green "connected" at the top right. If a red banner says Docker is not available, click "See how to fix this" (it opens the Health page).

Then click **Health** and press **Run checks**. It should show the same results as step 4.

### 7. Look at the catalog

Click **Catalog**. Find **"TEST: tiny web server (not a game)"**. Leave the Palworld and Enshrouded cards alone.

### 8. Deploy the test container

1. Press **Deploy** on the TEST card.
2. Name: `smoke test`. Keep host port `18080`. Leave the other fields as they are.
3. Tick **Start after deploy** and press Deploy.

The first time, Docker downloads the tiny image (a few seconds). You should see a card named "smoke test" with a green **RUNNING** badge.

On Nimo (second SSH window) check:

```bash
curl -s http://127.0.0.1:18080 | head -5
docker ps --filter label=gamedock.managed=true --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
```

You should see a few lines of text from the web server (starting with `Name: gamedock-smoke-test`) and exactly one container named `gamedock-...` in the list. Your Palworld and Enshrouded containers are not in that list and are unchanged.

(Docker publishes the test port on all of Nimo's network addresses, like any game server port. That is fine for this harmless test, and it is why the test is short.)

### 9. Stop and start

1. Press **Stop**. The badge turns to **STOPPED**. The `curl` from step 8 now fails (this is expected).
2. Press **Start**. Back to **RUNNING**, and `curl` works again.
3. Open **Monitoring**. After a few seconds you see CPU, memory and network numbers for the test container.

### 10. Edit its config file

1. On the card press **Config**, open "Test notes (edit me)", type a line such as `hello`, and Save. This works while running because the file is marked as editable live.
2. Check it on Nimo: `cat ~/GameDock/data/*/data/notes.txt` shows your line. After a second save there is also a `.bak` copy beside it.

### 11. Back up and restore

1. Press **Stop**, then **Backups**, then **Create backup**. A backup appears in the list.
2. On Nimo: `ls -l ~/GameDock/data/backups/*/` shows a `.tar.gz` file with permissions `-rw-------` (private).
3. Change the notes file again (step 10). Then in Backups press **Restore** on your backup and confirm. The notes file goes back to what it was when you made the backup, and an automatic "pre-restore" safety backup appears in the list.

If backup or restore ever says "permission denied" and mentions files owned by root, copy that message: it contains the fix. (The tiny test image does not write files, so this is not expected here, but game servers can.)

### 12. Delete the test server

1. Make sure it is stopped. Press **Delete**.
2. Tick **Also delete the data folder** and confirm both prompts. A last backup is saved under `data/backups/_deleted/` first.
3. The card disappears and the dashboard shows "No servers yet".

Check on Nimo:

```bash
docker ps -a --filter label=gamedock.managed=true     # should list nothing
ss -ltn | grep ':18080' || echo "18080 free again"
```

### 13. Compare with the start, then clean up

```bash
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | diff ~/docker-before.txt - && echo "Docker looks exactly as before"
```

You want "Docker looks exactly as before". (Times in the Status column, such as "Up 3 days", may differ; names and images must not.)

Then:

1. In the window running GameDock press **Ctrl+C**. Close the SSH tunnel window.
2. Optional: remove the downloaded test image: `docker rmi traefik/whoami:latest` (only that one; safe to skip).
3. Optional: remove what GameDock created: `rm -rf ~/GameDock/data` (this holds only the test server's files and backups). Remove `~/GameDock` entirely if you do not want to keep it.

Done. Tell whoever is helping you: the doctor output from step 4, anything red or surprising, and the last lines printed in the GameDock window.

## If something goes wrong

- **Stop GameDock** (Ctrl+C in its window). The test container, if it exists, keeps running by itself; that is harmless.
- **See what GameDock made:** `docker ps -a --filter label=gamedock.managed=true`. Only these containers belong to GameDock.
- **Remove only the test container:** `docker rm -f gamedock-<name shown in that list>`. Copy the exact name from the list; never remove anything that does not carry that label.
- **Remove only GameDock's files:** `rm -rf ~/GameDock/data`.
- Your Palworld and Enshrouded servers never needed any of this and should still be running. Check with `docker ps`.
- The README "Troubleshooting" table explains the common messages (Docker permission, port in use, root-owned files, "Host not allowed").
