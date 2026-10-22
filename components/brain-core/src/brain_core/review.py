"""Deterministic daily review: which items surface at 06:00, in what order, and why.

`build_review` takes plain data GT already collected (v2 items with their path, calendar
facts, open handed-off requests, why-log history, and source availability) and decides what
belongs in the review. No model call, no IO. See docs/product/steps/e-review-and-return.md §3.

`pick_thin_tasks` chooses the at most 3 tasks Diablo asks Nic about, and `answers_by_task`
finds the answers Nic already gave (§3.1; Proposed - speed-04, off with the rest of step E).
Since the 28 Sep redesign (N12c) it also picks a started task whose where-I-stopped note has no
first step, found through the note's link to its task (`note_for_task`, N12b).

The "Stuck" section (N10b, rebuild plan slice 5) lists every task Nic started whose note has not
changed for `STUCK_DAYS` days, weekends included, every morning until he moves it, parks it or
drops it (`stuck_note`).

The "Coming up" section (N16) lists open tasks whose date is in the next `COMING_UP_DAYS` days,
most important first by step F's ranking (`orientation.rank`), and counts the later ones in one
line. `coming_up_date` is the rule "where am I?" shares.

An item with a time of day is missed once that time is `MISSED_AFTER_MINUTES` behind, the same
day, and its Missed line asks what happened; one starting before 06:00 tomorrow is tonight's, so
it counts as today (`start_passed`, `due_tonight`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping

from brain_core.language import focus_preference, plain_word
from brain_core.presentation import relative_day_words, work_words
from brain_core.records import RECORD_SCHEMA_V1, RECORD_SCHEMA_V2, Record, first_saved
from brain_core.sitreps import ProjectTasks
from brain_core.sorting import SORTING_CAP, awaits_yes, fits_two_minutes, to_sort
from brain_core.timewords import NIGHT_ENDS, SINGAPORE, today_in_singapore

WINDOW_CLOSING_DAYS = 3
PICK_OR_PARK_CAP = 10
OPEN_STATES = ("open", "waiting", "blocked")
EMPTY_REVIEW_TEXT = "Nothing due, nothing new. Calendar is clear."

# §3 (Proposed - Nic to confirm, 28 Sep): the handed-off requests in "Waiting on" are named by
# their objective, trimmed to about 60 characters, and at most 10 are listed, newest first. The
# first live dry run listed 56 raw request IDs.
WAITING_ON_REQUEST_CAP = 10
REQUEST_TITLE_CHARS = 60
# The states Nic is shown a handed-off request in (§3, §12), kept as the why-log's `record_state`.
# Work-model §7: a result closes on valid proof or an accepted independent check.
REQUEST_OPEN = "open"
REQUEST_RETURNED_UNCHECKED = "returned_unchecked"
REQUEST_DONE = "done"
REQUEST_RETURNED_PROOF = "returned_proof"
REQUEST_PROOF_UNVERIFIED = "proof_unverified"
REQUEST_STALE_DAYS = 14


def quoted_proof(proof: str) -> str:
    """Agent evidence is displayed as a bounded, escaped quotation on one line."""
    return json.dumps(proof[:300], ensure_ascii=False)

# A time of day is when something starts (N8 puts it on the calendar there). Accepted - Nic, 28 Sep
# 2026, after "what next?" picked a midnight meeting at 6:43 pm: 30 minutes after its start time,
# the length its calendar entry gets, it is missed, the same day, and every ask words it as a
# question about what happened; "what next?" never picks it. Before this only its date counted,
# so it stayed "due today" all day.
MISSED_AFTER_MINUTES = 30
MISSED_START_QUESTION = "Did it happen? Done, move or drop?"
MISSED_QUESTION = "Recommit, change, defer or drop?"

# §3.1 (Proposed - Nic to confirm): at most 3 clarifying questions a morning, oldest first.
CLARIFY_CAP = 3
CLARIFY_RULE = "clarify_thin_task"
CLARIFY_REF_PREFIX = "clarify:"
# §3.1 as the redesign changed it (N12c; Accepted - Nic, 28 Sep 2026): a task Nic started whose
# note has no next step is asked about in these fixed words, within the same cap of 3.
CLARIFY_FIRST_STEP_RULE = "clarify_first_step"
FIRST_STEP_QUESTION = "What's the first step for '{title}'?"
# Notes for work handed through Gremlin Town (session gt-<request_id>, kept by the hub or by an
# agent) hold the other agent's steps: they belong under "Waiting on", never "In progress", and
# never give Nic a first step (rebuild plan G2).
HANDOFF_NOTE_PREFIX = "gt-"
# The key orientation's yes gave a task's note before notes named their task (step F §6).
ITEM_NOTE_PREFIX = "item-"
# §3 "Stuck" (N10b; Accepted - Nic, 28 Sep 2026; rebuild plan G2, G5): a task Nic started whose
# own note last changed this many days ago or more, weekends included, counted in Singapore
# calendar days (Proposed - Nic to confirm: a note changed on Friday, at any hour, is stuck in
# Monday's review). Fixed words, like Missed: the date of the note's last change, which stays the
# same every morning until the note moves; no motive, no "again", no count.
STUCK_DAYS = 3
STUCK_RULE = "stuck"
STUCK_TEXT = "{title} — started; no change since {date}"
# §3 "Coming up" (N16; Accepted - Nic, 28 Sep 2026): open tasks due in the next 7 days, or whose
# window closes after "Windows closing" and within them, most important first (step F §3), at
# most `COMING_UP_CAP` (Proposed - Nic to confirm, as Pick or park's), then one line counting
# the rest. Tasks further out are counted in one "later" line. Being listed here never counts
# towards "tends to stall" (step F §5): it asks nothing (redesign default 11).
COMING_UP_DAYS = 7
COMING_UP_CAP = 10
COMING_UP_RULE = "coming_up"
COMING_UP_MORE_REF = "__more_coming_up__"
COMING_UP_LATER_REF = "__later_coming_up__"
# How step F ranks, for both asks and "Coming up" (`orientation.rank`). Points are the Ciel and
# Brain Home PRD's §5 (Accepted - Nic, 28 Sep 2026); the nine rules stay one setting away,
# `[orientation] ranking = "rules"`, until a week of real use ("Yes, keep a way back"). Defined
# here, not in orientation, because orientation imports this module.
RANKING_POINTS = "points"
RANKING_RULES = "rules"
RANKINGS: frozenset[str] = frozenset({RANKING_POINTS, RANKING_RULES})
# Words that say nothing about a task beyond its title: the "I need to..." frame Nic's own
# save words use, plus articles, pronouns and prepositions (whatever follows a preposition
# still counts). Apostrophes are dropped before matching ("I'll" -> "ill"). Proposed - Nic to
# confirm.
FILLER_WORDS: frozenset[str] = frozenset(
    """
    a an the and or but so also just then
    i im ive ill id me my myself we us our
    nic nics he him his
    need needs needed have has had got gotta must will should shall would want wants
    going gonna said says asked asks told
    to do does doing it its this that these those
    about for on at in with of from by up
    please pls ok okay remind remember
    later soon sometime someday
    """.split()
)
_WORD = re.compile(r"\w+")

SECTION_TODAY = "today"
SECTION_MISSED = "missed"
SECTION_STUCK = "stuck"
SECTION_WINDOWS_CLOSING = "windows_closing"
SECTION_COMING_UP = "coming_up"
SECTION_PICK_OR_PARK = "pick_or_park"
# "Where am I?" keeps its "New suggestions"; the 06:00 review's became "To sort" (PRD §4.1).
SECTION_NEW_SUGGESTIONS = "new_suggestions"
SECTION_TO_SORT = "to_sort"
TO_SORT_RULE = "to_sort"
TO_SORT_MORE_REF = "__more_to_sort__"
SECTION_WAITING_ON = "waiting_on"
SECTION_CALENDAR_ENTRIES = "calendar_entries"
SECTION_COULD_NOT_SEE = "could_not_see"
# Ciel and Brain Home PRD §4.2 (build plan slice 5).
SECTION_TO_CHASE = "to_chase"
SECTION_NO_NEXT_ACTION = "no_next_action"
SECTION_WEEKLY_REVIEW = "weekly_review"
# §4.9: anything waited on this many days or more, or past the date it was expected, is to chase
# (gsd's IsAgingWaiting).
CHASE_AFTER_DAYS = 7
# Accepted - Nic, 28 Sep 2026 ("10, then 'N more'"): every section shows at most this many things,
# then how many more, so a full morning stays readable. Calendar events are never cut.
SECTION_CAP = 10
# §4.8 (Nic, 28 Sep: "Friday afternoon"): the weekly review's day, and the review's line that day.
WEEKLY_REVIEW_WEEKDAY = 4
WEEKLY_REVIEW_TEXT = "Ready this afternoon; ask for it when you have time"

# PRD §4.2's order: what's fixed today, what slipped, what's coming, then what to sort, chase and
# pick for the day.
SECTION_ORDER: tuple[str, ...] = (
    SECTION_TODAY,
    SECTION_MISSED,
    SECTION_STUCK,
    SECTION_WINDOWS_CLOSING,
    SECTION_COMING_UP,
    SECTION_TO_SORT,
    SECTION_TO_CHASE,
    SECTION_NO_NEXT_ACTION,
    SECTION_PICK_OR_PARK,
    SECTION_WAITING_ON,
    SECTION_CALENDAR_ENTRIES,
    SECTION_WEEKLY_REVIEW,
    SECTION_COULD_NOT_SEE,
)
# Lines in these sections carry no number: nothing in them is picked by number.
_UNNUMBERED_SECTIONS = frozenset({SECTION_MISSED, SECTION_WEEKLY_REVIEW})

SECTION_TITLES: dict[str, str] = {
    SECTION_TODAY: "Today",
    SECTION_MISSED: "Missed",
    SECTION_STUCK: "Stuck",
    SECTION_WINDOWS_CLOSING: "Windows closing",
    SECTION_COMING_UP: "Coming up",
    # §4.2: "pick 3 to 7 things for today" (gsd's small days); it was "Pick or park".
    SECTION_PICK_OR_PARK: "Your day: pick 3 to 7",
    SECTION_TO_SORT: "To sort",
    SECTION_TO_CHASE: "To chase",
    SECTION_NO_NEXT_ACTION: "Projects with no next action",
    SECTION_WEEKLY_REVIEW: "Weekly review",
    SECTION_WAITING_ON: "Waiting on",
    SECTION_CALENDAR_ENTRIES: "Calendar entries to add",
    SECTION_COULD_NOT_SEE: "Couldn't see",
}

_V1_TERMINAL_STATES: dict[str, frozenset[str]] = {
    "capture": frozenset({"archived"}),
    "commitment": frozenset({"done", "cancelled", "superseded"}),
}


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """One calendar fact for today, as step D's reader returns it."""

    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    description: str = ""
    # Step I (placement engine): an event marked "free" in Google takes no time, and the engine's
    # own entries say which they are ("block" or "slot"), so it never reads them as meetings.
    busy: bool = True
    exosuit_role: str | None = None


@dataclass(frozen=True, slots=True)
class ItemRecord:
    """A record with the path GT read it from, so the why-log can pin the version (§7)."""

    record: Record
    path: str

    @property
    def sha256(self) -> str:
        return self.record.sha256


