"""The work-model reader (plan 6.1): pure functions over supplied item records.

The index maps lasting first IDs to current Records with old fields mapped into
the work shape. No storage, ranking, moves, model calls or host dependencies.
All references accepted by readers resolve through the supplied index.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, NotRequired, TypedDict

from brain_core.errors import BrainError, ErrorCode
from brain_core.presentation import relative_day_words, stake_words
from brain_core.records import _RECORD_ID, WORK_FIELDS, Record, record_from_mapping
from brain_core.planning import active_followup_ref, followup_triggered

Thing = Record | str
WorkIndex = Mapping[str, Record]
History = Iterable[Any]  # why-log rows: mappings or objects with item_ref/rule/surfaced_at
Now = date | datetime
_SGT = timezone(timedelta(hours=8))
_ENDED = {"done", "dropped", "no_longer_needed"}


class Explanation(TypedDict):
    status: str  # up, open (container), waiting, parked, ended
    reason: str
    until: str | None


class Next(TypedDict):
    item_ref: str
    text: str
    status: str
    reason: str
    until: str | None
    children: tuple[Next, ...]  # a goal has each child's own next


class Progress(TypedDict):
    done: int
    total: int
    text: str
    started: bool
    last_ticked: str | None
    last_ticked_at: str | None
    last_ticked_day: str | None


class Need(TypedDict):
    kind: str
    item_ref: str
    text: str
    count: int
    allowed_answers: tuple[str, ...]
    answer_kind: NotRequired[str]  # opt-in transient provenance, never a wire/stored field


class _ReaderIndex(dict[str, Record]):
    """Transient own-fact provenance; never a stored record/data field."""

    def __init__(self, effective: Mapping[str, Record], own: Mapping[str, Record], versions: Iterable[Record] = ()):
        super().__init__(effective)
        self._own = own
        self._versions = {r.id: r for r in versions}


def index_things(items: Iterable[Record]) -> dict[str, Record]:
    """Collapse whole supersedes histories, then map both shapes without mutation.

    Keys are first IDs; parent/after/mark references resolve to first IDs.
    Returned records preserve source/provenance and carry mapped work data.
    """
    items = tuple(items)
    mapped = _own_index(items, for_write=False)
    # Effective facts are read-only projections; never serialize them as a move.
    effective: dict[str, Record] = {}

    def inherit(key: str, seen: frozenset[str]) -> Record:
        if key in effective:
            return effective[key]
        if key in seen:
            raise ValueError("work parent cycle")
        r = mapped[key]
        data = deepcopy(r.data)
        parent = data.get("parent")
        if parent in mapped:
            above = inherit(parent, seen | {key}).data
            if "labels" not in data and "labels" in above:
                data["labels"] = deepcopy(above["labels"])
            if not data.get("review_marks"):
                if "stake" not in data and "stake" in above:
                    data["stake"] = deepcopy(above["stake"])
                if "deadline" not in data.get("dates", {}) and "deadline" in above.get("dates", {}):
                    data.setdefault("dates", {})["deadline"] = deepcopy(above["dates"]["deadline"])
        effective[key] = replace(r, data=data)
        return effective[key]

    for key in mapped:
        inherit(key, frozenset())
    return _ReaderIndex(dict(sorted(effective.items())), mapped, items)


def write_index(items: Iterable[Record]) -> dict[str, Record]:
    """Return current first-ID Records mapped for writes, with own facts only.

    Shares index_things' collapse and old-shape mapping, without inheritance.
    Clears translated legacy fields/links/states and preserves unrelated links
    and provenance. Routines retain their old shape. Results are parser-valid;
    callers still keep raw Records separately for check_change and create the
    new version/history themselves. This function performs no write or IO.
    """
    return _own_index(items, for_write=True)


def first_id(thing: Record) -> str:
    """Return supersedes[0], or the record's own ID when it has no history."""
    return thing.supersedes[0] if thing.supersedes else thing.id


def kind_of(thing: Thing, index: WorkIndex) -> str:
    """Return goal, project, task or subtask from the current shape and parent."""
    r = _record(thing, index)
    if r.data.get("called") in {"goal", "project"}:
        return r.data["called"]
    parent = index.get(r.data.get("parent"))
    return "subtask" if parent and parent.data.get("called") not in {"goal", "project"} else "task"


def next_of(thing: Thing, index: WorkIndex, now: Now) -> Next:
    """Open action; first nonwaiting open part; first waiting part; else itself.

    Projects follow their first open task; goals return each child's own next.
    A waiting answer includes its reason/until; completed parts yield nothing next.
    """
    return _next(_record(thing, index), index, now, frozenset())


