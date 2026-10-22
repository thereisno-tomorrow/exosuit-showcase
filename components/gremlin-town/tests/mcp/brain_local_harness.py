"""Disposable fixtures and process helpers for the PC-local connector tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BRAIN_CORE_ROOT = REPOSITORY_ROOT.parent / "brain-core"
BRAIN_CORE_SRC = BRAIN_CORE_ROOT / "src"
BRAIN_PYTHON = BRAIN_CORE_ROOT / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)

for source in (REPOSITORY_ROOT / "src", BRAIN_CORE_SRC):
    rendered = str(source)
    if rendered not in sys.path:
        sys.path.insert(0, rendered)

from gremlin_town.mcp.local_project_connector import (  # noqa: E402
    CONFIG_SCHEMA,
    LocalProjectConnectorClient,
    RunningLocalProjectConnector,
)


READER_TOKEN = "fixture-reader-token"
WRITER_TOKEN = "fixture-writer-token"
CONTEXT_ONLY_TOKEN = "fixture-context-only-token"
SESSION_ID = "fixture-session-1"
DENIED_CANARY = "LOCAL-DENIED-CANARY-MUST-NOT-LEAK"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text_exact(path: Path, value: str) -> None:
    path.write_bytes(value.encode("utf-8"))


def load_lane_b_fake():
    """Load Brain Core's actual fake GitHub HTTP seam without copying or mocking it."""

    import importlib.util

    path = BRAIN_CORE_ROOT / "tests" / "unit" / "test_github_storage.py"
    name = "_brain_local_harness_lane_b_github_storage_tests"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_local_memory_provider(accepted_at, *, review_enabled: bool = False):
    """A GT memory provider over the real, installed Brain Core, with Brain Home's
    GitHub network replaced by Brain Core's own fake transport - a local save proof
    that exercises real record validation, language rules and date resolution
    without any live network or subprocess. ``review_enabled`` mirrors
    ``[review] enabled`` (step E, off by default): only then does a save/change
    confirmation gain a ``calendar_suggestion`` (spec §5)."""

    from brain_core.records import Evidence
    from gremlin_town.mcp.github_brain_provider import BrainCoreRecordAdapter, GitHubBrainProvider

    lane_b = load_lane_b_fake()
    fake = lane_b.FakeGitHub()
    # With an object cache, as the hub builds it: the provider scans up to 5,000 files, which Brain Core
    # refuses on a storage without one (the storage-limit plan, layer 1).
    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    storage = GitHubBrainHomeStorage(
        repository=lane_b.REPOSITORY, branch=lane_b.BRANCH, token=lane_b.TOKEN, transport=fake,
        object_cache=GitHubObjectCache(),
    )

    def source_evidence(_authorization, reference: str) -> Evidence:
        return Evidence(
            source_type="fixture",
            locator=f"fixture://local-harness/{reference}",
            content_sha256=sha256_text(reference),
            excerpt="local harness fixture evidence",
            actor="local-harness",
        )

    adapter = BrainCoreRecordAdapter(operation_time=lambda _operation_id: accepted_at, source_evidence=source_evidence)
    return GitHubBrainProvider(storage, adapter, review_enabled=review_enabled), fake


def _manifest(project_id: str, name: str, exported: list[str], search: list[str]) -> dict[str, Any]:
    return {
        "schema": "brain.vault/v1",
        "id": project_id,
        "name": name,
        "purpose": "Disposable connector acceptance fixture",
        "topics": ["telemetry", "fixture", "current"],
        "sensitivity": "private",
        "sync_class": "local-only",
        "default_branch": "main",
        "intent": None,
        "record_roots": [],
        "dependencies": [],
        "search": {"paths": search, "purpose": "ranking-only"},
        "exports": [{"name": "fixture-context", "paths": exported, "max_bytes": 20000}],
    }


def _grant(project_id: str, paths: list[str]) -> dict[str, Any]:
    return {
        "schema": "brain.grant/v1",
        "id": f"grant_{project_id.removeprefix('vault_')}",
        "issuer": "fixture-authority",
        "authenticated_source": "fixture://authorization",
        "source_message_sha256": "a" * 64,
        "session_id": SESSION_ID,
        "operation": "read",
        "vault": project_id,
        "paths": paths,
        "issued_at": "2020-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


@dataclass(slots=True)
class FixtureEnvironment:
    root: Path
    git_project: Path
    non_git_project: Path
    outside_root: Path
    git_continuity_root: Path
    non_git_continuity_root: Path
    config_path: Path
    node_map_path: Path
    git_project_id: str = "vault_git_fixture"
    non_git_project_id: str = "vault_nongit_fixture"

    def client(self, base_url: str, token: str = READER_TOKEN) -> LocalProjectConnectorClient:
        return LocalProjectConnectorClient(base_url, token)

    def running(self) -> RunningLocalProjectConnector:
        return RunningLocalProjectConnector(self.config_path, brain_src=BRAIN_CORE_SRC)

    def git_status(self) -> tuple[str, ...]:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=self.git_project,
            check=True,
            capture_output=True,
            text=True,
        )
        return tuple(line for line in result.stdout.splitlines() if line)


