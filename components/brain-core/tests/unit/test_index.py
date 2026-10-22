from __future__ import annotations

import json
import sqlite3
import sys
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain_core.contracts import GRANT_SCHEMA, Grant
from brain_core.discovery import discover_legacy_registry
from brain_core.errors import BrainError, ErrorCode
from brain_core.index import (
    _RebuildLock,
    _atomic_replace,
    _validate_candidate,
    index_status,
    query_index,
    rebuild_index,
)


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    exported = root / "docs" / "exported.md"
    exported.write_text("alpha exported evidence", encoding="utf-8")
    (root / "docs" / "search.md").write_text("beta private ranking", encoding="utf-8")
    (root / "docs" / "ignored.md").write_text("gamma must stay ignored", encoding="utf-8")
    (root / "brain.yaml").write_text(
        """schema: brain.vault/v1
id: vault_fixture
name: Fixture
purpose: test
search:
  paths:
    - docs/search.md
exports:
  - name: evidence
    paths:
      - docs/exported.md
""",
        encoding="utf-8",
    )
    node_map = tmp_path / "node.yaml"
    node_map.write_text(
        f"""schema: brain.node-map/v1
node: local
vaults:
  - id: vault_fixture
    root: "{root.as_posix()}"
""",
        encoding="utf-8",
    )
    return node_map, tmp_path / "index.sqlite3", exported


def _grant(path: str, now: datetime) -> Grant:
    return Grant(
        schema=GRANT_SCHEMA,
        id="grant_fixture",
        issuer="fixture",
        authenticated_source="test",
        source_message_sha256="a" * 64,
        session_id="test-session",
        operation="read",
        vault="vault_fixture",
        paths=(path,),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=1),
    )


def test_rebuild_indexes_only_declared_and_exact_granted_paths(tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)
    now = datetime.now(UTC)

    result = rebuild_index(
        node_map,
        database,
        grants=(_grant("docs/ignored.md", now),),
        now=now,
        session_id="test-session",
    )

    assert result.files == 3
    assert result.vaults == 1
    assert result.indexed_bytes > 0
    assert result.database_bytes == database.stat().st_size
    assert index_status(database).state == "fresh"
    exported = query_index(database, "alpha")
    search_only = query_index(database, ["beta"])
    granted = query_index(database, "gamma")
    assert [(row.path, row.exported, row.search_only) for row in exported] == [
        ("docs/exported.md", True, False)
    ]
    assert [(row.path, row.exported, row.search_only) for row in search_only] == [
        ("docs/search.md", False, True)
    ]
    assert granted[0].path == "docs/ignored.md"
    assert granted[0].root == str(tmp_path / "vault")
    assert granted[0].rank > 0.0


def test_policy_denial_happens_before_read(tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)
    now = datetime.now(UTC)
    missing = _grant("docs/does-not-exist.md", now)

    def deny_grants(manifest, path, grants, moment, session_id, node, mode):
        from brain_core.policy import AccessDecision

        if path == "docs/does-not-exist.md":
            return AccessDecision(False, "excluded", ErrorCode.GRANT_WRONG_PATH, "fixture denial")
        return AccessDecision(True, "search")

    result = rebuild_index(
        node_map,
        database,
        grants=(missing,),
        now=now,
        session_id="test-session",
        policy_check=deny_grants,
    )

    assert result.files == 2
    assert [(item.path, item.code, item.reason) for item in result.excluded] == [
        ("docs/does-not-exist.md", ErrorCode.GRANT_WRONG_PATH.value, "fixture denial")
    ]
    assert query_index(database, "does-not-exist") == ()


def test_synthetic_legacy_registry_can_build_without_project_writes(tmp_path: Path) -> None:
    root = tmp_path / "legacy-vault"
    second_root = tmp_path / "second-vault"
    root.mkdir()
    second_root.mkdir()
    (root / "intent.md").write_text("legacy searchable marker", encoding="utf-8")
    (second_root / "intent.md").write_text("legacy second marker", encoding="utf-8")
    registry = tmp_path / "legacy-registry.md"
    registry.write_text(
        f"""vaults:
  - vault: {root.as_posix()}
    vault-name: Legacy Fixture
    compass: intent.md
    loads-from: [intent.md]

    domains:
      - synthetic

  - vault: {second_root.as_posix()}
    vault-name: Second Fixture
    compass: intent.md
    loads-from: [intent.md]
    domains: [synthetic]
  - unrelated: parser-boundary
""",
        encoding="utf-8",
    )
    database = tmp_path / "legacy.sqlite3"

    result = rebuild_index(registry, database, legacy_registry=True)

    assert result.files == 2
    assert {match.vault_id for match in query_index(database, "legacy")} == {
        "vault_legacy_fixture",
        "vault_second_fixture",
    }

    registry.write_text(
        f"""vaults:
  - vault: {root.as_posix()}
    vault-name: Legacy Fixture
    loads-from: [intent.md]
""",
        encoding="utf-8",
    )
    _, single = discover_legacy_registry(registry)
    assert [manifest.id for manifest in single] == ["vault_legacy_fixture"]


