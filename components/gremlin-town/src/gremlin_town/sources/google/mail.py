"""Read-only Gmail reader.

Returns messages as ``SourceItem``s tagged ``google.mail``, carrying headers
and Gmail's snippet only — no body, no attachments (spec §3, Proposed). An
email is someone else's words (spec §2): ``said_by`` names the sender, and
nothing here labels a message a task or a promise. A look is not a record:
nothing here saves anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from gremlin_town.sources.result import SourceItem, SourceRead, UnavailableReason

from .auth import GoogleAuthError, GoogleTokenProvider
from .settings import GoogleSettings

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
METADATA_HEADERS = ("From", "To", "Subject", "Date")
REQUEST_TIMEOUT = 20.0
GMAIL_LINK = "https://mail.google.com/mail/u/0/#all/{message_id}"


class _MailFailure(Exception):
    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(reason)
        self.reason = reason


def _classify_status(status: int) -> UnavailableReason:
    if status in (401, 403):
        return UnavailableReason.PERMISSION_DENIED
    if status == 429:
        return UnavailableReason.RATE_LIMITED
    return UnavailableReason.PROVIDER_ERROR


class MailReader:
    def __init__(
        self,
        settings: GoogleSettings,
        token_provider: GoogleTokenProvider,
        transport: httpx.BaseTransport | None = None,
        *, request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self._settings = settings
        self._token_provider = token_provider
        self._request_timeout = request_timeout
        self._client = httpx.Client(transport=transport) if transport is not None else httpx.Client()

    def is_unread(self, message_id: str) -> bool:
        """Read only this message's labels for the morning review; no body or mail mutation."""
        if not self._settings.enabled:
            raise GoogleAuthError(UnavailableReason.DISABLED, "Google mail is disabled")
        access_token = self._token_provider.token()
        try:
            body = self._get(f"{API_BASE}/messages/{quote(message_id, safe='')}", access_token,
                             {"format": "minimal", "fields": "id,labelIds"})
            labels = body.get("labelIds")
            if body.get("id") != message_id or not isinstance(labels, list) or not all(isinstance(s, str) for s in labels):
                raise _MailFailure(UnavailableReason.PROVIDER_ERROR)
        except _MailFailure as exc:
            raise GoogleAuthError(exc.reason, "Google mail labels could not be read") from exc
        return "UNREAD" in labels

    def messages(self, query: str, max_results: int) -> SourceRead:
        if not self._settings.enabled:
            return SourceRead.unavailable_because(UnavailableReason.DISABLED)
        if max_results < 1:
            raise ValueError("max_results must be positive")
        try:
            access_token = self._token_provider.token()
        except GoogleAuthError as exc:
            return SourceRead.unavailable_because(exc.reason)

        try:
            message_ids = self._list_ids(query, max_results, access_token)
        except _MailFailure as exc:
            return SourceRead.unavailable_because(exc.reason)

        items: list[SourceItem] = []
        failed: list[str] = []
        for message_id in message_ids:
            try:
                items.append(self._fetch(message_id, access_token))
            except _MailFailure:
                failed.append(message_id)
        if failed:
            return SourceRead.partial(items=tuple(items), unavailable=tuple(failed))
        return SourceRead.ok(items=tuple(items))

    def _list_ids(self, query: str, max_results: int, access_token: str) -> list[str]:
        body = self._get(f"{API_BASE}/messages", access_token, {"q": query, "maxResults": max_results})
        ids: list[str] = []
        for entry in body.get("messages", []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                ids.append(entry["id"])
        return ids

    def _fetch(self, message_id: str, access_token: str) -> SourceItem:
        body = self._get(
            f"{API_BASE}/messages/{message_id}", access_token,
            {"format": "metadata", "metadataHeaders": list(METADATA_HEADERS)},
        )
        return self._item(body)

    def _get(self, url: str, access_token: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.request(
                "GET", url, params=params, headers={"Authorization": f"Bearer {access_token}"},
                timeout=self._request_timeout,
            )
        except httpx.HTTPError as exc:
            raise _MailFailure(UnavailableReason.NETWORK_ERROR) from exc
        if response.status_code != 200:
            raise _MailFailure(_classify_status(response.status_code))
        try:
            body = response.json()
        except ValueError as exc:
            raise _MailFailure(UnavailableReason.PROVIDER_ERROR) from exc
        if not isinstance(body, dict):
            raise _MailFailure(UnavailableReason.PROVIDER_ERROR)
        return body

    def _item(self, body: dict[str, Any]) -> SourceItem:
        message_id = body.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise _MailFailure(UnavailableReason.PROVIDER_ERROR)
        headers: dict[str, str] = {}
        payload = body.get("payload")
        if isinstance(payload, dict):
            for header in payload.get("headers", []) or []:
                if (isinstance(header, dict) and header.get("name") in METADATA_HEADERS
                        and isinstance(header.get("value"), str)):
                    headers[header["name"]] = header["value"]
        sender = headers.get("From")
        snippet = body.get("snippet")
        return SourceItem(
            source="google.mail",
            account=self._settings.account or "",
            provider_id=message_id,
            link=GMAIL_LINK.format(message_id=message_id),
            said_by=sender,
            read_at=datetime.now(UTC),
            fields={
                "thread_id": body.get("threadId"),
                "from": sender,
                "to": headers.get("To"),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
                "snippet": snippet if isinstance(snippet, str) else None,
            },
        )
