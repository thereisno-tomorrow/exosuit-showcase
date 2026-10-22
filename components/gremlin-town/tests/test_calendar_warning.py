"""Calendar provider failures must remain visible through the runtime and surfaces."""
from datetime import datetime, timedelta

import httpx
import pytest

from brain_core.review import build_morning, build_review
from brain_core.timewords import SINGAPORE
from gremlin_town.dashboard.reads import DashboardReader, DashboardSources
from gremlin_town.review.collect import ReviewSources, SourceUnavailable, collect_review_inputs
from gremlin_town.runtime import _google_calendar_reader
from gremlin_town.sources.google.calendar import CalendarReader
from gremlin_town.sources.google.settings import GoogleSettings

NOW = datetime(2026, 10, 5, 9, tzinfo=SINGAPORE)
CALENDARS = ("primary", "private-calendar@example.test")


@pytest.mark.parametrize("failed", [(), (CALENDARS[1],), CALENDARS])
def test_calendar_failures_keep_events_and_warn_at_both_surfaces(monkeypatch, failed):
    class Token:
        def token(self):
            return "fake-token"

    def respond(request):
        calendar = request.url.path.split("/calendars/")[1].split("/events")[0]
        if calendar in failed:
            return httpx.Response(503, json={"error": "private provider error"})
        return httpx.Response(200, json={"items": [{
            "id": "event-1", "summary": f"Meeting from {calendar}",
            "start": {"dateTime": NOW.isoformat()},
            "end": {"dateTime": (NOW + timedelta(hours=1)).isoformat()},
        }]})

    google = GoogleSettings.from_table({"enabled": True, "account": "nic@example.test",
                                       "credential_env": "X", "calendars": list(CALENDARS)})
    provider = CalendarReader(google, Token(), transport=httpx.MockTransport(respond))
    monkeypatch.setattr("gremlin_town.sources.google.calendar.CalendarReader", lambda *_: provider)
    try:
        read = _google_calendar_reader(google, (None, Token()))
        inputs = collect_review_inputs(ReviewSources(
            brain_items=lambda: (), open_requests=lambda: (), calendar_events=read,
        ), history=(), now=NOW)
        expected_count = len(CALENDARS) - len(failed)
        assert len(inputs.calendar_events) == expected_count
        assert inputs.source_status["calendar"] == ("partial" if failed else None)

        review = build_review(inputs)
        warnings = [line for line in review.lines if line.rule == "source_unavailable"]
        assert [line.text for line in warnings] == (["calendar: partial"] if failed else [])
        morning = build_morning(inputs)
        assert any(line.text == "Couldn't see calendar: partial"
                   for part in morning.parts for line in part.lines) == bool(failed)

        dashboard = DashboardReader(DashboardSources(items=lambda: (), calendar=read)).read(NOW)
        assert len(dashboard["day"]["timeline"]) == expected_count
        warning = "Couldn't see all your calendars; this day may be incomplete"
        for surface in ("day", "work"):
            assert (warning in dashboard[surface]["notices"]) == bool(failed)
            assert CALENDARS[1] not in str(dashboard[surface]["notices"])
            assert "private provider error" not in str(dashboard[surface]["notices"])

        strict = _google_calendar_reader(google, (None, Token()), strict=True)
        if failed:
            with pytest.raises(SourceUnavailable, match="partial") as refused:
                strict(NOW, NOW + timedelta(days=1))
            assert refused.value.items == ()
        else:
            assert len(strict(NOW, NOW + timedelta(days=1))) == expected_count
    finally:
        provider._client.close()
