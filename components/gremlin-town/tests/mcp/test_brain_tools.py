import asyncio
import json
from datetime import UTC, datetime

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import gremlin_town.mcp.brain_tools as brain_tools
from gremlin_town.contracts import Actor
from gremlin_town.mcp.brain_runtime import (
    BrainRuntimeError,
    ContextResult,
    ContinuityRecovery,
    ContinuityWriteResult,
    MemoryRecord,
    OwnerSelector,
    SourceEvidence,
    WriteReceipt,
    WorkRequest,
    work_check_only,
)
from gremlin_town.mcp.brain_tools import build_brain_tools
from gremlin_town.mcp.tools import build_tools


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
BC_NAMES = {
    "bc_context_retrieve",
    "bc_continuity_start",
    "bc_continuity_append",
    "bc_continuity_recover",
    "bc_continuity_list",
    "bc_continuity_close",
    "bc_decision_record",
    "bc_memory_save",
    "bc_memory_change",
    "bc_memory_read",
    "bc_memory_search",
    "bc_memory_list",
}
GT_NAMES = {
    "gt_participants_list",
    "gt_requests_create",
    "gt_requests_list",
    "gt_requests_read",
    "gt_requests_report",
    "gt_requests_reply",
    "gt_requests_cancel",
    "gt_requests_check",
    "gt_operations_get",
    "gt_events_list",
}


def write_receipt(operation_id="save-1"):
    return WriteReceipt(
        operation_id, "accepted", "vault://brain-home", "brn_20260921_120000_abcd", "commit-1",
        "a" * 64, "b" * 64, True,
    )


class Runtime:
    def __init__(self, events=None):
        self.calls = []
        self.events = events

    async def retrieve_context(self, caller, selector, query):
        self.calls.append(("context", caller, selector, query))
        if self.events is not None:
            self.events.append("runtime")
        return ContextResult(
            (SourceEvidence(
                "E1", "vault://demo", "vault://demo/context.md", "worktree:1",
                "a" * 64, "export", "fts5", "line:1", 3, "permitted excerpt",
            ),), (), "c" * 64, NOW, "current",
        )

    async def start_continuity(self, caller, selector, request):
        self.calls.append(("start", caller, selector, request))
        return ContinuityWriteResult("brn_20260921_120000_abcd", request.session_id, "open", True, NOW, write_receipt(request.operation_id))

    async def append_continuity(self, caller, selector, request):
        self.calls.append(("append", caller, selector, request))
        return ContinuityWriteResult("brn_20260921_120000_abcd", request.session_id, "open", True, NOW, write_receipt(request.operation_id))

    async def recover_continuity(self, caller, selector, request):
        self.calls.append(("recover", caller, selector, request))
        return ContinuityRecovery(
            "brn_20260921_120000_abcd", "session-1", "open", "intent", "next", NOW,
            "brain.lexical/v1", 20,
        )

    async def close_continuity(self, caller, selector, request):
        self.calls.append(("close", caller, selector, request))
        state = "recovered" if request.recovered else "closed"
        return ContinuityWriteResult("brn_20260921_120000_abcd", request.session_id, state, True, NOW, write_receipt(request.operation_id))

    async def record_decision(self, caller, selector, request):
        self.calls.append(("decision", caller, selector, request))
        return write_receipt(request.operation_id)

    async def save_memory(self, caller, selector, request):
        self.calls.append(("save", caller, selector, request))
        return write_receipt(request.operation_id)

    async def change_memory(self, caller, selector, request):
        self.calls.append(("change", caller, selector, request))
        return write_receipt(request.operation_id)

    async def change_work(self, caller, selector, request):
        self.calls.append(("work", caller, selector, request))
        return {"status": "validated" if request.check_only else "accepted",
                "change": request.operation_id, "remote_observed": not request.check_only}

    async def read_memory(self, caller, selector, record_id):
        self.calls.append(("read", caller, selector, record_id))
        return MemoryRecord(
            record_id, "item", "Title", "Body", "open", selector.project_id or "vault://brain-home",
            "commit-1", "a" * 64, NOW, "current",
        )


