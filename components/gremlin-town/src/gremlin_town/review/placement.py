"""The placement runner (step I; build plan slice 4): Brain Home and the calendar in, the day's
blocks and task slots out.

`run_placement` gathers one set of inputs, asks `brain_core.placement` for the day's plan and,
only while `[placement] write = true`, makes the calendar match it. Off (`[placement] enabled =
false`, the default) it reads nothing and writes nothing.

- **Not before the day is settled.** Blocks are drawn once Nic has settled his day in the 6am
  review: only the relaying agent's `day_settled` signal after Nic's yes. Picks never settle
  the day by elapsed time. A day off in his working window draws
  nothing.
- **Drawn once, then only reflowed.** The first run of the day draws the blocks; later runs keep
  them and move only future task slots (`placement.replan`), growing a block into free time in
  the window when a task needs room (Nic, 29 Sep 2026). One of his one-line changes (a
  `day_change` signal: "no deep focus today", "make it 3 hours", "I'm done at 4") redraws the
  day from now on; "move it" hands a passed slot back.
- **Quiet when nothing changed.** Each run fingerprints its inputs; the same fingerprint as the
  last run means no calendar read of the engine's entries and no write.
- **Only its own entries, only in the future.** Writes go through `CalendarWriter`'s placed-entry
  methods, which refuse anything past, in progress, with guests, or not the engine's. A refusal
  leaves that entry alone; a Google failure keeps the plan, records the error, and the next
  run tries again. If the calendar can't be read, nothing is planned or written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from time import monotonic
from typing import Any, Callable, Iterator, Mapping, Sequence

from brain_core import placement
from brain_core.language import working_window
from brain_core.placement import Busy, DayChanges, Plan, Rules
from brain_core.review import HistoryRow
from brain_core.timewords import SINGAPORE, today_in_singapore

from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.settings import PlacementSettings
from gremlin_town.review.why_log import WhyLog

logger = logging.getLogger(__name__)

DAY_SETTLED = "day_settled"
DAY_CHANGE = "day_change"
SIGNAL_KINDS = frozenset({DAY_SETTLED, DAY_CHANGE})
_CHANGE_KEYS = frozenset({"deep_focus_minutes", "ends_at", "move", "words"})
BLOCK_DESCRIPTION = "Drawn by Ciel from the day you settled in the 6am review."
_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Step I section 8 (Nic, 29 Sep 2026): "make it N hours" sizes deep focus within the day's rules.
DEEP_FOCUS_CHANGE_RANGE = (90, 360)
# The daily repair's own row in the why-log's runs (slice 7).
REPAIR_RUN = "placement_repair"
# Brain Home can't be read and won't be until it is tidied up: a scan past its size limit, or a tree
# too big for GitHub to list (the storage-limit plan, layer 1). The run plans nothing, writes nothing,
# and says so in plain words; the next poll finds the same, so no one is told a plan is coming.
CANNOT_READ_BRAIN_HOME = "cannot_read_brain_home"
# A run holds the lock this long at most; a crashed holder's lock is taken over after it.
LOCK_STALE_SECONDS = 300


class DayChangeError(ValueError):
    """A `day_change` signal's detail is malformed."""


class _Timing:
    """Where one run's time went, added to its result as ``seconds`` once it has read anything.

    Nothing logged it before 30 Sep 2026, when settling the day took 19.2 s of Diablo's 20 s
    (docs/product/2026-09-30-diablo-speed-and-token-fixes-plan.md, finding 6). The moments poll
    prints every run's result to its journal; no titles or other content are included."""

    def __init__(self) -> None:
        self._started = monotonic()
        self._parts: dict[str, float] = {}
        self.item_files: int | None = None

    @contextmanager
    def part(self, name: str) -> Iterator[None]:
        started = monotonic()
        try:
            yield
        finally:
            self._parts[name] = self._parts.get(name, 0.0) + monotonic() - started

    def summary(self) -> dict[str, Any] | None:
        if not self._parts:
            return None
        found: dict[str, Any] = {name: round(seconds, 2) for name, seconds in self._parts.items()}
        found["total"] = round(monotonic() - self._started, 2)
        if self.item_files is not None:
            found["item_files"] = self.item_files
        return found


