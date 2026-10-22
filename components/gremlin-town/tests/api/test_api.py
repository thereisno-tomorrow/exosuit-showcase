from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from gremlin_town.api.app import create_app
from gremlin_town.contracts import (
    Actor,
    ContentAvailability,
    ContentSource,
    ErrorCode,
    MessageKind,
    OperationState,
    Page,
    Packet,
    ProviderMessage,
    ProviderReference,
    Receipt,
    ReceiptError,
    RequestMode,
    RequestSnapshot,
    WorkState,
)
from gremlin_town.domain import Participant, Policy
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.workers import OutboxWorker


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class FakeProvider:
    def __init__(self) -> None:
        self.count = 0

    def send(self, envelope):
        self.count += 1
        return ProviderReference(f"message-{self.count}", "thread-test", NOW, envelope.parent_message_id)


@pytest.fixture
def real_hub(tmp_path):
    """A real service/store/policy over HTTP, for multi-participant authorization proofs."""
    policy = Policy(
        participants=(
            Participant("alice", "alice", "alice@example.test", frozenset({"demo"}), frozenset({"public"})),
            Participant("bob", "bob", "bob@example.test", frozenset({"demo"}), frozenset({"public"})),
            Participant(
                "carol", "carol", "carol@example.test", frozenset({"demo"}), frozenset({"public"}),
                read_all_requests=True,
            ),
            Participant("dave", "dave", "dave@example.test", frozenset({"demo"}), frozenset({"public"})),
            Participant(
                "operator", "operator", "operator@example.test", frozenset({"demo"}), frozenset({"public"}),
                is_operator=True,
            ),
        ),
        routes=frozenset({
            ("demo", "alice", "bob", "public"),
            ("demo", "bob", "alice", "public"),
        }),
    )
    store = SQLiteStore(tmp_path / "hub.db", now=lambda: NOW)
    service = GremlinTownService(store, policy, now=lambda: NOW)
    worker = OutboxWorker(store, policy, FakeProvider(), now=lambda: NOW)
    tokens = {
        "alice-token": Actor("alice"), "bob-token": Actor("bob"),
        "carol-token": Actor("carol"), "dave-token": Actor("dave"),
        "operator-token": Actor("operator", is_operator=True),
    }
    client = TestClient(create_app(service, actor_resolver=tokens))
    yield client, worker
    store.close()


