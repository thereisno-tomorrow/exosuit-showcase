"""Exact work replay reads a verified batch parent; all storage is fake."""
import json
import hashlib
from dataclasses import replace

import pytest

from brain_core.github_storage import GitHubConflict, GitHubStorageError, HTTPResponse
from brain_core.github_storage import memory_operation_record_suffix, memory_operation_dedupe_key
from brain_core.records import parse_record, render_record
from test_github_storage import FakeGitHub, storage, v2_standalone_record


class HistoryGitHub(FakeGitHub):
    def request(self, method, url, *, headers, body):
        if method == "GET" and "/git/commits/" in url and url.rsplit("/", 1)[-1] not in self.commits:
            return self._response(404, {"message": "missing fixture commit"})
        response = super().request(method, url, headers=headers, body=body)
        if method == "GET" and "/git/commits/" in url:
            sha = url.rsplit("/", 1)[-1]
            commit = self.commits[sha]
            value = json.loads(response.body)
            value["sha"] = commit.get("response_sha", sha)
            value["message"] = commit["message"]
            value["parents"] = commit.get("parents", [] if commit["parent"] is None else [{"sha": commit["parent"]}])
            return HTTPResponse(response.status, response.headers, json.dumps(value).encode())
        return response


def batch():
    fake = HistoryGitHub()
    adapter = storage(fake)
    before = adapter.revision()
    payloads = [v2_standalone_record("item", suffix) for suffix in ("b001", "b002")]
    result = adapter.publish_records(records=payloads, base_revision=before)
    records = tuple(parse_record(payload.decode(), "batch fixture") for payload in payloads)
    return fake, adapter, before, result, records


def test_memory_preflight_reuses_only_a_verified_pinned_revision():
    fake, adapter, _before, _result, records = batch()
    pinned = adapter.revision()
    operation, input_hash, caller = 'pinned-memory-operation', 'b' * 64, 'hermes-wa'
    record = replace(records[0],
        id=f'brn_20260921_040000_{memory_operation_record_suffix(operation, caller=caller)}',
        dedupe_key=memory_operation_dedupe_key(operation, input_hash),
        source={**records[0].source, 'recorded_by': caller})
    adapter.publish_record(record_bytes=render_record(record).encode(), base_revision=pinned)

    fake.events.clear()
    old = adapter.preflight_memory_operation(operation_id=operation, input_sha256=input_hash,
        caller=caller, revision=pinned)
    assert old.revision == pinned and old.match is None
    assert not any(event.startswith('GET_REF') for event in fake.events)
    fresh = adapter.preflight_memory_operation(operation_id=operation, input_sha256=input_hash, caller=caller)
    assert fresh.revision.commit_sha == fake.branch != pinned.commit_sha
    assert fresh.match.record == record
    assert sum(event.startswith('GET_REF') for event in fake.events) == 1

    fake.events.clear()
    for invalid in (replace(pinned, repository='another/repository'), replace(pinned, tree_sha='f' * 40)):
        with pytest.raises(ValueError):
            adapter.preflight_memory_operation(operation_id=operation, input_sha256=input_hash,
                caller=caller, revision=invalid)
    assert not any(event.startswith('GET_REF') for event in fake.events)


def work_lookup_fixture():
    fake,adapter,before,_result,records=batch()
    operation=hashlib.sha256(b'exact-work').hexdigest()
    suffixes=tuple(memory_operation_record_suffix(f'work-{operation}-0-{slot}',caller='hermes-wa') for slot in (0,1))
    exact=tuple(replace(record,id=f'brn_20260921_040000_{suffix}',dedupe_key='a'*64,
        source={**record.source,'recorded_by':'hermes-wa'})
        for record,suffix in zip(records,suffixes))
    result=adapter.publish_records(records=[render_record(record).encode() for record in exact],base_revision=adapter.revision())
    return fake,adapter,suffixes,exact,result


