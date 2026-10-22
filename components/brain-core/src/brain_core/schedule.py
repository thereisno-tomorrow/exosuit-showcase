"""Check a forecast against fresh work without changing stored records."""
from dataclasses import replace
from datetime import datetime
from math import ceil

from brain_core import work
from brain_core.records import OWNER_ESTIMATE
from brain_core.planning import followup_triggered
from brain_core.routines import is_routine
from brain_core.timewords import SINGAPORE

SPLIT_TASK_NOTICE = "Not scheduled · split this into shorter tasks"


def effective_minutes(record):
    """FlowSavvy's length, preserving owner minutes and existing estimate padding."""
    timing = record.data.get("timing") or {}
    minutes = timing.get("duration_minutes")
    if not minutes or timing.get("estimated_by") == OWNER_ESTIMATE:
        return minutes
    return timing.get("padded_minutes") or ceil(minutes * 1.5)


def needs_split(record) -> bool:
    """The API excludes 24 hours or more; never truncate or invent task parts."""
    return (effective_minutes(record) or 0) >= 1440


def split_notice_required(record, index) -> bool:
    """Only an oversized, unsplit task needs Nic to split it for scheduling.

    Containers already have steps, including when those steps are all done.
    Appointments and routines belong to their existing paths, not task scheduling.
    """
    return bool(record.kind == "item" and needs_split(record)
                and work.kind_of(record, index) in {"task", "subtask"}
                and record.state not in {"done", "dropped", "no_longer_needed", "parked"}
                and record.data.get("level") != "routine" and not is_routine(record)
                and not work.dates_of(record, index).get("appointment")
                and not any(child.data.get("parent") == work.first_id(record) for child in index.values()))


def eligible_at(ref, index, at: datetime, *, forecast=False, _seen=frozenset()) -> bool:
    """Future forecasts may follow schedulable prerequisites; current work may not.

    A parent can put all of its children on hold. The ordinary work reader only
    inherits ended/parked state, so inspect every ancestor's own constraints here.
    This is a projection for an external forecast, never a change to the work graph.
    """
    if ref in _seen or ref not in index:
        return False
    if forecast and needs_split(index[ref]):
        return False
    seen = _seen | {ref}
    chain = []
    cursor = ref
    ancestors = set()
    while cursor in index:
        if cursor in ancestors:
            return False
        ancestors.add(cursor)
        record = index[cursor]
        chain.append(record)
        cursor = record.data.get("parent")
    projected = dict(index)
    for record in chain:
        data = record.data
        dates = data.get("dates") or {}
        not_before = dates.get("not_before") or {}
        after = list(data.get("after") or ())
        parent = data.get("parent")
        if parent in index and index[parent].data.get("called") not in {"goal", "project"}:
            siblings = sorted((key for key, sibling in index.items()
                if sibling.data.get("parent") == parent and sibling.state not in {"done", "dropped", "no_longer_needed"}),
                key=lambda key: (index[key].data.get("order", 0), key))
            key = work.first_id(record)
            if key in siblings and (position := siblings.index(key)) > 0:
                after.append(siblings[position - 1])
        if (record.state in {"done", "dropped", "no_longer_needed", "parked"}
                or record.sensitivity not in {"public", "private"}
                or data.get("needs") or data.get("whose") or dates.get("appointment")
                or data.get("level") == "routine" or is_routine(record)):
            return False
        if record.state in {"waiting", "blocked"} and not (data.get("after") or not_before.get("day")):
            return False
        if not_before.get("event") and not not_before.get("day"):
            return False
        for name in ("not_before", "planned"):
            value = dates.get(name) or {}
            day = value.get("day") or value.get("start")
            if day:
                earliest = datetime.fromisoformat(f"{day}T{value.get('at') or '00:00:00'}").replace(tzinfo=SINGAPORE)
                if earliest > at:
                    return False
        for dependency in after:
            other = index.get(dependency)
            triggered = followup_triggered(other, work.first_id(record))
            if triggered or (other is not None and other.state == "done"):
                continue
            if (not forecast or other is None or not (other.data.get("timing") or {}).get("duration_minutes")
                    or not eligible_at(dependency, index, at, forecast=True, _seen=seen | ancestors)):
                return False
        if forecast:
            state = "open" if record.state in {"waiting", "blocked"} else record.state
            projected[work.first_id(record)] = replace(record, state=state, data={**data, "after": []})
    return work.is_up(ref, projected, at)
