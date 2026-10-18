"""Turning `docker container inspect` output into a small, safe dictionary for troubleshooting. Pure function: nothing is
run here (see DockerCliBackend.container_details). The container's environment (which can hold passwords) is never
read out of the JSON: only state, restart count, published ports and mounts."""
from __future__ import annotations

import json
from typing import Any

MAX_ITEMS = 60
MAX_TEXT = 300


def _text(v: Any) -> str:
    return str(v if v is not None else "")[:MAX_TEXT]


def parse_container_details(text: str) -> dict | None:
    """`docker container inspect <name>` JSON (a list with one object) ->
    {"status", "exit_code", "oom_killed", "restart_count", "started_at", "finished_at", "error", "health",
     "ports": [{"container": 27015, "protocol": "udp", "host_ip": "0.0.0.0", "host_port": 27015}],
     "unpublished": ["8211/udp"], "mounts": [{"source", "target", "writable", "type"}], "image"}.
    None if the text cannot be understood."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    state = data.get("State") if isinstance(data.get("State"), dict) else {}
    health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
    ports: list[dict] = []
    unpublished: list[str] = []
    net = (data.get("NetworkSettings") or {}).get("Ports") if isinstance(data.get("NetworkSettings"), dict) else None
    if not isinstance(net, dict) or not net:
        host_cfg = data.get("HostConfig") if isinstance(data.get("HostConfig"), dict) else {}
        net = host_cfg.get("PortBindings") if isinstance(host_cfg.get("PortBindings"), dict) else {}
    for key, bindings in sorted(net.items(), key=lambda kv: str(kv[0])):
        port, _, proto = str(key).partition("/")
        if not port.isdigit():
            continue
        if not bindings:
            unpublished.append(str(key))
            continue
        for b in bindings if isinstance(bindings, list) else []:
            if isinstance(b, dict) and str(b.get("HostPort", "")).isdigit():
                ports.append({"container": int(port), "protocol": proto or "tcp", "host_ip": _text(b.get("HostIp")),
                              "host_port": int(b["HostPort"])})
    mounts = []
    for m in data.get("Mounts") if isinstance(data.get("Mounts"), list) else []:
        if isinstance(m, dict):
            mounts.append({"source": _text(m.get("Source")), "target": _text(m.get("Destination")),
                           "writable": bool(m.get("RW", True)), "type": _text(m.get("Type"))})
    code = state.get("ExitCode")
    restarts = data.get("RestartCount")
    return {
        "status": _text(state.get("Status")),
        "exit_code": code if isinstance(code, int) else None,
        "oom_killed": bool(state.get("OOMKilled")),
        "restart_count": restarts if isinstance(restarts, int) else 0,
        "started_at": _text(state.get("StartedAt")),
        "finished_at": _text(state.get("FinishedAt")),
        "error": _text(state.get("Error")),
        "health": _text(health.get("Status")),
        "ports": ports[:MAX_ITEMS],
        "unpublished": unpublished[:MAX_ITEMS],
        "mounts": mounts[:MAX_ITEMS],
        "image": _text((data.get("Config") or {}).get("Image")) if isinstance(data.get("Config"), dict) else "",
    }
