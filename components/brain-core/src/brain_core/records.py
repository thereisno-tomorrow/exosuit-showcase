"""Evidence-bearing records, append-only lifecycle changes, and atomic storage."""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ruamel.yaml import YAML

from brain_core.contracts import canonical_json, parse_yaml, stable_hash
from brain_core.errors import BrainError, ErrorCode

RECORD_SCHEMA = "brain.record/v1"
RECORD_SCHEMA_V1 = RECORD_SCHEMA
RECORD_SCHEMA_V2 = "brain.record/v2"
RECORD_KINDS = frozenset({"capture", "commitment", "decision", "continuity", "entity", "preference"})
RECORD_KINDS_V2 = frozenset({"item", "fact", "continuity", "entity", "preference"})
ASSERTIONS = frozenset({"explicit", "inferred", "imported", "system"})
SENSITIVITIES = frozenset({"public", "private", "restricted", "local-only"})
ITEM_LEVELS = frozenset({"possible", "suggested", "accepted"})
ITEM_SIZES = frozenset({"task", "goal"})
ITEM_CALLED = frozenset({"goal", "project"})
STAKE_LEVELS = frozenset({"changes_life", "hurts_weeks", "nuisance", "nothing_much"})
REVIEW_MARKS = frozenset({"agents_wording", "maybe_same", "unclear", "question", "looks_like", "ask_stake", "spelling"})
DATE_KINDS = frozenset({"deadline", "appointment", "planned", "not_before", "suggested", "start_by", "done"})
WORK_FIELDS = frozenset({
    "parent", "order", "called", "next_action", "whose", "expected", "after", "needs", "dates",
    "finish_line", "labels", "started_at", "stake", "review_marks", "words_from",
})
ITEM_STATES = frozenset({"open", "waiting", "blocked", "parked", "done", "dropped", "no_longer_needed"})
SOURCE_SAID_BY = frozenset({"owner", "agent", "other"})
SOURCE_CAME_BY = frozenset({"direct", "agent_guess", "connected_source", "system"})
LINK_TYPES = frozenset(
    {"part_of", "waiting_on", "blocked_by", "handed_to", "about", "replaces", "came_from"}
)
COMMITMENT_CONDITIONS = (
    "recurring",
    "waiting",
    "overdue",
    "blocked",
    "conflicting",
    "duplicate",
    "stale",
    "unowned",
)
MAX_ENTITY_LINKS = 50
# Proposed - Nic approves before release speed-02 goes live (docs/product/
# 2026-09-26-gtbc-speed-goal.md Wave 3; aliases borrowed from the Instinct
# teardown). Read side and validation only: no save tool sets this field yet.
MAX_ALIASES = 5
MAX_ALIAS_LENGTH = 80
# Ciel redesign N5 (docs/product/steps/c-record-format.md §13), widened by the Ciel and Brain Home
# PRD §3 (Accepted - Nic, 28 Sep 2026): an item's stake. "high" is "can't undo" and counts only
# once Nic confirms it; "real_cost" and "minor" are agents' labelled guesses that count as saved.
# No field means nothing is known about the stake.
IMPORTANCE_LEVELS = frozenset({"high", "real_cost", "minor"})
# PRD §3: the kind of block a task belongs in (time blocks, PRD §4.3). Absent means unsorted.
ITEM_BLOCKS = frozenset({"deep_focus", "admin", "errands"})
MAX_RAISE_WITH_LENGTH = 128
# PRD §4.1: a next action's first step, the very next physical action, on one line.
MAX_FIRST_STEP_LENGTH = 256
# Ciel redesign N4 (step C §13). Proposed - Nic to confirm: a project ID is any short text
# until project sitreps (ROA-60) settle how a project is named; a focus list is short.
MAX_FOCUS_PROJECTS = 10
MAX_FOCUS_PROJECT_LENGTH = 128
# Step I (placement engine; PRD §4.3, Accepted - Nic, 29 Sep 2026): Nic's working window, per
# weekday, inside which Ciel draws each day's blocks. A day left out, or null, is a day off.
WORKING_WINDOW_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Step I §6 (Nic, 29 Sep 2026: "dont pad my responses just take them"): a length Nic gave himself
# is marked with this `estimated_by` and booked as he said it, so its padded minutes equal his.
OWNER_ESTIMATE = "nic"
# Task sitreps and Focus copies (docs/product/steps/b-project-sitreps.md): small copies of a
# project's notes and Compass Focus line that only Gremlin Town's hub writes. v1 only, always
# `assertion: system`, and never canonical: the project keeps the notes and the Compass. Kept
# out of RECORD_KINDS so every other reader and save path stays exactly as it was.
SITREP_KINDS = frozenset({"sitrep", "focus_copy"})
SITREP_FIELD_MAX_CHARS = 200
SITREP_MAX_BLOCKERS = 3
_RECORD_ID = re.compile(r"^brn_(\d{8})_(\d{6})_([0-9a-f]{4,16})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ORIGIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEMANTIC_MUTEX_GUARD = threading.Lock()
_SEMANTIC_MUTEXES: dict[str, Any] = {}


def _semantic_mutex(lock_path: Path) -> Any:
    key = os.path.normcase(str(lock_path.resolve(strict=False)))
    with _SEMANTIC_MUTEX_GUARD:
        return _SEMANTIC_MUTEXES.setdefault(key, threading.RLock())


@contextmanager
def _semantic_file_lock(lock_path: Path) -> Iterable[None]:
    """Hold one advisory byte lock for the complete semantic claim."""

    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise BrainError(
            ErrorCode.RECORD_WRITE_FAILED,
            "Could not open the semantic writer lock",
            details={"path": str(lock_path), "error": type(exc).__name__},
        ) from exc
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise BrainError(
            ErrorCode.RECORD_WRITE_FAILED,
            "Could not hold the semantic writer lock",
            details={"path": str(lock_path), "error": type(exc).__name__},
        ) from exc
    finally:
        handle.close()

STATES: dict[str, frozenset[str]] = {
    "capture": frozenset({"inbox", "triaged", "routed", "archived"}),
    "commitment": frozenset(
        {"proposed", "confirmed", "active", "waiting", "blocked", "done", "cancelled", "superseded"}
    ),
    "decision": frozenset({"proposed", "accepted", "superseded", "reversed"}),
    "continuity": frozenset({"open", "closed", "recovered"}),
    "entity": frozenset({"active", "dormant", "retired"}),
    "preference": frozenset({"proposed", "active", "retired"}),
    "item": ITEM_STATES,
    "fact": frozenset({"current", "outdated"}),
    "sitrep": frozenset({"open", "blocked", "closed"}),
    "focus_copy": frozenset({"current"}),
}

TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "capture": {
        "inbox": frozenset({"triaged", "archived"}),
        "triaged": frozenset({"routed", "archived"}),
        "routed": frozenset({"archived"}),
        "archived": frozenset(),
    },
    "commitment": {
        "proposed": frozenset({"confirmed", "cancelled", "superseded"}),
        "confirmed": frozenset({"active", "waiting", "blocked", "done", "cancelled", "superseded"}),
        "active": frozenset({"waiting", "blocked", "done", "cancelled", "superseded"}),
        "waiting": frozenset({"active", "blocked", "done", "cancelled", "superseded"}),
        "blocked": frozenset({"active", "waiting", "done", "cancelled", "superseded"}),
        "done": frozenset(),
        "cancelled": frozenset(),
        "superseded": frozenset(),
    },
    "decision": {
        "proposed": frozenset({"accepted", "superseded"}),
        "accepted": frozenset({"superseded", "reversed"}),
        "superseded": frozenset(),
        "reversed": frozenset(),
    },
    "continuity": {
        "open": frozenset({"closed", "recovered"}),
        "recovered": frozenset({"closed"}),
        "closed": frozenset(),
    },
    "entity": {
        "active": frozenset({"dormant", "retired"}),
        "dormant": frozenset({"active", "retired"}),
        "retired": frozenset(),
    },
    "preference": {
        "proposed": frozenset({"active", "retired"}),
        "active": frozenset({"retired"}),
        "retired": frozenset(),
    },
    "item": {
        "open": frozenset({"waiting", "blocked", "parked", "done", "dropped", "no_longer_needed"}),
        "waiting": frozenset({"open", "blocked", "parked", "done", "dropped", "no_longer_needed"}),
        "blocked": frozenset({"open", "waiting", "parked", "done", "dropped", "no_longer_needed"}),
        "parked": frozenset({"open", "waiting", "blocked", "done", "dropped", "no_longer_needed"}),
        "done": frozenset(),
        "dropped": frozenset(),
        "no_longer_needed": frozenset(),
    },
    "fact": {
        "current": frozenset({"outdated"}),
        "outdated": frozenset(),
    },
    "sitrep": {
        "open": frozenset({"blocked", "closed"}),
        "blocked": frozenset({"open", "closed"}),
        "closed": frozenset(),
    },
    "focus_copy": {
        "current": frozenset(),
    },
}

