"""Step F's two asks (Ciel rebuild slice 4): "where am I?" (`overview`) and "what next?"
(`what_next`), their shared `rank` (points, Ciel and Brain Home PRD §5; the nine rules while
`RANKING_RULES` is kept as the way back), and the pure helpers `run.py` and GT reuse. The points
engine's own tests are in test_points.py."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date, datetime, timedelta
import pytest

from brain_core.orientation import (
    FOCUS_PROJECT_TASK_CAP,
    IN_PROGRESS_NAMED_AT,
    NO_DATE_YET_MORE_REF,
    OVERVIEW_CHANNEL,
    OVERVIEW_SECTIONS,
    OVERVIEW_TITLES,
    SECTION_FINISHED_THIS_WEEK,
    SECTION_FOCUS_PROJECTS,
    SECTION_HIGHEST,
    SECTION_IN_PROGRESS,
    SECTION_NO_DATE_YET,
    SECTION_PICKED_FOR_TODAY,
    SECTION_ROUTINES,
    OrientationInputs,
    OrientationSignal,
    Overview,
    caution,
    focus_project_ids,
    focus_projects,
    in_progress,
    overview,
    project_task_ref,
    rank,
    same_project,
    surfaced_unchanged,
    tends_to_stall,
    time_box,
    what_next,
)
from brain_core.records import RECORD_SCHEMA_V2, Evidence, Record
from brain_core.review import (
    COMING_UP_CAP,
    COMING_UP_LATER_REF,
    COMING_UP_MORE_REF,
    COMING_UP_RULE,
    PICK_OR_PARK_CAP,
    RANKING_POINTS,
    RANKING_RULES,
    SECTION_COMING_UP,
    SECTION_COULD_NOT_SEE,
    SECTION_MISSED,
    SECTION_NEW_SUGGESTIONS,
    SECTION_TODAY,
    SECTION_WAITING_ON,
    SECTION_WINDOWS_CLOSING,
    CalendarEvent,
    HistoryRow,
    ItemRecord,
    OpenNote,
    OpenRequest,
    ReviewInputs,
)
from brain_core.sitreps import ProjectTasks, TaskSitrep
from brain_core.timewords import SINGAPORE

NOW = datetime(2026, 10, 4, 14, 0, 0, tzinfo=SINGAPORE)  # Sun 4 Oct 2026, 14:00 +08:00 (spec §9)
TODAY = date(2026, 10, 4)


@pytest.mark.parametrize("days_ago, word", [(0, "today"), (1, "yesterday")])
def test_timestamp_day_words_before_4am_use_nics_personal_day(days_ago, word):
    now = datetime(2026, 10, 4, 3, 5, tzinfo=SINGAPORE)
    stamped = now.replace(minute=0) - timedelta(days=days_ago)
    plain = item("early-plain", title="Buy light bulbs", created_at=stamped)
    started = item("early-started", title="Write the docs", created_at=stamped)
    task = sitrep("early-task", updated_at=stamped)
    picture = overview(inputs(
        plain, started, now=now, open_notes=(note(started, updated_at=stamped),),
        preferences=(focus_preference("early-focus", [GTBC]),),
        project_tasks=(summary(GTBC, task, as_of=stamped),),
    ), now)
    lines = {line.item_ref: line for line in picture.lines}
    assert f"last changed {word}" in lines[plain.id].text
    assert lines[plain.id].reasons == (f"no date or stake yet; saved {word}",)
    assert f"last changed {word}" in lines[started.id].text
    assert f"last changed {word}" in lines[project_task_ref(task)].text
    assert f"summary as of {word} at 3 am" in lines["project:" + GTBC].text

    # Explicit appointment and deadline dates stay on their calendar day.
    appointed = item("early-appointment", due="2026-10-04", at="09:00")
    deadline = item("early-deadline", due="2026-10-04", owed_to="Sam")
    dated = overview(inputs(appointed, deadline, now=now), now)
    assert any("tomorrow" in line.text and line.item_ref == appointed.id for line in dated.lines)
    assert any("tomorrow" in line.text and line.item_ref == deadline.id for line in dated.lines)

EVIDENCE = (
    Evidence(source_type="session_message", locator="session:test:1", content_sha256="a" * 64, excerpt="fixture"),
)


def owner_source() -> dict[str, object]:
    return {"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"}


def item(
    id_suffix: str,
    *,
    title: str = "an item",
    level: str = "accepted",
    state: str = "open",
    owed_to: str | None = None,
    repeat: str | None = None,
    due: str | None = None,
    at: str | None = None,
    window: dict[str, str] | None = None,
    padded_minutes: int | None = None,
    resolved_from: dict[str, str] | None = None,
    links: tuple[dict[str, str], ...] = (),
    created_at: datetime = NOW,
    updated_at: datetime | None = None,
    size: str = "task",
    importance: dict[str, object] | None = None,
    check: dict[str, str] | None = None,
    supersedes: tuple[str, ...] = (),
    source: dict[str, object] | None = None,
) -> Record:
    timing: dict[str, object] = {}
    if due is not None:
        timing["due"] = due
    if at is not None:
        timing["at"] = at
    if window is not None:
        timing["window"] = window
    if padded_minutes is not None:
        timing["padded_minutes"] = padded_minutes
    if resolved_from is not None:
        timing["resolved_from"] = resolved_from
    data: dict[str, object] = {"level": level, "size": size, "timing": timing}
    if owed_to is not None:
        data["owed_to"] = owed_to
    if repeat is not None:
        data["repeat"] = repeat
    if importance is not None:
        data["importance"] = importance
    if check is not None:
        data["check"] = check
    return Record(
        schema=RECORD_SCHEMA_V2,
        id=f"brn_20261004_120000_{id_suffix}",
        kind="item",
        title=title,
        state=state,
        created_at=created_at,
        updated_at=updated_at or created_at,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=EVIDENCE,
        supersedes=supersedes,
        dedupe_key=None,
        data=data,
        body="",
        source=source or owner_source(),
        links=links,
    )


def focus_preference(id_suffix: str, projects: list[str], *, updated_at: datetime = NOW) -> Record:
    return Record(
        schema=RECORD_SCHEMA_V2,
        id=f"brn_20261004_120000_{id_suffix}",
        kind="preference",
        title="Focus projects",
        state="active",
        created_at=updated_at,
        updated_at=updated_at,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=EVIDENCE,
        supersedes=(),
        dedupe_key=None,
        data={"focus_projects": projects},
        body="",
        source=owner_source(),
        links=(),
    )


def note(record: Record | None = None, *, session_id: str | None = None, next_step: str = "open the file",
         updated_at: datetime = NOW, task_id: str | None = None) -> OpenNote:
    return OpenNote(
        session_id=session_id or f"item-{record.id}",
        next_step=next_step,
        updated_at=updated_at,
        task_id=task_id if task_id is not None else (record.id if record is not None else None),
    )


def wrap(*records: Record) -> tuple[ItemRecord, ...]:
    return tuple(ItemRecord(record=record, path=f"records/item/2026/10/{record.id}.md") for record in records)


def review_inputs(
    *records: Record,
    calendar_events: tuple[CalendarEvent, ...] = (),
    open_requests: tuple[OpenRequest, ...] = (),
    history: tuple[HistoryRow, ...] = (),
    source_status: dict[str, str | None] | None = None,
    open_notes: tuple[OpenNote, ...] | None = None,
    now: datetime = NOW,
    ranking: str = RANKING_POINTS,
) -> ReviewInputs:
    return ReviewInputs(
        items=wrap(*records),
        calendar_events=calendar_events,
        calendar_marked_ids=frozenset(),
        open_requests=open_requests,
        history=history,
        source_status=source_status or {},
        now=now,
        open_notes=open_notes,
        ranking=ranking,
    )


def inputs(
    *records: Record,
    picked_today: frozenset[str] = frozenset(),
    preferences: tuple[Record, ...] = (),
    project_tasks: tuple[ProjectTasks, ...] | None = None,
    **review_kwargs: object,
) -> OrientationInputs:
    return OrientationInputs(
        review=review_inputs(*records, **review_kwargs),
        picked_today=picked_today,
        preferences=preferences,
        project_tasks=project_tasks,
    )


def history_row(record: Record, *, surfaced_at: datetime, sha256: str | None = None, channel: str = "orient") -> HistoryRow:
    return HistoryRow(
        item_ref=record.id,
        channel=channel,
        rule="oldest open",
        record_sha256=sha256 if sha256 is not None else record.sha256,
        record_state=record.state,
        surfaced_at=surfaced_at,
    )


def sitrep(
    session_id: str,
    *,
    project: str = "vault_gtbc_plans",
    intent: str = "Build slice 8",
    state: str = "open",
    next_step: str = "Write the tests",
    blockers: tuple[str, ...] = (),
    updated_at: datetime = NOW,
    sha: str = "c" * 64,
) -> TaskSitrep:
    """One project task, as the hub's task sitrep holds it (b-project-sitreps.md)."""

    return TaskSitrep(
        project=project, session_id=session_id, intent=intent, state=state, next_step=next_step, blockers=blockers,
        note_updated_at=updated_at, note_record_id="brn_20261004_120000_ab12", note_content_sha256=sha,
    )


def summary(project: str, *tasks: TaskSitrep, as_of: datetime | None = NOW) -> ProjectTasks:
    return ProjectTasks(project=project, as_of=as_of, tasks=tasks)


def top(answer) -> str:
    return answer.top_pick.record_id


def below_top_line(answer: Overview) -> Overview:
    """The sections below "Highest right now", for tests about those sections; the top line has
    tests of its own."""

    return Overview(
        lines=tuple(line for line in answer.lines if line.section != SECTION_HIGHEST),
        sections=tuple(name for name in answer.sections if name != SECTION_HIGHEST),
    )


def order(ranked) -> list[str]:
    return [candidate.ref for candidate in ranked]


# --- Rule 1: overdue, or due or closing today ----------------------------------------------------


def test_rule_1_due_today_beats_a_far_off_task():
    due_today = item("a1", title="pay the bill", due="2026-10-04")
    far_off = item("a2", title="buy storage boxes", due="2026-10-20")
    answer = what_next(inputs(due_today, far_off), NOW)
    assert top(answer) == due_today.id
    assert "due today" in answer.top_pick.rules
    assert answer.top_pick.fields_used["due"] == "2026-10-04"


def test_rule_1_an_overdue_date_says_when_it_was_due():
    overdue = item("a1", title="overdue task", due="2026-10-01")
    later = item("a2", title="later task", due="2026-10-20")
    answer = what_next(inputs(overdue, later), NOW)
    assert top(answer) == overdue.id
    assert answer.top_pick.rules == ("was due Thu 1 Oct",)


def test_rule_1_window_closing_today_matches_without_a_due_date():
    closing = item("a1", title="call the bank", window={"start": "2026-10-01", "end": "2026-10-04"})
    later = item("a2", title="later task", due="2026-10-20")
    answer = what_next(inputs(closing, later), NOW)
    assert top(answer) == closing.id
    assert "due today" in answer.top_pick.rules
    assert answer.top_pick.fields_used["due"] == "2026-10-04"


def test_rule_1_a_window_that_already_closed_is_overdue():
    closed = item("a1", title="renew the pass", window={"start": "2026-09-28", "end": "2026-10-02"})
    later = item("a2", title="later task", due="2026-10-20", created_at=NOW - timedelta(days=30))
    answer = what_next(inputs(closed, later), NOW)
    assert top(answer) == closed.id
    assert answer.top_pick.rules == ("was due Fri 2 Oct",)


# --- The nine rules, in order (step F §3; redesign default 1) -----------------------------------
# Kept behind `[orientation] ranking = "rules"` until a week of real use (Accepted - Nic, 28 Sep
# 2026: "Yes, keep a way back"), so these tests pin that ranking.


def test_each_rule_outranks_every_rule_below_it():
    """Nine candidates, each matching exactly one rule, oldest last: the rule decides, not age."""

    goal = item("g0", title="move into robotics", size="goal", state="waiting")
    blocker_target = item("r6", title="book the venue", created_at=NOW - timedelta(days=1))
    candidates = (
        item("r9", title="everything else", created_at=NOW - timedelta(days=400)),
        item("r8", title="research roles", links=({"type": "part_of", "target": goal.id},), created_at=NOW - timedelta(days=300)),
        item("r7", title="laundry", repeat="weekly:sun", created_at=NOW - timedelta(days=200)),
        blocker_target,
        item("r5", title="exosuit task", links=({"type": "about", "target": "vault_exosuit"},), created_at=NOW - timedelta(days=100)),
        item("r4", title="book flights", importance={"level": "high", "reason": "the fare doubles", "proposed_by": "hermes-wa", "confirmed": True}, created_at=NOW - timedelta(days=90)),
        item("r3", title="send Sam the deck", owed_to="Sam", due="2026-10-06", created_at=NOW - timedelta(days=80)),
        item("r2", title="picked task", created_at=NOW - timedelta(days=70)),
        item("r1", title="pay the bill", due="2026-10-04", created_at=NOW - timedelta(days=1)),
    )
    dependent = item("d6", title="send invites", state="blocked", links=({"type": "blocked_by", "target": blocker_target.id},))
    ranked = rank(
        wrap(*candidates, goal, dependent), TODAY,
        picked_today=frozenset({candidates[7].id}), focus_projects=frozenset({"vault_exosuit"}), ranking=RANKING_RULES,
    )
    names = [candidate.item.record.title for candidate in ranked]
    assert names[:9] == [
        "pay the bill", "picked task", "send Sam the deck", "exosuit task", "book the venue",
        "laundry", "research roles", "everything else", "book flights",
    ]
    reasons = {candidate.item.record.title: candidate.rules for candidate in ranked}
    assert reasons["pay the bill"] == ("due today",)
    assert reasons["picked task"] == ("you picked it for today",)
    assert reasons["send Sam the deck"] == ("Sam is waiting; due Tue 6 Oct",)
    assert reasons["book flights"] == ("oldest open",)  # Retired importance contributes nothing.
    assert reasons["exosuit task"] == ("focus project: vault_exosuit",)
    assert reasons["book the venue"] == ("1 thing is waiting on this",)
    assert reasons["laundry"] == ("every Sunday, due today",)
    assert reasons["research roles"] == ('part of "move into robotics"',)
    assert reasons["everything else"] == ("oldest open",)


def test_tie_on_rule_1_is_broken_by_rule_2_picked_today():
    due_today_a = item("a1", title="A", due="2026-10-04", created_at=NOW - timedelta(days=5))
    due_today_b = item("a2", title="B", due="2026-10-04", created_at=NOW - timedelta(days=1))
    answer = what_next(inputs(due_today_a, due_today_b, picked_today=frozenset({due_today_b.id})), NOW)
    assert top(answer) == due_today_b.id
    assert set(answer.top_pick.rules) == {"due today", "you picked it for today"}


def test_a_pick_made_before_the_task_changed_still_counts():
    """The morning pick names the version Nic saw; a later change writes a new version (G4)."""

    old = item("a1", title="call the bank", created_at=NOW - timedelta(hours=8))
    new = item("a2", title="call the bank", supersedes=(old.id,), created_at=NOW - timedelta(hours=1))
    other = item("a3", title="older task", created_at=NOW - timedelta(days=30))
    answer = what_next(inputs(old, new, other, picked_today=frozenset({old.id})), NOW)
    assert top(answer) == old.id
    assert answer.top_pick.rules == ("you picked it for today",)


