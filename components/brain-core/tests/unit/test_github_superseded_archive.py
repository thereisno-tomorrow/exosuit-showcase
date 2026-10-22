"""Layer 2: identical reader answers, immutable blobs, checked moves and retries."""
from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest

from brain_core.agenda import agenda
from brain_core.github_storage import GitHubBrainHomeStorage, GitHubConflict, GitHubObjectCache, GitHubStorageError
from brain_core.orientation import OrientationInputs, overview
from brain_core.placement import scope
from brain_core.records import RecordStore, parse_record, record_from_mapping, render_record
from brain_core.review import ItemRecord, ReviewInputs, answers_by_task, build_review, current_items
from brain_core.routines import routine_stats, versions_of
from brain_core.weekly import weekly_review
from test_github_archive import _chain
from test_github_storage import BRANCH, REPOSITORY, TOKEN, FakeGitHub, standalone_record, v2_standalone_record

NOW = datetime(2026, 9, 30, 5, tzinfo=UTC)


def adapter(fake, *, enabled=True, cache=None):
    return GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=fake,
                                  archive_superseded=enabled, object_cache=cache)


def path_of(record):
    return f"records/{record.kind}/{record.created_at:%Y/%m}/{record.id}.md"


def tree(fake):
    return fake.trees[fake.commits[fake.branch]["tree"]]


def record(kind="item", suffix="abcd", *, routine=False):
    raw = v2_standalone_record(kind, suffix) if kind in {"item", "fact"} else standalone_record(kind, suffix)
    value = parse_record(raw.decode(), "fixture").as_dict()
    if kind == "item":
        value["data"]["level"] = "accepted"
        value["data"]["timing"] = {"due": "2026-10-02"}
        if routine:
            value["data"]["repeat"] = "daily"
    return record_from_mapping(value)


def revised(previous, step, tmp_path, **changes):
    return RecordStore(tmp_path)._revised_candidate(
        previous, now=previous.created_at + timedelta(minutes=step), state=None, data=changes or None,
        title=None, body=None, evidence=(), entropy=f"{int((previous.supersedes[0] if previous.supersedes else previous.id).rsplit('_', 1)[1], 16):04x}{step:04x}", source=None,
    )


def publish(storage, record, *, batch=False, **kwargs):
    raw = render_record(record).encode()
    return storage.publish_records(records=[raw], **kwargs) if batch else storage.publish_record(record_bytes=raw, **kwargs)


def outputs(storage):
    read = storage.snapshot_records(kinds=frozenset({"item", "fact", "preference"}))
    items = tuple(ItemRecord(f.record, f.path) for f in read.records if f.record.kind == "item")
    facts = tuple(ItemRecord(f.record, f.path) for f in read.records if f.record.kind == "fact")
    preferences = tuple(f.record for f in read.records if f.record.kind == "preference")
    inputs = ReviewInputs(items, (), frozenset(), (), (), {}, NOW, facts=facts, open_notes=(), preferences=preferences)
    routines = [entry.record for entry in current_items(items) if entry.record.data.get("repeat")]
    all_records = tuple(entry.record for entry in items)
    return (current_items(items), answers_by_task(items, facts), build_review(inputs),
            overview(OrientationInputs(inputs, preferences=preferences), NOW), weekly_review(items, NOW, preferences=preferences),
            agenda(items, "Sam", NOW), scope(items, NOW.date(), NOW, frozenset()),
            tuple(routine_stats(r, versions_of(r, all_records), NOW.date()) for r in routines))


@pytest.mark.parametrize("seed", [3, 17, 41])
def test_generated_histories_preserve_every_core_reader_and_every_blob(tmp_path, seed):
    rng = random.Random(seed)
    off, on = FakeGitHub(), FakeGitHub()
    old, new = adapter(off, enabled=False), adapter(on, cache=GitHubObjectCache())
    written = {}
    for number, kind in enumerate(("item", "item", "fact", "preference", "capture", "commitment")):
        version = record(kind, f"{number + 20:04x}", routine=number == 1)
        for step in range(rng.randint(3, 7)):
            if step:
                changes = {"occurrence": {"day": f"2026-09-{21 + step:02d}", "outcome": "done"}} if number == 1 else {}
                version = revised(version, step, tmp_path, **changes)
            publish(old, version, batch=step % 2 == 0)
            publish(new, version, batch=step % 2 == 0)
            written[path_of(version)] = render_record(version).encode()
            assert outputs(old) == outputs(new)
    actual = {p.removeprefix("archive/"): on.blobs[b] for p, b in tree(on).items()}
    assert actual == written
    assert len(tree(on)) == len(written)  # no duplicated blobs at two paths
    for path, raw in written.items():
        rid = parse_record(raw.decode()).id
        assert new.find_record_by_id(rid, include_archive=True).content == raw
        assert new.read_records([path], archived_ok=True).records[0].content == raw