_COMMON_FIELDS = frozenset(
    {
        "schema",
        "id",
        "kind",
        "title",
        "state",
        "created_at",
        "updated_at",
        "assertion",
        "confidence",
        "sensitivity",
        "vaults",
        "owners",
        "entities",
        "evidence",
        "supersedes",
        "dedupe_key",
        "data",
        "body",
        "source",
        "links",
    }
)


def _invalid(locator: str, field: str, expected: str, actual: Any) -> None:
    raise BrainError(
        ErrorCode.CONTRACT_INVALID,
        f"Invalid record field {field} in {locator}",
        details={"locator": locator, "field": field, "expected": expected, "actual_type": type(actual).__name__},
    )


def _string(value: Any, field: str, locator: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        _invalid(locator, field, "string" if empty else "non-empty string", value)
    return value


def _strings(value: Any, field: str, locator: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _invalid(locator, field, "list of non-empty strings", value)
    result = tuple(_string(item, f"{field}[]", locator) for item in value)
    if len(set(result)) != len(result):
        _invalid(locator, field, "unique strings", value)
    return result


def _mapping(value: Any, field: str, locator: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _invalid(locator, field, "mapping", value)
    return dict(value)


def _timestamp(value: Any, field: str, locator: str) -> datetime:
    raw = _string(value, field, locator)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"Invalid timestamp {field} in {locator}",
            details={"locator": locator, "field": field, "value": raw},
        ) from exc
    if parsed.tzinfo is None:
        _invalid(locator, field, "timezone-aware timestamp", value)
    return parsed


def _choice(value: Any, field: str, allowed: frozenset[str], locator: str) -> str:
    selected = _string(value, field, locator)
    if selected not in allowed:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"Invalid record field {field} in {locator}",
            details={"locator": locator, "field": field, "allowed": sorted(allowed), "actual": selected},
        )
    return selected


def _date_string(value: Any, field: str, locator: str) -> str:
    text = _string(value, field, locator)
    if _DATE.fullmatch(text) is None:
        _invalid(locator, field, "date YYYY-MM-DD", text)
    return text


def _required_data(data: Mapping[str, Any], locator: str, fields: Iterable[str]) -> None:
    for field in fields:
        _string(data.get(field), f"data.{field}", locator)


def _string_list_data(data: Mapping[str, Any], locator: str, fields: Iterable[str]) -> None:
    for field in fields:
        _strings(data.get(field), f"data.{field}", locator)


def _known_data(data: Mapping[str, Any], locator: str, allowed: Iterable[str], *, field: str = "data") -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        _invalid(locator, field, f"known fields only; unknown={unknown}", data)


@dataclass(frozen=True, slots=True)
class Evidence:
    source_type: str
    locator: str
    content_sha256: str
    excerpt: str = ""
    session_id: str | None = None
    actor: str | None = None
    grant_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_type": self.source_type,
            "locator": self.locator,
            "content_sha256": self.content_sha256,
            "excerpt": self.excerpt,
        }
        for name in ("session_id", "actor", "grant_sha256"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


def evidence_from_mapping(value: Mapping[str, Any], locator: str) -> Evidence:
    data = _mapping(value, "evidence[]", locator)
    allowed = {"source_type", "locator", "content_sha256", "excerpt", "session_id", "actor", "grant_sha256"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        _invalid(locator, "evidence[]", f"known fields only; unknown={unknown}", data)
    content_hash = _string(data.get("content_sha256"), "evidence[].content_sha256", locator)
    if not _SHA256.fullmatch(content_hash):
        _invalid(locator, "evidence[].content_sha256", "64 lowercase hexadecimal characters", content_hash)
    grant_hash = data.get("grant_sha256")
    if grant_hash is not None:
        grant_hash = _string(grant_hash, "evidence[].grant_sha256", locator)
        if not _SHA256.fullmatch(grant_hash):
            _invalid(locator, "evidence[].grant_sha256", "64 lowercase hexadecimal characters", grant_hash)
    optional: dict[str, str | None] = {}
    for field in ("session_id", "actor"):
        raw = data.get(field)
        optional[field] = None if raw is None else _string(raw, f"evidence[].{field}", locator)
    return Evidence(
        source_type=_string(data.get("source_type"), "evidence[].source_type", locator),
        locator=_string(data.get("locator"), "evidence[].locator", locator),
        content_sha256=content_hash,
        excerpt=_string(data.get("excerpt", ""), "evidence[].excerpt", locator, empty=True),
        session_id=optional["session_id"],
        actor=optional["actor"],
        grant_sha256=grant_hash,
    )


@dataclass(frozen=True, slots=True)
class Record:
    schema: str
    id: str
    kind: str
    title: str
    state: str
    created_at: datetime
    updated_at: datetime
    assertion: str | None
    confidence: float | None
    sensitivity: str
    vaults: tuple[str, ...]
    owners: tuple[str, ...]
    entities: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    supersedes: tuple[str, ...]
    dedupe_key: str | None
    data: dict[str, Any]
    body: str = ""
    source: dict[str, Any] | None = None
    links: tuple[dict[str, Any], ...] = ()

    def as_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": self.schema,
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "sensitivity": self.sensitivity,
            "vaults": list(self.vaults),
            "owners": list(self.owners),
            "entities": list(self.entities),
            "evidence": [item.as_dict() for item in self.evidence],
            "supersedes": list(self.supersedes),
            "dedupe_key": self.dedupe_key,
            "data": self.data,
        }
        if self.assertion is not None:
            result["assertion"] = self.assertion
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.source is not None:
            result["source"] = dict(self.source)
        if self.links:
            result["links"] = [dict(item) for item in self.links]
        if include_body:
            result["body"] = self.body
        return result

    @property
    def sha256(self) -> str:
        return stable_hash(self.as_dict())


def first_id(record: Record) -> str:
    """The lasting identity: the first superseded version, or this record's ID."""

    return record.supersedes[0] if record.supersedes else record.id


def first_saved(record: Record) -> datetime:
    """When the first version of a record was saved: the earliest of this version's own time and
    the moments its older versions' IDs carry (every record ID starts with its UTC save time)."""

    moments = [record.created_at]
    for older in record.supersedes:
        match = _RECORD_ID.fullmatch(older)
        if match is not None:
            moments.append(datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC))
    return min(moments)


_TIME_OF_DAY = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


def _time_of_day_string(value: Any, field: str, locator: str) -> str:
    text = _string(value, field, locator)
    if _TIME_OF_DAY.fullmatch(text) is None:
        _invalid(locator, field, "time HH:MM (00:00-23:59)", text)
    return text


def _validate_timing(timing: dict[str, Any], locator: str) -> None:
    # P2 (docs/product/steps/c-record-format.md §5; Proposed - Nic approves before
    # speed-02 goes live): an optional time of day beside `due`. `at` without `due` is
    # invalid - a time of day only means something next to a date.
    # PRD §3 (Accepted - Nic, 28 Sep 2026): `start_by`, a date to start by, only from Nic's words
    # like `due`; and `estimated_by`, who guessed the rough length, so it always reads as a guess.
    allowed = (
        "due", "at", "window", "on_event", "on_return", "duration_minutes", "padded_minutes",
        "resolved_from", "start_by", "estimated_by",
    )
    _known_data(timing, locator, allowed, field="data.timing")
    if timing.get("due") is not None:
        _date_string(timing["due"], "data.timing.due", locator)
    if timing.get("start_by") is not None:
        _date_string(timing["start_by"], "data.timing.start_by", locator)
    if timing.get("estimated_by") is not None:
        _string(timing["estimated_by"], "data.timing.estimated_by", locator)
        if timing.get("duration_minutes") is None:
            _invalid(locator, "data.timing.estimated_by", "duration_minutes present (it names who estimated it)", timing.get("estimated_by"))
        if (
            timing["estimated_by"] == OWNER_ESTIMATE
            and timing.get("padded_minutes") is not None
            and timing.get("padded_minutes") != timing.get("duration_minutes")
        ):
            _invalid(locator, "data.timing.padded_minutes", "equal to duration_minutes (Nic's own length is never padded)", timing.get("padded_minutes"))
    if timing.get("at") is not None:
        if timing.get("due") is None:
            _invalid(locator, "data.timing.at", "due present (at has no meaning without a date)", timing.get("at"))
        _time_of_day_string(timing["at"], "data.timing.at", locator)
    if timing.get("window") is not None:
        window = _mapping(timing["window"], "data.timing.window", locator)
        _known_data(window, locator, ("start", "end"), field="data.timing.window")
        _date_string(window.get("start"), "data.timing.window.start", locator)
        _date_string(window.get("end"), "data.timing.window.end", locator)
    for field in ("on_event", "on_return"):
        if timing.get(field) is not None:
            _string(timing[field], f"data.timing.{field}", locator)
    for field in ("duration_minutes", "padded_minutes"):
        value = timing.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            _invalid(locator, f"data.timing.{field}", "positive integer", value)
    if timing.get("resolved_from") is not None:
        resolved = _mapping(timing["resolved_from"], "data.timing.resolved_from", locator)
        _known_data(resolved, locator, ("words", "today"), field="data.timing.resolved_from")
        _string(resolved.get("words"), "data.timing.resolved_from.words", locator)
        _date_string(resolved.get("today"), "data.timing.resolved_from.today", locator)


