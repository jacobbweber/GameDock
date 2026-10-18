"""Recorded defaults of config files (added in 0.5.2).

The first time GameDock sees a config file that exists and has a settings form, it saves a *baseline*: the parsed list
of settings (id, key, type, value) as the game wrote them. That is the game's own generated defaults plus the values
from the deploy form, from before any GameDock edit. Later, the Config form compares the file with the baseline, so
the settings that differ from the defaults stay marked after saving, closing and reopening.

Rules that keep this safe and predictable:

* A baseline stores the parsed settings, never the file text. Values of sensitive keys (the catalog's
  ``sensitive_keys``) are never stored at all, only a set / not set marker. The baseline is built from the masked
  text, so a secret is never even held in memory here.
* Baselines live in ``<data>/config-baselines/<server id>/<sha1 of the relative path>.json``, outside the game's
  mounted folders, written atomically with mode 0600 (folder 0700).
* A baseline is never overwritten automatically. The only way to replace it is the deliberate "Record current settings
  as the default" action; the old one is then kept once as ``<sha1>.prev.json``.
* Comparing is by meaning, not by text: ``1``, ``1.0`` and ``1.000000`` are the same number, ``True`` and ``true`` the
  same boolean, surrounding whitespace is ignored, and arrays/objects are compared as JSON.

This module is generic. It knows nothing about any particular game.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .configfiles import MASK
from .models import ConfigForm, FormField, MissingField

VERSION = 1
DIRNAME = "config-baselines"

ORIGIN_DEPLOY = "deploy"          # taken when a freshly deployed server first became ready
ORIGIN_FIRST_SEEN = "first-seen"  # taken the first time GameDock looked at an existing file
ORIGIN_PRE_SAVE = "pre-save"      # taken just before GameDock's first write to the file
ORIGIN_MANUAL = "manual"          # "Record current settings as the default"
ORIGINS = (ORIGIN_DEPLOY, ORIGIN_FIRST_SEEN, ORIGIN_PRE_SAVE, ORIGIN_MANUAL)

STATUS_SAME, STATUS_CHANGED, STATUS_NEW, STATUS_MISSING = "same", "changed", "new", "missing"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_NUM_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


# ------------------------------------------------------------------ comparing values
def _number(s: str) -> str:
    try:
        d = Decimal(s)
    except InvalidOperation:
        return s
    if d == 0:
        return "0"
    return str(d.normalize())


def _plain_json(v: object) -> object:
    """A JSON value reduced to something that compares by meaning (1 == 1.0, true != 1)."""
    if isinstance(v, bool):
        return f"b:{str(v).lower()}"
    if isinstance(v, (int, float)):
        return "n:" + _number(repr(v))
    if isinstance(v, str):
        return "s:" + v
    if v is None:
        return "null"
    if isinstance(v, list):
        return [_plain_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain_json(x) for k, x in v.items()}
    return str(v)


def canon(value: str | None, typ: str) -> str:
    """A string that is equal for two values that mean the same thing in a settings file."""
    v = (value or "").strip()
    low = v.lower()
    if typ == "auto":  # types differ between the two sides: decide by what the text looks like
        if low in ("true", "false"):
            return "B:" + low
        if _NUM_RE.match(v):
            return "N:" + _number(v)
        return "T:" + v
    if typ == "json":
        try:
            return "J:" + json.dumps(_plain_json(json.loads(v)), sort_keys=True, separators=(",", ":"))
        except ValueError:
            return "T:" + v
    if typ == "bool" and low in ("true", "false"):
        return "B:" + low
    if typ == "number" and _NUM_RE.match(v):
        return "N:" + _number(v)
    return "T:" + v


def same_value(a: str | None, a_type: str, b: str | None, b_type: str) -> bool:
    if a_type != b_type:
        return canon(a, "auto") == canon(b, "auto")
    return canon(a, a_type) == canon(b, b_type)


# ------------------------------------------------------------------ the snapshot
def _is_secret(f: FormField, sensitive_keys: tuple[str, ...]) -> bool:
    last = re.split(r"[.\[]", f.key)[-1].lower() if f.key else ""
    return f.sensitive or f.key.lower() in {k.lower() for k in sensitive_keys} or last in {k.lower() for k in sensitive_keys}


def make_snapshot(form: ConfigForm, sensitive_keys: tuple[str, ...], *, path: str, captured_at: str, origin: str,
                  source_mtime: float | None, source_size: int | None) -> dict:
    """The data stored for one file. `form` was built from masked text, so a secret is never in it; values of
    sensitive fields are dropped anyway and replaced by a set / not set marker."""
    fields = []
    for f in form.fields:
        secret = _is_secret(f, sensitive_keys)
        fields.append({
            "id": f.id, "key": f.key, "label": f.label, "group": f.group, "type": f.type, "sensitive": secret,
            "value": None if secret else f.value,
            "set": (f.value != "") if secret else None,
        })
    return {"version": VERSION, "path": path, "kind": form.kind, "captured_at": captured_at, "origin": origin,
            "source_mtime": source_mtime, "source_size": source_size, "fields": fields}


def _valid(d: object) -> bool:
    return (isinstance(d, dict) and d.get("version") == VERSION and isinstance(d.get("fields"), list)
            and all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in d["fields"]))


def compare(form: ConfigForm, snap: dict) -> ConfigForm:
    """`form` with the default, differs, status of every field filled in, the settings that the file lost listed in
    `missing`, and `differing` set. `form` may hold edits that are not saved yet: it is the working copy."""
    base = {x["id"]: x for x in snap["fields"]}
    out: list[FormField] = []
    seen: set[str] = set()
    for f in form.fields:
        seen.add(f.id)
        b = base.get(f.id)
        if b is None:
            out.append(replace(f, default=None, default_set=None, differs=True, status=STATUS_NEW))
            continue
        if b.get("sensitive") or f.sensitive:
            typed_new = f.value not in ("", MASK)
            b_set = bool(b.get("set"))
            differs = typed_new or ((f.value != "") != b_set)
            out.append(replace(f, default=None, default_set=b_set, differs=differs,
                               status=STATUS_CHANGED if differs else STATUS_SAME))
            continue
        differs = not same_value(f.value, f.type, b.get("value"), b.get("type", "text"))
        out.append(replace(f, default=b.get("value"), default_set=None, differs=differs,
                           status=STATUS_CHANGED if differs else STATUS_SAME))
    missing = tuple(
        MissingField(x["id"], x.get("key", x["id"]), x.get("label", x.get("key", x["id"])), x.get("value"),
                     x.get("type", "text"), bool(x.get("sensitive")), x.get("group", ""),
                     default_set=bool(x.get("set")) if x.get("sensitive") else None)
        for x in snap["fields"] if x["id"] not in seen)
    differing = sum(1 for f in out if f.differs) + len(missing)
    return replace(form, fields=tuple(out), missing=missing, differing=differing)


# ------------------------------------------------------------------ storage
class BaselineStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _key(rel: str) -> str:
        return hashlib.sha1(rel.encode("utf-8")).hexdigest()

    def _dir(self, server_id: str) -> Path:
        if not _ID_RE.match(server_id or ""):
            raise ValueError("bad server id")
        return self.root / server_id

    def path_for(self, server_id: str, rel: str) -> Path:
        return self._dir(server_id) / f"{self._key(rel)}.json"

    def load(self, server_id: str, rel: str) -> dict | None:
        try:
            p = self.path_for(server_id, rel)
            with open(p, "rb") as f:
                d = json.loads(f.read(8 * 1024 * 1024).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return d if _valid(d) else None

    def has_previous(self, server_id: str, rel: str) -> bool:
        try:
            return self.path_for(server_id, rel).with_suffix(".prev.json").is_file()
        except ValueError:
            return False

    def save(self, server_id: str, rel: str, snap: dict, keep_previous: bool = False) -> None:
        """Atomic, mode 0600. With keep_previous the existing baseline is first moved to <sha1>.prev.json (one level)."""
        d = self._dir(server_id)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        target = self.path_for(server_id, rel)
        if keep_previous and target.is_file():
            os.replace(target, target.with_name(target.name[:-len(".json")] + ".prev.json"))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".baseline.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(snap, indent=1, ensure_ascii=False).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def create_if_absent(self, server_id: str, rel: str, snap: dict) -> bool:
        """Save only if there is no valid baseline. Never overwrites. Returns True if it was written."""
        if self.load(server_id, rel) is not None:
            return False
        self.save(server_id, rel, snap)
        return True

    def remove_server(self, server_id: str) -> None:
        try:
            d = self._dir(server_id)
        except ValueError:
            return
        shutil.rmtree(d, ignore_errors=True)
