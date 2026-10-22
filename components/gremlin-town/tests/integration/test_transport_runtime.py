import pytest
from fastapi.testclient import TestClient

from gremlin_town.runtime import build_runtime, ConfigurationError
from gremlin_town.transport import BoundTransport, FakeTransport
from test_runtime_flow import _write_runtime_config, _packet, _headers
# Load the shared HTTP fixture by file so this test also runs on its own.
import importlib.util
from pathlib import Path
_fixture_spec = importlib.util.spec_from_file_location(
    "github_transport_fixture", Path(__file__).parents[1] / "test_github_transport.py")
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
github_backend = _fixture_module.github_backend


def config_for(tmp_path, monkeypatch, default='github'):
    path, _ = _write_runtime_config(tmp_path)
    secret = tmp_path / 'provider-secret'
    secret.write_text('fixture-only')
    monkeypatch.setenv('GT_TEST_GITHUB_SECRET', str(secret))
    monkeypatch.setenv('GT_TEST_MAIL_SECRET', str(secret))
    with path.open('a') as out:
        out.write(f'''
[transport]
default_backend="{default}"
[github]
owner="test-owner"
repository="mailbox"
writer_user_id=42
api_key_file_env="GT_TEST_GITHUB_SECRET"
[agentmail]
inbox_id="legacy-inbox"
api_key_file_env="GT_TEST_MAIL_SECRET"
''')
    return path


def test_runtime_builds_both_providers_without_network(tmp_path, monkeypatch):
    path = config_for(tmp_path, monkeypatch)
    created = {}
    def factory(name):
        def make(config):
            created[name] = config
            return FakeTransport()
        return make
    monkeypatch.setattr('gremlin_town.runtime.GitHubTransport', factory('github'))
    monkeypatch.setattr('gremlin_town.runtime.AgentMailTransport', factory('agentmail'))
    runtime = build_runtime(path, start_worker=False)
    try:
        assert runtime.service.default_backend == 'github:test-owner/mailbox'
        assert isinstance(runtime.service.provider, BoundTransport)
        assert set(runtime.service.provider.providers) == {'agentmail', 'github:test-owner/mailbox'}
        assert created['github'].writer_user_id == 42
        assert created['agentmail'].inbox_id == 'legacy-inbox'
    finally:
        runtime.service.store.close()


def test_authenticated_runtime_http_uses_configured_github_binding(tmp_path, monkeypatch, github_backend):
    fixture_service, _, _, state = github_backend
    path = config_for(tmp_path, monkeypatch)
    monkeypatch.setattr('gremlin_town.runtime.GitHubTransport', lambda config: fixture_service.provider)
    legacy = FakeTransport()
    monkeypatch.setattr('gremlin_town.runtime.AgentMailTransport', lambda config: legacy)
    runtime = build_runtime(path, start_worker=False)
    payload = {'client_operation_id': 'configured-http', 'mode': 'action_requested',
               'project': 'demo', 'recipient': 'bob', 'packet': _packet('request', 'bounded task')}
    try:
        with TestClient(runtime.app) as client:
            denied = client.post('/v1/requests', headers=_headers('invalid'), json=payload)
            assert denied.status_code == 401 and state['calls'] == []
            headers = _headers('alice-token-with-more-than-thirty-two-bytes')
            response = client.post('/v1/requests', headers=headers, json=payload)
            assert response.status_code == 200
            receipt = response.json()
            operation = runtime.service.store.fetchone(
                'SELECT backend_binding, operation_state FROM operations WHERE operation_id=?',
                (receipt['operation_id'],),
            )
            assert tuple(operation) == ('github:test-owner/mailbox', 'queued')
            assert state['calls'] == []
            runtime.worker.dispatch_once()
            read = client.get('/v1/requests/' + receipt['request_id'], headers=headers)
            assert read.status_code == 200
            assert read.json()['content'][0]['exact_link_verified'] is True
            assert read.json()['content'][0]['attributed_actor_id'] == 'alice'
            assert legacy.send_attempts == 0
    finally:
        runtime.service.store.close()


@pytest.mark.parametrize('edit', [
    ('writer_user_id=42', 'writer_user_id=true'),
    ('default_backend="github"', 'default_backend="unknown"'),
    ('owner="test-owner"', 'owner="../elsewhere"'),
    ('api_key_file_env="GT_TEST_GITHUB_SECRET"', 'api_key_file_env="GT_MISSING_SECRET"'),
])
def test_invalid_provider_config_refuses_before_database(tmp_path, monkeypatch, edit):
    path = config_for(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace(*edit))
    with pytest.raises(ConfigurationError):
        build_runtime(path, start_worker=False)
    assert not (tmp_path / 'gremlin.sqlite3').exists()


APP_SETTINGS = 'app_id=11\ninstallation_id=22\napp_private_key_file_env="GT_TEST_GITHUB_APP_KEY"'


def app_config_for(tmp_path, monkeypatch, key_text=None, settings=APP_SETTINGS):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    path = config_for(tmp_path, monkeypatch)
    if key_text is None:
        key_text = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()).decode()
    key = tmp_path / 'github-app.pem'
    key.write_text(key_text)
    monkeypatch.setenv('GT_TEST_GITHUB_APP_KEY', str(key))
    path.write_text(path.read_text().replace('api_key_file_env="GT_TEST_GITHUB_SECRET"', settings))
    return path


def test_runtime_builds_github_app_transport_without_minting(tmp_path, monkeypatch):
    path = app_config_for(tmp_path, monkeypatch, settings=APP_SETTINGS + '\nlegacy_writer_user_ids=[7]')
    created = {}
    def github(config):
        created['github'] = config
        return FakeTransport()
    monkeypatch.setattr('gremlin_town.runtime.GitHubTransport', github)
    monkeypatch.setattr('gremlin_town.runtime.AgentMailTransport', lambda config: FakeTransport())
    minted = []
    monkeypatch.setattr('gremlin_town.github_app.GitHubAppTokenProvider._mint',
                        lambda self, now: minted.append(now))
    runtime = build_runtime(path, start_worker=False)
    try:
        config = created['github']
        assert config.token == '' and config.token_provider.repository == 'test-owner/mailbox'
        assert config.writer_user_id == 42 and config.legacy_writer_user_ids == (7,)
        assert minted == []
    finally:
        runtime.service.store.close()


@pytest.mark.parametrize(('settings', 'key_text'), [
    (APP_SETTINGS + '\napi_key_file_env="GT_TEST_GITHUB_SECRET"', None),
    ('app_id=11\napp_private_key_file_env="GT_TEST_GITHUB_APP_KEY"', None),
    (APP_SETTINGS.replace('app_id=11', 'app_id="11"'), None),
    (APP_SETTINGS.replace('GT_TEST_GITHUB_APP_KEY', 'GT_MISSING_APP_KEY'), None),
    (APP_SETTINGS, 'not a private key'),
    (APP_SETTINGS + '\nlegacy_writer_user_ids=[42]', None),
    (APP_SETTINGS + '\nlegacy_writer_user_ids=[0]', None),
])
def test_invalid_github_app_config_refuses_before_database(tmp_path, monkeypatch, settings, key_text):
    path = app_config_for(tmp_path, monkeypatch, key_text=key_text, settings=settings)
    with pytest.raises(ConfigurationError):
        build_runtime(path, start_worker=False)
    assert not (tmp_path / 'gremlin.sqlite3').exists()
