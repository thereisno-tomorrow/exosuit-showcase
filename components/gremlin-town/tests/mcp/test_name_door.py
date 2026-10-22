"""Exact review-field provenance through the authenticated door; fake GitHub only."""
import pytest

from gremlin_town.mcp.brain_runtime import WorkRequest, BrainRuntimeError
from gremlin_town.review.why_log import Surfacing
from test_work_door import NOW, setup_door, call, records


@pytest.mark.parametrize("message,name", [("yes", "Atlas campaign"), ("Save", "Atlas campaign"),
                                         ("Call it Atlas job", "Atlas job")])
def test_door_accepts_shown_name_or_edit_and_replay_once(tmp_path, message, name):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, WorkRequest("capture-name", "I want a Atlas application", "owner", "direct",
            ({"move": "new", "title": "Atlas application", "looks_like": "project"},)))
        original = records(fake)[0]
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "needs_you",
            {"review_marks": ["looks_like"], "suggested_name": "Atlas campaign"},
            question="Looks like a project. Yes? Its name?", offered=("project", "title"))])
        (reference,) = log.review_references(NOW)
        change = WorkRequest("accept-name", message, "owner", "direct",
            ({"move": "organise", "thing": original.id, "title": name},), answers=(reference,))
        before = len(fake.commits)
        result = call(provider, change)
        assert result["status"] == "accepted" and len(fake.commits) == before + 1
        saved = next(r for r in records(fake) if r.title == name)
        assert saved.source["words"] == message and saved.body == original.body
        assert saved.data.get("review_marks") == original.data["review_marks"]
        assert log.answer_context(reference).open
        if name == "Atlas campaign":
            assert any(e.locator == reference and "Suggested: Atlas campaign" in e.excerpt for e in saved.evidence)
        assert call(provider, change)["status"] == "duplicate"
        assert len(fake.commits) == before + 1
    finally:
        log.close()


def test_name_on_other_line_or_unoffered_line_gives_no_authority(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, WorkRequest("two-names", "Atlas application and Venue rental", "owner", "direct", (
            {"move": "new", "title": "Atlas application"}, {"move": "new", "title": "Venue rental"})))
        original, other = sorted(records(fake), key=lambda record: record.title)
        log.record_surfacings([Surfacing(NOW, "review", other.id, other.sha256, "needs_you",
            {"suggested_name": "Atlas campaign"}, question="Its name?", offered=("title",))])
        (reference,) = log.review_references(NOW)
        head = fake.branch
        with pytest.raises(BrainRuntimeError, match="another thing"):
            call(provider, WorkRequest("wrong-line", "yes", "owner", "direct",
                ({"move": "organise", "thing": original.id, "title": "Atlas campaign"},), answers=(reference,)))
        assert fake.branch == head
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "needs_you",
            {"suggested_name": "Atlas campaign"}, question="What's next?", offered=("next_action",))])
        reference = log.review_references(NOW)[-1]
        with pytest.raises(BrainRuntimeError, match="did not offer"):
            call(provider, WorkRequest("wrong-offer", "yes", "owner", "direct",
                ({"move": "organise", "thing": original.id, "title": "Atlas campaign"},), answers=(reference,)))
        assert fake.branch == head
    finally:
        log.close()


@pytest.mark.parametrize("source", ["no-reference", "other", "connected-source", "unrelated"])
def test_yes_needs_owner_words_and_name_line_reference_for_name_authority(tmp_path, source):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, WorkRequest("capture-name", "I want a Atlas application", "owner", "direct",
            ({"move": "new", "title": "Atlas application", "looks_like": "project"},)))
        original = records(fake)[0]
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "needs_you",
            {"review_marks": ["looks_like"], "suggested_name": "Atlas campaign"},
            question="Its name?", offered=("title",))])
        (reference,) = log.review_references(NOW)
        head = fake.branch
        move = {"move": "organise", "thing": original.id, "title": "Atlas campaign"}
        if source == "unrelated":
            move = {"move": "next_action", "thing": original.id, "text": "Atlas campaign"}
        change = WorkRequest("unauthorized-name", "yes", "other" if source == "other" else "owner",
            "connected_source" if source == "connected-source" else "direct", (move,),
            speaker="Diablo" if source in {"other", "connected-source"} else None,
            answers=() if source == "no-reference" else (reference,))
        if source == "no-reference":
            result = call(provider, change)
            assert result["status"] == "accepted"
            assert any(flag["dropped"] and flag["field"] == "words" for flag in result["things"][0]["flags"])
            saved = next(r for r in records(fake) if r.id == result["things"][0]["record_id"])
            assert saved.title == original.title and saved.data["review_marks"]
            assert all(e.locator != reference for e in saved.evidence)
            assert all("Suggested:" not in e.excerpt for e in saved.evidence)
        else:
            with pytest.raises(BrainRuntimeError) as error:
                call(provider, change)
            assert error.value.fix
            if source == "unrelated":
                assert "did not offer" in error.value.message
            else:
                assert "nothing was saved" in error.value.message.lower()
                assert error.value.field == "said_by"
                assert "Atlas application: renaming was not saved: someone else's words can only be saved as suggestions" in error.value.message
                assert "still-open review line" in error.value.fix
            assert fake.branch == head and records(fake)[0].as_dict() == original.as_dict()
        assert log.answer_context(reference).open
        accepted = call(provider, WorkRequest("authorized-name", "yes", "owner", "direct",
            ({"move": "organise", "thing": original.id, "title": "Atlas campaign"},), answers=(reference,)))
        assert accepted["status"] == "accepted" and log.answer_context(reference).open
        saved = next(r for r in records(fake) if r.id == accepted["things"][0]["record_id"])
        assert any(mark["mark"] == "looks_like" for mark in saved.data.get("review_marks", ()))
        assert accepted["things"][0]["title"] == "Atlas campaign"
    finally:
        log.close()
