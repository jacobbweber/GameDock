"""Schedules: when backups and maintenance run, how that is worked out, and where it is stored.

Pure logic first (no Docker, no threads, no clock of its own) so it can be tested exactly:

    parse_time / validate_*   strict checks of what a person or an AI agent typed
    next_run                  the next moment a rule fires after a given moment (daily, weekly, every N hours)
    describe                  plain-language summary ("daily 03:00", "Sun 04:00", "every 6 hours")

Times of day are in the machine's own timezone (see host_timezone) and honour daylight saving: a time that does not
exist on the day the clocks jump forward runs right after the jump, and a time that happens twice runs once.

`ScheduleStore` keeps everything in <data>/schedules.json (written atomically) and in memory.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from threading import RLock

from .errors import ValidationError

log = logging.getLogger("gamedock.schedule")

FORMAT_VERSION = 1
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_EVERY_HOURS = 168          # one week
MAX_RETENTION = 100
HISTORY_LIMIT = 30             # recent runs kept per server
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
BACKUP_KINDS = ("interval", "daily", "weekly")
MAINTENANCE_KINDS = ("daily", "weekly")
RESULTS = ("ok", "failed", "skipped", "waiting")


# ----------------------------------------------------------------------------------------------- timezone
def host_timezone() -> tuple[tzinfo | None, str]:
    """(timezone object, name) of this machine. The object is None when only a fixed offset is known; then the
    system's own conversion is used (it still follows daylight saving)."""
    names = []
    env = os.environ.get("TZ", "").lstrip(":")
    if env:
        names.append(env)
    try:
        names.append(os.path.realpath("/etc/localtime").split("/zoneinfo/", 1)[1])
    except (IndexError, OSError):
        pass
    try:
        names.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    try:
        from zoneinfo import ZoneInfo
        for n in names:
            if n:
                try:
                    return ZoneInfo(n), n
                except Exception:  # noqa: BLE001 - unknown name, try the next source
                    continue
    except ImportError:
        pass
    return None, datetime.now().astimezone().tzname() or "local time"


def _local(dt_utc: datetime, tz: tzinfo | None) -> datetime:
    return dt_utc.astimezone(tz) if tz else dt_utc.astimezone()


def _local_to_utc(d: date, hour: int, minute: int, tz: tzinfo | None) -> datetime:
    """The UTC moment of a local wall-clock time. In a daylight-saving gap the time is read with the offset from
    before the jump (so 02:30 becomes 03:30); a repeated time resolves to its first occurrence."""
    naive = datetime(d.year, d.month, d.day, hour, minute)
    if tz is None:
        return naive.astimezone(timezone.utc)  # the system's own conversion treats a naive value as local time
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


# ----------------------------------------------------------------------------------------------- rules
@dataclass(frozen=True)
class Recurrence:
    kind: str                      # interval | daily | weekly
    every_hours: int = 0           # interval only
    time: str = ""                 # "HH:MM" for daily and weekly
    days: tuple[str, ...] = ()     # weekly only, in week order ("mon" ... "sun")


def parse_time(value: object) -> tuple[int, int]:
    m = TIME_RE.match(value.strip()) if isinstance(value, str) else None
    if not m:
        raise ValidationError("Time must be 24-hour hours and minutes written like 03:30 (00:00 to 23:59)")
    return int(m[1]), int(m[2])


def validate_recurrence(kind: object, every_hours: object, time_of_day: object, days: object,
                        allowed: tuple[str, ...]) -> Recurrence:
    """Strict check. Anything that does not apply to the chosen kind is ignored (not stored)."""
    if kind not in allowed:
        raise ValidationError(f"Frequency must be one of: {', '.join(allowed)}")
    if kind == "interval":
        if isinstance(every_hours, bool) or not isinstance(every_hours, int) or not 1 <= every_hours <= MAX_EVERY_HOURS:
            raise ValidationError(f"'Every how many hours' must be a whole number from 1 to {MAX_EVERY_HOURS}")
        return Recurrence("interval", every_hours=every_hours)
    h, m = parse_time(time_of_day)
    t = f"{h:02d}:{m:02d}"
    if kind == "daily":
        return Recurrence("daily", time=t)
    if (not isinstance(days, (list, tuple)) or not days
            or not all(isinstance(d, str) and d.lower() in DAY_NAMES for d in days)):
        raise ValidationError("Choose at least one weekday: " + ", ".join(DAY_NAMES))
    chosen = {d.lower() for d in days}
    return Recurrence("weekly", time=t, days=tuple(d for d in DAY_NAMES if d in chosen))


