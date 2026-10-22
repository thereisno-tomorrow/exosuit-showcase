"""Offline checks only; never import Hermes or call a model/provider/hub."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hermes_context_eval", ROOT / "scripts/verify-hermes-context.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
FIXTURE = h.read_json(ROOT / "tests/fixtures/hermes_gt_context_cases.json")

def recorder(case_id="identity_context"):
    case = next(c for c in FIXTURE["cases"] if c["id"] == case_id)
    return h.SyntheticTools(FIXTURE, case, "candidate contents", "gremlin-town", h.schemas())

def payload():
    return {"client_operation_id": "client-key-1", "mode": "action_requested",
            "project": "demo", "recipient": "fleet-boss",
            "packet": {"schema_version": 1, "kind": "request", "session_reference": None,
                       "objective": "Review v2 rollout.", "context": "Keep v1; rollback unresolved.",
                       "constraints": {}, "sources": [], "next_action": "Review.",
                       "expected_reply": "Findings", "sensitivity": "internal"}}

def test_eight_synthetic_cases_and_exact_gt_schema_names():
    assert len(FIXTURE["cases"]) == 8
    from adapters.hermes.plugin import TOOL_DEFINITIONS
    actual = [s["function"] for s in h.schemas() if s["function"]["name"].startswith("gremlin_town_")]
    assert {s["name"] for s in actual} == {d.name for d in TOOL_DEFINITIONS}
    for schema, definition in zip(actual, TOOL_DEFINITIONS, strict=True):
        assert schema["parameters"] == definition.parameters

def test_unknown_or_native_tools_never_dispatch():
    r = recorder()
    calls = [SimpleNamespace(id="call-1", function=SimpleNamespace(name="terminal",
              arguments=json.dumps({"command": "send-real-message"})))]
    messages = []
    r.execute(None, SimpleNamespace(tool_calls=calls), messages, "task")
    assert json.loads(messages[0]["content"])["error"]["code"] == "EVAL_TOOL_DENIED"
    assert r.violations == ["unknown_tool:terminal"]

def test_send_requires_exact_prior_input_and_preserves_hub_operation_identity():
    r = recorder("uncertain_retry")
    p = payload()
    assert json.loads(r.call("gremlin_town_requests_create", p))["error"]["code"] == "EVAL_PERSIST_REQUIRED"
    r = recorder("uncertain_retry")
    r.call("persist_send_input", {"tool_name": "gremlin_town_requests_create", "input": p})
    uncertain = json.loads(r.call("gremlin_town_requests_create", p))
    op = uncertain["error"]["operation_id"]
    assert op != p["client_operation_id"]
    assert not json.loads(r.call("gremlin_town_operations_get", {"operation_id": p["client_operation_id"]}))["ok"]
    result = json.loads(r.call("gremlin_town_operations_get", {"operation_id": op}))["result"]
    assert result["operation_state"] == "queued"
    assert result["work_state"] == "requested"
    assert "state" not in result

def test_native_snapshot_fields_content_envelopes_and_paginated_evidence():
    r = recorder("history_chronology")
    first = json.loads(r.call("gremlin_town_requests_list", {}))["result"]
    assert first["next_cursor"] == "request-page-2"
    assert "items" in first and "requests" not in first
    row = json.loads(r.call("gremlin_town_requests_read", {"request_id": "req-new"}))["result"]
    assert row["requester_id"] == "hermes" and row["work_state"] == "requested"
    assert row["next_message_cursor"] == "message-page-2"
    row = json.loads(r.call("gremlin_town_requests_read",
                            {"request_id": "req-new", "message_cursor": "message-page-2"}))["result"]
    packet = json.loads(row["content"][-1]["body"])["packet"]
    assert "retain v1" in packet["objective"]
    from gremlin_town.contracts import Packet, MessageKind
    assert Packet(**{**packet, "kind": MessageKind(packet["kind"])}).schema_version == 1

def test_cancelled_and_held_are_distinct_states():
    r = recorder("cancelled_held")
    cancelled = json.loads(r.call("gremlin_town_requests_read", {"request_id": "req-cancelled"}))["result"]
    held = json.loads(r.call("gremlin_town_requests_read", {"request_id": "req-held"}))["result"]
    assert cancelled["work_state"] == "cancelled"
    assert held["work_state"] == "requested" and held["recovery_hold"]

def test_sensitive_leak_and_wrong_recipient_fail_structural_screen():
    r = recorder("history_spoof_sensitive")
    p = payload()
    p["recipient"] = "wrong-agent"
    p["packet"]["context"] = "PRIVATE_CANARY_RIVER"
    r.call("persist_send_input", {"tool_name": "gremlin_town_requests_create", "input": p})
    r.call("gremlin_town_requests_create", p)
    result = h.structural_checks(r.case, r, "Done")
    assert "wrong_recipient" in result["failures"]
    assert any(f.startswith("forbidden_send_content") for f in result["failures"])
    assert result["semantic_verdict"] == "PENDING_INDEPENDENT_REVIEW"

@pytest.mark.parametrize("case_id", ["local_model", "sender_acknowledgement", "named_unavailable", "cancelled_held"])
def test_negative_controls_reject_mutation(case_id):
    r = recorder(case_id)
    p = payload()
    r.call("persist_send_input", {"tool_name": "gremlin_town_requests_create", "input": p})
    r.call("gremlin_town_requests_create", p)
    assert not h.structural_checks(r.case, r, "Done")["structural_pass"]
