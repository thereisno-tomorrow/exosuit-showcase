"""Confirmed owner saves wake scheduling; no guessed lengths in the new mode."""
from dataclasses import replace

import pytest

from gremlin_town.mcp.brain_runtime import BrainRuntimeError, WorkRequest
from test_work_door import setup_door, call, records


def change(**fields):
    return WorkRequest("synthetic-schedule-save", "Sort receipts for 30 minutes", "owner", "direct",
                       ({"move": "new", "title": "Sort receipts", **fields},))


def test_save_wakes_only_after_confirmed_commit_and_retry_keeps_one_record(tmp_path):
    provider, fake, _, log = setup_door(tmp_path)
    observed = []
    provider.schedule_wake = lambda: observed.append(len(records(fake)))
    provider.schedule_lengths = True
    try:
        request = change(rough_minutes=30, length_is_nics=True)
        assert call(provider, replace(request, check_only=True))["status"] == "validated"
        assert observed == [] and records(fake) == []
        assert call(provider, request)["schedule"]["status"] == "pending"
        assert observed == [1]
        assert call(provider, request)["status"] == "duplicate"
        assert observed == [1, 1] and len(records(fake)) == 1
    finally:
        log.close()


def test_wake_failure_reports_saved_truth_without_private_diagnostic(tmp_path):
    provider, fake, _, log = setup_door(tmp_path)
    def failure():
        raise OSError("synthetic-private-path")
    provider.schedule_wake = failure
    try:
        answer = call(provider, change())
        assert answer["status"] == "accepted" and len(records(fake)) == 1
        assert answer["schedule"]["status"] == "wake_failed"
        assert "synthetic-private-path" not in str(answer)
    finally:
        log.close()


@pytest.mark.parametrize("nested", [False, True])
def test_schedule_length_mode_refuses_estimates_before_any_save(tmp_path, nested):
    provider, fake, _, log = setup_door(tmp_path)
    provider.schedule_lengths = True
    request = change(rough_minutes=30)
    if nested:
        request = replace(request, moves=({"move": "new", "title": "Sort receipts"},
            {"move": "parts", "thing": "#1", "parts": [{"title": "File receipts", "rough_minutes": 30}]}))
    try:
        with pytest.raises(BrainRuntimeError, match="length.*Nic"):
            call(provider, request)
        assert records(fake) == []
    finally:
        log.close()


def test_length_mode_off_preserves_existing_estimates(tmp_path):
    provider, fake, _, log = setup_door(tmp_path)
    try:
        assert call(provider, change(rough_minutes=30))["status"] == "accepted"
        assert records(fake)[0].data["timing"]["duration_minutes"] == 30
        provider.schedule_lengths = True
        assert call(provider, change(rough_minutes=30))["status"] == "duplicate"
    finally:
        log.close()


def test_unsized_save_note_does_not_ask_for_length_in_schedule_mode():
    from gremlin_town.mcp.github_brain_provider import _length_note
    from types import SimpleNamespace
    record = SimpleNamespace(kind="item", state="open",
                             data={"level": "accepted", "size": "task", "block": "admin"})
    assert _length_note(record, asked=False, saving=True)["status"] == "needs_length"
    assert _length_note(record, asked=False, saving=True, schedule_mode=True) is None


def test_length_flow_setting_is_off_and_rejects_bad_or_unattached_switch(tmp_path):
    from gremlin_town.flowsavvy.settings import FlowSavvySettings
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert not FlowSavvySettings.from_table(None, base=tmp_path).length_flow
    assert "length_flow" in RUNTIME_CONFIG_SCHEMA["flowsavvy"]
    for value in (True, "true"):
        with pytest.raises(ValueError, match="length_flow"):
            FlowSavvySettings.from_table({"length_flow": value}, base=tmp_path)


