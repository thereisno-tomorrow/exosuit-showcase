from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from gremlin_town.api.public import PUBLIC_OPERATION_ROUTES, create_public_app
from gremlin_town.contracts import (
    Actor,
    CheckOutcome,
    ErrorCode,
    OperationState,
    Page,
    Receipt,
    ReceiptError,
    WorkState,
)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class PublicFakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Actor]] = []
        self.checked = None

    def participants_list(self, actor, project=None):
        self.calls.append(("participants_list", actor))
        return Page(items=({"participant_id": "grok"},), as_of=NOW)

    def requests_create(self, actor, command):
        self.calls.append(("requests_create", actor))
        return Receipt(
            "op-conflict",
            "req-hidden",
            NOW,
            None,
            None,
            17,
            actor.actor_id,
            error=ReceiptError(ErrorCode.OPERATION_INPUT_CONFLICT, "same key has different input"),
        )

    def requests_list(self, actor, filters, cursor=None):
        self.calls.append(("requests_list", actor))
        return Page(items=(), as_of=NOW)

    def requests_read(self, actor, request_id, message_cursor=None):
        self.calls.append(("requests_read", actor))
        raise AssertionError("route table/auth tests should not call requests_read")

    def requests_report(self, actor, command):
        self.calls.append(("requests_report", actor))
        raise AssertionError("route table/auth tests should not call requests_report")

    def requests_reply(self, actor, command):
        self.calls.append(("requests_reply", actor))
        raise AssertionError("route table/auth tests should not call requests_reply")

    def requests_cancel(self, actor, command):
        self.calls.append(("requests_cancel", actor))
        raise AssertionError("route table/auth tests should not call requests_cancel")

    def requests_check(self, actor, command):
        self.calls.append(("requests_check", actor))
        self.checked = command
        return Receipt("op-check", command.request_id, NOW, None, WorkState.RESULT_RETURNED, 8, actor.actor_id)

    def recovery_dispose(self, actor, command):
        raise AssertionError("recovery must not be registered")

    def operations_get(self, actor, operation_id):
        self.calls.append(("operations_get", actor))
        return Receipt(
            "op-recovery",
            "req-hidden",
            NOW,
            OperationState.UNCERTAIN,
            None,
            18,
            actor.actor_id,
            error=ReceiptError(ErrorCode.RECOVERY_REQUIRED, "operator disposition is required"),
        )

    def events_list(self, actor, filters, cursor=None):
        self.calls.append(("events_list", actor))
        return Page(items=(), as_of=NOW)


def route_pairs(app) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
    }


def test_public_app_exposes_exactly_the_ten_canonical_operations() -> None:
    app = create_public_app(PublicFakeService(), lambda token: Actor("grok") if token == "good" else None, "grok")
    assert PUBLIC_OPERATION_ROUTES == {
        ("GET", "/v1/participants"),
        ("POST", "/v1/requests"),
        ("GET", "/v1/requests"),
        ("GET", "/v1/requests/{request_id}"),
        ("POST", "/v1/requests/{request_id}/reports"),
        ("POST", "/v1/requests/{request_id}/replies"),
        ("POST", "/v1/requests/{request_id}/cancel"),
        ("POST", "/v1/requests/{request_id}/check"),
        ("GET", "/v1/operations/{operation_id}"),
        ("GET", "/v1/events"),
    }
    assert route_pairs(app) == set(PUBLIC_OPERATION_ROUTES)
    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None


def test_public_check_reaches_the_service_as_the_allowed_actor() -> None:
    # Fleet Boss reaches the hub only through this listener, so without this
    # route it could not check a result (D19, ROA-13).
    service = PublicFakeService()
    app = create_public_app(service, lambda token: Actor("grok") if token == "good" else None, "grok")
    response = TestClient(app).post(
        "/v1/requests/req-1/check",
        headers={"Authorization": "Bearer good"},
        json={
            "client_operation_id": "chk-1",
            "expected_revision": 7,
            "outcome": "accepted",
            "evidence": "commit abc123 is on main and tests pass",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["operation_id"] == "op-check"
    assert service.calls == [("requests_check", Actor("grok"))]
    assert service.checked.request_id == "req-1"
    assert service.checked.outcome is CheckOutcome.ACCEPTED
    assert service.checked.evidence == "commit abc123 is on main and tests pass"


def test_public_app_denies_excluded_routes_and_never_registers_recovery() -> None:
    app = create_public_app(PublicFakeService(), lambda token: Actor("grok") if token == "good" else None, "grok")
    client = TestClient(app)
    headers = {"Authorization": "Bearer good"}
    for path in (
        "/",
        "/login",
        "/logout",
        "/overview",
        "/attention",
        "/provenance",
        "/api/docs",
        "/openapi.json",
        "/health/live",
        "/health/ready",
        "/v1/requests/req-1/recovery-disposition",
    ):
        assert client.get(path, headers=headers).status_code == 404
        assert client.post(path, headers=headers, json={}).status_code == 404


def test_public_auth_requires_allowed_live_credential_and_ignores_sender_text() -> None:
    service = PublicFakeService()
    tokens = {"good": Actor("grok"), "other": Actor("other")}
    app = create_public_app(service, tokens.get, "grok")
    client = TestClient(app)

    for headers in ({}, {"Authorization": "Bearer random"}, {"Authorization": "Bearer other"}):
        response = client.get("/v1/participants", headers=headers)
        assert response.status_code == 401
        assert service.calls == []

    allowed = client.get("/v1/participants", headers={"Authorization": "Bearer good"})
    assert allowed.status_code == 200
    assert service.calls[-1] == ("participants_list", Actor("grok"))

    tokens.pop("good")
    revoked = client.get("/v1/participants", headers={"Authorization": "Bearer good"})
    assert revoked.status_code == 401


def test_public_allowed_routes_preserve_structured_conflict_and_recovery_errors() -> None:
    app = create_public_app(PublicFakeService(), lambda token: Actor("grok") if token == "good" else None, "grok")
    client = TestClient(app)
    headers = {"Authorization": "Bearer good"}
    create_payload = {
        "client_operation_id": "op-1",
        "mode": "action_requested",
        "project": "demo",
        "recipient": "recipient-from-route",
        "sender": "mallory-must-not-establish-identity",
        "packet": {
            "schema_version": 1,
            "kind": "request",
            "objective": "test",
            "context": "",
            "constraints": {},
            "sources": [],
            "next_action": "",
            "expected_reply": "",
            "sensitivity": "public",
        },
    }
    conflict = client.post("/v1/requests", headers=headers, json=create_payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "OPERATION_INPUT_CONFLICT"
    assert "req-hidden" not in conflict.text and "17" not in conflict.text

    recovery = client.get("/v1/operations/op-recovery", headers=headers)
    assert recovery.status_code == 409
    assert recovery.json()["error"]["code"] == "RECOVERY_REQUIRED"
