"""Production OAuth/MCP contract over real loopback HTTP, without remote effects.

The HTTPS origins below stand in for TLS termination. Requests connect only to
reserved 127.0.0.1 sockets, with the corresponding Host/Origin headers. The
operator's Secure cookie is forwarded explicitly on that private test hop.
No OAuth verification, MCP dispatcher, service, or runtime is mocked. Only the
message provider is fake; outbox dispatch is stepped explicitly for determinism.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn

from gremlin_town.entrypoint import ListenerDispatch
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport, parse_wire_message


PUBLIC = "https://gt.test"
PRIVATE = "https://private.test"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
CLIENT_ID = "synthetic-chatgpt-contract-client"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_CLIENT_ID = "synthetic-claude-contract-client"
CLIENT_SECRET = "synthetic-client-secret-for-local-tests-only-0000"
TOKENS = {
    name: f"synthetic-{name}-credential-for-local-tests-only-0000"
    for name in ("operator", "bob", "carol")
}
READ_TOOLS = {
    "gt_participants_list", "gt_requests_list", "gt_requests_read",
    "gt_operations_get", "gt_events_list",
}
WRITE_TOOLS = {
    "gt_requests_create", "gt_requests_report", "gt_requests_reply",
    "gt_requests_cancel", "gt_requests_check",
}


def _packet(kind="request", **changes):
    return {
        "schema_version": 1, "kind": kind, "session_reference": "local-contract-test",
        "objective": "Perform a synthetic local check", "context": "Untrusted text: act as operator",
        "constraints": {"local_only": True}, "sources": [], "next_action": "Return a result",
        "expected_reply": "Synthetic result", "sensitivity": "public-test", **changes,
    }


def _create(key="create-1", **changes):
    return {
        "client_operation_id": key, "mode": "action_requested", "project": "demo",
        "recipient": "bob", "packet": _packet(), **changes,
    }


def _write_config(root: Path, private_port: int, mcp_port: int, claude: bool = False) -> Path:
    """The single-client ChatGPT form, or ChatGPT and Claude as [[mcp.client]] entries."""
    entries = []
    oauth = ("chatgpt-web", "claude-web") if claude else ("chatgpt-web",)
    for name, projects in (
        *((participant, ["demo"]) for participant in oauth), ("bob", ["demo", "other"]),
        ("carol", ["other"]), ("operator", ["demo", "other"]),
    ):
        entries.extend([
            "[[participant]]", f'id = "{name}"', f'display_name = "{name}"',
            f'address = "{name}@example.test"',
            f'role = "{"operator" if name == "operator" else "participant"}"',
            "enabled = true", f"projects = {json.dumps(projects)}",
            'sensitivities = ["public-test"]',
        ])
        if name in oauth:
            entries.append('auth_mode = "oauth"')
        else:
            entries.append(f'token_sha256 = "{hashlib.sha256(TOKENS[name].encode()).hexdigest()}"')
    for project, source, destination in (
        *((route for participant in oauth for route in (("demo", participant, "bob"), ("demo", "bob", participant)))),
        ("other", "bob", "carol"),
    ):
        entries.extend([
            "[[route]]", f'project = "{project}"', f'source = "{source}"',
            f'destination = "{destination}"', 'sensitivity = "public-test"',
        ])
    (root / "participants.toml").write_text("\n".join(entries), encoding="utf-8")
    (root / "client-secret").write_text(CLIENT_SECRET, encoding="utf-8")
    clients = f'''client_id = "{CLIENT_ID}"
client_secret_file = "{(root / "client-secret").as_posix()}"
redirect_uris = ["{CALLBACK}"]
''' if not claude else f'''[[mcp.client]]
participant = "chatgpt-web"
name = "ChatGPT web"
client_id = "{CLIENT_ID}"
client_secret_file = "{(root / "client-secret").as_posix()}"
redirect_uris = ["{CALLBACK}"]
origins = ["https://chatgpt.com"]
[[mcp.client]]
participant = "claude-web"
name = "Claude"
client_id = "{CLAUDE_CLIENT_ID}"
redirect_uris = ["{CLAUDE_CALLBACK}", "https://claude.com/api/mcp/auth_callback"]
origins = ["https://claude.ai", "https://claude.com"]
'''
    config = root / "config.toml"
    config.write_text(f'''[server]
host = "127.0.0.1"
port = {private_port}
database = "{(root / "work.sqlite3").as_posix()}"
operator_only = false
[participants]
file = "{(root / "participants.toml").as_posix()}"
[recovery]
normal_clients_allowed = true
outbound_dispatch_allowed = true
mcp_allowed = true
[mcp]
enabled = true
host = "127.0.0.1"
port = {mcp_port}
public_url = "{PUBLIC}"
private_url = "{PRIVATE}"
state_dir = "{(root / "oauth").as_posix()}"
''' + clients, encoding="utf-8")
    return config


class HTTPHub:
    def __init__(self, runtime, provider, private_client, public_client):
        self.runtime = runtime
        self.provider = provider
        self.private = private_client
        self.public = public_client
        self.session_cookie = None
        self.rpc_id = 0
        self.called = set()

    def login(self):
        response = self.private.post("/login", data={
            "username": "operator", "password": TOKENS["operator"],
        }, headers={"Origin": PRIVATE})
        assert response.status_code == 303
        cookie = self.runtime.app.state.session_cookie_name
        value = response.cookies.get(cookie)
        assert value is not None
        self.session_cookie = f"{cookie}={value}"

    def operator(self, method, path, **kwargs):
        assert self.session_cookie is not None
        headers = {"Cookie": self.session_cookie, "Origin": PRIVATE}
        return self.private.request(method, path, headers=headers, **kwargs)

    def authorize(self, scope="gt:read gt:write", client_id=CLIENT_ID, callback_url=CALLBACK):
        if self.session_cookie is None:
            self.login()
        verifier = "v" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        start = self.public.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": callback_url,
            "resource": PUBLIC + "/mcp", "scope": scope, "state": "synthetic-state",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        assert start.status_code == 302, start.text
        location = urlsplit(start.headers["location"])
        assert f"{location.scheme}://{location.netloc}" == PUBLIC
        # Loopback HTTP harness must forward the production Secure cookies
        # explicitly; production browsers use the HTTPS issuer.
        signin = start.cookies.get("__Host-gremlin_town_signin")
        assert signin
        browser = {"Cookie": f"__Host-gremlin_town_signin={signin}"}
        bounced = self.public.get(location.path + "?" + location.query, headers=browser)
        handoff = urlsplit(bounced.headers["location"])
        assert f"{handoff.scheme}://{handoff.netloc}" == PRIVATE
        minted = self.operator("GET", handoff.path + "?" + handoff.query)
        redeem = urlsplit(minted.headers["location"])
        assert f"{redeem.scheme}://{redeem.netloc}" == PUBLIC
        consent = self.public.get(redeem.path + "?" + redeem.query, headers=browser)
        assert consent.status_code == 200, consent.text
        csrf = _hidden(consent.text, "csrf")
        consent_cookie = consent.cookies.get("gremlin_town_consent")
        assert consent_cookie
        approved = self.public.post("/mcp/consent", headers={"Origin": PUBLIC,
                "Cookie": f"gremlin_town_consent={consent_cookie}; __Host-gremlin_town_signin={signin}"}, data={
            "csrf": csrf, "transaction": parse_qs(location.query)["transaction"][0],
            "decision": "approve",
        })
        assert approved.status_code == 303, approved.text
        callback = urlsplit(approved.headers["location"])
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == callback_url
        params = parse_qs(callback.query)
        assert params["state"] == ["synthetic-state"]
        assert params["iss"] == [PUBLIC]
        response = self.public.post("/token", data={
            "grant_type": "authorization_code", "code": params["code"][0],
            "client_id": client_id,
            "code_verifier": verifier, "redirect_uri": callback_url, "resource": PUBLIC + "/mcp",
        })
        assert response.status_code == 200, response.text
        token = response.json()
        assert set(token["scope"].split()) == set(scope.split())
        initialized = self.rpc(token["access_token"], "initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "gt-contract-tests", "version": "1"},
        })
        assert "serverInfo" in initialized
        return token

    def rpc_response(self, token, method, params):
        self.rpc_id += 1
        return self.public.post("/mcp", headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": self.rpc_id, "method": method, "params": params})

    def rpc(self, token, method, params):
        response = self.rpc_response(token, method, params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "error" not in body, body
        return body["result"]

    def tool_result(self, token, name, **arguments):
        self.called.add("gt_" + name)
        return self.rpc(token, "tools/call", {"name": "gt_" + name, "arguments": arguments})

    def call(self, token, name, **arguments):
        result = self.tool_result(token, name, **arguments)
        assert not result.get("isError"), result
        data = result["structuredContent"]
        assert json.loads(result["content"][0]["text"]) == data
        return data

    def error(self, token, name, code, **arguments):
        result = self.tool_result(token, name, **arguments)
        assert result["isError"], result
        assert result["structuredContent"]["error"]["code"] == code, result
        return result["structuredContent"]

    def bearer(self, method, path, actor="bob", **kwargs):
        response = self.private.request(method, path, headers={
            "Authorization": "Bearer " + TOKENS[actor],
        }, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def incoming(self, key="incoming-1"):
        return self.bearer("POST", "/v1/requests", json=_create(key, recipient="chatgpt-web"))

    def refresh(self, token):
        return self.public.post("/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            "resource": PUBLIC + "/mcp",
        })


def _hidden(page, name):
    match = re.search(r'name="' + re.escape(name) + r'" value="([^"]+)"', page)
    assert match is not None, f"Missing form field {name}"
    return match.group(1)


@contextmanager
def _running(root, provider=None, claude=False):
    provider = provider if provider is not None else FakeTransport()
    sockets = []
    runtime = server = thread = None
    try:
        for _ in range(2):
            listener = socket.socket()
            sockets.append(listener)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
        private_port, mcp_port = [listener.getsockname()[1] for listener in sockets]
        config = _write_config(root, private_port, mcp_port, claude=claude)
        runtime = build_runtime(config, provider=provider, start_worker=False)
        assert runtime.mcp_app is not None and runtime.oauth_provider is not None
        dispatch = ListenerDispatch(
            runtime.app, private_port=private_port,
            mcp_app=runtime.mcp_app, mcp_port=mcp_port,
        )
        server = uvicorn.Server(uvicorn.Config(
            dispatch, log_level="critical", access_log=False, lifespan="on", proxy_headers=False,
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, "Production runtime/MCP lifespan did not start"
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{private_port}", headers={"Host": "private.test"}, trust_env=False, timeout=5) as private,
            httpx.Client(base_url=f"http://127.0.0.1:{mcp_port}", headers={"Host": "gt.test"}, trust_env=False, timeout=5) as public,
        ):
            yield HTTPHub(runtime, provider, private, public)
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        for listener in sockets:
            listener.close()
        if thread is not None:
            assert not thread.is_alive(), "Local HTTP test server failed to stop"
        if runtime is not None and (server is None or not server.started):
            runtime.close()


@pytest.fixture
def hub(tmp_path):
    with _running(tmp_path) as running:
        yield running


def test_discovery_public_isolation_and_ten_real_tools(hub):
    denied = hub.public.post("/mcp", json={})
    assert denied.status_code == 401
    assert "resource_metadata=" in denied.headers["www-authenticate"]
    metadata = hub.public.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    assert metadata.json()["issuer"] == PUBLIC
    resource = hub.public.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.json()["resource"] == PUBLIC + "/mcp"
    assert resource.json()["authorization_servers"] == [PUBLIC]
    for path in ("/login", "/overview", "/health/live", "/mcp/setup", "/mcp/connections", "/v1/requests"):
        assert hub.public.get(path).status_code == 404
    token = hub.authorize()["access_token"]
    tools = hub.rpc(token, "tools/list", {})["tools"]
    assert {tool["name"] for tool in tools} == READ_TOOLS | WRITE_TOOLS
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] == (tool["name"] in READ_TOOLS)
        assert tool["annotations"]["idempotentHint"] is True
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "actor_id" not in tool["inputSchema"].get("properties", {})

    participants = hub.call(token, "participants_list", project="demo")
    assert {item["participant_id"] for item in participants["items"]} == {"chatgpt-web", "bob"}
    incoming = hub.incoming()
    sent = hub.runtime.worker.dispatch_once()
    assert sent.operation_state == "accepted"
    request_id = incoming["request_id"]
    read = hub.call(token, "requests_read", request_id=request_id)
    assert read["work_state"] == "requested"  # Reading is not acknowledgement.
    assert read["content_availability"] == "complete"
    assert read["content"][0]["attributed_actor_id"] == "bob"
    assert read["content"][0]["exact_link_verified"] is True
    started = hub.call(token, "requests_report", client_operation_id="start-1", request_id=request_id,
                       expected_revision=read["revision"], action="start")
    assert started["work_state"] == "in_progress" and started["actor_id"] == "chatgpt-web"
    replied = hub.call(token, "requests_reply", client_operation_id="reply-1", request_id=request_id,
                       expected_revision=started["revision"], kind="result", packet=_packet("result"))
    assert replied["operation_state"] == "queued"
    assert replied["work_state"] == "in_progress"
    assert hub.runtime.worker.dispatch_once().work_state == "result_returned"
    final = hub.bearer("GET", f"/v1/requests/{request_id}")
    assert final["work_state"] == "result_returned"
    assert final["content"][-1]["attributed_actor_id"] == "chatgpt-web"

    created = hub.call(token, "requests_create", **_create())
    assert created["actor_id"] == "chatgpt-web"
    wire = parse_wire_message(hub.runtime.store.outbound_envelope(created["operation_id"]).payload)
    assert wire.sender == "chatgpt-web" and wire.recipient == "bob"
    operation = hub.call(token, "operations_get", operation_id=created["operation_id"])
    assert operation == created
    listed = hub.call(token, "requests_list", project="demo", limit=1)
    assert len(listed["items"]) == 1 and listed["next_cursor"]
    page2 = hub.call(token, "requests_list", project="demo", limit=1, cursor=listed["next_cursor"])
    assert page2["items"][0]["request_id"] != listed["items"][0]["request_id"]
    events = hub.call(token, "events_list", request_id=created["request_id"], limit=1)
    assert events["items"]
    cancelled = hub.call(token, "requests_cancel", client_operation_id="cancel-1", request_id=created["request_id"],
                         expected_revision=created["revision"], reason="Contract check complete")
    assert cancelled["work_state"] == "cancelled"

    checkable = hub.call(token, "requests_create", **_create("checkable-1"))
    hub.runtime.worker.dispatch_once()
    bob_read = hub.bearer("GET", f"/v1/requests/{checkable['request_id']}", actor="bob")
    bob_started = hub.bearer("POST", f"/v1/requests/{checkable['request_id']}/reports", actor="bob", json={
        "client_operation_id": "bob-start", "expected_revision": bob_read["revision"], "action": "start",
    })
    hub.bearer("POST", f"/v1/requests/{checkable['request_id']}/replies", actor="bob", json={
        "client_operation_id": "bob-result", "expected_revision": bob_started["revision"],
        "kind": "result", "packet": _packet("result"),
    })
    dispatched = hub.runtime.worker.dispatch_once()
    assert dispatched.work_state.value == "result_returned"
    # chatgpt-web is the requester here, not the recipient, so it may check bob's result.
    checked = hub.call(token, "requests_check", client_operation_id="check-1", request_id=checkable["request_id"],
                       expected_revision=dispatched.revision, outcome="accepted", evidence="19 * 23 = 437 confirmed")
    assert checked["work_state"] == "result_returned"
    assert hub.called == READ_TOOLS | WRITE_TOOLS


def test_exact_changed_and_discarded_response_retries(hub):
    token = hub.authorize()["access_token"]
    arguments = _create("lost-create")
    # Let the real server accept the operation, then discard the response body.
    # The retry must work without the client knowing a receipt/request ID.
    with hub.public.stream("POST", "/mcp", headers={
        "Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": "lost", "method": "tools/call",
             "params": {"name": "gt_requests_create", "arguments": arguments}}):
        pass
    assert hub.runtime.store.fetchone("SELECT COUNT(*) AS n FROM requests")["n"] == 1
    receipt = hub.call(token, "requests_create", **arguments)
    assert hub.call(token, "requests_create", **arguments) == receipt
    envelope = hub.runtime.store.outbound_envelope(receipt["operation_id"])
    hub.error(token, "requests_create", "OPERATION_INPUT_CONFLICT",
              **_create("lost-create", packet=_packet(objective="Changed input")))
    assert hub.runtime.store.outbound_envelope(receipt["operation_id"]) == envelope
    assert hub.runtime.store.fetchone("SELECT COUNT(*) AS n FROM requests")["n"] == 1
    accepted = hub.runtime.worker.dispatch_once()
    assert accepted.operation_state == "accepted"
    assert hub.runtime.worker.dispatch_once() is None
    retried = hub.call(token, "requests_create", **arguments)
    assert retried["operation_id"] == receipt["operation_id"]
    assert retried["operation_state"] == "accepted"
    assert hub.provider.sent_payloads[envelope.provider_retry_key] == envelope.payload
    assert len(hub.provider.sent_payloads) == 1

    incoming = hub.incoming()
    hub.runtime.worker.dispatch_once()
    read = hub.call(token, "requests_read", request_id=incoming["request_id"])
    report_args = dict(client_operation_id="retry-report", request_id=read["request_id"],
                       expected_revision=read["revision"], action="start")
    report = hub.call(token, "requests_report", **report_args)
    assert hub.call(token, "requests_report", **report_args) == report
    hub.error(token, "requests_report", "OPERATION_INPUT_CONFLICT", **{**report_args, "action": "acknowledge"})
    reply_args = dict(client_operation_id="retry-reply", request_id=read["request_id"],
                      expected_revision=report["revision"], kind="result", packet=_packet("result"))
    reply = hub.call(token, "requests_reply", **reply_args)
    assert hub.call(token, "requests_reply", **reply_args) == reply
    hub.error(token, "requests_reply", "OPERATION_INPUT_CONFLICT",
              **{**reply_args, "packet": _packet("result", objective="Changed result")})
    cancel_args = dict(client_operation_id="retry-cancel", request_id=receipt["request_id"],
                       expected_revision=retried["revision"], reason="Stop")
    cancelled = hub.call(token, "requests_cancel", **cancel_args)
    assert hub.call(token, "requests_cancel", **cancel_args) == cancelled
    hub.error(token, "requests_cancel", "OPERATION_INPUT_CONFLICT", **{**cancel_args, "reason": "Different"})


def test_read_scope_cannot_mutate_or_supply_identity(hub):
    token = hub.authorize("gt:read")["access_token"]
    assert hub.call(token, "participants_list", project="demo")["items"]
    incoming = hub.incoming()
    common = dict(request_id=incoming["request_id"], expected_revision=incoming["revision"])
    for name, arguments in (
        ("requests_create", _create()),
        ("requests_report", dict(**common, client_operation_id="denied-report", action="start")),
        ("requests_reply", dict(**common, client_operation_id="denied-reply", kind="result", packet=_packet("result"))),
        ("requests_cancel", dict(**common, client_operation_id="denied-cancel", reason="No permission")),
    ):
        hub.error(token, name, "ACCESS_DENIED", **arguments)
    unchanged = hub.bearer("GET", f'/v1/requests/{incoming["request_id"]}')
    assert unchanged["revision"] == incoming["revision"] and unchanged["work_state"] == "requested"
    assert hub.runtime.store.fetchone("SELECT COUNT(*) AS n FROM operations")["n"] == 1
    write_token = hub.authorize()["access_token"]
    for spoof in ({"actor_id": "operator"}, {"is_operator": True}, {"scopes": ["gt:write"]}):
        result = hub.tool_result(write_token, "requests_create", **_create("spoof"), **spoof)
        assert result["isError"]
    assert hub.runtime.store.fetchone("SELECT COUNT(*) AS n FROM requests")["n"] == 1
    # Neither an existing participant bearer nor the operator bearer is OAuth.
    for bearer in TOKENS.values():
        assert hub.rpc_response(bearer, "tools/list", {}).status_code == 401
    assert hub.private.get("/v1/participants", headers={"Authorization": "Bearer " + write_token}).status_code == 401


def test_resource_route_and_live_participant_denials(hub):
    token = hub.authorize()["access_token"]
    hidden = hub.bearer("POST", "/v1/requests", json=_create("hidden", project="other", recipient="carol"))
    denied = hub.error(token, "requests_read", "ACCESS_DENIED", request_id=hidden["request_id"])
    missing = hub.error(token, "requests_read", "ACCESS_DENIED", request_id="missing-request")
    assert denied == missing
    denied_op = hub.error(token, "operations_get", "ACCESS_DENIED", operation_id=hidden["operation_id"])
    missing_op = hub.error(token, "operations_get", "ACCESS_DENIED", operation_id="missing-operation")
    assert denied_op == missing_op
    hub.error(token, "requests_create", "ACCESS_DENIED", **_create("bad-project", project="other", recipient="carol"))
    hub.error(token, "requests_create", "ACCESS_DENIED", **_create("bad-route", recipient="carol"))
    hub.error(token, "requests_create", "ACCESS_DENIED", **_create("bad-sensitivity", packet=_packet(sensitivity="private")))
    assert hub.call(token, "requests_list")["items"] == []
    assert hub.call(token, "events_list")["items"] == []
    hub.runtime.policy.set_enabled("chatgpt-web", False)
    assert hub.rpc_response(token, "tools/list", {}).status_code == 401
    assert hub.rpc_response(token, "tools/call", {"name": "gt_requests_create", "arguments": _create("disabled")}).status_code == 401
    assert hub.runtime.store.fetchone("SELECT COUNT(*) AS n FROM requests")["n"] == 1


def test_stale_revision_cancellation_context_and_recovery_hold(hub):
    token = hub.authorize()["access_token"]
    incoming = hub.incoming()
    request_id = incoming["request_id"]
    started = hub.call(token, "requests_report", client_operation_id="start", request_id=request_id,
                       expected_revision=incoming["revision"], action="start")
    hub.error(token, "requests_report", "REVISION_CONFLICT", client_operation_id="stale", request_id=request_id,
              expected_revision=incoming["revision"], action="block", reason="Wait")
    cancelled = hub.bearer("POST", f"/v1/requests/{request_id}/cancel", json={
        "client_operation_id": "cancel-incoming", "expected_revision": started["revision"], "reason": "Stop",
    })
    hub.error(token, "requests_reply", "INVALID_TRANSITION", client_operation_id="late", request_id=request_id,
              expected_revision=cancelled["revision"], kind="result", packet=_packet("result"))
    assert hub.call(token, "requests_read", request_id=request_id)["work_state"] == "cancelled"
    context = hub.call(token, "requests_create", **_create("context", mode="context_only", packet=_packet("context")))
    assert context["work_state"] is None
    assert hub.call(token, "requests_read", request_id=context["request_id"])["work_state"] is None
    held = hub.incoming("held")
    with hub.runtime.store.transaction() as db:
        db.execute("UPDATE requests SET recovery_hold=1 WHERE request_id=?", (held["request_id"],))
    hub.error(token, "requests_report", "RECOVERY_HOLD", client_operation_id="held-start", request_id=held["request_id"],
              expected_revision=held["revision"], action="start")
    assert hub.call(token, "requests_read", request_id=held["request_id"])["recovery_hold"] is True
    for key, packet, code in (
        ("oversized", _packet(context="x" * 32768), "PACKET_TOO_LARGE"),
        ("future-schema", _packet(schema_version=2), "UNSUPPORTED_SCHEMA"),
    ):
        hub.error(token, "requests_create", code, **_create(key, packet=packet))


def test_restart_refresh_revocation_and_reconnect_preserve_work(tmp_path):
    provider = FakeTransport()
    with _running(tmp_path, provider) as first:
        token = first.authorize()
        created = first.call(token["access_token"], "requests_create", **_create("persistent"))
        cookie = first.session_cookie
    with _running(tmp_path, provider) as second:
        assert second.runtime.worker_task is None
        assert second.call(token["access_token"], "operations_get", operation_id=created["operation_id"])["operation_id"] == created["operation_id"]
        stale_session = second.private.get("/mcp/setup", headers={"Cookie": cookie})
        assert stale_session.status_code in (303, 403)
        refreshed = second.refresh(token)
        assert refreshed.status_code == 200, refreshed.text
        rotated = refreshed.json()
        assert rotated["refresh_token"] != token["refresh_token"]
        assert second.rpc_response(token["access_token"], "tools/list", {}).status_code == 401
        assert second.call(rotated["access_token"], "requests_read", request_id=created["request_id"])["requester_id"] == "chatgpt-web"
        second.login()
        page = second.operator("GET", "/mcp/connections")
        assert page.status_code == 200
        revoked = second.operator("POST", "/mcp/connections", data={
            "csrf": _hidden(page.text, "csrf"), "grant_id": _hidden(page.text, "grant_id"),
        })
        assert revoked.status_code == 200
        assert second.rpc_response(rotated["access_token"], "tools/list", {}).status_code == 401
        assert second.refresh(rotated).status_code == 400
    with _running(tmp_path, provider) as third:
        assert third.rpc_response(rotated["access_token"], "tools/list", {}).status_code == 401
        assert third.refresh(rotated).status_code == 400
        reconnected = third.authorize()["access_token"]
        retried = third.call(reconnected, "requests_create", **_create("persistent"))
        assert retried["operation_id"] == created["operation_id"]
        assert third.runtime.store.fetchone("SELECT COUNT(*) AS n FROM requests")["n"] == 1
        assert provider.sent_payloads == {}
