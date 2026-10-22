from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Callable, Mapping

from gremlin_town.contracts import (
    MAX_PACKET_BYTES,
    Actor,
    CancelRequest,
    CheckOutcome,
    CheckRequest,
    ContentAvailability,
    ContentSource,
    CreateRequest,
    ErrorCode,
    MessageKind,
    OperationState,
    Page,
    ProviderMessage,
    Receipt,
    ReceiptError,
    RecoveryDisposition,
    RecoveryRequest,
    ReplyRequest,
    ReportAction,
    ReportRequest,
    RequestMode,
    RequestSnapshot,
    WireMessage,
    WorkState,
    proof_kind,
)
from gremlin_town.domain import GremlinTownError, Policy, canonical_bytes, canonical_digest, packet_bytes
from gremlin_town.domain.models import OperationRecord, RequestRecord
from gremlin_town.storage import SQLiteStore
from gremlin_town.transport.errors import FramingError
from gremlin_town.transport.framing import canonical_wire_bytes, parse_wire_message


TERMINAL_STATES = {WorkState.RESULT_RETURNED, WorkState.DECLINED, WorkState.CANCELLED}
NONTERMINAL_STATES = {
    WorkState.REQUESTED,
    WorkState.ACKNOWLEDGED,
    WorkState.IN_PROGRESS,
    WorkState.BLOCKED,
}
# N12b: a request may name the Brain task it is for, by that task's record ID.
TASK_ID_PATTERN = re.compile(r"^brn_\d{8}_\d{6}_[0-9a-f]{4,16}$")

# Read-time rules shared with triage. Proof belongs to the dispatched result, never a
# queued or failed reply. IDs resolve locally; no evidence reference is fetched.
REQUEST_PROOF_SQL = """(SELECT json_extract(reserved.detail_json, '$.proof') FROM events reserved
    WHERE reserved.request_id=requests.request_id AND reserved.event_type='result_reserved'
      AND reserved.operation_id=(SELECT returned.operation_id FROM events returned
          WHERE returned.request_id=requests.request_id AND returned.event_type='result_returned'
          ORDER BY returned.sequence DESC LIMIT 1)
    ORDER BY reserved.sequence DESC LIMIT 1)"""
REQUEST_PROOF_VERIFIED_SQL = f"""COALESCE(({REQUEST_PROOF_SQL} IS NOT NULL AND (
    {REQUEST_PROOF_SQL} LIKE 'http://%' OR {REQUEST_PROOF_SQL} LIKE 'https://%'
    OR instr({REQUEST_PROOF_SQL}, '/')>0 OR instr({REQUEST_PROOF_SQL}, char(92))>0
    OR EXISTS (SELECT 1 FROM operations proof_operation
        WHERE proof_operation.operation_id={REQUEST_PROOF_SQL}
           OR proof_operation.client_operation_id={REQUEST_PROOF_SQL}
           OR proof_operation.request_id={REQUEST_PROOF_SQL}))), 0)"""
REQUEST_CHECK_SQL = """COALESCE((SELECT CASE check_event.event_type
    WHEN 'result_check_accepted' THEN 'accepted' WHEN 'result_check_rejected' THEN 'rejected' END
    FROM events check_event WHERE check_event.request_id=requests.request_id
    AND check_event.event_type IN ('result_check_accepted','result_check_rejected')
    ORDER BY check_event.sequence DESC LIMIT 1), 'none')"""
REQUEST_LAST_ACTIVITY_SQL = """COALESCE((SELECT activity.occurred_at FROM events activity
    WHERE activity.request_id=requests.request_id
    ORDER BY julianday(activity.occurred_at) DESC, activity.sequence DESC LIMIT 1), requests.updated_at)"""
REQUEST_STILL_OPEN_SQL = f"""((requests.work_state IN
    ('requested','acknowledged','in_progress','blocked') OR
    (requests.work_state='result_returned' AND {REQUEST_CHECK_SQL}!='accepted'
        AND NOT {REQUEST_PROOF_VERIFIED_SQL}))
    AND NOT EXISTS (SELECT 1 FROM events closed WHERE closed.request_id=requests.request_id
        AND closed.event_type='hub_delivery_closed'))"""


def create_digest(command: CreateRequest) -> str:
    """The create operation's input digest. ``task_id`` joins it only when set, so a request
    made without one - and its replay across the release that added the field - hashes
    exactly as before."""

    fields = asdict(command)
    if fields.get("task_id") is None:
        fields.pop("task_id", None)
    return canonical_digest(fields)