def _sitrep_line(value: Any, field: str, locator: str, *, empty: bool = False) -> str:
    text = _string(value, field, locator, empty=empty)
    if len(text) > SITREP_FIELD_MAX_CHARS or any(character in text for character in "\r\n\0"):
        _invalid(locator, field, f"one line of at most {SITREP_FIELD_MAX_CHARS} characters", text)
    return text


def _validate_aliases(data: dict[str, Any], locator: str) -> None:
    # Proposed - Nic approves before release speed-02 goes live (see
    # MAX_ALIASES above). An item or fact's optional alternate-name list,
    # searched like the title. Left out of ``data`` entirely when absent, so
    # a record that never sets it keeps exactly today's shape.
    aliases = data.get("aliases")
    if aliases is None:
        return
    values = _strings(aliases, "data.aliases", locator)
    if len(values) > MAX_ALIASES:
        _invalid(locator, "data.aliases", f"at most {MAX_ALIASES} distinct aliases", aliases)
    for alias in values:
        if len(alias) > MAX_ALIAS_LENGTH:
            _invalid(locator, "data.aliases[]", f"at most {MAX_ALIAS_LENGTH} characters", alias)


def _validate_importance(data: dict[str, Any], locator: str) -> None:
    # Ciel redesign N5 (step C §13; Accepted - Nic, 28 Sep 2026): an item's
    # {level: high, reason, proposed_by, confirmed}. No field means normal, so a record that
    # never sets it keeps exactly today's shape, and None (a change back to normal) is dropped
    # rather than stored. language.check_change (R12) decides who may propose or confirm it.
    if "importance" not in data:
        return
    if data["importance"] is None:
        del data["importance"]
        return
    importance = _mapping(data["importance"], "data.importance", locator)
    _known_data(importance, locator, ("level", "reason", "proposed_by", "confirmed"), field="data.importance")
    _choice(importance.get("level"), "data.importance.level", IMPORTANCE_LEVELS, locator)
    _string(importance.get("reason"), "data.importance.reason", locator)
    _string(importance.get("proposed_by"), "data.importance.proposed_by", locator)
    if not isinstance(importance.get("confirmed"), bool):
        _invalid(locator, "data.importance.confirmed", "true or false", importance.get("confirmed"))
    data["importance"] = importance


def _validate_focus_projects(data: dict[str, Any], locator: str) -> None:
    # Ciel redesign N4 (step C §13; Accepted - Nic, 28 Sep 2026): a preference naming Nic's
    # focus projects; the newest one wins (language.focus_preference). An empty list names
    # none. Left out of data entirely when absent, like aliases.
    if "focus_projects" not in data:
        return
    projects = _strings(data["focus_projects"], "data.focus_projects", locator)
    if len(projects) > MAX_FOCUS_PROJECTS:
        _invalid(locator, "data.focus_projects", f"at most {MAX_FOCUS_PROJECTS} project IDs", data["focus_projects"])
    for project in projects:
        if len(project) > MAX_FOCUS_PROJECT_LENGTH:
            _invalid(locator, "data.focus_projects[]", f"at most {MAX_FOCUS_PROJECT_LENGTH} characters", project)


def _validate_working_window(data: dict[str, Any], locator: str) -> None:
    # Step I (PRD §4.3; Accepted - Nic, 29 Sep 2026): a preference holding Nic's working window;
    # the newest one wins (language.working_window). Each weekday is null (a day off) or a start
    # and an end, HH:MM Singapore time, the start before the end. Left out of data when absent.
    if "working_window" not in data:
        return
    window = _mapping(data["working_window"], "data.working_window", locator)
    _known_data(window, locator, WORKING_WINDOW_DAYS, field="data.working_window")
    for day, hours in window.items():
        field = f"data.working_window.{day}"
        if hours is None:
            continue
        hours = _mapping(hours, field, locator)
        _known_data(hours, locator, ("start", "end"), field=field)
        start = _time_of_day_string(hours.get("start"), f"{field}.start", locator)
        end = _time_of_day_string(hours.get("end"), f"{field}.end", locator)
        if start >= end:
            _invalid(locator, field, "a start before the end", hours)


def _work_text(value: Any, field: str, locator: str, limit: int) -> str:
    text = _string(value, field, locator)
    if len(text) > limit:
        _invalid(locator, field, f"at most {limit} characters", value)
    return text


def _work_id(value: Any, field: str, locator: str) -> str:
    text = _string(value, field, locator)
    if _RECORD_ID.fullmatch(text) is None:
        _invalid(locator, field, "a record ID brn_YYYYMMDD_HHMMSS_<hex>", value)
    return text


def _work_bool(value: Any, field: str, locator: str) -> None:
    if not isinstance(value, bool):
        _invalid(locator, field, "boolean", value)


def _validate_work_date(value: Any, kind: str, field: str, locator: str) -> None:
    entry = _mapping(value, field, locator)
    _known_data(entry, locator, (
        "words", "read_by", "read_on", "day", "at", "start", "end", "event", "kind", "speaker", "confirmed",
    ), field=field)
    _work_text(entry.get("words"), f"{field}.words", locator, 256)
    read_by = _string(entry.get("read_by"), f"{field}.read_by", locator)
    if read_by.startswith("system:"):
        _string(read_by[len("system:"):], f"{field}.read_by", locator)
    _date_string(entry.get("read_on"), f"{field}.read_on", locator)
    for key in ("day", "start", "end"):
        if key in entry:
            _date_string(entry[key], f"{field}.{key}", locator)
    if "at" in entry:
        _time_of_day_string(entry["at"], f"{field}.at", locator)
    for key in ("event", "speaker"):
        if key in entry:
            _string(entry[key], f"{field}.{key}", locator)
    if "kind" in entry:
        _choice(entry["kind"], f"{field}.kind", frozenset({"deadline", "appointment", "planned"}), locator)
    if "confirmed" in entry:
        _work_bool(entry["confirmed"], f"{field}.confirmed", locator)
    if kind in ("deadline", "appointment", "expected", "start_by", "suggested", "done"):
        _date_string(entry.get("day"), f"{field}.day", locator)
    if kind == "appointment":
        _time_of_day_string(entry.get("at"), f"{field}.at", locator)
    elif kind == "planned" and "day" not in entry:
        for key in ("start", "end"):
            _date_string(entry.get(key), f"{field}.{key}", locator)
    elif kind == "not_before" and not ({"day", "event"} & entry.keys()):
        _invalid(locator, field, "day or event present", value)
    elif kind == "suggested":
        _choice(entry.get("kind"), f"{field}.kind", frozenset({"deadline", "appointment", "planned"}), locator)
        _string(entry.get("speaker"), f"{field}.speaker", locator)


