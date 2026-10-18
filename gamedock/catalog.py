"""Load catalog entries (game templates) from YAML/JSON files in the catalog directory."""
from __future__ import annotations

from pathlib import Path

import yaml

from .errors import CatalogError
from .models import ENV_NAME_RE, ID_RE, CatalogEntry, EnvField, PortMapping, VolumeMapping


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
    )


def entry_to_dict(e: CatalogEntry) -> dict:
    """Inverse of parse_definition (same shape as the YAML file)."""
    return {
        "id": e.id,
        "name": e.name,
        "image": e.image,
        "description": e.description,
        "notes": e.notes,
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
