"""Busy controls in FlowSavvy mode; Google and all records are fictional."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gremlin_town.review.busy_calendar import BusyChangeError, parse_busy_change, apply_busy_change
from gremlin_town.review.why_log import WhyLog
from gremlin_town.sources.google.calendar_writer import BusyEntry, CalendarWriteError
from gremlin_town.sources.result import UnavailableReason

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 2, 12, tzinfo=SGT)


class Calendar:
    def __init__(self):
        self.entries = {}
        self.writes = []
        self.lose_reply = False
        self.fail_before_write = False

    def find_busy(self, key):
        return self.entries.get(key)

    def write_busy(self, *, key, slot, current, now):
        self.writes.append(slot)
        if self.fail_before_write:
            raise CalendarWriteError(UnavailableReason.NETWORK_ERROR, "fake failure")
        self.entries[key] = BusyEntry(key, slot, False, '"revision"')
        if self.lose_reply:
            raise CalendarWriteError(UnavailableReason.NETWORK_ERROR, "fake failure")
        return self.entries[key]


def change(detail, now=NOW):
    if "operation_id" not in detail:
        # New client instructions carry one stable identity, reused on retries.
        key = hashlib.sha256(json.dumps(detail, sort_keys=True).encode()).hexdigest()[:16]
        detail = {**detail, "operation_id": "fixture-" + key}
    return parse_busy_change(json.dumps(detail), now)


def apply(log, calendar, detail, now=NOW):
    return apply_busy_change(log, calendar, change(detail, now), caller="hermes-wa", now=now)


def test_finish_blocks_only_rest_of_singapore_day():
    request = change({"ends_at": "16:00", "words": "done at four"})
    assert request.slot.start == NOW.replace(hour=16)
    assert request.slot.end == datetime(2026, 10, 3, tzinfo=SGT)
    assert request.kind == "finish"


def test_explicit_intervals_preserve_times_and_accept_already_started_unexpired_range():
    request = change({"busy": {"start": "11:00", "end": "13:00"}})
    assert request.slot.start == NOW.replace(hour=11)
    assert request.slot.end == NOW.replace(hour=13)
    explicit = change({"busy": {"start": "2026-10-03T06:00:00Z", "end": "2026-10-03T07:00:00Z"}})
    assert explicit.slot.start == datetime(2026, 10, 3, 14, tzinfo=SGT)


@pytest.mark.parametrize("detail", [
    {"deep_focus_minutes": 0}, {"deep_focus_minutes": 180}, {"move": "fictional"},
    {"ends_at": None}, {"ends_at": "24:00"}, {"ends_at": "09:00"},
    {"busy": {"start": "14:00", "end": "14:00"}},
    {"busy": {"start": "14:00", "end": "13:00"}},
    {"busy": {"start": "10:00", "end": "11:00"}},
    {"busy": {"start": "2026-10-03T14:00", "end": "2026-10-03T15:00"}},
    {"busy": {"start": "14:00", "end": "15:00", "extra": 1}},
    {"ends_at": "16:00", "busy": {"start": "14:00", "end": "15:00"}},
    {"words": "hello"}, {"ends_at": "16:00", "words": 2},
])
def test_invalid_or_removed_controls_cannot_write(detail):
    with pytest.raises(BusyChangeError):
        change(detail)


def test_repeated_operation_survives_restart_without_duplicate(tmp_path):
    calendar = Calendar()
    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    first = apply(log, calendar, {"ends_at": "16:00"})
    assert first["status"] == "blocked"
    log.close()
    log = WhyLog(path)
    try:
        assert apply(log, calendar, {"ends_at": "16:00"}, NOW + timedelta(minutes=1)) == first
        assert len(calendar.writes) == 1
    finally:
        log.close()


def test_new_finish_moves_same_owned_entry_and_old_replay_cannot_move_it_back(tmp_path):
    log, calendar = WhyLog(tmp_path / "why.sqlite3"), Calendar()
    try:
        first = apply(log, calendar, {"ends_at": "16:00"})
        apply(log, calendar, {"ends_at": "17:00"})
        assert len(calendar.entries) == 1
        assert next(iter(calendar.entries.values())).slot.start.hour == 17
        assert apply(log, calendar, {"ends_at": "16:00"}) == first
        assert len(calendar.writes) == 2
    finally:
        log.close()


def test_unknown_write_reconciles_by_read_only(tmp_path):
    log, calendar = WhyLog(tmp_path / "why.sqlite3"), Calendar()
    try:
        calendar.lose_reply = True
        assert apply(log, calendar, {"ends_at": "16:00"})["status"] == "needs_check"
        assert apply(log, calendar, {"ends_at": "16:00"})["status"] == "blocked"
        assert len(calendar.writes) == 1
    finally:
        log.close()


def test_unknown_write_that_is_absent_is_held_without_blind_retry_or_new_change(tmp_path):
    log, calendar = WhyLog(tmp_path / "why.sqlite3"), Calendar()
    try:
        calendar.fail_before_write = True
        assert apply(log, calendar, {"ends_at": "16:00"})["status"] == "needs_check"
        assert apply(log, calendar, {"ends_at": "16:00"})["status"] == "needs_check"
        assert apply(log, calendar, {"ends_at": "17:00"})["status"] == "needs_check"
        assert len(calendar.writes) == 1
    finally:
        log.close()


def test_guest_entry_is_left_alone(tmp_path):
    log, calendar = WhyLog(tmp_path / "why.sqlite3"), Calendar()
    try:
        apply(log, calendar, {"ends_at": "16:00"})
        key = next(iter(calendar.entries))
        calendar.entries[key] = replace(calendar.entries[key], has_guests=True)
        result = apply(log, calendar, {"ends_at": "17:00"})
        assert result["status"] == "needs_check"
        assert len(calendar.writes) == 1
    finally:
        log.close()


def test_config_switch_is_separate_off_and_schema_registered(tmp_path):
    from gremlin_town.flowsavvy.settings import FlowSavvySettings
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert FlowSavvySettings().busy_calendar is False
    assert "busy_calendar" in RUNTIME_CONFIG_SCHEMA["flowsavvy"]
    with pytest.raises(ValueError):
        FlowSavvySettings.from_table({"busy_calendar": "yes"}, base=tmp_path)


def test_new_instruction_can_return_to_an_earlier_finish_time(tmp_path):
    log, calendar = WhyLog(tmp_path / "why.sqlite3"), Calendar()
    try:
        for operation, clock in (("finish-one", "16:00"), ("finish-two", "17:00"), ("finish-three", "16:00")):
            assert apply(log, calendar, {"operation_id": operation, "ends_at": clock})["status"] == "blocked"
        assert len(calendar.writes) == 3
        assert next(iter(calendar.entries.values())).slot.start.hour == 16
    finally:
        log.close()


def test_operation_key_conflict_cannot_change_frozen_intent(tmp_path):
    from gremlin_town.review.why_log import WhyLogError
    log, calendar = WhyLog(tmp_path / "why.sqlite3"), Calendar()
    try:
        apply(log, calendar, {"operation_id": "finish-one", "ends_at": "16:00"})
        with pytest.raises(WhyLogError):
            apply(log, calendar, {"operation_id": "finish-one", "ends_at": "17:00"})
        assert len(calendar.writes) == 1
    finally:
        log.close()
