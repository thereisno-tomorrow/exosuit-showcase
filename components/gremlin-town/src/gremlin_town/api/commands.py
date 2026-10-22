"""Decode JSON bodies into frozen shared command objects."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from gremlin_town.contracts import (
    CancelRequest,
    CheckOutcome,
    CheckRequest,
    CreateRequest,
    MessageKind,
    Packet,
    ReportAction,
    ReportRequest,
    RecoveryDisposition,
    RecoveryRequest,
    ReplyRequest,
    RequestMode,
    proof_kind,
)


class InvalidCommand(ValueError):
    pass


def _required(data: Mapping[str, Any], name: str) -> Any:
    value = data.get(name)
    if value is None or value == "":
        raise InvalidCommand(f"missing required field: {name}")
    return value


def _enum(enum_type: type, value: Any, name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCommand(f"invalid {name}") from exc


def _integer(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCommand(f"invalid {name}") from exc


def packet_from_json(data: Mapping[str, Any]) -> Packet:
    if not isinstance(data, Mapping):
        raise InvalidCommand("packet must be an object")
    sources = data.get("sources", ())
    constraints = data.get("constraints", {})
    if not isinstance(sources, (list, tuple)) or not isinstance(constraints, Mapping) or any(not isinstance(source, Mapping) for source in sources):
        raise InvalidCommand("packet sources and constraints must be structured values")
    return Packet(
        schema_version=_integer(_required(data, "schema_version"), "schema_version"),
        kind=_enum(MessageKind, _required(data, "kind"), "kind"),
        session_reference=data.get("session_reference"),
        objective=str(_required(data, "objective")),
        context=str(data.get("context", "")),
        constraints=dict(constraints),
        sources=tuple(dict(source) for source in sources),
        next_action=str(data.get("next_action", "")),
        expected_reply=str(data.get("expected_reply", "")),
        sensitivity=str(data.get("sensitivity", "")),
    )


_TASK_ID = re.compile(r"^brn_\d{8}_\d{6}_[0-9a-f]{4,16}$")


def _task_id(value: Any) -> str | None:
    """N12b: the optional Brain record ID of the task a request is for."""

    if value is None:
        return None
    if not isinstance(value, str) or _TASK_ID.fullmatch(value) is None:
        raise InvalidCommand("task_id must be a Brain record ID")
    return value


def create_from_json(data: Mapping[str, Any]) -> CreateRequest:
    return CreateRequest(
        client_operation_id=str(_required(data, "client_operation_id")),
        mode=_enum(RequestMode, _required(data, "mode"), "mode"),
        project=str(_required(data, "project")),
        recipient=str(_required(data, "recipient")),
        packet=packet_from_json(_required(data, "packet")),
        prior_request_id=data.get("prior_request_id"),
        task_id=_task_id(data.get("task_id")),
    )


def report_from_json(request_id: str, data: Mapping[str, Any]) -> ReportRequest:
    return ReportRequest(
        client_operation_id=str(_required(data, "client_operation_id")),
        request_id=request_id,
        expected_revision=_integer(_required(data, "expected_revision"), "expected_revision"),
        action=_enum(ReportAction, _required(data, "action"), "action"),
        reason=data.get("reason"),
        session_reference=data.get("session_reference"),
    )


def reply_from_json(request_id: str, data: Mapping[str, Any]) -> ReplyRequest:
    try:
        proof_kind(data.get("proof"))
    except ValueError as exc:
        raise InvalidCommand(str(exc)) from exc
    return ReplyRequest(
        client_operation_id=str(_required(data, "client_operation_id")),
        request_id=request_id,
        expected_revision=_integer(_required(data, "expected_revision"), "expected_revision"),
        kind=_enum(MessageKind, _required(data, "kind"), "kind"),
        packet=packet_from_json(_required(data, "packet")),
        proof=data.get("proof"),
    )


def cancel_from_json(request_id: str, data: Mapping[str, Any]) -> CancelRequest:
    return CancelRequest(
        client_operation_id=str(_required(data, "client_operation_id")),
        request_id=request_id,
        expected_revision=_integer(_required(data, "expected_revision"), "expected_revision"),
        reason=str(_required(data, "reason")),
    )


def check_from_json(request_id: str, data: Mapping[str, Any]) -> CheckRequest:
    return CheckRequest(
        client_operation_id=str(_required(data, "client_operation_id")),
        request_id=request_id,
        expected_revision=_integer(_required(data, "expected_revision"), "expected_revision"),
        outcome=_enum(CheckOutcome, _required(data, "outcome"), "outcome"),
        evidence=str(_required(data, "evidence")),
    )


def recovery_from_json(request_id: str, data: Mapping[str, Any]) -> RecoveryRequest:
    return RecoveryRequest(
        client_operation_id=str(_required(data, "client_operation_id")),
        request_id=request_id,
        expected_revision=_integer(_required(data, "expected_revision"), "expected_revision"),
        disposition=_enum(RecoveryDisposition, _required(data, "disposition"), "disposition"),
        reason=str(_required(data, "reason")),
    )
