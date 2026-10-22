"""The 06:00 review with calendar entries on (N8; step E spec §3, §5): "Calendar entries to add"
lists only tasks with a date and no time, since a task with a start time gets its own entry
when it is saved. Off, the section is exactly as before."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import SECTION_CALENDAR_ENTRIES, ItemRecord, build_morning

from gremlin_town.review.collect import ReviewSources, collect_morning_inputs

NOW = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT on Sun 27 Sep 2026


def _item(record_id: str, title: str, timing: dict, *, level: str = "accepted") -> ItemRecord:
    record = Record(
        schema=RECORD_SCHEMA_V2, id=record_id, kind="item", title=title, state="open",
        created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1), assertion=None,
        confidence=None, sensitivity="private", vaults=(), owners=(), entities=(), evidence=(),
        supersedes=(), dedupe_key=None,
        data={"level": level, "size": "task", "owed_to": None, "repeat": None, "timing": timing,
              "reason": None, "check": None, "parked_until": None},
        body="", source={"said_by": "owner", "came_by": "direct", "recorded_by": "chatgpt-web", "words": title},
    )
    return ItemRecord(record=record, path=f"records/item/2026/09/{record_id}.md")


ITEMS = (
    _item("brn_20260926_100000_aa01", "Dentist", {"due": "2026-10-02", "at": "19:00"}),
    _item("brn_20260926_100000_aa02", "Pay the electricity bill", {"due": "2026-10-02"}),
    _item("brn_20260926_100000_aa03", "Clear the garage", {"window": {"start": "2026-10-05", "end": "2026-10-11"}}),
)


def _calendar_lines(calendar_adds_timed: bool) -> list[str]:
    sources = ReviewSources(brain_items=lambda: ITEMS, calendar_events=lambda start, end: (),
                            calendar_adds_timed=calendar_adds_timed)
    review = build_morning(collect_morning_inputs(sources, history=(), now=NOW))
    return [line.text for part in review.parts for line in part.lines if line.section == SECTION_CALENDAR_ENTRIES]


def test_off_every_dated_task_is_listed_exactly_as_before():
    assert _calendar_lines(False) == []


def test_on_a_task_with_a_start_time_is_left_out_since_it_gets_its_own_entry():
    assert _calendar_lines(True) == []


def test_on_the_calendar_itself_is_read_the_same_way():
    sources = ReviewSources(brain_items=lambda: ITEMS, calendar_events=lambda start, end: (),
                            calendar_adds_timed=True)
    inputs = collect_morning_inputs(sources, history=(), now=NOW)
    assert not hasattr(inputs, "calendar_marked_ids")
    assert inputs.source_status == {"brain": None, "calendar": None, "requests": "not_configured"}
