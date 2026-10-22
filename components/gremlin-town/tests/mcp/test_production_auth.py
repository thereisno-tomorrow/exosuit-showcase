import asyncio
from dataclasses import replace
import json
import sqlite3
import threading
import time
from unittest.mock import AsyncMock

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
import pytest

from gremlin_town.contracts import Actor
from gremlin_town.domain.policy import Participant, Policy
from gremlin_town.mcp.auth import ProductionAuth
from gremlin_town.oauth import OAuthClient, OAuthConfig, OAuthProvider


CHATGPT = OAuthClient(client_id='local-test', participant='chatgpt-web', name='ChatGPT web',
    redirect_uris=('https://chatgpt.com/connector/callback',), client_secret='s' * 48)
CLAUDE = OAuthClient(client_id='claude-test', participant='claude-web', name='Claude',
    redirect_uris=('https://claude.ai/api/mcp/auth_callback',))


@pytest.fixture
def auth_setup(tmp_path):
    config = OAuthConfig(db_path=tmp_path / 'oauth.sqlite3', issuer_url='https://mcp.example',
        resource_url='https://mcp.example/mcp', private_base_url='https://private.example', clients=(CHATGPT, CLAUDE))
    policy = Policy([Participant(name, name, 'unused@example.test', frozenset({'test'}), frozenset({'public-test'}))
        for name in ('chatgpt-web', 'claude-web')])
    provider = OAuthProvider(config, participant_enabled=lambda _: True)
    enabled = [True]
    auth = ProductionAuth(provider, policy, enabled=lambda: enabled[0])

    def issue(scopes=('gt:read', 'gt:write'), client=CHATGPT):
        # Seed isolated fake grants; protocol/consent is tested in test_oauth.py.
        with provider._db() as db:
            grant = str(db.execute('SELECT COUNT(*) FROM grants').fetchone()[0])
            db.execute('INSERT INTO grants VALUES (?, ?, ?, ?, NULL)',
                (grant, client.client_id, json.dumps(scopes), time.time()))
            return provider._issue(db, grant, list(scopes))

    return auth, provider, policy, enabled, issue


def test_no_identity_until_authorized_and_no_fallback(auth_setup):
    auth, _, _, _, issue = auth_setup
    tokens = issue()

    async def run():
        access = await auth.verify_token(tokens.access_token)
        assert access.subject == 'chatgpt-web'
        for reader in (auth.actor, auth.scopes):
            with pytest.raises(PermissionError):
                reader()
        with pytest.raises(PermissionError):
            await auth.authorize()
        context = auth_context_var.set(AuthenticatedUser(access))
        try:
            await auth.authorize()
            assert auth.actor() == Actor('chatgpt-web')
            assert auth.scopes() == frozenset({'gt:read', 'gt:write'})
        finally:
            auth_context_var.reset(context)
        with pytest.raises(PermissionError):
            auth.actor()

    asyncio.run(run())


@pytest.mark.parametrize('change', ['disabled', 'missing', 'operator', 'runtime', 'revoke', 'expire'])
def test_permission_change_between_calls_rejects_and_clears_context(auth_setup, change):
    auth, provider, policy, enabled, issue = auth_setup
    tokens = issue()

    async def run():
        access = await auth.verify_token(tokens.access_token)
        context = auth_context_var.set(AuthenticatedUser(access))
        try:
            await auth.authorize()
            if change == 'disabled':
                policy.set_enabled('chatgpt-web', False)
            elif change == 'missing':
                policy._participants.clear()
            elif change == 'operator':
                policy._participants['chatgpt-web'] = replace(policy.participant('chatgpt-web'), is_operator=True)
            elif change == 'runtime':
                enabled[0] = False
            elif change == 'revoke':
                await provider.revoke_token(access)
            else:
                with sqlite3.connect(provider.config.db_path) as db:
                    db.execute("UPDATE tokens SET expires=0 WHERE kind='access'")
            # Reuse the SDK-authenticated request to model a long-lived session.
            with pytest.raises(PermissionError):
                await auth.authorize()
            assert await auth.verify_token(tokens.access_token) is None
            for reader in (auth.actor, auth.scopes):
                with pytest.raises(PermissionError):
                    reader()
        finally:
            auth_context_var.reset(context)

    asyncio.run(run())


