"""Start GameDock on the in-memory fake backend with demo servers and take screenshots of the UI.

Also acts as a browser smoke test: it clicks through deploying, readiness, logs and the structured config form and
fails if a page error appears. Needs Playwright and a Chrome/Chromium (see requirements-dev.txt):

    .venv/bin/python scripts/make_screenshots.py --out docs --prefix gamedock-030 [--chrome /usr/bin/google-chrome]
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
import threading
import time
from pathlib import Path

import sys

import uvicorn
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # so the test helpers below can be imported

from gamedock.backends import FakeBackend
from gamedock.service import GameDockService
from gamedock.updates import UpdateChecker
from gamedock.web.app import create_app
from tests.gitrepo import Repos
from tests.test_self_update import Github

ROOT = Path(__file__).resolve().parent.parent
PAL_INI = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    'OptionSettings=(Difficulty=None,RandomizerType=None,DayTimeSpeedRate=1.000000,NightTimeSpeedRate=1.000000,'
    "ExpRate=1.000000,PalCaptureRate=1.000000,PalSpawnNumRate=1.000000,PlayerDamageRateAttack=1.000000,"
    "DeathPenalty=All,bEnablePlayerToPlayerDamage=False,bEnableFriendlyFire=False,bEnableInvaderEnemy=True,"
    "DropItemMaxNum=3000,BaseCampMaxNum=128,GuildPlayerMaxNum=20,bIsPvP=False,bHardcore=False,bPalLost=False,"
    "bEnableFastTravel=True,CoopPlayerMaxNum=4,ServerPlayerMaxNum=32,ServerName=\"Family Palworld, the sequel (EU)\","
    "ServerDescription=\"Friends only\",AdminPassword=\"hunter2hunter2\",ServerPassword=\"\","
    "PublicPort=8211,Region=\"\",bUseAuth=True,CrossplayPlatforms=(Steam,Xbox,PS5,Mac),"
    "bShowPlayerList=False,RCONEnabled=False,RCONPort=25575,AutoSaveSpan=30.000000)\n")
STEAM = ("Update state (0x61) downloading, progress: {p} ({a} / 4892200000)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-030")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8123)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    data = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    backend = FakeBackend()
    # a pretend GitHub with a newer GameDock than the "installed" one, so the header shows the update badge
    (data / "git-demo").mkdir()
    repos = Repos(data / "git-demo", version="0.3.0")
    repos.remote_commit(version="0.3.1", message="Nicer schedule summaries")
    updater = UpdateChecker(install_dir=repos.local, http_get=Github(repos), version="0.3.0", restart=lambda: None)
    service = GameDockService(backend, ROOT / "catalog", data, updater=updater)
    updater.check(force=True)
    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{args.port}"

    # demo servers
    pal = service.deploy("palworld", "Family Palworld", env={"ADMIN_PASSWORD": "hunter2hunter2"}).id
    service.deploy("valheim", "Viking Night", env={"SERVER_PASS": "valpass1"})
    service.deploy("minecraft-java", "Creative Build", env={"EULA": "TRUE"})
    service.start(pal)
    service.start("viking-night")
    # an image update waiting for Viking Night, and some schedules
    backend.publish_new_image(service.get_image_info("viking-night").image)
    service.check_image_update("viking-night", force=True)
    service.set_backup_schedule("viking-night", True, "daily", time_of_day="03:00", retention=7)
    service.set_maintenance_schedule("viking-night", True, "weekly", time_of_day="04:00", days=["sun"],
                                     backup_first=True, update_image=True, restart=True)
    backend.health["gamedock-" + pal] = "starting"
    backend.set_logs("gamedock-" + pal, "\n".join([
        "\x1b[36m[INFO]\x1b[0m Starting Palworld server container",
        "\x1b[36m[INFO]\x1b[0m Updating Palworld Dedicated Server",
        "Redirecting stderr to '/home/steam/Steam/logs/stderr.txt'",
        "Logging directory: '/home/steam/Steam/logs'",
        "\x1b[32m[ OK ]\x1b[0m Connecting anonymously to Steam Public...OK",
        "Waiting for client config...OK",
        "Waiting for user info...OK",
        " Update state (0x3) reconfiguring, progress: 0.00 (0 / 0)",
        " Update state (0x61) downloading, progress: 12.40 (606632124 / 4892200000)",
        " Update state (0x61) downloading, progress: 31.95 (1563045830 / 4892200000)",
        STEAM.format(p="51.68", a=2528448160),
    ]))
    cfg = data / pal / "palworld/Pal/Saved/Config/LinuxServer"
    cfg.mkdir(parents=True)
    (cfg / "PalWorldSettings.ini").write_text(PAL_INI)

    errors: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1200, "height": 1000})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(base + "/")
        expect(pg.locator(f"#card-{pal} .badge")).to_contain_text("Installing 52%")

        # 1. deploy: slow create shows a Deploying tile at once, with a visible activity bar
        backend.delays["create"] = 6
        pg.goto(base + "/#/catalog")
        pg.locator("#catalog-enshrouded button.primary").click()
        pg.locator("dialog button[type=submit]").click()
        expect(pg.locator(".card.working .badge", has_text="Deploying")).to_be_visible(timeout=5000)
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / f"{args.prefix}-deploying.png"))
        expect(pg.locator("#servers .card", has_text="Enshrouded")).to_have_count(1)
        expect(pg.locator("#servers .card .badge", has_text="Deploying")).to_have_count(0, timeout=15000)

        # 2. dashboard with an Installing 52% tile next to a ready one and a stopped one
        pg.goto(base + "/")
        expect(pg.locator(f"#card-{pal} .badge")).to_contain_text("Installing 52%")
        expect(pg.locator("#card-viking-night .badge.running")).to_contain_text("Running")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(out / f"{args.prefix}-installing.png"))

        # 3. log viewer
        pg.locator(f"#card-{pal} button", has_text="Logs").click()
        expect(pg.locator("#log-text")).to_contain_text("progress: 51.68")
        assert "\x1b" not in pg.locator("#log-text").inner_text()
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-logs.png"))
        pg.locator("#log-copy").click()
        pg.keyboard.press("Escape")

        # 4. structured config form with changes highlighted
        pg.locator(f"#card-{pal} button", has_text="Stop").first.click()
        expect(pg.locator(f"#card-{pal} .badge")).to_contain_text("stopped", timeout=8000)
        pg.locator(f"#card-{pal} button", has_text="Config").click()
        expect(pg.locator(".cfg-row").first).to_be_visible()
        n_fields = pg.locator(".cfg-row").count()
        assert n_fields > 30, n_fields
        pg.locator("#cfg-bIsPvP").evaluate("e => e.click()")
        pg.locator("#cfg-ServerPlayerMaxNum").fill("16")
        pg.locator("#cfg-ServerName").fill('Family Palworld, the sequel (EU) "v2"')
        pg.locator("#cfg-AdminPassword").fill("a-new-admin-password")
        pg.locator("#cfg-ExpRate").fill("2.000000")
        pg.locator("#cfg-DayTimeSpeedRate").fill("0.5")
        expect(pg.locator(".cfg-row.changed")).to_have_count(6)
        expect(pg.locator("#cfg-review")).to_contain_text("These changes will be saved")
        assert pg.locator("#cfg-AdminPassword").get_attribute("type") == "password"
        pg.evaluate("() => { document.querySelector('dialog').scrollTop = 0; document.querySelector('.cfg-list').scrollTop = 0; }")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-config-form.png"))
        pg.locator("#config-save").click()
        expect(pg.locator(".okmsg")).to_contain_text("Saved")
        saved = (cfg / "PalWorldSettings.ini").read_text()
        assert "ExpRate=2.000000" in saved and "DayTimeSpeedRate=0.5," in saved
        assert "bIsPvP=True" in saved and "ServerPlayerMaxNum=16" in saved, saved
        assert 'ServerName="Family Palworld, the sequel (EU) \\"v2\\""' in saved, saved
        assert 'AdminPassword="a-new-admin-password"' in saved and "RCONPort=25575" in saved

        # raw view wraps (never one endless line)
        pg.locator(".seg button", has_text="Raw text").click()
        area = pg.locator("#config-text")
        expect(area).to_be_visible()
        assert area.evaluate("e => getComputedStyle(e).whiteSpace") == "pre-wrap"
        assert area.evaluate("e => e.scrollWidth <= e.clientWidth + 2")
        assert "********" in area.input_value() and "a-new-admin-password" not in area.input_value()
        pg.screenshot(path=str(out / f"{args.prefix}-config-raw.png"))

        # 5. 0.3.0: version and update badge, update badge on a tile, schedules panel, maintenance dialog
        pg.goto(base + "/")
        expect(pg.locator("#version-chip")).to_contain_text("Version 0.3.0")
        expect(pg.locator("#update-pill")).to_contain_text("Update available")
        expect(pg.locator("#card-viking-night .update-flag .badge")).to_have_text("Update available")
        expect(pg.locator("#card-viking-night .sched-line")).to_contain_text("Backups: daily at 03:00")
        expect(pg.locator("#card-viking-night .sched-line")).to_contain_text("Maintenance: Sunday at 04:00")
        expect(pg.locator(f"#card-{pal} .sched-line")).to_contain_text("Schedules: none set up")
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(out / f"{args.prefix}-header-update.png"))
        pg.locator("#card-viking-night").screenshot(path=str(out / f"{args.prefix}-tile-update.png"))

        pg.locator("#version-chip").click()
        expect(pg.locator("#about-update")).to_be_enabled()
        pg.screenshot(path=str(out / f"{args.prefix}-about.png"))
        pg.locator("#about-update").click()
        expect(pg.locator("dialog h3")).to_have_text("Update GameDock?")
        pg.keyboard.press("Escape")

        pg.locator("#card-viking-night button", has_text="Schedules").click()
        expect(pg.locator("#sched-panel")).to_be_visible()
        pg.locator("#sched-keep").fill("5")
        pg.locator("#sched-backup-save").click()
        expect(pg.locator("#sched-panel .okmsg")).to_contain_text("Backup schedule saved")
        assert service.get_server_schedules("viking-night")["backup"]["retention"] == 5
        pg.evaluate("() => { document.querySelector('dialog').scrollTop = 0; }")
        pg.screenshot(path=str(out / f"{args.prefix}-schedules.png"))
        assert pg.locator("#sched-m-empty").is_disabled()

        pg.locator("#sched-run-now").click()
        expect(pg.locator("dialog h3")).to_contain_text("Run maintenance for Viking Night now?")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / f"{args.prefix}-maintenance.png"))
        pg.locator("dialog label.check input").check()
        pg.locator("dialog button[type=submit]").click()
        expect(pg.locator("#sched-panel .okmsg")).to_contain_text("Maintenance finished", timeout=20000)
        expect(pg.locator("#sched-panel table.history")).to_contain_text("Maintenance")
        pg.keyboard.press("Escape")
        pg.goto(base + "/#/schedules")
        expect(pg.locator("#sched-viking-night")).to_contain_text("Worked")
        pg.screenshot(path=str(out / f"{args.prefix}-schedules-page.png"))
        b.close()
    shutil.rmtree(data, ignore_errors=True)
    server.should_exit = True
    if errors:
        raise SystemExit("Browser errors:\n" + "\n".join(errors))
    print("OK: screenshots written to", out)


if __name__ == "__main__":
    main()
