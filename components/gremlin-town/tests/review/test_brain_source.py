"""Brain Home item and open-note reading over `SnapshotSource` (step F spec §8).

Uses a trivial fake satisfying `brain_source.SnapshotSource` structurally - not a fake
GitHub HTTP transport. `GitHubBrainHomeStorage` itself, which the real reader wraps, is
already proven against a fake transport in Brain Core's own
`tests/unit/test_github_storage.py`; these tests prove only this module's own
transformation from `GitHubRecordRead` into `ItemRecord`s and an open-notes mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brain_core.github_storage import GitHubRecordFile, GitHubRecordRead, GitHubRevision
from brain_core.records import RECORD_SCHEMA_V2, Record

from gremlin_town.review.brain_source import (
    brain_items_by_path_reader,
    brain_items_reader,
    brain_preferences_reader,
    brain_task_sitreps_reader,
    open_note_records_reader,
    open_notes_by_task_reader,
    open_notes_reader,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
REVISION = GitHubRevision(repository="trusted/brain-home", branch="accepted", commit_sha="c" * 40, tree_sha="t" * 40)


def item_record(record_id: str, *, title: str = "a task", level: str = "accepted", state: str = "open") -> Record:
    return Record(
        schema=RECORD_SCHEMA_V2,
        id=record_id,
        kind="item",
        title=title,
        state=state,
        created_at=NOW,
        updated_at=NOW,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=(),
        supersedes=(),
        dedupe_key=None,
        data={"level": level, "size": "task", "timing": {}},
        body="",
        source=None,
        links=(),
    )


def continuity_record(record_id: str, *, session_id: str, next_step: str, state: str = "open") -> Record:
    return Record(
        schema="brain.record/v1",
        id=record_id,
        kind="continuity",
        title=f"Continuity for {session_id}",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        assertion="explicit",
        confidence=1.0,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=(),
        supersedes=(),
        dedupe_key=f"continuity:{session_id}",
        data={"session_id": session_id, "intent": "fixture", "next_step": next_step},
        body="",
    )


class FakeSource:
    """Satisfies `brain_source.SnapshotSource` structurally: no HTTP, no GitHub API."""

    def __init__(self, records: tuple[Record, ...], *, paths: dict[str, Record] | None = None) -> None:
        self._records = records
        self._paths = paths or {}
        self.snapshot_calls: list[frozenset[str]] = []
        self.snapshot_limits: list[tuple[int, int | None]] = []
        self.read_calls: list[tuple[str, ...]] = []

    def _path_for(self, record: Record) -> str:
        return f"records/{record.kind}/2026/09/{record.id}.md"

    def snapshot_records(
        self, *, kinds: frozenset[str], max_records: int = 512, max_bytes: int | None = None,
    ) -> GitHubRecordRead:
        self.snapshot_calls.append(kinds)
        self.snapshot_limits.append((max_records, max_bytes))
        matching = tuple(
            GitHubRecordFile(self._path_for(r), b"", "blob-sha", r) for r in self._records if r.kind in kinds
        )
        return GitHubRecordRead(REVISION, matching)

    def read_records(self, paths, *, archived_ok: bool = False) -> GitHubRecordRead:
        paths = tuple(paths)
        self.read_calls.append(paths)
        files = tuple(
            GitHubRecordFile(path, b"", "blob-sha", self._paths[path]) for path in paths if path in self._paths
        )
        return GitHubRecordRead(REVISION, files)


# --- brain_items_reader --------------------------------------------------------------


def test_brain_items_reader_returns_every_item_as_an_item_record():
    task = item_record("brn_task")
    idea = item_record("brn_idea", level="possible")
    note = continuity_record("brn_note", session_id="item-brn_task", next_step="do it")
    source = FakeSource((task, idea, note))

    items = brain_items_reader(source)()

    assert {entry.record.id for entry in items} == {"brn_task", "brn_idea"}
    assert source.snapshot_calls == [frozenset({"item"})]
    winner = next(entry for entry in items if entry.record.id == "brn_task")
    assert winner.path == "records/item/2026/09/brn_task.md"
    assert winner.sha256 == task.sha256


def test_brain_items_reader_returns_nothing_when_the_store_is_empty():
    source = FakeSource(())
    assert brain_items_reader(source)() == ()


# --- brain_items_by_path_reader ------------------------------------------------------


def test_brain_items_by_path_reader_reads_only_the_named_paths():
    task = item_record("brn_task")
    other = item_record("brn_other")
    source = FakeSource(
        (),
        paths={"records/item/2026/09/brn_task.md": task, "records/item/2026/09/brn_other.md": other},
    )

    items = brain_items_by_path_reader(source)(("records/item/2026/09/brn_task.md",))

    assert [entry.record.id for entry in items] == ["brn_task"]
    assert source.read_calls == [("records/item/2026/09/brn_task.md",)]


# --- open_notes_reader -----------------------------------------------------------------


def test_open_notes_reader_maps_session_id_to_next_step_for_open_and_recovered_notes():
    open_note = continuity_record("brn_a", session_id="item-brn_task", next_step="drop the pricing into slide 6")
    recovered_note = continuity_record("brn_b", session_id="item-brn_other", next_step="call the bank", state="recovered")
    closed_note = continuity_record("brn_c", session_id="item-brn_done", next_step="ignored", state="closed")
    task = item_record("brn_task")
    source = FakeSource((open_note, recovered_note, closed_note, task))

    notes = open_notes_reader(source)()

    assert notes == {
        "item-brn_task": "drop the pricing into slide 6",
        "item-brn_other": "call the bank",
    }
    assert source.snapshot_calls == [frozenset({"continuity"})]


def test_open_notes_reader_is_empty_with_no_continuity_records():
    source = FakeSource((item_record("brn_task"),))
    assert open_notes_reader(source)() == {}


# --- N12b (Ciel slice 2): newest versions only, and notes by the task they name ------------


def _version(record_id, *, session_id, next_step, state="open", supersedes=(), task_id=None, minute=0):
    from dataclasses import replace

    note = continuity_record(record_id, session_id=session_id, next_step=next_step, state=state)
    data = dict(note.data)
    if task_id is not None:
        data["task_id"] = task_id
    moment = NOW.replace(minute=minute)
    return replace(note, data=data, supersedes=supersedes, created_at=moment, updated_at=moment)


def test_a_closed_note_no_longer_reads_as_open_through_its_older_versions():
    """Every version of a note stays in Brain Home; before 28 Sep an older, still-open version
    of a note that had since closed was read as open."""

    first = _version("brn_v1", session_id="item-brn_a", next_step="step one", minute=1)
    second = _version("brn_v2", session_id="item-brn_a", next_step="step two", supersedes=("brn_v1",), minute=2)
    closed = _version("brn_v3", session_id="item-brn_a", next_step="step two", state="closed",
                      supersedes=("brn_v1", "brn_v2"), minute=3)
    live = _version("brn_w1", session_id="item-brn_b", next_step="call them", minute=4)
    changed = _version("brn_w2", session_id="item-brn_b", next_step="call them at 3", supersedes=("brn_w1",), minute=5)
    source = FakeSource((first, second, closed, live, changed))

    assert open_notes_reader(source)() == {"item-brn_b": "call them at 3"}
    notes = open_note_records_reader(source)()
    assert [(note.session_id, note.next_step, note.record_id) for note in notes] == [("item-brn_b", "call them at 3", "brn_w2")]
    assert notes[0].updated_at == NOW.replace(minute=5)


def test_open_note_records_carry_the_task_they_name_and_the_by_task_mapping_finds_them():
    linked = _version("brn_n1", session_id="tokyo-trip", next_step="Compare two fares", task_id="brn_flights", minute=1)
    newer = _version("brn_n2", session_id="flights-again", next_step="Book it", task_id="brn_flights", minute=2)
    plain = _version("brn_n3", session_id="item-brn_other", next_step="call the bank", minute=3)
    handed = _version("brn_n4", session_id="gt-req_0123", next_step="Result returned, not yet checked.",
                      task_id="brn_deck", minute=4)
    source = FakeSource((linked, newer, plain, handed, item_record("brn_flights")))

    notes = {note.session_id: note for note in open_note_records_reader(source)()}
    assert {key: note.task_id for key, note in notes.items()} == {
        "tokyo-trip": "brn_flights", "flights-again": "brn_flights", "item-brn_other": None, "gt-req_0123": "brn_deck",
    }
    assert notes["tokyo-trip"].intent == "fixture"
    # By task: the newest of a task's own notes; handed-off work (gt-...) is not an actual person's next step.
    assert open_notes_by_task_reader(source)() == {"brn_flights": "Book it"}
    # The session mapping existing callers use is unchanged in shape.
    assert open_notes_reader(source)()["tokyo-trip"] == "Compare two fares"


# --- Preferences, for Nic's focus projects (N4) ----------------------------------------------


def preference_record(record_id: str, projects: list[str]) -> Record:
    from dataclasses import replace

    base = item_record(record_id)
    return replace(
        base, kind="preference", title="Focus projects", state="active", data={"focus_projects": projects},
        source={"said_by": "owner", "came_by": "direct", "words": "my focus", "recorded_by": "hermes-wa"},
    )


def test_brain_preferences_reader_returns_every_preference_record():
    focus = preference_record("brn_focus", ["vault_exosuit"])
    source = FakeSource((focus, item_record("brn_task")))

    assert brain_preferences_reader(source)() == (focus,)
    assert source.snapshot_calls == [frozenset({"preference"})]


# --- Task sitreps, for Nic's focus projects' tasks (N4, ROA-60) --------------------------------


def _sitrep_records():
    """Two versions of one open task, a blocked task and a closed one in vault_gtbc_plans, and
    another project's Focus copy: what the hub keeps in Brain Home (b-project-sitreps.md)."""

    import hashlib
    from datetime import timedelta

    from brain_core import sitreps
    from brain_core.records import Evidence

    hub = Evidence(source_type="gremlin_town_hub", locator="gt-hub://sitreps/fixture",
                   content_sha256=hashlib.sha256(b"hub").hexdigest(), excerpt="Written by the hub.", actor="system:sitreps")
    sha = hashlib.sha256(b"note").hexdigest()

    def record(sitrep, previous=None, *, minutes: int):
        return sitreps.sitrep_record(sitrep, previous=previous, now=NOW + timedelta(minutes=minutes), evidence=hub,
                                     owner_id="brain-home", owner_locator="vault://brain-home")

    def start(session_id, intent, minutes):
        return sitreps.start_sitrep(project="vault_gtbc_plans", session_id=session_id, intent=intent, next_step="Begin",
                                    note_record_id="brn_20260926_120000_ab12", note_content_sha256=sha,
                                    note_updated_at=NOW + timedelta(minutes=minutes))

    first = record(start("slice-8", "Build slice 8", 1), minutes=1)
    moved = sitreps.apply_event(sitreps.TaskSitrep.from_record(first), event="next_step", value="Write the tests",
                                note_record_id="brn_20260926_120000_ab13", note_content_sha256=sha,
                                note_updated_at=NOW + timedelta(minutes=5))
    second = record(moved, first, minutes=5)
    blocked = record(sitreps.apply_event(start("release", "Package the release", 2), event="blocker",
                                         value="Waiting on the merge", note_record_id="brn_20260926_120000_ab14",
                                         note_content_sha256=sha, note_updated_at=NOW + timedelta(minutes=3)), minutes=3)
    closed = record(sitreps.close_sitrep(start("old", "Old work", 0), recovered=False,
                                         note_record_id="brn_20260926_120000_ab15", note_content_sha256=sha,
                                         note_updated_at=NOW + timedelta(minutes=9)), minutes=9)
    focus = sitreps.focus_copy_record(project="vault_robotics", line="Finish the course.", section_sha256=sha,
                                      previous=None, now=NOW, evidence=hub, owner_id="brain-home",
                                      owner_locator="vault://brain-home")
    return first, second, blocked, closed, focus


