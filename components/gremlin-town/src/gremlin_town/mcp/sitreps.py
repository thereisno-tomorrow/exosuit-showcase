"""Task and project sitreps on the hub (docs/product/steps/b-project-sitreps.md), off by default.

With ``[sitreps] enabled = true`` the hub keeps, in Brain Home, one small task sitrep for each
project task whose "where I stopped" note it saves through the PC connector, and one Focus copy
per project: the first sentence of Focus in the Compass of the vault that holds it. The hub is
their only writer; no tool writes either. Brain Core builds and reads the records
(``brain_core.sitreps``); this module decides when, and talks to the PC and Brain Home.

- After each successful project note start, append or close through the PC connector, the hub
  updates that task's sitrep and refreshes the project's Focus copy. If a Brain Home write
  fails, the note still counts as saved: the hub keeps the sitrep it wanted in its own state,
  retries it, and the reply says "summary not updated yet".
- ``continuity.list`` for a project answers as before while the PC is on, plus the project
  sitrep. With the PC off, or for an agent not enrolled on the project, it answers from the
  sitreps, marked "summary as of <time>". Reading a full note with the PC off says it needs the
  PC. Agents make the same calls as before, so no model step is added.
- Closed task sitreps leave the live set 30 days after their note closed, moved whole to Brain
  Home's archive path (step C).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Callable

import anyio
from brain_core.github_storage import GitHubConflict
from brain_core.presentation import relative_day_words
from brain_core.timewords import personal_today
from brain_core.records import Evidence, Record, render_record
from brain_core.sitreps import (
    TaskSitrep,
    apply_event,
    archive_due,
    close_sitrep,
    current_focus_copy,
    current_sitreps,
    focus_copy_record,
    focus_line,
    project_sitrep,
    same_sitrep,
    seed_sitrep,
    sitrep_list,
    sitrep_record,
    start_sitrep,
)

from gremlin_town.mcp.brain_home_copy import SCAN_MAX_BYTES, SCAN_MAX_RECORDS
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation,
    BrainRuntime,
    BrainRuntimeError,
    ContinuityAppendRequest,
    ContinuityCloseRequest,
    ContinuityListRequest,
    ContinuityListResult,
    ContinuityRecoverRequest,
    ContinuityRecovery,
    ContinuityStartRequest,
    ContinuitySummary,
    ContinuityWriteResult,
    OwnerSelector,
)
from gremlin_town.mcp.local_project_connector import ConnectorError, ConnectorUnavailable

logger = logging.getLogger(__name__)

HUB_ACTOR = "system:sitreps"
OWNER_ID = "brain-home"
OWNER_LOCATOR = "vault://brain-home"
STATE_FILE = "sitreps.sqlite3"
FOCUS_REFRESH_INTERVAL = timedelta(hours=1)
ARCHIVE_SWEEP_INTERVAL = timedelta(hours=24)
ARCHIVE_BATCH = 500
RETRIES_PER_CALL = 5
PUBLISH_ATTEMPTS = 4
SEED_MAX_TOKENS = 2_500
NOT_UPDATED = "summary not updated yet"
NEEDS_PC = "The full note needs the PC, which is off or unreachable. List this project's notes for its summary."
_KINDS = frozenset({"sitrep", "focus_copy"})


@dataclass(frozen=True, slots=True)
class ContinuityWriteResultWithSummary(ContinuityWriteResult):
    """A saved project note whose sitrep isn't in Brain Home yet; the hub retries it."""

    summary: str = NOT_UPDATED


@dataclass(frozen=True, slots=True)
class SitrepListItem(ContinuitySummary):
    """One open or blocked task, from its sitrep, with its first three blockers."""

    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContinuityListWithProjectSitrep(ContinuityListResult):
    """A project's open notes read live from the PC, as before, plus the project sitrep."""

    project_sitrep: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SitrepListResult(ContinuityListResult):
    """A project's open tasks answered from their sitreps, saying when they were last updated."""

    summary: str = ""
    as_of: datetime | None = None
    project_sitrep: dict[str, object] | None = None


def _sitrep_json(sitrep: TaskSitrep) -> str:
    return json.dumps({"state": sitrep.state, **sitrep.data()}, sort_keys=True, separators=(",", ":"))


