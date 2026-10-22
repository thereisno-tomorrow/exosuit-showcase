"""Part notes use the existing body and only Nic's supplied words."""
import pytest

from brain_core import work
from brain_core.records import parse_record, render_record
from brain_core.review import ItemRecord, ReviewInputs, build_morning
from test_moves import NOW, answer, item, run


DETAILS = "Confirm the branch and documents.\nBring my passport."


def test_new_and_parts_keep_short_titles_and_original_details_through_tick():
    message = "Venue rental. Compare rates. Call bank. " + DETAILS
    out = run([], message, [
        {"move": "new", "title": "Venue rental", "details": "Compare rates"},
        {"move": "parts", "thing": "#1", "parts": [
            {"title": "Call bank", "details": DETAILS}]}])
    assert out.refusal is None and not out.flags
    parent, part = out.records
    assert parent.title == "Venue rental" and parent.body == "Compare rates"
    assert part.title == "Call bank" and part.body == DETAILS
    assert parse_record(render_record(part), "part.md").body == DETAILS
    assert out.reply["things"][1]["details"] == DETAILS
    assert "Call bank:" in out.reply["say"]
    assert "\n  Confirm the branch and documents.\n  Bring my passport." in out.reply["say"]
    entries = tuple(ItemRecord(record=r, path=f"{r.id}.md") for r in out.records)
    review = ReviewInputs(items=entries, calendar_events=(), calendar_marked_ids=frozenset(),
        open_requests=(), history=(), source_status={}, now=NOW)
    assert all("Confirm the branch" not in line.text and "Bring my passport" not in line.text
        for section in build_morning(review).parts for line in section.lines)
    ticked = run(out.records, "Done Call bank", [{"move": "tick", "thing": part.id, "words": "Done Call bank"}],
        operation="tick-part")
    assert ticked.refusal is None and ticked.records[0].body == DETAILS
    assert work.status_of(ticked.records[0], work.index_things([parent, *ticked.records]), NOW) == "ended"


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("details", ["Invented checklist", "", " ", 42])
def test_details_not_from_message_refuse_whole_change(new, details):
    parent = item("Venue rental")
    moves = [{"move": "new", "title": "Venue rental", "details": details}] if new else [
        {"move": "parts", "thing": parent.id, "parts": [{"title": "Call bank", "details": details}]}]
    out = run([parent], "Venue rental Call bank", moves)
    assert not out.records and out.refusal["field"] == ("details" if new else "parts.details")


def test_review_question_does_not_authorize_invented_details_on_yes():
    parent = item("Venue rental")
    line = answer(parent, f"Call bank: {DETAILS} Yes?", ["parts"])
    out = run([parent], "yes", [{"move": "parts", "thing": parent.id,
        "parts": [{"title": "Call bank", "details": DETAILS}]}], answers=[line])
    assert not out.records and out.refusal["field"] == "parts.details"


def test_new_details_cannot_be_other_peoples_words_and_omission_stays_empty():
    out = run([], "Call bank " + DETAILS, [{"move": "new", "title": "Call bank", "details": DETAILS}],
        said_by="other", came_by="direct", speaker="Sam")
    assert not out.records and out.refusal["field"] == "details"
    out = run([], "Venue rental Call bank", [{"move": "new", "title": "Venue rental"},
        {"move": "parts", "thing": "#1", "parts": [{"title": "Call bank"}]}])
    assert out.refusal is None and all(record.body == "" for record in out.records)
    assert all("details" not in thing for thing in out.reply["things"])
