"""`[review]`'s orchestration: disabled reads/sends nothing; enabled logs before sending.

The disabled case is capability 10 and the boundary's own "off by default"
promise (step E spec §2, §9's `test_disabled_by_default_reads_and_sends_nothing`).
The enabled case exercises a real `GremlinTownService` (real `SQLiteStore`,
real `Policy`, step B's `open=true` filter) so the review's delivery and its
"Waiting on" section are proven against the real request store, not a fake.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import CalendarEvent, ItemRecord, OpenRequest

from gremlin_town.contracts import Actor, CreateRequest, MessageKind, Packet, RequestMode
from gremlin_town.domain import Participant, Policy
from gremlin_town.review.calendar_link import marker
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.run import ReviewContext, run_moments, run_review
from gremlin_town.review.settings import ReviewSettings
from gremlin_town.review.why_log import Surfacing, WhyLog
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore

NOW = datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT on 2026-09-26


def fixture_log(path):
    """Synthetic empty pre-move input; these local fake captures happen after it."""
    log = WhyLog(path)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    return log


def test_missing_pinned_bootstrap_fails_with_field_and_fix_before_read_log_or_send(tmp_path):
    from gremlin_town.review.run import ReviewPrerequisiteError

    def forbidden(*args):
        raise AssertionError("missing bootstrap must not read records")

    class ForbiddenSender:
        def requests_create(self, *args):
            raise AssertionError("missing bootstrap must not send")

    log = WhyLog(tmp_path / "why.sqlite3")
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), log,
                            ReviewSources(brain_items=forbidden), ForbiddenSender())
    with pytest.raises(ReviewPrerequisiteError, match="morning_bootstrap.input_sha/first_ids") as failure:
        run_review(context, NOW)
    assert "stage_morning_bootstrap" in failure.value.fix
    assert log.history() == () and log.last_sent_review_at() is None
    assert log.morning_bootstrap() is None
    log.close()


def test_bootstrap_abort_retry_keeps_backdated_new_and_carry_then_consumes_once(tmp_path, real_service):
    from dataclasses import replace

    log = WhyLog(tmp_path / "why.sqlite3")
    carried = replace(make_item(id="brn_carried", title="Plan", created_at=NOW - timedelta(days=3)),
                      data={"level": "accepted", "dates": {"planned": {
                          "day": "2026-09-25", "words": "Friday", "read_by": "code", "read_on": "2026-09-25"}}})
    captured = make_item(id="brn_new", title="New backdated identity", created_at=NOW - timedelta(days=10))
    log.stage_morning_bootstrap(input_sha="c" * 40, first_ids=(carried.id,))
    entries = tuple(ItemRecord(record, f"raw/{record.id}.md") for record in (carried, captured))

    class FailedDelivery:
        def requests_create(self, *args):
            raise RuntimeError("fixture send aborted")

    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), log,
                            ReviewSources(brain_items=lambda: entries), FailedDelivery())
    with pytest.raises(RuntimeError, match="fixture send aborted"):
        run_review(context, NOW)
    assert any(row.rule == "carried_over" for row in log.history())
    assert log.morning_history() == () and log.shown_item_refs() == frozenset()
    assert not log.morning_bootstrap_completed()
    log.close()
    log = WhyLog(tmp_path / "why.sqlite3")
    recording = _CapturingRequestCreator(real_service)
    context = replace(context, why_log=log, service=recording)
    assert run_review(context, NOW)["outcome"] == "sent"
    packet = recording.created[-1].packet
    assert any(row["item_ref"] == captured.id and "New, nothing needed" in row["question"]
               for row in packet.constraints["lines"])
    assert any(row["item_ref"] == carried.id and "planned for" in row["question"]
               for row in packet.constraints["lines"])
    assert any(row.item_ref == carried.id and row.rule == "carried_over" for row in log.morning_history())
    assert log.morning_bootstrap_completed() and log.last_sent_review_at() == NOW
    assert run_review(context, NOW) == {"outcome": "already_sent"}
    # Successful first review consumes identity inventory without manufacturing
    # earlier carry counts; changed versions remain known the following morning.
    changed = replace(captured, id="brn_new_version", supersedes=(captured.id,), created_at=NOW + timedelta(hours=1),
                      updated_at=NOW + timedelta(hours=1))
    context = replace(context, sources=ReviewSources(brain_items=lambda: (ItemRecord(changed, "raw/version.md"),)))
    run_review(context, NOW + timedelta(days=1))
    assert not any("New, nothing needed" in row["question"] for row in recording.created[-1].packet.constraints["lines"])
    assert log.morning_bootstrap().known_first_ids == frozenset((carried.id,))
    log.close()


@pytest.mark.parametrize("failure", ["before_create", "lost_create_response", "record_run"])
def test_pending_morning_retries_minutes_later_after_restart_with_exact_packet(tmp_path, real_service, failure):
    from dataclasses import replace
    from brain_core.timewords import today_in_singapore
    from gremlin_town.review.why_log import WhyLogError

    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    item = make_item(id="brn_one", title="One", created_at=NOW - timedelta(hours=1))
    entry = ItemRecord(item, "raw/one.md")
    folder = tmp_path / "spool"
    folder.mkdir()

    class Delivery:
        commands = []
        accepted_ids = []

        def requests_create(self, actor, command):
            self.commands.append(command)
            first = len(self.commands) == 1
            if first and failure == "before_create":
                raise RuntimeError("fixture before create")
            receipt = real_service.requests_create(actor, command)
            self.accepted_ids.append(receipt.request_id)
            if first and failure == "lost_create_response":
                raise RuntimeError("fixture accepted response lost")
            return receipt

    delivery = Delivery()
    settings = replace(enabled_settings(str(path)), telegram_spool_dir=str(folder))
    context = ReviewContext(settings, log, ReviewSources(brain_items=lambda: (entry,)), delivery)
    if failure == "record_run":
        with log._connection:
            log._connection.execute("""CREATE TRIGGER fail_run BEFORE INSERT ON runs
                WHEN NEW.kind='review' AND NEW.outcome='sent'
                BEGIN SELECT RAISE(ABORT, 'fixture disk hiccup after request acceptance'); END""")
    with pytest.raises(WhyLogError if failure == "record_run" else RuntimeError):
        run_review(context, NOW)
    frozen = log.morning_packet(today_in_singapore(NOW))
    assert frozen is not None and not frozen.completed
    assert frozen.prepared_at == NOW and frozen.references == log.review_references(NOW)
    assert not log.morning_bootstrap_completed() and log.morning_history() == ()
    assert log.shown_item_refs() == frozenset() and list(folder.iterdir()) == []
    original_rows = log.history()
    log.close()

    # Simulate a new process and a real capture between the attempts. The retry
    # must not recollect it or generate another why-log reference batch.
    log = WhyLog(path)
    if failure == "record_run":
        with log._connection:
            log._connection.execute("DROP TRIGGER fail_run")
    arrived = make_item(id="brn_arrived", title="Captured between attempts", created_at=NOW + timedelta(minutes=2))

    def forbidden():
        raise AssertionError("a pending morning must reuse its snapshot rather than recollect")

    context = replace(context, why_log=log, sources=ReviewSources(brain_items=forbidden))
    result = run_review(context, NOW + timedelta(minutes=7))
    assert result["outcome"] == "sent"
    assert len(delivery.commands) == 2 and delivery.commands[0] == delivery.commands[1]
    assert set(delivery.accepted_ids) == {result["request_id"]}  # one user request, GT's real replay
    completed = log.morning_packet(today_in_singapore(NOW))
    assert completed.completed and completed.request == frozen.request and completed.references == frozen.references
    assert completed.prepared_at == NOW and log.last_sent_review_at() == NOW
    assert log.history() == original_rows and log.review_references(NOW + timedelta(minutes=7)) == ()
    assert log.morning_bootstrap_completed() and arrived.id not in log.shown_item_refs()
    for row in frozen.request["constraints"]["lines"]:
        saved = log.answer_context(row["reference"])
        if row["kind"] == "add_prompt":
            assert saved is None and row["reference"] is row["item_ref"] is None
            assert row["number"] == 0 and row["allowed_answers"] == row["offered"] == []
        else:
            assert saved.question == row["question"] and saved.offered == tuple(row["offered"])
    spool_file = folder / f"review-{today_in_singapore(NOW).isoformat()}.txt"
    assert spool_file.read_text(encoding="utf-8") == frozen.short
    assert "## Look" not in frozen.short and "## Look" in frozen.request["text"]
    assert run_review(context, NOW + timedelta(minutes=9)) == {"outcome": "already_sent"}
    assert len(delivery.commands) == 2 and list(folder.iterdir()) == [spool_file]

    context = replace(context, sources=ReviewSources(brain_items=lambda: (entry, ItemRecord(arrived, "raw/arrived.md"))))
    run_review(context, NOW + timedelta(days=1))
    new_rows = [row for row in delivery.commands[-1].packet.constraints["lines"]
                if "New, nothing needed" in row["question"]]
    assert [row["item_ref"] for row in new_rows] == [arrived.id]
    log.close()


@pytest.mark.parametrize("empty", [False, True], ids=["unavailable-lines", "no-lines"])
def test_pending_morning_preserves_line_based_emptiness_after_restart(tmp_path, real_service, monkeypatch, empty):
    from dataclasses import replace
    from brain_core.review import MORNING_PARTS, Morning, Review
    from brain_core.timewords import today_in_singapore
    import gremlin_town.review.run as runner

    if empty:
        # An explicit pure packet with four empty parts; no unavailable lines.
        monkeypatch.setattr(runner, "build_morning", lambda inputs: Morning(
            "Nothing here today.", tuple(Review((), (part,)) for part in MORNING_PARTS)))
    path = tmp_path / "why.sqlite3"
    log = fixture_log(path)

    class LostResponse:
        commands = []
        accepted_ids = []

        def requests_create(self, actor, command):
            self.commands.append(command)
            receipt = real_service.requests_create(actor, command)
            self.accepted_ids.append(receipt.request_id)
            if len(self.commands) == 1:
                raise RuntimeError("fixture accepted response lost")
            return receipt

    delivery = LostResponse()
    context = ReviewContext(enabled_settings(str(path)), log, ReviewSources(), delivery)
    with pytest.raises(RuntimeError, match="fixture accepted response lost"):
        run_review(context, NOW)
    frozen = log.morning_packet(today_in_singapore(NOW))
    assert frozen is not None and not frozen.completed
    assert frozen.is_empty is empty
    lines = frozen.request["constraints"]["lines"]
    if empty:
        assert lines == [] and frozen.references == ()
    else:
        assert [line["question"] for line in lines if "Couldn't see" in line["question"]] == [
            "Couldn't see brain: not_configured", "Couldn't see calendar: not_configured",
            "Couldn't see requests: not_configured",
        ]
        assert all(row.record_path is None and row.record_sha256 == "" for row in log.history())
    assert not log.morning_bootstrap_completed() and log.morning_history() == ()
    log.close()

    def forbidden(*args):
        raise AssertionError("retry must not rebuild its saved emptiness or content")

    log = WhyLog(path)
    monkeypatch.setattr(runner, "build_morning", forbidden)
    context = replace(context, why_log=log, sources=ReviewSources(brain_items=forbidden))
    result = run_review(context, NOW + timedelta(minutes=7))
    assert result["outcome"] == "sent" and result["is_empty"] is empty
    assert result["line_count"] == len(frozen.references)
    saved = log.morning_packet(today_in_singapore(NOW))
    assert saved.completed and saved.is_empty is empty
    assert saved.request == frozen.request and saved.references == frozen.references
    assert delivery.commands[0] == delivery.commands[1]
    assert set(delivery.accepted_ids) == {result["request_id"]}
    assert log.morning_bootstrap_completed() and log.last_sent_review_at() == NOW
    log.close()


def test_a_proved_result_is_seen_once_in_the_next_review(tmp_path):
    from tests.review.test_request_closed import Hub, context_for, AT_0900, NEXT_0600
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    context, recording, _ = context_for(hub, tmp_path)
    hub.at(AT_0900).return_result(request_id, proof="https://example.test/deck")
    run_review(context, hub.at(NEXT_0600).clock.now)
    assert 'back, with proof: "https://example.test/deck"' in recording.texts_to("hermes-wa")[-1]
    run_review(context, hub.at(NEXT_0600 + timedelta(days=1)).clock.now)
    assert "back, with proof" not in recording.texts_to("hermes-wa")[-1]


@pytest.mark.parametrize("proof,rule,words", [
    ("https://example.test/deck", "request_returned_proof", 'back, with proof: "https://example.test/deck"'),
    (None, "stale_request", "stale (with fleet-boss)"),
])
def test_request_review_consumption_survives_next_day_restart_and_ninety_day_pruning(tmp_path, proof, rule, words):
    from dataclasses import replace
    from tests.review.test_request_closed import Hub, context_for, AT_0900

    hub = Hub(tmp_path)
    context, recording, why_log = context_for(hub, tmp_path)
    first = datetime(2026, 10, 15, 22, tzinfo=timezone.utc)  # Friday, 06:00 SGT; idle over 14 days
    try:
        request_id = hub.hand_off("deck")
        if proof is not None:
            hub.at(AT_0900).return_result(request_id, proof=proof)
        original_state = hub.item("chatgpt-web", request_id)["work_state"]
        assert run_review(context, hub.at(first).clock.now)["outcome"] == "sent"
        assert words in recording.texts_to("hermes-wa")[-1]
        assert len([row for row in why_log.full_reason(request_id) if row.rule == rule]) == 1

        run_review(context, hub.at(first + timedelta(days=1)).clock.now)
        assert words not in recording.texts_to("hermes-wa")[-1]
        why_log.close()
        why_log = fixture_log(tmp_path / "why.sqlite3")
        context = replace(context, why_log=why_log)

        # Another Friday after retention expires. The request is still eligible at
        # the runtime reader, but its once-only review line must stay consumed.
        later = first + timedelta(days=98)
        hub.at(later)
        assert request_id in {request.id for request in context.sources.open_requests()}
        run_review(context, later)
        assert words not in recording.texts_to("hermes-wa")[-1]
        assert why_log.full_reason(request_id) == ()
        assert len([row for row in why_log.request_review_history()
                    if row.item_ref == request_id and row.rule == rule]) == 1
        item = hub.item("chatgpt-web", request_id)
        assert item["proof"] == proof
        assert item["work_state"] == original_state
    finally:
        why_log.close()
        hub.service.store.close()


@pytest.mark.parametrize("proof,rule,words", [
    ("https://example.test/deck", "request_returned_proof", "back, with proof"),
    (None, "stale_request", "stale (with fleet-boss)"),
])
def test_aborted_review_send_does_not_consume_proof_or_stale_line(tmp_path, proof, rule, words):
    from dataclasses import replace
    from tests.review.test_request_closed import Hub, context_for, AT_0900

    class FailedDelivery:
        def requests_create(self, actor, command):
            raise RuntimeError("fixture send aborted")

    hub = Hub(tmp_path)
    context, recording, why_log = context_for(hub, tmp_path)
    first = datetime(2026, 10, 15, 22, tzinfo=timezone.utc)
    try:
        request_id = hub.hand_off("deck")
        if proof is not None:
            hub.at(AT_0900).return_result(request_id, proof=proof)
        hub.at(first)
        with pytest.raises(RuntimeError, match="fixture send aborted"):
            run_review(replace(context, service=FailedDelivery()), first)
        assert any(row.rule == rule for row in why_log.full_reason(request_id))
        assert not any(row.item_ref == request_id for row in why_log.request_review_history())

        # Reopen between the aborted attempt and retry, so startup also must not
        # turn an undelivered surfacing into a consumed marker.
        why_log.close()
        why_log = fixture_log(tmp_path / "why.sqlite3")
        context = replace(context, why_log=why_log)
        assert run_review(context, first)["outcome"] == "sent"
        assert words in recording.texts_to("hermes-wa")[-1]
        assert len([row for row in why_log.request_review_history()
                    if row.item_ref == request_id and row.rule == rule]) == 1
    finally:
        why_log.close()
        hub.service.store.close()


@pytest.mark.parametrize("rule", ["request_returned_proof", "stale_request"])
def test_request_consumption_requires_logged_line_and_preserves_existing_sent_lines(tmp_path, rule):
    why_log = WhyLog(tmp_path / "why.sqlite3")
    try:
        why_log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent")
        assert why_log.request_review_history() == ()  # no candidate-only consumption
        surfaced_at = NOW + timedelta(days=1)
        why_log.record_surfacings([Surfacing(
            surfaced_at=surfaced_at, channel="review", item_ref="req_fixture",
            record_sha256="", rule=rule, fields_used={},
        )])
        assert why_log.request_review_history() == ()
        why_log.record_run(kind="review", started_at=surfaced_at, last_event_cursor=None, outcome="sent")
        # Model an older log that has sent surfacings but no W4 marker yet.
        with why_log._connection:
            why_log._connection.execute("DELETE FROM request_review_consumption")
        why_log.close()
        why_log = WhyLog(tmp_path / "why.sqlite3")
        why_log.prune(surfaced_at + timedelta(days=98))
        assert why_log.history() == ()
        (consumed,) = why_log.request_review_history()
        assert (consumed.item_ref, consumed.rule, consumed.surfaced_at) == ("req_fixture", rule, surfaced_at)
    finally:
        why_log.close()


def test_the_hubs_deliveries_close_on_sent_file_and_day_settled_only(tmp_path, real_service):
    from dataclasses import replace
    from gremlin_town.review.deliver import deliver
    from gremlin_town.review.run import close_hub_deliveries
    from gremlin_town.review.poll import poll_once
    from gremlin_town.domain import GremlinTownError
    spool = tmp_path / "spool"
    (spool / "sent").mkdir(parents=True)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    settings = replace(enabled_settings(str(tmp_path / "why.sqlite3")), telegram_spool_dir=str(spool))
    context = ReviewContext(settings, why_log, ReviewSources(), real_service)
    moment = deliver(real_service, sender_participant="exosuit-review", delivery_participant="hermes-wa",
                     project="vault_home", client_operation_id="moment-pc_on-hermes-wa-test", objective="Return", text="Back")
    review = deliver(real_service, sender_participant="exosuit-review", delivery_participant="hermes-wa",
                     project="vault_home", client_operation_id="review-2026-09-26", objective="Review", text="Pick today")
    assert close_hub_deliveries(context, NOW) == 0
    with pytest.raises(GremlinTownError):
        real_service.close_hub_delivery(Actor("hermes-wa"), moment.request_id,
                                       hub_participant="exosuit-review", proof="sent/fake.txt")
    # Even the hub cannot close another requester's work.
    other = real_service.requests_create(Actor("chatgpt-web"), CreateRequest(
        "review-other", RequestMode.ACTION_REQUESTED, "vault_home", "fleet-boss",
        Packet(1, MessageKind.REQUEST, None, "Other", "", {}, (), "", "", "private"),
    ))
    with pytest.raises(GremlinTownError):
        real_service.close_hub_delivery(Actor("exosuit-review"), other.request_id,
                                       hub_participant="exosuit-review", proof="sent/fake.txt")
    (spool / "sent" / "moment-pc_on-hermes-wa-test.txt").write_text("Back")
    disabled = replace(context, settings=replace(settings, enabled=False))
    assert poll_once(disabled, NOW) == ()
    assert real_service.store.fetchone("SELECT COUNT(*) FROM events WHERE event_type='hub_delivery_closed'")[0] == 0
    # No PC, task-finished or calendar signal qualifies. The poll still closes sent work.
    assert poll_once(context, NOW) == ()
    open_ids = {item["request_id"] for item in real_service.requests_list(Actor("exosuit-review"), {"open": True}).items}
    assert moment.request_id not in open_ids and review.request_id in open_ids
    why_log.record_signal(kind="day_settled", actor="local-codex", at=NOW)
    assert poll_once(context, NOW) == ()
    assert close_hub_deliveries(context, NOW) == 0
    assert len(real_service.store.fetchall("SELECT * FROM events WHERE event_type='hub_delivery_closed'")) == 2


def disabled_settings() -> ReviewSettings:
    return ReviewSettings.from_table(None)


def enabled_settings(why_log_file: str) -> ReviewSettings:
    return ReviewSettings.from_table({
        "enabled": True,
        "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa",
        "project": "vault_home",
        "why_log_file": why_log_file,
    })


class _AssertNeverCalled:
    def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("must not be called while [review] is disabled")

    def requests_create(self, actor: Actor, command: CreateRequest) -> object:
        raise AssertionError("must not send while [review] is disabled")


def test_disabled_by_default_reads_and_sends_nothing() -> None:
    guard = _AssertNeverCalled()
    context = ReviewContext(
        settings=disabled_settings(),
        why_log=None,
        sources=ReviewSources(brain_items=guard, calendar_events=guard, open_requests=guard),
        service=guard,
    )
    assert run_review(context, NOW) == {"outcome": "disabled"}
    assert run_moments(context, NOW, "pc_on") == {"outcome": "disabled"}
    assert run_moments(context, NOW, "pc_on", also_notify="local-codex") == {"outcome": "disabled"}


def test_morning_rows_pin_raw_version_and_exact_questions_before_sender(tmp_path, real_service):
    from dataclasses import replace
    from gremlin_town.review.run import TelegramMorningSender

    why_log = fixture_log(tmp_path / "why.sqlite3")
    # These IDs must be excluded by the public timestamp/channel read seam.
    why_log.record_surfacings([
        Surfacing(NOW - timedelta(seconds=1), "review", "brn_prior", "", "question", {}, question="Old?"),
        Surfacing(NOW, "weekly", "brn_weekly", "", "question", {}, question="Weekly?"),
    ])
    shown = replace(make_item(id="brn_v2", title="Venue rental permit"), supersedes=("brn_first",),
                    data={"level": "accepted", "review_marks": [{"mark": "question",
                          "text": "Three parts: venue appointment; inspector's approval; co-organizer's signature. Yes?"}]})
    entry = ItemRecord(shown, "records/current-v2.md")

    class Sender:
        calls = 0

        def send(self, context, morning, references, **kwargs):
            self.calls += 1
            assert references == why_log.review_references(NOW)
            rows = [line for part in morning.parts for line in part.lines]
            prompts = [line for line in rows if line.item_ref is None]
            assert len(prompts) == 1 and prompts[0].rule == "add_prompt"
            assert len(references) == len(rows) - 1
            for reference, line in zip(references, (line for line in rows if line.item_ref is not None), strict=True):
                saved = why_log.answer_context(reference)
                assert saved.question == line.text and saved.offered == line.offered
                assert saved.thing == line.item_ref
            assert all(why_log.answer_context(ref).thing not in {"brn_prior", "brn_weekly"} for ref in references)
            needs = next(line for line in rows if line.rule == "needs_you")
            assert needs.item_ref == "brn_first" and needs.record_sha256 == shown.sha256
            assert needs.record_path == entry.path and "parts" in needs.offered
            return TelegramMorningSender().send(context, morning, references, **kwargs)

    sender = Sender()
    recording = _CapturingRequestCreator(real_service)
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                            ReviewSources(brain_items=lambda: (entry,)), recording, morning_sender=sender)
    result = run_review(context, NOW)
    assert result["outcome"] == "sent" and sender.calls == 1
    packet = recording.created[0].packet
    refs = packet.constraints["lines"]
    for row in refs:
        saved = why_log.answer_context(row["reference"])
        if row["kind"] == "add_prompt":
            assert saved is None and row["reference"] is row["item_ref"] is None
            assert row["number"] == 0 and row["allowed_answers"] == row["offered"] == []
        else:
            assert saved.question == row["question"] and saved.offered == tuple(row["offered"])
    assert "venue appointment; inspector's approval; co-organizer's signature" in packet.context
    assert run_review(context, NOW) == {"outcome": "already_sent"} and sender.calls == 1
    why_log.close()


def test_r17_moved_over_morning_has_exact_day_picks_side_refs_and_no_blocking_duplicates(
    tmp_path, real_service, moved_over,
):
    from brain_core import work
    from gremlin_town.review.run import TelegramMorningSender

    now = datetime(2026, 9, 30, 22, tzinfo=timezone.utc)
    entries = tuple(ItemRecord(record, f"raw/{record.id}.md") for record in moved_over)
    before = [record.as_dict() for record in moved_over]
    log = WhyLog(tmp_path / "why.sqlite3")
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids={work.first_id(record) for record in moved_over})
    assert log.morning_history() == () and log.last_sent_review_at() is None
    index = work.index_things(moved_over)

    class Sender:
        def send(self, context, morning, references, **kwargs):
            rows = [line for part in morning.parts for line in part.lines]
            picks = [line for line in rows if line.rule == "your_day"]
            assert {line.item_ref for line in picks} == {
                "brn_20260929_123409_d00000000000000e", "brn_20260929_123405_d00000000000000d"}
            assert len([line for line in rows if line.rule == "add_prompt"]) == 1
            for reference, line in zip(references, (line for line in rows if line.item_ref is not None), strict=True):
                saved = log.answer_context(reference)
                assert saved.question == line.text and saved.offered == line.offered
                if line.record_path:
                    raw = next(record for record in moved_over if record.id == index[line.item_ref].id)
                    assert line.record_path == f"raw/{raw.id}.md" and line.record_sha256 == raw.sha256
            for pick in picks:
                question = next(line for line in morning.parts[2].lines if line.item_ref == pick.item_ref)
                assert pick.text.endswith(f"side question, see {question.number}")
                assert not index[pick.item_ref].data["stake"]["confirmed"]
            return TelegramMorningSender().send(context, morning, references, **kwargs)

    recording = _CapturingRequestCreator(real_service)
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), log,
        ReviewSources(brain_items=lambda: entries), recording, morning_sender=Sender())
    try:
        assert run_review(context, now)["outcome"] == "sent"
        assert log.morning_bootstrap_completed() and before == [record.as_dict() for record in moved_over]
        assert run_review(context, now) == {"outcome": "already_sent"}
        assert len(recording.created) == 1
    finally:
        log.close()


def test_actual_logged_question_answer_releases_current_work_but_not_a_new_revision(tmp_path, real_service):
    from dataclasses import replace
    from brain_core.review import build_morning
    from gremlin_town.api.review import record_review_response
    from gremlin_town.review.collect import collect_morning_inputs

    record = replace(make_item(id="brn_question", title="Confirm the parts"), data={
        "level": "accepted", "review_marks": [{"mark": "question", "text": "Parts: call; sign. Yes?"}]})
    log = fixture_log(tmp_path / "why.sqlite3")
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    recording = _CapturingRequestCreator(real_service)
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), log,
        ReviewSources(brain_items=lambda: (ItemRecord(record, "raw/original.md"),)), recording)
    try:
        run_review(context, NOW)
        questions = recording.created[-1].packet.constraints["lines"]
        question = next(line for line in questions if line["item_ref"] == record.id)
        assert question["offered"] == ["answer", "leave it", "parts"]
        assert not any(line["part"] == "your_day" and line["item_ref"] == record.id for line in questions)
        record_review_response(log, caller="hermes-wa", item_refs=[question["reference"]],
            response="answered", response_words="yes, those parts", now=NOW)
        def picture(sources):
            return build_morning(collect_morning_inputs(sources, log.morning_history(), NOW,
                bootstrap=log.morning_bootstrap(), shown_item_refs=log.shown_item_refs(),
                previous_review_at=log.last_sent_review_at()))
        released = picture(context.sources)
        assert any(line.item_ref == record.id for line in released.parts[3].lines)
        revised = replace(record, id="brn_question_v2", supersedes=(record.id,), updated_at=NOW + timedelta(minutes=1))
        changed = picture(ReviewSources(brain_items=lambda: (
            ItemRecord(record, "raw/original.md"), ItemRecord(revised, "raw/revised.md"))))
        pending = next(line for line in changed.parts[2].lines if line.item_ref == record.id)
        assert pending.record_path == "raw/revised.md" and pending.record_sha256 == revised.sha256
        assert not any(line.item_ref == record.id for line in changed.parts[3].lines)
    finally:
        log.close()


@pytest.mark.parametrize("earlier_count", [1, 10])
def test_current_day_side_question_uses_posted_number_after_earlier_answer(tmp_path, real_service, earlier_count):
    from dataclasses import replace
    from gremlin_town.api.review import record_review_response
    from gremlin_town.review.run import current_review_part

    earlier = tuple(replace(make_item(id=f"brn_earlier_{i}", title=f"Earlier {i}"), data={
        "level": "accepted", "review_marks": [{"mark": "question", "text": "Keep these parts?"}]})
        for i in range(earlier_count))
    side = replace(make_item(id="brn_side", title="Side"), data={
        "level": "accepted", "review_marks": [{"mark": "ask_stake", "text": "What's at stake?"}]})
    records = (*earlier, side)
    log = WhyLog(tmp_path / "why.sqlite3")
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=tuple(record.id for record in records))
    recording = _CapturingRequestCreator(real_service)
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), log,
        ReviewSources(brain_items=lambda: tuple(ItemRecord(record, f"raw/{record.id}.md") for record in records)),
        recording)
    try:
        run_review(context, NOW)
        original = recording.created[-1].packet.constraints["lines"]
        posted = [line for line in original if line["part"] == "new" and line["kind"] == "needs_you"]
        question = next((line for line in posted if line["item_ref"] == side.id), None)
        if earlier_count == 1:
            assert question is not None and question["number"] > posted[0]["number"]
        else:
            assert question is None  # Outside the original New cap; answering exposes it in the rebuild.
        record_review_response(log, caller="hermes-wa", item_refs=[posted[0]["reference"]],
            response="answered", response_words="yes", now=NOW + timedelta(minutes=1))

        rebuilt = current_review_part(context, NOW + timedelta(minutes=2), caller="hermes-wa")
        pick = next(line for line in rebuilt["lines"] if line["item_ref"] == side.id)
        suffix = (f"side question, see {question['number']}" if question is not None
                  else "side question in New; ask to see more")
        assert pick["text"].endswith(suffix)
        assert f"{pick['number']}. {pick['text']}" in rebuilt["text"]
        assert log.answer_context(pick["reference"]).question == pick["text"]
        assert recording.created[-1].packet.constraints["lines"] == original
    finally:
        log.close()


def test_morning_request_has_ten_recent_raw_confirmed_stake_examples(tmp_path, real_service):
    from dataclasses import replace

    records = tuple(ItemRecord(replace(make_item(id=f"brn_{i:02}", title=f"Call {i}"),
        updated_at=NOW - timedelta(minutes=i), data={"level": "accepted", "stake": {
            "level": "nuisance", "at_stake": f"Cost {i}", "confirmed": i != 0, "proposed_by": "nic"}}),
        f"records/{i}.md") for i in range(13))
    why_log = fixture_log(tmp_path / "why.sqlite3")
    recording = _CapturingRequestCreator(real_service)
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                            ReviewSources(brain_items=lambda: records), recording)
    run_review(context, NOW)
    examples = recording.created[0].packet.constraints["stake_examples"]
    assert [row["item_ref"] for row in examples] == [f"brn_{i:02}" for i in range(1, 11)]
    assert [row["stake"] for row in examples] == [row.record.data["stake"] for row in records[1:11]]
    why_log.close()


def test_last_successful_review_timestamp_ignores_aborted_and_other_runs(tmp_path):
    from gremlin_town.review.why_log import WhyLogError

    why_log = WhyLog(tmp_path / "why.sqlite3")
    assert why_log.last_sent_review_at() is None
    previous = NOW - timedelta(days=2)
    older_offset = (previous - timedelta(hours=1)).astimezone(timezone(timedelta(hours=8)))
    why_log.record_run(kind="review", started_at=previous, last_event_cursor=None, outcome="sent")
    why_log.record_run(kind="review", started_at=older_offset, last_event_cursor=None, outcome="sent")
    for kind, outcome in (("review", "aborted"), ("review", "failed"), ("weekly", "sent"), ("moment", "sent")):
        why_log.record_run(kind=kind, started_at=NOW, last_event_cursor=None, outcome=outcome)
    why_log.record_surfacings([Surfacing(NOW, "review", "brn_aborted", "", "question", {})])
    assert why_log.last_sent_review_at() == previous
    why_log.prune(NOW + timedelta(days=98))
    assert why_log.last_sent_review_at() == previous
    why_log.close()
    with pytest.raises(WhyLogError, match="last successful review run"):
        why_log.last_sent_review_at()


def test_morning_uses_actual_sent_old_review_before_moveover(tmp_path, real_service, moved_over):
    from dataclasses import replace
    from brain_core import work
    from brain_core.records import parse_record
    from brain_core.review import build_review
    from gremlin_town.review.collect import collect_review_inputs

    now = datetime(2026, 9, 30, 22, tzinfo=timezone.utc)  # labelled fixture: 1 Oct 06:00 SGT
    previous = now - timedelta(hours=17)  # labelled synthetic: 30 Sep 13:00 SGT, before fixture migration
    fixture_root = Path(__file__).resolve().parents[3] / "brain-core/tests/fixtures/work_model/audited"
    originals = tuple(ItemRecord(parse_record(path.read_text(encoding="utf-8"), str(path)), str(path))
                      for path in sorted((fixture_root / "records/item").rglob("*.md")))
    why_log = WhyLog(tmp_path / "why.sqlite3")
    try:
        old = build_review(collect_review_inputs(ReviewSources(brain_items=lambda: originals), (), previous))
        assert old.lines
        why_log.record_surfacings(Surfacing(
            surfaced_at=previous, channel="review", item_ref=line.item_ref, record_sha256=line.record_sha256 or "",
            rule=line.rule, fields_used=line.fields_used, record_path=line.record_path, record_state=line.record_state,
            question=line.text, offered=line.offered,
        ) for line in old.lines)
        why_log.record_run(kind="review", started_at=previous, last_event_cursor=None, outcome="sent")
        # A later aborted surfacing/run and weekly send must not replace that cutoff.
        why_log.record_surfacings([Surfacing(now - timedelta(hours=1), "review", "__aborted__", "", "fixture", {})])
        why_log.record_run(kind="review", started_at=now - timedelta(hours=1), last_event_cursor=None, outcome="aborted")
        why_log.record_run(kind="weekly", started_at=now - timedelta(minutes=30), last_event_cursor=None, outcome="sent")
        assert why_log.last_sent_review_at() == previous

        captured = make_item(id="brn_20260930_200000_000000000000c101", title="True capture after review",
                             created_at=now - timedelta(hours=2))
        existing = make_item(id="brn_20260928_220000_000000000000c102", title="Existing thing",
                             created_at=now - timedelta(days=2))
        successor = replace(existing, id="brn_20260930_203000_000000000000c103", supersedes=(existing.id,),
                            created_at=now - timedelta(minutes=90), updated_at=now - timedelta(minutes=90))
        why_log.stage_morning_bootstrap(input_sha="b" * 40,
            first_ids={work.first_id(record) for record in (*moved_over, existing, *(entry.record for entry in originals))})
        records = (*moved_over, existing, successor, captured)
        before = [record.as_dict() for record in records]
        entries = tuple(ItemRecord(record, f"records/{record.id}.md") for record in records)
        recording = _CapturingRequestCreator(real_service)
        context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                                ReviewSources(brain_items=lambda: entries), recording)
        assert run_review(context, now)["outcome"] == "sent"
        rows = recording.created[-1].packet.constraints["lines"]
        clear = [row for row in rows if "New, nothing needed" in row["question"]]
        assert [row["item_ref"] for row in clear] == [captured.id]
        index = work.index_things(records)
        for first in ("brn_20260929_105119_d000000000000008", "brn_20260929_105126_d00000000000000a"):
            row = next(row for row in rows if row["item_ref"] == first and row["part"] == "new")
            assert "For your review" in row["question"]
            saved = next(saved for saved in why_log.full_reason(first) if saved.surfaced_at == now and saved.rule == "needs_you")
            raw = next(record for record in records if record.id == index[first].id)
            assert saved.record_path == f"records/{raw.id}.md" and saved.record_sha256 == raw.sha256
        assert before == [record.as_dict() for record in records]
        assert why_log.last_sent_review_at() == now
    finally:
        why_log.close()


def make_item(
    *,
    id: str,
    title: str,
    timing: dict | None = None,
    state: str = "open",
    level: str = "accepted",
    parked_until: str | None = None,
    source: dict | None = None,
    links: tuple[dict, ...] = (),
    created_at: datetime = NOW,
    block: str | None = "admin",
) -> Record:
    """``block`` defaults to a sorted task (PRD §4.1), so "To sort" stays out of other tests."""

    data: dict = {"level": level, "size": "task"}
    if block is not None:
        data["block"] = block
    if timing is not None:
        data["timing"] = timing
    if parked_until is not None:
        data["parked_until"] = parked_until
    return Record(
        schema=RECORD_SCHEMA_V2,
        id=id,
        kind="item",
        title=title,
        state=state,
        created_at=created_at,
        updated_at=created_at,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=(),
        supersedes=(),
        dedupe_key=None,
        data=data,
        body="",
        source=source,
        links=links,
    )


@pytest.fixture
def real_service(tmp_path: Path) -> GremlinTownService:
    policy = Policy(
        participants=(
            Participant("chatgpt-web", "ChatGPT", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
            Participant("fleet-boss", "Fleet Boss", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
            Participant(
                "exosuit-review", "Exosuit review", "hub@example.test",
                frozenset({"vault_home"}), frozenset({"private"}), read_all_requests=True,
            ),
            Participant("hermes-wa", "Diablo", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
            Participant("local-codex", "Codex on the PC", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
        ),
        routes=frozenset({
            ("vault_home", "chatgpt-web", "fleet-boss", "private"),
            ("vault_home", "exosuit-review", "hermes-wa", "private"),
            ("vault_home", "exosuit-review", "local-codex", "private"),
        }),
    )
    store = SQLiteStore(tmp_path / "gremlin.db", now=lambda: NOW)
    return GremlinTownService(store, policy, now=lambda: NOW)


def test_review_run_sends_once_logs_before_sending_and_includes_open_request(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    # Nic's agent hands a request to fleet-boss; it is still open.
    real_service.requests_create(
        Actor(actor_id="chatgpt-web"),
        CreateRequest(
            client_operation_id="handoff-1",
            mode=RequestMode.ACTION_REQUESTED,
            project="vault_home",
            recipient="fleet-boss",
            packet=Packet(
                schema_version=1, kind=MessageKind.REQUEST, session_reference=None,
                objective="Draft the deck", context="", constraints={}, sources=(),
                next_action="Draft it", expected_reply="The draft", sensitivity="private",
            ),
        ),
    )

    due_today = ItemRecord(
        record=make_item(id="brn_invoice", title="Send invoice", timing={"due": "2026-09-26"}),
        path="records/item/2026/09/brn_invoice.md",
    )

    def open_requests():
        from brain_core.review import OpenRequest

        page = real_service.requests_list(Actor(actor_id="exosuit-review"), {"open": True})
        return tuple(
            OpenRequest(id=item["request_id"], handed_to=item["recipient_id"], since=date(2026, 9, 23))
            for item in page.items
        )

    why_log = fixture_log(tmp_path / "why.sqlite3")
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (due_today,), open_requests=open_requests),
        service=real_service,
    )

    result = run_review(context, NOW)
    assert result["outcome"] == "sent"
    assert result["is_empty"] is False
    history = why_log.history()
    assert any(row.item_ref == "brn_invoice" for row in history)
    assert not any(row.item_ref.startswith("req_") for row in history)  # full waiting stays weekly

    # A same-day rerun is a no-op: it never re-derives content or sends again.
    assert run_review(context, NOW) == {"outcome": "already_sent"}
    all_requests_from_review = real_service.requests_list(
        Actor(actor_id="exosuit-review", is_operator=False), {"recipient": "hermes-wa"}
    )
    assert len(all_requests_from_review.items) == 1
    why_log.close()


def test_moments_no_items_sends_no_request(tmp_path: Path, real_service: GremlinTownService) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(),
        service=real_service,
    )
    result = run_moments(context, NOW, "pc_on")
    assert result == {"outcome": "nothing", "signal": "pc_on"}
    assert real_service.requests_list(Actor(actor_id="exosuit-review"), {}).items == ()
    why_log.close()


def test_moments_notifies_both_damien_and_the_agent_nic_is_using(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    """D24 (2026-09-26 04:40): daytime returns go to both Diablo on Telegram and
    whichever agent Nic is using at the time; here that agent is `local-codex`."""

    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = ItemRecord(record=make_item(id="brn_admin", title="File the form"), path="records/item/2026/09/brn_admin.md")
    why_log.record_surfacings([
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW, channel="review", item_ref="brn_admin", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])
    why_log.record_response(item_ref="brn_admin", response="picked_today", response_words="do it", responded_at=NOW)

    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)),
        service=real_service,
    )
    result = run_moments(context, NOW, "pc_on", also_notify="local-codex")
    assert result["outcome"] == "sent"
    recipients = {entry["recipient"] for entry in result["deliveries"]}
    assert recipients == {"hermes-wa", "local-codex"}
    assert len(result["deliveries"]) == 2

    to_local_codex = real_service.requests_list(Actor(actor_id="exosuit-review"), {"recipient": "local-codex"})
    assert len(to_local_codex.items) == 1
    why_log.close()


def test_moments_also_notify_same_as_delivery_participant_sends_once(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = ItemRecord(record=make_item(id="brn_admin2", title="File the form"), path="records/item/2026/09/brn_admin2.md")
    why_log.record_surfacings([
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW, channel="review", item_ref="brn_admin2", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])
    why_log.record_response(item_ref="brn_admin2", response="picked_today", response_words="do it", responded_at=NOW)

    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)),
        service=real_service,
    )
    result = run_moments(context, NOW, "pc_on", also_notify="hermes-wa")
    assert len(result["deliveries"]) == 1
    why_log.close()


def test_moments_never_returns_an_unfinished_pick(tmp_path: Path, real_service: GremlinTownService) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = ItemRecord(record=make_item(id="brn_admin", title="File the form"), path="records/item/2026/09/brn_admin.md")

    # The review picked it today.
    why_log.record_surfacings([
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW,
            channel="review",
            item_ref="brn_admin",
            record_sha256=item.sha256,
            record_state="open",
            rule="pick_or_park",
            fields_used={},
            record_path=item.path,
        )
    ])
    why_log.record_response(item_ref="brn_admin", response="picked_today", response_words="do the form", responded_at=NOW)

    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)),
        service=real_service,
    )
    first = run_moments(context, NOW, "pc_on")
    assert first == {"outcome": "nothing", "signal": "pc_on"}

    second = run_moments(context, NOW, "calendar_gap")
    assert second == {"outcome": "nothing", "signal": "calendar_gap"}
    why_log.close()


class _CapturingService:
    """A minimal `deliver.RequestCreator`, to inspect the exact text sent (step F §8)."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def requests_create(self, actor, command):
        from gremlin_town.contracts import Receipt

        self.texts.append(command.packet.context)
        return Receipt(
            operation_id=command.client_operation_id,
            request_id=f"req-{len(self.texts)}",
            accepted_at=NOW,
            operation_state=None,
            work_state=None,
            revision=None,
            actor_id=actor.actor_id,
        )


