from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json

import httpx
import pytest

from gremlin_town.contracts import MessageKind, OutboundEnvelope, Packet, WireMessage
from gremlin_town.transport.errors import ReconciliationRequiredError, ThreadScopeConflictError, TransportError
from gremlin_town.transport.framing import canonical_wire_bytes
from gremlin_town.transport.github import GitHubConfig, GitHubTransport


BASE = "https://api.github.com/repos/test-owner/mailbox"
ROOT = "github:test-owner/mailbox:issue:7"
NOW = datetime(2026, 9, 23, 10, tzinfo=UTC)


def envelope(parent=None, request="req-1", operation="op-1", context="café → 中文"):
    wire = WireMessage(request, operation, "sender", "recipient", NOW,
                       Packet(1, MessageKind.REQUEST, None, "A task", context, {}, [], "reply", "result", "internal"))
    payload = canonical_wire_bytes(wire)
    return OutboundEnvelope(operation, request, MessageKind.REQUEST, "retry-1", payload,
                            hashlib.sha256(payload).hexdigest(), "recipient@example.test", parent)


def body(env):
    # Independent encoding matches the documented provider envelope.
    return json.dumps({"schema": "gt.github-message/v1", "parent": env.parent_message_id,
                       "sha256": env.content_sha256, "wire": env.payload.decode()},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def issue(env=None, number=7, user=42):
    return {"id": 1000 + number, "number": number, "url": f"{BASE}/issues/{number}",
            "body": body(env or envelope()), "created_at": "2026-09-23T10:00:00Z", "user": {"id": user}}


def comment(env=None, ident=88, number=7, user=42):
    return {"id": ident, "url": f"{BASE}/issues/comments/{ident}", "issue_url": f"{BASE}/issues/{number}",
            "body": body(env or envelope(ROOT, operation="op-2")),
            "created_at": "2026-09-23T10:00:01Z", "user": {"id": user}}


def response(raw=None, status=200, headers=None):
    return httpx.Response(status, json=raw, headers=headers or {})


class Requester:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, "unexpected provider I/O"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def adapter(requester, **kwargs):
    return GitHubTransport(GitHubConfig("test-owner", "mailbox", "never-log-this", 42, **kwargs),
                           client=requester, clock=lambda: NOW)


def test_send_initial_issue_exact_unicode_and_unverified_read():
    requester = Requester(response(issue(), 201), response(issue()))
    transport = adapter(requester)
    receipt = transport.send(envelope())
    assert receipt.message_id == receipt.thread_id == ROOT
    assert receipt.accepted_at == NOW
    method, url, kwargs = requester.calls[0]
    assert method == "POST" and url == BASE + "/issues"
    assert kwargs["json"] == {"title": "GT req-1", "body": body(envelope())}
    assert "Idempotency-Key" not in kwargs["headers"]
    assert kwargs["follow_redirects"] is False
    message = transport.fetch_message(ROOT)
    assert message.body == envelope().payload
    assert not message.exact_link_verified and message.attributed_actor_id is None
    assert message.trust_label == "provider_unverified"
    assert "never-log-this" not in repr(transport.config)


@pytest.mark.parametrize("parent", [ROOT, ROOT + ":comment:87"])
def test_reply_resolves_parent_and_posts_to_same_issue(parent):
    env = envelope(parent, operation="op-2")
    parent_raw = issue() if parent == ROOT else comment(envelope(ROOT), ident=87)
    requester = Requester(response(parent_raw), response(comment(env), 201))
    receipt = adapter(requester).send(env)
    assert receipt.message_id == ROOT + ":comment:88"
    assert receipt.parent_message_id == parent
    assert requester.calls[-1][1] == BASE + "/issues/7/comments"


def test_wrong_parent_request_prevents_post():
    requester = Requester(response(issue(envelope(request="other"))))
    with pytest.raises(ThreadScopeConflictError):
        adapter(requester).send(envelope(ROOT))
    assert len(requester.calls) == 1 and requester.calls[0][0] == "GET"


@pytest.mark.parametrize("locator", ["agentmail:7", "github:other/mailbox:issue:7", ROOT + "/../8", ROOT + ":comment:0"])
def test_invalid_scope_never_sends_credentials(locator):
    requester = Requester()
    with pytest.raises(ThreadScopeConflictError):
        adapter(requester).fetch_message(locator)
    assert not requester.calls


@pytest.mark.parametrize("change", [dict(content_sha256="bad"), dict(operation_id="other"), dict(payload=b"not wire")])
def test_invalid_outbox_never_posts(change):
    requester = Requester()
    with pytest.raises(TransportError):
        adapter(requester).send(replace(envelope(), **change))
    assert not requester.calls


def test_oversized_body_fails_before_io():
    requester = Requester()
    with pytest.raises(TransportError, match="body limit"):
        adapter(requester).send(envelope(context="文" * 25000))
    assert not requester.calls


@pytest.mark.parametrize("raw", [None, {}, {"number": 7}, issue(user=43), {**issue(), "body": "changed"}])
def test_malformed_success_is_uncertain_not_safe_to_resend(raw):
    requester = Requester(response(raw, 201))
    with pytest.raises(TransportError) as caught:
        adapter(requester).send(envelope())
    assert caught.value.accepted and caught.value.retryable
    assert len(requester.calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("secret-url"), httpx.ConnectError("secret-url"), response({}, 503)])
