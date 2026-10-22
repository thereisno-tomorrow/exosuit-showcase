from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from types import ModuleType

import anyio
import pytest

from brain_core.github_storage import (
    GitHubBrainHomeStorage,
    GitHubObjectCache,
    memory_operation_dedupe_key,
    memory_operation_record_suffix,
)
from brain_core.records import Evidence, canonical_json, parse_record, render_record
from gremlin_town.mcp.brain_runtime import (
    BrainOperation,
    BrainRuntimeError,
    DecisionRequest,
    MemoryChangeRequest,
    MemorySaveRequest,
    OwnerAuthorization,
    OwnerSelector,
    WorkRequest,
)
from gremlin_town.mcp.github_brain_provider import (
    BrainCoreRecordAdapter,
    GitHubBrainProvider,
)


SOURCE_BYTES = b"authorized provider fixture"
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
FIRST_TIME = datetime.fromisoformat("2026-09-21T12:00:00+08:00")
LATER_TIME = datetime.fromisoformat("2026-10-04T09:30:00+08:00")


def _load_lane_b_fake() -> ModuleType:
    """Load Lane B's actual fake HTTP seam without copying or mocking it."""

    path = (
        Path(__file__).resolve().parents[3]
        / "brain-core"
        / "tests"
        / "unit"
        / "test_github_storage.py"
    )
    name = "_brain_core_lane_b_github_storage_tests"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LANE_B = _load_lane_b_fake()


class _HistoryTransport:
    """Expose the fake's actual immutable commit metadata, as GitHub does."""

    def __init__(self, fake):
        self.fake = fake

    def request(self, method, url, *, headers, body):
        response = self.fake.request(method, url, headers=headers, body=body)
        if method == "GET" and "/git/commits/" in url:
            sha = url.rsplit("/", 1)[-1]
            commit = self.fake.commits[sha]
            value = json.loads(response.body)
            value["message"] = commit["message"]
            value["parents"] = [] if commit["parent"] is None else [{"sha": commit["parent"]}]
            return LANE_B.HTTPResponse(response.status, response.headers, json.dumps(value).encode())
        return response


def _authorization(operation: BrainOperation, access: str) -> OwnerAuthorization:
    return OwnerAuthorization(
        operation=operation,
        access=access,  # type: ignore[arg-type]
        selector=OwnerSelector("personal"),
        owner_id="brain-home",
        owner_locator="vault://brain-home",
        policy_revision="fixture-policy-v1",
        capability=object(),
    )


def _evidence(_: OwnerAuthorization, reference: str) -> Evidence:
    assert reference == "fixture-source"
    return Evidence(
        source_type="fixture",
        locator="fixture://authorized/source",
        content_sha256=SOURCE_SHA256,
        excerpt="authorized fixture",
        actor="nic",
        grant_sha256=hashlib.sha256(b"trusted-host-grant").hexdigest(),
    )


def _adapter(
    times: dict[str, datetime] | None = None,
    *,
    operation_time=None,
    semantic_locator=None,
) -> BrainCoreRecordAdapter:
    accepted = times or {}
    def fixture_clock(operation_id):
        if operation_id in accepted:
            return accepted[operation_id]
        # Capture now uses the same door batch path, in a separate retry namespace.
        # Preserve each fixture's exact accepted time for that deterministic key.
        for original, timestamp in accepted.items():
            bridge_id = "bridge-" + hashlib.sha256(original.encode()).hexdigest()
            if operation_id == "work-" + hashlib.sha256(bridge_id.encode()).hexdigest():
                return timestamp
            if operation_id == "work-" + hashlib.sha256(original.encode()).hexdigest():
                return timestamp
        raise KeyError(operation_id)
    resolver = operation_time or fixture_clock
    return BrainCoreRecordAdapter(
        operation_time=resolver,
        source_evidence=_evidence,
        semantic_locator=semantic_locator,
    )


def _provider(fake, adapter: BrainCoreRecordAdapter) -> GitHubBrainProvider:
    return GitHubBrainProvider(_hub_storage(fake), adapter)


class _OrderedPublication:
    def __init__(self) -> None:
        self.first_entered = threading.Event()
        self.second_entered = threading.Event()
        self.first_done = threading.Event()
        self.expected_revisions: list[tuple[str, str, str | None]] = []
        self.lock = threading.Lock()


class _OrderedStorage(GitHubBrainHomeStorage):
    def __init__(self, fake, order: _OrderedPublication, role: str) -> None:
        super().__init__(
            repository=LANE_B.REPOSITORY,
            branch=LANE_B.BRANCH,
            token=LANE_B.TOKEN,
            transport=_HistoryTransport(fake),
            object_cache=GitHubObjectCache(),  # as the hub builds it; its 5,000-file scans need one
        )
        self._order = order
        self._role = role

    def _before_publish(self, kind: str, expected_revision: str | None) -> None:
        with self._order.lock:
            self._order.expected_revisions.append(
                (self._role, kind, expected_revision)
            )
        if self._role == "first":
            self._order.first_entered.set()
            assert self._order.second_entered.wait(5), "second host did not finish preflight"
        else:
            self._order.second_entered.set()
            assert self._order.first_done.wait(5), "first host did not publish"

    def publish_record(
        self, *, record_bytes, expected_revision=None, retry_after_lost_reply=True, base_revision=None
    ):
        pinned = expected_revision if base_revision is None else base_revision.commit_sha
        self._before_publish("memory", pinned)
        try:
            return super().publish_record(
                record_bytes=record_bytes,
                expected_revision=expected_revision,
                retry_after_lost_reply=retry_after_lost_reply,
                base_revision=base_revision,
            )
        finally:
            if self._role == "first":
                self._order.first_done.set()

    def publish_records(self, *, records, base_revision=None, **kwargs):
        self._before_publish("memory", None if base_revision is None else base_revision.commit_sha)
        try:
            return super().publish_records(records=records, base_revision=base_revision, **kwargs)
        finally:
            if self._role == "first":
                self._order.first_done.set()

    def publish(
        self,
        *,
        record_bytes,
        receipt_bytes,
        expected_revision=None,
        retry_after_lost_reply=True,
    ):
        self._before_publish("decision", expected_revision)
        try:
            return super().publish(
                record_bytes=record_bytes,
                receipt_bytes=receipt_bytes,
                expected_revision=expected_revision,
                retry_after_lost_reply=retry_after_lost_reply,
            )
        finally:
            if self._role == "first":
                self._order.first_done.set()


def _memory_request(operation_id: str, kind: str, **overrides: object) -> MemorySaveRequest:
    fields: dict[str, object] = dict(
        operation_id=operation_id,
        kind=kind,
        title=f"A {kind} record",
        body=f"Validated {kind} body",
        source_reference="fixture-source",
        said_by="owner",
        came_by="direct",
        words=f"Validated {kind} body",
    )
    if kind == "item":
        fields["level"] = "accepted"
    elif kind == "fact":
        fields["observed_at"] = FIRST_TIME.isoformat()
    fields.update(overrides)
    return MemorySaveRequest(**fields)  # type: ignore[arg-type]


def _memory_change_request(
    operation_id: str, record_id: str, expected_sha256: str, **overrides: object
) -> MemoryChangeRequest:
    fields: dict[str, object] = dict(
        operation_id=operation_id,
        record_id=record_id,
        expected_sha256=expected_sha256,
        source_reference="fixture-source",
        said_by="agent",
        came_by="agent_guess",
        speaker="fixture-agent",
        words="Marked waiting for the fixture",
        state="waiting",
    )
    fields.update(overrides)
    return MemoryChangeRequest(**fields)  # type: ignore[arg-type]


def _decision(
    operation_id: str,
    *,
    because: str = "One canonical result is required",
    choice: str = "Publish through the configured Brain Home branch",
    governing_scope: str = "Brain Home publication",
) -> DecisionRequest:
    return DecisionRequest(
        operation_id=operation_id,
        choice=choice,
        title="Use the configured Brain Home branch",
        because=because,
        forecloses="Uncorrelated file-by-file publication",
        governing_scope=governing_scope,
        encoded_in="Gremlin Town Brain provider",
        completeness_check="Record and receipt share an accepted commit",
        source_reference="fixture-source",
    )


def _writes(fake) -> tuple[str, ...]:
    return tuple(event for event in fake.events if event.startswith(("POST_", "PATCH_")))


