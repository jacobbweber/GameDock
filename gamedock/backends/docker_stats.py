"""Parsing of `docker stats` / `docker inspect` output (pure functions, easy to test)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..models import ContainerStats

_UNITS = {
    "b": 1,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "pb": 1000**5,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5,
}
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")


def parse_percent(text: str) -> float:
    """'1.23%' -> 1.23. Anything unusable ('--', '') -> 0.0."""
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return 0.0


def parse_size(text: str) -> int:
    """'512MiB' -> 536870912, '1.2kB' -> 1200, '0B' -> 0. Unusable -> 0."""
    m = _SIZE_RE.match(text or "")
    if not m:
        return 0
    unit = (m.group(2) or "b").lower()
    return int(float(m.group(1)) * _UNITS.get(unit, 1))


def parse_pair(text: str) -> tuple[int, int]:
    """'512MiB / 8GiB' -> (bytes, bytes)."""
    left, _, right = (text or "").partition("/")
    return parse_size(left), parse_size(right)


def parse_stats_lines(output: str) -> dict[str, ContainerStats]:
    """Parse the output of `docker stats --no-stream --format '{{json .}}'` (one JSON object per line)."""
    result: dict[str, ContainerStats] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        name = (d.get("Name") or "").lstrip("/")
        if not name:
            continue
        mem_used, mem_limit = parse_pair(d.get("MemUsage", ""))
        rx, tx = parse_pair(d.get("NetIO", ""))
        br, bw = parse_pair(d.get("BlockIO", ""))
        try:
            pids = int(str(d.get("PIDs", "0")).strip() or 0)
        except ValueError:
            pids = 0
        result[name] = ContainerStats(
            running=True, cpu_percent=parse_percent(d.get("CPUPerc", "")), mem_used=mem_used,
            mem_limit=mem_limit, net_rx=rx, net_tx=tx, block_read=br, block_write=bw, pids=pids)
    return result


def parse_docker_time(text: str) -> datetime | None:
    """'2026-10-17T21:00:00.123456789Z' -> aware datetime. '0001-01-01T00:00:00Z' (never started) -> None."""
    text = (text or "").strip()
    if not text or text.startswith("0001-01-01"):
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", text)
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]  # keep microsecond precision at most
    tz = m.group(3)
    base = datetime.strptime(m.group(1) + frac, "%Y-%m-%dT%H:%M:%S.%f")
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        from datetime import timedelta
        return (base - sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))).replace(tzinfo=timezone.utc)
    return base.replace(tzinfo=timezone.utc)


INSPECT_FORMAT = "{{.Name}}|{{.State.Status}}|{{.State.StartedAt}}|{{.HostConfig.Memory}}"


def parse_inspect_lines(output: str) -> dict[str, tuple[str, datetime | None, int]]:
    """Parse `docker inspect --format INSPECT_FORMAT ...` -> {name: (status, started_at, memory_limit_bytes)}."""
    result = {}
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4 or not parts[0]:
            continue
        try:
            limit = int(parts[3])
        except ValueError:
            limit = 0
        result[parts[0].lstrip("/")] = (parts[1], parse_docker_time(parts[2]), limit)
    return result
