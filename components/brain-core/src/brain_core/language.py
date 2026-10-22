"""The v2 language: plain wording, change rules (R1-R14), and the fixed action tiers.

Deterministic only: no model call. Agents pick the kind, the level, the time words and the
links; this module checks the result against the rules in the language spec and computes the
plain word for an item. See docs/product/steps/c-record-format.md.
"""

from __future__ import annotations

import re
import string
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from brain_core.errors import BrainError, ErrorCode
from brain_core.timewords import from_words

if TYPE_CHECKING:
    from brain_core.records import Record

TIER_1_ACTIONS = frozenset({"read", "research", "draft", "link", "mark_waiting", "mark_blocked"})
TIER_3_ACTIONS = frozenset({"send", "spend", "delete", "publish", "security"})
TIERS: dict[str, tuple[str, ...]] = {
    "tier_1": tuple(sorted(TIER_1_ACTIONS)),
    "tier_3": tuple(sorted(TIER_3_ACTIONS)),
}


def may_act(action: str) -> str:
    """Tier 1 actions are 'yes'; tier 3 and anything unlisted is always 'ask'."""

    return "yes" if action in TIER_1_ACTIONS else "ask"


def plain_word(data: Mapping[str, Any]) -> str:
    """Compute an item's plain word from its data; never stored."""

    level = data.get("level")
    if level == "possible":
        return "idea"
    if level == "suggested":
        return "suggestion"
    if data.get("size") == "goal":
        return "goal"
    if data.get("repeat"):
        return "routine"
    if data.get("owed_to"):
        return "promise"
    return "task"


def actionable(records: Iterable["Record"]) -> tuple["Record", ...]:
    """Accepted items in open, waiting or blocked; ideas and suggestions never included."""

    return tuple(
        record
        for record in records
        if record.kind == "item"
        and record.data.get("level") == "accepted"
        and record.state in ("open", "waiting", "blocked")
    )


def focus_preference(records: Iterable["Record"]) -> "Record | None":
    """The preference naming Nic's focus projects (Ciel redesign N4, step C §13): the newest
    current, active v2 preference in his own words that carries ``data.focus_projects``, so
    the newest record wins. None when he has never named any; its list may be empty."""

    from brain_core.records import RECORD_SCHEMA_V2

    candidates = tuple(records)
    superseded = {record_id for record in candidates for record_id in record.supersedes}
    named = [
        record
        for record in candidates
        if record.schema == RECORD_SCHEMA_V2
        and record.kind == "preference"
        and record.state == "active"
        and record.id not in superseded
        and (record.source or {}).get("said_by") == "owner"
        and "focus_projects" in record.data
    ]
    return max(named, key=lambda record: (record.updated_at, record.id), default=None)


def working_window_preference(records: Iterable["Record"]) -> "Record | None":
    """The preference holding Nic's working window (step I, PRD §4.3): chosen as
    `focus_preference` chooses, the newest current, active v2 preference in his own words that
    carries ``data.working_window``. None when he has never set one."""

    from brain_core.records import RECORD_SCHEMA_V2

    candidates = tuple(records)
    superseded = {record_id for record in candidates for record_id in record.supersedes}
    named = [
        record
        for record in candidates
        if record.schema == RECORD_SCHEMA_V2
        and record.kind == "preference"
        and record.state == "active"
        and record.id not in superseded
        and (record.source or {}).get("said_by") == "owner"
        and "working_window" in record.data
    ]
    return max(named, key=lambda record: (record.updated_at, record.id), default=None)


# Step I (Nic, 29 Sep 2026: weekdays 9 to 6, weekends off), used until he saves his own.
DEFAULT_WORKING_WINDOW: Mapping[str, tuple[str, str] | None] = {
    "mon": ("09:00", "18:00"),
    "tue": ("09:00", "18:00"),
    "wed": ("09:00", "18:00"),
    "thu": ("09:00", "18:00"),
    "fri": ("09:00", "18:00"),
    "sat": None,
    "sun": None,
}


