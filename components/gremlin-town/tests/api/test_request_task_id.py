"""A request may name the Brain task it is for (Ciel rebuild slice 2, N12b; step B, B6).

The hub copies the task's record ID onto the request's "where I stopped" note (N12a), so the
request carries it from creation: an optional create input, stored with the request's own
creation event, and returned by list and read. Everything a request made without one sees stays
as it was, including the create operation's input digest - so a retry that straddles the release
still replays instead of conflicting.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from gremlin_town.api.app import create_app
from gremlin_town.contracts import (
    Actor,
    CreateRequest,
    ErrorCode,
    MessageKind,
    Packet,
    Page,
    ProviderReference,
    Receipt,
    RequestMode,
)
from gremlin_town.domain import GremlinTownError, Participant, Policy, canonical_digest
from gremlin_town.mcp.tools import build_tools
from gremlin_town.service import GremlinTownService
from gremlin_town.service.service import create_digest
from gremlin_town.storage import SQLiteStore
from gremlin_town.workers import OutboxWorker

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
TASK_ID = "brn_20260928_110000_abcd1234"


class FakeProvider:
    def __init__(self) -> None:
        self.count = 0

    def send(self, envelope):
        self.count += 1
        return ProviderReference(f"message-{self.count}", "thread-test", NOW, envelope.parent_message_id)


@pytest.fixture
def hub(tmp_path):
    policy = Policy(
        participants=(
            Participant("alice", "alice", "alice@example.test", frozenset({"demo"}), frozenset({"public"})),
            Participant("bob", "bob", "bob@example.test", frozenset({"demo"}), frozenset({"public"})),
            Participant(
                "carol", "carol", "carol@example.test", frozenset({"demo"}), frozenset({"public"}),
                read_all_requests=True,
            ),
        ),
        routes=frozenset({("demo", "alice", "bob", "public"), ("demo", "bob", "alice", "public")}),
    )
    store = SQLiteStore(tmp_path / "hub.db", now=lambda: NOW)
    service = GremlinTownService(store, policy, now=lambda: NOW)
    worker = OutboxWorker(store, policy, FakeProvider(), now=lambda: NOW)
    client = TestClient(create_app(service, actor_resolver={
        "alice-token": Actor("alice"), "bob-token": Actor("bob"), "carol-token": Actor("carol"),
    }))
    yield client, service, worker
    store.close()


def _auth(participant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {participant}-token"}


def _packet_json() -> dict:
    return {
        "schema_version": 1, "kind": "request", "objective": "Draft the deck", "context": "For Sam",
        "constraints": {}, "sources": [], "next_action": "Draft it", "expected_reply": "The draft",
        "sensitivity": "public",
    }


def _create(client, **extra) -> dict:
    body = {
        "client_operation_id": "c1", "mode": "action_requested", "project": "demo", "recipient": "bob",
        "packet": _packet_json(), **extra,
    }
    return client.post("/v1/requests", headers=_auth("alice"), json=body)


def test_a_request_names_its_task_and_list_and_read_return_it(hub) -> None:
    client, service, _worker = hub
    created = _create(client, task_id=TASK_ID)
    assert created.status_code == 200, created.text
    request_id = created.json()["request_id"]

    listed = client.get("/v1/requests", headers=_auth("alice")).json()["items"]
    assert [(item["request_id"], item["task_id"]) for item in listed] == [(request_id, TASK_ID)]
    # A reader with read_all_requests sees it too; it is part of the request, not the sender's view.
    assert client.get("/v1/requests", headers=_auth("carol")).json()["items"][0]["task_id"] == TASK_ID
    assert client.get(f"/v1/requests/{request_id}", headers=_auth("bob")).json()["task_id"] == TASK_ID

    events = client.get("/v1/events", headers=_auth("alice"), params={"event_type": "request_created"}).json()
    assert events["items"][0]["detail"] == {"mode": "action_requested", "prior_request_id": None, "task_id": TASK_ID}


def test_a_request_without_a_task_keeps_its_old_shape(hub) -> None:
    client, _service, _worker = hub
    request_id = _create(client).json()["request_id"]
    assert client.get("/v1/requests", headers=_auth("alice")).json()["items"][0]["task_id"] is None
    assert client.get(f"/v1/requests/{request_id}", headers=_auth("alice")).json()["task_id"] is None
    events = client.get("/v1/events", headers=_auth("alice"), params={"event_type": "request_created"}).json()
    assert events["items"][0]["detail"] == {"mode": "action_requested", "prior_request_id": None}


@pytest.mark.parametrize("bad", ["call-the-bank", "brn_2026_x", 42, ""])
def test_a_task_id_must_be_a_brain_record_id(hub, bad) -> None:
    client, _service, _worker = hub
    refused = _create(client, task_id=bad)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "INVALID_INPUT"
    assert client.get("/v1/requests", headers=_auth("alice")).json()["items"] == []


def test_the_service_itself_refuses_a_malformed_task_id(hub) -> None:
    _client, service, _worker = hub
    command = CreateRequest(
        "c9", RequestMode.ACTION_REQUESTED, "demo", "bob",
        Packet(1, MessageKind.REQUEST, None, "objective", "", {}, (), "next", "reply", "public"),
        task_id="not-a-record",
    )
    with pytest.raises(GremlinTownError) as raised:
        service.requests_create(Actor("alice"), command)
    assert raised.value.code is ErrorCode.INVALID_TRANSITION


def test_the_create_digest_is_unchanged_without_a_task_and_binds_one_when_given(hub) -> None:
    client, _service, _worker = hub
    packet = Packet(1, MessageKind.REQUEST, None, "objective", "", {}, (), "next", "reply", "public")
    plain = CreateRequest("c1", RequestMode.ACTION_REQUESTED, "demo", "bob", packet)
    old_shape = {key: value for key, value in asdict(plain).items() if key != "task_id"}
    assert set(old_shape) == {"client_operation_id", "mode", "project", "recipient", "packet", "prior_request_id"}
    assert create_digest(plain) == canonical_digest(old_shape)
    linked = CreateRequest("c1", RequestMode.ACTION_REQUESTED, "demo", "bob", packet, task_id=TASK_ID)
    assert create_digest(linked) != create_digest(plain)

    # Replaying the same key and input returns the same request; a different task is a conflict.
    first = _create(client, task_id=TASK_ID).json()
    again = _create(client, task_id=TASK_ID).json()
    assert again["request_id"] == first["request_id"]
    conflict = _create(client, task_id="brn_20260928_110000_ffff")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "OPERATION_INPUT_CONFLICT"


def test_requests_list_can_read_one_known_request_by_id(hub) -> None:
    """The hub's handoff-note keeper reads one request's title and task this way (N12a)."""

    client, service, _worker = hub
    first = _create(client, task_id=TASK_ID).json()["request_id"]
    second = client.post("/v1/requests", headers=_auth("alice"), json={
        "client_operation_id": "c2", "mode": "action_requested", "project": "demo", "recipient": "bob",
        "packet": _packet_json(),
    }).json()["request_id"]
    page = service.requests_list(Actor("carol"), {"request_id": first})
    assert [(item["request_id"], item["objective"], item["task_id"]) for item in page.items] == [
        (first, "Draft the deck", TASK_ID)
    ]
    assert [item["request_id"] for item in service.requests_list(Actor("carol"), {"request_id": second}).items] == [second]
    assert service.requests_list(Actor("carol"), {"request_id": "req_missing"}).items == ()


def test_the_mcp_create_tool_takes_a_task_id_and_refuses_a_malformed_one() -> None:
    class Service:
        def __init__(self) -> None:
            self.commands: list[CreateRequest] = []

        def requests_create(self, actor, command):
            self.commands.append(command)
            return Receipt("op", "req", NOW, None, None, 1, actor.actor_id)

        def participants_list(self, actor, project):  # pragma: no cover - not called
            return Page(items=())

    service = Service()
    tools = {
        tool.name: tool
        for tool in build_tools(service, lambda: Actor("alice"), scopes_provider=lambda: {"gt:read", "gt:write"})
    }
    create = tools["gt_requests_create"]
    assert create.parameters["properties"]["task_id"]["pattern"] == r"^brn_\d{8}_\d{6}_[0-9a-f]{4,16}$"
    arguments = {
        "client_operation_id": "x", "mode": "action_requested", "project": "demo", "recipient": "bob",
        "packet": {"schema_version": 1, "kind": "request", "objective": "o", "sensitivity": "public"},
    }
    assert not asyncio.run(create.run({**arguments, "task_id": TASK_ID})).isError
    assert service.commands[-1].task_id == TASK_ID
    assert not asyncio.run(create.run(arguments)).isError
    assert service.commands[-1].task_id is None
    with pytest.raises(ToolError):
        asyncio.run(create.run({**arguments, "task_id": "call-the-bank"}))
