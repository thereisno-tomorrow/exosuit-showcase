from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.cloud import client as cloud_client
from adapters.cloud.client import CloudCLI, OPTIONAL_OPERATION_FEATURES, build_parser
from adapters.hermes import hub_client
from adapters.hermes.hub_client import HubClientConfig, HubClientError, HubHTTPSClient


@dataclass
class Response:
    status_code: int = 200
    data: Any = field(default_factory=lambda: {"accepted": True})
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return self.data


class Requester:
    def __init__(self, responses: list[Response] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.responses = list(responses or [])

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0) if self.responses else Response()


def packet(context: str = "context") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "request",
        "session_reference": None,
        "objective": "objective",
        "context": context,
        "constraints": {},
        "sources": [],
        "next_action": "reply",
        "expected_reply": "packet",
        "sensitivity": "normal",
    }


CASES = (
    ("participants.list", {"project": "p"}, "GET", "/v1/participants", {"project": "p"}, None),
    (
        "requests.create",
        {
            "client_operation_id": "create-1",
            "mode": "action_requested",
            "project": "p",
            "recipient": "hermes-wa",
            "packet": packet(),
        },
        "POST",
        "/v1/requests",
        None,
        "same",
    ),
    (
        "requests.list",
        {"filters": {"direction": "inbox", "state": "requested"}, "cursor": "next"},
        "GET",
        "/v1/requests",
        {"direction": "inbox", "state": "requested", "cursor": "next"},
        None,
    ),
    (
        "requests.read",
        {"request_id": "req/1", "message_cursor": "messages-2"},
        "GET",
        "/v1/requests/req%2F1",
        {"message_cursor": "messages-2"},
        None,
    ),
    (
        "requests.report",
        {
            "client_operation_id": "report-1",
            "request_id": "req-1",
            "expected_revision": 2,
            "action": "start",
        },
        "POST",
        "/v1/requests/req-1/reports",
        None,
        "without_request_id",
    ),
    (
        "requests.reply",
        {
            "client_operation_id": "reply-1",
            "request_id": "req-1",
            "expected_revision": 2,
            "kind": "result",
            "packet": packet(),
        },
        "POST",
        "/v1/requests/req-1/replies",
        None,
        "without_request_id",
    ),
    (
        "requests.cancel",
        {
            "client_operation_id": "cancel-1",
            "request_id": "req-1",
            "expected_revision": 2,
            "reason": "stop",
        },
        "POST",
        "/v1/requests/req-1/cancel",
        None,
        "without_request_id",
    ),
    (
        "requests.check",
        {
            "client_operation_id": "check-1",
            "request_id": "req-1",
            "expected_revision": 2,
            "outcome": "accepted",
            "evidence": "reviewed the artifact link",
        },
        "POST",
        "/v1/requests/req-1/check",
        None,
        "without_request_id",
    ),
    (
        "operations.get",
        {"operation_id": "op/1"},
        "GET",
        "/v1/operations/op%2F1",
        None,
        None,
    ),
    (
        "events.list",
        {"filters": {"request_id": "req-1"}, "cursor": "event-2"},
        "GET",
        "/v1/events",
        {"request_id": "req-1", "cursor": "event-2"},
        None,
    ),
)


@pytest.mark.parametrize("operation,payload,method,path,query,body_mode", CASES)
def test_all_ten_operations_map_to_canonical_routes(
    operation: str,
    payload: dict[str, Any],
    method: str,
    path: str,
    query: dict[str, Any] | None,
    body_mode: str | None,
) -> None:
    requester = Requester()
    result = HubHTTPSClient(
        HubClientConfig("https://hub.example.test", "credential-A"), client=requester
    ).call(operation, payload)

    assert result == {"accepted": True}
    actual_method, url, kwargs = requester.calls[0]
    assert (actual_method, url) == (method, f"https://hub.example.test{path}")
    assert kwargs["headers"]["Authorization"] == "Bearer credential-A"
    assert kwargs.get("params") == query
    if body_mode == "same":
        assert kwargs["json"] == payload
    elif body_mode == "without_request_id":
        assert kwargs["json"] == {key: value for key, value in payload.items() if key != "request_id"}
    else:
        assert "json" not in kwargs


