from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain_core.errors import BrainError, ErrorCode
from brain_core.records import (
    Evidence,
    RECORD_SCHEMA,
    RecordStore,
    commitment_candidate_from_mapping,
    commitment_conditions,
    new_record_id,
    record_from_commitment_candidate,
    record_from_mapping,
)

NOW = datetime(2026, 8, 29, 4, 0, 0, tzinfo=UTC)
SOURCE_HASH = hashlib.sha256(b"phase-2-proof").hexdigest()


def evidence(locator: str = "proof:1") -> Evidence:
    return Evidence(
        source_type="fixture",
        locator=locator,
        content_sha256=hashlib.sha256(locator.encode()).hexdigest(),
        excerpt=locator,
    )


def record_mapping(kind: str, data: dict[str, object], *, entropy: str, state: str, sensitivity: str = "private") -> dict[str, object]:
    return {
        "schema": RECORD_SCHEMA,
        "id": new_record_id(NOW, entropy),
        "kind": kind,
        "title": f"{kind} proof",
        "state": state,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "assertion": "explicit",
        "confidence": 1.0,
        "sensitivity": sensitivity,
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [evidence().as_dict()],
        "supersedes": [],
        "dedupe_key": None,
        "data": data,
        "body": "",
    }


def test_one_real_promise_is_distinguished_from_five_hypotheticals() -> None:
    source = (
        "I will send the final deck. Maybe I could write a post. If the launch moves, I might revise it. "
        "We could ask for feedback. Perhaps I should make a video. It would be nice to hold a meetup."
    )
    phrases = (
        ("send the final deck", "explicit"),
        ("write a post", "inferred"),
        ("revise it", "inferred"),
        ("ask for feedback", "inferred"),
        ("make a video", "inferred"),
        ("hold a meetup", "inferred"),
    )
    built = []
    for index, (phrase, assertion) in enumerate(phrases, start=1):
        candidate = commitment_candidate_from_mapping(
            {
                "actor": "nic",
                "source_message_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "evidence_excerpt": phrase,
                "assertion": assertion,
                "confidence": 0.95,
                "owner": "nic",
                "action": phrase,
                "time": {},
            }
        )
        built.append(
            record_from_commitment_candidate(
                candidate,
                authenticated_actor="nic",
                source_message=source,
                session_id="promise-proof",
                now=NOW + timedelta(seconds=index),
                allow_explicit_confirmation=True,
                entropy=f"{index:04x}",
            )
        )
    assert sum(item is not None and item.state == "confirmed" for item in built) == 1
    assert sum(item is not None and item.state == "proposed" for item in built) == 5


