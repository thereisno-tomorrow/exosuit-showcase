"""FlowSavvy reads change orientation only in the new, explicit schedule mode."""

from dataclasses import replace
from datetime import timedelta
import pytest

from brain_core.orientation import SECTION_NOW, overview, what_next
from brain_core.review import CalendarEvent
from test_orientation import inputs, item
from test_orientation_plan import TODAY_AT, _records, slot


def scheduled(base, *slots):
    return replace(base, schedule_mode=True, plan_slots=slots)


def test_schedule_now_leads_even_when_another_item_ranks_higher():
    urgent, plain = _records()
    answer = what_next(scheduled(inputs(urgent, plain), slot(plain, "09:45", "10:15")), TODAY_AT)
    assert answer.top_pick.record_id == plain.id
    assert answer.top_pick.rules[0] == "scheduled for now"
    assert urgent.id in {entry.record_id for entry in answer.alternatives}


def test_schedule_mode_never_asks_about_elapsed_forecast_slots():
    plain = item("schedule-past", title="Tidy the desk", padded_minutes=30)
    answer = what_next(scheduled(inputs(plain), slot(plain, "09:00", "09:30")), TODAY_AT)
    assert answer.top_pick.record_id == plain.id
    assert answer.missed == ()


def test_schedule_mode_ignores_old_block_filters():
    urgent, plain = _records()
    block = CalendarEvent(title="Deep focus", start=TODAY_AT - timedelta(minutes=30), end=TODAY_AT + timedelta(hours=1))
    base = replace(inputs(urgent, plain, calendar_events=(block,)), blocks=True)
    answer = what_next(scheduled(base, slot(plain, "09:45", "10:15")), TODAY_AT)
    assert answer.top_pick.record_id == plain.id
    assert answer.moment is None
    assert not any("block" in entry.reason for entry in answer.can_wait)


def test_now_and_next_are_shown_using_schedule_words():
    urgent, plain = _records()
    data = scheduled(inputs(urgent, plain), slot(plain, "09:45", "10:15"), slot(urgent, "10:30", "11:00"))
    line = next(line for line in overview(data, TODAY_AT).lines if line.section == SECTION_NOW)
    assert line.text == "Scheduled now: Tidy the desk until 10:15 am; next: File the tax form at 10:30 am"
    assert what_next(data, TODAY_AT).plan_note == "Next scheduled: File the tax form at 10:30 am."


def test_no_schedule_now_keeps_ranking_and_names_the_next_slot():
    urgent, plain = _records()
    data = scheduled(inputs(urgent, plain), slot(plain, "11:00", "11:30"))
    answer = what_next(data, TODAY_AT)
    assert answer.top_pick.record_id == urgent.id
    assert answer.plan_note == "Next scheduled: Tidy the desk at 11 am."
    assert next(line for line in overview(data, TODAY_AT).lines if line.section == SECTION_NOW).text == "Nothing scheduled now; next: Tidy the desk at 11 am"


def test_unavailable_snapshot_says_so_and_falls_back_to_ranked_work():
    urgent, plain = _records()
    data = replace(inputs(urgent, plain), schedule_mode=True, plan_slots=None)
    answer = what_next(data, TODAY_AT)
    assert answer.top_pick.record_id == urgent.id
    assert answer.plan_note == "Couldn't see the schedule"
    assert any(line.text == "Couldn't see the schedule" for line in overview(data, TODAY_AT).lines)


def test_a_finished_task_is_never_current_or_next_in_a_stale_snapshot():
    urgent, plain = _records()
    done = replace(plain, state="done")
    data = scheduled(inputs(urgent, done), slot(done, "09:45", "10:15"), slot(done, "11:00", "11:30"))
    answer = what_next(data, TODAY_AT)
    assert answer.top_pick.record_id == urgent.id
    assert answer.plan_note is None
    assert all("Tidy the desk" not in line.text for line in overview(data, TODAY_AT).lines if line.section == SECTION_NOW)


