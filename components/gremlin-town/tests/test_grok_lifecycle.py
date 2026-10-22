import copy
import hashlib
import json

import pytest

from gremlin_town.lifecycle import GrokLifecycle
from gremlin_town.storage import SQLiteStore

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def bot(uid=U1, name="Temporary lifecycle test", revision=1, status="enabled"):
    return dict(uuid=uid, name=name, revision=revision, status=status, eligible=True)


def snapshot(revision=1, bots=None, **kw):
    return dict(source="grok", revision=revision, kind="snapshot", complete=True,
                next_cursor=None, bots=[bot()] if bots is None else bots, **kw)


class Source:
    def __init__(self): self.value = snapshot()
    def fetch(self):
        if isinstance(self.value, Exception): raise self.value
        return copy.deepcopy(self.value)


class Issuer:
    def __init__(self): self.installs = {}; self.calls = 0; self.fail = False
    def ensure(self, uid, actor, generation, name):
        self.calls += 1
        key = uid, generation
        self.installs.setdefault(key, hashlib.sha256((actor + str(generation)).encode()).hexdigest())
        if self.fail: raise RuntimeError("secret must never be logged")
        return dict(token_sha256=self.installs[key], delivery_address=uid + "@example.test",
                    protected=True, instructions_installed=True)


class Publisher:
    def __init__(self): self.current = {}; self.calls = 0
    def publish(self, bots):
        self.calls += 1
        self.current = copy.deepcopy(bots)


@pytest.fixture
def env(tmp_path):
    store = SQLiteStore(tmp_path / "existing-hub.sqlite3")
    source, issuer, publisher = Source(), Issuer(), Publisher()
    manager = GrokLifecycle(store, source=source, provisioner=issuer, publisher=publisher)
    yield manager, source, issuer, publisher
    store.close()


def test_create_rename_replay_and_missed_publication(env):
    m, source, issuer, p = env
    assert m.reconcile()["status"] == "synchronized"
    first = copy.deepcopy(p.current[U1])
    source.value = snapshot(2, [bot(name="Renamed", revision=2)])
    assert m.reconcile()["bots"][0]["name"] == "Renamed"
    assert p.current[U1]["actor_id"] == first["actor_id"]
    assert p.current[U1]["token_sha256"] == first["token_sha256"]
    p.current = {}  # A missed external publication is repaired on equal revision.
    assert m.reconcile()["status"] == "synchronized"
    assert p.current[U1]["state"] == "ready"
    assert issuer.calls == 1 and len(issuer.installs) == 1
    assert "token_sha256" not in json.dumps(m.status())


def test_disable_delete_same_name_replacement_and_tombstone(env):
    m, source, issuer, p = env
    m.reconcile(); original = p.current[U1]["token_sha256"]
    source.value = snapshot(2, [bot(revision=2, status="disabled")])
    m.reconcile()
    assert p.current[U1]["state"] == "inactive" and "token_sha256" not in p.current[U1]
    source.value = snapshot(3, [bot(U2, revision=3)])
    m.reconcile()
    assert p.current[U1]["registry_status"] == "deleted"
    assert p.current[U2]["token_sha256"] != original
    assert p.current[U1]["actor_id"] != p.current[U2]["actor_id"]
    source.value = snapshot(4, [bot(revision=4), bot(U2, revision=3)])
    m.reconcile()
    assert p.current[U1]["state"] == "inactive"  # Deleted UUID cannot be reused.


@pytest.mark.parametrize("failure", ["timeout", "partial", "cursor", "malformed", "stale", "conflict", "kind_type", "status_type"])
def test_bad_registry_never_revokes_last_accepted_roster(env, failure):
    m, source, issuer, p = env
    m.reconcile(); before = copy.deepcopy(p.current); calls = p.calls
    source.value = snapshot(2, [])
    if failure == "timeout": source.value = TimeoutError("secret endpoint")
    elif failure == "partial": source.value["complete"] = False
    elif failure == "cursor": source.value["next_cursor"] = "page-2"
    elif failure == "malformed": source.value["bots"] = [{"uuid": U1}]
    elif failure == "stale": source.value["revision"] = 0
    elif failure == "conflict": source.value["revision"] = 1
    elif failure == "kind_type": source.value["kind"] = {}
    elif failure == "status_type": source.value["bots"] = [{**bot(revision=2), "status": []}]
    assert m.reconcile()["status"] == "stale"
    assert p.current == before and p.calls == calls
    assert m.status()["last_success_at"] is not None


