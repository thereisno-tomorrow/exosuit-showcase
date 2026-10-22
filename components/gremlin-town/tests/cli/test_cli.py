from __future__ import annotations

import json
import importlib
import io

import httpx
import pytest

from gremlin_town.cli.client import JsonApiClient
from gremlin_town.cli.main import main


cli_module = importlib.import_module("gremlin_town.cli.main")


@pytest.mark.parametrize("action,method,path", [
    ("status", "GET", "/v1/operator/grok-lifecycle"),
    ("reconcile", "POST", "/v1/operator/grok-lifecycle/reconcile"),
    ("refresh", "POST", "/v1/operator/participants/refresh"),
])
def test_lifecycle_cli_has_no_registry_payload(action, method, path, capsys):
    def handler(request):
        assert request.method == method and request.url.path == path
        assert not request.content
        return httpx.Response(200, json={"status":"unconfigured"})
    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    assert main(["lifecycle", action], client=client) == 0
    assert json.loads(capsys.readouterr().out) == {"status":"unconfigured"}


@pytest.mark.parametrize("status", [200, 403])
def test_cli_json_round_trips_unicode_on_windows_pipe(monkeypatch, status):
    text = "Diablo \u2192 Codex: \u2713 \U0001f916"
    payload = {"body": text} if status == 200 else {"error": {"code": "ACCESS_DENIED", "message": text}}
    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload)))
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(cli_module.sys, "stdout", stream)
    result = main(["requests", "read", "req-1"], client=client)
    stream.flush()
    assert result == (0 if status == 200 else 2)
    assert json.loads(raw.getvalue().decode("cp1252")) == payload


def test_cli_emits_server_json_without_reinterpreting_receipt(monkeypatch, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        assert request.url.path == "/v1/requests/req-1/reports"
        return httpx.Response(200, json={"operation_id": "op-1", "operation_state": "queued", "work_state": "in_progress"})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    result = main(["requests", "report", "req-1", "--json", '{"client_operation_id":"op-1","expected_revision":2,"action":"start"}'], client=client)
    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"operation_id": "op-1", "operation_state": "queued", "work_state": "in_progress"}


def test_cli_supports_dotted_operation_and_structured_error(monkeypatch, capsys) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "ACCESS_DENIED", "message": "resource unavailable or access denied"}})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    result = main(["operations.get", "op-1"], client=client)
    assert result == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "ACCESS_DENIED"


