"""Stable public failures for the CLI and JSON protocol."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "brain.invalid_argument"
    PROTOCOL_INVALID = "brain.protocol.invalid"
    PROTOCOL_VERSION = "brain.protocol.version"
    YAML_INVALID = "brain.yaml.invalid"
    CONTRACT_INVALID = "brain.contract.invalid"
    DUPLICATE_ID = "brain.catalog.duplicate_id"
    ROOT_MISSING = "brain.path.root_missing"
    PATH_ABSOLUTE = "brain.path.absolute"
    PATH_TRAVERSAL = "brain.path.traversal"
    PATH_ESCAPE = "brain.path.escape"
    PATH_CASE_COLLISION = "brain.path.case_collision"
    PATH_MISSING = "brain.path.missing"
    GRANT_MISSING = "brain.grant.missing"
    GRANT_EXPIRED = "brain.grant.expired"
    GRANT_WRONG_PATH = "brain.grant.wrong_path"
    POLICY_EXCLUDED = "brain.policy.excluded"
    INDEX_BUSY = "brain.index.busy"
    INDEX_BUILD_FAILED = "brain.index.build_failed"
    INDEX_STALE = "brain.index.stale"
    INDEX_MISSING = "brain.index.missing"
    TOKENIZER_MISMATCH = "brain.tokenizer.mismatch"
    SOURCE_CHANGED = "brain.source.changed"
    SOURCE_BINARY = "brain.source.binary"
    SOURCE_OVERSIZED = "brain.source.oversized"
    SOURCE_UNREADABLE = "brain.source.unreadable"
    NOT_FOUND = "brain.not_found"
    CAPABILITY_UNAVAILABLE = "brain.capability.unavailable"
    RECORD_EXISTS = "brain.record.exists"
    RECORD_TRANSITION = "brain.record.transition"
    RECORD_CORRUPT = "brain.record.corrupt"
    RECORD_WRITE_FAILED = "brain.record.write_failed"
    CONTINUITY_EMPTY = "brain.continuity.empty"
    MIGRATION_MISMATCH = "brain.migration.mismatch"
    NEEDS_OWNER_WORDS = "brain.record.needs_owner_words"
    REASON_REQUIRED = "brain.record.reason_required"
    DONE_NEEDS_CHECK = "brain.record.done_needs_check"
    TIME_NOT_FROM_WORDS = "brain.record.time_not_from_words"
    RECORD_CHANGED = "brain.record.changed"
    ACTOR_MISMATCH = "brain.record.actor_mismatch"
    TIME_UNRESOLVED = "brain.time.unresolved"


_EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENT: 2,
    ErrorCode.PROTOCOL_INVALID: 3,
    ErrorCode.PROTOCOL_VERSION: 4,
    ErrorCode.YAML_INVALID: 10,
    ErrorCode.CONTRACT_INVALID: 11,
    ErrorCode.DUPLICATE_ID: 12,
    ErrorCode.ROOT_MISSING: 20,
    ErrorCode.PATH_ABSOLUTE: 21,
    ErrorCode.PATH_TRAVERSAL: 22,
    ErrorCode.PATH_ESCAPE: 23,
    ErrorCode.PATH_CASE_COLLISION: 24,
    ErrorCode.PATH_MISSING: 25,
    ErrorCode.GRANT_MISSING: 30,
    ErrorCode.GRANT_EXPIRED: 31,
    ErrorCode.GRANT_WRONG_PATH: 32,
    ErrorCode.POLICY_EXCLUDED: 33,
    ErrorCode.INDEX_BUSY: 40,
    ErrorCode.INDEX_BUILD_FAILED: 41,
    ErrorCode.INDEX_STALE: 42,
    ErrorCode.INDEX_MISSING: 43,
    ErrorCode.TOKENIZER_MISMATCH: 44,
    ErrorCode.SOURCE_CHANGED: 50,
    ErrorCode.SOURCE_BINARY: 51,
    ErrorCode.SOURCE_OVERSIZED: 52,
    ErrorCode.SOURCE_UNREADABLE: 53,
    ErrorCode.NOT_FOUND: 60,
    ErrorCode.CAPABILITY_UNAVAILABLE: 61,
    ErrorCode.RECORD_EXISTS: 70,
    ErrorCode.RECORD_TRANSITION: 71,
    ErrorCode.RECORD_CORRUPT: 72,
    ErrorCode.RECORD_WRITE_FAILED: 73,
    ErrorCode.CONTINUITY_EMPTY: 74,
    ErrorCode.MIGRATION_MISMATCH: 75,
    ErrorCode.NEEDS_OWNER_WORDS: 76,
    ErrorCode.REASON_REQUIRED: 77,
    ErrorCode.DONE_NEEDS_CHECK: 78,
    ErrorCode.TIME_NOT_FROM_WORDS: 79,
    ErrorCode.RECORD_CHANGED: 80,
    ErrorCode.ACTOR_MISMATCH: 81,
    ErrorCode.TIME_UNRESOLVED: 82,
}


class BrainError(Exception):
    """An expected, stable failure that may be rendered to a user."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.hint = hint

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self.code]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }
        if self.hint is not None:
            payload["hint"] = self.hint
        return payload


def fail(
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    hint: str | None = None,
) -> None:
    raise BrainError(code, message, details=details, hint=hint)
