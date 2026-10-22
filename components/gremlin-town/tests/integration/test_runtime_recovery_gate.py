from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from gremlin_town.recovery import restore_snapshot
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeOutcome, FakeTransport
from tests.integration.test_runtime_flow import _headers, _packet, _write_runtime_config


def _operator_only_config(root: Path, database: Path) -> tuple[Path, dict[str, str]]:
    root.mkdir()
    config, tokens = _write_runtime_config(root)
    text = config.read_text(encoding="utf-8")
    original_database = (root / "gremlin.sqlite3").as_posix()
    text = text.replace(original_database, database.as_posix())
    text = text.replace("operator_only = false", "operator_only = true")
    text = text.replace("normal_clients_allowed = true", "normal_clients_allowed = false")
    config.write_text(text, encoding="utf-8")
    return config, tokens


def test_consistent_precancellation_restore_requires_operator_disposition(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_config, source_tokens = _write_runtime_config(source_root)
    provider = FakeTransport(outcome=FakeOutcome.TIMEOUT_AFTER_ACCEPTANCE)
    source_runtime = build_runtime(source_config, provider=provider, start_worker=False)
    snapshot = tmp_path / "precancellation-snapshot.sqlite3"

    with TestClient(source_runtime.app) as source_client:
        created_response = source_client.post(
            "/v1/requests",
            headers=_headers(source_tokens["alice"]),
            json={
                "client_operation_id": "lost-cancellation-create",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "bob",
                "packet": _packet("request", "Prove restored work cannot outrun a lost cancellation."),
            },
        )
        assert created_response.status_code == 200
        created = created_response.json()
        uncertain = source_runtime.worker.dispatch_once()
        assert uncertain.operation_state.value == "uncertain"

        with sqlite3.connect(snapshot) as destination:
            source = sqlite3.connect(source_runtime.store.path)
            try:
                source.backup(destination)
            finally:
                source.close()

        cancelled = source_client.post(
            f"/v1/requests/{created['request_id']}/cancel",
            headers=_headers(source_tokens["alice"]),
            json={
                "client_operation_id": "lost-cancellation-after-snapshot",
                "expected_revision": created["revision"],
                "reason": "Cancellation committed only after the snapshot.",
            },
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["work_state"] == "cancelled"

    restored_database = tmp_path / "restored.sqlite3"
    restored = restore_snapshot(snapshot, restored_database)
    assert restored.audit.outbox_dispatch_blocked is True
    assert restored.request_holds_applied == 1
    assert restored.operation_holds_applied == 1

    recovery_config, recovery_tokens = _operator_only_config(
        tmp_path / "operator-runtime", restored_database
    )
    recovery_runtime = build_runtime(
        recovery_config, provider=provider, start_worker=False
    )
    with TestClient(recovery_runtime.app) as recovery_client:
        normal = recovery_client.get(
            f"/v1/requests/{created['request_id']}",
            headers=_headers(recovery_tokens["alice"]),
        )
        assert normal.status_code == 401

        operator_headers = _headers(recovery_tokens["operator"])
        held = recovery_client.get(
            f"/v1/requests/{created['request_id']}", headers=operator_headers
        )
        assert held.status_code == 200
        assert held.json()["work_state"] == "requested"
        assert held.json()["recovery_hold"] is True

        reconciled = recovery_runtime.worker.reconcile_once()
        assert reconciled is not None
        assert recovery_runtime.store.request(created["request_id"]).recovery_hold is True

        still_held = recovery_client.get(
            f"/v1/requests/{created['request_id']}", headers=operator_headers
        ).json()
        assert still_held["recovery_hold"] is True
        disposed = recovery_client.post(
            f"/v1/requests/{created['request_id']}/recovery-disposition",
            headers=operator_headers,
            json={
                "client_operation_id": "lost-cancellation-disposition",
                "expected_revision": still_held["revision"],
                "disposition": "confirm_cancelled",
                "reason": "The consistent snapshot predates the authoritative cancellation.",
            },
        )
        assert disposed.status_code == 200
        assert disposed.json()["work_state"] == "cancelled"
        assert recovery_runtime.store.request(created["request_id"]).recovery_hold is False
