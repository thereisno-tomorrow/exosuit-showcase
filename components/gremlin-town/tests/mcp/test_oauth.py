import base64
from dataclasses import replace
import hashlib
import json
import logging
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gremlin_town.oauth import OAuthClient, OAuthConfig, OAuthProvider
from gremlin_town.oauth.provider import SIGNED_OUT


CHATGPT = OAuthClient(client_id='chatgpt-test', participant='chatgpt-web', name='ChatGPT web',
    redirect_uris=('https://chatgpt.com/connector/callback',), origins=('https://chatgpt.com',), client_secret='s' * 48)
CLAUDE = OAuthClient(client_id='claude-test', participant='claude-web', name='Claude',
    redirect_uris=('https://claude.ai/api/mcp/auth_callback', 'https://claude.com/api/mcp/auth_callback'),
    origins=('https://claude.ai', 'https://claude.com'))


def _config(tmp_path, *clients, name='oauth.sqlite'):
    return OAuthConfig(db_path=tmp_path / name, issuer_url='https://mcp.example',
        resource_url='https://mcp.example/mcp', private_base_url='https://private.example', clients=clients)


class _Operators:
    """Fake private login: an x-test-session is signed in from first use until signed out."""

    def __init__(self):
        self.seen, self.signed_out = set(), set()

    async def __call__(self, request):
        session = request.headers.get('x-test-session')
        if not session or session in self.signed_out:
            return None
        self.seen.add(session)
        return session

    def signed_in(self):
        return tuple(self.seen - self.signed_out)


def _routes(provider, operators=None):
    operators = operators or _Operators()
    public = TestClient(Starlette(routes=provider.public_routes()), base_url=provider.config.issuer_url)
    private = TestClient(Starlette(routes=provider.private_routes(operators, operators.signed_in)),
        base_url=provider.config.private_base_url)
    return public, private


def _running(config, enabled):
    provider = OAuthProvider(config, participant_enabled=lambda participant: enabled.get(participant, False))
    return provider, *_routes(provider), enabled


@pytest.fixture
def setup(tmp_path):
    enabled = [True]
    provider = OAuthProvider(_config(tmp_path, CHATGPT), participant_enabled=lambda _: enabled[0])
    return provider, *_routes(provider), enabled


@pytest.fixture
def both(tmp_path):
    """ChatGPT and Claude on one issuer, each enabled independently by participant."""
    return _running(_config(tmp_path, CHATGPT, CLAUDE), {'chatgpt-web': True, 'claude-web': True})


def authorize(setup, client=None, **overrides):
    p, public, private, _ = setup
    client = client or p.config.clients[0]
    verifier = 'v' * 43
    params = dict(client_id=client.client_id, redirect_uri=client.redirect_uris[0],
        response_type='code', code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
        code_challenge_method='S256', state='test-state', scope='gt:read gt:write', resource=p.config.resource_url)
    params.update({key: value for key, value in overrides.items() if value is not None})
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
    response = public.get('/authorize', params=params, follow_redirects=False)
    return response, verifier


def _origin(url):
    parsed = urlparse(url)
    return parsed, f'{parsed.scheme}://{parsed.netloc}'


def _csrf(page):
    return re.search('name="csrf" value="([^"]+)"', page.text).group(1)


def redeem_consent(setup, location, session='operator-session', client=None):
    """Issuer consent GET → private handoff → issuer redeem. Returns the consent page."""
    provider, public, private, _ = setup
    client = public if client is None else client
    bounced = client.get(location, follow_redirects=False)
    assert bounced.status_code in (302, 303), bounced.text
    handoff_url = bounced.headers['location']
    parsed, origin = _origin(handoff_url)
    assert origin == provider.config.private_base_url
    assert parsed.path == '/mcp/consent-handoff'
    minted = private.get(handoff_url, headers={'x-test-session': session}, follow_redirects=False)
    assert minted.status_code in (302, 303), minted.text
    redeem_url = minted.headers['location']
    parsed, origin = _origin(redeem_url)
    assert origin == provider.config.issuer_url
    assert parsed.path == '/mcp/consent'
    assert parse_qs(parsed.query).get('handoff')
    page = client.get(redeem_url, follow_redirects=False)
    assert page.status_code == 200, page.text
    return page


