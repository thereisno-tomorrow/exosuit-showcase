import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from adapters.hermes import plugin
from gremlin_town.contracts import Actor
from gremlin_town.mcp.brain_tools import build_brain_tools, build_orientation_tools
from gremlin_town.mcp.tools import build_tools


def _never_authorize():
    async def _raise():
        raise AssertionError("schema inspection cannot authorize")
    return _raise


def _assert_required_subset_of_properties(label, parameters):
    properties = set(parameters.get("properties", {}))
    required = set(parameters.get("required", []))
    missing = required - properties
    assert not missing, f"{label}: required names {sorted(missing)} are missing from properties"


def test_every_advertised_schema_lists_every_required_property():
    """Regression for the _slim_schema bug the orchestrator caught: stripping every dict
    key named "title" at every depth also stripped bc_memory_save's and
    bc_decision_record's own `title` PARAMETER out of `properties` (a name map, where
    keys are parameter names, not schema keywords) while `required` and the strict
    additionalProperties: false schema still demanded it - no client could construct a
    valid call. Covers every advertised schema on both channels: MCP Brain tools,
    orientation tools, GT tools, and every plugin.py schema copy (Hermes). calendar_removal
    adds bc_calendar_remove, which the hub registers only while calendar entries are on, and
    project_saves bc_project_save, only while [orientation] is on."""
    brain_tools = build_brain_tools(
        None, lambda: None, scopes_provider=lambda: (), authorize=_never_authorize(),
        door=True, calendar_removal=True,
    )
    orientation_tools = build_orientation_tools(
        object(), lambda: None, scopes_provider=lambda: (), authorize=_never_authorize()
    )
    gt_tools = build_tools(object(), lambda: Actor("chatgpt-web"))

    for tool in (*brain_tools, *orientation_tools, *gt_tools):
        _assert_required_subset_of_properties(tool.name, tool.parameters)
        advertised = set(tool.parameters.get("properties", {}))
        actual = set(tool.fn_metadata.arg_model.model_fields)
        bridge = {"size", "importance", "importance_reason", "sort", "first_step", "links"} if tool.name == "bc_memory_save" else set()
        assert advertised == actual - bridge, (
            f"{tool.name}: advertised properties {advertised} != arg_model fields {actual}"
        )

    for definition in plugin.ALL_TOOL_DEFINITIONS:
        _assert_required_subset_of_properties(definition.name, definition.parameters)

    live_by_name = {t.name: t for t in (*brain_tools, *orientation_tools)}
    for definition in plugin.BRAIN_TOOL_DEFINITIONS:
        live = live_by_name[definition.name]
        assert set(definition.parameters.get("properties", {})) == set(live.parameters.get("properties", {})), (
            f"{definition.name}: plugin.py's advertised properties drifted from build_brain_tools/"
            "build_orientation_tools's live output"
        )


def test_ten_tools_have_strict_schemas_and_accurate_hints():
    tools = build_tools(object(), lambda: Actor("chatgpt-web"))
    assert len(tools) == 10
    writes = {
        "gt_requests_create", "gt_requests_report", "gt_requests_reply",
        "gt_requests_cancel", "gt_requests_check",
    }
    for tool in tools:
        assert tool.parameters["additionalProperties"] is False
        assert "actor" not in tool.parameters["properties"]
        assert tool.annotations.readOnlyHint is (tool.name not in writes)
        assert tool.annotations.destructiveHint is (tool.name == "gt_requests_cancel")
        if tool.name in writes:
            assert "client_operation_id" in tool.parameters["required"]
            assert "same" in tool.description


def test_actor_injection_rejected_before_service_call():
    tool = build_tools(object(), lambda: Actor("chatgpt-web"))[0]
    with pytest.raises(ToolError, match="Extra inputs"):
        asyncio.run(tool.run({"actor": "operator"}))


