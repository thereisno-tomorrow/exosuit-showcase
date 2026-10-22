"""Backend selection must survive default changes, retries and restoration."""
import sqlite3

import pytest

from gremlin_town.contracts import Actor, MessageKind, ReplyRequest, OperationState, Packet
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.storage.migrations import MIGRATIONS
from gremlin_town.transport import BoundTransport, FakeTransport, FakeOutcome
from gremlin_town.recovery import restore_snapshot
from gremlin_town.transport.errors import ReconciliationRequiredError
from gremlin_town.workers import OutboxWorker
from test_github_transport import github_backend, queue_request


def bind(service, worker, store, providers, default):
    router = BoundTransport(store, providers)
    service.provider = worker.provider = router
    service.default_backend = default
    return router


def binding(store, operation):
    return store.fetchone('SELECT backend_binding FROM operations WHERE operation_id=?', (operation,))[0]


def test_default_change_restart_keeps_queued_send_and_old_reply(github_backend):
    service, worker, store, state = github_backend
    github = service.provider
    legacy = FakeTransport()
    providers = {'agentmail': legacy, 'github:test-owner/mailbox': github}
    bind(service, worker, store, providers, 'agentmail')
    old = queue_request(service, 'old')
    assert binding(store, old.operation_id) == 'agentmail'
    path, policy = store.path, service.policy
    store.close()
    store = SQLiteStore(path)
    try:
        router = BoundTransport(store, providers)
        service = GremlinTownService(store, policy, provider=router, default_backend='github:test-owner/mailbox')
        worker = OutboxWorker(store, policy, router)
        worker.dispatch_once()
        assert legacy.send_attempts == 1 and state['calls'] == []
        current = service.requests_read(Actor('alice'), old.request_id)
        reply = service.requests_reply(Actor('alice'), ReplyRequest(
            'old-reply', old.request_id, current.revision, MessageKind.CLARIFICATION,
            Packet(1, MessageKind.CLARIFICATION, None, 'clarify', '', {}, [], '', '', 'public-test'),
        ))
        assert binding(store, reply.operation_id) == 'agentmail'
        worker.dispatch_once()
        assert legacy.send_attempts == 2 and state['calls'] == []
        new = queue_request(service, 'new')
        assert binding(store, new.operation_id) == 'github:test-owner/mailbox'
        worker.dispatch_once()
        snapshot = service.requests_read(Actor('bob'), new.request_id)
        assert snapshot.content[0].exact_link_verified
        assert snapshot.content[0].attributed_actor_id == 'alice'
        assert legacy.send_attempts == 2
        reply = service.requests_reply(Actor('bob'), ReplyRequest(
            'github-reply', new.request_id, snapshot.revision, MessageKind.CLARIFICATION,
            Packet(1, MessageKind.CLARIFICATION, None, 'clarify', '', {}, [], '', '', 'public-test'),
        ))
        assert binding(store, reply.operation_id) == 'github:test-owner/mailbox'
        worker.dispatch_once()
        result = service.requests_read(Actor('alice'), new.request_id)
        assert len(result.content) == 2
        assert all(item.exact_link_verified for item in result.content)
        assert result.content[1].attributed_actor_id == 'bob'
    finally:
        store.close()


def test_uncertainty_is_pinned_after_default_change(github_backend):
    service, worker, store, state = github_backend
    github = service.provider
    legacy = FakeTransport()
    bind(service, worker, store, {'agentmail': legacy, 'github:test-owner/mailbox': github}, 'github:test-owner/mailbox')
    state['lose_response'] = state['drop_write'] = True
    queued = queue_request(service)
    assert worker.dispatch_once().operation_state is OperationState.UNCERTAIN
    service.default_backend = 'agentmail'
    worker.reconcile_once()
    assert store.operation(queued.operation_id).recovery_required
    assert worker.dispatch_once() is None
    assert legacy.send_attempts == 0
    assert sum(method == 'POST' for method, _ in state['calls']) == 1


