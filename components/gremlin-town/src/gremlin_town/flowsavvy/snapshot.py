"""Read-only schedule API. Surfaces never call FlowSavvy or write Brain Home."""
from dataclasses import dataclass
from contextlib import closing
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3

UNAVAILABLE = "Couldn't see the schedule"


@dataclass(frozen=True, slots=True)
class ScheduleBlock:
    item_ref: str
    title: str
    start: datetime
    end: datetime
    deadline: datetime | None
    completed: bool = False
    part_completed: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot:
    fetched_at: datetime | None = None
    blocks: tuple[ScheduleBlock, ...] = ()
    available: bool = False
    age_seconds: int | None = None
    message: str | None = UNAVAILABLE
    horizon_start: date | None = None
    horizon_end: date | None = None

    @property
    def late_item_refs(self) -> frozenset[str]:
        return frozenset(block.item_ref for block in self.blocks if not block.completed
                         and not block.part_completed and block.deadline and block.end > block.deadline)


def _moment(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("snapshot time requires timezone")
    return result


def read_snapshot(state_file: Path, now: datetime, max_age_seconds: int = 600) -> ScheduleSnapshot:
    if now.tzinfo is None:
        raise ValueError("snapshot reader needs an aware datetime")
    if not state_file.is_file():
        return ScheduleSnapshot()
    try:
        # URI read-only prevents an unavailable reader from creating state.
        with closing(sqlite3.connect(state_file.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            found = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('snapshot', 'error')"))
        value = json.loads(found.get("snapshot", "null"))
        if not value:
            return ScheduleSnapshot()
        fetched = _moment(value["fetched_at"])
        age = max(0, int((now - fetched).total_seconds()))
        horizon_start, horizon_end = date.fromisoformat(value["horizon_start"]), date.fromisoformat(value["horizon_end"])
        if json.loads(found.get("error", "null")) or age > max_age_seconds or now < fetched:
            return ScheduleSnapshot(fetched_at=fetched, age_seconds=age, horizon_start=horizon_start, horizon_end=horizon_end)
        blocks = tuple(ScheduleBlock(item_ref=b["item_ref"], title=b["title"], start=_moment(b["start"]),
                       end=_moment(b["end"]), deadline=_moment(b["deadline"]) if b["deadline"] else None,
                       completed=b["completed"], part_completed=b["part_completed"]) for b in value["blocks"])
        return ScheduleSnapshot(fetched, blocks, True, age, None, horizon_start, horizon_end)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return ScheduleSnapshot()