async def allow():
    return None


def test_w14_orientation_evidence_schema_and_tier_text():
    from tests.adapters.test_hermes_brain import orientation_tools
    definitions = {tool.name: tool for tool in orientation_tools()}
    schema = definitions["bc_next_respond"].parameters
    assert schema["properties"]["said_by"]["enum"] == ["owner", "other"]
    assert schema["properties"]["message"]["maxLength"] == 8192
    assert "message" not in schema["required"]  # tier B and no/not_now need no door evidence
    assert "actor" not in schema["properties"]
    assert "Tier B" in definitions["bc_next_respond"].description
    assert "original raw message" in definitions["bc_next_respond"].description
    assert "Never invent a first step" in definitions["bc_next"].description
    assert "bc_memory_change" not in definitions["bc_orient"].description


def tools(runtime=None, actor=None, scopes=None, authorize=allow, *, door=False,
          calendar_removal=False, project_saves=False):
    subject = runtime or Runtime()
    values = scopes if scopes is not None else {"bc:read", "bc:write"}
    definitions = build_brain_tools(
        subject,
        lambda: actor or Actor("alice"),
        scopes_provider=lambda: values,
        authorize=authorize, door=door, calendar_removal=calendar_removal, project_saves=project_saves,
    )
    return subject, {tool.name: tool for tool in definitions}


def test_exact_bc_namespace_strict_schemas_and_no_trusted_host_inputs():
    _, definitions = tools()
    assert set(definitions) == BC_NAMES
    forbidden = {
        "owner_locator", "policy_revision", "capability", "root", "repository",
        "branch", "grant", "shell", "filesystem", "brain_json",
    }
    writes = {
        "bc_continuity_start", "bc_continuity_append", "bc_continuity_close",
        "bc_decision_record", "bc_memory_save", "bc_memory_change",
    }
    for name, tool in definitions.items():
        assert tool.parameters["additionalProperties"] is False
        schema = json.dumps(tool.parameters, sort_keys=True).casefold()
        assert all(term not in schema for term in forbidden)
        assert tool.annotations.readOnlyHint is (name not in writes)
        assert tool.annotations.destructiveHint is (name == "bc_continuity_close")
        if name in writes:
            assert "operation_id" in tool.parameters["required"]
            assert "same" in tool.description


def test_original_gt_tools_remain_unchanged_plus_requests_check():
    assert {tool.name for tool in build_tools(object(), lambda: Actor("alice"))} == GT_NAMES


def test_continuity_start_passes_the_task_id_and_refuses_a_malformed_one():
    """N12b: bc_continuity_start's optional task_id names the task the note is for."""

    runtime, definitions = tools()
    start = definitions["bc_continuity_start"]
    assert "pass it when the note is for a task" in start.parameters["properties"]["task_id"]["description"]
    base = {"operation_id": "note-start", "session_id": "call-the-bank", "intent": "Call the bank", "source_references": []}
    assert not asyncio.run(start.run({**base, "task_id": "brn_20260928_110000_abcd1234"})).isError
    assert runtime.calls[-1][3].task_id == "brn_20260928_110000_abcd1234"
    assert not asyncio.run(start.run(base)).isError
    assert runtime.calls[-1][3].task_id is None
    with pytest.raises(ToolError):
        asyncio.run(start.run({**base, "task_id": "call-the-bank"}))


