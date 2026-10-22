"""Exact busy-entry writes over an in-process HTTP fake, never live Google."""
import json
from dataclasses import replace

import httpx
import pytest

from gremlin_town.sources.google.calendar_writer import BusyEntry, CalendarWriteError, EventSlot
from test_google_calendar_writer import writer, query, SEVEN_PM

KEY = "a" * 64
NOW = SEVEN_PM.start.replace(hour=12)


def event(**changed):
    body = {"id": "b" + KEY, "etag": '"revision"', "summary": "Busy", "transparency": "opaque",
            "reminders": {"useDefault": False},
            "extendedProperties": {"private": {"exosuit_busy": KEY}}, **SEVEN_PM.google_times()}
    return {**body, **changed}


def test_exact_owned_busy_insert_never_has_guests_or_notifications():
    made, calls = writer(httpx.Response(200, json=event()))
    result = made.write_busy(key=KEY, slot=SEVEN_PM, current=None, now=NOW)
    assert result == BusyEntry(KEY, SEVEN_PM, False, '"revision"')
    [request] = calls
    assert request.method == "POST"
    assert query(request) == {"sendUpdates": "none"}
    body = json.loads(request.content)
    assert body == {key: value for key, value in event().items() if key != "etag"}
    assert "attendees" not in body
    assert body["reminders"] == {"useDefault": False}
    assert set(body["extendedProperties"]["private"]) == {"exosuit_busy"}


def test_find_busy_uses_deterministic_id_and_refuses_wrong_ownership():
    made, calls = writer(httpx.Response(200, json=event(extendedProperties={"private": {"exosuit": "fictional"}})))
    with pytest.raises(CalendarWriteError):
        made.find_busy(KEY)
    assert calls[0].method == "GET"
    assert calls[0].url.path.endswith("/b" + KEY)


@pytest.mark.parametrize("changed", [
    {"status": "cancelled"}, {"transparency": "transparent"},
    {"extendedProperties": {"private": {"exosuit_busy": KEY, "exosuit_day": "2026-10-02"}}},
    {"extendedProperties": "bad response"},
    {"start": {"dateTime": "2026-10-02T19:00:00"}},
])
def test_unusable_or_foreign_entry_is_not_claimed(changed):
    made, _ = writer(httpx.Response(200, json=event(**changed)))
    with pytest.raises(CalendarWriteError):
        made.find_busy(KEY)


def test_move_is_conditional_and_guest_entry_is_never_changed():
    current = BusyEntry(KEY, SEVEN_PM, False, '"revision"')
    made, calls = writer(httpx.Response(200, json=event()))
    made.write_busy(key=KEY, slot=SEVEN_PM, current=current, now=NOW)
    assert calls[0].method == "PATCH"
    assert calls[0].headers["If-Match"] == '"revision"'
    assert query(calls[0]) == {"sendUpdates": "none"}
    assert json.loads(calls[0].content) == SEVEN_PM.google_times()
    with pytest.raises(ValueError):
        made.write_busy(key=KEY, slot=SEVEN_PM, current=replace(current, has_guests=True), now=NOW)
    assert len(calls) == 1


def test_different_returned_time_is_not_success():
    made, _ = writer(httpx.Response(200, json=event(end={"dateTime": "2026-10-02T20:00:00+08:00"})))
    with pytest.raises(CalendarWriteError):
        made.write_busy(key=KEY, slot=SEVEN_PM, current=None, now=NOW)


def test_invalid_time_is_refused_before_any_request():
    made, calls = writer()
    for slot in (EventSlot(SEVEN_PM.end, SEVEN_PM.start), EventSlot(NOW, NOW)):
        with pytest.raises(ValueError):
            made.write_busy(key=KEY, slot=slot, current=None, now=NOW)
    assert calls == []


def test_precondition_failure_is_reported_without_unconditional_retry():
    made, calls = writer(httpx.Response(412))
    with pytest.raises(CalendarWriteError):
        made.write_busy(key=KEY, slot=SEVEN_PM, current=BusyEntry(KEY, SEVEN_PM, False, '"old"'), now=NOW)
    assert len(calls) == 1


@pytest.mark.parametrize("response", [httpx.Response(200, json={}), httpx.Response(200, json=[]),
                                      httpx.Response(204), httpx.Response(410)])
def test_only_explicit_not_found_is_an_absent_busy_entry(response):
    made, calls = writer(response)
    with pytest.raises(CalendarWriteError):
        made.find_busy(KEY)
    assert [call.method for call in calls] == ["GET"]


def test_explicit_not_found_is_an_absent_busy_entry():
    made, calls = writer(httpx.Response(404))
    assert made.find_busy(KEY) is None
    assert [call.method for call in calls] == ["GET"]


def test_unexpected_successful_read_never_allows_a_write(tmp_path):
    from gremlin_town.review.busy_calendar import apply_busy_change, parse_busy_change
    from gremlin_town.review.why_log import WhyLog
    made, calls = writer(httpx.Response(200, json={}))
    log = WhyLog(tmp_path / "why.sqlite3")
    try:
        change = parse_busy_change('{"ends_at":"16:00","operation_id":"read-surprise"}', NOW)
        result = apply_busy_change(log, made, change, caller="hermes-wa", now=NOW)
        assert result["status"] == "needs_check"
        assert [call.method for call in calls] == ["GET"]
    finally:
        log.close()