def _door(provider, operation_id, message, *moves):
    from gremlin_town.mcp.host_identity import bind_write_attribution
    async def invoke():
        with bind_write_attribution("hermes-wa"):
            return await provider.work(_authorization(BrainOperation.WORK, "write"),
                WorkRequest(operation_id, message, "owner", "direct", moves))
    result = anyio.run(invoke)
    record_id = result["things"][0]["record_id"]
    stored = next(item for item in provider._storage.snapshot_records(kinds=frozenset({"item"})).records
        if item.record.id == record_id)
    return SimpleNamespace(status=result["status"], record_id=record_id, version=result["version"],
        content_sha256=hashlib.sha256(stored.content).hexdigest(), timing=stored.record.data.get("timing", {}))


def _refused_item_change(provider, authorization, request):
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.change, authorization, request)
    assert refused.value.code == "ACCESS_DENIED" and refused.value.fix


def _dates(provider, receipt):
    """Check actual canonical dates; the capture bridge no longer writes due/at."""
    record = provider._storage.preflight_record_by_id(receipt.record_id).match.record
    return record.data.get("dates", {})


@pytest.mark.parametrize("kind", ["item", "fact"])
def test_memory_kinds_publish_and_read_through_real_storage(kind: str) -> None:
    operation_id = f"memory-{kind}"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, kind),
    )
    observed = anyio.run(
        provider.read,
        _authorization(BrainOperation.MEMORY_READ, "read"),
        saved.record_id,
    )

    assert saved.status == "accepted"
    assert saved.remote_observed is True
    assert saved.receipt_sha256 is None
    assert observed.record_id == saved.record_id
    assert observed.kind == kind
    assert observed.body == f"Validated {kind} body"
    assert observed.version == saved.version
    assert observed.content_sha256 == saved.content_sha256
    assert observed.freshness == "current"


@pytest.mark.parametrize("caller", ["hermes-wa", "chatgpt-web"])
def test_raise_with_bridge_saves_exact_content_and_real_agenda(caller, monkeypatch):
    from brain_core import moves
    from brain_core.agenda import agenda
    from brain_core.review import ItemRecord
    from gremlin_town.mcp.host_identity import bind_write_attribution

    original = _memory_request("raise-silver-budget", "item", title="Silver budget",
        body="Discuss the silver budget with Sam.\n\n", words="Discuss the silver budget with Sam.",
        raise_with="Sam")
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({original.operation_id: FIRST_TIME}))
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    core_apply = moves.apply_change
    changes = []

    def observe(index, change, **kwargs):
        changes.append(change)
        return core_apply(index, change, **kwargs)

    monkeypatch.setattr(moves, "apply_change", observe)
    with bind_write_attribution(caller):
        accepted = anyio.run(provider.save, authorization, original)
        observed = anyio.run(provider.read, _authorization(BrainOperation.MEMORY_READ, "read"),
            accepted.record_id)
    assert accepted.status == "accepted" and accepted.remote_observed
    assert accepted.receipt_sha256 is None
    assert observed.version == accepted.version and observed.content_sha256 == accepted.content_sha256
    assert observed.body == original.body.rstrip("\n")
    stored = provider._storage.snapshot_records(kinds=frozenset({"item"})).records
    assert len(stored) == 1
    record = stored[0].record
    assert record.data["raise_with"] == "Sam"
    assert record.source["recorded_by"] == caller and record.source["words"] == original.words
    assert record.vaults == (authorization.owner_locator,) and record.owners == (authorization.owner_id,)
    assert record.evidence[0].actor == caller
    assert record.body == original.body.rstrip("\n")
    assert accepted.content_sha256 == hashlib.sha256(render_record(record).encode()).hexdigest()
    public = asdict(original)
    for removed in ("size", "importance", "importance_reason", "sort", "first_step", "links"):
        public.pop(removed)
    assert record.dedupe_key == hashlib.sha256(canonical_json(public).encode()).hexdigest()
    assert changes[0]["_context"].capture_raise_with == {0: "Sam"}
    assert all("raise_with" not in move for move in changes[0]["moves"])
    person = agenda([ItemRecord(record, stored[0].path)], "Sam", FIRST_TIME)
    assert [(line.record_id, line.title, line.record_sha256) for line in person.to_raise] == [
        (accepted.record_id, "Silver budget", record.sha256)]
    assert not person.promised and not person.waiting_on
    head, writes = fake.branch, _writes(fake)

    def no_clock(_):
        raise AssertionError("exact raise_with bridge replay must precede the accepted-time writer")

    fresh = _provider(fake, _adapter(operation_time=no_clock))
    with bind_write_attribution(caller):
        duplicate = anyio.run(fresh.save, authorization, original)
        assert duplicate == replace(accepted, status="duplicate")
        for changed in (replace(original, raise_with="Rowan"), replace(original, raise_with=" Sam "),
                        replace(original, body=original.body.rstrip("\n"))):
            with pytest.raises(BrainRuntimeError) as conflict:
                anyio.run(fresh.save, authorization, changed)
            assert conflict.value.code == "brain.conflict" and conflict.value.field == "operation_id"
            assert conflict.value.fix
        with pytest.raises(BrainRuntimeError) as mismatch:
            anyio.run(fresh.save, replace(authorization, owner_id="another-owner"), original)
        assert mismatch.value.code == "ACCESS_DENIED" and mismatch.value.field == "scope"
        assert mismatch.value.fix
    assert fake.branch == head and _writes(fake) == writes


@pytest.mark.parametrize("caller", ["hermes-wa", "chatgpt-web"])
@pytest.mark.parametrize("value, code", [
    ("", "brain.invalid_argument"), (" \t", "brain.invalid_argument"),
    ("S" * 129, "brain.contract.invalid"), (7, "brain.invalid_argument"),
    (True, "brain.invalid_argument"), ([], "brain.invalid_argument"),
    ({"person": "Sam"}, "brain.invalid_argument"),
])
def test_raise_with_bridge_refuses_invalid_field_before_any_publication(caller, value, code):
    from gremlin_town.mcp.host_identity import bind_write_attribution

    original = _memory_request("raise-invalid", "item", title="Silver budget",
        words="Discuss the silver budget with Sam", raise_with=value)
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({original.operation_id: FIRST_TIME}))
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    head = fake.branch
    with bind_write_attribution(caller):
        with pytest.raises(BrainRuntimeError) as refused:
            anyio.run(provider.save, authorization, original)
        assert refused.value.code == code
        assert "raise_with" in refused.value.field and refused.value.fix
        assert fake.branch == head and not _writes(fake)
        assert not provider._storage.snapshot_records(kinds=frozenset({"item"})).records
        corrected = anyio.run(provider.save, authorization, replace(original, raise_with="Sam"))
        assert corrected.status == "accepted"


def test_raise_with_bridge_retry_identity_is_authenticated_caller_scoped():
    from gremlin_town.mcp.host_identity import bind_write_attribution

    original = _memory_request("raise-shared-key", "item", title="Silver budget",
        words="Discuss the silver budget with Sam", raise_with="Sam")
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({original.operation_id: FIRST_TIME}))
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    saved = {}
    for caller in ("hermes-wa", "chatgpt-web"):
        with bind_write_attribution(caller):
            saved[caller] = anyio.run(provider.save, authorization, original)
    assert len({receipt.record_id for receipt in saved.values()}) == 2
    head, writes = fake.branch, _writes(fake)
    for caller, accepted in saved.items():
        with bind_write_attribution(caller):
            assert anyio.run(provider.save, authorization, original) == replace(accepted, status="duplicate")
    stored = provider._storage.snapshot_records(kinds=frozenset({"item"})).records
    assert {entry.record.source["recorded_by"]: entry.record.id for entry in stored} == {
        caller: receipt.record_id for caller, receipt in saved.items()}
    assert all(entry.record.data["raise_with"] == "Sam" for entry in stored)
    assert fake.branch == head and _writes(fake) == writes


@pytest.mark.parametrize("caller", ["hermes-wa", "chatgpt-web"])
@pytest.mark.parametrize("invalid", [{"access": "read"}, {"operation": BrainOperation.WORK},
    {"owner_locator": "vault://unknown"}, {"capability": None}])
