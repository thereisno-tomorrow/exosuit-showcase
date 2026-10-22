"""Step C's archive path for hub-written task sitreps (docs/product/steps/b-project-sitreps.md).

`GitHubBrainHomeStorage.archive_records` moves whole sitrep record files to `archive/<path>`,
byte for byte, in one commit. Other kinds require an explicit allowlist.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from brain_core import sitreps
from brain_core.github_storage import (
    MAX_ARCHIVE_PATHS,
    GitHubConflict,
    GitHubObjectCache,
    GitHubPublicationUncertain,
    GitHubStorageError,
)
from brain_core.records import Evidence, render_record
from test_github_storage import BRANCH, REPOSITORY, TOKEN, FakeGitHub, storage

NOW = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)
HASH = hashlib.sha256(b"note").hexdigest()
HUB = Evidence(
    source_type="gremlin_town_hub",
    locator="gt-hub://sitreps/archive-fixture",
    content_sha256=hashlib.sha256(b"hub").hexdigest(),
    excerpt="Written by the hub from the project note; not the note itself.",
    actor="system:sitreps",
)


def _path(record) -> str:
    return f"records/{record.kind}/{record.created_at:%Y}/{record.created_at:%m}/{record.id}.md"


def _chain(session_id: str = "task-a") -> list:
    started = sitreps.start_sitrep(
        project="vault_fixture", session_id=session_id, intent="Old task", next_step="Step",
        note_record_id="brn_20260801_000000_aaaa", note_content_sha256=HASH, note_updated_at=NOW - timedelta(days=40),
    )
    first = sitreps.sitrep_record(started, previous=None, now=NOW - timedelta(days=40), evidence=HUB,
                                  owner_id="brain-home", owner_locator="vault://brain-home")
    closed = sitreps.close_sitrep(started, recovered=False, note_record_id="brn_20260801_000000_bbbb",
                                  note_content_sha256=HASH, note_updated_at=NOW - timedelta(days=31))
    second = sitreps.sitrep_record(closed, previous=first, now=NOW - timedelta(days=31), evidence=HUB,
                                   owner_id="brain-home", owner_locator="vault://brain-home")
    return [first, second]


def _seeded(fake: FakeGitHub, records) -> dict[str, bytes]:
    files = {_path(record): render_record(record).encode("utf-8") for record in records}
    fake.seed({**files, "records/continuity/2026/09/unrelated.md": b"not touched"})
    return files


def _tree(fake: FakeGitHub) -> dict[str, str]:
    return fake.trees[fake.commits[fake.branch]["tree"]]


def test_archive_moves_every_version_byte_for_byte_in_one_commit() -> None:
    fake = FakeGitHub()
    chain = _chain()
    files = _seeded(fake, chain)
    adapter = storage(fake)
    before = dict(_tree(fake))
    fake.events.clear()

    result = adapter.archive_records(paths=list(files), expected_revision=fake.branch)

    after = _tree(fake)
    assert result.status == "archived" and result.commit_sha == fake.branch
    assert result.repository == REPOSITORY and result.branch == BRANCH
    assert result.moved == tuple((path, "archive/" + path) for path in sorted(files))
    for path, content in files.items():
        assert path not in after
        assert after["archive/" + path] == before[path]
        assert fake.blobs[after["archive/" + path]] == content
    assert after["records/continuity/2026/09/unrelated.md"] == before["records/continuity/2026/09/unrelated.md"]
    assert [event for event in fake.events if event.startswith("POST_COMMIT")].__len__() == 1
    assert not [event for event in fake.events if event.startswith("POST_BLOB")]
    message = fake.commits[fake.branch]["message"]
    assert message.startswith("Archive 2 Brain Core sitrep records") and "Brain-Core-Archive-SHA256:" in message
    assert adapter.snapshot_records(kinds=frozenset({"sitrep"})).records == ()


def test_replaying_an_archive_that_landed_is_a_no_op() -> None:
    fake = FakeGitHub()
    files = _seeded(fake, _chain())
    adapter = storage(fake)
    first = adapter.archive_records(paths=list(files))
    fake.events.clear()
    again = adapter.archive_records(paths=list(files))
    assert again.status == "duplicate/no-op" and again.commit_sha == first.commit_sha
    assert not [event for event in fake.events if event.startswith(("POST_", "PATCH_"))]


@pytest.mark.parametrize(
    "paths",
    [
        ["records/continuity/2026/09/brn_20260928_100000_aaaa.md"],
        ["records/focus_copy/2026/09/brn_20260928_100000_aaaa.md"],
        ["records/item/2026/09/brn_20260928_100000_aaaa.md"],
        ["archive/records/sitrep/2026/09/brn_20260928_100000_aaaa.md"],
        ["records/sitrep/2026/13/brn_20260928_100000_aaaa.md"],
        [7],
    ],
)
def test_only_sitrep_record_paths_can_be_archived(paths: list) -> None:
    fake = FakeGitHub()
    with pytest.raises(GitHubStorageError) as refused:
        storage(fake).archive_records(paths=paths)
    assert refused.value.code == "github_archive_kind_refused"
    assert fake.events == []


def test_archive_needs_a_bounded_list_of_distinct_paths() -> None:
    fake = FakeGitHub()
    adapter = storage(fake)
    path = "records/sitrep/2026/09/brn_20260928_100000_aaaa.md"
    for paths in ([], [path, path], [f"records/sitrep/2026/09/brn_20260928_100000_{index:04x}.md" for index in range(MAX_ARCHIVE_PATHS + 1)]):
        with pytest.raises(GitHubStorageError) as refused:
            adapter.archive_records(paths=paths)
        assert refused.value.code == "github_archive_invalid"
    with pytest.raises(TypeError):
        adapter.archive_records(paths=path)  # type: ignore[arg-type]
    assert fake.events == []


def test_archive_refuses_a_stale_revision_a_missing_file_or_a_taken_archive_path() -> None:
    fake = FakeGitHub()
    files = _seeded(fake, _chain())
    adapter = storage(fake)
    stale = fake.branch
    fake.seed({"external.txt": b"moved on"})
    with pytest.raises(GitHubConflict) as moved:
        adapter.archive_records(paths=list(files), expected_revision=stale)
    assert moved.value.code == "github_revision_conflict"

    with pytest.raises(GitHubStorageError) as missing:
        adapter.archive_records(paths=["records/sitrep/2026/09/brn_20260928_100000_ffff.md"])
    assert missing.value.code == "github_path_missing"

    first = sorted(files)[0]
    fake.seed({"archive/" + first: b"someone else"})
    with pytest.raises(GitHubConflict) as taken:
        adapter.archive_records(paths=list(files))
    assert taken.value.code == "github_archive_conflict"


def test_archive_checks_each_file_really_is_that_sitrep_record() -> None:
    fake = FakeGitHub()
    chain = _chain()
    wrong_place = "records/sitrep/2026/09/brn_20260928_100000_abcd.md"
    fake.seed({wrong_place: render_record(chain[0]).encode("utf-8")})
    with pytest.raises(GitHubStorageError) as refused:
        storage(fake).archive_records(paths=[wrong_place])
    assert refused.value.code == "github_record_invalid"


def test_a_lost_reply_is_reconciled_from_the_branch_never_retried_blindly() -> None:
    fake = FakeGitHub()
    files = _seeded(fake, _chain())
    fake.lost_apply_once = True
    result = storage(fake).archive_records(paths=list(files))
    assert result.status == "reconciled"
    assert all(path not in _tree(fake) for path in files)

    fake = FakeGitHub()
    files = _seeded(fake, _chain())
    fake.lost_reject_count = 1
    with pytest.raises(GitHubPublicationUncertain) as uncertain:
        storage(fake).archive_records(paths=list(files))
    assert uncertain.value.code == "github_archive_uncertain"
    assert all(path in _tree(fake) for path in files)


def test_a_head_that_is_not_the_archive_commit_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub()
    files = _seeded(fake, _chain())
    adapter = storage(fake)
    fake.move_after_patch = fake.prepare_advance({"external.txt": b"raced"})
    monkeypatch.setattr(adapter, "_updated_head", lambda reply: None)
    with pytest.raises(GitHubConflict) as conflict:
        adapter.archive_records(paths=list(files))
    assert conflict.value.code == "github_revision_conflict"

    fake = FakeGitHub()
    files = _seeded(fake, _chain())
    adapter = storage(fake)
    monkeypatch.setattr(adapter, "_updated_head", lambda reply: "f" * 40)
    with pytest.raises(GitHubConflict):
        adapter.archive_records(paths=list(files))


def test_archive_keeps_the_hub_copy_current_so_the_next_read_needs_only_the_branch() -> None:
    from brain_core.github_storage import GitHubBrainHomeStorage

    fake = FakeGitHub()
    files = _seeded(fake, _chain())
    adapter = GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=fake,
                                     object_cache=GitHubObjectCache())
    adapter.snapshot_records(kinds=frozenset({"sitrep"}))
    adapter.archive_records(paths=list(files))
    fake.events.clear()
    assert adapter.snapshot_records(kinds=frozenset({"sitrep"})).records == ()
    assert not [event for event in fake.events if event.startswith(("GET_TREE", "GET_BLOB"))]


def test_sitrep_and_focus_copy_records_publish_and_read_back_like_other_hub_records() -> None:
    fake = FakeGitHub()
    adapter = storage(fake)
    chain = _chain("task-b")
    focus = sitreps.focus_copy_record(project="vault_fixture", line="Ship it.", section_sha256=HASH, previous=None,
                                      now=NOW, evidence=HUB, owner_id="brain-home", owner_locator="vault://brain-home")
    for record in (*chain, focus):
        published = adapter.publish_record(record_bytes=render_record(record).encode("utf-8"))
        assert published.status == "created" and published.record_kind == record.kind
    snapshot = adapter.snapshot_records(kinds=frozenset({"sitrep", "focus_copy"}))
    assert {item.record.id for item in snapshot.records} == {chain[0].id, chain[1].id, focus.id}
    assert sitreps.current_sitreps([item.record for item in snapshot.records], "vault_fixture") == {"task-b": chain[1]}


def test_explicit_item_archive_moves_exactly_the_listed_paths_and_preserves_default():
    from test_github_storage import v2_standalone_record
    from brain_core.records import parse_record
    fake = FakeGitHub()
    adapter = storage(fake)
    raws = [v2_standalone_record("item", suffix) for suffix in ("aaaa", "bbbb", "cccc")]
    files = {_path(parse_record(raw.decode())): raw for raw in raws}
    fake.seed(files)
    selected = list(files)[:2]
    with pytest.raises(GitHubStorageError) as refused:
        adapter.archive_records(paths=selected)
    assert refused.value.code == "github_archive_kind_refused"
    result = adapter.archive_records(paths=selected, kinds={"item"}, expected_revision=fake.branch)
    assert result.status == "archived"
    assert set(_tree(fake)) == {*("archive/" + p for p in selected), list(files)[2]}
    for path in selected:
        assert fake.blobs[_tree(fake)["archive/" + path]] == files[path]
