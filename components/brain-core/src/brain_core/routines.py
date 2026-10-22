"""Routines: their history, streaks and completion rates (Ciel and Brain Home PRD §4.7; build plan
slice 8). Pure functions, no IO.

A routine is an item with a ``repeat`` code can place in time: ``weekly:<mon..sun>`` or ``daily``.
Each time Nic says he did one, an agent saves a new version of the routine carrying that occurrence,
``data.occurrence = {"day": ..., "outcome": "done"}`` (Accepted - Nic, 28 Sep 2026: each occurrence as
a new version, so the history stays in the routine's own versions). An occurrence whose day passed
without one is missed: it counts against the streak, and it is never raised under "Missed"; the
next occurrence takes its place. **Proposed - Nic to confirm:** a miss is worked out from the days
that passed, not saved as a version of its own, since nothing writes records unasked (the 06:00
review only reads); the counts are the same.

Counted Taskwarrior-style, as taskwarrior-mcp counts habits: the streak is how many occurrences in a
row were done, back from the latest one that is over (today's, while not yet done, neither counts
nor breaks it); the completion rate is the share done of the last `RECENT_OCCURRENCES`; slipping is
the latest one that is over having been missed; strong is `STRONG_RATE` or more over at least
`STRONG_AFTER` occurrences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from brain_core.records import RECORD_SCHEMA_V2, Record, first_saved
from brain_core.timewords import SINGAPORE

# PRD §4.7 (Proposed - Nic to confirm): the completion rate counts the last 8 occurrences.
RECENT_OCCURRENCES = 8
STRONG_RATE = 0.8
STRONG_AFTER = 5
DONE = "done"
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def weekday(repeat: object) -> int | None:
    """The weekday of a ``weekly:<day>`` routine, or None."""

    if not isinstance(repeat, str) or not repeat.startswith("weekly:"):
        return None
    return _WEEKDAYS.get(repeat.split(":", 1)[1].strip().lower())


def is_routine(record: Record) -> bool:
    """An item whose ``repeat`` code can place in time."""

    if record.schema != RECORD_SCHEMA_V2 or record.kind != "item":
        return False
    repeat = record.data.get("repeat")
    return repeat == "daily" or weekday(repeat) is not None


def occurs_on(record: Record, day: date) -> bool:
    repeat = record.data.get("repeat")
    return repeat == "daily" or weekday(repeat) == day.weekday()


def occurrence_day(repeat: object, today: date) -> date | None:
    """A ``repeat``'s occurrence on or before ``today``, or None when code can't place it."""

    if repeat == "daily":
        return today
    day = weekday(repeat)
    return None if day is None else today - timedelta(days=(today.weekday() - day) % 7)


def latest_occurrence(record: Record, today: date) -> date | None:
    """The routine's occurrence on or before ``today``: today itself when it is one of its days."""

    return occurrence_day(record.data.get("repeat"), today) if is_routine(record) else None


def done_days(versions: Iterable[Record]) -> frozenset[date]:
    """Days whose newest explicit dated outcome is done, preserving corrections."""

    outcomes: dict[date, Record] = {}
    for version in versions:
        occurrence = version.data.get("occurrence")
        if isinstance(occurrence, Mapping):
            day = date.fromisoformat(occurrence["day"])
            prior = outcomes.get(day)
            # Saved revisions name their predecessors even when the clock has not
            # advanced. Causal correction wins before the timestamp fallback.
            if prior is None or prior.id in version.supersedes or (
                version.id not in prior.supersedes and version.updated_at > prior.updated_at
            ):
                outcomes[day] = version
    return frozenset(day for day, version in outcomes.items()
                     if version.data["occurrence"]["outcome"] == DONE)


def done_today(record: Record, today: date) -> bool:
    """Its newest version already records today's occurrence done: the next one is due instead."""

    occurrence = record.data.get("occurrence")
    return (
        is_routine(record) and isinstance(occurrence, Mapping) and occurrence.get("outcome") == DONE
        and occurrence.get("day") == today.isoformat()
    )


@dataclass(frozen=True, slots=True)
class RoutineStats:
    """``outcomes`` are the counted occurrences, oldest first, as (day, done); ``rate`` is None
    before the first occurrence is over."""

    streak: int
    done: int
    counted: int
    rate: float | None
    slipping: bool
    strong: bool
    outcomes: tuple[tuple[date, bool], ...]


def routine_stats(record: Record, versions: Sequence[Record], today: date) -> RoutineStats:
    """``record`` is the routine's newest version and ``versions`` all of its versions, it
    included. Occurrences run from the first on or after the day the routine was first saved."""

    start = first_saved(record).astimezone(SINGAPORE).date()
    done = done_days(versions)
    days: list[date] = []
    day = start
    while day <= today:
        if occurs_on(record, day):
            days.append(day)
        day += timedelta(days=1)
    # Today's occurrence counts once it is done; until then its day isn't over.
    if days and days[-1] == today and today not in done:
        days.pop()
    outcomes = tuple((day, day in done) for day in days)
    streak = 0
    for _day, was_done in reversed(outcomes):
        if not was_done:
            break
        streak += 1
    recent = outcomes[-RECENT_OCCURRENCES:]
    done_count = sum(1 for _day, was_done in recent if was_done)
    rate = done_count / len(recent) if recent else None
    return RoutineStats(
        streak=streak,
        done=done_count,
        counted=len(recent),
        rate=rate,
        slipping=bool(outcomes) and not outcomes[-1][1],
        strong=rate is not None and len(recent) >= STRONG_AFTER and rate >= STRONG_RATE,
        outcomes=outcomes,
    )


def versions_of(record: Record, records: Iterable[Record]) -> tuple[Record, ...]:
    """The routine's versions among ``records``: itself and every one it replaced."""

    older = set(record.supersedes)
    return (record, *(other for other in records if other.id in older))


def stats_words(stats: RoutineStats) -> str:
    """"streak 3; done 6 of the last 8 (75%); strong" - or, before any occurrence is over, "no
    occurrence yet"."""

    if stats.rate is None:
        return "no occurrence yet"
    last = f"the last {stats.counted}" if stats.counted > 1 else "the last one"
    words = f"streak {stats.streak}; done {stats.done} of {last} ({round(stats.rate * 100)}%)"
    if stats.slipping:
        words += "; slipping"
    elif stats.strong:
        words += "; strong"
    return words
