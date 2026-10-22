from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from brain_core.records import RECORD_SCHEMA_V1, RECORD_SCHEMA_V2, Record
from brain_core.sitreps import ProjectTasks, TaskSitrep
from brain_core.review import (
    COMING_UP_CAP,
    COMING_UP_LATER_REF,
    COMING_UP_MORE_REF,
    COMING_UP_RULE,
    EMPTY_REVIEW_TEXT,
    SECTION_CALENDAR_ENTRIES,
    SECTION_COMING_UP,
    SECTION_COULD_NOT_SEE,
    SECTION_MISSED,
    SECTION_NEW_SUGGESTIONS,
    SECTION_TO_SORT,
    SECTION_ORDER,
    SECTION_PICK_OR_PARK,
    SECTION_STUCK,
    SECTION_TODAY,
    SECTION_WAITING_ON,
    SECTION_WINDOWS_CLOSING,
    STUCK_DAYS,
    WAITING_ON_REQUEST_CAP,
    CalendarEvent,
    HistoryRow,
    ItemRecord,
    OpenNote,
    OpenRequest,
    ReviewInputs,
    build_review,
    is_thin,
    coming_up_date,
    has_real_time,
    missed,
    render_review,
    request_title,
    stuck_note,
)
from brain_core.timewords import SINGAPORE

NOW = datetime(2026, 9, 26, 6, 0, tzinfo=SINGAPORE)  # Sat 26 Sep 2026, 06:00 SGT
TODAY = date(2026, 9, 26)


def make_item(
    *,
    id: str,
    title: str = "Task",
    state: str = "open",
    level: str | None = "accepted",
    timing: dict[str, Any] | None = None,
    parked_until: str | None = None,
    owed_to: str | None = None,
    reason: str | None = None,
    source: dict[str, Any] | None = None,
    links: tuple[dict[str, Any], ...] = (),
    created_at: datetime = NOW,
    schema: str = RECORD_SCHEMA_V2,
    kind: str = "item",
    supersedes: tuple[str, ...] = (),
    block: str | None = "admin",
) -> Record:
    """``block`` defaults to a sorted task (PRD §4.1), so only the tests about "To sort" meet
    one that isn't."""

    data: dict[str, Any] = {}
    if level is not None:
        data["level"] = level
    data["size"] = "task"
    if block is not None and kind == "item":
        data["block"] = block
    if timing is not None:
        data["timing"] = timing
    if parked_until is not None:
        data["parked_until"] = parked_until
    if owed_to is not None:
        data["owed_to"] = owed_to
    if reason is not None:
        data["reason"] = reason
    return Record(
        schema=schema,
        id=id,
        kind=kind,
        title=title,
        state=state,
        created_at=created_at,
        updated_at=created_at,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=(),
        supersedes=supersedes,
        dedupe_key=None,
        data=data,
        body="",
        source=source,
        links=links,
    )


def wrap(record: Record, path: str | None = None) -> ItemRecord:
    return ItemRecord(record=record, path=path or f"records/{record.kind}/2026/09/{record.id}.md")


def inputs(
    *,
    items: tuple[ItemRecord, ...] = (),
    calendar_events: tuple[CalendarEvent, ...] = (),
    calendar_marked_ids: frozenset[str] = frozenset(),
    open_requests: tuple[OpenRequest, ...] = (),
    history: tuple[HistoryRow, ...] = (),
    source_status: dict[str, str | None] | None = None,
    now: datetime = NOW,
    facts: tuple[ItemRecord, ...] | None = None,
    open_notes: tuple[OpenNote, ...] | None = None,
) -> ReviewInputs:
    return ReviewInputs(
        items=items,
        calendar_events=calendar_events,
        calendar_marked_ids=calendar_marked_ids,
        open_requests=open_requests,
        history=history,
        source_status=source_status or {},
        now=now,
        facts=facts,
        open_notes=open_notes,
    )


def test_empty_review_is_one_line() -> None:
    review = build_review(inputs())
    assert review.is_empty
    assert render_review(review) == EMPTY_REVIEW_TEXT


def test_ideas_and_terminal_states_never_appear() -> None:
    idea = wrap(make_item(id="brn_idea", level="possible"))
    done = wrap(make_item(id="brn_done", state="done"))
    dropped = wrap(make_item(id="brn_dropped", state="dropped", reason="no longer needed"))
    no_longer_needed = wrap(make_item(id="brn_nln", state="no_longer_needed"))
    review = build_review(inputs(items=(idea, done, dropped, no_longer_needed)))
    assert review.is_empty


def test_a_suggestion_appears_only_under_to_sort() -> None:
    """PRD §4.1: "New suggestions" became "To sort", for Nic's yes."""

    suggestion = wrap(
        make_item(id="brn_sugg", level="suggested", title="Learn piano", source={"speaker": "fleet-boss"}, block=None)
    )
    review = build_review(inputs(items=(suggestion,)))
    assert [line.section for line in review.lines] == [SECTION_TO_SORT]
    assert review.lines[0].text == "Learn piano — suggested by fleet-boss"
    assert review.lines[0].rule == "to_sort"


def test_a_suggestion_comes_back_every_morning_until_it_is_sorted() -> None:
    """Until 29 Sep a suggestion was shown once; now it waits in "To sort" until Nic answers
    (build plan slice 4: "the backlog clears in a few mornings")."""

    suggestion = wrap(make_item(id="brn_sugg", level="suggested", title="Learn piano", block=None))
    history = (
        HistoryRow(
            item_ref="brn_sugg",
            channel="review",
            rule="to_sort",
            record_sha256="anything",
            record_state=None,
            surfaced_at=NOW - timedelta(days=1),
        ),
    )
    review = build_review(inputs(items=(suggestion,), history=history))
    assert [line.item_ref for line in review.lines] == ["brn_sugg"]


def test_v1_capture_is_unconfirmed_suggestion_unless_archived() -> None:
    live = wrap(make_item(id="brn_v1_live", schema=RECORD_SCHEMA_V1, kind="capture", state="inbox", level=None))
    archived = wrap(
        make_item(id="brn_v1_done", schema=RECORD_SCHEMA_V1, kind="capture", state="archived", level=None)
    )
    review = build_review(inputs(items=(live, archived)))
    assert len(review.lines) == 1
    assert review.lines[0].item_ref == "brn_v1_live"
    assert review.lines[0].text.endswith("unconfirmed")


