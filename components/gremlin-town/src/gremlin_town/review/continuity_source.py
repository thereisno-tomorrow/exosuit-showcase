"""Starting the running note for orientation's `yes`/`other` (step F spec §6).

Writes go only through Brain Core's existing public continuity record shape (the same
dict `brain_core.continuity.start_continuity` builds, via `record_from_mapping`) and
`GitHubBrainHomeStorage.publish_record` - not a new write path, and not step C2's
not-yet-merged v2 format (a v1 continuity record, same as every continuity note today).
`publish_record` already gives the "confirmed record or a clear failure" guarantee step A
built for Brain writes: same-content republish is a no-op, a lost reply is reconciled
against the actual branch head, and anything else raises.

Idempotent by the same contract `start_continuity` uses, adapted from `RecordStore` to
`GitHubBrainHomeStorage`: an existing open continuity record for one `session_id` is left
exactly as it is, never overwritten, never treated as an error - replaying the same `yes`
must return the same note (step F spec §6). Implements `orient.ContinuityStarter`; wired
only into `OrientationContext` (`runtime.py`), never into anything step E owns.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Callable, Protocol

from brain_core.continuity import continuity_data
from brain_core.github_storage import SNAPSHOT_MAX_BYTES, SNAPSHOT_MAX_RECORDS, GitHubConflict
from brain_core.records import RECORD_SCHEMA, Evidence, Record, new_record_id, record_from_mapping, render_record
from brain_core.review import HANDOFF_NOTE_PREFIX

from gremlin_town.review.brain_source import SnapshotSource

_CONTINUITY_KIND = frozenset({"continuity"})


class ContinuityWriteFailed(RuntimeError):
    """A continuity write did not end in a confirmed record (step A's guarantee)."""


class ContinuityPublisher(SnapshotSource, Protocol):
    """`SnapshotSource` (to check for an existing note) plus the one write this needs."""

    def publish_record(self, *, record_bytes: bytes) -> object: ...


def _existing_note(
    source: SnapshotSource,
    session_id: str,
    task_id: str | None = None,
    *,
    max_records: int = SNAPSHOT_MAX_RECORDS,
    max_bytes: int = SNAPSHOT_MAX_BYTES,
) -> Record | None:
    """The open note for this session - or, given `task_id`, any open note linked to that task
    (N12b), whatever it is called. Newest versions only: a note's older versions stay in Brain
    Home after it closes, and must never count as it still being open. The scan stays within
    ``max_records`` and ``max_bytes`` (the hub passes its own limits, through its copy)."""

    found = source.snapshot_records(kinds=_CONTINUITY_KIND, max_records=max_records, max_bytes=max_bytes)
    records = tuple(file.record for file in found.records)
    replaced = {old for record in records for old in record.supersedes}
    for record in records:
        if record.id in replaced or record.state != "open":
            continue
        data = record.data
        if data.get("session_id") == session_id:
            return record
        linked = task_id is not None and data.get("task_id") == task_id
        if linked and not str(data.get("session_id", "")).startswith(HANDOFF_NOTE_PREFIX):
            return record
    return None


def _build_record(
    *,
    session_id: str,
    actor: str,
    next_step: str,
    now: datetime,
    task_id: str | None,
    intent: str | None,
    owner_id: str,
    owner_locator: str | None,
) -> Record:
    evidence = Evidence(
        source_type="orientation",
        locator=f"orient:{session_id}",
        content_sha256=hashlib.sha256(next_step.encode("utf-8")).hexdigest(),
        excerpt=next_step,
        actor=actor,
    )
    return record_from_mapping(
        {
            "schema": RECORD_SCHEMA,
            "id": new_record_id(now),
            "kind": "continuity",
            "title": f"Continuity for {session_id}",
            "state": "open",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "assertion": "explicit",
            "confidence": 1.0,
            "sensitivity": "private",
            "vaults": [] if owner_locator is None else [owner_locator],
            "owners": [owner_id],
            "entities": [],
            "evidence": [evidence.as_dict()],
            "supersedes": [],
            "dedupe_key": f"continuity:{session_id}",
            "data": continuity_data(
                session_id=session_id,
                intent=intent or f"Take the tracked item's first step: {session_id}",
                sources=(),
                next_step=next_step,
                task_id=task_id,
            ),
            "body": "",
        },
        f"orient:{session_id}",
    )


class GitHubContinuityStarter:
    """`orient.ContinuityStarter` over `GitHubBrainHomeStorage` (or any `ContinuityPublisher`).

    `owner_id`/`owner_locator` stamp the note the way Brain Home's own continuity tools do
    (`runtime.py` passes the personal owner from the Brain owner policy), so agents' own
    `bc_continuity_list`/`bc_continuity_recover` find it too. Left as they were (`nic`, no
    vault) when not given. ``max_records`` and ``max_bytes`` bound its scan for an open note:
    Brain Core's defaults (512 files, 8 MiB) unless given; `runtime.py` passes the hub's limits
    and its copy of Brain Home (the storage-limit plan, layer 1)."""

    def __init__(
        self,
        storage: ContinuityPublisher,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        owner_id: str = "nic",
        owner_locator: str | None = None,
        max_records: int = SNAPSHOT_MAX_RECORDS,
        max_bytes: int = SNAPSHOT_MAX_BYTES,
    ) -> None:
        self._storage = storage
        self._now = now
        self._owner_id = owner_id
        self._owner_locator = owner_locator
        self._limits = {"max_records": max_records, "max_bytes": max_bytes}

    def start(
        self,
        *,
        session_id: str,
        actor: str,
        next_step: str,
        task_id: str | None = None,
        intent: str | None = None,
    ) -> object:
        """`next_step` may be empty: a yes with no first step leaves it so (N12c). `task_id`
        links the note to its task (N12b); an open note already linked to that task counts as
        this one, so a yes never starts a second note for the same task."""

        existing = _existing_note(self._storage, session_id, task_id, **self._limits)
        if existing is not None:
            return existing

        record = _build_record(
            session_id=session_id, actor=actor, next_step=next_step, now=self._now(), task_id=task_id,
            intent=intent, owner_id=self._owner_id, owner_locator=self._owner_locator,
        )
        try:
            result = self._storage.publish_record(record_bytes=render_record(record).encode("utf-8"))
        except GitHubConflict as exc:
            if exc.code == "github_record_conflict":
                # Someone else's concurrent start won the race; the same idempotent
                # contract applies - their note is this session's note now.
                again = _existing_note(self._storage, session_id, task_id, **self._limits)
                if again is not None:
                    return again
            raise ContinuityWriteFailed(f"could not confirm the note for {session_id!r}") from exc
        status = getattr(result, "status", None)
        if status not in ("created", "duplicate/no-op", "reconciled"):
            raise ContinuityWriteFailed(f"unexpected publish outcome {status!r} for {session_id!r}")
        return result
