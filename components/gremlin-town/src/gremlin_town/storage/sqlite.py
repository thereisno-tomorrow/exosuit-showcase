from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

from gremlin_town.contracts import MessageKind, OperationState, OutboundEnvelope, RequestMode, WorkState
from gremlin_town.domain.models import OperationRecord, RequestRecord

from .migrations import MIGRATIONS, SCHEMA_VERSION


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLiteStore:
    def __init__(self, path: str | Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        try:
            self._migrate()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _migrate(self) -> None:
        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {current} is newer than supported {SCHEMA_VERSION}")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            # executescript commits a pending transaction before executing. Put
            # BEGIN/COMMIT inside the script so DDL and version publish together.
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + MIGRATIONS[version]
                    + f"\nPRAGMA user_version = {version};\nCOMMIT;"
                )
            except BaseException:
                self.connection.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def fetchone(self, query: str, parameters: Iterable[object] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(query, tuple(parameters)).fetchone()

    def fetchall(self, query: str, parameters: Iterable[object] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(query, tuple(parameters)).fetchall()

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def request(self, request_id: str, connection: sqlite3.Connection | None = None) -> RequestRecord | None:
        db = connection or self.connection
        with self._lock:
            row = db.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        return RequestRecord(
            request_id=row["request_id"],
            mode=RequestMode(row["mode"]),
            project=row["project"],
            requester_id=row["requester_id"],
            recipient_id=row["recipient_id"],
            objective=row["objective"],
            sensitivity=row["sensitivity"],
            work_state=WorkState(row["work_state"]) if row["work_state"] else None,
            revision=row["revision"],
            recovery_hold=bool(row["recovery_hold"]),
            prior_request_id=row["prior_request_id"],
            thread_id=row["thread_id"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def operation(self, operation_id: str, connection: sqlite3.Connection | None = None) -> OperationRecord | None:
        db = connection or self.connection
        with self._lock:
            row = db.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
        return self._operation_row(row) if row else None

    def operation_by_client(
        self, actor_id: str, client_operation_id: str, connection: sqlite3.Connection | None = None
    ) -> OperationRecord | None:
        db = connection or self.connection
        with self._lock:
            row = db.execute(
                "SELECT * FROM operations WHERE actor_id = ? AND client_operation_id = ?",
                (actor_id, client_operation_id),
            ).fetchone()
        return self._operation_row(row) if row else None

    @staticmethod
    def _operation_row(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            operation_id=row["operation_id"],
            client_operation_id=row["client_operation_id"],
            actor_id=row["actor_id"],
            request_id=row["request_id"],
            operation_kind=row["operation_kind"],
            message_kind=MessageKind(row["message_kind"]) if row["message_kind"] else None,
            input_digest=row["input_digest"],
            accepted_at=_dt(row["accepted_at"]),
            operation_state=OperationState(row["operation_state"]) if row["operation_state"] else None,
            error_code=row["error_code"],
            error_message=row["error_message"],
            retryable=bool(row["retryable"]),
            payload=row["payload"],
            payload_sha256=row["payload_sha256"],
            provider_retry_key=row["provider_retry_key"],
            recipient_address=row["recipient_address"],
            route_source_id=row["route_source_id"],
            route_destination_id=row["route_destination_id"],
            route_sensitivity=row["route_sensitivity"],
            parent_message_id=row["parent_message_id"],
            provider_message_id=row["provider_message_id"],
            provider_thread_id=row["provider_thread_id"],
            provider_parent_message_id=row["provider_parent_message_id"],
            provider_accepted_at=_dt(row["provider_accepted_at"]),
            attempt_started_at=_dt(row["attempt_started_at"]),
            attempt_count=row["attempt_count"],
            next_attempt_at=_dt(row["next_attempt_at"]),
            transport_error_code=row["transport_error_code"],
            transport_status=row["transport_status"],
            uncertain_since=_dt(row["uncertain_since"]),
            recovery_required=bool(row["recovery_required"]),
            recovery_hold=bool(row["recovery_hold"]),
        )

    def outbound_envelope(self, operation_id: str) -> OutboundEnvelope:
        operation = self.operation(operation_id)
        if not operation or operation.payload is None:
            raise KeyError(operation_id)
        return OutboundEnvelope(
            operation_id=operation.operation_id,
            request_id=operation.request_id,
            kind=operation.message_kind,
            provider_retry_key=operation.provider_retry_key,
            payload=operation.payload,
            content_sha256=operation.payload_sha256,
            recipient_address=operation.recipient_address,
            parent_message_id=operation.parent_message_id,
        )

    def append_event(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str | None,
        operation_id: str | None,
        actor_id: str,
        event_type: str,
        detail: dict,
        occurred_at: datetime | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(request_id, operation_id, actor_id, event_type, detail_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, operation_id, actor_id, event_type, json.dumps(detail, sort_keys=True), (occurred_at or self.now()).isoformat()),
        )

    def next_dispatchable(self) -> OperationRecord | None:
        row = self.fetchone(
            """
            SELECT o.* FROM operations o
            JOIN requests r ON r.request_id = o.request_id
            WHERE o.operation_state = 'queued' AND o.recovery_hold = 0 AND r.recovery_hold = 0
              AND o.recovery_required = 0
              AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?)
              AND NOT EXISTS (
                SELECT 1 FROM operations earlier
                WHERE earlier.request_id = o.request_id AND earlier.accepted_at < o.accepted_at
                  AND earlier.operation_state IN ('queued', 'sending', 'uncertain')
            )
            ORDER BY o.accepted_at, o.operation_id LIMIT 1
            """,
            (self.now().isoformat(),),
        )
        return self._operation_row(row) if row else None

    def next_uncertain(self) -> OperationRecord | None:
        row = self.fetchone(
            """SELECT * FROM operations
            WHERE operation_state = 'uncertain'
              AND recovery_required = 0
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY accepted_at, operation_id LIMIT 1""",
            (self.now().isoformat(),),
        )
        return self._operation_row(row) if row else None

    def local_usage(self) -> dict[str, int]:
        """Return local durable usage only; this makes no provider-quota claim."""
        with self._lock:
            row = self.connection.execute(
            """SELECT
                SUM(CASE WHEN operation_state='queued' THEN 1 ELSE 0 END) AS queued_count,
                SUM(CASE WHEN operation_state='sending' THEN 1 ELSE 0 END) AS sending_count,
                SUM(CASE WHEN operation_state='uncertain' THEN 1 ELSE 0 END) AS uncertain_count,
                COALESCE(SUM(LENGTH(payload)), 0) AS retained_payload_bytes
            FROM operations"""
            ).fetchone()
            wal_path = self.path.with_name(f"{self.path.name}-wal")
            return {
                "queued_count": int(row["queued_count"] or 0),
                "sending_count": int(row["sending_count"] or 0),
                "uncertain_count": int(row["uncertain_count"] or 0),
                "retained_payload_bytes": int(row["retained_payload_bytes"]),
                "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
            }

    def mark_operation_uncertain(self, operation_id: str, message: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE operations SET operation_state='uncertain', error_message=?, retryable=1 WHERE operation_id=?",
                (message, operation_id),
            )

    def apply_restore_holds(self) -> None:
        """Called on a database restored from backup before normal clients start."""
        with self.transaction() as connection:
            connection.execute(
                """UPDATE requests SET recovery_hold=1
                WHERE (mode='action_requested' AND work_state IN ('requested','acknowledged','in_progress','blocked'))
                   OR EXISTS (SELECT 1 FROM operations o WHERE o.request_id=requests.request_id
                              AND o.operation_state IN ('queued','sending','uncertain'))"""
            )
            connection.execute(
                "UPDATE operations SET recovery_hold=1 WHERE operation_state IN ('queued','sending','uncertain')"
            )
            for row in connection.execute("SELECT request_id FROM requests WHERE recovery_hold=1"):
                self.append_event(
                    connection,
                    request_id=row["request_id"],
                    operation_id=None,
                    actor_id="system:restore",
                    event_type="recovery_hold_applied",
                    detail={},
                )

    def purge_outbox_payloads(self, request_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE operations SET payload=NULL WHERE request_id=? AND operation_state='accepted'", (request_id,)
            )

    def insert_empty_context(self, requester_id: str, recipient_id: str, project: str, at: datetime) -> str:
        request_id = self.new_id("req")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO requests(request_id,mode,project,requester_id,recipient_id,objective,sensitivity,work_state,revision,recovery_hold,prior_request_id,thread_id,created_at,updated_at) VALUES (?, 'context_only', ?, ?, ?, '', 'public-test', NULL, 1, 0, NULL, NULL, ?, ?)",
                (request_id, project, requester_id, recipient_id, at.isoformat(), at.isoformat()),
            )
        return request_id
