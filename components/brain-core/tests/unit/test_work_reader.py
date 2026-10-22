"""Observable W5 answers against the hand-built move-over, plus old-shape cases."""

from __future__ import annotations

import importlib.util
import random
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from brain_core.records import record_from_mapping, render_record
from brain_core.work import (
    allowed_answers, dates_of, explain, first_id, index_things, is_up,
    kind_of, length_of, needs_nic, next_of, progress_of, status_of, up_things, write_index,
)

_path = Path(__file__).resolve().parents[1] / "fixtures/work_model/moved_over.py"
_spec = importlib.util.spec_from_file_location("reader_moved_over", _path)
assert _spec and _spec.loader
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
TODAY = date(2026, 10, 1)


@pytest.fixture
def records():
    return fixture.moved_over_records()


@pytest.fixture
def index(records):
    return index_things(records)


def edited(index, ref, *, state=None, **data):
    values = dict(index)
    r = values[ref]
    values[ref] = replace(r, state=state or r.state, data=dict(deepcopy(r.data), **data))
    return values


def kinds(ref, index, now=TODAY, history=()):
    return {need["kind"] for need in needs_nic(ref, index, now, history)}


def test_exhibition_progress_wait_and_next(index):
    assert kind_of(fixture.Exhibition, index) == "task"
    assert kind_of(fixture.MEMO, index) == "subtask"
    assert progress_of(fixture.Exhibition, index) == {
        "done": 1, "total": 3, "text": "1 of 3", "started": True,
        "last_ticked": fixture.OFFICER, "last_ticked_at": "2026-09-30T12:00:12+00:00",
        "last_ticked_day": "2026-09-30",
    }
    next_item = next_of(fixture.Exhibition, index, TODAY)
    assert next_item["text"] == "Get a safety checklist from the inspector"
    assert next_item["item_ref"] == fixture.MEMO
    assert next_item["status"] == "waiting"
    assert next_item["until"] == "2026-10-08"
    assert "venue inspection" in next_item["reason"]
    assert status_of(fixture.Exhibition, index, TODAY) == "waiting"
    assert "nothing_next" not in kinds(fixture.Exhibition, index)


def test_on_eighth_memo_is_up_submit_still_waits(index):
    now = date(2026, 10, 8)
    assert is_up(fixture.MEMO, index, now)
    assert not is_up(fixture.SUBMIT, index, now)
    assert status_of(fixture.Exhibition, index, now) == "open"
    assert next_of(fixture.Exhibition, index, now)["item_ref"] == fixture.MEMO
    assert "after 'Get a safety checklist" in explain(fixture.SUBMIT, index, now)["reason"]
    assert fixture.MEMO in {first_id(r) for r in up_things(index, now)}
    assert fixture.SUBMIT not in {first_id(r) for r in up_things(index, now)}


def test_memo_tick_releases_submit_then_finish_line(index):
    index = edited(index, fixture.MEMO, state="done")
    assert next_of(fixture.Exhibition, index, date(2026, 10, 8))["item_ref"] == fixture.SUBMIT
    assert is_up(fixture.SUBMIT, index, date(2026, 10, 8))
    index = edited(index, fixture.SUBMIT, state="done")
    assert index[fixture.Exhibition].state == "open"
    assert next_of(fixture.Exhibition, index, TODAY)["reason"] == "nothing next"
    assert progress_of(fixture.Exhibition, index)["text"] == "3 of 3"
    line = next(n for n in needs_nic(fixture.Exhibition, index, TODAY) if n["kind"] == "nothing_next")
    assert line["text"] == "Done, or what's next?"  # no finish line was invented for him


def test_send_edit_then_send_up_and_nothing_next(index):
    assert next_of(fixture.SEND, index, TODAY)["item_ref"] == fixture.EDIT
    assert not is_up(fixture.SEND, index, TODAY)
    index = edited(index, fixture.EDIT, state="done")
    assert is_up(fixture.SEND, index, TODAY)
    assert next_of(fixture.SEND, index, TODAY)["item_ref"] == fixture.SEND
    assert "nothing_next" in kinds(fixture.SEND, index)


@pytest.mark.parametrize("state,status", [("parked", "parked"), ("done", "ended"), ("dropped", "ended"), ("no_longer_needed", "ended")])
def test_ancestor_state_is_derived_and_unparking_returns_parts(index, state, status):
    modified = edited(index, fixture.Exhibition, state=state, parked_until="2026-10-10")
    assert status_of(fixture.MEMO, modified, date(2026, 10, 8)) == status
    assert not is_up(fixture.MEMO, modified, date(2026, 10, 8))
    assert is_up(fixture.MEMO, index, date(2026, 10, 8))
    assert index[fixture.MEMO].state == "open"


def test_open_action_precedes_parts_and_ticking_it_keeps_task_open(index):
    with_action = edited(index, fixture.Exhibition, next_action={"text": "Call the coordinator", "done": False})
    assert next_of(fixture.Exhibition, with_action, TODAY)["text"] == "Call the coordinator"
    ticked = edited(with_action, fixture.Exhibition, next_action={"text": "Call the coordinator", "done": True})
    assert next_of(fixture.Exhibition, ticked, TODAY)["item_ref"] == fixture.MEMO
    lone = edited(index, fixture.EMAIL, next_action={"text": "Export addresses", "done": True})
    assert "nothing_next" in kinds(fixture.EMAIL, lone)
    assert lone[fixture.EMAIL].state == "open"


def test_first_nonwaiting_part_not_first_order_only(index):
    modified = edited(index, fixture.SUBMIT, after=[])
    assert next_of(fixture.Exhibition, modified, TODAY)["item_ref"] == fixture.SUBMIT
    assert modified[fixture.MEMO].data["order"] < modified[fixture.SUBMIT].data["order"]


def test_length_sums_open_parts_plus_own_length(index):
    assert length_of(fixture.Exhibition, index) == 25
    modified = edited(index, fixture.Exhibition, timing={"duration_minutes": 7})
    assert length_of(fixture.Exhibition, modified) == 32
    modified = edited(modified, fixture.MEMO, state="done")
    assert length_of(fixture.Exhibition, modified) == 22


def test_old_shape_mapping_preserves_words_day_and_first_ids():
    old = fixture._helpers.load_records()
    index = index_things(old)
    submit = next_of(fixture.SUBMIT, index, TODAY)
    assert submit["status"] == "waiting"
    assert "after" in submit["reason"]
    assert index[fixture.SUBMIT].data["after"] == [fixture.MEMO]
    assert kind_of(fixture.Exhibition, index) == "goal"
    due = dates_of(fixture.MEMO, index)["deadline"]
    assert (due["words"], due["read_on"], due["day"]) == ("8 October", "2026-09-30", "2026-10-08")
    assert index[fixture.SEND].data["next_action"]["text"] == "Open your brief and send it to Morgan"
    assert {m["mark"] for m in index[fixture.SEND].data["review_marks"]} == {"agents_wording"}
    released = edited(index, fixture.MEMO, state="done")
    assert is_up(fixture.SUBMIT, released, date(2026, 10, 8))


def test_old_start_by_and_window_end(index):
    seed = index[fixture.EMAIL]
    old = replace(seed, data=dict(level="accepted", size="task", timing={
        "due": "2026-10-20", "start_by": "2026-10-10",
        "resolved_from": {"words": "before 20 October", "today": "2026-09-30"}}))
    mapped = index_things([old])
    assert dates_of(old, mapped)["start_by"] == {
        "day": "2026-10-10", "words": "before 20 October", "read_on": "2026-09-30", "read_by": "code"}
    old = replace(old, data=dict(level="accepted", size="task", timing={
        "window": {"start": "2026-10-01", "end": "2026-10-20"},
        "resolved_from": {"words": "October window", "today": "2026-09-30"}}))
    assert dates_of(old, index_things([old]))["deadline"]["day"] == "2026-10-20"


