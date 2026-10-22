from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

import brain_core.github_storage as github_storage
from brain_core.contracts import canonical_json
from brain_core.durability import (
    CATALOG_BYTES,
    CATALOG_ID,
    CATALOG_PATH,
    CATALOG_SCHEMA,
    CATALOG_SHA256,
    EMPTY_SHA256,
    SharedDurabilityWriter,
)
from brain_core.github_storage import (
    GitHubBrainHomeStorage,
    GitHubConflict,
    GitHubStorageError,
    GitHubTransportError,
    HTTPResponse,
    UrllibHTTPTransport,
)
from brain_core.records import parse_record, record_from_mapping, render_record


REPOSITORY = "trusted/brain-home"
BRANCH = "accepted"
TOKEN = "fixture-token"
SOURCE_HASH = hashlib.sha256(b"github storage fixture").hexdigest()


def request(
    origin_id: str = "github-origin",
    *,
    choice: str = "Use the configured GitHub Brain Home branch",
) -> dict[str, object]:
    return {
        "schema": "brain.durability-request/v1",
        "verb": "decide",
        "operation": "accept",
        "origin_id": origin_id,
        "choice": choice,
        "title": "Use configured GitHub Brain Home",
        "because": "Shared records need one remotely observable revision",
        "forecloses": "Uncorrelated file-by-file updates",
        "governing_scope": "Brain Home publication",
        "encoded_in": "UNENCODED",
        "completeness_check": "Record and exact receipt share one accepted commit",
        "explicit_scope": {"kind": "personal", "project_id": None},
        "default_scope": {"kind": "ambiguous", "project_id": None},
        "source_context_sha256": SOURCE_HASH,
        "session_delta_sha256": None,
        "accepted_at": "2026-09-21T12:00:00+08:00",
        "ownership_catalog": {
            "path": CATALOG_PATH,
            "bytes": CATALOG_BYTES,
            "sha256": CATALOG_SHA256,
            "schema": CATALOG_SCHEMA,
            "catalog_id": CATALOG_ID,
        },
        "expected_owner_locator": "vault://brain-home",
        "expected_writer_generation": 1,
        "observed_preimage_sha256": EMPTY_SHA256,
    }


def writer_pair(
    tmp_path: Path,
    origin_id: str = "github-origin",
    *,
    choice: str = "Use the configured GitHub Brain Home branch",
) -> tuple[bytes, bytes]:
    brain_home = tmp_path / "brain-home"
    meta = tmp_path / "meta-vault"
    brain_home.mkdir(parents=True)
    (meta / "ops").mkdir(parents=True)
    (meta / "ops" / "decisions.md").write_text("# Decisions\n", encoding="utf-8", newline="\n")
    writer = SharedDurabilityWriter(
        root_bindings={"vault://brain-home": brain_home, "vault://meta-vault": meta}
    )
    receipt = writer.write(request(origin_id, choice=choice))
    assert receipt.status == "created"
    record_path = brain_home / str(receipt.canonical_locator).removeprefix("vault://brain-home/")
    receipt_path = brain_home / "records" / ".durability" / "receipts" / f"{origin_id}.json"
    return record_path.read_bytes(), receipt_path.read_bytes()


def duplicate_decision(record_bytes: bytes, suffix: str) -> tuple[str, bytes]:
    original = parse_record(record_bytes.decode("utf-8"), "decision duplicate fixture")
    value = original.as_dict()
    value["id"] = f"{original.id.rsplit('_', 1)[0]}_{suffix}"
    duplicate = record_from_mapping(value, "decision duplicate fixture")
    path = f"records/decision/{duplicate.created_at:%Y/%m}/{duplicate.id}.md"
    return path, render_record(duplicate).encode("utf-8")


class FakeGitHub:
    def __init__(self) -> None:
        self._next = 1
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        root_tree = self._sha()
        root_commit = self._sha()
        self.trees[root_tree] = {}
        self.commits[root_commit] = {
            "tree": root_tree,
            "parent": None,
            "message": "fixture root",
            "paths": set(),
        }
        self.branch = root_commit
        self.events: list[str] = []
        self.patch_payloads: list[dict[str, Any]] = []
        self.advance_after_ref: str | None = None
        self.conflict_on_patch = False
        self.lost_apply_once = False
        self.lost_reject_count = 0
        self.advance_after_commit_lookup: str | None = None
        self.move_after_patch: str | None = None
        self.wrap_base64 = False
        self.tamper_blob = False
        self.extra_tree_entry: dict[str, str] | None = None
        self.allowance = {"limit": 5000, "remaining": 4990, "used": 10, "reset": 1790000000}

    def _sha(self) -> str:
        value = f"{self._next:040x}"
        self._next += 1
        return value

    @staticmethod
    def _response(status: int, value: Any) -> HTTPResponse:
        return HTTPResponse(status, {}, json.dumps(value, sort_keys=True).encode("utf-8"))

    def _blob(self, content: bytes) -> str:
        framed = f"blob {len(content)}\0".encode("ascii") + content
        sha = hashlib.sha1(framed, usedforsecurity=False).hexdigest()
        self.blobs[sha] = content
        return sha

    def prepare_advance(self, files: Mapping[str, bytes], *, message: str = "external") -> str:
        base = self.commits[self.branch]
        tree = dict(self.trees[base["tree"]])
        for path, content in files.items():
            tree[path] = self._blob(content)
        tree_sha = self._sha()
        self.trees[tree_sha] = tree
        commit_sha = self._sha()
        self.commits[commit_sha] = {
            "tree": tree_sha,
            "parent": self.branch,
            "message": message,
            "paths": set(files),
        }
        return commit_sha

    def seed(self, files: Mapping[str, bytes], *, message: str = "seed") -> str:
        commit = self.prepare_advance(files, message=message)
        self.branch = commit
        return commit

    def _history(self) -> list[str]:
        result: list[str] = []
        current: str | None = self.branch
        while current is not None:
            result.append(current)
            current = self.commits[current]["parent"]
        return result

    def _fast_forward(self, candidate: str) -> bool:
        """GitHub moves a branch without force only onto a commit that contains its head."""

        current: str | None = candidate
        while current is not None:
            if current == self.branch:
                return True
            current = self.commits[current]["parent"]
        return False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HTTPResponse:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        parsed = urlsplit(url)
        if method == "GET" and parsed.path == "/rate_limit":
            self.events.append("GET_RATE_LIMIT")
            return self._response(200, {"resources": {"core": dict(self.allowance)}})
        prefix = f"/repos/{REPOSITORY}"
        assert parsed.path.startswith(prefix)
        tail = parsed.path[len(prefix):]
        payload = None if body is None else json.loads(body)

        if method == "GET" and tail.startswith("/git/ref/heads/"):
            assert unquote(tail.removeprefix("/git/ref/heads/")) == BRANCH
            observed = self.branch
            self.events.append(f"GET_REF {observed}")
            if self.advance_after_ref is not None:
                self.branch = self.advance_after_ref
                self.advance_after_ref = None
            return self._response(200, {"object": {"sha": observed, "type": "commit"}})

        if method == "GET" and tail.startswith("/git/commits/"):
            sha = tail.removeprefix("/git/commits/")
            self.events.append(f"GET_COMMIT {sha}")
            commit = self.commits[sha]
            return self._response(200, {"sha": sha, "tree": {"sha": commit["tree"]}})

        if method == "GET" and tail.startswith("/git/trees/"):
            tree_sha = tail.removeprefix("/git/trees/")
            self.events.append(f"GET_TREE {tree_sha}")
            entries = [
                {"path": path, "mode": "100644", "type": "blob", "sha": blob, "size": len(self.blobs[blob])}
                for path, blob in sorted(self.trees[tree_sha].items())
            ]
            if self.extra_tree_entry is not None:
                entries.append(dict(self.extra_tree_entry))
            return self._response(200, {"sha": tree_sha, "truncated": False, "tree": entries})

        if method == "GET" and tail.startswith("/git/blobs/"):
            blob_sha = tail.removeprefix("/git/blobs/")
            self.events.append(f"GET_BLOB {blob_sha}")
            content = self.blobs[blob_sha] + (b"tampered" if self.tamper_blob else b"")
            encoded = base64.b64encode(content).decode("ascii")
            if self.wrap_base64:
                encoded = "\n".join(encoded[index:index + 12] for index in range(0, len(encoded), 12))
            return self._response(
                200,
                {
                    "sha": blob_sha,
                    "encoding": "base64",
                    "size": len(content),
                    "content": encoded,
                },
            )

        if method == "GET" and tail == "/commits":
            query = parse_qs(parsed.query)
            assert query["sha"] == [BRANCH]
            receipt_path = query["path"][0]
            self.events.append(f"GET_COMMITS {receipt_path}")
            rows = []
            for sha in self._history():
                commit = self.commits[sha]
                if receipt_path in commit["paths"]:
                    rows.append({"sha": sha, "commit": {"message": commit["message"]}})
            if self.advance_after_commit_lookup is not None:
                self.branch = self.advance_after_commit_lookup
                self.advance_after_commit_lookup = None
            return self._response(200, rows)

        if method == "POST" and tail == "/git/blobs":
            assert payload["encoding"] == "base64"
            content = base64.b64decode(payload["content"], validate=True)
            sha = self._blob(content)
            self.events.append(f"POST_BLOB {sha}")
            return self._response(201, {"sha": sha})

        if method == "POST" and tail == "/git/trees":
            tree = dict(self.trees[payload["base_tree"]])
            paths: set[str] = set()
            for entry in payload["tree"]:
                assert entry["mode"] == "100644" and entry["type"] == "blob"
                if entry["sha"] is None:
                    # GitHub's tree API deletes a path whose entry has "sha": null.
                    tree.pop(entry["path"], None)
                else:
                    tree[entry["path"]] = entry["sha"]
                paths.add(entry["path"])
            sha = self._sha()
            self.trees[sha] = tree
            self.events.append(f"POST_TREE {sha}")
            return self._response(201, {"sha": sha, "paths": sorted(paths)})

        if method == "POST" and tail == "/git/commits":
            sha = self._sha()
            parent = payload["parents"][0]
            parent_tree = self.trees[self.commits[parent]["tree"]]
            next_tree = self.trees[payload["tree"]]
            paths = {path for path in set(parent_tree) | set(next_tree) if parent_tree.get(path) != next_tree.get(path)}
            self.commits[sha] = {
                "tree": payload["tree"],
                "parent": parent,
                "message": payload["message"],
                "paths": paths,
            }
            self.events.append(f"POST_COMMIT {sha}")
            return self._response(201, {"sha": sha})

        if method == "PATCH" and tail.startswith("/git/refs/heads/"):
            assert unquote(tail.removeprefix("/git/refs/heads/")) == BRANCH
            assert payload["force"] is False
            self.patch_payloads.append(payload)
            candidate = payload["sha"]
            if self.conflict_on_patch:
                external = self.prepare_advance({"external.txt": b"raced"})
                self.branch = external
                self.events.append(f"PATCH_CONFLICT {candidate}")
                return self._response(422, {"message": "not a fast forward"})
            if not self._fast_forward(candidate):
                self.events.append(f"PATCH_NOT_FAST_FORWARD {candidate}")
                return self._response(422, {"message": "Update is not a fast forward"})
            if self.lost_apply_once:
                self.lost_apply_once = False
                self.branch = candidate
                self.events.append(f"PATCH_LOST_ACCEPTED {candidate}")
                raise GitHubTransportError("fixture_lost_reply", "fixture lost accepted reply")
            if self.lost_reject_count:
                self.lost_reject_count -= 1
                self.events.append(f"PATCH_LOST_NOT_ACCEPTED {candidate}")
                raise GitHubTransportError("fixture_lost_reply", "fixture lost rejected reply")
            self.branch = candidate
            self.events.append(f"PATCH_OK {candidate}")
            if self.move_after_patch is not None:
                self.branch = self.move_after_patch
                self.move_after_patch = None
            return self._response(200, {"object": {"sha": candidate}})

        raise AssertionError((method, tail, parsed.query, payload))


