"""A memory save writes on its preflight's revision and makes only the calls GitHub needs.

Counted against ``FakeGitHub.events``: with a cold object cache a save is the branch,
its commit and tree, then the four writes (blob, tree, commit, branch update); warm,
the branch and the four writes. Archived records are found in the tree, not the
commit list, and every refusal the long path had still happens.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import pytest

import brain_core.github_storage as github_storage
from brain_core.github_storage import (
    GitHubBrainHomeStorage,
    GitHubConflict,
    GitHubObjectCache,
    GitHubPublicationUncertain,
    GitHubRevision,
    GitHubStorageError,
    HTTPResponse,
)
from brain_core.records import parse_record
from test_github_storage import (
    BRANCH,
    REPOSITORY,
    TOKEN,
    FakeGitHub,
    memory_operation_record,
    scoped_item_record,
    standalone_record,
)

WRITES = ["POST_BLOB", "POST_TREE", "POST_COMMIT", "PATCH_OK"]


def cached(fake: FakeGitHub, cache: GitHubObjectCache | None = None) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(
        repository=REPOSITORY,
        branch=BRANCH,
        token=TOKEN,
        transport=fake,
        object_cache=cache if cache is not None else GitHubObjectCache(),
    )


def uncached(fake: FakeGitHub) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=fake)


def kinds_of(events: list[str]) -> list[str]:
    return [event.split(" ", 1)[0] for event in events]


def operation(operation_id: str, kind: str = "capture") -> tuple[str, bytes]:
    """An operation's input hash and a record bound to it, as the provider builds one."""

    input_sha256 = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    key = github_storage.memory_operation_dedupe_key(operation_id, input_sha256)
    suffix = github_storage.memory_operation_record_suffix(operation_id)
    return input_sha256, memory_operation_record(kind, suffix, key)


def record_path(content: bytes) -> str:
    record = parse_record(content.decode("utf-8"), "write path fixture")
    return f"records/{record.kind}/{record.created_at:%Y/%m}/{record.id}.md"


def unrelated(count: int) -> dict[str, bytes]:
    return {
        f"records/commitment/2026/09/brn_20260921_040000_{index:08x}.md": standalone_record("commitment", f"{index:08x}")
        for index in range(count)
    }


def move(fake: FakeGitHub, source: str, target: str) -> str:
    """Commit one file's move, byte for byte, as the clear-out's archive step would."""

    tree = dict(fake.trees[fake.commits[fake.branch]["tree"]])
    tree[target] = tree.pop(source)
    tree_sha = fake._sha()
    fake.trees[tree_sha] = tree
    commit_sha = fake._sha()
    fake.commits[commit_sha] = {"tree": tree_sha, "parent": fake.branch, "message": "archive", "paths": {source, target}}
    fake.branch = commit_sha
    return commit_sha


def save(storage: GitHubBrainHomeStorage, operation_id: str, kind: str = "capture"):
    input_sha256, content = operation(operation_id, kind)
    preflight = storage.preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256)
    assert preflight.match is None
    return content, storage.publish_record(record_bytes=content, base_revision=preflight.revision)


def test_a_pinned_save_makes_seven_calls_cold_and_five_warm() -> None:
    fake = FakeGitHub()
    fake.seed(unrelated(40))
    storage = cached(fake)

    fake.events.clear()
    content, created = save(storage, "write-path-cold")
    assert kinds_of(fake.events) == ["GET_REF", "GET_COMMIT", "GET_TREE", *WRITES]
    assert created.status == "created" and created.remote_accepted is True
    assert created.commit_sha == fake.branch
    assert fake.commits[created.commit_sha]["tree"] == created.tree_sha
    assert fake.blobs[fake.trees[created.tree_sha][created.record_path]] == content

    fake.events.clear()
    _, second = save(storage, "write-path-warm", "commitment")
    assert kinds_of(fake.events) == ["GET_REF", *WRITES]
    assert second.commit_sha == fake.branch


