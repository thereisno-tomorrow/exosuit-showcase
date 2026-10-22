from __future__ import annotations

from dataclasses import replace
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from brain_core.errors import BrainError, ErrorCode
from brain_core.moments import SIGNALS, find_gap, returnable
from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import CalendarEvent, HistoryRow, ItemRecord
from brain_core.timewords import SINGAPORE

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=SINGAPORE)


def make_item(*, id: str, state: str = "open", title: str = "Task") -> Record:
    return Record(
        schema=RECORD_SCHEMA_V2,
        id=id,
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
        data={"level": "accepted", "size": "task"},
        body="",
        source=None,
        links=(),
    )


def wrap(record: Record) -> ItemRecord:
    return ItemRecord(record=record, path=f"records/item/2026/09/{record.id}.md")


def test_only_the_three_signals_exist() -> None:
    assert SIGNALS == {"pc_on", "task_finished", "calendar_gap"}
    item = wrap(make_item(id="brn_x"))
    with pytest.raises(BrainError) as excinfo:
        returnable((item,), (), "phone_unlock", NOW)
    assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("signal", ["pc_on", "task_finished", "calendar_gap"])
def test_unfinished_pick_never_returns_at_any_signal(signal: str) -> None:
    item = wrap(make_item(id="brn_picked"))
    history = (
        HistoryRow(
            item_ref="brn_picked",
            channel="review",
            rule="picked_today",
            record_sha256=item.sha256,
            record_state="open",
            surfaced_at=NOW - timedelta(hours=2),
            response="picked_today",
            responded_at=NOW - timedelta(hours=2),
        ),
    )
    result = returnable((item,), history, signal, NOW)
    assert result == ()


def test_picked_item_does_not_come_back_twice_same_day_unchanged() -> None:
    item = wrap(make_item(id="brn_picked"))
    history = (
        HistoryRow(
            item_ref="brn_picked",
            channel="review",
            rule="picked_today",
            record_sha256=item.sha256,
            record_state="open",
            surfaced_at=NOW - timedelta(hours=3),
            response="picked_today",
            responded_at=NOW - timedelta(hours=3),
        ),
        HistoryRow(
            item_ref="brn_picked",
            channel="moment",
            rule="picked_today",
            record_sha256=item.sha256,
            record_state="open",
            surfaced_at=NOW - timedelta(hours=1),
        ),
    )
    assert returnable((item,), history, "pc_on", NOW) == ()


def test_renaming_an_unfinished_pick_does_not_make_it_return() -> None:
    item = wrap(make_item(id="brn_picked", title="Renamed"))
    history = (
        HistoryRow(
            item_ref="brn_picked",
            channel="review",
            rule="picked_today",
            record_sha256="stale-sha",
            record_state="open",
            surfaced_at=NOW - timedelta(hours=3),
            response="picked_today",
            responded_at=NOW - timedelta(hours=3),
        ),
        HistoryRow(
            item_ref="brn_picked",
            channel="moment",
            rule="picked_today",
            record_sha256="stale-sha",
            record_state="open",
            surfaced_at=NOW - timedelta(hours=1),
        ),
    )
    assert returnable((item,), history, "pc_on", NOW) == ()


def test_pick_does_not_carry_to_a_later_day() -> None:
    item = wrap(make_item(id="brn_picked"))
    history = (
        HistoryRow(
            item_ref="brn_picked",
            channel="review",
            rule="picked_today",
            record_sha256=item.sha256,
            record_state="open",
            surfaced_at=NOW - timedelta(days=1),
            response="picked_today",
            responded_at=NOW - timedelta(days=1),
        ),
    )
    assert returnable((item,), history, "pc_on", NOW) == ()


def test_waiting_to_open_comes_back() -> None:
    item = wrap(make_item(id="brn_sam", state="open", title="Sam's pricing"))
    history = (
        HistoryRow(
            item_ref="brn_sam",
            channel="review",
            rule="waiting_on_item",
            record_sha256="previous-sha-while-waiting",
            record_state="waiting",
            surfaced_at=NOW - timedelta(hours=5),
        ),
    )
    assert returnable((item,), history, "calendar_gap", NOW) == (item,)


def test_untimed_and_suggested_items_never_return_in_the_day() -> None:
    untimed = wrap(make_item(id="brn_untimed"))
    suggested = wrap(replace(make_item(id="brn_sugg"), data={"level": "suggested"}))
    assert returnable((untimed, suggested), (), "pc_on", NOW) == ()


def test_due_time_passing_with_no_signal_returns_nothing() -> None:
    item = wrap(make_item(id="brn_overdue"))
    for signal in SIGNALS:
        assert returnable((item,), (), signal, NOW) == ()


def test_done_item_never_returns() -> None:
    item = wrap(make_item(id="brn_done", state="done"))
    history = (
        HistoryRow(
            item_ref="brn_done",
            channel="review",
            rule="picked_today",
            record_sha256=item.sha256,
            record_state="open",
            surfaced_at=NOW,
            response="picked_today",
            responded_at=NOW,
        ),
    )
    assert returnable((item,), history, "pc_on", NOW) == ()


