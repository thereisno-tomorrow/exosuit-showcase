from dataclasses import replace

from brain_core.review import ItemRecord, list_name_candidates, with_name_suggestions, build_morning
from test_morning import NOW, task, inputs, lines


def test_candidates_only_pending_names_with_original_words_and_children():
    first = replace(task("Original", key="brn_original"), source={"words": "I want to apply to Atlas"})
    current = task("Application", key="brn_current", supersedes=(first.id,), data={
        "review_marks": [{"mark": "question", "text": "Its name?"}], "labels": ["career"]})
    child = task("Send the CV", key="brn_cv", data={"parent": first.id})
    unrelated = task("Other", key="brn_other", data={"review_marks": [{"mark": "question", "text": "Its finish line?"}]})
    items = tuple(ItemRecord(r, f"{r.id}.md") for r in (first, current, child, unrelated))
    before = tuple(r.record.sha256 for r in items)
    (candidate,) = list_name_candidates(items, NOW)
    assert candidate.item_ref == first.id and candidate.title == "Application"
    assert candidate.first_words == "I want to apply to Atlas"
    assert candidate.tasks == ("Send the CV",) and candidate.labels == ("career",)
    assert before == tuple(r.record.sha256 for r in items)
    archived = replace(current, source={"words": "yes"}, body="I want to apply to Atlas")
    assert list_name_candidates((ItemRecord(archived, "current.md"),), NOW)[0].first_words == archived.body


def test_looks_like_project_or_goal_but_not_task_and_no_closed_or_routine():
    records = [task(kind, key=f"brn_{kind}", data={"review_marks": [{"mark": "looks_like", "kind": kind}]})
               for kind in ("project", "goal", "task")]
    records += [replace(records[0], id="brn_done", state="done"),
                replace(records[0], id="brn_routine", data={**records[0].data, "repeat": "daily"})]
    candidates = list_name_candidates(tuple(ItemRecord(r, f"{r.id}.md") for r in records), NOW)
    assert {c.item_ref for c in candidates} == {"brn_project", "brn_goal"}
    assert list_name_candidates((), NOW) == ()


def test_review_name_field_keeps_title_and_question_and_ignores_changed_version():
    record = task("Application", data={"review_marks": [{"mark": "looks_like", "kind": "project"}]})
    morning = build_morning(inputs((record,)))
    named = with_name_suggestions(morning, {record.id: (record.sha256, "Atlas application")})
    (line,) = [line for line in lines(named) if line.rule == "needs_you"]
    assert line.suggested_name == "Atlas application"
    assert line.text == next(line for line in lines(morning) if line.rule == "needs_you").text
    assert line.text == "Application — For your review: Looks like a project. Yes?"
    assert "Atlas application" not in line.text
    assert "title" in line.offered and line.fields_used["suggested_name"] == line.suggested_name
    assert all(line.suggested_name is None for line in lines(with_name_suggestions(morning, {record.id: ("stale", "Name")})))
    assert record.title == "Application"
    assert with_name_suggestions(morning, {}) == morning
    assert "title" not in next(line for line in lines(morning) if line.rule == "needs_you").offered


def test_candidate_pins_raw_version_even_with_inherited_reader_fields():
    parent = task("Parent", key="brn_parent", data={"called": "project", "labels": ["work"],
        "stake": {"level": "hurts_for_weeks", "confirmed": True, "proposed_by": "nic"}})
    child = task("Application", key="brn_child", data={"parent": parent.id,
        "review_marks": [{"mark": "question", "text": "Its name?"}]})
    (candidate,) = list_name_candidates(tuple(ItemRecord(r, f"{r.id}.md") for r in (parent, child)), NOW)
    assert candidate.record_sha256 == child.sha256
    morning = with_name_suggestions(build_morning(inputs((parent, child))),
                                    {child.id: (candidate.record_sha256, "Atlas application")})
    assert next(line for line in lines(morning) if line.rule == "needs_you").suggested_name == "Atlas application"