def test_rule_3_does_not_match_when_due_is_more_than_3_days_out():
    promise = item("a1", title="send Sam the deck", owed_to="Sam", due="2026-10-20")
    plain = item("a2", title="older task", created_at=NOW - timedelta(days=30))
    answer = what_next(inputs(promise, plain, ranking=RANKING_RULES), NOW)
    assert top(answer) == plain.id


def test_rule_3_someone_waiting_due_today_says_today():
    promise = item("a1", title="send Sam the deck", owed_to="Sam", due="2026-10-04")
    assert rank(wrap(promise), TODAY, ranking=RANKING_RULES)[0].rules == ("due today", "Sam is waiting; due today")


def test_rule_4_counts_only_importance_nic_confirmed():
    proposed = item("a1", title="proposed high", importance={"level": "high", "reason": "slips", "proposed_by": "hermes-wa", "confirmed": False}, created_at=NOW - timedelta(days=1))
    older = item("a2", title="older task", created_at=NOW - timedelta(days=30))
    confirmed = item("a3", title="confirmed high", importance={"level": "high", "reason": "missing it loses the booking", "proposed_by": "hermes-wa", "confirmed": True})
    assert order(rank(wrap(proposed, older), TODAY, ranking=RANKING_RULES)) == [older.id, proposed.id]
    ranked = rank(wrap(proposed, older, confirmed), TODAY, ranking=RANKING_RULES)
    # Work-02 retires importance even in the fallback; the same up filter applies.
    assert order(ranked) == [older.id, proposed.id, confirmed.id]
    assert all("importance" not in candidate.fields_used for candidate in ranked)


def test_rule_4_without_a_reason_still_says_high_importance():
    confirmed = item("a1", title="confirmed high", importance={"level": "high", "reason": "", "proposed_by": "nic", "confirmed": True})
    assert rank(wrap(confirmed), TODAY, ranking=RANKING_RULES)[0].rules == ("oldest open",)


def test_rule_5_part_of_or_about_a_focus_project_named_in_nics_newest_list():
    part = item("a1", title="exosuit part", links=({"type": "part_of", "target": "vault_exosuit"},), created_at=NOW - timedelta(days=1))
    about = item("a2", title="job search task", links=({"type": "about", "target": "job-search"},), created_at=NOW - timedelta(days=2))
    elsewhere = item("a3", title="other project", links=({"type": "about", "target": "vault_other"},), created_at=NOW - timedelta(days=90))
    older_list = focus_preference("p1", ["vault_other"], updated_at=NOW - timedelta(days=10))
    newest_list = focus_preference("p2", ["vault_exosuit", "job-search"], updated_at=NOW - timedelta(days=1))
    assert focus_project_ids((older_list, newest_list)) == frozenset({"vault_exosuit", "job-search"})
    answer = what_next(inputs(part, about, elsewhere, preferences=(older_list, newest_list), ranking=RANKING_RULES), NOW)
    assert [top(answer), *(alt.record_id for alt in answer.alternatives)] == [about.id, part.id, elsewhere.id]
    assert answer.top_pick.rules == ("focus project: job-search",)
    assert answer.alternatives[1].rules == ("oldest open",)


def test_no_focus_preference_means_no_focus_projects():
    assert focus_project_ids(()) == frozenset()


def test_rule_6_other_items_waiting_on_this_one_counts_links_to_older_versions():
    old = item("a0", title="clear the storeroom v1", state="open", created_at=NOW - timedelta(days=3))
    blocker = item("a1", title="clear the storeroom", supersedes=(old.id,), created_at=NOW - timedelta(days=1))
    by_new = item("a2", title="buy storage boxes", links=({"type": "waiting_on", "target": blocker.id},), created_at=NOW - timedelta(days=10))
    by_old = item("a3", title="list the boxes", state="blocked", links=({"type": "blocked_by", "target": old.id},), created_at=NOW - timedelta(days=10))
    answer = what_next(inputs(old, blocker, by_new, by_old), NOW)
    assert top(answer) == old.id
    assert answer.top_pick.rules == ('"list the boxes" is waiting on this',)
    assert answer.top_pick.fields_used["blocked_count"] == 1


def test_rule_7_routine_on_another_weekday_or_not_weekly_does_not_match():
    monday = item("a1", title="laundry", repeat="weekly:mon", created_at=NOW - timedelta(days=1))
    daily = item("a2", title="stretch", repeat="daily", created_at=NOW - timedelta(days=2))
    unknown_day = item("a3", title="odd routine", repeat="weekly:someday", created_at=NOW - timedelta(days=3))
    older = item("a4", title="older plain task", created_at=NOW - timedelta(days=60))
    assert order(rank(wrap(monday, daily, unknown_day, older), TODAY, ranking=RANKING_RULES)) == [
        older.id, unknown_day.id, daily.id, monday.id,
    ]


def test_rule_8_does_not_match_a_part_of_link_to_a_non_goal_but_follows_a_changed_goal():
    non_goal = item("a1", title="the deck", created_at=NOW - timedelta(days=5))
    part_of_task = item("a2", title="slide 6", links=({"type": "part_of", "target": non_goal.id},), created_at=NOW - timedelta(days=1))
    old_goal = item("g1", title="clear the house v1", size="goal", created_at=NOW - timedelta(days=40))
    goal = item("g2", title="clear the house", size="goal", supersedes=(old_goal.id,), state="waiting", created_at=NOW - timedelta(days=4))
    part_of_goal = item("a3", title="buy boxes", links=({"type": "part_of", "target": old_goal.id},), created_at=NOW - timedelta(days=2))
    older = item("a4", title="older plain task", created_at=NOW - timedelta(days=60))
    ranked = rank(wrap(non_goal, part_of_task, goal, part_of_goal, older), TODAY, ranking=RANKING_RULES)
    assert ranked[0].item.record.id == part_of_goal.id
    assert ranked[0].rules == ('part of "clear the house"',)
    assert [c.rules for c in ranked if c.item.record.id == part_of_task.id] == [("oldest open",)]


def test_rule_9_is_oldest_first_then_record_id():
    older = item("a1", title="older", created_at=NOW - timedelta(days=10))
    newer = item("a2", title="newer", created_at=NOW - timedelta(days=1))
    same_time = item("z9", title="same time as newer", created_at=NOW - timedelta(days=1))
    assert order(rank(wrap(same_time, newer, older), TODAY)) == [older.id, newer.id, same_time.id]


# --- Started beats new (N10a) --------------------------------------------------------------------


def test_started_beats_new_at_the_same_level():
    new = item("a1", title="new task", created_at=NOW - timedelta(days=20))
    started = item("a2", title="started task", created_at=NOW - timedelta(days=1))
    started = started_fact(started)
    answer = what_next(inputs(new, started, open_notes=(note(session_id="deck-for-sam", task_id=started.id),)), NOW)
    assert top(answer) == started.id
    assert answer.top_pick.rules == ("already started",)
    assert answer.top_pick.fields_used["started"] is True
    assert answer.alternatives[0].record_id == new.id


def test_started_never_beats_a_higher_rule():
    started = item("a1", title="started task", created_at=NOW - timedelta(days=20))
    due_today = item("a2", title="new, due today", due="2026-10-04")
    started = started_fact(started)
    answer = what_next(inputs(started, due_today, open_notes=(note(started),)), NOW)
    assert top(answer) == due_today.id
    assert answer.alternatives[0].rules == ("already started",)


def test_a_handed_off_note_does_not_make_a_task_started():
    """A `gt-<request_id>` note holds another agent's steps (G2): not an actual person's own start."""

    older = item("a1", title="older task", created_at=NOW - timedelta(days=20))
    handed = item("a2", title="handed off", created_at=NOW - timedelta(days=1))
    answer = what_next(inputs(older, handed, open_notes=(note(session_id="gt-req_1", task_id=handed.id),)), NOW)
    assert top(answer) == older.id
    assert answer.alternatives[0].rules == ("no date or stake yet; saved yesterday",)
    assert "started" not in answer.alternatives[0].fields_used


# --- Only the newest version counts (G4) ---------------------------------------------------------


def test_only_the_newest_version_of_a_changed_task_counts():
    old = item("a1", title="send the deck", due="2026-10-10", created_at=NOW - timedelta(days=3))
    new = item("a2", title="send the deck", due="2026-10-04", supersedes=(old.id,), created_at=NOW - timedelta(hours=1))
    other = item("a3", title="other", created_at=NOW - timedelta(days=1))
    answer = what_next(inputs(old, new, other), NOW)
    assert top(answer) == old.id
    assert [alt.record_id for alt in answer.alternatives] == [other.id]
    assert answer.can_wait == ()
    picture = overview(inputs(old, new, other), NOW)
    assert [line.item_ref for line in picture.lines if line.section == SECTION_TODAY] == [old.id]


# --- Ideas and suggestions are never candidates --------------------------------------------------


def test_ideas_and_suggestions_are_never_candidates():
    idea = item("a1", title="maybe learn piano", level="possible", due="2026-10-04")
    suggestion = item("a2", title="fleet-boss suggestion", level="suggested", due="2026-10-04")
    answer = what_next(inputs(idea, suggestion), NOW)
    assert answer.top_pick is None
    assert answer.not_enough.reason == "Nothing accepted and open right now."


# --- "What next?": a top pick and two alternatives (N2, case 5) ----------------------------------


def test_what_next_gives_a_top_pick_two_alternatives_and_what_can_wait():
    picks = [item(f"b{index}", title=f"task {index}", created_at=NOW - timedelta(days=10 - index)) for index in range(6)]
    waiting = item("w1", title="quote", state="waiting", owed_to="Ben", due="2026-10-04", created_at=NOW - timedelta(days=30))
    answer = what_next(inputs(*picks, waiting), NOW)
    assert top(answer) == picks[0].id
    assert answer.top_pick.plain_word == "task"
    assert [alt.record_id for alt in answer.alternatives] == [picks[1].id, picks[2].id]
    assert [alt.plain_word for alt in answer.alternatives] == ["task", "task"]
    assert [alt.rules for alt in answer.alternatives] == [
        ("no date or stake yet; saved Fri 25 Sep",), ("no date or stake yet; saved Sat 26 Sep",),
    ]
    # Waiting is excluded before ranking, including the ranked can-wait list.
    assert [entry.record_id for entry in answer.can_wait] == [picks[3].id, picks[4].id, picks[5].id]
    assert waiting.id not in {top(answer), *(a.record_id for a in answer.alternatives), *(a.record_id for a in answer.can_wait)}
    assert answer.not_enough is None


def test_fewer_open_items_give_fewer_alternatives():
    only = item("a1", title="the only open task")
    blocked = item("a2", title="visa form", state="blocked", links=({"type": "blocked_by", "target": "the embassy"},))
    answer = what_next(inputs(only, blocked), NOW)
    assert top(answer) == only.id
    assert answer.alternatives == ()
    assert answer.can_wait == ()


def test_can_wait_reasons_for_waiting_blocked_and_later_items():
    winner = item("a1", title="winner", due="2026-10-04")
    alt_one = item("a2", title="alt one", due="2026-10-04")
    alt_two = item("a3", title="alt two", due="2026-10-04")
    waiting = item("a4", title="waiting one", state="waiting", links=({"type": "waiting_on", "target": "Ben's pricing"},), created_at=NOW - timedelta(days=10))
    blocked = item("a5", title="blocked one", state="blocked", created_at=NOW - timedelta(days=9))
    later_due = item("a6", title="later due", due="2026-10-20", created_at=NOW - timedelta(days=8))
    answer = what_next(inputs(winner, alt_one, alt_two, waiting, blocked, later_due), NOW)
    reasons = {entry.record_id: entry.reason for entry in answer.can_wait}
    assert reasons == {
        later_due.id: "due Tue 20 Oct",
    }


def test_can_wait_reasons_for_windows_times_and_no_date():
    firsts = [item(f"f{index}", title=f"first {index}", due="2026-10-04") for index in range(3)]
    window_item = item("a1", title="window item", window={"start": "2026-10-01", "end": "2026-10-10"}, created_at=NOW - timedelta(days=1))
    timed = item("a2", title="timed", due="2026-10-20", at="00:15", created_at=NOW - timedelta(days=2))
    dateless = item("a3", title="dateless item", created_at=NOW - timedelta(days=3))
    answer = what_next(inputs(*firsts, window_item, timed, dateless), NOW)
    reasons = {entry.record_id: entry.reason for entry in answer.can_wait}
    assert reasons == {
        window_item.id: "due Sat 10 Oct",
        timed.id: "due Tue 20 Oct at 12:15 am",
        dateless.id: "no date set yet",
    }


def test_can_wait_says_so_when_it_is_also_due_or_overdue():
    firsts = [item(f"f{index}", title=f"first {index}", due="2026-10-04") for index in range(3)]
    also_due = item("a1", title="also due", due="2026-10-04", created_at=NOW + timedelta(minutes=1))
    overdue = item("a2", title="overdue", due="2026-10-01", created_at=NOW + timedelta(minutes=2))
    closes = item("a3", title="closes today", window={"start": "2026-10-01", "end": "2026-10-04"}, created_at=NOW + timedelta(minutes=3))
    picked = frozenset(entry.id for entry in firsts)
    answer = what_next(inputs(*firsts, also_due, overdue, closes, picked_today=picked), NOW)
    # The overdue one has the most points; the two due by the end of today tie, so the one saved
    # first comes first.
    assert [entry.reason for entry in answer.can_wait] == [
        "also overdue: was due Thu 1 Oct", "also due today", "also due today",
    ]
    closed = item("a4", title="closed", window={"start": "2026-09-28", "end": "2026-10-02"})
    answer = what_next(inputs(*firsts, closed, picked_today=picked), NOW)
    assert [entry.reason for entry in answer.can_wait] == ["also overdue: was due Fri 2 Oct"]


# --- not_enough and questions (N7) ---------------------------------------------------------------


def test_not_enough_with_no_candidates_says_so_plainly():
    answer = what_next(inputs(), NOW)
    assert answer.top_pick is None
    assert answer.alternatives == () and answer.can_wait == ()
    assert answer.not_enough.reason == "Nothing accepted and open right now."
    assert answer.not_enough.questions == ()


def test_not_enough_when_everything_is_waiting_asks_about_each_of_them():
    """N7: no limit on questions, and they may name vaults and tools."""

    waiting_one = item("a1", title="proposal", owed_to="Sam", state="waiting", due="2026-10-20", created_at=NOW - timedelta(days=5))
    waiting_two = item("a2", title="migration", state="waiting", links=({"type": "waiting_on", "target": "the vault migration tool"},))
    waiting_three = item("a3", title="refund", state="waiting", created_at=NOW - timedelta(days=1))
    answer = what_next(inputs(waiting_one, waiting_two, waiting_three), NOW)
    assert answer.top_pick is None
    assert answer.not_enough.reason == "Everything open right now is waiting on someone else."
    assert answer.not_enough.questions == (
        'Is "proposal" still waiting on Sam?',
        'Is "refund" still waiting on someone else?',
        'Is "migration" still waiting on the vault migration tool?',
    )