@dataclass(frozen=True, slots=True)
class OpenRequest:
    """A handed-off GT request considered for a daily or Friday review.

    ``objective`` is the request's own objective: how the review names it (§3). ``work_state``
    and ``check`` are step B's; they tell a result that is back but unchecked from work still
    being done. `proof` and `last_activity` also let a proved return appear once and
    an idle request leave the daily list for Friday's stale section."""

    id: str
    handed_to: str
    since: date
    objective: str = ""
    work_state: str = ""
    check: str = "none"
    proof: str | None = None
    proof_verified: bool = True
    last_activity: datetime | None = None

    @property
    def state(self) -> str:
        if self.work_state == "result_returned" and self.proof:
            return REQUEST_RETURNED_PROOF if self.proof_verified else REQUEST_PROOF_UNVERIFIED
        if self.work_state == "result_returned" and self.check == "none":
            return REQUEST_RETURNED_UNCHECKED
        return REQUEST_OPEN


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One prior surfacing, read from the why-log `surfacings` table (§7)."""

    item_ref: str
    channel: str
    rule: str
    record_sha256: str
    record_state: str | None
    surfaced_at: datetime
    response: str | None = None
    responded_at: datetime | None = None
    record_path: str | None = None


@dataclass(frozen=True, slots=True)
class OpenNote:
    """One open where-I-stopped note (step B `continuity.list`): its key, its next step (empty when
    none was given, N12c), when it last changed, and the task it belongs to (N12b), if it names one.
    GT reads these from Brain Home; only the newest version of each note counts."""

    session_id: str
    next_step: str
    updated_at: datetime
    task_id: str | None = None
    intent: str = ""
    record_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewInputs:
    """``facts`` are Brain Home's v2 facts, read only to find Nic's answers (§3.1). ``None``
    means they were not read: then no task is asked about and no answer is shown.

    ``open_notes`` are the open where-I-stopped notes, each with when it last changed (N12b, N12c,
    N10b). ``None`` means they were not read: then no started task is asked for its first step
    and the review has no "Stuck" section.

    ``preferences`` are Brain Home's preference records, read only for Nic's focus projects,
    which rank "Coming up" (N16, step F §3 rule 5). Empty, no task counts as a focus project's.

    ``ranking`` is how step F ranks, here and in both asks: `RANKING_POINTS`, or the nine rules
    (`RANKING_RULES`) while that way back is kept.

    ``project_tasks`` are Nic's projects' open and blocked tasks from the hub's task sitreps, read
    only to tell whether a focus project has a next action (PRD §4.10). ``None`` means they weren't
    read, and then no focus project is flagged: only goals are."""

    items: tuple[ItemRecord, ...]
    calendar_events: tuple[CalendarEvent, ...]
    calendar_marked_ids: frozenset[str]
    open_requests: tuple[OpenRequest, ...]
    history: tuple[HistoryRow, ...]
    source_status: Mapping[str, str | None]
    now: datetime
    facts: tuple[ItemRecord, ...] | None = None
    open_notes: tuple[OpenNote, ...] | None = None
    preferences: tuple[Record, ...] = ()
    ranking: str = RANKING_POINTS
    project_tasks: tuple[ProjectTasks, ...] | None = None


@dataclass(frozen=True, slots=True)
class ReviewLine:
    number: int
    section: str
    text: str
    item_ref: str | None
    rule: str
    record_sha256: str | None = None
    record_path: str | None = None
    record_state: str | None = None
    fields_used: Mapping[str, Any] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    allowed_answers: tuple[str, ...] = ()
    offered: tuple[str, ...] = ()
    suggested_name: str | None = None
    suggested_title: str | None = None
    suggested_label: str | None = None


@dataclass(frozen=True, slots=True)
class ClarifyTask:
    """One thin task Diablo asks Nic about, in a message separate from the review (§3.1).

    ``number`` is the task's line in today's review, or ``None`` when the Pick or park cap left
    it out. ``item_ref`` is its own why-log reference, so a response to the task's review line
    never lands on this row.

    A started task with no first step (N12c) carries ``rule`` ``clarify_first_step``, the fixed
    ``question`` to ask, and the ``session_id`` of the note Nic's answer goes into as its next
    step. A thin task leaves both ``None``: Diablo words that question himself.
    """

    record_id: str
    title: str
    number: int | None
    record_sha256: str
    record_path: str
    fields_used: Mapping[str, Any] = field(default_factory=dict)
    rule: str = CLARIFY_RULE
    question: str | None = None
    session_id: str | None = None

    @property
    def item_ref(self) -> str:
        return CLARIFY_REF_PREFIX + self.record_id


@dataclass(frozen=True, slots=True)
class SortEntry:
    """One item to sort this morning (PRD §4.1), beside the review like the clarifying questions:
    an agent's suggestion with its pre-sort, for Nic's yes, or an unsorted task Diablo pre-sorts
    himself. ``number`` is its line under "To sort"; ``fields_used`` holds what it already has
    (level, block, first step, rough length, who to raise it with, links) and whether it fits
    in two minutes."""

    record_id: str
    title: str
    number: int
    record_sha256: str
    record_path: str
    fields_used: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Review:
    lines: tuple[ReviewLine, ...]
    sections: tuple[str, ...]
    clarify: tuple[ClarifyTask, ...] = ()
    to_sort: tuple[SortEntry, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.lines


@dataclass(frozen=True, slots=True)
class Morning:
    """The short 06:00 message and four numbered parts: Look, Catch up, New, Your day.

    Parts contain ReviewLine values with lasting item identities and pinned shown versions.
    Only ``short`` goes to the Telegram courier; the full parts go in the review request.
    """

    short: str
    parts: tuple[Review, ...]


@dataclass(frozen=True, slots=True)
class MorningBootstrap:
    """First new-style review facts from the actual pinned move-over input snapshot.

    ``input_sha`` names that immutable snapshot; ``known_first_ids`` contains every
    item identity in it. ``completed`` says a successful sent morning consumed
    bootstrap; the known identities remain available after that. These are
    derivative review facts, not Brain records or invented surfacing history.
    """

    input_sha: str
    known_first_ids: frozenset[str]
    completed: bool = False


@dataclass(frozen=True, slots=True)
class ImportantEmail:
    """Read-only Look facts, never a Brain Home record or an answer prompt.

    Look's fields_used carries these fields under rule important_email; its
    reference is __important_email__:<message_id>, independent of item identities.
    """

    message_id: str
    sender: str
    subject: str
    score: int
    reason: str
    link: str
    flagged_at: datetime


@dataclass(frozen=True, slots=True)
class MorningInputs:
    """Collected morning facts; calendar markers and legacy clarification inputs are absent.

    ``previous_review_at`` is the last successful review's actual run timestamp.
    With no explicit bootstrap, None preserves the pure no-prior-review behavior.
    The host requires pinned bootstrap facts before its first new-style review.
    ``shown_first_ids`` comes only from actually shown why-log references.
    """

    items: tuple[ItemRecord, ...]
    calendar_events: tuple[CalendarEvent, ...]
    open_requests: tuple[OpenRequest, ...]
    history: tuple[HistoryRow, ...]
    source_status: Mapping[str, str | None]
    now: datetime
    preferences: tuple[Record, ...] = ()
    ranking: str = RANKING_POINTS
    previous_review_at: datetime | None = None
    bootstrap: MorningBootstrap | None = None
    shown_first_ids: frozenset[str] = frozenset()
    important_emails: tuple[ImportantEmail, ...] = ()


MORNING_PARTS = ("look", "catch_up", "new", "your_day")
MORNING_TITLES = ("Look", "Catch up", "New", "Your day: pick 3 to 7")


@dataclass(frozen=True, slots=True)
class NameCandidate:
    item_ref: str
    record_sha256: str
    title: str
    first_words: str
    tasks: tuple[str, ...]
    labels: tuple[str, ...]


def asks_for_name(record: Record) -> bool:
    """Only a pending name question or project/goal classification can ask for a name."""
    return any(
        mark.get("mark") == "looks_like" and mark.get("kind") in {"project", "goal"}
        or mark.get("mark") == "question" and bool(re.search(
            r"\b(?:name\s*\?|(?:what|its|the|a|your)\s+name\b|call\s+it\b)",
            mark.get("text", ""), re.I))
        for mark in record.data.get("review_marks", ())
    )


def list_name_candidates(items: Iterable[ItemRecord], now: datetime) -> tuple[NameCandidate, ...]:
    """Read-only, uncapped name requests over current reader identities, with original context."""
    from brain_core import work

    items = tuple(items)
    index = work.index_things(entry.record for entry in items)
    originals = {entry.record.id: entry.record for entry in items}
    candidates = []
    for fid, record in sorted(index.items()):
        if not asks_for_name(record) or work.status_of(record, index, now) == "ended" or record.data.get("repeat"):
            continue
        first = originals.get(fid)
        # Superseded first versions may be archived; the body and inherited
        # evidence retain the original note while current source.words may be "yes".
        first_words = ((first.source or {}).get("words") or first.body) if first is not None else (
            record.body or next((e.excerpt for e in record.evidence if e.source_type == "session_message"), "")
            or (record.source or {}).get("words", ""))
        children = []
        for child in index.values():
            parent = child.data.get("parent")
            while parent in index:
                if parent == fid:
                    children.append(child.title)
                    break
                parent = index[parent].data.get("parent")
        candidates.append(NameCandidate(fid, originals[record.id].sha256, record.title,
            first_words, tuple(children), tuple(record.data.get("labels", ()))))
    return tuple(candidates)


def with_name_suggestions(morning: Morning, names: Mapping[str, tuple[str, str]]) -> Morning:
    """Attach only to a matching pending name line; a stale answer never renames a record."""
    def attach(line):
        candidate = names.get(line.item_ref)
        if (candidate is None or candidate[0] != line.record_sha256 or line.rule != "needs_you"
                or not {"title", "project", "goal"}.intersection(line.offered)):
            return line
        return replace(line, suggested_name=candidate[1],
                       fields_used=dict(line.fields_used, suggested_name=candidate[1]),
                       offered=tuple(dict.fromkeys((*line.offered, "title"))))
    return replace(morning, parts=tuple(replace(part, lines=tuple(attach(line) for line in part.lines))
                                        for part in morning.parts))


def list_tidy_candidates(inputs: MorningInputs) -> tuple[NameCandidate, ...]:
    """The New items this morning will show, with original words; pure and read-only."""
    from brain_core import work
    ids = {line.item_ref for line in build_morning(inputs).parts[2].lines if line.record_sha256}
    index = work.index_things(entry.record for entry in inputs.items)
    raw = {entry.record.id: entry.record for entry in inputs.items}
    candidates = []
    for fid in sorted(ids):
        record = index[fid]
        if record.state != "open" or record.data.get("repeat"):
            continue
        original = raw.get(fid, record)
        words = original.body or (original.source or {}).get("words", "")
        tasks = tuple(child.title for child in index.values() if child.data.get("parent") == fid)
        candidates.append(NameCandidate(fid, raw[record.id].sha256, record.title, words,
                                        tasks, tuple(record.data.get("labels", ()))))
    return tuple(candidates)


def with_tidy_suggestions(morning: Morning, suggestions: Mapping[str, tuple[str, Mapping[str, str]]]) -> Morning:
    """Pinned proposals only. The door requires Save before these values count."""
    def attach(line):
        candidate = suggestions.get(line.item_ref)
        if candidate is None or candidate[0] != line.record_sha256 or line.section != "new":
            return line
        values = {key: value for key, value in candidate[1].items()
                  if key in {"suggested_name", "suggested_title", "suggested_label"} and value}
        if not values:
            return line
        offered = list(line.offered)
        if values.get("suggested_name") or values.get("suggested_title"):
            offered.append("title")
        if values.get("suggested_label"):
            offered.append("label")
        return replace(line, **values, offered=tuple(dict.fromkeys((*offered, "save"))),
                       allowed_answers=tuple(dict.fromkeys((*line.allowed_answers, "save", "rename"))),
                       fields_used=dict(line.fields_used, **values, tidy_suggestion=True))
    return replace(morning, parts=tuple(replace(part, lines=tuple(attach(line) for line in part.lines))
                                        for part in morning.parts))


def _morning_context(record: Record, index, now: datetime) -> str:
    """Reader-derived next, progress and labels, without inventing an action."""
    from brain_core import work

    next_item = work.next_of(record, index, now)
    progress = work.progress_of(record, index)
    bits = []
    if progress["total"]:
        bits.append(progress["text"])
    if next_item["item_ref"] != work.first_id(record) or next_item["text"] != record.title:
        bits.append(f"next: {next_item['text']}")
    labels = record.data.get("labels", ())
    if labels:
        bits.append(" · ".join(labels))
    return "; ".join(bits)


def _morning_offers(answers: tuple[str, ...]) -> tuple[str, ...]:
    """Door move names accompanying the visible answer names (W6/W12's existing seam)."""
    aliases = {
        "recommit": "date", "defer": "park",
        "change": "organise", "what's next": "next_action", "next action": "next_action",
        "yes": "accept", "correct": "title", "clarify": "organise",
        "picked_today": "date",
    }
    return tuple(dict.fromkeys((*answers, *(aliases[a] for a in answers if a in aliases))))


def _morning_question(need, record: Record, index) -> str:
    """Word a reader-selected mark as a question; original question marks stay verbatim."""
    for mark in record.data.get("review_marks", ()):
        words = mark.get("text") or mark.get("part") or mark["mark"].replace("_", " ")
        if need["text"] != words:
            continue
        if mark["mark"] == "looks_like":
            return f"Looks like a {mark['kind']}. Yes?"
        if mark["mark"] == "ask_stake":
            return "What's at stake?"
        if mark["mark"] == "agents_wording":
            return f"Agent's wording: {words}. Keep, or say it your way?"
        if mark["mark"] == "maybe_same":
            target = index.get(mark.get("as"))
            return f"Same as '{target.title if target else words}'? Merge, or leave separate?"
        if mark["mark"] == "spelling":
            return f"Keep '{words}', or correct it?"
    return need["text"]


def _review_question_words(text: str, *, ensure_question: bool = False) -> str:
    """Address Nic in generated prompts, including marks saved before W21."""
    text = re.sub(r"Its finish line, in his words\??", "Its finish line, in your words?", text)
    for before, after in (("his words", "your words"), ("his message", "your message"),
                          ("his reason", "your reason"), ("Nic's words", "your words")):
        text = text.replace(before, after)
    if ensure_question and not text.endswith("?"):
        text = text.rstrip(". ") + "?"
    # Keep each question's punctuation, with a visible separator between questions.
    return re.sub(r"(?<=\?)\s+(?!·)(?=[^?]*\?)", " · ", text)


def _question_key(text: str) -> str:
    """Compare the question, leaving off a preceding statement or separator."""
    return text.rsplit(". ", 1)[-1].strip(" ·").casefold()


def needs_review_length(record: Record, index, now: datetime) -> bool:
    """A current day-bearing thing needs its owner's length; never invent minutes."""
    from brain_core import work
    if (record.state != "open" or record.data.get("repeat")
            or work.status_of(record, index, now) in {"parked", "ended"} or work.length_of(record, index) > 0):
        return False
    dates = work.dates_of(record, index)
    return any(any(value.get(key) for key in ("day", "start", "end"))
               for kind in ("planned", "deadline", "appointment", "not_before")
               if (value := dates.get(kind)))


def build_morning(inputs: ReviewInputs | MorningInputs, *, today: date | None = None,
                  opening_deadlines: bool = False, new_lengths: bool = False,
                  section_cap: int = SECTION_CAP, prepared_index=None) -> Morning:
    """Build the work-02 morning from collected inputs through the sole work reader.

    Pure: no IO or model calls. Each part has at most ten entries plus an overflow line;
    numbers continue across parts. ``build_review`` remains the work-01 API.
    """
    from brain_core import work
    from brain_core.orientation import focus_project_ids, rank

    if type(section_cap) is not int or not 1 <= section_cap <= 2000:
        raise ValueError("section_cap must be from one to 2000")

    today = today if today is not None else today_in_singapore(inputs.now)
    index = work.index_things(entry.record for entry in inputs.items) if prepared_index is None else prepared_index
    raw = {entry.record.id: entry for entry in inputs.items}
    previous_review_at = getattr(inputs, "previous_review_at", None)
    bootstrap = getattr(inputs, "bootstrap", None)
    known = getattr(inputs, "shown_first_ids", frozenset())
    if bootstrap is not None:
        known = known | bootstrap.known_first_ids
    rows: dict[str, list[ReviewLine]] = {name: [] for name in MORNING_PARTS}
    fixed: list[str] = []

    def add(part, record, text, rule, *, answers=(), fields=None):
        entry = raw[record.id] if record is not None else None
        rows[part].append(ReviewLine(
            number=0, section=part, text=text,
            item_ref=work.first_id(record) if record is not None else f"__{rule}__",
            rule=rule, record_sha256=entry.sha256 if entry else None,
            record_path=entry.path if entry else None, record_state=entry.record.state if entry else None,
            fields_used=fields or {}, allowed_answers=tuple(answers), offered=_morning_offers(tuple(answers)),
        ))

    def history_for(record):
        ids = {work.first_id(record), record.id, *record.supersedes}
        return tuple(row for row in inputs.history if row.item_ref in ids and row.channel == "review")

    # Reader metadata remains intact, including done parts used by progress_of.
    active = [r for r in index.values() if work.status_of(r, index, inputs.now) not in {"ended", "parked"} or work.needs_nic(r, index, inputs.now)]
    needs = {work.first_id(r): work.needs_nic(r, index, inputs.now, inputs.history,
                                            with_answer_kind=True) for r in active}
    picked = frozenset(row.item_ref for row in inputs.history if row.response == "picked_today" and today_in_singapore(row.responded_at or row.surfaced_at) == today)

    def is_picked(record):
        # Picking a task includes its open parts, even when a part retains an older plan.
        while record is not None:
            planned = work.dates_of(record, index).get("planned") or {}
            if (work.first_id(record) in picked or planned.get("day") == today.isoformat()
                    or planned.get("start", "z") <= today.isoformat() <= planned.get("end", "")):
                return True
            record = index.get(record.data.get("parent"))
        return False

    ranked = rank(inputs.items, today, picked_today=picked, focus_projects=focus_project_ids(inputs.preferences), now=inputs.now, ranking=inputs.ranking, prepared_index=index)
    order = {work.first_id(candidate.item.record): i for i, candidate in enumerate(ranked) if candidate.item is not None}
    for record in active:
        dates = work.dates_of(record, index)
        record_needs = needs[work.first_id(record)]
        passed = {n["kind"] for n in record_needs}
        descriptions = []
        for kind in ("appointment", "deadline", "planned", "not_before"):
            if opening_deadlines and kind == "deadline":
                continue
            value = dates.get(kind) or {}
            day = value.get("day")
            is_today = day == today.isoformat() or (kind == "planned" and value.get("start", "z") <= today.isoformat() <= value.get("end", ""))
            if not is_today or (kind == "appointment" and "did_it_happen" in passed):
                continue
            label = {"appointment": f"at {_clock_word(value['at'])}" if value.get("at") else "appointment today",
                     "deadline": "deadline today", "planned": "planned for today", "not_before": "wait ends today"}[kind]
            descriptions.append(label)
            if kind in {"appointment", "deadline"}:
                fixed.append(f"{record.title} — {label}")
        # A person's expected day is a chase prompt, not proof their work finished.
        if descriptions:
            add("look", record, f"{record.title} — {', '.join(descriptions)}", "today", fields={"dates": dates})

    for event in sorted(inputs.calendar_events, key=lambda e: (e.start, e.title)):
        if event.exosuit_role in {"block", "slot"} or today_in_singapore(event.start) != today:
            continue
        text = f"{event.title} — {'all day' if event.all_day else 'at ' + _clock_word(event.start.astimezone(SINGAPORE).strftime('%H:%M'))}"
        fixed.append(text)
        add("look", None, text, "calendar_today", fields={"title": event.title, "start": event.start.isoformat()})
    if not fixed:
        add("look", None, "Today: Nothing at a set time.", "nothing_timed")

    if opening_deadlines:
        for offset, label, rule in ((0, "Due today", "due_today"), (1, "Due tomorrow", "due_tomorrow")):
            due_day = (today + timedelta(days=offset)).isoformat()
            for record in sorted(active, key=lambda r: (order.get(work.first_id(r), len(order)), work.first_id(r))):
                deadline = work.dates_of(record, index).get("deadline") or {}
                if deadline.get("day") == due_day and work.status_of(record, index, inputs.now) not in {"ended", "parked"}:
                    add("look", record, f"{label}: {record.title}", rule, fields={"deadline": deadline})

    emails = getattr(inputs, "important_emails", ())
    for email in emails:
        sender, subject, reason = (" ".join(value.split()) for value in (email.sender, email.subject, email.reason))
        text = f"Important email: {sender} — {subject}"
        if reason:
            text += f" ({reason})"
        text += f"\n{email.link}"
        rows["look"].append(ReviewLine(
            0, "look", text, f"__important_email__:{email.message_id}", "important_email",
            fields_used=dict(message_id=email.message_id, sender=email.sender, subject=email.subject,
                             score=email.score, reason=email.reason, link=email.link,
                             flagged_at=email.flagged_at.isoformat()),
        ))

    for record in sorted(active, key=lambda record: (order.get(work.first_id(record), len(order)), work.first_id(record))):
        dates = work.dates_of(record, index)
        upcoming = []
        for kind in (() if opening_deadlines else ("deadline", "appointment", "not_before")):
            value = dates.get(kind) or {}
            day = value.get("day")
            if day and today < date.fromisoformat(day) <= today + timedelta(days=COMING_UP_DAYS):
                label = "on" if kind == "not_before" and work.event_day(dates) else kind.replace('_', ' ')
                words = f"{label} {_date_word(day, today)}"
                if value.get("at"):
                    words += f" at {_clock_word(value['at'])}"
                if value.get("event"):
                    words += f" ({value['event']})"
                upcoming.append(words)
        if upcoming:
            add("look", record, f"Coming up: {record.title} — {', '.join(upcoming)}", "coming_up", fields={"dates": dates})
    for source, status in inputs.source_status.items():
        if status not in (None, "", "ok"):
            add("look", None, f"{source}: {status}" if source == "Brain Home size" else f"Couldn't see {source}: {status}",
                "brain_home_size" if source == "Brain Home size" else "source_unavailable", fields={"source": source, "status": status})

    catch_order = {name: i for i, name in enumerate(("missed", "did_it_happen", "carried_over", "to_chase", "nothing_next", "stalled", "carried_over_3_times", "over_two_hours"))}
    catch = []
    new_groups: dict[str, list] = {"new": [], "needs": [], "two": []}
    pending_questions = {}
    for record in active:
        if work.has_unfinished_prerequisites(record, index):
            continue
        fid = work.first_id(record)
        prior = history_for(record)
        data = record.data
        pending = []
        for need in needs[fid]:
            kind = need["kind"]
            if kind == "needs_you":
                pending.append(need)
                continue
            if any(row.response == "answered" and row.record_sha256 == raw[record.id].sha256 for row in prior):
                continue
            if kind == "over_two_hours" and any(row.rule == kind and row.record_sha256 == raw[record.id].sha256 for row in prior):
                continue
            # A parent's carry already names its next part; do not ask that part again.
            if kind == "carried_over" and is_picked(record):
                continue
            if kind == "carried_over" and data.get("parent") in index:
                parent = index[data["parent"]]
                plan = work.dates_of(record, index).get("planned") or {}
                parent_plan = work.dates_of(parent, index).get("planned") or {}
                if (plan.get("day") or plan.get("end")) == (parent_plan.get("day") or parent_plan.get("end")):
                    continue
            if kind == "stalled" and any(n["kind"] in {"missed", "did_it_happen"} for n in needs[fid]):
                continue
            text = need["text"]
            if kind == "did_it_happen":
                value = work.dates_of(record, index)["appointment"]
                day = _date_word(value['day'], today)
                when = (f"{day} at {_clock_word(value['at'])}" if day in {"yesterday", "today", "tomorrow"}
                        or day.startswith("this ") else f"at {_clock_word(value['at'])} on {day}")
                text = f"was {when}. Did it happen? {text}"
            elif kind == "missed":
                day = work.dates_of(record, index)["deadline"]["day"]
                text = f"missed since {_date_word(day, today)} ({days_word((today - date.fromisoformat(day)).days)}). {text}"
            elif kind == "carried_over":
                value = work.dates_of(record, index)["planned"]
                text = f"planned for {_date_word(value.get('day') or value['end'], today)}. {text}"
            elif kind == "carried_over_3_times":
                text = "Carried over 3 times. Still want this? If yes, what's a smaller first step?"
            elif kind == "over_two_hours":
                text = "This is long. Parts?"
            context = _morning_context(record, index, inputs.now)
            if context and kind in {"carried_over", "nothing_next"}:
                text += f" ({context})"
            catch.append((catch_order.get(kind, 99), record, text, need))
        if pending:
            if not any(row.response == "answered" and row.record_sha256 == raw[record.id].sha256 for row in prior):
                pending_questions[fid] = pending
                new_groups["needs"].append((record, "", "needs_you",
                    tuple(dict.fromkeys(a for n in pending for a in n["allowed_answers"]))))
        elif not any(row.rule in {"new_nothing_needed", "two_minutes"} and row.record_sha256 == raw[record.id].sha256 for row in prior):
            if (data.get("level") == "accepted" and not data.get("parent")
                    and fid not in known
                    and (bootstrap is not None and not bootstrap.completed
                         or previous_review_at is None or first_saved(raw[record.id].record) > previous_review_at)):
                small = 0 < work.length_of(record, index) <= 2
                group = "two" if small else "new"
                new_groups[group].append((record, "Two minutes or less. Do it now?" if small else "New, nothing needed",
                    "two_minutes" if small else "new_nothing_needed", work.allowed_answers("two_minutes") if small else ()))
    if new_lengths:
        # A date save is followed by a fresh read. It may no longer count as a new
        # capture, but its missing length still needs the next question.
        new_ids = {work.first_id(record) for group in new_groups.values() for record, *_ in group}
        for record in active:
            if work.has_unfinished_prerequisites(record, index):
                continue
            if needs_review_length(record, index, inputs.now):
                new_groups["needs"].append((record, "How long?", "needs_length",
                                             ("15 min", "30 min", "1 hour", "2 hours")))
            elif (record.state == "open" and work.first_id(record) not in new_ids
                    and not work.dates_of(record, index).get("planned")
                    and any(row.rule == "new_schedule" for row in history_for(record))):
                new_groups["new"].append((record, "When will you do it?", "new_schedule",
                                           ("today", "another day", "park", "drop")))
        new_groups["new"] = [
            (record, "When will you do it?", "new_schedule", ("today", "another day", "park", "drop"))
            if rule == "new_nothing_needed" and not work.dates_of(record, index).get("planned")
            else (record, text, rule, answers)
            for record, text, rule, answers in new_groups["new"]
            if not needs_review_length(record, index, inputs.now)
        ]
    for _, record, text, need in sorted(catch, key=lambda row: (row[0], row[1].created_at, work.first_id(row[1]))):
        add("catch_up", record, f"{record.title} — {_review_question_words(text)}", need["kind"], answers=need["allowed_answers"], fields={"need": need["kind"]})
    # A returned proof is an attributed, escaped fact. Ordinary waiting work stays weekly.
    for request in inputs.open_requests:
        consumed = any(row.item_ref == request.id and row.rule == "request_returned_proof" and row.channel == "review" for row in inputs.history)
        if request.state == REQUEST_RETURNED_PROOF:
            if not consumed:
                add("catch_up", None, f"{request_title(request.id, request.objective)} — back, with proof: {quoted_proof(request.proof or '')}", "request_returned_proof", fields={"proof": request.proof})
                rows["catch_up"][-1] = replace(rows["catch_up"][-1], item_ref=request.id, record_state=request.state)
        elif request.state == REQUEST_PROOF_UNVERIFIED:
            add("catch_up", None, f"{request_title(request.id, request.objective)} — back, proof unverified: {quoted_proof(request.proof or '')}", "request_proof_unverified")
            rows["catch_up"][-1] = replace(rows["catch_up"][-1], item_ref=request.id, record_state=request.state)
        elif request.last_activity is not None and inputs.now - request.last_activity >= timedelta(days=REQUEST_STALE_DAYS) and today.weekday() == WEEKLY_REVIEW_WEEKDAY and not any(row.item_ref == request.id and row.rule == "stale_request" for row in inputs.history):
            add("catch_up", None, f"{request_title(request.id, request.objective)} — stale (with {request.handed_to})", "stale_request", fields={"last_activity": request.last_activity.isoformat()})
            rows["catch_up"][-1] = replace(rows["catch_up"][-1], item_ref=request.id, record_state=request.state)
    # Compare questions only with Catch up lines that will actually be shown.
    # Keep the reader's needs/marks untouched: different questions still belong in New.
    catch_questions: dict[str, set[str]] = {}
    for line in rows["catch_up"][:section_cap]:
        record = index.get(line.item_ref)
        if record is None:
            continue
        questions = catch_questions.setdefault(line.item_ref, set())
        body = line.text.removeprefix(f"{record.title} — ")
        questions.update(_question_key(question) for question in re.findall(r"[^?]+\?", body))
    for group in new_groups.values():
        for record, text, rule, answers in group:
            if rule == "needs_you":
                pending = []
                for need in pending_questions[work.first_id(record)]:
                    words = _review_question_words(_morning_question(need, record, index), ensure_question=True)
                    words = " · ".join(question for question in words.split(" · ")
                                       if _question_key(question) not in catch_questions.get(work.first_id(record), ()))
                    if words:
                        pending.append((need, words))
                if not pending:
                    continue
                text = work_words("needs_you") + ": " + " · ".join(words for _, words in pending)
                answers = tuple(dict.fromkeys(a for need, _ in pending for a in need["allowed_answers"]))
            # Keep original marks beside the exact question for the door's clearing seam.
            add("new", record, f"{record.title} — {text}", rule, answers=answers,
                fields={"review_marks": [m["mark"] for m in record.data.get("review_marks", ())]})
            if rule == "needs_length":
                rows["new"][-1] = replace(rows["new"][-1], offered=("length", "rough_minutes"),
                    fields_used=dict(rows["new"][-1].fields_used, length_choices=(15, 30, 60, 120)))
            if rule == "needs_you" and record.data.get("review_marks"):
                marks = record.data["review_marks"]
                extra = []
                for mark in marks:
                    if mark["mark"] == "looks_like":
                        extra.append(mark["kind"])
                    if mark["mark"] == "question" and "parts" in mark.get("text", "").casefold():
                        extra.append("parts")
                    if mark["mark"] == "question":
                        question = mark.get("text", "").casefold()
                        if "park it" in question:
                            extra.append("park")
                        if "what's next" in question:
                            extra.append("next_action")
                        if "project" in question or "goal" in question:
                            extra.append("called")
                        if asks_for_name(replace(record, data={"review_marks": [mark]})):
                            extra.append("title")
                rows["new"][-1] = replace(rows["new"][-1], offered=tuple(dict.fromkeys((*rows["new"][-1].offered, *extra))))

    # R17: pick authority is established before caps. Catch up and blocking New
    # questions own the pick, including folded parts; side questions do not.
    side_answer_kinds = {"agents_wording", "spelling", "ask_stake", "proposed_stake",
                         "proposed_start_by", "date_suggested"}
    blocking = {fid: any(need["kind"] == "needs_you" and need["answer_kind"] not in side_answer_kinds
                        for need in record_needs) for fid, record_needs in needs.items()}
    asked = {line.item_ref for line in rows["catch_up"] if line.item_ref in index}
    asked.update(line.item_ref for line in rows["new"] if line.rule == "two_minutes"
                 or line.rule == "needs_you" and blocking[line.item_ref])
    side_questions = {line.item_ref for line in rows["new"]
                      if line.rule == "needs_you" and (not blocking[line.item_ref]
                                                       or is_picked(index[line.item_ref]))}

    def earlier_pick(record):
        while record is not None:
            if work.first_id(record) in asked and not is_picked(record):
                return True
            record = index.get(record.data.get("parent"))
        return False

    seen_projects = set()
    for candidate in sorted(ranked, key=lambda candidate: not (
            candidate.item is not None and is_picked(index[work.first_id(candidate.item.record)]))):
        if candidate.item is None:
            continue
        record = index[work.first_id(candidate.item.record)]
        if work.has_unfinished_prerequisites(record, index):
            continue
        # The earlier part's answer is the pick; a hidden capped ask still owns it.
        if earlier_pick(record):
            continue
        above = record
        project = None
        while above.data.get("parent") in index:
            above = index[above.data["parent"]]
            if work.kind_of(above, index) == "project":
                project = work.first_id(above)
                break
        if project in seen_projects:
            continue
        if project is not None:
            # A project offers its reader's next, regardless of its children's point order.
            if work.next_of(project, index, inputs.now)["item_ref"] != work.first_id(record):
                continue
            seen_projects.add(project)
        next_item = work.next_of(record, index, inputs.now)
        text = next_item["text"]
        parent = index.get(record.data.get("parent"))
        if parent:
            text += f" ({parent.title}; {work.progress_of(parent, index)['text']})"
        context = _morning_context(record, index, inputs.now)
        if context:
            text += f" · {context}"
        preselected = is_picked(record)
        if preselected:
            text += " — already picked"
        stake = record.data.get("stake") or {}
        if stake.get("confirmed") and stake.get("at_stake"):
            text += f" — {stake['at_stake']}"
        side_ref = None
        above = record
        while above is not None:
            if work.first_id(above) in side_questions:
                side_ref = work.first_id(above)
                break
            above = index.get(above.data.get("parent"))
        fields = {"preselected": preselected, "points": candidate.points}
        if side_ref is not None:
            fields["side_question_ref"] = side_ref
        add("your_day", record, text, "your_day", answers=("picked_today", "not_now"), fields=fields)

    # One informational waiting context, not the full daily waiting list. It has
    # no pick/answer authority; the reader supplies the existing next and progress.
    waiting = [record for record in active if not record.data.get("parent")
               and work.status_of(record, index, inputs.now) == "waiting"]
    if waiting:
        record = waiting[0]
        next_item = work.next_of(record, index, inputs.now)
        text = f"Waiting, can't be picked: {record.title}"
        context = _morning_context(record, index, inputs.now)
        if context:
            text += f" — {context}"
        if (work.event_day(work.dates_of(record, index))
                or work.event_day(work.dates_of(next_item["item_ref"], index))):
            text += f"; {next_item['reason']}"
        elif next_item["until"]:
            text += f"; waiting until {relative_day_words(next_item['until'], today)}"
        if len(waiting) > 1:
            text += f"; {len(waiting) - 1} other waiting things"
        add("your_day", record, text, "waiting_context", fields={"other_waiting": len(waiting) - 1})

    counts = {part: len(lines) for part, lines in rows.items()}
    parts = []
    number = 1
    for name in MORNING_PARTS:
        lines = rows[name]
        if name == "look" and len(lines) > section_cap:
            # Keep the mail timeout visible even when completed emails fill Look.
            required = [line for line in lines if line.rule == "brain_home_size" or
                        (line.rule == "source_unavailable" and line.fields_used.get("source") == "important email")]
            if any(line not in lines[:section_cap] for line in required):
                other = [line for line in lines if line not in required]
                slots = section_cap - len(required)
                lines = [*other[:slots], *required, *other[slots:]]
        shown = [replace(line, number=number + i) for i, line in enumerate(lines[:section_cap])]
        number += len(shown)
        if len(lines) > section_cap:
            shown.append(ReviewLine(0, name, f"{len(lines) - section_cap} more; ask to see them", f"__more_{name}__", "part_cap", fields_used={"remaining": len(lines) - section_cap}))
        if name == "new":
            shown.append(ReviewLine(0, name,
                "Anything new to add before you pick your day? Say it and it's saved.",
                None, "add_prompt"))
        parts.append(Review(tuple(shown), (name,)))
    # References are assigned only after caps; a hidden side question gets no
    # invented number, and its existing New question remains available on ask.
    side_numbers = {line.item_ref: line.number for line in parts[2].lines if line.rule == "needs_you"}
    parts[3] = replace(parts[3], lines=tuple(replace(line, text=line.text + (
        f" — side question, see {side_numbers[line.fields_used['side_question_ref']]}"
        if line.fields_used["side_question_ref"] in side_numbers
        else " — side question in New; ask to see more"))
        if "side_question_ref" in line.fields_used else line for line in parts[3].lines))
    short = f"Morning review, {_date_word(today.isoformat())}\n" + ("\n".join(fixed) if fixed else "Nothing at a set time.")
    if emails:
        short += f"\n{len(emails)} important unread email{'s' if len(emails) != 1 else ''}."
    short += f"\n{counts['catch_up']} to catch up, {counts['new']} new, then your day. Say ‘start’ when you're ready."
    if today.weekday() == WEEKLY_REVIEW_WEEKDAY:
        short += f"\nWeekly review: {WEEKLY_REVIEW_TEXT}."
    return Morning(short, tuple(parts))


def render_morning(morning: Morning) -> str:
    """Render all four parts verbatim for the one review request, excluding its short message."""
    parts = []
    for title, part in zip(MORNING_TITLES, morning.parts, strict=True):
        parts.append(f"## {title}")
        parts.extend(f"{line.number}. {line.text}" if line.number else line.text for line in part.lines)
        if not part.lines:
            parts.append("Nothing here today.")
    return "\n".join(parts)


def has_real_time(item: Record) -> bool:
    """A `due` date or a `window` is a real time; `on_event`/`on_return` are not (§5)."""

    timing = item.data.get("timing") or {}
    return bool(timing.get("due")) or bool(timing.get("window"))


def start_time(item: Record) -> datetime | None:
    """When an item with a due date and a time of day starts, in Singapore; ``None`` without both
    (a date alone is the whole day, and a window has no time of day)."""

    timing = item.data.get("timing") or {}
    due, at = timing.get("due"), timing.get("at")
    if not due or not at:
        return None
    return datetime.combine(date.fromisoformat(due), time.fromisoformat(at), tzinfo=SINGAPORE)


def start_passed(item: Record, now: datetime) -> bool:
    """Its start time is `MISSED_AFTER_MINUTES` or more behind ``now``: whatever it was is over."""

    start = start_time(item)
    return start is not None and now >= start + timedelta(minutes=MISSED_AFTER_MINUTES)


def due_tonight(item: Record, today: date) -> bool:
    """It starts before 06:00 the day after ``today``: tonight's small hours, which count as today,
    as "12am tonight" does in the time words (Accepted - Nic, 28 Sep 2026)."""

    timing = item.data.get("timing") or {}
    due, at = timing.get("due"), timing.get("at")
    return bool(due and at) and date.fromisoformat(due) == today + timedelta(days=1) and at < NIGHT_ENDS


def start_words(item: Record, today: date) -> str:
    """``"at 12 am today"`` or ``"at 3 pm Sun 27 Sep"``: when a timed item was set to start."""

    timing = item.data["timing"]
    day = "today" if date.fromisoformat(timing["due"]) == today else _date_word(timing["due"], today)
    return f"at {_clock_word(timing['at'])} {day}"


def missed(item: Record, now: datetime) -> bool:
    """An accepted item is missed when its due date or window end has passed while open (§6), or,
    with a time of day, once `start_passed` (Accepted - Nic, 28 Sep 2026), the same day."""

    if item.schema != RECORD_SCHEMA_V2 or item.kind != "item":
        return False
    if item.data.get("level") != "accepted" or item.state not in OPEN_STATES:
        return False
    if start_time(item) is not None:
        return start_passed(item, now)
    today = today_in_singapore(now)
    timing = item.data.get("timing") or {}
    due = timing.get("due")
    if due and date.fromisoformat(due) < today:
        return True
    window_end = (timing.get("window") or {}).get("end")
    return bool(window_end) and date.fromisoformat(window_end) < today


def coming_up_date(item: Record, today: date) -> date | None:
    """The date a task is "coming up" on (N16): its due date when that is after today, or its
    window's end when that is past "Windows closing" (more than `WINDOW_CLOSING_DAYS` days out).
    ``None`` for an undated task, and for one whose date Today, Windows closing or Missed already
    covers. Shared by the 6am review and "where am I?", so both list the same tasks."""

    timing = item.data.get("timing") or {}
    due = timing.get("due")
    if due:
        day = date.fromisoformat(due)
        return day if day > today else None
    window_end = (timing.get("window") or {}).get("end")
    if window_end:
        day = date.fromisoformat(window_end)
        return day if day > today + timedelta(days=WINDOW_CLOSING_DAYS) else None
    return None


def _date_word(iso_date: str, today: date | None = None) -> str:
    """A fixed, locale-independent rendering, e.g. "Thu 24 Sep" (never the host's locale)."""

    return relative_day_words(iso_date, today)


def _clock_word(at: str) -> str:
    """``"17:00"`` -> ``"5 pm"``, ``"09:30"`` -> ``"9:30 am"``, as step F's reasons write times."""

    hour, minute = (int(part) for part in at.split(":"))
    meridiem = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12} {meridiem}" if minute == 0 else f"{hour12}:{minute:02d} {meridiem}"


