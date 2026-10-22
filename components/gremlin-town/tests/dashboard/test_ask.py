"""Real GT persistence and fake delivery; all records/keys below are invented."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gremlin_town.contracts import Actor, MessageKind, Packet, ReplyRequest
from gremlin_town.dashboard.app import COOKIE, create_app, require_owner, require_write_owner
from gremlin_town.dashboard.ask import AskRefused, DashboardAsk, validate
from gremlin_town.dashboard.ask_routes import create_ask_router
from gremlin_town.dashboard.settings import DashboardSettings
from gremlin_town.domain import GremlinTownError, Participant, Policy
from gremlin_town.mcp.brain_runtime import BrainRuntimeError
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.transport.fake import FakeOutcome, FakeTransport
from gremlin_town.transport.framing import parse_wire_message
from gremlin_town.workers import OutboxWorker

NOW = datetime(2026, 10, 2, 9, tzinfo=UTC)
ORIGIN = "https://dashboard.example.test"
OWNER = "fictional-owner"
REF = "fictional-thing"


def payload(**extra):
    return {"operation_id": "fictional-op", "ref": REF, "action": "get_ready", "text": "  Draft two options.\nKeep it short.  ",
            "expected": {"record_id": "fixture-version", "sha256": "a" * 64}, **extra}


@pytest.fixture
def hub(tmp_path):
    policy = Policy([
        Participant(name, name, name + "@example.test", frozenset({"fixture-project"}), frozenset({"private"}))
        for name in ("fixture-sender", "fixture-recipient")
    ], frozenset({("fixture-project", a, b, "private") for a, b in
                  (("fixture-sender", "fixture-recipient"), ("fixture-recipient", "fixture-sender"))}))
    store = SQLiteStore(tmp_path / "gt.sqlite", now=lambda: NOW)
    provider = FakeTransport(clock=lambda: NOW)
    service = GremlinTownService(store, policy, provider=provider, now=lambda: NOW)
    worker = OutboxWorker(store, policy, provider, now=lambda: NOW)
    item = {"ref": REF, "record_id": "fixture-version", "record_sha256": "a" * 64}
    reads = []
    async def authorize(owner, ref):
        reads.append((owner, ref))
        return item if item.get("ref") == ref else None
    ask = DashboardAsk(service, owner_id=OWNER, sender_participant="fixture-sender",
                       recipient_participant="fixture-recipient", project="fixture-project", authorize_item=authorize)
    result = SimpleNamespace(ask=ask, service=service, store=store, policy=policy, provider=provider,
                             worker=worker, item=item, reads=reads)
    yield result
    store.close()


def send(hub, value=None, owner=OWNER):
    return asyncio.run(hub.ask.send(value or payload(), owner_id=owner))


def read(hub, ref=REF, owner=OWNER):
    return asyncio.run(hub.ask.read(ref, owner_id=owner))


def operation(hub, key="fictional-op"):
    return hub.store.operation_by_client("fixture-sender", hub.ask.prefix + key)


def test_request_keeps_exact_words_action_reference_and_current_proof(hub):
    result = send(hub)
    assert result["text"] == "Sent to Diablo" and result["delivery"] == "queued"
    assert result["words"] == payload()["text"]
    assert hub.provider.send_attempts == 0
    wire = parse_wire_message(operation(hub).payload)
    assert wire.packet.objective == wire.packet.next_action == "Get it ready"
    assert wire.packet.context == payload()["text"]
    assert wire.packet.sources == [{"type": "brain_item", "ref": REF, **payload()["expected"]}]
    assert wire.sender == "fixture-sender" and wire.recipient == "fixture-recipient"
    assert hub.reads == [(OWNER, REF)]


def test_button_alone_uses_only_the_selected_label(hub):
    send(hub, payload(action="do_it", text=""))
    packet = parse_wire_message(operation(hub).payload).packet
    assert packet.objective == packet.next_action == "Do it" and packet.context == ""


@pytest.mark.parametrize("extra", [{"owner_id": "another"}, {"recipient": "another"}, {"scope": "fleet"},
                                   {"moves": []}, {"request_id": "guessed"}, {"text": []}, {"action": {}},
                                   {"expected": {"record_id": "v", "sha256": "not-a-hash"}},
                                   {"operation_id": "../outside"}, {"text": "x" * 4001}])
def test_untrusted_fields_and_malformed_requests_are_rejected(hub, extra):
    with pytest.raises(AskRefused):
        send(hub, payload(**extra))
    assert not hub.store.fetchall("SELECT * FROM requests")


def test_stale_new_proof_refuses_but_exact_retry_survives_record_change(hub):
    with pytest.raises(AskRefused, match="changed"):
        send(hub, payload(expected={"record_id": "old", "sha256": "b" * 64}))
    first = send(hub)
    hub.item["record_sha256"] = "c" * 64
    assert send(hub) == first
    with pytest.raises(AskRefused, match="different words"):
        send(hub, payload(text="Different instruction"))
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1


def test_unknown_response_after_commit_retries_one_canonical_request(hub, monkeypatch):
    original = hub.service.requests_create
    def lose_reply(actor, command):
        original(actor, command)
        raise TimeoutError("fictional private detail must not escape")
    monkeypatch.setattr(hub.service, "requests_create", lose_reply)
    with pytest.raises(TimeoutError):
        send(hub)
    monkeypatch.setattr(hub.service, "requests_create", original)
    assert send(hub)["delivery"] == "queued"
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1
    hub.worker.dispatch_once()
    assert send(hub)["delivery"] == "accepted"
    assert hub.provider.send_attempts == 1


def test_concurrent_same_request_uses_one_outbox_row(hub):
    with ThreadPoolExecutor(max_workers=4) as executor:
        outputs = list(executor.map(lambda _: send(hub), range(8)))
    assert {x["operation_id"] for x in outputs} == {"fictional-op"}
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1


def test_uncertain_provider_delivery_never_creates_another_request(hub):
    send(hub)
    hub.provider.outcome = FakeOutcome.TIMEOUT_AFTER_ACCEPTANCE
    hub.worker.dispatch_once()
    result = send(hub)
    assert result["delivery"] == "uncertain" and "needs checking" in result["text"]
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1
    assert hub.provider.send_attempts == 1


def test_owner_current_visibility_and_route_are_checked_on_send_replay_and_read(hub):
    send(hub)
    with pytest.raises(AskRefused):
        read(hub, owner="another-owner")
    hub.item.clear()
    for action in (lambda: send(hub), lambda: read(hub)):
        with pytest.raises(AskRefused):
            action()
    hub.item.update(ref=REF, record_id="fixture-version", record_sha256="a" * 64)
    hub.policy.set_enabled("fixture-recipient", False)
    for action in (lambda: send(hub), lambda: read(hub)):
        with pytest.raises(GremlinTownError):
            action()
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1


def test_reader_never_exposes_unassociated_requests_or_another_owners_namespace(hub):
    send(hub)
    command = hub.ask._command(payload(operation_id="other-request"))
    hub.service.requests_create(hub.ask.actor, replace(command, client_operation_id="other-surface"))
    other = DashboardAsk(hub.service, owner_id="another-owner", sender_participant="fixture-sender",
                         recipient_participant="fixture-recipient", project="fixture-project",
                         authorize_item=hub.ask.authorize_item)
    asyncio.run(other.send(payload(), owner_id="another-owner"))
    assert len(read(hub)["requests"]) == 1
    with pytest.raises(AskRefused):
        read(hub, operation(hub).request_id)


def test_result_appears_only_after_recipient_result_is_accepted(hub):
    send(hub)
    hub.worker.dispatch_once()
    request = hub.store.request(operation(hub).request_id)
    reply = Packet(1, MessageKind.RESULT, None, "Two options", "<script>fictional</script>\nOption A\nOption B",
                   {}, (), "", "", "private")
    hub.service.requests_reply(Actor("fixture-recipient"), ReplyRequest(
        "fictional-reply", request.request_id, request.revision, MessageKind.RESULT, reply))
    assert read(hub)["requests"][0]["result"] is None
    hub.worker.dispatch_once()
    result = read(hub)["requests"][0]
    assert result["text"] == "Diablo's reply"
    assert result["result"] == {"objective": reply.objective, "text": reply.context}


@pytest.fixture
def browser(hub, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"fictional-test-session-secret!!!!")
    secret.chmod(0o600)
    settings = DashboardSettings(enabled=True, reads=True, ask_diablo=True, writes=False,
                                 origin=ORIGIN, rp_id="dashboard.example.test", owner_id=OWNER,
                                 state_file=tmp_path / "auth.sqlite", session_secret_file=secret)
    router = create_ask_router(hub.ask, settings=settings, require_owner=require_owner,
                              require_write_owner=require_write_owner)
    app = create_app(settings, router=router)
    # This fixture installs a synthetic local session. The auth suite exercises
    # real registration signatures. Request auth/Origin/CSRF here remain real.
    auth = app.state.dashboard_auth
    with auth.store.transaction() as db:
        db.execute("INSERT INTO credentials(id,public_key,sign_count) VALUES (?,?,?)",
                   (b"fixture-key", b"fixture-public", 0))
    cookie = auth._new_session(b"fixture-key")
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(COOKIE, cookie)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/auth/session").json()["csrf"]}
    return client, headers, settings


def test_real_session_origin_and_csrf_boundary_before_any_request(browser, hub):
    client, headers, _ = browser
    assert client.post("/api/ask-diablo", json=payload()).status_code == 403
    assert client.post("/api/ask-diablo", json=payload(), headers={**headers, "Origin": "https://other.example.test"}).status_code == 403
    assert client.post("/api/ask-diablo", json=payload(), headers={**headers, "X-CSRF-Token": "bad"}).status_code == 403
    assert not hub.store.fetchall("SELECT * FROM requests")
    assert client.post("/api/ask-diablo", json=payload(), headers=headers).status_code == 200
    assert client.get("/api/ask-diablo", params={"ref": REF}).status_code == 200
    client.cookies.clear()
    assert client.get("/api/ask-diablo", params={"ref": REF}).status_code == 401
    assert client.post("/api/ask-diablo", json=payload(), headers=headers).status_code == 401


@pytest.mark.parametrize("gate", ["enabled", "reads", "ask_diablo"])
def test_each_ask_gate_is_enforced_independent_of_writes(browser, hub, gate):
    client, headers, settings = browser
    object.__setattr__(settings, gate, False)
    assert client.post("/api/ask-diablo", json=payload(), headers=headers).status_code == 403
    assert client.get("/api/ask-diablo", params={"ref": REF}).status_code == 403
    assert not hub.store.fetchall("SELECT * FROM requests")


def test_routes_reject_unknown_query_duplicate_fields_and_sanitize_failures(browser, hub, monkeypatch):
    client, headers, _ = browser
    assert client.get("/api/ask-diablo?ref=x&ref=y").status_code == 400
    assert client.get("/api/ask-diablo?ref=x&request_id=guessed").status_code == 400
    assert client.post("/api/ask-diablo", content='{"action":"get_ready","action":"do_it"}',
                       headers={**headers, "Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/ask-diablo", content="x" * 25000,
                       headers={**headers, "Content-Type": "application/json"}).status_code == 413
    def fail(*args):
        raise RuntimeError("fictional-secret-data")
    monkeypatch.setattr(hub.ask, "authorize_item", fail)
    response = client.post("/api/ask-diablo", json=payload(), headers=headers)
    assert response.status_code == 503 and response.json()["retryable"] is True
    assert "fictional-secret-data" not in response.text
    assert not hub.store.fetchall("SELECT * FROM requests")


def test_brain_owner_policy_refusal_cannot_send_or_read_old_request(browser, hub, monkeypatch):
    client, headers, _ = browser
    send(hub)
    def revoked(*args):
        raise BrainRuntimeError("ACCESS_DENIED", "fictional private policy detail")
    monkeypatch.setattr(hub.ask, "authorize_item", revoked)
    for response in (client.post("/api/ask-diablo", json=payload(), headers=headers),
                     client.get("/api/ask-diablo", params={"ref": REF})):
        assert response.status_code == 403 and response.json()["retryable"] is False
        assert "fictional private" not in response.text
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1


def test_reopening_service_reuses_persisted_association_without_another_store(hub):
    send(hub)
    hub.ask = DashboardAsk(hub.service, owner_id=OWNER, sender_participant="fixture-sender",
                           recipient_participant="fixture-recipient", project="fixture-project",
                           authorize_item=hub.ask.authorize_item)
    assert read(hub)["requests"][0]["words"] == payload()["text"]
    send(hub)
    assert len(hub.store.fetchall("SELECT * FROM requests")) == 1


def test_unchanged_twenty_request_poll_is_local_and_adds_no_events(hub, monkeypatch):
    for index in range(20):
        send(hub, payload(operation_id=f"fixture-history-{index}"))
        hub.worker.dispatch_once()
    def forbidden(*args):
        raise AssertionError("A dashboard reply poll must never fetch a provider thread")
    monkeypatch.setattr(hub.provider, "fetch_thread", forbidden)
    monkeypatch.setattr(hub.service, "requests_read", forbidden)
    before = len(hub.store.fetchall("SELECT sequence FROM events"))
    for _ in range(2):
        requests = read(hub)["requests"]
        assert len(requests) == 20
        assert all(row["expected"] == payload()["expected"] for row in requests)
    assert len(hub.store.fetchall("SELECT sequence FROM events")) == before
    # Local reads still apply the service policy after the earlier visibility
    # callback: changing the GT caller's enrollment cannot expose old results.
    hub.policy.set_enabled("fixture-sender", False)
    with pytest.raises(GremlinTownError):
        read(hub)