def code(setup, client=None, **authorize_overrides):
    provider, public, _, _ = setup
    client = client or provider.config.clients[0]
    response, verifier = authorize(setup, client, **authorize_overrides)
    assert response.status_code == 302
    location = response.headers['location']
    parsed, origin = _origin(location)
    assert origin == provider.config.issuer_url
    assert parsed.path == '/mcp/consent'
    page = redeem_consent(setup, location)
    tx = parse_qs(parsed.query)['transaction'][0]
    approved = public.post('/mcp/consent', data={'transaction': tx, 'csrf': _csrf(page), 'decision': 'approve'},
        headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert approved.status_code == 303
    callback = urlparse(approved.headers['location'])
    values = parse_qs(callback.query)
    assert values['state'] == ['test-state']
    assert values['iss'] == [provider.config.issuer_url]
    registered = urlparse(authorize_overrides.get('redirect_uri') or client.redirect_uris[0])
    assert (callback.scheme, callback.hostname, callback.path) == (registered.scheme, registered.hostname, registered.path)
    return values['code'][0], verifier


def exchange(setup, auth_code, verifier, client=None, **overrides):
    """Public PKCE token exchange. No client_secret and no Authorization header."""
    p, public, _, _ = setup
    client = client or p.config.clients[0]
    data = dict(grant_type='authorization_code', client_id=client.client_id,
        code=auth_code, code_verifier=verifier, redirect_uri=client.redirect_uris[0], resource=p.config.resource_url)
    data.update(overrides)
    return public.post('/token', data=data)


def refresh(setup, token, client=None, **overrides):
    p, public, _, _ = setup
    client = client or p.config.clients[0]
    data = dict(grant_type='refresh_token', client_id=client.client_id,
        refresh_token=token, resource=p.config.resource_url)
    data.update(overrides)
    return public.post('/token', data=data)


def test_oauth_happy_path_hashes_and_restart(setup):
    p, public, _, _ = setup
    auth_code, verifier = code(setup)
    response = exchange(setup, auth_code, verifier)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert tokens['scope'] == 'gt:read gt:write'
    import asyncio
    access = asyncio.run(p.verify_token(tokens['access_token']))
    assert access.subject == 'chatgpt-web'
    assert access.resource == p.config.resource_url
    assert access.claims['grant_id']
    restarted = OAuthProvider(p.config, participant_enabled=lambda _: True)
    assert asyncio.run(restarted.verify_token(tokens['access_token'])) is not None
    with sqlite3.connect(p.config.db_path) as db:
        dump = '\n'.join(db.iterdump())
    consent_cookie = public.cookies.get('gremlin_town_consent')
    for secret in (auth_code, tokens['access_token'], tokens['refresh_token'], p.config.clients[0].client_secret, consent_cookie):
        assert secret and secret not in dump
    assert public.get('/register').status_code == 404
    metadata = public.get('/.well-known/oauth-authorization-server').json()
    assert 'registration_endpoint' not in metadata
    assert metadata['issuer'] == p.config.issuer_url
    assert metadata['authorization_response_iss_parameter_supported'] is True
    assert metadata['token_endpoint_auth_methods_supported'] == ['none']
    assert 'client_secret_post' not in metadata['token_endpoint_auth_methods_supported']
    assert metadata['revocation_endpoint_auth_methods_supported'] == ['none']
    client = asyncio.run(p.get_client(p.config.clients[0].client_id))
    assert client.token_endpoint_auth_method == 'none'
    assert client.client_secret is None
    assert client.client_id == p.config.clients[0].client_id
    assert client.client_name == 'ChatGPT web'
    assert client.grant_types == ['authorization_code', 'refresh_token']
    assert client.response_types == ['code']
    assert [str(uri) for uri in client.redirect_uris] == list(p.config.clients[0].redirect_uris)
    assert asyncio.run(p.get_client('pretend-chatgpt')) is None
    resource = public.get('/.well-known/oauth-protected-resource/mcp').json()
    assert resource['authorization_servers'] == [metadata['issuer']]


def test_omitted_scope_defaults_to_gt_only_while_client_registers_bc_scopes(setup):
    p, _, _, _ = setup
    auth_code, verifier = code(setup, scope=None)
    tokens = exchange(setup, auth_code, verifier).json()
    assert tokens['scope'] == 'gt:read gt:write'
    import asyncio
    access = asyncio.run(p.verify_token(tokens['access_token']))
    assert access.scopes == ['gt:read', 'gt:write']
    client = asyncio.run(p.get_client(p.config.clients[0].client_id))
    assert set(client.scope.split()) == {'gt:read', 'gt:write', 'bc:read', 'bc:write'}


@pytest.mark.parametrize('overrides', [{'resource': 'https://evil.example/mcp'}, {'code_verifier': 'w' * 43},
    {'redirect_uri': 'https://chatgpt.com/other'}, {'client_id': 'pretend-chatgpt'}])
def test_bad_exchange_does_not_consume_code(setup, overrides):
    auth_code, verifier = code(setup)
    assert exchange(setup, auth_code, verifier, **overrides).status_code in (400, 401)
    assert exchange(setup, auth_code, verifier).status_code == 200
    assert exchange(setup, auth_code, verifier).status_code == 400


def test_refresh_rotation_reuse_revokes_family(setup):
    import asyncio
    p, _, _, _ = setup
    auth_code, verifier = code(setup)
    first = exchange(setup, auth_code, verifier).json()
    next_response = refresh(setup, first['refresh_token'])
    assert next_response.status_code == 200, next_response.text
    second = next_response.json()
    assert asyncio.run(p.verify_token(first['access_token'])) is None
    assert asyncio.run(p.verify_token(second['access_token'])) is not None
    assert refresh(setup, first['refresh_token']).status_code == 400
    assert asyncio.run(p.verify_token(second['access_token'])) is None
    assert refresh(setup, second['refresh_token']).status_code == 400


def test_private_consent_requires_session_and_bound_csrf(setup):
    provider, public, private, _ = setup
    response, _ = authorize(setup)
    location = response.headers['location']
    bounced = public.get(location, follow_redirects=False)
    assert bounced.status_code in (302, 303)
    assert private.get(bounced.headers['location'], follow_redirects=False).status_code == 403
    tx = parse_qs(urlparse(location).query)['transaction'][0]
    assert public.post('/mcp/consent', data={'transaction': tx, 'csrf': 'missing', 'decision': 'approve'},
        headers={'origin': provider.config.issuer_url}).status_code == 403
    page = redeem_consent(setup, location, session='alice')
    csrf = _csrf(page)
    public_bob = TestClient(public.app, base_url=provider.config.issuer_url)
    # Same starting browser (sign-in cookie), separate consent session.
    public_bob.cookies.set('__Host-gremlin_town_signin', public.cookies.get('__Host-gremlin_town_signin'), domain='mcp.example', path='/')
    bob_page = redeem_consent(setup, location, session='bob', client=public_bob)
    assert _csrf(bob_page) != csrf
    assert public_bob.post('/mcp/consent', data={'transaction': tx, 'csrf': csrf, 'decision': 'approve'},
        headers={'origin': provider.config.issuer_url}).status_code == 403
    assert public.post('/mcp/consent', data={'transaction': tx, 'csrf': csrf, 'decision': 'approve'},
        headers={'origin': 'https://evil.example'}).status_code == 403
    assert public.post('/mcp/consent', data={'transaction': tx, 'csrf': csrf, 'decision': 'approve'},
        headers={'origin': provider.config.private_base_url}).status_code == 403


def test_disable_and_revoke(setup):
    import asyncio
    p, _, private, enabled = setup
    auth_code, verifier = code(setup)
    tokens = exchange(setup, auth_code, verifier).json()
    enabled[0] = False
    assert asyncio.run(p.verify_token(tokens['access_token'])) is None
    enabled[0] = True
    page = private.get('/mcp/connections', headers={'x-test-session': 'alice'})
    csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
    grant_id = re.search('name="grant_id" value="([^"]+)"', page.text).group(1)
    response = private.post('/mcp/connections', data={'grant_id': grant_id, 'csrf': csrf},
        headers={'x-test-session': 'alice', 'origin': 'https://private.example'})
    assert response.status_code == 200
    assert 'Access revoked' in response.text
    assert asyncio.run(p.verify_token(tokens['access_token'])) is None
    assert refresh(setup, tokens['refresh_token']).status_code == 400


@pytest.mark.parametrize('overrides', [
    {'resource': 'https://wrong.example/mcp'}, {'scope': 'admin'},
    {'redirect_uri': 'https://evil.example/callback'}, {'client_id': 'pretend-claude'},
    {'code_challenge_method': 'plain'}, {'code_challenge': 'short'},
])
def test_authorization_rejects_bad_request(setup, overrides):
    response, _ = authorize(setup, **overrides)
    if response.status_code == 302:
        parsed = urlparse(response.headers['location'])
        assert parsed.hostname == 'chatgpt.com'
        assert 'error' in parse_qs(parsed.query)
        assert parse_qs(parsed.query)['iss'] == [setup[0].config.issuer_url]
    else:
        assert response.status_code == 400


def test_deny_and_transaction_one_use(setup):
    provider, public, _, _ = setup
    response, _ = authorize(setup)
    location = response.headers['location']
    page = redeem_consent(setup, location, session='alice')
    data = {'csrf': _csrf(page), 'transaction': parse_qs(urlparse(location).query)['transaction'][0], 'decision': 'deny'}
    denied = public.post('/mcp/consent', data=data, headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert denied.status_code == 303
    assert parse_qs(urlparse(denied.headers['location']).query)['error'] == ['access_denied']
    assert urlparse(denied.headers['location']).hostname == 'chatgpt.com'
    assert public.get(location, follow_redirects=False).status_code == 400
    assert public.post('/mcp/consent', data=data, headers={'origin': provider.config.issuer_url}).status_code == 403


def test_expiration_and_restore_new_store(setup, tmp_path):
    import asyncio
    from dataclasses import replace
    p, public, _, _ = setup
    response, _ = authorize(setup)
    with sqlite3.connect(p.config.db_path) as db:
        db.execute('UPDATE pending SET expires=0')
    assert public.get(response.headers['location'], follow_redirects=False).status_code == 400
    auth_code, verifier = code(setup)
    with sqlite3.connect(p.config.db_path) as db:
        db.execute('UPDATE codes SET expires=0')
    assert exchange(setup, auth_code, verifier).status_code == 400
    auth_code, verifier = code(setup)
    tokens = exchange(setup, auth_code, verifier).json()
    fresh = OAuthProvider(replace(p.config, db_path=tmp_path / 'restored-oauth.sqlite'), participant_enabled=lambda _: True)
    assert asyncio.run(fresh.verify_token(tokens['access_token'])) is None
    with sqlite3.connect(p.config.db_path) as db:
        db.execute("UPDATE tokens SET expires=0 WHERE kind='access'")
    assert asyncio.run(p.verify_token(tokens['access_token'])) is None
    with sqlite3.connect(p.config.db_path) as db:
        db.execute("UPDATE tokens SET expires=0 WHERE kind='refresh'")
    assert refresh(setup, tokens['refresh_token']).status_code == 400


def test_concurrent_refresh_reuse_commits_revocation(setup):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from mcp.server.auth.provider import TokenError
    p, _, _, _ = setup
    auth_code, verifier = code(setup)
    tokens = exchange(setup, auth_code, verifier).json()
    client = asyncio.run(p.get_client(p.config.clients[0].client_id))
    loaded = asyncio.run(p.load_refresh_token(client, tokens['refresh_token']))
    def rotate():
        try:
            return asyncio.run(p.exchange_refresh_token(client, loaded, loaded.scopes))
        except TokenError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: rotate(), range(2)))
    successes = [outcome for outcome in outcomes if not isinstance(outcome, TokenError)]
    assert len(successes) == 1
    assert asyncio.run(p.verify_token(successes[0].access_token)) is None


