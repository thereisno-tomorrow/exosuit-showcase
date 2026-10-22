from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent))
from brain_local_harness import (  # noqa: E402
    BRAIN_CORE_SRC,
    CONTEXT_ONLY_TOKEN,
    DENIED_CANARY,
    READER_TOKEN,
    SESSION_ID,
    WRITER_TOKEN,
    build_fixture,
    create_escape_reparse,
    raw_request_without_body,
    remove_escape_reparse,
    sha256_text,
    write_text_exact,
    write_json,
)

from gremlin_town.mcp.local_project_connector import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    ConnectorError,
    ConnectorUnavailable,
    LocalConnectorContextProvider,
    LocalConnectorContinuityProvider,
    LocalProjectConnectorClient,
    LocalProjectContextCapability,
    LocalProjectConnectorRuntime,
)
from gremlin_town.mcp.brain_runtime import (  # noqa: E402
    BrainOperation,
    BrainRuntimeError,
    ContextQuery,
    ContinuityRecoverRequest,
    OwnerAuthorization,
    OwnerSelector,
)


def test_authentication_and_scope_are_checked_before_http_body_read(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        assert running.runtime.http_body_reads == 0

        unauthenticated = raw_request_without_body(
            running.base_url, "/v1/context/read", "wrong-token"
        )
        assert b" 401 " in unauthenticated
        assert b"UNAUTHENTICATED" in unauthenticated
        assert running.runtime.http_body_reads == 0

        wrong_scope = raw_request_without_body(
            running.base_url, "/v1/files/write", CONTEXT_ONLY_TOKEN
        )
        assert b" 403 " in wrong_scope
        assert b"ACCESS_DENIED" in wrong_scope
        assert running.runtime.http_body_reads == 0


def test_roots_grants_traversal_and_reparse_are_host_controlled(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    link = create_escape_reparse(fixture)
    try:
        with fixture.running() as running:
            client = fixture.client(running.base_url)

            with pytest.raises(ConnectorError) as extra:
                client.call(
                    "/v1/context/read",
                    {
                        "project_id": fixture.git_project_id,
                        "relative_path": "tracked.md",
                        "session_id": SESSION_ID,
                        "root": fixture.outside_root.as_posix(),
                    },
                )
            assert extra.value.code == "INVALID_REQUEST"
            assert "root" in extra.value.details["unknown"]

            reads_before = running.runtime.source_body_reads
            with pytest.raises(ConnectorError) as denied:
                client.read_context(fixture.git_project_id, "denied.md", SESSION_ID)
            assert denied.value.code == "brain.grant.wrong_path"
            assert DENIED_CANARY not in json.dumps(denied.value.as_dict())
            assert running.runtime.source_body_reads == reads_before

            for locator, expected in (
                ("../outside-enrollment/secret.md", "brain.path.traversal"),
                (fixture.outside_root.joinpath("secret.md").as_posix(), "brain.path.absolute"),
            ):
                with pytest.raises(ConnectorError) as refused:
                    client.read_context(fixture.git_project_id, locator, SESSION_ID)
                assert refused.value.code == expected
                assert running.runtime.source_body_reads == reads_before

            with pytest.raises(ConnectorError) as reparse:
                client.read_context(fixture.git_project_id, "escape/secret.md", SESSION_ID)
            assert reparse.value.code == "REPARSE_REFUSED"
            assert "outside-root secret body" not in json.dumps(reparse.value.as_dict())
            assert running.runtime.source_body_reads == reads_before
    finally:
        remove_escape_reparse(link)


def test_current_uncommitted_untracked_and_non_git_reads_then_explicit_offline(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    status = fixture.git_status()
    assert any(line.endswith("tracked.md") and line.lstrip().startswith("M") for line in status)
    assert any(line.endswith("local-only.md") and line.startswith("??") for line in status)
    assert not (fixture.non_git_project / ".git").exists()

    running = fixture.running().start()
    client = fixture.client(running.base_url)
    try:
        tracked = client.read_context(fixture.git_project_id, "tracked.md", SESSION_ID)
        untracked = client.read_context(fixture.git_project_id, "local-only.md", SESSION_ID)
        nongit = client.read_context(fixture.non_git_project_id, "current.md", SESSION_ID)
        assert tracked["text"] == "Uncommitted telemetry says 42 probes.\n"
        assert tracked["content_sha256"] == sha256_text(tracked["text"])
        assert tracked["freshness"] == "current_pc"
        assert untracked["text"].startswith("Local-only note")
        assert untracked["access_basis"] not in {"export", "search"}
        assert nongit["text"] == "Non-Git local context is current.\n"

        changed = "Edited again while connector is online.\n"
        write_text_exact(fixture.git_project / "tracked.md", changed)
        reread = client.read_context(fixture.git_project_id, "tracked.md", SESSION_ID)
        assert reread["text"] == changed
        assert reread["content_sha256"] == sha256_text(changed)
        assert reread["content_sha256"] != tracked["content_sha256"]
    finally:
        running.stop()

    with pytest.raises(ConnectorUnavailable) as offline:
        client.read_context(fixture.git_project_id, "tracked.md", SESSION_ID)
    assert offline.value.code == "PC_UNAVAILABLE"


def test_authorized_compare_before_write_and_read_after_write(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        reader = fixture.client(running.base_url, READER_TOKEN)
        writer = fixture.client(running.base_url, WRITER_TOKEN)
        before = reader.read_context(fixture.git_project_id, "tracked.md", SESSION_ID)

        with pytest.raises(ConnectorError) as scope_denied:
            reader.write_file(
                fixture.git_project_id,
                "tracked.md",
                SESSION_ID,
                before["content_sha256"],
                "must not be written\n",
            )
        assert scope_denied.value.code == "ACCESS_DENIED"
        assert (fixture.git_project / "tracked.md").read_text(encoding="utf-8") == before["text"]

        replacement = "Authorized compare-before-write content.\n"
        written = writer.write_file(
            fixture.git_project_id,
            "tracked.md",
            SESSION_ID,
            before["content_sha256"],
            replacement,
        )
        assert written["before_sha256"] == before["content_sha256"]
        assert written["content_sha256"] == sha256_text(replacement)
        assert written["changed"] is True
        observed = reader.read_context(fixture.git_project_id, "tracked.md", SESSION_ID)
        assert observed["text"] == replacement
        assert observed["content_sha256"] == written["content_sha256"]

        with pytest.raises(ConnectorError) as stale:
            writer.write_file(
                fixture.git_project_id,
                "tracked.md",
                SESSION_ID,
                before["content_sha256"],
                "stale overwrite\n",
            )
        assert stale.value.code == "VERSION_CONFLICT"
        assert stale.value.details["current_sha256"] == written["content_sha256"]
        assert (fixture.git_project / "tracked.md").read_text(encoding="utf-8") == replacement


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1:8080",
        "http://100.64.0.10:8080",
        "http://0.0.0.0:8080",
        "http://localhost:8080",
        "https://user:secret@example.invalid",
        "https://example.invalid/path",
    ],
)
def test_client_rejects_unsafe_or_non_origin_urls(url: str) -> None:
    with pytest.raises(ValueError):
        LocalProjectConnectorClient(url, "token")


def test_client_requires_finite_positive_timeout_and_https_off_loopback() -> None:
    with pytest.raises(ValueError):
        LocalProjectConnectorClient("http://127.0.0.1:8000", "token", timeout=float("inf"))
    with pytest.raises(ValueError):
        LocalProjectConnectorClient("http://127.0.0.1:8000", "token", timeout=0)
    assert LocalProjectConnectorClient("https://10.0.0.5:8443", "token").base_url == "https://10.0.0.5:8443"


def test_bind_requires_loopback_or_explicit_encrypted_overlay(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    config = json.loads(fixture.config_path.read_text(encoding="utf-8"))
    config["bind_host"] = "0.0.0.0"
    write_json(fixture.config_path, config)
    with pytest.raises(ConnectorError, match="wildcard"):
        LocalProjectConnectorRuntime(fixture.config_path, brain_src=BRAIN_CORE_SRC)

    config["bind_host"] = "10.20.30.40"
    write_json(fixture.config_path, config)
    with pytest.raises(ConnectorError, match="encrypted overlay"):
        LocalProjectConnectorRuntime(fixture.config_path, brain_src=BRAIN_CORE_SRC)

    config["encrypted_overlay_networks"] = ["10.20.30.0/24"]
    write_json(fixture.config_path, config)
    assert LocalProjectConnectorRuntime(fixture.config_path, brain_src=BRAIN_CORE_SRC).bind_host == "10.20.30.40"

    config["bind_host"] = "100.64.10.5"
    config["encrypted_overlay_networks"] = ["100.64.10.0/24"]
    write_json(fixture.config_path, config)
    assert LocalProjectConnectorRuntime(fixture.config_path, brain_src=BRAIN_CORE_SRC).bind_host == "100.64.10.5"

    config["bind_host"] = "8.8.8.8"
    config["encrypted_overlay_networks"] = ["8.8.8.8/32"]
    write_json(fixture.config_path, config)
    with pytest.raises(ConnectorError, match="encrypted overlay"):
        LocalProjectConnectorRuntime(fixture.config_path, brain_src=BRAIN_CORE_SRC)


def test_bearer_client_refuses_redirect_and_caps_success_response(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        redirected_hits = 0

        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/capture":
                type(self).redirected_hits += 1
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            elif self.path == "/oversized":
                self.send_response(200)
                self.send_header("Content-Length", str(MAX_RESPONSE_BYTES + 1))
                self.end_headers()
            elif self.path in {
                "/serve-gateway-offline",
                "/v1/context/retrieve",
                "/v1/continuity/recover",
            }:
                body = b"Bad Gateway"
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/unauthenticated":
                body = json.dumps({
                    "ok": False,
                    "error": {"code": "UNAUTHENTICATED", "message": "authentication required"},
                }).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/forbidden":
                body = json.dumps({
                    "ok": False,
                    "error": {"code": "ACCESS_DENIED", "message": "scope denied"},
                }).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/malformed-success":
                body = b"not json"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(400)
                self.send_header("Content-Length", str(MAX_RESPONSE_BYTES + 1))
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    client = LocalProjectConnectorClient(base_url, "sensitive-test-token")
    try:
        with pytest.raises(ConnectorError) as redirect:
            client.call("/redirect", {})
        assert redirect.value.code == "REDIRECT_REFUSED"
        assert Handler.redirected_hits == 0

        with pytest.raises(ConnectorError) as oversized:
            client.call("/oversized", {})
        assert oversized.value.code == "RESPONSE_TOO_LARGE"

        with pytest.raises(ConnectorError) as oversized_error:
            client.call("/oversized-error", {})
        assert oversized_error.value.code == "RESPONSE_TOO_LARGE"

        with pytest.raises(ConnectorUnavailable) as gateway:
            client.call("/serve-gateway-offline", {})
        assert gateway.value.code == "PC_UNAVAILABLE"

        capability = LocalProjectContextCapability("vault_demo", "session-1")
        selector = OwnerSelector("project", "vault_demo")
        context_authorization = OwnerAuthorization(
            BrainOperation.CONTEXT_RETRIEVE,
            "read",
            selector,
            "vault_demo",
            "vault://vault_demo",
            "test-policy",
            capability,
        )
        with pytest.raises(BrainRuntimeError) as context_unavailable:
            asyncio.run(LocalConnectorContextProvider(client).retrieve(
                context_authorization,
                ContextQuery("Serve backend is stopped", 1, 100),
            ))
        assert context_unavailable.value.code == "brain.capability.unavailable"
        assert context_unavailable.value.retryable is True

        continuity_authorization = OwnerAuthorization(
            BrainOperation.CONTINUITY_RECOVER,
            "read",
            selector,
            "vault_demo",
            "vault://vault_demo",
            "test-policy",
            capability,
        )
        with pytest.raises(BrainRuntimeError) as continuity_unavailable:
            asyncio.run(LocalConnectorContinuityProvider(client).recover(
                continuity_authorization,
                ContinuityRecoverRequest(100),
            ))
        assert continuity_unavailable.value.code == "brain.capability.unavailable"
        assert continuity_unavailable.value.retryable is True

        with pytest.raises(ConnectorError) as unauthenticated:
            client.call("/unauthenticated", {})
        assert not isinstance(unauthenticated.value, ConnectorUnavailable)
        assert unauthenticated.value.code == "UNAUTHENTICATED"
        assert unauthenticated.value.status == 401

        with pytest.raises(ConnectorError) as forbidden:
            client.call("/forbidden", {})
        assert not isinstance(forbidden.value, ConnectorUnavailable)
        assert forbidden.value.code == "ACCESS_DENIED"
        assert forbidden.value.status == 403

        with pytest.raises(ConnectorError) as malformed_success:
            client.call("/malformed-success", {})
        assert not isinstance(malformed_success.value, ConnectorUnavailable)
        assert malformed_success.value.code == "INVALID_RESPONSE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