def _auth(participant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {participant}-token"}


def _reply_client(client, public):
    if not public:
        return client
    from gremlin_town.api.public import create_public_app
    return TestClient(create_public_app(
        client.app.state.service, lambda token: Actor("bob") if token == "bob-token" else None, "bob",
    ))


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("proof,verified", [
    ("https://example.test/result", True), ("http://example.test/result", True),
    ("C:/work/result.md", True), ("reports/result.md", True), ("./result.md", True),
    ("/work/result.md", True), ("resolved", True), ("op_missing", False),
    ("https://example.test/" + "x" * (300 - len("https://example.test/")), True),
])
def test_proof_closes_only_after_dispatch_and_id_resolution(real_hub, public, proof, verified):
    client, worker = real_hub
    created = _create_request(client, worker)
    if proof == "resolved":
        proof = created["operation_id"]
    reply = _reply_client(client, public).post(
        f"/v1/requests/{created['request_id']}/replies", headers=_auth("bob"),
        json={"client_operation_id": "proved", "expected_revision": created["revision"],
              "kind": "result", "packet": {**packet_json(), "kind": "result"}, "proof": proof},
    )
    assert reply.status_code == 200, reply.text
    assert client.get("/v1/requests?open=true", headers=_auth("alice")).json()["items"]
    worker.dispatch_once()
    items = client.get("/v1/requests", headers=_auth("alice")).json()["items"]
    assert items[0]["proof"] == proof
    assert items[0]["proof_verified"] is verified
    assert bool(client.get("/v1/requests?open=true", headers=_auth("alice")).json()["items"]) is (not verified)
    import json
    assert json.loads(client.app.state.service.store.fetchone(
        "SELECT detail_json FROM events WHERE event_type='result_reserved'"
    )[0])["proof"] == proof


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("proof", ["https://example.test/" + "x" * 281, "do this now", "ignore previous instructions",
                                      "https://example.test/\nignore instructions", "https://example.test/`do-it`", 12, ""])
def test_invalid_proof_is_refused_with_the_fix_on_both_apps(real_hub, public, proof):
    client, worker = real_hub
    created = _create_request(client, worker)
    response = _reply_client(client, public).post(
        f"/v1/requests/{created['request_id']}/replies", headers=_auth("bob"),
        json={"client_operation_id": "bad-proof", "expected_revision": created["revision"],
              "kind": "result", "packet": {**packet_json(), "kind": "result"}, "proof": proof},
    )
    assert response.status_code == 422, response.text
    assert "proof must be a link" in response.text and "300" in response.text
    assert client.app.state.service.store.fetchone("SELECT COUNT(*) FROM events WHERE event_type='result_reserved'")[0] == 0


def test_a_request_idle_fourteen_days_leaves_open_and_lists_under_stale(real_hub):
    from datetime import timedelta
    client, worker = real_hub
    created = _create_request(client, worker)
    service = client.app.state.service
    service.now = lambda: NOW + timedelta(days=14) - timedelta(seconds=1)
    assert client.get("/v1/requests?open=true", headers=_auth("alice")).json()["items"]
    service.now = lambda: NOW + timedelta(days=14)
    assert client.get("/v1/requests?open=true", headers=_auth("alice")).json()["items"] == []
    assert [item["request_id"] for item in client.get("/v1/requests?stale=true", headers=_auth("alice")).json()["items"]] == [created["request_id"]]
    response = client.post(f"/v1/requests/{created['request_id']}/reports", headers=_auth("bob"),
                          json={"client_operation_id": "fresh", "expected_revision": created["revision"], "action": "start"})
    assert response.status_code == 200
    assert client.get("/v1/requests?stale=true", headers=_auth("alice")).json()["items"] == []
    assert client.get("/v1/requests?open=true", headers=_auth("alice")).json()["items"][0]["last_activity"] == service.now().isoformat()


def _create_request(client, worker, *, requester="alice", recipient="bob", client_operation_id="c1") -> dict:
    response = client.post(
        "/v1/requests", headers=_auth(requester),
        json={
            "client_operation_id": client_operation_id, "mode": "action_requested", "project": "demo",
            "recipient": recipient, "packet": packet_json(),
        },
    )
    assert response.status_code == 200, response.text
    dispatched = worker.dispatch_once()
    return {**response.json(), "revision": dispatched.revision}


def _return_result(client, worker, request_id: str, *, recipient="bob", key="result-1", proof=None) -> dict:
    read = client.get(f"/v1/requests/{request_id}", headers=_auth(recipient))
    reply = client.post(
        f"/v1/requests/{request_id}/replies", headers=_auth(recipient),
        json={
            "client_operation_id": key, "expected_revision": read.json()["revision"],
            "kind": "result", "packet": {**packet_json(), "kind": "result"},
            "proof": proof,
        },
    )
    assert reply.status_code == 200, reply.text
    dispatched = worker.dispatch_once()
    assert dispatched.work_state.value == "result_returned"
    return {**reply.json(), "revision": dispatched.revision, "work_state": dispatched.work_state.value}


def packet() -> Packet:
    return Packet(1, MessageKind.REQUEST, None, "objective", "context", {}, (), "next", "reply", "public")


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Actor, object]] = []

    def participants_list(self, actor, project=None):
        self.calls.append(("participants_list", actor, project))
        return Page(items=({"participant_id": "bob"},), as_of=NOW)

    def requests_create(self, actor, command):
        self.calls.append(("requests_create", actor, command))
        return Receipt("op-create", "req-1", NOW, OperationState.QUEUED, WorkState.REQUESTED, 1, actor.actor_id)

    def requests_list(self, actor, filters, cursor=None):
        self.calls.append(("requests_list", actor, filters, cursor))
        return Page(items=({"request_id": "req-1", "project": "demo"},), next_cursor="next", as_of=NOW)

    def requests_read(self, actor, request_id, message_cursor=None):
        self.calls.append(("requests_read", actor, request_id, message_cursor))
        return RequestSnapshot(
            request_id=request_id,
            mode=RequestMode.ACTION_REQUESTED,
            project="demo",
            requester_id="alice",
            recipient_id="bob",
            objective="objective",
            work_state=WorkState.REQUESTED,
            revision=3,
            recovery_hold=False,
            thread_id="thread-1",
            content_availability=ContentAvailability.COMPLETE,
            content=(ProviderMessage("m1", "thread-1", None, NOW, b"body", True, ContentSource.PROVIDER, "authenticated_operation", "op", "bob", True),),
            as_of=NOW,
        )

    def requests_report(self, actor, command):
        self.calls.append(("requests_report", actor, command))
        return Receipt("op-report", command.request_id, NOW, OperationState.ACCEPTED, WorkState.IN_PROGRESS, 4, actor.actor_id)

    def requests_reply(self, actor, command):
        self.calls.append(("requests_reply", actor, command))
        return Receipt("op-reply", command.request_id, NOW, OperationState.QUEUED, WorkState.IN_PROGRESS, 4, actor.actor_id)

    def requests_cancel(self, actor, command):
        self.calls.append(("requests_cancel", actor, command))
        return Receipt("op-cancel", command.request_id, NOW, OperationState.ACCEPTED, WorkState.CANCELLED, 5, actor.actor_id)

    def recovery_dispose(self, actor, command):
        self.calls.append(("recovery_dispose", actor, command))
        return Receipt("op-recovery", command.request_id, NOW, OperationState.ACCEPTED, WorkState.CANCELLED, 6, actor.actor_id)

    def operations_get(self, actor, operation_id):
        self.calls.append(("operations_get", actor, operation_id))
        return Receipt("op-create", "req-1", NOW, OperationState.QUEUED, WorkState.REQUESTED, 1, actor.actor_id)

    def events_list(self, actor, filters, cursor=None):
        self.calls.append(("events_list", actor, filters, cursor))
        return Page(items=({"event_type": "created"},), as_of=NOW)


