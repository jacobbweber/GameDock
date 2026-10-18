"""Screenshots (and a browser smoke test) of the 0.5.2 "differs from the default" markers in the Config form, on the
in-memory fake backend with made-up demo files (a Palworld-style OptionSettings file and an Enshrouded-style JSON).

    .venv/bin/python scripts/make_screenshots_defaults.py --out docs --prefix gamedock-052 [--chrome /usr/bin/google-chrome]
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
sys.path.insert(0, str(ROOT / "scripts"))

from check_dialog_layout import enshrouded_json  # noqa: E402

from gamedock.backends import FakeBackend  # noqa: E402
from gamedock.service import GameDockService  # noqa: E402
from gamedock.web.app import create_app  # noqa: E402

PAL_PATH = "palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
PAL_TEXT = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    'OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,NightTimeSpeedRate=1.000000,ExpRate=1.000000,'
    'DeathPenalty=All,bIsPvP=False,ServerPlayerMaxNum=16,ServerName="Family Palworld",ServerDescription="",'
    'AdminPassword="demo-admin-secret",ServerPassword="",CrossplayPlatforms=(Steam,Xbox),RCONEnabled=False)\n'
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-052")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8126)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    service = GameDockService(FakeBackend(), ROOT / "catalog", work / "data")
    ens = service.deploy("enshrouded", "weber brothers", env={}).id
    pal = service.deploy("palworld", "Family Palworld", env={"ADMIN_PASSWORD": "demo-admin-secret"}).id
    ens_dir = work / "data" / ens / "game" / "server"
    ens_dir.mkdir(parents=True)
    (ens_dir / "enshrouded_server.json").write_text(enshrouded_json())
    pal_file = work / "data" / pal / PAL_PATH
    pal_file.parent.mkdir(parents=True)
    pal_file.write_text(PAL_TEXT)
    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{args.port}"
    errors: list[str] = []

    def shot(pg, name, to_marked=False):
        """`to_marked`: scroll the list so that the first setting that differs from the default is at the top."""
        pg.evaluate("() => { const l = document.querySelector('.cfg-list'); if (l) l.scrollTop = 0; }")
        if to_marked:
            pg.evaluate("""() => { const l = document.querySelector('.cfg-list'), r = document.querySelector('.cfg-row.nondefault:not([hidden])');
                if (l && r) l.scrollTop = r.getBoundingClientRect().top - l.getBoundingClientRect().top - 60; }""")
        pg.wait_for_timeout(250)
        pg.screenshot(path=str(out / f"{args.prefix}-{name}.png"))
        print("wrote", f"{args.prefix}-{name}.png")

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1000, "height": 880})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        def open_config(sid):
            pg.goto(base + "/")
            pg.locator(f'#card-{sid} button[aria-label^="Config "]').first.click()
            expect(pg.locator(".cfg-row").first).to_be_visible()
            pg.wait_for_timeout(250)

        def set_value(field, value):
            loc = pg.locator(f'[id="cfg-{field}"]')
            loc.scroll_into_view_if_needed()
            if value == "toggle":
                loc.evaluate("e => e.click()")
            else:
                loc.fill(value)

        # 1. the very first time the file is opened: the defaults are recorded just now
        open_config(ens)
        expect(pg.locator("#cfg-baseline")).to_contain_text("first recorded just now")
        shot(pg, "config-first-recorded")

        # 2. change some settings and save
        for f, v in [("gameSettings.enableDurability", "toggle"), ("gameSettings.foodBuffDurationFactor", "2"),
                     ("gameSettings.enableGliderTurbulences", "toggle"), ("gameSettings.randomSpawnerAmount", "Many"),
                     ("slotCount", "4")]:
            set_value(f, v)
        pg.locator("#config-save").click()
        expect(pg.locator(".okmsg")).to_be_visible(timeout=8000)
        pg.keyboard.press("Escape")

        # 3. come back later: the review list is gone, the markers and the default lines are still there
        open_config(ens)
        expect(pg.locator(".cfg-row.nondefault")).to_have_count(5)
        shot(pg, "config-not-default", to_marked=True)

        # 4. only differing from default (survives closing and reopening)
        pg.locator("#cfg-only-default").check()
        expect(pg.locator(".cfg-row:visible")).to_have_count(5)
        shot(pg, "config-only-differing")

        # 5. reset all asks first and only fills the form
        pg.locator("#cfg-reset-all").click()
        expect(pg.locator("#cfg-reset-all-yes")).to_be_visible()
        shot(pg, "config-reset-all-confirm")
        pg.locator("#cfg-reset-all-yes").click()
        expect(pg.locator(".cfg-row.nondefault")).to_have_count(0)
        shot(pg, "config-reset-all-filled")
        pg.keyboard.press("Escape")

        # 6. Palworld-style OptionSettings: a changed, a new and a missing setting, and a password that is set
        open_config(pal)
        for f, v in [("ServerPlayerMaxNum", "24"), ("bIsPvP", "toggle"), ("DayTimeSpeedRate", "2.5"), ("ServerPassword", "joinme")]:
            set_value(f, v)
        pg.locator("#config-save").click()
        expect(pg.locator(".okmsg")).to_be_visible(timeout=8000)
        pg.keyboard.press("Escape")
        # a game update adds a setting and drops one; the game also rewrites 1.000000 as 1 (not a change)
        pal_file.write_text(pal_file.read_text().replace("DeathPenalty=All,", "").replace("ExpRate=1.000000", "ExpRate=1")
                            .replace("RCONEnabled=False)", "RCONEnabled=False,bAllowGlobalPalboxExport=True)"))
        open_config(pal)
        expect(pg.locator(".cfg-row.nondefault")).to_have_count(5)   # 4 changed + 1 new
        expect(pg.locator(".missing-row")).to_have_count(1)
        shot(pg, "config-palworld", to_marked=True)
        pg.locator("#cfg-only-default").check()
        pg.evaluate("() => { const l = document.querySelector('.cfg-list'); l.scrollTop = l.scrollHeight; }")
        pg.wait_for_timeout(250)
        pg.screenshot(path=str(out / f"{args.prefix}-config-palworld-new-missing.png"))
        print("wrote", f"{args.prefix}-config-palworld-new-missing.png")
        pg.locator("#cfg-record-baseline").scroll_into_view_if_needed()
        pg.locator("#cfg-record-baseline").click()
        expect(pg.locator("#cfg-record-yes")).to_be_visible()
        shot(pg, "config-record-confirm")
        b.close()
    server.should_exit = True
    shutil.rmtree(work, ignore_errors=True)
    if errors:
        raise SystemExit("browser errors: " + "; ".join(errors))
    print("ok")


if __name__ == "__main__":
    main()
