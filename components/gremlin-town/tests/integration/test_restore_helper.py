from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from gremlin_town.storage import SQLiteStore


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy" / "restore.sh"
VALIDATOR = ROOT / "deploy" / "validate-backup.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("restore_helper_validator", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(tmp_path: Path) -> Path:
    snapshot_dir = tmp_path / "snapshot with # and %"
    snapshot_dir.mkdir()
    database = snapshot_dir / "gremlin-town.sqlite3"
    store = SQLiteStore(database)
    try:
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with store.transaction() as connection:
            connection.execute("INSERT INTO sync_state(name, cursor) VALUES ('evidence', 'preserve')")
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()

    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()
    (snapshot_dir / "MANIFEST").write_text("created_utc=2026-09-21T00:00:00Z\n", encoding="ascii")
    (snapshot_dir / "config.toml").write_text(
        '[server]\ndatabase = "gremlin-town.sqlite3"\noperator_only = true\n', encoding="ascii"
    )
    (snapshot_dir / "participants.toml").write_text(
        '[[participant]]\nid = "test-operator"\nrole = "operator"\n'
        f'token_sha256 = "{hashlib.sha256(b"disposable-test-token").hexdigest()}"\n', encoding="ascii"
    )
    (snapshot_dir / "SHA256SUMS").write_text("".join(
        f"{_sha256(snapshot_dir / name)}  {name}\n"
        for name in ("MANIFEST", "config.toml", "participants.toml", "gremlin-town.sqlite3")
    ), encoding="ascii")
    return snapshot_dir


def _fingerprint(directory: Path) -> dict[str, tuple[str, int, int, int]]:
    # Inventory equality also detects any newly created SQLite sidecars.
    return {
        path.name: (_sha256(path), path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_mode)
        for path in directory.iterdir()
    }


def _observe_sqlite(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list[str]]:
    original_connect = sqlite3.connect
    connections = []
    statements = []

    def connect(database, *args, **kwargs):
        connections.append((database, kwargs))
        connection = original_connect(database, *args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    # Observe real connections and real SQL; do not replace validation behavior.
    monkeypatch.setattr(sqlite3, "connect", connect)
    return connections, statements


def test_restore_helper_uses_verified_immutable_integrity_read_without_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_dir = _snapshot(tmp_path)
    database = snapshot_dir / "gremlin-town.sqlite3"
    before = _fingerprint(snapshot_dir)
    validator = _load_validator()
    connections, statements = _observe_sqlite(monkeypatch)

    validator.validate_directory(snapshot_dir)

    file_connections = [(name, kwargs) for name, kwargs in connections if name != ":memory:"]
    assert file_connections == [(database.resolve().as_uri() + "?mode=ro&immutable=1", {"uri": True})]
    assert any(statement.upper() == "PRAGMA INTEGRITY_CHECK" for statement in statements)
    assert _fingerprint(snapshot_dir) == before


@pytest.mark.parametrize("member", ["MANIFEST", "config.toml", "participants.toml", "gremlin-town.sqlite3"])
def test_restore_validator_checks_every_checksum_before_opening_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str,
) -> None:
    snapshot_dir = _snapshot(tmp_path)
    checksums = snapshot_dir / "SHA256SUMS"
    checksums.write_text(checksums.read_text().replace(_sha256(snapshot_dir / member), "0" * 64), encoding="ascii")
    before = _fingerprint(snapshot_dir)
    validator = _load_validator()
    connections, statements = _observe_sqlite(monkeypatch)

    with pytest.raises(ValueError, match="backup checksum mismatch"):
        validator.validate_directory(snapshot_dir)

    assert connections == []
    assert statements == []
    assert _fingerprint(snapshot_dir) == before


def test_restore_helper_invokes_release_validator_and_real_archive_cli(tmp_path: Path) -> None:
    helper = HELPER.read_text(encoding="utf-8")
    assert '"$GT_BACKUP_RELEASE_ROOT/deploy/validate-backup.py" extract "$archive" "$target"' in helper
    snapshot_dir = _snapshot(tmp_path)
    before = _fingerprint(snapshot_dir)
    archive = tmp_path / "snapshot # %.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as output:
        for path in snapshot_dir.iterdir():
            output.add(path, arcname=path.name)
    archive_before = (_sha256(archive), archive.stat().st_mtime_ns)
    target = tmp_path / "restored # %"

    for arguments in (("directory", snapshot_dir), ("extract", archive, target)):
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), *map(str, arguments)],
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr

    assert _fingerprint(snapshot_dir) == before
    assert (_sha256(archive), archive.stat().st_mtime_ns) == archive_before
    assert {path.name: _sha256(path) for path in target.iterdir()} == {
        name: fingerprint[0] for name, fingerprint in before.items()
    }


MCP_CLIENTS_CONFIG = b'''[mcp]
enabled = true
state_dir = "/var/lib/gremlin-town-oauth"

[[mcp.client]]
participant = "chatgpt-web"
name = "ChatGPT web"
client_id = "gremlin-town-chatgpt"
client_secret_file = "/etc/gremlin-town/mcp-client-secret"
redirect_uris = ["https://chatgpt.com/connector_platform_oauth_redirect"]
origins = ["https://chatgpt.com"]

[[mcp.client]]
participant = "claude-web"
name = "Claude"
client_id = "gremlin-town-claude"
redirect_uris = ["https://claude.ai/api/mcp/auth_callback"]
'''


def test_backup_validator_checks_every_mcp_client_entry() -> None:
    validator = _load_validator()
    validator.validate_config(MCP_CLIENTS_CONFIG, b"")
    smuggled = MCP_CLIENTS_CONFIG + b'client_secret = "inline-secret-value"\n'
    with pytest.raises(ValueError, match="unknown or secret"):
        validator.validate_config(smuggled, b"")
    other_array = MCP_CLIENTS_CONFIG + b'\n[[participants.file]]\npath = "x"\n'
    with pytest.raises(ValueError, match="nested"):
        validator.validate_config(other_array, b"")


def test_backup_validator_accepts_the_connection_age_limit() -> None:
    validator = _load_validator()
    state = b'state_dir = "/var/lib/gremlin-town-oauth"\n'
    assert state in MCP_CLIENTS_CONFIG
    validator.validate_config(MCP_CLIENTS_CONFIG.replace(state, state + b"max_grant_days = 90\n"), b"")
