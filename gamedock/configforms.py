"""Structured ("form") view of config files.

The editor can show a settings file as a list of labelled fields instead of one block of text. This module
turns text into fields and applies field changes back to the text. It never rewrites a file wholesale: a change
replaces only the characters of the one value that changed, so comments, ordering, spacing and every setting
that was not touched stay byte for byte as they were.

Supported shapes (everything else has no form and falls back to the raw text editor):

  optionsettings  one ``OptionSettings=(Key=Value,Key=Value,...)`` entry, as Unreal Engine games such as Palworld
                  keep their settings. Quoted values may contain commas and parentheses.
  keyvalue        ordinary ``key=value`` lines (ini files with [sections], Java ``.properties`` such as Minecraft's
                  server.properties). Comment lines and blank lines are left alone.
  json            a JSON object. Flat top-level values are fields; nested objects and arrays are shown as raw JSON.

Callers pass text in which sensitive values are already masked (see configfiles.mask_text). A masked value that is
sent back unchanged is ignored, so the real secret in the file is never touched.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .configfiles import MASK
from .errors import ValidationError
from .models import ConfigFile, ConfigForm, FormField

KIND_OPTIONS, KIND_KEYVALUE, KIND_JSON = "optionsettings", "keyvalue", "json"
KV_SUFFIXES = (".properties", ".ini", ".cfg", ".conf", ".env")
MAX_FIELDS = 2000

_NUMBER_RE = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_OPTIONS_RE = re.compile(r"^[ \t]*OptionSettings[ \t]*=[ \t]*\(", re.MULTILINE)
_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]\r\n]+)\]\s*$")
_KV_RE = re.compile(r"^(?P<lead>[ \t]*)(?P<key>[^\s=#;!\[][^=]*?)(?P<sep>[ \t]*=[ \t]*)(?P<val>.*?)(?P<trail>[ \t]*)$")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


@dataclass
class _Item:
    id: str
    key: str
    group: str
    start: int          # span of the value text inside the file text
    end: int
    raw: str            # the value exactly as written
    type: str           # text | number | bool | json
    value: str          # what the form shows (quotes removed, escapes resolved)
    quoted: bool = False
    sensitive: bool = False


def humanize(key: str) -> str:
    """'ServerPlayerMaxNum' -> 'Server Player Max Num', 'max-players' -> 'Max Players', 'bIsPvP' -> 'Is PvP'."""
    k = key
    if re.match(r"^b[A-Z]", k):  # Unreal's boolean prefix
        k = k[1:]
    k = re.sub(r"[_\-./]+", " ", k)
    k = _CAMEL_RE.sub(" ", k)
    words: list[str] = []
    for w in k.split():
        if words and len(w) == 1 and w.isupper() and words[-1][-1:].islower():
            words[-1] += w  # "Pv" + "P" -> "PvP"
        else:
            words.append(w)
    return " ".join(w[:1].upper() + w[1:] for w in words) or key


def _classify(raw: str) -> tuple[str, str, bool]:
    """(type, shown value, quoted) for a raw ini-style value."""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return "text", _unescape(raw[1:-1]), True
    if raw.lower() in ("true", "false"):
        return "bool", raw, False
    if _NUMBER_RE.match(raw):
        return "number", raw, False
    return "text", raw, False


def _unescape(s: str) -> str:
    return re.sub(r'\\(["\\])', r"\1", s)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _is_sensitive(key: str, cf: ConfigFile) -> bool:
    return key.lower() in {k.lower() for k in cf.sensitive_keys}


def _unique(base: str, seen: dict[str, int]) -> str:
    n = seen.get(base, 0) + 1
    seen[base] = n
    return base if n == 1 else f"{base}#{n}"


# ------------------------------------------------------------------ optionsettings
def _scan_options(text: str, cf: ConfigFile) -> list[_Item] | None:
    m = _OPTIONS_RE.search(text)
    if not m:
        return None
    n, i = len(text), m.end()
    items: list[_Item] = []
    seen: dict[str, int] = {}
    while True:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return None  # the closing parenthesis is missing
        if text[i] == ")":
            break
        if text[i] == ",":
            i += 1
            continue
        k = i
        while k < n and text[k] not in "=,)":
            k += 1
        if k >= n:
            return None
        if text[k] != "=":  # a bare element without a value: not a setting, skip it
            i = k
            continue
        key = text[i:k].strip()
        v0 = k + 1
        p, depth, inq = v0, 0, False
        while p < n:
            c = text[p]
            if inq:
                if c == "\\" and p + 1 < n:
                    p += 2
                    continue
                if c == '"':
                    inq = False
            elif c == '"':
                inq = True
            elif c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            p += 1
        if p >= n or inq:
            return None
        chunk = text[v0:p]
        lead = len(chunk) - len(chunk.lstrip())
        start, end = v0 + lead, p - (len(chunk) - len(chunk.rstrip()))
        if end < start:
            end = start
        raw = text[start:end]
        typ, value, quoted = _classify(raw)
        sensitive = _is_sensitive(key, cf)
        if sensitive and typ != "text":
            typ = "text"
        if key:
            items.append(_Item(_unique(key, seen), key, "", start, end, raw, typ, value, quoted, sensitive))
        i = p
        if len(items) > MAX_FIELDS:
            return None
    return items


# ------------------------------------------------------------------ key=value (ini, properties)
def _scan_keyvalue(text: str, cf: ConfigFile) -> list[_Item] | None:
    items: list[_Item] = []
    seen: dict[str, int] = {}
    section = ""
    offset = 0
    candidates = 0  # lines that are neither blank, comments nor section headers
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        line_start = offset
        offset += len(line)
        stripped = body.strip()
        if not stripped or stripped[0] in "#;!":
            continue
        sm = _SECTION_RE.match(body)
        if sm:
            section = sm["name"].strip()
            continue
        candidates += 1
        m = _KV_RE.match(body)
        if not m:
            continue
        key = m["key"].strip()
        raw = m["val"]
        typ, value, quoted = _classify(raw)
        sensitive = _is_sensitive(key, cf)
        if sensitive and typ != "text":
            typ = "text"
        base = f"[{section}]{key}" if section else key
        items.append(_Item(_unique(base, seen), key, section, line_start + m.start("val"), line_start + m.end("val"),
                           raw, typ, value, quoted, sensitive))
        if len(items) > MAX_FIELDS:
            return None
    if not items or len(items) * 2 < candidates:  # mostly not key=value: do not pretend it is a settings file
        return None
    return items


# ------------------------------------------------------------------ json
def _scan_json(text: str, cf: ConfigFile) -> list[_Item] | None:
    dec = json.JSONDecoder()
    n = len(text)

    def ws(i: int) -> int:
        while i < n and text[i] in " \t\r\n":
            i += 1
        return i

    try:
        i = ws(0)
        if i >= n or text[i] != "{":
            return None
        i = ws(i + 1)
        items: list[_Item] = []
        seen: dict[str, int] = {}
        if i < n and text[i] == "}":
            return items if ws(i + 1) >= n else None
        while True:
            key, i = dec.raw_decode(text, ws(i))
            if not isinstance(key, str):
                return None
            i = ws(i)
            if i >= n or text[i] != ":":
                return None
            i = ws(i + 1)
            start = i
            val, i = dec.raw_decode(text, i)
            raw = text[start:i]
            if isinstance(val, bool):
                typ, shown = "bool", raw
            elif isinstance(val, (int, float)):
                typ, shown = "number", raw
            elif isinstance(val, str):
                typ, shown = "text", val
            else:
                typ, shown = "json", raw
            sensitive = _is_sensitive(key, cf) and isinstance(val, str)
            items.append(_Item(_unique(key, seen), key, "", start, i, raw, typ, shown, False, sensitive))
            if len(items) > MAX_FIELDS:
                return None
            i = ws(i)
            if i < n and text[i] == ",":
                i += 1
                continue
            if i < n and text[i] == "}":
                return items if ws(i + 1) >= n else None
            return None
    except (ValueError, IndexError):
        return None


# ------------------------------------------------------------------ public API
def detect_kind(text: str, cf: ConfigFile) -> str | None:
    """Which structured view fits this file, or None. Decided by the catalog format hint, the file name and the text."""
    fmt, path = cf.format, cf.path.lower()
    if fmt == "yaml":
        return None
    if fmt == "json" or path.endswith(".json"):
        return KIND_JSON if _scan_json(text, cf) is not None else None
    if _OPTIONS_RE.search(text):
        return KIND_OPTIONS if _scan_options(text, cf) is not None else None
    if fmt == "ini" or path.endswith(KV_SUFFIXES):
        return KIND_KEYVALUE if _scan_keyvalue(text, cf) is not None else None
    return None


def _scan(text: str, cf: ConfigFile, kind: str) -> list[_Item] | None:
    return {KIND_OPTIONS: _scan_options, KIND_KEYVALUE: _scan_keyvalue, KIND_JSON: _scan_json}[kind](text, cf)


def build_form(text: str, cf: ConfigFile) -> ConfigForm | None:
    """The fields of `text`, or None if this file has no structured view."""
    kind = detect_kind(text, cf)
    if kind is None:
        return None
    items = _scan(text, cf, kind) or []
    return ConfigForm(kind, tuple(
        FormField(it.id, it.key, humanize(it.key), it.value, it.type, it.sensitive, it.group) for it in items))


def _render(it: _Item, kind: str, new: str) -> str:
    """The text to write for a new value of this field, in the style the file already uses."""
    if "\n" in new or "\r" in new or "\0" in new:
        if not (it.type == "json" and kind == KIND_JSON):
            raise ValidationError(f"'{it.key}' cannot contain line breaks")
    if kind == KIND_JSON:
        if it.type == "bool":
            if new.strip().lower() not in ("true", "false"):
                raise ValidationError(f"'{it.key}' must be true or false")
            return new.strip().lower()
        if it.type == "number":
            try:
                v = json.loads(new)
            except ValueError:
                v = None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValidationError(f"'{it.key}' must be a number")
            return new.strip()
        if it.type == "json":
            try:
                json.loads(new)
            except ValueError as e:
                raise ValidationError(f"'{it.key}' is not valid JSON: {e}") from None
            return new.strip()
        return json.dumps(new, ensure_ascii=False)
    # ini style
    if it.quoted:
        return f'"{_escape(new)}"'
    if it.type == "bool":
        low = new.strip().lower()
        if low not in ("true", "false"):
            raise ValidationError(f"'{it.key}' must be True or False")
        if it.raw.islower():
            return low
        return "True" if low == "true" else "False"
    if it.type == "number":
        if not _NUMBER_RE.match(new.strip()):
            raise ValidationError(f"'{it.key}' must be a number")
        return new.strip()
    if kind == KIND_OPTIONS and _needs_quotes(new):
        return f'"{_escape(new)}"'
    return new


def _needs_quotes(v: str) -> bool:
    """A bare OptionSettings value breaks the line if it holds a comma, a quote or unbalanced parentheses
    (a balanced "(A,B)" list is legitimate and stays bare)."""
    if v != v.strip():
        return True
    depth = 0
    for ch in v:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return True
        elif ch == '"':
            return True
        elif ch == "," and depth == 0:
            return True
    return depth != 0


def apply_changes(text: str, cf: ConfigFile, changes: dict[str, str]) -> tuple[str, list[str]]:
    """Return (new text, ids that really changed). Only the changed values are replaced. A sensitive field that
    still holds the mask placeholder is left alone."""
    kind = detect_kind(text, cf)
    if kind is None:
        raise ValidationError("This file has no form view; edit the raw text instead")
    items = _scan(text, cf, kind) or []
    by_id = {it.id: it for it in items}
    edits: list[tuple[int, int, str]] = []
    changed: list[str] = []
    for fid, new in changes.items():
        it = by_id.get(fid)
        if it is None:
            raise ValidationError(f"Unknown setting '{fid}' in this file")
        new = "" if new is None else str(new)
        if it.sensitive and new == MASK:
            continue
        if new == it.value:
            continue
        rendered = _render(it, kind, new)
        if rendered == it.raw:
            continue
        edits.append((it.start, it.end, rendered))
        changed.append(fid)
    out = text
    for start, end, rendered in sorted(edits, reverse=True):
        out = out[:start] + rendered + out[end:]
    return out, changed
