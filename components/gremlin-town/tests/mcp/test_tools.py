import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime
import threading
import time

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from gremlin_town.contracts import Actor, ErrorCode, Page, Receipt, ReceiptError
from gremlin_town.mcp.tools import build_tools


def test_scope_checked_each_call_and_operator_refused():
    class Service:
        def participants_list(self, actor, project):
            return Page(items=({"actor_id": actor.actor_id},))
    actor = [Actor("alice")]
    scopes = [{"gt:read", "gt:write"}]
    tool = build_tools(Service(), lambda: actor[0], scopes_provider=lambda: scopes[0])[0]
    assert not asyncio.run(tool.run({})).isError
    scopes[0] = set()
    assert asyncio.run(tool.run({})).isError
    scopes[0] = {"gt:read"}
    actor[0] = Actor("operator", is_operator=True)
    assert asyncio.run(tool.run({})).isError


def test_default_scopes_cannot_write():
    tools = {tool.name: tool for tool in build_tools(object(), lambda: Actor("alice"))}
    result = asyncio.run(tools["gt_requests_cancel"].run({
        "client_operation_id": "x", "request_id": "r", "expected_revision": 1, "reason": "stop",
    }))
    assert result.isError and result.structuredContent["error"]["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "2"}, {"work_state": "made-up"}, {"attention": "true"}])
def test_invalid_filters_rejected(arguments):
    tool = next(t for t in build_tools(object(), lambda: Actor("alice")) if t.name == "gt_requests_list")
    with pytest.raises(ToolError):
        asyncio.run(tool.run(arguments))


@pytest.mark.parametrize("packet", ["{}", {"schema_version": 1, "kind": "request", "objective": "x", "sensitivity": "public-test", "actor": "operator"}])
def test_packet_must_be_structured_and_has_no_unknown_fields(packet):
    tool = next(t for t in build_tools(object(), lambda: Actor("alice")) if t.name == "gt_requests_create")
    with pytest.raises(ToolError):
        asyncio.run(tool.run({"client_operation_id": "x", "mode": "action_requested", "project": "demo", "recipient": "bob", "packet": packet}))


def test_unexpected_service_exception_redacted_and_receipt_error_preserved(caplog):
    class Service:
        def participants_list(self, actor, project):
            raise RuntimeError("secret-provider-url")
        def operations_get(self, actor, operation_id):
            return Receipt(operation_id, "req", datetime(2026, 9, 21, tzinfo=UTC), None,
                           None, 1, actor.actor_id, error=ReceiptError(ErrorCode.SOURCE_UNAVAILABLE, "not available", True))
    tools = {tool.name: tool for tool in build_tools(Service(), lambda: Actor("alice"))}
    error = asyncio.run(tools["gt_participants_list"].run({}))
    assert error.isError and "secret-provider-url" not in str(error)
    assert "secret-provider-url" not in caplog.text
    receipt = asyncio.run(tools["gt_operations_get"].run({"operation_id": "op"}))
    assert receipt.isError and receipt.structuredContent["error"]["retryable"] is True


def test_service_offload_bounded_and_actor_context_retained():
    current = ContextVar("actor")
    active = 0
    maximum = 0
    guard = threading.Lock()
    caller_thread = threading.get_ident()
    class Service:
        def participants_list(self, actor, project):
            nonlocal active, maximum
            assert threading.get_ident() != caller_thread
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return Page(items=({"actor_id": actor.actor_id},))
    async def scenario():
        tool = build_tools(Service(), current.get, concurrency_limit=2)[0]
        async def call(name):
            current.set(Actor(name))
            result = await tool.run({})
            assert result.structuredContent["items"][0]["actor_id"] == name
        await asyncio.gather(*(call(str(i)) for i in range(8)))
    asyncio.run(scenario())
    assert maximum == 2


def test_async_authorization_precedes_actor_and_scope_resolution_on_every_call():
    order = []
    class Service:
        def participants_list(self, actor, project):
            order.append("service")
            return Page(items=())
    denied = [False]
    async def authorize():
        await asyncio.sleep(0)
        order.append("authorize")
        if denied[0]:
            raise PermissionError("synthetic-private-auth-detail")
    def actor():
        order.append("actor")
        return Actor("alice")
    def scopes():
        order.append("scopes")
        return {"gt:read"}
    tool = build_tools(Service(), actor, scopes_provider=scopes, authorize=authorize)[0]
    assert not asyncio.run(tool.run({})).isError
    assert order == ["authorize", "actor", "scopes", "service"]
    denied[0] = True
    order.clear()
    result = asyncio.run(tool.run({}))
    assert order == ["authorize"]
    assert result.isError and result.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert "synthetic-private-auth-detail" not in str(result)


def test_async_authorization_failure_is_not_reported_as_permission_denial(caplog):
    async def authorize():
        raise RuntimeError("synthetic-storage-secret")
    tool = build_tools(object(), lambda: Actor("alice"), authorize=authorize)[0]
    result = asyncio.run(tool.run({}))
    assert result.isError and result.structuredContent["error"]["code"] == "INTERNAL_ERROR"
    assert "synthetic-storage-secret" not in str(result) + caplog.text
