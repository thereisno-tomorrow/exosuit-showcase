"""Authenticated HTTPS client for Gremlin Town participant adapters.

This module deliberately contains no service, lifecycle, provider, or storage
logic.  A bearer credential establishes the caller's identity at the hub; no
operation payload can select a credential or endpoint.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


JsonObject = Mapping[str, Any]


def work_check_only(client: "HubHTTPSClient", payload: JsonObject) -> Any:
    """Validate one exact work change through a door-enabled client without publishing.

    Maintenance-only HTTP body flag; never registered as a model tool. The hub
    owns validation and must leave Brain, calendar and why-log state untouched.
    No retries, writer substitution or local state changes.
    """
    if not isinstance(payload, Mapping):
        raise HubClientError("INVALID_ARGUMENT", "work arguments must be an object")
    return client.call("work", {**payload, "check_only": True})

# Grok's shared-cloud preflight found no programmatic managed-secret insertion
# seam. This fixed path is the approved configuration shape for a future
# enrollment; this client does not create, chmod, or populate it.
GROK_TOKEN_FILE = Path("/home/box/.config/gremlin-town/grok.token")
_IS_POSIX = os.name == "posix"

TEN_OPERATIONS = (
    "participants.list",
    "requests.create",
    "requests.list",
    "requests.read",
    "requests.report",
    "requests.reply",
    "requests.cancel",
    "requests.check",
    "operations.get",
    "events.list",
)

DOOR_OPERATIONS = ("work",)

BRAIN_OPERATIONS = ('memory.search', 'memory.list', 'context.retrieve', 'continuity.start', 'continuity.append', 'continuity.recover', 'continuity.list', 'continuity.close', 'decision.record', 'memory.save', 'memory.change', 'memory.read', 'orient', 'next', 'next.respond', 'agenda', 'weekly', 'calendar.remove')

# Step E's [review] feature (off by default; see docs/product/steps/e-review-and-return.md).
# Not GT's ten-operation idempotent-retry surface: neither call needs a
# client_operation_id, so neither is in MUTATING_OPERATIONS below.
REVIEW_OPERATIONS = ("review.respond", "moments.signal")

BRAIN_MUTATING_OPERATIONS = frozenset((*BRAIN_OPERATIONS, *DOOR_OPERATIONS)) - {
    "context.retrieve", "memory.read", "memory.search", "memory.list", "continuity.recover", "continuity.list",
    # orient and next are read-only. next.respond is a write, but its own idempotency comes from
    # replaying the same (surfacing_id, response) - not from client_operation_id/operation_id,
    # which its schema carries neither of (step F spec §6: "replaying the same yes returns the
    # same note"). Requiring operation_id here would demand an argument the tool doesn't have.
    "orient", "next", "next.respond",
    # agenda (PRD §4.9) and weekly (§4.8) only read.
    "agenda", "weekly",
}


MUTATING_OPERATIONS = frozenset(
    {
        "requests.create",
        "requests.report",
        "requests.reply",
        "requests.cancel",
        "requests.check",
    }
)

_CONFIG_ARGUMENTS = frozenset(
    {
        "actor",
        "actor_id",
        "authorization",
        "base_url",
        "endpoint",
        "hub_token",
        "hub_url",
        "sender",
        "token",
        "url",
    }
)

_SAFE_ERROR_FIELDS = frozenset(
    {
        "code",
        "field",
        "fix",
        "message",
        "retryable",
        "retry_after",
        "request_id",
        "operation_id",
        "expected_revision",
        "current_revision",
        "next_action",
        "recovery_action",
        "disposition",
    }
)


class HTTPSRequester(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class _HTTPResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward this participant's bearer credential.
        return None


class StandardLibraryHTTPSRequester:
    """Small urllib transport so packaged participant clients need no SDK."""

    def request(self, method: str, url: str, **kwargs: Any) -> _HTTPResponse:
        query = kwargs.get("params")
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        body = kwargs.get("json")
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            data=encoded_body,
            headers=dict(kwargs.get("headers", {})),
            method=method,
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=float(kwargs.get("timeout", 20.0))) as response:
                return _HTTPResponse(
                    status_code=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as exc:
            return _HTTPResponse(
                status_code=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers is not None else {},
                body=exc.read(),
            )


class HubClientError(RuntimeError):
    """A structured, participant-safe client or hub error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.status_code is not None:
            error["status_code"] = self.status_code
        if self.retry_after is not None:
            error["retry_after"] = self.retry_after
        for key, value in self.details.items():
            if key in _SAFE_ERROR_FIELDS and key not in error:
                error[key] = value
        return error


