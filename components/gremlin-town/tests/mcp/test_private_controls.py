"""Private controls use real hub login sessions and the real OAuth provider."""

import asyncio
import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import html
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gremlin_town.api.app import create_app
from gremlin_town.api.auth import new_session
from gremlin_town.api.public import create_public_app
from gremlin_town.contracts import Actor
from gremlin_town.domain.policy import Participant, Policy
from gremlin_town.mcp.private_controls import install_private_controls
from gremlin_town.oauth import OAuthClient, OAuthConfig, OAuthProvider


CHATGPT = OAuthClient(client_id='test-chatgpt', participant='chatgpt-web', name='ChatGPT web',
    redirect_uris=('https://chatgpt.com/connector/callback',), client_secret='synthetic-test-secret-' * 3)
CLAUDE = OAuthClient(client_id='test-claude', participant='claude-web', name='Claude',
    redirect_uris=('https://claude.ai/api/mcp/auth_callback', 'https://claude.com/api/mcp/auth_callback'))


@pytest.fixture
def controls(tmp_path):
    operator = Participant('operator', 'Operator', 'operator@example.test',
        frozenset({'test'}), frozenset({'test'}), is_operator=True)
    actor = Actor('operator', is_operator=True)
    policy = Policy([operator])
    app = create_app(object(), actor_resolver={'test-bearer': actor},
        login_resolver={'operator': ('test-password', actor)})
    app.state.runtime = SimpleNamespace(policy=policy)
    provider = OAuthProvider(OAuthConfig(
        db_path=tmp_path / 'oauth.sqlite3', issuer_url='https://mcp.example',
        resource_url='https://mcp.example/mcp', private_base_url='https://private.example',
        clients=(CHATGPT, CLAUDE)), lambda _: True)
    original_sessions = app.state.sessions
    original_lifespan = app.router.lifespan_context
    install_private_controls(app, provider)
    assert app.state.sessions is original_sessions
    assert app.router.lifespan_context is original_lifespan
    with TestClient(app, base_url=provider.config.private_base_url, follow_redirects=False) as private:
        with TestClient(Starlette(routes=provider.public_routes()), base_url=provider.config.issuer_url,
                        follow_redirects=False) as public:
            yield SimpleNamespace(app=app, provider=provider, private=private, public=public,
                                  operator=operator, actor=actor)


def login(controls):
    response = controls.private.post('/login', data={'username': 'operator', 'password': 'test-password'})
    assert response.status_code == 303
    session_id = controls.private.cookies.get(controls.app.state.session_cookie_name)
    assert session_id in controls.app.state.sessions
    return session_id


def pending(controls, client=CHATGPT):
    config = controls.provider.config
    verifier = 'v' * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    response = controls.public.get('/authorize', params={
        'client_id': client.client_id, 'redirect_uri': client.redirect_uris[0],
        'response_type': 'code', 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'state': 'test-state', 'scope': 'gt:read gt:write', 'resource': config.resource_url})
    assert response.status_code == 302
    return response.headers['location'], verifier


def field(page, name):
    return html.unescape(re.search(r'name="' + name + r'" value="([^"]+)"', page.text).group(1))


def consent_form(controls, client=CHATGPT):
    location, verifier = pending(controls, client)
    bounced = controls.public.get(location, follow_redirects=False)
    assert bounced.status_code == 302
    assert urlsplit(bounced.headers['location']).path == '/mcp/consent-handoff'
    minted = controls.private.get(bounced.headers['location'], follow_redirects=False)
    assert minted.status_code == 302
    page = controls.public.get(minted.headers['location'], follow_redirects=False)
    assert page.status_code == 200
    return page, {'transaction': field(page, 'transaction'), 'csrf': field(page, 'csrf'),
                  'decision': 'approve'}, verifier


def approve(controls, client=CHATGPT):
    _, form, verifier = consent_form(controls, client)
    response = controls.public.post('/mcp/consent', data=form,
        headers={'Origin': controls.provider.config.issuer_url})
    assert response.status_code == 303
    config = controls.provider.config
    values = parse_qs(urlsplit(response.headers['location']).query)
    assert values['state'] == ['test-state']
    assert values['iss'] == [config.issuer_url]
    secret = {'client_secret': client.client_secret} if client.client_secret else {}
    token = controls.public.post('/token', data={
        'client_id': client.client_id, **secret,
        'grant_type': 'authorization_code', 'code': values['code'][0], 'code_verifier': verifier,
        'redirect_uri': client.redirect_uris[0], 'resource': config.resource_url})
    assert token.status_code == 200
    return token.json()


