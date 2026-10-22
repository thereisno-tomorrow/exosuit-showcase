"""The few Notion API calls the view needs (docs/product/steps/h-notion-view.md §4).

Plain HTTPS through Brain Core's injectable `HTTPTransport`, so tests pass a fake instead of a network. Paced to
about three requests a second; a 429 or 5xx is retried a bounded number of times, then raised.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from brain_core.github_storage import GitHubTransportError, HTTPTransport, UrllibHTTPTransport

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"  # still addresses a one-data-source database by its ID
MIN_INTERVAL_SECONDS = 0.34
ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 30.0


class NotionError(RuntimeError):
    """Notion refused a call, or never answered."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        transport: HTTPTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(token, str) or not token or token != token.strip():
            raise ValueError("the Notion key must be a non-empty string with no surrounding whitespace")
        self._token = token
        self._transport = transport or UrllibHTTPTransport()
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = MIN_INTERVAL_SECONDS - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _call(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None, *, retry_unanswered: bool = True,
    ) -> dict[str, Any]:
        """``retry_unanswered=False`` retries only a 429, which Notion never acted on: a create that timed out or
        failed on Notion's side may still have made the page, and the repair's sweep archives any it did."""
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
            "User-Agent": "gremlin-town-notion-view",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(1, ATTEMPTS + 1):
            self._pace()
            try:
                response = self._transport.request(method, API_BASE + path, headers=headers, body=body)
            except GitHubTransportError as exc:  # the shared transport's "no response" error
                if attempt == ATTEMPTS or not retry_unanswered:
                    raise NotionError(f"Notion did not answer {method} {path}") from exc
                self._sleep(float(attempt))
                continue
            if response.status == 429 or (response.status >= 500 and retry_unanswered):
                if attempt == ATTEMPTS:
                    raise NotionError(f"Notion kept refusing {method} {path}", status=response.status)
                self._sleep(_retry_after(response.headers, attempt))
                continue
            try:
                value = json.loads(response.body) if response.body else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NotionError(f"Notion returned invalid JSON for {method} {path}", status=response.status) from exc
            if not 200 <= response.status < 300:
                code = value.get("code") if isinstance(value, dict) else None
                message = value.get("message") if isinstance(value, dict) else None
                raise NotionError(
                    f"Notion refused {method} {path}: {response.status} {code or ''} {message or ''}".strip(),
                    status=response.status, code=code,
                )
            if not isinstance(value, dict):
                raise NotionError(f"Notion returned a non-object for {method} {path}", status=response.status)
            return value
        raise AssertionError("unreachable")

    def database(self, database_id: str) -> dict[str, Any]:
        return self._call("GET", f"/databases/{database_id}")

    def create_page(self, database_id: str, properties: Mapping[str, Any]) -> str:
        page = self._call(
            "POST", "/pages", {"parent": {"database_id": database_id}, "properties": dict(properties)},
            retry_unanswered=False,
        )
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id:
            raise NotionError("Notion created a page without an ID")
        return page_id

    def update_page(self, page_id: str, properties: Mapping[str, Any]) -> None:
        self._call("PATCH", f"/pages/{page_id}", {"properties": dict(properties)})

    def pages(self, database_id: str) -> list[dict[str, Any]]:
        """Every row in the database, following Notion's pages of 100."""

        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            value = self._call("POST", f"/databases/{database_id}/query", payload)
            results = value.get("results")
            if not isinstance(results, list):
                raise NotionError("Notion returned a query without results")
            found.extend(page for page in results if isinstance(page, dict))
            if not value.get("has_more"):
                return found
            cursor = value.get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                raise NotionError("Notion said there were more rows but gave no cursor")

    def archive_page(self, page_id: str) -> None:
        """Moves the row to Notion's trash, where Nic can restore it."""

        try:
            self._call("PATCH", f"/pages/{page_id}", {"archived": True})
        except NotionError as exc:
            if exc.status == 404:  # already gone from Notion: nothing left to archive
                return
            raise


def _retry_after(headers: Mapping[str, str], attempt: int) -> float:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return min(max(float(value), 0.0), MAX_RETRY_WAIT_SECONDS)
            except ValueError:
                break
    return float(attempt)