def test_concurrent_code_exchange_only_one_succeeds(setup):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from mcp.server.auth.provider import TokenError
    p, _, _, _ = setup
    auth_code, _ = code(setup)
    client = asyncio.run(p.get_client(p.config.clients[0].client_id))
    loaded = asyncio.run(p.load_authorization_code(client, auth_code))
    def redeem():
        try:
            return asyncio.run(p.exchange_authorization_code(client, loaded))
        except TokenError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: redeem(), range(2)))
    assert len([value for value in outcomes if not isinstance(value, TokenError)]) == 1


def test_scope_cannot_escalate_and_resource_cannot_change_on_refresh(setup):
    auth_code, verifier = code(setup)
    tokens = exchange(setup, auth_code, verifier).json()
    assert refresh(setup, tokens['refresh_token'], scope='gt:read admin').status_code == 400
    assert refresh(setup, tokens['refresh_token'], scope='gt:read gt:write bc:read').status_code == 400
    assert refresh(setup, tokens['refresh_token'], resource='https://wrong.example').status_code == 400
    narrowed = refresh(setup, tokens['refresh_token'], scope='gt:read')
    assert narrowed.status_code == 200
    assert narrowed.json()['scope'] == 'gt:read'
    assert refresh(setup, narrowed.json()['refresh_token'], scope='gt:read gt:write').status_code == 400


def test_store_bound_to_issuer_and_each_client(setup):
    p, _, _, _ = setup
    for changed in (
        replace(p.config, clients=(replace(CHATGPT, redirect_uris=('https://chatgpt.com/other',)),)),
        replace(p.config, clients=(replace(CHATGPT, participant='claude-web'),)),
        replace(p.config, issuer_url='https://other.example', resource_url='https://other.example/mcp'),
    ):
        with pytest.raises(ValueError, match='binding changed'):
            OAuthProvider(changed, participant_enabled=lambda _: True)


def test_large_token_request_rejected(setup):
    _, public, _, _ = setup
    assert public.post('/token', data={'oversized': 'x' * 17000}).status_code == 413
    assert public.post('/authorize', data={'oversized': 'x' * 17000}).status_code == 413
    assert public.post('/revoke', data={'oversized': 'x' * 17000}).status_code == 413
    assert public.get('/authorize', params={'oversized': 'x' * 17000}).status_code == 414


def test_protocol_and_private_storage_run_outside_event_loop(setup, monkeypatch):
    import asyncio

    provider, public, private, _ = setup
    original_db = provider._db
    calls = []

    def checked_db():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(True)
        else:
            pytest.fail('OAuth SQLite transaction ran on the request event loop')
        return original_db()

    monkeypatch.setattr(provider, '_db', checked_db)
    auth_code, verifier = code(setup)
    tokens = exchange(setup, auth_code, verifier).json()
    assert asyncio.run(provider.verify_token(tokens['access_token']))
    rotated = refresh(setup, tokens['refresh_token'])
    assert rotated.status_code == 200
    assert private.get('/mcp/connections', headers={'x-test-session': 'operator-session'}).status_code == 200
    response = public.post('/revoke', data={
        'client_id': provider.config.clients[0].client_id, 'client_secret': provider.config.clients[0].client_secret,
        'token': rotated.json()['access_token'], 'token_type_hint': 'access_token',
    })
    assert response.status_code == 200
    assert asyncio.run(provider.verify_token(rotated.json()['access_token'])) is None
    assert len(calls) >= 10


def test_authorize_consent_location_is_issuer(setup):
    provider, public, private, _ = setup
    response, _ = authorize(setup)
    parsed, origin = _origin(response.headers['location'])
    assert response.status_code == 302
    assert origin == provider.config.issuer_url
    assert parsed.path == '/mcp/consent'
    assert 'transaction' in parse_qs(parsed.query)
    assert origin != provider.config.private_base_url
    assert public.get('/mcp/connections', follow_redirects=False).status_code == 404
    assert private.get('/mcp/consent', follow_redirects=False).status_code == 404
    assert private.get('/mcp/consent-handoff', follow_redirects=False).status_code == 403


def test_consent_origin_accepts_issuer_and_rejects_evil(setup):
    provider, public, _, _ = setup
    response, _ = authorize(setup)
    page = redeem_consent(setup, response.headers['location'])
    tx = parse_qs(urlparse(response.headers['location']).query)['transaction'][0]
    csrf = _csrf(page)
    body = {'transaction': tx, 'csrf': csrf, 'decision': 'approve'}
    assert public.post('/mcp/consent', data=body, headers={'origin': 'https://evil.example'}).status_code == 403
    approved = public.post('/mcp/consent', data=body, headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert approved.status_code == 303
    assert parse_qs(urlparse(approved.headers['location']).query)['code']


def test_handoff_redeem_approve_then_code_exchange(setup):
    provider, public, private, _ = setup
    response, verifier = authorize(setup)
    location = response.headers['location']
    parsed, origin = _origin(location)
    assert origin == provider.config.issuer_url
    assert parsed.path == '/mcp/consent'
    bounced = public.get(location, follow_redirects=False)
    assert bounced.status_code in (302, 303)
    assert urlparse(bounced.headers['location']).path == '/mcp/consent-handoff'
    minted = private.get(bounced.headers['location'], headers={'x-test-session': 'operator-session'}, follow_redirects=False)
    assert minted.status_code in (302, 303)
    page = public.get(minted.headers['location'], follow_redirects=False)
    assert page.status_code == 200
    assert 'Approve access' in page.text
    cookie = page.headers['set-cookie']
    assert 'gremlin_town_consent=' in cookie
    folded = cookie.lower()
    assert 'httponly' in folded and 'secure' in folded and 'samesite=lax' in folded
    tx = parse_qs(parsed.query)['transaction'][0]
    approved = public.post('/mcp/consent', data={'transaction': tx, 'csrf': _csrf(page), 'decision': 'approve'},
        headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert approved.status_code == 303
    callback = urlparse(approved.headers['location'])
    registered = urlparse(provider.config.clients[0].redirect_uris[0])
    assert (callback.scheme, callback.hostname, callback.path) == (registered.scheme, registered.hostname, registered.path)
    auth_code = parse_qs(callback.query)['code'][0]
    with sqlite3.connect(provider.config.db_path) as db:
        assert db.execute('SELECT used FROM codes').fetchone()[0] == 0
    exchanged = exchange(setup, auth_code, verifier)
    assert exchanged.status_code == 200, exchanged.text
    with sqlite3.connect(provider.config.db_path) as db:
        assert db.execute('SELECT used FROM codes').fetchone()[0] == 1
        assert public.cookies.get('gremlin_town_consent') not in '\n'.join(db.iterdump())


def test_handoff_single_use_expired_and_wrong_transaction(setup):
    provider, public, private, _ = setup
    first, _ = authorize(setup)
    second, _ = authorize(setup)
    first_tx = parse_qs(urlparse(first.headers['location']).query)['transaction'][0]
    second_tx = parse_qs(urlparse(second.headers['location']).query)['transaction'][0]
    bounced = public.get(first.headers['location'], follow_redirects=False)
    minted = private.get(bounced.headers['location'], headers={'x-test-session': 'alice'}, follow_redirects=False)
    assert minted.status_code in (302, 303)
    handoff = parse_qs(urlparse(minted.headers['location']).query)['handoff'][0]
    with sqlite3.connect(provider.config.db_path) as db:
        stored = db.execute('SELECT operator_session, transaction_hash, expires FROM handoffs').fetchone()
    assert stored[0] == hashlib.sha256(b'alice').hexdigest()
    assert stored[1] == hashlib.sha256(first_tx.encode()).hexdigest()
    assert 90 <= stored[2] - time.time() <= 120
    assert handoff not in open(provider.config.db_path, 'rb').read().decode('latin1', 'replace')
    wrong = provider.config.issuer_url + '/mcp/consent?' + urlencode({'transaction': second_tx, 'handoff': handoff})
    rejected = public.get(wrong, follow_redirects=False)
    assert rejected.status_code == 400
    assert 'handoff' in rejected.text
    with sqlite3.connect(provider.config.db_path) as db:
        db.execute('UPDATE handoffs SET expires=0')
    good = provider.config.issuer_url + '/mcp/consent?' + urlencode({'transaction': first_tx, 'handoff': handoff})
    expired = public.get(good, follow_redirects=False)
    assert expired.status_code == 400
    assert 'handoff' in expired.text
    minted = private.get(bounced.headers['location'], headers={'x-test-session': 'alice'}, follow_redirects=False)
    redeem_url = minted.headers['location']
    token = parse_qs(urlparse(redeem_url).query)['handoff'][0]
    page = public.get(redeem_url, follow_redirects=False)
    assert page.status_code == 200
    with sqlite3.connect(provider.config.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM handoffs WHERE hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0] == 0
    public.cookies.clear()
    replay = public.get(redeem_url, follow_redirects=False)
    assert replay.status_code == 400
    assert 'set-cookie' not in {name.lower() for name in replay.headers}


def test_exact_chatgpt_callback_is_unchanged(tmp_path):
    uri = 'https://chatgpt.com/connector_platform_oauth_redirect'
    config = _config(tmp_path, replace(CHATGPT, redirect_uris=(uri,)))
    provider = OAuthProvider(config, participant_enabled=lambda _: True)
    public, private = _routes(provider)
    local = (provider, public, private, [True])
    response, _ = authorize(local)
    page = redeem_consent(local, response.headers['location'])
    tx = parse_qs(urlparse(response.headers['location']).query)['transaction'][0]
    approved = public.post('/mcp/consent', data={'transaction': tx, 'csrf': _csrf(page), 'decision': 'approve'},
        headers={'origin': config.issuer_url}, follow_redirects=False)
    assert approved.status_code == 303
    location = approved.headers['location']
    assert location.startswith(uri + '?')
    query = parse_qs(urlparse(location).query)
    assert query['code']
    assert query['iss'] == [config.issuer_url]


_TOKEN_LOGGER = 'gremlin_town.oauth.token'
_RESOURCE_REQUIRED = {'error': 'invalid_request', 'error_description': 'Exact single MCP resource is required'}






def test_token_missing_resource_still_requires_exact_mcp_resource(setup, caplog):
    caplog.set_level(logging.WARNING, logger=_TOKEN_LOGGER)
    provider, public, _, _ = setup
    secret_code = 'CODE-do-not-log-9f3a'
    secret_verifier = 'VERIFIER-do-not-log-9f3a'
    secret_client = 'CLIENTSECRET-do-not-log-9f3a'
    response = public.post('/token', data={
        'grant_type': 'authorization_code', 'code': secret_code, 'code_verifier': secret_verifier,
        'client_id': provider.config.clients[0].client_id, 'client_secret': secret_client,
    })
    assert response.status_code == 400
    assert response.json() == _RESOURCE_REQUIRED

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    wrong = public.post('/token', data={
        'grant_type': 'refresh_token', 'refresh_token': 'REFRESH-do-not-log-9f3a',
        'client_id': provider.config.clients[0].client_id, 'client_secret': secret_client,
        'resource': 'https://evil.example/mcp',
    })
    assert wrong.status_code == 400
    assert wrong.json() == _RESOURCE_REQUIRED

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    chatgpt_missing = public.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': provider.config.clients[0].client_id,
        'code': secret_code, 'code_verifier': secret_verifier,
        'redirect_uri': provider.config.clients[0].redirect_uris[0],
    })
    assert chatgpt_missing.status_code == 400
    assert chatgpt_missing.json() == _RESOURCE_REQUIRED


