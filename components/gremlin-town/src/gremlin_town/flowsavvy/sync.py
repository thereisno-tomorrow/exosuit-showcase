"""One serialized reconciliation; task IDs and schedule stay in hub state only."""
from datetime import datetime, timedelta

from brain_core.timewords import SINGAPORE

from .client import FlowSavvyError
from .mapping import map_tasks


def sync_once(*, records, client, state, settings, now, picked=frozenset()):
    if not settings.enabled:
        return {"enabled": False, "created": 0, "updated": 0, "completed": 0, "removed": 0}
    if now.tzinfo is None:
        raise ValueError("FlowSavvy sync needs an aware datetime")
    with state.sync_lock():
        try:
            return _sync(records=records, client=client, state=state, settings=settings, now=now, picked=picked)
        except Exception as exc:
            # No raw exception text: storage errors can include private payloads.
            state.set("error", "sync_failed")
            if isinstance(exc, FlowSavvyError):
                raise
            raise FlowSavvyError("FlowSavvy sync failed before a fresh schedule was available") from None


def _sync(*, records, client, state, settings, now, picked):
    index, tasks = map_tasks(records, settings=settings, now=now, picked=picked)
    known = state.tasks()
    if any(row["phase"] == "creating" for row in known.values()):
        raise FlowSavvyError("FlowSavvy task creation needs operator reconciliation; no creation was retried")
    counts = dict(enabled=True, created=0, updated=0, completed=0, removed=0)
    active = {task.item_ref: task for task in tasks}
    # Remove ineligible tasks before creating dependencies or refreshing schedule.
    for ref, row in tuple(known.items()):
        if ref in active:
            continue
        if ref in index and index[ref].state == "done":
            if row["phase"] != "done":
                client.complete(row["remote"])
                state.put(ref, row["remote"], "done", row["payload"])
                known[ref]["phase"] = "done"
                counts["completed"] += 1
        else:
            client.remove(row["remote"])
            state.remove(ref)
            del known[ref]
            counts["removed"] += 1
    for task in tasks:
        ref = task.item_ref
        payload = dict(task.payload, blockedByIds=[known[dep]["remote"] for dep in task.after if dep in active])
        row = known.get(ref)
        if row is None:
            # Intent commits first. A timeout, process crash, or unreadable
            # response leaves this hold, never a blind second POST.
            state.put(ref, None, "creating", payload)
            remote = client.create(payload)
            state.put(ref, remote, "active", payload)
            known[ref] = {"remote": remote, "phase": "active", "payload": payload}
            counts["created"] += 1
        else:
            if row["phase"] == "done":
                client.uncomplete(row["remote"])
            if row["payload"] != payload or row["phase"] == "done":
                client.update(row["remote"], payload)
                state.put(ref, row["remote"], "active", payload)
                counts["updated"] += 1
    if any(counts[key] for key in ("created", "updated", "completed", "removed")):
        client.recalculate()
    start = now.astimezone(SINGAPORE).date()
    end = start + timedelta(days=6)  # API endDate is inclusive.
    remote_to_ref = {row["remote"]: ref for ref, row in state.tasks().items() if ref in active}
    blocks = []
    for row in client.schedule(start, end):
        ref = remote_to_ref.get(row.get("itemId"))
        if row.get("itemType") != "task" or ref is None:
            continue  # Never expose unrelated calendar events or remote tasks.
        try:
            begin, finish = (datetime.fromisoformat(row[key]) for key in ("startTime", "endTime"))
            if begin.tzinfo is not None or finish.tzinfo is not None or finish <= begin:
                raise ValueError()
            if type(row.get("completed")) is not bool or type(row.get("thisPartCompleted")) is not bool:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise FlowSavvyError("FlowSavvy returned an invalid schedule block") from None
        task = active[ref]
        blocks.append(dict(item_ref=ref, title=task.payload["title"], start=begin.replace(tzinfo=SINGAPORE).isoformat(),
                           end=finish.replace(tzinfo=SINGAPORE).isoformat(),
                           deadline=task.deadline.isoformat() if task.deadline else None,
                           completed=row["completed"], part_completed=row["thisPartCompleted"]))
    state.set("snapshot", dict(fetched_at=now.isoformat(), horizon_start=start.isoformat(), horizon_end=end.isoformat(),
                                blocks=sorted(blocks, key=lambda row: (row["start"], row["item_ref"]))))
    state.set("error", None)
    return counts