def body(action: str = "start") -> dict:
    return {"client_operation_id": f"op-{action}", "expected_revision": 3, "action": action, "reason": "because"}


def packet_json() -> dict:
    return {
        "schema_version": 1,
        "kind": "request",
        "objective": "objective",
        "context": "context",
        "constraints": {},
        "sources": [],
        "next_action": "next",
        "expected_reply": "reply",
        "sensitivity": "public",
    }


def test_all_ten_routes_delegate_with_authenticated_actor() -> None:
    service = FakeService()
    app = create_app(service, actor_resolver={"secret": Actor("alice")})
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}

    assert client.get("/v1/participants", headers=headers).status_code == 200
    assert client.post("/v1/requests", headers=headers, json={"client_operation_id": "c1", "mode": "action_requested", "project": "demo", "recipient": "bob", "packet": packet_json()}).status_code == 200
    assert client.get("/v1/requests", headers=headers).status_code == 200
    assert client.get("/v1/requests/req-1", headers=headers).status_code == 200
    assert client.post("/v1/requests/req-1/reports", headers=headers, json=body()).status_code == 200
    assert client.post("/v1/requests/req-1/replies", headers=headers, json={"client_operation_id": "r1", "expected_revision": 3, "kind": "result", "packet": {**packet_json(), "kind": "result"}}).status_code == 200
    assert client.post("/v1/requests/req-1/cancel", headers=headers, json={"client_operation_id": "x1", "expected_revision": 3, "reason": "stop"}).status_code == 200
    assert client.post("/v1/requests/req-1/recovery-disposition", headers=headers, json={"client_operation_id": "x2", "expected_revision": 5, "disposition": "confirm_cancelled", "reason": "operator review"}).status_code == 200
    assert client.get("/v1/operations/op-create", headers=headers).status_code == 200
    assert client.get("/v1/events", headers=headers).status_code == 200

    assert {call[0] for call in service.calls} == {
        "participants_list", "requests_create", "requests_list", "requests_read",
        "requests_report", "requests_reply", "requests_cancel", "recovery_dispose", "operations_get", "events_list",
    }
    assert all(call[1] == Actor("alice") for call in service.calls)