def explain(thing: Thing, index: WorkIndex, now: Now) -> Explanation:
    """Explain up, waiting (including reason/until), parked, ended or open container."""
    return _explain(_record(thing, index), index, now, frozenset())


def status_of(thing: Thing, index: WorkIndex, now: Now) -> str:
    """Return explain's status, including inherited parking/end and all-parts waits."""
    return explain(thing, index, now)["status"]


def has_unfinished_prerequisites(thing: Thing, index: WorkIndex) -> bool:
    """A step (or its container) waits for every recorded predecessor to be done.

    Accepting wording, parking, or dropping a predecessor is not completion.
    Missing predecessors remain unresolved. Person waits are a separate rule.
    """
    record = _record(thing, index)
    seen = set()
    while record is not None:
        ref = first_id(record)
        if ref in seen:
            raise ValueError("work parent cycle")
        seen.add(ref)
        for source in index.values():
            operation = source.data.get("planning", {}).get("operation", {})
            if active_followup_ref(source) == ref and not (followup_triggered(source, ref)):
                return True
        for target in record.data.get("after", ()):
            predecessor = index.get(target)
            triggered = followup_triggered(predecessor, ref)
            if not triggered and (predecessor is None or predecessor.state != "done"):
                return True
        record = index.get(record.data.get("parent"))
    return False


def is_up(thing: Thing, index: WorkIndex, now: Now) -> bool:
    """Whether this is Nic's actionable open task without open parts, or subtask."""
    return status_of(thing, index, now) == "up"


def up_things(index: WorkIndex, now: Now) -> tuple[Record, ...]:
    """Return current actionable records in deterministic sibling order."""
    return tuple(r for r in sorted(index.values(), key=_order) if is_up(r, index, now))


def done_day_of(thing: Thing, index: WorkIndex) -> date:
    """Explicit done day, else the Singapore 4am day of the completing version.

    Supplied history keeps later edits of a done thing from moving its done day.
    With no historical version supplied, the current version is the fallback.
    """
    r = _record(thing, index)
    explicit = r.data.get("dates", {}).get("done")
    if explicit:
        return date.fromisoformat(explicit["day"])
    versions = getattr(index, "_versions", {})
    completed = r
    for ref in reversed(r.supersedes):
        prior = versions.get(ref)
        if prior is None:
            continue
        if prior.state != "done":
            break
        completed = prior
    return _day(completed.updated_at)


def progress_of(thing: Thing, index: WorkIndex) -> Progress:
    """Part count, started fact, and latest ticked part/action with its time."""
    r = _record(thing, index)
    parts = _parts(r, index)
    ticked_parts = [p for p in parts if p.state == "done"]
    action = r.data.get("next_action")
    if not parts and action:
        total, done = 1, int(action["done"])
    else:
        total, done = len(parts), len(ticked_parts)
    ticked = [p for p in (r, *_descendants(r, index))
              if p.state == "done" or (p.data.get("next_action") or {}).get("done")]
    def tick_day(p: Record) -> date:
        return done_day_of(p, index) if p.state == "done" else _day(p.updated_at)
    last = max(ticked, key=lambda p: (tick_day(p), p.updated_at, p.id), default=None)
    return Progress(done=done, total=total, text=f"{done} of {total}",
                    started=bool(r.data.get("started_at") or ticked),
                    last_ticked=first_id(last) if last else None,
                    last_ticked_at=last.updated_at.isoformat() if last else None,
                    last_ticked_day=tick_day(last).isoformat() if last else None)


def dates_of(thing: Thing, index: WorkIndex, *, own: bool = False) -> dict[str, Any]:
    """Return separate date kinds; inherit only deadline unless review-marked.

    The index's mapped data retains date words/read_on. Inherited stake and labels
    are available on its records; marked parts inherit labels only. ``own=True``
    returns mapped dates before inheritance, for a once-per-deadline display.
    """
    record = _record(thing, index)
    if own and isinstance(index, _ReaderIndex):
        record = index._own[first_id(record)]
    return deepcopy(record.data.get("dates", {}))


def event_day(dates: Mapping[str, Any]) -> bool:
    """An explicit plan beside a dated event wait means work happens on that day."""
    wait = dates.get("not_before") or {}
    return bool(wait.get("day") and wait.get("event")
                and (dates.get("planned") or {}).get("day") == wait["day"])


def length_of(thing: Thing, index: WorkIndex) -> int:
    """Minutes: own length plus open parts; projects sum their open tasks."""
    return _length(_record(thing, index), index, frozenset())


