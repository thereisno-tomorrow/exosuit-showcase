"""The placement engine (step I; PRD §4.3, Accepted - Nic, 29 Sep 2026): pure functions, no IO.

Once Nic has settled his day in the 6am review, Ciel draws the day's blocks inside his working
window from what that day's work needs, and places each task in a block of its kind:

- **Scope** (`scope`): only what he settled today - his picks and what's due today (a missed item he
  recommitted to today has today as its new date). The one exception is the deep-focus minimum,
  filled from his other open deep-focus work by points.
- **Blocks** (`draw_day`): admin first ("i need most strength for admin"), then deep focus sized
  to the day's deep work - at least 2 hours when deep work is open, in blocks of at least 90
  minutes, at most 6 hours - then one errands trip as late as it fits, then admin around meetings.
  The window is never stretched; time left over stays free.
- **Slots**: deadlines first, then points (`orientation.rank`, so the calendar agrees with "what
  next?"); a task waiting on another goes after it; deep work may split into parts of at least an
  hour, admin and errands never. What doesn't fit is reported, never dropped.
- **Reflow** (`replan`): the blocks stay put for the day; only future slots move, and a slot that
  still fits keeps its place. A task that no longer fits its blocks - a pick added later, one Nic
  moved, a longer length - grows a block of its kind into free time in his window (Nic, 29 Sep
  2026: "ya auto grow into free time its fine"), next to a block of that kind where it can.

The same inputs always give the same plan. GT reads the calendar and Brain Home, calls this, and
writes the result (build plan slices 3 and 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable, Mapping, Sequence

from brain_core import work as reader
from brain_core.blocks import ADMIN, BLOCK_WORDS, DEEP_FOCUS, ERRANDS, task_kind
from brain_core.orientation import _clock_word, rank
from brain_core.records import Record
from brain_core.review import ItemRecord
from brain_core.timewords import SINGAPORE

# A slot's title starts with this, so it can never read as a block ("Admin ...") to
# `blocks.block_kind`, even for a task called "Admin for the lease" (step I §4).
SLOT_PREFIX = "▸ "

NO_LENGTH = "no_length"
DOESNT_FIT = "doesnt_fit"
WAITS_ON = "waits_on"
DAY_OFF = "day_off"
LATER_DAY = "later_day"
UNPLACED_REASONS = frozenset({NO_LENGTH, DOESNT_FIT, WAITS_ON, DAY_OFF, LATER_DAY})

_KINDS = (ADMIN, DEEP_FOCUS, ERRANDS)
_STEP = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Rules:
    """Step I §8's numbers (Nic, 29 Sep 2026, where marked there)."""

    buffer_after_meeting_minutes: int = 10
    deep_focus_min_minutes: int = 120
    deep_focus_block_min_minutes: int = 90
    deep_focus_max_minutes: int = 360
    deep_focus_blocks_max: int = 2
    split_min_minutes: int = 60


@dataclass(frozen=True, slots=True)
class DayChanges:
    """Nic's one-line changes to a day: ``deep_focus_minutes`` (0 for "no deep focus today", or
    his size for it) and ``ends_at`` ("I'm done at 4")."""

    deep_focus_minutes: int | None = None
    ends_at: time | None = None


@dataclass(frozen=True, slots=True)
class Busy:
    """Time taken by something the engine didn't place: a meeting, or a task's own timed entry."""

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class Work:
    """One task to place. ``key`` is its first record ID, the same through every change of it (as
    the calendar marker uses); ``ids`` every version's ID, which picks and links may name.
    ``belongs_to`` is the current parent's title, supplied by the one work reader."""

    key: str
    ids: tuple[str, ...]
    title: str
    first_step: str | None
    kind: str
    minutes: int
    points: float
    due_at: datetime | None = None
    picked: bool = False
    waits_on: tuple[str, ...] = ()
    fill: bool = False
    belongs_to: str | None = None
    # Packing order only, never a work-model wait or an explicit after dependency.
    _order_after: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class DayBlock:
    kind: str
    start: datetime
    end: datetime

    @property
    def title(self) -> str:
        return {ADMIN: "Admin", DEEP_FOCUS: "Deep focus", ERRANDS: "Errands"}[self.kind]


@dataclass(frozen=True, slots=True)
class Slot:
    """A slot keyed by the thing's first ID. ``belongs_to`` names its current parent.

    Work-model titles use the next action or part, followed by its parent in parentheses.
    """

    key: str
    kind: str
    start: datetime
    end: datetime
    task_title: str
    first_step: str | None = None
    part: int = 1
    parts: int = 1
    late: bool = False
    belongs_to: str | None = None

    @property
    def title(self) -> str:
        """The prefixed action/part and what it belongs to (work-model plan 6.3)."""
        words = self.first_step or self.task_title
        if self.belongs_to:
            words += f" ({self.belongs_to})"
        if self.parts > 1:
            words += f" (part {self.part} of {self.parts})"
        return SLOT_PREFIX + words

    @property
    def minutes(self) -> int:
        return int((self.end - self.start) / timedelta(minutes=1))


@dataclass(frozen=True, slots=True)
class Unplaced:
    key: str
    title: str
    reason: str
    kind: str | None = None
    minutes: int | None = None