def test_raise_with_bridge_repeats_authorization_before_any_effect(caller, invalid):
    from gremlin_town.mcp.host_identity import bind_write_attribution

    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({"raise-denied": FIRST_TIME}))
    authorization = replace(_authorization(BrainOperation.MEMORY_SAVE, "write"), **invalid)
    original = _memory_request("raise-denied", "item", raise_with="Sam")
    head, events = fake.branch, tuple(fake.events)
    with bind_write_attribution(caller):
        with pytest.raises(BrainRuntimeError) as refused:
            anyio.run(provider.save, authorization, original)
    assert refused.value.code == "brain.policy.excluded" and refused.value.fix
    assert fake.branch == head and tuple(fake.events) == events


@pytest.mark.parametrize("kind", ["item", "fact"])
def test_memory_save_refuses_missing_words_unless_came_by_is_system(kind: str) -> None:
    """Brain Core's _source_from_mapping refuses a v2 record missing source.words unless
    came_by is system; Gremlin Town must catch this before record_from_mapping runs and
    name the field, instead of the generic brain.contract.invalid (found live 2026-09-26)."""

    operation_id = f"missing-words-{kind}"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    with pytest.raises(BrainRuntimeError) as excinfo:
        anyio.run(
            provider.save,
            _authorization(BrainOperation.MEMORY_SAVE, "write"),
            _memory_request(operation_id, kind, words=None),
        )
    assert excinfo.value.code == "brain.invalid_argument"
    assert "words" in excinfo.value.message
    assert not _writes(fake)


def test_memory_save_allows_missing_words_when_came_by_is_system() -> None:
    operation_id = "system-source-no-words"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id, "fact",
            said_by="other", came_by="system", words=None, speaker="a monitoring script",
        ),
    )
    assert saved.status == "accepted"


@pytest.mark.parametrize("said_by", ["agent", "other"])
def test_memory_save_refuses_missing_speaker_when_said_by_is_agent_or_other(said_by: str) -> None:
    operation_id = f"missing-speaker-{said_by}"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    with pytest.raises(BrainRuntimeError) as excinfo:
        anyio.run(
            provider.save,
            _authorization(BrainOperation.MEMORY_SAVE, "write"),
            _memory_request(
                operation_id, "item",
                said_by=said_by, came_by="agent_guess", level="suggested", speaker=None,
            ),
        )
    assert excinfo.value.code == "brain.invalid_argument"
    assert "speaker" in excinfo.value.message
    assert not _writes(fake)


def test_memory_save_refuses_owner_said_by_without_direct_came_by() -> None:
    operation_id = "owner-not-direct"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    with pytest.raises(BrainRuntimeError) as excinfo:
        anyio.run(
            provider.save,
            _authorization(BrainOperation.MEMORY_SAVE, "write"),
            _memory_request(operation_id, "item", came_by="agent_guess"),
        )
    assert excinfo.value.code == "brain.invalid_argument"
    assert "came_by" in excinfo.value.message
    assert not _writes(fake)


def test_memory_save_accepts_a_valid_agent_source() -> None:
    """A legitimate agent-sourced save (not owner's own words) must still work: the fix
    for the missing-field checks must not over-refuse a well-formed agent save."""

    operation_id = "valid-agent-save"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id, "item",
            said_by="agent", came_by="agent_guess", level="suggested",
            words="Nic might want a Friday review.", speaker="the agent",
        ),
    )
    assert saved.status == "accepted"
    observed = anyio.run(
        provider.read,
        _authorization(BrainOperation.MEMORY_READ, "read"),
        saved.record_id,
    )
    assert observed.canonical_record is not None
    assert observed.canonical_record["source"]["said_by"] == "other"
    assert observed.canonical_record["source"]["came_by"] == "connected_source"
    assert observed.canonical_record["source"]["speaker"] == "the agent"


_SAVE_INSTRUCTION_VARIANTS = [
    "Save it to gtbc",
    "save it to gtbc.",
    "SAVE IT TO GTBC",
    "save   it  to   gtbc",
    "Add that to brain home please",
    "store this in gt",
    "remember it",
    "note down",
    "put that into gremlin town pls",
    # 2026-09-29: Fleet Boss's real save command, ending in a list reference.
    "store in brain home 3, 4, 5",
    "add these to brain home",
]


def _slug(words: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in words.lower())


@pytest.mark.parametrize("words", _SAVE_INSTRUCTION_VARIANTS)
def test_memory_save_refuses_a_bare_save_instruction_claimed_as_owner_words(words: str) -> None:
    """ROA-8: ChatGPT recorded its own save command ("Save it to gtbc") as Nic's exact
    words, and the record was accepted. words claimed as the owner's must be what he said
    about the thing, never his instruction to save it."""

    operation_id = f"save-instruction-{_slug(words)}"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    with pytest.raises(BrainRuntimeError) as excinfo:
        anyio.run(
            provider.save,
            _authorization(BrainOperation.MEMORY_SAVE, "write"),
            _memory_request(operation_id, "item", words=words),
        )
    assert excinfo.value.code == "brain.invalid_argument"
    assert "words" in excinfo.value.message
    assert not _writes(fake)


def test_memory_save_allows_the_same_words_when_said_by_is_agent() -> None:
    """The identical words are fine once they are not claimed as Nic's own; R2 (Brain
    Core) still caps an agent-sourced item at level suggested."""

    operation_id = "save-instruction-as-agent"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id, "item",
            said_by="agent", came_by="agent_guess", level="suggested",
            words="Save it to gtbc", speaker="the agent",
        ),
    )
    assert saved.status == "accepted"


@pytest.mark.parametrize(
    "words",
    ["save the receipts for the tax claim", "remember to call mum", "store 3 boxes in the garage"],
)
def test_memory_save_accepts_real_statements_that_merely_mention_save_or_remember(words: str) -> None:
    operation_id = f"real-statement-{_slug(words)}"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words=words),
    )
    assert saved.status == "accepted"


def test_memory_change_identity_refuses_a_bare_save_instruction_from_owner() -> None:
    """The change path shares _source_mapping with save, so it is refused here too,
    checked directly since it needs no existing record."""

    adapter = _adapter({"change-save-instruction": FIRST_TIME})
    request = _memory_change_request(
        "change-save-instruction", "brn_20260921_120000_aabb", "a" * 64,
        said_by="owner", came_by="direct", words="Save it to gtbc",
    )

    with pytest.raises(BrainRuntimeError) as excinfo:
        adapter.memory_change_identity(_authorization(BrainOperation.MEMORY_CHANGE, "write"), request)
    assert excinfo.value.code == "brain.invalid_argument"
    assert "words" in excinfo.value.message


@pytest.mark.parametrize(
    "overrides,missing_field",
    [
        ({"speaker": None}, "speaker"),
        ({"words": None}, "words"),
    ],
)
def test_memory_change_identity_refuses_the_same_missing_source_fields(
    overrides: dict[str, object], missing_field: str
) -> None:
    """memory_change_identity shares BrainCoreRecordAdapter._source_mapping with
    memory_identity, so the change path is refused before record_from_mapping the same
    way the save path is - checked here directly, since it needs no existing record."""

    adapter = _adapter({"change-missing-field": FIRST_TIME})
    fields: dict[str, object] = dict(
        operation_id="change-missing-field",
        record_id="brn_20260921_120000_aabb",
        expected_sha256="a" * 64,
        source_reference="fixture-source",
        said_by="other",
        came_by="connected_source",
        words="Someone else said this.",
        speaker="a colleague",
    )
    fields.update(overrides)
    request = MemoryChangeRequest(**fields)  # type: ignore[arg-type]

    with pytest.raises(BrainRuntimeError) as excinfo:
        adapter.memory_change_identity(_authorization(BrainOperation.MEMORY_CHANGE, "write"), request)
    assert excinfo.value.code == "brain.invalid_argument"
    assert missing_field in excinfo.value.message


def test_memory_replay_preflights_before_clock_and_conflicting_input_fails() -> None:
    operation_id = "desktop-replay-check"
    request = _memory_request(operation_id, "item", title="Fixture task", body="Deliver the Friday review.", words="Deliver the Friday review.")
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    fake = LANE_B.FakeGitHub()
    first = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    accepted = anyio.run(first.save, authorization, request)
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    path = next(path for path in tree if path.startswith("records/item/"))
    original = fake.blobs[tree[path]]
    original_record = parse_record(original.decode("utf-8"), path)

    def clock_must_not_run(_: str) -> datetime:
        raise AssertionError("remote operation preflight must precede operation_time")

    fresh = _provider(fake, _adapter(operation_time=clock_must_not_run))
    replay = anyio.run(fresh.save, authorization, request)

    assert replay.status == "duplicate"
    assert replay.record_id == accepted.record_id == original_record.id
    assert replay.content_sha256 == accepted.content_sha256
    assert original_record.created_at == original_record.updated_at == FIRST_TIME
    assert fake.blobs[fake.trees[fake.commits[fake.branch]["tree"]][path]] == original

    changed = _memory_request(operation_id, "item", title="Fixture task", body="Deliver the Monday review.", words="Deliver the Monday review.")
    with pytest.raises(BrainRuntimeError) as conflict:
        anyio.run(fresh.save, authorization, changed)
    assert conflict.value.code == "brain.conflict"


