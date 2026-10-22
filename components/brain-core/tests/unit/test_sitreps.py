"""Task sitreps and Focus copies (docs/product/steps/b-project-sitreps.md), Brain Core half.

The record kinds' validation and the pure helpers the hub builds them with: only the listed
orientation fields, each one short line; a project sitrep worked out from the task sitreps and
the Focus copy; closed chains due for step C's archive path after 30 days.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from brain_core import sitreps
from brain_core.errors import BrainError, ErrorCode
from brain_core.records import (
    SITREP_FIELD_MAX_CHARS,
    Evidence,
    parse_record,
    record_from_mapping,
    render_record,
)

NOW = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)
HASH_A = hashlib.sha256(b"note version a").hexdigest()
HASH_B = hashlib.sha256(b"note version b").hexdigest()
NOTE_A = "brn_20260928_095000_aaaa0001"
NOTE_B = "brn_20260928_095500_bbbb0002"
PROJECT = "vault_exosuit_fixture"
HUB = Evidence(
    source_type="gremlin_town_hub",
    locator="gt-hub://sitreps/fixture",
    content_sha256=hashlib.sha256(b"hub").hexdigest(),
    excerpt="Written by the hub from the project note; not the note itself.",
    actor="system:sitreps",
)


def _start(session_id: str = "task-a", *, intent: str = "Draft the sitrep spec", next_step: str = "Write the tests",
           at: datetime = NOW - timedelta(minutes=10), project: str = PROJECT) -> sitreps.TaskSitrep:
    return sitreps.start_sitrep(
        project=project, session_id=session_id, intent=intent, next_step=next_step,
        note_record_id=NOTE_A, note_content_sha256=HASH_A, note_updated_at=at,
    )


def _record(sitrep: sitreps.TaskSitrep, previous=None, *, now: datetime = NOW):
    return sitreps.sitrep_record(
        sitrep, previous=previous, now=now, evidence=HUB, owner_id="brain-home", owner_locator="vault://brain-home",
    )


def _focus(line: str | None, previous=None, *, now: datetime = NOW, project: str = PROJECT):
    return sitreps.focus_copy_record(
        project=project, line=line, section_sha256=None if line is None else HASH_A, previous=previous, now=now,
        evidence=HUB, owner_id="brain-home", owner_locator="vault://brain-home",
    )


def _raw(kind: str = "sitrep", **changes: object) -> dict[str, object]:
    record = _record(_start()) if kind == "sitrep" else _focus("Ship the thing.")
    value = record.as_dict()
    value.update(changes)
    return value


# --- record kinds -----------------------------------------------------------------------------


def test_sitrep_and_focus_copy_records_round_trip_as_deterministic_markdown() -> None:
    for record in (_record(_start()), _focus("Ship the thing."), _focus(None)):
        text = render_record(record)
        assert parse_record(text, record.kind) == record
        assert render_record(parse_record(text, record.kind)) == text


def test_task_sitrep_holds_only_the_listed_orientation_fields() -> None:
    record = _record(_start())
    assert set(record.data) == {"project", "session_id", "intent", "next_step", "blockers", "note_updated_at", "note"}
    assert set(record.data["note"]) == {"record_id", "content_sha256"}
    assert record.assertion == "system" and record.kind == "sitrep" and record.state == "open"
    for smuggled in ("effects", "sources", "decisions", "candidates", "chat"):
        data = {**record.data, smuggled: ["anything"]}
        with pytest.raises(BrainError) as refused:
            record_from_mapping(_raw(data=data))
        assert refused.value.details["field"] == "data"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"intent": "x" * (SITREP_FIELD_MAX_CHARS + 1)}, "data.intent"),
        ({"intent": "two\nlines"}, "data.intent"),
        ({"intent": ""}, "data.intent"),
        ({"next_step": "y" * (SITREP_FIELD_MAX_CHARS + 1)}, "data.next_step"),
        ({"project": ""}, "data.project"),
        ({"blockers": ["a", "b", "c", "d"]}, "data.blockers"),
        ({"blockers": ["z" * (SITREP_FIELD_MAX_CHARS + 1)]}, "data.blockers[]"),
        ({"note_updated_at": "not a time"}, "data.note_updated_at"),
        ({"note": {"record_id": "bad", "content_sha256": HASH_A}}, "data.note.record_id"),
        ({"note": {"record_id": NOTE_A, "content_sha256": "short"}}, "data.note.content_sha256"),
        ({"note": {"record_id": NOTE_A, "content_sha256": HASH_A, "text": "chat"}}, "data.note"),
    ],
)
def test_sitrep_fields_are_capped_single_lines(change: dict[str, object], field: str) -> None:
    data = {**_record(_start()).data, **change}
    with pytest.raises(BrainError) as refused:
        record_from_mapping(_raw(data=data))
    assert refused.value.code is ErrorCode.CONTRACT_INVALID
    assert refused.value.details["field"] == field


def test_an_empty_next_step_is_allowed() -> None:
    data = {**_record(_start()).data, "next_step": ""}
    assert record_from_mapping(_raw(data=data)).data["next_step"] == ""


@pytest.mark.parametrize("kind", ["sitrep", "focus_copy"])
def test_only_the_hub_can_have_written_them(kind: str) -> None:
    with pytest.raises(BrainError) as refused:
        record_from_mapping(_raw(kind, assertion="explicit"))
    assert refused.value.details["field"] == "assertion"
    v2 = {**_raw(kind), "schema": "brain.record/v2", "source": {"said_by": "agent", "came_by": "system", "recorded_by": "x", "speaker": "y"}}
    v2.pop("assertion")
    v2.pop("confidence")
    with pytest.raises(BrainError) as refused_v2:
        record_from_mapping(v2)
    assert refused_v2.value.details["field"] == "kind"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"line": "x" * (SITREP_FIELD_MAX_CHARS + 1)}, "data.line"),
        ({"line": "Ship it.", "section_sha256": None}, "data.section_sha256"),
        ({"line": None, "section_sha256": "short"}, "data.section_sha256"),
        ({"project": ""}, "data.project"),
        ({"compass": True}, "data"),
    ],
)
def test_focus_copy_holds_one_line_and_its_section_hash(change: dict[str, object], field: str) -> None:
    data = {**_focus("Ship the thing.").data, **change}
    with pytest.raises(BrainError) as refused:
        record_from_mapping(_raw("focus_copy", data=data))
    assert refused.value.details["field"] == field


def test_a_focus_section_without_a_first_sentence_keeps_its_hash() -> None:
    data = {"project": PROJECT, "line": None, "section_sha256": HASH_B}
    assert record_from_mapping(_raw("focus_copy", data=data)).data["section_sha256"] == HASH_B


# --- building task sitreps --------------------------------------------------------------------


def test_one_line_collapses_whitespace_and_caps_with_an_ellipsis() -> None:
    assert sitreps.one_line("  a\n\tb  c ") == "a b c"
    capped = sitreps.one_line("word " * 100)
    assert len(capped) == SITREP_FIELD_MAX_CHARS and capped.endswith("…")
    assert sitreps.one_line("x" * SITREP_FIELD_MAX_CHARS) == "x" * SITREP_FIELD_MAX_CHARS
    with pytest.raises(BrainError) as refused:
        sitreps.one_line(None)  # type: ignore[arg-type]
    assert refused.value.code is ErrorCode.INVALID_ARGUMENT


def test_start_append_and_close_move_the_sitrep_and_its_pointer() -> None:
    started = _start(intent="Draft\nthe  spec", next_step="Write " + "t" * 300)
    assert started.state == "open" and started.intent == "Draft the spec"
    assert len(started.next_step) == SITREP_FIELD_MAX_CHARS
    later = NOW - timedelta(minutes=5)
    pointer = {"note_record_id": NOTE_B, "note_content_sha256": HASH_B, "note_updated_at": later}

    blocked = sitreps.apply_event(started, event="blocker", value="Waiting on the connector", **pointer)
    assert blocked.state == "blocked" and blocked.blockers == ("Waiting on the connector",)
    assert (blocked.note_record_id, blocked.note_content_sha256, blocked.note_updated_at) == (NOTE_B, HASH_B, later)

    again = sitreps.apply_event(blocked, event="blocker", value="Waiting on the connector", **pointer)
    assert again.blockers == ("Waiting on the connector",)
    for value in ("Two", "Three", "Four"):
        again = sitreps.apply_event(again, event="blocker", value=value, **pointer)
    assert again.blockers == ("Waiting on the connector", "Two", "Three")

    moving = sitreps.apply_event(again, event="next_step", value="Try the new route", **pointer)
    assert moving.state == "open" and moving.next_step == "Try the new route"
    assert moving.blockers == again.blockers

    renamed = sitreps.apply_event(moving, event="intent", value="Ship sitreps", **pointer)
    assert renamed.intent == "Ship sitreps" and renamed.state == "open"

    for event in ("source", "effect", "decision", "commitment", "candidate"):
        moved = sitreps.apply_event(renamed, event=event, value="SECRET CHAT TEXT", **pointer)
        assert "SECRET CHAT TEXT" not in repr(moved.data())
        assert moved == renamed

    recovered = sitreps.close_sitrep(blocked, recovered=True, **pointer)
    assert recovered.state == "blocked"
    closed = sitreps.close_sitrep(renamed, recovered=False, **pointer)
    assert closed.state == "closed"
    assert sitreps.close_sitrep(closed, recovered=False, **pointer).state == "closed"
    with pytest.raises(BrainError) as refused:
        sitreps.apply_event(closed, event="next_step", value="more", **pointer)
    assert refused.value.code is ErrorCode.RECORD_TRANSITION


def test_unknown_events_and_naive_times_are_refused() -> None:
    started = _start()
    with pytest.raises(BrainError) as unknown:
        sitreps.apply_event(started, event="chat", value="x", note_record_id=NOTE_B, note_content_sha256=HASH_B,
                            note_updated_at=NOW)
    assert unknown.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(BrainError) as naive:
        _start(at=datetime(2026, 9, 28, 10, 0))
    assert naive.value.details["field"] == "note_updated_at"


def test_seed_takes_the_first_three_distinct_blockers_of_a_whole_note() -> None:
    seeded = sitreps.seed_sitrep(
        project=PROJECT, session_id="task-a", intent="Draft", next_step="Next",
        blockers=["One", "One", "", "Two", "Three", "Four"],
        note_record_id=NOTE_A, note_content_sha256=HASH_A, note_updated_at=NOW,
    )
    assert seeded.state == "open" and seeded.blockers == ("One", "Two", "Three")


# --- records ------------------------------------------------------------------------------------


def test_each_version_supersedes_only_the_one_before_it_and_keeps_one_evidence_item() -> None:
    first = _record(_start())
    second_sitrep = sitreps.apply_event(_start(), event="blocker", value="Stuck", note_record_id=NOTE_B,
                                        note_content_sha256=HASH_B, note_updated_at=NOW)
    second = _record(second_sitrep, first, now=NOW + timedelta(seconds=1))
    third = _record(sitreps.close_sitrep(second_sitrep, recovered=False, note_record_id=NOTE_B,
                                         note_content_sha256=HASH_B, note_updated_at=NOW),
                    second, now=NOW + timedelta(seconds=2))
    assert second.supersedes == (first.id,) and third.supersedes == (second.id,)
    assert len(third.evidence) == 1 and third.evidence[0].actor == "system:sitreps"
    assert third.dedupe_key == f"sitrep:{PROJECT}:task-a"
    assert _record(_start()) == first  # deterministic for the same input and time
    assert sitreps.TaskSitrep.from_record(second) == second_sitrep
    assert sitreps.same_sitrep(second, second_sitrep) and not sitreps.same_sitrep(first, second_sitrep)
    assert not sitreps.same_sitrep(_focus("x"), second_sitrep)


def test_a_previous_record_from_another_task_or_kind_is_refused() -> None:
    other = _record(_start("task-b"))
    with pytest.raises(BrainError) as refused:
        _record(_start("task-a"), other)
    assert refused.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(BrainError):
        _focus("x", other)
    with pytest.raises(BrainError):
        _focus("x", _focus("y", project="vault_other"))
    with pytest.raises(BrainError):
        sitreps.TaskSitrep.from_record(_focus("x"))
    with pytest.raises(BrainError) as naive:
        sitreps.sitrep_record(_start(), previous=None, now=datetime(2026, 9, 28), evidence=HUB,
                              owner_id="brain-home", owner_locator="vault://brain-home")
    assert naive.value.details["field"] == "now"


def test_focus_copy_versions_chain_and_the_newest_one_counts() -> None:
    first = _focus("Old focus.")
    second = _focus("New focus.", first, now=NOW + timedelta(seconds=1))
    assert second.supersedes == (first.id,) and second.state == "current"
    assert sitreps.current_focus_copy((first, second), PROJECT) == second
    assert sitreps.current_focus_copy((first, second), "vault_other") is None


# --- reading them back ------------------------------------------------------------------------


def _history() -> tuple[list, dict[str, object]]:
    """task-a open (two versions), task-b blocked, task-c closed, plus another project's task."""

    a1 = _record(_start("task-a", at=NOW - timedelta(minutes=30)), now=NOW - timedelta(minutes=30))
    a2_sitrep = sitreps.apply_event(sitreps.TaskSitrep.from_record(a1), event="next_step", value="Second step",
                                    note_record_id=NOTE_B, note_content_sha256=HASH_B, note_updated_at=NOW - timedelta(minutes=3))
    a2 = _record(a2_sitrep, a1, now=NOW - timedelta(minutes=3))
    b_start = _start("task-b", intent="Check the route", at=NOW - timedelta(minutes=20))
    b1 = _record(sitreps.apply_event(b_start, event="blocker", value="PC connector is old", note_record_id=NOTE_B,
                                     note_content_sha256=HASH_B, note_updated_at=NOW - timedelta(minutes=20)),
                 now=NOW - timedelta(minutes=20))
    c1 = _record(sitreps.close_sitrep(_start("task-c", intent="Finished thing", at=NOW - timedelta(minutes=1)),
                                      recovered=False, note_record_id=NOTE_B, note_content_sha256=HASH_B,
                                      note_updated_at=NOW - timedelta(minutes=1)), now=NOW - timedelta(minutes=1))
    other = _record(_start("task-x", project="vault_other"), now=NOW)
    return [a1, a2, b1, c1, other], {"a2": a2, "b1": b1, "c1": c1}