@dataclass(frozen=True, slots=True)
class Scope:
    """What the day plans: ``work`` (his picks and what's due today), ``deep_pool`` (his other open
    deep-focus work, by points, for the minimum) and what can't be placed at all."""

    work: tuple[Work, ...]
    deep_pool: tuple[Work, ...] = ()
    unplaced: tuple[Unplaced, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    day: date
    blocks: tuple[DayBlock, ...] = ()
    slots: tuple[Slot, ...] = ()
    unplaced: tuple[Unplaced, ...] = ()
    fill_keys: tuple[str, ...] = field(default=())


# --- scope ---------------------------------------------------------------------------------------


def _key(record: Record) -> str:
    return record.supersedes[0] if record.supersedes else record.id


def _ids(record: Record) -> tuple[str, ...]:
    return (record.id, *record.supersedes)


def _minutes(record: Record) -> int | None:
    timing = record.data.get("timing") or {}
    minutes = timing.get("padded_minutes") or timing.get("duration_minutes")
    return int(minutes) if minutes else None


def _on_day(value: Mapping[str, object] | None, today: date) -> bool:
    """A reader date's day or inclusive planned period, in Singapore."""

    if not value:
        return False
    day = today.isoformat()
    if value.get("day"):
        return value["day"] == day
    return bool(value.get("start") and value.get("end") and str(value["start"]) <= day <= str(value["end"]))


def scope(items: Sequence[ItemRecord], today: date, now: datetime, picked_today: frozenset[str]) -> Scope:
    """Current up work from the one reader: picks, today's deadlines and planned work.

    A pick with parts expands in order before its own length; waiting and later-day parts
    are reported, appointments excluded. Other up deep work fills the existing minimum.
    Missing lengths are reported, never invented (work-model plan W9 and 6.3).
    """

    index = reader.index_things(entry.record for entry in items)
    up = {reader.first_id(record) for record in reader.up_things(index, now)}
    paths = {entry.record.id: entry.path for entry in items}
    current = tuple(ItemRecord(record, paths[record.id]) for record in index.values())
    points = {candidate.item.record.id: candidate.points or 0.0
              for candidate in rank(current, today, picked_today=picked_today, now=now)
              if candidate.item is not None}
    picked = {key for key, record in index.items() if picked_today.intersection(_ids(record))}
    selected: set[str] = set()
    expanded: set[str] = set()
    preceding: dict[str, tuple[str, ...]] = {}
    own_slots: set[str] = set()
    for key in picked:
        parent = index[key]
        if reader.kind_of(key, index) != "task" or reader.status_of(key, index, now) in ("parked", "ended"):
            continue
        parts = sorted((part for part in index.values() if part.data.get("parent") == key and part.state not in ("done", "dropped")),
                       key=lambda part: (part.data.get("order", 1000), part.created_at, reader.first_id(part)))
        if not parts:
            continue
        previous: list[str] = []
        for part in parts:
            part_key = reader.first_id(part)
            selected.add(part_key)
            expanded.add(part_key)
            preceding[part_key] = tuple(previous)
            dates = reader.dates_of(part_key, index)
            planned = dates.get("planned") or {}
            later = str(planned.get("day") or planned.get("start") or "") > today.isoformat()
            if part_key in up and not later and not dates.get("appointment") and _minutes(part) is not None:
                previous.append(part_key)
        # The picked-parent exception ignores its parts, while the same reader
        # still enforces the parent's own after/needs/date and ancestor restrictions.
        own_index = {ref: record for ref, record in index.items() if record.data.get("parent") != key}
        if _minutes(parent) is not None and reader.is_up(key, own_index, now):
            selected.add(key)
            own_slots.add(key)
            preceding[key] = tuple(previous)

    work: list[Work] = []
    pool: list[Work] = []
    unplaced: list[Unplaced] = []
    for key, record in index.items():
        dates = reader.dates_of(key, index)
        if dates.get("appointment"):
            continue
        deadline = dates.get("deadline")
        due_today = _on_day(deadline, today)
        planned_today = _on_day(dates.get("planned"), today)
        in_scope = key in picked or key in selected or due_today or planned_today
        kind = task_kind(record.data.get("block"))
        minutes = _minutes(record)
        planned = dates.get("planned") or {}
        later = str(planned.get("day") or planned.get("start") or "") > today.isoformat()
        if key in expanded and later:
            unplaced.append(Unplaced(key, record.title, LATER_DAY, kind, minutes))
            continue
        if key not in up and key not in own_slots:
            if in_scope and reader.status_of(key, index, now) == "waiting":
                unplaced.append(Unplaced(key, record.title, WAITS_ON, kind, minutes))
            continue
        if minutes is None:
            if in_scope and key not in selected:
                unplaced.append(Unplaced(key, record.title, NO_LENGTH, kind))
            elif key in expanded:
                unplaced.append(Unplaced(key, record.title, NO_LENGTH, kind))
            continue
        after = tuple(ref for ref in record.data.get("after", ())
                      if ref not in index or reader.status_of(ref, index, now) != "ended")
        waits_on = tuple(dict.fromkeys(after))
        parent = index.get(record.data.get("parent"))
        action = record.data.get("next_action") or {}
        marked = any(mark.get("mark") == "agents_wording" and mark.get("part") == "next_action"
                     for mark in record.data.get("review_marks", ()))
        due_at = (datetime.combine(today, time.fromisoformat(str(deadline.get("at") or "23:59")), tzinfo=SINGAPORE)
                  if due_today else None)
        task = Work(key=key, ids=_ids(record), title=record.title,
                    first_step=action.get("text") if not action.get("done") and not marked else None,
                    kind=kind, minutes=minutes, points=points.get(record.id, 0.0), due_at=due_at,
                    picked=key in picked or key in expanded, waits_on=waits_on,
                    belongs_to=parent.title if parent is not None else None,
                    _order_after=preceding.get(key, ()))
        if in_scope:
            work.append(task)
        elif kind == DEEP_FOCUS and not waits_on and not later:
            pool.append(replace(task, fill=True))
    pool.sort(key=lambda task: (-task.points, task.key))
    return Scope(work=tuple(work), deep_pool=tuple(pool), unplaced=tuple(sorted(unplaced, key=lambda u: u.key)))


def _order(tasks: Iterable[Work]) -> list[Work]:
    """Deadlines then points, preserving selected parts' packing order within that list."""

    far = datetime.max.replace(tzinfo=SINGAPORE)
    pending = sorted(tasks, key=lambda task: (task.due_at is None, task.due_at or far, -task.points, task.key))
    ordered: list[Work] = []
    while pending:
        keys = {task.key for task in pending}
        task = next((task for task in pending if not keys.intersection(task._order_after)), None)
        if task is None:
            raise ValueError("placement part order cycle")
        pending.remove(task)
        ordered.append(task)
    return ordered


# --- free time -----------------------------------------------------------------------------------


Interval = tuple[datetime, datetime]


def _earliest(now: datetime) -> datetime:
    """The first time the engine plans from: the next 5-minute mark at least a minute after
    ``now``, so a slot written a few seconds after planning never starts in the past."""

    return _ceil_step(now + timedelta(minutes=1))


def _ceil_step(moment: datetime) -> datetime:
    base = moment.replace(second=0, microsecond=0)
    if base < moment:
        base += timedelta(minutes=1)
    remainder = base.minute % 5
    return base + timedelta(minutes=(5 - remainder) % 5)


def _subtract(intervals: Iterable[Interval], start: datetime, end: datetime) -> list[Interval]:
    kept: list[Interval] = []
    for low, high in intervals:
        if end <= low or start >= high:
            kept.append((low, high))
            continue
        if low < start:
            kept.append((low, start))
        if end < high:
            kept.append((end, high))
    return kept


def _join(intervals: Iterable[Interval]) -> list[Interval]:
    """Sorted, with touching or overlapping stretches joined into one, so a task can use both."""

    joined: list[Interval] = []
    for low, high in sorted(intervals):
        if joined and low <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], high))
        else:
            joined.append((low, high))
    return joined