@pytest.mark.parametrize('updates', [
    {'subject': 'fleet-boss-grok'}, {'subject': None}, {'client_id': 'other-client'},
    {'resource': 'https://other.example/mcp'}, {'resource': None},
    {'claims': {'iss': 'https://other.example'}}, {'claims': None},
    {'expires_at': 0}, {'expires_at': None}, {'scopes': ['admin']},
    {'scopes': []}, {'token': 'another-token'},
])
def test_wrong_token_bindings_fail_closed(auth_setup, updates):
    auth, provider, _, _, issue = auth_setup
    tokens = issue()

    async def run():
        access = await provider.verify_token(tokens.access_token)
        provider.verify_token = AsyncMock(return_value=access.model_copy(update=updates))
        assert await auth.verify_token(tokens.access_token) is None
        context = auth_context_var.set(AuthenticatedUser(access))
        try:
            with pytest.raises(PermissionError):
                await auth.authorize()
        finally:
            auth_context_var.reset(context)

    asyncio.run(run())


def test_refresh_token_and_unknown_bearer_are_not_access_tokens(auth_setup):
    auth, _, _, _, issue = auth_setup
    tokens = issue()

    async def run():
        for value in ('', 'chatgpt-web', 'not-a-token', tokens.refresh_token):
            assert await auth.verify_token(value) is None

    asyncio.run(run())


def test_concurrent_requests_keep_scopes_and_invalid_identity_separate(auth_setup):
    auth, provider, _, _, issue = auth_setup
    read = issue(('gt:read',))
    write = issue()

    async def run():
        read_access = await provider.verify_token(read.access_token)
        write_access = await provider.verify_token(write.access_token)
        invalid = read_access.model_copy(update={'token': 'unknown'})
        arrived = 0
        gate = asyncio.Event()

        async def call(access, expected):
            nonlocal arrived
            context = auth_context_var.set(AuthenticatedUser(access))
            try:
                if expected is None:
                    with pytest.raises(PermissionError):
                        await auth.authorize()
                else:
                    await auth.authorize()
                arrived += 1
                if arrived == 3:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=3)
                if expected is None:
                    with pytest.raises(PermissionError):
                        auth.actor()
                else:
                    assert auth.actor() == Actor('chatgpt-web')
                    assert auth.scopes() == expected
            finally:
                auth_context_var.reset(context)

        await asyncio.gather(call(read_access, {'gt:read'}),
            call(write_access, {'gt:read', 'gt:write'}), call(invalid, None))
        with pytest.raises(PermissionError):
            auth.actor()

    asyncio.run(run())


def test_authorize_uses_fresh_scopes_not_sdk_cached_claims(auth_setup):
    auth, provider, _, _, issue = auth_setup
    tokens = issue(('gt:read',))

    async def run():
        access = await provider.verify_token(tokens.access_token)
        access.scopes = ['gt:read', 'gt:write']
        context = auth_context_var.set(AuthenticatedUser(access))
        try:
            await auth.authorize()
            assert auth.scopes() == {'gt:read'}
        finally:
            auth_context_var.reset(context)

    asyncio.run(run())