def build_fixture(root: Path) -> FixtureEnvironment:
    git_project = root / "authorized-git-project"
    non_git_project = root / "authorized-nongit-project"
    outside_root = root / "outside-enrollment"
    git_continuity_root = root / "continuity-git"
    non_git_continuity_root = root / "continuity-nongit"
    for path in (
        git_project,
        non_git_project,
        outside_root,
        git_continuity_root,
        non_git_continuity_root,
    ):
        path.mkdir(parents=True)

    baseline = "Committed telemetry says 10 probes.\n"
    current = "Uncommitted telemetry says 42 probes.\n"
    untracked = "Local-only note recommends keeping the request reference.\n"
    nongit = "Non-Git local context is current.\n"
    denied = f"{DENIED_CANARY}\n"

    write_text_exact(git_project / "tracked.md", baseline)
    write_text_exact(git_project / "denied.md", denied)
    write_json(
        git_project / "brain.yaml",
        _manifest("vault_git_fixture", "Git fixture", ["tracked.md"], ["tracked.md"]),
    )
    write_json(
        git_project / ".brain" / "retrieval.json",
        {
            "schema": "brain.a02-retrieval-catalog/v1",
            "repository_version": "fixture-worktree-v1",
            "evidence": [
                {
                    "evidence_id": "E_TRACKED",
                    "owner_vault": "vault_git_fixture",
                    "relative_path": "tracked.md",
                    "content_sha256": sha256_text(baseline),
                    "state": "current",
                }
            ],
            "links": [],
        },
    )
    subprocess.run(["git", "init", "-q"], cwd=git_project, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=git_project, check=True)
    subprocess.run(["git", "config", "user.name", "Connector Fixture"], cwd=git_project, check=True)
    subprocess.run(
        ["git", "add", "tracked.md", "brain.yaml", ".brain/retrieval.json"],
        cwd=git_project,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "fixture baseline"], cwd=git_project, check=True)

    write_text_exact(git_project / "tracked.md", current)
    write_text_exact(git_project / "local-only.md", untracked)
    write_json(
        git_project / ".brain" / "retrieval.json",
        {
            "schema": "brain.a02-retrieval-catalog/v1",
            "repository_version": "fixture-worktree-v2-uncommitted",
            "evidence": [
                {
                    "evidence_id": "E_TRACKED",
                    "owner_vault": "vault_git_fixture",
                    "relative_path": "tracked.md",
                    "content_sha256": sha256_text(current),
                    "state": "current",
                },
                {
                    "evidence_id": "E_LOCAL",
                    "owner_vault": "vault_git_fixture",
                    "relative_path": "local-only.md",
                    "content_sha256": sha256_text(untracked),
                    "state": "current",
                },
                {
                    "evidence_id": "E_DENIED",
                    "owner_vault": "vault_git_fixture",
                    "relative_path": "denied.md",
                    "content_sha256": sha256_text(denied),
                    "state": "current",
                },
            ],
            "links": [],
        },
    )

    write_text_exact(non_git_project / "current.md", nongit)
    write_json(
        non_git_project / "brain.yaml",
        _manifest("vault_nongit_fixture", "Non-Git fixture", ["current.md"], ["current.md"]),
    )
    write_json(
        non_git_project / ".brain" / "retrieval.json",
        {
            "schema": "brain.a02-retrieval-catalog/v1",
            "repository_version": "fixture-nongit-v1",
            "evidence": [
                {
                    "evidence_id": "E_NONGIT",
                    "owner_vault": "vault_nongit_fixture",
                    "relative_path": "current.md",
                    "content_sha256": sha256_text(nongit),
                    "state": "current",
                }
            ],
            "links": [],
        },
    )
    write_text_exact(outside_root / "secret.md", "outside-root secret body\n")

    node_map = root / "node-map.yaml"
    write_json(
        node_map,
        {
            "schema": "brain.node-map/v1",
            "node": "local",
            "vaults": [
                {"id": "vault_git_fixture", "root": git_project.as_posix(), "manifest": "brain.yaml"},
                {"id": "vault_nongit_fixture", "root": non_git_project.as_posix(), "manifest": "brain.yaml"},
            ],
        },
    )

    config = root / "connector.json"
    write_json(
        config,
        {
            "schema": CONFIG_SCHEMA,
            "bind_host": "127.0.0.1",
            "port": 0,
            "encrypted_overlay_networks": [],
            "trusted_client_networks": ["127.0.0.0/8"],
            "auth": [
                {
                    "token_sha256": sha256_text(READER_TOKEN),
                    "scopes": ["context:read", "continuity:read"],
                },
                {
                    "token_sha256": sha256_text(WRITER_TOKEN),
                    "scopes": ["context:read", "project:write", "continuity:read", "continuity:write"],
                },
                {
                    "token_sha256": sha256_text(CONTEXT_ONLY_TOKEN),
                    "scopes": ["context:read"],
                },
            ],
            "node_map": node_map.as_posix(),
            "projects": [
                {
                    "id": "vault_git_fixture",
                    "retrieval_catalog": ".brain/retrieval.json",
                    "grants": [
                        _grant("vault_git_fixture", ["local-only.md", "escape/secret.md"])
                    ],
                    "write_paths": ["tracked.md"],
                    "continuity_root": git_continuity_root.as_posix(),
                },
                {
                    "id": "vault_nongit_fixture",
                    "retrieval_catalog": ".brain/retrieval.json",
                    "grants": [],
                    "write_paths": [],
                    "continuity_root": non_git_continuity_root.as_posix(),
                },
            ],
        },
    )
    return FixtureEnvironment(
        root,
        git_project,
        non_git_project,
        outside_root,
        git_continuity_root,
        non_git_continuity_root,
        config,
        node_map,
    )


