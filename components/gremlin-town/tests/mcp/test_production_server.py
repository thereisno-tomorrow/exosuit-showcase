"""Public production assembly with disposable state and no sender/network calls."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

import pytest
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from gremlin_town.contracts import Actor, ProviderReference
from gremlin_town.domain import Participant, Policy
from gremlin_town.mcp.server import MAX_REQUEST_BODY_SIZE, build_server
from gremlin_town.oauth import OAuthClient, OAuthConfig, OAuthProvider
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.workers import OutboxWorker


PUBLIC = "https://gt.example.test:8443"
NOW = datetime(2026, 9, 21, tzinfo=UTC)
NAMES = {"gt_participants_list", "gt_requests_create", "gt_requests_list", "gt_requests_read",
         "gt_requests_report", "gt_requests_reply", "gt_requests_cancel", "gt_requests_check",
         "gt_operations_get", "gt_events_list"}


class FakeAuth:
    def __init__(self):
        self.current = ContextVar("verified-test-token")
        self.authorizations = 0
        self.deny_call = False

    async def verify_token(self, token):
        if token not in {"alice", "bob", "read-only", "wrong-resource"}:
            return None
        return AccessToken(token=token, client_id="synthetic-client", subject="bob" if token == "bob" else "alice",
                           scopes=["gt:read"] if token == "read-only" else ["gt:read", "gt:write"],
                           resource="https://other.test/mcp" if token == "wrong-resource" else PUBLIC + "/mcp")

    async def authorize(self):
        self.authorizations += 1
        token = get_access_token()
        if self.deny_call or token is None:
            raise PermissionError("expired or revoked")
        self.current.set(token)

    def actor(self):
        return Actor(self.current.get().subject)

    def scopes(self):
        return self.current.get().scopes


@pytest.fixture
def public(tmp_path):
    policy = Policy(participants=tuple(
        Participant(name, name, "unused@example.test", frozenset({"demo"}), frozenset({"public-test"}))
        for name in ("alice", "bob")
    ), routes=frozenset({("demo", "alice", "bob", "public-test"), ("demo", "bob", "alice", "public-test")}))
    store = SQLiteStore(tmp_path / "work.sqlite3", now=lambda: NOW)
    service = GremlinTownService(store, policy, now=lambda: NOW)
    provider = OAuthProvider(OAuthConfig(
        db_path=tmp_path / "oauth.sqlite3", issuer_url=PUBLIC, resource_url=PUBLIC + "/mcp",
        private_base_url="https://private.example.test", clients=(
            OAuthClient(client_id="synthetic-client", participant="chatgpt-web", name="ChatGPT web",
                        redirect_uris=("https://chatgpt.com/connector_platform_oauth_redirect",),
                        origins=("https://chatgpt.com",), client_secret="synthetic-test-secret-" + "s" * 32),
            OAuthClient(client_id="synthetic-claude", participant="claude-web", name="Claude",
                        redirect_uris=("https://claude.ai/api/mcp/auth_callback",), origins=("https://claude.ai",)),
        ),
    ), participant_enabled=lambda _: True)
    auth = FakeAuth()
    bundle = build_server(service, provider, auth)
    # Master owns this lifecycle in production; the public mount must not start
    # a second session manager or its own service/outbox lifecycle.
    @asynccontextmanager
    async def lifespan(app):
        async with bundle.server.session_manager.run():
            yield
    parent = Starlette(routes=[Mount("/", app=bundle.app)], lifespan=lifespan)
    try:
        with TestClient(parent, base_url=PUBLIC) as client:
            yield client, auth, store, bundle, service
    finally:
        store.close()


def rpc(client, method, params=None, token="alice"):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                           headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})
    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    assert response.headers["content-type"].startswith("application/json")
    return response.json()["result"]


def call(client, name, arguments=None, token="alice"):
    return rpc(client, "tools/call", {"name": "gt_" + name, "arguments": arguments or {}}, token)


def packet(kind="request"):
    return {"schema_version": 1, "kind": kind, "objective": "Local fake-state test", "sensitivity": "public-test"}


def test_real_ten_tools_http_metadata_and_service_semantics(public):
    client, auth, store, bundle, service = public
    initialized = rpc(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                             "clientInfo": {"name": "local-test", "version": "1"}})
    assert initialized["serverInfo"]["name"] == "Gremlin Town"
    listed = rpc(client, "tools/list")["tools"]
    assert {tool["name"] for tool in listed} == NAMES
    writes = {
        "gt_requests_create", "gt_requests_report", "gt_requests_reply",
        "gt_requests_cancel", "gt_requests_check",
    }
    for tool in listed:
        required_scopes = ["gt:read", "gt:write"] if tool["name"] in writes else ["gt:read"]
        schemes = [{"type": "oauth2", "scopes": required_scopes}]
        assert tool["securitySchemes"] == schemes
        assert tool["_meta"]["securitySchemes"] == schemes
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "actor" not in tool["inputSchema"]["properties"]
        assert tool["annotations"]["readOnlyHint"] is (tool["name"] not in writes)
        assert tool["annotations"]["destructiveHint"] is (tool["name"] == "gt_requests_cancel")
        if tool["name"] in writes:
            assert "client_operation_id" in tool["inputSchema"]["required"]
    assert not call(client, "participants_list")["isError"]
    args = {"client_operation_id": "create-1", "project": "demo", "recipient": "bob",
            "mode": "action_requested", "packet": packet()}
    created = call(client, "requests_create", args)["structuredContent"]
    assert created["actor_id"] == "alice" and created["operation_state"] == "queued"
    assert call(client, "requests_create", args)["structuredContent"] == created
    conflict = call(client, "requests_create", {**args, "packet": {**packet(), "objective": "Changed"}})
    assert conflict["structuredContent"]["error"]["code"] == "OPERATION_INPUT_CONFLICT"
    request_id = created["request_id"]
    assert len(call(client, "requests_list")["structuredContent"]["items"]) == 1
    detail = call(client, "requests_read", {"request_id": request_id})["structuredContent"]
    assert detail["content_availability"] == "queued_only"
    assert call(client, "operations_get", {"operation_id": created["operation_id"]})["structuredContent"] == created
    assert call(client, "events_list")["structuredContent"]["items"]
    class FakeSender:
        def send(self, envelope):
            return ProviderReference("fake-message", "fake-thread", NOW, envelope.parent_message_id)
    worker = OutboxWorker(store, service.policy, FakeSender(), now=lambda: NOW)
    assert worker.dispatch_once().operation_state.value == "accepted"
    revision = service.requests_read(Actor("alice"), request_id).revision
    report = call(client, "requests_report", {"client_operation_id": "report-1", "request_id": request_id,
                  "expected_revision": revision, "action": "start"}, token="bob")["structuredContent"]
    assert report["work_state"] == "in_progress"
    reply = call(client, "requests_reply", {"client_operation_id": "reply-1", "request_id": request_id,
                 "expected_revision": report["revision"], "kind": "clarification", "packet": packet("clarification")}, token="bob")
    assert not reply["isError"], reply
    assert reply["structuredContent"]["operation_state"] == "queued"
    assert reply["structuredContent"]["work_state"] != "result_returned"
    cancelled = call(client, "requests_cancel", {"client_operation_id": "cancel-1", "request_id": request_id,
                     "expected_revision": reply["structuredContent"]["revision"], "reason": "test complete"})
    assert cancelled["structuredContent"]["work_state"] == "cancelled"
    assert len(store.fetchall("SELECT * FROM requests")) == 1
    assert auth.authorizations == 11
    assert bundle.server.settings.stateless_http and bundle.server.settings.json_response


def test_authentication_scope_and_per_call_denial(public):
    client, auth, store, _, _ = public
    for token in (None, "invalid", "wrong-resource"):
        response = client.post("/mcp", json={}, headers={} if token is None else {"Authorization": "Bearer " + token})
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
    assert auth.authorizations == 0
    assert not call(client, "participants_list", token="read-only")["isError"]
    denied = call(client, "requests_cancel", {"client_operation_id": "denied", "request_id": "missing",
                  "expected_revision": 1, "reason": "stop"}, token="read-only")
    assert denied["isError"] and denied["structuredContent"]["error"]["code"] == "ACCESS_DENIED"
    challenge = denied["_meta"]["mcp/www_authenticate"]
    assert challenge == [
        'Bearer resource_metadata="https://gt.example.test:8443/.well-known/oauth-protected-resource/mcp", '
        'error="insufficient_scope", error_description="Additional authorization is required for this tool.", '
        'scope="gt:write"'
    ]
    auth.deny_call = True
    policy_denied = call(client, "participants_list")
    assert policy_denied["structuredContent"]["error"]["code"] == "ACCESS_DENIED"
    assert "_meta" not in policy_denied
    auth.deny_call = False
    injection = call(client, "participants_list", {"actor": "operator"})
    assert injection["isError"]
    assert not store.fetchall("SELECT * FROM operations")


def test_public_oauth_metadata_and_private_route_isolation(public):
    client, _, _, _, _ = public
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == PUBLIC
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == PUBLIC + "/mcp"
    assert set(resource["scopes_supported"]) == {"gt:read", "gt:write", "bc:read", "bc:write"}
    for path in ("/", "/mcp/setup", "/setup", "/login", "/mcp/connections", "/overview",
                 "/health/live", "/health/ready", "/v1/requests", "/register", "/sse", "/messages/"):
        assert client.get(path, follow_redirects=False).status_code == 404, path
    assert client.get("/mcp/consent", follow_redirects=False).status_code == 400


@pytest.mark.parametrize("path", ["/mcp", "/token", "/.well-known/oauth-authorization-server"])
def test_public_boundary_rejects_wrong_hosts_origins_and_large_bodies(public, path):
    client, _, _, _, _ = public
    for host in ("private.example.test", "gt.example.test:9443", "attacker.test"):
        assert client.get(path, headers={"Host": host}).status_code == 421
    for origin in ("null", "https://attacker.test", "https://private.example.test", PUBLIC + ".attacker.test",
                   "https://claude.com", "http://claude.ai", "https://claude.ai.attacker.test"):
        assert client.get(path, headers={"Origin": origin}).status_code == 403
    assert client.get(path, headers=[("Origin", PUBLIC), ("Origin", "https://chatgpt.com")]).status_code == 403
    assert client.post(path, content=b"x" * (MAX_REQUEST_BODY_SIZE + 1)).status_code == 413
    assert client.post(path, content=iter([b"x" * 40000, b"y" * 40000])).status_code == 413


def test_approved_origin_and_forwarded_headers_cannot_expose_private_routes(public):
    client, _, _, _, _ = public
    # Only the public origin and origins that an enrolled client declares.
    for origin in (PUBLIC, "https://chatgpt.com", "https://claude.ai"):
        response = client.get("/.well-known/oauth-authorization-server", headers={"Origin": origin})
        assert response.status_code == 200
    response = client.get("/mcp/setup", headers={"X-Forwarded-Host": "private.example.test",
                                               "X-Forwarded-Port": "9443"})
    assert response.status_code == 404