def test_memory_change_refuses_a_second_change_from_the_same_stale_read() -> None:
    """R8: a change is refused once the record it names is no longer the head of its
    chain, even though its own content hash (the only hash the caller could have read)
    never moves - records are immutable, so a hash comparison alone cannot see that a
    different change already superseded it."""

    fake = LANE_B.FakeGitHub()
    save_op = "stale-change-create"
    first_op = "stale-change-first"
    second_op = "stale-change-second"
    provider = _provider(fake, _adapter({save_op: FIRST_TIME, first_op: FIRST_TIME, second_op: FIRST_TIME}))
    write_auth = _authorization(BrainOperation.MEMORY_SAVE, "write")
    change_auth = _authorization(BrainOperation.MEMORY_CHANGE, "write")

    created = anyio.run(provider.save, write_auth, _memory_request(save_op, "item"))
    first_request = _memory_change_request(first_op, created.record_id, created.content_sha256)
    second_request = _memory_change_request(
        second_op, created.record_id, created.content_sha256, state="blocked"
    )

    _refused_item_change(provider, change_auth, first_request)
    first_result = _door(provider, first_op, "I started A item record",
        {"move": "start", "thing": created.record_id})
    assert first_result.status == "accepted"
    assert first_result.record_id != created.record_id

    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.change, change_auth, second_request)
    assert refused.value.code == "ACCESS_DENIED" and refused.value.fix

    # Only the first, accepted change actually landed a second record.
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    assert len([path for path in tree if path.startswith("records/item/")]) == 2


def test_memory_change_of_the_newest_version_succeeds_and_first_change_still_replays() -> None:
    """Requirement 2: the newest version can still be changed, and replaying the
    FIRST change (same operation_id, same input) must keep returning its original
    receipt rather than being caught by the new stale-head check."""

    fake = LANE_B.FakeGitHub()
    save_op = "chain-change-create"
    first_op = "chain-change-first"
    second_op = "chain-change-second"
    provider = _provider(fake, _adapter({save_op: FIRST_TIME, first_op: FIRST_TIME, second_op: LATER_TIME}))
    write_auth = _authorization(BrainOperation.MEMORY_SAVE, "write")
    change_auth = _authorization(BrainOperation.MEMORY_CHANGE, "write")

    created = anyio.run(provider.save, write_auth, _memory_request(save_op, "item"))
    first_request = _memory_change_request(first_op, created.record_id, created.content_sha256)
    _refused_item_change(provider, change_auth, first_request)
    first_result = _door(provider, first_op, "I started A item record",
        {"move": "start", "thing": created.record_id})
    assert first_result.status == "accepted"

    second_request = _memory_change_request(
        second_op, first_result.record_id, first_result.content_sha256, state="blocked"
    )
    _refused_item_change(provider, change_auth, second_request)
    second_result = _door(provider, second_op, "Next I will check the fixture",
        {"move": "next_action", "thing": first_result.record_id, "text": "check the fixture"})
    assert second_result.status == "accepted"
    assert second_result.record_id != first_result.record_id

    writes_before_replay = _writes(fake)
    _refused_item_change(provider, change_auth, first_request)
    replay = _door(provider, first_op, "I started A item record",
        {"move": "start", "thing": created.record_id})
    assert replay.status == "duplicate"
    assert replay.record_id == first_result.record_id
    assert replay.content_sha256 == first_result.content_sha256
    assert _writes(fake) == writes_before_replay


def test_memory_read_of_a_superseded_record_reports_it_is_stale() -> None:
    """Requirement 3: memory.read of a superseded record says so; the current head
    still reads back fresh."""

    fake = LANE_B.FakeGitHub()
    save_op = "read-stale-create"
    change_op = "read-stale-change"
    provider = _provider(fake, _adapter({save_op: FIRST_TIME, change_op: FIRST_TIME}))
    write_auth = _authorization(BrainOperation.MEMORY_SAVE, "write")
    change_auth = _authorization(BrainOperation.MEMORY_CHANGE, "write")
    read_auth = _authorization(BrainOperation.MEMORY_READ, "read")

    created = anyio.run(provider.save, write_auth, _memory_request(save_op, "item"))
    _refused_item_change(provider, change_auth, _memory_change_request(change_op, created.record_id, created.content_sha256))
    changed = _door(provider, change_op, "I started A item record", {"move": "start", "thing": created.record_id})

    original = anyio.run(provider.read, read_auth, created.record_id)
    assert original.freshness == "stale"
    assert original.superseded_by == (changed.record_id,)

    head = anyio.run(provider.read, read_auth, changed.record_id)
    assert head.freshness == "current"
    assert head.superseded_by == ()


def test_two_hosts_memory_absence_race_converges_on_first_canonical_record() -> None:
    fake = LANE_B.FakeGitHub()
    initial_revision = fake.branch
    order = _OrderedPublication()
    operation_id = "two-host-memory"
    request = _memory_request(operation_id, "item")
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    first = GitHubBrainProvider(
        _OrderedStorage(fake, order, "first"),
        _adapter({operation_id: FIRST_TIME}),
    )
    second = GitHubBrainProvider(
        _OrderedStorage(fake, order, "second"),
        _adapter({operation_id: LATER_TIME}),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(anyio.run, first.save, authorization, request)
        assert order.first_entered.wait(5)
        second_future = executor.submit(anyio.run, second.save, authorization, request)
        first_result = first_future.result(timeout=10)
        second_result = second_future.result(timeout=10)

    assert first_result.status == "accepted"
    assert second_result.status == "duplicate"
    assert second_result.record_id == first_result.record_id
    first_attempts = [item for item in order.expected_revisions if item[1] == "memory"]
    assert first_attempts[:2] == [
        ("first", "memory", initial_revision),
        ("second", "memory", initial_revision),
    ]
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    records = [path for path in tree if path.startswith("records/item/")]
    assert len(records) == 1
    canonical = parse_record(fake.blobs[tree[records[0]]].decode("utf-8"), records[0])
    assert canonical.created_at == FIRST_TIME


def test_memory_record_suffix_matches_gt_identity() -> None:
    """P3 (finding 15): the identity's record suffix is scoped by the authenticated
    caller (here the fixture evidence's actor, "nic", with no host attribution bound)."""

    operation_id = "identity-suffix-check"
    adapter = _adapter({operation_id: FIRST_TIME})
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    request = _memory_request(operation_id, "item")

    identity = adapter.memory_identity(authorization, request)

    assert identity.source["recorded_by"] == "nic"
    assert identity.record_entropy == memory_operation_record_suffix(operation_id, caller="nic")
    assert identity.record_entropy != memory_operation_record_suffix(operation_id)


def test_concurrent_same_operation_waits_and_returns_duplicate() -> None:
    fake = LANE_B.FakeGitHub()
    entered_publish = threading.Event()
    release_publish = threading.Event()

    class _BlockingStorage(GitHubBrainHomeStorage):
        def publish_record(self, **kwargs):
            entered_publish.set()
            assert release_publish.wait(5), "test did not release the in-flight publish"
            return super().publish_record(**kwargs)

        def publish_records(self, **kwargs):
            entered_publish.set()
            assert release_publish.wait(5), "test did not release the in-flight publish"
            return super().publish_records(**kwargs)

    storage_ = _BlockingStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=_HistoryTransport(fake),
        object_cache=GitHubObjectCache(),
    )
    operation_id = "concurrent-save-check"
    provider = GitHubBrainProvider(storage_, _adapter({operation_id: FIRST_TIME}))
    request = _memory_request(operation_id, "item")
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(anyio.run, provider.save, authorization, request)
        assert entered_publish.wait(5), "first save did not reach publish"
        second_future = executor.submit(anyio.run, provider.save, authorization, request)
        assert not second_future.done(), "resend must wait for the in-flight save, not race it"
        release_publish.set()
        first_result = first_future.result(timeout=10)
        second_result = second_future.result(timeout=10)

    assert first_result.status == "accepted"
    assert second_result.status == "duplicate"
    assert second_result.record_id == first_result.record_id
    assert len([event for event in fake.events if event.startswith("POST_COMMIT")]) == 1