@pytest.mark.parametrize("batch", [False, True])
def test_publish_is_one_commit_with_moves_cache_and_no_op_replay(tmp_path, batch):
    fake = FakeGitHub()
    storage = adapter(fake, cache=GitHubObjectCache())
    first = record()
    publish(storage, first)
    next_record = revised(first, 1, tmp_path)
    fake.events.clear()
    accepted = publish(storage, next_record, batch=batch)
    assert "Brain-Core-Archived: 1\n" in fake.commits[fake.branch]["message"]
    assert len([e for e in fake.events if e.startswith("POST_COMMIT")]) == 1
    assert tree(fake)["archive/" + path_of(first)] == fake._blob(render_record(first).encode())
    assert path_of(first) not in tree(fake)
    fake.events.clear()
    storage.snapshot_records(kinds=frozenset({"item"}))
    assert not [e for e in fake.events if e.startswith(("GET_TREE", "GET_BLOB"))]
    replay = publish(storage, next_record, batch=batch)
    assert replay.status == "duplicate/no-op" and replay.commit_sha == accepted.commit_sha
    assert not [e for e in fake.events if e.startswith(("POST_", "PATCH_"))]


@pytest.mark.parametrize("batch", [False, True])
def test_lost_reply_and_racing_writers_confirm_moves_and_retry_from_new_head(tmp_path, batch):
    fake = FakeGitHub()
    storage = adapter(fake)
    first = record()
    publish(storage, first)
    second = revised(first, 1, tmp_path)
    stale = storage.revision()
    fake.lost_apply_once = True
    assert publish(storage, second, batch=batch, base_revision=stale).status == "reconciled"
    rival = revised(first, 2, tmp_path)
    with pytest.raises(GitHubConflict) as raced:
        publish(storage, rival, batch=batch, base_revision=stale)
    assert raced.value.code == "github_revision_conflict"
    assert publish(storage, rival, batch=batch, base_revision=storage.revision()).status == "created"
    assert path_of(first) not in tree(fake)


@pytest.mark.parametrize("batch", [False, True])
def test_a_lost_reply_cannot_confirm_only_the_record_without_its_moves(tmp_path, monkeypatch, batch):
    fake = FakeGitHub()
    storage = adapter(fake)
    first = record()
    publish(storage, first)
    before = dict(tree(fake))
    second = revised(first, 1, tmp_path)
    real = storage._patch_ref
    def partial(candidate):
        fake.lost_apply_once = True
        # Adversarial GitHub returns an accepted replacement whose tree lost its moves.
        candidate_tree = fake.trees[fake.commits[candidate]["tree"]]
        candidate_tree[path_of(first)] = before[path_of(first)]
        candidate_tree.pop("archive/" + path_of(first))
        return real(candidate)
    monkeypatch.setattr(storage, "_patch_ref", partial)
    with pytest.raises(GitHubConflict) as refused:
        publish(storage, second, batch=batch)
    assert refused.value.code in {"github_archive_conflict", "github_revision_conflict"}


def test_missing_history_warns_but_writes_and_conflicting_archive_refuses(tmp_path, caplog):
    first = record()
    second = revised(first, 1, tmp_path)
    fake = FakeGitHub()
    publish(adapter(fake), second)
    assert first.id in caplog.text and "missing" in caplog.text
    fake = FakeGitHub()
    fake.seed({path_of(first): render_record(first).encode(), "archive/" + path_of(first): b"different"})
    with pytest.raises(GitHubConflict) as refused:
        publish(adapter(fake), second)
    assert refused.value.code == "github_archive_conflict"
    assert not [e for e in fake.events if e.startswith("POST_")]


def test_same_blob_in_both_places_is_collapsed_without_overwriting(tmp_path):
    fake = FakeGitHub()
    first = record()
    raw = render_record(first).encode()
    fake.seed({path_of(first): raw, "archive/" + path_of(first): raw})
    publish(adapter(fake), revised(first, 1, tmp_path))
    assert path_of(first) not in tree(fake) and fake.blobs[tree(fake)["archive/" + path_of(first)]] == raw


