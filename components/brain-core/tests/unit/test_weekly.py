"""The weekly review packet (Ciel and Brain Home PRD §4.8; acceptance case 13)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import replace
import importlib.util
from pathlib import Path

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import CalendarEvent, HistoryRow, ItemRecord, OpenRequest
from brain_core.sitreps import ProjectTasks, TaskSitrep
from brain_core.timewords import SINGAPORE
from brain_core.weekly import PART_CAP, PARTS, TITLES, weekly_review

# Friday 9 Oct 2026, 15:00 in Singapore: the weekly review's afternoon.
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=SINGAPORE)


def item(
    suffix: str,
    title: str,
    *,
    state: str = "open",
    level: str = "accepted",
    saved: datetime = NOW - timedelta(days=2),
    updated: datetime | None = None,
    links: tuple[dict[str, str], ...] = (),
    supersedes: tuple[str, ...] = (),
    source: dict[str, object] | None = None,
    **data: object,
) -> ItemRecord:
    values: dict[str, object] = {"level": level, "size": "task", **data}
    if state == "done":
        values["check"] = {"locator": "said so", "checked_by": "nic"}
    record = Record(
        schema=RECORD_SCHEMA_V2, id=f"brn_{saved:%Y%m%d}_000000_{suffix}", kind="item", title=title, state=state,
        created_at=saved, updated_at=updated or saved, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=supersedes, dedupe_key=None, data=values,
        body="", source=source or {"said_by": "owner"}, links=links,
    )
    return ItemRecord(record=record, path=f"records/item/{record.id}.md")


def lines(review, part: str) -> list[str]:
    return [line.text for line in review.lines if line.part == part]


def test_the_packet_has_the_prds_parts_in_order():
    """Acceptance case 13's packet: finished, missed deadlines, unanswered needs-you, waiting by person, all goals and
    projects, routines, next week; ideas are opt-in."""

    done = item("a001", "Send the invoice", state="done", block="admin", updated=NOW - timedelta(days=2))
    old_done = item("a002", "Old thing", state="done", block="admin", updated=NOW - timedelta(days=9))
    late = item("a003", "Pay the phone bill", block="admin", timing={"due": "2026-10-05"})
    unsorted = item("a004", "Buy light bulbs")
    suggestion = item("a005", "Try the new gym", level="suggested", review_marks=[{"mark": "question", "text": "Try the new gym?"}])
    waiting = item("a006", "Get the quote", state="waiting", block="admin", links=({"type": "waiting_on", "target": "Sam"},))
    request = OpenRequest(id="gt_req_1", handed_to="fleet-boss", since=date(2026, 10, 7), objective="Research roles")
    goal = item("a007", "Move into robotics", size="goal", saved=NOW - timedelta(days=20))
    next_week = item("a008", "File taxes", block="deep_focus", timing={"due": "2026-10-14"})
    far = item("a009", "Renew passport", block="errands", timing={"due": "2026-12-01"})
    routine_start = NOW - timedelta(days=40)
    routine = item("a00a", "Laundry", block="errands", repeat="weekly:sun", saved=routine_start)
    review = weekly_review(
        (done, old_done, late, unsorted, suggestion, waiting, goal, next_week, far, routine), NOW,
        open_requests=(request,),
    )
    assert review.week_start == date(2026, 10, 3) and review.week_end == date(2026, 10, 9)
    assert review.parts == tuple(part for part in PARTS if part not in ("ideas", "parked"))
    assert lines(review, "finished") == ["Send the invoice — done Wed 7 Oct"]
    assert lines(review, "overdue") == ["Pay the phone bill — missed since Mon 5 Oct (4 days)"]
    assert lines(review, "needs_you") == ["Try the new gym — Try the new gym?"]
    assert "unsorted" not in review.parts
    assert lines(review, "waiting_by_person") == ["fleet-boss: Research roles", "Sam: Get the quote"]
    assert lines(review, "goals_and_projects") == ["Move into robotics — 0 of 0; next: Move into robotics (not an actionable task); still want it?"]
    # First saved on Sun 30 Aug: six Sundays are over, none done.
    assert lines(review, "routines") == ["Laundry — streak 0; done 0 of the last 6 (0%); slipping"]
    assert lines(review, "next_week") == ["File taxes — due Wed 14 Oct"]
    assert TITLES["goals_and_projects"].startswith("Goals and projects")


def test_a_strong_routine_and_a_goal_with_a_recent_step_are_shown_right():
    start = NOW - timedelta(days=36)  # Sat 3 Sep: five Sundays are over (6 Sep to 4 Oct)
    versions = [item("b000", "Laundry", block="errands", repeat="weekly:sun", saved=start)]
    for index, day in enumerate(("2026-09-06", "2026-09-13", "2026-09-20", "2026-09-27", "2026-10-04"), start=1):
        at = datetime.fromisoformat(day).replace(hour=19, tzinfo=SINGAPORE)
        versions.append(item(f"b{index:03x}", "Laundry", block="errands", repeat="weekly:sun", saved=at,
                             occurrence={"day": day, "outcome": "done"},
                             supersedes=tuple(version.record.id for version in versions)))
    goal = item("c000", "Clear the house", size="goal", saved=NOW - timedelta(days=30))
    step = item("c001", "Buy boxes", block="errands", links=({"type": "part_of", "target": goal.record.id},),
                saved=NOW - timedelta(days=1))
    review = weekly_review((*versions, goal, step), NOW)
    assert lines(review, "routines") == ["Laundry — streak 5; done 5 of the last 5 (100%); strong"]
    assert len(lines(review, "goals_and_projects")) == 1
    assert "Buy boxes" in lines(review, "goals_and_projects")[0]


def test_a_focus_project_is_checked_only_while_its_sitreps_are_read():
    focus = Record(
        schema=RECORD_SCHEMA_V2, id="brn_20261001_000000_f000", kind="preference", title="Focus", state="active",
        created_at=NOW - timedelta(days=8), updated_at=NOW - timedelta(days=8), assertion=None, confidence=None,
        sensitivity="private", vaults=(), owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None,
        data={"focus_projects": ["vault_exosuit", "job-search"]}, body="",
        source={"said_by": "owner", "came_by": "direct", "words": "my focus"}, links=(),
    )
    busy = ProjectTasks(project="vault_exosuit", as_of=NOW, tasks=(
        TaskSitrep(project="vault_exosuit", session_id="s1", intent="Ship", state="open", next_step="Merge", blockers=(),
                   note_updated_at=NOW - timedelta(days=1), note_record_id="brn_20261008_000000_ab12",
                   note_content_sha256="c" * 64),
    ))
    review = weekly_review((), NOW, preferences=(focus,), project_tasks=(busy,))
    assert lines(review, "goals_and_projects") == [
        "vault_exosuit — 0 of 1; next: Merge; still want it?",
        "job-search — 0 of 0; next: nothing next; still want it?",
    ]
    assert lines(weekly_review((), NOW, preferences=(focus,)), "goals_and_projects") == []


def test_each_part_stops_at_ten_then_says_how_many_more():
    late = tuple(item(f"d{index:03x}", f"Late {index:02d}", block="admin", timing={"due": "2026-10-01"}) for index in range(12))
    overdue = lines(weekly_review(late, NOW), "overdue")
    assert len(overdue) == PART_CAP + 1 and overdue[-1] == "2 more; ask to see them"


def test_every_goal_and_project_once_even_over_the_old_cap_and_parked():
    items = tuple(item(f"e{n:03x}", f"Project {n}", called="project", state="parked" if n == 0 else "open")
                  for n in range(12)) + (item("e100", "Goal", called="goal"),)
    review = weekly_review(items, NOW)
    entries = [line for line in review.lines if line.part == "goals_and_projects"]
    assert len(entries) == 13
    assert len({line.item_ref for line in entries}) == 13
    assert all("0 of 0" in line.text and "next:" in line.text and "still want it?" in line.text for line in entries)


def test_needs_you_is_unanswered_not_unsorted_and_keeps_original_hash():
    marked = item("f001", "Ask me", review_marks=[{"mark": "question", "text": "Keep this?"}])
    unsorted = item("f002", "Thin task")
    row = HistoryRow(item_ref=marked.record.id, channel="review", rule="needs_you", record_sha256=marked.sha256,
                     record_state="open", surfaced_at=NOW, response="yes", responded_at=NOW)
    assert lines(weekly_review((marked, unsorted), NOW), "needs_you") == ["Ask me — Keep this?"]
    assert not lines(weekly_review((marked, unsorted), NOW, history=(row,)), "needs_you")
    changed = ItemRecord(replace(marked.record, title="Ask something new"), marked.path)
    assert lines(weekly_review((changed,), NOW, history=(row,)), "needs_you")


def test_needs_you_addresses_you_and_separates_formatted_questions():
    marked = item("f003", "Task", review_marks=[{"mark": "question", "text": "Its finish line, in his words"}],
                  stake={"level": "nothing_much", "confirmed": False, "proposed_by": "fixture"})
    before = marked.record.as_dict()
    assert lines(weekly_review((marked,), NOW), "needs_you") == [
        "Task — Its finish line, in your words? · Keep proposed stake: nothing much?",
    ]
    assert marked.record.as_dict() == before


def test_stale_request_boundary_once_and_proof_closed_excluded():
    stale = OpenRequest("stale", "agent", NOW.date() - timedelta(days=14), "Old request", last_activity=NOW - timedelta(days=14))
    fresh = replace(stale, id="fresh", objective="Fresh", last_activity=NOW - timedelta(days=14) + timedelta(seconds=1))
    closed = replace(stale, id="closed", work_state="result_returned", proof="https://example.test/proof")
    first = weekly_review((), NOW, open_requests=(stale, fresh, closed))
    assert lines(first, "waiting_by_person") == ["agent: Fresh", "agent: Old request — stale"]
    row = HistoryRow("stale", "review", "stale_request", "", None, NOW - timedelta(days=100))
    assert lines(weekly_review((), NOW, open_requests=(stale,), history=(row,)), "waiting_by_person") == []


def test_ideas_require_yes_owner_words_and_paraphrases_are_labelled():
    own = item("1001", "Agent changed title", level="possible", source={"said_by": "owner", "words": "my own idea"})
    paraphrase = item("1002", "Saved agent wording", level="possible", source={"said_by": "agent", "words": "untrusted attribution"})
    assert not lines(weekly_review((own, paraphrase), NOW), "ideas")
    assert lines(weekly_review((own, paraphrase), NOW, ideas=True), "ideas") == [
        "my own idea", "Agent's paraphrase (needs your words): Saved agent wording",
    ]


def test_separate_next_week_dates_and_calendar_appointments_only():
    dates = item("2001", "Dates", dates={"deadline": {"day": "2026-10-12"},
                                         "appointment": {"day": "2026-10-13", "at": "10:00"},
                                         "planned": {"day": "2026-10-14"}, "start_by": {"day": "2026-10-15"}})
    event = CalendarEvent("Dentist", NOW + timedelta(days=2), NOW + timedelta(days=2, hours=1))
    review = weekly_review((dates,), NOW, calendar_events=(event,))
    assert lines(review, "next_week") == ["Dentist — appointment this Sunday, 11 Oct", "Dates — due Mon 12 Oct",
                                         "Dates — appointment Tue 13 Oct at 10 am"]


def test_frozen_packet_over_shared_moved_over_fixture():
    path = Path(__file__).resolve().parents[1] / "fixtures/work_model/moved_over.py"
    spec = importlib.util.spec_from_file_location("weekly_moved_over", path)
    assert spec and spec.loader
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    records = fixture.moved_over_records()
    before = [record.as_dict() for record in records]
    wrapped = tuple(ItemRecord(record, f"records/item/{record.id}.md") for record in records)
    at = datetime(2026, 10, 2, 15, tzinfo=SINGAPORE)
    review = weekly_review(wrapped, at)
    assert any(line.item_ref == fixture.OFFICER for line in review.lines if line.part == "finished")
    assert any(line.item_ref == fixture.SEND for line in review.lines if line.part == "needs_you")
    assert not lines(review, "ideas")
    assert set(review.parts) <= set(PARTS)
    assert {"unsorted", "quiet_or_no_next_action"}.isdisjoint(review.parts)
    shown = weekly_review(wrapped, at, ideas=True)
    assert len(lines(shown, "ideas")) == 14
    assert all("Agent's paraphrase" in text for text in lines(shown, "ideas"))
    assert [record.as_dict() for record in records] == before

    # Extra context is synthetic: the audited fixture does not assert a goal/project call,
    # a missed deadline or a next-week calendar event. Never invent audited titles for them.
    goal = item("3001", "Context goal", called="goal")
    project = item("3002", "Context project", called="project", parent=goal.record.id)
    part = item("3003", "Context part", parent=project.record.id, whose="Rowan")
    late = item("3004", "Context deadline", dates={"deadline": {"day": "2026-10-01"}})
    future = item("3005", "Context next week", dates={"appointment": {"day": "2026-10-05", "at": "09:00"}})
    stale = OpenRequest("context-stale", "context-agent", date(2026, 9, 1), "Context idle request",
                        last_activity=at - timedelta(days=14))
    slipping = item("3006", "Context slipping routine", repeat="daily", saved=at - timedelta(days=20))
    strong = [item("4000", "Context strong routine", repeat="daily", saved=at - timedelta(days=10))]
    for n in range(1, 11):
        day = at - timedelta(days=11 - n)
        strong.append(item(f"4{n:03x}", "Context strong routine", repeat="daily", saved=day,
                           occurrence={"day": day.date().isoformat(), "outcome": "done"},
                           supersedes=tuple(entry.record.id for entry in strong)))
    parked = item("5000", "Context parked", state="parked", parked_until={"event": "someday"})
    complete = weekly_review((*wrapped, goal, project, part, late, future, slipping, *strong, parked), at,
                             ideas=True, open_requests=(stale,))
    assert complete.parts == PARTS
    assert lines(complete, "parked") == ["Context parked — parked (someday)"]
    goals = [line for line in complete.lines if line.part == "goals_and_projects"]
    assert {line.item_ref for line in goals} == {goal.record.id, project.record.id}
    assert all("0 of 1" in line.text and "Context part" in line.text and "Rowan" in line.text
               and "still want it?" in line.text for line in goals)
    assert any(line.item_ref == stale.id and "stale" in line.text for line in complete.lines)
    assert any("slipping" in text for text in lines(complete, "routines"))
    assert any("strong" in text for text in lines(complete, "routines"))
