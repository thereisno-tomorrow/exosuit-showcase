from __future__ import annotations

import hashlib
import json

import pytest

from brain_core.contracts import PROTOCOL_VERSION, canonical_json, stable_hash
from brain_core.errors import BrainError, ErrorCode
from brain_core.protocol import error_response, parse_request, render_json, success_response


def response_hash(response: dict[str, object]) -> str:
    unsigned = dict(response)
    unsigned.pop("response_sha256")
    return stable_hash(unsigned)


def test_parse_request_accepts_default_and_explicit_parameters() -> None:
    minimal = parse_request(
        '{"protocol_version":"1","request_id":"request-1","operation":"catalog"}'
    )
    assert minimal.protocol_version == PROTOCOL_VERSION
    assert minimal.request_id == "request-1"
    assert minimal.operation == "catalog"
    assert minimal.parameters == {}

    explicit = parse_request(
        json.dumps(
            {
                "parameters": {"intent": "Compare synthetic evidence", "vaults": ["vault_career"]},
                "operation": "context.plan",
                "request_id": "request-2",
                "protocol_version": "1",
            }
        )
    )
    assert explicit.parameters == {
        "intent": "Compare synthetic evidence",
        "vaults": ["vault_career"],
    }


def test_parse_request_reports_invalid_json_location() -> None:
    with pytest.raises(BrainError) as caught:
        parse_request('{"protocol_version":')
    error = caught.value
    assert error.code is ErrorCode.PROTOCOL_INVALID
    assert error.details["line"] == 1
    assert error.details["column"] > 1


@pytest.mark.parametrize(
    "raw",
    [
        '{"protocol_version":"1","request_id":"one","request_id":"two","operation":"inspector"}',
        '{"protocol_version":"1","request_id":"one","operation":"inspector","parameters":{"nested":{"key":1,"key":2}}}',
    ],
)
def test_parse_request_refuses_duplicate_keys_at_every_depth(raw: str) -> None:
    with pytest.raises(BrainError) as raised:
        parse_request(raw)
    assert raised.value.code is ErrorCode.PROTOCOL_INVALID
    assert raised.value.details["refusal_code"] == "duplicate_key"
    assert raised.value.details["key"] in {"request_id", "key"}


@pytest.mark.parametrize("raw", ["null", "[]", '"request"', "1"])
def test_parse_request_requires_an_object(raw: str) -> None:
    with pytest.raises(BrainError) as caught:
        parse_request(raw)
    assert caught.value.code is ErrorCode.PROTOCOL_INVALID
    assert caught.value.details["actual_type"] in {"NoneType", "list", "str", "int"}


@pytest.mark.parametrize("version", [None, 1, "0", "2", "unknown"])
def test_parse_request_refuses_missing_typed_and_unknown_protocol_versions(version: object) -> None:
    raw = json.dumps({"protocol_version": version, "request_id": "r", "operation": "catalog"})
    with pytest.raises(BrainError) as caught:
        parse_request(raw)
    error = caught.value
    assert error.code is ErrorCode.PROTOCOL_VERSION
    assert error.exit_code == 4
    assert error.details == {"supported": ["1"], "actual": version}
    assert error.hint == "Reissue the request with protocol_version 1."


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", None),
        ("request_id", 1),
        ("request_id", ""),
        ("operation", None),
        ("operation", 1),
        ("operation", ""),
    ],
)
def test_parse_request_requires_nonempty_request_and_operation_strings(field: str, value: object) -> None:
    payload = {"protocol_version": "1", "request_id": "r", "operation": "catalog"}
    payload[field] = value
    with pytest.raises(BrainError) as caught:
        parse_request(json.dumps(payload))
    assert caught.value.code is ErrorCode.PROTOCOL_INVALID
    assert caught.value.details == {"field": field}


@pytest.mark.parametrize("parameters", [None, [], "intent", 1])
def test_parse_request_requires_parameter_object(parameters: object) -> None:
    payload = {
        "protocol_version": "1",
        "request_id": "r",
        "operation": "catalog",
        "parameters": parameters,
    }
    with pytest.raises(BrainError) as caught:
        parse_request(json.dumps(payload))
    assert caught.value.code is ErrorCode.PROTOCOL_INVALID
    assert caught.value.details == {
        "field": "parameters",
        "actual_type": type(parameters).__name__,
    }


def test_success_response_hash_is_canonical_and_deterministic() -> None:
    data = {"z": 2, "a": "café"}
    first = success_response("request-1", data)
    second = success_response("request-1", {"a": "café", "z": 2})
    assert first == second
    assert first["protocol_version"] == "1"
    assert first["result"] == "ok"
    assert first["response_sha256"] == response_hash(first)


def test_error_response_preserves_public_error_and_optional_request_id() -> None:
    error = BrainError(
        ErrorCode.PATH_ESCAPE,
        "Boundary escape refused",
        details={"locator": "link.md"},
        hint="Keep paths inside the repository.",
    )
    identified = error_response(error, "request-1")
    anonymous = error_response(error)
    assert identified["request_id"] == "request-1"
    assert anonymous["request_id"] is None
    assert identified["error"] == error.as_dict()
    assert identified["response_sha256"] == response_hash(identified)
    assert anonymous["response_sha256"] == response_hash(anonymous)


def test_render_json_is_compact_sorted_unicode_and_does_not_mutate_input() -> None:
    response = {"z": [2, 1], "a": "café"}
    before = dict(response)
    rendered = render_json(response)
    assert rendered == canonical_json(response) == '{"a":"café","z":[2,1]}'
    assert response == before
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == hashlib.sha256(
        render_json({"a": "café", "z": [2, 1]}).encode("utf-8")
    ).hexdigest()
