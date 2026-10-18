"""Small persistent store for deployed instances: one JSON file under the data dir.

The file may contain secret values (passwords), so it is created with mode 0600
and lives under data/, which is git-ignored. Writes are atomic.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from .errors import GameDockError
from .models import Instance, PortMapping

FORMAT_VERSION = 1


def _fsync_dir(path: Path) -> None:
    """Make the rename itself durable (not possible on every platform; then it is skipped)."""
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


def _to_json(i: Instance) -> dict:
    return {
        "id": i.id, "name": i.name, "game_id": i.game_id, "template": i.template,
        "ports": [{"protocol": p.protocol, "host": p.host, "container": p.container} for p in i.ports],
        "env": i.env, "created_at": i.created_at,
    }


def _from_json(d: dict) -> Instance:
    return Instance(
        id=d["id"], name=d["name"], game_id=d["game_id"], template=d["template"],
        ports=tuple(PortMapping(p["protocol"], int(p["host"]), int(p["container"])) for p in d["ports"]),
        env={str(k): str(v) for k, v in d.get("env", {}).items()},
        created_at=d.get("created_at", ""),
    )


class InstanceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def _load(self) -> dict[str, Instance]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {d["id"]: _from_json(d) for d in raw.get("instances", [])}
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            bak = self.path.with_name(self.path.name + ".bak")
            hint = (f" The previous version is kept at {bak}. To go back to it: stop GameDock, copy that file over "
                    f"{self.path}, start GameDock." if bak.is_file() else
                    " There is no .bak copy; restore the file from your own backup or fix the JSON by hand.")
            raise GameDockError(f"The list of deployed servers ({self.path}) is unreadable: {e}.{hint}") from e

    def _save(self, items: dict[str, Instance]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": FORMAT_VERSION, "instances": [_to_json(i) for i in items.values()]}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".instances-", suffix=".tmp")  # created 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())  # data is on disk before the rename makes it the real file
            os.chmod(tmp, 0o600)
            if self.path.is_file():  # keep the last good version (same 0600 mode: it holds secrets too)
                bak = self.path.with_name(self.path.name + ".bak")
                data = self.path.read_bytes()
                bfd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never briefly world-readable
                with os.fdopen(bfd, "wb") as bf:
                    bf.write(data)
                os.chmod(bak, 0o600)
            os.replace(tmp, self.path)
            _fsync_dir(self.path.parent)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def all(self) -> list[Instance]:
        with self._lock:
            return list(self._load().values())

    def get(self, instance_id: str) -> Instance | None:
        with self._lock:
            return self._load().get(instance_id)

    def put(self, instance: Instance) -> None:
        """Insert or replace."""
        with self._lock:
            items = self._load()
            items[instance.id] = instance
            self._save(items)

    def remove(self, instance_id: str) -> None:
        with self._lock:
            items = self._load()
            if items.pop(instance_id, None) is not None:
                self._save(items)
