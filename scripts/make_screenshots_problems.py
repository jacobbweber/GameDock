"""Screenshots (and a browser smoke test) of the 0.6.1 known-problem banner, on the in-memory fake backend with made-up
demo data: an ASKA-style server whose game log says Steam rejected the login token, on the server card and in Check setup.

    .venv/bin/python scripts/make_screenshots_problems.py --out docs --prefix gamedock-061 [--chrome /usr/bin/google-chrome]
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamedock.backends import FakeBackend  # noqa: E402
from gamedock.service import GameDockService  # noqa: E402
from gamedock.web.app import create_app  # noqa: E402

DEMO_TOKEN = "0123456789abcdef0123456789abcdef"   # made up: matches the shape, belongs to no account
CONTAINER_LOG = "[entrypoint] Updating the game with SteamCMD\nSuccess! App '3246670' fully installed.\n[entrypoint] Starting the server\n"
GAME_LOG = ("[10:02:11] Initializing Steam game server\n[10:02:14] Failed to connect to Steam reason: k_EResultAccountNotFound\n"
            "[10:02:14] Disconnected with reason: CustomAuthenticationFailed\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-061")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8128)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    catalog = work / "catalog"
    shutil.copytree(ROOT / "catalog", catalog)
    shutil.copy(ROOT / "docs" / "catalog-entries" / "aska.yaml", catalog / "aska.yaml")   # the example entry from this repository
    backend = FakeBackend()
    service = GameDockService(backend, catalog, work / "data")
    service.deploy("minecraft-java", "Creative Build", env={"EULA": "TRUE"}, start=True)
    sid = service.deploy("aska", "Family ASKA", env={"ASKA_AUTH_TOKEN": DEMO_TOKEN}, start=True).id
    backend.health[f"gamedock-{sid}"] = "unhealthy"
    backend.set_logs(f"gamedock-{sid}", CONTAINER_LOG)
    log = work / "data" / sid / "server" / "logs" / "aska_game_output.log"
    log.parent.mkdir(parents=True)
    log.write_text(GAME_LOG)

    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{args.port}"
    errors: list[str] = []

    def shot(pg, name, locator=None):
        pg.wait_for_timeout(300)
        path = str(out / f"{args.prefix}-{name}.png")
        (pg.locator(locator) if locator else pg).screenshot(path=path)
        print("wrote", f"{args.prefix}-{name}.png")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
            pg = b.new_page(viewport={"width": 1000, "height": 700})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            pg.goto(base + "/")
            card = pg.locator(f"#card-{sid}")
            expect(card.locator(".problem-box")).to_be_visible(timeout=8000)
            expect(card.locator(".problem-title")).to_contain_text("Steam rejected the login token")
            expect(card.locator(".problem-seen")).to_contain_text("CustomAuthenticationFailed")
            if DEMO_TOKEN in card.inner_text():
                errors.append("the card showed the token")
            shot(pg, "problem-card", f"#card-{sid}")

            card.locator('button[aria-label^="Check setup "]').click()
            expect(pg.locator("#check-list .chk-item").first).to_be_visible(timeout=8000)
            expect(pg.locator("#check-list")).to_contain_text("Known problem")
            if DEMO_TOKEN in pg.locator("dialog[open]").inner_text():
                errors.append("Check setup showed the token")
            shot(pg, "problem-check")
            pg.keyboard.press("Escape")
            b.close()
    finally:
        server.should_exit = True
        shutil.rmtree(work, ignore_errors=True)
    if errors:
        print("browser problems:", "; ".join(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()