def test_not_enough_when_everything_open_is_blocked():
    blocked = item("a1", title="visa form", state="blocked", due="2026-10-04")
    answer = what_next(inputs(blocked), NOW)
    assert answer.not_enough.reason == "Everything open right now is blocked on something else."
    assert answer.not_enough.questions == ()


def test_not_enough_when_everything_open_is_waiting_or_blocked_asks_about_the_waiting_ones():
    waiting = item("a1", title="quote", state="waiting", owed_to="Ben", created_at=NOW - timedelta(days=2))
    blocked = item("a2", title="visa form", state="blocked", created_at=NOW - timedelta(days=9))
    answer = what_next(inputs(waiting, blocked), NOW)
    assert answer.not_enough.reason == "Everything open right now is waiting on someone or blocked on something."
    assert answer.not_enough.questions == ('Is "quote" still waiting on Ben?',)


def test_not_enough_when_brain_home_could_not_be_read():
    answer = what_next(inputs(source_status={"brain": "GitHubError: 503"}), NOW)
    assert answer.top_pick is None
    assert answer.not_enough.reason == "Couldn't see your saved items, so there's nothing safe to recommend yet."
    assert answer.unknowns == ("brain: GitHubError: 503",)


def test_not_enough_when_brain_home_is_not_configured_even_with_other_sources_fine():
    answer = what_next(inputs(source_status={"brain": "not_configured", "calendar": None}), NOW)
    assert answer.not_enough.reason == "Couldn't see your saved items, so there's nothing safe to recommend yet."


def test_an_unreadable_calendar_or_notes_source_does_not_stop_a_pick():
    """Case 14: with the notes out of reach it still answers, and says what it couldn't see."""

    task = item("a1", title="send the deck", due="2026-10-04")
    answer = what_next(inputs(task, source_status={"brain": None, "calendar": "token_revoked", "notes": "connector unreachable"}), NOW)
    assert top(answer) == task.id
    assert answer.top_pick.first_step == ""
    assert answer.unknowns == ("calendar: token_revoked", "notes: connector unreachable")


# --- First step: from the task's own note (N12b), never invented -----------------------------------


def test_first_step_comes_from_the_note_linked_to_the_task_whatever_it_is_called():
    task = item("a1", title="send the deck", due="2026-10-04")
    answer = what_next(inputs(task, open_notes=(note(session_id="deck-for-sam", task_id=task.id, next_step="  drop the pricing into slide 6 "),)), NOW)
    assert answer.top_pick.first_step == ""  # Historical personal notes no longer supply actions.


def test_first_step_comes_from_the_older_item_key_too():
    task = item("a1", title="send the deck", due="2026-10-04")
    older_key = OpenNote(session_id=f"item-{task.id}", next_step="open the deck", updated_at=NOW)
    assert what_next(inputs(task, open_notes=(older_key,)), NOW).top_pick.first_step == ""


def test_first_step_is_empty_without_a_note_and_never_from_handed_off_work():
    task = item("a1", title="send the deck", due="2026-10-04")
    assert what_next(inputs(task), NOW).top_pick.first_step == ""
    handed = note(session_id="gt-req_1", task_id=task.id, next_step="the other agent's step")
    assert what_next(inputs(task, open_notes=(handed,)), NOW).top_pick.first_step == ""


def test_a_sorted_tasks_first_step_is_the_picks_first_step_until_a_note_moves_on():
    """PRD §4.1: a next action is sorted with its first step; once started, its note's step wins."""

    task = item("a1", title="call the accountant", due="2026-10-04")
    task = replace(task, data={**task.data, "block": "admin", "first_step": "Find their number"})
    assert what_next(inputs(task), NOW).top_pick.first_step == ""  # Old first_step is agent wording.
    started = note(task, next_step="Ask about the Q3 filing")
    assert what_next(inputs(task, open_notes=(started,)), NOW).top_pick.first_step == ""


# --- Stall detection and the 20-minute time box (N3) ---------------------------------------------


def test_stall_detection_at_one_earlier_day_is_not_enough():
    task = item("a1", title="stuck task", due="2026-10-04", padded_minutes=90)
    assert tends_to_stall(task, (history_row(task, surfaced_at=NOW - timedelta(days=1)),)) is False


def test_stall_detection_at_two_earlier_days_unchanged():
    task = item("a1", title="stuck task", due="2026-10-04", padded_minutes=90)
    history = (
        history_row(task, surfaced_at=NOW - timedelta(days=1)),
        history_row(task, surfaced_at=NOW - timedelta(days=2), channel="review"),
    )
    assert tends_to_stall(task, history) is True


def test_stall_detection_resets_once_the_record_changed():
    task = item("a1", title="stuck task", due="2026-10-04", padded_minutes=90)
    history = (
        history_row(task, surfaced_at=NOW - timedelta(days=1)),
        history_row(task, surfaced_at=NOW - timedelta(days=2), sha256="stale" * 16),
    )
    assert tends_to_stall(task, history) is False


def test_rows_where_am_i_wrote_never_count_towards_stalling():
    """Redesign default 11: "where am I?" logs what it showed, but only being recommended or
    brought back counts."""

    task = item("a1", title="stuck task", due="2026-10-04")
    history = (
        history_row(task, surfaced_at=NOW - timedelta(days=1), channel=OVERVIEW_CHANNEL),
        history_row(task, surfaced_at=NOW - timedelta(days=2), channel=OVERVIEW_CHANNEL),
        history_row(task, surfaced_at=NOW - timedelta(days=3)),
    )
    assert tends_to_stall(task, history) is False
    assert what_next(inputs(task, history=history), NOW).top_pick.time_box_minutes is None


def test_time_box_is_20_minutes_or_the_shorter_padded_estimate():
    assert time_box(item("a1", padded_minutes=90), True) == 20
    assert time_box(item("a1", padded_minutes=10), True) == 10
    assert time_box(item("a1"), True) == 20
    assert time_box(item("a1", padded_minutes=90), False) is None


def test_a_stalling_top_pick_gets_a_time_box_not_a_plan():
    task = item("a1", title="stuck task", due="2026-10-04", padded_minutes=90)
    history = (
        history_row(task, surfaced_at=NOW - timedelta(days=1)),
        history_row(task, surfaced_at=NOW - timedelta(days=2)),
    )
    answer = what_next(inputs(task, history=history), NOW)
    assert answer.top_pick.time_box_minutes == 20
    assert answer.top_pick.first_step == ""
    assert answer.top_pick.estimate_minutes == 90


def test_a_surfacing_from_earlier_today_does_not_count_toward_stalling():
    task = item("a1", title="stuck task", due="2026-10-04", padded_minutes=90)
    history = (
        history_row(task, surfaced_at=NOW - timedelta(days=1)),
        history_row(task, surfaced_at=NOW - timedelta(hours=2)),
    )
    assert what_next(inputs(task, history=history), NOW).top_pick.time_box_minutes is None


# --- Fitting the moment (calendar_gap) -------------------------------------------------------------


def test_calendar_gap_reason_shows_whether_the_estimate_fits():
    fits = item("a1", title="quick email", due="2026-10-04", padded_minutes=30)
    answer = what_next(inputs(fits), NOW, signal=OrientationSignal(kind="calendar_gap", gap_minutes=45))
    assert answer.top_pick.fields_used["gap_minutes"] == 45
    assert answer.top_pick.fields_used["fits_gap"] is True


def test_calendar_gap_still_picks_a_task_that_does_not_fit():
    long_task = item("a1", title="long task", due="2026-10-04", padded_minutes=90)
    answer = what_next(inputs(long_task), NOW, signal=OrientationSignal(kind="calendar_gap", gap_minutes=20))
    assert top(answer) == long_task.id
    assert answer.top_pick.fields_used["fits_gap"] is False


def test_calendar_gap_reason_omits_fits_gap_without_an_estimate():
    task = item("a1", title="an unpadded task", due="2026-10-04")
    answer = what_next(inputs(task), NOW, signal=OrientationSignal(kind="calendar_gap", gap_minutes=45))
    assert answer.top_pick.fields_used["gap_minutes"] == 45
    assert "fits_gap" not in answer.top_pick.fields_used


def test_pc_on_signal_has_no_gap_fields():
    task = item("a1", title="a task", due="2026-10-04")
    answer = what_next(inputs(task), NOW, signal=OrientationSignal(kind="pc_on"))
    assert "gap_minutes" not in answer.top_pick.fields_used


# --- The caution rule (§6) -------------------------------------------------------------------------


def test_caution_fires_for_a_due_date_or_window_closing_today_only():
    assert caution(item("a1", title="pay the electricity bill", due="2026-10-04"), TODAY) == (
        'Noted. "pay the electricity bill" is still due today.'
    )
    assert caution(item("a1", title="call the bank", window={"start": "2026-10-01", "end": "2026-10-04"}), TODAY) == (
        'Noted. "call the bank" is still due today.'
    )
    assert caution(item("a1", title="buy storage boxes", due="2026-10-20"), TODAY) is None
    assert caution(item("a1", title="buy storage boxes"), TODAY) is None


# --- Assumptions from step C's resolved_from --------------------------------------------------------


def test_assumption_shows_what_a_time_word_was_taken_to_mean():
    task = item("a1", title="send the deck", due="2026-10-02", resolved_from={"words": "Friday", "today": "2026-09-26"})
    other = item("a2", title="older task", created_at=NOW - timedelta(days=30))
    answer = what_next(inputs(task, other, picked_today=frozenset({task.id})), NOW)
    assert top(answer) == task.id
    assert answer.assumptions == ("took 'Friday' to mean 2026-10-02",)


def test_no_assumption_without_resolved_from():
    assert what_next(inputs(item("a1", title="send the deck", due="2026-10-04")), NOW).assumptions == ()


# --- A due time of day shows in the reason, never in the rank (§3) ---------------------------------


def test_due_today_with_a_time_says_the_time_whether_ahead_or_just_passed():
    ahead = item("a1", title="call the bank", due="2026-10-04", at="17:00")  # NOW is 14:00
    answer = what_next(inputs(ahead), NOW)
    assert answer.top_pick.rules == ("due today at 5 pm",)
    assert answer.top_pick.fields_used["at"] == "17:00"
    # Under 30 minutes behind, it may still be happening: still picked, saying so.
    just_passed = item("a1", title="call the bank", due="2026-10-04", at="13:40")
    assert what_next(inputs(just_passed), NOW).top_pick.rules == ("was due at 1:40 pm today",)


def test_a_time_of_day_never_changes_the_rank_under_the_nine_rules_but_does_by_points():
    later = item("a1", title="due tonight", due="2026-10-04", at="23:00", created_at=NOW - timedelta(days=3))
    earlier = item("a2", title="due this morning", due="2026-10-04", at="09:00", created_at=NOW - timedelta(days=1))
    assert order(rank(wrap(later, earlier), TODAY, now=NOW, ranking=RANKING_RULES)) == [later.id, earlier.id]
    assert order(rank(wrap(later, earlier), TODAY, now=NOW)) == [earlier.id, later.id]


def test_rank_without_now_does_not_claim_a_time_has_passed():
    (only,) = rank(wrap(item("a1", title="call the bank", due="2026-10-04", at="09:00")), TODAY)
    assert only.rules == ("due today at 9 am",)


# --- A start time that has passed, and tonight (Accepted - Nic, 28 Sep 2026) ------------------------
# Nic told Diablo "Later 12am I got a meeting with Greta" at 3:10 pm on Sun 27 Sep, saved due Mon 28
# Sep at 00:00. At 6:43 pm that Monday "what next?" picked it: "was due at 12 am today".

SUN_2330 = datetime(2026, 9, 27, 23, 30, tzinfo=SINGAPORE)
MON_1843 = datetime(2026, 9, 28, 18, 43, tzinfo=SINGAPORE)
WEEK_EARLIER = datetime(2026, 9, 20, 9, 0, tzinfo=SINGAPORE)


def greta() -> Record:
    return item(
        "greta", title="Meeting with Greta at 12am", due="2026-09-28", at="00:00",
        resolved_from={"today": "2026-09-27", "words": "Later 12am"},
        created_at=datetime(2026, 9, 27, 15, 10, tzinfo=SINGAPORE),
    )


def test_what_next_asks_about_a_passed_start_time_and_never_picks_it():
    meeting = greta()
    lunch = item("lunch", title="Eat lunch today", due="2026-09-27")
    oldest = item("gtbc", title="Evaluate GTBC memory", created_at=WEEK_EARLIER)
    answer = what_next(inputs(meeting, lunch, oldest, now=MON_1843), MON_1843)
    # Overdue with no time of day can still be done late, so it stays a pick.
    assert (answer.top_pick.record_id, answer.top_pick.rules) == (lunch.id, ("was due yesterday",))
    offered = {
        answer.top_pick.record_id,
        *(entry.record_id for entry in answer.alternatives),
        *(entry.record_id for entry in answer.can_wait),
    }
    assert meeting.id not in offered
    assert [(entry.record_id, entry.question) for entry in answer.missed] == [
        (meeting.id, '"Meeting with Greta at 12am" was today at 12 am. Did it happen? Done, move or drop?'),
    ]
    assert answer.missed[0].fields_used == {"due": "2026-09-28", "at": "00:00"}


def test_a_start_time_is_picked_for_30_minutes_then_asked_about():
    meeting = greta()
    just_after = datetime(2026, 9, 28, 0, 29, tzinfo=SINGAPORE)
    answer = what_next(inputs(meeting, now=just_after), just_after)
    assert answer.top_pick.rules == ("was due at 12 am tomorrow",)
    assert answer.missed == ()
    over = datetime(2026, 9, 28, 0, 30, tzinfo=SINGAPORE)
    answer = what_next(inputs(meeting, now=over), over)
    assert answer.top_pick is None
    assert answer.not_enough.reason == "Nothing to start right now: everything open was set for a time that has passed."
    assert [entry.record_id for entry in answer.missed] == [meeting.id]


def test_missed_start_uses_relative_days_and_day_before_time_in_both_asks():
    meeting = greta()
    for day, words in ((29, "yesterday at 12 am"), (30, "at 12 am on Mon 28 Sep")):
        now = datetime(2026, 9, day, 6, tzinfo=SINGAPORE)
        answer = what_next(inputs(meeting, now=now), now)
        assert f"was {words}." in answer.missed[0].question
        picture = overview(inputs(meeting, now=now), now)
        assert any(f"was {words}." in line.text for line in picture.lines)


def test_where_am_i_lists_a_passed_start_under_missed_not_today():
    picture = overview(inputs(greta(), now=MON_1843), MON_1843)
    assert [(line.section, line.text) for line in picture.lines] == [
        (SECTION_MISSED, "Meeting with Greta at 12am — was today at 12 am. Did it happen? Done, move or drop?"),
    ]


