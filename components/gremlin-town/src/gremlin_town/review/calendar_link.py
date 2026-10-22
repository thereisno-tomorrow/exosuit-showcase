"""Legacy calendar marker reads; suggested entries and add-event links are retired."""

from __future__ import annotations


from brain_core.review import CalendarEvent

MARKER_PREFIX = "exosuit:"


def marker(record_id: str) -> str:
    return f"{MARKER_PREFIX}{record_id}"


def marked_record_ids(events: tuple[CalendarEvent, ...]) -> frozenset[str]:
    """Record ids whose suggested entry is already on the calendar (§5, "stops once added")."""

    found: set[str] = set()
    for event in events:
        for token in (event.description or "").split():
            if token.startswith(MARKER_PREFIX):
                found.add(token[len(MARKER_PREFIX):])
    return frozenset(found)
