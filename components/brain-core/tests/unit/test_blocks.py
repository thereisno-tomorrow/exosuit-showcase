"""Time blocks (Ciel and Brain Home PRD §4.3; acceptance cases 4 and 5). Off until Nic's Google consent."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brain_core.blocks import Override, block_kind, blocks_from_calendar, moment
from brain_core.orientation import OrientationInputs, overview, what_next
from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import CalendarEvent, ItemRecord, ReviewInputs
from brain_core.timewords import SINGAPORE

# Monday 5 Oct 2026, 11:00 in Singapore.
NOW = datetime(2026, 10, 5, 11, 0, tzinfo=SINGAPORE)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, tzinfo=SINGAPORE)


def event(title: str, start: int, end: int, *, all_day: bool = False) -> CalendarEvent:
    return CalendarEvent(title=title, start=at(start), end=at(end), all_day=all_day)


def task(suffix: str, title: str, block: str | None, *, due: str | None = None, clock: str | None = None,
         saved: datetime = NOW - timedelta(days=1)) -> ItemRecord:
    timing: dict[str, object] = {}
    if due is not None:
        timing["due"] = due
    if clock is not None:
        timing["at"] = clock
    data: dict[str, object] = {"level": "accepted", "size": "task", "timing": timing}
    if block is not None:
        data["block"] = block
    record = Record(
        schema=RECORD_SCHEMA_V2, id=f"brn_20261004_000000_{suffix}", kind="item", title=title, state="open",
        created_at=saved, updated_at=saved, assertion=None, confidence=None, sensitivity="private", vaults=(),
        owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None, data=data, body="",
        source={"said_by": "owner"}, links=(),
    )
    return ItemRecord(record=record, path=f"records/item/{record.id}.md")


def inputs(*items: ItemRecord, events: tuple[CalendarEvent, ...] = (), blocks: bool = True) -> OrientationInputs:
    review = ReviewInputs(
        items=items, calendar_events=events, calendar_marked_ids=frozenset(), open_requests=(), history=(),
        source_status={}, now=NOW,
    )
    return OrientationInputs(review=review, blocks=blocks)


DAY = (event("Admin", 9, 10), event("Deep focus", 10, 13), event("Admin", 14, 15), event("Errands", 16, 17))
REPORT = task("a001", "Write the report", "deep_focus")
FORM = task("a002", "Send the tax form", "admin", due="2026-10-05", clock="17:00")
SHOP = task("a003", "Buy boxes", "errands")


def test_inside_a_deep_focus_block_only_deep_focus_tasks_are_offered():
    """Acceptance case 4: an admin task due at 5 pm waits for the 2 pm admin block."""

    answer = what_next(inputs(REPORT, FORM, SHOP, events=DAY), NOW)
    assert answer.top_pick.record_id == REPORT.record.id
    assert answer.alternatives == ()
    assert [(entry.record_id, entry.reason) for entry in answer.can_wait] == [
        (FORM.record.id, "waits for the 2 pm admin block"),
        (SHOP.record.id, "errands work; waits for an errands block"),
    ]
    assert answer.moment == {"kind": "deep_focus", "source": "block", "block": "Deep focus",
                             "block_ends": at(13).isoformat()}
    assert not hasattr(answer, "question")


def test_with_no_admin_block_before_the_deadline_it_comes_up_now_saying_why():
    """Acceptance case 4's other half."""

    day = (event("Deep focus", 10, 13), event("Admin", 18, 19))
    answer = what_next(inputs(REPORT, FORM, events=day), NOW)
    assert answer.top_pick.record_id == FORM.record.id
    assert answer.top_pick.rules[0] == "due today at 5 pm, and there's no admin block before then"
    assert answer.top_pick.fields_used["wrong_block"] == "admin"
    assert [alternative.record_id for alternative in answer.alternatives] == [REPORT.record.id]