def _is_v2_item(record: Record) -> bool:
    return record.schema == RECORD_SCHEMA_V2 and record.kind == "item"


def current_items(items: Iterable[ItemRecord]) -> tuple[ItemRecord, ...]:
    """Only the newest version of each record: records never change in place, a change writes
    a new record naming the old one in ``supersedes`` (§3)."""

    entries = tuple(items)
    replaced = {old for entry in entries for old in entry.record.supersedes}
    return tuple(entry for entry in entries if entry.record.id not in replaced)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold().replace("’", "").replace("'", "")))


def words_beyond_title(record: Record) -> tuple[str, ...]:
    """Words in the body or any kept excerpt that are neither in the title nor filler (§3.1)."""

    texts = [record.body, *(evidence.excerpt for evidence in record.evidence)]
    words = (record.source or {}).get("words")
    if isinstance(words, str):
        texts.append(words)
    extra: set[str] = set()
    for text in texts:
        extra |= _words(text)
    return tuple(sorted(extra - _words(record.title) - FILLER_WORDS))


def is_thin(record: Record) -> bool:
    """An accepted, open task with nothing beyond its title (§3.1, rules 2-4)."""

    data = record.data
    return (
        _is_v2_item(record)
        and data.get("level") == "accepted"
        and record.state == "open"
        and plain_word(data) == "task"
        and not data.get("owed_to")
        and not data.get("timing")
        and not record.links
        and not words_beyond_title(record)
        and not data.get("first_step")
    )