def test_token_diagnostics_removed_preserving_errors(setup, caplog):
    caplog.set_level(logging.WARNING, logger=_TOKEN_LOGGER)
    provider, public, _, _ = setup
    auth_code, verifier = code(setup)
    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    query_secret = 'QUERY-do-not-log-9f3a'
    header_secret = 'HEADER-do-not-log-9f3a'
    response = public.post(
        '/token?' + urlencode({'leak': query_secret}),
        data={
            'grant_type': 'authorization_code', 'client_id': provider.config.clients[0].client_id,
            'client_secret': provider.config.clients[0].client_secret, 'code': auth_code, 'code_verifier': verifier,
            'redirect_uri': provider.config.clients[0].redirect_uris[0], 'resource': provider.config.resource_url,
        },
        headers={'Authorization': 'Bearer ' + header_secret},
    )
    assert response.status_code == 200, response.text
    tokens = response.json()
    consent = public.cookies.get('gremlin_town_consent')

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    short_verifier = 'SHORT-VERIFIER-do-not-log-9f3a'
    pkce = public.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': provider.config.clients[0].client_id,
        'client_secret': provider.config.clients[0].client_secret, 'code': 'PKCE-CODE-do-not-log-9f3a',
        'code_verifier': short_verifier, 'redirect_uri': provider.config.clients[0].redirect_uris[0],
        'resource': provider.config.resource_url,
    })
    assert pkce.status_code == 400
    assert pkce.json() == {'error': 'invalid_grant', 'error_description': 'Invalid PKCE verifier'}

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    body_secret = 'JSONBODY-do-not-log-9f3a'
    other = public.post('/token', content=json.dumps({
        'grant_type': 'authorization_code', 'client_secret': body_secret, 'code': 'JSON-CODE-do-not-log-9f3a',
    }).encode(), headers={'content-type': 'application/json'})
    assert other.status_code == 415

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    missing_ct_secret = 'CTMISSING-do-not-log-9f3a'
    missing_ct = public.post('/token', content=f'client_secret={missing_ct_secret}&grant_type=authorization_code'.encode())
    assert missing_ct.status_code == 415

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    refreshed = refresh(setup, tokens['refresh_token'])
    assert refreshed.status_code == 200, refreshed.text
    rotated = refreshed.json()


def test_public_pkce_exchange_without_client_secret(setup, caplog):
    """ChatGPT posts client_id, code, code_verifier, grant_type, redirect_uri, resource. No secret."""
    caplog.set_level(logging.WARNING, logger=_TOKEN_LOGGER)
    provider, public, _, _ = setup
    auth_code, verifier = code(setup)
    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    chatgpt_form = {
        'client_id': provider.config.clients[0].client_id,
        'code': auth_code,
        'code_verifier': verifier,
        'grant_type': 'authorization_code',
        'redirect_uri': provider.config.clients[0].redirect_uris[0],
        'resource': provider.config.resource_url,
    }
    assert set(chatgpt_form) == {'client_id', 'code', 'code_verifier', 'grant_type', 'redirect_uri', 'resource'}
    issued = public.post('/token', data=chatgpt_form)
    assert issued.status_code == 200, issued.text
    assert 'authorization' not in {name.lower() for name in issued.request.headers}
    tokens = issued.json()
    assert tokens['access_token'] and tokens['refresh_token']
    with sqlite3.connect(provider.config.db_path) as db:
        used = db.execute('SELECT used FROM codes WHERE hash=?', (hashlib.sha256(auth_code.encode()).hexdigest(),)).fetchone()
    assert used == (1,)
    consent = public.cookies.get('gremlin_town_consent')

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    extra_code, extra_verifier = code(setup)
    unknown_secret = 'UNKNOWN-CLIENT-SECRET-do-not-log-9f3a'
    extra = public.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': provider.config.clients[0].client_id, 'client_secret': unknown_secret,
        'code': extra_code, 'code_verifier': extra_verifier, 'redirect_uri': provider.config.clients[0].redirect_uris[0],
        'resource': provider.config.resource_url,
    })
    assert extra.status_code == 200, extra.text
    extra_tokens = extra.json()

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    rejected_code, rejected_verifier = code(setup)
    basic_password = 'BASIC-CREDENTIAL-do-not-log-9f3a'
    basic_header = 'Basic ' + base64.b64encode(f'pretend-chatgpt:{basic_password}'.encode()).decode()
    wrong = public.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': 'pretend-chatgpt', 'client_secret': unknown_secret,
        'code': rejected_code, 'code_verifier': rejected_verifier, 'redirect_uri': provider.config.clients[0].redirect_uris[0],
        'resource': provider.config.resource_url,
    }, headers={'Authorization': basic_header})
    assert wrong.status_code == 401, wrong.text
    wrong_body = wrong.json()
    assert wrong_body['error'] == 'unauthorized_client'
    assert 'Invalid client_id' in wrong_body['error_description']

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    opaque = 'OPAQUE-AUTH-do-not-log-9f3a'
    other = public.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': 'pretend-chatgpt',
        'code': rejected_code, 'code_verifier': rejected_verifier, 'redirect_uri': provider.config.clients[0].redirect_uris[0],
        'resource': provider.config.resource_url,
    }, headers={'Authorization': opaque})
    assert other.status_code == 401, other.text

    with sqlite3.connect(provider.config.db_path) as db:
        used = db.execute('SELECT used FROM codes WHERE hash=?', (hashlib.sha256(rejected_code.encode()).hexdigest(),)).fetchone()
    assert used == (0,)

    assert not [r for r in caplog.records if r.name == _TOKEN_LOGGER]
    caplog.clear()
    recovered = exchange(setup, rejected_code, rejected_verifier)
    assert recovered.status_code == 200, recovered.text
    recovered_tokens = recovered.json()
    assert recovered_tokens['access_token'] and recovered_tokens['refresh_token']




