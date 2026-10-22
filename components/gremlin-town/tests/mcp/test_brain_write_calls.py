"""GitHub calls per Brain operation through the provider, with the hub's copy on.

Each operation works at one revision: the memory preflight's. A save is then the
branch read plus four writes once the copy is warm (seven when it is cold); a change
reads its record and scans for successors in that same revision, from the copy. The
race, refusal and replay rules are unchanged; the last tests race them with the copy on.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import anyio
import pytest

from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache
from brain_core.records import parse_record, render_record
from gremlin_town.mcp.brain_runtime import BrainOperation as Op, BrainRuntimeError, MemorySearchQuery
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider
from test_github_brain_provider import (
    FIRST_TIME,
    LANE_B,
    LATER_TIME,
    _adapter,
    _authorization,
    _memory_change_request,
    _memory_request,
    _HistoryTransport,
)

COLD = ["GET_REF", "GET_COMMIT", "GET_TREE"]
WRITES = ["POST_BLOB", "POST_TREE", "POST_COMMIT", "PATCH_OK"]
SAVE = _authorization(Op.MEMORY_SAVE, "write")
CHANGE = _authorization(Op.MEMORY_CHANGE, "write")


class _BlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def load(self, blob_sha: str) -> bytes | None:
        return self.blobs.get(blob_sha)

    def store(self, blob_sha: str, content: bytes) -> None:
        self.blobs[blob_sha] = content


def _storage(fake, blob_store: _BlobStore | None = None) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY,
        branch=LANE_B.BRANCH,
        token=LANE_B.TOKEN,
        transport=_HistoryTransport(fake),
        object_cache=GitHubObjectCache(blob_store=blob_store),
    )


def _kinds(events: list[str]) -> list[str]:
    return [event.split(" ", 1)[0] for event in events]


def _clock(_: str) -> datetime:
    return FIRST_TIME


def _items(fake, kind="item") -> list[str]:
    return [path for path in fake.trees[fake.commits[fake.branch]["tree"]] if path.startswith(f"records/{kind}/")]


def test_a_save_makes_seven_github_calls_with_a_cold_copy_and_five_warm() -> None:
    fake = LANE_B.FakeGitHub()
    fake.seed(
        {
            f"records/commitment/2026/09/brn_20260921_040000_{index:08x}.md": LANE_B.standalone_record(
                "commitment", f"{index:08x}"
            )
            for index in range(40)
        }
    )
    provider = GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock))

    fake.events.clear()
    first = anyio.run(provider.save, SAVE, _memory_request("calls-save-cold", "fact"))
    assert _kinds(fake.events) == [*COLD, *WRITES]

    fake.events.clear()
    second = anyio.run(provider.save, SAVE, _memory_request("calls-save-warm", "fact"))
    assert _kinds(fake.events) == ["GET_REF", *WRITES]

    for receipt in (first, second):
        assert receipt.status == "accepted" and receipt.remote_observed is True
    assert second.version == fake.branch


def test_a_near_duplicate_save_still_costs_five_calls_warm_and_names_the_earlier_record() -> None:
    """ROA-11 (Wave 3): the near-duplicate scan runs at the save's own pinned
    preflight revision and reuses the object cache the first save already
    warmed (tree and the earlier record's own blob), so finding a match costs
    nothing beyond an ordinary warm save."""

    fake = LANE_B.FakeGitHub()
    provider = GitHubBrainProvider(
        _storage(fake),
        _adapter({"calls-dup-first": FIRST_TIME, "calls-dup-second": FIRST_TIME + timedelta(hours=3)}),
    )

    fake.events.clear()
    first = anyio.run(provider.save, SAVE, _memory_request("calls-dup-first", "item", title='Buy printer ink', words="Buy printer ink"))
    assert _kinds(fake.events) == [*COLD, *WRITES]

    fake.events.clear()
    second = anyio.run(
        provider.save,
        SAVE,
        _memory_request("calls-dup-second", "item", title="Buy printer ink reminder", words="  buy   printer ink!!  "),
    )
    assert _kinds(fake.events) == ["GET_REF", *WRITES]

    assert first.duplicate_of is None
    assert second.duplicate_of is None
    record=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),second.record_id)
    assert any(mark=={'mark':'maybe_same','as':first.record_id}
        for mark in record.canonical_record['data']['review_marks'])
    assert second.status == "accepted" and second.record_id != first.record_id


def test_a_change_makes_eight_github_calls_with_a_cold_copy_and_five_warm() -> None:
    fake = LANE_B.FakeGitHub()
    saved = anyio.run(
        GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock)).save,
        SAVE,
        _memory_request("calls-change-target", "fact"),
    )
    # A fresh process with an empty copy: no record path known, nothing cached.
    provider = GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock))

    fake.events.clear()
    waiting = anyio.run(
        provider.change, CHANGE, _memory_change_request("calls-change-cold", saved.record_id, saved.content_sha256, state="current")
    )
    assert _kinds(fake.events) == [*COLD, "GET_BLOB", *WRITES]

    fake.events.clear()
    blocked = anyio.run(
        provider.change,
        CHANGE,
        _memory_change_request("calls-change-warm", waiting.record_id, waiting.content_sha256, state="current"),
    )
    assert _kinds(fake.events) == ["GET_REF", *WRITES]

    assert waiting.status == blocked.status == "accepted"
    assert blocked.version == fake.branch
    assert len(_items(fake,"fact")) == 3


def test_after_a_restart_with_the_copy_on_disk_a_change_among_many_items_makes_seven_calls() -> None:
    fake = LANE_B.FakeGitHub()
    store = _BlobStore()
    before = GitHubBrainProvider(_storage(fake, store), _adapter(operation_time=_clock))
    receipts = [
        anyio.run(before.save, SAVE, _memory_request(f"calls-many-{index}", "fact", title=f"Item {index}"))
        for index in range(20)
    ]
    target = receipts[7]
    after = GitHubBrainProvider(_storage(fake, store), _adapter(operation_time=_clock))

    fake.events.clear()
    changed = anyio.run(
        after.change, CHANGE, _memory_change_request("calls-many-change", target.record_id, target.content_sha256, state="current")
    )

    assert changed.status == "accepted"
    assert _kinds(fake.events) == [*COLD, *WRITES]


def test_search_and_read_make_one_github_call_with_a_warm_copy() -> None:
    fake = LANE_B.FakeGitHub()
    provider = GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock))
    saved = anyio.run(provider.save, SAVE, _memory_request("calls-read", "item"))

    fake.events.clear()
    found = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, "read"), MemorySearchQuery("item record"))
    assert _kinds(fake.events) == ["GET_REF"]
    assert [match.record_id for match in found.matches] == [saved.record_id]

    fake.events.clear()
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, "read"), saved.record_id)
    assert _kinds(fake.events) == ["GET_REF"]
    assert read.version == saved.version and read.freshness == "current"


def _archive(fake, record_id: str) -> str:
    """Move one record to the clear-out's archive path in one commit, byte for byte."""

    tree = dict(fake.trees[fake.commits[fake.branch]["tree"]])
    live = next(path for path in tree if path.endswith(f"/{record_id}.md"))
    archived = f"archive/{live}"
    tree[archived] = tree.pop(live)
    tree_sha = fake._sha()
    fake.trees[tree_sha] = tree
    commit_sha = fake._sha()
    fake.commits[commit_sha] = {"tree": tree_sha, "parent": fake.branch, "message": "archive", "paths": {live, archived}}
    fake.branch = commit_sha
    return archived


def test_a_replayed_save_of_an_archived_record_is_a_duplicate_found_without_the_commit_list() -> None:
    """Step C §7: archived records must not be re-created by a retried save."""

    fake = LANE_B.FakeGitHub()
    request = _memory_request("calls-archived-replay", "fact")
    saved = anyio.run(GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock)).save, SAVE, request)
    archived = _archive(fake, saved.record_id)
    head = fake.branch

    def must_not_rematerialize(_: str) -> datetime:
        raise AssertionError("a replay of an archived record must not build a new one")

    fresh = GitHubBrainProvider(_storage(fake), _adapter(operation_time=must_not_rematerialize))
    fake.events.clear()
    replay = anyio.run(fresh.save, SAVE, request)

    assert replay.status == "duplicate" and replay.remote_observed is True
    assert replay.record_id == saved.record_id
    assert replay.content_sha256 == saved.content_sha256
    assert replay.version == head == fake.branch
    assert _kinds(fake.events) == [*COLD, "GET_BLOB"]
    assert _items(fake) == []
    assert _items(fake,'fact')==[]
    assert archived in fake.trees[fake.commits[fake.branch]["tree"]]
    # The archive is outside the live set: its path is not kept for later reads.
    with pytest.raises(BrainRuntimeError) as missing:
        anyio.run(fresh.read, _authorization(Op.MEMORY_READ, "read"), saved.record_id)
    assert missing.value.code == "brain.not_found"