def _is_answer(record: Record) -> bool:
    source = record.source or {}
    return (
        record.schema == RECORD_SCHEMA_V2
        and record.kind == "fact"
        and record.state == "current"
        and source.get("said_by") == "owner"
        and isinstance(source.get("words"), str)
    )


def answers_by_task(
    items: Iterable[ItemRecord], facts: Iterable[ItemRecord]
) -> dict[str, tuple[ItemRecord, ...]]:
    """Nic's answers, keyed by the newest version's record ID (§3.1): current facts he said,
    linked ``about`` the task or any version it replaced. Oldest first; tasks without any are
    left out."""

    by_target: dict[str, list[ItemRecord]] = {}
    for fact in current_items(facts):
        if not _is_answer(fact.record):
            continue
        for link in fact.record.links:
            if link.get("type") == "about" and isinstance(link.get("target"), str):
                by_target.setdefault(link["target"], []).append(fact)
    result: dict[str, tuple[ItemRecord, ...]] = {}
    for entry in current_items(items):
        found = {
            fact.record.id: fact
            for target in (entry.record.id, *entry.record.supersedes)
            for fact in by_target.get(target, ())
        }
        if found:
            result[entry.record.id] = tuple(
                sorted(found.values(), key=lambda fact: (fact.record.created_at, fact.record.id))
            )
    return result


