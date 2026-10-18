"""Game version tracking and game updates for SteamCMD based servers (a mixin of GameDockService).

What it adds to a server (only when its catalog entry has ``steam_app_id``):

* the installed Steam build id and date, read from the game's ``appmanifest_<id>.acf`` in the server's data folders;
* the game's own version text (``version_log_pattern`` applied to the log), remembered together with the build it
  belongs to, so it survives log rotation and GameDock restarts;
* the newest public build id (see ``gamedock/steam.py``; answers are cached and nothing here blocks on the network
  while the dashboard refreshes);
* "Update now": a safety backup, a restart (the image downloads the new game files when the container starts,
  ``game_update_method: restart``), then waiting until the installed build id changes, or saying plainly that it did not.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import backups as bk
from . import steam
from .errors import BackendError, GameDockError, ValidationError
from .models import CatalogEntry, Instance, Status
from .steam import InstalledGame

log = logging.getLogger("gamedock.game_updates")

VERSION_LOG_LINES = 3000          # how much of the log is searched for the game's version text
VERSION_RESCAN_SECONDS = 60.0     # a running game whose version line was not found is searched again after this long
MANIFEST_SEARCH_RETRY = 30.0      # a manifest that was not found is looked for again after this long


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} minute{'s' if m != 1 else ''}" if m < 90 else f"{m // 60} hours"


class GameUpdateMixin:
    """Methods are used by GameDockService, which provides: data_dir, backend, steam, store, _spec, _template,
    _with_current_hints, _operation, _instance, _is_running, _stop, _start, _safety_backup, _data_dir_of, _readiness."""

    # ------------------------------------------------------------------ setup
    def _init_game_updates(self, steam_info: steam.SteamInfo | None, wait: float, poll: float, settle: float) -> None:
        self.steam = steam_info or steam.SteamInfo(enabled=False)
        self.game_update_wait = float(wait)        # longest wait for the new files after a restart
        self.game_update_poll = float(poll)
        self.game_update_settle = float(settle)    # games with no "ready" signal: how long to wait before deciding
        self._sleep = time.sleep
        self._manifest_cache: dict[str, str] = {}      # server id -> where its Steam record was found
        self._manifest_missing: dict[str, float] = {}
        self._version_scan: dict[str, float] = {}
        self._versions_file = Path(self.data_dir) / "game-versions.json"
        self._versions: dict[str, dict] = self._load_versions()

    def _load_versions(self) -> dict[str, dict]:
        try:
            doc = json.loads(self._versions_file.read_text(encoding="utf-8"))
            return {k: v for k, v in doc.items() if isinstance(v, dict) and isinstance(v.get("version"), str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_versions(self) -> None:
        try:
            self._versions_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._versions_file.parent), prefix=".game-versions-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._versions, f)
            os.replace(tmp, self._versions_file)
        except OSError as e:
            log.info("could not save the remembered game versions: %s", e)

    # ------------------------------------------------------------------ installed build
    def _read_game_installed(self, inst: Instance, t: CatalogEntry, fresh: bool = False) -> tuple[InstalledGame | None, str]:
        """(installed game, problem sentence when it could not be read). Cheap: the file's location is remembered and
        a failed search is not repeated for a few seconds (unless `fresh`)."""
        if not t.steam_app_id:
            return None, ""
        cached = self._manifest_cache.get(inst.id)
        path: Path | None = Path(cached) if cached else None
        if path is None or not path.is_file():
            self._manifest_cache.pop(inst.id, None)
            path = None
            missing = self._manifest_missing.get(inst.id)
            if missing and not fresh and time.monotonic() - missing < MANIFEST_SEARCH_RETRY:
                return None, self._no_manifest_text(t)
            roots = [Path(v.host) for v in self._spec(inst).volumes]
            path = steam.find_manifest(t.steam_app_id, roots, t.steam_manifest_dir, self._data_dir_of(inst))
            if path is None:
                self._manifest_missing[inst.id] = time.monotonic()
                return None, self._no_manifest_text(t)
            self._manifest_missing.pop(inst.id, None)
        game = steam.read_installed(path)       # a small file: read it each time so a change is never missed
        if game is None:
            self._manifest_cache.pop(inst.id, None)
            return None, (f"The game's Steam record ({path.name}) has no build number yet. The first download may "
                          "still be running, or GameDock is not allowed to read the file.")
        self._manifest_cache[inst.id] = str(path)
        return game, ""

    @staticmethod
    def _no_manifest_text(t: CatalogEntry) -> str:
        return (f"GameDock could not find the game's Steam record (appmanifest_{t.steam_app_id}.acf) in this server's "
                "data folders. The first download may not be finished, or the game's own files are not in a folder "
                "that GameDock keeps (the game's install folder must be one of the server's volumes).")

    # ------------------------------------------------------------------ game's own version text
    def _game_version_text(self, inst: Instance, t: CatalogEntry, installed: InstalledGame | None,
                           running: bool, started: str) -> str:
        if not t.version_log_pattern:
            return ""
        build = installed.build if installed else ""
        stored = self._versions.get(inst.id)
        if stored and build and not stored.get("build") and stored.get("started") == started:
            stored["build"] = build           # found while the Steam record was not readable yet: tie it to the build now
            self._save_versions()
        if stored:
            same_build = bool(build) and stored.get("build") == build
            same_run = not build and (stored.get("started") == started or not running)
            if same_build or same_run:
                return stored["version"]
        if not running:
            return ""
        now = time.monotonic()
        last = self._version_scan.get(inst.id)
        if last is not None and now - last < VERSION_RESCAN_SECONDS:
            return ""
        self._version_scan[inst.id] = now
        try:
            text = self.backend.get_logs(f"gamedock-{inst.id}", VERSION_LOG_LINES, started or None)
        except BackendError:
            return ""
        version = steam.extract_version(text, t.version_log_pattern)
        if version:
            self._versions[inst.id] = {"version": version, "build": build, "started": started}
            self._save_versions()
        return version

    # ------------------------------------------------------------------ the report
    def _game_report(self, inst: Instance, t: CatalogEntry, running: bool = False, started: str = "") -> dict:
        """Everything known about a server's game version, without using the network."""
        app = t.steam_app_id
        branch = t.steam_branch or "public"
        installed, problem = self._read_game_installed(inst, t)
        latest = self.steam.cached(app, branch) if app else None
        status, summary = steam.compute_status(bool(app), installed, latest, problem)
        if app and installed is not None and latest is None and not self.steam.enabled:
            summary = "Checking for game updates is switched off (GAMEDOCK_UPDATE_CHECK=false)."
        version = self._game_version_text(inst, t, installed, running, started) if app else ""
        can_update = bool(app) and t.game_update_method == "restart"
        return {
            "server_id": inst.id, "supported": bool(app), "steam_app_id": app, "branch": branch if app else "",
            "status": status, "status_label": steam.STATUS_LABEL[status], "summary": summary,
            "update_available": True if status == steam.UPDATE_AVAILABLE else (False if status == steam.UP_TO_DATE else None),
            "game_version": version or None,
            "installed": {"build": installed.build if installed else None,
                          "updated_at": _iso(installed.updated_at) if installed else None,
                          "found": installed is not None, "problem": "" if installed else problem,
                          "complete": installed.complete if installed else None},
            "latest": {"build": latest.build if latest and latest.ok else None,
                       "updated_at": _iso(latest.updated_at) if latest and latest.ok else None,
                       "checked_at": _iso(latest.checked_at) if latest and latest.checked_at else None,
                       "error": latest.error if latest and not latest.ok else ""},
            "checks_enabled": self.steam.enabled,
            "update_method": t.game_update_method, "can_update": can_update, "running": running,
            "update_note": t.update_note,
        }

    def get_game_update_status(self, server_id: str) -> dict:
        """What is known about the game's version (cached; asks nothing)."""
        inst = self._instance(server_id)
        t = self._with_current_hints(inst, self._template(inst))
        state = self.backend.get_state(f"gamedock-{inst.id}")
        return self._game_report(inst, t, state.status == Status.RUNNING, state.started_at)

    def check_game_update(self, server_id: str, force: bool = True) -> dict:
        """Ask for the newest public build (the Check now button; read-only, nothing is downloaded or changed) and
        return the full report. Never raises for network trouble: the report says it could not check."""
        inst = self._instance(server_id)
        t = self._with_current_hints(inst, self._template(inst))
        if t.steam_app_id:
            self.steam.get(t.steam_app_id, t.steam_branch or "public", force=force)
        return self.get_game_update_status(server_id)

    def check_game_updates_due(self, limit: int = 1) -> int:
        """Background helper: look up the newest build for at most `limit` games whose answer is missing or old."""
        done = 0
        seen: set[tuple[str, str]] = set()
        for inst in self.store.all():
            if done >= limit:
                break
            try:
                t = self._with_current_hints(inst, self._template(inst))
                key = (t.steam_app_id, t.steam_branch or "public")
                if not key[0] or key in seen or not self.steam.due(*key):
                    continue
                seen.add(key)
                self.steam.get(*key)
                done += 1
            except GameDockError:
                continue
        return done

    # ------------------------------------------------------------------ updating
    def _wait_for_game_build(self, inst: Instance, t: CatalogEntry, before: str | None) -> tuple[str, InstalledGame | None]:
        """After a restart: wait until the installed build differs from `before`. Returns (outcome, installed):
        changed | unchanged (the game came up on the old build) | stopped (the container is not running) |
        timeout (still working when the wait ran out) | unverified (there was no build to compare with)."""
        cname = f"gamedock-{inst.id}"
        if not before:
            installed, _ = self._read_game_installed(inst, t, fresh=True)
            return "unverified", installed
        began = time.monotonic()
        while True:
            installed, _ = self._read_game_installed(inst, t, fresh=True)
            moved = installed is not None and installed.build != before
            if moved and installed.complete:
                return "changed", installed
            try:
                state = self.backend.get_state(cname)
            except BackendError:
                return "stopped", installed
            if state.status != Status.RUNNING:
                return "stopped", installed
            elapsed = time.monotonic() - began
            if t.ready_log_pattern or state.health:
                if not moved and self._readiness(inst, t, state.health, state.started_at).ready:
                    return "unchanged", installed     # the game came up; the update step happens before that
            elif not moved and elapsed >= self.game_update_settle:
                return "unchanged", installed
            if elapsed >= self.game_update_wait:
                return "timeout", installed
            self._sleep(self.game_update_poll)

    def _describe_outcome(self, outcome: str, before: str | None, after: InstalledGame | None, newest: str | None) -> tuple[str, bool]:
        """(plain sentence, is_problem) for the result of waiting after a restart."""
        a = after.build if after else None
        if outcome == "changed":
            return f"The game was updated from build {before} to build {a}.", False
        if outcome == "unchanged":
            hope = f" (the newest known is {newest})" if newest and newest != a else ""
            return (f"The game did not update on restart: it is still on build {a}{hope}. The server was restarted and "
                    "is running. Check the logs: the image may have its automatic update switched off "
                    "(UPDATE_ON_BOOT), or Steam may not be serving the new build to it yet."), True
        if outcome == "stopped":
            return ("The server stopped after the restart, so GameDock could not confirm the update. Check the logs."), True
        if outcome == "timeout":
            return (f"The game is still starting after waiting {_ago(self.game_update_wait)}. The download may still be "
                    "running; check the logs and look at the game version again later."), True
        return ("The server was restarted, but GameDock could not read the installed build before the restart, so it "
                "cannot confirm that the game changed."), False

    def update_game(self, server_id: str, confirm: bool = False) -> dict:
        """Update the game itself (not the image): safety backup, restart so the image downloads the new game files,
        then check that the installed build id changed. The server must be running (a stopped server updates when it is
        started). Needs confirm=true because players are disconnected."""
        with self._operation(server_id, "updating game"):
            inst = self._instance(server_id)
            t = self._with_current_hints(inst, self._template(inst))
            if not confirm:
                raise ValidationError("Updating needs explicit confirmation (confirm=true)")
            if not t.steam_app_id or t.game_update_method != "restart":
                raise ValidationError(f"GameDock cannot update the game files of {t.name} by itself. Look at the game's "
                                      "own instructions, or use 'Update the image' if there is one.")
            if not self._is_running(inst):
                raise ValidationError("The server is stopped. This game updates itself when it starts, so just start the "
                                      "server; there is nothing to restart.")
            installed, _ = self._read_game_installed(inst, t, fresh=True)
            before = installed.build if installed else None
            branch = t.steam_branch or "public"
            latest = self.steam.get(t.steam_app_id, branch, force=True)
            newest = latest.build if latest.ok else None
            status, _ = steam.compute_status(True, installed, latest)
            if status == steam.UP_TO_DATE:
                return {"server_id": inst.id, "updated": False, "outcome": "up_to_date", "restarted": False,
                        "before_build": before, "after_build": before, "newest_build": newest, "backup": None,
                        "game_version": self._game_report(inst, t, True)["game_version"], "problem": False,
                        "note": f"The game is already up to date (build {before}). Nothing was changed.",
                        "warnings": []}
            warnings: list[str] = []
            self._stop(inst)
            backup_name = None
            try:
                files, _, _ = bk.folder_usage(self._data_dir_of(inst))
                if files:
                    backup_name = self._safety_backup(inst, "pre-update").name
            except BaseException:
                try:
                    self._start(inst)
                except GameDockError:
                    pass
                raise
            try:
                self._start(inst)
            except GameDockError as e:
                return {"server_id": inst.id, "updated": False, "outcome": "not_started", "restarted": False,
                        "before_build": before, "after_build": before, "newest_build": newest, "backup": backup_name,
                        "game_version": None, "problem": True, "warnings": [],
                        "note": f"The backup was saved, but the server could not be started again: {e}"}
            self._version_scan.pop(inst.id, None)
            outcome, after = self._wait_for_game_build(inst, t, before)
            sentence, problem = self._describe_outcome(outcome, before, after, newest)
            report = self._game_report(inst, t, self._is_running(inst),
                                       self.backend.get_state(f"gamedock-{inst.id}").started_at)
            note = ((f"Backed up first ({backup_name}). " if backup_name else "") + sentence)
            return {"server_id": inst.id, "updated": outcome == "changed", "outcome": outcome, "restarted": True,
                    "before_build": before, "after_build": after.build if after else None, "newest_build": newest,
                    "backup": backup_name, "game_version": report["game_version"], "problem": problem,
                    "note": note, "warnings": warnings}