def test_archived_plain_item_bridge_retry_never_recreates_its_record() -> None:
    """The bridge must retain the same whole-archive retry guarantee as the publisher."""
    fake=LANE_B.FakeGitHub()
    request=_memory_request('calls-archived-bridge','item')
    saved=anyio.run(GitHubBrainProvider(_storage(fake),_adapter(operation_time=_clock)).save,SAVE,request)
    archived=_archive(fake,saved.record_id)
    head,writes=fake.branch,len(fake.patch_payloads)
    def no_materialization(_):
        raise AssertionError('an exact archived bridge retry must reconcile before the clock')
    fresh=GitHubBrainProvider(_storage(fake),_adapter(operation_time=no_materialization))
    replay=anyio.run(fresh.save,SAVE,request)
    assert replay.status=='duplicate' and replay.remote_observed is True
    assert replay.record_id==saved.record_id and replay.content_sha256==saved.content_sha256
    assert fake.branch==head and len(fake.patch_payloads)==writes
    assert _items(fake)==[] and archived in fake.trees[fake.commits[fake.branch]['tree']]


class _RacedStorage(GitHubBrainHomeStorage):
    """Runs another host's whole operation just before this host's first publication.

    The other host has its own provider, storage and copy; it runs in this worker
    thread through the provider's synchronous entry, as a second hub would.
    """

    def __init__(self, fake, other) -> None:
        super().__init__(
            repository=LANE_B.REPOSITORY,
            branch=LANE_B.BRANCH,
            token=LANE_B.TOKEN,
            transport=_HistoryTransport(fake),
            object_cache=GitHubObjectCache(),
        )
        self._other = other
        self.base_revisions: list[str] = []

    def publish_record(self, **kwargs):
        self.base_revisions.append(kwargs["base_revision"].commit_sha)
        if self._other is not None:
            other, self._other = self._other, None
            other()
        return super().publish_record(**kwargs)

    def publish_records(self, **kwargs):
        self.base_revisions.append(kwargs['base_revision'].commit_sha)
        if self._other is not None:
            other,self._other=self._other,None
            other()
        return super().publish_records(**kwargs)


