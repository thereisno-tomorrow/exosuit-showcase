from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
from threading import Barrier, Event, Thread

import pytest

from gremlin_town.contracts import (
    Actor,
    CancelRequest,
    ContentAvailability,
    ContentSource,
    CreateRequest,
    ErrorCode,
    MessageKind,
    OperationState,
    Packet,
    Page,
    ProviderMessage,
    ProviderReference,
    RecoveryDisposition,
    RecoveryRequest,
    ReplyRequest,
    ReportAction,
    ReportRequest,
    RequestMode,
    WorkState,
    WireMessage,
)
from gremlin_town.domain import Participant, Policy
from gremlin_town.domain import canonical_bytes, canonical_digest
from gremlin_town.service import GremlinTownError, GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.storage.migrations import MIGRATIONS, SCHEMA_VERSION
from gremlin_town.transport import ProviderTransportError
from gremlin_town.transport.framing import OBSERVED_FOOTER
from gremlin_town.workers import OutboxWorker


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class FakeProvider:
    def __init__(self) -> None:
        self.sent = []
        self.references: dict[str, ProviderReference] = {}
        self.fail: Exception | None = None
        self.crash_after_accept = False

    def send(self, envelope):
        self.sent.append(envelope)
        if self.fail:
            raise self.fail
        reference = self.references.setdefault(
            envelope.provider_retry_key,
            ProviderReference(
                message_id=f"msg-{len(self.references) + 1}",
                thread_id="thread-1",
                parent_message_id=envelope.parent_message_id,
                accepted_at=NOW,
            ),
        )
        if self.crash_after_accept:
            raise TimeoutError("provider accepted but the receipt was lost")
        return reference

    def reconcile(self, envelope):
        return self.references.get(envelope.provider_retry_key)

    def fetch_message(self, message_id):
        raise NotImplementedError

    def fetch_thread(self, thread_id, cursor=None):
        if self.fail:
            raise self.fail
        envelope = self.sent[0] if self.sent else None
        return Page(
            items=(
                ProviderMessage(
                    message_id="msg-1" if envelope else "external-1",
                    thread_id=thread_id,
                    parent_message_id=None,
                    received_at=NOW,
                    body=envelope.payload if envelope else b"external body",
                    body_available=True,
                    content_source=ContentSource.PROVIDER,
                    trust_label="provider_claim",
                    linked_operation_id=None,
                    attributed_actor_id=None,
                    exact_link_verified=False,
                ),
            ),
            next_cursor=None,
            as_of=NOW,
        )


@pytest.fixture
def backend(tmp_path):
    clock = Clock()
    policy = Policy(
        participants=(
            Participant("alice", "Alice", "hub@example.test", frozenset({"demo"}), frozenset({"public-test"})),
            Participant("bob", "Bob", "hub@example.test", frozenset({"demo"}), frozenset({"public-test"}), aliases=("builder",)),
            Participant("mallory", "Mallory", "hub@example.test", frozenset({"other"}), frozenset({"public-test"})),
            Participant("operator", "Operator", "hub@example.test", frozenset({"demo", "other"}), frozenset({"public-test"}), is_operator=True),
        ),
        routes=frozenset({("demo", "alice", "bob", "public-test"), ("demo", "bob", "alice", "public-test")}),
    )
    store = SQLiteStore(tmp_path / "gremlin.db", now=clock)
    provider = FakeProvider()
    service = GremlinTownService(store, policy, provider=provider, now=clock)
    worker = OutboxWorker(store, policy, provider, now=clock)
    return service, worker, store, policy, provider, clock


def packet(kind=MessageKind.REQUEST, objective="Do the thing") -> Packet:
    return Packet(
        schema_version=1,
        kind=kind,
        session_reference="session-declared",
        objective=objective,
        context="Only use this packet.",
        constraints={"scope": "bounded"},
        sources=(),
        next_action="Return a result.",
        expected_reply="A concise answer.",
        sensitivity="public-test",
    )


def create(service, *, key="create-1", mode=RequestMode.ACTION_REQUESTED, prior=None):
    kind = MessageKind.REQUEST if mode is RequestMode.ACTION_REQUESTED else MessageKind.CONTEXT
    return service.requests_create(
        Actor("alice"),
        CreateRequest(key, mode, "demo", "bob", packet(kind), prior),
    )


def accept_initial(service, worker):
    receipt = create(service)
    assert receipt.operation_state is OperationState.QUEUED
    sent = worker.dispatch_once()
    assert sent.operation_state is OperationState.ACCEPTED
    return receipt.request_id


def assert_code(exc, code):
    assert exc.value.code is code


def test_create_read_report_reply_cancel_flow_and_append_only_events(backend):
    service, worker, _, _, _, _ = backend
    request_id = accept_initial(service, worker)

    snapshot = service.requests_read(Actor("bob"), request_id)
    assert snapshot.work_state is WorkState.REQUESTED
    assert snapshot.content_availability is ContentAvailability.COMPLETE
    assert snapshot.content[0].content_source is ContentSource.PROVIDER

    started = service.requests_report(
        Actor("bob"), ReportRequest("start-1", request_id, snapshot.revision, ReportAction.START)
    )
    assert started.work_state is WorkState.IN_PROGRESS

    queued = service.requests_reply(
        Actor("bob"),
        ReplyRequest("result-1", request_id, started.revision, MessageKind.RESULT, packet(MessageKind.RESULT, "42")),
    )
    assert queued.operation_state is OperationState.QUEUED
    assert queued.work_state is WorkState.IN_PROGRESS
    accepted = worker.dispatch_once()
    assert accepted.work_state is WorkState.RESULT_RETURNED

    with pytest.raises(GremlinTownError) as exc:
        service.requests_cancel(
            Actor("alice"), CancelRequest("cancel-too-late", request_id, accepted.revision, "stop")
        )
    assert_code(exc, ErrorCode.INVALID_TRANSITION)

    events = service.events_list(Actor("alice"), {"request_id": request_id, "limit": 2})
    assert len(events.items) == 2
    assert events.next_cursor is not None
    assert service.events_list(Actor("alice"), {"request_id": request_id}, events.next_cursor).items


def test_context_only_prior_links_and_terminal_scope_never_reopen(backend):
    service, worker, _, _, _, _ = backend
    context_receipt = create(service, key="context-1", mode=RequestMode.CONTEXT_ONLY)
    worker.dispatch_once()
    context = service.requests_read(Actor("bob"), context_receipt.request_id)
    assert context.work_state is None

    with pytest.raises(GremlinTownError) as exc:
        service.requests_report(
            Actor("bob"), ReportRequest("bad-start", context.request_id, context.revision, ReportAction.START)
        )
    assert_code(exc, ErrorCode.INVALID_TRANSITION)

    linked = create(service, key="linked", prior=context.request_id)
    assert linked.work_state is WorkState.REQUESTED
    assert service.requests_read(Actor("bob"), context.request_id).work_state is None

    with pytest.raises(GremlinTownError) as exc:
        service.requests_create(
            Actor("mallory"),
            CreateRequest("stolen-link", RequestMode.ACTION_REQUESTED, "other", "mallory", packet(), context.request_id),
        )
    assert_code(exc, ErrorCode.ACCESS_DENIED)


def test_prior_link_requires_new_recipient_access_too(backend):
    service, worker, _, policy, _, _ = backend
    prior = create(service, key="prior-context", mode=RequestMode.CONTEXT_ONLY)
    worker.dispatch_once()
    policy._participants["carol"] = Participant(
        "carol", "Carol", "hub@example.test", frozenset({"demo"}), frozenset({"public-test"})
    )
    policy._routes = policy._routes | {("demo", "alice", "carol", "public-test")}
    with pytest.raises(GremlinTownError) as exc:
        service.requests_create(
            Actor("alice"),
            CreateRequest(
                "leaking-link",
                RequestMode.ACTION_REQUESTED,
                "demo",
                "carol",
                packet(),
                prior.request_id,
            ),
        )
    assert_code(exc, ErrorCode.ACCESS_DENIED)


