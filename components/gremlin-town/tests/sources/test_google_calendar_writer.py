"""CalendarWriter (N8; step E spec §5): the exact find, add, move and remove requests over a fake
transport, found only by the marker, never with guests, and never a request while disabled."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from gremlin_town.sources.google.auth import GoogleAuthError
from gremlin_town.sources.google.calendar_writer import (
    CalendarEntry,
    CalendarWriteError,
    CalendarWriter,
    EventSlot,
)
from gremlin_town.sources.google.settings import GoogleSettings
from gremlin_town.sources.result import UnavailableReason

SGT = timezone(timedelta(hours=8))
ACCESS_TOKEN = "test-access-token"
RECORD_ID = "brn_20260928_101500_ab12"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SEVEN_PM = EventSlot(datetime(2026, 10, 2, 19, 0, tzinfo=SGT), datetime(2026, 10, 2, 19, 30, tzinfo=SGT))


class FixedToken:
    def token(self) -> str:
        return ACCESS_TOKEN


def settings(enabled: bool = True) -> GoogleSettings:
    if not enabled:
        return GoogleSettings.from_table(None)
    return GoogleSettings.from_table({"enabled": True, "account": "nic@example.test", "credential_env": "X"})


def writer(*responses, token=None, enabled: bool = True):
    calls: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert remaining, f"unexpected request: {request.method} {request.url}"
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    made = CalendarWriter(settings(enabled), token or FixedToken(), transport=httpx.MockTransport(handler))
    return made, calls


def event(event_id="evt-1", *, marker_id=RECORD_ID, description=None, start=None, end=None, **extra):
    body = {
        "id": event_id,
        "status": "confirmed",
        "summary": "Dentist",
        "description": f"exosuit:{marker_id}" if description is None else description,
        "start": start or {"dateTime": "2026-10-02T19:00:00+08:00", "timeZone": "Asia/Singapore"},
        "end": end or {"dateTime": "2026-10-02T19:30:00+08:00", "timeZone": "Asia/Singapore"},
        "extendedProperties": {"private": {"exosuit": marker_id}},
    }
    body.update(extra)
    return body


def listed(*events):
    return httpx.Response(200, json={"kind": "calendar#events", "items": list(events)})


def entry(event_id="evt-1", *, slot=SEVEN_PM, has_guests=False) -> CalendarEntry:
    return CalendarEntry(event_id=event_id, marker_id=RECORD_ID, slot=slot, has_guests=has_guests)


def query(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.url.query))


# --- insert ------------------------------------------------------------------

def test_insert_sends_exactly_the_title_slot_marker_and_no_guests():
    made, calls = writer(httpx.Response(200, json=event()))
    added = made.insert(title="Dentist", slot=SEVEN_PM, marker_id=RECORD_ID)

    assert added == CalendarEntry("evt-1", RECORD_ID, SEVEN_PM, has_guests=False)
    [request] = calls
    assert request.method == "POST"
    assert str(request.url) == EVENTS_URL + "?sendUpdates=none"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert json.loads(request.content) == {
        "summary": "Dentist",
        "description": f"exosuit:{RECORD_ID}",
        "start": {"dateTime": "2026-10-02T19:00:00+08:00", "timeZone": "Asia/Singapore"},
        "end": {"dateTime": "2026-10-02T19:30:00+08:00", "timeZone": "Asia/Singapore"},
        "extendedProperties": {"private": {"exosuit": RECORD_ID}},
    }
    assert "attendees" not in json.loads(request.content)


# --- find --------------------------------------------------------------------

def test_find_asks_for_the_marker_property_and_returns_the_marked_entry():
    made, calls = writer(listed(event()))
    found = made.find(RECORD_ID)

    assert found == CalendarEntry("evt-1", RECORD_ID, SEVEN_PM, has_guests=False)
    [request] = calls
    assert request.method == "GET"
    assert request.url.copy_with(query=None) == httpx.URL(EVENTS_URL)
    assert query(request) == {"privateExtendedProperty": f"exosuit={RECORD_ID}", "maxResults": "10"}
    assert request.content == b""


def test_find_accepts_the_marker_among_nics_own_notes():
    made, _ = writer(listed(event(description=f"Bring the forms.\n\nexosuit:{RECORD_ID}<br>see you")))
    assert made.find(RECORD_ID) is not None


@pytest.mark.parametrize("found", [
    event(status="cancelled"),
    # The property alone: someone removed the description's marker, so it isn't found by it.
    event(description="Bring the forms."),
    # A longer record ID that only starts the same way is a different marker.
    event(description=f"exosuit:{RECORD_ID}ff"),
    # The description alone, as a tap-to-add link writes it: an entry Nic added himself.
    {key: value for key, value in event().items() if key != "extendedProperties"},
    event(extendedProperties={"private": {"exosuit": "brn_20260928_101500_cd34"}}),
])
def test_find_ignores_anything_not_marked_as_exosuits_own(found):
    made, _ = writer(listed(found))
    assert made.find(RECORD_ID) is None


def test_find_with_no_entries_is_none():
    made, _ = writer(listed())
    assert made.find(RECORD_ID) is None


def test_find_reads_an_all_day_entry_and_its_guests():
    guest = event(
        start={"date": "2026-10-03"}, end={"date": "2026-10-04"},
        attendees=[{"email": "nic@example.test", "self": True}, {"email": "sam@example.test"}],
    )
    made, _ = writer(listed(guest))
    found = made.find(RECORD_ID)
    assert found == CalendarEntry("evt-1", RECORD_ID, EventSlot(date(2026, 10, 3), date(2026, 10, 4)), True)
    assert found.slot.all_day is True


def test_nic_alone_on_the_guest_list_is_not_a_guest():
    made, _ = writer(listed(event(attendees=[{"email": "nic@example.test", "self": True}])))
    assert made.find(RECORD_ID).has_guests is False


# --- move --------------------------------------------------------------------

def test_move_patches_only_the_times():
    eight_pm = EventSlot(datetime(2026, 10, 2, 20, 0, tzinfo=SGT), datetime(2026, 10, 2, 20, 30, tzinfo=SGT))
    moved_event = event(
        start={"dateTime": "2026-10-02T20:00:00+08:00"}, end={"dateTime": "2026-10-02T20:30:00+08:00"},
    )
    made, calls = writer(httpx.Response(200, json=moved_event))
    moved = made.move(entry(), eight_pm)

    assert moved.slot == eight_pm
    [request] = calls
    assert request.method == "PATCH"
    assert str(request.url) == EVENTS_URL + "/evt-1?sendUpdates=none"
    assert json.loads(request.content) == {
        "start": {"dateTime": "2026-10-02T20:00:00+08:00", "timeZone": "Asia/Singapore"},
        "end": {"dateTime": "2026-10-02T20:30:00+08:00", "timeZone": "Asia/Singapore"},
    }


def test_moving_a_timed_entry_to_all_day_clears_its_times():
    all_day = EventSlot(date(2026, 10, 3), date(2026, 10, 4))
    made, calls = writer(httpx.Response(200, json=event(start={"date": "2026-10-03"}, end={"date": "2026-10-04"})))
    made.move(entry(), all_day)
    assert json.loads(calls[0].content) == {
        "start": {"date": "2026-10-03", "dateTime": None, "timeZone": None},
        "end": {"date": "2026-10-04", "dateTime": None, "timeZone": None},
    }


def test_an_entry_with_guests_is_never_moved_or_removed():
    made, calls = writer()
    with pytest.raises(ValueError):
        made.move(entry(has_guests=True), SEVEN_PM)
    with pytest.raises(ValueError):
        made.delete(entry(has_guests=True))
    assert calls == []


# --- delete ------------------------------------------------------------------

@pytest.mark.parametrize("status", [204, 200, 404, 410])
def test_delete_removes_exactly_that_entry_and_already_gone_counts(status):
    made, calls = writer(httpx.Response(status))
    made.delete(entry("evt-9"))
    [request] = calls
    assert request.method == "DELETE"
    assert str(request.url) == EVENTS_URL + "/evt-9?sendUpdates=none"
    assert request.content == b""


def test_a_failed_delete_says_why():
    made, _ = writer(httpx.Response(500, json={"error": "backend"}))
    with pytest.raises(CalendarWriteError) as caught:
        made.delete(entry())
    assert caught.value.reason is UnavailableReason.PROVIDER_ERROR


# --- failures ----------------------------------------------------------------

@pytest.mark.parametrize(("response", "reason"), [
    (httpx.Response(401, json={"error": "no"}), UnavailableReason.PERMISSION_DENIED),
    (httpx.Response(403, json={"error": "no"}), UnavailableReason.PERMISSION_DENIED),
    (httpx.Response(429, json={"error": "slow down"}), UnavailableReason.RATE_LIMITED),
    (httpx.Response(500, json={"error": "backend"}), UnavailableReason.PROVIDER_ERROR),
    (httpx.Response(200, content=b"not json"), UnavailableReason.PROVIDER_ERROR),
    (httpx.Response(200, json={"summary": "no id"}), UnavailableReason.PROVIDER_ERROR),
    (httpx.ConnectError("refused"), UnavailableReason.NETWORK_ERROR),
])
def test_insert_failures_are_classified(response, reason):
    made, _ = writer(response)
    with pytest.raises(CalendarWriteError) as caught:
        made.insert(title="Dentist", slot=SEVEN_PM, marker_id=RECORD_ID)
    assert caught.value.reason is reason


def test_an_unusable_token_is_its_own_reason_and_sends_nothing():
    class RevokedToken:
        def token(self) -> str:
            raise GoogleAuthError(UnavailableReason.TOKEN_REVOKED, "revoked")

    made, calls = writer(token=RevokedToken())
    with pytest.raises(CalendarWriteError) as caught:
        made.find(RECORD_ID)
    assert caught.value.reason is UnavailableReason.TOKEN_REVOKED
    assert calls == []


def test_disabled_settings_never_touch_the_token_or_the_network():
    class ExplodingToken:
        def token(self) -> str:
            raise AssertionError("must not be called while disabled")

    made, calls = writer(token=ExplodingToken(), enabled=False)
    for attempt in (
        lambda: made.find(RECORD_ID),
        lambda: made.insert(title="Dentist", slot=SEVEN_PM, marker_id=RECORD_ID),
        lambda: made.move(entry(), SEVEN_PM),
        lambda: made.delete(entry()),
    ):
        with pytest.raises(CalendarWriteError) as caught:
            attempt()
        assert caught.value.reason is UnavailableReason.DISABLED
    assert calls == []


def test_every_request_goes_to_the_primary_calendars_events_and_writes_notify_no_one():
    made, calls = writer(
        listed(), httpx.Response(200, json=event()), httpx.Response(200, json=event()), httpx.Response(204),
    )
    made.find(RECORD_ID)
    made.insert(title="Dentist", slot=SEVEN_PM, marker_id=RECORD_ID)
    made.move(entry(), SEVEN_PM)
    made.delete(entry())

    assert [call.method for call in calls] == ["GET", "POST", "PATCH", "DELETE"]
    for call in calls:
        assert call.url.host == "www.googleapis.com"
        assert call.url.path.startswith("/calendar/v3/calendars/primary/events")
    for call in calls[1:]:
        assert query(call) == {"sendUpdates": "none"}


# --- Step I (placement engine slice 3): the engine's own blocks and slots ---------------------------

from gremlin_town.sources.google.calendar_writer import PlacedEntry  # noqa: E402

DAY = date(2026, 10, 2)
BEFORE = datetime(2026, 10, 2, 8, 0, tzinfo=SGT)
LATER = EventSlot(datetime(2026, 10, 2, 21, 0, tzinfo=SGT), datetime(2026, 10, 2, 21, 30, tzinfo=SGT))


def placed_event(event_id="evt-p1", *, role="slot", key="brn_x:1", day="2026-10-02", start=None, end=None, **extra):
    name = {"slot": "exosuit_slot", "block": "exosuit_block"}[role]
    body = {
        "id": event_id,
        "status": "confirmed",
        "summary": "▸ Call the bank",
        "start": start or {"dateTime": "2026-10-02T19:00:00+08:00", "timeZone": "Asia/Singapore"},
        "end": end or {"dateTime": "2026-10-02T19:30:00+08:00", "timeZone": "Asia/Singapore"},
        "transparency": "opaque",
        "extendedProperties": {"private": {"exosuit_day": day, name: key}},
    }
    body.update(extra)
    return body


def placed(event_id="evt-p1", *, slot=SEVEN_PM, has_guests=False, role="slot", key="brn_x:1") -> PlacedEntry:
    return PlacedEntry(event_id, DAY, role, key, "▸ Call the bank", slot, has_guests, True)


def test_insert_placed_sends_the_engines_properties_busy_and_no_guests():
    made, calls = writer(httpx.Response(200, json=placed_event()))
    added = made.insert_placed(
        day=DAY, role="slot", key="brn_x:1", title="▸ Call the bank", description="Call the bank",
        slot=SEVEN_PM, busy=True, now=BEFORE,
    )
    assert added == placed()
    [request] = calls
    assert request.method == "POST" and str(request.url) == EVENTS_URL + "?sendUpdates=none"
    sent = json.loads(request.content)
    assert sent["transparency"] == "opaque"
    assert sent["extendedProperties"] == {"private": {"exosuit_day": "2026-10-02", "exosuit_slot": "brn_x:1"}}
    assert "attendees" not in sent


def test_insert_placed_can_show_as_free():
    made, calls = writer(httpx.Response(200, json=placed_event(transparency="transparent")))
    added = made.insert_placed(day=DAY, role="slot", key="brn_x:1", title="t", description="", slot=SEVEN_PM, busy=False, now=BEFORE)
    assert json.loads(calls[0].content)["transparency"] == "transparent" and added.busy is False


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (dict(role="meeting"), "role"),
        (dict(key=""), "role"),
        (dict(slot=EventSlot(date(2026, 10, 2), date(2026, 10, 3))), "future"),
        (dict(now=datetime(2026, 10, 2, 19, 0, tzinfo=SGT)), "future"),
    ],
)
def test_insert_placed_refuses_other_roles_all_day_and_the_past(changes, message):
    made, calls = writer()
    arguments = dict(day=DAY, role="slot", key="brn_x:1", title="t", description="", slot=SEVEN_PM, busy=True, now=BEFORE)
    arguments.update(changes)
    with pytest.raises(ValueError, match=message):
        made.insert_placed(**arguments)
    assert calls == []


def test_list_placed_asks_for_the_day_and_keeps_only_the_engines_entries_for_it():
    n8 = event("evt-n8")
    other_day = placed_event("evt-od", day="2026-10-03")
    cancelled = placed_event("evt-c", status="cancelled")
    both = placed_event("evt-both")
    both["extendedProperties"]["private"]["exosuit_block"] = "2026-10-02:1"
    mixed = placed_event("evt-mixed")
    mixed["extendedProperties"]["private"]["exosuit"] = "brn_x"
    block = placed_event("evt-b", role="block", key="2026-10-02:1")
    made, calls = writer(listed(n8, other_day, cancelled, both, mixed, placed_event(), block))
    found = made.list_placed(DAY)
    assert [(entry.event_id, entry.role, entry.key) for entry in found] == [
        ("evt-b", "block", "2026-10-02:1"), ("evt-p1", "slot", "brn_x:1"),
    ]
    [request] = calls
    assert query(request)["privateExtendedProperty"] == "exosuit_day=2026-10-02"


def test_n8_find_never_returns_an_engine_entry():
    made, _ = writer(listed(placed_event()))
    assert made.find("brn_x") is None


def test_move_placed_reads_google_first_then_patches_the_time_and_title():
    made, calls = writer(httpx.Response(200, json=placed_event()), httpx.Response(200, json=placed_event(
        start={"dateTime": "2026-10-02T21:00:00+08:00"}, end={"dateTime": "2026-10-02T21:30:00+08:00"}, summary="▸ New")))
    moved = made.move_placed(placed(), LATER, now=BEFORE, title="▸ New")
    assert moved.slot == LATER and moved.title == "▸ New"
    read, request = calls
    assert read.method == "GET" and read.url.path.endswith("/events/evt-p1")
    assert request.method == "PATCH" and request.url.path.endswith("/events/evt-p1")
    assert query(request) == {"sendUpdates": "none"}
    assert json.loads(request.content)["summary"] == "▸ New"


def test_delete_placed_reads_google_first_then_removes_a_future_entry():
    made, calls = writer(httpx.Response(200, json=placed_event()), httpx.Response(204))
    made.delete_placed(placed(), now=BEFORE)
    read, request = calls
    assert read.method == "GET"
    assert request.method == "DELETE" and query(request) == {"sendUpdates": "none"}


@pytest.mark.parametrize(
    ("entry", "now", "message"),
    [
        (placed(has_guests=True), BEFORE, "guests"),
        (placed(), datetime(2026, 10, 2, 19, 0, tzinfo=SGT), "future"),  # starting now
        (placed(), datetime(2026, 10, 2, 19, 10, tzinfo=SGT), "future"),  # in progress
        (placed(), datetime(2026, 10, 2, 20, 0, tzinfo=SGT), "future"),  # past
        (placed(slot=None), BEFORE, "future"),
        (placed(role="meeting"), BEFORE, "own"),
    ],
)
def test_move_and_delete_refuse_past_in_progress_guests_and_others(entry, now, message):
    made, calls = writer()
    with pytest.raises(ValueError, match=message):
        made.delete_placed(entry, now=now)
    with pytest.raises(ValueError, match=message):
        made.move_placed(entry, LATER, now=now)
    assert calls == []


def test_move_placed_refuses_a_move_into_the_past():
    made, calls = writer()
    earlier = EventSlot(datetime(2026, 10, 2, 7, 0, tzinfo=SGT), datetime(2026, 10, 2, 7, 30, tzinfo=SGT))
    with pytest.raises(ValueError, match="future"):
        made.move_placed(placed(), earlier, now=BEFORE)
    assert calls == []


# Review fixes: the guards also run on Google's current copy; a move stays on its day.

@pytest.mark.parametrize(
    "current",
    [
        placed_event(attendees=[{"email": "sam@example.test"}]),  # guests added since
        placed_event(start={"dateTime": "2026-10-02T07:00:00+08:00"}, end={"dateTime": "2026-10-02T07:30:00+08:00"}),  # moved into the past
        event("evt-p1"),  # really an N8 entry, whatever the caller's copy said
        placed_event(day="2026-10-03"),  # another day's
    ],
)
def test_delete_and_move_refuse_what_google_says_now(current):
    made, calls = writer(httpx.Response(200, json=current))
    with pytest.raises(ValueError):
        made.delete_placed(placed(), now=BEFORE)
    made, calls = writer(httpx.Response(200, json=current))
    with pytest.raises(ValueError):
        made.move_placed(placed(), LATER, now=BEFORE)
    assert [request.method for request in calls] == ["GET"]


def test_delete_of_an_entry_already_gone_is_fine_and_a_move_of_one_is_refused():
    made, calls = writer(httpx.Response(404))
    made.delete_placed(placed(), now=BEFORE)
    assert [request.method for request in calls] == ["GET"]
    made, calls = writer(httpx.Response(410))
    with pytest.raises(ValueError, match="gone"):
        made.move_placed(placed(), LATER, now=BEFORE)


def test_move_placed_stays_on_its_own_day():
    made, calls = writer()
    next_day = EventSlot(datetime(2026, 10, 3, 10, 0, tzinfo=SGT), datetime(2026, 10, 3, 10, 30, tzinfo=SGT))
    with pytest.raises(ValueError, match="own day"):
        made.move_placed(placed(), next_day, now=BEFORE)
    assert calls == []



def test_a_block_under_way_may_change_its_end_to_a_later_time_than_now():
    under_way = datetime(2026, 10, 2, 19, 10, tzinfo=SGT)
    shorter = EventSlot(SEVEN_PM.start, datetime(2026, 10, 2, 19, 20, tzinfo=SGT))
    made, calls = writer(httpx.Response(200, json=placed_event(role="block", key="admin:1900")), httpx.Response(200, json=placed_event(
        role="block", key="admin:1900", end={"dateTime": "2026-10-02T19:20:00+08:00"})))
    moved = made.move_placed(placed(role="block", key="admin:1900"), shorter, now=under_way)
    assert moved.slot.end == shorter.end and [request.method for request in calls] == ["GET", "PATCH"]


@pytest.mark.parametrize(
    "new_slot",
    [
        EventSlot(datetime(2026, 10, 2, 19, 5, tzinfo=SGT), datetime(2026, 10, 2, 19, 20, tzinfo=SGT)),  # a new start
        EventSlot(SEVEN_PM.start, datetime(2026, 10, 2, 19, 5, tzinfo=SGT)),  # ending before now
    ],
)
def test_a_block_under_way_never_changes_its_start_or_ends_in_the_past(new_slot):
    made, calls = writer()
    with pytest.raises(ValueError):
        made.move_placed(placed(role="block", key="admin:1900"), new_slot, now=datetime(2026, 10, 2, 19, 10, tzinfo=SGT))
    assert calls == []



def test_an_entry_dragged_to_another_day_is_never_changed():
    made, calls = writer()
    next_day = EventSlot(datetime(2026, 10, 3, 10, 0, tzinfo=SGT), datetime(2026, 10, 3, 10, 30, tzinfo=SGT))
    with pytest.raises(ValueError, match="another day"):
        made.delete_placed(placed(slot=next_day), now=BEFORE)
    assert calls == []
