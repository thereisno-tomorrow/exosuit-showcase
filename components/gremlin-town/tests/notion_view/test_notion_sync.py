"""Copying Brain Home to Notion one way, against a fake Notion (docs/product/steps/h-notion-view.md §1, §4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gremlin_town.notion_view.client import NotionError
from gremlin_town.notion_view.mapping import PROPERTY_TYPES
from gremlin_town.notion_view.state import ViewState
from gremlin_town.notion_view.sync import (
    LAST_ERROR,
    NotionViewError,
    status,
    sync_once,
    watch,
    watch_once,
)

from test_notion_mapping import NOW, item

DB = "db-1"


@dataclass(frozen=True)
class Revision:
    commit_sha: str


@dataclass(frozen=True)
class File:
    record: Any


@dataclass(frozen=True)
class Read:
    records: tuple[File, ...]


class FakeBrain:
    def __init__(self, records=(), commit="c1"):
        self.records = list(records)
        self.commit = commit
        self.snapshots = 0
        self.limits: list[tuple[int, int]] = []

    def revision(self):
        return Revision(self.commit)

    def snapshot_records(self, *, kinds, revision=None, max_records=512, max_bytes=8 * 1024 * 1024):
        assert kinds == frozenset({"item"})
        self.snapshots += 1
        self.limits.append((max_records, max_bytes))
        return Read(tuple(File(record) for record in self.records))


class FakeNotion:
    def __init__(self, schema=None):
        self.schema = {name: {"type": kind} for name, kind in PROPERTY_TYPES.items()} if schema is None else schema
        self.pages_by_id: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_on: str | None = None
        self._next = 0

    def _maybe_fail(self, name):
        if self.fail_on == name:
            raise NotionError(f"{name} failed", status=500)

    def database(self, database_id):
        assert database_id == DB
        return {"properties": self.schema}

    def create_page(self, database_id, properties):
        self._maybe_fail("create")
        self._next += 1
        page_id = f"page-{self._next}"
        self.pages_by_id[page_id] = {"id": page_id, "archived": False, "properties": properties}
        self.calls.append(("create", page_id))
        return page_id

    def update_page(self, page_id, properties):
        self._maybe_fail("update")
        self.pages_by_id[page_id]["properties"] = properties
        self.calls.append(("update", page_id))

    def archive_page(self, page_id):
        self._maybe_fail("archive")
        self.pages_by_id[page_id]["archived"] = True
        self.calls.append(("archive", page_id))

    def pages(self, database_id):
        return [
            {"id": page["id"], "archived": page["archived"], "properties": {
                "Brain ID": {"rich_text": [{"plain_text": part["text"]["content"]} for part in page["properties"].get("Brain ID", {}).get("rich_text", [])]},
            }}
            for page in self.pages_by_id.values()
        ]

    def live_titles(self):
        return sorted(
            page["properties"]["Name"]["title"][0]["text"]["content"]
            for page in self.pages_by_id.values() if not page["archived"]
        )


@pytest.fixture
def state(tmp_path):
    view_state = ViewState(tmp_path / "view.sqlite3")
    yield view_state
    view_state.close()


def run(brain, notion, state, **kwargs):
    return sync_once(storage=brain, client=notion, state=state, database_id=DB, now=NOW, done_days=14, **kwargs)


def test_first_copy_creates_one_row_per_item(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa", title="one"), item("brn_20260901_000000_bbbb", title="two")])
    notion = FakeNotion()
    result = run(brain, notion, state)
    assert result["created"] == 2 and result["updated"] == 0
    assert notion.live_titles() == ["one", "two"]
    assert status(state)["last_commit"] == "c1"


def test_an_unchanged_copy_sends_nothing(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa")])
    notion = FakeNotion()
    run(brain, notion, state)
    notion.calls.clear()
    result = run(brain, notion, state)
    assert notion.calls == []
    assert result["created"] == result["updated"] == result["archived"] == 0


def test_a_new_version_updates_the_same_row(state):
    first = item("brn_20260901_000000_aaaa", title="old")
    brain = FakeBrain([first])
    notion = FakeNotion()
    run(brain, notion, state)
    brain.records.append(item("brn_20260902_000000_bbbb", title="new", supersedes=(first.id,)))
    brain.commit = "c2"
    result = run(brain, notion, state)
    assert result["updated"] == 1 and result["created"] == 0
    assert notion.live_titles() == ["new"]


def test_an_item_that_leaves_scope_is_archived(state):
    first = item("brn_20260901_000000_aaaa", title="secret soon")
    brain = FakeBrain([first])
    notion = FakeNotion()
    run(brain, notion, state)
    brain.records.append(item("brn_20260902_000000_bbbb", supersedes=(first.id,), sensitivity="restricted"))
    result = run(brain, notion, state)
    assert result["archived"] == 1
    assert notion.live_titles() == []
    assert state.rows() == {}


def test_relations_are_filled_once_every_row_has_a_page(state):
    goal = item("brn_20260901_000000_aaaa", title="goal", data={"size": "goal"})
    task = item("brn_20260902_000000_bbbb", title="task", links=({"type": "part_of", "target": goal.id},))
    notion = FakeNotion()
    run(FakeBrain([goal, task]), notion, state)
    goal_page = state.rows()[goal.id][0]
    task_page = state.rows()[task.id][0]
    assert notion.pages_by_id[task_page]["properties"]["Part of"] == {"relation": [{"id": goal_page}]}


def test_force_resends_every_row(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa"), item("brn_20260901_000000_bbbb")])
    notion = FakeNotion()
    run(brain, notion, state)
    assert run(brain, notion, state, force=True)["updated"] == 2


def test_the_sweep_archives_rows_it_made_but_lost_track_of_and_leaves_hand_typed_rows(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa", title="kept")])
    notion = FakeNotion()
    run(brain, notion, state)
    # A create that failed after Notion made the page, and a row Nic typed himself.
    notion.pages_by_id["orphan"] = {"id": "orphan", "archived": False, "properties": {
        "Name": {"title": [{"text": {"content": "orphan"}}]},
        "Brain ID": {"rich_text": [{"type": "text", "text": {"content": "brn_20260901_000000_aaaa"}}]},
    }}
    notion.pages_by_id["mine"] = {"id": "mine", "archived": False, "properties": {
        "Name": {"title": [{"text": {"content": "typed by Nic"}}]}, "Brain ID": {"rich_text": []},
    }}
    result = run(brain, notion, state, sweep=True)
    assert result["swept"] == 1
    assert notion.live_titles() == ["kept", "typed by Nic"]


def test_a_wrong_database_stops_the_copy_before_any_write(state):
    schema = {name: {"type": kind} for name, kind in PROPERTY_TYPES.items() if name != "Block"}
    notion = FakeNotion(schema=schema)
    with pytest.raises(NotionViewError, match="missing 'Block'"):
        run(FakeBrain([item("brn_20260901_000000_aaaa")]), notion, state)
    assert notion.calls == []
    assert "missing 'Block'" in state.get(LAST_ERROR)
    assert status(state)["last_commit"] is None


def test_a_failed_write_keeps_the_head_uncopied_but_remembers_pages_made(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa")])
    notion = FakeNotion()
    run(brain, notion, state)
    brain.records.append(item("brn_20260901_000000_bbbb"))
    brain.records.append(item("brn_20260902_000000_cccc", supersedes=("brn_20260901_000000_aaaa",), title="changed"))
    brain.commit = "c2"
    notion.fail_on = "update"
    with pytest.raises(NotionError):
        run(brain, notion, state)
    assert status(state)["last_commit"] == "c1"
    assert "brn_20260901_000000_bbbb" in state.rows()  # its page was made; the next copy won't make another
    notion.fail_on = None
    run(brain, notion, state)
    assert status(state)["last_commit"] == "c2" and status(state)["last_error"] is None
    assert len([call for call in notion.calls if call[0] == "create"]) == 2


def test_a_dry_run_needs_no_notion_and_writes_nothing(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa")])
    result = sync_once(storage=brain, client=None, state=state, database_id=None, now=NOW, done_days=14, dry_run=True)
    assert result == {"dry_run": True, "commit": "c1", "rows": 1, "create": 1, "update": 0, "archive": 0}
    assert state.rows() == {} and status(state)["last_success"] is None


def test_a_copy_without_a_key_or_database_refuses(state):
    with pytest.raises(NotionViewError):
        sync_once(storage=FakeBrain(), client=None, state=state, database_id=None, now=NOW, done_days=14)


def check(brain, notion, state, now, picked=frozenset()):
    return watch_once(
        storage=brain, client=notion, state=state, database_id=DB, now=now, done_days=14, repair_hours=24,
        picked=picked,
    )


def test_watch_copies_only_when_the_head_or_picks_move(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa")])
    notion = FakeNotion()
    assert check(brain, notion, state, NOW) is not None  # the first check is the first repair
    assert check(brain, notion, state, NOW + timedelta(minutes=1)) is None
    assert brain.snapshots == 1  # a quiet check reads only the head
    brain.commit = "c2"
    assert check(brain, notion, state, NOW + timedelta(minutes=2)) is not None
    picked = frozenset({"brn_20260901_000000_aaaa"})
    result = check(brain, notion, state, NOW + timedelta(minutes=3), picked=picked)
    assert result is not None and result["updated"] == 1
    assert check(brain, notion, state, NOW + timedelta(minutes=4), picked=picked) is None


def test_watch_repairs_once_a_day(state):
    brain = FakeBrain([item("brn_20260901_000000_aaaa")])
    notion = FakeNotion()
    check(brain, notion, state, NOW)
    assert check(brain, notion, state, NOW + timedelta(hours=23)) is None
    result = check(brain, notion, state, NOW + timedelta(hours=24))
    assert result is not None and result["updated"] == 1


def test_watch_logs_a_failure_and_keeps_checking(state, caplog):
    brain = FakeBrain([item("brn_20260901_000000_aaaa")])
    notion = FakeNotion()
    notion.fail_on = "create"
    ticks = iter([NOW, NOW + timedelta(minutes=1)])
    turns = iter([True, True, False])
    sleeps: list[float] = []

    def clock():
        moment = next(ticks)
        if moment != NOW:
            notion.fail_on = None
        return moment

    watch(
        storage=brain, client=notion, state=state, database_id=DB, done_days=14, repair_hours=24, check_seconds=60,
        picked_today=lambda now: frozenset(), clock=clock, sleep=sleeps.append, keep_going=lambda: next(turns),
    )
    assert "notion view copy failed" in caplog.text
    assert sleeps == [60, 60]
    assert notion.live_titles() == ["a task"]
    assert status(state)["last_error"] is None


def test_the_state_file_survives_reopening(tmp_path):
    path = tmp_path / "view.sqlite3"
    first = ViewState(path)
    first.put("root", "page", "hash")
    first.set("last_commit", "c1")
    first.close()
    again = ViewState(path)
    assert again.rows() == {"root": ("page", "hash")}
    assert again.get("last_commit") == "c1"
    again.close()
