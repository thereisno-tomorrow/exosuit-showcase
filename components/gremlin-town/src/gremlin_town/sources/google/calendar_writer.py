"""Exosuit's own calendar entries: find, add, move and remove them (N8; step E spec §5).

The one Google write Exosuit makes (step D spec §1, changed 28 Sep 2026). It writes only to
Nic's primary calendar, and only ever touches entries it added itself, found by their marker:

- the description ends with ``exosuit:<record_id>``, the same marker the tap-to-add link puts
  in its details and the calendar reader already reads back; and
- a private extended property ``exosuit`` holds the same record ID, so an entry is found with
  one exact query instead of a text search. An entry Nic added himself from a tap-to-add link
  has no such property, so nothing here ever moves or removes it.

No guests, ever: an entry is written with no ``attendees``, and every write sends
``sendUpdates=none``. An entry someone has since given guests is never moved or removed here
(``CalendarEntry.has_guests``), since that would change other people's calendars too.

Nothing here decides *whether* to write; ``gremlin_town.review.calendar_entries`` does. A
failure raises ``CalendarWriteError`` with one of step D's reason codes (§3.4) and never
carries secret material.

**The placement engine's entries** (step I §4; build plan slice 3) are a second kind, kept apart:
each block and task slot carries the private property ``exosuit_day`` (its date, so one exact
query lists a day's entries) and either ``exosuit_block`` or ``exosuit_slot`` (which one it is),
never N8's ``exosuit``. So N8's ``find`` never returns them and ``list_placed`` never returns an
N8 entry. ``move_placed`` and ``delete_placed`` refuse anything past or in progress, anything
someone has given guests, and anything without those properties.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from gremlin_town.sources.result import UnavailableReason

from .auth import GoogleAuthError, GoogleTokenProvider
from .settings import GoogleSettings

API_BASE = "https://www.googleapis.com/calendar/v3"
WRITE_CALENDAR = "primary"
MARKER_PROPERTY = "exosuit"
MARKER_PREFIX = "exosuit:"
TIME_ZONE = "Asia/Singapore"
# Shorter than the readers' 20 s: these calls run inside a save's own reply, after the Brain
# write has landed, so a slow Google delays what Nic hears, never what was saved.
REQUEST_TIMEOUT = 10.0
# One entry per task is expected; the limit only bounds a read if duplicates ever appeared.
FIND_LIMIT = 10
# Step I §4: the placement engine's own properties, and a day's entries in one read.
PLACED_DAY_PROPERTY = "exosuit_day"
PLACED_ROLE_PROPERTIES = {"block": "exosuit_block", "slot": "exosuit_slot"}
PLACED_LIMIT = 250
BUSY_PROPERTY = "exosuit_busy"
# Singapore keeps +08:00 all year. A fixed offset, not Brain Core's, because this package must
# import without Brain Core (the release packaging tests run it alone).
SINGAPORE = timezone(timedelta(hours=8))


class CalendarWriteError(RuntimeError):
    """A calendar read or write failed; ``reason`` is a step D reason code (§3.4)."""

    def __init__(self, reason: UnavailableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def marker(record_id: str) -> str:
    return f"{MARKER_PREFIX}{record_id}"


@dataclass(frozen=True, slots=True)
class EventSlot:
    """When an entry sits: a timed start and end, or all-day dates with Google's exclusive end."""

    start: datetime | date
    end: datetime | date

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def google_times(self, *, clear_other_form: bool = False) -> dict[str, dict[str, Any]]:
        """Google's ``start``/``end``. ``clear_other_form`` nulls the other form's fields, which a
        PATCH needs to turn a timed entry into an all-day one (or back)."""

        def one(value: datetime | date) -> dict[str, Any]:
            if self.all_day:
                fields: dict[str, Any] = {"date": value.isoformat()}
                if clear_other_form:
                    fields.update(dateTime=None, timeZone=None)
            else:
                fields = {"dateTime": value.isoformat(), "timeZone": TIME_ZONE}
                if clear_other_form:
                    fields["date"] = None
            return fields

        return {"start": one(self.start), "end": one(self.end)}


