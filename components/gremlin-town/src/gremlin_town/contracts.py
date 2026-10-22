"""Frozen interfaces shared by the hub, transports, and thin clients.

This module deliberately depends only on the Python standard library. Component
lanes may depend on it; changing a public name or field requires orchestrator
review and a matching update to ``docs/interfaces.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence


CONTRACT_VERSION = 1
MAX_PACKET_BYTES = 32 * 1024


def proof_kind(proof: str | None) -> str | None:
    """Classify bounded evidence references; prose is never proof or instructions."""
    if proof is None:
        return None
    fix = "proof must be a link (http/https), file path or hub operation ID, at most 300 characters"
    if not isinstance(proof, str) or not 1 <= len(proof) <= 300:
        raise ValueError(fix)
    if re.fullmatch(r"https?://[A-Za-z0-9][A-Za-z0-9._~:/?#\[\]@!$&()*+,=%+-]*", proof):
        return "link"
    if re.fullmatch(r"(?:[A-Za-z]:[/\\]|\.?\.?/|[A-Za-z0-9_.-]+[/\\])[A-Za-z0-9_./\\ :+-]+", proof):
        return "path"
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*[_-][A-Za-z0-9_-]+", proof):
        return "id"
    raise ValueError(fix)


JsonObject = Mapping[str, Any]


class RequestMode(StrEnum):
    CONTEXT_ONLY = "context_only"
    ACTION_REQUESTED = "action_requested"


class MessageKind(StrEnum):
    CONTEXT = "context"
    REQUEST = "request"
    CLARIFICATION = "clarification"
    RESULT = "result"
    CANCELLATION = "cancellation"


class WorkState(StrEnum):
    REQUESTED = "requested"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    RESULT_RETURNED = "result_returned"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class OperationState(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReportAction(StrEnum):
    ACKNOWLEDGE = "acknowledge"
    START = "start"
    BLOCK = "block"
    DECLINE = "decline"


class ErrorCode(StrEnum):
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    PACKET_TOO_LARGE = "PACKET_TOO_LARGE"
    RECIPIENT_AMBIGUOUS = "RECIPIENT_AMBIGUOUS"
    ACCESS_DENIED = "ACCESS_DENIED"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    OPERATION_INPUT_CONFLICT = "OPERATION_INPUT_CONFLICT"
    RESULT_PENDING = "RESULT_PENDING"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    THREAD_SCOPE_CONFLICT = "THREAD_SCOPE_CONFLICT"
    RECOVERY_HOLD = "RECOVERY_HOLD"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class ContentAvailability(StrEnum):
    """Overall request content state; an empty message list is never ambiguous."""

    EMPTY = "empty"
    QUEUED_ONLY = "queued_only"
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ContentSource(StrEnum):
    OUTBOX = "outbox"
    PROVIDER = "provider"


class RecoveryDisposition(StrEnum):
    KEEP_HELD = "keep_held"
    RELEASE_ACTIONABLE = "release_actionable"
    CONFIRM_CANCELLED = "confirm_cancelled"
    CONFIRM_DECLINED = "confirm_declined"


class CheckOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Actor:
    """Identity established by authentication, never by packet text."""

    actor_id: str
    is_operator: bool = False


@dataclass(frozen=True, slots=True)
class Packet:
    schema_version: int
    kind: MessageKind
    session_reference: str | None
    objective: str
    context: str
    constraints: JsonObject
    sources: Sequence[JsonObject]
    next_action: str
    expected_reply: str
    sensitivity: str


@dataclass(frozen=True, slots=True)
class WireMessage:
    """Hub-stamped content serialized exactly once before provider dispatch."""

    request_id: str
    operation_id: str
    sender: str
    recipient: str
    created_at: datetime
    packet: Packet


@dataclass(frozen=True, slots=True)
class CreateRequest:
    """``task_id`` (optional, N12b): the Brain record ID of the task this request is for. The hub
    copies it onto the request's "where I stopped" note. Left out of the operation digest when
    absent, so a request made without one hashes exactly as it did before the field existed."""

    client_operation_id: str
    mode: RequestMode
    project: str
    recipient: str
    packet: Packet
    prior_request_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReportRequest:
    client_operation_id: str
    request_id: str
    expected_revision: int
    action: ReportAction
    reason: str | None = None
    session_reference: str | None = None


@dataclass(frozen=True, slots=True)
class ReplyRequest:
    client_operation_id: str
    request_id: str
    expected_revision: int
    kind: MessageKind
    packet: Packet
    proof: str | None = None


@dataclass(frozen=True, slots=True)
class CancelRequest:
    client_operation_id: str
    request_id: str
    expected_revision: int
    reason: str


@dataclass(frozen=True, slots=True)
class CheckRequest:
    """A check someone made that a returned result is actually done; never automatic."""

    client_operation_id: str
    request_id: str
    expected_revision: int
    outcome: CheckOutcome
    evidence: str


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """Operator-only disposition; provider reconciliation cannot create this."""

    client_operation_id: str
    request_id: str
    expected_revision: int
    disposition: RecoveryDisposition
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderReference:
    message_id: str
    thread_id: str
    accepted_at: datetime
    parent_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReceiptError:
    code: ErrorCode
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class Receipt:
    operation_id: str
    request_id: str | None
    accepted_at: datetime
    operation_state: OperationState | None
    work_state: WorkState | None
    revision: int | None
    actor_id: str
    provider: ProviderReference | None = None
    error: ReceiptError | None = None


@dataclass(frozen=True, slots=True)
class OutboundEnvelope:
    """Immutable bytes and retry identity saved before provider dispatch."""

    operation_id: str
    request_id: str
    kind: MessageKind
    provider_retry_key: str
    payload: bytes
    content_sha256: str
    recipient_address: str
    parent_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    message_id: str
    thread_id: str
    parent_message_id: str | None
    received_at: datetime
    body: bytes | None
    body_available: bool
    content_source: ContentSource
    trust_label: str
    linked_operation_id: str | None = None
    attributed_actor_id: str | None = None
    exact_link_verified: bool = False


@dataclass(frozen=True, slots=True)
class Page:
    items: Sequence[JsonObject]
    next_cursor: str | None = None
    as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class RequestSnapshot:
    request_id: str
    mode: RequestMode
    project: str
    requester_id: str
    recipient_id: str
    objective: str
    work_state: WorkState | None
    revision: int
    recovery_hold: bool
    thread_id: str | None
    content_availability: ContentAvailability
    content: Sequence[ProviderMessage] = field(default_factory=tuple)
    next_message_cursor: str | None = None
    content_error: ReceiptError | None = None
    as_of: datetime | None = None
    task_id: str | None = None


class ProviderTransport(Protocol):
    """Provider I/O only. It must not apply request lifecycle rules."""

    def send(self, envelope: OutboundEnvelope) -> ProviderReference: ...

    def fetch_message(self, message_id: str) -> ProviderMessage: ...

    def fetch_thread(self, thread_id: str, cursor: str | None = None) -> Page: ...

    def reconcile(self, envelope: OutboundEnvelope) -> ProviderReference | None: ...


class GremlinTownService(Protocol):
    """One application-logic path used by API, CLI, views, and adapters."""

    def participants_list(self, actor: Actor, project: str | None = None) -> Page: ...

    def requests_create(self, actor: Actor, command: CreateRequest) -> Receipt: ...

    def requests_list(
        self, actor: Actor, filters: JsonObject, cursor: str | None = None
    ) -> Page: ...

    def requests_read(
        self, actor: Actor, request_id: str, message_cursor: str | None = None
    ) -> RequestSnapshot: ...

    def requests_report(self, actor: Actor, command: ReportRequest) -> Receipt: ...

    def requests_reply(self, actor: Actor, command: ReplyRequest) -> Receipt: ...

    def requests_cancel(self, actor: Actor, command: CancelRequest) -> Receipt: ...

    def requests_check(self, actor: Actor, command: CheckRequest) -> Receipt: ...

    def recovery_dispose(self, actor: Actor, command: RecoveryRequest) -> Receipt: ...

    def operations_get(self, actor: Actor, operation_id: str) -> Receipt: ...

    def events_list(
        self, actor: Actor, filters: JsonObject, cursor: str | None = None
    ) -> Page: ...
