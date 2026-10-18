from .base import ContainerBackend, ContainerState
from .docker_cli import DockerCliBackend
from .fake import FakeBackend


def make_backend(kind: str) -> ContainerBackend:
    if kind == "fake":
        return FakeBackend()
    if kind == "docker":
        return DockerCliBackend()
    raise ValueError(f"Unknown backend {kind!r} (use 'docker' or 'fake')")


__all__ = ["ContainerBackend", "ContainerState", "DockerCliBackend", "FakeBackend", "make_backend"]