def test_two_hosts_saving_one_operation_with_the_copy_on_keep_one_record() -> None:
    fake = LANE_B.FakeGitHub()
    initial = fake.branch
    request = _memory_request("calls-two-hosts", "item")
    first = GitHubBrainProvider(_storage(fake), _adapter({"calls-two-hosts": FIRST_TIME}))
    results = []
    storage = _RacedStorage(fake, lambda: results.append(anyio.run(first.save,SAVE,request)))
    second = GitHubBrainProvider(storage, _adapter({"calls-two-hosts": LATER_TIME}))

    late = anyio.run(second.save, SAVE, request)

    assert results[0].status == "accepted"
    assert late.status == "duplicate" and late.record_id == results[0].record_id
    assert storage.base_revisions == [initial]
    assert _kinds(fake.events).count("PATCH_NOT_FAST_FORWARD") == 1
    assert _kinds(fake.events).count("PATCH_OK") == 1
    [path] = _items(fake)
    head = fake.trees[fake.commits[fake.branch]["tree"]]
    assert parse_record(fake.blobs[head[path]].decode("utf-8"), path).created_at == FIRST_TIME


def test_bridge_reuses_preflight_head_but_recomputes_after_a_foreign_capture() -> None:
    from gremlin_town.mcp.host_identity import bind_write_attribution

    fake=LANE_B.FakeGitHub()
    initial=fake.branch
    other=GitHubBrainProvider(_storage(fake),_adapter(operation_time=_clock))
    winners=[]
    def capture_from_other_caller():
        with bind_write_attribution('local-codex'):
            winners.append(anyio.run(other.save,SAVE,_memory_request('calls-foreign-head','item',
                title='Buy printer ink',words='Buy printer ink')))
    storage=_RacedStorage(fake,capture_from_other_caller)
    provider=GitHubBrainProvider(storage,_adapter(operation_time=_clock))
    fake.events.clear()
    with bind_write_attribution('hermes-wa'):
        saved=anyio.run(provider.save,SAVE,_memory_request('calls-seeded-race','item',
            title='Buy printer ink',words='Buy printer ink'))
    assert saved.status=='accepted' and saved.version==fake.branch
    assert winners[0].status=='accepted' and winners[0].record_id!=saved.record_id
    assert storage.base_revisions==[initial,winners[0].version]
    assert _kinds(fake.events).count('PATCH_NOT_FAST_FORWARD')==1
    assert _kinds(fake.events).count('PATCH_OK')==2
    # Each caller preflights once. The publisher checks the conflicting head,
    # then the door discards its seed and reads that head to recompute.
    assert _kinds(fake.events).count('GET_REF')==4
    assert _kinds(fake.events)[:3]==COLD
    record=storage.preflight_record_by_id(saved.record_id).match.record
    assert {'mark':'maybe_same','as':winners[0].record_id} in record.data['review_marks']
    assert set(_items(fake))=={
        f'records/item/2026/09/{saved.record_id}.md',
        f'records/item/2026/09/{winners[0].record_id}.md'}


