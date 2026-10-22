"""Strict parsing of the hub-stamped WireMessage provider body.

The hub owns serialization. Transport only verifies provider text and maps it
back to the shared ``WireMessage``; it never creates sender, recipient, or
timestamp fields.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Mapping

from gremlin_town.contracts import MessageKind, Packet, WireMessage

from .errors import FramingError


OBSERVED_FOOTER = "\n\n--\nSent via AgentMail"
_ATTRIBUTION = re.compile(r"^\n\nOn [^\n]+ wrote:\n\n")
_WIRE_FIELDS = frozenset({"request_id", "operation_id", "sender", "recipient", "created_at", "packet"})
_PACKET_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "session_reference",
        "objective",
        "context",
        "constraints",
        "sources",
        "next_action",
        "expected_reply",
        "sensitivity",
    }
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def canonical_wire_bytes(wire: WireMessage) -> bytes:
    """Serialize a WireMessage using the hub's canonical JSON conventions."""

    return json.dumps(
        _json_value(wire), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _fail(message: str) -> None:
    raise FramingError(message)


def _wire_from_object(value: Any) -> WireMessage:
    if not isinstance(value, dict) or set(value) != _WIRE_FIELDS:
        _fail("wire message fields do not match the v1 schema")
    packet_value = value["packet"]
    if not isinstance(packet_value, dict) or set(packet_value) != _PACKET_FIELDS:
        _fail("wire packet fields do not match the v1 schema")
    try:
        kind = MessageKind(packet_value["kind"])
        created_at = datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise FramingError("wire message enum or timestamp is invalid") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if not all(isinstance(value[key], str) for key in ("request_id", "operation_id", "sender", "recipient")):
        _fail("wire message identity fields must be strings")
    if packet_value["schema_version"] != 1:
        _fail("unsupported wire packet schema")
    if packet_value["session_reference"] is not None and not isinstance(packet_value["session_reference"], str):
        _fail("packet session_reference must be a string or null")
    for key in ("objective", "context", "next_action", "expected_reply", "sensitivity"):
        if not isinstance(packet_value[key], str):
            _fail(f"packet {key} must be a string")
    if not isinstance(packet_value["constraints"], dict):
        _fail("packet constraints must be an object")
    if not isinstance(packet_value["sources"], list) or not all(
        isinstance(item, dict) for item in packet_value["sources"]
    ):
        _fail("packet sources must be a list of objects")
    return WireMessage(
        request_id=value["request_id"],
        operation_id=value["operation_id"],
        sender=value["sender"],
        recipient=value["recipient"],
        created_at=created_at,
        packet=Packet(
            schema_version=1,
            kind=kind,
            session_reference=packet_value["session_reference"],
            objective=packet_value["objective"],
            context=packet_value["context"],
            constraints=packet_value["constraints"],
            sources=packet_value["sources"],
            next_action=packet_value["next_action"],
            expected_reply=packet_value["expected_reply"],
            sensitivity=packet_value["sensitivity"],
        ),
    )


def _unquote_parent(value: str) -> bytes:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if any(not line.startswith(">") for line in lines):
        _fail("quoted parent contains an unquoted line")
    unquoted = "\n".join(line[2:] if line.startswith("> ") else line[1:] for line in lines)
    return unquoted.strip().encode("utf-8")


def parse_wire_message(
    body: bytes | str,
    *,
    known_parent: bytes | str | WireMessage | None = None,
) -> WireMessage:
    """Parse only a canonical WireMessage and a verified provider suffix."""

    raw = body if isinstance(body, bytes) else body.encode("utf-8")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FramingError("provider body is not valid UTF-8") from exc
    decoder = json.JSONDecoder()
    try:
        parsed, boundary = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise FramingError("provider body starts with invalid JSON") from exc
    wire = _wire_from_object(parsed)
    if text[:boundary].encode("utf-8") != canonical_wire_bytes(wire):
        _fail("wire message is not canonical UTF-8 JSON")
    suffix = text[boundary:]
    if suffix == "":
        return wire
    if not suffix.startswith(OBSERVED_FOOTER):
        _fail("unrecognized provider suffix")
    remainder = suffix[len(OBSERVED_FOOTER) :]
    if remainder == "":
        return wire
    match = _ATTRIBUTION.match(remainder)
    if match is None:
        _fail("provider suffix is not the observed attribution form")
    quoted = remainder[match.end() :]
    if known_parent is None:
        _fail("quoted parent requires trusted linkage")
    if isinstance(known_parent, WireMessage):
        expected = canonical_wire_bytes(known_parent)
    elif isinstance(known_parent, str):
        expected = known_parent.encode("utf-8")
    else:
        expected = known_parent
    if _unquote_parent(quoted) != expected.replace(b"\r\n", b"\n").replace(b"\r", b"\n").strip():
        _fail("quoted parent does not match trusted linkage")
    return wire


def frame_wire_message(wire: WireMessage) -> bytes:
    """Return the canonical bytes the backend should stage in its outbox."""

    return canonical_wire_bytes(wire)
