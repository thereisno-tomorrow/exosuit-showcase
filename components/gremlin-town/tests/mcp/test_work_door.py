"""The real door over a fake GitHub transport, with no live record effects."""
import json
from dataclasses import replace
from datetime import datetime, timedelta

import anyio
import pytest

from brain_core.records import parse_record, render_record, record_from_mapping
from brain_core.github_storage import GitHubStorageError
from gremlin_town.mcp.brain_runtime import BrainOperation, BrainRuntimeError, WorkRequest
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.review.why_log import Surfacing, WhyLog
from test_github_brain_provider import LANE_B, _adapter, _authorization, _hub_storage, _memory_request

NOW = datetime.fromisoformat("2026-09-30T12:00:00+08:00")
WORK = _authorization(BrainOperation.WORK, "write")
MESSAGE = ("Yeah, I started the exhibition's permit already, I basically texted my coordinator, "
    "but what's next is I have to get a safety checklist from the inspector on my venue inspection "
    "on 8 October, and then officially submit my permit along with the memo on Exhibit Portal.")


def setup_door(tmp_path):
    fake = LANE_B.FakeGitHub()
    pins = []
    def clock(operation):
        pins.append(operation)
        return NOW
    log = WhyLog(tmp_path / "why.sqlite3")
    provider = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=clock),
        why_log=log, clock=lambda: NOW, replay_marker=tmp_path / "work-replay-check-only")
    return provider, fake, pins, log


def request(operation="capture", **values):
    return WorkRequest(operation, "need to pay venue invoice", "owner", "direct",
        ({"move": "new", "title": "Pay venue invoice"},), **values)


def call(provider, change, caller="hermes-wa"):
    async def invoke():
        with bind_write_attribution(caller):
            return await provider.work(WORK, change)
    return anyio.run(invoke)


def records(fake):
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    return [parse_record(fake.blobs[blob].decode(), path) for path, blob in tree.items()
        if path.startswith("records/item/")]


@pytest.mark.parametrize("word", [None, "someday", "later", "not now"])
def test_undated_park_lands_closes_answer_and_replays_without_a_write(tmp_path, word):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        first = call(provider, request())["things"][0]["id"]
        log.record_surfacings([Surfacing(NOW, "review", first, "a" * 64, "stalled", {},
            question="Park or drop?", offered=("park", "drop"))])
        reference = str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
        change = WorkRequest("park-someday", word or "park", "owner", "direct",
            ({"move": "park", "thing": first},), answers=(reference,))
        result = call(provider, change)
        assert result["status"] == "accepted" and not log.answer_context(reference).open
        assert f"parked ({word or 'someday'})" in result["say"]
        saved = next(r for r in records(fake) if r.id == result["things"][0]["record_id"])
        assert saved.state == "parked" and saved.data["parked_until"] == {"event": word or "someday"}
        head = fake.branch
        replay = call(provider, change)
        assert replay["status"] == "duplicate" and replay["say"] == result["say"] and fake.branch == head
    finally:
        log.close()


