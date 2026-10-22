from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent))
from brain_local_harness import (  # noqa: E402
    BRAIN_CORE_SRC,
    DENIED_CANARY,
    SESSION_ID,
    WRITER_TOKEN,
    build_fixture,
    build_local_memory_provider,
    start_connector_process,
    start_fault_connector_process,
    sha256_text,
)

import anyio  # noqa: E402
from gremlin_town.mcp.brain_runtime import (  # noqa: E402
    BrainOperation,
    ContextQuery,
    ContextResult,
    ContinuityAppendRequest,
    ContinuityCloseRequest,
    ContinuityListRequest,
    ContinuityRecoverRequest,
    ContinuityStartRequest,
    MemorySaveRequest,
    OwnerAuthorization,
    OwnerSelector,
)
from gremlin_town.mcp.local_project_connector import (  # noqa: E402
    ConnectorError,
    ConnectorUnavailable,
    LocalConnectorContextProvider,
    LocalConnectorContinuityProvider,
    LocalProjectConnectorClient,
    LocalProjectContextCapability,
)


def operation_input_sha256(operation_kind: str, command: dict[str, object]) -> str:
    exact_input = {
        "operation_id": command["operation_id"],
        "operation_kind": operation_kind,
        "command": command,
    }
    encoded = json.dumps(
        exact_input,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def authorization(project_id: str, operation: BrainOperation, access: str) -> OwnerAuthorization:
    selector = OwnerSelector("project", project_id)
    return OwnerAuthorization(
        operation=operation,
        access=access,  # type: ignore[arg-type]
        selector=selector,
        owner_id=project_id,
        owner_locator=f"vault://{project_id}",
        policy_revision="fixture-policy-v1",
        capability=LocalProjectContextCapability(project_id, SESSION_ID),
    )


def test_actual_brain_discovery_retrieval_grants_and_lane_a_context_provider(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        client = fixture.client(running.base_url)
        raw = client.retrieve_context(
            fixture.git_project_id,
            "telemetry request reference denied",
            SESSION_ID,
            max_sources=12,
            max_source_tokens=1000,
        )
        assert raw["repository_version"] == "fixture-worktree-v2-uncommitted"
        assert raw["freshness"] == "current_pc"
        assert {item["evidence_id"] for item in raw["sources"]} == {"E_TRACKED", "E_LOCAL"}
        assert set(raw["ingested_ids"]) == {"E_TRACKED", "E_LOCAL"}
        assert "E_DENIED" not in json.dumps(raw)
        assert DENIED_CANARY not in json.dumps(raw)
        assert any(item["code"] == "brain.grant.wrong_path" for item in raw["exclusions"])
        assert all(len(item["content_sha256"]) == 64 for item in raw["sources"])

        provider = LocalConnectorContextProvider(client)
        result = asyncio.run(
            provider.retrieve(
                authorization(
                    fixture.git_project_id,
                    BrainOperation.CONTEXT_RETRIEVE,
                    "read",
                ),
                ContextQuery("telemetry request reference", max_sources=1, max_source_tokens=200),
            )
        )
        assert isinstance(result, ContextResult)
        assert result.freshness == "current"
        assert len(result.sources) == 1
        assert result.sources[0].source_version == "fixture-worktree-v2-uncommitted"
        assert result.sources[0].owner_locator == f"vault://{fixture.git_project_id}"
        assert len(result.result_sha256) == 64


def test_a_new_chat_finds_where_every_earlier_chat_stopped_in_the_project(tmp_path: Path) -> None:
    """ROA-15 (28 Sep 2026): project notes stay in the project's folder, and any agent on the
    project can list every open note there and read any one by its task, not only its own."""

    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        provider = LocalConnectorContinuityProvider(
            LocalProjectConnectorClient(running.base_url, WRITER_TOKEN)
        )
        project = fixture.git_project_id

        async def scenario():
            for key, session, intent in (
                ("earlier-chat-a", "task-a", "Draft the sitrep spec"),
                ("earlier-chat-b", "task-b", "Check the connector update"),
            ):
                await provider.start(
                    authorization(project, BrainOperation.CONTINUITY_START, "write"),
                    ContinuityStartRequest(key, session, intent, ("fixture://source/current",), "Keep going"),
                )
            listed = await provider.list_open(
                authorization(project, BrainOperation.CONTINUITY_LIST, "read"),
                ContinuityListRequest(limit=10),
            )
            earlier = await provider.recover(
                authorization(project, BrainOperation.CONTINUITY_RECOVER, "read"),
                ContinuityRecoverRequest(1000, "task-a"),
            )
            return listed, earlier

        listed, earlier = asyncio.run(scenario())
        assert {item.session_id for item in listed.items} == {"task-a", "task-b"}
        assert earlier.session_id == "task-a" and earlier.intent == "Draft the sitrep spec"


def test_typed_continuity_provider_uses_real_network_and_brain_core(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        provider = LocalConnectorContinuityProvider(
            LocalProjectConnectorClient(running.base_url, WRITER_TOKEN)
        )

        async def scenario():
            started = await provider.start(
                authorization(fixture.git_project_id, BrainOperation.CONTINUITY_START, "write"),
                ContinuityStartRequest(
                    "typed-start-1",
                    SESSION_ID,
                    "Exercise typed continuity over loopback",
                    ("fixture://source/current",),
                    "Append a blocker",
                ),
            )
            appended = await provider.append(
                authorization(fixture.git_project_id, BrainOperation.CONTINUITY_APPEND, "write"),
                ContinuityAppendRequest(
                    "typed-append-1", SESSION_ID, "blocker", "fixture blocker"
                ),
            )
            listed = await provider.list_open(
                authorization(fixture.git_project_id, BrainOperation.CONTINUITY_LIST, "read"),
                ContinuityListRequest(limit=10),
            )
            recovered = await provider.recover(
                authorization(fixture.git_project_id, BrainOperation.CONTINUITY_RECOVER, "read"),
                ContinuityRecoverRequest(1000),
            )
            closed = await provider.close(
                authorization(fixture.git_project_id, BrainOperation.CONTINUITY_CLOSE, "write"),
                ContinuityCloseRequest("typed-close-1", SESSION_ID, recovered=True),
            )
            return started, appended, listed, recovered, closed

        started, appended, listed, recovered, closed = asyncio.run(scenario())
        assert started.state == appended.state == "open"
        assert started.receipt is not None and started.receipt.remote_observed is False
        assert appended.receipt is not None and appended.receipt.operation_id == "typed-append-1"
        assert len(listed.items) == 1
        assert listed.items[0].session_id == SESSION_ID
        assert listed.items[0].intent == "Exercise typed continuity over loopback"
        assert listed.items[0].next_step == "Append a blocker"
        assert listed.items[0].state == "open"
        assert recovered.blockers == ("fixture blocker",)
        assert recovered.next_step == "Append a blocker"
        assert closed.state == "recovered"


def test_fresh_process_recovery_idempotent_operations_and_project_isolation(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    start = ContinuityStartRequest(
        "stable-start-1",
        SESSION_ID,
        "Survive a connector process restart",
        ("fixture://source/current",),
        "Recover from a fresh process",
    )
    append = ContinuityAppendRequest(
        "stable-append-1", SESSION_ID, "blocker", "PC connector rollout is not enrolled"
    )
    close = ContinuityCloseRequest("stable-close-1", SESSION_ID, recovered=True)

    first = start_connector_process(fixture, "first")
    try:
        client = LocalProjectConnectorClient(first.base_url, WRITER_TOKEN)
        start_result = client.start_continuity(fixture.git_project_id, start)
        append_result = client.append_continuity(fixture.git_project_id, append)
        assert start_result["state"] == append_result["state"] == "open"
    finally:
        first_stdout, first_stderr = first.stop()
    assert first_stdout == ""
    assert first_stderr == ""

    second = start_connector_process(fixture, "second")
    try:
        client = LocalProjectConnectorClient(second.base_url, WRITER_TOKEN)
        replay = client.append_continuity(fixture.git_project_id, append)
        assert replay == append_result

        conflicting = ContinuityAppendRequest(
            append.operation_id, SESSION_ID, "blocker", "different input under same key"
        )
        with pytest.raises(ConnectorError) as conflict:
            client.append_continuity(fixture.git_project_id, conflicting)
        assert conflict.value.code == "OPERATION_INPUT_CONFLICT"

        recovered_git = client.recover_continuity(fixture.git_project_id, max_tokens=1000)
        assert recovered_git["session_id"] == SESSION_ID
        assert recovered_git["blockers"] == ["PC connector rollout is not enrolled"]

        with pytest.raises(ConnectorError) as isolated_empty:
            client.recover_continuity(fixture.non_git_project_id, max_tokens=1000)
        assert isolated_empty.value.code == "brain.continuity.empty"

        other_start = ContinuityStartRequest(
            start.operation_id,
            "fixture-session-other-project",
            "Independent non-Git continuity",
            ("fixture://nongit/current",),
            "Remain isolated",
        )
        other = client.start_continuity(fixture.non_git_project_id, other_start)
        assert other["session_id"] == "fixture-session-other-project"
        recovered_other = client.recover_continuity(fixture.non_git_project_id, max_tokens=1000)
        assert recovered_other["intent"] == "Independent non-Git continuity"
        assert recovered_other["record_id"] != recovered_git["record_id"]

        closed = client.close_continuity(fixture.git_project_id, close)
        assert closed["state"] == "recovered"
    finally:
        second_stdout, second_stderr = second.stop()
    assert second_stdout == ""
    assert second_stderr == ""

    third = start_connector_process(fixture, "third")
    try:
        client = LocalProjectConnectorClient(third.base_url, WRITER_TOKEN)
        close_replay = client.close_continuity(fixture.git_project_id, close)
        assert close_replay == closed
        recovered_again = client.recover_continuity(fixture.git_project_id, max_tokens=1000)
        assert recovered_again["state"] == "recovered"
        assert recovered_again["session_id"] == SESSION_ID
        recovered_other_again = client.recover_continuity(
            fixture.non_git_project_id, max_tokens=1000
        )
        assert recovered_other_again["session_id"] == "fixture-session-other-project"
    finally:
        third_stdout, third_stderr = third.stop()
    assert third_stdout == ""
    assert third_stderr == ""


def test_project_note_recovered_through_local_connector(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        client = LocalProjectConnectorClient(running.base_url, WRITER_TOKEN)
        client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
            "session-a-start", "session-a", "Ship step A", ("fixture://source/current",), "Write the spec",
        ))
        client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
            "session-b-start", "session-b", "Ship step B", ("fixture://source/current",), "Build the wheel",
        ))

        by_session_a = client.recover_continuity(fixture.git_project_id, session_id="session-a", max_tokens=1000)
        assert by_session_a["session_id"] == "session-a"
        assert by_session_a["intent"] == "Ship step A"
        assert by_session_a["next_step"] == "Write the spec"

        by_session_b = client.recover_continuity(fixture.git_project_id, session_id="session-b", max_tokens=1000)
        assert by_session_b["session_id"] == "session-b"
        assert by_session_b["intent"] == "Ship step B"

        with pytest.raises(ConnectorError) as missing:
            client.recover_continuity(fixture.git_project_id, session_id="missing-session", max_tokens=1000)
        assert missing.value.code == "brain.continuity.empty"


def test_continuity_list_orders_newest_first_excludes_closed_and_is_project_isolated(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        client = LocalProjectConnectorClient(running.base_url, WRITER_TOKEN)

        client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
            "list-start-a", "list-session-a", "List test A", ("fixture://source/current",), "Do A",
        ))
        client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
            "list-start-b", "list-session-b", "List test B", ("fixture://source/current",), "Do B",
        ))
        client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
            "list-start-c", "list-session-c", "List test C", ("fixture://source/current",), "Do C",
        ))
        # A plain close (recovered=False) reaches "closed", which
        # brain_core.continuity.list_continuity excludes from the open list.
        client.close_continuity(
            fixture.git_project_id,
            ContinuityCloseRequest("list-close-a", "list-session-a", recovered=False),
        )
        client.start_continuity(fixture.non_git_project_id, ContinuityStartRequest(
            "list-start-other", "list-session-other", "Independent project note",
            ("fixture://nongit/current",), "Stay isolated",
        ))

        listed = client.list_continuity(fixture.git_project_id, limit=10)
        assert [item["session_id"] for item in listed["items"]] == ["list-session-c", "list-session-b"]
        assert all(item["state"] == "open" for item in listed["items"])
        newest = listed["items"][0]
        assert newest["intent"] == "List test C"
        assert newest["next_step"] == "Do C"
        assert newest["record_id"]
        assert "T" in newest["updated_at"]  # ISO 8601 timestamp, JSON-safe

        limited = client.list_continuity(fixture.git_project_id, limit=1)
        assert [item["session_id"] for item in limited["items"]] == ["list-session-c"]

        other_only = client.list_continuity(fixture.non_git_project_id, limit=10)
        assert [item["session_id"] for item in other_only["items"]] == ["list-session-other"]

        # Closing "recovered" keeps the note listed (just in a different state),
        # unlike a plain close above.
        recovered_note = client.close_continuity(
            fixture.git_project_id,
            ContinuityCloseRequest("list-close-b", "list-session-b", recovered=True),
        )
        assert recovered_note["state"] == "recovered"
        after_recovery = client.list_continuity(fixture.git_project_id, limit=10)
        by_session = {item["session_id"]: item["state"] for item in after_recovery["items"]}
        assert by_session == {"list-session-c": "open", "list-session-b": "recovered"}