def test_current_sitreps_keep_the_newest_version_of_each_task_in_the_project() -> None:
    records, named = _history()
    heads = sitreps.current_sitreps(records, PROJECT)
    assert heads == {"task-a": named["a2"], "task-b": named["b1"], "task-c": named["c1"]}


def test_list_shows_open_and_blocked_tasks_newest_first_never_closed() -> None:
    records, _ = _history()
    listed = sitreps.sitrep_list(records, PROJECT, limit=20)
    assert [(item["session_id"], item["state"]) for item in listed] == [("task-a", "open"), ("task-b", "blocked")]
    assert listed[0] == {
        "record_id": NOTE_B,
        "session_id": "task-a",
        "intent": "Draft the sitrep spec",
        "next_step": "Second step",
        "state": "open",
        "updated_at": (NOW - timedelta(minutes=3)).isoformat(),
        "blockers": [],
    }
    assert listed[1]["blockers"] == ["PC connector is old"]
    assert [item["session_id"] for item in sitreps.sitrep_list(records, PROJECT, limit=1)] == ["task-a"]
    for limit in (0, 101, True):
        with pytest.raises(BrainError):
            sitreps.sitrep_list(records, PROJECT, limit=limit)  # type: ignore[arg-type]


def test_project_sitrep_assembles_from_task_sitreps_and_the_focus_copy() -> None:
    records, _ = _history()
    focus = _focus("Make Exosuit the verified home for GT and Brain Core.", now=NOW - timedelta(minutes=40))
    view = sitreps.project_sitrep([*records, focus], PROJECT)
    assert view == {
        "project": PROJECT,
        "focus": {"line": "Make Exosuit the verified home for GT and Brain Core.",
                  "copied_at": (NOW - timedelta(minutes=40)).isoformat()},
        "open": [{"session_id": "task-a", "intent": "Draft the sitrep spec", "next_step": "Second step",
                  "updated_at": (NOW - timedelta(minutes=3)).isoformat()}],
        "open_total": 1,
        "blocked": [{"session_id": "task-b", "intent": "Check the route", "blocker": "PC connector is old",
                     "updated_at": (NOW - timedelta(minutes=20)).isoformat()}],
        "blocked_total": 1,
        "latest_activity": {"session_id": "task-c", "intent": "Finished thing", "state": "closed",
                            "updated_at": (NOW - timedelta(minutes=1)).isoformat()},
        "as_of": (NOW - timedelta(minutes=1)).isoformat(),
    }