def test_what_a_publication_created_is_kept_so_the_next_read_needs_only_the_branch() -> None:
    fake = FakeGitHub()
    fake.seed(unrelated(3))
    cache = GitHubObjectCache()
    storage = cached(fake, cache)
    storage.snapshot_records(kinds=frozenset({"capture", "commitment"}))
    content, created = save(storage, "write-path-remember")

    assert cache.commit_tree(created.commit_sha) == created.tree_sha
    assert cache.tree(created.tree_sha) == fake.trees[created.tree_sha]
    assert cache.blob(fake.trees[created.tree_sha][created.record_path]) == content

    fake.events.clear()
    read = storage.read_records((created.record_path,))
    snapshot = storage.snapshot_records(kinds=frozenset({"capture", "commitment"}))
    assert kinds_of(fake.events) == ["GET_REF", "GET_REF"]
    assert read.revision.commit_sha == snapshot.revision.commit_sha == created.commit_sha
    assert read.records[0].content == content
    reference = uncached(fake).snapshot_records(kinds=frozenset({"capture", "commitment"}))
    assert [(item.path, item.content) for item in snapshot.records] == [
        (item.path, item.content) for item in reference.records
    ]


def test_a_tree_that_cannot_be_derived_is_not_kept() -> None:
    fake = FakeGitHub()
    input_sha256, content = operation("write-path-odd-tree")
    path = record_path(content)
    folder = path.rsplit("/", 1)[0]
    fake.seed({folder: b"a file where a folder goes"})
    cache = GitHubObjectCache()
    storage = cached(fake, cache)
    preflight = storage.preflight_memory_operation(operation_id="write-path-odd-tree", input_sha256=input_sha256)

    created = storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert cache.commit_tree(created.commit_sha) == created.tree_sha
    assert cache.tree(created.tree_sha) is None


def test_a_pinned_save_on_a_branch_that_moved_is_a_revision_conflict_and_lands_nothing() -> None:
    fake = FakeGitHub()
    storage = cached(fake)
    input_sha256, content = operation("write-path-moved")
    preflight = storage.preflight_memory_operation(operation_id="write-path-moved", input_sha256=input_sha256)
    moved = fake.seed({"external.txt": b"another writer"})
    fake.events.clear()

    with pytest.raises(GitHubConflict) as conflict:
        storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert conflict.value.code == "github_revision_conflict"
    assert conflict.value.details == {"expected": preflight.revision.commit_sha, "actual": moved}
    assert kinds_of(fake.events) == [
        "POST_BLOB", "POST_TREE", "POST_COMMIT", "PATCH_NOT_FAST_FORWARD", "GET_REF", "GET_COMMIT",
    ]
    assert fake.branch == moved
    assert record_path(content) not in fake.trees[fake.commits[moved]["tree"]]


class _RefusingGitHub(FakeGitHub):
    """Refuses every branch update for a reason other than a moved branch."""

    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None) -> HTTPResponse:
        if method == "PATCH":
            self.events.append("PATCH_REFUSED")
            return HTTPResponse(422, {}, b'{"message": "Reference update failed"}')
        return super().request(method, url, headers=headers, body=body)


def test_a_refused_update_on_an_unmoved_branch_keeps_its_own_refusal() -> None:
    fake = _RefusingGitHub()
    storage = cached(fake)
    input_sha256, content = operation("write-path-refused")
    preflight = storage.preflight_memory_operation(operation_id="write-path-refused", input_sha256=input_sha256)

    with pytest.raises(GitHubConflict) as refused:
        storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert refused.value.code == "github_conflict"
    assert fake.branch == preflight.revision.commit_sha


class _ReplyGitHub(FakeGitHub):
    """Applies branch updates but answers them with a chosen body."""

    def __init__(self, reply: Mapping[str, Any]) -> None:
        super().__init__()
        self.reply = dict(reply)

    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None) -> HTTPResponse:
        response = super().request(method, url, headers=headers, body=body)
        if method == "PATCH" and response.status == 200:
            return HTTPResponse(200, {}, json.dumps(self.reply).encode("utf-8"))
        return response


