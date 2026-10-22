"""Run a loopback-only Gremlin Town browser demo with no real credentials."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
from pathlib import Path

import uvicorn

from gremlin_town.contracts import Actor, CreateRequest, MessageKind, Packet, RequestMode
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport


DEMO_USERNAME = "operator"
DEMO_PASSWORD = "operator-local-demo-token-not-a-real-secret-2026"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_fixture(root: Path, port: int) -> Path:
    participants = root / "participants.toml"
    participants.write_text(
        "\n".join(
            [
                "[[participant]]",
                'id = "alice"',
                'display_name = "Alice"',
                'address = "demo@example.invalid"',
                'role = "participant"',
                "enabled = true",
                f'token_sha256 = "{_digest("alice-local-demo-token-not-a-real-secret-2026")}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[participant]]",
                'id = "bob"',
                'display_name = "Bob"',
                'address = "demo@example.invalid"',
                'role = "participant"',
                "enabled = true",
                f'token_sha256 = "{_digest("bob-local-demo-token-not-a-real-secret-2026")}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[participant]]",
                f'id = "{DEMO_USERNAME}"',
                'display_name = "Local demo operator"',
                'address = "demo@example.invalid"',
                'role = "operator"',
                "enabled = true",
                f'token_sha256 = "{_digest(DEMO_PASSWORD)}"',
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
                f"port = {port}",
                f'database = "{(root / "demo.sqlite3").as_posix()}"',
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
    return config


def _seed(runtime) -> None:
    packet = Packet(
        schema_version=1,
        kind=MessageKind.REQUEST,
        session_reference="local-browser-demo",
        objective="Review the local fake-provider Gremlin Town demo.",
        context="This request contains synthetic data and never leaves this computer.",
        constraints={"network": "loopback-only", "provider": "fake"},
        sources=(),
        next_action="Inspect the request and provenance views.",
        expected_reply="No reply is required for the demo.",
        sensitivity="public-test",
    )
    runtime.service.requests_create(
        Actor("alice"),
        CreateRequest(
            client_operation_id="local-browser-demo-create",
            mode=RequestMode.ACTION_REQUESTED,
            project="demo",
            recipient="bob",
            packet=packet,
        ),
    )
    runtime.worker.dispatch_once()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    root_parent = Path.cwd() / ".test-tmp"
    root_parent.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="browser-demo-", dir=root_parent))
    runtime = build_runtime(
        _write_fixture(root, args.port), provider=FakeTransport(), start_worker=False
    )
    _seed(runtime)
    print("Gremlin Town local browser demo (fake provider, synthetic credentials)", flush=True)
    print(f"URL: http://localhost:{args.port}/login", flush=True)
    print(f"Username: {DEMO_USERNAME}", flush=True)
    print(f"Password: {DEMO_PASSWORD}", flush=True)
    print(f"Ephemeral data: {root}", flush=True)
    uvicorn.run(
        runtime.app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