@dataclass(frozen=True, slots=True)
class CalendarEntry:
    """One entry Exosuit added, as Google has it now. ``slot`` is ``None`` if unreadable."""

    event_id: str
    marker_id: str
    slot: EventSlot | None
    has_guests: bool


@dataclass(frozen=True, slots=True)
class BusyEntry:
    """Owned busy entry, distinct from records and placement. Its ID is derived from key."""

    key: str
    slot: EventSlot
    has_guests: bool
    etag: str | None


def _classify_status(status: int) -> UnavailableReason:
    if status in (401, 403):
        return UnavailableReason.PERMISSION_DENIED
    if status == 429:
        return UnavailableReason.RATE_LIMITED
    return UnavailableReason.PROVIDER_ERROR


def _slot(event: dict[str, Any]) -> EventSlot | None:
    start, end = event.get("start"), event.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    try:
        if isinstance(start.get("dateTime"), str) and isinstance(end.get("dateTime"), str):
            return EventSlot(datetime.fromisoformat(start["dateTime"]), datetime.fromisoformat(end["dateTime"]))
        if isinstance(start.get("date"), str) and isinstance(end.get("date"), str):
            return EventSlot(date.fromisoformat(start["date"]), date.fromisoformat(end["date"]))
    except ValueError:
        return None
    return None


def _has_guests(event: dict[str, Any]) -> bool:
    attendees = event.get("attendees")
    if not isinstance(attendees, list):
        return False
    return any(isinstance(attendee, dict) and not attendee.get("self") for attendee in attendees)


def _carries_marker(event: dict[str, Any], marker_id: str) -> bool:
    """Both parts of the marker: the private property and the description's own marker."""

    extended = event.get("extendedProperties")
    private = extended.get("private") if isinstance(extended, dict) else None
    if not isinstance(private, dict) or private.get(MARKER_PROPERTY) != marker_id:
        return False
    description = event.get("description")
    if not isinstance(description, str):
        return False
    # The whole marker, not a longer record ID that merely starts the same way.
    return re.search(rf"(?<![\w:]){re.escape(marker(marker_id))}(?!\w)", description) is not None


def _entry(event: dict[str, Any], marker_id: str) -> CalendarEntry:
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar returned an event with no id")
    return CalendarEntry(event_id=event_id, marker_id=marker_id, slot=_slot(event), has_guests=_has_guests(event))


@dataclass(frozen=True, slots=True)
class PlacedEntry:
    """One block or task slot the placement engine wrote, as Google has it now. ``key`` is the
    block's or slot's own key (a slot's is ``<first record ID>:<part>``)."""

    event_id: str
    day: date
    role: str
    key: str
    title: str
    slot: EventSlot | None
    has_guests: bool
    busy: bool


def _placed(event: dict[str, Any], day: date) -> PlacedEntry | None:
    """The entry, if it carries the engine's properties for ``day``; anything else is None."""

    extended = event.get("extendedProperties")
    private = extended.get("private") if isinstance(extended, dict) else None
    if not isinstance(private, dict) or private.get(PLACED_DAY_PROPERTY) != day.isoformat():
        return None
    if private.get(MARKER_PROPERTY) is not None:
        return None
    found = [(role, private.get(name)) for role, name in PLACED_ROLE_PROPERTIES.items() if isinstance(private.get(name), str)]
    if len(found) != 1 or not found[0][1]:
        return None
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar returned an event with no id")
    role, key = found[0]
    return PlacedEntry(
        event_id=event_id, day=day, role=role, key=key, title=str(event.get("summary") or ""), slot=_slot(event),
        has_guests=_has_guests(event), busy=event.get("transparency") != "transparent",
    )


