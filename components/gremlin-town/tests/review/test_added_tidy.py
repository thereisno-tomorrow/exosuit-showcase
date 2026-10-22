from dataclasses import replace
from datetime import timedelta
import json

import pytest

from brain_core.timewords import SINGAPORE

from gremlin_town.review.tidy import run_added_tidy, read_added_tidy, run_tidy, added_tidy_revision
from gremlin_town.contracts import Actor, MessageKind, Packet, ReplyRequest
from tests.dashboard.test_ask import hub as gt_hub
from test_tidy import setup
from test_names import NOW


def run(ctx, now=NOW, **extra):
    return run_added_tidy(ctx, now, batch_id="fixture-batch", item_refs=["brn_name", "brn_second"], enabled=True, **extra)


def reply(ctx, service, now=NOW):
    rows = ctx.why_log.name_requests(now.astimezone(SINGAPORE).date())
    row = next(row for row in rows if row["item_ref"].startswith("__added_tidy_batch__"))
    values = [dict(item_ref=candidate["item_ref"], suggested_title="Call the fixture lender", suggested_label="Home",
                   suggested_name=None) for candidate in row["request"]["constraints"]["tidy_candidates"]]
    service.result = (json.dumps({"suggestions": values}), now + timedelta(seconds=5))


def read(ctx, now=NOW, **extra):
    return read_added_tidy(ctx, now + timedelta(seconds=10), current_items=ctx.sources.brain_items(),
                           open_item_refs={"brn_name", "brn_second"}, enabled=True, **extra)


def test_mid_review_batch_returns_at_once_and_reuses_existing_name_request_storage(tmp_path):
    ctx, service = setup(tmp_path)
    try:
        assert run_tidy(ctx, NOW)["outcome"] == "outside_window"
        assert run(ctx) == {"outcome": "requested", "count": 2, "sent": 1}
        assert read(ctx) == {}  # the ordinary items are not withheld while Diablo works
        pending = read_added_tidy(ctx, NOW, current_items=ctx.sources.brain_items(),
            open_item_refs={"brn_name", "brn_second"}, enabled=True, include_waiting=True)
        assert pending == {"suggestions": {}, "waiting_refs": ["brn_name", "brn_second"]}
        reply(ctx, service)
        assert set(read(ctx)) == {"brn_name", "brn_second"}
        ready = read_added_tidy(ctx, NOW + timedelta(seconds=10), current_items=ctx.sources.brain_items(),
            open_item_refs={"brn_name", "brn_second"}, enabled=True, include_waiting=True)
        assert ready["waiting_refs"] == [] and set(ready["suggestions"]) == {"brn_name", "brn_second"}
        assert run(ctx)["outcome"] == "already_requested" and len(service.commands) == 1
    finally:
        ctx.why_log.close()


def test_batch_disabled_is_noop_and_independent_of_pre_review_tidy_setting(tmp_path):
    ctx, service = setup(tmp_path)
    try:
        assert run_added_tidy(ctx, NOW, batch_id="fixture-batch", item_refs=["brn_name"])["outcome"] == "disabled"
        ctx = replace(ctx, settings=replace(ctx.settings, tidy=False))
        assert run(ctx)["outcome"] == "requested"
        assert len(service.commands) == 1
    finally:
        ctx.why_log.close()


def test_late_tidy_does_not_override_answered_or_renamed_items(tmp_path):
    ctx, service = setup(tmp_path)
    try:
        run(ctx)
        reply(ctx, service)
        original, second = ctx.sources.brain_items()
        renamed = replace(original, record=replace(original.record, title="Nic's own replacement"))
        assert read_added_tidy(ctx, NOW + timedelta(seconds=10), current_items=(renamed, second),
                               open_item_refs={"brn_name"}, enabled=True) == {}
        assert len(service.commands) == 1
    finally:
        ctx.why_log.close()


def test_unknown_delivery_replays_frozen_batch_not_new_context(tmp_path):
    ctx, service = setup(tmp_path)
    original = service.requests_create
    def lose(actor, command):
        receipt = original(actor, command)
        if len(service.commands) == 1:
            raise TimeoutError("fixture lost response")
        return receipt
    service.requests_create = lose
    try:
        with pytest.raises(TimeoutError):
            run(ctx)
        changed = replace(ctx, sources=replace(ctx.sources, brain_items=lambda: ()))
        assert run(changed)["outcome"] == "requested"
        assert service.commands[0] == service.commands[1]
        with pytest.raises(ValueError, match="different batch"):
            run_added_tidy(ctx, NOW, batch_id="fixture-batch", item_refs=["brn_name"], enabled=True)
    finally:
        ctx.why_log.close()