def test_old_waiting_maps_needs_expected_and_appointment(index):
    seed = index[fixture.EMAIL]
    old = replace(seed, state="waiting", links=({"type": "waiting_on", "target": "Rowan"},),
                  data=dict(level="accepted", size="task", timing={"due": "2026-10-02", "resolved_from": {"words": "Friday", "today": "2026-09-30"}}))
    mapped = index_things([old])
    assert mapped[first_id(old)].data["needs"]["from"] == "Rowan"
    assert explain(old, mapped, TODAY)["until"] == "2026-10-02"
    assert "deadline" not in dates_of(old, mapped)
    old = replace(old, state="open", links=(), data=dict(level="accepted", size="task", timing={
        "due": "2026-10-02", "at": "12:00", "resolved_from": {"words": "Friday at noon", "today": "2026-09-30"}}))
    assert dates_of(old, index_things([old]))["appointment"]["at"] == "12:00"


def test_marked_parts_inherit_labels_but_neither_stake_nor_deadline(records):
    current = index_things(records)
    parent = current[fixture.Exhibition]
    parent = replace(parent, data=dict(parent.data, review_marks=[],
                     dates={"deadline": {"words": "by 20 Oct", "day": "2026-10-20", "read_by": "code", "read_on": "2026-09-30"}}))
    part = current[fixture.MEMO]
    part = replace(part, data=dict(level="accepted", size="task", parent=fixture.Exhibition,
                                   review_marks=[{"mark": "agents_wording"}]))
    mapped = index_things([parent, part])
    assert mapped[fixture.MEMO].data["labels"] == ["exhibition admin"]
    assert "deadline" not in dates_of(part, mapped)
    assert "stake" not in mapped[fixture.MEMO].data
    cleared = replace(part, data=dict(part.data, review_marks=[]))
    mapped = index_things([parent, cleared])
    assert mapped[fixture.MEMO].data["stake"] == parent.data["stake"]
    assert dates_of(cleared, mapped)["deadline"]["day"] == "2026-10-20"
    own = replace(cleared, data=dict(cleared.data, labels=["own"], stake={"level": "nuisance", "confirmed": True},
                  dates={"deadline": {"day": "2026-10-21", "words": "21 Oct", "read_by": "code", "read_on": "2026-09-30"}}))
    mapped = index_things([parent, own])
    assert mapped[fixture.MEMO].data["labels"] == ["own"]
    assert mapped[fixture.MEMO].data["stake"]["level"] == "nuisance"
    assert dates_of(own, mapped)["deadline"]["day"] == "2026-10-21"


def test_agent_date_stake_start_by_need_confirming(index):
    proposed = {"words": "by 20 Oct", "day": "2026-10-20", "read_on": "2026-09-30", "read_by": "agent:diablo"}
    modified = edited(index, fixture.EMAIL, review_marks=[], dates={"deadline": proposed, "start_by": dict(proposed, day="2026-10-10")},
                      stake={"at_stake": "a late fee", "confirmed": False, "proposed_by": "agent:diablo"})
    lines = needs_nic(fixture.EMAIL, modified, TODAY)
    assert len(lines) == 3
    assert all(line["kind"] == "needs_you" for line in lines)
    assert any("start_by" in line["text"] for line in lines)
    modified = edited(modified, fixture.EMAIL, dates={key: dict(value, confirmed=True) for key, value in modified[fixture.EMAIL].data["dates"].items()},
                      stake={"at_stake": "a late fee", "confirmed": True, "proposed_by": "owner"})
    assert not needs_nic(fixture.EMAIL, modified, TODAY)


def test_suggested_date_needs_yes_even_when_read_by_code(index):
    modified = edited(index, fixture.EMAIL, review_marks=[], stake={"confirmed": True}, dates={"suggested": {
        "words": "Thursday", "kind": "deadline", "speaker": "Morgan", "day": "2026-10-01", "read_by": "code", "read_on": "2026-09-30"}})
    line = needs_nic(fixture.EMAIL, modified, TODAY)[0]
    assert line["kind"] == "needs_you"
    assert line["allowed_answers"] == ("yes", "no")


@pytest.mark.parametrize("kind,answers", [
    ("missed", ("recommit", "change", "defer", "drop")),
    ("did_it_happen", ("done", "move", "drop")),
    ("carried_over", ("today", "another day", "done already", "drop")),
    ("to_chase", ("draft a nudge", "leave it")),
    ("nothing_next", ("done", "what's next")),
    ("stalled", ("what's in the way", "next action")),
    ("needs_you", ("keep", "change", "drop")),
    ("carried_over_3_times", ("parts", "park", "drop")),
    ("over_two_hours", ("parts", "leave it")),
    ("agents_wording", ("keep", "change")), ("maybe_same", ("merge", "leave separate")),
    ("unclear", ("clarify", "drop")), ("question", ("answer", "leave it")),
    ("looks_like", ("yes", "no")), ("ask_stake", ("stake", "nothing much")),
    ("spelling", ("keep", "correct")), ("suggested", ("yes", "no")),
    ("proposed_stake", ("keep", "change")), ("proposed_start_by", ("keep", "change")),
    ("two_minutes", ("do it now", "leave it")),
])
def test_allowed_answers(kind, answers):
    assert allowed_answers(kind) == answers


def test_unknown_answer_kind_refused():
    with pytest.raises(KeyError):
        allowed_answers("invented")


def test_review_deadline_appointment_carried_over_and_long_task(index):
    assert "carried_over" in kinds(fixture.SEND, index)
    assert "missed" not in kinds(fixture.SEND, index)
    assert "did_it_happen" in kinds(fixture.Banner, index)
    modified = edited(index, fixture.EMAIL, dates={"deadline": {"day": "2026-09-30"}}, timing={"duration_minutes": 121})
    assert {"missed", "over_two_hours"} <= kinds(fixture.EMAIL, modified)
    lone = edited(modified, fixture.EMAIL, timing={"duration_minutes": 120})
    assert "over_two_hours" not in kinds(fixture.EMAIL, lone)


def test_history_first_id_across_three_versions_and_carried_only_once(index):
    initial = index[fixture.CAMPAIGN]
    versions = [initial]
    history = []
    for n in range(3):
        latest = versions[-1]
        history.append({"item_ref": latest.id, "rule": "carried_over", "surfaced_at": f"2026-10-0{n+1}T06:00:00+08:00"})
        versions.append(replace(latest, id=f"brn_2026100{n+1}_000000_{0xa000+n:016x}", supersedes=(*latest.supersedes, latest.id),
                                updated_at=datetime(2026, 10, n+1, tzinfo=timezone.utc)))
    mapped = index_things(versions)
    assert len(mapped) == 1
    assert first_id(mapped[fixture.CAMPAIGN]) == fixture.CAMPAIGN
    lines = needs_nic(fixture.CAMPAIGN, mapped, date(2026, 10, 4), history)
    assert "carried_over" not in {line["kind"] for line in lines}
    assert next(line for line in lines if line["kind"] == "carried_over_3_times")["count"] == 3
    assert next_of(initial, mapped, TODAY)["item_ref"] == fixture.CAMPAIGN