def test_an_update_reply_that_names_no_head_is_confirmed_by_reading_back() -> None:
    fake = _ReplyGitHub({"ref": f"refs/heads/{BRANCH}"})
    storage = cached(fake)
    input_sha256, content = operation("write-path-bare-reply")
    preflight = storage.preflight_memory_operation(operation_id="write-path-bare-reply", input_sha256=input_sha256)
    fake.events.clear()

    created = storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert created.status == "created" and created.commit_sha == fake.branch
    assert kinds_of(fake.events) == [*WRITES, "GET_REF", "GET_COMMIT", "GET_TREE", "GET_BLOB"]


def test_an_update_reply_that_names_another_head_is_a_revision_conflict() -> None:
    other = "f" * 40
    fake = _ReplyGitHub({"object": {"sha": other, "type": "commit"}})
    storage = cached(fake)
    input_sha256, content = operation("write-path-other-head")
    preflight = storage.preflight_memory_operation(operation_id="write-path-other-head", input_sha256=input_sha256)

    with pytest.raises(GitHubConflict) as conflict:
        storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert conflict.value.code == "github_revision_conflict"
    assert conflict.value.details["actual"] == other


class _MislabellingGitHub(FakeGitHub):
    """Answers a blob upload with an ID that is not the hash of the uploaded bytes."""

    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None) -> HTTPResponse:
        response = super().request(method, url, headers=headers, body=body)
        if method == "POST" and url.endswith("/git/blobs"):
            self.events.append("POST_BLOB_MISLABELLED")
            return HTTPResponse(201, {}, json.dumps({"sha": "e" * 40}).encode("utf-8"))
        return response


def test_a_blob_id_that_does_not_match_the_record_stops_before_any_commit() -> None:
    fake = _MislabellingGitHub()
    storage = cached(fake)
    input_sha256, content = operation("write-path-blob-id")
    preflight = storage.preflight_memory_operation(operation_id="write-path-blob-id", input_sha256=input_sha256)

    with pytest.raises(GitHubStorageError) as mismatch:
        storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert mismatch.value.code == "github_blob_hash_mismatch"
    assert not any(event.startswith(("POST_TREE", "POST_COMMIT", "PATCH_")) for event in fake.events)
    assert fake.branch == preflight.revision.commit_sha


def test_a_lost_reply_on_a_pinned_save_is_reconciled_exactly_as_before() -> None:
    accepted = FakeGitHub()
    accepted.lost_apply_once = True
    storage = cached(accepted)
    input_sha256, content = operation("write-path-lost")
    preflight = storage.preflight_memory_operation(operation_id="write-path-lost", input_sha256=input_sha256)
    accepted.events.clear()

    reconciled = storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert reconciled.status == "reconciled" and reconciled.commit_sha == accepted.branch
    assert kinds_of(accepted.events)[:5] == ["POST_BLOB", "POST_TREE", "POST_COMMIT", "PATCH_LOST_ACCEPTED", "GET_COMMITS"]
    assert len(accepted.patch_payloads) == 1

    rejected = FakeGitHub()
    rejected.lost_reject_count = 1
    storage = cached(rejected)
    input_sha256, content = operation("write-path-lost-retry")
    preflight = storage.preflight_memory_operation(operation_id="write-path-lost-retry", input_sha256=input_sha256)
    rejected.events.clear()

    retried = storage.publish_record(record_bytes=content, base_revision=preflight.revision)

    assert retried.status == "created" and retried.commit_sha == rejected.branch
    assert kinds_of(rejected.events) == [
        "POST_BLOB", "POST_TREE", "POST_COMMIT", "PATCH_LOST_NOT_ACCEPTED", "GET_COMMITS", "GET_REF",
        "PATCH_OK", "GET_REF", "GET_COMMIT", "GET_TREE", "GET_BLOB",
    ]
    assert rejected.patch_payloads[0] == rejected.patch_payloads[1]

    uncertain = FakeGitHub()
    uncertain.lost_reject_count = 2
    storage = cached(uncertain)
    input_sha256, content = operation("write-path-lost-twice")
    preflight = storage.preflight_memory_operation(operation_id="write-path-lost-twice", input_sha256=input_sha256)
    with pytest.raises(GitHubPublicationUncertain):
        storage.publish_record(record_bytes=content, base_revision=preflight.revision)


