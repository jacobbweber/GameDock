import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from gamedock.backends import FakeBackend
from gamedock.service import GameDockService

SAMPLE = textwrap.dedent(
    """\
    id: sample
    name: Sample Game
    image: busybox:latest
    ports:
      - { protocol: udp, host: 27015, container: 27015 }
    volumes:
      - { host: ./saves, container: /saves }
    env:
      FIXED: value
    fields:
      - { name: SERVER_NAME, label: Server name, default: "Sample", required: true }
      - { name: MAX, default: "8" }
      - { name: SERVER_PASSWORD, label: Password, secret: true }
      - { name: ADMIN_PASSWORD, label: Admin password, secret: true, required: true }
    config_files:
      - path: conf/settings.ini
        label: Settings
        format: ini
        sensitive_keys: [AdminPassword, rcon.password]
      - path: conf/hot.txt
        label: Hot file
        hot_edit: true
      - path: conf/data.json
        format: json
        sensitive_keys: [token]
    """
)


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def catalog_dir(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    (d / "sample.yaml").write_text(SAMPLE)
    return d


ADMIN = {"ADMIN_PASSWORD": "adminpw"}


class StepClock:
    """Deterministic clock: every call is one second later."""

    def __init__(self):
        self.t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def clock():
    return StepClock()


@pytest.fixture
def service(backend, catalog_dir, tmp_path, clock):
    return GameDockService(backend, catalog_dir, tmp_path / "data", clock=clock)


@pytest.fixture
def deployed(service):
    """A deployed (stopped) server with id 'one'."""
    service.deploy("sample", "One", env=ADMIN)
    return "one"
