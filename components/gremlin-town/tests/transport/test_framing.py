from datetime import UTC, datetime

import pytest

from gremlin_town.contracts import MessageKind, Packet, WireMessage
from gremlin_town.transport.errors import FramingError
from gremlin_town.transport.framing import OBSERVED_FOOTER, frame_wire_message, parse_wire_message


def wire(request_id: str = "req-1", operation_id: str = "op-1") -> WireMessage:
    return WireMessage(
        request_id=request_id,
        operation_id=operation_id,
        sender="sender",
        recipient="recipient",
        created_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        packet=Packet(
            schema_version=1,
            kind=MessageKind.REQUEST,
            session_reference="session-1",
            objective="do the thing",
            context="context",
            constraints={"safe": True},
            sources=[{"kind": "local"}],
            next_action="reply",
            expected_reply="a packet",
            sensitivity="normal",
        ),
    )


def quoted(value: bytes) -> bytes:
    return b"\n".join(b">" + line for line in value.split(b"\n"))


def test_wire_round_trip_and_exact_observed_footer() -> None:
    body = frame_wire_message(wire())
    assert parse_wire_message(body) == wire()
    assert parse_wire_message(body + OBSERVED_FOOTER.encode()) == wire()


def test_wire_round_trip_preserves_unicode_before_the_suffix() -> None:
    value = wire()
    value = WireMessage(
        request_id=value.request_id,
        operation_id=value.operation_id,
        sender=value.sender,
        recipient=value.recipient,
        created_at=value.created_at,
        packet=Packet(
            schema_version=1,
            kind=value.packet.kind,
            session_reference=value.packet.session_reference,
            objective="do the café thing",
            context=value.packet.context,
            constraints=value.packet.constraints,
            sources=value.packet.sources,
            next_action=value.packet.next_action,
            expected_reply=value.packet.expected_reply,
            sensitivity=value.packet.sensitivity,
        ),
    )
    assert parse_wire_message(frame_wire_message(value)) == value


def test_reply_suffix_requires_exact_verified_parent() -> None:
    parent = frame_wire_message(wire())
    child = frame_wire_message(wire("req-2", "op-2"))
    suffix = OBSERVED_FOOTER.encode() + b"\n\nOn Parent wrote:\n\n" + quoted(parent)
    assert parse_wire_message(child + suffix, known_parent=parent).request_id == "req-2"
    with pytest.raises(FramingError, match="does not match"):
        parse_wire_message(child + suffix, known_parent=frame_wire_message(wire("other")))
    with pytest.raises(FramingError, match="trusted linkage"):
        parse_wire_message(child + suffix)


def test_wire_rejects_hostile_suffix_and_noncanonical_json() -> None:
    body = frame_wire_message(wire())
    with pytest.raises(FramingError, match="unrecognized"):
        parse_wire_message(body + b"\n<script>fetch('https://evil.invalid')</script>")
    with pytest.raises(FramingError, match="fields"):
        parse_wire_message(b'{"request_id":"req-1"}')