def test_out_of_order_bot_cannot_resurrect_disabled_identity(env):
    m, source, issuer, p = env
    m.reconcile()
    source.value = snapshot(3, [bot(revision=3, status="disabled")])
    source.value["kind"] = "events"
    m.reconcile()
    source.value = snapshot(4, [bot(revision=1)])
    m.reconcile()
    assert p.current[U1]["state"] == "inactive"
    assert issuer.calls == 1


def test_partial_provisioning_reuses_generation_and_stays_disabled(env):
    m, source, issuer, p = env
    issuer.fail = True
    assert m.reconcile()["status"] == "pending"
    assert p.current[U1]["state"] == "pending"
    assert "secret" not in json.dumps(m.status())
    issuer.fail = False
    assert m.reconcile()["status"] == "synchronized"
    assert len(issuer.installs) == 1


def test_rename_during_partial_provision_preserves_idempotency_key(env):
    m, source, issuer, p = env
    issuer.fail = True
    assert m.reconcile()["status"] == "pending"
    source.value = snapshot(2, [bot(name="Renamed while pending", revision=2)])
    issuer.fail = False
    assert m.reconcile()["status"] == "synchronized"
    assert list(issuer.installs) == [(U1, 1)]
    assert p.current[U1]["generation"] == 1


def test_reenable_rotates_and_restart_reuses_applied_state(env):
    m, source, issuer, p = env
    m.reconcile(); original = p.current[U1]["token_sha256"]
    source.value = snapshot(2, [bot(revision=2, status="disabled")]); m.reconcile()
    source.value = snapshot(3, [bot(revision=3)]); m.reconcile()
    assert p.current[U1]["token_sha256"] != original
    restarted = GrokLifecycle(m.store, source=source, provisioner=issuer, publisher=p)
    restarted.reconcile()
    assert len(issuer.installs) == 2


def test_missing_source_is_explicit_not_empty_registry(env):
    m, source, issuer, p = env
    m.reconcile(); before = copy.deepcopy(p.current)
    m.source = None
    assert m.reconcile()["failures"] == [{"code": "registry_adapter_unavailable"}]
    assert p.current == before


def test_event_batch_does_not_delete_absent_others(env):
    m, source, issuer, p = env
    source.value = snapshot(1, [bot(), bot(U2)]); m.reconcile()
    source.value = snapshot(2, [bot(revision=2, status="deleted")]); source.value["kind"] = "events"
    m.reconcile()
    assert p.current[U2]["state"] == "ready"


def test_same_name_distinct_uuid_gets_distinct_credentials(env):
    m, source, issuer, p = env
    source.value = snapshot(1, [bot(), bot(U2)])
    m.reconcile()
    assert len({b["actor_id"] for b in p.current.values()}) == 2
    assert len({b["token_sha256"] for b in p.current.values()}) == 2


@pytest.mark.parametrize("address", [None, "", "copied mailbox", "a@b\r\nBcc: x@y", "x" * 255])
def test_invalid_delivery_receipt_keeps_identity_pending(env, address):
    m, source, issuer, p = env
    original = issuer.ensure
    issuer.ensure = lambda *args: {**original(*args), "delivery_address": address}
    assert m.reconcile()["status"] == "pending"
    assert p.current[U1]["state"] == "pending"
    assert "token_sha256" not in p.current[U1]


def test_duplicate_address_and_revoked_token_never_ready(env):
    m, source, issuer, p = env
    m.reconcile()
    old = p.current[U1]["token_sha256"]
    original = issuer.ensure
    source.value = snapshot(2, [bot(), bot(U2, revision=2)])
    issuer.ensure = lambda *args: {**original(*args), "delivery_address": U1 + "@EXAMPLE.test"}
    assert m.reconcile()["status"] == "pending"
    assert p.current[U2]["state"] == "pending"
    source.value = snapshot(3, [bot(revision=3, status="disabled")]); m.reconcile()
    source.value = snapshot(4, [bot(revision=4)])
    issuer.ensure = lambda *args: {**original(*args), "token_sha256": old}
    assert m.reconcile()["status"] == "pending"
    assert p.current[U1]["state"] == "pending"


def test_final_publication_failure_retains_receipt_for_retry(env):
    m, source, issuer, p = env
    original = p.publish
    def fail_ready(bots):
        if any(b["state"] == "ready" for b in bots.values()):
            raise OSError("publication interrupted")
        original(bots)
    p.publish = fail_ready
    assert m.reconcile()["status"] == "publication_failed"
    assert p.current[U1]["state"] == "pending"
    p.publish = original
    assert m.reconcile()["status"] == "synchronized"
    assert issuer.calls == 1