def _validate_work(data: dict[str, Any], locator: str, *, state: str | None) -> None:
    """Work-01 structure; door limits belong to W6, not this frozen parser."""

    if not WORK_FIELDS.intersection(data):
        return
    if data.get("size") == "goal":
        _invalid(locator, "data.size", "task alongside work fields", data["size"])
    for key in ("first_step", "importance"):
        if key in data:
            _invalid(locator, f"data.{key}", "absent alongside work fields", data[key])
    timing = _mapping(data.get("timing") or {}, "data.timing", locator)
    for key in ("due", "at", "window", "on_event", "on_return", "resolved_from", "start_by"):
        if key in timing:
            _invalid(locator, f"data.timing.{key}", "absent alongside work fields", timing[key])
    if state in ("waiting", "blocked"):
        _invalid(locator, "state", "not waiting or blocked alongside work fields", state)
    if "parent" in data:
        _work_id(data["parent"], "data.parent", locator)
    if "order" in data:
        value = data["order"]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 999:
            _invalid(locator, "data.order", "integer from 1 through 999", value)
    if "called" in data:
        _choice(data["called"], "data.called", ITEM_CALLED, locator)
    if "next_action" in data:
        entry = _mapping(data["next_action"], "data.next_action", locator)
        _known_data(entry, locator, ("text", "done"), field="data.next_action")
        text = _work_text(entry.get("text"), "data.next_action.text", locator, 256)
        if any(character in text for character in "\r\n\0"):
            _invalid(locator, "data.next_action.text", "one line", text)
        _work_bool(entry.get("done"), "data.next_action.done", locator)
    if "whose" in data:
        _work_text(data["whose"], "data.whose", locator, 128)
    if "expected" in data:
        _validate_work_date(data["expected"], "expected", "data.expected", locator)
    if "after" in data:
        after = _strings(data["after"], "data.after", locator)
        if not 1 <= len(after) <= 20:
            _invalid(locator, "data.after", "1 to 20 distinct first IDs", data["after"])
        for value in after:
            _work_id(value, "data.after[]", locator)
    if "needs" in data:
        entry = _mapping(data["needs"], "data.needs", locator)
        _known_data(entry, locator, ("from", "what", "expected"), field="data.needs")
        _work_text(entry.get("from"), "data.needs.from", locator, 128)
        if "what" in entry:
            _work_text(entry["what"], "data.needs.what", locator, 256)
        if "expected" in entry:
            _validate_work_date(entry["expected"], "expected", "data.needs.expected", locator)
    if "dates" in data:
        dates = _mapping(data["dates"], "data.dates", locator)
        _known_data(dates, locator, DATE_KINDS, field="data.dates")
        if "start_by" in dates and "deadline" not in dates:
            _invalid(locator, "data.dates.start_by", "deadline present", dates["start_by"])
        for kind, entry in dates.items():
            _validate_work_date(entry, kind, f"data.dates.{kind}", locator)
    if "finish_line" in data:
        _work_text(data["finish_line"], "data.finish_line", locator, 256)
    if "labels" in data:
        labels = _strings(data["labels"], "data.labels", locator)
        if not 1 <= len(labels) <= 5:
            _invalid(locator, "data.labels", "1 to 5 distinct texts", data["labels"])
        for value in labels:
            _work_text(value, "data.labels[]", locator, 64)
    if "started_at" in data:
        _timestamp(data["started_at"], "data.started_at", locator)
    if "stake" in data:
        entry = _mapping(data["stake"], "data.stake", locator)
        _known_data(entry, locator, ("level", "at_stake", "odds", "cost_shape", "confirmed", "proposed_by"), field="data.stake")
        if "level" in entry:
            _choice(entry["level"], "data.stake.level", STAKE_LEVELS, locator)
        for key in ("at_stake", "odds"):
            if key in entry:
                _string(entry[key], f"data.stake.{key}", locator)
        if "cost_shape" in entry:
            _choice(entry["cost_shape"], "data.stake.cost_shape", frozenset({"grows", "lands"}), locator)
        _work_bool(entry.get("confirmed"), "data.stake.confirmed", locator)
        _string(entry.get("proposed_by"), "data.stake.proposed_by", locator)
    if "review_marks" in data:
        marks = data["review_marks"]
        if not isinstance(marks, list) or len(marks) > 10:
            _invalid(locator, "data.review_marks", "list of up to 10 marks", marks)
        for raw in marks:
            field = "data.review_marks[]"
            entry = _mapping(raw, field, locator)
            _known_data(entry, locator, ("mark", "part", "as", "kind", "text"), field=field)
            mark = _choice(entry.get("mark"), f"{field}.mark", REVIEW_MARKS, locator)
            for key in ("part", "text"):
                if key in entry:
                    _string(entry[key], f"{field}.{key}", locator)
            if "as" in entry:
                _work_id(entry["as"], f"{field}.as", locator)
            if "kind" in entry or mark == "looks_like":
                _choice(entry.get("kind"), f"{field}.kind", ITEM_CALLED, locator)
            if mark == "spelling":
                _string(entry.get("text"), f"{field}.text", locator)
    if "words_from" in data:
        _choice(data["words_from"], "data.words_from", frozenset({"chat", "agent"}), locator)


