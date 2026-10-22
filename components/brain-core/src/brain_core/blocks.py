"""Time blocks (Ciel and Brain Home PRD §4.3; build plan slice 10): pure functions, no IO.

Nic sets a weekly pattern of blocks on his calendar - deep focus, admin and errands - and Ciel only
reads it; it never moves or adds a block, and says nothing when one starts. Inside a block, "what
next?" offers only tasks of that block's type. A task of another type with a deadline waits for its
own kind of block if one comes before the deadline; if none does, it comes up now, saying so ("due
at 5 pm, and there's no admin block before then"). Outside any block nothing is assumed: every
task ranks together and no question is asked (Nic, 29 Sep 2026). One line from Nic overrides the block: "I'm out" (errands), "I'm wiped" (admin), "I've got 20
minutes" (whatever fits).

Reading the calendar needs Nic's Google consent (ROA-18), so all of this ships switched off:
``[orientation] blocks``. Off, "what next?" and "where am I?" answer exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from brain_core.records import ITEM_BLOCKS
from brain_core.review import CalendarEvent

DEEP_FOCUS = "deep_focus"
ADMIN = "admin"
ERRANDS = "errands"
# The block names Ciel recognises on the calendar, by type: an event whose title is one of these, or
# starts with one, ignoring case (Proposed - Nic to confirm; PRD §8 keeps his own names as one of
# his preferences, a later addition).
DEFAULT_BLOCK_NAMES: Mapping[str, tuple[str, ...]] = {
    DEEP_FOCUS: ("deep focus", "deep work"),
    ADMIN: ("admin",),
    ERRANDS: ("errands",),
}
BLOCK_WORDS: Mapping[str, str] = {DEEP_FOCUS: "deep-focus", ADMIN: "admin", ERRANDS: "errands"}
# Nic's one-line overrides (PRD §4.3): what each makes the moment.
OVERRIDE_KINDS: Mapping[str, str | None] = {"out": ERRANDS, "wiped": ADMIN, "minutes": None}
# A task sorted into no block counts as admin-sized, and a focus project's task from its sitrep as deep
# focus (Nic, 29 Sep 2026: kept as built).
UNSORTED_KIND = ADMIN
PROJECT_TASK_KIND = DEEP_FOCUS


@dataclass(frozen=True, slots=True)
class Block:
    kind: str
    title: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class Override:
    """Nic's own line, as the agent passed it: ``mode`` is ``out``, ``wiped`` or ``minutes``, and
    ``words`` what he said."""

    mode: str
    words: str
    minutes: int | None = None


@dataclass(frozen=True, slots=True)
class Moment:
    """What "what next?" works with right now: the kind of work, why (``block``, ``override`` or
    ``no_block``), the block it's in, and the minutes Nic said he has. ``kind`` is None outside any
    block unless Nic's own line names one: then every kind of task fits."""

    kind: str | None
    source: str
    block: Block | None = None
    minutes: int | None = None
    words: str | None = None


def block_kind(title: str) -> str | None:
    name = " ".join(title.split()).casefold()
    for kind, names in DEFAULT_BLOCK_NAMES.items():
        if any(name == known or name.startswith(known + " ") or name.startswith(known + ":") for known in names):
            return kind
    return None


def blocks_from_calendar(events: Iterable[CalendarEvent]) -> tuple[Block, ...]:
    """Today's blocks among the calendar's events, in time order. All-day events are never blocks."""

    blocks = []
    for event in events:
        kind = None if event.all_day else block_kind(event.title)
        if kind is not None:
            blocks.append(Block(kind=kind, title=event.title, start=event.start, end=event.end))
    return tuple(sorted(blocks, key=lambda block: (block.start, block.end, block.title)))


def moment(blocks: Sequence[Block], now: datetime, override: Override | None = None) -> Moment:
    """The moment's kind of work: Nic's own line first, then the block he's in, else none."""

    current = next((block for block in blocks if block.start <= now < block.end), None)
    if override is not None:
        if override.mode not in OVERRIDE_KINDS:
            raise ValueError(f"override mode must be one of {sorted(OVERRIDE_KINDS)}")
        kind = OVERRIDE_KINDS[override.mode] or (current.kind if current is not None else None)
        return Moment(kind=kind, source="override", block=current, minutes=override.minutes, words=override.words)
    if current is not None:
        return Moment(kind=current.kind, source="block", block=current)
    return Moment(kind=None, source="no_block")


def next_block(blocks: Sequence[Block], kind: str, after: datetime, before: datetime) -> Block | None:
    """The first block of ``kind`` starting at or after ``after`` and before ``before``."""

    return next((block for block in blocks if block.kind == kind and after <= block.start < before), None)


def task_kind(block: object, *, project_task: bool = False) -> str:
    if project_task:
        return PROJECT_TASK_KIND
    return block if block in ITEM_BLOCKS else UNSORTED_KIND
