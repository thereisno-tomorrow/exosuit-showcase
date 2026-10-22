"""CLI entry point with one JSON output shape for every operation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .client import CliError, JsonApiClient

BRAIN_OPERATIONS = (
    "context.retrieve", "memory.save", "memory.change", "memory.read", "memory.search",
    "memory.list", "decision.record", "continuity.start", "continuity.append",
    "continuity.recover", "continuity.list", "continuity.close",
)
BRAIN_WRITES = frozenset({
    "memory.save", "memory.change", "decision.record", "continuity.start", "continuity.append", "continuity.close",
})
_CONFIRMED_WRITE_OPERATIONS = frozenset({"memory.save", "memory.change"})
# Hosted Brain writes round-trip through GitHub and can exceed the default timeout.
BRAIN_WRITE_TIMEOUT = 60.0
# Proposed — Nic to confirm: one overall deadline for a write, instead of one
# timeout per attempt, so the caller never waits indefinitely for a confirmed answer.
BRAIN_WRITE_DEADLINE = 120.0
# Proposed — Nic to confirm: identical body resent up to this many times total.
BRAIN_WRITE_MAX_ATTEMPTS = 3
BRAIN_RETRY_DELAY = 2.0
_CONFIRMED_SAVE_STATUSES = frozenset({"accepted", "duplicate"})


def _confirmed_memory_save(payload: Mapping[str, Any], result: Any) -> bool:
    """"Saved" means status accepted/duplicate, remote_observed true, and proof present."""

    return (
        isinstance(result, dict)
        and result.get("status") in _CONFIRMED_SAVE_STATUSES
        and result.get("remote_observed") is True
        and result.get("operation_id") == payload.get("operation_id")
        and bool(result.get("record_id"))
        and bool(result.get("version"))
    )


def _save_unconfirmed_error() -> CliError:
    return CliError(
        f"Not saved: no confirmed answer within {int(BRAIN_WRITE_DEADLINE)} s. Treat this as "
        "not saved. Running the same command again is safe; it returns the real result.",
        code="SAVE_UNCONFIRMED",
        status_code=2,
    )


def _json_payload(value: str) -> dict[str, Any]:
    if value == "-":
        value = sys.stdin.read()
    elif value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("JSON payload must be an object")
    return result


def _add_json(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--json", required=True, type=_json_payload, help="JSON object, @file, or - for stdin")


def _default_config_path() -> Path:
    return Path.home() / ".config" / "gremlin-town" / "local-codex.toml"


def _valid_config_base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise CliError("configured base URL must be an HTTPS origin", code="CONFIG_ERROR", status_code=2)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CliError("configured base URL must be an HTTPS origin", code="CONFIG_ERROR", status_code=2) from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.hostname is None
        or port is None and ":" in parsed.netloc.rsplit("]", 1)[-1]
    ):
        raise CliError("configured base URL must be an HTTPS origin", code="CONFIG_ERROR", status_code=2)
    return value.rstrip("/")


def _load_config(raw_path: str | None) -> dict[str, Any]:
    explicit = raw_path is not None
    path = Path(raw_path) if explicit else _default_config_path()
    try:
        if not path.exists():
            if explicit:
                raise CliError("configured CLI file is unavailable", code="CONFIG_ERROR", status_code=2)
            return {}
        content = path.read_text(encoding="utf-8")
        config = tomllib.loads(content)
    except CliError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CliError("configured CLI file is invalid or unreadable", code="CONFIG_ERROR", status_code=2) from exc
    if not isinstance(config, dict) or set(config) - {"base_url", "token_file"}:
        raise CliError("configured CLI file contains an unknown key", code="CONFIG_ERROR", status_code=2)
    if "base_url" in config:
        config["base_url"] = _valid_config_base_url(config["base_url"])
    if "token_file" in config:
        token_file = config["token_file"]
        if not isinstance(token_file, str) or not Path(token_file).is_absolute():
            raise CliError("configured token file must be an absolute path", code="CONFIG_ERROR", status_code=2)
        config["token_file"] = token_file
    return config


def _read_token_file(path: str) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CliError("configured token file is unavailable", code="CONFIG_ERROR", status_code=2) from exc
    if not token:
        raise CliError("configured token file is empty", code="CONFIG_ERROR", status_code=2)
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gremlin-town", description="JSON-first Gremlin Town client")
    parser.add_argument("--base-url", default=None, help="hub base URL")
    parser.add_argument("--config", default=None, help="strict TOML client config path")
    parser.add_argument("--token", default=None, help="bearer token (or GREMLIN_TOWN_TOKEN)")
    parser.add_argument("--timeout", type=float, default=10.0)
    commands = parser.add_subparsers(dest="command", required=True)

    lifecycle = commands.add_parser("lifecycle", help="Private operator lifecycle controls")
    lifecycle_sub = lifecycle.add_subparsers(dest="lifecycle_action", required=True)
    for action in ("status", "reconcile", "refresh"):
        lifecycle_sub.add_parser(action).set_defaults(operation="lifecycle." + action)

    brain = commands.add_parser("brain", help="Shared Brain using this client's authenticated identity")
    brain_sub = brain.add_subparsers(dest="brain_operation", required=True)
    for operation in BRAIN_OPERATIONS:
        command = brain_sub.add_parser(operation, help="Use exact bc_* arguments; retain operation_id and input for retries")
        command.set_defaults(operation="brain." + operation)
        _add_json(command)
        if operation == "decision.record":
            command.add_argument("--verify", action="store_true", help="Independently read canonical decision before returning a durability receipt")

    participants = commands.add_parser("participants")
    participants.set_defaults(operation="participants.list")
    participants_sub = participants.add_subparsers(dest="action", required=True)
    participants_list = participants_sub.add_parser("list")
    participants_list.add_argument("--project")

    requests = commands.add_parser("requests")
    requests_sub = requests.add_subparsers(dest="action", required=True)
    requests_create = requests_sub.add_parser("create")
    requests_create.set_defaults(operation="requests.create")
    _add_json(requests_create)
    requests_list = requests_sub.add_parser("list")
    requests_list.set_defaults(operation="requests.list")
    requests_list.add_argument("--project")
    requests_list.add_argument("--recipient")
    requests_list.add_argument("--work-state")
    requests_list.add_argument("--attention", action=argparse.BooleanOptionalAction, default=None)
    requests_list.add_argument("--open", action=argparse.BooleanOptionalAction, default=None)
    requests_list.add_argument("--limit", type=int)
    requests_list.add_argument("--cursor")
    requests_read = requests_sub.add_parser("read")
    requests_read.set_defaults(operation="requests.read")
    requests_read.add_argument("request_id")
    requests_read.add_argument("--message-cursor")
    for action, operation in (
        ("report", "requests.report"), ("reply", "requests.reply"),
        ("cancel", "requests.cancel"), ("check", "requests.check"),
    ):
        command = requests_sub.add_parser(action)
        command.set_defaults(operation=operation)
        command.add_argument("request_id")
        _add_json(command)
    recovery = requests_sub.add_parser("recovery-dispose")
    recovery.set_defaults(operation="recovery_dispose")
    recovery.add_argument("request_id")
    _add_json(recovery)

    operations = commands.add_parser("operations")
    operations_sub = operations.add_subparsers(dest="action", required=True)
    operations_get = operations_sub.add_parser("get")
    operations_get.set_defaults(operation="operations.get")
    operations_get.add_argument("operation_id")

    moments = commands.add_parser("moments")
    moments_sub = moments.add_subparsers(dest="action", required=True)
    moments_signal = moments_sub.add_parser("signal")
    moments_signal.set_defaults(operation="moments.signal")
    moments_signal.add_argument("kind", choices=("pc_on", "day_settled", "day_change"))
    moments_signal.add_argument("--detail")

    review = commands.add_parser("review")
    review_sub = review.add_subparsers(dest="action", required=True)
    review_respond = review_sub.add_parser("respond")
    review_respond.set_defaults(operation="review.respond")
    review_respond.add_argument("--item-ref", required=True)
    review_respond.add_argument(
        "--response", required=True,
        choices=("picked_today", "accepted", "not_now", "parked", "dropped", "corrected", "no_answer"),
    )
    review_respond.add_argument("--response-words")

    events = commands.add_parser("events")
    events_sub = events.add_subparsers(dest="action", required=True)
    events_list = events_sub.add_parser("list")
    events_list.set_defaults(operation="events.list")
    events_list.add_argument("--request-id")
    events_list.add_argument("--event-type")
    events_list.add_argument("--limit", type=int)
    events_list.add_argument("--cursor")
    return parser


def _normalize_dotted(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    aliases = {
        "participants.list": ["participants", "list"],
        "requests.create": ["requests", "create"],
        "requests.list": ["requests", "list"],
        "requests.read": ["requests", "read"],
        "requests.report": ["requests", "report"],
        "requests.reply": ["requests", "reply"],
        "requests.cancel": ["requests", "cancel"],
        "requests.check": ["requests", "check"],
        "recovery_dispose": ["requests", "recovery-dispose"],
        "operations.get": ["operations", "get"],
        "events.list": ["events", "list"],
    }
    prefix = [item for item in argv if item.startswith("-")]
    # Global options may appear before the operation; leave argparse to parse
    # them while replacing only the operation token itself.
    for index, token in enumerate(argv):
        if token in aliases:
            return argv[:index] + aliases[token] + argv[index + 1 :]
    return argv


def _operation_request(client: JsonApiClient, args: argparse.Namespace) -> Any:
    operation = args.operation
    if operation.startswith("lifecycle."):
        paths = {"lifecycle.status": ("GET", "/v1/operator/grok-lifecycle"),
                 "lifecycle.reconcile": ("POST", "/v1/operator/grok-lifecycle/reconcile"),
                 "lifecycle.refresh": ("POST", "/v1/operator/participants/refresh")}
        method, path = paths[operation]
        return client.request(method, path)
    if operation.startswith("brain."):
        result = _brain_request(client, args.brain_operation, args.json)
        if args.brain_operation == "decision.record" and args.verify:
            return _verify_hosted_decision(client, args.json, result)
        return result
    if operation == "participants.list":
        return client.request("GET", "/v1/participants", query={"project": args.project})
    if operation == "requests.create":
        return client.request("POST", "/v1/requests", payload=args.json)
    if operation == "requests.list":
        return client.request("GET", "/v1/requests", query={"project": args.project, "recipient": args.recipient, "work_state": args.work_state, "attention": args.attention, "open": args.open, "limit": args.limit, "cursor": args.cursor})
    if operation == "requests.read":
        return client.request("GET", f"/v1/requests/{args.request_id}", query={"message_cursor": args.message_cursor})
    if operation in {"requests.report", "requests.reply", "requests.cancel", "requests.check"}:
        action = operation.rsplit(".", 1)[1]
        route_action = {"report": "reports", "reply": "replies", "cancel": "cancel", "check": "check"}[action]
        return client.request("POST", f"/v1/requests/{args.request_id}/{route_action}", payload=args.json)
    if operation == "recovery_dispose":
        return client.request("POST", f"/v1/requests/{args.request_id}/recovery-disposition", payload=args.json)
    if operation == "operations.get":
        return client.request("GET", f"/v1/operations/{args.operation_id}")
    if operation == "events.list":
        return client.request("GET", "/v1/events", query={"request_id": args.request_id, "event_type": args.event_type, "limit": args.limit, "cursor": args.cursor})
    if operation == "moments.signal":
        return client.request("POST", "/v1/moments/signals", payload={"kind": args.kind, "detail": args.detail})
    if operation == "review.respond":
        payload: dict[str, Any] = {"item_ref": args.item_ref, "response": args.response}
        if args.response_words is not None:
            payload["response_words"] = args.response_words
        return client.request("POST", "/v1/review/responses", payload=payload)
    raise CliError(f"unsupported operation: {operation}")


def _brain_request(client: JsonApiClient, operation: str, payload: Any) -> Any:
    """Send one Brain call. A write resends the identical body until a confirmed answer or the deadline."""
    if operation not in BRAIN_WRITES:
        return client.request("POST", f"/v1/brain/{operation}", payload=payload)

    timeout = max(client.timeout, BRAIN_WRITE_TIMEOUT)
    deadline = time.monotonic() + BRAIN_WRITE_DEADLINE
    last_error: CliError | None = None
    all_connect_errors = True
    for attempt in range(BRAIN_WRITE_MAX_ATTEMPTS):
        try:
            result = client.request("POST", f"/v1/brain/{operation}", payload=payload, timeout=timeout)
        except CliError as exc:
            retryable = exc.code == "NETWORK_ERROR" or exc.retryable
            if not retryable:
                raise  # A server refusal or conflict is final; never retry it.
            all_connect_errors = all_connect_errors and isinstance(exc.__cause__, httpx.ConnectError)
            last_error = exc
            if time.monotonic() >= deadline or attempt == BRAIN_WRITE_MAX_ATTEMPTS - 1:
                break
            sys.stderr.write(f"brain {operation}: {exc}; retrying with identical input\n")
            time.sleep(BRAIN_RETRY_DELAY * (attempt + 1))
            continue
        if operation in _CONFIRMED_WRITE_OPERATIONS and not _confirmed_memory_save(payload, result):
            raise _save_unconfirmed_error()
        return result

    if all_connect_errors:
        raise CliError(
            f"Not saved: Gremlin Town at {client.base_url} could not be reached.",
            code="HUB_UNREACHABLE", status_code=2,
        ) from last_error
    if last_error is not None and last_error.code != "NETWORK_ERROR":
        raise last_error
    raise _save_unconfirmed_error() from last_error


def _verify_hosted_decision(client: JsonApiClient, payload: dict, result: Any) -> dict:
    """One hosted writer, then a separate canonical read. No local fallback."""
    def refused():
        raise CliError("hosted canonical receipt verification failed; retain exact input for reconciliation", code="RECEIPT_INVALID", status_code=2)
    if not isinstance(result, dict):
        refused()
    receipt = result.get("durability_receipt")
    transport = result.get("transport_evidence")
    if (result.get("status") not in {"accepted", "duplicate"}
        or result.get("remote_observed") is not True
        or result.get("owner_locator") != "vault://brain-home"
        or result.get("operation_id") != payload.get("operation_id")
        or not isinstance(receipt, dict) or not isinstance(transport, dict)):
        refused()
    if (receipt.get("schema") != "brain.durability-receipt/v1"
        or receipt.get("writer_id") != "brain-core-shared-durability-writer/v1"
        or receipt.get("status") not in {"created", "duplicate/no-op"}
        or receipt.get("owner_locator") != "vault://brain-home"
        or receipt.get("origin_id") != payload.get("operation_id")
        or receipt.get("owner_id") != "brain-home"
        or receipt.get("canonical_identity") != result.get("record_id")
        or receipt.get("refusal_code") is not None
        or receipt.get("writer_generation") != 1
        or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("canonical_sha256", "")))
        or receipt.get("model_calls") != 0
        or receipt.get("canonical_sha256") != result.get("content_sha256")
        or transport.get("storage_transport") != "github"
        or transport.get("remote_io") is not True
        or transport.get("canonical_readback") is not True):
        refused()
    from dataclasses import fields
    from brain_core.durability import DurabilityReceipt
    if set(receipt) != {field.name for field in fields(DurabilityReceipt)}:
        refused()
    readback = client.request("POST", "/v1/brain/memory.read", payload={
        "record_id": result.get("record_id"), "scope": payload.get("scope")})
    if (not isinstance(readback, dict)
        or readback.get("record_id") != result.get("record_id")
        or readback.get("owner_locator") != "vault://brain-home"
        or readback.get("kind") != "decision"
        or readback.get("content_sha256") != receipt.get("canonical_sha256")
        or readback.get("freshness") != "current"
        or not isinstance(readback.get("canonical_record"), dict)):
        refused()
    from brain_core.records import record_from_mapping, render_record
    from brain_core.errors import BrainError
    try:
        canonical = record_from_mapping(readback["canonical_record"], "hosted canonical readback")
    except (BrainError, ValueError, TypeError):
        refused()
    if (canonical.id != result["record_id"] or canonical.kind != "decision"
        or canonical.state != "accepted" or canonical.assertion != "explicit"
        or canonical.owners != ("brain-home",) or canonical.vaults != ("vault://brain-home",)
        or canonical.dedupe_key != receipt.get("semantic_key")
        or not canonical.evidence or canonical.evidence[0].actor != "nic"
        or hashlib.sha256(render_record(canonical).encode("utf-8")).hexdigest() != receipt["canonical_sha256"]):
        refused()
    if receipt["status"] == "created":
        if canonical.data.get("origin_id") != payload["operation_id"] or len(canonical.evidence) < 2:
            refused()
        submitting = canonical.evidence[1]
        if not submitting.actor or submitting.actor == "nic":
            refused()
        if canonical.title != str(payload.get("title", "")).strip() or canonical.evidence[0].excerpt != str(payload.get("choice", "")).strip():
            refused()
        for wire, stored in (("because", "because"), ("forecloses", "forecloses"), ("governing_scope", "governs"), ("encoded_in", "encoded_in"), ("completeness_check", "completeness_check")):
            if canonical.data.get(stored) != str(payload.get(wire, "")).strip():
                refused()
    return {"result": "ok", "data": receipt,
        "transport_evidence": transport,
        "verification": {"independent_canonical_readback": True,
            "content_sha256": readback["content_sha256"],
            "version": readback.get("version"), "client_http_calls": 2}}


def main(argv: Sequence[str] | None = None, *, client: JsonApiClient | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    try:
        args = parser.parse_args(_normalize_dotted(raw_argv))
        if client is None:
            config = _load_config(args.config)
            base_url = args.base_url if args.base_url is not None else config.get("base_url", "http://127.0.0.1:8000")
            if not isinstance(base_url, str) or not base_url:
                raise CliError("base URL is unavailable", code="CONFIG_ERROR")
            if args.token is not None:
                token = args.token
            elif "GREMLIN_TOWN_TOKEN" in os.environ:
                token = os.environ["GREMLIN_TOWN_TOKEN"]
            elif config.get("token_file") is not None:
                token = _read_token_file(config["token_file"])
            else:
                token = None
            if token is not None:
                token = token.strip()
            if not token:
                raise CliError("a bearer token is required", code="UNAUTHORIZED")
            api_client = JsonApiClient(base_url, token, timeout=args.timeout)
        else:
            api_client = client
        result = _operation_request(api_client, args)
        # JSON escapes preserve Unicode across Windows legacy-encoded pipes.
        sys.stdout.write(json.dumps(result, ensure_ascii=True, sort_keys=True) + "\n")
        return 0
    except (CliError, argparse.ArgumentError, argparse.ArgumentTypeError) as exc:
        error = {"error": {"code": getattr(exc, "code", "INVALID_INPUT"), "message": str(exc)}}
        sys.stdout.write(json.dumps(error, ensure_ascii=True, sort_keys=True) + "\n")
        return getattr(exc, "status_code", 2)
    except SystemExit as exc:
        # argparse owns help/usage output and its exit code.
        return int(exc.code) if isinstance(exc.code, int) else 2
