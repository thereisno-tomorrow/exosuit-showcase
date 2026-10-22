"""Frozen weekly review (memo 2.25 and plan 6.3): pure, no IO.

Finished parts, missed deadlines, unanswered needs-you, waits by person, every
live goal/project once, routines, next week's dates, and ideas only on Nic's yes.
Effective work projections are read-only; original records retain audit hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

from brain_core.language import focus_preference
from brain_core.presentation import relative_day_words, work_words
from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import (
    ItemRecord,
    OpenRequest,
    current_items,
    CalendarEvent,
    HistoryRow,
    REQUEST_RETURNED_PROOF,
    REQUEST_STALE_DAYS,
    _review_question_words,
    _clock_word,
    request_title,
)
from brain_core.routines import is_routine, routine_stats, stats_words, versions_of
from brain_core.sitreps import ProjectTasks
from brain_core.work import dates_of, done_day_of, explain, first_id, index_things, kind_of, needs_nic, next_of, progress_of, status_of
from brain_core.timewords import today_in_singapore, personal_today

WEEK_DAYS = 7
PART_CAP = 10
PARTS: tuple[str, ...] = (
    "finished", "overdue", "needs_you", "waiting_by_person", "goals_and_projects", "routines", "ideas", "next_week", "parked",
)
TITLES: dict[str, str] = {
    "finished": "Finished this week",
    "overdue": "Missed deadlines",
    "needs_you": work_words("needs_you") + ": still unanswered",
    "waiting_by_person": "Waiting on, by person",
    "goals_and_projects": "Goals and projects: progress and next",
    "routines": "Routines: slipping, and strong",
    "ideas": "Ideas: take up, keep or drop",
    "next_week": "Next week's deadlines and appointments",
    "parked": "Parked",
}


def _day(day: date, today: date | None = None) -> str:
    return relative_day_words(day, today)


@dataclass(frozen=True, slots=True)
class WeeklyLine:
    part: str
    text: str
    item_ref: str
    fields_used: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WeeklyReview:
    week_start: date
    week_end: date
    lines: tuple[WeeklyLine, ...]

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(part for part in PARTS if any(line.part == part for line in self.lines))


def _accepted(record: Record) -> bool:
    return record.schema == RECORD_SCHEMA_V2 and record.kind == "item" and record.data.get("level") == "accepted"


def _capped(part: str, lines: list[WeeklyLine]) -> list[WeeklyLine]:
    if len(lines) <= PART_CAP:
        return lines
    extra = len(lines) - PART_CAP
    return [*lines[:PART_CAP], WeeklyLine(part, f"{extra} more; ask to see them", f"__more_{part}__", {"remaining": extra})]


def weekly_review(
    items: Iterable[ItemRecord],
    now: datetime,
    *,
    open_requests: Iterable[OpenRequest] = (),
    preferences: Iterable[Record] = (),
    project_tasks: tuple[ProjectTasks, ...] | None = None,
    ideas: bool = False,
    history: Iterable[HistoryRow] = (),
    calendar_events: Iterable[CalendarEvent] = (),
    prepared_index=None,
) -> WeeklyReview:
    """Build memo 2.25 exactly from the sole work reader, without writing records.

    ``ideas`` means Nic has agreed to see them. Owner words are quoted; existing
    agent paraphrases are labelled for his rewording, never passed off as his words.
    ``history`` includes W4's durable stale-request consumption markers; the host
    consumes only the stale requests actually shown in the returned packet.
    """
    everything = tuple(items)
    all_records = tuple(entry.record for entry in everything)
    originals = {first_id(entry.record): entry for entry in current_items(everything)}
    index = index_things(all_records) if prepared_index is None else prepared_index
    rows = tuple(history)
    today = personal_today(now)
    week_start = today - timedelta(days=WEEK_DAYS - 1)
    current = [record for record in index.values() if _accepted(record)]
    def event_parked(record: Record) -> bool:
        while True:
            if record.state == "parked" and isinstance(record.data.get("parked_until"), Mapping):
                return True
            parent = index.get(record.data.get("parent"))
            if parent is None:
                return False
            record = parent
    live = [record for record in current if status_of(record, index, now) != "ended" and not event_parked(record)]
    lines: list[WeeklyLine] = []

    finished = sorted(
        (record for record in current if record.state == "done"
         and week_start <= done_day_of(record, index) <= today),
        key=lambda record: (done_day_of(record, index), record.updated_at, record.id), reverse=True,
    )
    lines += _capped("finished", [
        WeeklyLine("finished", f"{record.title} — done {_day(done_day_of(record, index), today)}",
                   first_id(record), {"done_at": record.updated_at.isoformat(), "done_day": done_day_of(record, index).isoformat()})
        for record in finished
    ])

    overdue = []
    unanswered = []
    for ref, record in index.items():
        if is_routine(record):
            continue
        if record in live:
            deadline = dates_of(record, index).get("deadline")
            if deadline and deadline["day"] < today.isoformat():
                day = date.fromisoformat(deadline["day"])
                overdue.append(WeeklyLine("overdue", f"{record.title} — missed since {_day(day, today)} "
                                          f"({(today - day).days} days)", ref, {"deadline": deadline}))
        # Answered rows suppress the same saved version; a changed version may need a new answer.
        answered = any(row.item_ref in (ref, record.id, *record.supersedes)
                       and row.rule == "needs_you" and row.response is not None
                       and row.record_sha256 == originals[ref].sha256 for row in rows)
        needs = [need for need in needs_nic(record, index, now, rows) if need["kind"] == "needs_you"]
        if needs and not answered and record.data.get("level") != "possible":
            unanswered.append(WeeklyLine("needs_you", f"{record.title} — " + " · ".join(
                dict.fromkeys(_review_question_words(need["text"], ensure_question=True) for need in needs)),
                ref, {"needs": needs}))
    lines += _capped("overdue", overdue)
    lines += _capped("needs_you", unanswered)

    people: dict[str, list[str]] = {}
    for record in live:
        who = (record.data.get("needs") or {}).get("from") or record.data.get("whose")
        if who:
            people.setdefault(str(who), []).append(record.title)
    stale = []
    for request in open_requests:
        if request.state == REQUEST_RETURNED_PROOF or request.check == "accepted":
            continue
        last = request.last_activity
        idle = last is not None and now - last >= timedelta(days=REQUEST_STALE_DAYS)
        if idle:
            if not any(row.item_ref == request.id and row.rule == "stale_request" for row in rows):
                stale.append(WeeklyLine("waiting_by_person",
                    f"{request.handed_to}: {request_title(request.id, request.objective)} — stale",
                    request.id, {"rule": "stale_request", "last_activity": last.isoformat() if last else request.since.isoformat()}))
        else:
            people.setdefault(request.handed_to, []).append(request_title(request.id, request.objective))
    lines += _capped("waiting_by_person", [
        WeeklyLine("waiting_by_person", f"{person}: {'; '.join(titles)}", f"person:{person}", {"count": len(titles)})
        for person, titles in sorted(people.items(), key=lambda pair: pair[0].casefold())
    ] + stale)

    projects = []
    for record in live:
        if kind_of(record, index) not in ("goal", "project"):
            continue
        progress = progress_of(record, index)
        next_item = next_of(record, index, now)
        steps = next_item["children"] or (next_item,)
        next_text = "; ".join(
            step["text"] + (f" ({work_words(step['reason'])}" + (f" until {relative_day_words(step['until'], today)}" if step["until"] else "") + ")"
                            if step["reason"] else "") for step in steps
        )
        projects.append(WeeklyLine("goals_and_projects",
            f"{record.title} — {progress['text']}; next: {next_text}; still want it?", first_id(record),
            {"progress": progress, "next": next_item}))
    # Project-vault summaries join by the same name rule as orientation, never a second store.
    from brain_core.orientation import same_project
    preference = focus_preference(tuple(preferences))
    names = list(preference.data["focus_projects"]) if preference and project_tasks is not None else []
    names += [summary.project for summary in project_tasks or ()]
    seen = [record.title for record in live if kind_of(record, index) == "project"]
    for project in names:
        if any(same_project(project, name) for name in seen):
            continue
        seen.append(project)
        tasks = [task for summary in project_tasks or () if same_project(project, summary.project) for task in summary.tasks]
        done = sum(task.state in ("done", "closed") for task in tasks)
        steps = [task.next_step or task.intent for task in tasks if task.state in ("open", "blocked")]
        projects.append(WeeklyLine("goals_and_projects",
            f"{project} — {done} of {len(tasks)}; next: {'; '.join(steps) or 'nothing next'}; still want it?", f"project:{project}"))
    # Every goal/project, once: this section is not capped.
    lines += projects

    routine_lines = []
    for record in live:
        if record.state != "open" or not is_routine(record):
            continue
        original = originals[first_id(record)].record
        stats = routine_stats(original, versions_of(original, all_records), today)
        if stats.slipping or stats.strong:
            routine_lines.append(WeeklyLine("routines", f"{record.title} — {stats_words(stats)}", first_id(record),
                {"slipping": stats.slipping, "strong": stats.strong, "rate": stats.rate}))
    routine_lines.sort(key=lambda line: (not line.fields_used["slipping"], line.text))
    lines += _capped("routines", routine_lines)

    if ideas:
        for entry in current_items(everything):
            record = entry.record
            if record.data.get("level") != "possible" or record.state not in ("open", "parked"):
                continue
            source = record.source or {}
            words = source.get("words")
            owned = source.get("said_by") == "owner" and isinstance(words, str) and bool(words.strip())
            text = str(words).strip() if owned else record.title
            if not owned:
                text = f"Agent's paraphrase (needs your words): {text}"
            lines.append(WeeklyLine("ideas", text, first_id(record), {"paraphrase": not owned}))

    horizon = today + timedelta(days=WEEK_DAYS)
    upcoming = []
    for record in live:
        for kind, value in dates_of(record, index).items():
            if kind not in ("deadline", "appointment"):
                continue
            day = date.fromisoformat(value["day"])
            if today < day <= horizon:
                label = "due" if kind == "deadline" else "appointment"
                at = f" at {_clock_word(value['at'])}" if value.get("at") else ""
                upcoming.append((day, WeeklyLine("next_week", f"{record.title} — {label} {_day(day, today)}{at}",
                                               first_id(record), {kind: value})))
    for event in calendar_events:
        day = today_in_singapore(event.start)
        if today < day <= horizon and event.exosuit_role is None:
            upcoming.append((day, WeeklyLine("next_week", f"{event.title} — appointment {_day(day, today)}",
                                           f"calendar:{event.start.isoformat()}:{event.title}")))
    lines += _capped("next_week", [line for _, line in sorted(upcoming, key=lambda pair: (pair[0], pair[1].item_ref))])
    lines += _capped("parked", [WeeklyLine("parked", f"{record.title} — {explain(record, index, now)['reason']}",
        first_id(record), {"parked_until": record.data["parked_until"]}) for record in current
        if record.state == "parked" and isinstance(record.data.get("parked_until"), Mapping)])
    return WeeklyReview(week_start=week_start, week_end=today, lines=tuple(lines))