def _orientation_context(why_log: WhyLog, *, open_notes=None):
    from gremlin_town.review.orient import OrientationContext, OrientationSources
    from gremlin_town.review.settings import OrientationSettings

    return OrientationContext(
        orientation_settings=OrientationSettings.from_table({"enabled": True}, review_enabled=True),
        review_settings=enabled_settings(":memory:"),
        why_log=why_log,
        sources=OrientationSources(review=ReviewSources(), open_notes=open_notes),
    )


def test_moment_message_carries_the_first_step_and_time_box_when_orientation_is_enabled(
    tmp_path: Path,
) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    from dataclasses import replace
    item = ItemRecord(
        record=make_item(id="brn_admin", title="File the form", timing={"padded_minutes": 90}),
        path="records/item/2026/09/brn_admin.md",
    )
    item = ItemRecord(replace(item.record, data={**item.record.data,
        "next_action": {"text": "Owner action: print the form", "done": False}}), item.path)
    why_log.record_surfacings([
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW, channel="review", item_ref="brn_admin", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])
    why_log.record_response(item_ref="brn_admin", response="picked_today", response_words="do it", responded_at=NOW)
    # Surfaced on 2 earlier days unchanged, so it tends to stall (step F spec §4).
    why_log.record_surfacings([
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW.replace(day=NOW.day - 1), channel="orient", item_ref="brn_admin",
            record_sha256=item.sha256, record_state="open", rule="oldest open", fields_used={},
        ),
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW.replace(day=NOW.day - 2), channel="orient", item_ref="brn_admin",
            record_sha256=item.sha256, record_state="open", rule="oldest open", fields_used={},
        ),
    ])

    service = _CapturingService()
    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)),
        service=service,
    )
    orientation = _orientation_context(why_log, open_notes=lambda: {"item-brn_admin": "drop the pricing into slide 6"})

    result = run_moments(context, NOW, "pc_on", orientation=orientation)
    assert result["outcome"] == "sent"
    assert "next: Owner action: print the form" in service.texts[0]
    assert "drop the pricing into slide 6" not in service.texts[0]
    assert "20 minutes, then stop" in service.texts[0]
    why_log.close()


