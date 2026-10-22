"""Appointment entries and reconciliation (work-model W10, step E section 5).

Only accepted, open appointments get entries. Legacy due+at remains readable, but
no date alone becomes all-day and work dates never fall back to legacy timing.
Only the entry's lasting first-ID marker is touched; no guests are added, notified,
moved or removed. Closing a task asks about removal and never deletes automatically.
The separate configured-door-participant removal call checks Nic's yes first.
A Google failure does not undo the Brain save. No installation is implied here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from brain_core.records import Record
from brain_core.timewords import SINGAPORE

from gremlin_town.sources.google.calendar_writer import (
    CalendarEntry,
    CalendarWriteError,
    CalendarWriter,
    EventSlot,
)

DEFAULT_MINUTES = 30
OPEN_STATES = frozenset({"open", "waiting", "blocked"})
CLOSED_STATES = frozenset({"done", "dropped", "no_longer_needed"})
REMOVE_QUESTION = "Remove it from your calendar?"
# A block with one of these means the task already has an entry, so no tap-to-add link.
ENTRY_EXISTS = frozenset({"on_calendar", "moved", "has_guests"})

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def marker_id(record: Record) -> str:
    """The task's first record ID: the same through every change of the task."""

    return record.supersedes[0] if record.supersedes else record.id


def has_start_time(record: Record) -> bool:
    """An accepted, still-open item with a date and a time: one that gets an entry by itself."""

    if record.kind != "item" or record.data.get("level") != "accepted" or record.state not in OPEN_STATES:
        return False
    if (getattr(record, "source", None) or {}).get("said_by") == "other":
        return False
    return slot_for(record) is not None


def slot_for(record: Record) -> EventSlot | None:
    """Timed appointment slot; legacy due+at remains readable, never all-day.

    Work-model dates are authoritative: a deadline/planned day never falls back to timing.
    """

    timing = record.data.get("timing") or {}
    if "dates" in record.data:
        appointment = record.data["dates"].get("appointment") or {}
        due, at = appointment.get("day"), appointment.get("at")
    else:
        due, at = timing.get("due"), timing.get("at")
    if due and at:
        start = datetime.combine(date.fromisoformat(due), time.fromisoformat(at), tzinfo=SINGAPORE)
        minutes = timing.get("duration_minutes") or DEFAULT_MINUTES
        return EventSlot(start, start + timedelta(minutes=minutes))
    return None


def _date_word(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _clock_word(moment: datetime) -> str:
    meridiem = "am" if moment.hour < 12 else "pm"
    hour = moment.hour % 12 or 12
    return f"{hour} {meridiem}" if moment.minute == 0 else f"{hour}:{moment.minute:02d} {meridiem}"


def _minutes(slot: EventSlot) -> int:
    return int((slot.end - slot.start).total_seconds() // 60)


def slot_words(slot: EventSlot) -> str:
    """``"Fri 2 Oct 7 pm, 30 min"``; ``"Sat 3 Oct, all day"``; ``"Mon 5 Oct to Sun 11 Oct, all day"``."""

    if slot.all_day:
        last = slot.end - timedelta(days=1)
        if last == slot.start:
            return f"{_date_word(slot.start)}, all day"
        return f"{_date_word(slot.start)} to {_date_word(last)}, all day"
    local = slot.start.astimezone(SINGAPORE)
    return f"{_date_word(local.date())} {_clock_word(local)}, {_minutes(slot)} min"


def _block(status: str, text: str, slot: EventSlot | None = None, **extra: object) -> dict[str, object]:
    block: dict[str, object] = {"status": status, "text": text}
    if slot is not None:
        block.update(start=slot.start.isoformat(), end=slot.end.isoformat(), all_day=slot.all_day)
        if not slot.all_day:
            block["minutes"] = _minutes(slot)
    block.update(extra)
    return block


def failed(action: str, reason: str) -> dict[str, object]:
    """The calendar part failed; the Brain save it follows still stands."""

    return _block("failed", f"Saved, but {action} failed ({reason})", reason=reason)


def follow(writer: CalendarWriter, record: Record, *, changed: bool,
           keep_past_done: bool = False, now: datetime | None = None) -> dict[str, object] | None:
    """The calendar part of one save (``changed=False``) or change, or ``None`` when there is none.

    A save only ever needs an entry for a task with a start time; nothing else can have one yet.
    A change also looks for an entry to move, or to ask about removing.
    """

    if keep_past_done and (now is None or now.tzinfo is None):
        raise ValueError("Keeping a past appointment requires the aware host clock")
    if record.kind != "item":
        return None
    if (record.source or {}).get("said_by") == "other":
        return None
    accepted_open = record.data.get("level") == "accepted" and record.state in OPEN_STATES
    closed = record.state in CLOSED_STATES
    wanted = slot_for(record) if accepted_open else None
    timed = wanted is not None and not wanted.all_day
    if not (timed or (changed and (closed or wanted is not None))):
        return None

    key = marker_id(record)
    try:
        entry = writer.find(key)
    except CalendarWriteError as exc:
        return failed("checking your calendar", exc.reason.value)

    if entry is None:
        if not timed:
            return None
        try:
            writer.insert(title=record.title, slot=wanted, marker_id=key)
        except CalendarWriteError as exc:
            return failed("adding it to your calendar", exc.reason.value)
        return _block("added", f"Added to your calendar, {slot_words(wanted)}, under your calendar limit", wanted)

    if keep_past_done and record.state == "done" and entry.slot is not None:
        end = (datetime.combine(entry.slot.end, time.min, SINGAPORE)
               if entry.slot.all_day else entry.slot.end)
        if end <= now:
            return _block("kept", "Done. Kept its past calendar entry.", entry.slot)
    if entry.has_guests:
        return _block("has_guests", "Its calendar entry has guests now, so Exosuit left it as it is", entry.slot)
    if closed:
        where = f", {slot_words(entry.slot)}" if entry.slot is not None else ""
        return _block("ask_remove", f"It's on your calendar{where}. {REMOVE_QUESTION}", entry.slot,
                      question=REMOVE_QUESTION)
    if wanted is None:
        return None  # parked, or no longer accepted: its entry is left as it is
    if entry.slot == wanted:
        return _block("on_calendar", f"On your calendar, {slot_words(wanted)}", wanted)
    try:
        writer.move(entry, wanted)
    except CalendarWriteError as exc:
        return failed("moving its calendar entry", exc.reason.value)
    return _block("moved", f"Moved on your calendar to {slot_words(wanted)}, under your calendar limit", wanted)


def remove(writer: CalendarWriter, record: Record) -> tuple[str, str, str | None]:
    """Remove the entry found by a done or dropped task's marker, after Nic's yes.

    Returns ``(status, text, reason)``. The caller has already checked the task is the newest
    version, done or dropped, and that the call carries Nic's own words.
    """

    try:
        entry: CalendarEntry | None = writer.find(marker_id(record))
        if entry is None:
            return "not_found", "No calendar entry from Exosuit is on your calendar for this task", None
        if entry.has_guests:
            return "has_guests", "Its calendar entry has guests now, so Exosuit left it as it is", None
        writer.delete(entry)
    except CalendarWriteError as exc:
        return "failed", f"Removing it from your calendar failed ({exc.reason.value})", exc.reason.value
    return "removed", "Removed from your calendar", None