def test_stalled_changes_in_parts_reset_clock(index):
    assert "stalled" in kinds(fixture.Exhibition, index, date(2026, 10, 3))
    assert "stalled" not in kinds(fixture.Exhibition, index, date(2026, 10, 2))
    updated = dict(index)
    updated[fixture.MEMO] = replace(updated[fixture.MEMO], updated_at=datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
    assert "stalled" not in kinds(fixture.Exhibition, updated, date(2026, 10, 3))


def test_to_chase_uses_wait_history_and_expected_day(index):
    modified = edited(index, fixture.EMAIL, review_marks=[], stake={"confirmed": True}, needs={"from": "Rowan", "what": "Hall forms"})
    history = [{"item_ref": fixture.EMAIL, "rule": "waiting_on", "surfaced_at": "2026-09-24T06:00:00+08:00"}]
    assert "to_chase" in kinds(fixture.EMAIL, modified, TODAY, history)
    modified = edited(modified, fixture.EMAIL, needs={"from": "Rowan", "expected": {"day": "2026-09-30"}})
    assert "to_chase" in kinds(fixture.EMAIL, modified)


def test_project_goal_and_vault_join_by_name(index):
    project = replace(index[fixture.EMAIL], data=dict(level="accepted", size="task", called="project"), title="Exosuit")
    task = replace(index[fixture.CAMPAIGN], links=({"type": "about", "target": " exOSUIT "},),
                   data=dict(level="accepted", size="task", timing={"duration_minutes": 30}))
    mapped = index_things([project, task])
    assert mapped[first_id(task)].data["parent"] == first_id(project)
    assert next_of(project, mapped, TODAY)["item_ref"] == first_id(task)
    assert length_of(project, mapped) == 30
    assert not is_up(project, mapped, TODAY)
    goal = replace(index[fixture.Exhibition], data=dict(level="accepted", size="task", called="goal"))
    project = replace(project, data=dict(project.data, parent=first_id(goal)))
    mapped = index_things([goal, project, task])
    assert next_of(goal, mapped, TODAY)["children"][0]["item_ref"] == first_id(task)
    assert kind_of(task, mapped) == "task"


def test_order_independent_and_inputs_unchanged(records):
    before = tuple(render_record(r) for r in records)
    expected = index_things(records)
    answers = [(ref, next_of(ref, expected, TODAY), progress_of(ref, expected), dates_of(ref, expected),
                length_of(ref, expected), needs_nic(ref, expected, TODAY)) for ref in expected]
    for seed in range(8):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)
        actual = index_things(shuffled)
        assert [(ref, next_of(ref, actual, TODAY), progress_of(ref, actual), dates_of(ref, actual),
                 length_of(ref, actual), needs_nic(ref, actual, TODAY)) for ref in actual] == answers
        assert up_things(actual, TODAY) == up_things(expected, TODAY)
    assert tuple(render_record(r) for r in records) == before


def test_fixture_history_provenance_and_no_speculative_projects(records, index):
    for r in records:
        assert record_from_mapping(r.as_dict()).id == r.id
        assert r.source["said_by"] == "other"
        assert r.source["recorded_by"] == "fixture:synthetic-exhibition"
    assert not any(r.data.get("called") in {"goal", "project"} and r.state == "open" for r in index.values())
    assert not any(r.id in fixture._helpers.manifest()["test_data_ids"] for r in records)
    assert index[fixture.ADMIN].state == "no_longer_needed"
    labelled = {ref for ref, r in index.items() if "exhibition admin" in r.data.get("labels", ()) and kind_of(ref, index) == "task"}
    assert {fixture.Exhibition, fixture.VENUE_RENTAL, fixture.ExhibitCover, fixture.Printworks} <= labelled
    assert "parent" not in index[fixture.Printworks].data
    assert index[fixture.VENUE_RENTAL].data["timing"] == {}
    assert index[fixture.Exhibition].data["timing"] == {}
    assert not index[fixture.VENUE_RENTAL].data["stake"]["confirmed"]


def test_missing_blocker_waits_and_relationship_cycle_fails(index):
    modified = edited(index, fixture.EMAIL, after=["brn_20261001_000000_000000000000ffff"])
    assert status_of(fixture.EMAIL, modified, TODAY) == "waiting"
    cyclic = edited(index, fixture.Exhibition, parent=fixture.MEMO)
    with pytest.raises(ValueError, match="cycle"):
        index_things(cyclic.values())


def test_four_am_day_boundary(index):
    before = datetime.fromisoformat("2026-10-08T03:59:00+08:00")
    after = datetime.fromisoformat("2026-10-08T04:00:00+08:00")
    assert not is_up(fixture.MEMO, index, before)
    assert is_up(fixture.MEMO, index, after)
    with pytest.raises(ValueError, match="aware"):
        is_up(fixture.MEMO, index, datetime(2026, 10, 8))


def test_mark_on_any_part_suppresses_inherited_facts(index):
    parent = replace(index[fixture.Exhibition], data=dict(level="accepted", size="task", labels=["exhibition admin"],
                     stake={"level": "hurts_weeks", "confirmed": True}, dates={"deadline": {"day": "2026-10-20"}}))
    for mark in ("maybe_same", "unclear", "question", "looks_like", "ask_stake", "spelling"):
        part = replace(index[fixture.MEMO], data=dict(level="accepted", size="task", parent=first_id(parent), review_marks=[{"mark": mark}]))
        mapped = index_things([parent, part])
        assert mapped[fixture.MEMO].data["labels"] == ["exhibition admin"]
        assert "stake" not in mapped[fixture.MEMO].data
        assert "deadline" not in dates_of(part, mapped)


def test_child_action_tick_counts_started_and_latest_tick(index):
    modified = edited(index, fixture.MEMO, next_action={"text": "Ask the inspector", "done": True})
    modified[fixture.MEMO] = replace(modified[fixture.MEMO], updated_at=datetime(2026, 10, 1, 10, tzinfo=timezone.utc))
    progress = progress_of(fixture.Exhibition, modified)
    assert progress["text"] == "1 of 3"
    assert progress["last_ticked"] == fixture.MEMO
    assert progress["started"]


def test_parked_event_until_is_plain_text(index):
    modified = edited(index, fixture.Exhibition, state="parked", parked_until={"event": "officer replies"})
    assert explain(fixture.MEMO, modified, TODAY)["until"] == "officer replies"


@pytest.mark.parametrize("repeat", ["daily", "weekly:thu"])
def test_open_routine_is_reader_candidate_and_waits_still_apply(repeat):
    records = fixture._helpers.load_records("made_up_post_audit")
    seed = next(r for r in records if r.data.get("repeat"))
    routine = record_from_mapping(replace(seed, data=dict(seed.data, repeat=repeat)).as_dict())
    from brain_core.routines import is_routine
    assert is_routine(routine)
    mapped = index_things([routine])
    assert is_up(routine, mapped, TODAY)
    assert up_things(mapped, TODAY) == (mapped[first_id(routine)],)
    waiting = replace(routine, data=dict(routine.data, timing={"on_event": "after breakfast"}))
    mapped = index_things([waiting])
    assert status_of(waiting, mapped, TODAY) == "waiting"
    assert not up_things(mapped, TODAY)
    parked = replace(routine, state="parked", data=dict(routine.data, parked_until="2026-10-10"))
    mapped = index_things([parked])
    assert status_of(parked, mapped, TODAY) == "parked"
    assert not up_things(mapped, TODAY)
    ended = record_from_mapping(replace(routine, state="done", data=dict(routine.data, check={
        "locator": "synthetic routine completion", "checked_by": "fixture:checker"})).as_dict())
    mapped = index_things([ended])
    assert status_of(ended, mapped, TODAY) == "ended"
    assert not up_things(mapped, TODAY)


def test_write_index_child_move_does_not_persist_inherited_facts(records):
    current = index_things(records)
    raw = {first_id(r): r for r in records if r.id == current[first_id(r)].id}
    parent = replace(raw[fixture.Exhibition], data=dict(raw[fixture.Exhibition].data,
        dates={"deadline": {"day": "2026-10-20", "words": "by 20 Oct", "read_by": "code", "read_on": "2026-09-30"}}))
    child = raw[fixture.MEMO]
    inputs = [parent, child]
    before = tuple(render_record(r) for r in inputs)
    reader = index_things(inputs)
    assert reader[fixture.MEMO].data["labels"] == ["exhibition admin"]
    assert reader[fixture.MEMO].data["stake"] == parent.data["stake"]
    assert dates_of(child, reader)["deadline"]["day"] == "2026-10-20"
    writer = write_index(inputs)
    own = writer[fixture.MEMO]
    assert "labels" not in own.data
    assert "stake" not in own.data
    assert "deadline" not in own.data["dates"]
    # The move layer can add an action on this own-fact base without hoisting facts.
    value = own.as_dict()
    value["data"] = dict(value["data"], next_action={"text": "Ask the inspector", "done": False})
    moved = record_from_mapping(value)
    assert "stake" not in moved.data and "labels" not in moved.data
    assert "deadline" not in moved.data["dates"]
    assert moved.data["dates"]["not_before"]["day"] == "2026-10-08"
    assert tuple(render_record(r) for r in inputs) == before