def working_window(records: Iterable["Record"]) -> Mapping[str, tuple[str, str] | None]:
    """Nic's working window by weekday (``mon`` to ``sun``): a (start, end) pair in HH:MM
    Singapore time, or None for a day off. His newest saved window, where a day he left out is a
    day off; `DEFAULT_WORKING_WINDOW` when he has never saved one."""

    from brain_core.records import WORKING_WINDOW_DAYS

    preference = working_window_preference(records)
    if preference is None:
        return dict(DEFAULT_WORKING_WINDOW)
    saved = preference.data["working_window"]
    return {
        day: (saved[day]["start"], saved[day]["end"]) if saved.get(day) else None
        for day in WORKING_WINDOW_DAYS
    }


def _owner_words(source: Mapping[str, Any] | None) -> bool:
    if not source:
        return False
    return source.get("said_by") == "owner" and source.get("came_by") == "direct" and bool(source.get("words"))


# The timing fields that say when something happens (R7). Length fields are not among them.
_DATED_TIMING_FIELDS = ("due", "at", "window", "on_event", "on_return", "start_by", "resolved_from")


def _dated_timing(timing: Mapping[str, Any]) -> dict[str, Any]:
    return {field: timing[field] for field in _DATED_TIMING_FIELDS if timing.get(field) is not None}


def _proposal(importance: Mapping[str, Any] | None) -> tuple[Any, ...] | None:
    """What an importance proposal says, apart from whether Nic has confirmed it."""

    if not importance:
        return None
    return (importance.get("level"), importance.get("reason"), importance.get("proposed_by"))


_SAVE_INSTRUCTION_PUNCTUATION = string.punctuation + " "

# ROA-8 (2026-09-27, Nic approved the fix): an agent's own instruction to persist something
# already said is never Nic's statement about the thing itself - e.g. "Save it to gtbc" is
# ChatGPT's save command, not a quote of what Nic said. Matches only a verb from this fixed
# list, optionally followed by one object word, one preposition, one destination name and one
# politeness word, in that order, and nothing else. A real statement that happens to mention
# "save" ("save the receipts for the tax claim") carries extra words and will not match. Never
# extended by guesswork beyond this exact grammar. Shared verbatim by
# gremlin_town.mcp.github_brain_provider's early check, so the hosted and local paths refuse
# exactly the same words.
#
# Widened 2026-09-29 (Nic approved): Fleet Boss saved three items with the words "store in
# brain home 3, 4, 5". A list reference - numbers ("3", "#3", "3, 4 and 5", "3-5", "3 to 5",
# optionally after "item(s)", "number(s)" or "no.") or a pointer ("these", "them", "all of
# them", ...) - is also allowed between the verb and the destination and/or at the end.
# A number followed by any other word ("store 3 boxes in the garage") still does not match.
_LIST_NUMBER = r"#?\d+"
_LIST_REFERENCE = (
    r"(?:"
    rf"(?:(?:items?|numbers?|no\.?) )?{_LIST_NUMBER}(?:(?: ?, ?(?:and )?| and | to | ?- ?){_LIST_NUMBER})*"
    r"|these|those|them|both|all|all of them|all of these|everything"
    r")"
)
_SAVE_INSTRUCTION_ONLY = re.compile(
    r"^(?:save|add|put|store|remember|log|note)"
    r"(?: (?:it|this|that|down))?"
    rf"(?: {_LIST_REFERENCE})?"
    r"(?: (?:to|in|into))?"
    r"(?: (?:gtbc|brain home|brain|gt|gremlin town|memory))?"
    rf"(?: {_LIST_REFERENCE})?"
    r"(?: (?:please|pls|ok))?$"
)


def is_save_instruction_only(words: str) -> bool:
    """R11: True when *words* is nothing but a bare instruction to save something already
    said, once normalized (casefold, collapsed whitespace, stripped surrounding punctuation) -
    never Nic's actual statement about the thing."""

    normalized = " ".join(words.casefold().split()).strip(_SAVE_INSTRUCTION_PUNCTUATION)
    return bool(_SAVE_INSTRUCTION_ONLY.match(normalized))


def is_bare_reply(words: str) -> bool:
    """Plan 4.3: fixed replies that name nothing on their own.

    Reuse R11's save grammar and normalization. The additional words are
    limited to the plan's yes/ok, numbered done, and do/drop it forms.
    This predicate does not change R11 or any existing change rule.
    """

    normalized = " ".join(words.casefold().split()).strip(_SAVE_INSTRUCTION_PUNCTUATION)
    if is_save_instruction_only(normalized):
        return True
    if re.fullmatch(r"(?:yes|ok|[0-9]+(?: done)?|(?:do|drop) it)", normalized):
        return True
    affirmation = re.fullmatch(r"(?:yes|ok)[ ,]+(.+)", normalized)
    return bool(
        affirmation
        and (is_save_instruction_only(affirmation.group(1)) or affirmation.group(1) == "do it")
    )


