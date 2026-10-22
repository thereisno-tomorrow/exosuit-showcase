from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gremlin_town.runtime import ConfigurationError, _load_participants, build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_runtime_flow import _write_runtime_config


def mcp_config(root: Path) -> tuple[Path, dict[str, str]]:
    config, tokens = _write_runtime_config(root)
    participants = root / "participants.toml"
    with participants.open("a", encoding="utf-8") as handle:
        handle.write('''
[[participant]]
id = "chatgpt-web"
address = "fake@example.invalid"
auth_mode = "oauth"
projects = ["demo"]
sensitivities = ["public-test"]
[[route]]
project = "demo"
source = "chatgpt-web"
destination = "bob"
sensitivity = "public-test"
''')
    secret = root / "client.secret"
    secret.write_text("isolated-client-secret-with-at-least-32-characters", encoding="utf-8")
    text = config.read_text(encoding="utf-8").replace("outbound_dispatch_allowed = false", "outbound_dispatch_allowed = true")
    config.write_text(text + f'''
[mcp]
enabled = true
host = "127.0.0.1"
port = 8769
public_url = "https://gt.example.test"
private_url = "https://private.example.test"
state_dir = "{(root / 'oauth-state').as_posix()}"
client_id = "isolated-chatgpt-client"
client_secret_file = "{secret.as_posix()}"
redirect_uris = ["https://chatgpt.com/connector_platform_oauth_redirect"]
''', encoding="utf-8")
    return config, tokens


def test_oauth_only_enrollment_keeps_bearer_clients(tmp_path):
    config, tokens = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.token_resolver(tokens["alice"]).actor_id == "alice"
        assert runtime.token_resolver("chatgpt-web" * 4) is None
        assert runtime.policy.participant("chatgpt-web").is_operator is False
        policy, secrets = _load_participants(tmp_path / "participants.toml")
        assert {item.actor_id for item in secrets} == set(tokens)
        assert policy.participant("chatgpt-web").enabled
    finally:
        runtime.close()


@pytest.mark.parametrize("amendment", [
    'auth_mode = "invalid"',
    'auth_mode = "oauth"\nrole = "operator"',
    'auth_mode = "oauth"\ntoken_sha256 = "' + "a" * 64 + '"',
    'auth_mode = "bearer"',
])
def test_invalid_enrollment_is_rejected(tmp_path, amendment):
    mcp_config(tmp_path)
    path = tmp_path / "participants.toml"
    path.write_text(path.read_text().replace('auth_mode = "oauth"', amendment))
    with pytest.raises(ConfigurationError):
        _load_participants(path)


@pytest.mark.parametrize("before,after", [
    ("operator_only = false", "operator_only = true"),
    ("normal_clients_allowed = true", "normal_clients_allowed = false"),
    ("outbound_dispatch_allowed = true", "outbound_dispatch_allowed = false"),
    ("[recovery]", "[recovery]\nmcp_allowed = false"),
    ("port = 8769", "port = 8765"),
    ("port = 8769", "port = true"),
    ("port = 8769", "port = 8769.5"),
    ("port = 8769", 'port = "8769"'),
    ('host = "127.0.0.1"\nport = 8769', 'host = "0.0.0.0"\nport = 8769'),
    ('https://gt.example.test', 'http://gt.example.test'),
    ('https://gt.example.test', 'https://private.example.test'),
])
def test_invalid_mcp_configuration_opens_no_oauth_store(tmp_path, before, after):
    config, _ = mcp_config(tmp_path)
    config.write_text(config.read_text().replace(before, after))
    with pytest.raises(ConfigurationError):
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    assert not (tmp_path / "oauth-state").exists()


def test_connection_age_limit_defaults_to_90_days_and_is_configurable(tmp_path):
    from gremlin_town.oauth import OAuthConfig
    from gremlin_town.runtime import MCP_MAX_GRANT_DAYS

    config, _ = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.mcp.max_grant_days == MCP_MAX_GRANT_DAYS == 90
        assert runtime.oauth_provider.config.max_grant_seconds == 90 * 86400
        assert OAuthConfig.__dataclass_fields__["max_grant_seconds"].default == MCP_MAX_GRANT_DAYS * 86400
    finally:
        runtime.close()
    config.write_text(config.read_text().replace("port = 8769", "port = 8769\nmax_grant_days = 30"))
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.oauth_provider.config.max_grant_seconds == 30 * 86400
    finally:
        runtime.close()


