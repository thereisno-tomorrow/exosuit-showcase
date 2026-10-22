"""Result and item shapes shared by every read-only source reader.

A read reports what happened, separately from what it found (step D spec
§3.1, §3.4). Readers return a ``SourceRead`` to whoever asked and never save
it; an error is never turned into an empty ``ok``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


JsonObject = Mapping[str, Any]


class ReadStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class UnavailableReason(StrEnum):
    DISABLED = "disabled"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_INVALID = "credential_invalid"
    SCOPE_NOT_READ_ONLY = "scope_not_read_only"
    TOKEN_REVOKED = "token_revoked"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True, slots=True)
class SourceItem:
    """A single fact read from a source, kept as the kind its source is.

    ``said_by`` names who said it (an email's ``From``) and is ``None`` for a
    source, like a calendar, that has no author distinct from the account.
    """

    source: str
    account: str
    provider_id: str
    link: str
    said_by: str | None
    read_at: datetime
    fields: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceRead:
    """The outcome of one read: ``ok``, ``partial``, or ``unavailable``."""

    status: ReadStatus
    items: tuple[SourceItem, ...] = ()
    reason: UnavailableReason | None = None
    unavailable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is ReadStatus.UNAVAILABLE:
            if self.reason is None:
                raise ValueError("an unavailable read must name a reason")
            if self.items or self.unavailable:
                raise ValueError("an unavailable read carries no items or partial ids")
        elif self.reason is not None:
            raise ValueError("only an unavailable read carries a reason")
        elif self.status is ReadStatus.PARTIAL and not self.unavailable:
            raise ValueError("a partial read must name what could not be read")
        elif self.status is ReadStatus.OK and self.unavailable:
            raise ValueError("an ok read carries no partial ids")

    @classmethod
    def ok(cls, items: tuple[SourceItem, ...] = ()) -> "SourceRead":
        return cls(ReadStatus.OK, items=items)

    @classmethod
    def partial(cls, items: tuple[SourceItem, ...], unavailable: tuple[str, ...]) -> "SourceRead":
        return cls(ReadStatus.PARTIAL, items=items, unavailable=unavailable)

    @classmethod
    def unavailable_because(cls, reason: UnavailableReason) -> "SourceRead":
        return cls(ReadStatus.UNAVAILABLE, reason=reason)
