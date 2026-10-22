"""Explicit transport errors.

The transport layer reports provider facts.  It never turns those facts into
request or operation lifecycle transitions; the service owns that decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TransportError(Exception):
    code: str
    message: str
    retryable: bool = False
    retry_after: float | None = None
    status: int | None = None
    accepted: bool = False
    provider_code: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


class ProviderTransportError(TransportError):
    """An HTTP or provider failure with a stable, non-secret error mapping."""


class ReconciliationRequiredError(TransportError):
    def __init__(self, message: str = "provider retry window expired; reconcile manually") -> None:
        super().__init__(
            code="RECOVERY_REQUIRED",
            message=message,
            retryable=False,
        )


class ThreadScopeConflictError(TransportError):
    def __init__(self, message: str = "provider returned a message outside the requested thread") -> None:
        super().__init__(
            code="THREAD_SCOPE_CONFLICT",
            message=message,
            retryable=False,
        )


class FramingError(TransportError):
    def __init__(self, message: str) -> None:
        super().__init__(code="INVALID_PACKET", message=message, retryable=False)


def error_from_provider(code: str, message: str, **details: Any) -> ProviderTransportError:
    """Create an error without ever including provider response bodies."""

    return ProviderTransportError(code=code, message=message, **details)
