from dataclasses import replace
from datetime import timedelta
import sqlite3
import json

import pytest

from gremlin_town.review.names import run_names
from gremlin_town.review.run import ReviewContext
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.settings import ReviewSettings, ReviewSettingsError
from gremlin_town.review.why_log import WhyLog
from test_run import NOW, make_item
from brain_core.review import ItemRecord
from brain_core.timewords import SINGAPORE
from test_request_closed import Hub, packet
from gremlin_town.contracts import Actor, MessageKind, ReplyRequest, ReportAction, ReportRequest
from gremlin_town.review.names import read_names
from gremlin_town.transport import FakeTransport
from gremlin_town.review.run import run_review, current_review_part
from gremlin_town.contracts import ErrorCode
from gremlin_town.domain.errors import GremlinTownError
from gremlin_town.contracts import MAX_PACKET_BYTES
from gremlin_town.domain.serialization import canonical_bytes, packet_bytes


def names_hub(tmp_path):
    hub = Hub(tmp_path).at(NOW - timedelta(minutes=30))
    hub.service.provider = hub.worker.provider = FakeTransport(clock=hub.clock)
    return hub


def context(tmp_path, items=(), service=None):
    log = WhyLog(tmp_path / "names.db")
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    return ReviewContext(ReviewSettings.from_table({"enabled": True, "why_log_file": str(tmp_path / "names.db")}),
                         log, ReviewSources(brain_items=lambda: items), service)


def named_item():
    record = replace(make_item(id="brn_name", title="Application"), data={
        "level": "accepted", "size": "task", "review_marks": [{"mark": "question", "text": "Its name?"}]})
    return ItemRecord(record, "records/name.md")


def test_listing_empty_disabled_and_time_window(tmp_path):
    ctx = context(tmp_path)
    assert run_names(ctx, NOW - timedelta(minutes=30)) == {"outcome": "empty", "count": 0}
    assert run_names(ctx, NOW) == {"outcome": "outside_window"}
    assert run_names(ctx, NOW - timedelta(minutes=31)) == {"outcome": "outside_window"}
    assert run_names(replace(ctx, settings=ReviewSettings.from_table(None)), NOW) == {"outcome": "disabled"}
    ctx.why_log.close()


@pytest.mark.parametrize("value", ["06:00", "23:00", "5:30", "05:60", 530])
def test_invalid_names_time(value):
    with pytest.raises(ReviewSettingsError, match="names_at"):
        ReviewSettings.from_table({"names_at": value})


