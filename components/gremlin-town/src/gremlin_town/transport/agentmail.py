"""Permanent AgentMail HTTP transport.

This module performs provider I/O and mapping only. It never creates or
changes Gremlin Town lifecycle state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, urlencode, urljoin

import httpx

from gremlin_town.contracts import ContentSource, OutboundEnvelope, Page, ProviderMessage, ProviderReference

from .errors import FramingError, ProviderTransportError, ReconciliationRequiredError, ThreadScopeConflictError
from .framing import canonical_wire_bytes, parse_wire_message


class HTTPRequester(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class AgentMailConfig:
    inbox_id: str
    api_key: str
    base_url: str = "https://api.agentmail.to/v0/"
    retry_window: timedelta = timedelta(hours=24)
    timeout: float = 20.0
    page_limit: int = 100


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _retry_after(value: str | None, *, now: Callable[[], datetime]) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0.0, (date - now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class AgentMailTransport:
    def __init__(
        self,
        config: AgentMailConfig,
        *,
        client: HTTPRequester | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if config.page_limit < 1 or config.page_limit > 100:
            raise ValueError("AgentMail page_limit must be between 1 and 100")
        self.config = config
        self._client = client or httpx.Client()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _url(self, path: str, **query: str | int | None) -> str:
        base = self.config.base_url.rstrip("/") + "/"
        url = urljoin(base, path.lstrip("/"))
        values = [(key, str(value)) for key, value in query.items() if value is not None]
        return f"{url}?{urlencode(values)}" if values else url

    def _headers(self, *, retry_key: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.config.api_key}"}
        if retry_key is not None:
            headers["Idempotency-Key"] = retry_key
        return headers

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.config.timeout)
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderTransportError(
                "TIMEOUT",
                "provider request timed out",
                retryable=True,
                accepted=method.upper() == "POST",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderTransportError(
                "PROVIDER_UNAVAILABLE",
                "provider connection failed",
                retryable=True,
                accepted=method.upper() == "POST",
            ) from exc
        status = int(response.status_code)
        if 200 <= status < 300:
            return response
        provider_code: str | None = None
        try:
            error_body = response.json()
            if isinstance(error_body, dict):
                provider_code = error_body.get("code", error_body.get("error_code"))
        except (ValueError, json.JSONDecodeError):
            pass
        mapping = {
            400: ("INVALID_REQUEST", False),
            401: ("AUTHENTICATION_FAILED", False),
            403: ("PROVIDER_FORBIDDEN", False),
            404: ("SCOPE_OR_NOT_FOUND", False),
            409: ("CONFLICT", False),
            422: ("INVALID_REQUEST", False),
            429: ("RATE_LIMITED", True),
        }
        code, retryable = mapping.get(
            status,
            ("PROVIDER_UNAVAILABLE" if status >= 500 else "PROVIDER_ERROR", status >= 500),
        )
        raise ProviderTransportError(
            code,
            f"provider returned HTTP {status}",
            retryable=retryable,
            retry_after=_retry_after(response.headers.get("Retry-After"), now=self._clock),
            status=status,
            provider_code=provider_code,
        )

    @staticmethod
    def _text(envelope: OutboundEnvelope) -> str:
        try:
            return envelope.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderTransportError("INVALID_OUTBOX", "staged WireMessage is not UTF-8") from exc

    def send(self, envelope: OutboundEnvelope) -> ProviderReference:
        text = self._text(envelope)
        if envelope.parent_message_id is None:
            path = f"/inboxes/{quote(self.config.inbox_id, safe='')}/messages/send"
            payload = {
                "to": envelope.recipient_address,
                "subject": f"Gremlin Town {envelope.kind.value} {envelope.request_id} {envelope.operation_id}",
                "text": text,
            }
        else:
            path = (
                f"/inboxes/{quote(self.config.inbox_id, safe='')}/messages/"
                f"{quote(envelope.parent_message_id, safe='')}/reply"
            )
            payload = {"to": envelope.recipient_address, "text": text, "reply_all": False}
        response = self._request(
            "POST",
            self._url(path),
            headers={**self._headers(retry_key=envelope.provider_retry_key), "Content-Type": "application/json"},
            json=payload,
        )
        return self._reference(self._json(response), envelope.parent_message_id)

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderTransportError("MALFORMED_RESPONSE", "provider response was not valid JSON") from exc

    def fetch_message(self, message_id: str) -> ProviderMessage:
        response = self._request(
            "GET",
            self._url(
                f"/inboxes/{quote(self.config.inbox_id, safe='')}/messages/{quote(message_id, safe='')}"
            ),
            headers=self._headers(),
        )
        return self._message(self._json(response), expected_thread=None)

    def fetch_thread(self, thread_id: str, cursor: str | None = None) -> Page:
        response = self._request(
            "GET",
            self._url(
                f"/inboxes/{quote(self.config.inbox_id, safe='')}/threads/{quote(thread_id, safe='')}",
                limit=self.config.page_limit,
                page_token=cursor,
            ),
            headers=self._headers(),
        )
        data = self._json(response)
        raw_items = data.get("messages", []) if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            raise ProviderTransportError("MALFORMED_RESPONSE", "provider thread messages are not a list")
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            message = self._message(raw, expected_thread=thread_id)
            if message.message_id in seen:
                continue
            seen.add(message.message_id)
            items.append({"message": message})
        next_cursor = data.get("next_page_token") if isinstance(data, dict) else None
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ProviderTransportError("MALFORMED_RESPONSE", "provider page token is not a string")
        return Page(items=items, next_cursor=next_cursor, as_of=self._clock())

    def fetch_thread_complete(self, thread_id: str) -> tuple[ProviderMessage, ...]:
        pages: list[tuple[ProviderMessage, ...]] = []
        cursor: str | None = None
        seen_cursors: set[str | None] = set()
        while True:
            if cursor in seen_cursors:
                raise ProviderTransportError("MALFORMED_RESPONSE", "provider page token did not advance")
            seen_cursors.add(cursor)
            page = self.fetch_thread(thread_id, cursor)
            pages.append(tuple(item["message"] for item in page.items))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        # AgentMail returns pages newest-segment first but messages ascending in
        # each segment; reverse the page segments, then deduplicate overlap.
        result: dict[str, ProviderMessage] = {}
        for page in reversed(pages):
            for message in page:
                result[message.message_id] = message
        return tuple(result.values())

    def reconcile(self, envelope: OutboundEnvelope, *, created_at: datetime | None = None) -> ProviderReference | None:
        """Discover an accepted message without resending or using fake filters."""

        after: str | None = None
        seen_after: set[str | None] = set()
        candidates: dict[str, ProviderReference] = {}
        while True:
            if after in seen_after:
                raise ProviderTransportError("MALFORMED_RESPONSE", "provider after boundary did not advance")
            seen_after.add(after)
            response = self._request(
                "GET",
                self._url(
                    f"/inboxes/{quote(self.config.inbox_id, safe='')}/messages",
                    limit=self.config.page_limit,
                    after=after,
                ),
                headers=self._headers(),
            )
            data = self._json(response)
            raw_items = data.get("messages", []) if isinstance(data, dict) else []
            if not isinstance(raw_items, list):
                raise ProviderTransportError("MALFORMED_RESPONSE", "provider discovery messages are not a list")
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                reference = self._matching_reference(raw, envelope)
                if reference is not None:
                    candidates[reference.message_id] = reference
            if not raw_items:
                break
            # The last item is the overlap boundary. The next request repeats
            # that boundary, and candidate IDs are deduplicated above.
            last = raw_items[-1]
            next_after = last.get("timestamp", last.get("created_at", last.get("message_id")))
            if not isinstance(next_after, str):
                break
            after = next_after
            if len(raw_items) < self.config.page_limit and not data.get("next_page_token"):
                break
        if len(candidates) > 1:
            raise ReconciliationRequiredError("multiple provider messages match one saved WireMessage")
        if candidates:
            return next(iter(candidates.values()))
        if created_at is not None:
            created = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
            if self._clock() - created > self.config.retry_window:
                raise ReconciliationRequiredError()
        return None

    @classmethod
    def _matching_reference(cls, raw: Mapping[str, Any], envelope: OutboundEnvelope) -> ProviderReference | None:
        message_id = raw.get("message_id", raw.get("id"))
        if not isinstance(message_id, str):
            return None
        body = raw.get("text", raw.get("extracted_text"))
        if isinstance(body, str):
            try:
                wire = parse_wire_message(body)
            except FramingError:
                wire = None
            if wire is not None:
                if wire.operation_id != envelope.operation_id or wire.request_id != envelope.request_id:
                    return None
                if canonical_wire_bytes(wire) != envelope.payload:
                    return None
            elif body.encode("utf-8") != envelope.payload:
                return None
        elif raw.get("operation_id") != envelope.operation_id:
            return None
        return cls._reference(raw, envelope.parent_message_id)

    @staticmethod
    def _reference(value: Any, parent_message_id: str | None) -> ProviderReference:
        if not isinstance(value, dict):
            raise ProviderTransportError("MALFORMED_RESPONSE", "provider acceptance is not an object")
        message_id = value.get("message_id", value.get("id"))
        thread_id = value.get("thread_id", value.get("conversation_id"))
        if not isinstance(message_id, str) or not isinstance(thread_id, str):
            raise ProviderTransportError("MALFORMED_RESPONSE", "provider acceptance lacks message/thread identity")
        return ProviderReference(
            message_id=message_id,
            thread_id=thread_id,
            accepted_at=_parse_time(value.get("timestamp", value.get("accepted_at", value.get("created_at")))),
            parent_message_id=value.get("in_reply_to", value.get("parent_message_id", parent_message_id)),
        )

    @staticmethod
    def _body(value: Mapping[str, Any]) -> tuple[bytes | None, bool]:
        raw = value.get("text", value.get("extracted_text"))
        if raw is None or value.get("body_available") is False:
            return None, False
        if isinstance(raw, str):
            return raw.encode("utf-8"), True
        if isinstance(raw, bytes):
            return raw, True
        raise ProviderTransportError("MALFORMED_RESPONSE", "provider message body has an invalid type")

    @classmethod
    def _message(cls, value: Any, expected_thread: str | None) -> ProviderMessage:
        if not isinstance(value, dict):
            raise ProviderTransportError("MALFORMED_RESPONSE", "provider message is not an object")
        message_id = value.get("message_id", value.get("id"))
        thread_id = value.get("thread_id", value.get("conversation_id"))
        if not isinstance(message_id, str) or not isinstance(thread_id, str):
            raise ProviderTransportError("MALFORMED_RESPONSE", "provider message lacks identity")
        if expected_thread is not None and thread_id != expected_thread:
            raise ThreadScopeConflictError()
        body, available = cls._body(value)
        return ProviderMessage(
            message_id=message_id,
            thread_id=thread_id,
            parent_message_id=value.get("in_reply_to", value.get("parent_message_id")),
            received_at=_parse_time(value.get("timestamp", value.get("received_at", value.get("created_at")))),
            body=body,
            body_available=available,
            content_source=ContentSource.PROVIDER,
            trust_label="provider_unverified",
            exact_link_verified=False,
        )
