"""Screenshots (and a browser smoke test) of the 0.6.2 file owner repair, on the in-memory fake backend with made-up demo
data: a Valheim server deployed from an older entry (no user_env) whose container wrote files as root. Check setup shows
the File ownership row with Fix ownership; deleting a server together with its data succeeds after the automatic repair.

    .venv/bin/python scripts/make_screenshots_ownership.py --out docs --prefix gamedock-062 [--chrome /usr/bin/google-chrome]
"""
from __future__ import annotations

import argparse
import os
import re
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

from gamedock import ops_service, service as service_mod  # noqa: E402
from gamedock.backends import FakeBackend  # noqa: E402
from gamedock.hostcheck import foreign_owners as real_foreign_owners  # noqa: E402
from gamedock.service import GameDockService  # noqa: E402
from gamedock.web.app import create_app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--prefix", default="gamedock-062")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--port", type=int, default=8129)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="gamedock-shots-"))
    catalog = work / "catalog"
    shutil.copytree(ROOT / "catalog", catalog)
    # The incident: the Valheim entry before 0.6.2 had no user_env, so the image ran as root (its default PUID=0).
    v = catalog / "valheim.yaml"
    v.write_text(re.sub(r"(?m)^user_env:.*\n", "", v.read_text()))
    backend = FakeBackend()
    service = GameDockService(backend, catalog, work / "data")
    vid = service.deploy("valheim", "Viking Night", env={"SERVER_PASS": "demo-pass-1"}, start=True).id
    from gamedock.models import PortMapping
    wid = service.deploy("valheim", "Weber Brothers", env={"SERVER_PASS": "demo-pass-2"},
                         ports=[PortMapping("udp", 2458, 2456), PortMapping("udp", 2459, 2457)]).id

    # Pretend root wrote these files (mode 600/700 owned by uid 0): the box user owns them, so file owners are faked for
    # the demo and the unreadable modes are real, which is what makes backup and delete need the repair.
    fixed: set[str] = set()

    def demo_owners(root, cap=20000):
        sid = Path(root).name
        if sid in (vid, wid) and sid not in fixed:
            return {0}, str(Path(root) / "config" / "worlds_local" / "Dedicated.db")
        return real_foreign_owners(root, cap)

    ops_service.foreign_owners = demo_owners
    service_mod.foreign_owners = demo_owners
    real_repair = backend.repair_ownership

    def repair(host_dir, uid, gid):
        note = real_repair(host_dir, uid, gid)
        fixed.add(Path(host_dir).name)
        return note

    backend.repair_ownership = repair
    for sid in (vid, wid):
        cfg = work / "data" / sid / "config" / "worlds_local"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "Dedicated.db").write_text("demo world")
        (cfg / "Dedicated.db").chmod(0o000)
        cfg.chmod(0o000)

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
            pg = b.new_page(viewport={"width": 1000, "height": 860})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            pg.goto(base + "/")
            card = pg.locator(f"#card-{vid}")
            card.locator('button[aria-label^="Check setup "]').click()
            row = pg.locator('#check-list li[data-check="owner"]')
            expect(row).to_contain_text("root (0)", timeout=8000)
            expect(row.locator("#fix-ownership")).to_be_visible()
            row.scroll_into_view_if_needed()
            shot(pg, "check-ownership")
            row.locator("#fix-ownership").click()
            expect(pg.locator("#fix-ownership-result")).to_contain_text("Nothing was deleted", timeout=8000)
            row.scroll_into_view_if_needed()
            shot(pg, "fix-ownership-done")
            expect(pg.locator('#check-list li[data-check="owner"] .badge')).to_have_text("OK", timeout=8000)
            pg.keyboard.press("Escape")

            card = pg.locator(f"#card-{wid}")
            card.locator('button[aria-label^="Delete "]').click()
            pg.locator("#delete-data").check()
            pg.locator("#confirm-data").check()
            pg.locator('dialog[open] button.danger').click()
            done = pg.locator("dialog[open]")
            expect(done).to_contain_text("Server deleted together with its data", timeout=8000)
            expect(done).to_contain_text("automatically")
            shot(pg, "delete-repaired", "dialog[open]")
            if "demo-pass" in pg.content():
                errors.append("a password was shown")
            b.close()
    finally:
        server.should_exit = True
        for root, dirs, files in os.walk(work, onerror=lambda e: None):
            for n in dirs:
                try:
                    os.chmod(os.path.join(root, n), 0o755)
                except OSError:
                    pass
        shutil.rmtree(work, ignore_errors=True)
    if errors:
        print("browser problems:", "; ".join(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()
