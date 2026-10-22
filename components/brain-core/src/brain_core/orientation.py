"""Step F's two asks: pure functions over the sole work-model reader.

Both asks rank only up things with the existing points engine and nine-rule
fallback. Confirmed life changes pin first; confirmed hurts-weeks adds 8 points.
The overview folds parts into their task; the pick carries parent/progress and
only Nic's supplied next action. Project sitreps keep the existing name join.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from brain_core.blocks import BLOCK_WORDS, Block, Moment, Override, blocks_from_calendar, moment, next_block, task_kind
from brain_core.presentation import relative_day_words, stake_words, work_words
from brain_core.language import actionable, focus_preference, plain_word
from brain_core.moments import Signal as MomentSignal
from brain_core.records import RECORD_SCHEMA_V2, Record, first_saved
from brain_core import work
from brain_core.work import Progress
from brain_core.routines import done_today, is_routine, routine_stats, stats_words, versions_of
from brain_core.review import (
    COMING_UP_CAP,
    COMING_UP_DAYS,
    COMING_UP_LATER_REF,
    COMING_UP_MORE_REF,
    COMING_UP_RULE,
    MISSED_START_QUESTION,
    OPEN_STATES,
    PICK_OR_PARK_CAP,
    RANKING_POINTS,
    RANKING_RULES,
    RANKINGS,
    REQUEST_OPEN,
    REQUEST_RETURNED_UNCHECKED,
    SECTION_COMING_UP,
    SECTION_COULD_NOT_SEE,
    SECTION_MISSED,
    SECTION_NEW_SUGGESTIONS,
    SECTION_TODAY,
    SECTION_WAITING_ON,
    SECTION_WINDOWS_CLOSING,
    WAITING_ON_REQUEST_CAP,
    HistoryRow,
    ItemRecord,
    OpenNote,
    OpenRequest,
    ReviewInputs,
    coming_up_date,
    current_items,
    due_tonight,
    is_handoff_note,
    missed,
    missed_words,
    note_for_task,
    request_title,
    start_time,
    start_words,
    stuck_note,
)
from brain_core.sitreps import ProjectTasks, TaskSitrep
from brain_core.timewords import SINGAPORE, today_in_singapore

CAUTION_TEXT = 'Noted. "{title}" is still due today.'
CAUTION_TONIGHT_TEXT = 'Noted. "{title}" is still due tonight.'
STALL_TIME_BOX_MINUTES = 20
STALL_THRESHOLD_DAYS = 2
WAITING_WINDOW_DAYS = 3
WINDOW_CLOSING_DAYS = 3
MAX_ALTERNATIVES = 2
MAX_CAN_WAIT_ITEMS = 3
# N10d (Accepted - Nic, 28 Sep 2026): "what next?" and a yes to something new name what is in
# progress once there are this many things.
IN_PROGRESS_NAMED_AT = 3
# N10c: "Finished this week" is the last 7 days, today included.
FINISHED_WINDOW_DAYS = 7
# "Where am I?" logs what it showed on its own why-log channel; those rows never count towards
# "tends to stall" (redesign default 11). Its rules all start with this prefix, so no reader of
# step E's rules ("new_suggestion", "waiting_on_request", ...) ever mistakes one for a review row.
OVERVIEW_CHANNEL = "overview"
OVERVIEW_RULE_PREFIX = "overview_"
MORE_REQUESTS_ITEM_REF = "__more_requests__"

# "Where am I?"'s sections, most urgent first (step F §4). Missed, Today, Windows closing, Coming
# up, Waiting on, New suggestions and Couldn't see share the 6am review's names. It opens with the
# three highest scores across every section (Ciel and Brain Home PRD §4.5; Nic, 28 Sep: "Add a top
# line"), so a big thing can't hide under small ones.
SECTION_HIGHEST = "highest_right_now"
HIGHEST_RIGHT_NOW_COUNT = 3
# PRD §4.5 (Proposed - Nic to confirm), only while time blocks are on: the block Nic is in, at the top.
SECTION_NOW = "now"
SECTION_PICKED_FOR_TODAY = "picked_for_today"
SECTION_IN_PROGRESS = "in_progress"
SECTION_FOCUS_PROJECTS = "focus_projects"
SECTION_NO_DATE_YET = "no_date_yet"
SECTION_ROUTINES = "routines"
SECTION_FINISHED_THIS_WEEK = "finished_this_week"
SECTION_PARKED = "parked"
OVERVIEW_SECTIONS: tuple[str, ...] = (
    SECTION_NOW,
    SECTION_HIGHEST,
    SECTION_MISSED,
    SECTION_TODAY,
    SECTION_PICKED_FOR_TODAY,
    SECTION_IN_PROGRESS,
    SECTION_WINDOWS_CLOSING,
    SECTION_COMING_UP,
    SECTION_FOCUS_PROJECTS,
    SECTION_NO_DATE_YET,
    SECTION_ROUTINES,
    SECTION_WAITING_ON,
    SECTION_NEW_SUGGESTIONS,
    SECTION_FINISHED_THIS_WEEK,
    SECTION_PARKED,
    SECTION_COULD_NOT_SEE,
)
OVERVIEW_TITLES: dict[str, str] = {
    SECTION_NOW: "Now",
    SECTION_HIGHEST: "Highest right now",
    SECTION_MISSED: "Missed",
    SECTION_TODAY: "Today",
    SECTION_PICKED_FOR_TODAY: "Picked for today",
    SECTION_IN_PROGRESS: "In progress",
    SECTION_WINDOWS_CLOSING: "Windows closing",
    SECTION_COMING_UP: "Coming up",
    SECTION_FOCUS_PROJECTS: "Focus projects",
    SECTION_NO_DATE_YET: "No date yet",
    SECTION_ROUTINES: "Routines",
    SECTION_WAITING_ON: "Waiting on",
    SECTION_NEW_SUGGESTIONS: "New suggestions",
    SECTION_FINISHED_THIS_WEEK: "Finished this week",
    SECTION_PARKED: "Parked",
    SECTION_COULD_NOT_SEE: "Couldn't see",
}
# The sections that show Nic's open tasks by their own rules. An open task none of them shows goes
# to "Coming up" or "No date yet" instead (N17), so every open task shows at least once.
_SECTIONS_BEFORE_LEFTOVERS: tuple[str, ...] = (
    SECTION_MISSED,
    SECTION_TODAY,
    SECTION_PICKED_FOR_TODAY,
    SECTION_IN_PROGRESS,
    SECTION_WINDOWS_CLOSING,
    SECTION_WAITING_ON,
)
NO_DATE_YET_MORE_REF = "__more_no_date__"

_WEEKDAY_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAY_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# An item counts as part of a focus project when one of these links names the project itself
# (Proposed - Nic to confirm; step F §3, rule 5).
_PROJECT_LINK_TYPES = frozenset({"part_of", "about"})

# Project tasks (N4; step F §13, Proposed - Nic to confirm). One is named, in both asks and the
# why-log, ``project:<project>:<session_id>``; an answer to a pick names it back, so a reference
# longer than an answer can carry (128 characters) is a hash of the two instead.
PROJECT_REF_PREFIX = "project:"
_MAX_REF_CHARS = 128
# Only open and blocked tasks are listed or ranked; a closed one is done.
_PROJECT_TASK_STATES = frozenset({"open", "blocked"})
# A project task is a task: its sitrep carries no level, size, repeat or owed_to.
PROJECT_TASK_WORD = "task"
# "Where am I?" lists at most this many of one project's tasks, as its project summary does, then
# says how many more (Proposed - Nic to confirm).
FOCUS_PROJECT_TASK_CAP = 10
_MORE_TASKS_REF = "__more__"


@dataclass(frozen=True, slots=True)
class OrientationSignal:
    """The moment that triggered "what next?"; ``gap_minutes`` only for ``calendar_gap``."""

    kind: MomentSignal
    gap_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class OrientationInputs:
    """What both asks read. ``review`` is step E's own `ReviewInputs`, including its open
    where-I-stopped notes (``review.open_notes``; ``None`` when they couldn't be read).
    ``preferences`` are Nic's preference records, from which ``language.focus_preference`` finds
    his focus projects (N4). ``project_tasks`` are his projects' open and blocked tasks from the
    hub's task sitreps, one `sitreps.ProjectTasks` per project with how fresh its summary is
    (ROA-60; rebuild plan slice 8, part 2). ``None`` means they weren't read - sitreps are off,
    or couldn't be read - and then neither ask shows or ranks any project task."""

    review: ReviewInputs
    picked_today: frozenset[str] = frozenset()
    preferences: tuple[Record, ...] = ()
    project_tasks: tuple[ProjectTasks, ...] | None = None
    # Time blocks (Ciel and Brain Home PRD §4.3): off until Nic's Google consent (ROA-18); off,
    # both asks answer exactly as before (`brain_core.blocks`).
    blocks: bool = False
    # Today's task slots from the placement engine's plan (step I §5a; build plan slice 6), or
    # None while [placement] is off: then both asks answer exactly as before.
    plan_slots: tuple["PlannedSlot", ...] | None = None
    # FlowSavvy forecasts, explicitly distinct from placement's owner-set slots.
    # None slots in this mode means unavailable; an empty tuple is a valid empty schedule.
    schedule_mode: bool = False
    schedule_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlannedSlot:
    """One task slot in today's plan (step I): the task's first record ID (``key``, the same
    through every change of it), its title and first step, and its time."""

    key: str
    title: str
    start: datetime
    end: datetime
    first_step: str | None = None
    # A deep-focus fill task (not one of his picks or due today): never asked "Did it happen?".
    fill: bool = False


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One candidate in rank order: a saved item (``item``), or one of Nic's projects' tasks from
    its task sitrep (``task``); exactly one is set. ``rules`` are its reasons in plain words, the
    biggest first; ``points`` its score (``None`` under the nine rules), broken down by factor in
    ``fields_used["points"]``."""

    item: ItemRecord | None
    rules: tuple[str, ...]
    fields_used: dict[str, Any]
    started: bool = False
    task: TaskSitrep | None = None
    points: float | None = None

    @property
    def ref(self) -> str:
        """The item's lasting first ID, or the project task's reference (`project_task_ref`)."""

        return work.first_id(self.item.record) if self.item is not None else project_task_ref(self.task)

    @property
    def title(self) -> str:
        return self.item.record.title if self.item is not None else self.task.intent

    @property
    def state(self) -> str:
        return self.item.record.state if self.item is not None else self.task.state


@dataclass(frozen=True, slots=True)
class TopPick:
    """The up thing, its parent title, reader progress and Nic's supplied next action.

    ``record_id`` is a project task's reference when ``task`` is set (its sitrep).
    ``belongs_to`` names what the part belongs to; ``progress`` uses work.Progress.
    ``first_step`` carries his next action, never an invented agent step.
    """

    record_id: str
    title: str
    plain_word: str
    rules: tuple[str, ...]
    fields_used: dict[str, Any]
    first_step: str
    estimate_minutes: int | None
    time_box_minutes: int | None
    task: TaskSitrep | None = None
    belongs_to: str | None = None
    progress: Progress | None = None


@dataclass(frozen=True, slots=True)
class Alternative:
    record_id: str
    title: str
    plain_word: str
    rules: tuple[str, ...]
    fields_used: dict[str, Any]
    task: TaskSitrep | None = None
    belongs_to: str | None = None
    progress: Progress | None = None


@dataclass(frozen=True, slots=True)
class CanWaitItem:
    record_id: str
    title: str
    reason: str
    task: TaskSitrep | None = None


@dataclass(frozen=True, slots=True)
class InProgress:
    """One of Nic's own tasks with an open where-I-stopped note linked to it (rebuild plan G2):
    the note's next step, when it last changed, and whether it is stuck (N10b)."""

    record_id: str
    title: str
    session_id: str
    next_step: str
    last_changed: datetime
    stuck: bool


@dataclass(frozen=True, slots=True)
class NotEnough:
    """Instead of a pick: the plain reason, and as many questions as it needs (N7)."""

    reason: str
    questions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissedStart:
    """Something whose start time has passed (`review.start_passed`): "what next?" asks
    ``question`` about it, in these words, and never picks it (Accepted - Nic, 28 Sep 2026)."""

    record_id: str
    title: str
    question: str
    fields_used: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WhatNext:
    """With time blocks on (PRD §4.3), ``moment`` says what kind of work was offered and why."""

    top_pick: TopPick | None
    alternatives: tuple[Alternative, ...]
    can_wait: tuple[CanWaitItem, ...]
    in_progress: tuple[InProgress, ...]
    assumptions: tuple[str, ...]
    unknowns: tuple[str, ...]
    not_enough: NotEnough | None
    missed: tuple[MissedStart, ...] = ()
    moment: Mapping[str, Any] | None = None
    # Step I §5a: when the plan has a slot now but its task isn't the top pick, the plain words
    # saying so. None when there's no plan, no slot now, or the slot's task is the pick.
    plan_note: str | None = None


@dataclass(frozen=True, slots=True)
class OverviewLine:
    """One line of "where am I?". ``item_ref`` is a record ID, a GT request ID,
    ``calendar:<title>:<start>`` or a source name, as in the 6am review; ``rule`` is the why-log
    rule it is logged under; ``reasons`` are the ranking rules that placed an item."""

    section: str
    text: str
    item_ref: str
    rule: str
    reasons: tuple[str, ...] = ()
    record_sha256: str | None = None
    record_path: str | None = None
    record_state: str | None = None
    fields_used: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Overview:
    lines: tuple[OverviewLine, ...]
    sections: tuple[str, ...]

    @property
    def title(self) -> str:
        return work_words("orient")

    @property
    def is_empty(self) -> bool:
        return not self.lines


# --- Reading an item ---------------------------------------------------------------------------


def _timing(item: Record) -> Mapping[str, Any]:
    return item.data.get("timing") or {}


def _dates(item: Record, index: work.WorkIndex | None) -> dict[str, Any]:
    """The shared reader's date kinds for ``item``, read from the complete ``index`` every
    consumer shares (so an inherited deadline reads the same as an explicit one), or from the item
    alone when no index is given."""

    return work.dates_of(item, work.index_things((item,)) if index is None else index)


def _due(item: Record, index: work.WorkIndex | None = None) -> date | None:
    dates = _dates(item, index)
    raw = (dates.get("appointment") or dates.get("deadline") or {}).get("day")
    return date.fromisoformat(raw) if raw else None


def _start_time(item: Record, index: work.WorkIndex | None = None) -> datetime | None:
    due, at = _due(item, index), _at(item, index)
    return datetime.combine(due, time.fromisoformat(at), tzinfo=SINGAPORE) if due and at else None


def _missed(item: Record, now: datetime, index: work.WorkIndex | None = None) -> bool:
    start = _start_time(item, index)
    if start is not None:
        return start + timedelta(minutes=30) <= now
    due = _due(item, index)
    return due is not None and due < today_in_singapore(now)


def _due_tonight(item: Record, today: date, index: work.WorkIndex | None = None) -> bool:
    start = _start_time(item, index)
    return start is not None and start.date() == today + timedelta(days=1) and start.time() < time(6)


def _start_words(item: Record, today: date, index: work.WorkIndex | None = None) -> str:
    due, at = _due(item, index), _at(item, index)
    day = _day_word(due, today)
    if due < today and day != "yesterday":
        return f"at {_clock_word(at)} on {day}"
    return f"{day} at {_clock_word(at)}"


def _window_end(item: Record) -> date | None:
    return None  # Old windows are deadlines through the shared reader.


def _at(item: Record, index: work.WorkIndex | None = None) -> str | None:
    """The due date's time of day, ``HH:MM`` 24-hour Singapore time (step C §5), if one was given."""

    dates = _dates(item, index)
    return (dates.get("appointment") or {}).get("at")


def _padded_minutes(item: Record) -> int | None:
    return _timing(item).get("padded_minutes")


def _owed_to(item: Record) -> str | None:
    return item.data.get("owed_to")


def _link_target(item: Record, link_type: str) -> str | None:
    return next((link.get("target") for link in item.links if link.get("type") == link_type), None)


def _clock_word(at: str) -> str:
    """``"17:00"`` -> ``"5 pm"``, ``"09:30"`` -> ``"9:30 am"``: the spec's own reason wording (§3)."""

    hour, minute = (int(part) for part in at.split(":"))
    meridiem = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12} {meridiem}" if minute == 0 else f"{hour12}:{minute:02d} {meridiem}"


