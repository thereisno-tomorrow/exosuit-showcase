from dataclasses import replace

from brain_core.review import ItemRecord, MorningInputs
from gremlin_town.review.run import ReviewContext, _build_morning
from gremlin_town.review.settings import ReviewSettings
from test_run import NOW, make_item


def test_opening_setting_reaches_initial_and_refresh_shared_builder():
    record = replace(make_item(id="brn_deadline", title="A deadline"), data={
        "level": "accepted", "size": "task", "dates": {"deadline": {
            "day": "2026-09-27", "words": "tomorrow", "read_by": "code", "read_on": "2026-09-26"}}})
    inputs = MorningInputs((ItemRecord(record, "fixture.md"),), (), (), (), {}, NOW)
    settings = ReviewSettings.from_table({"opening_deadlines": True})
    context = ReviewContext(settings, None, None, None)
    assert any(line.rule == "due_tomorrow" for line in _build_morning(context, inputs).parts[0].lines)
    old = replace(context, settings=ReviewSettings.from_table(None))
    assert any(line.rule == "coming_up" for line in _build_morning(old, inputs).parts[0].lines)


def test_new_lengths_setting_defaults_off_and_is_wired():
    import pytest
    from gremlin_town.review.settings import ReviewSettingsError
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert not ReviewSettings.from_table(None).new_lengths
    assert "new_lengths" in RUNTIME_CONFIG_SCHEMA["review"]
    with pytest.raises(ReviewSettingsError):
        ReviewSettings.from_table({"new_lengths": 1})
    record = replace(make_item(id="brn_length", title="A task"), data={
        "level": "accepted", "size": "task", "dates": {"planned": {
            "day": "2026-09-27", "words": "tomorrow", "read_by": "code", "read_on": "2026-09-26"}}})
    inputs = MorningInputs((ItemRecord(record, "fixture.md"),), (), (), (), {}, NOW)
    context = ReviewContext(ReviewSettings.from_table({"new_lengths": True}), None, None, None)
    assert any(line.rule == "needs_length" for line in _build_morning(context, inputs).parts[2].lines)