def test_a_start_before_6am_tomorrow_counts_as_tonight():
    meeting = greta()
    report = item("report", title="Send the report", due="2026-09-29", created_at=WEEK_EARLIER)
    answer = what_next(inputs(meeting, report, now=SUN_2330), SUN_2330)
    assert (answer.top_pick.record_id, answer.top_pick.rules) == (meeting.id, ("due tonight at 12 am",))
    picture = overview(inputs(meeting, report, now=SUN_2330), SUN_2330)
    assert [(line.section, line.text) for line in picture.lines if line.section == SECTION_TODAY] == [
        (SECTION_TODAY, "Meeting with Greta at 12am — due tonight at 12 am"),
    ]
    run = item("run", title="Run", due="2026-09-28", at="06:00")
    (only,) = rank(wrap(run), date(2026, 9, 27), now=SUN_2330)
    assert only.rules == ("due tomorrow at 6 am",)  # 06:00 belongs to tomorrow morning, not tonight


def test_tonight_has_its_own_caution_and_can_wait_reason():
    assert caution(greta(), date(2026, 9, 27)) == 'Noted. "Meeting with Greta at 12am" is still due tonight.'
    due_today = [
        item(f"t{n}", title=f"today {n}", due="2026-09-27", created_at=WEEK_EARLIER + timedelta(hours=n))
        for n in range(3)
    ]
    answer = what_next(inputs(*due_today, greta(), now=SUN_2330), SUN_2330)
    assert [(entry.record_id, entry.reason) for entry in answer.can_wait] == [
        (greta().id, "also due tonight at 12 am"),
    ]


# --- In progress (N10b, N10c, N10d) ----------------------------------------------------------------


def test_in_progress_is_nics_own_started_work_with_stuck_marked_as_the_review_marks_it():
    """Stuck is the 6am review's own rule (`review.stuck_note`): an open task whose note last
    changed 3 or more Singapore days ago. A waiting task stays in progress but is never stuck."""

    fresh = item("a1", title="fresh")
    two_days = item("a2", title="two days")
    three_days = item("a3", title="three days")
    waiting = item("a8", title="waiting", state="waiting")
    parked = item("a4", title="parked", state="parked")
    suggestion = item("a5", title="suggestion", level="suggested")
    handed = item("a6", title="handed off")
    no_note = item("a7", title="not started")
    notes = (
        note(fresh, next_step=" call them "),
        note(two_days, updated_at=NOW - timedelta(days=2)),
        note(three_days, updated_at=datetime(2026, 10, 1, 23, 30, tzinfo=SINGAPORE)),
        note(waiting, updated_at=NOW - timedelta(days=9)),
        note(parked, updated_at=NOW - timedelta(days=9)),
        note(suggestion),
        note(session_id="gt-req_1", task_id=handed.id),
    )
    fresh = started_fact(fresh, "call them")
    two_days = started_fact(two_days, "open the file", NOW - timedelta(days=2))
    three_days = started_fact(three_days, "open the file", datetime(2026, 10, 1, 23, 30, tzinfo=SINGAPORE))
    waiting = started_fact(waiting, "open the file", NOW - timedelta(days=9))
    progress = in_progress(wrap(fresh, two_days, three_days, waiting, parked, suggestion, handed, no_note), notes, NOW)
    assert [(entry.title, entry.next_step, entry.stuck) for entry in progress] == [
        ("fresh", "call them", False),
        ("two days", "open the file", False),
        ("three days", "open the file", True),
        ("waiting", "open the file", True),
    ]
    assert progress[0].session_id == ""  # Personal work has no note session.
    assert progress[2].last_changed == datetime(2026, 10, 1, 23, 30, tzinfo=SINGAPORE)


def test_what_next_names_what_is_in_progress_from_three_things():
    tasks = [item(f"t{index}", title=f"task {index}", created_at=NOW - timedelta(days=index)) for index in range(4)]
    two = what_next(inputs(*(started_fact(task) if i < 2 else task for i, task in enumerate(tasks)), open_notes=tuple(note(task) for task in tasks[:2])), NOW)
    assert two.in_progress == ()
    three = what_next(inputs(*(started_fact(task) if i < IN_PROGRESS_NAMED_AT else task for i, task in enumerate(tasks)), open_notes=tuple(note(task) for task in tasks[:IN_PROGRESS_NAMED_AT])), NOW)
    assert [entry.title for entry in three.in_progress] == ["task 2", "task 1", "task 0"]
    handed = tuple(note(session_id=f"gt-req_{index}", task_id=task.id) for index, task in enumerate(tasks))
    assert what_next(inputs(*tasks, open_notes=handed), NOW).in_progress == ()


def test_a_not_enough_answer_still_names_what_is_in_progress():
    waiting = [item(f"w{index}", title=f"waiting {index}", state="waiting") for index in range(3)]
    answer = what_next(inputs(*(started_fact(task) for task in waiting), open_notes=tuple(note(task) for task in waiting)), NOW)
    assert answer.not_enough is not None
    assert len(answer.in_progress) == 3


# --- "Where am I?": the whole picture, ranked, recommending nothing (N1, §4) ----------------------


def test_the_overview_shows_every_section_in_order_ranked_inside_each():
    """Case 4's picture: two projects, a calendar item, a routine and a chore, plus a
    missed promise, started work (one stuck), a closing window, a focus project's task from its
    sitrep, waiting work, a request, a suggestion and this week's finished item. Nothing is
    recommended."""

    missed_promise = item("m1", title="Pay Sam back", owed_to="Sam", due="2026-10-01")
    missed_window = item("m2", title="Renew the pass", window={"start": "2026-09-28", "end": "2026-10-02"}, created_at=NOW - timedelta(days=9))
    due_today = item("t1", title="Submit the form", due="2026-10-04", at="17:00")
    routine = item("t2", title="Laundry", repeat="weekly:sun", created_at=NOW - timedelta(days=3))
    picked = item("p1", title="Tidy the garage", created_at=NOW - timedelta(days=12))
    exosuit = item("f1", title="Write the slice 4 docs", links=({"type": "about", "target": "vault_exosuit"},), created_at=NOW - timedelta(days=2))
    job = item("f2", title="Apply to robotics roles", links=({"type": "about", "target": "job-search"},), created_at=NOW - timedelta(days=1))
    closing = item("c1", title="Claim the rebate", window={"start": "2026-10-01", "end": "2026-10-07"})
    waiting = item("w1", title="Fix the leak", state="waiting", links=({"type": "waiting_on", "target": "the plumber"},))
    blocked = item("w2", title="Book the venue", state="blocked", created_at=NOW - timedelta(days=4))
    suggestion = item("s1", title="Try the new gym", level="suggested", source={"said_by": "agent", "came_by": "agent_guess", "speaker": "fleet-boss-grok", "recorded_by": "fleet-boss-grok"})
    finished = item("d1", title="Send the invoice", state="done", check={"locator": "sent email", "checked_by": "nic"}, updated_at=datetime(2026, 10, 2, 9, 0, tzinfo=SINGAPORE))
    chore = item("x1", title="Buy light bulbs", created_at=NOW - timedelta(days=40))
    events = (
        CalendarEvent(title="Dentist", start=NOW.replace(hour=16), end=NOW.replace(hour=17)),
        CalendarEvent(title="Holiday", start=NOW.replace(hour=0), end=NOW.replace(hour=23, minute=59), all_day=True),
    )
    requests = (OpenRequest(id="req_1", handed_to="fleet-boss-grok", since=date(2026, 9, 29), objective="Research robotics roles"),)
    notes = (
        note(exosuit, next_step="Draft the operations section", updated_at=NOW - timedelta(hours=3)),
        note(job, next_step="Shortlist three roles", updated_at=NOW - timedelta(days=4)),
        note(session_id="gt-req_1", task_id=None, next_step="Compare the three offers"),
    )
    exosuit = started_fact(exosuit, "Draft the operations section")
    job = started_fact(job, "Shortlist three roles", NOW - timedelta(days=4))
    exosuit_tasks = summary(
        "vault_exosuit",
        sitrep("sitreps", project="vault_exosuit", intent="Ship the sitreps", next_step="Merge the branch",
               updated_at=NOW - timedelta(hours=2)),
        as_of=NOW - timedelta(hours=1),
    )
    answer = overview(
        inputs(
            missed_promise, missed_window, due_today, routine, picked, exosuit, job, closing, waiting, blocked,
            suggestion, finished, chore,
            picked_today=frozenset({picked.id}),
            preferences=(focus_preference("p9", ["vault_exosuit", "job-search"]),),
            project_tasks=(exosuit_tasks,),
            calendar_events=events, open_requests=requests, open_notes=notes,
            source_status={"brain": None, "calendar": None, "requests": None, "email": "token_revoked"},
        ),
        NOW,
    )
    assert answer.sections == (
        SECTION_HIGHEST, SECTION_MISSED, SECTION_TODAY, SECTION_PICKED_FOR_TODAY, SECTION_IN_PROGRESS,
        SECTION_COMING_UP, SECTION_FOCUS_PROJECTS, SECTION_NO_DATE_YET, SECTION_WAITING_ON,
        SECTION_NEW_SUGGESTIONS, SECTION_FINISHED_THIS_WEEK, SECTION_COULD_NOT_SEE,
    )
    assert [(line.section, line.text) for line in answer.lines] == [
        # The three highest scores Nic could start now, across every section (PRD §4.5).
        (SECTION_HIGHEST, "Pay Sam back — was due Thu 1 Oct; Sam is waiting"),
        (SECTION_HIGHEST, "Renew the pass — was due Fri 2 Oct"),
        (SECTION_HIGHEST, "Write the slice 4 docs — focus project: vault_exosuit; already started"),
        (SECTION_MISSED, "Pay Sam back — missed since Thu 1 Oct (3 days)"),  # Sam is waiting: more points
        (SECTION_MISSED, "Renew the pass — missed since Fri 2 Oct (2 days)"),
        (SECTION_TODAY, "Holiday — on your calendar, all day"),
        (SECTION_TODAY, "Dentist — on your calendar at 4 pm"),
        (SECTION_TODAY, "Submit the form — due today at 5 pm"),
        (SECTION_TODAY, "Laundry — every Sunday, due today; no occurrence yet"),
        (SECTION_PICKED_FOR_TODAY, "Tidy the garage — you picked it for today"),
        (SECTION_IN_PROGRESS, "Write the slice 4 docs — next: Draft the operations section; last changed today"),
        (SECTION_IN_PROGRESS, "Apply to robotics roles — next: Shortlist three roles; last changed Wed 30 Sep; stuck"),
        (SECTION_COMING_UP, "Claim the rebate — due Wed 7 Oct"),
        (SECTION_FOCUS_PROJECTS, "vault_exosuit — summary as of today at 1 pm"),
        (SECTION_FOCUS_PROJECTS, "Ship the sitreps — next: Merge the branch; last changed today"),
        (SECTION_FOCUS_PROJECTS, "job-search — no summary yet"),
        (SECTION_NO_DATE_YET, "Buy light bulbs — last changed Tue 25 Aug"),
        (SECTION_WAITING_ON, "Book the venue — waiting: reason not recorded"),
        (SECTION_WAITING_ON, "Fix the leak — waiting on the plumber"),
        (SECTION_WAITING_ON, "Research robotics roles — with fleet-boss-grok since Tue 29 Sep; next: Compare the three offers"),
        (SECTION_NEW_SUGGESTIONS, "Try the new gym — suggested by fleet-boss-grok"),
        (SECTION_FINISHED_THIS_WEEK, "Send the invoice — done Fri 2 Oct"),
        (SECTION_COULD_NOT_SEE, "email: token_revoked"),
    ]
    # The chore is undated, not started and not picked: no other section shows it, so it is
    # under "No date yet" (N17; until 28 Sep it wasn't shown at all).
    assert [line.item_ref for line in answer.lines if line.section == SECTION_NO_DATE_YET] == [chore.id]
    by_ref = {line.item_ref: line for line in answer.lines if line.section == SECTION_IN_PROGRESS}
    assert by_ref[job.id].reasons == ("focus project: job-search", "already started")
    assert by_ref[job.id].fields_used["stuck"] is True
    # Nothing is recommended: no first step, no time box, nothing to say yes to (N1).
    for line in answer.lines:
        assert line.rule.startswith("overview_")
        assert not {"first_step", "time_box_minutes", "estimate_minutes"} & set(line.fields_used)
    assert OVERVIEW_TITLES[SECTION_PICKED_FOR_TODAY] == "Picked for today"
    assert answer.is_empty is False


def test_where_am_i_opens_with_the_highest_scores_so_a_big_thing_cannot_hide():
    """PRD acceptance case 19 (the test's P7): six small overdue chores fill Missed, which comes
    first; "we lose the flat", due today, sits in Today. "Highest right now" puts the flat first,
    then the usual sections. It still recommends nothing."""

    chores = [
        item(f"h00{index}", title=f"Chore {index}", due=f"2026-10-0{index % 3 + 1}", created_at=NOW - timedelta(days=9),
             importance={"level": "minor", "reason": "a mess", "proposed_by": "hermes-wa", "confirmed": False})
        for index in range(6)
    ]
    flat = item("h010", title="Send the signed tenancy agreement", due="2026-10-04", at="17:00", owed_to="the landlord",
                importance={"level": "high", "reason": "we lose the flat", "proposed_by": "hermes-wa", "confirmed": True})
    answer = overview(inputs(*chores, flat), NOW)
    assert answer.sections[:3] == (SECTION_HIGHEST, SECTION_MISSED, SECTION_TODAY)
    highest = [line for line in answer.lines if line.section == SECTION_HIGHEST]
    assert len(highest) == 3
    assert highest[0].text == (
        "Send the signed tenancy agreement — due today at 5 pm; the landlord is waiting"
    )
    assert highest[0].item_ref == flat.id and highest[0].fields_used["place"] == 1
    assert all(line.item_ref in {chore.id for chore in chores} for line in highest[1:])
    # Each also shows in its own section: the chores under Missed, the flat under Today.
    assert [line.item_ref for line in answer.lines if line.section == SECTION_TODAY] == [flat.id]
    assert len([line for line in answer.lines if line.section == SECTION_MISSED]) == 6
    for line in highest:
        assert line.rule == "overview_highest_right_now"
        assert not {"first_step", "time_box_minutes", "estimate_minutes"} & set(line.fields_used)
    assert OVERVIEW_TITLES[SECTION_HIGHEST] == "Highest right now"


def test_the_top_line_holds_only_what_nic_could_start_now():
    """Proposed - Nic to confirm: waiting and blocked items can't be started, and a start time
    that has passed is asked about, so none of them is in "Highest right now"; a focus project's
    task is. Fewer than three open things give fewer lines."""

    waiting = item("i001", title="Get the quote", state="waiting", owed_to="Ben", due="2026-10-04")
    blocked = item("i002", title="Book the venue", state="blocked", due="2026-10-03")
    passed = item("i003", title="Call the bank", due="2026-10-04", at="09:00")
    plain = item("i004", title="Buy light bulbs", created_at=NOW - timedelta(days=40))
    task = sitrep("slice-8", intent="Build slice 8")
    answer = overview(inputs(
        waiting, blocked, passed, plain,
        preferences=(focus_preference("p1", [GTBC]),), project_tasks=(summary(GTBC, task),),
    ), NOW)
    highest = [line for line in answer.lines if line.section == SECTION_HIGHEST]
    assert [line.item_ref for line in highest] == ["project:vault_gtbc_plans:slice-8", plain.id]
    assert highest[0].text == "Build slice 8 — focus project: vault_gtbc_plans; already started"
    assert (highest[0].record_sha256, highest[0].record_path, highest[0].record_state) == ("c" * 64, None, "open")
    assert highest[1].text == "Buy light bulbs — no date or stake yet; saved Tue 25 Aug"
    assert overview(inputs(waiting, blocked), NOW).sections[0] != SECTION_HIGHEST


