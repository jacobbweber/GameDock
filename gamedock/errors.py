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
