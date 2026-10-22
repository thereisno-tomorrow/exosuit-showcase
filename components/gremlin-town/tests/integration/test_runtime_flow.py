from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from adapters.hermes.hub_client import HubClientConfig, HubClientError, HubHTTPSClient
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport, frame_wire_message, parse_wire_message


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_runtime_config(
    root: Path, *, public_actor_id: str | None = None
) -> tuple[Path, dict[str, str]]:
    tokens = {
        "alice": "alice-token-with-more-than-thirty-two-bytes",
        "bob": "bob-token-with-more-than-thirty-two-bytes-00",
        "operator": "operator-token-with-more-than-thirty-two-bytes",
    }
    participants = root / "participants.toml"
    participants.write_text(
        "\n".join(
            [
                "[[participant]]",
                'id = "alice"',
                'display_name = "Alice"',
                'address = "hub@example.test"',
                'role = "participant"',
                "enabled = true",
                f'token_sha256 = "{_hash(tokens["alice"])}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[participant]]",
                'id = "bob"',
                'display_name = "Bob"',
                'address = "hub@example.test"',
                'role = "participant"',
                "enabled = true",
                f'token_sha256 = "{_hash(tokens["bob"])}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[participant]]",
                'id = "operator"',
                'display_name = "Operator"',
                'address = "hub@example.test"',
                'role = "operator"',
                "enabled = true",
                f'token_sha256 = "{_hash(tokens["operator"])}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[route]]",
                'project = "demo"',
                'source = "alice"',
                'destination = "bob"',
                'sensitivity = "public-test"',
                "",
                "[[route]]",
                'project = "demo"',
                'source = "bob"',
                'destination = "alice"',
                'sensitivity = "public-test"',
            ]
        ),
        encoding="utf-8",
    )
    config = root / "config.toml"
    config_lines = [
                "[server]",
                'host = "127.0.0.1"',
                "port = 8765",
                f'database = "{(root / "gremlin.sqlite3").as_posix()}"',
                "operator_only = false",
                "[participants]",
                f'file = "{participants.as_posix()}"',
                "[recovery]",
                "normal_clients_allowed = true",
                "outbound_dispatch_allowed = false",
    ]
    if public_actor_id is not None:
        operations = ", ".join(
            f'"{operation}"'
            for operation in (
                "participants.list",
                "requests.create",
                "requests.list",
                "requests.read",
                "requests.report",
                "requests.reply",
                "requests.cancel",
                "requests.check",
                "operations.get",
                "events.list",
            )
        )
        config_lines.extend(
            [
                "[public_gateway]",
                'host = "127.0.0.1"',
                "port = 8766",
                f'actor_id = "{public_actor_id}"',
                f"allowed_operations = [{operations}]",
            ]
        )
    config.write_text("\n".join(config_lines), encoding="utf-8")
    return config, tokens


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _packet(kind: str, objective: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind,
        "session_reference": "fresh-session",
        "objective": objective,
        "context": "Use only this packet.",
        "constraints": {"scope": "bounded"},
        "sources": [],
        "next_action": "Return the result.",
        "expected_reply": "A concise result.",
        "sensitivity": "public-test",
    }