def test_brain_task_sitreps_reader_gives_each_projects_open_tasks_and_as_of_reading_only():
    from datetime import timedelta

    first, second, blocked, closed, focus = _sitrep_records()
    source = FakeSource((first, second, blocked, closed, focus, item_record("brn_task")))

    projects = brain_task_sitreps_reader(source, max_records=5000, max_bytes=1024)()

    assert source.snapshot_calls == [frozenset({"sitrep", "focus_copy"})]
    assert source.snapshot_limits == [(5000, 1024)]
    assert source.read_calls == []
    assert [project.project for project in projects] == ["vault_gtbc_plans", "vault_robotics"]
    gtbc, robotics = projects
    assert [(task.session_id, task.state, task.next_step) for task in gtbc.tasks] == [
        ("slice-8", "open", "Write the tests"), ("release", "blocked", "Begin"),
    ]
    assert gtbc.as_of == NOW + timedelta(minutes=9)  # the closed task's summary was written last
    assert (robotics.tasks, robotics.as_of) == ((), NOW)
    assert brain_task_sitreps_reader(FakeSource(()))() == ()


# --- End to end: both asks answer from FakeSource-backed Brain Home, not not_enough ---------


def test_both_asks_answer_from_brain_source_backed_data_instead_of_not_enough():
    """Closes the gap this adapter was built for: with items, notes and preferences wired to a
    real (fake-transport) source, "what next?" picks from real data - its first step from the
    task's stored owner action, with its note retained as history - and "where am I?" shows it in progress."""

    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.orient import OrientationContext, OrientationSources, overview, what_next
    from gremlin_town.review.settings import OrientationSettings, ReviewSettings
    from gremlin_town.review.why_log import WhyLog

    from dataclasses import replace

    due_today = item_record("brn_due", title="pay the electricity bill")
    due_today = replace(due_today, data={"level": "accepted", "size": "task", "timing": {"due": "2026-09-26"},
        "started_at": NOW.isoformat(), "next_action": {"text": "log into the bank app", "done": False}},
        source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"})
    focus_task = replace(item_record("brn_focus_task", title="write the docs"), links=({"type": "about", "target": "vault_exosuit"},))
    note = _version("brn_note", session_id="bills", next_step="log into the bank app", task_id="brn_due")
    source = FakeSource((due_today, focus_task, note, preference_record("brn_focus", ["vault_exosuit"])))

    why_log = WhyLog(":memory:")
    context = OrientationContext(
        orientation_settings=OrientationSettings.from_table({"enabled": True}, review_enabled=True),
        review_settings=ReviewSettings.from_table({"enabled": True, "why_log_file": ":memory:"}),
        why_log=why_log,
        sources=OrientationSources(
            review=ReviewSources(brain_items=brain_items_reader(source), open_notes=open_note_records_reader(source)),
            preferences=brain_preferences_reader(source),
        ),
    )

    answer = what_next(context, NOW)
    assert answer["outcome"] == "answered"
    assert "not_enough" not in answer
    assert answer["top_pick"]["record_id"] == "brn_due"
    assert answer["top_pick"]["first_step"] == "log into the bank app"
    assert [(alt["record_id"], alt["rules"]) for alt in answer["alternatives"]] == [
        ("brn_focus_task", ["focus project: vault_exosuit"]),
    ]
    assert not {"brain: not_configured", "notes: not_configured"} & set(answer["unknowns"])

    picture = overview(context, NOW)
    today = next(section for section in picture["sections"] if section["section"] == "today")
    assert [line["item_ref"] for line in today["lines"]] == ["brn_due"]
    (today_reason,) = [row for row in why_log.full_reason("brn_due")
                       if row.channel == "overview" and row.rule == "overview_today"]
    assert today_reason.fields_used["progress"]["started"] is True
    assert "next: log into the bank app" in today["lines"][0]["text"]
    assert sum(line["item_ref"] == "brn_due" for section in picture["sections"]
               if section["section"] != "highest_right_now" for line in section["lines"]) == 1
    why_log.close()


def test_review_snapshot_pins_items_and_preferences_and_starts_fresh_each_time():
    from gremlin_town.review.brain_source import review_snapshot_reader
    class PinnedSource(FakeSource):
        def __init__(self):
            super().__init__((item_record("fixture-item"),))
            self.head_reads = 0
            self.pins = []
        def revision(self):
            self.head_reads += 1
            return REVISION
        def snapshot_records(self, *, revision=None, **kwargs):
            self.pins.append(revision)
            assert revision is REVISION
            return super().snapshot_records(**kwargs)
    source = PinnedSource()
    read = review_snapshot_reader(source, max_records=123, max_bytes=456)
    first = read()
    second = read()
    assert source.head_reads == 2
    assert source.pins == [REVISION] * 4
    assert source.snapshot_calls == [frozenset({"item"}), frozenset({"preference"})] * 2
    assert source.snapshot_limits == [(123,456)] * 4
    assert first.revision is REVISION and second.revision is REVISION
    assert first.items[0].record.id == "fixture-item"
    assert first.preferences == ()
