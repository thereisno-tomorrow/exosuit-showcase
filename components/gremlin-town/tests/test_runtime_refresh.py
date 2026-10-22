"""Local lifecycle publication through assembled listeners and queued dispatch."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import tomllib

import pytest
from fastapi.testclient import TestClient

from gremlin_town.contracts import Actor
from gremlin_town.domain import GremlinTownError
from gremlin_town.runtime import ConfigurationError, build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_runtime_flow import _write_runtime_config, _packet
from tests.integration.test_brain_agent_http import hub, post


def load(runtime):
    return tomllib.loads(runtime.participants_file.read_text(encoding="utf-8"))


def publish(runtime, document):
    lines = []
    for section in ("participant", "route"):
        for item in document.get(section, []):
            lines.append("[[" + section + "]]")
            lines.extend(f"{key} = {json.dumps(value)}" for key, value in item.items())
    runtime.participants_file.write_text("\n".join(lines), encoding="utf-8")


def headers(token):
    return {"Authorization": "Bearer " + token}


def queue(client, token, key):
    result = client.post("/v1/requests", headers=headers(token), json={
        "client_operation_id": key, "mode": "action_requested", "project": "demo",
        "recipient": "bob", "packet": _packet("request", "Lifecycle boundary probe"),
    })
    assert result.status_code == 200, result.text
    return result.json()


def test_assembled_http_rotation_rename_disable_and_invalid_candidate(hub):
    client, runtime, tokens, remote, settings = hub
    policy, resolver = runtime.policy, runtime.token_resolver
    stale = Actor("alice")
    document = load(runtime)
    alice = next(p for p in document["participant"] if p["id"] == "alice")
    new_token = "replacement-alice-credential-more-than-32-bytes"
    alice.update(display_name="Alice renamed", aliases=["New alias"],
        token_sha256=hashlib.sha256(new_token.encode()).hexdigest())
    publish(runtime, document)
    assert runtime.refresh_participants()["status"] == "refreshed"
    assert runtime.policy is policy and runtime.token_resolver is resolver
    assert resolver(tokens["alice"]) is None
    assert resolver(new_token).actor_id == "alice"
    assert policy.resolve_recipient("New alias").participant_id == "alice"
    assert client.get("/v1/participants",headers=headers(tokens["alice"])).status_code == 401
    assert client.get("/v1/participants",headers=headers(new_token)).status_code == 200
    assert post(client,tokens["alice"],"memory.search",{"query":"probe"}).status_code == 401
    assert post(client,new_token,"memory.search",{"query":"probe"}).status_code == 200
    runtime.participants_file.write_text("[[participant]]\nid = [",encoding="utf-8")
    with pytest.raises(ConfigurationError):
        runtime.refresh_participants()
    assert resolver(new_token).actor_id == "alice"
    assert policy.resolve_recipient("New alias").participant_id == "alice"
    alice["enabled"] = False
    publish(runtime,document)
    runtime.refresh_participants()
    assert client.get("/v1/participants",headers=headers(new_token)).status_code == 401
    assert post(client,new_token,"memory.search",{"query":"probe"}).status_code == 401
    with pytest.raises(GremlinTownError):
        policy.authenticate(stale)


def test_stale_mcp_binding_observes_refresh_disabled_identity(hub,monkeypatch):
    import gremlin_town.mcp.auth as auth_module
    from gremlin_town.mcp.auth import _Verified
    _, runtime, _, _, _ = hub
    token = object()
    monkeypatch.setattr(auth_module,"get_access_token",lambda: token)
    runtime.mcp_auth._verified.set(_Verified(token,Actor("chatgpt-web"),frozenset({"bc:read"})))
    assert runtime.mcp_auth.actor().actor_id == "chatgpt-web"
    document=load(runtime)
    next(p for p in document["participant"] if p["id"]=="chatgpt-web")["enabled"]=False
    publish(runtime,document)
    runtime.refresh_participants()
    with pytest.raises(PermissionError):
        runtime.mcp_auth.actor()


def test_public_gateway_rotation_does_not_admit_other_private_identity(tmp_path):
    config,tokens=_write_runtime_config(tmp_path,public_actor_id="bob")
    runtime=build_runtime(config,provider=FakeTransport(),start_worker=False)
    with TestClient(runtime.app), TestClient(runtime.public_app) as public:
        document=load(runtime)
        bob=next(p for p in document["participant"] if p["id"]=="bob")
        token="rotated-bob-token-more-than-thirty-two-bytes"
        bob["token_sha256"]=hashlib.sha256(token.encode()).hexdigest()
        publish(runtime,document);runtime.refresh_participants()
        assert public.get("/v1/participants",headers=headers(token)).status_code==200
        assert public.get("/v1/participants",headers=headers(tokens["bob"])).status_code==401
        assert public.get("/v1/participants",headers=headers(tokens["alice"])).status_code==401


def test_removed_recipient_cannot_dispatch_but_history_remains(tmp_path):
    config,tokens=_write_runtime_config(tmp_path)
    provider=FakeTransport();runtime=build_runtime(config,provider=provider,start_worker=False)
    with TestClient(runtime.app) as client:
        queued=queue(client,tokens["alice"],"queued-before-removal")
        original=runtime.store.outbound_envelope(queued["operation_id"]).payload
        document=load(runtime)
        document["participant"]=[p for p in document["participant"] if p["id"]!="bob"]
        document["route"]=[]
        publish(runtime,document);runtime.refresh_participants()
        result=runtime.worker.dispatch_once()
        assert result.operation_state=="failed"
        assert provider.send_attempts==0
        assert runtime.store.outbound_envelope(queued["operation_id"]).payload==original
        assert runtime.policy.participant("bob",require_enabled=False).enabled is False
        read=client.get("/v1/requests/"+queued["request_id"],headers=headers(tokens["alice"]))
        assert read.status_code==200,read.text
        assert client.get("/v1/participants",headers=headers(tokens["bob"])).status_code==401


def test_refresh_waits_for_inflight_send_and_blocks_next_queued_send(tmp_path):
    entered,release,refresh_started=threading.Event(),threading.Event(),threading.Event()
    class BlockingTransport(FakeTransport):
        def send(self,envelope):
            entered.set()
            assert release.wait(5)
            return super().send(envelope)
    config,tokens=_write_runtime_config(tmp_path)
    provider=BlockingTransport();runtime=build_runtime(config,provider=provider,start_worker=False)
    with TestClient(runtime.app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        first=queue(client,tokens["alice"],"inflight-before-disable")
        second=queue(client,tokens["alice"],"queued-after-inflight")
        sending=pool.submit(runtime.worker.dispatch_once)
        assert entered.wait(5)
        document=load(runtime)
        next(p for p in document["participant"] if p["id"]=="bob")["enabled"]=False
        publish(runtime,document)
        def refresh():
            refresh_started.set()
            return runtime.refresh_participants()
        refreshing=pool.submit(refresh)
        try:
            assert refresh_started.wait(5)
            assert not refreshing.done()
            # Short policy reads still work while provider I/O is in flight.
            assert runtime.policy.participant("bob").enabled
        finally:
            release.set()
        assert sending.result(timeout=5).operation_state=="accepted"
        assert refreshing.result(timeout=5)["status"]=="refreshed"
        assert runtime.worker.dispatch_once().operation_state=="failed"
        assert provider.send_attempts==1
        assert runtime.store.operation(second["operation_id"]).operation_state=="failed"


def test_invalid_security_candidate_never_partially_publishes(hub):
    _,runtime,tokens,_,_=hub
    original=load(runtime)
    for mutation in ("duplicate-token","unknown-route","oauth-bearer"):
        document=json.loads(json.dumps(original))
        alice=next(p for p in document["participant"] if p["id"]=="alice")
        alice["display_name"]="Must not publish"
        if mutation=="duplicate-token":
            next(p for p in document["participant"] if p["id"]=="bob")["token_sha256"]=alice["token_sha256"]
        elif mutation=="unknown-route":
            document["route"][0]["destination"]="unknown"
        else:
            chatgpt=next(p for p in document["participant"] if p["id"]=="chatgpt-web")
            chatgpt["auth_mode"]="bearer"
            chatgpt["token_sha256"]=hashlib.sha256(b"different-bearer-token-with-enough-bytes").hexdigest()
        publish(runtime,document)
        with pytest.raises(ConfigurationError):
            runtime.refresh_participants()
        assert runtime.policy.participant("alice").display_name=="Alice"
        assert runtime.token_resolver(tokens["alice"]).actor_id=="alice"


def test_new_identity_joins_without_replacing_policy_or_listener(tmp_path):
    config,tokens=_write_runtime_config(tmp_path)
    runtime=build_runtime(config,provider=FakeTransport(),start_worker=False)
    with TestClient(runtime.app) as client:
        document=load(runtime)
        token="new-charlie-credential-with-more-than-32-bytes"
        document["participant"].append({"id":"charlie", "display_name":"Charlie", "address":"hub@example.test",
            "projects":["demo"], "sensitivities":["public-test"], "enabled":True,
            "token_sha256":hashlib.sha256(token.encode()).hexdigest()})
        document["route"].append({"project":"demo", "source":"charlie", "destination":"bob", "sensitivity":"public-test"})
        assert client.get("/v1/participants",headers=headers(token)).status_code==401
        publish(runtime,document)
        first=runtime.refresh_participants()
        assert first==runtime.refresh_participants()
        assert first["participant_count"]==4
        assert client.get("/v1/participants",headers=headers(token)).status_code==200
        assert queue(client,token,"new-participant-request")["operation_state"]=="queued"