def test_after_im_out_errands_are_offered():
    """Acceptance case 5: one line from Nic overrides the block."""

    answer = what_next(inputs(REPORT, FORM, SHOP, events=DAY), NOW, override=Override(mode="out", words="I'm out"))
    assert answer.top_pick.record_id == SHOP.record.id
    assert answer.moment["kind"] == "errands" and answer.moment["source"] == "override"
    assert answer.moment["words"] == "I'm out"


def test_ive_got_20_minutes_keeps_the_blocks_kind_and_says_whether_it_fits():
    answer = what_next(inputs(REPORT, events=DAY), NOW, override=Override(mode="minutes", words="I've got 20 minutes",
                                                                         minutes=20))
    assert answer.top_pick.record_id == REPORT.record.id
    assert answer.top_pick.fields_used["gap_minutes"] == 20


def test_outside_any_block_nothing_is_assumed_and_every_task_ranks_together():
    """Nic, 29 Sep 2026 ("Never assume outside blocks"): no admin assumption, no question."""

    later_report = task("b003", "Draft the essay", "deep_focus", due="2026-10-20")
    small = task("b002", "Reply to the landlord", "admin")
    shop = task("b004", "Buy boxes", "errands")
    answer = what_next(inputs(later_report, small, shop, events=()), NOW)
    assert answer.moment == {"kind": None, "source": "no_block"}
    assert answer.can_wait == ()
    unblocked = what_next(inputs(later_report, small, shop, events=(), blocks=False), NOW)
    assert answer.top_pick.record_id == unblocked.top_pick.record_id
    assert [entry.record_id for entry in answer.alternatives] == [
        entry.record_id for entry in unblocked.alternatives
    ]
    # Between blocks too: at 13:30 the 2 pm admin block hasn't started, so nothing is held.
    between = what_next(inputs(later_report, small, shop, events=DAY), at(13, 30))
    assert between.moment == {"kind": None, "source": "no_block"}
    assert between.can_wait == ()


def test_outside_any_block_i_ve_got_20_minutes_names_no_kind():
    answer = what_next(inputs(REPORT, FORM, SHOP, events=()), NOW,
                       override=Override(mode="minutes", words="I've got 20 minutes", minutes=20))
    assert answer.moment["kind"] is None and answer.moment["source"] == "override"
    assert answer.can_wait == ()


def test_an_unsorted_task_counts_as_admin_sized():
    unsorted = task("c001", "Call the bank", None)
    answer = what_next(inputs(REPORT, unsorted, events=(event("Admin", 10, 12),)), NOW)
    assert answer.top_pick.record_id == unsorted.record.id


def test_with_blocks_off_the_calendar_changes_nothing():
    on = what_next(inputs(REPORT, FORM, SHOP, events=DAY), NOW)
    off = what_next(inputs(REPORT, FORM, SHOP, events=DAY, blocks=False), NOW)
    assert off.moment is None
    assert off.top_pick.record_id != on.top_pick.record_id or len(off.alternatives) == 2
    assert overview(inputs(REPORT, events=DAY, blocks=False), NOW).sections[0] != "now"


def test_where_am_i_opens_with_the_block_nic_is_in():
    picture = overview(inputs(REPORT, events=DAY), NOW)
    assert picture.sections[0] == "now"
    assert picture.lines[0].text == "In your deep-focus block until 1 pm; next: admin at 2 pm"
    outside = overview(inputs(REPORT, events=(event("Admin", 14, 15),)), NOW)
    assert outside.lines[0].text == "No block now; next: admin at 2 pm"


def test_blocks_are_read_from_event_titles_and_all_day_events_never_count():
    assert [block_kind(title) for title in ("Deep focus", "deep work: essay", "ADMIN", "Errands run", "Admins", "Lunch")] == [
        "deep_focus", "deep_focus", "admin", "errands", None, None,
    ]
    blocks = blocks_from_calendar((event("Admin", 14, 15), event("Deep focus", 0, 23, all_day=True), event("Deep focus", 10, 13)))
    assert [(block.kind, block.start.hour) for block in blocks] == [("deep_focus", 10), ("admin", 14)]
    with pytest.raises(ValueError):
        moment(blocks, NOW, Override(mode="asleep", words="zzz"))
