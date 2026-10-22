"""Work-model date kinds (plan section 5, W3). The old resolver tests stay in test_timewords.py."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from brain_core.errors import BrainError
from brain_core.timewords import (
    DAY_STARTS,
    DEADLINE_WITHOUT_WORD,
    DEADLINE_WORDS,
    NOT_IN_MESSAGE,
    PAST_DAY,
    SINGAPORE,
    check_agent_reading,
    find,
    find_one,
    personal_today,
    read_when,
    resolve,
    resolve_date_and_time,
)

WED = date(2026, 9, 30)  # the 30 Sep anchor: Wednesday
SATURDAY = date(2026, 9, 26)
MONDAY = date(2026, 9, 28)
NOON = time(12, 0)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "work_model"

# resolved_from.words in the W0 integration fixture. Listed so a new phrase shows up here.
FIXTURE_TIME_PHRASES = (
    "8 October",
    "Tomorrow",
    "at 12pm",
    "later at 8pm",
    "on 8 October",
    "today",
)


def _read(phrase: str, today: date = WED, clock: time | None = NOON, **kwargs):
    return read_when(phrase, today=today, now_clock=clock, **kwargs)


def test_constants() -> None:
    assert DAY_STARTS == "04:00"
    assert DEADLINE_WORDS == ("by", "before", "due", "latest")


def test_personal_today_is_the_singapore_date_minus_four_hours() -> None:
    assert personal_today(datetime(2026, 9, 30, 0, 10, tzinfo=SINGAPORE)) == date(2026, 9, 29)
    assert personal_today(datetime(2026, 9, 30, 3, 59, tzinfo=SINGAPORE)) == date(2026, 9, 29)
    assert personal_today(datetime(2026, 9, 30, 4, 0, tzinfo=SINGAPORE)) == date(2026, 9, 30)
    with pytest.raises(BrainError):
        personal_today(datetime(2026, 9, 30, 0, 10))


def test_four_am_boundary_on_30_sep() -> None:
    assert _read("tomorrow", clock=time(0, 10)).day == "2026-09-30"
    assert _read("tomorrow", clock=time(0, 10)).kind == "planned"
    assert _read("today", clock=time(3, 59)).day == "2026-09-29"
    assert _read("today", clock=time(4, 0)).day == "2026-09-30"
    moment = datetime(2026, 9, 30, 0, 10, tzinfo=SINGAPORE)
    assert _read("today", clock=time(0, 10)).day == personal_today(moment).isoformat()


def test_old_resolver_does_not_learn_the_new_phrases() -> None:
    with pytest.raises(BrainError):
        resolve("tues", today=WED)
    with pytest.raises(BrainError):
        resolve("before december", today=WED)
    assert find("before december") == ()
    assert find("before 1 december") == ("1 december",)
    assert find_one("oh btw i need to renew my passport before december") is None
    assert find_one("end of november") is None
    assert find_one("by end of november") is None
    assert find_one("next tues after lunch") is None
    assert find_one("by tomorrow noon") == "by tomorrow"


@pytest.mark.parametrize(
    "phrase",
    [
        "today",
        "tomorrow",
        "Friday",
        "on Friday",
        "next Friday",
        "by Friday",
        "tmr at 12pm",
        "tonight at 7pm",
        "tonight 6pm",
        "this weekend",
        "this week",
        "next week",
        "later at 8pm",
        "7pm",
        "8 October",
        "on 8 October",
        "30 november",
        "tomorrow morning",
    ],
)
def test_daytime_reading_matches_the_old_resolver(phrase: str) -> None:
    old = resolve_date_and_time(phrase, today=WED, now=NOON)
    new = _read(phrase)
    assert new is not None and new.read_by == "code"
    if old.window is not None:
        assert (new.start, new.end) == old.window
        assert new.day is None
        assert new.kind == "planned"
    else:
        assert new.day == old.due
        assert new.at == old.at


def test_phrasing_table_seventeen_read_and_two_do_not() -> None:
    """The study's rows: 17 phrases resolved on the old reader, 2 did not (research phrasing.md)."""

    tonight = _read("tonight 6pm")
    assert tonight.kind == "appointment" and tonight.day == "2026-09-30" and tonight.at == "18:00"
    at_seven = _read("tonight at 7pm")
    assert at_seven.kind == "appointment" and at_seven.at == "19:00" and at_seven.day == "2026-09-30"

    later = _read("later 12am", clock=time(21, 50))
    old_later = resolve_date_and_time("later 12am", today=WED, now=time(21, 50))
    assert later.kind == "appointment" and later.day == old_later.due and later.at == "00:00"
    no_clock = _read("later 12am", clock=None)
    assert no_clock.day == "2026-09-30" and no_clock.at == "00:00"

    tmr = _read("tmr at 12pm")
    assert tmr.kind == "appointment" and tmr.day == "2026-10-01" and tmr.at == "12:00"

    friday = _read("on Friday", today=SATURDAY)
    assert friday.kind == "planned" and friday.day == "2026-10-02" and friday.at is None

    today = _read("today")
    assert today.kind == "planned" and today.day == "2026-09-30" and today.at is None

    # Said at 00:10 on 30 Sep. The old resolver, given the calendar date, says 1 Oct.
    tomorrow = _read("Tomorrow", clock=time(0, 10))
    assert tomorrow.kind == "planned" and tomorrow.day == "2026-09-30"

    october = _read("on 8 October")
    assert october.kind == "planned" and october.day == "2026-10-08" and october.at is None

    exam = _read("next week's exam", today=SATURDAY)
    assert exam.kind == "planned" and exam.start == "2026-09-28" and exam.end == "2026-10-04"
    pitch = _read("next week's pitch", today=MONDAY)
    assert pitch.kind == "planned" and pitch.start == "2026-10-05" and pitch.end == "2026-10-11"

    assert _read("in the morning") is None
    assert _read("now") is None
    assert _read("going to the printer now") is None


