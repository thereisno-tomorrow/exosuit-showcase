"""Synthetic owner interactions against GT's actual SQLite outbox, no network."""
import asyncio
from dataclasses import replace
import hashlib
import json

import pytest

from gremlin_town.contracts import Actor, MessageKind, Packet, ReplyRequest
from gremlin_town.domain import GremlinTownError
from gremlin_town.review_card.diablo import DiabloRefused, ReviewDiablo, parse_draft
from gremlin_town.transport.framing import parse_wire_message
from tests.dashboard.test_ask import hub as dashboard_hub, OWNER, REF, payload as old_payload

BASE = {"day": "2026-10-02", "line_ref": "fixture-line", "item_ref": REF}
DRAFT = {"steps": [{"title": "Compare the fixture options", "detail": "Use current fixture terms"},
                   {"title": "Choose a fixture option", "detail": "Record the chosen fixture"}]}


def payload(**extra):
    return {**BASE, "operation_id": "fixture-ask", "expected": old_payload()["expected"],
            "action": "research", "text": "", **extra}


@pytest.fixture
def hub(dashboard_hub):
    hub = dashboard_hub
    hub.saves = []
    hub.item["offered"] = ("parts",)
    async def authorize(owner, day, line, ref):
        hub.reads.append((owner, day, line, ref))
        return hub.item if (day, line, ref) == (BASE["day"], BASE["line_ref"], REF) else None
    async def saver(request, *, owner_id):
        hub.saves.append((request, owner_id))
        return {"outcome": "accepted"}
    hub.ask = ReviewDiablo(hub.service, owner_id=OWNER, sender_participant="fixture-sender",
                          recipient_participant="fixture-recipient", project="fixture-project",
                          authorize_item=authorize, saver=saver, enabled=True)
    return hub


def send(hub, value=None, owner=OWNER):
    return asyncio.run(hub.ask.send(value or payload(), owner_id=owner))


def read(hub, value=None, owner=OWNER):
    return asyncio.run(hub.ask.read(value or BASE, owner_id=owner))


def operation(hub, key="fixture-ask"):
    return hub.store.operation_by_client("fixture-sender", hub.ask.prefix + key)


def reply(hub, value=None, key="fixture-ask"):
    hub.worker.dispatch_once()
    request = hub.store.request(operation(hub, key).request_id)
    text = json.dumps(value or DRAFT)
    packet = Packet(1, MessageKind.RESULT, None, "Fixture draft", text, {}, (), "", "", "private")
    hub.service.requests_reply(Actor("fixture-recipient"), ReplyRequest(
        "fixture-reply-" + key, request.request_id, request.revision, MessageKind.RESULT, packet))
    return text


def save_payload(hub):
    draft = read(hub)
    return {**BASE, "operation_id": "fixture-save", "draft_operation_id": draft["operation_id"],
            "draft_sha256": draft["draft_sha256"]}


def test_selected_action_exact_words_and_binding_use_existing_outbox(hub):
    words = "  Use only official fixture sources.\nThen compare.  "
    result = send(hub, payload(action="words", text=words))
    assert result["state"] == "waiting" and not hub.saves and hub.provider.send_attempts == 0
    wire = parse_wire_message(operation(hub).payload)
    assert wire.packet.context == words
    assert all(wire.packet.constraints[key] == value for key, value in BASE.items())
    assert wire.packet.sources == [{"type": "brain_item", "ref": REF, **old_payload()["expected"]}]
    assert "Save nothing" in wire.packet.next_action


def test_disabled_refuses_before_policy_or_store_access(hub):
    hub.ask.enabled = False
    with pytest.raises(DiabloRefused, match="not switched on"):
        send(hub)
    assert not hub.reads and not hub.store.fetchall("SELECT * FROM requests")


