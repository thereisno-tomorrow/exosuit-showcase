"""Pure work changes: the twelve moves in work-model plan sections 4.2–4.9.

The hub supplies a pinned work index, authenticated actor and accepted clock.
This module performs no IO or model calls. Review questions and undo history
must be supplied and validated by the hub; this module never reads a why-log.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy
from datetime import date, datetime, timedelta
import re
from typing import Any, Literal, Mapping, NotRequired, Sequence, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain_core.records import Record
from brain_core.work import WorkIndex
from brain_core.presentation import relative_day_words, work_words
from brain_core import language, timewords, work
from brain_core.errors import BrainError
from brain_core.contracts import canonical_json
from brain_core.records import WORK_FIELDS, record_from_mapping
from brain_core.review import FILLER_WORDS, _clock_word
from brain_core.routines import is_routine, occurrence_day


_MOVES = frozenset({"new", "start", "tick", "parts", "next_action", "wait", "date",
                    "end", "park", "organise", "undo", "stake"})
_CLOSED = frozenset({"done", "dropped", "no_longer_needed"})
_OWNER_ORGANISE = frozenset({"called", "accept", "keep", "title", "merge_into", "block"})
_SURFACE_FIELDS = {"new": {"review", "after"}, "organise": {"note", "reopen", "can_do_now", "review_outcome", "review_day", "remaining_minutes", "occurrence_outcome", "clear_wait", "planning", "material_kind", "requirements", "portion_outcomes", "portion_selection", "followup_disposition"}, "date": {"unpark", "appointment_timezone"},
                   "parts": {"approved_draft"}, "next_action": {"approved_draft", "details"}}
_FIELDS = {
    "new": {"title", "details", "level", "owed_to", "repeat", "when", "reading", "label", "block", "rough_minutes", "length_is_nics", "unclear", "looks_like", "stake", "ask_stake"},
    "start": set(), "tick": {"what", "when", "reading"}, "parts": {"parts"}, "next_action": {"text"},
    "wait": {"kind", "after", "who", "what", "expected", "event", "until", "reading"},
    "date": {"when", "reading", "event", "replaces", "proposed", "kind"},
    "end": {"ending", "reason"}, "park": {"until", "event"},
    "organise": {"called", "under", "label", "order", "finish_line", "merge_into", "accept", "keep", "title", "rough_minutes", "length_is_nics", "block"},
    "undo": {"change"}, "stake": {"level", "at_stake", "odds", "cost_shape", "confirm"},
}
_WORD = re.compile(r"\w+", re.UNICODE)
_NAME_REPLY = re.compile(r"\s*(?:\d+[.)]?\s*)?(?:yes|yep|yeah|ok|okay|save)"
                         r"(?:[, ]+use (?:that|that name|it))?[.!]?\s*", re.I)
_REVIEW_LENGTH = re.compile(r"(?<![\w.\-])(?P<amount>\d+|a|an|one|two)\s*"
                           r"(?P<unit>minutes?|mins?|hours?|hrs?|h)\b", re.I)


class _Refused(Exception):
    def __init__(self, message: str, field: str, fix: str, *, code: str = "brain.invalid_argument",
                 candidates: Sequence[str] = ()) -> None:
        self.detail = {"code": code, "message": message, "field": field, "fix": fix}
        if candidates:
            self.detail["which_one"] = list(candidates)


def _text(value: Any, field: str, cap: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or (cap and len(value) > cap):
        raise _Refused(f"{field} needs non-empty text" + (f" of at most {cap} characters" if cap else ""),
                       field, "send the named field with valid text")
    return value


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def _one_key(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) == len(right):
        differences = [i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        return len(differences) == 1 or (len(differences) == 2 and
               differences[1] == differences[0] + 1 and
               left[differences[0]] == right[differences[1]] and
               left[differences[1]] == right[differences[0]])
    if abs(len(left) - len(right)) != 1:
        return False
    shorter, longer = sorted((left, right), key=len)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def _his_wording(text: str, message: str) -> bool:
    wanted, said = _words(text), _words(message)
    return bool(wanted) and all(word in said or (len(word) >= 5 and
           any(_one_key(word, candidate) for candidate in said)) for word in wanted)


def _accepted_name(answer: Mapping[str, Any], title: str, message: str) -> bool:
    """A narrow name-only grant from the host's shown line and Nic's explicit yes/Save."""
    if answer.get("tidy_suggestion"):
        return _accepted_tidy(answer, "title", title, message)
    return (answer.get("suggested_name") == title and "title" in answer["offered"] and
        bool(_NAME_REPLY.fullmatch(message)))


def _accepted_tidy(answer, field, value, message):
    values = (answer.get("suggested_title"), answer.get("suggested_name")) if field == "title" else (answer.get("suggested_label"),)
    return (answer.get("tidy_suggestion") is True and value in values and field in answer["offered"]
            and bool(re.fullmatch(r"\s*save[.!]?\s*", message, re.I)))


def _tidy_owner_wording(value: str, message: str) -> bool:
    """Button replies approve the frozen proposal; they are never replacement text."""
    return (not language.is_bare_reply(message) and not _NAME_REPLY.fullmatch(message)
            and _his_wording(value, message))


def _answered_marks(answer: Mapping[str, Any], move: Move) -> set[str]:
    if ((answer.get("tidy_suggestion") or answer.get("length_choices") or (answer.get("suggested_name") and "title" in move)) and move["move"] == "organise"
            and _FIELDS["organise"].intersection(move) <= {"title", "label", "rough_minutes", "length_is_nics"}):
        # Tidy fields share the New line with independent questions. Saving its
        # wording does not answer any of those questions, even on the same line.
        return set()
    return set(answer.get("marks", ()))


def _remaining_marks(marks, answers, move):
    """A name answer clears its exact displayed name question, never a sibling."""
    cleared = set().union(*(_answered_marks(answer, move) for answer in answers))
    naming_questions = {answer["question"] for answer in answers
        if answer.get("suggested_name") and move.get("move") == "organise"
        and "title" in move
        and set(answer.get("offered", ())) <= {"title"}}
    return [mark for mark in marks if mark["mark"] not in cleared
            and not (mark["mark"] == "question" and mark.get("text") in naming_questions)]


def _accepted_review_length(answer: Mapping[str, Any], minutes: int, message: str) -> bool:
    """An offered duration or one unambiguous duration in the owner's statement."""
    if _words(message).intersection({"skip", "cancel", "no", "not", "never", "don", "dont", "or"}):
        return False
    found = list(_REVIEW_LENGTH.finditer(message))
    if len(found) != 1:
        return False
    match = found[0]
    amount = match["amount"].casefold()
    amount = int(amount) if amount.isdecimal() else {"a": 1, "an": 1, "one": 1, "two": 2}[amount]
    if amount * (60 if match["unit"].lower().startswith("h") else 1) != minutes:
        return False
    remaining = message[:match.start()] + message[match.end():]
    remainder = _words(remaining)
    if (any(word.isdecimal() for word in remainder)
            or remainder.intersection({"half", "quarter", "minute", "minutes", "min", "mins", "hour", "hours", "hr", "hrs"})):
        return False
    if not remainder:
        return minutes in answer.get("length_choices", ())
    return bool(remainder - {"yes", "ok", "okay", "save", "please"})


def _title_key(text: str) -> str:
    return " ".join(_WORD.findall(text.casefold()))


def _owner_quote(move: Move, message: str) -> bool:
    quote = move.get("words")
    return isinstance(quote, str) and not language.is_bare_reply(quote) and timewords.from_words(quote, message)


def _owner_only(move: Move, record: Record | None) -> bool:
    name = move["move"]
    return (name in {"end", "park", "undo"} or
            (name == "tick" and move.get("what", "thing") == "thing" and
             (record is None or not is_routine(record))) or
            (name == "organise" and (bool(_OWNER_ORGANISE.intersection(move)) or move.get("length_is_nics"))) or
            (name == "stake" and (bool(move.get("confirm")) or bool(record and record.data.get("stake", {}).get("confirmed")))))


def _require_supported_repeat(record: Record) -> None:
    """Preserve opaque old cadences until the caller supplies a supported code."""
    if record.data.get("repeat") is not None and not is_routine(record):
        raise _Refused("This repeat code cannot be mutated", "repeat",
                       "clarify the cadence as daily or weekly:<mon..sun>")


def _mark(data: dict[str, Any], mark: Mapping[str, Any]) -> None:
    entry = dict(mark)
    for field, cap in (("text", 1024), ("part", 128)):
        if field in entry:
            _text(entry[field], f"review_marks.{field}", cap)
    marks = data.setdefault("review_marks", [])
    if entry in marks and entry["mark"] != "question":
        return
    if len(marks) >= 10:
        position = next((i for i, old in enumerate(marks) if old["mark"] == "question"), 0)
        marks.pop(position)
    marks.append(entry)


def _length(data: dict[str, Any], move: Mapping[str, Any], actor: str, *, field_prefix: str = "") -> None:
    if "block" in move:
        data["block"] = move["block"]
    if "rough_minutes" in move:
        minutes = move["rough_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise _Refused(field_prefix + "rough_minutes needs a positive integer",
                           field_prefix + "rough_minutes", "send whole minutes")
        own = move.get("length_is_nics", False)
        data.setdefault("timing", {}).update(duration_minutes=minutes,
            padded_minutes=minutes if own else (minutes * 3 + 1) // 2,
            estimated_by="nic" if own else actor)


def _stake(data: dict[str, Any], move: Mapping[str, Any], actor: str, confirmed: bool = False,
           *, field_prefix: str = "") -> None:
    proposal = deepcopy(data.get("stake", {}))
    for key in ("level", "at_stake", "odds", "cost_shape"):
        if key in move:
            proposal[key] = move[key]
    for key, cap in (("at_stake", 512), ("odds", 256)):
        if key in proposal:
            _text(proposal[key], field_prefix + key, cap)
    proposal.update(confirmed=confirmed, proposed_by=actor)
    data["stake"] = proposal


def _shape(index: WorkIndex) -> None:
    for fid, record in index.items():
        parent = record.data.get("parent")
        if parent:
            if parent not in index:
                raise _Refused(f"Parent {parent} was not supplied", "under", "name an existing parent")
            if record.data.get("called") == "goal" or (record.data.get("called") == "project" and
                    work.kind_of(parent, index) != "goal") or work.kind_of(parent, index) == "subtask":
                raise _Refused("This parent breaks the work shape", "under", "use goal → project → task → subtask")
        for field in ("parent", "after"):
            def visit(current: str, trail: set[str]) -> None:
                if current in trail:
                    raise _Refused(f"A loop of {field} was found", field, "remove the link closing the loop")
                value = index[current].data.get(field, [])
                links = [value] if isinstance(value, str) else value
                for target in links:
                    if target not in index:
                        raise _Refused(f"Linked thing {target} was not supplied", field, "name an existing thing")
                    visit(target, trail | {current})
            visit(fid, set())