def test_probes() -> None:
    by_friday = _read("by Friday 5pm")
    assert by_friday.kind == "deadline" and by_friday.day == "2026-10-02" and by_friday.at == "17:00"
    by_today = _read("by 6pm today")
    assert by_today.kind == "deadline" and by_today.day == "2026-09-30" and by_today.at == "18:00"

    weekend = _read("this weekend")
    assert weekend.kind == "planned" and weekend.day is None
    assert weekend.start == "2026-10-03" and weekend.end == "2026-10-04"
    assert _read("next Tuesday").day == "2026-10-06"
    week = _read("this week")
    assert week.kind == "planned" and week.start == "2026-09-30" and week.end == "2026-10-04"

    tmrw = _read("tmrw at 12pm")
    assert tmrw.kind == "appointment" and tmrw.at == "12:00" and tmrw.day == "2026-10-01"
    morning = _read("tomorrow morning")
    assert morning.kind == "planned" and morning.at is None and morning.day == "2026-10-01"

    noon = _read("by tomorrow noon")
    assert noon.kind == "deadline" and noon.day == "2026-10-01" and noon.at == "12:00"

    end = _read("end of the month")
    assert end.kind == "planned" and end.day == "2026-09-30"
    two_weeks = _read("in 2 weeks")
    assert two_weeks.kind == "planned" and two_weeks.day == "2026-10-14"
    three_days = _read("in 3 days")
    assert three_days.kind == "planned" and three_days.day == "2026-10-03"
    before = _read("before december")
    assert before.kind == "deadline" and before.day == "2026-11-30"

    for phrase in (
        "next month",
        "the 15th",
        "mid Oct",
        "a few days",
        "by eod",
        "asap",
        "q4",
        "later",
        "when she replies",
        "every morning",
    ):
        assert _read(phrase) is None, phrase

    after = _read("after my appointment")
    assert after is not None and after.kind == "not_before" and after.day is None
    assert after.event == "my appointment"


def test_new_phrases_on_30_sep() -> None:
    before = _read("before december")
    assert before.kind == "deadline" and before.day == "2026-11-30" and before.read_by == "code"
    assert _read("before december", today=date(2026, 12, 1)).day == "2027-11-30"
    assert _read("before 1 december").day == "2026-11-30"
    assert _read("before 1 december").kind == "deadline"

    end = _read("end of november")
    assert end.kind == "planned" and end.day == "2026-11-30"
    by_end = _read("by end of november")
    assert by_end.kind == "deadline" and by_end.day == "2026-11-30"
    before_end = _read("before end of november")
    assert before_end.kind == "deadline" and before_end.day == "2026-11-29"

    lunch = _read("next tues after lunch")
    assert lunch.kind == "planned" and lunch.day == "2026-10-06" and lunch.at is None
    assert "after lunch" in lunch.words

    assert _read("tues").day == "2026-10-06"
    assert _read("thur").day == "2026-10-01"
    assert _read("thurs").day == "2026-10-01"
    assert _read("weds").day == "2026-10-07"

    by_weekend = _read("by the weekend")
    assert by_weekend.kind == "deadline" and by_weekend.day == "2026-10-04"
    owed = _read("this weekend", owed_to=True)
    assert owed.kind == "deadline" and owed.day == "2026-10-04" and owed.start is None

    assert _read("start by friday").kind == "start_by"
    assert _read("start by friday").day == "2026-10-02"
    assert _read("start on it by friday").kind == "start_by"

    memo = _read("8 October", event="venue inspection")
    assert memo.kind == "not_before" and memo.day == "2026-10-08"
    assert memo.event == "venue inspection" and memo.at is None
    assert _read("end of february", today=date(2026, 1, 15)).day == "2026-02-28"


