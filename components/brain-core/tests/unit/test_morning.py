"""Work-02 morning behavior over the shared W0/W5 reconstruction, never live data."""
from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain_core.review import (
    HistoryRow, ItemRecord, MorningBootstrap, MorningInputs, ReviewInputs, build_morning, build_review, render_morning,
)
from brain_core.work import first_id, index_things

_path = Path(__file__).resolve().parents[1] / "fixtures/work_model/moved_over.py"
_spec = importlib.util.spec_from_file_location("morning_moved_over", _path)
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
NOW = datetime(2026, 9, 30, 22, tzinfo=timezone.utc)


def inputs(records=(), *, now=NOW, history=(), status=None, previous_review_at=None):
    return MorningInputs(tuple(ItemRecord(r, f"records/{r.id}.md") for r in records), (), (),
                         tuple(history), status or {}, now, previous_review_at=previous_review_at)


def known_inputs(records, **kwargs):
    """Synthetic pinned test input inventory, never a fabricated prior review."""
    return replace(inputs(records, **kwargs), bootstrap=MorningBootstrap(
        "a" * 40, frozenset(first_id(record) for record in records)))


def task(name="Task", *, key="brn_task", data=None, state="open", supersedes=(), updated=None):
    base = index_things(fixture.moved_over_records())[fixture.ExhibitCover]
    return replace(base, id=key, title=name, supersedes=supersedes, state=state,
                   created_at=NOW - timedelta(days=1), updated_at=updated or NOW - timedelta(days=1),
                   data={"level": "accepted", "size": "task", **(data or {})})


def when(day, **extra):
    return dict(day=day, words=day, read_by="code", read_on="2026-09-30", **extra)


def test_sequential_questions_wait_for_actual_completion():
    from brain_core import work
    question = [{"mark": "question", "text": "What's next?"}]
    first = task("First fictional step", key="brn_chain_a", data={"review_marks": question})
    second = task("Second fictional step", key="brn_chain_b", data={"after": [first.id], "review_marks": question,
        "dates": {"planned": when("2026-10-01")}})
    third = task("Third fictional step", key="brn_chain_c", data={"after": [second.id], "review_marks": question})
    def asked(records):
        index = work.index_things(records)
        morning = build_morning(known_inputs(records), new_lengths=True)
        return {line.item_ref for part in morning.parts[1:] for line in part.lines
                if line.item_ref in index and line.rule != "waiting_context"
                and work.status_of(line.item_ref, index, NOW) != "ended"}
    assert asked((first, second, third)) == {first.id}
    assert asked((replace(first, title="Accepted fictional wording"), second, third)) == {first.id}
    assert asked((replace(first, state="done"), second, third)) == {second.id}
    assert asked((replace(first, state="done"), replace(second, state="done"), third)) == {third.id}
    assert work.has_unfinished_prerequisites(second, work.index_things((replace(first, state="dropped"), second)))
    assert work.has_unfinished_prerequisites(second, work.index_things((second,)))
    parent = task("Fictional container", key="brn_chain_parent", data={"after": [first.id]})
    child = task("Fictional child", key="brn_chain_child", data={"parent": parent.id, "review_marks": question})
    assert child.id not in asked((first, parent, child))
    person_wait = task("Fictional human wait", key="brn_chain_wait", data={"whose": "Fictional colleague", "review_marks": question})
    assert person_wait.id in asked((person_wait,))


def lines(morning, part=None):
    return tuple(line for p in morning.parts for line in p.lines if part is None or line.section == part)


def history(record, rule, now=NOW, **extra):
    return HistoryRow(first_id(record), "review", rule, record.sha256, record.state, now, **extra)


def test_first_morning_matches_memo_226_without_invented_data():
    records = fixture.moved_over_records()
    before = [r.sha256 for r in records]
    morning = build_morning(inputs(records))
    look = "\n".join(line.text for line in lines(morning, "look"))
    catch = "\n".join(line.text for line in lines(morning, "catch_up"))
    new = "\n".join(line.text for line in lines(morning, "new"))
    day = "\n".join(line.text for line in lines(morning, "your_day"))
    assert "Nothing at a set time" in look
    assert "Get a safety checklist from the inspector" in look and "Thu 8 Oct" in look
    assert "venue inspection" in look
    assert "Bring Banner to the printer" in catch and "Did it happen? · Done, move or drop?" in catch
    assert "Send Morgan" in catch and "Edit my brief" in catch
    assert "Plan the Atlas outreach campaign" in catch and "Today, another day, done already, or drop?" in catch
    assert "Venue rental" in new and "three parts" in new and "co-organizer" in new
    assert "Export exhibition mailing list" in new and "Park it, or what's next?" in new
    assert "project with 'Plan the Atlas outreach campaign'" in new
    assert "Save the ExhibitCover acknowledgement" in day
    assert "Switch Printworks to the Standard plan" in day
    assert {row.item_ref for row in lines(morning, "your_day") if row.rule == "your_day"} == {
        fixture.ExhibitCover, fixture.Printworks}
    waiting = next(row for row in lines(morning, "your_day") if row.rule == "waiting_context")
    assert "Waiting, can't be picked: Exhibition permit" in waiting.text
    assert "1 of 3" in waiting.text and "Thu 8 Oct" in waiting.text
    assert "on Thu 8 Oct (venue inspection)" in waiting.text and "waiting until" not in waiting.text
    assert waiting.allowed_answers == waiting.offered == ()
    exhibition = index_things(records)[fixture.Exhibition]
    assert waiting.item_ref == fixture.Exhibition and waiting.record_sha256 == exhibition.sha256
    assert waiting.record_path == f"records/{exhibition.id}.md"
    assert "Is that your day?" not in render_morning(morning)
    assert before == [r.sha256 for r in records]