def test_next_scheduled_can_be_a_future_dated_task():
    urgent, plain = _records()
    plain = replace(plain, data={"level": "accepted", "dates": {"not_before": {"day": "2026-10-05"}}})
    tomorrow = replace(slot(plain, "09:00", "09:30"),
                       start=TODAY_AT.replace(day=5, hour=9), end=TODAY_AT.replace(day=5, hour=9, minute=30))
    answer = what_next(scheduled(inputs(urgent, plain), tomorrow), TODAY_AT)
    assert answer.top_pick.record_id == urgent.id
    assert answer.plan_note == "Next scheduled: Tidy the desk on Mon 05 Oct at 9 am."


def test_scheduled_step_replaces_its_sibling_in_the_one_next_per_project_list():
    parent = item("schedule-project", title="Refresh the office")
    parent = replace(parent, data={"level": "accepted", "called": "project"})
    first = item("schedule-step1", title="Buy shelves")
    second = item("schedule-step2", title="Measure the desk")
    first = replace(first, data={"level": "accepted", "parent": parent.id, "order": 1})
    second = replace(second, data={"level": "accepted", "parent": parent.id, "order": 2})
    answer = what_next(scheduled(inputs(parent, first, second), slot(second, "09:45", "10:15")), TODAY_AT)
    assert answer.top_pick.record_id == second.id
    assert first.id not in {row.record_id for row in answer.alternatives}


@pytest.mark.parametrize("waiting", [
    {"needs": {"from": "Synthetic colleague", "what": "A response"}},
    {"whose": "Synthetic colleague"},
    {"state": "waiting"},
    {"dates": {"not_before": {"day": "2026-10-06"}}},
    {"dates": {"planned": {"start": "2026-10-06", "end": "2026-10-08"}}},
    {"after": ["synthetic-prerequisite"]},
])
@pytest.mark.parametrize("future", [False, True])
def test_fresh_ancestor_wait_invalidates_stale_schedule(waiting, future):
    parent = item("schedule-parent", title="Office refresh")
    state = waiting.get("state", "open")
    fields = {key: value for key, value in waiting.items() if key != "state"}
    parent = replace(parent, state=state, data={"level": "accepted", "called": "project", **fields})
    child = item("schedule-child", title="Measure shelves")
    child = replace(child, data={"level": "accepted", "parent": parent.id})
    block = slot(child, "11:00", "11:30") if future else slot(child, "09:45", "10:15")
    result = what_next(scheduled(inputs(parent, child), block), TODAY_AT)
    assert result.plan_note is None
    assert result.top_pick is None


def test_future_forecast_can_follow_a_schedulable_parent_prerequisite():
    predecessor = item("schedule-before", title="Buy shelves", padded_minutes=30)
    predecessor = replace(predecessor, data={**predecessor.data,
        "timing": {"duration_minutes": 30, "padded_minutes": 30, "estimated_by": "nic"}})
    parent = item("schedule-parent", title="Office refresh")
    parent = replace(parent, data={"level": "accepted", "called": "project", "after": [predecessor.id]})
    child = item("schedule-child", title="Measure shelves", padded_minutes=30)
    child = replace(child, data={**child.data, "parent": parent.id})
    future = what_next(scheduled(inputs(parent, child, predecessor), slot(child, "11:00", "11:30")), TODAY_AT)
    assert future.plan_note == "Next scheduled: Measure shelves at 11 am."
    assert future.top_pick.record_id == predecessor.id


def test_unsized_prerequisite_invalidates_the_dependent_future_forecast():
    predecessor = item("schedule-before", title="Buy shelves")
    child = item("schedule-child", title="Install shelves", padded_minutes=30)
    child = replace(child, data={**child.data, "after": [predecessor.id]})
    answer = what_next(scheduled(inputs(predecessor, child), slot(child, "11:00", "11:30")), TODAY_AT)
    assert answer.plan_note is None