def is_handoff_note(note: OpenNote) -> bool:
    """A note for work handed through Gremlin Town (``gt-<request_id>``): the other agent's steps."""

    return note.session_id.startswith(HANDOFF_NOTE_PREFIX)


def note_for_task(record: Record, notes: Iterable[OpenNote]) -> OpenNote | None:
    """One task's open note (N12b): a note linked to it by ``task_id`` - to its record ID or any
    version it replaced - whatever the note is called; otherwise a note under the older
    ``item-<record_id>`` key that names no other task. The most recently changed one wins.
    Handed-off work's notes never count: they are another agent's steps (rebuild plan G2)."""

    ids = (record.id, *record.supersedes)
    own = tuple(note for note in notes if not is_handoff_note(note))
    linked = [note for note in own if note.task_id is not None and note.task_id in ids]
    if not linked:
        keys = {ITEM_NOTE_PREFIX + record_id for record_id in ids}
        linked = [note for note in own if note.session_id in keys and (note.task_id is None or note.task_id in ids)]
    if not linked:
        return None
    return max(linked, key=lambda note: (note.updated_at, note.session_id))


def _may_need_first_step(record: Record) -> bool:
    """An item Nic accepted that is open now: a yes may have started it (step F §6). Both a
    missing first step (N12c) and "Stuck" (N10b) look only at these."""

    return _is_v2_item(record) and record.data.get("level") == "accepted" and record.state == "open"


def stuck_note(record: Record, notes: Iterable[OpenNote], today: date) -> OpenNote | None:
    """The note that makes a task stuck (N10b), or ``None``.

    A task is stuck when Nic accepted it, it is open, and its own open note (``note_for_task``:
    linked by ``task_id`` or the older ``item-<record_id>`` key) last changed ``STUCK_DAYS`` or
    more Singapore calendar days before ``today``, weekends included (rebuild plan G5). Only the
    note's change counts: a changed task record with an unchanged note is still stuck. A handed-off
    request's ``gt-`` note never counts, so work with another agent stays under "Waiting on" (G2).
    Parking, dropping or finishing a task takes it out; a waiting or blocked task is never stuck
    (Proposed - Nic to confirm: it isn't waiting on Nic, and a waiting one shows under "Waiting
    on")."""

    if not _may_need_first_step(record):
        return None
    note = note_for_task(record, notes)
    if note is None or (today - today_in_singapore(note.updated_at)).days < STUCK_DAYS:
        return None
    return note


@dataclass(frozen=True, slots=True)
class _ClarifyPick:
    entry: ItemRecord
    note: OpenNote | None = None  # set when the question is the task's first step (N12c)


def _clarify_picks(
    items: Iterable[ItemRecord],
    facts: Iterable[ItemRecord] | None,
    history: tuple[HistoryRow, ...],
    *,
    open_notes: Iterable[OpenNote] | None,
    cap: int,
) -> tuple[_ClarifyPick, ...]:
    entries = current_items(items)
    picks: dict[str, _ClarifyPick] = {}
    if open_notes is not None:
        notes = tuple(open_notes)
        for entry in entries:
            if not _may_need_first_step(entry.record):
                continue
            note = note_for_task(entry.record, notes)
            if note is None or note.next_step.strip():
                continue
            if _raised_before(
                history,
                item_ref=CLARIFY_REF_PREFIX + entry.record.id,
                rule=CLARIFY_FIRST_STEP_RULE,
                record_sha256=entry.sha256,
            ):
                continue
            picks[entry.record.id] = _ClarifyPick(entry, note)
    if facts is not None:
        answered = answers_by_task(entries, facts)
        for entry in entries:
            # One question per task a morning; a started task's first step comes first.
            if entry.record.id in picks or not is_thin(entry.record) or entry.record.id in answered:
                continue
            if _raised_before(
                history,
                item_ref=CLARIFY_REF_PREFIX + entry.record.id,
                rule=CLARIFY_RULE,
                record_sha256=entry.sha256,
            ):
                continue
            picks[entry.record.id] = _ClarifyPick(entry)
    ordered = sorted(picks.values(), key=lambda pick: (pick.entry.record.created_at, pick.entry.record.id))
    return tuple(ordered[:cap])


def pick_thin_tasks(
    items: Iterable[ItemRecord],
    facts: Iterable[ItemRecord],
    history: tuple[HistoryRow, ...],
    *,
    cap: int = CLARIFY_CAP,
    open_notes: Iterable[OpenNote] | None = None,
) -> tuple[ItemRecord, ...]:
    """The thin tasks to ask about this morning: at most ``cap``, oldest first, then record ID.

    Skips a task Nic already answered and one already asked about in this version (the
    "raised once" rule, keyed by its own ``clarify:<record_id>`` reference and SHA-256).

    With ``open_notes`` (N12c), a task he started whose note has no next step is picked too,
    within the same cap and order: an accepted, open item with an open note found through
    ``note_for_task``. Asked once per version of the task, like a thin one; a task that is both
    is asked only for its first step."""

    return tuple(
        pick.entry for pick in _clarify_picks(items, facts, history, open_notes=open_notes, cap=cap)
    )


def _accepted_open_items(items: tuple[ItemRecord, ...]) -> list[ItemRecord]:
    return [
        entry
        for entry in items
        if _is_v2_item(entry.record) and entry.record.data.get("level") == "accepted"
    ]


