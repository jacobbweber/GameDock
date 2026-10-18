"""Host port conflicts, in plain words (pure logic; the service supplies the facts).

Three different things can collide with a port a server wants on this machine:

  gamedock_server  another GameDock server already has that host port and protocol (running or not). Deploying is
                   refused for this one.
  host_listener    some other program or container on this machine is listening on it right now (found by trying to
                   bind it). The server would fail to start.
  catalog_default  another game in the catalog uses the same port number as its DEFAULT. Harmless until both are
                   deployed with defaults; it is shown so the second one can be given another port in time.

Each finding carries a `level` (conflict = will fail, warn = probably a problem, info = good to know) and, where it
helps, a free alternative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

CONFLICT, WARN, INFO = "conflict", "warn", "info"
SEARCH_SPAN = 400


@dataclass(frozen=True)
class Wanted:
    protocol: str
    host: int


@dataclass
class Finding:
    protocol: str
    host: int
    level: str
    kind: str          # gamedock_server | host_listener | catalog_default | duplicate | invalid
    message: str
    with_what: str = ""
    suggestion: int | None = None

    def to_dict(self) -> dict:
        return {"protocol": self.protocol, "host": self.host, "level": self.level, "kind": self.kind,
                "message": self.message, "with": self.with_what, "suggested_host_port": self.suggestion}


@dataclass
class PortReport:
    findings: list[Finding] = field(default_factory=list)
    host_check: bool = True      # False = this machine's listening ports could not be checked (no probe)

    @property
    def ok(self) -> bool:
        return not any(f.level == CONFLICT for f in self.findings)

    def to_dict(self, wanted: Iterable[Wanted]) -> dict:
        wanted = list(wanted)
        by_port: dict[tuple[str, int], list[Finding]] = {}
        for f in self.findings:
            by_port.setdefault((f.protocol, f.host), []).append(f)
        ports = []
        for w in wanted:
            fs = by_port.get((w.protocol, w.host), [])
            worst = CONFLICT if any(f.level == CONFLICT for f in fs) else WARN if any(f.level == WARN for f in fs) \
                else INFO if fs else "free"
            ports.append({"protocol": w.protocol, "host": w.host, "status": worst,
                          "suggested_host_port": next((f.suggestion for f in fs if f.suggestion), None)})
        conflicts = [f for f in self.findings if f.level == CONFLICT]
        warns = [f for f in self.findings if f.level == WARN]
        if conflicts:
            summary = f"{len(conflicts)} port conflict(s): " + " ".join(f.message for f in conflicts)
        elif warns:
            summary = "No hard conflicts, but: " + " ".join(f.message for f in warns)
        elif self.findings:
            summary = "No conflicts. Notes: " + " ".join(f.message for f in self.findings)
        else:
            summary = "All of these host ports look free."
        if not self.host_check:
            summary += " (Programs listening on this machine could not be checked with this container engine.)"
        return {"ok": self.ok, "ports": ports, "findings": [f.to_dict() for f in self.findings],
                "host_check_available": self.host_check, "summary": summary}


def suggest_free(protocol: str, host: int, taken: Callable[[str, int], bool]) -> int | None:
    """The next host port above `host` (then below) for which `taken(protocol, port)` is False."""
    for cand in list(range(host + 1, min(65535, host + SEARCH_SPAN) + 1)) + list(range(host - 1, max(1024, host - 50), -1)):
        if 1 <= cand <= 65535 and not taken(protocol, cand):
            return cand
    return None


def find_conflicts(wanted: list[Wanted], *, servers: list[tuple[str, str, set[tuple[str, int]], bool]],
                   catalog_defaults: list[tuple[str, str, set[tuple[str, int]]]],
                   probe: Callable[[str, int], bool | None] | None,
                   own_running: set[tuple[str, int]] = frozenset()) -> PortReport:
    """`servers`: (id, name, {(protocol, host)}, running) of the GameDock servers that count (the one being edited is
    left out by the caller). `catalog_defaults`: (id, name, {(protocol, host)}) of the OTHER catalog games.
    `probe(protocol, port)`: False = something listens there, None/True = free or unknown; None disables the check.
    `own_running`: ports held by the server being checked itself while it runs (they are expected to be bound)."""
    report = PortReport(host_check=probe is not None)
    used_by_servers = {(proto, host) for _, _, ports, _ in servers for proto, host in ports}
    defaults = {(proto, host) for _, _, ports in catalog_defaults for proto, host in ports}
    asked = {(w.protocol, w.host) for w in wanted}

    def taken(protocol: str, port: int) -> bool:
        if (protocol, port) in used_by_servers or (protocol, port) in asked or (protocol, port) in defaults:
            return True
        return bool(probe and (protocol, port) not in own_running and probe(protocol, port) is False)

    seen: set[tuple[str, int]] = set()
    for w in wanted:
        key = (w.protocol, w.host)
        if not isinstance(w.host, int) or not 1 <= w.host <= 65535:
            report.findings.append(Finding(w.protocol, w.host, CONFLICT, "invalid",
                                           f"{w.host} is not a valid port number (use 1 to 65535)."))
            continue
        if key in seen:
            report.findings.append(Finding(w.protocol, w.host, CONFLICT, "duplicate",
                                           f"Host port {w.host}/{w.protocol} is used twice in the same server.",
                                           suggestion=suggest_free(w.protocol, w.host, taken)))
            continue
        seen.add(key)
        hit = False
        for sid, name, ports, running in servers:
            if key in ports:
                hit = True
                state = "running" if running else "not running"
                report.findings.append(Finding(
                    w.protocol, w.host, CONFLICT, "gamedock_server",
                    f"Host port {w.host}/{w.protocol} is already used by your server '{name}' (a GameDock server, {state}). "
                    "Two servers cannot share a host port.", f"{name} ({sid})", suggest_free(w.protocol, w.host, taken)))
                break
        if not hit and probe is not None and key not in own_running and probe(w.protocol, w.host) is False:
            hit = True
            report.findings.append(Finding(
                w.protocol, w.host, CONFLICT, "host_listener",
                f"Host port {w.host}/{w.protocol} is in use on this machine by another program or container, so a "
                f"server on it could not start (find it with: sudo ss -lntup | grep :{w.host}).", "this machine",
                suggest_free(w.protocol, w.host, taken)))
        if not hit:
            for gid, name, ports in catalog_defaults:
                if key in ports:
                    report.findings.append(Finding(
                        w.protocol, w.host, INFO, "catalog_default",
                        f"Host port {w.host}/{w.protocol} is also the default port of the game '{name}' ({gid}). That is "
                        "fine until both are deployed with their defaults: give one of them another host port.",
                        f"{name} ({gid})", suggest_free(w.protocol, w.host, taken)))
                    break
    return report


def default_collisions(entry_id: str, entry_ports: Iterable[tuple[str, int]],
                       others: Iterable[tuple[str, str, set[tuple[str, int]]]]) -> list[tuple[str, int, str, str]]:
    """Defaults of one catalog entry that are also another game's default: (protocol, host, other id, other name)."""
    out = []
    for proto, host in entry_ports:
        for gid, name, ports in others:
            if gid != entry_id and (proto, host) in ports:
                out.append((proto, host, gid, name))
                break
    return out