def test_items_inside_a_section_follow_the_ranking_not_their_age():
    older = item("a1", title="older missed", due="2026-10-01", created_at=NOW - timedelta(days=20))
    picked = item("a2", title="picked missed", due="2026-10-02", created_at=NOW - timedelta(days=1))
    answer = overview(inputs(older, picked, picked_today=frozenset({picked.id})), NOW)
    missed_lines = [line for line in answer.lines if line.section == SECTION_MISSED]
    assert [line.item_ref for line in missed_lines] == [picked.id, older.id]
    assert missed_lines[0].reasons == ("was due Fri 2 Oct", "you picked it for today")
    assert missed_lines[0].fields_used["rules"] == ["was due Fri 2 Oct", "you picked it for today"]
    assert missed_lines[0].record_path == f"records/item/2026/10/{picked.id}.md"
    assert missed_lines[0].record_sha256 == picked.sha256


def test_an_in_progress_task_with_no_first_step_says_so():
    task = item("a1", title="Call the bank")
    answer = below_top_line(overview(inputs(task, open_notes=(note(task, next_step=""),)), NOW))
    assert [line.text for line in answer.lines] == ["Call the bank — last changed today"]


def test_windows_closing_is_today_to_three_days_out():
    today = item("a1", title="closes today", window={"start": "2026-10-01", "end": "2026-10-04"})
    three = item("a2", title="closes in three", window={"start": "2026-10-01", "end": "2026-10-07"})
    four = item("a3", title="closes in four", window={"start": "2026-10-01", "end": "2026-10-08"})
    answer = below_top_line(overview(inputs(today, three, four), NOW))
    assert [(line.section, line.text) for line in answer.lines] == [
        (SECTION_TODAY, "closes today — due today"),
        (SECTION_COMING_UP, "closes in three — due Wed 7 Oct"),
        (SECTION_COMING_UP, "closes in four — due Thu 8 Oct"),  # N16
    ]


def test_waiting_on_caps_requests_at_ten_newest_first_and_labels_a_returned_result():
    requests = tuple(
        OpenRequest(id=f"req_{index:02d}", handed_to="fleet-boss-grok", since=date(2026, 9, 20) + timedelta(days=index), objective=f"Job {index:02d}")
        for index in range(11)
    )
    returned = replace(requests[10], work_state="result_returned", check="none")
    notes = (
        note(session_id="gt-req_10", task_id=None, next_step="Result returned, not yet checked.", updated_at=NOW - timedelta(hours=2)),
        note(session_id="gt-req_09", task_id=None, next_step="  ", updated_at=NOW),
    )
    answer = overview(inputs(open_requests=(*requests[:10], returned), open_notes=notes), NOW)
    lines = [line for line in answer.lines if line.section == SECTION_WAITING_ON]
    assert lines[0].text == "Job 10 — returned, not yet checked (with fleet-boss-grok since Wed 30 Sep)"
    assert lines[0].record_state == "returned_unchecked"
    assert lines[1].text == "Job 09 — with fleet-boss-grok since Tue 29 Sep"
    assert lines[1].record_state == "open"
    assert len(lines) == 11
    assert (lines[-1].text, lines[-1].item_ref, lines[-1].fields_used) == (
        "1 more; ask to see them", "__more_requests__", {"remaining": 1},
    )


def test_new_suggestions_are_the_live_ones_oldest_first():
    newer = item("a1", title="newer idea", level="suggested", created_at=NOW - timedelta(days=1))
    older = item("a2", title="older idea", level="suggested", created_at=NOW - timedelta(days=5))
    dropped = item("a3", title="dropped idea", level="suggested", state="dropped")
    possible = item("a4", title="maybe piano", level="possible")
    answer = overview(inputs(newer, older, dropped, possible), NOW)
    assert [line.text for line in answer.lines] == [
        "older idea — suggested by an agent",
        "newer idea — suggested by an agent",
    ]


def test_finished_this_week_is_the_last_seven_days_newest_first():
    today = item("a1", title="today", state="done", check={"locator": "x", "checked_by": "nic"}, updated_at=NOW - timedelta(hours=1))
    six_days = item("a2", title="six days", state="done", check={"locator": "x", "checked_by": "sam"}, updated_at=NOW - timedelta(days=6))
    seven_days = item("a3", title="seven days", state="done", check={"locator": "x", "checked_by": "nic"}, updated_at=NOW - timedelta(days=7))
    suggestion = item("a4", title="a done suggestion", level="suggested", state="done", check={"locator": "x", "checked_by": "nic"})
    answer = overview(inputs(six_days, seven_days, today, suggestion), NOW)
    assert [line.text for line in answer.lines] == ["today — done today", "six days — done Mon 28 Sep"]
    assert answer.lines[1].fields_used["checked_by"] == "sam"


def test_the_overview_still_shows_what_it_could_see_when_brain_home_cannot_be_read():
    events = (CalendarEvent(title="Dentist", start=NOW.replace(hour=16), end=NOW.replace(hour=17)),)
    requests = (OpenRequest(id="req_1", handed_to="local-codex", since=date(2026, 10, 3)),)
    answer = overview(inputs(calendar_events=events, open_requests=requests, source_status={"brain": "GitHubError: 503", "notes": "not_configured"}), NOW)
    assert [(line.section, line.text) for line in answer.lines] == [
        (SECTION_TODAY, "Dentist — on your calendar at 4 pm"),
        (SECTION_WAITING_ON, "req_1 — with local-codex since yesterday"),
        (SECTION_COULD_NOT_SEE, "brain: GitHubError: 503"),
        (SECTION_COULD_NOT_SEE, "notes: not_configured"),
    ]
    assert answer.lines[0].item_ref == f"calendar:Dentist:{NOW.replace(hour=16).isoformat()}"


def test_an_empty_picture_has_no_sections():
    answer = overview(inputs(item("a1", title="an idea", level="possible"), item("a2", title="dropped", state="dropped")), NOW)
    assert answer.lines == () and answer.sections == () and answer.is_empty


# --- "Coming up" and "No date yet" (N16, N17; Accepted - Nic, 28 Sep 2026) -------------------------


def test_coming_up_lists_the_next_seven_days_most_important_first_and_counts_later_ones():
    """N16: Nic picked "7 days" and "most important first": the same points as every other
    section, so a plain task due tomorrow sits below one someone is waiting on, and above a
    window that closes on Friday."""

    plain = item("c001", title="Book the dentist", due="2026-10-05", created_at=NOW - timedelta(days=1))
    waiting = item("c002", title="Pay Sam back", due="2026-10-07", owed_to="Sam")
    high = item("c003", title="Renew the visa", due="2026-10-11", at="09:30",
                importance={"level": "high", "reason": "overstaying costs a fine", "proposed_by": "hermes-wa", "confirmed": True})
    window = item("c004", title="Clean the flat", window={"start": "2026-10-05", "end": "2026-10-09"}, created_at=NOW - timedelta(days=2))
    later = item("c005", title="File taxes", due="2026-10-12")
    much_later = item("c006", title="Service the car", window={"start": "2026-11-01", "end": "2026-11-30"})
    answer = below_top_line(overview(inputs(plain, waiting, high, window, later, much_later), NOW))
    assert answer.sections == (SECTION_COMING_UP,)
    assert [line.text for line in answer.lines] == [
        "Pay Sam back — due Wed 7 Oct",
        "Book the dentist — due tomorrow",
        "Clean the flat — due Fri 9 Oct",
        "Renew the visa — due Sun 11 Oct at 9:30 am",
        "File taxes — due Mon 12 Oct",
        "Service the car — due Mon 30 Nov",
    ]
    assert answer.lines[0].reasons == ("Sam is waiting", "due Wed 7 Oct")
    assert answer.lines[3].reasons == ("due Sun 11 Oct at 9:30 am",)  # importance retired
    assert answer.lines[0].fields_used["coming_up"] == "2026-10-07"
    assert answer.lines[0].rule == "overview_coming_up"
    assert [line.item_ref for line in answer.lines[-2:]] == [later.id, much_later.id]
    assert len({line.item_ref for line in answer.lines}) == len(answer.lines)
    for line in answer.lines:
        assert not {"first_step", "time_box_minutes", "estimate_minutes"} & set(line.fields_used)


def test_coming_up_lists_every_open_thing_once_even_beyond_ten():
    tasks = [
        item(f"c1{index:02x}", title=f"task {index:02d}", due="2026-10-06", created_at=NOW - timedelta(days=index))
        for index in range(COMING_UP_CAP + 2)
    ]
    lines = [line for line in overview(inputs(*tasks), NOW).lines if line.section == SECTION_COMING_UP]
    assert len(lines) == 12  # W7: every open thing once; no omitted overflow.
    assert len({line.item_ref for line in lines}) == 12
    assert lines[0].text == "task 11 — due Tue 6 Oct"  # the same date, so the oldest first
    assert (lines[-1].text, lines[-1].item_ref) == ("task 00 — due Tue 6 Oct", tasks[0].id)
    assert lines[-1].fields_used["coming_up"] == "2026-10-06"


def test_a_weekly_routine_is_coming_up_on_its_next_day():
    tuesday = item("c201", title="Gym", repeat="weekly:tue")
    sunday = item("c202", title="Laundry", repeat="weekly:sun")  # today, so under Today
    answer = below_top_line(overview(inputs(tuesday, sunday), NOW))
    assert [(line.section, line.text) for line in answer.lines] == [
        # W7 §6.3: each routine once, retaining its occurrence statistics.
        (SECTION_TODAY, "Laundry — every Sunday, due today; no occurrence yet"),
        (SECTION_COMING_UP, "Gym — every Tuesday; next Tue 6 Oct; no occurrence yet"),
    ]


def test_no_date_yet_lists_undated_tasks_most_important_first_then_parked_ones_back():
    """N17: an open task with no date that no other section shows, ranked by points (here what
    others wait on first); then parked tasks whose day has come, as the 6am review's "Pick or park" has them.
    A parked task whose day hasn't come isn't shown: it isn't open."""

    old = item("d001", title="Buy light bulbs", created_at=NOW - timedelta(days=40))
    blocker = item("d002", title="Clear the storeroom", created_at=NOW - timedelta(days=1))
    waits = item("d003", title="Buy boxes", state="waiting", links=({"type": "waiting_on", "target": blocker.id},))
    event = item("d004", title="Call the landlord", created_at=NOW - timedelta(days=2))
    event = replace(event, data={**event.data, "timing": {"on_event": "the studio reopens"}})
    back = item("d005", title="Sort the photos", state="parked", created_at=NOW - timedelta(days=9))
    back = replace(back, data={**back.data, "parked_until": "2026-10-03"})
    parked = item("d006", title="Paint the fence", state="parked")
    parked = replace(parked, data={**parked.data, "parked_until": "2026-10-20"})
    answer = below_top_line(overview(inputs(old, blocker, waits, event, back, parked), NOW))
    assert [(line.section, line.text) for line in answer.lines] == [
        (SECTION_NO_DATE_YET, "Buy light bulbs — last changed Tue 25 Aug"),
        (SECTION_NO_DATE_YET, "Clear the storeroom — last changed yesterday"),
        (SECTION_NO_DATE_YET, "Sort the photos — parked until yesterday"),
        (SECTION_WAITING_ON, "Call the landlord — not before the studio reopens"),
        (SECTION_WAITING_ON, "Buy boxes — waiting on " + blocker.id),
    ]
    assert answer.lines[0].reasons == ("no date or stake yet; saved Tue 25 Aug",)  # Only after earns blocking points.
    assert answer.lines[0].rule == "overview_no_date_yet"


def test_no_date_yet_lists_every_open_thing_once_even_beyond_ten():
    tasks = [item(f"d1{index:02x}", title=f"chore {index:02d}", created_at=NOW - timedelta(days=index)) for index in range(12)]
    lines = [line for line in overview(inputs(*tasks), NOW).lines if line.section == SECTION_NO_DATE_YET]
    assert len(lines) == 12  # W7: every open thing once, with no capped overflow.
    assert {line.item_ref for line in lines} == {task.id for task in tasks}
    assert len({line.item_ref for line in lines}) == len(lines)


def test_coming_up_and_no_date_yet_never_repeat_a_task_another_section_shows():
    picked = item("e001", title="picked, due Tue", due="2026-10-06")
    started = started_fact(item("e002", title="started, no date"))
    blocked = item("e003", title="blocked, due Wed", state="blocked", due="2026-10-07")
    answer = below_top_line(overview(inputs(picked, started, blocked, picked_today=frozenset({picked.id}), open_notes=(note(started),)), NOW))
    assert [(line.section, line.item_ref) for line in answer.lines] == [
        (SECTION_PICKED_FOR_TODAY, picked.id),
        (SECTION_IN_PROGRESS, started.id),
        (SECTION_WAITING_ON, blocked.id),
    ]


def test_every_open_task_shows_in_where_am_i():
    """N17, the safety test: whatever its dates, repeat, state, pick or note, every accepted task
    that is open, waiting or blocked is listed, or counted in "Coming up"'s later line. Until 28
    Sep a task due in a few days, or with no date, that Nic hadn't picked or started wasn't shown."""

    timings = (
        {}, {"due": "2026-09-30"}, {"due": "2026-10-04"}, {"due": "2026-10-04", "at": "08:00"}, {"due": "2026-10-05"},
        {"due": "2026-10-07", "at": "18:00"}, {"due": "2026-10-11"}, {"due": "2026-10-12"}, {"due": "2027-01-01"},
        {"window": {"start": "2026-09-20", "end": "2026-10-01"}}, {"window": {"start": "2026-10-01", "end": "2026-10-04"}},
        {"window": {"start": "2026-10-01", "end": "2026-10-07"}}, {"window": {"start": "2026-10-05", "end": "2026-10-08"}},
        {"window": {"start": "2026-10-12", "end": "2026-10-30"}}, {"on_event": "the studio reopens"},
    )
    repeats = (None, "weekly:sun", "weekly:wed", "daily")
    for timing, repeat, state, picked, started, owed in itertools.product(
        timings, repeats, ("open", "waiting", "blocked"), (False, True), (False, True), (None, "Sam"),
    ):
        record = item("f001", title="a task", state=state, repeat=repeat, owed_to=owed)
        record = replace(record, data={**record.data, "timing": dict(timing)})
        answer = below_top_line(overview(inputs(
            record,
            picked_today=frozenset({record.id}) if picked else frozenset(),
            open_notes=(note(record),) if started else (),
        ), NOW))
        # "Routines" is a view of each routine's streak, like the top line: it doesn't count here.
        listed = record.id in {line.item_ref for line in answer.lines if line.section != SECTION_ROUTINES}
        counted = any(line.item_ref == COMING_UP_LATER_REF for line in answer.lines)
        assert listed or counted, (timing, repeat, state, picked, started, owed)
        assert not (listed and counted)


