"""Incremental continuity records and bounded crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from brain_core.contracts import TOKENIZER_ID, canonical_json
from brain_core.errors import BrainError, ErrorCode
from brain_core.records import Evidence, RECORD_SCHEMA, Record, RecordStore, WriteReceipt, new_record_id, record_from_mapping
from brain_core.retrieval import token_count, tokenize

_LIST_EVENTS = {
    "source": "sources",
    "effect": "effects",
    "decision": "decisions",
    "commitment": "commitments",
    "candidate": "candidates",
    "blocker": "blockers",
}


@dataclass(frozen=True, slots=True)
class ContinuityWrite:
    record: Record
    receipt: WriteReceipt | None
    changed: bool


def continuity_data(
    *,
    session_id: str,
    intent: str,
    sources: Iterable[str],
    next_step: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """A new note's ``data``. ``task_id`` (N12b) is left out entirely when the note names no
    task, so such a note keeps exactly the shape every note had before 28 Sep."""

    data: dict[str, Any] = {
        "session_id": session_id,
        "intent": intent,
        "sources": list(dict.fromkeys(sources)),
        "effects": [],
        "decisions": [],
        "commitments": [],
        "candidates": [],
        "blockers": [],
        "next_step": next_step,
    }
    if task_id is not None:
        data["task_id"] = task_id
    return data


def start_continuity(
    store: RecordStore,
    *,
    session_id: str,
    intent: str,
    sources: Iterable[str],
    evidence: Evidence,
    now: datetime,
    next_step: str = "Continue the declared intent.",
    entropy: str | None = None,
    task_id: str | None = None,
) -> ContinuityWrite:
    """``task_id`` links the note to its task's record ID (N12b), whatever the note is called.
    ``next_step`` may be empty: a note started with no first step says so (N12c)."""

    existing = [record for record in store.current(kind="continuity") if record.data["session_id"] == session_id]
    if existing:
        raise BrainError(ErrorCode.RECORD_EXISTS, f"Continuity session already exists: {session_id}", details={"session_id": session_id})
    record = record_from_mapping(
        {
            "schema": RECORD_SCHEMA,
            "id": new_record_id(now, entropy),
            "kind": "continuity",
            "title": f"Continuity for {session_id}",
            "state": "open",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "assertion": "explicit",
            "confidence": 1.0,
            "sensitivity": "private",
            "vaults": [],
            "owners": ["nic"],
            "entities": [],
            "evidence": [evidence.as_dict()],
            "supersedes": [],
            "dedupe_key": f"continuity:{session_id}",
            "data": continuity_data(
                session_id=session_id, intent=intent, sources=sources, next_step=next_step, task_id=task_id,
            ),
            "body": "",
        },
        f"continuity:{session_id}",
    )
    return ContinuityWrite(record=record, receipt=store.create(record), changed=True)


def _session_record(store: RecordStore, session_id: str) -> Record:
    matches = [record for record in store.current(kind="continuity") if record.data["session_id"] == session_id]
    if not matches:
        raise BrainError(ErrorCode.CONTINUITY_EMPTY, f"No open continuity for session: {session_id}", details={"session_id": session_id})
    return max(matches, key=lambda record: (record.updated_at, record.id))


def append_event(
    store: RecordStore,
    *,
    session_id: str,
    event: str,
    value: str,
    evidence: Evidence,
    now: datetime,
    meaningful: bool = True,
    entropy: str | None = None,
) -> ContinuityWrite:
    previous = _session_record(store, session_id)
    if previous.state == "closed":
        raise BrainError(ErrorCode.RECORD_TRANSITION, "Closed continuity cannot accept events", details={"record_id": previous.id})
    if not meaningful:
        return ContinuityWrite(record=previous, receipt=None, changed=False)
    updates: dict[str, Any] = {}
    if event in _LIST_EVENTS:
        field = _LIST_EVENTS[event]
        values = list(previous.data[field])
        if value in values:
            return ContinuityWrite(record=previous, receipt=None, changed=False)
        values.append(value)
        updates[field] = values
    elif event in {"intent", "next_step"}:
        if previous.data[event] == value:
            return ContinuityWrite(record=previous, receipt=None, changed=False)
        updates[event] = value
    else:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, f"Unknown continuity event: {event}", details={"event": event, "supported": sorted((*_LIST_EVENTS, "intent", "next_step"))})
    revised, receipt = store.revise(previous.id, now=now, data=updates, evidence=(evidence,), entropy=entropy)
    return ContinuityWrite(record=revised, receipt=receipt, changed=True)


def close_continuity(
    store: RecordStore,
    *,
    session_id: str,
    evidence: Evidence,
    now: datetime,
    recovered: bool = False,
    entropy: str | None = None,
) -> ContinuityWrite:
    previous = _session_record(store, session_id)
    target = "recovered" if recovered else "closed"
    revised, receipt = store.revise(previous.id, now=now, state=target, evidence=(evidence,), entropy=entropy)
    return ContinuityWrite(record=revised, receipt=receipt, changed=True)


def list_continuity(store: RecordStore, *, limit: int = 20) -> list[dict[str, Any]]:
    """List open notes, newest first, bounded — for "what did I ask, what's still open"."""

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "limit must be from 1 through 100", details={"limit": limit})
    candidates = [record for record in store.current(kind="continuity") if record.state in {"open", "recovered"}]
    candidates.sort(key=lambda record: (record.updated_at, record.id), reverse=True)
    return [
        {
            "record_id": record.id,
            "session_id": record.data["session_id"],
            "intent": record.data["intent"],
            "next_step": record.data["next_step"],
            "state": record.state,
            "updated_at": record.updated_at,
            "task_id": record.data.get("task_id"),
        }
        for record in candidates[:limit]
    ]


def _truncate(value: str, maximum: int) -> str:
    tokens = tokenize(value)
    if len(tokens) <= maximum:
        return value
    if maximum <= 0:
        return ""
    if maximum == 1:
        return "…"
    return " ".join(tokens[: maximum - 1]) + " …"


def recover_continuity(store: RecordStore, *, session_id: str | None = None, max_tokens: int = 2_500) -> dict[str, Any]:
    if max_tokens < 80:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "max_tokens must be at least 80", details={"max_tokens": max_tokens})
    candidates = [record for record in store.current(kind="continuity") if record.state in {"open", "recovered"}]
    if session_id is not None:
        candidates = [record for record in candidates if record.data["session_id"] == session_id]
    if not candidates:
        detail: dict[str, Any] = {} if session_id is None else {"session_id": session_id}
        message = "No recoverable continuity record" if session_id is None else f"No recoverable continuity for session: {session_id}"
        raise BrainError(ErrorCode.CONTINUITY_EMPTY, message, details=detail)
    record = max(candidates, key=lambda item: (item.updated_at, item.id))
    brief: dict[str, Any] = {
        "record_id": record.id,
        "session_id": record.data["session_id"],
        "state": record.state,
        "intent": record.data["intent"],
        "sources": list(record.data["sources"]),
        "effects": list(record.data["effects"]),
        "decisions": list(record.data["decisions"]),
        "commitments": list(record.data["commitments"]),
        "candidates": list(record.data["candidates"]),
        "blockers": list(record.data["blockers"]),
        "next_step": record.data["next_step"],
        "evidence": [item.as_dict() for item in record.evidence],
        "updated_at": record.updated_at.isoformat(),
        "tokenizer": TOKENIZER_ID,
        "dropped": {},
        "tokens": 0,
    }
    if record.data.get("task_id") is not None:
        # N12b: only a note linked to a task carries it, so every other brief is unchanged.
        brief["task_id"] = record.data["task_id"]
    drop_order = ("effects", "candidates", "evidence", "sources", "decisions", "commitments", "blockers")
    while token_count(canonical_json(brief)) > max_tokens and any(brief[field] for field in drop_order):
        for field in drop_order:  # pragma: no branch - guarded by any() above
            if brief[field]:
                brief[field].pop(0)
                brief["dropped"][field] = brief["dropped"].get(field, 0) + 1
                break
    for field in drop_order:
        if not brief[field]:
            brief.pop(field)
    if token_count(canonical_json(brief)) > max_tokens:
        original_intent = brief["intent"]
        original_next_step = brief["next_step"]
        brief["intent"] = _truncate(original_intent, 1)
        brief["next_step"] = _truncate(original_next_step, 1)
        for field in ("tokenizer", "updated_at", "state", "session_id"):  # pragma: no branch - 80 tokens fits before exhaustion
            if token_count(canonical_json(brief)) <= max_tokens:
                break
            brief.pop(field)
        scalar_budget = max(1, max_tokens // 4)
        while True:
            brief["intent"] = _truncate(original_intent, scalar_budget)
            brief["next_step"] = _truncate(original_next_step, scalar_budget)
            if token_count(canonical_json(brief)) <= max_tokens:
                break
            scalar_budget -= 1
    brief["tokens"] = token_count(canonical_json(brief))
    return brief
