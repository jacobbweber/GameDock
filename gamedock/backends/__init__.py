from .base import ContainerBackend, ContainerState, DockerDiagnosis
from .docker_cli import DockerCliBackend
from .fake import FakeBackend


def make_backend(kind: str, stop_timeout: int = 60, helper_image: str | None = None) -> ContainerBackend:
    if kind == "fake":
        return FakeBackend()
    if kind == "docker":
        if helper_image:
            return DockerCliBackend(stop_timeout=stop_timeout, helper_image=helper_image)
        return DockerCliBackend(stop_timeout=stop_timeout)
    raise ValueError(f"Unknown backend {kind!r} (use 'docker' or 'fake')")


__all__ = ["ContainerBackend", "ContainerState", "DockerCliBackend", "DockerDiagnosis", "FakeBackend", "make_backend"]
