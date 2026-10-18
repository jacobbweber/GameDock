"""Browser check of the dialog layout (added in 0.5.1), on the in-memory fake backend.

Reproduces Jacob's bug: in the Config editor's settings form (a nested Enshrouded-style JSON file), changing a few
settings made the "These changes will be saved" box appear and the dialog broke (shrank, clipped Close/Save, two
scrollbars). It asserts, at several window sizes, that

  a. the dialog keeps exactly the same size before and after the changes,
  b. the footer buttons are fully inside the window and really visible (nothing covers them),
  c. at most one element inside the dialog scrolls vertically (and in the settings form it is the field list),

for the Config editor (form and raw view) and for every other dialog (Logs, Backups, Updates, Image update,
Schedules, Review, Deploy). Needs Playwright and Chrome (see requirements-dev.txt):

    .venv/bin/python scripts/check_dialog_layout.py [--shots docs --prefix gamedock-051 --suffix fixed] [--chrome /usr/bin/google-chrome]

The pytest wrapper tests/test_browser_dialog_layout.py runs the same check and is skipped without Playwright/Chrome.
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIZES = {"desktop-1000x880": (1000, 880), "small-700x500": (700, 500), "mobile-390x760": (390, 760)}

DEMO_YAML = """\
id: example-game
name: Example Game Dedicated Server
image: example/example-game-server:latest
description: A made-up game used only to open the review dialog.
ports:
  - { protocol: udp, host: 27100, container: 27015 }
volumes:
  - { host: ./saves, container: /home/steam/saves }
fields:
  - { name: SERVER_NAME, label: Server name, default: "My Example Server", required: true }
