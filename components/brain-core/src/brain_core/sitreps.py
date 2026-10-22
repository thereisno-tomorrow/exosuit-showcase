"""Task sitreps and Focus copies: the hub's small copies of project state in Brain Home.

Pure and deterministic, with no I/O and no model call (docs/product/steps/b-project-sitreps.md).
Gremlin Town's hub is their only writer: it builds each record here and publishes it through
Brain Home storage. The project keeps the canonical notes and Compass; these are copies.

A task sitrep holds only the orientation fields of one project task's "where I stopped" note:
its intent, its state (open, blocked or closed), its next step, its first three blockers, when
the note last changed, and a pointer to the full note (project, task, record ID, content hash).
Each text field is one line of at most about 200 characters. It never holds effects, sources,
decisions, candidates or chat text: those events only move the pointer.

A Focus copy holds the first sentence of Focus in the Compass of the vault that holds the
project. A project sitrep is never stored: `project_sitrep` works it out from the task sitreps
and the Focus copy each time it is asked for, so it cannot drift from them.

Step F reads every project's open and blocked tasks the same way (`open_tasks_by_project`), for
the "Focus projects" section of "where am I?" and the project tasks "what next?" ranks
(docs/product/steps/f-orientation.md §4, §13).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable

from brain_core.contracts import canonical_json
from brain_core.errors import BrainError, ErrorCode
from brain_core.records import (
    RECORD_SCHEMA,
    SITREP_FIELD_MAX_CHARS,
    SITREP_MAX_BLOCKERS,
    Evidence,
    Record,
    new_record_id,
    record_from_mapping,
)

SITREP_KIND = "sitrep"
FOCUS_COPY_KIND = "focus_copy"
OPEN_LIMIT = 10
BLOCKED_LIMIT = 10
ARCHIVE_AFTER = timedelta(days=30)
# Events whose values a sitrep never copies: they only move its pointer to the new note version.
_POINTER_ONLY_EVENTS = frozenset({"source", "effect", "decision", "commitment", "candidate"})
_ELLIPSIS = "…"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
# A sentence ends at . ! or ? followed by the end of the text or by a new capitalised sentence,
# so "e.g. the API" stays one sentence.
_SENTENCE_END = re.compile(r"[.!?](?=\s+[\"'(\[]?[A-Z]|\s*$)")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def one_line(value: str, limit: int = SITREP_FIELD_MAX_CHARS) -> str:
    """Collapse all whitespace to single spaces; cap at ``limit`` characters, ending in … if cut."""

    if not isinstance(value, str):
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "Sitrep text must be a string", details={"actual_type": type(value).__name__})
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + _ELLIPSIS


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, f"{field} must be a timezone-aware datetime", details={"field": field})
    return value


def _note_time(record: Record) -> datetime:
    return datetime.fromisoformat(str(record.data["note_updated_at"]).replace("Z", "+00:00"))


def _first_blockers(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        line = one_line(value)
        if line and line not in result:
            result.append(line)
        if len(result) == SITREP_MAX_BLOCKERS:
            break
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TaskSitrep:
    """One project task's orientation fields, as the hub derived them from its note."""

    project: str
    session_id: str
    intent: str
    state: str
    next_step: str
    blockers: tuple[str, ...]
    note_updated_at: datetime
    note_record_id: str
    note_content_sha256: str

    def data(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "session_id": self.session_id,
            "intent": self.intent,
            "next_step": self.next_step,
            "blockers": list(self.blockers),
            "note_updated_at": self.note_updated_at.isoformat(),
            "note": {"record_id": self.note_record_id, "content_sha256": self.note_content_sha256},
        }

    @classmethod
    def from_record(cls, record: Record) -> "TaskSitrep":
        if record.kind != SITREP_KIND:
            raise BrainError(ErrorCode.INVALID_ARGUMENT, "Not a task sitrep record", details={"record_id": record.id})
        data = record.data
        return cls(
            project=data["project"],
            session_id=data["session_id"],
            intent=data["intent"],
            state=record.state,
            next_step=data["next_step"],
            blockers=tuple(data["blockers"]),
            note_updated_at=_note_time(record),
            note_record_id=data["note"]["record_id"],
            note_content_sha256=data["note"]["content_sha256"],
        )


def start_sitrep(
    *,
    project: str,
    session_id: str,
    intent: str,
    next_step: str,
    note_record_id: str,
    note_content_sha256: str,
    note_updated_at: datetime,
) -> TaskSitrep:
    """The sitrep for a note that was just started: open, with its intent and first next step."""

    return TaskSitrep(
        project, session_id, one_line(intent), "open", one_line(next_step), (),
        _aware(note_updated_at, "note_updated_at"), note_record_id, note_content_sha256,
    )