@pytest.mark.parametrize("value", ["0", "366", "-1", '"90"', "90.5", "true"])
def test_invalid_connection_age_limit_opens_no_oauth_store(tmp_path, value):
    config, _ = mcp_config(tmp_path)
    config.write_text(config.read_text().replace("port = 8769", f"port = 8769\nmax_grant_days = {value}"))
    with pytest.raises(ConfigurationError, match="max_grant_days"):
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    assert not (tmp_path / "oauth-state").exists()


def test_disabled_mcp_does_not_open_secret_or_state(tmp_path):
    config, _ = mcp_config(tmp_path)
    config.write_text(config.read_text().replace("enabled = true", "enabled = false").replace("client.secret", "missing.secret"))
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    with TestClient(runtime.app) as client:
        assert runtime.mcp is runtime.mcp_app is runtime.oauth_provider is None
        assert client.get("/health/live").status_code == 200
        assert client.get("/mcp/setup").status_code == 404
    assert not (tmp_path / "oauth-state").exists()


def test_one_worker_and_one_mcp_lifecycle(tmp_path):
    config, _ = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport())
    with TestClient(runtime.app) as client:
        assert runtime.worker_task is not None
        task = runtime.worker_task
        assert task.get_name() == "gremlin-town-outbox"
        assert not task.done()
        assert client.get("/health/ready").status_code == 200
        assert runtime.worker_task is task
        assert runtime.mcp_server is not None
    assert task.done()


def test_runtime_permission_gate_rechecks_current_settings(tmp_path):
    config, _ = two_client_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.oauth_provider.participant_enabled("chatgpt-web")
        assert runtime.oauth_provider.participant_enabled("claude-web")
        runtime.policy.set_enabled("claude-web", False)
        assert not runtime.oauth_provider.participant_enabled("claude-web")
        assert runtime.oauth_provider.participant_enabled("chatgpt-web")
        runtime.settings = replace(runtime.settings, mcp_allowed=False)
        assert not runtime.oauth_provider.participant_enabled("chatgpt-web")
        assert not runtime.mcp_auth.enabled()
    finally:
        runtime.close()


TWO_CLIENTS = '''[[mcp.client]]
participant = "chatgpt-web"
name = "ChatGPT web"
client_id = "isolated-chatgpt-client"
client_secret_file = "{secret}"
redirect_uris = ["https://chatgpt.com/connector_platform_oauth_redirect"]
origins = ["https://chatgpt.com"]

[[mcp.client]]
participant = "claude-web"
name = "Claude"
client_id = "isolated-claude-client"
redirect_uris = ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"]
origins = ["https://claude.ai", "https://claude.com"]
'''
CLAUDE_ENROLLMENT = '''
[[participant]]
id = "claude-web"
address = "fake@example.invalid"
auth_mode = "oauth"
projects = ["demo"]
sensitivities = ["public-test"]
[[route]]
project = "demo"
source = "claude-web"
destination = "bob"
sensitivity = "public-test"
'''


def two_client_config(root: Path) -> tuple[Path, dict[str, str]]:
    """mcp_config, with ChatGPT and Claude as separate [[mcp.client]] entries."""
    config, tokens = mcp_config(root)
    with (root / "participants.toml").open("a", encoding="utf-8") as handle:
        handle.write(CLAUDE_ENROLLMENT)
    text = config.read_text(encoding="utf-8")
    single = text[text.index('client_id = "isolated-chatgpt-client"'):]
    config.write_text(text.replace(single, TWO_CLIENTS.format(secret=(root / "client.secret").as_posix())), encoding="utf-8")
    return config, tokens


def test_single_client_keys_still_mean_the_chatgpt_client(tmp_path):
    config, _ = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        (client,) = runtime.oauth_provider.config.clients
        assert (client.participant, client.name, client.client_id) == ("chatgpt-web", "ChatGPT web", "isolated-chatgpt-client")
        assert client.origins == ("https://chatgpt.com",)
        assert client.client_secret == "isolated-client-secret-with-at-least-32-characters"
    finally:
        runtime.close()