def _line(
    *,
    section: str,
    item: ItemRecord | None,
    text: str,
    item_ref: str,
    rule: str,
    fields_used: Mapping[str, Any],
    assumptions: tuple[str, ...] = (),
    unknowns: tuple[str, ...] = (),
) -> ReviewLine:
    return ReviewLine(
        number=0,
        section=section,
        text=text,
        item_ref=item_ref,
        rule=rule,
        record_sha256=item.sha256 if item is not None else None,
        record_path=item.path if item is not None else None,
        record_state=item.record.state if item is not None else None,
        fields_used=dict(fields_used),
        assumptions=assumptions,
        unknowns=unknowns,
    )


def _cap(
    lines: list[ReviewLine],
    section: str,
    *,
    more_ref: str,
    counted: Callable[[ReviewLine], bool] = lambda line: True,
) -> None:
    """At most `SECTION_CAP` of a section's counted lines, in their order, then one line saying how
    many more (Accepted - Nic, 28 Sep 2026: "10, then 'N more'"). Other lines stay where they are."""

    kept: list[ReviewLine] = []
    extra = 0
    last_kept = -1
    for line in lines:
        if counted(line):
            if sum(1 for kept_line in kept if counted(kept_line)) >= SECTION_CAP:
                extra += 1
                continue
            kept.append(line)
            last_kept = len(kept) - 1
        else:
            kept.append(line)
    if extra:
        kept.insert(last_kept + 1, _line(
            section=section, item=None, text=f"{extra} more; ask to see them", item_ref=more_ref,
            rule=f"{section}_cap", fields_used={"remaining": extra},
        ))
    lines[:] = kept


def _raised_before(history: tuple[HistoryRow, ...], *, item_ref: str, rule: str, record_sha256: str | None = None) -> bool:
    for row in history:
        if row.item_ref != item_ref or row.rule != rule:
            continue
        if record_sha256 is None or row.record_sha256 == record_sha256:
            return True
    return False


def _fill_today(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    calendar_events: tuple[CalendarEvent, ...],
    now: datetime,
) -> None:
    """Today's calendar, then what's due today or tonight. A start time already passed is under
    Missed instead."""

    today = today_in_singapore(now)
    for event in calendar_events:
        lines.append(
            _line(
                section=SECTION_TODAY,
                item=None,
                text=f"{event.title} — on your calendar",
                item_ref=f"calendar:{event.title}:{event.start.isoformat()}",
                rule="on_calendar_today",
                fields_used={"title": event.title, "start": event.start.isoformat()},
            )
        )
    for entry in accepted:
        if entry.record.state not in OPEN_STATES or missed(entry.record, now):
            continue
        timing = entry.record.data.get("timing") or {}
        due = timing.get("due")
        if due and date.fromisoformat(due) == today:
            lines.append(
                _line(
                    section=SECTION_TODAY,
                    item=entry,
                    text=f"{entry.record.title} — due today",
                    item_ref=entry.record.id,
                    rule="due_today",
                    fields_used={"due": due},
                )
            )
        elif due_tonight(entry.record, today):
            lines.append(
                _line(
                    section=SECTION_TODAY,
                    item=entry,
                    text=f"{entry.record.title} — due tonight at {_clock_word(timing['at'])}",
                    item_ref=entry.record.id,
                    rule="due_tonight",
                    fields_used={"due": due, "at": timing["at"]},
                )
            )