def test_project_sitrep_leaves_focus_out_when_the_vault_has_no_compass() -> None:
    records, _ = _history()
    for extra in ([], [_focus(None)]):
        view = sitreps.project_sitrep([*records, *extra], PROJECT)
        assert "focus" not in view
    empty = sitreps.project_sitrep([], PROJECT)
    assert empty == {"project": PROJECT, "open": [], "open_total": 0, "blocked": [], "blocked_total": 0,
                     "latest_activity": None, "as_of": None}
    assert sitreps.project_sitrep([_focus("Only focus.")], PROJECT)["as_of"] == NOW.isoformat()


def test_open_tasks_by_project_gives_step_f_every_projects_open_and_blocked_tasks_and_its_as_of() -> None:
    """What "where am I?" and "what next?" read (step F §4): per project, its open and blocked
    tasks (newest note first, closed ones left out) and the same "as of" as its project sitrep."""

    records, named = _history()
    focus_only = _focus("Only focus.", project="vault_focus_only", now=NOW - timedelta(hours=2))
    projects = sitreps.open_tasks_by_project([*records, focus_only])
    assert [project.project for project in projects] == ["vault_exosuit_fixture", "vault_focus_only", "vault_other"]
    fixture, focus_project, other = projects
    assert [(task.session_id, task.state) for task in fixture.tasks] == [("task-a", "open"), ("task-b", "blocked")]
    assert fixture.tasks[0] == sitreps.TaskSitrep.from_record(named["a2"])
    assert fixture.as_of.isoformat() == sitreps.project_sitrep(records, PROJECT)["as_of"] == (NOW - timedelta(minutes=1)).isoformat()
    assert (focus_project.tasks, focus_project.as_of) == ((), NOW - timedelta(hours=2))
    assert [task.session_id for task in other.tasks] == ["task-x"] and other.as_of == NOW
    assert sitreps.open_tasks_by_project([]) == ()