def test_tidy_poll_revision_uses_local_gt_state_without_reading_items(tmp_path, gt_hub, monkeypatch):
    ctx, _ = setup(tmp_path)
    ctx = replace(ctx, service=gt_hub.service, settings=replace(ctx.settings,
        sender_participant="fixture-sender", delivery_participant="fixture-recipient", project="fixture-project"))
    try:
        before = added_tidy_revision(ctx, NOW, enabled=True)
        run(ctx)
        queued = added_tidy_revision(ctx, NOW, enabled=True)
        assert queued != before
        gt_hub.worker.dispatch_once()
        row = ctx.why_log.name_requests(NOW.astimezone(SINGAPORE).date())[0]
        request = gt_hub.store.request(row["request_id"])
        gt_hub.service.requests_reply(Actor("fixture-recipient"), ReplyRequest("fixture-tidy-result", request.request_id,
            request.revision, MessageKind.RESULT, Packet(1, MessageKind.RESULT, None, "Fixture", '{"suggestions":[]}',
            {}, (), "", "", "private")))
        gt_hub.worker.dispatch_once()
        def forbidden(*args): pytest.fail("poll must not read items or provider")
        ctx = replace(ctx, sources=replace(ctx.sources, brain_items=forbidden))
        monkeypatch.setattr(gt_hub.service, "requests_read", forbidden)
        result = added_tidy_revision(ctx, NOW, enabled=True)
        assert result != queued and result == added_tidy_revision(ctx, NOW, enabled=True)
    finally:
        ctx.why_log.close()


@pytest.mark.parametrize("kind", ["foreign", "missing", "duplicate", "extra", "before", "future", "next_day"])
def test_bad_mid_review_results_never_become_suggestions(tmp_path, kind):
    ctx, service = setup(tmp_path)
    try:
        run(ctx)
        reply(ctx, service)
        value = json.loads(service.result[0])
        returned = service.result[1]
        if kind == "foreign": value["suggestions"][0]["item_ref"] = "foreign-item"
        if kind == "missing": value["suggestions"].pop()
        if kind == "duplicate": value["suggestions"].append(value["suggestions"][0])
        if kind == "extra": value["suggestions"][0]["done"] = True
        if kind == "before": returned = NOW - timedelta(seconds=1)
        if kind == "future": returned = NOW + timedelta(seconds=30)
        if kind == "next_day": returned = NOW + timedelta(days=1)
        service.result = json.dumps(value), returned
        assert read(ctx) == {}
    finally:
        ctx.why_log.close()


@pytest.mark.parametrize("reply_after_midnight", [False, True])
def test_added_tidy_and_exact_retry_survive_midnight_until_card_expires(tmp_path, reply_after_midnight):
    ctx, service = setup(tmp_path)
    before = NOW.astimezone(SINGAPORE).replace(hour=23, minute=58)
    after = before + timedelta(minutes=5)
    expires = after.replace(hour=6, minute=0)
    try:
        assert run(ctx, before)["outcome"] == "requested"
        reply(ctx, service, before)
        if reply_after_midnight:
            service.result = (service.result[0], after - timedelta(seconds=10))
        kwargs = dict(current_items=ctx.sources.brain_items(), open_item_refs={"brn_name", "brn_second"},
                      enabled=True)
        assert set(read_added_tidy(ctx, after, **kwargs)) == {"brn_name", "brn_second"}
        assert set(read_added_tidy(ctx, expires - timedelta(seconds=1), **kwargs)) == {"brn_name", "brn_second"}
        assert run(ctx, after)["outcome"] == "already_requested" and len(service.commands) == 1
        assert read_added_tidy(ctx, expires, **kwargs) == {}
        assert len(ctx.why_log.name_requests(before.date())) == 1
        assert not ctx.why_log.name_requests(after.date())
    finally:
        ctx.why_log.close()


def test_batch_added_after_midnight_is_associated_with_same_open_card(tmp_path):
    ctx, service = setup(tmp_path)
    now = NOW.astimezone(SINGAPORE).replace(hour=1, minute=15) + timedelta(days=1)
    try:
        assert run(ctx, now)["outcome"] == "requested"
        rows = ctx.why_log.name_requests(now.date() - timedelta(days=1))
        assert len(rows) == 1 and not ctx.why_log.name_requests(now.date())
        candidates = rows[0]["request"]["constraints"]["tidy_candidates"]
        service.result = (json.dumps({"suggestions": [dict(item_ref=item["item_ref"],
            suggested_title="Review fixture note", suggested_label=None, suggested_name=None) for item in candidates]}),
            now + timedelta(seconds=5))
        assert set(read(ctx, now)) == {"brn_name", "brn_second"}
    finally:
        ctx.why_log.close()


def test_local_tidy_fingerprint_uses_same_card_day_across_midnight(tmp_path, gt_hub):
    ctx, _ = setup(tmp_path)
    ctx = replace(ctx, service=gt_hub.service, settings=replace(ctx.settings,
        sender_participant="fixture-sender", delivery_participant="fixture-recipient", project="fixture-project"))
    before = NOW.astimezone(SINGAPORE).replace(hour=23, minute=58)
    after = before + timedelta(minutes=5)
    try:
        empty = added_tidy_revision(ctx, before, enabled=True)
        run(ctx, before)
        queued = added_tidy_revision(ctx, before, enabled=True)
        assert queued != empty and added_tidy_revision(ctx, after, enabled=True) == queued
        assert added_tidy_revision(ctx, after.replace(hour=6, minute=0), enabled=True) == empty
    finally:
        ctx.why_log.close()