def _span(interval: Interval) -> int:
    return int((interval[1] - interval[0]) / timedelta(minutes=1))


def free_time(
    start: datetime, end: datetime, busy: Iterable[Busy], now: datetime, rules: Rules, taken: Iterable[Interval] = (),
) -> list[Interval]:
    """The window from ``start`` to ``end``, minus busy time and the buffer after each, minus what's
    ``taken`` already, minus the past (`_earliest`)."""

    first = max(start, _earliest(now))
    free: list[Interval] = [(first, end)] if first < end else []
    buffer = timedelta(minutes=rules.buffer_after_meeting_minutes)
    for event in busy:
        free = _subtract(free, event.start, event.end + buffer)
    for low, high in taken:
        free = _subtract(free, low, high)
    return sorted(interval for interval in free if interval[0] < interval[1])


# --- packing -------------------------------------------------------------------------------------


def _take(rooms: list[Interval], start: datetime, end: datetime) -> None:
    rooms[:] = sorted(_subtract(rooms, start, end))


def _fit(rooms: Sequence[Interval], minutes: int, earliest: datetime | None) -> Interval | None:
    need = timedelta(minutes=minutes)
    for low, high in rooms:
        begin = max(low, earliest) if earliest is not None else low
        if begin + need <= high:
            return begin, begin + need
    return None


def _fit_by(rooms: Sequence[Interval], minutes: int, earliest: datetime | None, deadline: datetime | None) -> tuple[Interval, bool] | None:
    """The earliest room that ends by the deadline; failing that, the earliest room at all, late."""

    first = _fit(rooms, minutes, earliest)
    if first is None:
        return None
    if deadline is None or first[1] <= deadline:
        return first, False
    return first, True


def _split(rooms: Sequence[Interval], minutes: int, earliest: datetime | None, split_min: int) -> list[Interval] | None:
    """Deep work across rooms, earliest first: whole where it fits, else parts of at least
    ``split_min`` minutes each. None when it can't all be placed."""

    whole = _fit(rooms, minutes, earliest)
    if whole is not None:
        return [whole]
    parts: list[Interval] = []
    remaining = minutes
    for low, high in rooms:
        begin = max(low, earliest) if earliest is not None else low
        room = int((high - begin) / timedelta(minutes=1))
        if room <= 0:
            continue
        if room >= remaining:
            parts.append((begin, begin + timedelta(minutes=remaining)))
            return parts
        part = min(room, remaining - split_min)
        if part >= split_min:
            parts.append((begin, begin + timedelta(minutes=part)))
            remaining -= part
    return None


def _pack(
    tasks: Sequence[Work], rooms: list[Interval], rules: Rules, placed_end: dict[str, datetime],
    *, splittable: bool, report: bool = True,
) -> tuple[list[Slot], list[Unplaced]]:
    slots: list[Slot] = []
    unplaced: list[Unplaced] = []
    for task in tasks:
        blockers = [placed_end.get(key) for key in task.waits_on]
        order_ends = [placed_end.get(key) for key in task._order_after]
        if any(end is None for end in (*blockers, *order_ends)):
            if report:
                unplaced.append(Unplaced(task.key, task.title, WAITS_ON, task.kind, task.minutes))
            continue
        earliest = max((*blockers, *order_ends)) if blockers or order_ends else None
        if splittable:
            parts = _split(rooms, task.minutes, earliest, rules.split_min_minutes)
            if parts is None:
                if report:
                    unplaced.append(Unplaced(task.key, task.title, DOESNT_FIT, task.kind, task.minutes))
                continue
            late = task.due_at is not None and parts[-1][1] > task.due_at
            for number, (start, end) in enumerate(parts, 1):
                slots.append(Slot(task.key, task.kind, start, end, task.title, task.first_step, number, len(parts), late, task.belongs_to))
                _take(rooms, start, end)
            placed_end[task.key] = parts[-1][1]
            continue
        found = _fit_by(rooms, task.minutes, earliest, task.due_at)
        if found is None:
            if report:
                unplaced.append(Unplaced(task.key, task.title, DOESNT_FIT, task.kind, task.minutes))
            continue
        (start, end), late = found
        slots.append(Slot(task.key, task.kind, start, end, task.title, task.first_step, late=late, belongs_to=task.belongs_to))
        _take(rooms, start, end)
        placed_end[task.key] = end
    return slots, unplaced