def test_identity_access_idempotency_conflicts_and_stale_revision(backend):
    service, worker, _, _, _, _ = backend
    first = create(service)
    replay = create(service)
    assert replay.operation_id == first.operation_id
    with pytest.raises(GremlinTownError) as exc:
        create(service, key="create-1", mode=RequestMode.CONTEXT_ONLY)
    assert_code(exc, ErrorCode.OPERATION_INPUT_CONFLICT)

    request_id = first.request_id
    with pytest.raises(GremlinTownError) as exc:
        service.requests_read(Actor("mallory"), request_id)
    assert_code(exc, ErrorCode.ACCESS_DENIED)
    with pytest.raises(GremlinTownError) as exc:
        service.operations_get(Actor("mallory"), first.operation_id)
    assert_code(exc, ErrorCode.ACCESS_DENIED)
    with pytest.raises(GremlinTownError) as exc:
        service.requests_report(
            Actor("alice"), ReportRequest("impersonate", request_id, first.revision, ReportAction.START)
        )
    assert_code(exc, ErrorCode.ACCESS_DENIED)

    worker.dispatch_once()
    current = service.requests_read(Actor("bob"), request_id)
    with pytest.raises(GremlinTownError) as exc:
        service.requests_report(
            Actor("bob"), ReportRequest("stale", request_id, current.revision - 1, ReportAction.START)
        )
    assert_code(exc, ErrorCode.REVISION_CONFLICT)


def test_cached_receipts_and_dispatch_recheck_separate_revocations(backend):
    service, worker, _, policy, _, _ = backend
    receipt = create(service)
    policy.set_enabled("alice", False)
    with pytest.raises(GremlinTownError) as exc:
        create(service)
    assert_code(exc, ErrorCode.ACCESS_DENIED)
    policy.set_enabled("alice", True)
    policy.set_enabled("bob", False)
    denied = worker.dispatch_once()
    assert denied.operation_state is OperationState.FAILED
    assert denied.error.code is ErrorCode.ACCESS_DENIED
    assert service.operations_get(Actor("alice"), receipt.operation_id).operation_state is OperationState.FAILED

    policy.set_enabled("bob", True)
    assert create(service).operation_id == receipt.operation_id
    policy.set_enabled("bob", False)
    with pytest.raises(GremlinTownError) as exc:
        create(service)
    assert_code(exc, ErrorCode.ACCESS_DENIED)


def test_exact_outbox_bytes_crash_recovery_and_retry_window(backend):
    service, worker, store, _, provider, clock = backend
    created = create(service)
    before = store.outbound_envelope(created.operation_id)
    wire = WireMessage(**{
        **json.loads(before.payload),
        "created_at": datetime.fromisoformat(json.loads(before.payload)["created_at"]),
        "packet": packet(),
    })
    assert wire.request_id == created.request_id
    assert before.payload == canonical_bytes(wire)
    provider.crash_after_accept = True
    uncertain = worker.dispatch_once()
    assert uncertain.operation_state is OperationState.UNCERTAIN
    after = store.outbound_envelope(created.operation_id)
    assert after.payload == before.payload
    assert after.provider_retry_key == before.provider_retry_key
    assert after.content_sha256 == before.content_sha256

    provider.crash_after_accept = False
    reconciled = worker.reconcile_once()
    assert reconciled.operation_state is OperationState.ACCEPTED

    second = create(service, key="old-uncertain")
    provider.fail = TimeoutError("unknown")
    worker.dispatch_once()
    clock.advance(hours=25)
    provider.fail = None
    provider.references.clear()
    expired = worker.reconcile_once()
    assert expired.error.code is ErrorCode.RECOVERY_REQUIRED
    assert service.operations_get(Actor("alice"), second.operation_id).error.code is ErrorCode.RECOVERY_REQUIRED


def test_429_is_durably_throttled_until_retry_after_through_outbox_worker(backend):
    service, worker, store, _, provider, clock = backend
    created = create(service)
    original = store.outbound_envelope(created.operation_id)
    provider.fail = ProviderTransportError(
        "RATE_LIMITED",
        "provider returned HTTP 429",
        retryable=True,
        retry_after=60,
        status=429,
    )

    throttled = worker.dispatch_once()

    assert throttled.operation_state is OperationState.QUEUED
    assert throttled.error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert throttled.error.retryable
    operation = store.operation(created.operation_id)
    assert operation.attempt_count == 1
    assert operation.next_attempt_at == NOW + timedelta(seconds=60)
    assert operation.transport_error_code == "RATE_LIMITED"
    assert operation.transport_status == 429
    assert worker.dispatch_once() is None
    assert len(provider.sent) == 1

    clock.advance(seconds=59)
    assert worker.dispatch_once() is None
    assert len(provider.sent) == 1

    clock.advance(seconds=1)
    provider.fail = None
    accepted = worker.dispatch_once()
    assert accepted.operation_state is OperationState.ACCEPTED
    assert len(provider.sent) == 2
    retried = store.outbound_envelope(created.operation_id)
    assert retried.payload == original.payload
    assert retried.provider_retry_key == original.provider_retry_key


def test_retry_schedule_survives_store_reopen(backend):
    service, worker, store, policy, provider, clock = backend
    created = create(service)
    original = store.outbound_envelope(created.operation_id)
    provider.fail = ProviderTransportError(
        "RATE_LIMITED", "provider returned HTTP 429", retryable=True, retry_after=30, status=429
    )
    worker.dispatch_once()
    database = store.path
    store.close()

    restarted_store = SQLiteStore(database, now=clock)
    restarted_worker = OutboxWorker(restarted_store, policy, provider, now=clock)
    assert restarted_worker.dispatch_once() is None
    clock.advance(seconds=30)
    provider.fail = None
    accepted = restarted_worker.dispatch_once()
    assert accepted.operation_state is OperationState.ACCEPTED
    retried = restarted_store.outbound_envelope(created.operation_id)
    assert retried.payload == original.payload
    assert retried.provider_retry_key == original.provider_retry_key
    restarted_store.close()


def test_retryable_outages_use_bounded_exponential_backoff(backend):
    service, worker, store, _, provider, clock = backend
    created = create(service)
    provider.fail = ProviderTransportError(
        "PROVIDER_UNAVAILABLE", "provider unavailable", retryable=True, status=503
    )

    observed_delays = []
    for expected in (1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300):
        worker.dispatch_once()
        operation = store.operation(created.operation_id)
        observed_delays.append(int((operation.next_attempt_at - clock.value).total_seconds()))
        assert worker.dispatch_once() is None
        clock.advance(seconds=expected)

    assert observed_delays == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300]
    assert store.operation(created.operation_id).attempt_count == 11


def test_non_retryable_transport_rejection_fails_with_stable_provider_facts(backend):
    service, worker, store, _, provider, _ = backend
    created = create(service)
    provider.fail = ProviderTransportError(
        "PROVIDER_FORBIDDEN", "provider returned HTTP 403", retryable=False, status=403
    )

    rejected = worker.dispatch_once()

    assert rejected.operation_state is OperationState.FAILED
    assert not rejected.error.retryable
    operation = store.operation(created.operation_id)
    assert operation.transport_error_code == "PROVIDER_FORBIDDEN"
    assert operation.transport_status == 403
    assert operation.next_attempt_at is None
    assert worker.dispatch_once() is None


def test_uncertainty_is_reconciled_before_scheduled_resend(backend):
    service, worker, store, _, provider, clock = backend
    created = create(service)
    provider.fail = ProviderTransportError(
        "TIMEOUT", "provider acceptance receipt was lost", retryable=True, accepted=True
    )

    uncertain = worker.dispatch_once()
    assert uncertain.operation_state is OperationState.UNCERTAIN
    assert len(provider.sent) == 1
    assert worker.dispatch_once() is None

    provider.fail = None
    requeued = worker.reconcile_once()
    assert requeued.operation_state is OperationState.QUEUED
    assert store.operation(created.operation_id).next_attempt_at == clock.value + timedelta(seconds=1)
    assert worker.dispatch_once() is None
    assert len(provider.sent) == 1

    clock.advance(seconds=1)
    accepted = worker.dispatch_once()
    assert accepted.operation_state is OperationState.ACCEPTED
    assert len(provider.sent) == 2


def test_schema_v2_is_forward_migrated_and_local_usage_is_truthful(tmp_path):
    database = tmp_path / "version-one.db"
    connection = sqlite3.connect(database)
    connection.executescript(MIGRATIONS[1])
    connection.execute("PRAGMA user_version = 1")
    connection.close()

    clock = Clock()
    store = SQLiteStore(database, now=clock)
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    policy = Policy(
        participants=(
            Participant("alice", "Alice", "hub@example.test", frozenset({"demo"}), frozenset({"public-test"})),
            Participant("bob", "Bob", "hub@example.test", frozenset({"demo"}), frozenset({"public-test"})),
        ),
        routes=frozenset({("demo", "alice", "bob", "public-test")}),
    )
    service = GremlinTownService(store, policy, now=clock)
    first = create(service, key="usage-queued")
    second = create(service, key="usage-sending")
    third = create(service, key="usage-uncertain")
    worker = OutboxWorker(store, policy, FakeProvider(), now=clock)
    worker.force_sending(second.operation_id)
    store.mark_operation_uncertain(third.operation_id, "unknown provider outcome")

    usage = store.local_usage()
    expected_payload_bytes = store.connection.execute(
        "SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM operations"
    ).fetchone()[0]
    assert usage == {
        "queued_count": 1,
        "sending_count": 1,
        "uncertain_count": 1,
        "retained_payload_bytes": expected_payload_bytes,
        "database_bytes": database.stat().st_size,
        "wal_bytes": database.with_name(f"{database.name}-wal").stat().st_size,
    }
    assert first.operation_id
    store.close()


