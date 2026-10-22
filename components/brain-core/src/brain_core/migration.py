"""Strict MetaVault decision conversion with field and hash round trips."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from brain_core.contracts import stable_hash
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import read_local_text
from brain_core.records import Evidence, RECORD_SCHEMA, Record, RecordStore, new_record_id, record_from_mapping

_START = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] Decision: (.+)$")
_FIELD = re.compile(r"^  (Decision-ID|Because|Forecloses|Governs|Encoded in|Completeness check|State):(?: (.*))?$")
_REQUIRED = ("Decision-ID", "Because", "Forecloses", "Governs", "Encoded in", "Completeness check", "State")
_SINGAPORE = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class LegacyDecision:
    date: str
    title: str
    fields: dict[str, str]
    source_text: str

    @property
    def decision_id(self) -> str:
        return self.fields["Decision-ID"]

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()

    @property
    def field_sha256(self) -> str:
        return stable_hash({"date": self.date, "title": self.title, **self.fields})


def parse_decision_log(text: str, locator: str = "ops/decisions.md") -> tuple[LegacyDecision, ...]:
    records: list[LegacyDecision] = []
    current_date: str | None = None
    title = ""
    fields: dict[str, str] = {}
    source_lines: list[str] = []
    active_field: str | None = None

    def finish() -> None:
        nonlocal current_date, title, fields, source_lines, active_field
        if current_date is None:
            return
        missing = [field for field in _REQUIRED if not fields.get(field)]
        if missing:
            raise BrainError(
                ErrorCode.MIGRATION_MISMATCH,
                f"Decision record is incomplete in {locator}",
                details={"title": title, "missing": missing},
            )
        records.append(
            LegacyDecision(
                date=current_date,
                title=title,
                fields=dict(fields),
                source_text="\n".join(source_lines).rstrip() + "\n",
            )
        )
        current_date = None
        title = ""
        fields = {}
        source_lines = []
        active_field = None

    for line in text.splitlines():
        start = _START.match(line)
        if start:
            finish()
            current_date, title = start.groups()
            source_lines = [line]
            continue
        if current_date is None:
            continue
        field_match = _FIELD.match(line)
        if field_match:
            active_field = field_match.group(1)
            if active_field in fields:
                raise BrainError(ErrorCode.MIGRATION_MISMATCH, f"Duplicate decision field in {locator}", details={"title": title, "field": active_field})
            fields[active_field] = field_match.group(2) or ""
            source_lines.append(line)
        elif line.startswith("  ") and active_field is not None:
            fields[active_field] = f"{fields[active_field]}\n{line.strip()}".strip()
            source_lines.append(line)
        elif not line.strip():
            finish()
        else:
            raise BrainError(
                ErrorCode.MIGRATION_MISMATCH,
                f"Unexpected decision-log line in {locator}",
                details={"title": title, "line": line},
            )
    finish()
    seen: set[str] = set()
    for record in records:
        if record.decision_id in seen:
            raise BrainError(ErrorCode.DUPLICATE_ID, f"Duplicate Decision-ID: {record.decision_id}", details={"decision_id": record.decision_id})
        seen.add(record.decision_id)
    return tuple(records)


def decision_to_record(decision: LegacyDecision, *, locator: str = "ops/decisions.md") -> Record:
    created = datetime.fromisoformat(f"{decision.date}T00:00:00+08:00")
    entropy = hashlib.sha256(decision.decision_id.encode("utf-8")).hexdigest()[:8]
    evidence = Evidence(
        source_type="legacy_decision_log",
        locator=f"{locator}#{decision.decision_id}",
        content_sha256=decision.source_sha256,
        excerpt=f"Decision: {decision.title}",
    )
    return record_from_mapping(
        {
            "schema": RECORD_SCHEMA,
            "id": new_record_id(created, entropy),
            "kind": "decision",
            "title": decision.title,
            "state": "accepted",
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
            "assertion": "imported",
            "confidence": 1.0,
            "sensitivity": "private",
            "vaults": ["vault_meta"],
            "owners": ["nic"],
            "entities": [],
            "evidence": [evidence.as_dict()],
            "supersedes": [],
            "dedupe_key": f"decision:{decision.decision_id}",
            "data": {
                "because": decision.fields["Because"],
                "forecloses": decision.fields["Forecloses"],
                "governs": decision.fields["Governs"],
                "encoded_in": decision.fields["Encoded in"],
                "completeness_check": decision.fields["Completeness check"],
                "legacy_decision_id": decision.decision_id,
                "legacy_state": decision.fields["State"],
                "legacy_date": decision.date,
                "legacy_field_sha256": decision.field_sha256,
            },
            "body": "",
        },
        f"decision:{decision.decision_id}",
    )


def record_to_legacy_fields(record: Record) -> dict[str, str]:
    if record.kind != "decision" or record.assertion != "imported":
        raise BrainError(ErrorCode.MIGRATION_MISMATCH, "Record is not an imported legacy decision", details={"record_id": record.id})
    return {
        "Decision-ID": record.data["legacy_decision_id"],
        "Because": record.data["because"],
        "Forecloses": record.data["forecloses"],
        "Governs": record.data["governs"],
        "Encoded in": record.data["encoded_in"],
        "Completeness check": record.data["completeness_check"],
        "State": record.data["legacy_state"],
    }


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    source: str
    source_sha256: str
    count: int
    decision_ids: tuple[str, ...]
    field_sha256: str
    record_sha256: str
    created: int
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_sha256": self.source_sha256,
            "count": self.count,
            "decision_ids": list(self.decision_ids),
            "field_sha256": self.field_sha256,
            "record_sha256": self.record_sha256,
            "created": self.created,
            "dry_run": self.dry_run,
        }


def import_decisions(source: str | Path, store: RecordStore | None = None, *, dry_run: bool = True) -> ImportReceipt:
    path = Path(source)
    text = read_local_text(path, max_bytes=5_000_000)
    decisions = parse_decision_log(text, str(path))
    records = tuple(decision_to_record(decision, locator=str(path)) for decision in decisions)
    for decision, record in zip(decisions, records, strict=True):
        round_trip = record_to_legacy_fields(record)
        if round_trip != decision.fields or record.data["legacy_field_sha256"] != decision.field_sha256:
            raise BrainError(ErrorCode.MIGRATION_MISMATCH, "Decision field round trip failed", details={"decision_id": decision.decision_id})
    if not dry_run and store is None:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "A record store is required for a live import", details={"field": "store"})
    created = 0
    if not dry_run:
        assert store is not None
        for record in records:
            created += int(store.create(record).created)
    return ImportReceipt(
        source=str(path),
        source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        count=len(decisions),
        decision_ids=tuple(decision.decision_id for decision in decisions),
        field_sha256=stable_hash([decision.field_sha256 for decision in decisions]),
        record_sha256=stable_hash([record.sha256 for record in records]),
        created=created,
        dry_run=dry_run,
    )