def _retry_waiting(
    unplaced: list[Unplaced], work: Mapping[str, Work], rooms_for: Callable[[str], list[Interval]], rules: Rules,
    placed_end: dict[str, datetime],
) -> list[Slot]:
    """Place what was reported as waiting once everything it waits on has a slot, until nothing
    more can be placed: tasks are packed kind by kind in one pass, so a blocker can land after
    the task that waits on it (a lower-points admin blocker, or an errand waiting on admin).
    ``unplaced`` is updated in place; one that still has no room becomes "doesn't fit"."""

    placed_all: list[Slot] = []
    progress = True
    while progress:
        progress = False
        waiting = {entry.key: entry for entry in unplaced if entry.reason == WAITS_ON and entry.key in work}
        for task in _order(work[key] for key in waiting):
            if any(key not in placed_end for key in task.waits_on):
                continue
            if any(key not in placed_end and key in waiting for key in task._order_after):
                continue
            # Order cannot turn a part that didn't fit into a work dependency.
            # Predecessors still waiting are retried first; terminal omissions are skipped.
            task = replace(task, _order_after=tuple(key for key in task._order_after if key in placed_end))
            unplaced.remove(waiting[task.key])
            placed, missing = _pack([task], rooms_for(task.kind), rules, placed_end, splittable=task.kind == DEEP_FOCUS)
            placed_all += placed
            unplaced += [entry for entry in missing if not task.fill]
            progress = True
    return placed_all


def _after_kept(new: Sequence[Slot], kept: Sequence[Slot]) -> list[Slot]:
    """Number a task's new parts after the parts it keeps (placement-02), so (key, part) never
    names two slots: a pin, a passed part or a moved task's second slot stay one each."""

    last_part: dict[str, int] = {}
    for slot in kept:
        last_part[slot.key] = max(slot.part, last_part.get(slot.key, 0))
    fresh: dict[str, int] = {}
    for slot in new:
        fresh[slot.key] = fresh.get(slot.key, 0) + 1
    return [
        replace(slot, part=slot.part + last_part[slot.key], parts=last_part[slot.key] + fresh[slot.key])
        if slot.key in last_part else slot
        for slot in new
    ]


def _merge(slots: Iterable[Slot], kind: str) -> list[DayBlock]:
    blocks: list[DayBlock] = []
    for slot in sorted((slot for slot in slots if slot.kind == kind), key=lambda slot: slot.start):
        if blocks and blocks[-1].end == slot.start:
            blocks[-1] = DayBlock(kind, blocks[-1].start, slot.end)
        else:
            blocks.append(DayBlock(kind, slot.start, slot.end))
    return blocks


# --- drawing the day -----------------------------------------------------------------------------


def _deep_blocks(free: Sequence[Interval], target: int, block_min: int, blocks_max: int) -> list[Interval]:
    """Deep focus in the earliest stretches long enough for a block, at most ``blocks_max``."""

    blocks: list[Interval] = []
    remaining = target
    for low, high in free:
        if remaining <= 0 or len(blocks) >= blocks_max:
            break
        span = _span((low, high))
        if span >= min(block_min, remaining):
            size = min(remaining, span)
            blocks.append((low, low + timedelta(minutes=size)))
            remaining -= size
    return blocks


def _deep_target(work: Sequence[Work], pool: Sequence[Work], rules: Rules, changes: DayChanges) -> int:
    if changes.deep_focus_minutes is not None:
        return max(0, min(changes.deep_focus_minutes, rules.deep_focus_max_minutes))
    asked = sum(task.minutes for task in work if task.kind == DEEP_FOCUS)
    available = asked + sum(task.minutes for task in pool)
    return min(rules.deep_focus_max_minutes, max(asked, min(rules.deep_focus_min_minutes, available)))


def _opener(admin: Sequence[Work], free: Sequence[Interval]) -> list[Work]:
    """Admin tasks, in order, that fit one after another from the start of the first free stretch."""

    if not free:
        return []
    room = _span(free[0])
    chosen: list[Work] = []
    for task in admin:
        if not task.waits_on and not task._order_after and task.minutes <= room:
            chosen.append(task)
            room -= task.minutes
    return chosen


def window_times(day: date, window: tuple[str, str], changes: DayChanges = DayChanges()) -> tuple[datetime, datetime]:
    """The day's working window as times, ending earlier when Nic said so ("I'm done at 4")."""

    return _window(day, window, changes)


def _window(day: date, window: tuple[str, str], changes: DayChanges) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.fromisoformat(window[0]), tzinfo=SINGAPORE)
    end = datetime.combine(day, time.fromisoformat(window[1]), tzinfo=SINGAPORE)
    if changes.ends_at is not None:
        end = min(end, datetime.combine(day, changes.ends_at, tzinfo=SINGAPORE))
    return start, end


def _errand_trip(errands: Sequence[Work], free: Sequence[Interval]) -> tuple[list[Work], Interval | None]:
    """One trip for the errands, as late as it fits: the longest run of them, in order, that fits a
    stretch, ending by the earliest errand deadline where it can, else at the stretch's end."""

    for count in range(len(errands), 0, -1):
        chosen = list(errands[:count])
        total = timedelta(minutes=sum(task.minutes for task in chosen))
        deadlines = [task.due_at for task in chosen if task.due_at is not None]
        for low, high in reversed(free):
            if high - low < total:
                continue
            end = min([high, *deadlines]) if deadlines and min(deadlines) - total >= low else high
            return chosen, (end - total, end)
    return [], None