def test_only_accepted_recipient_reply_is_a_draft_and_read_does_not_fetch_provider(hub, monkeypatch):
    send(hub)
    text = reply(hub)
    assert read(hub)["state"] == "waiting"  # merely queued result is not proof
    hub.worker.dispatch_once()
    monkeypatch.setattr(hub.service, "requests_read", lambda *a: pytest.fail("provider read must not run"))
    before_events = len(hub.store.fetchall("SELECT * FROM events"))
    draft = read(hub)
    assert draft["state"] == "draft" and draft["steps"] == DRAFT["steps"]
    assert draft["draft_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert len(hub.store.fetchall("SELECT * FROM events")) == before_events
    assert not hub.saves


def test_poll_revision_is_local_and_changes_only_with_associated_gt_state(hub, monkeypatch):
    async def forbidden(*args):
        pytest.fail("poll must not read Brain or calendar")
    before = asyncio.run(hub.ask.revision(BASE["day"]))
    send(hub)
    sent = asyncio.run(hub.ask.revision(BASE["day"]))
    assert before != sent
    reply(hub)
    waiting = asyncio.run(hub.ask.revision(BASE["day"]))
    hub.worker.dispatch_once()
    monkeypatch.setattr(hub.ask, "authorize_item", forbidden)
    monkeypatch.setattr(hub.service, "requests_read", forbidden)
    accepted = asyncio.run(hub.ask.revision(BASE["day"]))
    assert waiting != accepted
    assert asyncio.run(hub.ask.revision(BASE["day"])) == accepted
    hub.policy.set_enabled("fixture-recipient", False)
    with pytest.raises(GremlinTownError):
        asyncio.run(hub.ask.revision(BASE["day"]))


def test_save_resolves_draft_itself_and_uses_exact_tap_with_raw_item_proof(hub):
    send(hub)
    text = reply(hub)
    hub.worker.dispatch_once()
    answer = asyncio.run(hub.ask.savedraft(save_payload(hub), owner_id=OWNER))
    assert answer["outcome"] == "accepted" and len(hub.saves) == 1
    request, owner = hub.saves[0]
    assert owner == OWNER and request.message == "Save these steps" and request.answers == (BASE["line_ref"],)
    assert request.expected_records == {REF: old_payload()["expected"]}
    assert request.moves[0]["parts"] == [{"title": step["title"], "details": step["detail"]} for step in DRAFT["steps"]]
    assert request.moves[0]["approved_draft"] == {"request_id": operation(hub).request_id,
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "author": "Diablo"}


def test_prepare_save_freezes_checked_request_before_any_brain_write(hub):
    send(hub)
    reply(hub)
    hub.worker.dispatch_once()
    request = asyncio.run(hub.ask.prepare_save(save_payload(hub), owner_id=OWNER))
    assert request.message == "Save these steps" and request.operation_id == "fixture-save"
    assert request.expected_records == {REF: old_payload()["expected"]}
    assert request.answers == (BASE["line_ref"],) and not hub.saves
    hub.item.clear()
    with pytest.raises(DiabloRefused):
        asyncio.run(hub.ask.prepare_save({**BASE, "operation_id": "fixture-save", "draft_operation_id": "fixture-ask",
            "draft_sha256": "a" * 64}, owner_id=OWNER))


def test_next_only_line_keeps_next_action_and_one_detail(hub):
    hub.item["offered"] = ("next_action",)
    with pytest.raises(DiabloRefused, match="does not offer"):
        send(hub)
    send(hub, payload(action="next"))
    reply(hub, {"steps": DRAFT["steps"][:1]})
    hub.worker.dispatch_once()
    asyncio.run(hub.ask.savedraft(save_payload(hub), owner_id=OWNER))
    change = hub.saves[0][0]
    assert change.message == "Save this step" and change.moves[0]["move"] == "next_action"
    assert change.moves[0]["text"] == DRAFT["steps"][0]["title"]
    assert change.moves[0]["details"] == DRAFT["steps"][0]["detail"]


def test_existing_chase_offer_drafts_a_nudge_but_cannot_save_or_send_it(hub):
    with pytest.raises(DiabloRefused, match="does not offer"):
        send(hub, payload(action="nudge"))
    hub.item["offered"] = ("draft a nudge", "leave it")
    send(hub, payload(action="nudge"))
    text = "Hi Fixture, could you send the current status?\nThanks."
    reply(hub, {"draft": text})
    hub.worker.dispatch_once()
    draft = read(hub)
    assert draft["state"] == "nudge" and draft["text"] == text and draft["steps"] == []
    with pytest.raises(DiabloRefused, match="does not save or send"):
        asyncio.run(hub.ask.savedraft(save_payload(hub), owner_id=OWNER))
    refinement = {**BASE, "operation_id": "fixture-refine", "previous_operation_id": "fixture-ask",
                  "expected": old_payload()["expected"], "text": "Make it shorter."}
    assert asyncio.run(hub.ask.refine(refinement, owner_id=OWNER))["state"] == "waiting"
    wire = parse_wire_message(operation(hub, "fixture-refine").payload)
    assert wire.packet.constraints["selected_action"] == "nudge" and wire.packet.context == refinement["text"]
    assert not hub.saves


def test_refinement_uses_same_association_and_old_draft_cannot_save(hub):
    send(hub)
    reply(hub)
    hub.worker.dispatch_once()
    old_save = save_payload(hub)
    refinement = {**BASE, "operation_id": "fixture-refine", "previous_operation_id": "fixture-ask",
                  "expected": old_payload()["expected"], "text": "Be specific for the second fixture step."}
    result = asyncio.run(hub.ask.refine(refinement, owner_id=OWNER))
    assert result["state"] == "waiting"
    refined = parse_wire_message(operation(hub, "fixture-refine").payload)
    assert refined.packet.context == refinement["text"]
    assert json.loads(refined.packet.constraints["previous_reply"]) == DRAFT
    with pytest.raises(DiabloRefused, match="newer draft"):
        asyncio.run(hub.ask.savedraft(old_save, owner_id=OWNER))
    assert not hub.saves
    reply(hub, {"steps": DRAFT["steps"][:1]}, key="fixture-refine")
    hub.worker.dispatch_once()
    assert read(hub)["operation_id"] == "fixture-refine"
    asyncio.run(hub.ask.savedraft(save_payload(hub), owner_id=OWNER))
    assert len(hub.saves) == 1


def test_lost_send_reply_has_one_retry_payload_and_no_duplicate_request(hub, monkeypatch):
    original = hub.service.requests_create
    def lose(actor, command):
        original(actor, command)
        raise TimeoutError("fictional lost reply")
    monkeypatch.setattr(hub.service, "requests_create", lose)
    with pytest.raises(TimeoutError): send(hub)
    monkeypatch.setattr(hub.service, "requests_create", original)
    hub.item["record_sha256"] = "b" * 64
    assert send(hub)["state"] == "stale"
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1
    with pytest.raises(DiabloRefused, match="different words"):
        send(hub, payload(action="words", text="Changed"))


def test_reload_reads_original_request_and_prevents_a_second_initial_send(hub):
    send(hub)
    assert read(hub)["operation_id"] == "fixture-ask"
    with pytest.raises(DiabloRefused, match="already has"):
        send(hub, payload(operation_id="new-browser-key"))
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1


def test_simultaneous_double_tap_cannot_start_two_requests(hub):
    async def both():
        return await asyncio.gather(hub.ask.send(payload(), owner_id=OWNER),
            hub.ask.send(payload(operation_id="second-tap"), owner_id=OWNER), return_exceptions=True)
    results = asyncio.run(both())
    assert sum(isinstance(result, DiabloRefused) for result in results) == 1
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1


def test_one_next_step_reply_cannot_be_replaced_with_multiple_steps(hub):
    hub.item["offered"] = ("next_action",)
    send(hub, payload(action="next"))
    reply(hub)
    hub.worker.dispatch_once()
    assert read(hub)["state"] == "invalid"
    value = {**BASE, "operation_id": "save-too-many", "draft_operation_id": "fixture-ask",
             "draft_sha256": hashlib.sha256(json.dumps(DRAFT).encode()).hexdigest()}
    with pytest.raises(DiabloRefused, match="one next step"):
        asyncio.run(hub.ask.savedraft(value, owner_id=OWNER))
    assert not hub.saves


@pytest.mark.parametrize("change", [{"recipient": "foreign"}, {"owner_id": "foreign"}, {"moves": []},
                                   {"action": "research", "text": "Hidden text"}, {"action": "words", "text": ""},
                                   {"expected": {"record_id": "version", "sha256": "wrong"}},
                                   {"action": []}, {"day": "today"}])
def test_forged_request_fields_are_refused(hub, change):
    with pytest.raises(DiabloRefused): send(hub, payload(**change))
    assert not hub.store.fetchall("SELECT * FROM requests")


def test_foreign_owner_line_and_current_policy_revocation_deny_send_read_and_save(hub):
    send(hub)
    reply(hub)
    hub.worker.dispatch_once()
    saved = save_payload(hub)
    for action in (lambda: send(hub, owner="foreign"), lambda: read(hub, {**BASE, "line_ref": "foreign"}),
                   lambda: asyncio.run(hub.ask.savedraft(saved, owner_id="foreign"))):
        with pytest.raises(DiabloRefused): action()
    hub.policy.set_enabled("fixture-recipient", False)
    for action in (lambda: send(hub), lambda: read(hub), lambda: asyncio.run(hub.ask.savedraft(saved, owner_id=OWNER))):
        with pytest.raises(GremlinTownError): action()
    assert not hub.saves


@pytest.mark.parametrize("draft", ["not JSON", '{"steps":[],"steps":[]}', json.dumps({"steps": []}),
    json.dumps({"steps": [{"title": "Paragraph\nsecond line", "detail": "Short"}]}),
    json.dumps({"steps": [{"title": "x" * 121, "detail": "Short"}]}),
    json.dumps({"steps": [{"title": "Short", "detail": "x" * 401}]}),
    json.dumps({"steps": [{"title": "Short", "detail": "Short", "done": True}]}),
    json.dumps({"steps": DRAFT["steps"], "owner_id": "foreign"})])
def test_draft_parser_rejects_injected_fields_and_unbounded_text(draft):
    with pytest.raises(DiabloRefused): parse_draft(draft)


def test_save_cannot_accept_client_supplied_draft_or_wrong_hash(hub):
    send(hub)
    reply(hub)
    hub.worker.dispatch_once()
    value = save_payload(hub)
    for payload in ({**value, "steps": DRAFT["steps"]}, {**value, "draft_sha256": "f" * 64}):
        with pytest.raises(DiabloRefused): asyncio.run(hub.ask.savedraft(payload, owner_id=OWNER))
    assert not hub.saves


def test_stale_item_is_visible_as_stale_and_save_keeps_original_door_proof(hub):
    send(hub)
    reply(hub)
    hub.worker.dispatch_once()
    value = save_payload(hub)
    hub.item["record_sha256"] = "c" * 64
    assert read(hub)["state"] == "stale"
    # The door owns atomic proof verification and accepted-retry handling. Never
    # refresh a pending save's proof, or a stale draft could overwrite new work.
    asyncio.run(hub.ask.savedraft(value, owner_id=OWNER))
    assert hub.saves[0][0].expected_records[REF]["sha256"] == "a" * 64
