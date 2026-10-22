"""Authenticated dashboard changes over the real door and an offline fake GitHub."""
from dataclasses import asdict, replace
import hashlib

import pytest

from brain_core.contracts import canonical_json
from brain_core.work import index_things
from gremlin_town.mcp.brain_runtime import BrainRuntimeError, WorkRequest
from gremlin_town.mcp.surface_door import bind_owner_tap
from test_work_door import WORK, call, records, request, setup_door

CALLER = "system:dashboard"


def tap(provider, change):
    with bind_owner_tap(CALLER, WORK.owner_id, change):
        return call(provider, change, CALLER)


def latest(fake, first):
    return index_things(records(fake))[first]


def proof(record):
    return {record.supersedes[0] if record.supersedes else record.id:
            {"record_id": record.id, "sha256": record.sha256}}


def change(operation, message, move, record=None):
    return WorkRequest(operation, message, "owner", "direct", (move,),
                       expected_records=proof(record) if record else None)


@pytest.fixture
def door(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    provider.code_callers = frozenset({CALLER})
    yield provider, fake, log
    log.close()


def test_note_preserves_body_and_undo_replays_original_proof(door):
    provider, fake, _ = door
    saved = tap(provider, replace(request(), message="Blue folder Original note",
        moves=({"move": "new", "title": "Blue folder", "details": "Original note"},)))
    first = saved["things"][0]["id"]
    before = latest(fake, first)
    note = "  Bring the signed copy.\nThen file it.  "
    add = change("append-note", "Add note\n" + note,
        {"move": "organise", "thing": first, "note": note}, before)
    tap(provider, add)
    after = latest(fake, first)
    assert after.body == "Original note\n" + note
    assert after.source["words"] == add.message
    undo = change("undo-note", "Undo", {"move": "undo", "change": add.operation_id}, after)
    assert tap(provider, undo)["status"] == "accepted"
    assert latest(fake, first).body == before.body
    head = fake.branch
    assert tap(provider, add)["status"] == "duplicate"
    assert tap(provider, undo)["status"] == "duplicate"
    assert fake.branch == head


def test_tick_without_invented_quote_then_reopen(door):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    tick = change("tick", "Mark done", {"move": "tick", "thing": first}, latest(fake, first))
    tap(provider, tick)
    done = latest(fake, first)
    assert done.state == "done" and done.data["dates"]["done"]["words"] == "Mark done"
    tap(provider, change("untick", "Mark not done", {"move": "organise", "thing": first, "reopen": True}, done))
    reopened = latest(fake, first)
    assert reopened.state == "open" and "done" not in reopened.data.get("dates", {})
    assert reopened.data.get("check") is None


def test_can_do_now_only_removes_park_and_planned(door):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    other = tap(provider, replace(request("other"), message="Collect blue folder",
        moves=({"move": "new", "title": "Collect blue folder"},)))["things"][0]["id"]
    fields = ({"move": "date", "thing": first, "kind": "deadline", "when": "Friday"},
              {"move": "date", "thing": first, "kind": "planned", "when": "Thursday"},
              {"move": "wait", "thing": first, "kind": "after", "after": [other]},
              {"move": "park", "thing": first})
    tap(provider, WorkRequest("park-with-truth", "Friday Thursday Someday", "owner", "direct", fields))
    parked = latest(fake, first)
    assert parked.state == "parked" and "planned" in parked.data["dates"]
    tap(provider, change("can-do-now", "Can do now",
        {"move": "organise", "thing": first, "can_do_now": True}, parked))
    result = latest(fake, first)
    assert result.state == "open" and result.data.get("parked_until") is None
    assert result.data["dates"]["deadline"] == parked.data["dates"]["deadline"]
    assert result.data["after"] == [other] and "planned" not in result.data["dates"]


def test_quick_add_is_accepted_and_for_review_and_parts_use_selected_parent(door):
    provider, fake, _ = door
    added = tap(provider, change("quick-add", "Inspect blue folder",
        {"move": "new", "title": "Inspect blue folder", "review": True}))
    first = added["things"][0]["id"]
    record = latest(fake, first)
    assert record.data["level"] == "accepted"
    assert {"mark": "question", "text": "Review this new thing"} in record.data["review_marks"]
    tap(provider, change("parts", "Add steps\nCall supplier\nFile copy",
        {"move": "parts", "thing": first, "parts": [{"title": "Call supplier"}, {"title": "File copy", "then": True}]}, record))
    children = [r for r in index_things(records(fake)).values() if r.data.get("parent") == first]
    assert len(children) == 2
    assert all(not r.data.get("review_marks") for r in children)


@pytest.mark.parametrize("field,value", [("reopen", "true"), ("can_do_now", 1), ("note", "not typed")])
def test_private_fields_are_typed_and_note_is_exact(door, field, value):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    head = fake.branch
    bad = change("bad-field", "Add note: typed not", {"move": "organise", "thing": first, field: value})
    with pytest.raises(BrainRuntimeError):
        tap(provider, bad)
    assert fake.branch == head


def test_generic_caller_cannot_send_private_fields_or_proofs(door):
    provider, fake, _ = door
    first = call(provider, request())["things"][0]["id"]
    record = latest(fake, first)
    head = fake.branch
    for move in ({"move": "organise", "thing": first, "note": "typed note"},
                 {"move": "new", "title": "typed note", "review": True}):
        with pytest.raises(BrainRuntimeError):
            call(provider, change("private", "typed note", move))
    with pytest.raises(BrainRuntimeError) as refused:
        call(provider, change("proof", "typed note", {"move": "organise", "thing": first, "label": "admin"}, record))
    assert refused.value.code == "ACCESS_DENIED" and fake.branch == head


def test_stale_or_substituted_proof_cannot_publish(door):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    old = latest(fake, first)
    tap(provider, change("note-a", "A", {"move": "organise", "thing": first, "note": "A"}, old))
    stale = change("stale", "B", {"move": "organise", "thing": first, "note": "B"}, old)
    head = fake.branch
    with pytest.raises(BrainRuntimeError) as refused:
        tap(provider, stale)
    assert refused.value.code == "brain.record.changed"
    fresh = replace(stale, expected_records=proof(latest(fake, first)))
    with bind_owner_tap(CALLER, WORK.owner_id, stale), pytest.raises(BrainRuntimeError) as refused:
        call(provider, fresh, CALLER)
    assert refused.value.code == "ACCESS_DENIED" and fake.branch == head


def test_absent_proof_retains_legacy_hash_and_replay(door):
    provider, fake, _ = door
    legacy = request()
    public = {key: value for key, value in asdict(legacy).items() if key not in {"check_only", "expected_records"}}
    expected = hashlib.sha256(canonical_json(public).encode()).hexdigest()
    call(provider, legacy)
    assert records(fake)[0].dedupe_key == expected
    head = fake.branch
    assert call(provider, legacy)["status"] == "duplicate" and fake.branch == head


def test_proof_is_rechecked_after_publication_conflict(door, monkeypatch):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    original = latest(fake, first)
    stale = change("race", "A", {"move": "organise", "thing": first, "note": "A"}, original)
    racing = change("winner", "B", {"move": "organise", "thing": first, "note": "B"}, original)
    publish = provider._storage.publish_records
    raced = False
    def publish_after_race(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            # Real second door publication wins at the pinned base before the
            # first publisher attempts its fast-forward update.
            with bind_owner_tap(CALLER, WORK.owner_id, racing):
                provider._work(WORK, racing, caller=CALLER)
        return publish(**kwargs)
    monkeypatch.setattr(provider._storage, "publish_records", publish_after_race)
    with pytest.raises(BrainRuntimeError) as refused:
        tap(provider, stale)
    assert refused.value.code == "brain.record.changed"
    assert latest(fake, first).body == "B"


@pytest.mark.parametrize("proof_value", [[], {"id": "x"}, {"record_id": "x", "sha256": 1},
                                       {"record_id": "x", "sha256": "A" * 64}])
def test_malformed_record_proof_fails_without_publication(door, proof_value):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    head = fake.branch
    bad = replace(change("bad-proof", "A", {"move": "organise", "thing": first, "note": "A"}),
                  expected_records={first: proof_value})
    with pytest.raises(BrainRuntimeError) as refused:
        tap(provider, bad)
    assert refused.value.field == "expected_records" and fake.branch == head


def test_proof_is_part_of_exact_retry_identity(door):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    add = change("append", "A", {"move": "organise", "thing": first, "note": "A"}, latest(fake, first))
    tap(provider, add)
    head = fake.branch
    with pytest.raises(BrainRuntimeError) as refused:
        tap(provider, replace(add, expected_records=proof(latest(fake, first))))
    assert refused.value.code == "brain.conflict" and fake.branch == head


def test_tap_keeps_review_answer_offered_target_checks(door):
    from gremlin_town.review.why_log import Surfacing
    from test_work_door import NOW
    provider, fake, log = door
    first = tap(provider, request())["things"][0]["id"]
    log.record_surfacings([Surfacing(NOW, "review", first, "a" * 64, "stalled", {},
        question="Park it?", offered=("park",))])
    reference = str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
    head = fake.branch
    bad = replace(change("not-offered", "Mark done", {"move": "tick", "thing": first}), answers=(reference,))
    with pytest.raises(BrainRuntimeError) as refused:
        tap(provider, bad)
    assert refused.value.field == "answers" and fake.branch == head


def test_forged_tick_quote_is_normalized_and_never_published(door):
    provider, fake, _ = door
    first = tap(provider, request())["things"][0]["id"]
    head = fake.branch
    bad = change("forged-tick", "Mark done", {"move": "tick", "thing": first,
        "words": "I personally checked this imaginary result"}, latest(fake, first))
    with pytest.raises(BrainRuntimeError) as refused:
        tap(provider, bad)
    assert refused.value.code == "brain.record.needs_owner_words"
    assert refused.value.field == "source.words"
    assert "quote the change's message word by word" in refused.value.message
    assert fake.branch == head and latest(fake, first).state == "open"


def test_today_unparks_in_one_version_and_undo_restores_prior_plan(door):
    provider, fake, _ = door
    first = tap(provider, replace(request(), message="Pay blue bill to Supplier",
        moves=({"move": "new", "title": "Pay blue bill", "owed_to": "Supplier"},)))["things"][0]["id"]
    prerequisite = tap(provider, replace(request("prerequisite"), message="Check invoice",
        moves=({"move": "new", "title": "Check invoice"},)))["things"][0]["id"]
    tap(provider, WorkRequest("park-planned", "Friday Thursday Someday", "owner", "direct", (
        {"move": "date", "thing": first, "kind": "deadline", "when": "Friday"},
        {"move": "date", "thing": first, "kind": "planned", "when": "Thursday"},
        {"move": "wait", "thing": first, "kind": "after", "after": [prerequisite]},
        {"move": "park", "thing": first})))
    parked = latest(fake, first)
    assert parked.state == "parked"
    count = len(records(fake))
    today = change("today", "Today", {"move": "date", "thing": first, "when": "today",
        "kind": "planned", "unpark": True}, parked)
    result = tap(provider, today)
    saved = latest(fake, first)
    assert result["status"] == "accepted" and len(result["things"]) == 1
    assert len(records(fake)) == count + 1
    assert saved.state == "open" and saved.data.get("parked_until") is None
    assert saved.data["dates"]["planned"]["day"] == "2026-09-30"
    assert saved.data["dates"]["deadline"] == parked.data["dates"]["deadline"]
    assert saved.data["after"] == [prerequisite] and saved.data["owed_to"] == "Supplier"
    undo = change("undo-today", "Undo", {"move": "undo", "change": "today"}, saved)
    tap(provider, undo)
    restored = latest(fake, first)
    assert restored.state == "parked" and restored.data == parked.data
    head = fake.branch
    assert tap(provider, today)["status"] == "duplicate"
    assert tap(provider, undo)["status"] == "duplicate" and fake.branch == head


def test_unpark_is_not_a_generic_date_power(door):
    provider, fake, _ = door
    first = call(provider, request())["things"][0]["id"]
    head = fake.branch
    with pytest.raises(BrainRuntimeError):
        call(provider, WorkRequest("generic-unpark", "Today", "owner", "direct", (
            {"move": "date", "thing": first, "when": "today", "unpark": True},)))
    assert fake.branch == head
