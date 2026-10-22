from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from gremlin_town.storage.migrations import SCHEMA_VERSION


TERMINAL_WORK_STATES = ("result_returned", "declined", "cancelled")
UNFINISHED_OPERATION_STATES = ("queued", "sending", "uncertain")


class RecoveryErrorCode(StrEnum):
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_NOT_FILE = "SOURCE_NOT_FILE"
    SOURCE_WAL_SIDECARS_PRESENT = "SOURCE_WAL_SIDECARS_PRESENT"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"
    DESTINATION_PARENT_MISSING = "DESTINATION_PARENT_MISSING"
    INTEGRITY_FAILED = "INTEGRITY_FAILED"
    SCHEMA_TOO_NEW = "SCHEMA_TOO_NEW"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    BACKUP_FAILED = "BACKUP_FAILED"
    PROMOTION_FAILED = "PROMOTION_FAILED"


class RecoveryError(Exception):
    def __init__(self, code: RecoveryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SchemaCompatibility:
    database_version: int
    supported_version: int
    compatible: bool


@dataclass(frozen=True, slots=True)
class RecoveryAudit:
    database: Path
    integrity_ok: bool
    schema: SchemaCompatibility
    unfinished_request_count: int
    held_unfinished_request_count: int
    unfinished_operation_count: int
    held_unfinished_operation_count: int
    missing_request_holds: tuple[str, ...]
    missing_operation_holds: tuple[str, ...]
    outbox_dispatch_blocked: bool
    normal_clients_enabled: bool | None

    @property
    def safe_for_operator_inspection(self) -> bool:
        return (
            self.integrity_ok
            and self.schema.compatible
            and not self.missing_request_holds
            and not self.missing_operation_holds
            and self.outbox_dispatch_blocked
            and self.normal_clients_enabled is False
        )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    source: Path
    destination: Path
    schema_version: int
    request_holds_applied: int
    operation_holds_applied: int
    audit: RecoveryAudit


def check_schema_compatibility(connection: sqlite3.Connection) -> SchemaCompatibility:
    try:
        database_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise RecoveryError(RecoveryErrorCode.INTEGRITY_FAILED, "cannot read SQLite schema version") from exc
    if database_version > SCHEMA_VERSION:
        raise RecoveryError(
            RecoveryErrorCode.SCHEMA_TOO_NEW,
            f"database schema {database_version} is newer than supported {SCHEMA_VERSION}",
        )
    if database_version not in (2, SCHEMA_VERSION):
        raise RecoveryError(
            RecoveryErrorCode.SCHEMA_UNSUPPORTED,
            f"database schema {database_version} is not supported by this recovery binary",
        )
    return SchemaCompatibility(database_version, SCHEMA_VERSION, True)


def _readonly_connection(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    encoded = quote(path.resolve().as_posix(), safe="/:")
    immutable_query = "&immutable=1" if immutable else ""
    connection = sqlite3.connect(f"file:{encoded}?mode=ro{immutable_query}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _check_integrity(connection: sqlite3.Connection) -> None:
    try:
        rows = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
    except sqlite3.DatabaseError as exc:
        raise RecoveryError(RecoveryErrorCode.INTEGRITY_FAILED, "SQLite integrity check could not run") from exc
    if rows != ("ok",):
        detail = "; ".join(str(row) for row in rows[:3]) or "no integrity result"
        raise RecoveryError(RecoveryErrorCode.INTEGRITY_FAILED, f"SQLite integrity check failed: {detail}")


def _unfinished_request_where(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    terminals = ",".join(f"'{state}'" for state in TERMINAL_WORK_STATES)
    return f"({prefix}work_state IS NULL OR {prefix}work_state NOT IN ({terminals}))"


def _unfinished_operation_where(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    states = ",".join(f"'{state}'" for state in UNFINISHED_OPERATION_STATES)
    return f"{prefix}operation_state IN ({states})"


def _apply_holds(connection: sqlite3.Connection) -> tuple[int, int]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        request_count = connection.execute(
            f"UPDATE requests SET recovery_hold=1 WHERE {_unfinished_request_where()} AND recovery_hold=0"
        ).rowcount
        operation_count = connection.execute(
            f"UPDATE operations SET recovery_hold=1 WHERE {_unfinished_operation_where()} AND recovery_hold=0"
        ).rowcount
        connection.commit()
    except (sqlite3.DatabaseError, RecoveryError):
        connection.rollback()
        raise
    return request_count, operation_count


def audit_recovery(
    database: Path, *, normal_clients_enabled: bool | None = None
) -> RecoveryAudit:
    database = Path(database).resolve()
    if not database.exists():
        raise RecoveryError(RecoveryErrorCode.SOURCE_NOT_FOUND, "recovery database does not exist")
    if not database.is_file():
        raise RecoveryError(RecoveryErrorCode.SOURCE_NOT_FILE, "recovery database is not a regular file")
    try:
        connection = _readonly_connection(database)
        try:
            _check_integrity(connection)
            schema = check_schema_compatibility(connection)
            missing_requests = tuple(
                row[0]
                for row in connection.execute(
                    f"SELECT request_id FROM requests WHERE {_unfinished_request_where()} AND recovery_hold=0 ORDER BY request_id"
                )
            )
            missing_operations = tuple(
                row[0]
                for row in connection.execute(
                    f"SELECT operation_id FROM operations WHERE {_unfinished_operation_where()} AND recovery_hold=0 ORDER BY operation_id"
                )
            )
            unfinished_requests = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM requests WHERE {_unfinished_request_where()}"
                ).fetchone()[0]
            )
            held_requests = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM requests WHERE {_unfinished_request_where()} AND recovery_hold=1"
                ).fetchone()[0]
            )
            unfinished_operations = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM operations WHERE {_unfinished_operation_where()}"
                ).fetchone()[0]
            )
            held_operations = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM operations WHERE {_unfinished_operation_where()} AND recovery_hold=1"
                ).fetchone()[0]
            )
        finally:
            connection.close()
    except RecoveryError:
        raise
    except sqlite3.DatabaseError as exc:
        raise RecoveryError(RecoveryErrorCode.INTEGRITY_FAILED, "recovery database cannot be audited") from exc
    blocked = not missing_requests and not missing_operations
    return RecoveryAudit(
        database=database,
        integrity_ok=True,
        schema=schema,
        unfinished_request_count=unfinished_requests,
        held_unfinished_request_count=held_requests,
        unfinished_operation_count=unfinished_operations,
        held_unfinished_operation_count=held_operations,
        missing_request_holds=missing_requests,
        missing_operation_holds=missing_operations,
        outbox_dispatch_blocked=blocked,
        normal_clients_enabled=normal_clients_enabled,
    )