def needs_nic(thing: Thing, index: WorkIndex, now: Now, history: History = (), *,
              with_answer_kind: bool = False) -> tuple[Need, ...]:
    """Return plan 6.1 review needs, matching supplied why-log history by first ID.

    Includes nothing next, missed, did it happen, carried over, stalled, to chase,
    needs you, carried over three times and over two hours without parts.
    Opt in to transient answer-kind provenance for internal question classification;
    the default dictionaries and allowed answers remain unchanged.
    """
    r = _record(thing, index)
    if r.data.get("material_kind"):
        return ()
    if status_of(r, index, now) in {"ended", "parked"}:
        result: list[Need] = []
        if r.state == "done":
            for mark in r.data.get("review_marks", ()):
                if mark["mark"] == "question":
                    need = Need(kind="needs_you", item_ref=first_id(r),
                                text=mark.get("text") or mark.get("part") or "question", count=0,
                                allowed_answers=allowed_answers("question"))
                    if with_answer_kind:
                        need["answer_kind"] = "question"
                    result.append(need)
        done = r.data.get("dates", {}).get("done", {})
        reader = done.get("read_by", "code")
        if r.state == "done" and done and not done.get("confirmed") and reader != "code" and not reader.startswith("system:"):
            need = Need(kind="needs_you", item_ref=first_id(r), text=f"Keep done: {done['words']}?", count=0,
                        allowed_answers=allowed_answers("agents_wording"))
            if with_answer_kind:
                need["answer_kind"] = "agents_wording"
            result.append(need)
        return tuple(result)
    day = _day(now).isoformat()
    rows = sorted((row for row in history if _history_ref(row, index) == first_id(r)),
                  key=lambda row: _moment(_field(row, "surfaced_at")))
    result: list[Need] = []

    def add(kind: str, text: str, count: int = 0, *, answers_kind: str | None = None,
            answer_kind: str | None = None) -> None:
        need = Need(kind=kind, item_ref=first_id(r), text=text, count=count,
                    allowed_answers=allowed_answers(answers_kind or kind))
        if with_answer_kind:
            need["answer_kind"] = answer_kind or answers_kind or kind
        result.append(need)

    data = r.data
    dates = dates_of(r, index)
    next_item = next_of(r, index, now)
    if next_item["reason"] == "nothing next":
        finish = data.get("finish_line")
        add("nothing_next", f"{finish}: done, or what's next?" if finish else "Done, or what's next?")
    if dates.get("deadline", {}).get("day", day) < day:
        add("missed", "Recommit, change, defer or drop?")
    appointment = dates.get("appointment")
    if appointment and _passed_appointment(appointment, now):
        add("did_it_happen", "Done, move or drop?")
    planned = dates.get("planned")
    carried = [row for row in rows if _field(row, "rule") == "carried_over"]
    if planned and (planned.get("day") or planned.get("end", day)) < day:
        # Said once for this planned day, even if the record has a new version.
        plan_day = planned.get("day") or planned["end"]
        if not any(_day(_moment(_field(row, "surfaced_at"))) > date.fromisoformat(plan_day) for row in carried):
            add("carried_over", "Today, another day, done already, or drop?")
    # One carry per personal day: the same carry logged again is not another carry.
    carries = len({_day(_moment(_field(row, "surfaced_at"))) for row in carried})
    if carries >= 3:
        add("carried_over_3_times", "Carried over 3 times: parts, park or drop?", carries)
    changed = max((p.updated_at for p in _descendants(r, index)), default=r.updated_at)
    changed = max(changed, r.updated_at)
    if progress_of(r, index)["started"] and (_day(now) - _day(changed)).days >= 3:
        add("stalled", "What's in the way? What's the very next physical thing?")
    wait = data.get("needs") or ({"from": data["whose"], "expected": data.get("expected")} if data.get("whose") else None)
    if wait:
        expected = (wait.get("expected") or {}).get("day")
        waiting_rows = [row for row in rows if _field(row, "rule") in {"waiting_on", "waiting_on_item", "to_chase"}]
        since = _day(_moment(_field(waiting_rows[0], "surfaced_at"))) if waiting_rows else _day(r.created_at)
        if (expected and expected < day) or (_day(now) - since).days >= 7:
            add("to_chase", f"Draft a nudge to {wait['from']}?")
    for mark in data.get("review_marks", ()):
        text = mark.get("text") or mark.get("part") or mark["mark"].replace("_", " ")
        add("needs_you", text, answers_kind=mark["mark"])
    source = r.source or {}
    # Others' words are saved at level suggested (plan 4.3); a yes accepts them.
    if data.get("level") == "suggested" and (source.get("said_by") == "other" or source.get("came_by") == "connected_source"):
        add("needs_you", "Someone else's suggestion: keep, change or drop?", answers_kind="suggested")
    # Confirmation belongs to the record that owns a proposal, not each record
    # that inherits its effective value. Identical explicit child facts are own.
    own = index._own[first_id(r)].data if isinstance(index, _ReaderIndex) else data
    for kind, value in (*own.get("dates", {}).items(), ("expected", own.get("expected")),
                        ("needs.expected", (own.get("needs") or {}).get("expected"))):
        reader = value.get("read_by", "code") if value else "code"
        if value and not value.get("confirmed") and (value.get("confirmed") is False or kind == "suggested"
                or (reader != "code" and not reader.startswith("system:"))):
            words = value["words"]
            if words in {value.get("day"), value.get("start"), value.get("end")}:
                words = relative_day_words(words, _day(now))
            add("needs_you", f"Keep {kind}: {words}?",
                answers_kind="suggested" if kind == "suggested" else "proposed_start_by" if kind == "start_by" else "agents_wording",
                answer_kind="date_suggested" if kind == "suggested" else None)
    stake = own.get("stake")
    if stake and not stake.get("confirmed"):
        add("needs_you", f"Keep proposed stake: {stake.get('at_stake') or stake_words(stake.get('level', ''))}?", answers_kind="proposed_stake")
    if not _parts(r, index) and length_of(r, index) > 120:
        add("over_two_hours", "Over 2 hours: parts? Say them, or leave it.")
    return tuple(result)


