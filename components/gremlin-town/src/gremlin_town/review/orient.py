"""Orchestrates step F's two asks and Nic's answer to a pick (§3-§6; Ciel slice 4).

- `overview` answers "where am I?" (redesign N1): the whole picture, ranked, recommending
  nothing. It logs each line it showed on the why-log's `overview` channel before answering;
  those rows never count towards "tends to stall".
- `what_next` answers "what next?" (N2): a top pick with a first step and, if it tends to stall,
  a time box, two alternatives and what can wait. It logs them on the `orient` channel before
  answering.
- `respond` logs the answer. Only the admitted door caller relays yes/other as
  Start with Nic's raw words; personal work never starts a continuity note.

Disabled (`[orientation] enabled = false`, the default) each returns `{"outcome": "disabled"}`
immediately: no reads, no why-log, nothing started. Enabled, both asks collect the same plain
data `collect.py` gives the 06:00 review, plus today's picks, Nic's preferences and, while
`[sitreps]` is on, his projects' open tasks from the hub's task sitreps, and decide with
`brain_core.orientation` (no model call). They write nothing except why-log rows (§2).
`respond` never re-derives the pick: it reads the item back by the path the why-log pinned, so
it never needs the whole store, except to name what's in progress before a new yes (N10d). A
picked project task needs no read at all: its note is already open in its project.
See docs/product/steps/f-orientation.md.
"""

from __future__ import annotations

import logging
import hashlib
import inspect
import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from brain_core.orientation import (
    PlannedSlot,
    IN_PROGRESS_NAMED_AT,
    OVERVIEW_CHANNEL,
    OVERVIEW_TITLES,
    PROJECT_REF_PREFIX,
    OrientationInputs,
    OrientationSignal,
    Overview,
    WhatNext,
    caution,
    in_progress,
    surfaced_unchanged,
    tends_to_stall,
    time_box,
)
from brain_core.agenda import agenda as build_agenda
from brain_core.blocks import Override
from brain_core.weekly import TITLES as WEEKLY_TITLES
from brain_core.weekly import weekly_review as build_weekly
from brain_core.orientation import focus_project_ids
from brain_core.orientation import overview as build_overview
from brain_core.orientation import what_next as build_what_next
from brain_core.records import Record
from brain_core import work
from brain_core.review import HistoryRow, ItemRecord, OpenNote
from brain_core.sitreps import ProjectTasks, TaskSitrep
from brain_core.timewords import SINGAPORE, today_in_singapore

from gremlin_town.review.brain_source import describe_failure
from gremlin_town.review.collect import ReviewSources, SourceUnavailable, collect_review_inputs
from gremlin_town.review.settings import OrientationSettings, ReviewSettings
from gremlin_town.review.why_log import Surfacing, WhyLog
from gremlin_town.mcp.brain_runtime import BrainCaller, BrainRuntimeError, WorkRequest, work_caller_allowed

logger = logging.getLogger(__name__)

NOT_ENOUGH_ITEM_REF = "__orient__"
# "What next?" rows, and the caution after picking something else, keep step F's own channel.
ORIENT_CHANNEL = "orient"
Response = Literal["yes", "no", "not_now", "other"]

# The older `session_id -> next_step` notes (step F §6's `item-<record_id>` key). Kept for
# `run.py`'s daytime returns; both asks read structured notes for their existing
# project/request surfaces, never as the started fact for personal work.
OpenNotesReader = Callable[[], Mapping[str, str]]
# Nic's preference records, from which Brain Core finds his focus projects (N4).
PreferencesReader = Callable[[], "tuple[Record, ...]"]
# His projects' open and blocked tasks from the hub's task sitreps, one entry per project with
# when its summary was last written (ROA-60; `brain_source.brain_task_sitreps_reader`).
TaskSitrepsReader = Callable[[], "tuple[ProjectTasks, ...]"]
# How "Couldn't see" names the task sitreps when they couldn't be read.
PROJECT_SUMMARIES_SOURCE = "project summaries"


class ContinuityStarter(Protocol):
    """Ensures the running note for one task exists; never raises for "already exists" -

    replaying the same `yes` must return the same note (§6), so an adapter wrapping
    `brain_core.continuity.start_continuity` is expected to catch its own `RECORD_EXISTS` and
    treat it as success before this protocol ever sees it. `next_step` may be empty (N12c);
    `task_id` links the note to its task (N12b) and `intent` names the task.
    """

    def start(
        self,
        *,
        session_id: str,
        actor: str,
        next_step: str,
        task_id: str | None = None,
        intent: str | None = None,
    ) -> object: ...


