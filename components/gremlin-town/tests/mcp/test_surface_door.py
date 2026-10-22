from dataclasses import replace

import anyio
import pytest

from gremlin_town.mcp.brain_runtime import BrainRuntimeError
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.mcp.surface_door import bind_owner_tap, configured_code_callers
from test_work_door import WORK, call, records, request, setup_door


def test_surfaces_are_independently_off_and_typed():
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert {"review_card", "dashboard"} <= RUNTIME_CONFIG_SCHEMA["work"]
    assert configured_code_callers({}) == frozenset()
    assert configured_code_callers({"review_card": True}) == {"system:review-card"}
    assert configured_code_callers({"dashboard": True}) == {"system:dashboard"}
    for value in ("true", 1, None):
        with pytest.raises(ValueError):
            configured_code_callers({"dashboard": value})


def test_system_retry_namespace_is_distinct():
    from brain_core.github_storage import memory_operation_record_suffix, GitHubStorageError
    assert memory_operation_record_suffix("fixture", caller="system:dashboard") != memory_operation_record_suffix(
        "fixture", caller="dashboard")
    for bad in ("system:", "system:system:dashboard", "System:dashboard", "system:../dashboard"):
        with pytest.raises(GitHubStorageError):
            memory_operation_record_suffix("fixture", caller=bad)


@pytest.mark.parametrize("caller", ["system:review-card", "system:dashboard"])
def test_registered_tap_uses_normal_door_and_replays(tmp_path, caller):
    provider, fake, pins, log = setup_door(tmp_path)
    provider.code_callers = frozenset({caller})
    change = request()
    try:
        with bind_owner_tap(caller, WORK.owner_id, change):
            saved = call(provider, change, caller)
            head = fake.branch
            replay = call(provider, change, caller)
        assert saved["status"] == "accepted" and replay["status"] == "duplicate"
        assert fake.branch == head
        assert records(fake)[0].source["recorded_by"] == caller
    finally:
        log.close()


@pytest.mark.parametrize("case", ["disabled", "missing_tap", "wrong_owner", "substitution", "not_owner", "unknown"])
def test_system_name_never_substitutes_for_owner_auth(tmp_path, case):
    provider, fake, pins, log = setup_door(tmp_path)
    caller = "system:unknown" if case == "unknown" else "system:dashboard"
    provider.code_callers = frozenset() if case == "disabled" else frozenset({caller})
    change = request()
    head = fake.branch
    async def invoke():
        with bind_write_attribution(caller):
            return await provider.work(WORK, change)
    try:
        with pytest.raises(BrainRuntimeError) as error:
            if case in {"missing_tap", "unknown"}:
                anyio.run(invoke)
            else:
                bound = replace(change, operation_id="other") if case == "substitution" else change
                if case == "not_owner":
                    change = bound = replace(change, said_by="other", speaker="someone")
                with bind_owner_tap(caller, "other-owner" if case == "wrong_owner" else WORK.owner_id, bound):
                    anyio.run(invoke)
        assert error.value.code == "ACCESS_DENIED"
        assert fake.branch == head and not pins
    finally:
        log.close()


def test_authenticated_tap_does_not_bypass_move_validation(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    provider.code_callers = frozenset({"system:dashboard"})
    change = replace(request(), moves=({"move": "made_up"},))
    head = fake.branch
    try:
        with bind_owner_tap("system:dashboard", WORK.owner_id, change), pytest.raises(BrainRuntimeError):
            call(provider, change, "system:dashboard")
        assert fake.branch == head
    finally:
        log.close()