class Move(TypedDict, total=False):
    """One planned move; fields are checked according to its move name.

    ``thing`` accepts any version ID, an exact normalised open title, or #n.
    ``words`` authorises an owner-only move by quoting the message exactly.
    Nested parts, readings and stakes retain the plan's field names.
    """

    move: Literal["new", "start", "tick", "parts", "next_action", "wait", "date",
                  "end", "park", "organise", "undo", "stake"]
    thing: str
    words: str
    title: str
    details: str
    level: str
    owed_to: str
    repeat: str
    when: str
    reading: Mapping[str, Any]
    label: str
    block: str
    rough_minutes: int
    length_is_nics: bool
    unclear: bool
    looks_like: Literal["goal", "project"]
    stake: Mapping[str, Any]
    ask_stake: bool
    what: str
    parts: Sequence[Mapping[str, Any]]
    text: str
    kind: str
    after: Sequence[str]
    who: str
    expected: str
    event: str
    until: str
    replaces: str
    proposed: bool
    ending: Literal["dropped", "no_longer_needed"]
    reason: str
    called: Literal["goal", "project", "task"]
    under: str
    order: int
    finish_line: str
    merge_into: str
    accept: bool
    keep: str
    change: str
    at_stake: str
    odds: str
    cost_shape: Literal["grows", "lands"]
    confirm: bool
    # Private authenticated surface extensions, never public tool fields.
    review_outcome: Literal["progress", "not_done", "happened", "not_happened", "canceled", "no_show"]
    review_day: str
    remaining_minutes: int
    occurrence_outcome: Literal["done", "skipped", "missed", "canceled"]
    clear_wait: bool
    planning: Mapping[str, Any]
    followup_disposition: Mapping[str, Any]
    material_kind: Literal["information", "reflection", "idea", "arrangement"]
    requirements: Mapping[str, Sequence[str]]
    portion_outcomes: Sequence[Mapping[str, Any]]
    portion_selection: Mapping[str, Any]
    note: str
    reopen: bool
    can_do_now: bool
    review: bool
    unpark: bool


class ValidatedAnswer(TypedDict):
    """Review evidence validated by W12, restricted again to its offered target.

    ``offered`` contains the line's offered move/answer names, such as parts,
    done, merge, keep or accept. ``thing`` is the target's lasting first ID.
    """

    reference: str
    question: str
    offered: Sequence[str]
    thing: str
    day: str
    open: bool
    marks: NotRequired[Sequence[str]]
    suggested_name: NotRequired[str | None]
    suggested_title: NotRequired[str | None]
    suggested_label: NotRequired[str | None]
    tidy_suggestion: NotRequired[bool]
    length_choices: NotRequired[Sequence[int]]


class RecordEnvelope(TypedDict):
    """Existing record ownership/evidence fields supplied by authenticated W12.

    W12 derives vaults and owners from OwnerAuthorization and evidence_actor
    from the caller, on initial and replay calls alike. No public input field.
    """

    vaults: Sequence[str]
    owners: Sequence[str]
    evidence_actor: str


@dataclass(frozen=True)
class ExecutionContext:
    """Authenticated W12 execution inputs, never public HTTP/tool arguments.

    All IDs are exact preplanned IDs. Raw records include archive-read history;
    write_index contains only a thing's own mapped facts from W5.write_index.
    operation_hash excludes this context and is stored in Record.dedupe_key.
    capture_raise_with supplies existing raise_with text by exact NEW move ordinal only.
    authenticated_owner_tap is derived by the host from the exact bound request;
    it authorizes a selected action without inventing a quote from the owner.
    """

    raw_records: Mapping[str, Record]
    write_index: WorkIndex
    planned_ids: Mapping[tuple[int, int], str]
    answer_context: Sequence[ValidatedAnswer] = ()
    undo_changes: Mapping[str, Sequence[str]] = field(default_factory=dict)
    allowed_code_callers: frozenset[str] = frozenset()
    operation_hash: str = ""
    record_envelope: RecordEnvelope | None = None
    capture_bodies: Mapping[int, str] = field(default_factory=dict)
    capture_raise_with: Mapping[int, str] = field(default_factory=dict)
    authenticated_owner_tap: bool = False


class Change(TypedDict):
    """A caller-scoped retry key, source message and one to twenty moves.

    ``answers`` names review lines; the hub verifies their age, openness and
    target and supplies their question text and offered moves before applying.
    Retry IDs and archived prior versions likewise come from the hub, never IO.
    """

    operation_id: str
    message: str
    said_by: Literal["owner", "other"]
    came_by: Literal["direct", "connected_source"]
    speaker: NotRequired[str]
    answers: NotRequired[Sequence[str]]
    moves: Sequence[Move]
    _context: NotRequired[ExecutionContext]


@dataclass(frozen=True)
class Outcome:
    """All proposed records, retained flags, whole-change refusal and reply facts.

    A refusal names the field or move and its fix, and returns no records.
    The hub adds storage/version/calendar facts after atomic publication.
    """

    records: tuple[Record, ...]
    flags: tuple[Mapping[str, Any], ...]
    refusal: Mapping[str, Any] | None
    reply: Mapping[str, Any]


def _read_date(data: Mapping[str, Any], move: Mapping[str, Any], *, actor: str,
               now: datetime, message: str, named_kind: str | None = None,
               phrase_field: str = "when") -> tuple[str | None, dict[str, Any] | None, tuple[str, ...]]:
    phrase = _text(move.get("when"), phrase_field, 256)
    if not timewords.from_words(phrase, message):
        return None, None, ("date left off: phrase not in his words",)
    event = move.get("event")
    if event and not timewords.from_words(event, message):
        return None, None, ("date left off: event not in his words",)
    local = now.astimezone(timewords.SINGAPORE)
    reading = timewords.read_when(phrase, today=local.date(), now_clock=local.time(),
                                  owed_to=bool(data.get("owed_to")), event=event, past=named_kind == "done")
    flags: tuple[str, ...] = ()
    agent_read = reading is None
    if agent_read:
        if not move.get("reading"):
            return None, None, ("date left off: code could not read the phrase; supply reading",)
        proposed = dict(move["reading"])
        # Kind is read from the words, never from an agent's requested deadline.
        if named_kind is None:
            proposed["kind"] = ("start_by" if re.match(r"start (?:on it )?by\b", phrase, re.I) else
                "deadline" if data.get("owed_to") or re.match(r"(?:by|before|due|latest)\b", phrase, re.I) else
                "not_before" if event or re.match(r"(?:after|not before)\b", phrase, re.I) else
                "appointment" if proposed.get("at") else "planned")
        else:
            proposed["kind"] = named_kind
        checked = timewords.check_agent_reading(phrase, message, proposed, timewords.personal_today(now),
                                                owed_to=bool(data.get("owed_to")) or named_kind == "deadline",
                                                allow_past=named_kind == "done")
        reading, flags = checked.reading, checked.flags
        if reading is None:
            return None, None, tuple(f"date left off: {flag}" for flag in flags)
    kind = named_kind or reading.kind
    if kind == "done":
        if not reading.day:
            return None, None, ("date left off: when did you do it? Give one day",)
        if date.fromisoformat(reading.day) > timewords.personal_today(now):
            raise _Refused("A thing cannot be done on a future day", "when", "that day hasn't happened yet")
    value = {key: getattr(reading, key) for key in ("day", "at", "start", "end", "event")
             if getattr(reading, key) is not None}
    value.update(words=phrase, read_on=timewords.personal_today(now).isoformat(),
                 read_by=actor if agent_read or (named_kind and named_kind != "done") else "code")
    if kind == "done":
        value = {key: value[key] for key in ("words", "read_by", "read_on", "day")}
    replacing = move.get("replaces")
    if replacing:
        old = data.get("dates", {}).get(replacing)
        if old is None:
            raise _Refused("replaces must name an existing date kind", "replaces", "name a kind on this thing")
        day = value.get("day") or value.get("end")
        if not day:
            return None, None, ("date left off: replacement needs a day",)
        kind, value = replacing, {**deepcopy(old), "day": day, "words": phrase,
                                 "read_by": value["read_by"], "read_on": value["read_on"]}
    if move.get("proposed"):
        kind = "start_by"
        value["confirmed"] = False
    if kind == "start_by" and "deadline" not in data.get("dates", {}):
        return None, None, ("date left off: start-by needs a deadline",)
    if agent_read and named_kind in {None, "done"}:
        day = value.get("day") or value.get("start")
        words = relative_day_words(day, timewords.personal_today(now)) if day else value.get("event", phrase)
        flags += (f"I read '{phrase}' as {words}",)
    return kind, value, flags


def _reply_dates(dates: Mapping[str, Any], now: datetime) -> list[str]:
    """The door's readable dates; the reader and stored dates retain their wire shape."""
    today = timewords.personal_today(now)
    tied_to_event = work.event_day(dates)
    lines = []
    for kind, value in dates.items():
        if kind == "planned" and tied_to_event:
            continue
        day = value.get("day")
        if day:
            words = relative_day_words(day, today)
        elif value.get("start"):
            words = (relative_day_words(value["start"], today) + " to "
                     + relative_day_words(value["end"], today))
        else:
            words = value.get("event") or value.get("words", "")
        if value.get("at"):
            words += " at " + _clock_word(value["at"])
        if day and value.get("event"):
            words += f" ({value['event']})"
        prefix = {"deadline": "due", "appointment": "on", "planned": "planned",
                  "not_before": "on" if tied_to_event else "not before",
                  "suggested": "suggested", "start_by": "start by", "done": "done"}[kind]
        if kind == "suggested":
            prefix = f"{value['speaker']} suggested"
        lines.append(f"{prefix} {words}")
    return lines


def _unsaved_change(title: str, move: Mapping[str, Any], field: str, message: str) -> str:
    action = {"new": "saving", "start": "starting", "tick": "marking done", "end": "ending",
              "park": "parking", "date": "date change", "wait": "waiting change", "parts": "parts change",
              "organise": "renaming" if "title" in move else "organising", "stake": "stakes change",
              "next_action": "next action", "undo": "undo"}.get(move["move"], "change")
    if field.rsplit(".", 1)[-1] in {"when", "expected"}:
        action = "date change"
    reason = {
        "Park until needs a day in his words": "say which day to park it until",
        "Park event is not in his words": "say which event to park it until",
        "Move needs a non-bare quote from his message or an offered review answer":
            "quote what you said or answer the review line",
        "Other people's words may only create suggestions": "someone else's words can only be saved as suggestions",
        "Undo dropped: others' words": "someone else's words cannot undo your change",
        "Undo needs a non-bare quote or an offered review answer": "quote what you said or answer the review line",
        "Routine keeps its existing shape": "a routine keeps its existing shape",
        "Start needs his words naming what he started": "say what you started",
        "Dropping needs his reason": "say why you want to drop it",
        "Wait's person is not in his words": "say who you are waiting for",
        "Title needs your words or yes to this line's suggested name": "say the name or accept the suggested name",
        "Finish line is not in his words": "say what finished would look like",
        "Suggested appointment needs its time": "say the appointment time",
    }.get(message, message.replace("his words", "your words"))
    return f"{title}: {action} was not saved: {reason.rstrip('.')}."


