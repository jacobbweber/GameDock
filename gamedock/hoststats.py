"""Host totals (CPU, memory, load, disk) from /proc and shutil. No extra dependencies; fields are None
where the platform does not provide them (e.g. no /proc)."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .models import HostStats


def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """First 'cpu' line of /proc/stat -> (total jiffies, idle jiffies)."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:]]
            if len(parts) < 4:
                return None
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
            return sum(parts[:8]), idle
    return None


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo -> {field: bytes}."""
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        bits = rest.split()
        if bits and bits[0].isdigit():
            out[key] = int(bits[0]) * (1024 if len(bits) > 1 and bits[1].lower() == "kb" else 1)
    return out


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


class HostMeter:
    """Reads host stats; keeps the previous CPU counters so CPU percent is the change between two reads."""

    def __init__(self) -> None:
        self._prev: tuple[int, int] | None = None

    def _cpu(self) -> float | None:
        text = _read("/proc/stat")
        cur = parse_proc_stat(text) if text else None
        if cur is None:
            return None
        if self._prev is None:  # first read: measure over a short moment
            self._prev = cur
            time.sleep(0.1)
            text = _read("/proc/stat")
            cur = parse_proc_stat(text) if text else cur
        d_total, d_idle = cur[0] - self._prev[0], cur[1] - self._prev[1]
        self._prev = cur
        if d_total <= 0:
            return 0.0
        return round(max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total)), 1)

    def read(self, data_dir: Path) -> HostStats:
        mem_text = _read("/proc/meminfo")
        mem = parse_meminfo(mem_text) if mem_text else {}
        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        try:
            load1, load5, load15 = os.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = None
        up = _read("/proc/uptime")
        path = Path(data_dir)
        while not path.exists() and path != path.parent:
            path = path.parent
        du = shutil.disk_usage(path)
        return HostStats(
            cpu_percent=self._cpu(), cpu_count=os.cpu_count() or 1,
            mem_total=total, mem_used=(total - avail) if total is not None and avail is not None else None,
            mem_available=avail, load1=load1, load5=load5, load15=load15,
            disk_total=du.total, disk_used=du.used, disk_free=du.free,
            uptime_seconds=float(up.split()[0]) if up else None)