def draw_day(
    day: date,
    window: tuple[str, str] | None,
    work_scope: Scope,
    busy: Sequence[Busy],
    now: datetime,
    rules: Rules = Rules(),
    changes: DayChanges = DayChanges(),
    kept: Sequence[Slot] = (),
    kept_blocks: Sequence[DayBlock] = (),
    credited: Mapping[str, int] | None = None,
) -> Plan:
    """Step I §2 and §3: draw the day's blocks and place today's work in them.

    ``kept`` and ``kept_blocks`` are what's already past or in progress when Nic changes his day
    (a `day_change`), and his pinned slots: they stay as they are and their time is taken. With
    ``credited`` (minutes each task's kept slots already cover), a task gets the rest of its length
    placed, if any; without it, a task with a kept slot isn't placed again."""

    if window is None:
        off = tuple(Unplaced(task.key, task.title, DAY_OFF, task.kind, task.minutes) for task in work_scope.work)
        return Plan(day=day, unplaced=off + work_scope.unplaced)
    start, end = _window(day, window, changes)
    taken = [(slot.start, slot.end) for slot in kept] + [(block.start, block.end) for block in kept_blocks]
    free = free_time(start, end, busy, now, rules, taken)
    if credited is None:
        done_keys = {slot.key for slot in kept}
        work = [task for task in work_scope.work if task.key not in done_keys]
        pool = [task for task in work_scope.deep_pool if task.key not in done_keys]
    else:
        def rest(tasks: Iterable[Work]) -> list[Work]:
            return [
                replace(task, minutes=task.minutes - credited.get(task.key, 0))
                for task in tasks if task.minutes - credited.get(task.key, 0) > 0
            ]

        work, pool = rest(work_scope.work), rest(work_scope.deep_pool)
    by_kind = {kind: _order(task for task in work if task.kind == kind) for kind in _KINDS}

    target = _deep_target(work, pool, rules, changes)
    block_min = min(rules.deep_focus_block_min_minutes, target) if target else 0

    def deep_after(opener: Sequence[Work]) -> tuple[list[Interval], list[Interval]]:
        rooms = list(free)
        if opener:
            _take(rooms, rooms[0][0], rooms[0][0] + timedelta(minutes=sum(task.minutes for task in opener)))
        blocks = _deep_blocks(rooms, target, block_min, rules.deep_focus_blocks_max) if target else []
        return rooms, blocks

    # Admin first, but never at the cost of the deep focus the day could otherwise have.
    opener = _opener(by_kind[ADMIN], free)
    best = sum(_span(block) for block in deep_after(())[1])
    while opener and sum(_span(block) for block in deep_after(opener)[1]) < best:
        opener = opener[:-1]
    rooms, deep_rooms = deep_after(opener)

    placed_end: dict[str, datetime] = {slot.key: slot.end for slot in kept}
    slots: list[Slot] = []
    unplaced: list[Unplaced] = list(work_scope.unplaced)
    if opener:
        opener_rooms = [(free[0][0], free[0][0] + timedelta(minutes=sum(task.minutes for task in opener)))]
        opened, _ = _pack(opener, opener_rooms, rules, placed_end, splittable=False)
        slots += opened
    for block in deep_rooms:
        _take(rooms, *block)
    deep_space = list(deep_rooms)
    deep_slots, deep_unplaced = _pack(by_kind[DEEP_FOCUS], deep_space, rules, placed_end, splittable=True)
    fill_slots, _ = _pack(pool, deep_space, rules, placed_end, splittable=True, report=False)
    slots += deep_slots + fill_slots
    unplaced += deep_unplaced
    # Deep-focus time nothing filled isn't drawn as a block: time left over stays free.
    rooms[:] = _join([*rooms, *deep_space])

    trip_work, trip = _errand_trip(by_kind[ERRANDS], rooms)
    trip_rooms: list[Interval] = []
    if trip is not None:
        _take(rooms, *trip)
        trip_rooms = [trip]
        trip_slots, trip_unplaced = _pack(trip_work, trip_rooms, rules, placed_end, splittable=False)
        slots += trip_slots
        unplaced += trip_unplaced
    unplaced += [Unplaced(task.key, task.title, DOESNT_FIT, task.kind, task.minutes) for task in by_kind[ERRANDS] if task not in trip_work]

    opened_keys = {task.key for task in opener}
    rest, rest_unplaced = _pack(
        [task for task in by_kind[ADMIN] if task.key not in opened_keys], rooms, rules, placed_end, splittable=False,
    )
    slots += rest
    unplaced += rest_unplaced
    by_key = {task.key: task for task in (*work, *pool)}
    slots += _retry_waiting(unplaced, by_key, lambda kind: trip_rooms if kind == ERRANDS else rooms, rules, placed_end)

    # Blocks are what the slots fill: time a block was sized for but nothing filled stays free.
    drawn = [*kept_blocks, *(block for kind in _KINDS for block in _merge(slots, kind))]
    blocks = sorted((block for kind in _KINDS for block in _merge_blocks(drawn, kind)), key=lambda block: block.start)
    slots = [*kept, *_after_kept(slots, kept)]
    fill_keys = tuple(sorted({slot.key for slot in fill_slots}))
    return Plan(
        day=day, blocks=tuple(blocks), slots=tuple(sorted(slots, key=lambda slot: (slot.start, slot.key))),
        unplaced=tuple(sorted(unplaced, key=lambda u: (u.reason, u.key))), fill_keys=fill_keys,
    )


# --- reflow --------------------------------------------------------------------------------------


