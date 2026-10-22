"""Collects the plain data brain_core.review / brain_core.moments decide over.

Brain Core stays deterministic; this is the one place that reads Brain Home
items, the calendar, and GT's own open requests - and a source's own failure
never crashes the whole review, it becomes one "Couldn't see" line (§3, §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from brain_core.moments import RequestStatus, requests_to_check
from brain_core.records import Record
from brain_core.review import RANKING_POINTS, CalendarEvent, HistoryRow, ItemRecord, MorningBootstrap, MorningInputs, OpenNote, OpenRequest, ReviewInputs
from brain_core.timewords import today_in_singapore

from gremlin_town.review.brain_source import describe_failure
from gremlin_town.review.calendar_link import marked_record_ids
from gremlin_town.review.calendar_entries import has_start_time
from brain_core.review import ImportantEmail

_WAITING_LIKE_STATES = frozenset({"waiting", "blocked", "parked"})
# The 06:00 review's heads-up about Brain Home's size (the storage-limit plan, section 5), shown under
# "Couldn't see" as "Brain Home size: ...".
BRAIN_HOME_SIZE_SOURCE = "Brain Home size"


class SourceUnavailable(Exception):
    """One source's own reader could not be read; the review still runs without it."""

    def __init__(self, reason: str, *, items: tuple[Any, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.items = items


BrainItemsReader = Callable[[], "tuple[ItemRecord, ...]"]
BrainItemsByPathReader = Callable[["tuple[str, ...]"], "tuple[ItemRecord, ...]"]
CalendarEventsReader = Callable[[datetime, datetime], "tuple[CalendarEvent, ...]"]
OpenRequestsReader = Callable[[], "tuple[OpenRequest, ...]"]
OpenNotesListReader = Callable[[], "tuple[OpenNote, ...]"]
RequestStatusReader = Callable[["tuple[str, ...]"], "tuple[RequestStatus, ...]"]
BrainPreferencesReader = Callable[[], "tuple[Record, ...]"]
ImportantEmailsReader = Callable[
    [datetime, datetime | None, tuple[HistoryRow, ...]], tuple[ImportantEmail, ...]
]


@dataclass(frozen=True, slots=True)
class ReviewSources:
    """Narrow, already-authorized adapters `runtime.py` supplies when it can.

    A source left `None`, or one whose reader raises, is reported as
    unavailable rather than failing the whole review (§3's "Couldn't see").
    `brain_facts` (v2 facts, read only for Nic's answers, §3.1) is the exception
    when left `None`: no line, since then nothing is asked and no answer shown.
    `open_notes` (every open where-I-stopped note with when it last changed, N12) is
    the same: left `None`, no started task is asked for its first step, the review has
    no "Stuck" section (N10b) and returns find no first step.
    `request_status` (read only at a daytime signal, §12) reads the current state
    of the GT requests Nic was shown in "Waiting on"; left `None`, none come back.
    `calendar_adds_timed` is true only while calendar entries are on (N8): a task
    with a start time then gets its entry when it is saved, so it is never listed
    under "Calendar entries to add".
    `brain_preferences` reads Nic's preference records, only for his focus projects,
    which rank "Coming up" (N16); left `None`, none count and nothing is said.
    `task_sitreps` reads Nic's projects' open tasks from the hub's task sitreps, only to tell
    whether a focus project has a next action (PRD §4.10); wired only while `[sitreps]` is on.
    Left `None`, no focus project is flagged and nothing is said; a failed read is listed.
    `brain_home_size` says how full each of the hub's Brain Home scans is (the storage-limit plan,
    section 5): at 50% of a limit or more, one "Brain Home size" line names the fullest; below,
    nothing. Wired only for the 06:00 review; orientation passes `None`. A failed read is listed.
    """

    brain_items: BrainItemsReader | None = None
    brain_items_by_path: BrainItemsByPathReader | None = None
    calendar_events: CalendarEventsReader | None = None
    open_requests: OpenRequestsReader | None = None
    brain_facts: BrainItemsReader | None = None
    open_notes: OpenNotesListReader | None = None
    request_status: RequestStatusReader | None = None
    calendar_adds_timed: bool = False
    brain_preferences: BrainPreferencesReader | None = None
    task_sitreps: Callable[[], Any] | None = None
    brain_home_size: Callable[[], Any] | None = None
    # Optional morning-only facts; the reader checks current UNREAD labels and
    # at most two delivered mornings, using the host's prior-review timestamp.
    important_emails: ImportantEmailsReader | None = None


def _read(reader: Callable[..., Any] | None, *args: Any) -> tuple[Any, str | None]:
    if reader is None:
        return (), "not_configured"
    try:
        return reader(*args), None
    except SourceUnavailable as exc:
        return exc.items, exc.reason
    except Exception as exc:  # a source's own failure never crashes the review (§3)
        # A Brain Home read past a size limit, or refused for GitHub's allowance, says so plainly.
        return (), describe_failure(exc)


def collect_review_inputs(
    sources: ReviewSources,
    history: tuple[HistoryRow, ...],
    now: datetime,
    *,
    ranking: str = RANKING_POINTS,
) -> ReviewInputs:
    """Everything `brain_core.review.build_review` needs, each source read separately.
    ``ranking`` is `[orientation] ranking`: how "Coming up", and both asks, rank."""

    today = today_in_singapore(now)
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=now.tzinfo)
    day_end = day_start + timedelta(days=1)

    items, brain_status = _read(sources.brain_items)
    calendar_events, calendar_status = _read(sources.calendar_events, day_start, day_end)
    open_requests, requests_status = _read(sources.open_requests)
    source_status = {"brain": brain_status, "calendar": calendar_status, "requests": requests_status}

    # Facts only ever add Nic's answers and the clarifying questions (§3.1): unread
    # (`None`) means neither happens, so a missing reader needs no "Couldn't see" line.
    facts, facts_status = _read(sources.brain_facts)
    if facts_status is not None:
        facts = None
        if facts_status != "not_configured":
            source_status["brain facts"] = facts_status

    # The same for open notes (N12c, N10b): unread means no first-step question and no
    # "Stuck" section; a failed read is listed under "Couldn't see".
    open_notes, notes_status = read_open_note_list(sources)
    if notes_status is not None and notes_status != "not_configured":
        source_status["notes"] = notes_status

    # Nic's focus projects rank "Coming up" (N16). Unread, it still lists the same tasks, ranked
    # without them; a failed read is listed under "Couldn't see".
    preferences, preferences_status = _read(sources.brain_preferences)
    if preferences_status is not None and preferences_status != "not_configured":
        source_status["preferences"] = preferences_status

    # Focus projects with no next action (PRD §4.10) need their tasks: unread, none is flagged.
    project_tasks = None
    if sources.task_sitreps is not None:
        tasks, tasks_status = _read(sources.task_sitreps)
        if tasks_status is None:
            project_tasks = tuple(tasks)
        else:
            source_status["project summaries"] = tasks_status

    if sources.brain_home_size is not None:
        size_line = brain_home_size_line(sources.brain_home_size, source_status)
        if size_line is not None:
            source_status[BRAIN_HOME_SIZE_SOURCE] = size_line

    marked_ids = marked_record_ids(calendar_events)
    if sources.calendar_adds_timed:
        # N8 (step E §3): "Calendar entries to add" is for tasks with only a date. The review
        # reads today's calendar only, so a task with a start time counts as handled here: its
        # entry was added when it was saved, and a failed add was said in that confirmation.
        marked_ids = marked_ids | frozenset(item.record.id for item in items if has_start_time(item.record))

    return ReviewInputs(
        items=items,
        calendar_events=calendar_events,
        calendar_marked_ids=marked_ids,
        open_requests=open_requests,
        history=history,
        source_status=source_status,
        now=now,
        facts=facts,
        open_notes=open_notes,
        preferences=tuple(preferences),
        ranking=ranking,
        project_tasks=project_tasks,
    )


def collect_morning_inputs(
    sources: ReviewSources, history: tuple[HistoryRow, ...], now: datetime, *, ranking: str = RANKING_POINTS,
    previous_review_at: datetime | None = None,
    bootstrap: "MorningBootstrap | None" = None, shown_item_refs: frozenset[str] = frozenset(),
) -> MorningInputs:
    """Work-02's complete item snapshot, calendar, requests and preferences; no marker scan.

    Old facts, first-step notes and sitreps are not read for the new daily flow.
    Done parts remain in the item snapshot so the sole reader can compute progress.
    ``collect_review_inputs`` stays available for the work-01 API and orientation.
    ``previous_review_at`` comes from the successful review run log, never an assumed clock.
    Bootstrap and actually-shown references are explicit host facts; raw items keep their pins.
    """
    from brain_core.timewords import SINGAPORE

    today = today_in_singapore(now)
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=SINGAPORE)
    items, brain_status = _read(sources.brain_items)
    events, calendar_status = _read(sources.calendar_events, day_start, day_start + timedelta(days=1))
    requests, request_status = _read(sources.open_requests)
    preferences, preference_status = _read(sources.brain_preferences)
    status = {"brain": brain_status, "calendar": calendar_status, "requests": request_status}
    if preference_status not in (None, "not_configured"):
        status["preferences"] = preference_status
    if sources.brain_home_size is not None:
        size = brain_home_size_line(sources.brain_home_size, status)
        if size is not None:
            status[BRAIN_HOME_SIZE_SOURCE] = size
    from brain_core import work

    # Map old physical references using raw lineage, preserving the sole reader's
    # complete snapshot and current raw path/hash (no mapped Record clones).
    aliases = {ref: work.first_id(entry.record) for entry in items
               for ref in (entry.record.id, *entry.record.supersedes)}
    shown_first_ids = frozenset(aliases.get(ref, ref) for ref in shown_item_refs)
    emails = ()
    if sources.important_emails is not None:
        emails, email_status = _read(sources.important_emails, now, previous_review_at, history)
        if email_status is not None:
            status["important email"] = email_status
    return MorningInputs(tuple(items), tuple(events), tuple(requests), history, status, now,
                         preferences=tuple(preferences), ranking=ranking, previous_review_at=previous_review_at,
                         bootstrap=bootstrap, shown_first_ids=shown_first_ids, important_emails=tuple(emails))


