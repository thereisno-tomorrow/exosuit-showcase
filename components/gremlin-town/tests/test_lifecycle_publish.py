"""Isolated file publication: real loaders/SQLite, no native or live effects."""
from copy import deepcopy
from contextlib import contextmanager
import json
from types import SimpleNamespace
import tomllib

import pytest

from gremlin_town import lifecycle_publish as module
from gremlin_town.lifecycle import LifecycleError
from gremlin_town.runtime import _load_participants
from gremlin_town.mcp.brain_assembly import load_owner_policy
from gremlin_town.storage.sqlite import SQLiteStore

UID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ACTOR = "grok-" + UID


def bot(uid=UID, **changes):
    return {"actor_id": "grok-" + uid, "name": "Native " + uid,
            "state": "ready", "token_sha256": "c" * 64,
            "delivery_address": uid + "@example.test", "revoked_hashes": [], **changes}


def document(runtime):
    return tomllib.loads(runtime.participants_file.read_text())


def row(runtime, actor=ACTOR):
    return next(p for p in document(runtime)["participant"] if p["id"] == actor)


@pytest.fixture
def runtime(tmp_path):
    participants = tmp_path / "participants.toml"
    rows = [{"id": actor, "display_name": actor, "address": actor + "@example.test",
             "role": "participant", "enabled": True, "auth_mode": "bearer",
             "token_sha256": digest * 64, "projects": ["demo"],
             "sensitivities": ["normal"], "aliases": []}
            for actor, digest in [(module.BASELINE, "a"), ("local-codex", "b")]]
    routes = [{"project": "demo", "source": module.BASELINE,
               "destination": "local-codex", "sensitivity": "normal"}]
    participants.write_text("# untouched header\n" + "".join(module._table("participant", p) for p in rows)
                            + "".join(module._table("route", r) for r in routes))
    brain = tmp_path / "brain.json"
    brain.write_text(json.dumps({"schema": "gt.brain-owner-policy/v1", "revision": "baseline",
        "owners": [{"kind": "personal", "project_id": None, "owner_id": "owner",
                    "owner_locator": "vault://brain-home", "readers": [module.BASELINE, "local-codex"],
                    "writers": [module.BASELINE], "operations": ["memory.read", "memory.save"], "session_id": None}],
        "subject_scopes": {module.BASELINE: ["bc:read", "bc:write"], "local-codex": ["bc:read"]}}))
    policy, _ = _load_participants(participants)
    store = SQLiteStore(tmp_path / "state.sqlite")
    result = SimpleNamespace(participants_file=participants, brain=SimpleNamespace(policy_file=brain),
                             policy=policy, store=store, snapshots=[], fail_refresh=None)
    def refresh():
        assert not store.connection.in_transaction
        with policy.dispatch_lock, policy.lock:
            candidate, _ = _load_participants(participants)
            result.snapshots.append(document(result))
            if result.fail_refresh == len(result.snapshots):
                raise RuntimeError("injected refresh failure")
            policy.refresh_from(candidate)
    result.refresh_participants = refresh
    transaction = store.transaction
    @contextmanager
    def checked_transaction():
        # Detect the SQLite -> policy lock inversion even with reentrant locks.
        assert not policy.lock._is_owned()
        with transaction() as db:
            yield db
    store.transaction = checked_transaction
    yield result
    store.close()


def publish(runtime, bots):
    return module.FilePolicyPublisher(runtime, runtime.participants_file).publish(bots)


def test_add_rename_noop_revoke_restart(runtime):
    baseline = document(runtime)
    original = runtime.participants_file.read_bytes()
    assert publish(runtime, {UID: bot()})["status"] == "published"
    assert row(runtime)["address"] == bot()["delivery_address"]
    assert row(runtime)["aliases"] == []
    assert runtime.snapshots[0]["participant"][-1]["enabled"] is False
    assert document(runtime)["participant"][:2] == baseline["participant"]
    assert any(p.read_bytes() == original for p in runtime.participants_file.parent.glob("*.lifecycle-preimage-*"))
    load_owner_policy(runtime.brain.policy_file)
    snapshot = runtime.participants_file.read_bytes()
    count = len(runtime.snapshots)
    assert publish(runtime, {UID: bot()})["status"] == "unchanged"
    assert len(runtime.snapshots) == count + 1
    assert runtime.participants_file.read_bytes() == snapshot
    old = row(runtime)
    publish(runtime, {UID: bot(name="Renamed")})
    assert row(runtime) == {**old, "display_name": "Renamed"}
    routes = document(runtime)["route"]
    assert len(routes) == 2
    publish(runtime, {})
    assert row(runtime)["enabled"] is False
    assert row(runtime)["address"] == old["address"]
    assert document(runtime)["route"] == routes
    runtime.policy, _ = _load_participants(runtime.participants_file)
    assert runtime.policy._participants[ACTOR].enabled is False
    brain = json.loads(runtime.brain.policy_file.read_text())
    assert ACTOR not in brain["subject_scopes"]
    assert ACTOR not in brain["owners"][0]["readers"]
    assert brain["subject_scopes"][module.BASELINE] == ["bc:read", "bc:write"]


