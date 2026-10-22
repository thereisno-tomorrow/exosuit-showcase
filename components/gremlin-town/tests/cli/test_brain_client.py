import json

import httpx
import pytest

from gremlin_town.cli.client import JsonApiClient
from gremlin_town.cli.main import BRAIN_OPERATIONS, main


@pytest.mark.parametrize("operation", BRAIN_OPERATIONS)
def test_brain_cli_uses_bearer_and_exact_body(operation, capsys):
    payload = {"operation_id": "retained-key", "scope": "personal", "body": "exact input"}
    expected = (
        {
            "status": "accepted", "operation_id": "retained-key", "record_id": "brn_x",
            "version": "v1", "remote_observed": True,
        }
        if operation in ("memory.save", "memory.change")
        else {"status": "ok"}
    )
    calls = []
    def serve(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer client-specific-test-token"
        assert request.method == "POST"
        assert request.url.path == f"/v1/brain/{operation}"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json=expected)
    client = JsonApiClient("https://hub.example", "client-specific-test-token", transport=httpx.MockTransport(serve))
    assert main(["brain", operation, "--json", json.dumps(payload)], client=client) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert len(calls) == 1


@pytest.mark.parametrize("status,code", [(403, "ACCESS_DENIED"), (409, "brain.conflict"), (503, "brain.capability.unavailable")])
def test_brain_cli_refusal_never_retries_or_falls_back(status, code, capsys):
    calls = []
    def serve(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": "refused", "retryable": False}})
    client = JsonApiClient("https://hub.example", "client-specific-test-token", transport=httpx.MockTransport(serve))
    assert main(["brain", "memory.save", "--json", '{"operation_id":"stable-key"}'], client=client) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == code
    assert len(calls) == 1


@pytest.mark.parametrize("mismatch", [False, True, "identity", "owner", "refusal", "record"])
def test_verified_decision_separately_reads_canonical_owner(mismatch, capsys):
    from brain_core.records import record_from_mapping, render_record
    import hashlib
    canonical = record_from_mapping({"schema":"brain.record/v1", "id":"brn_20260923_010000_abcd",
        "kind":"decision", "title":"Prepare Friday review", "state":"accepted",
        "created_at":"2026-09-23T01:00:00+00:00", "updated_at":"2026-09-23T01:00:00+00:00",
        "assertion":"explicit", "confidence":1.0, "sensitivity":"private",
        "vaults":["vault://brain-home"], "owners":["brain-home"], "entities":[], "supersedes":[],
        "dedupe_key":"a"*64, "body":"", "data":{"because":"review", "forecloses":"delay", "governs":"personal", "encoded_in":"UNENCODED", "completeness_check":"Friday", "origin_id":"stable-key"},
        "evidence":[{"source_type":"durability_request", "locator":"origin:stable-key", "content_sha256":"b"*64, "excerpt":"Prepare review", "actor":"nic"},
            {"source_type":"authenticated_host", "locator":"gt:local-codex", "content_sha256":"c"*64, "actor":"local-codex"}]}, "test")
    digest = hashlib.sha256(render_record(canonical).encode()).hexdigest()
    receipt = {"schema": "brain.durability-receipt/v1", "writer_id": "brain-core-shared-durability-writer/v1",
        "status": "created", "owner_locator": "vault://brain-home", "origin_id": "stable-key",
        "model_calls": 0, "network_calls": 0, "canonical_sha256": digest,
        "owner_id":"brain-home", "canonical_identity":canonical.id, "refusal_code":None,
        "writer_generation":1, "writer_locator":"vault://brain-home", "canonical_locator":"vault://brain-home/records/decision/test.md", "semantic_key":"a"*64}
    if mismatch == "identity": receipt["canonical_identity"] = "OTHER-RECORD"
    if mismatch == "owner": receipt["owner_id"] = "OTHER-OWNER"
    if mismatch == "refusal": receipt["refusal_code"] = "refused"
    calls = []
    def serve(request):
        calls.append(request.url.path)
        if request.url.path.endswith("decision.record"):
            return httpx.Response(200, json={"status": "accepted", "remote_observed": True,
                "operation_id": "stable-key", "record_id": canonical.id, "owner_locator": "vault://brain-home",
                "content_sha256": digest, "durability_receipt": receipt,
                "transport_evidence": {"storage_transport": "github", "remote_io": True,
                    "canonical_readback": True, "network_calls": None}})
        return httpx.Response(200, json={"record_id": canonical.id, "owner_locator": "vault://brain-home",
            "kind": "decision", "content_sha256": "b" * 64 if mismatch is True else digest,
            "freshness": "current", "version": "commit-one", "canonical_record": {} if mismatch == "record" else canonical.as_dict()})
    client = JsonApiClient("https://hub.example", "token", transport=httpx.MockTransport(serve))
    payload = {"operation_id":"stable-key", "scope":"personal", "title":"Prepare Friday review", "choice":"Prepare review", "because":"review", "forecloses":"delay", "governing_scope":"personal", "encoded_in":"UNENCODED", "completeness_check":"Friday"}
    status = main(["brain", "decision.record", "--verify", "--json", json.dumps(payload)], client=client)
    result = json.loads(capsys.readouterr().out)
    assert calls == (["/v1/brain/decision.record"] if mismatch in ("identity", "owner", "refusal") else ["/v1/brain/decision.record", "/v1/brain/memory.read"])
    if mismatch:
        assert status == 2 and result["error"]["code"] == "RECEIPT_INVALID"
    else:
        assert status == 0 and result["verification"]["independent_canonical_readback"]
        assert result["transport_evidence"]["network_calls"] is None


def test_brain_write_timeout_retries_identical_body_and_returns_real_result(monkeypatch, capsys):
    import importlib; cli_main = importlib.import_module("gremlin_town.cli.main")
    monkeypatch.setattr(cli_main, "BRAIN_RETRY_DELAY", 0)
    payload = {"operation_id": "stable-key", "scope": "personal", "kind": "capture", "title": "t", "body": "b"}
    bodies, timeouts = [], []
    def serve(request):
        bodies.append(json.loads(request.content))
        timeouts.append(request.extensions["timeout"]["read"])
        if len(bodies) == 1:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, json={
            "status": "duplicate", "record_id": "brn_20260925_063735_5b5c330b54bb3f40",
            "operation_id": "stable-key", "version": "commit-real-result", "remote_observed": True,
        })
    client = JsonApiClient("https://hub.example", "token", transport=httpx.MockTransport(serve))
    assert main(["brain", "memory.save", "--json", json.dumps(payload)], client=client) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "duplicate"
    assert "retrying with identical input" in captured.err
    assert bodies == [payload, payload]
    assert timeouts == [cli_main.BRAIN_WRITE_TIMEOUT] * 2