def next_run(rec: Recurrence, after: datetime, tz: tzinfo | None = None) -> datetime:
    """The first moment (UTC) strictly after `after` at which the rule fires."""
    after = after.astimezone(timezone.utc)
    if rec.kind == "interval":
        return after + timedelta(hours=rec.every_hours)
    h, m = parse_time(rec.time)
    allowed = set(range(7)) if rec.kind == "daily" else {DAY_NAMES.index(d) for d in rec.days}
    start = _local(after, tz).date() - timedelta(days=1)  # one day back covers zone changes around midnight
    for k in range(0, 16):
        d = start + timedelta(days=k)
        if d.weekday() not in allowed:
            continue
        cand = _local_to_utc(d, h, m, tz)
        if cand > after:
            return cand
    raise ValueError("no next run found")  # unreachable for validated rules


def describe(rec: Recurrence) -> str:
    """Short plain-language form used on tiles and in summaries."""
    if rec.kind == "interval":
        return "every hour" if rec.every_hours == 1 else f"every {rec.every_hours} hours"
    if rec.kind == "daily" or len(rec.days) == 7:
        return f"daily at {rec.time}"
    return f"{', '.join(DAY_LABELS[DAY_NAMES.index(d)] for d in rec.days)} at {rec.time}"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


# ----------------------------------------------------------------------------------------------- stored objects
@dataclass(frozen=True)
class RunState:
    """What happened last. `since` is when the rule was switched on or changed: the first run is worked out from it."""

    since: str = ""
    last_run: str = ""           # when the last run started (UTC ISO)
    last_result: str = ""        # ok | failed | skipped | waiting
    last_message: str = ""


@dataclass(frozen=True)
class BackupSchedule:
    enabled: bool = False
    rule: Recurrence = field(default_factory=lambda: Recurrence("daily", time="03:00"))
    retention: int | None = None   # keep this many backups; None = GameDock's normal setting
    state: RunState = field(default_factory=RunState)


@dataclass(frozen=True)
class MaintenanceSchedule:
    enabled: bool = False
    rule: Recurrence = field(default_factory=lambda: Recurrence("weekly", time="04:00", days=("sun",)))
    backup_first: bool = True
    update_image: bool = True
    restart: bool = True
    state: RunState = field(default_factory=RunState)


@dataclass(frozen=True)
class HistoryEntry:
    time: str
    kind: str       # backup | maintenance
    trigger: str    # schedule | manual | catch-up
    result: str     # ok | failed | skipped
    message: str


@dataclass(frozen=True)
class ServerSchedules:
    backup: BackupSchedule = field(default_factory=BackupSchedule)
    maintenance: MaintenanceSchedule = field(default_factory=MaintenanceSchedule)
    history: tuple[HistoryEntry, ...] = ()   # newest first


def effective_anchor(state: RunState) -> datetime | None:
    return parse_iso(state.last_run) or parse_iso(state.since)


def next_due(rule: Recurrence, state: RunState, tz: tzinfo | None) -> datetime | None:
    anchor = effective_anchor(state)
    return next_run(rule, anchor, tz) if anchor else None


# ----------------------------------------------------------------------------------------------- JSON
def _rule_to_json(r: Recurrence) -> dict:
    return {"kind": r.kind, "every_hours": r.every_hours if r.kind == "interval" else None,
            "time": r.time if r.kind != "interval" else None, "days": list(r.days) if r.kind == "weekly" else []}


def _state_to_json(s: RunState) -> dict:
    return {"since": s.since, "last_run": s.last_run, "last_result": s.last_result, "last_message": s.last_message}


def schedules_to_json(s: ServerSchedules) -> dict:
    return {
        "backup": {"enabled": s.backup.enabled, **_rule_to_json(s.backup.rule), "retention": s.backup.retention,
                   **_state_to_json(s.backup.state)},
        "maintenance": {"enabled": s.maintenance.enabled, **_rule_to_json(s.maintenance.rule),
                        "backup_first": s.maintenance.backup_first, "update_image": s.maintenance.update_image,
                        "restart": s.maintenance.restart, **_state_to_json(s.maintenance.state)},
        "history": [h.__dict__ for h in s.history],
    }