def _day_word(day: date, today: date) -> str:
    return relative_day_words(day, today)


def _is_v2_item(record: Record) -> bool:
    return record.schema == RECORD_SCHEMA_V2 and record.kind == "item"


def _accepted(record: Record) -> bool:
    return _is_v2_item(record) and record.data.get("level") == "accepted"


def _ids(record: Record) -> tuple[str, ...]:
    """A record's own ID and every older version's: links and picks may name any of them."""

    return (record.id, *record.supersedes)


def _picked(
    record: Record, picked_today: frozenset[str], today: date | None = None, index: work.WorkIndex | None = None,
) -> bool:
    while record is not None:
        if any(record_id in picked_today for record_id in _ids(record)):
            return True
        if today is not None:
            planned = _dates(record, index).get("planned") or {}
            day = today.isoformat()
            if planned.get("day") == day or bool(planned.get("start") and planned["start"] <= day <= planned["end"]):
                return True
        record = index.get(record.data.get("parent")) if index is not None else None
    return False


def _repeat_weekday(item: Record) -> int | None:
    repeat = item.data.get("repeat")
    if not repeat or not repeat.startswith("weekly:"):
        return None
    return _WEEKDAY_ABBR.get(repeat.split(":", 1)[1].strip().lower())


def _blocked_by_count(item: Record, candidates: Sequence[Record]) -> int:
    ids = set(_ids(item))
    return sum(
        1
        for other in candidates
        if other.id != item.id
        and any(target in ids for target in other.data.get("after", ()))
    )


def _goal_title(item: Record, candidates_by_id: Mapping[str, Record]) -> str | None:
    target = candidates_by_id.get(item.data.get("parent"))
    seen = set()
    while target is not None and target.id not in seen:
        seen.add(target.id)
        if work.kind_of(target, candidates_by_id) in ("goal", "project"):
            return target.title
        target = candidates_by_id.get(target.data.get("parent"))
    return None


def _confirmed_importance(item: Record) -> Mapping[str, Any] | None:
    """High importance counts only once Nic confirmed it (N5); a proposal alone never ranks."""

    importance = item.data.get("importance") or {}
    if importance.get("level") == "high" and importance.get("confirmed") is True:
        return importance
    return None


def same_project(focus_entry: str, project: str) -> bool:
    """The naming bridge (step F §13 item 14; Proposed - Nic to confirm): a focus project names a
    project when the two are equal ignoring case and surrounding spaces. Sitreps name a project
    by the vault ID the PC connector and the continuity tools use (``vault_gtbc_plans``), so a
    focus project an agent saved as that ID, resolved from Nic's words, names it. The same rule
    ties a saved task's ``part_of`` or ``about`` link to a focus project (rule 5)."""

    return focus_entry.strip().casefold() == project.strip().casefold()


def _matching_focus(project: str, focus_projects: Iterable[str]) -> str | None:
    """The focus project, as Nic saved it, that names ``project`` (the first in sorted order, if
    more than one does: they then differ only in case)."""

    return min((entry for entry in focus_projects if same_project(entry, project)), default=None)


def _focus_project(item: Record, focus_projects: frozenset[str]) -> str | None:
    for link in item.links:
        if link.get("type") in _PROJECT_LINK_TYPES:
            entry = _matching_focus(link["target"], focus_projects)
            if entry is not None:
                return entry
    return None


def focus_projects(preferences: Iterable[Record]) -> tuple[str, ...]:
    """Nic's focus projects in the order he named them: his newest focus preference's list (N4),
    or none."""

    preference = focus_preference(preferences)
    return tuple(preference.data["focus_projects"]) if preference is not None else ()


def focus_project_ids(preferences: Iterable[Record]) -> frozenset[str]:
    """Nic's focus projects: his newest focus preference's list (N4), or none."""

    return frozenset(focus_projects(preferences))


def project_task_ref(task: TaskSitrep) -> str:
    """How both asks and the why-log name one project task: ``project:<project>:<session_id>``,
    or ``project:`` and a hash of the two when that is longer than an answer can name back."""

    ref = f"{PROJECT_REF_PREFIX}{task.project}:{task.session_id}"
    if len(ref) <= _MAX_REF_CHARS:
        return ref
    digest = hashlib.sha256(f"{task.project}\x1f{task.session_id}".encode("utf-8")).hexdigest()
    return PROJECT_REF_PREFIX + digest[:32]


def _listed_tasks(summary: ProjectTasks) -> tuple[TaskSitrep, ...]:
    return tuple(task for task in summary.tasks if task.state in _PROJECT_TASK_STATES)


# --- Ranking (§3) -------------------------------------------------------------------------------


def _due_or_closing(
    item: Record, today: date, now_clock: str | None, index: work.WorkIndex | None = None,
    word_today: date | None = None
) -> tuple[str, dict[str, Any]] | None:
    """Rule 1: overdue, or due or closing today, or starting tonight before 06:00 (Accepted - Nic,
    28 Sep 2026). ``now`` only changes the wording of a due-today reason with a time of day ("was
    due at 9 am today"). Once that time is 30 minutes behind, the item is missed and "what next?"
    asks about it instead of ranking it (`what_next`)."""

    word_today = word_today or today
    due, window_end = _due(item, index), _window_end(item)
    fields: dict[str, Any] = {}
    if due is not None:
        fields["due"] = due.isoformat()
    if window_end is not None:
        fields["window_end"] = window_end.isoformat()
    if due is not None and due < today:
        return f"was due {_day_word(due, word_today)}", fields
    if due == today:
        at = _at(item, index)
        if at is None:
            return f"due {_day_word(today, word_today)}", fields
        fields["at"] = at
        passed = now_clock is not None and at < now_clock
        return (f"was due at {_clock_word(at)} {_day_word(today, word_today)}" if passed else f"due {_day_word(today, word_today)} at {_clock_word(at)}"), fields
    if _due_tonight(item, today, index):
        fields["at"] = _at(item, index)
        return f"due tonight at {_clock_word(fields['at'])}", fields
    if window_end is not None and window_end < today:
        return f"window closed {_day_word(window_end, word_today)}", fields
    if window_end == today:
        return f"window closes {_day_word(today, word_today)}", fields
    return None


def rank(
    candidates: Sequence[ItemRecord],
    today: date,
    *,
    picked_today: frozenset[str] = frozenset(),
    focus_projects: frozenset[str] = frozenset(),
    notes: Sequence[OpenNote] = (),
    signal: OrientationSignal | None = None,
    now: datetime | None = None,
    project_tasks: Sequence[TaskSitrep] = (),
    ranking: str = RANKING_POINTS,
    prepared_index=None,
) -> tuple[RankedCandidate, ...]:
    """Order candidates, the most important first: by points (`_rank_by_points`, PRD §5), or by
    §3's nine rules while ``ranking`` is `RANKING_RULES` (`_rank_by_rules`).

    Only the shared reader's up things compete; all supplied things remain available
    to compute dependencies and inherited facts. ``signal``
    never changes the order: at a calendar gap only the top pick's reason says whether it fits
    (§3). ``now`` measures due dates to the minute; without it, the start of ``today``."""

    if ranking not in RANKINGS:
        raise ValueError(f"ranking must be one of {sorted(RANKINGS)}, not {ranking!r}")
    index = work.index_things(entry.record for entry in candidates) if prepared_index is None else prepared_index
    raw = {entry.record.id: entry for entry in candidates}
    entries = {key: raw[record.id] for key, record in index.items()}
    candidates = tuple(replace(entries[key], record=record) for key, record in index.items())
    arguments = dict(
        picked_today=picked_today, focus_projects=focus_projects, notes=notes, signal=signal, now=now,
        project_tasks=project_tasks, reader_index=index,
    )
    ranked = (_rank_by_rules if ranking == RANKING_RULES else _rank_by_points)(candidates, today, **arguments)
    up = {work.first_id(record) for record in work.up_things(index, now or today)}
    return tuple(replace(candidate, item=entries[candidate.ref]) if candidate.item is not None else candidate
                 for candidate in ranked if candidate.ref in up or (candidate.task is not None and candidate.state == "open"))


