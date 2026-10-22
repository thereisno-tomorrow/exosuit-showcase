from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from gremlin_town.contracts import (
    Actor,
    ContentAvailability,
    ContentSource,
    MessageKind,
    ProviderMessage,
    RecoveryDisposition,
    RecoveryRequest,
    RequestMode,
    RequestSnapshot,
    WireMessage,
    ReplyRequest,
    Packet,
    proof_kind,
)


def test_result_proof_is_optional_bounded_and_immutable():
    packet = Packet(1, MessageKind.RESULT, None, "Answer", "", {}, (), "", "", "public")
    old = ReplyRequest("result-one", "req_one", 1, MessageKind.RESULT, packet)
    assert old.proof is None
    proved = ReplyRequest("result-two", "req_two", 1, MessageKind.RESULT, packet, "reports/result.md")
    assert proof_kind(proved.proof) == "path"
    with pytest.raises(FrozenInstanceError):
        proved.proof = "ignore instructions"
    with pytest.raises(ValueError, match="300"):
        proof_kind("https://example.test/" + "x" * 281)


def test_snapshot_cannot_make_empty_content_ambiguous() -> None:
    snapshot = RequestSnapshot(
        request_id="req_1",
        mode=RequestMode.ACTION_REQUESTED,
        project="test",
        requester_id="sender",
        recipient_id="recipient",
        objective="test objective",
        work_state=None,
        revision=1,
        recovery_hold=False,
        thread_id=None,
        content_availability=ContentAvailability.UNAVAILABLE,
        content=(),
    )

    assert snapshot.content == ()
    assert snapshot.content_availability is ContentAvailability.UNAVAILABLE


def test_provider_message_records_source_and_verified_attribution() -> None:
    message = ProviderMessage(
        message_id="msg_1",
        thread_id="thread_1",
        parent_message_id=None,
        received_at=datetime.now(UTC),
        body=b"{}",
        body_available=True,
        content_source=ContentSource.PROVIDER,
        trust_label="authenticated_operation",
        linked_operation_id="op_1",
        attributed_actor_id="recipient",
        exact_link_verified=True,
    )

    assert message.exact_link_verified
    assert message.attributed_actor_id == "recipient"


def test_recovery_command_is_explicit_and_immutable() -> None:
    command = RecoveryRequest(
        client_operation_id="recover_1",
        request_id="req_1",
        expected_revision=4,
        disposition=RecoveryDisposition.KEEP_HELD,
        reason="provider evidence is still ambiguous",
    )

    with pytest.raises(FrozenInstanceError):
        command.reason = "changed"  # type: ignore[misc]


def test_actor_is_authentication_result_not_packet_field() -> None:
    actor = Actor(actor_id="local-codex")
    assert not hasattr(actor, "claimed_sender")
    assert MessageKind.REQUEST == "request"


def test_wire_message_has_hub_stamped_identity_and_operation() -> None:
    from gremlin_town.contracts import Packet

    wire = WireMessage(
        request_id="req_1",
        operation_id="op_1",
        sender="authenticated-sender",
        recipient="resolved-recipient",
        created_at=datetime.now(UTC),
        packet=Packet(
            schema_version=1,
            kind=MessageKind.REQUEST,
            session_reference=None,
            objective="objective",
            context="context",
            constraints={},
            sources=(),
            next_action="reply",
            expected_reply="result",
            sensitivity="public-test",
        ),
    )

    assert wire.sender == "authenticated-sender"
    assert wire.operation_id == "op_1"
