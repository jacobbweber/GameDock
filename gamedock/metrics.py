"""Prometheus text exposition (format 0.0.4), written by hand so no client library is needed."""
from __future__ import annotations

from . import __version__
from .models import StatsSnapshot


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**kw: str) -> str:
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in kw.items()) + "}"


def render_metrics(snap: StatsSnapshot) -> str:
    lines: list[str] = []

    def metric(name: str, help_: str, kind: str, samples: list[tuple[str, float | int]]) -> None:
        samples = [(l, v) for l, v in samples if v is not None]
        if not samples:
            return
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value:g}" if isinstance(value, float) else f"{name}{labels} {value}")

    def per_server(attr, scale=1):
        return [(_labels(server=s.server_id, game=s.game_id), getattr(s, attr) * scale) for s in snap.servers]

    def per_running(attr):
        return [(_labels(server=s.server_id, game=s.game_id), getattr(s, attr)) for s in snap.servers
                if s.running and getattr(s, attr) is not None]

    metric("gamedock_build_info", "GameDock version.", "gauge", [(_labels(version=__version__), 1)])
    metric("gamedock_servers", "Number of deployed servers.", "gauge", [("", len(snap.servers))])
    metric("gamedock_server_running", "1 if the server's container is running.", "gauge",
           [(_labels(server=s.server_id, game=s.game_id), 1 if s.running else 0) for s in snap.servers])
    metric("gamedock_server_cpu_percent", "Server CPU use, 100 = one full core.", "gauge", per_server("cpu_percent"))
    metric("gamedock_server_memory_bytes", "Server memory in use.", "gauge", per_server("mem_used"))
    metric("gamedock_server_memory_limit_bytes", "Server memory limit.", "gauge", per_running("mem_limit"))
    metric("gamedock_server_network_receive_bytes_total", "Bytes received since the container started.", "counter",
           per_server("net_rx"))
    metric("gamedock_server_network_transmit_bytes_total", "Bytes sent since the container started.", "counter",
           per_server("net_tx"))
    metric("gamedock_server_block_read_bytes_total", "Bytes read from disk since the container started.", "counter",
           per_server("block_read"))
    metric("gamedock_server_block_write_bytes_total", "Bytes written to disk since the container started.", "counter",
           per_server("block_write"))
    metric("gamedock_server_uptime_seconds", "Seconds since the container started.", "gauge", per_running("uptime_seconds"))
    h = snap.host
    for name, help_, val in [
        ("gamedock_host_cpu_percent", "Whole-machine CPU use, 0-100.", h.cpu_percent),
        ("gamedock_host_cpu_count", "Number of CPU cores.", h.cpu_count),
        ("gamedock_host_memory_total_bytes", "Host memory total.", h.mem_total),
        ("gamedock_host_memory_used_bytes", "Host memory in use.", h.mem_used),
        ("gamedock_host_disk_total_bytes", "Size of the volume holding GameDock data.", h.disk_total),
        ("gamedock_host_disk_free_bytes", "Free space on the volume holding GameDock data.", h.disk_free),
        ("gamedock_host_load1", "Host 1-minute load average.", h.load1),
    ]:
        metric(name, help_, "gauge", [("", val)] if val is not None else [])
    return "\n".join(lines) + "\n"
