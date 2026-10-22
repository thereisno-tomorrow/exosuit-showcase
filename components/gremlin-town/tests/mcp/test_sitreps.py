"""Task and project sitreps on the hub (docs/product/steps/b-project-sitreps.md, "Tests (offline)").

Everything runs offline: the real PC connector over loopback HTTP with Brain Core (the local
harness), and Brain Home through Brain Core's own fake GitHub. The hub runtime is the real
production composition, `build_brain_runtime(..., sitreps_enabled=True)`.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from brain_local_harness import (  # noqa: E402
    SESSION_ID,
    WRITER_TOKEN,
    build_fixture,
    load_lane_b_fake,
)

from brain_core.durability import CATALOG_PATH  # noqa: E402
from brain_core.github_storage import GitHubTransportError  # noqa: E402
from brain_core.presentation import relative_day_words  # noqa: E402
from brain_core.records import render_record  # noqa: E402
from brain_core.sitreps import current_focus_copy, current_sitreps  # noqa: E402
from brain_core.timewords import today_in_singapore  # noqa: E402
from gremlin_town.contracts import Actor  # noqa: E402
from gremlin_town.mcp import local_project_connector  # noqa: E402
from gremlin_town.mcp.brain_assembly import (  # noqa: E402
    _GITHUB_OPERATIONS,
    BrainAssemblyError,
    build_brain_runtime,
    load_owner_policy,
)
from gremlin_town.mcp.brain_runtime import (  # noqa: E402
    BrainCaller,
    BrainOperation,
    BrainRuntime,
    BrainRuntimeError,
    ContinuityAppendRequest,
    ContinuityCloseRequest,
    ContinuityListRequest,
    ContinuityListResult,
    ContinuityRecoverRequest,
    ContinuityStartRequest,
    ContinuityWriteResult,
    OwnerSelector,
)
from gremlin_town.mcp.brain_tools import build_brain_tools  # noqa: E402
from gremlin_town.mcp.sitreps import (  # noqa: E402
    NEEDS_PC,
    NOT_UPDATED,
    ContinuityListWithProjectSitrep,
    ContinuityWriteResultWithSummary,
    SitrepBrainRuntime,
    SitrepListResult,
)
from gremlin_town.runtime import BrainMCPSettings  # noqa: E402

LANE_B = load_lane_b_fake()
GIT = "vault_git_fixture"
NONGIT = "vault_nongit_fixture"
WRITER = BrainCaller("local-codex", frozenset({"bc:read", "bc:write"}))
READER = BrainCaller("chatgpt-web", frozenset({"bc:read"}))  # one of Nic's agents, not enrolled on the projects
STRANGER = BrainCaller("stranger", frozenset({"bc:read"}))
PROJECT_ONLY = BrainCaller("project-only", frozenset({"bc:read", "bc:write"}))  # enrolled on the projects only
LISTED_FIELDS = {"project", "session_id", "intent", "next_step", "blockers", "note_updated_at", "note"}


class SwitchableGitHub(LANE_B.FakeGitHub):
    """Brain Core's fake GitHub, whose writes (``down``) or reads (``reads_down``) can fail like an outage."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False
        self.reads_down = False

    def request(self, method: str, url: str, *, headers: Any, body: bytes | None) -> Any:
        if (self.down and method in {"POST", "PATCH"}) or (self.reads_down and method == "GET"):
            raise GitHubTransportError("fixture_github_down", "GitHub is down in this test")
        return super().request(method, url, headers=headers, body=body)


def _vault(root: Path, focus: str) -> None:
    """Make ``root`` a Vault OS vault whose Compass has ``focus`` as its Focus section."""

    (root / "ops").mkdir(parents=True, exist_ok=True)
    (root / "ops" / "vault-manifest.md").write_text(
        f"---\nvault-name: Fixture\nroot-path: {root.resolve().as_posix()}\nexports:\n  compass: compass.md\n---\n",
        encoding="utf-8",
    )
    _compass(root, focus)


def _compass(root: Path, focus: str) -> None:
    (root / "compass.md").write_text(
        f"# Compass\n\n*Updated: 2026-09-28*\n\n## Focus\n\n{focus}\n\n## Questions\n\n1. None yet.\n",
        encoding="utf-8",
    )


def _policy(path: Path) -> Path:
    project_operations = [
        "context.retrieve", "continuity.start", "continuity.append",
        "continuity.recover", "continuity.list", "continuity.close",
    ]
    brain_home = sorted(operation.value for operation in _GITHUB_OPERATIONS)
    owners = [
        {
            "kind": "project", "project_id": project, "owner_id": project, "owner_locator": f"vault://{project}",
            "readers": ["local-codex", "project-only"], "writers": ["local-codex", "project-only"],
            "operations": project_operations,
            "session_id": SESSION_ID,
        }
        for project in (GIT, NONGIT)
    ] + [
        {
            "kind": kind, "project_id": None, "owner_id": "brain-home", "owner_locator": "vault://brain-home",
            "readers": ["local-codex", "chatgpt-web"], "writers": ["local-codex", "chatgpt-web"],
            "operations": brain_home + (["sitrep.read"] if kind == "cross-project" else []), "session_id": None,
        }
        for kind in ("personal", "cross-project")
    ]
    path.write_text(json.dumps({"schema": "gt.brain-owner-policy/v1", "revision": "fixture-sitreps-v1", "owners": owners}),
                    encoding="utf-8")
    return path