def days_word(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"


def missed_since(item: Record, today: date) -> tuple[date, int]:
    """The day a missed item was due, or its window closed, and how many days ago that was."""

    timing = item.data.get("timing") or {}
    day = date.fromisoformat(timing.get("due") or timing["window"]["end"])
    return day, (today - day).days


def missed_words(item: Record, today: date) -> str:
    """How long a missed item has been missed (Ciel and Brain Home PRD §4.6): "missed since Fri 25
    Sep (10 days)". Something whose start time passed says when it was, and how long ago once
    that was an earlier day: "was at 3 pm Sun 27 Sep (2 days ago)"."""

    if start_time(item) is not None:
        _day, days = missed_since(item, today)
        return f"was {start_words(item, today)}" + (f" ({days_word(days)} ago)" if days > 0 else "")
    day, days = missed_since(item, today)
    return f"missed since {_date_word(day.isoformat(), today)} ({days_word(days)})"


def _fill_missed(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    now: datetime,
) -> None:
    """Every missed item, in every review, until Nic's answer changes its record (N9, 28 Sep).

    Until 28 Sep a missed item was raised once and then dropped out of the review for good. Now
    only a changed record takes it out: a new date, parked, dropped or done. Each line says since
    when, and for how long, it has been missed (Ciel and Brain Home PRD §4.6; Nic, 28 Sep: "forget
    n9", the fixed no-count wording). Something with a start time is over, so its line asks what
    happened instead (Accepted - Nic, 28 Sep 2026)."""

    today = today_in_singapore(now)
    for entry in accepted:
        record = entry.record
        # Waiting on someone past the date it was expected: "To chase" asks about it instead
        # (PRD §4.9).
        if not missed(record, now) or record.state == "waiting":
            continue
        timing = record.data.get("timing") or {}
        due = timing.get("due")
        day, days = missed_since(record, today)
        if start_time(record) is not None:
            lines.append(
                _line(
                    section=SECTION_MISSED,
                    item=entry,
                    text=f"{record.title} — {missed_words(record, today)}. {MISSED_START_QUESTION}",
                    item_ref=record.id,
                    rule="missed",
                    fields_used={"due": due, "at": timing["at"], "missed_days": days},
                )
            )
            continue
        if due:
            fields_used = {"due": due}
        else:
            fields_used = {"window": timing["window"]}
        fields_used.update(missed_since=day.isoformat(), missed_days=days)
        text = f"{record.title} — {missed_words(record, today)}. {MISSED_QUESTION}"
        lines.append(
            _line(
                section=SECTION_MISSED,
                item=entry,
                text=text,
                item_ref=record.id,
                rule="missed",
                fields_used=fields_used,
            )
        )


def _fill_stuck(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    open_notes: tuple[OpenNote, ...] | None,
    now: datetime,
) -> None:
    """Every stuck task (``stuck_note``), in every review until Nic moves it (its note changes),
    parks it or drops it (N10b), longest unchanged first. Nothing when the notes weren't read.

    An item already under Missed is left out: it shows once, in the more urgent section, whose
    four answers cover moving, parking and dropping (Proposed - Nic to confirm). The review's other
    sections are unchanged, so a stuck task with no time also stays under "Pick or park"."""

    if open_notes is None:
        return
    today = today_in_singapore(now)
    found: list[tuple[ItemRecord, OpenNote]] = []
    for entry in accepted:
        if missed(entry.record, now):
            continue
        note = stuck_note(entry.record, open_notes, today)
        if note is not None:
            found.append((entry, note))
    found.sort(key=lambda pair: (pair[1].updated_at, pair[0].record.id))
    for entry, note in found:
        changed = today_in_singapore(note.updated_at).isoformat()
        lines.append(
            _line(
                section=SECTION_STUCK,
                item=entry,
                text=STUCK_TEXT.format(title=entry.record.title, date=_date_word(changed, today)),
                item_ref=entry.record.id,
                rule=STUCK_RULE,
                fields_used={
                    "state": entry.record.state,
                    "session_id": note.session_id,
                    "task_id": note.task_id,
                    "note_updated_at": note.updated_at.isoformat(),
                },
            )
        )


def _fill_windows_closing(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    today: date,
) -> None:
    deadline = today + timedelta(days=WINDOW_CLOSING_DAYS)
    for entry in accepted:
        record = entry.record
        if record.state not in OPEN_STATES:
            continue
        window = record.data.get("timing", {}).get("window")
        if not window:
            continue
        end = date.fromisoformat(window["end"])
        if today <= end <= deadline:
            lines.append(
                _line(
                    section=SECTION_WINDOWS_CLOSING,
                    item=entry,
                    text=f"{record.title} — window closes {_date_word(window['end'], today)}",
                    item_ref=record.id,
                    rule="window_closing",
                    fields_used={"window": window},
                )
            )


def _ranked(accepted: list[ItemRecord], inputs: ReviewInputs, today: date) -> tuple[Any, ...]:
    """Step F's ranking of the open, waiting and blocked tasks, for "Coming up" and "Your day"."""

    # Imported here: orientation imports this module.
    from brain_core.orientation import focus_project_ids, rank

    live = [entry for entry in accepted if entry.record.state in OPEN_STATES]
    return rank(
        live,
        today,
        focus_projects=focus_project_ids(inputs.preferences),
        notes=inputs.open_notes or (),
        now=inputs.now,
        ranking=inputs.ranking,
    )


def _fill_coming_up(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    inputs: ReviewInputs,
    today: date,
    shown: frozenset[str],
    ranked: tuple[Any, ...],
) -> None:
    """Open tasks coming up in the next `COMING_UP_DAYS` days (N16), most important first by step
    F's ranking, at most `COMING_UP_CAP` and then how many more; then how many come up later. A
    task already under Today, Missed, Stuck or Windows closing (``shown``) is left out. A weekly
    routine has no date, so it stays under "Pick or park" as before and is not listed twice."""

    from brain_core.work import first_id

    live = [entry for entry in accepted if entry.record.state in OPEN_STATES]
    order = {candidate.ref: index for index, candidate in enumerate(ranked)}
    rules = {candidate.ref: candidate.rules for candidate in ranked}
    soon: list[tuple[ItemRecord, date]] = []
    later = 0
    for entry in live:
        record = entry.record
        if record.state != "open" or record.id in shown:
            continue
        day = coming_up_date(record, today)
        if day is None:
            continue
        if day <= today + timedelta(days=COMING_UP_DAYS):
            soon.append((entry, day))
        else:
            later += 1
    # Ranking uses lasting identities and excludes non-up records. Legacy review
    # still lists their dates, after ranked records in a stable order.
    soon.sort(key=lambda pair: (
        order.get(first_id(pair[0].record), len(order)),
        pair[0].record.created_at, first_id(pair[0].record),
    ))
    for entry, day in soon[:COMING_UP_CAP]:
        timing = entry.record.data.get("timing") or {}
        if timing.get("due"):
            clause, fields_used = f"due {_date_word(day.isoformat(), today)}", {"due": timing["due"]}
        else:
            clause, fields_used = f"window closes {_date_word(day.isoformat(), today)}", {"window": timing["window"]}
        lines.append(
            _line(
                section=SECTION_COMING_UP,
                item=entry,
                text=f"{entry.record.title} — {clause}",
                item_ref=entry.record.id,
                rule=COMING_UP_RULE,
                fields_used={**fields_used, "rules": list(rules.get(first_id(entry.record), ()))},
            )
        )
    overflow = len(soon) - COMING_UP_CAP
    if overflow > 0:
        lines.append(
            _line(
                section=SECTION_COMING_UP,
                item=None,
                text=f"{overflow} more; ask to see them",
                item_ref=COMING_UP_MORE_REF,
                rule="coming_up_cap",
                fields_used={"remaining": overflow},
            )
        )
    if later:
        lines.append(
            _line(
                section=SECTION_COMING_UP,
                item=None,
                text=f"{later} more later; ask to see them",
                item_ref=COMING_UP_LATER_REF,
                rule="coming_up_later",
                fields_used={"later": later, "after": (today + timedelta(days=COMING_UP_DAYS)).isoformat()},
            )
        )


def _fill_pick_or_park(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    today: date,
    order: Mapping[str, int],
) -> None:
    """"Your day: pick 3 to 7" (PRD §4.2; it was "Pick or park"): open tasks with no time, most
    important first by step F's ranking, then parked ones whose day has come, oldest first. A
    pick Nic doesn't finish goes back where it was the next day; it is never missed (only dates
    are)."""

    from brain_core.work import first_id

    candidates: list[tuple[ItemRecord, str]] = []
    for entry in accepted:
        record = entry.record
        if record.state == "open" and not has_real_time(record):
            candidates.append((entry, "no time set"))
            continue
        if record.state == "parked":
            parked_until = record.data.get("parked_until")
            if isinstance(parked_until, str):
                parked_date = date.fromisoformat(parked_until)
                if parked_date <= today:
                    word = "today" if parked_date == today else _date_word(parked_until, today)
                    candidates.append((entry, f"parked until {word}"))
    candidates.sort(key=lambda pair: (
        pair[0].record.state == "parked", order.get(first_id(pair[0].record), len(order)),
        pair[0].record.created_at, first_id(pair[0].record),
    ))
    overflow = len(candidates) - PICK_OR_PARK_CAP
    for entry, reason in candidates[:PICK_OR_PARK_CAP]:
        lines.append(
            _line(
                section=SECTION_PICK_OR_PARK,
                item=entry,
                text=f"{entry.record.title} — {reason}",
                item_ref=entry.record.id,
                rule="pick_or_park",
                fields_used={"state": entry.record.state, "reason": reason},
            )
        )
    if overflow > 0:
        lines.append(
            _line(
                section=SECTION_PICK_OR_PARK,
                item=None,
                text=f"{overflow} more; ask to see them",
                item_ref="__more__",
                rule="pick_or_park_cap",
                fields_used={"remaining": overflow},
            )
        )


def _v1_is_live(record: Record) -> bool:
    terminal = _V1_TERMINAL_STATES.get(record.kind, frozenset())
    return record.state not in terminal


_BLOCK_WORDS = {"deep_focus": "deep focus", "admin": "admin", "errands": "errands"}


def _presort_words(record: Record) -> str:
    """What an item was sorted with so far, in few words: "admin, about 15 min, first: find the
    number"; empty when it has none of it."""

    data = record.data
    timing = data.get("timing") or {}
    parts: list[str] = []
    if data.get("block"):
        parts.append(_BLOCK_WORDS[data["block"]])
    minutes = timing.get("duration_minutes")
    if minutes is not None:
        parts.append("under two minutes" if fits_two_minutes(record) else f"about {minutes} min")
    if data.get("first_step"):
        parts.append(f"first: {data['first_step']}")
    if data.get("raise_with"):
        parts.append(f"raise with {data['raise_with']}")
    return ", ".join(parts)


def _sort_fields(record: Record) -> dict[str, Any]:
    data = record.data
    return {
        "word": plain_word(data),
        "level": data.get("level"),
        "state": record.state,
        "speaker": (record.source or {}).get("speaker"),
        "block": data.get("block"),
        "first_step": data.get("first_step"),
        "timing": dict(data.get("timing") or {}),
        "raise_with": data.get("raise_with"),
        "owed_to": data.get("owed_to"),
        "repeat": data.get("repeat"),
        "links": [dict(link) for link in record.links],
        "two_minutes": fits_two_minutes(record),
        "created_at": record.created_at.isoformat(),
    }


def _fill_to_sort(
    lines: list[ReviewLine],
    items: tuple[ItemRecord, ...],
    history: tuple[HistoryRow, ...],
) -> None:
    """"To sort" (PRD §4.1; build plan slice 4): agents' suggestions waiting for Nic's yes, with
    their pre-sort, and tasks he accepted that have no block type yet, oldest first, at most
    `SORTING_CAP` a morning (Accepted - Nic, 28 Sep 2026: "10 a morning"), then how many more.
    Each comes back every morning until it is sorted, so the backlog pages through as he answers.
    Old v1 records still unconfirmed are listed once, as before, outside the cap."""

    by_id = {entry.record.id: entry for entry in items}
    queue = to_sort(entry.record for entry in items)
    for record in queue[:SORTING_CAP]:
        if awaits_yes(record):
            clause = f"suggested by {(record.source or {}).get('speaker') or 'an agent'}"
        else:
            clause = "not sorted yet"
        presort = _presort_words(record)
        lines.append(
            _line(
                section=SECTION_TO_SORT,
                item=by_id[record.id],
                text=f"{record.title} — {clause}" + (f"; {presort}" if presort else ""),
                item_ref=record.id,
                rule=TO_SORT_RULE,
                fields_used=_sort_fields(record),
            )
        )
    overflow = len(queue) - SORTING_CAP
    if overflow > 0:
        lines.append(
            _line(
                section=SECTION_TO_SORT,
                item=None,
                text=f"{overflow} more to sort; ask to see them",
                item_ref=TO_SORT_MORE_REF,
                rule="to_sort_cap",
                fields_used={"remaining": overflow},
            )
        )
    unconfirmed = sorted(
        (
            entry for entry in items
            if entry.record.schema == RECORD_SCHEMA_V1 and entry.record.kind in _V1_TERMINAL_STATES
            and _v1_is_live(entry.record)
        ),
        key=lambda entry: entry.record.created_at,
    )
    for entry in unconfirmed:
        if _raised_before(history, item_ref=entry.record.id, rule="new_suggestion"):
            continue
        lines.append(
            _line(
                section=SECTION_TO_SORT,
                item=entry,
                text=f"{entry.record.title} — unconfirmed",
                item_ref=entry.record.id,
                rule="new_suggestion",
                fields_used={"schema": entry.record.schema, "reason": "unconfirmed"},
            )
        )


def request_title(request_id: str, objective: str = "") -> str:
    """How a handed-off request is named to Nic: its objective on one line, cut at a word to about
    ``REQUEST_TITLE_CHARS`` characters, or its ID when it has none (§3)."""

    words = " ".join(objective.split())
    if not words:
        return request_id
    if len(words) <= REQUEST_TITLE_CHARS:
        return words
    head = words[: REQUEST_TITLE_CHARS + 1]
    head = head.rsplit(" ", 1)[0] if " " in head else words[:REQUEST_TITLE_CHARS]
    return head.rstrip(" ,;:-—") + "…"


def _waiting_on_target(record: Record) -> str | None:
    for link in record.links or ():
        if link.get("type") == "waiting_on":
            return link.get("target")
    return None


def _chase_reason(record: Record, today: date) -> tuple[str, dict[str, Any]] | None:
    """Why a waiting item is to chase (PRD §4.9; gsd's IsAgingWaiting): past the date it was
    expected, or waited on `CHASE_AFTER_DAYS` days or more since it last changed. None otherwise."""

    if record.state != "waiting":
        return None
    who = _waiting_on_target(record) or "someone"
    due = (record.data.get("timing") or {}).get("due")
    if due and date.fromisoformat(due) < today:
        days = (today - date.fromisoformat(due)).days
        return f"waiting on {who}; expected {_date_word(due, today)} ({days_word(days)} ago)", {"expected": due, "who": who}
    since = today_in_singapore(record.created_at)
    days = (today - since).days
    if days < CHASE_AFTER_DAYS:
        return None
    return f"waiting on {who} since {_date_word(since.isoformat(), today)} ({days_word(days)})", {
        "waiting_since": since.isoformat(), "who": who,
    }


def _fill_to_chase(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    open_requests: tuple[OpenRequest, ...],
    today: date,
) -> frozenset[str]:
    """"To chase" (PRD §4.2, §4.9): Nic's items waiting on someone for `CHASE_AFTER_DAYS` days or
    more, or past the date expected, longest first; then requests handed to agents that long ago,
    oldest first (Proposed - Nic to confirm). A request keeps the "Waiting on" rule, so a daytime
    return still reports it. At most `SECTION_CAP` of each, then how many more. Returns what it
    listed, which "Waiting on" then leaves out."""

    chased: list[tuple[date, ItemRecord, str, dict[str, Any]]] = []
    for entry in accepted:
        found = _chase_reason(entry.record, today)
        if found is not None:
            reason, fields = found
            since = date.fromisoformat(fields.get("expected") or fields["waiting_since"])
            chased.append((since, entry, reason, fields))
    chased.sort(key=lambda row: (row[0], row[1].record.id))
    for _since, entry, reason, fields in chased:
        lines.append(_line(
            section=SECTION_TO_CHASE, item=entry, text=f"{entry.record.title} — {reason}", item_ref=entry.record.id,
            rule="to_chase", fields_used={**fields, "links": [dict(link) for link in entry.record.links]},
        ))
    _cap(lines, SECTION_TO_CHASE, more_ref="__more_to_chase__", counted=lambda line: line.rule == "to_chase")
    aged = sorted(
        (request for request in open_requests if request.state not in (REQUEST_RETURNED_PROOF, REQUEST_PROOF_UNVERIFIED)
         and (today - request.since).days >= CHASE_AFTER_DAYS),
        key=lambda request: (request.since, request.id),
    )
    for request in aged[:SECTION_CAP]:
        days = (today - request.since).days
        title = request_title(request.id, request.objective)
        line = _line(
            section=SECTION_TO_CHASE, item=None,
            text=f"{title} — with {request.handed_to} since {_date_word(request.since.isoformat(), today)} ({days_word(days)})",
            item_ref=request.id, rule="waiting_on_request",
            fields_used={
                "handed_to": request.handed_to, "since": request.since.isoformat(), "objective": request.objective,
                "work_state": request.work_state, "check": request.check,
            },
        )
        lines.append(replace(line, record_state=request.state))
    if len(aged) > SECTION_CAP:
        lines.append(_line(
            section=SECTION_TO_CHASE, item=None, text=f"{len(aged) - SECTION_CAP} more; ask to see them",
            item_ref="__more_chase_requests__", rule="to_chase_request_cap",
            fields_used={"remaining": len(aged) - SECTION_CAP},
        ))
    return frozenset(line.item_ref for line in lines)


def _names_project(entry: str, project: str) -> bool:
    """The naming bridge step F uses (`orientation.same_project`): equal ignoring case and spaces."""

    return entry.strip().casefold() == project.strip().casefold()


def _fill_no_next_action(
    lines: list[ReviewLine],
    items: tuple[ItemRecord, ...],
    preferences: tuple[Record, ...],
    project_tasks: tuple[ProjectTasks, ...] | None,
) -> None:
    """"Projects with no next action" (PRD §4.10): each goal Nic accepted that is still open, waiting
    or blocked with no open task of his linked `part_of` it; and, while the task sitreps can be read,
    each focus project with no open task, saved or in its sitrep. Every morning until it has one,
    or he parks it (a goal) or drops it from his focus projects. A goal first, oldest first; then
    the focus projects in the order he named them."""

    open_tasks = [
        entry.record for entry in items
        if _is_v2_item(entry.record) and entry.record.data.get("level") == "accepted"
        and entry.record.state == "open" and entry.record.data.get("size", "task") == "task"
    ]
    goals = sorted(
        (
            entry for entry in items
            if _is_v2_item(entry.record) and entry.record.data.get("level") == "accepted"
            and entry.record.data.get("size") == "goal" and entry.record.state in OPEN_STATES
        ),
        key=lambda entry: (entry.record.created_at, entry.record.id),
    )
    for entry in goals:
        ids = {entry.record.id, *entry.record.supersedes}
        if any(
            link.get("type") == "part_of" and link.get("target") in ids for task in open_tasks for link in task.links
        ):
            continue
        lines.append(_line(
            section=SECTION_NO_NEXT_ACTION, item=entry, text=f"{entry.record.title} — goal; no next action",
            item_ref=entry.record.id, rule="no_next_action", fields_used={"goal": entry.record.title},
        ))
    preference = focus_preference(preferences)
    if project_tasks is not None and preference is not None:
        seen: set[str] = set()
        for project in preference.data["focus_projects"]:
            key = project.strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            saved = any(
                link.get("type") in ("part_of", "about") and _names_project(project, str(link.get("target", "")))
                for task in open_tasks for link in task.links
            )
            sitrep = any(
                task.state == "open" for summary in project_tasks if _names_project(project, summary.project)
                for task in summary.tasks
            )
            if saved or sitrep:
                continue
            lines.append(_line(
                section=SECTION_NO_NEXT_ACTION, item=None, text=f"{project} — focus project; no next action",
                item_ref=f"project:{project}", rule="no_next_action", fields_used={"focus_project": project},
            ))
    _cap(lines, SECTION_NO_NEXT_ACTION, more_ref="__more_no_next_action__")


def _fill_weekly_review(lines: list[ReviewLine], today: date) -> None:
    """PRD §4.2, §4.8: on the weekly review's day, one line saying it's ready this afternoon."""

    if today.weekday() == WEEKLY_REVIEW_WEEKDAY:
        lines.append(_line(
            section=SECTION_WEEKLY_REVIEW, item=None, text=WEEKLY_REVIEW_TEXT, item_ref="__weekly_review__",
            rule="weekly_review_ready", fields_used={"day": today.isoformat()},
        ))


def _fill_waiting_on(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    open_requests: tuple[OpenRequest, ...],
    chased: frozenset[str] = frozenset(),
    today: date | None = None,
) -> None:
    """Waiting and handed-off work not already under "To chase"."""

    for entry in accepted:
        record = entry.record
        if record.state != "waiting" or record.id in chased:
            continue
        target = _waiting_on_target(record)
        reason = f"waiting on {target}" if target else "waiting"
        lines.append(
            _line(
                section=SECTION_WAITING_ON,
                item=entry,
                text=f"{record.title} — {reason}",
                item_ref=record.id,
                rule="waiting_on_item",
                fields_used={"links": record.links},
            )
        )
    _cap(lines, SECTION_WAITING_ON, more_ref="__more_waiting__", counted=lambda line: line.rule == "waiting_on_item")
    # Newest first by date; within a day, in the order given (GT lists most recently updated first).
    requests = sorted(
        (request for request in open_requests if request.id not in chased),
        key=lambda request: request.since, reverse=True,
    )
    for request in requests[:WAITING_ON_REQUEST_CAP]:
        title = request_title(request.id, request.objective)
        with_whom = f"with {request.handed_to} since {_date_word(request.since.isoformat(), today)}"
        if request.state == REQUEST_RETURNED_PROOF:
            text = f"{title} — back, with proof: {quoted_proof(request.proof or '')} ({with_whom})"
        elif request.state == REQUEST_PROOF_UNVERIFIED:
            text = f"{title} — back, proof unverified: {quoted_proof(request.proof or '')} ({with_whom})"
        elif request.state == REQUEST_RETURNED_UNCHECKED:
            text = f"{title} — returned, not yet checked ({with_whom})"
        else:
            text = f"{title} — {with_whom}"
        line = _line(
            section=SECTION_WAITING_ON,
            item=None,
            text=text,
            item_ref=request.id,
            rule="request_returned_proof" if request.state == REQUEST_RETURNED_PROOF else "waiting_on_request",
            fields_used={
                "handed_to": request.handed_to,
                "since": request.since.isoformat(),
                "objective": request.objective,
                "work_state": request.work_state,
                "check": request.check,
                **({"proof": request.proof, "proof_verified": request.proof_verified} if request.proof else {}),
            },
        )
        # The state Nic was shown, so a daytime return says only what changed since (§12).
        lines.append(replace(line, record_state=request.state))
    overflow = len(requests) - WAITING_ON_REQUEST_CAP
    if overflow > 0:
        lines.append(
            _line(
                section=SECTION_WAITING_ON,
                item=None,
                text=f"{overflow} more; ask to see them",
                item_ref="__more_requests__",
                rule="waiting_on_request_cap",
                fields_used={"remaining": overflow},
            )
        )


def _fill_calendar_entries(
    lines: list[ReviewLine],
    accepted: list[ItemRecord],
    calendar_marked_ids: frozenset[str],
    now: datetime,
) -> None:
    for entry in accepted:
        record = entry.record
        if record.state not in OPEN_STATES:
            continue
        if not has_real_time(record):
            continue
        if record.id in calendar_marked_ids:
            continue
        if missed(record, now):
            continue
        lines.append(
            _line(
                section=SECTION_CALENDAR_ENTRIES,
                item=entry,
                text=f"{record.title} — has a date, not on your calendar",
                item_ref=record.id,
                rule="calendar_entry_missing",
                fields_used={"timing": record.data.get("timing")},
            )
        )


def _fill_could_not_see(lines: list[ReviewLine], source_status: Mapping[str, str | None]) -> None:
    for source, status in source_status.items():
        if status in (None, "", "ok"):
            continue
        lines.append(
            _line(
                section=SECTION_COULD_NOT_SEE,
                item=None,
                text=f"{source}: {status}",
                item_ref=source,
                rule="source_unavailable",
                fields_used={"source": source, "status": status},
            )
        )


def _said(answers: tuple[ItemRecord, ...]) -> str:
    quoted = "; ".join(f'"{" ".join(fact.record.source["words"].split())}"' for fact in answers)
    return f" (you said: {quoted})"


_FIXED_WORDING_SECTIONS = frozenset({SECTION_MISSED, SECTION_STUCK})


def _with_answers(lines: list[ReviewLine], answers: Mapping[str, tuple[ItemRecord, ...]]) -> None:
    """Nic's answers go on the first line naming the task, never on Missed's or Stuck's fixed
    wording (§3.1, N10b): so a stuck task's answer stays on the line it was on before Stuck."""

    shown: set[str] = set()
    for index, line in enumerate(lines):
        found = answers.get(line.item_ref)
        if not found or line.section in _FIXED_WORDING_SECTIONS or line.item_ref in shown:
            continue
        shown.add(line.item_ref)
        lines[index] = replace(
            line,
            text=line.text + _said(found),
            fields_used={**line.fields_used, "answers": [fact.record.id for fact in found]},
        )


def _clarify_task(pick: _ClarifyPick, number: int | None) -> ClarifyTask:
    entry = pick.entry
    if pick.note is not None:
        return ClarifyTask(
            record_id=entry.record.id,
            title=entry.record.title,
            number=number,
            record_sha256=entry.sha256,
            record_path=entry.path,
            fields_used={
                "word": plain_word(entry.record.data),
                "level": entry.record.data.get("level"),
                "state": entry.record.state,
                "session_id": pick.note.session_id,
                "task_id": pick.note.task_id,
                "next_step": pick.note.next_step,
                "note_updated_at": pick.note.updated_at.isoformat(),
                "created_at": entry.record.created_at.isoformat(),
            },
            rule=CLARIFY_FIRST_STEP_RULE,
            question=FIRST_STEP_QUESTION.format(title=entry.record.title),
            session_id=pick.note.session_id,
        )
    return ClarifyTask(
        record_id=entry.record.id,
        title=entry.record.title,
        number=number,
        record_sha256=entry.sha256,
        record_path=entry.path,
        fields_used={
            "word": plain_word(entry.record.data),
            "level": entry.record.data.get("level"),
            "state": entry.record.state,
            "timing": dict(entry.record.data.get("timing") or {}),
            "links": [dict(link) for link in entry.record.links],
            "words_beyond_title": list(words_beyond_title(entry.record)),
            "answers": [],
            "created_at": entry.record.created_at.isoformat(),
        },
    )


def _clarify(
    picks: tuple[_ClarifyPick, ...], lines: list[ReviewLine]
) -> tuple[ClarifyTask, ...]:
    numbers: dict[str, int] = {}
    for line in lines:
        if line.number and line.item_ref not in numbers:
            numbers[line.item_ref] = line.number
    return tuple(_clarify_task(pick, numbers.get(pick.entry.record.id)) for pick in picks)


def build_review(inputs: ReviewInputs) -> Review:
    today = today_in_singapore(inputs.now)
    items = current_items(inputs.items)
    accepted = _accepted_open_items(items)

    sections: dict[str, list[ReviewLine]] = {name: [] for name in SECTION_ORDER}
    requests = []
    for request in inputs.open_requests:
        if request.state == REQUEST_RETURNED_PROOF:
            if any(row.item_ref == request.id and row.rule == "request_returned_proof"
                   and row.channel == "review" for row in inputs.history):
                continue
        elif request.last_activity is not None and inputs.now - request.last_activity >= timedelta(days=REQUEST_STALE_DAYS):
            if today.weekday() == WEEKLY_REVIEW_WEEKDAY and not any(
                row.item_ref == request.id and row.rule == "stale_request" for row in inputs.history
            ):
                sections[SECTION_WEEKLY_REVIEW].append(_line(
                    section=SECTION_WEEKLY_REVIEW, item=None,
                    text=f"{request_title(request.id, request.objective)} — stale (with {request.handed_to})",
                    item_ref=request.id, rule="stale_request",
                    fields_used={"last_activity": request.last_activity.isoformat()},
                ))
            continue
        requests.append(request)
    open_requests = tuple(requests)
    ranked = _ranked(accepted, inputs, today)
    order = {candidate.ref: index for index, candidate in enumerate(ranked)}
    _fill_today(sections[SECTION_TODAY], accepted, inputs.calendar_events, inputs.now)
    _cap(sections[SECTION_TODAY], SECTION_TODAY, more_ref="__more_today__",
         counted=lambda line: not line.item_ref.startswith("calendar:"))
    _fill_missed(sections[SECTION_MISSED], accepted, inputs.now)
    _cap(sections[SECTION_MISSED], SECTION_MISSED, more_ref="__more_missed__")
    _fill_stuck(sections[SECTION_STUCK], accepted, inputs.open_notes, inputs.now)
    _cap(sections[SECTION_STUCK], SECTION_STUCK, more_ref="__more_stuck__")
    _fill_windows_closing(sections[SECTION_WINDOWS_CLOSING], accepted, today)
    _cap(sections[SECTION_WINDOWS_CLOSING], SECTION_WINDOWS_CLOSING, more_ref="__more_windows__")
    shown = frozenset(
        line.item_ref
        for name in (SECTION_TODAY, SECTION_MISSED, SECTION_STUCK, SECTION_WINDOWS_CLOSING)
        for line in sections[name]
    )
    _fill_coming_up(sections[SECTION_COMING_UP], accepted, inputs, today, shown, ranked)
    _fill_to_sort(sections[SECTION_TO_SORT], items, inputs.history)
    chased = _fill_to_chase(sections[SECTION_TO_CHASE], accepted, open_requests, today)
    _fill_no_next_action(sections[SECTION_NO_NEXT_ACTION], items, inputs.preferences, inputs.project_tasks)
    _fill_pick_or_park(sections[SECTION_PICK_OR_PARK], accepted, today, order)
    _fill_waiting_on(sections[SECTION_WAITING_ON], accepted, open_requests, chased, today)
    _fill_calendar_entries(sections[SECTION_CALENDAR_ENTRIES], accepted, inputs.calendar_marked_ids, inputs.now)
    _cap(sections[SECTION_CALENDAR_ENTRIES], SECTION_CALENDAR_ENTRIES, more_ref="__more_calendar__")
    _fill_weekly_review(sections[SECTION_WEEKLY_REVIEW], today)
    _fill_could_not_see(sections[SECTION_COULD_NOT_SEE], inputs.source_status)

    lines: list[ReviewLine] = []
    present_sections: list[str] = []
    number = 1
    for name in SECTION_ORDER:
        entries = sections[name]
        if not entries:
            continue
        present_sections.append(name)
        for entry in entries:
            if name in _UNNUMBERED_SECTIONS:
                lines.append(entry)
                continue
            lines.append(replace(entry, number=number))
            number += 1

    clarify: tuple[ClarifyTask, ...] = ()
    if inputs.facts is not None:
        _with_answers(lines, answers_by_task(items, inputs.facts))
    if inputs.facts is not None or inputs.open_notes is not None:
        picks = _clarify_picks(
            items, inputs.facts, inputs.history, open_notes=inputs.open_notes, cap=CLARIFY_CAP
        )
        clarify = _clarify(picks, lines)
    sorting = tuple(
        SortEntry(
            record_id=line.item_ref,
            title=next(entry.record.title for entry in items if entry.record.id == line.item_ref),
            number=line.number,
            record_sha256=line.record_sha256,
            record_path=line.record_path,
            fields_used=line.fields_used,
        )
        for line in lines
        if line.section == SECTION_TO_SORT and line.rule == TO_SORT_RULE
    )
    return Review(lines=tuple(lines), sections=tuple(present_sections), clarify=clarify, to_sort=sorting)


def render_review(review: Review) -> str:
    if review.is_empty:
        return EMPTY_REVIEW_TEXT
    parts: list[str] = []
    current_section: str | None = None
    for line in review.lines:
        if line.section != current_section:
            current_section = line.section
            parts.append(f"## {SECTION_TITLES[line.section]}")
        if line.section in _UNNUMBERED_SECTIONS:
            parts.append(line.text)
        else:
            parts.append(f"{line.number}. {line.text}")
    return "\n".join(parts)