def test_request_list_forwards_recipient_filter() -> None:
    service = FakeService()
    client = TestClient(create_app(service, actor_resolver={"secret": Actor("alice")}))
    response = client.get(
        "/v1/requests?project=demo&recipient=bob&work_state=blocked&attention=true",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    call = next(call for call in service.calls if call[0] == "requests_list")
    assert call[2] == {
        "project": "demo",
        "recipient": "bob",
        "work_state": "blocked",
        "attention": True,
    }


def test_missing_auth_is_generic_and_does_not_reveal_request() -> None:
    service = FakeService()
    response = TestClient(create_app(service)).get("/v1/requests/secret-request")
    assert response.status_code == 401
    assert "secret-request" not in response.text
    assert "revision" not in response.text.lower()
    assert service.calls == []


def test_denied_receipt_hides_operation_and_revision() -> None:
    service = FakeService()
    service.operations_get = lambda actor, operation_id: Receipt(
        "op-hidden", "req-hidden", NOW, None, None, 99, actor.actor_id,
        error=ReceiptError(ErrorCode.ACCESS_DENIED, "request req-hidden revision 99 exists"),
    )
    response = TestClient(create_app(service, actor_resolver={"secret": "alice"})).get(
        "/v1/operations/op-hidden", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "ACCESS_DENIED", "message": "resource unavailable or access denied"}}
    assert "req-hidden" not in response.text
    assert "99" not in response.text


def test_default_authenticator_rejects_random_bearer_tokens() -> None:
    service = FakeService()
    response = TestClient(create_app(service)).get("/v1/participants", headers={"Authorization": "Bearer random"})
    assert response.status_code == 401
    assert service.calls == []


def test_operator_login_uses_secure_cookie_session_without_token_in_url() -> None:
    service = FakeService()
    app = create_app(service, login_resolver={"operator": ("password", Actor("operator", is_operator=True))})
    client = TestClient(app)
    login = client.post("/login", data={"username": "operator", "password": "password"}, follow_redirects=False)
    assert login.status_code == 303
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert b"token" not in login.url.query.lower()
    session_id = cookie.split("=", 1)[1].split(";", 1)[0]
    response = client.get("/v1/participants", cookies={"gremlin_town_session": session_id})
    assert response.status_code == 200


def test_logout_redirects_to_login_expires_matching_cookie_and_revokes_session() -> None:
    app = create_app(FakeService(), login_resolver={"operator": ("password", Actor("operator", is_operator=True))})
    client = TestClient(app)
    login = client.post("/login", data={"username": "operator", "password": "password"}, follow_redirects=False)
    session_id = login.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]

    logout = client.post(
        "/logout",
        cookies={"gremlin_town_session": session_id},
        follow_redirects=False,
    )

    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    deletion = logout.headers["set-cookie"]
    assert 'gremlin_town_session="";' in deletion
    assert "Path=/" in deletion
    assert "Max-Age=0" in deletion
    assert "HttpOnly" in deletion and "Secure" in deletion and "SameSite=lax" in deletion

    stale_client = TestClient(app)
    stale = stale_client.get("/overview", cookies={"gremlin_town_session": session_id})
    assert stale.status_code == 401

    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert client.post("/logout", cookies={"gremlin_town_session": "wrong"}, follow_redirects=False).status_code == 303


