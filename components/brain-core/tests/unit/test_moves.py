"""W6 source-message cases and the work-model plan's move checks."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from brain_core import work
from brain_core.moves import Change, ExecutionContext, ValidatedAnswer, apply_change
from brain_core.contracts import stable_hash
from brain_core.records import Record, WORK_FIELDS, record_from_mapping

NOW = datetime(2026, 9, 30, 6, tzinfo=UTC)
ACTOR = "hermes-wa"


@pytest.mark.parametrize("move,field", [
    ({"move": "park", "until": None}, "until"),
    ({"move": "park", "until": ""}, "until"),
    ({"move": "wait", "kind": "needs", "who": "Sam", "expected": ""}, "expected"),
    ({"move": "wait", "kind": "not_before"}, "until"),
    ({"move": "wait", "kind": "not_before", "event": " "}, "event"),
    ({"move": "stake", "at_stake": ""}, "at_stake"),
    ({"move": "stake", "odds": ""}, "odds"),
    ({"move": "new", "title": "Passport", "stake": {"at_stake": ""}}, "stake.at_stake"),
    ({"move": "tick", "when": ""}, "when"),
    ({"move": "date", "when": ""}, "when"),
    ({"move": "next_action", "text": ""}, "text"),
    ({"move": "undo", "change": ""}, "change"),
    ({"move": "organise", "under": ""}, "under"),
    ({"move": "organise", "merge_into": ""}, "merge_into"),
    ({"move": "wait", "kind": "after", "after": [""]}, "after"),
    ({"move": "parts", "parts": [{"title": "Passport", "when": ""}]}, "parts.when"),
    ({"move": "parts", "parts": [{"title": "Passport", "rough_minutes": 0}]}, "parts.rough_minutes"),
])
def test_move_text_error_names_its_input_field(move, field):
    parent = item("Passport")
    change = {**move, "words": "Park Passport with Sam"}
    if move["move"] not in {"new", "undo"}:
        change["thing"] = parent.id
    out = run([parent], "Park Passport with Sam", [change])
    assert out.refusal is not None and out.refusal["field"] == field
    assert field in out.refusal["message"] and out.refusal["fix"]


def item(title, *, number=1, data=None, state="open", source=None, links=()):
    words = title
    values = {"level": "accepted", "size": "task", "timing": {}}
    values.update(data or {})
    return record_from_mapping({"schema": "brain.record/v2",
        "id": f"brn_20260929_060000_{number:016x}", "kind": "item", "title": title, "state": state,
        "created_at": "2026-09-29T06:00:00+00:00", "updated_at": "2026-09-29T06:00:00+00:00",
        "sensitivity": "private", "vaults": [], "owners": [], "entities": [], "supersedes": [],
        "evidence": [{"source_type": "session_message", "locator": "fixture", "excerpt": words,
                      "content_sha256": sha256(words.encode()).hexdigest()}],
        "source": source or {"said_by": "owner", "came_by": "direct", "words": words, "recorded_by": ACTOR},
        "data": values, "links": list(links)})


def run(items, message, moves, *, said_by="owner", came_by="direct", speaker=None, now=NOW,
        actor=ACTOR, operation="test-change", answers=(), undo_changes=None, allowed_code_callers=frozenset(),
        raw_records=None, planned_ids=None, record_envelope=None, capture_bodies=None, operation_hash=None,
        capture_raise_with=None):
    change = Change(operation_id=operation, message=message, said_by=said_by, came_by=came_by, moves=moves)
    if speaker:
        change["speaker"] = speaker
    if answers:
        change["answers"] = [answer["reference"] for answer in answers]
    ids = planned_ids if planned_ids is not None else {(i, j): "brn_" + now.strftime("%Y%m%d_%H%M%S") + "_" +
        sha256(f"{actor}:{operation}:{i}:{j}".encode()).hexdigest()[:16]
        for i in range(len(moves)) for j in range(25)}
    change["_context"] = ExecutionContext(
        raw_records=raw_records if raw_records is not None else {r.id: r for r in items},
        write_index=work.write_index(items), planned_ids=ids, answer_context=answers,
        undo_changes=undo_changes or {}, allowed_code_callers=allowed_code_callers,
        operation_hash=stable_hash(change) if operation_hash is None else operation_hash,
        record_envelope=record_envelope, capture_bodies={} if capture_bodies is None else capture_bodies,
        capture_raise_with={} if capture_raise_with is None else capture_raise_with)
    return apply_change(work.index_things(items), change, actor=actor, now=now)


def test_capture_raise_with_is_canonical_before_body_envelope_hash_and_replay():
    from brain_core.records import parse_record, render_record
    body = "  Original budget notes\nUnicode: résumé\n\n"
    raise_with = "Sam"
    envelope = {"vaults": ["brain-home"], "owners": ["nic"], "evidence_actor": ACTOR}
    public_hash = stable_hash({"title": "silver budget", "body": body, "raise_with": raise_with})
    moves = [{"move": "new", "title": "silver budget"}]
    options = dict(capture_bodies={0: body}, capture_raise_with={0: raise_with},
                   record_envelope=envelope, operation_hash=public_hash)
    first = run([], "raise silver budget with Sam", moves, **options)
    assert first.refusal is None
    record = first.records[0]
    assert record.data["raise_with"] == raise_with and record.body == body
    assert record.vaults == ("brain-home",) and record.owners == ("nic",)
    assert all(e.actor == ACTOR for e in record.evidence)
    assert record.dedupe_key == public_hash
    parsed = parse_record(render_record(record), "capture.md")
    assert parsed.data == record.data and parsed.vaults == record.vaults and parsed.owners == record.owners
    assert parsed.source == record.source and parsed.evidence == record.evidence
    replay = run(first.records, "raise silver budget with Sam", moves, **options)
    assert replay.refusal is None and replay.reply["status"] == "duplicate"
    assert render_record(replay.records[0]) == render_record(record)
    invalid_replay = run(first.records, "raise silver budget with Sam", moves,
                         **{**options, "capture_raise_with": {False: raise_with}})
    assert invalid_replay.refusal["field"] == "_context.capture_raise_with" and not invalid_replay.records
    for changed_hash in (public_hash, stable_hash({"title": "silver budget", "body": body, "raise_with": "Jo"})):
        changed = run(first.records, "raise silver budget with Sam", moves,
                      **{**options, "capture_raise_with": {0: "Jo"}, "operation_hash": changed_hash})
        assert changed.refusal["code"] == "brain.conflict" and not changed.records


@pytest.mark.parametrize("raise_with", [{-1: "Sam"}, {2: "Sam"}, {True: "Sam"}, {False: "Sam"},
    {"0": "Sam"}, {1: "Sam"}, {0: None}, {0: 1}, {0: ""}, {0: "   "}, ["Sam"]])
def test_capture_raise_with_context_requires_nonempty_text_and_exact_new_ordinal(raise_with):
    parent = item("Passport")
    out = run([parent], "silver budget; Passport started", [{"move": "new", "title": "silver budget"},
              {"move": "start", "thing": parent.id}], capture_raise_with=raise_with)
    assert out.refusal["field"] == "_context.capture_raise_with" and out.refusal["fix"]
    assert not out.records


def test_capture_raise_with_only_named_new_preserves_routine_bytes_and_undo():
    parent = item("Passport", data={"raise_with": "Jo"})
    moves = [{"move": "start", "thing": parent.id}, {"move": "new", "title": "silver budget", "repeat": "daily"},
             {"move": "tick", "thing": "#1"}, {"move": "new", "title": "dentist"}]
    first = run([parent], "Passport started; silver budget; dentist", moves, operation="raise-with-change",
                capture_raise_with={1: "Sam"})
    assert first.refusal is None
    records = {record.title: record for record in first.records}
    assert records["Passport"].data["raise_with"] == "Jo"
    routine = records["silver budget"]
    assert routine.data["raise_with"] == "Sam" and not WORK_FIELDS.intersection(routine.data)
    assert "raise_with" not in records["dentist"].data
    replay = run([parent, *first.records], "Passport started; silver budget; dentist", moves,
                 operation="raise-with-change", capture_raise_with={1: "Sam"})
    assert replay.refusal is None and replay.reply["status"] == "duplicate"
    assert [r.sha256 for r in replay.records] == [r.sha256 for r in first.records]
    undo = run([parent, *first.records], "undo Passport start", [
        {"move": "undo", "change": "raise-with-change", "words": "undo Passport start"}],
        operation="undo-raise-with", undo_changes={"raise-with-change": [r.id for r in first.records]})
    assert undo.refusal is None
    restored = {record.title: record for record in undo.records}
    assert restored["Passport"].data == parent.data
    assert restored["silver budget"].data["raise_with"] == "Sam"
    assert not WORK_FIELDS.intersection(restored["silver budget"].data)


def test_capture_raise_with_empty_default_and_unknown_public_field_refused():
    ordinary = run([], "silver budget", [{"move": "new", "title": "silver budget"}])
    explicit = run([], "silver budget", [{"move": "new", "title": "silver budget"}], capture_raise_with={})
    assert ordinary.refusal is None and ordinary.records == explicit.records
    assert "raise_with" not in ordinary.records[0].data
    public = run([], "silver budget", [{"move": "new", "title": "silver budget", "raise_with": "Sam"}])
    assert public.refusal["field"] == "moves.0" and not public.records


def test_wifi_bill_clear_capture():
    out = run([], "need to pay venue invoice", [{"move": "new", "title": "pay venue invoice"}])
    assert out.refusal is None
    assert out.records[0].data["level"] == "accepted"
    assert not out.flags


def test_capture_body_is_exact_before_envelope_hash_and_strict_replay():
    body = "  Original notes\nUnicode: résumé\n\n"
    envelope = {"vaults": ["brain-home"], "owners": ["nic"], "evidence_actor": ACTOR}
    public_hash = stable_hash({"title": "venue invoice", "body": body})
    moves = [{"move": "new", "title": "venue invoice"}]
    first = run([], "need to pay venue invoice", moves, capture_bodies={0: body},
                operation_hash=public_hash, record_envelope=envelope)
    assert first.refusal is None and first.records[0].body == body
    assert first.records[0].dedupe_key == public_hash
    replay = run(first.records, "need to pay venue invoice", moves, capture_bodies={0: body},
                 operation_hash=public_hash, record_envelope=envelope)
    assert replay.refusal is None and replay.reply["status"] == "duplicate"
    assert replay.records[0].sha256 == first.records[0].sha256
    for changed_hash in (public_hash, stable_hash({"title": "venue invoice", "body": "changed"})):
        changed = run(first.records, "need to pay venue invoice", moves, capture_bodies={0: "changed"},
                      operation_hash=changed_hash, record_envelope=envelope)
        assert changed.refusal["code"] == "brain.conflict" and not changed.records


def test_capture_body_reaches_only_corresponding_new_and_survives_coalesced_moves():
    from dataclasses import replace
    parent = replace(item("Passport"), body="prior body")
    moves = [{"move": "start", "thing": parent.id}, {"move": "new", "title": "venue invoice"},
             {"move": "parts", "thing": "#1", "parts": [{"title": "open venue invoice"}]},
             {"move": "new", "title": "dentist"}, {"move": "start", "thing": "#1"}]
    out = run([parent], "Passport started; venue invoice started; open venue invoice; dentist", moves,
              capture_bodies={1: "wifi body"})
    assert out.refusal is None
    records = {record.title: record for record in out.records}
    assert records["Passport"].body == "prior body"
    assert records["venue invoice"].body == "wifi body"
    assert records["open venue invoice"].body == records["dentist"].body == ""


def test_prior_body_is_preserved_by_date_and_restored_by_undo():
    from dataclasses import replace
    parent = replace(item("Passport"), body="original body")
    first = run([parent], "Passport by end of october", [
        {"move": "date", "thing": parent.id, "when": "by end of october"}], operation="date-change")
    assert first.refusal is None and first.records[0].body == parent.body
    # An exact current fixture with a different body proves undo restores the
    # archived predecessor, rather than reusing the current body's text.
    current = replace(first.records[0], body="current body")
    out = run([parent, current], "undo Passport date", [
        {"move": "undo", "change": "date-change", "words": "undo Passport date"}],
        operation="undo-body", undo_changes={"date-change": [current.id]})
    assert out.refusal is None and out.records[0].body == parent.body


@pytest.mark.parametrize("bodies", [{-1: "body"}, {2: "body"}, {True: "body"}, {"0": "body"},
                                     {1: "body"}, {0: None}, {0: 1}, ["body"]])
def test_capture_body_context_requires_text_and_exact_new_ordinal(bodies):
    parent = item("Passport")
    out = run([parent], "venue invoice; Passport started", [{"move": "new", "title": "venue invoice"},
              {"move": "start", "thing": parent.id}], capture_bodies=bodies)
    assert out.refusal["field"] == "_context.capture_bodies" and out.refusal["fix"]
    assert not out.records


def test_capture_body_defaults_empty_and_is_never_a_public_move_field_or_owner_quote():
    ordinary = run([], "venue invoice", [{"move": "new", "title": "venue invoice"}])
    assert ordinary.refusal is None and ordinary.records[0].body == ""
    public = run([], "venue invoice", [{"move": "new", "title": "venue invoice", "body": "forbidden"}])
    assert public.refusal and not public.records
    quoted = run([], "save it", [{"move": "new", "title": "venue invoice"}], capture_bodies={0: "I need to pay venue invoice"})
    assert quoted.refusal["code"] == "brain.invalid_argument" and not quoted.records


def test_authenticated_envelope_replay_is_exact_and_preserves_evidence_authors():
    from dataclasses import replace
    parent = item("Passport")
    parent = replace(parent, evidence=(replace(parent.evidence[0], actor="original-writer"),))
    envelope = {"vaults": ["brain-home"], "owners": ["nic"], "evidence_actor": ACTOR}
    moves = [{"move": "start", "thing": parent.id}]
    first = run([parent], "Passport started", moves, record_envelope=envelope)
    assert first.refusal is None
    saved = first.records[0]
    assert saved.vaults == ("brain-home",) and saved.owners == ("nic",)
    assert [e.actor for e in saved.evidence] == ["original-writer", ACTOR]
    replay = run([parent, saved], "Passport started", moves, record_envelope=envelope)
    assert replay.refusal is None and replay.reply["status"] == "duplicate"
    assert replay.records[0].sha256 == saved.sha256
    changed = run([parent, saved], "Passport started", moves,
                  record_envelope={**envelope, "owners": ["someone-else"]})
    assert changed.refusal["code"] == "brain.conflict"
    forged = run([parent, saved], "Passport started", moves,
                 record_envelope={**envelope, "evidence_actor": "someone-else"})
    assert forged.refusal and not forged.records
    tampered = replace(saved, evidence=(*saved.evidence[:-1], replace(saved.evidence[-1], actor="forged-author")))
    conflict = run([parent, tampered], "Passport started", moves, record_envelope=envelope)
    assert conflict.refusal["code"] == "brain.conflict" and not conflict.records


def test_new_capture_envelope_replay_and_missing_prior_evidence_actor():
    envelope = {"vaults": ["brain-home"], "owners": ["nic"], "evidence_actor": ACTOR}
    moves = [{"move": "new", "title": "venue invoice"}]
    first = run([], "need to pay venue invoice", moves, record_envelope=envelope)
    replay = run(first.records, "need to pay venue invoice", moves, record_envelope=envelope)
    assert replay.refusal is None and replay.records[0].sha256 == first.records[0].sha256
    parent = item("Passport")
    first = run([parent], "Passport started", [{"move": "start", "thing": parent.id}], record_envelope=envelope)
    assert all(e.actor == ACTOR for e in first.records[0].evidence)


@pytest.mark.parametrize("state", ["done", "parked", "no_longer_needed", "dropped"])
def test_dropped_question_carries_closed_state_with_authenticated_envelope(state):
    carried = {"dropped": {"reason": "old reason"}, "done": {"check": {"locator": "fixture", "checked_by": "nic"}},
               "parked": {"parked_until": "2026-10-02"}}
    parent = item("Passport", state=state, data=carried.get(state, {}))
    envelope = {"vaults": ["brain-home"], "owners": ["nic"], "evidence_actor": ACTOR}
    out = run([parent], "save it", [{"move": "tick", "thing": parent.id}], record_envelope=envelope)
    assert out.refusal is None and out.records[0].state == state
    assert out.records[0].source["words"] == "save it"


def test_save_only_stake_confirm_preserves_raw_quote_and_saves_only_proposal():
    parent = item("Venue rental")
    out = run([parent], "save it", [{"move": "stake", "thing": parent.id,
              "level": "changes_life", "at_stake": "40 display slots a month", "confirm": True}])
    assert out.refusal is None
    assert out.records[0].data["stake"]["confirmed"] is False
    assert out.records[0].source["words"] == "save it"
    assert out.records[0].data["review_marks"][-1]["mark"] == "question"


def test_multiple_save_only_drops_coalesce_questions_without_authorising_moves():
    parent = item("Passport")
    out = run([parent], "save it", [{"move": "tick", "thing": parent.id},
                                    {"move": "organise", "thing": parent.id, "title": "forged title"}])
    assert out.refusal is None and len(out.records) == 1
    assert out.records[0].title == parent.title and out.records[0].state == parent.state
    assert len(out.records[0].data["review_marks"]) == 2
    assert len(out.flags) == 2 and all(flag["dropped"] for flag in out.flags)
    assert "Passport: marking done was not saved" in out.reply["say"]
    assert "Passport: renaming was not saved" in out.reply["say"]
    assert out.reply["say"].count("quote what you said or answer the review line") == 2


def test_dropped_changes_are_said_even_when_their_things_are_outside_the_reply_preview():
    parents = [item(f"Passport {n}", number=n) for n in range(1, 8)]
    out = run(parents, "save it", [{"move": "tick", "thing": parent.id} for parent in parents])
    assert out.refusal is None and len(out.flags) == 7
    for parent in parents:
        assert parent.state == "open"
        assert f"{parent.title}: marking done was not saved: quote what you said or answer the review line." in out.reply["say"]


@pytest.mark.parametrize("message,title,marked", [
    ("need to resaerch", "research", False),
    ("need to rest", "test", True),
    ("need to resxerxh", "research", True),
    ("need to researc", "research", False),
    ("need to researchh", "research", False),
])
def test_typo_rule_only_one_key_and_five_letters(message, title, marked):
    out = run([], message, [{"move": "new", "title": title}])
    assert bool(out.records[0].data.get("review_marks")) == marked


def test_exhibition_one_change_four_records_without_deadline():
    parent = item("exhibition permit")
    message = ("I started exhibition permit already; I basically texted my coordinator; get a safety checklist "
               "from the inspector on my venue inspection on 8 October; then officially submit my permit on Exhibit Portal")
    out = run([parent], message, [{"move": "start", "thing": parent.id},
        {"move": "parts", "thing": parent.id, "parts": [
            {"title": "texted my coordinator", "done": True},
            {"title": "get a safety checklist from the inspector", "when": "8 October", "event": "venue inspection"},
            {"title": "officially submit my permit on Exhibit Portal", "then": True}]}])
    assert out.refusal is None
    assert len(out.records) == 4
    assert out.records[1].state == "done"
    assert out.records[2].data["dates"]["not_before"]["day"] == "2026-10-08"
    assert out.records[3].data["after"] == [out.records[2].id]
    assert all("deadline" not in r.data.get("dates", {}) for r in out.records)


def test_edit_resume_is_a_subtask():
    parent = item("send my brief to Morgan")
    out = run([parent], "I need to edit my brief before I send my brief to Morgan",
              [{"move": "parts", "thing": parent.id, "parts": [{"title": "edit my brief"}]}])
    index = work.index_things([parent, *out.records])
    assert work.kind_of(out.records[0].id, index) == "subtask"
    assert work.next_of(parent.id, index, NOW)["item_ref"] == out.records[0].id


@pytest.mark.parametrize("title,message,move,state", [
    ("Board for Workshop", "Board for Workshop done", "tick", "done"),
    ("Sketch board", "Sketch board cancelled", "end", "no_longer_needed"),
])
def test_endings_keep_owner_quote(title, message, move, state):
    parent = item(title)
    fields = {"move": move, "thing": parent.id, "words": message}
    if move == "end":
        fields["ending"] = "no_longer_needed"
    out = run([parent], message, [fields])
    assert out.records[0].state == state
    assert out.records[0].source["words"] == message
    assert out.records[0].evidence[-1].excerpt == message


@pytest.mark.parametrize("move", [
    {"move": "tick"}, {"move": "end", "ending": "no_longer_needed"},
    {"move": "park", "until": "Monday"}, {"move": "organise", "called": "project"},
    {"move": "organise", "accept": True}, {"move": "organise", "keep": "date"},
    {"move": "organise", "title": "Renew passport"},
    {"move": "organise", "merge_into": "Other"},
    {"move": "organise", "rough_minutes": 15, "length_is_nics": True},
    {"move": "organise", "block": "admin"},
])
@pytest.mark.parametrize("words", [None, "yes", "yes save it", "drop it"])
def test_owner_only_missing_or_bare_quote_leaves_question(move, words):
    parent = item("Passport")
    fields = {**move, "thing": parent.id}
    if words:
        fields["words"] = words
    out = run([parent], words or "Passport", [fields])
    assert out.refusal is None
    assert out.flags[0]["dropped"]
    assert out.records[0].state == "open"
    assert out.records[0].data["review_marks"][-1]["mark"] == "question"


def test_stake_bad_confirm_keeps_proposal():
    parent = item("Venue rental")
    out = run([parent], "yes", [{"move": "stake", "thing": parent.id, "confirm": True,
        "level": "changes_life", "at_stake": "40 display slots a month"}])
    assert out.records[0].data["stake"]["confirmed"] is False
    assert out.records[0].data["stake"]["at_stake"] == "40 display slots a month"
    assert out.records[0].data["review_marks"][-1]["mark"] == "question"


def test_full_question_cap_replaces_oldest_question():
    marks = [{"mark": "agents_wording", "text": "keep me"},
             {"mark": "question", "text": "oldest"},
             *[{"mark": "question", "text": str(n)} for n in range(8)]]
    parent = item("Passport", data={"review_marks": marks})
    out = run([parent], "yes", [{"move": "tick", "thing": parent.id}])
    result = out.records[0].data["review_marks"]
    assert len(result) == 10
    assert result[0] == marks[0]
    assert not any(mark.get("text") == "oldest" for mark in result)


def test_unknown_and_ambiguous_titles_refuse_with_fix():
    items = [item("Passport", number=1), item("Passport", number=2)]
    for title, candidates in [("Passport", 2), ("unknown", 0)]:
        out = run(items, "start Passport", [{"move": "start", "thing": title}])
        assert not out.records
        assert out.refusal["fix"]
        assert len(out.refusal.get("which_one", [])) == candidates
        assert title in out.refusal["message"]


def test_parts_on_a_subtask_refuse():
    parent = item("Send")
    part = item("Edit", number=2, data={"parent": parent.id})
    out = run([parent, part], "Edit spelling", [{"move": "parts", "thing": part.id, "parts": [{"title": "spelling"}]}])
    assert out.refusal["fix"]


def test_after_loop_refuses_every_record():
    first, second = item("First"), item("Second", number=2)
    out = run([first, second], "First after Second", [
        {"move": "wait", "thing": first.id, "kind": "after", "after": [second.id]},
        {"move": "wait", "thing": second.id, "kind": "after", "after": [first.id]}])
    assert not out.records
    assert out.refusal["field"] == "after"
    assert out.refusal["fix"]


@pytest.mark.parametrize("said_by,came_by", [("other", "direct"), ("owner", "connected_source")])
def test_others_words_only_suggestions(said_by, came_by):
    parent = item("Portfolio")
    out = run([parent], "Morgan says Portfolio by Thursday", [
        {"move": "new", "title": "Portfolio", "when": "by Thursday"},
        {"move": "tick", "thing": parent.id, "words": "Portfolio"}],
        said_by=said_by, came_by=came_by, speaker="Morgan")
    assert out.records[0].data["level"] == "suggested"
    assert "deadline" not in out.records[0].data["dates"]
    assert out.records[0].data["dates"]["suggested"]["speaker"] == "Morgan"
    assert out.records[1].state == "open"
    assert out.flags[-1]["dropped"]


def test_missing_date_words_drop_with_question():
    parent = item("Passport")
    out = run([parent], "Passport", [{"move": "date", "thing": parent.id, "when": "Friday"}])
    assert "dates" not in out.records[0].data
    assert out.records[0].data["review_marks"][-1]["mark"] == "question"


def test_part_or_parent_not_named_marks_part():
    parent = item("Send resume to Morgan")
    out = run([parent], "Edit brief", [{"move": "parts", "thing": parent.id, "parts": [{"title": "Edit brief"}]}])
    assert out.records[0].data["review_marks"][0]["mark"] == "agents_wording"


def test_routine_capture_and_tick_have_no_work_fields_and_stay_open():
    out = run([], "weekly weigh myself", [{"move": "new", "title": "weigh myself", "repeat": "weekly:wed"},
        {"move": "tick", "thing": "#1"}])
    assert len(out.records) == 1
    assert all(not WORK_FIELDS.intersection(r.data) for r in out.records)
    assert out.records[0].state == "open"
    assert out.records[0].data["occurrence"] == {"day": "2026-09-30", "outcome": "done"}


@pytest.mark.parametrize("repeat", ["W0:f004", "fortnightly", "weekly:someday", "daily "])
@pytest.mark.parametrize("attempt", [
    {"move": "start"}, {"move": "tick"}, {"move": "tick", "words": "Finish routine"},
    {"move": "parts", "parts": [{"title": "first part"}]},
    {"move": "next_action", "text": "first step"},
    {"move": "wait", "kind": "needs", "who": "Morgan"},
    {"move": "date", "when": "Friday"},
    {"move": "end", "words": "Finish routine"},
    {"move": "park", "until": "Friday", "words": "Finish routine"},
    {"move": "organise", "label": "admin"},
    {"move": "organise", "title": "renamed", "words": "Finish routine"},
    {"move": "stake", "level": "nuisance"},
    {"move": "undo", "change": "opaque-op", "words": "Finish routine"},
])
def test_unknown_repeat_refuses_every_mutation_preserving_w0_and_opaque_bytes(repeat, attempt):
    from copy import deepcopy
    from pathlib import Path
    from brain_core.records import parse_record, render_record
    path = Path(__file__).resolve().parents[1] / "fixtures/work_model/made_up_post_audit/records/item/2026/09/brn_20260930_050004_d000000000000033.md"
    before_file = path.read_bytes()
    record = parse_record(path.read_text(encoding="utf-8"), str(path)) if repeat == "W0:f004" else item("Opaque routine", data={"repeat": repeat})
    before_record = render_record(record).encode("utf-8")
    before_data = deepcopy(record.as_dict())
    move = {**attempt, **({"thing": record.id} if attempt["move"] != "undo" else {})}
    actor = record.source["recorded_by"]
    out = run([record], "Finish routine; first part; first step; Morgan Friday", [move], actor=actor,
              undo_changes={"opaque-op": [record.id]})
    assert out.refusal["field"] == "repeat" and "daily" in out.refusal["fix"] and "weekly:" in out.refusal["fix"]
    assert not out.records and not out.flags
    assert record.as_dict() == before_data and render_record(record).encode("utf-8") == before_record
    assert not WORK_FIELDS.intersection(record.data)
    assert path.read_bytes() == before_file


def test_unknown_repeat_refusal_discards_other_batch_outputs_and_preserves_ordinary_null_repeat():
    opaque = item("Opaque routine", data={"repeat": "every morning"})
    out = run([opaque], "new task; Opaque routine", [{"move": "new", "title": "new task"},
              {"move": "tick", "thing": opaque.id}])
    assert out.refusal["field"] == "repeat" and not out.records
    ordinary = item("Passport", data={"repeat": None})
    out = run([ordinary], "Passport started", [{"move": "start", "thing": ordinary.id}])
    assert out.refusal is None and out.records[0].data["started_at"]


@pytest.mark.parametrize("now,repeat,day", [
    (datetime(2026, 9, 30, 19, 59, tzinfo=UTC), "daily", "2026-09-30"),
    (datetime(2026, 9, 30, 20, 0, tzinfo=UTC), "daily", "2026-10-01"),
    (NOW, "weekly:wed", "2026-09-30"),
])
def test_supported_routine_tick_without_quote_stays_open_at_personal_day_boundary(now, repeat, day):
    routine = item("Routine", data={"repeat": repeat})
    out = run([routine], "yes", [{"move": "tick", "thing": routine.id}], now=now)
    assert out.refusal is None and out.records[0].state == "open"
    assert out.records[0].data["occurrence"] == {"day": day, "outcome": "done"}
    assert not WORK_FIELDS.intersection(out.records[0].data)


def test_unknown_repeat_in_indirect_merge_or_undo_prior_refuses_atomically():
    from dataclasses import replace
    parent, target = item("Parent"), item("Target", number=2)
    child = item("Opaque child", number=3, data={"repeat": "fortnightly", "parent": parent.id})
    out = run([parent, target, child], "merge Parent into Target", [
        {"move": "organise", "thing": parent.id, "merge_into": target.id, "words": "merge Parent into Target"}])
    assert out.refusal["field"] == "repeat" and not out.records
    prior = item("Routine", data={"repeat": "fortnightly"})
    current = replace(item("Routine", data={"repeat": "daily"}, number=4), supersedes=(prior.id,))
    out = run([prior, current], "undo Routine", [{"move": "undo", "change": "routine-op", "words": "undo Routine"}],
              undo_changes={"routine-op": [current.id]})
    assert out.refusal["field"] == "repeat" and not out.records


def test_supported_repeat_flags_stay_reply_only_and_end_still_requires_owner_quote():
    routine = item("Routine", data={"repeat": "daily"})
    out = run([routine], "yes", [{"move": "end", "thing": routine.id}])
    assert out.refusal is None and out.flags[0]["dropped"]
    assert out.records[0].state == "open" and not WORK_FIELDS.intersection(out.records[0].data)
    capture = run([], "something else", [{"move": "new", "title": "Routine", "repeat": "daily"}])
    assert capture.refusal is None and capture.flags
    assert not WORK_FIELDS.intersection(capture.records[0].data)


def test_whole_history_and_first_ids():
    parent = item("Passport")
    first = run([parent], "Passport open doc", [{"move": "start", "thing": parent.id}], operation="first")
    second = run([parent, *first.records], "Passport open doc",
                 [{"move": "next_action", "thing": parent.id, "text": "open doc"}], operation="second")
    out = run([parent, *first.records, *second.records], "Passport started",
              [{"move": "start", "thing": first.records[0].id}], operation="third")
    assert out.records[-1].supersedes == (parent.id, first.records[0].id, second.records[0].id)
    assert out.reply["things"][-1]["id"] == parent.id


def test_multiple_moves_emit_only_one_final_version():
    parent = item("Passport")
    out = run([parent], "Passport started open doc", [
        {"move": "start", "thing": parent.id}, {"move": "next_action", "thing": parent.id, "text": "open doc"}])
    assert len(out.records) == 1
    assert out.records[0].data["started_at"]
    assert out.records[0].data["next_action"]["text"] == "open doc"
    assert out.records[0].supersedes == (parent.id,)


def answer(thing, question, offered, *, day="2026-09-30", open=True, marks=()):
    return ValidatedAnswer(reference="why-log:1", question=question, offered=offered,
                           thing=thing.id, day=day, open=open, marks=marks)


def test_review_yes_merges_only_the_offered_thing():
    first, second = item("Venue rental"), item("Get venue rental", number=2)
    line = answer(first, "Same as 'Get venue rental'? Merge?", ["merge"])
    out = run([first, second], "yes", [{"move": "organise", "thing": first.id, "merge_into": second.id}], answers=[line])
    assert out.refusal is None
    assert out.records[0].state == "no_longer_needed"
    assert out.records[0].evidence[-2].excerpt == line["question"]
    assert out.records[0].evidence[-2].locator == line["reference"]
    assert out.records[0].evidence[-1].excerpt == "yes"
    assert out.records[0].source["words"] == "yes"


def test_review_named_parts_receive_venue_rental_stake_without_own_copy():
    stake = {"level": "changes_life", "at_stake": "40 display slots a month", "confirmed": True, "proposed_by": ACTOR}
    parent = item("Venue rental", data={"stake": stake})
    line = answer(parent, "Venue rental names three parts: compare rates; call bank; sign loan. Yes?", ["parts"])
    out = run([parent], "yes", [{"move": "parts", "thing": parent.id,
        "parts": [{"title": "compare rates"}, {"title": "call bank"}, {"title": "sign loan"}]}], answers=[line])
    assert out.refusal is None
    assert len(out.records) == 3
    index = work.index_things([parent, *out.records])
    for part in out.records:
        assert not part.data.get("review_marks")
        assert "stake" not in part.data
        assert index[part.id].data["stake"] == stake


@pytest.mark.parametrize("wrong", ["target", "offered", "field"])
def test_answer_authority_does_not_leak(wrong):
    first, second = item("Venue rental"), item("Get venue rental", number=2)
    line = answer(first, "Same as 'Get venue rental'? Merge?", ["merge"])
    move = {"move": "organise", "thing": first.id, "merge_into": second.id}
    if wrong == "target":
        move["thing"], move["merge_into"] = second.id, first.id
    elif wrong == "offered":
        move = {"move": "tick", "thing": first.id}
    else:
        move["title"] = "Agent rename"
    out = run([first, second], "yes", [move], answers=[line])
    assert out.records[0].state == "open"
    assert out.records[0].title in {"Venue rental", "Get venue rental"}
    assert out.records[0].data["review_marks"][-1]["mark"] == "question"


@pytest.mark.parametrize("day,open,valid", [("2026-09-30", True, True), ("2026-09-29", True, True),
                                           ("2026-09-29", False, False), ("2026-09-28", True, False)])
def test_review_day_and_open_check(day, open, valid):
    parent = item("Venue rental")
    line = answer(parent, "Venue rental done?", ["done"], day=day, open=open)
    out = run([parent], "yes", [{"move": "tick", "thing": parent.id}], answers=[line])
    assert (out.refusal is None) == valid
    if not valid:
        assert out.refusal["fix"]
        assert not out.records


def test_typo_allowance_does_not_authorise_quote_or_date_or_person():
    parent = item("research")
    out = run([parent], "resaerch done", [{"move": "tick", "thing": parent.id, "words": "research done"}])
    assert out.records[0].state == "open"
    out = run([parent], "research by Frdiay", [{"move": "date", "thing": parent.id, "when": "by Friday"}])
    assert "dates" not in out.records[0].data
    out = run([parent], "research is Jsamine's", [{"move": "wait", "thing": parent.id, "kind": "theirs", "who": "Rowan"}])
    assert "whose" not in out.records[0].data


def test_replaces_deadline_keeps_the_kind_and_november_30():
    date = {"words": "before december", "read_by": "code", "read_on": "2026-09-29", "day": "2026-11-30"}
    parent = item("Passport", data={"dates": {"deadline": date}})
    out = run([parent], "actually lets say end of november", [
        {"move": "date", "thing": parent.id, "when": "end of november", "replaces": "deadline"}])
    assert out.refusal is None
    assert out.records[0].data["dates"]["deadline"]["day"] == "2026-11-30"
    assert "planned" not in out.records[0].data["dates"]


def test_old_shape_dates_and_wait_links_are_carried_without_loss():
    blocker = item("Get photo", number=2)
    parent = item("Passport", data={"timing": {"due": "2026-11-30",
        "resolved_from": {"words": "before december", "today": "2026-09-29"}}},
        links=[{"type": "blocked_by", "target": blocker.id}])
    out = run([parent, blocker], "Passport next action open form", [
        {"move": "next_action", "thing": parent.id, "text": "open form"}])
    assert out.refusal is None
    assert out.records[0].data["dates"]["deadline"]["day"] == "2026-11-30"
    assert out.records[0].data["after"] == [blocker.id]
    assert "due" not in out.records[0].data["timing"]
    assert not out.records[0].links


def test_own_write_data_does_not_copy_inherited_facts():
    parent = item("Venue rental", data={"labels": ["admin"], "stake": {"level": "changes_life", "confirmed": True, "proposed_by": ACTOR},
        "dates": {"deadline": {"day": "2026-11-30", "words": "by end of november", "read_by": "code", "read_on": "2026-09-29"}}})
    part = item("Call bank", number=2, data={"parent": parent.id})
    out = run([parent, part], "Call bank next action dial bank", [
        {"move": "next_action", "thing": part.id, "text": "dial bank"}])
    assert out.refusal is None
    assert not {"stake", "labels", "dates"}.intersection(out.records[0].data)


@pytest.mark.parametrize("field,value", [("accept", True), ("title", "Renew passport"), ("called", "task")])
def test_organise_owner_answers(field, value):
    parent = item("Passport", data={"level": "suggested", "called": "project"})
    message = "call Passport Renew passport accept it as a task"
    out = run([parent], message, [{"move": "organise", "thing": parent.id, field: value, "words": message}])
    assert out.refusal is None
    if field == "accept":
        assert out.records[0].data["level"] == "accepted"
    elif field == "title":
        assert out.records[0].title == value
    else:
        assert "called" not in out.records[0].data


def test_keep_confirms_agent_date():
    parent = item("Passport", data={"dates": {"planned": {"day": "2026-10-15", "words": "mid Oct", "read_by": ACTOR, "read_on": "2026-09-29"}}})
    out = run([parent], "Passport keep that date", [{"move": "organise", "thing": parent.id, "keep": "date", "words": "Passport keep that date"}])
    assert out.records[0].data["dates"]["planned"]["confirmed"] is True


@pytest.mark.parametrize("agent_read", [False, True])
def test_saved_date_reader_offers_keep_and_answer_confirms_only_offered_target(agent_read):
    from brain_core.orientation import rank
    from brain_core.review import ItemRecord, MorningBootstrap, MorningInputs, build_morning
    deadline = {"day": "2026-11-30", "words": "before december", "read_by": "code", "read_on": "2026-09-29"}
    parent = item("Passport", data={"dates": {"deadline": deadline}})
    other = item("Dental", number=2, data={"dates": {"planned": {
        "day": "2026-10-15", "words": "mid Oct", "read_by": ACTOR, "read_on": "2026-09-29"}}})
    kind, phrase, details = (("planned", "mid Oct", {"reading": {"day": "2026-10-15"}}) if agent_read else
                             ("start_by", "start by Monday", {"proposed": True}))
    first = run([parent, other], f"Passport {phrase}", [
        {"move": "date", "thing": parent.id, "when": phrase, **details}], operation="date-offer")
    assert first.refusal is None
    saved = first.records[0]
    assert saved.data["dates"][kind]["read_by"] == (ACTOR if agent_read else "code")
    assert not saved.data.get("review_marks"), "a saved date uses the reader's own date question"
    if agent_read:
        assert any(flag["message"].startswith("I read '") and not flag["dropped"] for flag in first.flags)
        assert "I read 'mid Oct' as Thu 15 Oct" in first.reply["say"]
    else:
        assert saved.data["dates"][kind]["confirmed"] is False
    all_records = [parent, other, saved]
    reader = work.index_things(all_records)
    expected = dict(kind="needs_you", item_ref=parent.id, text=f"Keep {kind}: {phrase}?",
                    count=0, allowed_answers=("keep", "change"))
    assert work.needs_nic(parent.id, reader, NOW) == (expected,)
    detailed = work.needs_nic(parent.id, reader, NOW, with_answer_kind=True)
    assert detailed == ({**expected, "answer_kind": "agents_wording" if agent_read else "proposed_start_by"},)
    wrapped = tuple(ItemRecord(record=r, path=f"records/{r.id}.md") for r in (saved, other))
    morning = build_morning(MorningInputs(wrapped, (), (), (), {}, NOW,
        bootstrap=MorningBootstrap("a" * 40, frozenset((parent.id, other.id)))))
    lines = [line for part in morning.parts for line in part.lines if line.item_ref == parent.id]
    shown = next(line for line in lines if line.section == "new")
    assert "keep" in shown.offered and shown.allowed_answers == ("keep", "change")
    assert any(line.section == "your_day" for line in lines), "R17's date question keeps its day pick"
    if not agent_read:
        rank_now = NOW.replace(month=10, day=6)
        ranked = rank((wrapped[0],), rank_now.date(), now=rank_now)
        assert not ranked[0].fields_used.get("points", {}).get("start_by")
    context = answer(parent, shown.text, shown.offered)
    keep = {"move": "organise", "thing": parent.id, "keep": "date"}
    landed = run(all_records, "keep that date", [keep], answers=[context], operation="date-kept")
    assert landed.refusal is None and not any(flag["dropped"] for flag in landed.flags)
    assert landed.records[0].data["dates"][kind]["confirmed"] is True
    assert landed.records[0].evidence[-2].excerpt == shown.text
    assert landed.records[0].source["words"] == "keep that date"
    if not agent_read:
        ranked = rank((ItemRecord(landed.records[0], "kept.md"),), rank_now.date(), now=rank_now)
        assert ranked[0].fields_used["points"]["start_by"] > 0
    after = work.index_things([*all_records, *landed.records])
    assert not work.needs_nic(parent.id, after, NOW)
    assert after[other.id].data["dates"]["planned"].get("confirmed") is None
    wrong = run(all_records, "keep that date", [{**keep, "thing": other.id}],
                answers=[context], operation="date-wrong-target")
    assert wrong.refusal is None and any(flag["dropped"] for flag in wrong.flags)
    assert wrong.records[0].data["dates"]["planned"].get("confirmed") is None
    bare = run(all_records, "keep that date", [keep], operation="date-without-answer")
    assert bare.refusal is None and any(flag["dropped"] for flag in bare.flags)
    assert bare.records[0].data["dates"][kind].get("confirmed") is not True


def test_saved_date_preserves_unrelated_questions_and_suggested_date_subtype():
    parent = item("Passport", data={"review_marks": [{"mark": "question", "text": "Original unresolved question"}]})
    out = run([parent], "Passport mid Oct", [{"move": "date", "thing": parent.id,
              "when": "mid Oct", "reading": {"day": "2026-10-15"}}])
    assert out.refusal is None
    assert out.records[0].data["review_marks"] == parent.data["review_marks"]
    reader = work.index_things([parent, *out.records])
    detailed = work.needs_nic(parent.id, reader, NOW, with_answer_kind=True)
    assert [need["answer_kind"] for need in detailed] == ["question", "agents_wording"]
    assert detailed[0]["allowed_answers"] == ("answer", "leave it")
    assert detailed[1]["allowed_answers"] == ("keep", "change")
    forward = run([], "send portfolio mid Oct", [{"move": "new", "title": "send portfolio",
                  "when": "mid Oct", "reading": {"day": "2026-10-15"}}], said_by="other", speaker="Morgan")
    assert forward.refusal is None
    record = forward.records[0]
    assert record.data["dates"]["suggested"]["speaker"] == "Morgan"
    assert not record.data.get("review_marks")
    assert any(flag["message"].startswith("I read '") for flag in forward.flags)
    reader = work.index_things(forward.records)
    default = work.needs_nic(record, reader, NOW)
    detailed = work.needs_nic(record, reader, NOW, with_answer_kind=True)
    assert [need["answer_kind"] for need in detailed] == ["suggested", "date_suggested"]
    assert all(need["allowed_answers"] == ("yes", "no") for need in default)
    assert default == tuple({key: value for key, value in need.items() if key != "answer_kind"} for need in detailed)


@pytest.mark.parametrize("message,phrase,reading", [
    ("Passport", "mid Oct", {"day": "2026-10-15"}),
    ("Passport mid Oct", "mid Oct", {"day": "2026-09-15"}),
    ("Passport mid Oct", "mid Oct", None),
])
def test_invalid_date_reading_retains_original_dropped_question(message, phrase, reading):
    parent = item("Passport")
    move = {"move": "date", "thing": parent.id, "when": phrase}
    if reading is not None:
        move["reading"] = reading
    out = run([parent], message, [move])
    assert out.refusal is None and "dates" not in out.records[0].data
    assert out.flags and all(flag["dropped"] for flag in out.flags)
    marks = out.records[0].data["review_marks"]
    assert all(mark["mark"] == "question" and message in mark["text"] for mark in marks)
    needs = work.needs_nic(parent.id, work.index_things([parent, *out.records]), NOW, with_answer_kind=True)
    assert all(need["answer_kind"] == "question" and need["allowed_answers"] == ("answer", "leave it") for need in needs)


def test_routine_date_flags_remain_reply_only_with_existing_raise_with():
    parent = item("stretch", data={"repeat": "daily", "raise_with": "Sam"})
    before = parent.sha256
    out = run([parent], "stretch mid Oct", [{"move": "date", "thing": parent.id,
              "when": "mid Oct", "reading": {"day": "2026-10-15"}}])
    assert out.refusal is None and out.flags
    assert not out.records
    assert parent.sha256 == before and parent.data["raise_with"] == "Sam"
    assert not WORK_FIELDS.intersection(parent.data)


def test_start_by_proposal_requires_deadline_and_stays_unconfirmed():
    date = {"day": "2026-11-30", "words": "before december", "read_by": "code", "read_on": "2026-09-29"}
    for present in (True, False):
        parent = item("Passport", data={"dates": {"deadline": date}} if present else None)
        out = run([parent], "Passport start by Monday", [
            {"move": "date", "thing": parent.id, "when": "start by Monday", "proposed": True}])
        assert ("start_by" in out.records[0].data.get("dates", {})) == present
        if present:
            assert out.records[0].data["dates"]["start_by"]["confirmed"] is False
            needs = work.needs_nic(parent.id, work.index_things([parent, *out.records]), NOW)
            assert any(need["text"] == "Keep start_by: start by Monday?" for need in needs)
            assert not out.records[0].data.get("review_marks")
        else:
            assert out.records[0].data["review_marks"][-1]["mark"] == "question"


def test_registered_code_caller_names_date_kind_and_unknown_is_refused():
    actor = "system:test-bridge"
    parent = item("Passport")
    move = {"move": "date", "thing": parent.id, "when": "Friday", "kind": "deadline"}
    out = run([parent], "Passport Friday", [move], actor=actor)
    assert out.refusal["code"] == "ACCESS_DENIED"
    out = run([parent], "Passport Friday", [move], actor=actor, allowed_code_callers=frozenset({actor}))
    assert out.refusal is None
    assert out.records[0].data["dates"]["deadline"]["read_by"] == actor
    assert not out.records[0].data.get("review_marks")


def test_capture_guesses_are_marks_not_called_or_confirmed_stakes():
    out = run([], "plan campaign", [{"move": "new", "title": "plan campaign", "looks_like": "project",
        "ask_stake": True, "stake": {"level": "changes_life", "at_stake": "40 display slots"}}])
    assert "called" not in out.records[0].data
    assert out.records[0].data["stake"]["confirmed"] is False
    assert {mark["mark"] for mark in out.records[0].data["review_marks"]} == {"looks_like", "ask_stake"}


@pytest.mark.parametrize("field,cap", [("at_stake", 512), ("odds", 256)])
def test_stake_working_caps(field, cap):
    parent = item("Passport")
    for size, valid in [(cap, True), (cap + 1, False)]:
        out = run([parent], "Passport has a stake", [{"move": "stake", "thing": parent.id, field: "x" * size}])
        assert (out.refusal is None) == valid
        if not valid:
            assert out.refusal["fix"]


def test_more_than_25_actual_records_refuses_everything():
    titles = [f"part {n}" for n in range(12)]
    message = "First Second " + " ".join(titles)
    out = run([], message, [{"move": "new", "title": "First"}, {"move": "new", "title": "Second"},
        {"move": "parts", "thing": "#1", "parts": [{"title": title} for title in titles]},
        {"move": "parts", "thing": "#2", "parts": [{"title": title} for title in titles]}])
    assert not out.records
    assert out.refusal["fix"] == "split it"


def test_reply_has_full_character_cap():
    title = "long " * 800
    out = run([], title, [{"move": "new", "title": title}])
    from brain_core.contracts import canonical_json
    assert len(canonical_json(out.reply)) <= 2000
    assert out.records[0].title == title


def test_missing_execution_context_refuses_with_fix():
    change = Change(operation_id="new", message="venue invoice", said_by="owner", came_by="direct",
                    moves=[{"move": "new", "title": "venue invoice"}])
    out = apply_change({}, change, actor=ACTOR, now=NOW)
    assert out.refusal["field"] == "_context"
    assert out.refusal["fix"]


def test_replay_is_duplicate_and_changed_moves_conflict():
    parent = item("Passport")
    moves = [{"move": "start", "thing": parent.id}]
    first = run([parent], "Passport started", moves)
    duplicate = run([parent, *first.records], "Passport started", moves)
    assert duplicate.refusal is None
    assert duplicate.reply["status"] == "duplicate"
    assert duplicate.records[0].sha256 == first.records[0].sha256
    conflict = run([parent, *first.records], "Passport open doc", [{"move": "next_action", "thing": parent.id, "text": "open doc"}])
    assert conflict.refusal["code"] == "brain.conflict"


def test_undo_date_restores_old_date_without_new_date_words():
    date = {"day": "2026-11-30", "words": "before december", "read_by": "code", "read_on": "2026-09-29"}
    parent = item("Passport", data={"dates": {"deadline": date}})
    first = run([parent], "Passport by end of october", [{"move": "date", "thing": parent.id, "when": "by end of october"}], operation="date-change")
    out = run([parent, *first.records], "undo Passport date", [{"move": "undo", "change": "date-change", "words": "undo Passport date"}],
        operation="undo-change", undo_changes={"date-change": [r.id for r in first.records]})
    assert out.refusal is None
    assert out.records[0].data["dates"]["deadline"] == date
    assert out.records[0].supersedes == (parent.id, first.records[0].id)


def test_undo_never_finds_tier_b_save_or_changed_since_record():
    parent = item("Passport")
    out = run([parent], "undo Passport", [{"move": "undo", "change": "tier-b-op", "words": "undo Passport"}],
              undo_changes={"tier-b-op": ["brn_20260930_060000_abcdef0123456789"]})
    assert out.refusal["fix"]
    first = run([parent], "Passport started", [{"move": "start", "thing": parent.id}], operation="start")
    second = run([parent, *first.records], "Passport open doc", [{"move": "next_action", "thing": parent.id, "text": "open doc"}], operation="action")
    out = run([parent, *first.records, *second.records], "undo Passport", [{"move": "undo", "change": "start", "words": "undo Passport"}],
        operation="undo", undo_changes={"start": [first.records[0].id]})
    assert not out.records
    assert out.refusal["fix"] == "send the correction as a new change"


def test_future_new_reference_uses_exact_preplanned_first_id():
    out = run([], "First after Second", [{"move": "new", "title": "First"},
        {"move": "wait", "thing": "#1", "kind": "after", "after": ["#2"]},
        {"move": "new", "title": "Second"}])
    assert out.refusal is None
    assert len(out.records) == 2
    assert out.records[0].data["after"] == [out.records[1].id]


def test_planned_day_on_only_the_first_of_two_new_things():
    out = run([], "send Morgan my brief today separately also plan an outreach campaign", [
        {"move": "new", "title": "send Morgan my brief", "when": "today"},
        {"move": "new", "title": "plan an outreach campaign"}])
    assert out.records[0].data["dates"]["planned"]["day"] == "2026-09-30"
    assert "dates" not in out.records[1].data


def test_w6_passes_calendar_clock_to_w3_before_4am():
    out = run([], "Tomorrow look at Board for Workshop", [{"move": "new", "title": "look at Board for Workshop", "when": "Tomorrow"}],
              now=datetime(2026, 9, 29, 16, 10, tzinfo=UTC))
    assert out.records[0].data["dates"]["planned"]["day"] == "2026-09-30"


def test_memo_passport_and_dentist_date_kinds():
    out = run([], "renew my passport before december and remind me to call the dentist next tues after lunch", [
        {"move": "new", "title": "renew my passport", "when": "before december"},
        {"move": "new", "title": "call the dentist", "when": "next tues after lunch"}])
    assert out.records[0].data["dates"]["deadline"]["day"] == "2026-11-30"
    assert out.records[1].data["dates"]["planned"]["day"] == "2026-10-06"
    assert out.records[1].data["dates"]["planned"]["words"] == "next tues after lunch"
    assert "appointment" not in out.records[1].data["dates"]


def test_rowan_expected_day_not_a_deadline():
    out = run([], "rowan said she'll send the hall forms over by friday", [
        {"move": "new", "title": "send hall forms"},
        {"move": "wait", "thing": "#1", "kind": "theirs", "who": "rowan", "expected": "by friday"}])
    assert len(out.records) == 1
    assert out.records[0].data["whose"] == "rowan"
    assert out.records[0].data["expected"]["day"] == "2026-10-02"
    assert "dates" not in out.records[0].data


def test_promise_weekend_is_a_deadline():
    out = run([], "i told sam i'd send him wedding photos this weekend", [
        {"move": "new", "title": "send wedding photos", "owed_to": "sam", "when": "this weekend"}])
    assert out.records[0].data["dates"]["deadline"]["day"] == "2026-10-04"


def test_agent_reading_is_said_back_and_past_day_dropped():
    parent = item("Passport")
    for day, valid in [("2026-10-15", True), ("2026-09-29", False)]:
        out = run([parent], "Passport mid Oct", [{"move": "date", "thing": parent.id, "when": "mid Oct", "reading": {"day": day}}])
        assert ("dates" in out.records[0].data) == valid
        if valid:
            assert out.records[0].data["dates"]["planned"]["read_by"] == ACTOR
            assert "I read 'mid Oct' as Thu 15 Oct" in out.reply["say"]


def test_event_only_wait_reading_keeps_event_words_without_a_calendar_day():
    parent = item("Call the bank")
    out = run([parent], "Waiting for the bank to open", [{
        "move": "wait", "thing": parent.id, "kind": "not_before", "event": "the bank to open",
        "reading": {"event": "the bank to open"}}])
    assert out.refusal is None
    saved = out.records[0].data["dates"]["not_before"]
    assert saved["event"] == "the bank to open" and "day" not in saved
    assert "I read 'the bank to open' as the bank to open" in out.reply["say"]
    assert any(flag["message"] == "I read 'the bank to open' as the bank to open" for flag in out.flags)


def test_code_reading_wins_over_agent_supplied_day():
    parent = item("Passport")
    out = run([parent], "Passport Friday", [{"move": "date", "thing": parent.id, "when": "Friday",
        "reading": {"day": "2026-12-31"}}])
    assert out.records[0].data["dates"]["planned"]["day"] == "2026-10-02"
    assert out.records[0].data["dates"]["planned"]["read_by"] == "code"


def test_own_length_and_block_then_park_keep_quote():
    parent = item("Passport")
    message = "Passport takes 15 minutes admin park Passport until Monday"
    out = run([parent], message, [{"move": "organise", "thing": parent.id, "rough_minutes": 15,
        "length_is_nics": True, "block": "admin", "words": message},
        {"move": "park", "thing": parent.id, "until": "Monday", "words": message}])
    assert len(out.records) == 1
    assert out.records[0].data["timing"] == {"duration_minutes": 15, "padded_minutes": 15, "estimated_by": "nic"}
    assert out.records[0].state == "parked"
    assert out.records[0].data["parked_until"] == "2026-10-05"


def test_end_dropped_without_reason_keeps_question():
    parent = item("Passport")
    out = run([parent], "drop Passport", [{"move": "end", "thing": parent.id, "ending": "dropped", "words": "drop Passport"}])
    assert out.records[0].state == "open"
    assert out.records[0].data["review_marks"][-1]["mark"] == "question"


def test_merge_moves_only_open_parts_under_first_target_id():
    first, second = item("Venue rental"), item("Get venue rental", number=2)
    part = item("Call bank", number=3, data={"parent": first.id})
    out = run([first, second, part], "merge Venue rental into Get venue rental", [
        {"move": "organise", "thing": first.id, "merge_into": second.id, "words": "merge Venue rental into Get venue rental"}])
    assert len(out.records) == 2
    assert out.records[0].state == "no_longer_needed"
    assert out.records[1].data["parent"] == second.id


def test_partial_retry_is_refused_without_patch_up():
    moves = [{"move": "new", "title": "Venue rental"}, {"move": "parts", "thing": "#1", "parts": [{"title": "call bank"}, {"title": "sign loan"}]}]
    first = run([], "Venue rental call bank sign loan", moves)
    out = run(first.records[:2], "Venue rental call bank sign loan", moves)
    assert not out.records
    assert out.refusal["code"] == "brain.record.exists"


def test_undo_all_records_verified_before_any_output():
    moves = [{"move": "new", "title": "Venue rental"}, {"move": "parts", "thing": "#1", "parts": [{"title": "call bank"}, {"title": "sign loan"}]}]
    first = run([], "Venue rental call bank sign loan", moves, operation="capture")
    second = run(first.records, "call bank started", [{"move": "start", "thing": first.records[1].id}], operation="start")
    out = run([*first.records, *second.records], "undo Venue rental", [{"move": "undo", "change": "capture", "words": "undo Venue rental"}],
        operation="undo", undo_changes={"capture": [r.id for r in first.records]})
    assert not out.records
    assert out.refusal["fix"] == "send the correction as a new change"


def test_dropped_question_mark_is_capped_without_losing_flag_words():
    parent = item("Passport")
    message = "Passport " + "long " * 300
    out = run([parent], message, [{"move": "tick", "thing": parent.id}])
    assert len(out.records[0].data["review_marks"][-1]["text"]) == 1024
    assert out.flags[0]["words"] == message


def test_moves_are_pure_and_dont_mutate_supplied_records():
    from copy import deepcopy
    parent = item("Passport", data={"review_marks": [{"mark": "question", "text": "Keep?"}]})
    before = deepcopy(parent.as_dict())
    out = run([parent], "yes", [{"move": "organise", "thing": parent.id, "keep": "question"}],
              answers=[answer(parent, "Passport keep its question?", ["keep"], marks=["question"])])
    assert out.refusal is None
    assert parent.as_dict() == before


def test_routine_when_uses_only_legacy_timing_shape():
    out = run([], "weigh myself weekly Friday", [{"move": "new", "title": "weigh myself", "repeat": "weekly:fri", "when": "Friday"}])
    assert out.refusal is None
    record = out.records[0]
    assert not WORK_FIELDS.intersection(record.data)
    assert record.data["timing"]["due"] == "2026-10-02"
    assert record.data["timing"]["resolved_from"] == {"words": "Friday", "today": "2026-09-30"}


@pytest.mark.parametrize("bad", [{"label": ""}, {"when": ""}, {"looks_like": "task"},
                                   {"stake": {"confirmed": True}}, {"stake": False}, {"reading": "tomorrow"}])
def test_invalid_capture_fields_refuse_with_fix(bad):
    out = run([], "Passport", [{"move": "new", "title": "Passport", **bad}])
    assert not out.records
    assert out.refusal["fix"]


@pytest.mark.parametrize("flag", ["done", "then", "length_is_nics"])
def test_parts_flags_require_booleans(flag):
    parent = item("Passport")
    out = run([parent], "Passport take photo", [{"move": "parts", "thing": parent.id,
        "parts": [{"title": "take photo", flag: "false"}]}])
    assert not out.records
    assert out.refusal["field"] == f"parts.{flag}"


def test_wait_reply_uses_the_personal_day_before_4am():
    parent = item("Memo")
    now = datetime(2026, 10, 7, 19, tzinfo=UTC)
    out = run([parent], "Memo not before 8 October", [{"move": "wait", "thing": parent.id,
        "kind": "not_before", "until": "8 October"}], now=now)
    assert out.refusal is None
    assert out.reply["things"][0]["why"] == "not before tomorrow"
    assert "not before tomorrow" in out.reply["say"]
    assert out.reply["things"][0]["dates"] == ["not before tomorrow"]
    assert out.records[0].data["dates"]["not_before"]["day"] == "2026-10-08"


@pytest.mark.parametrize("move", ["date", "wait"])
def test_event_on_a_day_saves_both_date_kinds_with_the_same_words(move):
    parent = item("Safety checklist")
    change = {"move": move, "thing": parent.id, "event": "venue inspection"}
    change.update({"when": "8 October"} if move == "date" else {"kind": "not_before", "until": "8 October"})
    out = run([parent], "Safety checklist at my venue inspection on 8 October", [change])
    assert out.refusal is None
    dates = out.records[0].data["dates"]
    assert set(dates) == {"planned", "not_before"}
    assert dates["planned"] == dates["not_before"]
    assert dates["planned"]["words"] == "8 October"
    assert out.reply["things"][0]["dates"] == ["on Thu 8 Oct (venue inspection)"]
    assert "on Thu 8 Oct (venue inspection)" in out.reply["say"]
    from brain_core.records import parse_record, render_record
    assert parse_record(render_record(out.records[0]), "frozen parser").data == out.records[0].data
    index = work.index_things(out.records)
    assert work.explain(parent.id, index, NOW)["reason"] == "on Thu 8 Oct (venue inspection)"
    on_day = datetime(2026, 10, 8, 1, tzinfo=UTC)
    assert work.is_up(parent.id, index, on_day)
    assert any(n["kind"] == "carried_over" for n in work.needs_nic(parent.id, index, on_day + timedelta(days=1)))


@pytest.mark.parametrize("when,expected", [
    ("today", "planned today"),
    ("tomorrow at 12pm", "on tomorrow at 12 pm"),
    ("Friday at 3pm", "on this Friday, 2 Oct at 3 pm"),
    ("8 October at 3pm", "on Thu 8 Oct at 3 pm"),
])
def test_door_dates_and_say_are_readable_relative_to_the_current_day(when, expected):
    out = run([], "Passport " + when, [{"move": "new", "title": "Passport", "when": when}])
    assert out.refusal is None
    assert out.reply["things"][0]["dates"] == [expected]
    assert expected in out.reply["say"]


def test_a_day_wait_without_an_event_keeps_only_not_before():
    parent = item("Memo")
    out = run([parent], "Memo not before 8 October", [{"move": "date", "thing": parent.id, "when": "not before 8 October"}])
    assert out.refusal is None
    assert set(out.records[0].data["dates"]) == {"not_before"}
