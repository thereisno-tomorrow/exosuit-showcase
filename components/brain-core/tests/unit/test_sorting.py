"""Sorting new things once (Ciel and Brain Home PRD §4.1; build plan slice 4): each outcome's parts,
what counts as unsorted, and the order of the morning's "To sort"."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.sorting import (
    SAVED_OUTCOMES,
    SORT_OUTCOMES,
    SORTING_CAP,
    TWO_MINUTES,
    awaits_yes,
    fits_two_minutes,
    is_unsorted,
    sorting_gaps,
    to_sort,
)
from brain_core.timewords import SINGAPORE

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=SINGAPORE)


def item(
    id_suffix: str,
    *,
    level: str = "accepted",
    state: str = "open",
    size: str = "task",
    saved: datetime = NOW,
    supersedes: tuple[str, ...] = (),
    **data: object,
) -> Record:
    return Record(
        schema=RECORD_SCHEMA_V2, id=f"brn_20261005_000000_{id_suffix}", kind="item", title=f"task {id_suffix}",
        state=state, created_at=saved, updated_at=saved, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=supersedes, dedupe_key=None,
        data={"level": level, "size": size, **data}, body="", source={"said_by": "owner"}, links=(),
    )


NEXT_ACTION = {
    "block": "admin", "first_step": "Find the accountant's number", "timing": {"duration_minutes": 10},
}


def test_the_seven_outcomes_and_the_six_that_are_saved():
    assert SORT_OUTCOMES == ("do_now", "next_action", "waiting_on", "raise_with", "someday", "reference", "drop")
    assert SAVED_OUTCOMES == frozenset(SORT_OUTCOMES) - {"do_now"}
    assert (SORTING_CAP, TWO_MINUTES) == (10, 2)


def test_a_next_action_needs_its_block_first_step_and_rough_length():
    """Acceptance case 3: an agent's next action with no block type is refused and sent back."""

    assert sorting_gaps("next_action", kind="item", state="open", data=NEXT_ACTION) == ()
    assert sorting_gaps("next_action", kind="item", state="open", data={}) == (
        "block (deep_focus, admin or errands)", "first_step (the very next physical action)", "rough_minutes",
    )
    no_block = {key: value for key, value in NEXT_ACTION.items() if key != "block"}
    assert sorting_gaps("next_action", kind="item", state="open", data=no_block) == ("block (deep_focus, admin or errands)",)
    blank_step = {**NEXT_ACTION, "first_step": "   "}
    assert sorting_gaps("next_action", kind="item", state="open", data=blank_step) == (
        "first_step (the very next physical action)",
    )
    assert "state open" in sorting_gaps("next_action", kind="item", state="waiting", data=NEXT_ACTION)
    assert "size task" in sorting_gaps("next_action", kind="item", state="open", data={**NEXT_ACTION, "size": "goal"})


def test_each_other_outcome_needs_its_own_part():
    link = ({"type": "waiting_on", "target": "Sam"},)
    assert sorting_gaps("waiting_on", kind="item", state="waiting", data={}, links=link) == ()
    assert sorting_gaps("waiting_on", kind="item", state="open", data={}) == ("state waiting", "a waiting_on link naming who")
    assert sorting_gaps("raise_with", kind="item", state="open", data={"raise_with": "Sam"}) == ()
    assert sorting_gaps("raise_with", kind="item", state="open", data={}) == ("raise_with (who to raise it with)",)
    assert sorting_gaps("someday", kind="item", state="open", data={"level": "possible"}) == ()
    assert sorting_gaps("someday", kind="item", state="open", data={"level": "suggested"}) == ("level possible (an idea)",)
    assert sorting_gaps("reference", kind="fact", state="current", data={}) == ()
    assert sorting_gaps("reference", kind="item", state="open", data={}) == ("kind fact (a reference is a plain fact)",)
    assert sorting_gaps("drop", kind="item", state="dropped", data={}) == ()
    assert sorting_gaps("drop", kind="item", state="open", data={}) == ("state dropped",)
    assert sorting_gaps("next_action", kind="fact", state="current", data={}) == ("kind item",)


def test_do_it_now_and_unknown_outcomes_are_never_saved():
    for outcome in ("do_now", "later", ""):
        assert sorting_gaps(outcome, kind="item", state="open", data=NEXT_ACTION) == (
            "sort one of drop, next_action, raise_with, reference, someday, waiting_on",
        )


def test_an_accepted_open_task_with_no_block_type_is_unsorted():
    """PRD §4.1: so the first sorting pass covers everything already in Brain Home."""

    assert is_unsorted(item("a1"))
    assert is_unsorted(item("a2", repeat="weekly:sun"))  # a routine is sorted like any task
    assert not is_unsorted(item("a3", block="errands"))
    assert not is_unsorted(item("a4", size="goal"))
    assert not is_unsorted(item("a5", state="waiting"))
    assert not is_unsorted(item("a6", level="suggested"))
    assert not is_unsorted(item("a7", level="possible"))


def test_a_suggestion_still_open_waits_for_nics_yes():
    assert awaits_yes(item("a1", level="suggested"))
    assert not awaits_yes(item("a2", level="suggested", state="dropped"))
    assert not awaits_yes(item("a3"))


def test_two_minutes_or_less_is_offered_now():
    assert fits_two_minutes(item("a1", timing={"duration_minutes": 2}))
    assert not fits_two_minutes(item("a2", timing={"duration_minutes": 3}))
    assert not fits_two_minutes(item("a3"))


def test_to_sort_is_suggestions_and_unsorted_tasks_oldest_first_by_their_first_version():
    old_suggestion = item("b1", level="suggested", saved=NOW - timedelta(days=3))
    # Changed an hour ago, but first saved 30 days ago: its first version decides.
    changed = item("b2", saved=NOW - timedelta(hours=1), supersedes=("brn_20260905_000000_abcd",))
    fresh = item("b3", saved=NOW - timedelta(days=1))
    sorted_task = item("b4", block="admin", saved=NOW - timedelta(days=90))
    idea = item("b5", level="possible", saved=NOW - timedelta(days=90))
    assert [record.id for record in to_sort((fresh, sorted_task, old_suggestion, idea, changed))] == [
        changed.id, old_suggestion.id, fresh.id,
    ]
    assert changed.supersedes and datetime(2026, 9, 5, tzinfo=UTC) < NOW


@pytest.mark.parametrize("outcome", sorted(SAVED_OUTCOMES))
def test_every_saved_outcome_names_what_is_missing_in_plain_words(outcome: str):
    gaps = sorting_gaps(outcome, kind="item", state="open", data={"level": "accepted"})
    assert all(isinstance(gap, str) and gap for gap in gaps)
