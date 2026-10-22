"""`bc_calendar_remove` (N8, rebuild plan G7): its own call, carrying Nic's words, registered only
while calendar entries are on, authorized as a change to the task, and logged as itself."""

from __future__ import annotations

import asyncio
import json

import pytest

from gremlin_town.contracts import Actor
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation,
    BrainRuntime,
    BrainRuntimeError,
    CalendarRemoveRequest,
    CalendarRemoveResult,
    OwnerAuthorization,
    OwnerSelector,
)
from gremlin_town.mcp.brain_tools import build_brain_tools

RECORD_ID = "brn_20260928_101500_ab12"


class Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def remove_calendar_entry(self, caller, selector, request):
        self.calls.append((caller, selector, request))
        return CalendarRemoveResult(request.operation_id, request.record_id, "removed", "Removed from your calendar")


class UsageLog:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


async def allow():
    return None


def _tools(runtime=None, *, scopes=("bc:read", "bc:write"), calendar_removal=True, usage_log=None,
           door=True, caller_id="hermes-wa"):
    subject = runtime or Runtime()
    built = build_brain_tools(
        subject, lambda: Actor(caller_id), scopes_provider=lambda: set(scopes), authorize=allow,
        usage_log=usage_log, calendar_removal=calendar_removal, door=door,
    )
    return subject, {tool.name: tool for tool in built}


ARGUMENTS = {
    "operation_id": "remove-dentist", "record_id": RECORD_ID,
    "said_by": "owner", "came_by": "direct", "words": "yes, take it off",
}


def test_the_tool_exists_only_when_asked_for():
    _, off = _tools(calendar_removal=False)
    _, on = _tools()
    assert "bc_calendar_remove" not in off
    assert set(on) - set(off) == {"bc_calendar_remove"}
    for enabled in (False, True):
        _, ordinary = _tools(calendar_removal=enabled, door=False)
        assert "bc_calendar_remove" not in ordinary and "bc_work" not in ordinary
        _, door = _tools(calendar_removal=enabled, door=True)
        assert ("bc_calendar_remove" in door) is enabled
        assert "bc_work" in door


def test_its_schema_needs_the_record_nics_words_and_a_stable_key_and_it_is_marked_destructive():
    _, tools = _tools()
    tool = tools["bc_calendar_remove"]
    assert tool.parameters["additionalProperties"] is False
    assert set(tool.parameters["required"]) == {"operation_id", "record_id", "said_by", "came_by", "words"}
    assert tool.parameters["properties"]["scope"]["enum"] == ["personal", "cross-project"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True
    assert "fresh yes every time" in tool.description and "same input" in tool.description
    # ROA-32: tool text states facts and preconditions, never how to report to the user.
    assert "tell the user" not in tool.description.casefold()
    assert tool.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["gt:read", "bc:read", "bc:write"]}]


def test_a_call_passes_exactly_the_request_and_is_logged_as_calendar_remove():
    log = UsageLog()
    runtime, tools = _tools(usage_log=log)
    result = asyncio.run(tools["bc_calendar_remove"].run(dict(ARGUMENTS)))

    assert not result.isError
    assert json.loads(result.content[0].text) == {
        "operation_id": "remove-dentist", "record_id": RECORD_ID, "status": "removed",
        "text": "Removed from your calendar", "reason": None,
    }
    [(caller, selector, request)] = runtime.calls
    assert caller.subject == "hermes-wa"
    assert selector == OwnerSelector("personal")
    assert request == CalendarRemoveRequest("remove-dentist", RECORD_ID, "owner", "direct", "yes, take it off")
    [event] = log.events
    assert (event.operation, event.operation_id, event.outcome) == ("calendar.remove", "remove-dentist", "ok")


def test_a_reader_cannot_remove_anything():
    runtime, tools = _tools(scopes=("bc:read",))
    result = asyncio.run(tools["bc_calendar_remove"].run(dict(ARGUMENTS)))
    assert result.isError
    assert json.loads(result.content[0].text)["error"]["code"] == "ACCESS_DENIED"
    assert runtime.calls == []


class Authorizer:
    def __init__(self) -> None:
        self.asked = []

    async def authorize(self, caller, operation, selector, access):
        self.asked.append((operation, access))
        return OwnerAuthorization(operation, access, selector, "brain-home", "vault://brain-home", "rev-1", object())


class Memory:
    def __init__(self, supported: bool) -> None:
        self.requests = []
        if supported:
            self.remove_calendar_entry = self._remove

    async def _remove(self, authorization, request):
        self.requests.append((authorization.operation, request))
        return CalendarRemoveResult(request.operation_id, request.record_id, "removed", "Removed from your calendar")


def _runtime(memory, authorizer) -> BrainRuntime:
    return BrainRuntime(authorizer=authorizer, context=None, continuity=None, decisions=None, memory=memory)


REQUEST = CalendarRemoveRequest("remove-dentist", RECORD_ID, "owner", "direct", "yes")
CALLER = BrainCaller("hermes-wa", frozenset({"bc:read", "bc:write"}))


def test_the_runtime_authorizes_it_as_a_change_to_the_task():
    memory, authorizer = Memory(supported=True), Authorizer()
    result = asyncio.run(_runtime(memory, authorizer).remove_calendar_entry(CALLER, OwnerSelector("personal"), REQUEST))
    assert result.status == "removed"
    assert authorizer.asked == [(BrainOperation.MEMORY_CHANGE, "write")]
    assert memory.requests == [(BrainOperation.MEMORY_CHANGE, REQUEST)]


def test_without_calendar_entries_the_runtime_says_they_are_not_switched_on():
    with pytest.raises(BrainRuntimeError) as caught:
        asyncio.run(_runtime(Memory(supported=False), Authorizer()).remove_calendar_entry(
            CALLER, OwnerSelector("personal"), REQUEST,
        ))
    assert caught.value.code == "brain.capability.unavailable"


@pytest.mark.parametrize("caller_id", ["chatgpt-web", "claude-web", "fleet-boss-grok", "local-codex", "system:unknown"])
def test_an_unauthorized_caller_cannot_invoke_calendar_removal(caller_id):
    memory, authorizer = Memory(supported=True), Authorizer()
    runtime = _runtime(memory, authorizer)
    _, tools = _tools(runtime, caller_id=caller_id)
    result = asyncio.run(tools["bc_calendar_remove"].run(dict(ARGUMENTS)))
    assert result.isError, result.content[0].text
    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "ACCESS_DENIED" and error["field"] == "caller"
    assert error["fix"] == "remove it through Diablo"
    assert memory.requests == []
    assert authorizer.asked == []


def test_calendar_removal_uses_the_configured_door_and_keeps_owner_authorization():
    memory, authorizer = Memory(supported=True), Authorizer()
    memory.door_participant = "door-next"
    runtime = _runtime(memory, authorizer)
    with pytest.raises(BrainRuntimeError) as denied:
        asyncio.run(runtime.remove_calendar_entry(CALLER, OwnerSelector("personal"), REQUEST))
    assert denied.value.code == "ACCESS_DENIED" and denied.value.fix == "remove it through Diablo"
    assert memory.requests == [] and authorizer.asked == []
    caller = BrainCaller("door-next", CALLER.scopes)
    result = asyncio.run(runtime.remove_calendar_entry(caller, OwnerSelector("personal"), REQUEST))
    assert result.status == "removed"
    assert authorizer.asked == [(BrainOperation.MEMORY_CHANGE, "write")]
    assert memory.requests == [(BrainOperation.MEMORY_CHANGE, REQUEST)]