def test_project_sitrep_shows_at_most_ten_open_tasks_and_counts_the_rest() -> None:
    records = [_record(_start(f"task-{index:02d}", at=NOW - timedelta(minutes=index))) for index in range(12)]
    blocked = [
        _record(sitreps.apply_event(_start(f"blocked-{index:02d}"), event="blocker", value="Stuck", note_record_id=NOTE_B,
                                    note_content_sha256=HASH_B, note_updated_at=NOW - timedelta(minutes=index)))
        for index in range(11)
    ]
    view = sitreps.project_sitrep([*records, *blocked], PROJECT)
    assert [item["session_id"] for item in view["open"]] == [f"task-{index:02d}" for index in range(10)]
    assert view["open_total"] == 12 and len(view["blocked"]) == 10 and view["blocked_total"] == 11


def test_a_closed_task_sitrep_is_due_for_the_archive_after_thirty_days_with_every_version() -> None:
    closed_at = NOW - timedelta(days=30)
    first = _record(_start("task-old", at=closed_at - timedelta(days=1)), now=closed_at - timedelta(days=1))
    closed = _record(sitreps.close_sitrep(sitreps.TaskSitrep.from_record(first), recovered=False, note_record_id=NOTE_B,
                                          note_content_sha256=HASH_B, note_updated_at=closed_at),
                     first, now=closed_at)
    recent = _record(sitreps.close_sitrep(_start("task-recent", at=NOW - timedelta(days=29)), recovered=False,
                                          note_record_id=NOTE_B, note_content_sha256=HASH_B,
                                          note_updated_at=NOW - timedelta(days=29)))
    still_open = _record(_start("task-open", at=NOW - timedelta(days=90)))
    records = [first, closed, recent, still_open, _focus("x")]
    assert sitreps.archive_due(records, now=NOW) == tuple(sorted((first, closed), key=lambda record: record.id))
    assert sitreps.archive_due(records, now=NOW - timedelta(seconds=1)) == ()
    with pytest.raises(BrainError):
        sitreps.archive_due(records, now=datetime(2026, 9, 28))


