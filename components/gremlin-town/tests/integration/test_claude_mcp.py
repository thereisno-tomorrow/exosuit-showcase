"""Claude and ChatGPT as separate OAuth participants on one hub, over real loopback HTTP.

Reuses the production contract harness from test_chatgpt_mcp: no OAuth
verification, MCP dispatcher, service, or runtime is mocked, and only the
message provider is fake. Claude registers no client secret; it is a public
PKCE client, exactly as Claude's custom-connector form sends it.
"""

from __future__ import annotations

import re
import time

import pytest

from gremlin_town.transport import FakeTransport, parse_wire_message
from tests.integration.test_chatgpt_mcp import (
    CLAUDE_CALLBACK, CLAUDE_CLIENT_ID, READ_TOOLS, WRITE_TOOLS, _create, _packet, _running,
)


@pytest.fixture
def pair(tmp_path):
    with _running(tmp_path, claude=True) as running:
        yield running


def claude_token(hub, scope="gt:read gt:write"):
    return hub.authorize(scope, client_id=CLAUDE_CLIENT_ID, callback_url=CLAUDE_CALLBACK)


def test_claude_connects_without_a_secret_and_works_as_claude_web(pair):
    token = claude_token(pair)["access_token"]
    assert {tool["name"] for tool in pair.rpc(token, "tools/list", {})["tools"]} == READ_TOOLS | WRITE_TOOLS
    participants = pair.call(token, "participants_list", project="demo")
    assert {"claude-web", "bob"} <= {item["participant_id"] for item in participants["items"]}
    incoming = pair.bearer("POST", "/v1/requests", json=_create("to-claude", recipient="claude-web"))
    assert pair.runtime.worker.dispatch_once().operation_state == "accepted"
    read = pair.call(token, "requests_read", request_id=incoming["request_id"])
    assert read["work_state"] == "requested"
    started = pair.call(token, "requests_report", client_operation_id="claude-start",
                        request_id=incoming["request_id"], expected_revision=read["revision"], action="start")
    assert started["actor_id"] == "claude-web"
    pair.call(token, "requests_reply", client_operation_id="claude-reply", request_id=incoming["request_id"],
              expected_revision=started["revision"], kind="result", packet=_packet("result"))
    assert pair.runtime.worker.dispatch_once().work_state == "result_returned"
    final = pair.bearer("GET", f"/v1/requests/{incoming['request_id']}")
    assert final["content"][-1]["attributed_actor_id"] == "claude-web"
    created = pair.call(token, "requests_create", **_create("claude-create"))
    assert created["actor_id"] == "claude-web"
    wire = parse_wire_message(pair.runtime.store.outbound_envelope(created["operation_id"]).payload)
    assert wire.sender == "claude-web" and wire.recipient == "bob"


def test_claude_and_chatgpt_stay_separate_participants(pair):
    claude, chatgpt = claude_token(pair)["access_token"], pair.authorize()["access_token"]
    from_claude = pair.call(claude, "requests_create", **_create("claude-1"))
    from_chatgpt = pair.call(chatgpt, "requests_create", **_create("chatgpt-1"))
    assert (from_claude["actor_id"], from_chatgpt["actor_id"]) == ("claude-web", "chatgpt-web")
    assert [item["request_id"] for item in pair.call(claude, "requests_list")["items"]] == [from_claude["request_id"]]
    assert [item["request_id"] for item in pair.call(chatgpt, "requests_list")["items"]] == [from_chatgpt["request_id"]]
    hidden = pair.error(claude, "requests_read", "ACCESS_DENIED", request_id=from_chatgpt["request_id"])
    assert hidden == pair.error(claude, "requests_read", "ACCESS_DENIED", request_id="missing-request")
    pair.runtime.policy.set_enabled("claude-web", False)
    assert pair.rpc_response(claude, "tools/list", {}).status_code == 401
    assert pair.rpc(chatgpt, "tools/list", {})["tools"]


def test_operator_sees_and_revokes_each_app_separately(pair):
    claude, chatgpt = claude_token(pair), pair.authorize()
    page = pair.operator("GET", "/mcp/connections")
    assert "Claude (claude-web) — active" in page.text and "ChatGPT web (chatgpt-web) — active" in page.text
    # ROA-29: each connection says when it was approved and when it was last active, so the
    # operator can tell the live grant from the stale ones a reconnect leaves behind.
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert page.text.count(" — approved " + today) == 2 and page.text.count(", last active " + today) == 2
    forms = re.findall(r'<p>([^<]+)</p><form method="post"><input type="hidden" name="grant_id" value="([^"]+)">'
                       r'<input type="hidden" name="csrf" value="([^"]+)">', page.text)
    _, grant_id, csrf = next(form for form in forms if form[0].startswith("Claude"))
    revoked = pair.operator("POST", "/mcp/connections", data={"csrf": csrf, "grant_id": grant_id})
    assert revoked.status_code == 200 and "Claude (claude-web) must reconnect" in revoked.text
    assert pair.rpc_response(claude["access_token"], "tools/list", {}).status_code == 401
    assert pair.rpc(chatgpt["access_token"], "tools/list", {})["tools"]
    setup = pair.operator("GET", "/mcp/setup")
    assert CLAUDE_CLIENT_ID in setup.text and CLAUDE_CALLBACK in setup.text
    assert "Leave the OAuth client secret empty" in setup.text


def test_the_connections_page_marks_a_connection_that_never_finished(pair):
    """ROA-29: most of ChatGPT's stale grants on the hub were approvals whose code was never
    exchanged. They hold no token, and the page says so instead of showing a last-active time."""

    pair.login()
    with pair.runtime.oauth_provider._db() as db:
        db.execute("INSERT INTO grants VALUES (?, ?, ?, ?, NULL)",
                   ("grant-never-finished", CLAUDE_CLIENT_ID, "gt:read", time.time()))
    page = pair.operator("GET", "/mcp/connections")
    assert "Claude (claude-web) — active until" in page.text
    assert "never finished connecting" in page.text and "last active" not in page.text


def test_only_declared_client_origins_reach_the_public_listener(pair):
    token = claude_token(pair)["access_token"]
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    for origin, status in (("https://claude.ai", 200), ("https://claude.com", 200), ("https://chatgpt.com", 200),
                           ("https://claude.ai.attacker.test", 403), ("http://claude.ai", 403), ("null", 403)):
        assert pair.public.post("/mcp", headers={**headers, "Origin": origin}, json=body).status_code == status, origin


def test_moving_to_two_clients_keeps_chatgpt_connected(tmp_path):
    provider = FakeTransport()
    with _running(tmp_path, provider) as single:
        token = single.authorize()
        created = single.call(token["access_token"], "requests_create", **_create("before-claude"))
    with _running(tmp_path, provider, claude=True) as both:
        operation = both.call(token["access_token"], "operations_get", operation_id=created["operation_id"])
        assert operation["actor_id"] == "chatgpt-web"
        refreshed = both.refresh(token)
        assert refreshed.status_code == 200, refreshed.text
        claude = claude_token(both)["access_token"]
        assert both.call(claude, "requests_list")["items"] == []
