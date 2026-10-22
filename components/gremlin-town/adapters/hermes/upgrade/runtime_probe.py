"""Isolated native-runtime contract probe; synthetic hub only, no network."""
from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import json
import os
import shutil
import socket
import sys
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path / "__init__.py", submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FixtureHub:
    def __call__(self, operation, payload, seconds):
        return self.call(operation, payload)

    def call(self, operation, payload, **kwargs):
        if operation in ("memory.list", "continuity.list"):
            return {"records": [], "notes": [], "items": [], "version": "fixture-only"}
        if operation == "memory.search":
            return {"matches": [{"record_id": "fixture", "title": "Practice recall", "body": "Practice recall",
                                 "kind": "fact", "state": "active"}]}
        if operation in ("memory.save", "review.respond"):
            return {"status": "accepted", "remote_observed": True, "fixture_only": True}
        raise RuntimeError("unsupported fixture operation")


def probe(home, source):
    # Importing native Hermes may create its own cache; HERMES_HOME is a disposable
    # directory supplied by the parent. All home inputs below are read-only.
    def refused(*args, **kwargs):
        raise RuntimeError("network disabled during upgrade probe")
    socket.socket.connect = refused
    socket.create_connection = refused
    sys.path.insert(0, str(source))
    rows = []
    def add(name, ok, detail):
        rows.append({"check": name, "status": "pass" if ok else "fail", "detail": detail})

    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from tools.registry import registry
    from tools.tool_search import ToolSearchConfig, assemble_tool_defs
    import yaml

    reliance = json.loads((Path(__file__).resolve().parent.parent / "HERMES-RELIANCE.json").read_text(encoding="utf-8"))
    expected_version = reliance["candidate"]["version"]
    add("hermes-version", importlib.metadata.version("hermes-agent") == expected_version,
        "installed candidate package version " + expected_version)

    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.send_cmd import register_send_subparser
    parser, subparsers, _ = build_top_level_parser()
    register_send_subparser(subparsers)
    send_args = parser.parse_args(["send", "--to", "discord:123", "--json", "--file", "-"])
    add("send-command-contract", send_args.to == "discord:123" and send_args.file == "-" and send_args.json,
        "native documented send parser accepts courier/mail arguments; no command dispatched")
    score_args = parser.parse_args(["-z", "fixture prompt", "--provider", "openai-codex", "--model", "gpt-6-luna", "--ignore-rules"])
    add("mail-command-contract", score_args.oneshot == "fixture prompt" and score_args.ignore_rules,
        "native documented one-shot parser accepts scorer arguments; no inference dispatched")
    from hermes_cli.default_soul import DEFAULT_SOUL_MD
    scorer_path = Path(__file__).parents[1] / "scripts" / "mail_scorer.py"
    scorer_spec = importlib.util.spec_from_file_location("scorer_default_pin", scorer_path)
    scorer_module = importlib.util.module_from_spec(scorer_spec)
    scorer_spec.loader.exec_module(scorer_module)
    add("mail-scorer-default-soul-pin", __import__("hashlib").sha256(DEFAULT_SOUL_MD.strip().encode()).hexdigest()
        == scorer_module.AUTO_SEEDED_SOUL_SHA256,
        "only pinned public auto-seeded SOUL allowed; --ignore-rules excludes context")

    installed = load("practice_gt", home / "plugins" / "gremlin-town")
    plugin = sys.modules["practice_gt.plugin"]
    manager = PluginManager()
    manifest = yaml.safe_load((home / "plugins" / "gremlin-town" / "plugin.yaml").read_text(encoding="utf-8"))
    native_manifest = PluginManifest(name="gremlin-town", version=str(manifest["version"]), source="user", kind="standalone",
                                     path=str(home / "plugins" / "gremlin-town"))
    # A noncredential fixture satisfies the loader's configuration gate. Sockets
    # are blocked and its endpoint is practice.invalid; no real token is read.
    os.environ["GREMLIN_TOWN_HUB_TOKEN"] = "practice-fixture-never-a-real-credential"
    try:
        manager._load_plugin(native_manifest)
    finally:
        os.environ.pop("GREMLIN_TOWN_HUB_TOKEN", None)
    loaded = [p for p in manager._plugins.values() if p.manifest.name == "gremlin-town"]
    add("native-plugin-loading", len(loaded) == 1 and not loaded[0].error,
        "candidate directory loader imports shipped entrypoint and registers without hub calls")
    context = PluginContext(PluginManifest(name="gremlin-town", source="user", path=str(home / "plugins" / "gremlin-town")), manager)
    plugin.register(context, client=FixtureHub())
    names = manifest["provides_tools"]
    direct = list(plugin.BRAIN_DIRECT_NAMES)
    expected_registered = direct + [plugin.MORE_TOOLS_NAME]
    registered = [n for n in expected_registered if registry.get_entry(n, scope=manager.scope_key) is not None]
    add("plugin", names == reliance["expected_tools"] and set(registered) == set(expected_registered),
        f"{len(registered)} compact registrations; {len(names)} original operations preserved by front")
    definitions = [{"type": "function", "function": {"name": d.name, "description": d.description,
                    "parameters": d.parameters}} for d in plugin.ALL_TOOL_DEFINITIONS if d.name in direct]
    definitions.append({"type": "function", "function": {"name": plugin.MORE_TOOLS_NAME, **plugin.MORE_TOOLS_SCHEMA}})
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    search = ToolSearchConfig.from_raw(config.get("tools", {}).get("tool_search"))
    assembled = assemble_tool_defs(definitions, config=search).tool_defs
    visible = {d["function"]["name"] for d in assembled}
    add("eager-tools", set(direct) <= visible and search.enabled == "off",
        "Plan B registers Brain plus one plugin front; no Hermes code patch")

    # Prove the proposed injection survives the native middleware and subscription
    # sanitizer, then fails Hermes's earlier allowed-name validation. No inference.
    from types import SimpleNamespace
    from hermes_cli import plugins as native_plugins
    from hermes_cli.middleware import apply_llm_request_middleware
    from agent.codex_runtime import _sanitize_consumer_codex_request
    from agent.turn_tool_validation import validate_tool_calls
    original_manager = native_plugins._delivery_manager
    native_plugins._delivery_manager = lambda: manager
    def inject(**kwargs):
        request = dict(kwargs["request"])
        request["tools"] = list(request.get("tools") or []) + [{"type": "function", **d["function"]} for d in definitions[:5]]
        return {"request": request, "source": "practice-plan-a"}
    handle = context.register_middleware("llm_request", inject)
    try:
        wire = _sanitize_consumer_codex_request(SimpleNamespace(_is_codex_backend=lambda: True),
            apply_llm_request_middleware({"model": "gpt-6", "tools": [], "input": []},
                                         provider="openai-codex", api_mode="codex_responses").payload)
        add("plan-a-codex-schemas", wire["tools"] == [{"type": "function", **d["function"]} for d in definitions[:5]],
            "native llm_request plus final consumer Codex sanitizer preserve injected schemas")
    finally:
        handle.dispose()
        native_plugins._delivery_manager = original_manager
    # Native documentation disclosure retains the admitted name and constraints.
    from tools.cronjob_tools import CRONJOB_SCHEMA
    docs = {}
    native_tool = {"type": "function", "function": CRONJOB_SCHEMA}
    docs_handle = context.register_middleware("llm_request", plugin._native_doc_middleware(docs))
    native_plugins._delivery_manager = lambda: manager
    try:
        shaped_request = apply_llm_request_middleware(
            {"model": "gpt-6", "tools": [native_tool], "input": []},
            provider="openai-codex", api_mode="codex_responses").payload
        shaped = _sanitize_consumer_codex_request(
            SimpleNamespace(_is_codex_backend=lambda: True), shaped_request)["tools"][0]
        shaped = shaped.get("function") or shaped
        front = plugin._more_tools((), FixtureHub(), None, native_docs=docs)
        help_result = json.loads(front({"action": "describe", "name": "cronjob_manage"}))
        add("native-doc-disclosure", shaped["name"] == "cronjob_manage"
            and shaped["parameters"]["required"] == CRONJOB_SCHEMA["parameters"]["required"]
            and help_result["result"] == CRONJOB_SCHEMA,
            "supported middleware preserves native name/input constraints; full original docs remain retrievable")
    finally:
        docs_handle.dispose()
        native_plugins._delivery_manager = original_manager
    full = [{"type": "function", "function": {"name": d.name, "description": d.description,
             "parameters": d.parameters}} for d in plugin.ALL_TOOL_DEFINITIONS]
    # Native deferral classification requires the original full registrations.
    # Register only in this scratch manager, with synthetic client and default mode.
    original_visibility = plugin._tool_visibility
    plugin._tool_visibility = lambda: "all"
    try:
        plugin.register(context, client=FixtureHub())
    finally:
        plugin._tool_visibility = original_visibility
    deferred = assemble_tool_defs(full, config=ToolSearchConfig.from_raw({"enabled": "on"})).tool_defs
    allowed = {d["function"]["name"] for d in deferred}
    fake = SimpleNamespace(valid_tool_names=allowed, _invalid_tool_retries=0,
        _uniquify_tool_call_ids=lambda calls: None, _repair_tool_call=lambda name: None,
        _buffer_vprint=lambda message: None,
        _build_assistant_message=lambda message, reason: {"role": "assistant", "content": ""})
    message = SimpleNamespace(tool_calls=[SimpleNamespace(id="fixture-call", function=SimpleNamespace(
        name="bc_memory_list", arguments="{}"))])
    messages = []
    verdict = validate_tool_calls(fake, message, "tool_calls", messages=messages,
                                  conversation_history=[], api_call_count=1, effective_task_id="fixture")
    add("plan-a-rejected", verdict.action == "continue" and "bc_memory_list" not in allowed
        and any("bc_memory_list" in m.get("content", "") for m in messages),
        "native turn validator rejects injected name; supported fallback selected")
    fake.valid_tool_names = visible
    message.tool_calls[0].function.name = "bc_memory_list"
    compact_verdict = validate_tool_calls(fake, message, "tool_calls", messages=[],
        conversation_history=[], api_call_count=1, effective_task_id="fixture")
    add("compact-native-validation", compact_verdict.action == "ok", "native turn validator admits direct Brain calls in Plan B")
    # Real registry dispatch of compact direct and front tools, against synthetic hub.
    direct_result = json.loads(registry.dispatch("bc_memory_list", {}, scope=manager.scope_key))
    rare_result = json.loads(registry.dispatch(plugin.MORE_TOOLS_NAME,
        {"action": "call", "name": "gremlin_town_review_respond", "arguments": {}}, scope=manager.scope_key))
    add("compact-direct-call", direct_result.get("ok") is True, "native registry direct Brain call executes")
    add("compact-rare-call", rare_result.get("ok") is True, "native registry front reaches original review handler")
    encoded = json.dumps(assembled, ensure_ascii=False, separators=(",", ":")).encode()
    second = assemble_tool_defs(definitions, config=search).tool_defs
    add("compact-schema-stability", encoded == json.dumps(second, ensure_ascii=False, separators=(",", ":")).encode(),
        "compact schema text byte-identical on repeated native assembly")
    sizes = [len(json.dumps(d, ensure_ascii=False)) for d in assembled]
    add("plugin-tool-budget", sum(sizes) <= 63000 and max(sizes) <= 12000,
        f"compact plugin schemas: {sum(sizes)} total characters; {max(sizes)} largest; full platform budget needs hub")
    add("manual-reset-config", config.get("session_reset", {}).get("mode") == "none", "manual /new selected; no timed reset")
    memory = load("practice_brain", home / "plugins" / "brain")
    providers = []
    class Collector:
        register_memory_provider = staticmethod(providers.append)
    memory.register(Collector())
    add("memory-interface", len(providers) == 1 and providers[0].name == "brain", "native MemoryProvider registration")
    implementation = sys.modules["practice_brain.provider"]
    brain = implementation.BrainProvider(hub=FixtureHub(), spawn=lambda *args, **kwargs: None)
    brain.initialize("fixture", hermes_home=str(Path(__import__('os').environ['HERMES_HOME'])))
    block = brain.prefetch("hello", session_id="fixture")
    add("brain-block-fixture", "Singapore time" in block and "Brain Home" in block, "synthetic prefetch, no canonical reads")
    usage = json.loads((home / "skills" / ".usage.json").read_text(encoding="utf-8"))
    skills = ("gremlin-town", "gremlin-town-review", "next-action", "calendar-time")
    add("skills", all((home / "skills" / n / "SKILL.md").is_file() and usage[n].get("pinned") is True for n in skills),
        "four packaged skills present and pinned")
    scratch = Path(os.environ["HERMES_HOME"])
    shutil.copytree(home / "skills", scratch / "skills", dirs_exist_ok=True)
    from tools.skills_tool import skill_view
    from tools.skill_usage import get_record
    views = [json.loads(skill_view(n, preprocess=False)) for n in skills]
    add("native-skill-loading", all("error" not in v for v in views) and all(get_record(n)["pinned"] for n in skills),
        "native skill_view reads four packaged skills, native usage reader observes pins; scratch only")
    # Check candidate scorer configuration through native platform resolution,
    # not merely the template's spelling. No inference or auth load.
    scorer_config = json.loads((Path(__file__).parent / "mail-scorer-config.yaml").read_text(encoding="utf-8"))
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import _select_tool_names
    sets = _get_platform_tools(scorer_config, "cli", include_default_mcp_servers=False)
    selected = _select_tool_names(list(sets), None, quiet_mode=True)
    add("mail-scorer-no-tools", not sets and not selected, "native candidate resolver selects zero tools for isolated scorer")
    add("review-skill", "review" in (home / "skills" / "gremlin-town-review" / "SKILL.md").read_text(encoding="utf-8").lower(),
        "packaged review instructions present")
    jobs = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
    add("scheduled-jobs", {j["name"] for j in jobs["jobs"]} >= {"own-memory-check", "important-mail"},
        "disabled practice job definitions; scheduler firing needs hub")
    add("models-config", config["model"]["provider"] == "openai-codex" and config["auxiliary"]["monitor"]["model"] == "gpt-6-luna",
        "provider/model declarations only; no login or inference")
    add("gateway-isolation", all(not x.get("enabled", False) for x in config["platforms"].values()), "all practice gateways off")
    # Exercise actual registered handlers, not an invented conversation result.
    for name, operation in (("bc_memory_save", "memory.save"), ("bc_memory_search", "memory.search"),
                            ("gremlin_town_review_respond", "review.respond")):
        output = json.loads(plugin._handler(FixtureHub(), operation)({}))
        add("fixture:" + name, output.get("ok") is True, "adapter handler against synthetic hub; real conversation needs hub/model")
    brain.shutdown()
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(probe(a.home, a.source)))
