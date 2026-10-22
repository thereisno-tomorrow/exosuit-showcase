from __future__ import annotations

import hashlib
import importlib.util
import io
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gremlin_town.recovery import restore_snapshot
from gremlin_town.runtime import ConfigurationError, build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_runtime_flow import _headers, _write_runtime_config
from tests.recovery.test_recovery import _make_snapshot


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("validate_backup", ROOT / "deploy/validate-backup.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def _checksums(directory: Path) -> None:
    (directory / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(validator.HASHED_FILES)
    ), encoding="ascii")


def _backup(directory: Path) -> Path:
    directory.mkdir()
    _write_runtime_config(directory)
    _make_snapshot(directory / "gremlin-town.sqlite3")
    (directory / "MANIFEST").write_text("created_utc=2026-09-21T00:00:00Z\n", encoding="ascii")
    _checksums(directory)
    return directory


def _archive(directory: Path, destination: Path, *, extra: tarfile.TarInfo | None = None,
             replace: tarfile.TarInfo | None = None) -> Path:
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(validator.FILES):
            if replace is not None and name == replace.name:
                archive.addfile(replace)
            else:
                archive.add(directory / name, arcname=name)
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b""))
    return destination


def test_valid_work_archive_roundtrip_is_create_only_and_preserves_source(tmp_path: Path) -> None:
    directory = _backup(tmp_path / "backup")
    archive = _archive(directory, tmp_path / "work.tar")
    before = archive.read_bytes()
    target = tmp_path / "restore"
    validator.extract_archive(archive, target)
    assert {path.name for path in target.iterdir()} == validator.FILES
    for name in validator.FILES:
        assert (target / name).read_bytes() == (directory / name).read_bytes()
    with pytest.raises(ValueError, match="must not exist"):
        validator.extract_archive(archive, target)
    assert archive.read_bytes() == before


