"""Load catalog entries (game templates) from YAML/JSON files in the catalog directory."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import yaml

from .errors import CatalogError
from .models import (
    CONFIG_FORMATS, ENV_NAME_RE, ID_RE, UPDATE_METHODS, CatalogEntry, ConfigFile, EnvField, PortMapping,
    VolumeMapping,
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
                      sensitive_keys=tuple(keys), hot_edit=bool(c.get("hot_edit", False)))


def parse_definition(data: object, source: str = "<definition>") -> CatalogEntry:
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: expected a mapping at top level")
    for key in ("id", "name", "image"):
        if not data.get(key) or not isinstance(data[key], str):
            raise CatalogError(f"{source}: missing or invalid '{key}'")
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
        volumes.append(VolumeMapping(host, cont))

    env = data.get("env") or {}
    if not isinstance(env, dict):
        raise CatalogError(f"{source}: 'env' must be a mapping")
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
        "config_files": [
            {"path": c.path, "label": c.label, "format": c.format,
             "sensitive_keys": list(c.sensitive_keys), "hot_edit": c.hot_edit}
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


def load_catalog(directory: Path) -> dict[str, CatalogEntry]:
    """Return {id: entry} for every *.yaml/*.yml/*.json file in directory."""
    defs: dict[str, CatalogEntry] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return defs
    files = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml"), *directory.glob("*.json")])
    for f in files:
        try:
            data = yaml.safe_load(f.read_text())  # JSON is valid YAML
        except yaml.YAMLError as e:
            raise CatalogError(f"{f.name}: invalid YAML/JSON: {e}") from e
        d = parse_definition(data, f.name)
        if d.id in defs:
            raise CatalogError(f"{f.name}: duplicate game id '{d.id}'")
        defs[d.id] = d
    return defs