def test_result_failure_pending_uniqueness_and_cancellation_race(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    revision = service.requests_read(Actor("bob"), request_id).revision
    first = service.requests_reply(
        Actor("bob"), ReplyRequest("result-a", request_id, revision, MessageKind.RESULT, packet(MessageKind.RESULT))
    )
    with pytest.raises(GremlinTownError) as exc:
        service.requests_reply(
            Actor("bob"), ReplyRequest("result-b", request_id, first.revision, MessageKind.RESULT, packet(MessageKind.RESULT, "different"))
        )
    assert_code(exc, ErrorCode.RESULT_PENDING)

    provider.fail = ValueError("permanent provider rejection")
    failed = worker.dispatch_once()
    assert failed.operation_state is OperationState.FAILED
    assert service.requests_read(Actor("bob"), request_id).work_state is WorkState.REQUESTED

    provider.fail = None
    late = service.requests_reply(
        Actor("bob"), ReplyRequest("result-late", request_id, failed.revision, MessageKind.RESULT, packet(MessageKind.RESULT, "late"))
    )
    cancelled = service.requests_cancel(
        Actor("alice"), CancelRequest("cancel-1", request_id, late.revision, "no longer needed")
    )
    assert cancelled.work_state is WorkState.CANCELLED
    # Simulate a send already in flight: provider evidence is retained but cancellation wins.
    worker.force_sending(late.operation_id)
    envelope = worker.store.outbound_envelope(late.operation_id)
    provider.references[envelope.provider_retry_key] = ProviderReference("late-msg", "thread-1", NOW, "msg-1")
    worker.mark_uncertain(late.operation_id, "receipt lost")
    reconciled = worker.reconcile_once()
    assert reconciled.operation_state is OperationState.ACCEPTED
    assert service.requests_read(Actor("alice"), request_id).work_state is WorkState.CANCELLED


def test_requester_clarification_routes_to_recipient_without_changing_work_state(backend):
    service, worker, store, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    current = service.requests_read(Actor("alice"), request_id)
    queued = service.requests_reply(
        Actor("alice"),
        ReplyRequest(
            "clarify-1",
            request_id,
            current.revision,
            MessageKind.CLARIFICATION,
            packet(MessageKind.CLARIFICATION, "Please clarify one detail"),
        ),
    )
    wire = json.loads(store.outbound_envelope(queued.operation_id).payload)
    assert wire["sender"] == "alice"
    assert wire["recipient"] == "bob"
    operation = store.operation(queued.operation_id)
    assert operation.recipient_address == "hub@example.test"
    assert operation.route_source_id == "alice"
    assert operation.route_destination_id == "bob"
    assert operation.parent_message_id == "msg-1"
    assert queued.work_state is WorkState.REQUESTED
    accepted = worker.dispatch_once()
    assert accepted.operation_state is OperationState.ACCEPTED
    assert accepted.work_state is WorkState.REQUESTED
    assert provider.sent[-1].parent_message_id == "msg-1"
    assert accepted.provider.parent_message_id == "msg-1"


def test_recipient_clarification_routes_to_requester_without_changing_work_state(backend):
    service, worker, store, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    current = service.requests_read(Actor("bob"), request_id)
    queued = service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "clarify-recipient",
            request_id,
            current.revision,
            MessageKind.CLARIFICATION,
            packet(MessageKind.CLARIFICATION, "Which format should I return?"),
        ),
    )

    wire = json.loads(store.outbound_envelope(queued.operation_id).payload)
    assert wire["sender"] == "bob"
    assert wire["recipient"] == "alice"
    operation = store.operation(queued.operation_id)
    assert operation.recipient_address == "hub@example.test"
    assert operation.route_source_id == "bob"
    assert operation.route_destination_id == "alice"
    assert operation.parent_message_id == "msg-1"
    assert queued.work_state is WorkState.REQUESTED

    accepted = worker.dispatch_once()
    assert accepted.operation_state is OperationState.ACCEPTED
    assert accepted.work_state is WorkState.REQUESTED
    assert provider.sent[-1].parent_message_id == "msg-1"
    assert accepted.provider.parent_message_id == "msg-1"


def test_content_availability_empty_queued_partial_unavailable_and_pagination(backend):
    service, worker, store, _, provider, _ = backend
    created = create(service)
    queued = service.requests_read(Actor("alice"), created.request_id)
    assert queued.content_availability is ContentAvailability.QUEUED_ONLY
    assert queued.content[0].content_source is ContentSource.OUTBOX

    worker.dispatch_once()
    provider.fail = ConnectionError("provider unavailable")
    partial = service.requests_read(Actor("alice"), created.request_id)
    assert partial.content_availability is ContentAvailability.PARTIAL
    assert partial.content_error.code is ErrorCode.SOURCE_UNAVAILABLE

    store.purge_outbox_payloads(created.request_id)
    unavailable = service.requests_read(Actor("alice"), created.request_id)
    assert unavailable.content_availability is ContentAvailability.UNAVAILABLE
    assert unavailable.content == ()

    empty_id = store.insert_empty_context("alice", "bob", "demo", NOW)
    empty = service.requests_read(Actor("alice"), empty_id)
    assert empty.content_availability is ContentAvailability.EMPTY


def test_restore_holds_require_operator_disposition_and_reconciliation_never_releases(backend):
    service, worker, store, _, provider, _ = backend
    created = create(service)
    store.apply_restore_holds()
    held = service.requests_read(Actor("alice"), created.request_id)
    assert held.recovery_hold
    assert service.requests_list(Actor("alice"), {}).items == ()
    historical = service.requests_list(Actor("alice"), {"include_recovery_held": True})
    assert historical.items[0]["recovery_hold"] is True
    with pytest.raises(GremlinTownError) as exc:
        worker.dispatch_once()
    assert_code(exc, ErrorCode.RECOVERY_HOLD)

    envelope = store.outbound_envelope(created.operation_id)
    provider.references[envelope.provider_retry_key] = ProviderReference("found", "thread-1", NOW)
    store.mark_operation_uncertain(created.operation_id, "restored interrupted operation")
    worker.reconcile_once()
    assert service.requests_read(Actor("alice"), created.request_id).recovery_hold

    with pytest.raises(GremlinTownError) as exc:
        service.recovery_dispose(
            Actor("alice"),
            RecoveryRequest("release-nope", created.request_id, held.revision, RecoveryDisposition.RELEASE_ACTIONABLE, "resume"),
        )
    assert_code(exc, ErrorCode.ACCESS_DENIED)

    current = service.requests_read(Actor("operator", is_operator=True), created.request_id)
    disposed = service.recovery_dispose(
        Actor("operator", is_operator=True),
        RecoveryRequest("confirm-cancel", created.request_id, current.revision, RecoveryDisposition.CONFIRM_CANCELLED, "lost cancellation may exist"),
    )
    assert disposed.work_state is WorkState.CANCELLED
    assert not service.requests_read(Actor("operator", is_operator=True), created.request_id).recovery_hold


def test_schema_normalization_pagination_and_restart_before_send(backend):
    service, worker, store, policy, provider, clock = backend
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert canonical_digest({"b": [2, 1], "a": {"y": 2, "x": 1}}) == canonical_digest(
        {"a": {"x": 1, "y": 2}, "b": [2, 1]}
    )

    first = create(service)
    create(service, key="create-2")
    page = service.requests_list(Actor("alice"), {"limit": 1})
    assert len(page.items) == 1 and page.next_cursor
    assert len(service.requests_list(Actor("alice"), {"limit": 1}, page.next_cursor).items) == 1

    # Simulate a process ending after the durable sending claim but before I/O.
    worker.force_sending(first.operation_id)
    db_path = store.path
    store.close()
    restarted_store = SQLiteStore(db_path, now=clock)
    restarted_worker = OutboxWorker(restarted_store, policy, provider, now=clock)
    assert restarted_worker.recover_interrupted() == 1
    assert restarted_worker.reconcile_once().operation_state is OperationState.QUEUED
    assert provider.sent == []
    assert restarted_store.operation(first.operation_id).next_attempt_at == clock.value + timedelta(seconds=1)
    clock.advance(seconds=1)
    accepted = restarted_worker.dispatch_once()
    assert accepted.operation_state is OperationState.ACCEPTED
    assert len(provider.sent) == 1
    restarted_store.close()


