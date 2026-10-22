"""No-date parking uses the frozen event shape and never wakes itself up."""
from datetime import timedelta

import pytest

from brain_core import work
from brain_core.orientation import OrientationInputs, overview, what_next
from brain_core.records import parse_record, render_record
from brain_core.review import ItemRecord, ReviewInputs, build_morning
from brain_core.weekly import weekly_review
from test_moves import NOW, answer, item, run


@pytest.mark.parametrize("word", [None, "someday", "later", "not now", "Later"])
@pytest.mark.parametrize("supply_until", [False, True])
def test_park_without_day_keeps_his_word_and_never_returns(word, supply_until):
    parent = item("Passport", data={"dates": {
        "deadline": {"day": "2026-09-20", "words": "by 20 Sep", "read_by": "code", "read_on": "2026-09-20"},
        "planned": {"day": "2026-09-21", "words": "21 Sep", "read_by": "code", "read_on": "2026-09-20"}}})
    part = item("Take photo", number=2, data={"parent": parent.id, "whose": "Sam"})
    message = "Park Passport" + (f" {word}" if word else "")
    move = {"move": "park", "thing": parent.id, "words": message}
    if supply_until and word:
        move["until"] = word
    out = run([parent, part], message, [move])
    assert out.refusal is None and not out.flags
    parked = out.records[0]
    assert parked.data["parked_until"] == {"event": word or "someday"}
    assert parse_record(render_record(parked), "parked.md").data == parked.data
    shown = f"parked ({word or 'someday'})"
    assert shown in out.reply["say"]
    for now in (NOW, NOW + timedelta(days=400)):
        index = work.index_things([parked, part])
        assert work.status_of(parked, index, now) == work.status_of(part, index, now) == "parked"
        assert not work.needs_nic(parked, index, now)
        entries = tuple(ItemRecord(record=r, path=f"{r.id}.md") for r in (parked, part))
        review = ReviewInputs(items=entries, now=now, calendar_events=(), source_status={},
            calendar_marked_ids=frozenset(), open_requests=(), history=())
        assert not any(line.item_ref in (parent.id, part.id)
            for section in build_morning(review).parts for line in section.lines)
        orientation = OrientationInputs(review=review)
        lines = [line for line in overview(orientation, now).lines if line.item_ref == parent.id]
        assert len(lines) == 1 and lines[0].section == "parked" and shown in lines[0].text
        assert what_next(orientation, now).top_pick is None
        weekly = [line for line in weekly_review(entries, now).lines if line.item_ref in (parent.id, part.id)]
        assert len(weekly) == 1 and weekly[0].part == "parked" and shown in weekly[0].text


def test_undated_park_can_answer_a_review_line_and_undo_returns_it():
    parent = item("Passport")
    line = answer(parent, "Park or drop?", ["park", "drop"])
    out = run([parent], "someday", [{"move": "park", "thing": parent.id}], answers=[line])
    assert out.refusal is None and out.records[0].state == "parked"
    parked = out.records[0]
    restored = run([parent, parked], "Undo parking Passport", [{"move": "undo", "change": "test-change",
        "words": "Undo parking Passport"}], operation="bring-back", undo_changes={"test-change": [parked.id]})
    assert restored.refusal is None and restored.records[0].state == "open"


def test_invented_park_word_is_dropped_and_a_dated_park_stays_dated():
    parent = item("Passport")
    out = run([parent], "Park Passport", [{"move": "park", "thing": parent.id,
        "until": "later", "words": "Park Passport"}])
    assert out.flags[0]["dropped"] and out.records[0].state == "open"
    out = run([parent], "Park Passport until Monday", [{"move": "park", "thing": parent.id,
        "until": "Monday", "words": "Park Passport until Monday"}])
    assert out.records[0].data["parked_until"] == "2026-10-05"
