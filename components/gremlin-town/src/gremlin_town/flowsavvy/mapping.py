"""Map the existing work reader's current records without changing stored data."""
from dataclasses import dataclass, replace
from datetime import datetime

from brain_core.schedule import effective_minutes, needs_split
from brain_core.routines import is_routine
from brain_core.timewords import SINGAPORE
from brain_core.work import first_id, index_things, is_up

from .client import FlowSavvyError


@dataclass(frozen=True)
class Task:
    item_ref: str
    payload: dict
    after: tuple[str, ...]
    deadline: datetime | None


def _date(value, *, end=False):
    day = value.get("day") or value.get("start")
    if not day:
        return None
    at = value.get("at") or ("23:59:59" if end else "00:00:00")
    return datetime.fromisoformat(f"{day}T{at}").replace(tzinfo=SINGAPORE)


def _ancestors(ref, index):
    seen = set()
    while ref in index:
        if ref in seen:
            raise FlowSavvyError("Work relationships contain a cycle")
        seen.add(ref)
        record = index[ref]
        yield record
        ref = record.data.get("parent")


def map_tasks(records, *, settings, now, picked=frozenset()):
    """Return index and ordered eligible tasks, including schedulable future waits.

    `is_up` governs actionability, except dated not-before and task dependencies
    are deferred to the scheduler. People/event waits still exclude work. The
    projection is transient; no field is saved or changed in supplied records.
    """
    index = index_things(records)
    scheduling = {}
    for ref, record in index.items():
        data = dict(record.data)
        dates = dict(data.get("dates", {}))
        if (dates.get("not_before") or {}).get("day"):
            dates.pop("not_before")
        data.update(after=[], dates=dates)
        state = record.state
        if state in {"waiting", "blocked"} and (record.data.get("after") or (record.data.get("dates", {}).get("not_before") or {}).get("day")):
            state = "open"
        scheduling[ref] = replace(record, data=data, state=state)
    tasks = {}
    for ref, record in index.items():
        data = record.data
        chain = tuple(_ancestors(ref, index))
        # A child's title includes its parent's title, and dates/stakes may be
        # inherited from further ancestors. Apply the strictest privacy boundary
        # before constructing any outgoing payload.
        if any(r.sensitivity not in {"public", "private"} for r in chain) or not is_up(ref, scheduling, now):
            continue
        # The reader inherits terminal/parked state, but not an ancestor's bare
        # legacy waiting/blocked state. Only the explicit date/dependency waits
        # cleared in the scheduling projection may be delegated to FlowSavvy.
        if any(scheduling[first_id(r)].state in {"waiting", "blocked"} for r in chain):
            continue
        if any(r.data.get("needs") or r.data.get("whose") or r.data.get("dates", {}).get("appointment")
               or (r.data.get("dates", {}).get("not_before", {}).get("event")
                   and not r.data.get("dates", {}).get("not_before", {}).get("day"))
               or r.data.get("level") == "routine" or is_routine(r) for r in chain):
            continue
        if any(child.data.get("parent") == ref for child in index.values()):
            continue  # A task with steps is represented by its open steps only.
        minutes = effective_minutes(record)
        if not minutes or needs_split(record):
            # Keep this task and its dependents out until Nic splits it. Other
            # tasks still reconcile, including removal of an earlier copy.
            continue
        dates = data.get("dates", {})
        deadline = _date(dates.get("deadline", {}), end=True)
        earliest = max((d for r in chain for name in ("not_before", "planned")
                        if (d := _date(r.data.get("dates", {}).get(name, {}))) is not None), default=None)
        kind = data.get("block") or "admin"
        today = now.astimezone(SINGAPORE).date()
        priority = "normal"
        if (ref in picked or any(first_id(r) in picked for r in chain)
                or any((planned := _date(r.data.get("dates", {}).get("planned", {}))) is not None
                       and planned.date() == today for r in chain)
                or (data.get("stake", {}).get("level") == "changes_life" and data.get("stake", {}).get("confirmed") is True)):
            priority = "high"
        if deadline and deadline.date() <= today:
            priority = "asap"
        title = record.title
        if len(chain) > 1:
            title = f"{title} — {chain[1].title}"
        tasks[ref] = Task(ref, {
            "itemType": "task", "title": title, "durationMinutes": minutes,
            "minLengthMinutes": min(60, minutes) if kind == "deep_focus" else minutes,
            "dueDateTime": deadline.replace(tzinfo=None).isoformat() if deadline else None,
            "canBeStartedAt": earliest.replace(tzinfo=None).isoformat() if earliest else None,
            "priority": priority, "schedulingHoursId": getattr(settings, kind + "_hours_id"),
            "listId": settings.list_id, "isAutoScheduled": True, "isAutoIgnored": False,
            "timeZone": "Asia/Singapore", "blockedByIds": [],
        }, tuple(dict.fromkeys(dep for r in chain for dep in r.data.get("after", ()))), deadline)
    # Explicit step order means the next open sibling is blocked by the previous
    # open sibling, in addition to any explicit after references.
    groups = {}
    for ref, r in index.items():
        parent = r.data.get("parent")
        if (r.state not in {"done", "dropped", "no_longer_needed"} and parent in index
                and index[parent].data.get("called") not in {"goal", "project"}):
            groups.setdefault(parent, []).append(ref)
    for siblings in groups.values():
        siblings.sort(key=lambda ref: (index[ref].data.get("order", 0), ref))
        for before, after in zip(siblings, siblings[1:]):
            if after in tasks:
                tasks[after] = replace(tasks[after], after=tuple(dict.fromkeys((*tasks[after].after, before))))
    # If a prerequisite cannot be sent (waiting on a person, missing/unsized,
    # parked, dropped), its dependent cannot run free in FlowSavvy either.
    changed = True
    while changed:
        changed = False
        for ref, task in tuple(tasks.items()):
            if any(dep not in tasks and (dep not in index or index[dep].state != "done") for dep in task.after):
                del tasks[ref]
                changed = True
    ordered = []
    visiting = set()
    visited = set()
    def visit(ref):
        if ref in visiting:
            raise FlowSavvyError("Work dependencies contain a cycle")
        if ref in visited:
            return
        visiting.add(ref)
        for dep in tasks[ref].after:
            if dep in tasks:
                visit(dep)
        visiting.remove(ref)
        visited.add(ref)
        ordered.append(tasks[ref])
    for ref in sorted(tasks):
        visit(ref)
    return index, tuple(ordered)
