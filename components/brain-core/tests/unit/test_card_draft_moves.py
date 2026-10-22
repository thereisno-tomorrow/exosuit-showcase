"""Approved model wording is separately attributed, never forged owner words."""
from dataclasses import replace
from hashlib import sha256

import pytest

from brain_core import moves, work
from brain_core.contracts import stable_hash
from test_moves import item, NOW

CALLER = "system:review-card"
APPROVAL = {"request_id": "fixture-request", "sha256": "a" * 64, "author": "Diablo"}


def run(move, *, tap=True, question=True, state="open"):
    record = replace(item("Fixture parent", state=state,
                          data={"check": {"locator": "fixture", "checked_by": "nic"}} if state == "done" else {}),
                     body="Original note")
    change = {"operation_id": "fixture-approve", "message": "Save these steps", "said_by": "owner", "came_by": "direct",
              "moves": [{"thing": record.id, **move}]}
    answer = {"reference": "1", "question": "What are the steps?", "thing": record.id, "day": NOW.date().isoformat(),
              "open": True, "offered": [move["move"]]}
    if question:
        change["answers"] = ["1"]
    context = moves.ExecutionContext(raw_records={record.id: record}, write_index=work.write_index([record]),
        planned_ids={(0, i): "brn_20260930_060000_" + sha256(str(i).encode()).hexdigest()[:16] for i in range(26)},
        operation_hash=stable_hash(change), allowed_code_callers=frozenset({CALLER}), authenticated_owner_tap=tap,
        answer_context=(answer,) if question else ())
    return moves.apply_change(work.index_things([record]), {**change, "_context": context}, actor=CALLER, now=NOW)


def parts(**extra):
    return {"move": "parts", "parts": [{"title": "Compare two fixture options", "details": "Use the latest fixture terms"}],
            "approved_draft": APPROVAL, **extra}


def test_approved_steps_keep_distinct_model_and_owner_evidence_without_wording_marks():
    outcome = run(parts())
    assert outcome.refusal is None
    child = next(record for record in outcome.records if record.data.get("parent"))
    assert child.title == "Compare two fixture options" and child.body == "Use the latest fixture terms"
    assert child.source["words"] == "Save these steps"
    assert child.source["recorded_by"] == CALLER and not child.data.get("review_marks")
    assert child.evidence[-2].actor == "Diablo" and child.evidence[-2].content_sha256 == "a" * 64
    assert child.evidence[-2].locator == "gt:request/fixture-request"
    assert child.evidence[-1].excerpt == "Save these steps"


def test_next_step_retains_original_note_and_existing_next_action_shape():
    result = run({"move": "next_action", "text": "Compare two fixture options", "details": "Read the current fixture terms",
                  "approved_draft": APPROVAL})
    assert result.refusal is None
    record = result.records[0]
    assert record.data["next_action"] == {"text": "Compare two fixture options", "done": False}
    assert record.body == "Original note\n\nDiablo's suggestion: Compare two fixture options — Read the current fixture terms"
    assert record.evidence[-2].actor == "Diablo" and record.source["words"] == "Save these steps"
    assert not record.data.get("review_marks")


@pytest.mark.parametrize("tap,question,state", [(False, True, "open"), (True, False, "open"), (True, True, "done")])
def test_draft_cannot_bypass_tap_review_line_or_open_state(tap, question, state):
    outcome = run(parts(), tap=tap, question=question, state=state)
    assert outcome.refusal and not outcome.records


@pytest.mark.parametrize("approval", [None, {}, {**APPROVAL, "author": "Nic"}, {**APPROVAL, "sha256": "wrong"},
                                      {**APPROVAL, "request_id": "../outside"}, {**APPROVAL, "extra": True}])
def test_approval_shape_is_strict(approval):
    result = run(parts(approved_draft=approval))
    assert result.refusal and not result.records


@pytest.mark.parametrize("field,value", [("done", True), ("when", "tomorrow"), ("rough_minutes", 30),
                                       ("length_is_nics", True), ("whose", "Fixture"), ("block", "admin")])
def test_model_draft_cannot_smuggle_owner_choices(field, value):
    result = run(parts(parts=[{**parts()["parts"][0], field: value}]))
    assert result.refusal and not result.records


@pytest.mark.parametrize("title,detail", [("x" * 121, "short"), ("line\nbreak", "short"), ("short", "x" * 401),
                                        ("short", "line\nbreak"), ("short", "")])
def test_long_or_multiline_drafts_are_refused(title, detail):
    result = run(parts(parts=[{"title": title, "details": detail}]))
    assert result.refusal and not result.records


def test_generic_next_action_cannot_claim_approved_details():
    result = run({"move": "next_action", "text": "Fixture", "details": "Invented"})
    assert result.refusal and not result.records
