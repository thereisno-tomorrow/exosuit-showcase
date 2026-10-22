"""Authentication helpers for the thin HTTP surface.

Bearer credentials or an authenticated session are the only inputs used to
establish identity. Request bodies, packet fields and query parameters are
deliberately never consulted for actor identity.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
import secrets
from typing import Any

from fastapi import Request

from gremlin_town.contracts import Actor


class AuthenticationError(Exception):
    """Raised when a request cannot establish an authenticated actor."""


ActorResolver = Callable[[str], Actor | None]


class ActorAuthenticator:
    """Resolve bearer tokens into actors without trusting packet text."""

    def __init__(
        self,
        resolver: ActorResolver | Mapping[str, Actor | str] | None = None,
    ) -> None:
        if resolver is None:
            # Fail closed. A bearer token is an opaque credential, never an
            # actor id; deployments must provide an explicit resolver.
            self._resolver: ActorResolver = lambda token: None
        elif isinstance(resolver, Mapping):
            values = dict(resolver)

            def resolve(token: str) -> Actor | None:
                value = values.get(token)
                if isinstance(value, Actor):
                    return value
                if isinstance(value, str):
                    return Actor(actor_id=value)
                return None

            self._resolver = resolve
        else:
            self._resolver = resolver

    def authenticate(self, authorization: str | None) -> Actor:
        if not authorization:
            raise AuthenticationError("missing bearer credentials")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip() or " " in token.strip():
            raise AuthenticationError("invalid bearer credentials")
        actor = self._resolver(token.strip())
        if actor is None or not actor.actor_id:
            raise AuthenticationError("invalid bearer credentials")
        return actor

    def __call__(self, request: Request) -> Actor:
        return self.authenticate(request.headers.get("authorization"))


def get_actor(request: Request) -> Actor:
    """FastAPI-friendly request dependency used by all HTTP and HTML routes."""

    authenticator: ActorAuthenticator = request.app.state.actor_authenticator
    try:
        return authenticator(request)
    except AuthenticationError:
        sessions = request.app.state.sessions
        session_id = request.cookies.get(request.app.state.session_cookie_name)
        session = sessions.get(session_id) if session_id else None
        if session is None:
            raise
        actor, expires_at = session
        if expires_at <= datetime.now(UTC):
            sessions.pop(session_id, None)
            raise AuthenticationError("expired session")
        return actor


def new_session(actor: Actor, *, ttl_seconds: int) -> tuple[str, datetime]:
    return secrets.token_urlsafe(32), datetime.now(UTC) + timedelta(seconds=ttl_seconds)


def actor_resolver_from_app(request: Request) -> Any:
    """Small compatibility hook for callers that need a named dependency."""

    return get_actor(request)
