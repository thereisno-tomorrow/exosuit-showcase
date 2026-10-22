"""Publishing several records in one commit, all or nothing (Ciel and Brain Home PRD §4.10; build plan
slice 7; Accepted - Nic, 28 Sep 2026: "Nothing saved; retry")."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from brain_core.github_storage import (
    MAX_BATCH_RECORDS,
    GitHubConflict,
    GitHubObjectCache,
    GitHubPublicationUncertain,
    GitHubStorageError,
)
from brain_core.records import record_from_mapping, render_record
from test_github_storage import BRANCH, REPOSITORY, TOKEN, FakeGitHub, storage

NOW = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
HASH = hashlib.sha256(b"plan").hexdigest()


def _item(suffix: str, title: str, *, size: str = "task", links: list[dict[str, str]] | None = None) -> bytes:
    record = record_from_mapping({
        "schema": "brain.record/v2",
        "id": f"brn_20260929_100000_{suffix}",
        "kind": "item",
        "title": title,
        "state": "open",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "sensitivity": "private",
        "vaults": ["vault://brain-home"],
        "owners": ["brain-home"],
        "entities": [],
        "evidence": [{"source_type": "session_message", "locator": "session:plan:1", "content_sha256": HASH,
                      "excerpt": "yes, save the plan", "actor": "nic"}],
        "supersedes": [],
        "dedupe_key": None,
        "data": {"level": "accepted", "size": size, "timing": {}},
        "body": title,
        "source": {"said_by": "owner", "came_by": "direct", "words": "yes, save the plan", "recorded_by": "chatgpt-web"},
        "links": links or [],
    }, "fixture")
    return render_record(record).encode("utf-8")


def _plan() -> list[bytes]:
    goal = _item("a000", "Move house", size="goal")
    boxes = _item("a001", "Buy boxes", links=[{"type": "part_of", "target": "brn_20260929_100000_a000"}])
    pack = _item("a002", "Pack the books", links=[
        {"type": "part_of", "target": "brn_20260929_100000_a000"},
        {"type": "blocked_by", "target": "brn_20260929_100000_a001"},
    ])
    return [goal, boxes, pack]


def _paths(records: list[bytes]) -> list[str]:
    return [f"records/item/2026/09/brn_20260929_100000_a00{index}.md" for index in range(len(records))]


def _tree(fake: FakeGitHub) -> dict[str, str]:
    return fake.trees[fake.commits[fake.branch]["tree"]]


def test_a_whole_project_lands_in_one_commit_with_a_receipt_per_record() -> None:
    fake = FakeGitHub()
    plan = _plan()
    result = storage(fake).publish_records(records=plan)
    assert result.status == "created" and result.commit_sha == fake.branch
    assert (result.repository, result.branch) == (REPOSITORY, BRANCH)
    assert [(record.record_id, record.record_path) for record in result.records] == [
        (f"brn_20260929_100000_a00{index}", path) for index, path in enumerate(_paths(plan))
    ]
    assert [record.record_sha256 for record in result.records] == [hashlib.sha256(content).hexdigest() for content in plan]
    tree = _tree(fake)
    assert [fake.blobs[tree[path]] for path in _paths(plan)] == plan
    assert len([event for event in fake.events if event.startswith("POST_COMMIT")]) == 1
    message = fake.commits[fake.branch]["message"]
    assert message.startswith("Publish 3 Brain Core records together")
    assert "Brain-Core-Batch-Record: brn_20260929_100000_a001 " in message
    assert "Brain-Core-Record-ID:" not in message  # never mistaken for one record's own commit


def test_replaying_a_batch_that_landed_is_a_no_op_and_a_partial_or_different_one_is_a_conflict() -> None:
    fake = FakeGitHub()
    plan = _plan()
    adapter = storage(fake)
    first = adapter.publish_records(records=plan)
    fake.events.clear()
    again = adapter.publish_records(records=plan)
    assert again.status == "duplicate/no-op" and again.commit_sha == first.commit_sha
    assert not [event for event in fake.events if event.startswith(("POST_", "PATCH_"))]

    fake = FakeGitHub()
    fake.seed({_paths(plan)[1]: plan[1]})
    with pytest.raises(GitHubConflict) as partial:
        storage(fake).publish_records(records=plan)
    assert partial.value.code == "github_batch_conflict"
    assert _paths(plan)[0] not in _tree(fake)  # nothing half-saved


def test_a_batch_needs_one_to_many_valid_records_at_distinct_paths() -> None:
    fake = FakeGitHub()
    adapter = storage(fake)
    plan = _plan()
    for records in ([], [plan[0], plan[0]]):
        with pytest.raises(GitHubStorageError) as refused:
            adapter.publish_records(records=records)
        assert refused.value.code == "github_batch_invalid"
    too_many = [_item(f"b{index:03x}", f"Step {index}") for index in range(MAX_BATCH_RECORDS + 1)]
    with pytest.raises(GitHubStorageError):
        adapter.publish_records(records=too_many)
    with pytest.raises(TypeError):
        adapter.publish_records(records=plan[0])  # type: ignore[arg-type]
    with pytest.raises(Exception):
        adapter.publish_records(records=[b"not a record"])
    assert fake.events == []


def test_a_moved_branch_refuses_the_whole_batch() -> None:
    fake = FakeGitHub()
    adapter = storage(fake)
    base = adapter.revision()
    fake.seed({"external.txt": b"moved on"})
    with pytest.raises(GitHubConflict) as moved:
        adapter.publish_records(records=_plan(), base_revision=base)
    assert moved.value.code == "github_revision_conflict"
    assert all(path not in _tree(fake) for path in _paths(_plan()))


def test_a_lost_reply_is_reconciled_from_the_branch_never_retried_blindly() -> None:
    fake = FakeGitHub()
    fake.lost_apply_once = True
    result = storage(fake).publish_records(records=_plan())
    assert result.status == "reconciled"
    assert all(path in _tree(fake) for path in _paths(_plan()))

    fake = FakeGitHub()
    fake.lost_reject_count = 1
    with pytest.raises(GitHubPublicationUncertain) as uncertain:
        storage(fake).publish_records(records=_plan())
    assert uncertain.value.code == "github_publication_uncertain"
    assert all(path not in _tree(fake) for path in _paths(_plan()))  # nothing saved: send it again


def test_a_batch_keeps_the_hub_copy_current_so_the_next_read_needs_only_the_branch() -> None:
    from brain_core.github_storage import GitHubBrainHomeStorage

    fake = FakeGitHub()
    adapter = GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=fake,
                                     object_cache=GitHubObjectCache())
    adapter.snapshot_records(kinds=frozenset({"item"}))
    adapter.publish_records(records=_plan())
    fake.events.clear()
    assert len(adapter.snapshot_records(kinds=frozenset({"item"})).records) == 3
    assert not [event for event in fake.events if event.startswith(("GET_TREE", "GET_BLOB"))]
