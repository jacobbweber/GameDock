"""Load catalog entries (game templates) from YAML/JSON files in the catalog directory."""
from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

from .errors import CatalogError
from .steam import APP_ID_RE, BRANCH_RE
from .models import (
    CONFIG_FORMATS, ENV_NAME_RE, GAME_UPDATE_METHODS, ID_RE, IMAGE_RE, UPDATE_METHODS, CatalogEntry, ConfigFile, EnvField, FailPattern,
    PortMapping,
    VolumeMapping, is_reserved_env_name,
)


def _parse_port(p: object, source: str) -> PortMapping:
    try:
        proto = str(p["protocol"]).lower()  # type: ignore[index]
        host, cont = int(p["host"]), int(p["container"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError) as e:
        raise CatalogError(f"{source}: bad port entry {p!r}") from e
    if proto not in ("tcp", "udp"):
        raise CatalogError(f"{source}: port protocol must be tcp or udp, got {proto!r}")
    if not (1 <= host <= 65535 and 1 <= cont <= 65535):
        raise CatalogError(f"{source}: port out of range in {p!r}")
    return PortMapping(proto, host, cont)


def _parse_field(f: object, source: str) -> EnvField:
    if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not ENV_NAME_RE.match(f["name"]):
        raise CatalogError(f"{source}: bad env field entry {f!r}")
    if is_reserved_env_name(f["name"]):
        raise CatalogError(f"{source}: '{f['name']}' is a reserved name (it would change how the docker command "
                           "behaves) and cannot be used as a setting")
    pattern = _parse_pattern(f.get("pattern"), f"pattern of setting {f['name']}", source, groups=None)
    hint = str(f.get("pattern_hint") or "").strip().rstrip(".").strip()
    if len(hint) > MAX_HINT or "\n" in hint or "\0" in hint:
        raise CatalogError(f"{source}: pattern_hint of {f['name']} must be one line of at most {MAX_HINT} characters")
    if hint and not pattern:
        raise CatalogError(f"{source}: setting {f['name']} has a pattern_hint but no pattern")
    default = "" if f.get("default") is None else str(f["default"])
    if pattern and default and not _full_match(pattern, default):
        raise CatalogError(f"{source}: the default of {f['name']} does not match its own pattern")
    return EnvField(
        name=f["name"],
        label=str(f.get("label") or f["name"]),
        default=default,
        secret=bool(f.get("secret", False)),
        required=bool(f.get("required", False)),
        description=str(f.get("description", "")),
        pattern=pattern,
        pattern_hint=hint,
    )


KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MAX_HINT = 300
MAX_FAIL_PATTERNS = 12
MAX_LOG_FILES = 6
MAX_TITLE = 120
MAX_ADVICE = 600


def _full_match(pattern: str, value: str) -> bool:
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        return False


def _parse_config_file(c: object, source: str) -> ConfigFile:
    if not isinstance(c, dict) or not isinstance(c.get("path"), str) or not c["path"].strip():
        raise CatalogError(f"{source}: bad config file entry {c!r}")
    path = c["path"].strip()
    pp = PurePosixPath(path)
    if pp.is_absolute() or ".." in pp.parts or "\\" in path or "\0" in path:
        raise CatalogError(f"{source}: config path must be relative and stay inside the data folder: {path!r}")
    fmt = str(c.get("format", "text")).lower()
    if fmt not in CONFIG_FORMATS:
        raise CatalogError(f"{source}: config format must be one of {CONFIG_FORMATS}, got {fmt!r}")
    keys = c.get("sensitive_keys") or []
    if not isinstance(keys, list) or not all(isinstance(k, str) and KEY_RE.match(k) for k in keys):
        raise CatalogError(f"{source}: bad sensitive_keys in config file {path!r}")
    return ConfigFile(path=str(pp), label=str(c.get("label") or path), format=fmt,
                      sensitive_keys=tuple(keys), hot_edit=bool(c.get("hot_edit", False)),
                      note=str(c.get("note") or "").strip())


MAX_PATTERN_LEN = 300


def _parse_pattern(value: object, name: str, source: str, groups: int | None) -> str:
    """A regular expression from a catalog file. Checked now so a typo is reported when the file is loaded,
    not when someone first starts the game. `groups` = exact number of capture groups required (None = any)."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise CatalogError(f"{source}: {name} must be text")
    if len(value) > MAX_PATTERN_LEN or "\n" in value or "\0" in value:
        raise CatalogError(f"{source}: {name} must be one line of at most {MAX_PATTERN_LEN} characters")
    try:
        rx = re.compile(value)
    except re.error as e:
        raise CatalogError(f"{source}: {name} is not a valid regular expression: {e}") from e
    if groups is not None and rx.groups != groups:
        raise CatalogError(f"{source}: {name} must contain exactly {groups} capture group, for example "
                           "'progress: *([0-9.]+)' (the part in brackets is the percentage)")
    return value


# A group that is repeated and contains its own repeat, such as (a+)+ or (.*)*, can make a regular expression take
# forever on some text. Log lines come from the game, so such patterns are refused in fail_log_patterns.
_NESTED_REPEAT_RE = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)[+*{]")


def _parse_fail_patterns(value: object, source: str) -> tuple[FailPattern, ...]:
    if value in (None, "", []):
        return ()
    if not isinstance(value, list):
        raise CatalogError(f"{source}: fail_log_patterns must be a list of {{pattern, title, advice}} items")
    if len(value) > MAX_FAIL_PATTERNS:
        raise CatalogError(f"{source}: fail_log_patterns has {len(value)} items; at most {MAX_FAIL_PATTERNS} are allowed")
    out: list[FailPattern] = []
    for i, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise CatalogError(f"{source}: fail_log_patterns item {i} must have pattern, title and advice")
        extra = sorted(str(k) for k in item if k not in FAIL_PATTERN_KEYS)
        if extra:
            raise CatalogError(f"{source}: fail_log_patterns item {i} has keys GameDock does not know: {', '.join(extra)} "
                               f"(allowed: {', '.join(FAIL_PATTERN_KEYS)})")
        pattern = _parse_pattern(item.get("pattern"), f"fail_log_patterns item {i} pattern", source, groups=None)
        if not pattern:
            raise CatalogError(f"{source}: fail_log_patterns item {i} needs a pattern (a regular expression)")
        if _NESTED_REPEAT_RE.search(pattern):
            raise CatalogError(f"{source}: fail_log_patterns item {i}: the pattern repeats a group that already repeats "
                               "(like (a+)+), which can hang on some log text. Write it more simply.")
        title, advice = (str(item.get(k) or "").strip() for k in ("title", "advice"))
        if not title or not advice:
            raise CatalogError(f"{source}: fail_log_patterns item {i} needs a title (what went wrong, in plain words) and "
                               "advice (what to do next)")
        if len(title) > MAX_TITLE or len(advice) > MAX_ADVICE or "\0" in title + advice:
            raise CatalogError(f"{source}: fail_log_patterns item {i}: title at most {MAX_TITLE} and advice at most "
                               f"{MAX_ADVICE} characters")
        out.append(FailPattern(pattern, " ".join(title.split()), " ".join(advice.split())))
    return tuple(out)


def _parse_log_files(value: object, source: str) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if not isinstance(value, list) or len(value) > MAX_LOG_FILES:
        raise CatalogError(f"{source}: log_files must be a list of at most {MAX_LOG_FILES} file paths")
    out = []
    for v in value:
        p = _parse_relative_path(v, "log_files item", source)
        if not p:
            raise CatalogError(f"{source}: log_files has an empty item")
        if p not in out:
            out.append(p)
    return tuple(out)


def _parse_save_command(value: object, source: str) -> tuple[str, ...]:
    """A command (list of words, run without a shell) that asks the game to save, run before a live backup."""
    if value is None or value == "" or value == []:
        return ()
    if isinstance(value, str):
        try:
            value = shlex.split(value)
        except ValueError as e:
            raise CatalogError(f"{source}: save_command is not valid: {e}") from e
    if (not isinstance(value, list) or not 1 <= len(value) <= 12
            or not all(isinstance(w, str) and w and len(w) <= 300 and not any(c in w for c in "\n\r\0") for w in value)):
        raise CatalogError(f"{source}: save_command must be a short list of words, for example [rcon-cli, save]")
    return tuple(value)


def _parse_relative_path(value: object, name: str, source: str) -> str:
    """A file inside the server's data folder (relative, no '..'), or '' when not given."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise CatalogError(f"{source}: {name} must be text")
    pp = PurePosixPath(value.strip())
    if (not value.strip() or pp.is_absolute() or ".." in pp.parts or "\\" in value or any(c in value for c in "\0\n\r")):
        raise CatalogError(f"{source}: {name} must be a file inside the server's data folder "
                           f"(relative, no '..'): {value!r}")
    return str(pp)


def _parse_steam(data: dict, source: str) -> tuple[str, str, str, str]:
    """(steam_app_id, steam_branch, steam_manifest_dir, game_update_method) with the default method filled in."""
    raw = data.get("steam_app_id")
    app_id = "" if raw in (None, "") else str(raw).strip()
    if isinstance(raw, bool) or (app_id and not APP_ID_RE.match(app_id)):
        raise CatalogError(f"{source}: steam_app_id must be a number such as 2394010 (got {raw!r})")
    branch = str(data.get("steam_branch") or "").strip()
    if branch and not BRANCH_RE.match(branch):
        raise CatalogError(f"{source}: steam_branch must be a plain branch name such as public or beta (got {branch!r})")
    mdir = str(data.get("steam_manifest_dir") or "").strip()
    if mdir:
        pp = PurePosixPath(mdir)
        if pp.is_absolute() or ".." in pp.parts or "\\" in mdir or "\0" in mdir or "\n" in mdir:
            raise CatalogError(f"{source}: steam_manifest_dir must be a folder inside the server's data folder "
                               f"(relative, no '..'): {mdir!r}")
        mdir = str(pp)
    if (branch or mdir) and not app_id:
        raise CatalogError(f"{source}: steam_branch and steam_manifest_dir only make sense together with steam_app_id")
    method = data.get("game_update_method")
    if method in (None, ""):
        method = "restart" if app_id else "none"
    method = str(method)
    if method not in GAME_UPDATE_METHODS:
        raise CatalogError(f"{source}: game_update_method must be one of {GAME_UPDATE_METHODS}, got {method!r}")
    if method == "restart" and not app_id:
        raise CatalogError(f"{source}: game_update_method 'restart' needs steam_app_id (GameDock must know which game "
                           "version to look for)")
    return app_id, branch, mdir, method


# ------------------------------------------------------------------ the schema (single source of truth for the keys)
# Every key a catalog file may contain. docs/ADDING_A_GAME.md and the `get_add_game_guide` tool are generated from
# gamedock/authoring.py, and a test checks that its field reference names exactly these keys.
ENTRY_KEYS = ("id", "name", "image", "description", "notes", "ports", "volumes", "env", "fields", "config_files",
              "update_method", "update_note", "backup_stop_first", "save_command", "ready_log_pattern",
              "progress_log_pattern", "steam_app_id", "steam_branch", "steam_manifest_dir", "version_log_pattern",
              "game_update_method", "installed_build_file", "stop_timeout", "fail_log_patterns", "log_files", "builtin_fail_patterns")
PORT_KEYS = ("protocol", "host", "container")
VOLUME_KEYS = ("host", "container")
FIELD_KEYS = ("name", "label", "default", "secret", "required", "description", "pattern", "pattern_hint")
FAIL_PATTERN_KEYS = ("pattern", "title", "advice")
CONFIG_FILE_KEYS = ("path", "label", "format", "sensitive_keys", "hot_edit", "note")

# Things a catalog file must never ask for. GameDock always creates plain, unprivileged containers on Docker's
# default network, so these keys do nothing; they are rejected (for every entry, not only AI-added ones) so nobody
# believes they work and so a copied docker-compose file cannot quietly ask for more power than it gets.
FORBIDDEN_KEYS = {
    "privileged": "privileged mode",
    "network_mode": "a special network mode (such as host networking)",
    "network": "a special network mode",
    "networks": "a special network mode",
    "devices": "device mappings",
    "device": "device mappings",
    "cap_add": "extra capabilities",
    "cap_drop": "capability changes",
    "capabilities": "extra capabilities",
    "security_opt": "security option changes",
    "pid": "sharing the host's process space",
    "ipc": "sharing the host's memory space",
    "userns_mode": "user namespace changes",
    "sysctls": "kernel setting changes",
}
MAX_DESCRIPTION = 2000
LIST_KEYS = ("ports", "volumes", "fields", "config_files", "fail_log_patterns", "log_files")
DOCKER_SOCKET_RE = re.compile(r"docker\.sock", re.I)


def unknown_keys(data: Mapping) -> list[str]:
    return sorted(str(k) for k in data if k not in ENTRY_KEYS)


def _bad_sub_keys(data: Mapping) -> list[str]:
    """Unknown keys inside ports / volumes / fields / config_files items (plain words, for strict checking)."""
    out = []
    for key, allowed in (("ports", PORT_KEYS), ("volumes", VOLUME_KEYS), ("fields", FIELD_KEYS),
                         ("config_files", CONFIG_FILE_KEYS), ("fail_log_patterns", FAIL_PATTERN_KEYS)):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items, 1):
            if isinstance(item, dict):
                extra = sorted(str(k) for k in item if k not in allowed)
                if extra:
                    out.append(f"'{key}' item {i} has keys GameDock does not know: {', '.join(extra)} "
                               f"(allowed: {', '.join(allowed)})")
    return out