def _validate_kind_data(kind: str, data: dict[str, Any], locator: str, sensitivity: str, *, state: str | None = None) -> None:
    if kind == "capture":
        _known_data(data, locator, ("channel", "original_source_hash", "route", "duplicate_of"))
        _required_data(data, locator, ("channel", "original_source_hash"))
        if not _SHA256.fullmatch(data["original_source_hash"]):
            _invalid(locator, "data.original_source_hash", "64 lowercase hexadecimal characters", data["original_source_hash"])
        for field in ("route", "duplicate_of"):
            if data.get(field) is not None:
                _string(data[field], f"data.{field}", locator)
    elif kind == "commitment":
        data.setdefault("conflict_ids", [])
        _known_data(
            data,
            locator,
            ("owner", "action", "time", "blocked_by", "decision_ids", "conflict_ids", "recurrence", "waiting_on", "duplicate_of"),
        )
        owner = data.get("owner")
        if owner is not None:
            _string(owner, "data.owner", locator)
        _required_data(data, locator, ("action",))
        _mapping(data.get("time", {}), "data.time", locator)
        _string_list_data(data, locator, ("blocked_by", "decision_ids", "conflict_ids"))
        for field in ("recurrence", "waiting_on", "duplicate_of"):
            if data.get(field) is not None:
                _string(data[field], f"data.{field}", locator)
    elif kind == "decision":
        _known_data(
            data,
            locator,
            (
                "because", "forecloses", "governs", "encoded_in", "completeness_check",
                "origin_id", "legacy_decision_id", "legacy_state", "legacy_date", "legacy_field_sha256",
            ),
        )
        _required_data(data, locator, ("because", "forecloses", "governs", "encoded_in", "completeness_check"))
        if data.get("origin_id") is not None:
            _string(data["origin_id"], "data.origin_id", locator)
    elif kind == "continuity":
        _known_data(
            data,
            locator,
            ("session_id", "intent", "sources", "effects", "decisions", "commitments", "candidates", "blockers", "next_step", "task_id"),
        )
        _required_data(data, locator, ("session_id", "intent"))
        # N12c (Ciel redesign, 28 Sep): a note started by a yes with no first step keeps an
        # empty next step until one is given, rather than a placeholder such as "Take the
        # first step." (step B, B6; step E §3.1 then asks Nic for one).
        _string(data.get("next_step"), "data.next_step", locator, empty=True)
        # N12b: the task this note belongs to, by its record ID (step C §13). Absent means
        # the note names no task; a record written before 28 Sep never carries it.
        if "task_id" in data:
            task_id = _string(data["task_id"], "data.task_id", locator)
            if _RECORD_ID.fullmatch(task_id) is None:
                _invalid(locator, "data.task_id", "a record ID brn_YYYYMMDD_HHMMSS_<hex>", task_id)
        _string_list_data(
            data,
            locator,
            ("sources", "effects", "decisions", "commitments", "candidates", "blockers"),
        )
    elif kind == "entity":
        _known_data(data, locator, ("entity_type", "why_it_matters", "privacy", "linked_events"))
        _choice(data.get("entity_type"), "data.entity_type", frozenset({"person", "organization"}), locator)
        _required_data(data, locator, ("why_it_matters",))
        privacy = _choice(data.get("privacy"), "data.privacy", SENSITIVITIES, locator)
        if privacy != sensitivity:
            _invalid(locator, "data.privacy", "equal to the record sensitivity", privacy)
        events = _strings(data.get("linked_events"), "data.linked_events", locator)
        if not events:
            _invalid(locator, "data.linked_events", "at least one meaningful linked event", events)
        if len(events) > MAX_ENTITY_LINKS:
            _invalid(locator, "data.linked_events", f"at most {MAX_ENTITY_LINKS} meaningful linked events", events)
    elif kind == "preference":
        _known_data(
            data, locator, ("scope", "stability", "evidence", "correction_history", "focus_projects", "working_window"),
        )
        _required_data(data, locator, ("scope",))
        _choice(data.get("stability"), "data.stability", frozenset({"durable", "conditional"}), locator)
        _string_list_data(data, locator, ("evidence", "correction_history"))
        _validate_focus_projects(data, locator)
        _validate_working_window(data, locator)
    elif kind == "item":
        data.setdefault("size", "task")
        for field in ("owed_to", "repeat", "timing", "reason", "check", "parked_until"):
            data.setdefault(field, None)
        _known_data(
            data,
            locator,
            (
                "level", "size", "owed_to", "repeat", "timing", "reason", "check", "parked_until", "aliases",
                "importance", "block", "raise_with", "first_step", "occurrence", "review_outcomes", "remaining_minutes", "planning", "material_kind", *WORK_FIELDS,
            ),
        )
        _choice(data.get("level"), "data.level", ITEM_LEVELS, locator)
        _choice(data.get("size"), "data.size", ITEM_SIZES, locator)
        _validate_work(data, locator, state=state)
        # PRD §3: the block type and who to raise it with. Left out of data when absent, so a
        # record that never sets them keeps exactly today's shape (as aliases).
        if "block" in data:
            if data["block"] is None:
                del data["block"]
            else:
                _choice(data["block"], "data.block", ITEM_BLOCKS, locator)
        if "raise_with" in data:
            if data["raise_with"] is None:
                del data["raise_with"]
            elif len(_string(data["raise_with"], "data.raise_with", locator)) > MAX_RAISE_WITH_LENGTH:
                _invalid(locator, "data.raise_with", f"at most {MAX_RAISE_WITH_LENGTH} characters", data["raise_with"])
        # PRD §4.7: the routine occurrence this version records done, kept like block when absent.
        if "material_kind" in data:
            _choice(data["material_kind"], "data.material_kind", frozenset({"information", "reflection", "idea", "arrangement"}), locator)
        if "planning" in data:
            from brain_core.planning import validate_planning
            try:
                validate_planning(data["planning"])
            except (ValueError, TypeError, KeyError) as exc:
                _invalid(locator, "data.planning", str(exc), data["planning"])
        if "remaining_minutes" in data:
            value = data["remaining_minutes"]
            if type(value) is not int or value <= 0:
                _invalid(locator, "data.remaining_minutes", "positive whole minutes", value)
        if "review_outcomes" in data:
            outcomes = data["review_outcomes"]
            if not isinstance(outcomes, list):
                _invalid(locator, "data.review_outcomes", "list", outcomes)
            for outcome in outcomes:
                entry = _mapping(outcome, "data.review_outcomes[]", locator)
                _known_data(entry, locator, ("day", "outcome", "recorded_at", "text"), field="data.review_outcomes[]")
                _date_string(entry.get("day"), "data.review_outcomes[].day", locator)
                _choice(entry.get("outcome"), "data.review_outcomes[].outcome", frozenset({"progress", "not_done", "done", "skipped", "missed", "canceled", "happened", "not_happened", "no_show"}), locator)
                _timestamp(entry.get("recorded_at"), "data.review_outcomes[].recorded_at", locator)
                if "text" in entry:
                    _string(entry["text"], "data.review_outcomes[].text", locator)

        if "occurrence" in data:
            if data["occurrence"] is None:
                del data["occurrence"]
            else:
                occurrence = _mapping(data["occurrence"], "data.occurrence", locator)
                _known_data(occurrence, locator, ("day", "outcome"), field="data.occurrence")
                _date_string(occurrence.get("day"), "data.occurrence.day", locator)
                _choice(occurrence.get("outcome"), "data.occurrence.outcome", frozenset({"done", "skipped", "missed", "canceled"}), locator)
                data["occurrence"] = occurrence
        # PRD §4.1: a next action's first step, on one line, kept like block when absent.
        if "first_step" in data:
            if data["first_step"] is None:
                del data["first_step"]
            else:
                step = _string(data["first_step"], "data.first_step", locator)
                if len(step) > MAX_FIRST_STEP_LENGTH or any(character in step for character in "\r\n\0"):
                    _invalid(locator, "data.first_step", f"one line of at most {MAX_FIRST_STEP_LENGTH} characters", step)
        for field in ("owed_to", "repeat"):
            if data.get(field) is not None:
                _string(data[field], f"data.{field}", locator)
        timing = _mapping(data.get("timing") or {}, "data.timing", locator)
        _validate_timing(timing, locator)
        data["timing"] = timing
        _validate_aliases(data, locator)
        _validate_importance(data, locator)
        if state == "dropped":
            _required_data(data, locator, ("reason",))
        elif data.get("reason") is not None:
            _string(data["reason"], "data.reason", locator)
        if state == "done":
            check = _mapping(data.get("check"), "data.check", locator)
            _known_data(check, locator, ("locator", "checked_by"), field="data.check")
            _string(check.get("locator"), "data.check.locator", locator)
            _string(check.get("checked_by"), "data.check.checked_by", locator)
            data["check"] = check
        elif data.get("check") is not None:
            data["check"] = _mapping(data["check"], "data.check", locator)
        parked_until = data.get("parked_until")
        if state == "parked" and parked_until is None:
            _invalid(locator, "data.parked_until", "required when state is parked", parked_until)
        if isinstance(parked_until, Mapping):
            parked = _mapping(parked_until, "data.parked_until", locator)
            _known_data(parked, locator, ("event",), field="data.parked_until")
            _string(parked.get("event"), "data.parked_until.event", locator)
            data["parked_until"] = parked
        elif parked_until is not None:
            data["parked_until"] = _date_string(parked_until, "data.parked_until", locator)
    elif kind == "sitrep":
        # Only the orientation fields, each one short line: never effects, sources, decisions,
        # candidates or chat text (b-project-sitreps.md, "What a task sitrep holds").
        _known_data(data, locator, ("project", "session_id", "intent", "next_step", "blockers", "note_updated_at", "note"))
        for field in ("project", "session_id", "intent"):
            _sitrep_line(data.get(field), f"data.{field}", locator)
        _sitrep_line(data.get("next_step"), "data.next_step", locator, empty=True)
        blockers = _strings(data.get("blockers"), "data.blockers", locator)
        if len(blockers) > SITREP_MAX_BLOCKERS:
            _invalid(locator, "data.blockers", f"at most {SITREP_MAX_BLOCKERS} blockers", blockers)
        for blocker in blockers:
            _sitrep_line(blocker, "data.blockers[]", locator)
        _timestamp(data.get("note_updated_at"), "data.note_updated_at", locator)
        note = _mapping(data.get("note"), "data.note", locator)
        _known_data(note, locator, ("record_id", "content_sha256"), field="data.note")
        if _RECORD_ID.fullmatch(_string(note.get("record_id"), "data.note.record_id", locator)) is None:
            _invalid(locator, "data.note.record_id", "brn_YYYYMMDD_HHMMSS_<hex>", note.get("record_id"))
        if _SHA256.fullmatch(_string(note.get("content_sha256"), "data.note.content_sha256", locator)) is None:
            _invalid(locator, "data.note.content_sha256", "64 lowercase hexadecimal characters", note.get("content_sha256"))
    elif kind == "focus_copy":
        _known_data(data, locator, ("project", "line", "section_sha256"))
        _sitrep_line(data.get("project"), "data.project", locator)
        line = data.get("line")
        section = data.get("section_sha256")
        if line is not None:
            _sitrep_line(line, "data.line", locator)
            if section is None:
                _invalid(locator, "data.section_sha256", "the Focus section hash when a line is copied", section)
        if section is not None and _SHA256.fullmatch(_string(section, "data.section_sha256", locator)) is None:
            _invalid(locator, "data.section_sha256", "64 lowercase hexadecimal characters", section)
    elif kind == "fact":  # pragma: no branch - kind is validated against the exhaustive set
        data.setdefault("valid_until", None)
        _known_data(data, locator, ("observed_at", "valid_until", "aliases"))
        _required_data(data, locator, ("observed_at",))
        _timestamp(data["observed_at"], "data.observed_at", locator)
        if data.get("valid_until") is not None:
            _timestamp(data["valid_until"], "data.valid_until", locator)
        _validate_aliases(data, locator)


def _source_from_mapping(value: Any, locator: str) -> dict[str, Any]:
    data = _mapping(value, "source", locator)
    _known_data(data, locator, ("said_by", "speaker", "came_by", "words", "recorded_by"), field="source")
    said_by = _choice(data.get("said_by"), "source.said_by", SOURCE_SAID_BY, locator)
    came_by = _choice(data.get("came_by"), "source.came_by", SOURCE_CAME_BY, locator)
    if said_by == "owner" and came_by != "direct":
        _invalid(locator, "source.came_by", "direct when source.said_by is owner", came_by)
    speaker = data.get("speaker")
    if said_by in ("agent", "other") or speaker is not None:
        speaker = _string(speaker, "source.speaker", locator)
    words = data.get("words")
    if came_by == "system":
        if words is not None:
            words = _string(words, "source.words", locator)
    else:
        words = _string(words, "source.words", locator)
    recorded_by = _string(data.get("recorded_by"), "source.recorded_by", locator)
    result: dict[str, Any] = {"said_by": said_by, "came_by": came_by, "recorded_by": recorded_by}
    if speaker is not None:
        result["speaker"] = speaker
    if words is not None:
        result["words"] = words
    return result


