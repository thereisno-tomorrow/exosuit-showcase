"""Diablo adapter package/HTTP proofs; no installed or native-session claims."""
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

import pytest
from adapters.hermes import hub_client, plugin
from adapters.hermes.hub_client import HubClientConfig, HubClientError, HubHTTPSClient
from gremlin_town.mcp.brain_tools import build_brain_tools
from tests.adapters.test_hermes import LoaderContext
from tests.adapters.test_cloud import Requester, Response
from tests.adapters.test_install_manifests import git_blob_sha256

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def native_shadow(tmp_path, monkeypatch, caplog):
    """Load shipped directory entrypoint; install inputs and transport are fake only."""
    from tests.adapters.test_hermes import FakeHubClient
    home = tmp_path / "fake-hermes-home"
    home.mkdir(mode=0o700)
    secrets = home / "secrets"
    secrets.mkdir(mode=0o700)
    key = secrets / "words-shadow.key"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.test")
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN", "fixture-only-token")
    monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_KEY_FILE", str(key))
    monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_OWNER_SENDER_ID", "fixture-nic")
    monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_REPLY_QUOTE_PREFIX", "fixture-quote\n")
    target = tmp_path / "native_plugin"
    target.mkdir()
    manifest = json.loads((ROOT / "adapters/hermes/install-manifest.json").read_text())
    for entry in manifest["files"]:
        shutil.copyfile(ROOT / entry["source"], target / Path(entry["source"]).name)
    name = "fixture_native_shadow"
    spec = importlib.util.spec_from_file_location(name, target / "__init__.py",
                                                 submodule_search_locations=[str(target)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        before_import = len(caplog.records)
        spec.loader.exec_module(module)
        # The configured key is deliberately absent at import: a read would fail and log.
        assert not any("words-shadow" in record.message for record in caplog.records[before_import:])
        key.write_bytes(b"fixture-shadow-key-one-32-bytes!!")
        key.chmod(0o600)
        implementation = sys.modules[name + ".plugin"]
        fake = FakeHubClient()
        configs = []
        def client(config, *, door):
            assert door is True
            configs.append(config)
            return fake
        monkeypatch.setattr(implementation, "HubHTTPSClient", client)
        def register(*, with_hook=True, broken_hook=False):
            ctx = LoaderContext()
            hooks = {}
            if with_hook:
                ctx.register_hook = lambda name, callback: hooks.update({name: callback})
            if broken_hook:
                def fail_hook(*args):
                    raise RuntimeError("PRIVATE registration exception")
                ctx.register_hook = fail_hook
            module.register(ctx)  # native signature: no injected observer or client
            handler = next(r["handler"] for r in ctx.registrations if r["name"] == "bc_work")
            return hooks.get("pre_llm_call"), handler
        yield home, key, implementation, fake, configs, register
    finally:
        # Monkeypatch deletion would restore these cached modules during its undo.
        for loaded in tuple(sys.modules):
            if loaded == name or loaded.startswith(name + "."):
                sys.modules.pop(loaded, None)


def test_native_shadow_consecutive_invocations_load_each_directory(tmp_path, caplog):
    """Exercise fixture teardown and monkeypatch undo before the next native import."""
    name = "fixture_native_shadow"
    outside_modules = (plugin, hub_client)
    previous_modules = None
    remaining = []
    for invocation in range(2):
        root = tmp_path / str(invocation)
        root.mkdir()
        with pytest.MonkeyPatch.context() as patches:
            fixture = native_shadow.__wrapped__(root, patches, caplog)
            try:
                home, key, implementation, fake, configs, register = next(fixture)
                modules = (sys.modules[name], implementation, sys.modules[name + ".hub_client"])
                for module, filename in zip(modules, ("__init__.py", "plugin.py", "hub_client.py")):
                    assert Path(module.__file__).resolve() == (root / "native_plugin" / filename).resolve()
                assert modules[0]._register is implementation.register
                assert implementation.HubClientConfig is modules[2].HubClientConfig
                if previous_modules is not None:
                    assert all(current is not previous for current, previous in zip(modules, previous_modules))
                previous_modules = modules
            finally:
                fixture.close()
        remaining.append([loaded for loaded in sys.modules if loaded == name or loaded.startswith(name + ".")])
        assert sys.modules[plugin.__name__] is outside_modules[0]
        assert sys.modules[hub_client.__name__] is outside_modules[1]
    assert remaining == [[], []]


def test_native_entrypoint_observes_keyed_turn_and_work_without_filling(native_shadow):
    import hashlib
    import hmac
    home, key, implementation, fake, configs, register = native_shadow
    assert not (home / "gt-words-shadow.jsonl").exists()  # import reads nothing
    hook, handler = register()
    assert configs[0].token == "fixture-only-token"
    text = "Pay fixture venue invoice"
    payload = {"message": text, "said_by": "owner", "came_by": "direct", "moves": []}
    original = deepcopy(payload)
    for sender, parent in (("fixture-nic", None), ("fixture-other", None), ("fixture-nic", "parent")):
        message = "fixture-quote\n" + text
        history = [{"role": "user", "content": message}]
        assert hook(session_id="s", turn_id="t", user_message=message, platform="telegram",
                    sender_id=sender, parent_session_id=parent, conversation_history=history) is None
        result = json.loads(handler(payload, session_id="s", turn_id="t"))
        assert result["result"]["operation"] == "work"
        assert history == [{"role": "user", "content": message}]
    assert payload == original and fake.calls == [("work", original)] * 3
    log = (home / "gt-words-shadow.jsonl").read_text()
    rows = [json.loads(line) for line in log.splitlines()]
    expected = lambda value: hmac.new(key.read_bytes(), value.encode(), hashlib.sha256).hexdigest()
    turns = [r for r in rows if r["phase"] == "turn"]
    work = [r for r in rows if r["phase"] == "work"]
    assert len(turns) == len(work) == 3
    assert all(r["message_hash"] == expected("fixture-quote\n" + text) and
               r["words_hash"] == expected(text) and r["platform"] == "telegram" for r in turns)
    assert all(r["door_message_hash"] == expected(text) and r["matches_words"] and
               not r["matches_message"] and r["observed"] for r in work)
    assert sum(r["owner_sender_mismatch"] for r in work) == 2
    assert text not in log and "fixture-quote" not in log and "fixture-nic" not in log
    key.write_bytes(b"fixture-shadow-key-two-32-bytes!!")
    hook2, _ = register()
    assert hook2(user_message=text) is None
    last = json.loads((home / "gt-words-shadow.jsonl").read_text().splitlines()[-1])
    assert last["message_hash"] == expected(text) and last["message_hash"] != turns[0]["words_hash"]


@pytest.mark.parametrize("invalid", ["missing-key", "missing-owner", "blank-owner", "long-owner",
                                     "long-prefix", "short-key", "large-key", "relative-key"])
def test_native_observer_bad_install_inputs_leave_turn_and_work_untouched(native_shadow, monkeypatch, caplog, invalid):
    home, key, implementation, fake, configs, register = native_shadow
    if invalid == "missing-key":
        monkeypatch.delenv("GREMLIN_TOWN_WORDS_SHADOW_KEY_FILE")
    elif invalid == "missing-owner":
        monkeypatch.delenv("GREMLIN_TOWN_WORDS_SHADOW_OWNER_SENDER_ID")
    elif invalid == "blank-owner":
        monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_OWNER_SENDER_ID", " ")
    elif invalid == "long-owner":
        monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_OWNER_SENDER_ID", "f" * 257)
    elif invalid == "long-prefix":
        monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_REPLY_QUOTE_PREFIX", "f" * 4097)
    elif invalid == "short-key":
        key.write_bytes(b"fake-short")
    elif invalid == "large-key":
        key.write_bytes(b"f" * 4097)
    else:
        monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_KEY_FILE", "private-message.key")
    hook, handler = register()
    assert hook(user_message="PRIVATE text") is None
    payload = {"message": "PRIVATE text", "said_by": "owner"}
    assert json.loads(handler(payload))["result"]["operation"] == "work"
    assert fake.calls == [("work", payload)]
    assert not (home / "gt-words-shadow.jsonl").exists()
    assert "words-shadow unavailable" in caplog.text
    assert "PRIVATE" not in caplog.text and "private-message" not in caplog.text


def test_native_observer_append_errors_never_affect_turn_or_tool(native_shadow, monkeypatch, caplog):
    home, key, implementation, fake, configs, register = native_shadow
    hook, handler = register()
    def broken(entry):
        raise OSError("PRIVATE exception text")
    monkeypatch.setattr(implementation.WordsShadow, "_append", lambda self, entry: broken(entry))
    assert hook(session_id="s", turn_id="t", user_message="PRIVATE words") is None
    payload = {"message": "PRIVATE words", "said_by": "owner"}
    assert json.loads(handler(payload, session_id="s", turn_id="t"))["result"]["operation"] == "work"
    assert fake.calls == [("work", payload)]
    assert "words-shadow observation failed" in caplog.text
    assert "words-shadow door comparison failed" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("unsafe", ["outside-home", "directory-key", "linked-key", "linked-log", "directory-log"])
def test_native_observer_refuses_unsafe_files_without_changing_work(native_shadow, monkeypatch, caplog, unsafe):
    home, key, implementation, fake, configs, register = native_shadow
    sentinel = home.parent / "fixture-sentinel"
    sentinel.write_bytes(b"fixture-sentinel-must-stay-unchanged-32-bytes")
    log = home / "gt-words-shadow.jsonl"
    if unsafe == "outside-home":
        monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_KEY_FILE", str(sentinel))
    elif unsafe == "directory-key":
        monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_KEY_FILE", str(key.parent))
    elif unsafe == "linked-key":
        linked = key.parent / "linked-key"
        os.link(key, linked)
        monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_KEY_FILE", str(linked))
    elif unsafe == "linked-log":
        os.link(sentinel, log)
    else:
        log.mkdir()
    hook, handler = register()
    assert hook(session_id="s", turn_id="t", user_message="PRIVATE words") is None
    payload = {"message": "PRIVATE words", "said_by": "owner"}
    assert json.loads(handler(payload, session_id="s", turn_id="t"))["result"]["operation"] == "work"
    assert fake.calls == [("work", payload)]
    assert sentinel.read_bytes() == b"fixture-sentinel-must-stay-unchanged-32-bytes"
    assert "words-shadow" in caplog.text and "PRIVATE" not in caplog.text


def test_native_observer_caps_memory_and_does_not_log_untrusted_metadata(native_shadow, caplog):
    home, key, implementation, fake, configs, register = native_shadow
    hook, handler = register()
    observer = hook.__self__
    for turn in range(70):
        hook(session_id="s", turn_id=str(turn), user_message="PRIVATE words",
             platform="PRIVATE text\nnot a platform")
    assert len(observer._turns) == 64
    assert ("s", "0") not in observer._turns and ("s", "69") in observer._turns
    hook(session_id="s" * 257, turn_id="t", user_message="PRIVATE words")
    assert len(observer._turns) == 64
    handler({"message": "PRIVATE words", "said_by": "owner"}, session_id="s", turn_id="0")
    rows = [json.loads(line) for line in (home / "gt-words-shadow.jsonl").read_text().splitlines()]
    assert all(r["platform"] == "unknown" for r in rows if r["phase"] == "turn")
    assert rows[-1]["observed"] is False
    assert "PRIVATE" not in (home / "gt-words-shadow.jsonl").read_text() + caplog.text


def test_native_observer_cannot_be_reconfigured_by_model_payload(native_shadow, monkeypatch):
    home, key, implementation, fake, configs, register = native_shadow
    hook, handler = register()
    # Protected inputs are captured at registration; neither later env nor tool fields replace them.
    monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_OWNER_SENDER_ID", "fixture-other")
    monkeypatch.setenv("GREMLIN_TOWN_WORDS_SHADOW_KEY_FILE", "private-invalid-path")
    hook(session_id="s", turn_id="t", user_message="Pay fixture venue invoice", sender_id="fixture-nic")
    payload = {"message": "Pay fixture venue invoice", "said_by": "owner", "owner_sender_id": "fixture-other",
               "key_file": "private-invalid-path", "reply_quote_prefix": "Pay "}
    original = deepcopy(payload)
    handler(payload, session_id="s", turn_id="t")
    assert payload == original and fake.calls == [("work", original)]
    row = json.loads((home / "gt-words-shadow.jsonl").read_text().splitlines()[-1])
    assert row["owner_sender"] and not row["owner_sender_mismatch"] and row["matches_words"]
    assert "private-invalid-path" not in (home / "gt-words-shadow.jsonl").read_text()


def test_native_observer_no_prefix_means_no_stripping(native_shadow, monkeypatch):
    home, key, implementation, fake, configs, register = native_shadow
    monkeypatch.delenv("GREMLIN_TOWN_WORDS_SHADOW_REPLY_QUOTE_PREFIX")
    hook, handler = register()
    hook(session_id="s", turn_id="t", user_message="fixture-quote\nPay fixture venue invoice")
    handler({"message": "Pay fixture venue invoice"}, session_id="s", turn_id="t")
    row = json.loads((home / "gt-words-shadow.jsonl").read_text().splitlines()[-1])
    assert row["message_hash"] == row["words_hash"] and not row["matches_words"]


@pytest.mark.parametrize("broken", [False, True])
def test_native_observer_missing_hook_keeps_tools_available(native_shadow, caplog, broken):
    home, key, implementation, fake, configs, register = native_shadow
    hook, handler = register(with_hook=False, broken_hook=broken)
    assert hook is None
    payload = {"message": "PRIVATE words", "said_by": "owner"}
    assert json.loads(handler(payload))["result"]["operation"] == "work"
    assert fake.calls == [("work", payload)] and not (home / "gt-words-shadow.jsonl").exists()
    assert f"Hermes hook registration {'failed' if broken else 'missing'}" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("boundary", ["key-symlink", "home-symlink", "key-replaced"])
def test_native_observer_checks_link_and_opened_file_boundaries(native_shadow, monkeypatch, caplog, boundary):
    import stat
    home, key, implementation, fake, configs, register = native_shadow
    real_lstat, real_fstat = Path.lstat, os.fstat
    def reported_link(path, *args, **kwargs):
        metadata = real_lstat(path, *args, **kwargs)
        if path == (home if boundary == "home-symlink" else key):
            values = list(metadata)
            values[0] = stat.S_IFLNK | 0o700
            return os.stat_result(values)
        return metadata
    def replaced(descriptor):
        values = list(real_fstat(descriptor))
        values[1] += 1
        return os.stat_result(values)
    if boundary == "key-replaced":
        monkeypatch.setattr(os, "fstat", replaced)
    else:
        # Emulate lstat's link result on Windows without needing symlink privileges.
        monkeypatch.setattr(Path, "lstat", reported_link)
    hook, handler = register()
    assert hook(user_message="PRIVATE words") is None
    assert json.loads(handler({"message": "PRIVATE words"}))["result"]["operation"] == "work"
    assert not (home / "gt-words-shadow.jsonl").exists()
    assert "words-shadow unavailable" in caplog.text and "PRIVATE" not in caplog.text


def test_shadow_posix_custody_checks_remain_strict_on_windows(native_shadow, monkeypatch):
    import stat
    from types import SimpleNamespace
    home, key, implementation, fake, configs, register = native_shadow
    monkeypatch.setattr(implementation, "os", SimpleNamespace(name="posix", geteuid=lambda: 1234))
    valid = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=1234)
    implementation._shadow_file(valid)
    for changes in ({"st_mode": stat.S_IFREG | 0o644}, {"st_uid": 4321}, {"st_nlink": 2}):
        with pytest.raises(ValueError):
            implementation._shadow_file(SimpleNamespace(**(vars(valid) | changes)))
    real_lstat = Path.lstat
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1234)
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: metadata if path == home else real_lstat(path, *a, **kw))
    implementation._shadow_directory(home, secret=True)
    for changes in ({"st_mode": stat.S_IFDIR | 0o777}, {"st_uid": 4321}):
        monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: SimpleNamespace(**(vars(metadata) | changes))
                            if path == home else real_lstat(path, *a, **kw))
        with pytest.raises(ValueError):
            implementation._shadow_directory(home)
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=1234)
                        if path == home else real_lstat(path, *a, **kw))
    with pytest.raises(ValueError):
        implementation._shadow_directory(home, secret=True)


