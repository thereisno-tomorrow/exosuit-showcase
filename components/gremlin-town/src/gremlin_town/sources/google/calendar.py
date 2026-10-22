"""Read-only Google Calendar reader.

Returns events as ``SourceItem``s tagged ``google.calendar``. A look is not a
record (spec §2): nothing here saves anything, and every item carries where
it came from. A failure on one calendar makes the whole read ``partial`` and
names that calendar; an error is never turned into an empty ``ok``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from gremlin_town.sources.result import SourceItem, SourceRead, UnavailableReason

from .auth import GoogleAuthError, GoogleTokenProvider
from .settings import GoogleSettings

API_BASE = "https://www.googleapis.com/calendar/v3"
REQUEST_TIMEOUT = 20.0


class _CalendarFailure(Exception):
    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(reason)
        self.reason = reason


def _classify_status(status: int) -> UnavailableReason:
    if status in (401, 403):
        return UnavailableReason.PERMISSION_DENIED
    if status == 429:
        return UnavailableReason.RATE_LIMITED
    return UnavailableReason.PROVIDER_ERROR


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _event_time(value: Any) -> str | None:
    """An event's start/end, kept exactly as Google sent it (spec §5)."""

    if not isinstance(value, dict):
        return None
    for key in ("dateTime", "date"):
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


# Singapore keeps +08:00 all year; a fixed offset, so this package imports without Brain Core.
_SINGAPORE = timezone(timedelta(hours=8))


def _placement_role(event: dict[str, Any]) -> str | None:
    """``"block"`` or ``"slot"`` for an entry the placement engine wrote (step I §4), else None."""

    extended = event.get("extendedProperties")
    private = extended.get("private") if isinstance(extended, dict) else None
    if not isinstance(private, dict) or not isinstance(private.get("exosuit_day"), str):
        return None
    if private.get("exosuit") is not None:
        return None
    # An engine entry Nic dragged to another day is his from then on (placement-02).
    start = (event.get("start") or {}).get("dateTime")
    if isinstance(start, str):
        try:
            if datetime.fromisoformat(start).astimezone(_SINGAPORE).date().isoformat() != private["exosuit_day"]:
                return None
        except ValueError:
            return None
    if isinstance(private.get("exosuit_slot"), str):
        return "slot"
    if isinstance(private.get("exosuit_block"), str):
        return "block"
    return None


class CalendarReader:
    def __init__(
        self,
        settings: GoogleSettings,
        token_provider: GoogleTokenProvider,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._token_provider = token_provider
        self._client = httpx.Client(transport=transport) if transport is not None else httpx.Client()

    def events(self, time_min: datetime, time_max: datetime) -> SourceRead:
        if not self._settings.enabled:
            return SourceRead.unavailable_because(UnavailableReason.DISABLED)
        try:
            access_token = self._token_provider.token()
        except GoogleAuthError as exc:
            return SourceRead.unavailable_because(exc.reason)

        items: list[SourceItem] = []
        failed: list[str] = []
        for calendar_id in self._settings.calendars:
            try:
                items.extend(self._events_for_calendar(calendar_id, access_token, time_min, time_max))
            except _CalendarFailure:
                failed.append(calendar_id)
        if failed:
            return SourceRead.partial(items=tuple(items), unavailable=tuple(failed))
        return SourceRead.ok(items=tuple(items))

    def _events_for_calendar(
        self, calendar_id: str, access_token: str, time_min: datetime, time_max: datetime,
    ) -> list[SourceItem]:
        items: list[SourceItem] = []
        page_token: str | None = None
        while True:
            params: dict[str, str] = {
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeMin": _rfc3339(time_min),
                "timeMax": _rfc3339(time_max),
            }
            if page_token is not None:
                params["pageToken"] = page_token
            body = self._get(f"{API_BASE}/calendars/{quote(calendar_id, safe='')}/events", access_token, params)
            for event in body.get("items", []) or []:
                if not isinstance(event, dict) or event.get("status") == "cancelled":
                    continue
                items.append(self._item(calendar_id, event))
            next_token = body.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token
        return items

    def _get(self, url: str, access_token: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._client.request(
                "GET", url, params=params, headers={"Authorization": f"Bearer {access_token}"},
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise _CalendarFailure(UnavailableReason.NETWORK_ERROR) from exc
        if response.status_code != 200:
            raise _CalendarFailure(_classify_status(response.status_code))
        try:
            body = response.json()
        except ValueError as exc:
            raise _CalendarFailure(UnavailableReason.PROVIDER_ERROR) from exc
        if not isinstance(body, dict):
            raise _CalendarFailure(UnavailableReason.PROVIDER_ERROR)
        return body

    def _item(self, calendar_id: str, event: dict[str, Any]) -> SourceItem:
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise _CalendarFailure(UnavailableReason.PROVIDER_ERROR)
        link = event.get("htmlLink")
        return SourceItem(
            source="google.calendar",
            account=self._settings.account or "",
            provider_id=event_id,
            link=link if isinstance(link, str) else "",
            said_by=None,
            read_at=datetime.now(UTC),
            fields={
                "calendar_id": calendar_id,
                "summary": event.get("summary"),
                "start": _event_time(event.get("start")),
                "end": _event_time(event.get("end")),
                "location": event.get("location"),
                "description": event.get("description"),
                # Step I: "transparent" means the event is marked free; and whether the placement
                # engine made it (a block or a task slot), from its own private property.
                "transparency": event.get("transparency"),
                "exosuit_role": _placement_role(event),
            },
        )