def _rank_by_rules(
    candidates: Sequence[ItemRecord],
    today: date,
    *,
    picked_today: frozenset[str] = frozenset(),
    focus_projects: frozenset[str] = frozenset(),
    notes: Sequence[OpenNote] = (),
    signal: OrientationSignal | None = None,
    now: datetime | None = None,
    project_tasks: Sequence[TaskSitrep] = (),
    reader_index: work.WorkIndex,
) -> tuple[RankedCandidate, ...]:
    """The way back (Accepted - Nic, 28 Sep 2026: "Yes, keep a way back"): §3's nine rules, as
    ranked before points (redesign default 1), until a week of real use.

    1. overdue, or due or closing today; 2. picked for today; 3. someone waiting, due within 3
    days; 4. high importance Nic confirmed (N5); 5. part of a focus project (N4); 6. other items
    are waiting on it; 7. a routine due today; 8. part of an accepted goal; 9. everything else,
    oldest first. The first rule that separates two candidates wins. At the same level, one Nic
    has started - an open where-I-stopped note linked to it (N12b) - beats a new one (N10a); then
    the oldest (by when its newest version was saved, as the 6am review orders), then record ID.

    Waiting and blocked items are ranked too, so their order under "can wait" follows the same
    rules, but only an ``open`` item can be picked (`what_next`). ``signal`` never changes the
    rank: at a calendar gap only the top pick's reason says whether it fits (§3).

    ``project_tasks`` (from task sitreps, N4) rank by the same rules (Proposed - Nic to
    confirm). A sitrep carries no date, importance, repeat or link, so only rule 5 can match: a
    project one of Nic's focus projects names (`same_project`). Each counts as started, since it
    exists only while its where-I-stopped note is open. "Oldest" is when that note last changed,
    and its reference (`project_task_ref`) stands in for the record ID.
    """

    word_today = work._day(now) if now is not None else today
    now_clock = now.astimezone(SINGAPORE).strftime("%H:%M") if now is not None else None
    records = tuple(entry.record for entry in candidates)
    by_id = reader_index
    ranked: list[tuple[tuple[Any, ...], RankedCandidate]] = []
    for entry in candidates:
        item = entry.record
        matches: list[tuple[int, str, dict[str, Any]]] = []

        closing = _due_or_closing(item, today, now_clock, by_id, word_today)
        if closing is not None:
            matches.append((1, *closing))
        if _picked(item, picked_today, word_today, by_id):
            matches.append((2, "you picked it for today", {}))
        due, owed_to = _due(item, by_id), _owed_to(item)
        if owed_to and due is not None and 0 <= (due - today).days <= WAITING_WINDOW_DAYS:
            matches.append(
                (3, f"{owed_to} is waiting; due {_day_word(due, word_today)}", {"owed_to": owed_to, "due": due.isoformat()})
            )
        importance = _confirmed_importance(item)
        if importance is not None:
            reason = importance.get("reason")
            matches.append(
                (4, f"high: {reason}" if reason else "high importance", {"importance": {"level": "high", "reason": reason}})
            )
        project = _focus_project(item, focus_projects) or _matching_focus(_goal_title(item, by_id) or "", focus_projects)
        if project is not None:
            matches.append((5, f"focus project: {project}", {"focus_project": project}))
        blocked_count = _blocked_by_count(item, records)
        if blocked_count:
            label = "1 thing is waiting on this" if blocked_count == 1 else f"{blocked_count} things are waiting on this"
            matches.append((6, label, {"blocked_count": blocked_count}))
        weekday = _repeat_weekday(item)
        if weekday is not None and weekday == today.weekday():
            matches.append((7, f"every {_WEEKDAY_FULL[weekday]}, due today", {"repeat": item.data.get("repeat")}))
        goal = _goal_title(item, by_id)
        if goal is not None:
            matches.append((8, f'part of "{goal}"', {"goal": goal}))

        started = work.progress_of(item, by_id)["started"]
        labels = [label for _number, label, _extra in matches]
        if started:
            labels.append("already started")
        fields_used: dict[str, Any] = {}
        for _number, _label, extra in matches:
            fields_used.update(extra)
        if started:
            fields_used["started"] = True

        matched = frozenset(number for number, _label, _extra in matches)
        comparator = tuple(0 if number in matched else 1 for number in range(1, 9))
        sort_key = (comparator, 0 if started else 1, item.created_at, item.id)
        ranked.append((
            sort_key,
            RankedCandidate(item=entry, rules=tuple(labels) or ("oldest open",), fields_used=fields_used, started=started),
        ))

    for task in project_tasks:
        focus = _matching_focus(task.project, focus_projects)
        task_labels = ([f"focus project: {focus}"] if focus is not None else []) + ["already started"]
        task_fields: dict[str, Any] = {"focus_project": focus} if focus is not None else {}
        task_fields.update(project=task.project, session_id=task.session_id, started=True)
        comparator = tuple(0 if number == 5 and focus is not None else 1 for number in range(1, 9))
        ranked.append((
            (comparator, 0, task.note_updated_at, project_task_ref(task)),
            RankedCandidate(item=None, rules=tuple(task_labels), fields_used=task_fields, started=True, task=task),
        ))

    ranked.sort(key=lambda pair: pair[0])
    return tuple(candidate for _key, candidate in ranked)


# --- Ranking by points (Ciel and Brain Home PRD §5) ---------------------------------------------

# The PRD's table (Accepted - Nic, 28 Sep 2026: "Points, Taskwarrior-style"): Taskwarrior's own
# default urgency weights ("i think its fine"), with someone waiting and today's picks at 8 ("8,
# like blocking"). One line per factor, so each change is one reviewable line; change one at a
# time, by about 1.0, and never let one dominate (Taskwarrior's own advice). How long a task takes
# adds nothing ("No, Taskwarrior's way"): the two-minute rule catches tiny tasks when they arrive.
POINTS: Mapping[str, float] = MappingProxyType({
    "picked": 8.0,       # in today's picks (Taskwarrior's +next)
    "due": 12.0,         # its due date, graded by `due_fraction`
    "blocking": 8.0,     # other tasks wait on it (blocking), and it inherits their score
    "waiting": 8.0,      # someone's waiting on Nic: it's a promise (owed_to)
    "hurts_weeks": 8.0,  # confirmed cost like a date due in about 36 hours
    "start_by": 5.0,     # its start-by date has come (scheduled)
    "focus": 5.0,        # part of one of Nic's focus projects (a per-project weight)
    "started": 4.0,      # started: an open where-I-stopped note (active)
    "age": 2.0,          # older counts a little more, up to a year
    "goal": 1.0,         # part of an accepted goal (project)
})
# Taskwarrior's `urgency_due`: 0.2 two weeks or more before the date, rising in a straight line to
# 1.0 at a week overdue, measured here to the minute.
DUE_FLOOR = 0.2
DUE_RISES_FROM_DAYS = 14
DUE_FULL_AFTER_DAYS = 7
# Taskwarrior's `urgency.age.max`: age counts in whole days, up to a year.
AGE_FULL_AFTER_DAYS = 365
# Taskwarrior's `urgency.inherit`: a task never scores below one that waits on it, and scores just
# above it, so it sorts first.
INHERIT_MARGIN = 0.01
# A date with no time of day is due by the end of that day, as the review's "missed" reads it.
END_OF_DAY = time(23, 59)
# The order reasons with the same points are named in.
_FACTOR_ORDER = (
    "picked", "due", "blocking", "waiting", "stake", "start_by", "focus", "started", "goal", "age",
)


def due_fraction(due_at: datetime, now: datetime) -> float:
    """How much of the due weight a date earns at ``now`` (Taskwarrior's `urgency_due`), both
    counted to the minute: `DUE_FLOOR` until two weeks before, then a straight line to 1.0 at a
    week overdue."""

    minutes_overdue = (now.replace(second=0, microsecond=0) - due_at) // timedelta(minutes=1)
    days_overdue = minutes_overdue / (24 * 60)
    if days_overdue >= DUE_FULL_AFTER_DAYS:
        return 1.0
    if days_overdue >= -DUE_RISES_FROM_DAYS:
        span = DUE_RISES_FROM_DAYS + DUE_FULL_AFTER_DAYS
        return (days_overdue + DUE_RISES_FROM_DAYS) * (1.0 - DUE_FLOOR) / span + DUE_FLOOR
    return DUE_FLOOR


def _routine_day(item: Record, today: date) -> date | None:
    """A weekly routine's next day, today included unless today's is already done (PRD §4.7)."""

    weekday = _repeat_weekday(item)
    if weekday is None:
        return None
    ahead = (weekday - today.weekday()) % 7
    if ahead == 0 and done_today(item, today):
        ahead = 7
    return today + timedelta(days=ahead)


def due_moment(item: Record, today: date, index: work.WorkIndex | None = None) -> tuple[datetime, str] | None:
    """When an item is due, to the minute in Singapore, and what set it: ``"due"``, its date at
    its time of day, or at `END_OF_DAY` without one; ``"window"``, its window's last day
    (Proposed - Nic to confirm, PRD §5); ``"routine"``, a weekly routine's next day, today
    included, when it has no date of its own (Proposed - Nic to confirm, PRD §5). ``None`` for
    anything undated. ``index`` is the complete reader index, so a deadline inherited from a
    parent counts as the item's own."""

    due = _due(item, index)
    if due is not None:
        at = _at(item, index)
        return datetime.combine(due, time.fromisoformat(at) if at else END_OF_DAY, tzinfo=SINGAPORE), "due"
    window_end = _window_end(item)
    if window_end is not None:
        return datetime.combine(window_end, END_OF_DAY, tzinfo=SINGAPORE), "window"
    day = (today + timedelta(days=int(done_today(item, today)))) if item.data.get("repeat") == "daily" else _routine_day(item, today)
    if day is not None:
        return datetime.combine(day, END_OF_DAY, tzinfo=SINGAPORE), "routine"
    return None


def _due_reason(item: Record, source: str, today: date, now_clock: str, index: work.WorkIndex | None = None,
                word_today: date | None = None) -> str:
    """The due date in the words the review and both asks already use."""

    word_today = word_today or today
    if source == "routine":
        if item.data.get("repeat") == "daily":
            day = today + timedelta(days=int(done_today(item, today)))
            return (f"every day, due {_day_word(day, word_today)}" if day == today
                    else f"every day; next {_day_word(day, word_today)}")
        day = _routine_day(item, today)
        weekday = _WEEKDAY_FULL[day.weekday()]
        return f"every {weekday}, due {_day_word(day, word_today)}" if day == today else f"every {weekday}; next {_day_word(day, word_today)}"
    if source == "window":
        end = _window_end(item)
        if end < today:
            return f"window closed {_day_word(end, word_today)}"
        return f"window closes {_day_word(today, word_today)}" if end == today else f"window closes {_day_word(end, word_today)}"
    due, at = _due(item, index), _at(item, index)
    if due < today:
        return f"was due {_day_word(due, word_today)}"
    if due == today:
        if at is None:
            return f"due {_day_word(today, word_today)}"
        return f"was due at {_clock_word(at)} {_day_word(today, word_today)}" if at < now_clock else f"due {_day_word(today, word_today)} at {_clock_word(at)}"
    if _due_tonight(item, today, index):
        return f"due tonight at {_clock_word(at)}"
    return f"due {_day_word(due, word_today)}" + (f" at {_clock_word(at)}" if at else "")


def _stake(item: Record) -> tuple[float, str, dict[str, Any]] | None:
    """Confirmed stake only: its points, cost line and facts; life changes pin separately."""
    stake = item.data.get("stake") or {}
    if stake.get("confirmed") is not True:
        return None
    words = [stake[key] for key in ("at_stake", "odds") if stake.get(key)]
    if stake.get("cost_shape") == "grows":
        words.append("cost grows while you wait")
    elif stake.get("cost_shape") == "lands":
        words.append("cost lands if missed")
    return (POINTS["hurts_weeks"] if stake.get("level") == "hurts_weeks" else 0.0,
            "; ".join(words) or stake_words(stake.get("level", "stake confirmed")), {"stake": dict(stake)})


@dataclass(slots=True)
class _Score:
    """One saved item's points by factor, each factor's reason, and the facts behind them."""

    entry: ItemRecord
    factors: dict[str, float]
    reasons: dict[str, str]
    fields: dict[str, Any]
    started: bool = False

    @property
    def own(self) -> float:
        return sum(self.factors.values())