def test_requests_list_filters_recipient_and_strict_attention(backend):
    service, _, store, _, _, _ = backend
    blocked = create(service, key="list-blocked")
    inbound = service.requests_create(
        Actor("bob"),
        CreateRequest(
            "list-inbound",
            RequestMode.ACTION_REQUESTED,
            "demo",
            "alice",
            packet(),
        ),
    )
    ordinary = create(service, key="list-ordinary", mode=RequestMode.CONTEXT_ONLY)

    service.requests_report(
        Actor("bob"),
        ReportRequest(
            "list-blocked-report",
            blocked.request_id,
            1,
            ReportAction.BLOCK,
            "waiting for input",
        ),
    )
    store.mark_operation_uncertain(inbound.operation_id, "provider outcome unknown")

    to_bob = service.requests_list(Actor("alice"), {"recipient": "bob"})
    assert {item["request_id"] for item in to_bob.items} == {
        blocked.request_id,
        ordinary.request_id,
    }
    assert all(item["recipient_id"] == "bob" for item in to_bob.items)
    assert all(item["updated_at"] is not None for item in to_bob.items)

    to_alice = service.requests_list(Actor("alice"), {"recipient": "alice"})
    assert [item["request_id"] for item in to_alice.items] == [inbound.request_id]

    inbox = service.requests_list(Actor("alice"), {})
    attention = service.requests_list(Actor("alice"), {"attention": True})
    assert len(attention.items) < len(inbox.items)
    assert {item["request_id"] for item in attention.items} == {
        blocked.request_id,
        inbound.request_id,
    }
    assert {item["attention"] for item in attention.items} == {
        "blocker",
        "ambiguous send; no acknowledgement",
    }
    not_attention = service.requests_list(Actor("alice"), {"attention": False})
    assert [item["request_id"] for item in not_attention.items] == [ordinary.request_id]
    assert not_attention.items[0]["attention"] is None


def test_requests_list_orders_latest_first_with_stable_request_id_ties(backend):
    service, _, store, _, _, clock = backend
    first = create(service, key="list-order-first")
    second = create(service, key="list-order-second")
    clock.advance(minutes=1)
    newest = create(service, key="list-order-newest")
    tied_at = NOW.isoformat()
    with store.transaction() as connection:
        connection.execute(
            "UPDATE requests SET updated_at=? WHERE request_id IN (?, ?)",
            (tied_at, first.request_id, second.request_id),
        )

    page = service.requests_list(Actor("alice"), {})
    tied_ids = sorted((first.request_id, second.request_id))
    assert [item["request_id"] for item in page.items] == [newest.request_id, *tied_ids]
    assert [item["updated_at"] for item in page.items] == [
        clock.value,
        NOW,
        NOW,
    ]


def test_requests_list_cursor_keeps_filters_after_access_boundary(backend):
    service, _, store, _, _, clock = backend
    first = create(service, key="list-page-first")
    clock.advance(seconds=1)
    second = create(service, key="list-page-second")
    clock.advance(seconds=1)
    hidden_id = "req_hidden_list_page"
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO requests
                (request_id, mode, project, requester_id, recipient_id, objective,
                 sensitivity, work_state, revision, recovery_hold, prior_request_id,
                 thread_id, created_at, updated_at)
                VALUES (?, 'action_requested', 'other', 'mallory', 'operator', ?,
                        'public-test', 'blocked', 1, 0, NULL, NULL, ?, ?)""",
            (hidden_id, "Hidden newest request", clock.value.isoformat(), clock.value.isoformat()),
        )

    filters = {"recipient": "bob", "limit": 1}
    page_one = service.requests_list(Actor("alice"), filters)
    assert [item["request_id"] for item in page_one.items] == [second.request_id]
    assert page_one.next_cursor is not None
    page_two = service.requests_list(Actor("alice"), filters, page_one.next_cursor)
    assert [item["request_id"] for item in page_two.items] == [first.request_id]
    assert page_two.next_cursor is None
    assert hidden_id not in {item["request_id"] for item in (*page_one.items, *page_two.items)}


def test_request_read_records_source_attempt_and_success_without_changing_revision(backend):
    service, worker, _, _, provider, clock = backend
    request_id = accept_initial(service, worker)
    original_revision = service.requests_read(Actor("alice"), request_id).revision

    clock.advance(seconds=5)
    successful = service.requests_read(Actor("alice"), request_id)
    successful_at = clock.value
    assert successful.revision == original_revision
    assert successful.work_state is WorkState.REQUESTED
    item = service.requests_list(Actor("alice"), {}).items[0]
    assert item["last_source_fetch_at"] == successful_at
    assert item["last_source_success_at"] == successful_at
    assert not any(
        event["event_type"] == "provider_message_unmatched"
        for event in service.events_list(Actor("alice"), {"request_id": request_id}).items
    )

    provider.fail = ConnectionError("provider unavailable")
    clock.advance(seconds=7)
    failed = service.requests_read(Actor("alice"), request_id)
    assert failed.content_error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert failed.revision == original_revision
    assert failed.work_state is WorkState.REQUESTED
    item = service.requests_list(Actor("alice"), {}).items[0]
    assert item["last_source_fetch_at"] == clock.value
    assert item["last_source_success_at"] == successful_at


def test_unmatched_provider_observation_is_metadata_only_deduplicated_and_current(backend):
    service, worker, store, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    unmatched_body = b"secret unmatched body"
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="unmatched-message",
                thread_id=thread_id,
                parent_message_id=None,
                received_at=NOW,
                body=unmatched_body,
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        )
    )
    original = service.requests_read(Actor("alice"), request_id)
    service.requests_read(Actor("alice"), request_id)
    reread = service.requests_read(Actor("alice"), request_id)
    assert reread.revision == original.revision
    assert reread.work_state is original.work_state is WorkState.REQUESTED

    events = service.events_list(Actor("alice"), {"request_id": request_id}).items
    unmatched = [event for event in events if event["event_type"] == "provider_message_unmatched"]
    assert len(unmatched) == 1
    assert unmatched[0]["detail"] == {
        "body_available": True,
        "parent_message_id": None,
        "provider_message_id": "unmatched-message",
        "provider_observed_at": NOW.isoformat(),
        "provider_thread_id": "thread-1",
        "trust_label": "untrusted_external",
    }
    assert unmatched_body.decode() not in json.dumps(unmatched[0])
    before_reconciliation = service.requests_list(Actor("alice"), {}).items[0]
    assert "unmatched provider message" in before_reconciliation["attention"]
    assert unmatched_body.decode() not in repr(before_reconciliation)

    clarification = service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "unmatched-reconciled",
            request_id,
            original.revision,
            MessageKind.CLARIFICATION,
            packet(MessageKind.CLARIFICATION),
        ),
    )
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO message_links
                (provider_message_id, request_id, operation_id, provider_thread_id,
                 parent_message_id, message_kind, actor_id, content_sha256,
                 accepted_at, trust_label, exact_link_verified)
                VALUES (?, ?, ?, 'thread-1', NULL, 'clarification', 'bob', ?, ?,
                        'authenticated_operation', 0)""",
            (
                "unmatched-message",
                request_id,
                clarification.operation_id,
                hashlib.sha256(unmatched_body).hexdigest(),
                NOW.isoformat(),
            ),
        )

    unverified_link = service.requests_list(Actor("alice"), {}).items[0]
    assert "unmatched provider message" in unverified_link["attention"]
    with store.transaction() as connection:
        connection.execute(
            "UPDATE message_links SET exact_link_verified=1 WHERE provider_message_id=?",
            ("unmatched-message",),
        )
    after_reconciliation = service.requests_list(Actor("alice"), {}).items[0]
    assert "unmatched provider message" not in after_reconciliation["attention"]
    retained = service.events_list(Actor("alice"), {"request_id": request_id}).items
    assert len([event for event in retained if event["event_type"] == "provider_message_unmatched"]) == 1


