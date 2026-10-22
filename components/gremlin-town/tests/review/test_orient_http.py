"""Step F's operations over the hub's real HTTP bridge (`POST /v1/brain/{operation}`):
`orient` ("where am I?") and `next` ("what next?") need only `bc:read`, like their MCP tools,
while `next.respond` needs `bc:write`; the old `orient.respond` is gone, and with
`[orientation]` off none of them exist (Ciel slice 4, gap G9)."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

from gremlin_town.mcp import brain_assembly
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_runtime import BrainOperation
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_brain_agent_http import post, running
from tests.integration.test_mcp_runtime import mcp_config
from tests.mcp.test_brain_composition import FIRST_TIME, LANE_B, _settings
from tests.review.test_review_sources_brain_wiring import ITEM_RECORD


def _hub_config(tmp_path, *, orientation: bool):
    config, tokens = mcp_config(tmp_path)
    settings = _settings(tmp_path)
    policy = json.loads(settings.policy_file.read_text())
    policy["subject_scopes"] = {"alice": ["bc:read", "bc:write"], "bob": ["bc:read"]}
    owner = policy["owners"][0]
    owner["readers"] = ["alice", "bob"]
    owner["writers"] = ["alice"]
    owner["operations"] = [op.value for op in BrainOperation if op != BrainOperation.CONTEXT_RETRIEVE]
    settings.policy_file.write_text(json.dumps(policy))
    with config.open("a") as handle:
        handle.write("\n[brain_core]\nenabled = true\n")
        for field in fields(settings):
            value = getattr(settings, field.name)
            if value is not None:
                encoded = json.dumps(value) if isinstance(value, bool) else json.dumps(str(value).replace(chr(92), '/'))
                handle.write(f"{field.name} = {encoded}\n")
        handle.write(
            "\n[review]\nenabled = true\nsender_participant = \"exosuit-review\"\n"
            f'delivery_participant = "hermes-wa"\nproject = "demo"\n'
            f'why_log_file = "{(tmp_path / "why.sqlite3").as_posix()}"\n'
        )
        handle.write(f"\n[orientation]\nenabled = {'true' if orientation else 'false'}\n")
    return config, tokens


@pytest.fixture
def hub_factory(tmp_path, monkeypatch):
    import gremlin_town.runtime as runtime_module

    remote = LANE_B.FakeGitHub()
    monkeypatch.setattr(
        brain_assembly, "build_brain_runtime",
        lambda configured, **kwargs: build_brain_runtime(
            configured, github_transport=remote, operation_clock=lambda: FIRST_TIME, **kwargs,
        ),
    )
    items = LANE_B.FakeGitHub()
    items.seed({"records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD})
    # As `_cached_brain_storage` builds it: with an object cache, or the hub's 5,000-file cap is refused.
    storage = GitHubBrainHomeStorage(repository="trusted/brain-home", branch="accepted", token="fixture-token",
                                     transport=items, object_cache=GitHubObjectCache())
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: storage)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (storage, None))

    def build(*, orientation: bool):
        config, tokens = _hub_config(tmp_path, orientation=orientation)
        return build_runtime(config, provider=FakeTransport(), start_worker=False), tokens

    build.remote=remote
    return build


def test_both_asks_need_only_read_scope_and_the_answer_needs_write(hub_factory):
    runtime, tokens = hub_factory(orientation=True)
    try:
        with running(runtime) as client:
            whole = post(client, tokens["bob"], "orient", {})
            assert whole.status_code == 200, whole.text
            assert whole.json()["outcome"] == "answered" and "top_pick" not in whole.json()

            pick = post(client, tokens["bob"], "next", {})
            assert pick.status_code == 200, pick.text
            assert pick.json()["top_pick"]["record_id"] == "brn_20260926_120000_aabb"
            assert pick.json()["top_pick"]["title"] == "Send Sam the deck"

            answer = {"surfacing_id": "brn_20260926_120000_aabb", "response": "not_now"}
            assert post(client, tokens["bob"], "next.respond", answer).status_code == 403
            recorded = post(client, tokens["alice"], "next.respond", answer)
            assert recorded.status_code == 200, recorded.text
            assert recorded.json() == {"outcome": "recorded", "response": "not_now"}

            # Renamed with the tools (gap G9): nothing answers to the old name.
            assert post(client, tokens["alice"], "orient.respond", answer).status_code == 422
    finally:
        runtime.close()


def test_agenda_and_weekly_need_only_read_scope_and_project_saves_need_write(hub_factory):
    """ROA-86: like their MCP tools, the two read-only asks of release ciel-03 answer a caller
    with only bc:read over HTTP too; a whole-project save still needs bc:write."""

    runtime, tokens = hub_factory(orientation=True)
    try:
        with running(runtime) as client:
            agenda = post(client, tokens["bob"], "agenda", {"person": "Sam"})
            assert agenda.status_code == 200, agenda.text
            weekly = post(client, tokens["bob"], "weekly", {})
            assert weekly.status_code == 200, weekly.text
            head,writes=hub_factory.remote.branch,len(hub_factory.remote.patch_payloads)
            for who in ('bob','alice'):
                refused=post(client,tokens[who],'project.save',{})
                assert refused.status_code==422
                assert refused.json()['error']['message']=='unsupported Brain operation'
                assert refused.json()['error']['fix']=="use bc_work for Nic's work, or memory.save for a new capture"
            assert hub_factory.remote.branch==head and len(hub_factory.remote.patch_payloads)==writes
    finally:
        runtime.close()


def test_with_orientation_off_none_of_the_operations_exist(hub_factory):
    runtime, tokens = hub_factory(orientation=False)
    try:
        with running(runtime) as client:
            for operation, body in (("orient", {}), ("next", {}), ("next.respond", {"surfacing_id": "x", "response": "no"})):
                refused = post(client, tokens["alice"], operation, body)
                assert refused.status_code == 422, operation
                assert refused.json()["error"]["message"] == "unsupported Brain operation"
    finally:
        runtime.close()