def test_equal_files_repair_missed_runtime_publication(runtime):
    publish(runtime, {UID: bot()})
    runtime.policy.set_enabled(ACTOR, False)
    before = runtime.participants_file.read_bytes(), runtime.brain.policy_file.read_bytes()
    assert publish(runtime, {UID: bot()})["status"] == "unchanged"
    assert runtime.policy._participants[ACTOR].enabled
    assert before == (runtime.participants_file.read_bytes(), runtime.brain.policy_file.read_bytes())


def test_removed_baseline_route_disables_managed_actor_and_preserves_history(runtime):
    publish(runtime, {UID: bot()})
    doc = document(runtime)
    doc["route"] = [r for r in doc["route"] if module.BASELINE not in (r["source"],r["destination"])]
    runtime.participants_file.write_text("".join(module._table(kind, item)
        for kind in ("participant", "route") for item in doc.get(kind, [])))
    with pytest.raises(LifecycleError, match="obsolete_managed_routes"):
        publish(runtime, {UID: bot()})
    assert not row(runtime)["enabled"]
    assert not runtime.policy._participants[ACTOR].enabled
    assert document(runtime)["route"] == doc["route"]
    assert ACTOR not in json.loads(runtime.brain.policy_file.read_text())["subject_scopes"]


def test_unchanged_inactive_refresh_failure_revokes_stale_memory(runtime):
    publish(runtime, {UID: bot()})
    publish(runtime, {})
    runtime.policy.set_enabled(ACTOR, True)
    runtime.fail_refresh = len(runtime.snapshots) + 1
    with pytest.raises(RuntimeError, match="injected refresh failure"):
        publish(runtime, {})
    assert not runtime.policy._participants[ACTOR].enabled
    assert not row(runtime)["enabled"]


def test_new_pending_omitted(runtime):
    before = runtime.participants_file.read_bytes()
    pending = bot(state="pending")
    pending.pop("token_sha256")
    pending.pop("delivery_address")
    assert publish(runtime, {UID: pending})["status"] == "unchanged"
    assert runtime.participants_file.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"state": "unknown"}, {"token_sha256": "0" * 64}, {"token_sha256": "a" * 64},
    {"token_sha256": "C" * 64}, {"revoked_hashes": ["c" * 64]},
    {"delivery_address": None}, {"delivery_address": "bad address"},
    {"delivery_address": module.BASELINE.upper() + "@EXAMPLE.TEST"},
    {"actor_id": "operator"},
])
def test_invalid_no_mutations(runtime, changes):
    before = runtime.participants_file.read_bytes(), runtime.brain.policy_file.read_bytes()
    with pytest.raises(LifecycleError):
        publish(runtime, {UID: bot(**changes)})
    assert before == (runtime.participants_file.read_bytes(), runtime.brain.policy_file.read_bytes())
    assert not runtime.snapshots


def test_address_and_hash_collisions_include_history(runtime):
    publish(runtime, {UID: bot()})
    publish(runtime, {})
    for changes in ({"delivery_address": bot()["delivery_address"], "token_sha256": "d" * 64},
                    {"token_sha256": "c" * 64}):
        with pytest.raises(LifecycleError):
            publish(runtime, {OTHER: bot(OTHER, **changes)})
    with pytest.raises(LifecycleError):
        publish(runtime, {UID: bot(delivery_address="different@example.test")})


def test_operator_baseline_refused(runtime):
    text = runtime.participants_file.read_text().replace('"role" = "participant"', '"role" = "operator"', 1)
    runtime.participants_file.write_text(text)
    with pytest.raises(LifecycleError, match="baseline"):
        publish(runtime, {UID: bot()})


@pytest.mark.parametrize("point", ["brain", "first_refresh", "final_refresh"])
def test_failure_disables_and_same_hash_retry_succeeds(runtime, monkeypatch, point):
    atomic = module._atomic
    if point == "brain":
        def fail(path, expected, content):
            if path == runtime.brain.policy_file:
                raise OSError("injected policy failure")
            return atomic(path, expected, content)
        monkeypatch.setattr(module, "_atomic", fail)
    else:
        runtime.fail_refresh = 1 if point == "first_refresh" else 2
    with pytest.raises((RuntimeError, OSError)):
        publish(runtime, {UID: bot()})
    assert row(runtime)["enabled"] is False
    if ACTOR in runtime.policy._participants:
        assert not runtime.policy._participants[ACTOR].enabled
    runtime.fail_refresh = None
    monkeypatch.setattr(module, "_atomic", atomic)
    publish(runtime, {UID: bot()})
    assert row(runtime)["enabled"] is True


