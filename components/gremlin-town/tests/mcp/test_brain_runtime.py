import asyncio
from datetime import UTC, datetime

import pytest

from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation,
    BrainRuntime,
    BrainRuntimeError,
    ContextQuery,
    ContextResult,
    ContinuityAppendRequest,
    ContinuityCloseRequest,
    ContinuityRecoverRequest,
    ContinuityRecovery,
    ContinuityStartRequest,
    ContinuityWriteResult,
    DecisionRequest,
    MemoryChangeRequest,
    MemoryRecord,
    MemorySaveRequest,
    OwnerAuthorization,
    OwnerSelector,
    RetrievalExclusion,
    SourceEvidence,
    WriteReceipt,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
CALLER = BrainCaller("alice", frozenset({"bc:read", "bc:write"}))
PROJECT = OwnerSelector("project", "demo")
PERSONAL = OwnerSelector("personal")


def receipt(operation_id="op-1"):
    return WriteReceipt(
        operation_id, "accepted", "vault://brain-home", "brn_1", "commit-1",
        "a" * 64, "b" * 64, True,
    )


def context_result():
    return ContextResult(
        sources=(SourceEvidence(
            "E1", "vault://demo", "vault://demo/context.md", "worktree:1", "a" * 64,
            "export", "fts5", "line:1", 4, "current permitted excerpt",
        ),),
        exclusions=(RetrievalExclusion("brain.grant.missing", "not ingested", 1),),
        result_sha256="c" * 64,
        observed_at=NOW,
        freshness="current",
    )


def continuity_write(operation_id="op-1", state="open"):
    return ContinuityWriteResult("brn_1", "session-1", state, True, NOW, receipt(operation_id))


def recovery():
    return ContinuityRecovery(
        "brn_1", "session-1", "open", "continue", "next", NOW,
        "brain.lexical/v1", 42, blockers=("fixture blocker",),
    )


def memory_record():
    return MemoryRecord(
        "brn_2", "capture", "Fixture", "Current body", "inbox",
        "vault://brain-home", "commit-2", "d" * 64, NOW, "current",
    )


class Authorizer:
    def __init__(self, events=None):
        self.calls = []
        self.events = events

    async def authorize(self, caller, operation, selector, access):
        self.calls.append((caller, operation, selector, access))
        if self.events is not None:
            self.events.append(("authorize", operation.value))
        return OwnerAuthorization(
            operation, access, selector, "owner-demo", "vault://demo", "policy-v1", object()
        )


class Context:
    def __init__(self, events=None):
        self.calls = []
        self.events = events

    async def retrieve(self, authorization, query):
        self.calls.append((authorization, query))
        if self.events is not None:
            self.events.append(("body", query.intent))
        return context_result()


class Continuity:
    def __init__(self):
        self.calls = []

    async def start(self, authorization, request):
        self.calls.append(("start", authorization, request))
        return continuity_write(request.operation_id)

    async def append(self, authorization, request):
        self.calls.append(("append", authorization, request))
        return continuity_write(request.operation_id)

    async def recover(self, authorization, request):
        self.calls.append(("recover", authorization, request))
        return recovery()

    async def close(self, authorization, request):
        self.calls.append(("close", authorization, request))
        return continuity_write(request.operation_id, "recovered" if request.recovered else "closed")


class Decisions:
    def __init__(self):
        self.calls = []

    async def record(self, authorization, request):
        self.calls.append((authorization, request))
        return receipt(request.operation_id)


class Memory:
    def __init__(self):
        self.calls = []

    async def save(self, authorization, request):
        self.calls.append(("save", authorization, request))
        return receipt(request.operation_id)

    async def change(self, authorization, request):
        self.calls.append(("change", authorization, request))
        return receipt(request.operation_id)

    async def read(self, authorization, record_id):
        self.calls.append(("read", authorization, record_id))
        return memory_record()


def runtime(authorizer=None, context=None, continuity=None, decisions=None, memory=None, **kwargs):
    return BrainRuntime(
        authorizer=authorizer or Authorizer(),
        context=context or Context(),
        continuity=continuity or Continuity(),
        decisions=decisions or Decisions(),
        memory=memory or Memory(),
        **kwargs,
    )


def test_owner_authorization_precedes_context_body_read_and_preserves_bc_evidence():
    events = []
    authorizer = Authorizer(events)
    provider = Context(events)
    result = asyncio.run(runtime(authorizer=authorizer, context=provider).retrieve_context(
        CALLER, PROJECT, ContextQuery("protected intent", 3, 500)
    ))

    assert events == [("authorize", "context.retrieve"), ("body", "protected intent")]
    assert authorizer.calls[0][2:] == (PROJECT, "read")
    assert result.sources[0].source_version == "worktree:1"
    assert result.sources[0].content_sha256 == "a" * 64
    assert result.freshness == "current"
    assert result.exclusions[0].code == "brain.grant.missing"