def allowed_answers(line_kind: str) -> tuple[str, ...]:
    """Return the review kind's offered answers as data; unknown kinds fail loudly."""
    answers = {
        "missed": ("recommit", "change", "defer", "drop"),
        "did_it_happen": ("done", "move", "drop"),
        "carried_over": ("today", "another day", "done already", "drop"),
        "to_chase": ("draft a nudge", "leave it"),
        "nothing_next": ("done", "what's next"),
        "stalled": ("what's in the way", "next action"),
        "needs_you": ("keep", "change", "drop"),
        "carried_over_3_times": ("parts", "park", "drop"),
        "over_two_hours": ("parts", "leave it"),
        "agents_wording": ("keep", "change"),
        "maybe_same": ("merge", "leave separate"),
        "unclear": ("clarify", "drop"),
        "question": ("answer", "leave it"),
        "looks_like": ("yes", "no"),
        "ask_stake": ("stake", "nothing much"),
        "spelling": ("keep", "correct"),
        "suggested": ("yes", "no"),
        "proposed_stake": ("keep", "change"),
        "proposed_start_by": ("keep", "change"),
        "two_minutes": ("do it now", "leave it"),
    }
    return answers[line_kind]


def _record(thing: Thing, index: WorkIndex) -> Record:
    ref = first_id(thing) if isinstance(thing, Record) else thing
    if ref in index:
        return index[ref]
    for r in index.values():
        if ref == r.id or ref in r.supersedes:
            return r
    raise KeyError(ref)


def _own_index(items: Iterable[Record], *, for_write: bool) -> dict[str, Record]:
    records = tuple(items)
    aliases = {ref: first_id(r) for r in records for ref in (r.id, *r.supersedes)}
    current: dict[str, Record] = {}
    for r in records:
        if r.kind != "item":
            continue
        key = first_id(r)
        previous = current.get(key)
        if previous is None or (len(r.supersedes), r.updated_at, r.id) > (
            len(previous.supersedes), previous.updated_at, previous.id
        ):
            current[key] = r
    projects = {r.title.strip().casefold(): key for key, r in sorted(current.items())
                if r.data.get("called") == "project"}
    result: dict[str, Record] = {}
    for key, r in sorted(current.items()):
        if for_write and r.data.get("repeat"):
            # Routines' old shape and occurrence history are not promoted.
            value = deepcopy(r.as_dict())
        else:
            data = _mapped_data(r, aliases, projects)
            if not for_write:
                result[key] = replace(r, data=data)
                continue
            value = _write_mapping(r, data, aliases, projects)
            residue = _unrepresented(r, value)
            if residue:
                field, reason, fix = residue
                if WORK_FIELDS.intersection(r.data):
                    raise BrainError(
                        ErrorCode.CONTRACT_INVALID,
                        f"Cannot convert {reason}; keep the old shape unchanged or supply the actual missing fact before adding work fields",
                        details={"record_id": r.id, "field": field, "fix": fix},
                    )
                # Keep every own fact and its provenance together when the
                # frozen work shape cannot represent the complete record.
                value = deepcopy(r.as_dict())
        # Fail loudly if a native fact is invalid for the frozen shape.
        result[key] = record_from_mapping(value, "work.write_index")
    return result


_PERSON_FIX = "Keep the old shape, or record the actual needs.from/whose from Nic's words; never invent a person"


