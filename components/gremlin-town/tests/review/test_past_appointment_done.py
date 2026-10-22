from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from gremlin_town.review.calendar_entries import follow
from gremlin_town.sources.google.calendar_writer import CalendarEntry, EventSlot
from test_run import make_item

NOW = datetime.fromisoformat("2026-10-02T06:00:00+08:00")


class Calendar:
    def __init__(self, end):
        self.entry = CalendarEntry("fixture-event", "brn_fixture", EventSlot(end - timedelta(minutes=30), end), False)
        self.calls = []

    def find(self, marker):
        self.calls.append("find")
        return self.entry


@pytest.mark.parametrize("state,offset,enabled,expected", [
    ("done", -1, True, "kept"), ("done", 1, True, "ask_remove"),
    ("dropped", -1, True, "ask_remove"), ("done", -1, False, "ask_remove"),
])
def test_past_done_keeps_calendar_without_question(state, offset, enabled, expected):
    record = replace(make_item(id="brn_fixture", title="Fixture appointment"), state=state)
    writer = Calendar(NOW + timedelta(days=offset))
    result = follow(writer, record, changed=True, keep_past_done=enabled, now=NOW)
    assert result["status"] == expected
    assert ("question" not in result) == (expected == "kept")
    assert writer.calls == ["find"]


def test_setting_defaults_off_and_requires_boolean():
    from gremlin_town.review.settings import ReviewSettings, ReviewSettingsError
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert not ReviewSettings.from_table(None).past_appointment_done
    assert ReviewSettings.from_table({"past_appointment_done": True}).past_appointment_done
    assert "past_appointment_done" in RUNTIME_CONFIG_SCHEMA["review"]
    with pytest.raises(ReviewSettingsError, match="past_appointment_done"):
        ReviewSettings.from_table({"past_appointment_done": "yes"})


@pytest.mark.parametrize("end_day,expected", [("2026-10-01", "kept"), ("2026-10-02", "kept"),
                                            ("2026-10-03", "ask_remove")])
def test_all_day_end_is_exclusive_singapore_midnight(end_day, expected):
    end = date.fromisoformat(end_day)
    writer = Calendar(NOW)
    writer.entry = replace(writer.entry, slot=EventSlot(end - timedelta(days=1), end))
    result = follow(writer, replace(make_item(id="brn_fixture", title="Fixture appointment"), state="done"),
                    changed=True, keep_past_done=True, now=NOW)
    assert result["status"] == expected
    assert writer.calls == ["find"]


def test_keep_acknowledgment_survives_why_log_restart(tmp_path):
    from gremlin_town.review.why_log import WhyLog
    key = dict(owner_locator="vault://fixture", owner_id="fixture", caller="system:dashboard",
               operation_id="fixture", operation_hash="a" * 64, brain_commit="b" * 40)
    value = follow(Calendar(NOW - timedelta(days=1)),
                   replace(make_item(id="brn_fixture", title="Fixture appointment"), state="done"),
                   changed=True, keep_past_done=True, now=NOW)
    log = WhyLog(tmp_path / "why.db")
    assert log.record_calendar_effect(**key, calendar=value) == value
    log.close()
    log = WhyLog(tmp_path / "why.db")
    assert log.calendar_effect(**key) == value
    log.close()
