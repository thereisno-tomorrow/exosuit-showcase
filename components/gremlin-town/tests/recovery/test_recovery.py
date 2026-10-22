from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from gremlin_town.recovery import (
    RecoveryError,
    RecoveryErrorCode,
    audit_recovery,
    check_schema_compatibility,
    restore_snapshot,
)
from gremlin_town.storage import SQLiteStore
from gremlin_town.storage.migrations import SCHEMA_VERSION


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(path: Path) -> tuple[str, int, int, int]:
    stat = path.stat()
    return (_digest(path), stat.st_size, stat.st_mtime_ns, stat.st_mode)


def _make_snapshot(path: Path) -> dict[str, object]:
    store = SQLiteStore(path)
    now = "2026-09-20T12:00:00+00:00"
    with store.transaction() as connection:
        requests = (
            ("req-lost-cancel", "requested", 0),
            ("req-sending", "in_progress", 0),
            ("req-uncertain", "blocked", 0),
            ("req-terminal", "cancelled", 0),
        )
        for request_id, work_state, hold in requests:
            connection.execute(
                """INSERT INTO requests(request_id, mode, project, requester_id, recipient_id,
                objective, sensitivity, work_state, revision, recovery_hold, prior_request_id,
                thread_id, created_at, updated_at)
                VALUES (?, 'action_requested', 'demo', 'alice', 'bob', ?, 'public-test', ?, 1, ?, NULL, ?, ?, ?)""",
                (request_id, request_id, work_state, hold, "thread-terminal" if request_id == "req-terminal" else None, now, now),
            )
        operations = (
            ("op-queued", "req-lost-cancel", "queued", b"queued-bytes", "retry-queued", None, None),
            ("op-sending", "req-sending", "sending", b"sending-bytes", "retry-sending", None, None),
            ("op-uncertain", "req-uncertain", "uncertain", b"uncertain-bytes", "retry-uncertain", None, None),
            (
                "op-terminal",
                "req-terminal",
                "accepted",
                b"terminal-bytes",
                "retry-terminal",
                "provider-message-1",
                "thread-terminal",
            ),
        )
        for operation_id, request_id, state, payload, retry_key, message_id, thread_id in operations:
            connection.execute(
                """INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id,
                operation_kind, message_kind, input_digest, accepted_at, operation_state,
                payload, payload_sha256, provider_retry_key, recipient_address, route_source_id,
                route_destination_id, route_sensitivity, provider_message_id, provider_thread_id,
                provider_accepted_at, recovery_hold)
                VALUES (?, ?, 'alice', ?, 'create', 'request', 'digest', ?, ?, ?, ?, ?,
                'hub@example.test', 'alice', 'bob', 'public-test', ?, ?, ?, 0)""",
                (
                    operation_id,
                    f"client-{operation_id}",
                    request_id,
                    now,
                    state,
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                    retry_key,
                    message_id,
                    thread_id,
                    now if message_id else None,
                ),
            )
        connection.execute(
            """INSERT INTO message_links(provider_message_id, request_id, operation_id,
            provider_thread_id, parent_message_id, message_kind, actor_id, content_sha256,
            accepted_at, trust_label, exact_link_verified)
            VALUES ('provider-message-1', 'req-terminal', 'op-terminal', 'thread-terminal',
            NULL, 'request', 'alice', ?, ?, 'authenticated_operation', 1)""",
            (hashlib.sha256(b"terminal-bytes").hexdigest(), now),
        )
        connection.execute(
            "INSERT INTO sync_state(name, cursor, overlap_checkpoint, last_success_at) VALUES ('inbox', 'cursor-9', 'cursor-7', ?)",
            (now,),
        )
        connection.execute(
            """INSERT INTO events(request_id, operation_id, actor_id, event_type, detail_json, occurred_at)
            VALUES ('req-terminal', 'op-terminal', 'alice', 'request_cancelled', '{"reason":"historical"}', ?)""",
            (now,),
        )
    store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.close()
    return {
        "payloads": {row[0]: row[3] for row in operations},
        "retry_keys": {row[0]: row[4] for row in operations},
    }