def test_a_change_raced_by_another_change_of_the_same_record_is_refused_as_changed() -> None:
    fake = LANE_B.FakeGitHub()
    saved = anyio.run(
        GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock)).save,
        SAVE,
        _memory_request("calls-raced-change-target", "fact"),
    )
    other = GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock))
    winner = []

    def other_host_marks_it_waiting() -> None:
        winner.append(
            other._change_serialized(
                CHANGE, _memory_change_request("calls-raced-change-one", saved.record_id, saved.content_sha256, state="current")
            )
        )

    provider = GitHubBrainProvider(_RacedStorage(fake, other_host_marks_it_waiting), _adapter(operation_time=_clock))
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(
            provider.change,
            CHANGE,
            _memory_change_request("calls-raced-change-two", saved.record_id, saved.content_sha256, state="current"),
        )

    assert refused.value.code == "brain.record.changed"
    assert winner[0].status == "accepted"
    assert _kinds(fake.events).count("PATCH_NOT_FAST_FORWARD") == 1
    assert {path.rsplit("/", 1)[1] for path in _items(fake,"fact")} == {
        f"{saved.record_id}.md",
        f"{winner[0].record_id}.md",
    }


def test_fact_change_reconciles_an_identical_winner_after_discarding_its_initial_pin(monkeypatch) -> None:
    fake = LANE_B.FakeGitHub()
    first = GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock))
    saved = anyio.run(first.save, SAVE, _memory_request('calls-fact-replay-target', 'fact'))
    request = _memory_change_request('calls-fact-replay-race', saved.record_id,
        saved.content_sha256, state='current')
    winner = []
    storage = _RacedStorage(fake, lambda: winner.append(first._change_serialized(CHANGE, request)))
    second = GitHubBrainProvider(storage, _adapter(operation_time=_clock))
    admitted_revisions = []
    original_read = second._current_memory_record
    def admitted_read(authorization, record_id, revision):
        admitted_revisions.append(revision.commit_sha)
        return original_read(authorization, record_id, revision)
    monkeypatch.setattr(second, '_current_memory_record', admitted_read)

    fake.events.clear()
    replay = anyio.run(second.change, CHANGE, request)

    assert winner[0].status == 'accepted' and replay.status == 'duplicate'
    assert replay.record_id == winner[0].record_id
    assert replay.content_sha256 == winner[0].content_sha256
    assert replay.version == winner[0].version == fake.branch
    assert storage.base_revisions == [saved.version]
    assert _kinds(fake.events).count('GET_REF') == 4
    assert _kinds(fake.events).count('PATCH_NOT_FAST_FORWARD') == 1
    assert _kinds(fake.events).count('PATCH_OK') == 1
    assert len(_items(fake, 'fact')) == 2
    # Initial denial check, first preflight admission, then the winning revision's
    # admission before its duplicate receipt. No old pin survives the conflict.
    assert admitted_revisions == [saved.version, saved.version, winner[0].version]


