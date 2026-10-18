"""Runtime settings, read from environment variables (optionally a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env loader. Existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"  # local only unless explicitly configured
    port: int = 8080
    catalog_dir: Path = Path("catalog")
    data_dir: Path = Path("data")
    backend: str = "docker"  # "docker" or "fake"
    backup_retention: int = 10  # keep the newest N backups per server
    stats_enabled: bool = True  # run the background sampler (history sparklines)
    stats_interval: float = 10.0  # seconds between samples
    stats_history_seconds: int = 3600  # how much history to keep in memory
    metrics_enabled: bool = False  # serve Prometheus text at /metrics
    metrics_allow_remote: bool = False  # /metrics answers only loopback clients unless this is true

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            host=env.get("GAMEDOCK_HOST", "127.0.0.1"),
            port=int(env.get("GAMEDOCK_PORT", "8080")),
            catalog_dir=Path(env.get("GAMEDOCK_CATALOG_DIR", "catalog")),
            data_dir=Path(env.get("GAMEDOCK_DATA_DIR", "data")),
            backend=env.get("GAMEDOCK_BACKEND", "docker").lower(),
            backup_retention=max(1, int(env.get("GAMEDOCK_BACKUP_RETENTION", "10"))),
            stats_enabled=_bool(env.get("GAMEDOCK_STATS_ENABLED", "true")),
            stats_interval=min(300.0, max(2.0, float(env.get("GAMEDOCK_STATS_INTERVAL", "10")))),
            stats_history_seconds=min(86400, max(60, int(env.get("GAMEDOCK_STATS_HISTORY_SECONDS", "3600")))),
            metrics_enabled=_bool(env.get("GAMEDOCK_METRICS_ENABLED", "false")),
            metrics_allow_remote=_bool(env.get("GAMEDOCK_METRICS_ALLOW_REMOTE", "false")),
        )
