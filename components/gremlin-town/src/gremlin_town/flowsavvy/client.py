"""Small HTTP client for the official FlowSavvy API, retrieved 2 October 2026.

Never logs request URLs, bodies, IDs, credentials or remote error text. No retries;
the sync layer durably records creation intent before a non-idempotent POST.
"""
from typing import Any
from urllib.parse import quote

import httpx


class FlowSavvyError(RuntimeError):
    """A sanitized, operator-visible connector failure."""


class FlowSavvyClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None):
        if not token.strip():
            raise FlowSavvyError("FlowSavvy credential is empty")
        self.http = httpx.Client(base_url="https://my.flowsavvy.app", transport=transport,
                                 headers={"Authorization": f"Bearer {token}"}, timeout=20,
                                 follow_redirects=False)

    def close(self):
        self.http.close()

    def _request(self, method: str, path: str, *, allow_missing=False, **kwargs) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise FlowSavvyError("FlowSavvy request did not return a usable response") from None
        if allow_missing and response.status_code == 404:
            return {}
        if not 200 <= response.status_code < 300:
            raise FlowSavvyError(f"FlowSavvy request failed (HTTP {response.status_code})")
        if response.status_code == 204:
            return {}
        try:
            value = response.json()
        except ValueError:
            raise FlowSavvyError("FlowSavvy returned invalid JSON") from None
        if not isinstance(value, dict):
            raise FlowSavvyError("FlowSavvy returned an invalid object")
        return value

    def create(self, body: dict) -> str:
        value = self._request("POST", "/api/tasks", json=body)
        remote = value.get("id")
        if not isinstance(remote, str) or not remote:
            raise FlowSavvyError("FlowSavvy creation returned no task reference")
        return remote

    def get(self, remote: str) -> dict:
        return self._request("GET", "/api/items/" + quote(remote, safe=""))

    def update(self, remote: str, managed: dict) -> None:
        current = self.get(remote)
        if current.get("itemType") != "task" or current.get("recurrenceRule") or current.get("parentItemId"):
            raise FlowSavvyError("FlowSavvy managed task has an unexpected shape")
        # PUT replaces the entire object, not a patch. Preserve unmanaged writable fields.
        readonly = {"id", "dateCreated", "lastModified", "url", "completed", "parentItemId",
                    "occurrenceDate", "cancelledOccurrenceDates"}
        body = {key: value for key, value in current.items() if key not in readonly}
        body.update(managed)
        self._request("PUT", "/api/tasks/" + quote(remote, safe=""), json=body)

    def complete(self, remote: str) -> None:
        self._request("POST", "/api/tasks/" + quote(remote, safe="") + "/complete")

    def uncomplete(self, remote: str) -> None:
        self._request("POST", "/api/tasks/" + quote(remote, safe="") + "/uncomplete")

    def remove(self, remote: str) -> None:
        self._request("DELETE", "/api/items/" + quote(remote, safe=""), allow_missing=True)

    def recalculate(self) -> None:
        self._request("POST", "/api/recalculate", params={"reschedulePastTasks": "false"})

    def schedule(self, start, end) -> list[dict]:
        value = self._request("GET", "/api/schedule", params={"startDate": start.isoformat(), "endDate": end.isoformat()})
        rows = value.get("scheduleItems")
        if rows is None:
            return []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise FlowSavvyError("FlowSavvy returned an invalid schedule")
        return rows