def test_names_cli_disabled_is_read_only(tmp_path, monkeypatch, capsys):
    import json
    from gremlin_town.entrypoint import main
    from tests.review.test_review_cli import write_config
    config = write_config(tmp_path, review_table="")
    monkeypatch.setenv("GT_TEST_AGENTMAIL_KEY", str(tmp_path / "agentmail.key"))
    assert main(["review", "names", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "disabled"}


def test_context_is_sent_once_with_existing_project_names(tmp_path):
    hub = names_hub(tmp_path)
    original = named_item()
    original = replace(original, record=replace(original.record, body="Original context " * 1100))
    project = replace(original, record=replace(original.record, id="brn_example", title="Venue rental",
        body="", data={"level": "accepted", "size": "task", "called": "project"}))
    ctx = replace(context(tmp_path, (original, project), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    assert run_names(ctx, hub.clock.now)["sent"] == 1  # the context fits one existing 32 KiB packet
    row = ctx.why_log.name_requests(hub.clock.now.astimezone(SINGAPORE).date())[0]
    assert row["request"]["constraints"]["project_name_examples"] == ["Venue rental"]
    assert row["request"]["constraints"]["name_candidate"]["first_words"] == original.record.body
    ctx.why_log.close()


@pytest.mark.parametrize("character", ["x", "漢", '"'])
def test_oversized_context_is_bounded_before_freeze_and_replays(tmp_path, monkeypatch, character):
    hub = names_hub(tmp_path)
    original = named_item()
    original = replace(original, record=replace(original.record, body=character * 40000,
        data=dict(original.record.data, labels=["keep this label"])))
    ctx = replace(context(tmp_path, (original,), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    prepare = ctx.why_log.prepare_name_request
    frozen_requests = []

    def checked_prepare(day, item_ref, record_sha256, now, request):
        assert len(canonical_bytes(request)) < MAX_PACKET_BYTES
        assert len(json.dumps(request, sort_keys=True).encode("utf-8")) < MAX_PACKET_BYTES
        frozen_requests.append(json.loads(json.dumps(request)))
        return prepare(day, item_ref, record_sha256, now, request)

    create = hub.service.requests_create
    packets = []

    def checked_create(actor, command):
        assert len(packet_bytes(command.packet)) < MAX_PACKET_BYTES
        packets.append(command.packet)
        return create(actor, command)

    monkeypatch.setattr(ctx.why_log, "prepare_name_request", checked_prepare)
    monkeypatch.setattr(hub.service, "requests_create", checked_create)
    assert run_names(ctx, hub.clock.now)["sent"] == 1
    row = ctx.why_log.name_requests(NOW.astimezone(SINGAPORE).date())[0]
    candidate = row["request"]["constraints"]["name_candidate"]
    assert candidate["title"] == original.record.title
    assert candidate["labels"] == ["keep this label"]
    assert 0 < len(candidate["first_words"]) < 40000
    assert original.record.body.startswith(candidate["first_words"])
    assert row["request"] == frozen_requests[0]
    assert packets[0].constraints == row["request"]["constraints"]
    assert run_names(ctx, hub.clock.now)["sent"] == 0
    assert ctx.why_log.name_requests(NOW.astimezone(SINGAPORE).date())[0]["request"] == row["request"]
    assert len(packets) == 1
    ctx.why_log.close()


@pytest.mark.parametrize("field", ["tasks", "labels", "project_name_examples"])
def test_longest_context_field_is_trimmed_while_title_and_short_words_survive(tmp_path, field):
    hub = names_hub(tmp_path)
    original = named_item()
    original = replace(original, record=replace(original.record, body="Keep original words"))
    items = [original]
    if field == "labels":
        items[0] = replace(original, record=replace(original.record,
            data=dict(original.record.data, labels=["x" * 40000])))
    else:
        data = {"level": "accepted", "size": "task"}
        data.update({"parent": original.record.id} if field == "tasks" else {"called": "project"})
        items.append(replace(original, record=replace(original.record, id="brn_context",
            title="x" * 40000, body="", data=data)))
    ctx = replace(context(tmp_path, items, hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    assert run_names(ctx, hub.clock.now)["sent"] == 1
    row = ctx.why_log.name_requests(NOW.astimezone(SINGAPORE).date())[0]
    assert len(json.dumps(row["request"], sort_keys=True).encode("utf-8")) < MAX_PACKET_BYTES
    constraints = row["request"]["constraints"]
    candidate = constraints["name_candidate"]
    assert candidate["title"] == original.record.title
    assert candidate["first_words"] == original.record.body
    trimmed = constraints[field] if field == "project_name_examples" else candidate[field]
    assert 0 < len(trimmed[0]) < 40000
    ctx.why_log.close()


@pytest.mark.parametrize("failure", ["raised", "receipt", "prepare"])
def test_failed_first_candidate_does_not_stop_remaining_requests(tmp_path, monkeypatch, caplog, failure):
    from gremlin_town.review import names
    from gremlin_town.review.why_log import WhyLogError

    hub = names_hub(tmp_path)
    first = named_item()
    second = replace(first, record=replace(first.record, id="brn_other", title="Other"))
    ctx = replace(context(tmp_path, (first, second), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    send = names.deliver
    attempts = []

    def failing_send(service, **request):
        attempts.append(request)
        if request["constraints"]["name_candidate"]["item_ref"] == first.record.id:
            if failure == "raised":
                raise GremlinTownError(ErrorCode.SOURCE_UNAVAILABLE, "private refusal details")
            if failure == "receipt":
                from gremlin_town.contracts import Receipt, ReceiptError
                return Receipt("failed-operation", None, hub.clock.now, None, None, None, "exosuit-review",
                               error=ReceiptError(ErrorCode.SOURCE_UNAVAILABLE, "private refusal details"))
        return send(service, **request)

    monkeypatch.setattr(names, "deliver", failing_send)
    if failure == "prepare":
        prepare = ctx.why_log.prepare_name_request

        def failing_prepare(day, item_ref, record_sha256, now, request):
            if item_ref == first.record.id:
                raise WhyLogError("private storage details")
            return prepare(day, item_ref, record_sha256, now, request)

        monkeypatch.setattr(ctx.why_log, "prepare_name_request", failing_prepare)
    assert run_names(ctx, hub.clock.now) == {"outcome": "requested", "count": 2, "sent": 1}
    rows = {row["item_ref"]: row for row in ctx.why_log.name_requests(NOW.astimezone(SINGAPORE).date())}
    assert rows[second.record.id]["request_id"]
    if failure != "prepare":
        assert rows[first.record.id]["request_id"] is None
        assert [request["constraints"]["name_candidate"]["item_ref"] for request in attempts] == [first.record.id, second.record.id]
        assert run_names(ctx, hub.clock.now)["sent"] == 0
        assert attempts[0] == attempts[-1]  # retry keeps the frozen request
    warnings = [record.getMessage() for record in caplog.records if record.name == "gremlin_town.review.names"]
    assert warnings and all("private" not in message for message in warnings)
    ctx.why_log.close()


def reply(hub, request_id, text, *, dispatch=True):
    hub.drain()
    revision = hub.item("hermes-wa", request_id)["revision"]
    started = hub.service.requests_report(Actor(actor_id="hermes-wa"),
        ReportRequest(f"start-{request_id}", request_id, revision, ReportAction.START))
    hub.service.requests_reply(Actor(actor_id="hermes-wa"), ReplyRequest(
        f"result-{request_id}", request_id, started.revision, MessageKind.RESULT, packet(MessageKind.RESULT, text)))
    if dispatch:
        hub.drain()


def test_real_request_context_replay_and_attributed_accepted_result(tmp_path):
    hub = names_hub(tmp_path)
    item = named_item()
    ctx = replace(context(tmp_path, (item,), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    assert run_names(ctx, hub.clock.now) == {"outcome": "requested", "count": 1, "sent": 1}
    (row,) = ctx.why_log.name_requests(NOW.astimezone(SINGAPORE).date())
    request = row["request"]
    assert request["constraints"]["name_candidate"]["title"] == item.record.title
    assert "first_words" in request["constraints"]["name_candidate"]
    assert "tasks" in request["constraints"]["name_candidate"]
    assert "labels" in request["constraints"]["name_candidate"]
    changed = replace(ctx, sources=ReviewSources(brain_items=lambda: (replace(item,
        record=replace(item.record, title="Changed")),)))
    assert run_names(changed, hub.clock.now)["sent"] == 0
    assert ctx.why_log.name_requests(hub.clock.now.astimezone(SINGAPORE).date())[0]["request"] == request
    assert read_names(ctx, NOW) == {}
    hub.at(NOW - timedelta(minutes=10))
    reply(hub, row["request_id"], '{"suggested_name":"Atlas application"}', dispatch=False)
    assert read_names(ctx, NOW) == {}  # queued is not returned
    hub.drain()
    assert read_names(ctx, NOW) == {item.record.id: (item.record.sha256, "Atlas application")}
    with pytest.raises(Exception, match="only the requester"):
        hub.service.local_request_result(Actor(actor_id="hermes-wa"), row["request_id"])
    ctx.why_log.close()


@pytest.mark.parametrize("text,late", [('{"suggested_name":"Name"}', True),
    ('["Name", "Other"]', False), ('{"suggested_name":["Name", "Other"]}', False),
    ('{"suggested_name":"Name", "other":"Other"}', False), ('Name', False),
    ('{"suggested_name":""}', False), ('{"suggested_name":"Name\\nOther"}', False)])
def test_late_or_malformed_answer_is_empty(tmp_path, text, late):
    hub = names_hub(tmp_path)
    ctx = replace(context(tmp_path, (named_item(),), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    run_names(ctx, hub.clock.now)
    row = ctx.why_log.name_requests(hub.clock.now.astimezone(SINGAPORE).date())[0]
    hub.at(NOW if late else NOW - timedelta(minutes=5))
    reply(hub, row["request_id"], text)
    assert read_names(ctx, NOW + timedelta(minutes=5)) == {}
    ctx.why_log.close()


def test_suggestion_in_frozen_morning_and_current_part_only_name_line(tmp_path):
    hub = names_hub(tmp_path)
    original = named_item()
    ctx = replace(context(tmp_path, (original,), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    before = original.record.sha256
    run_names(ctx, hub.clock.now)
    row = ctx.why_log.name_requests(hub.clock.now.astimezone(SINGAPORE).date())[0]
    hub.at(NOW - timedelta(minutes=5))
    reply(hub, row["request_id"], '{"suggested_name":"Atlas application"}')
    hub.at(NOW)
    assert run_review(ctx, NOW)["outcome"] == "sent"
    frozen = ctx.why_log.morning_packet(NOW.astimezone(SINGAPORE).date())
    (line,) = [line for line in frozen.request["constraints"]["lines"] if line["suggested_name"]]
    assert line["suggested_name"] == "Atlas application" and line["item_ref"] == original.record.id
    assert "Atlas application" not in line["question"] and "Atlas application" not in frozen.request["text"]
    assert ctx.why_log.answer_context(line["reference"]).suggested_name == "Atlas application"
    fresh = current_review_part(ctx, NOW + timedelta(minutes=1), caller="hermes-wa", part=3)
    shown = next(shown for shown in fresh["lines"] if shown["reference"] == line["reference"])
    assert shown["suggested_name"] == line["suggested_name"]
    assert original.record.sha256 == before
    assert run_review(ctx, NOW + timedelta(minutes=1)) == {"outcome": "already_sent"}
    assert ctx.why_log.morning_packet(NOW.astimezone(SINGAPORE).date()).request == frozen.request
    ctx.why_log.close()


@pytest.mark.parametrize("late,changed", [(True, False), (False, True)])
def test_missing_or_stale_result_keeps_original_question_and_never_waits(tmp_path, late, changed):
    hub = names_hub(tmp_path)
    original = named_item()
    ctx = replace(context(tmp_path, (original,), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    run_names(ctx, hub.clock.now)
    row = ctx.why_log.name_requests(hub.clock.now.astimezone(SINGAPORE).date())[0]
    hub.at(NOW if late else NOW - timedelta(minutes=5))
    reply(hub, row["request_id"], '{"suggested_name":"Atlas application"}')
    if changed:
        ctx = replace(ctx, sources=ReviewSources(brain_items=lambda: (
            replace(original, record=replace(original.record, body="new context")),)))
    hub.at(NOW)
    assert run_review(ctx, NOW)["outcome"] == "sent"
    packet = ctx.why_log.morning_packet(NOW.astimezone(SINGAPORE).date())
    assert all(line["suggested_name"] is None for line in packet.request["constraints"]["lines"])
    assert "Its name?" in packet.request["text"]
    ctx.why_log.close()


@pytest.mark.parametrize("call_site", ["morning", "current_part"])
@pytest.mark.parametrize("failure", ["missing", "service", "os", "sqlite"])
def test_lookup_failure_does_not_block_review_or_other_suggestions(tmp_path, monkeypatch, caplog,
                                                                 call_site, failure):
    hub = names_hub(tmp_path)
    original = named_item()
    other = replace(original, record=replace(original.record, id="brn_other", title="Other"))
    ctx = replace(context(tmp_path, (original, other), hub.service), settings=ReviewSettings.from_table({
        "enabled": True, "why_log_file": str(tmp_path / "names.db"), "project": "vault_home"}))
    run_names(ctx, hub.clock.now)
    rows = {row["item_ref"]: row for row in ctx.why_log.name_requests(NOW.astimezone(SINGAPORE).date())}
    hub.at(NOW - timedelta(minutes=5))
    reply(hub, rows[other.record.id]["request_id"], '{"suggested_name":"Other name"}')
    hub.at(NOW)
    if call_site == "current_part":
        assert run_review(ctx, NOW)["outcome"] == "sent"

    request_id = rows[original.record.id]["request_id"]
    if failure == "missing":
        request_id = "unavailable-request"
        ctx.why_log.record_name_request(NOW.astimezone(SINGAPORE).date(), original.record.id, request_id)
    else:
        reader = hub.service.local_request_result
        error = {"service": GremlinTownError(ErrorCode.SOURCE_UNAVAILABLE, "private result details"),
                 "os": OSError("private storage details"),
                 "sqlite": sqlite3.OperationalError("private database details")}[failure]

        def failing_reader(actor, lookup_id):
            if lookup_id == request_id:
                raise error
            return reader(actor, lookup_id)

        monkeypatch.setattr(hub.service, "local_request_result", failing_reader)

    if call_site == "morning":
        assert run_review(ctx, NOW)["outcome"] == "sent"
        lines = ctx.why_log.morning_packet(NOW.astimezone(SINGAPORE).date()).request["constraints"]["lines"]
    else:
        lines = current_review_part(ctx, NOW + timedelta(minutes=1), caller="hermes-wa", part=3)["lines"]
    questions = {line["item_ref"]: line for line in lines if line["offered"] and "title" in line["offered"]}
    assert questions[original.record.id]["suggested_name"] is None
    assert "Its name?" in questions[original.record.id].get("question", questions[original.record.id].get("text", ""))
    assert questions[other.record.id]["suggested_name"] == "Other name"
    code = ErrorCode.ACCESS_DENIED if failure == "missing" else ErrorCode.SOURCE_UNAVAILABLE
    warnings = [record for record in caplog.records if record.name == "gremlin_town.review.names"]
    assert [record.getMessage() for record in warnings] == [f"{request_id} {code.value}"]
    ctx.why_log.close()