def _date_entries(data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    entries = dict(data.get("dates") or {})
    if data.get("expected"):
        entries["expected"] = data["expected"]
    if (data.get("needs") or {}).get("expected"):
        entries["needs.expected"] = data["needs"]["expected"]
    return entries


def _mapped_old_dates(record: "Record | None") -> dict[str, Mapping[str, Any]]:
    """R7's old-shape carry boundary, without importing the future work reader."""

    if record is None:
        return {}
    timing = record.data.get("timing") or {}
    resolved = timing.get("resolved_from") or {}
    if not resolved.get("words") or not resolved.get("today"):
        return {}
    base = {"words": resolved["words"], "read_on": resolved["today"], "read_by": "code"}
    result: dict[str, Mapping[str, Any]] = {}
    if timing.get("due"):
        if record.state == "waiting":
            kind = "needs.expected" if any(link["type"] == "waiting_on" for link in record.links) else "expected"
        else:
            kind = "appointment" if timing.get("at") else "deadline"
        result[kind] = {**base, "day": timing["due"]}
        if timing.get("at"):
            result[kind] = {**result[kind], "at": timing["at"]}
    if timing.get("window"):
        result["deadline"] = {**base, "day": timing["window"]["end"]}
    if timing.get("start_by"):
        result["start_by"] = {**base, "day": timing["start_by"]}
    event = timing.get("on_event") or timing.get("on_return")
    if event:
        result["not_before"] = {**base, "event": event}
    return result


def _same_date_except_confirmation(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return {k: v for k, v in left.items() if k != "confirmed"} == {
        k: v for k, v in right.items() if k != "confirmed"
    }


_DEADLINE_WORD = re.compile(r"^(?:by|before|due|latest)\b", re.IGNORECASE)


def _stake_proposal(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: entry for key, entry in (value or {}).items() if key != "confirmed"}


def _dropped_question_only(previous: "Record | None", proposed: "Record", message: str | None,
                           actor: str) -> bool:
    """Prove an existing item's exact-message dropped question and narrow proposal.

    The caller supplies its own mapped previous version when other records are
    needed to resolve legacy references. W5 remains the only mapping authority.
    """
    from hashlib import sha256
    from brain_core import work

    if previous is None or previous.kind != "item" or proposed.kind != "item" or not isinstance(message, str) or not message:
        return False
    source = proposed.source or {}
    if source != {"said_by": "owner", "came_by": "direct", "words": message, "recorded_by": actor}:
        return False
    if proposed.id == previous.id or proposed.supersedes != tuple(dict.fromkeys((*previous.supersedes, previous.id))):
        return False
    if proposed.evidence[:len(previous.evidence)] != previous.evidence:
        return False
    added_evidence = proposed.evidence[len(previous.evidence):]
    digest = sha256(message.encode("utf-8")).hexdigest()
    if not added_evidence or any(e.excerpt != message or e.content_sha256 != digest or
                                e.source_type != "session_message" or e.locator != "message" or
                                e.actor not in (None, actor) for e in added_evidence):
        return False
    mapped = work.write_index((previous,))[work.first_id(previous)]
    old, new = mapped.as_dict(), proposed.as_dict()
    mutable = {"id", "created_at", "updated_at", "supersedes", "source", "evidence", "dedupe_key", "data"}
    if {k: v for k, v in old.items() if k not in mutable} != {k: v for k, v in new.items() if k not in mutable}:
        return False
    stake_changed = mapped.data.get("stake") != proposed.data.get("stake")
    if stake_changed:
        stake = proposed.data.get("stake") or {}
        if ((mapped.data.get("stake") or {}).get("confirmed") is True or
                stake.get("confirmed") is not False or stake.get("proposed_by") != actor):
            return False
    excluded = {"review_marks", "stake"} if stake_changed else {"review_marks"}
    if {k: v for k, v in mapped.data.items() if k not in excluded} != {k: v for k, v in proposed.data.items() if k not in excluded}:
        return False
    old_marks, new_marks = mapped.data.get("review_marks", []), proposed.data.get("review_marks", [])
    for count in range(1, len(new_marks) + 1):
        additions = new_marks[-count:]
        if any(set(mark) != {"mark", "text"} or mark["mark"] != "question" or not isinstance(mark.get("text"), str) or len(mark["text"]) > 1024 or
               not mark["text"].startswith(message + " — ") or not mark["text"][len(message) + 3:]
               for mark in additions):
            continue
        if stake_changed and (count != 1 or additions[0]["text"] !=
                message + " — stake: Move needs a non-bare quote from his message or an offered review answer"):
            continue
        expected = list(old_marks)
        for mark in additions:
            if len(expected) == 10:
                oldest = next((i for i, value in enumerate(expected) if value["mark"] == "question"), None)
                if oldest is None:
                    if count != 1 or len(old_marks) != 10:
                        break
                    oldest = 0  # Exactly one oldest eviction at the mandated cap.
                expected.pop(oldest)
            expected.append(mark)
        else:
            if expected == new_marks:
                return True
    return False


def check_change(
    previous: "Record | None", proposed: "Record", *, actor: str,
    message: str | None = None, answers: Iterable[str] = (), replaces: str | None = None,
    restored: "Record | None" = None, move_over: bool = False, dropped_question: bool = False,
) -> None:
    """Enforce R1-R14; optional door context never changes the stored record shape.

    ``answers`` contains the question text of review lines the door validated for
    this move. ``restored`` is the version undo restores; ``move_over`` permits
    only re-kinding a stored date. Legacy callers need no new arguments.
    ``dropped_question`` can admit only an exact-message question on an existing
    item's own W5 mapping. Only an actor's unconfirmed stake proposal accompanying
    its dropped-confirm question and the specified oldest-mark cap eviction may
    accompany that question; all other substantive facts remain unchanged.
    """

    from brain_core.records import RECORD_SCHEMA_V2

    if proposed.schema != RECORD_SCHEMA_V2 and dropped_question:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "dropped_question requires an existing v2 item", details={"field": "dropped_question"})
    if proposed.schema != RECORD_SCHEMA_V2:
        return
    source = proposed.source or {}
    questions = tuple(answers)
    source_words = " ".join((source.get("words") or "", *questions))

    def owner_words() -> bool:
        if not _owner_words(source):
            return False
        if questions or message is None:
            return True
        words = source["words"]
        return not is_bare_reply(words) and from_words(words, message)

    # R10: recorded_by is stamped from the authenticated caller, never the request body.
    if source.get("recorded_by") != actor:
        raise BrainError(
            ErrorCode.ACTOR_MISMATCH,
            "source.recorded_by must equal the authenticated caller",
            details={"record_id": proposed.id, "actor": actor},
        )

    if message is not None and _owner_words(source) and not questions and not from_words(source["words"], message):
        raise BrainError(
            ErrorCode.NEEDS_OWNER_WORDS, "source.words must quote the change's message word by word",
            details={"record_id": proposed.id, "field": "source.words"},
        )

    if dropped_question:
        if not questions and restored is None and not move_over and _dropped_question_only(previous, proposed, message, actor):
            return
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "dropped_question permits only an exact-message question on unchanged existing item facts",
                         details={"field": "dropped_question", "record_id": proposed.id})

    # R11: words must be Nic's own statement about the item, fact or preference - never his
    # instruction to save it (ROA-8). Checked before the kind-specific gates below, since a
    # save command claimed as his words is wrong regardless of kind - including preference,
    # whose own gate below would otherwise wave a said_by: owner source straight through.
    words = source.get("words")
    if source.get("said_by") == "owner" and isinstance(words, str) and is_save_instruction_only(words) and not questions:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "words must be what Nic said about the item, not an instruction to save it; "
            "quote his message about it",
            details={"record_id": proposed.id},
        )

    if proposed.kind == "preference":
        # A confirmed preference is only ever Nic's own words (language spec, "For builders").
        if source.get("said_by") != "owner":
            raise BrainError(
                ErrorCode.NEEDS_OWNER_WORDS,
                "A confirmed preference requires Nic's own words",
                details={"record_id": proposed.id},
            )
        return

    if proposed.kind != "item":
        return

    data = proposed.data
    level = data.get("level")
    state = proposed.state
    closed_state_changed = previous is None or previous.state != state or any(
        previous.data.get(field) != data.get(field) for field in ("parked_until", "reason", "check")
    )
    if message is not None and ((state == "done" and closed_state_changed) or restored is not None) and not owner_words():
        raise BrainError(ErrorCode.NEEDS_OWNER_WORDS, "Ending or undoing work requires Nic's own words", details={"record_id": proposed.id})

    # R1 / R2: creating directly at accepted needs owner words; an agent may create at
    # possible or suggested from its own guess.
    if previous is None and level == "accepted" and not owner_words():
        raise BrainError(
            ErrorCode.NEEDS_OWNER_WORDS,
            "Accepting an item requires Nic's own words",
            details={"record_id": proposed.id},
        )

    # R3: raising or lowering an existing item's level needs the change's own source to be
    # Nic's words, whatever the target level is. An unchanged level (e.g. only its state
    # moves) is not a level change and needs no owner words here.
    if previous is not None and previous.data.get("level") != level and not owner_words():
        raise BrainError(
            ErrorCode.NEEDS_OWNER_WORDS,
            "Changing an item's level requires Nic's own words",
            details={"record_id": proposed.id},
        )

    # R4: dropped, parked and no_longer_needed only from Nic's words; dropped needs a reason.
    if closed_state_changed and state in ("dropped", "parked", "no_longer_needed") and not owner_words():
        raise BrainError(
            ErrorCode.NEEDS_OWNER_WORDS,
            f"Marking an item {state} requires Nic's own words",
            details={"record_id": proposed.id, "state": state},
        )
    if state == "dropped" and not data.get("reason"):
        raise BrainError(
            ErrorCode.REASON_REQUIRED,
            "Dropping an item requires a reason",
            details={"record_id": proposed.id},
        )

    # R5: waiting and blocked may be set by any agent - no check needed (permitted by default).

    # R6: done needs a check by someone other than the agent that claimed it.
    if closed_state_changed and state == "done":
        check = data.get("check") or {}
        if check.get("checked_by") == actor:
            raise BrainError(
                ErrorCode.DONE_NEEDS_CHECK,
                "Done needs a check by someone other than the claiming actor",
                details={"record_id": proposed.id},
            )

    # R7: no invented deadlines - every timing must carry the time words it came from, and
    # those words must come from this change's source words: as one piece, or word by word
    # ("tmr at 12pm" from "tmr need to bring meshy to vet at 12pm"). Only checked when timing is
    # actually being set or changed here; timing carried over unchanged was already checked
    # when it was set, and an unrelated change's own words should not have to repeat it.
    # It covers dates, times and conditions only: a rough length is a labelled guess naming who
    # estimated it (Ciel and Brain Home PRD §3), never a deadline, so it needs no time words.
    timing = data.get("timing") or {}
    dated = _dated_timing(timing)
    timing_changed = previous is None or _dated_timing(previous.data.get("timing") or {}) != dated
    if restored is not None and _dated_timing(restored.data.get("timing") or {}) == dated:
        timing_changed = False
    if dated and timing_changed:
        resolved = timing.get("resolved_from") or {}
        words = resolved.get("words")
        if not words or not from_words(words, source_words):
            raise BrainError(
                ErrorCode.TIME_NOT_FROM_WORDS,
                "Timing must carry the time words it came from",
                details={"record_id": proposed.id},
            )

    earlier_dates = _date_entries(previous.data) if previous is not None else {}
    if replaces in earlier_dates and replaces not in _date_entries(data):
        raise BrainError(ErrorCode.TIME_NOT_FROM_WORDS, "A replacement must keep the named date kind", details={"record_id": proposed.id, "field": f"data.dates.{replaces}"})
    mapped = _mapped_old_dates(previous)
    restored_dates = {**_mapped_old_dates(restored), **(_date_entries(restored.data) if restored else {})}
    for kind, entry in _date_entries(data).items():
        old = earlier_dates.get(kind)
        if replaces == kind and old is not None:
            reading_keys = {"day", "words", "read_by", "read_on", "confirmed"}
            if {k: v for k, v in entry.items() if k not in reading_keys} != {k: v for k, v in old.items() if k not in reading_keys}:
                raise BrainError(ErrorCode.TIME_NOT_FROM_WORDS, "Replacing a date changes only its day and reading", details={"record_id": proposed.id, "field": f"data.dates.{kind}"})
        carried = old is not None and _same_date_except_confirmation(entry, old)
        carried = carried or entry == mapped.get(kind) or entry == restored_dates.get(kind)
        suggestion = earlier_dates.get("suggested")
        if suggestion and kind == suggestion.get("kind"):
            accepted = {key: value for key, value in suggestion.items() if key not in ("kind", "speaker")}
            carried = carried or _same_date_except_confirmation(entry, accepted)
        if move_over:
            # Re-kind only: all date content and provenance still equal the stored mapping.
            carried = carried or any(entry == stored for stored in mapped.values())
        if carried:
            continue
        words = entry.get("words") or ""
        if not words or not from_words(words, source_words):
            raise BrainError(
                ErrorCode.TIME_NOT_FROM_WORDS, "Date words must come from this change's source words",
                details={"record_id": proposed.id, "field": f"data.{kind if kind in ('expected', 'needs.expected') else 'dates.' + kind}"},
            )
        hub_reading = str(entry.get("read_by", "")).startswith("system:") and entry.get("read_by") == actor
        if entry.get("read_by") != "code" and not hub_reading and kind != "done":
            if any(entry.get(key) and entry[key] < entry["read_on"] for key in ("day", "start", "end")):
                raise BrainError(
                    ErrorCode.TIME_NOT_FROM_WORDS, "An agent-read date cannot precede its read_on day",
                    details={"record_id": proposed.id, "field": f"data.dates.{kind}"},
                )
        if kind == "deadline" and not data.get("owed_to") and not hub_reading:
            replacing = replaces == "deadline" and old is not None
            old_words = old.get("words", "") if replacing else ""
            if not _DEADLINE_WORD.match(words.strip()) and not _DEADLINE_WORD.match(old_words.strip()):
                raise BrainError(
                    ErrorCode.TIME_NOT_FROM_WORDS, "A deadline needs a deadline word or owed_to",
                    details={"record_id": proposed.id, "field": "data.dates.deadline"},
                )

    # R13: including clearing a kind Nic previously named.
    earlier_called = previous.data.get("called") if previous else None
    if data.get("called") != earlier_called and not owner_words():
        raise BrainError(ErrorCode.NEEDS_OWNER_WORDS, "Changing called requires Nic's own words", details={"record_id": proposed.id, "field": "data.called"})

    # R14: a changed proposal names its caller; confirming alone retains its proposer.
    stake = data.get("stake")
    earlier_stake = previous.data.get("stake") if previous else None
    if stake != earlier_stake:
        if ((stake or {}).get("confirmed") is True or (earlier_stake or {}).get("confirmed") is True) and not owner_words():
            raise BrainError(ErrorCode.NEEDS_OWNER_WORDS, "Confirming or changing a confirmed stake requires Nic's own words", details={"record_id": proposed.id, "field": "data.stake"})
        if stake and _stake_proposal(stake) != _stake_proposal(earlier_stake) and stake.get("proposed_by") != actor:
            raise BrainError(ErrorCode.ACTOR_MISMATCH, "data.stake.proposed_by must equal the authenticated caller", details={"record_id": proposed.id, "actor": actor, "field": "data.stake.proposed_by"})

    # R12 (Ciel redesign N5, step C §13): any agent may propose high importance, saved
    # unconfirmed. confirmed: true needs Nic's own words, like accepting a level (R1), and so
    # does changing or removing importance he already confirmed: nothing he confirmed is
    # quietly undone (R3). A new or restated proposal names its proposer, who must be the
    # authenticated caller (as R10); confirming alone keeps the original proposer.
    importance = data.get("importance")
    earlier = previous.data.get("importance") if previous is not None else None
    if importance != earlier:
        confirmed = bool(importance) and importance.get("confirmed") is True
        was_confirmed = bool(earlier) and earlier.get("confirmed") is True
        if (confirmed or was_confirmed) and not _owner_words(source):
            raise BrainError(
                ErrorCode.NEEDS_OWNER_WORDS,
                "Confirming an item's importance, or changing it once confirmed, requires Nic's own words",
                details={"record_id": proposed.id},
            )
        if importance and _proposal(importance) != _proposal(earlier) and importance.get("proposed_by") != actor:
            raise BrainError(
                ErrorCode.ACTOR_MISMATCH,
                "data.importance.proposed_by must equal the authenticated caller",
                details={"record_id": proposed.id, "actor": actor},
            )
