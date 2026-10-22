from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import brain_core.continuity as continuity
from brain_core.continuity import append_event, close_continuity, list_continuity, recover_continuity, start_continuity
from brain_core.errors import BrainError, ErrorCode
from brain_core.records import Evidence, RecordStore

NOW = datetime(2026, 8, 29, 2, 0, 0, tzinfo=UTC)


def evidence(locator: str = "message:1") -> Evidence:
    return Evidence(
        source_type="session_message",
        locator=locator,
        content_sha256=hashlib.sha256(locator.encode()).hexdigest(),
        excerpt=locator,
        session_id="session-a",
        actor="nic",
    )


def test_truncation_handles_unchanged_and_empty_budgets() -> None:
    assert continuity._truncate("one", 1) == "one"
    assert continuity._truncate("one two", 0) == ""


def test_incremental_continuity_survives_without_clean_shutdown(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    started = start_continuity(
        store,
        session_id="session-a",
        intent="Implement Phase 2",
        sources=["plan", "decision"],
        evidence=evidence(),
        now=NOW,
        entropy="a1b2",
    )
    assert started.changed and started.receipt is not None
    current = started
    for index, (event, value) in enumerate(
        [
            ("effect", "Installed restic"),
            ("decision", "Phase 2 approved"),
            ("candidate", "Possible follow-up"),
            ("commitment", "Complete P2A"),
            ("blocker", "B2 credentials missing"),
            ("next_step", "Build record core"),
        ],
        start=2,
    ):
        current = append_event(
            store,
            session_id="session-a",
            event=event,
            value=value,
            evidence=evidence(f"message:{index}"),
            now=NOW + timedelta(seconds=index),
            entropy=f"{index:04x}",
        )
        assert current.changed
    recovered = recover_continuity(RecordStore(tmp_path))
    assert recovered["intent"] == "Implement Phase 2"
    assert recovered["effects"] == ["Installed restic"]
    assert recovered["decisions"] == ["Phase 2 approved"]
    assert recovered["commitments"] == ["Complete P2A"]
    assert recovered["candidates"] == ["Possible follow-up"]
    assert recovered["blockers"] == ["B2 credentials missing"]
    assert recovered["next_step"] == "Build record core"
    assert recovered["tokens"] <= 2_500


def test_continuity_dedupes_noops_and_rejects_unknown_event(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    started = start_continuity(
        store,
        session_id="session-a",
        intent="Intent",
        sources=["same", "same"],
        evidence=evidence(),
        now=NOW,
        entropy="a1b2",
    )
    noop = append_event(
        store,
        session_id="session-a",
        event="effect",
        value="ignored",
        evidence=evidence("message:2"),
        now=NOW + timedelta(seconds=1),
        meaningful=False,
    )
    assert not noop.changed and noop.record == started.record
    first = append_event(
        store,
        session_id="session-a",
        event="source",
        value="same",
        evidence=evidence("message:3"),
        now=NOW + timedelta(seconds=2),
    )
    assert not first.changed
    same_intent = append_event(
        store,
        session_id="session-a",
        event="intent",
        value="Intent",
        evidence=evidence("message:4"),
        now=NOW + timedelta(seconds=3),
    )
    assert not same_intent.changed
    with pytest.raises(BrainError) as raised:
        append_event(
            store,
            session_id="session-a",
            event="unknown",
            value="x",
            evidence=evidence("message:5"),
            now=NOW + timedelta(seconds=4),
        )
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_duplicate_session_missing_session_and_closed_session_fail(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    start_continuity(
        store,
        session_id="session-a",
        intent="Intent",
        sources=[],
        evidence=evidence(),
        now=NOW,
        entropy="a1b2",
    )
    with pytest.raises(BrainError) as raised:
        start_continuity(
            store,
            session_id="session-a",
            intent="Again",
            sources=[],
            evidence=evidence(),
            now=NOW,
        )
    assert raised.value.code is ErrorCode.RECORD_EXISTS
    with pytest.raises(BrainError) as raised:
        append_event(
            store,
            session_id="missing",
            event="effect",
            value="x",
            evidence=evidence(),
            now=NOW,
        )
    assert raised.value.code is ErrorCode.CONTINUITY_EMPTY
    closed = close_continuity(
        store,
        session_id="session-a",
        evidence=evidence("message:close"),
        now=NOW + timedelta(seconds=1),
        entropy="b2c3",
    )
    assert closed.record.state == "closed"
    with pytest.raises(BrainError) as raised:
        append_event(
            store,
            session_id="session-a",
            event="effect",
            value="late",
            evidence=evidence(),
            now=NOW + timedelta(seconds=2),
        )
    assert raised.value.code is ErrorCode.RECORD_TRANSITION
    with pytest.raises(BrainError) as raised:
        recover_continuity(store)
    assert raised.value.code is ErrorCode.CONTINUITY_EMPTY


def test_recovered_continuity_can_close(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    start_continuity(
        store,
        session_id="session-a",
        intent="Intent",
        sources=[],
        evidence=evidence(),
        now=NOW,
        entropy="a1b2",
    )
    recovered = close_continuity(
        store,
        session_id="session-a",
        evidence=evidence("recover"),
        now=NOW + timedelta(seconds=1),
        recovered=True,
        entropy="b2c3",
    )
    assert recovered.record.state == "recovered"
    closed = close_continuity(
        store,
        session_id="session-a",
        evidence=evidence("close"),
        now=NOW + timedelta(seconds=2),
        entropy="c3d4",
    )
    assert closed.record.state == "closed"


def test_recovery_budget_drops_old_items_and_truncates_scalars(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    long = " ".join(f"word{i}" for i in range(300))
    start_continuity(
        store,
        session_id="session-a",
        intent=long,
        sources=[long, long + " source"],
        evidence=evidence(),
        now=NOW,
        next_step=long,
        entropy="a1b2",
    )
    brief = recover_continuity(store, max_tokens=80)
    assert brief["tokens"] <= 80
    assert brief["dropped"]["sources"] == 2
    assert brief["intent"].endswith("…")
    with pytest.raises(BrainError) as raised:
        recover_continuity(store, max_tokens=79)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_recover_by_session_returns_that_tasks_note(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    start_continuity(
        store, session_id="session-a", intent="Ship step A", sources=[],
        evidence=evidence(), now=NOW, entropy="a1b2",
    )
    start_continuity(
        store, session_id="session-b", intent="Ship step B", sources=[],
        evidence=evidence(), now=NOW + timedelta(seconds=1), entropy="c3d4",
    )

    latest = recover_continuity(store)
    assert latest["session_id"] == "session-b"

    by_session = recover_continuity(store, session_id="session-a")
    assert by_session["session_id"] == "session-a"
    assert by_session["intent"] == "Ship step A"

    with pytest.raises(BrainError) as raised:
        recover_continuity(store, session_id="missing-session")
    assert raised.value.code is ErrorCode.CONTINUITY_EMPTY


def test_continuity_list_returns_open_notes_newest_first(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    start_continuity(
        store, session_id="session-a", intent="Ship step A", sources=[],
        evidence=evidence(), now=NOW, next_step="Write the spec", entropy="a1b2",
    )
    start_continuity(
        store, session_id="session-b", intent="Ship step B", sources=[],
        evidence=evidence(), now=NOW + timedelta(seconds=1), next_step="Build the wheel", entropy="c3d4",
    )
    start_continuity(
        store, session_id="session-c", intent="Ship step C", sources=[],
        evidence=evidence(), now=NOW + timedelta(seconds=2), next_step="Write tests", entropy="e5f6",
    )
    close_continuity(store, session_id="session-c", evidence=evidence("close"), now=NOW + timedelta(seconds=3), entropy="f6a7")

    notes = list_continuity(store)
    assert [note["session_id"] for note in notes] == ["session-b", "session-a"]
    assert notes[0]["intent"] == "Ship step B"
    assert notes[0]["next_step"] == "Build the wheel"
    assert notes[0]["updated_at"] >= notes[1]["updated_at"]

    bounded = list_continuity(store, limit=1)
    assert [note["session_id"] for note in bounded] == ["session-b"]

    with pytest.raises(BrainError) as raised:
        list_continuity(store, limit=0)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


# -- N12b/N12c (Ciel redesign, 28 Sep): a note names its task, and may start with no first step --

TASK_ID = "brn_20260828_120000_abcd1234"


def test_a_note_names_its_task_and_list_and_recover_carry_it(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    started = start_continuity(
        store, session_id="call-the-bank", intent="Call the bank", sources=[],
        evidence=evidence(), now=NOW, next_step="Find the card", entropy="a1b2", task_id=TASK_ID,
    )
    assert started.record.data["task_id"] == TASK_ID
    start_continuity(
        store, session_id="session-b", intent="No task named", sources=[],
        evidence=evidence(), now=NOW + timedelta(seconds=1), entropy="c3d4",
    )
    # An append keeps the link: a note keeps naming its task however it changes.
    appended = append_event(
        store, session_id="call-the-bank", event="next_step", value="Call the number on the back",
        evidence=evidence("message:2"), now=NOW + timedelta(seconds=2), entropy="e5f6",
    )
    assert appended.record.data["task_id"] == TASK_ID

    notes = {note["session_id"]: note for note in list_continuity(store)}
    assert notes["call-the-bank"]["task_id"] == TASK_ID
    assert notes["session-b"]["task_id"] is None

    linked = recover_continuity(store, session_id="call-the-bank")
    assert linked["task_id"] == TASK_ID
    unlinked = recover_continuity(store, session_id="session-b")
    assert "task_id" not in unlinked  # a note that names no task recovers exactly as before
    assert "task_id" not in RecordStore(tmp_path).get(unlinked["record_id"]).data


def test_a_task_id_must_be_a_record_id_and_a_refusal_writes_nothing(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    for bad in ("call-the-bank", "brn_2026_x", ""):
        with pytest.raises(BrainError) as raised:
            start_continuity(
                store, session_id="session-a", intent="Intent", sources=[],
                evidence=evidence(), now=NOW, entropy="a1b2", task_id=bad,
            )
        assert raised.value.code is ErrorCode.CONTRACT_INVALID
    assert store.all() == ()


def test_a_note_may_start_with_no_next_step(tmp_path: Path) -> None:
    """N12c: a yes with no first step leaves the next step empty, never a placeholder."""

    store = RecordStore(tmp_path)
    start_continuity(
        store, session_id="item-" + TASK_ID, intent="Call the bank", sources=[],
        evidence=evidence(), now=NOW, next_step="", entropy="a1b2", task_id=TASK_ID,
    )
    assert recover_continuity(store)["next_step"] == ""
    assert list_continuity(store)[0]["next_step"] == ""
    # Once a first step is given it is appended like any other.
    append_event(
        store, session_id="item-" + TASK_ID, event="next_step", value="Find the card",
        evidence=evidence("message:2"), now=NOW + timedelta(seconds=1), entropy="c3d4",
    )
    assert recover_continuity(store)["next_step"] == "Find the card"


def test_recovery_budget_holds_for_a_note_linked_to_a_task(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    long = " ".join(f"word{i}" for i in range(300))
    start_continuity(
        store, session_id="session-a", intent=long, sources=[long], evidence=evidence(),
        now=NOW, next_step=long, entropy="a1b2", task_id=TASK_ID,
    )
    brief = recover_continuity(store, max_tokens=80)
    assert brief["tokens"] <= 80
    assert brief["task_id"] == TASK_ID