def test_write_index_old_dates_waits_parent_and_provenance_survive():
    inputs = fixture._helpers.load_records()
    raw = index_things(inputs)
    writer = write_index(inputs)
    assert list(writer) == list(raw)
    assert {ref: r.id for ref, r in writer.items()} == {ref: r.id for ref, r in raw.items()}
    submit = writer[fixture.SUBMIT]
    assert submit.state == "open"
    assert submit.data["after"] == [fixture.MEMO]
    assert submit.data["parent"] == fixture.Exhibition
    assert submit.data["dates"]["deadline"] == dates_of(fixture.SUBMIT, raw)["deadline"]
    assert submit.data["timing"]["duration_minutes"] == 15
    assert "due" not in submit.data["timing"]
    assert not any(link["type"] in {"part_of", "blocked_by"} for link in submit.links)
    assert writer[fixture.Exhibition].data["called"] == "goal"
    assert writer[fixture.Exhibition].data["size"] == "task"
    assert "first_step" not in writer[fixture.Exhibition].data
    for ref, written in writer.items():
        original = next(r for r in inputs if r.id == written.id)
        assert written.source == original.source
        assert written.evidence == original.evidence
        assert written.body == original.body
        assert written.supersedes == original.supersedes
        assert record_from_mapping(written.as_dict()) == written


def test_write_index_wait_expected_time_unrelated_links_and_start_by(index):
    seed = index[fixture.EMAIL]
    links = ({"type": "waiting_on", "target": "Rowan"},
             {"type": "about", "target": "insurance"}, {"type": "came_from", "target": "family chat"})
    waiting = record_from_mapping(replace(seed, state="waiting", links=links,
        data=dict(level="accepted", size="task", reason="Hall forms", timing={
            "due": "2026-10-02", "at": "12:00", "duration_minutes": 5,
            "resolved_from": {"words": "Friday at noon", "today": "2026-09-30"}})).as_dict())
    own = write_index([waiting])[first_id(waiting)]
    assert own.state == "open"
    assert own.data["needs"] == {"from": "Rowan", "expected": {
        "day": "2026-10-02", "at": "12:00", "words": "Friday at noon", "read_by": "code", "read_on": "2026-09-30"}}
    assert own.links == links[1:]
    assert own.data["timing"] == {"duration_minutes": 5}
    assert own.source == waiting.source
    deadline = record_from_mapping(replace(waiting, state="open", links=(), data=dict(level="accepted", size="task", timing={
        "due": "2026-10-20", "start_by": "2026-10-10", "resolved_from": {"words": "by 20 Oct", "today": "2026-09-30"}})).as_dict())
    own = write_index([deadline])[first_id(deadline)]
    assert own.data["dates"]["deadline"]["day"] == "2026-10-20"
    assert own.data["dates"]["start_by"]["day"] == "2026-10-10"
    assert own.data["timing"] == {}


def test_write_index_retains_native_own_facts_and_untranslated_links(index):
    source = index[fixture.MEMO]
    own_data = dict(level="accepted", size="task", parent=fixture.Exhibition, labels=["mine"],
                    stake={"level": "nuisance", "confirmed": True, "proposed_by": "owner"},
                    dates={"deadline": {"day": "2026-10-21", "words": "by 21 Oct", "read_on": "2026-09-30", "read_by": "code"}})
    links = ({"type": "about", "target": "venue"}, {"type": "came_from", "target": "family chat"})
    source = record_from_mapping(replace(source, data=own_data, links=links).as_dict())
    own = write_index([source])[first_id(source)]
    assert own.data["labels"] == ["mine"]
    assert own.data["stake"] == own_data["stake"]
    assert own.data["dates"] == own_data["dates"]
    assert own.links == links


def test_write_index_routine_old_shape_and_occurrence_history_unchanged():
    seed = next(r for r in fixture._helpers.load_records("made_up_post_audit") if r.data.get("repeat"))
    old = record_from_mapping(replace(seed, data=dict(seed.data, repeat="weekly:thu",
        timing={"due": "2026-10-01", "resolved_from": {"words": "Thursday", "today": "2026-09-30"}},
        first_step="Open the form")).as_dict())
    latest = record_from_mapping(replace(old, id="brn_20261001_000000_000000000000b001", supersedes=(old.id,),
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc), updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        data=dict(old.data, occurrence={"day": "2026-10-01", "outcome": "done"})).as_dict())
    writer = write_index([latest, old])
    assert list(writer) == [old.id]
    assert writer[old.id].as_dict() == latest.as_dict()
    assert "dates" not in writer[old.id].data
    assert "next_action" not in writer[old.id].data
    assert writer[old.id].data["timing"]["due"] == "2026-10-01"
    assert writer[old.id].data["occurrence"]["outcome"] == "done"


def test_write_index_is_order_independent_and_uses_same_current_collapse(records):
    expected = write_index(records)
    reader = index_things(records)
    before = tuple(render_record(r) for r in records)
    for seed in range(5):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)
        actual = write_index(iter(shuffled))
        assert actual == expected
        assert {ref: r.id for ref, r in actual.items()} == {ref: r.id for ref, r in reader.items()}
    assert tuple(render_record(r) for r in records) == before


@pytest.mark.parametrize("surfaced", [
    datetime(2026, 9, 30, 22, tzinfo=timezone.utc),
    datetime.fromisoformat("2026-10-01T06:00:00+08:00"),
    "2026-09-30T22:00:00+00:00", "2026-09-30T22:00:00Z",
    "2026-10-01T06:00:00+08:00", "2026-10-01T07:00:00+09:00",
])
def test_carried_over_utc_and_offset_forms_are_same_personal_day(index, surfaced):
    from brain_core.review import HistoryRow
    if isinstance(surfaced, datetime):
        row = HistoryRow(fixture.SEND, "review", "carried_over", "fixture-sha", "open", surfaced)
    else:
        row = {"item_ref": fixture.SEND, "rule": "carried_over", "surfaced_at": surfaced}
    assert "carried_over" not in kinds(fixture.SEND, index, date(2026, 10, 2), [row])


def test_carried_over_uses_four_am_personal_date(index):
    # 02:00 on 1 Oct still belongs to 30 Sep; 04:00 belongs to 1 Oct.
    row = {"item_ref": fixture.SEND, "rule": "carried_over", "surfaced_at": "2026-09-30T18:00:00Z"}
    assert "carried_over" in kinds(fixture.SEND, index, date(2026, 10, 2), [row])
    row["surfaced_at"] = "2026-09-30T20:00:00Z"
    assert "carried_over" not in kinds(fixture.SEND, index, date(2026, 10, 2), [row])


@pytest.mark.parametrize("reverse", [False, True])
def test_to_chase_earliest_wait_is_chronological_across_mixed_offsets(index, reverse):
    modified = edited(index, fixture.EMAIL, review_marks=[], stake={"confirmed": True}, needs={"from": "Rowan"})
    # The calendar text sorts the later instant first. Personal days are 24 and 25 Sep.
    history = [
        {"item_ref": fixture.EMAIL, "rule": "waiting_on", "surfaced_at": "2026-09-25T05:00:00+14:00"},
        {"item_ref": fixture.EMAIL, "rule": "waiting_on", "surfaced_at": datetime.fromisoformat("2026-09-24T12:00:00-10:00")},
    ]
    if reverse:
        history.reverse()
    assert "to_chase" in kinds(fixture.EMAIL, modified, date(2026, 10, 1), history)
    assert "to_chase" not in kinds(fixture.EMAIL, modified, date(2026, 9, 30), history)


