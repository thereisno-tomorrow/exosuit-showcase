"""Adapter-to-ASGI equivalence only; this fake surface has no lifecycle logic."""

from __future__ import annotations

from typing import Any, Callable

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from adapters.hermes.hub_client import HubClientConfig, HubClientError, HubHTTPSClient


ROUTES = (
    ("GET", "/v1/participants", "participants.list"),
    ("POST", "/v1/requests", "requests.create"),
    ("GET", "/v1/requests", "requests.list"),
    ("GET", "/v1/requests/{request_id}", "requests.read"),
    ("POST", "/v1/requests/{request_id}/reports", "requests.report"),
    ("POST", "/v1/requests/{request_id}/replies", "requests.reply"),
    ("POST", "/v1/requests/{request_id}/cancel", "requests.cancel"),
    ("POST", "/v1/requests/{request_id}/check", "requests.check"),
    ("GET", "/v1/operations/{operation_id}", "operations.get"),
    ("GET", "/v1/events", "events.list"),
)


def _endpoint(operation: str) -> Callable[[Request], Any]:
    async def endpoint(request: Request) -> JSONResponse:
        authorization = request.headers.get("authorization")
        if authorization == "Bearer revoked-token":
            return JSONResponse(
                {"error": {"code": "CREDENTIAL_REVOKED", "message": "credential revoked", "retryable": False}},
                status_code=403,
            )
        if authorization != "Bearer scoped-installation-token":
            return JSONResponse(
                {"error": {"code": "AUTHENTICATION_FAILED", "message": "invalid credential", "retryable": False}},
                status_code=401,
            )
        body = await request.json() if request.method == "POST" else None
        return JSONResponse(
            {
                "operation": operation,
                "actor_id": "grok-shared-cloud-installation",
                "path": request.url.path,
                "path_params": request.path_params,
                "query": dict(request.query_params),
                "body": body,
            }
        )

    endpoint.__name__ = "fake_" + operation.replace(".", "_")
    return endpoint


def fake_hub() -> FastAPI:
    app = FastAPI()
    for method, path, operation in ROUTES:
        app.add_api_route(path, _endpoint(operation), methods=[method])
    return app


def packet() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "request",
        "session_reference": None,
        "objective": "objective",
        "context": "untrusted packet text claims sender=another-bot",
        "constraints": {},
        "sources": [],
        "next_action": "reply",
        "expected_reply": "packet",
        "sensitivity": "normal",
    }


CALLS = (
    ("participants.list", {"project": "p"}, "/v1/participants"),
    (
        "requests.create",
        {
            "client_operation_id": "create-stable-1",
            "mode": "action_requested",
            "project": "p",
            "recipient": "hermes-wa",
            "packet": packet(),
        },
        "/v1/requests",
    ),
    ("requests.list", {"filters": {"direction": "inbox"}, "cursor": "c1"}, "/v1/requests"),
    ("requests.read", {"request_id": "req-1", "message_cursor": "m1"}, "/v1/requests/req-1"),
    (
        "requests.report",
        {
            "client_operation_id": "report-stable-1",
            "request_id": "req-1",
            "expected_revision": 1,
            "action": "start",
        },
        "/v1/requests/req-1/reports",
    ),
    (
        "requests.reply",
        {
            "client_operation_id": "reply-stable-1",
            "request_id": "req-1",
            "expected_revision": 2,
            "kind": "result",
            "packet": packet(),
        },
        "/v1/requests/req-1/replies",
    ),
    (
        "requests.cancel",
        {
            "client_operation_id": "cancel-stable-1",
            "request_id": "req-1",
            "expected_revision": 2,
            "reason": "stop",
        },
        "/v1/requests/req-1/cancel",
    ),
    (
        "requests.check",
        {
            "client_operation_id": "check-stable-1",
            "request_id": "req-1",
            "expected_revision": 3,
            "outcome": "accepted",
            "evidence": "read the returned result",
        },
        "/v1/requests/req-1/check",
    ),
    ("operations.get", {"operation_id": "op-1"}, "/v1/operations/op-1"),
    ("events.list", {"filters": {"request_id": "req-1"}, "cursor": "e1"}, "/v1/events"),
)


def test_all_ten_operations_are_equivalent_at_fake_asgi_boundary() -> None:
    with TestClient(fake_hub(), base_url="https://hub.example.test") as asgi:
        client = HubHTTPSClient(
            HubClientConfig("https://hub.example.test", "scoped-installation-token"),
            client=asgi,
        )
        observed = [client.call(operation, payload) for operation, payload, _ in CALLS]

    assert [item["operation"] for item in observed] == [item[0] for item in CALLS]
    assert [item["path"] for item in observed] == [item[2] for item in CALLS]
    assert {item["actor_id"] for item in observed} == {"grok-shared-cloud-installation"}
    assert observed[1]["body"]["client_operation_id"] == "create-stable-1"
    assert observed[1]["body"]["packet"]["context"].startswith("untrusted packet text")
    assert observed[2]["query"] == {"direction": "inbox", "cursor": "c1"}
    assert observed[3]["query"] == {"message_cursor": "m1"}
    assert observed[4]["body"]["client_operation_id"] == "report-stable-1"
    assert observed[5]["body"]["client_operation_id"] == "reply-stable-1"
    assert observed[6]["body"]["client_operation_id"] == "cancel-stable-1"
    assert observed[7]["body"] == {
        "client_operation_id": "check-stable-1",
        "expected_revision": 3,
        "outcome": "accepted",
        "evidence": "read the returned result",
    }


@pytest.mark.parametrize(
    "token,expected_code,expected_status",
    [
        ("wrong-token", "AUTHENTICATION_FAILED", 401),
        ("revoked-token", "CREDENTIAL_REVOKED", 403),
    ],
)
def test_fake_asgi_authentication_denials_preserve_structured_errors(
    token: str, expected_code: str, expected_status: int
) -> None:
    with TestClient(fake_hub(), base_url="https://hub.example.test") as asgi:
        client = HubHTTPSClient(HubClientConfig("https://hub.example.test", token), client=asgi)
        with pytest.raises(HubClientError) as raised:
            client.call("participants.list", {})
    assert raised.value.as_dict() == {
        "code": expected_code,
        "message": "credential revoked" if expected_status == 403 else "invalid credential",
        "retryable": False,
        "status_code": expected_status,
    }