def test_planned_work_lookup_finds_whole_archive_at_only_the_pinned_head():
    fake,adapter,suffixes,records,accepted=work_lookup_fixture()
    adapter.archive_records(paths=[entry.record_path for entry in accepted.records],kinds=frozenset({'item'}))
    pinned=adapter.revision()
    fake.seed({'unrelated.md':b'after pinned lookup'})
    writes=len(fake.patch_payloads)
    found=adapter.preflight_work_records(record_suffixes=suffixes,caller='hermes-wa',revision=pinned)
    assert {match.record.id for match in found}=={record.id for record in records}
    assert all(match.path.startswith('archive/') and match.revision==pinned for match in found)
    history=adapter.revision_before_batch(records=[match.record for match in found],revision=pinned)
    assert history.accepted.commit_sha==accepted.commit_sha
    assert len(fake.patch_payloads)==writes
    other=tuple(memory_operation_record_suffix('work-'+hashlib.sha256(b'exact-work').hexdigest()+f'-0-{slot}',
        caller='local-codex') for slot in (0,1))
    assert adapter.preflight_work_records(record_suffixes=other,caller='local-codex',revision=pinned)==()
    assert adapter.preflight_record_by_id(records[0].id,revision=pinned).match is None


@pytest.mark.parametrize('fault,code',[('archive-corrupt','github_record_corrupt'),('path','github_record_corrupt'),
    ('caller','github_work_caller_conflict'),('suffix-collision','github_work_identity_conflict'),
    ('live-archive-duplicate','github_work_identity_conflict'),('capacity','github_work_candidate_limit')])
def test_planned_work_lookup_refuses_ambiguous_or_invalid_exact_candidates(fault,code):
    fake,adapter,suffixes,records,accepted=work_lookup_fixture()
    path=accepted.records[0].record_path
    tree=fake.trees[fake.commits[fake.branch]['tree']]
    if fault=='archive-corrupt':
        del tree[path]
        tree['archive/'+path]=fake._blob(b'not a Brain record')
    elif fault=='path':
        tree[path]=fake._blob(render_record(replace(records[0],id='brn_20260921_040000_1234567890abcdef')).encode())
    elif fault=='caller':
        tree[path]=fake._blob(render_record(replace(records[0],source={**records[0].source,'recorded_by':'local-codex'})).encode())
    elif fault=='suffix-collision':
        collision=replace(records[0],id=records[0].id.replace('040000','040001'))
        tree['records/item/2026/09/'+collision.id+'.md']=fake._blob(render_record(collision).encode())
    elif fault=='live-archive-duplicate':
        tree['archive/'+path]=tree[path]
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(GitHubStorageError) as error:
        adapter.preflight_work_records(record_suffixes=suffixes,caller='hermes-wa',revision=adapter.revision(),
            max_candidates=1 if fault=='capacity' else 25)
    assert error.value.code==code
    assert fake.branch==head and len(fake.patch_payloads)==writes


def test_planned_work_lookup_has_a_parse_byte_bound_and_rejects_invalid_suffixes(monkeypatch):
    import brain_core.github_storage as module
    fake,adapter,suffixes,_records,_accepted=work_lookup_fixture()
    revision=adapter.revision()
    for invalid in ((),('a'*15,),('a'*16,'a'*16),('not-work-suffix',)):
        with pytest.raises(GitHubStorageError) as error:
            adapter.preflight_work_records(record_suffixes=invalid,caller='hermes-wa',revision=revision)
        assert error.value.code=='github_work_lookup_invalid'
    monkeypatch.setattr(module,'SNAPSHOT_MAX_BYTES',1)
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(GitHubStorageError) as error:
        adapter.preflight_work_records(record_suffixes=suffixes,caller='hermes-wa',revision=revision)
    assert error.value.code=='github_work_byte_limit'
    assert fake.branch==head and len(fake.patch_payloads)==writes