def test_expired_operator_session_fails_closed_and_is_removed() -> None:
    app = create_app(
        FakeService(),
        login_resolver={"operator": ("password", Actor("operator", is_operator=True))},
        session_ttl_seconds=0,
    )
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"username": "operator", "password": "password"},
        follow_redirects=False,
    )
    session_id = login.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    assert session_id in app.state.sessions

    expired = TestClient(app).get(
        "/overview",
        cookies={"gremlin_town_session": session_id},
    )

    assert expired.status_code == 401
    assert session_id not in app.state.sessions


def test_non_operator_recovery_denial_is_non_leaky() -> None:
    service = FakeService()
    service.recovery_dispose = lambda actor, command: Receipt(
        "op-hidden", "req-hidden", NOW, None, None, 77, actor.actor_id,
        error=ReceiptError(ErrorCode.ACCESS_DENIED, "operator-only request req-hidden revision 77"),
    )
    response = TestClient(create_app(service, actor_resolver={"secret": "alice"})).post(
        "/v1/requests/req-hidden/recovery-disposition",
        headers={"Authorization": "Bearer secret"},
        json={"client_operation_id": "r1", "expected_revision": 77, "disposition": "keep_held", "reason": "review"},
    )
    assert response.status_code == 404
    assert "req-hidden" not in response.text and "77" not in response.text


def test_read_all_participant_lists_and_reads_other_agents_requests(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="read-all-list")
    request_id = created["request_id"]
    _return_result(client, worker, request_id, key="read-all-result")

    listing = client.get("/v1/requests", headers=_auth("carol"))
    assert listing.status_code == 200
    ids = {item["request_id"] for item in listing.json()["items"]}
    assert request_id in ids

    read = client.get(f"/v1/requests/{request_id}", headers=_auth("carol"))
    assert read.status_code == 200, read.text
    assert read.json()["requester_id"] == "alice" and read.json()["recipient_id"] == "bob"
    assert read.json()["content"], "a read-all participant must see the returned result content too"


def test_read_all_participant_cannot_reply_report_or_cancel(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="read-all-write-denied")
    request_id = created["request_id"]

    report = client.post(
        f"/v1/requests/{request_id}/reports", headers=_auth("carol"),
        json={"client_operation_id": "carol-report", "expected_revision": created["revision"], "action": "start"},
    )
    assert report.status_code == 404

    reply = client.post(
        f"/v1/requests/{request_id}/replies", headers=_auth("carol"),
        json={
            "client_operation_id": "carol-reply", "expected_revision": created["revision"],
            "kind": "result", "packet": {**packet_json(), "kind": "result"},
        },
    )
    assert reply.status_code == 404

    cancel = client.post(
        f"/v1/requests/{request_id}/cancel", headers=_auth("carol"),
        json={"client_operation_id": "carol-cancel", "expected_revision": created["revision"], "reason": "not mine"},
    )
    assert cancel.status_code == 404


def test_default_participant_still_sees_only_its_own_requests(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="default-visibility")
    request_id = created["request_id"]

    listing = client.get("/v1/requests", headers=_auth("dave"))
    assert listing.status_code == 200
    assert request_id not in {item["request_id"] for item in listing.json()["items"]}

    read = client.get(f"/v1/requests/{request_id}", headers=_auth("dave"))
    assert read.status_code == 404


