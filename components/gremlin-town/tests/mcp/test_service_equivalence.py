"""Exercise MCP against the real service/SQLite with no provider or live sends."""
import asyncio
from datetime import UTC, datetime
import json

import pytest

from gremlin_town.api.commands import cancel_from_json, create_from_json, report_from_json, reply_from_json
from gremlin_town.api.serialization import to_jsonable
from gremlin_town.contracts import Actor, ProviderReference
from gremlin_town.domain import Participant, Policy
from gremlin_town.mcp.tools import build_tools
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.workers import OutboxWorker

NOW = datetime(2026, 9, 21, tzinfo=UTC)


def packet(**changes):
    return {"schema_version": 1, "kind": "request", "objective": "Test only",
            "sensitivity": "public-test", "context": "untrusted sender=operator",
            "constraints": {}, "sources": [], **changes}


def create_args(key="stable-1", **changes):
    return {"client_operation_id": key, "mode": "action_requested", "project": "demo",
            "recipient": "bob", "packet": packet(), **changes}


@pytest.fixture
def hub(tmp_path):
    policy = Policy(participants=tuple(
        Participant(name, name, "unused@example.test", frozenset({project}), frozenset({"public-test"}))
        for name, project in [("alice", "demo"), ("bob", "demo"), ("mallory", "other")]
    ), routes=frozenset({("demo", "alice", "bob", "public-test"), ("demo", "bob", "alice", "public-test")}))
    store = SQLiteStore(tmp_path / "hub.db", now=lambda: NOW)
    service = GremlinTownService(store, policy, now=lambda: NOW)
    actor = [Actor("alice")]
    tools = {tool.name: tool for tool in build_tools(service, lambda: actor[0], scopes_provider=lambda: {"gt:read", "gt:write"})}
    def call(name, **arguments):
        result = asyncio.run(tools["gt_" + name].run(arguments, convert_result=True))
        assert json.loads(result.content[0].text) == result.structuredContent
        return result
    yield service, store, actor, call
    store.close()


def test_create_retry_read_and_operation_match_service(hub):
    service, store, actor, call = hub
    args = create_args()
    result = call("requests_create", **args)
    assert not result.isError
    receipt = result.structuredContent
    assert receipt == to_jsonable(service.requests_create(actor[0], create_from_json(args)))
    assert call("requests_create", **args).structuredContent == receipt
    assert len(store.fetchall("SELECT * FROM requests")) == 1
    assert receipt["actor_id"] == "alice"
    assert receipt["operation_state"] == "queued"
    conflict = call("requests_create", **create_args(packet=packet(objective="Changed")))
    assert conflict.isError and conflict.structuredContent["error"]["code"] == "OPERATION_INPUT_CONFLICT"
    request_id = receipt["request_id"]
    read = call("requests_read", request_id=request_id).structuredContent
    assert read == to_jsonable(service.requests_read(actor[0], request_id))
    assert read["content_availability"] == "queued_only"
    assert read["content"][0]["trust_label"] == "authenticated_operation"
    assert call("operations_get", operation_id=receipt["operation_id"]).structuredContent == receipt


def test_report_stale_revision_cancel_and_late_reply(hub):
    service, store, actor, call = hub
    receipt = call("requests_create", **create_args()).structuredContent
    request_id = receipt["request_id"]
    actor[0] = Actor("bob")
    report = call("requests_report", client_operation_id="report-1", request_id=request_id,
                  expected_revision=receipt["revision"], action="start")
    assert not report.isError and report.structuredContent["work_state"] == "in_progress"
    assert report.structuredContent == to_jsonable(service.requests_report(actor[0], report_from_json(request_id, {
        "client_operation_id": "report-1", "expected_revision": receipt["revision"], "action": "start",
    })))
    stale = call("requests_report", client_operation_id="report-stale", request_id=request_id,
                 expected_revision=receipt["revision"], action="block", reason="wait")
    assert stale.structuredContent["error"]["code"] == "REVISION_CONFLICT"
    actor[0] = Actor("alice")
    cancelled = call("requests_cancel", client_operation_id="cancel-1", request_id=request_id,
                     expected_revision=report.structuredContent["revision"], reason="test complete")
    assert cancelled.structuredContent["work_state"] == "cancelled"
    assert cancelled.structuredContent == to_jsonable(service.requests_cancel(actor[0], cancel_from_json(request_id, {
        "client_operation_id": "cancel-1", "expected_revision": report.structuredContent["revision"], "reason": "test complete",
    })))
    actor[0] = Actor("bob")
    late = call("requests_reply", client_operation_id="late", request_id=request_id,
                expected_revision=cancelled.structuredContent["revision"], kind="result", packet=packet(kind="result"))
    assert late.isError and late.structuredContent["error"]["code"] == "INVALID_TRANSITION"