def test_restore_is_create_only_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.db"
    destination = tmp_path / "restored.db"
    expected = _make_snapshot(source)
    source_digest = _digest(source)
    source_stat = source.stat()
    source_files = tuple(sorted(path.name for path in tmp_path.glob("snapshot.db*")))

    result = restore_snapshot(source, destination)

    assert result.destination == destination.resolve()
    assert result.schema_version == SCHEMA_VERSION
    assert result.request_holds_applied == 3
    assert result.operation_holds_applied == 3
    assert result.audit.safe_for_operator_inspection
    assert not result.audit.normal_clients_enabled
    assert _digest(source) == source_digest
    assert source.stat().st_mtime_ns == source_stat.st_mtime_ns
    assert tuple(sorted(path.name for path in tmp_path.glob("snapshot.db*"))) == source_files

    connection = sqlite3.connect(destination)
    connection.row_factory = sqlite3.Row
    requests = {row["request_id"]: row for row in connection.execute("SELECT * FROM requests")}
    assert requests["req-lost-cancel"]["recovery_hold"] == 1
    assert requests["req-sending"]["recovery_hold"] == 1
    assert requests["req-uncertain"]["recovery_hold"] == 1
    assert requests["req-terminal"]["work_state"] == "cancelled"
    assert requests["req-terminal"]["recovery_hold"] == 0

    operations = {row["operation_id"]: row for row in connection.execute("SELECT * FROM operations")}
    for operation_id in ("op-queued", "op-sending", "op-uncertain"):
        assert operations[operation_id]["recovery_hold"] == 1
        assert operations[operation_id]["payload"] == expected["payloads"][operation_id]
        assert operations[operation_id]["provider_retry_key"] == expected["retry_keys"][operation_id]
    assert operations["op-terminal"]["provider_message_id"] == "provider-message-1"
    assert operations["op-terminal"]["provider_thread_id"] == "thread-terminal"
    assert tuple(connection.execute("SELECT cursor, overlap_checkpoint FROM sync_state WHERE name='inbox'").fetchone()) == (
        "cursor-9",
        "cursor-7",
    )
    assert connection.execute("SELECT COUNT(*) FROM events WHERE event_type='request_cancelled'").fetchone()[0] == 1
    connection.close()


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.db"
    destination = tmp_path / "existing.db"
    _make_snapshot(source)
    destination.write_bytes(b"do-not-overwrite")

    with pytest.raises(RecoveryError) as exc:
        restore_snapshot(source, destination)

    assert exc.value.code is RecoveryErrorCode.DESTINATION_EXISTS
    assert destination.read_bytes() == b"do-not-overwrite"


def test_restore_refuses_wal_sidecars_without_changing_source_or_destination(tmp_path: Path) -> None:
    source = tmp_path / "wal-snapshot.db"
    destination = tmp_path / "restored.db"
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE proof(value TEXT NOT NULL)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO proof(value) VALUES ('committed-only-in-wal')")
        connection.commit()

        immutable = sqlite3.connect(
            f"file:{source.resolve().as_posix()}?mode=ro&immutable=1", uri=True
        )
        try:
            assert immutable.execute("SELECT COUNT(*) FROM proof").fetchone()[0] == 0
        finally:
            immutable.close()

        source_files = (source, Path(f"{source}-wal"), Path(f"{source}-shm"))
        assert all(path.is_file() for path in source_files)
        before = {path.name: _fingerprint(path) for path in source_files}

        with pytest.raises(RecoveryError) as exc:
            restore_snapshot(source, destination)

        assert exc.value.code is RecoveryErrorCode.SOURCE_WAL_SIDECARS_PRESENT
        assert not destination.exists()
        assert not tuple(tmp_path.glob(".restored.db.*.restore"))
        assert all(path.is_file() for path in source_files)
        assert {path.name: _fingerprint(path) for path in source_files} == before
    finally:
        connection.close()