def test_moment_message_is_the_same_whether_orientation_is_disabled_or_absent(tmp_path: Path) -> None:
    """Owner actions and time boxes do not depend on orientation; retired notes add nothing."""

    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = ItemRecord(record=make_item(id="brn_admin", title="File the form"), path="records/item/2026/09/brn_admin.md")
    why_log.record_surfacings([
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW, channel="review", item_ref="brn_admin", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])
    why_log.record_response(item_ref="brn_admin", response="picked_today", response_words="do it", responded_at=NOW)

    service = _CapturingService()
    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)),
        service=service,
    )

    without_orientation = run_moments(context, NOW, "pc_on")
    assert service.texts[0] == "1. File the form — no longer waiting"
    why_log.close()

    # Disabled orientation behaves exactly like no orientation at all.
    why_log2 = WhyLog(tmp_path / "why2.sqlite3")
    disabled = _orientation_context(why_log2)
    from dataclasses import replace as _replace
    from gremlin_town.review.settings import OrientationSettings

    disabled = _replace(disabled, orientation_settings=OrientationSettings.from_table(None, review_enabled=True))
    why_log2.record_surfacings([
        __import__("gremlin_town.review.why_log", fromlist=["Surfacing"]).Surfacing(
            surfaced_at=NOW, channel="review", item_ref="brn_admin", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])
    why_log2.record_response(item_ref="brn_admin", response="picked_today", response_words="do it", responded_at=NOW)
    context2 = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why2.sqlite3")),
        why_log=why_log2,
        sources=ReviewSources(brain_items=lambda: (item,)),
        service=service,
    )
    run_moments(context2, NOW, "pc_on", orientation=disabled)
    assert service.texts[1] == "1. File the form — no longer waiting"
    why_log2.close()