@pytest.mark.parametrize("batch", [False, True])
def test_routines_keep_every_version_even_when_new_version_removes_repeat(tmp_path, batch):
    fake = FakeGitHub()
    storage = adapter(fake)
    first = record(routine=True)
    publish(storage, first)
    second = revised(first, 1, tmp_path, occurrence={"day": "2026-09-22", "outcome": "done"})
    publish(storage, second, batch=batch)
    assert all(path_of(r) in tree(fake) for r in (first, second))
    with pytest.raises(GitHubStorageError) as refused:
        storage.archive_superseded_records(paths=[path_of(first)], expected_revision=fake.branch)
    assert refused.value.code == "github_archive_routine_refused"
    ordinary = revised(second, 2, tmp_path, repeat=None)
    publish(storage, ordinary, batch=batch)
    assert all(path_of(r) in tree(fake) for r in (first, second, ordinary))


class ContentAddressedTrees(FakeGitHub):
    """Use content-addressed fake trees so restore proves the actual tree SHA, too."""
    def request(self, method, url, *, headers, body):
        response = super().request(method, url, headers=headers, body=body)
        if method == "POST" and urlsplit(url).path.endswith("/git/trees"):
            old = json.loads(response.body)["sha"]
            value = self.trees[old]
            sha = hashlib.sha1(json.dumps(value, sort_keys=True).encode(), usedforsecurity=False).hexdigest()
            self.trees[sha] = value
            return self._response(201, {"sha": sha})
        return response


def test_checked_move_replay_and_restore_return_identical_tree_hash(tmp_path):
    fake = ContentAddressedTrees()
    storage = adapter(fake, enabled=False)
    first = record()
    second = revised(first, 1, tmp_path)
    publish(storage, first)
    publish(storage, second)
    before = storage.revision()
    paths = [path_of(first)]
    fake.lost_apply_once = True
    assert storage.archive_superseded_records(paths=paths, expected_revision=fake.branch).status == "reconciled"
    assert storage.archive_superseded_records(paths=paths, expected_revision=fake.branch).status == "duplicate/no-op"
    fake.lost_apply_once = True
    assert storage.restore_archived_records(paths=paths, expected_revision=fake.branch).status == "reconciled"
    assert storage.revision().tree_sha == before.tree_sha
    assert storage.restore_archived_records(paths=paths, expected_revision=fake.branch).status == "duplicate/no-op"


def test_checked_move_refuses_unnamed_versions_and_stale_head_and_restore_conflicts(tmp_path):
    fake = FakeGitHub()
    storage = adapter(fake, enabled=False)
    first = record()
    publish(storage, first)
    with pytest.raises(GitHubStorageError) as refused:
        storage.archive_superseded_records(paths=[path_of(first)], expected_revision=fake.branch)
    assert refused.value.code == "github_archive_not_superseded"
    stale = fake.branch
    second = revised(first, 1, tmp_path)
    publish(storage, second)
    with pytest.raises(GitHubConflict) as raced:
        storage.archive_superseded_records(paths=[path_of(first)], expected_revision=stale)
    assert raced.value.code == "github_revision_conflict"
    storage.archive_superseded_records(paths=[path_of(first)], expected_revision=fake.branch)
    fake.seed({path_of(first): b"different"})
    with pytest.raises(GitHubConflict) as refused:
        storage.restore_archived_records(paths=[path_of(first)], expected_revision=fake.branch)
    assert refused.value.code == "github_archive_conflict"


@pytest.mark.parametrize("kind", ["item", "fact", "preference", "capture", "commitment"])
def test_local_writer_keeps_whole_history_oldest_first(tmp_path, kind):
    store = RecordStore(tmp_path)
    current = record(kind)
    store.create(current)
    ids = []
    for step in range(1, 5):
        ids.append(current.id)
        kwargs = {} if current.schema.endswith("/v1") else {
            "expected_sha256": hashlib.sha256(render_record(current).encode()).hexdigest(), "actor": "nic",
            "source": {"said_by": "owner", "came_by": "direct", "words": current.evidence[-1].excerpt, "recorded_by": "nic"},
        }
        current, _ = store.revise(current.id, now=current.created_at + timedelta(minutes=1), entropy=f"{step:04x}", **kwargs)
        assert current.supersedes == tuple(ids)