def test_a_base_revision_must_be_one_this_storage_observed() -> None:
    fake = FakeGitHub()
    storage = cached(fake)
    input_sha256, content = operation("write-path-base")
    revision = storage.preflight_memory_operation(operation_id="write-path-base", input_sha256=input_sha256).revision
    fake.events.clear()

    with pytest.raises(ValueError, match="cannot be combined"):
        storage.publish_record(record_bytes=content, base_revision=revision, expected_revision=revision.commit_sha)
    with pytest.raises(TypeError):
        storage.publish_record(record_bytes=content, base_revision=revision.commit_sha)  # type: ignore[arg-type]
    for wrong in (
        GitHubRevision(REPOSITORY, "other-branch", revision.commit_sha, revision.tree_sha),
        GitHubRevision("trusted/elsewhere", BRANCH, revision.commit_sha, revision.tree_sha),
        GitHubRevision(REPOSITORY, BRANCH, revision.commit_sha, "d" * 40),
    ):
        with pytest.raises(ValueError):
            storage.publish_record(record_bytes=content, base_revision=wrong)
        with pytest.raises(ValueError):
            storage.snapshot_records(kinds=frozenset({"capture"}), revision=wrong)
    assert not any(event.startswith(("POST_", "PATCH_")) for event in fake.events)


def test_pinned_reads_stay_on_their_revision_without_reading_the_branch() -> None:
    fake = FakeGitHub()
    first = standalone_record("capture", "a001")
    later = standalone_record("capture", "a002")
    fake.seed({record_path(first): first})
    storage = cached(fake)
    pinned = storage.revision()
    storage.snapshot_records(kinds=frozenset({"capture"}))
    fake.seed({record_path(later): later})
    fake.events.clear()

    snapshot = storage.snapshot_records(kinds=frozenset({"capture"}), revision=pinned)
    found = storage.preflight_record_by_id("brn_20260921_040000_a001", revision=pinned)
    absent = storage.preflight_record_by_id("brn_20260921_040000_a002", revision=pinned)

    assert fake.events == []
    assert snapshot.revision == found.revision == absent.revision == pinned
    assert [item.content for item in snapshot.records] == [first]
    assert found.match is not None and found.match.content == first
    assert absent.match is None


def test_a_replayed_save_of_an_archived_record_is_found_in_the_tree_without_the_commit_list() -> None:
    fake = FakeGitHub()
    storage = cached(fake)
    content, created = save(storage, "write-path-archived")
    archived = f"archive/{created.record_path}"
    head = move(fake, created.record_path, archived)
    fake.events.clear()

    replay = storage.preflight_memory_operation(
        operation_id="write-path-archived",
        input_sha256=operation("write-path-archived")[0],
    )

    assert replay.match is not None
    assert replay.match.path == archived
    assert replay.match.content == content
    assert replay.match.record.id == created.record_id
    assert replay.revision.commit_sha == head
    assert "GET_COMMITS" not in kinds_of(fake.events)

    with pytest.raises(GitHubConflict) as changed:
        storage.preflight_memory_operation(operation_id="write-path-archived", input_sha256="0" * 64)
    assert changed.value.code == "github_memory_operation_identity_conflict"