@pytest.mark.parametrize("explicit_child", [False, True])
def test_confirm_proposals_on_own_record_even_when_child_values_are_identical(index, explicit_child):
    proposal = {"level": "hurts_weeks", "at_stake": "The exhibition would be delayed", "confirmed": False, "proposed_by": "agent:diablo"}
    deadline = {"words": "by 20 October", "day": "2026-10-20", "read_by": "agent:diablo", "read_on": "2026-09-30"}
    parent = replace(index[fixture.Exhibition], data=dict(level="accepted", size="task", labels=["exhibition admin"],
                     stake=proposal, dates={"deadline": deadline}))
    child_data = dict(level="accepted", size="task", parent=fixture.Exhibition)
    if explicit_child:
        child_data.update(stake=deepcopy(proposal), dates={"deadline": deepcopy(deadline)})
    child = replace(index[fixture.MEMO], data=child_data)
    parent = record_from_mapping(parent.as_dict())
    child = record_from_mapping(child.as_dict())
    mapped = index_things([child, parent])
    assert mapped[fixture.MEMO].data["stake"] == mapped[fixture.Exhibition].data["stake"]
    assert dates_of(child, mapped)["deadline"] == dates_of(parent, mapped)["deadline"]
    parent_lines = [n for n in needs_nic(parent, mapped, TODAY) if n["kind"] == "needs_you"]
    child_lines = [n for n in needs_nic(child, mapped, TODAY) if n["kind"] == "needs_you"]
    assert len(parent_lines) == 2
    assert all(n["item_ref"] == fixture.Exhibition for n in parent_lines)
    assert len(child_lines) == (2 if explicit_child else 0)
    assert all(n["item_ref"] == fixture.MEMO for n in child_lines)
    # Provenance is transient, outside Record.data/as_dict, including the write seam.
    own = write_index([child, parent])[fixture.MEMO]
    assert ("stake" in own.data) == explicit_child
    assert ("deadline" in own.data.get("dates", {})) == explicit_child
    assert set(mapped[fixture.MEMO].as_dict()) == set(child.as_dict())


@pytest.mark.parametrize("called", ["goal", "project", None])
@pytest.mark.parametrize("child_state", ["done", "dropped", "no_longer_needed"])
def test_all_actual_parts_ended_asks_finish_line_at_every_level(index, called, child_state):
    data = dict(level="accepted", size="task", finish_line="permit approved")
    if called:
        data["called"] = called
    parent = replace(index[fixture.Exhibition], data=data)
    child = replace(index[fixture.CAMPAIGN], state=child_state, data=dict(level="accepted", size="task", parent=fixture.Exhibition,
        reason="no longer part of the plan", check={"locator": "synthetic completion", "checked_by": "fixture:checker"}))
    mapped = index_things([record_from_mapping(parent.as_dict()), record_from_mapping(child.as_dict())])
    answer = next_of(parent, mapped, TODAY)
    assert answer["reason"] == "nothing next"
    assert answer["children"] == ()
    line = next(n for n in needs_nic(parent, mapped, TODAY) if n["kind"] == "nothing_next")
    assert line["text"] == "permit approved: done, or what's next?"
    assert mapped[fixture.Exhibition].state == "open"


@pytest.mark.parametrize("called", ["goal", "project"])
@pytest.mark.parametrize("state", ["done", "parked"])
def test_all_ended_parts_do_not_ask_under_ended_or_parked_parent(index, called, state):
    parent = replace(index[fixture.Exhibition], state=state, data=dict(level="accepted", size="task", called=called,
        parked_until="2026-10-10", check={"locator": "synthetic completion", "checked_by": "fixture:checker"}))
    child = replace(index[fixture.CAMPAIGN], state="done", data=dict(level="accepted", size="task", parent=fixture.Exhibition,
        check={"locator": "synthetic completion", "checked_by": "fixture:checker"}))
    mapped = index_things([record_from_mapping(parent.as_dict()), record_from_mapping(child.as_dict())])
    assert not needs_nic(parent, mapped, TODAY)
    assert next_of(parent, mapped, TODAY)["reason"] != "nothing next"


def test_goal_keeps_each_open_child_next_and_wait(index):
    goal = replace(index[fixture.Exhibition], data=dict(level="accepted", size="task", called="goal"))
    child = replace(index[fixture.MEMO], data=dict(level="accepted", size="task", parent=fixture.Exhibition,
        dates={"not_before": {"words": "8 October", "day": "2026-10-08", "read_by": "code", "read_on": "2026-09-30"}}))
    mapped = index_things([goal, child])
    answer = next_of(goal, mapped, TODAY)
    assert answer["status"] == "waiting"
    assert answer["children"][0]["item_ref"] == fixture.MEMO
    assert answer["children"][0]["until"] == "2026-10-08"
    assert "nothing_next" not in kinds(fixture.Exhibition, mapped)
    assert next_of(goal, mapped, date(2026, 10, 8))["children"][0]["status"] == "up"


def test_unrepresented_legacy_wait_preserves_due_words_provenance_and_state(index):
    seed = index[fixture.EMAIL]
    waiting = record_from_mapping(replace(seed, state="waiting", links=({"type": "about", "target": "venue"},),
        data=dict(level="accepted", size="task", reason="waiting for the venue", first_step="Check the venue reply", timing={
            "due": "2026-10-08", "resolved_from": {"words": "8 October", "today": "2026-09-30"}})).as_dict())
    mapped = index_things([waiting])
    assert "needs" not in mapped[first_id(waiting)].data
    assert status_of(waiting, mapped, TODAY) == "waiting"
    assert explain(waiting, mapped, TODAY)["until"] == "2026-10-08"
    written = write_index([waiting])[first_id(waiting)]
    assert written.as_dict() == waiting.as_dict()
    assert written.state == "waiting"
    assert "needs" not in written.data and "whose" not in written.data
    assert written.data["timing"]["resolved_from"] == {"words": "8 October", "today": "2026-09-30"}
    # A mixed conversion is refused with the actual fix; it cannot invent a person.
    from brain_core.errors import BrainError
    mixed = replace(waiting, data=dict(waiting.data, next_action={"text": "Ask the venue", "done": False}))
    with pytest.raises(BrainError, match="keep the old shape unchanged") as error:
        write_index([mixed])
    assert error.value.details["field"] == "state"
    assert "actual needs.from/whose" in error.value.details["fix"]


def test_write_index_about_record_id_survives_translated_part_of():
    raw = fixture._helpers.load_records()
    parent = next(r for r in raw if r.id == "brn_20260930_035026_d000000000000028")
    child = next(r for r in raw if r.id == fixture.MEMO)
    about = {"type": "about", "target": parent.id}
    child = record_from_mapping(replace(child, links=(*child.links, about)).as_dict())
    written = write_index([parent, child])[fixture.MEMO]
    assert written.data["parent"] == fixture.Exhibition
    assert written.links == (about,)
    assert written.source == child.source and written.evidence == child.evidence


def test_write_index_consumes_only_named_project_about_that_supplied_parent(index):
    project = record_from_mapping(replace(index[fixture.EMAIL], title="Exosuit", data=dict(level="accepted", size="task", called="project")).as_dict())
    about = {"type": "about", "target": " exOSUIT "}
    seed = replace(index[fixture.CAMPAIGN], data=dict(level="accepted", size="task"), links=(about,))
    task = record_from_mapping(seed.as_dict())
    written = write_index([project, task])[first_id(task)]
    assert written.data["parent"] == first_id(project)
    assert written.links == ()
    native = record_from_mapping(replace(task, data=dict(task.data, parent=first_id(project))).as_dict())
    assert write_index([project, native])[first_id(task)].links == (about,)
    # A prior part_of supplied the parent, so about by name was not consumed.
    linked = record_from_mapping(replace(seed, links=({"type": "part_of", "target": project.id}, about)).as_dict())
    assert write_index([project, linked])[first_id(task)].links == (about,)


@pytest.mark.parametrize("event_field", ["on_event", "on_return"])
def test_write_index_personless_event_wait_is_mapped_and_remains_readable(index, event_field):
    waiting = record_from_mapping(replace(index[fixture.EMAIL], state="waiting", links=(),
        data=dict(level="accepted", size="task", first_step="Check the venue reply", timing={
            event_field: "the venue replies", "duration_minutes": 5,
            "resolved_from": {"words": "when the venue replies", "today": "2026-09-30"}})).as_dict())
    before = index_things([waiting])
    own = write_index([waiting])[first_id(waiting)]
    assert own.state == "open"
    assert "needs" not in own.data and "whose" not in own.data
    assert own.data["dates"]["not_before"] == {
        "event": "the venue replies", "words": "when the venue replies",
        "read_on": "2026-09-30", "read_by": "code"}
    assert own.data["timing"] == {"duration_minutes": 5}
    assert record_from_mapping(own.as_dict()) == own
    after = index_things([own])
    assert dates_of(waiting, before) == dates_of(own, after)
    assert length_of(waiting, before) == length_of(own, after)
    assert kind_of(waiting, before) == kind_of(own, after)
    assert status_of(waiting, before, TODAY) == status_of(own, after, TODAY) == "waiting"
    assert explain(waiting, before, TODAY) == explain(own, after, TODAY)
    assert next_of(waiting, before, TODAY) == next_of(own, after, TODAY)
    assert needs_nic(waiting, before, TODAY) == needs_nic(own, after, TODAY)
    # W6 can now write its own next action without inventing a wait owner.
    changed = replace(own, data=dict(own.data, next_action={"text": "Call the venue", "done": False}))
    assert record_from_mapping(changed.as_dict()) == changed
    assert own.source == waiting.source and own.evidence == waiting.evidence