def test_configured_client_secret_must_be_strong_but_is_optional():
    with pytest.raises(ValueError, match='strong secret'):
        replace(CHATGPT, client_secret='short')
    assert CLAUDE.client_secret is None


@pytest.mark.parametrize('change', [
    dict(redirect_uris=('http://claude.ai/api/mcp/auth_callback',)), dict(redirect_uris=()),
    dict(origins=('https://claude.ai/path',)), dict(origins=('http://claude.ai',)),
    dict(participant=' '), dict(name=''), dict(client_id=''),
])
def test_client_definition_is_exact(change):
    with pytest.raises(ValueError):
        replace(CLAUDE, **change)


def test_config_needs_distinct_clients_and_participants(tmp_path):
    for clients in ((), (CHATGPT, replace(CLAUDE, participant='chatgpt-web')),
                    (CHATGPT, replace(CLAUDE, client_id='chatgpt-test'))):
        with pytest.raises(ValueError):
            _config(tmp_path, *clients)
    assert _config(tmp_path, CHATGPT, CLAUDE).client_origins == (
        'https://chatgpt.com', 'https://claude.ai', 'https://claude.com')


def _token(setup, client):
    auth_code, verifier = code(setup, client)
    response = exchange(setup, auth_code, verifier, client)
    assert response.status_code == 200, response.text
    return response.json()


def test_each_client_connects_as_its_own_participant(both):
    import asyncio
    provider, public, _, _ = both
    registered = asyncio.run(provider.get_client(CLAUDE.client_id))
    assert registered.client_name == 'Claude' and registered.client_secret is None
    assert registered.token_endpoint_auth_method == 'none'
    assert [str(uri) for uri in registered.redirect_uris] == list(CLAUDE.redirect_uris)
    response, _ = authorize(both, CLAUDE)
    page = redeem_consent(both, response.headers['location'])
    assert 'Connect Claude to Gremlin Town' in page.text and 'Participant: claude-web' in page.text
    assert 'ChatGPT' not in page.text
    claude, chatgpt = _token(both, CLAUDE), _token(both, CHATGPT)
    for tokens, client in ((claude, CLAUDE), (chatgpt, CHATGPT)):
        access = asyncio.run(provider.verify_token(tokens['access_token']))
        assert (access.client_id, access.subject) == (client.client_id, client.participant)
    # Claude's second registered callback is also exact and usable.
    auth_code, verifier = code(both, CLAUDE, redirect_uri=CLAUDE.redirect_uris[1])
    assert exchange(both, auth_code, verifier, CLAUDE, redirect_uri=CLAUDE.redirect_uris[1]).status_code == 200


def test_codes_refresh_and_revocation_never_cross_clients(both):
    import asyncio
    provider, public, _, _ = both
    auth_code, verifier = code(both, CLAUDE)
    assert exchange(both, auth_code, verifier, CHATGPT).status_code == 400
    assert exchange(both, auth_code, verifier, CHATGPT, redirect_uri=CLAUDE.redirect_uris[0]).status_code == 400
    claude = exchange(both, auth_code, verifier, CLAUDE).json()
    assert refresh(both, claude['refresh_token'], CHATGPT).status_code == 400
    # A supplied client_secret is ignored for these public clients.
    assert public.post('/revoke', data={'client_id': CHATGPT.client_id, 'client_secret': 'unchecked',
        'token': claude['access_token'], 'token_type_hint': 'access_token'}).status_code == 200
    assert asyncio.run(provider.verify_token(claude['access_token'])).subject == 'claude-web'
    rotated = refresh(both, claude['refresh_token'], CLAUDE)
    assert rotated.status_code == 200, rotated.text
    assert asyncio.run(provider.verify_token(rotated.json()['access_token'])).subject == 'claude-web'


@pytest.mark.parametrize('hint', ['access_token', 'refresh_token'])
def test_public_client_revokes_without_a_client_secret(both, hint):
    """RFC 7009 revoke as a public client sends it: client_id and token, no secret."""
    import asyncio
    provider, public, _, _ = both
    claude, chatgpt = _token(both, CLAUDE), _token(both, CHATGPT)
    response = public.post('/revoke', data={'client_id': CLAUDE.client_id, 'token': claude[hint],
        'token_type_hint': hint})
    assert response.status_code == 200, response.text
    assert asyncio.run(provider.verify_token(claude['access_token'])) is None
    assert refresh(both, claude['refresh_token'], CLAUDE).status_code == 400
    # The other client's grant is untouched, and a foreign client still cannot revoke it.
    assert public.post('/revoke', data={'client_id': CLAUDE.client_id, 'token': chatgpt['access_token']}).status_code == 200
    assert asyncio.run(provider.verify_token(chatgpt['access_token'])).subject == 'chatgpt-web'
    # A missing client_id is still refused.
    assert public.post('/revoke', data={'token': chatgpt['access_token']}).status_code in (400, 401)
    assert asyncio.run(provider.verify_token(chatgpt['access_token'])) is not None


@pytest.mark.parametrize('client,callback', [
    (CLAUDE, CHATGPT.redirect_uris[0]), (CHATGPT, CLAUDE.redirect_uris[0]),
])
def test_callbacks_are_exact_per_client(both, client, callback):
    response, _ = authorize(both, client, redirect_uri=callback)
    assert response.status_code == 400 or 'error' in parse_qs(urlparse(response.headers['location']).query)


def test_disabling_one_participant_leaves_the_other(both):
    import asyncio
    provider, _, _, enabled = both
    claude, chatgpt = _token(both, CLAUDE), _token(both, CHATGPT)
    enabled['claude-web'] = False
    assert asyncio.run(provider.verify_token(claude['access_token'])) is None
    assert asyncio.run(provider.get_client(CLAUDE.client_id)) is None
    assert refresh(both, claude['refresh_token'], CLAUDE).status_code in (400, 401)
    assert authorize(both, CLAUDE)[0].status_code in (400, 401)
    assert asyncio.run(provider.verify_token(chatgpt['access_token'])).subject == 'chatgpt-web'
    enabled['claude-web'] = True
    assert asyncio.run(provider.verify_token(claude['access_token'])).subject == 'claude-web'