@pytest.mark.parametrize("count", [0, 3, 15])
def test_add_prompt_closes_new_without_a_number_thing_answer_or_count(count):
    records = tuple(task(f"New {i}", key=f"brn_new_{i}") for i in range(count))
    morning = build_morning(inputs(records))
    new = lines(morning, "new")
    prompt = new[-1]
    assert prompt.rule == "add_prompt"
    assert prompt.text == "Anything new to add before you pick your day? Say it and it's saved."
    assert prompt.number == 0 and prompt.item_ref is None
    assert prompt.record_sha256 is prompt.record_path is prompt.record_state is None
    assert prompt.allowed_answers == prompt.offered == ()
    assert sum(row.rule == "add_prompt" for row in lines(morning)) == 1
    assert f"{count} new, then your day" in morning.short
    text = render_morning(morning)
    assert f"\n{prompt.text}\n## Your day: pick 3 to 7" in text
    numbers = [row.number for row in lines(morning) if row.number]
    assert numbers == list(range(1, len(numbers) + 1))
    if count > 10:
        assert new[-2].rule == "part_cap" and len(new) == 12


def test_known_banner_appointment_stays_in_catch_up_with_raw_pins_not_your_day():
    records = fixture.moved_over_records()
    # Complete known inventory from the shared pre-move fixture, including test
    # records; its manifest names the audited input version. No prior review rows.
    bootstrap = MorningBootstrap(fixture._helpers.manifest()["audit_store_version"],
        frozenset(first_id(record) for record in fixture._helpers.load_records()))
    collected = replace(inputs(records), bootstrap=bootstrap,
        items=tuple(ItemRecord(record, f"records/item/2026/09/{record.id}.md") for record in records))
    before = [record.as_dict() for record in records]
    index = index_things(records)
    banner = index[fixture.Banner]
    from brain_core import work
    assert collected.history == ()
    assert work.status_of(banner, index, NOW) == "up"
    assert any(need["kind"] == "did_it_happen" for need in work.needs_nic(banner, index, NOW))
    morning = build_morning(collected)
    catch = next(row for row in lines(morning, "catch_up") if row.item_ref == fixture.Banner)
    assert catch.rule == "did_it_happen"
    assert catch.text == f"{banner.title} — was yesterday at 12 pm. Did it happen? · Done, move or drop?"
    assert catch.allowed_answers == ("done", "move", "drop")
    assert catch.offered == ("done", "move", "drop")
    assert catch.record_path == f"records/item/2026/09/{banner.id}.md"
    assert catch.record_sha256 == banner.sha256
    assert not any(row.item_ref == fixture.Banner for row in lines(morning, "your_day"))
    assert {fixture.ExhibitCover, fixture.Printworks} <= {row.item_ref for row in lines(morning, "your_day")}
    numbers = [row.number for row in lines(morning) if row.number]
    assert numbers == list(range(1, len(numbers) + 1))
    assert before == [record.as_dict() for record in records]


def test_your_day_keeps_unpassed_appointments_and_leaves_earlier_answers_in_their_parts():
    # Separate synthetic controls; their dates/titles are test premises, not W0 claims.
    today = task("Today appointment", key="brn_today", data={"dates": {"appointment": when("2026-10-01", at="12:00")}})
    future = task("Future appointment", key="brn_future", data={"dates": {"appointment": when("2026-10-02", at="12:00")}})
    missed = task("Missed deadline", key="brn_missed", data={"dates": {"deadline": when("2026-09-30")}})
    carried = task("Carried plan", key="brn_carried", data={"dates": {"planned": when("2026-09-30")}})
    marked = task("Ordinary task needing an answer", key="brn_marked", data={
        "review_marks": [{"mark": "question", "text": "Keep this wording?"}]})
    ordinary = task("Ordinary untouched task", key="brn_ordinary")
    records = (today, future, missed, carried, marked, ordinary)
    morning = build_morning(replace(inputs(records), bootstrap=MorningBootstrap(
        "a" * 40, frozenset(first_id(record) for record in records))))
    assert {row.item_ref for row in lines(morning, "your_day")} == {today.id, future.id, ordinary.id}
    assert any(row.item_ref == today.id and row.rule == "today" for row in lines(morning, "look"))
    assert any(row.item_ref == future.id and row.rule == "coming_up" for row in lines(morning, "look"))
    assert not any(row.rule == "did_it_happen" for row in lines(morning))
    assert any(row.item_ref == missed.id and row.rule == "missed" for row in lines(morning, "catch_up"))
    assert any(row.item_ref == carried.id and row.rule == "carried_over" for row in lines(morning, "catch_up"))
    assert any(row.item_ref == marked.id and row.rule == "needs_you" for row in lines(morning, "new"))


@pytest.mark.parametrize("part", ["catch_up", "new"])
def test_all_built_earlier_questions_own_membership_even_past_the_cap(part):
    records = tuple(task(f"Asked {i}", key=f"brn_asked_{i}", data={
        "dates": {"deadline": when("2026-09-30")}} if part == "catch_up" else {
        "review_marks": [{"mark": "question", "text": f"Keep wording {i}?"}]}) for i in range(12))
    ordinary = task("Untouched control", key="brn_ordinary")
    morning = build_morning(known_inputs((*records, ordinary)))
    asked = lines(morning, part)
    overflow = asked[-2] if part == "new" else asked[-1]
    assert len(asked) == (12 if part == "new" else 11) and overflow.fields_used["remaining"] == 2
    assert overflow.rule == "part_cap"
    assert {row.item_ref for row in lines(morning, "your_day")} == {ordinary.id}
    other = "new" if part == "catch_up" else "catch_up"
    assert not {row.item_ref for row in lines(morning, other)} & {record.id for record in records}
    numbers = [row.number for row in lines(morning) if row.number]
    assert numbers == list(range(1, len(numbers) + 1))