def replan(
    plan: Plan,
    work_scope: Scope,
    busy: Sequence[Busy],
    now: datetime,
    rules: Rules = Rules(),
    move_again: Mapping[str, datetime] | None = None,
    window: tuple[datetime, datetime] | None = None,
    pinned: Mapping[tuple[str, int], tuple[datetime, datetime]] | None = None,
) -> Plan:
    """Step I §3's reflow: the blocks stay; past and in-progress slots stay; a future slot that
    still fits keeps its place; everything else in today's work is placed again in what's left.

    A task whose last slot has passed while it's still open is being asked about ("Did it
    happen?"), so it isn't placed again until Nic says move: ``move_again`` maps its key to when
    he said it. Placed again, it gets its full length; the passed slot stays as history.

    With ``window`` (the day's working window, after Nic's changes), what doesn't fit its blocks
    grows one (`_grow`); without it, it's reported as not fitting.

    ``pinned`` holds slots Nic moved by hand on his calendar, by (key, part), at the times he left
    them (slice 7, the daily repair): his time wins. A pinned slot still to end keeps its place
    whatever its block or length, and everything else fits around it."""

    buffer = timedelta(minutes=rules.buffer_after_meeting_minutes)
    cutoff = _earliest(now)
    moves = dict(move_again or {})
    pins = {spot: times for spot, times in (pinned or {}).items() if times[1] > now}
    if pins:
        plan = replace(plan, slots=tuple(
            replace(slot, start=pins[(slot.key, slot.part)][0], end=pins[(slot.key, slot.part)][1])
            if (slot.key, slot.part) in pins else slot
            for slot in plan.slots
        ))
    pinned_keys = {key for key, _part in pins}
    pinned_times = tuple(pins.values())
    work = {task.key: task for task in work_scope.work}
    for task in work_scope.deep_pool:
        if task.key in plan.fill_keys:
            work.setdefault(task.key, task)

    def clashes(slot: Slot) -> bool:
        return any(slot.start < event.end + buffer and event.start < slot.end for event in busy)

    def in_block(slot: Slot) -> bool:
        return any(block.kind == slot.kind and block.start <= slot.start and slot.end <= block.end for block in plan.blocks)

    fixed = [slot for slot in plan.slots if slot.start < now]
    last_end: dict[str, datetime] = {}
    for slot in plan.slots:
        last_end[slot.key] = max(slot.end, last_end.get(slot.key, slot.end))
    moving = {key for key, said in moves.items() if key in last_end and last_end[key] <= now and said >= last_end[key]}
    asked_about = {key for key, end in last_end.items() if end <= now and key in work and key not in moving}
    by_key: dict[str, list[Slot]] = {}
    for slot in plan.slots:
        if slot.start >= now:
            by_key.setdefault(slot.key, []).append(slot)
    # Changed sibling order makes an otherwise fitting future slot invalid.
    # Release both sides of an inversion so they can swap inside the same blocks.
    # Past slots and Nic's pins retain their existing priority below.
    out_of_order: set[str] = set()
    for key, future in by_key.items():
        task = work.get(key)
        if task is None:
            continue
        first_start = min(slot.start for slot in future)
        for before in task._order_after:
            if before in last_end and last_end[before] > first_start:
                out_of_order.update((key, before))
    kept: list[Slot] = list(fixed)
    for key, future in sorted(by_key.items()):
        task = work.get(key)
        if task is not None and key in pinned_keys and key not in asked_about:
            kept += future  # Nic put it there himself
            continue
        still_fits = (
            task is not None
            and key not in asked_about
            and key not in out_of_order
            and task.minutes == sum(slot.minutes for slot in plan.slots if slot.key == key)
            and all(in_block(slot) and not clashes(slot) for slot in future)
            # A slot Nic moved by hand takes its time: whatever it lands on moves.
            and not any(slot.start < end and start < slot.end for slot in future for start, end in pinned_times)
        )
        if still_fits:
            kept += future

    # A retained successor no longer fits the schedule when an earlier part
    # needs to move or has just been added. Propagate through future slots only.
    fixed_minutes: dict[str, int] = {}
    for slot in fixed:
        if slot.key not in moving:
            fixed_minutes[slot.key] = fixed_minutes.get(slot.key, 0) + slot.minutes
    stable = {slot.key for slot in kept if slot.start >= now} | asked_about | pinned_keys
    stable.update(key for key, minutes in fixed_minutes.items() if key in work and minutes >= work[key].minutes)
    # An unchanged omission is not a moved predecessor. Reserve its successors
    # until packing proves it can fit, rather than releasing them on every reflow.
    unchanged_nonfitting = {
        entry.key for entry in plan.unplaced if entry.reason == DOESNT_FIT and entry.key in work
        and entry.key not in last_end and entry.minutes == work[entry.key].minutes
        and entry.kind == work[entry.key].kind
    }
    stable.update(unchanged_nonfitting)
    while True:
        released = {
            slot.key for slot in kept if slot.start >= now and slot.key not in pinned_keys
            and any(before in work and before not in stable for before in work[slot.key]._order_after)
        }
        if not released:
            break
        kept = [slot for slot in kept if slot.start < now or slot.key not in released]
        stable.difference_update(released)

    placed_keys = {slot.key for slot in kept if slot.end > now}
    done_minutes: dict[str, int] = {}
    for slot in fixed:
        if slot.key not in moving:
            done_minutes[slot.key] = done_minutes.get(slot.key, 0) + slot.minutes
    rooms_by_kind: dict[str, list[Interval]] = {kind: [] for kind in _KINDS}
    for block in plan.blocks:
        rooms_by_kind[block.kind].append((block.start, block.end))
    for kind in _KINDS:
        rooms = [(max(low, cutoff), high) for low, high in rooms_by_kind[kind] if high > cutoff]
        for event in busy:
            rooms = _subtract(rooms, event.start, event.end + buffer)
        for slot in kept:
            rooms = _subtract(rooms, slot.start, slot.end)
        rooms_by_kind[kind] = sorted(room for room in rooms if room[0] < room[1])

    placed_end = {slot.key: slot.end for slot in kept if slot.key not in moving}
    slots = list(kept)
    unplaced: list[Unplaced] = list(work_scope.unplaced)
    blocks = list(plan.blocks)
    chains = {key for task in work.values() if task._order_after for key in (task.key, *task._order_after)}
    # Across kinds too: a chain's predecessor must get its chance to fit or grow
    # before a later slot takes room. Unrelated work retains its existing passes.
    ordered = _order(work.values()) if chains else [
        task for kind in _KINDS for task in _order(task for task in work.values() if task.kind == kind)
    ]
    packing_work = dict(work)
    omitted_order: set[str] = set()
    for task in ordered:
        if task.key in placed_keys or task.key in asked_about:
            continue
        remaining = task.minutes - done_minutes.get(task.key, 0)
        if remaining <= 0:
            continue
        task = replace(task, minutes=remaining,
                       _order_after=tuple(before for before in task._order_after if before not in omitted_order))
        packing_work[task.key] = task
        # Try a previously non-fitting part with its successor reservations free.
        # Commit that release only if it fits, so other picks cannot steal a
        # valid retained slot merely because an earlier part remains too long.
        affected = {task.key} if task.key in unchanged_nonfitting else set()
        if affected:
            for following in ordered:
                if affected.intersection(following._order_after):
                    affected.add(following.key)
        reservations = [slot for slot in kept if slot.start >= now and slot.key != task.key
                        and slot.key in affected and slot.key not in pinned_keys]
        released_keys = {slot.key for slot in reservations}
        trial_slots = [slot for slot in slots if slot not in reservations]
        trial_end = {key: end for key, end in placed_end.items() if key not in released_keys}
        trial_end.update({slot.key: slot.end for slot in trial_slots if slot.key in released_keys})
        trial_rooms = _join([*rooms_by_kind[task.kind],
                             *((slot.start, slot.end) for slot in reservations if slot.kind == task.kind)])
        placed, missing = _pack(
            [task], trial_rooms, rules, trial_end, splittable=task.kind == DEEP_FOCUS,
            report=True,
        )
        if window is not None and task.key in chains and any(entry.reason == DOESNT_FIT for entry in missing):
            grown = _grow(missing, {task.key: task}, blocks, [*trial_slots, *placed], busy, now, window, rules, trial_end)
            if grown:
                placed += grown
                blocks += [DayBlock(slot.kind, slot.start, slot.end) for slot in grown]
                missing = []
        if placed:
            kept = [slot for slot in kept if slot not in reservations]
            placed_keys.difference_update(released_keys)
            for kind in _KINDS:
                rooms_by_kind[kind] = _join([*rooms_by_kind[kind],
                                            *((slot.start, slot.end) for slot in reservations if slot.kind == kind)])
            rooms_by_kind[task.kind] = trial_rooms
            placed_end = trial_end
            slots = [*trial_slots, *placed]
        if any(entry.reason == DOESNT_FIT for entry in missing):
            omitted_order.add(task.key)
        unplaced += [entry for entry in missing if not work[entry.key].fill]
    slots += _retry_waiting(unplaced, packing_work, lambda kind: rooms_by_kind[kind], rules, placed_end)
    slots = [*kept, *_after_kept(slots[len(kept):], kept)]
    if window is not None:
        grown = _after_kept(_grow(unplaced, packing_work, blocks, slots, busy, now, window, rules, placed_end), slots)
        slots += grown
        grown_keys = {slot.key for slot in grown}
        unplaced = [entry for entry in unplaced if entry.key not in grown_keys]
        for slot in grown:
            blocks.append(DayBlock(slot.kind, slot.start, slot.end))
        blocks = [block for kind in _KINDS for block in _merge_blocks(blocks, kind)]
    return replace(
        plan,
        blocks=tuple(sorted(blocks, key=lambda block: (block.start, block.kind))),
        slots=tuple(sorted(slots, key=lambda slot: (slot.start, slot.key))),
        unplaced=tuple(sorted(unplaced, key=lambda u: (u.reason, u.key))),
    )


