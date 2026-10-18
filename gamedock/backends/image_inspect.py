"""Turning what `docker image inspect`, `docker buildx imagetools inspect` and `docker manifest inspect` print into a
small, bounded, plain dictionary. Pure functions: no commands are run here (see DockerCliBackend.inspect_image)."""
from __future__ import annotations

import json
import re
from typing import Any

MAX_ITEMS = 60
MAX_TEXT = 300
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _text(v: Any) -> str:
    return str(v if v is not None else "")[:MAX_TEXT]


def _list(v: Any) -> list:
    return list(v) if isinstance(v, (list, tuple)) else []


def parse_local_inspect(text: str) -> dict | None:
    """`docker image inspect <ref>` JSON (a list with one object) -> the facts that help write a catalog entry.
    Environment variable VALUES are left out on purpose (only names). None if the text cannot be understood."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    cfg = data.get("Config") if isinstance(data.get("Config"), dict) else {}
    env_names = []
    for item in _list(cfg.get("Env")):
        name = str(item).split("=", 1)[0]
        if name and name not in env_names:
            env_names.append(name[:128])

    def port_key(p: str):
        m = re.match(r"^(\d+)/(tcp|udp)$", p)
        return (int(m.group(1)), m.group(2)) if m else (10**6, p)

    ports = sorted((str(p) for p in (cfg.get("ExposedPorts") or {})), key=port_key)
    volumes = sorted(str(v) for v in (cfg.get("Volumes") or {}))
    health = cfg.get("Healthcheck") if isinstance(cfg.get("Healthcheck"), dict) else None
    hc = None
    if health and health.get("Test") and health.get("Test") != ["NONE"]:
        hc = {"test": [_text(x) for x in _list(health.get("Test"))][:8],
              "interval_seconds": _seconds(health.get("Interval")),
              "start_period_seconds": _seconds(health.get("StartPeriod")),
              "retries": health.get("Retries")}
    return {
        "id": _text(data.get("Id")),
        "digests": [_text(d) for d in _list(data.get("RepoDigests"))][:10],
        "tags": [_text(t) for t in _list(data.get("RepoTags"))][:10],
        "created": _text(data.get("Created")),
        "os": _text(data.get("Os")),
        "architecture": _text(data.get("Architecture")),
        "size_bytes": data.get("Size") if isinstance(data.get("Size"), int) else None,
        "user": _text(cfg.get("User")) or "(not set: runs as root)",
        "exposed_ports": ports[:MAX_ITEMS],
        "volumes": volumes[:MAX_ITEMS],
        "env_names": env_names[:MAX_ITEMS * 2],
        "healthcheck": hc,
        "entrypoint": [_text(x) for x in _list(cfg.get("Entrypoint"))][:8],
        "cmd": [_text(x) for x in _list(cfg.get("Cmd"))][:8],
        "working_dir": _text(cfg.get("WorkingDir")),
    }


def _seconds(ns: Any) -> float | None:
    return round(ns / 1e9, 1) if isinstance(ns, (int, float)) and ns > 0 else None


def parse_imagetools(text: str) -> dict:
    """`docker buildx imagetools inspect <ref>` output -> {"digest", "platforms"}."""
    digest = ""
    m = re.search(r"^Digest:\s*(sha256:[0-9a-f]{64})\s*$", text, re.M)
    if m:
        digest = m.group(1)
    platforms: list[str] = []
    for m in re.finditer(r"^\s*Platform:\s*(\S+)\s*$", text, re.M):
        p = m.group(1)
        if p != "unknown/unknown" and p not in platforms:
            platforms.append(p)
    return {"digest": digest, "platforms": platforms[:MAX_ITEMS]}


def parse_manifest(text: str) -> dict | None:
    """`docker manifest inspect <ref>` JSON -> {"digest", "platforms"} (digest only for a one-platform image)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    platforms: list[str] = []
    for m in _list(data.get("manifests")):
        pl = m.get("platform") if isinstance(m, dict) else None
        if isinstance(pl, dict) and pl.get("os") and pl.get("os") != "unknown":
            s = f"{pl.get('os')}/{pl.get('architecture', '?')}" + (f"/{pl['variant']}" if pl.get("variant") else "")
            if s not in platforms:
                platforms.append(s)
    digest = ""
    d = (data.get("Descriptor") or {}).get("digest") if isinstance(data.get("Descriptor"), dict) else ""
    if isinstance(d, str) and DIGEST_RE.fullmatch(d):
        digest = d
    return {"digest": digest, "platforms": platforms[:MAX_ITEMS]}
