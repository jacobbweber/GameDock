"""Screenshots (and a browser smoke test) of the 0.4.0 game version features, on the in-memory fake backend.

A pretend Steam data service reports a newer Palworld build than the one "installed", the fake engine updates the game
files whenever a container starts (like UPDATE_ON_BOOT), and the script clicks through the tile, the Updates dialog and
"Update game now". Needs Playwright and Chrome (see requirements-dev.txt):

    .venv/bin/python scripts/make_screenshots_game.py --out docs --prefix gamedock-040
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamedock.backends import FakeBackend
from gamedock.service import GameDockService
from gamedock.steam import SteamInfo
from gamedock.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = {"25247047": "v1.0.5.102999", "25301234": "v1.0.6.103400", "20112233": "0.221.4"}
RELEASED = {"2394010": 1792301000, "896660": 1791900000}


class Steam:
    """A pretend public Steam data service: newest public build per app id."""

    def __init__(self):
        self.builds = {"2394010": "25247047", "896660": "20112233"}

    def __call__(self, url, timeout):
        app = url.rsplit("/", 1)[-1]
        doc = {"data": {app: {"depots": {"branches": {"public": {"buildid": self.builds[app],
                                                                 "timeupdated": str(RELEASED[app])}}}}}}
        return 200, json.dumps(doc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-040")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8124)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    data = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    backend = FakeBackend()
    mirror = Steam()
    info = SteamInfo(data / "steam-latest.json", http_get=mirror, manual_gap=0)
    service = GameDockService(backend, ROOT / "catalog", data, steam_info=info, game_update_settle=0, game_update_poll=0.2)
    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{args.port}"

    def pretend_steam_game(sid, app):
        installed = {"build": None}

        def build():
            installed["build"] = mirror.builds[app]          # the image updates the game on every start
            return installed["build"], 1792297341
        backend.simulate_steam_game(f"gamedock-{sid}", app, build,
                                    version=lambda b: f"[2026.10.18-00:22:11] Game version is {VERSIONS[b]}")

    pal = service.deploy("palworld", "Family Palworld", env={"ADMIN_PASSWORD": "hunter2hunter2"}).id
    val = service.deploy("valheim", "Viking Night", env={"SERVER_PASS": "valpass1"}).id
    mc = service.deploy("minecraft-java", "Creative Build", env={"EULA": "TRUE"}).id
    pretend_steam_game(pal, "2394010")
    pretend_steam_game(val, "896660")
    for sid in (pal, val, mc):
        service.start(sid)
        service._manifest_missing.clear()
    backend.append_log(f"gamedock-{pal}", "Running Palworld dedicated server on :8211")
    (data / pal / "palworld" / "Saved").mkdir(parents=True)
    (data / pal / "palworld" / "Saved" / "world.sav").write_text("world")
    for sid in (pal, val):
        service.check_game_update(sid)

    errors: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1200, "height": 900})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # 1. everything current: the tile line says the game version and "up to date"
        pg.goto(base + "/")
        card = pg.locator(f"#card-{pal}")
        expect(card.locator(".game-line")).to_have_text("Game version v1.0.5.102999, up to date")
        expect(pg.locator(f"#card-{val} .game-line")).to_have_text("Game build 20112233, up to date")
        expect(pg.locator(f"#card-{mc} .game-line")).to_have_count(0)           # not a Steam game: no line
        expect(card.locator(".update-flag")).to_have_count(0)
        expect(card.locator(".status-text.ok")).to_contain_text("Ready to join", timeout=15000)
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / f"{args.prefix}-up-to-date.png"))
        card.locator("button", has_text="Updates").click()
        expect(pg.locator("#game-status-badge")).to_have_text("Up to date")
        expect(pg.locator("#game-update-now")).to_be_disabled()
        pg.screenshot(path=str(out / f"{args.prefix}-dialog-up-to-date.png"))
        pg.keyboard.press("Escape")

        # 2. Steam publishes a new build: the tile shows the game update, distinct from an image update
        mirror.builds["2394010"] = "25301234"
        service.check_game_update(pal)
        pg.reload()
        expect(card.locator(".game-line")).to_have_text("Game version v1.0.5.102999, newer version available")
        expect(card.locator(".update-flag .badge")).to_have_text("Update available")
        expect(card.locator(".update-flag .hint")).to_contain_text("build 25247047 \u2192 25301234")
        pg.wait_for_timeout(400)
        pg.screenshot(path=str(out / f"{args.prefix}-tile-update.png"))
        card.screenshot(path=str(out / f"{args.prefix}-tile-only.png"))

        # 3. the Updates dialog
        card.locator(".update-flag button").click()
        expect(pg.locator("#game-status-badge")).to_have_text("Update available")
        expect(pg.locator("#game-facts")).to_contain_text("v1.0.5.102999")
        expect(pg.locator("#game-facts")).to_contain_text("25301234")
        expect(pg.locator("#game-update-now")).to_be_enabled()
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-dialog.png"))

        # 4. Update game now: confirmation, then working, then the result
        backend.delays["stop"] = 2
        pg.locator("#game-update-now").click()
        expect(pg.locator("dialog h3")).to_contain_text("Update the game on Family Palworld?")
        pg.locator("dialog label.check input").check()
        pg.locator("dialog button[type=submit]").click()
        expect(pg.locator(f"#card-{pal} .badge.busy")).to_contain_text("Updating game", timeout=6000)
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / f"{args.prefix}-updating.png"))
        expect(pg.locator("dialog h3")).to_have_text("Game updated", timeout=30000)
        expect(pg.locator("dialog")).to_contain_text("from build 25247047 to build 25301234")
        pg.screenshot(path=str(out / f"{args.prefix}-result.png"))
        pg.locator("dialog button", has_text="OK").click()
        pg.reload()
        expect(card.locator(".game-line")).to_have_text("Game version v1.0.6.103400, up to date", timeout=15000)
        expect(card.locator(".update-flag")).to_have_count(0)
        pg.wait_for_timeout(400)
        pg.screenshot(path=str(out / f"{args.prefix}-after-update.png"))

        # 5. a game that does not update on restart is reported plainly
        mirror.builds["896660"] = "20999999"
        service.check_game_update(val)
        pretend_steam_game(val, "896660")
        backend._start_hooks[f"gamedock-{val}"] = lambda: None          # this image does not update itself
        pg.reload()
        pg.locator(f"#card-{val} .update-flag button").click()
        pg.locator("#game-update-now").click()
        pg.locator("dialog label.check input").check()
        pg.locator("dialog button[type=submit]").click()
        expect(pg.locator("dialog h3")).to_have_text("The game was not confirmed as updated", timeout=30000)
        expect(pg.locator("dialog")).to_contain_text("did not update on restart")
        pg.screenshot(path=str(out / f"{args.prefix}-not-updated.png"))
        b.close()
    shutil.rmtree(data, ignore_errors=True)
    server.should_exit = True
    if errors:
        raise SystemExit("Browser errors:\n" + "\n".join(errors))
    print("OK: screenshots written to", out)


if __name__ == "__main__":
    main()