def server_tools():
    async def never():
        raise AssertionError("schema inspection cannot authorize")
    return build_brain_tools(None, lambda: None, scopes_provider=lambda: (), authorize=never, door=True)


def calendar_tools():
    """bc_calendar_remove: the hub registers it only while calendar entries are on (N8)."""
    async def never():
        raise AssertionError("schema inspection cannot authorize")
    tools = build_brain_tools(None, lambda: None, scopes_provider=lambda: (), authorize=never, door=True, calendar_removal=True)
    return [tool for tool in tools if tool.name == "bc_calendar_remove"]


def sample(operation):
    common = {"operation_id":"damien-proof-1", "scope":"personal"}
    memory_source = {"said_by":"owner", "came_by":"direct", "words":"Prepare Friday review."}
    return {
        "context.retrieve": {"project_id":"vault_gtbc_plans", "intent":"GTBC proof"},
        "memory.save": {**common, **memory_source, "kind":"item", "level":"accepted", "title":"Friday review", "body":"Prepare Friday review."},
        "memory.change": {**common, **memory_source, "record_id":"brn_20260923_120000_aabb", "expected_sha256":"a"*64, "state":"waiting"},
        "memory.read": {"scope":"personal", "record_id":"brn_20260923_120000_aabb"},
        "memory.search": {"query":"Friday"},
        "memory.list": {"scope":"personal"},
        "decision.record": {**common, "title":"Friday review", "choice":"Review Friday", "because":"One window", "forecloses":"Daily reviews", "governing_scope":"Personal routine", "encoded_in":"Weekly routine", "completeness_check":"Timing and owner explicit"},
        "continuity.start": {**common, "session_id":"damien-session", "intent":"Friday review", "source_references":[]},
        "continuity.append": {**common, "session_id":"damien-session", "event":"next_step", "value":"Prepare agenda"},
        "continuity.recover": {},
        "continuity.list": {},
        "continuity.close": {**common, "session_id":"damien-session"},
        "orient": {},
        "next": {"signal":"pc_on"},
        "next.respond": {"surfacing_id":"brn_20260926_120000_aabb", "response":"yes", "first_step":"Open the deck"},
        "agenda": {"person":"Sam"},
        "weekly": {},
        "project.save": {**common, "goal_title":"Move house", "goal_body":"Out by the 30th", "said_by":"owner",
                         "came_by":"direct", "words":"yes, save the plan",
                         "steps":[{"title":"Buy boxes", "body":"Twenty", "block":"errands", "rough_minutes":45,
                                   "first_step":"Check the shop hours"}]},
        "calendar.remove": {**common, "record_id":"brn_20260923_120000_aabb", "said_by":"owner", "came_by":"direct", "words":"yes"},
        "work": {"operation_id": "fixture-work", "message": "Pay venue invoice", "said_by": "owner",
                 "came_by": "direct", "moves": [{"move": "new", "title": "Pay venue invoice"}]},
    }[operation]