def test_exact_batch_parent_survives_same_clock_later_commit_without_writes():
    fake, adapter, before, accepted, records = batch()
    fake.seed({"unrelated.md": b"same timestamp, different commit"})
    pinned = adapter.revision()
    writes = len(fake.patch_payloads)
    found = adapter.revision_before_batch(records=records, revision=pinned)
    assert found.before == before
    assert found.accepted.commit_sha == accepted.commit_sha
    assert found.accepted.commit_sha != pinned.commit_sha
    assert len(fake.patch_payloads) == writes and fake.branch == pinned.commit_sha
    snapshot = adapter.snapshot_records(kinds=frozenset({"item"}), revision=found.before)
    assert not snapshot.records


@pytest.mark.parametrize("fault", ["partial", "hash", "extra", "duplicate-trailer", "malformed-trailer", "two-parents", "bytes", "preexisting"])
def test_partial_conflicting_or_unverifiable_batch_refuses_without_write(fault):
    fake, adapter, before, accepted, records = batch()
    requested = records
    commit = fake.commits[accepted.commit_sha]
    if fault == "partial":
        requested = records[:1]
    elif fault == "hash":
        requested = (replace(records[0], body="different body"), records[1])
    elif fault == "extra":
        commit["message"] += "Brain-Core-Batch-Record: brn_20260921_040000_beef " + "a" * 64 + "\n"
    elif fault == "duplicate-trailer":
        commit["message"] += commit["message"].splitlines()[2] + "\n"
    elif fault == "malformed-trailer":
        commit["message"] += "Brain-Core-Batch-Record: malformed\n"
    elif fault == "two-parents":
        commit["parents"] = [{"sha": before.commit_sha}, {"sha": before.commit_sha}]
    elif fault == "bytes":
        path = accepted.records[0].record_path
        fake.trees[commit["tree"]][path] = fake._blob(render_record(replace(records[0], body="tampered")).encode())
    elif fault == "preexisting":
        fake.trees[before.tree_sha][accepted.records[0].record_path] = fake._blob(render_record(records[0]).encode())
    head = fake.branch
    writes = len(fake.patch_payloads)
    with pytest.raises((GitHubConflict, GitHubStorageError)):
        adapter.revision_before_batch(records=requested, revision=adapter.revision())
    assert fake.branch == head and len(fake.patch_payloads) == writes


def test_bounded_history_and_missing_batch_refuse_without_writing():
    fake, adapter, _before, _accepted, records = batch()
    for i in range(100):
        fake.seed({"unrelated.md": str(i).encode()})
    head = fake.branch
    writes = len(fake.patch_payloads)
    with pytest.raises(GitHubStorageError) as error:
        adapter.revision_before_batch(records=records, revision=adapter.revision())
    assert error.value.code == "github_work_history_unavailable"
    assert fake.branch == head and len(fake.patch_payloads) == writes


def test_later_successor_and_archive_do_not_replace_the_verified_preimage():
    fake, adapter, before, accepted, records = batch()
    adapter.archive_superseded = True
    newer = replace(records[0], id="brn_20260921_040000_b003", supersedes=(records[0].id,), body="successor")
    adapter.publish_records(records=[render_record(newer).encode()], base_revision=adapter.revision())
    assert adapter.preflight_record_by_id(records[0].id, include_archive=True).match.path.startswith("archive/")
    found = adapter.revision_before_batch(records=records, revision=adapter.revision())
    assert found.before == before and found.accepted.commit_sha == accepted.commit_sha
    assert adapter.snapshot_records(kinds=frozenset({"item"}), revision=found.before).records == ()