def test_real_asgi_create_read_report_reply_flow(tmp_path: Path) -> None:
    config, tokens = _write_runtime_config(tmp_path)
    provider = FakeTransport()
    runtime = build_runtime(config, provider=provider, start_worker=False)
    with TestClient(runtime.app) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/v1/participants", headers=_headers("wrong-token-with-more-than-thirty-two-bytes")).status_code == 401

        created_response = client.post(
            "/v1/requests",
            headers=_headers(tokens["alice"]),
            json={
                "client_operation_id": "vertical-create-1",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "bob",
                "packet": _packet("request", "Add 17 and 25."),
            },
        )
        assert created_response.status_code == 200, created_response.text
        created = created_response.json()
        request_id = created["request_id"]
        assert created["operation_state"] == "queued"
        create_envelope = runtime.store.outbound_envelope(created["operation_id"])
        create_wire = parse_wire_message(create_envelope.payload)
        assert create_wire.sender == "alice" and create_wire.recipient == "bob"
        assert frame_wire_message(create_wire) == create_envelope.payload
        sent = runtime.worker.dispatch_once()
        assert sent.operation_state == "accepted"
        assert provider.sent_payloads[create_envelope.provider_retry_key] == create_envelope.payload

        read = client.get(
            f"/v1/requests/{request_id}", headers=_headers(tokens["bob"])
        ).json()
        assert read["work_state"] == "requested"
        assert read["content_availability"] == "complete"

        started = client.post(
            f"/v1/requests/{request_id}/reports",
            headers=_headers(tokens["bob"]),
            json={
                "client_operation_id": "vertical-start-1",
                "expected_revision": read["revision"],
                "action": "start",
            },
        ).json()
        assert started["work_state"] == "in_progress"

        reply_response = client.post(
            f"/v1/requests/{request_id}/replies",
            headers=_headers(tokens["bob"]),
            json={
                "client_operation_id": "vertical-result-1",
                "expected_revision": started["revision"],
                "kind": "result",
                "packet": _packet("result", "42"),
            },
        )
        assert reply_response.status_code == 200, reply_response.text
        assert reply_response.json()["operation_state"] == "queued"
        reply_envelope = runtime.store.outbound_envelope(reply_response.json()["operation_id"])
        reply_wire = parse_wire_message(reply_envelope.payload)
        assert reply_wire.sender == "bob" and reply_wire.recipient == "alice"
        assert frame_wire_message(reply_wire) == reply_envelope.payload
        returned = runtime.worker.dispatch_once()
        assert returned.work_state == "result_returned"
        assert provider.sent_payloads[reply_envelope.provider_retry_key] == reply_envelope.payload

        final = client.get(
            f"/v1/requests/{request_id}", headers=_headers(tokens["alice"])
        ).json()
        assert final["work_state"] == "result_returned"
        assert len(final["content"]) == 2