def test_setup_reuses_login_and_hides_secret_with_nonce_csp(controls):
    login(controls)
    page = controls.private.get('/mcp/setup')
    assert page.status_code == 200
    assert controls.provider.config.resource_url in page.text
    assert controls.provider.config.clients[0].client_id in page.text
    assert 'type="password" autocomplete="off" readonly' in page.text
    assert html.escape(controls.provider.config.clients[0].client_secret, quote=True) in page.text
    nonce = re.search(r'<script nonce="([^"]+)"', page.text).group(1)
    csp = page.headers['content-security-policy']
    assert "script-src 'nonce-" + nonce + "'" in csp
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert 'unsafe-inline' not in csp
    assert page.headers['cache-control'] == 'no-store'
    assert page.headers['referrer-policy'] == 'no-referrer'
    assert page.headers['x-content-type-options'] == 'nosniff'
    assert nonce not in controls.private.get('/mcp/setup').text
    assert '/mcp/connections' in page.text


def test_setup_escapes_configured_credentials(controls):
    login(controls)
    hostile = '\"><script>alert(1)</script>&' + 'synthetic' * 5
    controls.provider.config = replace(controls.provider.config, clients=(
        replace(CHATGPT, client_id=hostile, client_secret=hostile, name=hostile),
        replace(CLAUDE, participant=hostile, redirect_uris=('https://claude.ai/' + hostile,)),
    ))
    page = controls.private.get('/mcp/setup')
    assert page.status_code == 200
    assert hostile not in page.text
    assert html.escape(hostile, quote=True) in page.text
    assert page.text.count('<script') == 1


def test_setup_lists_each_app_and_only_shows_configured_secrets(controls):
    login(controls)
    page = controls.private.get('/mcp/setup')
    for client in (CHATGPT, CLAUDE):
        assert '<h2>' + client.name + '</h2>' in page.text
        assert client.participant + ' participant' in page.text
        assert client.client_id in page.text
        assert all(uri in page.text for uri in client.redirect_uris)
    assert page.text.count('type="password"') == 1
    start = page.text.index('<h2>Claude</h2>')
    claude = page.text[start:page.text.index('</section>', start)]
    assert 'Leave the OAuth client secret empty' in claude and 'data-secret' not in claude


def test_claude_consent_uses_the_same_operator_session_and_its_own_identity(controls):
    login(controls)
    page, _, _ = consent_form(controls, CLAUDE)
    assert 'Connect Claude to Gremlin Town' in page.text and 'Participant: claude-web' in page.text
    tokens = approve(controls, CLAUDE)
    assert asyncio.run(controls.provider.verify_token(tokens['access_token'])).subject == 'claude-web'
    assert 'Claude (claude-web) — active' in controls.private.get('/mcp/connections').text


@pytest.mark.parametrize('path', ['/mcp/setup', '/mcp/consent-handoff', '/mcp/connections'])
@pytest.mark.parametrize('credential', ['none', 'bearer', 'forged-cookie', 'nonoperator'])
def test_private_controls_require_operator_browser_session(controls, path, credential):
    headers = {'Authorization': 'Bearer test-bearer'} if credential == 'bearer' else {}
    if credential == 'forged-cookie':
        controls.private.cookies.set(controls.app.state.session_cookie_name, 'operator')
    if credential == 'nonoperator':
        session_id, expiry = new_session(Actor('operator'), ttl_seconds=600)
        controls.app.state.sessions[session_id] = (Actor('operator'), expiry)
        controls.private.cookies.set(controls.app.state.session_cookie_name, session_id)
    response = controls.private.get(path, headers=headers)
    assert response.status_code == (303 if path == '/mcp/setup' else 403)
    assert controls.provider.config.clients[0].client_secret not in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('change', ['expired', 'disabled', 'demoted', 'removed', 'logout'])
