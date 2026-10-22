from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain_core.clearout import CLEAROUT_SCHEMA, plan_clearout
from brain_core.errors import BrainError
from brain_core.records import RECORD_SCHEMA_V1, RECORD_SCHEMA_V2, RecordStore, new_record_id

NOW = datetime(2026, 9, 26, 21, 0, 0, tzinfo=UTC)
HASH = hashlib.sha256(b"clearout-fixture").hexdigest()


def evidence(excerpt: str = "evidence") -> dict[str, object]:
    return {
        "source_type": "fixture",
        "locator": "fixture:1",
        "content_sha256": HASH,
        "excerpt": excerpt,
    }


def v1_mapping(kind: str, *, entropy: str, state: str, data: dict[str, object]) -> dict[str, object]:
    return {
        "schema": RECORD_SCHEMA_V1,
        "id": new_record_id(NOW, entropy),
        "kind": kind,
        "title": f"{kind} fixture",
        "state": state,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "assertion": "explicit",
        "confidence": 1.0,
        "sensitivity": "private",
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [evidence()],
        "supersedes": [],
        "dedupe_key": None,
        "data": data,
        "body": "",
    }


def v2_mapping(kind: str, *, entropy: str, state: str, data: dict[str, object]) -> dict[str, object]:
    return {
        "schema": RECORD_SCHEMA_V2,
        "id": new_record_id(NOW, entropy),
        "kind": kind,
        "title": f"{kind} fixture",
        "state": state,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "sensitivity": "private",
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [evidence("owner words here")],
        "supersedes": [],
        "dedupe_key": None,
        "data": data,
        "body": "",
        "source": {
            "said_by": "owner",
            "came_by": "direct",
            "words": "owner words here",
            "recorded_by": "chatgpt-web",
        },
    }


def populate(root: Path) -> RecordStore:
    root.mkdir(parents=True, exist_ok=True)
    (root / "records").mkdir(exist_ok=True)
    store = RecordStore(root)

    from brain_core.records import record_from_mapping

    capture = record_from_mapping(
        v1_mapping(
            "capture",
            entropy="000001",
            state="inbox",
            data={"channel": "cli", "original_source_hash": HASH, "route": None, "duplicate_of": None},
        )
    )
    store.create(capture)
    store.revise(capture.id, now=NOW, state="triaged", evidence=())  # forms a 2-file revision chain

    commitment = record_from_mapping(
        v1_mapping(
            "commitment",
            entropy="000002",
            state="proposed",
            data={"owner": "nic", "action": "send deck", "time": {}, "blocked_by": [], "decision_ids": []},
        )
    )
    store.create(commitment)

    decision = record_from_mapping(
        v1_mapping(
            "decision",
            entropy="000003",
            state="accepted",
            data={
                "because": "evidence",
                "forecloses": "delay",
                "governs": "phase",
                "encoded_in": "plan.md",
                "completeness_check": "tests pass",
            },
        )
    )
    store.create(decision)

    continuity = record_from_mapping(
        v1_mapping(
            "continuity",
            entropy="000004",
            state="open",
            data={
                "session_id": "session",
                "intent": "continue",
                "sources": [],
                "effects": [],
                "decisions": [],
                "commitments": [],
                "candidates": [],
                "blockers": [],
                "next_step": "keep going",
            },
        )
    )
    store.create(continuity)

    entity = record_from_mapping(
        v1_mapping(
            "entity",
            entropy="000005",
            state="active",
            data={
                "entity_type": "person",
                "why_it_matters": "decision owner",
                "privacy": "private",
                "linked_events": ["decision:test"],
            },
        )
    )
    store.create(entity)

    preference = record_from_mapping(
        v1_mapping(
            "preference",
            entropy="000006",
            state="active",
            data={"scope": "communication", "stability": "durable", "evidence": [], "correction_history": []},
        )
    )
    store.create(preference)

    item = record_from_mapping(
        v2_mapping("item", entropy="000007", state="open", data={"level": "possible", "size": "task"})
    )
    store.create(item)

    fact = record_from_mapping(
        v2_mapping("fact", entropy="000008", state="current", data={"observed_at": NOW.isoformat()})
    )
    store.create(fact)

    return store


def test_plan_clearout_lists_every_capture_and_commitment_version_and_keeps_the_rest(tmp_path: Path) -> None:
    populate(tmp_path)
    plan = plan_clearout(str(tmp_path))

    assert plan.as_dict()["schema"] == CLEAROUT_SCHEMA
    assert plan.as_dict()["dry_run"] is True

    archived_kinds = sorted(entry.kind for entry in plan.archive)
    assert archived_kinds == ["capture", "capture", "commitment"]
    assert plan.totals == {"archive": 3, "keep": 6}
    assert plan.keep == {
        "decision": 1,
        "continuity": 1,
        "entity": 1,
        "preference": 1,
        "item": 1,
        "fact": 1,
    }

    for entry in plan.archive:
        assert entry.current_path.startswith(f"records/{entry.kind}/2026/09/")
        assert entry.archive_path == entry.current_path.replace("records/", "archive/records/", 1)
        assert len(entry.content_sha256) == 64
        assert (tmp_path / entry.current_path).is_file()
        assert not (tmp_path / entry.archive_path).exists()

    assert plan.archive == tuple(sorted(plan.archive, key=lambda entry: entry.record_id))
    assert len(plan.list_sha256) == 64


def test_plan_clearout_never_writes_anything(tmp_path: Path) -> None:
    populate(tmp_path)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    plan_clearout(str(tmp_path))
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert before == after
    assert not (tmp_path / "archive").exists()


def test_plan_clearout_accepts_no_apply_parameter() -> None:
    import inspect

    from brain_core.clearout import plan_clearout as fn

    assert list(inspect.signature(fn).parameters) == ["root"]


def test_plan_clearout_fails_loudly_on_a_corrupt_record(tmp_path: Path) -> None:
    populate(tmp_path)
    corrupt = tmp_path / "records" / "capture" / "2026" / "09" / "brn_20260926_210000_baadf0.md"
    corrupt.write_text("not a valid record\n", encoding="utf-8")
    with pytest.raises(BrainError):
        plan_clearout(str(tmp_path))


def test_plan_clearout_empty_root(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    plan = plan_clearout(str(tmp_path))
    assert plan.archive == ()
    assert plan.keep == {}
    assert plan.totals == {"archive": 0, "keep": 0}
