"""Read-only dry-run planning for the D6 clear-out: never archives, never touches Brain Home.

Lists every capture and commitment record (v1 or v2 alike, every version in a revision chain)
that would leave the live set. Decisions, continuity, entity and preference records - and every
v2 record, which has no capture/commitment kind - are kept and only counted.
See docs/product/steps/c-record-format.md §7.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from brain_core.contracts import stable_hash
from brain_core.records import Record, RecordStore, render_record

CLEAROUT_SCHEMA = "brain.clearout-plan/v1"
ARCHIVE_KINDS = frozenset({"capture", "commitment"})


@dataclass(frozen=True, slots=True)
class ClearoutEntry:
    record_id: str
    kind: str
    state: str
    title: str
    created_at: str
    current_path: str
    archive_path: str
    content_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "state": self.state,
            "title": self.title,
            "created_at": self.created_at,
            "current_path": self.current_path,
            "archive_path": self.archive_path,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class ClearoutPlan:
    root: str
    archive: tuple[ClearoutEntry, ...]
    keep: dict[str, int]
    totals: dict[str, int]
    list_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CLEAROUT_SCHEMA,
            "dry_run": True,
            "root": self.root,
            "archive": [entry.as_dict() for entry in self.archive],
            "keep": dict(self.keep),
            "totals": dict(self.totals),
            "list_sha256": self.list_sha256,
        }


def _current_path(record: Record) -> str:
    moment = record.created_at.astimezone(UTC)
    return f"records/{record.kind}/{moment:%Y}/{moment:%m}/{record.id}.md"


def _archive_path(record: Record) -> str:
    moment = record.created_at.astimezone(UTC)
    return f"archive/records/{record.kind}/{moment:%Y}/{moment:%m}/{record.id}.md"


def plan_clearout(root: str) -> ClearoutPlan:
    """List what would archive; a corrupt record fails the whole run loudly. Never writes."""

    store = RecordStore(root)
    archive_entries: list[ClearoutEntry] = []
    keep_counts: dict[str, int] = {}
    for record in store.all():
        if record.kind not in ARCHIVE_KINDS:
            keep_counts[record.kind] = keep_counts.get(record.kind, 0) + 1
            continue
        content_sha256 = hashlib.sha256(render_record(record).encode("utf-8")).hexdigest()
        archive_entries.append(
            ClearoutEntry(
                record_id=record.id,
                kind=record.kind,
                state=record.state,
                title=record.title,
                created_at=record.created_at.isoformat(),
                current_path=_current_path(record),
                archive_path=_archive_path(record),
                content_sha256=content_sha256,
            )
        )
    archive_entries.sort(key=lambda entry: entry.record_id)
    totals = {"archive": len(archive_entries), "keep": sum(keep_counts.values())}
    list_sha256 = stable_hash([entry.as_dict() for entry in archive_entries])
    return ClearoutPlan(
        root=str(store.root),
        archive=tuple(archive_entries),
        keep=keep_counts,
        totals=totals,
        list_sha256=list_sha256,
    )
