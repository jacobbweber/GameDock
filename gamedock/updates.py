"""GameDock's own version, "is there a newer GameDock?" and the one-click self-update.

* Version and commit come from the package and from `git` (when the install folder is a git checkout).
* The check for a newer release is a few read-only HTTPS requests to the public GitHub API. It runs at most every
  6 hours in the background (and when someone presses "Check now"). Offline or rate limited is not an error: the
  page just says it could not check. `GAMEDOCK_UPDATE_CHECK=false` switches the check off completely.
* The self-update (`apply`) only runs in a clean git checkout: it refuses while any action is in progress, fetches,
  makes sure the change is a plain fast-forward, runs `git pull --ff-only`, and then restarts GameDock only.
  Game containers are separate and keep running. It is web-only on purpose: it is not exposed to AI agents.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .errors import BackendError, ConflictError

log = logging.getLogger("gamedock.updates")

DEFAULT_REPO = "jacobbweber/GameDock"
CHECK_INTERVAL = 6 * 3600       # how often the background check may contact GitHub
RETRY_INTERVAL = 30 * 60        # after a failed check
MANUAL_GAP = 20                 # seconds between two presses of "Check now"
HTTP_TIMEOUT = 8.0
GIT_TIMEOUT = 20
GIT_PULL_TIMEOUT = 120
RESTART_EXIT_CODE = 75          # "temporary failure": a failure exit makes Restart=on-failure and Restart=always restart us
START_TIME = time.time()
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}$")
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
DEPENDENCY_FILES = ("pyproject.toml", "requirements.txt")

HttpGet = Callable[[str, dict, float], tuple[int, str]]


def parse_repo(value: str) -> str:
    """'owner/name' from 'owner/name', 'https://github.com/owner/name(.git)' or 'https://api.github.com/repos/owner/name'."""
    v = (value or "").strip()
    v = re.sub(r"^https://(www\.)?github\.com/", "", v)
    v = re.sub(r"^https://api\.github\.com/repos/", "", v)
    v = re.sub(r"\.git$", "", v).strip("/")
    if not REPO_RE.match(v):
        raise ValueError("GAMEDOCK_UPDATE_REPO must look like owner/name or https://github.com/owner/name "
                         f"(got '{value}')")
    return v


def default_http_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    if not url.startswith("https://api.github.com/"):
        raise OSError("refusing to contact anything but api.github.com")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https and host checked above
            return r.status, r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(20_000).decode("utf-8", "replace")


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def default_runner(args: list[str], cwd: Path, timeout: float):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_ASKPASS": os.environ.get("GIT_ASKPASS", "")}
    if not env["GIT_ASKPASS"]:
        env.pop("GIT_ASKPASS")
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)


@dataclass
class _Latest:
    checked_at: float = 0.0          # when we last tried
    ok_at: float = 0.0               # when a check last worked
    error: str = ""
    commit: str = ""
    version: str = ""
    available: bool | None = None    # None = unknown
    ahead_by: int | None = None
    relation: str = ""               # identical | ahead | behind | diverged | unknown_local | newer_version | ""
    message: str = ""                # first line of the newest commit message
    published: str = ""


def systemd_restart_info() -> tuple[str, str] | None:
    """(scope, unit) when this process runs under systemd, else None. scope is 'user' or 'system'."""
    if not os.environ.get("INVOCATION_ID"):
        return None
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"/([A-Za-z0-9_.@:-]+\.service)\s*$", text.strip().splitlines()[-1]) if text.strip() else None
    if not m:
        return None
    scope = "user" if "/user@" in text else "system"
    return scope, m.group(1)


def default_restart() -> None:
    """Restart GameDock only. Under systemd: ask systemd (`systemctl restart`, works with any Restart= setting), and if
    that does not work exit with a failure code so Restart=on-failure/always starts us again. Outside systemd: start the
    same command in place."""
    info = systemd_restart_info()
    if info and shutil.which("systemctl"):
        scope, unit = info
        cmd = ["systemctl", *(["--user"] if scope == "user" else []), "restart", "--no-block", unit]
        try:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                time.sleep(25)          # systemd stops us within moments; if it has not, fall through
        except (OSError, subprocess.SubprocessError):
            pass
        log.warning("systemctl did not restart GameDock; exiting so the service manager starts it again")
        os._exit(RESTART_EXIT_CODE)
    if info:
        os._exit(RESTART_EXIT_CODE)
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, "-m", "gamedock"])
    log.warning("Restarting GameDock in place: %s", " ".join(argv))
    os.execv(argv[0] if os.path.isabs(argv[0]) else (shutil.which(argv[0]) or sys.executable), argv)


class UpdateChecker:
    def __init__(self, install_dir: Path | None = None, repo: str = DEFAULT_REPO, enabled: bool = True,
                 self_update: bool = True, http_get: HttpGet | None = None, runner=None,
                 restart: Callable[[], None] | None = None, clock: Callable[[], float] = time.time,
                 version: str = __version__, service_name: str = "gamedock") -> None:
        self.install_dir = Path(install_dir).resolve() if install_dir else Path(__file__).resolve().parent.parent
        self.repo = parse_repo(repo)
        self.enabled = enabled
        self.self_update_enabled = self_update
        self.version = version
        self.service_name = service_name
        self._http = http_get or default_http_get
        self._runner = runner or default_runner
        self._restart = restart or default_restart
        self._clock = clock
        self._lock = threading.RLock()
        self._latest = _Latest()
        self._apply_lock = threading.Lock()
        self._git_cache: tuple[float, dict] | None = None
        self.updating = False

    # ------------------------------------------------------------------ git helpers
    def _git(self, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, str, str]:
        try:
            p = self._runner(["git", *args], self.install_dir, timeout)
        except FileNotFoundError:
            return 127, "", "git is not installed"
        except subprocess.TimeoutExpired:
            return 124, "", f"git {args[0]} did not finish in time"
        except OSError as e:
            return 126, "", str(e)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

    def _local(self) -> dict:
        """Facts about the install folder (cached for a few seconds)."""
        now = self._clock()
        if self._git_cache and now - self._git_cache[0] < 5:
            return self._git_cache[1]
        info: dict = {"is_git": False, "commit": "", "branch": "", "dirty": False, "why": ""}
        if not (self.install_dir / ".git").exists():
            info["why"] = "GameDock is not running from a git checkout (there is no .git folder in its install folder)."
        else:
            rc, top, err = self._git("rev-parse", "--show-toplevel")
            if rc == 127:
                info["why"] = "The git command is not installed on this machine."
            elif rc != 0 or Path(top).resolve() != self.install_dir:
                info["why"] = "GameDock's install folder is not the top of a git checkout."
            else:
                info["is_git"] = True
                rc, sha, _ = self._git("rev-parse", "HEAD")
                info["commit"] = sha if rc == 0 and SHA_RE.match(sha) else ""
                rc, br, _ = self._git("rev-parse", "--abbrev-ref", "HEAD")
                info["branch"] = br if rc == 0 and BRANCH_RE.match(br) and br != "HEAD" else ""
                rc, out, _ = self._git("status", "--porcelain", "--untracked-files=no")
                info["dirty"] = rc == 0 and bool(out)
                info["dirty_files"] = out.splitlines()[:5] if info["dirty"] else []
        self._git_cache = (now, info)
        return info

    # ------------------------------------------------------------------ status
    def manual_commands(self, deps_changed: bool = False) -> list[str]:
        cmds = [f"cd {self.install_dir}", "git pull --ff-only"]
        if deps_changed:
            cmds.append(".venv/bin/pip install -e .")
        cmds.append(f"systemctl --user restart {self.service_name}")
        return cmds

    def can_self_update(self) -> tuple[bool, str]:
        """(possible, plain-language reason when not). Does not look at running actions (see apply)."""
        loc = self._local()
        if not self.enabled:
            return False, "Update checks are switched off (GAMEDOCK_UPDATE_CHECK=false)."
        if not self.self_update_enabled:
            return False, "The Update button is switched off (GAMEDOCK_SELF_UPDATE=false)."
        if not loc["is_git"]:
            return False, loc["why"]
        if not loc["branch"]:
            return False, "The install folder is not on a branch (detached checkout), so it cannot pull updates."
        if loc["dirty"]:
            files = ", ".join(loc.get("dirty_files", [])[:3])
            return False, ("The install folder has local changes to GameDock's own files"
                           + (f" ({files})" if files else "")
                           + ", so GameDock will not pull over them. Save or undo them first (git status, git stash).")
        return True, ""

    def status(self) -> dict:
        """Everything the header needs. Never contacts the network."""
        loc = self._local()
        ok, why = self.can_self_update()
        L = self._latest
        commit = loc["commit"]
        d = {
            "version": self.version,
            "commit": commit, "commit_short": commit[:7],
            "branch": loc["branch"], "is_git": loc["is_git"], "dirty": loc["dirty"],
            "started_at": _iso(START_TIME),
            "install_dir": str(self.install_dir),
            "repo": self.repo, "repo_url": f"https://github.com/{self.repo}",
            "check_enabled": self.enabled,
            "checked_at": _iso(L.checked_at) if L.checked_at else None,
            "check_error": L.error,
            "update_available": L.available if self.enabled else None,
            "latest": {"commit": L.commit, "commit_short": L.commit[:7], "version": L.version,
                       "ahead_by": L.ahead_by, "relation": L.relation, "message": L.message,
                       "published": L.published} if L.ok_at else None,
            "self_update": {"possible": ok, "reason": why, "manual_commands": self.manual_commands()},
            "updating": self.updating,
        }
        return d

    # ------------------------------------------------------------------ checking
    def due(self) -> bool:
        if not self.enabled:
            return False
        L = self._latest
        wait = CHECK_INTERVAL if not L.error else RETRY_INTERVAL
        return not L.checked_at or self._clock() - L.checked_at >= wait

    def check(self, force: bool = False) -> dict:
        """Ask GitHub (read-only). `force` is the Check now button: allowed at any time except twice within a few seconds."""
        if not self.enabled:
            return self.status()
        with self._lock:
            now = self._clock()
            if force:
                if self._latest.checked_at and now - self._latest.checked_at < MANUAL_GAP:
                    return self.status()      # pressed twice in a row: the answer is the same
            elif not self.due():
                return self.status()
            self._latest.checked_at = now
            try:
                self._do_check()
                self._latest.error = ""
                self._latest.ok_at = now
            except _CheckProblem as e:
                self._latest.error = str(e)
                log.info("GameDock update check: %s", e)
            except Exception as e:  # noqa: BLE001 - never let a check break the page
                self._latest.error = f"Could not check for updates: {e}"
                log.info("GameDock update check failed: %s", e)
        return self.status()

    def _api(self, path: str, raw: bool = False) -> tuple[int, str]:
        headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
                   "User-Agent": f"GameDock/{self.version}", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            return self._http(f"https://api.github.com/repos/{self.repo}{path}", headers, HTTP_TIMEOUT)
        except (OSError, TimeoutError) as e:
            raise _CheckProblem("Could not reach GitHub (no internet, or GitHub is down). GameDock works fine "
                                "without it; it will try again later.") from e

    def _json(self, path: str) -> tuple[int, dict]:
        code, text = self._api(path)
        if code == 403 or code == 429:
            raise _CheckProblem("GitHub is limiting requests from this address right now. It will try again later.")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        return code, data if isinstance(data, dict) else {}

    def _do_check(self) -> None:
        loc = self._local()
        L = self._latest
        branch = loc["branch"] or "main"
        code, head = self._json(f"/commits/{branch}")
        if code == 404 and branch != "main":
            code, head = self._json("/commits/main")
        if code == 404:
            raise _CheckProblem(f"GitHub does not know the repository {self.repo} (or it is private).")
        sha = str(head.get("sha") or "")
        if code != 200 or not SHA_RE.match(sha):
            raise _CheckProblem(f"GitHub answered with an unexpected reply (status {code}).")
        commit = head.get("commit") or {}
        L.commit = sha
        L.message = str(commit.get("message") or "").split("\n", 1)[0][:200]
        L.published = str((commit.get("committer") or {}).get("date") or "")
        L.version = self._remote_version(sha)
        mine = loc["commit"]
        L.ahead_by = None
        if mine:
            if mine == sha:
                L.available, L.relation = False, "identical"
            else:
                code, cmp_ = self._json(f"/compare/{mine}...{sha}")
                if code == 200:
                    rel = str(cmp_.get("status") or "")
                    L.relation = rel
                    L.available = rel in ("ahead", "diverged")
                    L.ahead_by = cmp_.get("ahead_by") if rel in ("ahead", "diverged") else None
                else:  # GitHub has never seen our commit: it exists only on this machine
                    L.available, L.relation = False, "unknown_local"
        else:  # no git: compare version numbers
            if L.version:
                L.available = version_tuple(L.version) > version_tuple(self.version)
                L.relation = "newer_version" if L.available else "identical"
            else:
                L.available, L.relation = None, ""

    def _remote_version(self, sha: str) -> str:
        try:
            code, text = self._api(f"/contents/gamedock/__init__.py?ref={sha}", raw=True)
        except _CheckProblem:
            return ""
        m = VERSION_RE.search(text) if code == 200 else None
        return m.group(1) if m else ""

    # ------------------------------------------------------------------ updating
    def apply(self, freeze: Callable[[str], None], unfreeze: Callable[[], None]) -> dict:
        """Pull the newest GameDock and restart it. `freeze(reason)` must raise ConflictError if any action is running
        and keep new ones out; `unfreeze()` undoes it when the update does not happen."""
        if not self._apply_lock.acquire(blocking=False):
            raise ConflictError("An update is already in progress.")
        try:
            ok, why = self.can_self_update()
            if not ok:
                raise ConflictError(self._refusal(why))
            self._git_cache = None
            ok, why = self.can_self_update()  # look again, fresh
            if not ok:
                raise ConflictError(self._refusal(why))
            branch = self._local()["branch"]
            freeze("GameDock is updating")
            frozen = True
            try:
                rc, _, err = self._git("fetch", "origin", branch, timeout=GIT_PULL_TIMEOUT)
                if rc != 0:
                    raise BackendError("Could not download the update (git fetch failed): "
                                       f"{_short(err)} Nothing was changed.")
                rc, ahead, _ = self._git("rev-list", "--count", "HEAD..FETCH_HEAD")
                if rc == 0 and ahead == "0":
                    unfreeze()
                    frozen = False
                    return {"updated": False, "restarting": False, "version": self.version,
                            "message": "GameDock is already up to date. Nothing was changed."}
                rc, _, _ = self._git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")
                if rc != 0:
                    raise ConflictError(
                        "This copy of GameDock has commits that are not on GitHub (or the history was rewritten), so it "
                        "cannot be updated by a simple pull. Nothing was changed. Manual steps: "
                        + "; ".join(self.manual_commands()))
                rc, names, _ = self._git("diff", "--name-only", "HEAD", "FETCH_HEAD")
                changed_deps = [n for n in names.splitlines() if n in DEPENDENCY_FILES]
                if changed_deps:
                    raise ConflictError(
                        "This update changes GameDock's required packages ("
                        + ", ".join(changed_deps) + "), which needs a manual step the page cannot do safely. "
                        "Nothing was changed. Run these commands on the machine: "
                        + "; ".join(self.manual_commands(deps_changed=True)))
                before = self._local()["commit"][:7]
                rc, _, err = self._git("pull", "--ff-only", "origin", branch, timeout=GIT_PULL_TIMEOUT)
                if rc != 0:
                    raise BackendError(f"git pull --ff-only failed: {_short(err)} Nothing was restarted.")
                new_version = self._read_installed_version()
                self._git_cache = None
                after = self._local()["commit"][:7]
                frozen = False  # stays frozen: the process is about to restart
                self.updating = True
                threading.Timer(1.5, self._restart).start()
                return {"updated": True, "restarting": True, "from_version": self.version,
                        "version": new_version or self.version, "from_commit": before, "commit": after,
                        "message": f"Updated GameDock from {before} to {after}. GameDock is restarting now; your game "
                                   "servers keep running. This page will reload by itself."}
            finally:
                if frozen:
                    unfreeze()
        finally:
            self._apply_lock.release()

    def _refusal(self, why: str) -> str:
        return f"{why} To update by hand, run: " + "; ".join(self.manual_commands())

    def _read_installed_version(self) -> str:
        try:
            m = VERSION_RE.search((self.install_dir / "gamedock" / "__init__.py").read_text(encoding="utf-8"))
            return m.group(1) if m else ""
        except OSError:
            return ""


class _CheckProblem(Exception):
    """A check that failed for an expected reason; the text is shown to the user."""


def _short(text: str) -> str:
    t = " ".join((text or "").split())
    return (t[:300] + "...") if len(t) > 300 else (t or "no details.")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
