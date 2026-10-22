"""Deterministic time-word resolution, fixed to Asia/Singapore (+08:00, no DST since 1982).

A fixed offset avoids adding the tzdata package to an offline, frozen lock
(docs/product/steps/c-record-format.md, Proposed - Nic to confirm).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from brain_core.errors import BrainError, ErrorCode

SINGAPORE_OFFSET_HOURS = 8
SINGAPORE = timezone(timedelta(hours=SINGAPORE_OFFSET_HOURS), name="Asia/Singapore")

# His day runs 4am to 4am (work-model plan section 5.4). The old resolvers keep the
# calendar date; personal_today and read_when are what use this line.
DAY_STARTS = "04:00"

# A deadline word in the phrase (plan 5.2; memo 2.11). Leading for read_when's kind;
# anywhere, as a whole word, for an agent's reading.
DEADLINE_WORDS = ("by", "before", "due", "latest")

NOT_IN_MESSAGE = "not_in_message"
PAST_DAY = "past_day"
DEADLINE_WITHOUT_WORD = "deadline_without_deadline_word"

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
# Spoken short forms the old resolver does not accept. read_when maps them onto
# _WEEKDAYS; resolve, find and find_one never see them.
_READ_WEEKDAYS = {
    "tues": "tuesday",
    "weds": "wednesday",
    "thur": "thursday",
    "thurs": "thursday",
}
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTHS: dict[str, int] = {}
for _index, _name in enumerate(_MONTH_NAMES, start=1):
    _MONTHS[_name] = _index
    _MONTHS[_name[:3]] = _index

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_DAY = re.compile(r"^(?P<month>[a-z]+)\s+(?P<day>\d{1,2})$")
_DAY_MONTH = re.compile(r"^(?P<day>\d{1,2})\s+(?P<month>[a-z]+)$")

# One leading preposition/determiner is stripped before matching ("on Friday", "by
# Friday", "at 7pm", "this Friday", "for Friday"); "next <weekday>" is untouched since
# "next" is not in this list (docs/product/steps/c-record-format.md 2c).
_LEADING_STOPWORDS = ("on ", "by ", "at ", "this ", "for ")
_FILLER_WORDS = frozenset(stopword.strip() for stopword in _LEADING_STOPWORDS)

# "tonight"/"this evening"/"this morning"/"this afternoon" resolve to today, with an
# optional trailing time ("tonight at 7pm"). Checked before the generic stopword strip
# so "this evening" is not mistaken for stripped-"this" + the unrecognized word "evening".
_TODAY_PHRASES = ("tonight", "this evening", "this morning", "this afternoon")

# Speed-03 (Proposed - Nic to confirm; docs/product/steps/c-record-format.md §5). A week
# or weekend resolves to a window, not a due date: "this week" is today to Sunday, "next
# week" the following Monday to Sunday; "this weekend" is this week's Saturday (today on a
# Sunday) to Sunday, "next weekend" the following week's. "'s" and a plural "s" are the
# same phrase ("next week's exam", "next weeks exam").
_WINDOW_PHRASE = re.compile(r"^(?P<which>this|next) (?P<span>week|weekend)(?:'s|s)?$")

# Texting forms that can only mean one thing (speed-03, Proposed).
_ABBREVIATIONS = {"tmr": "tomorrow", "tmrw": "tomorrow", "tonite": "tonight"}

# One leading "later" says only that it is still to come ("later 12am", "later today",
# "later at 7pm"); it is dropped before the rules run (speed-03, Proposed).
_LATER = "later "

# A part of the day after a date names the date with no time ("tomorrow morning", "Friday
# night"); a time may still follow ("tomorrow morning at 9am") or come first (speed-03,
# Proposed).
_PARTS_OF_DAY = ("morning", "afternoon", "evening", "night")

# Trailing parts of a day that name no clock (plan 5.3). Kept in the phrase's words.
# Longest first. A phrase that is only one of these is not a date ("in the morning").
_KEPT_DAY_PARTS = (
    "in the morning",
    "in the afternoon",
    "in the evening",
    "in the night",
    "after breakfast",
    "after lunch",
    "after dinner",
    "after work",
    "before lunch",
    "at lunch",
    "morning",
    "afternoon",
    "evening",
    "night",
)

_START_BY_PREFIXES = ("start on it by ", "start by ")
_WINDOW_ALIASES = {"the weekend": "this weekend", "the week": "this week"}
_END_OF_MONTH = re.compile(r"^end of (?:the |this )?(?P<month>[a-z]+)$")
_IN_COUNT = re.compile(r"^in (?P<n>[a-z0-9]+) (?P<unit>day|days|week|weeks)$")
_IN_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# After "tonight" or "<date> night", a time before 06:00 is the small hours that follow:
# "tonight at 12am" and "12am tonight" are the coming midnight (speed-03, Proposed). The review
# and step F's ranking count a start time that early tomorrow as tonight, by the same line.
NIGHT_ENDS = "06:00"

# 12-hour ("7pm", "7 pm", "7:30pm") and 24-hour ("19:00") clock times. A bare hour with
# neither am/pm nor a colon never matches: nothing is guessed about which half of the
# day it means (docs/product/steps/c-record-format.md 2c; P2). A colon time whose hour
# could also be a 12-hour clock hour (1-12, e.g. "7:30") is likewise refused without
# am/pm - only an hour that is unambiguously 24-hour notation ("00", "13"-"23", e.g.
# "19:00") is accepted with no meridiem, so nothing about AM/PM is ever guessed.
_TIME_12H = re.compile(r"^(?P<hour>0?[1-9]|1[0-2])(?::(?P<minute>[0-5][0-9]))?\s*(?P<meridiem>am|pm)$")
_TIME_24H = re.compile(r"^(?P<hour>00|1[3-9]|2[0-3]):(?P<minute>[0-5][0-9])$")

# find()'s free-text scan tries windows up to this many whitespace-separated tokens
# wide (enough for "on next Friday at 7pm"), longest first, at each start position.
_MAX_PHRASE_WIDTH = 5

# Trailing sentence punctuation ("...on Friday.") is trimmed off a candidate before it
# is tested and reported; the reported phrase is still an exact (shorter) substring of
# the input, so R7's "must appear in words" still holds.
_TRAILING_PUNCTUATION = ".,!?;:"

# What from_words() trims off each word's ends before comparing: sentence punctuation,
# quotes and brackets, straight or curly.
_WORD_EDGES = _TRAILING_PUNCTUATION + "\"'()[]‘’“”"

# Three-letter weekday forms are ordinary English words too ("sun", "sat", "wed",
# "mon"), so find_one() never picks a phrase that leans on one out of free text: "put
# the plant in the sun" must not become due Sunday (S5: nothing is guessed). resolve()
# still accepts them when an agent names them as time_words.
_ABBREVIATED_WEEKDAYS = frozenset(name for name in _WEEKDAYS if len(name) == 3)

# find()'s acceptance probe only needs to know whether a candidate resolves at all,
# never to what date - the caller (_raw_memory_data) runs before the clock is read, so
# no real "today" exists yet. The probe date's only property that matters is being a
# valid date; which one is arbitrary.
_PROBE_TODAY = date(2024, 1, 1)


@dataclass(frozen=True, slots=True)
class ResolvedTime:
    """A resolved calendar date and, when the phrase named one, a time of day; or, for a
    week or weekend phrase, a window (start and end dates) with no due date."""

    due: str | None
    at: str | None = None
    window: tuple[str, str] | None = None


def today_in_singapore(now: datetime) -> date:
    """The calendar date in Asia/Singapore for one timezone-aware moment."""

    if now.tzinfo is None:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "now requires a timezone", details={"field": "now"})
    return now.astimezone(SINGAPORE).date()


def _strip_leading_stopword(text: str) -> str:
    for stopword in _LEADING_STOPWORDS:
        if text.startswith(stopword):
            return text[len(stopword):]
    return text


def _resolve_date_words(text: str, today: date) -> str | None:
    """Match ``text`` (already lowercased) against the fixed date-word rules.

    ``None`` when nothing matches, including a structurally-shaped but invalid
    calendar date (e.g. "2026-02-30") - the caller decides how to report that.
    Shared by :func:`resolve` and :func:`resolve_date_and_time` so both apply the
    exact same rules.
    """

    if text == "today":
        return today.isoformat()
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    next_week = text.startswith("next ")
    bare = text[len("next "):] if next_week else text
    if bare in _WEEKDAYS:
        target = _WEEKDAYS[bare]
        if next_week:
            days_to_monday = (7 - today.weekday()) % 7 or 7
            monday = today + timedelta(days=days_to_monday)
            return (monday + timedelta(days=target)).isoformat()
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (today + timedelta(days=days_ahead)).isoformat()

    iso_match = _ISO_DATE.fullmatch(text)
    if iso_match is not None:
        year, month, day = (int(part) for part in iso_match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    for pattern in (_MONTH_DAY, _DAY_MONTH):
        match = pattern.fullmatch(text)
        if match is None:
            continue
        month_name = match.group("month")
        if month_name not in _MONTHS:
            continue
        month = _MONTHS[month_name]
        day = int(match.group("day"))
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return None
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate.isoformat()

    return None


def resolve(words: str, *, today: date) -> str:
    """Resolve one time phrase to an ISO date (YYYY-MM-DD); refuse anything unrecognized.

    Rules (docs/product/steps/c-record-format.md §5):
    - "today", "tomorrow": that date.
    - a weekday (full name or three letters, e.g. "Fri"), optionally "this <weekday>":
      the next such day strictly after today.
    - "next <weekday>": that weekday in the following Monday-to-Sunday week.
    - "D Month", "Month D", "YYYY-MM-DD": that date; a day-month without a year is the
      next one on or after today.
    - one leading "on"/"by"/"at"/"this"/"for" is stripped before matching.
    - anything else is refused (time_unresolved): nothing is guessed.

    This is the original, date-only contract; :func:`resolve_date_and_time` extends it
    with an optional time of day (P2) without changing what this function returns.
    """

    text = _strip_leading_stopword(words.strip().lower())
    result = _resolve_date_words(text, today)
    if result is None:
        raise BrainError(ErrorCode.TIME_UNRESOLVED, f"Unresolvable time words: {words}", details={"words": words})
    return result


def _parse_clock(text: str) -> str | None:
    """One time-of-day phrase (no leading "at") to 24-hour "HH:MM"; ``None`` if ``text``
    is not a recognized time. A colon time with no am/pm ("19:00") is always read as the
    24-hour clock; a bare hour with neither am/pm nor a colon ("7") never matches.
    """

    candidate = text.strip()
    match = _TIME_12H.fullmatch(candidate)
    if match is not None:
        hour = int(match.group("hour")) % 12
        if match.group("meridiem") == "pm":
            hour += 12
        minute = int(match.group("minute") or "0")
        return f"{hour:02d}:{minute:02d}"
    match = _TIME_24H.fullmatch(candidate)
    if match is not None:
        return f"{int(match.group('hour')):02d}:{match.group('minute')}"
    return None


def _extract_time(text: str) -> tuple[str, str | None]:
    """Split a trailing time expression off ``text`` (already lowercased/stripped).

    Tries the last two whitespace-separated tokens first ("at 7pm", "7 pm"), then the
    last one ("7pm", "19:00"), so a date phrase before it is left intact. Returns
    ``(text, None)`` unchanged when no trailing time is found.
    """

    tokens = text.split()
    for width in (2, 1):
        if len(tokens) < width:
            continue
        chunk = " ".join(tokens[-width:])
        candidate = chunk[3:] if chunk.startswith("at ") else chunk
        at = _parse_clock(candidate)
        if at is not None:
            return " ".join(tokens[: len(tokens) - width]), at
    return text, None


def _extract_leading_time(text: str) -> tuple[str | None, str]:
    """Split a leading time expression off ``text`` ("7pm tomorrow", "at 7pm on Friday",
    "7 pm Friday"), the mirror of :func:`_extract_time`. ``(None, text)`` when there is
    none, or when nothing follows it (a time alone is :func:`_extract_time`'s case).
    """

    tokens = text.split()
    for width in (2, 1):
        if len(tokens) <= width:
            continue
        chunk = " ".join(tokens[:width])
        candidate = chunk[3:] if chunk.startswith("at ") else chunk
        at = _parse_clock(candidate)
        if at is not None:
            return at, " ".join(tokens[width:])
    return None, text


def _normalise(words: str) -> str:
    """Lowercase, single spaces, a curly apostrophe made straight, and the texting forms in
    ``_ABBREVIATIONS`` spelled out."""

    text = words.replace("’", "'").lower()
    return " ".join(_ABBREVIATIONS.get(token, token) for token in text.split())


def _resolve_window(text: str, today: date) -> tuple[str, str] | None:
    """A week or weekend phrase (already normalised) to ``(start, end)``; never starts before today."""

    match = _WINDOW_PHRASE.fullmatch(text)
    if match is None:
        return None
    monday = today - timedelta(days=today.weekday())
    if match.group("which") == "next":
        monday += timedelta(days=7)
    sunday = monday + timedelta(days=6)
    start = monday + timedelta(days=5) if match.group("span") == "weekend" else monday
    return max(start, today).isoformat(), sunday.isoformat()


def _resolve_day(text: str, today: date) -> tuple[date, bool] | None:
    """A date phrase, optionally ending in a part of the day ("tomorrow morning", "Friday
    night"): ``(date, whether it names a night)``, or ``None`` if it is not a date."""

    if text in _TODAY_PHRASES:
        return today, text == "tonight"
    text = _strip_leading_stopword(text)
    if text in _TODAY_PHRASES:
        return today, text == "tonight"
    tokens = text.split()
    part = tokens[-1] if len(tokens) > 1 and tokens[-1] in _PARTS_OF_DAY else None
    if part is not None:
        text = _strip_leading_stopword(" ".join(tokens[:-1]))
    resolved = _resolve_date_words(text, today)
    if resolved is None:
        return None
    return date.fromisoformat(resolved), part == "night"


def _dated(day: date, at: str | None, *, night: bool) -> ResolvedTime:
    if at is not None and night and at < NIGHT_ENDS:
        day += timedelta(days=1)  # the small hours after that night
    return ResolvedTime(day.isoformat(), at)


def _resolve_phrase(text: str, today: date, now: time | None) -> ResolvedTime | None:
    window = _resolve_window(text, today) or _resolve_window(_strip_leading_stopword(text), today)
    if window is not None:
        return ResolvedTime(None, None, window)

    for phrase in _TODAY_PHRASES:
        if text == phrase:
            return ResolvedTime(today.isoformat(), None)
        prefix = f"{phrase} "
        if text.startswith(prefix):
            at = _parse_clock(_strip_leading_stopword(text[len(prefix):]))
            return None if at is None else _dated(today, at, night=phrase == "tonight")

    stripped = _strip_leading_stopword(text)
    date_part, at = _extract_time(stripped)
    if at is None:
        at, date_part = _extract_leading_time(stripped)
    if not date_part:
        if at is None:
            return None
        # A time alone: today, or tomorrow once that time has passed today (speed-03).
        if now is not None and at < now.strftime("%H:%M"):
            return ResolvedTime((today + timedelta(days=1)).isoformat(), at)
        return ResolvedTime(today.isoformat(), at)
    day = _resolve_day(date_part, today)
    return None if day is None else _dated(day[0], at, night=day[1])


def resolve_date_and_time(words: str, *, today: date, now: time | None = None) -> ResolvedTime:
    """Resolve one phrase to a date and, when the phrase names one, a time (P2), or to a
    window for a week or weekend phrase (speed-03).

    Extends :func:`resolve`'s rules with an optional time of day: "7pm", "7 pm",
    "7:30pm", "19:00", "at 7pm" resolve to ``at`` ("HH:MM", 24-hour, Singapore). A date
    and a time together, in either order ("Friday 7pm", "7pm tomorrow", "tonight at
    7pm"), resolve both. A time alone ("7pm") resolves to today with that time - **Proposed,
    Nic to confirm** (docs/product/steps/c-record-format.md P2) - or, when ``now`` (the
    Singapore time of day) is given and that time has already passed today, to tomorrow
    (speed-03, Proposed). A bare hour with no am/pm and no colon ("7", "at 7") is refused
    (time_unresolved): nothing is guessed.

    Speed-03 also accepts (Proposed, §5): "this/next week(end)" as a window; one leading
    "later"; "tmr", "tmrw", "tonite"; a part of the day after a date ("tomorrow
    morning"); and, after "tonight" or "<date> night", a time before 06:00 as the small
    hours that follow ("12am tonight" is the coming midnight).
    """

    text = _normalise(words)
    if text.startswith(_LATER):
        text = text[len(_LATER):]
    resolved = _resolve_phrase(text, today, now)
    if resolved is None:
        raise BrainError(ErrorCode.TIME_UNRESOLVED, f"Unresolvable time words: {words}", details={"words": words})
    return resolved


def _is_resolvable(candidate: str) -> bool:
    try:
        resolve_date_and_time(candidate, today=_PROBE_TODAY)
    except BrainError:
        return False
    return True


def find(words: str) -> tuple[str, ...]:
    """The time phrases inside free text that :func:`resolve_date_and_time` could
    resolve on their own - scanned left to right, longest window first (up to
    ``_MAX_PHRASE_WIDTH`` whitespace-separated tokens), non-overlapping.

    Every result is an exact substring of ``words`` (so R7's "a time_words the agent
    gives must still appear in words" always holds for a found phrase too).
    Deterministic; finds nothing it is not sure of - nothing is guessed. Callers never
    pass ``today`` because this runs before the clock is read (e.g.
    ``_raw_memory_data``, before ``accepted_at`` exists); acceptance never depends on
    which day it is, only the resolved date would.
    """

    if not words:
        return ()
    spans = [(match.start(), match.end()) for match in re.finditer(r"\S+", words)]
    found: list[str] = []
    index = 0
    total = len(spans)
    while index < total:
        matched = False
        for width in range(min(_MAX_PHRASE_WIDTH, total - index), 0, -1):
            start, end = spans[index][0], spans[index + width - 1][1]
            candidate = words[start:end].rstrip(_TRAILING_PUNCTUATION)
            if candidate and _is_resolvable(candidate):
                found.append(candidate)
                index += width
                matched = True
                break
        if not matched:
            index += 1
    return tuple(found)


def find_one(words: str) -> str | None:
    """The one time phrase in ``words`` that is safe to use unasked, or ``None``.

    A day and a time said apart ("tmr need to bring meshy to vet at 12pm") are joined
    into one phrase ("tmr at 12pm") when, joined, they resolve: that is one moment, not
    two. ``None`` when :func:`find` sees no phrase, two that do not join into one moment,
    more than two, or a phrase that leans on a three-letter weekday form, which is also an
    English word ("in the sun", "I sat down"). The caller then leaves timing empty instead
    of guessing.
    """

    found = find(words)
    if len(found) == 2:
        joined = f"{found[0]} {found[1]}"
        found = (joined,) if _is_resolvable(joined) else found
    if len(found) != 1:
        return None
    tokens = {token.strip(_TRAILING_PUNCTUATION).lower() for token in found[0].split()}
    return None if tokens & _ABBREVIATED_WEEKDAYS else found[0]


def _word_set(text: str) -> set[str]:
    return {word for word in (token.strip(_WORD_EDGES).lower() for token in text.split()) if word}


def says_more(found: str, time_words: str) -> bool:
    """Whether ``found`` names more of the moment than ``time_words`` does: a word beyond
    them that is not a leading filler ("on", "by", "at", "this", "for"). "tmr at 12pm" says
    more than "at 12pm"; "for next weeks" says no more than "next weeks"."""

    return bool(_word_set(found) - _word_set(time_words) - _FILLER_WORDS)


def from_words(time_words: str, words: str) -> bool:
    """Whether ``time_words`` came from ``words`` (R7): as one piece, or word by word from
    anywhere in them, ignoring case and punctuation. "tmr at 12pm" came from "tmr need to
    bring meshy to vet at 12pm"; "next Tuesday" did not come from "pay the bill Friday"."""

    if not time_words or not words:
        return False
    if time_words in words:
        return True
    wanted = _word_set(time_words)
    return bool(wanted) and wanted <= _word_set(words)


@dataclass(frozen=True, slots=True)
class WhenReading:
    """One phrase read as a work-model date. ``read_by`` is ``code`` when code read it.

    A planned day sets ``day``. A planned period sets ``start`` and ``end`` and leaves
    ``day`` empty. A deadline or a start-by sets ``day``. An appointment sets ``day``
    and ``at``. Not-before sets ``day``, ``event``, or both.
    """

    kind: str
    day: str | None = None
    at: str | None = None
    start: str | None = None
    end: str | None = None
    event: str | None = None
    words: str = ""
    read_by: str = "code"


@dataclass(frozen=True, slots=True)
class AgentReadingCheck:
    """The flags from plan section 5.6, and the reading they leave.

    ``reading`` is ``None`` when the date is left off. A deadline with no deadline word
    is kept, with ``kind`` ``planned``, and ``DEADLINE_WITHOUT_WORD`` set.
    """

    flags: tuple[str, ...]
    reading: WhenReading | None


def personal_today(now: datetime) -> date:
    """The Singapore date of ``now`` minus four hours: the day that started at 04:00.

    ``today`` passed to :func:`read_when` is the calendar date, not this value.
    Passing this in as ``today`` as well as a clock before 04:00 shifts the day twice.
    """

    if now.tzinfo is None:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "now requires a timezone", details={"field": "now"})
    local = now.astimezone(SINGAPORE)
    if local.strftime("%H:%M") < DAY_STARTS:
        return local.date() - timedelta(days=1)
    return local.date()


def _has_deadline_word(phrase: str) -> bool:
    return bool(set(_normalise(phrase).split()) & set(DEADLINE_WORDS))


def _last_day(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _expand_read_words(text: str) -> str:
    tokens = [_READ_WEEKDAYS.get(token, token) for token in text.split()]
    return " ".join("12pm" if token in {"noon", "midday"} else token for token in tokens)


def _peel_day_part(text: str) -> str:
    for part in _KEPT_DAY_PARTS:
        if text == part:
            return text
        suffix = f" {part}"
        if text.endswith(suffix) and text[: -len(suffix)].strip():
            return text[: -len(suffix)].strip()
    return text


def _personal_base(today: date, now_clock: time | None) -> date:
    if now_clock is not None and now_clock.strftime("%H:%M") < DAY_STARTS:
        return today - timedelta(days=1)
    return today


def _time_only_clock(text: str) -> str | None:
    stripped = _strip_leading_stopword(text)
    candidate = stripped[3:] if stripped.startswith("at ") else stripped
    return _parse_clock(candidate)


def _time_only_day(personal: date, at: str, today: date, now_clock: time | None) -> date:
    """Where a bare clock sits. With no clock given, it is the base day, as resolve is.

    With a clock, a time before 04:00 is the small hours after that personal day, and a
    moment that has already passed moves to the next personal day.
    """

    if now_clock is None:
        return personal
    calendar = personal if at >= DAY_STARTS else personal + timedelta(days=1)
    at_hour, at_minute = int(at[:2]), int(at[3:])
    moment = calendar.toordinal() * 24 * 60 + at_hour * 60 + at_minute
    now_minutes = now_clock.hour * 60 + now_clock.minute
    if moment < today.toordinal() * 24 * 60 + now_minutes:
        nxt = personal + timedelta(days=1)
        calendar = nxt if at >= DAY_STARTS else nxt + timedelta(days=1)
    return calendar


def _end_of_month(text: str, today: date) -> date | None:
    match = _END_OF_MONTH.fullmatch(text)
    if match is None:
        return None
    name = match.group("month")
    if name == "month":
        return _last_day(today.year, today.month)
    if name not in _MONTHS:
        return None
    month = _MONTHS[name]
    day = _last_day(today.year, month)
    if day < today:
        day = _last_day(today.year + 1, month)
    return day


def _before_month(name: str, today: date) -> date | None:
    """The last day of the month before ``name``, on or after ``today``."""

    if name not in _MONTHS:
        return None
    month = _MONTHS[name]
    year = today.year
    first = date(year, month, 1)
    if first <= today:
        first = date(year + 1, month, 1)
    return first - timedelta(days=1)


def _in_count(text: str, today: date) -> date | None:
    match = _IN_COUNT.fullmatch(text)
    if match is None:
        return None
    raw = match.group("n")
    count = int(raw) if raw.isdigit() else _IN_NUMBERS.get(raw)
    if count is None or count < 1:
        return None
    days = count * 7 if match.group("unit").startswith("week") else count
    try:
        return today + timedelta(days=days)
    except OverflowError:
        return None


def _window_of(text: str, today: date) -> tuple[str, str] | None:
    direct = _resolve_window(text, today) or _resolve_window(_strip_leading_stopword(text), today)
    if direct is not None:
        return direct
    tokens = text.split()
    for width in range(len(tokens) - 1, 1, -1):
        head = " ".join(tokens[:width])
        found = _resolve_window(head, today) or _resolve_window(_strip_leading_stopword(head), today)
        if found is not None:
            return found
    return None


def _resolve_read_phrase(
    text: str, personal: date, calendar_today: date, now_clock: time | None,
) -> tuple[date | None, str | None, tuple[str, str] | None]:
    """A day, a clock and a window for ``text``. The day is ``None`` when only a window matched.

    ``personal`` is the 4am day the relative words use. ``calendar_today`` is the
    Singapore calendar date the clock sits on, so a bare time after midnight is compared
    with that morning and not with the personal day's own midnight.
    """

    clock = _time_only_clock(text)
    if clock is not None:
        return _time_only_day(personal, clock, calendar_today, now_clock), clock, None
    window = _window_of(text, personal)
    if window is not None:
        return None, None, window
    resolved = _resolve_phrase(text, personal, None)
    if resolved is None or resolved.due is None:
        return None, None, None
    return date.fromisoformat(resolved.due), resolved.at, None


def read_when(
    phrase: str,
    *,
    today: date,
    now_clock: time | None = None,
    owed_to: bool = False,
    event: str | None = None,
    past: bool = False,
) -> WhenReading | None:
    """Read one time phrase as a work-model date, or ``None`` when code cannot.

    ``today`` is the Singapore calendar date and ``now_clock`` the clock on that date.
    Before :data:`DAY_STARTS` the relative words use the day before (plan 5.4).
    :func:`resolve`, :func:`resolve_date_and_time`, :func:`find` and :func:`find_one`
    are not used for the phrases this adds, and their results stay as they are.

    Kind, in order: a leading word in :data:`DEADLINE_WORDS`; else a clock time
    (an appointment); else "after" / "not before", or a day passed with ``event``
    (not-before); else ``owed_to`` (a deadline on the day, or on a window's end);
    else a planned day or period. "start by" is a start-by even though it says "by".
    "before <month>" is the last day of the previous month. "before <date>" is the
    day before that date. "by <date>" is that date.
    """

    original = phrase.strip()
    if not original:
        return None
    text = _normalise(original)
    if text.startswith(_LATER):
        text = text[len(_LATER):].strip()
    if not text:
        return None

    kind_force: str | None = None
    for prefix in _START_BY_PREFIXES:
        if text.startswith(prefix):
            kind_force = "start_by"
            text = text[len(prefix):].strip()
            break

    deadline_word: str | None = None
    if kind_force is None:
        for word in DEADLINE_WORDS:
            if text == word or text.startswith(word + " "):
                deadline_word = word
                text = text[len(word):].strip()
                break
    if not text:
        return None
    if text in _KEPT_DAY_PARTS:
        return None

    not_before = False
    if text.startswith("not before "):
        not_before = True
        text = text[len("not before "):].strip()
    elif text.startswith("after ") and deadline_word is None and kind_force is None:
        not_before = True
        text = text[len("after "):].strip()
    if not text:
        return None

    text = _expand_read_words(_peel_day_part(text))
    text = _WINDOW_ALIASES.get(text, text)
    base = _personal_base(today, now_clock)

    day: date | None = None
    at: str | None = None
    window: tuple[str, str] | None = None
    month_boundary = False
    if past:
        text = _strip_leading_stopword(text)
        if text == "yesterday":
            day = base - timedelta(days=1)
        elif text in _WEEKDAYS:
            day = base - timedelta(days=(base.weekday() - _WEEKDAYS[text]) % 7)
        elif text.startswith("last ") and text[5:] in _WEEKDAYS:
            day = base - timedelta(days=(base.weekday() - _WEEKDAYS[text[5:]]) % 7 or 7)
        else:
            for pattern in (_MONTH_DAY, _DAY_MONTH):
                match = pattern.fullmatch(text)
                if match and match.group("month") in _MONTHS:
                    try:
                        day = date(base.year, _MONTHS[match.group("month")], int(match.group("day")))
                    except ValueError:
                        return None
    if deadline_word == "before" and day is None:
        before_month = _before_month(text, base)
        if before_month is not None:
            day = before_month
            month_boundary = True
    if day is None:
        end = _end_of_month(text, base)
        if end is not None:
            day = end
        else:
            counted = _in_count(text, base)
            if counted is not None:
                day = counted
            else:
                day, at, window = _resolve_read_phrase(text, base, today, now_clock)
    # "before <date>" is the day before. "before <month>" already returned above.
    if deadline_word == "before" and day is not None and not month_boundary:
        day -= timedelta(days=1)

    if day is None and window is None:
        if not_before:
            return WhenReading("not_before", event=event or text, words=original)
        return None

    if kind_force == "start_by":
        kind = "start_by"
    elif deadline_word is not None:
        kind = "deadline"
    elif at is not None:
        kind = "appointment"
    elif not_before or event:
        kind = "not_before"
    elif owed_to:
        kind = "deadline"
    else:
        kind = "planned"

    start = end_day = None
    if window is not None and kind in {"planned"} and not owed_to and deadline_word is None and not not_before and not event:
        start, end_day = window
        day = None
    elif window is not None:
        # A deadline, a promise, a start-by or a not-before lands on one day of the window.
        edge = window[0] if kind == "not_before" else window[1]
        day = date.fromisoformat(edge)
        if kind == "not_before" and deadline_word is None:
            kind = "not_before"

    if kind == "start_by" and day is None:
        return None
    if kind == "appointment" and (day is None or at is None):
        return None
    if kind == "not_before" and day is None and not (event or not_before):
        return None

    return WhenReading(
        kind,
        None if day is None else day.isoformat(),
        at,
        start,
        end_day,
        event,
        original,
    )


def _agent_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _reading_fields(reading: WhenReading | dict[str, object]) -> dict[str, object]:
    if isinstance(reading, WhenReading):
        return {
            "kind": reading.kind,
            "day": reading.day,
            "at": reading.at,
            "start": reading.start,
            "end": reading.end,
            "event": reading.event,
            "read_by": reading.read_by,
        }
    if isinstance(reading, dict):
        return reading
    raise BrainError(
        ErrorCode.INVALID_ARGUMENT,
        "reading must be a WhenReading or a mapping",
        details={"field": "reading"},
    )


def check_agent_reading(
    phrase: str,
    message: str,
    reading: WhenReading | dict[str, object],
    today: date,
    *,
    owed_to: bool = False,
    allow_past: bool = False,
) -> AgentReadingCheck:
    """Guard an agent's date for a phrase :func:`read_when` could not read (plan 5.6).

    ``today`` is his personal today. The date is left off when ``phrase`` is not in
    ``message``, or the day is before ``today`` unless ``allow_past`` is set for
    completion dates. A deadline with no deadline word in
    the phrase, and no ``owed_to``, is returned as a planned day.
    """

    fields = _reading_fields(reading)
    flags: list[str] = []
    if not from_words(phrase, message):
        flags.append(NOT_IN_MESSAGE)

    day = _agent_date(fields.get("day"))
    end = _agent_date(fields.get("end"))
    start = _agent_date(fields.get("start"))
    named = fields.get("day") or fields.get("end") or fields.get("start")
    when = day or end or start
    if isinstance(named, str) and named and (when is None or (when < today and not allow_past)):
        flags.append(PAST_DAY)

    kind = str(fields.get("kind") or "planned").lower()
    if kind == "deadline" and not owed_to and not _has_deadline_word(phrase):
        flags.append(DEADLINE_WITHOUT_WORD)
        kind = "planned"

    if NOT_IN_MESSAGE in flags or PAST_DAY in flags:
        return AgentReadingCheck(tuple(flags), None)

    def _text(name: str) -> str | None:
        value = fields.get(name)
        return value if isinstance(value, str) and value else None

    return AgentReadingCheck(
        tuple(flags),
        WhenReading(
            kind,
            None if day is None else day.isoformat(),
            _text("at"),
            None if start is None else start.isoformat(),
            None if end is None else end.isoformat(),
            _text("event"),
            phrase.strip(),
            _text("read_by") or "",
        ),
    )