def test_missed_stays_in_every_review_until_the_record_changes() -> None:
    """N9 (28 Sep): raised every morning until Nic's answer changes the record, saying since when
    and for how long (Ciel and Brain Home PRD §4.6: N9's fixed no-count wording goes)."""

    wording = "Pay the electricity bill — missed since Thu 24 Sep (2 days). Recommit, change, defer or drop?"
    overdue = wrap(make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"}))
    review = build_review(inputs(items=(overdue,)))
    assert [(line.section, line.text) for line in review.lines] == [(SECTION_MISSED, wording)]

    raised_yesterday = (
        HistoryRow(
            item_ref="brn_bill",
            channel="review",
            rule="missed",
            record_sha256=overdue.sha256,
            record_state="open",
            surfaced_at=NOW - timedelta(days=1),
        ),
    )
    again = build_review(inputs(items=(overdue,), history=raised_yesterday))
    assert [(line.section, line.text) for line in again.lines] == [(SECTION_MISSED, wording)]

    # Each answer is a record change, and each takes it out of Missed.
    recommitted = wrap(make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-10-02"}))
    parked = wrap(make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"},
                            state="parked", parked_until="2026-10-05"))
    dropped = wrap(make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"},
                             state="dropped", reason="paid by direct debit after all"))
    for answered in (recommitted, parked, dropped):
        after = build_review(inputs(items=(answered,), history=raised_yesterday))
        assert all(line.section != SECTION_MISSED for line in after.lines)


def test_missed_uses_window_end_when_no_due_date() -> None:
    overdue_window = wrap(
        make_item(id="brn_window", title="Finish deck", timing={"window": {"start": "2026-09-20", "end": "2026-09-24"}})
    )
    review = build_review(inputs(items=(overdue_window,)))
    assert review.lines[0].text == "Finish deck — missed since Thu 24 Sep (2 days). Recommit, change, defer or drop?"
    assert review.lines[0].fields_used == {
        "window": {"start": "2026-09-20", "end": "2026-09-24"}, "missed_since": "2026-09-24", "missed_days": 2,
    }


def test_windows_closing_within_three_days_boundary() -> None:
    closes_in_3 = wrap(
        make_item(id="brn_w3", timing={"window": {"start": "2026-09-26", "end": "2026-09-29"}})
    )
    closes_in_4 = wrap(
        make_item(id="brn_w4", timing={"window": {"start": "2026-09-26", "end": "2026-09-30"}})
    )
    review = build_review(inputs(items=(closes_in_3, closes_in_4)))
    ids = [line.item_ref for line in review.lines if line.section == SECTION_WINDOWS_CLOSING]
    assert ids == ["brn_w3"]


def test_already_passed_window_is_missed_not_closing() -> None:
    passed = wrap(make_item(id="brn_wp", timing={"window": {"start": "2026-09-10", "end": "2026-09-20"}}))
    review = build_review(inputs(items=(passed,)))
    sections = [line.section for line in review.lines]
    assert sections == [SECTION_MISSED]


def test_pick_or_park_cap_and_more_line_oldest_first() -> None:
    items = tuple(
        wrap(make_item(id=f"brn_open_{i}", created_at=datetime(2026, 9, i + 1, 6, 0, tzinfo=SINGAPORE)))
        for i in range(1, 12)
    )
    review = build_review(inputs(items=items))
    park_lines = [line for line in review.lines if line.section == SECTION_PICK_OR_PARK]
    assert len(park_lines) == 11
    assert [line.item_ref for line in park_lines[:10]] == [f"brn_open_{i}" for i in range(1, 11)]
    assert park_lines[10].text == "1 more; ask to see them"


def test_parked_item_appears_only_once_date_arrives() -> None:
    not_yet = wrap(make_item(id="brn_p1", state="parked", parked_until="2026-09-27"))
    today_arrived = wrap(make_item(id="brn_p2", state="parked", parked_until="2026-09-26"))
    already_passed = wrap(make_item(id="brn_p3", state="parked", parked_until="2026-09-20"))
    review = build_review(inputs(items=(not_yet, today_arrived, already_passed)))
    park = {line.item_ref: line.text for line in review.lines if line.section == SECTION_PICK_OR_PARK}
    assert "brn_p1" not in park
    assert park["brn_p2"].endswith("parked until today")
    assert "parked until" in park["brn_p3"] and "today" not in park["brn_p3"]


def test_could_not_see_lists_exact_reason_and_hides_ok_sources() -> None:
    review = build_review(inputs(source_status={"calendar": "token_revoked", "mail": "ok", "requests": None}))
    lines = [line for line in review.lines if line.section == SECTION_COULD_NOT_SEE]
    assert len(lines) == 1
    assert lines[0].text == "calendar: token_revoked"


def test_today_section_has_calendar_events_and_due_today_items() -> None:
    event = CalendarEvent(
        title="Standup",
        start=datetime(2026, 9, 26, 9, 0, tzinfo=SINGAPORE),
        end=datetime(2026, 9, 26, 9, 30, tzinfo=SINGAPORE),
    )
    due_today = wrap(make_item(id="brn_today", title="Send invoice", timing={"due": "2026-09-26"}))
    review = build_review(inputs(items=(due_today,), calendar_events=(event,)))
    today_lines = [line.text for line in review.lines if line.section == SECTION_TODAY]
    assert today_lines == ["Standup — on your calendar", "Send invoice — due today"]


def test_waiting_on_items_and_open_requests() -> None:
    waiting_item = wrap(
        make_item(id="brn_waiting", title="Get pricing", state="waiting", links=({"type": "waiting_on", "target": "Sam"},))
    )
    request = OpenRequest(
        id="gt_req_1", handed_to="fleet-boss", since=date(2026, 9, 23), objective="Research robotics roles"
    )
    review = build_review(inputs(items=(waiting_item,), open_requests=(request,)))
    lines = [line for line in review.lines if line.section == SECTION_WAITING_ON]
    assert [line.text for line in lines] == [
        "Get pricing — waiting on Sam",
        "Research robotics roles — with fleet-boss since Wed 23 Sep",
    ]
    # The why-log still keys the line by the request's ID, and keeps what it read and showed.
    assert lines[1].item_ref == "gt_req_1"
    assert lines[1].fields_used == {
        "handed_to": "fleet-boss", "since": "2026-09-23", "objective": "Research robotics roles",
        "work_state": "", "check": "none",
    }
    assert lines[1].record_state == "open"


def test_waiting_on_says_when_a_result_is_back_but_not_yet_checked() -> None:
    """Step E §12: "returned, not yet checked", never "done"; a rejected result is still open."""

    since = date(2026, 9, 23)
    back = OpenRequest(
        id="gt_req_back", handed_to="fleet-boss", since=since, objective="Draft the deck",
        work_state="result_returned", check="none",
    )
    rejected = OpenRequest(
        id="gt_req_redo", handed_to="fleet-boss", since=since, objective="Draft the budget",
        work_state="result_returned", check="rejected",
    )
    working = OpenRequest(
        id="gt_req_work", handed_to="fleet-boss", since=since, objective="Draft the plan", work_state="in_progress",
    )
    review = build_review(inputs(open_requests=(back, rejected, working)))
    assert [(line.text, line.record_state) for line in review.lines] == [
        ("Draft the deck — returned, not yet checked (with fleet-boss since Wed 23 Sep)", "returned_unchecked"),
        ("Draft the budget — with fleet-boss since Wed 23 Sep", "open"),
        ("Draft the plan — with fleet-boss since Wed 23 Sep", "open"),
    ]
    assert not any("done" in line.text for line in review.lines)


