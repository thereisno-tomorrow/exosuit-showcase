"""GitHub-backed Brain Home provider for the typed Brain Core MCP boundary.

Trusted host code constructs this provider with an already-configured GitHub
storage instance and a record adapter.  Callers never provide repository,
branch, root, owner, grant, or publication settings.
"""

from __future__ import annotations

from copy import deepcopy

import contextlib
import hashlib
import json
import logging
import re
import string
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import anyio

from brain_core import language, work
from brain_core.contracts import canonical_json
from brain_core.errors import BrainError
from brain_core.routines import is_routine, occurrence_day
from brain_core.schedule import SPLIT_TASK_NOTICE, split_notice_required
from brain_core.timewords import (
    SINGAPORE,
    find_one as find_one_time_phrase,
    from_words,
    resolve_date_and_time,
    says_more,
    today_in_singapore,
)
from brain_core.durability import (
    CATALOG_BYTES,
    CATALOG_ID,
    CATALOG_PATH,
    CATALOG_SCHEMA,
    CATALOG_SHA256,
    EMPTY_SHA256,
    RECEIPT_SCHEMA,
    WRITER_ID,
    DurabilityRequest,
    DurabilityReceipt,
    SharedDurabilityWriter,
)
from brain_core.github_storage import (
    MAX_DECISION_CANDIDATES,
    SCAN_KINDS,
    GitHubBrainHomeStorage,
    GitHubConflict,
    GitHubDecisionPreflight,
    GitHubMemoryOperationPreflight,
    GitHubPublicationResult,
    GitHubPublicationUncertain,
    GitHubRevision,
    GitHubStorageError,
    GitHubTransportError,
    memory_operation_record_suffix,
)
from brain_core.records import (
    OWNER_ESTIMATE,
    RECORD_SCHEMA_V2,
    Evidence,
    Record,
    evidence_from_mapping,
    new_record_id,
    parse_record,
    record_from_mapping,
    render_record,
)
from brain_core.review import ItemRecord, answers_by_task

from .brain_home_copy import SCAN_MAX_BYTES, SCAN_MAX_RECORDS
from .brain_runtime import (
    BrainOperation,
    BrainRuntimeError,
    CalendarRemoveRequest,
    CalendarRemoveResult,
    DecisionRequest,
    MemoryChangeRequest,
    MemoryListItem,
    MemoryListItemWithAnswers,
    MemoryListItemWithNow,
    MemoryListQuery,
    MemoryListResult,
    MemoryRecord,
    MemoryRecordWithBodyNote,
    MemoryRecordWithLinks,
    MemoryRecordWithLinksAndBodyNote,
    MemoryRecordWithNow,
    MemorySaveRequest,
    MemorySearchQuery,
    MemorySearchResult,
    OwnerAuthorization,
    ReaderNext,
    ReaderNow,
    ReaderPart,
    ReaderProgress,
    WriteReceipt,
    WriteReceiptWithCalendar,
    WriteReceiptWithCalendarAndLength,
    WriteReceiptWithLength,
    WorkRequest,
    work_caller_allowed,
    work_check_only,
)

if TYPE_CHECKING:
    from gremlin_town.sources.google.calendar_writer import CalendarWriter


logger = logging.getLogger(__name__)

_ORIGIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ROOT = "records/.durability/receipts"
_BRAIN_HOME = "vault://brain-home"
_PREFLIGHT_ATTEMPTS = 4


class _RefCountedLock:
    """A lock shared by every waiter for one key, torn down once none remain."""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refs = 0


def _record_path(record: Record) -> str:
    moment = record.created_at.astimezone(UTC)
    return f"records/{record.kind}/{moment:%Y}/{moment:%m}/{record.id}.md"


def map_storage_error(
    exc: Exception, *, reading: bool = False, nothing_saved: bool | None = None,
) -> BrainRuntimeError:
    """A Brain Core storage failure as the stable error an agent sees.

    Brain Home past a size limit - a scan past its cap, too many decisions, a tree too big for
    GitHub to list - is `brain.capacity.exceeded`, not retryable, with one plain sentence: it is not
    corrupt, and nothing is lost (the storage-limit plan, layer 1). GitHub's used-up hourly allowance,
    or a tree other than the one asked for, is `brain.capability.unavailable`, retryable.

    ``nothing_saved`` says the failure stopped a save or change before it landed, so the sentence says
    nothing was saved, never only "nothing is lost", which an agent could read as "your save is safe".
    It defaults to ``not reading``: a failed publish. A read inside a write, before its publish, passes
    ``reading=True, nothing_saved=True``."""

    from gremlin_town.review.brain_source import brain_problem

    problem = brain_problem(exc, nothing_saved=(not reading) if nothing_saved is None else nothing_saved)
    if problem is not None:
        sentence, lasting = problem
        if lasting:
            return BrainRuntimeError("brain.capacity.exceeded", sentence)
        return BrainRuntimeError("brain.capability.unavailable", sentence, retryable=True)
    if reading and isinstance(exc, GitHubConflict) and exc.code == "github_record_duplicate":
        # Two live copies of one record ID: a read can't choose, and nothing about it is a conflict
        # with this caller's operation.
        return BrainRuntimeError(
            "brain.record.corrupt", "Brain Home holds two copies of this record; an operator must check them",
        )
    if isinstance(exc, GitHubConflict):
        return BrainRuntimeError("brain.record.exists", "Stable operation conflicts with remote state")
    if isinstance(exc, GitHubPublicationUncertain):
        return BrainRuntimeError(
            "brain.record.write_failed",
            "Brain Home did not confirm the write; resend the same operation_id to get the real result",
            retryable=True,
        )
    if isinstance(exc, GitHubTransportError):
        return BrainRuntimeError(
            "brain.capability.unavailable",
            "Brain Home (GitHub) is unavailable; nothing was saved",
            retryable=True,
        )
    if isinstance(exc, GitHubStorageError):
        if reading and exc.code == "github_path_missing":
            return BrainRuntimeError("brain.not_found", "Brain Home record was not found")
        return BrainRuntimeError(
            "brain.record.corrupt" if reading else "brain.record.write_failed",
            "GitHub Brain Home rejected the operation",
        )
    return BrainRuntimeError("brain.internal_error", "Brain Home operation failed")


def _archived(path: str) -> bool:
    """A record found in Brain Home's `archive/` folder, moved there byte for byte (the storage-limit
    plan's layer 2): an old version, or a thing archived whole."""

    return path.startswith("archive/")


def find_by_id(storage: GitHubBrainHomeStorage, record_id: str, *, revision: GitHubRevision | None = None):
    """One record by ID, looking in Brain Home's `archive/` folder too (the storage-limit plan, layer 1).

    A record in both `records/` and `archive/` is read from its live copy, as it was before the archive
    was searched; two archived copies and no live one read as not found; two live copies are still a
    conflict for the caller to map. Each case says so in the journal. An archived copy that doesn't parse
    is skipped by Brain Core. Storage errors are the caller's to map."""

    try:
        return storage.preflight_record_by_id(record_id, revision=revision, include_archive=True)
    except GitHubConflict as exc:
        if exc.code != "github_record_duplicate":
            raise
        paths = [path for path in exc.details.get("paths") or () if isinstance(path, str)]
        archived = sum(path.startswith("archive/") for path in paths)
        live = len(paths) - archived
        if live > 1:
            logger.warning(
                "Brain Home holds record %s more than once in records/ (%s); an operator must check it",
                record_id, ", ".join(paths),
            )
            raise
        if live == 0:
            logger.warning(
                "Brain Home's archive holds record %s more than once (%s); answering not found",
                record_id, ", ".join(paths),
            )
        else:
            logger.warning("Brain Home holds record %s in both records/ and archive/; reading the live copy", record_id)
        return storage.preflight_record_by_id(record_id, revision=revision)


def _list_item(answers: tuple[ItemRecord, ...], **fields: object) -> MemoryListItem:
    """An item with Nic's answers carries them (step E spec §3.1); every other keeps the old shape."""

    if not answers:
        return MemoryListItem(**fields)  # type: ignore[arg-type]
    return MemoryListItemWithAnswers(
        **fields,  # type: ignore[arg-type]
        answers=tuple(
            {"record_id": answer.record.id, "words": answer.record.source["words"]} for answer in answers
        ),
    )


def _successor_index(records: Iterable[Record]) -> dict[str, list[Record]]:
    """Old record ID -> the records in ``records`` that name it in their own ``supersedes``."""

    index: dict[str, list[Record]] = {}
    for record in records:
        for old_id in record.supersedes:
            index.setdefault(old_id, []).append(record)
    return index


def _newest_id(record_id: str, index: dict[str, list[Record]]) -> str:
    """Follow the supersedes chain from ``record_id`` to its newest record (A -> B -> C gives C).

    Deterministic: among several successors the newest ``updated_at`` wins, then the larger ID.
    An ID nobody supersedes, or one outside the snapshot, comes back unchanged.
    """

    seen = {record_id}
    current = record_id
    while True:
        later = [item for item in index.get(current, ()) if item.id not in seen]
        if not later:
            return current
        best = max(later, key=lambda item: (item.updated_at, item.id))
        seen.add(best.id)
        current = best.id


def _links_at_newest(
    links: Iterable[dict[str, object]], index: dict[str, list[Record]]
) -> tuple[dict[str, object], ...]:
    """A caller's view of a record's links: each target at its newest version.

    A memory change gives the record a new ID, but links on other records keep the old one.
    The stored links are never rewritten; this only changes what a reply shows. Each link
    keeps exactly the stored shape ({type, target}) so an agent can copy it into
    memory.save/change (MemoryLinkInput forbids extra keys); the stored ID stays in a read's
    ``canonical_record``. ``index`` is ``_successor_index`` of the snapshot the reply was
    built from, so the answer is consistent with the rest of the reply.

    Each reply resolves only within the kinds and owner it indexes: read indexes its own
    kind plus items and facts, list its item (and fact, while review is on) scan, search its
    four searched kinds, preference list preferences. A target of a kind outside that scan,
    or another owner's record, stays as saved.
    """

    shown: list[dict[str, object]] = []
    for link in links:
        item = dict(link)
        stored = item.get("target")
        if isinstance(stored, str):
            item["target"] = _newest_id(stored, index)
        shown.append(item)
    return tuple(shown)


def _memory_reply(
    record: Record, view: dict[str, object], links: tuple[dict[str, object], ...],
    *, now: ReaderNow | None = None,
) -> MemoryRecord:
    """A read or search reply for one record, in the smallest shape that says what it must.

    A body is the note from the record's first save, and agents update first_step, state and
    dates without rewriting it. When a non-empty body sits beside a first_step or timing, the
    reply adds ``body_is`` saying the fields win (Nic, 30 Sep 2026); links are added only when
    the record has some. State does not count: every record has one, and for a fact the body is
    the content. Otherwise the reply keeps exactly its old shape. ``body`` is never changed.
    """

    if now is not None:
        return MemoryRecordWithNow(**view, links=links, now=now)
    noted = bool(record.body) and bool(record.data.get("first_step") or record.data.get("timing"))
    if links and noted:
        return MemoryRecordWithLinksAndBodyNote(**view, links=links)
    if links:
        return MemoryRecordWithLinks(**view, links=links)
    if noted:
        return MemoryRecordWithBodyNote(**view)
    return MemoryRecord(**view)


def _reader_now(record: Record, index: work.WorkIndex, moment: datetime) -> ReaderNow:
    """Project only the sole reader's effective facts; keep raw records separate."""
    key = work.first_id(record)
    thing = index[key]
    parent = index.get(thing.data.get("parent"))
    explanation = work.explain(thing, index, moment)
    progress = work.progress_of(thing, index)
    shown_progress: str | ReaderProgress = progress["text"]
    if progress["started"] or progress["last_ticked"]:
        shown_progress = ReaderProgress(text=progress["text"], started=progress["started"])
        for field in ("last_ticked", "last_ticked_at", "last_ticked_day"):
            if progress[field] is not None:
                shown_progress[field] = progress[field]
    answer = ReaderNow(**{
        "is": work.kind_of(thing, index),
        "under": parent.title if parent else None,
        "status": work.status_of(thing, index, moment),
        "next": _reader_next(work.next_of(thing, index, moment), key, explanation),
        "why": explanation["reason"],
        "progress": shown_progress,
        "dates": work.dates_of(thing, index),
    })
    for field in ("labels", "whose", "needs", "finish_line", "after", "expected", "stake"):
        if thing.data.get(field):
            answer[field] = thing.data[field]
    if parent:
        answer["parent"] = work.first_id(parent)
        if thing.body:
            answer["details"] = thing.body
    if explanation["until"]:
        answer["until"] = explanation["until"]
    parts = sorted((part for part in index.values() if part.data.get("parent") == key),
                   key=lambda part: (part.data.get("order", 0), work.first_id(part)))
    if parts:
        answer["parts"] = tuple(ReaderPart(
            record_id=work.first_id(part), title=part.title,
            **({"details": part.body} if part.body else {}),
            status=work.status_of(part, index, moment),
            next=_reader_next(work.next_of(part, index, moment), key, explanation),
        ) for part in parts)
    return answer


def _reader_next(value: work.Next, key: str, context: work.Explanation) -> str | ReaderNext:
    """Avoid repeating enclosing identity/status/reason without losing part answers."""
    shown = ReaderNext(text=value["text"])
    if value["item_ref"] != key:
        shown["item_ref"] = value["item_ref"]
    for field in ("status", "reason", "until"):
        if value[field] != context[field]:
            shown[field] = value[field]
    if value["children"]:
        shown["children"] = tuple(_reader_next(child, key, context) for child in value["children"])
    return value["text"] if len(shown) == 1 else shown


def _reader_search_text(record: Record, index: work.WorkIndex, first_words: Mapping[str, str]) -> str:
    """First words, inherited labels, parent title and parts from one reader index."""
    key = work.first_id(record)
    thing = index[key]
    parent = index.get(thing.data.get("parent"))
    parts = [part for part in index.values() if part.data.get("parent") == key]
    return " ".join((
        first_words[key],
        " ".join(thing.data.get("labels", ())),
        parent.title if parent else "",
        *(part.title for part in parts),
        *(first_words[work.first_id(part)] for part in parts),
    )).casefold()


def _memory_confirmation(
    record: Record, *, review_enabled: bool = False
) -> tuple[str | None, dict[str, object] | None, tuple[dict[str, object], ...]]:
    word = language.plain_word(record.data) if record.kind == "item" else None
    timing = record.data.get("timing") if record.kind == "item" else None
    return word, timing, tuple(dict(item) for item in record.links)


def _receipt(calendar: dict[str, object] | None, length: dict[str, object] | None = None, **values: object) -> WriteReceipt:
    """A save/change receipt; one with a calendar part (N8) or a length part (step I section 6)
    only when there is one, so every other receipt keeps exactly its old shape."""

    if calendar is None and length is None:
        return WriteReceipt(**values)  # type: ignore[arg-type]
    if length is None:
        return WriteReceiptWithCalendar(**values, calendar=calendar)  # type: ignore[arg-type]
    if calendar is None:
        return WriteReceiptWithLength(**values, length=length)  # type: ignore[arg-type]
    return WriteReceiptWithCalendarAndLength(**values, calendar=calendar, length=length)  # type: ignore[arg-type]


# Where Nic's answer goes: a change to the task just saved, never a second save (which would be a
# second task). In the receipt rather than the tool text, which is at its size floor.
_LENGTH_NEXT = "His answer goes to bc_work: organise this thing with rough_minutes, length_is_nics and his quoted message."


