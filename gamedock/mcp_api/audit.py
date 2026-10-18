"""Audit log of MCP calls: one JSON object per line, never any secret values.

Each record: time (UTC ISO), tool, args (secrets masked), status, and a short detail for errors.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

MASK = "***"
SECRET_NAME_RE = re.compile(r"pass|secret|token|key|pwd|auth|credential|private", re.I)
BULKY = {"content", "yaml_text"}  # file contents are never logged, only their size
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_STR = 300


def mask_args(args: Any, secret_names: Iterable[str] = (), _key: str = "") -> Any:
    """Copy of `args` with secret values replaced by ***. A value is secret when its key looks like one
    (password, token, key...) or is a field the catalog marks as secret. Long strings are shortened."""
    names = {n.lower() for n in secret_names}
    if isinstance(args, dict):
        out = {}
        for k, v in args.items():
            ks = str(k)
            if ks.lower() in names or SECRET_NAME_RE.search(ks):
                out[ks] = MASK if v not in (None, "") else v
            elif ks in BULKY and isinstance(v, str):
                out[ks] = f"<{len(v)} characters not logged>"
            else:
                out[ks] = mask_args(v, names, ks)
        return out
    if isinstance(args, (list, tuple)):
        return [mask_args(v, names, _key) for v in args]
    if isinstance(args, str) and len(args) > MAX_STR:
        return args[:MAX_STR] + "..."
    return args


class AuditLog:
    def __init__(self, path: Path, secret_names: Callable[[], Iterable[str]] = lambda: (),
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.path = Path(path)
        self._secret_names = secret_names
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, tool: str, args: dict | None, status: str, detail: str = "", client: str = "") -> dict:
        try:
            names = list(self._secret_names())
        except Exception:  # a broken catalog must not stop auditing; the name heuristic still applies
            names = []
        rec = {
            "time": self._clock().isoformat(timespec="seconds").replace("+00:00", "Z"),
            "tool": tool,
            "args": mask_args(args or {}, names),
            "status": status,
        }
        if detail:
            rec["detail"] = detail[:500]
        if client:
            rec["client"] = client
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_big()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return rec

    def _rotate_if_big(self) -> None:
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))  # keep one old file
        except FileNotFoundError:
            pass

    def recent(self, limit: int = 50, tool: str | None = None, status: str | None = None) -> list[dict]:
        """Newest first. Unreadable lines are skipped. `tool` / `status` keep only matching entries. Arguments are masked
        again on the way out (they were masked when written; this is a second safety net)."""
        limit = max(1, min(500, limit))
        with self._lock:
            files = [self.path.with_name(self.path.name + ".1"), self.path]
            lines: deque[str] = deque(maxlen=None if (tool or status) else limit)
            for f in files:
                try:
                    with open(f, encoding="utf-8") as fh:
                        lines.extend(fh)
                except FileNotFoundError:
                    continue
        try:
            names = list(self._secret_names())
        except Exception:  # noqa: BLE001
            names = []
        out = []
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if tool and rec.get("tool") != tool:
                continue
            if status and rec.get("status") != status:
                continue
            rec["args"] = mask_args(rec.get("args") or {}, names)
            out.append(rec)
            if len(out) >= limit:
                break
        return out