def test_each_configured_client_is_its_own_enrolled_participant(tmp_path):
    config, _ = two_client_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        chatgpt, claude = runtime.oauth_provider.config.clients
        assert (chatgpt.participant, claude.participant) == ("chatgpt-web", "claude-web")
        assert chatgpt.client_secret and claude.client_secret is None
        assert claude.redirect_uris == ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
        assert runtime.oauth_provider.config.client_origins == ("https://chatgpt.com", "https://claude.ai", "https://claude.com")
        assert [client.participant for client in runtime.mcp.clients] == ["chatgpt-web", "claude-web"]
    finally:
        runtime.close()


@pytest.mark.parametrize("file,before,after", [
    ("config.toml", "port = 8769", 'port = 8769\nredirect_uris = ["https://chatgpt.com/x"]'),
    ("config.toml", 'participant = "claude-web"', 'participant = "chatgpt-web"'),
    ("config.toml", 'client_id = "isolated-claude-client"', 'client_id = "isolated-chatgpt-client"'),
    ("config.toml", 'name = "Claude"', 'name = "Claude"\nscope = "all"'),
    ("config.toml", 'name = "Claude"', 'name = ""'),
    ("config.toml", 'origins = ["https://claude.ai", "https://claude.com"]', 'origins = "https://claude.ai"'),
    ("config.toml", 'origins = ["https://claude.ai", "https://claude.com"]', 'origins = ["https://claude.ai/path"]'),
    ("config.toml", '"https://claude.ai/api/mcp/auth_callback", ', '"http://claude.ai/api/mcp/auth_callback", '),
    ("participants.toml", 'id = "claude-web"', 'id = "claude-other"'),
    ("participants.toml", 'id = "claude-web"\naddress = "fake@example.invalid"\nauth_mode = "oauth"',
     'id = "claude-web"\naddress = "fake@example.invalid"\nauth_mode = "bearer"\ntoken_sha256 = "' + "c" * 64 + '"'),
])
def test_invalid_client_configuration_opens_no_oauth_store(tmp_path, file, before, after):
    two_client_config(tmp_path)
    path = tmp_path / file
    text = path.read_text(encoding="utf-8")
    assert before in text
    path.write_text(text.replace(before, after).replace('source = "claude-web"', 'source = "claude-other"')
                    if after == 'id = "claude-other"' else text.replace(before, after), encoding="utf-8")
    with pytest.raises((ConfigurationError, ValueError)):
        build_runtime(tmp_path / "config.toml", provider=FakeTransport(), start_worker=False)
    assert not (tmp_path / "oauth-state").exists()


def test_participant_refresh_keeps_every_oauth_client_oauth_only(tmp_path):
    config, _ = two_client_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        path = tmp_path / "participants.toml"
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace('id = "claude-web"\naddress = "fake@example.invalid"\nauth_mode = "oauth"',
            'id = "claude-web"\naddress = "fake@example.invalid"\nauth_mode = "bearer"\ntoken_sha256 = "' + "c" * 64 + '"'),
            encoding="utf-8")
        with pytest.raises(ConfigurationError, match="claude-web"):
            runtime.refresh_participants()
        assert runtime.oauth_provider.participant_enabled("claude-web")
    finally:
        runtime.close()


def test_github_app_transport_and_two_mcp_clients_build_together(tmp_path, monkeypatch):
    """The GitHub App release and the Claude client merged separately; one config must carry both."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    config, _ = two_client_config(tmp_path)
    key = tmp_path / "github-app.pem"
    key.write_bytes(rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    monkeypatch.setenv("GT_TEST_GITHUB_APP_KEY", str(key))
    with config.open("a", encoding="utf-8") as handle:
        handle.write('''
[transport]
default_backend = "github"
[github]
owner = "test-owner"
repository = "mailbox"
writer_user_id = 42
app_id = 11
installation_id = 22
app_private_key_file_env = "GT_TEST_GITHUB_APP_KEY"
legacy_writer_user_ids = [7]
''')
    created = {}
    monkeypatch.setattr("gremlin_town.runtime.GitHubTransport", lambda value: created.setdefault("github", value) and FakeTransport())
    runtime = build_runtime(config, provider=None, start_worker=False)
    try:
        github = created["github"]
        assert github.token == "" and github.token_provider.repository == "test-owner/mailbox"
        assert github.legacy_writer_user_ids == (7,)
        assert [client.participant for client in runtime.oauth_provider.config.clients] == ["chatgpt-web", "claude-web"]
    finally:
        runtime.close()