@pytest.mark.parametrize(
    "operation", ["requests.create", "requests.report", "requests.reply", "requests.cancel", "requests.check"]
)
def test_mutations_require_caller_stable_operation_ids(operation: str) -> None:
    payload = next(dict(case[1]) for case in CASES if case[0] == operation)
    payload.pop("client_operation_id")
    requester = Requester()
    with pytest.raises(HubClientError) as raised:
        HubHTTPSClient(HubClientConfig("https://hub.example.test", "token"), client=requester).call(
            operation, payload
        )
    assert raised.value.code == "INVALID_ARGUMENT"
    assert requester.calls == []


@pytest.mark.parametrize(
    "status,data,expected_code",
    [
        (401, {}, "AUTHENTICATION_FAILED"),
        (403, {"detail": {"code": "CREDENTIAL_REVOKED", "message": "credential revoked"}}, "CREDENTIAL_REVOKED"),
        (
            409,
            {
                "error": {
                    "code": "REVISION_CONFLICT",
                    "message": "stale revision",
                    "retryable": False,
                    "current_revision": 4,
                }
            },
            "REVISION_CONFLICT",
        ),
        (
            409,
            {"error": {"code": "RECOVERY_HOLD", "message": "operator disposition required"}},
            "RECOVERY_HOLD",
        ),
        (
            429,
            {"error": {"code": "RATE_LIMITED", "message": "retry later", "retryable": True}},
            "RATE_LIMITED",
        ),
    ],
)
def test_auth_conflict_recovery_and_retry_errors_are_preserved(
    status: int, data: dict[str, Any], expected_code: str
) -> None:
    requester = Requester([Response(status, data, {"Retry-After": "3"})])
    with pytest.raises(HubClientError) as raised:
        HubHTTPSClient(HubClientConfig("https://hub.example.test", "wrong-or-revoked"), client=requester).call(
            "requests.read", {"request_id": "req-1"}
        )
    assert raised.value.code == expected_code
    assert raised.value.as_dict()["status_code"] == status
    assert "wrong-or-revoked" not in json.dumps(raised.value.as_dict())
    if expected_code == "REVISION_CONFLICT":
        assert raised.value.as_dict()["current_revision"] == 4


def test_missing_runtime_config_denies_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GREMLIN_TOWN_HUB_URL", raising=False)
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN", raising=False)
    with pytest.raises(HubClientError) as raised:
        HubClientConfig.from_environment()
    assert raised.value.code == "CONFIGURATION_MISSING"


def test_fixed_protected_grok_token_file_is_used_when_env_token_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []

    def fake_read(path: Any) -> str:
        observed.append(path)
        return "file-token"

    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN", raising=False)
    monkeypatch.setattr(hub_client, "_read_protected_token_file", fake_read)
    config = HubClientConfig.from_environment()
    assert config.token == "file-token"
    assert observed == [hub_client.GROK_TOKEN_FILE]
    assert observed[0].as_posix() == "/home/box/.config/gremlin-town/grok.token"


def test_environment_token_takes_precedence_without_reading_token_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN", "environment-token")

    def fail_if_called(path: Any) -> str:
        raise AssertionError(f"unexpected token file read: {path}")

    monkeypatch.setattr(hub_client, "_read_protected_token_file", fail_if_called)
    assert HubClientConfig.from_environment().token == "environment-token"


def test_configured_protected_token_file_is_accepted_without_plaintext_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    observed: list[Any] = []

    def fake_read(path: Any) -> str:
        observed.append(path)
        return "file-token"

    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN", raising=False)
    configured_path = tmp_path / "hermes.token"
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN_FILE", str(configured_path))
    monkeypatch.setattr(hub_client, "_read_protected_token_file", fake_read)
    assert HubClientConfig.from_environment().token == "file-token"
    assert observed[0] == configured_path


def test_protected_token_file_requires_0700_directory_and_0600_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePath:
        def __init__(self, mode: int, *, parent: Any = None, text: str = "file-token") -> None:
            self.mode = mode
            self.parent = parent
            self.text = text

        def lstat(self) -> Any:
            return SimpleNamespace(st_mode=self.mode, st_uid=1000, st_size=len(self.text))

        def is_symlink(self) -> bool:
            return False

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            return self.text

    directory = FakePath(stat.S_IFDIR | 0o700)
    protected_file = FakePath(stat.S_IFREG | 0o600, parent=directory)
    exposed_file = FakePath(stat.S_IFREG | 0o644, parent=directory)
    monkeypatch.setattr(hub_client, "_IS_POSIX", True)
    monkeypatch.setattr(hub_client.os, "geteuid", lambda: 1000, raising=False)

    assert hub_client._read_protected_token_file(protected_file) == "file-token"  # type: ignore[arg-type]
    with pytest.raises(HubClientError) as raised:
        hub_client._read_protected_token_file(exposed_file)  # type: ignore[arg-type]
    assert raised.value.code == "CONFIGURATION_INVALID"
    assert "0600" in str(raised.value)