def test_sdk_lists_exact_tools():
    server = FastMCP(tools=build_tools(object(), lambda: Actor("chatgpt-web")))
    assert {tool.name for tool in asyncio.run(server.list_tools())} == {
        "gt_participants_list", "gt_requests_create", "gt_requests_list",
        "gt_requests_read", "gt_requests_report", "gt_requests_reply",
        "gt_requests_cancel", "gt_requests_check", "gt_operations_get", "gt_events_list",
    }


def test_write_tool_text_states_the_receipt_fact_not_a_reporting_instruction():
    """ROA-32: ChatGPT flagged bc_memory_save/bc_memory_change as a "Suspicious
    Instruction" because their old text told the model how to report a result to the
    user. The diet replaces that instruction with a fact about what the tool returns -
    every write tool still says what a receipt means, but never "tell the user"."""
    async def never():
        raise AssertionError("schema inspection cannot authorize")
    tools = build_brain_tools(None, lambda: None, scopes_provider=lambda: (), authorize=never)
    writes = {
        "bc_continuity_start", "bc_continuity_append", "bc_continuity_close",
        "bc_decision_record", "bc_memory_save", "bc_memory_change",
    }
    receipt_fact = (
        "Returns a receipt: saved means status accepted or duplicate with remote_observed "
        "true, naming the record in record_id."
    )
    write_tools = {tool.name for tool in tools if tool.name in writes}
    assert write_tools == writes
    for tool in tools:
        if tool.name in writes:
            assert receipt_fact in tool.description
            assert "tell the user" not in tool.description.casefold()
            assert "say it was not saved" not in tool.description.casefold()


def test_memory_write_tools_state_brain_owns_nics_items():
    """Decision 20260926-brain-owns-nic-s-items-agent-local-memory-owns-o-ccc55ee893fa,
    encoded here (the SKILL.md half is tests/adapters/test_hermes_skill_content.py):
    Brain Home owns Nic's commitments, facts and confirmed preferences, and a clash with
    an agent's own memory goes to Brain."""
    async def never():
        raise AssertionError("schema inspection cannot authorize")
    tools = {
        tool.name: tool
        for tool in build_brain_tools(None, lambda: None, scopes_provider=lambda: (), authorize=never)
    }
    for name in ("bc_memory_save", "bc_memory_change"):
        # Worded as one test since 29 Sep 2026; bc_memory_search says to look there first.
        assert "Brain Home holds anything Nic would be hurt to lose or have done late" in tools[name].description
        assert "Brain wins" in tools[name].description
    assert "Before answering from your own memory" in tools["bc_memory_search"].description


def test_tool_text_states_running_notes_and_checked_done_exactly_once():
    """_RUNNING_NOTE and _DONE are stated once, where a caller actually reads them, not
    repeated on every tool that touches the same concept: a client reads every tool's
    description together, so restating a shared protocol on each one only adds bytes."""
    async def never():
        raise AssertionError("schema inspection cannot authorize")
    gt_tools = {tool.name: tool for tool in build_tools(object(), lambda: Actor("chatgpt-web"))}
    bc_tools = {
        tool.name: tool
        for tool in build_brain_tools(None, lambda: None, scopes_provider=lambda: (), authorize=never)
    }
    done_sentence = "not done until someone other than the doer checks it with requests_check"
    notes_sentence = "read its matching continuity note"
    running_note_sentence = "Start this task's note"
    close_sentence = "Close it only when the task is done and checked"

    assert done_sentence in gt_tools["gt_requests_list"].description
    assert notes_sentence in gt_tools["gt_requests_list"].description
    for name in ("gt_requests_read", "gt_requests_check"):
        assert done_sentence not in gt_tools[name].description

    assert running_note_sentence in bc_tools["bc_continuity_start"].description
    assert close_sentence in bc_tools["bc_continuity_start"].description
    for name in ("bc_continuity_append", "bc_continuity_recover", "bc_continuity_list", "bc_continuity_close"):
        assert running_note_sentence not in bc_tools[name].description
