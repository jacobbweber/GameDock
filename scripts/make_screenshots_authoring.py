"""Screenshots (and a browser smoke test) of the 0.5.0 review step for games that an AI agent added, on the in-memory
fake backend. A copy of the catalog is used, so nothing is added to the real catalog folder. The game shown is made up.

    .venv/bin/python scripts/make_screenshots_authoring.py --out docs --prefix gamedock-050 [--chrome /usr/bin/google-chrome]
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamedock.backends import FakeBackend
from gamedock.service import GameDockService
from gamedock.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
DEMO = """\
# Added by an AI agent through GameDock's MCP endpoint (made-up demo game for the screenshots).
id: example-game
name: Example Game Dedicated Server
image: example/example-game-server:latest
description: A made-up game used only to show the review step.
notes: >-
  DEMO. Image and ports were taken from the (made-up) image page. Not verified: the Steam app id and the ready line.
ports:
  - { protocol: udp, host: 27100, container: 27015 }
  - { protocol: tcp, host: 27101, container: 27016 }
volumes:
  - { host: ./saves, container: /home/steam/saves }
  - { host: ./game, container: /opt/game }
env:
  PUID: "1000"
  PGID: "1000"
fields:
  - { name: SERVER_NAME, label: Server name, default: "My Example Server", required: true }
  - { name: SERVER_PASSWORD, label: Server password, secret: true, description: Leave empty for no password. }
  - { name: ADMIN_PASSWORD, label: Admin password, secret: true, required: true }
config_files:
  - { path: saves/settings.ini, label: Game settings, format: ini, sensitive_keys: [AdminPassword] }
progress_log_pattern: "progress: *([0-9]+(?:[.][0-9]+)?)"
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-050")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8125)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    catalog = work / "catalog"
    shutil.copytree(ROOT / "catalog", catalog)
    backend = FakeBackend()
    service = GameDockService(backend, catalog, work / "data")
    service.add_catalog_entry(DEMO)           # exactly what the MCP tool add_catalog_entry does
    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{args.port}"

    errors: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1200, "height": 900})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # 1. the banner on the dashboard, then the catalog with the badge and the disabled Deploy button
        pg.goto(base + "/")
        expect(pg.locator("#banners .banner.info")).to_contain_text("added by an AI agent")
        pg.goto(base + "/#/catalog")
        card = pg.locator("#catalog-example-game")
        expect(card.locator(".review-badge")).to_have_text("Added by AI, needs your review")
        expect(card.locator("button", has_text="Deploy")).to_be_disabled()
        expect(pg.locator("#catalog-palworld button", has_text="Deploy")).to_be_enabled()   # normal entries are unchanged
        expect(pg.locator("#catalog-palworld .review-badge")).to_have_count(0)
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-catalog-unreviewed.png"))

        # 2. the Review dialog
        card.locator("button", has_text="Review").click()
        dlg = pg.locator("dialog[open]")
        expect(dlg.locator("h3")).to_contain_text("Review: Example Game Dedicated Server")
        expect(dlg.locator(".review-lines")).to_contain_text("example/example-game-server:latest")
        expect(dlg.locator(".review-lines")).to_contain_text("./saves as /home/steam/saves")
        expect(dlg.locator(".review-lines")).to_contain_text("Secret (password) boxes: SERVER_PASSWORD, ADMIN_PASSWORD")
        expect(dlg.locator(".review-flags")).to_contain_text("no fixed version")
        expect(dlg.locator(".yaml-view")).to_contain_text("id: example-game")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-review-dialog.png"))

        # 3. Reject asks again; "No" keeps the entry
        dlg.locator("button", has_text="Reject").click()
        expect(dlg.locator(".confirm-text")).to_contain_text("Delete this entry's file for good?")
        pg.wait_for_timeout(200)
        pg.screenshot(path=str(out / f"{args.prefix}-reject-confirm.png"))
        dlg.locator("button", has_text="No, keep it").click()
        expect(pg.locator("dialog[open] button", has_text="Approve")).to_be_visible()

        # 4. Approve: the badge changes and Deploy works
        pg.locator("dialog[open] button", has_text="Approve").click()
        expect(card.locator(".review-ok")).to_have_text("Added by AI, reviewed by you")
        expect(card.locator("button", has_text="Deploy")).to_be_enabled()
        expect(pg.locator("#banners .banner.info")).to_have_count(0, timeout=20000)
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-catalog-approved.png"))
        b.close()
    server.should_exit = True
    shutil.rmtree(work, ignore_errors=True)
    if errors:
        raise SystemExit("browser errors: " + "; ".join(errors))
    print("ok")


if __name__ == "__main__":
    main()