def _shown_waiting(why_log: WhyLog, item: ItemRecord) -> None:
    why_log.record_surfacings([
        Surfacing(
            surfaced_at=NOW, channel="review", item_ref=item.record.id, record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])


def test_a_return_finds_its_first_step_through_the_notes_task_link_and_marks_the_rest(tmp_path: Path) -> None:
    """Retired task-linked, old-key and request-note first steps never enrich a returned line."""

    from brain_core.review import OpenNote

    linked = ItemRecord(record=make_item(id="brn_linked", title="Book the flights"), path="records/item/2026/09/brn_linked.md")
    old_key = ItemRecord(record=make_item(id="brn_oldkey", title="Pay the fee"), path="records/item/2026/09/brn_oldkey.md")
    empty = ItemRecord(record=make_item(id="brn_empty", title="Call the bank"), path="records/item/2026/09/brn_empty.md")
    handed = ItemRecord(record=make_item(id="brn_handed", title="Draft the deck"), path="records/item/2026/09/brn_handed.md")
    bare = ItemRecord(record=make_item(id="brn_bare", title="File the form"), path="records/item/2026/09/brn_bare.md")
    items = (linked, old_key, empty, handed, bare)
    notes = (
        OpenNote(session_id="tokyo-trip", next_step="Compare two fares", updated_at=NOW, task_id="brn_linked"),
        OpenNote(session_id="item-brn_oldkey", next_step="Log into the portal", updated_at=NOW),
        OpenNote(session_id="item-brn_empty", next_step="", updated_at=NOW, task_id="brn_empty"),
        OpenNote(session_id="gt-req_0123", next_step="Result returned, not yet checked.", updated_at=NOW, task_id="brn_handed"),
    )
    why_log = WhyLog(tmp_path / "why.sqlite3")
    for item in items:
        _shown_waiting(why_log, item)
    service = _CapturingService()
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(
            brain_items=lambda: items,
            open_notes=lambda: notes,
        ),
        service=service,
    )
    assert run_moments(context, NOW, "pc_on")["outcome"] == "sent"
    lines = set(service.texts[0].split("\n"))
    assert {line.split(". ", 1)[1] for line in lines} == {
        "Book the flights — no longer waiting",
        "Pay the fee — no longer waiting",
        "Call the bank — no longer waiting",
        "Draft the deck — no longer waiting",
        "File the form — no longer waiting",
    }
    why_log.close()