def _links_from_value(value: Any, locator: str) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        _invalid(locator, "links", "list of {type, target}", value)
    result: list[dict[str, Any]] = []
    for item in value:
        entry = _mapping(item, "links[]", locator)
        _known_data(entry, locator, ("type", "target"), field="links[]")
        link_type = _choice(entry.get("type"), "links[].type", LINK_TYPES, locator)
        target = _string(entry.get("target"), "links[].target", locator)
        result.append({"type": link_type, "target": target})
    return tuple(result)


def record_from_mapping(value: Mapping[str, Any], locator: str = "record") -> Record:
    data = _mapping(value, "record", locator)
    unknown = sorted(set(data) - _COMMON_FIELDS)
    if unknown:
        _invalid(locator, "record", f"known fields only; unknown={unknown}", data)
    schema = _string(data.get("schema"), "schema", locator)
    if schema not in (RECORD_SCHEMA_V1, RECORD_SCHEMA_V2):
        _invalid(locator, "schema", f"{RECORD_SCHEMA_V1} or {RECORD_SCHEMA_V2}", schema)
    is_v2 = schema == RECORD_SCHEMA_V2
    record_id = _string(data.get("id"), "id", locator)
    match = _RECORD_ID.fullmatch(record_id)
    if match is None:
        _invalid(locator, "id", "brn_YYYYMMDD_HHMMSS_<hex>", record_id)
    kind = _choice(data.get("kind"), "kind", RECORD_KINDS_V2 if is_v2 else RECORD_KINDS | SITREP_KINDS, locator)
    state = _choice(data.get("state"), "state", STATES[kind], locator)
    created_at = _timestamp(data.get("created_at"), "created_at", locator)
    updated_at = _timestamp(data.get("updated_at"), "updated_at", locator)
    if updated_at < created_at:
        _invalid(locator, "updated_at", "not earlier than created_at", data.get("updated_at"))
    if created_at.astimezone(UTC).strftime("%Y%m%d_%H%M%S") != f"{match.group(1)}_{match.group(2)}":
        _invalid(locator, "id", "timestamp prefix matching created_at in UTC", record_id)
    if is_v2:
        if data.get("assertion") is not None:
            _invalid(locator, "assertion", "absent for v2 records", data.get("assertion"))
        if data.get("confidence") is not None:
            _invalid(locator, "confidence", "absent for v2 records", data.get("confidence"))
        assertion: str | None = None
        confidence: float | None = None
    else:
        if data.get("source") is not None:
            _invalid(locator, "source", "absent for v1 records", data.get("source"))
        confidence_raw = data.get("confidence")
        if not isinstance(confidence_raw, (int, float)) or isinstance(confidence_raw, bool) or not 0 <= float(confidence_raw) <= 1:
            _invalid(locator, "confidence", "number from 0 through 1", confidence_raw)
        assertion = _choice(data.get("assertion"), "assertion", ASSERTIONS, locator)
        if kind in SITREP_KINDS and assertion != "system":
            _invalid(locator, "assertion", "system for a hub-written sitrep or focus_copy", assertion)
        confidence = float(confidence_raw)
    raw_evidence = data.get("evidence")
    if not isinstance(raw_evidence, list):
        _invalid(locator, "evidence", "list", raw_evidence)
    evidence = tuple(evidence_from_mapping(item, locator) for item in raw_evidence)
    if not evidence:
        _invalid(locator, "evidence", "at least one evidence item", raw_evidence)
    supersedes = _strings(data.get("supersedes", []), "supersedes", locator)
    for superseded in supersedes:
        if _RECORD_ID.fullmatch(superseded) is None:
            _invalid(locator, "supersedes", "record IDs", superseded)
    if record_id in supersedes:
        _invalid(locator, "supersedes", "must not include the record itself", supersedes)
    dedupe = data.get("dedupe_key")
    if dedupe is not None:
        dedupe = _string(dedupe, "dedupe_key", locator)
    record_data = _mapping(data.get("data"), "data", locator)
    try:
        canonical_json(record_data)
    except (TypeError, ValueError) as exc:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"Record data is not canonical JSON in {locator}",
            details={"locator": locator},
        ) from exc
    sensitivity = _choice(data.get("sensitivity"), "sensitivity", SENSITIVITIES, locator)
    _validate_kind_data(kind, record_data, locator, sensitivity, state=state)
    source_obj: dict[str, Any] | None = None
    if is_v2:
        source_obj = _source_from_mapping(data.get("source"), locator)
        # Evidence accumulates across revisions (new items are appended, oldest first), so
        # the item that belongs to *this* create or change is always the last one.
        if source_obj.get("words") is not None and source_obj["words"] != evidence[-1].excerpt:
            _invalid(locator, "source.words", "equal to this change's evidence excerpt", source_obj["words"])
    links = _links_from_value(data.get("links"), locator)
    if kind == "item" and WORK_FIELDS.intersection(record_data):
        own_first = supersedes[0] if supersedes else record_id
        if record_data.get("parent") == own_first:
            _invalid(locator, "data.parent", "not its own first ID", own_first)
        if own_first in record_data.get("after", []):
            _invalid(locator, "data.after", "not its own first ID", own_first)
        for entry in links:
            if entry["type"] in ("part_of", "blocked_by", "waiting_on"):
                _invalid(locator, "links[].type", "no old-only link alongside work fields", entry["type"])
    return Record(
        schema=schema,
        id=record_id,
        kind=kind,
        title=_string(data.get("title"), "title", locator),
        state=state,
        created_at=created_at,
        updated_at=updated_at,
        assertion=assertion,
        confidence=confidence,
        sensitivity=sensitivity,
        vaults=_strings(data.get("vaults", []), "vaults", locator),
        owners=_strings(data.get("owners", []), "owners", locator),
        entities=_strings(data.get("entities", []), "entities", locator),
        evidence=evidence,
        supersedes=supersedes,
        dedupe_key=dedupe,
        data=record_data,
        body=_string(data.get("body", ""), "body", locator, empty=True),
        source=source_obj,
        links=links,
    )


def new_record_id(now: datetime, entropy: str | None = None) -> str:
    if now.tzinfo is None:
        raise BrainError(ErrorCode.CONTRACT_INVALID, "Record ID time requires a timezone", details={"field": "now"})
    suffix = secrets.token_hex(4) if entropy is None else entropy
    if re.fullmatch(r"[0-9a-f]{4,16}", suffix) is None:
        raise BrainError(ErrorCode.CONTRACT_INVALID, "Record ID entropy must be lowercase hexadecimal", details={"entropy": suffix})
    return f"brn_{now.astimezone(UTC).strftime('%Y%m%d_%H%M%S')}_{suffix}"


def commitment_conditions(record: Record, *, now: datetime) -> tuple[str, ...]:
    if record.kind != "commitment":
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "Commitment conditions require a commitment record", details={"record_id": record.id})
    if now.tzinfo is None:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "Commitment condition time requires a timezone", details={"field": "now"})
    data = record.data
    time = data["time"]
    conditions: set[str] = set()
    if data.get("recurrence"):
        conditions.add("recurring")
    if record.state == "waiting" or data.get("waiting_on"):
        conditions.add("waiting")
    if record.state == "blocked" or data["blocked_by"]:
        conditions.add("blocked")
    if data["conflict_ids"]:
        conditions.add("conflicting")
    if data.get("duplicate_of"):
        conditions.add("duplicate")
    if data.get("owner") is None:
        conditions.add("unowned")
    terminal = record.state in {"done", "cancelled", "superseded"}
    for field, condition in (("due", "overdue"), ("stale_after", "stale")):
        raw = time.get(field)
        if raw is not None:
            moment = _timestamp(raw, f"data.time.{field}", record.id)
            if moment < now and (condition != "overdue" or not terminal):
                conditions.add(condition)
    return tuple(condition for condition in COMMITMENT_CONDITIONS if condition in conditions)


def render_record(record: Record) -> str:
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.width = 4096
    stream = io.StringIO()
    yaml.dump(record.as_dict(include_body=False), stream)
    body = record.body.rstrip("\n")
    return f"---\n{stream.getvalue()}---\n{body}\n"