def test_requests_list_reports_participation_age_and_no_ack_attention(backend):
    service, _, _, _, _, clock = backend
    no_ack = create(service, key="participation-no-ack")
    reported = create(service, key="participation-reported")
    clock.advance(seconds=10)
    service.requests_report(
        Actor("bob"),
        ReportRequest("participation-ack", reported.request_id, 1, ReportAction.ACKNOWLEDGE),
    )
    report_at = clock.value
    clock.advance(seconds=20)

    items = {
        item["request_id"]: item
        for item in service.requests_list(Actor("alice"), {}).items
    }
    assert items[no_ack.request_id]["last_authenticated_report_at"] is None
    assert items[no_ack.request_id]["participation"] == {
        "state": "no_acknowledgement",
        "summary": f"no acknowledgement since {NOW.isoformat()}",
        "since": NOW,
        "age_seconds": 30,
    }
    assert items[reported.request_id]["last_authenticated_report_at"] == report_at
    assert items[reported.request_id]["participation"] == {
        "state": "authenticated_report",
        "summary": f"last authenticated report at {report_at.isoformat()}",
        "since": report_at,
        "age_seconds": 20,
    }

    attention_ids = {
        item["request_id"]
        for item in service.requests_list(Actor("alice"), {"attention": True}).items
    }
    assert no_ack.request_id in attention_ids
    assert reported.request_id not in attention_ids


def test_verified_provider_attribution_must_match_durable_link(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-1",
                thread_id=thread_id,
                parent_message_id=None,
                received_at=NOW,
                body=b"tampered attribution",
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="authenticated_operation",
                linked_operation_id="wrong-operation",
                attributed_actor_id="mallory",
                exact_link_verified=True,
            ),
        )
    )
    with pytest.raises(GremlinTownError) as exc:
        service.requests_read(Actor("bob"), request_id)
    assert_code(exc, ErrorCode.THREAD_SCOPE_CONFLICT)


def test_verified_provider_attribution_accepts_footer_and_returns_canonical_body(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    envelope = provider.sent[0]
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-1",
                thread_id=thread_id,
                parent_message_id=None,
                received_at=NOW,
                body=envelope.payload + OBSERVED_FOOTER.encode(),
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        )
    )

    message = service.requests_read(Actor("bob"), request_id).content[0]

    assert message.exact_link_verified
    assert message.body == envelope.payload
    assert OBSERVED_FOOTER.encode() not in message.body


def test_verified_reply_attribution_checks_durable_quoted_parent(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    snapshot = service.requests_read(Actor("bob"), request_id)
    service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "provider-footer-reply",
            request_id,
            snapshot.revision,
            MessageKind.CLARIFICATION,
            packet(MessageKind.CLARIFICATION),
        ),
    )
    accepted_reply = worker.dispatch_once()
    assert accepted_reply.operation_state is OperationState.ACCEPTED
    parent, child = provider.sent
    quoted_parent = b"\n".join(b">" + line for line in parent.payload.split(b"\n"))
    suffix = OBSERVED_FOOTER.encode() + b"\n\nOn Parent wrote:\n\n" + quoted_parent
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-2",
                thread_id=thread_id,
                parent_message_id="msg-1",
                received_at=NOW,
                body=child.payload + suffix,
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        )
    )

    message = service.requests_read(Actor("alice"), request_id).content[0]
    assert message.exact_link_verified
    assert message.body == child.payload

    wrong_parent = quoted_parent + b"tampered"
    provider.fetch_message = lambda message_id: ProviderMessage(
        message_id=message_id,
        thread_id="thread-1",
        parent_message_id=None,
        received_at=NOW,
        body=parent.payload,
        body_available=True,
        content_source=ContentSource.PROVIDER,
        trust_label="provider_claim",
    )
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-2",
                thread_id=thread_id,
                parent_message_id="msg-1",
                received_at=NOW,
                body=child.payload
                + OBSERVED_FOOTER.encode()
                + b"\n\nOn Parent wrote:\n\n"
                + wrong_parent,
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        )
    )
    with pytest.raises(GremlinTownError) as exc:
        service.requests_read(Actor("alice"), request_id)
    assert_code(exc, ErrorCode.THREAD_SCOPE_CONFLICT)


def test_verified_reply_accepts_exact_trusted_provider_rendered_parent(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    snapshot = service.requests_read(Actor("bob"), request_id)
    service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "provider-rendered-parent-reply",
            request_id,
            snapshot.revision,
            MessageKind.RESULT,
            packet(MessageKind.RESULT, "42"),
        ),
    )
    accepted_reply = worker.dispatch_once()
    assert accepted_reply.operation_state is OperationState.ACCEPTED
    parent, child = provider.sent
    rendered_parent = parent.payload + OBSERVED_FOOTER.encode()
    quoted_parent = b"\n".join(b">" + line for line in rendered_parent.split(b"\n"))
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-2",
                thread_id=thread_id,
                parent_message_id="msg-1",
                received_at=NOW,
                body=child.payload
                + OBSERVED_FOOTER.encode()
                + b"\n\nOn Parent wrote:\n\n"
                + quoted_parent,
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
            ProviderMessage(
                message_id="msg-1",
                thread_id=thread_id,
                parent_message_id=None,
                received_at=NOW,
                body=rendered_parent,
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        )
    )

    messages = service.requests_read(Actor("alice"), request_id).content

    assert [message.message_id for message in messages] == ["msg-2", "msg-1"]
    assert all(message.exact_link_verified for message in messages)
    assert messages[0].body == child.payload
    assert messages[1].body == parent.payload


def test_reply_identity_mismatch_is_rejected_before_parent_fetch(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    snapshot = service.requests_read(Actor("bob"), request_id)
    service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "provider-wrong-parent-before-fetch",
            request_id,
            snapshot.revision,
            MessageKind.RESULT,
            packet(MessageKind.RESULT, "42"),
        ),
    )
    worker.dispatch_once()
    child = provider.sent[1]
    fetches = []
    provider.fetch_message = lambda message_id: fetches.append(message_id)
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-2",
                thread_id=thread_id,
                parent_message_id="foreign-parent",
                received_at=NOW,
                body=child.payload + OBSERVED_FOOTER.encode(),
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        )
    )

    with pytest.raises(GremlinTownError) as exc:
        service.requests_read(Actor("alice"), request_id)

    assert_code(exc, ErrorCode.THREAD_SCOPE_CONFLICT)
    assert fetches == []


def test_verified_reply_fetches_trusted_rendered_parent_across_page_boundary(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    snapshot = service.requests_read(Actor("bob"), request_id)
    service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "provider-page-boundary-reply",
            request_id,
            snapshot.revision,
            MessageKind.RESULT,
            packet(MessageKind.RESULT, "42"),
        ),
    )
    worker.dispatch_once()
    parent, child = provider.sent
    rendered_parent = parent.payload + OBSERVED_FOOTER.encode()
    quoted_parent = b"\n".join(b">" + line for line in rendered_parent.split(b"\n"))
    provider.fetch_message = lambda message_id: ProviderMessage(
        message_id=message_id,
        thread_id="thread-1",
        parent_message_id=None,
        received_at=NOW,
        body=rendered_parent,
        body_available=True,
        content_source=ContentSource.PROVIDER,
        trust_label="provider_claim",
    )
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-2",
                thread_id=thread_id,
                parent_message_id="msg-1",
                received_at=NOW,
                body=child.payload
                + OBSERVED_FOOTER.encode()
                + b"\n\nOn Parent wrote:\n\n"
                + quoted_parent,
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        ),
        next_cursor="older-page",
    )

    snapshot = service.requests_read(Actor("alice"), request_id)

    assert snapshot.next_message_cursor == "older-page"
    assert len(snapshot.content) == 1
    assert snapshot.content[0].message_id == "msg-2"
    assert snapshot.content[0].body == child.payload
    assert snapshot.content[0].exact_link_verified


def test_verified_reply_rejects_tampered_fetched_parent_across_page_boundary(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    snapshot = service.requests_read(Actor("bob"), request_id)
    service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "provider-page-boundary-tampered-parent",
            request_id,
            snapshot.revision,
            MessageKind.RESULT,
            packet(MessageKind.RESULT, "42"),
        ),
    )
    worker.dispatch_once()
    parent, child = provider.sent
    rendered_parent = parent.payload + OBSERVED_FOOTER.encode()
    quoted_parent = b"\n".join(b">" + line for line in rendered_parent.split(b"\n"))
    provider.fetch_message = lambda message_id: ProviderMessage(
        message_id=message_id,
        thread_id="thread-1",
        parent_message_id=None,
        received_at=NOW,
        body=b"tampered parent body",
        body_available=True,
        content_source=ContentSource.PROVIDER,
        trust_label="provider_claim",
    )
    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="msg-2",
                thread_id=thread_id,
                parent_message_id="msg-1",
                received_at=NOW,
                body=child.payload
                + OBSERVED_FOOTER.encode()
                + b"\n\nOn Parent wrote:\n\n"
                + quoted_parent,
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="provider_claim",
            ),
        )
    )

    with pytest.raises(GremlinTownError) as exc:
        service.requests_read(Actor("alice"), request_id)

    assert_code(exc, ErrorCode.THREAD_SCOPE_CONFLICT)