def test_an_unreadable_notes_source_still_sends_the_return_marked(tmp_path: Path) -> None:
    item = ItemRecord(record=make_item(id="brn_admin", title="File the form"), path="records/item/2026/09/brn_admin.md")
    why_log = WhyLog(tmp_path / "why.sqlite3")
    _shown_waiting(why_log, item)

    def broken():
        raise RuntimeError("Brain Home unreachable")

    service = _CapturingService()
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,), open_notes=broken),
        service=service,
    )
    assert run_moments(context, NOW, "pc_on")["outcome"] == "sent"
    assert service.texts == ["1. File the form — no longer waiting"]
    why_log.close()


class _CapturingRequestCreator:
    """Delegates to a real service but keeps the exact `CreateRequest` sent."""

    def __init__(self, inner: GremlinTownService) -> None:
        self._inner = inner
        self.created: list[CreateRequest] = []

    def requests_create(self, actor: Actor, command: CreateRequest):
        self.created.append(command)
        return self._inner.requests_create(actor, command)


def test_six_am_review_has_every_section(tmp_path: Path, real_service: GremlinTownService) -> None:
    """Case 16: one fixture, one item per section, checked against exact golden text."""

    real_service.requests_create(
        Actor(actor_id="chatgpt-web"),
        CreateRequest(
            client_operation_id="handoff-golden",
            mode=RequestMode.ACTION_REQUESTED,
            project="vault_home",
            recipient="fleet-boss",
            packet=Packet(
                schema_version=1, kind=MessageKind.REQUEST, session_reference=None,
                objective="Draft the deck", context="", constraints={}, sources=(),
                next_action="Draft it", expected_reply="The draft", sensitivity="private",
            ),
        ),
    )
    handed_off_request_id = real_service.requests_list(
        Actor(actor_id="exosuit-review"), {"recipient": "fleet-boss"}
    ).items[0]["request_id"]

    due_item = make_item(id="brn_invoice", title="Send invoice", timing={"due": "2026-09-26"})
    window_item = make_item(
        id="brn_deck", title="Finish the deck", timing={"window": {"start": "2026-09-26", "end": "2026-09-28"}}
    )
    untimed_item = make_item(id="brn_untimed", title="File the expenses")
    parked_item = make_item(id="brn_parked", title="Renew the domain", state="parked", parked_until="2026-09-26")
    suggestion_item = make_item(
        id="brn_suggest", title="Learn the new CRM", level="suggested", source={"speaker": "fleet-boss"}
    )
    waiting_item = make_item(
        id="brn_waiting", title="Get Sam's pricing", state="waiting", links=({"type": "waiting_on", "target": "Sam"},)
    )
    items = tuple(
        ItemRecord(record=record, path=f"records/item/2026/09/{record.id}.md")
        for record in (due_item, window_item, untimed_item, parked_item, suggestion_item, waiting_item)
    )
    # Already carries the exosuit markers (§5), so brn_invoice and brn_deck don't
    # also show up under "Calendar entries to add".
    calendar_event = CalendarEvent(
        title="Standup",
        start=datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 26, 1, 30, tzinfo=timezone.utc),
        description=f"{marker('brn_invoice')} {marker('brn_deck')}",
    )

    def open_requests() -> tuple[OpenRequest, ...]:
        page = real_service.requests_list(Actor(actor_id="exosuit-review"), {"open": True})
        return tuple(
            OpenRequest(
                id=item["request_id"], handed_to=item["recipient_id"], since=date(2026, 9, 23),
                objective=item["objective"],
            )
            for item in page.items
        )

    capturing_service = _CapturingRequestCreator(real_service)
    why_log = fixture_log(tmp_path / "why.sqlite3")
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(
            brain_items=lambda: items,
            calendar_events=lambda start, end: (calendar_event,),
            open_requests=open_requests,
        ),
        service=capturing_service,
    )

    result = run_review(context, NOW)
    assert result["outcome"] == "sent"
    assert len(capturing_service.created) == 1
    text = capturing_service.created[0].packet.context

    assert [line for line in text.splitlines() if line.startswith("## ")] == [
        "## Look", "## Catch up", "## New", "## Your day: pick 3 to 7",
    ]
    assert "Standup — at 9 am" in text and "Send invoice — deadline today" in text
    assert "Coming up: Finish the deck" in text
    assert "New, nothing needed" in text
    assert handed_off_request_id not in text
    assert not any(row.item_ref == handed_off_request_id for row in why_log.history())
    assert "Calendar entries to add" not in text and "## Waiting on" not in text and "## To sort" not in text
    why_log.close()


