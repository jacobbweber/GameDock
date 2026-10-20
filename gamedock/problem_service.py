"""Finding out WHY a server that is not ready is stuck (mixed into GameDockService). Read-only: this only looks at the
container's log, at log files the catalog entry names, and at how the container last ended. See gamedock/problems.py for
the patterns and the redaction."""
from __future__ import annotations

import os
import stat
import time
from dataclasses import replace
from pathlib import Path

from . import checks as ck
from . import problems as pb
from . import readiness as rd
from .errors import BackendError, GameDockError
from .models import CatalogEntry, Instance

PROBLEM_LOG_LINES = 600          # container log lines scanned for the card (the page polls every 3 s, answers are cached)
PROBLEM_TTL = 6.0                # seconds a "what is wrong?" answer is reused
LOG_FILE_TAIL_BYTES = 64 * 1024  # only the end of a log file is read
LOG_FILE_STALE_SLACK = 15.0      # a log file not written since 15 s before this start belongs to an older run
_PROBLEM_HINT_KEYS = ("fail_log_patterns", "log_files", "builtin_fail_patterns")


def read_tail(path: Path, base: Path, max_bytes: int = LOG_FILE_TAIL_BYTES) -> tuple[str, float] | None:
    """(text of the last max_bytes, modification time) of a regular file inside `base`, or None. Symbolic links and
    paths that leave `base` are refused, so a catalog entry can never make GameDock read another file."""
    try:
        resolved = path.resolve()
        if base.resolve() not in resolved.parents:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(resolved, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > max_bytes:
            os.lseek(fd, st.st_size - max_bytes, os.SEEK_SET)
        data = b""
        while len(data) < max_bytes + 1:
            chunk = os.read(fd, max_bytes + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    text = data.decode("utf-8", "replace")
    if st.st_size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]        # do not start in the middle of a line
    return text, st.st_mtime


class ProblemsMixin:
    """Uses from GameDockService: backend, _catalog_current (via _current_entries), _redact, _data_dir_of, _now, _template."""

    def _init_problems(self) -> None:
        self._problem_cache: dict[str, tuple[float, tuple, dict]] = {}
        self._readiness_text: dict[str, tuple[float, str, str]] = {}   # server id -> (when, start time, log text) read for readiness

    # ------------------------------------------------------------------ what comes from the current catalog
    def _with_current_problem_hints(self, inst: Instance, t: CatalogEntry) -> CatalogEntry:
        """fail_log_patterns, log_files and builtin_fail_patterns only describe how to READ the game's logs, so they always
        come from the current catalog entry when there is one (a server keeps a snapshot of everything else)."""
        try:
            cur = self._current_entries().get(inst.game_id)
        except Exception:  # noqa: BLE001 - a broken catalog must never break the server list
            return t
        if cur is None:
            return t
        return replace(t, **{k: getattr(cur, k) for k in _PROBLEM_HINT_KEYS})

    def _with_current_field_rules(self, inst: Instance, t: CatalogEntry) -> CatalogEntry:
        """The format rule of a setting (pattern, pattern_hint) only checks what is typed in, so a server deployed before
        the catalog entry got the rule is checked with the current rule."""
        try:
            cur = self._current_entries().get(inst.game_id)
        except Exception:  # noqa: BLE001
            return t
        if cur is None:
            return t
        rules = {f.name: f for f in cur.fields if f.pattern}
        values = {f.name: f.rules for f in cur.fields if f.rules is not None}
        if not rules and not values:
            return t
        out = []
        for f in t.fields:
            if f.name in rules and not f.pattern:
                f = replace(f, pattern=rules[f.name].pattern, pattern_hint=rules[f.name].pattern_hint)
            if f.name in values:      # value rules (choices, ranges) always come from the current catalog entry
                f = replace(f, rules=values[f.name])
            out.append(f)
        return replace(t, fields=tuple(out))

    # ------------------------------------------------------------------ reading the sources
    def _secret_values(self, inst: Instance, t: CatalogEntry) -> tuple[str, ...]:
        return tuple(v for v in (inst.env.get(f.name, "") for f in t.fields if f.secret) if len(v) >= 3)

    def _log_file_sources(self, inst: Instance, t: CatalogEntry, started) -> list[tuple[str, str]]:
        """[(relative path, text)] for the declared log files that exist and were written during this run."""
        out: list[tuple[str, str]] = []
        base = self._data_dir_of(inst)
        for rel in t.log_files:
            got = read_tail(base / rel, base)
            if got is None:
                continue
            text, mtime = got
            if started is not None and mtime < started.timestamp() - LOG_FILE_STALE_SLACK:
                continue
            out.append((rel, text))
        return out

    def _container_log(self, inst: Instance, started_text: str, lines: int) -> str:
        try:
            return self.backend.get_logs(f"gamedock-{inst.id}", lines, started_text or None)
        except BackendError:
            return ""

    def _scan_problem(self, inst: Instance, t: CatalogEntry, container_text: str, started) -> pb.Problem | None:
        """Look for a known problem in already-read container log text and in the declared log files."""
        t = self._with_current_problem_hints(inst, t)
        sources: list[tuple[str, str]] = []
        if container_text:
            sources.append(("the container log", container_text))
        sources += [(f"the game's log file {rel}", text) for rel, text in self._log_file_sources(inst, t, started)]
        p = pb.scan(sources, t.fail_log_patterns, t.builtin_fail_patterns, self._secret_values(inst, t))
        if p is not None and p.setting:
            p = self._locate_setting(inst, p)
        return p

    def _locate_setting(self, inst: Instance, p: pb.Problem) -> pb.Problem:
        """Which config file and setting path the log's setting name is (TombstoneMode -> gameSettings.tombstoneMode in
        game/server/enshrouded_server.json), from the current setting rules; also the allowed values when the log did
        not list them."""
        from . import settings_schema as ss
        try:
            files = self.config_schema(inst)
        except Exception:  # noqa: BLE001
            return p
        for c in files:
            spec = ss.find_by_name(c.schema, p.setting) if c.schema else None
            if spec is None:
                continue
            allowed = p.allowed or tuple(spec.allowed_values)
            advice = p.advice
            if allowed and not p.allowed:
                advice = f"Allowed values: {', '.join(allowed)}. " + advice
            return replace(p, config_path=c.path, setting_path=spec.path, allowed=allowed,
                           advice=advice.replace("Open Config,", f"Open Config ({c.path}, setting {spec.path}),", 1))
        if len(files) == 1 and files[0].format in ("json", "ini"):
            return replace(p, config_path=files[0].path)
        return p

    # ------------------------------------------------------------------ for the server list
    def _card_problem(self, inst: Instance, t: CatalogEntry, status, health: str, started_text: str,
                      exit_code, oom_killed: bool, ready: bool) -> dict:
        """The problem to show on the server's card ({} when there is none). Cheap: answers are cached for a few seconds."""
        from .models import Status
        if status == Status.RUNNING:
            if ready:
                return {}
            key = (health, started_text)
            now = time.monotonic()
            cached = self._problem_cache.get(inst.id)
            if cached and cached[1] == key and now - cached[0] < PROBLEM_TTL:
                return cached[2]
            started = ck.parse_started(started_text)
            shared = self._readiness_text.get(inst.id)
            if shared and shared[1] == started_text and now - shared[0] < PROBLEM_TTL:
                text = shared[2]
            else:
                text = self._container_log(inst, started_text, PROBLEM_LOG_LINES)
            try:
                p = self._scan_problem(inst, t, text, started)
            except Exception:  # noqa: BLE001 - this is only a hint; never break the server list
                p = None
            d = p.to_dict() if p else {}
            self._problem_cache[inst.id] = (now, key, d)
            return d
        self._problem_cache.pop(inst.id, None)
        self._readiness_text.pop(inst.id, None)
        if status in (Status.STOPPED, Status.ERROR) and oom_killed:
            p = pb.docker_problem(exit_code, True)
            return p.to_dict() if p else {}
        return {}

    # ------------------------------------------------------------------ for Check setup and diagnostics
    def find_problem(self, inst: Instance, t: CatalogEntry, running: bool, container_text: str, started_text: str,
                     details: dict | None) -> pb.Problem | None:
        """The most likely reason this server is stuck or has stopped, or None. `container_text` is log text that was
        already read (redacted or not: it is redacted again here)."""
        started = ck.parse_started(started_text)
        if not running:
            if details:
                p = pb.docker_problem(details.get("exit_code"), bool(details.get("oom_killed")))
                if p:
                    return p
            if not (details and details.get("exit_code")):
                return None      # stopped normally: old log lines say nothing about a problem now
        try:
            return self._scan_problem(inst, t, container_text, started)
        except GameDockError:
            return None

    def log_file_tails(self, inst: Instance, t: CatalogEntry, lines: int = 40) -> list[dict]:
        """Last `lines` lines of each declared log file, secrets hidden (for the diagnostics bundle)."""
        t = self._with_current_problem_hints(inst, t)
        base = self._data_dir_of(inst)
        secrets = self._secret_values(inst, t)
        out = []
        for rel in t.log_files:
            got = read_tail(base / rel, base, 32 * 1024)
            if got is None:
                out.append({"path": rel, "found": False, "text": ""})
                continue
            text = ck.redact_text(rd.clean_log_text(got[0]).rstrip("\n"), secrets)
            out.append({"path": rel, "found": True, "text": "\n".join(text.split("\n")[-lines:])})
        return out