@pytest.mark.parametrize("case", ["corrupt", "newer"])
def test_invalid_source_never_promotes_partial_destination(tmp_path: Path, case: str) -> None:
    source = tmp_path / f"{case}.db"
    destination = tmp_path / "restored.db"
    if case == "corrupt":
        source.write_bytes(b"not a sqlite database")
        expected = RecoveryErrorCode.INTEGRITY_FAILED
    else:
        connection = sqlite3.connect(source)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        expected = RecoveryErrorCode.SCHEMA_TOO_NEW

    with pytest.raises(RecoveryError) as exc:
        restore_snapshot(source, destination)

    assert exc.value.code is expected
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".restored.db.*.restore"))


def test_schema_compatibility_is_typed_and_rejects_unsafe_rollback(tmp_path: Path) -> None:
    current = tmp_path / "current.db"
    _make_snapshot(current)
    connection = sqlite3.connect(current)
    compatibility = check_schema_compatibility(connection)
    assert compatibility.compatible
    assert compatibility.database_version == SCHEMA_VERSION
    connection.close()

    newer = sqlite3.connect(":memory:")
    newer.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RecoveryError) as exc:
        check_schema_compatibility(newer)
    assert exc.value.code is RecoveryErrorCode.SCHEMA_TOO_NEW
    newer.close()


def test_audit_detects_each_missing_hold_independently(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.db"
    destination = tmp_path / "restored.db"
    _make_snapshot(source)
    restore_snapshot(source, destination)

    connection = sqlite3.connect(destination)
    connection.execute("UPDATE requests SET recovery_hold=0 WHERE request_id='req-lost-cancel'")
    connection.commit()
    request_failure = audit_recovery(destination)
    assert request_failure.missing_request_holds == ("req-lost-cancel",)
    assert not request_failure.safe_for_operator_inspection
    assert not request_failure.outbox_dispatch_blocked

    connection.execute("UPDATE requests SET recovery_hold=1 WHERE request_id='req-lost-cancel'")
    connection.execute("UPDATE operations SET recovery_hold=0 WHERE operation_id='op-queued'")
    connection.commit()
    connection.close()
    operation_failure = audit_recovery(destination)
    assert operation_failure.missing_operation_holds == ("op-queued",)
    assert not operation_failure.safe_for_operator_inspection
    assert not operation_failure.outbox_dispatch_blocked


def test_standalone_audit_does_not_invent_external_runtime_state(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.db"
    destination = tmp_path / "restored.db"
    _make_snapshot(source)
    restored = restore_snapshot(source, destination)
    assert restored.audit.normal_clients_enabled is False
    assert restored.audit.safe_for_operator_inspection

    standalone = audit_recovery(destination)
    assert standalone.normal_clients_enabled is None
    assert not standalone.safe_for_operator_inspection


def test_schema_two_restore_holds_survive_additive_binding_upgrade(tmp_path, monkeypatch):
    import gremlin_town.storage.sqlite as storage_module
    source = tmp_path / 'legacy.db'
    destination = tmp_path / 'restored-legacy.db'
    with monkeypatch.context() as scoped:
        scoped.setattr(storage_module, 'SCHEMA_VERSION', 2)
        _make_snapshot(source)
    result = restore_snapshot(source, destination)
    assert result.schema_version == 2
    restored = SQLiteStore(destination)
    try:
        assert restored.fetchone('PRAGMA user_version')[0] == 3
        assert restored.fetchone("SELECT COUNT(*) FROM operations WHERE backend_binding != 'agentmail'")[0] == 0
        assert restored.operation('op-queued').recovery_hold
        assert restored.operation('op-sending').recovery_hold
        assert restored.operation('op-uncertain').recovery_hold
    finally:
        restored.close()