def test_folded_parent_parts_and_all_questions_keep_one_part_and_current_raw_versions():
    parent = task("Send", key="brn_parent", data={"dates": {"planned": when("2026-09-30")},
        "review_marks": [{"mark": "question", "text": "Make it a project? Its name?"}]})
    part = task("Edit", key="brn_part", data={"parent": parent.id, "order": 1,
        "dates": {"planned": when("2026-09-30")},
        "review_marks": [{"mark": "question", "text": "Keep Edit wording?"}]})
    done = task("Done part", key="brn_done", data={"parent": parent.id, "order": 2}, state="done")
    current = replace(part, id="brn_part_v2", supersedes=(part.id,), updated_at=NOW)
    ordinary = task("Untouched", key="brn_other")
    records = (parent, part, done, current, ordinary)
    before = [record.as_dict() for record in records]
    morning = build_morning(known_inputs(records))
    catch = lines(morning, "catch_up")
    carry = next(row for row in catch if row.item_ref == parent.id and row.rule == "carried_over")
    assert "1 of 2" in carry.text and "next: Edit" in carry.text
    new = lines(morning, "new")
    assert any("Make it a project? · Its name?" in row.text and "called" in row.offered for row in new)
    question = next(row for row in new if row.item_ref == part.id and row.rule == "needs_you")
    assert "Keep Edit wording?" in question.text and question.offered == ("answer", "leave it")
    assert question.record_path == f"records/{current.id}.md" and question.record_sha256 == current.sha256
    assert {row.item_ref for row in lines(morning, "your_day")} == {ordinary.id}
    assert before == [record.as_dict() for record in records]


def test_only_actual_current_version_answers_release_an_earlier_question():
    record = task("Question", key="brn_question", data={
        "review_marks": [{"mark": "question", "text": "Keep these words?"}]})
    current = replace(record, id="brn_current", supersedes=(record.id,), updated_at=NOW)
    ordinary = task("Untouched", key="brn_other")
    original = build_morning(known_inputs((record, ordinary)))
    assert {row.item_ref for row in lines(original, "your_day")} == {ordinary.id}
    old_answer = history(record, "needs_you", response="answered", responded_at=NOW)
    # A response to the old hash cannot silently answer a changed raw version.
    changed = build_morning(known_inputs((record, current, ordinary), history=(old_answer,)))
    assert any(row.item_ref == record.id and row.record_sha256 == current.sha256 for row in lines(changed, "new"))
    answered = history(current, "needs_you", response="answered", responded_at=NOW)
    released = build_morning(known_inputs((record, current, ordinary), history=(answered,)))
    assert [row.rule for row in lines(released, "new")] == ["add_prompt"]
    assert {row.item_ref for row in lines(released, "your_day")} == {record.id, ordinary.id}
    shown = next(row for row in lines(released, "your_day") if row.item_ref == record.id)
    assert shown.record_sha256 == current.sha256 and shown.record_path == f"records/{current.id}.md"


@pytest.mark.parametrize("kind,data", [
    ("stake", {"stake": {"level": "changes_life", "confirmed": False, "at_stake": "Synthetic cost", "proposed_by": "fixture"}}),
    ("date", {"dates": {"deadline": {"day": "2026-10-02", "words": "Friday", "read_by": "agent", "read_on": "2026-09-30"}}}),
    ("title", {"review_marks": [{"mark": "agents_wording", "text": "Synthetic title"}]}),
    ("ask_stake", {"review_marks": [{"mark": "ask_stake", "text": "Synthetic stake question"}]}),
])
def test_r17_typed_side_questions_keep_up_work_pickable_with_exact_visible_pointer(kind, data):
    record = task(f"Side {kind}", data=data)
    before = record.as_dict()
    morning = build_morning(known_inputs((record,)))
    question = next(row for row in lines(morning, "new") if row.item_ref == record.id)
    pick = next(row for row in lines(morning, "your_day") if row.item_ref == record.id)
    assert pick.text.endswith(f"side question, see {question.number}")
    assert question.allowed_answers and "picked_today" not in question.allowed_answers
    assert pick.allowed_answers == ("picked_today", "not_now")
    assert pick.record_path == question.record_path == f"records/{record.id}.md"
    assert pick.record_sha256 == question.record_sha256 == record.sha256
    assert before == record.as_dict()
    if kind == "stake":
        neutral = task(f"Side {kind}")
        control = next(row for row in lines(build_morning(known_inputs((neutral,))), "your_day"))
        assert pick.fields_used["points"] == control.fields_used["points"]
        assert not record.data["stake"]["confirmed"]


def test_capped_side_question_has_no_invented_numeric_pointer_and_does_not_block_pick():
    blocking = tuple(task(f"Blocked {i}", key=f"brn_blocked_{i}", data={
        "review_marks": [{"mark": "question", "text": f"Parts {i}, yes?"}]}) for i in range(11))
    side = task("Side question outside New cap", key="brn_side", data={
        "review_marks": [{"mark": "ask_stake", "text": "What's at stake?"}]})
    morning = build_morning(known_inputs((*blocking, side)))
    assert lines(morning, "new")[-2].fields_used["remaining"] == 2
    assert side.id not in {row.item_ref for row in lines(morning, "new")}
    pick = next(row for row in lines(morning, "your_day") if row.item_ref == side.id)
    assert pick.text.endswith("side question in New; ask to see more")
    assert "see 0" not in pick.text
    assert {row.item_ref for row in lines(morning, "your_day")} == {side.id}


