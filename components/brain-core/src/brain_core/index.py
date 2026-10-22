"""Atomic, node-local SQLite FTS5 catalog operations."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brain_core.contracts import Grant, TOKENIZER_ID, VaultManifest, stable_hash
from brain_core.discovery import discover_catalog, discover_legacy_registry
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import assert_no_case_collisions, normalize_relative, read_snapshot
from brain_core.policy import AccessDecision, check_search_access

INDEX_SCHEMA_VERSION = "1"
DEFAULT_LOCK_TIMEOUT = 1.0
DEFAULT_PUBLISH_TIMEOUT = 1.0
_TERM = re.compile(r"[^\W_]+", flags=re.UNICODE)
PolicyCheck = Callable[[VaultManifest, str, tuple[Grant, ...], datetime, str, str, str], AccessDecision]
FaultHook = Callable[[str, Path], None]


@dataclass(frozen=True, slots=True)
class IndexExclusion:
    vault_id: str
    path: str
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    generation: str
    vaults: int
    files: int
    indexed_bytes: int
    database_bytes: int
    excluded: tuple[IndexExclusion, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexStatus:
    state: str
    generation: str | None
    last_good_generation: str | None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class IndexMatch:
    vault_id: str
    path: str
    root: str
    content_sha256: str
    size: int
    search_only: bool
    exported: bool
    rank: float


def _status_path(database: Path) -> Path:
    return database.with_name(f"{database.name}.status.json")


def _lock_path(database: Path) -> Path:
    return database.with_name(f"{database.name}.rebuild.lock")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_replace(source: Path, destination: Path, timeout: float = DEFAULT_PUBLISH_TIMEOUT) -> None:
    """Bound the short Windows sharing wait while retaining os.replace semantics."""

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


class _RebuildLock:
    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = max(0.0, timeout)
        self._stream: Any = None

    def _try_lock(self) -> bool:
        assert self._stream is not None
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _owner(self) -> dict[str, Any]:
        try:
            assert self._stream is not None
            self._stream.seek(1)
            raw = self._stream.read().decode("utf-8").strip()
            value = json.loads(raw) if raw else {}
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def __enter__(self) -> _RebuildLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b", buffering=0)
        if self.path.stat().st_size == 0:
            self._stream.write(b" ")
        deadline = time.monotonic() + self.timeout
        while not self._try_lock():
            if time.monotonic() >= deadline:
                owner = self._owner()
                self._stream.close()
                self._stream = None
                raise BrainError(
                    ErrorCode.INDEX_BUSY,
                    "Another index rebuild owns the exclusive lock",
                    details={
                        "lock": str(self.path),
                        "owner": owner,
                        "timeout_seconds": self.timeout,
                    },
                    hint="Retry after the reported rebuild operation finishes.",
                )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        owner = {
            "operation": "index-rebuild",
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        encoded = json.dumps(owner, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._stream.seek(1)
        self._stream.truncate()
        self._stream.write(encoded)
        self._stream.flush()
        os.fsync(self._stream.fileno())
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        assert self._stream is not None
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None


def _database_generation(database: Path) -> str | None:
    if not database.is_file():
        return None
    try:
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key = 'generation'").fetchone()
    except sqlite3.Error as exc:
        raise BrainError(
            ErrorCode.INDEX_BUILD_FAILED,
            f"Cannot inspect index database: {database}",
            details={"database": str(database), "error": str(exc)},
            hint="Delete the derived index and rebuild it.",
        ) from exc
    return None if row is None else str(row[0])


def index_status(database_path: str | Path) -> IndexStatus:
    database = Path(database_path)
    generation = _database_generation(database)
    if generation is None:
        return IndexStatus("missing", None, None)
    status_file = _status_path(database)
    if not status_file.is_file():
        return IndexStatus("fresh", generation, generation)
    try:
        attempts = 0
        while True:
            try:
                status_text = status_file.read_text(encoding="utf-8")
                break
            except PermissionError:
                attempts += 1
                if attempts == 3:
                    raise
                time.sleep(0.01)
        payload = json.loads(status_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrainError(
            ErrorCode.INDEX_BUILD_FAILED,
            f"Cannot read index status: {status_file}",
            details={"status": str(status_file), "error": type(exc).__name__},
            hint="Delete the derived status file and rebuild the index.",
        ) from exc
    if not isinstance(payload, dict):
        raise BrainError(
            ErrorCode.INDEX_BUILD_FAILED,
            f"Index status is invalid: {status_file}",
            details={"status": str(status_file), "expected": "object"},
        )
    last_good = payload.get("last_good_generation")
    if payload.get("state") == "stale" and last_good == generation:
        return IndexStatus(
            "stale",
            generation,
            generation,
            str(payload.get("error_code")) if payload.get("error_code") else None,
            str(payload.get("error_message")) if payload.get("error_message") else None,
        )
    return IndexStatus("fresh", generation, generation)


def _mark_status(
    database: Path,
    *,
    state: str,
    generation: str | None,
    error: BaseException | None = None,
) -> None:
    error_code = error.code.value if isinstance(error, BrainError) else None
    payload = {
        "state": state,
        "last_good_generation": generation,
        "error_code": error_code,
        "error_message": str(error) if error is not None else None,
    }
    _atomic_json(_status_path(database), payload)


def _candidate_paths(manifest: VaultManifest, grants: tuple[Grant, ...]) -> tuple[str, ...]:
    granted = tuple(path for grant in grants if grant.vault == manifest.id for path in grant.paths)
    candidates = tuple(dict.fromkeys((*manifest.index_paths, *granted)))
    normalized = tuple(normalize_relative(path) for path in candidates)
    assert_no_case_collisions(list(normalized), kind="path")
    return tuple(sorted(normalized))


def _max_bytes(manifest: VaultManifest, path: str) -> int:
    limits = [export.max_bytes for export in manifest.exports if path in export.paths]
    return min(limits) if limits else 1_000_000


def _open_candidate(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE files (
            id INTEGER PRIMARY KEY,
            vault_id TEXT NOT NULL,
            path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            root TEXT NOT NULL,
            search_only INTEGER NOT NULL CHECK (search_only IN (0, 1)),
            exported INTEGER NOT NULL CHECK (exported IN (0, 1)),
            access TEXT NOT NULL,
            UNIQUE (vault_id, path)
        );
        CREATE VIRTUAL TABLE file_fts USING fts5(
            body,
            vault_id UNINDEXED,
            path UNINDEXED,
            tokenize='unicode61'
        );
        """
    )
    return connection