def seed_sitrep(
    *,
    project: str,
    session_id: str,
    intent: str,
    next_step: str,
    blockers: Iterable[str],
    note_record_id: str,
    note_content_sha256: str,
    note_updated_at: datetime,
) -> TaskSitrep:
    """A sitrep built from a whole open note the hub has no sitrep for yet (it counts as open)."""

    return TaskSitrep(
        project, session_id, one_line(intent), "open", one_line(next_step), _first_blockers(blockers),
        _aware(note_updated_at, "note_updated_at"), note_record_id, note_content_sha256,
    )


def apply_event(
    previous: TaskSitrep,
    *,
    event: str,
    value: str,
    note_record_id: str,
    note_content_sha256: str,
    note_updated_at: datetime,
) -> TaskSitrep:
    """The sitrep after one continuity event was appended to its note.

    A blocker makes the task ``blocked`` (the first three are kept); a later next step means
    work moved on, so it is ``open`` again. Intent and next step replace their lines. The
    other events are never copied: they only move the pointer to the new note version.
    """

    if previous.state == "closed":
        raise BrainError(
            ErrorCode.RECORD_TRANSITION,
            "A closed task sitrep accepts no further events",
            details={"session_id": previous.session_id},
        )
    pointer = {
        "note_record_id": note_record_id,
        "note_content_sha256": note_content_sha256,
        "note_updated_at": _aware(note_updated_at, "note_updated_at"),
    }
    if event == "intent":
        return replace(previous, intent=one_line(value), **pointer)
    if event == "next_step":
        return replace(previous, next_step=one_line(value), state="open", **pointer)
    if event == "blocker":
        return replace(previous, blockers=_first_blockers((*previous.blockers, value)), state="blocked", **pointer)
    if event in _POINTER_ONLY_EVENTS:
        return replace(previous, **pointer)
    raise BrainError(ErrorCode.INVALID_ARGUMENT, f"Unknown continuity event: {event}", details={"event": event})


def close_sitrep(
    previous: TaskSitrep,
    *,
    recovered: bool,
    note_record_id: str,
    note_content_sha256: str,
    note_updated_at: datetime,
) -> TaskSitrep:
    """The sitrep after its note was closed. A note marked recovered stays listed, as it does."""

    return replace(
        previous,
        state=previous.state if recovered else "closed",
        note_record_id=note_record_id,
        note_content_sha256=note_content_sha256,
        note_updated_at=_aware(note_updated_at, "note_updated_at"),
    )


def _entropy(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _check_previous(previous: Record | None, kind: str, key: tuple[str, ...]) -> None:
    if previous is None:
        return
    actual = (previous.data.get("project"),) + ((previous.data.get("session_id"),) if kind == SITREP_KIND else ())
    if previous.kind != kind or actual != key:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "The previous record belongs to a different sitrep",
            details={"record_id": previous.id, "kind": previous.kind},
        )


def _record(
    *,
    kind: str,
    state: str,
    title: str,
    dedupe_key: str,
    data: dict[str, Any],
    previous: Record | None,
    now: datetime,
    evidence: Evidence,
    owner_id: str,
    owner_locator: str,
) -> Record:
    now = _aware(now, "now")
    entropy = _entropy(kind, dedupe_key, state, canonical_json(data), previous.id if previous is not None else "")
    return record_from_mapping(
        {
            "schema": RECORD_SCHEMA,
            "id": new_record_id(now, entropy),
            "kind": kind,
            "title": title,
            "state": state,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "assertion": "system",
            "confidence": 1.0,
            "sensitivity": "private",
            "vaults": [owner_locator],
            "owners": [owner_id],
            "entities": [],
            "evidence": [evidence.as_dict()],
            # One link back is enough to tell the newest version, and keeps every version small.
            "supersedes": [] if previous is None else [previous.id],
            "dedupe_key": dedupe_key,
            "data": data,
            "body": "",
        },
        dedupe_key,
    )


def sitrep_record(
    sitrep: TaskSitrep,
    *,
    previous: Record | None,
    now: datetime,
    evidence: Evidence,
    owner_id: str,
    owner_locator: str,
) -> Record:
    """The next version of one task's sitrep record, superseding ``previous`` when there is one."""

    _check_previous(previous, SITREP_KIND, (sitrep.project, sitrep.session_id))
    return _record(
        kind=SITREP_KIND,
        state=sitrep.state,
        title=one_line(f"Sitrep for {sitrep.project} task {sitrep.session_id}"),
        dedupe_key=f"sitrep:{sitrep.project}:{sitrep.session_id}",
        data=sitrep.data(),
        previous=previous,
        now=now,
        evidence=evidence,
        owner_id=owner_id,
        owner_locator=owner_locator,
    )


