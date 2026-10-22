"""The hub's Brain Home copy: kept on disk, checked on load, and wired into the runtime."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import anyio
import pytest

from brain_core.github_storage import SNAPSHOT_MAX_RECORDS, GitHubStorageError
from brain_core.records import parse_record, record_from_mapping, render_record
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_home_copy import COPY_FILE, SQLiteBlobStore, open_copy_store
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation as Op,
    MemorySaveRequest,
    MemorySearchQuery,
    OwnerSelector,
)
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider
from test_brain_composition import _settings
from test_github_brain_provider import FIRST_TIME, LANE_B, _adapter, _authorization

LOGGER = "gremlin_town.mcp.brain_home_copy"


def _allow(policy_file: Path, *operations: str) -> None:
    policy = json.loads(policy_file.read_text(encoding="utf-8"))
    owner = policy["owners"][0]
    owner["operations"] = sorted(set(owner["operations"]) | set(operations))
    policy_file.write_text(json.dumps(policy), encoding="utf-8")


def _owned_commitments(count: int) -> dict[str, bytes]:
    files = {}
    for index in range(count):
        value = parse_record(LANE_B.standalone_record("commitment", f"{index:08x}").decode("utf-8")).as_dict()
        value["owners"] = ["brain-home"]
        value["vaults"] = ["vault://brain-home"]
        record = record_from_mapping(value, "owned commitment fixture")
        files[f"records/commitment/2026/09/{record.id}.md"] = render_record(record).encode("utf-8")
    return files


def test_the_copy_persists_across_opens(tmp_path: Path) -> None:
    path = tmp_path / "state" / COPY_FILE
    first = SQLiteBlobStore(path)
    first.store("a" * 40, b"saved record")
    first.close()

    second = SQLiteBlobStore(path)
    try:
        assert second.load("a" * 40) == b"saved record"
        assert second.load("b" * 40) is None
    finally:
        second.close()


def test_copy_failures_are_logged_and_never_raised(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = SQLiteBlobStore(tmp_path / COPY_FILE)
    store.close()

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert store.load("a" * 40) is None
        store.store("a" * 40, b"record")

    assert "Brain Home copy read failed" in caplog.text
    assert "Brain Home copy write failed" in caplog.text


def test_an_unopenable_copy_falls_back_to_github_and_memory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    not_a_directory = tmp_path / "state"
    not_a_directory.write_text("a file where the state directory should be", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert open_copy_store(not_a_directory) is None

    assert "Brain Home copy unavailable" in caplog.text


def test_the_runtime_keeps_its_copy_beside_provider_state_and_reuses_it_after_a_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _allow(settings.policy_file, "memory.search")
    remote = LANE_B.FakeGitHub()
    caller = BrainCaller("chatgpt-web", frozenset({"bc:read", "bc:write"}))
    selector = OwnerSelector("personal")
    query = MemorySearchQuery("reconcile")

    async def first_process() -> tuple[str | None, list[str | None]]:
        runtime = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: FIRST_TIME)
        saved = await runtime.save_memory(
            caller,
            selector,
            MemorySaveRequest(
                operation_id="copy-restart-1",
                kind="item",
                title="Reconcile before retrying",
                body="Always reconcile an accepted operation before retrying it.",
                source_reference="mcp-request:copy-restart-1",
                said_by="owner",
                came_by="direct",
                words="Always reconcile an accepted operation before retrying it.",
                level="accepted",
            ),
        )
        found = await runtime.search_memory(caller, selector, query)
        await runtime.aclose()
        return saved.record_id, [match.record_id for match in found.matches]

    record_id, first_matches = anyio.run(first_process)
    copy = settings.decision_staging_dir / COPY_FILE
    assert copy.is_file()
    with closing(sqlite3.connect(copy)) as database:
        assert database.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] >= 1

    remote.events.clear()

    async def second_process() -> list[str | None]:
        runtime = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: FIRST_TIME)
        found = await runtime.search_memory(caller, selector, query)
        await runtime.aclose()
        return [match.record_id for match in found.matches]

    assert anyio.run(second_process) == first_matches == [record_id]
    assert not any(event.startswith("GET_BLOB") for event in remote.events)


def test_hosted_search_scans_past_brain_cores_uncached_default() -> None:
    fake = LANE_B.FakeGitHub()
    fake.seed(_owned_commitments(SNAPSHOT_MAX_RECORDS + 20))
    storage = _hub_storage(fake)
    with pytest.raises(GitHubStorageError, match="explicit limit"):
        storage.snapshot_records(kinds=frozenset({"commitment"}))

    provider = GitHubBrainProvider(storage, _adapter(operation_time=lambda _: FIRST_TIME))
    result = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, "read"), MemorySearchQuery("fixture", limit=5))

    assert len(result.matches) == 5
    assert result.truncated


def _hub_storage(fake):
    """A storage as the hub builds it, with an object cache: Brain Core refuses the hub's 5,000-file scan
    cap without one (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1)."""

    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=fake,
        object_cache=GitHubObjectCache(),
    )
