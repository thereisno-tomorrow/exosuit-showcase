from __future__ import annotations

from gremlin_town.contracts import ErrorCode


class GremlinTownError(Exception):
    """A machine-readable service failure safe for adapters to translate."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