def test_note_writer_keeps_whole_history_and_publish_moves_note_events(tmp_path):
    from brain_core.continuity import append_event, start_continuity
    from brain_core.records import Evidence
    store = RecordStore(tmp_path)
    evidence = Evidence("fixture", "fixture:note", "f" * 64, "note")
    current = start_continuity(store, session_id="session", intent="Ship", sources=(), evidence=evidence, now=NOW)
    # ContinuityWrite carries the exact writer record.
    current = current.record
    fake = FakeGitHub()
    storage = adapter(fake)
    publish(storage, current)
    ids = []
    for step in range(3):
        ids.append(current.id)
        result = append_event(store, session_id="session", event="effect", value=f"effect {step}",
                              evidence=evidence, now=NOW + timedelta(minutes=step + 1))
        current = result.record
        assert current.supersedes == tuple(ids)
        publish(storage, current)
    assert storage.snapshot_records(kinds=frozenset({"continuity"})).records[0].record == current


def test_sitreps_read_identically_after_their_replaced_version_moves():
    from brain_core.sitreps import current_sitreps
    off, on = FakeGitHub(), FakeGitHub()
    for version in _chain():
        publish(adapter(off, enabled=False), version)
        publish(adapter(on), version)
    def read(fake):
        return current_sitreps([f.record for f in adapter(fake).snapshot_records(kinds=frozenset({"sitrep"})).records], "vault_fixture")
    assert read(off) == read(on)


def test_switch_is_off_by_default_and_rejects_non_boolean():
    from test_github_storage import storage
    assert storage(FakeGitHub()).archive_superseded is False
    with pytest.raises(TypeError):
        GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN, archive_superseded="false")


def test_replaying_a_batch_after_later_changes_archived_it_creates_nothing(tmp_path):
    fake = FakeGitHub()
    storage = adapter(fake)
    first, other = record(), record("fact", "eeee")
    storage.publish_records(records=[render_record(r).encode() for r in (first, other)])
    replacements = [revised(r, 1, tmp_path) for r in (first, other)]
    storage.publish_records(records=[render_record(r).encode() for r in replacements])
    after = dict(tree(fake))
    fake.events.clear()
    replay = storage.publish_records(records=[render_record(r).encode() for r in (first, other)])
    assert replay.status == "duplicate/no-op" and tree(fake) == after
    assert not [e for e in fake.events if e.startswith(("POST_", "PATCH_"))]


def test_multi_record_batch_writes_and_moves_every_blob_in_one_commit(tmp_path):
    fake = FakeGitHub()
    storage = adapter(fake)
    first, other = record(), record("fact", "eeee")
    for version in (first, other):
        publish(storage, version)
    base = dict(tree(fake))
    fake.events.clear()
    replacements = [revised(r, 1, tmp_path) for r in (first, other)]
    storage.publish_records(records=[render_record(r).encode() for r in replacements])
    assert len([e for e in fake.events if e.startswith("POST_COMMIT")]) == 1
    assert "Brain-Core-Archived: 2\n" in fake.commits[fake.branch]["message"]
    assert tree(fake) == {**{"archive/" + path: blob for path, blob in base.items()},
                          **{path_of(r): fake._blob(render_record(r).encode()) for r in replacements}}