def test_native_brain_vault_id_is_used_for_context_and_continuity_only():
    runtime, definitions = tools()
    vault_id = "vault_git_fixture"

    context = asyncio.run(definitions["bc_context_retrieve"].run({
        "project_id": vault_id,
        "intent": "current fixture context",
    }))
    started = asyncio.run(definitions["bc_continuity_start"].run({
        "operation_id": "native-start",
        "project_id": vault_id,
        "session_id": "session-1",
        "intent": "native vault continuity",
        "source_references": ["source-1"],
    }))
    appended = asyncio.run(definitions["bc_continuity_append"].run({
        "operation_id": "native-append",
        "project_id": vault_id,
        "session_id": "session-1",
        "event": "blocker",
        "value": "none",
    }))
    recovered = asyncio.run(definitions["bc_continuity_recover"].run({
        "project_id": vault_id,
        "max_tokens": 2500,
    }))
    closed = asyncio.run(definitions["bc_continuity_close"].run({
        "operation_id": "native-close",
        "project_id": vault_id,
        "session_id": "session-1",
        "recovered": True,
    }))

    assert all(not result.isError for result in (context, started, appended, recovered, closed))
    assert [call[2] for call in runtime.calls] == [OwnerSelector("project", vault_id)] * 5

    with pytest.raises(ToolError):
        asyncio.run(definitions["bc_decision_record"].run({
            "operation_id": "native-decision",
            "scope": "project",
            "project_id": vault_id,
            "title": "Keep decision owner IDs separate",
            "choice": "Use the decision-owner schema",
            "because": "Native connector vault IDs are a different boundary",
            "forecloses": "Silently widening decision owner IDs",
            "governing_scope": "project ownership",
            "encoded_in": "owner policy",
            "completeness_check": "decision schema remains unchanged",
            "source_reference": "source-1",
        }))
    assert len(runtime.calls) == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_locator", "vault://brain-home"),
        ("root", "C:/private"),
        ("repository", "owner/private"),
        ("branch", "main"),
        ("grant", {"path": "private.md"}),
    ],
)
def test_caller_cannot_inject_trusted_owner_storage_or_grant_fields(field, value):
    _, definitions = tools()
    arguments = {"project_id": "vault_demo", "intent": "find current evidence", field: value}
    with pytest.raises(ToolError, match="Extra inputs"):
        asyncio.run(definitions["bc_context_retrieve"].run(arguments))


def test_opaque_source_references_cannot_be_used_as_filesystem_paths():
    runtime, definitions = tools()
    with pytest.raises(ToolError):
        asyncio.run(definitions["bc_memory_save"].run({
            "operation_id": "save-1",
            "scope": "personal",
            "kind": "item",
            "level": "accepted",
            "said_by": "owner",
            "came_by": "direct",
            "words": "Body",
            "title": "Title",
            "body": "Body",
            "source_reference": "C:/private/file.md",
        }))
    assert runtime.calls == []


def test_authentication_global_scope_and_owner_runtime_order_is_per_call():
    order = []
    runtime = Runtime(order)
    actor = [Actor("alice")]
    scopes = [{"bc:read", "bc:write"}]

    async def authorize():
        order.append("authorize")

    definitions = build_brain_tools(
        runtime,
        lambda: actor[0],
        scopes_provider=lambda: (order.append("scopes") or scopes[0]),
        authorize=authorize,
    )
    tool = next(item for item in definitions if item.name == "bc_context_retrieve")
    first = asyncio.run(tool.run({"project_id": "vault_demo", "intent": "current evidence"}))
    assert not first.isError
    assert order == ["authorize", "scopes", "authorize", "runtime"]

    order.clear()
    scopes[0] = set()
    denied = asyncio.run(tool.run({"project_id": "vault_demo", "intent": "must not reach runtime"}))
    assert denied.isError and denied.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert order == ["authorize", "scopes"]

    order.clear()
    scopes[0] = {"bc:read"}
    actor[0] = Actor("operator", is_operator=True)
    denied = asyncio.run(tool.run({"project_id": "vault_demo", "intent": "must not reach runtime"}))
    assert denied.isError and order == ["authorize"]


