"""Sorting new things once (Ciel and Brain Home PRD §4.1; build plan slice 4): pure functions, no IO.

An agent pre-sorts each new item into one of seven outcomes and Nic approves the sorting in the
next 06:00 review; his words still decide what becomes a task. Each outcome needs its parts, and
an item missing one is refused, not half-filed: `sorting_gaps` names what is missing, the way
gsd's `disposition.go` refuses an incomplete disposition. An outcome is not stored: it is the shape
the record ends up in (a next action is an open task with a block type, a first step and a rough
length; waiting on someone is a waiting item with a ``waiting_on`` link; and so on).

Any open task Nic accepted with no block type counts as unsorted, so the first sorting pass covers
everything already in Brain Home. The review's "To sort" takes the oldest `SORTING_CAP` a morning,
agents' suggestions and unsorted tasks together, and they come back each morning until sorted.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from brain_core.records import RECORD_SCHEMA_V2, Record, first_saved

# The seven outcomes (PRD §4.1). "Do it now" is offered, never saved: done, it is done; put off,
# it becomes a next action.
DO_NOW = "do_now"
NEXT_ACTION = "next_action"
WAITING_ON = "waiting_on"
RAISE_WITH = "raise_with"
SOMEDAY = "someday"
REFERENCE = "reference"
DROP = "drop"
SORT_OUTCOMES: tuple[str, ...] = (DO_NOW, NEXT_ACTION, WAITING_ON, RAISE_WITH, SOMEDAY, REFERENCE, DROP)
SAVED_OUTCOMES: frozenset[str] = frozenset(SORT_OUTCOMES) - {DO_NOW}
# The two-minute rule, where GTD places it: when an item first comes in (Nic, 28 Sep: "if it
# takes 2 mins to do sth not that urgent or high roi, it still makes sense to do it first").
TWO_MINUTES = 2
# Accepted - Nic, 28 Sep 2026 ("10 a morning"): the review's sorting cap, separate from its three
# clarifying questions.
SORTING_CAP = 10


def sorting_gaps(
    outcome: str,
    *,
    kind: str,
    state: str,
    data: Mapping[str, Any],
    links: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, ...]:
    """What a record still needs to be sorted as ``outcome``, in the words a refusal names them;
    empty when it is complete. ``data`` is the record's data as it will be saved."""

    if outcome not in SAVED_OUTCOMES:
        return (f"sort one of {', '.join(sorted(SAVED_OUTCOMES))}",)
    if outcome == REFERENCE:
        return () if kind == "fact" else ("kind fact (a reference is a plain fact)",)
    if kind != "item":
        return ("kind item",)
    timing = data.get("timing") or {}
    gaps: list[str] = []
    if outcome == NEXT_ACTION:
        if state != "open":
            gaps.append("state open")
        if data.get("size", "task") != "task":
            gaps.append("size task")
        if not data.get("block"):
            gaps.append("block (deep_focus, admin or errands)")
        if not (data.get("first_step") or "").strip():
            gaps.append("first_step (the very next physical action)")
        if timing.get("duration_minutes") is None:
            gaps.append("rough_minutes")
    elif outcome == WAITING_ON:
        if state != "waiting":
            gaps.append("state waiting")
        if not any(link.get("type") == "waiting_on" and link.get("target") for link in links):
            gaps.append("a waiting_on link naming who")
    elif outcome == RAISE_WITH:
        if not (data.get("raise_with") or "").strip():
            gaps.append("raise_with (who to raise it with)")
    elif outcome == SOMEDAY:
        if data.get("level") != "possible":
            gaps.append("level possible (an idea)")
    elif outcome == DROP:
        if state != "dropped":
            gaps.append("state dropped")
    return tuple(gaps)


def _is_v2_item(record: Record) -> bool:
    return record.schema == RECORD_SCHEMA_V2 and record.kind == "item"


def is_unsorted(record: Record) -> bool:
    """An open task Nic accepted with no block type (PRD §4.1): routines count, goals don't."""

    data = record.data
    return (
        _is_v2_item(record)
        and data.get("level") == "accepted"
        and record.state == "open"
        and data.get("size", "task") == "task"
        and not data.get("block")
    )


def awaits_yes(record: Record) -> bool:
    """An agent's suggestion still open: pre-sorted, it waits for Nic's yes (PRD §4.1)."""

    return _is_v2_item(record) and record.data.get("level") == "suggested" and record.state == "open"


def fits_two_minutes(record: Record) -> bool:
    """Its rough length is two minutes or less: offer to do it right then."""

    minutes = (record.data.get("timing") or {}).get("duration_minutes")
    return minutes is not None and minutes <= TWO_MINUTES


def to_sort(records: Iterable[Record]) -> tuple[Record, ...]:
    """Everything waiting to be sorted, oldest first (by when its first version was saved), then
    record ID: agents' suggestions awaiting Nic's yes, and unsorted tasks. Newest versions only."""

    return tuple(sorted(
        (record for record in records if awaits_yes(record) or is_unsorted(record)),
        key=lambda record: (first_saved(record), record.id),
    ))
