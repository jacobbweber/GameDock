"""Screenshots (and a browser smoke test) of the 0.6.0 troubleshooting tools, on the in-memory fake backend with made-up
demo data: the Check setup checklist, the port-conflict note in the Deploy dialog and the diagnostics.

    .venv/bin/python scripts/make_screenshots_checks.py --out docs --prefix gamedock-060 [--chrome /usr/bin/google-chrome]
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

from gamedock import steam  # noqa: E402
from gamedock.backends import FakeBackend  # noqa: E402
from gamedock.service import GameDockService  # noqa: E402
from gamedock.web.app import create_app  # noqa: E402

LOG = ("[2026-10-18 10:01:02] Starting dedicated server\n[2026-10-18 10:01:20] Loading world 'Family'\n"
       "[2026-10-18 10:01:41] login password=demo-admin-secret accepted\n[2026-10-18 10:02:05] Setting breakpad minidump AppID = 2394010\n"
       "[2026-10-18 10:02:09] Running Palworld dedicated server on :8211\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-060")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8127)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    backend = FakeBackend()
    service = GameDockService(backend, ROOT / "catalog", work / "data")
    pal = service.deploy("palworld", "Family Palworld", env={"ADMIN_PASSWORD": "demo-admin-secret"}, start=True).id
    service.deploy("minecraft-java", "Creative Build", env={"EULA": "TRUE"}, start=True)
    backend.set_logs(f"gamedock-{pal}", LOG)
    # a Steam record with another app id than the catalog's: a realistic mistake the check should explain
    d = work / "data" / pal / "palworld" / "steamapps"
    d.mkdir(parents=True)
    (d / "appmanifest_1623730.acf").write_text(steam.render_manifest("1623730", "25247047"))
    backend.restart_counts[f"gamedock-{pal}"] = 0

    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{args.port}"
    errors: list[str] = []

    def shot(pg, name):
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-{name}.png"))
        print("wrote", f"{args.prefix}-{name}.png")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
            pg = b.new_page(viewport={"width": 1000, "height": 880})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            pg.goto(base + "/")
            pg.locator(f'#card-{pal} button[aria-label^="Check setup "]').click()
            expect(pg.locator("#check-list .chk-item").first).to_be_visible(timeout=8000)
            expect(pg.locator("#check-summary")).to_be_visible()
            shot(pg, "check-setup")
            pg.locator("#diag-details summary").click()
            pg.evaluate("() => { const b = document.querySelector('dialog[open] .dialog-body'); if (b) b.scrollTop = b.scrollHeight; }")
            expect(pg.locator("#diag-text")).to_contain_text("GameDock diagnostics")
            text = pg.locator("#diag-text").inner_text()
            if "demo-admin-secret" in text:
                errors.append("the diagnostics showed a password")
            shot(pg, "diagnostics")
            pg.keyboard.press("Escape")

            pg.goto(base + "/#/catalog")
            pg.locator("#catalog-minecraft-java button.primary").click()
            expect(pg.locator("dialog[open] .port-note")).to_be_visible(timeout=8000)
            shot(pg, "port-conflict")
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
