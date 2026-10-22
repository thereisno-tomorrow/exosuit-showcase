"""Test-only support for the recall benchmark: build a FakeGitHub snapshot from
``recall_fixtures/records.json`` the way tests/mcp/test_github_everyday.py
seeds a fake for GitHubBrainProvider. Not collected by pytest (no test_*
functions), and not shipped: gremlin_town.benchmarks.recall (the production
module) knows nothing about FakeGitHub or JSON record fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from brain_core.records import record_from_mapping, render_record
from gremlin_town.mcp.brain_runtime import (
    BrainOperation,
    OwnerAuthorization,
    OwnerSelector,
)
from gremlin_town.mcp.github_brain_provider import BrainCoreRecordAdapter, GitHubBrainProvider

FIXTURES_DIR = Path(__file__).resolve().parent / "recall_fixtures"


def load_lane_b() -> ModuleType:
    """Load Brain Core's real FakeGitHub/storage fixture, the way
    tests/mcp/test_github_brain_provider.py's _load_lane_b_fake does, without
    copying or mocking it. A separate small loader (rather than importing
    across tests/mcp -> tests/benchmarks) because pytest's no-__init__.py test
    layout only puts each test file's own directory on sys.path.
    """

    path = (
        Path(__file__).resolve().parents[3]
        / "brain-core"
        / "tests"
        / "unit"
        / "test_github_storage.py"
    )
    name = "_brain_core_lane_b_for_recall_benchmark"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_fixture_records(records_path: Path = FIXTURES_DIR / "records.json") -> list[dict[str, object]]:
    payload = json.loads(records_path.read_text(encoding="utf-8"))
    records = payload["records"]
    assert isinstance(records, list) and records, f"{records_path}: expected a non-empty 'records' list"
    return records


def fixture_snapshot_files(records_path: Path = FIXTURES_DIR / "records.json") -> dict[str, bytes]:
    """Render every fixture record through Brain Core exactly as a real save
    would, keyed by the same repository-relative path GitHubBrainProvider
    itself writes records at (records/<kind>/<YYYY>/<MM>/<id>.md) - ready for
    FakeGitHub.seed().
    """

    files: dict[str, bytes] = {}
    for entry in load_fixture_records(records_path):
        record = record_from_mapping(dict(entry), f"recall fixture {entry['id']}")
        path = f"records/{record.kind}/{record.created_at:%Y}/{record.created_at:%m}/{record.id}.md"
        files[path] = render_record(record).encode("utf-8")
    return files


def _unused_operation_time(_operation_id: str):
    raise AssertionError("memory.search must not need a trusted operation time")


def _unused_source_evidence(_authorization, _reference):
    raise AssertionError("memory.search must not need a trusted source-evidence resolver")


def build_provider(fake) -> GitHubBrainProvider:
    """A GitHubBrainProvider over `fake`, with a record adapter whose write-only
    resolvers assert if search ever reaches them (it shouldn't: _search reads
    the storage snapshot directly).
    """

    lane_b = load_lane_b()
    adapter = BrainCoreRecordAdapter(
        operation_time=_unused_operation_time,
        source_evidence=_unused_source_evidence,
    )
    # With an object cache, as the hub builds it (the provider's 5,000-file scans need one).
    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    storage = GitHubBrainHomeStorage(
        repository=lane_b.REPOSITORY, branch=lane_b.BRANCH, token=lane_b.TOKEN, transport=fake,
        object_cache=GitHubObjectCache(),
    )
    return GitHubBrainProvider(storage, adapter)


def search_authorization() -> OwnerAuthorization:
    return OwnerAuthorization(
        operation=BrainOperation.MEMORY_SEARCH,
        access="read",
        selector=OwnerSelector("personal"),
        owner_id="brain-home",
        owner_locator="vault://brain-home",
        policy_revision="fixture-policy-v1",
        capability=object(),
    )