def _reply(index: WorkIndex, records: Sequence[Record], flags: Sequence[Mapping[str, Any]],
           now: datetime, operation: str, moves: Sequence[Move]) -> dict[str, Any]:
    things = []
    for record in records[:5]:
        fid = work.first_id(record)
        current = index[fid]
        explanation = work.explain(fid, index, now)
        progress = work.progress_of(fid, index)
        parent = current.data.get("parent")
        next_item = work.next_of(fid, index, now)
        things.append({"id": fid, "record_id": record.id, "title": record.title,
            **({"details": record.body} if record.body else {}),
            "is": work.kind_of(fid, index), "under": index[parent].title if parent else None,
            "now": work_words(record.state if record.state in _CLOSED else explanation["status"]),
            "next": {**next_item, "reason": work_words(next_item["reason"])},
            "why": work_words(explanation["reason"]),
            "progress": progress["text"], "dates": _reply_dates(work.dates_of(fid, index), now),
            **({"done_day": work.done_day_of(fid, index).isoformat()} if current.state == "done" else {}),
            "flags": [flag for flag in flags if flag.get("thing") == fid]})
    def say() -> str:
        lines = []
        for thing in things:
            reason = (f"done {relative_day_words(thing['done_day'], timewords.personal_today(now))}"
                      if "done_day" in thing else thing["why"])
            lines.append(f"{thing['title']}: {thing['progress']}. {reason}"
                         + "".join(f". {words}" for words in thing["dates"] if words not in reason)
                         + ("\n" + "\n".join("  " + line for line in thing["details"].splitlines())
                            if thing.get("details") else ""))
        if len(records) > len(things):
            lines.append(f"and {len(records) - len(things)} more")
        for flag in flags:
            if flag["dropped"]:
                target = index.get(flag.get("thing"))
                lines.append(_unsaved_change(target.title if target else f"Change {moves[flag['move']].get('change', operation)}",
                                            moves[flag["move"]], flag["field"], flag["message"]))
            elif flag["message"].startswith("I read '"):
                lines.append(flag["message"])
        return ("\n" if any(thing.get("details") for thing in things) else " ").join(lines)
    reply = {"status": "accepted", "change": operation, "things": things, "say": say()}
    while len(canonical_json(reply)) > 2000 and things:
        things.pop()
        reply["say"] = say()
    # Never cut off an unsaved change's explanation to shorten a reply.
    while len(canonical_json(reply)) > 2000 and not any(flag["dropped"] for flag in flags):
        reply["say"] = reply["say"][:-1]
    return reply