def parse_day_change(detail: str | None) -> dict[str, Any]:
    """A `day_change` detail: a JSON object with any of ``deep_focus_minutes`` (0 for none, or 90
    to 360), ``ends_at`` ("HH:MM", 24-hour), ``move`` (a record ID Nic said to move) and ``words``
    (his)."""

    try:
        value = json.loads(detail or "")
    except json.JSONDecodeError as exc:
        raise DayChangeError("day_change detail must be a JSON object") from exc
    if not isinstance(value, dict) or not value or set(value) - _CHANGE_KEYS:
        raise DayChangeError(f"day_change detail must be an object with some of {sorted(_CHANGE_KEYS)}")
    if not set(value) - {"words"}:
        raise DayChangeError("day_change needs deep_focus_minutes, ends_at or move, not only words")
    minutes = value.get("deep_focus_minutes")
    low, high = DEEP_FOCUS_CHANGE_RANGE
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or not (minutes == 0 or low <= minutes <= high)):
        raise DayChangeError(f"deep_focus_minutes must be 0, or a whole number from {low} to {high}")
    if value.get("ends_at") is not None and (not isinstance(value["ends_at"], str) or not _CLOCK.fullmatch(value["ends_at"])):
        raise DayChangeError("ends_at must be HH:MM, 24-hour")
    for key in ("move", "words"):
        if value.get(key) is not None and (not isinstance(value[key], str) or not value[key]):
            raise DayChangeError(f"{key} must be a non-empty string")
    return value


@dataclass
class PlacementContext:
    """What `run_placement` needs: `runtime.py` builds one from `[placement]` and the review's
    sources; tests build one from fakes. ``writer`` is the calendar writer, used only while
    ``settings.write`` is on. ``calendar_events`` is a strict reader (a partial read raises), used
    in place of the review's own, which returns what it could."""

    settings: PlacementSettings
    why_log: WhyLog
    sources: ReviewSources
    writer: Any | None = None
    history: Callable[[], tuple[HistoryRow, ...]] | None = field(default=None)
    calendar_events: Callable[[datetime, datetime], tuple[Any, ...]] | None = None


def _rules(settings: PlacementSettings) -> Rules:
    return Rules(
        buffer_after_meeting_minutes=settings.buffer_after_meeting_minutes,
        deep_focus_min_minutes=settings.deep_focus_min_minutes,
        deep_focus_block_min_minutes=settings.deep_focus_block_min_minutes,
        deep_focus_max_minutes=settings.deep_focus_max_minutes,
        split_min_minutes=settings.split_min_minutes,
    )


def _picks(history: Sequence[HistoryRow], today) -> tuple[frozenset[str], datetime | None]:
    ids: set[str] = set()
    last: datetime | None = None
    for row in history:
        if row.response != "picked_today":
            continue
        moment = row.responded_at or row.surfaced_at
        if today_in_singapore(moment) == today:
            ids.add(row.item_ref)
            last = moment if last is None or moment > last else last
    return frozenset(ids), last


def _day_changes(signals: Sequence[Any]) -> tuple[DayChanges, dict[str, datetime], int, list[int]]:
    """Nic's one-line changes today, the newest winning; the moves he asked for, by record ID; how
    many changes redraw the day (a move doesn't); and the IDs of stored signals that don't parse,
    which are skipped and reported, never allowed to stop the day's runs."""

    deep: int | None = None
    ends: time | None = None
    moves: dict[str, datetime] = {}
    redraws = 0
    skipped: list[int] = []
    for signal in signals:
        try:
            change = parse_day_change(signal.detail)
        except DayChangeError:
            skipped.append(signal.id)
            continue
        if change.get("deep_focus_minutes") is not None:
            deep = change["deep_focus_minutes"]
        if change.get("ends_at") is not None:
            ends = time.fromisoformat(change["ends_at"])
        if change.get("deep_focus_minutes") is not None or change.get("ends_at") is not None:
            redraws += 1
        if change.get("move"):
            moves[change["move"]] = signal.at
    return DayChanges(deep_focus_minutes=deep, ends_at=ends), moves, redraws, skipped