def test_decision_publishes_atomic_existing_writer_record_and_receipt() -> None:
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({"decision-one": FIRST_TIME}))

    result = anyio.run(
        provider.record,
        _authorization(BrainOperation.DECISION_RECORD, "write"),
        _decision("decision-one"),
    )

    assert result.status == "accepted"
    assert result.remote_observed is True
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    record_paths = [path for path in tree if path.endswith(".md")]
    receipt_paths = [path for path in tree if path.endswith(".json")]
    assert len(record_paths) == len(receipt_paths) == 1
    record_bytes = fake.blobs[tree[record_paths[0]]]
    receipt_bytes = fake.blobs[tree[receipt_paths[0]]]
    record = parse_record(record_bytes.decode("utf-8"), record_paths[0])
    receipt = json.loads(receipt_bytes)
    assert record.id == result.record_id
    assert receipt["status"] == "created"
    assert receipt["canonical_identity"] == record.id
    assert receipt["canonical_sha256"] == hashlib.sha256(record_bytes).hexdigest()
    assert result.receipt_sha256 == hashlib.sha256(receipt_bytes).hexdigest()
    assert len([event for event in fake.events if event.startswith("POST_COMMIT")]) == 1


def test_exact_decision_replay_is_remote_duplicate_without_another_write() -> None:
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({"decision-replay": FIRST_TIME}))
    authorization = _authorization(BrainOperation.DECISION_RECORD, "write")
    request = _decision("decision-replay")

    created = anyio.run(provider.record, authorization, request)
    writes = _writes(fake)
    duplicate = anyio.run(provider.record, authorization, request)

    assert duplicate.status == "duplicate"
    assert duplicate.record_id == created.record_id
    assert duplicate.content_sha256 == created.content_sha256
    assert duplicate.receipt_sha256 == created.receipt_sha256
    assert _writes(fake) == writes


def test_same_origin_with_conflicting_input_fails_before_remote_write() -> None:
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({"decision-conflict": FIRST_TIME}))
    authorization = _authorization(BrainOperation.DECISION_RECORD, "write")
    anyio.run(provider.record, authorization, _decision("decision-conflict"))
    writes = _writes(fake)

    with pytest.raises(BrainRuntimeError) as conflict:
        anyio.run(
            provider.record,
            authorization,
            _decision("decision-conflict", because="A different stable input"),
        )

    assert conflict.value.code == "brain.record.exists"
    assert conflict.value.retryable is False
    assert _writes(fake) == writes


def test_lost_accepted_decision_reply_reconciles_before_retry() -> None:
    fake = LANE_B.FakeGitHub()
    fake.lost_apply_once = True
    provider = _provider(fake, _adapter({"decision-lost-reply": FIRST_TIME}))

    result = anyio.run(
        provider.record,
        _authorization(BrainOperation.DECISION_RECORD, "write"),
        _decision("decision-lost-reply"),
    )

    assert result.status == "accepted"
    assert result.remote_observed is True
    patches = [event for event in fake.events if event.startswith("PATCH_")]
    assert len(patches) == 1
    assert patches[0].startswith("PATCH_LOST_ACCEPTED")


def test_later_new_origin_semantically_dedupes_to_one_remote_decision() -> None:
    fake = LANE_B.FakeGitHub()
    authorization = _authorization(BrainOperation.DECISION_RECORD, "write")
    first_provider = _provider(
        fake, _adapter({"semantic-origin-one": FIRST_TIME})
    )
    first = anyio.run(
        first_provider.record, authorization, _decision("semantic-origin-one")
    )
    first_tree = fake.trees[fake.commits[fake.branch]["tree"]]
    canonical_path = next(path for path in first_tree if path.endswith(".md"))

    # A fresh adapter has no in-memory winner.  Its trusted semantic index points
    # to the accepted remote record, which is the sole record staged for the real
    # writer's later-date duplicate pass.
    second_provider = _provider(
        fake,
        _adapter(
            {"semantic-origin-two": LATER_TIME},
            semantic_locator=lambda _authorization, _semantic_key: canonical_path,
        ),
    )
    second = anyio.run(
        second_provider.record, authorization, _decision("semantic-origin-two")
    )

    assert first.status == "accepted"
    assert second.status == "duplicate"
    assert second.record_id == first.record_id
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    record_paths = [path for path in tree if path.endswith(".md")]
    receipt_paths = sorted(path for path in tree if path.endswith(".json"))
    assert len(record_paths) == 1
    assert receipt_paths == [
        "records/.durability/receipts/semantic-origin-one.json",
        "records/.durability/receipts/semantic-origin-two.json",
    ]
    first_receipt = json.loads(fake.blobs[tree[receipt_paths[0]]])
    second_receipt = json.loads(fake.blobs[tree[receipt_paths[1]]])
    assert first_receipt["status"] == "created"
    assert second_receipt["status"] == "duplicate/no-op"
    assert first_receipt["canonical_identity"] == second_receipt["canonical_identity"]
    assert first_receipt["semantic_key"] == second_receipt["semantic_key"]
    canonical = parse_record(
        fake.blobs[tree[record_paths[0]]].decode("utf-8"), record_paths[0]
    )
    assert canonical.created_at == FIRST_TIME


def test_decision_semantic_key_uses_bc_whitespace_normalization() -> None:
    fake = LANE_B.FakeGitHub()
    authorization = _authorization(BrainOperation.DECISION_RECORD, "write")
    first = _provider(fake, _adapter({"normalized-origin-one": FIRST_TIME}))
    accepted = anyio.run(
        first.record, authorization, _decision("normalized-origin-one")
    )

    second = _provider(fake, _adapter({"normalized-origin-two": LATER_TIME}))
    duplicate = anyio.run(
        second.record,
        authorization,
        _decision(
            "normalized-origin-two",
            choice="  Publish through the configured Brain Home branch  ",
            governing_scope="  Brain Home publication  ",
        ),
    )

    assert duplicate.status == "duplicate"
    assert duplicate.record_id == accepted.record_id
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    assert len([path for path in tree if path.startswith("records/decision/")]) == 1


def test_two_hosts_decision_absence_race_keeps_one_canonical_record() -> None:
    fake = LANE_B.FakeGitHub()
    initial_revision = fake.branch
    order = _OrderedPublication()
    authorization = _authorization(BrainOperation.DECISION_RECORD, "write")
    first = GitHubBrainProvider(
        _OrderedStorage(fake, order, "first"),
        _adapter({"race-decision-one": FIRST_TIME}),
    )
    second = GitHubBrainProvider(
        _OrderedStorage(fake, order, "second"),
        _adapter({"race-decision-two": LATER_TIME}),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            anyio.run, first.record, authorization, _decision("race-decision-one")
        )
        assert order.first_entered.wait(5)
        second_future = executor.submit(
            anyio.run, second.record, authorization, _decision("race-decision-two")
        )
        first_result = first_future.result(timeout=10)
        second_result = second_future.result(timeout=10)

    assert first_result.status == "accepted"
    assert second_result.status == "duplicate"
    assert second_result.record_id == first_result.record_id
    decision_attempts = [
        item for item in order.expected_revisions if item[1] == "decision"
    ]
    assert decision_attempts[:2] == [
        ("first", "decision", initial_revision),
        ("second", "decision", initial_revision),
    ]
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    records = [path for path in tree if path.startswith("records/decision/")]
    receipts = [
        path for path in tree if path.startswith("records/.durability/receipts/")
    ]
    assert len(records) == 1
    assert len(receipts) == 2