def test_open_filter_includes_unchecked_results_and_excludes_finished(real_hub) -> None:
    client, worker = real_hub

    requested = _create_request(client, worker, client_operation_id="open-requested")

    in_progress = _create_request(client, worker, client_operation_id="open-in-progress")
    started = client.post(
        f"/v1/requests/{in_progress['request_id']}/reports", headers=_auth("bob"),
        json={"client_operation_id": "ip-1", "expected_revision": in_progress["revision"], "action": "start"},
    )
    assert started.status_code == 200, started.text

    blocked = _create_request(client, worker, client_operation_id="open-blocked")
    block = client.post(
        f"/v1/requests/{blocked['request_id']}/reports", headers=_auth("bob"),
        json={
            "client_operation_id": "bl-1", "expected_revision": blocked["revision"],
            "action": "block", "reason": "waiting on Sam",
        },
    )
    assert block.status_code == 200, block.text

    unchecked = _create_request(client, worker, client_operation_id="open-unchecked")
    _return_result(client, worker, unchecked["request_id"], key="unchecked-result")

    proved = _create_request(client, worker, client_operation_id="open-proved")
    _return_result(client, worker, proved["request_id"], key="proved-result", proof="https://example.test/result")

    checked = _create_request(client, worker, client_operation_id="open-checked")
    checked_result = _return_result(client, worker, checked["request_id"], key="checked-result")
    accept = client.post(
        f"/v1/requests/{checked['request_id']}/check", headers=_auth("alice"),
        json={
            "client_operation_id": "chk-1", "expected_revision": checked_result["revision"],
            "outcome": "accepted", "evidence": "commit abc123 is on main and tests pass",
        },
    )
    assert accept.status_code == 200, accept.text

    declined = _create_request(client, worker, client_operation_id="open-declined")
    decline = client.post(
        f"/v1/requests/{declined['request_id']}/reports", headers=_auth("bob"),
        json={
            "client_operation_id": "dc-1", "expected_revision": declined["revision"],
            "action": "decline", "reason": "not doable",
        },
    )
    assert decline.status_code == 200, decline.text

    cancelled = _create_request(client, worker, client_operation_id="open-cancelled")
    cancel = client.post(
        f"/v1/requests/{cancelled['request_id']}/cancel", headers=_auth("alice"),
        json={"client_operation_id": "cn-1", "expected_revision": cancelled["revision"], "reason": "no longer needed"},
    )
    assert cancel.status_code == 200, cancel.text

    listing = client.get("/v1/requests?open=true", headers=_auth("alice"))
    assert listing.status_code == 200
    open_items = {item["request_id"]: item["check"] for item in listing.json()["items"]}

    assert open_items.get(requested["request_id"]) == "none"
    assert in_progress["request_id"] in open_items
    assert blocked["request_id"] in open_items
    assert open_items.get(unchecked["request_id"]) == "none"
    assert checked["request_id"] not in open_items
    assert proved["request_id"] not in open_items
    assert declined["request_id"] not in open_items
    assert cancelled["request_id"] not in open_items


def test_result_alone_is_not_done(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="not-done")
    result = _return_result(client, worker, created["request_id"], key="not-done-result")
    assert result["work_state"] == "result_returned"

    listing = client.get("/v1/requests?open=true", headers=_auth("alice"))
    item = next(item for item in listing.json()["items"] if item["request_id"] == created["request_id"])
    assert item["check"] == "none"
    assert item["work_state"] == "result_returned"


def test_recipient_cannot_check_its_own_result(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="self-check")
    result = _return_result(client, worker, created["request_id"], key="self-check-result")

    denied = client.post(
        f"/v1/requests/{created['request_id']}/check", headers=_auth("bob"),
        json={
            "client_operation_id": "chk-self", "expected_revision": result["revision"],
            "outcome": "accepted", "evidence": "looks fine to me",
        },
    )
    assert denied.status_code == 404


def _checked_by(client, request_id: str) -> list[str]:
    """Who recorded an accepted check, as the requester sees the request's events."""
    events = client.get(
        "/v1/events", headers=_auth("alice"),
        params={"request_id": request_id, "event_type": "result_check_accepted"},
    )
    assert events.status_code == 200, events.text
    return [item["actor_id"] for item in events.json()["items"]]