def _shown_waiting(item_ref: str, now: datetime) -> HistoryRow:
    return HistoryRow(item_ref=item_ref, channel="review", rule="waiting_on_item",
                      record_sha256="shown-old-raw-hash", record_state="waiting",
                      surfaced_at=now - timedelta(days=1))


def test_memo_returns_once_on_8_oct_across_three_successors() -> None:
    path = Path(__file__).resolve().parents[1] / "fixtures/work_model/moved_over.py"
    spec = importlib.util.spec_from_file_location("moments_moved_over", path)
    assert spec and spec.loader
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    from brain_core.work import first_id, index_things

    records = fixture.moved_over_records()
    current = index_things(records)[fixture.MEMO]
    for n in range(3):
        current = replace(current, id=f"brn_memo_version_{n}",
                          supersedes=(*current.supersedes, current.id))
    items = tuple(wrap(r) for r in records) + (wrap(current),)
    before = datetime(2026, 10, 7, 10, tzinfo=SINGAPORE)
    now = datetime(2026, 10, 8, 10, tzinfo=SINGAPORE)
    history = (_shown_waiting(fixture.MEMO, before),)
    assert returnable(items, history, "pc_on", before) == ()
    result = returnable(items, history, "pc_on", now)
    assert result == (wrap(current),)
    assert result[0].path.endswith(f"/{current.id}.md")
    returned = HistoryRow(item_ref=first_id(current), channel="moment",
                          rule="right_moment_return", record_sha256=result[0].sha256,
                          record_state="open", surfaced_at=now)
    for signal in SIGNALS:
        assert returnable(items, (*history, returned), signal, now) == ()


@pytest.mark.parametrize("data,state", [
    ({"level": "suggested"}, "open"),
    ({"level": "accepted", "needs": {"from": "Sam", "what": "signature"}}, "open"),
    ({"level": "accepted", "dates": {"not_before": {"day": "2026-10-08"}}}, "open"),
    ({"level": "accepted"}, "parked"),
])
def test_last_shown_waiting_does_not_return_until_reader_says_up(data: dict, state: str) -> None:
    item = wrap(replace(make_item(id="brn_wait", state=state), data=data))
    assert returnable((item,), (_shown_waiting(item.record.id, NOW),), "pc_on", NOW) == ()


def test_full_index_keeps_a_part_waiting_under_parked_parent() -> None:
    parent = wrap(make_item(id="brn_parent", state="parked"))
    part = wrap(replace(make_item(id="brn_part"), data={"level": "accepted", "parent": "brn_parent"}))
    assert returnable((parent, part), (_shown_waiting("brn_part", NOW),), "pc_on", NOW) == ()


def test_latest_shown_up_prevents_replaying_old_waiting_history() -> None:
    item = wrap(make_item(id="brn_wait"))
    up = HistoryRow(item_ref=item.record.id, channel="review", rule="today",
                    record_sha256=item.sha256, record_state="up", surfaced_at=NOW)
    assert returnable((item,), (_shown_waiting(item.record.id, NOW), up), "pc_on", NOW) == ()


def test_last_recorded_surfacing_wins_timestamp_tie() -> None:
    item = wrap(make_item(id="brn_wait"))
    waiting = _shown_waiting(item.record.id, NOW)
    up = replace(waiting, record_state="up", rule="today")
    assert returnable((item,), (waiting, up), "pc_on", NOW) == ()
    assert returnable((item,), (up, waiting), "pc_on", NOW) == (item,)


def test_legacy_physical_history_aliases_resolve_to_current_thing_and_suppress_replays() -> None:
    current = wrap(replace(make_item(id="brn_current"), supersedes=("brn_first", "brn_middle")))
    waiting = _shown_waiting("brn_middle", NOW)
    assert returnable((current,), (waiting,), "task_finished", NOW) == (current,)
    shown_up = replace(waiting, item_ref="brn_current", record_state="up", surfaced_at=NOW)
    assert returnable((current,), (waiting, shown_up), "pc_on", NOW) == ()
    returned = replace(waiting, item_ref="brn_middle", channel="moment",
                       record_sha256=current.sha256, surfaced_at=NOW)
    assert returnable((current,), (waiting, returned), "pc_on", NOW) == ()


def _event(start_offset_minutes: int, duration_minutes: int = 30) -> CalendarEvent:
    start = NOW + timedelta(minutes=start_offset_minutes)
    return CalendarEvent(title="Meeting", start=start, end=start + timedelta(minutes=duration_minutes))


def test_gap_edge_29_vs_30_minutes() -> None:
    assert find_gap((_event(29),), NOW) is False
    assert find_gap((_event(30),), NOW) is True


def test_gap_false_during_a_current_event() -> None:
    ongoing = CalendarEvent(title="Now", start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=10))
    assert find_gap((ongoing,), NOW) is False


def test_gap_true_with_no_upcoming_events() -> None:
    assert find_gap((), NOW) is True


def test_gap_hour_edges_0759_0800_2100() -> None:
    just_before_open = datetime(2026, 9, 26, 7, 59, tzinfo=SINGAPORE)
    at_open = datetime(2026, 9, 26, 8, 0, tzinfo=SINGAPORE)
    at_close = datetime(2026, 9, 26, 21, 0, tzinfo=SINGAPORE)
    assert find_gap((), just_before_open) is False
    assert find_gap((), at_open) is True
    assert find_gap((), at_close) is False
