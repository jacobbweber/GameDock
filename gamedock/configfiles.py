"""Safe reading and writing of the config files a catalog entry declares.

Only files declared in the catalog can be touched, and each resolved path must stay inside the
server's data folder (symlinks that lead outside are refused). Writes keep a timestamped .bak copy.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import AccessError, OperationError, TooLargeError, ValidationError
from .models import ConfigFile

MAX_CONFIG_BYTES = 256 * 1024
KEEP_BAKS = 10
MASK = "********"


def safe_path(data_root: Path, rel: str) -> Path:
    """Return the absolute path of `rel` inside data_root, or raise ValidationError."""
    if not rel or "\0" in rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValidationError("Config path must be relative and stay inside the server's data folder")
    root = data_root.resolve()
    candidate = root / rel
    resolved = candidate.resolve()  # follows symlinks in every component
    if resolved != root and root not in resolved.parents:
        raise ValidationError("Config path escapes the server's data folder (symlink or '..'); refused")
    if candidate.is_symlink():
        raise ValidationError("Config file is a symlink; refusing to edit it")
    return resolved


# --------------------------------------------------------------------------- masking
def _pattern(keys: tuple[str, ...]) -> re.Pattern | None:
    if not keys:
        return None
    alt = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    # optional quotes around the key; value is a quoted string or a bare token (ini, properties, json, yaml,
    # and Palworld-style inline OptionSettings=(Key=Value,...) are all handled by this one shape)
    return re.compile(
        rf'(?<![A-Za-z0-9_.\-])(?P<q1>"?)(?P<key>{alt})(?P<q2>"?)(?P<sep>[ \t]*[=:][ \t]*)'
        rf'(?P<val>"(?:[^"\\\r\n]|\\.)*"|[^\s,)}}\]\r\n]*)',
        re.IGNORECASE,
    )


def _unquote(v: str) -> str:
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def _quote_like(template: str, raw: str) -> str:
    return f'"{raw}"' if template.startswith('"') else raw


def mask_text(text: str, keys: tuple[str, ...]) -> str:
    pat = _pattern(keys)
    if not pat:
        return text

    def repl(m: re.Match) -> str:
        if not _unquote(m["val"]):
            return m.group(0)  # nothing to hide
        return f'{m["q1"]}{m["key"]}{m["q2"]}{m["sep"]}{_quote_like(m["val"], MASK)}'

    return pat.sub(repl, text)


def unmask_text(new_text: str, old_text: str, keys: tuple[str, ...]) -> str:
    """Put real values back where the user left the mask placeholder untouched."""
    pat = _pattern(keys)
    if not pat or MASK not in new_text:
        return new_text
    olds: dict[str, list[str]] = {}
    for m in pat.finditer(old_text):
        olds.setdefault(m["key"].lower(), []).append(m["val"])
    seen: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        key = m["key"].lower()
        idx = seen.get(key, 0)
        seen[key] = idx + 1
        if _unquote(m["val"]) != MASK:
            return m.group(0)
        stored = olds.get(key, [])
        if idx >= len(stored):
            raise ValidationError(
                f"'{m['key']}' still shows the hidden placeholder. Type the real value, or delete the line.")
        return f'{m["q1"]}{m["key"]}{m["q2"]}{m["sep"]}{_quote_like(m["val"], _unquote(stored[idx]))}'

    return pat.sub(repl, new_text)


# --------------------------------------------------------------------------- read / write
@dataclass(frozen=True)
class ConfigRead:
    exists: bool
    size: int
    content: str  # masked
    masked: bool


def read_config(data_root: Path, cf: ConfigFile) -> ConfigRead:
    path = safe_path(data_root, cf.path)
    if not path.exists():
        return ConfigRead(False, 0, "", False)
    if not path.is_file():
        raise ValidationError("Config path is not a regular file")
    size = path.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise TooLargeError(f"Config file is {size} bytes; the editor limit is {MAX_CONFIG_BYTES}")
    try:
        text = _read_text(path)
    except PermissionError as e:
        raise AccessError(f"Cannot read {cf.path}: permission denied (owned by another user?)") from e
    masked = mask_text(text, cf.sensitive_keys)
    return ConfigRead(True, size, masked, masked != text)


def _read_text(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        raw = f.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise TooLargeError(f"Config file is larger than the editor limit of {MAX_CONFIG_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("Config file is not valid UTF-8 text; edit it by hand") from None


@dataclass(frozen=True)
class ConfigWrite:
    size: int
    backup: str | None  # file name of the .bak copy, if one was made


def write_config(data_root: Path, cf: ConfigFile, content: str, now: datetime) -> ConfigWrite:
    if "\0" in content:
        raise ValidationError("Content contains a NUL character")
    path = safe_path(data_root, cf.path)
    if path.exists() and not path.is_file():
        raise ValidationError("Config path is not a regular file")
    old_text = ""
    if path.exists():
        try:
            old_text = _read_text(path)
        except PermissionError as e:
            raise AccessError(f"Cannot read {cf.path}: permission denied (owned by another user?)") from e
        except ValidationError:
            old_text = ""  # not text; we still keep a byte-for-byte .bak below
    final = unmask_text(content, old_text, cf.sensitive_keys)
    data = final.encode("utf-8")
    if len(data) > MAX_CONFIG_BYTES:
        raise TooLargeError(f"Content is {len(data)} bytes; the limit is {MAX_CONFIG_BYTES}")
    if cf.format == "json":
        try:
            json.loads(final)
        except ValueError as e:
            raise ValidationError(f"Not valid JSON: {e}") from None

    parent = path.parent
    bak_name = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            bak_name = _make_bak(path, now)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except PermissionError as e:
        raise AccessError(
            f"Cannot write {cf.path}: permission denied. The file or folder is probably owned by another user "
            f"(such as root). Fix with: sudo chown -R $(id -u):$(id -g) '{data_root}'") from e
    except OSError as e:
        raise OperationError(f"Could not save {cf.path}: {e.strerror or e}") from e
    _prune_baks(path)
    return ConfigWrite(len(data), bak_name)


def _make_bak(path: Path, now: datetime) -> str:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    name, n = f"{path.name}.{stamp}.bak", 2
    while (path.parent / name).exists():
        name = f"{path.name}.{stamp}-{n}.bak"
        n += 1
    with open(path, "rb") as src, open(path.parent / name, "wb") as dst:
        dst.write(src.read())
    os.chmod(path.parent / name, stat.S_IMODE(path.stat().st_mode))
    return name


def _bak_key(name: str):
    m = re.search(r"\.(\d{8}-\d{6})(?:-(\d+))?\.bak$", name)
    return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)


def list_baks(path: Path) -> list[str]:
    """Names of .bak copies for `path`, newest first."""
    if not path.parent.is_dir():
        return []
    names = [p.name for p in path.parent.iterdir()
             if p.name.startswith(path.name + ".") and p.name.endswith(".bak") and _bak_key(p.name)[0]]
    return sorted(names, key=_bak_key, reverse=True)


def _prune_baks(path: Path) -> None:
    for name in list_baks(path)[KEEP_BAKS:]:
        try:
            (path.parent / name).unlink()
        except OSError:
            pass