def _settings(root: Path, connector_url: str) -> BrainMCPSettings:
    secrets = root / "hub-secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "connector").write_text(WRITER_TOKEN, encoding="utf-8")
    (secrets / "github").write_text(LANE_B.TOKEN, encoding="utf-8")
    sources = root / "brain-sources.json"
    sources.write_text(json.dumps({"schema": "gt.brain-source-evidence/v1", "revision": "fixture", "sources": []}),
                       encoding="utf-8")
    return BrainMCPSettings(
        connector_url=connector_url,
        connector_token_file=secrets / "connector",
        github_repository=LANE_B.REPOSITORY,
        github_branch=LANE_B.BRANCH,
        github_token_file=secrets / "github",
        policy_file=_policy(root / "brain-policy.json"),
        decision_staging_dir=root / "provider-state",
        ownership_catalog_file=Path(CATALOG_PATH),
        source_registry_file=sources,
    )


@dataclass
class Hub:
    root: Path
    fixture: Any
    connector: Any
    remote: SwitchableGitHub
    clock: list[datetime]
    settings: BrainMCPSettings
    runtime: BrainRuntime

    def call(self, method: str, *args: Any) -> Any:
        return anyio.run(getattr(self.runtime, method), *args)

    def start(self, session: str, intent: str = "Draft the sitrep spec", next_step: str = "Write the tests", *,
              project: str = GIT) -> ContinuityWriteResult:
        return self.call("start_continuity", WRITER, OwnerSelector("project", project),
                         ContinuityStartRequest(f"start-{session}", session, intent, ("fixture://source/current",), next_step))

    def append(self, session: str, event: str, value: str, *, key: str, project: str = GIT) -> ContinuityWriteResult:
        return self.call("append_continuity", WRITER, OwnerSelector("project", project),
                         ContinuityAppendRequest(f"append-{key}", session, event, value))  # type: ignore[arg-type]

    def close(self, session: str, *, project: str = GIT) -> ContinuityWriteResult:
        return self.call("close_continuity", WRITER, OwnerSelector("project", project),
                         ContinuityCloseRequest(f"close-{session}", session))

    def list(self, caller: BrainCaller = WRITER, *, project: str = GIT, limit: int = 20) -> ContinuityListResult:
        return self.call("list_continuity", caller, OwnerSelector("project", project), ContinuityListRequest(limit))

    def recover(self, session: str, caller: BrainCaller = WRITER) -> Any:
        return self.call("recover_continuity", caller, OwnerSelector("project", GIT),
                         ContinuityRecoverRequest(1000, session))

    def records(self) -> list[Any]:
        snapshot = LANE_B.storage(self.remote).snapshot_records(kinds=frozenset({"sitrep", "focus_copy"}))
        return [item.record for item in snapshot.records]

    def sitrep(self, session: str, project: str = GIT) -> Any:
        return current_sitreps(self.records(), project).get(session)

    def focus(self, project: str = GIT) -> Any:
        return current_focus_copy(self.records(), project)

    def tree(self) -> dict[str, str]:
        return self.remote.trees[self.remote.commits[self.remote.branch]["tree"]]


@contextmanager
def hub_for(root: Path, *, focus: str | None = "Make the fixture the verified home for sitreps. Then the rest.",
            sitreps: bool = True) -> Iterator[Hub]:
    fixture = build_fixture(root / "pc")
    if focus is not None:
        _vault(fixture.git_project, focus)
    connector = fixture.running().start()
    remote = SwitchableGitHub()
    clock = [datetime.now(UTC)]
    settings = _settings(root / "hub", connector.base_url)
    runtime = build_brain_runtime(settings, github_transport=remote, operation_clock=lambda: clock[0],
                                  sitreps_enabled=sitreps)
    try:
        yield Hub(root, fixture, connector, remote, clock, settings, runtime)
    finally:
        anyio.run(runtime.aclose)
        connector.stop()