def test_fresh_process_exact_replay_uses_accepted_bytes_without_rematerializing() -> None:
    fake = LANE_B.FakeGitHub()
    authorization = _authorization(BrainOperation.DECISION_RECORD, "write")
    request = _decision("fresh-process-replay")
    first_provider = _provider(
        fake, _adapter({"fresh-process-replay": FIRST_TIME})
    )
    created = anyio.run(first_provider.record, authorization, request)
    tree_before = fake.trees[fake.commits[fake.branch]["tree"]]
    record_path = next(path for path in tree_before if path.endswith(".md"))
    accepted_bytes = fake.blobs[tree_before[record_path]]
    accepted_record = parse_record(accepted_bytes.decode("utf-8"), record_path)
    writes_before = _writes(fake)

    def must_not_rematerialize(_: str) -> datetime:
        raise AssertionError("fresh replay must reconcile accepted material before materialization")

    fresh_provider = _provider(
        fake,
        _adapter(operation_time=must_not_rematerialize),
    )
    replayed = anyio.run(fresh_provider.record, authorization, request)

    tree_after = fake.trees[fake.commits[fake.branch]["tree"]]
    assert replayed.status == "duplicate"
    assert replayed.record_id == created.record_id
    assert _writes(fake) == writes_before
    assert fake.blobs[tree_after[record_path]] == accepted_bytes
    observed = parse_record(fake.blobs[tree_after[record_path]].decode("utf-8"), record_path)
    assert observed.created_at == accepted_record.created_at == FIRST_TIME
    assert observed.updated_at == accepted_record.updated_at == FIRST_TIME


def test_provider_authorization_fails_before_source_or_body_materialization() -> None:
    fake = LANE_B.FakeGitHub()
    calls = 0

    def source(_: OwnerAuthorization, __: str) -> Evidence:
        nonlocal calls
        calls += 1
        return _evidence(_, __)

    adapter = BrainCoreRecordAdapter(
        operation_time=lambda _: FIRST_TIME,
        source_evidence=source,
    )
    provider = _provider(fake, adapter)
    wrong_scope = OwnerAuthorization(
        operation=BrainOperation.MEMORY_SAVE,
        access="write",
        selector=OwnerSelector("personal"),
        owner_id="meta-vault",
        owner_locator="vault://meta-vault",
        policy_revision="fixture-policy-v1",
        capability=object(),
    )

    with pytest.raises(BrainRuntimeError) as denied:
        anyio.run(provider.save, wrong_scope, _memory_request("denied-memory", "item"))

    assert denied.value.code == "brain.policy.excluded"
    assert calls == 0
    assert _writes(fake) == ()


def test_unsupported_memory_kind_names_the_valid_values():
    """2f/ROA-6: the fallback in _raw_memory_data names the field and the fix, instead of
    a bare 'Unsupported memory kind'."""

    adapter = _adapter({"kind-fixture": FIRST_TIME})
    junk = replace(_memory_request("kind-fixture", "item"), kind="routine")
    with pytest.raises(BrainRuntimeError) as excinfo:
        adapter._raw_memory_data(_authorization(BrainOperation.MEMORY_SAVE, "write"), junk, "nic")
    assert excinfo.value.code == "brain.invalid_argument"
    assert excinfo.value.message == "kind must be item, fact, or preference"


def test_invalid_memory_record_message_carries_the_underlying_field_error():
    """2f/ROA-6: 'Memory request is not a valid Brain Core record' used to name neither
    the field nor the rule (found live 2026-09-26); it must now carry records.py's own
    error, the same way the words/speaker refusals already do."""

    adapter = _adapter({"bad-record": FIRST_TIME})
    request = _memory_request("bad-record", "item")
    with pytest.raises(BrainRuntimeError) as excinfo:
        adapter.materialize_memory(_authorization(BrainOperation.MEMORY_SAVE, "write"), request)
    assert excinfo.value.code == "brain.contract.invalid"
    assert excinfo.value.message.startswith("Memory request is not a valid Brain Core record: ")
    assert "dedupe_key" in excinfo.value.message


def test_invalid_memory_change_message_carries_the_underlying_field_error():
    from brain_core.records import record_from_mapping

    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({"change-source": FIRST_TIME, "bad-change": FIRST_TIME}))
    saved = anyio.run(
        provider.save, _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request("change-source", "item"),
    )
    observed = anyio.run(
        provider.read, _authorization(BrainOperation.MEMORY_READ, "read"), saved.record_id
    )
    previous = record_from_mapping(observed.canonical_record, "test fixture")
    change_request = _memory_change_request("bad-change", saved.record_id, saved.content_sha256)
    with pytest.raises(BrainRuntimeError) as excinfo:
        provider._records.materialize_memory_change(
            _authorization(BrainOperation.MEMORY_CHANGE, "write"), change_request, previous
        )
    assert excinfo.value.code == "brain.contract.invalid"
    assert excinfo.value.message.startswith("Memory change is not a valid Brain Core record: ")
    assert "dedupe_key" in excinfo.value.message


def test_decision_operation_id_message_names_the_fix():
    """_existing_origin's own shape check (2f); decision_semantic_key's separate,
    stricter brain-core validation runs first inside the real .record() path, so this
    exercises _existing_origin directly the way _record's loop calls it."""

    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({}))
    with pytest.raises(BrainRuntimeError) as excinfo:
        provider._existing_origin(
            _authorization(BrainOperation.DECISION_RECORD, "write"),
            _decision("Decision With Spaces"),
        )
    assert excinfo.value.code == "brain.invalid_argument"
    assert "operation_id must match" in excinfo.value.message


def test_guessed_preference_refused_before_clock_or_publication():
    """S6/ROA-17: a preference is allowed only from the owner's own direct words; an
    agent's guess is refused before the clock is read or anything is published, the
    same as the old blanket refusal did."""

    fake = LANE_B.FakeGitHub()
    before = fake.branch
    def no_clock(_):
        raise AssertionError("refused preference must not materialize")
    provider = _provider(fake, _adapter(operation_time=no_clock))
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(
            provider.save,
            _authorization(BrainOperation.MEMORY_SAVE, "write"),
            _memory_request(
                "refused-preference", "preference",
                said_by="agent", came_by="agent_guess", speaker="fixture-agent",
            ),
        )
    assert refused.value.code == "brain.record.needs_owner_words"
    assert "suggested item" in refused.value.message
    assert fake.branch == before


def test_owner_worded_preference_is_accepted():
    """S6/ROA-17: said_by owner + came_by direct + words is enough to save a preference;
    source.words carries the same evidence excerpt an item would (memory_identity is
    kind-agnostic), so no separate plumbing is needed for the preference path."""

    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({"accepted-preference": FIRST_TIME}))
    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request("accepted-preference", "preference"),
    )
    assert saved.status == "accepted" and saved.remote_observed is True
    assert saved.state == "active"
    observed = anyio.run(
        provider.read, _authorization(BrainOperation.MEMORY_READ, "read"), saved.record_id
    )
    assert observed.canonical_record["source"]["words"] == "Validated preference body"
    assert observed.canonical_record["source"]["said_by"] == "owner"


# --- 2c: everyday time words, and the find() fallback when time_words is omitted ----


def test_provider_save_of_dinner_with_sam_tonight_at_7pm_finds_the_one_time_phrase() -> None:
    """docs/product/steps/2026-09-26-gtbc-speed-goal.md §6, stream 2c "done when": a
    provider-level save with no time_words but one findable phrase in words resolves
    both the date and the time."""

    operation_id = "auto-find-single-phrase"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id,
            "item",
            words="dinner with Sam tonight at 7pm",
            time_words=None,
        ),
    )

    assert saved.status == "accepted"
    assert _dates(provider, saved) == {"appointment": {"day": "2026-09-21", "at": "19:00",
        "words": "tonight at 7pm", "read_on": "2026-09-21", "read_by": "code"}}


def test_provider_save_joins_a_day_and_a_time_said_apart() -> None:
    """Nic, 29 Sep 2026 ("tmr need to bring banner to printer at 12pm"): a day and a time at
    opposite ends of his message are one moment, found with no time_words."""

    operation_id = "auto-find-day-and-time-apart"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words="tmr need to bring banner to printer at 7pm", time_words=None),
    )

    assert saved.status == "accepted"
    assert _dates(provider, saved) == {"appointment": {"day": "2026-09-22", "at": "19:00",
        "words": "tmr at 7pm", "read_on": "2026-09-21", "read_by": "code"}}


def test_provider_save_accepts_time_words_taken_from_apart_pieces() -> None:
    operation_id = "time-words-from-pieces"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id, "item", words="tmr need to bring banner to printer at 7pm", time_words="tmr at 7pm"
        ),
    )

    assert saved.status == "accepted"
    date = _dates(provider, saved)["appointment"]
    assert date["day"] == "2026-09-22"
    assert date["words"] == "tmr at 7pm" and date["read_on"] == "2026-09-21"


