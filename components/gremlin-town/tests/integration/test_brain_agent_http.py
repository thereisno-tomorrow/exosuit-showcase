"""Real loopback HTTP over production GT composition and isolated canonical storage."""
from contextlib import contextmanager
from dataclasses import fields
import hashlib
import json
import socket
import threading
import time
from urllib.parse import urlsplit

import anyio
import httpx
import pytest
import uvicorn

from adapters.hermes.hub_client import HubClientConfig, HubHTTPSClient
from brain_core.github_storage import GitHubBrainHomeStorage, GitHubTransportError
from gremlin_town.cli.client import JsonApiClient
from gremlin_town.cli.main import _brain_request
from gremlin_town.contracts import Actor
from gremlin_town.mcp import brain_assembly
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_runtime import BrainOperation
from gremlin_town.mcp.brain_tools import build_brain_tools
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_mcp_runtime import mcp_config
from tests.mcp.test_brain_composition import _settings, LANE_B, FIRST_TIME
from tests.mcp.test_github_brain_provider import _HistoryTransport

@contextmanager
def running(runtime):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    server = uvicorn.Server(uvicorn.Config(runtime.app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}") as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()
        sock.close()

@pytest.fixture
def hub(tmp_path, monkeypatch):
    config, tokens = mcp_config(tmp_path)
    settings = _settings(tmp_path)
    policy = json.loads(settings.policy_file.read_text())
    policy["subject_scopes"] = {"alice": ["bc:read", "bc:write"], "bob": ["bc:read"], "operator": ["bc:read", "bc:write"]}
    owner = policy["owners"][0]
    owner["readers"] = ["alice", "bob"]
    owner["writers"] = ["alice"]
    owner["operations"] = [op.value for op in BrainOperation if op != BrainOperation.CONTEXT_RETRIEVE]
    settings.policy_file.write_text(json.dumps(policy))
    with config.open("a") as handle:
        handle.write("\n[brain_core]\nenabled = true\n")
        for field in fields(settings):
            value = getattr(settings, field.name)
            if value is not None:  # Unset optional credential fields stay absent.
                encoded = json.dumps(value) if isinstance(value, bool) else json.dumps(str(value).replace(chr(92), '/'))
                handle.write(f"{field.name} = {encoded}\n")
    remote = LANE_B.FakeGitHub()
    monkeypatch.setattr(brain_assembly, "build_brain_runtime", lambda configured, **kwargs: build_brain_runtime(configured, github_transport=_HistoryTransport(remote), operation_clock=lambda: FIRST_TIME, **kwargs))
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    with running(runtime) as client:
        yield client, runtime, tokens, remote, settings

def post(client, token, op, body):
    return client.post("/v1/brain/" + op, headers={"Authorization": "Bearer " + token}, json=body)

def save_body():
    return {
        "operation_id": "agent-save-1", "scope": "personal", "kind": "item", "level": "accepted",
        "said_by": "owner", "came_by": "direct", "words": "Prepare the Friday decision deck.",
        "title": "Friday delivery", "body": "Prepare the Friday decision deck.",
    }


def test_work_and_calendar_door_refuse_tier_b_with_fix(hub):
    client, runtime, tokens, remote, settings = hub
    head = remote.branch
    for operation in ("work", "calendar.remove", "project.save"):
        response = post(client, tokens["alice"], operation, {})
        assert response.status_code in (403, 422)
        error = response.json()["error"]
        assert error["field"] and error["fix"]
    assert remote.branch == head


def test_public_gateway_door_and_private_review_routes_are_not_available(hub):
    from gremlin_town.api.public import create_public_app
    from gremlin_town.api.brain import install_brain_routes
    from starlette.testclient import TestClient
    _, runtime, tokens, remote, _ = hub
    app = create_public_app(runtime.service, runtime.token_resolver, "alice")
    install_brain_routes(app, runtime)
    head = remote.branch
    with TestClient(app) as client:
        result = post(client, tokens["alice"], "work", {})
        assert result.status_code == 403 and result.json()["error"]["fix"]
        for path in ("/v1/review/responses", "/v1/moments/signals"):
            assert client.post(path, headers={"Authorization": "Bearer " + tokens["alice"]}, json={}).status_code == 404
    assert remote.branch == head


def test_public_listener_refuses_even_when_its_enrolled_actor_is_the_configured_door(hub):
    from gremlin_town.api.public import create_public_app
    from gremlin_town.api.brain import install_brain_routes
    from starlette.testclient import TestClient
    _, runtime, tokens, remote, _ = hub
    runtime.door_participant = "alice"
    runtime.brain_runtime.door_participant = "alice"
    runtime.brain_runtime._memory.door_participant = "alice"
    app = create_public_app(runtime.service, runtime.token_resolver, "alice")
    install_brain_routes(app, runtime)
    head = remote.branch
    with TestClient(app) as client:
        response = post(client, tokens["alice"], "work", {"operation_id": "public-door-match",
            "message": "Need wifi", "said_by": "owner", "came_by": "direct",
            "moves": [{"move": "new", "title": "Wifi"}]})
        assert response.status_code == 403 and response.json()["error"]["code"] == "ACCESS_DENIED"
        assert response.json()["error"]["fix"] and remote.branch == head
        remove = post(client, tokens["alice"], "calendar.remove", {})
        assert remove.status_code == 403 and remove.json()["error"]["fix"] == "remove it through Diablo"


def test_work_validation_names_failing_field(hub):
    client, runtime, tokens, remote, _ = hub
    runtime.door_participant = "alice"
    response = post(client, tokens["alice"], "work", {"operation_id": "schema", "message": "Need wifi",
        "said_by": "owner", "came_by": "direct", "moves": [{"move": "new", "title": "Wifi", "_context": {}}]})
    assert response.status_code == 422
    error = response.json()["error"]
    assert "moves" in error["field"] and error["fix"]


@pytest.fixture(params=["hermes-wa", "door-next"])
def named_door_hub(tmp_path, monkeypatch, request):
    """Give every role write authority so only the work gate separates the tiers."""
    config, _ = mcp_config(tmp_path)
    config.write_text(config.read_text().replace('client_id = "isolated-chatgpt-client"',
        '[[mcp.client]]\nparticipant = "fixture-oauth"\nname = "Fixture OAuth"\nclient_id = "isolated-chatgpt-client"'))
    actors = ("hermes-wa", "door-next", "chatgpt-web", "claude-web", "fleet-boss-grok", "local-codex")
    tokens = {actor: actor + "-fixture-bearer-token-with-at-least-thirty-two-bytes" for actor in actors}
    participant_text = (tmp_path / "participants.toml").read_text()
    participant_text = participant_text.replace("chatgpt-web", "fixture-oauth")
    for actor in actors:
        participant_text += '\n[[participant]]\nid = ' + json.dumps(actor) + '\naddress = "hub@example.test"\n' \
            + 'projects = ["demo"]\nsensitivities = ["public-test"]\ntoken_sha256 = "' \
            + hashlib.sha256(tokens[actor].encode()).hexdigest() + '"\n'
    participant_text += '\n[[route]]\nproject = "demo"\nsource = "alice"\ndestination = "hermes-wa"\nsensitivity = "public-test"\n'
    (tmp_path / "participants.toml").write_text(participant_text)
    settings = _settings(tmp_path)
    policy = json.loads(settings.policy_file.read_text())
    policy["subject_scopes"] = {actor: ["bc:read", "bc:write"] for actor in actors}
    owner = policy["owners"][0]
    owner.update(readers=list(actors), writers=list(actors),
        operations=[operation.value for operation in BrainOperation if operation != BrainOperation.CONTEXT_RETRIEVE])
    settings.policy_file.write_text(json.dumps(policy))
    with config.open("a") as handle:
        handle.write('\n[work]\ndoor_participant = ' + json.dumps(request.param) + '\n')
        handle.write('\n[review]\nenabled = true\nsender_participant = "alice"\ndelivery_participant = "hermes-wa"\nproject = "demo"\n')
        handle.write('why_log_file = ' + json.dumps((tmp_path / "why.sqlite3").as_posix()) + '\n[brain_core]\nenabled = true\n')
        for field in fields(settings):
            value = getattr(settings, field.name)
            if value is not None:
                handle.write(field.name + " = " + (json.dumps(value) if isinstance(value, bool)
                    else json.dumps(str(value).replace(chr(92), '/'))) + "\n")
    remote = LANE_B.FakeGitHub()
    monkeypatch.setattr(brain_assembly, "build_brain_runtime", lambda configured, **kwargs:
        build_brain_runtime(configured, github_transport=_HistoryTransport(remote), operation_clock=lambda: FIRST_TIME, **kwargs))
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    with running(runtime) as client:
        yield client, runtime, tokens, remote, request.param


def test_private_http_gate_uses_configured_door_and_refuses_item_changes_for_everyone(named_door_hub):
    client, runtime, tokens, remote, door = named_door_hub
    body = {"operation_id": "http-door", "message": "Need venue invoice", "said_by": "owner", "came_by": "direct",
        "moves": [{"move": "new", "title": "Venue invoice"}]}
    head = remote.branch
    for actor, token in tokens.items():
        if actor == door:
            continue
        response = post(client, token, "work", body)
        assert response.status_code == 403 and response.json()["error"]["fix"], response.text
        assert remote.branch == head
    accepted = post(client, tokens[door], "work", body)
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted", accepted.text
    record_id = accepted.json()["things"][0]["record_id"]
    saved_head = remote.branch
    for actor, token in tokens.items():
        response = post(client, token, "memory.change", {"operation_id": "item-change-" + actor,
            "scope": "personal", "record_id": record_id, "expected_sha256": "a" * 64,
            "said_by": "owner", "came_by": "direct", "words": "I changed venue invoice"})
        assert response.status_code == 403 and response.json()["error"]["fix"], response.text
        assert remote.branch == saved_head


def test_public_fleet_boss_has_no_work_item_change_or_review_routes(named_door_hub):
    from gremlin_town.api.public import create_public_app
    from gremlin_town.api.brain import install_brain_routes
    from starlette.testclient import TestClient
    client, runtime, tokens, remote, door = named_door_hub
    saved = post(client, tokens[door], "work", {"operation_id": "public-target", "message": "Need wifi",
        "said_by": "owner", "came_by": "direct", "moves": [{"move": "new", "title": "Wifi"}]}).json()
    app = create_public_app(runtime.service, runtime.token_resolver, "fleet-boss-grok")
    install_brain_routes(app, runtime)
    head = remote.branch
    with TestClient(app) as public:
        response = post(public, tokens["fleet-boss-grok"], "work", {})
        assert response.status_code == 403 and response.json()["error"]["fix"]
        response = post(public, tokens["fleet-boss-grok"], "memory.change", {"operation_id": "public-change",
            "scope": "personal", "record_id": saved["things"][0]["record_id"], "expected_sha256": "a" * 64,
            "said_by": "owner", "came_by": "direct", "words": "Change wifi"})
        assert response.status_code == 403 and response.json()["error"]["fix"]
        for path in ("/v1/review/responses", "/v1/moments/signals"):
            assert public.post(path, headers={"Authorization": "Bearer " + tokens["fleet-boss-grok"]}, json={}).status_code == 404
    assert remote.branch == head


def test_marker_validates_brain_writes_and_retains_owner_guards(named_door_hub):
    client, runtime, tokens, remote, door = named_door_hub
    provider = runtime.brain_runtime._memory
    provider.replay_marker.write_text("fixture replay")
    head = remote.branch
    bodies = {"continuity.start": {"operation_id": "marker-note", "session_id": "fixture-session", "intent": "Need wifi", "source_references": []},
        "decision.record": {"operation_id": "marker-decision", "scope": "personal", "title": "Wifi",
            "choice": "Pay wifi", "because": "Need connection", "forecloses": "Skipping it",
            "governing_scope": "Personal", "encoded_in": "UNENCODED", "completeness_check": "Owner and choice given"},
        "work": {"operation_id": "marker-work", "message": "Need wifi", "said_by": "owner", "came_by": "direct",
            "moves": [{"move": "new", "title": "Wifi"}]},
        "memory.save": {**save_body(), "operation_id": "marker-save"}}
    for operation, body in bodies.items():
        response = post(client, tokens[door], operation, body)
        assert response.status_code == 200 and response.json()["status"] == "validated", response.text
        assert remote.branch == head
    excluded = post(client, tokens[door], "continuity.start", {**bodies["continuity.start"], "scope": "cross-project"})
    assert excluded.status_code == 403 and excluded.json()["error"]["fix"]
    assert remote.branch == head and not runtime.review_why_log.history()
    assert not list(runtime.brain.decision_staging_dir.glob("*.json"))


def test_public_brain_gateway_retains_its_enrolled_actor_boundary(hub):
    from gremlin_town.api.public import create_public_app
    from gremlin_town.api.brain import install_brain_routes
    from starlette.testclient import TestClient
    _, runtime, tokens, remote, _ = hub
    app = create_public_app(runtime.service, runtime.token_resolver, "alice")
    install_brain_routes(app, runtime)
    with TestClient(app) as client:
        assert post(client, tokens["bob"], "memory.save", save_body()).status_code == 401
        assert post(client, tokens["alice"], "memory.save", save_body()).status_code == 200
        for path in ("/login", "/overview", "/mcp", "/health/live"):
            assert client.get(path).status_code == 404

def test_http_shared_save_read_search_continuity_retry_and_provenance(hub):
    client, runtime, tokens, remote, settings = hub
    saved = post(client, tokens["alice"], "memory.save", save_body())
    assert saved.status_code == 200, saved.text
    receipt = saved.json()
    assert receipt["status"] == "accepted"
    read = post(client, tokens["bob"], "memory.read", {"scope":"personal", "record_id":receipt["record_id"]})
    assert read.status_code == 200, read.text
    assert read.json()["state"] == "open"
    search = post(client, tokens["bob"], "memory.search", {"query":"Friday"})
    assert search.status_code == 200 and len(search.json()["matches"]) == 1
    retry = post(client, tokens["alice"], "memory.save", save_body())
    assert retry.status_code == 200 and retry.json()["status"] == "duplicate"
    conflict = post(client, tokens["alice"], "memory.save", {**save_body(), "body":"Different input"})
    assert conflict.status_code == 409, conflict.text
    start = {"operation_id":"continuity-start-1", "session_id":"agent-session", "intent":"Prepare Friday delivery", "source_references":[]}
    response = post(client,tokens["alice"],"continuity.start",start)
    assert response.status_code == 200, response.text
    assert post(client,tokens["alice"],"continuity.start",start).json()["receipt"]["status"] == "duplicate"
    assert post(client,tokens["alice"],"continuity.start",{**start,"intent":"Changed"}).status_code == 409
    recovery = post(client,tokens["bob"],"continuity.recover",{})
    assert recovery.status_code == 200 and recovery.json()["intent"] == start["intent"]
    storage = GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY,branch=LANE_B.BRANCH,token=LANE_B.TOKEN,transport=remote)
    for item in storage.snapshot_records(kinds=frozenset({"item", "continuity"})).records:
        assert item.record.evidence[0].actor == "alice"
        if item.record.kind == "item":
            # v2's structured source.recorded_by carries attribution; the evidence excerpt
            # is instead the exact quoted words (R2), not also asked to carry attribution.
            assert item.record.source["recorded_by"] == "alice"
        else:
            assert '"executing_agent":"alice"' in item.record.evidence[0].excerpt
            assert '"recipient_hub_id":null' in item.record.evidence[0].excerpt

