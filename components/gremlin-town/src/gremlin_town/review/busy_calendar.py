"""Explicit day changes in FlowSavvy mode, through the existing calendar writer.

Proposed: a finish time blocks the rest of that Singapore day; a later finish
change moves that one owned entry. Explicit intervals are separate busy entries.
The why-log freezes operation intent before Google, and uncertain writes can
only be reconciled by reading. This never writes a Brain Home record.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from brain_core.timewords import SINGAPORE
from gremlin_town.sources.google.calendar_writer import CalendarWriteError, EventSlot

_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
CHECK_WORDS = "Couldn't confirm the calendar change. It needs checking before another change to this time."


class BusyChangeError(ValueError):
    """Invalid or unavailable day control; nothing should be recorded or written."""


@dataclass(frozen=True)
class BusyChange:
    kind: str
    slot: EventSlot
    operation_id: str | None = None
    reconcile_only: bool = False


def parse_busy_change(detail: str | None, now: datetime, *, why_log=None, caller: str | None = None) -> BusyChange:
    try:
        value = json.loads(detail or "")
    except (ValueError, TypeError) as exc:
        raise BusyChangeError("Give a finish time or a start and end to block out.") from exc
    if not isinstance(value, dict) or set(value) - {"ends_at", "busy", "words", "operation_id"}:
        raise BusyChangeError("In schedule mode, give a finish time or a start and end to block out. Change working hours in FlowSavvy.")
    if ("ends_at" in value) == ("busy" in value):
        raise BusyChangeError("Give either a finish time or a start and end to block out.")
    if "words" in value and (not isinstance(value["words"], str) or not value["words"].strip()):
        raise BusyChangeError("words must contain Nic's words.")
    operation_id = value.get("operation_id")
    if "operation_id" in value and (not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", operation_id)):
        raise BusyChangeError("operation_id must be a stable 8 to 128 character retry key.")
    local = now.astimezone(SINGAPORE)
    # Relative clock text must resolve against the original operation's date,
    # never tomorrow merely because a lost-response retry crossed midnight.
    stored = None
    if why_log is not None and caller is not None and operation_id:
        stored = why_log.busy_change(_hash([caller, operation_id]))
        if stored is not None:
            anchor = 1
            busy = value.get("busy")
            if isinstance(busy, dict):
                raw_start, raw_end = busy.get("start"), busy.get("end")
                if (not isinstance(raw_start, str) or not _CLOCK.fullmatch(raw_start)) and (
                        isinstance(raw_end, str) and _CLOCK.fullmatch(raw_end)):
                    anchor = 2
            local = datetime.fromisoformat(stored["intent"][anchor]).astimezone(SINGAPORE)

    def moment(raw: Any) -> datetime:
        if not isinstance(raw, str):
            raise BusyChangeError("Use HH:MM for today, or a date and time with its time zone.")
        if _CLOCK.fullmatch(raw):
            return datetime.combine(local.date(), time.fromisoformat(raw), SINGAPORE)
        try:
            result = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise BusyChangeError("Use HH:MM for today, or a date and time with its time zone.") from exc
        if result.utcoffset() is None:
            raise BusyChangeError("A dated time needs its time zone.")
        return result.astimezone(SINGAPORE)

    if "ends_at" in value:
        if not isinstance(value["ends_at"], str) or not _CLOCK.fullmatch(value["ends_at"]):
            raise BusyChangeError("Give today's finish time as HH:MM.")
        start = moment(value["ends_at"])
        end = datetime.combine(local.date() + timedelta(days=1), time(), SINGAPORE)
        kind = "finish"
    else:
        busy = value["busy"]
        if not isinstance(busy, dict) or set(busy) != {"start", "end"}:
            raise BusyChangeError("Give both the start and end of the time to block out.")
        start, end = moment(busy["start"]), moment(busy["end"])
        kind = "interval"
    if end <= start:
        raise BusyChangeError("The end must follow the start and still be in the future.")
    intent = [kind, start.isoformat(), end.isoformat()]
    if stored is None and why_log is not None and caller is not None:
        key = _hash([caller, operation_id] if operation_id else [caller, *intent])
        stored = why_log.busy_change(key)
    if stored is not None and stored["intent"] != intent:
        raise BusyChangeError("This retry does not match the saved calendar instruction.")
    relative = kind == "finish" or any(_CLOCK.fullmatch(value["busy"][key]) for key in ("start", "end"))
    if relative and operation_id is None and stored is None:
        raise BusyChangeError("Relative calendar times need an operation_id so retries keep the same day.")
    elapsed = end <= now or (kind == "finish" and start < now)
    if elapsed and stored is None:
        raise BusyChangeError("That time has passed. Give a finish time from now onward, or an interval that has not ended.")
    return BusyChange(kind, EventSlot(start, end), operation_id, reconcile_only=elapsed)


def _hash(parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def apply_busy_change(why_log, writer, change: BusyChange, *, caller: str, now: datetime) -> dict[str, Any]:
    slot = change.slot
    intent = [change.kind, slot.start.isoformat(), slot.end.isoformat()]
    # One finish entry per day; interval entries are keyed by their exact range.
    entry_key = _hash(["busy", caller, change.kind,
                       slot.start.date().isoformat() if change.kind == "finish" else intent])
    operation_key = _hash([caller, change.operation_id] if change.operation_id else [caller, *intent])
    stored = why_log.prepare_busy_change(operation_key, entry_key, intent)
    if stored["state"] == "done":
        return stored["result"]
    if stored["state"] == "blocked":
        return {"status": "needs_check", "text": CHECK_WORDS}
    try:
        current = writer.find_busy(entry_key)
        if current is not None and current.has_guests:
            return {"status": "needs_check", "text": "That calendar entry has guests now, so it was left as it is."}
        if current is None or current.slot != slot:
            if change.reconcile_only or slot.end <= now or (change.kind == "finish" and slot.start < now):
                return {"status": "needs_check", "text": CHECK_WORDS}
            # Compare-and-swap gives a single caller permission to attempt this
            # mutation. A lost reply never permits another mutation on a retry.
            if not why_log.attempt_busy_change(operation_key):
                return {"status": "needs_check", "text": CHECK_WORDS}
            writer.write_busy(key=entry_key, slot=slot, current=current, now=now)
        result = {"status": "blocked", "text": "Blocked out on your calendar.",
                  "start": slot.start.isoformat(), "end": slot.end.isoformat()}
        why_log.complete_busy_change(operation_key, result)
        return result
    except (CalendarWriteError, ValueError):
        # Provider errors contain no user content, but the public reply needs no
        # provider detail. Preserve the pending operation for independent reads.
        return {"status": "needs_check", "text": CHECK_WORDS}