"""

# a lot of nested settings, like the real Enshrouded file (the form needs a long list to scroll)
FACTORS = ["playerHealthFactor", "playerManaFactor", "playerStaminaFactor", "playerBodyHeatFactor", "playerDivingTimeFactor",
           "enableDurability", "enableStarvingDebuff", "foodBuffDurationFactor", "fromHungerToStarvingFactor",
           "shroudTimeFactor", "tombstoneMode", "enableGliderTurbulences", "weatherFrequency", "fishingDifficulty",
           "miningDamageFactor", "plantGrowthSpeedFactor", "resourceDropStackAmountFactor", "factoryProductionSpeedFactor",
           "perkUpgradeRecyclingFactor", "perkCostFactor", "experienceCombatFactor", "experienceMiningFactor",
           "experienceExplorationQuestsFactor", "randomSpawnerAmount", "aggroPoolAmount", "enemyDamageFactor",
           "enemyHealthFactor", "enemyStaminaFactor", "enemyPerceptionRangeFactor", "bossDamageFactor", "bossHealthFactor",
           "threatBonus", "pacifyAllEnemies", "tamingStartleRepercussion", "dayTimeDuration", "nightTimeDuration"]


def enshrouded_json() -> str:
    gs: dict = {}
    for k in FACTORS:
        if k.startswith("enable") or k.startswith("pacify"):
            gs[k] = True
        elif k in ("tombstoneMode", "weatherFrequency", "fishingDifficulty", "randomSpawnerAmount", "tamingStartleRepercussion"):
            gs[k] = "Normal"
        elif k.endswith("Duration"):
            gs[k] = 1800000000000
        else:
            gs[k] = 1
    doc = {"name": "weber brothers", "saveDirectory": "./savegame", "logDirectory": "./logs", "ip": "0.0.0.0", "queryPort": 15637,
           "slotCount": 3, "tags": [], "voiceChatMode": "Proximity", "enableVoiceChat": False, "enableTextChat": False,
           "gameSettingsPreset": "Default", "gameSettings": gs,
           "userGroups": [{"name": "Default", "password": "", "canKickBan": False, "canAccessInventories": True,
                           "canEditWorld": True, "canEditBase": True, "canExtendBase": True, "reservedSlots": 0}],
           "bannedAccounts": []}
    return json.dumps(doc, indent="\t") + "\n"


# toggles / edits that make the review box appear (booleans, numbers and text)
TOGGLES = [("gameSettings.enableDurability", "toggle"), ("gameSettings.enableGliderTurbulences", "toggle"),
           ("gameSettings.foodBuffDurationFactor", "2"), ("gameSettings.randomSpawnerAmount", "Many")]

SCROLLERS_JS = """(dlg) => [dlg, ...dlg.querySelectorAll('*')].filter((e) => {
  const cs = getComputedStyle(e);
  return (cs.overflowY === 'auto' || cs.overflowY === 'scroll') && cs.display !== 'none' && e.clientHeight > 0
         && e.scrollHeight > e.clientHeight + 1;
}).map((e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (e.className ? '.' + String(e.className).split(' ').join('.') : ''))"""


class Checker:
    def __init__(self, page, size_name: str):
        self.pg = page
        self.size = size_name
        self.failures: list[str] = []

    def fail(self, what: str, msg: str) -> None:
        self.failures.append(f"[{self.size}] {what}: {msg}")

    def box(self):
        return self.pg.evaluate("""() => { const r = document.querySelector('dialog[open]').getBoundingClientRect();
                                           return {x: r.x, y: r.y, w: r.width, h: r.height}; }""")

    def scrollers(self) -> list[str]:
        return self.pg.evaluate(SCROLLERS_JS, self.pg.locator("dialog[open]").element_handle())

    def footer_ok(self, what: str) -> None:
        """Every footer button is fully inside the window and the dialog, and is the element on top at its centre."""
        res = self.pg.evaluate("""() => {
          const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
          const dlg = document.querySelector('dialog[open]'), d = dlg.getBoundingClientRect();
          const out = {buttons: 0, problems: []};
          dlg.querySelectorAll('.dialog-foot .dialog-actions button').forEach((b) => {
            if (b.closest('[hidden]') || getComputedStyle(b).display === 'none') return;
            out.buttons++;
            const r = b.getBoundingClientRect(), name = (b.textContent || '').trim() || b.id;
            if (r.left < -0.5 || r.top < -0.5 || r.right > vw + 0.5 || r.bottom > vh + 0.5) out.problems.push(name + ' outside the window');
            if (r.left < d.left - 0.5 || r.right > d.right + 0.5 || r.top < d.top - 0.5 || r.bottom > d.bottom + 0.5) out.problems.push(name + ' outside the dialog');
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            if (!hit || !(hit === b || b.contains(hit))) out.problems.push(name + ' covered by ' + (hit ? hit.tagName : 'nothing'));
            // not clipped by any scrolling/hiding ancestor
            for (let p = b.parentElement; p && p !== dlg.parentElement; p = p.parentElement) {
              const cs = getComputedStyle(p);
              if (cs.overflowY !== 'visible') { const pr = p.getBoundingClientRect(); if (r.bottom > pr.bottom + 0.5 || r.top < pr.top - 0.5) out.problems.push(name + ' clipped by ' + p.tagName); }
            }
          });
          const bottom = d.bottom, top = d.top;
          if (top < -0.5 || bottom > vh + 0.5 || d.left < -0.5 || d.right > vw + 0.5) out.problems.push('dialog itself is outside the window');
          return out; }""")
        if res["buttons"] == 0:
            self.fail(what, "no footer buttons found")
        for p in res["problems"]:
            self.fail(what, p)

    def single_scroller(self, what: str, expect: str | None = None, allow_review: bool = False) -> None:
        s = self.scrollers()
        if allow_review or not self.size.startswith("desktop"):     # the capped review box may need its own little scroll
            s = [x for x in s if "cfg-review" not in x]
        if len(s) > 1:
            self.fail(what, f"{len(s)} scrolling areas inside the dialog: {s}")
        if expect is not None and (len(s) != 1 or expect not in s[0]):
            self.fail(what, f"expected the only scroller to be '{expect}', found {s}")
        # nothing may stick out of the (overflow: hidden) dialog either, or the browser can still scroll it by hand
        # (focus, find-in-page, scrollIntoView): that is how the hidden toggle checkboxes once moved the whole dialog
        over = self.pg.evaluate("() => { const d = document.querySelector('dialog[open]'); return [d.scrollTop, d.scrollHeight - d.clientHeight]; }")
        if over[0] != 0 or over[1] > 1:
            self.fail(what, f"content sticks out of the dialog (scrollTop {over[0]}, {over[1]}px too tall)")


def run(shots: Path | None = None, prefix: str = "gamedock-051", suffix: str = "after", chrome: str | None = None,
        port: int = 8131, sizes: dict | None = None) -> list[str]:
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    from gamedock.backends import FakeBackend
    from gamedock.service import GameDockService
    from gamedock.web.app import create_app

    sizes = sizes or SIZES
    work = Path(tempfile.mkdtemp(prefix="gamedock-layout-"))
    catalog = work / "catalog"
    shutil.copytree(ROOT / "catalog", catalog)
    backend = FakeBackend()
    service = GameDockService(backend, catalog, work / "data")
    service.add_catalog_entry(DEMO_YAML)
    ens = service.deploy("enshrouded", "weber brothers", env={}).id
    mc = service.deploy("minecraft-java", "Creative Build", env={"EULA": "TRUE"}).id
    pal = service.deploy("palworld", "Family Palworld", env={"ADMIN_PASSWORD": "hunter2hunter2"}).id
    cfg = work / "data" / ens / "game" / "server"
    cfg.mkdir(parents=True)
    (cfg / "enshrouded_server.json").write_text(enshrouded_json())
    backend.set_logs("gamedock-" + ens, "\n".join(f"[INFO] line {i}: Enshrouded dedicated server is running" for i in range(300)))
    service.set_backup_schedule(mc, True, "daily", time_of_day="03:00", retention=7)
    for i in range(12):
        try:
            service.create_backup(mc)
        except Exception:
            break

    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)
    base = f"http://127.0.0.1:{port}"

    failures: list[str] = []
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            kw = {"executable_path": chrome} if chrome else {}
            b = p.chromium.launch(args=["--no-sandbox"], **kw)
            for size_name, (w, h) in sizes.items():
                pg = b.new_page(viewport={"width": w, "height": h})
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                c = Checker(pg, size_name)

                def tile(sid, label, pg=pg):
                    return pg.locator(f'#card-{sid} button[aria-label^="{label} "]').first
                desktop = size_name.startswith("desktop")
                tag = "" if desktop else f"-{size_name.split('-')[0]}"      # desktop shots keep the plain names

                # ---- the Config editor: settings form, then the raw view
                pg.goto(base + "/")
                tile(ens, "Config").click()
                expect(pg.locator(".cfg-row").first).to_be_visible()
                assert pg.locator(".cfg-row").count() > 35
                pg.wait_for_timeout(250)
                before = c.box()
                c.footer_ok("config form, nothing changed")
                for sel in (".seg", ".seg button", "#cfg-search", ".cfg-tools label.check"):       # header controls are not squashed
                    hh = pg.locator(sel).first.bounding_box()["height"]
                    if hh < 20:
                        c.fail("config form header", f"{sel} is only {hh:.0f}px high")
                c.single_scroller("config form, nothing changed", "cfg-list")
                for field, how in TOGGLES:
                    loc = pg.locator(f'[id="cfg-{field}"]')
                    loc.scroll_into_view_if_needed()
                    if how == "toggle":
                        loc.evaluate("e => e.click()")
                    else:
                        loc.fill(how)
                expect(pg.locator(".cfg-row.changed")).to_have_count(4)
                expect(pg.locator("#cfg-review")).to_be_visible()
                pg.wait_for_timeout(250)
                after = c.box()
                if before != after:
                    c.fail("config form", f"dialog size/position changed when the review box appeared: {before} -> {after}")
                c.footer_ok("config form, 4 changes")
                c.single_scroller("config form, 4 changes", "cfg-list")
                # the review box stays small and the header + review box stay on screen
                rv = pg.evaluate("""() => { const r = document.querySelector('#cfg-review').getBoundingClientRect(),
                    d = document.querySelector('dialog[open]').getBoundingClientRect(),
                    s = document.querySelector('#cfg-search').getBoundingClientRect();
                  return {rh: r.height, dh: d.height, rin: r.top >= d.top && r.bottom <= d.bottom, sin: s.top >= d.top && s.bottom <= d.bottom}; }""")
                if rv["rh"] > rv["dh"] * 0.27:
                    c.fail("config form", f"review box is {rv['rh']:.0f}px of a {rv['dh']:.0f}px dialog (over 25%)")
                if not (rv["rin"] and rv["sin"]):
                    c.fail("config form", f"search box or review box not fully inside the dialog: {rv}")
                # scroll the list to the end: still the same frame, header and buttons stay put
                pg.evaluate("() => { const l = document.querySelector('.cfg-list'); l.scrollTop = l.scrollHeight; }")
                pg.wait_for_timeout(150)
                if c.box() != before:
                    c.fail("config form", "dialog size changed while scrolling the list")
                c.footer_ok("config form, scrolled to the end")
                if shots:
                    shots.mkdir(parents=True, exist_ok=True)
                    pg.evaluate("() => { document.querySelector('.cfg-list').scrollTop = 0; }")
                    pg.wait_for_timeout(200)
                    pg.screenshot(path=str(shots / f"{prefix}-config{tag}-{suffix}.png"))
                # many changes: the review box scrolls on its own and the buttons stay
                for f in ["gameSettings.playerHealthFactor", "gameSettings.playerManaFactor", "gameSettings.playerStaminaFactor",
                          "gameSettings.playerBodyHeatFactor", "gameSettings.playerDivingTimeFactor", "gameSettings.shroudTimeFactor",
                          "gameSettings.miningDamageFactor", "gameSettings.perkCostFactor", "gameSettings.enemyDamageFactor",
                          "gameSettings.bossHealthFactor", "gameSettings.threatBonus", "gameSettings.aggroPoolAmount"]:
                    pg.locator(f'[id="cfg-{f}"]').fill("3")
                pg.wait_for_timeout(250)
                if c.box() != before:
                    c.fail("config form, 16 changes", f"dialog size changed: {before} -> {c.box()}")
                c.footer_ok("config form, 16 changes")
                c.single_scroller("config form, 16 changes", "cfg-list", allow_review=True)
                rh = pg.locator("#cfg-review").bounding_box()["height"]
                if rh > before["h"] * 0.27:
                    c.fail("config form, 16 changes", f"review box is {rh:.0f}px of a {before['h']:.0f}px dialog (over 25%)")
                if desktop and shots:
                    pg.screenshot(path=str(shots / f"{prefix}-config-many-{suffix}.png"))
                # undo everything: the box goes away, the frame stays
                pg.evaluate("() => document.querySelectorAll('.cfg-row .cfg-sub button').forEach((x) => x.click())")
                pg.wait_for_timeout(250)
                if c.box() != before:
                    c.fail("config form, after undo", f"dialog size changed: {before} -> {c.box()}")
                # raw view
                pg.locator(".seg button", has_text="Raw text").click()
                expect(pg.locator("#config-text")).to_be_visible()
                pg.wait_for_timeout(250)
                if c.box() != before:
                    c.fail("config raw", f"dialog size changed in the raw view: {before} -> {c.box()}")
                c.footer_ok("config raw")
                c.single_scroller("config raw", "config-text")
                if desktop and shots:
                    pg.screenshot(path=str(shots / f"{prefix}-config-raw-{suffix}.png"))
                pg.keyboard.press("Escape")

                # ---- the other dialogs
                def dialog_case(name, opener, ready, shot=None):
                    pg.goto(base + "/")
                    opener()
                    expect(pg.locator(ready)).to_be_visible(timeout=8000)
                    pg.wait_for_timeout(300)
                    c.footer_ok(name)
                    c.single_scroller(name)
                    if desktop and shots and shot:
                        pg.screenshot(path=str(shots / f"{prefix}-{shot}-{suffix}.png"))
                    pg.keyboard.press("Escape")

                dialog_case("logs", lambda: tile(ens, "Logs").click(), "#log-text", "logs")
                dialog_case("backups", lambda: tile(mc, "Backups").click(), "dialog[open] table.list", "backups")
                dialog_case("updates (Steam game)", lambda: tile(pal, "Updates").click(), "#game-status-badge", "updates")
                dialog_case("image update", lambda: tile(mc, "Updates").click(), "dialog[open] h3")
                dialog_case("schedules", lambda: pg.locator(f"#card-{mc} .sched-line").first.click(), "#sched-panel", "schedules")
                dialog_case("review catalog entry", lambda: (pg.goto(base + "/#/catalog"),
                            pg.locator("#catalog-example-game button", has_text="Review").click()), ".yaml-view", "review")
                dialog_case("deploy form", lambda: (pg.goto(base + "/#/catalog"),
                            pg.locator("#catalog-palworld button.primary").click()), "dialog[open] button[type=submit]")
                dialog_case("delete confirmation", lambda: tile(mc, "Delete").click(),
                            "dialog[open] h3")
                failures.extend(c.failures)
                pg.close()
            b.close()
    finally:
        server.should_exit = True
        shutil.rmtree(work, ignore_errors=True)
    if errors:
        failures.append("browser errors: " + "; ".join(errors))
    return failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None, help="folder for screenshots (desktop size)")
    ap.add_argument("--prefix", default="gamedock-051")
    ap.add_argument("--suffix", default="after")
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--port", type=int, default=8131)
    a = ap.parse_args()
    failures = run(Path(a.shots) if a.shots else None, a.prefix, a.suffix, a.chrome, a.port)
    if failures:
        print("\n".join(failures))
        raise SystemExit(f"{len(failures)} layout problem(s)")
    print("ok")


if __name__ == "__main__":
    main()