def _busy(events: Sequence[Any]) -> tuple[Busy, ...]:
    """Meetings and timed-task entries; never the engine's own, never free or all-day events."""

    return tuple(sorted(
        (Busy(event.start, event.end) for event in events
         if not event.all_day and event.busy and event.exosuit_role is None),
        key=lambda busy: (busy.start, busy.end),
    ))


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def _desired(plan: Plan, keep_keys: Mapping[tuple[str, int], str] | None = None) -> dict[tuple[str, str], tuple[str, str, datetime, datetime]]:
    """Every entry the plan wants on the calendar, by (role, key): its title, description and time.
    A slot Nic moved by hand keeps its entry's key (``keep_keys``), so it stays the entry he moved."""

    keep = keep_keys or {}
    wanted: dict[tuple[str, str], tuple[str, str, datetime, datetime]] = {}
    for block in plan.blocks:
        wanted[("block", f"{block.kind}:{block.start.astimezone(SINGAPORE):%H%M}")] = (
            block.title, BLOCK_DESCRIPTION, block.start, block.end,
        )
    for slot in plan.slots:
        why = "Late: this only fits after its deadline.\n" if slot.late else ""
        # The start is part of the key: a task moved after its slot passed gets a second slot, and
        # a slot that moves is a new entry in place of the old one.
        entry_key = keep.get((slot.key, slot.part)) or f"{slot.key}:{slot.part}@{slot.start.astimezone(SINGAPORE):%H%M}"
        wanted[("slot", entry_key)] = (
            slot.title, f"{slot.task_title}\n{why}Placed by Ciel (step I).", slot.start, slot.end,
        )
    return wanted


def _slot_spot(entry: Any) -> tuple[str, int] | None:
    head, _, _clock = entry.key.partition("@")
    key, _, part = head.rpartition(":")
    return (key, int(part)) if key and part.isdigit() else None


def _away(entry: Any) -> bool:
    """An engine entry Nic dragged to another day (placement-02): its start isn't on its own day."""

    return entry.slot is not None and not entry.slot.all_day and entry.slot.start.astimezone(SINGAPORE).date() != entry.day


Known = dict[tuple[str, int], set[tuple[datetime, datetime]]]


def _hand_moves(entries: Sequence[Any], known: Known, now: datetime) -> tuple[dict[tuple[str, int], tuple[datetime, datetime]], dict[tuple[str, int], str]]:
    """Slots Nic moved or resized by hand (slice 7): an engine slot entry still to end whose time
    is none the engine has written or tried to write for it (``known``, which keeps the times of a
    run whose writes failed, so a slot left unmoved by a failure is never taken for his). His time
    wins: returned as pins by (key, part), with the entry's key so the plan keeps that entry.
    Blocks aren't his to move: they're put back."""

    pins: dict[tuple[str, int], tuple[datetime, datetime]] = {}
    keep: dict[tuple[str, int], str] = {}
    for entry in entries:
        if entry.role != "slot" or entry.slot is None or entry.slot.all_day or entry.slot.end <= now or _away(entry):
            continue
        spot = _slot_spot(entry)
        if spot is None:
            continue
        keep[spot] = entry.key
        # At a time the engine never wrote for it (or for a slot it doesn't know, such as one
        # dragged back from another day), the entry is where Nic put it.
        if (entry.slot.start, entry.slot.end) not in known.get(spot, set()):
            pins[spot] = (entry.slot.start, entry.slot.end)
    return pins, keep


def _stored(last: Mapping[str, Any] | None) -> tuple[dict, dict, Known, set[str]]:
    """What the last run kept beside its plan (placement-02): Nic's pins (a slot he moved stays
    pinned for the day), their entries' keys, the times the engine may have written, and the tasks
    he dragged to another day."""

    data = (last or {}).get("plan") or {}

    def when(value: str) -> datetime:
        return datetime.fromisoformat(value)

    pins = {(p["key"], int(p["part"])): (when(p["start"]), when(p["end"])) for p in data.get("pins", [])}
    keep = {(p["key"], int(p["part"])): p["entry_key"] for p in data.get("pins", []) if p.get("entry_key")}
    known: Known = {}
    for key, part, start, end in data.get("known", [(s["key"], s["part"], s["start"], s["end"]) for s in data.get("slots", [])]):
        known.setdefault((key, int(part)), set()).add((when(start), when(end)))
    return pins, keep, known, set(data.get("away", []))