def test_the_reviews_coming_up_rows_never_count_towards_tends_to_stall():
    """N16 with redesign default 11: a task listed ahead of its date was only shown, not brought
    back, so it never gets the time box for stalling."""

    task = item("g001", title="Pay Sam back", due="2026-10-07")
    rows = tuple(
        HistoryRow(item_ref=task.id, channel="review", rule=COMING_UP_RULE, record_sha256=task.sha256,
                   record_state="open", surfaced_at=NOW - timedelta(days=days))
        for days in (1, 2)
    )
    assert not tends_to_stall(task, rows)
    assert tends_to_stall(task, tuple(replace(row, rule="pick_or_park") for row in rows))


# --- Project work (N4; rebuild plan slice 8, part 2): focus projects' tasks from task sitreps -------


GTBC = "vault_gtbc_plans"


def test_focus_projects_lists_each_focus_projects_tasks_with_how_fresh_its_summary_is():
    """N1, N4: Nic's focus projects in the order he named them, each with its summary's "as of",
    then its open and blocked tasks, ranked (same rule and started, so oldest note first). A focus
    project with no summary says so; a project he didn't name isn't shown. Nothing is recommended."""

    moving = sitrep("slice-8", intent="Build slice 8", next_step="Write the tests", updated_at=NOW - timedelta(hours=3),
                    sha="1" * 64)
    blocked = sitrep("release", intent="Package the release", state="blocked", blockers=("Waiting on the merge", "CI"),
                     updated_at=datetime(2026, 10, 2, 10, 0, tzinfo=SINGAPORE), sha="2" * 64)
    no_step = sitrep("docs", intent="Write the docs", next_step="  ", updated_at=NOW - timedelta(days=1), sha="3" * 64)
    closed = sitrep("old", intent="Old work", state="closed", updated_at=NOW - timedelta(days=9))
    other = sitrep("elsewhere", project="vault_other", intent="Not a focus project")
    preferences = (focus_preference("p1", [GTBC, "vault_robotics"]),)
    projects = (
        summary(GTBC, moving, blocked, no_step, closed, as_of=datetime(2026, 10, 4, 9, 30, tzinfo=SINGAPORE)),
        summary("vault_other", other),
    )
    answer = below_top_line(overview(inputs(preferences=preferences, project_tasks=projects), NOW))
    assert answer.sections == (SECTION_FOCUS_PROJECTS,)
    assert [line.text for line in answer.lines] == [
        "vault_gtbc_plans — summary as of today at 9:30 am",
        "Package the release — blocked: Waiting on the merge; last changed Fri 2 Oct",
        "Write the docs — no first step yet; last changed yesterday",
        "Build slice 8 — next: Write the tests; last changed today",
        "vault_robotics — no summary yet",
    ]
    header, first, *_rest, robotics = answer.lines
    assert header.item_ref == "project:vault_gtbc_plans" and header.rule == "overview_focus_projects"
    assert header.fields_used == {"focus_project": GTBC, "project": GTBC, "as_of": "2026-10-04T09:30:00+08:00", "tasks": 3}
    assert first.item_ref == project_task_ref(blocked) == "project:vault_gtbc_plans:release"
    assert first.reasons == ("focus project: vault_gtbc_plans", "already started")
    assert (first.record_sha256, first.record_state, first.record_path) == ("2" * 64, "blocked", None)
    assert first.fields_used["project"] == GTBC and first.fields_used["session_id"] == "release"
    assert first.fields_used["blockers"] == ["Waiting on the merge", "CI"]
    assert (robotics.item_ref, robotics.fields_used["project"], robotics.fields_used["as_of"]) == (
        "project:vault_robotics", None, None,
    )
    for line in answer.lines:
        assert not {"first_step", "time_box_minutes", "estimate_minutes"} & set(line.fields_used)
    assert focus_projects(preferences) == (GTBC, "vault_robotics")


def test_a_focus_project_with_nothing_open_says_so_and_how_fresh_that_is():
    closed = sitrep("done", state="closed")
    answer = overview(inputs(
        preferences=(focus_preference("p1", [GTBC]),),
        project_tasks=(summary(GTBC, closed, as_of=datetime(2026, 10, 2, 9, 0, tzinfo=SINGAPORE)),),
    ), NOW)
    assert [line.text for line in answer.lines] == ["vault_gtbc_plans — nothing open; summary as of Fri 2 Oct at 9 am"]
    assert answer.lines[0].fields_used["tasks"] == 0


def test_a_focus_project_lists_at_most_ten_tasks_then_says_how_many_more():
    tasks = tuple(sitrep(f"task-{index:02d}", intent=f"Task {index:02d}", updated_at=NOW - timedelta(hours=index))
                  for index in range(FOCUS_PROJECT_TASK_CAP + 2))
    answer = below_top_line(overview(inputs(preferences=(focus_preference("p1", [GTBC]),), project_tasks=(summary(GTBC, *tasks),)), NOW))
    assert len(answer.lines) == 1 + FOCUS_PROJECT_TASK_CAP + 1
    assert answer.lines[1].text.startswith("Task 11 — ")  # oldest note first
    assert (answer.lines[-1].text, answer.lines[-1].item_ref, answer.lines[-1].fields_used) == (
        "2 more; ask to see them", "project:vault_gtbc_plans:__more__", {"project": GTBC, "remaining": 2},
    )


def test_with_sitreps_off_neither_ask_shows_or_ranks_a_project_task():
    """Nothing read (sitreps off, or unreadable): both asks answer exactly as before, whatever
    Nic's focus projects are (Proposed: the section is left out rather than saying "no summary")."""

    assert OrientationInputs(review=review_inputs()).project_tasks is None
    assert OVERVIEW_SECTIONS.index(SECTION_FOCUS_PROJECTS) == OVERVIEW_SECTIONS.index(SECTION_COMING_UP) + 1
    assert OVERVIEW_SECTIONS.index(SECTION_FOCUS_PROJECTS) == OVERVIEW_SECTIONS.index(SECTION_NO_DATE_YET) - 1
    task = item("a1", title="exosuit task", links=({"type": "about", "target": GTBC},))
    preferences = (focus_preference("p1", [GTBC]),)
    off = overview(inputs(task, preferences=preferences, open_notes=(note(task),)), NOW)
    assert SECTION_FOCUS_PROJECTS not in off.sections
    answer = what_next(inputs(task, preferences=preferences), NOW)
    assert top(answer) == task.id and answer.alternatives == ()
    assert what_next(inputs(preferences=preferences), NOW).not_enough.reason == "Nothing accepted and open right now."


def test_no_focus_projects_means_no_section_even_with_sitreps_on():
    answer = overview(inputs(project_tasks=(summary(GTBC, sitrep("slice-8")),)), NOW)
    assert answer.sections == ()
    assert what_next(inputs(project_tasks=(summary(GTBC, sitrep("slice-8")),)), NOW).top_pick is None


def test_a_focus_projects_open_task_competes_by_points_with_its_next_step_as_the_first_step():
    """N4, by points (PRD §5): a focus project's task is started, so it scores 9 (focus project 5,
    started 4): above a plain task due today with no stake, below one with a stake (the test's
    P9). Its first step is the sitrep's next step."""

    due_today = item("a1", title="pay the bill", due="2026-10-04")
    high = item("a2", title="book flights", importance={"level": "high", "reason": "the fare doubles", "proposed_by": "hermes-wa", "confirmed": True})
    blocker = item("a3", title="clear the storeroom", created_at=NOW - timedelta(days=50))
    waits = item("a4", title="buy boxes", state="waiting", links=({"type": "waiting_on", "target": blocker.id},))
    old = item("a5", title="old chore", created_at=NOW - timedelta(days=300))
    task = sitrep("slice-8", intent="Build slice 8", next_step="  Write the tests ", sha="9" * 64)
    answer = what_next(inputs(
        due_today, high, blocker, waits, old,
        preferences=(focus_preference("p1", [GTBC]),), project_tasks=(summary(GTBC, task),),
    ), NOW)
    assert [top(answer), *(alt.record_id for alt in answer.alternatives)] == [
        "project:vault_gtbc_plans:slice-8", due_today.id, old.id,
    ]
    assert [entry.record_id for entry in answer.can_wait] == [blocker.id, high.id]
    staked = replace(due_today, data={**due_today.data, "importance": {
        "level": "real_cost", "reason": "a late fee", "proposed_by": "hermes-wa", "confirmed": False,
    }})
    ahead = what_next(inputs(
        staked, preferences=(focus_preference("p1", [GTBC]),), project_tasks=(summary(GTBC, task),),
    ), NOW)
    assert [top(ahead), ahead.alternatives[0].record_id] == ["project:vault_gtbc_plans:slice-8", staked.id]  # Retired importance has no lift.

    only = what_next(
        inputs(blocker, old, preferences=(focus_preference("p1", [GTBC]),), project_tasks=(summary(GTBC, task),)),
        NOW, signal=OrientationSignal(kind="calendar_gap", gap_minutes=30),
    )
    pick = only.top_pick
    assert (pick.record_id, pick.title, pick.plain_word, pick.first_step) == (
        "project:vault_gtbc_plans:slice-8", "Build slice 8", "task", "Write the tests",
    )
    assert pick.rules == ("focus project: vault_gtbc_plans", "already started")
    assert pick.fields_used == {
        "focus_project": GTBC, "project": GTBC, "session_id": "slice-8", "started": True, "gap_minutes": 30,
        "points": {"total": 9.0, "focus": 5.0, "started": 4.0},
    }
    assert (pick.estimate_minutes, pick.time_box_minutes, pick.task) == (None, None, task)
    assert only.assumptions == ()
    assert answer.top_pick.task == task and answer.top_pick.plain_word == "task"


def test_a_non_focus_projects_tasks_do_not_compete():
    """Proposed - Nic to confirm: only a project one of his focus projects names competes."""

    chore = item("a1", title="old chore", created_at=NOW - timedelta(days=30))
    elsewhere = summary("vault_other", sitrep("x", project="vault_other", intent="Not a focus project"))
    answer = what_next(inputs(chore, preferences=(focus_preference("p1", [GTBC]),), project_tasks=(elsewhere,)), NOW)
    assert top(answer) == chore.id
    assert answer.alternatives == () and answer.can_wait == ()


def test_project_tasks_score_for_their_focus_project_and_being_started():
    """A project task always has an open note, so it counts as started (Proposed): it beats a new
    saved task in the same focus project, however old, and sits just below a started one, which
    also scores for its age."""

    new_saved = item("a1", title="new saved task", links=({"type": "about", "target": GTBC},), created_at=NOW - timedelta(days=90))
    started_saved = item("a2", title="started saved task", links=({"type": "part_of", "target": GTBC},), created_at=NOW - timedelta(days=2))
    started_saved = started_fact(started_saved)
    task = sitrep("slice-8", updated_at=NOW - timedelta(days=1))
    ranked = rank(
        wrap(new_saved, started_saved), TODAY, focus_projects=frozenset({GTBC}), notes=(note(started_saved),),
        project_tasks=(task,),
    )
    assert order(ranked) == [started_saved.id, "project:vault_gtbc_plans:slice-8", new_saved.id]
    assert [candidate.started for candidate in ranked] == [True, True, False]
    assert ranked[1].rules == ("focus project: vault_gtbc_plans", "already started")
    assert (ranked[1].item, ranked[1].title, ranked[1].state) == (None, "Build slice 8", "open")
    # A project task handed to rank with no focus project that names it scores only for being started.
    (loose,) = rank((), TODAY, project_tasks=(task,))
    assert loose.rules == ("already started",)
    assert loose.fields_used == {
        "project": GTBC, "session_id": "slice-8", "started": True, "points": {"total": 4.0, "started": 4.0},
    }
    assert loose.points == 4.0


def test_a_blocked_project_task_can_wait_but_is_never_picked():
    chore = item("a1", title="old chore", created_at=NOW - timedelta(days=30))
    blocked = sitrep("release", intent="Package the release", state="blocked", blockers=("Waiting on the merge",))
    bare = sitrep("infra", intent="Move the hub", state="blocked", updated_at=NOW - timedelta(hours=1))
    open_task = sitrep("slice-8")
    preferences = (focus_preference("p1", [GTBC]),)
    answer = what_next(inputs(chore, preferences=preferences, project_tasks=(summary(GTBC, blocked, bare),)), NOW)
    assert top(answer) == chore.id
    assert [(entry.record_id, entry.reason) for entry in answer.can_wait] == [
        (project_task_ref(blocked), "blocked: Waiting on the merge"),
        (project_task_ref(bare), "blocked on something else"),
    ]
    assert not {project_task_ref(blocked), project_task_ref(bare)} & {
        candidate.ref for candidate in rank(wrap(chore), TODAY, focus_projects=frozenset({GTBC}),
                                           project_tasks=(blocked, bare), now=NOW)
    }
    assert blocked not in tuple(entry.task for entry in answer.alternatives)

    late_fee = {"level": "real_cost", "reason": "a late fee", "proposed_by": "hermes-wa", "confirmed": False}
    picks = [item(f"b{index}", title=f"due {index}", due="2026-10-04", importance=late_fee) for index in range(3)]
    crowded = what_next(inputs(*picks, preferences=preferences, project_tasks=(summary(GTBC, open_task),)), NOW)
    assert [entry.record_id for entry in crowded.can_wait] == [picks[2].id]  # Old proposed importance adds nothing; sitrep stays top.

    only_blocked = what_next(inputs(preferences=preferences, project_tasks=(summary(GTBC, blocked),)), NOW)
    assert only_blocked.not_enough.reason == "Everything open right now is blocked on something else."
    waiting = item("w1", title="quote", state="waiting", owed_to="Ben")
    mixed = what_next(inputs(waiting, preferences=preferences, project_tasks=(summary(GTBC, blocked),)), NOW)
    assert mixed.not_enough.reason == "Everything open right now is waiting on someone or blocked on something."
    assert mixed.not_enough.questions == ('Is "quote" still waiting on Ben?',)


def test_a_closed_project_task_is_never_listed_or_ranked():
    closed = sitrep("done", intent="Finished work", state="closed")
    answer = what_next(inputs(preferences=(focus_preference("p1", [GTBC]),), project_tasks=(summary(GTBC, closed),)), NOW)
    assert answer.not_enough.reason == "Nothing accepted and open right now."


def test_a_project_task_picked_on_two_earlier_days_with_its_note_unchanged_gets_the_time_box():
    task = sitrep("slice-8", sha="7" * 64)
    ref = project_task_ref(task)

    def row(days: int, sha: str = "7" * 64, channel: str = "orient") -> HistoryRow:
        return HistoryRow(item_ref=ref, channel=channel, rule="focus project", record_sha256=sha, record_state="open",
                          surfaced_at=NOW - timedelta(days=days))

    projects = (summary(GTBC, task),)
    preferences = (focus_preference("p1", [GTBC]),)
    stalled = what_next(inputs(preferences=preferences, project_tasks=projects, history=(row(1), row(2))), NOW)
    assert stalled.top_pick.time_box_minutes == 20
    moved = what_next(inputs(preferences=preferences, project_tasks=projects, history=(row(1), row(2, sha="6" * 64))), NOW)
    assert moved.top_pick.time_box_minutes is None
    assert surfaced_unchanged(ref, "7" * 64, (row(1, channel=OVERVIEW_CHANNEL), row(2, channel=OVERVIEW_CHANNEL))) is False
    assert time_box(None, True) == 20 and time_box(None, False) is None


