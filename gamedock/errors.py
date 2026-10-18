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


class CatalogError(GameDockError):
    pass


class AccessError(GameDockError):
    """A file could not be read or written because of permissions (HTTP 403)."""


class TooLargeError(GameDockError):
    """A file is bigger than GameDock is willing to handle (HTTP 413)."""


class OperationError(GameDockError):
    """An operation failed for a reason other than bad input (HTTP 500), e.g. disk full."""