def test_waiting_on_request_without_an_objective_falls_back_to_its_id() -> None:
    request = OpenRequest(id="gt_req_1", handed_to="fleet-boss", since=date(2026, 9, 23))
    review = build_review(inputs(open_requests=(request,)))
    assert [line.text for line in review.lines] == ["gt_req_1 — with fleet-boss since Wed 23 Sep"]
    assert request_title("gt_req_1", " \n ") == "gt_req_1"


def test_request_title_is_one_line_cut_at_a_word_to_about_60_characters() -> None:
    assert request_title("gt_req_1", "Draft the deck") == "Draft the deck"
    assert request_title("gt_req_1", "Draft the\n  deck ") == "Draft the deck"
    exactly_60 = "x" * 50 + " abcdefghi"
    assert request_title("gt_req_1", exactly_60) == exactly_60

    long_objective = "Research robotics roles in Singapore and list the ten that pay best, with links"
    title = request_title("gt_req_1", long_objective)
    assert title == "Research robotics roles in Singapore and list the ten that…"
    assert len(title) <= 61

    # Cut at a word even when the word ends exactly at the limit; no dangling punctuation.
    assert request_title("gt_req_1", "a" * 59 + ", then the rest") == "a" * 59 + "…"
    # One very long word has nowhere to cut but the limit itself.
    assert request_title("gt_req_1", "y" * 80) == "y" * 60 + "…"


def test_waiting_on_requests_cap_and_more_line_newest_first() -> None:
    """Proposed - Nic to confirm (28 Sep): at most 10 requests, newest first, then one line. Since
    ciel-03 (PRD §4.2: "10, then 'N more'") the waiting items stop at 10 too; requests handed off a
    week or more ago are under "To chase" instead, so these are all from the last week."""

    days = (20, 21, 22, 23, 24, 25, 25, 25, 25, 25, 25, 25)
    requests = tuple(
        OpenRequest(id=f"gt_req_{index:02d}", handed_to="fleet-boss", since=date(2026, 9, day), objective=f"Task {index}")
        for index, day in enumerate(days)
    )
    waiting_items = tuple(
        wrap(make_item(id=f"brn_wait_{n}", title=f"Wait {n}", state="waiting")) for n in range(12)
    )
    review = build_review(inputs(items=waiting_items, open_requests=requests))
    lines = [line for line in review.lines if line.section == SECTION_WAITING_ON]

    # Items in `waiting` come first: 10, then how many more.
    assert [line.item_ref for line in lines[:10]] == [f"brn_wait_{n}" for n in range(10)]
    assert (lines[10].text, lines[10].rule, lines[10].fields_used) == (
        "2 more; ask to see them", "waiting_on_cap", {"remaining": 2},
    )
    request_lines = lines[11:]
    assert len(request_lines) == WAITING_ON_REQUEST_CAP + 1
    assert [line.item_ref for line in request_lines[:10]] == [f"gt_req_{index:02d}" for index in (5, 6, 7, 8, 9, 10, 11, 4, 3, 2)]
    assert request_lines[10].text == "2 more; ask to see them"
    assert request_lines[10].rule == "waiting_on_request_cap"
    assert request_lines[10].fields_used == {"remaining": 2}
    # Numbered like every other line, so Nic can answer "show me 22".
    assert request_lines[10].number == 22


def test_waiting_on_requests_on_the_same_day_keep_the_order_given() -> None:
    """GT lists requests most recently updated first; within one date that order is kept."""

    requests = (
        OpenRequest(id="gt_req_b", handed_to="fleet-boss", since=date(2026, 9, 23), objective="Later today"),
        OpenRequest(id="gt_req_a", handed_to="fleet-boss", since=date(2026, 9, 23), objective="Earlier today"),
        OpenRequest(id="gt_req_c", handed_to="fleet-boss", since=date(2026, 9, 24), objective="Newest"),
    )
    review = build_review(inputs(open_requests=requests))
    assert [line.item_ref for line in review.lines] == ["gt_req_c", "gt_req_b", "gt_req_a"]
    assert all(line.rule == "waiting_on_request" for line in review.lines)


def test_calendar_entries_to_add_excludes_marked_ids() -> None:
    unmarked = wrap(make_item(id="brn_cal_1", title="Send Sam the deck", timing={"due": "2026-10-02"}))
    marked = wrap(make_item(id="brn_cal_2", title="Already scheduled", timing={"due": "2026-10-02"}))
    review = build_review(inputs(items=(unmarked, marked), calendar_marked_ids=frozenset({"brn_cal_2"})))
    lines = [line for line in review.lines if line.section == SECTION_CALENDAR_ENTRIES]
    assert len(lines) == 1
    assert lines[0].item_ref == "brn_cal_1"
    assert lines[0].text == "Send Sam the deck — has a date, not on your calendar"


def test_has_real_time() -> None:
    assert has_real_time(make_item(id="brn_t1", timing={"due": "2026-09-30"})) is True
    assert has_real_time(make_item(id="brn_t2", timing={"window": {"start": "2026-09-26", "end": "2026-09-27"}})) is True
    assert has_real_time(make_item(id="brn_t3", timing={"on_event": "Sam replies"})) is False
    assert has_real_time(make_item(id="brn_t4")) is False


def test_missed_helper_ignores_non_open_and_non_accepted() -> None:
    assert missed(make_item(id="brn_m1", timing={"due": "2026-09-01"}), NOW) is True
    assert missed(make_item(id="brn_m2", state="done", timing={"due": "2026-09-01"}), NOW) is False
    assert missed(make_item(id="brn_m3", level="suggested", timing={"due": "2026-09-01"}), NOW) is False
    assert missed(make_item(id="brn_m4", schema=RECORD_SCHEMA_V1, kind="capture", level=None), NOW) is False