@dataclass(frozen=True, slots=True)
class HubClientConfig:
    base_url: str
    token: str
    timeout: float = 20.0

    def __post_init__(self) -> None:
        raw_url = self.base_url.strip()
        split = urlsplit(raw_url)
        if (
            split.scheme.lower() != "https"
            or not split.netloc
            or split.username is not None
            or split.password is not None
            or split.query
            or split.fragment
        ):
            raise HubClientError(
                "CONFIGURATION_INVALID",
                "GREMLIN_TOWN_HUB_URL must be an HTTPS origin or path without credentials, query, or fragment",
            )
        if not self.token.strip():
            raise HubClientError(
                "CONFIGURATION_MISSING",
                "a hub token is required from protected environment or token-file configuration",
            )
        if self.timeout <= 0:
            raise HubClientError("CONFIGURATION_INVALID", "hub timeout must be positive")
        normalized_path = split.path.rstrip("/")
        object.__setattr__(
            self,
            "base_url",
            urlunsplit(("https", split.netloc, normalized_path, "", "")),
        )

    @classmethod
    def from_environment(cls, *, settings_path: Path | None = None) -> "HubClientConfig":
        settings: dict[str, str] = {}
        if settings_path is not None:
            try:
                raw_settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw_settings = {}
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise HubClientError(
                    "CONFIGURATION_INVALID",
                    "the non-secret hub settings file is unreadable or invalid",
                ) from exc
            if not isinstance(raw_settings, dict) or set(raw_settings) - {"hub_url", "token_file"}:
                raise HubClientError(
                    "CONFIGURATION_INVALID",
                    "the non-secret hub settings file has unsupported fields",
                )
            if not all(isinstance(value, str) for value in raw_settings.values()):
                raise HubClientError(
                    "CONFIGURATION_INVALID",
                    "the non-secret hub settings values must be strings",
                )
            settings = raw_settings
        base_url = os.environ.get("GREMLIN_TOWN_HUB_URL", settings.get("hub_url", ""))
        token = os.environ.get("GREMLIN_TOWN_HUB_TOKEN", "").strip()
        configured_token_file = os.environ.get(
            "GREMLIN_TOWN_HUB_TOKEN_FILE", settings.get("token_file", "")
        ).strip()
        timeout_text = os.environ.get("GREMLIN_TOWN_HUB_TIMEOUT", "20")
        if not base_url:
            raise HubClientError(
                "CONFIGURATION_MISSING",
                "GREMLIN_TOWN_HUB_URL is required",
            )
        if not token:
            token_path = Path(configured_token_file) if configured_token_file else GROK_TOKEN_FILE
            if configured_token_file and not (
                token_path.is_absolute() or PurePosixPath(configured_token_file).is_absolute()
            ):
                raise HubClientError(
                    "CONFIGURATION_INVALID",
                    "GREMLIN_TOWN_HUB_TOKEN_FILE must be an absolute protected-runtime path",
                )
            token = _read_protected_token_file(token_path)
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise HubClientError(
                "CONFIGURATION_INVALID", "GREMLIN_TOWN_HUB_TIMEOUT must be a number"
            ) from exc
        return cls(base_url=base_url, token=token, timeout=timeout)