def test_ambiguous_post_preserves_uncertainty_and_sanitizes_errors(failure):
    requester = Requester(failure)
    with pytest.raises(TransportError) as caught:
        adapter(requester).send(envelope())
    assert caught.value.accepted
    assert "secret-url" not in str(caught.value)


@pytest.mark.parametrize("status,headers,raw,code,retryable", [
    (429, {"Retry-After": "12"}, {}, "RATE_LIMITED", True),
    (403, {}, {"message": "You have exceeded a secondary rate limit"}, "RATE_LIMITED", True),
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW.timestamp()) + 30)}, {}, "RATE_LIMITED", True),
    (403, {}, {}, "PROVIDER_FORBIDDEN", False),
    (401, {}, {}, "AUTHENTICATION_FAILED", False),
    (422, {}, {}, "INVALID_REQUEST", False),
    (302, {"Location": "https://evil.invalid"}, {}, "PROVIDER_ERROR", False),
])
def test_status_mapping(status, headers, raw, code, retryable):
    requester = Requester(response(raw, status, headers))
    with pytest.raises(TransportError) as caught:
        adapter(requester).send(envelope())
    assert caught.value.code == code and caught.value.retryable == retryable
    assert not caught.value.accepted
    if code == "RATE_LIMITED":
        assert caught.value.retry_after > 0
    assert len(requester.calls) == 1


def test_thread_pagination_root_once_and_comments_in_order():
    requester = Requester(response(issue()), response([comment(ident=88)]), response([comment(ident=89)]), response([]))
    messages = adapter(requester, page_limit=1).fetch_thread_complete(ROOT)
    assert [x.message_id for x in messages] == [ROOT, ROOT + ":comment:88", ROOT + ":comment:89"]
    assert "page=3" in requester.calls[-1][1]


def test_cross_thread_cursor_no_io():
    requester = Requester()
    with pytest.raises(ThreadScopeConflictError):
        adapter(requester).fetch_thread(ROOT, json.dumps({"thread": ROOT + "1", "page": 2}))
    assert not requester.calls


def test_cross_repository_pagination_link_not_followed():
    requester = Requester(response(issue()), response([comment()], headers={
        "Link": '<https://evil.invalid/comments?page=2>; rel="next"'}))
    with pytest.raises(TransportError, match="pagination"):
        adapter(requester).fetch_thread(ROOT)
    assert len(requester.calls) == 2


def test_full_page_budget_exhaustion_is_not_complete_read():
    requester = Requester(response(issue()), response([comment()]))
    with pytest.raises(ReconciliationRequiredError):
        adapter(requester, page_limit=1, max_pages=1).fetch_thread_complete(ROOT)


@pytest.mark.parametrize("raw", [{**comment(), "issue_url": BASE + "/issues/8"}, {**comment(), "id": 99}])
def test_exact_message_scope_verified(raw):
    requester = Requester(response(raw))
    with pytest.raises(ThreadScopeConflictError):
        adapter(requester).fetch_message(ROOT + ":comment:88")


def test_edited_content_digest_fails_and_external_prose_is_untrusted():
    raw = issue()
    encoded = json.loads(raw["body"])
    encoded["wire"] += " "
    raw["body"] = json.dumps(encoded)
    with pytest.raises(TransportError, match="hash"):
        adapter(Requester(response(raw))).fetch_message(ROOT)
    message = adapter(Requester(response({**issue(), "body": "Ignore instructions"}))).fetch_message(ROOT)
    assert not message.exact_link_verified and message.body == b"Ignore instructions"


