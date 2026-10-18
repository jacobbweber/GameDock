"""Load catalog entries (game templates) from YAML/JSON files in the catalog directory."""
from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath

import yaml

from .errors import CatalogError
from .steam import APP_ID_RE, BRANCH_RE
from .models import (
    CONFIG_FORMATS, ENV_NAME_RE, GAME_UPDATE_METHODS, ID_RE, IMAGE_RE, UPDATE_METHODS, CatalogEntry, ConfigFile, EnvField, PortMapping,
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
    return EnvField(
        name=f["name"],
        label=str(f.get("label") or f["name"]),
        default="" if f.get("default") is None else str(f["default"]),
        secret=bool(f.get("secret", False)),
        required=bool(f.get("required", False)),
        description=str(f.get("description", "")),
    )


KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


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


def parse_definition(data: object, source: str = "<definition>") -> CatalogEntry:
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: expected a mapping at top level")
    for key in ("id", "name", "image"):
        if not data.get(key) or not isinstance(data[key], str):
            raise CatalogError(f"{source}: missing or invalid '{key}'")
    if not IMAGE_RE.match(data["image"]):
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
             "required": f.required, "description": f.description}
            for f in e.fields
        ],
    }


def load_catalog(directory: Path, errors: list[tuple[str, str]] | None = None) -> dict[str, CatalogEntry]:
    """Return {id: entry} for every *.yaml/*.yml/*.json file in directory.

    By default the first bad file raises CatalogError. If `errors` is given, bad files are skipped and
    (file name, problem) is appended to it instead, so one typo cannot hide every game.
    """
    defs: dict[str, CatalogEntry] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return defs
    files = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml"), *directory.glob("*.json")])
    for f in files:
        try:
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))  # JSON is valid YAML
            except yaml.YAMLError as e:
                raise CatalogError(f"{f.name}: invalid YAML/JSON: {e}") from e
            except (UnicodeDecodeError, OSError) as e:
                raise CatalogError(f"{f.name}: cannot read the file as UTF-8 text: {e}") from e
            d = parse_definition(data, f.name)
            if d.id in defs:
                raise CatalogError(f"{f.name}: duplicate game id '{d.id}'")
        except CatalogError as e:
            if errors is None:
                raise
            errors.append((f.name, str(e)))
            continue
        defs[d.id] = d
    return defs