def _merge_blocks(blocks: Iterable[DayBlock], kind: str) -> list[DayBlock]:
    merged: list[DayBlock] = []
    for block in sorted((block for block in blocks if block.kind == kind), key=lambda block: block.start):
        if merged and block.start <= merged[-1].end:
            merged[-1] = DayBlock(kind, merged[-1].start, max(merged[-1].end, block.end))
        else:
            merged.append(block)
    return merged


def _grow(
    unplaced: Sequence[Unplaced], work: Mapping[str, Work], blocks: Sequence[DayBlock], slots: Sequence[Slot],
    busy: Sequence[Busy], now: datetime, window: tuple[datetime, datetime], rules: Rules,
    placed_end: dict[str, datetime],
) -> list[Slot]:
    """Place what didn't fit its blocks in free time in the window, outside every block: first in
    free time touching a block of its kind (so that block grows), else anywhere free. Deep focus
    still stops at its daily cap; admin and errands still never split."""

    taken = [(block.start, block.end) for block in blocks] + [(slot.start, slot.end) for slot in slots]
    free = free_time(window[0], window[1], busy, now, rules, taken)
    deep_minutes = sum(_span((block.start, block.end)) for block in blocks if block.kind == DEEP_FOCUS)
    missing = {entry.key for entry in unplaced if entry.reason == DOESNT_FIT}
    grown: list[Slot] = []
    for kind in _KINDS:
        for task in _order(work[key] for key in missing if work[key].kind == kind):
            if kind == DEEP_FOCUS and deep_minutes + task.minutes > rules.deep_focus_max_minutes:
                continue
            same_kind = [block for block in (*blocks, *(DayBlock(s.kind, s.start, s.end) for s in grown)) if block.kind == kind]
            touching = [room for room in free if any(room[0] == block.end or room[1] == block.start for block in same_kind)]
            placed: list[Slot] = []
            for rooms in (touching, list(free)):
                placed, _ = _pack([task], rooms, rules, placed_end, splittable=kind == DEEP_FOCUS, report=False)
                if placed:
                    break
            for slot in placed:
                _take(free, slot.start, slot.end)
            if kind == DEEP_FOCUS:
                deep_minutes += sum(slot.minutes for slot in placed)
            grown += placed
    return grown