def test_auth_revalidation_denial_precedes_actor_scope_and_runtime():
    events = []
    runtime = Runtime(events)

    async def deny():
        events.append("authorize")
        raise PermissionError("private auth detail")

    definitions = build_brain_tools(
        runtime,
        lambda: (events.append("actor") or Actor("alice")),
        scopes_provider=lambda: (events.append("scopes") or {"bc:read"}),
        authorize=deny,
    )
    tool = next(item for item in definitions if item.name == "bc_context_retrieve")
    result = asyncio.run(tool.run({"project_id": "vault_demo", "intent": "protected"}))
    assert events == ["authorize"]
    assert result.isError and result.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert "private auth detail" not in str(result)


def test_write_requires_both_bc_scopes_and_never_calls_runtime_when_missing():
    runtime, definitions = tools(scopes={"bc:read"})
    result = asyncio.run(definitions["bc_memory_save"].run({
        "operation_id": "save-1",
        "scope": "personal",
        "kind": "item",
        "level": "accepted",
        "said_by": "owner",
        "came_by": "direct",
        "words": "Protected body",
        "title": "Title",
        "body": "Protected body",
        "source_reference": "source-1",
    }))
    assert result.isError and result.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert runtime.calls == []


def test_gt_transport_identity_and_gt_scopes_do_not_grant_bc_read_access():
    runtime, definitions = tools(scopes={"gt:read", "gt:write"})
    result = asyncio.run(definitions["bc_context_retrieve"].run({
        "project_id": "vault_demo", "intent": "protected context",
    }))
    assert result.isError
    assert result.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert runtime.calls == []


def test_typed_write_request_is_not_constructed_before_auth(monkeypatch):
    constructed = []

    class ObservedMemorySaveRequest:
        def __init__(self, *args):
            constructed.append(args)

    async def deny():
        raise PermissionError("denied before typed request construction")

    monkeypatch.setattr(brain_tools, "MemorySaveRequest", ObservedMemorySaveRequest)
    runtime, definitions = tools(authorize=deny)
    result = asyncio.run(definitions["bc_memory_save"].run({
        "operation_id": "save-1",
        "scope": "personal",
        "kind": "item",
        "level": "accepted",
        "said_by": "owner",
        "came_by": "direct",
        "words": "Protected body",
        "title": "Title",
        "body": "Protected body",
        "source_reference": "source-1",
    }))
    assert result.isError and result.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert constructed == []
    assert runtime.calls == []


def test_tools_construct_only_typed_semantic_requests_and_preserve_receipts():
    runtime, definitions = tools()
    decision = asyncio.run(definitions["bc_decision_record"].run({
        "operation_id": "decide-1",
        "scope": "project",
        "project_id": "demo",
        "title": "Choose A",
        "choice": "Use A",
        "because": "It is bounded",
        "forecloses": "Use B",
        "governing_scope": "demo",
        "encoded_in": "project decision log",
        "completeness_check": "owner and consequence supplied",
        "source_reference": "source-1",
    }))
    assert not decision.isError
    assert decision.structuredContent["status"] == "accepted"
    assert decision.structuredContent["remote_observed"] is True
    _, caller, selector, request = runtime.calls[-1]
    assert caller.subject == "alice"
    assert selector == OwnerSelector("project", "demo")
    assert request.source_reference == "source-1"
    assert not hasattr(request, "owner_locator")
    assert not hasattr(request, "repository")
    assert not hasattr(request, "grant")


def test_bc_failure_codes_and_freshness_failures_are_preserved_without_secret_logs(caplog):
    class Changed(Runtime):
        async def retrieve_context(self, caller, selector, query):
            raise BrainRuntimeError("brain.source.changed", "Source changed during read")

    _, definitions = tools(runtime=Changed())
    result = asyncio.run(definitions["bc_context_retrieve"].run({
        "project_id": "vault_demo", "intent": "current evidence",
    }))
    assert result.isError
    assert result.structuredContent["error"] == {
        "code": "brain.source.changed",
        "message": "Source changed during read",
        "retryable": False,
        "field": "arguments",
        "fix": "correct the named field and resend the request",
    }
    assert caplog.text == ""