def test_denials_and_revocation_precede_provider_effects(hub):
    client, runtime, tokens, remote, settings = hub
    calls = []
    async def forbidden(*args):
        calls.append(args)
        raise AssertionError("provider reached")
    runtime.brain_runtime._memory.save = forbidden
    assert client.post("/v1/brain/memory.save",json=save_body()).status_code == 401
    assert post(client,"invalid","memory.save",save_body()).status_code == 401
    assert post(client,tokens["operator"],"memory.save",save_body()).status_code == 403
    assert post(client,tokens["bob"],"memory.save",save_body()).status_code == 403
    assert post(client,tokens["alice"],"memory.save",{**save_body(),"scope":"cross-project"}).status_code == 403
    assert post(client,tokens["alice"],"memory.save",{**save_body(),"actor":"bob"}).status_code == 422
    runtime.policy.set_enabled("alice",False)
    assert post(client,tokens["alice"],"memory.save",save_body()).status_code in {401,403}
    runtime.policy.set_enabled("alice",True)
    policy=json.loads(settings.policy_file.read_text());policy["owners"][0]["writers"]=[]
    settings.policy_file.write_text(json.dumps(policy))
    assert post(client,tokens["alice"],"memory.save",save_body()).status_code == 403
    assert calls == []

def test_guessed_preferences_refused_owner_worded_ones_accepted_and_unknown_selectors_rejected(hub):
    client, runtime, tokens, remote, settings = hub
    accepted = post(
        client, tokens["alice"], "memory.save",
        {**save_body(), "kind": "preference", "operation_id": "agent-preference-accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "active"
    for body in (
        {
            **save_body(), "kind": "preference", "said_by": "agent", "came_by": "agent_guess",
            "speaker": "fixture-agent", "operation_id": "agent-preference-denied",
        },
        {**save_body(),"repository":"elsewhere"},
    ):
        response=post(client,tokens["alice"],"memory.save",body)
        assert response.status_code == 422, response.text
    policy=json.loads(settings.policy_file.read_text());policy.pop("subject_scopes")
    settings.policy_file.write_text(json.dumps(policy))
    assert post(client,tokens["alice"],"memory.save",save_body()).status_code == 403


def test_decision_full_fields_and_lost_reply_reconcile(hub):
    client, runtime, tokens, remote, settings = hub
    remote.lost_apply_once = True
    body = {"operation_id":"decision-http-1", "scope":"personal", "title":"Friday reporting", "choice":"Use Friday for reporting", "because":"One review window", "forecloses":"Daily reporting", "governing_scope":"Personal weekly reporting", "encoded_in":"Weekly routine", "completeness_check":"Owner and timing explicit"}
    response = post(client,tokens["alice"],"decision.record",body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["durability_receipt"]["writer_id"] == "brain-core-shared-durability-writer/v1"
    assert response.json()["transport_evidence"]["canonical_readback"] is True
    assert response.json()["transport_evidence"]["network_calls"] is None
    retry = post(client,tokens["alice"],"decision.record",body)
    assert retry.status_code == 200 and retry.json()["status"] == "duplicate", retry.text
    assert post(client,tokens["alice"],"decision.record",{**body,"because":"Changed"}).status_code == 409
    storage = GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY,branch=LANE_B.BRANCH,token=LANE_B.TOKEN,transport=remote)
    records=storage.snapshot_records(kinds=frozenset({"decision"})).records
    assert len(records)==1
    assert records[0].record.evidence[0].actor == "nic"
    assert records[0].record.evidence[1].actor == "alice"
    # A fresh provider has no in-process locator cache; readback must use canonical storage.
    fresh = build_brain_runtime(settings, github_transport=_HistoryTransport(remote), operation_clock=lambda: FIRST_TIME)
    original = runtime.brain_runtime._memory
    runtime.brain_runtime._memory = fresh._memory
    try:
        read = post(client,tokens["bob"],"memory.read",{"scope":"personal", "record_id":response.json()["record_id"]})
        assert read.status_code == 200, read.text
        assert read.json()["content_sha256"] == response.json()["content_sha256"]
        assert read.json()["canonical_record"]["evidence"][1]["actor"] == "alice"
        assert read.json()["canonical_record"]["evidence"][0]["actor"] == "nic"
    finally:
        runtime.brain_runtime._memory = original
        import anyio
        anyio.run(fresh.aclose)
    assert '"executing_agent":"alice"' in records[0].record.evidence[1].excerpt


def test_same_key_concurrent_http_writes_have_one_record(hub):
    from concurrent.futures import ThreadPoolExecutor
    client, runtime, tokens, remote, settings = hub
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(lambda _:post(client,tokens["alice"],"memory.save",save_body()),range(2)))
    assert all(reply.status_code==200 for reply in replies), [r.text for r in replies]
    assert {reply.json()["status"] for reply in replies} == {"accepted","duplicate"}
    assert len({reply.json()["record_id"] for reply in replies})==1


def test_http_calls_and_early_refusals_reach_the_usage_log(hub):
    from gremlin_town.mcp.brain_usage import read_usage
    client, runtime, tokens, _, settings = hub
    assert post(client, tokens["alice"], "memory.save", save_body()).status_code == 200
    assert post(client, "not-a-token", "memory.search", {"query": "x"}).status_code == 401
    assert post(client, tokens["alice"], "memory.search", {"query": "x", "unknown": 1}).status_code == 422
    rows = read_usage(settings.decision_staging_dir / "brain-usage.sqlite3")
    assert [(r["actor"], r["operation"], r["outcome"], r["error_code"]) for r in rows] == [
        ("alice", "memory.save", "ok", None),
        ("unauthenticated", "memory.search", "error", "UNAUTHORIZED"),
        ("alice", "memory.search", "error", "brain.invalid_argument"),
    ]
    assert all(r["transport"] == "http" for r in rows)
    assert b"Friday decision deck" not in (settings.decision_staging_dir / "brain-usage.sqlite3").read_bytes()


def test_lost_reply_save_lands_once_and_replay_confirms(hub):
    client, runtime, tokens, remote, settings = hub
    remote.lost_apply_once = True
    body = save_body()

    first = post(client, tokens["alice"], "memory.save", body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "accepted"
    assert first.json()["remote_observed"] is True

    replay = post(client, tokens["alice"], "memory.save", body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "duplicate"
    assert replay.json()["record_id"] == first.json()["record_id"]
    assert replay.json()["remote_observed"] is True

    storage = GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=remote)
    records = storage.snapshot_records(kinds=frozenset({"item"})).records
    assert len(records) == 1
    assert records[0].record.id == first.json()["record_id"]


def test_brain_home_down_says_so_and_saves_nothing(hub):
    client, runtime, tokens, remote, settings = hub
    initial_branch = remote.branch
    original_request = remote.request

    def down(*args, **kwargs):
        raise GitHubTransportError("fixture_down", "Brain Home fixture is unreachable")

    remote.request = down
    try:
        response = post(client, tokens["alice"], "memory.save", save_body())
    finally:
        remote.request = original_request

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "brain.capability.unavailable"
    assert "Brain Home" in error["message"] and "nothing was saved" in error["message"]
    assert error["retryable"] is True
    assert remote.branch == initial_branch
    storage = GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=remote)
    assert storage.snapshot_records(kinds=frozenset({"item"})).records == ()


def test_commitment_saved_on_every_route_reads_back(hub):
    client, runtime, tokens, remote, settings = hub
    title = "Send Sam the deck"
    body_text = "I'll send Sam the deck tomorrow."
    base_url = str(client.base_url).rstrip("/")

    async def allow() -> None:
        return None

    mcp_tools = build_brain_tools(
        runtime.brain_runtime, lambda: Actor("alice"),
        scopes_provider=lambda: frozenset({"bc:read", "bc:write"}),
        authorize=allow,
    )
    mcp_save = next(tool for tool in mcp_tools if tool.name == "bc_memory_save")
    mcp_call = anyio.run(mcp_save.run, {
        "operation_id": "commitment-route-mcp", "scope": "personal",
        "kind": "item", "level": "accepted", "owed_to": "Sam", "said_by": "owner", "came_by": "direct", "words": body_text, "title": title, "body": body_text,
    })
    assert not mcp_call.isError, mcp_call.structuredContent
    mcp_result = mcp_call.structuredContent
    assert mcp_result["status"] == "accepted" and mcp_result["remote_observed"] is True

    cli_client = JsonApiClient(base_url, tokens["alice"])
    cli_result = _brain_request(cli_client, "memory.save", {
        "operation_id": "commitment-route-cli", "scope": "personal",
        "kind": "item", "level": "accepted", "owed_to": "Sam", "said_by": "owner", "came_by": "direct", "words": body_text, "title": title, "body": body_text,
    })
    assert cli_result["status"] == "accepted" and cli_result["remote_observed"] is True

    class _LoopbackRequester:
        def request(self, method, url, **kwargs):
            return client.request(method, urlsplit(url).path, **kwargs)

    hermes_client = HubHTTPSClient(
        HubClientConfig("https://fixture.test", tokens["alice"]), client=_LoopbackRequester(),
    )
    hermes_result = hermes_client.call("memory.save", {
        "operation_id": "commitment-route-hermes", "scope": "personal",
        "kind": "item", "level": "accepted", "owed_to": "Sam", "said_by": "owner", "came_by": "direct", "words": body_text, "title": title, "body": body_text,
    })
    assert hermes_result["status"] == "accepted" and hermes_result["remote_observed"] is True

    for label, result in (("mcp", mcp_result), ("cli", cli_result), ("hermes", hermes_result)):
        read = post(client, tokens["bob"], "memory.read", {"scope": "personal", "record_id": result["record_id"]})
        assert read.status_code == 200, (label, read.text)
        assert read.json()["content_sha256"] == result["content_sha256"], label


def test_note_survives_unclosed_chat_and_next_agent_recovers_it(hub):
    client, runtime, tokens, remote, settings = hub
    start = post(client, tokens["alice"], "continuity.start", {
        "operation_id": "note-start-1", "session_id": "task-friday-deck",
        "intent": "Ship the Friday deck", "source_references": [],
    })
    assert start.status_code == 200, start.text
    appended = post(client, tokens["alice"], "continuity.append", {
        "operation_id": "note-append-1", "session_id": "task-friday-deck",
        "event": "next_step", "value": "Send Sam the outline",
    })
    assert appended.status_code == 200, appended.text

    # Agent A's chat ends here without closing. Agent B recovers this exact task's note
    # by session_id, not just "the latest note" (there could be several open at once).
    recovered = post(client, tokens["bob"], "continuity.recover", {"session_id": "task-friday-deck"})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["session_id"] == "task-friday-deck"
    assert recovered.json()["next_step"] == "Send Sam the outline"


def test_switching_agents_continues_without_reexplaining(hub):
    client, runtime, tokens, remote, settings = hub
    created = client.post(
        "/v1/requests", headers={"Authorization": "Bearer " + tokens["alice"]},
        json={
            "client_operation_id": "handoff-1", "mode": "action_requested", "project": "demo",
            "recipient": "bob", "packet": {
                "schema_version": 1, "kind": "request", "objective": "Prepare the Friday review",
                "context": "", "constraints": {}, "sources": [], "next_action": "", "expected_reply": "",
                "sensitivity": "public-test",
            },
        },
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["request_id"]
    session_id = f"gt-{request_id}"

    # This fixture's default policy only grants alice Brain Home write access;
    # give bob (the recipient doing the work) write access to keep its own note too.
    policy = json.loads(settings.policy_file.read_text())
    policy["owners"][0]["writers"] = ["alice", "bob"]
    policy["subject_scopes"]["bob"] = ["bc:read", "bc:write"]
    settings.policy_file.write_text(json.dumps(policy))

    # Agent A (bob, the recipient) starts a running note for this task as it works.
    started = post(client, tokens["bob"], "continuity.start", {
        "operation_id": "handoff-note-start", "session_id": session_id,
        "intent": "Prepare the Friday review", "source_references": [],
    })
    assert started.status_code == 200, started.text
    appended = post(client, tokens["bob"], "continuity.append", {
        "operation_id": "handoff-note-step-1", "session_id": session_id,
        "event": "next_step", "value": "Draft the outline",
    })
    assert appended.status_code == 200, appended.text

    # Agent B lists what's still open and reads the note by the gt-<request_id> convention.
    listing = client.get("/v1/requests?open=true", headers={"Authorization": "Bearer " + tokens["bob"]})
    assert listing.status_code == 200, listing.text
    assert request_id in {item["request_id"] for item in listing.json()["items"]}
    recovered = post(client, tokens["bob"], "continuity.recover", {"session_id": session_id})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["next_step"] == "Draft the outline"

    # Continuing lands on the same note, not a new one.
    second_append = post(client, tokens["bob"], "continuity.append", {
        "operation_id": "handoff-note-step-2", "session_id": session_id,
        "event": "next_step", "value": "Send Sam the outline",
    })
    assert second_append.status_code == 200, second_append.text
    final = post(client, tokens["bob"], "continuity.recover", {"session_id": session_id})
    assert final.status_code == 200, final.text
    assert final.json()["session_id"] == recovered.json()["session_id"] == session_id
    assert final.json()["next_step"] == "Send Sam the outline"