def test_before_a_counted_span_is_the_day_before() -> None:
    """A counted span is a date, so "before" steps back one day. The plain and "by" readings do not."""

    three = _read("before in 3 days")
    assert three.kind == "deadline" and three.day == "2026-10-02"
    two = _read("before in 2 weeks")
    assert two.kind == "deadline" and two.day == "2026-10-13"

    assert _read("in 3 days").kind == "planned" and _read("in 3 days").day == "2026-10-03"
    assert _read("in 2 weeks").kind == "planned" and _read("in 2 weeks").day == "2026-10-14"
    assert _read("by in 3 days").kind == "deadline" and _read("by in 3 days").day == "2026-10-03"
    assert _read("by in 2 weeks").kind == "deadline" and _read("by in 2 weeks").day == "2026-10-14"
    assert _read("before december").kind == "deadline" and _read("before december").day == "2026-11-30"
    assert _read("end of november").kind == "planned" and _read("end of november").day == "2026-11-30"
    assert _read("by end of november").kind == "deadline" and _read("by end of november").day == "2026-11-30"
    assert _read("before end of november").kind == "deadline" and _read("before end of november").day == "2026-11-29"


def test_30_sep_messages_read_as_the_memo() -> None:
    """Memo 2.15's dates. A wait's expected day and someone else's suggestion are stored by the door."""

    messages = json.loads((FIXTURE / "messages_30_sep.json").read_text(encoding="utf-8"))
    text = "\n".join(item["message"] for item in messages["messages"])
    for phrase in (
        "8 October",
        "today",
        "Tomorrow",
        "this week",
        "before december",
        "end of november",
        "next tues after lunch",
        "by friday",
        "this weekend",
        "by Thursday",
    ):
        assert phrase.lower() in text.lower()

    assert _read("before december").kind == "deadline" and _read("before december").day == "2026-11-30"
    dentist = _read("next tues after lunch")
    assert dentist.kind == "planned" and dentist.day == "2026-10-06" and "after lunch" in dentist.words
    assert _read("by friday").day == "2026-10-02"
    assert _read("by Thursday").day == "2026-10-01"
    photos = _read("this weekend", owed_to=True)
    assert photos.kind == "deadline" and photos.day == "2026-10-04"
    assert _read("this weekend").kind == "planned" and _read("this weekend").end == "2026-10-04"
    assert _read("Tomorrow", clock=time(0, 10)).day == "2026-09-30"
    assert _read("today").kind == "planned" and _read("today").day == "2026-09-30"
    assert _read("8 October", event="venue inspection").kind == "not_before"

    assert _read("before I send to Morgan") is None
    assert _read("had an amazing laksa for lunch lol") is None
    assert _read("I need to edit my brief before I send to Morgan") is None


def test_fixture_phrases_are_listed_and_read() -> None:
    found: set[str] = set()
    for path in (FIXTURE).rglob("*.md"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("      words:"):
                found.add(line.split(":", 1)[1].strip().strip("'"))
    assert tuple(sorted(found)) == FIXTURE_TIME_PHRASES
    for phrase in FIXTURE_TIME_PHRASES:
        reading = _read(phrase)
        assert reading is not None and reading.read_by == "code"
        assert reading.words == phrase


def test_agent_reading_is_refused_for_the_three_guards() -> None:
    today = WED
    missing = check_agent_reading(
        "mid Oct", "renew my passport", {"day": "2026-10-15", "kind": "planned"}, today,
    )
    assert missing.reading is None and missing.flags == (NOT_IN_MESSAGE,)

    past = check_agent_reading(
        "mid Oct", "let's say mid Oct", {"day": "2026-09-01", "kind": "planned"}, today,
    )
    assert past.reading is None and past.flags == (PAST_DAY,)

    deadline = check_agent_reading(
        "mid Oct", "please save mid Oct", {"day": "2026-10-15", "kind": "deadline"}, today,
    )
    assert deadline.flags == (DEADLINE_WITHOUT_WORD,)
    assert deadline.reading is not None and deadline.reading.kind == "planned"
    assert deadline.reading.day == "2026-10-15"

    kept = check_agent_reading(
        "mid Oct",
        "mid Oct",
        {"day": "2026-10-15", "kind": "planned", "read_by": "participant:diablo"},
        today,
    )
    assert kept.flags == ()
    assert kept.reading is not None
    assert kept.reading.day == "2026-10-15" and kept.reading.read_by == "participant:diablo"

    allowed = check_agent_reading(
        "by mid Oct", "by mid Oct", {"day": "2026-10-15", "kind": "deadline"}, today,
    )
    assert allowed.flags == () and allowed.reading is not None and allowed.reading.kind == "deadline"

    promise = check_agent_reading(
        "this weekend",
        "this weekend",
        {"day": "2026-10-04", "kind": "deadline"},
        today,
        owed_to=True,
    )
    assert promise.flags == () and promise.reading is not None and promise.reading.kind == "deadline"


def test_frozen_parser_accepts_planned_beside_a_dated_event_wait():
    from brain_core.records import record_from_mapping
    from test_moves import item
    record = item("Memo")
    value = record.as_dict()
    day = dict(day="2026-10-08", event="venue inspection", words="8 October",
               read_by="code", read_on="2026-09-30")
    value["data"]["dates"] = {"planned": dict(day), "not_before": dict(day)}
    parsed = record_from_mapping(value, "W22 frozen date shape")
    assert parsed.data["dates"]["planned"] == parsed.data["dates"]["not_before"]