def _unrepresented(r: Record, value: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return the first legacy fact the converted shape could not hold.

    Checks the whole candidate before anything is written: old links left over,
    mapped text over the frozen work bounds, dates without stored words, states
    that stayed waiting/blocked, a start-by without a deadline, leftover legacy
    timing keys, and graph fields that did not become record IDs.
    """
    data = value["data"]
    if not WORK_FIELDS.intersection(data):
        return None  # still the old shape, which the parser reads as it is
    for link in value["links"]:
        if link["type"] in {"part_of", "blocked_by", "waiting_on"}:
            return (f"links[{r.links.index(link)}]", f"a legacy {link['type']} link to {link['target']!r} that no work field holds",
                    f"Keep the old shape, or record what the {link['type']} link means from Nic's words; never guess a record, turn a record ID into a person, or drop the link")
    # The frozen work bounds (records._validate_work; plan 3.1 field table).
    for field, text in (("whose", data.get("whose")), ("needs.from", (data.get("needs") or {}).get("from"))):
        top = field.split(".")[0]
        if text is not None and top not in r.data and len(text) > 128:
            return (f"data.{field}", f"a legacy waiting_on name of {len(text)} characters, over the 128 the work shape holds",
                    "Keep the old shape, or record the person's name in 128 characters from Nic's words; never cut or drop it")
    if "after" not in r.data and len(data.get("after", ())) > 20:
        return ("data.after", f"{len(data['after'])} legacy blocked_by links, over the 20 the work shape holds",
                "Keep the old shape, or say from Nic's words which 20 or fewer things it waits after; never drop a blocker")
    derived = [*(() if "dates" in r.data else data.get("dates", {}).values()),
               *(() if "expected" in r.data else [data.get("expected")]),
               *(() if "needs" in r.data else [(data.get("needs") or {}).get("expected")])]
    for entry in filter(None, derived):
        if "words" not in entry:
            return ("data.timing.resolved_from", "a legacy date with no stored original words",
                    "Keep the old shape, or record Nic's actual words for the date in a corrected owner quote; never invent date words")
        if len(entry["words"]) > 256:
            return ("data.timing.resolved_from.words", f"legacy date words of {len(entry['words'])} characters, over the 256 the work shape holds",
                    "Keep the old shape, or record the date's words in 256 characters from Nic's words; never cut them")
    if value["state"] == "waiting":
        return "state", "a legacy wait without a recorded person", _PERSON_FIX
    if value["state"] == "blocked":
        return ("state", "a legacy blocked state without a blocking record ID",
                "Keep the old shape, or record the actual blocking thing's first ID from Nic's words; never invent a blocker")
    dates = data.get("dates", {})
    if "start_by" in dates and "deadline" not in dates:
        return (
            "data.timing.start_by", "a legacy start_by without a recorded deadline",
            "Keep the old shape, or record the actual deadline from Nic's words before adding work fields; never invent a deadline or drop start_by",
        )
    timing = data.get("timing") or {}
    for key in ("due", "at", "window", "on_event", "on_return", "start_by", "resolved_from"):
        if key not in timing:
            continue
        if key == "due" and r.state == "waiting":
            # An event represents the wait, but does not give due a person or
            # authorize interpreting it as the event's date or a deadline.
            return f"data.timing.{key}", "a legacy wait's due without a recorded person", _PERSON_FIX
        return (f"data.timing.{key}", f"legacy timing.{key} that no work date holds beside the others",
                f"Keep the old shape, or restate timing.{key} as a work date from Nic's words; never guess its meaning or drop it")
    own_first = first_id(r)
    parent = data.get("parent")
    if "parent" not in r.data and parent is not None and (parent == own_first or not _RECORD_ID.fullmatch(parent)):
        return ("data.parent", f"a legacy part_of {parent!r} that is not another record's ID",
                "Keep the old shape, or record the actual parent's first ID from Nic's words; never guess which record it names")
    for position, ref in enumerate(data.get("after", ())):
        if ref not in r.data.get("after", ()) and (ref == own_first or not _RECORD_ID.fullmatch(ref)):
            return (f"data.after[{position}]", f"a legacy blocked_by {ref!r} that is not another record's ID",
                    "Keep the old shape, or record the actual blocking thing's first ID from Nic's words; never guess which record it names")
    return None


def _write_mapping(r: Record, data: dict[str, Any], aliases: Mapping[str, str], projects: Mapping[str, str]) -> dict[str, Any]:
    """Clean the shared mapped own facts, never calculate or persist inheritance."""
    value = deepcopy(r.as_dict())
    value["data"] = data
    if data.get("called"):
        data["size"] = "task"
    if data.get("next_action"):
        data.pop("first_step", None)
    dates = data.get("dates", {})
    timing = data.get("timing") or {}
    translated_date = False
    due = timing.get("due")
    due_targets = (dates.get("deadline"), dates.get("appointment"), data.get("expected"),
                   (data.get("needs") or {}).get("expected"))
    if due and any(target and target.get("day") == due for target in due_targets):
        timing.pop("due")
        if any(target and target.get("at") == timing.get("at") for target in due_targets):
            timing.pop("at", None)
        translated_date = True
    window = timing.get("window")
    if window and dates.get("deadline", {}).get("day") == window["end"]:
        timing.pop("window")
        translated_date = True
    if timing.get("start_by") and dates.get("start_by", {}).get("day") == timing["start_by"]:
        timing.pop("start_by")
        translated_date = True
    for field in ("on_event", "on_return"):
        if timing.get(field) and dates.get("not_before", {}).get("event") == timing[field]:
            timing.pop(field)
            translated_date = True
    if translated_date:
        timing.pop("resolved_from", None)
    if "timing" in data:
        data["timing"] = timing
    links = []
    parent_link = _parent_link(r, aliases, projects)
    for position, link in enumerate(r.links):
        target = aliases.get(link["target"], link["target"])
        parent_target = _parent_from_link(link, aliases, projects)
        consumed_parent = parent_link is not None and position == parent_link[0]
        translated = (
            (link["type"] == "part_of" and parent_target is not None and data.get("parent") == parent_target)
            or (link["type"] == "about" and consumed_parent and data.get("parent") == parent_target)
            or (link["type"] == "blocked_by" and target in data.get("after", ()))
            or (link["type"] == "waiting_on" and (data.get("needs") or {}).get("from") == target)
        )
        if not translated:
            links.append(deepcopy(link))
    value["links"] = links
    if r.state == "blocked" and data.get("after"):
        value["state"] = "open"
    elif r.state == "waiting" and (data.get("needs") or data.get("whose") or dates.get("not_before")):
        value["state"] = "open"
    return value


def _mapped_data(r: Record, aliases: Mapping[str, str], projects: Mapping[str, str]) -> dict[str, Any]:
    data = deepcopy(r.data)
    parent_link = _parent_link(r, aliases, projects)
    if parent_link is not None:
        data["parent"] = parent_link[1]
    for link in r.links:
        target = aliases.get(link["target"], link["target"])
        if link["type"] == "blocked_by":
            if "after" not in r.data:
                data.setdefault("after", []).append(target)
        elif link["type"] == "waiting_on" and r.state == "waiting" and not _RECORD_ID.fullmatch(target):
            # A record ID names a thing, not a person; never turn it into needs.from.
            data.setdefault("needs", {"from": target})
    if data.get("parent"):
        data["parent"] = aliases.get(data["parent"], data["parent"])
    if "after" in data:
        data["after"] = list(dict.fromkeys(aliases.get(ref, ref) for ref in data["after"]))
    planning = data.get("planning", {})
    operation = planning.get("operation")
    if operation:
        operation["followup_ref"] = aliases.get(operation["followup_ref"], operation["followup_ref"])
    for disposition in planning.get("followup_dispositions", []):
        for field in ("from_ref", "to_ref"):
            if field in disposition:
                disposition[field] = aliases.get(disposition[field], disposition[field])
    for block in planning.get("shared_blocks", []):
        block["task_refs"] = list(dict.fromkeys(aliases.get(ref, ref) for ref in block["task_refs"]))
    for mark in data.get("review_marks", ()):
        if "as" in mark:
            mark["as"] = aliases.get(mark["as"], mark["as"])
    if data.get("size") == "goal":
        data.setdefault("called", "goal")
    if data.get("first_step") and "next_action" not in data:
        data["next_action"] = {"text": data["first_step"], "done": False}
        data.setdefault("review_marks", []).append({"mark": "agents_wording", "part": "next_action"})
    timing = data.get("timing") or {}
    dates = data.setdefault("dates", {})
    resolved = timing.get("resolved_from") or {}
    # The parser stores resolved_from as {words, today}, both or neither; this is
    # R7's carry boundary (language._mapped_old_dates). Without it no original
    # words exist, so the projection carries none rather than inventing them.
    provenance = ({"words": resolved["words"], "read_on": resolved["today"], "read_by": "code"} if resolved
                  else {"read_on": _day(r.created_at).isoformat(), "read_by": "code"})
    if timing.get("due"):
        value = dict(provenance, day=timing["due"])
        if timing.get("at"):
            value["at"] = timing["at"]
        if r.state == "waiting":
            if data.get("needs"):
                data["needs"].setdefault("expected", value)
            elif data.get("whose"):
                data.setdefault("expected", value)
        elif timing.get("at"):
            dates.setdefault("appointment", dict(value, at=timing["at"]))
        else:
            dates.setdefault("deadline", value)
    if timing.get("window"):
        dates.setdefault("deadline", dict(provenance, day=timing["window"]["end"]))
    if timing.get("start_by"):
        dates.setdefault("start_by", dict(provenance, day=timing["start_by"]))
    event = timing.get("on_event") or timing.get("on_return")
    if event:
        dates.setdefault("not_before", dict(provenance, event=event))
    if not dates:
        data.pop("dates")
    data.pop("importance", None)
    return data


def _parent_link(r: Record, aliases: Mapping[str, str], projects: Mapping[str, str]) -> tuple[int, str] | None:
    """Select the consumed parent link: explicit part_of before a project name join."""
    if WORK_FIELDS.intersection(r.data):
        return None  # native (or mixed) records keep their own parent and links
    for link_type in ("part_of", "about"):
        for position, link in enumerate(r.links):
            if link["type"] == link_type:
                target = _parent_from_link(link, aliases, projects)
                if target is not None:
                    return position, target
    return None


def _parent_from_link(link: Mapping[str, str], aliases: Mapping[str, str], projects: Mapping[str, str]) -> str | None:
    target = aliases.get(link["target"], link["target"])
    if link["type"] == "part_of" and target.startswith("brn_"):
        return target
    if link["type"] in {"part_of", "about"} and not target.startswith("brn_"):
        return projects.get(target.strip().casefold())
    return None


def _order(r: Record) -> tuple:
    return (r.data.get("order", 1000), r.created_at, first_id(r))


def _parts(r: Record, index: WorkIndex) -> tuple[Record, ...]:
    return tuple(sorted((p for p in index.values() if p.data.get("parent") == first_id(r)), key=_order))


def _open_parts(r: Record, index: WorkIndex) -> tuple[Record, ...]:
    return tuple(p for p in _parts(r, index) if p.state not in _ENDED)


def _day(now: Now) -> date:
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("work reader needs an aware datetime or a local date")
        return (now.astimezone(_SGT) - timedelta(hours=4)).date()
    return now


def _moment(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("why-log surfaced_at must be an aware datetime or ISO timestamp")
    return moment.astimezone(timezone.utc)


def _explain(r: Record, index: WorkIndex, now: Now, seen: frozenset[str]) -> Explanation:
    key = first_id(r)
    if key in seen:
        raise ValueError("work relationship cycle")
    seen = seen | {key}
    if r.state in _ENDED:
        return Explanation(status="ended", reason=r.state, until=None)
    if r.data.get("material_kind"):
        return Explanation(status="open", reason="retained " + r.data["material_kind"], until=None)
    # Ancestor state only; a parent's all-parts wait must not recurse into this part.
    above = r
    ancestors = {key}
    while above.data.get("parent") in index:
        above = index[above.data["parent"]]
        if first_id(above) in ancestors:
            raise ValueError("work parent cycle")
        ancestors.add(first_id(above))
        if above.state in _ENDED:
            return Explanation(status="ended", reason=f"under ended {above.title}", until=None)
        if above.state == "parked":
            return Explanation(status="parked", reason=f"under parked {above.title}", until=_parked_until(above))
    if r.state == "parked":
        reason = (f"parked ({_parked_until(r)})" if isinstance(r.data.get("parked_until"), Mapping) else "parked")
        return Explanation(status="parked", reason=reason, until=_parked_until(r))
    data = r.data
    from brain_core.planning import required_followup
    triggers = [source for source in index.values() if active_followup_ref(source) == key]
    for source in triggers:
        followup = required_followup(source, index)
        if not followup["approved"] or followup["conditional"]:
            return Explanation(status="waiting", reason=f"required follow-up after confirmed start of {source.title}", until=None)

    wait = data.get("needs")
    if wait or data.get("whose"):
        who = wait["from"] if wait else data["whose"]
        expected = (wait.get("expected") if wait else data.get("expected")) or {}
        what = f": {wait['what']}" if wait and wait.get("what") else ""
        until = expected.get("day")
        return Explanation(status="waiting", reason=f"waiting on {who}{what}" + (f", expected {relative_day_words(until, _day(now))}" if until else ""), until=until)
    for ref in data.get("after", ()):
        blocker = index.get(ref)
        triggered = followup_triggered(blocker, key)
        if not triggered and (blocker is None or blocker.state != "done"):
            waiting = _explain(blocker, index, now, seen) if blocker else None
            return Explanation(status="waiting", reason=f"after '{blocker.title if blocker else ref}'",
                               until=waiting["until"] if waiting else None)
    not_before = data.get("dates", {}).get("not_before")
    if not_before:
        day, event = not_before.get("day"), not_before.get("event")
        if (day and day > _day(now).isoformat()) or (event and not day):
            prefix = "on" if event_day(data.get("dates", {})) else "not before"
            return Explanation(status="waiting", reason=f"{prefix} {relative_day_words(day, _day(now)) if day else event}" + (f" ({event})" if day and event else ""), until=day)
    if r.state in {"blocked", "waiting"} and not (data.get("after") or not_before):
        # An old waiting_on to a record ID is a recorded dependency, not a person:
        # show it by that thing's first ID rather than dropping the fact.
        on = [_first_ref(link["target"], index) for link in r.links if link["type"] == "waiting_on"] if r.state == "waiting" else []
        return Explanation(status="waiting", reason=f"waiting on {', '.join(on)}" if on else r.data.get("reason") or "waiting: reason not recorded",
                           until=(data.get("timing") or {}).get("due") if r.state == "waiting" else None)
    parts = _open_parts(r, index)
    if parts:
        statuses = [_explain(p, index, now, seen) for p in parts]
        if all(s["status"] == "waiting" for s in statuses):
            return Explanation(status="waiting", reason=statuses[0]["reason"], until=statuses[0]["until"])
        return Explanation(status="open", reason="open parts", until=None)
    if data.get("called") in {"goal", "project"} or data.get("level") != "accepted":
        return Explanation(status="open", reason="not an actionable task", until=None)
    return Explanation(status="up", reason="up", until=None)


def _next(r: Record, index: WorkIndex, now: Now, seen: frozenset[str]) -> Next:
    key = first_id(r)
    if key in seen:
        raise ValueError("work parent cycle")
    seen = seen | {key}
    explanation = explain(r, index, now)
    result = Next(item_ref=key, text=r.title, children=(), **explanation)
    if explanation["status"] in {"ended", "parked"}:
        return result
    parts = _open_parts(r, index)
    if r.data.get("called") == "goal":
        result["children"] = tuple(_next(p, index, now, seen) for p in parts)
        if not parts and _parts(r, index) and explanation["status"] != "waiting":
            result["reason"] = "nothing next"
        return result
    action = r.data.get("next_action")
    if action and not action["done"]:
        result["text"] = action["text"]
        return result
    if parts:
        candidates = [p for p in parts if status_of(p, index, now) not in {"waiting", "parked", "ended"}]
        chosen = _next(candidates[0] if candidates else parts[0], index, now, seen)
        # A container's own wait still governs the selected part.
        if explanation["status"] == "waiting" and candidates:
            chosen.update(explanation)
        return chosen
    if explanation["status"] != "waiting" and (action or _parts(r, index) or r.data.get("called")):
        result["reason"] = "nothing next"
    return result


def _length(r: Record, index: WorkIndex, seen: frozenset[str]) -> int:
    key = first_id(r)
    if key in seen:
        raise ValueError("work parent cycle")
    seen = seen | {key}
    if r.data.get("material_kind"):
        return 0
    own = r.data.get("remaining_minutes", (r.data.get("timing") or {}).get("duration_minutes") or 0)
    if r.data.get("called") == "project":
        own = 0
    return own + sum(_length(p, index, seen) for p in _open_parts(r, index))


def _descendants(r: Record, index: WorkIndex) -> tuple[Record, ...]:
    result: list[Record] = []
    queue = list(_parts(r, index))
    seen = {first_id(r)}
    while queue:
        p = queue.pop(0)
        if first_id(p) in seen:
            raise ValueError("work parent cycle")
        seen.add(first_id(p))
        result.append(p)
        queue.extend(_parts(p, index))
    return tuple(result)


def _field(row: Any, field: str, default: Any = None) -> Any:
    return row.get(field, default) if isinstance(row, Mapping) else getattr(row, field, default)


def _first_ref(ref: str, index: WorkIndex) -> str:
    """Resolve any version ID in the index to its first ID; other text stays as stored."""
    for r in index.values():
        if ref == r.id or ref in r.supersedes:
            return first_id(r)
    return ref


def _history_ref(row: Any, index: WorkIndex) -> str:
    return _first_ref(_field(row, "item_ref", ""), index)


def _passed_appointment(value: Mapping[str, Any], now: Now) -> bool:
    if isinstance(now, datetime):
        return f"{value['day']}T{value['at']}" < now.astimezone(_SGT).isoformat()[:16]
    return value["day"] < now.isoformat()


def _parked_until(r: Record) -> str | None:
    until = r.data.get("parked_until")
    return until.get("event") if isinstance(until, Mapping) else until


def review_outcomes_of(thing: Thing, index: WorkIndex) -> tuple[Mapping[str, Any], ...]:
    """Explicit dated outcomes only; no inferred completion from elapsed time."""
    return tuple(deepcopy(_record(thing, index).data.get("review_outcomes", ())))


def has_review_outcome(thing: Thing, index: WorkIndex, day: str) -> bool:
    return any(entry["day"] == day for entry in review_outcomes_of(thing, index))