def test_connections_page_names_each_app_and_revokes_only_one(both):
    import asyncio
    provider, _, private, _ = both
    claude, chatgpt = _token(both, CLAUDE), _token(both, CHATGPT)
    page = private.get('/mcp/connections', headers={'x-test-session': 'alice'})
    assert 'Claude (claude-web) — active' in page.text and 'ChatGPT web (chatgpt-web) — active' in page.text
    forms = re.findall(r'<p>([^<]+)</p><form method="post"><input type="hidden" name="grant_id" value="([^"]+)">'
        r'<input type="hidden" name="csrf" value="([^"]+)">', page.text)
    label, grant_id, csrf = next(form for form in forms if form[0].startswith('Claude'))
    revoked = private.post('/mcp/connections', data={'grant_id': grant_id, 'csrf': csrf},
        headers={'x-test-session': 'alice', 'origin': provider.config.private_base_url})
    assert 'Claude (claude-web) must reconnect' in revoked.text
    assert asyncio.run(provider.verify_token(claude['access_token'])) is None
    assert asyncio.run(provider.verify_token(chatgpt['access_token'])).subject == 'chatgpt-web'


_SINGLE_CLIENT_SCHEMA = '''
    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE pending (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE grants (id TEXT PRIMARY KEY, client TEXT NOT NULL, scopes TEXT NOT NULL, created REAL NOT NULL, revoked REAL);
    CREATE TABLE tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL, scopes TEXT NOT NULL,
        expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
'''


def _single_client_binding(redirect_uris=CHATGPT.redirect_uris):
    return json.dumps(['https://mcp.example', 'https://mcp.example/mcp', CHATGPT.client_id, tuple(redirect_uris)])


def _single_client_store(path, redirect_uris=CHATGPT.redirect_uris):
    """The store shape and binding that the single-client release wrote."""
    access, transaction = 'single-client-access-token', 'single-client-transaction'
    with sqlite3.connect(path) as db:
        db.executescript(_SINGLE_CLIENT_SCHEMA)
        db.execute("INSERT INTO metadata VALUES ('binding', ?)", (_single_client_binding(redirect_uris),))
        db.execute("INSERT INTO grants VALUES ('legacy-grant', ?, ?, ?, NULL)", (CHATGPT.client_id, '["gt:read"]', time.time()))
        db.execute("INSERT INTO tokens VALUES (?, 'access', 'legacy-grant', '[\"gt:read\"]', ?, 0)",
            (hashlib.sha256(access.encode()).hexdigest(), time.time() + 600))
        db.execute('INSERT INTO pending VALUES (?, ?, ?)', (hashlib.sha256(transaction.encode()).hexdigest(), '{}', time.time() + 600))
    return access, transaction


def test_single_client_store_upgrades_in_place_and_keeps_chatgpt_grants(tmp_path):
    import asyncio
    access, transaction = _single_client_store(tmp_path / 'oauth.sqlite')
    provider, public, _, _ = _running(_config(tmp_path, CHATGPT, CLAUDE), {'chatgpt-web': True, 'claude-web': True})
    verified = asyncio.run(provider.verify_token(access))
    assert (verified.client_id, verified.subject, verified.scopes) == (CHATGPT.client_id, 'chatgpt-web', ['gt:read'])
    with sqlite3.connect(tmp_path / 'oauth.sqlite') as db:
        keys = {row[0] for row in db.execute('SELECT key FROM metadata')}
    # grants_aged_from starts the grant age limit (L1); the single-client release never reads it.
    assert keys == {'binding:' + CHATGPT.client_id, 'binding:' + CLAUDE.client_id, 'grants_aged_from'}
    # Pending requests from before the upgrade name no client, so they expire.
    assert public.get('/mcp/consent', params={'transaction': transaction}, follow_redirects=False).status_code == 400
    restarted = OAuthProvider(_config(tmp_path, CHATGPT, CLAUDE), participant_enabled=lambda _: True)
    assert asyncio.run(restarted.verify_token(access)).subject == 'chatgpt-web'
    assert _token(_running(_config(tmp_path, CHATGPT, CLAUDE), {'claude-web': True}), CLAUDE)['access_token']


def test_code_rollback_to_the_single_client_release_keeps_working(tmp_path):
    """The previous release's own statements still work on an upgraded store, and upgrading again is safe."""
    import asyncio
    from mcp.server.auth.provider import AuthorizationParams
    path = tmp_path / 'oauth.sqlite'
    access, _ = _single_client_store(path)
    both = _running(_config(tmp_path, CHATGPT, CLAUDE), {'chatgpt-web': True, 'claude-web': True})
    claude = _token(both, CLAUDE)
    assert authorize(both, CHATGPT)[0].status_code == 302  # leaves a new-format pending row
    with sqlite3.connect(path) as db:
        assert [row[1] for row in db.execute('PRAGMA table_info(pending)')] == ['hash', 'data', 'expires']
        rows = db.execute("SELECT data FROM pending UNION ALL SELECT data FROM codes").fetchall()
        written = [data for (data,) in rows if 'gt_client_id' in json.loads(data)]
        assert len(written) == 2  # ChatGPT's pending request and Claude's redeemed code
        for data in written:  # The single-client release parses these rows unchanged.
            AuthorizationParams.model_validate_json(data)
        db.execute('INSERT INTO pending VALUES (?, ?, ?)', ('rolled-back', '{}', time.time() + 600))
        # On start the single-client release finds no binding and writes its own again.
        assert db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone() is None
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('binding', ?)", (_single_client_binding(),))
    again = OAuthProvider(_config(tmp_path, CHATGPT, CLAUDE), participant_enabled=lambda _: True)
    assert asyncio.run(again.verify_token(access)).subject == 'chatgpt-web'
    assert asyncio.run(again.verify_token(claude['access_token'])).subject == 'claude-web'


@pytest.mark.parametrize('clients', [
    (replace(CHATGPT, redirect_uris=('https://chatgpt.com/moved',)), CLAUDE),
    (replace(CHATGPT, participant='chatgpt-renamed'),),
    (CLAUDE,),
])
def test_single_client_store_refuses_a_changed_or_missing_chatgpt_client(tmp_path, clients):
    _single_client_store(tmp_path / 'oauth.sqlite')
    with pytest.raises(ValueError, match='binding changed'):
        OAuthProvider(_config(tmp_path, *clients), participant_enabled=lambda _: True)


def _browser(setup):
    """A second browser on the same issuer, with its own empty cookie jar."""
    provider, public, _, _ = setup
    return TestClient(public.app, base_url=provider.config.issuer_url)


@pytest.mark.parametrize('client', [CHATGPT, CLAUDE], ids=['chatgpt', 'claude'])
def test_authorize_sets_a_host_only_signin_cookie(both, client):
    response, _ = authorize(both, client)
    assert response.status_code == 302
    cookie = next(value for name, value in response.headers.multi_items()
        if name == 'set-cookie' and value.startswith('__Host-gremlin_town_signin='))
    folded = cookie.lower()
    assert 'httponly' in folded and 'secure' in folded and 'samesite=lax' in folded and 'path=/' in folded
    assert 'domain=' not in folded
    value = cookie.split(';', 1)[0].split('=', 1)[1]
    assert value not in open(both[0].config.db_path, 'rb').read().decode('latin1', 'replace')


@pytest.mark.parametrize('client', [CHATGPT, CLAUDE], ids=['chatgpt', 'claude'])
def test_forwarded_consent_link_fails_in_another_browser(both, client):
    """M1: a sign-in started in one browser cannot be approved from another."""
    provider, _, private, _ = both
    response, _ = authorize(both, client)
    location = response.headers['location']
    victim = _browser(both)
    opened = victim.get(location, follow_redirects=False)
    assert opened.status_code == 400
    assert 'browser that started it' in opened.text
    # Even a valid operator handoff cannot bind the link to the other browser.
    tx = parse_qs(urlparse(location).query)['transaction'][0]
    minted = private.get(provider.config.private_base_url + '/mcp/consent-handoff?' + urlencode({'transaction': tx}),
        headers={'x-test-session': 'operator-session'}, follow_redirects=False)
    assert minted.status_code in (302, 303)
    redeemed = victim.get(minted.headers['location'], follow_redirects=False)
    assert redeemed.status_code == 400
    assert 'set-cookie' not in {name.lower() for name in redeemed.headers}


