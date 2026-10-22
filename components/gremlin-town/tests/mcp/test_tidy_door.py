import pytest

from brain_core.review import ItemRecord, MorningInputs, build_morning
from gremlin_town.mcp.brain_runtime import WorkRequest, BrainRuntimeError
from gremlin_town.review.why_log import Surfacing
from test_work_door import NOW, setup_door, call, records, request


@pytest.mark.parametrize("message,accepted", [("Save", True), ("yes", False), ("skip", False)])
def test_tidy_title_and_label_need_save(tmp_path, message, accepted):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, request())
        original = records(fake)[0]
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "new_nothing_needed",
            {"suggested_title": "Check the invoice", "suggested_label": "Home", "tidy_suggestion": True},
            question="New", offered=("title", "label", "save"))])
        (ref,) = log.review_references(NOW)
        change = WorkRequest("tidy-save", message, "owner", "direct", ({"move": "organise", "thing": original.id,
            "title": "Check the invoice", "label": "Home"},), answers=(ref,))
        head = fake.branch
        if not accepted:
            with pytest.raises(BrainRuntimeError):
                call(provider, change)
            assert fake.branch == head and log.answer_context(ref).open
        else:
            result = call(provider, change)
            assert result["status"] == "accepted"
            saved = next(r for r in records(fake) if r.title == "Check the invoice")
            assert saved.data["labels"] == ["Home"]
            assert saved.body == original.body
            assert saved.source["words"] == "Save"
            assert any("Suggested title: Check the invoice" in e.excerpt for e in saved.evidence)
            assert call(provider, change)["status"] == "duplicate"
    finally:
        log.close()


def test_changed_record_cannot_accept_old_tidy_proposal(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, request())
        original = records(fake)[0]
        call(provider, WorkRequest("new-length", "It takes 30 minutes", "owner", "direct", (
            {"move": "organise", "thing": original.id, "rough_minutes": 30},)))
        # Simulate a render whose earlier read raced with the change above.
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "new_nothing_needed",
            {"suggested_label": "Home", "tidy_suggestion": True}, question="New", offered=("label", "save"))])
        (ref,) = log.review_references(NOW)
        head = fake.branch
        with pytest.raises(BrainRuntimeError, match="changed after"):
            call(provider, WorkRequest("old-tidy", "Save", "owner", "direct", (
                {"move": "organise", "thing": original.id, "label": "Home"},), answers=(ref,)))
        assert fake.branch == head and log.answer_context(ref).open
    finally:
        log.close()


@pytest.mark.parametrize("field,value", [("label", "Home"), ("title", "Check the invoice")])
def test_saving_tidy_wording_preserves_unanswered_classification(tmp_path, field, value):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, WorkRequest("capture-project", "I want an invoice project", "owner", "direct",
            ({"move": "new", "title": "Invoice project", "looks_like": "project"},)))
        original = records(fake)[0]
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "needs_you",
            {"review_marks": ["looks_like"], "suggested_title": "Check the invoice",
             "suggested_label": "Home", "tidy_suggestion": True},
            question="Looks like a project. Yes?", offered=("project", "title", "label", "save"))])
        (reference,) = log.review_references(NOW)
        result = call(provider, WorkRequest("tidy-wording", "Save", "owner", "direct",
            ({"move": "organise", "thing": original.id, field: value},), answers=(reference,)))
        saved = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
        assert saved.data.get("called") is None
        assert saved.data["review_marks"] == original.data["review_marks"]
        assert (saved.title if field == "title" else saved.data["labels"][0]) == value
        fresh = build_morning(MorningInputs((ItemRecord(saved, "records/fixture.md"),), (), (), (), {}, NOW))
        question = next(line for line in fresh.parts[2].lines if line.rule == "needs_you")
        assert "project" in question.offered
        log.record_surfacings([Surfacing(NOW, "review", original.id, saved.sha256, "needs_you",
            dict(question.fields_used, tidy_suggestion=True), question=question.text, offered=question.offered)])
        classification = log.review_references(NOW)[-1]
        result = call(provider, WorkRequest("answer-classification", "yes", "owner", "direct",
            ({"move": "organise", "thing": original.id, "called": "project"},), answers=(classification,)))
        classified = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
        assert classified.data["called"] == "project" and not classified.data["review_marks"]
    finally:
        log.close()


@pytest.mark.parametrize("message", ["Save", "yes", "OK", "yep", "save please"])
@pytest.mark.parametrize("field", ["title", "label"])
def test_bare_reply_cannot_replace_the_shown_tidy_wording(tmp_path, message, field):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, request())
        original = records(fake)[0]
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "new_nothing_needed",
            {"suggested_title": "Check the invoice", "suggested_label": "Home", "tidy_suggestion": True},
            question="New", offered=("title", "label", "save"))])
        (reference,) = log.review_references(NOW)
        head = fake.branch
        with pytest.raises(BrainRuntimeError):
            call(provider, WorkRequest("replace-with-button", message, "owner", "direct",
                ({"move": "organise", "thing": original.id, field: message},), answers=(reference,)))
        assert fake.branch == head and log.answer_context(reference).open
    finally:
        log.close()


@pytest.mark.parametrize("field,value", [("title", "Review the invoice"), ("label", "Household")])
def test_tidy_wording_can_be_replaced_with_owners_actual_words(tmp_path, field, value):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, request())
        original = records(fake)[0]
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "new_nothing_needed",
            {"suggested_title": "Check the invoice", "suggested_label": "Home", "tidy_suggestion": True},
            question="New", offered=("title", "label", "save"))])
        (reference,) = log.review_references(NOW)
        result = call(provider, WorkRequest("edit-wording", "Call it " + value, "owner", "direct",
            ({"move": "organise", "thing": original.id, field: value},), answers=(reference,)))
        saved = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
        assert (saved.title if field == "title" else saved.data["labels"][0]) == value
    finally:
        log.close()


def test_tidy_save_keeps_classification_open_in_actual_immediate_redraw(tmp_path, monkeypatch):
    from gremlin_town.review.run import current_review_part, run_review
    from test_review_length_door import dated_project, review_context
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        original = dated_project(provider, fake)
        proposal = {original.id: (original.sha256, {"suggested_label": "Home"})}
        monkeypatch.setattr("gremlin_town.review.tidy.read_tidy", lambda *args: proposal)
        context = review_context(tmp_path, fake, log, tidy=True)
        run_review(context, NOW)
        lines = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
        question = next(line for line in lines if line["kind"] == "needs_you")
        change = WorkRequest("tidy-label-only", "Save", "owner", "direct",
            ({"move": "organise", "thing": original.id, "label": "Home"},), answers=(question["reference"],))
        call(provider, change)
        assert log.answer_context(question["reference"]).open
        lines = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
        fresh = next(line for line in lines if line["kind"] == "needs_you" and not line["answered"])
        assert fresh["reference"] != question["reference"] and "project" in fresh["offered"]
        assert any(line["kind"] == "needs_length" and not line["answered"] for line in lines)
        assert current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"] == lines
        assert current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"] == lines
        assert call(provider, change)["status"] == "duplicate"
        result = call(provider, WorkRequest("classify-after-tidy", "yes", "owner", "direct",
            ({"move": "organise", "thing": original.id, "called": "project"},), answers=(fresh["reference"],)))
        saved = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
        assert saved.data["called"] == "project" and saved.data["labels"] == ["Home"]
    finally:
        log.close()
