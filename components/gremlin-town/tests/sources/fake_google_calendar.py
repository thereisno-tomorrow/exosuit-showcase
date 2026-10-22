"""A tiny in-memory Google Calendar for tests: just enough of the events API for Exosuit's own
calendar entries (N8) - list by private extended property, insert, patch, delete - behind
`httpx.MockTransport`, recording every request. Nothing here reaches Google."""

from __future__ import annotations

import json

import httpx

EVENTS_PATH = "/calendar/v3/calendars/primary/events"


class FakeGoogleCalendar:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.calls: list[httpx.Request] = []
        self.fail_next: dict[str, int] = {}
        self._next_id = 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "www.googleapis.com", request.url
        if request.method in self.fail_next:
            return httpx.Response(self.fail_next.pop(request.method), json={"error": "fixture failure"})
        path = request.url.path
        if path == EVENTS_PATH and request.method == "GET":
            name, value = dict(httpx.QueryParams(request.url.query))["privateExtendedProperty"].split("=", 1)
            items = [
                event for event in self.events.values()
                if event["status"] != "cancelled"
                and event.get("extendedProperties", {}).get("private", {}).get(name) == value
            ]
            return httpx.Response(200, json={"kind": "calendar#events", "items": items})
        if path == EVENTS_PATH and request.method == "POST":
            event_id = f"evt{self._next_id}"
            self._next_id += 1
            self.events[event_id] = {"id": event_id, "status": "confirmed", **json.loads(request.content)}
            return httpx.Response(200, json=self.events[event_id])
        event_id = path.removeprefix(EVENTS_PATH + "/")
        event = self.events.get(event_id)
        if request.method == "PATCH" and event is not None:
            for key, value in json.loads(request.content).items():
                merged = {**event.get(key, {}), **value}
                event[key] = {name: item for name, item in merged.items() if item is not None}
            return httpx.Response(200, json=event)
        if request.method == "DELETE":
            if event is None or event["status"] == "cancelled":
                return httpx.Response(410)
            event["status"] = "cancelled"
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def requests(self) -> list[tuple[str, str]]:
        """``(method, path after the events collection)``: ``"/"`` for the collection itself."""

        return [(call.method, call.url.path.removeprefix(EVENTS_PATH) or "/") for call in self.calls]

    def live(self) -> list[dict]:
        return [event for event in self.events.values() if event["status"] != "cancelled"]


class FixedToken:
    """A token provider double: a fixed access token, never a token request."""

    def token(self) -> str:
        return "test-access-token"