def test_live_session_and_role_rechecked_before_handoff(controls, change):
    session_id = login(controls)
    location, _ = pending(controls)
    bounced = controls.public.get(location, follow_redirects=False)
    assert urlsplit(bounced.headers['location']).path == '/mcp/consent-handoff'
    if change == 'expired':
        controls.app.state.sessions[session_id] = (controls.actor, datetime.now(UTC) - timedelta(seconds=1))
    elif change == 'disabled':
        controls.app.state.runtime.policy.set_enabled('operator', False)
    elif change == 'demoted':
        controls.app.state.runtime.policy = Policy([replace(controls.operator, is_operator=False)])
    elif change == 'removed':
        controls.app.state.runtime.policy = Policy([])
    else:
        assert controls.private.post('/logout').status_code == 303
    denied = controls.private.get(bounced.headers['location'],
        headers={'Authorization': 'Bearer test-bearer'}, follow_redirects=False)
    assert denied.status_code == 403
    assert session_id not in controls.app.state.sessions
    assert controls.public.cookies.get('gremlin_town_consent') is None
    assert controls.private.get('/mcp/setup').status_code == 303
    assert controls.private.get('/mcp/connections').status_code == 403


@pytest.mark.parametrize('change', ['logout', 'expired', 'disabled', 'demoted', 'restart'])
def test_ending_the_operator_session_ends_an_open_approval_page(controls, change):
    """L3: the approval page stops working the moment the sign-in that opened it ends."""
    session_id = login(controls)
    location, _ = pending(controls)
    minted = controls.private.get(controls.public.get(location).headers['location'])
    page = controls.public.get(minted.headers['location'])
    assert page.status_code == 200
    form = {'transaction': field(page, 'transaction'), 'csrf': field(page, 'csrf'), 'decision': 'approve'}
    if change == 'logout':
        assert controls.private.post('/logout').status_code == 303
    elif change == 'expired':
        controls.app.state.sessions[session_id] = (controls.actor, datetime.now(UTC) - timedelta(seconds=1))
    elif change == 'disabled':
        controls.app.state.runtime.policy.set_enabled('operator', False)
    elif change == 'demoted':
        controls.app.state.runtime.policy = Policy([replace(controls.operator, is_operator=False)])
    else:
        controls.app.state.sessions.clear()  # A service restart forgets every browser sign-in.
    origin = {'Origin': controls.provider.config.issuer_url}
    refused = controls.public.post('/mcp/consent', data=form, headers=origin)
    assert refused.status_code == 403 and 'Operator sign-in ended' in refused.text
    assert session_id not in controls.app.state.sessions
    # Signing in again finishes the same sign-in from the app.
    controls.app.state.runtime.policy = Policy([controls.operator])
    login(controls)
    reopened = controls.public.get(location)
    assert urlsplit(reopened.headers['location']).path == '/mcp/consent-handoff'
    page = controls.public.get(controls.private.get(reopened.headers['location']).headers['location'])
    approved = controls.public.post('/mcp/consent', data={**form, 'csrf': field(page, 'csrf')}, headers=origin)
    assert approved.status_code == 303


def test_signing_out_one_session_leaves_another_sessions_page_working(controls):
    first = login(controls)
    _, form, _ = consent_form(controls)
    second = login(controls)  # The same operator signs in again, then signs that one out.
    assert first != second and first in controls.app.state.sessions
    assert controls.private.post('/logout').status_code == 303
    assert second not in controls.app.state.sessions and first in controls.app.state.sessions
    approved = controls.public.post('/mcp/consent', data=form,
        headers={'Origin': controls.provider.config.issuer_url})
    assert approved.status_code == 303


def test_consent_and_revocation_use_existing_session(controls):
    login(controls)
    tokens = approve(controls)
    assert asyncio.run(controls.provider.verify_token(tokens['access_token'])) is not None
    page = controls.private.get('/mcp/connections')
    form = {'csrf': field(page, 'csrf'), 'grant_id': field(page, 'grant_id')}
    response = controls.private.post('/mcp/connections', data=form,
        headers={'Origin': controls.provider.config.private_base_url})
    assert response.status_code == 200
    assert 'Access revoked' in response.text
    assert asyncio.run(controls.provider.verify_token(tokens['access_token'])) is None
    assert controls.private.post('/mcp/connections', data=form,
        headers={'Origin': controls.provider.config.private_base_url}).status_code == 403


