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
      FOO: bar
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


@pytest.fixture
def service(backend, catalog_dir, tmp_path):
    return GameDockService(backend, catalog_dir, tmp_path / "data")