def _check_changeable(entry: PlacedEntry, now: datetime, new_slot: EventSlot | None = None) -> None:
    """Step I §4: only the engine's own entries, only in the future, never one with guests. The one
    exception is ``new_slot`` for an entry under way that keeps its start and ends later than now:
    a block cut short ("I'm done at 4") never touches time already past."""

    if entry.role not in PLACED_ROLE_PROPERTIES or not entry.key:
        raise ValueError("only the placement engine's own entries are changed")
    if entry.has_guests:
        raise ValueError("an entry with guests is never moved or removed")
    if entry.slot is None or entry.slot.all_day:
        raise ValueError("only an entry that starts in the future is moved or removed")
    if entry.slot.start.astimezone(SINGAPORE).date() != entry.day:
        raise ValueError("an entry Nic moved to another day is his now")
    if entry.slot.start > now:
        if new_slot is not None and new_slot.start <= now:
            raise ValueError("an entry is only moved to a timed slot in the future")
        return
    under_way = entry.slot.end > now
    if new_slot is not None and under_way and new_slot.start == entry.slot.start and new_slot.end > now:
        return
    raise ValueError("only an entry that starts in the future is moved or removed")


class CalendarWriter:
    def __init__(
        self,
        settings: GoogleSettings,
        token_provider: GoogleTokenProvider,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._token_provider = token_provider
        self._client = httpx.Client(transport=transport) if transport is not None else httpx.Client()
        self._events_url = f"{API_BASE}/calendars/{quote(WRITE_CALENDAR, safe='')}/events"

    def find_busy(self, key: str) -> BusyEntry | None:
        """Look up the exact deterministic ID; never search titles or mistake another entry for ours."""
        event_id = self._busy_id(key)
        event = self._send("GET", self._event_url(event_id), params={}, not_found_is_none=True)
        if event is None:
            return None
        return self._busy_entry(event, key)

    @staticmethod
    def _busy_id(key: str) -> str:
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid busy entry key")
        return "b" + key  # Google's allowed base32hex alphabet; stable across retries.

    def _busy_entry(self, event: dict[str, Any], key: str) -> BusyEntry:
        extended = event.get("extendedProperties")
        private = extended.get("private") if isinstance(extended, dict) else None
        private = private if isinstance(private, dict) else {}
        slot = _slot(event)
        if (event.get("id") != self._busy_id(key) or private.get(BUSY_PROPERTY) != key
                or any(name in private for name in (MARKER_PROPERTY, PLACED_DAY_PROPERTY))
                or event.get("status") == "cancelled" or slot is None or slot.all_day
                or slot.start.utcoffset() is None or slot.end.utcoffset() is None
                or slot.end <= slot.start or event.get("transparency", "opaque") != "opaque"):
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "The owned busy entry needs checking")
        etag = event.get("etag")
        return BusyEntry(key, slot, bool(event.get("attendees")), etag if isinstance(etag, str) else None)

    def write_busy(self, *, key: str, slot: EventSlot, current: BusyEntry | None, now: datetime) -> BusyEntry:
        """Insert or move one owned busy entry. Matching ETag protects edits made since the read."""
        event_id = self._busy_id(key)
        if (slot.all_day or not isinstance(slot.end, datetime) or slot.start.utcoffset() is None
                or slot.end.utcoffset() is None or slot.end <= slot.start or slot.end <= now):
            raise ValueError("a busy entry needs an ordered timed range ending in the future")
        if current is not None:
            if current.key != key or current.has_guests or not current.etag:
                raise ValueError("only an owned busy entry without guests can move")
            body = self._send("PATCH", self._event_url(event_id), params={"sendUpdates": "none"},
                              json=slot.google_times(), extra_headers={"If-Match": current.etag})
        else:
            event = {"id": event_id, "summary": "Busy", "transparency": "opaque", "reminders": {"useDefault": False},
                     "extendedProperties": {"private": {BUSY_PROPERTY: key}}, **slot.google_times()}
            body = self._send("POST", self._events_url, params={"sendUpdates": "none"}, json=event)
        written = self._busy_entry(body, key)
        if written.slot != slot or written.has_guests:
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "The busy entry did not match the requested time")
        return written

    def find(self, marker_id: str) -> CalendarEntry | None:
        """The entry Exosuit added for this marker, or ``None``. Cancelled entries never count."""

        body = self._send("GET", self._events_url, params={
            "privateExtendedProperty": f"{MARKER_PROPERTY}={marker_id}",
            "maxResults": str(FIND_LIMIT),
        })
        items = body.get("items", [])
        if not isinstance(items, list):
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar returned an invalid event list")
        for event in items:
            if isinstance(event, dict) and event.get("status") != "cancelled" and _carries_marker(event, marker_id):
                return _entry(event, marker_id)
        return None

    def insert(self, *, title: str, slot: EventSlot, marker_id: str) -> CalendarEntry:
        """Add one entry: the task's title, its slot, the marker, and no guests."""

        event = {
            "summary": title,
            "description": marker(marker_id),
            **slot.google_times(),
            "extendedProperties": {"private": {MARKER_PROPERTY: marker_id}},
        }
        body = self._send("POST", self._events_url, params={"sendUpdates": "none"}, json=event)
        return _entry(body, marker_id)

    def move(self, entry: CalendarEntry, slot: EventSlot) -> CalendarEntry:
        """Move an entry to a new slot. Its title, description and anything Nic added stay."""

        if entry.has_guests:
            raise ValueError("an entry with guests is never moved")
        switching = entry.slot is None or entry.slot.all_day != slot.all_day
        body = self._send(
            "PATCH", self._event_url(entry.event_id), params={"sendUpdates": "none"},
            json=slot.google_times(clear_other_form=switching),
        )
        return _entry(body, entry.marker_id)

    def delete(self, entry: CalendarEntry) -> None:
        """Remove one entry found by its marker. One already gone counts as removed."""

        if entry.has_guests:
            raise ValueError("an entry with guests is never removed")
        self._send("DELETE", self._event_url(entry.event_id), params={"sendUpdates": "none"}, expect_body=False)

    # --- the placement engine's entries (step I §4) -----------------------------------------------

    def list_placed(self, day: date) -> tuple[PlacedEntry, ...]:
        """Every live block and task slot the engine wrote for ``day``, by one exact query."""

        body = self._send("GET", self._events_url, params={
            "privateExtendedProperty": f"{PLACED_DAY_PROPERTY}={day.isoformat()}",
            "maxResults": str(PLACED_LIMIT),
            "singleEvents": "true",
        })
        items = body.get("items", [])
        if not isinstance(items, list):
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar returned an invalid event list")
        placed = []
        for event in items:
            if isinstance(event, dict) and event.get("status") != "cancelled":
                entry = _placed(event, day)
                if entry is not None:
                    placed.append(entry)
        return tuple(sorted(placed, key=lambda entry: (entry.role, entry.key, entry.event_id)))

    def insert_placed(
        self, *, day: date, role: str, key: str, title: str, description: str, slot: EventSlot, busy: bool, now: datetime,
    ) -> PlacedEntry:
        """Add one block or slot: timed, in the future, with the engine's properties, no guests."""

        if role not in PLACED_ROLE_PROPERTIES or not key:
            raise ValueError("role must be block or slot, with a key")
        if slot.all_day or slot.start <= now:
            raise ValueError("the engine only adds timed entries in the future")
        event = {
            "summary": title,
            "description": description,
            **slot.google_times(),
            "transparency": "opaque" if busy else "transparent",
            "extendedProperties": {"private": {PLACED_DAY_PROPERTY: day.isoformat(), PLACED_ROLE_PROPERTIES[role]: key}},
        }
        body = self._send("POST", self._events_url, params={"sendUpdates": "none"}, json=event)
        entry = _placed(body, day)
        if entry is None:
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar dropped the entry's properties")
        return entry

    def move_placed(self, entry: PlacedEntry, slot: EventSlot, *, now: datetime, title: str | None = None) -> PlacedEntry:
        """Move a future block or slot to a new future time on the same day, and retitle it when
        asked. The guards run on the caller's copy, then again on Google's current one."""

        _check_changeable(entry, now, slot)
        if slot.all_day or (slot.start <= now and not (entry.slot is not None and slot.start == entry.slot.start)):
            raise ValueError("an entry is only moved to a timed slot in the future")
        if slot.start.astimezone(SINGAPORE).date() != entry.day or slot.end.astimezone(SINGAPORE).date() != entry.day:
            raise ValueError("an entry is only moved within its own day")
        if self._current(entry, now, slot) is None:
            raise ValueError("the entry is gone, so there is nothing to move")
        change: dict[str, Any] = dict(slot.google_times())
        if title is not None:
            change["summary"] = title
        body = self._send("PATCH", self._event_url(entry.event_id), params={"sendUpdates": "none"}, json=change)
        moved = _placed(body, entry.day)
        if moved is None:
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar dropped the entry's properties")
        return moved

    def delete_placed(self, entry: PlacedEntry, *, now: datetime) -> None:
        """Remove a future block or slot. One already gone counts as removed. The guards run on the
        caller's copy, then again on Google's current one."""

        _check_changeable(entry, now)
        if self._current(entry, now) is None:
            return
        self._send("DELETE", self._event_url(entry.event_id), params={"sendUpdates": "none"}, expect_body=False)

    def _current(self, entry: PlacedEntry, now: datetime, new_slot: EventSlot | None = None) -> PlacedEntry | None:
        """Google's current copy of the entry, checked again: still the engine's for that day,
        still in the future, still without guests. None when it is gone."""

        event = self._send("GET", self._event_url(entry.event_id), params={}, allow_missing=True)
        if not event or event.get("status") == "cancelled":
            return None
        current = _placed(event, entry.day)
        if current is None or current.event_id != entry.event_id:
            raise ValueError("only the placement engine's own entries are changed")
        _check_changeable(current, now, new_slot)
        return current

    def _event_url(self, event_id: str) -> str:
        return f"{self._events_url}/{quote(event_id, safe='')}"

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str],
        json: dict[str, Any] | None = None,
        expect_body: bool = True,
        allow_missing: bool = False,
        not_found_is_none: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if not self._settings.enabled:
            raise CalendarWriteError(UnavailableReason.DISABLED, "Google is not enabled")
        try:
            access_token = self._token_provider.token()
        except GoogleAuthError as exc:
            raise CalendarWriteError(exc.reason, "Google access token unavailable") from exc
        try:
            response = self._client.request(
                method, url, params=params, json=json,
                headers={"Authorization": f"Bearer {access_token}", **(extra_headers or {})}, timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise CalendarWriteError(UnavailableReason.NETWORK_ERROR, "Google Calendar had no usable response") from exc
        if not expect_body:
            if response.status_code in (200, 204, 404, 410):
                return {}
            raise CalendarWriteError(_classify_status(response.status_code), f"Google Calendar refused the {method}")
        if not_found_is_none and response.status_code == 404:
            return None
        if allow_missing and response.status_code in (404, 410):
            return {}
        if response.status_code != 200:
            raise CalendarWriteError(_classify_status(response.status_code), f"Google Calendar refused the {method}")
        try:
            body = response.json()
        except ValueError as exc:
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar reply was not JSON") from exc
        if not isinstance(body, dict):
            raise CalendarWriteError(UnavailableReason.PROVIDER_ERROR, "Google Calendar reply was not an object")
        return body