def test_mixed_blocking_and_side_question_blocks_parent_and_its_parts():
    parent = task("Unconfirmed parts", key="brn_parent", data={"review_marks": [
        {"mark": "question", "text": "Parts: book; call. Yes?"},
        {"mark": "ask_stake", "text": "What's at stake?"}]})
    part = task("Known open part", key="brn_part", data={"parent": parent.id})
    ordinary = task("Untouched", key="brn_other")
    morning = build_morning(known_inputs((parent, part, ordinary)))
    question = next(row for row in lines(morning, "new") if row.item_ref == parent.id)
    assert "Parts: book; call" in question.text and "What's at stake?" in question.text
    assert "parts" in question.offered and "stake" in question.offered
    assert {row.item_ref for row in lines(morning, "your_day")} == {ordinary.id}


def test_suggested_date_and_blocking_questions_share_answers_but_not_pick_authority():
    proposed = {"suggested": {**when("2026-10-02", kind="deadline", speaker="fixture"), "read_by": "agent"}}
    date = task("Own proposed date", key="brn_date_v2", supersedes=("brn_date",),
                data={"dates": proposed})
    project = task("Possible project", key="brn_project", data={"review_marks": [
        {"mark": "looks_like", "kind": "project", "text": "Is this a project?"}]})
    other = replace(task("Other person's suggestion", key="brn_other", data={"level": "suggested"}),
        source={"said_by": "other", "came_by": "direct", "speaker": "fixture", "words": "A suggestion"})
    mixed = task("Date and project question", key="brn_mixed", data={"dates": proposed,
        "review_marks": [{"mark": "looks_like", "kind": "project", "text": "Is this a project?"}]})
    ordinary = task("Untouched control", key="brn_control")
    records = (date, project, other, mixed, ordinary)
    before = [record.as_dict() for record in records]
    morning = build_morning(known_inputs(records))
    questions = {row.item_ref: row for row in lines(morning, "new") if row.item_ref is not None}
    assert set(questions) == {first_id(record) for record in (date, project, other, mixed)}
    assert all(row.allowed_answers == ("yes", "no") for row in questions.values())
    assert "Keep suggested: tomorrow?" in questions[first_id(date)].text
    assert "Looks like a project. Yes?" in questions[project.id].text
    assert "Someone else's suggestion" in questions[other.id].text
    assert "Keep suggested: tomorrow?" in questions[mixed.id].text
    assert "Looks like a project. Yes?" in questions[mixed.id].text
    picks = {row.item_ref: row for row in lines(morning, "your_day")}
    assert set(picks) == {first_id(date), ordinary.id}
    pick, question = picks[first_id(date)], questions[first_id(date)]
    assert pick.text.endswith(f"side question, see {question.number}")
    assert pick.allowed_answers == ("picked_today", "not_now")
    assert pick.record_path == question.record_path == f"records/{date.id}.md"
    assert pick.record_sha256 == question.record_sha256 == date.sha256
    assert "answer_kind" not in render_morning(morning)
    assert before == [record.as_dict() for record in records]


@pytest.mark.parametrize("minutes,rule", [(1, "two_minutes"), (10, "new_nothing_needed")])
def test_new_since_previous_review_uses_original_raw_capture_time(minutes, rule):
    cutoff = NOW - timedelta(hours=6)
    original = replace(task("Existing task", key="brn_20260928_220000_000000000000c001",
                            data={"timing": {"duration_minutes": minutes}}),
                       created_at=NOW - timedelta(days=2))
    successor = replace(original, id="brn_20260930_210000_000000000000c002", supersedes=(original.id,),
                        created_at=NOW - timedelta(hours=1), updated_at=NOW - timedelta(hours=1))
    captured = replace(task("Actual new capture", key="brn_20260930_210001_000000000000c003",
                            data={"timing": {"duration_minutes": minutes}}),
                       created_at=NOW - timedelta(hours=1), updated_at=NOW - timedelta(hours=1))
    boundary = replace(captured, id="brn_20260930_160000_000000000000c004", title="Exactly at cutoff",
                       created_at=cutoff, updated_at=cutoff)
    records = (original, successor, captured, boundary)
    before = [r.as_dict() for r in records]
    rows = lines(build_morning(inputs(records, previous_review_at=cutoff)), "new")
    assert [(row.item_ref, row.rule) for row in rows] == [(captured.id, rule), (None, "add_prompt")]
    assert rows[0].record_path == f"records/{captured.id}.md" and rows[0].record_sha256 == captured.sha256
    assert before == [r.as_dict() for r in records]


def test_previous_review_cutoff_keeps_preexisting_questions_and_other_parts():
    records = fixture.moved_over_records()
    original = build_morning(inputs(records))
    cutoff = NOW - timedelta(hours=6)
    filtered = build_morning(inputs(records, previous_review_at=cutoff))
    rows = lines(filtered, "new")
    assert not any(row.rule in {"new_nothing_needed", "two_minutes"} for row in rows)
    raw = {record.id: record for record in records}
    index = index_things(records)
    for key in (fixture.VENUE_RENTAL, fixture.EMAIL, fixture.SEND):
        row = next(row for row in rows if row.item_ref == key and row.rule == "needs_you")
        shown = raw[index[key].id]
        assert row.rule == "needs_you" and "For your review" in row.text
        assert row.record_sha256 == shown.sha256 and row.record_path == f"records/{shown.id}.md"
    for part in ("look", "catch_up"):
        assert tuple(replace(row, number=0) for row in lines(filtered, part)) == tuple(
            replace(row, number=0) for row in lines(original, part))
    def day_without_pointer_numbers(morning):
        result = []
        for row in lines(morning, "your_day"):
            if "side_question_ref" in row.fields_used:
                target = next(question for question in lines(morning, "new")
                              if question.item_ref == row.fields_used["side_question_ref"])
                assert row.text.endswith(f"side question, see {target.number}")
                row = replace(row, text=row.text.rsplit(" — side question, see ", 1)[0])
            result.append(replace(row, number=0))
        return tuple(result)
    assert day_without_pointer_numbers(filtered) == day_without_pointer_numbers(original)