def _read_protected_token_file(path: Path) -> str:
    """Read the fixed Grok token file without weakening or changing custody."""

    try:
        parent_stat = path.parent.lstat()
        file_stat = path.lstat()
    except OSError as exc:
        raise HubClientError(
            "CONFIGURATION_MISSING",
            "the configured protected hub token file is unavailable",
        ) from exc

    if path.parent.is_symlink() or path.is_symlink() or not stat.S_ISDIR(parent_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise HubClientError(
            "CONFIGURATION_INVALID",
            "the fixed protected Grok token path must be a regular file inside a real directory",
        )
    if _IS_POSIX:
        current_uid = os.geteuid()
        if parent_stat.st_uid != current_uid or file_stat.st_uid != current_uid:
            raise HubClientError(
                "CONFIGURATION_INVALID",
                "the fixed protected Grok token file and directory must be owned by the runtime user",
            )
        if stat.S_IMODE(parent_stat.st_mode) != 0o700 or stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise HubClientError(
                "CONFIGURATION_INVALID",
                "the fixed Grok token directory must be mode 0700 and token file mode 0600",
            )
    if file_stat.st_size > 16 * 1024:
        raise HubClientError("CONFIGURATION_INVALID", "the fixed protected Grok token file is too large")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HubClientError("CONFIGURATION_INVALID", "the fixed protected Grok token file could not be read") from exc
    if not token or "\n" in token or "\r" in token:
        raise HubClientError(
            "CONFIGURATION_INVALID",
            "the fixed protected Grok token file must contain exactly one non-empty token",
        )
    return token


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    path: str
    query: Mapping[str, Any] | None = None
    body: Mapping[str, Any] | None = None


class HubHTTPSClient:
    """Translate GT and Brain calls over HTTPS with one protected participant identity."""

    def __init__(self, config: HubClientConfig, *, client: HTTPSRequester | None = None,
                 door: bool = False) -> None:
        self.config = config
        self.door = door
        self._client = client or StandardLibraryHTTPSRequester()

    def call(self, operation: str, payload: JsonObject | None = None) -> Any:
        if operation not in (*TEN_OPERATIONS, *BRAIN_OPERATIONS, *REVIEW_OPERATIONS,
                             *(DOOR_OPERATIONS if self.door else ())):
            raise HubClientError("INVALID_OPERATION", "operation is not in the participant surface")
        if payload is not None and not isinstance(payload, Mapping):
            raise HubClientError("INVALID_ARGUMENT", "operation arguments must be an object")
        arguments = dict(payload or {})
        forbidden = sorted(_CONFIG_ARGUMENTS.intersection(arguments))
        if forbidden:
            raise HubClientError(
                "INVALID_ARGUMENT",
                f"operation arguments cannot set protected field: {forbidden[0]}",
            )
        if operation in MUTATING_OPERATIONS:
            operation_id = arguments.get("client_operation_id")
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise HubClientError(
                    "INVALID_ARGUMENT",
                    "client_operation_id is required for mutating operations and must be reused for retries",
                )

        if operation in BRAIN_MUTATING_OPERATIONS:
            operation_id = arguments.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise HubClientError(
                    "INVALID_ARGUMENT",
                    "operation_id is required and exact input must be retained for retries",
                )
        # No actor injection, owner substitution, default insertion or automatic retry.
        request = (_Request("POST", f"/v1/brain/{operation}", body=arguments)
                   if operation in (*BRAIN_OPERATIONS, *DOOR_OPERATIONS) else _map_request(operation, arguments))
        url = f"{self.config.base_url}{request.path}"
        is_brain_write = operation in BRAIN_MUTATING_OPERATIONS
        timeout = max(self.config.timeout, 60.0) if is_brain_write else self.config.timeout
        kwargs: dict[str, Any] = {
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.token}",
            },
            "timeout": timeout,
        }
        if request.query:
            kwargs["params"] = request.query
        if request.body is not None:
            kwargs["headers"]["Content-Type"] = "application/json"
            kwargs["json"] = request.body

        try:
            response = self._client.request(request.method, url, **kwargs)
        except TimeoutError as exc:
            if is_brain_write:
                raise HubClientError(
                    "TIMEOUT",
                    f"Not saved: the hub at {self.config.base_url} did not respond in time.",
                    retryable=True,
                ) from exc
            raise HubClientError("TIMEOUT", "hub request timed out", retryable=True) from exc
        except (OSError, URLError) as exc:
            if is_brain_write:
                raise HubClientError(
                    "UNAVAILABLE",
                    f"Not saved: the hub at {self.config.base_url} could not be reached.",
                    retryable=True,
                ) from exc
            raise HubClientError("UNAVAILABLE", "hub connection failed", retryable=True) from exc

        status = int(response.status_code)
        if not 200 <= status < 300:
            raise _response_error(response, status)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HubClientError(
                "MALFORMED_RESPONSE",
                "hub response was not JSON",
                status_code=status,
            ) from exc


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise HubClientError("INVALID_ARGUMENT", f"{name} is required")
    return value


