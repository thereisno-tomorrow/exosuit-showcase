"""Google OAuth credentials and token refresh for the Google readers and the calendar writer.

Nothing here performs a browser consent flow: ``GoogleCredential.load`` only
reads a JSON file Nic creates himself with Google's installed-app flow, and
``GoogleTokenProvider`` only exchanges the refresh token already in that file
for a short-lived access token. The allowed scopes are enforced here, not just
documented (step D spec §3.5, changed by N8 on 28 Sep 2026): a credential must
name exactly calendar entries (``calendar.events``, the one write Exosuit makes)
and read-only Gmail. Any other set is refused before it is ever used, so no
request is sent. The refusal keeps its old reason code, ``scope_not_read_only``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from gremlin_town.sources.result import UnavailableReason

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# N8 (28 Sep 2026): calendar entries need write access to events, and nothing more; Gmail
# stays read-only. Exactly this pair, never a superset (step D spec §1, §3.5).
ALLOWED_SCOPES = frozenset({CALENDAR_EVENTS_SCOPE, GMAIL_READ_ONLY_SCOPE})
REFRESH_MARGIN = timedelta(seconds=60)
_REQUIRED_TEXT_FIELDS = ("client_id", "client_secret", "refresh_token", "account")


class GoogleAuthError(RuntimeError):
    """A credential or token could not be used. Never carries secret material."""

    def __init__(self, reason: UnavailableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _required_text(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise GoogleAuthError(UnavailableReason.CREDENTIAL_INVALID,
                              f"Google credential is missing {field}")
    return value


@dataclass(frozen=True, slots=True)
class GoogleCredential:
    client_id: str
    client_secret: str
    refresh_token: str
    scopes: frozenset[str]
    account: str

    @classmethod
    def load(cls, path: Path) -> "GoogleCredential":
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GoogleAuthError(UnavailableReason.CREDENTIAL_MISSING,
                                  "Google credential file is unreadable") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoogleAuthError(UnavailableReason.CREDENTIAL_INVALID,
                                  "Google credential file is not valid JSON") from exc
        if not isinstance(data, dict):
            raise GoogleAuthError(UnavailableReason.CREDENTIAL_INVALID,
                                  "Google credential file is not an object")

        client_id, client_secret, refresh_token, account = (
            _required_text(data, field) for field in _REQUIRED_TEXT_FIELDS
        )

        scopes_value = data.get("scopes")
        if not isinstance(scopes_value, list) or not all(
            isinstance(scope, str) and scope for scope in scopes_value
        ):
            raise GoogleAuthError(UnavailableReason.CREDENTIAL_INVALID,
                                  "Google credential scopes must be a list of strings")
        scopes = frozenset(scopes_value)
        if scopes != ALLOWED_SCOPES:
            raise GoogleAuthError(UnavailableReason.SCOPE_NOT_READ_ONLY,
                                  "Google credential scopes are not exactly the allowed pair")

        return cls(client_id, client_secret, refresh_token, scopes, account)

    @property
    def writes_calendar(self) -> bool:
        """Whether this credential lets Exosuit add its own calendar entries (N8)."""

        return CALENDAR_EVENTS_SCOPE in self.scopes


@dataclass(frozen=True, slots=True)
class _CachedAccessToken:
    token: str
    expires_at: datetime


class GoogleTokenProvider:
    """Exchange the stored refresh token for a short-lived access token.

    Holds no browser or consent code: the refresh token already came from
    Nic's own use of Google's installed-app flow. A rejected refresh token
    (``invalid_grant``) means the token was revoked, which is reported as
    ``token_revoked``, not ``credential_invalid`` — the file was fine when it
    was written.
    """

    def __init__(
        self,
        credential: GoogleCredential,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        token_url: str = TOKEN_URL,
        timeout: float = 20.0,
        refresh_margin: timedelta = REFRESH_MARGIN,
    ) -> None:
        self._credential = credential
        self._client = httpx.Client(transport=transport) if transport is not None else httpx.Client()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_url = token_url
        self._timeout = timeout
        self._refresh_margin = refresh_margin
        self._lock = threading.Lock()
        self._cached: _CachedAccessToken | None = None

    def token(self) -> str:
        """Return a current access token, refreshing when none is cached or it expires soon."""

        with self._lock:
            now = self._clock()
            cached = self._cached
            if cached is None or cached.expires_at - self._refresh_margin <= now:
                cached = self._cached = self._refresh(now)
            return cached.token

    def _refresh(self, now: datetime) -> _CachedAccessToken:
        try:
            response = self._client.request(
                "POST",
                self._token_url,
                data={
                    "client_id": self._credential.client_id,
                    "client_secret": self._credential.client_secret,
                    "refresh_token": self._credential.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GoogleAuthError(UnavailableReason.NETWORK_ERROR,
                                  "Google token refresh had no usable response") from exc
        if response.status_code != 200:
            raise self._refusal(response)
        return self._validated(response, now)

    def _refusal(self, response: httpx.Response) -> GoogleAuthError:
        if response.status_code == 429:
            return GoogleAuthError(UnavailableReason.RATE_LIMITED, "Google token refresh was rate limited")
        if response.status_code >= 500:
            return GoogleAuthError(UnavailableReason.PROVIDER_ERROR, "Google token refresh failed on Google's side")
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if error == "invalid_grant":
            return GoogleAuthError(UnavailableReason.TOKEN_REVOKED, "Google refresh token was rejected")
        return GoogleAuthError(UnavailableReason.CREDENTIAL_INVALID, "Google token refresh was refused")

    def _validated(self, response: httpx.Response, now: datetime) -> _CachedAccessToken:
        try:
            body = response.json()
        except ValueError as exc:
            raise GoogleAuthError(UnavailableReason.PROVIDER_ERROR,
                                  "Google token response was not JSON") from exc
        if not isinstance(body, dict):
            raise GoogleAuthError(UnavailableReason.PROVIDER_ERROR,
                                  "Google token response was not an object")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise GoogleAuthError(UnavailableReason.PROVIDER_ERROR,
                                  "Google token response is missing access_token")
        expires_in = body.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise GoogleAuthError(UnavailableReason.PROVIDER_ERROR,
                                  "Google token response has an invalid expiry")
        return _CachedAccessToken(token, now + timedelta(seconds=expires_in))
