import hashlib
import json

from tests.integration.test_brain_agent_http import hub, post, save_body
from tests.test_runtime_refresh import load, publish, headers


def test_new_brain_subject_uses_current_grants_without_runtime_rebuild(hub):
    client, runtime, tokens, remote, settings = hub
    original = runtime.brain_runtime
    token = "new-lifecycle-subject-token-long-enough"
    actor = "grok-11111111-1111-4111-8111-111111111111"
    document = load(runtime)
    template = next(p for p in document["participant"] if p["id"] == "alice")
    document["participant"].append({**template, "id": actor, "address": "new@example.test",
        "aliases": [], "token_sha256": hashlib.sha256(token.encode()).hexdigest()})
    publish(runtime, document)
    runtime.refresh_participants()
    assert post(client, token, "memory.save", save_body()).status_code == 403
    policy = json.loads(settings.policy_file.read_text())
    policy["subject_scopes"][actor] = ["bc:read", "bc:write"]
    policy["owners"][0]["readers"].append(actor)
    policy["owners"][0]["writers"].append(actor)
    settings.policy_file.write_text(json.dumps(policy))
    saved = post(client, token, "memory.save", save_body())
    assert saved.status_code == 200, saved.text
    read = post(client, tokens["alice"], "memory.read", {"scope":"personal", "record_id":saved.json()["record_id"]})
    assert read.status_code == 200, read.text
    assert read.json()["canonical_record"]["evidence"][0]["actor"] == actor
    policy["owners"][0]["writers"].remove(actor)
    settings.policy_file.write_text(json.dumps(policy))
    assert post(client, token, "memory.save", {**save_body(), "operation_id":"revoked-write"}).status_code == 403
    assert runtime.brain_runtime is original


def test_operator_controls_reject_payload_and_public_access(hub):
    client, runtime, tokens, remote, settings = hub
    base = "/v1/operator/grok-lifecycle"
    assert client.get(base).status_code == 403
    assert client.get(base, headers=headers(tokens["alice"])).status_code == 403
    response = client.get(base, headers=headers(tokens["operator"]))
    assert response.status_code == 200 and not response.json()["integration_configured"]
    assert client.post(base + "/reconcile", headers=headers(tokens["operator"]), json={}).status_code == 422
    response = client.post(base + "/reconcile", headers=headers(tokens["operator"]))
    assert response.status_code == 200
    assert response.json()["failures"] == [{"code":"registry_adapter_unavailable"}]
    assert client.post("/v1/operator/participants/refresh", headers=headers(tokens["operator"])).status_code == 200


def test_manager_publisher_existing_listener_add_rename_delete_and_retry(hub):
    from tests.test_grok_lifecycle import Source, snapshot, bot, U1
    from gremlin_town.lifecycle import GrokLifecycle
    from gremlin_town.lifecycle_publish import FilePolicyPublisher
    client, runtime, tokens, remote, settings = hub
    document = load(runtime)
    alice = next(p for p in document["participant"] if p["id"] == "alice")
    document["participant"].append({**alice, "id":"fleet-boss-grok",
        "address":"baseline@example.test", "token_sha256":"a" * 64, "aliases":[]})
    publish(runtime, document); runtime.refresh_participants()
    policy = json.loads(settings.policy_file.read_text())
    policy["subject_scopes"]["fleet-boss-grok"] = ["bc:read", "bc:write"]
    for field in ("readers", "writers"):
        policy["owners"][0][field].append("fleet-boss-grok")
    settings.policy_file.write_text(json.dumps(policy))
    token = "assembled-native-provisioning-fixture-token"
    class Issuer:
        calls = 0
        def ensure(self, *args):
            self.calls += 1
            return {"token_sha256":hashlib.sha256(token.encode()).hexdigest(),
                    "delivery_address":"new-native@example.test", "protected":True,
                    "instructions_installed":True}
    source, issuer = Source(), Issuer()
    manager = GrokLifecycle(runtime.store, source=source, provisioner=issuer,
        publisher=FilePolicyPublisher(runtime, runtime.participants_file))
    assert manager.reconcile()["status"] == "synchronized"
    assert client.get("/v1/participants",headers=headers(token)).status_code == 200
    saved = post(client, token, "memory.save", save_body())
    assert saved.status_code == 200, saved.text
    record_id = saved.json()["record_id"]
    source.value = snapshot(2, [bot(name="Renamed native", revision=2)])
    assert manager.reconcile()["status"] == "synchronized"
    assert manager.reconcile()["status"] == "synchronized" and issuer.calls == 1
    source.value = snapshot(3, [])
    assert manager.reconcile()["status"] == "synchronized"
    assert client.get("/v1/participants",headers=headers(token)).status_code == 401
    assert post(client, token, "memory.read", {"scope":"personal", "record_id":record_id}).status_code == 401
    read = post(client, tokens["alice"], "memory.read", {"scope":"personal", "record_id":record_id})
    assert read.status_code == 200
    assert read.json()["canonical_record"]["evidence"][0]["actor"] == "grok-" + U1
    retained = next(p for p in load(runtime)["participant"] if p["id"] == "grok-" + U1)
    assert retained["enabled"] is False
