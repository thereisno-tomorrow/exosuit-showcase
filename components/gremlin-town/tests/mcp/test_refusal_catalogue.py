"""The door and every mapped refusal tell an agent how to fix its request."""
import pytest

from brain_core.errors import BrainError, ErrorCode
from gremlin_town.mcp.brain_runtime import BrainRuntimeError, work_caller_allowed
from gremlin_town.mcp.brain_tools import _mapped_error


@pytest.mark.parametrize("code", [
    "ACCESS_DENIED", "brain.invalid_argument", "brain.policy.excluded", "brain.contract.invalid",
    "brain.capability.unavailable", "brain.record.write_failed", "brain.record.exists", "brain.conflict",
    "brain.record.changed", "brain.not_found", "brain.capacity.exceeded", "brain.record.corrupt",
    "brain.record.needs_owner_words", "brain.internal_error",
])
def test_runtime_refusals_have_field_and_fix(code):
    result = _mapped_error(BrainRuntimeError(code, "fixture refusal"))
    assert result.structuredContent["error"]["fix"]
    assert result.structuredContent["error"]["field"]


@pytest.mark.parametrize("caller", ["chatgpt-web", "claude-web", "fleet-boss-grok", "local-codex",
                                      "system:unknown", None])
def test_gate_refuses_tier_b_and_unregistered_code(caller):
    assert not work_caller_allowed(caller)


def test_configured_door_is_independent_of_review_delivery():
    assert work_caller_allowed("hermes-wa")
    assert not work_caller_allowed("hermes-wa", door_participant="diablo-next")
    assert work_caller_allowed("diablo-next", door_participant="diablo-next")
    assert not work_caller_allowed("system:lark-bridge", door_participant="system:lark-bridge")


def test_core_refusal_preserves_field_and_hint():
    result = _mapped_error(BrainError(ErrorCode.INVALID_ARGUMENT, "bad date",
        details={"field": "moves.0.when"}, hint="quote the date phrase"))
    assert result.structuredContent["error"]["field"] == "moves.0.when"
    assert result.structuredContent["error"]["fix"] == "quote the date phrase"


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_core_refusal_has_a_fix(code):
    result = _mapped_error(BrainError(code, "fixture refusal", details={"field": "moves"}))
    error = result.structuredContent["error"]
    assert error["fix"] and error["field"]
    assert error["code"] in (str(code), "ACCESS_DENIED", "brain.capability.unavailable")


def test_every_http_dispatched_operation_refuses_empty_arguments_with_fix(hub):
    from gremlin_town.mcp.brain_tools import build_brain_tools
    from tests.integration.test_brain_agent_http import post
    client, runtime, tokens, remote, _ = hub
    async def never():
        raise AssertionError("metadata lookup must not execute a tool")
    names = {tool.name.removeprefix("bc_").replace("_", ".")
        for tool in build_brain_tools(runtime.brain_runtime, lambda: None, door=True,
            scopes_provider=lambda: (), authorize=never)}
    names.update(("calendar.remove", "project.save"))
    head = remote.branch
    for operation in names:
        # Read/list defaults are valid; malformed scope still exercises their refusal path.
        response = post(client, tokens["alice"], operation, {"scope": "not-an-owner"})
        assert response.status_code >= 400, (operation, response.text)
        assert response.json()["error"]["fix"], (operation, response.text)
    assert remote.branch == head


# The production-composed loopback fixture stays in its original integration module.
from tests.integration.test_brain_agent_http import hub
