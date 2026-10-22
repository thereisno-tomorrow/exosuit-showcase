"""W0 reply caps, frozen for W13/W16; characters of actual tool text, plus 15%.

The reproducible probes use only synthetic exhibition scenarios and in-process fakes.
Memory: 200 independent accepted/open tasks cycling the 12 audited accepted open
items; default list limit 20, search query 'permit' with default limit 10.
Requests: 11 fake requests with the full exhibition message as their objective;
Diablo's unfiltered handler selects 10 and returns a next cursor. No live services.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import anyio

FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "brain-core/tests/fixtures/work_model"
# Measured once in W0 on 30 Sep; caps are ceil(measured * 1.15).
BASELINE_REPLY_CHARS = {"bc_memory_list": 11_294, "bc_memory_search": 6_565, "gremlin_town_requests_list": 9_114}
REPLY_CAPS = {"bc_memory_list": 12_989, "bc_memory_search": 7_550, "gremlin_town_requests_list": 10_482, "bc_work": 2_000}
MEMORY_LIST_CAP = REPLY_CAPS["bc_memory_list"]
MEMORY_SEARCH_CAP = REPLY_CAPS["bc_memory_search"]
REQUEST_LIST_CAP = REPLY_CAPS["gremlin_town_requests_list"]
DOOR_REPLY_CAP = REPLY_CAPS["bc_work"]


def _fixture_helpers():
    spec = importlib.util.spec_from_file_location("w0_reply_fixture", FIXTURE_ROOT / "helpers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def memory_reply_texts() -> dict[str, str]:
    """Real provider/storage reader and MCP rendering over a seeded fake GitHub."""
    from brain_core.records import render_record
    from gremlin_town.contracts import Actor
    from gremlin_town.mcp.brain_runtime import BrainOperation
    from gremlin_town.mcp.brain_tools import build_brain_tools
    from tests.mcp.test_github_brain_provider import LANE_B, _adapter, _authorization, _provider

    fake = LANE_B.FakeGitHub()
    records = _fixture_helpers().generated_reply_records()
    assert len(records) == 200
    fake.seed({f"records/item/{r.created_at:%Y/%m}/{r.id}.md": render_record(r).encode("utf-8") for r in records})
    provider = _provider(fake, _adapter())

    class Runtime:
        async def list_memory(self, caller, selector, query):
            assert selector.kind == "personal" and query.limit == 20
            return await provider.list(_authorization(BrainOperation.MEMORY_LIST, "read"), query)

        async def search_memory(self, caller, selector, query):
            assert selector.kind == "personal" and query.limit == 10
            return await provider.search(_authorization(BrainOperation.MEMORY_SEARCH, "read"), query)

    async def allow():
        return None

    async def exercise():
        tools = {t.name: t for t in build_brain_tools(Runtime(), lambda: Actor("w0-fixture"),
                 scopes_provider=lambda: {"bc:read"}, authorize=allow)}
        result = {}
        for name, args, rows in (("bc_memory_list", {}, 20), ("bc_memory_search", {"query": "permit"}, 10)):
            reply = await tools[name].run(args)
            assert not reply.isError
            text = "".join(c.text for c in reply.content if c.type == "text")
            data = json.loads(text)
            assert len(data["items" if name == "bc_memory_list" else "matches"]) == rows
            assert data["truncated"]
            result[name] = text
        return result

    return anyio.run(exercise)


def request_reply_text(tmp_path: Path) -> str:
    """Real list shape via the Diablo handler and the existing in-process hub fake."""
    from adapters.hermes import plugin
    from tests.api.test_api import _auth, packet_json, real_hub

    hub = real_hub.__wrapped__(tmp_path)
    client, worker = next(hub)
    source = json.loads((FIXTURE_ROOT / "messages_30_sep.json").read_text(encoding="utf-8"))
    words = next(m["message"] for m in source["messages"] if m["id"] == "M01")
    try:
        for i in range(11):
            packet = packet_json()
            packet["objective"] = words
            response = client.post("/v1/requests", headers=_auth("alice"), json={
                "client_operation_id": f"w0-cap-{i:02}", "mode": "action_requested", "project": "demo",
                "recipient": "bob", "packet": packet,
            })
            assert response.status_code == 200, response.text
            worker.dispatch_once()

        class LocalHub:
            def call(self, operation, payload):
                assert operation == "requests.list" and payload == {"filters": {"limit": 10}}
                response = client.get("/v1/requests", headers=_auth("alice"), params=payload["filters"])
                assert response.status_code == 200, response.text
                return response.json()

        text = plugin._handler(LocalHub(), "requests.list")({})
        envelope = json.loads(text)
        assert envelope["ok"], envelope
        data = envelope["result"]
        assert len(data["items"]) == 10 and data["next_cursor"] is not None
        return text
    finally:
        client.close()
        hub.close()


def measure_reply_chars(tmp_path: Path) -> dict[str, int]:
    texts = memory_reply_texts()
    texts["gremlin_town_requests_list"] = request_reply_text(tmp_path)
    return {name: len(text) for name, text in texts.items()}
