from datetime import date

import pytest

from brain_core.presentation import day_words, relative_day_words


@pytest.mark.parametrize("day,expected", [
    ("2026-10-05", "yesterday"), ("2026-10-06", "today"),
    ("2026-10-07", "tomorrow"), ("2026-10-08", "this Thursday, 8 Oct"),
    ("2026-10-11", "this Sunday, 11 Oct"), ("2026-10-12", "Mon 12 Oct"),
    ("2026-10-04", "Sun 4 Oct"),
])
def test_relative_days_share_one_locale_independent_rule(day, expected):
    assert relative_day_words(day, date(2026, 10, 6)) == expected
    assert relative_day_words(date.fromisoformat(day), date(2026, 10, 6)) == expected
    assert relative_day_words(day) == day_words(day)


def test_tomorrow_wins_across_the_week_and_year_boundary():
    assert relative_day_words("2026-10-12", date(2026, 10, 11)) == "tomorrow"
    assert relative_day_words("2027-01-01", date(2026, 12, 31)) == "tomorrow"


@pytest.mark.parametrize("internal,words,label", [
    ("up", "can do now", "Can do now"),
    ("needs_you", "For your review", "For your review"),
    ("orient", "All my work", "All my work"),
])
def test_work_labels_use_nics_words(internal, words, label):
    from brain_core.presentation import work_words
    assert work_words(internal) == words
    assert work_words(internal, label=True) == label
    assert work_words("waiting") == "waiting"


def test_fixture_morning_weekly_all_work_and_door_have_no_retired_labels():
    import json
    import re
    from brain_core import work
    from brain_core.orientation import overview, what_next
    from brain_core.review import build_morning, render_morning
    from brain_core.weekly import TITLES, weekly_review
    from test_morning import fixture, inputs as morning_inputs, NOW
    from test_orientation import inputs as orientation_inputs
    from test_moves import run

    records = fixture.moved_over_records()
    before = tuple(record.sha256 for record in records)
    morning = build_morning(morning_inputs(records))
    weekly = weekly_review(morning_inputs(records).items, NOW)
    all_work = overview(orientation_inputs(*records, now=NOW), NOW)
    pick = what_next(orientation_inputs(*records, now=NOW), NOW)
    task = work.index_things(records)[fixture.ExhibitCover]
    reply = run(records, "Start " + task.title, [{"move": "start", "thing": task.id,
        "words": "Start " + task.title}], now=NOW).reply
    assert reply["things"][0]["now"] == "can do now"
    assert reply["things"][0]["why"] == "can do now"
    assert work.status_of(task, work.index_things(records), NOW) == "up"
    assert all_work.title == "All my work"
    assert "For your review" in render_morning(morning)
    texts = (render_morning(morning),
             "\n".join([*(TITLES[part] for part in weekly.parts), *(line.text for line in weekly.lines)]),
             "\n".join([all_work.title, *(line.text for line in all_work.lines)]),
             json.dumps(reply), str(pick))
    for text in texts:
        assert not re.search(r"Needs you|needs Nic|Where am I\?|\(up\)|\. up[.\s]|\"now\": \"up\"", text)
    assert tuple(record.sha256 for record in records) == before