def brain_home_size_line(reader: Callable[[], Any], source_status: dict[str, str | None]) -> str | None:
    """The "Brain Home size" line's words, or None when every scan is under half its limit.

    The fullest scan's sentence alone (Brain Core's `capacity.worst`): scans that read several kinds
    together count the same records again, so a count of others would mislead; the size check lists
    them all. Left out when a read in this same review already said the same sentence (a scan past
    its limit), so it isn't said twice. A failed size read is said instead."""

    from brain_core.capacity import worst

    entries, status = _read(reader)
    if status is not None:
        return status
    fullest = worst(entries)
    if fullest is None or fullest.sentence in source_status.values():
        return None
    return fullest.sentence


def read_open_note_list(sources: ReviewSources) -> tuple["tuple[OpenNote, ...] | None", str | None]:
    """Every open note, or `None` and why when they couldn't be read (`not_configured` when no
    reader was given). A failing reader never stops the review or a return (§3)."""

    notes, status = _read(sources.open_notes)
    return (None, status) if status is not None else (tuple(notes), None)


def _moment_candidate_paths(history: tuple[HistoryRow, ...], today: date) -> frozenset[str]:
    """Only the records named in today's picks and waiting items (§4), not the whole store."""

    paths: set[str] = set()
    for row in history:
        if row.record_path is None:
            continue
        if row.response == "picked_today" and today_in_singapore(row.responded_at or row.surfaced_at) == today:
            paths.add(row.record_path)
        elif row.record_state in _WAITING_LIKE_STATES:
            paths.add(row.record_path)
    return frozenset(paths)


def collect_moments_candidates(
    sources: ReviewSources,
    history: tuple[HistoryRow, ...],
    now: datetime,
) -> tuple[tuple[ItemRecord, ...], str | None]:
    """Full raw snapshot for the sole reader, only when a waiting thing was shown.

    Historical shown paths can point at archived versions. Current lineage and parents
    must come from the full snapshot; returnable selects what became actionable.
    """
    if not any(row.record_state in _WAITING_LIKE_STATES for row in history):
        return (), None
    return _read(sources.brain_items)


def collect_moments_requests(
    sources: ReviewSources,
    history: tuple[HistoryRow, ...],
) -> tuple[tuple[RequestStatus, ...], str | None]:
    """The current state of the GT requests Nic was shown as open and not yet told are done
    (§12, ROA-21) - and only those: with none shown, nothing is read."""

    request_ids = requests_to_check(history)
    if not request_ids:
        return (), None
    return _read(sources.request_status, request_ids)
