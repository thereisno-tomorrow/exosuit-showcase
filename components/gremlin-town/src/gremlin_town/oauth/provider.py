from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial

import anyio
import hashlib
import html
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, TokenError, construct_redirect_uri
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.server.transport_security import RequestBodyLimitMiddleware
from pydantic import AnyHttpUrl, AnyUrl
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

DEFAULT_SCOPES = ('gt:read', 'gt:write')
SUPPORTED_SCOPES = (*DEFAULT_SCOPES, 'bc:read', 'bc:write')
# no-referrer makes Chromium send Origin: null on form POSTs, failing our CSRF gate.
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
           'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
           'X-Content-Type-Options': 'nosniff'}
# Issuer-only consent session. The private operator cookie cannot cross hosts.
CONSENT_COOKIE = 'gremlin_town_consent'
# Set at /authorize and required on consent, so a consent link only works in the
# browser that started the sign-in. __Host- blocks cookies set from other subdomains.
BROWSER_COOKIE = '__Host-gremlin_town_signin'
WRONG_BROWSER = 'Finish this connection in the browser that started it. Start again from the app you are connecting.'
_signin_browser: ContextVar[str | None] = ContextVar('gremlin_town_signin_browser', default=None)
HANDOFF_SECONDS = 120
CONSENT_SESSION_SECONDS = 600
# Single-client stores issued every grant to this participant.
LEGACY_SUBJECT = 'chatgpt-web'
EXPIRED = 'Connection request expired. Start again from the app you are connecting.'
SIGNED_OUT = ('Operator sign-in ended. Sign in again on the private network, then start again '
              'from the app you are connecting.')
# Pending and code rows keep their single-client schema so a code rollback still
# works: the client travels inside the stored JSON, which older code ignores.
PENDING_CLIENT_KEY = 'gt_client_id'
# A grant (one approved connection) lasts at most max_grant_seconds; then refresh
# fails and the app must reconnect. Grants approved before this limit existed age
# from the first start of a release that has it, recorded once in this metadata row,
# so an install cuts nobody off. Older releases ignore the row.
GRANTS_AGED_FROM = 'grants_aged_from'


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _secret() -> str:
    return secrets.token_urlsafe(32)


class _Transaction:
    # A class context manager avoids contextlib assigning __traceback__ on the
    # SDK's frozen TokenError/AuthorizeError dataclasses during exception unwind.
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('BEGIN IMMEDIATE')
        return self.db

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()
        return False


@dataclass(frozen=True)
class OAuthClient:
    """One preregistered public PKCE client, enrolled as exactly one participant."""
    client_id: str
    participant: str
    name: str
    redirect_uris: tuple[str, ...]
    # Browser origins this client's requests may carry. Omitted Origin is always allowed.
    origins: tuple[str, ...] = ()
    # Only shown on the private setup page, for connector forms that insist on a
    # secret. The client stays public: the token endpoint never checks it.
    client_secret: str | None = field(default=None, repr=False)

    def __post_init__(self):
        object.__setattr__(self, 'redirect_uris', tuple(self.redirect_uris))
        object.__setattr__(self, 'origins', tuple(self.origins))
        if not self.client_id or not self.participant.strip() or not self.name.strip() or not self.redirect_uris:
            raise ValueError('A preconfigured client, participant, name and exact callbacks are required')
        if self.client_secret is not None and len(self.client_secret) < 32:
            raise ValueError('A configured client secret must be a strong secret')
        for uri in self.redirect_uris:
            parsed = urlsplit(uri)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.fragment or parsed.username or parsed.password:
                raise ValueError('Callbacks must be exact HTTPS URLs without fragments')
        for origin in self.origins:
            parsed = urlsplit(origin)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                    or parsed.username or parsed.password):
                raise ValueError('Client origins must be exact HTTPS origins')


@dataclass(frozen=True)
class OAuthConfig:
    db_path: Path
    issuer_url: str
    resource_url: str
    private_base_url: str
    clients: tuple[OAuthClient, ...]
    access_seconds: int = 900
    refresh_seconds: int = 30 * 86400
    transaction_seconds: int = 600
    code_seconds: int = 120
    max_grant_seconds: int = 90 * 86400

    def __post_init__(self):
        object.__setattr__(self, 'db_path', Path(self.db_path))
        for name in ('issuer_url', 'private_base_url'):
            value = getattr(self, name).rstrip('/')
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError(f'{name} must be an HTTPS origin without a path')
            object.__setattr__(self, name, value)
        resource = urlsplit(self.resource_url)
        if (resource.scheme, resource.netloc) != (urlsplit(self.issuer_url).scheme, urlsplit(self.issuer_url).netloc) or resource.query or resource.fragment:
            raise ValueError('resource_url must be on issuer_url without query or fragment')
        object.__setattr__(self, 'clients', tuple(self.clients))
        if not self.clients or not all(isinstance(client, OAuthClient) for client in self.clients):
            raise ValueError('At least one preconfigured client is required')
        if (len({client.client_id for client in self.clients}) != len(self.clients)
                or len({client.participant for client in self.clients}) != len(self.clients)):
            raise ValueError('Each client needs a distinct client ID and participant')
        if min(self.access_seconds, self.refresh_seconds, self.transaction_seconds, self.code_seconds,
               self.max_grant_seconds) <= 0:
            raise ValueError('Token, transaction and grant lifetimes must be positive')

    def client(self, client_id) -> OAuthClient | None:
        return next((client for client in self.clients if client.client_id == client_id), None)

    @property
    def client_origins(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(origin for client in self.clients for origin in client.origins))