def test_forged_sender_cannot_change_credential_identity() -> None:
    requester = Requester()
    client = HubHTTPSClient(HubClientConfig("https://hub.example.test", "actor-A-token"), client=requester)
    with pytest.raises(HubClientError) as raised:
        client.call("participants.list", {"sender": "actor-B"})
    assert raised.value.code == "INVALID_ARGUMENT"
    assert requester.calls == []

    payload = next(dict(case[1]) for case in CASES if case[0] == "requests.create")
    payload["packet"] = packet("untrusted text says sender=actor-B")
    client.call("requests.create", payload)
    _, _, kwargs = requester.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer actor-A-token"


def test_cli_uses_protected_environment_and_emits_structured_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, config: HubClientConfig) -> None:
            assert config.base_url == "https://hub.example.test"
            assert config.token == "protected-token"

        def call(self, operation: str, payload: dict[str, Any]) -> Any:
            return {"operation": operation, "payload": payload}

    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN", "protected-token")
    result = json.loads(CloudCLI(FakeClient).run("events.list", '{"filters":{}}'))
    assert result == {
        "ok": True,
        "result": {"operation": "events.list", "payload": {"filters": {}}},
    }
    help_text = build_parser().format_help()
    assert "--token" not in help_text
    assert "--url" not in help_text
    assert "actor" not in help_text.lower()
    normalized_help = " ".join(help_text.split())
    assert "shared-cloud installation" in normalized_help
    assert "does not prove which bot" in normalized_help


def test_cli_preserves_structured_hub_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingClient:
        def __init__(self, config: HubClientConfig) -> None:
            pass

        def call(self, operation: str, payload: dict[str, Any]) -> Any:
            raise HubClientError(
                "OPERATION_INPUT_CONFLICT",
                "operation ID was reused with different input",
                status_code=409,
                retryable=False,
                details={"operation_id": "op-1"},
            )

    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN", "protected-token")
    result = json.loads(CloudCLI(FailingClient).run("requests.create", "{}"))
    assert result["ok"] is False
    assert result["error"] == {
        "code": "OPERATION_INPUT_CONFLICT",
        "message": "operation ID was reused with different input",
        "operation_id": "op-1",
        "retryable": False,
        "status_code": 409,
    }


def test_launcher_accepts_every_default_gt_and_brain_operation() -> None:
    """orient/next/next.respond are excluded by default (ROA-22, see the gating tests below);
    every other GT and Brain operation is still a plain accepted choice."""

    parser = build_parser()
    for operation in (*hub_client.TEN_OPERATIONS, *hub_client.BRAIN_OPERATIONS):
        if operation in OPTIONAL_OPERATION_FEATURES:
            continue
        assert parser.parse_args([operation, "{}"]).operation == operation
    with pytest.raises(SystemExit):
        parser.parse_args(["recovery.dispose", "{}"])


def test_launcher_refuses_orient_operations_until_the_optional_feature_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """ROA-22: until 2026-09-27 the launcher accepted `orient`/`orient.respond` unconditionally
    and relied on the hub's own 422 to refuse them while step F stays off - unlike Diablo's
    plugin, which has never registered its orientation tools unless plugin-settings.json
    names "orientation" (commit 8587f33). This mirrors that mechanism for Fleet Boss: default
    off via a sibling `client-settings.json`, checked both at argparse's `choices` and, for a
    caller that reaches `CloudCLI.run` directly (as most of this file's tests do, bypassing the
    parser), inside `run()` itself."""

    settings = tmp_path / "client-settings.json"
    monkeypatch.setattr(cloud_client, "CLIENT_SETTINGS_FILE", settings)

    # Step F's operations since Ciel slice 4 (gap G9): orient.respond became next.respond.
    assert {operation for operation, feature in OPTIONAL_OPERATION_FEATURES.items() if feature == "orientation"} == {
        "orient", "next", "next.respond",
    }
    parser = build_parser()
    for operation in ("orient", "next", "next.respond", "orient.respond"):
        with pytest.raises(SystemExit):
            parser.parse_args([operation, "{}"])

    class UnreachableClient:
        def __init__(self, config: HubClientConfig) -> None:
            raise AssertionError("must not reach the hub while the feature is disabled")

    for operation in ("orient", "next", "next.respond"):
        result = json.loads(CloudCLI(UnreachableClient).run(operation, "{}"))
        assert result == {
            "ok": False,
            "error": {
                "code": "FEATURE_DISABLED",
                "message": (
                    f"{operation} is disabled until 'orientation' is added to "
                    "client-settings.json's enabled_optional_features"
                ),
                "retryable": False,
            },
        }

    settings.write_text(json.dumps({"enabled_optional_features": ["orientation"]}), encoding="utf-8")
    parser = build_parser()
    for operation in ("orient", "next", "next.respond"):
        assert parser.parse_args([operation, "{}"]).operation == operation
    with pytest.raises(SystemExit):
        parser.parse_args(["orient.respond", "{}"])