def test_no_previous_review_keeps_existing_new_capture_default():
    record = task("Previously unshown task")
    default = inputs((record,))
    assert default.previous_review_at is None
    assert build_morning(default) == build_morning(replace(default, previous_review_at=None))
    assert any(row.rule == "new_nothing_needed" for row in lines(build_morning(default), "new"))


@pytest.mark.parametrize("code,words", [
    ("nothing_much", "nothing much"), ("nuisance", "a nuisance"),
    ("hurts_weeks", "hurts for weeks"), ("changes_life", "changes my life"),
])
def test_stake_levels_are_words_in_review_and_next_but_codes_in_records(code, words):
    from brain_core.orientation import OrientationInputs, overview, what_next
    proposed = task(data={"stake": {"level": code, "confirmed": False, "proposed_by": "fixture"}})
    before = proposed.as_dict()
    row = next(row for row in lines(build_morning(inputs((proposed,))), "new") if row.rule == "needs_you")
    assert row.text == f"Task — For your review: Keep proposed stake: {words}?"
    assert row.allowed_answers == ("keep", "change") and row.offered == ("keep", "change", "organise")
    confirmed = replace(proposed, data={**proposed.data, "stake": {**proposed.data["stake"], "confirmed": True}})
    review = ReviewInputs((ItemRecord(confirmed, "raw.md"),), (), frozenset(), (), (), {}, NOW)
    orient = OrientationInputs(review)
    pick = what_next(orient, NOW).top_pick
    assert pick is not None and words in pick.rules
    assert pick.fields_used["stake"]["level"] == code
    assert any(words in row.text for row in overview(orient, NOW).lines)
    assert proposed.as_dict() == before and confirmed.data["stake"]["level"] == code


@pytest.mark.parametrize("saved_question", ["Its finish line, in his words", "Its finish line, in his words?",
                                           "Its finish line, in your words?"])
def test_review_addresses_you_and_separates_questions_without_changing_saved_marks(saved_question):
    record = task(data={"review_marks": [{"mark": "question", "text": saved_question}],
                        "stake": {"level": "nothing_much", "confirmed": False, "proposed_by": "fixture"}})
    before = record.as_dict()
    row = next(row for row in lines(build_morning(inputs((record,))), "new") if row.rule == "needs_you")
    assert row.text == "Task — For your review: Its finish line, in your words? · Keep proposed stake: nothing much?"
    assert row.allowed_answers == ("answer", "leave it", "keep", "change")
    assert row.text in render_morning(build_morning(inputs((record,))))
    assert record.as_dict() == before


def test_duplicate_appointment_question_is_suppressed_but_different_question_and_need_survive():
    from brain_core.work import needs_nic
    record = task("Banner", key="brn_banner_v2", supersedes=("brn_banner",), data={
        "dates": {"appointment": when("2026-09-30", at="12:00")},
        "review_marks": [{"mark": "question", "text": "Did it happen?"},
                         {"mark": "question", "text": "Is it a project?"}]})
    before = record.as_dict()
    index = index_things((record,))
    assert any(n["kind"] == "needs_you" and n["text"] == "Did it happen?" for n in needs_nic(record, index, NOW))
    morning = build_morning(known_inputs((record,)))
    catch = next(row for row in lines(morning, "catch_up") if row.item_ref == "brn_banner")
    new = next(row for row in lines(morning, "new") if row.item_ref == "brn_banner")
    assert "Did it happen?" in catch.text and new.text == "Banner — For your review: Is it a project?"
    assert new.allowed_answers == ("answer", "leave it")
    assert render_morning(morning).count("Did it happen?") == 1
    only_repeat = replace(record, data={**record.data, "review_marks": record.data["review_marks"][:1]})
    assert [row.rule for row in lines(build_morning(known_inputs((only_repeat,))), "new")] == ["add_prompt"]
    assert record.as_dict() == before


def test_new_keeps_question_when_appointment_is_beyond_catch_up_display_cap():
    missed = [task(key=f"brn_late_{i}", data={"dates": {"deadline": when("2026-09-29")}}) for i in range(10)]
    appointment = task("Banner", key="brn_banner", data={
        "dates": {"appointment": when("2026-09-30", at="12:00")},
        "review_marks": [{"mark": "question", "text": "Did it happen?"}]})
    morning = build_morning(known_inputs((*missed, appointment)))
    assert not any(row.item_ref == appointment.id for row in lines(morning, "catch_up"))
    assert any(row.item_ref == appointment.id and row.text == "Banner — For your review: Did it happen?"
               for row in lines(morning, "new"))


def test_new_omits_only_repeated_question_within_one_saved_mark():
    record = task("Banner", data={"dates": {"appointment": when("2026-09-30", at="12:00")},
        "review_marks": [{"mark": "question", "text": "Did it happen? Is it a project?"}]})
    morning = build_morning(known_inputs((record,)))
    assert render_morning(morning).count("Did it happen?") == 1
    assert next(row for row in lines(morning, "new")).text == "Banner — For your review: Is it a project?"
    assert record.data["review_marks"][0]["text"] == "Did it happen? Is it a project?"