@pytest.mark.parametrize('change', ['expired', 'disabled', 'demoted'])
def test_revocation_rechecks_operator_after_form_was_loaded(controls, change):
    session_id = login(controls)
    tokens = approve(controls)
    page = controls.private.get('/mcp/connections')
    form = {'csrf': field(page, 'csrf'), 'grant_id': field(page, 'grant_id')}
    if change == 'expired':
        controls.app.state.sessions[session_id] = (controls.actor, datetime.now(UTC) - timedelta(seconds=1))
    elif change == 'disabled':
        controls.app.state.runtime.policy.set_enabled('operator', False)
    else:
        controls.app.state.runtime.policy = Policy([replace(controls.operator, is_operator=False)])
    response = controls.private.post('/mcp/connections', data=form,
        headers={'Origin': controls.provider.config.private_base_url})
    assert response.status_code == 403
    assert asyncio.run(controls.provider.verify_token(tokens['access_token'])) is not None


@pytest.mark.parametrize('origin', [None, 'null', 'https://evil.example', 'https://private.example.evil'])
def test_consent_and_revocation_reject_wrong_origin(controls, origin):
    login(controls)
    approve(controls)
    _, consent, _ = consent_form(controls)
    connections = controls.private.get('/mcp/connections')
    revoke = {'csrf': field(connections, 'csrf'), 'grant_id': field(connections, 'grant_id')}
    headers = {'Origin': origin} if origin else {}
    assert controls.public.post('/mcp/consent', data=consent, headers=headers).status_code == 403
    assert controls.private.post('/mcp/connections', data=revoke, headers=headers).status_code == 403


def test_csrf_is_bound_to_session_action_and_target(controls):
    login(controls)
    approve(controls)
    _, consent, _ = consent_form(controls)
    connections = controls.private.get('/mcp/connections')
    revoke = {'csrf': field(connections, 'csrf'), 'grant_id': field(connections, 'grant_id')}
    public_headers = {'Origin': controls.provider.config.issuer_url}
    private_headers = {'Origin': controls.provider.config.private_base_url}
    assert controls.public.post('/mcp/consent', data={**consent, 'csrf': revoke['csrf']}, headers=public_headers).status_code == 403
    assert controls.private.post('/mcp/connections', data={**revoke, 'grant_id': 'other'}, headers=private_headers).status_code == 403
    login(controls)  # Same operator, different private session cannot reuse revocation CSRF.
    assert controls.private.post('/mcp/connections', data=revoke, headers=private_headers).status_code == 403


def test_exact_callback_csp_and_deny(controls):
    login(controls)
    page, form, _ = consent_form(controls)
    callback = controls.provider.config.clients[0].redirect_uris[0]
    assert "form-action 'self' " + callback + ';' in page.headers['content-security-policy']
    assert page.headers['referrer-policy'] == 'same-origin'
    response = controls.public.post('/mcp/consent', data={**form, 'decision': 'deny'},
        headers={'Origin': controls.provider.config.issuer_url})
    assert response.status_code == 303
    assert response.headers['location'].startswith(callback + '?')
    assert parse_qs(urlsplit(response.headers['location']).query)['error'] == ['access_denied']
    assert response.headers['content-security-policy'] == page.headers['content-security-policy']


def test_private_routes_absent_from_public_surfaces(controls):
    public_api = create_public_app(object(), lambda _: controls.actor, 'operator')
    with TestClient(public_api) as public:
        for path in ['/mcp/setup', '/mcp/consent', '/mcp/consent-handoff', '/mcp/connections']:
            for method in ['GET', 'POST']:
                assert public.request(method, path).status_code == 404
        for path in ['/mcp/setup', '/mcp/consent-handoff', '/mcp/connections']:
            for method in ['GET', 'POST']:
                assert controls.public.request(method, path).status_code == 404
    assert controls.private.get('/mcp/consent').status_code == 404
    assert controls.public.get('/mcp/consent').status_code == 400


def test_provider_body_limits_remain_installed(controls):
    login(controls)
    for client, path, origin in [
        (controls.public, '/mcp/consent', controls.provider.config.issuer_url),
        (controls.private, '/mcp/connections', controls.provider.config.private_base_url),
    ]:
        response = client.post(path, data={'oversized': 'x' * 17000}, headers={'Origin': origin})
        assert response.status_code == 413