def test_parked_admin_task_shows_in_review_then_returns_only_at_a_moment(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    """A picked parked task and a renamed successor stay quiet until genuinely actionable."""

    at_0600 = NOW
    at_1000 = datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc)
    at_1200 = datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc)
    at_1500 = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)
    at_1600 = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)

    admin_task = make_item(
        id="brn_admin", title="Renew the business licence", state="parked", parked_until="2026-09-26",
    )
    admin_item = ItemRecord(record=admin_task, path="records/item/2026/09/brn_admin.md")

    why_log_path = tmp_path / "why.sqlite3"
    why_log = fixture_log(why_log_path)

    # Work-02 follows the shared reader: parked work stays outside the new morning.
    review_context = ReviewContext(
        settings=enabled_settings(str(why_log_path)),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (admin_item,)),
        service=real_service,
    )
    review_result = run_review(review_context, at_0600)
    assert review_result["outcome"] == "sent"
    assert not any(row.item_ref == "brn_admin" for row in why_log.history())
    # Preserve the daytime regression over an existing work-01 surfacing. Its
    # historical picked/waiting row remains readable after the morning changes.
    why_log.record_surfacings([Surfacing(
        surfaced_at=at_0600, channel="review", item_ref="brn_admin", record_sha256=admin_item.sha256,
        record_path=admin_item.path, record_state="parked", rule="pick_or_park", fields_used={},
    )])

    # Nic picks it during the review.
    why_log.record_response(
        item_ref="brn_admin", response="picked_today", response_words="I'll renew it today", responded_at=at_0600,
    )

    moments_context = ReviewContext(
        settings=review_context.settings,
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (admin_item,)),
        service=real_service,
    )

    # 10:00, pc_on: returns once.
    first = run_moments(moments_context, at_1000, "pc_on")
    assert first == {"outcome": "nothing", "signal": "pc_on"}

    # 12:00, calendar_gap, and 15:00, task_finished: nothing changed, so nothing sent.
    assert run_moments(moments_context, at_1200, "calendar_gap") == {"outcome": "nothing", "signal": "calendar_gap"}
    assert run_moments(moments_context, at_1500, "task_finished") == {"outcome": "nothing", "signal": "task_finished"}

    # The record changes (Nic edits the title); the next signal returns it again.
    changed_task = make_item(
        id="brn_admin", title="Renew the business licence before Friday",
        state="parked", parked_until="2026-09-26",
    )
    changed_item = ItemRecord(record=changed_task, path="records/item/2026/09/brn_admin.md")
    assert changed_item.sha256 != admin_item.sha256
    changed_context = ReviewContext(
        settings=review_context.settings,
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (changed_item,)),
        service=real_service,
    )
    again = run_moments(changed_context, at_1600, "task_finished")
    assert again == {"outcome": "nothing", "signal": "task_finished"}

    why_log.close()


def test_moment_with_nothing_to_return_sends_nothing(tmp_path: Path, real_service: GremlinTownService) -> None:
    """Capability 10: a signal with no candidates at all sends nothing."""

    why_log = WhyLog(tmp_path / "why.sqlite3")
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(),
        service=real_service,
    )
    assert run_moments(context, NOW, "task_finished") == {"outcome": "nothing", "signal": "task_finished"}
    assert real_service.requests_list(Actor(actor_id="exosuit-review"), {}).items == ()
    why_log.close()


