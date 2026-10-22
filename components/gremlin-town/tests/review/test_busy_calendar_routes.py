"""FlowSavvy day controls stay behind their switch and the existing door identity."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport
from gremlin_town.sources.google.calendar_writer import CalendarWriteError
from gremlin_town.sources.result import UnavailableReason
from test_api_routes import _headers, write_config
from test_busy_calendar import Calendar, NOW


def runtime_for(tmp_path, monkeypatch, *, busy=True):
    import gremlin_town.api.review as api

    class Clock:
        value = NOW

        @staticmethod
        def now(_zone):
            return Clock.value

    monkeypatch.setattr(api, "datetime", Clock)
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    runtime.flowsavvy = replace(runtime.flowsavvy, enabled=True, busy_calendar=busy)
    runtime.calendar_writer = Calendar()
    runtime.calendar_writer.clock = Clock
    return runtime


def send(client, detail, participant="hermes-wa"):
    return client.post("/v1/moments/signals", headers=_headers(participant),
                       json={"kind": "day_change", "detail": json.dumps(detail)})


def old_unkeyed_finish(runtime):
    """An operation already stored by the previous local implementation."""
    from gremlin_town.review.busy_calendar import BusyChange, apply_busy_change
    from gremlin_town.sources.google.calendar_writer import EventSlot
    slot = EventSlot(NOW.replace(hour=16), (NOW + timedelta(days=1)).replace(hour=0))
    return apply_busy_change(runtime.review_why_log, runtime.calendar_writer, BusyChange("finish", slot),
                             caller="hermes-wa", now=NOW)


@pytest.mark.parametrize("detail", [
    {"ends_at": "16:00"},
    {"busy": {"start": "14:00", "end": "15:00"}},
    {"busy": {"start": "14:00", "end": "2026-10-02T15:00:00+08:00"}},
    {"busy": {"start": "2026-10-02T14:00:00+08:00", "end": "15:00"}},
])
def test_new_relative_time_needs_stable_key_before_any_signal_or_write(tmp_path, monkeypatch, detail):
    runtime = runtime_for(tmp_path, monkeypatch)
    try:
        with TestClient(runtime.app) as client:
            result = send(client, detail)
            assert result.status_code == 422
            assert result.json()["error"]["code"] == "INVALID_INPUT"
            assert "operation_id" in result.json()["error"]["message"]
            assert runtime.review_why_log.signals_on("day_change", NOW.date()) == ()
            assert runtime.calendar_writer.writes == []
    finally:
        runtime.close()


def test_old_unkeyed_ambiguous_retry_cannot_become_tomorrows_new_entry(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    runtime.calendar_writer.lose_reply = True
    try:
        assert old_unkeyed_finish(runtime)["status"] == "needs_check"
        runtime.calendar_writer.clock.value = NOW + timedelta(days=1)
        with TestClient(runtime.app) as client:
            result = send(client, {"ends_at": "16:00"})
            assert result.status_code == 422
            assert "operation_id" in result.json()["error"]["message"]
            assert len(runtime.calendar_writer.writes) == 1
            assert len(runtime.calendar_writer.entries) == 1
    finally:
        runtime.close()


def test_fully_dated_interval_can_derive_stable_key_across_midnight(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    runtime.calendar_writer.lose_reply = True
    detail = {"busy": {"start": "2026-10-02T14:00:00+08:00", "end": "2026-10-02T15:00:00+08:00"}}
    try:
        with TestClient(runtime.app) as client:
            assert send(client, detail).json()["calendar"]["status"] == "needs_check"
            runtime.calendar_writer.clock.value = NOW + timedelta(days=1)
            result = send(client, detail)
            assert result.status_code == 200
            assert result.json()["calendar"]["status"] == "blocked"
            assert len(runtime.calendar_writer.writes) == 1
    finally:
        runtime.close()


def test_flow_busy_reply_and_retry_skip_placement(tmp_path, monkeypatch):
    import gremlin_town.review.placement as placement
    monkeypatch.setattr(placement, "run_placement", lambda *a: (_ for _ in ()).throw(AssertionError("placement called")))
    runtime = runtime_for(tmp_path, monkeypatch)
    runtime.placement_context = object()
    try:
        with TestClient(runtime.app) as client:
            one = send(client, {"ends_at": "16:00", "operation_id": "first-finish"})
            two = send(client, {"ends_at": "16:00", "operation_id": "first-finish"})
            assert one.status_code == two.status_code == 200
            assert one.json()["calendar"]["status"] == "blocked"
            assert "placement" not in one.json()
            assert len(runtime.calendar_writer.writes) == 1
    finally:
        runtime.close()


def test_uncertain_finish_reconciles_after_its_start_without_another_write(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    runtime.calendar_writer.clock.value = NOW.replace(hour=15, minute=59)
    detail = {"ends_at": "16:00", "operation_id": "late-retry"}
    runtime.calendar_writer.lose_reply = True
    try:
        with TestClient(runtime.app) as client:
            first = send(client, detail)
            assert first.json()["calendar"]["status"] == "needs_check"
            runtime.calendar_writer.clock.value = NOW.replace(hour=16, minute=1)
            retried = send(client, detail)
            assert retried.status_code == 200
            assert retried.json()["calendar"]["status"] == "blocked"
            assert len(runtime.calendar_writer.writes) == 1
    finally:
        runtime.close()


def test_uncertain_finish_after_midnight_reuses_original_day(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    detail = {"ends_at": "16:00", "operation_id": "next-day-retry"}
    runtime.calendar_writer.lose_reply = True
    try:
        with TestClient(runtime.app) as client:
            first = send(client, detail)
            assert first.json()["calendar"]["status"] == "needs_check"
            runtime.calendar_writer.clock.value = NOW + timedelta(days=1)
            retried = send(client, detail)
            assert retried.status_code == 200
            assert retried.json()["calendar"]["start"].startswith("2026-10-02T16:00")
            assert len(runtime.calendar_writer.writes) == 1
    finally:
        runtime.close()


def test_unconfirmed_elapsed_finish_cannot_be_written_by_a_retry_or_new_request(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    detail = {"ends_at": "16:00", "operation_id": "missing-retry"}
    runtime.calendar_writer.fail_before_write = True
    try:
        with TestClient(runtime.app) as client:
            assert send(client, detail).json()["calendar"]["status"] == "needs_check"
            runtime.calendar_writer.clock.value = NOW.replace(hour=16, minute=1)
            retried = send(client, detail)
            assert retried.status_code == 200
            assert retried.json()["calendar"]["status"] == "needs_check"
            new = send(client, {"ends_at": "16:00", "operation_id": "brand-new-request"})
            assert new.status_code == 422
            assert len(runtime.calendar_writer.writes) == 1
    finally:
        runtime.close()


def test_prepared_but_never_attempted_finish_cannot_start_writing_after_its_time(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    detail = {"ends_at": "16:00", "operation_id": "initial-read-failed"}
    calendar = runtime.calendar_writer
    original_read = calendar.find_busy

    def failed_read(_key):
        raise CalendarWriteError(UnavailableReason.NETWORK_ERROR, "fictional failed read")

    calendar.find_busy = failed_read
    try:
        with TestClient(runtime.app) as client:
            assert send(client, detail).json()["calendar"]["status"] == "needs_check"
            calendar.find_busy = original_read
            calendar.clock.value = NOW.replace(hour=16, minute=1)
            assert send(client, detail).json()["calendar"]["status"] == "needs_check"
            assert calendar.writes == []
    finally:
        runtime.close()


def test_late_retry_must_still_match_original_input(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    runtime.calendar_writer.lose_reply = True
    try:
        with TestClient(runtime.app) as client:
            assert send(client, {"ends_at": "16:00", "operation_id": "exact-late-retry"}).status_code == 200
            runtime.calendar_writer.clock.value = NOW.replace(hour=16, minute=1)
            assert send(client, {"ends_at": "17:00", "operation_id": "exact-late-retry"}).status_code == 422
            assert len(runtime.calendar_writer.writes) == 1
    finally:
        runtime.close()


def test_same_day_retry_without_explicit_key_can_confirm_elapsed_finish(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    runtime.calendar_writer.lose_reply = True
    try:
        assert old_unkeyed_finish(runtime)["status"] == "needs_check"
        with TestClient(runtime.app) as client:
            runtime.calendar_writer.clock.value = NOW.replace(hour=16, minute=1)
            assert send(client, {"ends_at": "16:00"}).json()["calendar"]["status"] == "blocked"
            assert len(runtime.calendar_writer.writes) == 1
    finally:
        runtime.close()


def test_old_controls_and_other_callers_do_not_record_or_write(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    try:
        with TestClient(runtime.app) as client:
            for detail in ({"deep_focus_minutes": 0}, {"deep_focus_minutes": 180}, {"move": "fictional"}):
                assert send(client, detail).status_code == 422
            assert send(client, {"ends_at": "16:00"}, "local-codex").status_code == 403
            assert runtime.calendar_writer.writes == []
            assert runtime.review_why_log.signals_on("day_change", NOW.date()) == ()
    finally:
        runtime.close()


def test_busy_off_refuses_valid_control_without_recording_it(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch, busy=False)
    try:
        with TestClient(runtime.app) as client:
            assert send(client, {"ends_at": "16:00", "operation_id": "disabled-finish"}).status_code == 409
            assert runtime.calendar_writer.writes == []
            assert runtime.review_why_log.signals_on("day_change", NOW.date()) == ()
    finally:
        runtime.close()


def test_replay_only_checks_and_does_not_touch_calendar(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch)
    marker = tmp_path / "replay"
    marker.write_text("check only")
    runtime.brain_runtime = SimpleNamespace(_memory=SimpleNamespace(replay_marker=marker))
    try:
        with TestClient(runtime.app) as client:
            answer = send(client, {"ends_at": "16:00", "operation_id": "replay-finish"})
            assert answer.json()["status"] == "validated"
            assert runtime.calendar_writer.writes == []
            assert runtime.review_why_log.signals_on("day_change", NOW.date()) == ()
            runtime.brain_runtime = None
    finally:
        runtime.brain_runtime = None
        runtime.close()