class WorkDoor(Protocol):
    """Host door callback: authenticated actor and the exact public work request.

    The host preserves BrainRuntime.change_work authorization and publication;
    orientation never constructs a second writer. Async hosts return an awaitable.
    """

    def __call__(self, actor: BrainCaller, request: WorkRequest) -> Mapping[str, object] | Awaitable[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class OrientationSources:
    """`review` is step E's own `ReviewSources` (items, calendar, open requests, open notes).
    `preferences` reads Nic's preferences for his focus projects; left `None`, none count.
    `task_sitreps` reads his projects' open tasks from the hub's task sitreps, wired only while
    `[sitreps]` is on (N4, ROA-60); left `None`, both asks answer exactly as before: no "Focus
    projects" section, and no project task competes."""

    review: ReviewSources
    open_notes: OpenNotesReader | None = None
    preferences: PreferencesReader | None = None
    task_sitreps: TaskSitrepsReader | None = None
    # A hub-local snapshot reader. It never calls FlowSavvy from a surface read.
    schedule: Callable[[datetime], Any] | None = None


@dataclass(frozen=True, slots=True)
class OrientationContext:
    """The host's read sources and optional authenticated Start door.

    work_door accepts BrainCaller with its verified scopes and exact WorkRequest.
    door_participant is host configuration; W12 alone defines caller admission.
    Defaults keep existing constructors compatible and fail explicitly on Start
    if the required host wiring is absent.
    """

    orientation_settings: OrientationSettings
    review_settings: ReviewSettings
    why_log: WhyLog | None
    sources: OrientationSources
    continuity: ContinuityStarter | None = None
    # `[placement]` on (step I; build plan slice 6): both asks read today's plan from the why-log.
    # Off (the default), they read nothing more and answer exactly as before.
    placement_enabled: bool = False
    # Host wiring only; admission remains W12's work_caller_allowed gate.
    work_door: WorkDoor | None = None
    door_participant: str = "hermes-wa"
    schedule_enabled: bool = False


def _read(reader: Callable[[], Any] | None) -> tuple[Any, str | None]:
    """A source's value, or why it couldn't be read: a failing source never blocks an answer (§7)."""

    if reader is None:
        return (), "not_configured"
    try:
        return reader(), None
    except SourceUnavailable as exc:
        return (), exc.reason
    except Exception as exc:
        return (), describe_failure(exc)


def read_open_notes(reader: OpenNotesReader | None) -> tuple[Mapping[str, str], str | None]:
    if reader is None:
        return {}, "not_configured"
    try:
        return reader(), None
    except Exception as exc:  # a source's own failure never blocks orientation (spec §7)
        return {}, describe_failure(exc)


def read_note_records(context: OrientationContext) -> tuple[tuple[OpenNote, ...], str | None]:
    """Every open note `respond` can find a task's note among (N12b), and why not if it can't:
    step E's structured notes when configured - they carry each note's task link - else
    orientation's own `session_id -> next_step` mapping, whose notes name no task (found by the
    older `item-<record_id>` key). Like `read_open_notes`, a failing source never blocks the
    answer (spec §7); the caller reports it."""

    reader = context.sources.review.open_notes
    if reader is not None:
        try:
            return tuple(reader()), None
        except Exception as exc:
            return (), describe_failure(exc)
    mapping, status = read_open_notes(context.sources.open_notes)
    earliest = datetime.min.replace(tzinfo=UTC)
    notes = tuple(OpenNote(session_id=key, next_step=value, updated_at=earliest) for key, value in mapping.items())
    return notes, status


def _picked_today_ids(history: tuple[HistoryRow, ...], today: date) -> frozenset[str]:
    ids: set[str] = set()
    for row in history:
        if row.response != "picked_today":
            continue
        moment = row.responded_at or row.surfaced_at
        if today_in_singapore(moment) == today:
            ids.add(row.item_ref)
    return frozenset(ids)


def _enabled_why_log(context: OrientationContext) -> WhyLog:
    why_log = context.why_log
    if why_log is None:
        raise RuntimeError("[orientation] is enabled but no why-log was built")
    return why_log


def _collect(context: OrientationContext, history: tuple[HistoryRow, ...], now: datetime) -> OrientationInputs:
    """Step E's plain data plus today's picks, Nic's preferences and, while `[sitreps]` is on,
    his projects' open tasks. The review's own facts are never read: neither ask uses Nic's
    answers to its questions. Nor are its own preferences: the asks read them once, below."""

    sources = context.sources
    # Nor does either ask say Brain Home's size: that heads-up is the 06:00 review's alone.
    review = collect_review_inputs(
        replace(sources.review, brain_facts=None, brain_preferences=None, task_sitreps=None, brain_home_size=None),
        history, now,
        ranking=context.orientation_settings.ranking,
    )
    status = dict(review.source_status)
    if sources.review.open_notes is None:
        # Both asks read the notes (N12b): without them nothing shows as in progress and no
        # first step comes from a note, so say so (the 06:00 review leaves this out).
        status["notes"] = "not_configured"
    preferences, preferences_status = _read(sources.preferences)
    if preferences_status not in (None, "not_configured"):
        status["preferences"] = preferences_status
    # Sitreps off: no reader, nothing read, nothing said. On but unreadable: named under
    # "Couldn't see", and the asks go on without project tasks (§7).
    project_tasks: tuple[ProjectTasks, ...] | None = None
    if sources.task_sitreps is not None:
        tasks, tasks_status = _read(sources.task_sitreps)
        if tasks_status is None:
            project_tasks = tuple(tasks)
        else:
            status[PROJECT_SUMMARIES_SOURCE] = tasks_status
    result = OrientationInputs(
        review=replace(review, source_status=status),
        picked_today=_picked_today_ids(history, today_in_singapore(now)),
        preferences=tuple(preferences),
        project_tasks=project_tasks,
        blocks=context.orientation_settings.blocks and not context.schedule_enabled,
        plan_slots=(_plan_slots(context, review.items, now, status)
                    if context.placement_enabled and not context.schedule_enabled else None),
    )
    if not context.schedule_enabled:
        return result
    try:
        snapshot = sources.schedule(now) if sources.schedule is not None else None
        available = snapshot is not None and snapshot.available
        index = work.index_things(entry.record for entry in review.items)
        slots = tuple(
            PlannedSlot(key=block.item_ref, title=index[block.item_ref].title,
                        start=block.start, end=block.end)
            for block in snapshot.blocks if block.item_ref in index
        ) if available else None
        updated = snapshot.fetched_at if snapshot is not None else None
    except Exception:
        # Reader errors must reach the surface, but never expose local paths or remote data.
        slots, updated = None, None
        logger.warning("Schedule snapshot could not be read")
    return replace(result, plan_slots=slots, schedule_mode=True, schedule_updated_at=updated)


def _with_schedule(answer: dict[str, object], inputs: OrientationInputs, now: datetime) -> dict[str, object]:
    if inputs.schedule_mode:
        updated = inputs.schedule_updated_at
        answer["schedule"] = {
            "available": inputs.plan_slots is not None,
            "updated_at": updated.isoformat() if updated is not None else None,
            "age_seconds": max(0, int((now - updated).total_seconds())) if updated is not None else None,
        }
    return answer


def _plan_slots(
    context: OrientationContext, items: tuple[ItemRecord, ...], now: datetime, status: dict[str, str | None],
) -> tuple[PlannedSlot, ...]:
    """Today's task slots from the latest placement run, with titles and first steps from each
    task's current version in Brain Home (the why-log keeps none). No run today, no slots. A plan
    that can't be read is named under "Couldn't see" and the asks answer as with no plan (§7).
    A task Nic said to move after its last slot ended has been handed back: its passed slots are
    left out, so it isn't asked about again while it waits for room."""

    from brain_core.placement import plan_from_mapping
    from brain_core.review import current_items

    from gremlin_town.review.placement import DAY_CHANGE, DayChangeError, parse_day_change

    why_log = context.why_log
    today = today_in_singapore(now)
    try:
        last = why_log.latest_placement(today) if why_log is not None else None
        plan = plan_from_mapping(last["plan"]) if last is not None else None
        changes = why_log.signals_on(DAY_CHANGE, today) if why_log is not None and plan is not None else ()
    except Exception as exc:  # a failing source never blocks the answer (§7)
        status["placement"] = f"{type(exc).__name__}: {exc}"
        return ()
    if plan is None:
        return ()
    words: dict[str, tuple[str, str | None]] = {}
    ids: dict[str, str] = {}
    for entry in current_items(items):
        key = entry.record.supersedes[0] if entry.record.supersedes else entry.record.id
        words[key] = (entry.record.title, entry.record.data.get("first_step"))
        for record_id in (entry.record.id, *entry.record.supersedes):
            ids[record_id] = key
    moved: dict[str, datetime] = {}
    for signal in changes:
        try:
            target = parse_day_change(signal.detail).get("move")
        except DayChangeError:
            continue
        if target in ids:
            moved[ids[target]] = max(signal.at, moved.get(ids[target], signal.at))
    last_end = {}
    for slot in plan.slots:
        last_end[slot.key] = max(slot.end, last_end.get(slot.key, slot.end))
    fill = set(plan.fill_keys)
    return tuple(
        PlannedSlot(
            key=slot.key, title=words[slot.key][0], start=slot.start, end=slot.end,
            first_step=words[slot.key][1], fill=slot.key in fill,
        )
        for slot in plan.slots
        if slot.key in words and not (slot.key in moved and slot.end <= now and moved[slot.key] >= last_end[slot.key])
    )


# --- "Where am I?" --------------------------------------------------------------------------------


def _overview_surfacings(answer: Overview, now: datetime) -> tuple[Surfacing, ...]:
    return tuple(
        Surfacing(
            surfaced_at=now,
            channel=OVERVIEW_CHANNEL,
            item_ref=line.item_ref,
            record_sha256=line.record_sha256 or "",
            record_path=line.record_path,
            record_state=line.record_state,
            rule=line.rule,
            fields_used=line.fields_used,
        )
        for line in answer.lines
    )


def _overview_line(line) -> dict[str, object]:
    result: dict[str, object] = {"text": line.text, "item_ref": line.item_ref}
    if line.reasons:
        result["reasons"] = list(line.reasons)
    if line.fields_used.get("stuck"):
        result["stuck"] = True
    if line.fields_used.get("project"):
        # "Focus projects" lines (N4): which project a line belongs to, as sitreps name it.
        result["project"] = line.fields_used["project"]
    return result


def _overview_as_dict(answer: Overview) -> dict[str, object]:
    return {
        "outcome": "answered",
        "title": answer.title,
        "sections": [
            {
                "section": name,
                "title": OVERVIEW_TITLES[name],
                "lines": [_overview_line(line) for line in answer.lines if line.section == name],
            }
            for name in answer.sections
        ],
    }


def _orientation_inputs(context: OrientationContext, history: tuple[HistoryRow, ...], now: datetime) -> OrientationInputs:
    """Keep raw source records for audit pins; the shared reader owns projection.

    Normalize pick history through its first-ID index without changing the
    weekly/agenda collector or copying the reader's mapping/ranking logic.
    """
    inputs = _collect(context, history, now)
    index = work.index_things(entry.record for entry in inputs.review.items)
    aliases = {ref: fid for fid, record in index.items() for ref in (record.id, *record.supersedes)}
    picked = frozenset(aliases.get(ref, ref) for ref in inputs.picked_today)
    return replace(inputs, picked_today=picked)


def overview(context: OrientationContext, now: datetime) -> dict[str, object]:
    """"Where am I?" (N1, case 4): the whole picture, ranked. Read-only apart from the why-log
    rows it writes before answering (§2), on the `overview` channel. It recommends nothing."""

    if not context.orientation_settings.enabled:
        return {"outcome": "disabled"}
    why_log = _enabled_why_log(context)
    why_log.prune(now)
    history = why_log.history()
    inputs = _orientation_inputs(context, history, now)
    answer = build_overview(inputs, now)
    why_log.record_surfacings(_overview_surfacings(answer, now))  # logged before answering (§2)
    return _with_schedule(_overview_as_dict(answer), inputs, now)


# --- Agendas (Ciel and Brain Home PRD §4.9) ------------------------------------------------------


def agenda(context: OrientationContext, now: datetime, *, person: str, exact: bool = False) -> dict[str, object]:
    """"What do I need to raise with Sam?" (PRD §4.9, acceptance case 14): what to raise with
    ``person``, Nic's promises to them, their parts (``whose``), and needs from them,
    including legacy waits, through the sole reader (`brain_core.agenda`).
    A first name finds a full one; when several people answer to it the outcome
    is ``ask_which`` with their names (Nic, 29 Sep 2026). Read-only, and it writes no why-log row: it
    only filters his records."""

    if not context.orientation_settings.enabled:
        return {"outcome": "disabled"}
    inputs = _collect(context, (), now)
    review = inputs.review
    unknowns = [
        f"{source}: {status}" for source, status in review.source_status.items() if status not in (None, "", "ok")
    ]
    if review.source_status.get("brain") not in (None, "", "ok"):
        return {"outcome": "not_enough", "reason": "Couldn't see your saved items.", "unknowns": unknowns}
    result = build_agenda(
        review.items, person, now, notes=review.open_notes or (),
        focus_projects=focus_project_ids(inputs.preferences), ranking=context.orientation_settings.ranking,
        exact=exact,
    )
    if result.ask_which:
        return {"outcome": "ask_which", "person": result.person, "ask_which": list(result.ask_which),
                "unknowns": unknowns}

    def listed(lines) -> list[dict[str, object]]:
        return [
            {"record_id": line.record_id, "title": line.title, "state": line.state, "reasons": list(line.reasons)}
            for line in lines
        ]

    return {
        "outcome": "answered",
        "person": result.person,
        "to_raise": listed(result.to_raise),
        "promised": listed(result.promised),
        "waiting_on": listed(result.waiting_on),
        "unknowns": unknowns,
    }


# --- The weekly review (Ciel and Brain Home PRD §4.8) ---------------------------------------------


def weekly(context: OrientationContext, now: datetime, ideas: bool = False) -> dict[str, object]:
    """The weekly review's packet (PRD §4.8, acceptance case 13), worked out fresh each time: ready
    every Friday afternoon, and it waits until Nic opens it. Read-only: an agent walks him through
    it one decision at a time, and each answer is an ordinary change in his words.
    Only displayed stale requests write once-only weekly consumption markers.
    ``ideas`` requests the saved ideas only after Nic agrees to go through them.
    """

    if not context.orientation_settings.enabled:
        return {"outcome": "disabled"}
    history = context.why_log.request_review_history() if context.why_log is not None else ()
    inputs = _collect(context, history, now)
    review = inputs.review
    tomorrow = today_in_singapore(now) + timedelta(days=1)
    start = datetime.combine(tomorrow, datetime.min.time(), tzinfo=SINGAPORE)
    calendar = context.sources.review.calendar_events
    events, calendar_status = _read((lambda: calendar(start, start + timedelta(days=7))) if calendar else None)
    status = {**review.source_status, "calendar": calendar_status}
    unknowns = [
        f"{source}: {value}" for source, value in status.items() if value not in (None, "", "ok")
    ]
    if review.source_status.get("brain") not in (None, "", "ok"):
        return {"outcome": "not_enough", "reason": "Couldn't see your saved items.", "unknowns": unknowns}
    packet = build_weekly(
        review.items, now, open_requests=review.open_requests, preferences=inputs.preferences,
        project_tasks=inputs.project_tasks, ideas=ideas, history=history, calendar_events=events,
    )
    answer = {
        "outcome": "answered",
        "week": {"from": packet.week_start.isoformat(), "to": packet.week_end.isoformat()},
        "sections": [
            {
                "part": part,
                "title": WEEKLY_TITLES[part],
                "lines": [{"text": line.text, "item_ref": line.item_ref} for line in packet.lines if line.part == part],
            }
            for part in packet.parts
        ],
        "unknowns": unknowns,
        **({"ideas_question": "Want to go through your ideas?"} if not ideas else {}),
    }
    stale = tuple(Surfacing(
        surfaced_at=now, channel="weekly", item_ref=line.item_ref,
        record_sha256="", rule="stale_request", fields_used=line.fields_used,
    ) for line in packet.lines if line.fields_used.get("rule") == "stale_request")
    why_log = _enabled_why_log(context) if stale else context.why_log
    if why_log is not None:
        why_log.record_surfacings(stale)
        why_log.record_run(kind="weekly", started_at=now, last_event_cursor=None, outcome="answered",
                           consume_channel="weekly")
    return answer


# --- "What next?" ----------------------------------------------------------------------------------


def _row(
    now: datetime,
    item_ref: str,
    entry: ItemRecord | None,
    *,
    rule: str,
    fields_used: Mapping[str, Any],
    task: TaskSitrep | None = None,
    **extra: Any,
) -> Surfacing:
    """One "what next?" row. A saved item's pins its record's version and path; a project task's
    pins its note's version (the sitrep's pointer, for "tends to stall") and keeps the project,
    task and next step a later answer needs, since it has no Brain Home path (N4)."""

    if task is not None:
        return Surfacing(
            surfaced_at=now,
            channel=ORIENT_CHANNEL,
            item_ref=item_ref,
            record_sha256=task.note_content_sha256,
            record_state=task.state,
            rule=rule,
            fields_used={**fields_used, **_task_fields(task)},
            **extra,
        )
    return Surfacing(
        surfaced_at=now,
        channel=ORIENT_CHANNEL,
        item_ref=item_ref,
        record_sha256=entry.sha256 if entry else "",
        record_path=entry.path if entry else None,
        record_state=entry.record.state if entry else None,
        rule=rule,
        fields_used=fields_used,
        **extra,
    )


def _task_fields(task: TaskSitrep) -> dict[str, object]:
    return {"project": task.project, "session_id": task.session_id, "next_step": task.next_step}


def _what_next_surfacings(answer: WhatNext, items: tuple[ItemRecord, ...], now: datetime) -> tuple[Surfacing, ...]:
    # Picks carry lasting first IDs. Pin the original source bytes/path, not the
    # reader's mapped projection; collapse versions using the sole work reader.
    index = work.index_things(entry.record for entry in items)
    by_id = {work.first_id(entry.record): entry for entry in items
             if work.first_id(entry.record) in index
             and entry.record.id == index[work.first_id(entry.record)].id}
    # Asked about, never picked: a start time that has passed (Accepted - Nic, 28 Sep 2026).
    asked = [
        _row(now, entry.record_id, by_id.get(entry.record_id), rule="missed",
             fields_used={**entry.fields_used, "question": entry.question})
        for entry in answer.missed
    ]
    if answer.top_pick is None:
        reason = answer.not_enough.reason if answer.not_enough else "not_enough"
        questions = list(answer.not_enough.questions) if answer.not_enough else []
        return (
            _row(now, NOT_ENOUGH_ITEM_REF, None, rule=reason, fields_used={"questions": questions}, unknowns=answer.unknowns),
            *asked,
        )
    pick = answer.top_pick
    rows = [
        _row(
            now, pick.record_id, by_id.get(pick.record_id), rule="; ".join(pick.rules), fields_used=pick.fields_used,
            task=pick.task, assumptions=answer.assumptions, unknowns=answer.unknowns,
        )
    ]
    rows += [
        _row(
            now, alternative.record_id, by_id.get(alternative.record_id), rule="alternative",
            fields_used={**alternative.fields_used, "rules": list(alternative.rules)}, task=alternative.task,
        )
        for alternative in answer.alternatives
    ]
    rows += [
        _row(now, entry.record_id, by_id.get(entry.record_id), rule="can_wait", fields_used={"reason": entry.reason},
             task=entry.task)
        for entry in answer.can_wait
    ]
    return tuple(rows + asked)


def _in_progress_list(entries) -> list[dict[str, object]]:
    return [
        {"record_id": entry.record_id, "title": entry.title, "next_step": entry.next_step, "stuck": entry.stuck}
        for entry in entries
    ]


def _with_project(entry: dict[str, object], task: TaskSitrep | None) -> dict[str, object]:
    """A project task's entry also names its project and task (its note's session), N4."""

    if task is not None:
        entry.update(project=task.project, session_id=task.session_id)
    return entry


def _what_next_as_dict(answer: WhatNext, surfacing_id: str) -> dict[str, object]:
    result: dict[str, object] = {"outcome": "answered", "surfacing_id": surfacing_id}
    if answer.top_pick is not None:
        pick = answer.top_pick
        result["top_pick"] = _with_project({
            "record_id": pick.record_id,
            "title": pick.title,
            "plain_word": pick.plain_word,
            "rules": list(pick.rules),
            "fields_used": dict(pick.fields_used),
            "first_step": pick.first_step,
            "estimate_minutes": pick.estimate_minutes,
            "time_box_minutes": pick.time_box_minutes,
            "belongs_to": pick.belongs_to,
            "progress": pick.progress,
        }, pick.task)
    result["alternatives"] = [
        _with_project(
            {"record_id": entry.record_id, "title": entry.title, "plain_word": entry.plain_word, "rules": list(entry.rules),
             **({"belongs_to": entry.belongs_to, "progress": entry.progress} if entry.belongs_to is not None else {})},
            entry.task,
        )
        for entry in answer.alternatives
    ]
    result["can_wait"] = [
        _with_project({"record_id": entry.record_id, "title": entry.title, "reason": entry.reason}, entry.task)
        for entry in answer.can_wait
    ]
    if answer.in_progress:
        result["in_progress"] = _in_progress_list(answer.in_progress)
    if answer.missed:
        result["missed"] = [
            {"record_id": entry.record_id, "title": entry.title, "question": entry.question}
            # A passed slot (step I §5a): "move" is a day_change signal, not a new time.
            | ({"move": entry.fields_used["move"]} if "move" in entry.fields_used else {})
            for entry in answer.missed
        ]
    result["assumptions"] = list(answer.assumptions)
    result["unknowns"] = list(answer.unknowns)
    if answer.moment is not None:
        result["moment"] = dict(answer.moment)  # time blocks (PRD §4.3), only while on
    if answer.not_enough is not None:
        result["not_enough"] = {"reason": answer.not_enough.reason, "questions": list(answer.not_enough.questions)}
    if answer.plan_note is not None:
        result["plan_note"] = answer.plan_note  # step I §5a, only while [placement] is on
    return result


def what_next(
    context: OrientationContext,
    now: datetime,
    *,
    signal: OrientationSignal | None = None,
    override: Override | None = None,
) -> dict[str, object]:
    """"What next?" (N2, cases 4, 5, 14): read-only except for the why-log rows it writes (§2).
    `surfacing_id` is the top pick's record ID, or `__orient__` when there isn't enough."""

    if not context.orientation_settings.enabled:
        return {"outcome": "disabled"}
    why_log = _enabled_why_log(context)
    why_log.prune(now)
    history = why_log.history()
    inputs = _orientation_inputs(context, history, now)
    answer = build_what_next(inputs, now, signal=signal, override=override if inputs.blocks else None)
    why_log.record_surfacings(_what_next_surfacings(answer, inputs.review.items, now))  # before answering (§2)
    surfacing_id = answer.top_pick.record_id if answer.top_pick is not None else NOT_ENOUGH_ITEM_REF
    return _with_schedule(_what_next_as_dict(answer, surfacing_id), inputs, now)


# --- Nic's answer to a pick ------------------------------------------------------------------------


def _resolve_item(context: OrientationContext, item_ref: str) -> ItemRecord | None:
    why_log = context.why_log
    if why_log is None or item_ref == NOT_ENOUGH_ITEM_REF:
        return None
    reasons = why_log.full_reason(item_ref)
    if not reasons:
        return None
    path = reasons[-1].record_path
    reader = context.sources.review.brain_items_by_path
    if path is None or reader is None:
        return None
    items = reader((path,))
    return items[0] if items else None


def _project_task_pick(why_log: WhyLog, item_ref: str, now: datetime) -> dict[str, object] | None:
    """A picked project task (N4), read back from the row "what next?" logged for it; ``None`` for
    anything else. Its where-I-stopped note is already open in its project, so it is the one
    tracked item: nothing starts, its first step is the next step its sitrep carried, and the
    time box follows the same rule as a saved task's (note unchanged on 2 earlier days)."""

    if not item_ref.startswith(PROJECT_REF_PREFIX):
        return None
    reasons = why_log.full_reason(item_ref)
    if not reasons or not {"project", "session_id"} <= set(reasons[-1].fields_used):
        return None
    row = reasons[-1]
    today = today_in_singapore(now)
    stall_history = tuple(entry for entry in why_log.history() if today_in_singapore(entry.surfaced_at) < today)
    return {
        "outcome": "started",
        "project": row.fields_used["project"],
        "session_id": row.fields_used["session_id"],
        "first_step": str(row.fields_used.get("next_step") or "").strip(),
        "time_box_minutes": time_box(None, surfaced_unchanged(item_ref, row.record_sha256, stall_history)),
    }


def _cautioned_today(why_log: WhyLog, item_ref: str, today: date) -> bool:
    return any(
        row.rule == "caution" and today_in_singapore(row.surfaced_at) == today for row in why_log.full_reason(item_ref)
    )


def _named_before_a_new_yes(
    context: OrientationContext, notes: tuple[OpenNote, ...], now: datetime, *, item_ref: str | None = None,
) -> tuple[list[dict[str, object]], str | None]:
    """N10d: what's in progress, when 3 or more things are, to name before a new note starts."""

    items, status = _read(context.sources.review.brain_items)
    if status is not None:
        return [], None if status == "not_configured" else status
    progress = in_progress(items, notes, now)
    aliases = {entry.record.id: work.first_id(entry.record) for entry in items}
    if item_ref is not None and any(aliases.get(entry.record_id, entry.record_id) == item_ref for entry in progress):
        return [], None
    return (_in_progress_list(progress) if len(progress) >= IN_PROGRESS_NAMED_AT else []), None


def _start_task_note(
    context: OrientationContext, why_log: WhyLog, note_ref: str, now: datetime,
) -> dict[str, object] | None:
    """Only project sitreps use their project's existing continuity note.

    Personal work is started through the door; it never creates a note here.
    """
    result = _project_task_pick(why_log, note_ref, now)
    if note_ref.startswith(PROJECT_REF_PREFIX) and result is None:
        raise BrainRuntimeError("brain.not_found", "Could not resolve the project's existing note",
                                field="target", fix="ask bc_next again and answer a returned project task reference")
    return result


def respond(
    context: OrientationContext,
    now: datetime,
    *,
    actor: str | BrainCaller,
    surfacing_id: str,
    response: Response,
    words: str | None = None,
    target: str | None = None,
    first_step: str | None = None,
    said_by: Literal["owner", "other"] | None = None,
    message: str | None = None,
) -> dict[str, object] | Awaitable[dict[str, object]]:
    """Answer one pick without re-ranking it (plan 4.7, step F section 6).

    Tier B records only the why-log answer. The admitted authenticated caller
    relays yes/other as Start with explicit said_by, raw message and its words
    quote through the host's existing work door. Missing wiring names the fix.
    An async door returns an awaitable; the async host awaits it directly.
    first_step remains accepted for compatibility but cannot invent an action.
    Project sitreps keep their project's existing note, next step and time box.
    no/not_now and the once-daily caution retain their existing meaning.
    """

    if not context.orientation_settings.enabled:
        return {"outcome": "disabled"}
    why_log = _enabled_why_log(context)

    # The answer lands on the "what next?" row it answers, never on a later "where am I?" row.
    why_log.record_response(
        item_ref=surfacing_id, response=response, response_words=words, responded_at=now, channel=ORIENT_CHANNEL,
    )

    if response in ("no", "not_now"):
        return {"outcome": "recorded", "response": response}

    subject = actor.subject if isinstance(actor, BrainCaller) else actor
    if not work_caller_allowed(subject, door_participant=context.door_participant):
        return {"outcome": "recorded", "response": response}
    if not isinstance(actor, BrainCaller):
        raise BrainRuntimeError("ACCESS_DENIED", "Start needs the authenticated caller and scopes",
                                field="actor", fix="pass the host-authenticated BrainCaller to next.respond; a legacy string only logs")

    note_ref = target if (response == "other" and target) else surfacing_id
    today = today_in_singapore(now)
    project_task = _start_task_note(context, why_log, note_ref, now)
    if project_task is not None:
        result: dict[str, object] = project_task
    else:
        if context.work_door is None:
            raise BrainRuntimeError("NOT_CONFIGURED", "Orientation Start has no work door",
                                    field="work_door", fix="wire OrientationContext.work_door to BrainRuntime.change_work")
        if said_by is None or message is None or not message.strip():
            raise BrainRuntimeError("INVALID_ARGUMENT", "Start needs said_by and Nic's raw message",
                                    field="message", fix="supply said_by and the original message; words must quote that message")
        item = _resolve_item(context, note_ref)
        if item is None:
            raise BrainRuntimeError("NOT_FOUND", "Could not resolve the pinned orientation pick",
                                    field="surfacing_id", fix="ask bc_next again and answer its returned surfacing_id")
        move = {"move": "start", "thing": work.first_id(item.record)}
        if words is not None:
            move["words"] = words
        reason = why_log.full_reason(note_ref)[-1]
        payload = {"actor": subject, "message": message, "said_by": said_by, "move": move,
                   "surfaced_at": reason.surfaced_at.isoformat()}
        operation_id = "orient-start-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        request = WorkRequest(operation_id=operation_id, message=message, said_by=said_by,
                              came_by="direct", moves=(move,))
        named, items_status = _named_before_a_new_yes(context, (), now, item_ref=work.first_id(item.record))
        rows = tuple(row for row in why_log.history() if today_in_singapore(row.surfaced_at) < today)
        box = time_box(item.record, tends_to_stall(item.record, rows))

        def finish(reply: Mapping[str, object]) -> dict[str, object]:
            # Preserve the door's flags/dropped result: a refused/dropped Start
            # must never be reported as started by orientation.
            answer = dict(reply)
            answer["time_box_minutes"] = box
            if named:
                answer["in_progress"] = named
            if items_status:
                answer["unknowns"] = [f"brain: {items_status}"]
            return _respond_caution(context, why_log, now, surfacing_id, response, target, answer)

        reply = context.work_door(actor, request)
        if inspect.isawaitable(reply):
            async def complete() -> dict[str, object]:
                return finish(await reply)
            return complete()
        return finish(reply)

    return _respond_caution(context, why_log, now, surfacing_id, response, target, result)