def test_a_wrong_project_id_reaches_the_agent_with_the_rule_and_the_valid_ids():
    """ROA-72 ("Rule + helpful error"): unlike a policy denial, this answer is passed on as is."""

    message = (
        "Project vault_exo is not enrolled. A project ID is vault_ plus the vault's name, for example "
        "vault_exosuit. Valid project IDs: vault_exosuit, vault_metavault."
    )

    class Unknown(Runtime):
        async def retrieve_context(self, caller, selector, query):
            raise BrainRuntimeError("brain.project.unknown", message)

    _, definitions = tools(runtime=Unknown())
    result = asyncio.run(definitions["bc_context_retrieve"].run({"project_id": "vault_exo", "intent": "anything"}))
    assert result.isError
    assert result.structuredContent["error"]["code"] == "brain.project.unknown"
    assert result.structuredContent["error"]["message"] == message


def test_owner_policy_denials_are_redacted_to_access_denied():
    class Denied(Runtime):
        async def read_memory(self, caller, selector, record_id):
            raise BrainRuntimeError("brain.policy.excluded", "secret owner policy explanation")

    _, definitions = tools(runtime=Denied())
    result = asyncio.run(definitions["bc_memory_read"].run({
        "scope": "personal", "record_id": "brn_20260921_120000_abcd",
    }))
    assert result.isError
    assert result.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert "secret owner policy explanation" not in str(result)


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        (
            "bc_decision_record",
            {
                "operation_id": "decide-1", "scope": "personal", "project_id": "demo",
                "title": "Title", "choice": "A", "because": "Because",
                "forecloses": "B", "governing_scope": "personal",
                "encoded_in": "Brain Home", "completeness_check": "complete",
                "source_reference": "source-1",
            },
        ),
        (
            "bc_memory_save",
            {
                "operation_id": "save-1", "scope": "personal", "project_id": "demo",
                "kind": "item", "level": "accepted", "said_by": "owner", "came_by": "direct",
                "words": "Body", "title": "Title", "body": "Body",
                "source_reference": "source-1",
            },
        ),
        (
            "bc_memory_read",
            {"scope": "personal", "project_id": "demo", "record_id": "brn_20260921_120000_abcd"},
        ),
    ],
)
def test_invalid_scope_pairs_map_to_stable_error_without_runtime_body_access(tool_name, arguments):
    runtime, definitions = tools()
    invalid_pair = asyncio.run(definitions[tool_name].run(arguments))
    assert invalid_pair.isError
    assert invalid_pair.structuredContent["error"]["code"] == "brain.invalid_argument"
    assert runtime.calls == []


def test_strict_scalars_fail_without_runtime_body_access():
    runtime, definitions = tools()

    with pytest.raises(ToolError):
        asyncio.run(definitions["bc_continuity_recover"].run({
            "project_id": "vault_demo", "max_tokens": "2500",
        }))
    assert runtime.calls == []


def test_cancellation_reaches_runtime_without_becoming_an_error_result():
    started = asyncio.Event()

    class Blocking(Runtime):
        async def retrieve_context(self, caller, selector, query):
            started.set()
            await asyncio.Event().wait()

    async def scenario():
        _, definitions = tools(runtime=Blocking())
        task = asyncio.create_task(definitions["bc_context_retrieve"].run({
            "project_id": "vault_demo", "intent": "wait",
        }))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_build_requires_secure_host_callbacks_and_valid_concurrency():
    with pytest.raises(TypeError):
        build_brain_tools(Runtime(), lambda: Actor("alice"))
    with pytest.raises(ValueError, match="positive integer"):
        build_brain_tools(
            Runtime(), lambda: Actor("alice"),
            scopes_provider=lambda: {"bc:read"}, authorize=allow,
            concurrency_limit=True,
        )