def focus_copy_record(
    *,
    project: str,
    line: str | None,
    section_sha256: str | None,
    previous: Record | None,
    now: datetime,
    evidence: Evidence,
    owner_id: str,
    owner_locator: str,
) -> Record:
    """The next version of a project's Focus copy; ``line`` is None when there is no Focus."""

    _check_previous(previous, FOCUS_COPY_KIND, (project,))
    return _record(
        kind=FOCUS_COPY_KIND,
        state="current",
        title=one_line(f"Focus copy for {project}"),
        dedupe_key=f"focus_copy:{project}",
        data={"project": project, "line": line, "section_sha256": section_sha256},
        previous=previous,
        now=now,
        evidence=evidence,
        owner_id=owner_id,
        owner_locator=owner_locator,
    )


def same_sitrep(record: Record, sitrep: TaskSitrep) -> bool:
    """True when ``record`` already says exactly what ``sitrep`` says."""

    return record.kind == SITREP_KIND and record.state == sitrep.state and record.data == sitrep.data()


def _heads(records: Iterable[Record]) -> list[Record]:
    items = list(records)
    superseded = {record_id for record in items for record_id in record.supersedes}
    return [record for record in items if record.id not in superseded]


def _newest(records: Iterable[Record]) -> Record | None:
    return max(records, key=lambda record: (record.updated_at, record.id), default=None)


def current_sitreps(records: Iterable[Record], project: str) -> dict[str, Record]:
    """The newest version of each task sitrep in ``project``, keyed by its task (session_id)."""

    heads: dict[str, list[Record]] = {}
    for record in _heads(item for item in records if item.kind == SITREP_KIND and item.data.get("project") == project):
        heads.setdefault(record.data["session_id"], []).append(record)
    return {session_id: _newest(versions) for session_id, versions in heads.items()}  # type: ignore[misc]


def current_focus_copy(records: Iterable[Record], project: str) -> Record | None:
    """The newest version of ``project``'s Focus copy, if the hub has made one."""

    return _newest(_heads(item for item in records if item.kind == FOCUS_COPY_KIND and item.data.get("project") == project))


def _live(heads: dict[str, Record]) -> list[Record]:
    return sorted(heads.values(), key=lambda record: (_note_time(record), record.id), reverse=True)


def _as_of(heads: dict[str, Record], focus: Record | None) -> datetime | None:
    """When the hub last wrote any of a project's task sitreps or its Focus copy (S5)."""

    written = [record.updated_at for record in (*heads.values(), *((focus,) if focus is not None else ()))]
    return max(written) if written else None


def sitrep_list(records: Iterable[Record], project: str, *, limit: int) -> list[dict[str, Any]]:
    """Open and blocked task sitreps in ``project``, newest note first, at most ``limit``."""

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "limit must be from 1 through 100", details={"limit": limit})
    listed = [record for record in _live(current_sitreps(records, project)) if record.state in {"open", "blocked"}]
    return [
        {
            "record_id": record.data["note"]["record_id"],
            "session_id": record.data["session_id"],
            "intent": record.data["intent"],
            "next_step": record.data["next_step"],
            "state": record.state,
            "updated_at": _note_time(record).isoformat(),
            "blockers": list(record.data["blockers"]),
        }
        for record in listed[:limit]
    ]


def project_sitrep(records: Iterable[Record], project: str) -> dict[str, Any]:
    """The whole project at a glance, worked out now from its task sitreps and Focus copy.

    The Focus line is left out when the vault that holds the project has no Compass (or no
    Focus). ``as_of`` is when the hub last wrote any of it; ``None`` when it never has.
    """

    items = tuple(records)
    heads = current_sitreps(items, project)
    focus = current_focus_copy(items, project)
    live = _live(heads)
    open_tasks = [record for record in live if record.state == "open"]
    blocked = [record for record in live if record.state == "blocked"]
    result: dict[str, Any] = {"project": project}
    if focus is not None and focus.data.get("line"):
        result["focus"] = {"line": focus.data["line"], "copied_at": focus.updated_at.isoformat()}
    result["open"] = [
        {
            "session_id": record.data["session_id"],
            "intent": record.data["intent"],
            "next_step": record.data["next_step"],
            "updated_at": _note_time(record).isoformat(),
        }
        for record in open_tasks[:OPEN_LIMIT]
    ]
    result["open_total"] = len(open_tasks)
    result["blocked"] = [
        {
            "session_id": record.data["session_id"],
            "intent": record.data["intent"],
            "blocker": record.data["blockers"][0] if record.data["blockers"] else None,
            "updated_at": _note_time(record).isoformat(),
        }
        for record in blocked[:BLOCKED_LIMIT]
    ]
    result["blocked_total"] = len(blocked)
    latest = live[0] if live else None
    result["latest_activity"] = None if latest is None else {
        "session_id": latest.data["session_id"],
        "intent": latest.data["intent"],
        "state": latest.state,
        "updated_at": _note_time(latest).isoformat(),
    }
    as_of = _as_of(heads, focus)
    result["as_of"] = as_of.isoformat() if as_of is not None else None
    return result


