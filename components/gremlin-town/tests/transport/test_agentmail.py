from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gremlin_town.contracts import MessageKind, OutboundEnvelope
from gremlin_town.transport.agentmail import AgentMailConfig, AgentMailTransport
from gremlin_town.transport.errors import ReconciliationRequiredError, ThreadScopeConflictError, TransportError


class Requester:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def response(status, *, json_value=None, headers=None):
    return httpx.Response(status, json=json_value, headers=headers or {})


def envelope(parent: str | None = None):
    return OutboundEnvelope(
        operation_id="op",
        request_id="req",
        kind=MessageKind.REQUEST,
        provider_retry_key="retry/key?1",
        payload=b"immutable-wire-bytes",
        content_sha256="digest",
        recipient_address="agent@example.test",
        parent_message_id=parent,
    )


def transport(requester, **kwargs):
    return AgentMailTransport(
        AgentMailConfig(inbox_id="inbox/a", api_key="secret", **kwargs),
        client=requester,
    )


def test_agentmail_initial_send_uses_real_json_api_shape() -> None:
    requester = Requester([response(200, json_value={"message_id": "m1", "thread_id": "t1", "created_at": "2026-09-20T12:00:00Z"})])
    reference = transport(requester).send(envelope())
    method, url, kwargs = requester.calls[0]
    assert reference.message_id == "m1"
    assert method == "POST"
    assert url == "https://api.agentmail.to/v0/inboxes/inbox%2Fa/messages/send"
    assert kwargs["json"] == {
        "to": "agent@example.test",
        "subject": "Gremlin Town request req op",
        "text": "immutable-wire-bytes",
    }
    assert kwargs["headers"]["Idempotency-Key"] == "retry/key?1"


def test_agentmail_reply_selects_reply_endpoint_and_no_send() -> None:
    requester = Requester([response(200, json_value={"message_id": "m2", "thread_id": "t1"})])
    transport(requester).send(envelope("parent/id"))
    method, url, kwargs = requester.calls[0]
    assert method == "POST"
    assert url.endswith("/messages/parent%2Fid/reply")
    assert kwargs["json"] == {"to": "agent@example.test", "text": "immutable-wire-bytes", "reply_all": False}


def test_agentmail_exact_read_and_thread_paths_paginate_and_order() -> None:
    requester = Requester(
        [
            response(200, json_value={"message_id": "m0", "thread_id": "t1", "text": "read"}),
            response(200, json_value={"messages": [{"message_id": "m2", "thread_id": "thread/id", "text": "two"}], "next_page_token": "older"}),
            response(200, json_value={"messages": [{"message_id": "m1", "thread_id": "thread/id", "text": "one"}, {"message_id": "m2", "thread_id": "thread/id", "text": "dup"}]}),
        ]
    )
    adapter = transport(requester)
    adapter.fetch_message("message/id")
    messages = adapter.fetch_thread_complete("thread/id")
    assert requester.calls[0][1].endswith("/messages/message%2Fid")
    assert "/threads/thread%2Fid?limit=100" in requester.calls[1][1]
    assert "page_token=older" in requester.calls[2][1]
    assert [message.message_id for message in messages] == ["m1", "m2"]
    assert all(message.content_source.value == "provider" for message in messages)


def test_agentmail_wrong_thread_fails_before_body_is_exposed() -> None:
    requester = Requester([response(200, json_value={"messages": [{"message_id": "m3", "thread_id": "wrong", "text": "secret body"}]})])
    with pytest.raises(ThreadScopeConflictError):
        transport(requester).fetch_thread("t1")


def test_agentmail_preserves_retry_after_and_structured_status_codes() -> None:
    requester = Requester([response(429, headers={"Retry-After": "7"})])
    with pytest.raises(TransportError) as caught:
        transport(requester).send(envelope())
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.retry_after == 7
    requester = Requester([response(409, json_value={"code": "message_in_progress"})])
    with pytest.raises(TransportError) as caught:
        transport(requester).send(envelope())
    assert caught.value.code == "CONFLICT"
    assert caught.value.provider_code == "message_in_progress"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (httpx.TimeoutException("timed out"), "TIMEOUT"),
        (httpx.ConnectError("connection dropped"), "PROVIDER_UNAVAILABLE"),
    ],
)
def test_agentmail_post_no_response_is_acceptance_ambiguous(failure, code) -> None:
    class FailingRequester:
        def request(self, method, url, **kwargs):
            raise failure

    with pytest.raises(TransportError) as caught:
        transport(FailingRequester()).send(envelope())
    assert caught.value.code == code
    assert caught.value.accepted is True
    assert caught.value.retryable is True


def test_agentmail_get_timeout_remains_unavailable_not_accepted() -> None:
    class FailingRequester:
        def request(self, method, url, **kwargs):
            raise httpx.TimeoutException("timed out")

    with pytest.raises(TransportError) as caught:
        transport(FailingRequester()).fetch_message("message/id")
    assert caught.value.code == "TIMEOUT"
    assert caught.value.accepted is False


@pytest.mark.parametrize("status", [429, 503])
def test_agentmail_definite_http_response_is_not_acceptance_ambiguous(status: int) -> None:
    requester = Requester([response(status, headers={"Retry-After": "7"})])
    with pytest.raises(TransportError) as caught:
        transport(requester).send(envelope())
    assert caught.value.accepted is False
    assert caught.value.retryable is True
    assert caught.value.retry_after == 7


def test_agentmail_http_mapping_does_not_log_provider_secrets_or_bodies(caplog) -> None:
    requester = Requester([response(409, json_value={"code": "message_in_progress", "body": "provider-secret"})])
    with pytest.raises(TransportError):
        transport(requester).send(envelope())
    assert "provider-secret" not in caplog.text
    assert "secret" not in caplog.text


def test_agentmail_reconciliation_uses_safe_discovery_and_expires() -> None:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    requester = Requester([response(200, json_value={"messages": []})])
    adapter = AgentMailTransport(
        AgentMailConfig(inbox_id="inbox", api_key="secret", retry_window=timedelta(hours=24)),
        client=requester,
        clock=lambda: now,
    )
    with pytest.raises(ReconciliationRequiredError) as caught:
        adapter.reconcile(envelope(), created_at=now - timedelta(hours=25))
    assert caught.value.code == "RECOVERY_REQUIRED"
    method, url, _ = requester.calls[0]
    assert method == "GET"
    assert url.endswith("/messages?limit=100")
    assert "idempotency_key" not in url


def test_agentmail_reconciliation_marks_ambiguous_timeout_without_resend() -> None:
    requester = Requester(
        [
            response(
                200,
                json_value={
                    "messages": [
                        {"message_id": "m1", "thread_id": "t", "operation_id": "op", "text": "immutable-wire-bytes", "timestamp": "2026-09-20T11:00:00Z"},
                        {"message_id": "m2", "thread_id": "t", "operation_id": "op", "text": "immutable-wire-bytes", "timestamp": "2026-09-20T10:00:00Z"},
                    ]
                },
            )
        ]
    )
    with pytest.raises(ReconciliationRequiredError) as caught:
        transport(requester).reconcile(envelope())
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert [call[0] for call in requester.calls] == ["GET"]