def _absolute_like(path: str) -> bool:
    p = path.strip()
    return p.startswith(("/", "~", "\\")) or bool(re.match(r"^[A-Za-z]:", p)) or "$" in p


def safety_problems(data: Mapping, origin: str = "repo") -> list[str]:
    """Every safety rule problem in a catalog definition, in plain words (empty list = fine).

    For every entry: no privileged mode, host networking, device mappings, extra capabilities or similar keys; no
    Docker socket mount. For entries added by an AI agent (origin 'mcp') also: only the keys GameDock knows, host
    folders must be relative (kept under the server's own data folder) and host ports must be 1024 or higher."""
    out: list[str] = []
    for k in data:
        if str(k) in FORBIDDEN_KEYS:
            out.append(f"'{k}' asks for {FORBIDDEN_KEYS[str(k)]}. GameDock always creates plain, unprivileged "
                       "containers and does not support this; remove the key.")
    strict = origin == "mcp"
    volumes = data.get("volumes") if isinstance(data.get("volumes"), list) else []
    for v in volumes:
        if not isinstance(v, dict):
            continue
        host, cont = str(v.get("host", "")), str(v.get("container", ""))
        if DOCKER_SOCKET_RE.search(host) or DOCKER_SOCKET_RE.search(cont):
            out.append("A volume mounts the Docker socket (docker.sock). That would give the game full control of the "
                       "machine, so it is never allowed.")
        elif strict and _absolute_like(host):
            out.append(f"Volume host folder {host!r} is not relative. Entries added by an AI agent may only use "
                       "relative folders such as ./saves, which are kept inside the server's own data folder.")
    if strict:
        extra = unknown_keys(data)
        if extra:
            out.append("The entry has keys GameDock does not know: " + ", ".join(extra)
                       + ". Only the documented keys are accepted (see get_add_game_guide).")
        out.extend(_bad_sub_keys(data))
        ports = data.get("ports") if isinstance(data.get("ports"), list) else []
        for p in ports:
            try:
                host = int(p["host"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < host < 1024:
                out.append(f"Host port {host} is below 1024. Entries added by an AI agent must use host ports "
                           "1024 or higher.")
    return out


def parse_definition(data: object, source: str = "<definition>", origin: str = "repo") -> CatalogEntry:
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: expected a mapping at top level")
    problems = safety_problems(data, origin)
    if problems:
        raise CatalogError(f"{source}: " + " ".join(problems))
    for key in LIST_KEYS:
        if data.get(key) not in (None, "") and not isinstance(data[key], list):
            raise CatalogError(f"{source}: '{key}' must be a list")
    for key in ("name", "image", "description", "notes", "update_note"):
        if data.get(key) is not None and not isinstance(data[key], (str, int, float)):
            raise CatalogError(f"{source}: '{key}' must be text")
    for key in ("name", "description", "notes", "update_note"):
        if len(str(data.get(key) or "")) > MAX_DESCRIPTION:
            raise CatalogError(f"{source}: '{key}' is longer than {MAX_DESCRIPTION} characters")
    for key in ("id", "name", "image"):
        if not data.get(key) or not isinstance(data[key], str):
            raise CatalogError(f"{source}: missing or invalid '{key}'")
    if not IMAGE_RE.match(data["image"]) or ".." in data["image"] or "//" in data["image"]:
        raise CatalogError(f"{source}: image {data['image']!r} is not a valid image name "
                           "(letters, digits and . _ - / : @ only, no spaces)")
    gid = data["id"]
    if not ID_RE.match(gid):
        raise CatalogError(f"{source}: id '{gid}' must be lowercase letters, digits, '-' or '_'")

    ports = [_parse_port(p, source) for p in data.get("ports") or []]
    seen = set()
    for p in ports:
        if (p.protocol, p.container) in seen:
            raise CatalogError(f"{source}: duplicate container port {p.container}/{p.protocol}")
        seen.add((p.protocol, p.container))

    volumes = []
    for v in data.get("volumes") or []:
        try:
            host, cont = str(v["host"]), str(v["container"])
        except (KeyError, TypeError) as e:
            raise CatalogError(f"{source}: bad volume entry {v!r}") from e
        if ".." in Path(host).parts:
            raise CatalogError(f"{source}: volume host path may not contain '..': {host!r}")
        if not host.strip() or "\0" in host or "\n" in host:
            raise CatalogError(f"{source}: bad volume host path {host!r}")
        if not cont.startswith("/") or "\0" in cont or "\n" in cont or ".." in PurePosixPath(cont).parts:
            raise CatalogError(f"{source}: volume container path must be absolute (start with /): {cont!r}")
        volumes.append(VolumeMapping(host, cont))

    env = data.get("env") or {}
    if not isinstance(env, dict):
        raise CatalogError(f"{source}: 'env' must be a mapping")
    for k, v in env.items():
        if not ENV_NAME_RE.match(str(k)):
            raise CatalogError(f"{source}: bad name in 'env': {k!r}")
        if is_reserved_env_name(str(k)):
            raise CatalogError(f"{source}: '{k}' in 'env' is a reserved name and cannot be used")
        if any(c in str(v) for c in "\n\r\0"):
            raise CatalogError(f"{source}: value of '{k}' in 'env' may not contain line breaks")
    fields = [_parse_field(f, source) for f in data.get("fields") or []]
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise CatalogError(f"{source}: duplicate env field name")
    clash = set(names) & {str(k) for k in env}
    if clash:
        raise CatalogError(f"{source}: {sorted(clash)} listed in both 'env' and 'fields'")

    update_method = str(data.get("update_method", "pull_recreate"))
    if update_method not in UPDATE_METHODS:
        raise CatalogError(f"{source}: update_method must be one of {UPDATE_METHODS}, got {update_method!r}")
    config_files = [_parse_config_file(c, source) for c in data.get("config_files") or []]
    if len({c.path for c in config_files}) != len(config_files):
        raise CatalogError(f"{source}: duplicate config file path")

    ready_pattern = _parse_pattern(data.get("ready_log_pattern"), "ready_log_pattern", source, groups=None)
    progress_pattern = _parse_pattern(data.get("progress_log_pattern"), "progress_log_pattern", source, groups=1)

    update_note = str(data.get("update_note") or "").strip()
    stop_first = data.get("backup_stop_first", False)
    if not isinstance(stop_first, bool):
        raise CatalogError(f"{source}: backup_stop_first must be true or false")
    save_command = _parse_save_command(data.get("save_command"), source)
    steam_app_id, steam_branch, steam_manifest_dir, game_update_method = _parse_steam(data, source)
    version_pattern = _parse_pattern(data.get("version_log_pattern"), "version_log_pattern", source, groups=1)
    installed_file = _parse_relative_path(data.get("installed_build_file"), "installed_build_file", source)
    stop_timeout = data.get("stop_timeout", 0)
    if stop_timeout in (None, ""):
        stop_timeout = 0
    if isinstance(stop_timeout, bool) or not isinstance(stop_timeout, int) or not (0 <= stop_timeout <= 600):
        raise CatalogError(f"{source}: stop_timeout must be a whole number of seconds from 1 to 600 (or left out)")

    fail_patterns = _parse_fail_patterns(data.get("fail_log_patterns"), source)
    log_files = _parse_log_files(data.get("log_files"), source)
    builtin = data.get("builtin_fail_patterns", True)
    if builtin is None:
        builtin = True
    if not isinstance(builtin, bool):
        raise CatalogError(f"{source}: builtin_fail_patterns must be true or false")

    return CatalogEntry(
        id=gid,
        name=data["name"],
        image=data["image"],
        ports=tuple(ports),
        volumes=tuple(volumes),
        env={str(k): str(v) for k, v in env.items()},
        fields=tuple(fields),
        description=str(data.get("description", "")),
        notes=str(data.get("notes", "")),
        update_method=update_method,
        config_files=tuple(config_files),
        ready_log_pattern=ready_pattern,
        progress_log_pattern=progress_pattern,
        update_note=update_note,
        backup_stop_first=stop_first,
        save_command=save_command,
        steam_app_id=steam_app_id,
        steam_branch=steam_branch,
        steam_manifest_dir=steam_manifest_dir,
        version_log_pattern=version_pattern,
        game_update_method=game_update_method,
        installed_build_file=installed_file,
        stop_timeout=stop_timeout,
        fail_log_patterns=fail_patterns,
        log_files=log_files,
        builtin_fail_patterns=builtin,
        origin=origin,
    )


def entry_to_dict(e: CatalogEntry) -> dict:
    """Inverse of parse_definition (same shape as the YAML file)."""
    return {
        "id": e.id,
        "name": e.name,
        "image": e.image,
        "description": e.description,
        "notes": e.notes,
        "update_method": e.update_method,
        "ready_log_pattern": e.ready_log_pattern,
        "progress_log_pattern": e.progress_log_pattern,
        "update_note": e.update_note,
        "backup_stop_first": e.backup_stop_first,
        "save_command": list(e.save_command),
        "steam_app_id": e.steam_app_id,
        "steam_branch": e.steam_branch,
        "steam_manifest_dir": e.steam_manifest_dir,
        "version_log_pattern": e.version_log_pattern,
        "game_update_method": e.game_update_method,
        "installed_build_file": e.installed_build_file,
        "stop_timeout": e.stop_timeout,
        "fail_log_patterns": [{"pattern": f.pattern, "title": f.title, "advice": f.advice} for f in e.fail_log_patterns],
        "log_files": list(e.log_files),
        "builtin_fail_patterns": e.builtin_fail_patterns,
        "config_files": [
            {"path": c.path, "label": c.label, "format": c.format,
             "sensitive_keys": list(c.sensitive_keys), "hot_edit": c.hot_edit, "note": c.note}
            for c in e.config_files
        ],
        "ports": [{"protocol": p.protocol, "host": p.host, "container": p.container} for p in e.ports],
        "volumes": [{"host": v.host, "container": v.container} for v in e.volumes],
        "env": dict(e.env),
        "fields": [
            {"name": f.name, "label": f.label, "default": f.default, "secret": f.secret,
             "required": f.required, "description": f.description, "pattern": f.pattern,
             "pattern_hint": f.pattern_hint}
            for f in e.fields
        ],
    }


def load_catalog(directory: Path, errors: list[tuple[str, str]] | None = None,
                 added_by_ai: Mapping[str, Mapping] | None = None) -> dict[str, CatalogEntry]:
    """Return {id: entry} for every *.yaml/*.yml/*.json file in directory. Files whose name starts with '_' (such as
    the _template.yaml starting point) are ignored.

    By default the first bad file raises CatalogError. If `errors` is given, bad files are skipped and
    (file name, problem) is appended to it instead, so one typo cannot hide every game.

    `added_by_ai` is the review record (see gamedock/authoring.py): {entry id: record}. An entry whose id is in it was
    added by an AI agent, so it is held to the stricter rules (origin 'mcp') and counts as `reviewed` only when a
    person approved exactly the bytes that are in the file now.
    """
    defs: dict[str, CatalogEntry] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return defs
    records = added_by_ai or {}
    files = sorted(f for f in [*directory.glob("*.yaml"), *directory.glob("*.yml"), *directory.glob("*.json")]
                   if not f.name.startswith("_") and not f.name.startswith("."))
    for f in files:
        try:
            try:
                raw = f.read_bytes()
                data = yaml.safe_load(raw.decode("utf-8"))  # JSON is valid YAML
            except yaml.YAMLError as e:
                raise CatalogError(f"{f.name}: invalid YAML/JSON: {e}") from e
            except (UnicodeDecodeError, OSError) as e:
                raise CatalogError(f"{f.name}: cannot read the file as UTF-8 text: {e}") from e
            rec = records.get(str(data.get("id"))) if isinstance(data, dict) else None
            d = parse_definition(data, f.name, origin="mcp" if rec else "repo")
            digest = hashlib.sha256(raw).hexdigest()
            reviewed = True
            if rec:
                reviewed = bool(rec.get("reviewed")) and rec.get("reviewed_sha256") == digest
            d = replace(d, reviewed=reviewed, source_file=f.name, file_sha256=digest)
            if d.id in defs:
                raise CatalogError(f"{f.name}: duplicate game id '{d.id}'")
        except CatalogError as e:
            if errors is None:
                raise
            errors.append((f.name, str(e)))
            continue
        defs[d.id] = d
    return defs
