from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gremlin_town.dashboard.host import create_dashboard_app, sources_from_runtime
from gremlin_town.dashboard.settings import DashboardSettings
from gremlin_town.mcp.brain_runtime import BrainRuntimeError
from tests.test_dashboard_auth import ORIGIN, RP, enrol
from tests.test_dashboard_reads import NOW, thing


def fixture(tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"fictional-host-session-secret!!!!")
    secret.chmod(0o600)
    settings = DashboardSettings(enabled=True, reads=True, origin=ORIGIN, rp_id=RP, owner_id="owner",
        state_file=tmp_path / "auth.sqlite", session_secret_file=secret)
    entry = thing(1)
    own = replace(entry, record=replace(entry.record, vaults=("vault://brain-home",)))
    other = replace(thing(2), record=replace(thing(2).record, owners=("different-owner",), vaults=("vault://brain-home",)))
    state = {"allowed": True, "source_calls": 0, "auth_calls": 0}
    class Brain:
        async def aclose(self):
            pass

        async def _authorize(self, caller, operation, selector, access):
            assert caller.subject == "system:dashboard" and selector.kind == "personal" and access == "read"
            state["auth_calls"] += 1
            if not state["allowed"]:
                raise BrainRuntimeError("ACCESS_DENIED", "Synthetic revoked read access")
            return SimpleNamespace(owner_id="owner", owner_locator="vault://brain-home")
    def items():
        state["source_calls"] += 1
        return (own, other)
    runtime = SimpleNamespace(brain_runtime=Brain(), review_sources=SimpleNamespace(brain_items=items),
        orientation_context=SimpleNamespace(sources=SimpleNamespace(preferences=None)),
        flowsavvy=SimpleNamespace(enabled=False), google_calendar_reader=None,
        review_why_log=None, orientation=SimpleNamespace(ranking="points"))
    return settings, runtime, state, own, other


def test_composed_reads_require_session_live_policy_and_exact_owner(tmp_path):
    settings, runtime, state, own, other = fixture(tmp_path)
    app = create_dashboard_app(settings, runtime, clock=lambda: NOW)
    client = TestClient(app, base_url=ORIGIN)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    for url in ("/api/day", "/api/work", "/api/work/" + own.record.id, "/app", "/assets/dashboard.js"):
        assert client.get(url).status_code == 401
    assert state["source_calls"] == 0
    enrol(app, client)
    assert client.get("/", follow_redirects=False).headers["location"] == "/app"
    response = client.get("/api/work/" + own.record.id)
    assert response.status_code == 200 and response.json()["record_sha256"] == own.record.sha256
    assert client.get("/api/work/" + other.record.id).status_code == 404
    page = client.get("/app")
    assert "actions.js" not in page.text
    assert client.post("/api/actions", json={}).status_code == 404
    previous = state["source_calls"]
    state["allowed"] = False
    response = client.get("/api/day")
    assert response.status_code == 503 and "Synthetic" not in response.text
    assert state["source_calls"] == previous


def test_disabled_host_does_not_touch_runtime_or_auth_store():
    app = create_dashboard_app(DashboardSettings(), object())
    assert TestClient(app).get("/api/day").status_code == 404


@pytest.mark.parametrize("writes,ask", [(False, False), (True, False), (False, True), (True, True)])
def test_action_assets_follow_their_independent_gates(tmp_path, writes, ask):
    settings, runtime, *_ = fixture(tmp_path)
    runtime.service = object()
    runtime.review = SimpleNamespace(sender_participant="review", delivery_participant="diablo", project="project")
    app = create_dashboard_app(replace(settings, writes=writes, ask_diablo=ask), runtime, clock=lambda: NOW)
    client = TestClient(app, base_url=ORIGIN)
    enrol(app, client)
    page = client.get("/app").text
    for name, enabled in (("actions.js", writes), ("ask.js", ask)):
        assert (name in page) is enabled
        assert client.get("/assets/" + name).status_code == (200 if enabled else 404)


def test_missing_brain_reader_refuses_startup(tmp_path):
    settings, runtime, *_ = fixture(tmp_path)
    runtime.brain_runtime = None
    with pytest.raises(ValueError, match="Brain Home"):
        create_dashboard_app(settings, runtime)


def test_configured_owner_cannot_select_a_different_brain_owner(tmp_path):
    settings, runtime, state, *_ = fixture(tmp_path)
    reader = sources_from_runtime(replace(settings, owner_id="someone-else"), runtime)
    with pytest.raises(Exception, match="access"):
        reader.items()
    assert state["source_calls"] == 0


def test_read_listener_uses_existing_runtime_without_dispatch_worker_and_closes(tmp_path, monkeypatch):
    from gremlin_town.dashboard.run import main
    from tests.test_dashboard_auth_cli import config
    path = config(tmp_path)
    with path.open("a") as target:
        target.write("reads = true\n")
    settings, runtime, *_ = fixture(tmp_path)
    calls = []
    runtime.close = lambda: calls.append("closed")
    def build(config_path, *, start_worker):
        assert config_path == path and start_worker is False
        return runtime
    monkeypatch.setattr("gremlin_town.runtime.build_runtime", build)
    monkeypatch.setattr("gremlin_town.dashboard.run.uvicorn.run", lambda app, **options: calls.append(options))
    assert main(["serve", "--config", str(path)]) == 0
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["workers"] == 1
    assert calls[0]["access_log"] is False and calls[-1] == "closed"


def test_systemd_credential_file_path_override(tmp_path, monkeypatch):
    from gremlin_town.dashboard.run import load_settings
    from tests.test_dashboard_auth_cli import config
    path = config(tmp_path)
    supplied = tmp_path / "credential-directory" / "dashboard_session"
    monkeypatch.setenv("GT_DASHBOARD_SESSION_SECRET_FILE", str(supplied))
    assert load_settings(path).session_secret_file == supplied
