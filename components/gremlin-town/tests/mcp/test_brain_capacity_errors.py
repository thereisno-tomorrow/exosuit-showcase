"""Brain Home's size limits, as agents see them (docs/product/2026-09-30-brain-home-storage-limit-plan.md,
layer 1): past a limit the error is `brain.capacity.exceeded` with one plain sentence, never "corrupt"; GitHub's
used-up hourly allowance is retryable; and an old version moved to Brain Home's `archive/` folder still reads
and refuses a change as it did before the move, while a thing archived whole stays not found.

Every test runs the real `GitHubBrainHomeStorage` against Brain Core's fake GitHub transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

import anyio
import pytest

from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache, GitHubStorageError, HTTPResponse

import gremlin_town.mcp.github_brain_provider as provider_module
import gremlin_town.mcp.github_continuity as continuity_module
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation,
    BrainRuntimeError,
    ContinuityListRequest,
    MemoryChangeRequest,
    MemoryListQuery,
    MemorySaveRequest,
    MemorySearchQuery,
    OwnerSelector,
    WorkRequest,
)
from gremlin_town.mcp.brain_tools import _mapped_error
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider, map_storage_error
from gremlin_town.mcp.github_continuity import GitHubContinuityProvider

from test_brain_composition import _settings
from test_github_brain_provider import (
    FIRST_TIME,
    LANE_B,
    _adapter,
    _authorization,
    _decision,
    _memory_change_request,
    _memory_request,
    _HistoryTransport,
)

OVER = "Nothing is lost. Ask for the storage tidy-up now."
# A save or change a limit stopped says nothing was saved, never only that nothing is lost.
OVER_UNSAVED = "Nothing was saved; what's already saved is safe. Ask for the storage tidy-up now."


def _hub_storage(fake) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=_HistoryTransport(fake),
        object_cache=GitHubObjectCache(),
    )


def _files(kind: str, count: int) -> dict[str, bytes]:
    # The cap is checked on the tree, before any file is read, so the files' bytes don't matter.
    return {f"records/{kind}/2026/09/brn_20260921_040000_{index:08x}.md": b"x" for index in range(count)}


# --- Past a cap: brain.capacity.exceeded, in plain words ---------------------------------------------------


def test_agents_list_past_the_cap_says_capacity_exceeded_not_corrupt(monkeypatch) -> None:
    monkeypatch.setattr(provider_module, "SCAN_MAX_RECORDS", 2)
    fake = LANE_B.FakeGitHub()
    fake.seed(_files("item", 3))
    provider = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=lambda _: FIRST_TIME))

    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.list, _authorization(BrainOperation.MEMORY_LIST, "read"), MemoryListQuery())

    assert refused.value.code == "brain.capacity.exceeded"
    assert refused.value.retryable is False
    assert refused.value.message == f"Brain Home holds 3 task records and the hub reads at most 2 at once. {OVER}"


def test_agents_search_names_every_kind_it_reads_together(monkeypatch) -> None:
    monkeypatch.setattr(provider_module, "SCAN_MAX_RECORDS", 2)
    fake = LANE_B.FakeGitHub()
    fake.seed({**_files("item", 2), **{path.replace("_0000", "_1111"): b"x" for path in _files("fact", 1)}})
    provider = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=lambda _: FIRST_TIME))

    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.search, _authorization(BrainOperation.MEMORY_SEARCH, "read"), MemorySearchQuery("deck"))

    assert refused.value.code == "brain.capacity.exceeded"
    assert refused.value.message == (
        f"Brain Home holds 3 task, fact, capture and commitment records and the hub reads at most 2 at once. {OVER}"
    )


def test_agents_notes_past_the_cap_say_capacity_exceeded(monkeypatch) -> None:
    monkeypatch.setattr(continuity_module, "SCAN_MAX_RECORDS", 1)
    fake = LANE_B.FakeGitHub()
    fake.seed(_files("continuity", 2))
    continuity = GitHubContinuityProvider(_hub_storage(fake), lambda _: FIRST_TIME)

    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(continuity.list_open, _authorization(BrainOperation.CONTINUITY_LIST, "read"), ContinuityListRequest())

    assert refused.value.code == "brain.capacity.exceeded"
    assert refused.value.message == f"Brain Home holds 2 note records and the hub reads at most 1 at once. {OVER}"


def test_recording_a_decision_checks_up_to_1000_and_past_it_says_so(monkeypatch) -> None:
    fake = LANE_B.FakeGitHub()
    storage = _hub_storage(fake)
    bounds: list[int] = []
    real = storage.preflight_decision_by_semantic_key

    def spy(semantic_key, *, max_candidates=256):
        bounds.append(max_candidates)
        return real(semantic_key, max_candidates=max_candidates)

    monkeypatch.setattr(storage, "preflight_decision_by_semantic_key", spy)
    provider = GitHubBrainProvider(storage, _adapter({"decision-one": FIRST_TIME, "decision-two": FIRST_TIME}))
    anyio.run(provider.record, _authorization(BrainOperation.DECISION_RECORD, "write"), _decision("decision-one"))
    assert bounds == [1000]

    monkeypatch.setattr(provider_module, "MAX_DECISION_CANDIDATES", 1)
    fake.seed(_files("decision", 1))  # a second decision file: two, past a bound of one
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.record, _authorization(BrainOperation.DECISION_RECORD, "write"), _decision("decision-two"))
    assert refused.value.code == "brain.capacity.exceeded"
    assert refused.value.message == (
        f"Brain Home holds 2 decision records and the hub reads at most 1 at once. {OVER_UNSAVED}"
    )


def test_a_tree_too_big_to_list_is_capacity_too_and_another_tree_is_retryable() -> None:
    truncated = GitHubStorageError(
        "github_tree_incomplete", "GitHub did not return the complete pinned tree",
        details={"truncated": True, "tree_matches": True},
    )
    different = GitHubStorageError(
        "github_tree_incomplete", "GitHub did not return the complete pinned tree",
        details={"truncated": False, "tree_matches": False},
    )

    too_big = map_storage_error(truncated, reading=True)
    other = map_storage_error(different, reading=True)

    assert (too_big.code, too_big.retryable) == ("brain.capacity.exceeded", False)
    assert too_big.message == (
        "Brain Home has grown too big for GitHub to hand over in one go, so the hub can't read it. Nothing is lost. "
        "Ask for the Brain Home size check."
    )
    assert (other.code, other.retryable) == ("brain.capability.unavailable", True)
    assert other.message == (
        "GitHub answered with a different version of Brain Home than the one asked for, so the hub couldn't read "
        "it this time. Nothing is lost. It should work again shortly."
    )


class _AllowanceUsedUp:
    def request(self, method, url, *, headers, body):  # type: ignore[no-untyped-def]
        return HTTPResponse(
            403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, b'{"message": "rate limited"}',
        )


def test_githubs_used_up_allowance_is_retryable_and_says_when_it_refills() -> None:
    storage = GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=_AllowanceUsedUp(),
        object_cache=GitHubObjectCache(),
    )
    provider = GitHubBrainProvider(storage, _adapter(operation_time=lambda _: FIRST_TIME))

    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.list, _authorization(BrainOperation.MEMORY_LIST, "read"), MemoryListQuery())

    # 1790000000 is 14:13 on 21 Sep 2026 UTC, 22:13 in Singapore.
    assert refused.value.code == "brain.capability.unavailable"
    assert refused.value.retryable is True
    assert refused.value.message == (
        "GitHub's hourly allowance for Brain Home is used up until 22:13. Nothing is lost; it works again then."
    )


def test_the_orientation_tools_map_a_storage_error_instead_of_internal_error(caplog) -> None:
    error = GitHubStorageError(
        "github_scan_limit", "Brain Home record scan exceeds its explicit limit",
        details={"kinds": ["item"], "limit": "files", "files": 5013, "max_records": 5000},
    )

    reply = _mapped_error(error)

    assert "Brain MCP operation failed on Brain Home storage (GitHubStorageError github_scan_limit)" in caplog.text

    assert reply.isError is True
    assert reply.structuredContent == {"error": {
        "code": "brain.capacity.exceeded",
        "message": f"Brain Home holds 5,013 task records and the hub reads at most 5,000 at once. {OVER}",
        "retryable": False,
        "field": "arguments", "fix": "split the change or ask the operator to make room; nothing was saved",
    }}


# --- An old version in archive/: read and change as before the move ----------------------------------------


def _move_to_archive(fake, *paths: str) -> None:
    """One commit moving each file to archive/ plus its path, byte for byte (the layout layer 2 will use)."""

    tree = dict(fake.trees[fake.commits[fake.branch]["tree"]])
    for path in paths:
        tree["archive/" + path] = tree.pop(path)
    tree_sha = fake._sha()
    fake.trees[tree_sha] = tree
    commit_sha = fake._sha()
    fake.commits[commit_sha] = {
        "tree": tree_sha, "parent": fake.branch, "message": "archive", "paths": {*paths, *("archive/" + p for p in paths)},
    }
    fake.branch = commit_sha


def _path_of(fake, record_id: str) -> str:
    [path] = [path for path in fake.trees[fake.commits[fake.branch]["tree"]] if path.endswith(f"/{record_id}.md")]
    return path


def _runtime_settings(tmp_path: Path):
    settings = _settings(tmp_path)
    policy = json.loads(settings.policy_file.read_text(encoding="utf-8"))
    policy["owners"][0]["operations"] += ["memory.change", "memory.list", "work"]
    for key in ('readers','writers'):
        policy['owners'][0][key].append('hermes-wa')
    settings.policy_file.write_text(json.dumps(policy), encoding="utf-8")
    return settings


CALLER = BrainCaller("chatgpt-web", frozenset({"bc:read", "bc:write"}))
PERSONAL = OwnerSelector("personal")
DOOR = BrainCaller('hermes-wa',frozenset({'bc:read','bc:write'}))
SAVED_AT = datetime.fromisoformat("2026-09-21T12:00:00+08:00")


def _save(operation_id: str) -> MemorySaveRequest:
    return MemorySaveRequest(
        operation_id=operation_id, kind="item", title="Renew the passport", body="Renew the passport",
        source_reference=f"mcp-request:{operation_id}", said_by="owner", came_by="direct",
        words="Renew the passport", level="accepted",
    )


def _change(operation_id: str, record_id: str, expected_sha256: str) -> MemoryChangeRequest:
    return MemoryChangeRequest(
        operation_id=operation_id, record_id=record_id, expected_sha256=expected_sha256,
        source_reference=f"mcp-request:{operation_id}", said_by="owner", came_by="direct",
        words="I'm waiting on the embassy", state="waiting",
    )


async def _door_change(runtime,remote,request,*,move=None):
    """Retired item mutation refuses; the real door performs its permitted wait."""
    head,writes=remote.branch,len(remote.patch_payloads)
    with pytest.raises(BrainRuntimeError) as denied:
        await runtime.change_memory(CALLER,PERSONAL,request)
    assert denied.value.code=='ACCESS_DENIED'
    assert denied.value.fix=="save it as a new thing with memory.save; Nic's morning review merges it"
    assert remote.branch==head and len(remote.patch_payloads)==writes
    permitted=move or {'move':'wait','kind':'needs','who':'the embassy'}
    result=await runtime.change_work(DOOR,PERSONAL,WorkRequest(request.operation_id,
        request.words,'owner','direct',({**permitted,'thing':request.record_id},)))
    record_id=result['things'][0]['record_id']
    current=await runtime.read_memory(CALLER,PERSONAL,record_id)
    return SimpleNamespace(status=result['status'],record_id=record_id,
        content_sha256=current.content_sha256,version=result['version'])


def test_reading_an_old_version_after_it_moved_to_the_archive_gives_the_same_reply(tmp_path: Path) -> None:
    """Through the assembled runtime, so its real record locator finds the ID in archive/."""

    settings = _runtime_settings(tmp_path)
    remote = LANE_B.FakeGitHub()

    async def exercise():
        runtime = build_brain_runtime(settings, github_transport=_HistoryTransport(remote), operation_clock=lambda: SAVED_AT)
        try:
            saved = await runtime.save_memory(CALLER, PERSONAL, _save("passport-save"))
            changed = await _door_change(runtime,remote,_change("passport-wait",saved.record_id,saved.content_sha256))
            before = await runtime.read_memory(CALLER, PERSONAL, saved.record_id)
        finally:
            await runtime.aclose()
        _move_to_archive(remote, _path_of(remote, saved.record_id))
        fresh = build_brain_runtime(settings, github_transport=_HistoryTransport(remote), operation_clock=lambda: SAVED_AT)
        try:
            after = await fresh.read_memory(CALLER, PERSONAL, saved.record_id)
            with pytest.raises(BrainRuntimeError) as refused:
                await fresh.change_memory(
                    CALLER, PERSONAL, _change("passport-wait-again", saved.record_id, saved.content_sha256),
                )
            # A replay of the change that replaced it still finds the version it replaced.
            replay = await _door_change(fresh,remote,_change("passport-wait",saved.record_id,saved.content_sha256))
        finally:
            await fresh.aclose()
        return saved, changed, before, after, refused.value, replay

    saved, changed, before, after, refused, replay = anyio.run(exercise)

    assert (before.freshness, before.superseded_by) == ("stale", (changed.record_id,))
    assert (after.freshness, after.superseded_by) == ("stale", (changed.record_id,))
    assert after.content_sha256 == before.content_sha256 == saved.content_sha256
    assert after.canonical_record == before.canonical_record
    assert refused.code == "ACCESS_DENIED"
    assert refused.fix=="save it as a new thing with memory.save; Nic's morning review merges it"
    assert (replay.status, replay.record_id) == ("duplicate", changed.record_id)


def test_a_thing_archived_whole_is_not_found_by_read_or_change(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path)
    remote = LANE_B.FakeGitHub()

    async def exercise():
        runtime = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: SAVED_AT)
        try:
            saved = await runtime.save_memory(CALLER, PERSONAL, _save("test-data-save"))
        finally:
            await runtime.aclose()
        _move_to_archive(remote, _path_of(remote, saved.record_id))  # test data, archived whole
        fresh = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: SAVED_AT)
        errors = []
        try:
            for call in (
                fresh.read_memory(CALLER, PERSONAL, saved.record_id),
                fresh.change_memory(CALLER, PERSONAL, _change("test-data-wait", saved.record_id, saved.content_sha256)),
            ):
                with pytest.raises(BrainRuntimeError) as missing:
                    await call
                errors.append(missing.value.code)
        finally:
            await fresh.aclose()
        return errors

    assert anyio.run(exercise) == ["brain.not_found", "brain.not_found"]


# --- The review of the branch (30 Sep 2026): reads map as reads, writes say nothing was saved -----------


def test_any_other_raw_storage_error_is_logged_and_stays_internal_error(caplog) -> None:
    """_mapped_error can't tell a read from a write, so only a size limit, the allowance or GitHub being
    unreachable is mapped; anything else is INTERNAL_ERROR, as before the branch, with its journal line."""

    reply = _mapped_error(GitHubStorageError("github_api_error", "GitHub rejected the requested operation"))

    assert reply.structuredContent == {"error": {
        "code": "INTERNAL_ERROR", "message": "Brain operation failed", "retryable": False,
        "field": "request", "fix": "correct the request and send it again",
    }}
    assert "Brain MCP operation failed on Brain Home storage (GitHubStorageError github_api_error)" in caplog.text


def test_a_read_whose_record_lookup_fails_maps_as_a_read() -> None:
    """The lookup of a record's path sits inside the read's own error mapping: a GitHub 5xx there is a
    read failure, not brain.record.write_failed."""

    from gremlin_town.mcp.github_brain_provider import BrainCoreRecordAdapter

    def failing_locator(authorization, record_id):
        raise GitHubStorageError(
            "github_api_error", "GitHub rejected the requested operation", details={"method": "GET", "status": 502},
        )

    adapter = BrainCoreRecordAdapter(
        operation_time=lambda _: FIRST_TIME, source_evidence=lambda *_: None, record_locator=failing_locator,
    )
    provider = GitHubBrainProvider(_hub_storage(LANE_B.FakeGitHub()), adapter)

    with pytest.raises(BrainRuntimeError) as failed:
        anyio.run(provider.read, _authorization(BrainOperation.MEMORY_READ, "read"), "brn_20260921_040000_00000000")

    assert failed.value.code == "brain.record.corrupt"  # as every other failed read maps
    assert failed.value.code != "brain.record.write_failed"


class _AllowanceUsedUpFrom:
    """GitHub answers normally, then refuses every request once `after` requests have been made."""

    def __init__(self, fake, after: int) -> None:
        self.fake, self.after, self.calls = fake, after, 0

    def request(self, method, url, *, headers, body):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls > self.after:
            return HTTPResponse(
                403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, b'{"message": "rate limited"}',
            )
        return self.fake.request(method, url, headers=headers, body=body)


def test_a_save_stopped_by_githubs_allowance_says_nothing_was_saved() -> None:
    storage = GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN,
        transport=_AllowanceUsedUpFrom(LANE_B.FakeGitHub(), after=0), object_cache=GitHubObjectCache(),
    )
    provider = GitHubBrainProvider(storage, _adapter(operation_time=lambda _: FIRST_TIME))

    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.save, _authorization(BrainOperation.MEMORY_SAVE, "write"), _memory_request("allowance", "item"))

    assert (refused.value.code, refused.value.retryable) == ("brain.capability.unavailable", True)
    assert refused.value.message == (
        "GitHub's hourly allowance for Brain Home is used up until 22:13, so nothing was saved. Try again then."
    )


def test_a_change_stopped_by_a_limit_says_nothing_was_saved(monkeypatch) -> None:
    from gremlin_town.mcp.host_identity import bind_write_attribution

    fake = LANE_B.FakeGitHub()
    provider = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=lambda _: FIRST_TIME))
    saved = anyio.run(provider.save, _authorization(BrainOperation.MEMORY_SAVE, "write"), _memory_request("to-change", "item"))
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as denied:
        anyio.run(provider.change,_authorization(BrainOperation.MEMORY_CHANGE,'write'),
            _memory_change_request('retired-change',saved.record_id,saved.content_sha256))
    assert denied.value.code=='ACCESS_DENIED' and denied.value.fix=="save it as a new thing with memory.save; Nic's morning review merges it"
    assert fake.branch==head and len(fake.patch_payloads)==writes
    monkeypatch.setattr(provider_module, "SCAN_MAX_RECORDS", 1)
    fake.seed(_files("item", 1))  # a second item file: past a limit of one
    head,writes=fake.branch,len(fake.patch_payloads)

    with pytest.raises(BrainRuntimeError) as refused:
        with bind_write_attribution('hermes-wa'):
            anyio.run(provider.work,_authorization(BrainOperation.WORK,'write'),
                WorkRequest('change-it',"I'm waiting on the embassy",'owner','direct',
                    ({'move':'wait','thing':saved.record_id,'kind':'needs','who':'the embassy'},)))

    assert refused.value.code == "brain.capacity.exceeded"
    assert refused.value.message == f"Brain Home holds 2 task records and the hub reads at most 1 at once. {OVER_UNSAVED}"
    assert fake.branch==head and len(fake.patch_payloads)==writes


# --- Archived files read by ID as tolerantly as a scan reads them --------------------------------------------


def _item(record_id: str, *, supersedes: tuple[str, ...] = (), title: str = "File the form") -> tuple[str, bytes]:
    import hashlib

    from brain_core.records import record_from_mapping, render_record

    moment = (
        f"{record_id[4:8]}-{record_id[8:10]}-{record_id[10:12]}T"
        f"{record_id[13:15]}:{record_id[15:17]}:{record_id[17:19]}+00:00"
    )
    record = record_from_mapping({
        "schema": "brain.record/v2", "id": record_id, "kind": "item", "title": title, "state": "open",
        "created_at": moment, "updated_at": moment, "sensitivity": "private",
        "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:item",
                      "content_sha256": hashlib.sha256(record_id.encode()).hexdigest(), "excerpt": title}],
        "supersedes": list(supersedes), "dedupe_key": None, "data": {"level": "accepted", "size": "task"},
        "body": title, "source": {"said_by": "owner", "came_by": "direct", "words": title, "recorded_by": "hermes-wa"},
    }, "storage-limit fixture")
    return f"records/item/{moment[:4]}/{moment[5:7]}/{record_id}.md", render_record(record).encode("utf-8")


def _item_file(record_id: str) -> tuple[str, bytes]:
    return _item(record_id)


def _entity_file(record_id: str) -> tuple[str, bytes]:
    from brain_core.records import record_from_mapping, render_record

    record = record_from_mapping({
        "schema": "brain.record/v1", "id": record_id, "kind": "entity", "title": "Sam", "state": "active",
        "created_at": "2026-09-21T04:00:00+00:00", "updated_at": "2026-09-21T04:00:00+00:00",
        "assertion": "explicit", "confidence": 1.0, "sensitivity": "private",
        "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:entity", "content_sha256": "e" * 64, "excerpt": "Sam"}],
        "supersedes": [], "dedupe_key": None,
        "data": {"entity_type": "person", "why_it_matters": "a colleague", "privacy": "private", "linked_events": ["x"]},
        "body": "",
    }, "entity fixture")
    return f"records/entity/2026/09/{record_id}.md", render_record(record).encode("utf-8")


def _read_in_a_fresh_runtime(tmp_path: Path, remote, record_id: str):
    settings = _runtime_settings(tmp_path)

    async def exercise():
        runtime = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: SAVED_AT)
        try:
            return await runtime.read_memory(CALLER, PERSONAL, record_id)
        finally:
            await runtime.aclose()

    return anyio.run(exercise)


def test_an_archived_file_that_does_not_parse_reads_as_not_found(tmp_path: Path, caplog) -> None:
    record_id = "brn_20260926_010000_aaaa9999"
    path = _item_file(record_id)[0]
    remote = LANE_B.FakeGitHub()
    remote.seed({"archive/" + path: b"not a record"})

    with pytest.raises(BrainRuntimeError) as missing:
        _read_in_a_fresh_runtime(tmp_path, remote, record_id)

    assert missing.value.code == "brain.not_found"
    assert "Skipping an archived Brain Home file that is not a valid record: archive/" + path in caplog.text


def test_an_archived_entity_reads_as_not_found_not_an_internal_error(tmp_path: Path, caplog) -> None:
    record_id = "brn_20260921_040000_eeee1111"
    path, content = _entity_file(record_id)
    remote = LANE_B.FakeGitHub()
    remote.seed({"archive/" + path: content})

    with pytest.raises(BrainRuntimeError) as missing:
        _read_in_a_fresh_runtime(tmp_path, remote, record_id)

    assert missing.value.code == "brain.not_found"
    assert f"Brain Home's archive holds entity record {record_id}, which the hub can't check" in caplog.text


def test_a_record_in_both_places_reads_from_its_live_copy(tmp_path: Path, caplog) -> None:
    record_id = "brn_20260926_010000_aaaa7777"
    path, content = _item_file(record_id)
    remote = LANE_B.FakeGitHub()
    remote.seed({path: content, "archive/" + path: content})

    record = _read_in_a_fresh_runtime(tmp_path, remote, record_id)

    assert (record.record_id, record.freshness) == (record_id, "current")
    assert f"Brain Home holds record {record_id} in both records/ and archive/; reading the live copy" in caplog.text


def test_both_versions_of_a_thing_archived_whole_are_not_found(tmp_path: Path) -> None:
    old_id, new_id = "brn_20260926_010000_aaaa1111", "brn_20260926_020000_bbbb2222"
    old_path, old_bytes = _item(old_id)
    new_path, new_bytes = _item(new_id, supersedes=(old_id,), title="File the form today")
    remote = LANE_B.FakeGitHub()
    remote.seed({old_path: old_bytes, new_path: new_bytes})
    _move_to_archive(remote, old_path, new_path)

    for record_id in (old_id, new_id):
        with pytest.raises(BrainRuntimeError) as missing:
            _read_in_a_fresh_runtime(tmp_path, remote, record_id)
        assert missing.value.code == "brain.not_found"


def test_diablos_signal_tool_names_the_new_placement_outcomes() -> None:
    from adapters.hermes.plugin import ALL_TOOL_DEFINITIONS

    [signal] = [tool for tool in ALL_TOOL_DEFINITIONS if tool.name == "gremlin_town_moments_signal"]

    for words in ("plan_pending", "cannot_read_brain_home", "plan_failed", "placement.error"):
        assert words in signal.description


def _fact(record_id: str) -> tuple[str, bytes]:
    from brain_core.records import record_from_mapping, render_record

    record = record_from_mapping({
        "schema": "brain.record/v2", "id": record_id, "kind": "fact", "title": "Sam's number", "state": "current",
        "created_at": "2026-09-26T01:00:00+00:00", "updated_at": "2026-09-26T01:00:00+00:00",
        "sensitivity": "private", "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:fact", "content_sha256": "f" * 64,
                      "excerpt": "Sam's number"}],
        "supersedes": [], "dedupe_key": None,
        "data": {"observed_at": "2026-09-26T01:00:00+00:00", "valid_until": None}, "body": "Sam's number",
        "source": {"said_by": "owner", "came_by": "direct", "words": "Sam's number", "recorded_by": "hermes-wa"},
    }, "fact fixture")
    return f"records/fact/2026/09/{record_id}.md", render_record(record).encode("utf-8")


def test_find_by_id_says_which_duplicate_it_found(caplog) -> None:
    """The journal names the real cause: live and archived (the live copy is read), two archived copies
    (not found), or two live copies (still a conflict, which a read maps as corrupt)."""

    from brain_core.github_storage import GitHubConflict

    from gremlin_town.mcp.github_brain_provider import find_by_id

    record_id = "brn_20260926_010000_abcd1234"
    item_path, item_bytes = _item(record_id)
    fact_path, fact_bytes = _fact(record_id)

    both = LANE_B.FakeGitHub()
    both.seed({item_path: item_bytes, "archive/" + item_path: item_bytes})
    assert find_by_id(_hub_storage(both), record_id).match.path == item_path
    assert f"Brain Home holds record {record_id} in both records/ and archive/; reading the live copy" in caplog.text

    caplog.clear()
    archived = LANE_B.FakeGitHub()
    archived.seed({"archive/" + item_path: item_bytes, "archive/" + fact_path: fact_bytes})
    assert find_by_id(_hub_storage(archived), record_id).match is None
    assert f"Brain Home's archive holds record {record_id} more than once" in caplog.text
    assert "both records/ and archive/" not in caplog.text

    caplog.clear()
    live = LANE_B.FakeGitHub()
    live.seed({item_path: item_bytes, fact_path: fact_bytes})
    with pytest.raises(GitHubConflict) as duplicate:
        find_by_id(_hub_storage(live), record_id)
    assert duplicate.value.code == "github_record_duplicate"
    assert f"Brain Home holds record {record_id} more than once in records/" in caplog.text
    assert "both records/ and archive/" not in caplog.text
    mapped = map_storage_error(duplicate.value, reading=True)
    assert (mapped.code, mapped.message) == (
        "brain.record.corrupt", "Brain Home holds two copies of this record; an operator must check them",
    )


def test_layer_two_move_keeps_old_id_stale_and_whole_item_archive_is_not_found(tmp_path):
    from dataclasses import replace
    from gremlin_town.mcp.github_brain_provider import find_by_id
    from gremlin_town.review.brain_source import brain_items_by_path_reader
    remote = LANE_B.FakeGitHub()
    settings = replace(_runtime_settings(tmp_path), archive_superseded=True)
    async def exercise():
        runtime = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: SAVED_AT)
        try:
            saved = await runtime.save_memory(CALLER, PERSONAL, _save("layer2-save"))
            first_path = _path_of(remote, saved.record_id)
            changed = await _door_change(runtime,remote,_change("layer2-change",saved.record_id,saved.content_sha256))
            reply = await runtime.read_memory(CALLER, PERSONAL, saved.record_id)
            return saved, changed, first_path, reply
        finally:
            await runtime.aclose()
    saved, changed, first_path, reply = anyio.run(exercise)
    storage = _hub_storage(remote)
    assert (reply.freshness, reply.superseded_by) == ("stale", (changed.record_id,))
    found = find_by_id(storage, saved.record_id).match
    assert found is not None and found.path == "archive/" + first_path
    pinned = brain_items_by_path_reader(storage)
    assert pinned([first_path])[0].record.id == saved.record_id
    current_path = _path_of(remote, changed.record_id)
    storage.archive_records(paths=[current_path], kinds={"item"}, expected_revision=remote.branch)
    async def missing_reads():
        runtime = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: SAVED_AT)
        try:
            for record_id in (saved.record_id, changed.record_id):
                with pytest.raises(BrainRuntimeError) as absent:
                    await runtime.read_memory(CALLER, PERSONAL, record_id)
                assert absent.value.code == "brain.not_found"
        finally:
            await runtime.aclose()
    anyio.run(missing_reads)
    assert pinned([first_path, current_path]) == ()


@pytest.mark.parametrize("kind", ["item", "fact", "preference"])
@pytest.mark.parametrize("linked", [False, True])
def test_hosted_writer_keeps_full_history_and_agent_reads_equal_with_switch_on(tmp_path, kind, linked):
    from dataclasses import replace
    from brain_core.records import record_from_mapping
    from gremlin_town.mcp.brain_runtime import MemorySearchQuery, MemoryListQuery
    from gremlin_town.notion_view.mapping import plan_rows
    from gremlin_town.review.calendar_entries import marker_id
    results = []
    for enabled in (False, True):
        root = tmp_path / str(enabled)
        root.mkdir()
        settings = _runtime_settings(root)
        policy = json.loads(settings.policy_file.read_text(encoding="utf-8"))
        policy["owners"][0]["operations"] += ["memory.search"]
        settings.policy_file.write_text(json.dumps(policy), encoding="utf-8")
        settings = replace(settings, archive_superseded=enabled)
        fake = LANE_B.FakeGitHub()
        async def exercise():
            runtime = build_brain_runtime(settings, github_transport=fake, operation_clock=lambda: SAVED_AT)
            try:
                saved = await runtime.save_memory(CALLER, PERSONAL, _memory_request(
                    "history-save", kind, source_reference="mcp-request:history-save",
                    links=({"type": "about", "target": "brn_20260801_000000_eeee"},) if linked else ()))
                if kind=='item' and linked:
                    # Capture ignores legacy arbitrary links. This explicitly synthetic old
                    # fixture retains the original link for full-history reader equivalence.
                    current=await runtime.read_memory(CALLER,PERSONAL,saved.record_id)
                    literal={**current.canonical_record,'links':[
                        {'type':'about','target':'brn_20260801_000000_eeee'}]}
                    from brain_core.records import render_record
                    fake.seed({_path_of(fake,saved.record_id):render_record(record_from_mapping(literal)).encode()})
                    saved=SimpleNamespace(record_id=saved.record_id,content_sha256=
                        (await runtime.read_memory(CALLER,PERSONAL,saved.record_id)).content_sha256)
                ids = []
                for step in range(4):
                    ids.append(saved.record_id)
                    request = _memory_change_request(
                        f"history-{step}", saved.record_id, saved.content_sha256,
                        state={"item": "open", "fact": "current", "preference": "active"}[kind],
                        said_by="owner", came_by="direct", speaker=None, words=f"I changed this {kind}",
                        source_reference=f"mcp-request:history-{step}",
                    )
                    if kind=='item':
                        saved = await _door_change(runtime,fake,request,
                            move={'move':'next_action','text':request.words,'words':request.words})
                    else:
                        saved = await runtime.change_memory(CALLER,PERSONAL,request)
                    current = await runtime.read_memory(CALLER, PERSONAL, saved.record_id)
                    envelope = record_from_mapping(current.canonical_record)
                    assert envelope.supersedes == tuple(ids)
                listing = await runtime.list_memory(CALLER, PERSONAL, MemoryListQuery(kind="preference" if kind == "preference" else "item"))
                search = await runtime.search_memory(CALLER, PERSONAL, MemorySearchQuery("record"))
                old = await runtime.read_memory(CALLER, PERSONAL, ids[0])
                # Original archive-off behavior lists every historical successor,
                # including intermediates, in the live snapshot's canonical order.
                assert old.superseded_by == tuple(sorted((*ids[1:], saved.record_id)))
                assert bool(old.canonical_record.get("links")) is linked
                # The tree layout changes the branch revision. Compare every answer
                # field while treating that transport revision as metadata.
                search = replace(search, version="head", matches=tuple(replace(m, version="head") for m in search.matches))
                return replace(listing, version="head"), search, replace(old, version="head")
            finally:
                await runtime.aclose()
        read_answers = anyio.run(exercise)
        records = tuple(file.record for file in _hub_storage(fake).snapshot_records(kinds=frozenset({kind})).records)
        results.append((read_answers, plan_rows(records, now=SAVED_AT, done_days=7),
                        tuple(marker_id(r) for r in records if r.id not in {old for v in records for old in v.supersedes})))
    assert results[0] == results[1]


def test_note_reader_and_notion_rows_equal_over_generated_histories_with_archive_switch():
    from brain_core.records import parse_record, record_from_mapping, render_record
    from gremlin_town.review.brain_source import current_open_notes
    from gremlin_town.notion_view.mapping import plan_rows
    from gremlin_town.review.calendar_entries import marker_id
    from datetime import timedelta
    import random
    outputs = []
    for enabled in (False, True):
        fake = LANE_B.FakeGitHub()
        storage = GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN,
                                        transport=fake, archive_superseded=enabled)
        rng = random.Random(37)
        for group in range(6):
            ids = []
            for step in range(rng.randint(3, 6)):
                at = SAVED_AT + timedelta(minutes=group * 10 + step)
                # IDs encode UTC; use the same timestamp in the envelope.
                from datetime import UTC
                at = at.astimezone(UTC)
                rid = f"brn_{at:%Y%m%d_%H%M%S}_{group:04x}{step:04x}"
                _, raw = _item(rid, supersedes=tuple(ids))
                item = parse_record(raw.decode())
                storage.publish_record(record_bytes=raw)
                ids.append(item.id)
        for step in range(4):
            raw = LANE_B.standalone_record("preference", f"a{step:03x}")
            value = parse_record(raw.decode()).as_dict()
            value.update(kind="continuity", state="open", supersedes=[f"brn_20260921_040000_a{i:03x}" for i in range(step)],
                         data={"session_id": "note", "intent": "Continue", "sources": [], "effects": [], "decisions": [],
                               "commitments": [], "candidates": [], "blockers": [], "next_step": f"Step {step}"})
            storage.publish_record(record_bytes=render_record(record_from_mapping(value)).encode())
        read = storage.snapshot_records(kinds=frozenset({"continuity"}))
        items = tuple(f.record for f in storage.snapshot_records(kinds=frozenset({"item"})).records)
        rows = plan_rows(items, now=SAVED_AT, done_days=7)
        outputs.append((current_open_notes(read), rows, tuple(marker_id(row.record) for row in rows)))
    assert outputs[0] == outputs[1]


def test_hosted_note_writer_keeps_whole_history_and_read_answers_equal_with_archiving():
    from gremlin_town.mcp.brain_runtime import ContinuityStartRequest, ContinuityAppendRequest, ContinuityRecoverRequest
    from gremlin_town.review.brain_source import current_open_notes
    outputs = []
    for enabled in (False, True):
        fake = LANE_B.FakeGitHub()
        storage = _hub_storage(fake)
        storage.archive_superseded = enabled
        provider = GitHubContinuityProvider(storage, lambda _: FIRST_TIME)
        async def exercise():
            result = await provider.start(_authorization(BrainOperation.CONTINUITY_START, "write"),
                                          ContinuityStartRequest("note-start", "session", "Ship", ()))
            ids = []
            for step in range(4):
                ids.append(result.record_id)
                result = await provider.append(_authorization(BrainOperation.CONTINUITY_APPEND, "write"),
                                               ContinuityAppendRequest(f"note-{step}", "session", "effect", f"effect {step}"))
                match = storage.find_record_by_id(result.record_id)
                assert match.record.supersedes == tuple(ids)
            listing = await provider.list_open(_authorization(BrainOperation.CONTINUITY_LIST, "read"), ContinuityListRequest())
            recovery = await provider.recover(_authorization(BrainOperation.CONTINUITY_RECOVER, "read"),
                                              ContinuityRecoverRequest(session_id="session"))
            return listing, recovery
        outputs.append((anyio.run(exercise), current_open_notes(storage.snapshot_records(kinds=frozenset({"continuity"})))))
    assert outputs[0] == outputs[1]