def test_question_separator_does_not_separate_trailing_parent_context():
    parent = task("Project", data={"size": "project", "dates": {"planned": when("2026-09-30")}})
    part = task("First part", key="brn_part", data={"size": "subtask", "parent": parent.id})
    row = next(row for row in lines(build_morning(known_inputs((parent, part))), "catch_up")
               if row.item_ref == parent.id and row.rule == "carried_over")
    assert "Today, another day, done already, or drop? (0 of 1; next: First part)" in row.text


@pytest.mark.parametrize("data,reason", [
    ({"dates": {"not_before": when("2026-10-08", event="venue inspection")}},
     "not before Thu 8 Oct (venue inspection)"),
    ({"whose": "Inspector Lee", "expected": when("2026-10-08")}, "waiting on Inspector Lee, expected Thu 8 Oct"),
    ({"needs": {"from": "Inspector Lee", "expected": when("2026-10-08")}}, "waiting on Inspector Lee, expected Thu 8 Oct"),
])
def test_waiting_dates_use_review_day_words_in_morning_reader_and_orientation(data, reason):
    from brain_core.orientation import OrientationInputs, overview
    from brain_core.work import explain
    waiting = task("Venue memo", data=data)
    before = waiting.as_dict()
    row = next(row for row in lines(build_morning(known_inputs((waiting,))), "your_day") if row.rule == "waiting_context")
    assert "waiting until Thu 8 Oct" in row.text and "2026-10-08" not in row.text
    why = explain(waiting, index_things((waiting,)), NOW)
    assert why["reason"] == reason and why["until"] == "2026-10-08"
    review = ReviewInputs((ItemRecord(waiting, "raw.md"),), (), frozenset(), (), (), {}, NOW)
    orient = overview(OrientationInputs(review), NOW)
    assert any(why["reason"] in row.text for row in orient.lines)
    assert all("2026-10-08" not in row.text for row in orient.lines)
    assert waiting.as_dict() == before


@pytest.mark.parametrize("now,day_words", [
    (NOW, "Thu 8 Oct"),
    (NOW + timedelta(days=5), "this Thursday, 8 Oct"),
])
def test_your_day_event_wait_uses_the_same_on_words_as_look_and_reader(now, day_words):
    from brain_core.work import explain
    event_date = when("2026-10-08", event="venue inspection")
    waiting = task("Venue memo", data={"dates": {
        "planned": dict(event_date), "not_before": dict(event_date)}})
    morning = build_morning(known_inputs((waiting,), now=now))
    context = next(row for row in lines(morning, "your_day") if row.rule == "waiting_context")
    reason = explain(waiting, index_things((waiting,)), now)["reason"]
    assert reason == f"on {day_words} (venue inspection)"
    assert reason in context.text and "waiting until" not in context.text
    assert reason in "\n".join(row.text for row in lines(morning, "look"))
    assert context.allowed_answers == context.offered == ()


def test_catch_up_offers_another_day_once_using_memo_words():
    record = task(data={"dates": {"planned": when("2026-09-30")}})
    row = next(row for row in lines(build_morning(known_inputs((record,))), "catch_up") if row.rule == "carried_over")
    assert row.allowed_answers == ("today", "another day", "done already", "drop")
    assert row.offered == ("today", "another day", "done already", "drop")


@pytest.mark.parametrize("completed", [False, True])
def test_bootstrap_identity_survives_changed_version_and_keeps_needs_you(completed):
    old = task("Old capture", key="brn_old")
    changed = replace(old, id="brn_changed", supersedes=(old.id,), title="Edited old capture",
                      created_at=NOW, updated_at=NOW)
    asked = task("Venue rental", key="brn_venue rental", data={"review_marks": [
        {"mark": "question", "text": "Three parts: book; call; sign. Yes?"}]})
    bootstrap = MorningBootstrap("a" * 40, frozenset((old.id, asked.id)), completed)
    facts = replace(inputs((old, changed, asked), previous_review_at=NOW - timedelta(hours=6)), bootstrap=bootstrap)
    before = [entry.record.as_dict() for entry in facts.items]
    rows = lines(build_morning(facts), "new")
    assert [(row.item_ref, row.rule) for row in rows] == [(asked.id, "needs_you"), (None, "add_prompt")]
    assert rows[0].record_sha256 == asked.sha256 and rows[0].record_path == f"records/{asked.id}.md"
    assert "parts" in rows[0].offered and "book; call; sign" in rows[0].text
    assert before == [entry.record.as_dict() for entry in facts.items]
    assert facts.history == ()  # bootstrap never manufactures carry/said-once history


@pytest.mark.parametrize("minutes,rule", [(1, "two_minutes"), (10, "new_nothing_needed")])
def test_first_bootstrap_lists_new_backdated_identity_and_excludes_actually_shown(minutes, rule):
    backdated = task("Genuine post-move capture with backdated clock", key="brn_new",
                     data={"timing": {"duration_minutes": minutes}})
    shown = task("Shown through old API", key="brn_seen_v2", supersedes=("brn_seen",),
                 data={"timing": {"duration_minutes": minutes}})
    cutoff = NOW - timedelta(hours=6)
    bootstrap = MorningBootstrap("b" * 40, frozenset())
    facts = replace(inputs((backdated, shown), previous_review_at=cutoff),
                    bootstrap=bootstrap, shown_first_ids=frozenset(("brn_seen",)))
    rows = lines(build_morning(facts), "new")
    assert [(row.item_ref, row.rule) for row in rows] == [(backdated.id, rule), (None, "add_prompt")]
    assert rows[0].record_sha256 == backdated.sha256
    # After bootstrap, the actual successful previous-run cutoff is still used.
    normal = replace(facts, bootstrap=replace(bootstrap, completed=True))
    assert [row.rule for row in lines(build_morning(normal), "new")] == ["add_prompt"]


