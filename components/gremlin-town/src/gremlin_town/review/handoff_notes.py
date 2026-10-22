"""The hub keeps the "where I stopped" note for work handed through Gremlin Town (N12a).

Gremlin Town already sees every step of a request, so the hub keeps its note and no agent has
to remember (step B, B6; Ciel rebuild plan slice 2). Run from the moments poll
(`review/poll.py`), behind `[review] handoff_notes` (off by default; **Proposed — Nic to
confirm**), it reads the GT events since its own cursor in the why-log's `runs` table and makes
at most one note write per event, to the request's note `gt-<request_id>`:

- the recipient reports it started (`work_start`): start the note - its intent the request's
  title, its next step the report's reason if any (else empty), linked to the request's
  `task_id` when the request names one. A start that finds the note already open adds the
  reason as the next step instead;
- a blocker (`work_block`): add it as a blocker;
- `result_returned`: the next step becomes "Result returned, not yet checked.";
- `result_check_rejected`: the next step says so, with the check's evidence (**Proposed**);
- `result_check_accepted`: close the note;
- `request_cancelled`, and `work_decline` (**Proposed**): close it, the close's evidence saying
  why.

An event for a request with no open note (it started before this was switched on, or it never
reported a start) changes nothing. Requests the hub sends itself - the 06:00 review and daytime
updates from `[review] sender_participant` - never get a note.

**Writes.** The hub writes as itself: the evidence actor is `system:hub-notes`, never an agent
(rebuild plan G3; the id is **Proposed**). Notes live in Brain Home even for project requests,
stamped with the personal Brain Home owner so agents' own `bc_continuity_*` tools read them.
Each write stages the note's versions and runs Brain Core's own continuity functions, then
publishes the one new version through `GitHubBrainHomeStorage.publish_record` at the revision it
read: a confirmed record or a clear failure (step A), the same confirmed statuses
`continuity_source.py` accepts. **Replay-safe:** each written version carries the event's own
key as its `dedupe_key`, so an event already applied is found and skipped. **Concurrent
writes:** a branch that moved since the read is read again and the write retried, a few times.

**Failures.** A write that fails stops the pass at that event; the cursor stays before it, the
failure is recorded in the why-log's `runs` table (and logged), and the next poll retries it.
Nothing is dropped silently. On its very first pass the keeper starts from the newest event
rather than writing notes for every past request (**Proposed**).

Plain, deterministic Python: no model call, and nothing here sends a message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal, Mapping, Protocol

from brain_core.continuity import append_event, close_continuity, start_continuity
from brain_core.github_storage import GitHubConflict
from brain_core.records import Evidence, Record, RecordStore, render_record

from gremlin_town.contracts import Actor

if TYPE_CHECKING:
    from gremlin_town.review.run import ReviewContext

logger = logging.getLogger(__name__)

HUB_ACTOR = "system:hub-notes"  # Proposed - Nic to confirm (rebuild plan G3: "stamped as the hub")
CURSOR_KIND = "handoff_notes"
KEY_PREFIX = "gt-hub-note:"
RESULT_RETURNED_STEP = "Result returned, not yet checked."
HANDLED_EVENT_TYPES = frozenset({
    "work_start", "work_block", "work_decline",
    "result_returned", "result_check_accepted", "result_check_rejected",
    "request_cancelled",
})
MAX_PAGES_PER_POLL = 10  # up to 1,000 events a poll; the rest wait for the next one
PUBLISH_ATTEMPTS = 4
CONFIRMED_STATUSES = frozenset({"created", "duplicate/no-op", "reconciled"})
# The same bounds the hub's own Brain Home continuity uses (mcp/brain_home_copy.py): with the
# hub's copy of Brain Home, a scan costs one GitHub call plus any changed records.
SCAN_MAX_RECORDS = 5000
SCAN_MAX_BYTES = 32 * 1024 * 1024
_CONTINUITY = frozenset({"continuity"})


class HandoffNoteWriteFailed(RuntimeError):
    """A hub note write did not end in a confirmed record; the next poll retries it."""


@dataclass(frozen=True, slots=True)
class NoteChange:
    """One write to one request's note, made from one GT event."""

    key: str
    session_id: str
    action: Literal["start", "append", "close"]
    at: datetime
    excerpt: str
    content_sha256: str
    locator: str
    event: Literal["next_step", "blocker"] | None = None
    value: str | None = None
    intent: str | None = None
    next_step: str = ""
    task_id: str | None = None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _moment(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def change_for_event(event: Mapping[str, Any], request: Mapping[str, Any]) -> NoteChange | None:
    """The note write one GT event makes, if any. Pure: `request` is the request's list item."""

    event_type = event.get("event_type")
    if event_type not in HANDLED_EVENT_TYPES:
        return None
    request_id = str(event["request_id"])
    sequence = event["sequence"]
    detail = event.get("detail") or {}
    actor = str(event.get("actor_id") or "someone")
    reason = _text(detail.get("reason"))
    evidence_words = _text(detail.get("evidence"))
    base: dict[str, Any] = {
        "key": f"{KEY_PREFIX}{request_id}:{sequence}",
        "session_id": f"gt-{request_id}",
        "at": _moment(event["occurred_at"]),
        "content_sha256": hashlib.sha256(
            json.dumps(
                {
                    "sequence": sequence, "request_id": request_id, "event_type": event_type,
                    "actor_id": event.get("actor_id"), "detail": detail, "occurred_at": event.get("occurred_at"),
                },
                sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "locator": f"gt-event:{sequence}",
    }
    if event_type == "work_start":
        intent = _text(request.get("objective")) or f"Work handed to {request.get('recipient_id')}"
        task_id = request.get("task_id")
        return NoteChange(
            action="start", intent=intent, next_step=reason or "",
            task_id=task_id if isinstance(task_id, str) else None,
            excerpt=f"{actor} reported it started" + (f": {reason}" if reason else ""), **base,
        )
    if event_type == "work_block":
        if reason is None:
            return None
        return NoteChange(action="append", event="blocker", value=reason, excerpt=f"{actor} reported a blocker: {reason}", **base)
    if event_type == "result_returned":
        if request.get("proof") and request.get("proof_verified"):
            return NoteChange(action="close", excerpt="The result returned with proof", **base)
        return NoteChange(
            action="append", event="next_step", value=RESULT_RETURNED_STEP,
            excerpt="The result was returned; nobody has checked it yet", **base,
        )
    if event_type == "result_check_rejected":
        step = "Result checked and not accepted" + (f": {evidence_words}" if evidence_words else ".")
        return NoteChange(action="append", event="next_step", value=step, excerpt=f"{actor} checked the result and did not accept it", **base)
    if event_type == "result_check_accepted":
        return NoteChange(
            action="close",
            excerpt=f"{actor} checked the result and accepted it" + (f": {evidence_words}" if evidence_words else ""),
            **base,
        )
    if event_type == "request_cancelled":
        return NoteChange(action="close", excerpt=f"{actor} cancelled the request" + (f": {reason}" if reason else ""), **base)
    # work_decline
    return NoteChange(action="close", excerpt=f"{actor} declined the request" + (f": {reason}" if reason else ""), **base)


class NoteStorage(Protocol):
    """What `GitHubBrainHomeStorage` offers that keeping notes needs."""

    def snapshot_records(self, *, kinds: frozenset[str], max_records: int, max_bytes: int) -> Any: ...

    def publish_record(self, *, record_bytes: bytes, expected_revision: str | None = None) -> Any: ...


def _record_path(record: Record) -> str:
    moment = record.created_at.astimezone(UTC)
    return f"records/{record.kind}/{moment:%Y}/{moment:%m}/{record.id}.md"


class HandoffNoteWriter:
    """Applies `NoteChange`s to Brain Home, one confirmed record at a time."""

    def __init__(
        self,
        storage: NoteStorage,
        *,
        owner_id: str,
        owner_locator: str,
        max_records: int = SCAN_MAX_RECORDS,
        max_bytes: int = SCAN_MAX_BYTES,
        attempts: int = PUBLISH_ATTEMPTS,
    ) -> None:
        self._storage = storage
        self._owner_id = owner_id
        self._owner_locator = owner_locator
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._attempts = attempts

    def _owned(self, record: Record) -> bool:
        return self._owner_locator in record.vaults and self._owner_id in record.owners

    def apply(self, change: NoteChange) -> str:
        """`written`, or why nothing was: `already_written` (a replay), `no_note`, `note_closed`,
        `exists` or `unchanged`. Raises when a write could not be confirmed."""

        for _attempt in range(self._attempts):
            snapshot = self._storage.snapshot_records(
                kinds=_CONTINUITY, max_records=self._max_records, max_bytes=self._max_bytes,
            )
            items = tuple(item for item in snapshot.records if self._owned(item.record))
            if any(item.record.dedupe_key == change.key for item in items):
                return "already_written"
            with tempfile.TemporaryDirectory(prefix="gt-hub-notes-") as directory:
                root = Path(directory)
                for item in items:
                    target = root / _record_path(item.record)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(item.content)
                outcome, record = self._plan(RecordStore(root), change)
            if record is None:
                return outcome
            record = replace(record, owners=(self._owner_id,), vaults=(self._owner_locator,), dedupe_key=change.key)
            try:
                published = self._storage.publish_record(
                    record_bytes=render_record(record).encode("utf-8"),
                    expected_revision=snapshot.revision.commit_sha,
                )
            except GitHubConflict as exc:
                if exc.code == "github_revision_conflict":
                    continue  # the branch moved since the read: read again and retry
                raise HandoffNoteWriteFailed(f"could not confirm the note write for {change.session_id!r}") from exc
            status = getattr(published, "status", None)
            if status not in CONFIRMED_STATUSES:
                raise HandoffNoteWriteFailed(f"unexpected publish outcome {status!r} for {change.session_id!r}")
            return "written"
        raise HandoffNoteWriteFailed(f"Brain Home changed repeatedly while writing {change.session_id!r}")

    def _plan(self, store: RecordStore, change: NoteChange) -> tuple[str, Record | None]:
        versions = [record for record in store.current(kind="continuity") if record.data["session_id"] == change.session_id]
        note = max(versions, key=lambda record: (record.updated_at, record.id)) if versions else None
        evidence = Evidence(
            source_type="gremlin_town_event",
            locator=change.locator,
            content_sha256=change.content_sha256,
            excerpt=change.excerpt,
            actor=HUB_ACTOR,
        )
        # A note's versions never go back in time, even if an agent wrote to it after the event.
        at = change.at if note is None else max(change.at, note.updated_at)
        entropy = hashlib.sha256(change.key.encode("utf-8")).hexdigest()[:16]
        if change.action == "start":
            if note is None:
                started = start_continuity(
                    store, session_id=change.session_id, intent=change.intent or change.session_id, sources=(),
                    evidence=evidence, now=at, next_step=change.next_step, entropy=entropy, task_id=change.task_id,
                )
                return "written", started.record
            if note.state == "closed":
                return "note_closed", None
            if change.next_step:
                # Already open (an agent started it, or work resumed): the reason is the next step.
                resumed = append_event(
                    store, session_id=change.session_id, event="next_step", value=change.next_step,
                    evidence=evidence, now=at, entropy=entropy,
                )
                return ("written", resumed.record) if resumed.changed else ("exists", None)
            return "exists", None
        if note is None:
            return "no_note", None
        if note.state == "closed":
            return "note_closed", None
        if change.action == "append":
            assert change.event is not None and change.value is not None
            appended = append_event(
                store, session_id=change.session_id, event=change.event, value=change.value,
                evidence=evidence, now=at, entropy=entropy,
            )
            return ("written", appended.record) if appended.changed else ("unchanged", None)
        closed = close_continuity(store, session_id=change.session_id, evidence=evidence, now=at, entropy=entropy)
        return "written", closed.record


class _EventsSource(Protocol):
    def events_list(self, actor: Actor, filters: Mapping[str, Any], cursor: str | None = None) -> Any: ...

    def requests_list(self, actor: Actor, filters: Mapping[str, Any], cursor: str | None = None) -> Any: ...


def _events_after(service: _EventsSource, actor: Actor, cursor: str | None, *, max_pages: int) -> Iterator[Mapping[str, Any]]:
    current = cursor
    for _page in range(max_pages):
        page = service.events_list(actor, {}, current)
        yield from page.items
        if not page.next_cursor or not page.items:
            return
        current = page.next_cursor


def _latest_sequence(service: _EventsSource, actor: Actor) -> int:
    latest = 0
    for event in _events_after(service, actor, None, max_pages=1_000_000):
        latest = max(latest, int(event["sequence"]))
    return latest


def _request(service: _EventsSource, actor: Actor, request_id: str) -> Mapping[str, Any] | None:
    page = service.requests_list(actor, {"request_id": request_id, "include_recovery_held": True})
    return page.items[0] if page.items else None


def _describe(exc: Exception) -> str:
    """The failure as the runs table and the journal say it: the plain sentence for a Brain Home
    size limit or GitHub's used-up allowance, saying the note wasn't saved (the storage-limit plan,
    layer 1), else the error's type and code."""

    from gremlin_town.review.brain_source import brain_problem

    problem = brain_problem(exc, nothing_saved=True)  # a note write that failed: nothing was saved
    if problem is not None:
        return problem[0]
    code = getattr(getattr(exc, "code", None), "value", getattr(exc, "code", None))
    return f"{type(exc).__name__}" + (f" {code}" if isinstance(code, str) else "")


def keep_handoff_notes(context: ReviewContext, now: datetime) -> dict[str, object]:
    """One pass over the GT events since the cursor. Returns what it did, including any failure."""

    settings = context.settings
    writer = context.handoff_notes
    why_log = context.why_log
    if writer is None or why_log is None:
        raise RuntimeError("[review] handoff_notes is on but no note writer or why-log was built")
    service: _EventsSource = context.service  # type: ignore[assignment]
    actor = Actor(actor_id=settings.sender_participant)

    cursor = why_log.latest_cursor(CURSOR_KIND)
    if cursor is None:
        # First pass: start from now rather than writing notes for every past request (Proposed).
        latest = _latest_sequence(service, actor)
        why_log.record_run(kind=CURSOR_KIND, started_at=now, last_event_cursor=str(latest), outcome="started")
        return {"kind": CURSOR_KIND, "outcome": "started", "cursor": str(latest), "written": 0}

    requests: dict[str, Mapping[str, Any] | None] = {}
    last_good = cursor
    written = 0
    failure: str | None = None
    for event in _events_after(service, actor, cursor, max_pages=MAX_PAGES_PER_POLL):
        if event.get("event_type") in HANDLED_EVENT_TYPES:
            request_id = str(event["request_id"])
            if request_id not in requests:
                requests[request_id] = _request(service, actor, request_id)
            request = requests[request_id]
            # The hub's own deliveries (the review and daytime updates) are not handed-off work.
            own = request is not None and request.get("requester_id") == settings.sender_participant
            change = None if request is None or own else change_for_event(event, request)
            if change is not None:
                try:
                    outcome = writer.apply(change)
                except Exception as exc:
                    from gremlin_town.review.brain_source import lasting_brain_problem

                    failure = f"{event['event_type']} at {event['sequence']}: {_describe(exc)}"
                    if lasting_brain_problem(exc) is not None:
                        # A size limit won't pass by itself: every poll fails the same way until
                        # Brain Home is tidied up (the storage-limit plan, layer 1).
                        logger.error("Handoff note write failed and will keep failing until fixed (%s)", failure)
                    else:
                        logger.warning("Handoff note write failed; the next poll retries it (%s)", failure)
                    break
                written += outcome == "written"
        last_good = str(event["sequence"])

    if failure is not None or last_good != cursor:
        why_log.record_run(
            kind=CURSOR_KIND, started_at=now, last_event_cursor=last_good,
            outcome=f"failed: {failure}" if failure else f"written: {written}",
        )
    result: dict[str, object] = {
        "kind": CURSOR_KIND, "outcome": "failed" if failure else "checked", "cursor": last_good, "written": written,
    }
    if failure:
        result["failure"] = failure
    return result