def test_provider_save_uses_his_time_when_the_agent_rewords_it() -> None:
    """Nic, 29 Sep 2026: the agent need not use his exact words. Its own wording gives way
    to the time his message names, instead of a refusal and a retry."""

    operation_id = "reworded-time-words"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id, "item", words="tmr need to bring banner to printer at 7pm", time_words="tomorrow 7pm"
        ),
    )

    assert saved.status == "accepted"
    date = _dates(provider, saved)["appointment"]
    assert date["day"] == "2026-09-22"
    assert date["at"] == "19:00"
    assert date["words"] == "tmr at 7pm" and date["read_on"] == "2026-09-21"


def test_provider_save_keeps_his_day_when_the_agent_sends_only_the_time() -> None:
    """Nic, 29 Sep 2026: Diablo sent "at 12pm" of "tmr need to bring banner to printer at 12pm";
    the day he said must not be lost to it."""

    operation_id = "partial-time-words"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id, "item", words="tmr need to bring banner to printer at 7pm", time_words="at 7pm"
        ),
    )

    assert saved.status == "accepted"
    date = _dates(provider, saved)["appointment"]
    assert date["day"] == "2026-09-22"  # not today, the 21st, as "at 7pm" alone gives
    assert date["words"] == "tmr at 7pm" and date["read_on"] == "2026-09-21"


def test_provider_save_still_refuses_a_time_he_never_said() -> None:
    operation_id = "invented-time-words"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(provider.save, _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words="need to bring banner to the printer", time_words="Friday 7pm"))
    assert saved.status == "accepted" and _dates(provider, saved) == {}
    assert any(flag["field"] == "when" and flag["dropped"] and "not in his words" in flag["message"]
        for flag in saved.record["flags"])
    assert getattr(saved, "calendar", None) is None


def test_provider_save_with_explicit_time_words_resolves_date_and_time_together() -> None:
    operation_id = "explicit-date-and-time"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words="pay rent by Friday 7pm", time_words="Friday 7pm"),
    )

    assert saved.status == "accepted"
    date = next(iter(_dates(provider, saved).values()))
    assert date["day"] == "2026-09-25"
    assert date["at"] == "19:00" and date["read_on"] == "2026-09-21"


def test_provider_save_leaves_timing_empty_when_words_contain_several_different_phrases() -> None:
    """"Several different phrases -> don't guess: leave timing empty"
    (docs/product/2026-09-26-gtbc-speed-goal.md §6, stream 2c)."""

    operation_id = "auto-find-multiple-phrases"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id,
            "item",
            words="call mum Friday 7pm and also next Tuesday at 9am",
            time_words=None,
        ),
    )

    assert saved.status == "accepted"
    assert saved.timing == {}


def test_provider_save_does_not_read_an_english_word_as_a_weekday() -> None:
    """"sun" is a weekday form and a word: the save stays undated rather than due Sunday."""

    operation_id = "auto-find-english-word"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words="Put the basil in the sun", time_words=None),
    )

    assert saved.status == "accepted"
    assert saved.timing == {}


def test_provider_save_does_not_auto_find_when_time_words_is_already_given() -> None:
    operation_id = "explicit-time-words-wins"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(
            operation_id,
            "item",
            # R7 (docs/product/steps/c-record-format.md) still requires the given
            # time_words to appear in words, so "today" must literally be there even
            # though the sentence also contains other, unrelated time phrases.
            words="call mum Friday 7pm and also next Tuesday at 9am, or maybe today",
            time_words="today",
        ),
    )

    assert saved.status == "accepted"
    date = _dates(provider, saved)["planned"]
    assert date["day"] == "2026-09-21"
    assert "at" not in date and date["words"] == "today"


def test_provider_save_refuses_a_bare_hour_time_words_as_unresolved() -> None:
    operation_id = "bare-hour-refused"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))

    saved = anyio.run(provider.save, _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words="ping me at 7", time_words="7"))
    assert saved.status == "accepted" and _dates(provider, saved) == {}
    assert any(flag["field"] == "when" and flag["dropped"] and "could not read" in flag["message"]
        for flag in saved.record["flags"])
    assert getattr(saved, "calendar", None) is None


def test_memory_change_does_not_auto_find_time_words_from_words() -> None:
    """"Change requests do not auto-find" (docs/product/2026-09-26-gtbc-speed-goal.md
    §6, stream 2c): a change with words containing a time phrase but no time_words
    leaves timing untouched, unlike a save."""

    operation_id = "change-no-auto-find"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME, f"change-of-{operation_id}": LATER_TIME}))

    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words="a plain task with no date"),
    )
    old = _memory_change_request(f"change-of-{operation_id}", saved.record_id, saved.content_sha256,
        words="tonight at 7pm", time_words=None)
    head = fake.branch
    _refused_item_change(provider, _authorization(BrainOperation.MEMORY_CHANGE, "write"), old)
    assert fake.branch == head
    changed = _door(provider, f"change-of-{operation_id}", "I started this tonight at 7pm",
        {"move": "start", "thing": saved.record_id})
    assert changed.status == "accepted"
    assert _dates(provider, changed) == {} and changed.timing == {}


# --- 2e / P3: operation ids scoped by the authenticated caller ----------------------


def test_two_agents_with_the_same_operation_id_get_two_distinct_records() -> None:
    """Finding 15: agents mint operation ids from the words, so two different agents
    can land on the same id. Scoping the record suffix (and its preflight search) by
    the authenticated caller means they still get two separate records."""

    from gremlin_town.mcp.host_identity import bind_write_attribution

    operation_id = "buy-milk"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")

    with bind_write_attribution("nic"):
        nic_saved = anyio.run(
            provider.save, authorization,
            _memory_request(operation_id, "item", words="buy milk today", title="Nic's reminder"),
        )
    with bind_write_attribution("damien"):
        damien_saved = anyio.run(
            provider.save, authorization,
            _memory_request(operation_id, "item", words="buy milk tomorrow", title="Diablo's reminder"),
        )

    assert nic_saved.status == "accepted"
    assert damien_saved.status == "accepted"
    assert nic_saved.record_id != damien_saved.record_id


def test_replay_by_either_caller_returns_its_own_record() -> None:
    from gremlin_town.mcp.host_identity import bind_write_attribution

    operation_id = "buy-milk-replay"
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({operation_id: FIRST_TIME}))
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")

    def save(caller: str, words: str):
        with bind_write_attribution(caller):
            return anyio.run(provider.save, authorization, _memory_request(operation_id, "item", words=words))

    nic_first = save("nic", "buy milk today")
    damien_first = save("damien", "buy milk tomorrow")
    nic_replay = save("nic", "buy milk today")
    damien_replay = save("damien", "buy milk tomorrow")

    assert nic_first.status == "accepted"
    assert damien_first.status == "accepted"
    assert nic_replay.status == "duplicate"
    assert nic_replay.record_id == nic_first.record_id
    assert damien_replay.status == "duplicate"
    assert damien_replay.record_id == damien_first.record_id


def test_pre_release_legacy_suffix_record_is_found_by_its_author_and_not_by_others() -> None:
    """A record saved before caller scoping was installed still carries the legacy,
    unscoped suffix. Its own author's replay still finds it (status duplicate); a
    different caller using the same operation_id does not see it and gets its own
    new record instead - not a conflict."""

    from dataclasses import replace as dataclass_replace
    from gremlin_town.mcp.host_identity import bind_write_attribution

    operation_id = "pre-release-op"
    fake = LANE_B.FakeGitHub()
    adapter = _adapter({operation_id: FIRST_TIME})
    storage_instance = _hub_storage(fake)
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    nic_request = _memory_request(operation_id, "item", words="renew the passport")

    with bind_write_attribution("nic"):
        identity = adapter.memory_identity(authorization, nic_request)
        legacy_identity = dataclass_replace(
            identity,
            record_entropy=memory_operation_record_suffix(operation_id),
            dedupe_key=memory_operation_dedupe_key(operation_id, identity.input_sha256),
        )
        material = adapter.materialize_memory(authorization, nic_request, identity=legacy_identity)
    published = storage_instance.publish_record(record_bytes=material.record_bytes)

    provider = GitHubBrainProvider(storage_instance, adapter)

    with bind_write_attribution("nic"):
        nic_replay = anyio.run(provider.save, authorization, nic_request)
    assert nic_replay.status == "duplicate"
    assert nic_replay.record_id == published.record_id

    head = fake.branch
    with bind_write_attribution("nic"), pytest.raises(BrainRuntimeError) as conflict:
        anyio.run(provider.save, authorization, replace(nic_request, body="changed original note"))
    assert conflict.value.fix and fake.branch == head

    with bind_write_attribution("damien"):
        damien_saved = anyio.run(
            provider.save, authorization,
            _memory_request(operation_id, "item", words="renew mum's passport"),
        )
    assert damien_saved.status == "accepted"
    assert damien_saved.record_id != published.record_id