def restore_snapshot(source: Path, destination: Path) -> RestoreResult:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.exists():
        raise RecoveryError(RecoveryErrorCode.SOURCE_NOT_FOUND, "snapshot source does not exist")
    if not source.is_file():
        raise RecoveryError(RecoveryErrorCode.SOURCE_NOT_FILE, "snapshot source is not a regular file")
    if destination.exists():
        raise RecoveryError(RecoveryErrorCode.DESTINATION_EXISTS, "restore destination already exists")
    if not destination.parent.is_dir():
        raise RecoveryError(
            RecoveryErrorCode.DESTINATION_PARENT_MISSING,
            "restore destination parent directory does not exist",
        )
    sidecars = (Path(f"{source}-wal"), Path(f"{source}-shm"))
    if any(sidecar.exists() for sidecar in sidecars):
        raise RecoveryError(
            RecoveryErrorCode.SOURCE_WAL_SIDECARS_PRESENT,
            "snapshot source has adjacent SQLite WAL sidecars and cannot be read immutably",
        )

    stage_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".restore", dir=destination.parent, delete=False
    )
    stage = Path(stage_handle.name)
    stage_handle.close()
    try:
        source_connection = _readonly_connection(source, immutable=True)
        try:
            _check_integrity(source_connection)
            compatibility = check_schema_compatibility(source_connection)
            stage_connection = sqlite3.connect(stage)
            try:
                source_connection.backup(stage_connection)
            finally:
                stage_connection.close()
        except RecoveryError:
            raise
        except sqlite3.DatabaseError as exc:
            raise RecoveryError(RecoveryErrorCode.BACKUP_FAILED, "SQLite backup failed") from exc
        finally:
            source_connection.close()

        stage_connection = sqlite3.connect(stage)
        try:
            _check_integrity(stage_connection)
            check_schema_compatibility(stage_connection)
            request_holds, operation_holds = _apply_holds(stage_connection)
        except RecoveryError:
            raise
        except sqlite3.DatabaseError as exc:
            raise RecoveryError(RecoveryErrorCode.INTEGRITY_FAILED, "restored schema cannot be held safely") from exc
        finally:
            stage_connection.close()

        # This new staging database has never been attached to a runtime, so
        # normal client access is observably disabled here. A standalone audit
        # cannot infer the state of an external process and reports unknown.
        audit = audit_recovery(stage, normal_clients_enabled=False)
        if not audit.safe_for_operator_inspection:
            raise RecoveryError(RecoveryErrorCode.INTEGRITY_FAILED, "restored database failed recovery audit")
        try:
            os.link(stage, destination)
        except FileExistsError as exc:
            raise RecoveryError(RecoveryErrorCode.DESTINATION_EXISTS, "restore destination appeared during promotion") from exc
        except OSError as exc:
            raise RecoveryError(RecoveryErrorCode.PROMOTION_FAILED, "create-only restore promotion failed") from exc
        destination_audit = replace(audit, database=destination)
        return RestoreResult(
            source=source,
            destination=destination,
            schema_version=compatibility.database_version,
            request_holds_applied=request_holds,
            operation_holds_applied=operation_holds,
            audit=destination_audit,
        )
    finally:
        stage.unlink(missing_ok=True)
