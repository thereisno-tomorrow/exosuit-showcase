"""Routines: history, streaks and completion rates (Ciel and Brain Home PRD §4.7; acceptance case 12)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from brain_core.errors import BrainError
from brain_core.orientation import due_moment, rank
from brain_core.records import RECORD_SCHEMA_V2, Record, record_from_mapping
from brain_core.review import ItemRecord
from brain_core.routines import (
    RECENT_OCCURRENCES,
    done_days,
    done_today,
    is_routine,
    latest_occurrence,
    routine_stats,
    stats_words,
    versions_of,
)
from brain_core.timewords import SINGAPORE

# Sunday 4 Oct 2026, 20:00 in Singapore.
NOW = datetime(2026, 10, 4, 20, 0, tzinfo=SINGAPORE)
TODAY = NOW.date()


def routine(repeat: str = "weekly:sun", *, saved: datetime, suffix: str = "a000", occurrence: str | None = None,
            supersedes: tuple[str, ...] = ()) -> Record:
    data: dict[str, object] = {"level": "accepted", "size": "task", "repeat": repeat}
    if occurrence is not None:
        data["occurrence"] = {"day": occurrence, "outcome": "done"}
    return Record(
        schema=RECORD_SCHEMA_V2, id=f"brn_{saved.astimezone(SINGAPORE):%Y%m%d}_000000_{suffix}", kind="item",
        title="Laundry", state="open", created_at=saved, updated_at=saved, assertion=None, confidence=None,
        sensitivity="private", vaults=(), owners=(), entities=(), evidence=(), supersedes=supersedes,
        dedupe_key=None, data=data, body="", source={"said_by": "owner"}, links=(),
    )


def history(done_on: list[str], *, started: datetime, repeat: str = "weekly:sun") -> tuple[Record, tuple[Record, ...]]:
    """A routine first saved at ``started`` and one new version per day Nic said it was done."""

    versions = [routine(repeat, saved=started, suffix="a000")]
    for index, day in enumerate(done_on, start=1):
        at = datetime.fromisoformat(day).replace(hour=19, tzinfo=SINGAPORE)
        versions.append(routine(repeat, saved=at, suffix=f"a{index:03x}", occurrence=day,
                                supersedes=tuple(version.id for version in versions)))
    return versions[-1], tuple(versions)


def test_a_sunday_routine_done_4_of_the_last_5_times_shows_80_percent_and_its_streak():
    """Acceptance case 12. Five Sundays are over (30 Aug to 27 Sep; today's isn't done yet, so it
    neither counts nor breaks the streak): done on four, missed on 13 Sep."""

    newest, versions = history(["2026-08-30", "2026-09-06", "2026-09-20", "2026-09-27"], started=datetime(2026, 8, 28, 9, tzinfo=SINGAPORE))
    stats = routine_stats(newest, versions, TODAY)
    assert [(day.isoformat(), done) for day, done in stats.outcomes] == [
        ("2026-08-30", True), ("2026-09-06", True), ("2026-09-13", False), ("2026-09-20", True), ("2026-09-27", True),
    ]
    assert (stats.done, stats.counted, stats.rate, stats.streak) == (4, 5, 0.8, 2)
    assert (stats.slipping, stats.strong) == (False, True)
    assert stats_words(stats) == "streak 2; done 4 of the last 5 (80%); strong"


def test_a_missed_occurrence_counts_against_the_streak_and_shows_slipping():
    """The next occurrence takes its place: it is never raised under Missed."""

    newest, versions = history(["2026-09-13", "2026-09-20"], started=datetime(2026, 9, 10, 9, tzinfo=SINGAPORE))
    stats = routine_stats(newest, versions, TODAY)
    assert (stats.streak, stats.done, stats.counted, stats.slipping, stats.strong) == (0, 2, 3, True, False)
    assert stats_words(stats) == "streak 0; done 2 of the last 3 (67%); slipping"


def test_todays_occurrence_counts_once_done_and_moves_the_routine_to_next_week():
    newest, versions = history(["2026-09-27", "2026-10-04"], started=datetime(2026, 9, 25, 9, tzinfo=SINGAPORE))
    stats = routine_stats(newest, versions, TODAY)
    assert (stats.streak, stats.counted) == (2, 2)
    assert done_today(newest, TODAY) and not done_today(versions[1], TODAY)
    assert due_moment(newest, TODAY)[0].date() == date(2026, 10, 11)
    assert due_moment(versions[1], TODAY)[0].date() == TODAY
    ranked = rank((ItemRecord(record=newest, path="records/item/x.md"),), TODAY, now=NOW)
    assert ranked[0].rules[0] == "every Sunday; next Sun 11 Oct"


def test_the_rate_counts_only_the_last_eight():
    days = [(date(2026, 6, 7) + timedelta(weeks=week)).isoformat() for week in range(9)]  # 7 Jun to 2 Aug done
    newest, versions = history(days, started=datetime(2026, 6, 5, 9, tzinfo=SINGAPORE))
    stats = routine_stats(newest, versions, TODAY)
    # 17 Sundays are over (7 Jun to 27 Sep): the first 9 done, the last 8 missed.
    assert (stats.counted, stats.done, stats.rate, stats.streak, stats.slipping) == (RECENT_OCCURRENCES, 0, 0.0, 0, True)


def test_a_daily_routine_counts_every_day_and_one_not_yet_over_says_so():
    newest, versions = history(["2026-10-02", "2026-10-03"], started=datetime(2026, 10, 1, 21, tzinfo=SINGAPORE), repeat="daily")
    stats = routine_stats(newest, versions, TODAY)
    assert [done for _day, done in stats.outcomes] == [False, True, True]
    assert stats.streak == 2
    fresh = routine("weekly:mon", saved=NOW)
    assert stats_words(routine_stats(fresh, (fresh,), TODAY)) == "no occurrence yet"


def test_what_counts_as_a_routine_and_its_latest_occurrence():
    assert is_routine(routine("weekly:sun", saved=NOW)) and is_routine(routine("daily", saved=NOW))
    assert not is_routine(routine("monthly", saved=NOW)) and not is_routine(routine("weekly:someday", saved=NOW))
    assert latest_occurrence(routine("weekly:fri", saved=NOW), TODAY) == date(2026, 10, 2)
    assert latest_occurrence(routine("weekly:sun", saved=NOW), TODAY) == TODAY
    assert latest_occurrence(routine("monthly", saved=NOW), TODAY) is None


def test_the_history_is_the_routines_own_versions():
    newest, versions = history(["2026-09-27"], started=datetime(2026, 9, 25, 9, tzinfo=SINGAPORE))
    other = routine(saved=NOW, suffix="ffff", occurrence="2026-10-04")
    assert versions_of(newest, (*versions, other)) == (newest, versions[0])
    assert done_days((*versions, other)) == frozenset({date(2026, 9, 27), date(2026, 10, 4)})


def _mapping(occurrence: object) -> dict[str, object]:
    return {
        "schema": "brain.record/v2", "id": "brn_20261004_120000_abcd", "kind": "item", "title": "Laundry",
        "state": "open", "created_at": "2026-10-04T12:00:00+00:00", "updated_at": "2026-10-04T12:00:00+00:00",
        "sensitivity": "private", "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "session_message", "locator": "session:x:1", "content_sha256": "a" * 64,
                      "excerpt": "done", "actor": "nic"}],
        "supersedes": [], "dedupe_key": None, "body": "",
        "data": {"level": "accepted", "size": "task", "repeat": "weekly:sun", "occurrence": occurrence},
        "source": {"said_by": "owner", "came_by": "direct", "words": "done", "recorded_by": "hermes-wa"},
        "links": [],
    }


def test_a_record_keeps_supported_occurrence_outcomes_and_refuses_unknown_shapes():
    record = record_from_mapping(_mapping({"day": "2026-10-04", "outcome": "done"}), "fixture")
    assert record.data["occurrence"] == {"day": "2026-10-04", "outcome": "done"}
    for outcome in ("skipped", "missed", "canceled"):
        assert record_from_mapping(_mapping({"day": "2026-10-04", "outcome": outcome}), "fixture").data["occurrence"]["outcome"] == outcome
    assert "occurrence" not in record_from_mapping(_mapping(None), "fixture").data
    for bad in ({"day": "Sunday", "outcome": "done"}, {"day": "2026-10-04", "outcome": "invented"},
                {"day": "2026-10-04", "outcome": "done", "note": "x"}, "done"):
        with pytest.raises(BrainError):
            record_from_mapping(_mapping(bad), "fixture")


@pytest.mark.parametrize('reverse', [False, True])
def test_equal_timestamp_occurrence_correction_follows_supersedes(reverse):
    done=routine('daily',saved=NOW,occurrence='2026-10-03')
    skipped=replace(done,id='brn_20261004_000000_a001',supersedes=(done.id,),
                    data={**done.data,'occurrence':{'day':'2026-10-03','outcome':'skipped'}})
    versions=[done,skipped]
    assert done_days(reversed(versions) if reverse else versions)==frozenset()
    corrected=replace(skipped,id='brn_20261004_000000_a002',supersedes=(done.id,skipped.id),
                      data={**done.data,'occurrence':{'day':'2026-10-03','outcome':'done'}})
    versions.append(corrected)
    assert done_days(reversed(versions) if reverse else versions)==frozenset({date(2026,10,3)})