def test_project_note_names_its_task_through_the_local_connector(tmp_path: Path) -> None:
    """N12b on the PC-local route: task_id travels to the project's own note and back through
    list and recover; a start that names no task sends no task_id at all, so an older connector
    still takes it."""

    task_id = "brn_20260928_110000_abcd1234"
    fixture = build_fixture(tmp_path)
    with fixture.running() as running:
        client = LocalProjectConnectorClient(running.base_url, WRITER_TOKEN)
        client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
            "task-start-a", "task-session-a", "Linked note", ("fixture://source/current",), "Do A", task_id,
        ))
        client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
            "task-start-b", "task-session-b", "Plain note", ("fixture://source/current",), "Do B",
        ))
        listed = {item["session_id"]: item["task_id"] for item in client.list_continuity(fixture.git_project_id)["items"]}
        assert listed == {"task-session-a": task_id, "task-session-b": None}
        assert client.recover_continuity(fixture.git_project_id, session_id="task-session-a")["task_id"] == task_id
        assert "task_id" not in client.recover_continuity(fixture.git_project_id, session_id="task-session-b")

        with pytest.raises(ConnectorError) as refused:
            client.start_continuity(fixture.git_project_id, ContinuityStartRequest(
                "task-start-bad", "task-session-bad", "Bad link", ("fixture://source/current",), "Do C",
                "not-a-record",
            ))
        assert refused.value.code == "brain.contract.invalid"

    sent: list[dict[str, object]] = []

    class Recording(LocalProjectConnectorClient):
        def call(self, route, body):  # type: ignore[override]
            sent.append(body)
            return {}

    recording = Recording("http://127.0.0.1:9", WRITER_TOKEN)
    recording.start_continuity("vault_x", ContinuityStartRequest("p", "s", "i", (), "n"))
    recording.start_continuity("vault_x", ContinuityStartRequest("p", "s", "i", (), "n", task_id))
    assert "task_id" not in sent[0]
    assert sent[1]["task_id"] == task_id