def test_moments_fixture_memo_uses_current_raw_pins_parent_progress_and_owner_action(tmp_path, moved_over):
    from dataclasses import replace
    from brain_core import work

    first = "brn_20260930_035041_d000000000000029"
    current = work.index_things(moved_over)[first]
    for n in range(3):
        current = replace(current, id=f"brn_memo_successor_{n}", supersedes=(*current.supersedes, current.id))
    current = replace(current, data={**current.data,
        "next_action": {"text": "Ask the inspector for the safety checklist", "done": False}})
    entries = tuple(ItemRecord(r, f"records/raw/{r.id}.md") for r in (*moved_over, current))
    item = entries[-1]
    now = datetime(2026, 10, 8, 2, tzinfo=timezone.utc)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    why_log.record_surfacings([Surfacing(
        surfaced_at=now - timedelta(days=2), channel="review", item_ref=first,
        record_sha256="old-shown-hash", record_path="archive/old-memo.md",
        record_state="waiting", rule="waiting_on_item", fields_used={},
    )])
    calls = []
    def full_snapshot():
        calls.append("full")
        return entries
    def retired(*args):
        raise AssertionError("historical path and retired first-step sources must not be read")
    service = _CapturingService()
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
        ReviewSources(brain_items=full_snapshot, brain_items_by_path=retired, open_notes=retired), service)
    assert run_moments(context, now - timedelta(days=1), "pc_on")["outcome"] == "nothing"
    assert run_moments(context, now, "pc_on")["item_ids"] == (current.id,)
    assert service.texts == [
        "1. Get a safety checklist from the inspector — no longer waiting (Exhibition permit; 1 of 3) "
        "(next: Ask the inspector for the safety checklist)"
    ]
    [row] = [row for row in why_log.history() if row.channel == "moment"]
    assert (row.item_ref, row.record_path, row.record_sha256, row.record_state) == (
        first, item.path, item.sha256, "up")
    assert run_moments(context, now, "calendar_gap")["outcome"] == "nothing"
    assert calls == ["full", "full", "full"]
    why_log.close()


def test_moments_no_longer_fits_notice_stays_once_without_unfinished_pick(tmp_path):
    why_log = WhyLog(tmp_path / "why.sqlite3")
    item = ItemRecord(make_item(id="brn_pick", title="File the form"), "records/pick.md")
    _picked_history = Surfacing(NOW, "review", item.record.id, item.sha256, "pick_or_park", {},
                               record_state="open", record_path=item.path)
    why_log.record_surfacings([_picked_history])
    why_log.record_response(item_ref=item.record.id, response="picked_today", response_words="do it", responded_at=NOW)
    why_log.note_placement_notice(day=date(2026, 9, 26), key="brn_pick", title=item.record.title, noted_at=NOW)
    service = _CapturingService()
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                            ReviewSources(brain_items=lambda: (item,)), service)
    result = run_moments(context, NOW, "pc_on")
    assert result["item_ids"] == () and result["placement_notices"] == ("brn_pick",)
    assert service.texts == ['1. "File the form" no longer fits today\'s plan. Drop it, move it to tomorrow, or swap it for something?']
    assert run_moments(context, NOW, "calendar_gap")["outcome"] == "nothing"
    why_log.close()


@pytest.mark.parametrize("check,proof,verified,expected", [
    ("accepted", None, True, "Draft the deck — done (was with fleet-boss)"),
    ("none", None, True, "Draft the deck — returned, not yet checked (with fleet-boss)"),
    ("none", "https://example.test/deck", True, 'Draft the deck — back, with proof: "https://example.test/deck" (with fleet-boss)'),
    ("none", "fixture-proof-id", False, 'Draft the deck — back, proof unverified: "fixture-proof-id" (with fleet-boss)'),
])
def test_moments_keeps_request_result_wording_and_once_only(tmp_path, check, proof, verified, expected):
    from brain_core.moments import RequestStatus
    why_log = WhyLog(tmp_path / "why.sqlite3")
    why_log.record_surfacings([Surfacing(NOW, "review", "req_fixture", "", "waiting_on_request", {}, record_state="open")])
    status = RequestStatus("req_fixture", "fleet-boss", "result_returned", check=check,
                           objective="Draft the deck", proof=proof, proof_verified=verified)
    service = _CapturingService()
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                            ReviewSources(request_status=lambda refs: (status,)), service)
    assert run_moments(context, NOW, "task_finished")["request_ids"] == ("req_fixture",)
    assert service.texts == ["1. " + expected]
    assert run_moments(context, NOW, "pc_on")["outcome"] == "nothing"
    why_log.close()


def test_moments_full_snapshot_failure_is_reported_without_retired_path_fallback(tmp_path):
    from gremlin_town.review.collect import SourceUnavailable
    why_log = WhyLog(tmp_path / "why.sqlite3")
    why_log.record_surfacings([Surfacing(NOW, "review", "brn_wait", "old", "waiting_on_item", {}, record_state="waiting")])
    def unavailable():
        raise SourceUnavailable("credential_invalid")
    def retired(*args):
        raise AssertionError("no stale-path fallback")
    service = _CapturingService()
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
        ReviewSources(brain_items=unavailable, brain_items_by_path=retired), service)
    assert run_moments(context, NOW, "pc_on") == {
        "outcome": "nothing", "signal": "pc_on", "couldnt_read": "credential_invalid"}
    assert service.texts == []
    why_log.close()


@pytest.mark.parametrize("done,marked", [(False, False), (True, False), (False, True)])
def test_moments_uses_only_unfinished_owner_next_action_never_retired_steps(tmp_path, done, marked):
    from dataclasses import replace
    record = make_item(id="brn_action", title="Owner's task")
    record = replace(record, data={**record.data, "first_step": "Retired agent step",
        "next_action": {"text": "Owner's exact physical action", "done": done},
        "review_marks": [{"mark": "agents_wording", "part": "next_action"}] if marked else []})
    item = ItemRecord(record, "records/owner-action.md")
    why_log = WhyLog(tmp_path / "why.sqlite3")
    _shown_waiting(why_log, item)
    service = _CapturingService()
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                            ReviewSources(brain_items=lambda: (item,)), service)
    assert run_moments(context, NOW, "pc_on")["outcome"] == "sent"
    assert ("next: Owner's exact physical action" in service.texts[0]) == (not done and not marked)
    assert "Retired agent step" not in service.texts[0]
    assert "no first step yet" not in service.texts[0]
    assert "picked today" not in service.texts[0]
    why_log.close()


def test_moments_full_index_keeps_whose_and_dependency_waits_quiet(tmp_path):
    from dataclasses import replace
    blocker = make_item(id="brn_blocker", title="Get the signature")
    theirs = replace(make_item(id="brn_theirs", title="Sam's signature"),
                     data={"level": "accepted", "whose": "Sam"})
    part = replace(make_item(id="brn_part", title="Submit it"),
                   data={"level": "accepted", "after": [blocker.id]})
    entries = tuple(ItemRecord(r, f"records/{r.id}.md") for r in (blocker, theirs, part))
    why_log = WhyLog(tmp_path / "why.sqlite3")
    for item in entries[1:]:
        _shown_waiting(why_log, item)
    service = _CapturingService()
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                            ReviewSources(brain_items=lambda: entries), service)
    assert run_moments(context, NOW, "pc_on")["outcome"] == "nothing"
    assert service.texts == []
    why_log.close()


_MOTIVE_WORDS = ("again", "why", "keep", "always", "procrastinat", "avoid")


def test_missed_promise_raised_every_morning_with_exact_wording_and_no_motive_words(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    """Case 12, at the GT level (the text run_review actually delivers): the
    line says since when and for how long, and nothing else; the record's state
    never changes; and the next morning's review carries it again, a day longer,
    because nothing about the record changed (N9, 28 Sep; Ciel and Brain Home PRD
    §4.6, where the fixed no-count wording goes)."""

    overdue = make_item(
        id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"},
    )
    item = ItemRecord(record=overdue, path="records/item/2026/09/brn_bill.md")

    capturing_service = _CapturingRequestCreator(real_service)
    why_log = fixture_log(tmp_path / "why.sqlite3")
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(
            brain_items=lambda: (item,),
            calendar_events=lambda start, end: (),
            open_requests=lambda: (),
        ),
        service=capturing_service,
    )

    first = run_review(context, NOW)
    assert first["outcome"] == "sent"
    text = capturing_service.created[0].packet.context
    missed = "Pay the electricity bill — missed since Thu 24 Sep (2 days). Recommit, change, defer or drop?"
    assert missed in text and "## Catch up" in text
    assert not any(word in text.lower() for word in _MOTIVE_WORDS)
    assert overdue.state == "open"
    second = run_review(context, NOW + timedelta(days=1))
    assert second["outcome"] == "sent"
    assert missed.replace("(2 days)", "(3 days)") in capturing_service.created[1].packet.context
    assert not any(word in capturing_service.created[1].packet.context.lower() for word in _MOTIVE_WORDS)
    why_log.close()


# -- Ciel rebuild slice 5: Stuck (N10b), and returns' first step and time box (N12b, N3) --------

_SGT = timezone(timedelta(hours=8))


def _spooled_settings(tmp_path: Path) -> tuple[ReviewSettings, Path]:
    spool = tmp_path / "spool"
    spool.mkdir()
    settings = ReviewSettings.from_table({
        "enabled": True,
        "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa",
        "project": "vault_home",
        "why_log_file": str(tmp_path / "why.sqlite3"),
        "telegram_spool_dir": str(spool),
    })
    return settings, spool