@pytest.mark.parametrize("operation", ["read", "write"])
def test_owner_denial_happens_before_any_dependency_body_access(operation):
    calls = []

    class Deny:
        async def authorize(self, caller, selected, selector, access):
            calls.append((selected, selector, access))
            raise PermissionError("private policy detail")

    context = Context()
    memory = Memory()
    subject = runtime(authorizer=Deny(), context=context, memory=memory)
    with pytest.raises(PermissionError, match="private policy detail"):
        if operation == "read":
            asyncio.run(subject.retrieve_context(CALLER, PROJECT, ContextQuery("must not reach body")))
        else:
            asyncio.run(subject.save_memory(
                CALLER, PERSONAL,
                MemorySaveRequest(
                    "save-1", "item", "Title", "must not be written", "source-1",
                    said_by="owner", came_by="direct", words="must not be written",
                ),
            ))
    assert len(calls) == 1
    assert context.calls == [] and memory.calls == []


def test_mismatched_trusted_capability_is_refused_before_dependency():
    class WrongOwner:
        async def authorize(self, caller, operation, selector, access):
            return OwnerAuthorization(
                BrainOperation.MEMORY_READ, "read", PERSONAL,
                "wrong", "vault://wrong", "policy-v1", object(),
            )

    provider = Context()
    with pytest.raises(BrainRuntimeError) as caught:
        asyncio.run(runtime(authorizer=WrongOwner(), context=provider).retrieve_context(
            CALLER, PROJECT, ContextQuery("intent")
        ))
    assert caught.value.code == "brain.policy.excluded"
    assert provider.calls == []


def test_all_typed_operations_delegate_with_operation_specific_access():
    authorizer = Authorizer()
    continuity = Continuity()
    decisions = Decisions()
    memory = Memory()
    subject = runtime(authorizer=authorizer, continuity=continuity, decisions=decisions, memory=memory)

    async def scenario():
        await subject.start_continuity(CALLER, PROJECT, ContinuityStartRequest(
            "start-1", "session-1", "intent", ("source-1",)
        ))
        await subject.append_continuity(CALLER, PROJECT, ContinuityAppendRequest(
            "append-1", "session-1", "blocker", "fixture blocker"
        ))
        await subject.recover_continuity(CALLER, PROJECT, ContinuityRecoverRequest(500))
        await subject.close_continuity(CALLER, PROJECT, ContinuityCloseRequest(
            "close-1", "session-1", recovered=True
        ))
        await subject.record_decision(CALLER, PERSONAL, DecisionRequest(
            "decide-1", "Use A", "Choice", "Because", "B", "personal",
            "Brain Home", "complete", "source-1",
        ))
        await subject.save_memory(CALLER, PERSONAL, MemorySaveRequest(
            "save-1", "item", "Title", "Body", "source-1", said_by="owner", came_by="direct", words="Body"
        ))
        await subject.change_memory(CALLER, PERSONAL, MemoryChangeRequest(
            "change-1", "brn_2", "e" * 64, "source-1", said_by="owner", came_by="direct", words="Body", state="waiting"
        ))
        await subject.read_memory(CALLER, PERSONAL, "brn_2")

    asyncio.run(scenario())
    assert [(call[1], call[3]) for call in authorizer.calls] == [
        (BrainOperation.CONTINUITY_START, "write"),
        (BrainOperation.CONTINUITY_APPEND, "write"),
        (BrainOperation.CONTINUITY_RECOVER, "read"),
        (BrainOperation.CONTINUITY_CLOSE, "write"),
        (BrainOperation.DECISION_RECORD, "write"),
        (BrainOperation.MEMORY_SAVE, "write"),
        (BrainOperation.MEMORY_CHANGE, "write"),
        (BrainOperation.MEMORY_READ, "read"),
    ]
    assert [call[0] for call in continuity.calls] == ["start", "append", "recover", "close"]
    assert decisions.calls[0][1].source_reference == "source-1"
    assert [call[0] for call in memory.calls] == ["save", "change", "read"]
    assert memory.calls[-1][2] == "brn_2"


def test_cancellation_propagates_to_async_dependency():
    started = asyncio.Event()

    class BlockingContext(Context):
        async def retrieve(self, authorization, query):
            started.set()
            await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(runtime(context=BlockingContext()).retrieve_context(
            CALLER, PROJECT, ContextQuery("intent")
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_runtime_closes_only_explicit_managed_resources_once_in_reverse_order():
    events = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            events.append(self.name)

    one, two = Resource("one"), Resource("two")
    subject = runtime(managed_resources=(one, two, one))

    async def scenario():
        await subject.aclose()
        await subject.aclose()
        with pytest.raises(BrainRuntimeError) as caught:
            await subject.retrieve_context(CALLER, PROJECT, ContextQuery("after close"))
        assert caught.value.code == "brain.capability.unavailable"

    asyncio.run(scenario())
    assert events == ["two", "one"]


def test_scope_selector_never_accepts_owner_locations_or_cross_scope_project_ids():
    with pytest.raises(TypeError):
        OwnerSelector("project", "demo", owner_locator="vault://demo")
    with pytest.raises(ValueError, match="requires project_id"):
        OwnerSelector("project")
    with pytest.raises(ValueError, match="only for project scope"):
        OwnerSelector("personal", "demo")