def _respond_caution(
    context: OrientationContext, why_log: WhyLog, now: datetime, surfacing_id: str,
    response: Response, target: str | None, result: dict[str, object],
) -> dict[str, object]:
    """The existing once-daily caution, after the answer's own operation completes."""
    today = today_in_singapore(now)

    if response == "other" and target:
        try:
            declined = _resolve_item(context, surfacing_id)
            caution_text = None
            if declined is not None:
                items, status = _read(context.sources.review.brain_items)
                if status is not None:
                    logger.warning("Couldn't read the current work snapshot for its caution (%s)", status)
                else:
                    index = work.index_things(entry.record for entry in items)
                    caution_text = caution(declined.record, today, index)
        except Exception as exc:  # Start completed: a failed read only loses the caution, loudly
            logger.warning("Couldn't read the declined pick for its caution (%s)", describe_failure(exc))
            declined = None
        if declined is not None:
            if caution_text is not None and not _cautioned_today(why_log, surfacing_id, today):
                why_log.record_surfacings((
                    Surfacing(
                        surfaced_at=now,
                        channel=ORIENT_CHANNEL,
                        item_ref=surfacing_id,
                        record_sha256=declined.sha256,
                        record_path=declined.path,
                        record_state=declined.record.state,
                        rule="caution",
                        fields_used={},
                    ),
                ))
                result["caution"] = caution_text

    return result
