from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from gremlin_town.contracts import MessageKind, OperationState, RequestMode, WorkState


@dataclass(frozen=True, slots=True)
class RequestRecord:
    request_id: str
    mode: RequestMode
    project: str
    requester_id: str
    recipient_id: str
    objective: str
    sensitivity: str
    work_state: WorkState | None
    revision: int
    recovery_hold: bool
    prior_request_id: str | None
    thread_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    client_operation_id: str
    actor_id: str
    request_id: str
    operation_kind: str
    message_kind: MessageKind | None
    input_digest: str
    accepted_at: datetime
    operation_state: OperationState | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    payload: bytes | None
    payload_sha256: str | None
    provider_retry_key: str | None
    recipient_address: str | None
    route_source_id: str | None
    route_destination_id: str | None
    route_sensitivity: str | None
    parent_message_id: str | None
    provider_message_id: str | None
    provider_thread_id: str | None
    provider_parent_message_id: str | None
    provider_accepted_at: datetime | None
    attempt_started_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime | None
    transport_error_code: str | None
    transport_status: int | None
    uncertain_since: datetime | None
    recovery_required: bool
    recovery_hold: bool
