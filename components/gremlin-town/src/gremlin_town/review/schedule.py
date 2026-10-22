"""Read a local scheduling forecast for the existing daytime return.

No remote request, record write, or separate notice store. Successful delivery
is recorded in the existing why-log, once per item per day (Proposed).
"""
from datetime import datetime

from brain_core import work
from brain_core.schedule import eligible_at
from brain_core.timewords import SINGAPORE, today_in_singapore


def late_notices(snapshot, items, history, now: datetime) -> tuple[dict, ...]:
    if snapshot is None or not snapshot.available:
        return ()
    today = today_in_singapore(now)
    told = {row.item_ref for row in history if row.rule == "scheduled_after_deadline"
            and today_in_singapore(row.surfaced_at) == today}
    entries = {entry.record.id: entry for entry in items}
    index = work.index_things(entry.record for entry in items)
    endings = {}
    for block in snapshot.blocks:
        if block.item_ref in index:
            endings[block.item_ref] = max(endings.get(block.item_ref, block.end), block.end)
    result = []
    for ref, end in sorted(endings.items()):
        record = index[ref]
        if ref in told or not eligible_at(ref, index, max(end, now), forecast=True):
            continue
        deadline = work.dates_of(ref, index).get("deadline") or {}
        if not deadline.get("day"):
            continue
        due = datetime.fromisoformat(f"{deadline['day']}T{deadline.get('at') or '23:59:59'}").replace(tzinfo=SINGAPORE)
        if end <= due:
            continue
        entry = entries[record.id]
        result.append({"key": ref, "title": record.title, "end": end.isoformat(),
                       "deadline": due.isoformat(), "sha256": entry.sha256, "path": entry.path,
                       "state": record.state})
    return tuple(result)