def apply_plan(
    writer: Any, plan: Plan, now: datetime, *, busy: bool, dry_run: bool = False,
    entries: Sequence[Any] | None = None, keep_keys: Mapping[tuple[str, int], str] | None = None,
) -> dict[str, Any]:
    """Make the day's engine entries match the plan: remove what's gone, move what moved, add
    what's new, all in the future only. With ``dry_run``, only reads and reports. ``entries`` is
    the day's engine entries when the caller has read them already."""

    from gremlin_town.sources.google.calendar_writer import EventSlot

    existing: dict[tuple[str, str], Any] = {}
    duplicates: list[Any] = []
    for entry in (entries if entries is not None else writer.list_placed(plan.day)):
        if entry.slot is None or entry.slot.all_day:
            continue  # moved into the all-day row, or unreadable: his, never compared or changed
        key = (entry.role, entry.key)
        if key in existing:
            duplicates.append(entry)
        else:
            existing[key] = entry
    wanted = _desired(plan, keep_keys)
    changes: dict[str, list[str]] = {"added": [], "moved": [], "removed": [], "left_alone": []}

    def attempt(kind: str, label: str, action: Callable[[], Any]) -> None:
        if dry_run:
            changes[kind].append(label)
            return
        try:
            action()
        except ValueError:  # the writer's guards: past, in progress, guests, not the engine's
            changes["left_alone"].append(label)
            return
        changes[kind].append(label)

    for entry in duplicates:
        attempt("removed", f"{entry.role} {entry.key}", lambda entry=entry: writer.delete_placed(entry, now=now))
    for key, entry in sorted(existing.items()):
        if key not in wanted:
            attempt("removed", f"{key[0]} {key[1]}", lambda entry=entry: writer.delete_placed(entry, now=now))
    for key, (title, description, start, end) in sorted(wanted.items(), key=lambda item: (item[1][2], item[0])):
        entry = existing.get(key)
        label = f"{key[0]} {key[1]} {start.astimezone(SINGAPORE):%H:%M}-{end.astimezone(SINGAPORE):%H:%M}"
        if entry is None:
            if start > now:
                attempt("added", label, lambda key=key, title=title, description=description, start=start, end=end: writer.insert_placed(
                    day=plan.day, role=key[0], key=key[1], title=title, description=description,
                    slot=EventSlot(start, end), busy=busy, now=now,
                ))
            continue
        same_time = entry.slot is not None and entry.slot.start == start and entry.slot.end == end
        if same_time and entry.title == title:
            continue
        started = entry.slot is not None and entry.slot.start <= now
        # A block under way may only change its end, to a later time than now ("I'm done at 4").
        shortened = started and entry.slot.start == start and end > now and entry.slot.end > now
        if start > now or shortened:
            attempt("moved", label, lambda entry=entry, start=start, end=end, title=title: writer.move_placed(
                entry, EventSlot(start, end), now=now, title=title,
            ))
        else:
            changes["left_alone"].append(label)
    return changes


def run_placement(context: PlacementContext, now: datetime, *, dry_run: bool = False) -> dict[str, Any]:
    """One placement run (step I §5). Returns what happened, with the plan's summary lines.

    One run at a time across processes (the hub's signal route and the moments poll share the
    why-log): a run that finds another under way skips, with outcome "busy"."""

    settings = context.settings
    if not settings.enabled:
        return {"outcome": "disabled"}
    owner = f"placement-{id(context)}-{now.isoformat()}"
    if not context.why_log.take_placement_lock(owner, now, stale_after=timedelta(seconds=LOCK_STALE_SECONDS)):
        return {"outcome": "busy"}
    timing = _Timing()
    try:
        result = _run(context, now, dry_run=dry_run, timing=timing)
    finally:
        context.why_log.release_placement_lock(owner)
    seconds = timing.summary()
    return result if seconds is None else {**result, "seconds": seconds}


def _cannot_read(exc: BaseException) -> dict[str, Any] | None:
    """`cannot_read_brain_home` with the plain sentence, when ``exc`` is a Brain Home read that
    won't pass by itself; None for anything else, which the caller raises as before."""

    from gremlin_town.review.brain_source import lasting_brain_problem

    sentence = lasting_brain_problem(exc)
    if sentence is None:
        return None
    logger.error("placement couldn't read Brain Home and planned nothing: %s", sentence)
    return {"outcome": CANNOT_READ_BRAIN_HOME, "error": sentence}


