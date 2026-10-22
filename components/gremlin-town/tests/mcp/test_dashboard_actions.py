"""Real passkey authentication and checked saves over an in-process fake GitHub."""
from fastapi.testclient import TestClient

from brain_core import work
from gremlin_town.dashboard.app import create_app, require_write_owner
from gremlin_town.dashboard.settings import DashboardSettings
from gremlin_town.dashboard.write_routes import create_write_router
from gremlin_town.dashboard.writes import DashboardWriter
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.mcp.surface_door import bind_owner_tap
from tests.test_dashboard_auth import ORIGIN, RP, enrol
from test_work_door import WORK, records, setup_door


def test_authenticated_actions_publish_replay_and_refuse_stale_page(tmp_path):
    provider, fake, _, log = setup_door(tmp_path)
    provider.code_callers = frozenset({"system:dashboard"})
    provider.schedule_lengths = True
    class Runtime:
        async def change_work_from_tap(self, caller, selector, request, *, authenticated_owner_id):
            assert selector.kind == "personal" and caller.subject == "system:dashboard"
            with bind_owner_tap(caller.subject, authenticated_owner_id, request), bind_write_attribution(caller.subject):
                return await provider.work(WORK, request)
    secret = tmp_path / "session-secret"
    secret.write_bytes(b"fictional-dashboard-action-secret")
    secret.chmod(0o600)
    settings = DashboardSettings(enabled=True, reads=True, writes=True, origin=ORIGIN, rp_id=RP,
        owner_id=WORK.owner_id, state_file=tmp_path / "auth.sqlite", session_secret_file=secret)
    router = create_write_router(DashboardWriter(Runtime()), settings=settings, require_write_owner=require_write_owner)
    app = create_app(settings, router=router)
    client = TestClient(app, base_url=ORIGIN)
    try:
        new = {"action": "quick_add", "operation_id": "add-fixture", "text": "Inspect blue folder"}
        initial = fake.branch
        assert client.post("/api/actions", json=new).status_code == 401
        assert fake.branch == initial
        enrol(app, client)
        csrf = client.get("/auth/session").json()["csrf"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        assert client.post("/api/actions", json=new, headers={"Origin": ORIGIN}).status_code == 403
        assert client.post("/api/actions", json=new, headers={**headers, "Origin": "https://evil.example.test"}).status_code == 403
        assert fake.branch == initial
        assert client.post("/api/actions", json=new, headers=headers).status_code == 200
        first = next(iter(work.index_things(records(fake))))
        def current():
            return work.index_things(records(fake))[first]
        def payload(action, key, **extra):
            record = next(record for record in records(fake) if record.id == current().id)
            return {"action": action, "operation_id": key, "ref": first,
                "expected": {"record_id": record.id, "sha256": record.sha256}, **extra}
        assert current().data["review_marks"][0]["mark"] == "question"
        note = payload("note", "note-fixture", text="Bring the small adapter.")
        assert client.post("/api/actions", json=note, headers=headers).status_code == 200
        head = fake.branch
        assert client.post("/api/actions", json=note, headers=headers).json()["status"] == "duplicate"
        assert fake.branch == head and current().body == note["text"]
        stale = {**note, "operation_id": "stale-note", "text": "This came from an old page."}
        assert client.post("/api/actions", json=stale, headers=headers).status_code == 409
        assert fake.branch == head
        for action, key, extra in [
            ("length", "length-fixture", {"minutes": 30}),
            ("today", "today-fixture", {}),
            ("park", "park-fixture", {"day": "2026-10-08"}),
            ("today", "today-unpark-fixture", {}),
            ("can_do_now", "unpark-fixture", {}),
            ("tick", "tick-fixture", {}),
            ("untick", "untick-fixture", {}),
            ("steps", "steps-fixture", {"text": "Check fitting, ask supplier then file receipt"}),
        ]:
            response = client.post("/api/actions", json=payload(action, key, **extra), headers=headers)
            assert response.status_code == 200, (action, response.json())
            if key == "today-unpark-fixture":
                assert current().state == "open" and current().data["dates"]["planned"]["day"] == "2026-09-30"
        assert current().state == "open" and current().data["timing"]["duration_minutes"] == 30
        assert len([r for r in work.index_things(records(fake)).values() if r.data.get("parent") == first]) == 3
        # Logout revokes authority; a previously copied CSRF value is insufficient.
        assert client.post("/auth/logout", json={}, headers=headers).status_code == 200
        head = fake.branch
        assert client.post("/api/actions", json=payload("tick", "after-logout"), headers=headers).status_code == 401
        assert fake.branch == head
    finally:
        log.close()
