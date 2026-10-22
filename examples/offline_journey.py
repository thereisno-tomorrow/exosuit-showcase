"""Synthetic, offline walkthrough of the real request and continuity engines.

Run from an environment with both local component packages installed. No server,
test helpers, credentials, external accounts, model calls or network I/O are used.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from brain_core.continuity import append_event, recover_continuity, start_continuity
from brain_core.records import Evidence, RecordStore
from gremlin_town.contracts import (
    Actor, CheckOutcome, CheckRequest, ContentAvailability, ContentSource,
    CreateRequest, ErrorCode, MessageKind, OperationState, OutboundEnvelope,
    Packet, Page, ProviderMessage, ProviderReference, ReplyRequest,
    ReportAction, ReportRequest, RequestMode, WorkState,
)
from gremlin_town.domain import Participant, Policy
from gremlin_town.service import GremlinTownError, GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.workers import OutboxWorker

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
SESSION = "synthetic-launch-review"
OBJECTIVE = "Check the launch checklist: 3 checks, 0 failures."
RESULT = "3 checks passed; 0 failures. Ready for human review."
NEXT_STEP = "Ask the human reviewer whether to publish the synthetic launch."


def require(condition: bool, message: str) -> None:
    """Keep demo checks active even when Python is run with -O."""
    if not condition:
        raise RuntimeError(message)


class MemoryProvider:
    """External transport substitute; provider receipts and bytes live in memory.

    GT policy, persistence, correlation and state transitions are real. This fake
    models an accepted write whose response is lost; it is not a GitHub emulator.
    """

    def __init__(self) -> None:
        self.messages: dict[str, ProviderMessage] = {}
        self.receipts: dict[str, ProviderReference] = {}
        self.send_calls = 0
        self.lose_next_receipt = True

    def send(self, envelope: OutboundEnvelope) -> ProviderReference:
        self.send_calls += 1
        if envelope.provider_retry_key not in self.receipts:
            message_id = f"synthetic-message-{len(self.messages) + 1}"
            reference = ProviderReference(message_id, "synthetic-thread", NOW,
                                          envelope.parent_message_id)
            self.receipts[envelope.provider_retry_key] = reference
            self.messages[message_id] = ProviderMessage(
                message_id, reference.thread_id, envelope.parent_message_id,
                NOW, envelope.payload, True, ContentSource.PROVIDER,
                "provider_claim",
            )
        if self.lose_next_receipt:
            self.lose_next_receipt = False
            raise TimeoutError("Synthetic fault: accepted, response lost")
        return self.receipts[envelope.provider_retry_key]

    def reconcile(self, envelope: OutboundEnvelope) -> ProviderReference | None:
        return self.receipts.get(envelope.provider_retry_key)

    def fetch_message(self, message_id: str) -> ProviderMessage:
        return self.messages[message_id]

    def fetch_thread(self, thread_id: str, cursor: str | None = None) -> Page:
        require(cursor is None, "The small demo thread should fit one page")
        return Page(tuple(m for m in self.messages.values() if m.thread_id == thread_id),
                    as_of=NOW)


def packet(kind: MessageKind, objective: str) -> Packet:
    return Packet(
        schema_version=1, kind=kind, session_reference=SESSION,
        objective=objective, context="Invented launch checklist for this demo only.",
        constraints={"network": "forbidden", "publication": "human review required"},
        sources=(), next_action="Return the checklist result; do not publish.",
        expected_reply="Check count and failure count.", sensitivity="public-test",
    )


def policy() -> Policy:
    return Policy(
        participants=tuple(
            Participant(name, name.title(), f"{name}@example.test",
                        frozenset({project}), frozenset({"public-test"}))
            for name, project in (("alice", "showcase"), ("bob", "showcase"),
                                  ("mallory", "unrelated"))
        ),
        routes=frozenset({("showcase", "alice", "bob", "public-test"),
                          ("showcase", "bob", "alice", "public-test")}),
    )


def journey(root: Path) -> None:
    print("EXOSUIT / synthetic offline journey")
    print("Real: policy, SQLite outbox, service, record store. Fake: transport and work result.")
    provider = MemoryProvider()
    rules = policy()
    db_path = root / "requests.sqlite3"
    store = SQLiteStore(db_path, now=lambda: NOW)
    service = GremlinTownService(store, rules, provider=provider, now=lambda: NOW)
    worker = OutboxWorker(store, rules, provider, now=lambda: NOW)
    alice, bob = Actor("alice"), Actor("bob")
    try:
        command = CreateRequest("synthetic-create", RequestMode.ACTION_REQUESTED,
                                "showcase", "bob", packet(MessageKind.REQUEST, OBJECTIVE))
        created = service.requests_create(alice, command)
        require(created.operation_state is OperationState.QUEUED, "Request must queue")
        replay = service.requests_create(alice, command)
        require(replay.operation_id == created.operation_id, "Replay duplicated operation")
        print("1. Alice -> Bob: queued; replay uses the same operation.")

        uncertain = worker.dispatch_once()
        require(uncertain.operation_state is OperationState.UNCERTAIN, "Lost receipt must be uncertain")
        require(len(provider.messages) == 1, "Provider should already hold the message")
        print("2. Injected lost receipt: provider has 1 message; hub reports uncertain.")

        # Reopen the durable database and construct new worker/service objects.
        # The fake provider remains alive, modelling an independent provider.
        store.close()
        store = SQLiteStore(db_path, now=lambda: NOW)
        service = GremlinTownService(store, rules, provider=provider, now=lambda: NOW)
        worker = OutboxWorker(store, rules, provider, now=lambda: NOW)
        accepted = worker.reconcile_once()
        require(accepted.operation_state is OperationState.ACCEPTED, "Receipt not recovered")
        require(provider.send_calls == 1 and len(provider.messages) == 1, "Reconciliation resent")
        print("3. Reopened SQLite, reconciled: accepted; 1 send, 1 message, no resend.")

        try:
            service.requests_read(Actor("mallory"), created.request_id)
        except GremlinTownError as exc:
            require(exc.code is ErrorCode.ACCESS_DENIED, "Unexpected access failure")
        else:
            raise RuntimeError("An unrelated participant read the request")
        print("4. Unrelated participant cannot read the request: ACCESS_DENIED.")

        snapshot = service.requests_read(bob, created.request_id)
        require(snapshot.content_availability is ContentAvailability.COMPLETE, "Provider read incomplete")
        require(all(m.exact_link_verified for m in snapshot.content), "Provider link unverified")
        started = service.requests_report(bob, ReportRequest(
            "synthetic-start", created.request_id, snapshot.revision, ReportAction.START))
        service.requests_reply(bob, ReplyRequest(
            "synthetic-result", created.request_id, started.revision,
            MessageKind.RESULT, packet(MessageKind.RESULT, RESULT)))
        returned = worker.dispatch_once()
        require(returned.work_state is WorkState.RESULT_RETURNED, "Result not returned")
        result_snapshot = service.requests_read(alice, created.request_id)
        result_messages = [m for m in result_snapshot.content if m.attributed_actor_id == "bob"]
        require(len(result_messages) == 1 and result_messages[0].exact_link_verified,
                "Result must have a verified operation link")
        require(json.loads(result_messages[0].body)["packet"]["objective"] == RESULT,
                "Independent provider read disagrees with the expected result")
        print("5. Bob returns a synthetic result; Alice reads verified provider bytes.")

        try:
            service.requests_check(bob, CheckRequest(
                "synthetic-self-check", created.request_id, result_snapshot.revision,
                CheckOutcome.ACCEPTED, "I checked my own work"))
        except GremlinTownError as exc:
            require(exc.code is ErrorCode.ACCESS_DENIED, "Unexpected self-check failure")
        else:
            raise RuntimeError("Recipient checked its own result")
        service.requests_check(alice, CheckRequest(
            "synthetic-independent-check", created.request_id, result_snapshot.revision,
            CheckOutcome.ACCEPTED, "Synthetic verifier compared exact checklist result bytes."))
        events = service.events_list(alice, {"request_id": created.request_id, "limit": 100})
        require(any(e["event_type"] == "result_check_accepted" for e in events.items),
                "Independent check is missing from durable events")
        print("6. Bob cannot self-check; Alice's independent check enters the event log.")
    finally:
        store.close()

    # This is an explicit application-level handoff, not an automatic GT->Brain
    # integration claim. The evidence says what this synthetic script observed.
    brain_path = root / "brain"
    brain_path.mkdir()
    evidence = Evidence("session_message", "synthetic:offline-journey",
                        hashlib.sha256(RESULT.encode()).hexdigest(), RESULT,
                        session_id=SESSION, actor="alice")
    brain = RecordStore(brain_path)
    start_continuity(brain, session_id=SESSION, intent=OBJECTIVE, sources=[],
                     evidence=evidence, now=NOW, next_step=NEXT_STEP)
    append_event(brain, session_id=SESSION, event="effect", value=RESULT,
                 evidence=evidence, now=NOW + timedelta(seconds=1))
    recovered = recover_continuity(RecordStore(brain_path), session_id=SESSION)
    require(recovered["effects"] == [RESULT], "Durable result did not recover")
    require(recovered["next_step"] == NEXT_STEP, "Stopping note did not recover")
    print("7. Fresh Brain Core reader recovers the result and stopping note:")
    print(f"   {recovered['next_step']}")
    print("PASS: all invariants checked. No external messages or publication.")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="exosuit-synthetic-") as directory:
        journey(Path(directory))
