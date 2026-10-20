"""Screenshots (and a browser smoke test) of the 0.6.3 setting rules, on the in-memory fake backend with made-up demo
data: the Enshrouded tombstoneMode dropdown, a wrong value already in the file ("Off") marked with its one-click fix,
and the card of a server whose game refused its settings ("Failed to start").

    .venv/bin/python scripts/make_screenshots_settings.py --out docs --prefix gamedock-063 [--chrome /usr/bin/google-chrome]
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
from tests.test_enshrouded import REAL_JSON, SETTINGS_PATH  # noqa: E402

OFF_JSON = REAL_JSON.replace('"tombstoneMode": "AddBackpackMaterials"', '"tombstoneMode": "Off"')
LOG = ("[Session] 'HostOnline' (up)!\n[error] Failed to parse enum 'TombstoneMode: Invalid value 'Off'. Supported "
       "'TombstoneMode' values: AddBackpackMaterials, Everything, NoTombstone\n[error] Failed to parse Config!\n"
       "INFO gave up: enshrouded entered FATAL state, too many start retries too quickly\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-063")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8129)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    catalog = work / "catalog"
    shutil.copytree(ROOT / "catalog", catalog)
    backend = FakeBackend()
    service = GameDockService(backend, catalog, work / "data")
    good = service.deploy("enshrouded", "Weekend World").id
    for sid, text in ((good, REAL_JSON),):
        p = work / "data" / sid / SETTINGS_PATH
        p.parent.mkdir(parents=True)
        p.write_text(text)

    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{args.port}"
    errors: list[str] = []

    def shot(pg, name, locator=None):
        pg.wait_for_timeout(300)
        (pg.locator(locator) if locator else pg).screenshot(path=str(out / f"{args.prefix}-{name}.png"))
        print("wrote", f"{args.prefix}-{name}.png")

    def open_config(pg, sid):
        pg.goto(base + "/")
        pg.locator(f'#card-{sid} button[aria-label^="Config "]').first.click()
        expect(pg.locator(".cfg-row").first).to_be_visible(timeout=8000)
        pg.locator("#cfg-search").fill("tombstone")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
            pg = b.new_page(viewport={"width": 1000, "height": 1100})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            open_config(pg, good)
            sel = pg.locator('[id="cfg-gameSettings.tombstoneMode"]')
            expect(sel).to_have_value("AddBackpackMaterials")
            assert sel.evaluate("e => e.tagName") == "SELECT"
            shot(pg, "tombstone-dropdown", "dialog[open]")
            pg.keyboard.press("Escape")

            (work / "data" / good / SETTINGS_PATH).write_text(OFF_JSON)
            open_config(pg, good)
            expect(pg.locator('[id="bad-gameSettings.tombstoneMode"]')).to_be_visible()
            expect(pg.locator('[id="bad-gameSettings.tombstoneMode"]')).to_contain_text("Use")
            shot(pg, "invalid-value", "dialog[open]")
            pg.keyboard.press("Escape")

            service.start(good)
            backend.set_logs(f"gamedock-{good}", LOG)
            service._problem_cache.clear()
            service._readiness_text.clear()
            pg.goto(base + "/")
            card = pg.locator(f"#card-{good}")
            expect(card).to_contain_text("Failed to start", timeout=8000)
            expect(card).to_contain_text("NoTombstone")
            shot(pg, "failed-card", f"#card-{good}")
            b.close()
    finally:
        server.should_exit = True
        shutil.rmtree(work, ignore_errors=True)
    if errors:
        print("browser problems:", "; ".join(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()
