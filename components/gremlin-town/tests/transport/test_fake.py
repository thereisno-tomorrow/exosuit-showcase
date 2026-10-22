from datetime import UTC, datetime

import pytest

from gremlin_town.contracts import ContentSource, MessageKind, OutboundEnvelope
from gremlin_town.transport.errors import ProviderTransportError
from gremlin_town.transport.fake import FakeOutcome, FakeTransport


def envelope(operation: str = "op-1", key: str = "key-1", payload: bytes = b"exact") -> OutboundEnvelope:
    return OutboundEnvelope(
        operation_id=operation,
        request_id="req-1",
        kind=MessageKind.REQUEST,
        provider_retry_key=key,
        payload=payload,
        content_sha256="digest",
        recipient_address="recipient@example.test",
    )


def test_fake_preserves_bytes_and_reuses_provider_identity() -> None:
    transport = FakeTransport()
    first = transport.send(envelope())
    second = transport.send(envelope())

    assert first == second
    assert transport.sent_payloads["key-1"] == b"exact"
    assert transport.fetch_message(first.message_id).content_source is ContentSource.PROVIDER
    assert transport.send_attempts == 2
    with pytest.raises(ProviderTransportError, match="different bytes"):
        transport.send(envelope(payload=b"changed"))


@pytest.mark.parametrize(
    ("outcome", "accepted"),
    [
        (FakeOutcome.TIMEOUT_BEFORE_ACCEPTANCE, False),
        (FakeOutcome.TIMEOUT_AFTER_ACCEPTANCE, True),
    ],
)
def test_fake_timeout_boundaries_are_observable(outcome: FakeOutcome, accepted: bool) -> None:
    transport = FakeTransport(outcome=outcome)
    with pytest.raises(ProviderTransportError) as caught:
        transport.send(envelope())
    assert caught.value.accepted is accepted
    reconciled = transport.reconcile(envelope())
    assert (reconciled is not None) is accepted


def test_fake_outage_does_not_create_provider_message() -> None:
    transport = FakeTransport(outcome=FakeOutcome.OUTAGE)
    with pytest.raises(ProviderTransportError) as caught:
        transport.send(envelope())
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert transport.messages == ()


def test_fake_reorders_and_paginates_without_losing_messages() -> None:
    transport = FakeTransport(page_size=1, reorder=True)
    for index in range(3):
        transport.send(envelope(operation=f"op-{index}", key=f"key-{index}"))

    page = transport.fetch_thread("req-1")
    assert page.next_cursor == "1"
    complete = transport.fetch_thread_complete("req-1")
    assert {message.message_id for message in complete} == {"fake-msg-0001", "fake-msg-0002", "fake-msg-0003"}