def test_a_note_start_append_and_close_keep_one_task_sitrep_with_only_the_listed_fields(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        started = hub.start("task-a", "Draft the sitrep spec", "Write the tests")
        assert type(started) is ContinuityWriteResult  # nothing extra when the summary is up to date
        head = hub.sitrep("task-a")
        assert head.state == "open" and head.assertion == "system"
        assert set(head.data) == LISTED_FIELDS
        assert (head.data["intent"], head.data["next_step"], head.data["blockers"]) == (
            "Draft the sitrep spec", "Write the tests", [])
        assert head.data["note"] == {"record_id": started.record_id, "content_sha256": started.receipt.content_sha256}
        assert head.data["note_updated_at"] == started.updated_at.isoformat()
        assert head.evidence[-1].actor == "system:sitreps"

        blocked = hub.append("task-a", "blocker", "Waiting on the connector update", key="a1")
        head = hub.sitrep("task-a")
        assert head.state == "blocked" and head.data["blockers"] == ["Waiting on the connector update"]
        assert head.data["note"]["record_id"] == blocked.record_id

        hub.append("task-a", "effect", "EFFECT-CANARY emailed Sam the deck", key="a2")
        hub.append("task-a", "source", "SOURCE-CANARY", key="a3")
        hub.append("task-a", "decision", "DECISION-CANARY", key="a4")
        hub.append("task-a", "candidate", "CANDIDATE-CANARY", key="a5")
        moving = hub.append("task-a", "next_step", "Try the new route", key="a6")
        head = hub.sitrep("task-a")
        assert head.state == "open" and head.data["next_step"] == "Try the new route"
        assert head.data["note"]["record_id"] == moving.record_id

        closed = hub.close("task-a")
        head = hub.sitrep("task-a")
        assert head.state == "closed" and head.data["note"]["record_id"] == closed.record_id

        versions = [record for record in hub.records() if record.kind == "sitrep"]
        assert len(versions) == 8  # start, 6 appends, close: one chain, one current version
        for record in versions:
            assert set(record.data) == LISTED_FIELDS
            text = render_record(record)
            assert not any(canary in text for canary in ("EFFECT-CANARY", "SOURCE-CANARY", "DECISION-CANARY", "CANDIDATE-CANARY"))
        assert list(current_sitreps(hub.records(), GIT)) == ["task-a"]


def test_a_replayed_earlier_write_never_rolls_the_sitrep_back(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.start("task-a")
        hub.append("task-a", "next_step", "First step", key="r1")
        hub.append("task-a", "next_step", "Second step", key="r2")
        versions = len([record for record in hub.records() if record.kind == "sitrep"])
        replayed = hub.append("task-a", "next_step", "First step", key="r1")  # same operation, same input
        assert type(replayed) is ContinuityWriteResult
        assert hub.sitrep("task-a").data["next_step"] == "Second step"
        assert len([record for record in hub.records() if record.kind == "sitrep"]) == versions


def test_the_project_sitrep_assembles_from_task_sitreps_and_the_focus_copy(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.start("task-a", "Draft the sitrep spec", "Write the tests")
        hub.start("task-b", "Check the connector", "Ask Nic")
        hub.append("task-b", "blocker", "The PC connector is old", key="b1")
        listed = hub.list()
        assert isinstance(listed, ContinuityListWithProjectSitrep)  # PC on: the notes themselves, as before
        assert {item.session_id for item in listed.items} == {"task-a", "task-b"}
        view = listed.project_sitrep
        assert view["project"] == GIT
        assert view["focus"]["line"] == "Make the fixture the verified home for sitreps."
        assert [task["session_id"] for task in view["open"]] == ["task-a"]
        assert view["blocked"] == [{
            "session_id": "task-b", "intent": "Check the connector", "blocker": "The PC connector is old",
            "updated_at": hub.sitrep("task-b").data["note_updated_at"],
        }]
        assert view["latest_activity"]["session_id"] == "task-b"
        assert view["as_of"] is not None

        # A project whose vault has no Compass: the Focus line is left out, and no copy is stored.
        hub.start("task-n", "Tidy the non-Git notes", "Start", project=NONGIT)
        other = hub.list(project=NONGIT).project_sitrep
        assert "focus" not in other
        assert [task["session_id"] for task in other["open"]] == ["task-n"]
        assert hub.focus(NONGIT) is None


def test_any_of_nics_agents_can_list_and_read_sitreps_even_if_not_enrolled_on_the_project(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.start("task-a", "Draft the sitrep spec", "Write the tests")
        hub.start("task-b", "Check the connector", "Ask Nic")
        hub.append("task-b", "blocker", "Waiting on the organizer", key="b1")

        answered = hub.list(READER)
        assert isinstance(answered, SitrepListResult)
        assert answered.summary == f"summary as of {relative_day_words(today_in_singapore(answered.as_of), today_in_singapore(hub.clock[0]))}; the full notes need access to this project"
        assert [(item.session_id, item.state) for item in answered.items] == [("task-b", "blocked"), ("task-a", "open")]
        assert answered.items[0].blockers == ("Waiting on the organizer",)
        assert answered.project_sitrep["focus"]["line"] == "Make the fixture the verified home for sitreps."
        assert [task["session_id"] for task in answered.project_sitrep["open"]] == ["task-a"]

        # Reading sitreps is all it widens: the full note and every write stay with the project's agents.
        with pytest.raises(BrainRuntimeError) as full_note:
            hub.recover("task-a", READER)
        assert full_note.value.code == "brain.policy.excluded"
        # An agent without the Brain Home grant gets nothing.
        with pytest.raises(BrainRuntimeError) as stranger:
            hub.list(STRANGER)
        assert stranger.value.code == "brain.policy.excluded"


def test_a_failed_brain_home_write_leaves_the_note_saved_queues_a_retry_and_says_so(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        tools = {tool.name: tool for tool in build_brain_tools(
            hub.runtime, lambda: Actor("local-codex"), scopes_provider=lambda: {"bc:read", "bc:write"},
            authorize=_allowed,
        )}
        hub.remote.down = True
        saved = anyio.run(tools["bc_continuity_start"].run, {
            "operation_id": "start-task-a", "session_id": "task-a", "intent": "Draft the sitrep spec",
            "source_references": ["fixture:source:current"], "project_id": GIT, "next_step": "Write the tests",
        })
        reply = saved.structuredContent
        assert saved.isError is False
        assert reply["summary"] == NOT_UPDATED == "summary not updated yet"
        assert reply["receipt"]["status"] == "accepted" and reply["record_id"]
        # The note is saved on the PC, and Brain Home has no sitrep yet.
        assert [item.session_id for item in hub.list().items] == ["task-a"]
        assert hub.sitrep("task-a") is None
        appended = hub.append("task-a", "next_step", "Keep going", key="a1")
        assert isinstance(appended, ContinuityWriteResultWithSummary) and appended.summary == NOT_UPDATED
        assert {field.name for field in fields(appended)} == {field.name for field in fields(ContinuityWriteResult)} | {"summary"}

        hub.remote.down = False
        answered = hub.list(READER)  # the next call retries the queued write before answering
        assert [(item.session_id, item.next_step) for item in answered.items] == [("task-a", "Keep going")]
        assert hub.sitrep("task-a").data["note"]["record_id"] == appended.record_id


async def _allowed() -> None:
    return None


def test_with_the_pc_off_lists_answer_as_of_and_a_full_note_needs_the_pc(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.start("task-a", "Draft the sitrep spec", "Write the tests")
        hub.append("task-a", "blocker", "Waiting on the connector update", key="a1")
        hub.start("task-b", "Check the route", "Ask Nic")
        hub.connector.stop()  # the PC is off

        answered = hub.list()
        assert isinstance(answered, SitrepListResult)
        assert answered.as_of is not None
        assert answered.summary == f"summary as of {relative_day_words(today_in_singapore(answered.as_of), today_in_singapore(hub.clock[0]))}; the full note needs the PC"
        assert {(item.session_id, item.state) for item in answered.items} == {("task-a", "blocked"), ("task-b", "open")}
        view = answered.project_sitrep
        assert view["as_of"] == answered.as_of.isoformat()
        assert view["focus"]["line"] == "Make the fixture the verified home for sitreps."
        assert [task["session_id"] for task in view["blocked"]] == ["task-a"]

        with pytest.raises(BrainRuntimeError) as needs_pc:
            hub.recover("task-a")
        assert needs_pc.value.code == "brain.capability.unavailable"
        assert needs_pc.value.message == NEEDS_PC and needs_pc.value.retryable is True

        nothing = hub.list(project=NONGIT)
        assert nothing.items == () and nothing.summary == "no summary saved yet; the full note needs the PC"

        # An agent not on the project asks later: the hourly Focus check finds the PC off and moves on.
        hub.clock[0] += timedelta(hours=2)
        later = hub.list(READER)
        assert {item.session_id for item in later.items} == {"task-a", "task-b"}
        assert later.project_sitrep["focus"]["line"] == "Make the fixture the verified home for sitreps."


def test_pc_unavailable_summary_formats_the_day_and_preserves_the_timestamp(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.clock[0] = datetime(2026, 9, 30, 22, tzinfo=UTC)
        hub.start("task-a", "Draft the sitrep spec", "Write the tests")
        hub.connector.stop()

        answered = hub.list()
        assert isinstance(answered, SitrepListResult)
        # 22:00 UTC on 30 Sep is 06:00 on Thu 1 Oct for Nic, so the day shown is his.
        assert answered.summary == "summary as of today; the full note needs the PC"
        assert answered.as_of == datetime(2026, 9, 30, 22, tzinfo=UTC)
        assert answered.project_sitrep["as_of"] == "2026-09-30T22:00:00+00:00"


def test_the_focus_copy_refreshes_on_a_note_save_and_at_most_once_an_hour_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = local_project_connector.LocalProjectConnectorClient.read_compass_focus

    def counted(self: Any, project_id: str) -> dict[str, Any]:
        calls.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(local_project_connector.LocalProjectConnectorClient, "read_compass_focus", counted)
    with hub_for(tmp_path, focus="First focus. More.") as hub:
        start = hub.clock[0]
        hub.start("task-a")
        assert hub.focus().data["line"] == "First focus." and len(calls) == 1

        _compass(hub.fixture.git_project, "Second focus. More.")
        hub.clock[0] = start + timedelta(minutes=10)
        assert hub.list().project_sitrep["focus"]["line"] == "First focus."
        assert len(calls) == 1  # checked at the save ten minutes ago
        hub.clock[0] = start + timedelta(minutes=65)
        assert hub.list().project_sitrep["focus"]["line"] == "Second focus."
        assert hub.list().project_sitrep["focus"]["copied_at"] == hub.clock[0].isoformat()
        assert len(calls) == 2  # once in that hour, however often it is asked

        _compass(hub.fixture.git_project, "Third focus. More.")
        hub.clock[0] = start + timedelta(minutes=66)
        hub.append("task-a", "next_step", "Next", key="a1")
        assert hub.focus().data["line"] == "Third focus." and len(calls) == 3
        hub.append("task-a", "next_step", "Again", key="a2")
        assert len(calls) == 4  # every save checks, but an unchanged line writes nothing new
        assert len([record for record in hub.records() if record.kind == "focus_copy"]) == 3


def test_agents_make_the_same_calls_so_no_model_step_is_added(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        plain = build_brain_runtime(hub.settings, github_transport=hub.remote, operation_clock=lambda: hub.clock[0])
        try:
            assert type(plain) is BrainRuntime and isinstance(hub.runtime, SitrepBrainRuntime)

            def described(runtime: BrainRuntime) -> list[tuple[Any, ...]]:
                return [
                    (tool.name, tool.description, json.dumps(tool.parameters, sort_keys=True), tool.annotations)
                    for tool in build_brain_tools(runtime, lambda: Actor("local-codex"),
                                                  scopes_provider=lambda: {"bc:read", "bc:write"}, authorize=_allowed)
                ]

            assert described(hub.runtime) == described(plain)
            tools = {tool.name: tool for tool in build_brain_tools(
                hub.runtime, lambda: Actor("local-codex"), scopes_provider=lambda: {"bc:read", "bc:write"},
                authorize=_allowed,
            )}
            reply = anyio.run(tools["bc_continuity_start"].run, {
                "operation_id": "start-task-a", "session_id": "task-a", "intent": "One call is enough",
                "source_references": [], "project_id": GIT,
            }).structuredContent
            assert "summary" not in reply
            assert hub.sitrep("task-a").data["intent"] == "One call is enough"  # no second call was needed
        finally:
            anyio.run(plain.aclose)


def test_a_closed_task_sitrep_is_archived_after_30_days_and_no_longer_listed(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        now = hub.clock[0]
        hub.start("task-old", "Finished work", "Nothing")
        hub.close("task-old")
        hub.start("task-live", "Still going", "Next")
        old = [record for record in hub.records() if record.kind == "sitrep" and record.data["session_id"] == "task-old"]
        old_files = {
            path: hub.tree()[path]
            for path in (f"records/sitrep/{record.created_at:%Y}/{record.created_at:%m}/{record.id}.md" for record in old)
        }
        assert len(old_files) == 2

        hub.clock[0] = now + timedelta(days=29)
        assert hub.list(READER).project_sitrep["open"][0]["session_id"] == "task-live"
        assert all(path in hub.tree() for path in old_files)

        hub.clock[0] = now + timedelta(days=31)
        hub.remote.down = True  # an archive move that fails is simply tried again on the next call
        assert [item.session_id for item in hub.list(READER).items] == ["task-live"]
        assert all(path in hub.tree() for path in old_files)
        hub.remote.down = False
        answered = hub.list(READER)
        assert [item.session_id for item in answered.items] == ["task-live"]
        tree = hub.tree()
        for path, blob in old_files.items():
            assert path not in tree
            assert tree["archive/" + path] == blob  # moved byte for byte, step C's archive path
        assert hub.sitrep("task-old") is None and hub.sitrep("task-live") is not None
        assert answered.project_sitrep["latest_activity"]["session_id"] == "task-live"
        assert hub.runtime._sitreps._state.wanted("sitrep", GIT, "task-old") is None


def test_sitreps_are_off_by_default_and_project_calls_answer_exactly_as_before(tmp_path: Path) -> None:
    with hub_for(tmp_path, sitreps=False) as hub:
        assert type(hub.runtime) is BrainRuntime
        assert type(hub.start("task-a")) is ContinuityWriteResult
        assert type(hub.list()) is ContinuityListResult
        assert hub.records() == []
        assert not (hub.settings.decision_staging_dir / "sitreps.sqlite3").exists()
        with pytest.raises(BrainRuntimeError) as denied:
            hub.list(READER)
        assert denied.value.code == "brain.policy.excluded"
        hub.connector.stop()
        with pytest.raises(BrainRuntimeError) as unavailable:
            hub.list()
        assert unavailable.value.code == "brain.capability.unavailable" and unavailable.value.message != NEEDS_PC


def test_sitrep_reads_are_granted_only_on_brain_home_owners(tmp_path: Path) -> None:
    path = _policy(tmp_path / "policy.json")
    policy = load_owner_policy(path)
    by_kind = {owner.selector.kind: owner for owner in policy.owners if owner.selector.kind != "project"}
    assert BrainOperation.SITREP_READ in by_kind["cross-project"].operations
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["owners"][0]["operations"].append("sitrep.read")
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BrainAssemblyError):
        load_owner_policy(path)


def test_the_sitreps_setting_is_off_by_default_and_needs_brain_core(tmp_path: Path) -> None:
    from gremlin_town.runtime import ConfigurationError, build_runtime
    from gremlin_town.transport import FakeTransport
    from tests.integration.test_mcp_runtime import mcp_config

    config, _ = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.sitreps_enabled is False and runtime.brain_runtime is None
    finally:
        runtime.close()
    base = config.read_text(encoding="utf-8")
    for addition, message in (
        ("[sitreps]\nenabled = true\n", r"\[sitreps\] enabled requires \[brain_core\] enabled"),
        ('[sitreps]\nenabled = "yes"\n', "sitreps.enabled must be a boolean"),
        ("[sitreps]\nenabled = false\nwindow_days = 30\n", "unknown sitreps configuration: window_days"),
    ):
        config.write_text(base + "\n" + addition, encoding="utf-8")
        with pytest.raises(ConfigurationError, match=message):
            build_runtime(config, provider=FakeTransport(), start_worker=False)


def test_switching_sitreps_on_reaches_the_hubs_brain_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gremlin_town.mcp import brain_assembly
    from gremlin_town.runtime import build_runtime
    from gremlin_town.transport import FakeTransport
    from tests.integration.test_mcp_runtime import mcp_config

    config, _ = mcp_config(tmp_path)
    settings = _settings(tmp_path / "hub", "http://127.0.0.1:65530")
    with config.open("a", encoding="utf-8") as handle:
        handle.write("\n[brain_core]\nenabled = true\n")
        for field in fields(settings):
            value = getattr(settings, field.name)
            if value is not None:
                encoded = json.dumps(value) if isinstance(value, bool) else json.dumps(str(value).replace(chr(92), '/'))
                handle.write(f"{field.name} = {encoded}\n")
        handle.write("\n[sitreps]\nenabled = true\n")
    seen: dict[str, Any] = {}

    def capture(configured: Any, **kwargs: Any) -> BrainRuntime:
        seen.update(kwargs)
        return build_brain_runtime(configured, github_transport=LANE_B.FakeGitHub(), **kwargs)

    monkeypatch.setattr(brain_assembly, "build_brain_runtime", capture)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.sitreps_enabled is True and seen["sitreps_enabled"] is True
        assert isinstance(runtime.brain_runtime, SitrepBrainRuntime)
    finally:
        anyio.run(runtime.brain_runtime.aclose)
        runtime.close()


def test_an_unexpected_sitrep_failure_still_leaves_the_note_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with hub_for(tmp_path) as hub:
        def broken(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("a bug in the sitrep step")

        monkeypatch.setattr(hub.runtime._sitreps, "after_write", broken)
        saved = hub.start("task-a")
        assert isinstance(saved, ContinuityWriteResultWithSummary) and saved.summary == NOT_UPDATED
        assert [item.session_id for item in hub.list().items] == ["task-a"]


def _plain(hub: Hub, state_dir: Path | None = None, *, sitreps: bool = False) -> BrainRuntime:
    from dataclasses import replace

    settings = hub.settings if state_dir is None else replace(hub.settings, decision_staging_dir=state_dir)
    return build_brain_runtime(settings, github_transport=hub.remote, operation_clock=lambda: hub.clock[0],
                               sitreps_enabled=sitreps)


def test_a_note_started_before_sitreps_were_on_gets_its_sitrep_at_its_next_save(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        earlier = _plain(hub)
        selector = OwnerSelector("project", GIT)
        try:
            for session in ("task-early", "task-quiet"):
                anyio.run(earlier.start_continuity, WRITER, selector, ContinuityStartRequest(
                    f"start-{session}", session, "Started before sitreps", ("fixture://source/current",), "First step"))
            anyio.run(earlier.append_continuity, WRITER, selector,
                      ContinuityAppendRequest("append-early", "task-early", "blocker", "Old blocker"))
        finally:
            anyio.run(earlier.aclose)
        assert hub.sitrep("task-early") is None

        hub.append("task-early", "next_step", "Carry on", key="e1")
        head = hub.sitrep("task-early")
        assert (head.state, head.data["intent"], head.data["next_step"], head.data["blockers"]) == (
            "open", "Started before sitreps", "Carry on", ["Old blocker"])
        # A note that closes without ever having had a sitrep leaves none behind.
        assert type(hub.close("task-quiet")) is ContinuityWriteResult
        assert hub.sitrep("task-quiet") is None


def test_a_hub_that_lost_its_own_state_carries_on_from_brain_home(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.start("task-a", "Draft the sitrep spec", "Step one")
        hub.append("task-a", "blocker", "Waiting", key="a1")
        before = [record.id for record in hub.records() if record.kind == "sitrep"]
        fresh = _plain(hub, tmp_path / "fresh-state", sitreps=True)
        selector = OwnerSelector("project", GIT)
        try:
            # A replayed start reaches a hub with no state: Brain Home's later sitrep wins, nothing is written.
            replayed = anyio.run(fresh.start_continuity, WRITER, selector, ContinuityStartRequest(
                "start-task-a", "task-a", "Draft the sitrep spec", ("fixture://source/current",), "Step one"))
            assert type(replayed) is ContinuityWriteResult
            assert [record.id for record in hub.records() if record.kind == "sitrep"] == before
            anyio.run(fresh.aclose)
            fresh = _plain(hub, tmp_path / "fresh-state-2", sitreps=True)
            # A replay of the latest write, on a hub with no state: rebuilt from Brain Home, same as it.
            anyio.run(fresh.append_continuity, WRITER, selector,
                      ContinuityAppendRequest("append-a1", "task-a", "blocker", "Waiting"))
            assert [record.id for record in hub.records() if record.kind == "sitrep"] == before
            anyio.run(fresh.append_continuity, WRITER, selector,
                      ContinuityAppendRequest("append-fresh", "task-a", "next_step", "Step two"))
        finally:
            anyio.run(fresh.aclose)
        head = hub.sitrep("task-a")
        assert (head.state, head.data["intent"], head.data["next_step"], head.data["blockers"]) == (
            "open", "Draft the sitrep spec", "Step two", ["Waiting"])


def test_with_brain_home_unreadable_a_save_still_works_from_the_note_itself(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        early = _plain(hub)
        selector = OwnerSelector("project", GIT)
        try:
            for session in ("task-a", "task-b"):
                anyio.run(early.start_continuity, WRITER, selector, ContinuityStartRequest(
                    f"start-{session}", session, "Started early", ("fixture://source/current",), "First step"))
        finally:
            anyio.run(early.aclose)
        hub.remote.reads_down = True
        appended = hub.append("task-a", "next_step", "Keep going", key="a1")
        assert isinstance(appended, ContinuityWriteResultWithSummary)  # worked out from the note, queued
        closed = hub.close("task-b")  # a closed note can't be read back, so there is nothing to queue
        assert isinstance(closed, ContinuityWriteResultWithSummary)
        hub.remote.reads_down = False
        hub.list()
        head = hub.sitrep("task-a")
        assert (head.data["intent"], head.data["next_step"]) == ("Started early", "Keep going")
        assert hub.sitrep("task-b") is None


def test_a_sitrep_write_that_meets_another_brain_home_write_tries_again(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.remote.advance_after_ref = hub.remote.prepare_advance({"unrelated.txt": b"someone else's save"})
        started = hub.start("task-a")
        assert type(started) is ContinuityWriteResult
        assert hub.sitrep("task-a") is not None and "unrelated.txt" in hub.tree()


def test_an_older_pc_connector_without_the_focus_route_leaves_focus_out_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def old_connector(self: Any, project_id: str) -> dict[str, Any]:
        raise local_project_connector.ConnectorError("NOT_FOUND", "connector route not found", status=404)

    monkeypatch.setattr(local_project_connector.LocalProjectConnectorClient, "read_compass_focus", old_connector)
    with hub_for(tmp_path) as hub:
        assert type(hub.start("task-a")) is ContinuityWriteResult  # the task sitrep is up to date
        assert hub.focus() is None
        assert "focus" not in hub.list().project_sitrep


def test_an_agent_on_the_project_without_the_brain_home_grant_gets_the_notes_as_before(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.start("task-a")
        listed = hub.list(PROJECT_ONLY)
        assert type(listed) is ContinuityListResult and [item.session_id for item in listed.items] == ["task-a"]
        hub.connector.stop()
        with pytest.raises(BrainRuntimeError) as unavailable:
            hub.list(PROJECT_ONLY)
        assert unavailable.value.code == "brain.capability.unavailable"
        assert unavailable.value.message == "PC-local connector is unavailable"  # no summary without the grant


def test_personal_notes_are_untouched_by_sitreps(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        personal = OwnerSelector("personal")
        saved = hub.call("start_continuity", WRITER, personal, ContinuityStartRequest(
            "start-personal", "personal-task", "Buy the gift", (), "Pick a shop"))
        assert type(saved) is ContinuityWriteResult
        assert type(hub.call("list_continuity", WRITER, personal, ContinuityListRequest(5))) is ContinuityListResult
        assert hub.records() == []


def test_when_brain_home_cant_be_read_the_live_notes_still_answer(tmp_path: Path) -> None:
    with hub_for(tmp_path) as hub:
        hub.start("task-a")
        hub.remote.reads_down = True
        listed = hub.list()
        assert type(listed) is ContinuityListResult  # the notes from the PC; the extra view is left out
        hub.connector.stop()
        with pytest.raises(BrainRuntimeError) as unreadable:
            hub.list()
        assert unreadable.value.code == "brain.capability.unavailable" and unreadable.value.retryable
        assert "could not be read from Brain Home" in unreadable.value.message


def test_a_note_that_starts_with_no_next_step_gets_a_sitrep_with_none(tmp_path: Path) -> None:
    # Step B's N12c (slice 2) lets a note start with an empty next step; the sitrep follows it.
    from types import SimpleNamespace

    with hub_for(tmp_path) as hub:
        started = hub.start("task-a")
        request = SimpleNamespace(session_id="task-z", intent="Started with no first step", next_step=None)
        result = ContinuityWriteResult(
            record_id=started.record_id, session_id="task-z", state="open", changed=True,
            updated_at=started.updated_at, receipt=started.receipt,
        )
        assert hub.runtime._sitreps.after_write(GIT, BrainOperation.CONTINUITY_START, request, result) is True
        assert hub.sitrep("task-z").data["next_step"] == ""


def test_where_am_i_and_what_next_read_the_sitreps_the_hub_wrote_and_write_nothing(tmp_path: Path) -> None:
    """Step F's projects part (N4; rebuild plan slice 8, part 2), end to end: the hub, the one
    writer, keeps the sitreps as project notes are saved; "where am I?" and "what next?" read
    them back through Brain Core's own reader and leave Brain Home exactly as it was."""

    from brain_core.records import RECORD_SCHEMA_V2, Record

    from gremlin_town.review.brain_source import brain_task_sitreps_reader
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.orient import OrientationContext, OrientationSources, overview, what_next
    from gremlin_town.review.settings import OrientationSettings, ReviewSettings
    from gremlin_town.review.why_log import WhyLog

    focus = Record(
        schema=RECORD_SCHEMA_V2, id="brn_20260928_020000_eeff", kind="preference", title="Focus projects",
        state="active", created_at=datetime(2026, 9, 28, 2, 0, tzinfo=UTC), updated_at=datetime(2026, 9, 28, 2, 0, tzinfo=UTC),
        assertion=None, confidence=None, sensitivity="private", vaults=(), owners=(), entities=(), evidence=(),
        supersedes=(), dedupe_key=None, data={"focus_projects": [GIT]}, body="",
        source={"said_by": "owner", "came_by": "direct", "words": "focus on the git fixture", "recorded_by": "hermes-wa"},
        links=(),
    )
    with hub_for(tmp_path) as hub:
        hub.start("task-a", "Draft the sitrep spec", "Write the tests")
        hub.start("task-b", "Check the connector", "Ask Nic")
        hub.append("task-b", "blocker", "The PC connector is old", key="b1")
        before = dict(hub.tree())
        why_log_file = tmp_path / "why.sqlite3"
        why_log = WhyLog(why_log_file)
        context = OrientationContext(
            orientation_settings=OrientationSettings.from_table({"enabled": True}, review_enabled=True),
            review_settings=ReviewSettings.from_table({"enabled": True, "why_log_file": str(why_log_file)}),
            why_log=why_log,
            sources=OrientationSources(
                review=ReviewSources(brain_items=lambda: (), open_notes=lambda: (),
                                     calendar_events=lambda start, end: (), open_requests=lambda: ()),
                preferences=lambda: (focus,),
                task_sitreps=brain_task_sitreps_reader(LANE_B.storage(hub.remote)),
            ),
        )
        try:
            picture = overview(context, datetime.now(UTC))
            top, section = picture["sections"]
            assert top["section"] == "highest_right_now"
            texts = [line["text"] for line in section["lines"]]
            assert section["section"] == "focus_projects"
            assert texts[0].startswith("vault_git_fixture — summary as of ")
            assert sorted(text.split(";")[0] for text in texts[1:]) == [
                "Check the connector — blocked: The PC connector is old", "Draft the sitrep spec — next: Write the tests",
            ]
            answer = what_next(context, datetime.now(UTC))
            assert answer["top_pick"]["record_id"] == f"project:{GIT}:task-a"
            assert answer["top_pick"]["first_step"] == "Write the tests"
            assert [entry["reason"] for entry in answer["can_wait"]] == ["blocked: The PC connector is old"]
            assert hub.tree() == before  # read-only: no retry, no archive, no write
        finally:
            why_log.close()


def test_summary_day_uses_the_current_personal_day():
    from gremlin_town.mcp.sitreps import _summary_line
    from datetime import timedelta
    saved = datetime(2026, 10, 7, 22, tzinfo=UTC)
    assert _summary_line(saved, pc_off=True, now=saved + timedelta(days=1)).startswith("summary as of yesterday;")
    assert _summary_line(saved, pc_off=True, now=datetime(2026, 10, 8, 19, tzinfo=UTC)).startswith("summary as of today;")
    assert _summary_line(saved, pc_off=True).startswith("summary as of Thu 8 Oct;")