# --- words ---------------------------------------------------------------------------------------


def _clock(moment: datetime) -> str:
    return _clock_word(moment.astimezone(SINGAPORE).strftime("%H:%M"))


def _length_words(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h" if not rest else f"{hours} h {rest} min"


def plan_summary(plan: Plan) -> tuple[str, ...]:
    """Step I §5a's lines: today's blocks with how many tasks each holds, then what doesn't fit,
    what's late, what waits on another task and what needs a length."""

    if not plan.blocks:
        lines = ["No blocks today."]
    else:
        parts = []
        for block in plan.blocks:
            inside = sum(1 for slot in plan.slots if slot.kind == block.kind and block.start <= slot.start < block.end)
            count = f" ({inside} task{'s' if inside != 1 else ''})" if inside else ""
            parts.append(f"{BLOCK_WORDS[block.kind].replace('-', ' ')} {_clock(block.start)}–{_clock(block.end)}{count}")
        lines = ["Today: " + " · ".join(parts)]
    late = [slot for slot in plan.slots if slot.late and slot.part == slot.parts]
    for slot in late:
        lines.append(f"Late: \"{slot.task_title}\" only fits after its deadline, ending {_clock(slot.end)}.")
    by_reason: dict[str, list[Unplaced]] = {}
    for entry in plan.unplaced:
        by_reason.setdefault(entry.reason, []).append(entry)
    if by_reason.get(DOESNT_FIT):
        entries = by_reason[DOESNT_FIT]
        need = sum(entry.minutes or 0 for entry in entries)
        names = ", ".join(f"\"{entry.title}\"" for entry in entries)
        lines.append(f"Doesn't fit today ({_length_words(need)}): {names}. Drop, move to tomorrow or swap one?")
    if by_reason.get(WAITS_ON):
        names = ", ".join(f"\"{entry.title}\"" for entry in by_reason[WAITS_ON])
        lines.append(f"Waiting on another task first: {names}.")
    if by_reason.get(LATER_DAY):
        names = ", ".join(f'"{entry.title}"' for entry in by_reason[LATER_DAY])
        lines.append(f"Planned for a later day: {names}.")
    if by_reason.get(NO_LENGTH):
        names = ", ".join(f"\"{entry.title}\"" for entry in by_reason[NO_LENGTH])
        lines.append(f"Needs a length: {names}. How long will each take?")
    if by_reason.get(DAY_OFF):
        lines.append("Today is outside your working window, so nothing was planned.")
    return tuple(lines)


def window_for(day: date, working_window: Mapping[str, tuple[str, str] | None]) -> tuple[str, str] | None:
    """That day's hours from `language.working_window`, or None on a day off."""

    return working_window.get(("mon", "tue", "wed", "thu", "fri", "sat", "sun")[day.weekday()])


# --- storing a plan (build plan slice 4: the runner keeps the day's plan in the why-log) --------


def plan_to_mapping(plan: Plan, *, with_text: bool = True) -> dict[str, object]:
    """A plan as plain JSON-ready data; `plan_from_mapping` reads it back exactly. Without
    ``with_text``, only times, kinds and record IDs: no task titles or first steps (what the
    why-log keeps, build plan slice 4), which `with_titles` puts back from Brain Home."""

    if not with_text:
        plan = with_titles(plan, {})
    return {
        "day": plan.day.isoformat(),
        "blocks": [{"kind": b.kind, "start": b.start.isoformat(), "end": b.end.isoformat()} for b in plan.blocks],
        "slots": [
            {
                "key": x.key, "kind": x.kind, "start": x.start.isoformat(), "end": x.end.isoformat(),
                "task_title": x.task_title, "first_step": x.first_step, "belongs_to": x.belongs_to, "part": x.part, "parts": x.parts, "late": x.late,
            }
            for x in plan.slots
        ],
        "unplaced": [
            {"key": u.key, "title": u.title, "reason": u.reason, "kind": u.kind, "minutes": u.minutes} for u in plan.unplaced
        ],
        "fill_keys": list(plan.fill_keys),
    }


def with_titles(plan: Plan, titles: Mapping[str, tuple[str, str | None] | tuple[str, str | None, str | None]]) -> Plan:
    """Restore current title, action and parent from key -> (title, action, parent).

    Existing two-field callers remain accepted with no parent; missing tasks get no text.
    """

    return replace(
        plan,
        slots=tuple(
            replace(slot, task_title=titles.get(slot.key, ("", None))[0], first_step=titles.get(slot.key, ("", None))[1],
                    belongs_to=titles[slot.key][2] if slot.key in titles and len(titles[slot.key]) == 3 else None)
            for slot in plan.slots
        ),
        unplaced=tuple(replace(entry, title=titles.get(entry.key, ("", None))[0]) for entry in plan.unplaced),
    )


def plan_from_mapping(data: Mapping[str, object]) -> Plan:
    def when(value: object) -> datetime:
        return datetime.fromisoformat(str(value))

    return Plan(
        day=date.fromisoformat(str(data["day"])),
        blocks=tuple(DayBlock(str(b["kind"]), when(b["start"]), when(b["end"])) for b in data["blocks"]),  # type: ignore[index, union-attr]
        slots=tuple(
            Slot(
                str(x["key"]), str(x["kind"]), when(x["start"]), when(x["end"]), str(x["task_title"]),
                x.get("first_step"), int(x["part"]), int(x["parts"]), bool(x["late"]), x.get("belongs_to"),
            )
            for x in data["slots"]  # type: ignore[union-attr]
        ),
        unplaced=tuple(
            Unplaced(str(u["key"]), str(u["title"]), str(u["reason"]), u.get("kind"), u.get("minutes"))
            for u in data["unplaced"]  # type: ignore[union-attr]
        ),
        fill_keys=tuple(str(key) for key in data["fill_keys"]),  # type: ignore[union-attr]
    )
