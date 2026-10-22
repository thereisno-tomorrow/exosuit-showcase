"""Persistent fixture transport checks; no claim of production Brain policy."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[2] / "adapters/hermes/upgrade"

def test_live_preparation_retains_required_brain_tools_and_existing_features():
    spec = importlib.util.spec_from_file_location('live_switch_settings', HERE / 'live_switch.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    original = {'features': ['review', 'orientation', 'calendar'], 'preserved': True}
    projected = installer.compact_settings(original)
    assert projected['always_ready_tools'] == [
        'bc_memory_save', 'bc_memory_list', 'bc_memory_change', 'bc_memory_search',
        'bc_work', 'gremlin_town_review_respond']
    assert projected['features'] == original['features']
    assert projected['preserved'] is True
    assert 'always_ready_tools' not in original

spec = importlib.util.spec_from_file_location("practice_fixture_test", HERE / "practice_fixture.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def store(tmp_path):
    (tmp_path / "fixture-only.json").write_text(json.dumps({"fixture_only": True}))
    return module.FixtureStore(tmp_path)


def save_args():
    return dict(operation_id="practice-save-one", scope="personal", kind="item",
                title="Practice purple lantern", body="Fixture: check the purple lantern",
                said_by="owner", came_by="direct", words="Fixture: check the purple lantern")


def test_continuity_start_is_limited_to_fixed_fixture_note_and_replay_safe(store):
    args = {"operation_id": "practice-start", "session_id": "practice-provider-note",
            "scope": "personal", "intent": "Practice compression"}
    assert store.call("continuity.start", args)["status"] == "accepted"
    assert store.call("continuity.start", args)["status"] == "duplicate"
    with pytest.raises(module.FixtureError):
        store.call("continuity.start", {**args, "session_id": "foreign"})
    with pytest.raises(module.FixtureError):
        store.call("continuity.start", {**args, "intent": "Practice changed"})


def test_save_survives_reopening_and_recall(store):
    receipt = store.call("memory.save", save_args())
    fresh = module.FixtureStore(store.path.parent)
    found = fresh.call("memory.search", {"query": "purple lantern"})["matches"]
    assert receipt["remote_observed"] is True
    assert found[0]["record_id"] == receipt["record_id"]
    assert found[0]["body"] == save_args()["body"]
    assert receipt["fixture_only"] is True


def test_same_operation_is_idempotent_but_changed_payload_refused(store):
    original = store.call("memory.save", save_args())
    replay = store.call("memory.save", save_args())
    assert replay["status"] == "duplicate"
    assert replay["record_id"] == original["record_id"]
    with pytest.raises(module.FixtureError, match="conflict"):
        store.call("memory.save", {**save_args(), "body": "Fixture different"})
    assert len(store.call("memory.list", {})["records"]) == 1


def test_nonfixture_save_and_project_scope_are_refused(store):
    with pytest.raises(module.FixtureError, match="practice or fixture"):
        store.call("memory.save", {**save_args(), "title": "Real task", "body": "Real task", "words": "Real task"})
    with pytest.raises(module.FixtureError, match="personal fixture"):
        store.call("memory.save", {**save_args(), "scope": "project", "project_id": "real-vault"})
    assert store.call("memory.list", {})["records"] == []


def test_review_only_accepts_existing_fixture_references(store):
    with pytest.raises(module.FixtureError, match="stored fixture"):
        store.call("review.respond", {"item_refs": ["brn_real"], "response": "picked_today"})
    receipt = store.call("memory.save", save_args())
    result = store.call("review.respond", {"item_refs": [receipt["record_id"]], "response": "picked_today"})
    assert result["recorded"] is True and result["fixture_only"] is True


def test_unused_operation_cannot_pass_as_active(store):
    with pytest.raises(module.FixtureError, match="inactive"):
        store.call("calendar.remove", {})


def test_provider_marker_replay_is_once_and_foreign_note_refused(store):
    args = dict(operation_id="hermes-compressed-fixture", session_id="practice-provider-note",
                event="effect", value="compressed: fixture marker")
    assert store.call("continuity.append", args)["status"] == "accepted"
    assert store.call("continuity.append", args)["status"] == "duplicate"
    with pytest.raises(module.FixtureError, match="conflict"):
        store.call("continuity.append", {**args, "value": "compressed: changed"})
    with pytest.raises(module.FixtureError, match="fixed practice note"):
        store.call("continuity.append", {**args, "session_id": "real-note"})
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM continuity_markers").fetchone()[0] == 1


def test_fixture_prefetch_matches_installed_reader_contract(store):
    from datetime import date
    from adapters.hermes.memory_brain.provider import render_block
    store.call("memory.save", save_args())
    block = render_block(store.call("memory.list", {}), store.call("continuity.list", {}),
                         today=date(2026, 10, 4))
    assert "Practice purple lantern" in block
    assert "practice-provider-note" in block


def test_marker_required_before_any_store_creation(tmp_path):
    with pytest.raises(module.FixtureError, match="marker required"):
        module.FixtureStore(tmp_path)
    assert not (tmp_path / "fixture.sqlite3").exists()


def test_observer_budgets_include_native_and_large_schema():
    observer_spec = importlib.util.spec_from_file_location("observer_metrics_test", HERE / "practice_observer_plugin.py")
    observer = importlib.util.module_from_spec(observer_spec)
    observer_spec.loader.exec_module(observer)
    result = observer.tool_metrics([{"name": "terminal", "description": "x" * 12_001}])
    assert result["within_budget"] is False
    assert result["tool_names"] == ["terminal"]
    assert "description" not in result


def test_execution_observer_measures_shaped_request_and_calls_provider_once(monkeypatch):
    from adapters.hermes import plugin
    spec = importlib.util.spec_from_file_location("final_request_observer_test", HERE / "practice_observer_plugin.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    rows, calls = [], []
    monkeypatch.setattr(observer, "append", rows.append)
    # The candidate sanitizer is separately exercised by runtime_probe.
    import sys
    from types import ModuleType
    native = ModuleType("agent.codex_runtime")
    native._sanitize_consumer_codex_request = lambda agent, request: request
    monkeypatch.setitem(sys.modules, "agent.codex_runtime", native)
    request = {"tools": [{"name": "cronjob_manage", "description": "x" * 64_000,
                         "parameters": {"type": "object"}}]}
    shaped = plugin._native_doc_middleware({})(request=request)["request"]
    def provider(payload):
        calls.append(payload)
        return "fixture-response"
    assert observer.execution(request=shaped, next_call=provider) == "fixture-response"
    assert calls == [shaped]
    assert rows[0]["event"] == "llm_execution" and rows[0]["within_budget"]


def test_hub_observer_cannot_use_old_cli_weekly_receipt_as_discord_proof(tmp_path, monkeypatch):
    from types import SimpleNamespace
    observer_spec = importlib.util.spec_from_file_location("hub_observer_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(observer_spec)
    observer_spec.loader.exec_module(observer)
    root = tmp_path / "practice-20261004"
    home = root / "home"
    home.mkdir(parents=True)
    (root / "practice-only.json").write_text(json.dumps({"practice_only": True}))
    name = "hermes-upgrade-practice-20261004"
    start = "2026-10-04T12:00:00.000000000Z"
    (home / "practice-starts.json").write_text(json.dumps({name: start}))
    (home / "config.yaml").write_text(json.dumps({"platforms": {
        n: {"enabled": n == "discord"} for n in ("discord", "telegram", "feishu", "webhook", "api_server")},
        "session_reset": {"mode": "none"}}))
    rows = [{"container_name": name, "container_started_at": start, "event": "llm_execution", "platform": "discord",
             "within_budget": True, "schemas_sha256": "stable", "total_chars": 61939, "largest_chars": 7412}]
    rows += [{"container_name": name, "container_started_at": start, "event": "tool_request",
              "tool_name": "gremlin_town_more_tools", "front_action": action,
              "front_target": "bc_weekly" if action != "list" else "<other>"}
             for action in ("list", "describe", "call")]
    (home / "practice-observer.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "fixture-only.json").write_text(json.dumps({"fixture_only": True}))
    store = module.FixtureStore(fixture)
    with store.connect() as db:
        db.execute("INSERT INTO calls(at,operation,outcome) VALUES (?,?,?)",
                   ("2026-10-04T11:00:00+00:00", "weekly", "accepted"))

    def command(argv):
        fmt = argv[argv.index("--format") + 1] if "--format" in argv else ""
        return {"{{.Id}}": "fixture-container-id", "{{.Image}}": "sha256:fixture",
                "{{json .Mounts}}": json.dumps([{"Source": str(home)}]), "{{.RestartCount}}": "0",
                "{{.State.StartedAt}}": start,
                "{{json .State}}": json.dumps({"Running": True, "StartedAt": start, "OOMKilled": False})}.get(
                    fmt, "[discord] Connected as test bot")

    monkeypatch.setattr(observer, "command", command)
    monkeypatch.setattr(observer.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=0))
    report = observer.observe(root, name, "sha256:fixture")
    assert next(c for c in report["checks"] if c["check"] == "hub:rare-conversation")["status"] == "fail"
    assert report["safe_to_switch"] is False
    with store.connect() as db:
        db.execute("INSERT INTO calls(at,operation,outcome) VALUES (?,?,?)",
                   ("2026-10-04T12:00:01+00:00", "weekly", "accepted"))
    report = observer.observe(root, name, "sha256:fixture")
    assert next(c for c in report["checks"] if c["check"] == "hub:rare-conversation")["status"] == "pass"
    assert next(c for c in report["checks"] if c["check"] == "hub:manual-new")["status"] == "pending"


def test_prepared_brain_options_are_accepted_by_installed_provider_contract():
    import tempfile
    from adapters.hermes.memory_brain.provider import load_options
    prepare_spec = importlib.util.spec_from_file_location("brain_options_prepare_test", HERE / "practice.py")
    prepare = importlib.util.module_from_spec(prepare_spec)
    prepare_spec.loader.exec_module(prepare)
    scratch = HERE.parents[4] / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="brain-options-test-", dir=scratch) as root:
        home = prepare.prepare(Path(root) / "practice")
        assert load_options(home).prefetch is True


def test_discord_heartbeat_refuses_stale_or_previous_start(tmp_path):
    from datetime import UTC, datetime
    spec = importlib.util.spec_from_file_location("heartbeat_observer_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    record = {"updated_at": "2026-10-04T12:01:00Z", "platforms": {"discord": {
        "state": "connected", "updated_at": "2026-10-04T12:00:05Z"}}}
    path = tmp_path / "gateway_state.json"
    path.write_text(json.dumps(record))
    now = datetime(2026, 10, 4, 12, 1, 30, tzinfo=UTC)
    assert observer.discord_connected(tmp_path, "2026-10-04T12:00:00Z", now)
    assert not observer.discord_connected(tmp_path, "2026-10-04T12:00:10Z", now)
    assert not observer.discord_connected(tmp_path, "2026-10-04T12:00:00Z", now.replace(minute=5))
    record["platforms"]["discord"]["state"] = "retrying"
    path.write_text(json.dumps(record))
    assert not observer.discord_connected(tmp_path, "2026-10-04T12:00:00Z", now)


def test_native_receipts_require_current_start_and_successful_help_before_list(tmp_path):
    import sqlite3
    spec = importlib.util.spec_from_file_location("native_receipts_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("CREATE TABLE sessions(id TEXT,source TEXT,ended_at REAL,end_reason TEXT)")
        db.execute("CREATE TABLE messages(session_id TEXT,role TEXT,tool_name TEXT,content TEXT,timestamp REAL)")
        db.execute("INSERT INTO sessions VALUES ('fixture','discord',100,'session_reset')")
        db.execute("INSERT INTO messages VALUES ('fixture','tool','cronjob_manage',?,101)", (json.dumps({"success": True, "jobs": []}),))
    start = "1970-01-01T00:01:30Z"
    result = observer.native_receipts(tmp_path, start)
    assert result["manual_new_count"] == 1 and not result["cron_help_then_list"]
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("INSERT INTO messages VALUES ('fixture','tool','gremlin_town_more_tools',?,102)", (json.dumps({"ok": True, "result": {"name": "cronjob_manage"}}),))
        db.execute("INSERT INTO messages VALUES ('fixture','tool','cronjob_manage',?,103)", (json.dumps({"success": False, "jobs": []}),))
    assert not observer.native_receipts(tmp_path, start)["cron_help_then_list"]
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("INSERT INTO messages VALUES ('fixture','tool','cronjob_manage',?,104)", (json.dumps({"success": True, "jobs": []}),))
    assert observer.native_receipts(tmp_path, start)["cron_help_then_list"]
    result = observer.native_receipts(tmp_path, "1970-01-01T00:02:00Z")
    assert result == {"manual_new_count": 0, "cron_help_then_list": False}


def test_signing_key_helper_refuses_non_root_without_creating_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("practice_credentials_test", HERE / "practice_credentials.py")
    credentials = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(credentials)
    monkeypatch.setattr(credentials, "os", SimpleNamespace(name="posix", geteuid=lambda: 1000))
    with pytest.raises(ValueError, match="root operator required"):
        credentials.create(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_ancillary_delivery_receipt_from_another_start_cannot_pass(tmp_path):
    spec = importlib.util.spec_from_file_location("ancillary_receipts_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = {"fixture_only": True, "container": "fixture", "started_at": "old",
              "deliveries": [{"kind": kind, "message_id": kind, "remote_observed": True}
                             for kind in ("review", "mail")]}
    path = evidence / "courier-fixture.json"
    path.write_text(json.dumps(record))
    assert not observer.ancillary_receipts(tmp_path, "fixture", "current")["courier"]
    record["started_at"] = "current"
    path.write_text(json.dumps(record))
    assert observer.ancillary_receipts(tmp_path, "fixture", "current")["courier"]
    record["deliveries"][1]["remote_observed"] = False
    path.write_text(json.dumps(record))
    assert not observer.ancillary_receipts(tmp_path, "fixture", "current")["courier"]


def test_natural_idle_ignores_retained_earlier_markers(tmp_path):
    import sqlite3
    spec = importlib.util.spec_from_file_location("idle_receipts_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    home = tmp_path / "home"
    home.mkdir()
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    receipt = {"fixture_only": True, "container": "fixture", "started_at": "current",
               "at": "2026-10-04T15:48:03+00:00", "replayed_operation_id": "compressed-current"}
    (home / "practice-memory-replay.json").write_text(json.dumps(receipt))
    (home / "practice-memory-watch-idle.json").write_text(json.dumps({**receipt, "idle_marked": True,
                                                                    "natural_idle_elapsed_seconds": 1870}))
    with sqlite3.connect(fixture / "fixture.sqlite3") as db:
        db.execute("CREATE TABLE continuity_markers(id TEXT,body TEXT)")
        db.executemany("INSERT INTO continuity_markers VALUES (?,?)", [
            ("compressed-current", "{}"),
            ("hermes-idle-old", json.dumps({"value": "idle: no turn since 2026-10-04T14:34Z; fixture"})),
            ("hermes-idle-current", json.dumps({"value": "idle: no turn since 2026-10-04T15:48Z; fixture"}))])
    assert observer.ancillary_receipts(tmp_path, "fixture", "current")["natural_idle"]
    assert not observer.ancillary_receipts(tmp_path, "fixture", "other")["natural_idle"]


def test_completed_natural_job_uses_native_due_time_when_next_run_is_null(tmp_path):
    import sqlite3
    spec = importlib.util.spec_from_file_location("due_receipts_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    start = "2026-10-04T15:34:00Z"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    (evidence / "natural-job-created.json").write_text(json.dumps({"fixture_only": True,
        "container": "fixture", "started_at": start, "job_id": "one", "manual_run_invoked": False,
        "next_run_at": None}))
    (home / "practice-final-ticks.jsonl").write_text(json.dumps({"fixture_only": True,
        "at": "2026-10-04T15:47:01+00:00"}) + "\n")
    with sqlite3.connect(home / "cron/executions.db") as db:
        db.execute("CREATE TABLE executions(job_id TEXT,source TEXT,status TEXT,delivery_outcome TEXT,scheduled_instant TEXT)")
        db.execute("INSERT INTO executions VALUES ('one','builtin','completed','suppressed','2026-10-04T15:47:00+00:00')")
    assert observer.ancillary_receipts(tmp_path, "fixture", start)["natural_no_agent"]
    with sqlite3.connect(home / "cron/executions.db") as db:
        db.execute("UPDATE executions SET scheduled_instant=NULL")
    assert not observer.ancillary_receipts(tmp_path, "fixture", start)["natural_no_agent"]

def test_completed_acceptance_refuses_missing_and_prior_start_receipts(tmp_path):
    spec = importlib.util.spec_from_file_location("completed_receipts_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
    (tmp_path / "evidence").mkdir()
    assert not any(observer.completed_acceptance(tmp_path, "fixture", "current").values())
    fields = ("synthetic_mail_input", "actual_documented_native_scorer", "independent_discord_delivery", "one_score_one_delivery", "second_run_suppressed", "cursor_readback")
    value = {"fixture_only": True, "container": "fixture", "started_at": "old", "real_mailbox_access": False, **dict.fromkeys(fields, True)}
    path = tmp_path / "evidence/mail-flow-fixture.json"; path.write_text(json.dumps(value))
    assert not observer.completed_acceptance(tmp_path, "fixture", "current")["mail"]
    value["started_at"] = "current"; path.write_text(json.dumps(value))
    assert observer.completed_acceptance(tmp_path, "fixture", "current")["mail"]
    value["second_run_suppressed"] = False; path.write_text(json.dumps(value))
    assert not observer.completed_acceptance(tmp_path, "fixture", "current")["mail"]


def test_in_place_compression_needs_native_commit_and_independent_marker(tmp_path):
    import sqlite3
    spec = importlib.util.spec_from_file_location("in_place_receipts_test", HERE / "hub_observer.py")
    observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
    home = tmp_path / "home"; home.mkdir(); (tmp_path / "fixture").mkdir()
    fields = ("documented_native_compress_command", "native_in_place_commit", "private_synthetic_ui_success", "independent_fixture_marker", "continuity_note_preserved")
    value = {"fixture_only": True, "container": "fixture", "started_at": "current", "session_id": "same-chat", "fixture_marker_id": "marker", "native_active_messages": 6, **dict.fromkeys(fields, True)}
    (home / "practice-native-compression.json").write_text(json.dumps(value))
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE messages(session_id TEXT,active INTEGER,compacted INTEGER,_compressed_summary INTEGER)")
        db.executemany("INSERT INTO messages VALUES ('same-chat',1,0,?)", [(1,), *[(0,)] * 5])
        db.executemany("INSERT INTO messages VALUES ('same-chat',0,1,0)", [()] * 11)
    with sqlite3.connect(tmp_path / "fixture/fixture.sqlite3") as db:
        db.execute("CREATE TABLE continuity_markers(id TEXT,body TEXT)")
    assert not observer.completed_acceptance(tmp_path, "fixture", "current")["compression"]
    with sqlite3.connect(tmp_path / "fixture/fixture.sqlite3") as db:
        db.execute("INSERT INTO continuity_markers VALUES ('marker',?)", (json.dumps({"session_id": "practice-provider-note"}),))
    assert observer.completed_acceptance(tmp_path, "fixture", "current")["compression"]
    with sqlite3.connect(home / "state.db") as db:
        db.execute("UPDATE messages SET _compressed_summary=0")
    assert not observer.completed_acceptance(tmp_path, "fixture", "current")["compression"]

def test_live_observer_refuses_unbound_and_symlink_binding(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("live_observer_guard", HERE / "live_observer_plugin.py")
    observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError):observer.append({"event": "fixture"})
    (tmp_path / "upgrade-observation.json").write_text(json.dumps({"enabled": True, "container": "other", "started_at": "old"}))
    with pytest.raises(RuntimeError, match="explicit live start binding"):observer.append({"event": "fixture"})


def test_live_observer_logging_failure_preserves_exactly_one_provider_call(monkeypatch):
    import sys
    from types import ModuleType
    spec = importlib.util.spec_from_file_location("live_observer_call", HERE / "live_observer_plugin.py")
    observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
    sanitizer = ModuleType("agent.codex_runtime")
    sanitizer._sanitize_consumer_codex_request = lambda _agent, request: request
    monkeypatch.setitem(sys.modules, "agent.codex_runtime", sanitizer)
    def broken(_row):raise OSError("fixture log failure")
    monkeypatch.setattr(observer, "append", broken)
    calls=[]; request={"model":"fixture","tools":[{"function":{"name":"fixture"}}]}
    def provider(value):calls.append(value);return "reply"
    assert observer.execution(request=request,next_call=provider,platform="discord",provider="fixture") == "reply"
    assert calls == [request] and calls[0] is request
