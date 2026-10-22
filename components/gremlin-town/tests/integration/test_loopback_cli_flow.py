from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from gremlin_town.cli.main import main as cli_main
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_config(root: Path) -> tuple[Path, dict[str, str]]:
    tokens = {
        "alice": "alice-loopback-token-with-more-than-thirty-two-bytes",
        "bob": "bob-loopback-token-with-more-than-thirty-two-bytes-00",
        "operator": "operator-loopback-token-with-more-than-thirty-two-bytes",
    }
    participants = root / "participants.toml"
    participants.write_text(
        "\n".join(
            [
                "[[participant]]",
                'id = "alice"',
                'display_name = "Alice"',
                'address = "hub@example.test"',
                'role = "participant"',
                "enabled = true",
                f'token_sha256 = "{_hash(tokens["alice"])}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[participant]]",
                'id = "bob"',
                'display_name = "Bob"',
                'address = "hub@example.test"',
                'role = "participant"',
                "enabled = true",
                f'token_sha256 = "{_hash(tokens["bob"])}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[participant]]",
                'id = "operator"',
                'display_name = "Operator"',
                'address = "hub@example.test"',
                'role = "operator"',
                "enabled = true",
                f'token_sha256 = "{_hash(tokens["operator"])}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[route]]",
                'project = "demo"',
                'source = "alice"',
                'destination = "bob"',
                'sensitivity = "public-test"',
                "",
                "[[route]]",
                'project = "demo"',
                'source = "bob"',
                'destination = "alice"',
                'sensitivity = "public-test"',
            ]
        ),
        encoding="utf-8",
    )
    config = root / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[server]",
                'host = "127.0.0.1"',
                "port = 8765",
                f'database = "{(root / "gremlin.sqlite3").as_posix()}"',
                "operator_only = false",
                "[participants]",
                f'file = "{participants.as_posix()}"',
                "[recovery]",
                "normal_clients_allowed = true",
                "outbound_dispatch_allowed = false",
            ]
        ),
        encoding="utf-8",
    )
    return config, tokens


def _packet(objective: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "request",
        "session_reference": "fresh-loopback-session",
        "objective": objective,
        "context": "Use only this packet.",
        "constraints": {"scope": "bounded"},
        "sources": [],
        "next_action": "Return the result.",
        "expected_reply": "A concise result.",
        "sensitivity": "public-test",
    }


@contextmanager
def _loopback_server(app: Any) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="gremlin-town-loopback-test",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise RuntimeError("loopback Uvicorn server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("loopback Uvicorn server did not stop")


def _invoke(capsys: pytest.CaptureFixture[str], base_url: str, token: str, *args: str) -> tuple[int, dict[str, Any]]:
    code = cli_main(["--base-url", base_url, "--token", token, *args])
    output = capsys.readouterr().out.strip()
    return code, json.loads(output)


def test_actual_loopback_listener_cli_flow_and_failure_cases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, tokens = _write_config(tmp_path)
    provider = FakeTransport()
    runtime = build_runtime(config, provider=provider, start_worker=False)

    with _loopback_server(runtime.app) as base_url:
        code, denied = _invoke(
            capsys,
            base_url,
            "wrong-loopback-token-with-more-than-thirty-two-bytes",
            "participants.list",
        )
        assert code == 2
        assert denied["error"]["code"] == "UNAUTHORIZED"

        create_payload = {
            "client_operation_id": "loopback-create-1",
            "mode": "action_requested",
            "project": "demo",
            "recipient": "bob",
            "packet": _packet("Add 17 and 25."),
        }
        create_json = json.dumps(create_payload, separators=(",", ":"))
        code, created = _invoke(
            capsys, base_url, tokens["alice"], "requests.create", "--json", create_json
        )
        assert code == 0
        assert created["operation_state"] == "queued"
        request_id = created["request_id"]

        code, duplicate = _invoke(
            capsys, base_url, tokens["alice"], "requests.create", "--json", create_json
        )
        assert code == 0
        assert duplicate["operation_id"] == created["operation_id"]

        changed = dict(create_payload)
        changed["packet"] = _packet("Different work under the same key.")
        code, conflict = _invoke(
            capsys,
            base_url,
            tokens["alice"],
            "requests.create",
            "--json",
            json.dumps(changed, separators=(",", ":")),
        )
        assert code == 2
        assert conflict["error"]["code"] == "OPERATION_INPUT_CONFLICT"

        sent = runtime.worker.dispatch_once()
        assert sent is not None and sent.operation_state.value == "accepted"

        code, read = _invoke(
            capsys, base_url, tokens["bob"], "requests.read", request_id
        )
        assert code == 0
        assert read["content_availability"] == "complete"

        start_json = json.dumps(
            {
                "client_operation_id": "loopback-start-1",
                "expected_revision": read["revision"],
                "action": "start",
            },
            separators=(",", ":"),
        )
        code, started = _invoke(
            capsys,
            base_url,
            tokens["bob"],
            "requests.report",
            request_id,
            "--json",
            start_json,
        )
        assert code == 0
        assert started["work_state"] == "in_progress"

        stale_json = json.dumps(
            {
                "client_operation_id": "loopback-stale-1",
                "expected_revision": read["revision"],
                "action": "acknowledge",
            },
            separators=(",", ":"),
        )
        code, stale = _invoke(
            capsys,
            base_url,
            tokens["bob"],
            "requests.report",
            request_id,
            "--json",
            stale_json,
        )
        assert code == 2
        assert stale["error"]["code"] == "REVISION_CONFLICT"

        result_packet = _packet("42")
        result_packet["kind"] = "result"
        reply_json = json.dumps(
            {
                "client_operation_id": "loopback-result-1",
                "expected_revision": started["revision"],
                "kind": "result",
                "packet": result_packet,
            },
            separators=(",", ":"),
        )
        code, reply = _invoke(
            capsys,
            base_url,
            tokens["bob"],
            "requests.reply",
            request_id,
            "--json",
            reply_json,
        )
        assert code == 0
        assert reply["operation_state"] == "queued"
        returned = runtime.worker.dispatch_once()
        assert returned is not None and returned.work_state.value == "result_returned"

        code, final = _invoke(
            capsys, base_url, tokens["alice"], "requests.read", request_id
        )
        assert code == 0
        assert final["work_state"] == "result_returned"
        assert len(final["content"]) == 2

        code, operation = _invoke(
            capsys,
            base_url,
            tokens["alice"],
            "operations.get",
            reply["operation_id"],
        )
        assert code == 0
        assert operation["operation_state"] == "accepted"

        code, events = _invoke(
            capsys,
            base_url,
            tokens["alice"],
            "events.list",
            "--request-id",
            request_id,
        )
        assert code == 0
        assert any(item["event_type"] == "result_returned" for item in events["items"])
