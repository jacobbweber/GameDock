class GameDockError(Exception):
    """Base class for expected, user-facing errors."""


class NotFoundError(GameDockError):
    pass


class ValidationError(GameDockError):
    """The request is not acceptable as written (HTTP 400)."""


class ConflictError(GameDockError):
    """The request conflicts with current state, e.g. port in use (HTTP 409)."""


class BackendError(GameDockError):
    pass


class DockerUnavailableError(BackendError):
    """Docker is missing, not running, or this user may not use it. `kind` is one of
    not_installed, daemon_down, permission."""

    def __init__(self, message: str, kind: str = "daemon_down") -> None:
        super().__init__(message)
        self.kind = kind


class ForeignContainerError(BackendError):
    """A container with GameDock's name exists but GameDock did not create it; it is never touched."""


class CatalogError(GameDockError):
    pass


class AccessError(GameDockError):
    """A file could not be read or written because of permissions (HTTP 403)."""


class TooLargeError(GameDockError):
    """A file is bigger than GameDock is willing to handle (HTTP 413)."""


class OperationError(GameDockError):
    """An operation failed for a reason other than bad input (HTTP 500), e.g. disk full."""
