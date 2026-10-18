import textwrap

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


@pytest.fixture
def service(backend, catalog_dir, tmp_path):
    return GameDockService(backend, catalog_dir, tmp_path / "data")


@pytest.fixture
def deployed(service):
    """A deployed (stopped) server with id 'one'."""
    service.deploy("sample", "One", env=ADMIN)
    return "one"
