from __future__ import annotations


class ActiveTurnError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


class ConflictError(RuntimeError):
    pass
