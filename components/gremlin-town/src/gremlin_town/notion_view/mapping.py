"""Brain Home items as Notion rows (docs/product/steps/h-notion-view.md §2, §3).

Plain, deterministic functions with no network: which items are shown, which row each belongs to across its
versions, and the Notion property values for it. `sync.py` does the talking.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.timewords import today_in_singapore

# Only these ever leave the hub for Notion, another company's cloud (PRD §4.13; Accepted - Nic, 29 Sep 2026).
SHOWN_SENSITIVITIES = frozenset({"public", "private"})
LIVE_STATES = frozenset({"open", "waiting", "blocked", "parked"})
FINISHED_STATES = frozenset({"done", "dropped", "no_longer_needed"})
MAX_TEXT = 2000  # Notion's limit for one rich-text object

STATUS_NAMES = {
    "open": "Open", "waiting": "Waiting", "blocked": "Blocked", "parked": "Parked",
    "done": "Done", "dropped": "Dropped", "no_longer_needed": "No longer needed",
}
LEVEL_NAMES = {"possible": "Possible", "suggested": "Suggested", "accepted": "Accepted"}
SIZE_NAMES = {"task": "Task", "goal": "Goal"}
BLOCK_NAMES = {"deep_focus": "Deep focus", "admin": "Admin", "errands": "Errands"}
STAKE_CONFIRMED_HIGH = "Can't undo"
STAKE_UNCONFIRMED_HIGH = "Can't undo?"
STAKE_NAMES = {"real_cost": "Real cost", "minor": "Minor"}

RELATION_PROPERTY = "Part of"
# Every property the database must have, by Notion type. Made once through the Notion MCP; each sync checks them.
PROPERTY_TYPES: dict[str, str] = {
    "Name": "title",
    "Status": "select",
    "Level": "select",
    "Size": "select",
    "Block": "select",
    "Stake": "select",
    "First step": "rich_text",
    "Due": "date",
    "Start by": "date",
    "Window": "date",
    "Minutes": "number",
    "Owed to": "rich_text",
    "Parked until": "rich_text",
    "Picked today": "checkbox",
    RELATION_PROPERTY: "relation",
    "Brain ID": "rich_text",
    "Brain version": "rich_text",
    "Changed": "date",
}
@dataclass(frozen=True, slots=True)
class Row:
    """One shown item: ``root_id`` keys its Notion row for good; ``record`` is its newest version."""

    root_id: str
    record: Record
    part_of: tuple[str, ...]
    picked_today: bool


def picked_today_refs(history: Iterable[Any], today: date) -> frozenset[str]:
    """Record IDs Nic picked for today (why-log `picked_today` answers, Singapore day), as `review/orient.py` reads them."""

    return frozenset(
        row.item_ref
        for row in history
        if row.response == "picked_today" and today_in_singapore(row.responded_at or row.surfaced_at) == today
    )


def _lineages(items: tuple[Record, ...]) -> dict[str, frozenset[str]]:
    """Every record ID with all the IDs it replaced, directly or through older versions still present."""

    by_id = {record.id: record for record in items}
    lineage: dict[str, frozenset[str]] = {}
    for record in items:
        seen = {record.id}
        pending = list(record.supersedes)
        while pending:
            older = pending.pop()
            if older in seen:
                continue
            seen.add(older)
            if older in by_id:
                pending.extend(by_id[older].supersedes)
        lineage[record.id] = frozenset(seen)
    return lineage


def plan_rows(
    records: Iterable[Record], *, now: datetime, done_days: int, picked: frozenset[str] = frozenset(),
) -> tuple[Row, ...]:
    """The rows Notion should hold, sorted by root: newest version of each shown item.

    An item's root is its oldest known ID; record IDs sort by the time in them, so the smallest is the oldest. If
    two current versions share a root, the newer one wins."""

    items = tuple(record for record in records if record.kind == "item" and record.schema == RECORD_SCHEMA_V2)
    lineage = _lineages(items)
    root_of = {record_id: min(ids) for record_id, ids in lineage.items()}
    for ids in lineage.values():
        for record_id in ids:
            root_of.setdefault(record_id, min(ids))
    replaced = {older for record in items for older in record.supersedes}
    cutoff = now - timedelta(days=done_days)

    chosen: dict[str, Record] = {}
    for record in items:
        if record.id in replaced or record.sensitivity not in SHOWN_SENSITIVITIES:
            continue
        if record.state not in LIVE_STATES | FINISHED_STATES:
            continue
        if record.state in FINISHED_STATES and record.updated_at < cutoff:
            continue
        root = root_of[record.id]
        current = chosen.get(root)
        if current is None or (record.updated_at, record.id) > (current.updated_at, current.id):
            chosen[root] = record

    rows = []
    for root, record in sorted(chosen.items()):
        part_of = sorted({
            root_of.get(link["target"], link["target"])
            for link in record.links
            if link.get("type") == "part_of" and isinstance(link.get("target"), str)
        } - {root})
        rows.append(Row(
            root_id=root,
            record=record,
            part_of=tuple(part_of),
            picked_today=bool(lineage[record.id] & picked),
        ))
    return tuple(rows)


def _text(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": value[:MAX_TEXT]}}]}


def _select(value: str | None) -> dict[str, Any]:
    return {"select": None if value is None else {"name": value}}


def _date(start: Any, end: Any = None) -> dict[str, Any]:
    if not isinstance(start, str) or not start:
        return {"date": None}
    value: dict[str, Any] = {"start": start}
    if isinstance(end, str) and end:
        value["end"] = end
    return {"date": value}


def _stake(data: Mapping[str, Any]) -> str | None:
    importance = data.get("importance")
    if not isinstance(importance, Mapping):
        return None
    level = importance.get("level")
    if level == "high":
        return STAKE_CONFIRMED_HIGH if importance.get("confirmed") is True else STAKE_UNCONFIRMED_HIGH
    return STAKE_NAMES.get(level)


def _parked_until(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("event"), str):
        return f"after: {value['event']}"
    return None


def properties(row: Row, *, page_for: Mapping[str, str]) -> dict[str, Any]:
    """Notion property values for one row. ``page_for`` maps roots to page IDs, for `Part of`; a goal that isn't
    shown (say it's `restricted`) is simply left out."""

    record = row.record
    data = record.data
    timing = data.get("timing") if isinstance(data.get("timing"), Mapping) else {}
    window = timing.get("window") if isinstance(timing.get("window"), Mapping) else {}
    minutes = timing.get("duration_minutes")
    return {
        "Name": {"title": [{"type": "text", "text": {"content": (record.title or "(untitled)")[:MAX_TEXT]}}]},
        "Status": _select(STATUS_NAMES.get(record.state)),
        "Level": _select(LEVEL_NAMES.get(data.get("level"))),
        "Size": _select(SIZE_NAMES.get(data.get("size"))),
        "Block": _select(BLOCK_NAMES.get(data.get("block"))),
        "Stake": _select(_stake(data)),
        "First step": _text(data.get("first_step")),
        "Due": _date(timing.get("due")),
        "Start by": _date(timing.get("start_by")),
        "Window": _date(window.get("start"), window.get("end")),
        "Minutes": {"number": minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else None},
        "Owed to": _text(data.get("owed_to")),
        "Parked until": _text(_parked_until(data.get("parked_until"))),
        "Picked today": {"checkbox": row.picked_today},
        RELATION_PROPERTY: {"relation": [{"id": page_for[root]} for root in row.part_of if root in page_for]},
        "Brain ID": _text(row.root_id),
        "Brain version": _text(record.id),
        "Changed": _date(record.updated_at.isoformat()),
    }


def content_hash(values: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def schema_problems(found: Mapping[str, Any]) -> tuple[str, ...]:
    """What the database is missing or has as the wrong type, from a `GET /v1/databases/{id}` `properties` map."""

    problems = []
    for name, kind in PROPERTY_TYPES.items():
        value = found.get(name)
        actual = value.get("type") if isinstance(value, Mapping) else None
        if actual is None:
            problems.append(f"missing {name!r} ({kind})")
        elif actual != kind:
            problems.append(f"{name!r} is {actual}, needs {kind}")
    return tuple(problems)
