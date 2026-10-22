"""OAuth controls on the existing, operator-session-only private application.

Consent is served on the public issuer. This listener proves the operator
session by minting a one-time handoff, and it hosts connection revoke.
"""

from __future__ import annotations

from datetime import UTC, datetime
import html
import secrets

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from gremlin_town.contracts import ErrorCode
from gremlin_town.domain.errors import GremlinTownError
from gremlin_town.oauth.provider import HEADERS, OAuthClient, OAuthProvider


def _client_section(position: int, client: OAuthClient) -> str:
    name = html.escape(client.name)
    section = (
        '<section><h2>' + name + '</h2>'
        '<p>' + name + ' uses the ' + html.escape(client.participant) + ' participant.</p>'
        '<p>OAuth client ID:</p><code>' + html.escape(client.client_id) + '</code>'
        '<p>Registered callbacks:</p><ul>'
        + ''.join('<li><code>' + html.escape(uri) + '</code></li>' for uri in client.redirect_uris) + '</ul>'
    )
    if client.client_secret is None:
        return section + '<p>Leave the OAuth client secret empty. This client is public.</p></section>'
    field = 'client-secret-' + str(position)
    return section + (
        '<div data-secret="' + html.escape(client.name, quote=True) + '">'
        '<p><label for="' + field + '">OAuth client secret (copy into connector settings only)</label></p>'
        '<input id="' + field + '" type="password" autocomplete="off" readonly value="'
        + html.escape(client.client_secret, quote=True) + '">'
        '<p><button type="button" data-copy>Copy secret</button> '
        '<button type="button" data-show aria-controls="' + field + '" aria-pressed="false">Show secret</button></p>'
        '<p role="status" aria-live="polite"></p></div></section>'
    )


def install_private_controls(app: FastAPI, provider: OAuthProvider) -> None:
    """Mount controls without creating sessions, credentials, or a lifecycle.

    The composition root must set ``app.state.runtime.policy`` before serving.
    Bearer authentication intentionally cannot substitute for a browser login.
    The provider owns issuer consent, the one-time handoff, exact callback
    validation and session-bound CSRF.
    """

    def signed_in_operator(state, session_id: str | None) -> str | None:
        """The session ID while it is a live operator session; a dead one is dropped."""
        session = state.sessions.get(session_id) if session_id else None
        if session is None:
            return None
        actor, expires_at = session
        if expires_at <= datetime.now(UTC):
            state.sessions.pop(session_id, None)
            return None
        try:
            # Read current policy each time, including after a policy replacement.
            state.runtime.policy.require_operator(actor)
        except GremlinTownError as error:
            if error.code != ErrorCode.ACCESS_DENIED:
                raise
            state.sessions.pop(session_id, None)
            return None
        return session_id

    async def require_operator(request: Request) -> str | None:
        state = request.app.state
        return signed_in_operator(state, request.cookies.get(state.session_cookie_name))

    def signed_in_sessions() -> tuple[str, ...]:
        # Approval pages recheck these, so /logout (or expiry, a role change, a
        # restart) ends every page opened under a session at once. Copy the keys
        # first: checking drops dead sessions.
        return tuple(session_id for session_id in tuple(app.state.sessions)
                     if signed_in_operator(app.state, session_id))

    async def setup(request: Request):
        if not await require_operator(request):
            return RedirectResponse('/login', status_code=303, headers=HEADERS)

        config = provider.config
        nonce = secrets.token_urlsafe(24)
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Connect an app to Gremlin Town</title></head><body><main>'
            '<h1>Connect an app to Gremlin Town</h1>'
            '<p>Each app below connects as its own participant, with the permissions configured for it. '
            'Read access can inspect permitted requests; write access can create requests '
            'and report, reply, or cancel within those permissions.</p>'
            '<p>MCP URL:</p><code>' + html.escape(config.resource_url) + '</code>'
            + ''.join(_client_section(position, client) for position, client in enumerate(config.clients))
            + '<p>Return to the app to connect. Keep your private network connection active '
            'and this operator session signed in. The app returns here once to confirm '
            'the session, then the approval form is completed on the public connector.</p>'
            '<p><a href="/mcp/connections">View or revoke connections</a></p>'
            '<p><a href="/overview">Return to overview</a></p>'
            '<script nonce="' + nonce + '">'
            """
for (const box of document.querySelectorAll('[data-secret]')) {
  const field = box.querySelector('input');
  const show = box.querySelector('[data-show]');
  const status = box.querySelector('[role="status"]');
  function reveal(visible) {
    field.type = visible ? 'text' : 'password';
    show.textContent = visible ? 'Hide secret' : 'Show secret';
    show.setAttribute('aria-pressed', String(visible));
  }
  show.addEventListener('click', () => reveal(field.type === 'password'));
  box.querySelector('[data-copy]').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      status.textContent = 'Copied. Paste into the OAuth Client Secret field in ' + box.dataset.secret + '.';
    } catch {
      reveal(true);
      field.focus();
      field.select();
      status.textContent = 'Automatic copy was blocked. Press Ctrl+C (or Cmd+C) to copy the selected secret.';
    }
  });
}
"""
            '</script></main></body></html>'
        )
        headers = {
            **HEADERS,
            'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': HEADERS['Content-Security-Policy']
            + "; script-src 'nonce-" + nonce + "'",
        }
        return HTMLResponse(body, headers=headers)

    # /mcp/consent-handoff and /mcp/connections. Issuer /mcp/consent is public.
    app.router.routes.extend([
        Route('/mcp/setup', setup, methods=['GET']),
        *provider.private_routes(require_operator, signed_in_sessions),
    ])