@pytest.mark.parametrize('caller', ['hermes-wa', 'chatgpt-web', 'claude-web',
    'fleet-boss-grok', 'local-codex', 'system-unknown'])
def test_direct_item_change_denies_every_caller_before_receipt_calendar_or_publish(caller, monkeypatch):
    from gremlin_town.mcp.host_identity import bind_write_attribution

    fake = LANE_B.FakeGitHub()
    provider = GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock))
    saved = anyio.run(provider.save, SAVE, _memory_request('calls-denied-item-target', 'item'))
    head, writes = fake.branch, len(fake.patch_payloads)
    def forbidden_effect(*args, **kwargs):
        raise AssertionError('a denied item change cannot publish or reconcile a calendar receipt')
    monkeypatch.setattr(provider, '_confirmation', forbidden_effect)
    monkeypatch.setattr(provider._storage, 'publish_record', forbidden_effect)
    with bind_write_attribution(caller), pytest.raises(BrainRuntimeError) as denied:
        anyio.run(provider.change, CHANGE, _memory_change_request('calls-denied-item-change',
            saved.record_id, saved.content_sha256, state='current'))
    assert denied.value.code == 'ACCESS_DENIED'
    assert denied.value.field == 'record_id'
    assert denied.value.fix == ('use bc_work' if caller == 'hermes-wa' else
        "save it as a new thing with memory.save; Nic's morning review merges it")
    assert fake.branch == head and len(fake.patch_payloads) == writes


def test_fact_change_retry_rechecks_item_admission_on_the_conflicting_head():
    fake = LANE_B.FakeGitHub()
    setup = GitHubBrainProvider(_storage(fake), _adapter(operation_time=_clock))
    saved = anyio.run(setup.save, SAVE, _memory_request('calls-kind-race-fact', 'fact'))
    item = anyio.run(setup.save, SAVE, _memory_request('calls-kind-race-item', 'item'))
    item_path = next(path for path in _items(fake) if path.endswith(f'/{item.record_id}.md'))
    tree = fake.trees[fake.commits[fake.branch]['tree']]
    accepted_item = parse_record(fake.blobs[tree[item_path]].decode(), item_path)
    # A hostile fixture publisher replaces the named fact's identity with an
    # otherwise valid accepted item. Never invent provenance or relax its parser.
    hostile = replace(accepted_item, id=saved.record_id)
    payload = render_record(hostile).encode()
    assert parse_record(payload.decode(), 'hostile fixture') == hostile
    competing_heads = []
    def hostile_publication():
        head = dict(fake.trees[fake.commits[fake.branch]['tree']])
        old_path = next(path for path in head if path.endswith(f'/{saved.record_id}.md'))
        new_path = f'records/item/{hostile.created_at:%Y/%m}/{hostile.id}.md'
        head.pop(old_path)
        blob_sha = fake._blob(payload)
        head[new_path] = blob_sha
        tree_sha = fake._sha()
        fake.trees[tree_sha] = head
        commit_sha = fake._sha()
        fake.commits[commit_sha] = {'tree': tree_sha, 'parent': fake.branch,
            'message': 'hostile fixture replacement', 'paths': {old_path, new_path}}
        fake.branch = commit_sha
        competing_heads.append(commit_sha)
    storage = _RacedStorage(fake, hostile_publication)
    provider = GitHubBrainProvider(storage, _adapter(operation_time=_clock))
    initial = fake.branch
    fake.events.clear()
    with pytest.raises(BrainRuntimeError) as denied:
        anyio.run(provider.change, CHANGE, _memory_change_request('calls-kind-race-change',
            saved.record_id, saved.content_sha256, state='current'))
    assert denied.value.code == 'ACCESS_DENIED' and denied.value.field == 'record_id'
    assert denied.value.fix == "save it as a new thing with memory.save; Nic's morning review merges it"
    assert storage.base_revisions == [initial]
    assert fake.branch == competing_heads[0]
    assert _kinds(fake.events).count('PATCH_NOT_FAST_FORWARD') == 1
    assert _kinds(fake.events).count('PATCH_OK') == 0