def test_the_review_carries_stuck_after_missed_every_morning_and_the_courier_gets_the_request_text(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    """N10b (Ciel slice 5): a task Nic started whose note hasn't changed for 3 days shows under
    "Stuck", right after Missed, in the same words each morning; a missed task that is also stuck
    shows once, under Missed (Proposed - Nic to confirm); the other sections read as before. The
    courier's file is exactly the text of the request Diablo reads when Nic answers."""

    from brain_core.review import OpenNote

    invoice = make_item(id="brn_invoice", title="Send invoice", timing={"due": "2026-09-26"})
    bill = make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"})
    bank = make_item(id="brn_bank", title="Call the bank", created_at=NOW - timedelta(days=9))
    flights = make_item(id="brn_flights", title="Book the flights", created_at=NOW - timedelta(days=8))
    from dataclasses import replace
    bank = replace(bank, data={**bank.data, "started_at": "2026-09-22T10:00:00+00:00"})
    flights = replace(flights, updated_at=NOW - timedelta(days=1),
                      data={**flights.data, "started_at": "2026-09-25T10:00:00+00:00"})
    items = tuple(
        ItemRecord(record=record, path=f"records/item/2026/09/{record.id}.md")
        for record in (invoice, bill, bank, flights)
    )
    notes = (
        OpenNote(
            session_id="bank-call", next_step="Find the card", task_id="brn_bank",
            updated_at=datetime(2026, 9, 22, 18, 0, tzinfo=_SGT),
        ),
        OpenNote(  # moved last night: not stuck
            session_id="tokyo-trip", next_step="Compare two fares", task_id="brn_flights",
            updated_at=datetime(2026, 9, 25, 21, 0, tzinfo=_SGT),
        ),
        OpenNote(  # stuck too, but missed: shown once, under Missed
            session_id="item-brn_bill", next_step="Log in to SP", updated_at=datetime(2026, 9, 20, 9, 0, tzinfo=_SGT),
        ),
    )
    settings, spool = _spooled_settings(tmp_path)
    capturing = _CapturingRequestCreator(real_service)
    why_log = fixture_log(tmp_path / "why.sqlite3")
    context = ReviewContext(
        settings=settings,
        why_log=why_log,
        sources=ReviewSources(
            brain_items=lambda: items,
            calendar_events=lambda start, end: (),
            open_requests=lambda: (),
            open_notes=lambda: notes,
        ),
        service=capturing,
    )

    assert run_review(context, NOW)["outcome"] == "sent"
    text = capturing.created[0].packet.context
    assert "## Catch up" in text
    assert "Call the bank — What's in the way? · What's the very next physical thing?" in text
    assert "Book the flights — What's in the way?" not in text
    assert "Calendar entries to add" not in text
    assert not any(word in text.lower() for word in _MOTIVE_WORDS)
    assert (spool / "review-2026-09-26.txt").read_bytes() == capturing.created[0].packet.constraints["short"].encode("utf-8")
    [row] = [row for row in why_log.history() if row.rule == "stalled"]
    assert (row.item_ref, row.record_state, row.record_path) == ("brn_bank", "open", "records/item/2026/09/brn_bank.md")
    assert run_review(context, NOW + timedelta(days=1))["outcome"] == "sent"
    next_text = capturing.created[1].packet.context
    assert "Call the bank — What's in the way? · What's the very next physical thing?" in next_text
    assert (spool / "review-2026-09-27.txt").read_bytes() == capturing.created[1].packet.constraints["short"].encode("utf-8")
    why_log.close()


def test_a_return_carries_its_first_step_from_the_tasks_note_and_a_time_box_without_orientation(
    tmp_path: Path,
) -> None:
    """Owner next actions and the existing stall box reach the courier verbatim without orientation."""

    from brain_core.review import OpenNote

    stalling = ItemRecord(record=make_item(id="brn_form", title="File the form"), path="records/item/2026/09/brn_form.md")
    fresh = ItemRecord(
        record=make_item(id="brn_flights", title="Book the flights"), path="records/item/2026/09/brn_flights.md",
    )
    from dataclasses import replace
    stalling = ItemRecord(replace(stalling.record, data={**stalling.record.data,
        "next_action": {"text": "Print page 2", "done": False}}), stalling.path)
    fresh = ItemRecord(replace(fresh.record, data={**fresh.record.data,
        "next_action": {"text": "Compare two fares", "done": False}}), fresh.path)
    notes = (
        OpenNote(session_id="tax-forms", next_step="Print page 2", updated_at=NOW, task_id="brn_form"),
        OpenNote(session_id="tokyo-trip", next_step="Compare two fares", updated_at=NOW, task_id="brn_flights"),
    )
    settings, spool = _spooled_settings(tmp_path)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    why_log.record_surfacings([
        Surfacing(
            surfaced_at=NOW - timedelta(days=days), channel="review", item_ref="brn_form",
            record_sha256=stalling.sha256, record_state="waiting", rule="waiting_on_item", fields_used={},
            record_path=stalling.path,
        )
        for days in (2, 1)
    ])
    for item in (stalling, fresh):
        _shown_waiting(why_log, item)
    service = _CapturingService()
    context = ReviewContext(
        settings=settings,
        why_log=why_log,
        sources=ReviewSources(
            brain_items=lambda: (stalling, fresh),
            open_notes=lambda: notes,
        ),
        service=service,
    )

    assert run_moments(context, NOW, "pc_on", signal_id="pc_on-1")["outcome"] == "sent"
    assert service.texts == [
        "1. File the form — no longer waiting (next: Print page 2; 20 minutes, then stop)\n"
        "2. Book the flights — no longer waiting (next: Compare two fares)"
    ]
    assert (spool / "moment-pc_on-hermes-wa-pc_on-1.txt").read_bytes() == service.texts[0].encode("utf-8")
    why_log.close()


@pytest.mark.parametrize(("ranking", "expected"), [
    ("points", ["brn_dentist", "brn_flat"]),  # due tomorrow outscores a window closing in 5 days
    ("rules", ["brn_flat", "brn_dentist"]),  # the nine rules: neither matches one, so the oldest first
])
def test_the_reviews_coming_up_ranks_as_orientation_ranking_says(
    tmp_path: Path, real_service: GremlinTownService, ranking: str, expected: list[str],
) -> None:
    """`[orientation] ranking` (PRD §5; the way back kept until a week of real use) orders the
    06:00 review's "Coming up" as it orders both asks."""

    dentist = ItemRecord(
        record=make_item(id="brn_dentist", title="Book the dentist", timing={"due": "2026-09-27"}),
        path="records/item/2026/09/brn_dentist.md",
    )
    flat = ItemRecord(
        record=make_item(id="brn_flat", title="Clean the flat", timing={"window": {"start": "2026-09-28", "end": "2026-10-01"}},
                         created_at=NOW - timedelta(days=2)),
        path="records/item/2026/09/brn_flat.md",
    )
    why_log = fixture_log(tmp_path / "why.sqlite3")
    context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (dentist, flat)),
        service=real_service,
        ranking=ranking,
    )
    assert run_review(context, NOW)["outcome"] == "sent"
    assert [row.item_ref for row in why_log.history() if row.rule == "coming_up"] == expected
    why_log.close()


# -- The weekly sweep (29 Sep 2026) ----------------------------------------------------------------

FRIDAY = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT on Friday 2 Oct 2026


class _RecordingService:
    """Keeps each request's recipient, operation key and packet; fails for ``fail_for``."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.sent: list[CreateRequest] = []
        self.fail_for = fail_for

    def requests_create(self, actor, command):
        from gremlin_town.contracts import Receipt

        if command.recipient == self.fail_for:
            raise RuntimeError("no route")
        self.sent.append(command)
        return Receipt(operation_id=command.client_operation_id, request_id=f"req-{len(self.sent)}",
                       accepted_at=FRIDAY, operation_state=None, work_state=None, revision=None,
                       actor_id=actor.actor_id)


def _sweep_context(tmp_path: Path, service, *, sweep: str | None = "fleet-boss-grok") -> ReviewContext:
    table = {"enabled": True, "sender_participant": "exosuit-review", "delivery_participant": "hermes-wa",
             "project": "vault_home", "why_log_file": str(tmp_path / "why.sqlite3")}
    if sweep is not None:
        table["sweep_participant"] = sweep
    return ReviewContext(
        settings=ReviewSettings.from_table(table),
        why_log=fixture_log(tmp_path / "why.sqlite3"),
        sources=ReviewSources(brain_items=lambda: (), open_requests=lambda: ()),
        service=service,
    )


def test_fridays_review_also_asks_fleet_boss_for_the_weekly_sweep_once(tmp_path: Path) -> None:
    service = _RecordingService()
    context = _sweep_context(tmp_path, service)
    result = run_review(context, FRIDAY)
    assert [command.recipient for command in service.sent] == ["hermes-wa", "fleet-boss-grok"]
    sweep = service.sent[1]
    assert sweep.client_operation_id == "sweep-fleet-boss-grok-2026-10-02"
    assert "Would Nic be hurt if this were forgotten or done late?" in sweep.packet.context
    assert "level suggested" in sweep.packet.context and "bc_memory_search" in sweep.packet.context
    assert result["sweep"] == {"outcome": "sent", "recipient": "fleet-boss-grok", "request_id": "req-2"}
    assert run_review(context, FRIDAY) == {"outcome": "already_sent"}  # a rerun sends neither again
    assert len(service.sent) == 2


def test_no_sweep_on_other_days_or_without_the_setting(tmp_path: Path) -> None:
    service = _RecordingService()
    assert "sweep" not in run_review(_sweep_context(tmp_path / "sat", service), NOW)  # a Saturday
    other = _RecordingService()
    assert "sweep" not in run_review(_sweep_context(tmp_path / "unset", other, sweep=None), FRIDAY)
    assert [command.recipient for command in service.sent + other.sent] == ["hermes-wa", "hermes-wa"]


def test_a_failed_sweep_never_stops_the_review(tmp_path: Path) -> None:
    service = _RecordingService(fail_for="fleet-boss-grok")
    result = run_review(_sweep_context(tmp_path, service), FRIDAY)
    assert result["outcome"] == "sent"
    assert result["sweep"] == {"outcome": "failed", "recipient": "fleet-boss-grok", "error": "RuntimeError"}
    assert [command.recipient for command in service.sent] == ["hermes-wa"]


def test_sweep_participant_is_checked() -> None:
    base = {"enabled": True, "why_log_file": "/tmp/why.sqlite3"}
    assert ReviewSettings.from_table({**base, "sweep_participant": "fleet-boss-grok"}).sweep_participant == "fleet-boss-grok"
    assert ReviewSettings.from_table(base).sweep_participant is None
    for bad in ({**base, "sweep_participant": ""}, {**base, "sweep_participant": 3},
                {"enabled": False, "sweep_participant": "fleet-boss-grok"}):
        with pytest.raises(ValueError):
            ReviewSettings.from_table(bad)