def test_expected_hash_conflict(runtime):
    path = runtime.participants_file
    before = path.read_bytes()
    with pytest.raises(LifecycleError, match="preimage_conflict"):
        module._atomic(path, "f" * 64, b"replacement")
    assert path.read_bytes() == before


def test_holds_persist_on_reenable_and_refresh_outside_transaction(runtime):
    publish(runtime, {UID: bot()})
    with runtime.store.transaction() as db:
        db.execute("""INSERT INTO requests(request_id,mode,project,requester_id,recipient_id,
            objective,sensitivity,work_state,revision,created_at,updated_at)
            VALUES('req','action_requested','demo','local-codex',?,'probe','normal','requested',1,'now','now')""", (ACTOR,))
        db.execute("""INSERT INTO operations(operation_id,client_operation_id,actor_id,request_id,
            operation_kind,input_digest,accepted_at,operation_state,recipient_address,route_destination_id)
            VALUES('op','client','local-codex','req','request','digest','now','queued',?,?)""",
            (bot()["delivery_address"], ACTOR))
    publish(runtime, {UID: bot(state="inactive", revoked_hashes=["c" * 64])})
    publish(runtime, {UID: bot(token_sha256="d" * 64, revoked_hashes=["c" * 64])})
    req = runtime.store.fetchone("SELECT * FROM requests")
    op = runtime.store.fetchone("SELECT * FROM operations")
    assert req["recovery_hold"] == op["recovery_hold"] == 1
    assert req["work_state"] == "requested" and op["operation_state"] == "queued"
    assert op["recipient_address"] == bot()["delivery_address"]
    events = runtime.store.fetchall("SELECT * FROM events")
    assert len(events) == 2
    assert all(json.loads(e["detail_json"])["reason"] == "participant_inactive" for e in events)


def test_two_bots_reverse_mapping_is_noop(runtime):
    first, second = bot(), bot(OTHER, token_sha256="d" * 64)
    publish(runtime, {UID: first, OTHER: second})
    before = runtime.participants_file.read_bytes(), runtime.brain.policy_file.read_bytes()
    assert publish(runtime, {OTHER: second, UID: first})["status"] == "unchanged"
    assert before == (runtime.participants_file.read_bytes(), runtime.brain.policy_file.read_bytes())
    assert len(document(runtime)["route"]) == 3


def test_pending_orphan_brain_grants_revoked_without_placeholder(runtime):
    path = runtime.brain.policy_file
    brain = json.loads(path.read_text())
    brain["subject_scopes"][ACTOR] = ["bc:read"]
    brain["owners"][0]["readers"].append(ACTOR)
    path.write_text(json.dumps(brain))
    publish(runtime, {UID: bot(state="pending")})
    assert ACTOR not in json.loads(path.read_text())["subject_scopes"]
    assert not any(p["id"] == ACTOR for p in document(runtime)["participant"])


def test_missing_route_table_supported(runtime):
    doc = document(runtime)
    runtime.participants_file.write_text("".join(module._table("participant", p) for p in doc["participant"]))
    publish(runtime, {UID: bot()})
    assert "route" not in document(runtime)


def test_active_hash_cannot_match_pending_or_revoked_other(runtime):
    for second in (bot(OTHER, state="pending"), bot(OTHER, state="inactive", token_sha256="d"*64, revoked_hashes=["c"*64])):
        with pytest.raises(LifecycleError, match="reused_or_revoked_hash"):
            publish(runtime, {UID: bot(), OTHER: second})


def test_operator_existing_identity_refused(runtime):
    publish(runtime, {UID: bot()})
    doc = document(runtime)
    doc["participant"][-1]["role"] = "operator"
    runtime.participants_file.write_text("".join(module._table(kind, item)
        for kind in ("participant", "route") for item in doc.get(kind, [])))
    with pytest.raises(LifecycleError, match="operator_identity_collision"):
        publish(runtime, {UID: bot()})


def test_final_replace_failure_returns_disk_to_disabled(runtime, monkeypatch):
    replace = module._replace
    calls = 0
    def fail_after_replace(path, temporary):
        nonlocal calls
        replace(path, temporary)
        if path == runtime.participants_file:
            calls += 1
            if calls == 2:
                raise OSError("injected post-replacement failure")
    monkeypatch.setattr(module, "_replace", fail_after_replace)
    with pytest.raises(OSError):
        publish(runtime, {UID: bot()})
    assert not row(runtime)["enabled"]
    assert not runtime.policy._participants[ACTOR].enabled