def test_an_archived_record_from_before_caller_scoping_is_a_duplicate_only_for_its_author() -> None:
    """A save from before P3 carries the legacy suffix. Archived, its author's replay
    still returns it as a duplicate; another caller's same operation id is a new save."""

    from dataclasses import replace

    from brain_core.github_storage import memory_operation_dedupe_key, memory_operation_record_suffix
    from gremlin_town.mcp.host_identity import bind_write_attribution

    operation_id = "calls-archived-legacy"
    fake = LANE_B.FakeGitHub()
    storage = _storage(fake)
    adapter = _adapter(operation_time=_clock)
    request = _memory_request(operation_id, "item", words="Renew the passport")
    with bind_write_attribution("nic"):
        identity = adapter.memory_identity(SAVE, request)
        legacy = replace(
            identity,
            record_entropy=memory_operation_record_suffix(operation_id),
            dedupe_key=memory_operation_dedupe_key(operation_id, identity.input_sha256),
        )
        material = adapter.materialize_memory(SAVE, request, identity=legacy)
    published = storage.publish_record(record_bytes=material.record_bytes)
    archived = _archive(fake, published.record_id)
    head = fake.branch
    provider = GitHubBrainProvider(storage, adapter)

    with bind_write_attribution("nic"):
        replay = anyio.run(provider.save, SAVE, request)
    assert replay.status == "duplicate" and replay.remote_observed is True
    assert replay.record_id == published.record_id
    assert replay.version == head == fake.branch

    with bind_write_attribution("damien"):
        other = anyio.run(provider.save, SAVE, request)
    assert other.status == "accepted"
    assert other.record_id != published.record_id
    [path] = _items(fake)
    assert path.endswith(f"/{other.record_id}.md")
    assert archived in fake.trees[fake.commits[fake.branch]["tree"]]


def test_retired_item_change_refuses_and_permitted_door_keeps_body_and_history():
    import hashlib
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.mcp.host_identity import bind_write_attribution
    fake=LANE_B.FakeGitHub()
    provider=GitHubBrainProvider(_storage(fake),_adapter(operation_time=_clock))
    saved=anyio.run(provider.save,SAVE,_memory_request('calls-item-door-target','item'))
    original=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),saved.record_id)
    head,writes=fake.branch,len(fake.patch_payloads)
    with bind_write_attribution('hermes-wa'),pytest.raises(BrainRuntimeError) as denied:
        anyio.run(provider.change,CHANGE,
            _memory_change_request('calls-retired-item',saved.record_id,saved.content_sha256))
    assert denied.value.code=='ACCESS_DENIED' and denied.value.fix=='use bc_work'
    assert fake.branch==head and len(fake.patch_payloads)==writes
    with bind_write_attribution('hermes-wa'):
        changed=anyio.run(provider.work,_authorization(Op.WORK,'write'),WorkRequest(
            'calls-permitted-item','Marked waiting for the fixture','owner','direct',
            ({'move':'wait','thing':saved.record_id,'kind':'needs','who':'the fixture'},)))
    current=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),changed['things'][0]['record_id'])
    assert changed['status']=='accepted' and current.record_id==saved.record_id
    physical_id=changed['things'][0]['record_id']
    assert physical_id!=saved.record_id
    physical=provider._storage.preflight_record_by_id(physical_id).match
    assert current.canonical_record['id']==physical_id==physical.record.id
    assert current.content_sha256==hashlib.sha256(physical.content).hexdigest()
    assert physical.record.body==original.body
    assert current.body==original.body
    assert current.canonical_record['supersedes']==[saved.record_id]
    assert current.canonical_record['data']['needs']=={'from':'the fixture'}
    assert current.canonical_record['source']['recorded_by']=='hermes-wa'
