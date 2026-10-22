"""Reads reuse GitHub objects by ID while the branch reference is read live every call."""

from __future__ import annotations

import logging
import threading

import pytest

import brain_core.github_storage as github_storage
from brain_core.github_storage import (
    GitHubBrainHomeStorage,
    GitHubObjectCache,
    GitHubRecordRead,
    GitHubStorageError,
)
from test_github_storage import BRANCH, REPOSITORY, TOKEN, FakeGitHub, standalone_record

KINDS = frozenset({"commitment"})


class MemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def load(self, blob_sha: str) -> bytes | None:
        return self.blobs.get(blob_sha)

    def store(self, blob_sha: str, content: bytes) -> None:
        self.blobs[blob_sha] = content


def cached(fake: FakeGitHub, cache: GitHubObjectCache | None = None) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(
        repository=REPOSITORY,
        branch=BRANCH,
        token=TOKEN,
        transport=fake,
        object_cache=cache or GitHubObjectCache(),
    )


def uncached(fake: FakeGitHub) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=fake)


def commitment_path(suffix: str) -> str:
    return f"records/commitment/2026/09/brn_20260921_040000_{suffix}.md"


def seeded(count: int) -> FakeGitHub:
    fake = FakeGitHub()
    fake.seed({commitment_path(f"{index:08x}"): standalone_record("commitment", f"{index:08x}") for index in range(count)})
    return fake


def kinds_of(events: list[str]) -> list[str]:
    return [event.split(" ", 1)[0] for event in events]


def summary(read: GitHubRecordRead) -> tuple[object, ...]:
    return (
        read.revision,
        tuple((item.path, item.content, item.blob_sha, item.record.as_dict()) for item in read.records),
    )


def test_warm_snapshot_reads_only_the_branch_reference() -> None:
    fake = seeded(20)
    storage = cached(fake)

    first = storage.snapshot_records(kinds=KINDS)
    assert kinds_of(fake.events).count("GET_BLOB") == 20

    fake.events.clear()
    second = storage.snapshot_records(kinds=KINDS)

    assert kinds_of(fake.events) == ["GET_REF"]
    assert summary(second) == summary(first)


def test_cached_reads_match_uncached_reads() -> None:
    fake = seeded(12)
    storage = cached(fake)
    storage.snapshot_records(kinds=KINDS)

    assert summary(storage.snapshot_records(kinds=KINDS)) == summary(uncached(fake).snapshot_records(kinds=KINDS))
    paths = (commitment_path("00000003"), commitment_path("00000007"))
    assert summary(storage.read_records(paths)) == summary(uncached(fake).read_records(paths))


def test_a_new_commit_is_seen_at_once_and_only_its_new_file_downloads() -> None:
    fake = seeded(10)
    storage = cached(fake)
    storage.snapshot_records(kinds=KINDS)

    new_path = commitment_path("0000beef")
    head = fake.seed({new_path: standalone_record("commitment", "0000beef")})
    fake.events.clear()
    read = storage.snapshot_records(kinds=KINDS)

    assert read.revision.commit_sha == head
    assert new_path in {item.path for item in read.records}
    assert kinds_of(fake.events) == ["GET_REF", "GET_COMMIT", "GET_TREE", "GET_BLOB"]


def test_the_copy_survives_a_restart_through_the_blob_store() -> None:
    fake = seeded(15)
    store = MemoryBlobStore()
    before = cached(fake, GitHubObjectCache(blob_store=store)).snapshot_records(kinds=KINDS)
    assert len(store.blobs) == 15

    fake.events.clear()
    after = cached(fake, GitHubObjectCache(blob_store=store)).snapshot_records(kinds=KINDS)

    assert kinds_of(fake.events) == ["GET_REF", "GET_COMMIT", "GET_TREE"]
    assert summary(after) == summary(before)


def test_a_stored_copy_that_fails_its_hash_is_ignored_and_replaced(caplog: pytest.LogCaptureFixture) -> None:
    fake = seeded(3)
    store = MemoryBlobStore()
    good = cached(fake, GitHubObjectCache(blob_store=store)).snapshot_records(kinds=KINDS)
    victim = good.records[1]
    store.blobs[victim.blob_sha] = victim.content + b"tampered on disk"

    fake.events.clear()
    with caplog.at_level(logging.WARNING, logger="brain_core.github_storage"):
        read = cached(fake, GitHubObjectCache(blob_store=store)).snapshot_records(kinds=KINDS)

    assert summary(read) == summary(good)
    assert fake.events.count(f"GET_BLOB {victim.blob_sha}") == 1
    assert store.blobs[victim.blob_sha] == victim.content
    assert victim.blob_sha in caplog.text


