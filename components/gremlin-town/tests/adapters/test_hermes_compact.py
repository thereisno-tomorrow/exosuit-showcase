"""Compact tool surface keeps the original plugin permission boundary."""
import json
import pytest
from adapters.hermes import plugin


class Client:
    def __init__(self):
        self.calls = []
    def call(self, operation, args):
        self.calls.append((operation, args))
        return {"fixture": True}


class Context:
    def __init__(self):
        self.tools = {}
    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = (schema, handler)
    def register_hook(self, *args):
        pass
    def register_middleware(self, kind, callback):
        self.middleware = (kind, callback)


def test_on_demand_setting_loads_the_complete_compact_plugin(tmp_path, monkeypatch):
    ctx, client = surface(tmp_path, monkeypatch)
    settings = json.loads(plugin.PLUGIN_SETTINGS_FILE.read_text())
    settings["native_tool_docs"] = "on-demand"
    plugin.PLUGIN_SETTINGS_FILE.write_text(json.dumps(settings))
    ctx = Context()
    plugin.register(ctx, client=client)
    assert set(ctx.tools) == {*plugin.BRAIN_DIRECT_NAMES, plugin.MORE_TOOLS_NAME}
    assert ctx.middleware[0] == "llm_request"


def test_native_docs_disclosure_preserves_schema_inputs_and_never_dispatches():
    import copy
    docs = {}
    shape = plugin._native_doc_middleware(docs)
    original = {"type": "function", "function": {"name": "memory", "description": "Full instructions",
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string", "description": "Description field"},
            "action": {"type": "string", "enum": ["add", "remove"], "description": "Allowed actions"}},
            "required": ["description"], "additionalProperties": False}}}
    baseline = copy.deepcopy(original)
    request = {"model": "fixture", "tools": [original, {"name": "terminal", "description": "Keep daily docs"}]}
    shaped = shape(request=request)["request"]
    assert original == baseline
    parameters = shaped["tools"][0]["function"]["parameters"]
    assert "description" in parameters["properties"]
    assert parameters["properties"]["action"]["enum"] == ["add", "remove"]
    assert parameters["required"] == ["description"] and parameters["additionalProperties"] is False
    assert shaped["tools"][1] == request["tools"][1]
    assert shape(request=request)["request"] == shaped
    client = Client()
    front = plugin._more_tools((), client, None, native_docs=docs)
    assert json.loads(front({"action": "describe", "name": "memory"}))["result"] == baseline["function"]
    assert json.loads(front({"action": "call", "name": "memory", "arguments": {}}))["ok"] is False
    assert not client.calls


def surface(tmp_path, monkeypatch, features=()):
    settings = tmp_path / "plugin-settings.json"
    settings.write_text(json.dumps({"enabled_optional_features": list(features), "tool_visibility": "brain-and-more"}))
    monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", settings)
    ctx, client = Context(), Client()
    plugin.register(ctx, client=client)
    return ctx, client


def test_compact_registers_only_five_brain_and_front(tmp_path, monkeypatch):
    ctx, _ = surface(tmp_path, monkeypatch)
    assert set(ctx.tools) == {*plugin.BRAIN_DIRECT_NAMES, plugin.MORE_TOOLS_NAME}


def test_rare_tools_can_be_listed_described_and_called(tmp_path, monkeypatch):
    ctx, client = surface(tmp_path, monkeypatch, ("review",))
    front = ctx.tools[plugin.MORE_TOOLS_NAME][1]
    listing = json.loads(front({"action": "list"}))
    assert "gremlin_town_review_respond" in {r["name"] for r in listing["result"]}
    described = json.loads(front({"action": "describe", "name": "gremlin_town_review_respond"}))
    assert "parameters" in described["result"]
    assert not client.calls
    assert json.loads(front({"action": "call", "name": "gremlin_town_review_respond", "arguments": {}}))["ok"]
    assert client.calls == [("review.respond", {})]


@pytest.mark.parametrize("name", ["gremlin_town_review_respond", "bc_work", "terminal", "gremlin_town_more_tools"])
def test_front_refuses_disabled_external_direct_and_recursive_tools(tmp_path, monkeypatch, name):
    ctx, client = surface(tmp_path, monkeypatch)
    out = json.loads(ctx.tools[plugin.MORE_TOOLS_NAME][1]({"action": "call", "name": name, "arguments": {}}))
    assert not out["ok"]
    assert not client.calls


@pytest.mark.parametrize("args", [[], {"action": "list", "token": "never"}, {"action": "call", "name": []},
    {"action": "call", "name": "gremlin_town_requests_list", "arguments": []}])
def test_front_rejects_bad_shapes_without_hub_call(tmp_path, monkeypatch, args):
    ctx, client = surface(tmp_path, monkeypatch)
    assert not json.loads(ctx.tools[plugin.MORE_TOOLS_NAME][1](args))["ok"]
    assert not client.calls


def test_unknown_mode_refused_before_registration(tmp_path, monkeypatch):
    ctx, _ = surface(tmp_path, monkeypatch)
    plugin.PLUGIN_SETTINGS_FILE.write_text('{"tool_visibility":"maybe"}')
    with pytest.raises(plugin.HubClientError):
        plugin.register(Context(), client=Client())


def test_front_preserves_original_protected_argument_refusal(tmp_path, monkeypatch):
    ctx, client = surface(tmp_path, monkeypatch)
    class Protected(Client):
        def call(self, op, args):
            if "token" in args:
                raise plugin.HubClientError("INVALID_ARGUMENT", "protected argument")
            return super().call(op, args)
    available = [d for d in plugin.ALL_TOOL_DEFINITIONS if d.name not in plugin.OPTIONAL_TOOL_FEATURES]
    client = Protected()
    out = plugin._more_tools(available, client, None)({"action": "call", "name": "gremlin_town_requests_list", "arguments": {"token": "never"}})
    assert json.loads(out)["error"]["code"] == "INVALID_ARGUMENT"
    assert not client.calls


def test_frequent_review_tool_can_stay_direct(tmp_path, monkeypatch):
    surface(tmp_path, monkeypatch, ("review",))
    plugin.PLUGIN_SETTINGS_FILE.write_text(json.dumps({"enabled_optional_features": ["review"],
        "tool_visibility": "brain-and-more",
        "always_ready_tools": [*plugin.BRAIN_DIRECT_NAMES, "gremlin_town_review_respond"]}))
    ctx = Context()
    plugin.register(ctx, client=Client())
    assert "gremlin_town_review_respond" in ctx.tools
    listing = json.loads(ctx.tools[plugin.MORE_TOOLS_NAME][1]({"action": "list"}))
    assert "gremlin_town_review_respond" not in {r["name"] for r in listing["result"]}


@pytest.mark.parametrize("names", [["bc_memory_list"], [*plugin.BRAIN_DIRECT_NAMES, "terminal"],
    [*plugin.BRAIN_DIRECT_NAMES, "gremlin_town_review_respond"], "all"])
def test_invalid_always_ready_selection_refused(tmp_path, monkeypatch, names):
    surface(tmp_path, monkeypatch)
    plugin.PLUGIN_SETTINGS_FILE.write_text(json.dumps({"tool_visibility": "brain-and-more", "always_ready_tools": names}))
    ctx = Context()
    with pytest.raises(plugin.HubClientError):
        plugin.register(ctx, client=Client())
    assert not ctx.tools