def test_details_write_part_bodies_and_read_back_in_where_it_stands(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    details = "Confirm the branch and documents.\nBring my passport."
    try:
        change = WorkRequest("parts-with-notes", "Venue rental Call bank Sign loan Compare rates " + details,
            "owner", "direct", (
                {"move": "new", "title": "Venue rental", "details": "Compare rates"},
                {"move": "parts", "thing": "#1", "parts": [
                    {"title": "Call bank", "details": details}, {"title": "Sign loan", "then": True}]}))
        result = call(provider, change)
        assert result["status"] == "accepted"
        assert "\n  Confirm the branch and documents.\n  Bring my passport." in result["say"]
        parent = result["things"][0]["id"]
        read_auth = _authorization(BrainOperation.MEMORY_READ, "read")
        view = anyio.run(provider.read, read_auth, parent)
        projected = view.reader_reply_fields()
        parts = projected["now"]["parts"]
        assert [(part["title"], part.get("details")) for part in parts] == [
            ("Call bank", details), ("Sign loan", None)]
        part_view = anyio.run(provider.read, read_auth, parts[0]["record_id"])
        assert part_view.body == part_view.now["details"] == details
        assert parts[1].get("details") is None
        head = fake.branch
        assert call(provider, change) == {**result, "status": "duplicate"} and fake.branch == head
        with pytest.raises(BrainRuntimeError) as error:
            call(provider, WorkRequest("invented-details", "Venue rental Call bank", "owner", "direct", (
                {"move": "parts", "thing": parent, "parts": [{"title": "Call bank", "details": "Invented checklist"}]},)))
        assert error.value.field == "parts.details" and fake.branch == head
    finally:
        log.close()


@pytest.mark.parametrize("planned", [False, True], ids=["undated-ranked", "today-already-picked"])
def test_addition_before_your_day_is_saved_and_in_the_fresh_part(tmp_path, planned):
    from types import SimpleNamespace
    from brain_core.orientation import rank
    from brain_core.review import ItemRecord
    from brain_core.work import first_id
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.run import ReviewContext, current_review_part, run_review
    from gremlin_town.review.settings import ReviewSettings

    provider, fake, pins, log = setup_door(tmp_path)
    try:
        call(provider, request(operation="existing"))
        log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=(r.id for r in records(fake)))
        settings = ReviewSettings.from_table({"enabled": True, "sender_participant": "exosuit-review",
            "delivery_participant": "hermes-wa", "project": "vault_home", "why_log_file": str(tmp_path / "why.sqlite3")})
        class Sender:
            def requests_create(self, actor, command):
                return SimpleNamespace(request_id="req_fixture")
        source = lambda: tuple(ItemRecord(r, f"records/{r.id}.md") for r in records(fake))
        context = ReviewContext(settings, log, ReviewSources(brain_items=source), Sender())
        run_review(context, NOW - timedelta(minutes=1))
        frozen = log.morning_packet(NOW.date())
        initial = frozen.request["constraints"]["lines"]
        prompt = next(line for line in initial if line["kind"] == "add_prompt")
        assert prompt["number"] == 0 and prompt["item_ref"] is prompt["reference"] is None
        assert prompt["allowed_answers"] == prompt["offered"] == []
        assert prompt == [line for line in initial if line["part"] == "new"][-1]
        assert len(frozen.references) == len(initial) - 1
        assert all(row.item_ref is not None for row in log.history())

        moves = [{"move": "new", "title": "Draft the proposal"}]
        if planned:
            moves.append({"move": "date", "thing": "#1", "when": "today"})
        receipt = call(provider, WorkRequest("add-before-day", "I need to draft the proposal" + (" today" if planned else ""),
            "owner", "direct", tuple(moves)))
        assert receipt["status"] == "accepted"
        added = next(r for r in records(fake) if r.title == "Draft the proposal")
        assert (added.data.get("dates", {}).get("planned", {}).get("day") == NOW.date().isoformat()) is planned
        assert "Draft the proposal" not in frozen.request["text"]
        head = fake.branch
        third = current_review_part(context, NOW + timedelta(minutes=1), caller="hermes-wa", part=3)
        assert third["lines"][-1]["kind"] == "add_prompt"
        assert third["lines"][-1]["reference"] is third["lines"][-1]["item_ref"] is None
        assert third["text"].endswith(prompt["question"])
        assert sum(line["kind"] == "add_prompt" for line in third["lines"]) == 1
        assert any(line["item_ref"] == first_id(added) for line in third["lines"])
        assert current_review_part(context, NOW + timedelta(minutes=2), caller="hermes-wa", part=3)["lines"] == third["lines"]
        rebuilt = current_review_part(context, NOW + timedelta(minutes=3), caller="hermes-wa")
        picks = [line for line in rebuilt["lines"] if line["allowed_answers"]]
        addition = next(line for line in picks if line["item_ref"] == first_id(added))
        assert addition["already_picked"] is planned
        assert ("already picked" in addition["text"]) is planned
        if planned:
            assert picks[0] == addition
        else:
            ranked = rank(source(), NOW.date(), now=NOW + timedelta(minutes=3))
            assert [line["item_ref"] for line in picks] == [first_id(candidate.item.record) for candidate in ranked]
        assert picks[0]["number"] == max(line["number"] for line in initial if line["part"] != "your_day") + 1
        assert log.answer_context(addition["reference"]).thing == first_id(added)
        assert log.morning_packet(NOW.date()).request == frozen.request and fake.branch == head
    finally:
        log.close()


