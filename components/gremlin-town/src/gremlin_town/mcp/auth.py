"""Production OAuth identity, revalidated for every tool invocation."""

from __future__ import annotations

from collections.abc import Callable, Collection
from contextvars import ContextVar
from dataclasses import dataclass
import time

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from gremlin_town.contracts import Actor
from gremlin_town.domain.errors import GremlinTownError
from gremlin_town.domain.policy import Policy
from gremlin_town.oauth.provider import OAuthProvider, SUPPORTED_SCOPES


@dataclass(frozen=True)
class _Verified:
    request_token: AccessToken
    actor: Actor
    scopes: frozenset[str]


class ProductionAuth:
    """The actor is always the participant enrolled for the token's OAuth client."""

    def __init__(self, provider: OAuthProvider, policy: Policy, *, enabled: Callable[[], bool] = lambda: True):
        self.provider = provider
        self.policy = policy
        self.enabled = enabled
        self._verified: ContextVar[_Verified | None] = ContextVar('gt_verified_oauth', default=None)

    def _participant_enabled(self, actor_id: str) -> bool:
        if not self.enabled():
            return False
        try:
            participant = self.policy.participant(actor_id)
        except GremlinTownError:
            return False
        return not participant.is_operator

    async def verify_token(self, token: str) -> AccessToken | None:
        """SDK transport verifier. Never sets invocation identity in context."""
        if not token or not self.enabled():
            return None
        verified = await self.provider.verify_token(token)
        client = self.provider.config.client(verified.client_id) if verified is not None else None
        if (verified is None or client is None
                or verified.subject != client.participant
                or not self._participant_enabled(client.participant)
                or verified.token != token
                or verified.resource != self.provider.config.resource_url
                or not verified.claims
                or verified.claims.get('iss') != self.provider.config.issuer_url
                or verified.expires_at is None or verified.expires_at <= time.time()
                or not verified.scopes or not set(verified.scopes) <= set(SUPPORTED_SCOPES)):
            return None
        return verified

    async def authorize(self) -> None:
        """Re-read durable grant and current policy even on an existing session."""
        self._verified.set(None)
        current = get_access_token()
        if current is None:
            raise PermissionError('participant authentication required')
        verified = await self.verify_token(current.token)
        if verified is None:
            raise PermissionError('participant authentication required')
        self._verified.set(_Verified(current, Actor(verified.subject), frozenset(verified.scopes)))

    def _context(self) -> _Verified:
        verified = self._verified.get()
        if (verified is None or get_access_token() is not verified.request_token
                or not self._participant_enabled(verified.actor.actor_id)):
            raise PermissionError('participant authentication required')
        return verified

    def actor(self) -> Actor:
        return self._context().actor

    def scopes(self) -> Collection[str]:
        return self._context().scopes
