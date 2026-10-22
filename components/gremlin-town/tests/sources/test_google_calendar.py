"""CalendarReader: timed and all-day events, pagination, cancellation, and per-calendar failure."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gremlin_town.sources.google.calendar import CalendarReader
from gremlin_town.sources.google.settings import GoogleSettings
from gremlin_town.sources.result import ReadStatus, UnavailableReason

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "google"

TIME_MIN = datetime(2026, 9, 26, tzinfo=UTC)
TIME_MAX = datetime(2026, 10, 3, tzinfo=UTC)
ACCESS_TOKEN = "test-access-token"


class FixedToken:
    """A token provider double that hands back a fixed access token."""

    def token(self) -> str:
        return ACCESS_TOKEN


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def settings(*, calendars=("primary",), account="nic@example.test"):
    return GoogleSettings.from_table({
        "enabled": True, "account": account, "credential_env": "X", "calendars": list(calendars),
    })


def recording_transport(handler):
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), calls


def reader(handler, **settings_kwargs) -> tuple[CalendarReader, list[httpx.Request]]:
    transport, calls = recording_transport(handler)
    return CalendarReader(settings(**settings_kwargs), FixedToken(), transport=transport), calls


def test_timed_and_all_day_events_become_source_items_and_pagination_is_followed():
    page1 = load_fixture("calendar_events_page1.json")
    page2 = load_fixture("calendar_events_page2.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "www.googleapis.com"
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        params = dict(httpx.QueryParams(request.url.query))
        assert params["singleEvents"] == "true"
        assert params["orderBy"] == "startTime"
        if "pageToken" in params:
            assert params["pageToken"] == "page-2-token"
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    made, calls = reader(handler)
    read = made.events(TIME_MIN, TIME_MAX)

    assert read.status is ReadStatus.OK
    assert len(calls) == 2
    assert [item.provider_id for item in read.items] == ["evt-timed-1", "evt-allday-1"]

    timed = read.items[0]
    assert timed.source == "google.calendar"
    assert timed.account == "nic@example.test"
    assert timed.link == "https://www.google.com/calendar/event?eid=evt-timed-1"
    assert timed.said_by is None
    assert timed.fields["start"] == "2026-09-26T15:00:00+08:00"
    assert timed.fields["end"] == "2026-09-26T15:30:00+08:00"

    allday = read.items[1]
    assert allday.fields["start"] == "2026-09-27"
    assert allday.fields["end"] == "2026-09-28"


def test_description_is_captured_for_the_exosuit_marker():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [{
                "id": "evt-marked-1",
                "status": "confirmed",
                "htmlLink": "https://www.google.com/calendar/event?eid=evt-marked-1",
                "summary": "Send Sam the deck",
                "description": "exosuit:brn_20260926_deck",
                "start": {"date": "2026-10-02"},
                "end": {"date": "2026-10-03"},
            }]
        })

    made, _ = reader(handler)
    read = made.events(TIME_MIN, TIME_MAX)
    assert read.items[0].fields["description"] == "exosuit:brn_20260926_deck"


def test_missing_description_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [{
                "id": "evt-plain-1",
                "status": "confirmed",
                "htmlLink": "https://www.google.com/calendar/event?eid=evt-plain-1",
                "summary": "Standup",
                "start": {"dateTime": "2026-09-26T09:00:00+08:00"},
                "end": {"dateTime": "2026-09-26T09:30:00+08:00"},
            }]
        })

    made, _ = reader(handler)
    read = made.events(TIME_MIN, TIME_MAX)
    assert read.items[0].fields["description"] is None


def test_cancelled_events_are_dropped():
    page1 = load_fixture("calendar_events_page1.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": page1["items"]})

    made, _ = reader(handler)
    read = made.events(TIME_MIN, TIME_MAX)
    assert [item.provider_id for item in read.items] == ["evt-timed-1"]


@pytest.mark.parametrize(("status", "reason"), [
    (403, UnavailableReason.PERMISSION_DENIED),
    (429, UnavailableReason.RATE_LIMITED),
    (500, UnavailableReason.PROVIDER_ERROR),
    (503, UnavailableReason.PROVIDER_ERROR),
])
def test_http_failures_are_classified(status, reason):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "provider says no"})

    made, _ = reader(handler)
    read = made.events(TIME_MIN, TIME_MAX)
    assert read.status is ReadStatus.PARTIAL
    assert read.unavailable == ("primary",)
    assert read.items == ()


def test_transport_exception_is_a_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    made, _ = reader(handler)
    read = made.events(TIME_MIN, TIME_MAX)
    assert read.status is ReadStatus.PARTIAL
    assert read.unavailable == ("primary",)


def test_one_failing_calendar_among_two_makes_the_read_partial_and_names_it():
    good = load_fixture("calendar_events_page2.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/calendars/primary/" in str(request.url):
            return httpx.Response(200, json=good)
        return httpx.Response(403, json={"error": "forbidden"})

    made, _ = reader(handler, calendars=("primary", "broken@example.test"))
    read = made.events(TIME_MIN, TIME_MAX)

    assert read.status is ReadStatus.PARTIAL
    assert read.unavailable == ("broken@example.test",)
    assert [item.provider_id for item in read.items] == ["evt-allday-1"]


def test_disabled_settings_never_touch_the_token_provider_or_transport():
    class ExplodingToken:
        def token(self) -> str:
            raise AssertionError("must not be called while disabled")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send a request while disabled")

    transport = httpx.MockTransport(handler)
    disabled = GoogleSettings.from_table(None)
    made = CalendarReader(disabled, ExplodingToken(), transport=transport)
    read = made.events(TIME_MIN, TIME_MAX)
    assert read.status is ReadStatus.UNAVAILABLE
    assert read.reason is UnavailableReason.DISABLED


# --- Step I (placement engine slice 3): free events and the engine's own entries ------------------


def _one_event(**extra):
    body = {
        "id": "evt-1", "status": "confirmed", "htmlLink": "https://www.google.com/calendar/event?eid=evt-1",
        "summary": "Lunch", "start": {"dateTime": "2026-10-01T12:00:00+08:00"}, "end": {"dateTime": "2026-10-01T13:00:00+08:00"},
    }
    body.update(extra)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [body]})

    made, _ = reader(handler)
    [item] = made.events(TIME_MIN, TIME_MAX).items
    return item


def test_a_free_event_says_so_and_a_busy_one_has_no_transparency():
    assert _one_event(transparency="transparent").fields["transparency"] == "transparent"
    assert _one_event().fields["transparency"] is None


@pytest.mark.parametrize(
    ("private", "role"),
    [
        ({"exosuit_day": "2026-10-01", "exosuit_slot": "brn_x:1"}, "slot"),
        ({"exosuit_day": "2026-10-01", "exosuit_block": "2026-10-01:1"}, "block"),
        ({"exosuit_slot": "brn_x:1"}, None),  # no day: not the engine's
        ({"exosuit": "brn_x"}, None),  # an N8 entry
        ({"exosuit_day": "2026-10-01", "exosuit_slot": "brn_x:1", "exosuit": "brn_x"}, None),  # N8 marker wins
        ({"exosuit_day": "2026-09-30", "exosuit_slot": "brn_x:1"}, None),  # dragged to another day: his now
        ({}, None),
    ],
)
def test_the_engines_own_entries_say_which_they_are(private, role):
    assert _one_event(extendedProperties={"private": private}).fields["exosuit_role"] == role


def test_the_runtime_turns_them_into_calendar_events():
    from gremlin_town.runtime import _google_calendar_event

    free = _google_calendar_event(_one_event(transparency="transparent"))
    slot = _google_calendar_event(_one_event(extendedProperties={"private": {"exosuit_day": "2026-10-01", "exosuit_slot": "k:1"}}))
    meeting = _google_calendar_event(_one_event())
    assert (free.busy, free.exosuit_role) == (False, None)
    assert (slot.busy, slot.exosuit_role) == (True, "slot")
    assert (meeting.busy, meeting.exosuit_role) == (True, None)
