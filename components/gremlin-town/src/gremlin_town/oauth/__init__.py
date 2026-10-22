"""Preregistered-client OAuth for the public MCP listener.

Each configured client is one public PKCE app enrolled as exactly one OAuth-only
participant, such as chatgpt-web or claude-web.
The public routes never accept an operator credential. Attach private_routes only
to the existing private listener and supply its authenticated session resolver
and the list of its signed-in sessions, which approval pages recheck.
The separate SQLite file is live authentication state: do not restore it from
backups. A restored hub requires new consent and a new OAuth database.
"""

from .provider import OAuthClient, OAuthConfig, OAuthProvider

__all__ = ['OAuthClient', 'OAuthConfig', 'OAuthProvider']