def test_provider_trust_is_derived_from_message_link_and_unknown_mail_is_untrusted(backend):
    service, worker, _, _, provider, _ = backend
    request_id = accept_initial(service, worker)
    trusted = service.requests_read(Actor("bob"), request_id).content[0]
    assert trusted.exact_link_verified
    assert trusted.attributed_actor_id == "alice"
    assert trusted.linked_operation_id

    provider.fetch_thread = lambda thread_id, cursor=None: Page(
        items=(
            ProviderMessage(
                message_id="external-same-thread",
                thread_id=thread_id,
                parent_message_id=None,
                received_at=NOW,
                body=b"I claim to be Alice",
                body_available=True,
                content_source=ContentSource.PROVIDER,
                trust_label="authenticated_operation",
                linked_operation_id=trusted.linked_operation_id,
                attributed_actor_id="alice",
                exact_link_verified=True,
            ),
        )
    )
    external = service.requests_read(Actor("bob"), request_id).content[0]
    assert not external.exact_link_verified
    assert external.attributed_actor_id is None
    assert external.linked_operation_id is None
    assert external.trust_label == "untrusted_external"


def test_event_access_filtering_happens_before_pagination(backend):
    service, _, _, policy, _, _ = backend
    policy._routes = policy._routes | {("other", "operator", "mallory", "public-test")}
    # Put inaccessible events before and between later accessible events.
    other = service.requests_create(
        Actor("operator", is_operator=True),
        CreateRequest("other", RequestMode.ACTION_REQUESTED, "other", "mallory", packet(), None),
    )
    own = create(service, key="own")
    service.requests_report(
        Actor("mallory"), ReportRequest("other-start", other.request_id, other.revision, ReportAction.START)
    )
    service.requests_cancel(
        Actor("alice"), CancelRequest("own-cancel", own.request_id, own.revision, "done")
    )

    cursor = None
    visible = []
    cursors = []
    while True:
        page = service.events_list(Actor("alice"), {"limit": 1}, cursor)
        visible.extend(page.items)
        if page.next_cursor is None:
            break
        cursors.append(page.next_cursor)
        cursor = page.next_cursor

    assert [item["request_id"] for item in visible] == [own.request_id, own.request_id]
    assert cursors == [str(visible[0]["sequence"])]


def _shared_services(tmp_path, name):
    clock = Clock()
    policy = Policy(
        participants=(
            Participant("alice", "Alice", "alice@example.test", frozenset({"demo"}), frozenset({"public-test"})),
            Participant("bob", "Bob", "bob@example.test", frozenset({"demo"}), frozenset({"public-test"})),
            Participant(
                "operator",
                "Operator",
                "operator@example.test",
                frozenset({"demo"}),
                frozenset({"public-test"}),
                is_operator=True,
            ),
        ),
        routes=frozenset(
            {
                ("demo", "alice", "bob", "public-test"),
                ("demo", "bob", "alice", "public-test"),
            }
        ),
    )
    database = tmp_path / name
    first_store = SQLiteStore(database, now=clock)
    second_store = SQLiteStore(database, now=clock)
    provider = FakeProvider()
    return (
        GremlinTownService(first_store, policy, provider=provider, now=clock),
        GremlinTownService(second_store, policy, provider=provider, now=clock),
        first_store,
        second_store,
        policy,
        provider,
        clock,
    )


def _gate_next_transactions(stores):
    barrier = Barrier(len(stores))
    for store in stores:
        original = store.transaction

        @contextmanager
        def gated(original=original):
            barrier.wait(timeout=10)
            with original() as connection:
                yield connection

        store.transaction = gated


def _run_concurrently(*calls):
    outcomes = [None] * len(calls)

    def invoke(index, call):
        try:
            outcomes[index] = call()
        except BaseException as exc:
            outcomes[index] = exc

    threads = [Thread(target=invoke, args=(index, call)) for index, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "concurrent service call did not complete"
    return outcomes


def test_concurrent_create_idempotency_serializes_across_store_instances(tmp_path):
    services = _shared_services(tmp_path, "same-create.db")
    first, second, first_store, second_store, _, _, _ = services
    command = CreateRequest("same-key", RequestMode.ACTION_REQUESTED, "demo", "bob", packet(), None)
    _gate_next_transactions((first_store, second_store))

    outcomes = _run_concurrently(
        lambda: first.requests_create(Actor("alice"), command),
        lambda: second.requests_create(Actor("alice"), command),
    )

    assert all(not isinstance(outcome, BaseException) for outcome in outcomes)
    assert outcomes[0].operation_id == outcomes[1].operation_id
    assert first_store.connection.execute(
        "SELECT COUNT(*) FROM operations WHERE actor_id='alice' AND client_operation_id='same-key'"
    ).fetchone()[0] == 1
    assert first_store.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    first_store.close()
    second_store.close()

    services = _shared_services(tmp_path, "changed-create.db")
    first, second, first_store, second_store, _, _, _ = services
    first_command = CreateRequest("changed-key", RequestMode.ACTION_REQUESTED, "demo", "bob", packet(objective="one"), None)
    second_command = CreateRequest("changed-key", RequestMode.ACTION_REQUESTED, "demo", "bob", packet(objective="two"), None)
    _gate_next_transactions((first_store, second_store))
    outcomes = _run_concurrently(
        lambda: first.requests_create(Actor("alice"), first_command),
        lambda: second.requests_create(Actor("alice"), second_command),
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], GremlinTownError)
    assert errors[0].code is ErrorCode.OPERATION_INPUT_CONFLICT
    assert first_store.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    first_store.close()
    second_store.close()


def _prepare_concurrent_accepted_request(tmp_path, name):
    first, second, first_store, second_store, policy, provider, clock = _shared_services(tmp_path, name)
    created = create(first, key=f"create-{name}")
    worker = OutboxWorker(first_store, policy, provider, now=clock)
    worker.dispatch_once()
    current = first.requests_read(Actor("alice"), created.request_id)
    return first, second, first_store, second_store, created.request_id, current.revision


@pytest.mark.parametrize("race", ["reply_cancel", "reply_reply"])
def test_same_revision_mutations_cannot_both_commit_across_store_instances(tmp_path, race):
    first, second, first_store, second_store, request_id, revision = _prepare_concurrent_accepted_request(
        tmp_path, f"{race}.db"
    )
    reply_one = ReplyRequest("reply-one", request_id, revision, MessageKind.RESULT, packet(MessageKind.RESULT, "one"))
    if race == "reply_cancel":
        calls = (
            lambda: first.requests_reply(Actor("bob"), reply_one),
            lambda: second.requests_cancel(Actor("alice"), CancelRequest("cancel-race", request_id, revision, "stop")),
        )
    else:
        reply_two = ReplyRequest("reply-two", request_id, revision, MessageKind.RESULT, packet(MessageKind.RESULT, "two"))
        calls = (
            lambda: first.requests_reply(Actor("bob"), reply_one),
            lambda: second.requests_reply(Actor("bob"), reply_two),
        )
    _gate_next_transactions((first_store, second_store))

    outcomes = _run_concurrently(*calls)

    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    loser = next(outcome for outcome in outcomes if isinstance(outcome, BaseException))
    assert isinstance(loser, GremlinTownError)
    assert loser.code in {ErrorCode.REVISION_CONFLICT, ErrorCode.INVALID_TRANSITION, ErrorCode.RESULT_PENDING}
    first_store.close()
    second_store.close()


def test_concurrent_recovery_dispositions_cannot_both_commit(tmp_path):
    first, second, first_store, second_store, _, _, _ = _shared_services(tmp_path, "recovery-race.db")
    created = create(first, key="recovery-create")
    first_store.apply_restore_holds()
    held = first.requests_read(Actor("operator", is_operator=True), created.request_id)
    actor = Actor("operator", is_operator=True)
    release = RecoveryRequest(
        "release-race", created.request_id, held.revision, RecoveryDisposition.RELEASE_ACTIONABLE, "resume"
    )
    cancel = RecoveryRequest(
        "cancel-race", created.request_id, held.revision, RecoveryDisposition.CONFIRM_CANCELLED, "lost cancel"
    )
    _gate_next_transactions((first_store, second_store))

    outcomes = _run_concurrently(
        lambda: first.recovery_dispose(actor, release),
        lambda: second.recovery_dispose(actor, cancel),
    )

    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    loser = next(outcome for outcome in outcomes if isinstance(outcome, BaseException))
    assert isinstance(loser, GremlinTownError)
    assert loser.code in {ErrorCode.REVISION_CONFLICT, ErrorCode.INVALID_TRANSITION}
    assert first_store.connection.execute(
        "SELECT COUNT(*) FROM operations WHERE operation_kind='recovery_dispose'"
    ).fetchone()[0] == 1
    first_store.close()
    second_store.close()