@pytest.mark.parametrize("importance", ["proposed", "confirmed", "real_cost", "minor"])
def test_exact_pre_cut_importance_and_sort_retry_is_read_only_and_caller_scoped(importance):
    """Literal original raw identity and stored shape, never a new legacy write."""
    from brain_core.contracts import canonical_json
    from brain_core.records import new_record_id, record_from_mapping, render_record
    from gremlin_town.mcp.host_identity import bind_write_attribution
    fake = LANE_B.FakeGitHub()
    adapter = _adapter({"old-annotated": FIRST_TIME})
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    request = _memory_request("old-annotated", "item", body="  original note\n\ninside\n",
        words="I must call; losing this cannot be undone; it takes 45 minutes", importance=importance,
        importance_reason="Losing this cannot be undone", sort="next_action", first_step="Call Nic", block="admin",
        rough_minutes=45)
    with bind_write_attribution("nic"):
        base = adapter.memory_identity(authorization, replace(request, importance=None, importance_reason=None, sort=None))
    raw_data = {**base.data, "importance_input": {"set": importance, "reason": request.importance_reason,
        "proposed_by": "nic"}, "sort_input": "next_action"}
    payload = {"schema": "gt.brain-memory-input/v2", "operation_id": request.operation_id,
        "kind": "item", "title": request.title, "body": request.body, "state": base.state,
        "owner_id": authorization.owner_id, "owner_locator": authorization.owner_locator,
        "evidence": base.evidence.as_dict(), "requested_dedupe_key": request.dedupe_key,
        "data": raw_data, "source": base.source, "links": []}
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    stored_data = {**adapter._resolved_memory_data("item", base.data, FIRST_TIME), "importance": {
        "level": "high" if importance in ("proposed", "confirmed") else importance,
        "reason": request.importance_reason, "proposed_by": "nic", "confirmed": importance == "confirmed"}}
    rid = new_record_id(FIRST_TIME, memory_operation_record_suffix(request.operation_id, caller="nic"))
    record = record_from_mapping({"schema": "brain.record/v2", "id": rid, "kind": "item",
        "title": request.title, "body": request.body, "state": "open", "created_at": FIRST_TIME.isoformat(),
        "updated_at": FIRST_TIME.isoformat(), "sensitivity": "private", "vaults": [authorization.owner_locator],
        "owners": [authorization.owner_id], "entities": [], "evidence": [base.evidence.as_dict()],
        "supersedes": [], "dedupe_key": memory_operation_dedupe_key(request.operation_id, digest),
        "data": stored_data, "source": base.source}, "literal accepted pre-cut record")
    fake.seed({f"records/item/2026/09/{rid}.md": render_record(record).encode()})
    provider = _provider(fake, adapter)
    head, writes = fake.branch, len(fake.patch_payloads)
    with bind_write_attribution("nic"):
        replay = anyio.run(provider.save, authorization, request)
    assert replay.status == "duplicate" and replay.record_id == rid
    assert fake.branch == head and len(fake.patch_payloads) == writes
    with bind_write_attribution("nic"), pytest.raises(BrainRuntimeError) as changed:
        anyio.run(provider.save, authorization, replace(request, importance_reason="different original input"))
    assert changed.value.fix and fake.branch == head
    with bind_write_attribution("local-codex"):
        other = anyio.run(provider.save, authorization, request)
    assert other.status == "accepted" and other.record_id != rid
    assert set(other.ignored) == {"importance", "importance_reason", "sort", "first_step"} and other.fix
    current = provider._storage.preflight_record_by_id(other.record_id).match.record
    assert not any(key in current.data for key in ("importance", "first_step", "sort", "sort_input", "importance_input"))


def test_invalid_new_stale_fields_are_ignored_but_cannot_claim_an_exact_legacy_binding():
    fake = LANE_B.FakeGitHub()
    provider = _provider(fake, _adapter({"invalid-stale": FIRST_TIME}))
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    request = _memory_request("invalid-stale", "item", size="unknown-size", importance="unknown",
        importance_reason="not an old setting", sort="drop", first_step="stale action",
        links=({"relation": "not-a-legacy-link", "target": "unknown"},))
    saved = anyio.run(provider.save, authorization, request)
    assert saved.status == "accepted" and saved.fix == "refresh the connector"
    assert set(saved.ignored) == {"size", "importance", "importance_reason", "sort", "first_step", "links"}
    record = provider._storage.preflight_record_by_id(saved.record_id).match.record
    assert not record.links and not any(key in record.data for key in ("importance", "sort", "first_step"))
    assert record.data.get("size", "task") == "task"
    head = fake.branch
    replay = anyio.run(provider.save, authorization, request)
    assert replay.status == "duplicate" and replay.record_id == saved.record_id and fake.branch == head


def test_unknown_legacy_identity_never_returns_duplicate_but_new_capture_ignores_it():
    from gremlin_town.mcp.host_identity import bind_write_attribution
    fake = LANE_B.FakeGitHub()
    adapter = _adapter({"old-plain": FIRST_TIME, "new-plain": FIRST_TIME})
    authorization = _authorization(BrainOperation.MEMORY_SAVE, "write")
    original = _memory_request("old-plain", "item")
    with bind_write_attribution("nic"):
        identity = adapter.memory_identity(authorization, original)
        bound = replace(identity, dedupe_key=memory_operation_dedupe_key(original.operation_id, identity.input_sha256))
        material = adapter.materialize_memory(authorization, original, identity=bound)
    fake.seed({material.record_path: material.record_bytes})
    provider = _provider(fake, adapter)
    head = fake.branch
    # Old importance_reason without importance was invalid before the cut. Its
    # omitted data field could share a binding, but must never imply a duplicate.
    with bind_write_attribution("nic"), pytest.raises(BrainRuntimeError) as invalid:
        anyio.run(provider.save, authorization, replace(original, importance_reason="unknown legacy instruction"))
    assert invalid.value.code == "brain.invalid_argument" and invalid.value.fix and fake.branch == head
    with bind_write_attribution("nic"):
        new = anyio.run(provider.save, authorization, replace(original,
            operation_id="new-plain", importance_reason="unknown legacy instruction"))
    assert new.status == "accepted" and new.ignored == ("importance_reason",) and new.fix


# --- speed-03 (Proposed): Nic's own Diablo tests on Sat 26 Sep 2026, through the provider ---

NIC_TEST_TIME = datetime.fromisoformat("2026-09-26T21:50:00+08:00")  # Saturday evening


def _nic_save(operation_id: str, words: str, time_words: str | None):
    provider = _provider(LANE_B.FakeGitHub(), _adapter({operation_id: NIC_TEST_TIME}))
    saved = anyio.run(
        provider.save,
        _authorization(BrainOperation.MEMORY_SAVE, "write"),
        _memory_request(operation_id, "item", words=words, time_words=time_words),
    )
    return saved, provider


def test_next_weeks_exam_is_saved_in_one_step_with_a_window_and_no_due_date() -> None:
    saved, provider = _nic_save("study-exam", "I need to study for next weeks exam", "next weeks")
    assert saved.status == "accepted"
    assert _dates(provider, saved) == {"planned": {"start": "2026-09-28", "end": "2026-10-04",
        "words": "next weeks", "read_on": "2026-09-26", "read_by": "code"}}
    assert "deadline" not in _dates(provider, saved)


@pytest.mark.parametrize("time_words", ["Later 12am", None], ids=["named", "found-in-words"])
def test_later_12am_said_at_2150_is_saved_as_the_coming_midnight(time_words: str | None) -> None:
    saved, provider = _nic_save("call-tommy", "Later 12am I got a call with Tommy", time_words)
    assert saved.status == "accepted"
    assert _dates(provider, saved) == {"appointment": {"day": "2026-09-27", "at": "00:00",
        "words": "Later 12am", "read_on": "2026-09-26", "read_by": "code"}}


def _hub_storage(fake):
    """A storage as the hub builds it, with an object cache: Brain Core refuses the hub's 5,000-file scan
    cap without one (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1)."""

    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=_HistoryTransport(fake),
        object_cache=GitHubObjectCache(),
    )