def test_observe_commits_is_pinned_complete_oldest_first_and_materializes_each_revision():
    fake, adapter, before, accepted, _records = batch()
    fake.seed({"later.md": b"later"}, message="other caller commit")
    head = adapter.revision()
    writes = len(fake.patch_payloads)
    # Moving the live branch after pinning cannot change the observed interval.
    fake.seed({"newer.md": b"outside pinned interval"})
    observed = adapter.observe_commits_since(before.commit_sha, revision=head)
    assert observed.base == before and observed.head == head
    assert [commit.revision.commit_sha for commit in observed.commits] == [accepted.commit_sha, head.commit_sha]
    assert [commit.parent_sha for commit in observed.commits] == [before.commit_sha, accepted.commit_sha]
    assert observed.commits[0].message.startswith("Publish 2 Brain Core records together")
    assert observed.commits[1].message == "other caller commit"
    assert not adapter.snapshot_records(kinds=frozenset({"item"}), revision=observed.base).records
    assert len(adapter.snapshot_records(kinds=frozenset({"item"}), revision=observed.commits[0].revision).records) == 2
    assert len(fake.patch_payloads) == writes
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        observed.commits[0].message = "changed"
    with pytest.raises(FrozenInstanceError):
        observed.head = before


def test_observe_equal_heads_is_empty_and_default_head_is_read_once():
    fake, adapter, before, accepted, _records = batch()
    fake.events.clear()
    observed = adapter.observe_commits_since(before.commit_sha)
    assert observed.head.commit_sha == accepted.commit_sha
    assert len([event for event in fake.events if event.startswith("GET_REF")]) == 1
    fake.events.clear()
    empty = adapter.observe_commits_since(observed.head.commit_sha, revision=observed.head)
    assert empty.base == empty.head == observed.head and empty.commits == ()
    assert not any(event.startswith("GET_REF") for event in fake.events)


@pytest.mark.parametrize("fault", ["missing-ancestor", "merge", "cycle", "malformed-parent", "malformed-message", "missing-parents", "wrong-sha", "incomplete", "over-cap"])
def test_observe_refuses_unverified_history_without_partial_results_or_writes(fault):
    fake, adapter, before, accepted, _records = batch()
    fake.seed({"later.md": b"later"})
    head = adapter.revision()
    current = fake.commits[head.commit_sha]
    expected = before.commit_sha
    cap = 100
    if fault == "missing-ancestor":
        expected = "f" * 40
    elif fault == "merge":
        current["parents"] = [{"sha": accepted.commit_sha}, {"sha": before.commit_sha}]
    elif fault == "cycle":
        current["parent"] = head.commit_sha
    elif fault == "malformed-parent":
        current["parents"] = [{"sha": "invalid"}]
    elif fault == "malformed-message":
        current["message"] = None
    elif fault == "missing-parents":
        current["parents"] = None
    elif fault == "wrong-sha":
        current["response_sha"] = before.commit_sha
    elif fault == "incomplete":
        current["parents"] = []
    elif fault == "over-cap":
        cap = 1
    writes = len(fake.patch_payloads)
    with pytest.raises(GitHubStorageError):
        adapter.observe_commits_since(expected, revision=head, max_commits=cap)
    assert fake.branch == head.commit_sha and len(fake.patch_payloads) == writes


@pytest.mark.parametrize("cap", [0, -1, True, 101, "100"])
def test_observe_rejects_invalid_cap_before_reading(cap):
    fake = HistoryGitHub()
    with pytest.raises(ValueError):
        storage(fake).observe_commits_since(fake.branch, max_commits=cap)
    assert not fake.events


def test_observe_accepts_the_exact_cap_and_propagates_missing_commit_read_error():
    fake, adapter, before, _accepted, _records = batch()
    fake.seed({"later.md": b"later"})
    pinned = adapter.revision()
    assert len(adapter.observe_commits_since(before.commit_sha, revision=pinned, max_commits=2).commits) == 2
    fake.commits[pinned.commit_sha]["parent"] = "e" * 40
    with pytest.raises(GitHubStorageError) as error:
        adapter.observe_commits_since(before.commit_sha, revision=pinned)
    assert error.value.code == "github_api_error"