@pytest.mark.parametrize("future", [False, True])
def test_waiting_earlier_step_holds_later_step_forecast(future):
    parent = item("schedule-task", title="Build shelves")
    first = item("schedule-part1", title="Buy shelves", padded_minutes=30)
    second = item("schedule-part2", title="Install shelves", padded_minutes=30)
    first = replace(first, data={**first.data, "parent": parent.id, "order": 1,
        "needs": {"from": "Synthetic colleague", "what": "Response"}})
    second = replace(second, data={**second.data, "parent": parent.id, "order": 2})
    forecast = slot(second, "11:00", "11:30") if future else slot(second, "09:45", "10:15")
    answer = what_next(scheduled(inputs(parent, first, second), forecast), TODAY_AT)
    assert answer.plan_note is None and answer.top_pick is None


@pytest.mark.parametrize("minutes,estimate", [(1440, "nic"), (960, "agent:test")])
def test_oversized_task_ignores_stale_schedule_and_asks_owner_to_split(minutes, estimate):
    large = item("schedule-large", title="Long synthetic task")
    large = replace(large, data={**large.data, "timing": {"duration_minutes": minutes, "estimated_by": estimate}})
    base = inputs(large)
    data = scheduled(base, slot(large, "09:45", "10:15"), slot(large, "11:00", "11:30"))
    answer = what_next(data, TODAY_AT)
    assert answer.top_pick.record_id == large.id
    assert "scheduled for now" not in answer.top_pick.rules
    assert answer.top_pick.fields_used["schedule_issue"] == "needs_split"
    assert answer.plan_note == "Not scheduled · split this into shorter tasks"
    overview_lines = overview(data, TODAY_AT).lines
    assert any("split this into shorter tasks" in line.text for line in overview_lines if line.item_ref == large.id)
    assert next(line for line in overview_lines if line.section == SECTION_NOW).text == "Nothing scheduled now"
    assert "split this" not in str(what_next(base, TODAY_AT))
    assert "split this" not in str(overview(base, TODAY_AT))


def test_oversized_prerequisite_removes_dependent_future_forecast():
    first = item("schedule-large", title="Long synthetic task")
    first = replace(first, data={**first.data, "timing": {"duration_minutes": 1440, "estimated_by": "nic"}})
    second = item("schedule-second", title="Next synthetic task")
    second = replace(second, data={**second.data, "after": [first.id], "timing": {"duration_minutes": 30, "estimated_by": "nic"}})
    answer = what_next(scheduled(inputs(first, second), slot(second, "11:00", "11:30")), TODAY_AT)
    assert "Next scheduled:" not in answer.plan_note
    assert answer.top_pick.record_id == first.id


@pytest.mark.parametrize("kind", ["container", "appointment", "routine", "goal", "project"])
def test_non_task_lengths_do_not_produce_split_notices(kind):
    large = item("schedule-nontask", title="Synthetic long item")
    data = {"level": "accepted", "timing": {"duration_minutes": 1440, "estimated_by": "nic"}}
    if kind == "appointment":
        data["dates"] = {"appointment": {"day": "2026-10-04", "at": "11:00"}}
    elif kind == "routine":
        data["repeat"] = "daily"
    elif kind in {"goal", "project"}:
        data["called"] = kind
    large = replace(large, data=data)
    records = [large]
    if kind == "container":
        child = item("schedule-finished-step", title="Synthetic completed step", state="done")
        records.append(replace(child, data={"level": "accepted", "parent": large.id,
                               "timing": {"duration_minutes": 30, "estimated_by": "nic"}}))
    data = scheduled(inputs(*records))
    assert "split this" not in str(what_next(data, TODAY_AT))
    assert "split this" not in str(overview(data, TODAY_AT))


def test_canonical_routine_does_not_keep_a_stale_forecast():
    routine = item("schedule-routine", title="Synthetic routine")
    routine = replace(routine, data={"level": "accepted", "repeat": "daily",
                                    "timing": {"duration_minutes": 30, "estimated_by": "nic"}})
    data = scheduled(inputs(routine), slot(routine, "09:45", "10:15"), slot(routine, "11:00", "11:30"))
    line = next(line for line in overview(data, TODAY_AT).lines if line.section == SECTION_NOW)
    assert line.text == "Nothing scheduled now"
