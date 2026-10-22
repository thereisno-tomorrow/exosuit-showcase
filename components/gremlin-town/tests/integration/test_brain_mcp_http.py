"""M3 Brain Core acceptance over real local OAuth and MCP HTTP surfaces.

All sockets bind to 127.0.0.1. GitHub is B's fake HTTP transport and the PC
connector uses C's disposable authorized project fixture. Nothing is enrolled,
published, messaged, or deployed outside the test roots.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import socket
import sys
import threading
import time

import anyio
import httpx
import uvicorn

from brain_core.durability import CATALOG_PATH
from brain_core.github_storage import GitHubBrainHomeStorage
from gremlin_town.entrypoint import ListenerDispatch
from gremlin_town.mcp import brain_assembly
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    MemorySaveRequest,
    OwnerSelector,
)
from gremlin_town.runtime import BrainMCPSettings, build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_chatgpt_mcp import (
    HTTPHub,
    PRIVATE,
    PUBLIC,
    READ_TOOLS,
    WRITE_TOOLS,
    _write_config,
)
from tests.mcp.test_brain_composition import LANE_B


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from brain_local_harness import (  # noqa: E402
    DENIED_CANARY,
    SESSION_ID,
    WRITER_TOKEN,
    build_fixture,
)


BC_TOOLS = {
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
PUBLIC_PROJECT_ID = "vault_git_fixture"
FIRST_TIME = datetime.fromisoformat("2026-09-21T12:00:00+08:00")
LATER_TIME = datetime.fromisoformat("2026-10-04T09:30:00+08:00")


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _append_brain_config(
    config: Path,
    root: Path,
    connector_url: str,
    project_id: str,
) -> None:
    connector_token = root / "brain-connector-token"
    connector_token.write_text(WRITER_TOKEN, encoding="utf-8")
    github_token = root / "brain-github-token"
    github_token.write_text(LANE_B.TOKEN, encoding="utf-8")
    policy = _write_json(
        root / "brain-policy.json",
        {
            "schema": "gt.brain-owner-policy/v1",
            "revision": "m3-fixture-policy-v1",
            "owners": [
                {
                    "kind": "project",
                    "project_id": project_id,
                    "owner_id": project_id,
                    "owner_locator": f"vault://{project_id}",
                    "readers": ["chatgpt-web"],
                    "writers": ["chatgpt-web"],
                    "operations": [
                        "context.retrieve",
                        "continuity.start",
                        "continuity.append",
                        "continuity.recover",
                        "continuity.close",
                    ],
                    "session_id": SESSION_ID,
                },
                {
                    "kind": "personal",
                    "project_id": None,
                    "owner_id": "brain-home",
                    "owner_locator": "vault://brain-home",
                    "readers": ["chatgpt-web"],
                    "writers": ["chatgpt-web"],
                    "operations": ["decision.record", "memory.save", "memory.read", "memory.search", "continuity.start", "continuity.append", "continuity.recover", "continuity.close"],
                    "session_id": None,
                },
            ],
        },
    )
    sources = _write_json(
        root / "brain-sources.json",
        {
            "schema": "gt.brain-source-evidence/v1",
            "revision": "m3-empty-document-evidence-v1",
            "sources": [],
        },
    )
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            f'''
[brain_core]
enabled = true
connector_url = "{connector_url}"
connector_token_file = "{connector_token.as_posix()}"
github_repository = "{LANE_B.REPOSITORY}"
github_branch = "{LANE_B.BRANCH}"
github_token_file = "{github_token.as_posix()}"
policy_file = "{policy.as_posix()}"
decision_staging_dir = "{(root / 'brain-provider-state').as_posix()}"
ownership_catalog_file = "{Path(CATALOG_PATH).as_posix()}"
source_registry_file = "{sources.as_posix()}"
'''
        )


def _personal_brain_settings(root: Path) -> BrainMCPSettings:
    root.mkdir(parents=True)
    connector_token = root / "connector-token"
    connector_token.write_text(WRITER_TOKEN, encoding="utf-8")
    github_token = root / "github-token"
    github_token.write_text(LANE_B.TOKEN, encoding="utf-8")
    policy = _write_json(
        root / "policy.json",
        {
            "schema": "gt.brain-owner-policy/v1",
            "revision": "m3-memory-replay-v1",
            "owners": [
                {
                    "kind": "personal",
                    "project_id": None,
                    "owner_id": "brain-home",
                    "owner_locator": "vault://brain-home",
                    "readers": ["chatgpt-web"],
                    "writers": ["chatgpt-web"],
                    "operations": ["memory.save", "memory.read"],
                    "session_id": None,
                }
            ],
        },
    )
    sources = _write_json(
        root / "sources.json",
        {
            "schema": "gt.brain-source-evidence/v1",
            "revision": "m3-memory-replay-empty-v1",
            "sources": [],
        },
    )
    return BrainMCPSettings(
        connector_url="http://127.0.0.1:1",
        connector_token_file=connector_token,
        github_repository=LANE_B.REPOSITORY,
        github_branch=LANE_B.BRANCH,
        github_token_file=github_token,
        policy_file=policy,
        decision_staging_dir=root / "state",
        ownership_catalog_file=Path(CATALOG_PATH),
        source_registry_file=sources,
    )


@contextmanager
def _running_brain_http(root: Path, monkeypatch):
    fixture = build_fixture(root / "connector")
    remote = LANE_B.FakeGitHub()

    def fake_http_storage(**kwargs):
        kwargs.pop("transport", None)
        return GitHubBrainHomeStorage(transport=remote, **kwargs)

    monkeypatch.setattr(brain_assembly, "GitHubBrainHomeStorage", fake_http_storage)
    sockets: list[socket.socket] = []
    runtime = None
    server = None
    thread = None
    with fixture.running() as connector:
        try:
            for _ in range(2):
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(128)
                sockets.append(listener)
            private_port, mcp_port = [item.getsockname()[1] for item in sockets]
            config = _write_config(root, private_port, mcp_port)
            _append_brain_config(config, root, connector.base_url, PUBLIC_PROJECT_ID)
            runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
            dispatch = ListenerDispatch(
                runtime.app,
                private_port=private_port,
                mcp_app=runtime.mcp_app,
                mcp_port=mcp_port,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    dispatch,
                    log_level="critical",
                    access_log=False,
                    lifespan="on",
                    proxy_headers=False,
                )
            )
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": sockets}, daemon=True
            )
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            with (
                httpx.Client(
                    base_url=f"http://127.0.0.1:{private_port}",
                    headers={"Host": "private.test"},
                    trust_env=False,
                    timeout=5,
                ) as private,
                httpx.Client(
                    base_url=f"http://127.0.0.1:{mcp_port}",
                    headers={"Host": "gt.test"},
                    trust_env=False,
                    timeout=5,
                ) as public,
            ):
                yield HTTPHub(runtime, FakeTransport(), private, public), fixture, remote
        finally:
            if server is not None:
                server.should_exit = True
            if thread is not None:
                thread.join(timeout=10)
            for listener in sockets:
                listener.close()
            if thread is not None:
                assert not thread.is_alive()
            if runtime is not None and (server is None or not server.started):
                runtime.close()


def _bc_tool(hub: HTTPHub, token: str, name: str, **arguments):
    result = hub.rpc(
        token,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result


def _bc_ok(hub: HTTPHub, token: str, name: str, **arguments):
    result = _bc_tool(hub, token, name, **arguments)
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_fresh_state_memory_replay_reconciles_before_later_clock(
    tmp_path: Path,
) -> None:
    from tests.mcp.test_github_brain_provider import _HistoryTransport

    remote = LANE_B.FakeGitHub()
    caller = BrainCaller("chatgpt-web", frozenset({"bc:read", "bc:write"}))
    selector = OwnerSelector("personal")
    request = MemorySaveRequest(
        operation_id="desktop-replay-check",
        kind="item",
        title="Fixture preference",
        body="Prepare the Friday decision deck.",
        source_reference="mcp-request:desktop-replay-check",
        said_by="owner",
        came_by="direct",
        words="Prepare the Friday decision deck.",
        level="accepted",
    )

    async def exercise() -> tuple[object, object]:
        first = build_brain_runtime(
            _personal_brain_settings(tmp_path / "first-host"),
            github_transport=_HistoryTransport(remote),
            operation_clock=lambda: FIRST_TIME,
        )
        accepted = await first.save_memory(caller, selector, request)
        await first.aclose()

        second = build_brain_runtime(
            _personal_brain_settings(tmp_path / "second-host"),
            github_transport=_HistoryTransport(remote),
            operation_clock=lambda: LATER_TIME,
        )
        replayed = await second.save_memory(caller, selector, request)
        await second.aclose()
        return accepted, replayed

    accepted, replayed = anyio.run(exercise)

    assert accepted.status == "accepted"
    assert replayed.status == "duplicate"
    # The bridge's caller-scoped work namespace is distinct from the pre-cut save namespace.
    assert accepted.record_id == replayed.record_id == "brn_20260921_040000_39aa54e18cda10a3"
    assert accepted.record_id != "brn_20260921_040000_ce6d5bb206c21eab"
    tree = remote.trees[remote.commits[remote.branch]["tree"]]
    preferences = [path for path in tree if path.startswith("records/item/")]
    assert len(preferences) == 1


def test_authenticated_oauth_mcp_brain_flow_and_scope_denial(
    tmp_path: Path, monkeypatch
) -> None:
    with _running_brain_http(tmp_path, monkeypatch) as (hub, fixture, remote):
        gt_only = hub.authorize("gt:read gt:write")["access_token"]
        tools = hub.rpc(gt_only, "tools/list", {})["tools"]
        names = {tool["name"] for tool in tools}
        assert names == READ_TOOLS | WRITE_TOOLS | BC_TOOLS
        assert len(names) == 22
        for tool in tools:
            if tool["name"].startswith("bc_"):
                scopes = ["gt:read", "bc:read"]
                if tool["annotations"]["readOnlyHint"] is False:
                    scopes.append("bc:write")
            else:
                scopes = ["gt:read"]
                if tool["annotations"]["readOnlyHint"] is False:
                    scopes.append("gt:write")
            schemes = [{"type": "oauth2", "scopes": scopes}]
            assert tool["securitySchemes"] == schemes
            assert tool["_meta"]["securitySchemes"] == schemes
        denied = _bc_tool(
            hub,
            gt_only,
            "bc_context_retrieve",
            project_id=PUBLIC_PROJECT_ID,
            intent="telemetry request reference",
        )
        assert denied["isError"] is True
        assert denied["structuredContent"]["error"]["code"] == "ACCESS_DENIED"
        challenge = denied["_meta"]["mcp/www_authenticate"]
        assert challenge == [
            'Bearer resource_metadata="https://gt.test/.well-known/oauth-protected-resource/mcp", '
            'error="insufficient_scope", error_description="Additional authorization is required for this tool.", '
            'scope="bc:read"'
        ]

        bc_read_only = hub.authorize("gt:read gt:write bc:read")["access_token"]
        denied_write = _bc_tool(
            hub,
            bc_read_only,
            "bc_continuity_start",
            operation_id="scope-step-up",
            project_id=PUBLIC_PROJECT_ID,
            session_id=SESSION_ID,
            intent="prove minimal write scope",
            source_references=["mcp-request:scope-step-up"],
        )
        assert denied_write["structuredContent"]["error"]["code"] == "ACCESS_DENIED"
        assert denied_write["_meta"]["mcp/www_authenticate"] == [
            'Bearer resource_metadata="https://gt.test/.well-known/oauth-protected-resource/mcp", '
            'error="insufficient_scope", error_description="Additional authorization is required for this tool.", '
            'scope="bc:write"'
        ]

        token = hub.authorize("gt:read gt:write bc:read bc:write")["access_token"]
        tools = hub.rpc(token, "tools/list", {})["tools"]
        names = {tool["name"] for tool in tools}
        assert names == READ_TOOLS | WRITE_TOOLS | BC_TOOLS
        assert len(READ_TOOLS | WRITE_TOOLS) == 10
        assert len(BC_TOOLS) == 12
        assert len(names) == 22

        context = _bc_ok(
            hub,
            token,
            "bc_context_retrieve",
            project_id=PUBLIC_PROJECT_ID,
            intent="telemetry request reference denied",
            max_sources=12,
            max_source_tokens=1000,
        )
        assert context["freshness"] == "current"
        assert {item["evidence_id"] for item in context["sources"]} == {
            "E_TRACKED",
            "E_LOCAL",
        }
        assert DENIED_CANARY not in json.dumps(context)

        started = _bc_ok(
            hub,
            token,
            "bc_continuity_start",
            operation_id="m3-continuity-start",
            project_id=PUBLIC_PROJECT_ID,
            session_id=SESSION_ID,
            intent="Exercise continuity through authenticated MCP HTTP",
            source_references=["fixture-source-current"],
            next_step="Append one blocker",
        )
        assert started["state"] == "open"
        appended = _bc_ok(
            hub,
            token,
            "bc_continuity_append",
            operation_id="m3-continuity-append",
            project_id=PUBLIC_PROJECT_ID,
            session_id=SESSION_ID,
            event="blocker",
            value="Live trust enrollment remains gated",
        )
        assert appended["state"] == "open"
        recovered = _bc_ok(
            hub,
            token,
            "bc_continuity_recover",
            project_id=PUBLIC_PROJECT_ID,
            max_tokens=1000,
        )
        assert recovered["blockers"] == ["Live trust enrollment remains gated"]
        closed = _bc_ok(
            hub,
            token,
            "bc_continuity_close",
            operation_id="m3-continuity-close",
            project_id=PUBLIC_PROJECT_ID,
            session_id=SESSION_ID,
            recovered=True,
        )
        assert closed["state"] == "recovered"

        saved = _bc_ok(
            hub,
            token,
            "bc_memory_save",
            operation_id="m3-preference-save",
            scope="personal",
            kind="item",
            level="accepted",
            said_by="owner",
            came_by="direct",
            words="Look up accepted state before any exact retry.",
            title="Reconcile before retry",
            body="Look up accepted state before any exact retry.",
            source_reference="mcp-request:m3-preference-save",
        )
        assert saved["status"] == "accepted" and saved["remote_observed"] is True
        memory = _bc_ok(
            hub,
            token,
            "bc_memory_read",
            scope="personal",
            record_id=saved["record_id"],
        )
        assert memory["body"] == "Look up accepted state before any exact retry."
        assert memory["freshness"] == "current"

        decision = _bc_ok(
            hub,
            token,
            "bc_decision_record",
            operation_id="m3-decision-record",
            scope="personal",
            title="Use stable retry identity",
            choice="Reconcile accepted state before retry",
            because="A lost reply may hide an accepted canonical write",
            forecloses="Replacement operation keys",
            governing_scope="Brain Home publication",
            encoded_in="Gremlin Town Brain provider",
            completeness_check="Record and receipt share an accepted commit",
            source_reference="mcp-request:m3-decision-record",
        )
        assert decision["status"] == "accepted"
        assert decision["receipt_sha256"] is not None

        separated = _bc_tool(
            hub,
            token,
            "bc_context_retrieve",
            project_id="vault_another_project",
            intent="must not cross project scope",
        )
        assert separated["isError"] is True
        # ROA-72 ("Rule + helpful error"): a project the hub doesn't know is still refused, with
        # the rule and the project IDs this agent may use instead of a bare denial.
        assert separated["structuredContent"]["error"]["code"] == "brain.project.unknown"
        assert separated["structuredContent"]["error"]["message"].startswith(
            "Project vault_another_project is not enrolled. A project ID is vault_ plus the vault's name"
        )
        assert "Valid project IDs: " in separated["structuredContent"]["error"]["message"]
        assert "_meta" not in separated

        before_refusals = remote.branch
        for scope, project_id in (
            ("system", None),
            ("fleet", None),
            ("project", "some-project"),
        ):
            arguments = {
                "operation_id": f"m3-{scope}-decision-refused",
                "scope": scope,
                "title": "Unsupported owner adapter",
                "choice": f"Do not route {scope} decisions to Brain Home",
                "because": "No canonical owner adapter is enrolled for this scope",
                "forecloses": "Silent ownership widening",
                "governing_scope": f"{scope} memory",
                "encoded_in": "Current adapter boundary",
                "completeness_check": "Request is refused before publication",
                "source_reference": f"mcp-request:m3-{scope}-decision-refused",
            }
            if project_id is not None:
                arguments["project_id"] = project_id
            unsupported = _bc_tool(
                hub,
                token,
                "bc_decision_record",
                **arguments,
            )
            assert unsupported["isError"] is True
            assert unsupported["structuredContent"]["error"]["code"] == "ACCESS_DENIED"
        assert remote.branch == before_refusals

        tree = remote.trees[remote.commits[remote.branch]["tree"]]
        assert any(path.startswith("records/item/") for path in tree)
        assert len([path for path in tree if path.startswith("records/decision/")]) == 1


def test_everyday_memory_and_general_continuity_over_authenticated_http(tmp_path, monkeypatch):
    with _running_brain_http(tmp_path, monkeypatch) as (hub, fixture, remote):
        token = hub.authorize("gt:read gt:write bc:read bc:write")["access_token"]
        saved = _bc_ok(hub, token, "bc_memory_save", operation_id="wombat-http-save",
            scope="personal", kind="item", level="possible", said_by="owner", came_by="direct",
            words="I wanna raise a wombat", title="Wombat aspiration", body="I wanna raise a wombat")
        assert saved["status"] == "accepted"
        before = remote.branch
        found = _bc_ok(hub, token, "bc_memory_search", query="wombat")
        assert found["matches"] == []
        read = _bc_ok(hub, token, "bc_memory_read", scope="personal", record_id=saved["record_id"])
        assert read["record_id"] == saved["record_id"]
        assert read["title"] == "Wombat aspiration" and read["body"] == "I wanna raise a wombat"
        assert read["canonical_record"]["source"]["words"] == "I wanna raise a wombat"
        assert read["canonical_record"]["data"]["level"] == "possible"
        assert remote.branch == before
        continuity = _bc_ok(hub, token, "bc_continuity_start", operation_id="general-http-start",
            session_id="general-chat-one", intent="Discuss a personal aspiration", source_references=[])
        assert continuity["receipt"]["owner_locator"] == "vault://brain-home"
        _bc_ok(hub, token, "bc_continuity_append", operation_id="general-http-next",
            session_id="general-chat-one", event="next_step", value="Discuss responsible options")
        recovered = _bc_ok(hub, token, "bc_continuity_recover")
        assert recovered["next_step"] == "Discuss responsible options"
        gt_only = hub.authorize("gt:read gt:write")["access_token"]
        before = remote.branch
        denied = _bc_tool(hub, gt_only, "bc_memory_search", query="wombat")
        assert denied["isError"] and denied["structuredContent"]["error"]["code"] == "ACCESS_DENIED"
        assert "wombat" not in json.dumps(denied)
        assert remote.branch == before