def _titles(scope: placement.Scope) -> dict[str, tuple[str, str | None, str | None]]:
    """Current reader titles, next actions and parents, through placement scope."""
    return {task.key: (task.title, task.first_step, task.belongs_to) for task in (*scope.work, *scope.deep_pool)} | {
        entry.key: (entry.title, None, None) for entry in scope.unplaced
    }


def _run(context: PlacementContext, now: datetime, *, dry_run: bool, timing: _Timing) -> dict[str, Any]:
    settings = context.settings
    why_log = context.why_log
    today = today_in_singapore(now)
    history = context.history() if context.history is not None else why_log.history()
    picks, _last_pick = _picks(history, today)
    settled = bool(why_log.signals_on(DAY_SETTLED, today))
    if not settled:
        return {"outcome": "not_settled"}
    sources = context.sources
    calendar_events = context.calendar_events or sources.calendar_events
    if sources.brain_items is None or calendar_events is None:
        return {"outcome": "not_configured"}
    try:
        with timing.part("brain_home"):
            preferences = sources.brain_preferences() if sources.brain_preferences is not None else ()
    except Exception as exc:
        unreadable = _cannot_read(exc)
        if unreadable is None:
            raise
        return unreadable
    window = placement.window_for(today, working_window(preferences))
    if window is None:
        return {"outcome": "day_off", "summary": ["Today is outside your working window, so nothing was planned."]}
    changes, moves_by_id, redraws, skipped = _day_changes(why_log.signals_on(DAY_CHANGE, today))
    day_start = datetime.combine(today, time.min, tzinfo=SINGAPORE)
    try:
        with timing.part("calendar_read"):
            events = tuple(calendar_events(day_start, day_start + timedelta(days=1)))
    except Exception as exc:  # the calendar can't be read: plan nothing, write nothing (step I §4)
        return {"outcome": "calendar_unavailable", "error": f"{type(exc).__name__}: {exc}"}
    busy = _busy(events)
    try:
        with timing.part("brain_home"):
            items = tuple(sources.brain_items())
    except Exception as exc:
        unreadable = _cannot_read(exc)
        if unreadable is None:
            raise
        return unreadable
    timing.item_files = len(items)
    scope = placement.scope(items, today, now, picks)
    keys_by_id = {record_id: task.key for task in (*scope.work, *scope.deep_pool) for record_id in task.ids}
    moves = {keys_by_id[record_id]: at for record_id, at in moves_by_id.items() if record_id in keys_by_id}
    rules = _rules(settings)
    start, end = placement.window_times(today, window, changes)

    last = why_log.latest_placement(today)
    stored_pins, stored_keep, known, away = _stored(last)
    full_scope = scope
    if away:
        scope = replace(
            scope,
            work=tuple(task for task in scope.work if task.key not in away),
            deep_pool=tuple(task for task in scope.deep_pool if task.key not in away),
        )
    titles = _titles(scope)
    previous = placement.with_titles(placement.plan_from_mapping(last["plan"]), titles) if last is not None else None
    # Which of the plan's slots have started or ended by now: a slot passing changes what may move.
    passed = tuple(
        (slot.key, slot.part, slot.start, slot.end <= now) for slot in previous.slots if slot.start < now
    ) if previous else ()
    # Points move a little every minute (due dates count to the minute), so they stay out of the
    # fingerprint: what's in the day, how long, of what kind, due when, waiting on what.
    shape = tuple(
        (task.key, task.kind, task.minutes, task.due_at, task.picked, task.waits_on, task.first_step, task.title, task.belongs_to,
         task._order_after)
        for task in (*scope.work, *scope.deep_pool)
    )
    fingerprint = _fingerprint(
        shape, scope.unplaced, busy, changes, sorted(moves.items()), window, redraws, passed,
        # Switching write on must make the next run write, not find the plan-only run unchanged.
        settings.write and context.writer is not None,
    )
    # The daily repair (slice 7; Nic, 30 Sep: "yes add repair"): once a day, from repair_hour, the
    # run goes ahead even with nothing changed, so a slot or block deleted, or a block moved, by hand
    # is put back for the rest of today. A slot Nic moved by hand is kept where he put it.
    writing = context.writer is not None and settings.write and not dry_run
    local_now = now.astimezone(SINGAPORE)
    repair_due = (
        writing and previous is not None and local_now.hour >= settings.repair_hour
        and why_log.latest_cursor(REPAIR_RUN) != today.isoformat()
    )
    if (
        last is not None and last["fingerprint"] == fingerprint and last["outcome"] in ("planned", "written")
        and not dry_run and not repair_due
    ):
        return {"outcome": "unchanged", "summary": list(placement.plan_summary(previous))}

    # One read of the engine's own entries per run that writes (or shows what it would write).
    entries: tuple[Any, ...] | None = None
    # A slot Nic moved stays pinned for the rest of the day, whatever else changes (placement-02).
    pins = {spot: times for spot, times in stored_pins.items() if times[1] > now}
    keep_keys = dict(stored_keep)
    if context.writer is not None and (settings.write or dry_run):
        from gremlin_town.sources.google.calendar_writer import CalendarWriteError

        try:
            with timing.part("calendar_read"):
                listed = tuple(context.writer.list_placed(today))
        except CalendarWriteError as exc:
            return {"outcome": "calendar_unavailable", "error": f"{exc.reason.value}: {exc}"}
        # A slot Nic dragged to another day is his from then on: Ciel never touches it again, and its
        # task leaves today's plan (placement-02).
        # Rebuilt from what's listed each run: a slot dragged back onto today is today's again.
        # A slot dragged into the all-day row is his too (placement-02).
        away = {
            spot[0] for entry in listed
            if entry.role == "slot" and (_away(entry) or entry.slot is None or entry.slot.all_day)
            for spot in [_slot_spot(entry)] if spot
        }
        scope = replace(
            full_scope,
            work=tuple(task for task in full_scope.work if task.key not in away),
            deep_pool=tuple(task for task in full_scope.deep_pool if task.key not in away),
        )
        entries = tuple(entry for entry in listed if not _away(entry) and entry.slot is not None and not entry.slot.all_day)
        if previous is not None:
            new_pins, new_keep = _hand_moves(entries, known, now)
            pins.update(new_pins)
            keep_keys.update(new_keep)
        pins = {spot: times for spot, times in pins.items() if spot[0] not in away}
    # A pin is for a task still in today's work: one done, dropped or gone is let go.
    in_day = {task.key for task in (*scope.work, *scope.deep_pool)}
    pins = {spot: times for spot, times in pins.items() if spot[0] in in_day}

    if previous is not None and pins:
        # A pin for a slot the last plan doesn't have (one dragged back from another day) joins it,
        # so the plan keeps it where Nic put it.
        present = {(slot.key, slot.part) for slot in previous.slots}
        kinds = {task.key: task.kind for task in (*scope.work, *scope.deep_pool)}
        extra = tuple(
            placement.Slot(key, kinds[key], start, end, "", part=part, parts=part)
            for (key, part), (start, end) in sorted(pins.items()) if (key, part) not in present and key in kinds
        )
        if extra:
            previous = replace(previous, slots=previous.slots + extra)

    if previous is None:
        plan = placement.draw_day(today, window, scope, busy, now, rules, changes)
    elif redraws > last["changes_seen"]:
        # What's past stays, and so does every slot Nic pinned by hand.
        kept_from = tuple(slot for slot in previous.slots if slot.start < now or (slot.key, slot.part) in pins)
        kept = tuple(
            replace(slot, start=pins[(slot.key, slot.part)][0], end=pins[(slot.key, slot.part)][1])
            if (slot.key, slot.part) in pins else slot
            for slot in kept_from
        )
        # What the kept parts cover, at their planned lengths; the rest of a task is placed again.
        credited: dict[str, int] = {}
        for slot in kept_from:
            credited[slot.key] = credited.get(slot.key, 0) + slot.minutes
        kept_blocks = tuple(
            placement.DayBlock(block.kind, block.start, max([now, *(s.end for s in kept if block.start <= s.start < block.end)]))
            for block in previous.blocks if block.start < now
        )
        plan = placement.draw_day(
            today, window, scope, busy, now, rules, changes, kept=kept, kept_blocks=kept_blocks, credited=credited,
        )
    else:
        plan = placement.replan(previous, scope, busy, now, rules, moves, window=(start, end), pinned=pins)

    plan = placement.with_titles(plan, titles)
    result: dict[str, Any] = {"summary": list(placement.plan_summary(plan))}
    # Step I §5a: a pick or something due today that had a place and has lost it comes back once,
    # in the daytime return (the poll sends it; build plan slice 6). Deep-focus fill never does.
    # Noted in the why-log, before the plan row, so a failure later loses nothing; the next daytime
    # return carries it (run_moments) and marks it sent.
    if previous is not None:
        had_place = {slot.key for slot in previous.slots if slot.end > now}
        asked = {task.key for task in scope.work}
        lost = [
            {"key": entry.key, "title": entry.title}
            for entry in plan.unplaced
            if entry.reason == placement.DOESNT_FIT and entry.key in had_place and entry.key in asked
        ]
        if lost and not dry_run:
            for entry in lost:
                why_log.note_placement_notice(day=today, key=entry["key"], title=entry["title"], noted_at=now)
        result["no_longer_fits"] = lost
    if skipped:
        result["skipped_changes"] = skipped
    outcome, error = "planned", None
    if context.writer is not None and (settings.write or dry_run):
        from gremlin_town.sources.google.calendar_writer import CalendarWriteError

        try:
            with timing.part("calendar_write"):
                result["calendar"] = apply_plan(
                    context.writer, plan, now, busy=settings.busy, dry_run=dry_run or not settings.write,
                    entries=entries, keep_keys=keep_keys,
                )
            outcome = "written" if settings.write and not dry_run else "planned"
            if pins:
                result["kept_as_moved"] = sorted(f"{key}:{part}" for key, part in pins)
            if repair_due and outcome == "written":
                why_log.record_run(kind=REPAIR_RUN, started_at=now, last_event_cursor=today.isoformat(), outcome="repaired")
                result["repaired"] = True
        except CalendarWriteError as exc:
            outcome, error = "write_failed", f"{exc.reason.value}: {exc}"
            result["error"] = error
    result["outcome"] = outcome
    result["plan"] = placement.plan_to_mapping(plan)
    if not dry_run:
        # The times the engine may now have on the calendar: this plan's, and, after a failed write,
        # also every time it had before, since some writes landed and some didn't.
        now_known: Known = {}
        if outcome == "write_failed":
            for spot, times in known.items():
                now_known.setdefault(spot, set()).update(times)
        for slot in plan.slots:
            now_known.setdefault((slot.key, slot.part), set()).add((slot.start, slot.end))
        stored = placement.plan_to_mapping(plan, with_text=False) | {
            "pins": [
                {"key": key, "part": part, "start": start.isoformat(), "end": end.isoformat(),
                 "entry_key": keep_keys.get((key, part))}
                for (key, part), (start, end) in sorted(pins.items())
            ],
            "known": sorted(
                [key, part, start.isoformat(), end.isoformat()] for (key, part), times in now_known.items() for start, end in times
            ),
            "away": sorted(away),
        }
        why_log.record_placement(
            day=today, ran_at=now, fingerprint=fingerprint, changes_seen=redraws,
            plan=stored, outcome=outcome, error=error,
        )
    return result


def clear_future(writer: Any, now: datetime) -> dict[str, list[str]]:
    """Remove today's future engine entries (a rollback step; needs Nic's go on the hub). One the
    writer refuses (guests added, say) is skipped and reported, and the rest still go."""

    removed: list[str] = []
    skipped: list[str] = []
    for entry in writer.list_placed(today_in_singapore(now)):
        if entry.slot is not None and not entry.slot.all_day and entry.slot.start > now:
            try:
                writer.delete_placed(entry, now=now)
            except ValueError:
                skipped.append(f"{entry.role} {entry.key}")
                continue
            removed.append(f"{entry.role} {entry.key}")
    return {"removed": removed, "skipped": skipped}


def status(why_log: WhyLog) -> dict[str, Any]:
    last = why_log.latest_placement()
    if last is None:
        return {"last_run": None}
    plan = placement.plan_from_mapping(last["plan"])
    return {
        "last_run": last["ran_at"], "day": last["day"], "outcome": last["outcome"], "error": last["error"],
        # Times and kinds only: the why-log keeps no task text (build plan slice 4).
        "summary": list(placement.plan_summary(plan))[:1],
        "slots": len(plan.slots), "not_placed": len(plan.unplaced),
    }