class _Builder:
    """One in-memory batch; all record IDs and historical reads are caller supplied."""

    def __init__(self, index: WorkIndex, change: Change, actor: str, now: datetime,
                 context: ExecutionContext) -> None:
        self.index = dict(index)
        self.write = dict(context.write_index)
        self.change, self.actor, self.now = change, actor, now
        self.context = context
        self.ids, self.raw, self.answers = context.planned_ids, dict(context.raw_records), context.answer_context
        self.pending: dict[str, Record] = {}
        self.baselines: dict[str, Record | None] = {}
        self.origins: dict[str, tuple[int, int]] = {}
        self.provenance_questions: dict[str, tuple[ValidatedAnswer, ...]] = {}
        self.flags: list[dict[str, Any]] = []
        self.ordinal = 0
        self.questions: tuple[Mapping[str, Any], ...] = ()
        self.new = {}
        for n, (i, _) in enumerate(((i, move) for i, move in enumerate(change["moves"]) if move["move"] == "new"), 1):
            if (i, 0) not in self.ids:
                raise _Refused("Missing planned new ID", "_context.planned_ids", "supply the new move's slot 0")
            self.new[f"#{n}"] = self.ids[(i, 0)]

    @property
    def records(self) -> list[Record]:
        return [self.pending[fid] for fid in sorted(self.pending, key=self.origins.__getitem__)]

    def resolve(self, reference: Any, *, field: str = "thing") -> Record:
        value = _text(reference, field)
        value = self.new.get(value, value)
        for fid, record in self.write.items():
            if value in (fid, record.id, *record.supersedes):
                return record
        matched = [r for r in self.write.values() if r.state not in _CLOSED and
                   _title_key(r.title) == _title_key(value)]
        if len(matched) != 1:
            raise _Refused(f"{value!r} matches {len(matched)} open things", field,
                "name one of these IDs" if matched else "name an existing thing or send new",
                candidates=[r.id for r in matched])
        return matched[0]

    def flag(self, record: Record, message: str, field: str, *, mark: str | None = None,
             dropped: bool = False) -> Record:
        if dropped and self.questions:
            raise _Refused(_unsaved_change(record.title, self.change["moves"][self.ordinal], field, message).rstrip('.'),
                           field, "correct the named field and answer the still-open review line")
        fid = work.first_id(record)
        self.flags.append({"move": self.ordinal, "thing": fid, "field": field,
                           "message": message, "dropped": dropped,
                           "words": self.change["message"]})
        data = deepcopy(record.data)
        if record.data.get("repeat") is None:
            if dropped:
                _mark(data, {"mark": "question", "text":
                            f"{self.change['message']} — {self.change['moves'][self.ordinal]['move']}: {message}"[:1024]})
            elif mark:
                _mark(data, {"mark": mark, "text": message[:1024]})
        return replace(record, data=data)

    def publish(self, draft: Record, previous: Record | None, move: Move, slot: int = 0,
                restored: Record | None = None, *, observation: bool = False,
                obligations_checked: bool = False) -> Record:
        _require_supported_repeat(draft)
        fid = work.first_id(previous) if previous else draft.id
        if not obligations_checked:
            self.protect_obligations({**self.write, fid: draft}, field="move")
        if fid not in self.pending and len(self.pending) >= 25:
            raise _Refused("A change may write at most 25 records", "moves", "split it")
        origin = self.origins.setdefault(fid, (self.ordinal, slot))
        if fid not in self.baselines:
            self.baselines[fid] = previous
        baseline = self.baselines[fid]
        questions = {answer["reference"]: answer for answer in self.provenance_questions.get(fid, ())}
        questions.update({answer["reference"]: answer for answer in self.questions})
        self.provenance_questions[fid] = tuple(questions.values())
        try:
            record_id = self.ids[origin]
        except KeyError:
            raise _Refused("The hub has not supplied this planned record ID", "planned_ids",
                           "plan every record position before applying") from None
        quote = self.change["message"] if self.questions or not _owner_only(move, previous) else move.get("words", self.change["message"])
        source = {"said_by": "other" if self.change["came_by"] == "connected_source" else self.change["said_by"], "came_by": self.change["came_by"],
                  "words": quote, "recorded_by": self.actor}
        if self.change.get("speaker"):
            source["speaker"] = self.change["speaker"]
        if observation or questions or fid in self.pending:
            # A coalesced version keeps the literal complete message as its quote;
            # authority for each mutation was checked before reaching this point.
            quote = self.change["message"]
            source["words"] = quote
        evidence = list(previous.as_dict()["evidence"]) if previous else []
        for question in self.questions:
            shown = question["question"]
            if move["move"] == "organise" and question.get("tidy_suggestion"):
                for field in ("title", "label"):
                    if _accepted_tidy(question, field, move.get(field, ""), self.change["message"]):
                        shown += f" Suggested {field}: {move[field]}. Save, or your own?"
            elif move["move"] == "organise" and _accepted_name(question, move.get("title", ""), self.change["message"]):
                shown += f" Suggested: {question['suggested_name']}. Yes, or your own?"
            evidence.append(_evidence(shown, question["reference"]))
        if move.get("approved_draft") and (slot or move["move"] == "next_action"):
            approval = move["approved_draft"]
            evidence.append({"source_type": "agent_result", "locator": "gt:request/" + approval["request_id"],
                             "content_sha256": approval["sha256"], "actor": approval["author"],
                             "excerpt": (move["text"] + "\n" + move["details"] if move["move"] == "next_action"
                                         else draft.title + ("\n" + draft.body if draft.body else ""))})
        evidence.append(_evidence(quote, "message"))
        history = list(dict.fromkeys((*baseline.supersedes, baseline.id))) if baseline else []
        mapping = deepcopy(draft.as_dict())
        if "review_marks" in mapping["data"]:
            mapping["data"]["review_marks"] = _remaining_marks(mapping["data"]["review_marks"],
                [question for question in self.questions if question["thing"] == fid], move)
        mapping.update(id=record_id, created_at=self.now.isoformat(), updated_at=self.now.isoformat(),
                       source=source, evidence=evidence, supersedes=history, dedupe_key=self.context.operation_hash)
        mapping = _enveloped_mapping(mapping, self.context.record_envelope)
        try:
            record = record_from_mapping(mapping)
        except BrainError as error:
            raise _Refused(error.message, error.details.get("field", "record"),
                           "correct the named field to the record contract", code=str(error.code)) from error
        checked_previous = previous
        question_previous = self.context.write_index.get(fid) if observation else None
        if question_previous is not None and self.context.record_envelope is not None:
            question_previous = record_from_mapping(_enveloped_mapping(question_previous.as_dict(), self.context.record_envelope))
        question_only = observation and not questions and restored is None and language._dropped_question_only(
            question_previous, record, self.change["message"], self.actor)
        if question_only:
            checked_previous = question_previous
        elif observation and fid not in self.pending:
            checked_previous = self.write.get(fid)
        if checked_previous is not None and self.context.record_envelope is not None:
            checked_previous = record_from_mapping(_enveloped_mapping(checked_previous.as_dict(), self.context.record_envelope))
        try:
            language.check_change(checked_previous, record, actor=self.actor, message=self.change["message"],
                answers=tuple(q["question"] for q in questions.values()), replaces=move.get("replaces"), restored=restored,
                dropped_question=question_only)
        except BrainError as error:
            refusal = _Refused(error.message, error.details.get("field", "message"),
                error.hint or "correct the named field using the owner's exact message", code=str(error.code))
            refusal.detail["details"] = deepcopy(error.details)
            raise refusal from error
        self.pending[fid] = record
        self.raw[record.id] = record
        self.write[fid] = record
        self.write = work.write_index(self.write.values())
        _shape(self.write)
        # Only the reader builds effective inherited projections for replies.
        self.index = work.index_things(self.write.values())
        return record

    def protect_obligations(self, candidate: WorkIndex, *, field: str) -> None:
        """Validate the entire proposed graph before publishing corrective versions."""
        from brain_core.planning import active_followup_ref, required_followup, followup_triggered
        candidate = work.index_things(candidate.values())
        for ref, source in self.write.items():
            operation = source.data.get("planning", {}).get("operation", {})
            if operation.get("actual_start"):
                proposed = candidate.get(ref)
                if proposed is None or proposed.data.get("planning", {}).get("operation") != operation:
                    raise _Refused("Confirmed physical startup cannot be erased or rewritten", field,
                                   "preserve the actual start and correct the remaining obligation explicitly")

            history = source.data.get("planning", {}).get("followup_dispositions", [])
            proposed_history = candidate.get(ref).data.get("planning", {}).get("followup_dispositions", []) if candidate.get(ref) else []
            if proposed_history[:len(history)] != history:
                raise _Refused("Accepted follow-up history cannot be erased or rewritten", field,
                               "append an explicit governed disposition")

        def blocked_path(index, target_ref):
            target = index.get(target_ref)
            if target is not None and target.state == "done":
                return set()
            blocked, seen = set(), set()
            while target_ref:
                if target_ref in seen:
                    raise _Refused("Work parent cycle", field, "correct the parent relationship")
                seen.add(target_ref)
                target = index.get(target_ref)
                if target is None:
                    blocked.add(target_ref)
                    break
                if target.state in _CLOSED or target.state == "parked":
                    blocked.add(target_ref)
                target_ref = target.data.get("parent")
            return blocked

        followup_refs = {active_followup_ref(source)
                         for source in candidate.values()
                         if followup_triggered(source, active_followup_ref(source))}
        for ref in followup_refs:
            if ref is None:
                continue
            target = candidate.get(ref)
            if target is not None and target.state == "done":
                continue
            if blocked_path(candidate, ref) - blocked_path(self.write, ref):
                raise _Refused("This work contains a triggered required follow-up", field,
                               "finish the required return or explicitly change its obligation before dropping or parking")
        # Preserve the existing rule for ending the started source even if its
        # target is missing or was dropped by a legacy client.
        for ref, source in candidate.items():
            required = required_followup(source, candidate)
            if not required or required["conditional"] or not required["approved"]:
                continue
            target = candidate.get(required["item_ref"])
            previous = self.write.get(ref)
            if (source.state in {"dropped", "no_longer_needed", "parked"}) and (
                    previous is None or source.state != previous.state) and (
                    target is None or target.state != "done"):
                raise _Refused("This started operation still has its required follow-up", field,
                               "finish or explicitly transfer the remaining obligation before dropping or parking")

    def typed_appointment(self, move: Mapping[str, Any], message: str) -> tuple[str, dict[str, Any], tuple[str, ...]]:
        """Authenticated explicit instants keep the existing Singapore date representation."""
        phrase = _text(move.get("when"), "when", 256)
        zone_name = _text(move.get("appointment_timezone"), "appointment_timezone", 128)
        if phrase not in message or zone_name not in message:
            raise _Refused("Appointment time and zone must be in your submitted answer", "when",
                           "submit the exact clock time and time zone together")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::00)?(?:Z|[+-]\d{2}:\d{2})", phrase):
            raise _Refused("Appointment needs a complete clock time and explicit offset", "when",
                           "confirm the day, clock time and offset at minute precision")
        try:
            instant = datetime.fromisoformat(phrase)
            zone = ZoneInfo(zone_name)
            local = instant.astimezone(zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise _Refused("Appointment time or time zone is invalid", "appointment_timezone",
                           "confirm a valid clock time and named time zone")
        if local.replace(tzinfo=None) != instant.replace(tzinfo=None) or local.utcoffset() != instant.utcoffset():
            raise _Refused("Appointment clock and offset do not match its time zone", "when",
                           "confirm the actual offset, including which repeated clock time you mean")
        singapore = instant.astimezone(timewords.SINGAPORE)
        value = {"day": singapore.date().isoformat(), "at": singapore.strftime("%H:%M"),
                 "words": f"{phrase} ({zone_name})", "read_by": "code",
                 "read_on": timewords.personal_today(self.now).isoformat()}
        if move.get("proposed"):
            value["confirmed"] = False
        return "appointment", value, ()

    def date(self, record: Record, move: Mapping[str, Any], message: str, *, field_prefix: str = "") -> Record:
        if move.get("unpark") and record.state not in {"open", "parked"}:
            raise _Refused("Only an open or parked thing can be planned this way", "unpark",
                           "refresh this thing before changing its day")
        if "appointment_timezone" in move:
            kind, value, flags = self.typed_appointment(move, message)
        else:
            kind, value, flags = _read_date(record.data, move, actor=self.actor, now=self.now, message=message,
                                          named_kind=move.get("kind") if self.actor in self.context.allowed_code_callers else None,
                                          phrase_field=field_prefix + "when")
        for flag in flags:
            record = self.flag(record, flag, field_prefix + "when", dropped=value is None)
        if value is None:
            return record
        data = deepcopy(record.data)
        if self.change["said_by"] == "other" or self.change["came_by"] == "connected_source":
            if kind not in {"deadline", "appointment", "planned"} or not (value.get("day") or value.get("end")):
                return self.flag(record, "date left off: others' suggestion needs a day", "when", dropped=True)
            value.update(kind=kind, speaker=self.change["speaker"], day=value.get("day") or value.get("end"))
            kind = "suggested"
        data.setdefault("dates", {})[kind] = value
        if kind == "not_before" and value.get("day") and value.get("event"):
            data["dates"]["planned"] = deepcopy(value)
        state = record.state
        if move.get("unpark") and state == "parked":
            state = "open"
            data.pop("parked_until", None)
        return replace(record, data=data, state=state)

    def apply(self, move: Move) -> None:
        name = move["move"]
        record = None if name == "new" else self.resolve(move.get("thing")) if name != "undo" else None
        if record is not None:
            _require_supported_repeat(record)
        fid = work.first_id(record) if record else None
        self.questions = tuple(q for q in self.answers if q["thing"] == fid and _offered(q, move))
        message = " ".join((self.change["message"], *(q["question"] for q in self.questions)))
        owner = self.change["said_by"] == "owner" and self.change["came_by"] == "direct"
        if name != "new" and not owner:
            if record:
                dropped = self.flag(record, "Other people's words may only create suggestions", "said_by", dropped=True)
                self.publish(dropped, self.raw[record.id], move, observation=True)
            else:
                self.flags.append({"move": self.ordinal, "field": "said_by", "message": "Undo dropped: others' words", "dropped": True})
            return
        if (_owner_only(move, record) and not self.questions and not self.context.authenticated_owner_tap
                and not _owner_quote(move, self.change["message"])):
            if name == "undo":
                self.undo(move, authorised=False)
                return
            dropped = self.flag(record, "Move needs a non-bare quote from his message or an offered review answer", "words", dropped=True)
            if name == "stake":
                data = deepcopy(dropped.data)
                if record.data.get("stake", {}).get("confirmed"):
                    self.publish(dropped, self.raw[record.id], move, observation=True)
                    return
                _stake(data, move, self.actor)
                dropped = replace(dropped, data=data)
            self.publish(dropped, self.raw[record.id], move, observation=True)
            return
        if name == "new":
            self.capture(move, message)
            return
        if name == "undo":
            self.undo(move, authorised=True)
            return
        previous = self.raw[record.id]
        data, state, title = deepcopy(record.data), record.state, record.title
        operation = data.get("planning", {}).get("operation", {})
        if name in {"end", "park"} and operation.get("actual_start"):
            from brain_core.planning import required_followup
            required = required_followup(record, self.index)
            followup = self.index.get(required["item_ref"]) if required else None
            if required and (followup is None or followup.state != "done"):
                raise _Refused("This started operation still has its required follow-up", "move", "finish or explicitly transfer the remaining obligation before dropping or parking")
        if name == "parts":
            self.parts(record, move, message)
            return
        if is_routine(record) and name not in {"tick", "end", "park"} and not (name == "organise" and "occurrence_outcome" in move):
            self.flag(record, "Routine keeps its existing shape", "move", dropped=True)
            return
        if name == "start":
            if language.is_bare_reply(message):
                record = self.flag(record, "Start needs his words naming what he started", "message", dropped=True)
                data = record.data
            else:
                data["started_at"] = self.now.isoformat()
        elif name == "tick":
            if move.get("what", "thing") not in {"thing", "next_action"}:
                raise _Refused("Unknown tick target", "what", "send thing or next_action")
            if ("when" in move or "reading" in move) and (is_routine(record) or move.get("what", "thing") == "next_action"):
                raise _Refused("Only a whole thing takes a done day", "when", "leave when and reading off this tick")
            if is_routine(record):
                if state != "open":
                    raise _Refused("A closed or parked routine cannot be ticked", "thing", "name an open routine")
                data["occurrence"] = {"day": occurrence_day(data["repeat"], timewords.personal_today(self.now)).isoformat(), "outcome": "done"}
            elif move.get("what", "thing") == "next_action":
                if "next_action" not in data:
                    raise _Refused("No next action to tick", "what", "set a next action first")
                data["next_action"]["done"] = True
            else:
                state = "done"
                data["check"] = {"locator": "message", "checked_by": "nic"}
                if "when" in move:
                    _, value, flags = _read_date(data, move, actor=self.actor, now=self.now,
                                                message=self.change["message"], named_kind="done")
                    for flag in flags:
                        record = self.flag(replace(record, data=data), flag, "when", dropped=value is None)
                        data = record.data
                    if value:
                        data.setdefault("dates", {})["done"] = value
                else:
                    day = timewords.personal_today(self.now).isoformat()
                    words = (self.change["message"] if self.questions else
                             move.get("words", self.change["message"]) if self.context.authenticated_owner_tap
                             else move["words"])
                    data.setdefault("dates", {})["done"] = {
                        "day": day, "read_on": day, "read_by": "code", "words": words[:256]}
        elif name == "next_action":
            text = _text(move.get("text"), "text", 256)
            data["next_action"] = {"text": text, "done": False}
            if move.get("approved_draft"):
                if record.state != "open" or not self.questions:
                    raise _Refused("Draft approval needs its open review line", "approved_draft", "save the draft from the review card")
                detail = _text(move.get("details"), "details", 400)
                if len(text) > 120 or not text.isprintable() or not detail.isprintable():
                    raise _Refused("A draft needs a short title and one detail line", "details", "reply in the thread to shorten the draft")
                record = replace(record, body=record.body + ("\n\n" if record.body else "")
                                 + "Diablo's suggestion: " + text + " — " + detail)
            elif not _his_wording(text, message):
                marked = self.flag(replace(record, data=data), "Next action is the agent's wording", "text", mark="agents_wording")
                data = marked.data
        elif name == "date":
            record = self.date(record, move, message)
            data, state = record.data, record.state
        elif name == "wait":
            record = self.wait(record, move, message)
            data = record.data
        elif name == "end":
            state = move.get("ending")
            direct_drop = self.context.authenticated_owner_tap and self.change["message"] == "Drop"
            if state == "dropped" and not move.get("reason") and not direct_drop:
                record = self.flag(record, "Dropping needs his reason", "reason", dropped=True)
                data, state = record.data, record.state
            else:
                if state not in {"dropped", "no_longer_needed"}:
                    raise _Refused("Invalid ending", "ending", "send dropped or no_longer_needed")
                if "reason" in move:
                    data["reason"] = move["reason"]
                elif state == "dropped" and direct_drop:
                    # The frozen record contract requires a reason. Record the
                    # observed act, never an invented personal explanation.
                    data["reason"] = "Owner selected Drop."
        elif name == "park":
            event = move.get("event")
            until = move.get("until")
            if "until" not in move and not event:
                match = re.search(r"\b(someday|later|not now)\b", message, re.IGNORECASE)
                event = match.group() if match else "someday"
            elif isinstance(until, str) and until.casefold().strip() in {"someday", "later", "not now"}:
                event = until
            if event:
                if ("until" in move or "event" in move) and not timewords.from_words(event, message):
                    record = self.flag(record, "Park event is not in his words", "event", dropped=True)
                    data = record.data
                else:
                    state, data["parked_until"] = "parked", {"event": event}
            else:
                kind, value, flags = _read_date(data, {"when": move.get("until")}, actor=self.actor,
                    now=self.now, message=message, phrase_field="until")
                if not value or not value.get("day"):
                    record = self.flag(record, "Park until needs a day in his words", "until", dropped=True)
                    data = record.data
                else:
                    state, data["parked_until"] = "parked", value["day"]
        elif name == "organise":
            record = self.organise(record, move, message)
            data, state, title = record.data, record.state, record.title
        elif name == "stake":
            _stake(data, move, self.actor, bool(move.get("confirm")))
        draft = replace(record, data=data, state=state, title=title)
        self.publish(draft, previous, move)

    def details(self, fields: Mapping[str, Any], *, prefix: str = "") -> str:
        if "details" not in fields:
            return ""
        text = _text(fields["details"], prefix + "details", 8192)
        if (self.change["said_by"] != "owner" or self.change["came_by"] != "direct"
                or not _his_wording(text, self.change["message"])):
            raise _Refused("Details must come from his message", prefix + "details", "quote his details from message")
        return text

    def capture(self, move: Move, message: str) -> None:
        title = _text(move.get("title"), "title")
        other = self.change["said_by"] == "other" or self.change["came_by"] == "connected_source"
        data = {"level": "suggested" if other else move.get("level", "accepted"), "size": "task", "timing": {}}
        if "owed_to" in move:
            data["owed_to"] = move["owed_to"]
        if "repeat" in move:
            data["repeat"] = move["repeat"]
        if self.ordinal in self.context.capture_raise_with:
            data["raise_with"] = self.context.capture_raise_with[self.ordinal]
        _length(data, move, self.actor)
        record = Record(schema="brain.record/v2", id=self.ids[(self.ordinal, 0)], kind="item", title=title,
            state="open", created_at=self.now, updated_at=self.now, assertion=None, confidence=None,
            sensitivity="private", vaults=(), owners=(), entities=(), evidence=(), supersedes=(),
            dedupe_key=None, data=data, body=self.details(move) if "details" in move
            else self.context.capture_bodies.get(self.ordinal, ""))
        routine = "repeat" in move
        if routine and not is_routine(record):
            raise _Refused("repeat needs a known routine", "repeat", "send daily or weekly:<mon..sun>")
        if not _his_wording(title, message):
            record = self.flag(record, "Title is the agent's wording", "title", mark="agents_wording")
        title_words = _words(title) - FILLER_WORDS
        for existing in self.index.values():
            if existing.state in _CLOSED:
                continue
            shared = title_words & (_words(existing.title) - FILLER_WORDS)
            if len(shared) >= 2 or title_words and title_words <= _words(existing.title):
                record = self.flag(record, f"Maybe same as {existing.title}", "title")
                if not routine:
                    data = deepcopy(record.data)
                    _mark(data, {"mark": "maybe_same", "as": work.first_id(existing)})
                    record = replace(record, data=data)
        data = deepcopy(record.data)
        if not routine:
            if move.get("review"):
                _mark(data, {"mark": "question", "text": "Review this new thing"})
            if "label" in move:
                data["labels"] = [move["label"]]
            for field in ("unclear", "ask_stake"):
                if move.get(field):
                    _mark(data, {"mark": field})
            if move.get("looks_like"):
                _mark(data, {"mark": "looks_like", "kind": move["looks_like"]})
            if "stake" in move:
                _stake(data, move["stake"], self.actor, field_prefix="stake.")
        elif any(move.get(field) for field in ("label", "unclear", "ask_stake", "looks_like", "stake")):
            record = self.flag(record, "Routine flags are reply-only; work fields left off", "repeat")
        record = replace(record, data=data)
        if "when" in move:
            if routine:
                kind, value, flags = _read_date(data, move, actor=self.actor, now=self.now, message=message)
                for flag in flags:
                    record = self.flag(record, flag, "when", dropped=value is None)
                if value:
                    timing = deepcopy(record.data["timing"])
                    timing["resolved_from"] = {"words": value["words"], "today": value["read_on"]}
                    if value.get("day"):
                        timing["due"] = value["day"]
                    if value.get("at"):
                        timing["at"] = value["at"]
                    if value.get("start"):
                        timing["window"] = {"start": value["start"], "end": value["end"]}
                    data = deepcopy(record.data)
                    data["timing"] = timing
                    record = replace(record, data=data)
            else:
                record = self.date(record, move, message)
        if "after" in move:
            # An authenticated capture may bind its explicitly ordered earlier
            # additions without spending a second move on each dependency.
            record = self.wait(record, {"kind": "after", "after": move["after"]}, message)
        self.publish(record, None, move)

    def parts(self, parent: Record, move: Move, message: str) -> None:
        if is_routine(parent) or work.kind_of(parent, self.index) == "subtask":
            raise _Refused("A subtask or routine cannot hold parts", "parts", "put parts on a task or project")
        parts = move.get("parts")
        if not isinstance(parts, (list, tuple)) or not 1 <= len(parts) <= 20:
            raise _Refused("parts needs 1 to 20 parts", "parts", "send one to twenty named parts")
        approval = move.get("approved_draft")
        if approval:
            if parent.state != "open":
                raise _Refused("Draft steps need an open item", "parts", "open the current review item")
            if not self.questions:
                raise _Refused("Draft approval needs its open review line", "approved_draft", "save the draft from the review card")
        fid = work.first_id(parent)
        existing = [r for r in self.write.values() if r.data.get("parent") == fid]
        offset = max((r.data.get("order", 0) for r in existing), default=0)
        prior = None
        for number, part in enumerate(parts, 1):
            if not isinstance(part, Mapping) or set(part) - {"title", "details", "then", "done", "when", "reading", "event", "whose", "block", "rough_minutes", "length_is_nics"}:
                raise _Refused("Unknown part field", "parts", "use only the planned part fields")
            if approval and set(part) - {"title", "details", "then"}:
                raise _Refused("A draft approves step wording only", "parts", "set dates, people and lengths with separate owner answers")
            for field in ("then", "done", "length_is_nics"):
                if field in part and not isinstance(part[field], bool):
                    raise _Refused("Part's flag must be boolean", f"parts.{field}", "send true or false")
            title = _text(part.get("title"), "parts.title")
            if approval and (len(title) > 120 or not title.isprintable()):
                raise _Refused("A draft needs a short action title", "parts.title", "use one line of at most 120 characters")
            details = _text(part.get("details"), "parts.details", 400) if approval else self.details(part, prefix="parts.")
            if approval and not details.isprintable():
                raise _Refused("A draft needs one detail line", "parts.details", "use one detail line of at most 400 characters")
            data = {"level": "accepted", "size": "task", "timing": {}, "parent": fid, "order": offset + number}
            if part.get("whose"):
                if not timewords.from_words(part["whose"], message):
                    raise _Refused("Part's person is not in his words", "parts.whose", "quote the named person")
                data["whose"] = part["whose"]
            if part.get("then"):
                if prior is None:
                    raise _Refused("First part has no preceding part", "parts.then", "remove then from the first part")
                data["after"] = [prior]
            _length(data, part, self.actor, field_prefix="parts.")
            state = "done" if part.get("done") else "open"
            if state == "done":
                data["check"] = {"locator": "message", "checked_by": "nic"}
            record = replace(parent, id=self.ids[(self.ordinal, number)], title=title, state=state,
                             data=data, supersedes=(), evidence=(), source=None, links=(), dedupe_key=None,
                             body=details)
            if not approval and (not _his_wording(title, message) or not (self.questions or self.context.authenticated_owner_tap
                                                                        or _his_wording(parent.title, message))):
                record = self.flag(record, "Part or its parent is the agent's wording", "parts.title", mark="agents_wording")
            if "when" in part:
                record = self.date(record, part, message, field_prefix="parts.")
            record = self.publish(record, None, move, number)
            prior = work.first_id(record)
        if approval or any(question.get("marks") for question in self.questions):
            # The approved draft answers the parent's review line even when it
            # had no marks. Publish its unchanged facts plus tap provenance so
            # the ordinary door closes that answer and stale proofs stop matching.
            self.publish(parent, self.raw[parent.id], move)

    def wait(self, record: Record, move: Move, message: str) -> Record:
        data = deepcopy(record.data)
        kind = move.get("kind")
        if kind == "after":
            links = move.get("after")
            if not isinstance(links, (list, tuple)) or not 1 <= len(links) <= 20:
                raise _Refused("after needs 1 to 20 things", "after", "name the prerequisite things")
            data["after"] = list(dict.fromkeys(work.first_id(self.resolve(link, field="after")) for link in links))
        elif kind in {"theirs", "needs"}:
            who = _text(move.get("who"), "who", 128)
            if not timewords.from_words(who, message):
                return self.flag(record, "Wait's person is not in his words", "who", dropped=True)
            if kind == "theirs":
                data["whose"] = who
            else:
                data["needs"] = {"from": who}
                if "what" in move:
                    data["needs"]["what"] = move["what"]
            if "expected" in move:
                _, value, flags = _read_date(data, {"when": move["expected"], "reading": move.get("reading")},
                    actor=self.actor, now=self.now, message=message, phrase_field="expected")
                for flag in flags:
                    record = self.flag(replace(record, data=data), flag, "expected", dropped=value is None)
                    data = record.data
                if value and (value.get("day") or value.get("end")):
                    expected = {key: value[key] for key in ("words", "read_by", "read_on")}
                    expected["day"] = value.get("day") or value["end"]
                    if kind == "theirs":
                        data["expected"] = expected
                    else:
                        data["needs"]["expected"] = expected
        elif kind == "not_before":
            phrase_field = "until" if "until" in move or "event" not in move else "event"
            date_move = {"when": move.get(phrase_field), "event": move.get("event"), "reading": move.get("reading")}
            _, value, flags = _read_date(data, date_move, actor=self.actor, now=self.now,
                                       message=message, phrase_field=phrase_field)
            for flag in flags:
                record = self.flag(replace(record, data=data), flag, phrase_field, dropped=value is None)
                data = record.data
            if value:
                data.setdefault("dates", {})["not_before"] = value
                if value.get("day") and value.get("event"):
                    data["dates"]["planned"] = deepcopy(value)
        else:
            raise _Refused("Unknown wait kind", "kind", "send after, theirs, needs or not_before")
        return replace(record, data=data)

    def followup_disposition(self, record: Record, request: Mapping[str, Any], message: str) -> dict:
        """Owner-only transition of an active physical return, with generated history."""
        from brain_core.planning import active_followup_ref, required_followup, resource_conflicts, validate_planning, in_work_window, _instant
        from brain_core.schedule import eligible_at, effective_minutes
        def refuse(text):
            raise _Refused(text, "followup_disposition", "confirm the fresh required return, quoted reason and agreed replacement responsibility")
        if self.actor != "system:review-card" or not self.context.authenticated_owner_tap or self.change["said_by"] != "owner" or self.change["came_by"] != "direct":
            refuse("Follow-up disposition needs the authenticated owner review surface")
        if not isinstance(request, Mapping):
            refuse("Follow-up disposition must be a structured request")
        kind = request.get("kind")
        fields = {"kind", "from_ref", "reason"}
        if kind == "transfer":
            fields |= {"to_ref", "at", "assignee", "approved"}
        if not isinstance(kind, str) or kind not in {"transfer", "no_longer_required"} or set(request) != fields:
            refuse("Unknown follow-up disposition fields")
        operation = record.data.get("planning", {}).get("operation", {})
        active = required_followup(record, self.index)
        if not operation.get("actual_start") or not operation.get("approved") or not active:
            refuse("Only a confirmed started operation with an active return can change its obligation")
        if request["from_ref"] != active["item_ref"]:
            refuse("The required return has changed; this from_ref is stale")
        old = self.index.get(active["item_ref"])
        if old and old.state == "done":
            refuse("The required return is already completed")
        reason = _text(request.get("reason"), "followup_disposition.reason", 1024)
        if canonical_json(request) not in self.change["message"]:
            refuse("Disposition needs the exact approved request and quoted owner reason")
        data = deepcopy(record.data)
        spec = data["planning"]
        entry = {**dict(request), "change_id": self.change["operation_id"],
                 "recorded_at": self.now.isoformat(), "evidence": _evidence(reason, self.change["operation_id"])}
        if kind == "transfer":
            if request["approved"] is not True:
                refuse("Replacement responsibility has not been explicitly agreed")
            _text(request.get("assignee"), "followup_disposition.assignee", 128)
            try:
                due_matches = _instant(request["at"]) == _instant(active["at"])
            except (TypeError, ValueError):
                due_matches = False
            if not due_matches:
                refuse("A replacement keeps the original due instant")
            target = self.resolve(request["to_ref"], field="followup_disposition.to_ref")
            target_ref = work.first_id(target)
            source_ref = work.first_id(record)
            visited = {source_ref, operation["followup_ref"]}
            visited.update(old_entry.get("to_ref") for old_entry in spec.get("followup_dispositions", []))
            if target_ref in visited:
                refuse("A return transfer cannot revisit its source or prior targets")
            entry["to_ref"] = target_ref
            # Any operation already bound to this replacement, conditional or started,
            # is a conflicting obligation. Traverse outward to catch operation cycles.
            if any(work.first_id(source) != source_ref and active_followup_ref(source) == target_ref for source in self.index.values()):
                refuse("Replacement already belongs to another required return")
            cursor, seen = target_ref, set()
            while cursor is not None:
                if cursor == source_ref or cursor in seen:
                    refuse("Required-return transfer would create a cycle")
                seen.add(cursor)
                cursor = active_followup_ref(self.index.get(cursor))
            duration = target.data.get("timing", {}).get("duration_minutes")
            if type(duration) is not int or duration <= 0 or target.data.get("material_kind"):
                refuse("Replacement must be actionable work with a positive estimate")
            spec.setdefault("followup_dispositions", []).append(entry)
            candidate = {**self.index, source_ref: replace(record, data=data)}
            if not work.is_up(target_ref, candidate, self.now) or work.has_unfinished_prerequisites(target_ref, candidate):
                refuse("Replacement must be open and ready with its requirements satisfied")
            due = _instant(active["at"])
            if not eligible_at(target_ref, candidate, due):
                refuse("Replacement is not eligible at the original due time")
            cursor = target_ref
            end = due + timedelta(minutes=effective_minutes(target))
            while cursor in candidate:
                constrained = candidate[cursor]
                if not in_work_window(constrained, due, end):
                    refuse("Replacement cannot fit its recorded work window at the original due time")
                cursor = constrained.data.get("parent")
            if any(target.id in {conflict["left"], conflict["right"]} for conflict in resource_conflicts(candidate.values())):
                refuse("Replacement has conflicting resource occupancy")
        else:
            spec.setdefault("followup_dispositions", []).append(entry)
        try:
            validate_planning(spec)
        except (KeyError, ValueError, TypeError) as exc:
            refuse(str(exc))
        return data

    def organise(self, record: Record, move: Move, message: str) -> Record:
        lengths = [question for question in self.questions if question.get("length_choices")]
        if lengths and "rough_minutes" in move:
            if not move.get("length_is_nics") or not any(_accepted_review_length(
                    question, move["rough_minutes"], self.change["message"]) for question in lengths):
                raise _Refused("Length needs your chosen duration", "rough_minutes",
                               "choose a shown length or say how long it takes; Skip saves nothing")
        if any(question.get("tidy_suggestion") for question in self.questions):
            for field in ("title", "label"):
                if field in move and not _tidy_owner_wording(move[field], self.change["message"]):
                    if not any(_accepted_tidy(question, field, move[field], self.change["message"]) for question in self.questions):
                        raise _Refused("This tidy suggestion needs your Save", field, "tap Save on this suggestion or type your own wording")
        if "title" in move and not _his_wording(move["title"], self.change["message"]):
            if not any(_accepted_name(question, move["title"], self.change["message"]) for question in self.questions):
                return self.flag(record, "Title needs your words or yes to this line's suggested name", "title", dropped=True)
        data = deepcopy(record.data)
        if any(key in move for key in ("review_outcome", "occurrence_outcome", "remaining_minutes", "clear_wait")):
            if self.change["said_by"] != "owner" or self.change["came_by"] != "direct":
                raise _Refused("Review outcomes need your direct answer", "review_outcome", "answer from the authenticated surface")
            if "review_outcome" in move or "occurrence_outcome" in move:
                try:
                    day = date.fromisoformat(move.get("review_day", ""))
                except (ValueError, TypeError):
                    raise _Refused("Review outcome needs its actual date", "review_day", "send an ISO calendar date")
                if day > (self.now + timedelta(hours=14)).date():
                    raise _Refused("An outcome cannot be in the future", "review_day", "select the actual commitment date")
                result = move.get("review_outcome")
                if result is not None and result not in {"progress", "not_done", "happened", "not_happened", "canceled", "no_show"}:
                    raise _Refused("Unknown review outcome", "review_outcome", "send a supported explicit dated outcome")
                if record.state in _CLOSED:
                    raise _Refused("Closed work needs an explicit correction", "review_outcome", "reopen the affected work first")
                if result == "progress" and not move.get("note"):
                    raise _Refused("Progress needs what happened", "note", "say where you got to")
                if "occurrence_outcome" in move:
                    from brain_core.routines import occurs_on
                    result = move["occurrence_outcome"]
                    if not is_routine(record) or not occurs_on(record, day) or result not in {"done", "skipped", "missed", "canceled"}:
                        raise _Refused("Outcome needs a real routine occurrence", "occurrence_outcome", "select the occurrence date and supported outcome")
                    data["occurrence"] = {"day": day.isoformat(), "outcome": result}
                entry = {"day": day.isoformat(), "outcome": result, "recorded_at": self.now.isoformat()}
                if move.get("note"):
                    entry["text"] = move["note"]
                data.setdefault("review_outcomes", []).append(entry)
            if "remaining_minutes" in move:
                minutes = move["remaining_minutes"]
                if type(minutes) is not int or minutes <= 0 or move.get("review_outcome") != "progress":
                    raise _Refused("Remaining effort needs positive minutes with progress", "remaining_minutes", "send the remaining whole minutes")
                if not any(_accepted_review_length({"length_choices": [minutes]}, minutes, match.group()) for match in _REVIEW_LENGTH.finditer(self.change["message"])):
                    raise _Refused("Remaining effort needs your stated duration", "remaining_minutes", "say the remaining duration")
                data["remaining_minutes"] = minutes
                data.setdefault("timing", {}).update(duration_minutes=minutes, padded_minutes=minutes, estimated_by="nic")
            if move.get("clear_wait"):
                if not move.get("note") or not any(key in data for key in ("needs", "whose", "expected")):
                    raise _Refused("Arrival needs the existing wait and your confirmation", "clear_wait", "say what has arrived")
                for key in ("needs", "whose", "expected"):
                    data.pop(key, None)

        if "followup_disposition" in move:
            if set(move) - {"move", "thing", "words", "followup_disposition"}:
                raise _Refused("Follow-up disposition must be a dedicated action", "followup_disposition", "approve only this transition")
            data = self.followup_disposition(record, move["followup_disposition"], message)

        if "planning" in move:
            from brain_core.planning import validate_planning
            try:
                validate_planning(move["planning"])
            except (ValueError, TypeError, KeyError) as exc:
                raise _Refused(str(exc), "planning", "confirm the exact operation, resource occupancy and ready portions")
            if canonical_json(move["planning"]) not in self.change["message"]:
                raise _Refused("Planning needs your explicit approval of these fields", "planning", "confirm the shown operation and constraints")
            previous = data.get("planning", {}).get("operation", {})
            if previous.get("actual_start") and move["planning"].get("operation") != previous:
                raise _Refused("Started operation retains its required follow-up", "planning", "complete the obligation or explicitly transfer responsibility")
            operation = move["planning"].get("operation")
            if operation and operation.get("actual_start"):
                from brain_core.planning import _instant
                if _instant(operation["actual_start"]) > self.now:
                    raise _Refused("Actual startup cannot be in the future", "planning.operation.actual_start", "record the confirmed time it really started")
            if operation:
                target = self.resolve(operation["followup_ref"], field="planning.operation.followup_ref")
                if work.first_id(target) == work.first_id(record):
                    raise _Refused("An operation needs a separate follow-up", "planning.operation.followup_ref", "name the required follow-up item")
            proposed=deepcopy(move["planning"])
            if operation:
                proposed["operation"]["followup_ref"] = work.first_id(target)
            for block in proposed.get("shared_blocks", []):
                block["task_refs"] = [work.first_id(self.resolve(ref,field="planning.shared_blocks.task_refs")) for ref in block["task_refs"]]

            previous_planning=data.get("planning", {})
            for preserved in ("portion_selections", "portion_history", "followup_dispositions"):
                if preserved in proposed and proposed[preserved] != previous_planning.get(preserved):
                    raise _Refused("Canonical answer and disposition history use their governed actions", "planning", "use the explicit portion answer or selection")
                if preserved in previous_planning:
                    proposed[preserved]=deepcopy(previous_planning[preserved])
            old_portions={part["id"]:part for part in previous_planning.get("portions", [])}
            for part in proposed.get("portions", []):
                old=old_portions.get(part["id"])
                if old and (part["quantity"] != old["quantity"] or part.get("done_quantity",0) != old.get("done_quantity",0)):
                    raise _Refused("An identified portion retains its quantity and outcome history", "planning.portions", "use the portion outcome or explicit correction action")
            if any(ref not in {part["id"] for part in proposed.get("portions",[])} for ref in old_portions):
                raise _Refused("Existing identified portions cannot silently disappear", "planning.portions", "retain the existing portion identities")
            data["planning"] = proposed

        if "material_kind" in move:
            kind = move["material_kind"]
            if not isinstance(kind, str) or kind not in {"information", "reflection", "idea", "arrangement"}:
                raise _Refused("Unknown retained material kind", "material_kind", "choose information, reflection, idea or arrangement")
            if not _his_wording(kind, message):
                raise _Refused("Retaining material needs your classification choice", "material_kind", "confirm the shown material kind")
            if record.state in _CLOSED or data.get("called") or data.get("repeat"):
                raise _Refused("This structured or ended work needs an explicit correction", "material_kind", "retain a capture item rather than reclassifying a project or series")
            data["material_kind"] = kind
            data.pop("review_marks", None)
        if "requirements" in move:
            requirements = move["requirements"]
            if not isinstance(requirements, Mapping) or set(requirements) != {"remove", "add"} or any(not isinstance(requirements[field], list) for field in ("remove", "add")):
                raise _Refused("Requirement change needs the exact removals and replacements", "requirements", "confirm which predecessor requirements change")
            if canonical_json(requirements) not in message:
                raise _Refused("Requirement changes need your explicit approval", "requirements", "approve the shown affected task and predecessor changes")
            existing = data.get("after", [])
            remove=[]
            for ref in requirements["remove"]:
                resolved=work.first_id(self.resolve(ref, field="requirements.remove"))
                if resolved not in existing or resolved in remove:
                    raise _Refused("Only existing predecessor requirements can be removed", "requirements.remove", "choose the current requirement on this task")
                remove.append(resolved)
            add=[]
            for ref in requirements["add"]:
                resolved=work.first_id(self.resolve(ref, field="requirements.add"))
                if resolved == work.first_id(record) or resolved in add:
                    raise _Refused("Replacement prerequisites must be distinct other work", "requirements.add", "choose valid predecessor tasks")
                add.append(resolved)
            predecessors = list(dict.fromkeys([ref for ref in existing if ref not in remove] + add))
            if predecessors:
                data["after"] = predecessors
            else:
                data.pop("after", None)
        if "portion_outcomes" in move:
            outcomes = move["portion_outcomes"]
            try:
                outcome_day = date.fromisoformat(move.get("review_day", ""))
            except (ValueError, TypeError):
                raise _Refused("Portion outcomes need their actual date", "review_day", "send the occurrence day")
            if not isinstance(outcomes, list) or not outcomes or canonical_json(outcomes) not in message:
                raise _Refused("Portion outcomes need explicit quantity approval", "portion_outcomes", "confirm each identified portion and finished quantity")
            portions = {part["id"]: part for part in data.get("planning", {}).get("portions", [])}
            seen=set()
            for outcome in outcomes:
                if not isinstance(outcome, Mapping) or set(outcome)-{"id", "done_quantity", "text", "correct"} or not isinstance(outcome.get("id"), str) or outcome["id"] not in portions or outcome["id"] in seen:
                    raise _Refused("Outcome needs a distinct existing portion identity", "portion_outcomes", "select the existing identified portions")
                seen.add(outcome["id"])
                part=portions[outcome["id"]]
                count=outcome.get("done_quantity")
                if type(count) is not int or not 0 <= count <= part["quantity"] or type(outcome.get("correct", False)) is not bool:
                    raise _Refused("Finished portion quantity is outside its obligation", "portion_outcomes", "give the cumulative finished whole quantity")
                if count < part.get("done_quantity", 0) and not outcome.get("correct"):
                    raise _Refused("Reducing finished quantity needs an explicit correction", "portion_outcomes", "confirm the previously finished output that became invalid")
                part["done_quantity"] = count
                entry={"day":outcome_day.isoformat(), "id":outcome["id"], "done_quantity":count,"corrected":outcome.get("correct",False)}
                if "text" in outcome:
                    entry["text"]=_text(outcome["text"], "portion_outcomes.text", 8192)
                data["planning"].setdefault("portion_history", []).append(entry)
        if "portion_selection" in move:
            from brain_core.planning import ready_portions
            selection=move["portion_selection"]
            if not isinstance(selection, Mapping) or set(selection) != {"day", "ids"} or not isinstance(selection["ids"], list) or not selection["ids"] or any(not isinstance(ref,str) for ref in selection["ids"]) or len(set(selection["ids"])) != len(selection["ids"]) or canonical_json(selection) not in message:
                raise _Refused("Portion selection needs your exact dated group approval", "portion_selection", "approve only the shown currently ready portion identities")
            try:
                date.fromisoformat(selection["day"])
            except (ValueError, TypeError):
                raise _Refused("Portion selection needs a valid day", "portion_selection.day", "select the local planning day")
            ready={part["id"]:part for part in ready_portions(replace(record,data=data),self.now)}
            if any(ref not in ready for ref in selection["ids"]):
                raise _Refused("Some selected portions are not currently verified ready", "portion_selection.ids", "refresh and choose only currently ready portions")
            saved={"day":selection["day"],"portions":[{"id":ref,"quantity":ready[ref]["remaining_quantity"]} for ref in selection["ids"]]}
            selections=data["planning"].setdefault("portion_selections", [])
            selections[:]=[old for old in selections if old["day"] != selection["day"]]
            selections.append(saved)

        old_finish = data.get("finish_line")
        state, title = record.state, record.title
        if "note" in move:
            note = _text(move["note"], "note", 8192)
            if note not in self.change["message"]:
                raise _Refused("Note must be exact text from your message", "note", "submit the text you typed")
            record = replace(record, body=record.body + ("\n" if record.body else "") + note)
        if move.get("reopen"):
            if state != "done":
                raise _Refused("Only a done thing can be unticked", "reopen", "refresh this thing before changing it")
            state = "open"
            data.pop("check", None)
            data.get("dates", {}).pop("done", None)
        if move.get("can_do_now"):
            if state not in {"open", "parked"}:
                raise _Refused("Only an open or parked thing can be made available", "can_do_now",
                               "refresh this thing before changing it")
            state = "open"
            data.pop("parked_until", None)
            data.get("dates", {}).pop("planned", None)
        if "called" in move:
            if move["called"] == "task":
                data.pop("called", None)
            else:
                data["called"] = move["called"]
        if "under" in move:
            if move["under"] == "none":
                data.pop("parent", None)
            else:
                parent = self.resolve(move["under"], field="under")
                data["parent"] = work.first_id(parent)
                if not _his_wording(parent.title, message):
                    record = self.flag(replace(record, data=data), "Parent is not named in his words", "under", mark="agents_wording")
                    data = record.data
        if "label" in move:
            data["labels"] = [move["label"]]
        for field in ("order", "finish_line"):
            if field in move:
                data[field] = move[field]
        if "finish_line" in move and not timewords.from_words(move["finish_line"], message):
            record = self.flag(replace(record, data=data), "Finish line is not in his words", "finish_line", dropped=True)
            data = record.data
            if old_finish is not None:
                data["finish_line"] = old_finish
            else:
                data.pop("finish_line", None)
        if "title" in move:
            title = move["title"]
        _length(data, move, self.actor)
        if move.get("accept"):
            data["level"] = "accepted"
            suggested = data.get("dates", {}).get("suggested")
            if suggested:
                if suggested["kind"] == "appointment" and not suggested.get("at"):
                    record = self.flag(replace(record, data=data), "Suggested appointment needs its time", "accept", dropped=True)
                    data = record.data
                else:
                    accepted = {key: value for key, value in suggested.items() if key not in {"kind", "speaker"}}
                    data["dates"].pop("suggested")
                    data["dates"][suggested["kind"]] = accepted
        if "keep" in move:
            keep = move["keep"]
            if keep == "date":
                for value in data.get("dates", {}).values():
                    if value.get("read_by") != "code" or value.get("confirmed") is False:
                        value["confirmed"] = True
                data["review_marks"] = [mark for mark in data.get("review_marks", [])
                                        if not (mark["mark"] == "question" and mark.get("text", "").startswith("Keep '"))]
            else:
                data["review_marks"] = [mark for mark in data.get("review_marks", []) if mark["mark"] != keep]
        if self.questions:
            # Only the marks addressed by this offered answer are cleared.
            data["review_marks"] = _remaining_marks(data.get("review_marks", []), self.questions, move)
        if "merge_into" in move:
            target = self.resolve(move["merge_into"], field="merge_into")
            if work.first_id(target) == work.first_id(record):
                raise _Refused("Cannot merge a thing into itself", "merge_into", "name another thing")
            state, data["reason"] = "no_longer_needed", f"merged into {target.title}"
            children = [r for r in self.write.values() if r.data.get("parent") == work.first_id(record) and r.state not in _CLOSED]
            for number, child in enumerate(children, 1):
                _require_supported_repeat(child)
                child_data = deepcopy(child.data)
                child_data["parent"] = work.first_id(target)
                self.publish(replace(child, data=child_data), self.raw[child.id], move, number)
        return replace(record, data=data, state=state, title=title)

    def undo(self, move: Move, *, authorised: bool) -> None:
        operation = _text(move.get("change"), "change", 128)
        ids = self.context.undo_changes.get(operation)
        if not ids or len(set(ids)) != len(ids):
            raise _Refused("Undo needs this change's exact planned IDs", "change",
                           "supply the original caller-scoped planned IDs; send a correction as a new change if unavailable")
        targets = []
        # Verify every record and current version before emitting anything.
        for record_id in ids:
            version = self.context.raw_records.get(record_id)
            if version is None or (version.source or {}).get("recorded_by") != self.actor:
                raise _Refused("Undo record is missing or belongs to another caller", "change", "send the correction as a new change")
            current = self.write.get(work.first_id(version))
            if current is None or current.id != record_id:
                raise _Refused("An undo target has changed since", "change", "send the correction as a new change")
            _require_supported_repeat(current)
            prior = None
            if version.supersedes:
                prior = self.context.raw_records.get(version.supersedes[-1])
                if prior is None:
                    raise _Refused("Undo needs the exact archived prior version", "_context.raw_records",
                                   "archive-read the last superseded ID")
                _require_supported_repeat(prior)
            targets.append((version, current, prior))
        candidate = dict(self.write)
        for version, current, prior in targets:
            offered = any(answer["thing"] == work.first_id(version) and _offered(answer, move)
                          for answer in self.answers)
            if not authorised and not offered:
                continue
            candidate[work.first_id(current)] = _undo_draft(current, prior)
        self.protect_obligations(candidate, field="change")
        for slot, (version, current, prior) in enumerate(targets):
            self.questions = tuple(answer for answer in self.answers if answer["thing"] == work.first_id(version) and _offered(answer, move))
            if not authorised and not self.questions:
                draft = self.flag(current, "Undo needs a non-bare quote or an offered review answer", "words", dropped=True)
            else:
                draft = _undo_draft(current, prior)
            self.publish(draft, version, move, slot, restored=prior if authorised or self.questions else None,
                         observation=not authorised and not self.questions, obligations_checked=True)


def _undo_draft(current, prior):
    if prior is None:
        return replace(current, data={**deepcopy(current.data), "reason": "undone"}, state="no_longer_needed")
    return replace(current, title=prior.title, state=prior.state, data=deepcopy(prior.data),
                   links=deepcopy(prior.links), body=prior.body)


def _evidence(words: str, reference: str) -> dict[str, str]:
    from hashlib import sha256
    return {"source_type": "session_message", "locator": reference,
            "content_sha256": sha256(words.encode("utf-8")).hexdigest(), "excerpt": words}


def _offered(answer: ValidatedAnswer, move: Move) -> bool:
    offered = set(answer["offered"])
    name = move["move"]
    if name == "organise":
        fields = _OWNER_ORGANISE.intersection(move)
        if answer.get("tidy_suggestion") and "label" in move:
            fields = fields | {"label"}
        if move.get("length_is_nics"):
            fields = fields | {"rough_minutes"}
        aliases = {"merge_into": {"merge", "merge_into"}, "title": {"title", "rename"},
                   "called": {"called", move.get("called", "")}, "rough_minutes": {"rough_minutes", "length"},
                   "block": {"block"}, "accept": {"accept"}, "keep": {"keep"}, "label": {"label"}}
        return bool(fields) and all("organise" in offered or bool(aliases[key] & offered) for key in fields)
    aliases = {"tick": {"tick", "done", "done already"}, "end": {"end", "drop", "dropped", "no_longer_needed"},
               "date": {"date", "move", "today", "another day", "another_day"}, "parts": {"parts", "add_parts"},
               "stake": {"stake", "confirm"}}
    return bool(aliases.get(name, {name}) & offered)


def _enveloped_mapping(mapping: dict[str, Any], envelope: RecordEnvelope | None) -> dict[str, Any]:
    """Prepare authenticated existing metadata identically on first save and replay."""
    if envelope is not None:
        mapping = deepcopy(mapping)
        mapping.update(vaults=list(envelope["vaults"]), owners=list(envelope["owners"]))
        for evidence in mapping["evidence"]:
            if not evidence.get("actor"):
                evidence["actor"] = envelope["evidence_actor"]
    return mapping


def _validate(change: Change, context: ExecutionContext, actor: str, now: datetime) -> None:
    if now.tzinfo is None:
        raise _Refused("now must carry a timezone", "now", "supply the hub's accepted timezone-aware clock")
    if not isinstance(actor, str) or not actor:
        raise _Refused("An authenticated actor is required", "actor", "supply the authenticated caller")
    if actor.startswith("system:") and actor not in context.allowed_code_callers:
        raise _Refused("Unknown hub code caller", "actor", "use an explicitly registered hub code caller", code="ACCESS_DENIED")
    if type(context.authenticated_owner_tap) is not bool or (context.authenticated_owner_tap
            and (actor not in context.allowed_code_callers or not actor.startswith("system:")
                 or change.get("said_by") != "owner" or change.get("came_by") != "direct")):
        raise _Refused("Owner tap needs a registered authenticated surface", "_context.authenticated_owner_tap",
                       "bind the exact owner action at the host boundary", code="ACCESS_DENIED")
    envelope = context.record_envelope
    if envelope is not None:
        if (not isinstance(envelope, Mapping) or set(envelope) != {"vaults", "owners", "evidence_actor"}
                or envelope.get("evidence_actor") != actor
                or any(not isinstance(envelope.get(key), (list, tuple))
                       or any(not isinstance(value, str) or not value for value in envelope[key])
                       for key in ("vaults", "owners"))):
            raise _Refused("Record envelope must come from this authenticated caller", "_context.record_envelope",
                           "supply authenticated vaults, owners and caller evidence actor", code="ACCESS_DENIED")
    if not isinstance(context.operation_hash, str) or re.fullmatch(r"[0-9a-f]{64}", context.operation_hash) is None:
        raise _Refused("Missing canonical public change hash", "_context.operation_hash", "have W12 hash the public change excluding _context")
    if not isinstance(change, Mapping) or set(change) - set(Change.__annotations__):
        raise _Refused("Unknown change field", "change", "send only the planned public fields and internal hub context")
    _text(change.get("operation_id"), "operation_id", 128)
    _text(change.get("message"), "message")
    if change.get("said_by") not in {"owner", "other"} or change.get("came_by") not in {"direct", "connected_source"}:
        raise _Refused("Unknown source kind", "said_by/came_by", "send owner/other and direct/connected_source")
    if change["said_by"] == "other" or change["came_by"] == "connected_source":
        _text(change.get("speaker"), "speaker", 128)
    moves = change.get("moves")
    if not isinstance(moves, (list, tuple)) or not 1 <= len(moves) <= 20:
        raise _Refused("A change needs 1 to 20 moves", "moves", "split the change into batches of one to twenty moves")
    for ordinal, move in enumerate(moves):
        private = _SURFACE_FIELDS.get(move.get("move"), set()) if isinstance(move, Mapping) else set()
        allowed_private = private if context.authenticated_owner_tap else set()
        if not isinstance(move, Mapping) or move.get("move") not in _MOVES or set(move) - (_FIELDS.get(move.get("move"), set()) | allowed_private | {"move", "thing", "words"}):
            raise _Refused("Unknown move or field", f"moves.{ordinal}", "use one of the twelve moves and its planned fields")
        if "appointment_timezone" in move and (actor not in context.allowed_code_callers
                or move.get("kind") != "appointment" or change["said_by"] != "owner"
                or change["came_by"] != "direct" or "replaces" in move or "reading" in move):
            raise _Refused("Typed appointment requires an authenticated appointment answer", "appointment_timezone",
                           "submit the appointment from the owner review card")
        if "kind" in move and move["move"] == "date" and actor not in context.allowed_code_callers:
            raise _Refused("Only a registered hub code caller may name a date kind", f"moves.{ordinal}.kind", "send when; code reads its kind")
        for key in ("length_is_nics", "unclear", "ask_stake", "proposed", "confirm", "accept", "reopen", "can_do_now", "review", "unpark", "clear_wait"):
            if key in move and not isinstance(move[key], bool):
                raise _Refused(f"{key} must be boolean", f"moves.{ordinal}.{key}", "send true or false")
        if "unpark" in move and move.get("kind") != "planned":
            raise _Refused("Unparking with a day requires the planned date kind", f"moves.{ordinal}.unpark",
                           "submit the planned-day action from the authenticated surface")
        if "approved_draft" in move:
            approval = move["approved_draft"]
            if (not isinstance(approval, Mapping) or set(approval) != {"request_id", "sha256", "author"}
                    or approval.get("author") != "Diablo"
                    or not isinstance(approval.get("request_id"), str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", approval["request_id"]) is None
                    or not isinstance(approval.get("sha256"), str)
                    or re.fullmatch(r"[a-f0-9]{64}", approval["sha256"]) is None):
                raise _Refused("Draft approval needs its exact accepted reply", "approved_draft",
                               "save the latest Diablo draft from the review card")
        if move["move"] == "next_action" and "details" in move and "approved_draft" not in move:
            raise _Refused("Draft detail needs its accepted reply", "details", "save the latest Diablo draft from the review card")
        if "looks_like" in move and move["looks_like"] not in {"goal", "project"}:
            raise _Refused("looks_like names a goal or project", f"moves.{ordinal}.looks_like", "send goal or project")
        if "reading" in move and not isinstance(move["reading"], Mapping):
            raise _Refused("reading must be a date mapping", f"moves.{ordinal}.reading", "send day and optional at/end")
        if "stake" in move:
            if not isinstance(move["stake"], Mapping) or set(move["stake"]) - {"level", "at_stake", "odds", "cost_shape"}:
                raise _Refused("Capture stake must be an unconfirmed proposal", f"moves.{ordinal}.stake", "send only level, at_stake, odds and cost_shape")
    bodies = context.capture_bodies
    if not isinstance(bodies, Mapping) or any(
        type(ordinal) is not int or not 0 <= ordinal < len(moves)
        or moves[ordinal]["move"] != "new" or not isinstance(body, str)
        for ordinal, body in bodies.items()
    ):
        raise _Refused("Capture bodies require exact new-move ordinals and text", "_context.capture_bodies",
                       "supply each existing bridge body only for its corresponding new move")
    raise_with = context.capture_raise_with
    if not isinstance(raise_with, Mapping) or any(
        type(ordinal) is not int or not 0 <= ordinal < len(moves)
        or moves[ordinal]["move"] != "new" or not isinstance(value, str) or not value.strip()
        for ordinal, value in raise_with.items()
    ):
        raise _Refused("Capture raise_with requires exact new-move ordinals and nonempty text", "_context.capture_raise_with",
                       "supply existing bridge raise_with text only for its corresponding new move")
    planned = tuple(context.planned_ids.values())
    if len(set(planned)) != len(planned):
        raise _Refused("Planned IDs must be distinct", "_context.planned_ids", "supply a distinct caller-scoped ID for every position")
    for fid, record in context.write_index.items():
        if fid != work.first_id(record) or record.id not in context.raw_records:
            raise _Refused("Write index lacks its exact raw provenance", "_context.raw_records", "supply every current raw version by exact ID")
    referenced = tuple(change.get("answers", ()))
    questions = {answer["reference"]: answer for answer in context.answer_context}
    if len(questions) != len(context.answer_context) or len(set(referenced)) != len(referenced) or set(referenced) != set(questions):
        raise _Refused("Answers require their exact validated contexts", "answers", "supply one validated context for each answer reference")
    today = now.astimezone(timewords.SINGAPORE).date()
    for answer in context.answer_context:
        try:
            day = date.fromisoformat(answer["day"])
        except (TypeError, ValueError):
            raise _Refused("Answer day must be a date", "answers", "validate the line's calendar day in W12") from None
        if not (day == today or (day.toordinal() == today.toordinal() - 1 and answer["open"] is True)):
            raise _Refused(f"Review line {answer['reference']} is stale", "answers", "answer today's line or yesterday's still-open line")
        if answer["thing"] not in context.write_index:
            raise _Refused(f"Review line {answer['reference']} names an unknown target", "answers", "answer the line for an existing thing")


def _replay_context(context: ExecutionContext, actor: str) -> tuple[ExecutionContext, dict[str, Record]]:
    """Reconstruct only supplied exact pre-change versions to verify a retry."""
    found = {record_id: context.raw_records[record_id] for record_id in context.planned_ids.values()
             if record_id in context.raw_records}
    if not found:
        return context, found
    prior = {fid: context.raw_records[record.id] for fid, record in context.write_index.items()}
    for record in found.values():
        if (record.source or {}).get("recorded_by") != actor or record.dedupe_key != context.operation_hash:
            raise _Refused("Operation ID already has different content or caller", "operation_id", "use a new operation ID for different moves", code="brain.conflict")
        fid = work.first_id(record)
        if record.supersedes:
            previous = context.raw_records.get(record.supersedes[-1])
            if previous is None:
                raise _Refused("Retry needs the exact prior version", "_context.raw_records", "archive-read the last superseded ID")
            prior[fid] = previous
        else:
            prior.pop(fid, None)
    return replace(context, write_index=work.write_index(prior.values())), found


def apply_change(index: WorkIndex, change: Change, *, actor: str, now: datetime) -> Outcome:
    """Build one atomic change against planned reader/check_change interfaces.

    Resolve first IDs, preserve whole history, enforce owner quotes and answer
    context, flag/drop uncertain moves and refuse broken shapes or >25 records.
    Unknown system callers are refused. Routines retain their old record shape.
    """
    flags: tuple[Mapping[str, Any], ...] = ()
    try:
        context = change.get("_context")
        if not isinstance(context, ExecutionContext):
            raise _Refused("This change needs authenticated execution context", "_context", "have W12 supply raw versions, own write index, exact planned IDs and operation hash")
        _validate(change, context, actor, now)
        context, found = _replay_context(context, actor)
        builder = _Builder(work.index_things(context.write_index.values()) if found else index,
                           change, actor, now, context)
        # All new IDs and explicit captures exist before references are resolved;
        # #n can therefore name a later new move without a fabricated placeholder.
        ordered = [(i, move) for i, move in enumerate(change["moves"]) if move["move"] == "new"]
        ordered += [(i, move) for i, move in enumerate(change["moves"]) if move["move"] != "new"]
        for ordinal, move in ordered:
            builder.ordinal = ordinal
            builder.apply(move)
        flags = tuple(builder.flags)
        if found:
            expected = {record.id: record for record in builder.records}
            if expected.keys() != found.keys():
                raise _Refused("Only some exact planned records exist", "operation_id", "inspect the partial publication; nothing is patched up", code="brain.record.exists")
            if any(expected[key].sha256 != found[key].sha256 for key in expected):
                raise _Refused("Operation ID already has different moves", "operation_id", "use a new operation ID", code="brain.conflict")
        reply_index = work.index_things([*context.raw_records.values(), *builder.records])
        reply = _reply(reply_index, builder.records, builder.flags, now, change["operation_id"], change["moves"])
        if found:
            reply["status"] = "duplicate"
        return Outcome(tuple(builder.records), flags, None, reply)
    except _Refused as error:
        return Outcome((), flags, error.detail, {})