def test_current_day_rebuild_includes_answers_and_logs_reusable_references(tmp_path):
    from types import SimpleNamespace
    from brain_core import work
    from brain_core.review import ItemRecord
    from gremlin_town.api.review import record_review_response
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.run import ReviewContext, current_review_part, run_review
    from gremlin_town.review.settings import ReviewSettings

    provider, fake, pins, log = setup_door(tmp_path)
    captured = call(provider, WorkRequest("day-fixture", "Research Atlas on 29 September; Venue rental permit; Email migration; Pay bill", "owner", "direct", (
        {"move": "new", "title": "Research Atlas", "when": "29 September"},
        {"move": "new", "title": "Venue rental permit"},
        {"move": "new", "title": "Email migration"},
        {"move": "new", "title": "Pay bill"},
    )))
    refs = {r.title: work.first_id(r) for r in records(fake)}
    initial = []
    for record in records(fake):
        if record.title == "Research Atlas":
            record = replace(record, data={**record.data, "dates": {"planned": {
                "day": "2026-09-29", "words": "29 September", "read_by": "code", "read_on": "2026-09-28"}}})
        if record.title in {"Venue rental permit", "Email migration"}:
            record = replace(record, data={**record.data, "review_marks": [{"mark": "question", "text": "Parts: compare rates; call bank. Yes?"}]})
        initial.append(record)
    fake.seed({f"records/item/{r.created_at:%Y/%m}/{r.id}.md": render_record(r).encode() for r in initial})
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=refs.values())
    settings = ReviewSettings.from_table({"enabled": True, "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa", "project": "vault_home", "why_log_file": str(tmp_path / "why.sqlite3")})
    class Sender:
        def requests_create(self, actor, command):
            return SimpleNamespace(request_id="req_fixture")
    context = ReviewContext(settings, log, ReviewSources(brain_items=lambda: tuple(
        ItemRecord(r, f"records/{r.id}.md") for r in records(fake))), Sender())
    try:
        run_review(context, NOW)
        frozen = log.morning_packet(NOW.date())
        frozen_bytes = dict(frozen.request)
        original = frozen.request["constraints"]["lines"]
        carried = next(line for line in original if line["part"] == "catch_up" and line["item_ref"] == refs["Research Atlas"])
        venue_rental = next(line for line in original if line["part"] == "new" and line["item_ref"] == refs["Venue rental permit"])
        call(provider, WorkRequest("carry-today", "today", "owner", "direct",
            ({"move": "date", "thing": refs["Research Atlas"], "when": "today"},), answers=(carried["reference"],)))
        call(provider, WorkRequest("confirm-parts", "yes", "owner", "direct",
            ({"move": "parts", "thing": refs["Venue rental permit"], "parts": [
                {"title": "compare rates"}, {"title": "call bank", "then": True}]},), answers=(venue_rental["reference"],)))
        head = fake.branch
        rebuilt = current_review_part(context, NOW + timedelta(minutes=1), caller="hermes-wa")
        picks = [line for line in rebuilt["lines"] if line["allowed_answers"]]
        assert picks[0]["item_ref"] == refs["Research Atlas"] and picks[0]["already_picked"]
        assert "already picked" in picks[0]["text"]
        assert any("compare rates" in line["text"] for line in picks)
        assert not any(line["item_ref"] == refs["Email migration"] for line in picks)
        last = max(line["number"] for line in original if line["part"] != "your_day")
        assert picks[0]["number"] == last + 1
        for line in rebuilt["lines"]:
            saved = log.answer_context(line["reference"])
            assert saved.thing == line["item_ref"] and saved.question == line["text"]
            assert saved.offered == tuple(line["offered"])
        again = current_review_part(context, NOW + timedelta(minutes=2), caller="hermes-wa")
        assert [line["reference"] for line in again["lines"]] == [line["reference"] for line in rebuilt["lines"]]
        bill = next(line for line in original if line["part"] == "your_day" and line["item_ref"] == refs["Pay bill"])
        assert next(line for line in picks if line["item_ref"] == refs["Pay bill"])["reference"] == bill["reference"]
        result = record_review_response(log, caller="hermes-wa", item_refs=[bill["reference"]],
            response="picked_today", response_words="yes", now=NOW + timedelta(minutes=3))
        assert result["recorded"]
        assert log.morning_packet(NOW.date()).request == frozen_bytes and fake.branch == head
        code = "system:fixture-relayer"
        with pytest.raises(BrainRuntimeError, match="Only Diablo"):
            current_review_part(context, NOW, caller=code, part=3)
        third = current_review_part(context, NOW + timedelta(minutes=4), caller="local-codex", part=3,
                                    relayer=code, code_callers=frozenset({code}))
        answered = next(line for line in third["lines"] if line["reference"] == venue_rental["reference"])
        assert answered["item_ref"] == refs["Venue rental permit"] and answered["answered"]
        assert answered["response"] == "answered" and answered["response_words"] == "yes"
        assert answered["allowed_answers"] == venue_rental["allowed_answers"]
        unanswered = next(line for line in third["lines"] if line["item_ref"] == refs["Email migration"])
        assert not unanswered["answered"] and unanswered["response"] is None
        assert log.morning_packet(NOW.date()).request == frozen_bytes and fake.branch == head
        fresh = next(line for line in picks if "compare rates" in line["text"])
        receipt = call(provider, WorkRequest("current-part-pick", "today", "owner", "direct",
            ({"move": "date", "thing": fresh["item_ref"], "when": "today"},), answers=(fresh["reference"],)))
        assert receipt["status"] == "accepted"
        selected = current_review_part(context, NOW + timedelta(minutes=5), caller="hermes-wa")
        assert next(line for line in selected["lines"] if line["item_ref"] == fresh["item_ref"])["already_picked"]
        assert log.morning_packet(NOW.date()).request == frozen_bytes
    finally:
        log.close()


@pytest.mark.parametrize("caller", ["chatgpt-web", "claude-web", "fleet-boss-grok", "local-codex", "system:unknown"])
def test_direct_provider_repeats_gate_before_any_effect(tmp_path, caller):
    provider, fake, pins, log = setup_door(tmp_path)
    head = fake.branch
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, request(), caller)
    assert error.value.code == "ACCESS_DENIED" and error.value.fix
    assert fake.branch == head and not pins
    log.close()