def _score_item(
    entry: ItemRecord,
    today: date,
    now: datetime,
    now_clock: str,
    *,
    picked_today: frozenset[str],
    focus_projects: frozenset[str],
    notes: Sequence[OpenNote],
    by_id: Mapping[str, Record],
    blocks: Sequence[Record],
    word_today: date,
) -> _Score:
    item = entry.record
    score = _Score(entry=entry, factors={}, reasons={}, fields={})

    def add(factor: str, points: float, reason: str | None, **fields: Any) -> None:
        score.factors[factor] = points
        if reason is not None:
            score.reasons[factor] = reason
        score.fields.update(fields)

    if _picked(item, picked_today, word_today, by_id):
        add("picked", POINTS["picked"], "you picked it for today")
    moment = due_moment(item, today, by_id)
    if moment is not None:
        due_at, source = moment
        fields: dict[str, Any] = {"due_at": due_at.isoformat(), "due_from": source}
        if source == "due":
            fields["due"] = _due(item, by_id).isoformat()
            if _at(item, by_id):
                fields["at"] = _at(item, by_id)
        elif source == "window":
            fields["window_end"] = _window_end(item).isoformat()
        else:
            fields["repeat"] = item.data.get("repeat")
        add("due", POINTS["due"] * due_fraction(due_at, now), _due_reason(item, source, today, now_clock, by_id, word_today), **fields)
    if blocks:
        reason = (
            f'"{blocks[0].title}" is waiting on this' if len(blocks) == 1
            else f"{len(blocks)} things are waiting on this"
        )
        add("blocking", POINTS["blocking"], reason, blocked_count=len(blocks))
    owed_to = _owed_to(item)
    if owed_to:
        add("waiting", POINTS["waiting"], f"{owed_to} is waiting", owed_to=owed_to)
    stake = _stake(item)
    if stake is not None:
        add("stake", stake[0], stake[1], **stake[2])
    start = work.dates_of(item, by_id).get("start_by") or {}
    start_by = start.get("day")
    if start_by is not None and start.get("confirmed") is not False and date.fromisoformat(start_by) <= today:
        day = date.fromisoformat(start_by)
        reason = f"start by {_day_word(day, word_today)}" if day == today else f"start by {_day_word(day, word_today)}, now passed"
        add("start_by", POINTS["start_by"], reason, start_by=start_by)
    project = _focus_project(item, focus_projects) or _matching_focus(_goal_title(item, by_id) or "", focus_projects)
    if project is not None:
        add("focus", POINTS["focus"], f"focus project: {project}", focus_project=project)
    if work.progress_of(item, by_id)["started"]:
        score.started = True
        add("started", POINTS["started"], "already started", started=True)
    goal = _goal_title(item, by_id)
    if goal is not None:
        add("goal", POINTS["goal"], f'part of "{goal}"', goal=goal)
    saved = first_saved(item)
    age_days = max(0, (now - saved) // timedelta(days=1))
    age = POINTS["age"] * min(age_days / AGE_FULL_AFTER_DAYS, 1.0)
    if age:
        add("age", age, None, first_saved=saved.isoformat())
    return score


def _blocking(index: work.WorkIndex) -> dict[str, tuple[Record, ...]]:
    """For each candidate, the other candidates that wait on it: a ``waiting_on`` or
    ``blocked_by`` link naming any of its versions, as ``_blocked_by_count`` counts them."""

    waiting: dict[str, dict[str, Record]] = {}
    for record in index.values():
        if not _accepted(record) or record.state not in OPEN_STATES:
            continue
        for target in record.data.get("after", ()):
            blocker = index.get(target)
            if blocker is not None and blocker.id != record.id:
                waiting.setdefault(work.first_id(blocker), {})[work.first_id(record)] = record
    return {
        blocker: tuple(sorted(found.values(), key=lambda record: (record.created_at, record.id)))
        for blocker, found in waiting.items()
    }


def _settle(
    scores: Mapping[str, _Score], blocking: Mapping[str, tuple[Record, ...]]
) -> dict[str, tuple[float, str | None]]:
    """Each item's final points and, when it inherited them, the title of what it inherited from
    (Taskwarrior's `urgency.inherit`, all the way down the chain): a task scores at least
    `INHERIT_MARGIN` above the best task waiting on it. A loop of tasks waiting on each other is
    cut where it closes, so the same records always give the same answer."""

    settled: dict[str, tuple[float, str | None]] = {}

    def visit(record_id: str, path: frozenset[str]) -> float:
        if record_id in settled:
            return settled[record_id][0]
        own = scores[record_id].own
        best: tuple[float, str] | None = None
        for waiting in blocking.get(record_id, ()):
            waiting_id = work.first_id(waiting)
            if waiting_id in path or waiting_id == record_id:
                continue
            points = visit(waiting_id, path | {record_id})
            if best is None or points > best[0]:
                best = (points, waiting.title)
        if best is not None and best[0] + INHERIT_MARGIN > own:
            settled[record_id] = (best[0] + INHERIT_MARGIN, best[1])
        else:
            settled[record_id] = (own, None)
        return settled[record_id][0]

    for record_id in sorted(scores):
        visit(record_id, frozenset({record_id}))
    return settled


def _breakdown(factors: Mapping[str, float], total: float) -> dict[str, float]:
    """``fields_used["points"]``: the total, then every factor's points, the biggest first."""

    order = sorted(factors, key=lambda name: (-factors[name], name))
    return {"total": round(total, 2), **{name: round(factors[name], 2) for name in order}}


def _explained(
    score: _Score, total: float, inherited_from: str | None, today: date
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Reasons in plain words, the biggest first, and the facts with the full breakdown."""

    factors = dict(score.factors)
    if inherited_from is not None:
        factors["inherited"] = total - score.own

    def size(factor: str) -> float:
        return factors[factor] + (factors.get("inherited", 0.0) if factor == "blocking" else 0.0)

    reasons = [
        score.reasons[factor]
        for factor in sorted(score.reasons, key=lambda factor: (-size(factor), _FACTOR_ORDER.index(factor)))
    ]
    if (score.entry.record.data.get("stake") or {}).get("level") == "changes_life" and "stake" in score.reasons:
        reasons.remove(score.reasons["stake"])
        reasons.insert(0, score.reasons["stake"])
    if not reasons:
        saved = work._day(first_saved(score.entry.record))
        reasons.append(f"no date or stake yet; saved {_day_word(saved, today)}")
    fields = dict(score.fields)
    if inherited_from is not None:
        fields["inherited_from"] = inherited_from
    fields["points"] = _breakdown(factors, total)
    return tuple(reasons), fields


def _rank_by_points(
    candidates: Sequence[ItemRecord],
    today: date,
    *,
    picked_today: frozenset[str],
    focus_projects: frozenset[str],
    notes: Sequence[OpenNote],
    signal: OrientationSignal | None,
    now: datetime | None,
    project_tasks: Sequence[TaskSitrep],
    reader_index: work.WorkIndex,
) -> tuple[RankedCandidate, ...]:
    """PRD §5: each candidate's points are the sum of its factors' weights (`POINTS`), the due
    date graded by `due_fraction`; a task others wait on never scores below them (`_settle`).
    The highest score first; ties go to the one that has gone longest without a change (when its
    newest version was saved, as before), then the record ID.

    A project task from a task sitrep (N4) carries no date, stake or link, so it scores for its
    focus project (when one of Nic's focus projects names it) and for being started, which it
    always is; "without a change" is when its note last changed, and its reference stands in for
    the record ID."""

    moment = now if now is not None else datetime.combine(today, time(0, 0), tzinfo=SINGAPORE)
    now_clock = moment.astimezone(SINGAPORE).strftime("%H:%M")
    records = tuple(entry.record for entry in candidates)
    by_id = reader_index
    blocking = _blocking(reader_index)
    scores = {
        work.first_id(entry.record): _score_item(
            entry, today, moment, now_clock, picked_today=picked_today, focus_projects=focus_projects,
            notes=notes, by_id=by_id, blocks=blocking.get(work.first_id(entry.record), ()),
            word_today=work._day(now) if now is not None else today,
        )
        for entry in candidates
    }
    settled = _settle(scores, blocking)

    ranked: list[tuple[tuple[Any, ...], RankedCandidate]] = []
    for entry in candidates:
        item = entry.record
        stake = item.data.get("stake") or {}
        pinned = stake.get("level") == "changes_life" and stake.get("confirmed") is True
        total, inherited_from = settled[work.first_id(item)]
        reasons, fields = _explained(scores[work.first_id(item)], total, inherited_from, work._day(now) if now is not None else today)
        ranked.append((
            (0 if pinned else 1, -round(total, 6), item.updated_at, item.id),
            RankedCandidate(
                item=entry, rules=reasons, fields_used=fields, started=scores[work.first_id(item)].started, points=round(total, 2),
            ),
        ))

    for task in project_tasks:
        focus = _matching_focus(task.project, focus_projects)
        factors = {"started": POINTS["started"], **({"focus": POINTS["focus"]} if focus is not None else {})}
        total = sum(factors.values())
        task_fields: dict[str, Any] = {"focus_project": focus} if focus is not None else {}
        task_fields.update(project=task.project, session_id=task.session_id, started=True)
        task_fields["points"] = _breakdown(factors, total)
        reasons = ([f"focus project: {focus}"] if focus is not None else []) + ["already started"]
        ranked.append((
            (1, -round(total, 6), task.note_updated_at, project_task_ref(task)),
            RankedCandidate(
                item=None, rules=tuple(reasons), fields_used=task_fields, started=True, task=task,
                points=round(total, 2),
            ),
        ))

    ranked.sort(key=lambda pair: pair[0])
    return tuple(candidate for _key, candidate in ranked)


def surfaced_unchanged(item_ref: str, record_sha256: str, history: Sequence[HistoryRow]) -> bool:
    """Surfaced on 2+ earlier days in this same version (§5's "tends to stall"): for a saved item
    its record's hash, for a project task its note's (the sitrep's pointer). Rows "where am I?"
    wrote (channel ``overview``) never count, nor do the 6am review's "Coming up" rows (N16): a
    task listed ahead of its date was only shown, not brought back. ``history`` must already be
    filtered to rows earlier than today; this function does no date filtering."""

    earlier_days = {
        today_in_singapore(row.surfaced_at)
        for row in history
        if row.item_ref == item_ref
        and row.record_sha256 == record_sha256
        and row.channel != OVERVIEW_CHANNEL
        and row.rule != COMING_UP_RULE
    }
    return len(earlier_days) >= STALL_THRESHOLD_DAYS


def tends_to_stall(item: Record, history: Sequence[HistoryRow]) -> bool:
    """Surfaced on 2+ earlier days with no change to the record since (§5)."""

    return surfaced_unchanged(work.first_id(item), item.sha256, history)


def time_box(item: Record | None, stalls: bool) -> int | None:
    """20 minutes for a stalling item, or its padded estimate if that's shorter (§5, N3). A
    project task (``None``) has no estimate, so it gets the 20 minutes."""

    if not stalls:
        return None
    padded = _padded_minutes(item) if item is not None else None
    if padded is not None and padded < STALL_TIME_BOX_MINUTES:
        return padded
    return STALL_TIME_BOX_MINUTES


def caution(declined: Record, today: date, index: work.WorkIndex | None = None) -> str | None:
    """The fixed one-line caution (§6), only when the declined item is due or closes today, or is
    due tonight."""

    if _due(declined, index) == today or _window_end(declined) == today:
        return CAUTION_TEXT.format(title=declined.title)
    if _due_tonight(declined, today, index):
        return CAUTION_TONIGHT_TEXT.format(title=declined.title)
    return None


# --- In progress (N10b, N10c, N10d) -------------------------------------------------------------


def in_progress(items: Iterable[ItemRecord], notes: Iterable[OpenNote], now: datetime) -> tuple[InProgress, ...]:
    """Started facts and next from the shared reader, with no personal-note fallback.

    Latest change includes parts; three days without a change marks it stuck.
    Handed-off request notes remain on their own existing surface.
    """

    today = today_in_singapore(now)
    supplied = tuple(items)
    index = work.index_things(entry.record for entry in supplied)
    result: list[InProgress] = []
    for record in index.values():
        if not _accepted(record) or work.status_of(record, index, now) in ("ended", "parked"):
            continue
        progress = work.progress_of(record, index)
        if not progress["started"]:
            continue
        next_ = work.next_of(record, index, now)
        next_record = index.get(next_["item_ref"], record)
        if any(mark.get("mark") == "agents_wording" and mark.get("part") == "next_action" for mark in next_record.data.get("review_marks", ())):
            next_ = {**next_, "text": next_record.title}
        changed = max((part.updated_at for part in index.values()
                       if part.data.get("parent") == work.first_id(record)), default=record.updated_at)
        changed = max(record.updated_at, changed)
        result.append(InProgress(record_id=work.first_id(record), title=record.title, session_id="",
                                 next_step=next_["text"], last_changed=changed,
                                 stuck=(today - today_in_singapore(changed)).days >= 3))
    return tuple(result)


# --- What both asks share -----------------------------------------------------------------------


def _unknowns(source_status: Mapping[str, str | None]) -> tuple[str, ...]:
    """Every source the 6am review's own "Couldn't see" would list (E §3's rule)."""

    return tuple(
        f"{source}: {status}" for source, status in source_status.items() if status not in (None, "", "ok")
    )


@dataclass(frozen=True, slots=True)
class _Picture:
    now: datetime
    today: date
    items: tuple[ItemRecord, ...]
    live: tuple[ItemRecord, ...]
    notes: tuple[OpenNote, ...]
    ranked: tuple[RankedCandidate, ...]
    index: work.WorkIndex


def _focus_tasks(project_tasks: tuple[ProjectTasks, ...] | None, focus: frozenset[str]) -> tuple[TaskSitrep, ...]:
    """The open and blocked tasks of Nic's focus projects: a project task competes only when one
    of his focus projects names its project (N4; Proposed - Nic to confirm)."""

    return tuple(
        task
        for summary in project_tasks or ()
        if _matching_focus(summary.project, focus) is not None
        for task in _listed_tasks(summary)
    )


def _picture(inputs: OrientationInputs, now: datetime, signal: OrientationSignal | None = None) -> _Picture:
    """Newest versions only (G4); every accepted item still open, waiting or blocked, and every
    open or blocked task of Nic's focus projects, ranked."""

    today = today_in_singapore(now)
    index = work.index_things(entry.record for entry in inputs.review.items)
    originals = {entry.record.id: entry for entry in inputs.review.items}
    items = tuple(originals[record.id] for record in index.values())
    live = tuple(entry for entry in items if _accepted(entry.record) and work.status_of(entry.record, index, now) not in ("ended", "parked"))
    notes = tuple(inputs.review.open_notes or ())
    focus = focus_project_ids(inputs.preferences)
    ranked = rank(
        items, today, picked_today=inputs.picked_today, focus_projects=focus, notes=notes, signal=signal, now=now,
        project_tasks=_focus_tasks(inputs.project_tasks, focus), ranking=inputs.review.ranking,
    )
    return _Picture(now=now, today=today, items=items, live=live, notes=notes, ranked=ranked, index=index)


def _named_in_progress(picture: _Picture, now: datetime) -> tuple[InProgress, ...]:
    progress = _ranked_in_progress(picture, now)
    return progress if len(progress) >= IN_PROGRESS_NAMED_AT else ()


def _ranked_in_progress(picture: _Picture, now: datetime) -> tuple[InProgress, ...]:
    """In progress, most important first (§3). Nic's own saved tasks only (G2): project tasks
    show under "Focus projects" instead, and never count towards N10d's three."""

    order = {candidate.ref: index for index, candidate in enumerate(picture.ranked)}
    live_ids = {work.first_id(entry.record) for entry in picture.live}
    progress = (entry for entry in in_progress(picture.items, picture.notes, now) if entry.record_id in live_ids)
    return tuple(sorted(progress, key=lambda entry: (order.get(entry.record_id, len(order)), entry.record_id)))


# --- "What next?" (N2, §5) ----------------------------------------------------------------------


def _summary_owner(record: Record, index: work.WorkIndex) -> Record | None:
    owner = None
    while record.data.get("parent") in index:
        record = index[record.data["parent"]]
        if work.kind_of(record, index) in {"task", "subtask", "project"}:
            owner = record
    return owner


def _summary_candidates(candidates: Iterable[RankedCandidate], index: work.WorkIndex,
                        now: datetime, excluded: set[str]) -> tuple[RankedCandidate, ...]:
    """One reader-selected eligible next per parent, keeping its rank and pin."""
    eligible_index = {ref: record for ref, record in index.items() if ref not in excluded}
    result = []
    for candidate in candidates:
        if candidate.ref in excluded:
            continue
        if candidate.item is not None:
            record = index[candidate.ref]
            owner = _summary_owner(record, index)
            if owner is not None and work.next_of(owner, eligible_index, now)["item_ref"] != candidate.ref:
                continue
        result.append(candidate)
    return tuple(result)


def _can_wait_reason(item: Record, today: date, index: work.WorkIndex | None = None,
                word_today: date | None = None) -> str:
    if item.state == "waiting":
        target = _link_target(item, "waiting_on") or _owed_to(item)
        return f"waiting on {target}" if target else "waiting on someone else"
    if item.state == "blocked":
        target = _link_target(item, "blocked_by")
        return f"blocked by {target}" if target else "blocked on something else"
    word_today = word_today or today
    due, window_end = _due(item, index), _window_end(item)
    if _due_tonight(item, today, index):
        return f"also due tonight at {_clock_word(_at(item, index))}"
    if due is not None and due > today:
        at = _at(item, index)
        return f"due {_day_word(due, word_today)}" + (f" at {_clock_word(at)}" if at else "")
    if due is not None:
        return f"also due {_day_word(due, word_today)}" if due == today else f"also overdue: was due {_day_word(due, word_today)}"
    if window_end is not None and window_end > today:
        return f"window closes {_day_word(window_end, word_today)}"
    if window_end is not None:
        return f"window also closes {_day_word(window_end, word_today)}" if window_end == today else f"window also closed {_day_word(window_end, word_today)}"
    return "no date set yet"


def _candidate_can_wait_reason(candidate: RankedCandidate, today: date, index: work.WorkIndex | None = None,
                               word_today: date | None = None) -> str:
    """A saved item's reason, or a project task's: blocked with its first blocker, else undated."""

    if candidate.item is not None:
        return _can_wait_reason(candidate.item.record, today, index, word_today)
    task = candidate.task
    if task.state == "blocked":
        return f"blocked: {task.blockers[0]}" if task.blockers else "blocked on something else"
    return "no date set yet"


def _plain_word(candidate: RankedCandidate) -> str:
    return plain_word(candidate.item.record.data) if candidate.item is not None else PROJECT_TASK_WORD


def _assumptions(item: Record) -> tuple[str, ...]:
    timing = _timing(item)
    resolved = timing.get("resolved_from") or {}
    words = resolved.get("words")
    target = timing.get("due") or (timing.get("window") or {}).get("start")
    if not words or not target:
        return ()
    return (f"took '{words}' to mean {target}",)


def _waiting_question(item: Record) -> str:
    target = _link_target(item, "waiting_on") or _owed_to(item) or "someone else"
    return f'Is "{item.title}" still waiting on {target}?'


PLANNED_FOR_NOW = "planned for now"
SCHEDULED_FOR_NOW = "scheduled for now"


def _slot_key(record: Record) -> str:
    return work.first_id(record)


def _slot_clock(moment_: datetime) -> str:
    return _clock_word(moment_.astimezone(SINGAPORE).strftime("%H:%M"))


def _open_keys(picture: _Picture) -> frozenset[str]:
    return frozenset(_slot_key(entry.record) for entry in picture.live if entry.record.state == "open")


def _current_slot(slots: Sequence[PlannedSlot], now: datetime, open_keys: frozenset[str]) -> PlannedSlot | None:
    """The slot now, only while its task is still open: one Nic finished early isn't "now"."""

    return next(
        (slot for slot in sorted(slots, key=lambda slot: slot.start) if slot.start <= now < slot.end and slot.key in open_keys),
        None,
    )


def _schedule_slots(inputs: OrientationInputs, picture: _Picture) -> tuple[PlannedSlot, ...]:
    """Join the forecast to fresh records; a remote title/state never overrides Brain Home."""
    from brain_core.schedule import eligible_at, needs_split
    slots = []
    for slot in inputs.plan_slots or ():
        record = picture.index.get(slot.key)
        if record is None or needs_split(record):
            continue
        future = slot.start > picture.now
        if not eligible_at(slot.key, picture.index, slot.start if future else picture.now, forecast=future):
            continue
        action = record.data.get("next_action") or {}
        slots.append(replace(slot, title=record.title,
                             first_step=action.get("text") if not action.get("done") else None))
    return tuple(sorted(slots, key=lambda entry: (entry.start, entry.key)))


def _schedule_time(at: datetime, now: datetime) -> str:
    local = at.astimezone(SINGAPORE)
    clock = _slot_clock(at)
    if local.date() == now.astimezone(SINGAPORE).date():
        return f"at {clock}"
    return f"on {local.strftime('%a %d %b')} at {clock}"


def _schedule_note(inputs: OrientationInputs, picture: _Picture) -> str | None:
    if not inputs.schedule_mode:
        return None
    if inputs.plan_slots is None:
        return "Couldn't see the schedule"
    following = next((slot for slot in _schedule_slots(inputs, picture) if slot.start > picture.now), None)
    if following is not None:
        return f"Next scheduled: {following.title} {_schedule_time(following.start, picture.now)}."
    return None


def _passed_slots(picture: _Picture, slots: Sequence[PlannedSlot]) -> tuple[MissedStart, ...]:
    """Step I §5a: a task whose last slot today has ended while it's still open is asked about like
    a passed start time, and never picked until Nic answers. "Move" hands it back to the engine
    (a `day_change` with ``move``); it is never "missed", since only dates can be missed."""

    last: dict[str, PlannedSlot] = {}
    for slot in slots:
        if slot.key not in last or slot.end > last[slot.key].end:
            last[slot.key] = slot
    order = {candidate.ref: index for index, candidate in enumerate(picture.ranked)}
    passed = [
        (entry, last[_slot_key(entry.record)])
        for entry in picture.live
        if entry.record.state == "open" and _slot_key(entry.record) in last
        and last[_slot_key(entry.record)].end <= picture.now and not last[_slot_key(entry.record)].fill
    ]
    passed.sort(key=lambda pair: (order.get(work.first_id(pair[0].record), len(order)), work.first_id(pair[0].record)))
    return tuple(
        MissedStart(
            record_id=work.first_id(entry.record),
            title=entry.record.title,
            question=(
                f'"{entry.record.title}" was planned for {_slot_clock(slot.start)}\u2013{_slot_clock(slot.end)}. '
                f"{MISSED_START_QUESTION}"
            ),
            fields_used={
                "planned": {"start": slot.start.isoformat(), "end": slot.end.isoformat()},
                # Exactly the signal tool's arguments: detail is a JSON string.
                "move": {"kind": "day_change", "detail": json.dumps({"move": work.first_id(entry.record)})},
            },
        )
        for entry, slot in passed
    )


def _missed_starts(picture: _Picture) -> tuple[MissedStart, ...]:
    """Nic's items whose start time has passed, most important first (§3), each with the review's
    question about what happened (Accepted - Nic, 28 Sep 2026)."""

    over = []
    for entry in picture.live:
        at = _at(entry.record, picture.index)
        due = _due(entry.record, picture.index)
        if at and due and datetime.combine(due, time.fromisoformat(at), tzinfo=SINGAPORE) + timedelta(minutes=30) <= picture.now:
            over.append(MissedStart(record_id=work.first_id(entry.record), title=entry.record.title,
                                   question=f'"{entry.record.title}" was {_start_words(entry.record, work._day(picture.now), picture.index)}. {MISSED_START_QUESTION}',
                                   fields_used={"due": due.isoformat(), "at": at}))
    return tuple(over)


def _moment_fields(situation: Moment, now: datetime) -> dict[str, Any]:
    fields: dict[str, Any] = {"kind": situation.kind, "source": situation.source}
    if situation.block is not None:
        fields.update(block=situation.block.title, block_ends=situation.block.end.isoformat())
    if situation.minutes is not None:
        fields["minutes"] = situation.minutes
    if situation.words is not None:
        fields["words"] = situation.words
    return fields


def _fit_moment(
    ranked: tuple[RankedCandidate, ...], situation: Moment, blocks: tuple[Block, ...], now: datetime, today: date,
    index: work.WorkIndex,
) -> tuple[tuple[RankedCandidate, ...], tuple[tuple[RankedCandidate, str], ...]]:
    """PRD §4.3: tasks of the moment's kind; one of another kind with a deadline waits for its own
    kind of block if one comes before the deadline, and otherwise comes up now, saying why. What
    waits comes back with the reason it waits. Outside any block (no kind) every task fits."""

    if situation.kind is None:
        return ranked, ()
    kept: list[RankedCandidate] = []
    held: list[tuple[RankedCandidate, str]] = []
    for candidate in ranked:
        record = index[work.first_id(candidate.item.record)] if candidate.item is not None else None
        kind = task_kind(record.data.get("block") if record is not None else None, project_task=record is None)
        if kind == situation.kind:
            kept.append(candidate)
            continue
        words = BLOCK_WORDS[kind]
        deadline = due_moment(record, today, index) if record is not None else None
        # Only today's calendar is read, so a deadline after today waits for its kind of block,
        # which Nic's weekly pattern brings; the wrong-block rule weighs today's deadlines.
        if deadline is None or deadline[0].astimezone(SINGAPORE).date() > today:
            article = "an" if words[0] in "aeiou" else "a"
            held.append((candidate, f"{words} work; waits for {article} {words} block"))
            continue
        coming = next_block(blocks, kind, now, deadline[0])
        if coming is not None:
            clock = _clock_word(coming.start.astimezone(SINGAPORE).strftime("%H:%M"))
            held.append((candidate, f"waits for the {clock} {words} block"))
            continue
        reason = f"{candidate.rules[0] if candidate.rules else 'due'}, and there's no {words} block before then"
        kept.append(RankedCandidate(
            item=candidate.item, rules=(reason, *candidate.rules[1:]),
            fields_used={**candidate.fields_used, "wrong_block": kind}, started=candidate.started,
            task=candidate.task, points=candidate.points,
        ))
    return tuple(kept), tuple(held)


def what_next(
    inputs: OrientationInputs,
    now: datetime,
    signal: OrientationSignal | None = None,
    override: Override | None = None,
) -> WhatNext:
    """"What next?", only when Nic asks (§5): the top pick, two alternatives and up to 3 that can
    wait, or ``not_enough`` with its plain reason and any questions (§3); with either, ``missed``
    asks about anything whose start time has passed. No model call: this only reads what was
    actually saved. ``now`` is timezone-aware; dates run in Asia/Singapore."""

    picture = _picture(inputs, now, signal)
    review = inputs.review
    unknowns = _unknowns(review.source_status)
    named = _named_in_progress(picture, now)
    # A start time that has passed is over: it is asked about and never picked, offered or left
    # to wait (Accepted - Nic, 28 Sep 2026). Everything else ranks as before.
    over = _missed_starts(picture)
    if inputs.plan_slots is not None and not inputs.schedule_mode:
        started_over = {entry.record_id for entry in over}
        over += tuple(entry for entry in _passed_slots(picture, inputs.plan_slots) if entry.record_id not in started_over)
    over_ids = {entry.record_id for entry in over}
    ranked = _summary_candidates(picture.ranked, picture.index, now, over_ids)
    scheduled_now = None
    if inputs.schedule_mode:
        from brain_core.schedule import eligible_at
        ranked = tuple(candidate for candidate in ranked if candidate.item is None
            or not picture.index[candidate.ref].data.get("parent")
            or eligible_at(candidate.ref, picture.index, now))
        scheduled_now = _current_slot(_schedule_slots(inputs, picture), now, _open_keys(picture))
        if scheduled_now is not None:
            # The forecast controls today's order; only an actually actionable task can lead.
            preferred = next((candidate for candidate in picture.ranked
                              if candidate.ref == scheduled_now.key and candidate.ref not in over_ids), None)
            if preferred is not None:
                owner = _summary_owner(picture.index[preferred.ref], picture.index)
                ranked = (preferred, *(candidate for candidate in ranked
                    if candidate.ref != preferred.ref and (owner is None or candidate.item is None
                        or _summary_owner(picture.index[candidate.ref], picture.index) != owner)))
    # Time blocks (PRD §4.3), only while on: what fits the moment, and what waits for its block.
    situation: Moment | None = None
    held: tuple[tuple[RankedCandidate, str], ...] = ()
    if inputs.blocks and not inputs.schedule_mode:
        blocks = blocks_from_calendar(review.calendar_events)
        situation = moment(blocks, now, override)
        # Outside any block nothing is assumed and nothing is asked: every task ranks together
        # (Nic, 29 Sep 2026).
        ranked, held = _fit_moment(ranked, situation, blocks, now, picture.today, picture.index)
    if (override is not None and override.mode == "minutes" and override.minutes is not None
            and (situation is None or situation.kind is None)):
        index = picture.index
        ranked = tuple(candidate for candidate in ranked if candidate.item is not None
                       and 0 < work.length_of(candidate.item.record, index) <= override.minutes)
    moment_fields = _moment_fields(situation, now) if situation is not None else None

    def not_enough(reason: str, questions: tuple[str, ...] = ()) -> WhatNext:
        return WhatNext(
            top_pick=None, alternatives=(), can_wait=(), in_progress=named, assumptions=(), unknowns=unknowns,
            not_enough=NotEnough(reason=reason, questions=questions), missed=over, moment=moment_fields,
            plan_note=_schedule_note(inputs, picture),
        )

    # Every rule ranks Brain Home items, so an unreadable Brain Home means the candidates can't
    # be ranked at all: say so rather than "nothing open", which would be a guess (§3).
    if review.source_status.get("brain") not in (None, "", "ok"):
        return not_enough("Couldn't see your saved items, so there's nothing safe to recommend yet.")
    if not ranked:
        if over:
            if any("planned" in entry.fields_used for entry in over):
                return not_enough("Nothing to start right now: everything open was planned or set for a time that has passed.")
            return not_enough("Nothing to start right now: everything open was set for a time that has passed.")
        if held:
            return not_enough(f"Nothing {BLOCK_WORDS[situation.kind]} to do right now; the rest waits for its own block.")
        index = picture.index
        waiting = [(entry.record, work.explain(entry.record, index, now)) for entry in picture.live
                   if work.status_of(entry.record, index, now) == "waiting"]
        if waiting:
            questions = tuple(_waiting_question(record) for record, why in sorted(waiting, key=lambda pair: (pair[0].created_at, pair[0].id))
                              if index[work.first_id(record)].data.get("needs") or record.state == "waiting")
            reason = "Everything open right now is waiting on someone or blocked on something."
            if all(index[work.first_id(record)].data.get("needs") or record.state == "waiting" for record, why in waiting) and not any(task.state == "blocked" for task in _focus_tasks(inputs.project_tasks, focus_project_ids(inputs.preferences))):
                reason = "Everything open right now is waiting on someone else."
            elif not questions:
                reason = "Everything open right now is blocked on something else."
            return not_enough(reason, questions)
        if any(task.state == "blocked" for task in _focus_tasks(inputs.project_tasks, focus_project_ids(inputs.preferences))):
            return not_enough("Everything open right now is blocked on something else.")
        return not_enough("Nothing accepted and open right now.")

    # Only an open item can be started now; waiting and blocked items can wait (§3).
    choices = list(ranked)  # rank already filters through the shared reader's up_things.
    if not choices:
        waiting = sorted(
            (
                entry.record for entry in picture.live
                if entry.record.state == "waiting" and work.first_id(entry.record) not in over_ids
            ),
            key=lambda record: (record.created_at, record.id),
        )
        if not waiting:
            return not_enough("Everything open right now is blocked on something else.")
        reason = (
            "Everything open right now is waiting on someone else."
            if len(waiting) == len(ranked)
            else "Everything open right now is waiting on someone or blocked on something."
        )
        return not_enough(reason, tuple(_waiting_question(record) for record in waiting))

    top, alternatives = choices[0], choices[1 : 1 + MAX_ALTERNATIVES]
    # The normal three suggestions do not turn today's other selections into can-wait work.
    alternatives += [entry for entry in choices[1 + MAX_ALTERNATIVES:]
                     if entry.item is not None and _picked(entry.item.record, inputs.picked_today,
                                                           work._day(picture.now), picture.index)]
    chosen = {top.ref, *(entry.ref for entry in alternatives)}
    can_wait = tuple(
        CanWaitItem(
            record_id=entry.ref,
            title=entry.title,
            reason=_candidate_can_wait_reason(entry, picture.today, picture.index, work._day(picture.now)),
            task=entry.task,
        )
        for entry in ranked
        if entry.ref not in chosen
    )
    can_wait = (can_wait + tuple(
        CanWaitItem(record_id=entry.ref, title=entry.title, reason=reason, task=entry.task) for entry, reason in held
    ) + tuple(
        CanWaitItem(record_id=project_task_ref(task), title=task.intent,
                    reason=f"blocked: {task.blockers[0]}" if task.blockers else "blocked on something else", task=task)
        for task in _focus_tasks(inputs.project_tasks, focus_project_ids(inputs.preferences))
        if task.state == "blocked"
    ))[:MAX_CAN_WAIT_ITEMS]

    stall_history = tuple(row for row in review.history if today_in_singapore(row.surfaced_at) < picture.today)
    record: Record | None = None
    if top.item is not None:
        saved = top.item.record
        index = picture.index
        record = index[work.first_id(saved)]
        estimate = _padded_minutes(saved)
        action = record.data.get("next_action") or {}
        marked = any(mark.get("mark") == "agents_wording" and mark.get("part") == "next_action" for mark in record.data.get("review_marks", ()))
        first_step = action.get("text", "") if not action.get("done") and not marked else ""
        stalls = tends_to_stall(saved, stall_history)
        assumptions = _assumptions(saved)
    else:
        # A project task (N4): its next step is the one its sitrep carries, and it tends to stall
        # when it was picked on 2 earlier days with its note unchanged since. No estimate.
        estimate, assumptions = None, ()
        first_step = top.task.next_step.strip()
        stalls = surfaced_unchanged(top.ref, top.task.note_content_sha256, stall_history)
    fields_used = dict(top.fields_used)
    gap = signal.gap_minutes if signal is not None and signal.kind == "calendar_gap" else None
    if override is not None and override.mode == "minutes":
        gap = override.minutes
    if situation is not None and situation.minutes is not None:
        gap = situation.minutes  # "I've got 20 minutes" (PRD §4.3)
    if gap is not None:
        fields_used["gap_minutes"] = gap
        if estimate is not None:
            fields_used["fits_gap"] = estimate <= gap
    # The plan's slot now (step I §5a): its task leads with "planned for now" when it's the pick;
    # otherwise the answer says the plan had something else. The ranking itself never changes.
    rules, plan_note = top.rules, _schedule_note(inputs, picture)
    if inputs.schedule_mode and record is not None:
        from brain_core.schedule import SPLIT_TASK_NOTICE, split_notice_required
        if split_notice_required(record, picture.index):
            fields_used["schedule_issue"] = "needs_split"
            plan_note = SPLIT_TASK_NOTICE + (". " + plan_note if plan_note else "")
    slot_now = (scheduled_now if inputs.schedule_mode else
                _current_slot(inputs.plan_slots, now, _open_keys(picture)) if inputs.plan_slots is not None else None)
    if slot_now is not None:
        if top.item is not None and _slot_key(top.item.record) == slot_now.key:
            rules = (SCHEDULED_FOR_NOW if inputs.schedule_mode else PLANNED_FOR_NOW, *top.rules)
            fields_used["scheduled" if inputs.schedule_mode else "planned"] = {
                "start": slot_now.start.isoformat(), "end": slot_now.end.isoformat()}
        elif not inputs.schedule_mode:
            why = f": {top.rules[0]}" if top.rules else ""
            plan_note = (
                f'The plan says "{slot_now.title}" until {_slot_clock(slot_now.end)}, but "{top.title}" comes first{why}.'
            )
    top_pick = TopPick(
        record_id=top.ref,
        title=top.title,
        plain_word=_plain_word(top),
        rules=rules,
        fields_used=fields_used,
        first_step=first_step,
        estimate_minutes=estimate,
        time_box_minutes=(min(override.minutes, estimate) if estimate is not None else override.minutes)
        if override is not None and override.mode == "minutes" and override.minutes is not None
        else time_box(top.item.record if top.item is not None else None, stalls),
        task=top.task,
        belongs_to=(next((entry.record.title for entry in picture.items if work.first_id(entry.record) == record.data.get("parent")), None) if record is not None else top.task.project),
        progress=(work.progress_of(index.get(record.data.get("parent"), record), index) if record is not None else None),
    )
    return WhatNext(
        top_pick=top_pick,
        alternatives=tuple(
            Alternative(
                record_id=entry.ref,
                title=entry.title,
                plain_word=_plain_word(entry),
                rules=entry.rules,
                fields_used=dict(entry.fields_used),
                task=entry.task,
                belongs_to=(picture.index[picture.index[entry.ref].data["parent"]].title
                            if entry.item is not None and picture.index[entry.ref].data.get("parent") in picture.index else None),
                progress=(work.progress_of(picture.index[picture.index[entry.ref].data["parent"]], picture.index)
                          if entry.item is not None and picture.index[entry.ref].data.get("parent") in picture.index else None),
            )
            for entry in alternatives
        ),
        can_wait=can_wait,
        in_progress=named,
        assumptions=assumptions,
        unknowns=unknowns,
        not_enough=None,
        missed=over,
        moment=moment_fields,
        plan_note=plan_note,
    )


# --- "Where am I?" (N1, §4) ---------------------------------------------------------------------


def _rule(section: str) -> str:
    return OVERVIEW_RULE_PREFIX + section


def _item_line(
    section: str, entry: ItemRecord, text: str, *, ranked: Mapping[str, RankedCandidate], **fields: Any
) -> OverviewLine:
    candidate = ranked.get(work.first_id(entry.record))
    reasons = candidate.rules if candidate is not None else ()
    fields_used: dict[str, Any] = dict(candidate.fields_used) if candidate is not None else {}
    fields_used.update(fields)
    if reasons:
        fields_used["rules"] = list(reasons)
    return OverviewLine(
        section=section,
        text=text,
        item_ref=work.first_id(entry.record),
        rule=_rule(section),
        reasons=reasons,
        record_sha256=entry.sha256,
        record_path=entry.path,
        record_state=entry.record.state,
        fields_used=fields_used,
    )


def _in_rank_order(entries: Iterable[ItemRecord], order: Mapping[str, int]) -> list[ItemRecord]:
    return sorted(entries, key=lambda entry: order[work.first_id(entry.record)])


def _now_lines(inputs: OrientationInputs, picture: _Picture) -> list[OverviewLine]:
    """The block Nic is in and when it ends, and the next one today (PRD §4.5; Proposed - Nic to
    confirm). Only while time blocks are on; nothing is recommended."""

    if inputs.schedule_mode:
        if inputs.plan_slots is None:
            text, fields = "Couldn't see the schedule", {"available": False}
        else:
            slots = _schedule_slots(inputs, picture)
            current = next((slot for slot in slots if slot.start <= picture.now < slot.end), None)
            following = next((slot for slot in slots if slot.start > picture.now), None)
            text = (f"Scheduled now: {current.title} until {_slot_clock(current.end)}"
                    if current is not None else "Nothing scheduled now")
            if following is not None:
                text += f"; next: {following.title} {_schedule_time(following.start, picture.now)}"
            fields = {"available": True, "slot": current.key if current else None,
                      "next": following.key if following else None}
        return [OverviewLine(section=SECTION_NOW, text=text, item_ref="__now__",
                             rule=_rule(SECTION_NOW), fields_used=fields)]
    slot = _current_slot(inputs.plan_slots, picture.now, _open_keys(picture)) if inputs.plan_slots is not None else None
    if not inputs.blocks and slot is None:
        return []

    def clock(moment_: datetime) -> str:
        return _clock_word(moment_.astimezone(SINGAPORE).strftime("%H:%M"))

    fields: dict[str, Any] = {}
    if inputs.blocks:
        blocks = blocks_from_calendar(inputs.review.calendar_events)
        current = next((block for block in blocks if block.start <= picture.now < block.end), None)
        upcoming = next((block for block in blocks if block.start > picture.now), None)
        if current is not None:
            text = f"In your {BLOCK_WORDS[current.kind]} block until {clock(current.end)}"
        else:
            text = "No block now"
        if upcoming is not None:
            text += f"; next: {BLOCK_WORDS[upcoming.kind]} at {clock(upcoming.start)}"
        fields = {
            "block": current.title if current is not None else None,
            "kind": current.kind if current is not None else None,
            "next": upcoming.title if upcoming is not None else None,
        }
    if slot is not None:
        # Step I §5a: the task the plan has now, by its first step.
        planned = f"\u25b8 {slot.first_step or slot.title} until {clock(slot.end)}"
        text = f"{text} \u00b7 {planned}" if inputs.blocks else f"Planned now: {planned}"
        fields["slot"] = slot.key
    return [OverviewLine(
        section=SECTION_NOW, text=text, item_ref="__now__", rule=_rule(SECTION_NOW), fields_used=fields,
    )]


def _highest_lines(picture: _Picture) -> list[OverviewLine]:
    """"Highest right now" (PRD §4.5): the three highest scores among what Nic could start now,
    across every section, each with its reasons, biggest first. Waiting and blocked items can't
    be started, and a start time that has passed is asked about, so neither is here (Proposed -
    Nic to confirm). Each also shows in its own section. Nothing is recommended: no first step,
    no time box, nothing to say yes to (N1)."""

    over = {work.first_id(entry.record) for entry in picture.live
            if _start_time(entry.record, picture.index) is not None and _missed(entry.record, picture.now, picture.index)}
    top = _summary_candidates(picture.ranked, picture.index, picture.now, over)
    lines: list[OverviewLine] = []
    for place, candidate in enumerate(top[:HIGHEST_RIGHT_NOW_COUNT], start=1):
        fields_used = {**candidate.fields_used, "rules": list(candidate.rules), "place": place}
        title = candidate.title
        if candidate.item is not None:
            parent = picture.index.get(picture.index[candidate.ref].data.get("parent"))
            if parent is not None:
                title += f" ({parent.title})"
        text = f"{title} — {'; '.join(candidate.rules)}"
        if candidate.item is not None:
            entry = candidate.item
            sha256, path, state = entry.sha256, entry.path, entry.record.state
        else:
            sha256, path, state = candidate.task.note_content_sha256, None, candidate.task.state
        lines.append(OverviewLine(
            section=SECTION_HIGHEST, text=text, item_ref=candidate.ref, rule=_rule(SECTION_HIGHEST),
            reasons=candidate.rules, record_sha256=sha256, record_path=path, record_state=state,
            fields_used=fields_used,
        ))
    return lines


def _missed_lines(picture: _Picture, ranked: Mapping[str, RankedCandidate], order: Mapping[str, int]) -> list[OverviewLine]:
    """Accepted items whose date or window passed while open, until Nic answers (N9). The review's
    own words for how long (PRD §4.6), and nothing recommended (N1); one whose start time passed
    carries the review's question about what happened (Accepted - Nic, 28 Sep 2026)."""

    lines: list[OverviewLine] = []
    index = picture.index
    for entry in _in_rank_order((entry for entry in picture.live if _missed(entry.record, picture.now, index)), order):
        due = _due(entry.record, index)
        clause = ("was " + _start_words(entry.record, work._day(picture.now), index)) if _at(entry.record, index) else (
            f"missed since {_day_word(due, work._day(picture.now))} ({(picture.today - due).days} days)")
        if _start_time(entry.record, index) is not None:
            clause += f". {MISSED_START_QUESTION}"
        lines.append(_item_line(SECTION_MISSED, entry, f"{entry.record.title} — {clause}", ranked=ranked))
    return lines


def _today_lines(
    inputs: OrientationInputs, picture: _Picture, ranked: Mapping[str, RankedCandidate], order: Mapping[str, int]
) -> list[OverviewLine]:
    """Today's calendar events in time order, then what's due today, most important first: items
    due today or tonight and routines whose day it is (§3's rule 7, "a routine due today"). One
    whose start time has passed is under Missed instead."""

    lines: list[OverviewLine] = []
    for event in sorted(inputs.review.calendar_events, key=lambda event: event.start):
        when = ", all day" if event.all_day else f" at {_clock_word(event.start.astimezone(SINGAPORE).strftime('%H:%M'))}"
        lines.append(OverviewLine(
            section=SECTION_TODAY,
            text=f"{event.title} — on your calendar{when}",
            item_ref=f"calendar:{event.title}:{event.start.isoformat()}",
            rule=_rule(SECTION_TODAY),
            fields_used={"title": event.title, "start": event.start.isoformat(), "all_day": event.all_day},
        ))
    weekday = picture.today.weekday()
    index = picture.index
    due_today = (
        entry for entry in picture.live
        if not _missed(entry.record, picture.now, index) and (
            _due(entry.record, index) == picture.today or _due_tonight(entry.record, picture.today, index)
            or (entry.record.data.get("repeat") and (due := due_moment(entry.record, picture.today, index)) is not None
                and due[0].date() == picture.today)
        )
    )
    for entry in _in_rank_order(due_today, order):
        at = _at(entry.record, index)
        if _due_tonight(entry.record, picture.today, index):
            clause = f"due tonight at {_clock_word(at)}"
        elif _due(entry.record, index) != picture.today:
            clause = "every day, due today" if entry.record.data.get("repeat") == "daily" else f"every {_WEEKDAY_FULL[weekday]}, due today"
        elif at:
            clause = f"due today at {_clock_word(at)}"
        else:
            clause = "due today"
        lines.append(_item_line(SECTION_TODAY, entry, f"{entry.record.title} — {clause}", ranked=ranked))
    return lines


def _picked_lines(
    inputs: OrientationInputs, picture: _Picture, ranked: Mapping[str, RankedCandidate], order: Mapping[str, int]
) -> list[OverviewLine]:
    picked = (entry for entry in picture.live
              if _picked(entry.record, inputs.picked_today, work._day(picture.now), picture.index))
    return [
        _item_line(SECTION_PICKED_FOR_TODAY, entry, f"{entry.record.title} — you picked it for today", ranked=ranked)
        for entry in _in_rank_order(picked, order)
    ]


def _in_progress_lines(picture: _Picture, now: datetime, ranked: Mapping[str, RankedCandidate]) -> list[OverviewLine]:
    """Each started task's next step and when its note last changed; stuck ones marked (N10b).
    No "how far along" figure: notes record what's been done, not what's left (N10c)."""

    by_id = {work.first_id(entry.record): entry for entry in picture.live}
    lines: list[OverviewLine] = []
    for progress in _ranked_in_progress(picture, now):
        step = f"next: {progress.next_step}" if progress.next_step else "no first step yet"
        changed = _day_word(work._day(progress.last_changed), work._day(picture.now))
        text = f"{progress.title} — {step}; last changed {changed}" + ("; stuck" if progress.stuck else "")
        lines.append(_item_line(
            SECTION_IN_PROGRESS, by_id[progress.record_id], text, ranked=ranked,
            session_id=progress.session_id, next_step=progress.next_step,
            note_updated_at=progress.last_changed.isoformat(), stuck=progress.stuck,
        ))
    return lines


def _windows_closing_lines(
    picture: _Picture, ranked: Mapping[str, RankedCandidate], order: Mapping[str, int]
) -> list[OverviewLine]:
    deadline = picture.today + timedelta(days=WINDOW_CLOSING_DAYS)
    closing = (
        entry for entry in picture.live
        if (end := _window_end(entry.record)) is not None and picture.today <= end <= deadline
    )
    return [
        _item_line(
            SECTION_WINDOWS_CLOSING, entry,
            f"{entry.record.title} — window closes {_day_word(_window_end(entry.record), work._day(picture.now))}",
            ranked=ranked,
        )
        for entry in _in_rank_order(closing, order)
    ]


def _as_of_words(as_of: datetime, today: date) -> str:
    """``"today at 1:30 pm"`` or ``"Fri 2 Oct at 9 am"``, Singapore time."""

    local = as_of.astimezone(SINGAPORE)
    return f"{_day_word(work._day(as_of), today)} at {_clock_word(local.strftime('%H:%M'))}"


def _project_line(entry: str, summary: ProjectTasks | None, listed: int, today: date) -> OverviewLine:
    """One focus project, as Nic named it, and how fresh its summary is: "as of" is when the hub
    last wrote any of it (sitreps S5), so it says how old everything under it may be."""

    if summary is None or summary.as_of is None:
        text, as_of = f"{entry} — no summary yet", None
    else:
        as_of = summary.as_of.isoformat()
        text = f"{entry} — {'' if listed else 'nothing open; '}summary as of {_as_of_words(summary.as_of, today)}"
    project = summary.project if summary is not None else None
    return OverviewLine(
        section=SECTION_FOCUS_PROJECTS,
        text=text,
        item_ref=PROJECT_REF_PREFIX + (project or entry),
        rule=_rule(SECTION_FOCUS_PROJECTS),
        fields_used={"focus_project": entry, "project": project, "as_of": as_of, "tasks": listed},
    )


def _project_task_line(task: TaskSitrep, ranked: Mapping[str, RankedCandidate], today: date) -> OverviewLine:
    """One project task from its sitrep: its next step, or its first blocker, and when its note
    last changed, like an "In progress" line."""

    ref = project_task_ref(task)
    candidate = ranked[ref]
    if task.state == "blocked":
        clause = f"blocked: {task.blockers[0]}" if task.blockers else "blocked"
    elif task.next_step.strip():
        clause = f"next: {task.next_step.strip()}"
    else:
        clause = "no first step yet"
    changed = _day_word(work._day(task.note_updated_at), today)
    return OverviewLine(
        section=SECTION_FOCUS_PROJECTS,
        text=f"{task.intent} — {clause}; last changed {changed}",
        item_ref=ref,
        rule=_rule(SECTION_FOCUS_PROJECTS),
        reasons=candidate.rules,
        record_sha256=task.note_content_sha256,
        record_state=task.state,
        fields_used={
            **candidate.fields_used,
            "rules": list(candidate.rules),
            "next_step": task.next_step,
            "blockers": list(task.blockers),
            "note_updated_at": task.note_updated_at.isoformat(),
        },
    )


def _focus_project_lines(
    inputs: OrientationInputs, picture: _Picture, ranked: Mapping[str, RankedCandidate], order: Mapping[str, int]
) -> list[OverviewLine]:
    """Nic's focus projects in the order he named them (N4): each one's line with how fresh its
    summary is, then its open and blocked tasks from the task sitreps, ranked like any section
    (§3), at most 10 and then how many more. A focus project the hub has no summary of says "no
    summary yet". With sitreps off, or unreadable, there is nothing to show and the section is
    left out, exactly as before (Proposed - Nic to confirm). Nothing here is recommended (N1)."""

    if inputs.project_tasks is None:
        return []
    lines: list[OverviewLine] = []
    seen: set[str] = set()
    for entry in focus_projects(inputs.preferences):
        key = entry.strip().casefold()
        if key in seen:
            continue  # the same project again, in another case
        seen.add(key)
        summary = next((found for found in inputs.project_tasks if same_project(entry, found.project)), None)
        tasks = sorted(_listed_tasks(summary), key=lambda task: task.note_updated_at) if summary is not None else []
        lines.append(_project_line(entry, summary, len(tasks), work._day(picture.now)))
        lines += [_project_task_line(task, ranked, work._day(picture.now)) for task in tasks[:FOCUS_PROJECT_TASK_CAP]]
        overflow = len(tasks) - FOCUS_PROJECT_TASK_CAP
        if overflow > 0:
            lines.append(OverviewLine(
                section=SECTION_FOCUS_PROJECTS,
                text=f"{overflow} more; ask to see them",
                item_ref=f"{PROJECT_REF_PREFIX}{summary.project}:{_MORE_TASKS_REF}",
                rule=_rule(SECTION_FOCUS_PROJECTS),
                fields_used={"project": summary.project, "remaining": overflow},
            ))
    return lines


def _request_line(request: OpenRequest, handoff_notes: Mapping[str, OpenNote], today: date) -> OverviewLine:
    """A request handed to an agent, named as the 6am review names it (§3). While it is still
    being worked on, the hub's own note for it adds its next step (rebuild plan G2)."""

    title = request_title(request.id, request.objective)
    with_whom = f"with {request.handed_to} since {_day_word(request.since, today)}"
    if request.state == REQUEST_RETURNED_UNCHECKED:
        text = f"{title} — returned, not yet checked ({with_whom})"
    else:
        text = f"{title} — {with_whom}"
    fields_used: dict[str, Any] = {
        "handed_to": request.handed_to,
        "since": request.since.isoformat(),
        "objective": request.objective,
        "work_state": request.work_state,
        "check": request.check,
    }
    note = handoff_notes.get(request.id)
    if note is not None and request.state == REQUEST_OPEN and note.next_step.strip():
        text += f"; next: {note.next_step.strip()}"
        fields_used.update(session_id=note.session_id, next_step=note.next_step.strip())
    return OverviewLine(
        section=SECTION_WAITING_ON,
        text=text,
        item_ref=request.id,
        rule=_rule(SECTION_WAITING_ON),
        record_state=request.state,
        fields_used=fields_used,
    )


def _waiting_on_lines(
    inputs: OrientationInputs, picture: _Picture, ranked: Mapping[str, RankedCandidate], order: Mapping[str, int]
) -> list[OverviewLine]:
    """Items waiting on someone or blocked, with what they wait on, most important first; then
    requests handed to agents that are still open, newest first, at most 10 as in the review."""

    lines: list[OverviewLine] = []
    index = picture.index
    held = (entry for entry in picture.live if work.status_of(entry.record, index, picture.now) == "waiting")
    for entry in _in_rank_order(held, order):
        why = work.explain(entry.record, index, picture.now)
        clause = why["reason"]
        next_record = index.get(work.next_of(entry.record, index, picture.now)["item_ref"], entry.record)
        if why["until"] and not (work.event_day(work.dates_of(entry.record, index))
                                  or work.event_day(work.dates_of(next_record, index))):
            clause += "; waiting until " + _day_word(date.fromisoformat(why["until"]), work._day(picture.now))
        lines.append(_item_line(SECTION_WAITING_ON, entry, f"{entry.record.title} — {clause}", ranked=ranked,
                                why=dict(why)))
    handoff_notes: dict[str, OpenNote] = {}
    for note in sorted((note for note in picture.notes if is_handoff_note(note)), key=lambda note: note.updated_at):
        handoff_notes[note.session_id.removeprefix("gt-")] = note  # the most recent wins
    requests = sorted(inputs.review.open_requests, key=lambda request: request.since, reverse=True)
    lines += [_request_line(request, handoff_notes, work._day(picture.now)) for request in requests[:WAITING_ON_REQUEST_CAP]]
    overflow = len(requests) - WAITING_ON_REQUEST_CAP
    if overflow > 0:
        lines.append(OverviewLine(
            section=SECTION_WAITING_ON,
            text=f"{overflow} more; ask to see them",
            item_ref=MORE_REQUESTS_ITEM_REF,
            rule=_rule(SECTION_WAITING_ON),
            fields_used={"remaining": overflow},
        ))
    return lines


def _routine_next_day(item: Record, today: date) -> date | None:
    """A weekly routine with no date of its own: its next day after today (N16). Today itself is
    under Today already, unless today's is done; then it is next week's (PRD §4.7)."""

    timing = _timing(item)
    weekday = _repeat_weekday(item)
    if timing.get("due") or timing.get("window") or weekday is None:
        return None
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


def _coming_up_lines(
    picture: _Picture, leftovers: Sequence[ItemRecord], ranked: Mapping[str, RankedCandidate],
    order: Mapping[str, int],
) -> list[OverviewLine]:
    """Open tasks no other section shows whose date is after today (N16): due in the next
    `COMING_UP_DAYS` days, a window closing after "Windows closing", or a weekly routine's next
    day. Most important first (§3; Accepted - Nic, 28 Sep 2026), at most `COMING_UP_CAP`, then
    how many more; then how many come up later, as the 6am review's own "Coming up" does."""

    today = picture.today
    horizon = today + timedelta(days=COMING_UP_DAYS)
    soon: list[tuple[ItemRecord, date]] = []
    later = 0
    for entry in leftovers:
        due = due_moment(entry.record, today, picture.index)
        day = due[0].date() if due is not None and due[0].date() > today else None
        if day is None:
            continue
        soon.append((entry, day))
    soon.sort(key=lambda pair: order[work.first_id(pair[0].record)])
    lines: list[OverviewLine] = []
    for entry, day in soon:
        timing = _timing(entry.record)
        if _due(entry.record, picture.index) is not None:
            at = _at(entry.record, picture.index)
            clause = f"due {_day_word(day, work._day(picture.now))}" + (f" at {_clock_word(at)}" if at else "")
        elif timing.get("window"):
            clause = f"window closes {_day_word(day, work._day(picture.now))}"
        else:
            clause = f"every {_WEEKDAY_FULL[day.weekday()]}; next {_day_word(day, work._day(picture.now))}"
        lines.append(_item_line(
            SECTION_COMING_UP, entry, f"{entry.record.title} — {clause}", ranked=ranked, coming_up=day.isoformat(),
        ))
    return lines


def _parked_until(item: Record) -> date | None:
    raw = item.data.get("parked_until")
    return date.fromisoformat(raw) if isinstance(raw, str) else None


def _no_date_yet_lines(
    picture: _Picture, leftovers: Sequence[ItemRecord], ranked: Mapping[str, RankedCandidate],
    order: Mapping[str, int],
) -> list[OverviewLine]:
    """Open tasks no other section shows that have no date (N17), most important first (§3); then
    parked tasks whose "parked until" day has come, oldest first. This is the 6am review's "Pick
    or park" list, except that weekly routines show under "Coming up". At most
    `PICK_OR_PARK_CAP`, then how many more (Proposed - Nic to confirm, as Pick or park's)."""

    today = picture.today
    undated = sorted(
        (
            entry for entry in leftovers
            if due_moment(entry.record, today, picture.index) is None
        ),
        key=lambda entry: order[work.first_id(entry.record)],
    )
    back = sorted(
        (
            entry for entry in picture.items
            if _accepted(entry.record) and entry.record.state == "parked"
            and (until := _parked_until(entry.record)) is not None and until <= today
        ),
        key=lambda entry: (entry.record.created_at, entry.record.id),
    )
    lines: list[OverviewLine] = []
    for entry in (*undated, *back):
        record = entry.record
        until = _parked_until(record) if record.state == "parked" else None
        if until is not None:
            clause = f"parked until {_day_word(until, work._day(picture.now))}"
        else:
            clause = f"last changed {_day_word(work._day(record.updated_at), work._day(picture.now))}"
        lines.append(_item_line(SECTION_NO_DATE_YET, entry, f"{record.title} — {clause}", ranked=ranked))
    return lines


def _routine_lines(inputs: OrientationInputs, picture: _Picture, order: Mapping[str, int]) -> list[OverviewLine]:
    """Each routine Nic accepted that is still open, with its streak and completion rate (PRD
    §4.5, §4.7), most important first. Worked out from its own versions each time. Each also shows
    in its day's section; nothing is recommended."""

    everything = tuple(entry.record for entry in inputs.review.items)
    routines = [entry for entry in picture.live if entry.record.state == "open" and is_routine(entry.record)]
    lines: list[OverviewLine] = []
    for entry in _in_rank_order(routines, order):
        stats = routine_stats(entry.record, versions_of(entry.record, everything), picture.today)
        repeat = entry.record.data["repeat"]
        every = "every day" if repeat == "daily" else f"every {_WEEKDAY_FULL[_repeat_weekday(entry.record)]}"
        lines.append(OverviewLine(
            section=SECTION_ROUTINES,
            text=f"{entry.record.title} — {every}; {stats_words(stats)}",
            item_ref=work.first_id(entry.record),
            rule=_rule(SECTION_ROUTINES),
            record_sha256=entry.sha256,
            record_path=entry.path,
            record_state=entry.record.state,
            fields_used={
                "repeat": repeat, "streak": stats.streak, "done": stats.done, "counted": stats.counted,
                "rate": stats.rate, "slipping": stats.slipping, "strong": stats.strong,
            },
        ))
    return lines


def _new_suggestion_lines(picture: _Picture) -> list[OverviewLine]:
    """Suggestions not yet accepted or dropped, oldest first. Never candidates (§3)."""

    suggestions = sorted(
        (
            entry for entry in picture.items
            if _is_v2_item(entry.record) and entry.record.data.get("level") == "suggested"
            and entry.record.state in OPEN_STATES
        ),
        key=lambda entry: (entry.record.created_at, entry.record.id),
    )
    lines: list[OverviewLine] = []
    for entry in suggestions:
        speaker = (entry.record.source or {}).get("speaker") or "an agent"
        lines.append(_item_line(
            SECTION_NEW_SUGGESTIONS, entry, f"{entry.record.title} — suggested by {speaker}", ranked={},
            speaker=speaker,
        ))
    return lines


def _finished_lines(picture: _Picture) -> list[OverviewLine]:
    """Nic's items done and checked in the last 7 days, the most recently finished first (N10c)."""

    finished = sorted(
        (
            entry for entry in picture.items
            if _accepted(entry.record) and entry.record.state == "done"
            and 0 <= (work._day(picture.now) - work.done_day_of(entry.record, picture.index)).days < FINISHED_WINDOW_DAYS
        ),
        key=lambda entry: (work.done_day_of(entry.record, picture.index), entry.record.updated_at, entry.record.id),
        reverse=True,
    )
    lines: list[OverviewLine] = []
    for entry in finished:
        day = _day_word(work.done_day_of(entry.record, picture.index), work._day(picture.now))
        check = entry.record.data.get("check") or {}
        lines.append(_item_line(
            SECTION_FINISHED_THIS_WEEK, entry, f"{entry.record.title} — done {day}", ranked={},
            done_at=entry.record.updated_at.isoformat(), checked_by=check.get("checked_by"),
            done_day=work.done_day_of(entry.record, picture.index).isoformat(),
        ))
    return lines


def _could_not_see_lines(source_status: Mapping[str, str | None]) -> list[OverviewLine]:
    return [
        OverviewLine(
            section=SECTION_COULD_NOT_SEE,
            text=f"{source}: {status}",
            item_ref=source,
            rule=_rule(SECTION_COULD_NOT_SEE),
            fields_used={"source": source, "status": status},
        )
        for source, status in source_status.items()
        if status not in (None, "", "ok")
    ]


def overview(inputs: OrientationInputs, now: datetime) -> Overview:
    """The current picture, with up-only highest scores and one detailed line per thing.

    Parts fold into the task with progress/next; waits show why and until.
    Calendar, sitrep and request formatting, list limits and overflow counts stay.
    """

    picture = _picture(inputs, now)
    ranked = {candidate.ref: candidate for candidate in picture.ranked}
    order = {candidate.ref: index for index, candidate in enumerate(picture.ranked)}
    index = picture.index
    for entry in sorted(picture.live, key=lambda entry: (entry.record.updated_at, entry.record.id)):
        ranked.setdefault(work.first_id(entry.record), RankedCandidate(item=entry, rules=(), fields_used={}))
        order.setdefault(work.first_id(entry.record), len(order))
    for task in _focus_tasks(inputs.project_tasks, focus_project_ids(inputs.preferences)):
        ref = project_task_ref(task)
        focus = _matching_focus(task.project, focus_project_ids(inputs.preferences))
        reasons = ((f"focus project: {focus}",) if focus is not None else ()) + ("already started",)
        ranked.setdefault(ref, RankedCandidate(item=None, rules=reasons,
            fields_used={"project": task.project, "session_id": task.session_id, "focus_project": focus}, task=task))
        order.setdefault(ref, len(order))
    active = replace(picture, live=tuple(entry for entry in picture.live
        if work.status_of(entry.record, index, now) != "waiting"))
    sections: dict[str, list[OverviewLine]] = {
        SECTION_NOW: _now_lines(inputs, picture),
        SECTION_HIGHEST: _highest_lines(picture),
        SECTION_MISSED: _missed_lines(active, ranked, order),
        SECTION_TODAY: _today_lines(inputs, active, ranked, order),
        SECTION_PICKED_FOR_TODAY: _picked_lines(inputs, active, ranked, order),
        SECTION_IN_PROGRESS: _in_progress_lines(active, now, ranked),
        SECTION_WINDOWS_CLOSING: _windows_closing_lines(active, ranked, order),
        SECTION_FOCUS_PROJECTS: _focus_project_lines(inputs, picture, ranked, order),
        SECTION_WAITING_ON: _waiting_on_lines(inputs, picture, ranked, order),
        SECTION_ROUTINES: _routine_lines(inputs, active, order),
        SECTION_NEW_SUGGESTIONS: _new_suggestion_lines(picture),
        SECTION_FINISHED_THIS_WEEK: _finished_lines(picture),
        SECTION_PARKED: [_item_line(SECTION_PARKED, entry,
            f"{entry.record.title} — {work.explain(entry.record, index, now)['reason']}", ranked=ranked)
            for entry in picture.items if _accepted(entry.record) and entry.record.state == "parked"
            and isinstance(entry.record.data.get("parked_until"), Mapping)],
        SECTION_COULD_NOT_SEE: _could_not_see_lines(inputs.review.source_status),
    }
    shown = {line.item_ref for name in _SECTIONS_BEFORE_LEFTOVERS for line in sections[name]}
    leftovers = tuple(entry for entry in active.live if work.first_id(entry.record) not in shown)
    sections[SECTION_COMING_UP] = _coming_up_lines(picture, leftovers, ranked, order)
    sections[SECTION_NO_DATE_YET] = _no_date_yet_lines(picture, leftovers, ranked, order)
    seen: set[str] = set()
    folded: dict[str, ItemRecord] = {}
    for entry in picture.live:
        record = index[work.first_id(entry.record)]
        if any(part.data.get("parent") == work.first_id(record) for part in index.values()) and work.kind_of(record, index) in ("task", "subtask"):
            folded[work.first_id(record)] = entry
    for name in OVERVIEW_SECTIONS:
        if name in (SECTION_NOW, SECTION_HIGHEST, SECTION_FOCUS_PROJECTS, SECTION_COULD_NOT_SEE):
            continue
        kept = []
        for line in sections[name]:
            record = next((entry.record for entry in picture.items if work.first_id(entry.record) == line.item_ref), None)
            if record is not None:
                mapped = index[work.first_id(record)]
                parent = index.get(mapped.data.get("parent"))
                if parent is not None and work.first_id(parent) in folded and name != SECTION_MISSED:
                    continue
                if work.first_id(record) in seen:
                    continue
                if work.first_id(record) in folded and name != SECTION_PARKED:
                    progress = work.progress_of(mapped, index)
                    next_ = work.next_of(mapped, index, now)
                    next_record = index.get(next_["item_ref"], mapped)
                    if any(mark.get("mark") == "agents_wording" and mark.get("part") == "next_action" for mark in next_record.data.get("review_marks", ())):
                        next_ = {**next_, "text": next_record.title}
                    why = work.explain(mapped, index, now)
                    clauses = [progress["text"]]
                    if next_["text"] != record.title:
                        clauses.append("next: " + next_["text"])
                    if next_["status"] == "waiting":
                        clauses.append(next_["reason"])
                        if next_["until"] and not (work.event_day(work.dates_of(mapped, index))
                                                    or work.event_day(work.dates_of(next_record, index))):
                            clauses.append("waiting until " + _day_word(date.fromisoformat(next_["until"]), work._day(picture.now)))
                    line = replace(line, text=record.title + " — " + "; ".join(clauses),
                                   fields_used={**line.fields_used, "progress": dict(progress), "next": dict(next_), "why": dict(why)})
                if work.first_id(record) not in folded:
                    progress = work.progress_of(mapped, index)
                    next_ = work.next_of(mapped, index, now)
                    next_record = index.get(next_["item_ref"], mapped)
                    marked = any(mark.get("mark") == "agents_wording" and mark.get("part") == "next_action" for mark in next_record.data.get("review_marks", ()))
                    if marked:
                        next_ = {**next_, "text": next_record.title}
                    if not marked and next_["text"] != record.title and "next:" not in line.text:
                        line = replace(line, text=line.text + "; next: " + next_["text"])
                    line = replace(line, fields_used={**line.fields_used, "progress": dict(progress),
                                   "next": dict(next_), "why": dict(work.explain(mapped, index, now)),
                                   "dates": work.dates_of(mapped, index), "labels": list(mapped.data.get("labels", ()))})
                if is_routine(record):
                    stats = routine_stats(record, versions_of(record, tuple(entry.record for entry in inputs.review.items)), picture.today)
                    line = replace(line, text=line.text + "; " + stats_words(stats),
                                   fields_used={**line.fields_used, "streak": stats.streak, "rate": stats.rate})
                if inputs.schedule_mode and name not in (SECTION_PARKED, SECTION_FINISHED_THIS_WEEK):
                    from brain_core.schedule import SPLIT_TASK_NOTICE, split_notice_required
                    target = index.get(work.next_of(mapped, index, now)["item_ref"], mapped)
                    if split_notice_required(target, index):
                        line = replace(line, text=line.text + "; " + SPLIT_TASK_NOTICE,
                                       fields_used={**line.fields_used, "schedule_issue": "needs_split"})
                seen.add(work.first_id(record))
            kept.append(line)
        sections[name] = kept
    present = tuple(name for name in OVERVIEW_SECTIONS if sections[name])
    return Overview(lines=tuple(line for name in present for line in sections[name]), sections=present)