def parse_record(text: str, locator: str = "record") -> Record:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise BrainError(
            ErrorCode.RECORD_CORRUPT,
            f"Record frontmatter is malformed in {locator}",
            details={"locator": locator},
        )
    frontmatter, body = text[4:].split("\n---\n", 1)
    data = parse_yaml(frontmatter, locator)
    data["body"] = body.rstrip("\n")
    return record_from_mapping(data, locator)


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    record_id: str
    relative_path: str
    content_sha256: str
    created: bool


class RecordStore:
    """Append-only record store rooted in one existing Brain Home directory."""

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root)
        if not candidate.is_dir():
            raise BrainError(ErrorCode.ROOT_MISSING, f"Brain Home root is missing: {candidate}", details={"root": str(candidate)})
        self.root = candidate.resolve(strict=True)
        self.records_root = self.root / "records"

    def _destination(self, record: Record) -> Path:
        moment = record.created_at.astimezone(UTC)
        destination = self.records_root / record.kind / f"{moment:%Y}" / f"{moment:%m}" / f"{record.id}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = destination.parent.resolve(strict=True)
        try:
            common = os.path.commonpath((os.path.normcase(str(self.root)), os.path.normcase(str(resolved_parent))))
        except ValueError as exc:
            raise BrainError(ErrorCode.PATH_ESCAPE, "Record destination escaped Brain Home", details={"record_id": record.id}) from exc
        if common != os.path.normcase(str(self.root)):
            raise BrainError(ErrorCode.PATH_ESCAPE, "Record destination escaped Brain Home", details={"record_id": record.id})
        return destination

    def create(
        self,
        record: Record,
        *,
        fault: Callable[[str, Path], None] | None = None,
    ) -> WriteReceipt:
        text = render_record(record)
        content = text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        destination = self._destination(record)
        temporary = destination.with_name(f".{record.id}.{secrets.token_hex(6)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if fault is not None:
                fault("after_temp", temporary)
            try:
                os.link(temporary, destination)
                created = True
            except FileExistsError:
                existing = destination.read_bytes()
                if existing != content:
                    raise BrainError(
                        ErrorCode.RECORD_EXISTS,
                        f"Record ID already exists with different content: {record.id}",
                        details={"record_id": record.id, "path": str(destination)},
                    )
                created = False
            except OSError as exc:
                raise BrainError(
                    ErrorCode.RECORD_WRITE_FAILED,
                    f"Could not publish record atomically: {record.id}",
                    details={"record_id": record.id, "error": type(exc).__name__},
                ) from exc
            if fault is not None:
                fault("after_publish", destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        relative = destination.relative_to(self.root).as_posix()
        return WriteReceipt(record_id=record.id, relative_path=relative, content_sha256=digest, created=created)

    def create_semantic(
        self,
        record: Record,
        *,
        semantic_key: str,
        origin_id: str,
        observed_preimage_sha256: str,
        fault: Callable[[str, Path], None] | None = None,
        before_create: Callable[[], None] | None = None,
        finalize: Callable[[Record, WriteReceipt, bool], None] | None = None,
    ) -> tuple[Record, WriteReceipt]:
        """Claim one decision semantic key while holding the owner-local writer lock."""

        if _SHA256.fullmatch(semantic_key) is None or record.kind != "decision" or record.dedupe_key != semantic_key:
            raise BrainError(
                ErrorCode.CONTRACT_INVALID,
                "Semantic creation requires a decision with the requested dedupe key",
                details={"record_id": record.id, "semantic_key": semantic_key},
            )
        if _ORIGIN_ID.fullmatch(origin_id) is None or record.data.get("origin_id") != origin_id:
            raise BrainError(
                ErrorCode.CONTRACT_INVALID,
                "Semantic creation requires the winning origin in decision data",
                details={"record_id": record.id, "origin_id": origin_id},
            )
        if _SHA256.fullmatch(observed_preimage_sha256) is None:
            raise BrainError(
                ErrorCode.CONTRACT_INVALID,
                "Semantic creation requires a canonical preimage hash",
                details={"record_id": record.id, "refusal_code": "canonical_preimage_invalid"},
            )
        lock_path = self.records_root / ".durability" / "writer.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        mutex = _semantic_mutex(lock_path)
        with mutex, _semantic_file_lock(lock_path):
            decisions = tuple(item for item in self.all() if item.kind == "decision")
            matches = tuple(item for item in decisions if item.dedupe_key == semantic_key)
            if len(matches) > 1:
                raise BrainError(
                    ErrorCode.RECORD_CORRUPT,
                    "Decision semantic key has multiple canonical origins",
                    details={"semantic_key": semantic_key, "matches": [item.id for item in matches]},
                )
            if matches:
                accepted = matches[0]
                accepted_origin = accepted.data.get("origin_id")
                if not isinstance(accepted_origin, str) or _ORIGIN_ID.fullmatch(accepted_origin) is None:
                    raise BrainError(
                        ErrorCode.RECORD_CORRUPT,
                        "Decision semantic key has no valid canonical origin",
                        details={"semantic_key": semantic_key, "refusal_code": "semantic_origin_corrupt"},
                    )
                content = render_record(accepted).encode("utf-8")
                destination = self._destination(accepted)
                receipt = WriteReceipt(
                    record_id=accepted.id,
                    relative_path=destination.relative_to(self.root).as_posix(),
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    created=accepted.data.get("origin_id") == origin_id,
                )
                if finalize is not None:
                    finalize(accepted, receipt, False)
                return accepted, receipt
            origin_matches = tuple(item for item in decisions if item.data.get("origin_id") == origin_id)
            if origin_matches:
                raise BrainError(
                    ErrorCode.RECORD_CORRUPT,
                    "Origin ID already belongs to a different semantic decision",
                    details={"origin_id": origin_id, "refusal_code": "origin_id_conflict"},
                )
            if before_create is not None:
                before_create()
            moment = record.created_at.astimezone(UTC)
            candidate_path = self.records_root / record.kind / f"{moment:%Y}" / f"{moment:%m}" / f"{record.id}.md"
            candidate_preimage = candidate_path.read_bytes() if candidate_path.exists() else b""
            actual_preimage_sha256 = hashlib.sha256(candidate_preimage).hexdigest()
            if actual_preimage_sha256 != observed_preimage_sha256:
                raise BrainError(
                    ErrorCode.CONTRACT_INVALID,
                    "Canonical decision preimage changed",
                    details={
                        "record_id": record.id,
                        "refusal_code": "canonical_preimage_changed",
                        "expected": observed_preimage_sha256,
                        "actual": actual_preimage_sha256,
                    },
                )
            receipt = self.create(record, fault=fault)
            if finalize is not None:
                finalize(record, receipt, True)
            return record, receipt

    def all(self) -> tuple[Record, ...]:
        if not self.records_root.exists():
            return ()
        records: list[Record] = []
        seen: set[str] = set()
        for path in sorted(self.records_root.glob("*/*/*/*.md"), key=lambda item: item.as_posix()):
            resolved = path.resolve(strict=True)
            try:
                common = os.path.commonpath((os.path.normcase(str(self.root)), os.path.normcase(str(resolved))))
            except ValueError as exc:
                raise BrainError(ErrorCode.PATH_ESCAPE, "Record path escaped Brain Home", details={"path": str(path)}) from exc
            if common != os.path.normcase(str(self.root)):
                raise BrainError(ErrorCode.PATH_ESCAPE, "Record path escaped Brain Home", details={"path": str(path)})
            try:
                record = parse_record(path.read_text(encoding="utf-8"), str(path))
            except (OSError, UnicodeError) as exc:
                raise BrainError(ErrorCode.RECORD_CORRUPT, f"Cannot read record: {path}", details={"path": str(path)}) from exc
            if record.id in seen:
                raise BrainError(ErrorCode.DUPLICATE_ID, f"Duplicate record ID: {record.id}", details={"record_id": record.id})
            if path.stem != record.id or path.parent.parent.parent.name != record.kind:
                raise BrainError(
                    ErrorCode.RECORD_CORRUPT,
                    f"Record path does not match its envelope: {path}",
                    details={"path": str(path), "record_id": record.id, "kind": record.kind},
                )
            seen.add(record.id)
            records.append(record)
        return tuple(sorted(records, key=lambda item: (item.created_at, item.id)))

    def get(self, record_id: str) -> Record:
        matches = [record for record in self.all() if record.id == record_id]
        if not matches:
            raise BrainError(ErrorCode.NOT_FOUND, f"Record not found: {record_id}", details={"record_id": record_id})
        return matches[0]

    def current(self, *, kind: str | None = None) -> tuple[Record, ...]:
        records = self.all()
        superseded = {record_id for record in records for record_id in record.supersedes}
        return tuple(record for record in records if record.id not in superseded and (kind is None or record.kind == kind))

    def _revised_candidate(
        self,
        previous: Record,
        *,
        now: datetime,
        state: str | None,
        data: Mapping[str, Any] | None,
        title: str | None,
        body: str | None,
        evidence: Iterable[Evidence],
        entropy: str | None,
        source: Mapping[str, Any] | None,
    ) -> Record:
        target_state = previous.state if state is None else state
        if state is not None and state != previous.state and state not in TRANSITIONS[previous.kind][previous.state]:
            raise BrainError(
                ErrorCode.RECORD_TRANSITION,
                f"Invalid {previous.kind} transition: {previous.state} -> {state}",
                details={"record_id": previous.id, "from": previous.state, "to": state},
            )
        merged_data = dict(previous.data)
        if data is not None:
            merged_data.update(data)
        replacement = replace(
            previous,
            id=new_record_id(now, entropy),
            state=target_state,
            title=previous.title if title is None else title,
            body=previous.body if body is None else body,
            created_at=now,
            updated_at=now,
            evidence=tuple((*previous.evidence, *tuple(evidence))),
            supersedes=tuple(dict.fromkeys((*previous.supersedes, previous.id))),
            data=merged_data,
            source=(dict(source) if source is not None else previous.source),
        )
        return record_from_mapping(replacement.as_dict(), replacement.id)

    def revise(
        self,
        record_id: str,
        *,
        now: datetime,
        state: str | None = None,
        data: Mapping[str, Any] | None = None,
        title: str | None = None,
        body: str | None = None,
        evidence: Iterable[Evidence] = (),
        entropy: str | None = None,
        expected_sha256: str | None = None,
        actor: str | None = None,
        source: Mapping[str, Any] | None = None,
    ) -> tuple[Record, WriteReceipt]:
        """Publish one append-only revision; v2 records require expected_sha256, actor and source.

        v2 changes run under the writer lock so a stale expected_sha256 always loses (R8):
        the current head is re-read inside the lock and language.check_change (R1-R10) runs
        before anything is written, so a refusal leaves no partial write.
        """

        probe = self.get(record_id)
        if probe.schema != RECORD_SCHEMA_V2:
            if expected_sha256 is not None or actor is not None or source is not None:
                raise BrainError(
                    ErrorCode.INVALID_ARGUMENT,
                    "expected_sha256, actor and source only apply to v2 record changes",
                    details={"record_id": record_id},
                )
            validated = self._revised_candidate(
                probe,
                now=now,
                state=state,
                data=data,
                title=title,
                body=body,
                evidence=evidence,
                entropy=entropy,
                source=None,
            )
            return validated, self.create(validated)

        if expected_sha256 is None or actor is None or source is None:
            raise BrainError(
                ErrorCode.INVALID_ARGUMENT,
                "v2 record changes require expected_sha256, actor and source",
                details={"record_id": record_id},
            )
        lock_path = self.records_root / ".durability" / "writer.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        mutex = _semantic_mutex(lock_path)
        with mutex, _semantic_file_lock(lock_path):
            all_records = self.all()
            previous = next((record for record in all_records if record.id == record_id), None)
            if previous is None:  # pragma: no cover - records are append-only; self.get() above already proved existence
                raise BrainError(ErrorCode.NOT_FOUND, f"Record not found: {record_id}", details={"record_id": record_id})
            # expected_sha256 is the WriteReceipt.content_sha256 the caller was handed for
            # this record (the published file's own hash) - the only hash it could have.
            actual_sha256 = hashlib.sha256(render_record(previous).encode("utf-8")).hexdigest()
            superseded_ids = {superseded for record in all_records for superseded in record.supersedes}
            if record_id in superseded_ids or expected_sha256 != actual_sha256:
                # Records are immutable once written, so a content hash alone can never
                # detect that someone already revised this record (R8): the same bytes
                # would still be sitting under record_id. The current-head check is what
                # actually stops two changes from the same read both landing.
                raise BrainError(
                    ErrorCode.RECORD_CHANGED,
                    "The record changed since it was read",
                    details={"record_id": record_id, "expected": expected_sha256, "actual": actual_sha256},
                )
            validated = self._revised_candidate(
                previous,
                now=now,
                state=state,
                data=data,
                title=title,
                body=body,
                evidence=evidence,
                entropy=entropy,
                source=source,
            )
            from brain_core.language import check_change

            check_change(previous, validated, actor=actor)
            return validated, self.create(validated)

    def exact_capture_duplicate(self, source_hash: str) -> Record | None:
        if not _SHA256.fullmatch(source_hash):
            _invalid("capture lookup", "source_hash", "64 lowercase hexadecimal characters", source_hash)
        for record in reversed(self.current(kind="capture")):
            if record.data["original_source_hash"] == source_hash:
                return record
        return None


@dataclass(frozen=True, slots=True)
class CommitmentCandidate:
    actor: str
    source_message_sha256: str
    evidence_excerpt: str
    assertion: str
    confidence: float
    owner: str
    action: str
    time: dict[str, Any]


def commitment_candidate_from_mapping(value: Mapping[str, Any], locator: str = "candidate") -> CommitmentCandidate:
    data = _mapping(value, "candidate", locator)
    allowed = {"actor", "source_message_sha256", "evidence_excerpt", "assertion", "confidence", "owner", "action", "time"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        _invalid(locator, "candidate", f"known fields only; unknown={unknown}", data)
    source_hash = _string(data.get("source_message_sha256"), "source_message_sha256", locator)
    if not _SHA256.fullmatch(source_hash):
        _invalid(locator, "source_message_sha256", "64 lowercase hexadecimal characters", source_hash)
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        _invalid(locator, "confidence", "number from 0 through 1", confidence)
    assertion = _choice(data.get("assertion"), "assertion", frozenset({"explicit", "inferred"}), locator)
    return CommitmentCandidate(
        actor=_string(data.get("actor"), "actor", locator),
        source_message_sha256=source_hash,
        evidence_excerpt=_string(data.get("evidence_excerpt"), "evidence_excerpt", locator),
        assertion=assertion,
        confidence=float(confidence),
        owner=_string(data.get("owner"), "owner", locator),
        action=_string(data.get("action"), "action", locator),
        time=_mapping(data.get("time", {}), "time", locator),
    )


def record_from_commitment_candidate(
    candidate: CommitmentCandidate,
    *,
    authenticated_actor: str,
    source_message: str,
    session_id: str,
    now: datetime,
    allow_explicit_confirmation: bool,
    sensitivity: str = "private",
    entropy: str | None = None,
    minimum_confidence: float = 0.5,
) -> Record | None:
    actual_hash = hashlib.sha256(source_message.encode("utf-8")).hexdigest()
    if candidate.actor != authenticated_actor or candidate.source_message_sha256 != actual_hash:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            "Commitment candidate source authentication failed",
            details={"actor_match": candidate.actor == authenticated_actor, "hash_match": candidate.source_message_sha256 == actual_hash},
        )
    if candidate.evidence_excerpt not in source_message:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            "Commitment candidate evidence is not an exact source span",
            details={"session_id": session_id},
        )
    if not 0 <= minimum_confidence <= 1:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "minimum_confidence must be from 0 through 1", details={"minimum_confidence": minimum_confidence})
    if candidate.confidence < minimum_confidence:
        return None
    state = "confirmed" if candidate.assertion == "explicit" and allow_explicit_confirmation else "proposed"
    evidence = Evidence(
        source_type="session_message",
        session_id=session_id,
        actor=authenticated_actor,
        locator=f"session:{session_id}",
        content_sha256=actual_hash,
        excerpt=candidate.evidence_excerpt,
    )
    return record_from_mapping(
        {
            "schema": RECORD_SCHEMA,
            "id": new_record_id(now, entropy),
            "kind": "commitment",
            "title": candidate.action,
            "state": state,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "assertion": candidate.assertion,
            "confidence": candidate.confidence,
            "sensitivity": sensitivity,
            "vaults": [],
            "owners": [candidate.owner],
            "entities": [],
            "evidence": [evidence.as_dict()],
            "supersedes": [],
            "dedupe_key": stable_hash({"owner": candidate.owner, "action": candidate.action, "time": candidate.time}),
            "data": {
                "owner": candidate.owner,
                "action": candidate.action,
                "time": candidate.time,
                "blocked_by": [],
                "decision_ids": [],
            },
            "body": "",
        },
        "commitment candidate",
    )