def test_an_unsafe_tree_is_never_kept() -> None:
    fake = seeded(2)
    fake.extra_tree_entry = {"path": "unsafe-entry", "mode": "120000", "type": "blob", "sha": "f" * 40}
    storage = cached(fake)

    for _ in range(2):
        fake.events.clear()
        with pytest.raises(GitHubStorageError) as unsafe:
            storage.snapshot_records(kinds=KINDS)
        assert unsafe.value.code == "github_tree_unsafe"
        assert "GET_TREE" in kinds_of(fake.events)


def test_a_blob_that_fails_its_hash_is_never_kept() -> None:
    fake = seeded(2)
    store = MemoryBlobStore()
    storage = cached(fake, GitHubObjectCache(blob_store=store))

    fake.tamper_blob = True
    with pytest.raises(GitHubStorageError) as mismatch:
        storage.snapshot_records(kinds=KINDS)
    assert mismatch.value.code == "github_blob_hash_mismatch"
    assert store.blobs == {}

    fake.tamper_blob = False
    assert summary(storage.snapshot_records(kinds=KINDS)) == summary(uncached(fake).snapshot_records(kinds=KINDS))


def test_each_record_version_is_parsed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = github_storage.parse_record

    def counting(text: str, locator: str = "record") -> object:
        calls.append(locator)
        return real(text, locator)

    monkeypatch.setattr(github_storage, "parse_record", counting)
    fake = seeded(8)
    storage = cached(fake)
    for _ in range(3):
        storage.snapshot_records(kinds=KINDS)
    storage.find_record_by_id("brn_20260921_040000_00000002")

    assert len(calls) == 8


def test_a_saved_record_reads_back_with_one_call() -> None:
    fake = FakeGitHub()
    storage = cached(fake)
    created = storage.publish_record(record_bytes=standalone_record("commitment", "cafe0001"))

    fake.events.clear()
    read = storage.read_records((created.record_path,))

    assert kinds_of(fake.events) == ["GET_REF"]
    assert read.revision.commit_sha == created.commit_sha
    assert read.records[0].content == standalone_record("commitment", "cafe0001")


def test_lookups_by_id_reuse_the_copy() -> None:
    fake = seeded(6)
    storage = cached(fake)
    first = storage.find_record_by_id("brn_20260921_040000_00000004")

    fake.events.clear()
    second = storage.find_record_by_id("brn_20260921_040000_00000004")

    assert kinds_of(fake.events) == ["GET_REF"]
    assert first is not None and second is not None and second.content == first.content


def test_a_small_memory_limit_evicts_but_reads_stay_correct() -> None:
    fake = seeded(10)
    record_size = len(standalone_record("commitment", "00000000"))
    storage = cached(fake, GitHubObjectCache(max_blob_bytes=record_size * 3))
    first = storage.snapshot_records(kinds=KINDS)

    fake.events.clear()
    second = storage.snapshot_records(kinds=KINDS)

    assert summary(second) == summary(first)
    assert kinds_of(fake.events).count("GET_BLOB") > 0


def test_concurrent_reads_agree() -> None:
    fake = seeded(25)
    storage = cached(fake)
    expected = summary(uncached(fake).snapshot_records(kinds=KINDS))
    results: list[tuple[object, ...]] = []
    failures: list[BaseException] = []

    def read() -> None:
        try:
            results.append(summary(storage.snapshot_records(kinds=KINDS)))
        except BaseException as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    assert results == [expected] * 8


def test_scan_limits_are_explicit_for_callers() -> None:
    fake = seeded(4)
    storage = cached(fake)

    with pytest.raises(GitHubStorageError, match="explicit limit"):
        storage.snapshot_records(kinds=KINDS, max_records=3)
    with pytest.raises(GitHubStorageError, match="byte limit"):
        storage.snapshot_records(kinds=KINDS, max_bytes=10)
    assert len(storage.snapshot_records(kinds=KINDS, max_records=4).records) == 4


def test_cache_arguments_are_checked() -> None:
    with pytest.raises(TypeError):
        GitHubBrainHomeStorage(
            repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=FakeGitHub(), object_cache=object()
        )
    with pytest.raises(ValueError):
        GitHubObjectCache(max_trees=0)