@dataclass(frozen=True, slots=True)
class ProjectTasks:
    """One project's open and blocked tasks from its task sitreps, newest note first, and when
    the hub last wrote any of its summary (``as_of``, as `project_sitrep` says it). What step F
    reads for Nic's focus projects (docs/product/steps/f-orientation.md §4)."""

    project: str
    as_of: datetime | None
    tasks: tuple[TaskSitrep, ...] = ()


def open_tasks_by_project(records: Iterable[Record]) -> tuple[ProjectTasks, ...]:
    """Every project the hub has written a task sitrep or Focus copy for, by project ID: all of
    its open and blocked tasks, newest note first, and its "as of". Worked out now from the
    records, like `project_sitrep`, so it cannot drift from them; closed tasks are left out."""

    items = tuple(records)
    projects = sorted({str(record.data["project"]) for record in items if record.kind in {SITREP_KIND, FOCUS_COPY_KIND}})
    result: list[ProjectTasks] = []
    for project in projects:
        heads = current_sitreps(items, project)
        live = [record for record in _live(heads) if record.state in {"open", "blocked"}]
        result.append(ProjectTasks(
            project=project,
            as_of=_as_of(heads, current_focus_copy(items, project)),
            tasks=tuple(TaskSitrep.from_record(record) for record in live),
        ))
    return tuple(result)


def archive_due(records: Iterable[Record], *, now: datetime) -> tuple[Record, ...]:
    """Every version of each task sitrep whose newest version was closed at least 30 days ago.

    The closed time is when its note was closed. The result is what step C's archive path
    moves out of the live set, whole revision chains at a time.
    """

    now = _aware(now, "now")
    chains: dict[tuple[str, str], list[Record]] = {}
    for record in records:
        if record.kind == SITREP_KIND:
            chains.setdefault((record.data["project"], record.data["session_id"]), []).append(record)
    due: list[Record] = []
    for versions in chains.values():
        head = _newest(_heads(versions))
        if head is not None and head.state == "closed" and now - _note_time(head) >= ARCHIVE_AFTER:
            due.extend(versions)
    return tuple(sorted(due, key=lambda record: record.id))


def focus_section(compass_text: str) -> str | None:
    """The text under a Compass's ``Focus`` heading, up to the next heading of the same level."""

    if not isinstance(compass_text, str):
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "Compass text must be a string", details={"actual_type": type(compass_text).__name__})
    level = 0
    body: list[str] = []
    for line in compass_text.splitlines():
        heading = _HEADING.match(line)
        if level:
            if heading is not None and len(heading.group(1)) <= level:
                break
            body.append(line)
        elif heading is not None and heading.group(2).casefold() == "focus":
            level = len(heading.group(1))
    text = "\n".join(body).strip()
    return text or None


def focus_line(section: str | None) -> str | None:
    """The first sentence of a Focus section, one line of at most about 200 characters."""

    if section is None or not section.strip():
        return None
    paragraph = next(block for block in _PARAGRAPH_BREAK.split(section.strip()) if block.strip())
    lines = [line.strip() for line in paragraph.splitlines()]
    if _LIST_MARKER.match(lines[0]):
        first = [_LIST_MARKER.sub("", lines[0])]
        for line in lines[1:]:
            if _LIST_MARKER.match(line):
                break
            first.append(line)
        lines = first
    text = " ".join(line.removeprefix(">").strip() for line in lines).strip()
    match = _SENTENCE_END.search(text)
    sentence = one_line(text[: match.end()] if match is not None else text)
    return sentence or None


__all__ = [
    "ARCHIVE_AFTER",
    "BLOCKED_LIMIT",
    "FOCUS_COPY_KIND",
    "OPEN_LIMIT",
    "SITREP_KIND",
    "ProjectTasks",
    "TaskSitrep",
    "apply_event",
    "archive_due",
    "close_sitrep",
    "current_focus_copy",
    "current_sitreps",
    "focus_copy_record",
    "focus_line",
    "focus_section",
    "one_line",
    "open_tasks_by_project",
    "project_sitrep",
    "same_sitrep",
    "seed_sitrep",
    "sitrep_list",
    "sitrep_record",
    "start_sitrep",
]
