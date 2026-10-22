from pathlib import Path

import pytest

from gremlin_town.runtime import (
    ConfigurationError,
    MCPClientSettings,
    MCPSettings,
    ServerSettings,
    _brain_mcp_settings,
)


def settings(tmp_path: Path):
    server = ServerSettings("127.0.0.1", 8765, tmp_path / "work.sqlite3", False, True, True, 1.0)
    mcp = MCPSettings(
        "127.0.0.1", 8769, "https://gt.example.test", "https://private.example.test", tmp_path / "oauth",
        (MCPClientSettings("chatgpt-web", "ChatGPT web", "chatgpt", tmp_path / "client-secret",
                           ("https://chatgpt.com/callback",), ("https://chatgpt.com",)),),
    )
    return server, mcp


def configured(**changes):
    brain = {
        "enabled": True,
        "connector_url": "http://127.0.0.1:9876",
        "connector_token_file": "secrets/connector",
        "github_repository": "owner/brain-home",
        "github_branch": "main",
        "github_token_file": "secrets/github",
        "policy_file": "brain-policy.json",
        "decision_staging_dir": "state/brain-decisions",
        "ownership_catalog_file": "ownership.yaml",
        "source_registry_file": "brain-sources.json",
    }
    brain.update(changes)
    return {"brain_core": brain}


def test_disabled_brain_configuration_requires_no_enrollment(tmp_path):
    server, _ = settings(tmp_path)
    assert _brain_mcp_settings({}, tmp_path, server, None) is None
    assert _brain_mcp_settings({"brain_core": {"enabled": False}}, tmp_path, server, None) is None


def test_enabled_configuration_is_trusted_and_resolves_paths(tmp_path):
    server, mcp = settings(tmp_path)
    value = _brain_mcp_settings(configured(), tmp_path, server, mcp)
    assert value.connector_url == "http://127.0.0.1:9876"
    assert value.github_repository == "owner/brain-home" and value.github_branch == "main"
    assert value.connector_token_file == (tmp_path / "secrets/connector").resolve()
    assert value.policy_file == (tmp_path / "brain-policy.json").resolve()
    assert value.source_registry_file == (tmp_path / "brain-sources.json").resolve()


def test_github_app_mode_replaces_the_token_file(tmp_path):
    server, mcp = settings(tmp_path)
    app = configured(github_app_id=5072789, github_installation_id=164780317,
                     github_app_private_key_file="secrets/brain-app.pem")
    del app["brain_core"]["github_token_file"]
    value = _brain_mcp_settings(app, tmp_path, server, mcp)
    assert value.github_token_file is None
    assert (value.github_app_id, value.github_installation_id) == (5072789, 164780317)
    assert value.github_app_private_key_file == (tmp_path / "secrets/brain-app.pem").resolve()
    assert _brain_mcp_settings(configured(), tmp_path, server, mcp).github_app_id is None


@pytest.mark.parametrize("changes", [
    {"github_app_id": 1, "github_installation_id": 2, "github_app_private_key_file": "key.pem"},
    {"github_token_file": None, "github_app_id": 1, "github_app_private_key_file": "key.pem"},
    {"github_token_file": None, "github_app_id": "1", "github_installation_id": 2,
     "github_app_private_key_file": "key.pem"},
    {"github_token_file": None, "github_app_id": 1, "github_installation_id": 0,
     "github_app_private_key_file": "key.pem"},
    {"github_token_file": None},
])
def test_github_credential_mode_must_be_exactly_one(tmp_path, changes):
    server, mcp = settings(tmp_path)
    value = configured(**changes)
    value["brain_core"] = {key: item for key, item in value["brain_core"].items() if item is not None}
    with pytest.raises(ConfigurationError):
        _brain_mcp_settings(value, tmp_path, server, mcp)


def test_https_dns_connector_origin_is_accepted(tmp_path):
    server, mcp = settings(tmp_path)
    value = _brain_mcp_settings(
        configured(connector_url="https://connector.example.test:10443"),
        tmp_path,
        server,
        mcp,
    )
    assert value.connector_url == "https://connector.example.test:10443"