def test_memo_bootstrap_known_inventory_does_not_hide_unanswered_old_marks():
    records = fixture.moved_over_records()
    facts = replace(inputs(records), bootstrap=MorningBootstrap("c" * 40,
                    frozenset(first_id(record) for record in records)))
    morning = build_morning(facts)
    assert not any(row.rule in {"new_nothing_needed", "two_minutes"} for row in lines(morning, "new"))
    for first in (fixture.VENUE_RENTAL, fixture.EMAIL, fixture.SEND):
        assert any(row.item_ref == first and row.rule == "needs_you" for row in lines(morning, "new"))
    assert any(row.rule == "carried_over" for row in lines(morning, "catch_up"))
    assert facts.history == ()


def test_raw_hash_path_and_first_identity_survive_reader_projection():
    parent = task("Parent", key="brn_parent", data={"stake": {"confirmed": True, "level": "changes_life", "at_stake": "$100", "proposed_by": "nic"}})
    child = task("Part", key="brn_part_v2", supersedes=("brn_part",), data={"parent": parent.id})
    morning = build_morning(inputs((parent, child)))
    rows = [line for line in lines(morning) if line.item_ref == "brn_part"]
    assert rows and all(line.record_sha256 == child.sha256 for line in rows)
    assert all(line.record_path == "records/brn_part_v2.md" for line in rows)
    assert any("$100" in line.text for line in rows)


def test_carried_over_once_and_parent_names_part_once():
    parent = task("Send", data={"dates": {"planned": when("2026-09-30")}})
    part = task("Edit", key="brn_part", data={"parent": parent.id, "dates": {"planned": when("2026-09-30")}})
    morning = build_morning(inputs((parent, part)))
    carry = [line for line in lines(morning) if line.rule == "carried_over"]
    assert len(carry) == 1 and "Edit" in carry[0].text
    following = build_morning(inputs((parent, part), now=NOW + timedelta(days=1), history=(history(parent, "carried_over"),)))
    assert not [line for line in lines(following) if line.rule == "carried_over" and line.item_ref == parent.id]
    assert not [line for line in lines(morning) if line.rule == "missed"]


def test_three_distinct_carried_versions_share_lasting_identity():
    original = task(data={"dates": {"planned": when("2026-09-25")}})
    versions = [replace(original, id=f"brn_v{i}", supersedes=(original.id,), updated_at=NOW - timedelta(days=4-i)) for i in range(3)]
    current = replace(original, id="brn_current", supersedes=(original.id, *(v.id for v in versions)),
                      data={**original.data, "dates": {"planned": when("2026-09-30")}})
    prior = tuple(history(v, "carried_over", NOW - timedelta(days=3-i)) for i, v in enumerate(versions))
    morning = build_morning(inputs((original, *versions, current), history=prior))
    row = next(line for line in lines(morning) if line.rule == "carried_over_3_times")
    assert row.item_ref == original.id
    assert "Still want this? · If yes, what's a smaller first step?" in row.text


def test_repeated_logging_of_one_carry_is_not_three_distinct_carries():
    record = task(data={"dates": {"planned": when("2026-09-30")}})
    one = history(record, "carried_over")
    assert not any(line.rule == "carried_over_3_times" for line in lines(
        build_morning(inputs((record,), now=NOW + timedelta(days=1), history=(one, one, one)))))


def test_other_suggestion_is_visible_in_needs_you_with_accept_offer():
    record = task("Morgan's portfolio date", data={"level": "suggested"})
    record = replace(record, source={"said_by": "other", "came_by": "direct", "speaker": "Morgan", "words": "send your portfolio"})
    rows = lines(build_morning(inputs((record,))), "new")
    assert any(line.rule == "needs_you" and "Someone else's suggestion" in line.text for line in rows)
    assert "accept" in rows[0].offered


def test_missed_deadline_daily_until_the_date_or_state_changes():
    record = task("Bill", data={"dates": {"deadline": when("2026-09-30")}})
    prior = (history(record, "missed"),)
    for offset in (0, 1, 2):
        morning = build_morning(inputs((record,), now=NOW + timedelta(days=offset), history=prior))
        assert any(line.rule == "missed" for line in lines(morning))
    changed = replace(record, id="brn_changed", supersedes=(record.id,), data={"level": "accepted", "dates": {"deadline": when("2026-10-10")}})
    assert not any(line.rule == "missed" for line in lines(build_morning(inputs((record, changed), history=prior))))
    assert not any(line.rule == "missed" for line in lines(build_morning(inputs((replace(record, state="done"),), history=prior))))


@pytest.mark.parametrize("part", ["look", "catch_up", "new", "your_day"])
def test_caps_each_part_and_keeps_continuous_numbers(part):
    records = tuple(task(f"Task {i}", key=f"brn_{i}", data={"dates": {"deadline": when("2026-10-01" if part == "look" else "2026-09-30")}} if part in {"look", "catch_up"} else {}) for i in range(15))
    morning = build_morning(known_inputs(records) if part == "your_day" else inputs(records))
    rows = lines(morning, part)
    overflow = rows[-2] if part == "new" else rows[-1]
    assert len(rows) == (12 if part == "new" else 11) and overflow.rule == "part_cap"
    assert overflow.text == "5 more; ask to see them"
    numbers = [line.number for line in lines(morning) if line.number]
    assert numbers == list(range(1, len(numbers) + 1))