def test_normal_restart_preserves_refresh_and_reconnect_after_revoke(auth_setup):
    auth, provider, policy, _, issue = auth_setup
    original = issue()
    restarted = OAuthProvider(provider.config, participant_enabled=lambda _: True)
    restarted_auth = ProductionAuth(restarted, policy)

    async def run():
        assert await restarted_auth.verify_token(original.access_token)
        client = await restarted.get_client(restarted.config.clients[0].client_id)
        refresh = await restarted.load_refresh_token(client, original.refresh_token)
        rotated = await restarted.exchange_refresh_token(client, refresh, ['gt:read'])
        assert await auth.verify_token(original.access_token) is None
        access = await auth.verify_token(rotated.access_token)
        assert access.scopes == ['gt:read']
        await restarted.revoke_token(access)
        assert await auth.verify_token(rotated.access_token) is None
        assert await restarted.load_refresh_token(client, rotated.refresh_token) is None
        replacement = issue()
        assert await restarted_auth.verify_token(replacement.access_token)

    asyncio.run(run())


def test_runtime_disable_during_verification_fails_closed(auth_setup):
    auth, provider, _, enabled, issue = auth_setup
    tokens = issue()
    verify = provider.verify_token

    async def disable(token):
        result = await verify(token)
        enabled[0] = False
        return result

    provider.verify_token = disable
    assert asyncio.run(auth.verify_token(tokens.access_token)) is None


def test_sqlite_verification_does_not_block_event_loop(auth_setup, monkeypatch):
    auth, provider, _, _, issue = auth_setup
    tokens = issue()
    entered, release = threading.Event(), threading.Event()
    original = provider._load_access_token

    def blocked(token):
        entered.set()
        if not release.wait(timeout=3):
            raise AssertionError('event loop was blocked by token storage')
        return original(token)

    monkeypatch.setattr(provider, '_load_access_token', blocked)

    async def run():
        pending = asyncio.create_task(auth.verify_token(tokens.access_token))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            release.set()
            assert await pending
        finally:
            release.set()
            await pending

    asyncio.run(run())


def test_storage_errors_surface_and_clear_old_context(auth_setup, monkeypatch):
    auth, provider, _, _, issue = auth_setup
    tokens = issue()

    async def run():
        access = await provider.verify_token(tokens.access_token)
        context = auth_context_var.set(AuthenticatedUser(access))
        try:
            await auth.authorize()
            monkeypatch.setattr(provider, 'verify_token', AsyncMock(side_effect=sqlite3.OperationalError('locked')))
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                await auth.authorize()
            with pytest.raises(PermissionError):
                auth.actor()
        finally:
            auth_context_var.reset(context)

    asyncio.run(run())


def test_identity_comes_only_from_the_token_client(auth_setup):
    auth, provider, policy, _, issue = auth_setup
    claude = issue(client=CLAUDE)

    async def run():
        access = await auth.verify_token(claude.access_token)
        assert (access.client_id, access.subject) == ('claude-test', 'claude-web')
        context = auth_context_var.set(AuthenticatedUser(access))
        try:
            await auth.authorize()
            assert auth.actor() == Actor('claude-web')
        finally:
            auth_context_var.reset(context)
        # Neither app's token can be presented as the other participant.
        for forged in (access.model_copy(update={'subject': 'chatgpt-web'}),
                       access.model_copy(update={'client_id': CHATGPT.client_id})):
            provider.verify_token = AsyncMock(return_value=forged)
            assert await auth.verify_token(claude.access_token) is None

    asyncio.run(run())
    with pytest.raises(TypeError):
        ProductionAuth(provider, policy, actor_id='operator')


def test_each_participant_is_checked_separately(auth_setup):
    auth, _, policy, _, issue = auth_setup
    chatgpt, claude = issue(), issue(client=CLAUDE)

    async def run():
        policy.set_enabled('claude-web', False)
        assert await auth.verify_token(claude.access_token) is None
        assert (await auth.verify_token(chatgpt.access_token)).subject == 'chatgpt-web'
        policy._participants['chatgpt-web'] = replace(policy.participant('chatgpt-web'), is_operator=True)
        assert await auth.verify_token(chatgpt.access_token) is None

    asyncio.run(run())
