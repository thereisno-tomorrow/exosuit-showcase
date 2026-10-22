from types import SimpleNamespace

import pytest
from brain_core.review import ItemRecord
from gremlin_town.mcp.brain_runtime import BrainRuntimeError, WorkRequest
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.run import ReviewContext, current_review_part, run_review
from gremlin_town.review.settings import ReviewSettings
from test_work_door import NOW, setup_door, call, records, request


def review_context(tmp_path, fake, log, **settings):
    class Sender:
        def requests_create(self, actor, command):
            return SimpleNamespace(request_id="req_fixture", error=None)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    return ReviewContext(ReviewSettings.from_table({"enabled": True,
        "why_log_file": str(tmp_path / "why.sqlite3"), "new_lengths": True, **settings}), log,
        ReviewSources(brain_items=lambda: tuple(ItemRecord(r, f"records/{r.id}.md") for r in records(fake))), Sender())


def dated_project(provider, fake):
    result = call(provider, WorkRequest("dated-project", "Invoice project today", "owner", "direct",
        ({"move": "new", "title": "Invoice project", "looks_like": "project", "when": "today"},)))
    return next(record for record in records(fake) if record.id == result["things"][0]["record_id"])


def test_length_preserves_classification_in_actual_immediate_redraw(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        original = dated_project(provider, fake)
        context = review_context(tmp_path, fake, log)
        run_review(context, NOW)
        lines = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
        length = next(line for line in lines if line["kind"] == "needs_length")
        change = WorkRequest("length-only", "30 min", "owner", "direct", (
            {"move": "organise", "thing": original.id, "rough_minutes": 30, "length_is_nics": True},),
            answers=(length["reference"],))
        result = call(provider, change)
        saved = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
        assert saved.data["review_marks"] == original.data["review_marks"]
        fresh = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
        classification = next(line for line in fresh if line["kind"] == "needs_you" and not line["answered"])
        assert "project" in classification["offered"]
        assert call(provider, change)["status"] == "duplicate"
        result = call(provider, WorkRequest("classify-after-length", "yes", "owner", "direct",
            ({"move": "organise", "thing": original.id, "called": "project"},), answers=(classification["reference"],)))
        saved = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
        assert saved.data["called"] == "project" and not saved.data["review_marks"]
    finally:
        log.close()


@pytest.mark.parametrize("message,minutes,accepted", [
    ("skip", 120, False), ("yes", 120, False), ("15 min", 120, False),
    ("skip 2 hours", 120, False), ("not 2 hours", 120, False),
    ("don't choose 2 hours", 120, False), ("30 or 120 minutes", 120, False),
    ("1.5 hours", 300, False), ("-2 hours", 120, False),
    ("an hour and a half", 60, False), ("one hour and thirty minutes", 60, False),
    ("It takes 30 minutes", 120, False), ("30 min", 30, True),
    ("2 hours", 120, True), ("It takes 45 minutes", 45, True),
])
def test_length_requires_the_owners_actual_duration(tmp_path, message, minutes, accepted):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        original = dated_project(provider, fake)
        context = review_context(tmp_path, fake, log)
        run_review(context, NOW)
        line = next(line for line in current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
                    if line["kind"] == "needs_length")
        change = WorkRequest("owner-length", message, "owner", "direct", (
            {"move": "organise", "thing": original.id, "rough_minutes": minutes, "length_is_nics": True},),
            answers=(line["reference"],))
        head = fake.branch
        if accepted:
            result = call(provider, change)
            saved = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
            assert saved.data["timing"]["duration_minutes"] == minutes
        else:
            with pytest.raises(BrainRuntimeError):
                call(provider, change)
            assert fake.branch == head and log.answer_context(line["reference"]).open
            fresh = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
            assert any(row["kind"] == "needs_length" and not row["answered"] for row in fresh)
    finally:
        log.close()


def test_bare_duration_chip_requires_that_value_in_the_frozen_choices(tmp_path):
    from gremlin_town.review.why_log import Surfacing
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        original = dated_project(provider, fake)
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "needs_length",
            {"length_choices": [15]}, question="How long?", offered=("length", "rough_minutes"))])
        (reference,) = log.review_references(NOW)
        head = fake.branch
        with pytest.raises(BrainRuntimeError):
            call(provider, WorkRequest("unoffered-length", "30 min", "owner", "direct",
                ({"move": "organise", "thing": original.id, "rough_minutes": 30, "length_is_nics": True},),
                answers=(reference,)))
        assert fake.branch == head and log.answer_context(reference).open
    finally:
        log.close()


def test_new_day_then_length_refreshes_and_saves_through_the_door(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    class Sender:
        def requests_create(self, actor, command):
            return SimpleNamespace(request_id="req_fixture", error=None)
    try:
        call(provider, request())
        original = records(fake)[0]
        log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
        context = ReviewContext(ReviewSettings.from_table({"enabled": True,
            "why_log_file": str(tmp_path / "why.sqlite3"), "new_lengths": True}), log,
            ReviewSources(brain_items=lambda: tuple(ItemRecord(r, f"records/{r.id}.md") for r in records(fake))), Sender())
        run_review(context, NOW)
        new = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
        question = next(row for row in new if row["kind"] == "new_schedule")
        assert not any(row["kind"] == "needs_length" for row in new)
        dated = call(provider, WorkRequest("choose-day", "today", "owner", "direct", (
            {"move": "date", "thing": original.id, "when": "today"},), answers=(question["reference"],)))
        assert dated["status"] == "accepted"
        new = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
        length = next(row for row in new if row["kind"] == "needs_length")
        assert not length["answered"]
        chosen = WorkRequest("choose-length", "30 min", "owner", "direct", (
            {"move": "organise", "thing": original.id, "rough_minutes": 30, "length_is_nics": True},),
            answers=(length["reference"],))
        result = call(provider, chosen)
        assert result["status"] == "accepted"
        saved = next(r for r in records(fake) if r.id == result["things"][0]["record_id"])
        assert saved.data["timing"] == {"duration_minutes": 30, "padded_minutes": 30, "estimated_by": "nic"}
        assert call(provider, chosen)["status"] == "duplicate"
        new = current_review_part(context, NOW, caller="hermes-wa", part=3)["lines"]
        assert not any(row["kind"] == "needs_length" and not row["answered"] for row in new)
    finally:
        log.close()