def test_fresh_process_reconciles_crash_after_canonical_effect_before_receipt(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    request = ContinuityStartRequest(
        "fault-after-effect-1",
        SESSION_ID,
        "Reconcile a committed effect after process exit",
        ("fixture://source/current",),
        "Replay from a fresh process",
    )
    command: dict[str, object] = {
        "project_id": fixture.git_project_id,
        "operation_id": request.operation_id,
        "session_id": request.session_id,
        "intent": request.intent,
        "source_references": list(request.source_references),
        "next_step": request.next_step,
    }
    receipt_path = (
        fixture.git_continuity_root
        / ".connector-operations"
        / f"{sha256_text(request.operation_id)}.json"
    )

    crashed = start_fault_connector_process(
        fixture,
        "fault-after-effect",
        fault_stage="after_effect",
    )
    try:
        client = LocalProjectConnectorClient(crashed.base_url, WRITER_TOKEN)
        with pytest.raises(ConnectorUnavailable) as unavailable:
            client.start_continuity(fixture.git_project_id, request)
        assert unavailable.value.code == "PC_UNAVAILABLE"
        assert crashed.process.wait(timeout=5) == 97
    finally:
        crashed_stdout, crashed_stderr = crashed.stop()
    assert crashed_stdout == ""
    assert crashed_stderr == ""

    pending = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert pending["state"] == "pending"
    assert pending["operation_id"] == request.operation_id
    assert pending["operation_kind"] == "continuity.start"
    assert pending["input_sha256"] == operation_input_sha256("continuity.start", command)
    canonical_records = tuple(fixture.git_continuity_root.glob("records/continuity/**/*.md"))
    assert len(canonical_records) == 1

    fresh = start_connector_process(fixture, "fault-reconcile-fresh")
    try:
        client = LocalProjectConnectorClient(fresh.base_url, WRITER_TOKEN)
        reconciled = client.start_continuity(fixture.git_project_id, request)
        assert reconciled["reconciled_from_pending"] is True
        assert reconciled["receipt"]["operation_id"] == request.operation_id

        replayed = client.start_continuity(fixture.git_project_id, request)
        assert replayed == reconciled

        changed = ContinuityStartRequest(
            request.operation_id,
            SESSION_ID,
            "Changed input must never execute",
            request.source_references,
            request.next_step,
        )
        with pytest.raises(ConnectorError) as conflict:
            client.start_continuity(fixture.git_project_id, changed)
        assert conflict.value.code == "OPERATION_INPUT_CONFLICT"
    finally:
        fresh_stdout, fresh_stderr = fresh.stop()
    assert fresh_stdout == ""
    assert fresh_stderr == ""
    assert len(tuple(fixture.git_continuity_root.glob("records/continuity/**/*.md"))) == 1
    finalized = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert finalized["state"] == "accepted"
    assert finalized["input_sha256"] == pending["input_sha256"]


def test_fresh_process_returns_uncertain_when_pending_has_no_canonical_effect(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    request = ContinuityStartRequest(
        "fault-after-pending-1",
        SESSION_ID,
        "Exit before the canonical effect",
        ("fixture://source/current",),
        "Return an explicit uncertain result",
    )
    receipt_path = (
        fixture.git_continuity_root
        / ".connector-operations"
        / f"{sha256_text(request.operation_id)}.json"
    )

    crashed = start_fault_connector_process(
        fixture,
        "fault-after-pending",
        fault_stage="after_pending",
    )
    try:
        client = LocalProjectConnectorClient(crashed.base_url, WRITER_TOKEN)
        with pytest.raises(ConnectorUnavailable):
            client.start_continuity(fixture.git_project_id, request)
        assert crashed.process.wait(timeout=5) == 97
    finally:
        crashed.stop()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["state"] == "pending"
    assert not tuple(fixture.git_continuity_root.glob("records/continuity/**/*.md"))

    fresh = start_connector_process(fixture, "fault-uncertain-fresh")
    try:
        client = LocalProjectConnectorClient(fresh.base_url, WRITER_TOKEN)
        with pytest.raises(ConnectorError) as uncertain:
            client.start_continuity(fixture.git_project_id, request)
        assert uncertain.value.code == "OPERATION_OUTCOME_UNCERTAIN"
        assert uncertain.value.details["operation_id"] == request.operation_id

        changed = ContinuityStartRequest(
            request.operation_id,
            SESSION_ID,
            "Changed input still conflicts while outcome is uncertain",
            request.source_references,
            request.next_step,
        )
        with pytest.raises(ConnectorError) as conflict:
            client.start_continuity(fixture.git_project_id, changed)
        assert conflict.value.code == "OPERATION_INPUT_CONFLICT"
    finally:
        fresh_stdout, fresh_stderr = fresh.stop()
    assert fresh_stdout == ""
    assert fresh_stderr == ""
    assert not tuple(fixture.git_continuity_root.glob("records/continuity/**/*.md"))


def _local_memory_authorization(access: str = "write", operation: BrainOperation = BrainOperation.MEMORY_SAVE) -> OwnerAuthorization:
    return OwnerAuthorization(
        operation=operation,
        access=access,
        selector=OwnerSelector("personal"),
        owner_id="brain-home",
        owner_locator="vault://brain-home",
        policy_revision="fixture-policy-v1",
        capability=object(),
    )


def test_local_harness_save_of_both_test_2_sentences_returns_right_word_and_date() -> None:
    """C2 §9 PRD test 2: local harness save (real Brain Core, no live network) of
    both test-2 sentences returns the right plain word and resolved date."""

    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 26, 21, 0, tzinfo=timezone(timedelta(hours=8)))
    provider, _fake = build_local_memory_provider(now)

    idea_request = MemorySaveRequest(
        operation_id="local-harness-piano-idea",
        kind="item",
        title="Learn piano",
        body="maybe I should learn piano",
        source_reference="fixture-current-message",
        said_by="owner",
        came_by="direct",
        words="maybe I should learn piano",
        level="possible",
    )
    idea_saved = anyio.run(provider.save, _local_memory_authorization("write"), idea_request)
    assert idea_saved.status == "accepted" and idea_saved.remote_observed is True
    assert idea_saved.word == "idea"

    promise_request = MemorySaveRequest(
        operation_id="local-harness-electricity-promise",
        kind="item",
        title="Pay the electricity bill",
        body="I'll pay the electricity bill Friday",
        source_reference="fixture-current-message",
        said_by="owner",
        came_by="direct",
        words="I'll pay the electricity bill Friday",
        level="accepted",
        owed_to="the electricity company",
        time_words="Friday",
    )
    promise_saved = anyio.run(provider.save, _local_memory_authorization("write"), promise_request)
    assert promise_saved.status == "accepted" and promise_saved.remote_observed is True
    assert promise_saved.word == "promise"
    assert promise_saved.timing=={}

    read_auth = _local_memory_authorization("read", BrainOperation.MEMORY_READ)
    idea_read = anyio.run(provider.read, read_auth, idea_saved.record_id)
    assert idea_read.canonical_record["data"]["level"] == "possible"
    promise_read = anyio.run(provider.read, read_auth, promise_saved.record_id)
    assert promise_read.canonical_record['data']['dates']['deadline']=={
        'day':'2026-10-02','words':'Friday','read_by':'code','read_on':'2026-09-26'}


def test_calendar_suggestion_appears_only_when_review_is_enabled() -> None:
    """Step E spec §5: the save confirmation gains a calendar_suggestion for a
    timed item, but only when [review] enabled = true; off by default."""

    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 26, 21, 0, tzinfo=timezone(timedelta(hours=8)))

    def promise_request(operation_id: str) -> MemorySaveRequest:
        return MemorySaveRequest(
            operation_id=operation_id,
            kind="item",
            title="Pay the electricity bill",
            body="I'll pay the electricity bill Friday",
            source_reference="fixture-current-message",
            said_by="owner",
            came_by="direct",
            words="I'll pay the electricity bill Friday",
            level="accepted",
            owed_to="the electricity company",
            time_words="Friday",
        )

    disabled_provider, _ = build_local_memory_provider(now)
    disabled_saved = anyio.run(
        disabled_provider.save, _local_memory_authorization("write"), promise_request("review-off-1"),
    )
    assert disabled_saved.calendar_suggestion is None

    enabled_provider, _ = build_local_memory_provider(now, review_enabled=True)
    enabled_saved = anyio.run(
        enabled_provider.save, _local_memory_authorization("write"), promise_request("review-on-1"),
    )
    assert enabled_saved.calendar_suggestion is None
    # The retired tap-to-add suggestion stays absent even with review on; the
    # real reader still carries the original title and resolved date exactly.
    enabled_read=anyio.run(enabled_provider.read,_local_memory_authorization('read',BrainOperation.MEMORY_READ),enabled_saved.record_id)
    assert enabled_read.title=='Pay the electricity bill'
    assert enabled_read.canonical_record['data']['dates']['deadline']=={
        'day':'2026-10-02','words':'Friday','read_by':'code','read_on':'2026-09-26'}
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.mcp.host_identity import bind_write_attribution
    with bind_write_attribution('hermes-wa'):
        changed=anyio.run(enabled_provider.work,_local_memory_authorization('write',BrainOperation.WORK),
            WorkRequest('review-on-door',"I'll pay the electricity bill Friday",'owner','direct',
                ({'move':'date','thing':enabled_saved.record_id,'when':'Friday'},)))
    assert changed['status']=='accepted' and 'calendar_suggestion' not in changed
    door_read=anyio.run(enabled_provider.read,_local_memory_authorization('read',BrainOperation.MEMORY_READ),
        changed['things'][0]['record_id'])
    assert door_read.title==enabled_read.title
    assert door_read.body==enabled_read.body
    assert door_read.canonical_record['data']['dates']==enabled_read.canonical_record['data']['dates']
    assert door_read.canonical_record['supersedes']==[enabled_saved.record_id]

    # An untimed item enabled the same way gets no suggestion - nothing to add.
    idea_request = MemorySaveRequest(
        operation_id="review-on-idea-1",
        kind="item",
        title="Learn piano",
        body="maybe I should learn piano",
        source_reference="fixture-current-message",
        said_by="owner",
        came_by="direct",
        words="maybe I should learn piano",
        level="possible",
    )
    idea_saved = anyio.run(enabled_provider.save, _local_memory_authorization("write"), idea_request)
    assert idea_saved.calendar_suggestion is None
