from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from gremlin_town.contracts import (
    ErrorCode,
    MessageKind,
    OperationState,
    ProviderReference,
    Receipt,
    ReceiptError,
    WorkState,
)
from gremlin_town.domain import GremlinTownError, Policy
from gremlin_town.domain.models import OperationRecord, RequestRecord
from gremlin_town.storage import SQLiteStore
from gremlin_town.transport.errors import TransportError


TERMINAL = {WorkState.RESULT_RETURNED, WorkState.DECLINED, WorkState.CANCELLED}
RETRY_BACKOFF_FLOOR = timedelta(seconds=1)
RETRY_BACKOFF_CAP = timedelta(minutes=5)


class OutboxWorker:
    def __init__(
        self,
        store: SQLiteStore,
        policy: Policy,
        provider,
        *,
        now: Callable[[], datetime] | None = None,
        retry_window: timedelta = timedelta(hours=24),
    ) -> None:
        self.store = store
        self.policy = policy
        self.provider = provider
        self.now = now or (lambda: datetime.now(UTC))
        self.retry_window = retry_window

    def _receipt(self, operation: OperationRecord) -> Receipt:
        request = self.store.request(operation.request_id)
        provider = None
        if operation.provider_message_id:
            provider = ProviderReference(
                operation.provider_message_id,
                operation.provider_thread_id,
                operation.provider_accepted_at,
                operation.provider_parent_message_id,
            )
        error = None
        if operation.error_code:
            error = ReceiptError(ErrorCode(operation.error_code), operation.error_message or operation.error_code, operation.retryable)
        return Receipt(
            operation.operation_id,
            operation.request_id,
            operation.accepted_at,
            operation.operation_state,
            request.work_state,
            request.revision,
            operation.actor_id,
            provider,
            error,
        )

    def _dispatch_authorized(self, request: RequestRecord, operation: OperationRecord) -> None:
        request = self.store.request(request.request_id)
        operation = self.store.operation(operation.operation_id)
        if request.recovery_hold or operation.recovery_hold:
            raise GremlinTownError(ErrorCode.RECOVERY_HOLD, "restored operation is held")
        self.policy.require_project(request.requester_id, request.project)
        self.policy.require_project(request.recipient_id, request.project)
        self.policy.require_route(
            request.project,
            operation.route_source_id,
            operation.route_destination_id,
            operation.route_sensitivity,
        )
        destination = self.policy.participant(operation.route_destination_id)
        if destination.address != operation.recipient_address:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "configured destination changed before dispatch")
        if request.work_state is WorkState.CANCELLED:
            raise GremlinTownError(ErrorCode.INVALID_TRANSITION, "cancelled work cannot be dispatched")

    def dispatch_once(self) -> Receipt | None:
        # Refresh commits between dispatches, never between authorization and send.
        # A send already in progress settles first; refresh waits for this boundary.
        with self.policy.dispatch_lock:
            return self._dispatch_once_locked()

    def _dispatch_once_locked(self) -> Receipt | None:
        operation = self.store.next_dispatchable()
        if operation is None:
            held = self.store.fetchone(
                """SELECT 1 FROM operations o JOIN requests r ON r.request_id=o.request_id
                WHERE o.operation_state='queued' AND (o.recovery_hold=1 OR r.recovery_hold=1) LIMIT 1"""
            )
            if held:
                raise GremlinTownError(ErrorCode.RECOVERY_HOLD, "queued operations are held after restoration")
            return None
        if self._retry_window_expired(operation):
            self._mark_recovery_required(operation)
            return self._receipt(self.store.operation(operation.operation_id))
        request = self.store.request(operation.request_id)
        try:
            self._dispatch_authorized(request, operation)
        except GremlinTownError as exc:
            self._settle_dispatch_denial(operation.operation_id, exc)
            return self._receipt(self.store.operation(operation.operation_id))
        with self.store.transaction() as connection:
            changed = connection.execute(
                """UPDATE operations SET operation_state='sending', attempt_started_at=?,
                    attempt_count=attempt_count+1, next_attempt_at=NULL, error_code=NULL,
                    error_message=NULL, retryable=0
                WHERE operation_id=? AND operation_state='queued'""",
                (self.now().isoformat(), operation.operation_id),
            ).rowcount
            if not changed:
                return None
            self.store.append_event(
                connection,
                request_id=request.request_id,
                operation_id=operation.operation_id,
                actor_id="system:outbox",
                event_type="dispatch_started",
                detail={},
            )
        # Recheck both configuration and request state immediately before I/O.
        operation = self.store.operation(operation.operation_id)
        request = self.store.request(operation.request_id)
        try:
            self._dispatch_authorized(request, operation)
        except GremlinTownError as exc:
            self._settle_dispatch_denial(operation.operation_id, exc)
            return self._receipt(self.store.operation(operation.operation_id))
        envelope = self.store.outbound_envelope(operation.operation_id)
        try:
            reference = self.provider.send(envelope)
        except TransportError as exc:
            if exc.accepted:
                self._mark_uncertain(
                    operation.operation_id,
                    exc.message,
                    transport_error_code=exc.code,
                    transport_status=exc.status,
                )
            elif exc.retryable:
                self._schedule_retry(operation.operation_id, exc)
            else:
                self._mark_failed(
                    operation.operation_id,
                    exc.message,
                    transport_error_code=exc.code,
                    transport_status=exc.status,
                )
        except (TimeoutError, ConnectionError, OSError) as exc:
            self._mark_uncertain(operation.operation_id, str(exc))
        except Exception as exc:
            self._mark_failed(operation.operation_id, str(exc))
        else:
            self._accept(operation.operation_id, reference)
        return self._receipt(self.store.operation(operation.operation_id))

    def _backoff(self, attempt_count: int, retry_after: float | None = None) -> timedelta:
        exponent = min(max(attempt_count - 1, 0), 20)
        seconds = min(
            RETRY_BACKOFF_CAP.total_seconds(),
            RETRY_BACKOFF_FLOOR.total_seconds() * (2**exponent),
        )
        if retry_after is not None:
            seconds = max(seconds, max(0.0, retry_after))
        return timedelta(seconds=seconds)

    def _schedule_retry(self, operation_id: str, error: TransportError) -> None:
        with self.store.transaction() as connection:
            operation = self.store.operation(operation_id, connection)
            request = self.store.request(operation.request_id, connection)
            if operation.operation_state is not OperationState.SENDING:
                return
            try:
                self._dispatch_authorized(request, operation)
            except GremlinTownError as exc:
                self._settle_dispatch_denial_in_transaction(connection, operation, request, exc)
                return
            next_attempt_at = self.now() + self._backoff(operation.attempt_count, error.retry_after)
            connection.execute(
                """UPDATE operations SET operation_state='queued', next_attempt_at=?,
                    error_code='SOURCE_UNAVAILABLE', error_message=?, retryable=1,
                    transport_error_code=?, transport_status=? WHERE operation_id=?""",
                (next_attempt_at.isoformat(), error.message, error.code, error.status, operation_id),
            )
            self.store.append_event(
                connection,
                request_id=operation.request_id,
                operation_id=operation_id,
                actor_id="system:outbox",
                event_type="dispatch_retry_scheduled",
                detail={
                    "attempt_count": operation.attempt_count,
                    "next_attempt_at": next_attempt_at.isoformat(),
                    "transport_error_code": error.code,
                    "transport_status": error.status,
                },
            )

    def _settle_dispatch_denial(self, operation_id: str, error: GremlinTownError) -> None:
        with self.store.transaction() as connection:
            operation = self.store.operation(operation_id, connection)
            request = self.store.request(operation.request_id, connection)
            self._settle_dispatch_denial_in_transaction(connection, operation, request, error)

    def _settle_dispatch_denial_in_transaction(
        self,
        connection,
        operation: OperationRecord,
        request: RequestRecord,
        error: GremlinTownError,
    ) -> None:
        if operation.operation_state not in {OperationState.QUEUED, OperationState.SENDING}:
            return
        if request.work_state is WorkState.CANCELLED or operation.operation_state is OperationState.CANCELLED:
            state = OperationState.CANCELLED
            event_type = "dispatch_cancelled"
            connection.execute(
                """UPDATE operations SET operation_state='cancelled', error_code=NULL,
                    error_message='cancelled before provider acceptance', retryable=0,
                    next_attempt_at=NULL WHERE operation_id=?""",
                (operation.operation_id,),
            )
        elif request.recovery_hold or operation.recovery_hold or error.code is ErrorCode.RECOVERY_HOLD:
            state = OperationState.QUEUED
            event_type = "dispatch_held"
            connection.execute(
                """UPDATE operations SET operation_state='queued', recovery_hold=1,
                    error_code='RECOVERY_HOLD', error_message=?, retryable=0,
                    next_attempt_at=NULL WHERE operation_id=?""",
                (error.message, operation.operation_id),
            )
        else:
            state = OperationState.FAILED
            event_type = "dispatch_denied"
            connection.execute(
                """UPDATE operations SET operation_state='failed', error_code=?,
                    error_message=?, retryable=0, next_attempt_at=NULL WHERE operation_id=?""",
                (error.code.value, error.message, operation.operation_id),
            )
        self.store.append_event(
            connection,
            request_id=request.request_id,
            operation_id=operation.operation_id,
            actor_id="system:outbox",
            event_type=event_type,
            detail={"code": error.code.value, "state": state.value},
        )

    def _mark_uncertain(
        self,
        operation_id: str,
        message: str,
        *,
        transport_error_code: str | None = None,
        transport_status: int | None = None,
    ) -> None:
        with self.store.transaction() as connection:
            operation = self.store.operation(operation_id, connection)
            connection.execute(
                """UPDATE operations SET operation_state='uncertain', error_code=NULL,
                    error_message=?, retryable=1, next_attempt_at=NULL,
                    transport_error_code=?, transport_status=?,
                    uncertain_since=COALESCE(uncertain_since, ?)
                WHERE operation_id=?""",
                (
                    message,
                    transport_error_code,
                    transport_status,
                    self.now().isoformat(),
                    operation_id,
                ),
            )
            self.store.append_event(
                connection,
                request_id=operation.request_id,
                operation_id=operation_id,
                actor_id="system:outbox",
                event_type="dispatch_uncertain",
                detail={"message": message},
            )

    def _mark_failed(
        self,
        operation_id: str,
        message: str,
        *,
        transport_error_code: str | None = None,
        transport_status: int | None = None,
    ) -> None:
        with self.store.transaction() as connection:
            operation = self.store.operation(operation_id, connection)
            connection.execute(
                """UPDATE operations SET operation_state='failed', error_code='SOURCE_UNAVAILABLE',
                    error_message=?, retryable=0, next_attempt_at=NULL,
                    transport_error_code=?, transport_status=? WHERE operation_id=?""",
                (message, transport_error_code, transport_status, operation_id),
            )
            self.store.append_event(
                connection,
                request_id=operation.request_id,
                operation_id=operation_id,
                actor_id="system:outbox",
                event_type="dispatch_failed",
                detail={"message": message},
            )

    def _accept(self, operation_id: str, reference: ProviderReference) -> None:
        with self.store.transaction() as connection:
            operation = self.store.operation(operation_id, connection)
            request = self.store.request(operation.request_id, connection)
            if request.thread_id and request.thread_id != reference.thread_id:
                raise GremlinTownError(ErrorCode.THREAD_SCOPE_CONFLICT, "provider response changed request thread")
            links = connection.execute(
                """SELECT * FROM message_links
                WHERE provider_message_id=? OR operation_id=?""",
                (reference.message_id, operation_id),
            ).fetchall()
            exact_link = len(links) == 1 and self._message_link_matches(
                links[0], operation, request, reference
            )
            if links and not exact_link:
                connection.execute(
                    """UPDATE operations SET operation_state='uncertain',
                        error_code='THREAD_SCOPE_CONFLICT',
                        error_message='provider message identity conflicts with a durable link',
                        retryable=0, recovery_required=1, next_attempt_at=NULL
                    WHERE operation_id=? AND operation_state!='accepted'""",
                    (operation_id,),
                )
                self.store.append_event(
                    connection,
                    request_id=request.request_id,
                    operation_id=operation_id,
                    actor_id="system:outbox",
                    event_type="provider_message_conflict",
                    detail={"provider_message_id": reference.message_id},
                )
                return
            if exact_link and operation.operation_state is OperationState.ACCEPTED:
                return
            if not exact_link:
                connection.execute(
                    """INSERT INTO message_links(provider_message_id, request_id, operation_id,
                    provider_thread_id, parent_message_id, message_kind, actor_id, content_sha256,
                    accepted_at, trust_label, exact_link_verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        reference.message_id,
                        request.request_id,
                        operation_id,
                        reference.thread_id,
                        reference.parent_message_id,
                        operation.message_kind.value,
                        operation.actor_id,
                        operation.payload_sha256,
                        reference.accepted_at.isoformat(),
                        "authenticated_operation",
                    ),
                )
            connection.execute(
                """
                UPDATE operations SET operation_state='accepted', provider_message_id=?, provider_thread_id=?,
                    provider_parent_message_id=?, provider_accepted_at=?, error_code=NULL, error_message=NULL,
                    retryable=0, recovery_required=0, next_attempt_at=NULL
                WHERE operation_id=?
                """,
                (
                    reference.message_id,
                    reference.thread_id,
                    reference.parent_message_id,
                    reference.accepted_at.isoformat(),
                    operation_id,
                ),
            )
            next_state = request.work_state
            event_type = "message_accepted"
            if operation.operation_kind == "reply_result":
                if request.work_state not in TERMINAL:
                    next_state = WorkState.RESULT_RETURNED
                    event_type = "result_returned"
                else:
                    event_type = "late_result_retained"
            connection.execute(
                "UPDATE requests SET thread_id=COALESCE(thread_id, ?), work_state=?, revision=revision+1, updated_at=? WHERE request_id=?",
                (
                    reference.thread_id,
                    next_state.value if next_state else None,
                    self.now().isoformat(),
                    request.request_id,
                ),
            )
            self.store.append_event(
                connection,
                request_id=request.request_id,
                operation_id=operation_id,
                actor_id="system:outbox",
                event_type=event_type,
                detail={"provider_message_id": reference.message_id},
            )

    @staticmethod
    def _message_link_matches(link, operation, request, reference) -> bool:
        return (
            link["provider_message_id"] == reference.message_id
            and link["request_id"] == request.request_id
            and link["operation_id"] == operation.operation_id
            and link["provider_thread_id"] == reference.thread_id
            and link["parent_message_id"] == reference.parent_message_id
            and link["message_kind"] == operation.message_kind.value
            and link["actor_id"] == operation.actor_id
            and link["content_sha256"] == operation.payload_sha256
            and link["accepted_at"] == reference.accepted_at.isoformat()
            and link["trust_label"] == "authenticated_operation"
            and bool(link["exact_link_verified"])
        )

    def reconcile_once(self) -> Receipt | None:
        operation = self.store.next_uncertain()
        if operation is None:
            return None
        envelope = self.store.outbound_envelope(operation.operation_id)
        try:
            reference = self.provider.reconcile(envelope)
        except TransportError as exc:
            if self._retry_window_expired(operation):
                self._mark_recovery_required(operation)
            elif exc.retryable:
                self._defer_reconciliation(operation, exc)
            else:
                self._mark_recovery_required(operation, exc)
            return self._receipt(self.store.operation(operation.operation_id))
        if reference is not None:
            self._accept(operation.operation_id, reference)
            return self._receipt(self.store.operation(operation.operation_id))
        if self._retry_window_expired(operation):
            self._mark_recovery_required(operation)
        else:
            request = self.store.request(operation.request_id)
            if not request.recovery_hold and request.work_state is not WorkState.CANCELLED:
                next_attempt_at = self.now() + self._backoff(operation.attempt_count)
                with self.store.transaction() as connection:
                    connection.execute(
                        """UPDATE operations SET operation_state='queued', error_message=NULL,
                            next_attempt_at=? WHERE operation_id=?""",
                        (next_attempt_at.isoformat(), operation.operation_id),
                    )
                    self.store.append_event(
                        connection,
                        request_id=operation.request_id,
                        operation_id=operation.operation_id,
                        actor_id="system:outbox",
                        event_type="reconciliation_requeued",
                        detail={"next_attempt_at": next_attempt_at.isoformat()},
                    )
        return self._receipt(self.store.operation(operation.operation_id))

    def _retry_window_expired(self, operation: OperationRecord) -> bool:
        if operation.uncertain_since is None:
            return False
        return self.now() - operation.uncertain_since >= self.retry_window

    def _defer_reconciliation(self, operation: OperationRecord, error: TransportError) -> None:
        next_attempt_at = self.now() + self._backoff(operation.attempt_count, error.retry_after)
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE operations SET next_attempt_at=?, error_message=?, retryable=1,
                    transport_error_code=?, transport_status=? WHERE operation_id=?""",
                (next_attempt_at.isoformat(), error.message, error.code, error.status, operation.operation_id),
            )
            self.store.append_event(
                connection,
                request_id=operation.request_id,
                operation_id=operation.operation_id,
                actor_id="system:outbox",
                event_type="reconciliation_deferred",
                detail={
                    "next_attempt_at": next_attempt_at.isoformat(),
                    "transport_error_code": error.code,
                    "transport_status": error.status,
                },
            )

    def _mark_recovery_required(
        self,
        operation: OperationRecord,
        error: TransportError | None = None,
    ) -> None:
        message = "provider retry window expired with no exact reconciliation match"
        if error is not None and not error.retryable:
            message = "provider reconciliation failed without a safe automatic retry"
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE operations SET operation_state='uncertain', error_code='RECOVERY_REQUIRED',
                    error_message=?, retryable=0, recovery_required=1, next_attempt_at=NULL,
                    transport_error_code=COALESCE(?, transport_error_code),
                    transport_status=COALESCE(?, transport_status) WHERE operation_id=?""",
                (
                    message,
                    error.code if error else None,
                    error.status if error else None,
                    operation.operation_id,
                ),
            )
            self.store.append_event(
                connection,
                request_id=operation.request_id,
                operation_id=operation.operation_id,
                actor_id="system:outbox",
                event_type="recovery_required",
                detail={},
            )

    def recover_interrupted(self) -> int:
        with self.store.transaction() as connection:
            rows = connection.execute(
                """SELECT operation_id, request_id, attempt_started_at, uncertain_since
                FROM operations WHERE operation_state='sending'"""
            ).fetchall()
            for row in rows:
                if row["attempt_started_at"] is None:
                    connection.execute(
                        """UPDATE operations SET operation_state='uncertain',
                            error_code='RECOVERY_REQUIRED',
                            error_message='interrupted send has no durable attempt timestamp',
                            retryable=0, recovery_required=1, next_attempt_at=NULL
                        WHERE operation_id=?""",
                        (row["operation_id"],),
                    )
                    event_type = "interrupted_send_recovery_required"
                else:
                    uncertain_since = row["uncertain_since"]
                    if uncertain_since is None or row["attempt_started_at"] < uncertain_since:
                        uncertain_since = row["attempt_started_at"]
                    connection.execute(
                        """UPDATE operations SET operation_state='uncertain',
                            error_message='interrupted send requires reconciliation', retryable=1,
                            next_attempt_at=NULL, uncertain_since=?
                        WHERE operation_id=?""",
                        (uncertain_since, row["operation_id"]),
                    )
                    event_type = "interrupted_send_recovered"
                self.store.append_event(
                    connection,
                    request_id=row["request_id"],
                    operation_id=row["operation_id"],
                    actor_id="system:restart",
                    event_type=event_type,
                    detail={},
                )
            return len(rows)

    def force_sending(self, operation_id: str) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE operations SET operation_state='sending', attempt_started_at=? WHERE operation_id=?",
                (self.now().isoformat(), operation_id),
            )

    def mark_uncertain(self, operation_id: str, message: str) -> None:
        self._mark_uncertain(operation_id, message)
