class GameDockError(Exception):
    """Base class for expected, user-facing errors."""


class NotFoundError(GameDockError):
    pass


class BackendError(GameDockError):
    pass


class CatalogError(GameDockError):
    pass