def test_brain_cli_unreachable_hub_says_not_saved(monkeypatch, capsys):
    import importlib; cli_main = importlib.import_module("gremlin_town.cli.main")
    monkeypatch.setattr(cli_main, "BRAIN_RETRY_DELAY", 0)
    calls = []
    def serve(request):
        calls.append(request)
        raise httpx.ConnectError("unreachable", request=request)
    client = JsonApiClient("https://hub.example", "token", transport=httpx.MockTransport(serve))
    assert main(["brain", "memory.save", "--json", '{"operation_id":"stable-key"}'], client=client) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "HUB_UNREACHABLE"
    assert error["message"] == "Not saved: Gremlin Town at https://hub.example could not be reached."
    assert "may or may not" not in error["message"]
    assert len(calls) == cli_main.BRAIN_WRITE_MAX_ATTEMPTS


def test_brain_cli_deadline_says_not_saved(monkeypatch, capsys):
    import importlib; cli_main = importlib.import_module("gremlin_town.cli.main")
    monkeypatch.setattr(cli_main, "BRAIN_RETRY_DELAY", 0)
    calls = []
    def serve(request):
        calls.append(request)
        raise httpx.ReadTimeout("The read operation timed out", request=request)
    client = JsonApiClient("https://hub.example", "token", transport=httpx.MockTransport(serve))
    assert main(["brain", "memory.save", "--json", '{"operation_id":"stable-key"}'], client=client) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "SAVE_UNCONFIRMED"
    assert "Not saved" in error["message"] and "may or may not" not in error["message"]
    assert len(calls) == cli_main.BRAIN_WRITE_MAX_ATTEMPTS


def test_brain_cli_resends_on_retryable_server_answer(monkeypatch, capsys):
    import importlib; cli_main = importlib.import_module("gremlin_town.cli.main")
    monkeypatch.setattr(cli_main, "BRAIN_RETRY_DELAY", 0)
    payload = {"operation_id": "stable-key", "scope": "personal", "kind": "capture", "title": "t", "body": "b"}
    bodies = []
    def serve(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {
                "code": "brain.record.write_failed",
                "message": "Brain Home did not confirm the write; resend the same operation_id to get the real result",
                "retryable": True,
            }})
        return httpx.Response(200, json={
            "status": "accepted", "record_id": "brn_20260925_063735_5b5c330b54bb3f40",
            "operation_id": "stable-key", "version": "commit-real-result", "remote_observed": True,
        })
    client = JsonApiClient("https://hub.example", "token", transport=httpx.MockTransport(serve))
    assert main(["brain", "memory.save", "--json", json.dumps(payload)], client=client) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    assert bodies == [payload, payload]


def test_brain_cli_rejects_unconfirmed_success(capsys):
    payload = {"operation_id": "stable-key", "scope": "personal", "kind": "capture", "title": "t", "body": "b"}
    calls = []
    def serve(request):
        calls.append(request)
        return httpx.Response(200, json={
            "status": "accepted", "record_id": "brn_20260925_063735_5b5c330b54bb3f40",
            "operation_id": "a-different-operation-id", "version": "commit-x", "remote_observed": True,
        })
    client = JsonApiClient("https://hub.example", "token", transport=httpx.MockTransport(serve))
    assert main(["brain", "memory.save", "--json", json.dumps(payload)], client=client) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "SAVE_UNCONFIRMED"
    assert len(calls) == 1


def test_brain_read_keeps_default_timeout(capsys):
    timeouts = []
    def serve(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"matches": []})
    client = JsonApiClient("https://hub.example", "token", timeout=10.0, transport=httpx.MockTransport(serve))
    assert main(["brain", "memory.search", "--json", '{"query":"x"}'], client=client) == 0
    assert timeouts == [10.0]
