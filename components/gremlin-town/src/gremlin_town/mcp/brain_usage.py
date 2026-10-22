"""Metadata-only log of Brain Core calls, used to improve agent workflow.

Each row records who called which operation, how it ended and how long it took.
It never stores arguments, memory bodies, queries or results. The log is process
evidence for ``brain-core/docs/interaction-learning.md``, not a canonical record,
and a logging failure never changes the Brain result returned to the caller.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

RETENTION_DAYS = 180
TRANSPORTS = frozenset({"mcp", "http"})
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class BrainUsageEvent:
    actor: str
    transport: str
    operation: str
    outcome: str  # "ok" or "error"
    error_code: str | None
    duration_ms: int
    operation_id: str | None = None
    record_id: str | None = None


def _safe(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE.fullmatch(value) else None


class BrainUsageLog:
    """Append-only SQLite log beside the Brain provider state."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retention_days: int = RETENTION_DAYS,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._database = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self._database.execute("PRAGMA journal_mode=WAL")
        self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS brain_usage (
                id INTEGER PRIMARY KEY,
                at TEXT NOT NULL,
                actor TEXT NOT NULL,
                transport TEXT NOT NULL,
                operation TEXT NOT NULL,
                outcome TEXT NOT NULL,
                error_code TEXT,
                duration_ms INTEGER NOT NULL,
                operation_id TEXT,
                record_id TEXT
            )
            """
        )
        cutoff = (self._clock() - timedelta(days=retention_days)).isoformat()
        self._database.execute("DELETE FROM brain_usage WHERE at < ?", (cutoff,))

    def record(self, event: BrainUsageEvent) -> None:
        """Write one row; report failures loudly in the service log, never to the caller."""

        try:
            if event.transport not in TRANSPORTS or event.outcome not in {"ok", "error"}:
                raise ValueError("invalid usage event")
            row = (
                self._clock().isoformat(),
                _safe(event.actor) or "unknown",
                event.transport,
                _safe(event.operation) or "unknown",
                event.outcome,
                _safe(event.error_code),
                max(0, int(event.duration_ms)),
                _safe(event.operation_id),
                _safe(event.record_id),
            )
            with self._lock:
                self._database.execute(
                    "INSERT INTO brain_usage(at, actor, transport, operation, outcome, error_code,"
                    " duration_ms, operation_id, record_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
        except (sqlite3.Error, ValueError, TypeError) as exc:
            logger.error("Brain usage log write failed (%s)", type(exc).__name__)

    def close(self) -> None:
        with self._lock:
            self._database.close()


def read_usage(path: Path, *, since: str | None = None) -> list[dict[str, object]]:
    """Read rows through a read-only connection; ``since`` is an ISO date."""

    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        database.row_factory = sqlite3.Row
        query = "SELECT * FROM brain_usage"
        params: tuple[str, ...] = ()
        if since:
            query += " WHERE at >= ?"
            params = (datetime.fromisoformat(since).date().isoformat(),)
        return [dict(row) for row in database.execute(query + " ORDER BY at, id", params)]
    finally:
        database.close()


def render_markdown(rows: list[dict[str, object]]) -> str:
    """Render one usage-log table row per UTC day, agent and transport."""

    groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["at"])[:10], str(row["actor"]), str(row["transport"]))].append(row)
    lines = []
    for (day, actor, transport), items in sorted(groups.items()):
        operations = Counter(str(item["operation"]) for item in items)
        ops = ", ".join(f"`{name}` ×{count}" if count > 1 else f"`{name}`" for name, count in sorted(operations.items()))
        ok = sum(1 for item in items if item["outcome"] == "ok")
        errors = Counter(str(item["error_code"] or "unknown") for item in items if item["outcome"] != "ok")
        slowest = max(int(item["duration_ms"]) for item in items) / 1000
        outcome = f"{ok} ok" + (f", {sum(errors.values())} error" if errors else "") + f"; slowest {slowest:.1f} s"
        friction = ", ".join(f"`{code}` ×{count}" if count > 1 else f"`{code}`" for code, count in sorted(errors.items())) or "none"
        lines.append(f"| {day} | {actor} ({transport}, server log) | {ops} | {outcome} | {friction} |")
    return "\n".join(lines) + ("\n" if lines else "")
