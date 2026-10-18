# Task prompt for an AI harness: add a game to GameDock

Copy everything in the box below into your harness (AutoReiv, Crucible, Agent-OS or similar) as the task. Replace `<<GAME NAME>>` and, if you know them, the optional hints. Leave the rest as it is.

The harness needs an MCP server connection to GameDock first (HTTP / Streamable HTTP, URL `http://192.168.1.29:8100/mcp`, header `Authorization: Bearer <token>`). The token is in GameDock's `.env` file on the Nimo machine (`/home/nimoadmin/GameDock/.env`, the line `GAMEDOCK_MCP_TOKEN=`); put it into the harness's secret store, not into this prompt.

```text
TASK: Add the game "<<GAME NAME>>" to GameDock's catalog, test it, and report back.

GameDock is a small manager for game servers that each run in their own Docker container. You are
connected to it over MCP.

CONNECTING
- MCP endpoint: http://192.168.1.29:8100/mcp (HTTP / Streamable HTTP).
- Authentication: the header "Authorization: Bearer <token>". The token is the value of
  GAMEDOCK_MCP_TOKEN in the GameDock .env file on the Nimo machine. Use the token only in that header.
  NEVER print, log, quote or repeat the token, in any message, file or tool argument.
- After connecting, list the tools. Tell me which mode you have (read-only, standard or full).

RULES
1. Do not invent anything. Never make up a Docker image name, a port, a protocol, a folder, an environment
   variable or a Steam app id. Research each one from the image's own documentation (Docker Hub or
   GitHub), SteamDB, or the game's official pages. Write the links you used into the entry's "notes". If
   you cannot confirm something, leave it out and say so in "notes" and in your report.
2. Never put a password or token in the entry. Passwords are fields with "secret: true".
3. Entries you add are marked "Added by AI, needs your review". You cannot approve them and they cannot
   be deployed until I approve them in the GameDock web page. Do not try to get around that, and do not
   call GameDock's web API directly.
4. Do not change, stop, delete, back up or restore any existing server. Do not touch the existing
   catalog entries.
5. Never paste my files or secrets into your reports.

STEPS
1. Call get_add_game_guide (or read the resource gamedock://add-a-game). Follow its ten steps.
2. Research the dedicated server for "<<GAME NAME>>": a maintained Docker image and a specific tag, the
   ports with their protocols (UDP or TCP), the folders that hold saves and settings, the environment
   variables (and which are secret), the Steam app id of the DEDICATED SERVER (not the game client) and how
   the image updates the game with SteamCMD, and the log lines for ready, version and download progress.
   Optional hints from me: <<IMAGE / LINKS / NOTES, or "none">>.
3. Call inspect_image on the image. It never downloads or runs the image. If the image is not on the
   machine, say so; ask me to run "docker pull <image>" on the GameDock machine if you need its declared
   ports and folders, then call inspect_image again.
4. Write the catalog entry from the template in the guide. Call validate_catalog_entry repeatedly until
   "valid" is true. Read the warnings and fix those that apply.
5. If you have standard or full mode: call add_catalog_entry. If you only have read-only mode: stop here
   and give me the validated YAML in your report. After adding, tell me the entry is waiting for my review
   (GameDock web page > Catalog > Review > Approve). Wait for me to say it is approved. Do not deploy
   before that.
6. After my approval:
   - If you have FULL mode and I have said you may test: deploy_server a test copy named
     "test-<<game id>>" on SPARE host ports (20000 or higher; call list_servers first so you do not use a
     port that is taken), with start=true.
   - Otherwise (standard or read-only mode, or if I have not said you may test): ask me before you deploy,
     and tell me exactly which ports you would use.
   Wait for "ready" to be true (the first start can take many minutes: poll get_server_status and read
   get_server_logs). Do not tell me to join before it is ready.
7. Verify, using the checklist in the guide: logs show the ready and version lines and they match the
   patterns; read_config_file works for each listed config file; an empty password means no password;
   create_backup works and list_backups shows it; stop_server and start_server work. Do not restore or
   delete anything. If something fails, use update_catalog_entry to fix the entry (it goes back to "needs
   review", so tell me), and try again.
8. Stop the test server when finished only if I have said so; otherwise leave it running and tell me.

REPORT (use exactly this structure)
- Game: name and the id you chose.
- Result: PASS, PARTIAL or FAIL, in one sentence.
- Entry: id, file name, image and tag, ports (protocol, host and container), folders, secret fields.
- Sources: the links you used for each fact.
- Not verified: everything you could not confirm, and any guesses you refused to make.
- Review status: reviewed or waiting for my approval.
- Test server: id, host ports, how long the first start took, final status and phase.
- Checklist: each item from the guide's verification checklist with PASS, FAIL or NOT TESTED and a short
  note.
- Problems and fixes: what failed, what you changed in the entry (and how many times you updated it).
- Next steps for me: what I need to do or decide.
```

## After the agent has finished

1. Open the GameDock web page, **Catalog**, and press **Review** on the entry. Read the plain summary and the YAML. Approve it if it looks right, or Reject it (that deletes the entry).
2. When the verification is done, the agent's report says what was checked. Anything marked NOT TESTED is still open.
