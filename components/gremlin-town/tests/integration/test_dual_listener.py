from __future__ import annotations

import hashlib
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient

from gremlin_town.entrypoint import ListenerDispatch
from gremlin_town.runtime import ConfigurationError, build_runtime
from gremlin_town.transport import FakeTransport


OPERATIONS = [
    "participants.list",
    "requests.create",
    "requests.list",
    "requests.read",
    "requests.report",
    "requests.reply",
    "requests.cancel",
    "requests.check",
    "operations.get",
    "events.list",
]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _config(
    root: Path,
    *,
    private_host: str = "127.0.0.1",
    grok_enabled: bool = True,
) -> tuple[Path, str, str]:
    grok_token = "grok-dual-listener-token-with-more-than-thirty-two-bytes"
    operator_token = "operator-dual-listener-token-with-more-than-thirty-two-bytes"
    participants = root / "participants.toml"
    participants.write_text(
        "\n".join(
            [
                "[[participant]]",
                'id = "fleet-boss-grok"',
                'display_name = "Grok"',
                'address = "demo@example.invalid"',
                'role = "participant"',
                f"enabled = {str(grok_enabled).lower()}",
                f'token_sha256 = "{_digest(grok_token)}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
                "",
                "[[participant]]",
                'id = "operator-nic"',
                'display_name = "Operator"',
                'address = "demo@example.invalid"',
                'role = "operator"',
                "enabled = true",
                f'token_sha256 = "{_digest(operator_token)}"',
                'projects = ["demo"]',
                'sensitivities = ["public-test"]',
            ]
        ),
        encoding="utf-8",
    )
    config = root / "config.toml"
    operations = ", ".join(f'"{operation}"' for operation in OPERATIONS)
    config.write_text(
        "\n".join(
            [
                "[server]",
                f'host = "{private_host}"',
                "port = 8765",
                f'database = "{(root / "dual.sqlite3").as_posix()}"',
                "operator_only = false",
                "[public_gateway]",
                'host = "127.0.0.1"',
                "port = 8766",
                'actor_id = "fleet-boss-grok"',
                f"allowed_operations = [{operations}]",
                "[participants]",
                f'file = "{participants.as_posix()}"',
                "[recovery]",
                "normal_clients_allowed = true",
                "outbound_dispatch_allowed = false",
            ]
        ),
        encoding="utf-8",
    )
    return config, grok_token, operator_token


def _socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