# --- the Focus line ---------------------------------------------------------------------------


COMPASS = """<!-- Vault OS v4: declared intent only. -->

# Compass

*Updated: 2026-09-28*

## Focus

Make Exosuit the verified development home for GT and Brain Core, and the home of Ciel's product design. Preserve
source and product evidence before relocation.

### Detail

More focus detail.

## Questions

1. Which releases?
"""


def test_focus_section_is_the_text_under_focus_up_to_the_next_same_level_heading() -> None:
    section = sitreps.focus_section(COMPASS)
    assert section is not None
    assert section.startswith("Make Exosuit the verified development home")
    assert "### Detail" in section and "More focus detail." in section
    assert "Questions" not in section
    assert sitreps.focus_section("# Compass\n\n## Questions\n\n1. x\n") is None
    assert sitreps.focus_section("## Focus\n\n## Questions\n") is None
    assert sitreps.focus_section("## focus\nLower case heading.") == "Lower case heading."
    with pytest.raises(BrainError):
        sitreps.focus_section(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("section", "line"),
    [
        (sitreps.focus_section(COMPASS),
         "Make Exosuit the verified development home for GT and Brain Core, and the home of Ciel's product design."),
        ("Ship the API, e.g. the list route. Then the rest.", "Ship the API, e.g. the list route."),
        ("- First bullet goal\n  continues here\n- Second bullet", "First bullet goal continues here"),
        ("1. Numbered goal! Other text.", "Numbered goal!"),
        ("> Quoted focus line? Yes.", "Quoted focus line?"),
        ("No sentence end at all", "No sentence end at all"),
        ("\n\nSecond paragraph wins when the first is blank.", "Second paragraph wins when the first is blank."),
        ("word " * 80, ("word " * 80).strip()[: SITREP_FIELD_MAX_CHARS - 1].rstrip() + "…"),
        (None, None),
        ("   ", None),
    ],
)
def test_focus_line_is_the_first_sentence_on_one_capped_line(section: str | None, line: str | None) -> None:
    assert sitreps.focus_line(section) == line