def _validate_candidate(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    files = connection.execute("SELECT count(*) FROM files").fetchone()[0]
    indexed = connection.execute("SELECT count(*) FROM file_fts").fetchone()[0]
    if integrity != ("ok",) or files != indexed:
        raise BrainError(
            ErrorCode.INDEX_BUILD_FAILED,
            "Candidate index failed validation",
            details={"integrity": integrity[0] if integrity else None, "files": files, "fts_rows": indexed},
        )
    connection.execute("SELECT rowid FROM file_fts WHERE file_fts MATCH 'validation' LIMIT 1").fetchall()


def rebuild_index(
    node_map_path: str | Path,
    database_path: str | Path,
    *,
    grants: tuple[Grant, ...] = (),
    now: datetime | None = None,
    session_id: str = "index-rebuild",
    node: str | None = None,
    mode: str = "think",
    legacy_registry: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    policy_check: PolicyCheck = check_search_access,
    fault: FaultHook | None = None,
) -> IndexBuildResult:
    """Build a complete temporary generation, validate it, then atomically publish it."""

    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now(UTC)
    temporary: Path | None = None

    # candidate generation -> complete validation -> os.replace atomic publish
    #          failure --------> remove candidate; preserve last good database
    with _RebuildLock(_lock_path(database), lock_timeout):
        previous_generation = _database_generation(database)
        _mark_status(
            database,
            state="stale" if previous_generation is not None else "missing",
            generation=previous_generation,
            error=BrainError(ErrorCode.INDEX_STALE, "index rebuild is in progress"),
        )
        try:
            if legacy_registry:
                node_map, manifests = discover_legacy_registry(node_map_path, node=node or "legacy-local")
            else:
                node_map, manifests = discover_catalog(node_map_path)
            selected_node = node or node_map.node
            handle, raw_temporary = tempfile.mkstemp(prefix=f".{database.name}.", suffix=".candidate", dir=database.parent)
            os.close(handle)
            temporary = Path(raw_temporary)
            connection = _open_candidate(temporary)
            rows_for_generation: list[dict[str, Any]] = []
            exclusions: list[IndexExclusion] = []
            indexed_bytes = 0
            try:
                if fault is not None:
                    fault("after_schema", temporary)
                for manifest in manifests:
                    for path in _candidate_paths(manifest, grants):
                        decision = policy_check(manifest, path, grants, moment, session_id, selected_node, mode)
                        if not decision.allowed:
                            exclusions.append(
                                IndexExclusion(
                                    vault_id=manifest.id,
                                    path=path,
                                    code=(decision.code or ErrorCode.POLICY_EXCLUDED).value,
                                    reason=decision.reason or "policy denied indexing",
                                )
                            )
                            continue
                        snapshot = read_snapshot(manifest.root, path, max_bytes=_max_bytes(manifest, path))
                        exported = path in manifest.export_paths
                        search_only = not exported
                        cursor = connection.execute(
                            """
                            INSERT INTO files (
                                vault_id, path, content_sha256, size, mtime_ns, root,
                                search_only, exported, access
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                manifest.id,
                                path,
                                snapshot.content_sha256,
                                snapshot.size,
                                snapshot.mtime_ns,
                                manifest.root,
                                int(search_only),
                                int(exported),
                                decision.access,
                            ),
                        )
                        connection.execute(
                            "INSERT INTO file_fts(rowid, body, vault_id, path) VALUES (?, ?, ?, ?)",
                            (cursor.lastrowid, snapshot.text, manifest.id, path),
                        )
                        row_identity = {
                            "vault_id": manifest.id,
                            "path": path,
                            "content_sha256": snapshot.content_sha256,
                            "size": snapshot.size,
                            "search_only": search_only,
                            "exported": exported,
                        }
                        rows_for_generation.append(row_identity)
                        indexed_bytes += snapshot.size
                        if fault is not None:
                            fault("after_row", temporary)
                generation = stable_hash(
                    {
                        "schema": INDEX_SCHEMA_VERSION,
                        "tokenizer": TOKENIZER_ID,
                        "node": selected_node,
                        "files": rows_for_generation,
                    }
                )
                metadata = {
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "tokenizer": TOKENIZER_ID,
                    "generation": generation,
                    "node": selected_node,
                    "vaults": str(len(manifests)),
                    "files": str(len(rows_for_generation)),
                    "indexed_bytes": str(indexed_bytes),
                }
                connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", sorted(metadata.items()))
                if fault is not None:
                    fault("before_validate", temporary)
                _validate_candidate(connection)
                connection.commit()
            finally:
                connection.close()
            if fault is not None:
                fault("before_publish", temporary)
            _atomic_replace(temporary, database)
            temporary = None
            _mark_status(database, state="fresh", generation=generation)
            return IndexBuildResult(
                generation=generation,
                vaults=len(manifests),
                files=len(rows_for_generation),
                indexed_bytes=indexed_bytes,
                database_bytes=database.stat().st_size,
                excluded=tuple(exclusions),
            )
        except BaseException as exc:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            _mark_status(
                database,
                state="stale" if previous_generation is not None else "missing",
                generation=previous_generation,
                error=exc,
            )
            if isinstance(exc, BrainError):
                raise
            raise BrainError(
                ErrorCode.INDEX_BUILD_FAILED,
                "Index rebuild failed before atomic publish",
                details={
                    "database": str(database),
                    "last_good_generation": previous_generation,
                    "error": type(exc).__name__,
                },
                hint="Repair the source or storage failure, then run the rebuild again.",
            ) from exc


def _query_terms(intent_terms: str | Sequence[str]) -> tuple[str, ...]:
    values = (intent_terms,) if isinstance(intent_terms, str) else tuple(intent_terms)
    return tuple(sorted({match.group(0).casefold() for value in values for match in _TERM.finditer(value)}))


def query_index(
    database_path: str | Path,
    intent_terms: str | Sequence[str],
    *,
    allow_stale: bool = False,
    limit: int = 100,
) -> tuple[IndexMatch, ...]:
    """Return ranked metadata only; source text and matched terms never leave SQLite."""

    database = Path(database_path)
    status = index_status(database)
    if status.state == "missing":
        raise BrainError(
            ErrorCode.INDEX_MISSING,
            f"Index database is missing: {database}",
            details={"database": str(database)},
            hint="Run the index rebuild command.",
        )
    if status.state == "stale" and not allow_stale:
        raise BrainError(
            ErrorCode.INDEX_STALE,
            "The last good index is stale after a failed rebuild",
            details={
                "database": str(database),
                "generation": status.generation,
                "failure_code": status.error_code,
            },
            hint="Rebuild, or explicitly allow degraded stale-index use.",
        )
    terms = _query_terms(intent_terms)
    bounded_limit = max(1, min(int(limit), 1000))
    with closing(sqlite3.connect(database)) as connection:
        if terms:
            expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
            rows = connection.execute(
                """
                SELECT f.vault_id, f.path, f.content_sha256, f.size,
                       f.search_only, f.exported, f.root, bm25(file_fts) AS score
                FROM file_fts
                JOIN files AS f ON f.id = file_fts.rowid
                WHERE file_fts MATCH ?
                ORDER BY score, f.vault_id, f.path
                LIMIT ?
                """,
                (expression, bounded_limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT vault_id, path, content_sha256, size,
                       search_only, exported, root, 0.0 AS score
                FROM files
                ORDER BY vault_id, path
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
    return tuple(
        IndexMatch(
            vault_id=str(row[0]),
            path=str(row[1]),
            content_sha256=str(row[2]),
            size=int(row[3]),
            search_only=bool(row[4]),
            exported=bool(row[5]),
            root=str(row[6]),
            rank=max(0.0, -float(row[7])),
        )
        for row in rows
    )