def test_actual_private_and_public_sockets_are_isolated(tmp_path: Path) -> None:
    config, grok_token, operator_token = _config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    assert runtime.public_app is not None
    private_socket, public_socket = _socket(), _socket()
    private_port = int(private_socket.getsockname()[1])
    public_port = int(public_socket.getsockname()[1])
    dispatch = ListenerDispatch(
        runtime.app,
        runtime.public_app,
        private_port=private_port,
        public_port=public_port,
    )
    server = uvicorn.Server(
        uvicorn.Config(dispatch, log_level="critical", access_log=False, lifespan="on")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [private_socket, public_socket]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        private = f"http://127.0.0.1:{private_port}"
        public = f"http://127.0.0.1:{public_port}"
        assert httpx.get(f"{private}/health/live").status_code == 200
        assert httpx.get(f"{public}/health/live").status_code == 404
        assert httpx.get(f"{public}/health/capacity").status_code == 404
        assert httpx.get(f"{public}/login").status_code == 404
        assert httpx.get(f"{public}/api/docs").status_code == 404
        grok = httpx.get(
            f"{public}/v1/participants",
            headers={"Authorization": f"Bearer {grok_token}"},
        )
        assert grok.status_code == 200
        operator_public = httpx.get(
            f"{public}/v1/participants",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert operator_public.status_code == 401
        operator_private = httpx.get(
            f"{private}/v1/participants",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert operator_private.status_code == 200
        assert httpx.get(f"{private}/health/capacity").status_code == 401
        assert httpx.get(
            f"{private}/health/capacity",
            headers={"Authorization": f"Bearer {grok_token}"},
        ).status_code == 401
        capacity = httpx.get(
            f"{private}/health/capacity",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert capacity.status_code == 200
        snapshot = capacity.json()
        assert snapshot["usage"]["pending_operations"] == 0
        assert snapshot["usage"]["retained_payload_bytes"] == 0
        assert snapshot["warning_thresholds"]["pending_operations"] == 100
        assert snapshot["warning_thresholds"]["database_footprint_bytes"] == 512 * 1024 * 1024
        assert snapshot["warning_thresholds"]["filesystem_free_bytes"] == 2 * 1024 * 1024 * 1024
        assert isinstance(snapshot["warnings"], list)
        assert "provider_quota" not in str(snapshot)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        private_socket.close()
        public_socket.close()
    assert not thread.is_alive()


def test_disabled_public_actor_composes_but_all_public_credentials_are_denied(
    tmp_path: Path,
) -> None:
    config, grok_token, operator_token = _config(tmp_path, grok_enabled=False)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.public_app is not None
        assert runtime.public_gateway is not None
        assert runtime.public_gateway.actor_id == "fleet-boss-grok"
        assert runtime.token_resolver(grok_token) is None

        with TestClient(runtime.public_app) as public:
            for token in (grok_token, operator_token):
                response = public.get(
                    "/v1/participants",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 401

        with TestClient(runtime.app) as private:
            assert private.get(
                "/v1/participants",
                headers={"Authorization": f"Bearer {grok_token}"},
            ).status_code == 401
            assert private.get(
                "/v1/participants",
                headers={"Authorization": f"Bearer {operator_token}"},
            ).status_code == 200
    finally:
        runtime.close()


def test_runtime_refuses_non_loopback_listener(tmp_path: Path) -> None:
    config, _, _ = _config(tmp_path, private_host="0.0.0.0")
    try:
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    except ConfigurationError as exc:
        assert "loopback-only" in str(exc)
    else:
        raise AssertionError("non-loopback listener was accepted")


def test_runtime_rejects_duplicate_credentials_and_port_only_dispatch_collision(tmp_path: Path) -> None:
    duplicate_root = tmp_path / "duplicate-token"
    duplicate_root.mkdir()
    config, grok_token, _ = _config(duplicate_root)
    participants = duplicate_root / "participants.toml"
    participants.write_text(
        participants.read_text(encoding="utf-8").replace(
            _digest("operator-dual-listener-token-with-more-than-thirty-two-bytes"),
            _digest(grok_token),
        ),
        encoding="utf-8",
    )
    try:
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    except ConfigurationError as exc:
        assert "token hashes must be unique" in str(exc)
    else:
        raise AssertionError("duplicate participant credentials were accepted")

    collision_root = tmp_path / "same-port"
    collision_root.mkdir()
    config, _, _ = _config(collision_root)
    config.write_text(
        config.read_text(encoding="utf-8").replace("port = 8766", "port = 8765"),
        encoding="utf-8",
    )
    try:
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    except ConfigurationError as exc:
        assert "different ports" in str(exc)
    else:
        raise AssertionError("port-only listener dispatch collision was accepted")


def test_runtime_rejects_ignored_safety_and_scope_configuration(tmp_path: Path) -> None:
    config, _, _ = _config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[limits]\nstaged_body_days = 7\nevent_days = 400\n",
        encoding="utf-8",
    )
    try:
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    except ConfigurationError as exc:
        assert "unknown top-level configuration: limits" in str(exc)
    else:
        raise AssertionError("ignored retention configuration was accepted")

    second_root = tmp_path / "unknown-public"
    second_root.mkdir()
    config, _, _ = _config(second_root)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'actor_id = "fleet-boss-grok"',
            'actor_id = "fleet-boss-grok"\ndeny_health = true',
        ),
        encoding="utf-8",
    )
    try:
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    except ConfigurationError as exc:
        assert "unknown public_gateway configuration: deny_health" in str(exc)
    else:
        raise AssertionError("ignored public safety configuration was accepted")

    third_root = tmp_path / "unknown-participant"
    third_root.mkdir()
    config, _, _ = _config(third_root)
    participants = third_root / "participants.toml"
    participants.write_text(
        participants.read_text(encoding="utf-8").replace(
            'role = "participant"',
            'role = "participant"\nall_projects = true',
            1,
        ),
        encoding="utf-8",
    )
    try:
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    except ConfigurationError as exc:
        assert "unknown participant configuration: all_projects" in str(exc)
    else:
        raise AssertionError("ignored participant scope configuration was accepted")


def test_runtime_refuses_a_public_allowlist_without_check(tmp_path: Path) -> None:
    # A host config written before requests.check joined the public listener
    # (D19, ROA-13) stops the service at startup: the listener never serves a
    # route that its own config does not list.
    config, _, _ = _config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(' "requests.check",', ""),
        encoding="utf-8",
    )
    try:
        build_runtime(config, provider=FakeTransport(), start_worker=False)
    except ConfigurationError as exc:
        assert "exact ten-operation allowlist" in str(exc)
    else:
        raise AssertionError("a public allowlist without requests.check was accepted")