@pytest.mark.parametrize("name", [
    "oauth.sqlite3", "oauth.sqlite3-wal", "session.json", "client-secret", "keys.pem",
    "../escaped", "/absolute", "nested/oauth.sqlite3", "config.toml", "./MANIFEST", "..\\escaped",
])
def test_unlisted_duplicate_and_traversal_members_refused_before_writes(tmp_path: Path, name: str) -> None:
    directory = _backup(tmp_path / "backup")
    archive = _archive(directory, tmp_path / "work.tar", extra=tarfile.TarInfo(name))
    target = tmp_path / "restore"
    before = {path.name for path in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="five allowed"):
        validator.extract_archive(archive, target)
    assert {path.name for path in tmp_path.iterdir()} == before
    assert not target.exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_allowed_name_cannot_be_link_or_special_file(tmp_path: Path, kind: bytes) -> None:
    directory = _backup(tmp_path / "backup")
    member = tarfile.TarInfo("config.toml")
    member.type = kind
    member.linkname = "../outside"
    archive = _archive(directory, tmp_path / "work.tar", replace=member)
    with pytest.raises(ValueError, match="special files"):
        validator.extract_archive(archive, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


@pytest.mark.parametrize("mutation", ["checksum", "missing-hash", "inline-secret", "participant-token", "session-table", "oauth-db", "session-column", "trigger"])
def test_invalid_content_never_creates_restore_target(tmp_path: Path, mutation: str) -> None:
    directory = _backup(tmp_path / "backup")
    if mutation == "checksum":
        (directory / "MANIFEST").write_text("changed", encoding="ascii")
    elif mutation == "missing-hash":
        (directory / "SHA256SUMS").write_text("", encoding="ascii")
    else:
        if mutation == "inline-secret":
            with (directory / "config.toml").open("a", encoding="ascii") as handle:
                handle.write('\n[agentmail]\napi_key = "fake-secret"\n')
        elif mutation == "participant-token":
            path = directory / "participants.toml"
            path.write_text(path.read_text().replace("token_sha256 =", "token ="))
        else:
            with closing(sqlite3.connect(directory / "gremlin-town.sqlite3")) as db:
                if mutation == "session-table":
                    db.execute("CREATE TABLE sessions(token TEXT)")
                elif mutation == "session-column":
                    db.execute("ALTER TABLE requests ADD COLUMN access_token TEXT")
                elif mutation == "trigger":
                    db.execute("CREATE TRIGGER injected AFTER INSERT ON requests BEGIN SELECT 1; END")
                else:
                    db.execute("DROP TABLE requests")
                    db.execute("CREATE TABLE tokens(hash TEXT)")
        _checksums(directory)
    archive = _archive(directory, tmp_path / "work.tar")
    with pytest.raises(ValueError):
        validator.extract_archive(archive, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()
    assert not tuple(tmp_path.glob(".gt-validate-*"))


def _recovery_config(root: Path, database: Path, oauth_dir: Path, *, enabled: bool,
                     operator_only: bool = True, clients: bool = False,
                     dispatch: bool = False, mcp_allowed: bool = False) -> tuple[Path, dict[str, str]]:
    root.mkdir()
    path, tokens = _write_runtime_config(root)
    content = path.read_text().replace((root / "gremlin.sqlite3").as_posix(), database.as_posix())
    content = content.replace("operator_only = false", f"operator_only = {str(operator_only).lower()}")
    content = content.replace("normal_clients_allowed = true", f"normal_clients_allowed = {str(clients).lower()}")
    content = content.replace("outbound_dispatch_allowed = false", f"outbound_dispatch_allowed = {str(dispatch).lower()}")
    content += f'''\nmcp_allowed = {str(mcp_allowed).lower()}
[mcp]
enabled = {str(enabled).lower()}
host = "127.0.0.1"
port = 8769
public_url = "https://gt.example.test"
private_url = "https://private.example.test"
state_dir = "{oauth_dir.as_posix()}"
client_id = "fake-client"
client_secret_file = "{(root / 'missing-secret').as_posix()}"
redirect_uris = ["https://chatgpt.com/connector_platform_oauth_redirect"]
'''
    path.write_text(content, encoding="utf-8")
    return path, tokens


@pytest.mark.parametrize("gate", ["operator", "clients", "dispatch", "mcp"])
def test_copied_live_oauth_path_refused_before_provider_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str) -> None:
    # A disposable sentinel represents a live OAuth DB. Its bytes and directory
    # inventory must survive refusal unchanged; no installed state is accessed.
    oauth_dir = tmp_path / "live-oauth"
    oauth_dir.mkdir()
    sentinel = oauth_dir / "oauth.sqlite3"
    sentinel.write_bytes(b"disposable-live-state")
    config, _ = _recovery_config(tmp_path / "recovery", tmp_path / "work.sqlite3", oauth_dir,
        enabled=True, operator_only=gate == "operator", clients=gate != "clients",
        dispatch=gate != "dispatch", mcp_allowed=gate != "mcp")
    def forbidden(*args, **kwargs):
        pytest.fail("recovery constructed an OAuth provider")
    monkeypatch.setattr("gremlin_town.oauth.OAuthProvider", forbidden)
    monkeypatch.setattr("gremlin_town.runtime.SQLiteStore", forbidden)
    monkeypatch.setattr("gremlin_town.runtime._read_provider_secret", forbidden)
    with pytest.raises(ConfigurationError, match="MCP must be disabled in recovery mode"):
        build_runtime(config, provider=FakeTransport())
    assert sentinel.read_bytes() == b"disposable-live-state"
    assert {path.name for path in oauth_dir.iterdir()} == {"oauth.sqlite3"}
    assert not (tmp_path / "work.sqlite3").exists()


@pytest.mark.parametrize("copied_live_path", [False, True])
def test_isolated_restore_has_holds_no_sender_and_no_mcp(tmp_path: Path, copied_live_path: bool) -> None:
    source = tmp_path / "snapshot.sqlite3"
    _make_snapshot(source)
    destination = tmp_path / "recovery.sqlite3"
    restored = restore_snapshot(source, destination)
    assert restored.request_holds_applied == 3
    assert restored.operation_holds_applied == 3
    oauth_dir = tmp_path / "must-not-be-created"
    if copied_live_path:
        oauth_dir.mkdir()
        (oauth_dir / "oauth.sqlite3").write_bytes(b"disposable-live-state")
    config, tokens = _recovery_config(tmp_path / "config", destination, oauth_dir, enabled=False)
    provider = FakeTransport()
    runtime = build_runtime(config, provider=provider)  # Exercise default worker startup.
    with TestClient(runtime.app) as client:
        assert runtime.worker_task is None
        for name in ("mcp", "mcp_app", "mcp_server", "oauth_provider", "mcp_auth"):
            assert getattr(runtime, name) is None
        assert client.get("/v1/requests/req-lost-cancel", headers=_headers(tokens["alice"])).status_code == 401
        response = client.get("/v1/requests/req-lost-cancel", headers=_headers(tokens["operator"]))
        assert response.status_code == 200
        assert response.json()["recovery_hold"] is True
        assert runtime.store.request("req-terminal").work_state.value == "cancelled"
        assert provider.sent_payloads == {}
        assert client.get("/mcp/setup").status_code == 404
    if copied_live_path:
        assert {path.name for path in oauth_dir.iterdir()} == {"oauth.sqlite3"}
        assert (oauth_dir / "oauth.sqlite3").read_bytes() == b"disposable-live-state"
    else:
        assert not oauth_dir.exists()


def test_recovery_example_explicitly_disables_all_delivery_surfaces() -> None:
    import tomllib
    config = tomllib.loads((ROOT / "deploy/config.recovery.toml.example").read_text())
    assert config["server"]["operator_only"] is True
    assert config["server"]["port"] == 8770
    assert config["recovery"] == {
        "normal_clients_allowed": False, "outbound_dispatch_allowed": False, "mcp_allowed": False,
    }
    assert config["mcp"] == {"enabled": False}
    assert "public_gateway" not in config
