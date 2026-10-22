from dataclasses import replace

from brain_core.review import build_morning
from test_morning import inputs, task, when, lines
import pytest


def test_tidy_is_a_pinned_suggestion_and_never_edits_records():
    from brain_core.review import list_tidy_candidates, with_tidy_suggestions
    record = task("raw note", data={"review_marks": [{"mark": "looks_like", "kind": "project"}]})
    source = inputs((record,))
    before = record.sha256
    candidates = list_tidy_candidates(source)
    assert candidates[0].item_ref == record.id
    morning = build_morning(source)
    proposal = {"suggested_title": "Call the lender", "suggested_label": "Home", "suggested_name": "Home loan"}
    shown = with_tidy_suggestions(morning, {record.id: (before, proposal)})
    line = next(line for line in lines(shown, "new") if line.item_ref == record.id)
    assert line.suggested_title == "Call the lender" and line.suggested_label == "Home"
    assert {"title", "label", "save"} <= set(line.offered)
    assert line.fields_used["tidy_suggestion"] is True
    assert record.sha256 == before
    assert with_tidy_suggestions(morning, {record.id: ("stale", proposal)}) == morning


def test_opening_has_only_today_and_tomorrows_deadlines_when_enabled():
    records = (
        task("Due now", key="brn_due", data={"dates": {"deadline": when("2026-10-01")}}),
        task("Due next", key="brn_next", data={"dates": {"deadline": when("2026-10-02")}}),
        task("Due later", key="brn_later", data={"dates": {"deadline": when("2026-10-07")}}),
        task("Meeting tomorrow", key="brn_meeting", data={"dates": {"appointment": when("2026-10-02", at="10:00")}}),
        task("For tomorrow", key="brn_plan", data={"dates": {"planned": when("2026-10-02")}}),
        task("Wait tomorrow", key="brn_wait", data={"dates": {"not_before": when("2026-10-02")}}),
    )
    source = inputs(records)
    enabled = lines(build_morning(source, opening_deadlines=True), "look")
    due = [line for line in enabled if line.rule in {"due_today", "due_tomorrow"}]
    assert [(line.rule, line.text) for line in due] == [
        ("due_today", "Due today: Due now"), ("due_tomorrow", "Due tomorrow: Due next")]
    assert not any(line.rule == "coming_up" for line in enabled)
    assert not any(line.item_ref == "brn_due" and line.rule == "today" for line in enabled)
    assert all(not line.allowed_answers for line in due)
    old = lines(build_morning(source), "look")
    assert any(line.rule == "coming_up" and line.item_ref == "brn_later" for line in old)
    assert any(line.rule == "coming_up" and line.item_ref == "brn_meeting" for line in old)


@pytest.mark.parametrize("state,day,length,expected", [
    ("open", True, None, True), ("open", False, None, False), ("open", True, 30, False),
    ("parked", True, None, False), ("done", True, None, False), ("dropped", True, None, False),
    ("no_longer_needed", True, None, False),
])
def test_review_asks_length_only_after_day_on_open_unsized_work(state, day, length, expected):
    data = {"dates": {"planned": when("2026-10-02")}} if day else {}
    if length:
        data["timing"] = {"duration_minutes": length, "padded_minutes": length, "estimated_by": "nic"}
    if state == "parked":
        data["parked_until"] = {"event": "someday"}
    record = task(state=state, data=data)
    morning = build_morning(inputs((record,)), new_lengths=True)
    questions = [line for line in lines(morning, "new") if line.rule == "needs_length"]
    assert bool(questions) is expected
    if expected:
        assert questions[0].text == "Task — How long?"
        assert questions[0].allowed_answers == ("15 min", "30 min", "1 hour", "2 hours")
        assert {"length", "rough_minutes"} <= set(questions[0].offered)
    assert not any(line.rule == "needs_length" for line in lines(build_morning(inputs((record,))), "new"))


def test_new_undated_thing_asks_when_before_length():
    record = task()
    morning = build_morning(inputs((record,)), new_lengths=True)
    line = next(line for line in lines(morning, "new") if line.item_ref == record.id)
    assert "When will you do it?" in line.text
    assert {"today", "another day", "park", "drop"} <= set(line.offered)
    assert "length" not in line.offered


def test_skipped_new_when_question_returns_until_day_or_park():
    from test_morning import history
    record = task()
    source = replace(inputs((record,), history=(history(record, "new_schedule"),)),
                     shown_first_ids=frozenset({record.id}))
    questions = lines(build_morning(source, new_lengths=True), "new")
    assert any(line.rule == "new_schedule" for line in questions)
    parked = replace(record, state="parked", data={**record.data, "parked_until": {"event": "someday"}})
    assert not any(line.rule in {"new_schedule", "needs_length"}
                   for line in lines(build_morning(replace(source, items=(replace(source.items[0], record=parked),)),
                                                  new_lengths=True), "new"))