def _sitrep_from_json(text: str) -> TaskSitrep:
    value = json.loads(text)
    return TaskSitrep(
        project=value["project"],
        session_id=value["session_id"],
        intent=value["intent"],
        state=value["state"],
        next_step=value["next_step"],
        blockers=tuple(value["blockers"]),
        note_updated_at=datetime.fromisoformat(value["note_updated_at"]),
        note_record_id=value["note"]["record_id"],
        note_content_sha256=value["note"]["content_sha256"],
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SitrepState:
    """The hub's own sitrep bookkeeping, beside the Brain provider state; never canonical.

    It keeps the newest sitrep and Focus copy the hub wants in Brain Home (so the next one can
    be worked out without a read, and a failed write is retried), when each project's Focus was
    last checked, and when closed sitreps were last archived. Losing it loses nothing that
    matters: the next write for a task rebuilds from Brain Home or from the note itself.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._database = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        try:
            self._database.execute("PRAGMA journal_mode=WAL")
            self._database.execute(
                """
                CREATE TABLE IF NOT EXISTS wanted (
                    kind TEXT NOT NULL,
                    project TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    published INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT,
                    changed_at TEXT NOT NULL,
                    PRIMARY KEY (kind, project, session_id)
                )
                """
            )
            self._database.execute("CREATE TABLE IF NOT EXISTS marks (name TEXT PRIMARY KEY, at TEXT NOT NULL)")
        except sqlite3.Error:
            self._database.close()
            raise

    def _one(self, sql: str, values: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with self._lock:
            return self._database.execute(sql, values).fetchone()

    def _run(self, sql: str, values: tuple[Any, ...]) -> None:
        with self._lock:
            self._database.execute(sql, values)

    def wanted(self, kind: str, project: str, session_id: str = "") -> tuple[str, bool] | None:
        row = self._one(
            "SELECT payload, published FROM wanted WHERE kind = ? AND project = ? AND session_id = ?",
            (kind, project, session_id),
        )
        return None if row is None else (str(row[0]), bool(row[1]))

    def want(self, kind: str, project: str, session_id: str, payload: str, now: datetime) -> None:
        self._run(
            """
            INSERT INTO wanted(kind, project, session_id, payload, published, attempts, last_error, changed_at)
            VALUES (?, ?, ?, ?, 0, 0, NULL, ?)
            ON CONFLICT(kind, project, session_id) DO UPDATE SET
                payload = excluded.payload, published = 0, attempts = 0, last_error = NULL,
                changed_at = excluded.changed_at
            WHERE wanted.payload != excluded.payload OR wanted.published = 0
            """,
            (kind, project, session_id, payload, now.isoformat()),
        )

    def published(self, kind: str, project: str, session_id: str, payload: str) -> None:
        self._run(
            "UPDATE wanted SET published = 1, last_error = NULL WHERE kind = ? AND project = ? AND session_id = ? AND payload = ?",
            (kind, project, session_id, payload),
        )

    def failed(self, kind: str, project: str, session_id: str, error: str) -> None:
        self._run(
            "UPDATE wanted SET attempts = attempts + 1, last_error = ? WHERE kind = ? AND project = ? AND session_id = ?",
            (error, kind, project, session_id),
        )

    def pending(self, limit: int) -> list[tuple[str, str, str, str]]:
        with self._lock:
            rows = self._database.execute(
                "SELECT kind, project, session_id, payload FROM wanted WHERE published = 0 ORDER BY changed_at, kind, project, session_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [(str(kind), str(project), str(session_id), str(payload)) for kind, project, session_id, payload in rows]

    def forget(self, project: str, session_id: str) -> None:
        self._run("DELETE FROM wanted WHERE kind = 'sitrep' AND project = ? AND session_id = ?", (project, session_id))

    def mark(self, name: str) -> datetime | None:
        row = self._one("SELECT at FROM marks WHERE name = ?", (name,))
        return None if row is None else datetime.fromisoformat(str(row[0]))

    def set_mark(self, name: str, at: datetime) -> None:
        self._run(
            "INSERT INTO marks(name, at) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET at = excluded.at",
            (name, at.isoformat()),
        )

    def close(self) -> None:
        with self._lock:
            self._database.close()


class SitrepService:
    """The hub's one writer and reader of task sitreps and Focus copies in Brain Home."""

    def __init__(
        self,
        *,
        storage: Any,
        connector: Any,
        state_dir: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._connector = connector
        self._clock = clock
        self._state = SitrepState(state_dir / STATE_FILE)
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("sitrep clock returned an invalid time")
        return now

    # --- Brain Home ---------------------------------------------------------------------------

    def _snapshot(self) -> Any:
        return self._storage.snapshot_records(kinds=_KINDS, max_records=SCAN_MAX_RECORDS, max_bytes=SCAN_MAX_BYTES)

    @staticmethod
    def _evidence(locator: str, payload: str, excerpt: str) -> Evidence:
        return Evidence(
            source_type="gremlin_town_hub",
            locator=locator,
            content_sha256=_sha256(payload),
            excerpt=excerpt,
            actor=HUB_ACTOR,
        )

    def _publish(self, build: Callable[[list[Record]], Record | None]) -> None:
        """Write the record ``build`` returns on the branch it read, or nothing if it returns None."""

        for _ in range(PUBLISH_ATTEMPTS):
            snapshot = self._snapshot()
            record = build([item.record for item in snapshot.records])
            if record is None:
                return
            try:
                self._storage.publish_record(
                    record_bytes=render_record(record).encode("utf-8"),
                    expected_revision=snapshot.revision.commit_sha,
                )
                return
            except GitHubConflict as exc:
                if exc.code != "github_revision_conflict":
                    raise
        raise RuntimeError("Brain Home changed repeatedly during the sitrep write")

    def _publish_sitrep(self, wanted: TaskSitrep) -> bool:
        payload = _sitrep_json(wanted)
        adopted: list[TaskSitrep] = []

        def build(records: list[Record]) -> Record | None:
            current = current_sitreps(records, wanted.project).get(wanted.session_id)
            if current is not None:
                if same_sitrep(current, wanted):
                    return None
                existing = TaskSitrep.from_record(current)
                if existing.note_updated_at > wanted.note_updated_at:
                    # Brain Home already shows a later version of the note: keep it.
                    adopted.append(existing)
                    return None
            return sitrep_record(
                wanted,
                previous=current,
                now=self._now(),
                evidence=self._evidence(
                    f"gt-hub://sitreps/{wanted.project}/{wanted.session_id}",
                    payload,
                    "Written by the Gremlin Town hub from the project note; not the note itself.",
                ),
                owner_id=OWNER_ID,
                owner_locator=OWNER_LOCATOR,
            )

        try:
            self._publish(build)
        except Exception as exc:  # the note is saved; the reply and the queue say the summary is behind
            self._state.failed("sitrep", wanted.project, wanted.session_id, type(exc).__name__)
            logger.warning(
                "Sitrep for %s task %s is not in Brain Home yet (%s); queued for retry",
                wanted.project, wanted.session_id, type(exc).__name__,
            )
            return False
        if adopted:
            newer = _sitrep_json(adopted[0])
            self._state.want("sitrep", wanted.project, wanted.session_id, newer, self._now())
            self._state.published("sitrep", wanted.project, wanted.session_id, newer)
        else:
            self._state.published("sitrep", wanted.project, wanted.session_id, payload)
        return True

    def _publish_focus(self, project: str, payload: str) -> bool:
        wanted = json.loads(payload)

        def build(records: list[Record]) -> Record | None:
            current = current_focus_copy(records, project)
            if current is None and wanted["line"] is None and wanted["section_sha256"] is None:
                return None  # no Compass Focus and none copied before: nothing to say
            if current is not None and (current.data.get("line"), current.data.get("section_sha256")) == (
                wanted["line"], wanted["section_sha256"]
            ):
                return None
            return focus_copy_record(
                project=project,
                line=wanted["line"],
                section_sha256=wanted["section_sha256"],
                previous=current,
                now=self._now(),
                evidence=self._evidence(
                    f"gt-hub://focus/{project}",
                    payload,
                    "Copied by the Gremlin Town hub from the Compass Focus of the vault that holds the project.",
                ),
                owner_id=OWNER_ID,
                owner_locator=OWNER_LOCATOR,
            )

        try:
            self._publish(build)
        except Exception as exc:
            self._state.failed("focus_copy", project, "", type(exc).__name__)
            logger.warning("Focus copy for %s is not in Brain Home yet (%s); queued for retry", project, type(exc).__name__)
            return False
        self._state.published("focus_copy", project, "", payload)
        return True

    def _retry_pending(self) -> None:
        """Retry queued writes, oldest first, stopping at the first one that still fails."""

        for kind, project, session_id, payload in self._state.pending(RETRIES_PER_CALL):
            done = self._publish_sitrep(_sitrep_from_json(payload)) if kind == "sitrep" else self._publish_focus(project, payload)
            if not done:
                return

    def _sweep_archive(self) -> None:
        """At most once a day, move closed task sitreps 30 days old to the archive path."""

        now = self._now()
        last = self._state.mark("archive_sweep")
        if last is not None and now - last < ARCHIVE_SWEEP_INTERVAL:
            return
        try:
            snapshot = self._snapshot()
            due = {record.id for record in archive_due([item.record for item in snapshot.records], now=now)}
            files = sorted((item for item in snapshot.records if item.record.id in due), key=lambda item: item.path)
            batch = files[:ARCHIVE_BATCH]
            if batch:
                self._storage.archive_records(
                    paths=[item.path for item in batch], expected_revision=snapshot.revision.commit_sha
                )
                for item in batch:
                    self._state.forget(item.record.data["project"], item.record.data["session_id"])
            if len(files) <= ARCHIVE_BATCH:
                self._state.set_mark("archive_sweep", now)
        except Exception as exc:  # the next sweep tries again; nothing is lost meanwhile
            logger.warning("Closed sitreps could not be archived yet (%s); the next sweep retries", type(exc).__name__)

    # --- the PC ------------------------------------------------------------------------------

    def _refresh_focus(self, project: str, *, force: bool) -> bool:
        """Copy the project's Compass Focus line from the PC. False only if Brain Home refused it."""

        now = self._now()
        checked = self._state.mark(f"focus:{project}")
        if not force and checked is not None and now - checked < FOCUS_REFRESH_INTERVAL:
            return True
        self._state.set_mark(f"focus:{project}", now)
        try:
            value = self._connector.read_compass_focus(project)
        except ConnectorUnavailable:
            return True  # the PC is off: the copy keeps the time it was made
        except ConnectorError as exc:
            logger.warning("Compass Focus for %s could not be read from the PC connector (%s)", project, exc.code)
            return True
        section = value.get("focus") if value.get("compass") is True else None
        section = section if isinstance(section, str) and section.strip() else None
        payload = json.dumps(
            {"line": focus_line(section), "section_sha256": None if section is None else _sha256(section)},
            sort_keys=True,
            separators=(",", ":"),
        )
        if self._state.wanted("focus_copy", project) == (payload, True):
            return True
        self._state.want("focus_copy", project, "", payload, now)
        return self._publish_focus(project, payload)

    def _seed(
        self, project: str, session_id: str, operation: BrainOperation, pointer: dict[str, Any]
    ) -> TaskSitrep | None:
        """A starting point for a task the hub has no sitrep for in its own state."""

        try:
            current = current_sitreps([item.record for item in self._snapshot().records], project).get(session_id)
        except Exception as exc:
            if operation is BrainOperation.CONTINUITY_CLOSE:
                raise  # a closed note can't be read back from the PC to rebuild from
            logger.warning("Brain Home unreadable while seeding a sitrep (%s); using the note itself", type(exc).__name__)
            current = None
        if current is not None:
            return TaskSitrep.from_record(current)
        if operation is BrainOperation.CONTINUITY_CLOSE:
            return None  # never summarised, now closed: there is nothing to show or close
        note = self._connector.recover_continuity(project, session_id=session_id, max_tokens=SEED_MAX_TOKENS)
        return seed_sitrep(
            project=project,
            session_id=session_id,
            intent=str(note["intent"]),
            # A note may start with no next step (step B's N12c); its sitrep then has none either.
            next_step=str(note.get("next_step") or ""),
            blockers=[str(value) for value in note.get("blockers", ())],
            **pointer,
        )

    def _next_sitrep(
        self, project: str, operation: BrainOperation, request: Any, result: ContinuityWriteResult
    ) -> TaskSitrep | None:
        content_sha256 = None if result.receipt is None else result.receipt.content_sha256
        if not content_sha256:
            raise ValueError("the project note write returned no content hash")
        pointer = {
            "note_record_id": result.record_id,
            "note_content_sha256": content_sha256,
            "note_updated_at": result.updated_at,
        }
        known = self._state.wanted("sitrep", project, request.session_id)
        previous = None if known is None else _sitrep_from_json(known[0])
        if previous is not None and result.updated_at < previous.note_updated_at:
            return None  # a replay of an earlier write: the sitrep already shows later ones
        if operation is BrainOperation.CONTINUITY_START:
            return start_sitrep(
                project=project, session_id=request.session_id, intent=request.intent,
                next_step=request.next_step or "", **pointer,
            )
        if previous is None:
            previous = self._seed(project, request.session_id, operation, pointer)
            if previous is None:
                return None
        if operation is BrainOperation.CONTINUITY_APPEND:
            return apply_event(previous, event=request.event, value=request.value, **pointer)
        return close_sitrep(previous, recovered=request.recovered, **pointer)

    # --- what the runtime calls ----------------------------------------------------------------

    def after_write(
        self,
        project: str,
        operation: BrainOperation,
        request: ContinuityStartRequest | ContinuityAppendRequest | ContinuityCloseRequest,
        result: ContinuityWriteResult,
    ) -> bool:
        """Bring the task's sitrep and the project's Focus copy up to date after a saved note.

        Returns False when either is still behind in Brain Home; it stays queued and is retried.
        """

        with self._lock:
            ok = True
            try:
                wanted = self._next_sitrep(project, operation, request, result)
            except Exception as exc:  # the note is saved; the reply says the summary is behind
                logger.warning(
                    "Sitrep for %s task %s could not be worked out (%s)",
                    project, request.session_id, type(exc).__name__,
                )
                wanted, ok = None, False
            if wanted is not None:
                self._state.want("sitrep", project, request.session_id, _sitrep_json(wanted), self._now())
                ok = self._publish_sitrep(wanted) and ok
            ok = self._refresh_focus(project, force=True) and ok
            if ok:
                self._retry_pending()
                self._sweep_archive()
            return ok

    def project_view(self, project: str, *, refresh_focus: bool) -> dict[str, Any]:
        """The project sitrep, worked out now from Brain Home."""

        with self._lock:
            if refresh_focus:
                self._refresh_focus(project, force=False)
            self._retry_pending()
            self._sweep_archive()
            return project_sitrep([item.record for item in self._snapshot().records], project)

    def list_view(
        self, project: str, *, limit: int, refresh_focus: bool
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Open and blocked tasks from their sitreps, and the project sitrep, from one read."""

        with self._lock:
            if refresh_focus:
                self._refresh_focus(project, force=False)
            self._retry_pending()
            self._sweep_archive()
            records = [item.record for item in self._snapshot().records]
            return sitrep_list(records, project, limit=limit), project_sitrep(records, project)

    def close(self) -> None:
        self._state.close()

    async def aclose(self) -> None:
        self.close()


def _summary_line(as_of: datetime | None, *, pc_off: bool, now: datetime | None = None) -> str:
    rest = "the full note needs the PC" if pc_off else "the full notes need access to this project"
    if as_of is None:
        return f"no summary saved yet; {rest}"
    # His day, not UTC's: 22:00 UTC on 30 Sep is already Thu 1 Oct in Singapore.
    return f"summary as of {relative_day_words(personal_today(as_of), personal_today(now) if now else None)}; {rest}"


class SitrepBrainRuntime(BrainRuntime):
    """``BrainRuntime`` with ``[sitreps]`` on. Only project continuity calls change."""

    def __init__(self, *, sitreps: SitrepService, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._sitreps = sitreps

    async def _summarised(
        self, selector: OwnerSelector, operation: BrainOperation, request: Any, result: ContinuityWriteResult
    ) -> ContinuityWriteResult:
        if selector.kind != "project" or selector.project_id is None:
            return result
        try:
            updated = await anyio.to_thread.run_sync(
                self._sitreps.after_write, selector.project_id, operation, request, result
            )
        except Exception as exc:  # the note is saved whatever happens here; the reply says so
            logger.error("Sitrep update failed after a saved project note (%s)", type(exc).__name__)
            updated = False
        if updated:
            return result
        return ContinuityWriteResultWithSummary(**{field.name: getattr(result, field.name) for field in fields(result)})

    async def start_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityStartRequest
    ) -> ContinuityWriteResult:
        result = await super().start_continuity(caller, selector, request)
        return await self._summarised(selector, BrainOperation.CONTINUITY_START, request, result)

    async def append_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityAppendRequest
    ) -> ContinuityWriteResult:
        result = await super().append_continuity(caller, selector, request)
        return await self._summarised(selector, BrainOperation.CONTINUITY_APPEND, request, result)

    async def close_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityCloseRequest
    ) -> ContinuityWriteResult:
        result = await super().close_continuity(caller, selector, request)
        return await self._summarised(selector, BrainOperation.CONTINUITY_CLOSE, request, result)

    async def _may_read_sitreps(self, caller: BrainCaller) -> bool:
        try:
            await self._authorize(caller, BrainOperation.SITREP_READ, OwnerSelector("cross-project"), "read")
        except (BrainRuntimeError, PermissionError):
            return False
        return True

    async def list_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityListRequest
    ) -> ContinuityListResult:
        if selector.kind != "project" or selector.project_id is None:
            return await super().list_continuity(caller, selector, request)
        project = selector.project_id
        try:
            live = await super().list_continuity(caller, selector, request)
        except BrainRuntimeError as exc:
            if exc.code not in {"brain.capability.unavailable", "brain.policy.excluded"}:
                raise
            if not await self._may_read_sitreps(caller):
                raise
            pc_off = exc.code == "brain.capability.unavailable"
            try:
                items, view = await anyio.to_thread.run_sync(
                    partial(self._sitreps.list_view, project, limit=request.limit, refresh_focus=not pc_off)
                )
            except Exception as read_error:
                raise BrainRuntimeError(
                    "brain.capability.unavailable",
                    "The project's summary could not be read from Brain Home",
                    retryable=True,
                ) from read_error
            as_of = None if view["as_of"] is None else datetime.fromisoformat(view["as_of"])
            return SitrepListResult(
                items=tuple(
                    SitrepListItem(
                        record_id=item["record_id"],
                        session_id=item["session_id"],
                        intent=item["intent"],
                        next_step=item["next_step"],
                        state=item["state"],
                        updated_at=datetime.fromisoformat(item["updated_at"]),
                        blockers=tuple(item["blockers"]),
                    )
                    for item in items
                ),
                summary=_summary_line(as_of, pc_off=pc_off, now=self._sitreps._now()),
                as_of=as_of,
                project_sitrep=view,
            )
        if not await self._may_read_sitreps(caller):
            return live
        try:
            view = await anyio.to_thread.run_sync(partial(self._sitreps.project_view, project, refresh_focus=True))
        except Exception as exc:  # the live notes are the answer; the extra view is logged as missing
            logger.warning("Project sitrep for %s could not be read (%s)", project, type(exc).__name__)
            return live
        return ContinuityListWithProjectSitrep(items=live.items, project_sitrep=view)

    async def recover_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityRecoverRequest
    ) -> ContinuityRecovery:
        try:
            return await super().recover_continuity(caller, selector, request)
        except BrainRuntimeError as exc:
            if selector.kind == "project" and exc.code == "brain.capability.unavailable":
                raise BrainRuntimeError("brain.capability.unavailable", NEEDS_PC, retryable=True) from exc
            raise


__all__ = [
    "ContinuityListWithProjectSitrep",
    "ContinuityWriteResultWithSummary",
    "NEEDS_PC",
    "NOT_UPDATED",
    "SitrepBrainRuntime",
    "SitrepListItem",
    "SitrepListResult",
    "SitrepService",
    "SitrepState",
]