@pytest.mark.parametrize("event_field", ["on_event", "on_return"])
@pytest.mark.parametrize("at", [None, "12:30"])
def test_write_index_preserves_personless_event_and_due_wait_in_mixed_batch(index, event_field, at):
    timing = {event_field: "the venue replies", "due": "2026-10-08", "duration_minutes": 5,
              "resolved_from": {"words": "when the venue replies, due 8 October", "today": "2026-09-30"}}
    if at:
        timing["at"] = at
    legacy = record_from_mapping(replace(index[fixture.EMAIL], state="waiting",
        links=({"type": "about", "target": "venue"}, {"type": "came_from", "target": "message"}),
        data=dict(level="accepted", size="task", reason="waiting for the venue",
                  first_step="Check the venue reply", timing=timing)).as_dict())
    neighbor = index[fixture.CAMPAIGN]
    inputs = [legacy, neighbor]
    raw = [deepcopy(r.as_dict()) for r in inputs]
    before = index_things(inputs)
    written = write_index(inputs)
    own = written[first_id(legacy)]
    assert own.as_dict() == raw[0]
    assert written[first_id(neighbor)].as_dict() == raw[1]
    assert all(record_from_mapping(r.as_dict()) == r for r in written.values())
    after = index_things(written.values())
    assert status_of(legacy, before, TODAY) == "waiting"
    assert dates_of(legacy, before)["not_before"]["words"] == timing["resolved_from"]["words"]
    for read in (kind_of, dates_of, length_of, progress_of):
        assert read(legacy, before) == read(own, after)
    for read in (status_of, explain, next_of, needs_nic):
        assert read(legacy, before, TODAY) == read(own, after, TODAY)
    assert [r.as_dict() for r in inputs] == raw

    from brain_core.errors import BrainError, ErrorCode
    mixed = replace(legacy, data=dict(legacy.data, next_action={"text": "Call the venue", "done": False}))
    with pytest.raises(BrainError, match="keep the old shape unchanged") as error:
        write_index([mixed, neighbor])
    assert error.value.code == ErrorCode.CONTRACT_INVALID
    assert error.value.details["record_id"] == legacy.id
    assert error.value.details["field"] == "data.timing.due"
    assert "actual needs.from/whose" in error.value.details["fix"]
    assert [r.as_dict() for r in inputs] == raw


@pytest.mark.parametrize("appointment", [False, True])
def test_write_index_preserves_start_by_without_deadline_in_mixed_batch(index, appointment):
    timing = {"start_by": "2026-10-05", "duration_minutes": 15,
              "resolved_from": {"words": "start by 5 October", "today": "2026-09-30"}}
    if appointment:
        timing.update(due="2026-10-08", at="12:30")
    legacy = record_from_mapping(replace(index[fixture.EMAIL], links=({"type": "about", "target": "venue"},),
        data=dict(level="accepted", size="task", first_step="Call the venue", timing=timing)).as_dict())
    neighbor = index[fixture.CAMPAIGN]
    inputs = [neighbor, legacy]
    raw = [deepcopy(r.as_dict()) for r in inputs]
    before = index_things(inputs)
    assert "deadline" not in dates_of(legacy, before)
    assert dates_of(legacy, before)["start_by"]["day"] == "2026-10-05"
    written = write_index(inputs)
    own = written[first_id(legacy)]
    assert own.as_dict() == raw[1]
    assert written[first_id(neighbor)].as_dict() == raw[0]
    assert all(record_from_mapping(r.as_dict()) == r for r in written.values())
    after = index_things(written.values())
    for read in (kind_of, dates_of, length_of, progress_of):
        assert read(legacy, before) == read(own, after)
    for read in (status_of, explain, next_of, needs_nic):
        assert read(legacy, before, TODAY) == read(own, after, TODAY)
    assert [r.as_dict() for r in inputs] == raw

    from brain_core.errors import BrainError, ErrorCode
    mixed = replace(legacy, data=dict(legacy.data, next_action={"text": "Book the appointment", "done": False}))
    with pytest.raises(BrainError, match="keep the old shape unchanged") as error:
        write_index([neighbor, mixed])
    assert error.value.code == ErrorCode.CONTRACT_INVALID
    assert error.value.details["record_id"] == legacy.id
    assert error.value.details["field"] == "data.timing.start_by"
    assert "actual deadline" in error.value.details["fix"]
    assert [r.as_dict() for r in inputs] == raw


@pytest.mark.parametrize("about_first", [False, True])
def test_explicit_part_of_wins_named_about_in_both_link_orders(index, about_first):
    parent = index[fixture.Exhibition]
    project = record_from_mapping(replace(index[fixture.EMAIL], title="Exosuit",
        data=dict(level="accepted", size="task", called="project")).as_dict())
    part_of = {"type": "part_of", "target": parent.id}
    named_about = {"type": "about", "target": " exOSUIT "}
    id_about = {"type": "about", "target": parent.id}
    links = (named_about, part_of, id_about) if about_first else (part_of, named_about, id_about)
    child = record_from_mapping(replace(index[fixture.CAMPAIGN], links=links,
        data=dict(level="accepted", size="task")).as_dict())
    inputs = [child, project, parent]
    before = index_things(inputs)
    assert before[first_id(child)].data["parent"] == first_id(parent)
    own = write_index(inputs)[first_id(child)]
    assert own.data["parent"] == first_id(parent)
    assert own.links == (named_about, id_about)
    assert record_from_mapping(own.as_dict()) == own
    assert index_things([own, project, parent])[first_id(child)].data == before[first_id(child)].data


_RESIDUE_ROWS = {
    "blocked_without_blocker_first_step": (dict(state="blocked", first_step="Call them"), "state", "blocking thing's first ID"),
    "blocked_without_blocker_due": (dict(state="blocked", timing={"due": "2026-10-08"}), "state", "blocking thing's first ID"),
    "blocked_by_free_text": (dict(state="blocked", links=({"type": "blocked_by", "target": "the landlord"},), first_step="Do it"),
                             "data.after[0]", "blocking thing's first ID"),
    "part_of_unknown_project": (dict(links=({"type": "part_of", "target": "No Such Project"},), first_step="Do it"),
                                "links[0]", "part_of link means"),
    "open_with_waiting_on_link": (dict(links=({"type": "waiting_on", "target": "Inspector Lee"},), first_step="Do it"),
                                  "links[0]", "waiting_on link means"),
    "due_and_window_with_other_end": (dict(timing={"due": "2026-10-08", "window": {"start": "2026-10-01", "end": "2026-10-10"}}),
                                      "data.timing.window", "restate timing.window"),
    "on_event_and_on_return": (dict(timing={"on_event": "the venue replies", "on_return": "back from leave"}),
                               "data.timing.on_return", "restate timing.on_return"),
    # Valid old rows over the frozen work bounds (plan 3.1): never cut or dropped.
    "waiting_on_name_over_128": (dict(state="waiting", links=({"type": "waiting_on", "target": "J" * 200},), first_step="Chase"),
                                 "data.needs.from", "in 128 characters"),
    "date_words_over_256": (dict(timing={"due": "2026-10-20", "resolved_from": {"words": "w" * 300, "today": "2026-09-30"}}),
                            "data.timing.resolved_from.words", "in 256 characters"),
    "blocked_by_21_records": (dict(state="blocked", links=tuple({"type": "blocked_by", "target": f"brn_20260101_000000_{n:04x}"}
                                                              for n in range(21))), "data.after", "20 or fewer"),
    # A record ID names a thing, not a person.
    "waiting_on_record_id": (dict(state="waiting", links=({"type": "waiting_on", "target": fixture.MEMO},), first_step="Chase"),
                             "links[0]", "turn a record ID into a person"),
    # No stored original words: never invented from the day or event.
    "due_without_words": (dict(timing={"due": "2026-10-20", "resolved_from": None}), "data.timing.resolved_from", "corrected owner quote"),
    "event_without_words": (dict(timing={"on_event": "the venue replies", "resolved_from": None}),
                            "data.timing.resolved_from", "corrected owner quote"),
}