@pytest.fixture
def migration():
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[4] / "components/gremlin-town/deploy/release/brain-home-archive-superseded.py"
    spec = importlib.util.spec_from_file_location("w2_superseded_migration", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_dry_run_apply_and_restore_keep_current_blobs_and_tree_hash(tmp_path, migration):
    fake = ContentAddressedTrees()
    storage = adapter(fake, enabled=False)
    versions = [record()]
    for step in range(1, 4):
        versions.append(revised(versions[-1], step, tmp_path))
    routine = record(suffix="ffff", routine=True)
    routine_next = revised(routine, 1, tmp_path, occurrence={"day": "2026-09-22", "outcome": "done"})
    for version in [*versions, routine, routine_next]:
        publish(storage, version)
    before = storage.revision()
    before_tree = dict(tree(fake))
    storage = adapter(fake)
    fake.events.clear()
    dry = migration.execute(storage)
    assert dry["counts"]["item"] == {"live_now": 6, "to_move": 3, "live_after": 3}
    assert set(dry["moves"]) == {path_of(v) for v in versions[:-1]}
    assert not [e for e in fake.events if e.startswith(("POST_", "PATCH_"))]
    journal = tmp_path / "moved.json"
    fake.lost_apply_once = True
    moved = migration.execute(storage, apply=True, expected_head=dry["head"], moved_list=journal)
    assert moved["current"] == dry["current"]
    assert moved["moved"] == dry["moves"] == json.loads(journal.read_text())["moves"]
    assert all(tree(fake)["archive/" + path] == blob for path, blob in dry["moves"].items())
    off = adapter(fake, enabled=False)
    restored = migration.execute(off, apply=True, expected_head=fake.branch, restore=journal)
    assert restored["restored"] == 3
    assert tree(fake) == before_tree and off.revision().tree_sha == before.tree_sha


def test_migration_leaves_incomplete_histories_alone_and_refuses_different_archive_blob(tmp_path, migration):
    fake = FakeGitHub()
    storage = adapter(fake, enabled=False)
    first = record()
    second = revised(first, 1, tmp_path)
    third = revised(second, 2, tmp_path).as_dict()
    third["supersedes"] = [second.id]  # omits the first ID, reachable through second
    for version in (first, second, record_from_mapping(third)):
        publish(storage, version)
    dry = migration.execute(adapter(fake))
    assert not dry["moves"] and any("omits history" in line for line in dry["left_alone"])
    fake.seed({"archive/" + path_of(second): b"different"})
    with pytest.raises(GitHubConflict) as refused:
        migration.execute(adapter(fake))
    assert refused.value.code == "github_archive_conflict"


def test_migration_batches_501_paths_and_verifies_each_move(tmp_path, migration, monkeypatch):
    fake = FakeGitHub()
    originals = [record(suffix=f"{i + 1:04x}") for i in range(501)]
    current = revised(originals[0], 99, tmp_path).as_dict()
    current["supersedes"] = [r.id for r in originals]
    current = record_from_mapping(current)
    fake.seed({path_of(r): render_record(r).encode() for r in [*originals, current]})
    storage = adapter(fake)
    real = storage.archive_superseded_records
    batches = []
    def checked(**kwargs):
        batches.append(len(kwargs["paths"]))
        return real(**kwargs)
    monkeypatch.setattr(storage, "archive_superseded_records", checked)
    result = migration.execute(storage, apply=True, expected_head=fake.branch, moved_list=tmp_path / "moved.json")
    assert batches == [500, 1]
    assert len(result["moved"]) == 501
    assert result["counts"]["item"] == {"live_now": 1, "to_move": 0, "live_after": 1}


def test_migration_refuses_stale_head_and_changed_current_after_move(tmp_path, migration, monkeypatch):
    fake = FakeGitHub()
    off = adapter(fake, enabled=False)
    first = record()
    publish(off, first)
    second = revised(first, 1, tmp_path)
    publish(off, second)
    storage = adapter(fake)
    with pytest.raises(GitHubConflict):
        migration.execute(storage, apply=True, expected_head="f" * 40, moved_list=tmp_path / "stale.json")
    real = storage.archive_superseded_records
    def unexpected(**kwargs):
        result = real(**kwargs)
        changed = second.as_dict()
        changed["title"] = "Unexpected overwrite"
        fake.seed({path_of(second): render_record(record_from_mapping(changed)).encode()})
        return result
    monkeypatch.setattr(storage, "archive_superseded_records", unexpected)
    with pytest.raises(GitHubStorageError) as refused:
        migration.execute(storage, apply=True, expected_head=fake.branch, moved_list=tmp_path / "uncertain.json")
    assert refused.value.code == "github_archive_verification_failed"
    assert json.loads((tmp_path / "uncertain.json").read_text())["moves"] == {path_of(first): fake._blob(render_record(first).encode())}


def test_migration_recomputes_after_a_race_and_restore_refuses_wrong_blob(tmp_path, migration, monkeypatch):
    fake = FakeGitHub()
    off = adapter(fake, enabled=False)
    first = record()
    publish(off, first)
    publish(off, revised(first, 1, tmp_path))
    storage = adapter(fake)
    real = storage.archive_superseded_records
    calls = []
    def raced(**kwargs):
        calls.append(kwargs["expected_revision"])
        if len(calls) == 1:
            fake.seed({"external.txt": b"raced"})
        return real(**kwargs)
    monkeypatch.setattr(storage, "archive_superseded_records", raced)
    journal = tmp_path / "moved.json"
    migration.execute(storage, apply=True, expected_head=fake.branch, moved_list=journal)
    assert len(calls) == 2 and calls[0] != calls[1]
    fake.seed({"archive/" + path_of(first): b"tampered"})
    with pytest.raises(GitHubConflict) as refused:
        migration.execute(adapter(fake, enabled=False), apply=True, expected_head=fake.branch, restore=journal)
    assert refused.value.code == "github_archive_conflict"
