"""GitHub App installation tokens for the hub's GitHub credentials.

A GitHub App's private key does not expire. The hub signs a short JSON Web
Token with it and exchanges that for an installation access token that GitHub
limits to one hour. Each token is narrowed to one repository and one
permission, cached, and replaced shortly before it expires, so nothing needs
manual renewal. Nothing here writes a credential to disk, and no error message
contains key or token material.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub accepts an app JSON Web Token for at most ten minutes. Back-dating the
# issue time by a minute tolerates modest clock drift, as GitHub recommends.
JWT_BACKDATE = timedelta(seconds=60)
JWT_LIFETIME = timedelta(minutes=9)
# Installation tokens last one hour; replace one well before it runs out.
REFRESH_MARGIN = timedelta(minutes=10)

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PERMISSION_LEVELS = frozenset({"read", "write"})


class GitHubAppError(RuntimeError):
    """A token could not be obtained. Messages never include key or token material."""

    def __init__(self, code: str, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


def load_private_key(path: Path) -> str:
    """Read and parse a PEM RSA private key without any network access."""

    try:
        pem = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise GitHubAppError("CONFIG_ERROR", "GitHub App private key file is unreadable") from exc
    try:
        key = load_pem_private_key(pem.encode("ascii"), password=None)
    except (ValueError, TypeError) as exc:
        raise GitHubAppError("CONFIG_ERROR", "GitHub App private key file is not an unencrypted PEM private key") from exc
    if not isinstance(key, RSAPrivateKey):
        raise GitHubAppError("CONFIG_ERROR", "GitHub App private key must be an RSA key")
    return pem


@dataclass(frozen=True, slots=True)
class _CachedToken:
    token: str
    expires_at: datetime


class GitHubAppTokenProvider:
    """Mint and cache installation tokens narrowed to one repository and permission set."""

    def __init__(
        self,
        *,
        app_id: int,
        installation_id: int,
        private_key: str,
        repository: str,
        permissions: Mapping[str, str],
        client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        api_base: str = API_BASE,
        timeout: float = 20.0,
        refresh_margin: timedelta = REFRESH_MARGIN,
    ) -> None:
        for name, value in (("app_id", app_id), ("installation_id", installation_id)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("repository must be an exact owner/name")
        if (not permissions or any(not isinstance(k, str) or not k or v not in _PERMISSION_LEVELS
                                   for k, v in permissions.items())):
            raise ValueError("permissions must map permission names to read or write")
        if "metadata" in permissions:
            raise ValueError("metadata is granted automatically and must not be requested")
        if not isinstance(private_key, str) or "PRIVATE KEY" not in private_key:
            raise ValueError("private_key must be PEM text")
        if not api_base.startswith("https://") or api_base.endswith("/"):
            raise ValueError("api_base must be an HTTPS origin without a trailing slash")
        if timeout <= 0 or refresh_margin <= timedelta(0) or refresh_margin >= timedelta(minutes=50):
            raise ValueError("timeout and refresh margin must be positive, and the margin under 50 minutes")
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._repository = repository
        self._repository_name = repository.split("/", 1)[1]
        self._permissions = dict(permissions)
        self._client = client or httpx.Client()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._url = f"{api_base}/app/installations/{installation_id}/access_tokens"
        self._timeout = timeout
        self._refresh_margin = refresh_margin
        self._lock = threading.Lock()
        self._cached: _CachedToken | None = None

    @property
    def repository(self) -> str:
        return self._repository

    def token(self) -> str:
        """Return a current token, minting a new one when none is cached or it expires soon."""

        with self._lock:
            now = self._clock()
            cached = self._cached
            if cached is None or cached.expires_at - self._refresh_margin <= now:
                cached = self._cached = self._mint(now)
            return cached.token

    def invalidate(self, token: str) -> None:
        """Drop a token GitHub rejected so the next call mints a fresh one."""

        with self._lock:
            if self._cached is not None and self._cached.token == token:
                self._cached = None

    def _assertion(self, now: datetime) -> str:
        issued = now - JWT_BACKDATE
        claims = {
            "iat": int(issued.timestamp()),
            "exp": int((issued + JWT_LIFETIME).timestamp()),
            "iss": str(self._app_id),
        }
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    def _mint(self, now: datetime) -> _CachedToken:
        try:
            response = self._client.request(
                "POST",
                self._url,
                headers={
                    "Authorization": f"Bearer {self._assertion(now)}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                json={"repositories": [self._repository_name], "permissions": self._permissions},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise GitHubAppError(
                "TOKEN_UNAVAILABLE", "GitHub App token request had no usable response", retryable=True,
            ) from exc
        status = response.status_code
        if status != 201:
            retryable = status == 429 or status >= 500
            raise GitHubAppError(
                "TOKEN_UNAVAILABLE" if retryable else "TOKEN_REFUSED",
                f"GitHub refused the App token request (HTTP {status})",
                status=status,
                retryable=retryable,
            )
        return self._validated(response, now)

    def _validated(self, response: Any, now: datetime) -> _CachedToken:
        def malformed(reason: str) -> GitHubAppError:
            return GitHubAppError("TOKEN_MALFORMED", f"GitHub App token response is malformed: {reason}")

        try:
            body = response.json()
        except ValueError as exc:
            raise malformed("not JSON") from exc
        if not isinstance(body, dict):
            raise malformed("not an object")
        token = body.get("token")
        if not isinstance(token, str) or not token or any(character.isspace() for character in token):
            raise malformed("missing token")
        try:
            expires_at = datetime.fromisoformat(str(body.get("expires_at")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise malformed("invalid expiry") from exc
        if expires_at.tzinfo is None or expires_at <= now + self._refresh_margin:
            raise malformed("expiry is missing a timezone or too soon")
        # GitHub narrows the token to what was asked for. Refuse anything broader
        # than the requested permissions plus the automatic read-only metadata.
        granted = body.get("permissions")
        if not isinstance(granted, dict):
            raise malformed("permissions missing")
        granted = dict(granted)
        metadata = granted.pop("metadata", None)
        if granted != self._permissions or metadata not in (None, "read"):
            raise malformed("permissions differ from the request")
        repositories = body.get("repositories")
        if (not isinstance(repositories, list) or len(repositories) != 1 or not isinstance(repositories[0], dict)
                or str(repositories[0].get("full_name", "")).casefold() != self._repository.casefold()):
            raise malformed("repository scope differs from the request")
        return _CachedToken(token, expires_at)


class AppTokenHTTPTransport:
    """Brain Core ``HTTPTransport`` that authenticates each request with an App token.

    The Brain Home storage is constructed with a placeholder token; this wrapper
    replaces the Authorization header on every request, so the placeholder never
    leaves the process. A 401 means GitHub rejected the credential before doing
    anything, so one retry with a freshly minted token is safe even for writes.
    """

    def __init__(self, provider: GitHubAppTokenProvider, inner: Any) -> None:
        self._provider = provider
        self._inner = inner

    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None) -> Any:
        from brain_core.github_storage import GitHubTransportError

        for attempt in (1, 2):
            try:
                token = self._provider.token()
            except GitHubAppError as exc:
                # Report as unavailable: no request was sent, and callers already
                # retry an unavailable Brain Home with the same operation identity.
                raise GitHubTransportError(
                    "github_transport_unavailable",
                    "GitHub App token is unavailable",
                    details={"error": exc.code, "status": exc.status},
                ) from exc
            response = self._inner.request(
                method, url, headers={**headers, "Authorization": f"Bearer {token}"}, body=body,
            )
            if response.status != 401 or attempt == 2:
                return response
            self._provider.invalidate(token)
        raise AssertionError("unreachable")


# The storage requires a non-empty token string. With an App, the transport above
# supplies the real Authorization header, so this placeholder is never sent.
APP_TOKEN_PLACEHOLDER = "github-app-installation-token"