def test_render_review_numbers_lines_and_skips_numbers_for_missed() -> None:
    overdue = wrap(make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"}))
    suggestion = wrap(make_item(id="brn_sugg", level="suggested", title="Learn piano", block=None))
    text = render_review(build_review(inputs(items=(overdue, suggestion))))
    assert "## Missed" in text
    assert "Pay the electricity bill — missed since Thu 24 Sep (2 days)" in text
    assert "## To sort" in text
    assert "1. Learn piano — suggested by an agent" in text


# -- Stuck (N10b; rebuild plan slice 5, G2, G5) ---------------------------------------------

_MOTIVE_WORDS = ("again", "why", "keep", "always", "procrastinat", "avoid")


def at_sgt(day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=SINGAPORE)


def note(
    task_id: str | None, changed: datetime, *, session_id: str | None = None, next_step: str = "Find the card"
) -> OpenNote:
    return OpenNote(
        session_id=session_id or f"note-{task_id}", next_step=next_step, updated_at=changed, task_id=task_id,
    )


def stuck_lines(review) -> list[tuple[int, str]]:
    return [(line.number, line.text) for line in review.lines if line.section == SECTION_STUCK]


def test_stuck_shows_a_started_task_whose_note_last_changed_three_days_ago_not_two() -> None:
    """G5: counted in Singapore calendar days from the note's last change, whatever the hour
    (Proposed - Nic to confirm): late on Wed 23 Sep is 3 days before Sat 26 Sep, though only
    about 54 hours."""

    three = wrap(make_item(id="brn_bank", title="Call the bank"))
    two = wrap(make_item(id="brn_fees", title="Pay the fees"))
    notes = (note("brn_bank", at_sgt(23, 23, 59)), note("brn_fees", at_sgt(24, 0, 1)))
    review = build_review(inputs(items=(three, two), open_notes=notes))
    assert STUCK_DAYS == 3
    assert stuck_lines(review) == [(1, "Call the bank — started; no change since Wed 23 Sep")]
    assert stuck_note(three.record, notes, TODAY) == notes[0]
    assert stuck_note(two.record, notes, TODAY) is None


def test_stuck_counts_weekends() -> None:
    monday = datetime(2026, 9, 28, 6, 0, tzinfo=SINGAPORE)
    task = wrap(make_item(id="brn_bank", title="Call the bank"))
    review = build_review(inputs(items=(task,), open_notes=(note("brn_bank", at_sgt(25, 18)),), now=monday))
    assert stuck_lines(review) == [(1, "Call the bank — started; no change since Fri 25 Sep")]


def test_the_date_is_the_notes_last_change_in_singapore() -> None:
    task = wrap(make_item(id="brn_bank", title="Call the bank"))
    utc_evening = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)  # Wed 23 Sep, 04:00 in Singapore
    review = build_review(inputs(items=(task,), open_notes=(note("brn_bank", utc_evening),)))
    assert stuck_lines(review) == [(1, "Call the bank — started; no change since Wed 23 Sep")]


def test_stuck_shows_every_morning_in_the_same_words_until_the_note_moves() -> None:
    task = wrap(make_item(id="brn_bank", title="Call the bank"))
    quiet = note("brn_bank", at_sgt(22))
    wording = "Call the bank — started; no change since Tue 22 Sep"
    assert stuck_lines(build_review(inputs(items=(task,), open_notes=(quiet,)))) == [(1, wording)]

    shown_yesterday = (
        HistoryRow(
            item_ref="brn_bank", channel="review", rule="stuck", record_sha256=task.sha256,
            record_state="open", surfaced_at=NOW,
        ),
    )
    sunday = NOW + timedelta(days=1)
    again = build_review(inputs(items=(task,), open_notes=(quiet,), history=shown_yesterday, now=sunday))
    assert stuck_lines(again) == [(1, wording)]
    assert not any(word in wording.lower() for word in _MOTIVE_WORDS)
    assert "day" not in wording  # a date, never a count that grows each morning

    # Nic moves it: the note changes, and it stays out of Stuck until 3 more days pass.
    moved = note("brn_bank", at_sgt(27, 2))
    assert stuck_lines(
        build_review(inputs(items=(task,), open_notes=(moved,), history=shown_yesterday, now=sunday))
    ) == []
    wednesday = NOW + timedelta(days=4)
    assert stuck_lines(build_review(inputs(items=(task,), open_notes=(moved,), now=wednesday))) == [
        (1, "Call the bank — started; no change since Sun 27 Sep"),
    ]


@pytest.mark.parametrize(
    "answer",
    [
        {"state": "parked", "parked_until": "2026-10-05"},
        {"state": "dropped", "reason": "the bank called me"},
        {"state": "done"},
    ],
)
def test_parking_dropping_or_finishing_a_task_takes_it_out_of_stuck(answer: dict[str, Any]) -> None:
    before = wrap(make_item(id="brn_bank1", title="Call the bank"))
    after = wrap(make_item(id="brn_bank2", title="Call the bank", supersedes=("brn_bank1",), **answer))
    quiet = note("brn_bank1", at_sgt(22))
    assert stuck_lines(build_review(inputs(items=(before,), open_notes=(quiet,)))) != []
    assert stuck_lines(build_review(inputs(items=(before, after), open_notes=(quiet,)))) == []


