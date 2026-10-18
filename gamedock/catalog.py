"""Load game definitions from YAML files in the catalog directory."""
from __future__ import annotations

from pathlib import Path

import yaml

from .errors import CatalogError
from .models import ID_RE, GameDefinition, PortMapping, VolumeMapping


def parse_definition(data: object, source: str = "<definition>") -> GameDefinition:
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: expected a mapping at top level")
    for key in ("id", "name", "image"):
        if not data.get(key) or not isinstance(data[key], str):
            raise CatalogError(f"{source}: missing or invalid '{key}'")
    gid = data["id"]
    if not ID_RE.match(gid):
        raise CatalogError(f"{source}: id '{gid}' must be lowercase letters, digits, '-' or '_'")

    ports = []
    for p in data.get("ports") or []:
        try:
            proto = str(p["protocol"]).lower()
            host, cont = int(p["host"]), int(p["container"])
        except (KeyError, TypeError, ValueError) as e:
            raise CatalogError(f"{source}: bad port entry {p!r}") from e
        if proto not in ("tcp", "udp"):
            raise CatalogError(f"{source}: port protocol must be tcp or udp, got {proto!r}")
        if not (1 <= host <= 65535 and 1 <= cont <= 65535):
            raise CatalogError(f"{source}: port out of range in {p!r}")
        ports.append(PortMapping(proto, host, cont))

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

    return GameDefinition(
        id=gid,
        name=data["name"],
        image=data["image"],
        ports=tuple(ports),
        volumes=tuple(volumes),
        env={str(k): str(v) for k, v in env.items()},
        description=str(data.get("description", "")),
    )


def load_catalog(directory: Path) -> dict[str, GameDefinition]:
    """Return {id: definition} for every *.yaml/*.yml/*.json file in directory."""
    defs: dict[str, GameDefinition] = {}
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