def test_scoped_https_adapter_runs_all_ten_operations_against_canonical_public_app(
    tmp_path: Path,
) -> None:
    config, tokens = _write_runtime_config(tmp_path, public_actor_id="bob")
    provider = FakeTransport()
    runtime = build_runtime(config, provider=provider, start_worker=False)
    assert runtime.public_app is not None

    with (
        TestClient(runtime.app) as private,
        TestClient(runtime.public_app, base_url="https://hub.example.test") as public,
    ):
        adapter = HubHTTPSClient(
            HubClientConfig("https://hub.example.test", tokens["bob"]),
            client=public,
        )

        participants = adapter.call("participants.list", {"project": "demo"})
        assert {item["participant_id"] for item in participants["items"]} == {"alice", "bob"}

        outbound = adapter.call(
            "requests.create",
            {
                "client_operation_id": "adapter-create-outbound",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "alice",
                "packet": _packet("request", "Adapter-created request"),
            },
        )
        runtime.worker.dispatch_once()
        assert adapter.call("operations.get", {"operation_id": outbound["operation_id"]})[
            "operation_state"
        ] == "accepted"
        listed = adapter.call("requests.list", {"filters": {"direction": "outbox"}})
        assert outbound["request_id"] in {item["request_id"] for item in listed["items"]}
        outbound_read = adapter.call("requests.read", {"request_id": outbound["request_id"]})
        events = adapter.call(
            "events.list", {"filters": {"request_id": outbound["request_id"]}}
        )
        assert events["items"]
        cancelled = adapter.call(
            "requests.cancel",
            {
                "client_operation_id": "adapter-cancel-outbound",
                "request_id": outbound["request_id"],
                "expected_revision": outbound_read["revision"],
                "reason": "exercise canonical cancel route",
            },
        )
        assert cancelled["work_state"] == "cancelled"

        inbound_response = private.post(
            "/v1/requests",
            headers=_headers(tokens["alice"]),
            json={
                "client_operation_id": "adapter-seed-inbound",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "bob",
                "packet": _packet("request", "Request for the public adapter"),
            },
        )
        assert inbound_response.status_code == 200
        inbound = inbound_response.json()
        runtime.worker.dispatch_once()
        inbound_read = adapter.call("requests.read", {"request_id": inbound["request_id"]})
        started = adapter.call(
            "requests.report",
            {
                "client_operation_id": "adapter-report-inbound",
                "request_id": inbound["request_id"],
                "expected_revision": inbound_read["revision"],
                "action": "start",
            },
        )
        result = adapter.call(
            "requests.reply",
            {
                "client_operation_id": "adapter-reply-inbound",
                "request_id": inbound["request_id"],
                "expected_revision": started["revision"],
                "kind": "result",
                "packet": _packet("result", "Adapter result"),
            },
        )
        assert result["operation_state"] == "queued"
        assert runtime.worker.dispatch_once().work_state == "result_returned"

        # D19: the doer never checks its own result. The route exists, so this is
        # the service's refusal, not a missing route's bare NOT_FOUND.
        own_result = adapter.call("requests.read", {"request_id": inbound["request_id"]})
        with pytest.raises(HubClientError) as own_check:
            adapter.call(
                "requests.check",
                {
                    "client_operation_id": "adapter-check-own-result",
                    "request_id": inbound["request_id"],
                    "expected_revision": own_result["revision"],
                    "outcome": "accepted",
                    "evidence": "I returned it myself",
                },
            )
        assert own_check.value.code == "ACCESS_DENIED"

        to_check = adapter.call(
            "requests.create",
            {
                "client_operation_id": "adapter-create-to-check",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "alice",
                "packet": _packet("request", "A result for the public adapter to check"),
            },
        )
        runtime.worker.dispatch_once()
        alice_read = private.get(
            f"/v1/requests/{to_check['request_id']}", headers=_headers(tokens["alice"])
        ).json()
        alice_result = private.post(
            f"/v1/requests/{to_check['request_id']}/replies",
            headers=_headers(tokens["alice"]),
            json={
                "client_operation_id": "private-result-to-check",
                "expected_revision": alice_read["revision"],
                "kind": "result",
                "packet": _packet("result", "Alice's result"),
            },
        )
        assert alice_result.status_code == 200, alice_result.text
        assert runtime.worker.dispatch_once().work_state == "result_returned"
        returned = adapter.call("requests.read", {"request_id": to_check["request_id"]})
        checked = adapter.call(
            "requests.check",
            {
                "client_operation_id": "adapter-check-alice-result",
                "request_id": to_check["request_id"],
                "expected_revision": returned["revision"],
                "outcome": "accepted",
                "evidence": "Read alice's result packet; it answers the request",
            },
        )
        assert checked["work_state"] == "result_returned"
        assert checked["revision"] == returned["revision"] + 1
        assert checked["actor_id"] == "bob"

        with pytest.raises(HubClientError) as conflict:
            adapter.call(
                "requests.create",
                {
                    "client_operation_id": "adapter-create-outbound",
                    "mode": "action_requested",
                    "project": "demo",
                    "recipient": "alice",
                    "packet": _packet("request", "Changed input under the same key"),
                },
            )
        assert conflict.value.code == "OPERATION_INPUT_CONFLICT"
        assert conflict.value.retryable is False


def test_double_check_read_is_passive_and_identical_create_never_reassigns(
    tmp_path: Path,
) -> None:
    config, tokens = _write_runtime_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    create_body = {
        "client_operation_id": "double-check-create-1",
        "mode": "action_requested",
        "project": "demo",
        "recipient": "bob",
        "packet": _packet("request", "Read twice without starting or duplicating work."),
    }

    with TestClient(runtime.app) as client:
        created = client.post(
            "/v1/requests", headers=_headers(tokens["alice"]), json=create_body
        )
        assert created.status_code == 200
        original = created.json()
        runtime.worker.dispatch_once()

        first = client.get(
            f"/v1/requests/{original['request_id']}", headers=_headers(tokens["bob"])
        )
        second = client.get(
            f"/v1/requests/{original['request_id']}", headers=_headers(tokens["bob"])
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["work_state"] == second.json()["work_state"] == "requested"
        assert first.json()["revision"] == second.json()["revision"]

        operation_count = runtime.store.fetchone(
            "SELECT COUNT(*) AS count FROM operations WHERE request_id=?",
            (original["request_id"],),
        )["count"]
        duplicate = client.post(
            "/v1/requests", headers=_headers(tokens["alice"]), json=create_body
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["request_id"] == original["request_id"]
        assert duplicate.json()["operation_id"] == original["operation_id"]
        assert runtime.store.fetchone(
            "SELECT COUNT(*) AS count FROM operations WHERE request_id=?",
            (original["request_id"],),
        )["count"] == operation_count