def test_disable_and_route_revocation_are_rechecked_between_pages_and_cached_calls(backend):
    service, worker, _, policy, _, _ = backend
    first = create(service, key="revocation-first")
    worker.dispatch_once()
    second = create(service, key="revocation-second")
    first_page = service.events_list(Actor("alice"), {"limit": 1})
    assert first_page.next_cursor is not None

    policy._routes = frozenset(route for route in policy._routes if route[1:3] != ("alice", "bob"))
    with pytest.raises(GremlinTownError) as exc:
        create(service, key="revocation-first")
    assert_code(exc, ErrorCode.ACCESS_DENIED)
    assert service.events_list(Actor("alice"), {"limit": 10}, first_page.next_cursor).items == ()
    assert service.requests_list(Actor("alice"), {}).items == ()

    policy._routes = policy._routes | {("demo", "alice", "bob", "public-test")}
    request_page = service.requests_list(Actor("alice"), {"limit": 1})
    assert request_page.next_cursor is not None
    policy.set_enabled("alice", False)
    with pytest.raises(GremlinTownError) as exc:
        service.requests_list(Actor("alice"), {"limit": 1}, request_page.next_cursor)
    assert_code(exc, ErrorCode.ACCESS_DENIED)
    assert second.request_id


@pytest.mark.parametrize("race", ["revoke_before_claim", "cancel_after_claim"])
def test_dispatch_authorization_race_settles_one_operation_and_allows_next(backend, race):
    service, worker, store, policy, provider, _ = backend
    created = create(service, key=f"raced-{race}")
    original_authorize = worker._dispatch_authorized
    checks = 0

    def race_authorization(request, operation):
        nonlocal checks
        checks += 1
        if race == "revoke_before_claim" and checks == 1:
            policy._routes = frozenset(
                route for route in policy._routes if route[1:3] != ("alice", "bob")
            )
        elif race == "cancel_after_claim" and checks == 2:
            service.requests_cancel(
                Actor("alice"),
                CancelRequest("cancel-after-claim", created.request_id, created.revision, "race"),
            )
        return original_authorize(request, operation)

    worker._dispatch_authorized = race_authorization
    settled = worker.dispatch_once()

    if race == "revoke_before_claim":
        assert settled.operation_state is OperationState.FAILED
        assert settled.error.code is ErrorCode.ACCESS_DENIED
        policy._routes = policy._routes | {("demo", "alice", "bob", "public-test")}
    else:
        assert settled.operation_state is OperationState.CANCELLED
        assert settled.work_state is WorkState.CANCELLED
    assert provider.sent == []

    worker._dispatch_authorized = original_authorize
    unrelated = create(service, key=f"unrelated-{race}")
    accepted = worker.dispatch_once()
    assert accepted.operation_id == unrelated.operation_id
    assert accepted.operation_state is OperationState.ACCEPTED
    assert len(provider.sent) == 1
    assert store.operation(created.operation_id).operation_state is settled.operation_state


def test_retryable_send_failure_cannot_requeue_a_concurrently_cancelled_operation(backend):
    service, worker, store, _, provider, _ = backend
    created = create(service, key="cancel-during-retry")
    original_send = provider.send

    def cancel_then_throttle(envelope):
        service.requests_cancel(
            Actor("alice"),
            CancelRequest("cancel-during-provider", created.request_id, created.revision, "race"),
        )
        raise ProviderTransportError(
            "RATE_LIMITED", "provider returned HTTP 429", retryable=True, retry_after=30, status=429
        )

    provider.send = cancel_then_throttle
    settled = worker.dispatch_once()

    assert settled.operation_state is OperationState.CANCELLED
    assert settled.work_state is WorkState.CANCELLED
    assert store.operation(created.operation_id).next_attempt_at is None

    provider.send = original_send
    unrelated = create(service, key="unrelated-after-retry-race")
    accepted = worker.dispatch_once()
    assert accepted.operation_id == unrelated.operation_id
    assert accepted.operation_state is OperationState.ACCEPTED


def test_provider_message_id_collision_never_claims_second_result_and_exact_replay_is_idempotent(backend):
    service, worker, store, _, provider, _ = backend
    first_request = accept_initial(service, worker)
    second_created = create(service, key="collision-second-create")
    second_accepted = worker.dispatch_once()
    second_request = second_created.request_id

    first_revision = store.request(first_request).revision
    second_revision = second_accepted.revision
    first_result = service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "collision-first-result",
            first_request,
            first_revision,
            MessageKind.RESULT,
            packet(MessageKind.RESULT, "first"),
        ),
    )
    second_result = service.requests_reply(
        Actor("bob"),
        ReplyRequest(
            "collision-second-result",
            second_request,
            second_revision,
            MessageKind.RESULT,
            packet(MessageKind.RESULT, "second"),
        ),
    )

    def colliding_send(envelope):
        return ProviderReference("shared-result-id", "thread-1", NOW, envelope.parent_message_id)

    provider.send = colliding_send
    accepted = worker.dispatch_once()
    assert accepted.operation_state is OperationState.ACCEPTED
    result_requests = {
        first_result.operation_id: first_request,
        second_result.operation_id: second_request,
    }
    accepted_request = result_requests[accepted.operation_id]
    conflicting_operation_id = next(
        operation_id for operation_id in result_requests if operation_id != accepted.operation_id
    )
    conflicting_request = result_requests[conflicting_operation_id]
    accepted_revision = store.request(accepted_request).revision

    conflicted = worker.dispatch_once()
    assert conflicted.operation_id == conflicting_operation_id
    assert conflicted.operation_state is OperationState.UNCERTAIN
    assert conflicted.error.code is ErrorCode.THREAD_SCOPE_CONFLICT
    assert store.operation(conflicting_operation_id).recovery_required
    assert store.request(conflicting_request).work_state is WorkState.REQUESTED
    assert store.connection.execute(
        "SELECT COUNT(*) FROM message_links WHERE provider_message_id='shared-result-id'"
    ).fetchone()[0] == 1

    worker._accept(
        accepted.operation_id,
        ProviderReference(
            "shared-result-id",
            "thread-1",
            NOW,
            store.operation(accepted.operation_id).parent_message_id,
        ),
    )
    assert store.request(accepted_request).revision == accepted_revision
    assert store.connection.execute(
        "SELECT COUNT(*) FROM message_links WHERE operation_id=?", (accepted.operation_id,)
    ).fetchone()[0] == 1


def test_same_store_reader_waits_for_writer_rollback_before_observing_state(backend):
    service, _, store, _, _, _ = backend
    created = create(service, key="same-store-rollback")
    original_revision = store.request(created.request_id).revision
    updated = Barrier(2)
    allow_rollback = Event()
    reader_started = Event()
    reader_finished = Event()
    observed = []

    def writer():
        try:
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE requests SET revision=999 WHERE request_id=?", (created.request_id,)
                )
                updated.wait(timeout=10)
                assert allow_rollback.wait(timeout=10)
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

    def reader():
        updated.wait(timeout=10)
        reader_started.set()
        observed.append(store.request(created.request_id).revision)
        reader_finished.set()

    writer_thread = Thread(target=writer)
    reader_thread = Thread(target=reader)
    writer_thread.start()
    reader_thread.start()
    assert reader_started.wait(timeout=10)
    assert not reader_finished.wait(timeout=0.2)
    allow_rollback.set()
    writer_thread.join(timeout=10)
    reader_thread.join(timeout=10)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert observed == [original_revision]
    assert store.request(created.request_id).revision == original_revision