_SAVE_ITEM = {
    "operation_id": "save-important", "scope": "personal", "kind": "item", "level": "accepted",
    "said_by": "owner", "came_by": "direct", "words": "I need to renew my passport",
    "title": "Renew my passport", "body": "I need to renew my passport",
}


def test_memory_tools_carry_importance_and_focus_projects_to_the_runtime():
    """Save bridges stale importance for the provider to ignore; focus lists remain supported,
    while item importance has no change bridge."""

    runtime, definitions = tools()
    saved = asyncio.run(definitions["bc_memory_save"].run({
        **_SAVE_ITEM, "importance": "proposed", "importance_reason": "The Japan trip is off without it",
    }))
    assert not saved.isError
    request = runtime.calls[-1][3]
    assert (request.importance, request.importance_reason, request.focus_projects) == (
        "proposed", "The Japan trip is off without it", None,
    )

    asyncio.run(definitions["bc_memory_save"].run({
        **_SAVE_ITEM, "operation_id": "save-focus", "kind": "preference", "title": "Focus projects",
        "focus_projects": ["vault_exosuit", "job-search"],
    }))
    assert runtime.calls[-1][3].focus_projects == ("vault_exosuit", "job-search")

    before = len(runtime.calls)
    with pytest.raises(ToolError, match="Extra inputs"):
        asyncio.run(definitions["bc_memory_change"].run({
            "operation_id": "confirm-important", "scope": "personal", "record_id": "brn_20260921_120000_abcd",
            "expected_sha256": "a" * 64, "said_by": "owner", "came_by": "direct", "words": "yes",
            "importance": "confirmed",
        }))
    assert len(runtime.calls) == before



@pytest.mark.parametrize(
    "bad",
    [
        {"importance": "high"},
        {"importance_reason": ""},
        {"focus_projects": [f"p{i}" for i in range(11)]},
        {"focus_projects": [""]},
        {"focus_projects": ["x" * 129]},
        {"focus_projects": "vault_exosuit"},
    ],
)
def test_memory_save_bounds_importance_and_focus_projects_before_the_runtime(bad):
    runtime, definitions = tools()
    with pytest.raises(ToolError):
        asyncio.run(definitions["bc_memory_save"].run({**_SAVE_ITEM, **bad}))
    assert runtime.calls == []


def test_memory_change_has_no_focus_projects():
    """Focus projects change by saving a new list (the newest wins), never by editing one."""

    _, definitions = tools()
    assert "focus_projects" not in definitions["bc_memory_change"].parameters["properties"]
    assert {"importance", "importance_reason", "sort", "first_step", "size", "links"}.isdisjoint(
        definitions["bc_memory_change"].parameters["properties"]
    )


def test_saving_says_to_sort_anything_new_and_to_offer_two_minute_things_now():
    """W12 replaces pre-sort instructions with plain capture and the door boundary."""

    _, definitions = tools()
    save = " ".join(definitions["bc_memory_save"].description.split())
    assert "Save new things as said; changes to Nic's work go through Diablo or the morning review." in save
    assert "Sort anything new" not in save
    change = definitions["bc_memory_change"].description
    assert "Change one fact or preference" in change
    assert "items use bc_work through Diablo or the morning review" in change
    assert "sort checks an outcome" not in change
    retired = {"size", "importance", "importance_reason", "sort", "first_step", "links"}
    for name in ("bc_memory_save", "bc_memory_change"):
        assert retired.isdisjoint(definitions[name].parameters["properties"])
    model = definitions["bc_memory_save"].fn_metadata.arg_model
    assert retired <= model.model_fields.keys()
    schema = model.model_json_schema()
    assert schema["properties"]["sort"]["anyOf"][0]["enum"] == [
        "next_action", "waiting_on", "raise_with", "someday", "reference", "drop",
    ]
    assert schema["properties"]["first_step"]["anyOf"][0]["maxLength"] == 256