def test_read_all_participant_may_check_a_result_it_did_not_return(real_hub) -> None:
    # D19: anyone but the doer may check. carol neither asked for the work nor did it.
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="read-all-check")
    result = _return_result(client, worker, created["request_id"], key="read-all-check-result")

    check = client.post(
        f"/v1/requests/{created['request_id']}/check", headers=_auth("carol"),
        json={
            "client_operation_id": "chk-carol", "expected_revision": result["revision"],
            "outcome": "accepted", "evidence": "read bob's result; it answers the request",
        },
    )
    assert check.status_code == 200, check.text
    assert _checked_by(client, created["request_id"]) == ["carol"]

    listing = client.get("/v1/requests?open=true", headers=_auth("alice"))
    assert created["request_id"] not in {item["request_id"] for item in listing.json()["items"]}


def test_operator_may_check_a_result(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="operator-check")
    result = _return_result(client, worker, created["request_id"], key="operator-check-result")

    check = client.post(
        f"/v1/requests/{created['request_id']}/check", headers=_auth("operator"),
        json={
            "client_operation_id": "chk-operator", "expected_revision": result["revision"],
            "outcome": "accepted", "evidence": "Nic said the result is done",
        },
    )
    assert check.status_code == 200, check.text
    assert _checked_by(client, created["request_id"]) == ["operator"]


def test_participant_without_read_all_cannot_check_another_agents_result(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="no-read-all-check")
    result = _return_result(client, worker, created["request_id"], key="no-read-all-check-result")

    denied = client.post(
        f"/v1/requests/{created['request_id']}/check", headers=_auth("dave"),
        json={
            "client_operation_id": "chk-dave", "expected_revision": result["revision"],
            "outcome": "accepted", "evidence": "looks done",
        },
    )
    assert denied.status_code == 404
    assert _checked_by(client, created["request_id"]) == []

    listing = client.get("/v1/requests?open=true", headers=_auth("alice"))
    item = next(item for item in listing.json()["items"] if item["request_id"] == created["request_id"])
    assert item["check"] == "none"


def test_check_requires_evidence_and_is_replay_safe(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="check-replay")
    result = _return_result(client, worker, created["request_id"], key="check-replay-result")

    missing = client.post(
        f"/v1/requests/{created['request_id']}/check", headers=_auth("alice"),
        json={
            "client_operation_id": "chk-missing", "expected_revision": result["revision"],
            "outcome": "accepted", "evidence": "",
        },
    )
    assert missing.status_code == 422

    body = {
        "client_operation_id": "chk-2", "expected_revision": result["revision"],
        "outcome": "accepted", "evidence": "commit abc123 is on main and tests pass",
    }
    first = client.post(f"/v1/requests/{created['request_id']}/check", headers=_auth("alice"), json=body)
    assert first.status_code == 200, first.text
    replay = client.post(f"/v1/requests/{created['request_id']}/check", headers=_auth("alice"), json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()


def test_rejected_check_keeps_item_open_with_reason(real_hub) -> None:
    client, worker = real_hub
    created = _create_request(client, worker, client_operation_id="check-rejected")
    result = _return_result(client, worker, created["request_id"], key="check-rejected-result")

    rejected = client.post(
        f"/v1/requests/{created['request_id']}/check", headers=_auth("alice"),
        json={
            "client_operation_id": "chk-r1", "expected_revision": result["revision"],
            "outcome": "rejected", "evidence": "Sam says the deck is missing slide 3",
        },
    )
    assert rejected.status_code == 200, rejected.text

    listing = client.get("/v1/requests?open=true", headers=_auth("alice"))
    item = next(item for item in listing.json()["items"] if item["request_id"] == created["request_id"])
    assert item["check"] == "rejected"
    assert item["work_state"] == "result_returned"
