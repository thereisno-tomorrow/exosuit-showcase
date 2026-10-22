"""Versioned JSON request and response envelopes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from brain_core.contracts import PROTOCOL_VERSION, canonical_json, stable_hash
from brain_core.errors import BrainError, ErrorCode


@dataclass(frozen=True, slots=True)
class ProtocolRequest:
    protocol_version: str
    request_id: str
    operation: str
    parameters: dict[str, Any]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BrainError(
                ErrorCode.PROTOCOL_INVALID,
                "Request contains a duplicate JSON object key",
                details={"refusal_code": "duplicate_key", "key": key},
            )
        result[key] = value
    return result


def parse_request(raw: str) -> ProtocolRequest:
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise BrainError(
            ErrorCode.PROTOCOL_INVALID,
            "Request is not valid JSON",
            details={"line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(value, Mapping):
        raise BrainError(
            ErrorCode.PROTOCOL_INVALID,
            "Request envelope must be an object",
            details={"actual_type": type(value).__name__},
        )
    version = value.get("protocol_version")
    if version != PROTOCOL_VERSION:
        raise BrainError(
            ErrorCode.PROTOCOL_VERSION,
            f"Unsupported protocol version: {version}",
            details={"supported": [PROTOCOL_VERSION], "actual": version},
            hint="Reissue the request with protocol_version 1.",
        )
    request_id = value.get("request_id")
    operation = value.get("operation")
    parameters = value.get("parameters", {})
    if not isinstance(request_id, str) or not request_id:
        raise BrainError(
            ErrorCode.PROTOCOL_INVALID,
            "request_id must be a non-empty string",
            details={"field": "request_id"},
        )
    if not isinstance(operation, str) or not operation:
        raise BrainError(
            ErrorCode.PROTOCOL_INVALID,
            "operation must be a non-empty string",
            details={"field": "operation"},
        )
    if not isinstance(parameters, Mapping):
        raise BrainError(
            ErrorCode.PROTOCOL_INVALID,
            "parameters must be an object",
            details={"field": "parameters", "actual_type": type(parameters).__name__},
        )
    return ProtocolRequest(
        protocol_version=version,
        request_id=request_id,
        operation=operation,
        parameters=dict(parameters),
    )


def success_response(request_id: str, data: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "result": "ok",
        "data": data,
    }
    response["response_sha256"] = stable_hash(response)
    return response


def error_response(error: BrainError, request_id: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "result": "error",
        "error": error.as_dict(),
    }
    response["response_sha256"] = stable_hash(response)
    return response


def render_json(response: Mapping[str, Any]) -> str:
    return canonical_json(dict(response))