def test_cli_recovery_disposition_uses_operator_route(capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/requests/req-1/recovery-disposition"
        return httpx.Response(200, json={"operation_id": "op-recovery", "operation_state": "accepted"})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    result = main(["requests", "recovery-dispose", "req-1", "--json", '{"client_operation_id":"r1","expected_revision":2,"disposition":"keep_held","reason":"review"}'], client=client)
    assert result == 0
    assert json.loads(capsys.readouterr().out)["operation_id"] == "op-recovery"


def test_cli_request_list_forwards_recipient_filter(capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/requests"
        assert dict(request.url.params) == {
            "project": "demo",
            "recipient": "bob",
            "work_state": "blocked",
            "attention": "true",
            "limit": "10",
            "cursor": "20",
        }
        return httpx.Response(200, json={"items": []})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    result = main(
        [
            "requests",
            "list",
            "--project",
            "demo",
            "--recipient",
            "bob",
            "--work-state",
            "blocked",
            "--attention",
            "--limit",
            "10",
            "--cursor",
            "20",
        ],
        client=client,
    )
    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"items": []}


def test_cli_consumes_explicit_config_and_token_file_without_printing_secret(tmp_path, monkeypatch, capsys) -> None:
    token_file = tmp_path / "local-codex.token"
    token_file.write_text("  config-secret\n", encoding="utf-8")
    config = tmp_path / "local-codex.toml"
    config.write_text(f'base_url = "https://hub.example.test/"\ntoken_file = "{token_file.as_posix()}"\n', encoding="utf-8")
    captured = []

    class CapturingClient:
        def __init__(self, base_url, token, *, timeout):
            captured.append((base_url, token, timeout))

        def request(self, method, path, *, query=None, payload=None):
            return {"ok": True}

    monkeypatch.setattr(cli_module, "JsonApiClient", CapturingClient)
    monkeypatch.delenv("GREMLIN_TOWN_TOKEN", raising=False)
    result = main(["--config", str(config), "participants", "list"])

    output = capsys.readouterr().out
    assert result == 0
    assert json.loads(output) == {"ok": True}
    assert captured == [("https://hub.example.test", "config-secret", 10.0)]
    assert "config-secret" not in output


def test_cli_default_config_and_credential_precedence(monkeypatch, tmp_path, capsys) -> None:
    token_file = tmp_path / "local-codex.token"
    token_file.write_text("config-secret", encoding="utf-8")
    config = tmp_path / "local-codex.toml"
    config.write_text(f'base_url = "https://hub.example.test"\ntoken_file = "{token_file.as_posix()}"\n', encoding="utf-8")
    captured = []

    class CapturingClient:
        def __init__(self, base_url, token, *, timeout):
            captured.append((base_url, token))

        def request(self, method, path, *, query=None, payload=None):
            return {"ok": True}

    monkeypatch.setattr(cli_module, "JsonApiClient", CapturingClient)
    monkeypatch.setattr(cli_module, "_default_config_path", lambda: config)
    monkeypatch.delenv("GREMLIN_TOWN_TOKEN", raising=False)
    assert main(["participants", "list"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("GREMLIN_TOWN_TOKEN", "env-secret")
    assert main(["participants", "list"]) == 0
    capsys.readouterr()

    assert main(["--base-url", "http://diagnostic.invalid:8000", "--token", "cli-secret", "participants", "list"]) == 0
    capsys.readouterr()
    assert captured == [
        ("https://hub.example.test", "config-secret"),
        ("https://hub.example.test", "env-secret"),
        ("http://diagnostic.invalid:8000", "cli-secret"),
    ]


def test_cli_rejects_strict_config_and_missing_token_safely(tmp_path, capsys) -> None:
    cases = (
        ("unknown", 'unexpected = "secret-value"\n', "secret-value"),
        ("malformed", 'token_file = "secret-value\n', "secret-value"),
        ("relative", 'token_file = "relative.token"\n', "relative.token"),
        ("missing", f'token_file = "{(tmp_path / "missing.token").as_posix()}"\n', "missing.token"),
        ("insecure-base", 'base_url = "http://hub.example.test"\n', "http://hub.example.test"),
    )
    for name, contents, secret in cases:
        config = tmp_path / f"{name}.toml"
        config.write_text(contents, encoding="utf-8")
        result = main(["--config", str(config), "participants", "list"])
        output = capsys.readouterr().out
        assert result == 2
        assert json.loads(output)["error"]["code"] == "CONFIG_ERROR"
        assert secret not in output


def test_moments_signal_posts_kind_and_detail(capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/moments/signals"
        assert json.loads(request.content) == {"kind": "pc_on", "detail": None}
        return httpx.Response(200, json={"recorded": True, "kind": "pc_on", "actor": "local-codex", "at": "now"})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    assert main(["moments", "signal", "pc_on"], client=client) == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True


def test_moments_signal_rejects_unknown_kind() -> None:
    # argparse's usage error becomes main()'s normal SystemExit-to-return-code mapping.
    assert main(["moments", "signal", "phone_unlock"], client=JsonApiClient("https://hub.invalid", "secret")) == 2


def test_review_respond_posts_item_and_response(capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/review/responses"
        assert json.loads(request.content) == {
            "item_ref": "brn_bill", "response": "parked", "response_words": "Monday please",
        }
        return httpx.Response(200, json={"recorded": True, "item_ref": "brn_bill", "response": "parked"})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    result = main(
        ["review", "respond", "--item-ref", "brn_bill", "--response", "parked", "--response-words", "Monday please"],
        client=client,
    )
    assert result == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True


def test_review_respond_rejects_unknown_response_value() -> None:
    result = main(
        ["review", "respond", "--item-ref", "brn_bill", "--response", "maybe"],
        client=JsonApiClient("https://hub.invalid", "secret"),
    )
    assert result == 2
