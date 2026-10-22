"""Private move consent is host context, never a caller-controlled move flag."""
from hashlib import sha256

import pytest

from brain_core import moves, work
from brain_core.contracts import stable_hash
from test_moves import item, NOW

CALLER = "system:dashboard"


def run(record, move, message, *, tap=True, caller=CALLER, registered=True):
    records = [record] if record else []
    change = {"operation_id": "surface", "message": message, "said_by": "owner", "came_by": "direct", "moves": [move]}
    context = moves.ExecutionContext(raw_records={r.id: r for r in records}, write_index=work.write_index(records),
        planned_ids={(0, i): "brn_20260930_060000_" + sha256(str(i).encode()).hexdigest()[:16] for i in range(26)},
        operation_hash=stable_hash(change), allowed_code_callers=frozenset({CALLER}) if registered else frozenset(),
        authenticated_owner_tap=tap)
    return moves.apply_change(work.index_things(records), {**change, "_context": context}, actor=caller, now=NOW)


@pytest.mark.parametrize("tap,caller,registered", [("true", CALLER, True), (True, "hermes-wa", True),
                                                  (True, CALLER, False)])
def test_forged_tap_context_refuses(tap, caller, registered):
    record = item("Blue folder")
    result = run(record, {"move": "organise", "thing": record.id, "note": "exact note"}, "exact note",
                 tap=tap, caller=caller, registered=registered)
    assert result.refusal["code"] == "ACCESS_DENIED" and not result.records


@pytest.mark.parametrize("field,value", [("note", "exact note"), ("reopen", True), ("can_do_now", True)])
def test_private_fields_are_not_generic_move_arguments(field, value):
    record = item("Blue folder")
    result = run(record, {"move": "organise", "thing": record.id, field: value}, "exact note", tap=False)
    assert result.refusal and not result.records


@pytest.mark.parametrize("state", ["open", "parked", "dropped", "no_longer_needed"])
def test_untick_cannot_reopen_other_states(state):
    data = {"parked_until": {"event": "someday"}} if state == "parked" else {"reason": "fixture"} if state in {"dropped", "no_longer_needed"} else {}
    record = item("Blue folder", state=state, data=data)
    result = run(record, {"move": "organise", "thing": record.id, "reopen": True}, "Mark not done")
    assert result.refusal["field"] == "reopen" and not result.records


def test_can_do_now_preserves_real_wait_dates_and_needs():
    day = {"day": "2026-10-08", "read_on": "2026-09-29", "read_by": "code", "words": "8 October"}
    record = item("Blue folder", state="parked", data={"parked_until": "2026-10-10",
        "dates": {"planned": day, "deadline": day, "not_before": day}, "needs": {"from": "Supplier"}})
    result = run(record, {"move": "organise", "thing": record.id, "can_do_now": True}, "Can do now")
    assert result.refusal is None
    saved = result.records[0]
    assert saved.state == "open" and saved.data.get("parked_until") is None
    assert saved.data["dates"] == {"deadline": day, "not_before": day}
    assert saved.data["needs"] == record.data["needs"]


def test_tap_does_not_authorize_invented_details_or_titles():
    result = run(None, {"move": "new", "title": "Invented title"}, "Tap Add")
    assert result.refusal is None
    assert any(mark["mark"] == "agents_wording" for mark in result.records[0].data["review_marks"])
    result = run(None, {"move": "new", "title": "Blue folder", "details": "Invented note"}, "Blue folder")
    assert result.refusal["field"] == "details" and not result.records


@pytest.mark.parametrize("field", ["review", "reopen", "can_do_now"])
@pytest.mark.parametrize("value", [1, "true", None])
def test_surface_flags_require_actual_booleans(field, value):
    record = item("Blue folder")
    move = {"move": "new", "title": "Blue folder"} if field == "review" else {"move": "organise", "thing": record.id}
    result = run(record, {**move, field: value}, "Blue folder")
    assert result.refusal and result.refusal["field"].endswith(field) and not result.records


def test_tapped_tick_forged_quote_returns_structured_language_refusal():
    record = item("Blue folder")
    result = run(record, {"move": "tick", "thing": record.id,
                         "words": "I personally checked this imaginary result"}, "Mark done")
    assert not result.records
    assert result.refusal["code"] == "brain.record.needs_owner_words"
    assert result.refusal["field"] == "source.words"
    assert result.refusal["message"] == "source.words must quote the change's message word by word"
    assert result.refusal["details"]["field"] == "source.words"
    assert result.refusal["fix"]


@pytest.mark.parametrize("state", ["done", "dropped", "no_longer_needed"])
def test_planned_date_unpark_refuses_closed_records(state):
    record = item("Blue folder", state=state, data={"reason": "fixture",
        "check": {"locator": "fixture", "checked_by": "nic"}})
    result = run(record, {"move": "date", "thing": record.id, "kind": "planned",
                         "when": "today", "unpark": True}, "Today")
    assert result.refusal["field"] == "unpark" and not result.records


@pytest.mark.parametrize("value", [1, "true", None])
def test_unpark_flag_is_strictly_boolean(value):
    record = item("Blue folder")
    result = run(record, {"move": "date", "thing": record.id, "kind": "planned",
                         "when": "today", "unpark": value}, "Today")
    assert result.refusal["field"].endswith("unpark") and not result.records


@pytest.mark.parametrize("kind", [None, "deadline", "appointment", "not_before"])
def test_unpark_requires_explicit_planned_kind(kind):
    record = item("Blue folder")
    move = {"move": "date", "thing": record.id, "when": "today", "unpark": True}
    if kind is not None:
        move["kind"] = kind
    result = run(record, move, "Today")
    assert result.refusal["field"].endswith("unpark") and not result.records


@pytest.mark.parametrize("unpark", [None, False])
def test_planned_date_without_unpark_keeps_existing_parking(unpark):
    record = item("Blue folder", state="parked", data={"parked_until": {"event": "someday"}})
    move = {"move": "date", "thing": record.id, "kind": "planned", "when": "today"}
    if unpark is not None:
        move["unpark"] = unpark
    result = run(record, move, "Today")
    assert result.refusal is None
    assert result.records[0].state == "parked"
    assert result.records[0].data["parked_until"] == record.data["parked_until"]
