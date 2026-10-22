from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from gremlin_town.dashboard.writes import DashboardWriter, InvalidAction, compile_action, split_steps
from gremlin_town.dashboard.write_routes import create_write_router
from gremlin_town.mcp.brain_runtime import BrainRuntimeError


def action(name="tick", **extra):
    base = {"action": name, "operation_id": "test-save", **extra}
    if name != "quick_add":
        base.update(ref="item-fixture", expected={"record_id": "item-version", "sha256": "a" * 64})
    return base


def test_tap_keeps_selection_and_pins_version_without_accepting_moves():
    request = compile_action(action())
    assert request.message == "Mark done"
    assert request.moves == ({"move": "tick", "thing": "item-fixture"},)
    assert request.expected_records == {"item-fixture": {"record_id": "item-version", "sha256": "a" * 64}}
    for key, value in {"moves": [], "owner_id": "another", "caller": "hermes-wa", "said_by": "owner"}.items():
        with pytest.raises(InvalidAction):
            compile_action({**action(), key: value})


def test_notes_and_steps_keep_the_typed_words():
    text = "Check the fitting\nBring the small adapter."
    request = compile_action(action("note", text=text))
    assert request.message == request.moves[0]["note"] == text
    assert split_steps("Check fitting, ask supplier then collect part\nStore receipt") == [
        {"title": "Check fitting"}, {"title": "ask supplier"},
        {"title": "collect part", "then": True}, {"title": "Store receipt"},
    ]
    assert compile_action(action("quick_add", text="Check fitting")).moves[0] == {
        "move": "new", "title": "Check fitting", "review": True}


@pytest.mark.parametrize("minutes", [True, 0, -1, 1.5, [], {}, "30", 525601])
def test_malformed_lengths_do_not_reach_the_door(minutes):
    with pytest.raises(InvalidAction):
        compile_action(action("length", minutes=minutes))


def test_length_and_park_use_the_selected_value():
    request = compile_action(action("length", minutes=30))
    assert request.message == "Set length: 30 min"
    assert request.moves[0]["length_is_nics"] is True
    assert compile_action(action("park", day="Someday")).moves[0]["event"] == "Someday"
    assert compile_action(action("park", day="2026-10-08")).moves[0]["until"] == "2026-10-08"
    with pytest.raises(InvalidAction):
        compile_action(action("park", day="whenever"))
    assert compile_action(action("today")).moves[0]["kind"] == "planned"


@pytest.mark.parametrize("change", ["", "../other", "CAPS", [], "x" * 129])
def test_invalid_undo_key_refuses(change):
    with pytest.raises(InvalidAction):
        compile_action(action("undo", change=change))


def test_runtime_receives_only_server_identity_and_verified_owner():
    import anyio
    calls = []
    class Runtime:
        async def change_work_from_tap(self, caller, selector, request, *, authenticated_owner_id):
            calls.append((caller, selector, request, authenticated_owner_id))
            return {"status": "accepted", "remote_observed": True}
    async def invoke():
        return await DashboardWriter(Runtime()).change(action(), owner_id="fixture-owner")
    anyio.run(invoke)
    caller, selector, _, owner = calls[0]
    assert caller.subject == "system:dashboard" and selector.kind == "personal" and owner == "fixture-owner"


def client(*, enabled=True, writes=True, fail=None, confirmed=True):
    calls = []
    class Writer:
        async def change(self, payload, *, owner_id):
            request = compile_action(payload)
            calls.append((request, owner_id))
            if fail:
                raise fail
            return {"status": "accepted", "remote_observed": confirmed, "say": "Saved"}
    async def authenticated(request: Request):
        if request.headers.get("X-Test-Session") != "owner":
            raise HTTPException(401)
        if request.headers.get("X-Test-Csrf") != "bound":
            raise HTTPException(403)
        return SimpleNamespace(owner_id="fixture-owner")
    app = FastAPI()
    app.include_router(create_write_router(Writer(), settings=SimpleNamespace(enabled=enabled, writes=writes),
                                          require_write_owner=authenticated))
    return TestClient(app), calls


HEADERS = {"X-Test-Session": "owner", "X-Test-Csrf": "bound"}


def test_route_requires_auth_and_feature_gate_before_reading_body():
    c, calls = client()
    assert c.post("/api/actions", json=action()).status_code == 401
    assert c.post("/api/actions", json=action(), headers={"X-Test-Session": "owner"}).status_code == 403
    off, _ = client(writes=False)
    assert off.post("/api/actions", json=action(), headers=HEADERS).status_code == 403
    assert not calls


def test_route_saves_once_and_reports_refusal_without_claiming_saved():
    c, calls = client()
    response = c.post("/api/actions", json=action(), headers=HEADERS)
    assert response.status_code == 200 and len(calls) == 1
    assert response.json()["operation_id"] == "test-save"
    denied, _ = client(fail=BrainRuntimeError("brain.conflict", "This item changed. Refresh it."))
    response = denied.post("/api/actions", json=action(), headers=HEADERS)
    assert response.status_code == 409 and response.json()["status"] == "refused"
    checking, _ = client(confirmed=False)
    response = checking.post("/api/actions", json=action(), headers=HEADERS)
    assert response.status_code == 503 and response.json()["status"] == "unconfirmed"


def test_bad_or_oversized_payloads_never_reach_the_writer():
    c, calls = client()
    headers = {**HEADERS, "Content-Type": "application/json"}
    assert c.post("/api/actions", content='{"action":"tick","action":"undo"}', headers=headers).status_code == 422
    assert c.post("/api/actions", content="{" + "x" * 16400, headers=headers).status_code == 413
    assert c.post("/api/actions", content="{", headers=headers).status_code == 400
    assert c.post("/api/actions", content="{}", headers=HEADERS).status_code == 415
    assert not calls