@pytest.mark.parametrize('client', [CHATGPT, CLAUDE], ids=['chatgpt', 'claude'])
def test_consent_post_requires_the_starting_browser(both, client):
    provider, public, _, _ = both
    response, _ = authorize(both, client)
    location = response.headers['location']
    page = redeem_consent(both, location)
    tx = parse_qs(urlparse(location).query)['transaction'][0]
    body = {'transaction': tx, 'csrf': _csrf(page), 'decision': 'approve'}
    other = _browser(both)
    other.cookies.set('gremlin_town_consent', public.cookies.get('gremlin_town_consent'), domain='mcp.example', path='/mcp/consent')
    refused = other.post('/mcp/consent', data=body, headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert refused.status_code == 403
    approved = public.post('/mcp/consent', data=body, headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert approved.status_code == 303
    assert parse_qs(urlparse(approved.headers['location']).query)['code']


def test_parallel_signins_in_one_browser_both_complete(both):
    provider, public, _, _ = both
    first, _ = authorize(both, CHATGPT)
    second, _ = authorize(both, CLAUDE)
    for response in (first, second):
        location = response.headers['location']
        page = redeem_consent(both, location)
        tx = parse_qs(urlparse(location).query)['transaction'][0]
        approved = public.post('/mcp/consent', data={'transaction': tx, 'csrf': _csrf(page), 'decision': 'approve'},
            headers={'origin': provider.config.issuer_url}, follow_redirects=False)
        assert approved.status_code == 303


# Security review L1: a grant (one approved connection) has a maximum age.

DAY = 86400
# The OAuth store exactly as the installed release (speed-03, e898f29) creates it.
# The grant age limit adds no table or column: only one metadata row, which that
# release never reads, so a code rollback needs no store change.
_SPEED_03_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pending (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, client TEXT NOT NULL, scopes TEXT NOT NULL,
        created REAL NOT NULL, revoked REAL);
    CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, data TEXT NOT NULL,
        expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL,
        scopes TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
    CREATE TABLE IF NOT EXISTS csrf (hash TEXT PRIMARY KEY, session TEXT NOT NULL, action TEXT NOT NULL,
        target TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS handoffs (hash TEXT PRIMARY KEY, transaction_hash TEXT NOT NULL,
        operator_session TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS consent_sessions (hash TEXT PRIMARY KEY, operator_session TEXT NOT NULL,
        transaction_hash TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS browser_bindings (transaction_hash TEXT PRIMARY KEY, browser TEXT NOT NULL,
        expires REAL NOT NULL);
'''


def _schema(db):
    """Every table and index, whitespace-normalized."""
    return [(kind, name, ' '.join((sql or '').split()))
            for kind, name, sql in db.execute('SELECT type, name, sql FROM sqlite_schema ORDER BY type, name')]


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _speed_03_store(path):
    """A store written by the installed release: both apps bound, and a ChatGPT
    connection approved 200 days ago that the app last refreshed 10 days ago."""
    now, scopes = time.time(), json.dumps(['gt:read', 'gt:write'])
    access, refresh_token = 'speed-03-access-token-' + 'a' * 24, 'speed-03-refresh-token-' + 'r' * 24
    with sqlite3.connect(path) as db:
        db.executescript(_SPEED_03_SCHEMA)
        for client in (CHATGPT, CLAUDE):
            db.execute('INSERT INTO metadata VALUES (?, ?)', ('binding:' + client.client_id, json.dumps(
                ['https://mcp.example', 'https://mcp.example/mcp', client.client_id, client.participant, client.redirect_uris])))
        db.execute('INSERT INTO grants VALUES (?, ?, ?, ?, NULL)', ('speed-03-grant', CHATGPT.client_id, scopes, now - 200 * DAY))
        for raw, kind, expires, used in (('speed-03-rotated-away', 'refresh', now + 20 * DAY, 1),
                                         (access, 'access', now + 600, 0), (refresh_token, 'refresh', now + 20 * DAY, 0)):
            db.execute('INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?)', (_sha(raw), kind, 'speed-03-grant', scopes, expires, used))
    return access, refresh_token


def _aged_from(path):
    with sqlite3.connect(path) as db:
        return float(db.execute("SELECT value FROM metadata WHERE key='grants_aged_from'").fetchone()[0])


def _time_passes(path, seconds):
    """Grant ages advance by `seconds`: approval times and the age-limit start move back.
    Token expiries stay put, as if the app kept refreshing. A restart reads the change."""
    stamp = _aged_from(path)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE grants SET created = created - ?', (seconds,))
        db.execute("UPDATE metadata SET value=? WHERE key='grants_aged_from'", (repr(stamp - seconds),))


def test_a_connection_ends_at_its_maximum_age_and_the_app_must_reconnect(tmp_path):
    import asyncio
    config = _config(tmp_path, CHATGPT)
    first = _running(config, {'chatgpt-web': True})
    tokens = exchange(first, *code(first)).json()
    # Day 89: still connected, and refreshing still works.
    _time_passes(config.db_path, 89 * DAY)
    day_89 = _running(config, {'chatgpt-web': True})
    assert asyncio.run(day_89[0].verify_token(tokens['access_token'])) is not None
    rotated = refresh(day_89, tokens['refresh_token'])
    assert rotated.status_code == 200, rotated.text
    rotated = rotated.json()
    # Past day 90 the connection is over: the day-89 refresh did not extend it, and
    # neither the 30-day refresh token nor the new access token outlives it.
    _time_passes(config.db_path, DAY + 60)
    day_90 = _running(config, {'chatgpt-web': True})
    assert asyncio.run(day_90[0].verify_token(rotated['access_token'])) is None
    ended = refresh(day_90, rotated['refresh_token'])
    assert ended.status_code == 400 and ended.json()['error'] == 'invalid_grant'
    # Reconnecting is a normal new approval with a fresh 90 days.
    fresh = exchange(day_90, *code(day_90)).json()
    assert asyncio.run(day_90[0].verify_token(fresh['access_token'])).subject == 'chatgpt-web'
    assert refresh(day_90, fresh['refresh_token']).status_code == 200


def test_the_age_limit_is_configurable(tmp_path):
    import asyncio
    config = replace(_config(tmp_path, CHATGPT), max_grant_seconds=30 * DAY)
    first = _running(config, {'chatgpt-web': True})
    tokens = exchange(first, *code(first)).json()
    _time_passes(config.db_path, 30 * DAY + 60)
    later = _running(config, {'chatgpt-web': True})
    assert asyncio.run(later[0].verify_token(tokens['access_token'])) is None
    assert refresh(later, tokens['refresh_token']).status_code == 400
    assert OAuthConfig.__dataclass_fields__['max_grant_seconds'].default == 90 * DAY
    with pytest.raises(ValueError, match='positive'):
        replace(config, max_grant_seconds=0)


def test_existing_connections_keep_refreshing_after_install_and_age_from_it(tmp_path):
    """The live store: a connection approved 200 days ago is not cut off by the install.
    Its 90 days start at the first start of this release."""
    import asyncio
    path = tmp_path / 'oauth.sqlite'
    access, refresh_token = _speed_03_store(path)
    enabled = {'chatgpt-web': True, 'claude-web': True}
    before = time.time()
    installed = _running(_config(tmp_path, CHATGPT, CLAUDE), enabled)
    assert before <= _aged_from(path) <= time.time()
    assert asyncio.run(installed[0].verify_token(access)).subject == 'chatgpt-web'
    rotated = refresh(installed, refresh_token, CHATGPT)
    assert rotated.status_code == 200, rotated.text
    rotated = rotated.json()
    assert asyncio.run(installed[0].verify_token(rotated['access_token'])).subject == 'chatgpt-web'
    page = installed[2].get('/mcp/connections', headers={'x-test-session': 'alice'})
    ends = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(_aged_from(path) + 90 * DAY))
    assert 'ChatGPT web (chatgpt-web) — active until ' + ends in page.text
    # No table or column changed, and the only new row is one the previous release ignores.
    with sqlite3.connect(':memory:') as speed_03, sqlite3.connect(path) as db:
        speed_03.executescript(_SPEED_03_SCHEMA)
        assert _schema(db) == _schema(speed_03)
        assert {key for (key,) in db.execute('SELECT key FROM metadata')} == {
            'binding:' + CHATGPT.client_id, 'binding:' + CLAUDE.client_id, 'grants_aged_from'}
    stamp = _aged_from(path)
    _running(_config(tmp_path, CHATGPT, CLAUDE), enabled)
    assert _aged_from(path) == stamp  # A restart never moves it.
    # Day 89 after install: still connected.
    _time_passes(path, 89 * DAY)
    day_89 = _running(_config(tmp_path, CHATGPT, CLAUDE), enabled)
    again = refresh(day_89, rotated['refresh_token'], CHATGPT)
    assert again.status_code == 200, again.text
    # Past day 90 after install: ChatGPT must reconnect, and the page says the grant expired.
    _time_passes(path, DAY + 60)
    day_90 = _running(_config(tmp_path, CHATGPT, CLAUDE), enabled)
    assert asyncio.run(day_90[0].verify_token(again.json()['access_token'])) is None
    assert refresh(day_90, again.json()['refresh_token'], CHATGPT).status_code == 400
    page = day_90[2].get('/mcp/connections', headers={'x-test-session': 'alice'})
    assert 'ChatGPT web (chatgpt-web) — expired ' in page.text
    assert 'name="grant_id"' not in page.text  # Nothing left to revoke.


def test_a_new_store_uses_the_same_tables_as_the_installed_release(tmp_path):
    OAuthProvider(_config(tmp_path, CHATGPT, CLAUDE), participant_enabled=lambda _: True)
    with sqlite3.connect(':memory:') as speed_03, sqlite3.connect(tmp_path / 'oauth.sqlite') as db:
        speed_03.executescript(_SPEED_03_SCHEMA)
        assert _schema(db) == _schema(speed_03)


def test_connections_page_shows_when_each_connection_ends(both):
    provider, _, private, _ = both
    _token(both, CLAUDE)
    with sqlite3.connect(provider.config.db_path) as db:
        (created,) = db.execute('SELECT created FROM grants').fetchone()
    page = private.get('/mcp/connections', headers={'x-test-session': 'alice'})
    ends = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(created + provider.config.max_grant_seconds))
    assert 'Claude (claude-web) — active until ' + ends + ' — ' in page.text


# Security review L2: a second redemption of a code ends everything issued from it
# (RFC 6749 section 4.1.2).

def _grant_revoked(provider):
    with sqlite3.connect(provider.config.db_path) as db:
        return db.execute('SELECT revoked FROM grants').fetchone()[0] is not None


@pytest.mark.parametrize('verifier', ['right', 'wrong'])
def test_reusing_a_code_fails_and_revokes_every_token_issued_from_it(setup, verifier):
    """Also with a wrong verifier: whoever replays the code, it has leaked."""
    import asyncio
    p, _, _, _ = setup
    auth_code, good = code(setup)
    tokens = exchange(setup, auth_code, good).json()
    rotated = refresh(setup, tokens['refresh_token'])
    assert rotated.status_code == 200
    rotated = rotated.json()
    assert not _grant_revoked(p)
    again = exchange(setup, auth_code, good if verifier == 'right' else 'w' * 43)
    assert again.status_code == 400 and again.json()['error'] == 'invalid_grant'
    assert _grant_revoked(p)
    for token in (tokens['access_token'], rotated['access_token']):
        assert asyncio.run(p.verify_token(token)) is None
    assert refresh(setup, rotated['refresh_token']).status_code == 400


def test_a_concurrent_second_redemption_revokes_the_first(setup):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from mcp.server.auth.provider import TokenError
    p, _, _, _ = setup
    auth_code, _ = code(setup)
    client = asyncio.run(p.get_client(p.config.clients[0].client_id))
    loaded = asyncio.run(p.load_authorization_code(client, auth_code))
    def redeem():
        try:
            return asyncio.run(p.exchange_authorization_code(client, loaded))
        except TokenError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: redeem(), range(2)))
    issued = [value for value in outcomes if not isinstance(value, TokenError)]
    assert len(issued) == 1
    # The losing redemption committed its revocation before raising.
    assert asyncio.run(p.verify_token(issued[0].access_token)) is None
    assert refresh(setup, issued[0].refresh_token).status_code == 400


def test_another_clients_replay_and_an_expired_replay_revoke_nothing(both):
    """One app cannot end another's connection. Proposed, Nic to confirm: a used code
    replayed after its 120-second life is only refused, so an old callback URL (for
    example in browser history) cannot disconnect an app."""
    import asyncio
    provider, _, _, _ = both
    auth_code, verifier = code(both, CLAUDE)
    claude = exchange(both, auth_code, verifier, CLAUDE).json()
    assert exchange(both, auth_code, verifier, CHATGPT, redirect_uri=CLAUDE.redirect_uris[0]).status_code == 400
    assert asyncio.run(provider.verify_token(claude['access_token'])).subject == 'claude-web'
    with sqlite3.connect(provider.config.db_path) as db:
        db.execute('UPDATE codes SET expires=?', (time.time() - 1,))
    assert exchange(both, auth_code, verifier, CLAUDE).status_code == 400
    assert asyncio.run(provider.verify_token(claude['access_token'])).subject == 'claude-web'
    assert refresh(both, claude['refresh_token'], CLAUDE).status_code == 200


# Security review L3: an approval page ends with the operator sign-in that opened it.

def _signed_out_setup(tmp_path):
    provider = OAuthProvider(_config(tmp_path, CHATGPT), participant_enabled=lambda _: True)
    operators = _Operators()
    return (provider, *_routes(provider, operators), [True]), operators


@pytest.mark.parametrize('decision', ['approve', 'deny'])
def test_signing_out_ends_an_open_approval_page_at_once(tmp_path, decision):
    local, operators = _signed_out_setup(tmp_path)
    provider, public, private, _ = local
    response, _ = authorize(local)
    location = response.headers['location']
    page = redeem_consent(local, location, session='alice')
    body = {'transaction': parse_qs(urlparse(location).query)['transaction'][0], 'csrf': _csrf(page), 'decision': decision}
    operators.signed_out.add('alice')
    refused = public.post('/mcp/consent', data=body, headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert refused.status_code == 403 and refused.text == SIGNED_OUT
    with sqlite3.connect(provider.config.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM consent_sessions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM pending').fetchone()[0] == 1  # The app's sign-in is still open.
    # Reopening the page asks for a live operator sign-in again, and a new one works.
    bounced = public.get(location, follow_redirects=False)
    assert urlparse(bounced.headers['location']).path == '/mcp/consent-handoff'
    assert private.get(bounced.headers['location'], headers={'x-test-session': 'alice'}).status_code == 403
    page = redeem_consent(local, location, session='bob')
    finished = public.post('/mcp/consent', data={**body, 'csrf': _csrf(page)},
        headers={'origin': provider.config.issuer_url}, follow_redirects=False)
    assert finished.status_code == 303
    assert parse_qs(urlparse(finished.headers['location']).query)['code' if decision == 'approve' else 'error']


def test_a_handoff_minted_before_signing_out_cannot_open_the_page(tmp_path):
    local, operators = _signed_out_setup(tmp_path)
    provider, public, private, _ = local
    response, _ = authorize(local)
    bounced = public.get(response.headers['location'], follow_redirects=False)
    minted = private.get(bounced.headers['location'], headers={'x-test-session': 'alice'}, follow_redirects=False)
    assert minted.status_code == 302
    operators.signed_out.add('alice')
    opened = public.get(minted.headers['location'], follow_redirects=False)
    assert opened.status_code == 400 and opened.text == SIGNED_OUT
    assert 'set-cookie' not in {name.lower() for name in opened.headers}


def test_approval_pages_refuse_everyone_until_private_routes_exist(tmp_path):
    """Fail closed: without the private listener's session list, no one is signed in."""
    provider = OAuthProvider(_config(tmp_path, CHATGPT), participant_enabled=lambda _: True)
    assert provider._signed_in_operators() == frozenset()
    operators = _Operators()
    _routes(provider, operators)
    operators.seen.add('alice')
    assert provider._signed_in_operators() == {hashlib.sha256(b'alice').hexdigest()}