def test_archived_candidates_keep_the_corrupt_and_duplicate_checks() -> None:
    operation_id = "write-path-archive-checks"
    input_sha256, content = operation(operation_id)
    live = record_path(content)
    suffix = github_storage.memory_operation_record_suffix(operation_id)

    corrupt = FakeGitHub()
    corrupt.seed({f"archive/records/capture/2026/09/brn_20260921_040000_{suffix}.md": b"not a record"})
    with pytest.raises(GitHubStorageError) as invalid:
        cached(corrupt).preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256)
    assert invalid.value.code == "github_memory_operation_corrupt"

    misplaced = FakeGitHub()
    misplaced.seed({f"archive/{live.replace('/capture/', '/commitment/')}": content})
    with pytest.raises(GitHubStorageError) as wrong_path:
        cached(misplaced).preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256)
    assert wrong_path.value.code == "github_memory_operation_corrupt"

    twice = FakeGitHub()
    twice.seed({live: content, f"archive/{live}": content})
    with pytest.raises(GitHubConflict) as duplicate:
        cached(twice).preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256)
    assert duplicate.value.code == "github_memory_operation_duplicate"

    ignored = FakeGitHub()
    ignored.seed(
        {
            f"archive/archive/{live}": b"not a candidate",
            f"archive/other/{live}": b"not a candidate",
            f"archive/records/decision/2026/09/brn_20260921_040000_{suffix}.md": b"not a candidate",
        }
    )
    absent = cached(ignored).preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256)
    assert absent.match is None
    assert "GET_BLOB" not in kinds_of(ignored.events)


def test_an_archived_legacy_record_is_found_by_its_author_and_is_no_candidate_for_another_caller() -> None:
    """P3 and the archive together: a record saved before caller scoping (legacy suffix)
    and later archived is still found by its own author's replay, and is not a
    candidate at all for another caller's identically named operation."""

    operation_id = "write-path-archived-legacy"
    input_sha256 = hashlib.sha256(b"archived legacy input").hexdigest()
    dedupe_key = github_storage.memory_operation_dedupe_key(operation_id, input_sha256)
    legacy_suffix = github_storage.memory_operation_record_suffix(operation_id)
    content = scoped_item_record(legacy_suffix, dedupe_key, "nic")
    archived = f"archive/records/item/2026/09/brn_20260921_040000_{legacy_suffix}.md"
    fake = FakeGitHub()
    fake.seed({archived: content})
    storage = cached(fake)

    by_its_author = storage.preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256, caller="nic")
    assert by_its_author.match is not None
    assert by_its_author.match.path == archived
    assert by_its_author.match.content == content
    with pytest.raises(GitHubConflict) as changed:
        storage.preflight_memory_operation(operation_id=operation_id, input_sha256="0" * 64, caller="nic")
    assert changed.value.code == "github_memory_operation_identity_conflict"

    # Another caller: not a match, and not a conflict even with different input,
    # because a legacy-suffix record only counts for its own author.
    for other_input in (input_sha256, "0" * 64):
        by_another = storage.preflight_memory_operation(
            operation_id=operation_id, input_sha256=other_input, caller="damien"
        )
        assert by_another.match is None


def test_an_archived_caller_scoped_record_is_never_read_for_another_caller() -> None:
    operation_id = "write-path-archived-scoped"
    input_sha256 = hashlib.sha256(b"archived scoped input").hexdigest()
    dedupe_key = github_storage.memory_operation_dedupe_key(operation_id, input_sha256)
    suffix = github_storage.memory_operation_record_suffix(operation_id, caller="nic")
    content = scoped_item_record(suffix, dedupe_key, "nic")
    archived = f"archive/records/item/2026/09/brn_20260921_040000_{suffix}.md"
    fake = FakeGitHub()
    fake.seed({archived: content})

    found = cached(fake).preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256, caller="nic")
    assert found.match is not None and found.match.path == archived

    fake.events.clear()
    other = cached(fake).preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256, caller="damien")
    assert other.match is None
    assert "GET_BLOB" not in kinds_of(fake.events)
