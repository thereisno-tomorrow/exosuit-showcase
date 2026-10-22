"""Synthetic exhibition scenarios and deterministic fakes; never reads Brain Home."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from brain_core.records import parse_record, record_from_mapping, render_record

ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 9, 30, 12, 20, tzinfo=timezone(timedelta(hours=8)))


def manifest() -> dict:
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def load_records(group: str = "audited") -> tuple:
    """Read actual Markdown records; groups are audited or made_up_post_audit."""
    return tuple(parse_record(path.read_text(encoding="utf-8"), str(path))
                 for path in sorted((ROOT / group / "records" / "item").rglob("*.md")))


def write_fixture(destination: Path, *, include_made_up: bool = False) -> tuple[Path, ...]:
    """Materialize a fake Brain Home tree for downstream migration/storage tests."""
    records = load_records()
    if include_made_up:
        records += load_records("made_up_post_audit")
    paths = []
    for record in records:
        path = destination / f"records/item/{record.created_at:%Y/%m}/{record.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_record(record), encoding="utf-8", newline="\n")
        paths.append(path)
    return tuple(paths)


def generated_reply_records(count: int = 200) -> tuple:
    """Made-up independent open tasks, cycling audited text, with no version links.

    These are size probes, not audited history. Fields and lengths are deterministic.
    """
    open_ids = set(manifest()["audited_open_ids"])
    seeds = [r for r in load_records() if r.id in open_ids and r.data["level"] == "accepted"]
    records = []
    for index in range(count):
        value = deepcopy(seeds[index % len(seeds)].as_dict())
        now = datetime(2026, 9, 30, 5, tzinfo=UTC) + timedelta(seconds=index)
        value.update(id=f"brn_{now:%Y%m%d_%H%M%S}_{index:016x}",
                     created_at=now.isoformat(), updated_at=now.isoformat(),
                     state="open", supersedes=[], links=[], dedupe_key=None)
        # Keep the independent size probe within its original field-length envelope.
        # Expanded scenario labels are for the behavioral fixture, not this probe.
        value["title"] = value["title"].replace("exhibition", "show").replace("Exhibition", "Show")
        if value["data"].get("first_step"):
            value["data"]["first_step"] = "Review the synthetic next step"
        records.append(record_from_mapping(value, "made-up reply measurement"))
    return tuple(records)