def test_sort_and_first_step_reach_the_runtime_and_a_two_line_first_step_is_refused():
    runtime, definitions = tools()
    save = definitions["bc_memory_save"]
    base = {
        "operation_id": "sort-save", "scope": "personal", "kind": "item", "title": "Call the accountant", "body": "VAT",
        "said_by": "owner", "came_by": "direct", "words": "call the accountant about the VAT", "level": "suggested",
        "sort": "next_action", "block": "admin", "first_step": "Find their number", "rough_minutes": 15,
    }
    assert not asyncio.run(save.run(base)).isError
    request = runtime.calls[-1][3]
    assert (request.sort, request.first_step) == ("next_action", "Find their number")
    with pytest.raises(ToolError):
        asyncio.run(save.run({**base, "first_step": "one\ntwo"}))


@pytest.mark.parametrize("door,calendar_removal", [(False, False), (False, True), (True, False), (True, True)])
def test_door_and_calendar_factory_boundaries_and_retired_project_tool(door, calendar_removal):
    _, definitions = tools(door=door, calendar_removal=calendar_removal, project_saves=True)
    assert ("bc_work" in definitions) is door
    assert ("bc_calendar_remove" in definitions) is (door and calendar_removal)
    assert "bc_project_save" not in definitions
    assert set(definitions) == BC_NAMES | ({"bc_work"} if door else set()) | (
        {"bc_calendar_remove"} if door and calendar_removal else set()
    )
    if door:
        assert definitions["bc_work"].annotations.readOnlyHint is False
        assert definitions["bc_work"].annotations.idempotentHint is True
        assert definitions["bc_work"].parameters["additionalProperties"] is False
    if door and calendar_removal:
        assert definitions["bc_calendar_remove"].annotations.destructiveHint is True


def test_save_bridge_preserves_all_six_retired_arguments_without_advertising_them():
    runtime, definitions = tools()
    tool = definitions["bc_memory_save"]
    values = {"size": "goal", "importance": "proposed", "importance_reason": "Trip at risk",
              "sort": "next_action", "first_step": "Find the number",
              "links": [{"type": "part_of", "target": "brn_20260921_120000_abcd"}]}
    assert values.keys().isdisjoint(tool.parameters["properties"])
    result = asyncio.run(tool.run({**_SAVE_ITEM, **values}))
    assert not result.isError
    request = runtime.calls[-1][3]
    for field in ("size", "importance", "importance_reason", "sort", "first_step"):
        assert getattr(request, field) == values[field]
    assert request.links == tuple(values["links"])


@pytest.mark.parametrize("stale", [
    {"importance": "confirmed"}, {"importance_reason": "Trip at risk"},
    {"sort": "next_action"}, {"first_step": "Find the number"},
    {"size": "goal"}, {"links": []}, {"occurrence": "done"},
])
def test_change_has_no_bridge_for_retired_item_arguments(stale):
    runtime, definitions = tools()
    arguments = {"operation_id": "confirm-important", "scope": "personal",
                 "record_id": "brn_20260921_120000_abcd", "expected_sha256": "a" * 64,
                 "said_by": "owner", "came_by": "direct", "words": "yes"}
    with pytest.raises(ToolError, match="Extra inputs"):
        asyncio.run(definitions["bc_memory_change"].run({**arguments, **stale}))
    assert runtime.calls == []


_WORK = {"operation_id": "work-1", "message": "Call the bank", "said_by": "owner",
         "came_by": "direct", "moves": [{"move": "new", "title": "Call the bank"}]}