def _rule_from_json(d: dict, allowed: tuple[str, ...]) -> Recurrence:
    return validate_recurrence(d.get("kind"), d.get("every_hours"), d.get("time"), d.get("days"), allowed)


def _state_from_json(d: dict) -> RunState:
    result = d.get("last_result") or ""
    return RunState(str(d.get("since") or ""), str(d.get("last_run") or ""),
                    result if result in RESULTS else "", str(d.get("last_message") or "")[:500])


def schedules_from_json(d: dict) -> ServerSchedules:
    b, m = d.get("backup") or {}, d.get("maintenance") or {}
    retention = b.get("retention")
    if retention is not None:
        retention = validate_retention(retention)
    backup = BackupSchedule(bool(b.get("enabled", False)), _rule_from_json(b, BACKUP_KINDS) if b else BackupSchedule().rule,
                            retention, _state_from_json(b))
    maint = MaintenanceSchedule(
        bool(m.get("enabled", False)), _rule_from_json(m, MAINTENANCE_KINDS) if m else MaintenanceSchedule().rule,
        bool(m.get("backup_first", True)), bool(m.get("update_image", True)), bool(m.get("restart", True)),
        _state_from_json(m))
    hist = tuple(HistoryEntry(str(h["time"]), str(h["kind"]), str(h["trigger"]), str(h["result"]), str(h["message"])[:500])
                 for h in (d.get("history") or [])[:HISTORY_LIMIT])
    return ServerSchedules(backup, maint, hist)


def validate_retention(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RETENTION:
        raise ValidationError(f"Backups to keep must be a whole number from 1 to {MAX_RETENTION} (or empty for the default)")
    return value


# ----------------------------------------------------------------------------------------------- store
def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class ScheduleStore:
    """All servers' schedules in one JSON file. Reads are served from memory; every change is written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._items: dict[str, ServerSchedules] | None = None
        self.problem = ""   # plain-language note when the file could not be read (it is then set aside)

    # -- loading
    def _load(self) -> dict[str, ServerSchedules]:
        if self._items is not None:
            return self._items
        items: dict[str, ServerSchedules] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for sid, d in (raw.get("servers") or {}).items():
                    try:
                        items[str(sid)] = schedules_from_json(d)
                    except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as e:
                        log.error("Ignoring the stored schedule of '%s': %s", sid, e)
                        self.problem = f"The stored schedule of '{sid}' was invalid and has been ignored ({e})."
            except (ValueError, OSError, AttributeError) as e:
                aside = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
                try:
                    os.replace(self.path, aside)
                except OSError:
                    aside = self.path
                self.problem = (f"The schedules file could not be read ({e}). It was kept as {aside} and all schedules "
                                "start empty; set them up again.")
                log.error(self.problem)
                items = {}
        self._items = items
        return items

    def _save(self) -> None:
        items = self._load()
        payload = json.dumps({"version": FORMAT_VERSION, "servers": {k: schedules_to_json(v) for k, v in items.items()}},
                             indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".schedules-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            _fsync_dir(self.path.parent)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- access
    def get(self, server_id: str) -> ServerSchedules:
        with self._lock:
            return self._load().get(server_id, ServerSchedules())

    def all(self) -> dict[str, ServerSchedules]:
        with self._lock:
            return dict(self._load())

    def put(self, server_id: str, value: ServerSchedules) -> None:
        with self._lock:
            self._load()[server_id] = value
            self._save()

    def update(self, server_id: str, fn) -> ServerSchedules:
        """Read, change with fn(ServerSchedules) -> ServerSchedules, write: one step under the lock."""
        with self._lock:
            new = fn(self.get(server_id))
            self._load()[server_id] = new
            self._save()
            return new

    def remove(self, server_id: str) -> None:
        with self._lock:
            if self._load().pop(server_id, None) is not None:
                self._save()


def with_history(s: ServerSchedules, entry: HistoryEntry) -> ServerSchedules:
    return replace(s, history=(entry, *s.history)[:HISTORY_LIMIT])
