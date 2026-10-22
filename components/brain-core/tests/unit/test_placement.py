"""Step I, the placement engine (build plan slice 2): `brain_core.placement`, pure.

One test or more per rule in the spec's §2 and §3. The day is Thursday 1 Oct 2026, Singapore time,
with the working window 9 to 6 unless a test says otherwise."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from brain_core.blocks import ADMIN, DEEP_FOCUS, ERRANDS, block_kind
from brain_core.placement import (
    DAY_OFF,
    DOESNT_FIT,
    NO_LENGTH,
    SLOT_PREFIX,
    WAITS_ON,
    Busy,
    DayBlock,
    DayChanges,
    Plan,
    Rules,
    Scope,
    Slot,
    Work,
    draw_day,
    free_time,
    plan_summary,
    replan,
    scope,
    window_for,
)
from brain_core.language import DEFAULT_WORKING_WINDOW
from brain_core.records import RECORD_SCHEMA_V2, Evidence, Record
from brain_core.review import ItemRecord
from brain_core.timewords import SINGAPORE

DAY = date(2026, 10, 1)  # a Thursday
NINE = datetime(2026, 10, 1, 8, 0, tzinfo=SINGAPORE)  # "now", before the window opens
WINDOW = ("09:00", "18:00")
EVIDENCE = (Evidence(source_type="session_message", locator="session:test:1", content_sha256="a" * 64, excerpt="fixture"),)


def at(clock: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=SINGAPORE)


def work(key: str, kind: str, minutes: int, *, points: float = 1.0, due: str | None = None, waits_on: tuple[str, ...] = (),
         title: str | None = None, first_step: str | None = None) -> Work:
    return Work(
        key=key, ids=(key,), title=title or key, first_step=first_step, kind=kind, minutes=minutes, points=points,
        due_at=at(due) if due else None, picked=due is None, waits_on=waits_on,
    )


def day(*tasks: Work, pool: tuple[Work, ...] = (), busy: tuple[Busy, ...] = (), now: datetime = NINE,
        window: tuple[str, str] | None = WINDOW, changes: DayChanges = DayChanges(), rules: Rules = Rules()) -> Plan:
    return draw_day(DAY, window, Scope(work=tasks, deep_pool=tuple(replace(t, fill=True) for t in pool)), busy, now, rules, changes)


def meeting(start: str, end: str) -> Busy:
    return Busy(at(start), at(end))


def blocks(plan: Plan) -> list[tuple[str, str, str]]:
    return [(block.kind, block.start.strftime("%H:%M"), block.end.strftime("%H:%M")) for block in plan.blocks]


def slots(plan: Plan) -> list[tuple[str, str, str]]:
    return [(slot.key, slot.start.strftime("%H:%M"), slot.end.strftime("%H:%M")) for slot in plan.slots]


def reasons(plan: Plan) -> dict[str, str]:
    return {entry.key: entry.reason for entry in plan.unplaced}


# --- blocks --------------------------------------------------------------------------------------


def test_admin_comes_first_then_deep_focus():
    plan = day(work("bank", ADMIN, 30), work("essay", DEEP_FOCUS, 120))
    assert blocks(plan) == [(ADMIN, "09:00", "09:30"), (DEEP_FOCUS, "09:30", "11:30")]
    assert slots(plan) == [("bank", "09:00", "09:30"), ("essay", "09:30", "11:30")]


def test_deep_focus_gets_its_two_hour_minimum_filled_from_other_deep_work_by_points():
    plan = day(
        work("picked", DEEP_FOCUS, 45),
        pool=(work("big", DEEP_FOCUS, 75, points=9.0), work("small", DEEP_FOCUS, 60, points=2.0)),
    )
    assert blocks(plan) == [(DEEP_FOCUS, "09:00", "11:00")]
    assert slots(plan) == [("picked", "09:00", "09:45"), ("big", "09:45", "11:00")]
    assert plan.fill_keys == ("big",)
    assert "small" not in reasons(plan)


def test_no_deep_work_open_means_no_deep_block():
    plan = day(work("bank", ADMIN, 30))
    assert blocks(plan) == [(ADMIN, "09:00", "09:30")]


def test_deep_focus_stops_at_six_hours_and_says_the_rest_doesnt_fit():
    plan = day(*(work(f"deep{n}", DEEP_FOCUS, 120, points=10 - n) for n in range(4)))
    deep = [block for block in plan.blocks if block.kind == DEEP_FOCUS]
    assert sum((block.end - block.start) / timedelta(minutes=1) for block in deep) == 360
    assert reasons(plan) == {"deep3": DOESNT_FIT}


def test_no_deep_block_shorter_than_90_minutes():
    busy = tuple(meeting(f"{h:02d}:00", f"{h:02d}:05") for h in range(9, 18))  # an hour apart, so 45-minute gaps
    plan = day(work("essay", DEEP_FOCUS, 120), busy=busy)
    assert not [block for block in plan.blocks if block.kind == DEEP_FOCUS]
    assert reasons(plan) == {"essay": DOESNT_FIT}


def test_the_window_is_never_stretched():
    plan = day(*(work(f"a{n}", ADMIN, 120, points=10 - n) for n in range(6)))
    assert max(block.end for block in plan.blocks) <= at("18:00")
    assert sorted(reasons(plan)) == ["a4", "a5"]


def test_admin_opener_gives_way_so_deep_focus_keeps_its_room():
    # Free 9-12 and 1-2:30. A full admin opener would take the only stretch long enough for deep focus.
    busy = (meeting("12:00", "13:00"), meeting("14:30", "18:00"))
    plan = day(work("forms", ADMIN, 120, points=5.0), work("essay", DEEP_FOCUS, 150), busy=busy)
    essay = [slot for slot in plan.slots if slot.key == "essay"]
    assert sum(slot.minutes for slot in essay) == 150
    assert reasons(plan) == {"forms": DOESNT_FIT}


def test_errands_are_one_trip_as_late_as_it_fits_with_no_travel_added():
    plan = day(work("post", ERRANDS, 20), work("pharmacy", ERRANDS, 15), work("bank", ADMIN, 30))
    assert (ERRANDS, "17:25", "18:00") in blocks(plan)
    errand_slots = [slot for slot in plan.slots if slot.kind == ERRANDS]
    assert sum(slot.minutes for slot in errand_slots) == 35


def test_an_errand_due_at_three_pulls_the_trip_earlier():
    plan = day(work("post", ERRANDS, 20, due="15:00"), work("pharmacy", ERRANDS, 15))
    [trip] = [block for block in plan.blocks if block.kind == ERRANDS]
    assert trip.end == at("15:00") and not any(slot.late for slot in plan.slots)


def test_admin_fills_around_meetings_after_deep_focus():
    busy = (meeting("12:00", "13:00"),)
    tasks = [work(f"a{n}", ADMIN, 60, points=10 - n) for n in range(4)] + [work("essay", DEEP_FOCUS, 180)]
    plan = day(*tasks, busy=busy)
    admin_slots = [slot for slot in plan.slots if slot.kind == ADMIN]
    assert len(admin_slots) == 4
    assert all(not (slot.start < at("13:10") and at("12:00") < slot.end) for slot in plan.slots)


def test_a_day_off_draws_nothing():
    plan = day(work("bank", ADMIN, 30), window=None)
    assert plan.blocks == () and reasons(plan) == {"bank": DAY_OFF}
    assert window_for(date(2026, 10, 3), DEFAULT_WORKING_WINDOW) is None
    assert window_for(DAY, DEFAULT_WORKING_WINDOW) == ("09:00", "18:00")


# --- Nic's one-line changes --------------------------------------------------------------------


def test_no_deep_focus_today():
    plan = day(work("essay", DEEP_FOCUS, 120), work("bank", ADMIN, 30), changes=DayChanges(deep_focus_minutes=0))
    assert [block.kind for block in plan.blocks] == [ADMIN]
    assert reasons(plan) == {"essay": DOESNT_FIT}


def test_make_it_three_hours():
    plan = day(work("essay", DEEP_FOCUS, 240), changes=DayChanges(deep_focus_minutes=180))
    assert blocks(plan) == []  # a 4-hour task can't split into 3 hours without a part left over
    plan = day(work("essay", DEEP_FOCUS, 120), pool=(work("more", DEEP_FOCUS, 60),), changes=DayChanges(deep_focus_minutes=180))
    assert blocks(plan) == [(DEEP_FOCUS, "09:00", "12:00")]


def test_done_at_four():
    plan = day(*(work(f"a{n}", ADMIN, 60, points=10 - n) for n in range(9)), changes=DayChanges(ends_at=time(16, 0)))
    assert max(block.end for block in plan.blocks) == at("16:00")
    assert len(reasons(plan)) == 2


# --- slots ---------------------------------------------------------------------------------------


def test_deadlines_go_first_even_over_more_points():
    busy = (meeting("09:30", "18:00"),)
    plan = day(work("keen", ADMIN, 30, points=20.0), work("due", ADMIN, 30, points=1.0, due="17:00"), busy=busy)
    assert slots(plan) == [("due", "09:00", "09:30")]
    assert reasons(plan) == {"keen": DOESNT_FIT}


def test_a_task_goes_after_the_one_it_waits_on():
    plan = day(work("draft", ADMIN, 30, points=1.0), work("send", ADMIN, 15, points=9.0, waits_on=("draft",)))
    start = {slot.key: slot.start for slot in plan.slots}
    assert start["send"] >= start["draft"] + timedelta(minutes=30)


def test_a_task_waiting_on_something_not_placed_today_is_reported():
    plan = day(work("send", ADMIN, 15, waits_on=("elsewhere",)))
    assert reasons(plan) == {"send": WAITS_ON}


def test_deep_work_splits_into_parts_of_at_least_an_hour_and_admin_never_splits():
    busy = (meeting("11:00", "12:20"),)  # free 9-11 and 12:30 onwards (with the buffer)
    plan = day(work("essay", DEEP_FOCUS, 180), busy=busy, rules=Rules(deep_focus_min_minutes=180))
    parts = [slot for slot in plan.slots if slot.key == "essay"]
    assert [(slot.part, slot.parts) for slot in parts] == [(1, 2), (2, 2)]
    assert all(slot.minutes >= 60 for slot in parts)
    assert parts[0].title.endswith("(part 1 of 2)")
    admin = day(work("forms", ADMIN, 150), busy=(meeting("11:00", "12:00"), meeting("14:00", "18:00")))
    assert reasons(admin) == {"forms": DOESNT_FIT}


def test_the_buffer_after_a_meeting():
    plan = day(work("bank", ADMIN, 30), busy=(meeting("08:00", "09:00"),))
    assert slots(plan) == [("bank", "09:10", "09:40")]
    assert free_time(at("09:00"), at("10:00"), [meeting("09:00", "09:20")], NINE, Rules()) == [(at("09:30"), at("10:00"))]


def test_the_past_is_never_planned():
    plan = day(work("bank", ADMIN, 30), now=at("10:02"))
    assert slots(plan) == [("bank", "10:05", "10:35")]


def test_a_task_that_only_fits_after_its_deadline_is_placed_late():
    plan = day(work("form", ADMIN, 60, due="09:30"))
    [slot] = plan.slots
    assert slot.late and slot.start == at("09:00")
    assert any(line.startswith("Late:") for line in plan_summary(plan))


def test_same_inputs_same_plan():
    tasks = (work("a", ADMIN, 30, points=3.0), work("b", ADMIN, 30, points=3.0), work("essay", DEEP_FOCUS, 150),
             work("post", ERRANDS, 20))
    busy = (meeting("13:00", "14:00"),)
    assert day(*tasks, busy=busy) == day(*reversed(tasks), busy=busy)


def test_a_slot_never_reads_as_a_block():
    plan = day(work("x", ADMIN, 30, title="Admin for the lease"), work("y", DEEP_FOCUS, 120, first_step="Deep focus on it"))
    assert all(slot.title.startswith(SLOT_PREFIX) and block_kind(slot.title) is None for slot in plan.slots)
    assert all(block_kind(block.title) == block.kind for block in plan.blocks)


# --- scope from Brain Home records ------------------------------------------------------------


def record(suffix: str, *, block: str | None = "admin", minutes: int | None = 30, due: str | None = None, at_: str | None = None,
           state: str = "open", level: str = "accepted", links: tuple[dict[str, str], ...] = (), size: str = "task") -> ItemRecord:
    timing: dict[str, object] = {}
    if minutes is not None:
        timing.update(duration_minutes=minutes, padded_minutes=minutes, estimated_by="nic")
    if due is not None:
        timing["due"] = due
    if at_ is not None:
        timing["at"] = at_
    data: dict[str, object] = {"level": level, "size": size, "timing": timing}
    if block is not None:
        data["block"] = block
    created = datetime(2026, 9, 30, 12, 0, tzinfo=SINGAPORE)
    return ItemRecord(Record(
        schema=RECORD_SCHEMA_V2, id=f"brn_20260930_120000_{suffix}", kind="item", title=suffix, state=state,
        created_at=created, updated_at=created, assertion=None, confidence=None, sensitivity="private", vaults=(),
        owners=(), entities=(), evidence=EVIDENCE, supersedes=(), dedupe_key=None, data=data, body="",
        source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"}, links=links,
    ), f"items/{suffix}.md")


def test_scope_is_what_nic_settled_today():
    items = [
        record("pick1"),
        record("duetoday", due="2026-10-01"),
        record("duefriday", due="2026-10-02"),
        record("nodate"),
        record("timed", due="2026-10-01", at_="15:00"),
        record("waiting", state="waiting"),
        record("idea", level="possible"),
        record("goal", size="goal"),
        record("nolength", minutes=None, due="2026-10-01"),
        record("deep", block="deep_focus"),
    ]
    picked = frozenset({"brn_20260930_120000_pick1"})
    result = scope(items, DAY, NINE, picked)
    assert sorted(task.title for task in result.work) == ["duetoday", "pick1"]
    assert [task.title for task in result.deep_pool] == ["deep"]
    assert [(entry.title, entry.reason) for entry in result.unplaced] == [("nolength", NO_LENGTH)]
    due_task = next(task for task in result.work if task.title == "duetoday")
    assert due_task.due_at == datetime(2026, 10, 1, 23, 59, tzinfo=SINGAPORE)
    assert next(task for task in result.work if task.title == "pick1").picked


def test_scope_uses_padded_minutes_and_counts_no_block_as_admin():
    item = record("guess", block=None, due="2026-10-01")
    item.record.data["timing"].update(duration_minutes=20, padded_minutes=30, estimated_by="chatgpt-web")
    [task] = scope([item], DAY, NINE, frozenset()).work
    assert (task.kind, task.minutes) == (ADMIN, 30)


def test_scope_only_waits_on_open_blockers():
    blocker = record("blocker")
    done = record("done", state="done")
    waiting = record("waiter", due="2026-10-01", links=(
        {"type": "blocked_by", "target": blocker.record.id}, {"type": "blocked_by", "target": done.record.id}))
    result = scope([blocker, done, waiting], DAY, NINE, frozenset())
    assert result.work == ()
    assert [(entry.key, entry.reason) for entry in result.unplaced] == [(waiting.record.id, WAITS_ON)]
    ended = replace(blocker, record=replace(blocker.record, state="done"))
    [task] = scope([ended, done, waiting], DAY, NINE, frozenset()).work
    assert task.key == waiting.record.id
    assert task.waits_on == ()


# --- reflow --------------------------------------------------------------------------------------


def test_a_new_meeting_moves_only_the_slot_it_lands_on_and_the_blocks_stay():
    tasks = (work("a", ADMIN, 30, points=3.0), work("b", ADMIN, 30, points=2.0), work("essay", DEEP_FOCUS, 120))
    first = day(*tasks)
    moved = replan(first, Scope(work=tasks), (meeting("09:30", "09:50"),), NINE)
    assert moved.blocks == first.blocks
    before, after = dict((s[0], s[1:]) for s in slots(first)), dict((s[0], s[1:]) for s in slots(moved))
    assert after["a"] == before["a"] and after["essay"] == before["essay"]
    assert "b" not in after and reasons(moved) == {"b": DOESNT_FIT}  # its block is full, and blocks don't grow


def test_replan_with_nothing_changed_changes_nothing():
    tasks = (work("a", ADMIN, 30), work("essay", DEEP_FOCUS, 120), work("post", ERRANDS, 20))
    first = day(*tasks)
    assert replan(first, Scope(work=tasks), (), NINE) == first


def test_a_pick_added_after_the_day_is_settled_goes_into_free_room_in_the_blocks():
    tasks = (work("a", ADMIN, 30), work("essay", DEEP_FOCUS, 120))
    first = day(*tasks, busy=(meeting("12:00", "18:00"),))
    later = replan(first, Scope(work=(*tasks, work("new", ADMIN, 15))), (meeting("12:00", "18:00"),), NINE)
    assert "new" in reasons(later)  # the admin block is full and blocks don't grow on their own
    assert later.blocks == first.blocks


def test_a_passed_slot_is_asked_about_not_placed_again_until_nic_says_move():
    tasks = (work("a", ADMIN, 30, points=3.0), work("b", ADMIN, 30, points=2.0), work("c", ADMIN, 30, points=1.0))
    first = day(*tasks)
    assert slots(first) == [("a", "09:00", "09:30"), ("b", "09:30", "10:00"), ("c", "10:00", "10:30")]
    held = replan(first, Scope(work=tasks), (), at("09:35"))
    assert held == first and "a" not in reasons(held)
    # Nic: "move it", and c is done, which frees 10:00-10:30 in the admin block.
    moved = replan(held, Scope(work=tasks[:2]), (), at("09:40"), move_again={"a": at("09:40")})
    assert slots(moved) == [("a", "09:00", "09:30"), ("b", "09:30", "10:00"), ("a", "10:00", "10:30")]
    # Moved without room, it doesn't fit, and says so.
    stuck = replan(held, Scope(work=tasks), (), at("09:40"), move_again={"a": at("09:40")})
    assert reasons(stuck) == {"a": DOESNT_FIT}


def test_a_done_task_frees_its_future_slot():
    tasks = (work("a", ADMIN, 30), work("b", ADMIN, 30))
    first = day(*tasks)
    later = replan(first, Scope(work=(tasks[1],)), (), NINE)
    assert [slot.key for slot in later.slots] == ["b"]


def test_a_longer_length_re_places_the_task():
    tasks = (work("a", ADMIN, 30), work("b", ADMIN, 30))
    first = day(*tasks)
    longer = replan(first, Scope(work=(replace(tasks[0], minutes=45), tasks[1])), (), NINE)
    assert "a" not in {slot.key for slot in longer.slots} and reasons(longer) == {"a": DOESNT_FIT}
    shorter = replan(first, Scope(work=(replace(tasks[0], minutes=20), tasks[1])), (), NINE)
    assert ("a", "09:00", "09:20") in slots(shorter)


# --- words ---------------------------------------------------------------------------------------


def test_plan_summary_reads_like_the_spec():
    plan = day(work("bank", ADMIN, 60), work("essay", DEEP_FOCUS, 120), work("post", ERRANDS, 30),
               work("huge", ADMIN, 600), work("send", ADMIN, 15, waits_on=("elsewhere",)))
    lines = plan_summary(plan)
    assert lines[0] == "Today: admin 9 am–10 am (1 task) · deep focus 10 am–12 pm (1 task) · errands 5:30 pm–6 pm (1 task)"
    assert any(line.startswith("Doesn't fit today (10 h): \"huge\"") for line in lines)
    assert any(line.startswith("Waiting on another task first") for line in lines)


# --- blocks grow into free time (Nic, 29 Sep 2026: "ya auto grow into free time its fine") -------

WINDOW_TIMES = (at("09:00"), at("18:00"))


def test_a_later_pick_grows_the_block_it_touches():
    first = day(work("a", ADMIN, 30))
    later = replan(first, Scope(work=(work("a", ADMIN, 30), work("new", ADMIN, 15))), (), NINE, window=WINDOW_TIMES)
    assert blocks(later) == [(ADMIN, "09:00", "09:45")]
    assert ("new", "09:30", "09:45") in slots(later) and reasons(later) == {}


def test_a_later_pick_with_no_touching_room_gets_a_block_of_its_own_in_free_time():
    tasks = (work("a", ADMIN, 30), work("essay", DEEP_FOCUS, 120))
    first = day(*tasks)
    later = replan(first, Scope(work=(*tasks, work("new", ADMIN, 15))), (), NINE, window=WINDOW_TIMES)
    assert blocks(later) == [(ADMIN, "09:00", "09:30"), (DEEP_FOCUS, "09:30", "11:30"), (ADMIN, "11:30", "11:45")]
    assert [slot for slot in later.slots if slot.key != "new"] == list(first.slots)


def test_a_moved_task_and_a_longer_length_grow_too():
    tasks = (work("a", ADMIN, 30, points=3.0), work("b", ADMIN, 30, points=2.0), work("c", ADMIN, 30, points=1.0))
    first = day(*tasks)
    moved = replan(first, Scope(work=tasks), (), at("09:40"), move_again={"a": at("09:40")}, window=WINDOW_TIMES)
    assert ("a", "10:30", "11:00") in slots(moved) and reasons(moved) == {}
    longer = replan(first, Scope(work=(replace(tasks[0], minutes=45), *tasks[1:])), (), NINE, window=WINDOW_TIMES)
    a_slots = [slot for slot in longer.slots if slot.key == "a"]
    assert [slot.minutes for slot in a_slots] == [45] and reasons(longer) == {}


def test_growth_stays_inside_the_window_and_clear_of_meetings():
    first = day(work("a", ADMIN, 30), busy=(meeting("09:30", "18:00"),))
    later = replan(first, Scope(work=(work("a", ADMIN, 30), work("new", ADMIN, 15))), (meeting("09:30", "18:00"),), NINE,
                   window=WINDOW_TIMES)
    assert reasons(later) == {"new": DOESNT_FIT} and blocks(later) == blocks(first)


def test_deep_focus_never_grows_past_six_hours():
    tasks = tuple(work(f"deep{n}", DEEP_FOCUS, 120, points=10 - n) for n in range(3))
    first = day(*tasks)
    later = replan(first, Scope(work=(*tasks, work("more", DEEP_FOCUS, 60))), (), NINE, window=WINDOW_TIMES)
    assert reasons(later) == {"more": DOESNT_FIT}


def test_without_the_window_nothing_grows():
    first = day(work("a", ADMIN, 30))
    later = replan(first, Scope(work=(work("a", ADMIN, 30), work("new", ADMIN, 15))), (), NINE)
    assert reasons(later) == {"new": DOESNT_FIT} and blocks(later) == blocks(first)


# --- review fixes (orchestrator review of slices 1-3, 29 Sep 2026) ------------------------------


def test_an_errand_waiting_on_admin_is_placed_after_it_never_dropped():
    plan = day(work("print label", ADMIN, 15), work("post parcel", ERRANDS, 20, waits_on=("print label",)))
    start = {slot.key: slot for slot in plan.slots}
    assert "post parcel" in start and start["post parcel"].start >= start["print label"].end
    assert reasons(plan) == {}


def test_an_errand_waiting_on_something_never_placed_is_reported():
    plan = day(work("post parcel", ERRANDS, 20, waits_on=("elsewhere",)))
    assert reasons(plan) == {"post parcel": WAITS_ON}


def test_a_higher_points_task_waits_for_a_lower_points_blocker_that_missed_the_opener():
    # The opener has 20 minutes (a meeting at 9:20), too short for either; A is packed first by points.
    plan = day(work("A", ADMIN, 30, points=9.0, waits_on=("B",)), work("B", ADMIN, 30, points=1.0),
               busy=(meeting("09:20", "10:00"),))
    start = {slot.key: slot for slot in plan.slots}
    assert set(start) == {"A", "B"} and start["A"].start >= start["B"].end
    assert reasons(plan) == {}


def test_deep_work_waiting_on_admin_goes_after_it_across_kinds():
    plan = day(work("outline", ADMIN, 30), work("essay", DEEP_FOCUS, 120, waits_on=("outline",)),
               busy=(meeting("09:20", "09:40"),))
    start = {slot.key: slot for slot in plan.slots}
    assert start["essay"].start >= start["outline"].end
    assert blocks(plan) == [(ADMIN, "09:50", "10:20"), (DEEP_FOCUS, "10:20", "12:20")]


def test_replan_places_a_waiting_task_once_its_blocker_has_room():
    tasks = (work("A", ADMIN, 30, points=9.0, waits_on=("B",)), work("B", ADMIN, 30, points=1.0))
    first = day(*tasks)
    assert replan(first, Scope(work=tasks), (), NINE) == first


def test_a_task_linked_to_a_newer_version_of_its_blocker_still_waits_on_it():
    first_version = record("blocker")
    newer = replace(first_version.record, id="brn_20260930_130000_blocker2", supersedes=(first_version.record.id,))
    blocker = ItemRecord(newer, "items/blocker.md")
    waiter = record("waiter", due="2026-10-01", links=({"type": "blocked_by", "target": newer.id},))
    result = scope([blocker, waiter], DAY, NINE, frozenset({newer.id}))
    assert [task.key for task in result.work] == [first_version.record.id]
    assert [(entry.key, entry.reason) for entry in result.unplaced] == [(waiter.record.id, WAITS_ON)]
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    start = {slot.key: slot for slot in plan.slots}
    assert waiter.record.id not in start and first_version.record.id in start
    ended = replace(blocker, record=replace(newer, state="done"))
    cleared = scope([first_version, ended, waiter], DAY, NINE, frozenset({newer.id}))
    assert [task.key for task in cleared.work] == [waiter.record.id]
    assert cleared.work[0].waits_on == () and cleared.unplaced == ()
    assert [slot.key for slot in draw_day(DAY, WINDOW, cleared, (), NINE).slots] == [waiter.record.id]


def test_nothing_is_planned_to_start_within_the_next_minute():
    plan = day(work("bank", ADMIN, 30), now=at("09:05"))
    assert slots(plan) == [("bank", "09:10", "09:40")]



# --- slice 4: the runner keeps the plan, and redraws keep what's past -----------------------------

from brain_core.placement import plan_from_mapping, plan_to_mapping, window_times  # noqa: E402


def test_a_plan_round_trips_through_plain_data():
    plan = day(work("bank", ADMIN, 30), work("essay", DEEP_FOCUS, 150, due="17:00"), work("post", ERRANDS, 20),
               work("huge", ADMIN, 600), busy=(meeting("11:00", "12:20"),))
    import json

    assert plan_from_mapping(json.loads(json.dumps(plan_to_mapping(plan)))) == plan


def test_window_times_end_earlier_when_nic_says_so():
    assert window_times(DAY, WINDOW) == (at("09:00"), at("18:00"))
    assert window_times(DAY, WINDOW, DayChanges(ends_at=time(16, 0))) == (at("09:00"), at("16:00"))


def test_a_redraw_joins_the_kept_block_with_new_ones_of_its_kind():
    tasks = tuple(work(f"a{n}", ADMIN, 60, points=10 - n) for n in range(4))
    first = day(*tasks)
    now = at("10:05")
    kept = tuple(slot for slot in first.slots if slot.start < now)
    kept_blocks = (DayBlock(ADMIN, at("09:00"), at("11:00")),)
    redrawn = draw_day(DAY, WINDOW, Scope(work=tasks), (), now, Rules(), DayChanges(ends_at=time(16, 0)), kept=kept, kept_blocks=kept_blocks)
    assert blocks(redrawn) == [(ADMIN, "09:00", "13:00")]



def test_a_plan_can_be_kept_without_task_text_and_given_it_back():
    from brain_core.placement import with_titles

    plan = day(work("bank", ADMIN, 30, first_step="Call the bank"), work("huge", ADMIN, 600))
    bare = plan_from_mapping(plan_to_mapping(plan, with_text=False))
    assert all(slot.task_title == "" and slot.first_step is None for slot in bare.slots)
    assert all(entry.title == "" for entry in bare.unplaced)
    assert with_titles(bare, {"bank": ("bank", "Call the bank"), "huge": ("huge", None)}) == plan


# --- slice 7: a slot Nic moved by hand keeps his time ---------------------------------------------


def test_a_pinned_slot_keeps_his_time_and_what_it_lands_on_moves():
    tasks = (work("a", ADMIN, 30, points=3.0), work("b", ADMIN, 30, points=2.0))
    first = day(*tasks)
    assert slots(first) == [("a", "09:00", "09:30"), ("b", "09:30", "10:00")]
    moved = replan(first, Scope(work=tasks), (), NINE, window=(at("09:00"), at("18:00")),
                   pinned={("a", 1): (at("09:30"), at("10:00"))})
    after = dict((key, (start, end)) for key, start, end in slots(moved))
    assert after["a"] == ("09:30", "10:00")
    assert after["b"] != ("09:30", "10:00")


def test_a_pinned_slot_outside_any_block_or_resized_is_still_kept():
    tasks = (work("a", ADMIN, 30),)
    first = day(*tasks)
    moved = replan(first, Scope(work=tasks), (), NINE, pinned={("a", 1): (at("15:00"), at("15:10"))})
    assert slots(moved) == [("a", "15:00", "15:10")] and reasons(moved) == {}


def test_a_pin_that_has_ended_changes_nothing():
    tasks = (work("a", ADMIN, 30),)
    first = day(*tasks)
    assert replan(first, Scope(work=tasks), (), NINE, pinned={("a", 1): (at("07:00"), at("07:30"))}) == first


# --- W9: current work, ordered parts and parent titles ----------------------------------------


def work_item(suffix: str, **data) -> ItemRecord:
    entry = record(suffix)
    return replace(entry, record=replace(entry.record, data={**entry.record.data, **data}))


def work_date(day: str, **fields) -> dict:
    return {"day": day, "words": day, "read_on": "2026-10-01", "read_by": "code", **fields}


def send_with_parts():
    old = record("send", minutes=15)
    send = replace(old, record=replace(old.record, id="brn_20261001_070000_send2", supersedes=(old.record.id,)))
    edit = work_item("edit", parent=old.record.id, order=1)
    return old, send, edit


def test_picking_send_places_edit_then_send_never_the_old_version():
    old, send, edit = send_with_parts()
    result = scope([old, send, edit], DAY, NINE, frozenset({old.record.id}))
    assert {task.key for task in result.work} == {old.record.id, edit.record.id}
    assert next(task for task in result.work if task.key == old.record.id).ids == (send.record.id, old.record.id)
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    assert [slot.key for slot in plan.slots] == [edit.record.id, old.record.id]
    assert plan.slots[0].end <= plan.slots[1].start
    assert len([slot for slot in plan.slots if slot.key == old.record.id]) == 1
    assert plan.slots[0].title == "\u25b8 edit (send)"


def test_picked_parts_follow_stored_order_even_when_points_disagree():
    send = work_item("send", next_action={"text": "Send the brief", "done": False})
    edit = work_item("edit", parent=send.record.id, order=1)
    check = work_item("check", parent=send.record.id, order=2, owed_to="Morgan")
    plan = draw_day(DAY, WINDOW, scope([check, send, edit], DAY, NINE, frozenset({send.record.id})), (), NINE)
    assert [slot.key for slot in plan.slots] == [edit.record.id, check.record.id, send.record.id]
    assert all(a.end <= b.start for a, b in zip(plan.slots, plan.slots[1:]))


def test_part_waiting_on_someone_is_left_out_and_reported():
    _, send, edit = send_with_parts()
    edit = replace(edit, record=replace(edit.record, data={**edit.record.data, "needs": {"from": "Morgan", "what": "edits"}}))
    result = scope([send, edit], DAY, NINE, frozenset({send.record.id}))
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    assert [slot.key for slot in plan.slots] == [send.record.supersedes[0]]
    assert reasons(plan) == {edit.record.id: WAITS_ON}
    assert any("edit" in line for line in plan_summary(plan))


def test_later_day_part_is_left_out_and_reported_never_filled_from_pool():
    from brain_core.placement import LATER_DAY

    _, send, edit = send_with_parts()
    edit = replace(edit, record=replace(edit.record, data={**edit.record.data, "block": "deep_focus", "dates": {"planned": work_date("2026-10-02")}}))
    result = scope([send, edit], DAY, NINE, frozenset({send.record.id}))
    assert result.deep_pool == ()
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    assert [slot.key for slot in plan.slots] == [send.record.supersedes[0]]
    assert reasons(plan)[edit.record.id] == LATER_DAY
    assert any("later day" in line and "edit" in line for line in plan_summary(plan))


def test_today_deadline_and_planned_day_or_period_are_included_appointments_excluded():
    deadline = work_item("deadline", dates={"deadline": work_date("2026-10-01")})
    planned = work_item("planned", dates={"planned": work_date("2026-10-01")})
    period = work_item("period", dates={"planned": {"start": "2026-09-30", "end": "2026-10-02", "words": "this week", "read_on": "2026-10-01", "read_by": "code"}})
    appointment = work_item("appointment", dates={"appointment": work_date("2026-10-01", at="15:00")})
    future = work_item("future", dates={"deadline": work_date("2026-10-02")})
    result = scope([deadline, planned, period, appointment, future], DAY, NINE, frozenset({appointment.record.id}))
    assert {task.title for task in result.work} == {"deadline", "planned", "period"}
    assert next(task for task in result.work if task.title == "deadline").due_at == at("23:59")


def test_parent_without_own_length_places_only_its_parts():
    send = record("send", minutes=None)
    edit = work_item("edit", parent=send.record.id, order=1)
    plan = draw_day(DAY, WINDOW, scope([send, edit], DAY, NINE, frozenset({send.record.id})), (), NINE)
    assert [slot.key for slot in plan.slots] == [edit.record.id]
    assert not plan.unplaced


def test_old_part_of_maps_to_current_parent_title_and_first_id():
    old = record("parent")
    parent = replace(old, record=replace(old.record, id="brn_20261001_070000_parent2", title="Current parent", supersedes=(old.record.id,)))
    part = record("part", links=({"type": "part_of", "target": old.record.id},))
    plan = draw_day(DAY, WINDOW, scope([old, parent, part], DAY, NINE, frozenset({part.record.id})), (), NINE)
    assert [slot.key for slot in plan.slots] == [part.record.id]
    assert plan.slots[0].title == "\u25b8 part (Current parent)"


def test_parent_title_round_trips_and_bare_plan_keeps_no_parent_text():
    from brain_core.placement import with_titles

    task = replace(work("part", ADMIN, 30, first_step="Edit the brief"), belongs_to="Send Morgan my brief")
    plan = day(task)
    assert plan_from_mapping(plan_to_mapping(plan)) == plan
    bare = plan_from_mapping(plan_to_mapping(plan, with_text=False))
    assert bare.slots[0].belongs_to is None
    assert with_titles(bare, {"part": (task.title, task.first_step, task.belongs_to)}) == plan


@pytest.mark.parametrize("omission", ["waiting", "later", "no_length", "doesnt_fit"])
def test_omitted_or_nonfitting_first_part_creates_no_wait_for_next_part_or_own_slot(omission):
    from brain_core.placement import LATER_DAY

    send = record("send", minutes=15)
    first = work_item("first", parent=send.record.id, order=1)
    second = work_item("second", parent=send.record.id, order=2)
    data = dict(first.record.data)
    if omission == "waiting":
        data["needs"] = {"from": "Morgan"}
        reason = WAITS_ON
    elif omission == "later":
        data["dates"] = {"planned": work_date("2026-10-02")}
        reason = LATER_DAY
    elif omission == "no_length":
        data["timing"] = {}
        reason = NO_LENGTH
    else:
        data["timing"] = {"duration_minutes": 600, "padded_minutes": 600, "estimated_by": "nic"}
        reason = DOESNT_FIT
    first = replace(first, record=replace(first.record, data=data))
    result = scope([send, second, first], DAY, NINE, frozenset({send.record.id}))
    assert all(not task.waits_on for task in result.work)
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    assert [slot.key for slot in plan.slots] == [second.record.id, send.record.id]
    assert plan.slots[0].end <= plan.slots[1].start
    assert reasons(plan) == {first.record.id: reason}


def test_explicit_after_still_excludes_part_even_when_its_order_alone_would_not():
    send = record("send", minutes=15)
    first = work_item("first", parent=send.record.id, order=1, needs={"from": "Morgan"})
    second = work_item("second", parent=send.record.id, order=2, after=[first.record.id])
    result = scope([send, second, first], DAY, NINE, frozenset({send.record.id}))
    assert [task.key for task in result.work] == [send.record.id]
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    assert [slot.key for slot in plan.slots] == [send.record.id]
    assert reasons(plan) == {first.record.id: WAITS_ON, second.record.id: WAITS_ON}


def test_picked_parts_keep_order_across_block_kinds_before_own_slot():
    send = record("send", minutes=15)
    first = work_item("first", parent=send.record.id, order=1, block="deep_focus")
    second = work_item("second", parent=send.record.id, order=2)
    plan = draw_day(DAY, WINDOW, scope([send, second, first], DAY, NINE, frozenset({send.record.id})), (), NINE)
    assert [slot.key for slot in plan.slots] == [first.record.id, second.record.id, send.record.id]
    assert all(a.end <= b.start for a, b in zip(plan.slots, plan.slots[1:]))


def test_picked_parent_exception_still_respects_its_own_explicit_wait():
    send = work_item("send", needs={"from": "Morgan"})
    edit = work_item("edit", parent=send.record.id, order=1)
    result = scope([send, edit], DAY, NINE, frozenset({send.record.id}))
    assert [task.key for task in result.work] == [edit.record.id]
    assert [(entry.key, entry.reason) for entry in result.unplaced] == [(send.record.id, WAITS_ON)]


@pytest.mark.parametrize("case", ["explicit_999", "created_at", "first_id"])
def test_part_packing_matches_reader_order_default_and_ties(case):
    from brain_core import work as reader

    parent = record("parent", minutes=15)
    z = work_item("z", parent=parent.record.id)
    b = work_item("b", parent=parent.record.id)
    if case == "explicit_999":
        z = replace(z, record=replace(z.record, data={**z.record.data, "order": 999}))
        expected = [z.record.id, b.record.id]
    elif case == "created_at":
        z = replace(z, record=replace(z.record, created_at=z.record.created_at - timedelta(minutes=1)))
        expected = [z.record.id, b.record.id]
    else:
        expected = [b.record.id, z.record.id]
    index = reader.index_things(entry.record for entry in (parent, b, z))
    assert reader.next_of(parent.record.id, index, NINE)["item_ref"] == expected[0]
    result = scope([parent, b, z], DAY, NINE, frozenset({parent.record.id}))
    assert all(task.waits_on == () for task in result.work)
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    assert [slot.key for slot in plan.slots] == [*expected, parent.record.id]


@pytest.mark.parametrize("change", ["meeting", "new_first", "longer", "unfitting", "waiting"])
def test_replan_releases_future_successors_of_changed_or_new_predecessor(change):
    parent = record("parent", minutes=15)
    e1 = work_item("e1", parent=parent.record.id, order=1)
    e2 = work_item("e2", parent=parent.record.id, order=2)
    picked = frozenset({parent.record.id})
    original = scope([parent, e1, e2], DAY, NINE, picked)
    first = draw_day(DAY, WINDOW, original, (), NINE)
    assert slots(first) == [(e1.record.id, "09:00", "09:30"), (e2.record.id, "09:30", "10:00"),
                            (parent.record.id, "10:00", "10:15")]
    busy = ()
    extra = []
    expected = [e1.record.id, e2.record.id, parent.record.id]
    if change == "meeting":
        busy = (meeting("09:00", "09:20"),)
    elif change == "new_first":
        extra = [work_item("e0", parent=parent.record.id, order=1)]
        e1 = replace(e1, record=replace(e1.record, data={**e1.record.data, "order": 2}))
        e2 = replace(e2, record=replace(e2.record, data={**e2.record.data, "order": 3}))
        expected.insert(0, extra[0].record.id)
    elif change in ("longer", "unfitting"):
        minutes = 60 if change == "longer" else 600
        e1 = replace(e1, record=replace(e1.record, data={**e1.record.data,
                     "timing": {"duration_minutes": minutes, "padded_minutes": minutes, "estimated_by": "nic"}}))
        if change == "unfitting":
            expected.remove(e1.record.id)
    else:
        e1 = replace(e1, record=replace(e1.record, data={**e1.record.data, "needs": {"from": "Morgan"}}))
        expected.remove(e1.record.id)
    result = scope([parent, e1, e2, *extra], DAY, NINE, picked)
    assert all(task.waits_on == () for task in result.work)
    changed = replan(first, result, busy, NINE, window=WINDOW_TIMES)
    assert [slot.key for slot in changed.slots] == expected
    assert all(a.end <= b.start for a, b in zip(changed.slots, changed.slots[1:]))
    if change == "unfitting":
        assert reasons(changed) == {e1.record.id: DOESNT_FIT}
    elif change == "waiting":
        assert reasons(changed) == {e1.record.id: WAITS_ON}
    else:
        assert not changed.unplaced
    assert replan(changed, result, busy, NINE, window=WINDOW_TIMES) == changed


def test_replan_nonfitting_predecessor_keeps_later_slot_when_unrelated_work_finishes():
    other = work("other", ADMIN, 30, points=9.0)
    first = work("first", ADMIN, 600)
    later = replace(work("later", ADMIN, 30), _order_after=("first",))
    original = day(other, first, later)
    assert slots(original) == [("other", "09:00", "09:30"), ("later", "09:30", "10:00")]
    assert reasons(original) == {"first": DOESNT_FIT}

    changed = replan(original, Scope(work=(first, later)), (), NINE, window=WINDOW_TIMES)
    assert slots(changed) == [("later", "09:30", "10:00")]
    assert changed.slots == tuple(slot for slot in original.slots if slot.key == "later")
    assert reasons(changed) == {"first": DOESNT_FIT}
    assert replan(changed, Scope(work=(first, later)), (), NINE, window=WINDOW_TIMES) == changed


def test_replan_nonfitting_predecessor_keeps_later_slot_when_higher_priority_pick_arrives():
    other = work("other", ADMIN, 30, points=9.0)
    first = work("first", ADMIN, 600)
    later = replace(work("later", ADMIN, 30), _order_after=("first",))
    original = day(other, first, later)
    assert slots(original) == [("other", "09:00", "09:30"), ("later", "09:30", "10:00")]
    assert reasons(original) == {"first": DOESNT_FIT}
    new = work("new", ADMIN, 30, points=5.0)
    changed_scope = Scope(work=(other, first, later, new))

    changed = replan(original, changed_scope, (), NINE, window=WINDOW_TIMES)
    assert slots(changed) == [("other", "09:00", "09:30"), ("later", "09:30", "10:00"),
                              ("new", "10:00", "10:30")]
    assert all(slot in changed.slots for slot in original.slots)
    assert reasons(changed) == {"first": DOESNT_FIT}
    assert replan(changed, changed_scope, (), NINE, window=WINDOW_TIMES) == changed


@pytest.mark.parametrize("change", ["shorter", "more_room"])
def test_replan_previously_nonfitting_predecessor_can_fit_and_repack_its_successors(change):
    other = work("other", ADMIN, 30, points=9.0)
    first = work("first", ADMIN, 600 if change == "shorter" else 60)
    later = replace(work("later", ADMIN, 30), _order_after=("first",))
    own = replace(work("own", ADMIN, 15), _order_after=("first", "later"))
    original = day(other, first, later, own, window=("09:00", "10:15"))
    assert slots(original) == [("other", "09:00", "09:30"), ("later", "09:30", "10:00"),
                               ("own", "10:00", "10:15")]
    assert reasons(original) == {"first": DOESNT_FIT}
    if change == "shorter":
        first = replace(first, minutes=30)
    changed_scope = Scope(work=(other, first, later, own))

    changed = replan(original, changed_scope, (), NINE, window=WINDOW_TIMES)
    assert [slot.key for slot in changed.slots] == ["other", "first", "later", "own"]
    assert original.slots[0] in changed.slots
    assert all(a.end <= b.start for a, b in zip(changed.slots, changed.slots[1:]))
    assert not changed.unplaced
    assert replan(changed, changed_scope, (), NINE, window=WINDOW_TIMES) == changed


@pytest.mark.parametrize("protected", ["past", "pinned"])
def test_replan_part_changes_keep_past_or_pinned_slot_and_unrelated_future_slot(protected):
    parent = record("parent", minutes=15)
    e1 = work_item("e1", parent=parent.record.id, order=1)
    e2 = work_item("e2", parent=parent.record.id, order=2)
    other = record("other", minutes=15)
    picked = frozenset({parent.record.id, other.record.id})
    result = scope([parent, e1, e2, other], DAY, NINE, picked)
    first = draw_day(DAY, WINDOW, result, (), NINE)
    e1_slot = next(slot for slot in first.slots if slot.key == e1.record.id)
    other_slot = next(slot for slot in first.slots if slot.key == other.record.id)
    if protected == "past":
        now, pins = e1_slot.start + timedelta(minutes=5), {}
    else:
        now, pins = NINE, {(e1.record.id, 1): (e1_slot.start, e1_slot.end)}
    e1 = replace(e1, record=replace(e1.record, data={**e1.record.data,
                 "timing": {"duration_minutes": 60, "padded_minutes": 60, "estimated_by": "nic"}}))
    changed = replan(first, scope([parent, e1, e2, other], DAY, now, picked), (), now, window=WINDOW_TIMES, pinned=pins)
    assert e1_slot in changed.slots
    assert other_slot in changed.slots


@pytest.mark.parametrize("agent", [True, False])
def test_slot_title_uses_reader_wording_mark_and_keeps_actual_owner_action(agent):
    parent = record("parent", minutes=None)
    part = work_item("part", parent=parent.record.id, order=1)
    if agent:
        # A legacy step is mapped and labelled by the sole reader, not by placement.
        part = replace(part, record=replace(part.record, data={**part.record.data, "first_step": "Agent's invented step"},
                       source={**part.record.source, "said_by": "agent", "came_by": "agent_guess"}))
        expected = SLOT_PREFIX + "part (parent)"
    else:
        part = replace(part, record=replace(part.record, data={**part.record.data,
                       "next_action": {"text": "Nic's actual next action", "done": False}}))
        expected = SLOT_PREFIX + "Nic's actual next action (parent)"
    result = scope([parent, part], DAY, NINE, frozenset({parent.record.id}))
    plan = draw_day(DAY, WINDOW, result, (), NINE)
    assert [slot.title for slot in plan.slots] == [expected]
    assert result.work[0].first_step == (None if agent else "Nic's actual next action")