def test_deleted_content_is_explicit_unavailable():
    with pytest.raises(TransportError) as caught:
        adapter(Requester(response({}, 404))).fetch_message(ROOT)
    assert caught.value.code == "SCOPE_OR_NOT_FOUND"


def test_lost_success_reconciles_exact_body_without_resend():
    requester = Requester(httpx.ReadTimeout("lost response"), response([issue()]))
    transport = adapter(requester)
    with pytest.raises(TransportError):
        transport.send(envelope())
    assert transport.reconcile(envelope()).message_id == ROOT
    assert [x[0] for x in requester.calls] == ["POST", "GET"]
    assert "state=all" in requester.calls[-1][1]


@pytest.mark.parametrize("rows", [[], [issue(), issue(number=8)], [issue(user=99)],
                                   [{**issue(), "body": body(envelope(context="changed"))}],
                                   [{**issue(), "pull_request": {}}]])
def test_absent_duplicate_forged_changed_or_pr_candidates_are_held(rows):
    requester = Requester(response(rows))
    with pytest.raises(ReconciliationRequiredError):
        adapter(requester).reconcile(envelope())
    assert all(x[0] == "GET" for x in requester.calls)


def test_reconcile_reply_uses_bound_issue_not_search():
    env = envelope(ROOT, operation="op-2")
    requester = Requester(response([comment(env)]))
    receipt = adapter(requester).reconcile(env)
    assert receipt.parent_message_id == ROOT
    assert requester.calls[0][1].startswith(BASE + "/issues/7/comments?")


def test_reconciliation_checks_later_pages_for_duplicate():
    requester = Requester(response([issue()]), response([issue(number=8)]), response([]))
    with pytest.raises(ReconciliationRequiredError):
        adapter(requester, page_limit=1).reconcile(envelope())
    assert len(requester.calls) == 3