def test_a_missed_task_that_is_also_stuck_shows_once_under_missed() -> None:
    """Proposed - Nic to confirm: once, in the more urgent section, whose four answers cover it."""

    overdue = wrap(make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"}))
    quiet = (note("brn_bill", at_sgt(20)),)
    assert stuck_note(overdue.record, quiet, TODAY) is not None
    review = build_review(inputs(items=(overdue,), open_notes=quiet))
    assert [(line.section, line.text) for line in review.lines] == [
        (SECTION_MISSED, "Pay the electricity bill — missed since Thu 24 Sep (2 days). Recommit, change, defer or drop?"),
    ]


@pytest.mark.parametrize(
    "variant",
    [
        {"state": "waiting", "links": ({"type": "waiting_on", "target": "Sam"},)},
        {"state": "blocked"},
        {"level": "suggested"},
        {"level": "possible"},
    ],
)
def test_only_an_accepted_open_task_can_be_stuck(variant: dict[str, Any]) -> None:
    entry = wrap(make_item(id="brn_bank", title="Call the bank", **variant))
    quiet = (note("brn_bank", at_sgt(20)),)
    assert stuck_note(entry.record, quiet, TODAY) is None
    assert stuck_lines(build_review(inputs(items=(entry,), open_notes=quiet))) == []


def test_handed_off_work_is_never_stuck() -> None:
    """G2: a gt-<request_id> note is another agent's work; it shows under "Waiting on"."""

    entry = wrap(make_item(id="brn_deck", title="Draft the deck"))
    hub_note = note("brn_deck", at_sgt(20), session_id="gt-req_0123456789abcdef")
    assert stuck_note(entry.record, (hub_note,), TODAY) is None
    assert stuck_lines(build_review(inputs(items=(entry,), open_notes=(hub_note,)))) == []


def test_no_stuck_section_without_notes_or_without_a_note_for_the_task() -> None:
    entry = wrap(make_item(id="brn_bank", title="Call the bank"))
    for open_notes in (None, (), (note("brn_other", at_sgt(20)),)):
        assert SECTION_STUCK not in build_review(inputs(items=(entry,), open_notes=open_notes)).sections


def test_a_task_is_stuck_through_its_notes_link_or_old_key_and_only_the_notes_change_counts() -> None:
    first = wrap(make_item(id="brn_bank1", title="Call the bank"))
    changed = wrap(
        make_item(id="brn_bank2", title="Call the bank", timing={"due": "2026-10-02"}, supersedes=("brn_bank1",))
    )
    linked = note("brn_bank1", at_sgt(21), session_id="bank-call")  # linked to the task's first version
    assert stuck_note(changed.record, (linked,), TODAY) == linked
    old_key = OpenNote(session_id="item-brn_bank1", next_step="Find the card", updated_at=at_sgt(21))
    assert stuck_note(changed.record, (old_key,), TODAY) == old_key
    # The task itself changed (a new date); its note did not, so it is still stuck.
    assert stuck_lines(build_review(inputs(items=(first, changed), open_notes=(linked,)))) == [
        (1, "Call the bank — started; no change since Mon 21 Sep"),
    ]
    # A newer note for the task counts: the most recently changed one wins.
    fresh = note("brn_bank2", at_sgt(25), session_id="bank-call-2")
    assert stuck_note(changed.record, (linked, fresh), TODAY) is None


def test_stuck_comes_right_after_missed_and_leaves_every_other_section_as_it_was() -> None:
    overdue = wrap(make_item(id="brn_bill", title="Pay the electricity bill", timing={"due": "2026-09-24"}))
    due_today = wrap(make_item(id="brn_invoice", title="Send invoice", timing={"due": "2026-09-26"}))
    flights = wrap(make_item(id="brn_flights", title="Book the flights", created_at=NOW - timedelta(days=9)))
    bank = wrap(make_item(id="brn_bank", title="Call the bank", created_at=NOW - timedelta(days=8)))
    form = wrap(make_item(id="brn_form", title="File the form", created_at=NOW - timedelta(days=7)))
    items = (overdue, due_today, bank, flights, form)
    notes = (
        note("brn_bank", at_sgt(23)),
        note("brn_flights", at_sgt(21)),
        note("brn_form", at_sgt(25)),  # moved yesterday: not stuck
        note("brn_bill", at_sgt(20)),  # stuck, but missed: shown under Missed only
    )
    with_notes = build_review(inputs(items=items, open_notes=notes))
    assert SECTION_ORDER.index(SECTION_STUCK) == SECTION_ORDER.index(SECTION_MISSED) + 1
    # Longest unchanged first; a stuck task with no time also stays under "Pick or park".
    assert render_review(with_notes) == "\n".join([
        "## Today",
        "1. Send invoice — due today",
        "## Missed",
        "Pay the electricity bill — missed since Thu 24 Sep (2 days). Recommit, change, defer or drop?",
        "## Stuck",
        "2. Book the flights — started; no change since Mon 21 Sep",
        "3. Call the bank — started; no change since Wed 23 Sep",
        "## Your day: pick 3 to 7",
        "4. Book the flights — no time set",
        "5. Call the bank — no time set",
        "6. File the form — no time set",
        "## Calendar entries to add",
        "7. Send invoice — has a date, not on your calendar",
    ])
    without_notes = build_review(inputs(items=items))
    assert [(line.section, line.text) for line in with_notes.lines if line.section != SECTION_STUCK] == [
        (line.section, line.text) for line in without_notes.lines
    ]


def test_a_stuck_line_logs_the_task_and_the_note_it_read() -> None:
    entry = wrap(make_item(id="brn_bank", title="Call the bank"))
    quiet = note("brn_bank", at_sgt(22), session_id="bank-call")
    review = build_review(inputs(items=(entry,), open_notes=(quiet,)))
    [line] = [line for line in review.lines if line.section == SECTION_STUCK]
    assert (line.item_ref, line.rule, line.record_sha256, line.record_path, line.record_state) == (
        "brn_bank", "stuck", entry.sha256, entry.path, "open",
    )
    assert line.fields_used == {
        "state": "open", "session_id": "bank-call", "task_id": "brn_bank", "note_updated_at": at_sgt(22).isoformat(),
    }


def test_an_answer_about_a_stuck_task_stays_on_the_line_it_was_on() -> None:
    """Like Missed, Stuck's words are fixed: Nic's answer (§3.1) stays where it was before Stuck."""

    entry = wrap(make_item(id="brn_bank", title="Call the bank"))
    fact = wrap(
        make_item(
            id="brn_fact", kind="fact", state="current", level=None,
            source={"said_by": "owner", "words": "DBS, about the card fee"},
            links=({"type": "about", "target": "brn_bank"},),
        )
    )
    review = build_review(inputs(items=(entry,), facts=(fact,), open_notes=(note("brn_bank", at_sgt(22)),)))
    assert [(line.section, line.text) for line in review.lines] == [
        (SECTION_STUCK, "Call the bank — started; no change since Tue 22 Sep"),
        (SECTION_PICK_OR_PARK, 'Call the bank — no time set (you said: "DBS, about the card fee")'),
    ]


# --- "Coming up" (N16; Accepted - Nic, 28 Sep 2026) -----------------------------------------------


def preference(focus_projects: list[str]) -> Record:
    record = make_item(id="brn_pref", kind="preference", state="active", level=None)
    return replace(
        record,
        data={"focus_projects": focus_projects},
        source={"said_by": "owner", "came_by": "direct", "words": "my focus", "recorded_by": "chatgpt-web"},
    )


def test_the_sections_follow_the_prds_order() -> None:
    """PRD §4.2: what's fixed today, what slipped, what's coming, then what to sort, chase and pick."""

    assert SECTION_ORDER == (
        SECTION_TODAY, SECTION_MISSED, SECTION_STUCK, SECTION_WINDOWS_CLOSING, SECTION_COMING_UP, SECTION_TO_SORT,
        "to_chase", "no_next_action", SECTION_PICK_OR_PARK, SECTION_WAITING_ON, SECTION_CALENDAR_ENTRIES,
        "weekly_review", SECTION_COULD_NOT_SEE,
    )


def test_coming_up_date_is_a_due_date_after_today_or_a_window_closing_after_windows_closing() -> None:
    def dated(timing: dict[str, Any]) -> Record:
        return make_item(id="brn_x", timing=timing)

    assert coming_up_date(dated({"due": "2026-09-27"}), TODAY) == date(2026, 9, 27)
    assert coming_up_date(dated({"due": "2026-09-26"}), TODAY) is None  # Today
    assert coming_up_date(dated({"due": "2026-09-25"}), TODAY) is None  # Missed
    assert coming_up_date(dated({"window": {"start": "2026-09-20", "end": "2026-09-29"}}), TODAY) is None  # Windows closing
    assert coming_up_date(dated({"window": {"start": "2026-09-20", "end": "2026-09-30"}}), TODAY) == date(2026, 9, 30)
    assert coming_up_date(dated({"on_event": "the studio reopens"}), TODAY) is None
    assert coming_up_date(make_item(id="brn_y"), TODAY) is None


def test_coming_up_lists_the_next_seven_days_most_important_first_then_counts_later_ones() -> None:
    plain = wrap(make_item(id="brn_dentist", title="Book the dentist", timing={"due": "2026-09-27"}, created_at=NOW - timedelta(days=1)))
    waiting = wrap(make_item(id="brn_sam", title="Pay Sam back", timing={"due": "2026-09-29"}, owed_to="Sam"))
    window = wrap(make_item(id="brn_flat", title="Clean the flat", timing={"window": {"start": "2026-09-28", "end": "2026-10-01"}}, created_at=NOW - timedelta(days=2)))
    later = wrap(make_item(id="brn_taxes", title="File taxes", timing={"due": "2026-10-04"}))
    waits = wrap(make_item(id="brn_parts", title="Order the parts", state="waiting", timing={"due": "2026-09-28"}))
    review = build_review(inputs(items=(plain, waiting, window, later, waits), calendar_marked_ids=frozenset({
        "brn_dentist", "brn_sam", "brn_flat", "brn_taxes", "brn_parts",
    })))
    assert render_review(review) == "\n".join([
        "## Coming up",
        "1. Pay Sam back — due Tue 29 Sep",
        "2. Book the dentist — due tomorrow",
        "3. Clean the flat — window closes Thu 1 Oct",
        "4. 1 more later; ask to see them",
        "## Waiting on",
        "5. Order the parts — waiting",
    ])
    first, *_, later_line = [line for line in review.lines if line.section == SECTION_COMING_UP]
    assert (first.item_ref, first.rule, first.record_sha256, first.record_path) == (
        "brn_sam", COMING_UP_RULE, waiting.sha256, waiting.path,
    )
    assert first.fields_used == {"due": "2026-09-29", "rules": ["Sam is waiting", "due Tue 29 Sep"]}
    assert (later_line.item_ref, later_line.rule, later_line.fields_used) == (
        COMING_UP_LATER_REF, "coming_up_later", {"later": 1, "after": "2026-10-03"},
    )


def test_coming_up_ranks_a_focus_projects_task_first_from_nics_preferences() -> None:
    plain = wrap(make_item(id="brn_dentist", title="Book the dentist", timing={"due": "2026-09-28"}, created_at=NOW - timedelta(days=5)))
    focus = wrap(make_item(id="brn_docs", title="Write the docs", timing={"due": "2026-09-30"},
                           links=({"type": "about", "target": "vault_exosuit"},)))
    without = build_review(inputs(items=(plain, focus)))
    with_focus = replace(inputs(items=(plain, focus)), preferences=(preference(["vault_exosuit"]),))
    assert [line.item_ref for line in without.lines if line.section == SECTION_COMING_UP] == ["brn_dentist", "brn_docs"]
    ranked = [line for line in build_review(with_focus).lines if line.section == SECTION_COMING_UP]
    assert [line.item_ref for line in ranked] == ["brn_docs", "brn_dentist"]
    assert ranked[0].fields_used["rules"] == ["due Wed 30 Sep", "focus project: vault_exosuit"]


def test_coming_up_lists_at_most_ten_then_says_how_many_more() -> None:
    items = tuple(
        wrap(make_item(id=f"brn_t{index:02d}", title=f"task {index:02d}", timing={"due": "2026-09-28"}, created_at=NOW - timedelta(days=index)))
        for index in range(COMING_UP_CAP + 3)
    )
    lines = [line for line in build_review(inputs(items=items)).lines if line.section == SECTION_COMING_UP]
    assert len(lines) == COMING_UP_CAP + 1
    assert lines[0].text == "task 12 — due Mon 28 Sep"  # the same level, so the oldest first
    assert (lines[-1].text, lines[-1].item_ref, lines[-1].fields_used) == (
        "3 more; ask to see them", COMING_UP_MORE_REF, {"remaining": 3},
    )


def test_coming_up_leaves_out_stuck_tasks_and_routines_stay_under_pick_or_park() -> None:
    """A stuck task already shows under Stuck. A weekly routine has no date, so it stays under
    "Pick or park" as before (only "where am I?" shows it under "Coming up")."""

    grant = wrap(make_item(id="brn_grant", title="Apply for the grant", timing={"due": "2026-09-30"}))
    gym = wrap(replace(make_item(id="brn_gym", title="Gym"), data={
        "level": "accepted", "size": "task", "repeat": "weekly:mon", "block": "errands",
    }))
    review = build_review(inputs(items=(grant, gym), open_notes=(note("brn_grant", at_sgt(23)),), calendar_marked_ids=frozenset({"brn_grant"})))
    assert [(line.section, line.text) for line in review.lines] == [
        (SECTION_STUCK, "Apply for the grant — started; no change since Wed 23 Sep"),
        (SECTION_PICK_OR_PARK, "Gym — no time set"),
    ]


# --- A start time that has passed, and tonight (Accepted - Nic, 28 Sep 2026) ------------------------
# Nic told Diablo "Later 12am I got a meeting with Greta" at 3:10 pm on Sun 27 Sep, saved due Mon 28
# Sep at 00:00. The 06:00 review that Monday listed it under Today, "due today", six hours after it.

GRETA = make_item(
    id="brn_greta",
    title="Meeting with Greta at 12am",
    timing={"due": "2026-09-28", "at": "00:00", "resolved_from": {"today": "2026-09-27", "words": "Later 12am"}},
)
SUN_0600 = datetime(2026, 9, 27, 6, 0, tzinfo=SINGAPORE)
MON_0600 = datetime(2026, 9, 28, 6, 0, tzinfo=SINGAPORE)


def test_missed_counts_a_start_time_30_minutes_after_it_the_same_day() -> None:
    assert missed(GRETA, datetime(2026, 9, 27, 23, 30, tzinfo=SINGAPORE)) is False
    assert missed(GRETA, datetime(2026, 9, 28, 0, 29, tzinfo=SINGAPORE)) is False
    assert missed(GRETA, datetime(2026, 9, 28, 0, 30, tzinfo=SINGAPORE)) is True


def test_the_review_puts_a_passed_start_time_under_missed_asking_what_happened() -> None:
    review = build_review(inputs(items=(wrap(GRETA),), now=MON_0600))
    # Only under Missed: not under Today, and no calendar entry suggested for something over.
    assert [(line.section, line.text) for line in review.lines if line.item_ref == "brn_greta"] == [
        (SECTION_MISSED, "Meeting with Greta at 12am — was at 12 am today. Did it happen? Done, move or drop?"),
    ]


def test_a_missed_start_from_an_earlier_day_names_its_day() -> None:
    call = make_item(id="brn_call", title="Call Sam", timing={"due": "2026-09-24", "at": "15:00"})
    review = build_review(inputs(items=(wrap(call),)))  # NOW is Sat 26 Sep, 06:00
    assert [line.text for line in review.lines if line.section == SECTION_MISSED] == [
        "Call Sam — was at 3 pm Thu 24 Sep (2 days ago). Did it happen? Done, move or drop?",
    ]


def test_the_review_lists_a_start_before_6am_tomorrow_under_today_as_tonight() -> None:
    review = build_review(inputs(items=(wrap(GRETA),), now=SUN_0600))
    assert [line.text for line in review.lines if line.section == SECTION_TODAY] == [
        "Meeting with Greta at 12am — due tonight at 12 am",
    ]
    run = make_item(id="brn_run", title="Run", timing={"due": "2026-09-28", "at": "06:00"})
    review = build_review(inputs(items=(wrap(run),), now=SUN_0600))
    assert [line for line in review.lines if line.section == SECTION_TODAY] == []  # tomorrow morning's


# --- "To sort" (Ciel and Brain Home PRD §4.1; build plan slice 4) ------------------------------------


def test_to_sort_lists_suggestions_with_their_pre_sort_and_tasks_not_sorted_yet_oldest_first() -> None:
    """A suggestion shows the agent's pre-sort for Nic's yes; an accepted task with no block type is
    "not sorted yet". Both travel beside the review for Diablo, numbered as their lines."""

    presorted = wrap(replace(
        make_item(id="brn_vat", title="Call the accountant about the VAT", level="suggested", source={"speaker": "chatgpt-web"},
                  timing={"duration_minutes": 15, "padded_minutes": 23, "estimated_by": "chatgpt-web"},
                  created_at=NOW - timedelta(days=2)),
        data={"level": "suggested", "size": "task", "block": "admin", "first_step": "Find their number",
              "timing": {"duration_minutes": 15, "padded_minutes": 23, "estimated_by": "chatgpt-web"}},
    ))
    tiny = wrap(replace(
        make_item(id="brn_jo", title="Reply to Jo's text", level="suggested", source={"speaker": "hermes-wa"}, block=None,
                  created_at=NOW - timedelta(days=1)),
        data={"level": "suggested", "size": "task", "timing": {"duration_minutes": 2, "padded_minutes": 3, "estimated_by": "hermes-wa"}},
    ))
    unsorted = wrap(make_item(id="brn_bulbs", title="Buy light bulbs", block=None, created_at=NOW - timedelta(days=40)))
    review = build_review(inputs(items=(presorted, tiny, unsorted)))
    to_sort = [line for line in review.lines if line.section == SECTION_TO_SORT]
    assert [line.text for line in to_sort] == [
        "Buy light bulbs — not sorted yet",
        "Call the accountant about the VAT — suggested by chatgpt-web; admin, about 15 min, first: Find their number",
        "Reply to Jo's text — suggested by hermes-wa; under two minutes",
    ]
    assert [(entry.record_id, entry.number) for entry in review.to_sort] == [
        ("brn_bulbs", to_sort[0].number), ("brn_vat", to_sort[1].number), ("brn_jo", to_sort[2].number),
    ]
    assert review.to_sort[2].fields_used["two_minutes"] is True
    assert review.to_sort[1].fields_used["first_step"] == "Find their number"
    assert review.to_sort[1].fields_used["speaker"] == "chatgpt-web"
    # The unsorted task also keeps its own line under "Pick or park".
    assert [line.item_ref for line in review.lines if line.section == SECTION_PICK_OR_PARK] == ["brn_bulbs"]


def test_to_sort_takes_ten_a_morning_then_says_how_many_more_and_they_come_back_until_sorted() -> None:
    """Accepted - Nic, 28 Sep 2026 ("10 a morning"): the backlog pages through as he answers."""

    backlog = tuple(
        wrap(make_item(id=f"brn_old{index:02d}", title=f"Old task {index:02d}", block=None,
                       created_at=NOW - timedelta(days=100 - index)))
        for index in range(13)
    )
    review = build_review(inputs(items=backlog))
    to_sort = [line for line in review.lines if line.section == SECTION_TO_SORT]
    assert [line.text for line in to_sort[:2]] == ["Old task 00 — not sorted yet", "Old task 01 — not sorted yet"]
    assert len(review.to_sort) == 10
    assert (to_sort[-1].text, to_sort[-1].item_ref, to_sort[-1].fields_used) == (
        "3 more to sort; ask to see them", "__more_to_sort__", {"remaining": 3},
    )
    shown_yesterday = tuple(
        HistoryRow(item_ref=entry.record_id, channel="review", rule="to_sort", record_sha256=entry.record_sha256,
                   record_state="open", surfaced_at=NOW - timedelta(days=1))
        for entry in review.to_sort
    )
    again = build_review(inputs(items=backlog, history=shown_yesterday))
    assert [entry.record_id for entry in again.to_sort] == [entry.record_id for entry in review.to_sort]
    # Sorting the first three lets the next ones in.
    sorted_three = tuple(
        wrap(replace(entry.record, data={**entry.record.data, "block": "admin"})) if index < 3 else entry
        for index, entry in enumerate(backlog)
    )
    later = build_review(inputs(items=sorted_three))
    assert [entry.record_id for entry in later.to_sort][-3:] == ["brn_old10", "brn_old11", "brn_old12"]


def test_old_unconfirmed_v1_records_are_listed_once_outside_the_cap() -> None:
    live = wrap(make_item(id="brn_v1_live", schema=RECORD_SCHEMA_V1, kind="capture", state="inbox", level=None))
    review = build_review(inputs(items=(live,)))
    assert [(line.section, line.text, line.rule) for line in review.lines] == [
        (SECTION_TO_SORT, "Task — unconfirmed", "new_suggestion"),
    ]
    assert review.to_sort == ()
    once = (HistoryRow(item_ref="brn_v1_live", channel="review", rule="new_suggestion", record_sha256="x",
                       record_state=None, surfaced_at=NOW - timedelta(days=1)),)
    assert build_review(inputs(items=(live,), history=once)).is_empty


def test_a_task_with_a_first_step_is_not_thin() -> None:
    thin = make_item(id="brn_thin", title="Call the bank", block=None)
    stepped = replace(thin, data={**thin.data, "first_step": "Find the card"})
    assert is_thin(thin) and not is_thin(stepped)


# --- The review's new parts (Ciel and Brain Home PRD §4.2; build plan slice 5) ---------------------


def _section(review, name: str) -> list[tuple[int, str]]:
    return [(line.number, line.text) for line in review.lines if line.section == name]


def test_something_waited_on_for_a_week_or_past_its_date_is_to_chase_not_waiting_or_missed() -> None:
    """Acceptance case 15: something waited on for 8 days appears under "To chase". Past the date
    Nic expected it, it is to chase too, rather than missed: someone else has to act."""

    sam = ({"type": "waiting_on", "target": "Sam"},)
    eight_days = wrap(make_item(id="brn_quote", title="Get the quote", state="waiting", links=sam, created_at=NOW - timedelta(days=8)))
    six_days = wrap(make_item(id="brn_keys", title="Get the keys back", state="waiting", links=sam, created_at=NOW - timedelta(days=6)))
    expected = wrap(make_item(id="brn_form", title="Get the signed form", state="waiting", links=sam,
                              timing={"due": "2026-09-24"}, created_at=NOW - timedelta(days=1)))
    old_request = OpenRequest(id="gt_req_old", handed_to="fleet-boss", since=date(2026, 9, 18), objective="Draft the deck")
    new_request = OpenRequest(id="gt_req_new", handed_to="fleet-boss", since=date(2026, 9, 24), objective="Book the flights")
    review = build_review(inputs(items=(eight_days, six_days, expected), open_requests=(old_request, new_request),
                                 calendar_marked_ids=frozenset({"brn_form"})))
    assert _section(review, "to_chase") == [
        (1, "Get the quote — waiting on Sam since Fri 18 Sep (8 days)"),
        (2, "Get the signed form — waiting on Sam; expected Thu 24 Sep (2 days ago)"),
        (3, "Draft the deck — with fleet-boss since Fri 18 Sep (8 days)"),
    ]
    assert _section(review, SECTION_MISSED) == []
    assert _section(review, SECTION_WAITING_ON) == [
        (4, "Get the keys back — waiting on Sam"), (5, "Book the flights — with fleet-boss since Thu 24 Sep"),
    ]
    # A chased request keeps the "Waiting on" rule, so a daytime return still reports it.
    chased_request = next(line for line in review.lines if line.item_ref == "gt_req_old")
    assert (chased_request.rule, chased_request.record_state) == ("waiting_on_request", "open")


def test_a_goal_or_focus_project_with_no_next_action_is_flagged_until_it_has_one() -> None:
    """Acceptance case 16: a goal with no open step is flagged every morning until it has one or
    Nic parks it. A focus project is checked only while its task sitreps can be read."""

    lonely = wrap(replace(make_item(id="brn_goal1", title="Move into robotics"), data={"level": "accepted", "size": "goal"}))
    busy = wrap(replace(make_item(id="brn_goal2", title="Clear the house"), data={"level": "accepted", "size": "goal"}))
    step = wrap(make_item(id="brn_step", title="Buy boxes", links=({"type": "part_of", "target": "brn_goal2"},)))
    parked = wrap(replace(make_item(id="brn_goal3", title="Learn piano", state="parked", parked_until="2026-12-01"),
                          data={"level": "accepted", "size": "goal", "parked_until": "2026-12-01"}))
    focus = (preference(["vault_exosuit", "job-search", "vault_gtbc_plans"]),)
    saved_task = wrap(make_item(id="brn_job", title="Shortlist roles", links=({"type": "about", "target": "job-search"},)))
    sitreps = (
        ProjectTasks(project="vault_exosuit", as_of=NOW, tasks=(
            TaskSitrep(project="vault_exosuit", session_id="s1", intent="Ship it", state="open", next_step="Merge",
                       blockers=(), note_updated_at=NOW, note_record_id="brn_20260926_000000_ab12",
                       note_content_sha256="c" * 64),
        )),
    )
    items = (lonely, busy, step, parked, saved_task)
    with_sitreps = build_review(replace(inputs(items=items), preferences=focus, project_tasks=sitreps))
    assert [text for _number, text in _section(with_sitreps, "no_next_action")] == [
        "Move into robotics — goal; no next action",
        "vault_gtbc_plans — focus project; no next action",
    ]
    without = build_review(replace(inputs(items=items), preferences=focus))
    assert [text for _number, text in _section(without, "no_next_action")] == [
        "Move into robotics — goal; no next action",
    ]


def test_your_day_lists_untimed_tasks_most_important_first_then_parked_ones_back() -> None:
    """PRD §4.2: "pick 3 to 7 things for today", most important first by the same points."""

    old_minor = wrap(make_item(id="brn_old", title="Tidy the photos", created_at=NOW - timedelta(days=40)))
    claim = wrap(replace(make_item(id="brn_claim", title="Submit the claim"), data={
        "level": "accepted", "size": "task", "block": "admin",
        "importance": {"level": "real_cost", "reason": "S$400 unclaimed", "proposed_by": "hermes-wa", "confirmed": False},
    }))
    back = wrap(make_item(id="brn_back", title="Renew the domain", state="parked", parked_until="2026-09-26"))
    review = build_review(inputs(items=(old_minor, claim, back)))
    assert _section(review, SECTION_PICK_OR_PARK) == [
        (1, "Tidy the photos — no time set"), (2, "Submit the claim — no time set"),
        (3, "Renew the domain — parked until today"),
    ]
    assert render_review(review).startswith("## Your day: pick 3 to 7\n")


def test_on_fridays_the_review_says_the_weekly_review_is_ready_this_afternoon() -> None:
    """PRD §4.2 and §4.8 (Nic, 28 Sep: "Friday afternoon"). One unnumbered line; not on other days."""

    task = wrap(make_item(id="brn_x", title="File the expenses"))
    friday = datetime(2026, 10, 2, 6, 0, tzinfo=SINGAPORE)
    review = build_review(inputs(items=(task,), now=friday))
    assert render_review(review).endswith("## Weekly review\nReady this afternoon; ask for it when you have time")
    assert [line.number for line in review.lines if line.section == "weekly_review"] == [0]
    assert all(line.section != "weekly_review" for line in build_review(inputs(items=(task,))).lines)


def test_every_section_stops_at_ten_then_says_how_many_more_but_never_cuts_the_calendar() -> None:
    """Accepted - Nic, 28 Sep 2026 ("10, then 'N more'"): a morning with everything in it stays
    readable."""

    overdue = tuple(
        wrap(make_item(id=f"brn_late{n:02d}", title=f"Late {n:02d}", timing={"due": "2026-09-20"})) for n in range(12)
    )
    due_today = tuple(
        wrap(make_item(id=f"brn_now{n:02d}", title=f"Now {n:02d}", timing={"due": "2026-09-26"})) for n in range(11)
    )
    events = tuple(
        CalendarEvent(title=f"Meeting {n}", start=at_sgt(26, 8 + n), end=at_sgt(26, 9 + n)) for n in range(11)
    )
    review = build_review(inputs(items=(*overdue, *due_today), calendar_events=events,
                                 calendar_marked_ids=frozenset(entry.record.id for entry in (*overdue, *due_today))))
    missed_lines = _section(review, SECTION_MISSED)
    assert len(missed_lines) == 11 and missed_lines[-1] == (0, "2 more; ask to see them")
    today_lines = _section(review, SECTION_TODAY)
    assert [text for _number, text in today_lines[:11]] == [f"Meeting {n} — on your calendar" for n in range(11)]
    assert len(today_lines) == 11 + 10 + 1 and today_lines[-1][1] == "1 more; ask to see them"