def test_the_naming_bridge_matches_a_focus_project_ignoring_case():
    """Proposed - Nic to confirm: sitreps name a project by its vault ID; a focus project saved as
    that ID, in any case, names it. Nic's plain word for a project names no sitrep, so it shows
    with no summary - which is how a mismatch shows. The same rule reaches saved tasks' links."""

    assert same_project(" Vault_GTBC_Plans ", GTBC) and not same_project("Exosuit", GTBC)
    task = sitrep("slice-8")
    preferences = (focus_preference("p1", ["Vault_GTBC_Plans", "vault_gtbc_plans", "Exosuit"]),)
    answer = below_top_line(overview(inputs(preferences=preferences, project_tasks=(summary(GTBC, task),)), NOW))
    assert [line.text for line in answer.lines] == [
        "Vault_GTBC_Plans — summary as of today at 2 pm",
        "Build slice 8 — next: Write the tests; last changed today",
        "Exosuit — no summary yet",
    ]
    assert answer.lines[1].reasons == ("focus project: Vault_GTBC_Plans", "already started")
    linked = item("a1", title="exosuit task", links=({"type": "about", "target": "VAULT_EXOSUIT"},))
    assert rank(wrap(linked), TODAY, focus_projects=frozenset({"vault_exosuit"}))[0].rules == ("focus project: vault_exosuit",)


def test_a_project_task_reference_stays_short_enough_to_answer_back():
    short = sitrep("slice-8")
    assert project_task_ref(short) == "project:vault_gtbc_plans:slice-8"
    long = sitrep("s" * 128, project="vault_" + "p" * 100)
    ref = project_task_ref(long)
    assert ref.startswith("project:") and len(ref) == len("project:") + 32
    assert ref == project_task_ref(long) != project_task_ref(sitrep("t" * 128, project="vault_" + "p" * 100))


# Work-model W7: memo 2.20 R rows, current shape rather than historical notes.
def w7_thing(suffix, title, **data):
    record = item(suffix, title=title)
    return replace(record, data={**record.data, **data})


def w7_fixture():
    exhibition = w7_thing("exhibition", "Exhibition permit")
    acknowledged = w7_thing("ack", "Acknowledge call-up", parent=exhibition.id, order=1)
    acknowledged = replace(acknowledged, state="done")
    memo = w7_thing("memo", "inspector's checklist", parent=exhibition.id, order=2,
                    dates={"not_before": {"day": "2026-10-08", "words": "venue inspection", "read_by": "code", "read_on": "2026-10-04"}})
    submit = w7_thing("submit", "Submit permit", parent=exhibition.id, order=3, after=[memo.id])
    send = w7_thing("send", "Send Morgan my résumé")
    edit = w7_thing("edit", "Edit my résumé", parent=send.id, order=1,
                    next_action={"text": "Update my latest role", "done": False}, timing={"padded_minutes": 15, "duration_minutes": 15})
    return exhibition, acknowledged, memo, submit, send, edit


def round2_fixture():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fixtures/work_model/moved_over.py"
    spec = importlib.util.spec_from_file_location("round2_moved_over", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture, fixture.moved_over_records()


@pytest.mark.parametrize("selection", ["planned", "window", "picked"])
def test_fixture_todays_work_never_falls_into_can_wait(selection):
    from brain_core import work

    fixture, records = round2_fixture()
    now = NOW.replace(day=2, hour=6)
    index = work.index_things(records)
    wanted = (fixture.SEND, fixture.CAMPAIGN, fixture.ExhibitCover, fixture.Printworks)
    versions = []
    for number, fid in enumerate(wanted):
        record = index[fid]
        planned = ({"day": "2026-10-02"} if selection == "planned" else
                   {"start": "2026-10-01", "end": "2026-10-03"})
        data = {**record.data, "dates": {"planned": {**planned, "words": "today",
                "read_by": "code", "read_on": "2026-10-02"}}} if selection != "picked" else record.data
        versions.append(replace(record, id=f"brn_round2_{number}",
                                supersedes=(*record.supersedes, record.id), data=data))
    answer = what_next(inputs(*records, *versions, now=now,
                             picked_today=frozenset(wanted) if selection == "picked" else frozenset()), now)
    choices = {answer.top_pick.record_id, *(row.record_id for row in answer.alternatives)}
    assert {fixture.EDIT, *wanted[1:]} <= choices
    assert not {fixture.EDIT, *wanted} & {row.record_id for row in answer.can_wait}
    edit = next(row for row in rank(wrap(*records, *versions), now.date(), now=now,
                                   picked_today=frozenset(wanted) if selection == "picked" else frozenset())
                if row.ref == fixture.EDIT)
    assert "you picked it for today" in edit.rules


@pytest.mark.parametrize("ranking", [RANKING_POINTS, RANKING_RULES])
@pytest.mark.parametrize("selection", ["planned", "window"])
def test_planned_work_before_4am_stays_in_his_day_and_out_of_can_wait(ranking, selection):
    now = datetime(2026, 10, 5, 3, 5, tzinfo=SINGAPORE)
    planned = ({"day": "2026-10-04"} if selection == "planned" else
               {"start": "2026-10-03", "end": "2026-10-04"})
    records = tuple(w7_thing(f"early-planned-{n}", f"Today's task {n}", dates={"planned": {
        **planned, "words": "today", "read_by": "code", "read_on": "2026-10-04"}}) for n in range(4))
    supplied = inputs(*records, now=now, ranking=ranking)
    wanted = {record.id for record in records}

    picture = overview(supplied, now)
    assert {row.item_ref for row in picture.lines if row.section == SECTION_PICKED_FOR_TODAY} == wanted
    assert not wanted & {row.item_ref for row in picture.lines if row.section == SECTION_NO_DATE_YET}
    answer = what_next(supplied, now)
    assert {answer.top_pick.record_id, *(row.record_id for row in answer.alternatives)} == wanted
    assert not answer.can_wait
    assert all("you picked it for today" in row.rules
               for row in rank(wrap(*records), now.date(), now=now, ranking=ranking))

    # Date commitments still change day at midnight, independently of planned work.
    appointed = item("early-calendar-appointment", due="2026-10-05", at="09:00")
    deadline = item("early-calendar-deadline", due="2026-10-05")
    overdue = item("early-calendar-overdue", due="2026-10-04")
    dated = overview(inputs(appointed, deadline, overdue, now=now, ranking=ranking), now)
    assert {row.item_ref for row in dated.lines if row.section == SECTION_TODAY} == {appointed.id, deadline.id}
    assert {row.item_ref for row in dated.lines if row.section == SECTION_MISSED} == {overdue.id}
    assert not what_next(inputs(appointed, now=now, ranking=ranking), now).missed


def test_fixture_event_day_wait_is_said_once_for_parts_and_their_parent():
    from brain_core import work

    fixture, records = round2_fixture()
    index = work.index_things(records)
    memo = index[fixture.MEMO]
    now = NOW.replace(day=2, hour=6)
    for supplied, fid in ((records, fixture.Exhibition), ((replace(memo, data={
            **memo.data, "parent": None}),), fixture.MEMO)):
        picture = overview(inputs(*supplied, now=now), now)
        (row,) = [row for row in picture.lines if row.item_ref == fid and row.section != SECTION_HIGHEST]
        assert "on Thu 8 Oct (venue inspection)" in row.text
        assert row.text.count("Thu 8 Oct") == 1
        assert "waiting until" not in row.text


@pytest.mark.parametrize("first_waits", [False, True])
def test_fixture_summary_has_one_next_per_parent_but_review_keeps_all_up_parts(first_waits):
    from brain_core import work
    from brain_core.review import MorningBootstrap, MorningInputs, build_morning

    fixture, records = round2_fixture()
    now = NOW.replace(day=2, hour=6)
    venue_rental = work.index_things(records)[fixture.VENUE_RENTAL]
    venue_rental = replace(venue_rental, id="brn_venue rental_confirmed", supersedes=(*venue_rental.supersedes, venue_rental.id),
                       data={**venue_rental.data, "stake": {**venue_rental.data["stake"], "confirmed": True},
                             "review_marks": []})
    parts = tuple(replace(item(f"round2_part_{n}", title=title), data={
        "level": "accepted", "size": "subtask", "parent": fixture.VENUE_RENTAL, "order": n,
        **({"needs": {"from": "the bank"}} if first_waits and n == 1 else {})})
        for n, title in enumerate(("a venue appointment", "a inspector's approval", "the co-organizer's signature"), 1))
    records = (*records, venue_rental, *parts)
    first = parts[1] if first_waits else parts[0]
    selected = frozenset({fixture.SEND, fixture.CAMPAIGN})
    picture = overview(inputs(*records, now=now, picked_today=selected), now)
    highest = [row for row in picture.lines if row.section == SECTION_HIGHEST]
    venue_rental_rows = [row for row in highest if row.item_ref in {part.id for part in parts}]
    assert len(venue_rental_rows) == 1 and highest[0].item_ref == first.id
    assert highest[0].text == f"{first.title} ({venue_rental.title}) — about 40 display slots a month; cost grows while you wait"
    assert highest[0].text.count("about 40 display slots a month") == 1
    assert {fixture.EDIT, fixture.CAMPAIGN} <= {row.item_ref for row in highest}
    answer = what_next(inputs(*records, now=now, picked_today=selected), now)
    assert answer.top_pick.record_id == first.id and answer.top_pick.belongs_to == venue_rental.title
    assert len([ref for ref in (answer.top_pick.record_id, *(row.record_id for row in answer.alternatives),
                               *(row.record_id for row in answer.can_wait)) if ref in {part.id for part in parts}]) == 1
    morning = build_morning(MorningInputs(wrap(*records), (), (), (), {}, now,
        bootstrap=MorningBootstrap("a" * 40, frozenset(work.index_things(records)))))
    day_refs = {row.item_ref for row in morning.parts[3].lines if row.rule == "your_day"}
    assert {part.id for part in parts if part is not parts[0] or not first_waits} <= day_refs


@pytest.mark.parametrize("ranking", [RANKING_POINTS, RANKING_RULES])
def test_missed_first_part_does_not_hide_its_actionable_sibling(ranking):
    parent = w7_thing("missed-parent", "Venue rental paperwork")
    missed_part = w7_thing("missed-first", "Venue appointment", parent=parent.id, order=1,
                          dates={"appointment": {"day": TODAY.isoformat(), "at": "09:00",
                                 "words": "today at 9 am", "read_by": "code", "read_on": TODAY.isoformat()}})
    ready_part = w7_thing("ready-second", "Get the co-organizer's signature", parent=parent.id, order=2)
    supplied = inputs(parent, missed_part, ready_part, ranking=ranking)

    answer = what_next(supplied, NOW)
    assert answer.not_enough is None
    assert answer.top_pick.record_id == ready_part.id
    assert answer.top_pick.belongs_to == parent.title
    assert [row.record_id for row in answer.missed] == [missed_part.id]
    assert not answer.alternatives and not answer.can_wait

    picture = overview(supplied, NOW)
    assert [row.item_ref for row in picture.lines if row.section == SECTION_HIGHEST] == [ready_part.id]
    assert any(row.section == SECTION_MISSED and "Venue appointment" in row.text for row in picture.lines)


def test_w7_what_next_picks_up_parts_and_shows_parent_progress_and_his_action():
    records = w7_fixture()
    answer = what_next(inputs(*records), NOW)
    pick = answer.top_pick
    assert pick.title == "Edit my résumé"
    assert pick.belongs_to == "Send Morgan my résumé"
    assert pick.progress["text"] == "0 of 1"
    assert pick.first_step == "Update my latest role"
    assert {pick.record_id, *(a.record_id for a in answer.alternatives)} == {records[-1].id}


def test_w7_where_am_i_folds_exhibition_into_one_progress_and_wait_line():
    records = w7_fixture()
    answer = overview(inputs(*records), NOW)
    detailed = [line for line in answer.lines if line.section != SECTION_HIGHEST]
    (line,) = [line for line in detailed if line.item_ref == records[0].id]
    assert "1 of 3" in line.text
    assert "inspector's checklist" in line.text
    assert "8 Oct" in line.text
    assert not {r.id for r in records[1:4]} & {line.item_ref for line in detailed}


def test_w7_on_appointment_day_memo_is_up_but_submit_still_waits():
    records = w7_fixture()
    on_day = NOW.replace(day=8)
    pick = what_next(inputs(*records[:4]), on_day).top_pick
    assert pick.title == "inspector's checklist"
    assert pick.belongs_to == "Exhibition permit"
    assert pick.progress["text"] == "1 of 3"
    assert pick.first_step == ""


def test_w7_old_notes_and_agent_worded_actions_never_supply_a_first_step():
    task = w7_thing("task", "Pause hall rental payments", next_action={"text": "Agent guessed step", "done": False},
                    review_marks=[{"mark": "agents_wording", "part": "next_action"}])
    old_note = OpenNote(session_id="old", task_id=task.id, next_step="Stale historical step", updated_at=NOW)
    pick = what_next(inputs(task, open_notes=(old_note,)), NOW).top_pick
    assert pick.title == task.title
    assert pick.first_step == ""


def test_w7_started_fact_counts_but_a_personal_note_alone_does_not():
    task = w7_thing("started", "Pause hall rental payments")
    note = OpenNote(session_id="old", task_id=task.id, next_step="Old next", updated_at=NOW)
    assert not in_progress(wrap(task), (note,), NOW)
    started = replace(task, data={**task.data, "started_at": NOW.isoformat()})
    (progress,) = in_progress(wrap(started), (note,), NOW)
    assert progress.record_id == task.id
    assert progress.next_step != "Old next"



def test_w7_r09_short_ask_filters_long_up_things_before_the_pin():
    from brain_core.blocks import Override
    long = w7_thing("long", "Venue rental", timing={"padded_minutes": 45, "duration_minutes": 45},
                    stake={"level": "changes_life", "confirmed": True, "at_stake": "40 display slots a month", "proposed_by": "fixture"})
    short = w7_thing("short", "Quick reply", timing={"padded_minutes": 15, "duration_minutes": 15})
    pick = what_next(inputs(long, short), NOW, override=Override(mode="minutes", words="got 15 mins free", minutes=15)).top_pick
    assert pick.title == "Quick reply"


def test_w7_explicit_minutes_in_a_block_offers_work_without_claiming_it_fits():
    from brain_core.blocks import Override

    block = CalendarEvent(title="Deep focus", start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))
    for duration in (None, 45):
        task = w7_thing("report", "Write the report", block="deep_focus",
                        timing={} if duration is None else {"padded_minutes": duration, "duration_minutes": duration})
        base = inputs(task, calendar_events=(block,))
        pick = what_next(replace(base, blocks=True), NOW,
                         override=Override(mode="minutes", words="I've got 20 minutes", minutes=20)).top_pick
        assert pick.record_id == task.id
        assert pick.first_step == ""
        assert pick.estimate_minutes == duration
        assert pick.time_box_minutes == 20
        assert pick.fields_used["gap_minutes"] == 20
        if duration is None:
            assert "fits_gap" not in pick.fields_used
        else:
            assert pick.fields_used["fits_gap"] is False



