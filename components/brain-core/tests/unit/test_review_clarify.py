"""Clarifying questions about thin tasks (step E spec §3.1; Proposed - speed-04, ROA-20).

Code picks which tasks are too thin, at most 3 a morning, oldest first; Diablo only asks.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest

from brain_core.records import RECORD_SCHEMA_V1, RECORD_SCHEMA_V2, Evidence, Record
from brain_core.review import (
    CLARIFY_CAP,
    CLARIFY_FIRST_STEP_RULE,
    CLARIFY_RULE,
    EMPTY_REVIEW_TEXT,
    SECTION_CALENDAR_ENTRIES,
    SECTION_MISSED,
    SECTION_PICK_OR_PARK,
    SECTION_TODAY,
    ClarifyTask,
    HistoryRow,
    ItemRecord,
    OpenNote,
    ReviewInputs,
    answers_by_task,
    build_review,
    current_items,
    is_thin,
    note_for_task,
    pick_thin_tasks,
    render_review,
    words_beyond_title,
)
from brain_core.timewords import SINGAPORE

NOW = datetime(2026, 9, 27, 6, 0, tzinfo=SINGAPORE)  # Sun 27 Sep 2026, 06:00 SGT
EARLIER = NOW - timedelta(days=5)


def _record(
    *,
    id: str,
    kind: str,
    title: str,
    state: str,
    data: dict[str, Any],
    body: str,
    words: str | None,
    said_by: str,
    links: tuple[dict[str, Any], ...],
    supersedes: tuple[str, ...],
    created_at: datetime,
    schema: str = RECORD_SCHEMA_V2,
    change_words: str | None = None,
) -> Record:
    # Evidence accumulates across versions, oldest first; source.words is the latest one's.
    excerpts = tuple(text for text in (words, change_words) if text is not None)
    source = None
    if schema == RECORD_SCHEMA_V2:
        source = {"said_by": said_by, "came_by": "direct" if said_by == "owner" else "agent_guess", "recorded_by": "hermes-wa"}
        if said_by != "owner":
            source["speaker"] = "fleet-boss"
        if excerpts:
            source["words"] = excerpts[-1]
    evidence = tuple(
        Evidence(source_type="fixture", locator="fixture:1", content_sha256="a" * 64, excerpt=text)
        for text in excerpts
    )
    return Record(
        schema=schema, id=id, kind=kind, title=title, state=state,
        created_at=created_at, updated_at=created_at,
        assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=evidence, supersedes=supersedes,
        dedupe_key=None, data=data, body=body, source=source, links=links,
    )


def task(
    id: str,
    title: str = "Call the bank",
    *,
    body: str = "",
    words: str | None = None,
    change_words: str | None = None,
    state: str = "open",
    level: str = "accepted",
    size: str = "task",
    timing: dict[str, Any] | None = None,
    owed_to: str | None = None,
    repeat: str | None = None,
    parked_until: str | None = None,
    links: tuple[dict[str, Any], ...] = (),
    supersedes: tuple[str, ...] = (),
    created_at: datetime = EARLIER,
    schema: str = RECORD_SCHEMA_V2,
    block: str | None = "admin",
) -> ItemRecord:
    """``block`` defaults to a sorted task (PRD §4.1), so "To sort" stays out of these tests."""

    data = {
        "level": level, "size": size, "owed_to": owed_to, "repeat": repeat, "timing": timing or {},
        "reason": None, "check": None, "parked_until": parked_until,
    }
    if block is not None:
        data["block"] = block
    record = _record(
        id=id, kind="item", title=title, state=state, data=data, body=body, words=words, said_by="owner",
        links=links, supersedes=supersedes, created_at=created_at, schema=schema, change_words=change_words,
    )
    return ItemRecord(record=record, path=f"records/item/2026/09/{id}.md")


def answer(
    id: str,
    about: str,
    words: str = "DBS, about the card fee they charged twice",
    *,
    said_by: str = "owner",
    state: str = "current",
    supersedes: tuple[str, ...] = (),
    created_at: datetime = NOW - timedelta(days=1),
) -> ItemRecord:
    record = _record(
        id=id, kind="fact", title="Call the bank", state=state,
        data={"observed_at": created_at.isoformat(), "valid_until": None},
        body=words, words=words, said_by=said_by, links=({"type": "about", "target": about},),
        supersedes=supersedes, created_at=created_at,
    )
    return ItemRecord(record=record, path=f"records/fact/2026/09/{id}.md")


def inputs(
    items: tuple[ItemRecord, ...],
    *,
    facts: tuple[ItemRecord, ...] | None = (),
    history: tuple[HistoryRow, ...] = (),
    open_notes: tuple[OpenNote, ...] | None = None,
) -> ReviewInputs:
    return ReviewInputs(
        items=items, calendar_events=(), calendar_marked_ids=frozenset(), open_requests=(),
        history=history, source_status={}, now=NOW, facts=facts, open_notes=open_notes,
    )


def note(
    task_id: str | None,
    *,
    session_id: str | None = None,
    next_step: str = "",
    updated_at: datetime = NOW - timedelta(days=1),
) -> OpenNote:
    return OpenNote(
        session_id=session_id or f"item-{task_id}", next_step=next_step, updated_at=updated_at, task_id=task_id,
    )


def asked(entry: ItemRecord, *, days_ago: int = 1) -> HistoryRow:
    return HistoryRow(
        item_ref="clarify:" + entry.record.id, channel="review", rule=CLARIFY_RULE,
        record_sha256=entry.sha256, record_state="open", surfaced_at=NOW - timedelta(days=days_ago),
        record_path=entry.path,
    )


# -- the rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "words"),
    [
        ("", None),
        ("Call the bank", None),
        ("I need to call the bank.", "I need to call the bank"),
        ("Remind me to call the bank later", "remind me to call the bank later pls"),
        ("Nic’s got to call the bank", "I'll call the bank"),
    ],
)
def test_a_task_with_nothing_beyond_its_title_is_thin(body: str, words: str | None) -> None:
    entry = task("brn_a", body=body, words=words)
    assert words_beyond_title(entry.record) == ()
    assert is_thin(entry.record)


def test_any_word_beyond_the_title_in_the_body_or_a_kept_excerpt_is_enough() -> None:
    in_body = task("brn_a", body="Call the bank about the card fee")
    assert words_beyond_title(in_body.record) == ("card", "fee")
    assert not is_thin(in_body.record)

    in_words = task("brn_b", body="Call the bank", words="I need to call DBS")
    assert words_beyond_title(in_words.record) == ("dbs",)
    assert not is_thin(in_words.record)

    number = task("brn_c", body="call the bank at 3")
    assert words_beyond_title(number.record) == ("3",)


@pytest.mark.parametrize(
    "variant",
    [
        {"timing": {"due": "2026-09-30"}},
        {"timing": {"on_event": "after the bank replies"}},
        {"timing": {"duration_minutes": 20, "padded_minutes": 30}},
        {"links": ({"type": "part_of", "target": "brn_goal"},)},
        {"owed_to": "Sam"},
        {"repeat": "weekly:sun"},
        {"size": "goal"},
        {"state": "waiting"},
        {"state": "blocked"},
        {"state": "parked", "parked_until": "2026-10-05"},
        {"state": "done"},
        {"level": "suggested"},
        {"level": "possible"},
        {"schema": RECORD_SCHEMA_V1},
    ],
)
def test_only_an_accepted_open_plain_task_with_no_time_or_links_can_be_thin(variant: dict[str, Any]) -> None:
    entry = task("brn_a", **variant)
    assert not is_thin(entry.record)
    assert pick_thin_tasks((entry,), (), ()) == ()


# -- the cap and the order ---------------------------------------------------------------


def test_at_most_three_a_morning_oldest_first_then_record_id() -> None:
    newest = task("brn_e", "Sort the insurance thing", created_at=NOW - timedelta(hours=2))
    oldest = task("brn_d", "Fix it", created_at=NOW - timedelta(days=9))
    tie_b = task("brn_b", "Book the thing", created_at=NOW - timedelta(days=4))
    tie_a = task("brn_a", "Email Tommy", created_at=NOW - timedelta(days=4))
    middle = task("brn_c", "Groceries", created_at=NOW - timedelta(days=2))
    clear = task("brn_f", "Call the bank", body="about the card fee", created_at=NOW - timedelta(days=30))

    picked = pick_thin_tasks((newest, oldest, tie_b, tie_a, middle, clear), (), ())
    assert CLARIFY_CAP == 3
    assert [entry.record.id for entry in picked] == ["brn_d", "brn_a", "brn_b"]


def test_the_choice_does_not_depend_on_the_order_items_were_read_in() -> None:
    entries = [
        task("brn_a", created_at=NOW - timedelta(days=1)),
        task("brn_b", created_at=NOW - timedelta(days=3)),
        task("brn_c", created_at=NOW - timedelta(days=3)),
        task("brn_d", created_at=NOW - timedelta(days=7)),
        task("brn_e", created_at=NOW - timedelta(days=2)),
    ]
    results = {
        tuple(entry.record.id for entry in pick_thin_tasks(order, (), ()))
        for order in itertools.permutations(entries)
    }
    assert results == {("brn_d", "brn_b", "brn_c")}


# -- once only, and answers --------------------------------------------------------------


def test_a_task_is_asked_about_once_unless_its_record_changes() -> None:
    first = task("brn_a", created_at=NOW - timedelta(days=6))
    second = task("brn_b", created_at=NOW - timedelta(days=5))
    # Its own review line (rule pick_or_park, item_ref = the record ID) is not "asked".
    shown = HistoryRow(
        item_ref="brn_b", channel="review", rule="pick_or_park", record_sha256=second.sha256,
        record_state="open", surfaced_at=NOW - timedelta(days=1),
    )
    picked = pick_thin_tasks((first, second), (), (asked(first), shown))
    assert [entry.record.id for entry in picked] == ["brn_b"]

    # Parked, then reopened with "ok do it": a new version that still says nothing beyond the
    # title, so it may be asked about again.
    reopened = task(
        "brn_a3", words="I need to call the bank", change_words="ok do it",
        supersedes=("brn_a", "brn_a2"), created_at=NOW - timedelta(hours=3),
    )
    picked = pick_thin_tasks((first, reopened), (), (asked(first),))
    assert [entry.record.id for entry in picked] == ["brn_a3"]

    # Words said with a change count too: "park it till Monday" is more than the title.
    explained = task("brn_a4", change_words="park it till Monday", supersedes=("brn_a",))
    assert words_beyond_title(explained.record) == ("monday", "park", "till")
    assert not is_thin(explained.record)


def test_an_answer_stops_the_question_even_after_the_task_changes() -> None:
    original = task("brn_a", created_at=NOW - timedelta(days=6))
    other = task("brn_b", "Fix it", created_at=NOW - timedelta(days=5))
    reply = answer("brn_f1", about="brn_a")
    assert [entry.record.id for entry in pick_thin_tasks((original, other), (reply,), ())] == ["brn_b"]

    # A later version (it was marked waiting, then open again) still carries the answer,
    # which was linked to the version Nic answered about.
    later = task("brn_a3", supersedes=("brn_a", "brn_a2"), created_at=NOW - timedelta(days=2))
    answers = answers_by_task((original, later, other), (reply,))
    assert list(answers) == ["brn_a3"]
    assert [fact.record.id for fact in answers["brn_a3"]] == ["brn_f1"]
    assert [entry.record.id for entry in pick_thin_tasks((original, later, other), (reply,), ())] == ["brn_b"]


def test_only_nics_own_current_facts_are_answers() -> None:
    entry = task("brn_a")
    by_agent = answer("brn_f1", about="brn_a", said_by="agent")
    outdated = answer("brn_f2", about="brn_a", state="outdated")
    replaced = answer("brn_f3", about="brn_a")
    replacement = answer("brn_f4", about="brn_a", state="outdated", supersedes=("brn_f3",))
    elsewhere = answer("brn_f5", about="vault:robotics:notes/plan.md")
    facts = (by_agent, outdated, replaced, replacement, elsewhere)
    assert answers_by_task((entry,), facts) == {}
    assert [picked.record.id for picked in pick_thin_tasks((entry,), facts, ())] == ["brn_a"]


def test_nothing_is_asked_or_shown_when_facts_were_not_read() -> None:
    entry = task("brn_a")
    review = build_review(inputs((entry,), facts=None, history=()))
    assert review.clarify == ()
    assert [line.text for line in review.lines] == ["Call the bank — no time set"]


# -- what Diablo gets --------------------------------------------------------------------


def test_the_payload_names_each_task_and_its_line_in_todays_review() -> None:
    # Ten older untimed tasks that say enough fill Pick or park's cap of 10.
    clear = tuple(
        task(f"brn_c{index:02d}", f"Task {index}", body=f"detail {index}", created_at=NOW - timedelta(days=20 - index))
        for index in range(10)
    )
    thin_listed = task("brn_a", "Fix it", created_at=NOW - timedelta(days=30))
    thin_unlisted = task("brn_z", "Groceries", created_at=NOW - timedelta(days=1))
    review = build_review(inputs((*clear, thin_unlisted, thin_listed)))

    listed_number = next(line.number for line in review.lines if line.item_ref == "brn_a")
    assert review.clarify == (
        ClarifyTask(
            record_id="brn_a", title="Fix it", number=listed_number,
            record_sha256=thin_listed.sha256, record_path="records/item/2026/09/brn_a.md",
            fields_used={
                "word": "task", "level": "accepted", "state": "open", "timing": {}, "links": [],
                "words_beyond_title": [], "answers": [],
                "created_at": thin_listed.record.created_at.isoformat(),
            },
        ),
        ClarifyTask(
            record_id="brn_z", title="Groceries", number=None,
            record_sha256=thin_unlisted.sha256, record_path="records/item/2026/09/brn_z.md",
            fields_used={
                "word": "task", "level": "accepted", "state": "open", "timing": {}, "links": [],
                "words_beyond_title": [], "answers": [],
                "created_at": thin_unlisted.record.created_at.isoformat(),
            },
        ),
    )
    assert [task.item_ref for task in review.clarify] == ["clarify:brn_a", "clarify:brn_z"]
    assert any(line.text == "2 more; ask to see them" for line in review.lines)  # brn_c09 and brn_z


def test_the_rendered_review_never_carries_the_questions() -> None:
    entry = task("brn_a", "Fix it")
    with_facts = build_review(inputs((entry,)))
    without = build_review(inputs((entry,), facts=None))
    assert [task.record_id for task in with_facts.clarify] == ["brn_a"]
    assert render_review(with_facts) == render_review(without) == "## Your day: pick 3 to 7\n1. Fix it — no time set"


def test_an_answer_shows_once_on_the_first_line_naming_the_task_and_never_on_missed() -> None:
    due_today = task("brn_a", "Pay the bill", timing={"due": "2026-09-27"})
    overdue = task("brn_m", "Renew the licence", timing={"due": "2026-09-20"})
    replies = (
        answer("brn_f1", about="brn_a", words="the electricity one,\n  from SP"),
        answer("brn_f2", about="brn_a", words="it's in the app", created_at=NOW - timedelta(hours=5)),
        answer("brn_f3", about="brn_m", words="the driving licence"),
    )
    review = build_review(inputs((due_today, overdue), facts=replies))
    texts = {(line.section, line.item_ref): line for line in review.lines}

    today_line = texts[(SECTION_TODAY, "brn_a")]
    assert today_line.text == 'Pay the bill — due today (you said: "the electricity one, from SP"; "it\'s in the app")'
    assert today_line.fields_used["answers"] == ["brn_f1", "brn_f2"]
    calendar_line = texts[(SECTION_CALENDAR_ENTRIES, "brn_a")]
    assert calendar_line.text == "Pay the bill — has a date, not on your calendar"
    assert "answers" not in calendar_line.fields_used

    assert texts[(SECTION_MISSED, "brn_m")].text == (
        "Renew the licence — missed since Sun 20 Sep (7 days). Recommit, change, defer or drop?"
    )
    assert review.clarify == ()  # neither is thin: both have a time


def test_an_answered_thin_task_shows_the_answer_in_pick_or_park_and_is_not_asked() -> None:
    entry = task("brn_a")
    review = build_review(inputs((entry,), facts=(answer("brn_f1", about="brn_a"),)))
    assert review.clarify == ()
    assert [(line.section, line.text) for line in review.lines] == [
        (SECTION_PICK_OR_PARK, 'Call the bank — no time set (you said: "DBS, about the card fee they charged twice")'),
    ]


# -- the newest version only -------------------------------------------------------------


def test_current_items_keeps_only_the_newest_version() -> None:
    first = task("brn_a1")
    second = task("brn_a2", supersedes=("brn_a1",))
    third = task("brn_a3", supersedes=("brn_a1", "brn_a2"))
    other = task("brn_b")
    assert [entry.record.id for entry in current_items((first, second, third, other))] == ["brn_a3", "brn_b"]


def test_a_changed_task_is_listed_once_in_its_newest_state() -> None:
    before = task("brn_a1", created_at=NOW - timedelta(days=4))
    parked = task(
        "brn_a2", state="parked", parked_until="2026-10-05", words="park it till next month",
        supersedes=("brn_a1",), created_at=NOW - timedelta(days=1),
    )
    review = build_review(inputs((before, parked)))
    assert review.is_empty
    assert review.clarify == ()
    assert render_review(review) == EMPTY_REVIEW_TEXT


# -- a started task with no first step (N12c; Accepted - Nic, 28 Sep 2026) -------------------


def first_step_asked(entry: ItemRecord, *, days_ago: int = 1) -> HistoryRow:
    return HistoryRow(
        item_ref="clarify:" + entry.record.id, channel="review", rule=CLARIFY_FIRST_STEP_RULE,
        record_sha256=entry.sha256, record_state="open", surfaced_at=NOW - timedelta(days=days_ago),
        record_path=entry.path,
    )


def test_a_started_task_whose_note_has_no_first_step_is_asked_for_one() -> None:
    started = task("brn_a", "Call the bank", body="about the card fee")  # not thin: it says more
    review = build_review(inputs((started,), facts=None, open_notes=(note("brn_a", session_id="call-bank"),)))
    assert [(picked.record_id, picked.rule, picked.question, picked.session_id) for picked in review.clarify] == [
        ("brn_a", CLARIFY_FIRST_STEP_RULE, "What's the first step for 'Call the bank'?", "call-bank"),
    ]
    only = review.clarify[0]
    assert only.item_ref == "clarify:brn_a"
    assert only.fields_used["next_step"] == ""
    assert only.fields_used["task_id"] == "brn_a"
    assert only.number == next(line.number for line in review.lines if line.item_ref == "brn_a")
    # The review itself is unchanged: the question travels beside it, never inside it.
    assert render_review(review) == "## Your day: pick 3 to 7\n1. Call the bank — no time set"


def test_a_note_is_found_through_its_task_link_whatever_its_name_or_the_old_item_key() -> None:
    changed = task("brn_a2", "Call the bank", body="about the fee", supersedes=("brn_a1",))
    linked_to_old_version = note("brn_a1", session_id="bank-call")
    assert note_for_task(changed.record, (linked_to_old_version,)) == linked_to_old_version
    old_key = note(None, session_id="item-brn_a1")
    assert note_for_task(changed.record, (old_key,)) == old_key
    # A link beats the old key, and a note linked to another task is never this task's.
    assert note_for_task(changed.record, (old_key, linked_to_old_version)) == linked_to_old_version
    elsewhere = note("brn_zz", session_id="item-brn_a2")
    assert note_for_task(changed.record, (elsewhere,)) is None
    newer = note("brn_a2", session_id="bank-call-2", updated_at=NOW)
    assert note_for_task(changed.record, (linked_to_old_version, newer)) == newer


def test_no_question_when_the_note_has_a_first_step_or_there_is_no_note_or_notes_were_not_read() -> None:
    started = task("brn_a", "Call the bank", body="about the card fee")
    has_step = note("brn_a", next_step="Find the card")
    assert build_review(inputs((started,), open_notes=(has_step,))).clarify == ()
    assert build_review(inputs((started,), open_notes=())).clarify == ()
    assert build_review(inputs((started,), facts=None, open_notes=None)).clarify == ()
    # Blank space is no first step.
    blank = note("brn_a", next_step="   ")
    assert [picked.record_id for picked in build_review(inputs((started,), open_notes=(blank,))).clarify] == ["brn_a"]


def test_handed_off_work_never_gets_the_question() -> None:
    """A gt-<request_id> note is the other agent's work: Waiting on, not an actual person's first step (G2)."""

    started = task("brn_a", "Draft the deck", body="for Sam")
    hub_note = note("brn_a", session_id="gt-req_0123456789abcdef")
    assert note_for_task(started.record, (hub_note,)) is None
    assert build_review(inputs((started,), open_notes=(hub_note,))).clarify == ()


@pytest.mark.parametrize(
    "variant",
    [
        {"state": "waiting"},
        {"state": "blocked"},
        {"state": "parked", "parked_until": "2026-10-05"},
        {"state": "done"},
        {"level": "suggested"},
        {"level": "possible"},
    ],
)
def test_only_an_accepted_open_item_is_asked_for_its_first_step(variant: dict[str, Any]) -> None:
    entry = task("brn_a", "Call the bank", body="about the fee", **variant)
    assert pick_thin_tasks((entry,), (), (), open_notes=(note("brn_a"),)) == ()


def test_first_step_questions_share_the_cap_of_three_oldest_first_one_question_per_task() -> None:
    thin_old = task("brn_t1", "Fix it", created_at=NOW - timedelta(days=9))
    started_older = task("brn_s1", "Book flights", body="for Tokyo", created_at=NOW - timedelta(days=10))
    started_both = task("brn_s2", "Groceries", created_at=NOW - timedelta(days=8))  # thin and started
    thin_new = task("brn_t2", "Email Tommy", created_at=NOW - timedelta(days=2))
    notes = (note("brn_s1"), note("brn_s2"))
    everything = (thin_new, started_both, thin_old, started_older)
    review = build_review(inputs(everything, open_notes=notes))
    assert CLARIFY_CAP == 3
    assert [(picked.record_id, picked.rule) for picked in review.clarify] == [
        ("brn_s1", CLARIFY_FIRST_STEP_RULE),
        ("brn_t1", CLARIFY_RULE),
        ("brn_s2", CLARIFY_FIRST_STEP_RULE),  # asked for its first step, not also as thin
    ]
    assert [entry.record.id for entry in pick_thin_tasks(everything, (), (), open_notes=notes)] == [
        "brn_s1", "brn_t1", "brn_s2",
    ]


def test_the_first_step_question_is_asked_once_unless_the_task_changes() -> None:
    started = task("brn_a", "Call the bank", body="about the card fee")
    open_notes = (note("brn_a"),)
    assert build_review(inputs((started,), history=(first_step_asked(started),), open_notes=open_notes)).clarify == ()
    # A thin question's history is its own: it doesn't stop the first-step one.
    assert [picked.record_id for picked in build_review(
        inputs((started,), history=(asked(started),), open_notes=open_notes)
    ).clarify] == ["brn_a"]
    changed = task("brn_a2", "Call the bank", body="about the card fee, today", supersedes=("brn_a",))
    assert [picked.record_id for picked in build_review(
        inputs((started, changed), history=(first_step_asked(started),), open_notes=open_notes)
    ).clarify] == ["brn_a2"]