def storage(fake: FakeGitHub) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(
        repository=REPOSITORY,
        branch=BRANCH,
        token=TOKEN,
        transport=fake,
    )


def test_layer_two_switch_defaults_off_and_keeps_existing_publication_behavior():
    adapter = storage(FakeGitHub())
    assert adapter.archive_superseded is False


def test_coherent_read_stays_on_one_revision_when_branch_advances() -> None:
    fake = FakeGitHub()
    old_commit = fake.seed({"records/a.md": b"old-a", "records/b.md": b"old-b"})
    new_commit = fake.prepare_advance({"records/a.md": b"new-a", "records/b.md": b"new-b"})
    fake.advance_after_ref = new_commit

    result = storage(fake).read(("records/a.md", "records/b.md"))

    assert result.revision.commit_sha == old_commit
    assert result.file("records/a.md").content == b"old-a"
    assert result.file("records/b.md").content == b"old-b"
    assert fake.branch == new_commit


def test_atomic_record_and_receipt_publication_and_duplicate_noop(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    fake = FakeGitHub()
    adapter = storage(fake)

    created = adapter.publish(record_bytes=record, receipt_bytes=receipt)
    writes_after_create = tuple(event for event in fake.events if event.startswith(("POST_", "PATCH_")))
    duplicate = adapter.publish(record_bytes=record, receipt_bytes=receipt)
    writes_after_duplicate = tuple(event for event in fake.events if event.startswith(("POST_", "PATCH_")))

    assert created.status == "created" and created.remote_accepted is True
    assert duplicate.status == "duplicate/no-op"
    assert duplicate.commit_sha == created.commit_sha
    assert writes_after_duplicate == writes_after_create
    tree = fake.trees[fake.commits[fake.branch]["tree"]]
    assert set(tree) == {created.record_path, created.receipt_path}
    assert fake.blobs[tree[created.record_path]] == record
    assert fake.blobs[tree[created.receipt_path]] == receipt
    publication_commits = [event for event in writes_after_create if event.startswith("POST_COMMIT")]
    assert len(publication_commits) == 1


def test_semantic_decision_lookup_is_coherent_and_reads_only_decision_records(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    fake = FakeGitHub()
    adapter = storage(fake)
    created = adapter.publish(record_bytes=record, receipt_bytes=receipt)
    old_commit = fake.branch
    next_commit = fake.prepare_advance({"unrelated.txt": b"advanced"})
    fake.advance_after_ref = next_commit
    fake.events.clear()

    match = adapter.find_decision_by_semantic_key(created.semantic_key)

    assert match is not None
    assert match.revision.commit_sha == old_commit == created.commit_sha
    assert match.path == created.record_path
    assert match.content == record
    assert match.record.kind == "decision"
    assert match.record.dedupe_key == created.semantic_key
    assert fake.branch == next_commit
    assert len([event for event in fake.events if event.startswith("GET_TREE")]) == 1
    assert len([event for event in fake.events if event.startswith("GET_BLOB")]) == 1


def test_semantic_decision_lookup_keeps_exact_receipt_and_locator_reads_public(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    fake = FakeGitHub()
    adapter = storage(fake)
    created = adapter.publish(record_bytes=record, receipt_bytes=receipt)

    match = adapter.find_decision_by_semantic_key(created.semantic_key)
    exact = adapter.read((created.record_path, created.receipt_path))
    accepted = adapter.lookup_accepted(record_bytes=record, receipt_bytes=receipt)

    assert match is not None and match.path == created.record_path
    assert exact.revision.commit_sha == created.commit_sha
    assert exact.file(created.record_path).content == record
    assert exact.file(created.receipt_path).content == receipt
    assert accepted is not None and accepted.commit_sha == created.commit_sha


def test_semantic_decision_lookup_empty_and_candidate_bound_fail_loudly() -> None:
    semantic_key = "a" * 64
    assert storage(FakeGitHub()).find_decision_by_semantic_key(semantic_key) is None

    fake = FakeGitHub()
    fake.seed(
        {
            f"records/decision/2026/09/brn_20260921_04000{index}_{index:04x}.md": b"not fetched"
            for index in range(3)
        }
    )
    with pytest.raises(GitHubStorageError) as bounded:
        storage(fake).find_decision_by_semantic_key(semantic_key, max_candidates=2)

    assert bounded.value.code == "github_decision_candidate_limit"
    assert bounded.value.details == {"candidates": 3, "max_candidates": 2}
    assert not any(event.startswith("GET_BLOB") for event in fake.events)

    adapter = storage(FakeGitHub())
    with pytest.raises(GitHubStorageError) as invalid_key:
        adapter.find_decision_by_semantic_key("A" * 64)
    assert invalid_key.value.code == "github_semantic_key_invalid"
    for value in (0, True, github_storage.MAX_DECISION_CANDIDATES + 1):
        with pytest.raises(ValueError, match="max_candidates"):
            adapter.find_decision_by_semantic_key(semantic_key, max_candidates=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("corrupt", [b"\xff", b"not a record"])
def test_semantic_decision_lookup_rejects_corrupt_decision_candidates(corrupt: bytes) -> None:
    path = "records/decision/2026/09/brn_20260921_040000_dead.md"
    fake = FakeGitHub()
    fake.seed({path: corrupt, "records/capture/not-a-decision.md": b"ignored"})

    with pytest.raises(GitHubStorageError) as invalid:
        storage(fake).find_decision_by_semantic_key("b" * 64)

    assert invalid.value.code == "github_decision_corrupt"
    assert invalid.value.details == {"path": path}


def test_semantic_decision_lookup_rejects_duplicate_valid_matches(tmp_path: Path) -> None:
    record, _ = writer_pair(tmp_path)
    original = parse_record(record.decode("utf-8"), "decision duplicate source")
    first_path = f"records/decision/{original.created_at:%Y/%m}/{original.id}.md"
    second_path, second_record = duplicate_decision(record, "d00d")
    fake = FakeGitHub()
    fake.seed({first_path: record, second_path: second_record})

    with pytest.raises(GitHubConflict) as duplicate:
        storage(fake).find_decision_by_semantic_key(str(original.dedupe_key))

    assert duplicate.value.code == "github_decision_duplicate"
    assert duplicate.value.details["paths"] == sorted((first_path, second_path))


def test_record_id_lookup_is_restart_safe_coherent_and_fetches_only_exact_id() -> None:
    target = standalone_record("capture", "e001")
    other = standalone_record("preference", "e002")
    fake = FakeGitHub()
    first_adapter = storage(fake)
    created = first_adapter.publish_record(record_bytes=target)
    first_adapter.publish_record(record_bytes=other)
    observed_commit = fake.branch
    next_commit = fake.prepare_advance({"unrelated.txt": b"advanced"})
    fake.advance_after_ref = next_commit
    fake.events.clear()

    restarted_adapter = storage(fake)
    match = restarted_adapter.find_record_by_id(created.record_id)

    assert match is not None
    assert match.revision.commit_sha == observed_commit
    assert match.path == created.record_path
    assert match.content == target
    assert match.record.id == created.record_id
    assert fake.branch == next_commit
    assert len([event for event in fake.events if event.startswith("GET_TREE")]) == 1
    assert len([event for event in fake.events if event.startswith("GET_BLOB")]) == 1


def test_record_id_lookup_empty_and_candidate_bound_fail_loudly() -> None:
    record_id = "brn_20260921_040000_e003"
    adapter = storage(FakeGitHub())
    assert adapter.find_record_by_id(record_id) is None

    fake = FakeGitHub()
    fake.seed(
        {
            f"records/{kind}/2026/09/{record_id}.md": b"not fetched"
            for kind in ("capture", "commitment", "decision")
        }
    )
    with pytest.raises(GitHubStorageError) as bounded:
        storage(fake).find_record_by_id(record_id, max_candidates=2)

    assert bounded.value.code == "github_record_candidate_limit"
    assert bounded.value.details == {"candidates": 3, "max_candidates": 2}
    assert not any(event.startswith("GET_BLOB") for event in fake.events)

    with pytest.raises(GitHubStorageError) as invalid_id:
        adapter.find_record_by_id("not-a-record-id")
    assert invalid_id.value.code == "github_record_id_invalid"
    for value in (0, True, github_storage.MAX_RECORD_CANDIDATES + 1):
        with pytest.raises(ValueError, match="max_candidates"):
            adapter.find_record_by_id(record_id, max_candidates=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("corrupt", [b"\xff", b"not a record"])
def test_record_id_lookup_rejects_corrupt_exact_candidates(corrupt: bytes) -> None:
    record_id = "brn_20260921_040000_e004"
    path = f"records/entity/2026/09/{record_id}.md"
    fake = FakeGitHub()
    fake.seed({path: corrupt, "records/capture/2026/09/brn_20260921_040000_eeee.md": b"ignored"})

    with pytest.raises(GitHubStorageError) as invalid:
        storage(fake).find_record_by_id(record_id)

    assert invalid.value.code == "github_record_corrupt"
    assert invalid.value.details == {"path": path}


def test_record_id_lookup_rejects_duplicate_valid_ids() -> None:
    capture = standalone_record("capture", "e005")
    commitment = standalone_record("commitment", "e005")
    capture_record = parse_record(capture.decode("utf-8"), "duplicate capture")
    commitment_record = parse_record(commitment.decode("utf-8"), "duplicate commitment")
    assert capture_record.id == commitment_record.id
    capture_path = f"records/capture/{capture_record.created_at:%Y/%m}/{capture_record.id}.md"
    commitment_path = f"records/commitment/{commitment_record.created_at:%Y/%m}/{commitment_record.id}.md"
    fake = FakeGitHub()
    fake.seed({capture_path: capture, commitment_path: commitment})

    with pytest.raises(GitHubConflict) as duplicate:
        storage(fake).find_record_by_id(capture_record.id)

    assert duplicate.value.code == "github_record_duplicate"
    assert duplicate.value.details["paths"] == sorted((capture_path, commitment_path))


def test_preflights_preserve_absent_pinned_revisions() -> None:
    fake = FakeGitHub()
    adapter = storage(fake)
    expected_revision = fake.branch

    decision = adapter.preflight_decision_by_semantic_key("a" * 64)
    record = adapter.preflight_record_by_id("brn_20260921_040000_e006")

    assert decision.revision.commit_sha == expected_revision
    assert decision.semantic_key == "a" * 64
    assert decision.match is None
    assert record.revision.commit_sha == expected_revision
    assert record.record_id == "brn_20260921_040000_e006"
    assert record.match is None


def test_memory_operation_two_state_replay_precedes_timestamped_materialization() -> None:
    operation_id = "desktop-replay-check"
    input_sha256 = hashlib.sha256(b"exact pre-materialization request").hexdigest()
    fake = FakeGitHub()
    first_adapter = storage(fake)

    absent = first_adapter.preflight_memory_operation(
        operation_id=operation_id,
        input_sha256=input_sha256,
    )
    assert absent.dedupe_key == f"mcp-memory-operation:{operation_id}:{input_sha256}"
    suffix = github_storage.memory_operation_record_suffix(operation_id)
    first_record = memory_operation_record("capture", suffix, absent.dedupe_key)
    replay_record = memory_operation_record(
        "capture",
        suffix,
        absent.dedupe_key,
        timestamp="2026-09-21T05:00:00+00:00",
    )
    first_id = parse_record(first_record.decode("utf-8"), "first materialization").id
    replay_id = parse_record(replay_record.decode("utf-8"), "replay materialization").id

    assert absent.match is None
    assert first_id != replay_id
    created = first_adapter.publish_record(
        record_bytes=first_record,
        expected_revision=absent.revision.commit_sha,
    )

    restarted_adapter = storage(fake)
    existing = restarted_adapter.preflight_memory_operation(
        operation_id=operation_id,
        input_sha256=input_sha256,
    )

    assert existing.match is not None
    assert existing.match.record.id == created.record_id == first_id
    assert existing.match.content == first_record
    assert existing.match.content != replay_record
    assert existing.match.path == created.record_path
    assert existing.revision.commit_sha == created.commit_sha

    changed_input = hashlib.sha256(b"changed pre-materialization request").hexdigest()
    with pytest.raises(GitHubConflict) as conflict:
        restarted_adapter.preflight_memory_operation(
            operation_id=operation_id,
            input_sha256=changed_input,
        )
    assert conflict.value.code == "github_memory_operation_identity_conflict"


def test_memory_operation_absence_race_conflicts_before_any_publication() -> None:
    operation_id = "desktop-race-check"
    input_sha256 = hashlib.sha256(b"exact race input").hexdigest()
    fake = FakeGitHub()
    adapter = storage(fake)
    absent = adapter.preflight_memory_operation(
        operation_id=operation_id,
        input_sha256=input_sha256,
    )
    assert absent.match is None
    record = memory_operation_record("commitment", "e008", absent.dedupe_key)
    external = fake.prepare_advance({"external.txt": b"concurrent"})
    fake.advance_after_commit_lookup = external
    fake.events.clear()

    with pytest.raises(GitHubConflict) as conflict:
        adapter.publish_record(
            record_bytes=record,
            expected_revision=absent.revision.commit_sha,
        )

    assert conflict.value.code == "github_revision_conflict"
    assert conflict.value.details == {"expected": absent.revision.commit_sha, "actual": external}
    assert fake.branch == external
    assert any(event.startswith("GET_COMMITS") for event in fake.events)
    assert not any(event.startswith(("POST_", "PATCH_")) for event in fake.events)


def test_memory_operation_preflight_is_bounded_and_rejects_corruption_and_duplicates() -> None:
    operation_id = "desktop-bounded-check"
    input_sha256 = hashlib.sha256(b"bounded input").hexdigest()
    dedupe_key = github_storage.memory_operation_dedupe_key(operation_id, input_sha256)
    suffix = github_storage.memory_operation_record_suffix(operation_id)

    bounded = FakeGitHub()
    bounded.seed(
        {
            f"records/{kind}/2026/09/brn_20260921_04000{index}_{suffix}.md": b"not fetched"
            for index, kind in enumerate(("capture", "commitment", "preference"))
        }
    )
    with pytest.raises(GitHubStorageError) as limit:
        storage(bounded).preflight_memory_operation(
            operation_id=operation_id,
            input_sha256=input_sha256,
            max_candidates=2,
        )
    assert limit.value.code == "github_memory_operation_candidate_limit"
    assert not any(event.startswith("GET_BLOB") for event in bounded.events)

    corrupt = FakeGitHub()
    corrupt_path = f"records/capture/2026/09/brn_20260921_040000_{suffix}.md"
    corrupt.seed({corrupt_path: b"not a record"})
    with pytest.raises(GitHubStorageError) as invalid:
        storage(corrupt).preflight_memory_operation(
            operation_id=operation_id,
            input_sha256=input_sha256,
        )
    assert invalid.value.code == "github_memory_operation_corrupt"

    malformed = FakeGitHub()
    malformed_record = memory_operation_record(
        "capture",
        suffix,
        "mcp-memory-operation:malformed",
    )
    malformed.seed(
        {f"records/capture/2026/09/brn_20260921_040000_{suffix}.md": malformed_record}
    )
    with pytest.raises(GitHubStorageError) as malformed_binding:
        storage(malformed).preflight_memory_operation(
            operation_id=operation_id,
            input_sha256=input_sha256,
        )
    assert malformed_binding.value.code == "github_memory_operation_corrupt"

    duplicate = FakeGitHub()
    first = memory_operation_record("capture", suffix, dedupe_key)
    second = memory_operation_record(
        "preference",
        suffix,
        dedupe_key,
        timestamp="2026-09-21T05:00:00+00:00",
    )
    duplicate.seed(
        {
            f"records/capture/2026/09/brn_20260921_040000_{suffix}.md": first,
            f"records/preference/2026/09/brn_20260921_050000_{suffix}.md": second,
        }
    )
    with pytest.raises(GitHubConflict) as duplicates:
        storage(duplicate).preflight_memory_operation(
            operation_id=operation_id,
            input_sha256=input_sha256,
        )
    assert duplicates.value.code == "github_memory_operation_duplicate"

    adapter = storage(FakeGitHub())
    with pytest.raises(GitHubStorageError) as invalid_operation:
        adapter.preflight_memory_operation(operation_id="BAD", input_sha256=input_sha256)
    assert invalid_operation.value.code == "github_memory_operation_id_invalid"
    with pytest.raises(GitHubStorageError) as invalid_input:
        adapter.preflight_memory_operation(operation_id=operation_id, input_sha256="bad")
    assert invalid_input.value.code == "github_memory_operation_input_invalid"


def test_memory_preflight_reads_only_matching_records() -> None:
    operation_id = "desktop-scale-check"
    input_sha256 = hashlib.sha256(b"scale check input").hexdigest()
    suffix = github_storage.memory_operation_record_suffix(operation_id)
    dedupe_key = github_storage.memory_operation_dedupe_key(operation_id, input_sha256)

    fake = FakeGitHub()
    unrelated = {
        f"records/capture/2026/09/brn_20260921_040000_{index:04x}.md": b"unrelated"
        for index in range(300)
    }
    matching = memory_operation_record("commitment", suffix, dedupe_key)
    fake.seed({**unrelated, f"records/commitment/2026/09/brn_20260921_040000_{suffix}.md": matching})
    fake.events.clear()

    result = storage(fake).preflight_memory_operation(
        operation_id=operation_id,
        input_sha256=input_sha256,
    )

    assert result.match is not None
    assert result.match.content == matching
    assert len([event for event in fake.events if event.startswith("GET_TREE")]) == 1
    assert len([event for event in fake.events if event.startswith("GET_BLOB")]) <= 1


def test_memory_preflight_ignores_unrelated_records_past_limit() -> None:
    operation_id = "desktop-scale-check-2"
    input_sha256 = hashlib.sha256(b"scale check input 2").hexdigest()

    fake = FakeGitHub()
    unrelated = {
        f"records/capture/2026/09/brn_20260921_{index:06d}_{index:04x}.md": b"unrelated"
        for index in range(1100)
    }
    fake.seed(unrelated)

    result = storage(fake).preflight_memory_operation(
        operation_id=operation_id,
        input_sha256=input_sha256,
    )

    assert result.match is None


# --- P3 / finding 15: operation IDs scoped by the authenticated caller ---------------


def test_memory_operation_record_suffix_is_scoped_by_caller() -> None:
    operation_id = "shared-op-id"
    legacy = github_storage.memory_operation_record_suffix(operation_id)
    nic_suffix = github_storage.memory_operation_record_suffix(operation_id, caller="nic")
    damien_suffix = github_storage.memory_operation_record_suffix(operation_id, caller="damien")

    assert nic_suffix != legacy
    assert damien_suffix != legacy
    assert nic_suffix != damien_suffix
    assert github_storage.memory_operation_record_suffix(operation_id, caller="nic") == nic_suffix  # deterministic

    with pytest.raises(GitHubStorageError) as invalid:
        github_storage.memory_operation_record_suffix(operation_id, caller="BAD CALLER")
    assert invalid.value.code == "github_memory_operation_id_invalid"


def test_preflight_memory_operation_two_callers_with_the_same_operation_id_do_not_collide() -> None:
    """Finding 15: agents mint operation ids from the words, so two different agents can
    land on the same id. Scoping by caller means each still gets its own record, and a
    replay by either finds only its own (docs/product/2026-09-26-gtbc-speed-goal.md P3)."""

    operation_id = "shared-reminder"
    nic_input = hashlib.sha256(b"nic's exact input").hexdigest()
    damien_input = hashlib.sha256(b"damien's exact input").hexdigest()
    nic_dedupe_key = github_storage.memory_operation_dedupe_key(operation_id, nic_input)
    damien_dedupe_key = github_storage.memory_operation_dedupe_key(operation_id, damien_input)
    nic_suffix = github_storage.memory_operation_record_suffix(operation_id, caller="nic")
    damien_suffix = github_storage.memory_operation_record_suffix(operation_id, caller="damien")

    fake = FakeGitHub()
    adapter = storage(fake)

    nic_preflight = adapter.preflight_memory_operation(operation_id=operation_id, input_sha256=nic_input, caller="nic")
    assert nic_preflight.match is None
    nic_record = scoped_item_record(nic_suffix, nic_dedupe_key, "nic")
    adapter.publish_record(record_bytes=nic_record, expected_revision=nic_preflight.revision.commit_sha)

    # Diablo's scoped search must not see nic's record, even though it is the same
    # operation_id: the suffixes differ, so the candidate scan never reads it.
    damien_preflight = adapter.preflight_memory_operation(
        operation_id=operation_id, input_sha256=damien_input, caller="damien"
    )
    assert damien_preflight.match is None
    damien_record = scoped_item_record(damien_suffix, damien_dedupe_key, "damien")
    adapter.publish_record(record_bytes=damien_record, expected_revision=damien_preflight.revision.commit_sha)

    nic_id = parse_record(nic_record.decode("utf-8"), "nic fixture").id
    damien_id = parse_record(damien_record.decode("utf-8"), "damien fixture").id
    assert nic_id != damien_id

    nic_replay = adapter.preflight_memory_operation(operation_id=operation_id, input_sha256=nic_input, caller="nic")
    assert nic_replay.match is not None
    assert nic_replay.match.record.id == nic_id

    damien_replay = adapter.preflight_memory_operation(
        operation_id=operation_id, input_sha256=damien_input, caller="damien"
    )
    assert damien_replay.match is not None
    assert damien_replay.match.record.id == damien_id


def test_preflight_memory_operation_legacy_suffix_record_found_only_by_its_own_author() -> None:
    """A record saved before caller scoping was installed still carries the legacy,
    unscoped suffix. Its own author's replay still finds it; anyone else's identically-
    named operation_id silently does not see it (skipped, not a conflict)."""

    operation_id = "pre-release-reminder"
    input_sha256 = hashlib.sha256(b"pre-release exact input").hexdigest()
    dedupe_key = github_storage.memory_operation_dedupe_key(operation_id, input_sha256)
    legacy_suffix = github_storage.memory_operation_record_suffix(operation_id)

    fake = FakeGitHub()
    legacy_record = scoped_item_record(legacy_suffix, dedupe_key, "nic")
    fake.seed({f"records/item/2026/09/brn_20260921_040000_{legacy_suffix}.md": legacy_record})
    adapter = storage(fake)

    by_its_author = adapter.preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256, caller="nic")
    assert by_its_author.match is not None
    assert by_its_author.match.content == legacy_record

    by_someone_else = adapter.preflight_memory_operation(
        operation_id=operation_id, input_sha256=input_sha256, caller="damien"
    )
    assert by_someone_else.match is None

    # caller=None (the legacy, unscoped call) still finds it too - exactly as before.
    unscoped = adapter.preflight_memory_operation(operation_id=operation_id, input_sha256=input_sha256)
    assert unscoped.match is not None
    assert unscoped.match.content == legacy_record


def test_semantic_decision_absence_revision_conditions_publication(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    semantic_key = str(parse_record(record.decode("utf-8"), "decision preflight").dedupe_key)
    fake = FakeGitHub()
    adapter = storage(fake)
    absent = adapter.preflight_decision_by_semantic_key(semantic_key)
    assert absent.match is None
    external = fake.prepare_advance({"external.txt": b"concurrent"})
    fake.advance_after_commit_lookup = external
    fake.events.clear()

    with pytest.raises(GitHubConflict) as conflict:
        adapter.publish(
            record_bytes=record,
            receipt_bytes=receipt,
            expected_revision=absent.revision.commit_sha,
        )

    assert conflict.value.code == "github_revision_conflict"
    assert fake.branch == external
    assert not any(event.startswith(("POST_", "PATCH_")) for event in fake.events)


def test_expected_revision_and_concurrent_ref_update_conflicts(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    fake = FakeGitHub()
    adapter = storage(fake)
    old = adapter.revision().commit_sha
    fake.seed({"external.txt": b"advanced"})
    with pytest.raises(GitHubConflict) as stale:
        adapter.publish(record_bytes=record, receipt_bytes=receipt, expected_revision=old)
    assert stale.value.code == "github_revision_conflict"
    assert not any(event.startswith("POST_") for event in fake.events)

    racing = FakeGitHub()
    racing.conflict_on_patch = True
    with pytest.raises(GitHubConflict) as conflict:
        storage(racing).publish(record_bytes=record, receipt_bytes=receipt)
    assert conflict.value.code == "github_conflict"
    assert len(racing.patch_payloads) == 1


def test_lost_accepted_reply_reconciles_before_any_retry(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    fake = FakeGitHub()
    fake.lost_apply_once = True

    result = storage(fake).publish(record_bytes=record, receipt_bytes=receipt)

    assert result.status == "reconciled" and result.remote_accepted is True
    patch_events = [event for event in fake.events if event.startswith("PATCH_")]
    assert len(patch_events) == 1
    lost_index = next(index for index, event in enumerate(fake.events) if event.startswith("PATCH_LOST_ACCEPTED"))
    lookup_index = next(
        index
        for index, event in enumerate(fake.events)
        if index > lost_index and event.startswith("GET_COMMITS")
    )
    assert lost_index < lookup_index


def test_retry_after_lost_unaccepted_reply_reuses_same_candidate_after_lookup(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    fake = FakeGitHub()
    fake.lost_reject_count = 1

    result = storage(fake).publish(record_bytes=record, receipt_bytes=receipt)

    assert result.status == "created"
    assert len(fake.patch_payloads) == 2
    assert fake.patch_payloads[0] == fake.patch_payloads[1]
    first = next(index for index, event in enumerate(fake.events) if event.startswith("PATCH_LOST_NOT_ACCEPTED"))
    lookup = next(index for index, event in enumerate(fake.events) if event.startswith("GET_COMMITS") and index > first)
    retry = next(index for index, event in enumerate(fake.events) if event.startswith("PATCH_OK"))
    assert first < lookup < retry
    assert len([event for event in fake.events if event.startswith("POST_COMMIT")]) == 1


def test_stable_origin_cannot_be_reused_for_different_exact_input(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path / "one")
    other_record, other_receipt = writer_pair(
        tmp_path / "two",
        choice="Use a different publication decision",
    )
    fake = FakeGitHub()
    adapter = storage(fake)
    adapter.publish(record_bytes=record, receipt_bytes=receipt)

    with pytest.raises(GitHubConflict) as conflict:
        adapter.publish(record_bytes=other_record, receipt_bytes=other_receipt)

    assert conflict.value.code == "github_publication_identity_conflict"
    assert len(fake.patch_payloads) == 1


def test_repository_branch_and_token_are_explicit_host_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-token-must-not-be-used")
    fake = FakeGitHub()
    adapter = storage(fake)

    revision = adapter.revision()

    assert revision.repository == REPOSITORY
    assert revision.branch == BRANCH
    with pytest.raises(ValueError, match="token"):
        GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token="", transport=fake)


def test_wrapped_base64_is_accepted_and_blob_hash_is_recomputed() -> None:
    fake = FakeGitHub()
    fake.seed({"records/value.md": b"wrapped base64 fixture"})
    fake.wrap_base64 = True
    assert storage(fake).read(("records/value.md",)).file("records/value.md").content == b"wrapped base64 fixture"

    fake.tamper_blob = True
    with pytest.raises(GitHubStorageError) as mismatch:
        storage(fake).read(("records/value.md",))
    assert mismatch.value.code == "github_blob_hash_mismatch"


@pytest.mark.parametrize(("mode", "kind"), [("120000", "blob"), ("160000", "commit")])
def test_symlink_and_gitlink_tree_entries_are_rejected(mode: str, kind: str) -> None:
    fake = FakeGitHub()
    fake.seed({"records/value.md": b"value"})
    fake.extra_tree_entry = {
        "path": "unsafe-entry",
        "mode": mode,
        "type": kind,
        "sha": "f" * 40,
    }

    with pytest.raises(GitHubStorageError) as unsafe:
        storage(fake).read(("records/value.md",))

    assert unsafe.value.code == "github_tree_unsafe"


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_urllib_transport_denies_redirect_before_forwarding_authorization() -> None:
    destination_authorization: list[str | None] = []

    class Destination(_QuietHandler):
        def do_GET(self) -> None:
            destination_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    destination, destination_thread = _serve(Destination)
    destination_url = f"http://127.0.0.1:{destination.server_port}/credential-target"

    class Redirect(_QuietHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", destination_url)
            self.end_headers()

    source, source_thread = _serve(Redirect)
    try:
        response = UrllibHTTPTransport().request(
            "GET",
            f"http://127.0.0.1:{source.server_port}/redirect",
            headers={"Authorization": "Bearer must-not-forward"},
            body=None,
        )
        assert response.status == 302
        assert destination_authorization == []
    finally:
        source.shutdown()
        destination.shutdown()
        source_thread.join(timeout=5)
        destination_thread.join(timeout=5)


def test_urllib_transport_caps_response_body_reads() -> None:
    class Oversized(_QuietHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 17)

    server, thread = _serve(Oversized)
    try:
        with pytest.raises(GitHubTransportError) as too_large:
            UrllibHTTPTransport(max_response_bytes=16).request(
                "GET",
                f"http://127.0.0.1:{server.server_port}/oversized",
                headers={},
                body=None,
            )
        assert too_large.value.code == "github_response_too_large"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def standalone_record(kind: str, suffix: str) -> bytes:
    data: dict[str, Any]
    state: str
    if kind == "capture":
        state = "inbox"
        data = {
            "channel": "mcp",
            "original_source_hash": hashlib.sha256(f"source-{suffix}".encode()).hexdigest(),
            "route": None,
            "duplicate_of": None,
        }
    elif kind == "commitment":
        state = "proposed"
        data = {"owner": "nic", "action": "Review fixture", "time": {}, "blocked_by": [], "decision_ids": []}
    elif kind == "preference":
        state = "active"
        data = {"scope": "fixture", "stability": "durable", "evidence": ["fixture"], "correction_history": []}
    else:  # pragma: no cover - test helper contract
        raise AssertionError(kind)
    record = record_from_mapping(
        {
            "schema": "brain.record/v1",
            "id": f"brn_20260921_040000_{suffix}",
            "kind": kind,
            "title": f"Fixture {kind}",
            "state": state,
            "created_at": "2026-09-21T04:00:00+00:00",
            "updated_at": "2026-09-21T04:00:00+00:00",
            "assertion": "explicit",
            "confidence": 1.0,
            "sensitivity": "private",
            "vaults": ["vault://brain-home"],
            "owners": ["nic"],
            "entities": [],
            "evidence": [
                {
                    "source_type": "fixture",
                    "locator": f"fixture:{kind}",
                    "content_sha256": hashlib.sha256(kind.encode()).hexdigest(),
                    "excerpt": kind,
                }
            ],
            "supersedes": [],
            "dedupe_key": f"fixture:{kind}:{suffix}",
            "data": data,
            "body": "",
        }
    )
    return render_record(record).encode("utf-8")


def memory_operation_record(
    kind: str,
    suffix: str,
    dedupe_key: str,
    *,
    timestamp: str = "2026-09-21T04:00:00+00:00",
) -> bytes:
    original = parse_record(standalone_record(kind, suffix).decode("utf-8"), "memory operation fixture")
    value = original.as_dict()
    compact = timestamp.replace("-", "").replace(":", "").replace("T", "_").split("+")[0]
    value["id"] = f"brn_{compact}_{suffix}"
    value["created_at"] = timestamp
    value["updated_at"] = timestamp
    value["dedupe_key"] = dedupe_key
    return render_record(record_from_mapping(value, "memory operation fixture")).encode("utf-8")


def scoped_item_record(
    suffix: str,
    dedupe_key: str,
    recorded_by: str,
    *,
    timestamp: str = "2026-09-21T04:00:00+00:00",
) -> bytes:
    """A v2 item record carrying ``source.recorded_by``, for caller-scoped preflight
    tests (P3, finding 15). ``suffix`` may be either the legacy, unscoped record-ID
    suffix or a caller-scoped one - the caller decides which by what it passes in.
    """

    compact = timestamp.replace("-", "").replace(":", "").replace("T", "_").split("+")[0]
    record = record_from_mapping(
        {
            "schema": "brain.record/v2",
            "id": f"brn_{compact}_{suffix}",
            "kind": "item",
            "title": "Fixture item",
            "state": "open",
            "created_at": timestamp,
            "updated_at": timestamp,
            "sensitivity": "private",
            "vaults": ["vault://brain-home"],
            "owners": ["nic"],
            "entities": [],
            "evidence": [
                {
                    "source_type": "fixture",
                    "locator": "fixture:item",
                    "content_sha256": hashlib.sha256(b"item").hexdigest(),
                    "excerpt": "fixture words",
                }
            ],
            "supersedes": [],
            "dedupe_key": dedupe_key,
            "data": {"level": "accepted", "size": "task"},
            "body": "",
            "source": {
                "said_by": "owner",
                "came_by": "direct",
                "words": "fixture words",
                "recorded_by": recorded_by,
            },
        },
        "scoped memory operation fixture",
    )
    return render_record(record).encode("utf-8")


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [("capture", "c001"), ("commitment", "c002"), ("preference", "c003")],
)
def test_existing_schema_record_publication_and_coherent_read(kind: str, suffix: str) -> None:
    content = standalone_record(kind, suffix)
    fake = FakeGitHub()
    adapter = storage(fake)

    created = adapter.publish_record(record_bytes=content)
    duplicate = adapter.publish_record(record_bytes=content)
    observed = adapter.read_records((created.record_path,))

    assert created.status == "created" and created.record_kind == kind
    assert duplicate.status == "duplicate/no-op" and duplicate.commit_sha == created.commit_sha
    assert observed.revision.commit_sha == created.commit_sha
    assert observed.records[0].record.kind == kind
    assert observed.records[0].content == content


def test_decision_record_requires_atomic_writer_receipt_publication(tmp_path: Path) -> None:
    record, _ = writer_pair(tmp_path)

    with pytest.raises(GitHubStorageError) as refused:
        storage(FakeGitHub()).publish_record(record_bytes=record)

    assert refused.value.code == "github_record_kind_requires_owner_route"


def test_low_level_contract_validation_and_host_configuration() -> None:
    with pytest.raises(KeyError):
        github_storage.GitHubRead(
            github_storage.GitHubRevision(REPOSITORY, BRANCH, "a" * 40, "b" * 40),
            (),
        ).file("missing")
    for value in (None, "bad"):
        with pytest.raises(GitHubStorageError):
            github_storage._git_object(value, "fixture")
    with pytest.raises(GitHubStorageError):
        github_storage._mapping([], "fixture")
    for path in ("", "/root", "a\\b", "a/../b", "a//b"):
        with pytest.raises(GitHubStorageError):
            github_storage._safe_path(path, "fixture")
    assert len(github_storage._git_blob_sha(b"fixture", 64)) == 64
    with pytest.raises(GitHubStorageError):
        github_storage._git_blob_sha(b"fixture", 32)

    invalid_configs = [
        {"repository": "missing-slash", "branch": BRANCH, "token": TOKEN},
        {"repository": REPOSITORY, "branch": "bad branch", "token": TOKEN},
        {"repository": REPOSITORY, "branch": BRANCH, "token": " padded "},
        {"repository": REPOSITORY, "branch": BRANCH, "token": TOKEN, "api_base": "http://api.github.test"},
    ]
    for config in invalid_configs:
        with pytest.raises(ValueError):
            GitHubBrainHomeStorage(transport=FakeGitHub(), **config)
    for kwargs in ({"timeout_seconds": 0}, {"max_response_bytes": 0}):
        with pytest.raises(ValueError):
            UrllibHTTPTransport(**kwargs)


def test_urllib_transport_maps_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenOpener:
        def open(self, request: object, timeout: float) -> None:
            raise OSError("offline")

    transport = UrllibHTTPTransport()
    monkeypatch.setattr(transport, "_opener", BrokenOpener())
    with pytest.raises(GitHubTransportError) as unavailable:
        transport.request("GET", "https://api.github.test/value", headers={}, body=None)
    assert unavailable.value.code == "github_transport_unavailable"


def _receipt_bytes(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(value)) + "\n").encode("utf-8")


def test_decision_publication_rejects_non_writer_or_mismatched_inputs(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path)
    original = json.loads(receipt)
    invalid_receipts: list[bytes] = []
    for change in (
        {"schema": "other"},
        {"status": "failure", "refusal_code": "denied"},
        {"origin_id": "BAD"},
        {"semantic_key": "bad"},
        {"canonical_identity": "brn_20260921_040000_ffff"},
        {"canonical_locator": "vault://other/records/value.md"},
        {"canonical_locator": "vault://brain-home/records/decision/2026/09/wrong.md"},
        {"owner_id": "other"},
        {"network_calls": 1},
    ):
        invalid_receipts.append(_receipt_bytes({**original, **change}))
    without_field = dict(original)
    without_field.pop("writer_id")
    invalid_receipts.append(_receipt_bytes(without_field))
    invalid_receipts.append(json.dumps(original, indent=2).encode("utf-8"))

    for invalid in invalid_receipts:
        with pytest.raises(GitHubStorageError) as refused:
            storage(FakeGitHub()).publish(record_bytes=record, receipt_bytes=invalid)
        assert refused.value.code == "github_publication_invalid"
    with pytest.raises(TypeError):
        github_storage.GitHubBrainHomeStorage._validate_publication("record", receipt)  # type: ignore[arg-type]
    with pytest.raises(GitHubStorageError):
        github_storage.GitHubBrainHomeStorage._validate_publication(b"\xff", receipt)
    with pytest.raises(GitHubStorageError):
        github_storage.GitHubBrainHomeStorage._validate_publication(record, b"not-json")


def test_decision_lost_reply_uncertainty_and_head_change_are_explicit(tmp_path: Path) -> None:
    record, receipt = writer_pair(tmp_path / "one")
    no_retry = FakeGitHub()
    no_retry.lost_reject_count = 1
    with pytest.raises(github_storage.GitHubPublicationUncertain):
        storage(no_retry).publish(
            record_bytes=record,
            receipt_bytes=receipt,
            retry_after_lost_reply=False,
        )

    record_two, receipt_two = writer_pair(tmp_path / "two", origin_id="github-origin-two")
    lost_twice = FakeGitHub()
    lost_twice.lost_reject_count = 2
    with pytest.raises(github_storage.GitHubPublicationUncertain):
        storage(lost_twice).publish(record_bytes=record_two, receipt_bytes=receipt_two)
    assert len(lost_twice.patch_payloads) == 2

    record_three, receipt_three = writer_pair(tmp_path / "three", origin_id="github-origin-three")
    moved = FakeGitHub()
    external = moved.prepare_advance({"external.txt": b"moved"})
    moved.move_after_patch = external
    with pytest.raises(GitHubConflict) as changed:
        storage(moved).publish(record_bytes=record_three, receipt_bytes=receipt_three)
    assert changed.value.code == "github_revision_conflict"


def v2_standalone_record(kind: str, suffix: str) -> bytes:
    data: dict[str, Any] = {"level": "possible", "size": "task"} if kind == "item" else {"observed_at": "2026-09-21T04:00:00+00:00"}
    record = record_from_mapping(
        {
            "schema": "brain.record/v2",
            "id": f"brn_20260921_040000_{suffix}",
            "kind": kind,
            "title": f"Fixture {kind}",
            "state": "open" if kind == "item" else "current",
            "created_at": "2026-09-21T04:00:00+00:00",
            "updated_at": "2026-09-21T04:00:00+00:00",
            "sensitivity": "private",
            "vaults": ["vault://brain-home"],
            "owners": ["nic"],
            "entities": [],
            "evidence": [
                {
                    "source_type": "fixture",
                    "locator": f"fixture:{kind}",
                    "content_sha256": hashlib.sha256(kind.encode()).hexdigest(),
                    "excerpt": "owner words",
                }
            ],
            "supersedes": [],
            "dedupe_key": f"fixture:{kind}:{suffix}",
            "data": data,
            "body": "",
            "source": {"said_by": "owner", "came_by": "direct", "words": "owner words", "recorded_by": "chatgpt-web"},
        },
        kind,
    )
    return render_record(record).encode("utf-8")


def test_standalone_record_accepts_v2_item_and_fact_kinds() -> None:
    for kind, suffix in (("item", "e001"), ("fact", "e002")):
        content = v2_standalone_record(kind, suffix)
        result = storage(FakeGitHub()).publish_record(record_bytes=content)
        assert result.status == "created"
        assert result.record_kind == kind
        assert result.record_path.startswith(f"records/{kind}/")


def test_snapshot_records_reads_v2_item_and_fact_kinds() -> None:
    item_path = "records/item/2026/09/brn_20260921_040000_e001.md"
    fact_path = "records/fact/2026/09/brn_20260921_040000_e002.md"
    fake = FakeGitHub()
    fake.seed({
        item_path: v2_standalone_record("item", "e001"),
        fact_path: v2_standalone_record("fact", "e002"),
    })
    result = storage(fake).snapshot_records(kinds=frozenset({"item", "fact"}))
    kinds = {record.record.kind for record in result.records}
    assert kinds == {"item", "fact"}


def test_snapshot_records_still_rejects_unsupported_kinds() -> None:
    with pytest.raises(ValueError):
        storage(FakeGitHub()).snapshot_records(kinds=frozenset({"entity"}))


def test_snapshot_records_min_created_at_excludes_older_paths_without_reading_their_blobs() -> None:
    """ROA-11's near-duplicate window (Wave 3): a path older than the cutoff is
    dropped before any blob is fetched, so a cold scan reads only what is left."""

    old_path = "records/item/2026/09/brn_20260920_040000_aaaa1111.md"
    new_path = "records/item/2026/09/brn_20260926_040000_bbbb2222.md"
    fake = FakeGitHub()
    fake.seed({
        old_path: scoped_item_record("aaaa1111", "dedupe-old", "nic", timestamp="2026-09-20T04:00:00+00:00"),
        new_path: scoped_item_record("bbbb2222", "dedupe-new", "nic", timestamp="2026-09-26T04:00:00+00:00"),
    })
    cutoff = datetime.fromisoformat("2026-09-25T00:00:00+00:00")

    fake.events.clear()
    result = storage(fake).snapshot_records(kinds=frozenset({"item"}), min_created_at=cutoff)

    assert [record.record.id for record in result.records] == ["brn_20260926_040000_bbbb2222"]
    assert len([event for event in fake.events if event.startswith("GET_BLOB")]) == 1


def test_snapshot_records_min_created_at_keeps_a_record_exactly_at_the_cutoff() -> None:
    at_cutoff_path = "records/item/2026/09/brn_20260925_000000_cccc3333.md"
    fake = FakeGitHub()
    fake.seed({
        at_cutoff_path: scoped_item_record("cccc3333", "dedupe-cutoff", "nic", timestamp="2026-09-25T00:00:00+00:00"),
    })
    cutoff = datetime.fromisoformat("2026-09-25T00:00:00+00:00")

    result = storage(fake).snapshot_records(kinds=frozenset({"item"}), min_created_at=cutoff)

    assert [record.record.id for record in result.records] == ["brn_20260925_000000_cccc3333"]


def test_snapshot_records_min_created_at_rejects_a_naive_datetime() -> None:
    with pytest.raises(ValueError):
        storage(FakeGitHub()).snapshot_records(kinds=frozenset({"item"}), min_created_at=datetime(2026, 9, 25))


def test_standalone_record_still_rejects_entity() -> None:
    record = record_from_mapping(
        {
            "schema": "brain.record/v1",
            "id": "brn_20260921_040000_e003",
            "kind": "entity",
            "title": "Fixture entity",
            "state": "active",
            "created_at": "2026-09-21T04:00:00+00:00",
            "updated_at": "2026-09-21T04:00:00+00:00",
            "assertion": "explicit",
            "confidence": 1.0,
            "sensitivity": "private",
            "vaults": ["vault://brain-home"],
            "owners": ["nic"],
            "entities": [],
            "evidence": [
                {
                    "source_type": "fixture",
                    "locator": "fixture:entity",
                    "content_sha256": hashlib.sha256(b"entity").hexdigest(),
                    "excerpt": "entity",
                }
            ],
            "supersedes": [],
            "dedupe_key": "fixture:entity:e003",
            "data": {"entity_type": "person", "why_it_matters": "fixture", "privacy": "private", "linked_events": ["decision:test"]},
            "body": "",
        },
        "entity",
    )
    content = render_record(record).encode("utf-8")
    with pytest.raises(GitHubStorageError) as raised:
        storage(FakeGitHub()).publish_record(record_bytes=content)
    assert raised.value.code == "github_record_kind_requires_owner_route"


def test_standalone_record_lost_accepted_reply_is_reconciled() -> None:
    fake = FakeGitHub()
    fake.lost_apply_once = True
    content = standalone_record("capture", "d001")

    result = storage(fake).publish_record(record_bytes=content)

    assert result.status == "reconciled"
    assert len(fake.patch_payloads) == 1


def test_standalone_record_type_utf8_revision_and_path_conflicts() -> None:
    adapter = storage(FakeGitHub())
    with pytest.raises(TypeError):
        adapter.publish_record(record_bytes="record")  # type: ignore[arg-type]
    with pytest.raises(GitHubStorageError):
        adapter.publish_record(record_bytes=b"\xff")

    content = standalone_record("capture", "d002")
    stale_fake = FakeGitHub()
    stale_adapter = storage(stale_fake)
    old = stale_adapter.revision().commit_sha
    stale_fake.seed({"external.txt": b"advanced"})
    with pytest.raises(GitHubConflict) as stale:
        stale_adapter.publish_record(record_bytes=content, expected_revision=old)
    assert stale.value.code == "github_revision_conflict"

    conflicting = FakeGitHub()
    record = github_storage.GitHubBrainHomeStorage._validate_standalone_record(content)
    conflicting.seed({record[1]: b"different"})
    with pytest.raises(GitHubConflict) as mismatch:
        storage(conflicting).publish_record(record_bytes=content)
    assert mismatch.value.code == "github_record_conflict"


# --- Brain Home's size, layer 1 (docs/product/2026-09-30-brain-home-storage-limit-plan.md §4) -------------


def _commitment_path(suffix: str) -> str:
    return f"records/commitment/2026/09/brn_20260921_040000_{suffix}.md"


def _seed_commitments(fake: FakeGitHub, count: int) -> dict[str, bytes]:
    files = {_commitment_path(f"{index:08x}"): standalone_record("commitment", f"{index:08x}") for index in range(count)}
    fake.seed(files)
    return files


def _cached(fake: FakeGitHub) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(
        repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=fake,
        object_cache=github_storage.GitHubObjectCache(),
    )


def test_a_scan_just_under_its_cap_reads_and_one_just_over_refuses_with_its_details() -> None:
    fake = FakeGitHub()
    _seed_commitments(fake, 3)
    kinds = frozenset({"commitment"})

    assert len(storage(fake).snapshot_records(kinds=kinds, max_records=3).records) == 3
    with pytest.raises(GitHubStorageError) as over:
        storage(fake).snapshot_records(kinds=kinds, max_records=2)

    assert over.value.code == "github_scan_limit"
    assert over.value.details == {"kinds": ["commitment"], "limit": "files", "files": 3, "max_records": 2}


def test_a_scan_past_its_byte_cap_says_the_bytes_it_counted() -> None:
    fake = FakeGitHub()
    files = _seed_commitments(fake, 2)
    size = sum(len(content) for content in files.values())

    assert len(storage(fake).snapshot_records(kinds=frozenset({"commitment"}), max_bytes=size).records) == 2
    with pytest.raises(GitHubStorageError) as over:
        storage(fake).snapshot_records(kinds=frozenset({"commitment"}), max_bytes=size - 1)

    details = over.value.details
    assert over.value.code == "github_scan_limit"
    assert (details["kinds"], details["limit"], details["files"], details["max_bytes"]) == (
        ["commitment"], "bytes", 2, size - 1,
    )
    assert details["bytes"] > size - 1


def test_a_scan_at_80_percent_of_a_cap_logs_kinds_and_counts_only(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeGitHub()
    _seed_commitments(fake, 4)
    kinds = frozenset({"commitment"})

    with caplog.at_level("WARNING", logger="brain_core.github_storage"):
        storage(fake).snapshot_records(kinds=kinds, max_records=6)  # 67%: quiet
    assert caplog.records == []

    with caplog.at_level("WARNING", logger="brain_core.github_storage"):
        storage(fake).snapshot_records(kinds=kinds, max_records=5)  # 80%
    [warning] = caplog.records
    assert warning.getMessage() == "Brain Home scan of commitment: 4 files, at or past 80% of its 5-file limit"
    assert "Fixture" not in caplog.text  # no titles or other content


def test_a_cap_above_the_defaults_needs_an_object_cache() -> None:
    fake = FakeGitHub()
    _seed_commitments(fake, 1)
    kinds = frozenset({"commitment"})

    with pytest.raises(ValueError, match="object cache"):
        storage(fake).snapshot_records(kinds=kinds, max_records=github_storage.SNAPSHOT_MAX_RECORDS + 1)
    with pytest.raises(ValueError, match="object cache"):
        storage(fake).snapshot_records(kinds=kinds, max_bytes=github_storage.SNAPSHOT_MAX_BYTES + 1)
    assert fake.events == []  # refused before any GitHub request

    assert len(_cached(fake).snapshot_records(kinds=kinds, max_records=5000, max_bytes=32 * 1024 * 1024).records) == 1


def test_read_records_with_archived_ok_reads_a_moved_file_from_the_archive_folder() -> None:
    fake = FakeGitHub()
    content = standalone_record("commitment", "a0000001")
    path = _commitment_path("a0000001")
    fake.seed({"archive/" + path: content})

    with pytest.raises(GitHubStorageError) as missing:
        storage(fake).read_records((path,))
    assert missing.value.code == "github_path_missing"

    [found] = storage(fake).read_records((path,), archived_ok=True).records
    assert (found.path, found.content, found.archived) == (path, content, True)
    assert found.record.id == "brn_20260921_040000_a0000001"

    # A current file at the path wins; it is not archived.
    current = FakeGitHub()
    current.seed({path: content, "archive/" + path: content})
    [live] = storage(current).read_records((path,), archived_ok=True).records
    assert live.archived is False


def test_a_path_in_neither_place_is_missing_and_names_the_path_asked_for() -> None:
    fake = FakeGitHub()
    fake.seed({"README.md": b"brain home"})
    path = _commitment_path("a0000002")

    with pytest.raises(GitHubStorageError) as missing:
        storage(fake).read_records((path,), archived_ok=True)

    assert missing.value.code == "github_path_missing"
    assert missing.value.details == {"path": path}


def test_an_archived_file_that_does_not_parse_is_skipped_but_a_current_one_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    good_path, bad_path = _commitment_path("a0000003"), _commitment_path("a0000004")
    fake = FakeGitHub()
    fake.seed({good_path: standalone_record("commitment", "a0000003"), "archive/" + bad_path: b"not a record"})

    with caplog.at_level("WARNING", logger="brain_core.github_storage"):
        found = storage(fake).read_records((good_path, bad_path), archived_ok=True).records
    assert [item.path for item in found] == [good_path]
    assert "archive/" + bad_path in caplog.text

    broken = FakeGitHub()
    broken.seed({bad_path: b"not a record"})
    with pytest.raises(Exception):
        storage(broken).read_records((bad_path,), archived_ok=True)


def test_preflight_by_id_can_include_the_archive_and_a_record_in_both_places_conflicts() -> None:
    content = standalone_record("commitment", "a0000005")
    path = _commitment_path("a0000005")
    record_id = "brn_20260921_040000_a0000005"
    fake = FakeGitHub()
    fake.seed({"archive/" + path: content})

    assert storage(fake).preflight_record_by_id(record_id).match is None
    match = storage(fake).preflight_record_by_id(record_id, include_archive=True).match
    assert match is not None and match.path == "archive/" + path and match.content == content
    assert storage(fake).find_record_by_id(record_id, include_archive=True).path == "archive/" + path

    both = FakeGitHub()
    both.seed({path: content, "archive/" + path: content})
    assert storage(both).preflight_record_by_id(record_id).match.path == path
    with pytest.raises(GitHubConflict) as duplicate:
        storage(both).preflight_record_by_id(record_id, include_archive=True)
    assert duplicate.value.code == "github_record_duplicate"


def test_record_counts_come_from_one_tree_read_and_no_file_read() -> None:
    fake = FakeGitHub()
    files = _seed_commitments(fake, 3)
    fake.seed({
        "records/item/2026/09/brn_20260921_040000_b0000001.md": b"item one",
        "records/.durability/receipts/origin.json": b"{}",
        "archive/records/commitment/2026/09/brn_20260921_040000_c0000001.md": b"old",
        "archive/records/item/2026/09/brn_20260921_040000_c0000002.md": b"old",
        "archive/records/item/2026/09/brn_20260921_040000_c0000003.md": b"old",
        "README.md": b"brain home",
    })
    fake.events.clear()

    counts = storage(fake).record_counts()

    assert [event.split()[0] for event in fake.events] == ["GET_REF", "GET_COMMIT", "GET_TREE"]
    assert counts.live_files == {"commitment": 3, "item": 1}
    assert counts.live_bytes == {"commitment": sum(len(value) for value in files.values()), "item": len(b"item one")}
    assert counts.archived_files == {"commitment": 1, "item": 2}
    assert (counts.receipts, counts.other_files, counts.tree_entries) == (1, 1, 9)
    assert counts.revision.commit_sha == fake.branch


def test_rate_limit_reads_the_hourly_allowance() -> None:
    fake = FakeGitHub()

    allowance = storage(fake).rate_limit()

    assert (allowance.limit, allowance.remaining, allowance.used, allowance.reset) == (5000, 4990, 10, 1790000000)
    fake.allowance = {"limit": 5000, "remaining": -1, "used": 10, "reset": 1}
    with pytest.raises(GitHubStorageError):
        storage(fake).rate_limit()


class _RefusingTransport:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers

    def request(self, method, url, *, headers, body):  # type: ignore[no-untyped-def]
        return HTTPResponse(self.status, self.headers, b'{"message": "refused"}')


@pytest.mark.parametrize(
    ("status", "headers", "details"),
    [
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790000000"}, {"rate_limited": True, "reset": 1790000000}),
        (429, {"retry-after": "60"}, {"rate_limited": True, "reset": None}),
        (403, {"x-ratelimit-remaining": "12"}, {}),
        (404, {"x-ratelimit-remaining": "0"}, {}),
    ],
)
def test_a_used_up_allowance_is_marked_on_the_error_without_changing_its_code(status, headers, details) -> None:
    adapter = GitHubBrainHomeStorage(
        repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=_RefusingTransport(status, headers),
    )

    with pytest.raises(GitHubStorageError) as refused:
        adapter.revision()

    assert refused.value.code == "github_api_error"
    assert refused.value.details == {"method": "GET", "status": status, **details}


def test_an_archived_candidate_that_does_not_parse_is_skipped_by_id_but_a_live_one_still_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _commitment_path("a0000006")
    record_id = "brn_20260921_040000_a0000006"
    fake = FakeGitHub()
    fake.seed({"archive/" + path: b"not a record"})

    with caplog.at_level("WARNING", logger="brain_core.github_storage"):
        assert storage(fake).preflight_record_by_id(record_id, include_archive=True).match is None
    assert "archive/" + path in caplog.text

    # A readable live copy is found even when a broken archived copy sits beside it.
    beside = FakeGitHub()
    beside.seed({path: standalone_record("commitment", "a0000006"), "archive/" + path: b"not a record"})
    assert storage(beside).preflight_record_by_id(record_id, include_archive=True).match.path == path

    broken = FakeGitHub()
    broken.seed({path: b"not a record"})
    with pytest.raises(GitHubStorageError) as corrupt:
        storage(broken).preflight_record_by_id(record_id, include_archive=True)
    assert corrupt.value.code == "github_record_corrupt"


def test_an_incomplete_tree_says_whether_github_truncated_it_or_sent_another() -> None:
    fake = FakeGitHub()
    fake.seed({"README.md": b"brain home"})

    class Truncated:
        def request(self, method, url, *, headers, body):  # type: ignore[no-untyped-def]
            response = fake.request(method, url, headers=headers, body=body)
            if "/git/trees/" in url:
                value = json.loads(response.body)
                value["truncated"] = True
                return HTTPResponse(200, {}, json.dumps(value).encode("utf-8"))
            return response

    class Different:
        def request(self, method, url, *, headers, body):  # type: ignore[no-untyped-def]
            response = fake.request(method, url, headers=headers, body=body)
            if "/git/trees/" in url:
                value = json.loads(response.body)
                value["sha"] = "f" * 40
                return HTTPResponse(200, {}, json.dumps(value).encode("utf-8"))
            return response

    for transport, details in (
        (Truncated(), {"truncated": True, "tree_matches": True}),
        (Different(), {"truncated": False, "tree_matches": False}),
    ):
        adapter = GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN, transport=transport)
        with pytest.raises(GitHubStorageError) as incomplete:
            adapter.record_counts()
        assert (incomplete.value.code, incomplete.value.details) == ("github_tree_incomplete", details)


def test_cached_complete_tree_reuses_sizes_but_each_unpinned_read_checks_head():
    from brain_core.github_storage import GitHubObjectCache
    fake = FakeGitHub()
    _seed_commitments(fake, 3)
    adapter = GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN,
                                    transport=fake, object_cache=GitHubObjectCache())
    first = adapter.record_counts()
    fake.events.clear()
    second = adapter.record_counts()
    assert second == first
    assert [event.split()[0] for event in fake.events] == ["GET_REF"]
    fake.events.clear()
    assert adapter.record_counts(first.revision) == first
    assert fake.events == []


def test_path_only_tree_cache_requires_listing_for_counts_and_returns_copies():
    from brain_core.github_storage import GitHubObjectCache
    fake = FakeGitHub()
    _seed_commitments(fake, 1)
    cache = GitHubObjectCache()
    adapter = GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN,
                                    transport=fake, object_cache=cache)
    revision = adapter.revision()
    paths = {path: sha for path,sha in fake.trees[revision.tree_sha].items()}
    cache.remember_tree(revision.tree_sha, paths)
    fake.events.clear()
    first = adapter.record_counts(revision)
    assert [event.split()[0] for event in fake.events] == ["GET_TREE"]
    fake.events.clear()
    assert adapter.record_counts(revision) == first
    assert fake.events == []


def test_request_observer_reports_only_fixed_categories_and_times():
    from brain_core.github_storage import observe_github_requests
    fake = FakeGitHub()
    observed = []
    adapter = storage(fake)
    with observe_github_requests(lambda category, seconds: observed.append((category, seconds))):
        adapter.record_counts()
    assert [category for category, _ in observed] == ["read_ref", "read_commit", "read_tree"]
    assert all(isinstance(seconds, float) and seconds >= 0 for _, seconds in observed)
    adapter.record_counts()
    assert len(observed) == 3


def test_cached_tree_metadata_is_copied_bounded_and_new_head_gets_new_tree():
    from brain_core.github_storage import GitHubObjectCache
    fake = FakeGitHub()
    _seed_commitments(fake, 1)
    cache = GitHubObjectCache(max_trees=1)
    adapter = GitHubBrainHomeStorage(repository=REPOSITORY, branch=BRANCH, token=TOKEN,
                                    transport=fake, object_cache=cache)
    first = adapter.record_counts()
    blobs,sizes,entries = cache.tree_listing(first.revision.tree_sha)
    blobs.clear();sizes.clear()
    assert adapter.record_counts(first.revision) == first
    fake.seed({"README.md":b"different"})
    fake.events.clear()
    second = adapter.record_counts()
    assert second.revision != first.revision
    assert [event.split()[0] for event in fake.events] == ["GET_REF","GET_COMMIT","GET_TREE"]
    assert cache.tree_listing(first.revision.tree_sha) is None


def test_cached_malformed_size_keeps_refusing_without_refetch():
    from brain_core.github_storage import GitHubObjectCache
    fake = FakeGitHub()
    _seed_commitments(fake, 1)
    class InvalidSize:
        def request(self, method, url, *, headers, body):
            response = fake.request(method,url,headers=headers,body=body)
            if "/git/trees/" in url:
                value = json.loads(response.body)
                value["tree"][0]["size"] = True
                return HTTPResponse(200,{},json.dumps(value).encode())
            return response
    adapter = GitHubBrainHomeStorage(repository=REPOSITORY,branch=BRANCH,token=TOKEN,
                                    transport=InvalidSize(),object_cache=GitHubObjectCache())
    with pytest.raises(GitHubStorageError,match="size is invalid"):
        adapter.record_counts()
    fake.events.clear()
    with pytest.raises(GitHubStorageError,match="size is invalid"):
        adapter.record_counts()
    assert [event.split()[0] for event in fake.events] == ["GET_REF"]


def test_transport_error_is_timed_and_observer_context_is_restored():
    from brain_core.github_storage import observe_github_requests
    class Failing:
        def request(self,*args,**kwargs):
            raise RuntimeError("fictional transport failure")
    adapter = GitHubBrainHomeStorage(repository=REPOSITORY,branch=BRANCH,token=TOKEN,transport=Failing())
    observed = []
    with observe_github_requests(lambda category,seconds: observed.append(category)):
        with pytest.raises(RuntimeError):
            adapter.revision()
    assert observed == ["read_ref"]
    with pytest.raises(RuntimeError):
        adapter.revision()
    assert observed == ["read_ref"]