def test_restart_keeps_original_uncertainty_window_and_never_resends_after_expiry(backend):
    service, _, store, policy, provider, clock = backend
    created = create(service, key="old-interrupted-send")
    missing = create(service, key="missing-attempt-send")
    original_attempt = clock.value - timedelta(hours=25)
    with store.transaction() as connection:
        connection.execute(
            """UPDATE operations SET operation_state='sending', attempt_started_at=?,
                uncertain_since=NULL WHERE operation_id=?""",
            (original_attempt.isoformat(), created.operation_id),
        )
        connection.execute(
            """UPDATE operations SET operation_state='sending', attempt_started_at=NULL,
                uncertain_since=NULL WHERE operation_id=?""",
            (missing.operation_id,),
        )
    database = store.path
    store.close()

    restarted_store = SQLiteStore(database, now=clock)
    restarted_worker = OutboxWorker(restarted_store, policy, provider, now=clock)
    assert restarted_worker.recover_interrupted() == 2
    recovered = restarted_store.operation(created.operation_id)
    assert recovered.uncertain_since == original_attempt
    missing_attempt = restarted_store.operation(missing.operation_id)
    assert missing_attempt.operation_state is OperationState.UNCERTAIN
    assert missing_attempt.recovery_required
    assert missing_attempt.error_code == ErrorCode.RECOVERY_REQUIRED.value

    expired = restarted_worker.reconcile_once()
    assert expired.operation_state is OperationState.UNCERTAIN
    assert expired.error.code is ErrorCode.RECOVERY_REQUIRED
    assert restarted_store.operation(created.operation_id).recovery_required
    assert restarted_worker.reconcile_once() is None
    assert restarted_worker.dispatch_once() is None
    assert provider.sent == []
    restarted_store.close()


@pytest.fixture
def directory_service(tmp_path):
    policy = Policy(
        participants=(
            Participant(
                "alice",
                "Alice",
                "hub@example.test",
                frozenset({"alpha", "beta"}),
                frozenset({"public", "private"}),
            ),
            Participant(
                "bob",
                "Bob",
                "hub@example.test",
                frozenset({"alpha", "beta", "bob-only"}),
                frozenset({"public"}),
                aliases=("builder",),
            ),
            Participant(
                "carol",
                "Carol",
                "hub@example.test",
                frozenset({"alpha"}),
                frozenset({"public"}),
                aliases=("inbound-only",),
            ),
            Participant(
                "dave",
                "Dave",
                "hub@example.test",
                frozenset({"unrelated"}),
                frozenset({"public"}),
                aliases=("unrelated-alias",),
            ),
            Participant(
                "eve",
                "Eve",
                "hub@example.test",
                frozenset({"beta"}),
                frozenset({"public"}),
                aliases=("disabled-route",),
                enabled=False,
            ),
            Participant(
                "frank",
                "Frank",
                "hub@example.test",
                frozenset({"beta"}),
                frozenset({"private"}),
            ),
            Participant(
                "operator",
                "Operator",
                "hub@example.test",
                frozenset({"alpha", "beta", "unrelated"}),
                frozenset({"public", "private"}),
                is_operator=True,
            ),
        ),
        routes=frozenset(
            {
                ("alpha", "alice", "bob", "public"),
                ("alpha", "carol", "alice", "public"),
                ("beta", "alice", "eve", "public"),
                ("beta", "alice", "frank", "private"),
            }
        ),
    )
    store = SQLiteStore(tmp_path / "directory.db")
    service = GremlinTownService(store, policy)
    yield service
    store.close()


def directory_items(service, actor, project=None):
    return {
        item["participant_id"]: item
        for item in service.participants_list(actor, project).items
    }


def test_directory_without_project_is_outbound_scoped_and_redacts_projects(directory_service):
    items = directory_items(directory_service, Actor("alice"))

    assert set(items) == {"alice", "bob", "frank"}
    assert items["alice"]["projects"] == ["alpha", "beta"]
    assert items["alice"]["sensitivities"] == ["private", "public"]
    assert items["bob"]["projects"] == ["alpha"]
    assert items["bob"]["sensitivities"] == ["public"]
    assert items["bob"]["aliases"] == ["builder"]
    assert items["frank"]["projects"] == ["beta"]
    assert items["frank"]["sensitivities"] == ["private"]
    assert "carol" not in items  # An inbound-only route grants no directory visibility.
    assert "dave" not in items  # No unrelated-project participant or alias crosses the boundary.
    assert "eve" not in items  # Even a configured outbound route cannot expose a disabled target.


def test_directory_with_project_is_outbound_scoped_and_redacts_projects(directory_service):
    items = directory_items(directory_service, Actor("alice"), "alpha")

    assert set(items) == {"alice", "bob"}
    assert items["alice"]["projects"] == ["alpha"]
    assert items["bob"]["projects"] == ["alpha"]
    assert items["bob"]["sensitivities"] == ["public"]
    assert items["bob"]["aliases"] == ["builder"]
    assert "carol" not in items
    assert "dave" not in items

    beta_items = directory_items(directory_service, Actor("alice"), "beta")
    assert set(beta_items) == {"alice", "frank"}
    assert beta_items["frank"]["sensitivities"] == ["private"]
    assert "eve" not in beta_items

    with pytest.raises(GremlinTownError) as exc:
        directory_service.participants_list(Actor("alice"), "unrelated")
    assert_code(exc, ErrorCode.ACCESS_DENIED)


def test_directory_without_route_table_uses_shared_project_and_sensitivity(tmp_path):
    policy = Policy(
        participants=(
            Participant("alice", "Alice", "hub@example.test", frozenset({"alpha", "beta"}), frozenset({"public"})),
            Participant("bob", "Bob", "hub@example.test", frozenset({"alpha", "bob-only"}), frozenset({"public"})),
            Participant("carol", "Carol", "hub@example.test", frozenset({"beta"}), frozenset({"private"})),
            Participant("dave", "Dave", "hub@example.test", frozenset({"unrelated"}), frozenset({"public"})),
        ),
        routes=None,
    )
    store = SQLiteStore(tmp_path / "implicit-routes.db")
    service = GremlinTownService(store, policy)
    try:
        unfiltered = directory_items(service, Actor("alice"))
        assert set(unfiltered) == {"alice", "bob"}
        assert unfiltered["alice"]["projects"] == ["alpha", "beta"]
        assert unfiltered["alice"]["sensitivities"] == ["public"]
        assert unfiltered["bob"]["projects"] == ["alpha"]
        assert unfiltered["bob"]["sensitivities"] == ["public"]  # No route table: the shared sensitivity still applies.
        # Carol shares project "beta" with alice, but nothing in "public" and
        # "private" overlaps, so no route table means no visibility, not "any".
        assert "carol" not in unfiltered

        filtered = directory_items(service, Actor("alice"), "alpha")
        assert set(filtered) == {"alice", "bob"}
        assert all(item["projects"] == ["alpha"] for item in filtered.values())
        assert all(item["sensitivities"] == ["public"] for item in filtered.values())
    finally:
        store.close()


def test_operator_directory_remains_full_but_honors_project_filter(directory_service):
    actor = Actor("operator", is_operator=True)
    all_items = directory_items(directory_service, actor)
    assert set(all_items) == {"alice", "bob", "carol", "dave", "frank", "operator"}
    assert all_items["bob"]["projects"] == ["alpha", "beta", "bob-only"]
    # The operator view bypasses per-destination route filtering entirely (as it
    # already does for projects), so sensitivities are each participant's raw
    # configuration, not narrowed by any specific route.
    assert all_items["alice"]["sensitivities"] == ["private", "public"]
    assert all_items["bob"]["sensitivities"] == ["public"]

    filtered = directory_items(directory_service, actor, "alpha")
    assert set(filtered) == {"alice", "bob", "carol", "operator"}
    assert all(item["projects"] == ["alpha"] for item in filtered.values())


def test_directory_sensitivities_reflect_configured_routes_not_raw_overlap(tmp_path):
    """Two participants can share every sensitivity they're each configured with,
    but the directory must report only what a specific route in the table allows
    -- the same table `require_route` checks on an actual send -- never the
    wider raw intersection of their two sensitivity sets."""
    policy = Policy(
        participants=(
            Participant("alice", "Alice", "hub@example.test", frozenset({"alpha"}), frozenset({"public", "private"})),
            Participant("grace", "Grace", "hub@example.test", frozenset({"alpha"}), frozenset({"public", "private"})),
        ),
        routes=frozenset({("alpha", "alice", "grace", "public")}),
    )
    store = SQLiteStore(tmp_path / "route-narrowed.db")
    service = GremlinTownService(store, policy)
    try:
        items = directory_items(service, Actor("alice"))
        assert items["grace"]["projects"] == ["alpha"]
        assert items["grace"]["sensitivities"] == ["public"]  # Not ["private", "public"].

        # Same source of truth as the request path: a listed sensitivity passes,
        # an unlisted one is refused exactly as requests_create/reply enforce today.
        policy.require_route("alpha", "alice", "grace", "public")
        with pytest.raises(GremlinTownError) as exc:
            policy.require_route("alpha", "alice", "grace", "private")
        assert_code(exc, ErrorCode.ACCESS_DENIED)
    finally:
        store.close()
