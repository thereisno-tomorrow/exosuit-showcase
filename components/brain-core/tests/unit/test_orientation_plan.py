"""Step I §5a (build plan slice 6): what Nic sees of today's plan in both asks. Off (no plan
slots) both answer exactly as before; on, "where am I?" names the slot he's in, "what next?"
leads with "planned for now" or says the plan had something else, and a passed slot is asked
about like a passed start time."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from brain_core.orientation import PLANNED_FOR_NOW, SECTION_NOW, PlannedSlot, overview, what_next
from brain_core.review import CalendarEvent
from brain_core.timewords import SINGAPORE

from test_orientation import NOW, inputs, item

TODAY_AT = NOW.replace(hour=10, minute=0)  # Sun 4 Oct 2026, 10:00 SGT


def slot(record, start: str, end: str, *, first_step: str | None = None) -> PlannedSlot:
    def at(clock: str) -> datetime:
        hour, minute = (int(part) for part in clock.split(":"))
        return NOW.replace(hour=hour, minute=minute, second=0, microsecond=0)

    return PlannedSlot(key=record.id, title=record.title, start=at(start), end=at(end), first_step=first_step)


def _records():
    urgent = item("u001", title="File the tax form", due="2026-10-04", padded_minutes=30)
    plain = item("p001", title="Tidy the desk", padded_minutes=30)
    return urgent, plain


# --- off means off ---------------------------------------------------------------------------------


def test_no_plan_and_an_empty_plan_answer_exactly_as_before():
    urgent, plain = _records()
    base = inputs(urgent, plain)
    for now in (NOW, TODAY_AT):
        assert what_next(replace(base, plan_slots=()), now) == what_next(base, now)
        assert overview(replace(base, plan_slots=()), now) == overview(base, now)
    blocks_on = replace(base, blocks=True)
    assert overview(replace(blocks_on, plan_slots=()), TODAY_AT) == overview(blocks_on, TODAY_AT)


# --- what next ------------------------------------------------------------------------------------


def test_the_slot_now_leads_with_planned_for_now_when_it_is_the_pick():
    urgent, plain = _records()
    answer = what_next(replace(inputs(urgent, plain), plan_slots=(slot(urgent, "09:45", "10:15"),)), TODAY_AT)
    assert answer.top_pick.record_id == urgent.id
    assert answer.top_pick.rules[0] == PLANNED_FOR_NOW and answer.plan_note is None


def test_when_something_else_comes_first_the_answer_says_the_plan_had_the_slot():
    urgent, plain = _records()
    answer = what_next(replace(inputs(urgent, plain), plan_slots=(slot(plain, "09:45", "10:15"),)), TODAY_AT)
    assert answer.top_pick.record_id == urgent.id  # the ranking never changes
    assert PLANNED_FOR_NOW not in answer.top_pick.rules
    assert answer.plan_note.startswith('The plan says "Tidy the desk" until 10:15 am, but "File the tax form" comes first')


def test_a_passed_slot_is_asked_about_and_never_picked():
    urgent, plain = _records()
    answer = what_next(replace(inputs(urgent, plain), plan_slots=(slot(plain, "09:00", "09:30"),)), TODAY_AT)
    [asked] = answer.missed
    assert asked.record_id == plain.id
    assert asked.question == '"Tidy the desk" was planned for 9 am–9:30 am. Did it happen? Done, move or drop?'
    assert asked.fields_used["move"] == {"kind": "day_change", "detail": '{"move": "%s"}' % plain.id}
    picked = {answer.top_pick.record_id, *(entry.record_id for entry in answer.alternatives)}
    assert plain.id not in picked


def test_a_slot_still_to_come_or_a_done_task_is_not_asked_about():
    urgent, plain = _records()
    later = what_next(replace(inputs(urgent, plain), plan_slots=(slot(plain, "11:00", "11:30"),)), TODAY_AT)
    assert later.missed == ()
    done = replace(plain, state="done")
    finished = what_next(replace(inputs(urgent, done), plan_slots=(slot(done, "09:00", "09:30"),)), TODAY_AT)
    assert finished.missed == ()


def test_a_task_moved_to_a_later_slot_is_not_asked_about():
    urgent, plain = _records()
    slots = (slot(plain, "09:00", "09:30"), slot(plain, "11:00", "11:30"))
    assert what_next(replace(inputs(urgent, plain), plan_slots=slots), TODAY_AT).missed == ()


# --- where am I -----------------------------------------------------------------------------------


def _now_line(answer):
    return next(line for line in answer.lines if line.section == SECTION_NOW)


def test_the_now_line_names_the_slot_inside_its_block():
    urgent, plain = _records()
    block = CalendarEvent(title="Admin", start=TODAY_AT - timedelta(minutes=30), end=TODAY_AT + timedelta(hours=1))
    base = replace(inputs(urgent, plain, calendar_events=(block,)), blocks=True)
    answer = overview(replace(base, plan_slots=(slot(urgent, "09:45", "10:15", first_step="Open the form"),)), TODAY_AT)
    line = _now_line(answer)
    assert line.text == "In your admin block until 11 am · ▸ Open the form until 10:15 am"
    assert line.fields_used["slot"] == urgent.id


def test_the_now_line_names_the_slot_even_with_blocks_off():
    urgent, plain = _records()
    answer = overview(replace(inputs(urgent, plain), plan_slots=(slot(plain, "09:45", "10:15"),)), TODAY_AT)
    assert _now_line(answer).text == "Planned now: ▸ Tidy the desk until 10:15 am"



# --- review fixes (slice 6) ------------------------------------------------------------------------


def test_a_slot_whose_task_is_done_is_not_now():
    urgent, plain = _records()
    done = replace(plain, state="done")
    slots = (slot(done, "09:45", "10:15"),)
    answer = what_next(replace(inputs(urgent, done), plan_slots=slots), TODAY_AT)
    assert answer.plan_note is None and PLANNED_FOR_NOW not in answer.top_pick.rules
    assert all(line.section != SECTION_NOW for line in overview(replace(inputs(urgent, done), plan_slots=slots), TODAY_AT).lines)


def test_a_deep_focus_fill_slot_is_never_asked_about():
    urgent, plain = _records()
    filled = replace(slot(plain, "09:00", "09:30"), fill=True)
    answer = what_next(replace(inputs(urgent, plain), plan_slots=(filled,)), TODAY_AT)
    assert answer.missed == ()
    picked = {answer.top_pick.record_id, *(entry.record_id for entry in answer.alternatives)}
    assert plain.id in picked


def test_with_only_a_passed_slot_the_answer_says_planned():
    plain = item("p001", title="Tidy the desk", padded_minutes=30)
    answer = what_next(replace(inputs(plain), plan_slots=(slot(plain, "09:00", "09:30"),)), TODAY_AT)
    assert answer.not_enough.reason == "Nothing to start right now: everything open was planned or set for a time that has passed."