def _minutes_words(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h" if not rest else f"{hours} h {rest} min"


def _length_note(record: Record, *, asked: bool, saving: bool, schedule_mode: bool = False) -> dict[str, object] | None:
    """What the agent says about a task's length when it confirms (step I section 6; Nic, 29 Sep
    2026: "when acknowledghing agent should tell me his estimate so i can correct it"). Given
    when the save or change carried a length; on a save, also when a sorted task has none, to ask
    for one. Never a made-up length."""

    if record.kind != "item":
        return None
    timing = record.data.get("timing") or {}
    minutes = timing.get("duration_minutes")
    if minutes is None:
        if schedule_mode:
            return None
        if saving and record.data.get("block") and record.state == "open" and record.data.get("size", "task") == "task":
            return {"status": "needs_length", "text": "Saved. How long will it take?", "next": _LENGTH_NEXT}
        return None
    if not asked:
        return None
    planned = timing.get("padded_minutes") or minutes
    yours = timing.get("estimated_by") == OWNER_ESTIMATE
    if yours:
        text = f"{_minutes_words(minutes)}, as you said."
    else:
        text = (
            f"My guess: {_minutes_words(minutes)}, planned as {_minutes_words(planned)} with padding. "
            "Say a number to change it."
        )
    note: dict[str, object] = {
        "minutes": minutes, "planned_minutes": planned, "estimated_by": timing.get("estimated_by"), "yours": yours,
        "text": text,
    }
    if not yours:
        note["next"] = _LENGTH_NEXT
    return note


def _memory_confirmed(record: Record) -> bool:
    """v1 captures and commitments carry no source; readers treat them as unconfirmed."""

    from brain_core.records import RECORD_SCHEMA_V1
    return not (record.schema == RECORD_SCHEMA_V1 and record.kind in {"capture", "commitment"})


def _importance(record: Record) -> dict[str, object] | None:
    """An item's importance as saved, confirmed or not (Ciel redesign N5); None is normal."""

    value = record.data.get("importance") if record.kind == "item" else None
    return dict(value) if isinstance(value, dict) else None


def _rough_length(minutes: int | None, estimated_by: str, *, nics: bool = False) -> dict[str, object] | None:
    """An agent's rough length (Ciel and Brain Home PRD §3), refused here when out of range. It
    joins the input identity and lands in ``timing`` as a labelled guess: the minutes, the
    padded minutes (x1.5, as step C pads) and who estimated it. Never a date, so R7 never asks
    for time words for it.

    With ``nics`` the number is Nic's own (step I §6; Nic, 29 Sep 2026: "dont pad my responses
    just take them"): ``estimated_by`` is `OWNER_ESTIMATE` and it is booked unpadded. Only his
    words can carry it (`_check_nics_length`)."""

    if minutes is None:
        if nics:
            raise BrainRuntimeError("brain.invalid_argument", "length_is_nics needs rough_minutes, the number Nic gave")
        return None
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
        raise BrainRuntimeError("brain.invalid_argument", "rough_minutes must be a whole number from 1 to 1440")
    return {"minutes": minutes, "estimated_by": OWNER_ESTIMATE if nics else estimated_by}


def _padded(length: Mapping[str, object]) -> int:
    """The minutes a plan books: an agent's guess x1.5 rounded up (step C), Nic's own as he said it."""

    minutes = int(length["minutes"])
    return minutes if length["estimated_by"] == OWNER_ESTIMATE else -(-minutes * 3 // 2)


_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10,
                 "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50}
_LENGTH_WORDS = re.compile(
    r"(?P<number>\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|ten|fifteen|twenty|thirty|forty|forty-five|fifty)"
    r"\s*(?P<unit>hours?|hrs?|h|minutes?|mins?|m)\b",
    re.IGNORECASE,
)
# Longest first: a phrase is taken out of the words once it's matched, so "half an hour" is 30 and
# never also "an hour".
_PHRASE_MINUTES = (
    ("an hour and a half", 90), ("one hour and a half", 90), ("hour and a half", 90),
    ("quarter of an hour", 15), ("half an hour", 30), ("half hour", 30), ("quarter hour", 15),
)


def _lengths_in(words: str) -> set[int]:
    """Every length in minutes Nic's words say: "an hour", "90 mins", "1.5 hours", "2h", "half an
    hour", "an hour and a half". A bare number is not a length ("call at 3pm", "due 30 Sep")."""

    text = " ".join(words.casefold().replace("\u2019", "'").split())
    found: set[int] = set()
    for phrase, minutes in _PHRASE_MINUTES:
        if phrase in text:
            found.add(minutes)
            text = text.replace(phrase, " ")
    for match in _LENGTH_WORDS.finditer(text):
        raw = match["number"]
        number = float(raw) if raw[0].isdigit() else _NUMBER_WORDS[raw]
        found.add(round(number * 60) if match["unit"].startswith("h") else round(number))
    return found


def _check_nics_length(data: Mapping[str, object], source: Mapping[str, object]) -> None:
    """Step I §6: a length marked as Nic's own needs his words, like a routine's occurrence, and
    those words must say that length ("make it an hour" for 60)."""

    length = data.get("length_input")
    if not isinstance(length, Mapping) or length.get("estimated_by") != OWNER_ESTIMATE:
        return
    if not (source.get("said_by") == "owner" and source.get("came_by") == "direct" and source.get("words")):
        raise BrainRuntimeError(
            "brain.record.needs_owner_words",
            "length_is_nics marks the number as Nic's own: said_by owner, came_by direct, words with it",
        )
    if int(length["minutes"]) not in _lengths_in(str(source["words"])):
        raise BrainRuntimeError(
            "brain.record.needs_owner_words",
            f"length_is_nics needs Nic's words to say the length: they don't say {length['minutes']} minutes",
        )


_ITEM_ONLY_FIELDS = ("block", "raise_with", "first_step")


def _sorting_fields(request: object, kind: str | None) -> dict[str, object]:
    """block, raise_with and first_step as given (PRD §3, §4.1); items only."""

    fields = {name: getattr(request, name) for name in _ITEM_ONLY_FIELDS if getattr(request, name) is not None}
    if fields and kind is not None and kind != "item":
        raise BrainRuntimeError("brain.invalid_argument", "block, raise_with and first_step are for items only")
    return fields


def _check_routine_change(identity: object, record: Record, previous: Record) -> None:
    """PRD §4.7: a routine never ends by being done - only its occurrences are, in Nic's words - so
    marking the routine itself done is refused (drop it, or say it's no longer needed, to end it)."""

    if record.state == "done" and previous.state != "done" and is_routine(previous):
        raise BrainRuntimeError(
            "brain.invalid_argument",
            "A routine isn't done, only its occurrence is: use occurrence=done in Nic's words, and it stays "
            "open; to end the routine, drop it or mark it no_longer_needed",
        )
    source = identity.source
    if "occurrence_input" in identity.raw_data and not (
        source.get("said_by") == "owner" and source.get("came_by") == "direct" and source.get("words")
    ):
        raise BrainRuntimeError(
            "brain.record.needs_owner_words", "A routine's occurrence is done only on Nic's word: said_by owner, came_by direct, words",
        )


_LENGTH_FIELDS = ("duration_minutes", "padded_minutes", "estimated_by")


_PUNCTUATION_AND_SPACE = string.punctuation + " "


def _normalize_owner_words(words: str) -> str:
    """ROA-11 (Wave 3): casefold, collapse whitespace, strip surrounding punctuation."""

    collapsed = " ".join(words.casefold().split())
    return collapsed.strip(_PUNCTUATION_AND_SPACE)


def _capture_time_words(request: MemorySaveRequest) -> str | None:
    """Keep the existing capture-only extraction from the caller's own words."""
    given = request.time_words
    found = find_one_time_phrase(request.words) if request.words is not None and request.find_time_words else None
    if found is not None and (given is None or not from_words(given, request.words or "")
            or (from_words(given, found) and says_more(found, given))):
        return found
    return given


def _safe_record_path(locator: str, owner_locator: str) -> str:
    prefix = f"{owner_locator}/"
    if not locator.startswith(prefix):
        raise BrainRuntimeError("brain.record.corrupt", "Canonical record locator has the wrong owner")
    relative = locator[len(prefix) :]
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not relative
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != relative
    ):
        raise BrainRuntimeError("brain.record.corrupt", "Canonical record locator is unsafe")
    return relative


def _receipt_mapping(receipt_bytes: bytes) -> dict[str, object]:
    try:
        value = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrainRuntimeError("brain.record.corrupt", "Durability receipt is invalid") from exc
    if not isinstance(value, dict):
        raise BrainRuntimeError("brain.record.corrupt", "Durability receipt is invalid")
    return value


@dataclass(frozen=True, slots=True)
class MemoryMaterial:
    record: Record
    record_bytes: bytes
    record_path: str


@dataclass(frozen=True, slots=True)
class MemoryOperationIdentity:
    operation_id: str
    record_entropy: str
    input_sha256: str
    kind: str
    title: str
    body: str
    evidence: Evidence
    state: str
    data: dict[str, object]
    dedupe_key: str
    source: dict[str, object]
    links: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryChangeIdentity:
    operation_id: str
    record_entropy: str
    input_sha256: str
    record_id: str
    expected_sha256: str
    evidence: Evidence
    raw_data: dict[str, object]
    state: str | None
    source: dict[str, object]
    links: tuple[dict[str, object], ...] = ()
    dedupe_key: str = ""


@dataclass(frozen=True, slots=True)
class DecisionMaterial:
    record: Record
    record_bytes: bytes
    receipt: DurabilityReceipt
    receipt_bytes: bytes
    record_path: str
    receipt_path: str


class BrainCoreRecordAdapter:
    """Translate typed requests into exact existing Brain Core artifacts.

    The supplied resolvers are trusted host dependencies.  ``operation_time``
    must return the stable accepted time for an operation ID; ``source_evidence``
    resolves an authorized opaque source reference; locator resolvers expose
    only owner-scoped indexes and never accept paths from MCP callers.
    ``scoped_operation_time``, when supplied, returns that stable time for one
    authenticated caller's ``(caller, operation_id)`` (P3): memory saves and
    changes use it, so two callers with the same operation ID each keep their
    own first time. Decisions keep ``operation_time``, as does memory when no
    scoped resolver is supplied.
    """

    def __init__(
        self,
        *,
        operation_time: Callable[[str], datetime],
        source_evidence: Callable[[OwnerAuthorization, str], Evidence],
        record_locator: Callable[[OwnerAuthorization, str], str | None] | None = None,
        semantic_locator: Callable[[OwnerAuthorization, str], str | None] | None = None,
        catalog_path: str | Path | None = None,
        writer_generation: int = 1,
        scoped_operation_time: Callable[[str, str], datetime] | None = None,
    ) -> None:
        if writer_generation <= 0:
            raise ValueError("writer_generation must be positive")
        self._operation_time = operation_time
        self._scoped_operation_time = scoped_operation_time
        self._source_evidence = source_evidence
        self._record_locator = record_locator
        self._semantic_locator = semantic_locator
        self._catalog_path = catalog_path
        self._writer_generation = writer_generation
        self._known_records: dict[tuple[str, str], str] = {}
        self._known_semantics: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _scope(authorization: OwnerAuthorization) -> dict[str, str | None]:
        return {
            "kind": authorization.selector.kind,
            "project_id": authorization.selector.project_id,
        }

    def _accepted_at(self, operation_id: str, caller: str | None = None) -> datetime:
        """The pinned accepted time: per caller for memory when the host scopes it (P3)."""

        if work_check_only.get():
            return datetime.now(UTC)
        if caller is not None and self._scoped_operation_time is not None:
            accepted_at = self._scoped_operation_time(caller, operation_id)
        else:
            accepted_at = self._operation_time(operation_id)
        if not isinstance(accepted_at, datetime) or accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
            raise BrainRuntimeError(
                "brain.contract.invalid",
                "Trusted operation time must be a timezone-aware datetime",
            )
        return accepted_at

    def _evidence(self, authorization: OwnerAuthorization, reference: str) -> Evidence:
        evidence = self._source_evidence(authorization, reference)
        from gremlin_town.mcp.host_identity import attribute_evidence
        evidence = attribute_evidence(evidence)
        if not isinstance(evidence, Evidence):
            raise BrainRuntimeError(
                "brain.contract.invalid", "Trusted source resolver returned an invalid evidence type"
            )
        try:
            return evidence_from_mapping(evidence.as_dict(), "github provider evidence")
        except Exception as exc:
            raise BrainRuntimeError(
                "brain.contract.invalid", "Trusted source resolver returned invalid evidence"
            ) from exc

    @staticmethod
    def _memory_scope(authorization: OwnerAuthorization) -> str:
        selector = authorization.selector
        return selector.kind if selector.project_id is None else f"project:{selector.project_id}"

    @staticmethod
    def _recorded_by(evidence: Evidence) -> str:
        """The authenticated caller (R10) - never the request body. Falls back to the
        trusted evidence resolver's actor when no host attribution is bound (e.g. a direct
        provider call outside the MCP/HTTP tool boundary, which always binds one)."""

        from gremlin_town.mcp.host_identity import current_write_attribution
        attribution = current_write_attribution()
        recorded_by = attribution["authenticated_subject"] if attribution else evidence.actor
        if not recorded_by:
            raise BrainRuntimeError("brain.contract.invalid", "Memory save requires an identified actor")
        return recorded_by

    def _memory_evidence(self, authorization: OwnerAuthorization, source_reference: str, words: str | None) -> Evidence:
        """The v2 evidence excerpt IS the quoted words (R2); provenance (locator, hash,
        actor, grant) still comes from the trusted source resolver."""

        evidence = self._evidence(authorization, source_reference)
        return evidence if words is None else replace(evidence, excerpt=words)

    @staticmethod
    def _source_mapping(
        said_by: str, came_by: str, words: str | None, speaker: str | None, recorded_by: str
    ) -> dict[str, object]:
        """Build the v2 source mapping, refusing here - before ``record_from_mapping`` ever
        runs - the same three rules ``brain_core.records._source_from_mapping`` enforces:
        ``words`` is required unless ``came_by`` is ``system``; ``speaker`` is required when
        ``said_by`` is ``agent`` or ``other``; ``said_by: owner`` requires ``came_by: direct``.
        A fourth check (ROA-8) matches ``brain_core.language.check_change``'s R11: with
        ``said_by: owner``, ``words`` that is nothing but a bare instruction to save
        something already said (e.g. "Save it to gtbc") is refused here too, so a caller
        through this provider gets the same precise message instead of a deep, generic
        failure - the hosted and local paths end up enforcing the identical rule.

        Found live 2026-09-26: a failure deep inside ``record_from_mapping`` only ever
        surfaced as the generic ``brain.contract.invalid`` ("Memory request is not a valid
        Brain Core record"), naming neither the missing field nor the rule - Diablo needed
        6 tries and Codex's plain-agent saves all failed before both fields were added by
        trial and error. Checking the same rules here instead refuses with
        ``brain.invalid_argument`` and a message naming the exact field and rule.
        """

        if said_by == "owner" and came_by != "direct":
            raise BrainRuntimeError(
                "brain.invalid_argument",
                "came_by must be direct when said_by is owner",
            )
        if said_by in ("agent", "other") and (speaker is None or not speaker.strip()):
            raise BrainRuntimeError(
                "brain.invalid_argument",
                "speaker is required when said_by is agent or other",
            )
        if came_by != "system" and (words is None or not words.strip()):
            raise BrainRuntimeError(
                "brain.invalid_argument",
                "words is required unless came_by is system",
            )
        if said_by == "owner" and words is not None and language.is_save_instruction_only(words):
            raise BrainRuntimeError(
                "brain.invalid_argument",
                "words must be what Nic said about the item, not an instruction to save it; "
                "quote his message about it",
            )
        source: dict[str, object] = {"said_by": said_by, "came_by": came_by, "recorded_by": recorded_by}
        if speaker is not None:
            source["speaker"] = speaker
        if words is not None:
            source["words"] = words
        return source

    @staticmethod
    def _resolve_timing(time_words: str, accepted_at: datetime) -> dict[str, object]:
        today = today_in_singapore(accepted_at)
        try:
            # The Singapore time of day lets a time alone that has already passed today
            # mean tomorrow ("12am" said at 21:50; speed-03, Proposed).
            resolved = resolve_date_and_time(
                time_words, today=today, now=accepted_at.astimezone(SINGAPORE).time()
            )
        except BrainError as exc:
            raise BrainRuntimeError(str(exc.code), exc.message) from exc
        resolved_from = {"words": time_words, "today": today.isoformat()}
        if resolved.window is not None:
            # "this/next week(end)" (speed-03, Proposed): a window, no due date.
            start, end = resolved.window
            return {"window": {"start": start, "end": end}, "resolved_from": resolved_from}
        timing: dict[str, object] = {"due": resolved.due, "resolved_from": resolved_from}
        # P2 (docs/product/steps/c-record-format.md §5; Proposed - Nic approves before
        # speed-02 goes live): only present when the words also named a time of day.
        if resolved.at is not None:
            timing["at"] = resolved.at
        return timing

    def _raw_memory_data(
        self, authorization: OwnerAuthorization, request: MemorySaveRequest, recorded_by: str
    ) -> tuple[str, dict[str, object]]:
        """Data as the caller gave it, before any clock-dependent resolution (§5)."""

        if request.kind != "item" and (request.importance is not None or request.importance_reason is not None):
            raise BrainRuntimeError("brain.invalid_argument", "importance is for items only")
        if request.kind != "preference" and request.focus_projects is not None:
            raise BrainRuntimeError("brain.invalid_argument", "focus_projects is for a preference only")
        sorting = _sorting_fields(request, request.kind)
        if request.kind != "item" and request.rough_minutes is not None:
            raise BrainRuntimeError("brain.invalid_argument", "rough_minutes is for items only")
        if request.sort == "drop":
            raise BrainRuntimeError(
                "brain.invalid_argument", "sort=drop: nothing to save; only Nic's words drop something already saved",
            )
        if request.sort is not None and request.kind == "preference":
            raise BrainRuntimeError("brain.invalid_argument", "sort is for items and facts only")
        if request.kind == "item":
            if request.level is None:
                raise BrainRuntimeError("brain.invalid_argument", "level is required for an item")
            data: dict[str, object] = {
                "level": request.level,
                "size": request.size or "task",
                "owed_to": request.owed_to,
                "repeat": request.repeat,
                "reason": None,
                "check": None,
                "parked_until": None,
            }
            time_words = request.time_words
            found = (
                find_one_time_phrase(request.words)
                if request.words is not None and request.find_time_words
                else None
            )
            # 2c fallback: when the agent gave the words but not time_words, and exactly one
            # time phrase is found inside them, use it - it then joins the input identity
            # exactly like an agent-given time_words (R7 still holds: a found phrase always
            # comes from words). Several different phrases, or one that leans on a
            # three-letter weekday that is also an English word ("in the sun"), are not
            # resolved: nothing is guessed, so timing is left empty instead
            # (docs/product/steps/c-record-format.md 2c). Change requests do not get this
            # fallback.
            # Nic, 29 Sep 2026 ("dont need to be my exact words"): the found time also wins
            # over agent time_words that did not come from his message ("tomorrow noon" for
            # his "tmr ... at 12pm"), instead of a refusal and a retry, and over agent
            # time_words that are only part of it ("at 12pm" of "tmr at 12pm"), so the day he
            # said is never lost. With nothing found, time_words stay as given and R7 judges them.
            if found is not None and (
                time_words is None
                or not from_words(time_words, request.words or "")
                or (from_words(time_words, found) and says_more(found, time_words))
            ):
                time_words = found
            if time_words is not None:
                data["time_words"] = time_words
            data.update(sorting)
            length = _rough_length(request.rough_minutes, recorded_by, nics=request.length_is_nics)
            if length is not None:
                data["length_input"] = length
            if request.sort is not None:
                data["sort_input"] = request.sort
            # A new item waiting on someone is saved waiting (PRD §4.1); a project's step waiting on
            # another starts blocked (§4.10); anything else open.
            if request.initial_state is not None:
                return request.initial_state, data
            return ("waiting" if request.sort == "waiting_on" else "open"), data
        if request.kind == "fact":
            fact: dict[str, object] = {"observed_at": request.observed_at, "valid_until": request.valid_until}
            if request.sort is not None:
                fact["sort_input"] = request.sort
            return "current", fact
        if request.kind == "preference":
            data = {
                "scope": self._memory_scope(authorization),
                "stability": "durable",
                "evidence": [request.source_reference],
                "correction_history": [],
            }
            if request.focus_projects is not None:
                # Ciel redesign N4: Nic's focus projects; his newest list wins.
                data["focus_projects"] = list(request.focus_projects)
            return "active", data
        raise BrainRuntimeError("brain.invalid_argument", "kind must be item, fact, or preference")

    @staticmethod
    def _resolved_memory_data(kind: str, raw_data: dict[str, object], accepted_at: datetime) -> dict[str, object]:
        """Resolve clock-dependent dates once ``accepted_at`` is known."""

        data = dict(raw_data)
        data.pop("sort_input", None)  # checked against the whole record, never stored (PRD §4.1)
        # PRD §4.7: the routine's latest occurrence, on or before the day this change is accepted.
        if data.pop("occurrence_input", None) is not None:
            day = occurrence_day(data.get("repeat"), today_in_singapore(accepted_at)) if kind == "item" else None
            if day is None:
                raise BrainRuntimeError(
                    "brain.invalid_argument", "occurrence is for a routine: an item that repeats weekly:<day> or daily",
                )
            data["occurrence"] = {"day": day.isoformat(), "outcome": "done"}
        length_input = data.pop("length_input", None)
        if length_input is not None and kind != "item":
            raise BrainRuntimeError("brain.invalid_argument", "rough_minutes is for items only")
        if kind != "item" and any(field in data for field in _ITEM_ONLY_FIELDS):
            raise BrainRuntimeError("brain.invalid_argument", "block, raise_with and first_step are for items only")
        if kind == "item":
            time_words = data.pop("time_words", None)
            earlier = dict(data.get("timing") or {})
            if time_words is not None:
                data["timing"] = BrainCoreRecordAdapter._resolve_timing(time_words, accepted_at)
                # New dates replace the old ones; a rough length already given stays (PRD §3).
                data["timing"].update({field: earlier[field] for field in _LENGTH_FIELDS if field in earlier})
            else:
                data.setdefault("timing", {})
            if length_input is not None:
                minutes = int(length_input["minutes"])
                data["timing"] = {
                    **data["timing"],
                    "duration_minutes": minutes,
                    "padded_minutes": _padded(length_input),
                    "estimated_by": length_input["estimated_by"],
                }
        elif kind == "fact" and data.get("observed_at") is None:
            data["observed_at"] = accepted_at.isoformat()
        return data

    def memory_identity(
        self, authorization: OwnerAuthorization, request: MemorySaveRequest
    ) -> MemoryOperationIdentity:
        """Build the stable operation and exact-input identity without reading the clock."""

        evidence = self._memory_evidence(authorization, request.source_reference, request.words)
        recorded_by = self._recorded_by(evidence)
        source = self._source_mapping(request.said_by, request.came_by, request.words, request.speaker, recorded_by)
        state, data = self._raw_memory_data(authorization, request, recorded_by)
        _check_nics_length(data, source)
        links = tuple(dict(item) for item in request.links)
        payload = {
            "schema": "gt.brain-memory-input/v2",
            "operation_id": request.operation_id,
            "kind": request.kind,
            "title": request.title,
            "body": request.body,
            "state": state,
            "owner_id": authorization.owner_id,
            "owner_locator": authorization.owner_locator,
            "evidence": evidence.as_dict(),
            "requested_dedupe_key": request.dedupe_key,
            "data": data,
            "source": source,
            "links": [dict(item) for item in links],
        }
        return MemoryOperationIdentity(
            request.operation_id,
            # P3 (finding 15): scoped by the authenticated caller, so two different
            # callers minting the same operation_id from the words get distinct
            # records instead of colliding.
            memory_operation_record_suffix(request.operation_id, caller=recorded_by),
            hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
            request.kind,
            request.title,
            request.body,
            evidence,
            state,
            data,
            "",
            source,
            links,
        )

    def planned_record_id(self, identity: MemoryOperationIdentity) -> str:
        """The record ID `materialize_memory` gives this identity: its pinned time and its suffix,
        so a whole project's steps can link to each other before any is saved (PRD §4.10)."""

        return new_record_id(self._accepted_at(identity.operation_id, identity.source["recorded_by"]), identity.record_entropy)

    def assert_exact_memory_replay(
        self,
        authorization: OwnerAuthorization,
        identity: MemoryOperationIdentity,
        record: Record,
    ) -> None:
        resolved_data = self._resolved_memory_data(identity.kind, identity.data, record.created_at)
        if (
            record.schema != RECORD_SCHEMA_V2
            or record.kind not in {"item", "fact", "preference"}
            or record.kind != identity.kind
            or record.title != identity.title
            or record.body != identity.body
            or record.vaults != (authorization.owner_locator,)
            or record.owners != (authorization.owner_id,)
            or record.sensitivity != "private"
            or record.state != identity.state
            or record.evidence != (identity.evidence,)
            or record.dedupe_key != identity.dedupe_key
            or record.data != resolved_data
            or record.source != identity.source
            or record.links != identity.links
        ):
            raise BrainRuntimeError(
                "brain.record.exists",
                "Stable memory operation is already bound to different input",
            )

    def materialize_memory(
        self,
        authorization: OwnerAuthorization,
        request: MemorySaveRequest,
        identity: MemoryOperationIdentity | None = None,
    ) -> MemoryMaterial:
        identity = identity or self.memory_identity(authorization, request)
        accepted_at = self._accepted_at(request.operation_id, identity.source["recorded_by"])
        record_id = new_record_id(
            accepted_at,
            identity.record_entropy,
        )
        resolved_data = self._resolved_memory_data(identity.kind, identity.data, accepted_at)
        try:
            record = record_from_mapping(
                {
                    "schema": RECORD_SCHEMA_V2,
                    "id": record_id,
                    "kind": request.kind,
                    "title": request.title,
                    "state": identity.state,
                    "created_at": accepted_at.isoformat(),
                    "updated_at": accepted_at.isoformat(),
                    "sensitivity": "private",
                    "vaults": [authorization.owner_locator],
                    "owners": [authorization.owner_id],
                    "entities": [],
                    "evidence": [identity.evidence.as_dict()],
                    "supersedes": [],
                    "dedupe_key": identity.dedupe_key,
                    "data": resolved_data,
                    "body": request.body,
                    "source": identity.source,
                    "links": [dict(item) for item in identity.links],
                },
                f"memory operation {request.operation_id}",
            )
        except Exception as exc:
            raise BrainRuntimeError(
                "brain.contract.invalid", f"Memory request is not a valid Brain Core record: {exc}"
            ) from exc
        try:
            language.check_change(None, record, actor=identity.source["recorded_by"])
        except BrainError as exc:
            raise BrainRuntimeError(str(exc.code), exc.message) from exc
        content = render_record(record).encode("utf-8")
        return MemoryMaterial(record, content, _record_path(record))

    @staticmethod
    def _raw_change_data(request: MemoryChangeRequest, recorded_by: str) -> dict[str, object]:
        """Change data as the caller gave it; merged onto the previous record's data (R1-R12)."""

        data: dict[str, object] = {}
        for field in ("level", "size", "owed_to", "repeat", "reason", "parked_until"):
            value = getattr(request, field)
            if value is not None:
                data[field] = value
        if request.time_words is not None:
            data["time_words"] = request.time_words
        if request.check_locator is not None or request.checked_by is not None:
            data["check"] = {"locator": request.check_locator, "checked_by": request.checked_by}
        data.update(_sorting_fields(request, None))
        length = _rough_length(request.rough_minutes, recorded_by, nics=request.length_is_nics)
        if length is not None:
            data["length_input"] = length
        if request.sort is not None:
            data["sort_input"] = request.sort
        if request.occurrence is not None:
            data["occurrence_input"] = request.occurrence
        return data

    def memory_change_identity(
        self, authorization: OwnerAuthorization, request: MemoryChangeRequest
    ) -> MemoryChangeIdentity:
        """Build the stable change operation and exact-input identity without reading the clock."""

        evidence = self._memory_evidence(authorization, request.source_reference, request.words)
        recorded_by = self._recorded_by(evidence)
        source = self._source_mapping(request.said_by, request.came_by, request.words, request.speaker, recorded_by)
        raw_data = self._raw_change_data(request, recorded_by)
        _check_nics_length(raw_data, source)
        links = tuple(dict(item) for item in request.links)
        payload = {
            "schema": "gt.brain-memory-change-input/v1",
            "operation_id": request.operation_id,
            "record_id": request.record_id,
            "expected_sha256": request.expected_sha256,
            "state": request.state,
            "data": raw_data,
            "source": source,
            "evidence": evidence.as_dict(),
            "links": [dict(item) for item in links],
        }
        return MemoryChangeIdentity(
            request.operation_id,
            # P3 (finding 15): scoped by the authenticated caller, matching memory_identity.
            memory_operation_record_suffix(request.operation_id, caller=recorded_by),
            hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
            request.record_id,
            request.expected_sha256,
            evidence,
            raw_data,
            request.state,
            source,
            links,
        )

    def materialize_memory_change(
        self,
        authorization: OwnerAuthorization,
        request: MemoryChangeRequest,
        previous: Record,
        identity: MemoryChangeIdentity | None = None,
    ) -> MemoryMaterial:
        identity = identity or self.memory_change_identity(authorization, request)
        accepted_at = self._accepted_at(request.operation_id, identity.source["recorded_by"])
        record_id = new_record_id(accepted_at, identity.record_entropy)
        merged_raw = dict(previous.data)
        merged_raw.update(identity.raw_data)
        resolved_data = self._resolved_memory_data(previous.kind, merged_raw, accepted_at)
        target_state = previous.state if identity.state is None else identity.state
        try:
            record = record_from_mapping(
                {
                    "schema": RECORD_SCHEMA_V2,
                    "id": record_id,
                    "kind": previous.kind,
                    "title": previous.title,
                    "state": target_state,
                    "created_at": accepted_at.isoformat(),
                    "updated_at": accepted_at.isoformat(),
                    "sensitivity": previous.sensitivity,
                    "vaults": list(previous.vaults),
                    "owners": list(previous.owners),
                    "entities": list(previous.entities),
                    "evidence": [item.as_dict() for item in (*previous.evidence, identity.evidence)],
                    "supersedes": list(dict.fromkeys((*previous.supersedes, previous.id))),
                    "dedupe_key": identity.dedupe_key,
                    "data": resolved_data,
                    "body": previous.body,
                    "source": identity.source,
                    "links": [dict(item) for item in (*previous.links, *identity.links)],
                },
                f"memory change {request.operation_id}",
            )
        except Exception as exc:
            raise BrainRuntimeError(
                "brain.contract.invalid", f"Memory change is not a valid Brain Core record: {exc}"
            ) from exc
        _check_routine_change(identity, record, previous)
        try:
            language.check_change(previous, record, actor=identity.source["recorded_by"])
        except BrainError as exc:
            raise BrainRuntimeError(str(exc.code), exc.message) from exc
        content = render_record(record).encode("utf-8")
        return MemoryMaterial(record, content, _record_path(record))

    def assert_exact_memory_change_replay(
        self,
        authorization: OwnerAuthorization,
        identity: MemoryChangeIdentity,
        record: Record,
        previous: Record,
    ) -> None:
        merged_raw = dict(previous.data)
        merged_raw.update(identity.raw_data)
        resolved_data = self._resolved_memory_data(previous.kind, merged_raw, record.created_at)
        target_state = previous.state if identity.state is None else identity.state
        expected_links = tuple(dict(item) for item in (*previous.links, *identity.links))
        if (
            record.schema != RECORD_SCHEMA_V2
            or record.kind != previous.kind
            or record.title != previous.title
            or record.body != previous.body
            or record.vaults != previous.vaults
            or record.owners != previous.owners
            or record.sensitivity != previous.sensitivity
            or record.state != target_state
            or record.data != resolved_data
            or record.source != identity.source
            or record.dedupe_key != identity.dedupe_key
            or identity.record_id not in record.supersedes
            or record.links != expected_links
        ):
            raise BrainRuntimeError(
                "brain.record.exists",
                "Stable memory change operation is already bound to different input",
            )

    def decision_semantic_key(
        self, authorization: OwnerAuthorization, request: DecisionRequest
    ) -> str:
        normalized = DurabilityRequest.from_mapping(
            {
                "schema": "brain.durability-request/v1",
                "verb": "decide",
                "operation": "accept",
                "origin_id": request.operation_id,
                "choice": request.choice,
                "title": request.title,
                "because": request.because,
                "forecloses": request.forecloses,
                "governing_scope": request.governing_scope,
                "encoded_in": request.encoded_in,
                "completeness_check": request.completeness_check,
                "explicit_scope": self._scope(authorization),
                "default_scope": {"kind": "ambiguous", "project_id": None},
                "source_context_sha256": "0" * 64,
                "session_delta_sha256": None,
                "accepted_at": "2000-01-01T00:00:00+00:00",
                "ownership_catalog": {
                    "path": CATALOG_PATH,
                    "bytes": CATALOG_BYTES,
                    "sha256": CATALOG_SHA256,
                    "schema": CATALOG_SCHEMA,
                    "catalog_id": CATALOG_ID,
                },
                "expected_owner_locator": authorization.owner_locator,
                "expected_writer_generation": self._writer_generation,
                "observed_preimage_sha256": EMPTY_SHA256,
            }
        )
        payload = {
            "choice": normalized.choice,
            "governing_scope": normalized.governing_scope,
            "operation": "accept",
            "owner_locator": authorization.owner_locator,
            "schema": "brain.durable-decision-key/v1",
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def _durability_request(
        self, authorization: OwnerAuthorization, request: DecisionRequest
    ) -> dict[str, object]:
        evidence = self._evidence(authorization, request.source_reference)
        from gremlin_town.mcp.host_identity import current_write_attribution
        provenance = {"source_evidence": [evidence.as_dict()]} if current_write_attribution() else {}
        return {
            **provenance,
            "schema": "brain.durability-request/v1",
            "verb": "decide",
            "operation": "accept",
            "origin_id": request.operation_id,
            "choice": request.choice,
            "title": request.title,
            "because": request.because,
            "forecloses": request.forecloses,
            "governing_scope": request.governing_scope,
            "encoded_in": request.encoded_in,
            "completeness_check": request.completeness_check,
            "explicit_scope": self._scope(authorization),
            "default_scope": {"kind": "ambiguous", "project_id": None},
            "source_context_sha256": evidence.content_sha256,
            "session_delta_sha256": None,
            "accepted_at": self._accepted_at(request.operation_id).isoformat(),
            "ownership_catalog": {
                "path": CATALOG_PATH,
                "bytes": CATALOG_BYTES,
                "sha256": CATALOG_SHA256,
                "schema": CATALOG_SCHEMA,
                "catalog_id": CATALOG_ID,
            },
            "expected_owner_locator": authorization.owner_locator,
            "expected_writer_generation": self._writer_generation,
            "observed_preimage_sha256": EMPTY_SHA256,
        }

    def materialize_decision(
        self,
        authorization: OwnerAuthorization,
        request: DecisionRequest,
        *,
        existing_record_bytes: bytes | None = None,
    ) -> DecisionMaterial:
        semantic_key = self.decision_semantic_key(authorization, request)
        with tempfile.TemporaryDirectory(prefix="gt-bc-writer-") as temporary:
            root = Path(temporary)
            brain_home = root / "brain-home"
            meta_vault = root / "meta-vault"
            brain_home.mkdir()
            (meta_vault / "ops").mkdir(parents=True)
            (meta_vault / "ops" / "decisions.md").write_text(
                "# Decisions\n", encoding="utf-8", newline="\n"
            )
            if existing_record_bytes is not None:
                try:
                    existing = parse_record(
                        existing_record_bytes.decode("utf-8"), "staged remote decision"
                    )
                except Exception as exc:
                    raise BrainRuntimeError(
                        "brain.record.corrupt", "Remote semantic decision is invalid"
                    ) from exc
                if (
                    existing.kind != "decision"
                    or existing.dedupe_key != semantic_key
                    or authorization.owner_locator not in existing.vaults
                ):
                    raise BrainRuntimeError(
                        "brain.record.corrupt", "Remote semantic decision does not match the request"
                    )
                staged = brain_home / _record_path(existing)
                staged.parent.mkdir(parents=True)
                staged.write_bytes(existing_record_bytes)
            writer = SharedDurabilityWriter(
                root_bindings={
                    _BRAIN_HOME: brain_home,
                    "vault://meta-vault": meta_vault,
                },
                catalog_path=self._catalog_path,
            )
            receipt = writer.write(self._durability_request(authorization, request))
            if receipt.status not in {"created", "duplicate/no-op"} or receipt.refusal_code is not None:
                raise BrainRuntimeError(
                    "brain.record.write_failed",
                    f"Shared durability writer refused the decision: {receipt.refusal_code or receipt.status}",
                )
            if receipt.owner_locator != authorization.owner_locator or receipt.owner_id != authorization.owner_id:
                raise BrainRuntimeError(
                    "brain.policy.excluded", "Shared durability writer resolved a different owner"
                )
            if not isinstance(receipt.canonical_locator, str):
                raise BrainRuntimeError("brain.record.corrupt", "Durability receipt has no canonical locator")
            record_path = _safe_record_path(receipt.canonical_locator, authorization.owner_locator)
            receipt_path = f"{_RECEIPT_ROOT}/{request.operation_id}.json"
            record_bytes = (brain_home / record_path).read_bytes()
            receipt_bytes = (brain_home / receipt_path).read_bytes()
            record = parse_record(record_bytes.decode("utf-8"), "materialized decision")
            if record.dedupe_key != semantic_key or receipt.semantic_key != semantic_key:
                raise BrainRuntimeError("brain.record.corrupt", "Decision semantic identity changed")
            return DecisionMaterial(
                record,
                record_bytes,
                receipt,
                receipt_bytes,
                record_path,
                receipt_path,
            )

    def assert_exact_decision_replay(
        self,
        authorization: OwnerAuthorization,
        request: DecisionRequest,
        *,
        record_bytes: bytes,
        receipt_bytes: bytes,
    ) -> Record:
        try:
            record = parse_record(record_bytes.decode("utf-8"), "accepted decision replay")
        except Exception as exc:
            raise BrainRuntimeError("brain.record.corrupt", "Accepted decision record is invalid") from exc
        receipt = _receipt_mapping(receipt_bytes)
        from gremlin_town.mcp.host_identity import current_write_attribution
        evidence = self._evidence(authorization, request.source_reference)
        expected_data = {
            "because": request.because,
            "forecloses": request.forecloses,
            "governs": request.governing_scope,
            "encoded_in": request.encoded_in,
            "completeness_check": request.completeness_check,
            "origin_id": request.operation_id,
        }
        expected_semantic = self.decision_semantic_key(authorization, request)
        exact = (
            record.kind == "decision"
            and record.title == request.title
            and record.state == "accepted"
            and record.assertion == "explicit"
            and record.confidence == 1.0
            and record.sensitivity == "private"
            and record.vaults == (authorization.owner_locator,)
            and record.owners == (authorization.owner_id,)
            and record.dedupe_key == expected_semantic
            and record.data == expected_data
            and record.body == ""
            and len(record.evidence) == (2 if current_write_attribution() else 1)
            and (not current_write_attribution() or record.evidence[1] == evidence)
            and record.evidence[0].source_type == "durability_request"
            and record.evidence[0].locator == f"origin:{request.operation_id}"
            and record.evidence[0].content_sha256 == evidence.content_sha256
            and record.evidence[0].excerpt == request.choice
            and receipt.get("schema") == RECEIPT_SCHEMA
            and receipt.get("writer_id") == WRITER_ID
            and receipt.get("origin_id") == request.operation_id
            and receipt.get("semantic_key") == expected_semantic
            and receipt.get("canonical_identity") == record.id
            and receipt.get("canonical_sha256") == hashlib.sha256(record_bytes).hexdigest()
            and receipt.get("owner_locator") == authorization.owner_locator
            and receipt.get("owner_id") == authorization.owner_id
        )
        if not exact:
            raise BrainRuntimeError(
                "brain.record.exists",
                "Stable decision operation is already bound to different input",
            )
        return record

    def remember_record(self, authorization: OwnerAuthorization, record_id: str, path: str) -> None:
        with self._lock:
            self._known_records[(authorization.owner_locator, record_id)] = path

    def locate_record(self, authorization: OwnerAuthorization, record_id: str) -> str:
        with self._lock:
            path = self._known_records.get((authorization.owner_locator, record_id))
        if path is None and self._record_locator is not None:
            path = self._record_locator(authorization, record_id)
        if not isinstance(path, str) or not path:
            raise BrainRuntimeError("brain.not_found", "Memory record was not found")
        return path

    def remember_semantic(self, authorization: OwnerAuthorization, semantic_key: str, path: str) -> None:
        with self._lock:
            self._known_semantics[(authorization.owner_locator, semantic_key)] = path

    def locate_semantic(self, authorization: OwnerAuthorization, semantic_key: str) -> str | None:
        with self._lock:
            path = self._known_semantics.get((authorization.owner_locator, semantic_key))
        if path is None and self._semantic_locator is not None:
            path = self._semantic_locator(authorization, semantic_key)
        return path


class GitHubBrainProvider:
    """Concrete MemoryProvider and DecisionProvider over BC GitHub storage."""

    def __init__(
        self,
        storage: GitHubBrainHomeStorage,
        record_adapter: BrainCoreRecordAdapter,
        *,
        review_enabled: bool = False,
        calendar: CalendarWriter | None = None,
        why_log=None,
        door_participant: str = "hermes-wa",
        code_callers: frozenset[str] = frozenset(),
        schedule_wake: Callable[[], None] | None = None,
        schedule_lengths: bool = False,
        past_appointment_done: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        replay_marker: Path | None = None,
    ) -> None:
        if not isinstance(storage, GitHubBrainHomeStorage):
            raise TypeError("storage must be GitHubBrainHomeStorage")
        if not isinstance(record_adapter, BrainCoreRecordAdapter):
            raise TypeError("record_adapter must be BrainCoreRecordAdapter")
        if review_enabled and calendar is not None and why_log is None:
            raise BrainRuntimeError("brain.capability.unavailable", "Calendar effects need the existing review why-log",
                field="calendar", fix="supply the configured review why-log")
        self._storage = storage
        self._why_log = why_log
        self.door_participant = door_participant
        from .surface_door import SURFACE_CALLERS
        if not code_callers <= SURFACE_CALLERS:
            raise ValueError("Unknown surface code caller")
        self.code_callers = code_callers
        self.schedule_wake = schedule_wake
        self.schedule_lengths = schedule_lengths
        self._past_appointment_done = past_appointment_done
        self._clock = clock
        self.replay_marker = replay_marker
        self._records = record_adapter
        # Review state is supplied by the host; calendar entries use the real writer.
        # to the save/change confirmation for a timed item (spec §5).
        self._review_enabled = review_enabled
        # N8 (off by default): the host passes a calendar writer only when [review] is on
        # and the Google credential carries calendar.events. Then a task with a start time
        # gets its own calendar entry instead of a suggestion (step E spec §5).
        self._calendar = calendar if review_enabled else None
        self._decision_lock = threading.RLock()
        self._memory_operation_locks: dict[str, threading.Lock] = {}
        self._memory_operation_locks_guard = threading.Lock()

    @staticmethod
    def _authorize(
        authorization: OwnerAuthorization,
        operation: BrainOperation,
        access: str,
    ) -> None:
        if (
            not isinstance(authorization, OwnerAuthorization)
            or authorization.operation is not operation
            or authorization.access != access
            or authorization.owner_locator != _BRAIN_HOME
            or not authorization.owner_id.strip()
            or authorization.capability is None
        ):
            raise BrainRuntimeError(
                "brain.policy.excluded", "Owner authorization does not permit this operation"
            )

    @staticmethod
    def _map_storage_error(
        exc: Exception, *, reading: bool = False, nothing_saved: bool | None = None,
    ) -> BrainRuntimeError:
        return map_storage_error(exc, reading=reading, nothing_saved=nothing_saved)

    def _confirmation(
        self, record: Record, *, changed: bool
    ) -> tuple[
        str | None, dict[str, object] | None, tuple[dict[str, object], ...],
        dict[str, object] | None,
    ]:
        """``_memory_confirmation`` plus the calendar part: ``(word, timing, links, calendar)``.
        With calendar entries off, ``calendar`` is ``None``
        and the rest is exactly ``_memory_confirmation``'s."""

        word, timing, links = _memory_confirmation(
            record, review_enabled=self._review_enabled
        )
        if self._calendar is None or self._validate_only(self._caller()):
            return word, timing, links, None
        from gremlin_town.review import calendar_entries

        try:
            calendar = calendar_entries.follow(self._calendar, record, changed=changed,
                keep_past_done=self._past_appointment_done, now=self._clock())
        except Exception as exc:  # the Brain save already stands; say the calendar part failed
            logger.warning("Calendar part of a Brain write failed (%s)", type(exc).__name__)
            calendar = calendar_entries.failed("the calendar part", "internal_error")
        return word, timing, links, calendar

    @contextlib.contextmanager
    def _memory_operation_lock(self, operation_id: str):
        """Hold one lock per ``operation_id`` so a resend waits instead of racing."""

        with self._memory_operation_locks_guard:
            entry = self._memory_operation_locks.get(operation_id)
            if entry is None:
                entry = _RefCountedLock()
                self._memory_operation_locks[operation_id] = entry
            entry.refs += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._memory_operation_locks_guard:
                entry.refs -= 1
                if entry.refs == 0 and self._memory_operation_locks.get(operation_id) is entry:
                    del self._memory_operation_locks[operation_id]

    async def save(
        self, authorization: OwnerAuthorization, request: MemorySaveRequest
    ) -> WriteReceipt:
        self._authorize(authorization, BrainOperation.MEMORY_SAVE, "write")
        return await anyio.to_thread.run_sync(self._save_serialized, authorization, request)

    def _caller(self) -> str | None:
        from .host_identity import current_write_attribution
        attribution = current_write_attribution()
        return attribution["authenticated_subject"] if attribution else None

    def _validate_only(self, caller: str | None) -> bool:
        return work_check_only.get() or bool(caller == self.door_participant and
            self.replay_marker is not None and self.replay_marker.is_file())

    @staticmethod
    def _work_suffix(operation: str, ordinal: int, slot: int, caller: str) -> str:
        # A separate namespace keeps a bridge save with the same retry key out of undo.
        key = hashlib.sha256(operation.encode()).hexdigest()
        return memory_operation_record_suffix(f"work-{key}-{ordinal}-{slot}", caller=caller)

    async def work(self, authorization: OwnerAuthorization, request: WorkRequest) -> Mapping[str, object]:
        """Repeat the host gate before reading or executing any public door change."""
        self._authorize(authorization, BrainOperation.WORK, "write")
        caller = self._caller()
        if not work_caller_allowed(caller, door_participant=self.door_participant, code_callers=self.code_callers):
            raise BrainRuntimeError("ACCESS_DENIED", "This caller cannot change Nic's work", field="caller",
                fix="save it as a new thing with memory.save; Nic's morning review merges it")
        if caller.startswith("system:"):
            from .surface_door import owner_tap_matches
            if not owner_tap_matches(caller, authorization.owner_id, request):
                raise BrainRuntimeError("ACCESS_DENIED", "An authenticated owner tap is required", field="caller",
                    fix="authenticate the owner on this surface and submit the exact tapped change")
        if authorization.selector.kind != "personal":
            raise BrainRuntimeError("ACCESS_DENIED", "The door writes the personal owner only", field="scope",
                fix="use the personal owner scope")
        return await anyio.to_thread.run_sync(self._work_serialized, authorization, request, caller)

    def _work_serialized(self, authorization, request, caller):
        with self._memory_operation_lock(f"work:{caller}:{request.operation_id}"):
            from .surface_door import owner_tap_matches
            from brain_core import work
            output = []
            result = self._work(authorization, request, caller=caller, record_output=output)
            if (caller in self.code_callers and owner_tap_matches(caller, authorization.owner_id, request)
                    and result.get("remote_observed") is True):
                # Private code-call result, after the public reply budget. The
                # exact publication/replay records cover every added thing even
                # when `things` was shortened for an agent's tool response.
                result["created_refs"] = [work.first_id(record) for record in output if not record.supersedes]
            return result

    def _work_records(self, authorization, revision=None):
        try:
            snapshot = self._storage.snapshot_records(kinds=frozenset({"item"}),
                max_records=SCAN_MAX_RECORDS, max_bytes=SCAN_MAX_BYTES, revision=revision)
            records = [entry.record for entry in snapshot.records
                if authorization.owner_id in entry.record.owners and authorization.owner_locator in entry.record.vaults]
            return snapshot.revision, records
        except Exception as exc:
            raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc

    def _exact_work_records(self, records, operation, caller, revision):
        suffixes = {self._work_suffix(operation, i, slot, caller) for i in range(20) for slot in range(26)}
        candidates = {rid for record in records for rid in (record.id, *record.supersedes)
            if rid.rsplit("_", 1)[-1] in suffixes}
        raw = {r.id: r for r in records}
        found = []
        for rid in sorted(candidates):
            record = raw.get(rid)
            if record is None:
                match = find_by_id(self._storage, rid, revision=revision).match
                if match is None:
                    raise BrainRuntimeError("brain.record.exists", "Part of this change is missing", field="operation_id",
                        fix="send the correction as a new change; nothing was saved")
                record = match.record
            if record.source is None or record.source.get("recorded_by") != caller:
                raise BrainRuntimeError("ACCESS_DENIED", "The planned record belongs to another caller",
                    field="operation_id", fix="send the correction as a new change")
            found.append(record)
        return found

    def _replay_work_records(self, authorization, operation, caller, revision):
        """Discover exact retry IDs, including wholly archived batches, only for replay."""
        suffixes = tuple(sorted({self._work_suffix(operation, i, slot, caller)
            for i in range(20) for slot in range(26)}))
        try:
            matches = self._storage.preflight_work_records(record_suffixes=suffixes, caller=caller,
                revision=revision, max_candidates=25)
        except Exception as exc:
            raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc
        records = []
        for match in matches:
            record = match.record
            if authorization.owner_id not in record.owners or authorization.owner_locator not in record.vaults:
                raise BrainRuntimeError("ACCESS_DENIED", "The planned work record belongs to a different owner",
                    field="scope", fix="use the authorized personal owner")
            records.append(record)
        return records

    def _work_calendar(self, authorization, request, caller, public_hash, brain_commit, prepared, current):
        """Reconcile current records only, retaining the first successful acknowledgment.

        Called after exact record/hash/batch verification and only for real writes.
        Historical acknowledgments never authorize another calendar effect.
        """
        if self._calendar is None:
            return None
        if self._why_log is None:
            raise BrainRuntimeError("brain.capability.unavailable", "Calendar effects need the existing review why-log",
                field="calendar", fix="supply the configured review why-log")
        key = dict(owner_locator=authorization.owner_locator, owner_id=authorization.owner_id, caller=caller,
            operation_hash=public_hash, brain_commit=brain_commit)
        from gremlin_town.review import calendar_entries
        from brain_core import work
        effects = []
        for record in prepared:
            # Each exact planned record is a deterministic slot of this work
            # namespace. A successful slot must not hide another slot's failure.
            slot_key = {**key, "operation_id": f"work:{request.operation_id}:{record.id}"}
            saved = self._why_log.calendar_effect(**slot_key)
            if saved is not None:
                effects.append(saved)
                continue
            # The caller supplies the current reader index from the pinned head,
            # never the reconstructed pre-change snapshot used for replay.
            latest = current.get(work.first_id(record))
            if latest is None or latest.id != record.id:
                if calendar_entries.slot_for(record) is not None or record.state in calendar_entries.CLOSED_STATES:
                    effects.append({"status": "skipped", "text":
                        "Calendar left as it is: a later or archived version owns this effect."})
                continue
            calendar = self._confirmation(record, changed=bool(record.supersedes))[-1]
            if calendar is not None:
                if calendar["status"] in ("added", "moved", "on_calendar", "ask_remove", "kept"):
                    calendar = self._why_log.record_calendar_effect(**slot_key, calendar=calendar)
                effects.append(calendar)
        if not effects:
            return None
        return effects[0]  # preserve the door's existing first-calendar-block reply

    def _answer_context(self, request, index, now, *, replay=False, raw=None):
        from brain_core import work
        contexts = []
        today = today_in_singapore(now)
        for reference in request.answers:
            line = self._why_log.answer_context(reference) if self._why_log is not None else None
            fix = "answer a current open review line for this thing, or quote the full correction as a new change"
            if line is None or (not replay and not line.open) or line.day not in (today, today - timedelta(days=1)):
                raise BrainRuntimeError("brain.invalid_argument", f"Review line {reference!r} is not current and open",
                    field="answers", fix=fix)
            matching = []
            for move in request.moves:
                named = move.get("thing")
                for fid, record in index.items():
                    if named in (fid, record.id, *record.supersedes) or (isinstance(named, str) and
                        _normalize_owner_words(named) == _normalize_owner_words(record.title)):
                        if fid == line.thing:
                            matching.append(move)
            if not matching:
                raise BrainRuntimeError("brain.invalid_argument", f"Review line {reference!r} names another thing",
                    field="answers", fix=fix)
            if line.tidy_suggestion:
                current_record = index.get(line.thing)
                pinned = (raw or {}).get(current_record.id) if current_record is not None else None
                if pinned is None or pinned.sha256 != line.record_sha256:
                    raise BrainRuntimeError("brain.conflict", "This thing changed after its tidy suggestion", field="answers",
                        fix="refresh this item before saving its suggestion")
            offered = set(line.offered)
            def covers(move):
                name = move.get("move")
                if name == "organise":
                    aliases = {"merge_into": {"merge", "merge_into"}, "title": {"title", "rename"},
                        "called": {"called", move.get("called", "")}, "rough_minutes": {"rough_minutes", "length"},
                        "block": {"block"}, "accept": {"accept"}, "keep": {"keep"}, "label": {"label"}}
                    fields = {key for key in ("called", "accept", "keep", "title", "merge_into", "block") if key in move}
                    if move.get("length_is_nics"):
                        fields.add("rough_minutes")
                    if line.tidy_suggestion and "label" in move:
                        fields.add("label")
                    return bool(fields) and all("organise" in offered or offered.intersection(aliases[key]) for key in fields)
                aliases = {"tick": {"tick", "done", "done already"}, "end": {"end", "drop", "dropped", "no_longer_needed"},
                    "date": {"date", "move", "today", "another day", "another_day"}, "parts": {"parts", "add_parts"},
                    "stake": {"stake", "confirm"}}
                return bool(offered.intersection(aliases.get(name, {name})))
            if not any(covers(move) for move in matching):
                raise BrainRuntimeError("brain.invalid_argument", f"Review line {reference!r} did not offer this move",
                    field="answers", fix=fix)
            contexts.append({"reference": line.reference, "question": line.question, "offered": line.offered,
                "thing": line.thing, "day": line.day.isoformat(), "open": line.open or replay, "marks": line.marks,
                "suggested_name": line.suggested_name, "suggested_title": line.suggested_title,
                "suggested_label": line.suggested_label, "tidy_suggestion": line.tidy_suggestion,
                "length_choices": line.length_choices})
        return contexts

    def _work(self, authorization, request, *, caller, bridge_hash=None, record_output=None, capture_bodies=None,
              capture_raise_with: Mapping[int, str] | None = None, initial_revision: GitHubRevision | None = None):
        from brain_core import moves, work
        from .surface_door import owner_tap_matches
        authenticated_tap = (caller in self.code_callers
            and owner_tap_matches(caller, authorization.owner_id, request))
        if request.expected_records is not None and not authenticated_tap:
            raise BrainRuntimeError("ACCESS_DENIED", "Record checks require an authenticated owner tap",
                field="expected_records", fix="refresh and submit the action from the authenticated surface")
        if not _ORIGIN_ID.fullmatch(request.operation_id):
            raise BrainRuntimeError("brain.invalid_argument", "Invalid retry key", field="operation_id",
                fix="use 1 to 128 lowercase letters, digits or hyphens")
        public = {key: value for key, value in asdict(request).items() if key != "check_only"}
        if request.expected_records is None:
            public.pop("expected_records", None)
        public_hash = bridge_hash or hashlib.sha256(canonical_json(public).encode()).hexdigest()
        # The proof participates in exact retries, but is not a Brain Core move
        # argument. Preserve old hashes exactly when the metadata is absent.
        expected_records = public.pop("expected_records", None)
        checking = request.check_only or self._validate_only(caller)
        for attempt in range(_PREFLIGHT_ATTEMPTS):
            # Only a trusted authenticated legacy preflight may seed the first
            # attempt. A publication conflict always recomputes at a fresh head.
            revision, records = self._work_records(authorization,
                revision=initial_revision if attempt == 0 else None)
            current = work.index_things(records)
            live_ids = {record.id for record in records}
            raw = {record.id: record for record in records}
            existing = self._replay_work_records(authorization, request.operation_id, caller, revision)
            if self.schedule_lengths and not existing:
                for move in request.moves:
                    parts = move.get("parts", ())
                    fields = (move, *(parts if isinstance(parts, (list, tuple)) else ()))
                    if any(isinstance(entry, Mapping) and "rough_minutes" in entry
                           and (not entry.get("length_is_nics") or request.said_by != "owner"
                                or request.came_by != "direct"
                                or type(entry["rough_minutes"]) is not int
                                or entry["rough_minutes"] not in _lengths_in(request.message)) for entry in fields):
                        raise BrainRuntimeError("brain.invalid_argument", "The length must come from Nic. Nothing was saved.",
                            field="rough_minutes", fix="leave the length out unless Nic supplied it")
            if any(record.dedupe_key != public_hash for record in existing):
                raise BrainRuntimeError("brain.conflict", "This operation_id already names different moves",
                    field="operation_id", fix="use a new operation_id for a different change")
            if existing:
                now = existing[0].created_at
                try:
                    history = self._storage.revision_before_batch(records=existing, revision=revision)
                except Exception as exc:
                    raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc
                input_revision, records = self._work_records(authorization, revision=history.before)
                raw = {r.id: r for r in records}
                raw.update((r.id, r) for r in existing)
            else:
                # Check-only never invokes the accepted-time writer.
                now = self._clock() if checking else self._records._accepted_at(
                    "work-" + hashlib.sha256(request.operation_id.encode()).hexdigest(), caller)
                input_revision = revision
            index = work.index_things(records)
            own_index = work.write_index(records)
            self._check_work_expected_records(expected_records, own_index, raw)
            planned = {(i, slot): new_record_id(now, self._work_suffix(request.operation_id, i, slot, caller))
                for i in range(len(request.moves)) for slot in range(26)}
            undo = {}
            for move in request.moves:
                if move.get("move") != "undo":
                    continue
                operation = move.get("change")
                if not isinstance(operation, str) or not _ORIGIN_ID.fullmatch(operation):
                    raise BrainRuntimeError("brain.invalid_argument", "Undo needs the original change's operation_id",
                        field="change", fix="name a door change made by this caller")
                previous = self._exact_work_records(records, operation, caller, input_revision)
                if not previous:
                    raise BrainRuntimeError("brain.not_found", "No door change from this caller was found",
                        field="change", fix="name a door change made by this caller")
                undo[operation] = tuple(record.id for record in previous)
                for record in previous:
                    raw[record.id] = record
                    for old in record.supersedes:
                        match = find_by_id(self._storage, old, revision=input_revision).match
                        if match is not None:
                            raw[old] = match.record
            contexts = self._answer_context(request, index, now, replay=bool(existing), raw=raw)
            envelope = moves.RecordEnvelope(vaults=(authorization.owner_locator,),
                owners=(authorization.owner_id,), evidence_actor=caller)
            change = {**public, "_context": moves.ExecutionContext(raw_records=raw, write_index=own_index,
                allowed_code_callers=self.code_callers,
                planned_ids=planned, answer_context=contexts, undo_changes=undo, operation_hash=public_hash,
                record_envelope=envelope, capture_bodies=capture_bodies or {},
                capture_raise_with=capture_raise_with if capture_raise_with is not None else {},
                authenticated_owner_tap=authenticated_tap)}
            outcome = moves.apply_change(index, change, actor=caller, now=now)
            if outcome.refusal:
                refusal = outcome.refusal
                raise BrainRuntimeError(refusal["code"], refusal["message"] + ". Nothing was saved.",
                    field=str(refusal.get("field", refusal.get("move", "moves"))), fix=refusal["fix"])
            # Core validates and applies the authenticated envelope before its replay
            # comparison. Publish those exact records, preserving existing authors.
            prepared = list(outcome.records)
            schedule_index = work.index_things([*index.values(), *prepared])
            if record_output is not None:
                record_output[:] = prepared
            if len(prepared) > 25:
                raise BrainRuntimeError("brain.capacity.exceeded", "The change exceeds 25 records; nothing was saved",
                    field="moves", fix="split it")
            if existing:
                if {r.id: render_record(r) for r in existing} != {r.id: render_record(r) for r in prepared}:
                    raise BrainRuntimeError("brain.record.exists", "Only part of this change is saved",
                        field="operation_id", fix="send the correction as a new change; nothing was saved")
                if not checking:
                    for record in prepared:
                        if record.id in live_ids:
                            self._records.remember_record(authorization, record.id, _record_path(record))
                reply = self._work_reply(outcome.reply, "validated" if checking else "duplicate", None if checking else history.accepted.commit_sha,
                                         records=prepared, index=schedule_index)
                if not checking:
                    calendar = self._work_calendar(authorization, request, caller, public_hash,
                        history.accepted.commit_sha, prepared, current)
                    if calendar is not None:
                        reply["calendar"] = calendar
                return self._cap_work_reply(reply)
            if checking:
                return self._work_reply(outcome.reply, "validated", None)
            if not prepared:
                return self._work_reply(outcome.reply, "accepted", revision.commit_sha)
            try:
                result = self._storage.publish_records(records=[render_record(r).encode() for r in prepared],
                    base_revision=revision)
            except GitHubConflict as exc:
                if exc.code == "github_revision_conflict":
                    continue
                raise self._map_storage_error(exc) from exc
            except Exception as exc:
                raise self._map_storage_error(exc) from exc
            reply = self._work_reply(outcome.reply, "accepted", result.commit_sha,
                                     records=prepared, index=schedule_index)
            for record in prepared:
                self._records.remember_record(authorization, record.id, _record_path(record))
            current = work.index_things([*records, *prepared])
            if self._why_log is not None:
                dropped = {flag["thing"] for flag in outcome.flags if flag["dropped"]}
                partial = {answer["thing"] for answer in contexts
                           if answer.get("tidy_suggestion") or answer.get("length_choices") or (answer.get("suggested_name") and any(move.get("move") == "organise" and "title" in move and set(move) <= {"move", "thing", "title", "words"} for move in request.moves))}
                pending_rules = {}
                for record in prepared:
                    fid = work.first_id(record)
                    if fid in partial and record.state == "open":
                        rules = set()
                        if any(need["kind"] == "needs_you" for need in work.needs_nic(current[fid], current, now)):
                            rules.add("needs_you")
                        if not work.length_of(current[fid], current):
                            rules.add("needs_length")
                        if not work.dates_of(current[fid], current).get("planned"):
                            rules.add("new_schedule")
                        pending_rules[fid] = rules
                self._why_log.mark_today_answered(
                    (work.first_id(r) for r in prepared if work.first_id(r) not in dropped),
                    message=request.message, now=now, pending_rules=pending_rules,
                    validated_references=request.answers if caller == "system:review-card" and owner_tap_matches(caller, authorization.owner_id, request) else ())
            calendar = self._work_calendar(authorization, request, caller, public_hash, result.commit_sha, prepared, current)
            if calendar is not None:
                reply["calendar"] = calendar
            return self._cap_work_reply(reply)
        raise BrainRuntimeError("brain.record.write_failed", "Branch moved four times; nothing was saved",
            retryable=True, field="moves", fix="resend the same operation_id")

    @staticmethod
    def _check_work_expected_records(expected, index, raw):
        """Check a surface's displayed versions at the exact publication base.

        On a retry, index/raw have already been reconstructed from the original
        pre-change revision. Never compare an exact retry with its own new head.
        """
        if expected is None:
            return
        if not isinstance(expected, Mapping) or any(
                not isinstance(ref, str) or not ref
                or not isinstance(proof, Mapping) or set(proof) != {"record_id", "sha256"}
                or not isinstance(proof["record_id"], str) or not proof["record_id"]
                or not isinstance(proof["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", proof["sha256"]) is None
                for ref, proof in expected.items()):
            raise BrainRuntimeError("brain.invalid_argument", "Record checks need exact version IDs and hashes",
                field="expected_records", fix="refresh the displayed records before submitting this action")
        for ref, proof in expected.items():
            current = index.get(ref)
            record = raw.get(current.id) if current is not None else None
            if record is None or record.id != proof["record_id"] or record.sha256 != proof["sha256"]:
                raise BrainRuntimeError("brain.record.changed", "This thing changed since it was displayed. Nothing was saved.",
                    field="expected_records", fix="refresh this thing and try the action again")

    @staticmethod
    def _cap_work_reply(reply):
        while len(json.dumps(reply)) > 2000 and reply.get("things"):
            reply["things"].pop()
            reply["more"] = reply.get("more", 0) + 1
        if len(json.dumps(reply)) > 2000:
            reply["say"] = str(reply.get("say", ""))[:400]
        return reply

    def _work_reply(self, source, status, version, *, records=(), index=None):
        reply = json.loads(json.dumps(dict(source)))
        reply.update(status=status, version=version, remote_observed=status != "validated")
        if status == "validated":
            reply["say"] = "Checked only; nothing was saved. " + str(reply.get("say", ""))
        elif status in {"accepted", "duplicate"} and self.schedule_wake is not None:
            try:
                self.schedule_wake()
            except Exception:
                logger.warning("Saved work could not wake the schedule service")
                reply["schedule"] = {"status": "wake_failed", "text": "Saved, but the schedule could not be updated."}
            else:
                reply["schedule"] = {"status": "pending", "text": "Saved. The schedule is updating."}
            split = {work.first_id(record) for record in records
                     if split_notice_required(record, index)}
            if split:
                for thing in reply.get("things", ()):
                    if thing["id"] in split:
                        thing.update(schedule_issue="needs_split", schedule_label=SPLIT_TASK_NOTICE)
                schedule = reply["schedule"]
                schedule.update(issue="needs_split", count=len(split))
                if schedule["status"] == "pending":
                    schedule.update(status="needs_split", text="Saved. " + SPLIT_TASK_NOTICE)
                else:
                    schedule["text"] += " " + SPLIT_TASK_NOTICE
        return self._cap_work_reply(reply)

    def _save_serialized(
        self, authorization: OwnerAuthorization, request: MemorySaveRequest
    ) -> WriteReceipt:
        with self._memory_operation_lock(request.operation_id):
            token = work_check_only.set(self._validate_only(self._caller()))
            try:
                return self._save(authorization, request)
            finally:
                work_check_only.reset(token)

    def _memory_preflight(
        self, identity: MemoryOperationIdentity, *, revision: GitHubRevision | None = None,
    ) -> GitHubMemoryOperationPreflight:
        """Ask B for one bounded operation binding at one pinned revision."""

        try:
            return self._storage.preflight_memory_operation(
                operation_id=identity.operation_id,
                input_sha256=identity.input_sha256,
                # P3 (finding 15): identity.record_entropy (the suffix new records are
                # written with) is now caller-scoped, so the search must be too, or a
                # caller's own just-written record would never be found again.
                caller=identity.source.get("recorded_by"),
                revision=revision,
            )
        except Exception as exc:
            if isinstance(exc, BrainRuntimeError):
                raise
            raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc


    def _save_item_bridge(self, authorization, request, *, initial_revision: GitHubRevision | None = None):
        evidence = self._records._memory_evidence(authorization, request.source_reference, request.words)
        caller = self._records._recorded_by(evidence)
        self._records._source_mapping(request.said_by, request.came_by, request.words, request.speaker, caller)
        move = {"move": "new", "title": request.title}
        for old, new in (("level", "level"), ("owed_to", "owed_to"), ("repeat", "repeat"),
                         ("block", "block"), ("rough_minutes", "rough_minutes")):
            value = getattr(request, old)
            if value is not None:
                move[new] = value
        when = _capture_time_words(request)
        if when is not None:
            move["when"] = when
        if request.length_is_nics:
            move["length_is_nics"] = True
        ignored = tuple(name for name in ("size", "importance", "importance_reason", "sort", "first_step", "links")
            if getattr(request, name) is not None and (name != "links" or request.links))
        bridge_id = "bridge-" + hashlib.sha256(request.operation_id.encode()).hexdigest()
        door_request = WorkRequest(bridge_id, request.words or request.body,
            "owner" if request.said_by == "owner" else "other",
            "direct" if request.came_by == "direct" else "connected_source", (move,),
            speaker=request.speaker or (caller if request.said_by != "owner" else None),
            check_only=self._validate_only(caller))
        bridge_public = asdict(request)
        for name in ("size", "importance", "importance_reason", "sort", "first_step", "links"):
            bridge_public.pop(name)
        bridge_hash = hashlib.sha256(canonical_json(bridge_public).encode()).hexdigest()
        output = []
        reply = self._work(authorization, door_request, caller=caller, bridge_hash=bridge_hash, record_output=output,
            capture_bodies={0: request.body.rstrip("\n")},
            capture_raise_with={0: request.raise_with} if request.raise_with is not None else None,
            initial_revision=initial_revision)
        record = output[0]
        # Presentation caps can omit even the only row. Publication evidence and
        # receipt identity always come from the prepared record, not that display.
        thing = {"id": work.first_id(record), "record_id": record.id, "title": record.title}
        thing.update(next((entry for entry in reply.get("things", ()) if entry.get("record_id") == record.id), {}))
        digest = hashlib.sha256(render_record(record).encode()).hexdigest() if reply["status"] != "validated" else None
        return _receipt(reply.get("calendar"),
            _length_note(record, asked=request.rough_minutes is not None, saving=True, schedule_mode=self.schedule_lengths),
            operation_id=request.operation_id, status=reply["status"],
            owner_locator=authorization.owner_locator, record_id=record.id, version=reply["version"],
            content_sha256=digest, receipt_sha256=None, remote_observed=reply["remote_observed"],
            state=work.status_of(record, work.index_things(output), record.updated_at), level=record.data.get("level"), word=language.plain_word(record.data),
            timing=record.data.get("timing"), record={**thing, "dates": deepcopy(record.data.get("dates", {})), "content_sha256": digest,
                "version": reply["version"], "state": record.state,
                **({"schedule": reply["schedule"]} if "schedule" in reply else {})},
            ignored=ignored, fix="refresh the connector" if ignored else None)

    def _legacy_memory_identity(self, authorization, request):
        """Normalize pre-cut input only for an exact read-only retry comparison.

        Removed arguments never validate a new capture. They still participate
        in the original legacy hash and must describe a known valid old request
        before an existing record can be returned as a duplicate.
        """
        clean = replace(request, level=request.level or "possible", size=None, importance=None,
            importance_reason=None, sort=None, first_step=None, links=())
        base = self._records.memory_identity(authorization, clean)
        data = {**base.data, "level": request.level, "size": request.size or "task"}
        if request.first_step is not None:
            data["first_step"] = request.first_step
        if request.sort is not None:
            data["sort_input"] = request.sort
        importance = request.importance
        invalid = None
        if importance not in (None, "normal", "proposed", "confirmed", "real_cost", "minor"):
            invalid = "importance is not a known pre-cut setting"
        if importance in (None, "normal"):
            if request.importance_reason is not None:
                invalid = "importance_reason is not valid without a pre-cut importance setting"
        else:
            instruction = {"set": importance, "proposed_by": base.source["recorded_by"]}
            if request.importance_reason is not None:
                instruction["reason"] = request.importance_reason
            else:
                invalid = "the pre-cut importance setting needs its original reason"
            data["importance_input"] = instruction
        if request.level not in ("possible", "accepted") or request.size not in (None, "task", "goal"):
            invalid = "the request does not describe a known pre-cut item level and size"
        state = request.initial_state or ("waiting" if request.sort == "waiting_on" else "open")
        links = tuple(dict(link) if isinstance(link, Mapping) else link for link in request.links)
        payload = {"schema": "gt.brain-memory-input/v2", "operation_id": request.operation_id,
            "kind": request.kind, "title": request.title, "body": request.body, "state": state,
            "owner_id": authorization.owner_id, "owner_locator": authorization.owner_locator,
            "evidence": base.evidence.as_dict(), "requested_dedupe_key": request.dedupe_key,
            "data": data, "source": base.source, "links": list(links)}
        return replace(base, input_sha256=hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
            state=state, data=data, links=links), invalid

    def _legacy_save_replay(self, authorization, request) -> tuple[WriteReceipt | None, GitHubRevision]:
        """Retry an exact pre-cut save through its original caller-scoped binding."""
        identity, invalid = self._legacy_memory_identity(authorization, request)
        preflight = self._memory_preflight(identity)
        if preflight.match is None:
            return None, preflight.revision
        match = preflight.match
        if invalid:
            raise BrainRuntimeError("brain.invalid_argument", invalid, field="operation_id",
                fix="supply the complete valid original legacy request, or capture it with a new operation_id")
        raw_data = dict(identity.data)
        instruction = raw_data.pop("importance_input", None)
        expected_data = self._records._resolved_memory_data("item", raw_data, match.record.created_at)
        if instruction is not None:
            expected_data["importance"] = {"level": {"proposed": "high", "confirmed": "high",
                "real_cost": "real_cost", "minor": "minor"}[instruction["set"]],
                "reason": instruction["reason"], "proposed_by": instruction["proposed_by"],
                "confirmed": instruction["set"] == "confirmed"}
        if request.sort is not None:
            from brain_core.sorting import sorting_gaps
            gaps = sorting_gaps(request.sort, kind="item", state=identity.state,
                data=expected_data, links=identity.links)
            if gaps:
                raise BrainRuntimeError("brain.invalid_argument", "Invalid pre-cut sort: " + "; ".join(gaps),
                    field="sort", fix="supply the complete valid original legacy request")
        self._records.assert_exact_memory_replay(authorization,
            replace(identity, dedupe_key=preflight.dedupe_key, data=expected_data,
                body=identity.body.rstrip("\n")), match.record)
        checking = self._validate_only(self._caller())
        if not checking and match.path == _record_path(match.record):
            self._records.remember_record(authorization, match.record.id, match.path)
        # This is an already accepted legacy record, not a new calendar write.
        word, timing, links = _memory_confirmation(match.record, review_enabled=self._review_enabled)
        return _receipt(None, operation_id=request.operation_id,
            status="validated" if checking else "duplicate", owner_locator=authorization.owner_locator,
            record_id=match.record.id, version=None if checking else match.revision.commit_sha,
            content_sha256=None if checking else hashlib.sha256(match.content).hexdigest(),
            receipt_sha256=None, remote_observed=not checking, word=word, timing=timing, links=links,
            state=match.record.state, level=match.record.data.get("level")), preflight.revision

    def _save(
        self, authorization: OwnerAuthorization, request: MemorySaveRequest
    ) -> WriteReceipt:
        if request.kind == "item":
            replay, revision = self._legacy_save_replay(authorization, request)
            if replay is not None:
                return replay
            return self._save_item_bridge(authorization, request, initial_revision=revision)
        identity = self._records.memory_identity(authorization, request)
        material: MemoryMaterial | None = None
        for _ in range(_PREFLIGHT_ATTEMPTS):
            preflight = self._memory_preflight(identity)
            bound_identity = replace(identity, dedupe_key=preflight.dedupe_key)
            if preflight.match is not None:
                match = preflight.match
                self._records.assert_exact_memory_replay(
                    authorization, bound_identity, match.record
                )
                if match.path == _record_path(match.record):  # an archived record is not re-read
                    self._records.remember_record(
                        authorization, match.record.id, match.path
                    )
                word, timing, links, calendar = self._confirmation(
                    match.record, changed=False
                )
                return _receipt(
                    calendar,
                    _length_note(match.record, asked=request.rough_minutes is not None, saving=True, schedule_mode=self.schedule_lengths),
                    operation_id=request.operation_id,
                    status="duplicate",
                    owner_locator=authorization.owner_locator,
                    record_id=match.record.id,
                    version=match.revision.commit_sha,
                    content_sha256=hashlib.sha256(match.content).hexdigest(),
                    receipt_sha256=None,
                    remote_observed=True,
                    word=word,
                    timing=timing,
                    links=links,
                    state=match.record.state,
                    level=match.record.data.get("level"),
                    importance=_importance(match.record),
                )
            if request.kind == "preference" and not (
                request.said_by == "owner" and request.came_by == "direct" and request.words
            ):
                raise BrainRuntimeError(
                    "brain.record.needs_owner_words",
                    "A preference is accepted only from Nic's own words (said_by owner, "
                    "came_by direct, with words); save it as a suggested item instead",
                )
            if material is None:
                material = self._records.materialize_memory(
                    authorization, request, bound_identity
                )
            if self._validate_only(self._caller()):
                return _receipt(None, operation_id=request.operation_id, status="validated",
                    owner_locator=authorization.owner_locator, record_id=material.record.id, version=None,
                    content_sha256=None, receipt_sha256=None, remote_observed=False)
            try:
                result = self._storage.publish_record(
                    record_bytes=material.record_bytes,
                    base_revision=preflight.revision,
                )
            except GitHubConflict as exc:
                if exc.code == "github_revision_conflict":
                    continue
                raise self._map_storage_error(exc) from exc
            except Exception as exc:
                if isinstance(exc, BrainRuntimeError):
                    raise
                raise self._map_storage_error(exc) from exc
            self._records.remember_record(
                authorization, material.record.id, result.record_path
            )
            word, timing, links, calendar = self._confirmation(
                material.record, changed=False
            )
            return _receipt(
                calendar,
                _length_note(material.record, asked=request.rough_minutes is not None, saving=True, schedule_mode=self.schedule_lengths),
                operation_id=request.operation_id,
                status="duplicate" if result.status == "duplicate/no-op" else "accepted",
                owner_locator=authorization.owner_locator,
                record_id=result.record_id,
                version=result.commit_sha,
                content_sha256=result.record_sha256,
                receipt_sha256=None,
                remote_observed=result.remote_accepted,
                word=word,
                timing=timing,
                links=links,
                state=material.record.state,
                level=material.record.data.get("level"),
                importance=_importance(material.record),
            )
        raise BrainRuntimeError(
            "brain.record.write_failed",
            "GitHub branch changed repeatedly during memory publication",
            retryable=True,
        )


    async def change(
        self, authorization: OwnerAuthorization, request: MemoryChangeRequest
    ) -> WriteReceipt:
        self._authorize(authorization, BrainOperation.MEMORY_CHANGE, "write")
        return await anyio.to_thread.run_sync(self._change_serialized, authorization, request)

    def _change_serialized(
        self, authorization: OwnerAuthorization, request: MemoryChangeRequest
    ) -> WriteReceipt:
        with self._memory_operation_lock(request.operation_id):
            token = work_check_only.set(self._validate_only(self._caller()))
            try:
                return self._change(authorization, request)
            finally:
                work_check_only.reset(token)

    def _current_memory_record(
        self, authorization: OwnerAuthorization, record_id: str, revision: GitHubRevision
    ) -> tuple[Record, bytes]:
        """The record a change names, found and read at the change's own pinned revision.

        It is looked up by ID in that revision's tree, so no further GitHub call is
        needed once the tree and record are in the copy.

        An old version moved to Brain Home's `archive/` folder (the storage-limit plan's layer 2)
        is found there too, so a change naming it is refused as changed, as before the move, and a
        replay still finds the version it replaced. One that no record names, a thing archived
        whole, is not found.
        """

        try:
            found = find_by_id(self._storage, record_id, revision=revision)
        except Exception as exc:
            if isinstance(exc, BrainRuntimeError):
                raise
            raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc
        if found.match is None or self._archived_and_unnamed(
            found.match.record, _archived(found.match.path), revision, nothing_saved=True,
        ):
            raise BrainRuntimeError("brain.not_found", "Memory record was not found")
        item = found.match
        if authorization.owner_locator not in item.record.vaults or authorization.owner_id not in item.record.owners:
            raise BrainRuntimeError("brain.policy.excluded", "Record is outside the authorized owner")
        return item.record, item.content

    def _archived_and_unnamed(
        self, record: Record, archived: bool, revision: GitHubRevision, *, nothing_saved: bool = False,
    ) -> bool:
        """True for a record read from `archive/` that no record of its kind names in `supersedes`: a
        thing archived whole, answered as not found. A kind the hub never scans can't be checked, so it
        counts as unnamed, and the journal says so."""

        if not archived:
            return False
        if record.kind not in SCAN_KINDS:
            logger.warning(
                "Brain Home's archive holds %s record %s, which the hub can't check; answering not found",
                record.kind, record.id,
            )
            return True
        return not self._successor_ids(record.id, record.kind, revision, nothing_saved=nothing_saved)

    def _successor_ids(
        self, record_id: str, kind: str, revision: GitHubRevision | None = None, *, nothing_saved: bool = False,
        records: Sequence[Record] | None = None,
    ) -> tuple[str, ...]:
        """IDs of records that already name ``record_id`` in their own ``supersedes``.

        Records are immutable, so ``record_id``'s own content hash never changes even
        after some other change has replaced it (R8). A stale ``expected_sha256`` read
        guard alone cannot see that; the only way to tell is to check whether some
        other current-kind record has already claimed to supersede it. ``revision``
        pins the scan to the operation's revision; the scan limits are the copy's,
        as for search. Named archived versions complete the same historical successor
        list that a live-only history returned, in canonical path order. Only a current
        same-kind record's whole history admits those archive reads.
        """

        try:
            if records is None:
                snapshot = self._storage.snapshot_records(
                    kinds=frozenset({kind}),
                    max_records=SCAN_MAX_RECORDS,
                    max_bytes=SCAN_MAX_BYTES,
                    revision=revision,
                )
                revision = snapshot.revision
                records = tuple(item.record for item in snapshot.records)
            elif revision is None:
                raise ValueError("Successor records require their pinned revision")
            live = [record for record in records if record.kind == kind]
            replaced = {old for record in live for old in record.supersedes}
            named = {
                old for record in live if record.id not in replaced and record_id in record.supersedes
                for old in record.supersedes
            }
            known = {record.id for record in live}
            for old in sorted(named - known):
                match = find_by_id(self._storage, old, revision=revision).match
                if match is not None and _archived(match.path) and match.record.kind == kind:
                    live.append(match.record)
            index = _successor_index(sorted(live, key=_record_path))
            return tuple(item.id for item in index.get(record_id, ()))
        except Exception as exc:
            if isinstance(exc, BrainRuntimeError):
                raise
            raise self._map_storage_error(exc, reading=True, nothing_saved=nothing_saved) from exc

    def _change(
        self, authorization: OwnerAuthorization, request: MemoryChangeRequest
    ) -> WriteReceipt:
        revision = self._storage.revision()
        target, _ = self._current_memory_record(authorization, request.record_id, revision)
        if target.kind == "item":
            raise BrainRuntimeError("ACCESS_DENIED", "Item changes go through the door", field="record_id",
                fix="use bc_work" if self._caller() == self.door_participant else
                "save it as a new thing with memory.save; Nic's morning review merges it")
        if request.importance is not None or request.importance_reason is not None:
            raise BrainRuntimeError("brain.invalid_argument", "importance is for items only",
                field="importance", fix="change only the advertised fact or preference fields")
        identity = self._records.memory_change_identity(authorization, request)
        for attempt in range(_PREFLIGHT_ATTEMPTS):
            # The target-kind admission already read this trusted revision. Keep
            # the first reconciliation on it; a failed publication must reconcile
            # against a fresh head, including its successor and replay checks.
            preflight = self._memory_preflight(identity, revision=revision if attempt == 0 else None)
            previous, previous_bytes = self._current_memory_record(
                authorization, request.record_id, preflight.revision)
            # Repeat admission on each attempt's exact tree before a replay
            # receipt, calendar reconciliation, or publication can take effect.
            if previous.kind == "item":
                raise BrainRuntimeError("ACCESS_DENIED", "Item changes go through the door", field="record_id",
                    fix="use bc_work" if self._caller() == self.door_participant else
                    "save it as a new thing with memory.save; Nic's morning review merges it")
            bound_identity = replace(identity, dedupe_key=preflight.dedupe_key)
            if preflight.match is not None:
                match = preflight.match
                self._records.assert_exact_memory_change_replay(authorization, bound_identity, match.record, previous)
                if match.path == _record_path(match.record):  # an archived record is not re-read
                    self._records.remember_record(authorization, match.record.id, match.path)
                word, timing, links, calendar = self._confirmation(
                    match.record, changed=True
                )
                change_content_sha256 = hashlib.sha256(match.content).hexdigest()
                return _receipt(
                    calendar,
                    _length_note(match.record, asked=request.rough_minutes is not None, saving=False, schedule_mode=self.schedule_lengths),
                    operation_id=request.operation_id,
                    status="duplicate",
                    owner_locator=authorization.owner_locator,
                    record_id=match.record.id,
                    version=match.revision.commit_sha,
                    content_sha256=change_content_sha256,
                    receipt_sha256=None,
                    remote_observed=True,
                    word=word,
                    timing=timing,
                    links=links,
                    record={
                        "id": match.record.id,
                        "state": match.record.state,
                        "level": match.record.data.get("level"),
                        "timing": timing,
                        "word": word,
                        "content_sha256": change_content_sha256,
                        "version": match.revision.commit_sha,
                    },
                    importance=_importance(match.record),
                )
            actual_sha256 = hashlib.sha256(previous_bytes).hexdigest()
            # A hash match alone is not enough (R8): the named record's own bytes never
            # change, so a second change built from the same read must also be refused
            # once some other change has already superseded it and become the head.
            # Both checks run at the revision the change is written on; the branch
            # update is fast-forward only, so nothing can land in between.
            if actual_sha256 != request.expected_sha256 or self._successor_ids(
                request.record_id, previous.kind, preflight.revision, nothing_saved=True,
            ):
                raise BrainRuntimeError("brain.record.changed", "The record changed since it was read")
            material = self._records.materialize_memory_change(authorization, request, previous, bound_identity)
            if self._validate_only(self._caller()):
                return _receipt(None, operation_id=request.operation_id, status="validated",
                    owner_locator=authorization.owner_locator, record_id=material.record.id, version=None,
                    content_sha256=None, receipt_sha256=None, remote_observed=False)
            try:
                result = self._storage.publish_record(
                    record_bytes=material.record_bytes,
                    base_revision=preflight.revision,
                )
            except GitHubConflict as exc:
                if exc.code == "github_revision_conflict":
                    continue
                raise self._map_storage_error(exc) from exc
            except Exception as exc:
                if isinstance(exc, BrainRuntimeError):
                    raise
                raise self._map_storage_error(exc) from exc
            self._records.remember_record(
                authorization, material.record.id, result.record_path
            )
            word, timing, links, calendar = self._confirmation(
                material.record, changed=True
            )
            return _receipt(
                calendar,
                _length_note(material.record, asked=request.rough_minutes is not None, saving=False, schedule_mode=self.schedule_lengths),
                operation_id=request.operation_id,
                status="duplicate" if result.status == "duplicate/no-op" else "accepted",
                owner_locator=authorization.owner_locator,
                record_id=result.record_id,
                version=result.commit_sha,
                content_sha256=result.record_sha256,
                receipt_sha256=None,
                remote_observed=result.remote_accepted,
                word=word,
                timing=timing,
                links=links,
                record={
                    "id": material.record.id,
                    "state": material.record.state,
                    "level": material.record.data.get("level"),
                    "timing": timing,
                    "word": word,
                    "content_sha256": result.record_sha256,
                    "version": result.commit_sha,
                },
                importance=_importance(material.record),
            )
        raise BrainRuntimeError(
            "brain.record.write_failed",
            "GitHub branch changed repeatedly during memory change publication",
            retryable=True,
        )

    async def search(self, authorization: OwnerAuthorization, query: MemorySearchQuery) -> MemorySearchResult:
        self._authorize(authorization, BrainOperation.MEMORY_SEARCH, "read")
        return await anyio.to_thread.run_sync(self._search, authorization, query)

    def _search(self, authorization: OwnerAuthorization, query: MemorySearchQuery) -> MemorySearchResult:
        terms = set(re.findall(r"\w+", query.query.casefold()))
        if not terms or not 1 <= query.limit <= 20:
            raise BrainRuntimeError("brain.invalid_argument", "A non-empty search query and limit 1..20 are required")
        try:
            snapshot = self._storage.snapshot_records(
                kinds=frozenset({"capture", "commitment", "item", "fact"}),
                max_records=SCAN_MAX_RECORDS,
                max_bytes=SCAN_MAX_BYTES,
            )
        except Exception as exc:
            raise self._map_storage_error(exc, reading=True) from exc
        allowed = [item for item in snapshot.records if authorization.owner_locator in item.record.vaults and authorization.owner_id in item.record.owners]
        superseded = {rid for item in allowed for rid in item.record.supersedes}
        reader_index = work.index_things(item.record for item in allowed if item.record.kind == "item")
        active_items = {item.record.id: item.record for item in allowed if item.record.kind == "item"}
        first_words = {}
        # The current chain explicitly names its first version even after archiving.
        # Resolve only that ID at the same revision as the current reader snapshot.
        for first, current in reader_index.items():
            original = active_items.get(first)
            if original is None and first in current.supersedes:
                try:
                    match = find_by_id(self._storage, first, revision=snapshot.revision).match
                except Exception as exc:
                    raise self._map_storage_error(exc, reading=True) from exc
                original = match.record if match is not None else None
            if (original is None or original.id != first or original.kind != current.kind
                    or work.first_id(original) != first
                    or authorization.owner_locator not in original.vaults
                    or authorization.owner_id not in original.owners):
                raise BrainRuntimeError(
                    "brain.contract.invalid", "Search cannot verify the item's first version",
                    field="supersedes",
                    fix="restore the exact authorized first item version before searching",
                )
            first_words[first] = str((original.source or {}).get("words", ""))
        moment = self._clock()
        ranked = []
        for item in allowed:
            record = item.record
            if record.id in superseded:
                continue
            if record.kind == "item" and reader_index[work.first_id(record)].id != record.id:
                continue
            # Ideas are recalled only when asked for, never through a broad label/parent hit.
            if record.kind == "item" and record.data.get("level") == "possible" and not terms.intersection({"idea", "ideas"}):
                continue
            # Proposed - Nic approves before release speed-02 goes live
            # (docs/product/2026-09-26-gtbc-speed-goal.md Wave 3; aliases
            # borrowed from the Instinct teardown). No save tool sets
            # data.aliases yet, so this only fires for a record some other
            # writer already gave them; an alias hit ranks like a title hit.
            aliases = record.data.get("aliases") or ()
            title_text = " ".join((record.title, *aliases)).casefold()
            body_text = record.body.casefold()
            reader_text = _reader_search_text(record, reader_index, first_words) if record.kind == "item" else ""
            title_score = sum(term in title_text for term in terms)
            score = sum(term in title_text or term in body_text or term in reader_text for term in terms)
            if score:
                ranked.append((title_score, score, record.updated_at, record.id, item))
        # A title hit outranks a body-only hit; ties keep the prior order -
        # newest updated_at, then record_id, both descending (search ranking,
        # docs/product/2026-09-26-gtbc-speed-goal.md Wave 3).
        ranked.sort(key=lambda entry: entry[:4], reverse=True)
        index = _successor_index(item.record for item in allowed)
        matches = tuple(
            self._search_match(authorization, snapshot.revision.commit_sha, item, index,
                               now=_reader_now(item.record, reader_index, moment) if item.record.kind == "item" else None)
            for _, _, _, _, item in ranked[:query.limit]
        )
        return MemorySearchResult(matches, snapshot.revision.commit_sha, "current", len(ranked) > query.limit or any(len(x[4].record.body)>2000 for x in ranked[:query.limit]))

    @staticmethod
    def _search_match(
        authorization: OwnerAuthorization, version: str, item, index: dict[str, list[Record]],
        *, now: ReaderNow | None = None,
    ) -> MemoryRecord:
        record = item.record
        view = dict(
            record_id=work.first_id(record) if now is not None else record.id, kind=record.kind, title=record.title,
            body=record.body[:2000], state=record.state, owner_locator=authorization.owner_locator,
            version=version, content_sha256=hashlib.sha256(item.content).hexdigest(),
            updated_at=record.updated_at, freshness="current",
            word=_memory_confirmation(record)[0], confirmed=_memory_confirmed(record),
            importance=_importance(record),
        )
        return _memory_reply(record, view, _links_at_newest(record.links, index) if record.links else (), now=now)

    async def list(self, authorization: OwnerAuthorization, query: MemoryListQuery) -> MemoryListResult:
        self._authorize(authorization, BrainOperation.MEMORY_LIST, "read")
        return await anyio.to_thread.run_sync(self._list, authorization, query)

    def _list(self, authorization: OwnerAuthorization, query: MemoryListQuery) -> MemoryListResult:
        if not query.state or not 1 <= query.limit <= 50:
            raise BrainRuntimeError("brain.invalid_argument", "state must be non-empty and limit 1..50 is required")
        if query.kind == "preference":
            return self._list_preferences(authorization, query)
        if query.kind != "item":
            raise BrainRuntimeError("brain.invalid_argument", "kind must be item or preference")
        # level=None means the caller didn't pass one: infer it from word instead of
        # defaulting to "accepted" outright, since plain_word() only ever returns
        # "idea"/"suggestion" for level possible/suggested - word="idea" with an
        # outright "accepted" default would then always list nothing. An explicit
        # level (accepted included) is used exactly as given, no inference; several
        # levels list them all in one call (speed-03).
        if query.level is None:
            levels = frozenset({{"idea": "possible", "suggestion": "suggested"}.get(query.word, "accepted")})
        elif isinstance(query.level, str):
            levels = frozenset({query.level})
        else:
            levels = frozenset(query.level)
        # While [review] is on, the same scan also reads facts, for Nic's answers to the
        # review's clarifying questions (step E spec §3.1); off, it reads items exactly as before.
        try:
            snapshot = self._storage.snapshot_records(
                kinds=frozenset({"item", "fact"}) if self._review_enabled else frozenset({"item"}),
                max_records=SCAN_MAX_RECORDS,
                max_bytes=SCAN_MAX_BYTES,
            )
        except Exception as exc:
            raise self._map_storage_error(exc, reading=True) from exc
        allowed = [item for item in snapshot.records if authorization.owner_locator in item.record.vaults and authorization.owner_id in item.record.owners]
        answers = (
            answers_by_task(
                (ItemRecord(item.record, item.path) for item in allowed if item.record.kind == "item"),
                (ItemRecord(item.record, item.path) for item in allowed if item.record.kind == "fact"),
            )
            if self._review_enabled
            else {}
        )
        link_index = _successor_index(item.record for item in allowed)
        allowed = [item for item in allowed if item.record.kind == "item"]
        reader_index = work.index_things(item.record for item in allowed)
        moment = self._clock()
        superseded = {rid for item in allowed for rid in item.record.supersedes}
        states = frozenset(query.state)
        candidates = []
        for item in allowed:
            record = item.record
            if record.id in superseded or record.state not in states:
                continue
            if reader_index[work.first_id(record)].id != record.id:
                continue
            if record.data.get("level") not in levels:
                continue
            word = language.plain_word(record.data)
            if query.word is not None and word != query.word:
                continue
            timing = record.data.get("timing")
            timing = dict(timing) if isinstance(timing, dict) else {}
            # A window sorts and filters by its start, as orientation reads it (speed-03).
            due = timing.get("due") or (timing.get("window") or {}).get("start")
            if query.due_before is not None and (due is None or due > query.due_before):
                continue
            candidates.append((item, record, word, due, timing))
        # Two stable sorts: updated_at newest-first, then due date ascending with the
        # undated group last - a single tuple key can't mix ascending due with
        # descending updated_at, but a stable sort preserves the first pass's order
        # for whatever the second pass treats as ties (P4; same due bucket).
        by_recency = sorted(candidates, key=lambda c: c[1].updated_at, reverse=True)
        ordered = sorted(by_recency, key=lambda c: (c[3] is None, c[3] or ""))
        truncated = len(ordered) > query.limit
        items = tuple(
            MemoryListItemWithNow(
                now=_reader_now(record, reader_index, moment),
                answers=tuple({"record_id": answer.record.id, "words": answer.record.source["words"]}
                              for answer in answers.get(record.id, ())),
                record_id=work.first_id(record),
                word=word,
                title=record.title,
                state=record.state,
                level=record.data.get("level"),
                timing=timing,
                owed_to=record.data.get("owed_to"),
                links=_links_at_newest(record.links, link_index),
                updated_at=record.updated_at,
                confirmed=_memory_confirmed(record),
                expected_sha256=hashlib.sha256(item.content).hexdigest(),
                importance=_importance(record),
                block=record.data.get("block"),
                raise_with=record.data.get("raise_with"),
                first_step=record.data.get("first_step"),
            )
            for item, record, word, due, timing in ordered[: query.limit]
        )
        return MemoryListResult(items, snapshot.revision.commit_sha, "current", truncated)

    def _list_preferences(self, authorization: OwnerAuthorization, query: MemoryListQuery) -> MemoryListResult:
        """Nic's confirmed preferences, newest first (memory follow-ons P1).

        Confirmed means in his own words (``source.said_by`` owner, which Brain Core requires
        of every v2 preference); a v1 preference has no source, so it is never listed. Only
        ``active`` ones: ``retired`` is a preference he no longer holds. ``word``, ``state``,
        ``level`` and ``due_before`` are item filters and are not applied (Proposed - Nic to
        confirm). One snapshot read, like items, so a warm copy costs one GitHub call. Of the
        preferences naming his focus projects, only the newest is listed: it replaces the
        older ones (Ciel redesign N4, step C §13: "the newest record wins").
        """

        try:
            snapshot = self._storage.snapshot_records(
                kinds=frozenset({"preference"}),
                max_records=SCAN_MAX_RECORDS,
                max_bytes=SCAN_MAX_BYTES,
            )
        except Exception as exc:
            raise self._map_storage_error(exc, reading=True) from exc
        allowed = [item for item in snapshot.records if authorization.owner_locator in item.record.vaults and authorization.owner_id in item.record.owners]
        superseded = {rid for item in allowed for rid in item.record.supersedes}
        link_index = _successor_index(item.record for item in allowed)
        focus = language.focus_preference(item.record for item in allowed)
        confirmed = [
            item for item in allowed
            if item.record.id not in superseded
            and item.record.state == "active"
            and (item.record.source or {}).get("said_by") == "owner"
            and ("focus_projects" not in item.record.data or (focus is not None and item.record.id == focus.id))
        ]
        confirmed.sort(key=lambda item: (item.record.updated_at, item.record.id), reverse=True)
        items = tuple(
            MemoryListItem(
                record_id=item.record.id,
                word=None,
                title=item.record.title,
                state=item.record.state,
                level=None,
                timing={},
                owed_to=None,
                links=_links_at_newest(item.record.links, link_index),
                updated_at=item.record.updated_at,
                confirmed=_memory_confirmed(item.record),
                expected_sha256=hashlib.sha256(item.content).hexdigest(),
            )
            for item in confirmed[: query.limit]
        )
        return MemoryListResult(items, snapshot.revision.commit_sha, "current", len(confirmed) > query.limit)

    async def read(self, authorization: OwnerAuthorization, record_id: str) -> MemoryRecord:
        self._authorize(authorization, BrainOperation.MEMORY_READ, "read")
        return await anyio.to_thread.run_sync(self._read, authorization, record_id)

    def _read(self, authorization: OwnerAuthorization, record_id: str) -> MemoryRecord:
        """One record by ID. An old version moved to Brain Home's `archive/` folder (the storage-limit
        plan's layer 2) reads as before the move: `freshness: stale`, with the current version in
        `superseded_by`. One that no record names, a thing archived whole, is not found, as before."""

        try:
            # Inside the try: a storage failure while finding the record maps as a read, like the rest.
            path = self._records.locate_record(authorization, record_id).removeprefix("archive/")
            result = self._storage.read_records((path,), archived_ok=True)
        except Exception as exc:
            if isinstance(exc, BrainRuntimeError):
                raise
            raise self._map_storage_error(exc, reading=True) from exc
        if len(result.records) != 1:
            raise BrainRuntimeError("brain.record.corrupt", "Brain Home returned an invalid record set")
        item = result.records[0]
        record = item.record
        if self._archived_and_unnamed(record, getattr(item, "archived", False), result.revision):
            # Archived and named by no record: a thing archived whole (test data, say), not found, as
            # before it moved.
            raise BrainRuntimeError("brain.not_found", "Memory record was not found")
        if (
            record.id != record_id
            or record.kind not in {"capture", "commitment", "item", "fact", "preference", "decision"}
            or authorization.owner_locator not in record.vaults
            or authorization.owner_id not in record.owners
        ):
            raise BrainRuntimeError("brain.policy.excluded", "Record is outside the authorized owner")
        word, _, _ = _memory_confirmation(record)
        shown_links: tuple[dict[str, object], ...] = ()
        reader_index: work.WorkIndex = {}
        if record.links or record.kind == "item":
            # One snapshot at the revision the record was read from serves both the
            # successors and the links, so the reply is consistent. Link targets may be
            # any kind Nic links to, so the scan covers items and facts as well as this kind.
            try:
                snapshot = self._storage.snapshot_records(
                    kinds=frozenset({record.kind, "item", "fact"}),
                    max_records=SCAN_MAX_RECORDS,
                    max_bytes=SCAN_MAX_BYTES,
                    revision=result.revision,
                )
            except Exception as exc:
                if isinstance(exc, BrainRuntimeError):
                    raise
                raise self._map_storage_error(exc, reading=True) from exc
            everything = [item.record for item in snapshot.records]
            successors = self._successor_ids(record.id, record.kind, result.revision, records=everything)
            reader_index = work.index_things(
                raw for raw in everything if raw.kind == "item"
                and authorization.owner_locator in raw.vaults and authorization.owner_id in raw.owners
            ) if record.kind == "item" else {}
            # Only this owner's records decide what a link is shown as.
            shown_links = _links_at_newest(
                record.links,
                _successor_index(
                    item
                    for item in everything
                    if authorization.owner_locator in item.vaults and authorization.owner_id in item.owners
                ),
            )
        else:
            successors = self._successor_ids(record.id, record.kind, result.revision)
        now = (_reader_now(record, reader_index, self._clock())
               if record.kind == "item" and not successors
               and reader_index[work.first_id(record)].id == record.id else None)
        view = dict(
            record_id=work.first_id(record) if now is not None else record.id,
            kind=record.kind,
            title=record.title,
            body=record.body,
            state=record.state,
            owner_locator=authorization.owner_locator,
            version=result.revision.commit_sha,
            content_sha256=hashlib.sha256(item.content).hexdigest(),
            updated_at=record.updated_at,
            freshness="stale" if successors else "current",
            canonical_record=record.as_dict(),
            word=word,
            confirmed=_memory_confirmed(record),
            superseded_by=successors,
            importance=_importance(record),
        )
        return _memory_reply(record, view, shown_links, now=now)

    async def remove_calendar_entry(
        self, authorization: OwnerAuthorization, request: CalendarRemoveRequest
    ) -> CalendarRemoveResult:
        self._authorize(authorization, BrainOperation.MEMORY_CHANGE, "write")
        if not work_caller_allowed(self._caller(), door_participant=self.door_participant):
            raise BrainRuntimeError("ACCESS_DENIED", "This caller cannot remove Nic's calendar entry",
                field="caller", fix="remove it through Diablo")
        return await anyio.to_thread.run_sync(self._remove_calendar_entry, authorization, request)

    def _remove_calendar_entry(
        self, authorization: OwnerAuthorization, request: CalendarRemoveRequest
    ) -> CalendarRemoveResult:
        """Remove the entry Exosuit added for a done or dropped task, only with Nic's yes (G7).

        Deleting is normally a fresh yes every time (language spec part 7, tier 3), so this is
        its own call, never a side effect of a change, and it refuses anything but Nic's own
        words. It reads the task at the current head, so a task reopened since cannot lose its
        entry, and deletes only the entry found by the task's marker.
        """

        if self._calendar is None:
            raise BrainRuntimeError("brain.capability.unavailable", "Calendar entries are not switched on")
        if request.said_by != "owner" or request.came_by != "direct" or not (request.words or "").strip():
            raise BrainRuntimeError(
                "brain.record.needs_owner_words",
                "Removing a calendar entry needs Nic's own yes: said_by owner, came_by direct, "
                "and his exact words",
            )
        try:
            found = self._storage.preflight_record_by_id(request.record_id)
        except Exception as exc:
            if isinstance(exc, BrainRuntimeError):
                raise
            raise self._map_storage_error(exc, reading=True) from exc
        if found.match is None:
            raise BrainRuntimeError("brain.not_found", "Memory record was not found")
        record = found.match.record
        if authorization.owner_locator not in record.vaults or authorization.owner_id not in record.owners:
            raise BrainRuntimeError("brain.policy.excluded", "Record is outside the authorized owner")
        from gremlin_town.review import calendar_entries

        if record.kind != "item" or record.state not in calendar_entries.CLOSED_STATES:
            raise BrainRuntimeError(
                "brain.invalid_argument", "Only a done or dropped task's calendar entry can be removed"
            )
        if self._successor_ids(record.id, record.kind, found.revision):
            raise BrainRuntimeError(
                "brain.record.changed", "The task changed since; read it again before removing its calendar entry"
            )
        status, text, reason = calendar_entries.remove(self._calendar, record)
        return CalendarRemoveResult(
            operation_id=request.operation_id, record_id=record.id, status=status, text=text, reason=reason,
        )

    async def record(
        self, authorization: OwnerAuthorization, request: DecisionRequest
    ) -> WriteReceipt:
        self._authorize(authorization, BrainOperation.DECISION_RECORD, "write")
        return await anyio.to_thread.run_sync(self._record, authorization, request)

    def _existing_origin(
        self, authorization: OwnerAuthorization, request: DecisionRequest
    ) -> tuple[Record, GitHubPublicationResult] | None:
        if _ORIGIN_ID.fullmatch(request.operation_id) is None:
            raise BrainRuntimeError(
                "brain.invalid_argument",
                "operation_id must match ^[a-z0-9][a-z0-9-]{0,127}$; choose a new id in that shape",
            )
        receipt_path = f"{_RECEIPT_ROOT}/{request.operation_id}.json"
        try:
            receipt_read = self._storage.read((receipt_path,))
        except GitHubStorageError as exc:
            if exc.code == "github_path_missing":
                return None
            raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc
        receipt_bytes = receipt_read.file(receipt_path).content
        receipt = _receipt_mapping(receipt_bytes)
        locator = receipt.get("canonical_locator")
        if not isinstance(locator, str):
            raise BrainRuntimeError("brain.record.corrupt", "Accepted durability receipt has no locator")
        record_path = _safe_record_path(locator, authorization.owner_locator)
        try:
            pair = self._storage.read((record_path, receipt_path))
            record_bytes = pair.file(record_path).content
            receipt_bytes = pair.file(receipt_path).content
            accepted = self._storage.lookup_accepted(
                record_bytes=record_bytes, receipt_bytes=receipt_bytes
            )
        except Exception as exc:
            if isinstance(exc, BrainRuntimeError):
                raise
            raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc
        if accepted is None:
            raise BrainRuntimeError(
                "brain.record.corrupt", "Remote origin has no independently accepted publication"
            )
        record = self._records.assert_exact_decision_replay(
            authorization,
            request,
            record_bytes=record_bytes,
            receipt_bytes=receipt_bytes,
        )
        self._records.remember_semantic(
            authorization, accepted.semantic_key, accepted.record_path
        )
        return record, accepted

    def _canonical_receipt(self, accepted, request):
        """Independent coherent readback; the staging receipt alone is insufficient."""
        receipt_path = f"{_RECEIPT_ROOT}/{request.operation_id}.json"
        try:
            pair = self._storage.read((accepted.record_path, receipt_path))
            record_bytes = pair.file(accepted.record_path).content
            receipt_bytes = pair.file(receipt_path).content
            if (hashlib.sha256(record_bytes).hexdigest() != accepted.record_sha256
                    or hashlib.sha256(receipt_bytes).hexdigest() != accepted.receipt_sha256):
                raise BrainRuntimeError("brain.record.corrupt", "Canonical readback differs from accepted publication")
            return _receipt_mapping(receipt_bytes)
        except BrainRuntimeError:
            raise
        except Exception as exc:
            raise self._map_storage_error(exc, reading=True) from exc

    @staticmethod
    def _transport_evidence():
        return {"storage_transport": "github", "remote_io": True,
                "canonical_readback": True, "network_calls": None,
                "staging_receipt_network_count_is_not_hosted_count": True}

    def _decision_preflight(self, semantic_key: str) -> GitHubDecisionPreflight:
        # Decisions are never replaced, so each is one file: through the hub's copy, the check reads
        # up to Brain Core's highest bound, 1,000 (the storage-limit plan, layer 1; the default was 256).
        try:
            return self._storage.preflight_decision_by_semantic_key(
                semantic_key, max_candidates=MAX_DECISION_CANDIDATES,
            )
        except Exception as exc:
            if isinstance(exc, BrainRuntimeError):
                raise
            raise self._map_storage_error(exc, reading=True, nothing_saved=True) from exc

    def _record(
        self, authorization: OwnerAuthorization, request: DecisionRequest
    ) -> WriteReceipt:
        with self._decision_lock:
            semantic_key = self._records.decision_semantic_key(authorization, request)
            for _ in range(_PREFLIGHT_ATTEMPTS):
                prior = self._existing_origin(authorization, request)
                if prior is not None:
                    record, accepted = prior
                    return WriteReceipt(
                        operation_id=request.operation_id,
                        status="duplicate",
                        owner_locator=authorization.owner_locator,
                        record_id=record.id,
                        version=accepted.commit_sha,
                        content_sha256=accepted.record_sha256,
                        receipt_sha256=accepted.receipt_sha256,
                        remote_observed=True,
                        durability_receipt=self._canonical_receipt(accepted, request),
                        transport_evidence=self._transport_evidence(),
                    )

                preflight = self._decision_preflight(semantic_key)
                existing_bytes = (
                    None if preflight.match is None else preflight.match.content
                )
                if preflight.match is not None and (
                    preflight.match.record.kind != "decision"
                    or preflight.match.record.dedupe_key != semantic_key
                    or authorization.owner_locator not in preflight.match.record.vaults
                ):
                    raise BrainRuntimeError(
                        "brain.record.corrupt", "Semantic decision lookup resolved the wrong record"
                    )

                material = self._records.materialize_decision(
                    authorization,
                    request,
                    existing_record_bytes=existing_bytes,
                )
                try:
                    result = self._storage.publish(
                        record_bytes=material.record_bytes,
                        receipt_bytes=material.receipt_bytes,
                        expected_revision=preflight.revision.commit_sha,
                    )
                except GitHubConflict as exc:
                    if exc.code == "github_revision_conflict":
                        continue
                    raise self._map_storage_error(exc) from exc
                except Exception as exc:
                    if isinstance(exc, BrainRuntimeError):
                        raise
                    raise self._map_storage_error(exc) from exc
                self._records.remember_semantic(
                    authorization, result.semantic_key, result.record_path
                )
                return WriteReceipt(
                    operation_id=request.operation_id,
                    status=(
                        "duplicate"
                        if material.receipt.status == "duplicate/no-op"
                        or result.status == "duplicate/no-op"
                        else "accepted"
                    ),
                    owner_locator=authorization.owner_locator,
                    record_id=material.record.id,
                    version=result.commit_sha,
                    content_sha256=result.record_sha256,
                    receipt_sha256=result.receipt_sha256,
                    remote_observed=result.remote_accepted,
                    durability_receipt=self._canonical_receipt(result, request),
                    transport_evidence=self._transport_evidence(),
                )
            raise BrainRuntimeError(
                "brain.record.write_failed",
                "GitHub branch changed repeatedly during decision publication",
                retryable=True,
            )


__all__ = ["BrainCoreRecordAdapter", "GitHubBrainProvider"]
