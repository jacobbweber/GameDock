"""Pure helpers for the setup check (verify_game_setup), the diagnostics bundle and the lint script: result types,
text redaction, config-file parsing, log reading. No Docker, no GameDock objects."""
from __future__ import annotations

import configparser
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
_ORDER = {FAIL: 3, WARN: 2, PASS: 1, SKIP: 0}
LABEL = {PASS: "OK", WARN: "Check", FAIL: "Problem", SKIP: "Skipped"}


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str = ""
    fix: str = ""
    action: str = ""  # a button the web page offers next to the check (e.g. "fix_ownership"); "" = none

    def to_dict(self) -> dict:
        d = {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail}
        if self.fix:
            d["fix"] = self.fix
        if self.action:
            d["action"] = self.action
        return d


def summarize(checks: list[Check]) -> dict:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    overall = FAIL if counts[FAIL] else WARN if counts[WARN] else PASS
    parts = [f"{counts[PASS]} OK"]
    if counts[WARN]:
        parts.append(f"{counts[WARN]} to check")
    if counts[FAIL]:
        parts.append(f"{counts[FAIL]} problem(s)")
    if counts[SKIP]:
        parts.append(f"{counts[SKIP]} skipped")
    suggestions: list[str] = []
    for c in sorted(checks, key=lambda c: -_ORDER.get(c.status, 0)):
        if c.fix and c.status in (FAIL, WARN) and c.fix not in suggestions:
            suggestions.append(c.fix)
    return {"overall": overall, "counts": counts, "summary": ", ".join(parts), "suggestions": suggestions}


# ---------------------------------------------------------------------------------------------- redaction
_SECRET_KEY = r"[A-Za-z0-9_.\-]*(?:pass(?:word|wd)?|pwd|secret|token|api[_\-]?key|auth|credential|private[_\-]?key)[A-Za-z0-9_.\-]*"
_ASSIGN_RE = re.compile(rf"(?i)\b({_SECRET_KEY})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;)]+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_URL_CRED_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")
MASK = "********"


def redact_text(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Hide anything that looks like a password or token in text that is going to be shared (diagnostics, pasted into a
    chat). Known secret values are replaced first; then `password=...`, `Bearer ...` and `scheme://user:pass@` shapes."""
    for v in sorted({s for s in extra_secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(v, MASK)
    text = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:{MASK}@", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", text)
    return _ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)


# ---------------------------------------------------------------------------------------------- config files
def check_config_text(fmt: str, text: str, form_kind: str | None, form_fields: int) -> tuple[str, str, str]:
    """(status, detail, fix) for one config file's text and its declared format."""
    shown = f" A form view with {form_fields} setting(s) is available ({form_kind})." if form_kind else ""
    if not text.strip():
        return WARN, "The file exists but is empty.", (
            "Many games fill it in on the first start or when the first world is created: run the check again after that. "
            "If it stays empty, this may not be the file the game really reads.")
    if fmt == "json":
        try:
            data = json.loads(text)
        except ValueError as e:
            return FAIL, f"The file is declared as json but is not valid JSON ({e}).", (
                "If the game writes something else, change this config file's format to text (or ini) in the catalog entry.")
        n = len(data) if isinstance(data, (dict, list)) else 1
        return PASS, f"Valid JSON with {n} top-level item(s).{shown}", ""
    if fmt == "yaml":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return FAIL, f"The file is declared as yaml but is not valid YAML ({str(e).splitlines()[0]}).", (
                "Change this config file's format to text in the catalog entry if the game does not write real YAML.")
        return PASS, f"Valid YAML ({type(data).__name__}).{shown}", ""
    if fmt == "ini":
        cp = configparser.RawConfigParser(strict=False, allow_no_value=True, interpolation=None)
        cp.optionxform = str  # type: ignore[assignment]
        try:
            cp.read_string(text)
        except configparser.MissingSectionHeaderError:
            if form_kind == "keyvalue":
                return PASS, f"Plain key=value lines without [sections].{shown}", ""
            return WARN, "The file is declared as ini but has no [section] headers and does not look like key=value lines.", (
                "Set this config file's format to text in the catalog entry unless it really is an ini file.")
        except configparser.Error as e:
            return FAIL, f"The file is declared as ini but could not be read as ini ({str(e).splitlines()[0]}).", (
                "Set this config file's format to text in the catalog entry if the game's file is not ini.")
        n = sum(len(cp[s]) for s in cp.sections())
        return PASS, f"Valid ini with {len(cp.sections())} section(s) and {n} setting(s).{shown}", ""
    return PASS, f"Readable text file ({len(text)} characters).{shown}", ""


def find_by_name(root: Path, basename: str, cap: int = 6000, limit: int = 3, max_depth: int = 8) -> list[str]:
    """Paths (relative to `root`) of files called `basename`, to suggest the right config path. Bounded, no symlinks."""
    out: list[str] = []
    budget = [cap]

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth or budget[0] <= 0 or len(out) >= limit:
            return
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            budget[0] -= 1
            if budget[0] <= 0:
                return
            try:
                if e.is_file(follow_symlinks=False) and e.name == basename:
                    out.append(os.path.relpath(e.path, root))
                elif e.is_dir(follow_symlinks=False):
                    walk(Path(e.path), depth + 1)
            except OSError:
                continue

    walk(root, 0)
    return out


# ---------------------------------------------------------------------------------------------- logs
_READY_WORDS = re.compile(r"(?i)\b(ready|listening|session|server (?:is )?(?:now )?(?:open|started|running|up)|"
                          r"now open|accepting|started|open for|joinable|online|running)\b")


def ready_line_candidates(text: str, limit: int = 3) -> list[str]:
    """Log lines that look like "the game can be joined now", newest first, to suggest a ready_log_pattern."""
    out: list[str] = []
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        if _READY_WORDS.search(line) and 8 <= len(line) <= 200 and line not in out:
            out.append(line)
            if len(out) >= limit:
                break
    return out


def parse_started(started: str):
    """RFC 3339 text (Docker's, with up to 9 fractional digits) -> aware datetime, or None."""
    from datetime import datetime, timezone
    if not started or started.startswith("0001-"):
        return None
    t = re.sub(r"(\.\d{1,6})\d*", r"\1", started.replace("Z", "+00:00"))
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