def _query(arguments: Mapping[str, Any], *, expand_filters: bool = False) -> dict[str, Any]:
    query = {key: value for key, value in arguments.items() if value is not None}
    if expand_filters:
        filters = query.pop("filters", {})
        if not isinstance(filters, Mapping):
            raise HubClientError("INVALID_ARGUMENT", "filters must be a JSON object")
        overlap = set(filters).intersection(query)
        if overlap:
            raise HubClientError("INVALID_ARGUMENT", "filter names conflict with top-level query fields")
        query.update(filters)
    return query


def _without(arguments: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in arguments.items() if name != key}


def _map_request(operation: str, arguments: Mapping[str, Any]) -> _Request:
    if operation == "participants.list":
        return _Request("GET", "/v1/participants", query=_query(arguments))
    if operation == "requests.create":
        return _Request("POST", "/v1/requests", body=arguments)
    if operation == "requests.list":
        return _Request("GET", "/v1/requests", query=_query(arguments, expand_filters=True))
    if operation == "requests.read":
        request_id = quote(_required_text(arguments, "request_id"), safe="")
        return _Request("GET", f"/v1/requests/{request_id}", query=_query(_without(arguments, "request_id")))
    if operation == "requests.report":
        request_id = quote(_required_text(arguments, "request_id"), safe="")
        return _Request("POST", f"/v1/requests/{request_id}/reports", body=_without(arguments, "request_id"))
    if operation == "requests.reply":
        request_id = quote(_required_text(arguments, "request_id"), safe="")
        return _Request("POST", f"/v1/requests/{request_id}/replies", body=_without(arguments, "request_id"))
    if operation == "requests.cancel":
        request_id = quote(_required_text(arguments, "request_id"), safe="")
        return _Request("POST", f"/v1/requests/{request_id}/cancel", body=_without(arguments, "request_id"))
    if operation == "requests.check":
        request_id = quote(_required_text(arguments, "request_id"), safe="")
        return _Request("POST", f"/v1/requests/{request_id}/check", body=_without(arguments, "request_id"))
    if operation == "operations.get":
        operation_id = quote(_required_text(arguments, "operation_id"), safe="")
        return _Request("GET", f"/v1/operations/{operation_id}", query=_query(_without(arguments, "operation_id")))
    if operation == "events.list":
        return _Request("GET", "/v1/events", query=_query(arguments, expand_filters=True))
    if operation == "review.respond":
        return _Request("POST", "/v1/review/responses", body=arguments)
    if operation == "moments.signal":
        return _Request("POST", "/v1/moments/signals", body=arguments)
    raise HubClientError("INVALID_OPERATION", "operation is not in the participant surface")


def _response_error(response: Any, status: int) -> HubClientError:
    retry_after: float | None = None
    try:
        retry_after = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        pass

    payload: Any = None
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        pass
    candidate: Mapping[str, Any] = {}
    if isinstance(payload, Mapping):
        nested = payload.get("error", payload.get("detail", payload))
        if isinstance(nested, Mapping):
            candidate = nested

    default_code = {
        400: "REQUEST_FAILED",
        401: "AUTHENTICATION_FAILED",
        403: "ACCESS_DENIED",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "INVALID_ARGUMENT",
        429: "RATE_LIMITED",
    }.get(status, "UNAVAILABLE" if status >= 500 else "REQUEST_FAILED")
    code = candidate.get("code")
    message = candidate.get("message")
    retryable_value = candidate.get("retryable")
    details = {key: value for key, value in candidate.items() if key in _SAFE_ERROR_FIELDS}
    return HubClientError(
        code if isinstance(code, str) and code else default_code,
        message if isinstance(message, str) and message else f"hub returned HTTP {status}",
        status_code=status,
        retryable=retryable_value if isinstance(retryable_value, bool) else status == 429 or status >= 500,
        retry_after=retry_after,
        details=details,
    )
