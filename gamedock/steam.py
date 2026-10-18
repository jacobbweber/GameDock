"""Game versions for SteamCMD based servers: what is installed, what is newest, and how they compare.

Three small, independent pieces (no Docker, no GameDock objects, easy to test):

* ``parse_manifest`` / ``find_manifest`` / ``read_installed`` read Steam's own record of an installed game, the
  ``steamapps/appmanifest_<app id>.acf`` file (Valve "KeyValues" text) that SteamCMD keeps in the game's folder. It
  holds the installed *build id* and when it was last updated.
* ``SteamInfo`` asks a public mirror of Steam's data for the newest public build id of an app (no account or key). The
  answer is kept for an hour, also on disk, so a restart does not ask again. A failure is never an error: the answer
  just says "could not check". ``GAMEDOCK_UPDATE_CHECK=false`` switches every outbound question off.
* ``compute_status`` turns the two build ids into one of: up_to_date, update_available, could_not_check, unknown.

``extract_version`` reads the human game version (for example v1.0.5.102999) from log text with the catalog's
``version_log_pattern``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .readiness import strip_ansi

log = logging.getLogger("gamedock.steam")

DEFAULT_INFO_URL = "https://api.steamcmd.net/v1/info/{appid}"
LATEST_TTL = 3600.0          # how long an answer from the mirror is reused
FAILURE_TTL = 600.0          # a failed question is asked again after this long
MANUAL_GAP = 20.0            # seconds between two presses of "Check now" for one game
HTTP_TIMEOUT = 8.0
MAX_MANIFEST_BYTES = 1_000_000
SEARCH_MAX_DEPTH = 5         # folders below a volume that are searched for steamapps/appmanifest_<id>.acf
SEARCH_MAX_ENTRIES = 4000    # and a hard cap on how many directory entries one search may look at
APP_ID_RE = re.compile(r"^[0-9]{1,10}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

UP_TO_DATE, UPDATE_AVAILABLE, COULD_NOT_CHECK, UNKNOWN = "up_to_date", "update_available", "could_not_check", "unknown"
STATUS_LABEL = {UP_TO_DATE: "Up to date", UPDATE_AVAILABLE: "Update available", COULD_NOT_CHECK: "Could not check",
                UNKNOWN: "Unknown (no Steam id for this game)"}

HttpGet = Callable[[str, float], "tuple[int, str]"]


# ---------------------------------------------------------------------------------------------- manifest
_KV_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_manifest(text: str) -> dict:
    """Parse Valve KeyValues text (an ``appmanifest_*.acf`` file) into nested dicts. Values stay text. Tolerant:
    a truncated file gives whatever was complete before the break."""
    root: dict = {}
    stack = [root]
    pending: str | None = None
    for m in _KV_TOKEN.finditer(text or ""):
        word, brace = m.group(1), m.group(2)
        if brace == "{":
            if pending is not None:
                child: dict = {}
                stack[-1][pending] = child
                stack.append(child)
                pending = None
        elif brace == "}":
            pending = None
            if len(stack) > 1:
                stack.pop()
        else:
            word = word.replace('\\"', '"').replace("\\\\", "\\")
            if pending is None:
                pending = word
            else:
                stack[-1][pending] = word
                pending = None
    return root


@dataclass(frozen=True)
class InstalledGame:
    build: str                 # Steam build id as text ("25247047")
    updated_at: float | None   # unix seconds of the last update (the manifest's LastUpdated), if present
    state_flags: str = ""      # "4" = fully installed; other values mean an update or validation is running
    name: str = ""
    path: str = ""
    comparable: bool = True    # False = `build` is some version text, not a Steam build number (cannot be compared)

    @property
    def complete(self) -> bool:
        return self.state_flags in ("", "4")


def installed_from_manifest(data: dict, path: str = "") -> InstalledGame | None:
    state = data.get("AppState") if isinstance(data.get("AppState"), dict) else data
    build = str(state.get("buildid") or "").strip()
    if not build.isdigit():
        return None
    updated = state.get("LastUpdated")
    try:
        updated_at = float(updated) if updated not in (None, "") and float(updated) > 0 else None
    except (TypeError, ValueError):
        updated_at = None
    return InstalledGame(build, updated_at, str(state.get("StateFlags") or ""), str(state.get("name") or ""), path)


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def find_manifest(app_id: str, roots: list[Path], manifest_dir: str = "", base: Path | None = None,
                  max_depth: int = SEARCH_MAX_DEPTH, max_entries: int = SEARCH_MAX_ENTRIES) -> Path | None:
    """Locate ``appmanifest_<app_id>.acf``.

    With ``manifest_dir`` (relative to ``base``, the server's data folder) only that folder, or its ``steamapps``
    subfolder, is looked at. Otherwise the server's volumes (``roots``) are searched for a ``steamapps`` folder holding
    the file, at most ``max_depth`` levels deep and ``max_entries`` entries in total. Symlinked folders are not
    followed."""
    name = f"appmanifest_{app_id}.acf"
    if manifest_dir:
        if base is None:
            return None
        for sub in ("", "steamapps"):
            p = base / manifest_dir / sub / name
            if p.is_file() and _inside(p, base):
                return p
        return None
    budget = [max_entries]

    def walk(folder: Path, depth: int) -> Path | None:
        if depth > max_depth or budget[0] <= 0:
            return None
        direct = folder / "steamapps" / name
        try:
            if direct.is_file():
                return direct
            with os.scandir(folder) as it:
                subdirs = []
                for e in it:
                    budget[0] -= 1
                    if budget[0] <= 0:
                        break
                    try:
                        if e.is_dir(follow_symlinks=False):
                            subdirs.append(Path(e.path))
                    except OSError:
                        continue
        except OSError:
            return None
        for sub in sorted(subdirs):
            if sub.name == "steamapps":
                continue
            found = walk(sub, depth + 1)
            if found:
                return found
        return None

    for root in roots:
        found = walk(Path(root), 0)
        if found:
            return found
    return None


def read_installed(path: Path) -> InstalledGame | None:
    """The installed game described by one manifest file; None if it cannot be read or has no build id."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_MANIFEST_BYTES)
    except OSError:
        return None
    return installed_from_manifest(parse_manifest(text), str(path))


VERSION_FILE_RE = re.compile(r"^[\w.+\-() ]{1,64}$")


def read_version_file(path: Path) -> InstalledGame | None:
    """The installed version from a small text file such as ``game/current_version`` (first non-empty line).
    A number is taken as the Steam build id; any other text is shown as the version but cannot be compared with
    Steam's newest build. None if the file cannot be read, is empty, or holds anything but a short version text."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(4096)
    except OSError:
        return None
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not VERSION_FILE_RE.match(line):
        return None
    return InstalledGame(line, None, "", "", str(path), comparable=line.isdigit())


def render_manifest(app_id: str, build: str, updated_at: int | None = None, name: str = "Game Server",
                    state_flags: str = "4") -> str:
    """Manifest text in Steam's format (used by the pretend engine and by tests)."""
    lines = ['"AppState"', "{", f'\t"appid"\t\t"{app_id}"', '\t"Universe"\t\t"1"', f'\t"name"\t\t"{name}"',
             f'\t"StateFlags"\t\t"{state_flags}"', '\t"installdir"\t\t"server"']
    if updated_at is not None:
        lines.append(f'\t"LastUpdated"\t\t"{int(updated_at)}"')
    lines += [f'\t"buildid"\t\t"{build}"', f'\t"TargetBuildID"\t\t"{build}"',
              '\t"InstalledDepots"', "\t{", '\t\t"2394011"', "\t\t{", '\t\t\t"manifest"\t\t"123"', "\t\t}", "\t}", "}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- version text
def extract_version(text: str, pattern: str) -> str:
    """The last match of ``pattern`` (one capture group) in log text, or ''. ANSI colours are ignored."""
    if not pattern or not text:
        return ""
    try:
        rx = re.compile(pattern)
    except re.error:
        return ""
    found = ""
    for line in re.split(r"[\r\n]+", strip_ansi(text)):
        m = rx.search(line)
        if m and m.groups() and m.group(1):
            found = m.group(1)
    found = found.strip()
    return found[:64] if re.fullmatch(r"[\w.+\-() ]{1,64}", found[:64]) else ""


# ---------------------------------------------------------------------------------------------- latest build
class SteamInfoError(Exception):
    """A question to the mirror that failed for an expected reason; the text is plain language."""


def parse_steam_info(text: str, app_id: str, branch: str) -> tuple[str, float | None]:
    """(build id, time updated) from the mirror's JSON: data.<app id>.depots.branches.<branch>.{buildid,timeupdated}."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise SteamInfoError("The Steam data service sent an answer that is not JSON.") from None
    app = ((doc.get("data") or {}).get(app_id) if isinstance(doc, dict) and isinstance(doc.get("data"), dict) else None)
    if not isinstance(app, dict):
        raise SteamInfoError(f"The Steam data service has no information for app {app_id}.")
    branches = (app.get("depots") or {}).get("branches") if isinstance(app.get("depots"), dict) else None
    entry = branches.get(branch) if isinstance(branches, dict) else None
    if not isinstance(entry, dict):
        raise SteamInfoError(f"Steam lists no '{branch}' branch for app {app_id}.")
    build = str(entry.get("buildid") or "").strip()
    if not build.isdigit():
        raise SteamInfoError(f"Steam gave no build number for the '{branch}' branch of app {app_id}.")
    try:
        updated = float(entry["timeupdated"]) if entry.get("timeupdated") not in (None, "") else None
    except (TypeError, ValueError):
        updated = None
    return build, updated


@dataclass(frozen=True)
class LatestBuild:
    app_id: str
    branch: str
    ok: bool
    build: str = ""
    updated_at: float | None = None
    checked_at: float = 0.0
    error: str = ""


def default_http_get(url: str, timeout: float) -> tuple[int, str]:
    if not url.lower().startswith(("https://", "http://")):
        raise OSError("only http and https addresses are used")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"GameDock/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - scheme checked above; the URL is the owner's setting
            return r.status, r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(20_000).decode("utf-8", "replace")


class SteamInfo:
    """Newest public build ids, asked of a public mirror of Steam's data (``GAMEDOCK_STEAM_INFO_URL``)."""

    def __init__(self, cache_file: Path | None = None, url_template: str = DEFAULT_INFO_URL, enabled: bool = True,
                 http_get: HttpGet | None = None, clock: Callable[[], float] = time.time,
                 ttl: float = LATEST_TTL, failure_ttl: float = FAILURE_TTL, manual_gap: float = MANUAL_GAP) -> None:
        self.cache_file = Path(cache_file) if cache_file else None
        self.url_template = self.check_template(url_template or DEFAULT_INFO_URL)
        self.enabled = enabled
        self._http = http_get or default_http_get
        self._clock = clock
        self.ttl, self.failure_ttl, self.manual_gap = ttl, failure_ttl, manual_gap
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self._entries: dict[str, LatestBuild] = {}
        self._load()

    # ------------------------------------------------------------------ configuration
    @staticmethod
    def check_template(template: str) -> str:
        t = template.strip()
        if not t.lower().startswith(("https://", "http://")):
            raise ValueError("GAMEDOCK_STEAM_INFO_URL must start with https:// (or http://) and contain {appid}")
        if "{appid}" not in t:
            t = t.rstrip("/") + "/{appid}"
        return t

    def url_for(self, app_id: str) -> str:
        return self.url_template.replace("{appid}", urllib.parse.quote(app_id, safe=""))

    # ------------------------------------------------------------------ cache on disk
    @staticmethod
    def _key(app_id: str, branch: str) -> str:
        return f"{app_id}:{branch}"

    def _load(self) -> None:
        if not self.cache_file:
            return
        try:
            doc = json.loads(self.cache_file.read_text(encoding="utf-8"))
            for key, v in (doc.get("entries") or {}).items():
                app_id, _, branch = key.partition(":")
                if APP_ID_RE.match(app_id) and BRANCH_RE.match(branch):
                    self._entries[key] = LatestBuild(app_id, branch, bool(v.get("ok")), str(v.get("build") or ""),
                                                     v.get("updated_at"), float(v.get("checked_at") or 0),
                                                     str(v.get("error") or ""))
        except (OSError, ValueError, AttributeError, TypeError):
            self._entries = {}        # a missing or damaged cache file only means asking again

    def _save(self) -> None:
        if not self.cache_file:
            return
        doc = {"version": 1, "entries": {k: {"ok": v.ok, "build": v.build, "updated_at": v.updated_at,
                                             "checked_at": v.checked_at, "error": v.error}
                                         for k, v in sorted(self._entries.items())[:200]}}
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.cache_file.parent), prefix=".steam-latest-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, self.cache_file)
        except OSError as e:
            log.info("could not save the Steam build cache: %s", e)

    # ------------------------------------------------------------------ questions
    def cached(self, app_id: str, branch: str) -> LatestBuild | None:
        """What is known right now. Never uses the network. A successful answer older than the cache time is still
        returned (the status text says when it was checked); None = never asked."""
        with self._lock:
            return self._entries.get(self._key(app_id, branch))

    def due(self, app_id: str, branch: str) -> bool:
        if not self.enabled:
            return False
        e = self.cached(app_id, branch)
        if e is None:
            return True
        return self._clock() - e.checked_at >= (self.ttl if e.ok else self.failure_ttl)

    def get(self, app_id: str, branch: str = "public", force: bool = False) -> LatestBuild:
        """The newest build, from the cache while it is fresh. `force` (Check now) asks again, but not twice within a
        few seconds. Never raises: a problem is in the returned entry (ok=False, error=plain sentence)."""
        key = self._key(app_id, branch)
        cur = self.cached(app_id, branch)
        if not self.enabled:
            return cur or LatestBuild(app_id, branch, False, error="Update checks are switched off "
                                                                   "(GAMEDOCK_UPDATE_CHECK=false).")
        now = self._clock()
        if cur and not self.due(app_id, branch) and not force:
            return cur
        if cur and force and now - cur.checked_at < self.manual_gap:
            return cur
        with self._fetch_lock:
            cur2 = self.cached(app_id, branch)
            if cur2 is not None and cur2 is not cur and not self.due(app_id, branch):
                return cur2                  # another thread just asked
            result = self._ask(app_id, branch)
            with self._lock:
                self._entries[key] = result
                self._save()
            return result

    def _ask(self, app_id: str, branch: str) -> LatestBuild:
        now = self._clock()
        try:
            try:
                code, text = self._http(self.url_for(app_id), HTTP_TIMEOUT)
            except (OSError, TimeoutError) as e:
                raise SteamInfoError("Could not reach the Steam data service (no internet, or it is down). "
                                     "GameDock works fine without it and will try again later.") from e
            if code in (403, 429):
                raise SteamInfoError("The Steam data service is limiting requests from this address right now. "
                                     "GameDock will try again later.")
            if code != 200:
                raise SteamInfoError(f"The Steam data service answered with an unexpected status ({code}).")
            build, updated = parse_steam_info(text, app_id, branch)
            return LatestBuild(app_id, branch, True, build, updated, now)
        except SteamInfoError as e:
            log.info("Steam build check for app %s failed: %s", app_id, e)
            return LatestBuild(app_id, branch, False, checked_at=now, error=str(e))
        except Exception as e:  # noqa: BLE001 - a check must never break the page
            log.info("Steam build check for app %s failed unexpectedly: %s", app_id, e)
            return LatestBuild(app_id, branch, False, checked_at=now, error=f"Could not check: {e}")


# ---------------------------------------------------------------------------------------------- comparing
def compute_status(has_steam_id: bool, installed: InstalledGame | None, latest: LatestBuild | None,
                   installed_problem: str = "") -> tuple[str, str]:
    """(status, plain sentence). `installed` None = the manifest was not found/readable (give `installed_problem`)."""
    if not has_steam_id:
        return UNKNOWN, ("GameDock does not know this game's Steam id, so it cannot tell which game version is "
                         "installed or newest.")
    if installed is None:
        return COULD_NOT_CHECK, installed_problem or "The installed game version could not be read."
    if latest is None:
        return COULD_NOT_CHECK, "The newest game version has not been looked up yet."
    if not latest.ok:
        return COULD_NOT_CHECK, latest.error or "The newest game version could not be looked up."
    if not installed.comparable:
        return COULD_NOT_CHECK, (f"The installed game version is '{installed.build}', which is not a Steam build number, "
                                 f"so GameDock cannot compare it with the newest Steam build ({latest.build}).")
    a, b = int(installed.build), int(latest.build)
    if a == b or a > b:
        return UP_TO_DATE, f"The installed game (build {installed.build}) is the newest public version."
    return UPDATE_AVAILABLE, (f"A newer game version is available (installed build {installed.build}, newest "
                              f"{latest.build}).")