def orientation_tools():
    async def never():
        raise AssertionError("schema inspection cannot authorize")
    from gremlin_town.review.orient import OrientationContext, OrientationSources
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.settings import OrientationSettings, ReviewSettings
    from gremlin_town.review.why_log import WhyLog
    from gremlin_town.mcp.brain_tools import build_orientation_tools

    why_log = WhyLog(":memory:")
    context = OrientationContext(
        orientation_settings=OrientationSettings.from_table({"enabled": True}, review_enabled=True),
        review_settings=ReviewSettings.from_table({"enabled": True, "why_log_file": ":memory:"}),
        why_log=why_log,
        sources=OrientationSources(review=ReviewSources()),
    )
    return build_orientation_tools(context, lambda: None, scopes_provider=lambda: (), authorize=never)


def test_exact_schemas_and_23_tools_preserve_original_gt_and_orientation_definitions(monkeypatch, tmp_path):
    """bc_orient/bc_next/bc_next_respond's schemas must still exactly mirror build_orientation_tools
    even though a default install (read below) does not register them - OPTIONAL_TOOL_FEATURES
    gates registration, not the static schema tuples this equality checks. With "orientation"
    on, Diablo has 28 tools: W15 adds the door; the step F pair became three, and bc_agenda,
    bc_weekly joined them (release ciel-03, PRD §4.8-§4.10)."""

    expected = {t.name:t.parameters for t in server_tools()}
    expected.update({t.name: t.parameters for t in orientation_tools()})
    expected.update({t.name: t.parameters for t in calendar_tools()})
    assert {t.name:t.parameters for t in plugin.BRAIN_TOOL_DEFINITIONS} == expected
    ctx=LoaderContext()
    plugin.register(ctx,client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-hermes-token"),client=Requester()))
    assert len(ctx.registrations)==23
    assert [r["name"] for r in ctx.registrations[:10]] == [t.name for t in plugin.TOOL_DEFINITIONS]
    assert tuple(t.operation for t in plugin.TOOL_DEFINITIONS)==hub_client.TEN_OPERATIONS
    assert all(not r["override"] for r in ctx.registrations)
    assert len({r["name"] for r in ctx.registrations})==23
    assert set(plugin.OPTIONAL_TOOL_FEATURES).isdisjoint({r["name"] for r in ctx.registrations})
    yaml=(ROOT/"adapters/hermes/plugin.yaml").read_text()
    listed=[line.strip()[2:] for line in yaml.splitlines() if line.strip().startswith("- ")]
    assert set(t.name for t in plugin.ALL_TOOL_DEFINITIONS).issubset(listed)
    assert [t.name for t in orientation_tools()] == ["bc_orient","bc_next","bc_next_respond","bc_agenda","bc_weekly"]
    settings=tmp_path/"plugin-settings.json"
    settings.write_text(json.dumps({"enabled_optional_features":["orientation"]}),encoding="utf-8")
    monkeypatch.setattr(plugin,"PLUGIN_SETTINGS_FILE",settings)
    on=LoaderContext()
    plugin.register(on,client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-hermes-token"),client=Requester()))
    assert len(on.registrations)==28
    assert [r["name"] for r in on.registrations if r["name"] in plugin.OPTIONAL_TOOL_FEATURES]==[
        "bc_orient","bc_next","bc_next_respond","bc_agenda","bc_weekly"]


@pytest.mark.parametrize("operation", hub_client.BRAIN_OPERATIONS)
def test_all_brain_handlers_post_exact_flat_input_and_own_bearer(operation, monkeypatch, tmp_path):
    body=sample(operation); before=deepcopy(body)
    tool=next(t for t in plugin.BRAIN_TOOL_DEFINITIONS if t.operation==operation)
    required_feature = plugin.OPTIONAL_TOOL_FEATURES.get(tool.name)
    if required_feature is not None:
        # orient/next/next.respond (`[orientation]`) only register when enabled - every other
        # BRAIN_OPERATIONS tool is always-on, so this is a no-op for the rest of this
        # parametrization.
        settings = tmp_path / "plugin-settings.json"
        settings.write_text(json.dumps({"enabled_optional_features": [required_feature]}), encoding="utf-8")
        monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", settings)
    request=Requester()
    ctx=LoaderContext()
    plugin.register(ctx,client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-hermes-token"),client=request))
    handler=next(r["handler"] for r in ctx.registrations if r["name"]==tool.name)
    assert json.loads(handler(body, runtime_context="ignored"))["ok"] is True
    method,url,kwargs=request.calls[0]
    assert method=="POST" and url=="https://hub.test/v1/brain/"+operation
    assert kwargs["json"]==before and body==before
    assert kwargs["headers"]["Authorization"]=="Bearer fixture-hermes-token"
    assert kwargs["headers"]["Content-Type"]=="application/json"
    assert "params" not in kwargs


@pytest.mark.parametrize("status,code,retryable",[(401,"UNAUTHORIZED",False),(403,"ACCESS_DENIED",False),(422,"brain.invalid_argument",False),(409,"brain.conflict",False),(503,"brain.capability.unavailable",True)])
def test_structured_errors_survive_without_retry(status,code,retryable):
    request=Requester([Response(status,{"error":{"code":code,"message":"fixture failure","retryable":retryable}})])
    client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-token"),client=request)
    with pytest.raises(HubClientError) as failure:
        client.call("memory.save",sample("memory.save"))
    assert failure.value.code==code and failure.value.retryable is retryable
    assert failure.value.status_code==status and len(request.calls)==1


def test_lost_reply_does_not_regenerate_or_retry_input():
    class Lost(Requester):
        def request(self,method,url,**kwargs):
            self.calls.append((method,url,deepcopy(kwargs)))
            if len(self.calls)==1:
                raise TimeoutError("fixture-token must not leak")
            return Response()
    request=Lost();client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-token"),client=request)
    body=sample("memory.save");before=deepcopy(body)
    with pytest.raises(HubClientError) as failure:client.call("memory.save",body)
    assert failure.value.retryable and len(request.calls)==1 and body==before
    assert "fixture-token" not in json.dumps(failure.value.as_dict())
    client.call("memory.save",body)
    assert request.calls[0]==request.calls[1]


def test_damien_write_timeout_says_not_saved():
    class TimingOut(Requester):
        def request(self,method,url,**kwargs):
            self.calls.append((method,url,kwargs))
            raise TimeoutError("fixture timeout")
    request=TimingOut()
    client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-token",timeout=20.0),client=request)
    with pytest.raises(HubClientError) as failure:
        client.call("memory.save",sample("memory.save"))
    assert failure.value.code=="TIMEOUT" and failure.value.retryable is True
    assert "Not saved" in str(failure.value) and "https://hub.test" in str(failure.value)
    assert request.calls[0][2]["timeout"]==60.0

    reads=TimingOut()
    read_client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-token",timeout=20.0),client=reads)
    with pytest.raises(HubClientError) as read_failure:
        read_client.call("memory.read",{"scope":"personal","record_id":"brn_20260923_120000_aabb"})
    assert read_failure.value.code=="TIMEOUT" and "Not saved" not in str(read_failure.value)
    assert reads.calls[0][2]["timeout"]==20.0


@pytest.mark.parametrize("field",["actor","actor_id","token","endpoint","base_url","authorization"])
def test_protected_arguments_denied_before_transport(field):
    request=Requester();client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-token"),client=request)
    with pytest.raises(HubClientError):client.call("memory.save",{**sample("memory.save"),field:"other"})
    assert request.calls==[]


@pytest.mark.parametrize("operation",sorted(hub_client.BRAIN_MUTATING_OPERATIONS))
def test_all_mutations_require_stable_operation_id(operation):
    request=Requester();client=HubHTTPSClient(HubClientConfig("https://hub.test","fixture-token"),client=request)
    body=sample(operation);body.pop("operation_id")
    with pytest.raises(HubClientError):client.call(operation,body)
    assert request.calls==[]


def test_missing_hermes_configuration_never_reads_grok_fallback(monkeypatch,tmp_path):
    monkeypatch.setattr(plugin,"SETTINGS_FILE",tmp_path/"absent.json")
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN",raising=False)
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN_FILE",raising=False)
    def forbidden(path):raise AssertionError("fallback token read")
    monkeypatch.setattr(hub_client,"_read_protected_token_file",forbidden)
    with pytest.raises(HubClientError):plugin._hermes_configuration()


def test_existing_hermes_token_file_used_without_enrollment(monkeypatch):
    for key in ("GREMLIN_TOWN_HUB_TOKEN","GREMLIN_TOWN_HUB_TOKEN_FILE","GREMLIN_TOWN_HUB_URL"):
        monkeypatch.delenv(key,raising=False)
    observed=[]
    monkeypatch.setattr(hub_client,"_read_protected_token_file",lambda p:observed.append(p) or "hermes-fixture")
    config=plugin._hermes_configuration()
    assert observed==[Path("/opt/data/secrets/gremlin-town.token")]
    assert config.token=="hermes-fixture"


def test_credential_redirect_refused():
    assert hub_client._NoRedirect().redirect_request(None,None,307,"redirect",{},"https://elsewhere.test") is None


def test_install_manifest_hashes_and_isolated_stdlib_package(tmp_path):
    manifest=json.loads((ROOT/"adapters/hermes/install-manifest.json").read_text())
    assert manifest["participant_id"]=="hermes-wa"
    assert manifest["network_or_install_effects"] is False
    target=tmp_path/"hermes_candidate";target.mkdir()
    for entry in manifest["files"]+manifest["skill_files"]:
        raw=(ROOT/entry["source"]).read_bytes()
        assert git_blob_sha256(ROOT/entry["source"])==entry["sha256"]
        if entry in manifest["files"]:(target/Path(entry["source"]).name).write_bytes(raw)
    # The copied plugin-settings.json is the real, unmodified one (enabled_optional_features:
    # []), so the isolated register() call below registers everything except the four
    # OPTIONAL_TOOL_FEATURES tools - same default-off behavior test_hermes.py checks in-process.
    expected_tool_count=len(plugin.ALL_TOOL_DEFINITIONS)-len(plugin.OPTIONAL_TOOL_FEATURES)
    script=f"""
import importlib.util,sys
from pathlib import Path
path=Path(sys.argv[1]);spec=importlib.util.spec_from_file_location('isolated_hermes',path/'__init__.py',submodule_search_locations=[str(path)])
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
class Context:
 def __init__(self):self.tools=[]
 def register_tool(self,*args,**kwargs):self.tools.append((args,kwargs))
c=Context();m.register(c);assert len(c.tools)=={expected_tool_count}
assert not any(n=='gremlin_town' or n.startswith('brain_core') for n in sys.modules)
print('{expected_tool_count} tools; standalone local loader only')
"""
    env=dict(os.environ,GREMLIN_TOWN_HUB_URL="https://hub.test",GREMLIN_TOWN_HUB_TOKEN="isolated-fixture-token",PYTHONDONTWRITEBYTECODE="1")
    result=subprocess.run([sys.executable,"-I","-S","-B","-c",script,str(target)],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert f"{expected_tool_count} tools" in result.stdout


@pytest.fixture
def damien_http(tmp_path,monkeypatch):
    from gremlin_town.mcp import brain_assembly
    from gremlin_town.mcp.brain_assembly import build_brain_runtime
    from gremlin_town.mcp.brain_runtime import BrainOperation
    from gremlin_town.runtime import build_runtime
    from gremlin_town.transport import FakeTransport
    from tests.integration.test_mcp_runtime import mcp_config
    from tests.integration.test_brain_agent_http import running
    from tests.mcp.test_brain_composition import _settings,LANE_B,FIRST_TIME
    config,tokens=mcp_config(tmp_path)
    participants=tmp_path/'participants.toml'
    participants.write_text(participants.read_text().replace('alice','hermes-wa'))
    settings=_settings(tmp_path);policy=json.loads(settings.policy_file.read_text())
    policy['subject_scopes']={'hermes-wa':['bc:read','bc:write'],'bob':['bc:read']}
    owner=policy['owners'][0];owner['readers']=['hermes-wa','bob'];owner['writers']=['hermes-wa']
    owner['operations']=[op.value for op in BrainOperation if op!=BrainOperation.CONTEXT_RETRIEVE]
    settings.policy_file.write_text(json.dumps(policy))
    with config.open('a') as handle:
        handle.write('\n[brain_core]\nenabled = true\n')
        for field in fields(settings):
            if getattr(settings,field.name) is not None:  # Unset optional credential fields stay absent.
                value = getattr(settings,field.name)
                encoded = json.dumps(value) if isinstance(value, bool) else json.dumps(str(value).replace(chr(92), "/"))
                handle.write(f'{field.name} = {encoded}\n')
    remote=LANE_B.FakeGitHub()
    from tests.mcp.test_github_brain_provider import _HistoryTransport
    monkeypatch.setattr(brain_assembly,'build_brain_runtime',lambda configured,**kwargs:build_brain_runtime(configured,github_transport=_HistoryTransport(remote),operation_clock=lambda:FIRST_TIME,**kwargs))
    runtime=build_runtime(config,provider=FakeTransport(),start_worker=False)
    with running(runtime) as loopback:
        class LoopbackOnly:
            def request(self,method,url,**kwargs):
                assert urlsplit(url).netloc=='fixture.test'
                return loopback.request(method,urlsplit(url).path,**kwargs)
        client=HubHTTPSClient(HubClientConfig('https://fixture.test',tokens['alice']),client=LoopbackOnly())
        yield client,loopback,tokens,remote,runtime


def test_real_http_damien_identity_shared_read_retry_conflict_and_revocation(damien_http):
    from brain_core.github_storage import GitHubBrainHomeStorage
    from tests.mcp.test_brain_composition import LANE_B
    client,loopback,tokens,remote,runtime=damien_http
    body=sample('memory.save');saved=client.call('memory.save',body)
    assert saved['status']=='accepted' and saved['word']=='task'
    other=loopback.post('/v1/brain/memory.read',headers={'Authorization':'Bearer '+tokens['bob']},json={'scope':'personal','record_id':saved['record_id']})
    assert other.status_code==200 and other.json()['state']=='open'
    assert client.call('memory.save',body)['status']=='duplicate'
    with pytest.raises(HubClientError) as conflict:client.call('memory.save',{**body,'body':'Changed input'})
    assert conflict.value.status_code==409
    assert client.call('memory.read',{'scope':'personal','record_id':saved['record_id']})['state']=='open'
    assert len(client.call('memory.search',{'query':'Friday'})['matches'])==1
    for operation in ('continuity.start','continuity.append','continuity.close','decision.record'):
        payload=sample(operation);payload['operation_id']='damien-'+operation.replace('.','-')
        if operation=='continuity.close':
            assert 'intent' in client.call('continuity.recover',{})
        result=client.call(operation,payload)
        assert 'error' not in result
    with pytest.raises(HubClientError):client.call('continuity.recover',{})
    for denied in (
        {**body,'scope':'cross-project','operation_id':'damien-denied-scope'},
        {**body,'kind':'preference','said_by':'agent','came_by':'agent_guess','speaker':'hermes-wa','operation_id':'damien-denied-preference'},
    ):
        with pytest.raises(HubClientError) as refusal:client.call('memory.save',denied)
        assert refusal.value.status_code in {403,422}
    # An owner-worded preference (said_by owner, came_by direct, with words) is now
    # accepted on the hosted path (S6, ROA-17) - only a guessed preference is refused above.
    owner_preference=client.call('memory.save',{**body,'kind':'preference','operation_id':'damien-accepted-preference'})
    assert owner_preference['status']=='accepted' and owner_preference['state']=='active'
    assert client.call('memory.list',{'scope':'personal'})['items'][0]['record_id']==saved['record_id']
    storage=GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY,branch=LANE_B.BRANCH,token=LANE_B.TOKEN,transport=remote)
    records=storage.snapshot_records(kinds=frozenset({'item','decision','continuity'})).records
    assert records
    for item in records:
        if item.record.kind=='item':
            # v2's structured source.recorded_by carries attribution; the evidence excerpt
            # is instead the exact quoted words (R2), so it is not asked to also carry it.
            assert item.record.source['recorded_by']=='hermes-wa'
        else:
            assert any(e.actor=='hermes-wa' and '"executing_agent":"hermes-wa"' in e.excerpt for e in item.record.evidence)
    runtime.policy.set_enabled('hermes-wa',False)
    with pytest.raises(HubClientError) as revoked:client.call('memory.search',{'query':'Friday'})
    assert revoked.value.status_code in {401,403}


def test_speed_02_stream_2gt_done_when_save_list_change_with_no_read(damien_http):
    """The original save/list/no-read case follows work-02's plain capture and door tick.
    The stored appointment is checked independently; no memory.read is needed to tick it.
    """
    import hashlib
    from brain_core.github_storage import GitHubBrainHomeStorage
    from tests.mcp.test_brain_composition import FIRST_TIME, LANE_B

    client, loopback, tokens, remote, runtime = damien_http
    saved = client.call('memory.save', {
        'operation_id': 'speed-02-harness-save', 'scope': 'personal', 'kind': 'item',
        'level': 'accepted', 'said_by': 'owner', 'came_by': 'direct',
        'words': 'dinner with Sam tonight at 7pm', 'title': 'Dinner with Sam',
        'body': 'dinner with Sam tonight at 7pm',
        'time_words': 'tonight at 7pm',
    })
    assert saved['status'] == 'accepted' and saved['remote_observed'] is True
    assert saved['state'] == 'up' and saved['level'] == 'accepted'
    appointment = saved['record']['dates']['appointment']
    assert appointment['day'] == FIRST_TIME.date().isoformat()
    assert appointment['at'] == '19:00' and appointment['words'] == 'tonight at 7pm'
    assert appointment['read_on'] == FIRST_TIME.date().isoformat()
    storage = GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH,
                                    token=LANE_B.TOKEN, transport=remote)
    stored = next(x for x in storage.snapshot_records(kinds=frozenset({'item'})).records
                  if x.record.id == saved['record_id'])
    assert stored.record.data['dates']['appointment'] == appointment
    assert not stored.record.data.get('timing')

    listed = client.call('memory.list', {'scope': 'personal'})
    assert not listed['truncated'] and listed['freshness'] == 'current'
    item = next(entry for entry in listed['items'] if entry['record_id'] == saved['record_id'])
    assert item['now']['is'] == 'task' and 'word' not in item
    assert item['expected_sha256'] == hashlib.sha256(stored.content).hexdigest()

    response = loopback.post('/v1/brain/work', headers={'Authorization': 'Bearer ' + tokens['alice']}, json={
        'operation_id': 'speed-02-harness-done', 'said_by': 'owner', 'came_by': 'direct',
        'message': 'Dinner with Sam happened',
        'moves': [{'move': 'tick', 'thing': item['record_id'], 'what': 'thing',
                   'words': 'Dinner with Sam happened'}],
    })
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed['status'] == 'accepted' and changed['remote_observed'] is True
    [thing] = changed['things']
    assert thing['id'] == saved['record_id'] and thing['record_id'] != saved['record_id']
    assert thing['now'] == 'done'
    successor = next(x.record for x in storage.snapshot_records(kinds=frozenset({'item'})).records
                     if x.record.id == thing['record_id'])
    assert successor.state == 'done' and saved['record_id'] in successor.supersedes
    assert successor.data['check']['checked_by'] == 'nic'
    assert successor.source['words'] == 'Dinner with Sam happened'
    # No memory.read between the list and the change: raw private HTTP reaches work,
    # because the plugin's bc_work entry is a later W15 interface.


def test_ciel_slice_3_damien_proposes_importance_and_only_nics_yes_confirms_it(damien_http):
    """Stale importance is ignored by the save bridge; its confirmation uses the door.
    The original owner preference checks remain independent of the work changes.
    """
    import hashlib
    from brain_core.github_storage import GitHubBrainHomeStorage
    from tests.mcp.test_brain_composition import LANE_B

    client, loopback, tokens, remote, runtime = damien_http
    words = {'said_by': 'owner', 'came_by': 'direct', 'words': 'I need to renew my passport'}
    saved = client.call('memory.save', {
        'operation_id': 'slice-3-propose', 'scope': 'personal', 'kind': 'item', 'level': 'accepted', **words,
        'title': 'Renew my passport', 'body': 'I need to renew my passport',
        'importance': 'proposed', 'importance_reason': 'The Japan trip is off without it',
    })
    assert saved['status'] == 'accepted' and saved['state'] == 'up'
    assert saved['importance'] is None
    assert saved['ignored'] == ['importance', 'importance_reason']
    assert saved['fix'] == 'refresh the connector'

    change = {'scope': 'personal', 'record_id': saved['record_id'],
              'expected_sha256': 'a' * 64, 'importance': 'confirmed'}
    with pytest.raises(HubClientError) as refused:
        client.call('memory.change', {**change, 'operation_id': 'slice-3-self-confirm', 'said_by': 'agent',
                                      'came_by': 'agent_guess', 'speaker': 'hermes-wa', 'words': 'Nic will agree'})
    assert refused.value.code == 'brain.invalid_argument'

    storage = GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH,
                                    token=LANE_B.TOKEN, transport=remote)
    def current_stake(record_id):
        record = next(x.record for x in storage.snapshot_records(kinds=frozenset({'item'})).records
                      if x.record.id == record_id)
        return record.data['stake']

    def work(operation, message, move):
        response = loopback.post('/v1/brain/work', headers={'Authorization': 'Bearer ' + tokens['alice']},
            json={'operation_id': operation, 'message': message, 'said_by': 'owner',
                  'came_by': 'direct', 'moves': [move]})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'accepted'
        return response.json()

    proposed = work('slice-3-stake', 'I need to renew my passport', {
        'move': 'stake', 'thing': saved['record_id'], 'level': 'changes_life',
        'at_stake': 'The Japan trip is off without it', 'confirm': False,
    })
    proposed_id = proposed['things'][0]['record_id']
    proposal = {'level': 'changes_life', 'at_stake': 'The Japan trip is off without it',
                'proposed_by': 'hermes-wa', 'confirmed': False}
    assert current_stake(proposed_id) == proposal
    quote = 'I confirm the passport stake because the Japan trip is off without it'
    confirmed = work('slice-3-yes', quote, {
        'move': 'stake', 'thing': proposed_id,
        'confirm': True, 'words': quote,
    })
    assert confirmed['status'] == 'accepted' and confirmed['remote_observed'] is True
    assert current_stake(confirmed['things'][0]['record_id']) == {**proposal, 'confirmed': True}
    listed = client.call('memory.list', {'scope': 'personal'})
    assert [item['record_id'] for item in listed['items']] == [saved['record_id']]
    physical_id = confirmed['things'][0]['record_id']
    assert physical_id != saved['record_id'] and physical_id != proposed_id
    physical = storage.preflight_record_by_id(physical_id).match
    assert physical.record.supersedes == (saved['record_id'], proposed_id)
    assert physical.record.body == 'I need to renew my passport'
    assert physical.record.source['words'] == quote
    assert listed['items'][0]['expected_sha256'] == hashlib.sha256(physical.content).hexdigest()
    assert listed['items'][0]['now']['stake'] == {**proposal, 'confirmed': True}

    focus = client.call('memory.save', {
        'operation_id': 'slice-3-focus', 'scope': 'personal', 'kind': 'preference', 'title': 'Focus projects',
        'said_by': 'owner', 'came_by': 'direct', 'words': 'My focus is Exosuit and the job search',
        'body': 'My focus is Exosuit and the job search', 'focus_projects': ['vault_exosuit', 'job-search'],
    })
    assert focus['status'] == 'accepted' and focus['importance'] is None
    preferences = client.call('memory.list', {'scope': 'personal', 'kind': 'preference'})
    assert [item['record_id'] for item in preferences['items']] == [focus['record_id']]



def test_diablos_signal_tool_takes_the_day_signals():
    """Placement slice 5: Diablo sends day_settled once Nic has picked his day, and day_change for
    his one-line changes; the hub's route takes the same three kinds."""

    from gremlin_town.api.review import ALLOWED_SIGNAL_KINDS

    [tool] = [item for item in plugin.REVIEW_TOOL_DEFINITIONS if item.name == "gremlin_town_moments_signal"]
    assert set(tool.parameters["properties"]["kind"]["enum"]) == set(ALLOWED_SIGNAL_KINDS) == {"pc_on", "day_settled", "day_change"}
    assert "placement.summary" in tool.description and "day_change" in tool.description


def test_work_is_door_only_and_maintenance_check_only_is_not_a_tool():
    from adapters.hermes.hub_client import DOOR_OPERATIONS, work_check_only
    assert DOOR_OPERATIONS == ("work",)
    payload = {"operation_id": "fixture-check", "message": "Pay venue invoice", "said_by": "owner",
               "came_by": "direct", "moves": [{"move": "new", "title": "Pay venue invoice"}]}
    transport = Requester([Response(200, {"status": "validated", "remote_observed": True})])
    config = HubClientConfig("https://hub.test", "fixture-token")
    closed = HubHTTPSClient(config, client=transport)
    with pytest.raises(HubClientError, match="participant surface"):
        work_check_only(closed, payload)
    assert transport.calls == []
    door = HubHTTPSClient(config, client=transport, door=True)
    original = deepcopy(payload)
    assert work_check_only(door, payload)["status"] == "validated"
    assert payload == original
    assert len(transport.calls) == 1
    method, url, kwargs = transport.calls[0]
    assert (method, url) == ("POST", "https://hub.test/v1/brain/work")
    assert kwargs["json"] == {**payload, "check_only": True}
    assert all(d.name != "work_check_only" for d in plugin.ALL_TOOL_DEFINITIONS)


def test_work_definition_matches_actual_door_factory_and_journals_exact_input(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    expected = next(t for t in server_tools() if t.name == "bc_work")
    actual = next(t for t in plugin.BRAIN_TOOL_DEFINITIONS if t.name == "bc_work")
    assert actual.description == expected.description
    assert actual.parameters == expected.parameters
    assert "bc_project_save" not in {t.name for t in plugin.BRAIN_TOOL_DEFINITIONS}
    requester = Requester([Response(200, {"status": "accepted", "remote_observed": True,
                                         "change": "fixture-work", "things": [], "say": "Saved."})])
    client = HubHTTPSClient(HubClientConfig("https://hub.test", "fixture-token"), client=requester, door=True)
    args = {"operation_id": "fixture-work", "message": "Pay venue invoice", "said_by": "owner",
            "came_by": "direct", "moves": [{"move": "new", "title": "Pay venue invoice"}]}
    result = json.loads(plugin._handler(client, "work")(args))
    assert result["ok"] is True
    journal = [json.loads(line) for line in (tmp_path / plugin.WRITE_JOURNAL_NAME).read_text().splitlines()]
    assert journal[0]["arguments"] == args
    assert journal[1]["status"] == "accepted" and journal[1]["remote_observed"] is True
    assert requester.calls[0][2]["json"] == args
    assert json.loads(plugin._handler(client, "work")({**args, "check_only": True}))["ok"] is False
    assert len(requester.calls) == 1


def test_review_definition_accepts_many_supplied_references():
    tool = next(d for d in plugin.REVIEW_TOOL_DEFINITIONS if d.operation == "review.respond")
    assert tool.parameters["required"] == []
    assert tool.parameters["properties"]["current_day"] == {"type": "boolean"}
    assert tool.parameters["properties"]["item_refs"]["items"] == {"type": "string", "minLength": 1}
    assert "bc_work" in tool.description