def create_escape_reparse(fixture: FixtureEnvironment) -> Path:
    link = fixture.git_project / "escape"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(fixture.outside_root)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"could not create disposable junction: {result.stderr or result.stdout}")
    else:
        link.symlink_to(fixture.outside_root, target_is_directory=True)
    return link


def remove_escape_reparse(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    elif link.is_junction():
        link.rmdir()


@dataclass(slots=True)
class ConnectorProcess:
    process: subprocess.Popen[str]
    ready_file: Path
    base_url: str

    def stop(self) -> tuple[str, str]:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        stdout, stderr = self.process.communicate(timeout=1)
        return stdout, stderr


def start_connector_process(fixture: FixtureEnvironment, label: str) -> ConnectorProcess:
    if not BRAIN_PYTHON.is_file():
        raise RuntimeError(f"Brain Core test interpreter missing: {BRAIN_PYTHON}")
    ready = fixture.root / f"{label}-ready.json"
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT / "src"), str(BRAIN_CORE_SRC), existing]
    ).rstrip(os.pathsep)
    process = subprocess.Popen(
        [
            str(BRAIN_PYTHON),
            "-m",
            "gremlin_town.mcp.local_project_connector",
            "serve",
            "--config",
            str(fixture.config_path),
            "--ready-file",
            str(ready),
            "--brain-src",
            str(BRAIN_CORE_SRC),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if ready.is_file():
            value = json.loads(ready.read_text(encoding="utf-8"))
            return ConnectorProcess(process, ready, value["base_url"])
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"connector exited before ready: {stdout}\n{stderr}")
        time.sleep(0.05)
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    raise RuntimeError(f"connector did not become ready: {stdout}\n{stderr}")


def start_fault_connector_process(
    fixture: FixtureEnvironment,
    label: str,
    *,
    fault_stage: str,
    exit_code: int = 97,
) -> ConnectorProcess:
    """Start a real connector process that exits at one reconciliation boundary."""

    if fault_stage not in {"after_pending", "after_effect"}:
        raise ValueError("fault_stage must be after_pending or after_effect")
    if not BRAIN_PYTHON.is_file():
        raise RuntimeError(f"Brain Core test interpreter missing: {BRAIN_PYTHON}")
    ready = fixture.root / f"{label}-ready.json"
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT / "src"), str(BRAIN_CORE_SRC), existing]
    ).rstrip(os.pathsep)
    process = subprocess.Popen(
        [
            str(BRAIN_PYTHON),
            str(Path(__file__).resolve()),
            "serve-fault",
            "--config",
            str(fixture.config_path),
            "--ready-file",
            str(ready),
            "--brain-src",
            str(BRAIN_CORE_SRC),
            "--fault-stage",
            fault_stage,
            "--exit-code",
            str(exit_code),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if ready.is_file():
            try:
                value = json.loads(ready.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.01)
                continue
            return ConnectorProcess(process, ready, value["base_url"])
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"fault connector exited before ready: {stdout}\n{stderr}")
        time.sleep(0.05)
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    raise RuntimeError(f"fault connector did not become ready: {stdout}\n{stderr}")


def raw_request_without_body(base_url: str, route: str, token: str, content_length: int = 9999) -> bytes:
    host_port = base_url.removeprefix("http://")
    host, port_text = host_port.rsplit(":", 1)
    with socket.create_connection((host.strip("[]"), int(port_text)), timeout=2) as connection:
        request = (
            f"POST {route} HTTP/1.1\r\n"
            f"Host: {host_port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {content_length}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _serve_fault_connector(arguments: argparse.Namespace) -> None:
    def operation_fault(stage: str, _operation_id: str) -> None:
        if stage == arguments.fault_stage:
            os._exit(arguments.exit_code)

    running = RunningLocalProjectConnector(
        arguments.config,
        brain_src=arguments.brain_src,
        operation_fault=operation_fault,
    ).start()
    write_json(Path(arguments.ready_file), {"base_url": running.base_url})
    running.thread.join()


def _main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    fault = subparsers.add_parser("serve-fault")
    fault.add_argument("--config", required=True)
    fault.add_argument("--ready-file", required=True)
    fault.add_argument("--brain-src", required=True)
    fault.add_argument("--fault-stage", choices=("after_pending", "after_effect"), required=True)
    fault.add_argument("--exit-code", type=int, required=True)
    arguments = parser.parse_args()
    if arguments.command == "serve-fault":
        _serve_fault_connector(arguments)


if __name__ == "__main__":
    _main()
