from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from gremlin_town.review.names import run_names
from gremlin_town.review.tidy import read_tidy
from gremlin_town.review.settings import ReviewSettings, ReviewSettingsError
from test_names import NOW, context, named_item


class Service:
    def __init__(self):
        self.commands = []
        self.result = None

    def requests_create(self, actor, command):
        self.commands.append(command)
        return SimpleNamespace(request_id="req_fixture_tidy", error=None)

    def local_request_result(self, actor, request_id):
        return self.result


def setup(tmp_path):
    service = Service()
    item = named_item()
    second = replace(item, record=replace(item.record, id="brn_second", title="Second note"))
    ctx = context(tmp_path, (item, second), service)
    return replace(ctx, settings=replace(ctx.settings, tidy=True)), service


def reply(ctx, **changes):
    row = ctx.why_log.name_requests(NOW.date() + timedelta(days=1))[0]
    values = [dict(item_ref=candidate["item_ref"], suggested_title="Call the lender", suggested_label="Home",
                   suggested_name=None, **changes) for candidate in row["request"]["constraints"]["tidy_candidates"]]
    return json.dumps({"suggestions": values})


def test_tidy_is_one_frozen_request_and_validated_on_time_result(tmp_path):
    ctx, service = setup(tmp_path)
    try:
        assert run_names(ctx, NOW - timedelta(minutes=30)) == {"outcome": "requested", "count": 2, "sent": 1}
        assert len(service.commands) == 1
        assert run_names(ctx, NOW - timedelta(minutes=20)) == {"outcome": "already_requested"}
        service.result = (reply(ctx), NOW - timedelta(minutes=10))
        proposals = read_tidy(ctx, NOW)
        assert set(proposals) == {"brn_name", "brn_second"}
        assert proposals["brn_name"][1]["suggested_title"] == "Call the lender"
        assert read_tidy(replace(ctx, settings=replace(ctx.settings, tidy=False)), NOW) == {}
    finally:
        ctx.why_log.close()


@pytest.mark.parametrize("kind", ["late", "before_request", "duplicate", "missing", "unknown", "extra", "markup", "not_json"])
def test_bad_results_never_become_suggestions(tmp_path, kind):
    ctx, service = setup(tmp_path)
    try:
        run_names(ctx, NOW - timedelta(minutes=30))
        payload = json.loads(reply(ctx))
        returned = NOW - timedelta(minutes=5)
        if kind == "late":
            returned = NOW
        if kind == "before_request":
            returned = NOW - timedelta(minutes=31)
        if kind == "duplicate":
            payload["suggestions"].append(payload["suggestions"][0])
        if kind == "missing":
            payload["suggestions"].pop()
        if kind == "unknown":
            payload["suggestions"][0]["item_ref"] = "brn_unknown"
        if kind == "extra":
            payload["suggestions"][0]["deadline"] = "tomorrow"
        if kind == "markup":
            payload["suggestions"][0]["suggested_title"] = "line\nbreak"
        service.result = ("not json" if kind == "not_json" else json.dumps(payload), returned)
        assert read_tidy(ctx, NOW) == {}
    finally:
        ctx.why_log.close()


def test_tidy_setting_is_independently_off_and_in_backup_schema():
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert not ReviewSettings.from_table(None).tidy
    assert ReviewSettings.from_table({"tidy": True}).tidy
    assert "tidy" in RUNTIME_CONFIG_SCHEMA["review"]
    with pytest.raises(ReviewSettingsError):
        ReviewSettings.from_table({"tidy": "yes"})


def test_lost_response_reuses_frozen_request_even_if_items_changed(tmp_path):
    ctx, service = setup(tmp_path)
    original_create = service.requests_create
    def lose_once(actor, command):
        result = original_create(actor, command)
        if len(service.commands) == 1:
            raise OSError("fixture lost reply")
        return result
    service.requests_create = lose_once
    try:
        with pytest.raises(OSError):
            run_names(ctx, NOW - timedelta(minutes=30))
        changed = replace(ctx, sources=replace(ctx.sources, brain_items=lambda: ()))
        assert run_names(changed, NOW - timedelta(minutes=20))["sent"] == 1
        assert service.commands[0] == service.commands[1]
        assert read_tidy(ctx, NOW) == {}
        def failed(*args):
            raise OSError("fixture unavailable")
        service.local_request_result = failed
        assert read_tidy(ctx, NOW) == {}
    finally:
        ctx.why_log.close()