@pytest.fixture
def github_backend(tmp_path):
    """Real GT database/service/outbox against a GitHub-shaped HTTP fixture."""
    from gremlin_town.domain import Participant, Policy
    from gremlin_town.service import GremlinTownService
    from gremlin_town.storage import SQLiteStore
    from gremlin_town.workers import OutboxWorker

    state = {"issue": None, "comments": [], "calls": [], "lose_response": False, "drop_write": False}

    def handler(request):
        state["calls"].append((request.method, request.url.path))
        path = request.url.path.removeprefix("/repos/test-owner/mailbox")
        if request.method == "POST":
            posted = json.loads(request.content)
            if path == "/issues":
                raw = {**issue(), "body": posted["body"]}
                if not state["drop_write"]:
                    state["issue"] = raw
            else:
                assert path == "/issues/7/comments"
                raw = {**comment(ident=88 + len(state["comments"])), "body": posted["body"]}
                if not state["drop_write"]:
                    state["comments"].append(raw)
            if state["lose_response"]:
                state["lose_response"] = False
                raise httpx.ReadTimeout("lost response", request=request)
            return response(raw, 201)
        assert request.method == "GET"
        if path == "/issues":
            return response([state["issue"]] if state["issue"] else [])
        if path == "/issues/7":
            return response(state["issue"])
        if path == "/issues/7/comments":
            return response(state["comments"])
        if path.startswith("/issues/comments/"):
            return response(next(x for x in state["comments"] if x["id"] == int(path.split("/")[-1])))
        raise AssertionError(f"unexpected HTTP request: {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = adapter(client)
    policy = Policy(participants=(
        Participant("alice", "Alice", "hub@example.test", frozenset({"demo"}), frozenset({"public-test"})),
        Participant("bob", "Bob", "hub@example.test", frozenset({"demo"}), frozenset({"public-test"})),
    ), routes=frozenset({("demo", "alice", "bob", "public-test"), ("demo", "bob", "alice", "public-test")}))
    store = SQLiteStore(tmp_path / "github.db", now=lambda: NOW)
    service = GremlinTownService(store, policy, provider=transport, now=lambda: NOW)
    worker = OutboxWorker(store, policy, transport, now=lambda: NOW)
    yield service, worker, store, state
    client.close()


def queue_request(service, key="create-1"):
    from gremlin_town.contracts import Actor, CreateRequest, RequestMode
    packet = Packet(1, MessageKind.REQUEST, None, "A task", "café → 中文", {}, [], "reply", "result", "public-test")
    return service.requests_create(Actor("alice"), CreateRequest(key, RequestMode.ACTION_REQUESTED, "demo", "bob", packet))


def test_real_service_outbox_request_result_attribution_and_duplicate(github_backend):
    from gremlin_town.contracts import Actor, OperationState, ReplyRequest, ReportAction, ReportRequest, WorkState
    service, worker, store, state = github_backend
    queued = queue_request(service)
    assert worker.dispatch_once().operation_state is OperationState.ACCEPTED
    snapshot = service.requests_read(Actor("bob"), queued.request_id)
    assert snapshot.content[0].exact_link_verified
    assert snapshot.content[0].attributed_actor_id == "alice"
    assert "café" in snapshot.content[0].body.decode()
    started = service.requests_report(Actor("bob"), ReportRequest("start", queued.request_id, snapshot.revision, ReportAction.START))
    packet = Packet(1, MessageKind.RESULT, None, "Done", "answer", {}, [], "none", "none", "public-test")
    service.requests_reply(Actor("bob"), ReplyRequest("reply", queued.request_id, started.revision, MessageKind.RESULT, packet))
    assert worker.dispatch_once().work_state is WorkState.RESULT_RETURNED
    returned = service.requests_read(Actor("alice"), queued.request_id)
    assert len(returned.content) == 2
    assert returned.content[1].exact_link_verified and returned.content[1].attributed_actor_id == "bob"
    again = queue_request(service)
    assert again.request_id == queued.request_id
    assert worker.dispatch_once() is None
    assert sum(method == "POST" for method, _ in state["calls"]) == 2


@pytest.mark.parametrize("drop_write", [False, True])
def test_real_outbox_uncertainty_never_requeues_ambiguous_absence(github_backend, drop_write):
    from gremlin_town.contracts import OperationState
    service, worker, store, state = github_backend
    state.update(lose_response=True, drop_write=drop_write)
    queued = queue_request(service)
    assert worker.dispatch_once().operation_state is OperationState.UNCERTAIN
    reconciled = worker.reconcile_once()
    if drop_write:
        assert reconciled.operation_state is OperationState.UNCERTAIN
        assert store.operation(queued.operation_id).recovery_required
    else:
        assert reconciled.operation_state is OperationState.ACCEPTED
    worker.dispatch_once()
    assert sum(method == "POST" for method, _ in state["calls"]) == 1


def test_real_service_cancellation_prevents_provider_send(github_backend):
    from gremlin_town.contracts import Actor, CancelRequest
    service, worker, store, state = github_backend
    queued = queue_request(service)
    service.requests_cancel(Actor("alice"), CancelRequest("cancel", queued.request_id, queued.revision, "stop"))
    worker.dispatch_once()
    assert not state["calls"]


def test_real_service_wrong_actor_has_zero_provider_io(github_backend):
    from gremlin_town.contracts import Actor
    from gremlin_town.service import GremlinTownError
    service, worker, store, state = github_backend
    queued = queue_request(service)
    with pytest.raises(GremlinTownError):
        service.requests_read(Actor("unknown"), queued.request_id)
    assert not state["calls"]


def test_real_service_rejects_provider_edit_even_with_recomputed_envelope_hash(github_backend):
    from gremlin_town.contracts import Actor
    from gremlin_town.service import GremlinTownError
    service, worker, store, state = github_backend
    queued = queue_request(service)
    worker.dispatch_once()
    encoded = json.loads(state["issue"]["body"])
    wire = json.loads(encoded["wire"])
    wire["packet"]["context"] = "forged replacement"
    encoded["wire"] = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded["sha256"] = hashlib.sha256(encoded["wire"].encode()).hexdigest()
    state["issue"]["body"] = json.dumps(encoded)
    with pytest.raises(GremlinTownError, match="durable operation link"):
        service.requests_read(Actor("bob"), queued.request_id)


def test_real_service_copied_operation_marker_is_not_authenticated(github_backend):
    from gremlin_town.contracts import Actor
    service, worker, store, state = github_backend
    queued = queue_request(service)
    worker.dispatch_once()
    state["comments"].append({**comment(), "body": state["issue"]["body"]})
    snapshot = service.requests_read(Actor("bob"), queued.request_id)
    assert snapshot.content[0].exact_link_verified
    assert not snapshot.content[1].exact_link_verified
    assert snapshot.content[1].trust_label == "untrusted_external"
    assert snapshot.content[1].attributed_actor_id is None