def test_work_tool_preserves_new_and_nested_part_details():
    runtime, definitions = tools(actor=Actor("hermes-wa"), door=True)
    moves = [{"move": "new", "title": "Venue rental", "details": "Compare rates"},
        {"move": "parts", "thing": "#1", "parts": [{"title": "Call bank", "details": "Confirm documents"}]}]
    result = asyncio.run(definitions["bc_work"].run({**_WORK,
        "message": "Venue rental Compare rates Call bank Confirm documents", "moves": moves}))
    assert not result.isError and runtime.calls[-1][3].moves == tuple(moves)
    for value in (42, ""):
        with pytest.raises(ToolError):
            asyncio.run(definitions["bc_work"].run({**_WORK,
                "moves": [{"move": "new", "title": "Venue rental", "details": value}]}))


def test_work_constructs_typed_public_request_and_takes_check_only_only_from_host_context():
    runtime, definitions = tools(actor=Actor("hermes-wa"), door=True)
    tool = definitions["bc_work"]
    assert {"_context", "check_only", "words_from"}.isdisjoint(tool.parameters["properties"])
    result = asyncio.run(tool.run({**_WORK, "answers": ["review-line-1"]}))
    assert result.structuredContent["status"] == "accepted"
    operation, caller, selector, request = runtime.calls[-1]
    assert operation == "work" and caller.subject == "hermes-wa"
    assert selector == OwnerSelector("personal")
    assert isinstance(request, WorkRequest)
    assert request.operation_id == "work-1" and request.message == "Call the bank"
    assert request.moves == ({"move": "new", "title": "Call the bank"},)
    assert request.answers == ("review-line-1",)
    assert request.check_only is False
    assert not hasattr(request, "_context")
    token = work_check_only.set(True)
    try:
        checked = asyncio.run(tool.run(_WORK))
    finally:
        work_check_only.reset(token)
    assert checked.structuredContent["status"] == "validated"
    assert runtime.calls[-1][3].check_only is True
    assert work_check_only.get() is False


@pytest.mark.parametrize("injected", [
    {"_context": {}}, {"check_only": True}, {"words_from": "trusted"},
    {"moves": [{"move": "new", "title": "Call the bank", "_context": {}}]},
    {"moves": [{"move": "new", "title": "Call the bank", "words_from": "trusted"}]},
    {"moves": [{"move": "parts", "thing": "Call the bank", "parts": [
        {"title": "Find the number", "_context": {}}]}]},
])
def test_work_rejects_forged_context_and_non_public_arguments_before_runtime(injected):
    runtime, definitions = tools(actor=Actor("hermes-wa"), door=True)
    with pytest.raises(ToolError, match="Extra inputs"):
        asyncio.run(definitions["bc_work"].run({**_WORK, **injected}))
    assert runtime.calls == []


def test_weekly_tool_ideas_schema_is_opt_in_strict_boolean_and_frozen_text():
    from tests.adapters.test_hermes_brain import orientation_tools

    definitions = {tool.name: tool for tool in orientation_tools()}
    weekly = definitions["bc_weekly"]
    assert set(weekly.parameters["properties"]) == {"ideas"}
    assert weekly.parameters["properties"]["ideas"]["type"] == "boolean"
    assert weekly.parameters["properties"]["ideas"]["default"] is False
    assert weekly.parameters["additionalProperties"] is False
    for value in ("yes", 1, None):
        with pytest.raises(ToolError):
            asyncio.run(weekly.run({"ideas": value}))
    text = weekly.description
    assert "every goal and project once" in text and "unanswered For your review" in text
    assert "ideas=true" in text and "only on his yes" in text and "paraphrases" in text
    assert "14 days once" in text and "appointments" in text
    assert "Diablo or the morning review" in text
    assert "still to sort" not in text
    assert "bc_memory_change" not in text
    agenda = definitions["bc_agenda"]
    assert set(agenda.parameters["properties"]) == {"person", "exact"}
    assert "whose" in agenda.description and "needs.from" in agenda.description
    assert "ask_which" in agenda.description and "stable order" in agenda.description
    assert "Diablo" in agenda.description and "bc_memory_change" not in agenda.description