@pytest.mark.parametrize("change,match", [
    ({"connector_url": "http://0.0.0.0:9876"}, "non-wildcard"),
    ({"connector_url": "http://100.64.0.1:9876"}, "require HTTPS"),
    ({"connector_url": "http://localhost:9876"}, "require HTTPS"),
    ({"connector_url": "https://*.example.test:9876"}, "valid exact DNS hostname"),
    ({"connector_url": "https://bad..example.test:9876"}, "valid exact DNS hostname"),
    ({"connector_url": "https://connector.example.test"}, "host and port"),
    ({"connector_url": "https://connector.example.test:65536"}, "valid origin and port"),
    ({"connector_url": "https://user@connector.example.test:9876"}, "without credentials"),
    ({"connector_url": "https://@connector.example.test:9876"}, "without credentials"),
    ({"connector_url": "https://connector.example.test:9876/path"}, "without credentials"),
    ({"connector_url": "https://connector.example.test:9876?query=yes"}, "without credentials"),
    ({"connector_url": "https://connector.example.test:9876#fragment"}, "without credentials"),
    ({"github_repository": "not-an-owner-repo"}, "owner/name"),
    ({"github_branch": "../main"}, "branch is invalid"),
])
def test_unsafe_or_ambiguous_enrollment_is_refused(tmp_path, change, match):
    server, mcp = settings(tmp_path)
    with pytest.raises(ConfigurationError, match=match):
        _brain_mcp_settings(configured(**change), tmp_path, server, mcp)


def test_enabled_brain_requires_the_existing_mcp_and_normal_mode(tmp_path):
    server, mcp = settings(tmp_path)
    with pytest.raises(ConfigurationError, match=r"requires \[mcp\]"):
        _brain_mcp_settings(configured(), tmp_path, server, None)
    recovery = ServerSettings(
        server.host, server.port, server.database, False, False, False, server.worker_poll_seconds, False,
    )
    with pytest.raises(ConfigurationError, match="disabled in recovery"):
        _brain_mcp_settings(configured(), tmp_path, recovery, mcp)


def test_orientation_brain_storage_is_none_without_brain_core():
    from gremlin_town.runtime import _orientation_brain_storage

    assert _orientation_brain_storage(None) is None


def test_orientation_brain_storage_builds_from_the_fixed_token_file(tmp_path):
    from gremlin_town.runtime import _orientation_brain_storage

    server, mcp = settings(tmp_path)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "github").write_text("fixture-github-token", encoding="utf-8")
    brain = _brain_mcp_settings(configured(), tmp_path, server, mcp)

    storage = _orientation_brain_storage(brain)

    assert storage is not None
    assert storage.repository == "owner/brain-home"
    assert storage.branch == "main"


@pytest.mark.parametrize("enabled", [False, True])
def test_archive_switch_reaches_both_runtime_storage_factories(tmp_path, enabled):
    from gremlin_town.runtime import _cached_brain_storage, _orientation_brain_storage
    server, mcp = settings(tmp_path)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "github").write_text("fixture-github-token", encoding="utf-8")
    brain = _brain_mcp_settings(configured(archive_superseded=enabled), tmp_path, server, mcp)
    assert brain.archive_superseded is enabled
    assert _orientation_brain_storage(brain).archive_superseded is enabled
    storage, copy = _cached_brain_storage(brain)
    try:
        assert storage.archive_superseded is enabled
    finally:
        if copy is not None:
            copy.close()


def test_archive_switch_defaults_false_and_refuses_non_boolean(tmp_path):
    server, mcp = settings(tmp_path)
    assert _brain_mcp_settings(configured(), tmp_path, server, mcp).archive_superseded is False
    with pytest.raises(ConfigurationError, match="archive_superseded"):
        _brain_mcp_settings(configured(archive_superseded="false"), tmp_path, server, mcp)