def test_query_missing_stale_empty_and_bounded_limit(tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)
    with pytest.raises(BrainError) as raised:
        query_index(database, "alpha")
    assert raised.value.code is ErrorCode.INDEX_MISSING

    rebuild_index(node_map, database)
    assert [row.path for row in query_index(database, "", limit=0)] == ["docs/exported.md"]
    assert len(query_index(database, "", limit=50_000)) == 2

    status_path = database.with_name(f"{database.name}.status.json")
    status_path.write_text(
        json.dumps(
            {
                "state": "stale",
                "last_good_generation": index_status(database).generation,
                "error_code": ErrorCode.PATH_MISSING.value,
                "error_message": "source missing",
            }
        ),
        encoding="utf-8",
    )
    assert index_status(database).state == "stale"
    with pytest.raises(BrainError) as raised:
        query_index(database, "alpha")
    assert raised.value.code is ErrorCode.INDEX_STALE
    assert query_index(database, "alpha", allow_stale=True)[0].path == "docs/exported.md"


def test_invalid_status_fails_loudly(monkeypatch, tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)
    rebuild_index(node_map, database)
    status_path = database.with_name(f"{database.name}.status.json")
    status_path.write_text("not json", encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        index_status(database)
    assert raised.value.code is ErrorCode.INDEX_BUILD_FAILED

    real_read_text = Path.read_text
    calls = 0

    def transient_read_text(path: Path, *args, **kwargs) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("synthetic publish race")
        return real_read_text(path, *args, **kwargs)

    status_path.write_text('{"state":"fresh"}', encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", transient_read_text)
    assert index_status(database).state == "fresh"
    assert calls == 2
    monkeypatch.setattr(Path, "read_text", real_read_text)

    status_path.unlink()
    assert index_status(database).state == "fresh"

    status_path.write_text("[]", encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        index_status(database)
    assert raised.value.code is ErrorCode.INDEX_BUILD_FAILED

    def denied_read_text(path: Path, *args, **kwargs) -> str:
        raise PermissionError("synthetic persistent denial")

    monkeypatch.setattr(Path, "read_text", denied_read_text)
    with pytest.raises(BrainError) as raised:
        index_status(database)
    assert raised.value.code is ErrorCode.INDEX_BUILD_FAILED


def test_missing_generation_and_corrupt_database_are_visible(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()
    assert index_status(database).state == "missing"

    database.write_bytes(b"not sqlite")
    with pytest.raises(BrainError) as raised:
        index_status(database)
    assert raised.value.code is ErrorCode.INDEX_BUILD_FAILED


def test_candidate_validation_rejects_mismatched_tables() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE files (id INTEGER PRIMARY KEY);
            CREATE VIRTUAL TABLE file_fts USING fts5(body);
            INSERT INTO files DEFAULT VALUES;
            """
        )
        with pytest.raises(BrainError) as raised:
            _validate_candidate(connection)
    finally:
        connection.close()
    assert raised.value.code is ErrorCode.INDEX_BUILD_FAILED


def test_atomic_replace_retries_windows_sharing_violation(monkeypatch, tmp_path: Path) -> None:
    import brain_core.index as index_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    real_replace = index_module.os.replace
    calls = 0

    def transient_replace(first, second) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("synthetic sharing violation")
        real_replace(first, second)

    monkeypatch.setattr(index_module.os, "replace", transient_replace)
    _atomic_replace(source, destination, timeout=0.1)
    assert destination.read_text(encoding="utf-8") == "new"
    assert calls == 2


def test_atomic_replace_timeout_is_bounded(monkeypatch, tmp_path: Path) -> None:
    import brain_core.index as index_module

    source = tmp_path / "source"
    source.write_text("new", encoding="utf-8")

    def denied_replace(first, second) -> None:
        raise PermissionError("synthetic persistent sharing violation")

    monkeypatch.setattr(index_module.os, "replace", denied_replace)
    with pytest.raises(PermissionError):
        _atomic_replace(source, tmp_path / "destination", timeout=0.0)


def test_fault_wraps_error_removes_candidate_and_preserves_database(tmp_path: Path) -> None:
    node_map, database, exported = _write_fixture(tmp_path)
    first = rebuild_index(node_map, database)
    before = database.read_bytes()
    exported.write_text("changed alpha evidence", encoding="utf-8")

    def fail_before_publish(point: str, temporary: Path) -> None:
        if point == "before_publish":
            raise OSError("synthetic disk full")

    with pytest.raises(BrainError) as raised:
        rebuild_index(node_map, database, fault=fail_before_publish)

    assert raised.value.code is ErrorCode.INDEX_BUILD_FAILED
    assert raised.value.details["last_good_generation"] == first.generation
    assert database.read_bytes() == before
    assert index_status(database).state == "stale"
    assert not list(database.parent.glob(f".{database.name}.*.candidate"))


def test_exact_source_failure_preserves_error_and_prior_generation(tmp_path: Path) -> None:
    node_map, database, exported = _write_fixture(tmp_path)
    rebuild_index(node_map, database)
    before = database.read_bytes()
    exported.unlink()

    with pytest.raises(BrainError) as raised:
        rebuild_index(node_map, database)

    assert raised.value.code is ErrorCode.PATH_MISSING
    assert raised.value.details["locator"] == "docs/exported.md"
    assert database.read_bytes() == before
    assert index_status(database).state == "stale"


def test_source_changed_failure_preserves_prior_generation(monkeypatch, tmp_path: Path) -> None:
    import brain_core.index as index_module

    node_map, database, _ = _write_fixture(tmp_path)
    rebuild_index(node_map, database)
    before = database.read_bytes()

    def changed(*args, **kwargs):
        raise BrainError(
            ErrorCode.SOURCE_CHANGED,
            "Synthetic source changed during rebuild",
            details={"locator": "docs/exported.md"},
        )

    monkeypatch.setattr(index_module, "read_snapshot", changed)
    with pytest.raises(BrainError) as raised:
        rebuild_index(node_map, database)
    assert raised.value.code is ErrorCode.SOURCE_CHANGED
    assert database.read_bytes() == before
    assert index_status(database).state == "stale"


def test_busy_lock_is_bounded_and_reports_owner(tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)
    lock_path = database.with_name(f"{database.name}.rebuild.lock")

    with _RebuildLock(lock_path, 0.0):
        started = datetime.now(UTC)
        with pytest.raises(BrainError) as raised:
            rebuild_index(node_map, database, lock_timeout=0.02)

    assert raised.value.code is ErrorCode.INDEX_BUSY
    assert raised.value.details["owner"]["operation"] == "index-rebuild"
    assert (datetime.now(UTC) - started).total_seconds() < 0.5


def test_busy_lock_with_unreadable_owner_returns_stable_empty_owner(tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)
    lock_path = database.with_name(f"{database.name}.rebuild.lock")
    with _RebuildLock(lock_path, 0.0) as lock:
        lock._stream.seek(1)
        lock._stream.truncate()
        lock._stream.write(b"not json")
        lock._stream.flush()
        with pytest.raises(BrainError) as raised:
            rebuild_index(node_map, database, lock_timeout=0.0)
    assert raised.value.code is ErrorCode.INDEX_BUSY
    assert raised.value.details["owner"] == {}


def test_foreign_platform_lock_branch_is_executable(monkeypatch, tmp_path: Path) -> None:
    import brain_core.index as index_module

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 4

        def __init__(self) -> None:
            self.calls = []

        def flock(self, fileno, operation) -> None:
            self.calls.append((fileno, operation))

    fake = FakeFcntl()
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    monkeypatch.setattr(index_module.os, "name", "posix")
    path = tmp_path / "foreign.lock"
    lock = _RebuildLock(path, 0.0)
    lock._stream = path.open("w+b", buffering=0)
    lock._stream.write(b" ")
    assert lock._try_lock()
    lock.__exit__(None, None, None)
    assert fake.calls[0][1] == fake.LOCK_EX | fake.LOCK_NB
    assert fake.calls[1][1] == fake.LOCK_UN


def test_windows_lock_branch_is_executable_on_every_host(monkeypatch, tmp_path: Path) -> None:
    import brain_core.index as index_module

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self) -> None:
            self.calls = []

        def locking(self, fileno, operation, length) -> None:
            self.calls.append((fileno, operation, length))

    path = tmp_path / "windows.lock"
    lock = _RebuildLock(path, 0.0)
    lock._stream = path.open("w+b", buffering=0)
    lock._stream.write(b" ")
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(index_module.os, "name", "nt")
    assert lock._try_lock()
    lock.__exit__(None, None, None)
    assert fake.calls[0][1:] == (fake.LK_NBLCK, 1)
    assert fake.calls[1][1:] == (fake.LK_UNLCK, 1)


def test_system_exit_never_publishes_partial_candidate(tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)

    def killed(point: str, temporary: Path) -> None:
        if point == "after_row":
            raise SystemExit(9)

    with pytest.raises(SystemExit):
        rebuild_index(node_map, database, fault=killed)
    assert not database.exists()
    assert not list(database.parent.glob(f".{database.name}.*.candidate"))


def test_deleted_candidate_during_system_exit_still_preserves_prior(tmp_path: Path) -> None:
    node_map, database, _ = _write_fixture(tmp_path)
    rebuild_index(node_map, database)
    before = database.read_bytes()

    def killed(point: str, temporary: Path) -> None:
        if point == "before_publish":
            temporary.unlink()
            raise SystemExit(9)

    with pytest.raises(SystemExit):
        rebuild_index(node_map, database, fault=killed)
    assert database.read_bytes() == before
    assert index_status(database).state == "stale"


def test_interrupt_after_publish_leaves_complete_database(monkeypatch, tmp_path: Path) -> None:
    import brain_core.index as index_module

    node_map, database, _ = _write_fixture(tmp_path)
    real_mark_status = index_module._mark_status
    calls = 0

    def interrupted_status(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SystemExit(9)
        real_mark_status(*args, **kwargs)

    monkeypatch.setattr(index_module, "_mark_status", interrupted_status)
    with pytest.raises(SystemExit):
        rebuild_index(node_map, database)
    assert database.is_file()
    assert query_index(database, "alpha")[0].path == "docs/exported.md"