class GremlinTownService:
    def __init__(
        self,
        store: SQLiteStore,
        policy: Policy,
        *,
        provider=None,
        default_backend: str = "agentmail",
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.provider = provider
        self.default_backend = default_backend
        self.now = now or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or store.new_id

    def _access(self, actor: Actor, request: RequestRecord | None, *, read_only: bool = False) -> RequestRecord:
        caller = self.policy.authenticate(actor)
        if request is None:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "request is not accessible")
        self._authorize_request(actor, caller, request, read_only=read_only)
        return request

    def _authorize_request(self, actor: Actor, caller, request: RequestRecord, *, read_only: bool = False) -> None:
        self.policy.require_project(caller.participant_id, request.project)
        is_operator = actor.is_operator and caller.is_operator
        is_participant = caller.participant_id in {request.requester_id, request.recipient_id}
        is_read_all = read_only and caller.read_all_requests
        if not is_operator and not is_participant and not is_read_all:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "request is not accessible")
        if not is_operator:
            self.policy.require_route_configuration(
                request.project,
                request.requester_id,
                request.recipient_id,
                request.sensitivity,
            )

    def _accessible_request_ids(self, actor: Actor, caller, *, read_only: bool = False) -> tuple[str, ...]:
        if actor.is_operator and caller.is_operator:
            rows = self.store.fetchall("SELECT request_id FROM requests")
            return tuple(row["request_id"] for row in rows)
        if read_only and caller.read_all_requests and caller.projects:
            placeholders = ",".join("?" for _ in caller.projects)
            rows = self.store.fetchall(
                f"SELECT request_id FROM requests WHERE requester_id=? OR recipient_id=? OR project IN ({placeholders})",
                (caller.participant_id, caller.participant_id, *caller.projects),
            )
        else:
            rows = self.store.fetchall(
                "SELECT request_id FROM requests WHERE requester_id=? OR recipient_id=?",
                (caller.participant_id, caller.participant_id),
            )
        accessible = []
        for row in rows:
            request = self.store.request(row["request_id"])
            try:
                self._authorize_request(actor, caller, request, read_only=read_only)
            except GremlinTownError as exc:
                if exc.code is not ErrorCode.ACCESS_DENIED:
                    raise
            else:
                accessible.append(row["request_id"])
        return tuple(accessible)

    def _operation_access(
        self, actor: Actor, operation: OperationRecord | None, *, read_only: bool = False
    ) -> tuple[OperationRecord, RequestRecord]:
        self.policy.authenticate(actor)
        if operation is None:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "operation is not accessible")
        request = self._access(actor, self.store.request(operation.request_id), read_only=read_only)
        return operation, request

    def _cached(
        self, actor: Actor, client_operation_id: str, digest: str, *, read_only: bool = False
    ) -> Receipt | None:
        operation = self.store.operation_by_client(actor.actor_id, client_operation_id)
        if operation is None:
            return None
        operation, request = self._operation_access(actor, operation, read_only=read_only)
        if operation.input_digest != digest:
            raise GremlinTownError(ErrorCode.OPERATION_INPUT_CONFLICT, "client operation ID has different normalized input")
        # Cached outcomes are not an authorization bypass: both endpoints and
        # the currently configured route must still be valid.
        self.policy.require_project(request.requester_id, request.project)
        self.policy.require_project(request.recipient_id, request.project)
        if operation.route_source_id:
            self.policy.require_route(
                request.project,
                operation.route_source_id,
                operation.route_destination_id,
                operation.route_sensitivity,
            )
        return self._receipt(operation, request)

    @staticmethod
    def _error(operation: OperationRecord) -> ReceiptError | None:
        if not operation.error_code:
            return None
        try:
            code = ErrorCode(operation.error_code)
        except ValueError:
            code = ErrorCode.SOURCE_UNAVAILABLE
        return ReceiptError(code, operation.error_message or code.value, operation.retryable)

    def _receipt(self, operation: OperationRecord, request: RequestRecord | None = None) -> Receipt:
        request = request or self.store.request(operation.request_id)
        provider = None
        if operation.provider_message_id:
            from gremlin_town.contracts import ProviderReference

            provider = ProviderReference(
                operation.provider_message_id,
                operation.provider_thread_id,
                operation.provider_accepted_at,
                operation.provider_parent_message_id,
            )
        return Receipt(
            operation_id=operation.operation_id,
            request_id=operation.request_id,
            accepted_at=operation.accepted_at,
            operation_state=operation.operation_state,
            work_state=request.work_state if request else None,
            revision=request.revision if request else None,
            actor_id=operation.actor_id,
            provider=provider,
            error=self._error(operation),
        )

    def participants_list(self, actor: Actor, project: str | None = None) -> Page:
        # `projects` and `sensitivities` both come from Policy.visible(), which reads
        # the one route table `require_route` enforces on every send. Never
        # narrowed here and never omitted, even when the route table leaves a
        # participant with none: additive fields beside the pre-existing ones.
        visible_participants = self.policy.visible(actor, project)
        items = tuple(
            {
                "participant_id": participant.participant_id,
                "display_name": participant.display_name,
                "projects": sorted(participant.projects),
                "sensitivities": sorted(participant.sensitivities),
                "aliases": list(participant.aliases),
            }
            for participant in visible_participants
        )
        return Page(items=items, as_of=self.now())

    def requests_create(self, actor: Actor, command: CreateRequest) -> Receipt:
        caller = self.policy.authenticate(actor)
        digest = create_digest(command)
        cached = self._cached(actor, command.client_operation_id, digest)
        if cached:
            return cached
        if command.task_id is not None and (
            not isinstance(command.task_id, str) or TASK_ID_PATTERN.fullmatch(command.task_id) is None
        ):
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "task_id must be a Brain record ID")
        if command.packet.schema_version != 1:
            raise GremlinTownError(ErrorCode.UNSUPPORTED_SCHEMA, "only packet schema version 1 is supported")
        if len(packet_bytes(command.packet)) > MAX_PACKET_BYTES:
            raise GremlinTownError(ErrorCode.PACKET_TOO_LARGE, "packet exceeds the configured byte limit")
        expected_kind = MessageKind.CONTEXT if command.mode is RequestMode.CONTEXT_ONLY else MessageKind.REQUEST
        if command.packet.kind is not expected_kind:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "packet kind does not match request mode")
        recipient = self.policy.resolve_recipient(command.recipient)
        self.policy.require_route(
            command.project, caller.participant_id, recipient.participant_id, command.packet.sensitivity
        )
        if command.prior_request_id:
            prior = self._access(actor, self.store.request(command.prior_request_id))
            if prior.project != command.project:
                raise GremlinTownError(ErrorCode.ACCESS_DENIED, "linked request is outside the project")
            self._require_participant_access(recipient.participant_id, prior)

        request_id = self.id_factory("req")
        operation_id = self.id_factory("op")
        accepted_at = self.now()
        payload = canonical_bytes(
            WireMessage(
                request_id=request_id,
                operation_id=operation_id,
                sender=caller.participant_id,
                recipient=recipient.participant_id,
                created_at=accepted_at,
                packet=command.packet,
            )
        )
        payload_sha = hashlib.sha256(payload).hexdigest()
        provider_key = f"gt-{operation_id.removeprefix('op_')}"
        work_state = WorkState.REQUESTED if command.mode is RequestMode.ACTION_REQUESTED else None
        with self.store.transaction() as connection:
            # A racing identical call is resolved from the durable key.
            existing = self.store.operation_by_client(actor.actor_id, command.client_operation_id, connection)
            if existing:
                if existing.input_digest != digest:
                    raise GremlinTownError(ErrorCode.OPERATION_INPUT_CONFLICT, "client operation ID conflict")
                return self._receipt(existing, self.store.request(existing.request_id, connection))
            connection.execute(
                """
                INSERT INTO requests(request_id, mode, project, requester_id, recipient_id, objective, sensitivity,
                    work_state, revision, recovery_hold, prior_request_id, thread_id, created_at, updated_at, backend_binding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, NULL, ?, ?, ?)
                """,
                (
                    request_id,
                    command.mode.value,
                    command.project,
                    caller.participant_id,
                    recipient.participant_id,
                    command.packet.objective,
                    command.packet.sensitivity,
                    work_state.value if work_state else None,
                    command.prior_request_id,
                    accepted_at.isoformat(),
                    accepted_at.isoformat(),
                    self.default_backend,
                ),
            )
            connection.execute(
                """
                INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id,
                    operation_kind, message_kind, input_digest, accepted_at, operation_state,
                    payload, payload_sha256, provider_retry_key, recipient_address,
                    route_source_id, route_destination_id, route_sensitivity)
                VALUES (?, ?, ?, ?, 'create', ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    command.client_operation_id,
                    caller.participant_id,
                    request_id,
                    command.packet.kind.value,
                    digest,
                    accepted_at.isoformat(),
                    payload,
                    payload_sha,
                    provider_key,
                    recipient.address,
                    caller.participant_id,
                    recipient.participant_id,
                    command.packet.sensitivity,
                ),
            )
            created_detail: dict[str, object] = {"mode": command.mode.value, "prior_request_id": command.prior_request_id}
            if command.task_id is not None:
                # Stored with the request's own creation event, not a new column, so an
                # older release reading this database is unaffected (N12b; Proposed).
                created_detail["task_id"] = command.task_id
            self.store.append_event(
                connection,
                request_id=request_id,
                operation_id=operation_id,
                actor_id=caller.participant_id,
                event_type="request_created",
                detail=created_detail,
                occurred_at=accepted_at,
            )
        return self._receipt(self.store.operation(operation_id), self.store.request(request_id))

    def _request_task_id(self, request_id: str) -> str | None:
        row = self.store.fetchone(
            """SELECT detail_json FROM events WHERE request_id=? AND event_type='request_created'
            ORDER BY sequence LIMIT 1""",
            (request_id,),
        )
        if row is None:
            return None
        value = json.loads(row["detail_json"]).get("task_id")
        return value if isinstance(value, str) else None

    def requests_list(self, actor: Actor, filters: Mapping, cursor: str | None = None) -> Page:
        caller = self.policy.authenticate(actor)
        as_of = self.now()
        limit = max(1, min(int(filters.get("limit", 100)), 100))
        offset = self._cursor(cursor)
        authenticated_report = """EXISTS (
            SELECT 1 FROM events report_event
            WHERE report_event.request_id=requests.request_id
              AND report_event.actor_id=requests.recipient_id
              AND report_event.event_type IN
                  ('work_acknowledge', 'work_start', 'work_block', 'work_decline')
        )"""
        current_unmatched_message = """EXISTS (
            SELECT 1 FROM events unmatched_event
            WHERE unmatched_event.request_id=requests.request_id
              AND unmatched_event.event_type='provider_message_unmatched'
              AND NOT EXISTS (
                  SELECT 1 FROM message_links reconciled_link
                  WHERE reconciled_link.provider_message_id=
                        json_extract(unmatched_event.detail_json, '$.provider_message_id')
                    AND reconciled_link.exact_link_verified=1
              )
        )"""
        needs_attention = """(
            COALESCE(requests.work_state='blocked', 0)
            OR EXISTS (
                SELECT 1 FROM operations attention_operation
                WHERE attention_operation.request_id=requests.request_id
                  AND attention_operation.operation_state='uncertain'
            )
            OR {current_unmatched_message}
            OR (
                requests.mode='action_requested'
                AND requests.work_state='requested'
                AND requests.recovery_hold=0
                AND NOT {authenticated_report}
            )
        )""".format(
            current_unmatched_message=current_unmatched_message,
            authenticated_report=authenticated_report,
        )
        check_state = REQUEST_CHECK_SQL
        still_open = REQUEST_STILL_OPEN_SQL
        idle = f"julianday({REQUEST_LAST_ACTIVITY_SQL}) <= julianday(?) - 14"
        clauses = []
        values: list[object] = []
        accessible_ids = self._accessible_request_ids(actor, caller, read_only=True)
        if not accessible_ids:
            return Page((), None, self.now())
        clauses.append(f"request_id IN ({','.join('?' for _ in accessible_ids)})")
        values.extend(accessible_ids)
        if not bool(filters.get("include_recovery_held", False)):
            clauses.append("recovery_hold=0")
        if filters.get("request_id"):
            # One known request's list item, with no provider fetch: the hub's own handoff-note
            # keeper reads a request's title and task this way (N12a). Not on the HTTP/MCP lists.
            clauses.append("request_id=?")
            values.append(str(filters["request_id"]))
        if filters.get("project"):
            self.policy.require_project(caller.participant_id, str(filters["project"]))
            clauses.append("project=?")
            values.append(str(filters["project"]))
        if filters.get("work_state"):
            clauses.append("work_state=?")
            values.append(str(filters["work_state"]))
        if filters.get("recipient"):
            clauses.append("recipient_id=?")
            values.append(str(filters["recipient"]))
        if filters.get("attention") is not None:
            clauses.append(needs_attention if bool(filters["attention"]) else f"NOT {needs_attention}")
        if filters.get("open") is not None:
            daily_open = f"({still_open} AND NOT ({idle}))"
            clauses.append(daily_open if bool(filters["open"]) else f"NOT {daily_open}")
            values.append(as_of.isoformat())
        if filters.get("stale") is not None:
            stale = f"({still_open} AND ({idle}))"
            clauses.append(stale if bool(filters["stale"]) else f"NOT {stale}")
            values.append(as_of.isoformat())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.store.fetchall(
            f"""SELECT requests.*,
                COALESCE(requests.work_state='blocked', 0) AS attention_blocker,
                EXISTS (
                    SELECT 1 FROM operations attention_operation
                    WHERE attention_operation.request_id=requests.request_id
                      AND attention_operation.operation_state='uncertain'
                ) AS attention_ambiguous_send,
                {current_unmatched_message} AS attention_unmatched_message,
                (requests.mode='action_requested'
                 AND requests.work_state='requested'
                 AND requests.recovery_hold=0
                 AND NOT {authenticated_report}) AS attention_no_acknowledgement,
                (SELECT MAX(fetch_event.occurred_at) FROM events fetch_event
                 WHERE fetch_event.request_id=requests.request_id
                   AND fetch_event.event_type='source_fetch_attempted') AS last_source_fetch_at,
                (SELECT MAX(success_event.occurred_at) FROM events success_event
                 WHERE success_event.request_id=requests.request_id
                   AND success_event.event_type='source_fetch_succeeded') AS last_source_success_at,
                (SELECT MAX(report_event.occurred_at) FROM events report_event
                 WHERE report_event.request_id=requests.request_id
                   AND report_event.actor_id=requests.recipient_id
                   AND report_event.event_type IN
                       ('work_acknowledge', 'work_start', 'work_block', 'work_decline'))
                    AS last_authenticated_report_at,
                {check_state} AS check_state,
                {REQUEST_PROOF_SQL} AS proof,
                {REQUEST_PROOF_VERIFIED_SQL} AS proof_verified,
                {REQUEST_LAST_ACTIVITY_SQL} AS last_activity,
                (SELECT json_extract(created_event.detail_json, '$.task_id') FROM events created_event
                 WHERE created_event.request_id=requests.request_id
                   AND created_event.event_type='request_created'
                 ORDER BY created_event.sequence LIMIT 1) AS task_id
                FROM requests{where}
                ORDER BY requests.updated_at DESC, requests.request_id ASC
                LIMIT ? OFFSET ?""",
            (*values, limit + 1, offset),
        )
        items = tuple(self._request_item(row, as_of) for row in rows[:limit])
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return Page(items, next_cursor, as_of)

    @staticmethod
    def _request_item(row, as_of: datetime) -> dict:
        created_at = datetime.fromisoformat(row["created_at"])
        last_report_at = (
            datetime.fromisoformat(row["last_authenticated_report_at"])
            if row["last_authenticated_report_at"]
            else None
        )
        participation_since = last_report_at or created_at
        participation = {
            "state": "authenticated_report" if last_report_at else "no_acknowledgement",
            "summary": (
                f"last authenticated report at {last_report_at.isoformat()}"
                if last_report_at
                else f"no acknowledgement since {created_at.isoformat()}"
            ),
            "since": participation_since,
            "age_seconds": max(0, int((as_of - participation_since).total_seconds())),
        }
        attention_reasons = []
        if row["attention_blocker"]:
            attention_reasons.append("blocker")
        if row["attention_ambiguous_send"]:
            attention_reasons.append("ambiguous send")
        if row["attention_unmatched_message"]:
            attention_reasons.append("unmatched provider message")
        if row["attention_no_acknowledgement"]:
            attention_reasons.append("no acknowledgement")
        return {
            "request_id": row["request_id"],
            "mode": row["mode"],
            "project": row["project"],
            "requester_id": row["requester_id"],
            "recipient_id": row["recipient_id"],
            "objective": row["objective"],
            "sensitivity": row["sensitivity"],
            "work_state": row["work_state"],
            "revision": row["revision"],
            "recovery_hold": bool(row["recovery_hold"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
            "last_source_fetch_at": (
                datetime.fromisoformat(row["last_source_fetch_at"])
                if row["last_source_fetch_at"]
                else None
            ),
            "last_source_success_at": (
                datetime.fromisoformat(row["last_source_success_at"])
                if row["last_source_success_at"]
                else None
            ),
            "last_authenticated_report_at": last_report_at,
            "participation": participation,
            "attention": "; ".join(attention_reasons) or None,
            "check": row["check_state"],
            "proof": row["proof"],
            "proof_verified": bool(row["proof_verified"]),
            "last_activity": datetime.fromisoformat(row["last_activity"]),
            "task_id": row["task_id"] if isinstance(row["task_id"], str) else None,
        }

    @staticmethod
    def _cursor(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            value = int(cursor)
        except (TypeError, ValueError) as exc:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "invalid pagination cursor") from exc
        if value < 0:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "invalid pagination cursor")
        return value

    def requests_read(self, actor: Actor, request_id: str, message_cursor: str | None = None) -> RequestSnapshot:
        request = self._access(actor, self.store.request(request_id), read_only=True)
        local_messages = self._outbox_messages(request_id)
        content = local_messages
        availability = ContentAvailability.EMPTY if not local_messages else ContentAvailability.QUEUED_ONLY
        content_error = None
        next_cursor = None
        if request.thread_id and self.provider is not None:
            self._record_source_fetch(actor, request, "source_fetch_attempted", self.now())
            try:
                page = self.provider.fetch_thread(request.thread_id, message_cursor)
                provider_messages = tuple(self._coerce_message(item) for item in page.items)
                if any(item.thread_id != request.thread_id for item in provider_messages):
                    raise GremlinTownError(ErrorCode.THREAD_SCOPE_CONFLICT, "provider returned a mixed thread")
                content = self._correlate_provider_messages(request, provider_messages)
                self._record_source_success(actor, request, content, self.now())
                next_cursor = page.next_cursor
                available_count = sum(item.body_available and item.body is not None for item in provider_messages)
                if provider_messages and available_count == 0:
                    availability = ContentAvailability.UNAVAILABLE
                elif available_count < len(provider_messages) or next_cursor:
                    availability = ContentAvailability.PARTIAL
                else:
                    availability = ContentAvailability.COMPLETE
            except GremlinTownError:
                raise
            except Exception as exc:
                content_error = ReceiptError(ErrorCode.SOURCE_UNAVAILABLE, str(exc), True)
                availability = ContentAvailability.PARTIAL if local_messages else ContentAvailability.UNAVAILABLE
        elif local_messages and any(
            operation.operation_state is not OperationState.QUEUED
            for operation in self._message_operations(request_id)
            if operation.payload is not None
        ):
            availability = ContentAvailability.PARTIAL
        return RequestSnapshot(
            request_id=request.request_id,
            mode=request.mode,
            project=request.project,
            requester_id=request.requester_id,
            recipient_id=request.recipient_id,
            objective=request.objective,
            work_state=request.work_state,
            revision=request.revision,
            recovery_hold=request.recovery_hold,
            thread_id=request.thread_id,
            content_availability=availability,
            content=content,
            next_message_cursor=next_cursor,
            content_error=content_error,
            as_of=self.now(),
            task_id=self._request_task_id(request.request_id),
        )

    def _record_source_fetch(
        self, actor: Actor, request: RequestRecord, event_type: str, observed_at: datetime
    ) -> None:
        with self.store.transaction() as connection:
            self.store.append_event(
                connection,
                request_id=request.request_id,
                operation_id=None,
                actor_id=actor.actor_id,
                event_type=event_type,
                detail={"provider_thread_id": request.thread_id},
                occurred_at=observed_at,
            )

    def _record_source_success(
        self,
        actor: Actor,
        request: RequestRecord,
        messages: tuple[ProviderMessage, ...],
        observed_at: datetime,
    ) -> None:
        with self.store.transaction() as connection:
            self.store.append_event(
                connection,
                request_id=request.request_id,
                operation_id=None,
                actor_id=actor.actor_id,
                event_type="source_fetch_succeeded",
                detail={"provider_thread_id": request.thread_id},
                occurred_at=observed_at,
            )
            existing_message_ids = {
                __import__("json").loads(row["detail_json"]).get("provider_message_id")
                for row in connection.execute(
                    "SELECT detail_json FROM events WHERE event_type='provider_message_unmatched'"
                ).fetchall()
            }
            for message in messages:
                if message.exact_link_verified or message.message_id in existing_message_ids:
                    continue
                self.store.append_event(
                    connection,
                    request_id=request.request_id,
                    operation_id=None,
                    actor_id=actor.actor_id,
                    event_type="provider_message_unmatched",
                    detail={
                        "provider_message_id": message.message_id,
                        "provider_thread_id": message.thread_id,
                        "parent_message_id": message.parent_message_id,
                        "provider_observed_at": message.received_at.isoformat(),
                        "body_available": message.body_available,
                        "trust_label": "untrusted_external",
                    },
                    occurred_at=observed_at,
                )
                existing_message_ids.add(message.message_id)

    @staticmethod
    def _coerce_message(item) -> ProviderMessage:
        if isinstance(item, ProviderMessage):
            return item
        if isinstance(item, dict) and isinstance(item.get("message"), ProviderMessage):
            return item["message"]
        return ProviderMessage(**item)

    def _correlate_provider_messages(
        self, request: RequestRecord, messages: tuple[ProviderMessage, ...]
    ) -> tuple[ProviderMessage, ...]:
        message_by_id = {message.message_id: message for message in messages}
        correlated_by_id: dict[str, ProviderMessage] = {}
        trusted_provider_bodies: dict[str, bytes] = {}
        visiting: set[str] = set()

        def correlate(message: ProviderMessage) -> ProviderMessage:
            cached = correlated_by_id.get(message.message_id)
            if cached is not None:
                return cached
            if message.message_id in visiting:
                raise GremlinTownError(
                    ErrorCode.THREAD_SCOPE_CONFLICT,
                    "provider attribution contains a parent cycle",
                )
            visiting.add(message.message_id)
            link = self.store.fetchone(
                """SELECT link.request_id, link.operation_id,
                link.provider_thread_id, link.parent_message_id, link.actor_id,
                link.content_sha256, parent_operation.payload AS parent_payload
                FROM message_links link
                LEFT JOIN message_links parent_link
                  ON parent_link.provider_message_id=link.parent_message_id
                 AND parent_link.request_id=link.request_id
                 AND parent_link.exact_link_verified=1
                LEFT JOIN operations parent_operation
                  ON parent_operation.operation_id=parent_link.operation_id
                WHERE link.provider_message_id=?""",
                (message.message_id,),
            )
            if link is None:
                result = ProviderMessage(
                    message_id=message.message_id,
                    thread_id=message.thread_id,
                    parent_message_id=message.parent_message_id,
                    received_at=message.received_at,
                    body=message.body,
                    body_available=message.body_available,
                    content_source=ContentSource.PROVIDER,
                    trust_label="untrusted_external",
                    linked_operation_id=None,
                    attributed_actor_id=None,
                    exact_link_verified=False,
                )
                correlated_by_id[message.message_id] = result
                visiting.remove(message.message_id)
                return result
            if (
                link["request_id"] != request.request_id
                or link["provider_thread_id"] != request.thread_id
                or message.parent_message_id != link["parent_message_id"]
            ):
                raise GremlinTownError(
                    ErrorCode.THREAD_SCOPE_CONFLICT,
                    "provider attribution does not match the durable operation link",
                )
            canonical_body = message.body
            digest_matches = not message.body_available or message.body is None
            if message.body_available and message.body is not None:
                if link["parent_message_id"] is not None and link["parent_payload"] is None:
                    digest_matches = False
                else:
                    known_parent = link["parent_payload"]
                    try:
                        wire = parse_wire_message(message.body, known_parent=known_parent)
                    except FramingError:
                        parent_id = link["parent_message_id"]
                        if parent_id is None:
                            digest_matches = False
                        else:
                            parent = message_by_id.get(parent_id)
                            if parent is None:
                                if self.provider is None:
                                    raise GremlinTownError(
                                        ErrorCode.SOURCE_UNAVAILABLE,
                                        "provider is unavailable for parent verification",
                                    )
                                parent = self._coerce_message(self.provider.fetch_message(parent_id))
                                if parent.message_id != parent_id or parent.thread_id != request.thread_id:
                                    raise GremlinTownError(
                                        ErrorCode.THREAD_SCOPE_CONFLICT,
                                        "provider returned a mismatched parent message",
                                    )
                                message_by_id[parent_id] = parent
                            correlate(parent)
                            rendered_parent = trusted_provider_bodies.get(parent_id)
                            if rendered_parent is None:
                                digest_matches = False
                            else:
                                try:
                                    wire = parse_wire_message(
                                        message.body,
                                        known_parent=rendered_parent,
                                    )
                                except FramingError:
                                    digest_matches = False
                                else:
                                    canonical_body = canonical_wire_bytes(wire)
                                    digest_matches = (
                                        hashlib.sha256(canonical_body).hexdigest()
                                        == link["content_sha256"]
                                    )
                    else:
                        canonical_body = canonical_wire_bytes(wire)
                        digest_matches = (
                            hashlib.sha256(canonical_body).hexdigest()
                            == link["content_sha256"]
                        )
            if not digest_matches:
                raise GremlinTownError(
                    ErrorCode.THREAD_SCOPE_CONFLICT,
                    "provider attribution does not match the durable operation link",
                )
            if message.body_available and message.body is not None:
                trusted_provider_bodies[message.message_id] = message.body
            result = ProviderMessage(
                message_id=message.message_id,
                thread_id=message.thread_id,
                parent_message_id=message.parent_message_id,
                received_at=message.received_at,
                body=canonical_body,
                body_available=message.body_available,
                content_source=ContentSource.PROVIDER,
                trust_label="authenticated_operation",
                linked_operation_id=link["operation_id"],
                attributed_actor_id=link["actor_id"],
                exact_link_verified=True,
            )
            correlated_by_id[message.message_id] = result
            visiting.remove(message.message_id)
            return result

        return tuple(correlate(message) for message in messages)

    def _message_operations(self, request_id: str) -> tuple[OperationRecord, ...]:
        rows = self.store.fetchall(
            "SELECT * FROM operations WHERE request_id=? AND message_kind IS NOT NULL ORDER BY accepted_at, operation_id",
            (request_id,),
        )
        return tuple(self.store._operation_row(row) for row in rows)

    def _outbox_messages(self, request_id: str) -> tuple[ProviderMessage, ...]:
        messages = []
        for operation in self._message_operations(request_id):
            if operation.payload is None:
                continue
            messages.append(
                ProviderMessage(
                    message_id=operation.provider_message_id or f"outbox:{operation.operation_id}",
                    thread_id=operation.provider_thread_id or f"local:{request_id}",
                    parent_message_id=operation.parent_message_id,
                    received_at=operation.provider_accepted_at or operation.accepted_at,
                    body=operation.payload,
                    body_available=True,
                    content_source=ContentSource.OUTBOX,
                    trust_label="authenticated_operation",
                    linked_operation_id=operation.operation_id,
                    attributed_actor_id=operation.actor_id,
                    exact_link_verified=bool(operation.provider_message_id),
                )
            )
        return tuple(messages)

    def requests_report(self, actor: Actor, command: ReportRequest) -> Receipt:
        digest = canonical_digest(command)
        cached = self._cached(actor, command.client_operation_id, digest)
        if cached:
            return cached
        request = self._access(actor, self.store.request(command.request_id))
        target = self._validate_report(actor, command, request)
        return self._local_mutation(actor, command, digest, request, target, f"work_{command.action.value}", command.reason)

    def _validate_report(
        self, actor: Actor, command: ReportRequest, request: RequestRecord
    ) -> WorkState:
        if actor.actor_id != request.recipient_id:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only the recipient may report work state")
        if request.mode is RequestMode.CONTEXT_ONLY or request.recovery_hold:
            code = ErrorCode.RECOVERY_HOLD if request.recovery_hold else ErrorCode.INVALID_TRANSITION
            raise GremlinTownError(code, "request is not actionable")
        if request.revision != command.expected_revision:
            raise GremlinTownError(ErrorCode.REVISION_CONFLICT, "request revision is stale")
        transitions = {
            ReportAction.ACKNOWLEDGE: ({WorkState.REQUESTED}, WorkState.ACKNOWLEDGED),
            ReportAction.START: ({WorkState.REQUESTED, WorkState.ACKNOWLEDGED, WorkState.BLOCKED}, WorkState.IN_PROGRESS),
            ReportAction.BLOCK: ({WorkState.REQUESTED, WorkState.ACKNOWLEDGED, WorkState.IN_PROGRESS}, WorkState.BLOCKED),
            ReportAction.DECLINE: (NONTERMINAL_STATES, WorkState.DECLINED),
        }
        allowed, target = transitions[command.action]
        if request.work_state not in allowed:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "work-state transition is not allowed")
        if command.action in {ReportAction.BLOCK, ReportAction.DECLINE} and not command.reason:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "this report requires a reason")
        return target

    def _local_mutation(self, actor, command, digest, request, target, event_type, reason) -> Receipt:
        operation_id = self.id_factory("op")
        at = self.now()
        with self.store.transaction() as connection:
            current = self._access(actor, self.store.request(request.request_id, connection))
            target = self._validate_report(actor, command, current)
            new_revision = current.revision + 1
            connection.execute(
                "UPDATE requests SET work_state=?, revision=?, updated_at=? WHERE request_id=?",
                (target.value if target else None, new_revision, at.isoformat(), request.request_id),
            )
            connection.execute(
                """INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id,
                operation_kind, input_digest, accepted_at, operation_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                (operation_id, command.client_operation_id, actor.actor_id, request.request_id, event_type, digest, at.isoformat()),
            )
            self.store.append_event(
                connection,
                request_id=request.request_id,
                operation_id=operation_id,
                actor_id=actor.actor_id,
                event_type=event_type,
                detail={"reason": reason, "revision": new_revision},
                occurred_at=at,
            )
        return self._receipt(self.store.operation(operation_id))

    def requests_reply(self, actor: Actor, command: ReplyRequest) -> Receipt:
        try:
            proof_kind(command.proof)
        except ValueError as exc:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, str(exc)) from exc
        if command.proof is not None and command.kind is not MessageKind.RESULT:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "proof belongs on a result reply")
        fields = asdict(command)
        if command.proof is None:
            fields.pop("proof")  # preserve replay digests of pre-W4 replies
        digest = canonical_digest(fields)
        cached = self._cached(actor, command.client_operation_id, digest)
        if cached:
            return cached
        request = self._access(actor, self.store.request(command.request_id))
        self._validate_reply(actor, command, request)
        if command.packet.kind is not command.kind or command.kind not in {MessageKind.RESULT, MessageKind.CLARIFICATION}:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "reply kind is invalid")
        if command.packet.schema_version != 1:
            raise GremlinTownError(ErrorCode.UNSUPPORTED_SCHEMA, "only packet schema version 1 is supported")
        if len(packet_bytes(command.packet)) > MAX_PACKET_BYTES:
            raise GremlinTownError(ErrorCode.PACKET_TOO_LARGE, "packet exceeds the configured byte limit")
        operation_id = self.id_factory("op")
        at = self.now()
        destination_id = (
            request.recipient_id if actor.actor_id == request.requester_id else request.requester_id
        )
        self.policy.require_route(request.project, actor.actor_id, destination_id, command.packet.sensitivity)
        destination = self.policy.participant(destination_id)
        payload = canonical_bytes(
            WireMessage(
                request_id=request.request_id,
                operation_id=operation_id,
                sender=actor.actor_id,
                recipient=destination_id,
                created_at=at,
                packet=command.packet,
            )
        )
        payload_sha = hashlib.sha256(payload).hexdigest()
        try:
            with self.store.transaction() as connection:
                current = self._access(actor, self.store.request(command.request_id, connection))
                self._validate_reply(actor, command, current)
                parent = connection.execute(
                    """SELECT provider_message_id FROM operations WHERE request_id=?
                    AND provider_message_id IS NOT NULL ORDER BY provider_accepted_at LIMIT 1""",
                    (current.request_id,),
                ).fetchone()
                if parent is None:
                    raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "initial provider message is not accepted")
                if command.kind is MessageKind.RESULT:
                    pending = connection.execute(
                        """SELECT 1 FROM operations WHERE request_id=? AND operation_kind='reply_result'
                        AND operation_state IN ('queued','sending','uncertain')""",
                        (current.request_id,),
                    ).fetchone()
                    if pending:
                        raise GremlinTownError(ErrorCode.RESULT_PENDING, "a result operation is already pending")
                connection.execute(
                    """
                    INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id,
                        operation_kind, message_kind, input_digest, accepted_at, operation_state,
                        payload, payload_sha256, provider_retry_key, recipient_address,
                        route_source_id, route_destination_id, route_sensitivity, parent_message_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        command.client_operation_id,
                        actor.actor_id,
                        current.request_id,
                        "reply_result" if command.kind is MessageKind.RESULT else "reply_clarification",
                        command.kind.value,
                        digest,
                        at.isoformat(),
                        payload,
                        payload_sha,
                        f"gt-{operation_id.removeprefix('op_')}",
                        destination.address,
                        actor.actor_id,
                        destination_id,
                        command.packet.sensitivity,
                        parent["provider_message_id"],
                    ),
                )
                connection.execute(
                    "UPDATE requests SET revision=revision+1, updated_at=? WHERE request_id=?",
                    (at.isoformat(), current.request_id),
                )
                self.store.append_event(
                    connection,
                    request_id=current.request_id,
                    operation_id=operation_id,
                    actor_id=actor.actor_id,
                    event_type="result_reserved" if command.kind is MessageKind.RESULT else "clarification_queued",
                    detail={"proof": command.proof} if command.proof is not None else {},
                    occurred_at=at,
                )
        except sqlite3.IntegrityError as exc:
            if "one_pending_result_per_request" in str(exc) or "UNIQUE constraint failed: operations.request_id" in str(exc):
                raise GremlinTownError(ErrorCode.RESULT_PENDING, "a result operation is already pending") from exc
            raise
        return self._receipt(self.store.operation(operation_id))

    def local_request_result(self, actor: Actor, request_id: str) -> tuple[str, datetime] | None:
        """Internal read of an accepted recipient result, with no provider fetch or network wait."""
        request = self._access(actor, self.store.request(request_id), read_only=True)
        if actor.actor_id != request.requester_id:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only the requester may read its local result")
        if request.work_state is not WorkState.RESULT_RETURNED or request.recovery_hold:
            return None
        row = self.store.fetchone("""SELECT payload,provider_accepted_at FROM operations
            WHERE request_id=? AND operation_kind='reply_result' AND actor_id=?
            AND operation_state='accepted' ORDER BY provider_accepted_at DESC LIMIT 1""",
            (request_id, request.recipient_id))
        if row is None:
            return None
        wire = json.loads(row["payload"])
        if wire["packet"]["kind"] != "result" or wire["sender"] != request.recipient_id:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "local result attribution does not match recipient")
        return wire["packet"]["context"], datetime.fromisoformat(row["provider_accepted_at"])

    def hub_delivery_requests(self, actor: Actor, *, hub_participant: str) -> tuple[dict, ...]:
        self.policy.authenticate(actor)
        if actor.actor_id != hub_participant:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only the hub may read its own deliveries")
        rows = self.store.fetchall(
            """SELECT requests.request_id, operations.client_operation_id FROM requests JOIN operations
                ON operations.request_id=requests.request_id AND operations.operation_kind='create'
                WHERE requests.requester_id=? AND requests.work_state NOT IN ('declined','cancelled')
                AND NOT EXISTS (SELECT 1 FROM events WHERE events.request_id=requests.request_id
                    AND events.event_type='hub_delivery_closed')""", (hub_participant,),
        )
        return tuple(dict(row) for row in rows)

    def close_hub_delivery(
        self, actor: Actor, request_id: str, *, hub_participant: str, proof: str,
    ) -> bool:
        """Internal poll action, absent from every HTTP/MCP route. Only the configured
        hub sender may close its own review or moment delivery; replay is a no-op."""
        self.policy.authenticate(actor)
        if actor.actor_id != hub_participant:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only the hub may close its own delivery")
        with self.store.transaction() as connection:
            request = self._access(actor, self.store.request(request_id, connection))
            created = connection.execute(
                "SELECT client_operation_id FROM operations WHERE request_id=? AND operation_kind='create'",
                (request_id,),
            ).fetchone()
            if request.requester_id != hub_participant or created is None or not (
                created[0].startswith("review-") or created[0].startswith("moment-")
            ):
                raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only the hub's review and moment deliveries may close")
            if connection.execute(
                "SELECT 1 FROM events WHERE request_id=? AND event_type='hub_delivery_closed'", (request_id,),
            ).fetchone():
                return False
            self.store.append_event(
                connection, request_id=request_id, operation_id=None, actor_id=actor.actor_id,
                event_type="hub_delivery_closed", detail={"proof": proof}, occurred_at=self.now(),
            )
            return True

    @staticmethod
    def _validate_reply(actor: Actor, command: ReplyRequest, request: RequestRecord) -> None:
        if request.mode is RequestMode.CONTEXT_ONLY and command.kind is MessageKind.RESULT:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "context-only requests cannot receive results")
        if request.recovery_hold:
            raise GremlinTownError(ErrorCode.RECOVERY_HOLD, "request is held after restoration")
        if command.kind is MessageKind.RESULT and actor.actor_id != request.recipient_id:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only the recipient may return a result")
        if command.kind is MessageKind.CLARIFICATION and actor.actor_id not in {
            request.requester_id,
            request.recipient_id,
        }:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only a request participant may clarify")
        if request.work_state in TERMINAL_STATES:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "terminal requests do not reopen")
        if request.revision != command.expected_revision:
            raise GremlinTownError(ErrorCode.REVISION_CONFLICT, "request revision is stale")

    def requests_cancel(self, actor: Actor, command: CancelRequest) -> Receipt:
        digest = canonical_digest(command)
        cached = self._cached(actor, command.client_operation_id, digest)
        if cached:
            return cached
        request = self._access(actor, self.store.request(command.request_id))
        self._validate_cancel(actor, command, request)
        operation_id = self.id_factory("op")
        at = self.now()
        with self.store.transaction() as connection:
            current = self._access(actor, self.store.request(command.request_id, connection))
            self._validate_cancel(actor, command, current)
            connection.execute(
                "UPDATE requests SET work_state='cancelled', revision=revision+1, updated_at=? WHERE request_id=?",
                (at.isoformat(), current.request_id),
            )
            connection.execute(
                "UPDATE operations SET operation_state='cancelled', error_message='cancelled before dispatch', retryable=0 WHERE request_id=? AND operation_state='queued'",
                (current.request_id,),
            )
            connection.execute(
                """INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id,
                operation_kind, input_digest, accepted_at, operation_state)
                VALUES (?, ?, ?, ?, 'cancel', ?, ?, NULL)""",
                (operation_id, command.client_operation_id, actor.actor_id, current.request_id, digest, at.isoformat()),
            )
            self.store.append_event(
                connection,
                request_id=current.request_id,
                operation_id=operation_id,
                actor_id=actor.actor_id,
                event_type="request_cancelled",
                detail={"reason": command.reason},
                occurred_at=at,
            )
        return self._receipt(self.store.operation(operation_id))

    def _validate_cancel(self, actor: Actor, command: CancelRequest, request: RequestRecord) -> None:
        caller = self.policy.authenticate(actor)
        if actor.actor_id != request.requester_id and not caller.is_operator:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "only the requester or operator may cancel")
        if request.recovery_hold:
            raise GremlinTownError(ErrorCode.RECOVERY_HOLD, "request is held after restoration")
        if request.mode is RequestMode.CONTEXT_ONLY or request.work_state in TERMINAL_STATES:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "request cannot be cancelled")
        if request.revision != command.expected_revision:
            raise GremlinTownError(ErrorCode.REVISION_CONFLICT, "request revision is stale")

    def requests_check(self, actor: Actor, command: CheckRequest) -> Receipt:
        """Record that someone other than the doer checked a returned result. Never automatic."""
        digest = canonical_digest(command)
        cached = self._cached(actor, command.client_operation_id, digest, read_only=True)
        if cached:
            return cached
        request = self._access(actor, self.store.request(command.request_id), read_only=True)
        self._validate_check(actor, command, request)
        operation_id = self.id_factory("op")
        at = self.now()
        event_type = (
            "result_check_accepted" if command.outcome is CheckOutcome.ACCEPTED else "result_check_rejected"
        )
        with self.store.transaction() as connection:
            current = self._access(actor, self.store.request(command.request_id, connection), read_only=True)
            self._validate_check(actor, command, current)
            new_revision = current.revision + 1
            connection.execute(
                "UPDATE requests SET revision=?, updated_at=? WHERE request_id=?",
                (new_revision, at.isoformat(), current.request_id),
            )
            connection.execute(
                """INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id,
                operation_kind, input_digest, accepted_at, operation_state)
                VALUES (?, ?, ?, ?, 'check', ?, ?, NULL)""",
                (operation_id, command.client_operation_id, actor.actor_id, current.request_id, digest, at.isoformat()),
            )
            self.store.append_event(
                connection,
                request_id=current.request_id,
                operation_id=operation_id,
                actor_id=actor.actor_id,
                event_type=event_type,
                detail={"evidence": command.evidence, "revision": new_revision},
                occurred_at=at,
            )
        return self._receipt(self.store.operation(operation_id))

    @staticmethod
    def _validate_check(actor: Actor, command: CheckRequest, request: RequestRecord) -> None:
        if actor.actor_id == request.recipient_id:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "the recipient cannot check its own result")
        if request.work_state is not WorkState.RESULT_RETURNED:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "only a returned result can be checked")
        if request.revision != command.expected_revision:
            raise GremlinTownError(ErrorCode.REVISION_CONFLICT, "request revision is stale")

    def recovery_dispose(self, actor: Actor, command: RecoveryRequest) -> Receipt:
        self.policy.require_operator(actor)
        digest = canonical_digest(command)
        cached = self._cached(actor, command.client_operation_id, digest)
        if cached:
            return cached
        request = self._access(actor, self.store.request(command.request_id))
        if not request.recovery_hold:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "request has no recovery hold")
        if request.revision != command.expected_revision:
            raise GremlinTownError(ErrorCode.REVISION_CONFLICT, "request revision is stale")
        self._validate_recovery(command, request)
        operation_id = self.id_factory("op")
        at = self.now()
        keep = command.disposition is RecoveryDisposition.KEEP_HELD
        with self.store.transaction() as connection:
            current = self._access(actor, self.store.request(command.request_id, connection))
            self._validate_recovery(command, current)
            targets = {
                RecoveryDisposition.KEEP_HELD: current.work_state,
                RecoveryDisposition.RELEASE_ACTIONABLE: current.work_state,
                RecoveryDisposition.CONFIRM_CANCELLED: WorkState.CANCELLED,
                RecoveryDisposition.CONFIRM_DECLINED: WorkState.DECLINED,
            }
            connection.execute(
                "UPDATE requests SET work_state=?, recovery_hold=?, revision=revision+1, updated_at=? WHERE request_id=?",
                (targets[command.disposition].value if targets[command.disposition] else None, int(keep), at.isoformat(), current.request_id),
            )
            if not keep:
                if command.disposition in {RecoveryDisposition.CONFIRM_CANCELLED, RecoveryDisposition.CONFIRM_DECLINED}:
                    connection.execute(
                        "UPDATE operations SET operation_state='cancelled', recovery_hold=0, retryable=0 WHERE request_id=? AND operation_state IN ('queued','sending','uncertain')",
                        (current.request_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE operations SET recovery_hold=0 WHERE request_id=?", (current.request_id,)
                    )
            connection.execute(
                """INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id,
                operation_kind, input_digest, accepted_at, operation_state)
                VALUES (?, ?, ?, ?, 'recovery_dispose', ?, ?, NULL)""",
                (operation_id, command.client_operation_id, actor.actor_id, current.request_id, digest, at.isoformat()),
            )
            self.store.append_event(
                connection,
                request_id=current.request_id,
                operation_id=operation_id,
                actor_id=actor.actor_id,
                event_type="recovery_disposed",
                detail={"disposition": command.disposition.value, "reason": command.reason},
                occurred_at=at,
            )
        return self._receipt(self.store.operation(operation_id))

    @staticmethod
    def _validate_recovery(command: RecoveryRequest, request: RequestRecord) -> None:
        if not request.recovery_hold:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "request has no recovery hold")
        if request.revision != command.expected_revision:
            raise GremlinTownError(ErrorCode.REVISION_CONFLICT, "request revision is stale")
        if command.disposition is RecoveryDisposition.RELEASE_ACTIONABLE and request.work_state in TERMINAL_STATES:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "terminal requests cannot be released as actionable")

    def operations_get(self, actor: Actor, operation_id: str) -> Receipt:
        operation, request = self._operation_access(actor, self.store.operation(operation_id))
        return self._receipt(operation, request)

    def events_list(self, actor: Actor, filters: Mapping, cursor: str | None = None) -> Page:
        caller = self.policy.authenticate(actor)
        request_id = filters.get("request_id")
        event_type = filters.get("event_type")
        if request_id:
            self._access(actor, self.store.request(str(request_id)), read_only=True)
        limit = max(1, min(int(filters.get("limit", 100)), 100))
        after = self._cursor(cursor)
        clauses = ["e.sequence>?"]
        values: list[object] = [after]
        if request_id:
            clauses.append("e.request_id=?")
            values.append(request_id)
        if event_type is not None:
            clauses.append("e.event_type=?")
            values.append(event_type)
        accessible_ids = self._accessible_request_ids(actor, caller, read_only=True)
        if not accessible_ids:
            return Page((), None, self.now())
        clauses.append(f"e.request_id IN ({','.join('?' for _ in accessible_ids)})")
        values.extend(accessible_ids)
        rows = self.store.fetchall(
            f"""SELECT e.* FROM events e JOIN requests r ON r.request_id=e.request_id
            WHERE {' AND '.join(clauses)} ORDER BY e.sequence LIMIT ?""",
            (*values, limit + 1),
        )
        visible = rows[:limit]
        items = tuple(
            {
                "sequence": row["sequence"],
                "request_id": row["request_id"],
                "operation_id": row["operation_id"],
                "actor_id": row["actor_id"],
                "event_type": row["event_type"],
                "detail": __import__("json").loads(row["detail_json"]),
                "occurred_at": row["occurred_at"],
            }
            for row in visible
        )
        next_cursor = str(visible[-1]["sequence"]) if len(rows) > limit and visible else None
        return Page(items, next_cursor, self.now())

    def _require_participant_access(self, participant_id: str, request: RequestRecord) -> None:
        participant = self.policy.require_project(participant_id, request.project)
        if not participant.is_operator and participant_id not in {
            request.requester_id,
            request.recipient_id,
        }:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "linked request is not accessible to the new recipient")