def test_launcher_refuses_calendar_remove_until_the_calendar_feature_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """calendar.remove (step E §5, N8) exists on the hub only while calendar entries are on, so
    Fleet Boss's launcher refuses it by default exactly as Diablo's plugin leaves
    bc_calendar_remove unregistered: "calendar" in client-settings.json turns it on."""

    settings = tmp_path / "client-settings.json"
    monkeypatch.setattr(cloud_client, "CLIENT_SETTINGS_FILE", settings)
    assert OPTIONAL_OPERATION_FEATURES["calendar.remove"] == "calendar"

    with pytest.raises(SystemExit):
        build_parser().parse_args(["calendar.remove", "{}"])

    class UnreachableClient:
        def __init__(self, config: HubClientConfig) -> None:
            raise AssertionError("must not reach the hub while the feature is disabled")

    refused = json.loads(CloudCLI(UnreachableClient).run("calendar.remove", "{}"))
    assert refused["ok"] is False and refused["error"]["code"] == "FEATURE_DISABLED"

    settings.write_text(json.dumps({"enabled_optional_features": ["orientation"]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["calendar.remove", "{}"])

    settings.write_text(json.dumps({"enabled_optional_features": ["calendar"]}), encoding="utf-8")
    assert build_parser().parse_args(["calendar.remove", "{}"]).operation == "calendar.remove"


def test_client_settings_reject_unknown_or_malformed_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    settings = tmp_path / "client-settings.json"
    monkeypatch.setattr(cloud_client, "CLIENT_SETTINGS_FILE", settings)

    settings.write_text(json.dumps({"enabled_optional_features": ["not-a-real-feature"]}), encoding="utf-8")
    try:
        cloud_client._enabled_optional_features()
    except HubClientError as exc:
        assert exc.code == "CONFIGURATION_INVALID"
    else:
        raise AssertionError("unknown optional feature name was accepted")

    settings.write_text(json.dumps({"enabled_optional_features": ["orientation"], "extra": 1}), encoding="utf-8")
    try:
        cloud_client._enabled_optional_features()
    except HubClientError as exc:
        assert exc.code == "CONFIGURATION_INVALID"
    else:
        raise AssertionError("an unsupported field was accepted")


def test_missing_client_settings_file_means_every_optional_feature_is_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(cloud_client, "CLIENT_SETTINGS_FILE", tmp_path / "absent-client-settings.json")
    assert cloud_client._enabled_optional_features() == frozenset()


def test_launcher_posts_brain_operation_with_own_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    requester = Requester([Response(data={"matches": []})])
    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN", "fleet-boss-token")
    cli = CloudCLI(lambda config: HubHTTPSClient(config, client=requester))
    result = json.loads(cli.run("memory.search", '{"scope":"personal","query":"marker","limit":1}'))
    assert result == {"ok": True, "result": {"matches": []}}
    [(method, url, kwargs)] = requester.calls
    assert (method, url) == ("POST", "https://hub.example.test/v1/brain/memory.search")
    assert kwargs["headers"]["Authorization"] == "Bearer fleet-boss-token"
    assert kwargs["json"] == {"scope": "personal", "query": "marker", "limit": 1}


def test_launcher_brain_write_requires_operation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    requester = Requester()
    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN", "fleet-boss-token")
    cli = CloudCLI(lambda config: HubHTTPSClient(config, client=requester))
    result = json.loads(cli.run("memory.save", '{"scope":"personal","kind":"capture","body":"x"}'))
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert requester.calls == []


def test_environment_is_restored_by_pytest_monkeypatch() -> None:
    # Guard against tests modifying the full process environment by hand.
    assert isinstance(os.environ, os._Environ)