@pytest.mark.parametrize("row", sorted(_RESIDUE_ROWS))
def test_write_index_preserves_unrepresentable_legacy_facts_in_mixed_batch(index, row):
    facts, field, fix = _RESIDUE_ROWS[row]
    facts = dict(facts)
    state, links = facts.pop("state", "open"), facts.pop("links", ())
    if "timing" in facts:
        timing = dict({"resolved_from": {"words": "the old words", "today": "2026-09-30"}}, **facts["timing"])
        facts["timing"] = {key: value for key, value in timing.items() if value is not None}
    links = (*links, {"type": "came_from", "target": "message"})
    legacy = record_from_mapping(replace(index[fixture.EMAIL], state=state, links=links,
        data=dict(level="accepted", size="task", reason="r", importance={"level": "high", "reason": "rent", "proposed_by": "agent", "confirmed": False}, **facts)).as_dict())
    neighbor = index[fixture.CAMPAIGN]
    inputs = [legacy, neighbor]
    raw = [deepcopy(r.as_dict()) for r in inputs]
    before = index_things(inputs)
    written = write_index(inputs)
    own = written[first_id(legacy)]
    # Exact old bytes: state, links, timing and its provenance, source and body.
    assert own.as_dict() == raw[0]
    assert render_record(own) == render_record(legacy)
    assert written[first_id(neighbor)] == write_index([neighbor])[first_id(neighbor)]
    assert all(record_from_mapping(r.as_dict()) == r for r in written.values())
    after = index_things(written.values())
    for read in (kind_of, dates_of, length_of, progress_of):
        assert read(legacy, before) == read(own, after)
    for read in (status_of, explain, next_of, needs_nic):
        assert read(legacy, before, TODAY) == read(own, after, TODAY)
    assert [r.as_dict() for r in inputs] == raw

    from brain_core.errors import BrainError, ErrorCode
    mixed = replace(legacy, data=dict(legacy.data, next_action={"text": "Call the venue", "done": False}))
    with pytest.raises(BrainError, match="keep the old shape unchanged") as error:
        write_index([neighbor, mixed])
    assert error.value.code == ErrorCode.CONTRACT_INVALID
    assert error.value.details["record_id"] == legacy.id
    assert error.value.details["field"] == field
    assert fix in error.value.details["fix"]
    assert [r.as_dict() for r in inputs] == raw


def test_write_index_representable_first_step_still_maps_beside_preserved_peer(index):
    legacy = record_from_mapping(replace(index[fixture.EMAIL], state="blocked", links=(),
        data=dict(level="accepted", size="task", reason="r", first_step="Call them")).as_dict())
    plain = record_from_mapping(replace(index[fixture.CAMPAIGN], links=(),
        data=dict(level="accepted", size="task", first_step="Draft the brief")).as_dict())
    written = write_index([legacy, plain])
    assert written[first_id(legacy)].as_dict() == legacy.as_dict()
    mapped = written[first_id(plain)].data
    assert mapped["next_action"] == {"text": "Draft the brief", "done": False}
    assert {"mark": "agents_wording", "part": "next_action"} in mapped["review_marks"]
    assert "first_step" not in mapped


def test_write_index_native_malformed_graph_field_still_rejects(index):
    from brain_core.errors import BrainError
    native = record_from_mapping(replace(index[fixture.CAMPAIGN], links=(),
        data=dict(level="accepted", size="task", next_action={"text": "Do it", "done": False})).as_dict())
    bad = replace(native, data=dict(native.data, after=["the landlord"]))
    with pytest.raises(BrainError, match=r"data\.after\[\]"):
        write_index([bad])


@pytest.mark.parametrize("surfaced", [
    ["2026-10-01T06:00:00+08:00"] * 3,  # one carry logged three times
    ["2026-10-01T06:00:00+08:00", "2026-09-30T22:00:00+00:00", "2026-10-01T09:30:00+08:00"],  # same personal day
])
def test_repeated_logging_of_one_carry_counts_once(index, surfaced):
    history = [{"item_ref": fixture.SEND, "rule": "carried_over", "surfaced_at": at} for at in surfaced]
    assert "carried_over_3_times" not in kinds(fixture.SEND, index, date(2026, 10, 4), history)


def test_three_carries_on_distinct_days_still_count_three(index):
    history = [{"item_ref": fixture.SEND, "rule": "carried_over", "surfaced_at": f"2026-10-0{n}T06:00:00+08:00"}
               for n in (1, 1, 2, 3)]
    line = next(n for n in needs_nic(fixture.SEND, index, date(2026, 10, 4), history) if n["kind"] == "carried_over_3_times")
    assert line["count"] == 3
    assert line["allowed_answers"] == ("parts", "park", "drop")


@pytest.mark.parametrize("source", [
    {"said_by": "other", "came_by": "direct"},
    {"said_by": "owner", "came_by": "connected_source"},
])
def test_others_suggested_thing_needs_you_with_yes_to_accept(index, source):
    modified = edited(index, fixture.EMAIL, level="suggested", review_marks=[])
    modified[fixture.EMAIL] = replace(modified[fixture.EMAIL], source=dict(modified[fixture.EMAIL].source or {}, **source))
    line = next(n for n in needs_nic(fixture.EMAIL, modified, TODAY) if "Someone else's suggestion" in n["text"])
    assert line["kind"] == "needs_you"
    assert line["allowed_answers"] == allowed_answers("suggested") == ("yes", "no")
    own = edited(index, fixture.EMAIL, level="suggested", review_marks=[])
    own[fixture.EMAIL] = replace(own[fixture.EMAIL], source=dict(own[fixture.EMAIL].source or {}, said_by="owner", came_by="direct"))
    assert not any("Someone else's suggestion" in n["text"] for n in needs_nic(fixture.EMAIL, own, TODAY))


def test_answer_kind_opt_in_disambiguates_same_answers_without_changing_default_needs(index):
    import json
    from hashlib import sha256
    seed = index[fixture.ExhibitCover]
    date = replace(seed, links=(), data={"level": "accepted", "size": "task", "dates": {
        "suggested": {"kind": "deadline", "day": "2026-10-02", "speaker": "fixture",
                      "words": "Friday", "read_by": "agent", "read_on": "2026-09-30"}}})
    project = replace(date, data={"level": "accepted", "size": "task", "review_marks": [
        {"mark": "looks_like", "kind": "project", "text": "Is this a project?"}]})
    # Explicit synthetic speaker/evidence pair; no audited owner words are
    # attributed to someone else, and the frozen source/evidence guard applies.
    other = replace(date, data={"level": "suggested", "size": "task"},
        evidence=(replace(seed.evidence[-1], locator="synthetic:reader-other-suggestion",
                          excerpt="A suggestion", content_sha256=sha256(b"A suggestion").hexdigest()),),
        source={**seed.source, "said_by": "other", "came_by": "direct", "speaker": "fixture", "words": "A suggestion"})
    mixed = replace(date, data={**date.data, "review_marks": project.data["review_marks"]})
    cases = ((date, ("Keep suggested: Friday?",), ("date_suggested",)),
             (project, ("Is this a project?",), ("looks_like",)),
             (other, ("Someone else's suggestion: keep, change or drop?",), ("suggested",)),
             (mixed, ("Is this a project?", "Keep suggested: Friday?"), ("looks_like", "date_suggested")))
    for record, words, kinds in cases:
        # Parser-valid native inputs, not fabricated metadata or selection hints.
        record = record_from_mapping(record.as_dict())
        before = record.as_dict()
        reader = index_things((record,))
        expected = tuple(dict(kind="needs_you", item_ref=first_id(record), text=text,
                              count=0, allowed_answers=("yes", "no")) for text in words)
        default = needs_nic(record, reader, TODAY)
        assert default == expected
        assert needs_nic(record, reader, TODAY, with_answer_kind=False) == expected
        detailed = needs_nic(record, reader, TODAY, with_answer_kind=True)
        assert tuple(need["answer_kind"] for need in detailed) == kinds
        stripped = tuple({key: value for key, value in need.items() if key != "answer_kind"}
                         for need in detailed)
        assert json.dumps(stripped) == json.dumps(default) == json.dumps(expected)
        assert record.as_dict() == before


