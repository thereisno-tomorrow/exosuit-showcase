"""GitHub Issues conversation transport; lifecycle and authorization stay in GT.

Only a durably queued operation may call send. GitHub POSTs have no GT
idempotency guarantee: ambiguous sends must go through reconcile, which never
returns a safe-to-resend absence. Runtime selection is deliberately separate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from gremlin_town.contracts import ContentSource, OutboundEnvelope, Page, ProviderMessage, ProviderReference
from gremlin_town.github_app import GitHubAppError, GitHubAppTokenProvider
from .errors import FramingError, ProviderTransportError, ReconciliationRequiredError, ThreadScopeConflictError
from .framing import canonical_wire_bytes, parse_wire_message


class HTTPRequester(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    """Either a fixed ``token`` or a GitHub App ``token_provider``, never both.

    ``writer_user_id`` is the account the current credential posts as. With an
    App that is its bot account. ``legacy_writer_user_ids`` are earlier writers
    whose messages reconciliation may still need to recognise; sends never use them.
    """

    owner: str
    repository: str
    token: str = field(repr=False)
    writer_user_id: int
    timeout: float = 20.0
    page_limit: int = 100
    max_pages: int = 100
    max_body_bytes: int = 60_000
    token_provider: GitHubAppTokenProvider | None = field(default=None, repr=False, compare=False)
    legacy_writer_user_ids: tuple[int, ...] = ()


def _error(code: str, message: str) -> ProviderTransportError:
    return ProviderTransportError(code, message)


class GitHubTransport:
    def __init__(self, config: GitHubConfig, *, client: HTTPRequester | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        for value in (config.owner, config.repository):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
                raise ValueError("invalid GitHub repository binding")
        if type(config.writer_user_id) is not int or config.writer_user_id <= 0:
            raise ValueError("a verified GitHub writer user ID is required")
        if (not isinstance(config.legacy_writer_user_ids, tuple)
                or any(type(item) is not int or item <= 0 for item in config.legacy_writer_user_ids)
                or config.writer_user_id in config.legacy_writer_user_ids):
            raise ValueError("legacy writer IDs must be positive integers other than the current writer")
        if bool(config.token) == (config.token_provider is not None):
            raise ValueError("configure exactly one of a GitHub token or a GitHub App token provider")
        if (config.token_provider is not None and config.token_provider.repository.casefold()
                != f"{config.owner}/{config.repository}".casefold()):
            raise ValueError("GitHub App token provider is bound to another repository")
        if not 1 <= config.page_limit <= 100 or config.max_pages < 1:
            raise ValueError("invalid GitHub pagination limits")
        if config.timeout <= 0 or not 1 <= config.max_body_bytes <= 60_000:
            raise ValueError("invalid GitHub timeout or body limit")
        self.config = config
        self._client = client or httpx.Client()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._base = f"https://api.github.com/repos/{config.owner}/{config.repository}"
        self._prefix = f"github:{config.owner}/{config.repository}:issue:"

    def _ids(self, locator: str) -> tuple[int, int | None]:
        if not isinstance(locator, str) or not locator.startswith(self._prefix):
            raise ThreadScopeConflictError("GitHub reference is outside the configured repository")
        match = re.fullmatch(r"([1-9][0-9]*)(?::comment:([1-9][0-9]*))?", locator[len(self._prefix):])
        if match is None:
            raise ThreadScopeConflictError("invalid GitHub message reference")
        return int(match[1]), int(match[2]) if match[2] else None

    def _thread(self, number: int) -> str:
        return f"{self._prefix}{number}"

    def _retry_after(self, headers: Any) -> float | None:
        value = headers.get("Retry-After")
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    moment = parsedate_to_datetime(value)
                    return max(0.0, (moment - self._clock()).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        if headers.get("X-RateLimit-Remaining") == "0":
            try:
                return max(1.0, float(headers["X-RateLimit-Reset"]) - self._clock().timestamp())
            except (KeyError, ValueError, OverflowError):
                return 60.0
        return None

    def _token(self) -> str:
        provider = self.config.token_provider
        if provider is None:
            return self.config.token
        try:
            return provider.token()
        except GitHubAppError as exc:
            # Nothing was sent, so this is never an accepted (uncertain) write.
            raise ProviderTransportError(
                "PROVIDER_UNAVAILABLE" if exc.retryable else "AUTHENTICATION_FAILED",
                "GitHub App token is unavailable", status=exc.status, retryable=exc.retryable,
            ) from exc

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 payload: dict | None = None) -> Any:
        url = self._base + path
        if query:
            url += "?" + urlencode(query)
        for attempt in (1, 2):
            token = self._token()
            kwargs: dict[str, Any] = {
                "headers": {"Authorization": f"Bearer {token}",
                            "Accept": "application/vnd.github.raw+json",
                            "X-GitHub-Api-Version": "2022-11-28"},
                "timeout": self.config.timeout, "follow_redirects": False,
            }
            if payload is not None:
                kwargs["json"] = payload
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderTransportError("PROVIDER_UNAVAILABLE", "GitHub request had no usable response",
                                             retryable=True, accepted=method == "POST") from exc
            # GitHub rejects a bad credential before acting, so one retry with a
            # freshly minted App token cannot duplicate a write.
            if response.status_code != 401 or attempt == 2 or self.config.token_provider is None:
                break
            self.config.token_provider.invalidate(token)
        status = response.status_code
        if 200 <= status < 300:
            return response
        retry_after = self._retry_after(response.headers)
        # A plain 403 is a permission denial, not permission to retry indefinitely.
        limited = status == 429 or (status == 403 and retry_after is not None)
        if status == 403 and not limited:
            try:
                message = response.json().get("message", "").lower()
                limited = "rate limit" in message or "abuse detection" in message
            except (ValueError, AttributeError, TypeError):
                pass
        code = "RATE_LIMITED" if limited else {
            401: "AUTHENTICATION_FAILED", 403: "PROVIDER_FORBIDDEN", 404: "SCOPE_OR_NOT_FOUND",
            410: "SCOPE_OR_NOT_FOUND", 422: "INVALID_REQUEST",
        }.get(status, "PROVIDER_UNAVAILABLE" if status >= 500 else "PROVIDER_ERROR")
        raise ProviderTransportError(code, f"GitHub returned HTTP {status}", status=status,
                                     retryable=limited or status >= 500,
                                     retry_after=(retry_after or 60.0) if limited else retry_after,
                                     accepted=method == "POST" and status >= 500)

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise _error("MALFORMED_RESPONSE", "GitHub response is not JSON") from exc

    @staticmethod
    def _time(raw: dict) -> datetime:
        try:
            value = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError()
            return value
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise _error("MALFORMED_RESPONSE", "GitHub message lacks a valid creation time") from exc

    def _body(self, envelope: OutboundEnvelope) -> str:
        try:
            wire = parse_wire_message(envelope.payload.decode("utf-8"))
        except (UnicodeDecodeError, FramingError) as exc:
            raise _error("INVALID_OUTBOX", "outbox is not a valid WireMessage") from exc
        if (canonical_wire_bytes(wire) != envelope.payload
                or wire.operation_id != envelope.operation_id or wire.request_id != envelope.request_id
                or wire.packet.kind != envelope.kind
                or hashlib.sha256(envelope.payload).hexdigest() != envelope.content_sha256):
            raise _error("INVALID_OUTBOX", "outbox identity or content hash disagrees")
        if envelope.parent_message_id is not None:
            self._ids(envelope.parent_message_id)
        result = json.dumps({"schema": "gt.github-message/v1", "parent": envelope.parent_message_id,
                             "sha256": envelope.content_sha256, "wire": envelope.payload.decode("utf-8")},
                            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(result.encode("utf-8")) > self.config.max_body_bytes:
            raise _error("INVALID_OUTBOX", "GitHub body limit exceeded; no content was sent")
        return result

    def _decode_body(self, body: Any, thread: str) -> tuple[bytes | None, str | None]:
        if body is None:
            return None, None
        if not isinstance(body, str):
            raise _error("MALFORMED_RESPONSE", "GitHub body is not text")
        try:
            data = json.loads(body)
        except ValueError:
            return body.encode("utf-8"), None  # External prose remains unverified.
        if not isinstance(data, dict) or data.get("schema") != "gt.github-message/v1":
            return body.encode("utf-8"), None
        if set(data) != {"schema", "parent", "sha256", "wire"} or not isinstance(data["wire"], str):
            raise _error("INVALID_PACKET", "GitHub envelope is malformed")
        wire_bytes = data["wire"].encode("utf-8")
        if hashlib.sha256(wire_bytes).hexdigest() != data["sha256"]:
            raise _error("INVALID_PACKET", "GitHub envelope content hash changed")
        parent = data["parent"]
        if parent is not None:
            number, _ = self._ids(parent)
            if self._thread(number) != thread:
                raise ThreadScopeConflictError("GitHub parent belongs to another conversation")
        return wire_bytes, parent

    def _message(self, raw: Any, number: int, *, comment: bool) -> ProviderMessage:
        if not isinstance(raw, dict) or "pull_request" in raw:
            raise _error("MALFORMED_RESPONSE", "GitHub result is not an Issue message")
        if type(raw.get("id")) is not int or raw["id"] <= 0:
            raise _error("MALFORMED_RESPONSE", "GitHub message lacks an ID")
        thread = self._thread(number)
        if comment:
            if (raw.get("issue_url") != f"{self._base}/issues/{number}"
                    or raw.get("url") != f"{self._base}/issues/comments/{raw['id']}"):
                raise ThreadScopeConflictError()
            locator = f"{thread}:comment:{raw['id']}"
        else:
            if raw.get("number") != number or raw.get("url") != f"{self._base}/issues/{number}":
                raise ThreadScopeConflictError()
            locator = thread
        body, parent = self._decode_body(raw.get("body"), thread)
        return ProviderMessage(locator, thread, parent, self._time(raw), body, body is not None,
                               ContentSource.PROVIDER, "provider_unverified", exact_link_verified=False)

    def send(self, envelope: OutboundEnvelope) -> ProviderReference:
        body = self._body(envelope)
        number = None
        if envelope.parent_message_id is None:
            path = "/issues"
            payload = {"title": f"GT {envelope.request_id}", "body": body}
        else:
            number, _ = self._ids(envelope.parent_message_id)
            parent = self.fetch_message(envelope.parent_message_id)
            try:
                wire = parse_wire_message((parent.body or b"").decode("utf-8"))
            except (UnicodeDecodeError, FramingError) as exc:
                raise ThreadScopeConflictError("GitHub parent is not a GT message") from exc
            if wire.request_id != envelope.request_id:
                raise ThreadScopeConflictError("GitHub parent belongs to another request")
            path = f"/issues/{number}/comments"
            payload = {"body": body}
        response = self._request("POST", path, payload=payload)
        try:
            raw = self._json(response)
            if number is None:
                if not isinstance(raw, dict) or type(raw.get("number")) is not int or raw["number"] <= 0:
                    raise _error("MALFORMED_RESPONSE", "GitHub acceptance lacks Issue number")
                number = raw["number"]
            message = self._message(raw, number, comment=envelope.parent_message_id is not None)
            if (raw.get("user", {}).get("id") != self.config.writer_user_id or raw.get("body") != body
                    or message.parent_message_id != envelope.parent_message_id):
                raise _error("MALFORMED_RESPONSE", "GitHub acceptance differs from submitted message")
            return ProviderReference(message.message_id, message.thread_id, message.received_at,
                                     message.parent_message_id)
        except (ProviderTransportError, ThreadScopeConflictError, AttributeError, TypeError) as exc:
            # Even malformed success may have committed the write. Never resend.
            raise ProviderTransportError("MALFORMED_RESPONSE", "GitHub acceptance needs reconciliation",
                                         accepted=True, retryable=True) from exc

    def fetch_message(self, message_id: str) -> ProviderMessage:
        number, comment = self._ids(message_id)
        path = f"/issues/{number}" if comment is None else f"/issues/comments/{comment}"
        raw = self._json(self._request("GET", path))
        message = self._message(raw, number, comment=comment is not None)
        if message.message_id != message_id:
            raise ThreadScopeConflictError()
        return message

    def _list(self, path: str, page: int, **filters: str) -> tuple[list, bool]:
        if not 1 <= page <= self.config.max_pages:
            raise ReconciliationRequiredError("GitHub scan exceeds configured page budget")
        query = {**filters, "per_page": self.config.page_limit, "page": page}
        response = self._request("GET", path, query=query)
        raw = self._json(response)
        if not isinstance(raw, list) or len(raw) > self.config.page_limit or any(not isinstance(x, dict) for x in raw):
            raise _error("MALFORMED_RESPONSE", "GitHub listing is malformed")
        next_url = response.links.get("next", {}).get("url")
        if next_url:
            parsed = urlparse(next_url)
            expected = urlparse(self._base + path)
            wanted = {**{k: [str(v)] for k, v in query.items()}, "page": [str(page + 1)]}
            if ((parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path)
                    or parsed.fragment or parse_qs(parsed.query) != wanted or not raw):
                raise _error("MALFORMED_RESPONSE", "GitHub pagination escaped scope or did not advance")
        return raw, bool(next_url) or len(raw) == self.config.page_limit

    def fetch_thread(self, thread_id: str, cursor: str | None = None) -> Page:
        number, comment = self._ids(thread_id)
        if comment is not None:
            raise ThreadScopeConflictError("thread reference must identify an Issue")
        page = 1
        if cursor is not None:
            try:
                data = json.loads(cursor)
                if (set(data) != {"thread", "page"} or data["thread"] != thread_id
                        or type(data["page"]) is not int or data["page"] < 2):
                    raise ValueError()
                page = data["page"]
            except (ValueError, TypeError, KeyError) as exc:
                raise ThreadScopeConflictError("invalid or cross-thread GitHub cursor") from exc
        messages = [self.fetch_message(thread_id)] if page == 1 else []
        raw, more = self._list(f"/issues/{number}/comments", page)
        messages.extend(self._message(item, number, comment=True) for item in raw)
        next_cursor = json.dumps({"thread": thread_id, "page": page + 1}) if more else None
        return Page([{"message": item} for item in messages], next_cursor, self._clock())

    def fetch_thread_complete(self, thread_id: str) -> tuple[ProviderMessage, ...]:
        messages: dict[str, ProviderMessage] = {}
        cursor = None
        while True:
            page = self.fetch_thread(thread_id, cursor)
            for item in page.items:
                message = item["message"]
                previous = messages.get(message.message_id)
                if previous is not None and previous != message:
                    raise _error("INVALID_PACKET", "GitHub message changed during pagination")
                messages[message.message_id] = message
            cursor = page.next_cursor
            if cursor is None:
                return tuple(messages.values())

    def reconcile(self, envelope: OutboundEnvelope, *, created_at: datetime | None = None) -> ProviderReference:
        expected_body = self._body(envelope)
        number = self._ids(envelope.parent_message_id)[0] if envelope.parent_message_id else None
        path = f"/issues/{number}/comments" if number else "/issues"
        filters = {} if number else {"state": "all", "sort": "created", "direction": "asc"}
        matches: dict[str, ProviderReference] = {}
        page = 1
        while True:
            rows, more = self._list(path, page, **filters)
            for raw in rows:
                if "pull_request" in raw or raw.get("body") != expected_body:
                    continue
                user = raw.get("user")
                writers = (self.config.writer_user_id, *self.config.legacy_writer_user_ids)
                if not isinstance(user, dict) or user.get("id") not in writers:
                    continue
                issue_number = number or raw.get("number")
                if type(issue_number) is not int or issue_number <= 0:
                    raise _error("MALFORMED_RESPONSE", "GitHub candidate lacks Issue number")
                message = self._message(raw, issue_number, comment=number is not None)
                matches[message.message_id] = ProviderReference(
                    message.message_id, message.thread_id, message.received_at, message.parent_message_id)
            if not more:
                break
            page += 1
        if len(matches) != 1:
            raise ReconciliationRequiredError("GitHub acceptance absent or ambiguous; automatic resend is unsafe")
        return next(iter(matches.values()))