def started_fact(record, action=None, changed=NOW):
    data = {**record.data, "started_at": changed.isoformat()}
    if action is not None:
        data["next_action"] = {"text": action, "done": False}
    return replace(record, updated_at=changed, data=data)


def test_w7_review_inherited_deadline_matches_explicit_before_block_filter():
    deadline = {"day": TODAY.isoformat(), "words": "due today", "read_by": "code", "read_on": TODAY.isoformat()}
    parent = w7_thing("parent_due", "File the application", dates={"deadline": deadline})
    part = w7_thing("admin_part", "Send the form", parent=parent.id, block="admin")
    block = CalendarEvent(title="Deep focus", start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))
    inherited_inputs = replace(inputs(parent, part, calendar_events=(block,)), blocks=True)
    inherited = what_next(inherited_inputs, NOW).top_pick
    explicit = what_next(replace(inputs(parent, replace(part, data={**part.data, "dates": {"deadline": deadline}}),
                                       calendar_events=(block,)), blocks=True), NOW).top_pick
    assert inherited is not None and explicit is not None
    assert (inherited.record_id, inherited.rules, inherited.fields_used["due_at"]) == (
        explicit.record_id, explicit.rules, explicit.fields_used["due_at"])
    assert inherited.rules[0] == "due today, and there's no admin block before then"
    assert inherited.fields_used["wrong_block"] == "admin"
    (ranked_part,) = rank(wrap(parent, part), TODAY, now=NOW)
    assert ranked_part.item.sha256 == wrap(part)[0].sha256

    marked = replace(part, data={**part.data, "review_marks": [{"mark": "agents_wording", "part": "title"}]})
    assert what_next(replace(inputs(parent, marked, calendar_events=(block,)), blocks=True), NOW).top_pick is None
    ordinary = w7_thing("wrong_block", "Sort receipts", block="admin",
        stake={"level": "changes_life", "confirmed": True, "at_stake": "home", "proposed_by": "fixture"})
    assert what_next(replace(inputs(ordinary, calendar_events=(block,)), blocks=True), NOW).top_pick is None
    fits = w7_thing("right_block", "Write the report", block="deep_focus")
    assert what_next(replace(inputs(ordinary, fits, calendar_events=(block,)), blocks=True), NOW).top_pick.record_id == fits.id


def test_w7_review_ticked_parts_start_parents_in_both_asks_without_notes():
    records = []
    parents = []
    for number in range(3):
        parent = w7_thing(f"progress_parent_{number}", f"Application {number}")
        done = w7_thing(f"progress_done_{number}", "Ticked part", parent=parent.id, order=1)
        current = replace(done, id=done.id + "_v2", supersedes=(done.id,), state="done", updated_at=NOW)
        next_part = w7_thing(f"progress_next_{number}", "Remaining part", parent=parent.id, order=2)
        records.extend((parent, done, current, next_part))
        parents.append(parent)
    snapshot = wrap(*records)
    assert {entry.record_id for entry in in_progress(snapshot, (), NOW)} == {parent.id for parent in parents}
    answer = what_next(inputs(*records), NOW)
    assert {entry.record_id for entry in answer.in_progress} == {parent.id for parent in parents}
    picture = overview(inputs(*records), NOW)
    details = [line for line in picture.lines if line.section != SECTION_HIGHEST]
    for parent in parents:
        (line,) = [line for line in details if line.item_ref == parent.id]
        assert line.section == SECTION_IN_PROGRESS
        assert line.fields_used["progress"]["text"] == "1 of 2"
        assert line.fields_used["progress"]["started"] is True
        assert line.record_sha256 == wrap(parent)[0].sha256
    assert not {record.id for record in records if record.state == "done"} & {line.item_ref for line in details}

    unticked = tuple(replace(record, state="open") if record.state == "done" else record for record in records)
    notes = tuple(OpenNote(session_id=f"history-{parent.id}", task_id=parent.id, next_step="Old step", updated_at=NOW)
                  for parent in parents)
    assert in_progress(wrap(*unticked), notes, NOW) == ()
    assert what_next(inputs(*unticked, open_notes=notes), NOW).in_progress == ()


def test_w7_round2_successors_use_first_refs_with_current_raw_pins_in_both_asks():
    from brain_core import work
    from brain_core.placement import Plan, Slot, with_titles

    records = []
    current = {}
    for number in range(4):
        old = w7_thing(f"first_ref_{number}", f"Old title {number}")
        latest = replace(old, id=old.id + "_v2", supersedes=(old.id,), title=f"Current title {number}",
                         updated_at=NOW, data={**old.data, "started_at": NOW.isoformat()})
        if number == 0:
            latest = replace(latest, data={**latest.data, "stake": {
                "level": "changes_life", "confirmed": True, "at_stake": "home", "proposed_by": "fixture"}})
        records.extend((old, latest))
        current[old.id] = wrap(latest)[0]
    ranked = rank(wrap(*records), TODAY, now=NOW)
    assert {candidate.ref for candidate in ranked} == set(current)
    for candidate in ranked:
        assert candidate.item is current[candidate.ref] or candidate.item == current[candidate.ref]
        assert (candidate.item.path, candidate.item.sha256) == (current[candidate.ref].path, current[candidate.ref].sha256)
    answer = what_next(inputs(*records), NOW)
    assert answer.top_pick.record_id == records[0].id
    assert {answer.top_pick.record_id, *(entry.record_id for entry in answer.alternatives),
            *(entry.record_id for entry in answer.can_wait)} == set(current)
    assert {entry.record_id for entry in answer.in_progress} == set(current)
    assert all(entry.title == current[entry.record_id].record.title for entry in answer.in_progress)
    picture = overview(inputs(*records), NOW)
    details = [line for line in picture.lines if line.section != SECTION_HIGHEST]
    assert {line.item_ref for line in details} == set(current)
    assert len(details) == len(current)
    for line in picture.lines:
        assert (line.record_path, line.record_sha256) == (current[line.item_ref].path, current[line.item_ref].sha256)
    assert {work.first_id(entry.record): entry for entry in wrap(*records)}[answer.top_pick.record_id].sha256 == current[answer.top_pick.record_id].sha256
    plan = Plan(day=TODAY, slots=(Slot(key=answer.top_pick.record_id, kind="admin", start=NOW,
                                      end=NOW + timedelta(minutes=20), task_title=""),))
    titled = with_titles(plan, {candidate.ref: (candidate.title, None) for candidate in ranked})
    assert titled.slots[0].task_title == current[answer.top_pick.record_id].record.title


def test_w7_round2_parent_and_part_historical_aliases_fold_once():
    parent = w7_thing("first_parent", "Old application")
    current_parent = replace(parent, id=parent.id + "_v2", supersedes=(parent.id,), title="Application")
    part = w7_thing("first_part", "Old part", parent=current_parent.id, order=1)
    current_part = replace(part, id=part.id + "_v2", supersedes=(part.id,), title="Send form")
    completed = replace(w7_thing("first_done", "Done", parent=parent.id, order=2), state="done")
    records = (parent, current_parent, part, current_part, completed)
    answer = what_next(inputs(*records), NOW)
    assert answer.top_pick.record_id == part.id
    assert answer.top_pick.belongs_to == "Application"
    assert answer.top_pick.progress["text"] == "1 of 2"
    picture = overview(inputs(*records), NOW)
    details = [line for line in picture.lines if line.section != SECTION_HIGHEST]
    (line,) = [line for line in details if line.item_ref == parent.id]
    assert line.fields_used["next"]["item_ref"] == part.id
    assert line.fields_used["progress"]["text"] == "1 of 2"
    assert line.record_sha256 == wrap(current_parent)[0].sha256
    assert part.id not in {line.item_ref for line in details}


def test_w7_round2_moved_over_graph_has_unique_current_records_and_raw_pins():
    import importlib.util
    from pathlib import Path
    from brain_core import work

    path = Path(__file__).resolve().parents[1] / "fixtures/work_model/moved_over.py"
    spec = importlib.util.spec_from_file_location("w7_moved_over", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    records = fixture.moved_over_records()
    index = work.index_things(records)
    raw = {record.id: wrap(record)[0] for record in records}
    # The current record may also be supplied through an existing alias pin lookup.
    # Graph traversal must still see each first ID exactly once, in either ranking mode.
    aliases = tuple(raw[record.id].record for record in index.values())
    ranked = rank(wrap(*records, *aliases), TODAY, now=NOW)
    assert len({candidate.ref for candidate in ranked}) == len(ranked)
    assert {candidate.ref for candidate in ranked} == {work.first_id(record) for record in work.up_things(index, NOW)}
    for candidate in ranked:
        current = raw[index[candidate.ref].id]
        assert candidate.item == current
        assert candidate.item.sha256 == current.sha256
    assert work.progress_of(fixture.Exhibition, index)["text"] == "1 of 3"
    rules = rank(wrap(*records), TODAY, now=NOW, ranking=RANKING_RULES)
    assert {candidate.ref for candidate in rules} == {candidate.ref for candidate in ranked}
    assert what_next(inputs(*records), NOW).top_pick is not None
    assert overview(inputs(*records), NOW).lines


def _w7_date(day):
    return {"day": day.isoformat(), "words": "fixture", "read_by": "code", "read_on": TODAY.isoformat()}


def _w7_inherited_fixture(parent_dates, *, marked=False):
    """A parent with ``parent_dates``, its one part (saved twice, so its lasting ID is its first
    version's) with no date of its own, and three picked fillers due today that outrank it."""

    parent = w7_thing("inh_parent", "File the application", called="project", dates=parent_dates)
    first = w7_thing("inh_part", "Send the form", parent=parent.id, order=1)
    marks = {"review_marks": [{"mark": "agents_wording", "part": "title"}]} if marked else {}
    part = replace(first, id=first.id + "_v2", supersedes=(first.id,), data={**first.data, **marks})
    fillers = tuple(w7_thing(f"inh_filler_{number}", f"Filler {number}", dates={"deadline": _w7_date(TODAY)})
                    for number in range(3))
    fillers = tuple(replace(filler, created_at=NOW - timedelta(days=30), updated_at=NOW - timedelta(days=30))
                    for filler in fillers)
    return parent, first, part, fillers


def _w7_with_own(part, dates):
    return replace(part, data={**part.data, "dates": dates})


def _w7_part_views(parent, first, part, fillers, ranking):
    records = (parent, first, part, *fillers)
    picked = frozenset(filler.id for filler in fillers)
    answer = what_next(inputs(*records, picked_today=picked, ranking=ranking), NOW)
    (waits,) = [entry for entry in answer.can_wait if entry.record_id == first.id]
    picture = overview(inputs(*records, picked_today=picked, ranking=ranking), NOW)
    (line,) = [line for line in picture.lines if line.item_ref == first.id and line.section != SECTION_HIGHEST]
    return waits, line


def test_w7_inherited_deadline_displays_as_the_explicit_one_in_both_asks_and_both_rankings():
    """Review P2 (orientation.py:368): date display rebuilt a one-item index, so a part under a
    project due today read "no date set yet" in can wait and landed in "No date yet" while
    "Highest right now" scored it due today. Display and classification now read the same
    complete reader index as ranking; the raw record, path and hash stay the audit pins."""

    for ranking in (RANKING_POINTS, RANKING_RULES):
        for day, section, reason, clause in (
            (TODAY, SECTION_TODAY, "also due today", "due today"),
            (TODAY + timedelta(days=2), SECTION_COMING_UP, "due Tue 6 Oct", "due Tue 6 Oct"),
        ):
            parent, first, part, fillers = _w7_inherited_fixture({"deadline": _w7_date(day)})
            own = _w7_with_own(part, {"deadline": _w7_date(day)})
            inherited = _w7_part_views(parent, first, part, fillers, ranking)
            explicit = _w7_part_views(parent, first, own, fillers, ranking)
            for (waits, line), raw in ((inherited, part), (explicit, own)):
                assert waits.reason == reason, (ranking, day)
                assert line.section == section, (ranking, day)
                assert line.text == f"Send the form — {clause}"
                # Canonical lasting ID, and the raw newest version's own audit pins.
                assert line.item_ref == first.id
                assert (line.record_path, line.record_sha256) == (wrap(raw)[0].path, wrap(raw)[0].sha256)
            assert inherited[0].reason == explicit[0].reason
            assert (inherited[1].section, inherited[1].text) == (explicit[1].section, explicit[1].text)
            # The raw part never gained the parent's date: no copied or mapped record leaks out.
            assert "dates" not in part.data


def test_w7_inherited_display_keeps_planned_apart_and_skips_marked_parts():
    for ranking in (RANKING_POINTS, RANKING_RULES):
        # A parent's plan selects its part for today without copying a deadline.
        parent, first, part, fillers = _w7_inherited_fixture({"planned": _w7_date(TODAY)})
        records = (parent, first, part, *fillers)
        picked = frozenset(filler.id for filler in fillers)
        answer = what_next(inputs(*records, picked_today=picked, ranking=ranking), NOW)
        assert first.id in {answer.top_pick.record_id, *(row.record_id for row in answer.alternatives)}
        assert first.id not in {row.record_id for row in answer.can_wait}
        picture = overview(inputs(*records, picked_today=picked, ranking=ranking), NOW)
        assert {line.section for line in picture.lines if line.item_ref == first.id
                and line.section != SECTION_HIGHEST} == {SECTION_PICKED_FOR_TODAY}
        assert "dates" not in part.data
        # A part's own planned day picks it for today; the inherited deadline still reads as due.
        parent, first, part, fillers = _w7_inherited_fixture({"deadline": _w7_date(TODAY + timedelta(days=2))})
        planned = _w7_with_own(part, {"planned": _w7_date(TODAY)})
        records = (parent, first, planned, *fillers)
        picked = frozenset(filler.id for filler in fillers)
        picture = overview(inputs(*records, picked_today=picked, ranking=ranking), NOW)
        sections = {line.section for line in picture.lines if line.item_ref == first.id and line.section != SECTION_HIGHEST}
        assert sections == {SECTION_PICKED_FOR_TODAY}, ranking
        answer = what_next(inputs(*records, picked_today=picked, ranking=ranking), NOW)
        assert first.id in {answer.top_pick.record_id, *(row.record_id for row in answer.alternatives)}
        assert first.id not in {row.record_id for row in answer.can_wait}
        from brain_core import work
        from brain_core.orientation import _can_wait_reason
        assert _can_wait_reason(planned, TODAY, work.index_things(records)) == "due Tue 6 Oct"
        # A review-marked part inherits no deadline, in display as in ranking.
        parent, first, part, fillers = _w7_inherited_fixture({"deadline": _w7_date(TODAY)}, marked=True)
        waits, line = _w7_part_views(parent, first, part, fillers, ranking)
        assert (waits.reason, line.section) == ("no date set yet", SECTION_NO_DATE_YET), ranking
        assert (line.record_path, line.record_sha256) == (wrap(part)[0].path, wrap(part)[0].sha256)