def test_default_reader_needs_shape_stays_identical_across_shared_fixture(index):
    import json
    for record in index.values():
        default = needs_nic(record, index, TODAY)
        explicit = needs_nic(record, index, TODAY, with_answer_kind=False)
        detailed = needs_nic(record, index, TODAY, with_answer_kind=True)
        assert all(set(need) == {"kind", "item_ref", "text", "count", "allowed_answers"} for need in default)
        stripped = tuple({key: value for key, value in need.items() if key != "answer_kind"}
                         for need in detailed)
        assert json.dumps(default) == json.dumps(explicit) == json.dumps(stripped)


@pytest.mark.parametrize("reader", ["code", "system:test-bridge", "hermes-wa"])
@pytest.mark.parametrize("confirmation", [None, False, True])
def test_date_keep_offer_respects_explicit_confirmation_and_original_reader(index, reader, confirmation):
    seed = index[fixture.ExhibitCover]
    value = dict(words="start by Monday", day="2026-10-05", read_by=reader, read_on="2026-09-30")
    if confirmation is not None:
        value["confirmed"] = confirmation
    record = record_from_mapping(replace(seed, links=(), data={"level": "accepted", "size": "task", "dates": {
        "deadline": dict(words="by 9 October", day="2026-10-09", read_by="code", read_on="2026-09-30"),
        "start_by": value}}).as_dict())
    before = render_record(record)
    mapped = index_things([record])
    asked = confirmation is False or confirmation is None and reader == "hermes-wa"
    expected = (dict(kind="needs_you", item_ref=first_id(record), text="Keep start_by: start by Monday?",
                     count=0, allowed_answers=("keep", "change")),) if asked else ()
    assert needs_nic(record, mapped, TODAY) == expected
    assert needs_nic(record, mapped, TODAY, with_answer_kind=True) == tuple(
        {**need, "answer_kind": "proposed_start_by"} for need in expected)
    assert render_record(record) == before


def test_read_projection_never_invents_date_words_and_matches_r7_when_stored(index):
    from brain_core.language import _mapped_old_dates
    seed = index[fixture.EMAIL]
    bare = record_from_mapping(replace(seed, links=(), data=dict(level="accepted", size="task", timing={"due": "2026-10-20"})).as_dict())
    deadline = dates_of(bare, index_things([bare]))["deadline"]
    assert deadline["day"] == "2026-10-20" and "words" not in deadline
    assert write_index([bare])[first_id(bare)].as_dict() == bare.as_dict()
    # W0's Sam promise has no stored words: its projection claims none, its write keeps the old shape.
    raw = fixture._helpers.load_records()
    sam = next(r for r in raw if r.title.startswith("Sam wedding photos"))
    assert "words" not in index_things(raw)[first_id(sam)].data["dates"]["deadline"]
    assert write_index(raw)[first_id(sam)].as_dict() == max(
        (r for r in raw if first_id(r) == first_id(sam)), key=lambda r: len(r.supersedes)).as_dict()
    # Real stored words carry exactly as R7's boundary maps them.
    worded = record_from_mapping(replace(bare, data=dict(bare.data, timing={
        "due": "2026-10-02", "start_by": "2026-09-30", "resolved_from": {"words": "by Friday", "today": "2026-09-26"}})).as_dict())
    assert write_index([worded])[first_id(worded)].data["dates"] == _mapped_old_dates(worded)


def test_native_about_link_is_not_turned_into_a_parent(index):
    project = record_from_mapping(replace(index[fixture.EMAIL], title="Exosuit",
        data=dict(level="accepted", size="task", called="project")).as_dict())
    about = {"type": "about", "target": "Exosuit"}
    native = record_from_mapping(replace(index[fixture.CAMPAIGN], links=(about,),
        data=dict(level="accepted", size="task", labels=["work"])).as_dict())
    read = index_things([project, native])[first_id(native)]
    assert "parent" not in read.data and read.links == (about,)
    assert write_index([project, native])[first_id(native)].as_dict() == native.as_dict()
    assert kind_of(native, index_things([project, native])) == "task"
    # The same named about on an old-shape record still joins the project.
    old = record_from_mapping(replace(native, data=dict(level="accepted", size="task")).as_dict())
    written = write_index([project, old])[first_id(old)]
    assert written.data["parent"] == first_id(project) and written.links == ()
    # An explicit native parent still resolves and inherits.
    child = record_from_mapping(replace(native, data=dict(native.data, parent=first_id(project))).as_dict())
    assert index_things([project, child])[first_id(child)].data["parent"] == first_id(project)


def test_old_waiting_on_record_id_reads_as_that_dependency_not_a_person(index):
    raw = fixture._helpers.load_records()
    memo = max((r for r in raw if first_id(r) == fixture.MEMO), key=lambda r: len(r.supersedes))
    target = memo.id if memo.supersedes else fixture.MEMO  # any version resolves to the first ID
    waits = record_from_mapping(replace(index[fixture.EMAIL], state="waiting",
        links=({"type": "waiting_on", "target": target},),
        data=dict(level="accepted", size="task", reason="boxes", timing={"due": "2026-10-08", "resolved_from": {
            "words": "8 October", "today": "2026-09-30"}})).as_dict())
    mapped = index_things([*raw, waits])
    assert "needs" not in mapped[first_id(waits)].data and "whose" not in mapped[first_id(waits)].data
    assert explain(waits, mapped, TODAY) == {"status": "waiting", "reason": f"waiting on {fixture.MEMO}", "until": "2026-10-08"}
    assert write_index([*raw, waits])[first_id(waits)].as_dict() == waits.as_dict()
    # A person's old wait still reads through needs as before.
    person = record_from_mapping(replace(waits, links=({"type": "waiting_on", "target": "Inspector Lee"},)).as_dict())
    assert explain(person, index_things([person]), TODAY)["reason"] == "waiting on Inspector Lee, expected Thu 8 Oct"


def test_fixture_titles_questions_and_officer_body_follow_their_sources(index):
    """M01/M04 exact phrases, plan 9.3's question, the officer quote (W0 bytes untouched)."""
    import json
    raw = json.loads((fixture.ROOT / "messages_30_sep.json").read_text(encoding="utf-8"))
    messages = {m["id"]: m["message"] for m in raw["messages"]}
    submit, edit = index[fixture.SUBMIT].title, index[fixture.EDIT].title
    assert submit == "Officially submit my permit along with the memo on Exhibit Portal"
    assert submit.casefold() in messages["M01"].casefold()
    assert edit == "Edit my brief" and edit.casefold() in messages["M04"].casefold()
    assert {"mark": "question", "text": "Did it happen?"} in index[fixture.Banner].data["review_marks"]
    assert "did_it_happen" in kinds(fixture.Banner, index)
    officer = index[fixture.OFFICER]
    assert officer.body.splitlines()[0] == "I basically texted my coordinator" == officer.data["reason"]
    assert "I basically texted my coordinator" in messages["M01"]
    assert "Actual tick time unknown; version timestamps are migration timestamps." in officer.body
    # The actual tick time stays unknown: no started or done time is invented for it.
    assert "started_at" not in officer.data and officer.updated_at == officer.created_at
    # A finish line is his words: none is invented, the first review asks (plan 9.3, R13).
    assert "finish_line" not in index[fixture.Exhibition].data
    assert {"mark": "question", "text": "Its finish line, in your words?"} in index[fixture.Exhibition].data["review_marks"]