def test_router_refuses_none_from_nonlegacy_reconcile(github_backend):
    service, worker, store, _ = github_backend
    fake = FakeTransport()
    router = bind(service, worker, store, {'github:test-owner/mailbox': fake}, 'github:test-owner/mailbox')
    queued = queue_request(service)
    with pytest.raises(ReconciliationRequiredError):
        router.reconcile(store.outbound_envelope(queued.operation_id))
    assert fake.send_attempts == 0


def test_legacy_uncertain_operation_reconciles_on_original_provider(github_backend):
    service, worker, store, state = github_backend
    legacy = FakeTransport(outcome=FakeOutcome.TIMEOUT_AFTER_ACCEPTANCE)
    bind(service, worker, store, {'agentmail': legacy, 'github:test-owner/mailbox': service.provider}, 'agentmail')
    queued = queue_request(service)
    assert worker.dispatch_once().operation_state is OperationState.UNCERTAIN
    service.default_backend = 'github:test-owner/mailbox'
    assert worker.reconcile_once().operation_state is OperationState.ACCEPTED
    assert binding(store, queued.operation_id) == 'agentmail'
    assert legacy.send_attempts == 1 and state['calls'] == []


def test_removed_repository_binding_never_falls_back(github_backend):
    service, worker, store, state = github_backend
    bind(service, worker, store, {'github:test-owner/mailbox': service.provider}, 'github:test-owner/mailbox')
    queued = queue_request(service)
    other = FakeTransport()
    service.provider = worker.provider = BoundTransport(store, {'github:other/repo': other, 'agentmail': other})
    worker.dispatch_once()
    assert store.operation(queued.operation_id).operation_state is OperationState.QUEUED
    assert state['calls'] == [] and other.send_attempts == 0


def test_bindings_immutable_and_restore_holds_preserve_them(github_backend, tmp_path):
    service, worker, store, state = github_backend
    bind(service, worker, store, {'github:test-owner/mailbox': service.provider}, 'github:test-owner/mailbox')
    queued = queue_request(service)
    for table in ('requests', 'operations'):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            store.connection.execute(f"UPDATE {table} SET backend_binding='agentmail'")
    snapshot = tmp_path / 'snapshot.sqlite'
    with sqlite3.connect(snapshot) as db:
        store.connection.backup(db)
    destination = tmp_path / 'restored.sqlite'
    assert restore_snapshot(snapshot, destination).schema_version == 3
    restored = SQLiteStore(destination)
    try:
        assert binding(restored, queued.operation_id) == 'github:test-owner/mailbox'
        assert restored.operation(queued.operation_id).recovery_hold
        assert restored.outbound_envelope(queued.operation_id) == store.outbound_envelope(queued.operation_id)
    finally:
        restored.close()
    assert state['calls'] == []


def test_schema_two_migration_and_atomic_failure(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite'
    db = sqlite3.connect(path)
    db.executescript(MIGRATIONS[1] + MIGRATIONS[2] + 'PRAGMA user_version=2;')
    db.execute("INSERT INTO requests(request_id,mode,project,requester_id,recipient_id,objective,sensitivity,revision,created_at,updated_at) VALUES('legacy','context_only','p','a','b','','internal',1,'now','now')")
    db.commit()
    db.close()
    real = MIGRATIONS[3]
    monkeypatch.setitem(MIGRATIONS, 3, real + '\nINVALID SQL;')
    with pytest.raises(sqlite3.OperationalError):
        SQLiteStore(path)
    db = sqlite3.connect(path)
    assert db.execute('PRAGMA user_version').fetchone()[0] == 2
    assert 'backend_binding' not in [r[1] for r in db.execute('PRAGMA table_info(requests)')]
    db.close()
    monkeypatch.setitem(MIGRATIONS, 3, real)
    store = SQLiteStore(path)
    assert store.fetchone("SELECT backend_binding FROM requests WHERE request_id='legacy'")[0] == 'agentmail'
    store.close()
