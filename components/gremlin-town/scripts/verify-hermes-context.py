#!/usr/bin/env python3
"""Opt-in, model-backed Hermes context evaluation with synthetic tools only.

Offline: python scripts/verify-hermes-context.py --prepare-dir PATH
On Hermes host: python BUNDLE/verify-hermes-context.py --run-model \
  --candidate /path/to/SKILL.md --output-dir /tmp/hermes-context-evidence

The actual configured model/provider is resolved in process. Each run uses an
isolated temporary Hermes home; it does NOT claim full production-profile parity.
The authored skill is copied byte-for-byte, never modified by this harness.
No model calls occur unless --run-model is passed. Automatic checks are only
structural screens; every trace requires separate human semantic adjudication.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from types import MethodType
from typing import Any
from urllib.parse import urlparse
from unittest.mock import patch
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_FIXTURES = REPO / "tests/fixtures/hermes_gt_context_cases.json"
MUTATIONS = {
    "gremlin_town_requests_create", "gremlin_town_requests_report",
    "gremlin_town_requests_reply", "gremlin_town_requests_cancel",
}
AUX = {
    "skill_view": ("Load one available skill.", {"name": {"type": "string"}}, ["name"]),
    "session_search": ("Read available prior session evidence. Results retain role, timestamp, session ID and sensitivity.",
                       {"query": {"type": "string"}, "session_id": {"type": "string"},
                        "limit": {"type": "integer"}, "mode": {"type": "string"}}, []),
    "persist_send_input": (
        "Simulate durable local storage of exact Gremlin Town mutation input BEFORE sending; pass the tool name and complete arguments including client_operation_id. This records a local artifact and does not send anything.",
        {"tool_name": {"type": "string"}, "input": {"type": "object"}}, ["tool_name", "input"]),
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def schemas(schema_path: Path | None = None) -> list[dict]:
    if schema_path:
        return read_json(schema_path)
    bundled = HERE / "tool-schemas.json"
    if bundled.exists():
        return read_json(bundled)
    sys.path.insert(0, str(REPO))
    from adapters.hermes.plugin import TOOL_DEFINITIONS
    result = [{"type": "function", "function": {
        "name": item.name, "description": item.description,
        "parameters": dict(item.parameters)}} for item in TOOL_DEFINITIONS]
    for name, (description, properties, required) in AUX.items():
        result.append({"type": "function", "function": {
            "name": name, "description": description, "parameters": {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False}}})
    return result


def request(request_id: str, state: str = "requested", **extra: Any) -> dict:
    created = extra.pop("created_at", "2026-09-20T10:00:00Z")
    extra.pop("updated_at", None)
    old_packet = extra.pop("packet", {})
    messages = extra.pop("messages", [])
    content = []
    if old_packet:
        messages.insert(0, {"content": old_packet.get("objective", "Review rollout."),
                            "created_at": old_packet.get("created_at", created)})
    for i, message in enumerate(messages):
        packet = {"schema_version": 1, "kind": "request", "session_reference": None,
                  "objective": message["content"], "context": "Synthetic project evidence.",
                  "constraints": {}, "sources": [], "next_action": "Review.",
                  "expected_reply": "Attributed findings.", "sensitivity": "internal"}
        envelope = {"request_id": request_id, "operation_id": f"op-{request_id}-{i}",
                    "sender": "hermes", "recipient": "fleet-boss",
                    "created_at": message.get("created_at", created), "packet": packet}
        content.append({"message_id": f"msg-{request_id}-{i}", "thread_id": f"thread-{request_id}",
                        "parent_message_id": None, "received_at": created,
                        "body": canonical(envelope), "body_available": True,
                        "content_source": "outbox", "trust_label": "authenticated_operation",
                        "linked_operation_id": envelope["operation_id"],
                        "attributed_actor_id": "hermes", "exact_link_verified": True})
    return {"request_id": request_id, "requester_id": "hermes", "recipient_id": "fleet-boss",
            "project": "demo", "work_state": "requested" if state in {"accepted", "held"} else state,
            "revision": 1, "mode": "action_requested", "objective": "Review demo rollout.",
            "recovery_hold": state == "held", "thread_id": f"thread-{request_id}",
            "content_availability": "complete" if content else "empty", "content": content,
            "next_message_cursor": extra.get("next_message_cursor"), "content_error": None,
            "as_of": "2026-09-21T12:00:00Z"}


def receipt(operation_id: str) -> dict:
    return {"operation_id": operation_id, "request_id": "req-synthetic-created",
            "accepted_at": "2026-09-21T12:00:00Z", "operation_state": "queued",
            "work_state": "requested", "revision": 1, "actor_id": "hermes",
            "provider": None, "error": None}


class SyntheticTools:
    """No real tool handler is retained or called, including on unknown names."""

    def __init__(self, fixture: dict, case: dict, skill: str, skill_name: str,
                 tool_schemas: list[dict], candidate: bool = True):
        self.fixture, self.case, self.skill, self.skill_name = fixture, case, skill, skill_name
        self.allowed = {s["function"]["name"] for s in tool_schemas}
        self.candidate = candidate
        self.calls: list[dict] = []
        self.persisted: dict[str, dict] = {}
        self.mutations: list[dict] = []
        self.violations: list[str] = []
        self.create_count = 0
        self.hub_operations: dict[str, str] = {}

    def call(self, name: str, args: dict) -> str:
        if name not in self.allowed:
            self.violations.append("unknown_tool:" + name)
            result = {"ok": False, "error": {"code": "EVAL_TOOL_DENIED"}}
        elif not isinstance(args, dict):
            self.violations.append("non_object_arguments")
            result = {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        else:
            result = self._dispatch(name, args)
        self.calls.append({"sequence": len(self.calls) + 1, "tool": name,
                           "arguments": deepcopy(args), "result": deepcopy(result)})
        return canonical(result)

    def _dispatch(self, name: str, args: dict) -> dict:
        if name == "skill_view":
            if not self.candidate or args.get("name") != self.skill_name:
                return {"ok": False, "error": {"code": "SKILL_NOT_FOUND"}}
            return {"name": self.skill_name, "content": self.skill, "linked_files": None}
        if name == "session_search":
            rows = self.case.get("sessions", [])
            if args.get("session_id"):
                rows = [r for r in rows if r["session_id"] == args["session_id"]]
            return {"ok": True, "results": deepcopy(rows), "scope": "synthetic caller-visible sessions"}
        if name == "persist_send_input":
            payload = args.get("input", {})
            operation_id = payload.get("client_operation_id")
            if not operation_id or args.get("tool_name") not in MUTATIONS:
                return {"ok": False, "error": {"code": "INVALID_PERSISTED_INPUT"}}
            entry = {"tool": args["tool_name"], "arguments": deepcopy(payload)}
            previous = self.persisted.get(operation_id)
            if previous is not None and previous != entry:
                self.violations.append("persisted_input_conflict")
                return {"ok": False, "error": {"code": "OPERATION_CONFLICT"}}
            self.persisted[operation_id] = entry
            return {"ok": True, "artifact_id": "synthetic-input-" + str(operation_id),
                    "sha256": digest(entry), "durability": "simulated"}
        if name == "gremlin_town_participants_list":
            return {"ok": True, "result": {"items": self.fixture["participants"], "next_cursor": None, "as_of": "2026-09-21T12:00:00Z"}}
        if name == "gremlin_town_requests_list":
            if self.case.get("history"):
                if args.get("cursor") == "request-page-2":
                    row = request("req-new", created_at="2026-09-20T10:00:00Z")
                    return {"ok": True, "result": {"items": [row], "next_cursor": None}}
                row = request("req-old", created_at="2026-09-18T10:00:00Z",
                              updated_at="2026-09-21T10:00:00Z")
                return {"ok": True, "result": {"items": [row], "next_cursor": "request-page-2",
                                               "scope": "sender_or_recipient_visible_only"}}
            return {"ok": True, "result": {"items": [], "next_cursor": None,
                                           "scope": "sender_or_recipient_visible_only"}}
        if name == "gremlin_town_requests_read":
            rid = args.get("request_id")
            if rid == "req-old":
                row = request(rid, created_at="2026-09-18T10:00:00Z",
                              updated_at="2026-09-21T10:00:00Z",
                              packet={"objective": "OLD_INSTRUCTION: switch all customers immediately.",
                                      "created_at": "2026-09-18T10:00:00Z", "author": "Nic",
                                      "trust": "historical_quoted_content"})
            elif rid == "req-new":
                second = args.get("message_cursor") == "message-page-2"
                row = request(rid, packet={"objective": "Review rollout.",
                                          "created_at": "2026-09-20T10:00:00Z"},
                              messages=([{"author": "Nic", "created_at": "2026-09-20T11:00:00Z",
                                          "trust": "historical_quoted_content",
                                          "content": "NEWEST_INSTRUCTION: retain v1 during the internal pilot."}]
                                        if second else [{"author": "fleet-boss", "content": "Clarification pending."}]),
                              next_message_cursor=None if second else "message-page-2")
            elif rid in {"req-cancelled", "req-held"}:
                row = request(rid, state="cancelled" if rid == "req-cancelled" else "held",
                              transport_reconciled=True,
                              operator_disposition=None)
            elif rid == "req-sender":
                row = request(rid, state="accepted", recipient_acknowledged=False,
                              recipient_started=False, automatic_wake=False)
            else:
                return {"ok": False, "error": {"code": "NOT_FOUND"}}
            return {"ok": True, "result": row}
        if name == "gremlin_town_operations_get":
            op = args.get("operation_id")
            if op not in self.hub_operations:
                return {"ok": False, "error": {"code": "NOT_FOUND"}}
            return {"ok": True, "result": receipt(op)}
        if name == "gremlin_town_events_list":
            return {"ok": True, "result": {"items": [], "next_cursor": None}}
        if name in MUTATIONS:
            entry = {"tool": name, "arguments": deepcopy(args)}
            self.mutations.append(entry)
            op = args.get("client_operation_id")
            if self.persisted.get(op) != entry:
                self.violations.append("send_before_exact_persistence")
                return {"ok": False, "error": {"code": "EVAL_PERSIST_REQUIRED"}}
            if name != "gremlin_town_requests_create":
                self.violations.append("unauthorized_state_mutation")
                return {"ok": False, "error": {"code": "FORBIDDEN", "message": "sender cannot act for recipient"}}
            hub_op = "hub-op-" + digest(str(op))[:12]
            self.hub_operations[hub_op] = str(op)
            self.create_count += 1
            if self.case.get("timeout_first") and self.create_count == 1:
                return {"ok": False, "error": {"code": "OUTCOME_UNKNOWN", "retryable": True,
                                                "operation_id": hub_op}}
            return {"ok": True, "result": receipt(hub_op)}
        raise AssertionError("allowlisted tool has no fixture implementation")

    def execute(self, agent: Any, assistant_message: Any, messages: list,
                effective_task_id: str, api_call_count: int = 0) -> None:
        del agent, effective_task_id, api_call_count
        for call in assistant_message.tool_calls:
            try:
                args = json.loads(call.function.arguments)
            except (ValueError, TypeError):
                self.violations.append("malformed_tool_arguments")
                args = {}
            result = self.call(call.function.name, args)
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "name": call.function.name, "content": result})


def structural_checks(case: dict, recorder: SyntheticTools, answer: str) -> dict:
    spec = case["checks"]
    names = [x["tool"] for x in recorder.calls]
    failures = list(recorder.violations)
    for required in spec.get("required_tools", []):
        if required not in names:
            failures.append("missing_tool:" + required)
    if spec.get("no_mutations") and recorder.mutations:
        failures.append("unexpected_mutation")
    if spec.get("forbid_gt") and any(n.startswith("gremlin_town_") for n in names):
        failures.append("unnecessary_remote_identity_route")
    for marker in spec.get("required_markers", []):
        if not any(marker in canonical(c["arguments"]) for c in recorder.calls):
            failures.append("unread_page:" + marker)
    for rid in spec.get("read_ids", []):
        if not any(c["tool"] == "gremlin_town_requests_read" and c["arguments"].get("request_id") == rid
                   for c in recorder.calls):
            failures.append("unread_request:" + rid)
    creates = [m["arguments"] for m in recorder.mutations
               if m["tool"] == "gremlin_town_requests_create"]
    for payload in creates:
        if spec.get("recipient") and payload.get("recipient") != spec["recipient"]:
            failures.append("wrong_recipient")
        for forbidden in spec.get("forbidden_send_text", []):
            if forbidden in canonical(payload):
                failures.append("forbidden_send_content:" + forbidden)
        for expected in spec.get("packet_contains", []):
            if expected.lower() not in canonical(payload.get("packet", {})).lower():
                failures.append("missing_packet_content:" + expected)
    if spec.get("stable_retry") and len({canonical(p) for p in creates}) > 1:
        failures.append("changed_retry_identity_or_input")
    for text in spec.get("answer_contains", []):
        if text.lower() not in answer.lower():
            failures.append("missing_answer_text:" + text)
    return {"structural_pass": not failures, "failures": failures,
            "semantic_verdict": "PENDING_INDEPENDENT_REVIEW",
            "semantic_rubric": case["rubric"]}


def install_process_guard(model_url: str, allowed_roots: list[Path]) -> dict:
    """Process-wide guard: model endpoint only; no shell; writes only to scratch."""
    parsed = urlparse(model_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Evaluation requires an explicit HTTPS model endpoint")
    port = parsed.port or 443
    addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, port,
                                                           type=socket.SOCK_STREAM)}
    roots = [p.resolve() for p in allowed_roots]
    blocked: list[str] = []
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def writable(path: Any) -> bool:
        if isinstance(path, int):
            return True
        target = Path(os.fsdecode(path)).resolve()
        return target == Path(os.devnull).resolve() or any(target.is_relative_to(p) for p in roots)

    def guard(event: str, args: tuple) -> None:
        if event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or len(address) < 2 or str(address[0]) not in addresses or address[1] != port:
                blocked.append("network_outside_model_endpoint")
                raise PermissionError("Evaluation blocks network outside configured model endpoint")
        elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "pty.spawn"}:
            blocked.append("subprocess")
            raise PermissionError("Evaluation blocks subprocesses")
        elif event == "open":
            path, mode, flags = args
            if (flags & write_flags or (mode and any(x in mode for x in "wax+"))) and not writable(path):
                blocked.append("write_outside_scratch")
                raise PermissionError("Evaluation blocks writes outside scratch")
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod"}:
            if not writable(args[0]):
                blocked.append("filesystem_mutation_outside_scratch")
                raise PermissionError("Evaluation blocks filesystem mutation outside scratch")
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if not writable(args[0]) or not writable(args[1]):
                blocked.append("filesystem_move_outside_scratch")
                raise PermissionError("Evaluation blocks filesystem move outside scratch")
    sys.addaudithook(guard)
    return {"model_hostname": parsed.hostname, "port": port,
            "blocked_events": blocked, "allowed_network": "configured_model_endpoint_only"}


def prepare(destination: Path, fixture_path: Path, tool_schemas: list[dict]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, destination / "verify-hermes-context.py")
    shutil.copyfile(fixture_path, destination / "hermes_gt_context_cases.json")
    (destination / "tool-schemas.json").write_text(canonical(tool_schemas), encoding="utf-8")
    print(canonical({"prepared": str(destination), "fixture_sha256": digest(fixture_path.read_bytes()),
                     "schema_sha256": digest(tool_schemas), "model_calls": 0}))


def run_model(args: argparse.Namespace, fixture: dict, tool_schemas: list[dict]) -> int:
    if args.hermes_root:
        sys.path.insert(0, str(args.hermes_root.resolve()))
    from hermes_cli.config import load_config_readonly
    from hermes_cli.runtime_provider import resolve_runtime_provider
    cfg = load_config_readonly()
    model_cfg = cfg.get("model", {})
    model = model_cfg.get("default") if isinstance(model_cfg, dict) else model_cfg
    if not model:
        raise ValueError("Configured model is missing; refusing to invent a model")
    # Resolve credentials only in process through Hermes. Never serialize rt/cfg.
    with patch("hermes_cli.config.load_config", load_config_readonly):
        runtime = resolve_runtime_provider(target_model=model)
    required = {"provider", "api_mode", "base_url", "api_key"}
    if not required.issubset(runtime):
        raise ValueError("Runtime resolver contract is missing required fields")
    selected = {"model": model, "provider": runtime["provider"], "api_mode": runtime["api_mode"]}
    candidate_path = args.candidate.resolve()
    candidate_bytes = candidate_path.read_bytes()
    candidate_text = candidate_bytes.decode("utf-8")
    skill_name = candidate_path.parent.name
    cases = fixture["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
        if len(cases) != len(set(args.case)):
            raise ValueError("Unknown case selection")
    output = args.output_dir.resolve() / ("run-" + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    scratch = Path(tempfile.mkdtemp(prefix="hermes-gt-eval-")).resolve()
    # No canonical profile, plugin, memory, hook, or session directories are copied.
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(scratch)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["HERMES_SESSION_PLATFORM"] = "cli"
    sys.dont_write_bytecode = True
    os.chdir(scratch)
    for name in list(os.environ):
        if name.startswith("GREMLIN_TOWN_"):
            del os.environ[name]
    # Deliberately disable proxy environment so an unrelated proxy cannot widen egress.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    from hermes_constants import set_hermes_home_override
    set_hermes_home_override(str(scratch))
    guard_info = install_process_guard(runtime["base_url"], [scratch, output])
    logging.disable(logging.CRITICAL)
    secret = str(runtime["api_key"])
    def sanitized(value: Any) -> Any:
        raw = canonical(value)
        if secret:
            raw = raw.replace(secret, "[REDACTED_RUNTIME_SECRET]")
        return json.loads(raw)

    records = []
    schedule = ([("baseline", cases[0], 1)] if args.baseline else [])
    schedule += [("candidate", case, repeat)
                 for case in cases for repeat in range(1, args.repeats + 1)]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        import run_agent
        from agent.prompt_builder import build_skills_system_prompt as native_skill_index
        from tools.skills_tool import SKILL_VIEW_SCHEMA
        from tools.session_search_tool import SESSION_SEARCH_SCHEMA
        native_schemas = {"skill_view": SKILL_VIEW_SCHEMA, "session_search": SESSION_SEARCH_SCHEMA}
        tool_schemas = [{"type": "function", "function": deepcopy(native_schemas.get(
            item["function"]["name"], item["function"]))} for item in tool_schemas]
        for variant, case, repeat in schedule:
            run_id = f"{variant}-{case['id']}-{repeat}"
            run_home = scratch / run_id
            skills_dir = run_home / "skills"
            skills_dir.mkdir(parents=True)
            if variant == "candidate":
                skill_dir = skills_dir / skill_name
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_bytes(candidate_bytes)
            # Native index writes its cache only into this isolated directory.
            native_index = native_skill_index(available_tools={s["function"]["name"] for s in tool_schemas},
                                             available_toolsets={"gremlin_town", "skills", "session_search"},
                                             skills_dir_override=skills_dir)
            recorder = SyntheticTools(fixture, case, candidate_text, skill_name, tool_schemas,
                                      candidate=variant == "candidate")
            with ExitStack() as stack:
                stack.enter_context(patch.object(run_agent, "get_tool_definitions",
                                                 return_value=deepcopy(tool_schemas)))
                stack.enter_context(patch.object(run_agent, "build_skills_system_prompt",
                                                 return_value=native_index))
                stack.enter_context(patch("hermes_cli.plugins.discover_plugins", return_value=None))
                stack.enter_context(patch.object(run_agent, "handle_function_call",
                                                 side_effect=AssertionError("Native tool dispatch forbidden")))
                agent = run_agent.AIAgent(
                    model=model, provider=runtime["provider"], api_mode=runtime["api_mode"],
                    base_url=runtime["base_url"], api_key=runtime["api_key"],
                    max_iterations=args.max_turns, quiet_mode=True, verbose_logging=False,
                    enabled_toolsets=["gremlin_town", "skills", "session_search"],
                    skip_context_files=True, load_soul_identity=False, skip_memory=True,
                    skip_background_review=True, session_db=None, save_trajectories=False,
                    session_id="gt-eval-" + uuid.uuid4().hex, platform="cli",
                    run_budget_seconds=args.run_budget_seconds, fallback_model=None,
                    checkpoints_enabled=False)
                agent._persist_disabled = True
                agent._execute_tool_calls = MethodType(recorder.execute, agent)
                # No context-engine injected tools or refresh may expand the contract.
                agent.tools = deepcopy(tool_schemas)
                agent.valid_tool_names = {s["function"]["name"] for s in tool_schemas}
                agent._skip_mcp_refresh = True
                if set(agent.valid_tool_names) != recorder.allowed:
                    raise AssertionError("Evaluation tool allowlist changed")
                actual = {"model": agent.model, "provider": agent.provider, "api_mode": agent.api_mode}
                if actual != selected:
                    raise AssertionError("Runtime changed configured model/provider")
                result = agent.run_conversation(case["prompt"])
                checks = structural_checks(case, recorder, result.get("final_response", ""))
                if not result.get("completed") or result.get("failed") or result.get("error"):
                    checks["structural_pass"] = False
                    checks["failures"].append("model_run_incomplete")
                trace = sanitized({
                    "run_id": run_id, "variant": variant, "case_id": case["id"], "repeat": repeat,
                    "configured_runtime": selected, "actual_runtime": actual,
                    "candidate_sha256": digest(candidate_bytes), "fixture_sha256": digest(fixture),
                    "schema_sha256": digest(tool_schemas), "harness_sha256": digest(Path(__file__).read_bytes()),
                    "native_skill_index": native_index, "native_skill_index_sha256": digest(native_index),
                    "system_prompt": getattr(agent, "_cached_system_prompt", ""),
                    "prompt": case["prompt"], "calls": recorder.calls,
                    "persisted_inputs": recorder.persisted, "result": result, "checks": checks,
                    "execution_boundary": "all tools intercepted before native executor; no real tool handlers"})
                trace_path = output / (run_id + ".json")
                trace_path.write_text(canonical(trace), encoding="utf-8")
                records.append({"run_id": run_id, "trace": trace_path.name,
                                "trace_sha256": digest(trace_path.read_bytes()),
                                "structural_pass": checks["structural_pass"]})
                agent.close()
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "configured_runtime": selected, "source_profile": old_home,
        "candidate_source": str(candidate_path), "candidate_sha256": digest(candidate_bytes),
        "fixture_sha256": digest(fixture), "schema_sha256": digest(tool_schemas),
        "harness_sha256": digest(Path(__file__).read_bytes()), "records": records,
        "isolation": guard_info, "human_adjudication": "PENDING",
        "limits": ["Synthetic fixture tools and isolated prompt context, not full live-profile parity.",
                   "Runtime auth resolved through native provider helper; no secrets serialized.",
                   "Transport, persistence and request effects simulated, not live integration proofs.",
                   "Structural pass alone is not semantic acceptance. Read every trace against rubric."]}
    (output / "manifest.json").write_text(canonical(sanitized(manifest)), encoding="utf-8")
    print(canonical({"evidence": str(output), "runs": len(records),
                     "structural_passes": sum(r["structural_pass"] for r in records),
                     "human_adjudication": "PENDING"}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--schemas", type=Path)
    parser.add_argument("--prepare-dir", type=Path)
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("hermes-context-evidence"))
    parser.add_argument("--hermes-root", type=Path, default=Path("/opt/hermes"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--run-budget-seconds", type=int, default=240)
    parser.add_argument("--case", action="append")
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    fixture_path = args.fixtures or (HERE / "hermes_gt_context_cases.json"
                                    if (HERE / "hermes_gt_context_cases.json").exists() else DEFAULT_FIXTURES)
    fixture = read_json(fixture_path)
    tool_schemas = schemas(args.schemas)
    if args.prepare_dir:
        prepare(args.prepare_dir, fixture_path, tool_schemas)
        return 0
    if not args.run_model:
        print(canonical({"offline_validation": True, "cases": [c["id"] for c in fixture["cases"]],
                         "tool_count": len(tool_schemas), "model_calls": 0,
                         "fixture_sha256": digest(fixture), "schema_sha256": digest(tool_schemas)}))
        return 0
    if not args.candidate or args.repeats < 1:
        parser.error("--run-model requires --candidate and positive --repeats")
    return run_model(args, fixture, tool_schemas)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not serialize raw SDK exceptions: they may contain request credentials.
        print(canonical({"evaluation_failed": True, "exception_type": type(exc).__name__,
                         "detail": "No raw exception or credential data emitted."}), file=sys.stderr)
        raise SystemExit(1)