def test_duplicate_captures_are_linked_across_channels_without_deletion(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    first = record_from_mapping(
        record_mapping(
            "capture",
            {"channel": "cli", "original_source_hash": SOURCE_HASH, "route": None, "duplicate_of": None},
            entropy="a1b2",
            state="inbox",
        )
    )
    store.create(first)
    assert store.exact_capture_duplicate(SOURCE_HASH) == first
    second_value = record_mapping(
        "capture",
        {"channel": "telegram", "original_source_hash": SOURCE_HASH, "route": None, "duplicate_of": first.id},
        entropy="b2c3",
        state="inbox",
    )
    second_value["created_at"] = (NOW + timedelta(seconds=1)).isoformat()
    second_value["updated_at"] = second_value["created_at"]
    second_value["id"] = new_record_id(NOW + timedelta(seconds=1), "b2c3")
    second = record_from_mapping(second_value)
    store.create(second)
    assert len(store.all()) == 2
    assert second.data["duplicate_of"] == first.id
    assert {item.data["channel"] for item in store.current(kind="capture")} == {"cli", "telegram"}


def test_commitment_conditions_cover_phase2_operational_states() -> None:
    commitment = record_from_mapping(
        record_mapping(
            "commitment",
            {
                "owner": None,
                "action": "Complete the recovery drill",
                "time": {
                    "due": (NOW - timedelta(days=1)).isoformat(),
                    "stale_after": (NOW - timedelta(hours=1)).isoformat(),
                },
                "blocked_by": ["credential:b2"],
                "decision_ids": ["20260829-brain-core-phase2-approved"],
                "conflict_ids": ["commitment:other"],
                "recurrence": "weekly",
                "waiting_on": "nic",
                "duplicate_of": "commitment:original",
            },
            entropy="c3d4",
            state="waiting",
        )
    )
    assert commitment_conditions(commitment, now=NOW) == (
        "recurring",
        "waiting",
        "overdue",
        "blocked",
        "conflicting",
        "duplicate",
        "stale",
        "unowned",
    )
    with pytest.raises(BrainError) as raised:
        commitment_conditions(commitment, now=datetime(2026, 8, 29))
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(BrainError):
        commitment_conditions(
            record_from_mapping(
                record_mapping(
                    "preference",
                    {"scope": "tests", "stability": "durable", "evidence": ["proof:1"], "correction_history": []},
                    entropy="d4e5",
                    state="active",
                )
            ),
            now=NOW,
        )


def test_preference_can_be_corrected_and_retired_but_temporary_is_refused(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    original = record_from_mapping(
        record_mapping(
            "preference",
            {"scope": "receipts", "stability": "durable", "evidence": ["proof:1"], "correction_history": []},
            entropy="e5f6",
            state="active",
        )
    )
    store.create(original)
    corrected, _ = store.revise(
        original.id,
        now=NOW + timedelta(seconds=1),
        data={"correction_history": ["Prefer shorter receipts"]},
        evidence=(evidence("proof:correction"),),
        entropy="f6a7",
    )
    retired, _ = store.revise(
        corrected.id,
        now=NOW + timedelta(seconds=2),
        state="retired",
        evidence=(evidence("proof:retire"),),
        entropy="a7b8",
    )
    assert len(store.all()) == 3
    assert store.current(kind="preference") == (retired,)
    temporary = record_mapping(
        "preference",
        {"scope": "this reply", "stability": "temporary", "evidence": ["proof:1"], "correction_history": []},
        entropy="b8c9",
        state="proposed",
    )
    with pytest.raises(BrainError) as raised:
        record_from_mapping(temporary)
    assert raised.value.code is ErrorCode.CONTRACT_INVALID


def test_bulk_relationship_ingestion_and_privacy_mismatch_are_refused() -> None:
    bulk = record_mapping(
        "entity",
        {
            "entity_type": "person",
            "why_it_matters": "decision owner",
            "privacy": "private",
            "linked_events": [f"event:{index}" for index in range(51)],
        },
        entropy="c9d0",
        state="active",
    )
    with pytest.raises(BrainError) as raised:
        record_from_mapping(bulk)
    assert raised.value.details["field"] == "data.linked_events"
    mismatch = record_mapping(
        "entity",
        {"entity_type": "person", "why_it_matters": "decision owner", "privacy": "public", "linked_events": ["event:1"]},
        entropy="d0e1",
        state="active",
        sensitivity="private",
    )
    with pytest.raises(BrainError) as raised:
        record_from_mapping(mismatch)
    assert raised.value.details["field"] == "data.privacy"


def test_deleting_node_local_derived_state_loses_no_canonical_record(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(
        record_mapping(
            "preference",
            {"scope": "tests", "stability": "durable", "evidence": ["proof:1"], "correction_history": []},
            entropy="e1f2",
            state="active",
        )
    )
    store.create(record)
    before = tuple(item.sha256 for item in store.all())
    derived = tmp_path / ".state" / "records.sqlite3"
    derived.parent.mkdir()
    with closing(sqlite3.connect(derived)) as connection:
        connection.execute("CREATE TABLE cache(record_id TEXT, sha256 TEXT)")
        connection.execute("INSERT INTO cache VALUES (?, ?)", (record.id, record.sha256))
        connection.commit()
    derived.unlink()
    after = tuple(item.sha256 for item in RecordStore(tmp_path).all())
    assert before == after == (record.sha256,)