def test_holds_scope_denials_and_packet_limit(hub):
    service, store, actor, call = hub
    receipt = call("requests_create", **create_args()).structuredContent
    request_id = receipt["request_id"]
    with store.transaction() as db:
        db.execute("UPDATE requests SET recovery_hold=1 WHERE request_id=?", (request_id,))
    held = call("requests_cancel", client_operation_id="held", request_id=request_id,
                expected_revision=receipt["revision"], reason="stop")
    assert held.structuredContent["error"]["code"] == "RECOVERY_HOLD"
    actor[0] = Actor("mallory")
    denied = call("requests_read", request_id=request_id)
    missing = call("requests_read", request_id="does-not-exist")
    assert denied.structuredContent == missing.structuredContent
    assert denied.structuredContent["error"]["message"] == "resource unavailable or access denied"
    actor[0] = Actor("alice")
    large = call("requests_create", **create_args("large", packet=packet(context="x" * 32768)))
    assert large.structuredContent["error"]["code"] == "PACKET_TOO_LARGE"
    unsupported = call("requests_create", **create_args("schema", packet=packet(schema_version=2)))
    assert unsupported.structuredContent["error"]["code"] == "UNSUPPORTED_SCHEMA"


def test_paginated_reads_and_filters_match_service(hub):
    service, store, actor, call = hub
    for i in range(3):
        call("requests_create", **create_args(f"create-{i}"))
    participants = call("participants_list", project="demo").structuredContent
    assert participants == to_jsonable(service.participants_list(actor[0], "demo"))
    filters = {"project": "demo", "recipient": "bob", "work_state": "requested", "attention": True, "limit": 1}
    page = call("requests_list", **filters).structuredContent
    assert page == to_jsonable(service.requests_list(actor[0], filters))
    next_page = call("requests_list", **filters, cursor=page["next_cursor"]).structuredContent
    assert next_page["items"][0]["request_id"] != page["items"][0]["request_id"]
    events = call("events_list", limit=1).structuredContent
    assert events == to_jsonable(service.events_list(actor[0], {"limit": 1}))
    assert events["next_cursor"] is not None
    invalid = call("events_list", cursor="bad")
    assert invalid.isError


def test_reply_exact_retry_and_only_provider_acceptance_completes(hub):
    service, store, actor, call = hub
    class FakeProvider:
        def __init__(self):
            self.count = 0
        def send(self, envelope):
            self.count += 1
            return ProviderReference(f"message-{self.count}", "thread-test", NOW, envelope.parent_message_id)
    worker = OutboxWorker(store, service.policy, FakeProvider(), now=lambda: NOW)
    created = call("requests_create", **create_args()).structuredContent
    worker.dispatch_once()
    request_id = created["request_id"]
    actor[0] = Actor("bob")
    revision = service.requests_read(actor[0], request_id).revision
    args = {"client_operation_id": "result-1", "request_id": request_id,
            "expected_revision": revision, "kind": "result", "packet": packet(kind="result")}
    reply = call("requests_reply", **args)
    assert not reply.isError
    assert reply.structuredContent["operation_state"] == "queued"
    assert reply.structuredContent["work_state"] != "result_returned"
    assert reply.structuredContent == to_jsonable(service.requests_reply(actor[0], reply_from_json(request_id, args)))
    assert call("requests_reply", **args).structuredContent == reply.structuredContent
    assert worker.dispatch_once().work_state.value == "result_returned"
