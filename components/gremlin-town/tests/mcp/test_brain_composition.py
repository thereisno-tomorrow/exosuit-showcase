from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import anyio
from fastapi.testclient import TestClient

from brain_core.durability import CATALOG_PATH
from brain_core.github_storage import GitHubBrainHomeStorage
from gremlin_town.mcp import brain_assembly
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    DecisionRequest,
    MemorySaveRequest,
    OwnerSelector,
)
from gremlin_town.runtime import BrainMCPSettings, build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_mcp_runtime import mcp_config


FIRST_TIME = datetime.fromisoformat("2026-09-21T12:00:00+08:00")
LATER_TIME = datetime.fromisoformat("2026-10-04T09:30:00+08:00")


def _lane_b_fake_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "brain-core"
        / "tests"
        / "unit"
        / "test_github_storage.py"
    )
    name = "_brain_core_lane_b_composition_fake"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LANE_B = _lane_b_fake_module()


def _write(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _write_json(path: Path, value: object) -> Path:
    return _write(path, json.dumps(value))


def _settings(tmp_path: Path) -> BrainMCPSettings:
    connector_token = _write(tmp_path / "secrets" / "connector", "fixture-connector-token")
    github_token = _write(tmp_path / "secrets" / "github", LANE_B.TOKEN)
    policy = _write_json(
        tmp_path / "brain-policy.json",
        {
            "schema": "gt.brain-owner-policy/v1",
            "revision": "fixture-policy-v1",
            "owners": [
                {
                    "kind": "personal",
                    "project_id": None,
                    "owner_id": "brain-home",
                    "owner_locator": "vault://brain-home",
                    "readers": ["chatgpt-web"],
                    "writers": ["chatgpt-web"],
                    "operations": ["decision.record", "memory.save", "memory.read"],
                    "session_id": None,
                }
            ],
        },
    )
    sources = _write_json(
        tmp_path / "brain-sources.json",
        {
            "schema": "gt.brain-source-evidence/v1",
            "revision": "fixture-sources-v1",
            "sources": [],
        },
    )
    return BrainMCPSettings(
        connector_url="http://127.0.0.1:65530",
        connector_token_file=connector_token,
        github_repository=LANE_B.REPOSITORY,
        github_branch=LANE_B.BRANCH,
        github_token_file=github_token,
        policy_file=policy,
        decision_staging_dir=tmp_path / "provider-state",
        ownership_catalog_file=Path(CATALOG_PATH),
        source_registry_file=sources,
    )


def _decision(operation_id: str) -> DecisionRequest:
    return DecisionRequest(
        operation_id=operation_id,
        choice="Publish through the configured Brain Home branch",
        title="Use the configured Brain Home branch",
        because="One canonical result is required",
        forecloses="Uncorrelated file-by-file publication",
        governing_scope="Brain Home publication",
        encoded_in="Gremlin Town Brain provider",
        completeness_check="Record and receipt share an accepted commit",
        source_reference="mcp-request:ordinary-decision",
    )


def test_assembled_runtime_fresh_process_save_read_and_semantic_dedupe(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    remote = LANE_B.FakeGitHub()
    caller = BrainCaller("chatgpt-web", frozenset({"bc:read", "bc:write"}))
    selector = OwnerSelector("personal")

    async def exercise() -> None:
        first = build_brain_runtime(
            settings,
            github_transport=remote,
            operation_clock=lambda: FIRST_TIME,
        )
        saved = await first.save_memory(
            caller,
            selector,
            MemorySaveRequest(
                operation_id="ordinary-preference-1",
                kind="item",
                title="Prefer exact reconciliation",
                body="Always reconcile an accepted operation before retrying it.",
                source_reference="mcp-request:ordinary-preference-1",
                said_by="owner",
                came_by="direct",
                words="Always reconcile an accepted operation before retrying it.",
                level="accepted",
            ),
        )
        observed = await first.read_memory(caller, selector, saved.record_id or "")
        accepted = await first.record_decision(caller, selector, _decision("decision-origin-one"))
        await first.aclose()

        fresh = build_brain_runtime(
            settings,
            github_transport=remote,
            operation_clock=lambda: LATER_TIME,
        )
        recovered = await fresh.read_memory(caller, selector, saved.record_id or "")
        duplicate = await fresh.record_decision(caller, selector, _decision("decision-origin-two"))
        await fresh.aclose()

        assert saved.status == "accepted" and saved.remote_observed is True
        assert observed.record_id == recovered.record_id == saved.record_id
        assert observed.content_sha256 == recovered.content_sha256 == saved.content_sha256
        assert accepted.status == "accepted"
        assert duplicate.status == "duplicate"
        assert duplicate.record_id == accepted.record_id

    anyio.run(exercise)

    tree = remote.trees[remote.commits[remote.branch]["tree"]]
    decision_records = [
        path for path in tree if path.startswith("records/decision/") and path.endswith(".md")
    ]
    decision_receipts = [
        path for path in tree if path.startswith("records/.durability/receipts/")
    ]
    assert len(decision_records) == 1
    assert sorted(decision_receipts) == [
        "records/.durability/receipts/decision-origin-one.json",
        "records/.durability/receipts/decision-origin-two.json",
    ]


def test_github_app_mode_saves_and_reads_with_one_minted_token(tmp_path: Path) -> None:
    from dataclasses import replace
    from datetime import UTC, timedelta

    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = _write(tmp_path / "secrets" / "brain-app.pem", key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode())
    settings = replace(_settings(tmp_path), github_token_file=None, github_app_id=5,
                       github_installation_id=9, github_app_private_key_file=key_file)
    mints: list[dict] = []

    class MintClient:
        def request(self, method, url, **kwargs):
            mints.append({"method": method, "url": url, "json": kwargs["json"]})
            # The fake GitHub accepts only its fixture token, so every storage
            # request proves the placeholder was replaced by the minted token.
            return httpx.Response(201, json={
                "token": LANE_B.TOKEN,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "permissions": {"contents": "write", "metadata": "read"},
                "repositories": [{"full_name": LANE_B.REPOSITORY}],
            })

    remote = LANE_B.FakeGitHub()
    caller = BrainCaller("chatgpt-web", frozenset({"bc:read", "bc:write"}))
    selector = OwnerSelector("personal")

    async def exercise() -> None:
        runtime = build_brain_runtime(settings, github_transport=remote, github_app_client=MintClient(),
                                      operation_clock=lambda: FIRST_TIME)
        saved = await runtime.save_memory(caller, selector, MemorySaveRequest(
            operation_id="app-token-capture-1", kind="item", title="Hub renews its own GitHub access",
            body="The hub mints one-hour GitHub App tokens.", source_reference="mcp-request:app-token-capture-1",
            said_by="owner", came_by="direct", words="The hub mints one-hour GitHub App tokens.",
            level="possible",
        ))
        observed = await runtime.read_memory(caller, selector, saved.record_id or "")
        await runtime.aclose()
        assert saved.status == "accepted" and observed.record_id == saved.record_id

    anyio.run(exercise)
    assert mints == [{"method": "POST", "url": "https://api.github.com/app/installations/9/access_tokens",
                      "json": {"repositories": ["brain-home"], "permissions": {"contents": "write"}}}]


def test_m2_runtime_config_composes_original_nine_and_nine_bc_tools(
    tmp_path: Path, monkeypatch
) -> None:
    config, _ = mcp_config(tmp_path)
    settings = _settings(tmp_path)
    remote = LANE_B.FakeGitHub()

    def fake_http_storage(**kwargs):
        kwargs.pop("transport", None)
        return GitHubBrainHomeStorage(transport=remote, **kwargs)

    monkeypatch.setattr(brain_assembly, "GitHubBrainHomeStorage", fake_http_storage)
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            f'''
[brain_core]
enabled = true
connector_url = "{settings.connector_url}"
connector_token_file = "{settings.connector_token_file.as_posix()}"
github_repository = "{settings.github_repository}"
github_branch = "{settings.github_branch}"
github_token_file = "{settings.github_token_file.as_posix()}"
policy_file = "{settings.policy_file.as_posix()}"
decision_staging_dir = "{settings.decision_staging_dir.as_posix()}"
ownership_catalog_file = "{settings.ownership_catalog_file.as_posix()}"
source_registry_file = "{settings.source_registry_file.as_posix()}"
'''
        )

    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    with TestClient(runtime.app):
        tools = anyio.run(runtime.mcp_server.list_tools)
        names = {tool.name for tool in tools}
        assert len(names) == 22
        assert {
            "gt_participants_list",
            "gt_requests_create",
            "gt_requests_list",
            "gt_requests_read",
            "gt_requests_report",
            "gt_requests_reply",
            "gt_requests_cancel",
            "gt_requests_check",
            "gt_operations_get",
            "gt_events_list",
        } <= names
        assert {name for name in names if name.startswith("bc_")} == {
            "bc_context_retrieve",
            "bc_continuity_start",
            "bc_continuity_append",
            "bc_continuity_recover",
            "bc_continuity_list",
            "bc_continuity_close",
            "bc_decision_record",
            "bc_memory_save",
            "bc_memory_change",
            "bc_memory_read",
    "bc_memory_search",
            "bc_memory_list",
        }
        assert runtime.brain_runtime is not None