@pytest.mark.parametrize("repetitions", [0, 39, 40, 120])
def test_assembled_memory_bridge_suppresses_prompt_and_reports_wake(tmp_path, repetitions):
    import anyio
    from test_brain_composition import _settings, LANE_B
    from gremlin_town.mcp.brain_assembly import build_brain_runtime
    from gremlin_town.mcp.brain_runtime import BrainCaller, MemorySaveRequest, OwnerSelector
    remote = LANE_B.FakeGitHub()
    wakes = []
    async def exercise():
        runtime = build_brain_runtime(_settings(tmp_path), github_transport=remote,
            schedule_wake=lambda: wakes.append(True), schedule_lengths=True)
        try:
            saved = await runtime.save_memory(BrainCaller("chatgpt-web", frozenset({"bc:read", "bc:write"})),
                OwnerSelector("personal"), MemorySaveRequest(
                    operation_id="synthetic-unsized-capture", kind="item", title="Sort receipts",
                    body="Sort receipts. " + "Synthetic note. " * repetitions,
                    source_reference="mcp-request:synthetic-unsized-capture",
                    said_by="owner", came_by="direct", words="Sort receipts. " + "Synthetic note. " * repetitions,
                    level="accepted", block="admin"))
            assert saved.status == "accepted"
            assert getattr(saved, "length", None) is None
            assert saved.record["schedule"]["status"] == "pending"
        finally:
            await runtime.aclose()
    anyio.run(exercise)
    assert wakes == [True]


def test_schedule_length_claim_requires_the_actual_duration_in_owner_words(tmp_path):
    provider, fake, _, log = setup_door(tmp_path)
    provider.schedule_lengths = True
    try:
        with pytest.raises(BrainRuntimeError, match="length.*Nic"):
            call(provider, replace(change(rough_minutes=30, length_is_nics=True), message="Sort receipts"))
        assert records(fake) == []
    finally:
        log.close()


@pytest.mark.parametrize("value", [[], {}, True])
def test_direct_host_malformed_length_has_clear_refusal(tmp_path, value):
    provider, fake, _, log = setup_door(tmp_path)
    provider.schedule_lengths = True
    try:
        with pytest.raises(BrainRuntimeError, match="length.*Nic"):
            call(provider, change(rough_minutes=value, length_is_nics=True))
        assert records(fake) == []
    finally:
        log.close()


@pytest.mark.parametrize("enabled,failed", [(False, False), (True, False), (True, True)])
def test_long_task_save_keeps_original_length_and_reports_split_only_when_schedule_enabled(tmp_path, enabled, failed):
    provider, fake, _, log = setup_door(tmp_path)
    wakes = []
    def wake():
        wakes.append(True)
        if failed:
            raise OSError("Synthetic private error")
    if enabled:
        provider.schedule_wake = wake
    request = replace(change(rough_minutes=1440, length_is_nics=True), message="Sort receipts for 24 hours")
    try:
        assert call(provider, replace(request, check_only=True))["status"] == "validated"
        assert not wakes and not records(fake)
        result = call(provider, request)
        assert result["status"] == "accepted"
        assert records(fake)[0].data["timing"]["duration_minutes"] == 1440
        if enabled:
            assert result["schedule"]["issue"] == "needs_split"
            assert result["schedule"]["status"] == ("wake_failed" if failed else "needs_split")
            assert "split this into shorter tasks" in result["schedule"]["text"]
            assert "Synthetic private error" not in str(result)
            assert call(provider, request)["schedule"]["issue"] == "needs_split"
            assert len(records(fake)) == 1
        else:
            assert "schedule" not in result and "split this" not in str(result)
    finally:
        log.close()


def test_canonical_routine_save_does_not_ask_to_split(tmp_path):
    from brain_core.routines import is_routine
    provider, fake, _, log = setup_door(tmp_path)
    provider.schedule_wake = lambda: None
    request = replace(change(rough_minutes=1440, length_is_nics=True, repeat="daily"),
                      message="Sort receipts daily for 24 hours")
    try:
        result = call(provider, request)
        assert is_routine(records(fake)[0])
        assert result["schedule"].get("issue") is None
        assert "split this" not in str(result)
    finally:
        log.close()