def test_done_day_goes_through_the_real_door_and_replay(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        created = call(provider, request())
        first = created["things"][0]["id"]
        words = "I did pay venue invoice yesterday"
        change = WorkRequest("tick-yesterday", words, "owner", "direct", (
            {"move": "tick", "thing": first, "what": "thing", "words": words, "when": "yesterday"},))
        accepted = call(provider, change)
        assert accepted["status"] == "accepted" and "done yesterday" in accepted["say"]
        assert accepted["things"][0]["done_day"] == "2026-09-29"
        saved = next(r for r in records(fake) if r.state == "done")
        assert saved.data["dates"]["done"] == dict(words="yesterday", read_by="code",
            read_on="2026-09-30", day="2026-09-29")
        from brain_core import work
        from gremlin_town.mcp.github_brain_provider import _reader_now
        view = _reader_now(saved, work.index_things(records(fake)), NOW)
        assert view["progress"]["last_ticked_day"] == "2026-09-29"
        head = fake.branch
        assert call(provider, change) == {**accepted, "status": "duplicate"}
        assert fake.branch == head
    finally:
        log.close()


def test_future_done_day_refuses_without_publishing(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        created = call(provider, request())
        first = created["things"][0]["id"]
        words = "I did pay venue invoice tomorrow"
        head = fake.branch
        with pytest.raises(BrainRuntimeError) as error:
            call(provider, WorkRequest("tick-tomorrow", words, "owner", "direct", (
                {"move": "tick", "thing": first, "words": words, "when": "tomorrow"},)))
        assert error.value.fix == "that day hasn't happened yet" and fake.branch == head
    finally:
        log.close()


def test_no_host_identity_is_not_admission(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    with pytest.raises(BrainRuntimeError, match="cannot change"):
        anyio.run(provider.work, WORK, request())
    assert not pins
    log.close()


def test_exhibition_is_four_records_in_one_commit_and_safe_to_retry(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    change = WorkRequest("exhibition", MESSAGE, "owner", "direct", (
        {"move": "new", "title": "Exhibition permit"},
        {"move": "start", "thing": "#1"},
        {"move": "parts", "thing": "#1", "parts": [
            {"title": "Text my coordinator", "done": True},
            {"title": "Get a safety checklist from the inspector", "then": True,
             "when": "8 October", "event": "venue inspection"},
            {"title": "Submit my permit along with the memo on Exhibit Portal", "then": True},
        ]},
    ))
    before = len(fake.commits)
    accepted = call(provider, change)
    assert accepted["status"] == "accepted" and len(fake.commits) == before + 1
    saved = records(fake)
    assert len(saved) == 4
    parent = next(r for r in saved if "parent" not in r.data)
    parts = sorted((r for r in saved if r.data.get("parent") == parent.id), key=lambda r: r.data["order"])
    assert parent.data["started_at"] and parts[0].state == "done"
    assert parts[1].data["dates"]["not_before"]["day"] == "2026-10-08"
    assert parts[2].data["after"] == [parts[1].id] and not parts[2].data.get("dates")
    assert all("deadline" not in r.data.get("dates", {}) for r in saved)
    assert "1 of 3" in accepted["say"] and len(json.dumps(accepted)) <= 2000
    replay = call(provider, change)
    assert replay == {**accepted, "status": "duplicate"}
    assert len(fake.commits) == before + 1
    log.close()


@pytest.mark.parametrize("archived", [False, True])
def test_exact_replay_uses_original_batch_parent_after_same_clock_other_capture_and_successor(tmp_path, archived):
    provider, fake, pins, log = setup_door(tmp_path)
    original = request("first-snapshot")
    first = call(provider, original)
    write = _authorization(BrainOperation.MEMORY_SAVE, "write")
    with bind_write_attribution("local-codex"):
        other = anyio.run(provider.save, write, _memory_request("later-same-clock", "item",
            title="Pay venue invoice", words="need to pay venue invoice"))
    assert other.record_id != first["things"][0]["record_id"]
    provider._storage.archive_superseded = archived
    call(provider, WorkRequest("successor-snapshot", "I started Pay venue invoice", "owner", "direct",
        ({"move": "start", "thing": first["things"][0]["record_id"]},)))
    head = fake.branch
    patches = len(fake.patch_payloads)
    replay = call(provider, original)
    assert replay == {**first, "status": "duplicate"}
    assert fake.branch == head and len(fake.patch_payloads) == patches
    with pytest.raises(BrainRuntimeError) as conflict:
        call(provider, replace(original, message="changed message"))
    assert conflict.value.code == "brain.conflict" and conflict.value.fix
    assert fake.branch == head
    log.close()


@pytest.mark.parametrize("caller", ["hermes-wa", "chatgpt-web"])
@pytest.mark.parametrize("archive", ["successor", "whole-batch"])
def test_raise_with_bridge_replays_exact_original_archived_batch(tmp_path, caller, archive):
    provider, fake, pins, log = setup_door(tmp_path)
    write = _authorization(BrainOperation.MEMORY_SAVE, "write")
    original = _memory_request("raise-archived", "item", title="Silver budget",
        body="Discuss the silver budget with Sam.\n\n", words="Discuss the silver budget with Sam.",
        raise_with="Sam")
    try:
        with bind_write_attribution(caller):
            accepted = anyio.run(provider.save, write, original)
        captured = records(fake)
        assert len(captured) == 1 and captured[0].data["raise_with"] == "Sam"
        assert captured[0].body == original.body.rstrip("\n")
        if archive == "whole-batch":
            paths = tuple(entry.path for entry in provider._storage.snapshot_records(kinds=frozenset({"item"})).records)
            provider._storage.archive_records(paths=paths, kinds=frozenset({"item"}))
        else:
            provider._storage.archive_superseded = True
            successor = call(provider, WorkRequest("raise-start", "I started Silver budget", "owner", "direct",
                ({"move": "start", "thing": accepted.record_id},)))
            latest = next(record for record in records(fake) if record.id == successor["things"][0]["record_id"])
            assert latest.data["raise_with"] == "Sam" and latest.body == original.body.rstrip("\n")
        # A separate caller using the same raw key is a new batch at the same clock.
        other_caller = "chatgpt-web" if caller == "hermes-wa" else "hermes-wa"
        with bind_write_attribution(other_caller):
            other = anyio.run(provider.save, write, replace(original, raise_with="Rowan"))
        assert other.record_id != accepted.record_id
        head, writes, pinned = fake.branch, len(fake.patch_payloads), tuple(pins)
        tree = fake.trees[fake.commits[head]["tree"]]
        assert any(accepted.record_id in path for path in tree if path.startswith("archive/"))

        def no_clock(_):
            raise AssertionError("raise_with archived replay must use its original batch parent before the clock")

        fresh = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=no_clock),
            why_log=log, clock=lambda: NOW)
        with bind_write_attribution(caller):
            assert anyio.run(fresh.save, write, original) == replace(accepted, status="duplicate")
            with pytest.raises(BrainRuntimeError) as conflict:
                anyio.run(fresh.save, write, replace(original, raise_with="Rowan"))
            assert conflict.value.code == "brain.conflict" and conflict.value.fix
        assert fake.branch == head and len(fake.patch_payloads) == writes and tuple(pins) == pinned
        assert accepted.record_id not in {record.id for record in records(fake)}
        with pytest.raises(BrainRuntimeError) as denied:
            call(fresh, request("raise-denied"), "system:unknown")
        assert denied.value.code == "ACCESS_DENIED" and denied.value.fix
        assert fake.branch == head and len(fake.patch_payloads) == writes
    finally:
        log.close()


def test_raise_with_is_not_a_public_door_move_field(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    head = fake.branch
    change = replace(request("raise-public-field"),
        moves=({"move": "new", "title": "Pay venue invoice", "raise_with": "Sam"},))
    try:
        with pytest.raises(BrainRuntimeError) as refused:
            call(provider, change)
        assert refused.value.code == "brain.invalid_argument" and refused.value.fix
        assert fake.branch == head and not fake.patch_payloads and not records(fake)
    finally:
        log.close()


def test_raise_with_bridge_marker_validates_without_pin_or_publication(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    original = _memory_request("raise-marker", "item", title="Silver budget",
        words="Discuss the silver budget with Sam", raise_with="Sam")
    write = _authorization(BrainOperation.MEMORY_SAVE, "write")
    head = fake.branch
    try:
        provider.replay_marker.write_text("fixture replay")
        with bind_write_attribution("hermes-wa"):
            checked = anyio.run(provider.save, write, original)
            assert checked.status == "validated" and not checked.remote_observed
            assert checked.version is None and checked.content_sha256 is None
            assert fake.branch == head and not pins and not records(fake) and not fake.patch_payloads
            with pytest.raises(BrainRuntimeError) as refused:
                anyio.run(provider.save, write, replace(original, raise_with=""))
            assert "raise_with" in refused.value.field and refused.value.fix
            assert fake.branch == head and not pins and not fake.patch_payloads
            provider.replay_marker.unlink()
            accepted = anyio.run(provider.save, write, original)
        assert accepted.status == "accepted" and len(pins) == 1
        assert records(fake)[0].data["raise_with"] == "Sam"
    finally:
        log.close()


def test_check_only_and_marker_do_not_pin_or_publish(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    head = fake.branch
    assert call(provider, request(check_only=True))["status"] == "validated"
    assert fake.branch == head and not pins and not log.history()
    provider.replay_marker.write_text("fixture replay")
    assert call(provider, request())["status"] == "validated"
    assert fake.branch == head and not pins
    provider.replay_marker.unlink()
    assert call(provider, request())["status"] == "accepted"
    assert fake.branch != head and len(pins) == 1
    log.close()


def test_conflicting_operation_id_is_refused(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    call(provider, request())
    head = fake.branch
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, replace(request(), message="Need to call dentist",
            moves=({"move": "new", "title": "Call dentist"},)))
    assert error.value.code == "brain.conflict" and error.value.fix
    assert fake.branch == head
    log.close()


def test_whole_archived_batch_retry_is_exact_and_does_not_expose_or_restore_archive(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    original = request("whole-archive")
    accepted = call(provider, original)
    record_id = accepted["things"][0]["record_id"]
    paths = tuple(item.path for item in provider._storage.snapshot_records(kinds=frozenset({"item"})).records)
    provider._storage.archive_records(paths=paths, kinds=frozenset({"item"}))
    with bind_write_attribution("local-codex"):
        other = anyio.run(provider.save, _authorization(BrainOperation.MEMORY_SAVE, "write"),
            _memory_request("whole-archive", "item", title="Pay venue invoice", words="need to pay venue invoice"))
    assert other.record_id != record_id
    head, writes = fake.branch, len(fake.patch_payloads)

    def no_materialization(_):
        raise AssertionError("exact whole-archive retry must reconcile before the clock")

    fresh = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=no_materialization),
        why_log=log, clock=lambda: NOW)
    assert call(fresh, original) == {**accepted, "status": "duplicate"}
    assert fake.branch == head and len(fake.patch_payloads) == writes
    assert [record.id for record in records(fake)] == [other.record_id]
    assert any(record_id in path for path in fake.trees[fake.commits[head]["tree"]] if path.startswith("archive/"))
    with pytest.raises(BrainRuntimeError) as conflict:
        call(fresh, replace(original, message="Changed original message"))
    assert conflict.value.code == "brain.conflict" and conflict.value.fix
    with pytest.raises(BrainRuntimeError) as denied:
        call(fresh, original, "local-codex")
    assert denied.value.code == "ACCESS_DENIED" and denied.value.fix
    with pytest.raises(BrainRuntimeError) as missing:
        anyio.run(fresh.read, _authorization(BrainOperation.MEMORY_READ, "read"), record_id)
    assert missing.value.code == "brain.not_found" and missing.value.fix
    assert fake.branch == head and len(fake.patch_payloads) == writes
    log.close()


def test_undo_restores_prior_as_new_and_cannot_find_bridge_save(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    first = call(provider, request())
    fid = first["things"][0]["id"]
    changed = WorkRequest("start", "I started Pay venue invoice", "owner", "direct",
        ({"move": "start", "thing": fid},))
    call(provider, changed)
    call(provider, WorkRequest("undo-start", "Undo my start of Pay venue invoice", "owner", "direct",
        ({"move": "undo", "change": "start", "words": "Undo my start of Pay venue invoice"},)))
    saved = records(fake)
    current = next(r for r in saved if r.id not in {old for r in saved for old in r.supersedes})
    assert len(current.supersedes) == 2 and "started_at" not in current.data
    bridge = _memory_request("tier-b", "item", title="New capture", body="New capture", words="New capture")
    with bind_write_attribution("chatgpt-web"):
        anyio.run(provider.save, _authorization(BrainOperation.MEMORY_SAVE, "write"), bridge)
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, WorkRequest("undo-tier-b", "Undo New capture", "owner", "direct",
            ({"move": "undo", "change": "tier-b", "words": "Undo New capture"},)))
    assert error.value.fix
    log.close()


def test_partial_replay_is_refused_without_repairing_the_fixture(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    change = WorkRequest("partial", "Need venue invoice and dentist", "owner", "direct",
        ({"move": "new", "title": "Venue invoice"}, {"move": "new", "title": "Dentist"}))
    accepted = call(provider, change)
    missing = accepted["things"][1]["record_id"]
    # An intentionally incomplete fake revision; no real record is changed.
    head = fake.prepare_advance({}, message="partial fixture")
    tree = fake.trees[fake.commits[head]["tree"]]
    for path in list(tree):
        if missing in path:
            del tree[path]
    fake.branch = head
    commits = len(fake.commits)
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, change)
    assert error.value.code == "brain.record.exists" and error.value.fix
    assert fake.branch == head and len(fake.commits) == commits
    log.close()


def test_revision_conflict_recomputes_against_the_new_head(tmp_path, monkeypatch):
    provider, fake, pins, log = setup_door(tmp_path)
    original = provider._storage.publish_records
    revisions = []
    outsider = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=lambda key: NOW), clock=lambda: NOW)
    external_id = []
    def publish(**kwargs):
        revisions.append(kwargs["base_revision"].commit_sha)
        if len(revisions) == 1:
            external = call(outsider, WorkRequest("external", "Board for Workshop", "owner", "direct",
                ({"move": "new", "title": "Board for Workshop"},)))
            external_id.append(external["things"][0]["id"])
        return original(**kwargs)
    monkeypatch.setattr(provider._storage, "publish_records", publish)
    result = call(provider, WorkRequest("raced", "Look at Board for Workshop", "owner", "direct",
        ({"move": "new", "title": "Look at Board for Workshop"},)))
    assert result["status"] == "accepted" and len(revisions) == 2 and revisions[0] != revisions[1]
    saved = next(record for record in records(fake) if record.id == result["things"][0]["record_id"])
    assert {"mark": "maybe_same", "as": external_id[0]} in saved.data["review_marks"]
    log.close()


def review_merge(provider, fake, log, *, age=0, wrong_target=False, offered=("merge",), answered=False):
    first = call(provider, WorkRequest("first", "Board for Workshop", "owner", "direct",
        ({"move": "new", "title": "Board for Workshop"},)))["things"][0]["id"]
    second = call(provider, WorkRequest("second", "Look at Board for Workshop", "owner", "direct",
        ({"move": "new", "title": "Look at Board for Workshop"},)))["things"][0]["id"]
    item = first if wrong_target else second
    log.record_surfacings([Surfacing(NOW - timedelta(days=age), "review", item, "a" * 64,
        "maybe_same", {"review_marks": [{"mark": "maybe_same", "as": first}]},
        question="Same as 'Board for Workshop'? Merge?", offered=offered)])
    reference = str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
    if answered:
        log.record_response(item_ref=item, response="accepted", response_words="yes", responded_at=NOW)
    return first, second, WorkRequest("merge", "yes", "owner", "direct",
        ({"move": "organise", "thing": second, "merge_into": first},), answers=(reference,))


@pytest.mark.parametrize("age", [0, 1])
def test_bare_yes_merges_only_a_current_offered_thing(tmp_path, age):
    provider, fake, pins, log = setup_door(tmp_path)
    first, second, change = review_merge(provider, fake, log, age=age)
    result = call(provider, change)
    assert result["status"] == "accepted"
    saved = next(record for record in records(fake) if second in record.supersedes)
    assert saved.state == "no_longer_needed"
    assert saved.evidence[-2].excerpt == "Same as 'Board for Workshop'? Merge?"
    assert saved.evidence[-1].excerpt == "yes" and saved.source["words"] == "yes"
    assert log.history()[0].response == ("answered" if age == 0 else None)
    log.close()


def test_catch_up_another_day_offer_authorises_date_on_both_door_checks(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        first = call(provider, request())["things"][0]["id"]
        log.record_surfacings([Surfacing(NOW, "review", first, "a" * 64, "carried_over", {},
            question="Today, another day, or drop?", offered=("another day",))])
        reference = str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
        result = call(provider, WorkRequest("another-day-answer", "tomorrow", "owner", "direct",
            ({"move": "date", "thing": first, "when": "tomorrow"},), answers=(reference,)))
        assert result["status"] == "accepted"
        saved = next(record for record in records(fake) if first in record.supersedes)
        assert saved.data["dates"]["planned"]["day"] == "2026-10-01"
        assert saved.evidence[-2].excerpt == "Today, another day, or drop?"
    finally:
        log.close()


@pytest.mark.parametrize("explicit_answer", [True, False])
def test_dropped_park_leaves_review_open_for_corrected_answer(tmp_path, explicit_answer):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        first = call(provider, request())["things"][0]["id"]
        log.record_surfacings([Surfacing(NOW, "review", first, "a" * 64, "stalled", {},
            question="Park or drop?", offered=("park", "drop"))])
        reference = str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
        head = fake.branch
        words = "Park Pay venue invoice until next blursday"
        change = WorkRequest("dropped-park", words, "owner", "direct", (
            {"move": "new", "title": "Call dentist"},
            {"move": "park", "thing": first, "until": "next blursday", "words": words},
        ), answers=(reference,) if explicit_answer else ())
        if explicit_answer:
            with pytest.raises(BrainRuntimeError, match="Pay venue invoice: parking was not saved") as error:
                call(provider, change)
            assert error.value.field == "until" and error.value.fix
            assert error.value.message == "Pay venue invoice: parking was not saved: say which day to park it until. Nothing was saved."
            assert fake.branch == head and len(records(fake)) == 1
        else:
            reply = call(provider, change)
            assert reply["status"] == "accepted"
            assert "Pay venue invoice: parking was not saved: say which day to park it until." in reply["say"]
        assert log.answer_context(reference).open
        corrected = call(provider, WorkRequest("corrected-park", "tomorrow", "owner", "direct",
            ({"move": "park", "thing": first, "until": "tomorrow"},), answers=(reference,)))
        assert corrected["status"] == "accepted" and not log.answer_context(reference).open
        current = next(r for r in records(fake) if r.id == corrected["things"][0]["record_id"])
        assert current.state == "parked" and current.data["parked_until"] == "2026-10-01"
    finally:
        log.close()


@pytest.mark.parametrize("message,suggested,title", [
    ("no", "Wifi payment", "Wifi payment"),
    ("yes", "Wifi payment", "Invented project"),
    ("yes", None, "Wifi payment"),
])
def test_rejected_suggested_name_refuses_whole_change_and_keeps_review_open(
        tmp_path, message, suggested, title):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        first = call(provider, request())["things"][0]["id"]
        log.record_surfacings([Surfacing(NOW, "review", first, "a" * 64, "needs_you",
            {"suggested_name": suggested}, question="Its name?", offered=("title",))])
        reference = str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
        head = fake.branch
        before = [r.as_dict() for r in records(fake)]
        with pytest.raises(BrainRuntimeError) as error:
            call(provider, WorkRequest("rejected-name", message, "owner", "direct", (
                {"move": "next_action", "thing": first, "text": "Call bank"},
                {"move": "organise", "thing": first, "title": title}), answers=(reference,)))
        assert error.value.field == "title"
        assert "Pay venue invoice: renaming was not saved: say the name or accept the suggested name" in error.value.message
        assert "nothing was saved" in error.value.message.lower()
        assert "still-open review line" in error.value.fix
        assert fake.branch == head and [r.as_dict() for r in records(fake)] == before
        assert log.answer_context(reference).open
        corrected = call(provider, WorkRequest("corrected-name", "Call it Venue invoice", "owner", "direct",
            ({"move": "organise", "thing": first, "title": "Venue invoice"},), answers=(reference,)))
        assert corrected["status"] == "accepted" and not log.answer_context(reference).open
        assert corrected["things"][0]["title"] == "Venue invoice"
    finally:
        log.close()


@pytest.mark.parametrize("words,when,day", [
    ("done", None, "2026-09-30"),
    ("done already", None, "2026-09-30"),
    ("I did it yesterday", "yesterday", "2026-09-29"),
])
def test_done_already_answers_a_carried_over_line_through_tick(tmp_path, words, when, day):
    from brain_core.review import ItemRecord, MorningBootstrap, MorningInputs, build_morning
    from brain_core import work
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        first = call(provider, request())["things"][0]["id"]
        record = replace(records(fake)[0], data={**records(fake)[0].data, "dates": {"planned": {
            "day": "2026-09-29", "words": "29 September", "read_by": "code", "read_on": "2026-09-28"}}})
        fake.seed({f"records/item/{record.created_at:%Y/%m}/{record.id}.md": render_record(record).encode()})
        morning = build_morning(MorningInputs((ItemRecord(record, "fixture.md"),), (), (), (), {}, NOW,
            bootstrap=MorningBootstrap("a" * 40, frozenset({first}))))
        line = next(line for part in morning.parts for line in part.lines if line.rule == "carried_over")
        assert "done already" in line.text and "done already" in line.allowed_answers
        log.record_surfacings([Surfacing(NOW, "review", first, "a" * 64, line.rule, line.fields_used,
            question=line.text, offered=line.offered)])
        reference = str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
        move = {"move": "tick", "thing": first}
        if when:
            move["when"] = when
        change = WorkRequest("carry-done", words, "owner", "direct", (move,), answers=(reference,))
        result = call(provider, change)
        assert result["status"] == "accepted" and result["things"][0]["done_day"] == day
        latest = next(r for r in records(fake) if r.id == result["things"][0]["record_id"])
        assert latest.state == "done" and work.done_day_of(latest, work.index_things(records(fake))).isoformat() == day
        assert latest.source["words"] == words and not log.answer_context(reference).open
        head = fake.branch
        replay = call(provider, change)
        assert replay["status"] == "duplicate" and replay["things"][0]["done_day"] == day
        assert fake.branch == head
    finally:
        log.close()


def test_door_date_reading_say_and_flags_use_day_words_and_records_keep_iso(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        result = call(provider, WorkRequest("read-day-words", "Passport mid Oct", "owner", "direct", (
            {"move": "new", "title": "Passport", "when": "mid Oct", "reading": {"day": "2026-10-15"}},)))
        assert result["status"] == "accepted"
        assert "I read 'mid Oct' as Thu 15 Oct" in result["say"]
        assert result["things"][0]["flags"][0]["message"] == "I read 'mid Oct' as Thu 15 Oct"
        assert records(fake)[0].data["dates"]["planned"]["day"] == "2026-10-15"
    finally:
        log.close()


@pytest.mark.parametrize("invalid", ["two-days", "wrong-target", "not-offered", "already-answered"])
def test_review_answers_refuse_stale_wrong_or_unoffered_authority(tmp_path, invalid):
    provider, fake, pins, log = setup_door(tmp_path)
    first, second, change = review_merge(provider, fake, log, age=2 if invalid == "two-days" else 0,
        wrong_target=invalid == "wrong-target", offered=("done",) if invalid == "not-offered" else ("merge",),
        answered=invalid == "already-answered")
    head = fake.branch
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, change)
    assert error.value.fix and fake.branch == head
    log.close()


def test_more_than_25_outputs_refuses_the_entire_change(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    head = fake.branch
    parts = [{"title": f"part {number}"} for number in range(20)]
    message = "Create Alpha and Beta with " + "; ".join(part["title"] for part in parts)
    change = WorkRequest("too-many", message, "owner", "direct", (
        {"move": "new", "title": "Alpha"}, {"move": "parts", "thing": "#1", "parts": parts},
        {"move": "new", "title": "Beta"}, {"move": "parts", "thing": "Beta", "parts": parts}))
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, change)
    assert "nothing was saved" in error.value.message.lower() and error.value.fix
    assert fake.branch == head and not records(fake)
    log.close()


def test_storage_limit_says_nothing_was_saved(tmp_path, monkeypatch):
    provider, fake, pins, log = setup_door(tmp_path)
    head = fake.branch
    def refuse(**kwargs):
        raise GitHubStorageError("github_scan_limit", "fixture record limit")
    monkeypatch.setattr(provider._storage, "publish_records", refuse)
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, request())
    assert "nothing was saved" in error.value.message.lower() and error.value.fix
    assert fake.branch == head and not records(fake)
    log.close()


@pytest.mark.parametrize("field", ["evidence", "owners", "vaults"])
def test_replay_never_copies_tampered_envelopes_to_force_equality(tmp_path, field):
    provider, fake, pins, log = setup_door(tmp_path)
    call(provider, request())
    [saved] = records(fake)
    changed = saved.as_dict()
    if field == "evidence":
        changed[field][0]["actor"] = "system:unregistered"
    else:
        changed[field] = ["foreign-owner" if field == "owners" else "vault://foreign"]
    tampered = record_from_mapping(changed)
    fake.seed({f"records/item/{saved.created_at:%Y/%m}/{saved.id}.md": render_record(tampered).encode()})
    head = fake.branch
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, request())
    if field in ("owners", "vaults"):
        # Exact replay discovery now admits only the authenticated owner before
        # reconstructing the receipt or consulting derivative calendar state.
        assert error.value.code == "ACCESS_DENIED" and error.value.field == "scope"
        assert error.value.fix == "use the authorized personal owner"
    else:
        assert error.value.code in ("brain.conflict", "brain.record.exists") and error.value.fix
    assert fake.branch == head and records(fake)[0].as_dict() == changed
    log.close()


def test_door_successor_preserves_prior_evidence_authors_and_undo_is_exact_caller(tmp_path):
    provider, fake, pins, log = setup_door(tmp_path)
    first = call(provider, request())
    [saved] = records(fake)
    prior = saved.as_dict()
    prior["evidence"][0]["actor"] = "nic"
    fake.seed({f"records/item/{saved.created_at:%Y/%m}/{saved.id}.md": render_record(record_from_mapping(prior)).encode()})
    changed = call(provider, WorkRequest("authored", "I started Pay venue invoice", "owner", "direct",
        ({"move": "start", "thing": first["things"][0]["id"]},)))
    current = next(record for record in records(fake) if record.id == changed["things"][0]["record_id"])
    assert current.evidence[0].actor == "nic" and current.evidence[-1].actor == "hermes-wa"
    assert current.vaults == (WORK.owner_locator,) and current.owners == (WORK.owner_id,)
    head = fake.branch
    provider.door_participant = "door-next"
    with pytest.raises(BrainRuntimeError) as error:
        call(provider, WorkRequest("undo-other", "Undo my start", "owner", "direct",
            ({"move": "undo", "change": "authored", "words": "Undo my start"},)), caller="door-next")
    assert error.value.code == "brain.not_found" and error.value.fix
    assert fake.branch == head
    log.close()


@pytest.mark.parametrize("name_message,new_title", [("yes","Wifi payment"),("Save","Wifi payment"),("Call it Wifi job","Wifi job")])
def test_name_only_save_keeps_independent_question_on_checked_door(tmp_path,name_message,new_title):
    provider, fake, pins, log = setup_door(tmp_path)
    try:
        first = call(provider, request())["things"][0]["id"]
        record = replace(records(fake)[0], data={**records(fake)[0].data, "review_marks": [{"mark":"question", "text":"Its name?"},{"mark":"question", "text":"What is the next physical action?"}]})
        fake.seed({f"records/item/{record.created_at:%Y/%m}/{record.id}.md": render_record(record).encode()})
        log.record_surfacings([Surfacing(NOW, "review", first, "a" * 64, "needs_you", {"suggested_name":"Wifi payment", "marks":["question"]}, question="Its name?", offered=("title",))])
        reference=str(log._connection.execute("SELECT id FROM surfacings ORDER BY id DESC LIMIT 1").fetchone()["id"])
        result=call(provider, WorkRequest("name-only-save", name_message, "owner", "direct", ({"move":"organise", "thing":first, "title":new_title},), answers=(reference,)))
        assert result["status"] == "accepted"
        latest=next(r for r in records(fake) if r.id == result["things"][0]["record_id"])
        assert latest.title == new_title
        assert latest.data["review_marks"] == [{"mark":"question", "text":"What is the next physical action?"}]
        assert log.answer_context(reference).open
    finally:
        log.close()