def test_size_only_in_look_and_friday_line_only_on_friday():
    thursday = build_morning(inputs(status={"Brain Home size": "task records at 50%"}))
    assert "Brain Home size" in render_morning(thursday) and "Brain Home size" not in thursday.short
    assert lines(thursday, "look")[-1].rule == "brain_home_size"
    assert "Weekly review" not in thursday.short
    friday = build_morning(inputs(now=NOW + timedelta(days=1)))
    assert "Weekly review: Ready this afternoon" in friday.short


def test_size_remains_visible_when_look_has_more_than_ten_lines():
    records = tuple(task(f"Appointment {i}", key=f"brn_{i}", data={"dates": {"appointment": when("2026-10-01", at="12:00")}}) for i in range(15))
    morning = build_morning(inputs(records, status={"Brain Home size": "task records at 50%"}))
    assert len(lines(morning, "look")) == 11
    assert any(line.rule == "brain_home_size" for line in lines(morning, "look"))
    assert "Brain Home size" not in morning.short


def test_removed_sections_and_legacy_api_remain_separate():
    record = task(data={"dates": {"appointment": when("2026-10-02", at="12:00")}})
    morning = build_morning(inputs((record,)))
    assert not any(word in render_morning(morning) for word in ("Calendar entries to add", "To sort", "What's the first step", "## Waiting on"))
    old = ReviewInputs((ItemRecord(record, "raw.md"),), (), frozenset(), (), (), {}, NOW)
    assert build_review(old).sections  # work-01 still callable with its original inputs


def test_each_question_has_reader_answer_names_and_door_offers():
    record = task(data={"review_marks": [{"mark": "question", "text": "Three parts: book; call; sign. Yes?"}]})
    row = next(line for line in lines(build_morning(inputs((record,)))) if line.rule == "needs_you")
    assert row.allowed_answers == ("answer", "leave it")
    assert "parts" in row.offered and "book; call; sign" in row.text


def test_long_without_parts_offer_is_once_per_version_and_done_parts_feed_progress():
    long = task(data={"timing": {"duration_minutes": 121}})
    assert any(line.rule == "over_two_hours" for line in lines(build_morning(inputs((long,)))))
    assert not any(line.rule == "over_two_hours" for line in lines(build_morning(inputs((long,), history=(history(long, "over_two_hours"),)))))
    child = task("Done part", key="brn_child", data={"parent": long.id}, state="done")
    row = next(line for line in lines(build_morning(inputs((long, child)))) if line.rule == "nothing_next")
    assert "1 of 1" in row.text


def test_own_planned_today_is_preselected_and_confirmed_stake_is_pinned():
    planned = task("Planned", key="brn_planned", data={"dates": {"planned": when("2026-10-01")}})
    pinned = task("Pinned", key="brn_pinned", data={"stake": {"level": "changes_life", "confirmed": True, "at_stake": "$1500 a month", "proposed_by": "nic"}})
    rows = lines(build_morning(known_inputs((planned, pinned))), "your_day")
    assert rows[1].item_ref == pinned.id and "$1500 a month" in rows[1].text
    assert "already picked" in rows[0].text and rows[0].fields_used["preselected"]


def test_banner_two_mornings_later_keeps_the_absolute_day():
    morning = build_morning(inputs(fixture.moved_over_records(), now=NOW + timedelta(days=1)))
    row = next(row for row in lines(morning, "catch_up") if row.item_ref == fixture.Banner)
    assert "was at 12 pm on Wed 30 Sep" in row.text
    assert morning.short.startswith("Morning review, Fri 2 Oct\n")


@pytest.mark.parametrize("offset, title", [(0, "Thu 1 Oct"), (1, "Fri 2 Oct"), (5, "Tue 6 Oct")])
def test_fixture_review_title_always_names_its_absolute_day(offset, title):
    morning = build_morning(inputs(fixture.moved_over_records(), now=NOW + timedelta(days=offset)))
    assert morning.short.splitlines()[0] == f"Morning review, {title}"


def test_upcoming_dates_use_this_week_words():
    waiting = task("Memo", data={"dates": {"not_before": when("2026-10-08")}})
    now = NOW + timedelta(days=5)
    morning = build_morning(known_inputs((waiting,), now=now))
    assert "not before this Thursday, 8 Oct" in lines(morning, "look")[-1].text
    from brain_core.work import explain
    assert explain(waiting, index_things((waiting,)), now)["reason"] == "not before this Thursday, 8 Oct"


@pytest.mark.parametrize("selection", ["planned", "picked"])
def test_fixture_today_parent_keeps_its_next_part_already_picked(selection):
    records = fixture.moved_over_records()
    index = index_things(records)
    send = index[fixture.SEND]
    prior = ()
    if selection == "planned":
        revised = replace(send, id="brn_send_today", supersedes=(*send.supersedes, send.id),
                          data={**send.data, "dates": {"planned": when("2026-10-01")}})
        records = (*records, revised)
    else:
        prior = (history(send, "carried_over", response="picked_today", responded_at=NOW),)
    morning = build_morning(known_inputs(records, history=prior))
    row = next(row for row in lines(morning, "your_day") if row.item_ref == fixture.EDIT)
    assert row.text.startswith("Edit my brief (Send Morgan (Atlas) my brief; 0 of 1)")
    assert row.fields_used["preselected"] and "already picked" in row.text
    assert row.allowed_answers == ("picked_today", "not_now")
    assert not any(row.rule == "carried_over" and row.item_ref in {fixture.SEND, fixture.EDIT}
                   for row in lines(morning, "catch_up"))
