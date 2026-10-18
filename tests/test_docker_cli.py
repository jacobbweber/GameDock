import subprocess

import pytest

from gamedock.backends import DockerCliBackend
from gamedock.errors import BackendError
from gamedock.models import ContainerSpec, PortMapping, Status, VolumeMapping


class Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        rc, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)


SPEC = ContainerSpec(
    "gamedock-x", "img:1", (PortMapping("udp", 1000, 2000),),
    (VolumeMapping("/h", "/c"),), {"SECRET": "hunter2"}, {"a": "b"},
)


def test_create_args_do_not_contain_env_values():
    args = DockerCliBackend.build_create_args(SPEC)
    assert "hunter2" not in " ".join(args)
    assert args[-1] == "img:1"
    assert "1000:2000/udp" in args and "/h:/c" in args


def test_create_passes_env_via_child_environment():
    r = Recorder([(1, "", "Error: No such object: gamedock-x"), (0, "id", "")])
    DockerCliBackend(runner=r).create(SPEC)
    assert r.calls[1][1]["env"]["SECRET"] == "hunter2"


def test_state_mapping():
    r = Recorder([(0, "running\n", ""), (0, "exited\n", ""), (1, "", "Error: No such object: x")])
    b = DockerCliBackend(runner=r)
    assert b.get_state("x").status == Status.RUNNING
    assert b.get_state("x").status == Status.STOPPED
    assert b.get_state("x").status == Status.NOT_CREATED


def test_failure_raises_backend_error():
    r = Recorder([(1, "", "boom")])
    with pytest.raises(BackendError):
        DockerCliBackend(runner=r).start("x")


def test_missing_docker_binary():
    def runner(*a, **k):
        raise FileNotFoundError

    with pytest.raises(BackendError, match="not found"):
        DockerCliBackend(runner=runner).start("x")