class OAuthProvider:
    """MCP SDK authorization provider and per-request TokenVerifier.

    SQLite transactions serialize exchange/rotation across threads and processes.
    Only hashes of bearer values persist. A configured client_secret stays in
    runtime configuration, is not registered on the public PKCE client, and
    is never stored in this database. Every grant belongs to one client, and a
    token's subject is always that client's configured participant. A grant is
    usable until it is revoked or reaches its maximum age (see GRANTS_AGED_FROM).
    """

    def __init__(self, config: OAuthConfig, participant_enabled: Callable[[str], bool]):
        self.config = config
        self.participant_enabled = participant_enabled
        # Set by private_routes. Until then no operator counts as signed in.
        self._signed_in: Callable[[], Iterable[str]] | None = None
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
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
            ''')
        with self._db() as db:
            legacy = db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
            if legacy:
                # Upgrade a single-client store in place so its existing grants survive.
                # Idempotent: a rolled-back single-client release writes this row again.
                owner = next((client for client in config.clients if client.participant == LEGACY_SUBJECT and legacy['value']
                    == json.dumps([config.issuer_url, config.resource_url, client.client_id, client.redirect_uris])), None)
                if owner is None:
                    raise ValueError('OAuth store configuration binding changed; use a fresh store and reauthorize')
                db.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('binding:' + owner.client_id, self._binding(owner)))
                db.execute("DELETE FROM metadata WHERE key='binding'")
            for client in config.clients:
                existing = db.execute('SELECT value FROM metadata WHERE key=?', ('binding:' + client.client_id,)).fetchone()
                if existing and existing['value'] != self._binding(client):
                    raise ValueError(f'OAuth store configuration binding changed for client {client.client_id!r}; '
                        'use a fresh store and reauthorize')
                db.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('binding:' + client.client_id, self._binding(client)))
            # Written once, by the first start with a grant age limit; never moved later.
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', (GRANTS_AGED_FROM, repr(time.time())))
            self._aged_from = float(db.execute('SELECT value FROM metadata WHERE key=?', (GRANTS_AGED_FROM,)).fetchone()['value'])

    def _db(self):
        return _Transaction(self.config.db_path)

    def _binding(self, client):
        return json.dumps([self.config.issuer_url, self.config.resource_url, client.client_id, client.participant,
            client.redirect_uris])

    def _client(self, client_id):
        """The configured client, only while its participant may use MCP."""
        client = self.config.client(client_id)
        return client if client is not None and self.participant_enabled(client.participant) else None

    @staticmethod
    def _pending_client_id(data):
        """Rows written before multi-client support name no client, so they simply expire."""
        return json.loads(data).get(PENDING_CLIENT_KEY)

    async def get_client(self, client_id):
        client = self._client(client_id)
        if client is None:
            return None
        return OAuthClientInformationFull(client_id=client.client_id, client_secret=None,
            client_name=client.name, redirect_uris=[AnyUrl(u) for u in client.redirect_uris],
            token_endpoint_auth_method='none', grant_types=['authorization_code', 'refresh_token'],
            response_types=['code'], scope=' '.join(SUPPORTED_SCOPES))

    async def register_client(self, client_info):
        raise NotImplementedError('Dynamic client registration is disabled')

    async def authorize(self, client, params):
        # The public /authorize wrapper supplies the browser value it sets as a cookie.
        browser = _signin_browser.get()
        if browser is None:
            raise AuthorizeError('server_error', 'Sign-in must start at the issuer /authorize route')
        return await anyio.to_thread.run_sync(partial(self._authorize, client, params, browser))

    def _authorize(self, client, params: AuthorizationParams, browser: str):
        registered = self._client(client.client_id)
        if registered is None:
            raise AuthorizeError('unauthorized_client', 'Client is not enabled')
        if params.resource != self.config.resource_url:
            raise AuthorizeError('invalid_request', 'Exact MCP resource is required')
        if str(params.redirect_uri) not in registered.redirect_uris or not params.redirect_uri_provided_explicitly:
            raise AuthorizeError('invalid_request', 'Exact registered callback is required')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.code_challenge):
            raise AuthorizeError('invalid_request', 'Valid S256 PKCE challenge is required')
        # Omitted scopes retain the established GT-only grant. Brain access is
        # always an explicit consent request and remains subject to the separate
        # host-owned Brain policy on every tool call.
        scopes = params.scopes if params.scopes is not None else list(DEFAULT_SCOPES)
        if not scopes or not set(scopes) <= set(SUPPORTED_SCOPES):
            raise AuthorizeError('invalid_scope', 'Unsupported scope')
        params.scopes = sorted(set(scopes))
        transaction = _secret()
        with self._db() as db:
            now = time.time()
            db.execute('DELETE FROM pending WHERE expires < ?', (now,))
            db.execute('DELETE FROM csrf WHERE expires < ?', (now,))
            db.execute('DELETE FROM handoffs WHERE expires < ?', (now,))
            db.execute('DELETE FROM consent_sessions WHERE expires < ?', (now,))
            db.execute('DELETE FROM browser_bindings WHERE expires < ?', (now,))
            if db.execute('SELECT COUNT(*) FROM pending').fetchone()[0] >= 100:
                raise AuthorizeError('temporarily_unavailable', 'Too many pending authorization requests')
            data = json.dumps({**params.model_dump(mode='json'), PENDING_CLIENT_KEY: registered.client_id})
            db.execute('INSERT INTO pending VALUES (?, ?, ?)', (_hash(transaction), data, time.time() + self.config.transaction_seconds))
            db.execute('INSERT INTO browser_bindings VALUES (?, ?, ?)', (
                _hash(transaction), _hash(browser), time.time() + self.config.transaction_seconds))
        # The app must finish on the issuer. Operator proof is a one-time private handoff.
        return self.config.issuer_url + '/mcp/consent?' + urlencode({'transaction': transaction})

    def _grant_deadline(self, created):
        """When a grant stops working: its maximum age after approval, or after the age limit arrived."""
        return max(created, self._aged_from) + self.config.max_grant_seconds

    def _age_bound(self):
        """Parameters for ``MAX(created, ?) > ?``: true while a grant is younger than its maximum age."""
        return self._aged_from, time.time() - self.config.max_grant_seconds

    def _valid_grant(self, db, grant_id, client_id):
        """A grant is usable only by the client it was issued to, and only until its maximum age."""
        return db.execute('SELECT * FROM grants WHERE id=? AND client=? AND revoked IS NULL AND MAX(created, ?) > ?',
            (grant_id, client_id, *self._age_bound())).fetchone()

    async def load_authorization_code(self, client, authorization_code):
        return await anyio.to_thread.run_sync(partial(self._load_authorization_code, client, authorization_code))

    def _redeemable_code(self, db, authorization_code, client_id):
        """The code's row while this client may still redeem it, else None.

        A code is single-use. Presenting a used code again within its lifetime ends
        the grant it created (RFC 6749 section 4.1.2; security review L2): every token
        issued from the code, refreshed ones included, stops working. Callers must let
        the transaction commit, never raise inside it, so the revocation is kept.
        """
        row = db.execute('SELECT * FROM codes WHERE hash=? AND expires>?', (_hash(authorization_code), time.time())).fetchone()
        if not row or not self._valid_grant(db, row['grant_id'], client_id):
            return None
        if row['used']:
            db.execute('UPDATE grants SET revoked=? WHERE id=? AND revoked IS NULL', (time.time(), row['grant_id']))
            return None
        return row

    def _load_authorization_code(self, client, authorization_code):
        registered = self._client(client.client_id)
        if registered is None:
            return None
        with self._db() as db:
            row = self._redeemable_code(db, authorization_code, registered.client_id)
            if row is None:
                return None
            params = AuthorizationParams.model_validate_json(row['data'])
            return AuthorizationCode(code=authorization_code, client_id=registered.client_id, expires_at=row['expires'],
                subject=registered.participant, **params.model_dump(exclude={'state'}))

    def _issue(self, db, grant_id, scopes):
        access, refresh = _secret(), _secret()
        now = time.time()
        for raw, kind, lifetime in ((access, 'access', self.config.access_seconds), (refresh, 'refresh', self.config.refresh_seconds)):
            db.execute('INSERT INTO tokens VALUES (?, ?, ?, ?, ?, 0)', (_hash(raw), kind, grant_id, json.dumps(scopes), now + lifetime))
        return OAuthToken(access_token=access, token_type='Bearer', expires_in=self.config.access_seconds,
            refresh_token=refresh, scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        return await anyio.to_thread.run_sync(partial(self._exchange_authorization_code, client, authorization_code))

    def _exchange_authorization_code(self, client, authorization_code):
        registered = self._client(client.client_id)
        if registered is None:
            raise TokenError('invalid_grant', 'Client is not enabled')
        result = None
        with self._db() as db:
            # Two concurrent redemptions both load the code; the second one lands here.
            row = self._redeemable_code(db, authorization_code.code, registered.client_id)
            if row is not None:
                db.execute('UPDATE codes SET used=1 WHERE hash=?', (_hash(authorization_code.code),))
                scopes = AuthorizationParams.model_validate_json(row['data']).scopes
                result = self._issue(db, row['grant_id'], scopes)
        # Raise after committing so a reused code's revocation is durable.
        if result is None:
            raise TokenError('invalid_grant', 'Code is unavailable')
        return result

    async def load_refresh_token(self, client, refresh_token):
        return await anyio.to_thread.run_sync(partial(self._load_refresh_token, client, refresh_token))

    def _load_refresh_token(self, client, refresh_token):
        registered = self._client(client.client_id)
        if registered is None:
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM tokens WHERE hash=? AND kind='refresh'", (_hash(refresh_token),)).fetchone()
            if not row or not self._valid_grant(db, row['grant_id'], registered.client_id):
                return None
            if row['used']:
                db.execute('UPDATE grants SET revoked=? WHERE id=?', (time.time(), row['grant_id']))
                return None
            if row['expires'] <= time.time():
                return None
            return RefreshToken(token=refresh_token, client_id=registered.client_id, scopes=json.loads(row['scopes']),
                expires_at=int(row['expires']), resource=self.config.resource_url, subject=registered.participant)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return await anyio.to_thread.run_sync(partial(self._exchange_refresh_token, client, refresh_token, scopes))

    def _exchange_refresh_token(self, client, refresh_token, scopes):
        registered = self._client(client.client_id)
        if registered is None:
            raise TokenError('invalid_grant', 'Client is not enabled')
        result = None
        with self._db() as db:
            row = db.execute("SELECT * FROM tokens WHERE hash=? AND kind='refresh'", (_hash(refresh_token.token),)).fetchone()
            if row and self._valid_grant(db, row['grant_id'], registered.client_id):
                if row['used']:
                    db.execute('UPDATE grants SET revoked=? WHERE id=?', (time.time(), row['grant_id']))
                elif row['expires'] > time.time():
                    if not scopes or not set(scopes) <= set(json.loads(row['scopes'])):
                        raise TokenError('invalid_scope', 'Scopes cannot increase')
                    db.execute('UPDATE tokens SET used=1 WHERE grant_id=?', (row['grant_id'],))
                    result = self._issue(db, row['grant_id'], sorted(set(scopes)))
        # Raise after committing so concurrent refresh reuse revocation is durable.
        if result is None:
            raise TokenError('invalid_grant', 'Refresh token is unavailable')
        return result

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def load_access_token(self, token):
        return await anyio.to_thread.run_sync(partial(self._load_access_token, token))

    def _load_access_token(self, token):
        with self._db() as db:
            row = db.execute("SELECT tokens.*, grants.client FROM tokens JOIN grants ON grants.id=tokens.grant_id "
                "WHERE tokens.hash=? AND tokens.kind='access' AND tokens.used=0 AND tokens.expires>? AND grants.revoked IS NULL "
                "AND MAX(grants.created, ?) > ?", (_hash(token), time.time(), *self._age_bound())).fetchone()
        registered = self._client(row['client']) if row else None
        if registered is None:
            return None
        return AccessToken(token=token, client_id=registered.client_id, scopes=json.loads(row['scopes']),
            expires_at=int(row['expires']), resource=self.config.resource_url, subject=registered.participant,
            claims={'iss': self.config.issuer_url, 'grant_id': row['grant_id']})

    async def revoke_token(self, token):
        return await anyio.to_thread.run_sync(partial(self._revoke_token, token))

    def _revoke_token(self, token):
        with self._db() as db:
            row = db.execute('SELECT grant_id FROM tokens WHERE hash=?', (_hash(token.token),)).fetchone()
            if row:
                db.execute('UPDATE grants SET revoked=? WHERE id=?', (time.time(), row['grant_id']))

    def revoke_all(self):
        """Explicit operator recovery action; never invoked on normal startup."""
        with self._db() as db:
            db.execute('UPDATE grants SET revoked=? WHERE revoked IS NULL', (time.time(),))
            db.execute('DELETE FROM pending')
            db.execute('DELETE FROM csrf')
            db.execute('DELETE FROM handoffs')
            db.execute('DELETE FROM consent_sessions')
            db.execute('DELETE FROM browser_bindings')

    def public_routes(self):
        routes = create_auth_routes(self, AnyHttpUrl(self.config.issuer_url),
            client_registration_options=ClientRegistrationOptions(enabled=False, valid_scopes=list(SUPPORTED_SCOPES)),
            revocation_options=RevocationOptions(enabled=True))
        # SDK 1.30.0 checks PKCE and redirect but does not enforce the token
        # request resource indicator. Wrap that gap before delegating to it.
        for route in routes:
            if route.path == '/.well-known/oauth-authorization-server':
                # SDK's model does not include RFC 9207's discovery flag.
                metadata = build_metadata(AnyHttpUrl(self.config.issuer_url), None,
                    ClientRegistrationOptions(enabled=False, valid_scopes=list(SUPPORTED_SCOPES)), RevocationOptions(enabled=True)).model_dump(mode='json', exclude_none=True)
                metadata['issuer'] = self.config.issuer_url
                metadata['authorization_response_iss_parameter_supported'] = True
                metadata['token_endpoint_auth_methods_supported'] = ['none']
                metadata['revocation_endpoint_auth_methods_supported'] = ['none']
                async def issuer_metadata(request):
                    return JSONResponse(metadata, headers=HEADERS)
                route.app = cors_middleware(issuer_metadata, ['GET', 'OPTIONS'])
            if route.path == '/authorize':
                authorize_app = route.app
                async def issuer_redirect(scope, receive, send, authorize_app=authorize_app):
                    if len(scope.get('query_string', b'')) > 8192:
                        await Response(status_code=414)(scope, receive, send)
                        return
                    # Reuse this browser's value so parallel sign-ins in one browser all stay valid.
                    browser = Request(scope).cookies.get(BROWSER_COOKIE, '')
                    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', browser):
                        browser = _secret()
                    consent_url = self.config.issuer_url + '/mcp/consent?'
                    async def with_issuer(message):
                        if message['type'] == 'http.response.start':
                            headers, to_consent = [], False
                            for name, value in message.get('headers', []):
                                if name.lower() == b'location':
                                    to_consent = value.decode('latin1').startswith(consent_url)
                                    value = construct_redirect_uri(value.decode('latin1'), iss=self.config.issuer_url).encode('latin1')
                                headers.append((name, value))
                            if to_consent:
                                cookie = Response()
                                cookie.set_cookie(BROWSER_COOKIE, browser, max_age=self.config.transaction_seconds,
                                    httponly=True, secure=True, samesite='lax', path='/')
                                headers.extend(header for header in cookie.raw_headers if header[0] == b'set-cookie')
                            message = dict(message, headers=headers)
                        await send(message)
                    reset = _signin_browser.set(browser)
                    try:
                        await authorize_app(scope, receive, with_issuer)
                    finally:
                        _signin_browser.reset(reset)
                route.app = issuer_redirect
            if route.path == '/token':
                sdk_app = route.app
                async def checked_token(scope, receive, send, sdk_app=sdk_app):
                    if scope['method'] == 'POST':
                        request = Request(scope, receive)
                        chunks, length = [], 0
                        async for chunk in request.stream():
                            length += len(chunk)
                            if length > 16384:
                                await Response(status_code=413)(scope, receive, send)
                                return
                            chunks.append(chunk)
                        body = b''.join(chunks)
                        request._body = body
                        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                            await Response(status_code=415)(scope, receive, send)
                            return
                        form = await request.form()
                        if any(len(form.getlist(key)) != 1 for key in form) or form.get('resource') != self.config.resource_url:
                            error_body = {'error': 'invalid_request', 'error_description': 'Exact single MCP resource is required'}
                            await JSONResponse(error_body, status_code=400, headers=HEADERS)(scope, receive, send)
                            return
                        if form.get('grant_type') == 'authorization_code' and not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', str(form.get('code_verifier', ''))):
                            error_body = {'error': 'invalid_grant', 'error_description': 'Invalid PKCE verifier'}
                            await JSONResponse(error_body, status_code=400, headers=HEADERS)(scope, receive, send)
                            return
                        del form
                        async def replay():
                            return {'type': 'http.request', 'body': body, 'more_body': False}
                        await sdk_app(scope, replay, send)
                    else:
                        await sdk_app(scope, receive, send)
                route.app = checked_token
            if route.path == '/revoke':
                # SDK 1.30.0's revocation model requires a client_secret field even
                # for public clients, so a standard RFC 7009 revoke got 400 and the
                # grant stayed live. Supply the empty field; 'none' auth never reads it.
                sdk_revoke = route.app
                async def public_revoke(scope, receive, send, sdk_revoke=sdk_revoke):
                    if scope['method'] != 'POST':
                        await sdk_revoke(scope, receive, send)
                        return
                    chunks, length = [], 0
                    async for chunk in Request(scope, receive).stream():
                        length += len(chunk)
                        if length > 16384:
                            await Response(status_code=413)(scope, receive, send)
                            return
                        chunks.append(chunk)
                    body = b''.join(chunks)
                    content_type = Headers(scope=scope).get('content-type', '').split(';')[0]
                    if content_type == 'application/x-www-form-urlencoded' and 'client_secret' not in parse_qs(
                            body.decode('latin1'), keep_blank_values=True):
                        body += (b'&' if body else b'') + b'client_secret='
                    async def replay():
                        return {'type': 'http.request', 'body': body, 'more_body': False}
                    await sdk_revoke(scope, replay, send)
                route.app = public_revoke
            if route.path in ('/authorize', '/token', '/revoke'):
                route.app = RequestBodyLimitMiddleware(route.app, 16384)
        protected = create_protected_resource_routes(AnyHttpUrl(self.config.resource_url), [AnyHttpUrl(self.config.issuer_url)], scopes_supported=list(SUPPORTED_SCOPES), resource_name='Gremlin Town')
        async def resource_metadata(request):
            return JSONResponse({'resource': self.config.resource_url, 'authorization_servers': [self.config.issuer_url],
                'scopes_supported': list(SUPPORTED_SCOPES), 'bearer_methods_supported': ['header'], 'resource_name': 'Gremlin Town'}, headers=HEADERS)
        for route in protected:
            route.app = cors_middleware(resource_metadata, ['GET', 'OPTIONS'])
        routes.extend(protected)
        routes.extend(self._issuer_consent_routes())
        return routes

    def _csrf(self, session, action, target):
        csrf = _secret()
        with self._db() as db:
            db.execute('DELETE FROM csrf WHERE expires < ?', (time.time(),))
            db.execute('INSERT INTO csrf VALUES (?, ?, ?, ?, ?)', (_hash(csrf), _hash(session), action, target, time.time() + 600))
        return csrf

    def _consume_csrf(self, db, csrf, session, action, target):
        row = db.execute('DELETE FROM csrf WHERE hash=? AND session=? AND action=? AND target=? AND expires>? RETURNING hash',
            (_hash(csrf), _hash(session), action, target, time.time())).fetchone()
        return row is not None

    def _issuer_consent_routes(self):
        """Consent form on the issuer. A missing session bounces to the private handoff."""

        def same_browser(db, request, transaction):
            browser = request.cookies.get(BROWSER_COOKIE, '')
            return bool(browser) and db.execute(
                'SELECT 1 FROM browser_bindings WHERE transaction_hash=? AND browser=? AND expires>?',
                (_hash(transaction), _hash(browser), time.time())).fetchone() is not None

        def consent_headers(redirect_uri):
            # Browsers also check a form's redirect destination. Permit the
            # already-validated callback path, never arbitrary sites or schemes.
            callback = quote(str(redirect_uri).split('?', 1)[0], safe=':/%.-_~')
            return {**HEADERS, 'Content-Security-Policy': HEADERS['Content-Security-Policy'].replace(
                "form-action 'self';", "form-action 'self' " + callback + ';')}

        async def consent(request):
            form = await request.form() if request.method == 'POST' else None
            # Operator sessions live in the private app's memory: read them here, on the
            # event loop, before the SQLite work moves to a worker thread.
            signed_in = self._signed_in_operators()
            return await anyio.to_thread.run_sync(partial(consent_response, request, form, signed_in))

        def consent_response(request, form, signed_in):
            if request.method == 'GET':
                transaction = request.query_params.get('transaction', '')
                handoff = request.query_params.get('handoff', '')
                minted = None
                with self._db() as db:
                    now = time.time()
                    db.execute('DELETE FROM handoffs WHERE expires < ?', (now,))
                    db.execute('DELETE FROM consent_sessions WHERE expires < ?', (now,))
                    row = db.execute('SELECT * FROM pending WHERE hash=? AND expires>?', (_hash(transaction), time.time())).fetchone()
                    registered = self.config.client(self._pending_client_id(row['data'])) if row else None
                    if registered is None:
                        return Response(EXPIRED, status_code=400, headers=HEADERS)
                    if not same_browser(db, request, transaction):
                        return Response(WRONG_BROWSER, status_code=400, headers=HEADERS)
                    params_json = row['data']
                    cookie = request.cookies.get(CONSENT_COOKIE, '')
                    bound = None
                    if cookie:
                        bound = db.execute(
                            'SELECT operator_session FROM consent_sessions WHERE hash=? AND transaction_hash=? AND expires>?',
                            (_hash(cookie), _hash(transaction), time.time())).fetchone()
                        if bound is not None and bound['operator_session'] not in signed_in:
                            # L3: the operator signed out, so this page needs a new sign-in.
                            db.execute('DELETE FROM consent_sessions WHERE hash=?', (_hash(cookie),))
                            bound = None
                    if bound is None and handoff:
                        redeemed = db.execute(
                            'DELETE FROM handoffs WHERE hash=? AND transaction_hash=? AND expires>? RETURNING operator_session',
                            (_hash(handoff), _hash(transaction), time.time())).fetchone()
                        if redeemed is None:
                            return Response('Consent handoff is invalid or expired. Start again from the app you are connecting.',
                                status_code=400, headers=HEADERS)
                        if redeemed['operator_session'] not in signed_in:
                            return Response(SIGNED_OUT, status_code=400, headers=HEADERS)
                        minted = _secret()
                        db.execute('INSERT INTO consent_sessions VALUES (?, ?, ?, ?)', (
                            _hash(minted), redeemed['operator_session'], _hash(transaction), time.time() + CONSENT_SESSION_SECONDS))
                        cookie = minted
                    elif bound is None:
                        cookie = ''
                if not cookie:
                    location = self.config.private_base_url + '/mcp/consent-handoff?' + urlencode({'transaction': transaction})
                    return RedirectResponse(location, status_code=302, headers=HEADERS)
                params = AuthorizationParams.model_validate_json(params_json)
                csrf = self._csrf(cookie, 'consent', _hash(transaction))
                response = HTMLResponse('<h1>Connect ' + html.escape(registered.name) + ' to Gremlin Town</h1><p>Participant: '
                    + html.escape(registered.participant) + '</p><p>Permissions: '
                    + html.escape(' '.join(params.scopes or [])) + '</p><p>Return to: ' + html.escape(str(params.redirect_uri))
                    + '</p><form method="post"><input type="hidden" name="transaction" value="' + html.escape(transaction, quote=True)
                    + '"><input type="hidden" name="csrf" value="' + csrf + '"><button name="decision" value="approve">Approve access</button> '
                    + '<button name="decision" value="deny">Deny</button></form>', headers=consent_headers(params.redirect_uri))
                if minted is not None:
                    response.set_cookie(CONSENT_COOKIE, minted, max_age=CONSENT_SESSION_SECONDS, httponly=True,
                        secure=True, samesite='lax', path='/mcp/consent')
                return response
            if request.headers.get('origin') != self.config.issuer_url:
                return Response(status_code=403, headers=HEADERS)
            transaction, csrf = str(form.get('transaction', '')), str(form.get('csrf', ''))
            decision = form.get('decision')
            if decision not in ('approve', 'deny'):
                return Response(status_code=400, headers=HEADERS)
            cookie = request.cookies.get(CONSENT_COOKIE, '')
            if not cookie:
                return Response(status_code=403, headers=HEADERS)
            with self._db() as db:
                bound = db.execute(
                    'SELECT operator_session FROM consent_sessions WHERE hash=? AND transaction_hash=? AND expires>?',
                    (_hash(cookie), _hash(transaction), time.time())).fetchone()
                if bound is None or not same_browser(db, request, transaction):
                    return Response(status_code=403, headers=HEADERS)
                if bound['operator_session'] not in signed_in:
                    # L3: an approval page ends with the operator sign-in that opened it.
                    db.execute('DELETE FROM consent_sessions WHERE hash=?', (_hash(cookie),))
                    return Response(SIGNED_OUT, status_code=403, headers=HEADERS)
                if not self._consume_csrf(db, csrf, cookie, 'consent', _hash(transaction)):
                    return Response(status_code=403, headers=HEADERS)
                row = db.execute('DELETE FROM pending WHERE hash=? AND expires>? RETURNING *', (_hash(transaction), time.time())).fetchone()
                registered = self._client(self._pending_client_id(row['data'])) if row else None
                if registered is None:
                    return Response('Connection request unavailable', status_code=400, headers=HEADERS)
                params = AuthorizationParams.model_validate_json(row['data'])
                if decision == 'deny':
                    location = construct_redirect_uri(str(params.redirect_uri), error='access_denied', state=params.state, iss=self.config.issuer_url)
                else:
                    grant_id, code = _secret(), _secret()
                    db.execute('INSERT INTO grants VALUES (?, ?, ?, ?, NULL)', (grant_id, registered.client_id, json.dumps(params.scopes), time.time()))
                    db.execute('INSERT INTO codes VALUES (?, ?, ?, ?, 0)', (_hash(code), grant_id, row['data'], time.time() + self.config.code_seconds))
                    location = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state, iss=self.config.issuer_url)
            return RedirectResponse(location, status_code=303, headers=consent_headers(params.redirect_uri))

        routes = [Route('/mcp/consent', consent, methods=['GET', 'POST'])]
        for route in routes:
            route.app = RequestBodyLimitMiddleware(route.app, 16384)
        return routes

    def _signed_in_operators(self) -> frozenset[str]:
        """Hashes of the operator sessions signed in right now."""
        return frozenset(_hash(session) for session in self._signed_in()) if self._signed_in else frozenset()

    def private_routes(self, require_operator: Callable[[Request], Awaitable[str | None]],
                       signed_in: Callable[[], Iterable[str]]):
        """Resolver must return the existing authenticated operator session ID.

        None denies. The root may raise/return a login redirect before invoking
        these routes, but it must never accept an operator ID supplied by a client.
        Consent itself is on the issuer. This listener only mints the handoff.

        ``signed_in`` returns the IDs of the operator sessions signed in right now.
        The issuer's approval page and its POST recheck the session that minted
        their handoff against it, so signing out, the session expiring, losing the
        operator role or a service restart ends every open approval page at once
        (security review L3). It is called on the event loop and must not block.
        """
        self._signed_in = signed_in

        async def consent_handoff(request):
            session = await require_operator(request)
            if not session:
                return Response('Operator sign-in required', status_code=403, headers=HEADERS)
            return await anyio.to_thread.run_sync(partial(handoff_response, request, session))

        def handoff_response(request, session):
            transaction = request.query_params.get('transaction', '')
            with self._db() as db:
                row = db.execute('SELECT 1 FROM pending WHERE hash=? AND expires>?', (_hash(transaction), time.time())).fetchone()
                if not row:
                    return Response(EXPIRED, status_code=400, headers=HEADERS)
                db.execute('DELETE FROM handoffs WHERE expires < ?', (time.time(),))
                token = _secret()
                db.execute('INSERT INTO handoffs VALUES (?, ?, ?, ?)', (
                    _hash(token), _hash(transaction), _hash(session), time.time() + HANDOFF_SECONDS))
            location = self.config.issuer_url + '/mcp/consent?' + urlencode({'transaction': transaction, 'handoff': token})
            return RedirectResponse(location, status_code=302, headers=HEADERS)

        async def connections(request):
            session = await require_operator(request)
            if not session:
                return Response('Operator sign-in required', status_code=403, headers=HEADERS)
            form = await request.form() if request.method == 'POST' else None
            return await anyio.to_thread.run_sync(partial(connections_response, request, session, form))

        def label(client_id):
            client = self.config.client(client_id)
            return (client.name + ' (' + client.participant + ')') if client else 'Unconfigured client ' + client_id

        def connections_response(request, session, form):
            if request.method == 'POST':
                if request.headers.get('origin') != self.config.private_base_url:
                    return Response(status_code=403, headers=HEADERS)
                grant_id = str(form.get('grant_id', ''))
                with self._db() as db:
                    if not self._consume_csrf(db, str(form.get('csrf', '')), session, 'revoke', grant_id):
                        return Response(status_code=403, headers=HEADERS)
                    changed = db.execute('UPDATE grants SET revoked=? WHERE id=? AND revoked IS NULL RETURNING client',
                        (time.time(), grant_id)).fetchone()
                return HTMLResponse('<h1>Access revoked</h1><p>' + html.escape(label(changed['client']))
                    + ' must reconnect to use this grant again.</p>' if changed else '<h1>Connection already unavailable</h1>', headers=HEADERS)
            with self._db() as db:
                grants = db.execute('SELECT * FROM grants ORDER BY created DESC LIMIT 100').fetchall()
                # Every connect and refresh issues a new access token, and tokens are never deleted,
                # so the newest one's issue time is when the app was last active (to within one
                # access-token lifetime). A grant with no token never finished connecting (ROA-29).
                newest = {row[0]: row[1] for row in db.execute(
                    "SELECT grant_id, MAX(expires) FROM tokens WHERE kind='access' GROUP BY grant_id")}
            body = '<h1>Gremlin Town connections</h1>'
            now = time.time()
            stamp = lambda moment: time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(moment))
            for grant in grants:
                deadline = self._grant_deadline(grant['created'])
                ends = stamp(deadline)
                live = not grant['revoked'] and deadline > now
                state = 'revoked' if grant['revoked'] else ('active until ' if live else 'expired ') + ends
                last = newest.get(grant['id'])
                used = ('last active ' + stamp(last - self.config.access_seconds)) if last else 'never finished connecting'
                body += ('<p>' + html.escape(label(grant['client'])) + ' — ' + state
                    + ' — approved ' + stamp(grant['created']) + ', ' + used
                    + ' — ' + html.escape(grant['scopes']) + '</p>')
                if live:
                    csrf = self._csrf(session, 'revoke', grant['id'])
                    body += '<form method="post"><input type="hidden" name="grant_id" value="' + grant['id'] + '"><input type="hidden" name="csrf" value="' + csrf + '"><button>Revoke access</button></form>'
            return HTMLResponse(body + ('<p>No connections.</p>' if not grants else ''), headers=HEADERS)

        routes = [Route('/mcp/consent-handoff', consent_handoff, methods=['GET']), Route('/mcp/connections', connections, methods=['GET', 'POST'])]
        for route in routes:
            route.app = RequestBodyLimitMiddleware(route.app, 16384)
        return routes
