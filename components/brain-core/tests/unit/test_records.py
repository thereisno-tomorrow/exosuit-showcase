from __future__ import annotations

import hashlib
from copy import deepcopy
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import brain_core.records as records
from brain_core.errors import BrainError, ErrorCode
from brain_core.records import (
    Evidence,
    RecordStore,
    commitment_candidate_from_mapping,
    first_saved,
    new_record_id,
    parse_record,
    record_from_commitment_candidate,
    record_from_mapping,
    render_record,
)

NOW = datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC)
HASH = hashlib.sha256(b"evidence").hexdigest()


def evidence(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_type": "session_message",
        "locator": "session:test:1",
        "content_sha256": HASH,
        "excerpt": "evidence",
        "session_id": "test",
        "actor": "nic",
    }
    value.update(changes)
    return value


def kind_data(kind: str) -> dict[str, object]:
    return {
        "capture": {"channel": "cli", "original_source_hash": HASH, "route": None, "duplicate_of": None},
        "commitment": {"owner": "nic", "action": "send deck", "time": {}, "blocked_by": [], "decision_ids": []},
        "decision": {
            "because": "evidence",
            "forecloses": "delay",
            "governs": "phase",
            "encoded_in": "plan.md",
            "completeness_check": "tests pass",
        },
        "continuity": {
            "session_id": "session",
            "intent": "continue",
            "sources": [],
            "effects": [],
            "decisions": [],
            "commitments": [],
            "candidates": [],
            "blockers": [],
            "next_step": "test",
        },
        "entity": {
            "entity_type": "person",
            "why_it_matters": "decision owner",
            "privacy": "private",
            "linked_events": ["decision:test"],
        },
        "preference": {
            "scope": "communication",
            "evidence": ["message:1"],
            "stability": "durable",
            "correction_history": [],
        },
    }[kind]


def mapping(kind: str = "decision", **changes: object) -> dict[str, object]:
    states = {
        "capture": "inbox",
        "commitment": "proposed",
        "decision": "accepted",
        "continuity": "open",
        "entity": "active",
        "preference": "active",
    }
    value: dict[str, object] = {
        "schema": "brain.record/v1",
        "id": new_record_id(NOW, "a1b2"),
        "kind": kind,
        "title": f"{kind} title",
        "state": states[kind],
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "assertion": "explicit",
        "confidence": 1.0,
        "sensitivity": "private",
        "vaults": ["vault_meta"],
        "owners": ["nic"],
        "entities": [],
        "evidence": [evidence()],
        "supersedes": [],
        "dedupe_key": None,
        "data": kind_data(kind),
        "body": "Body text",
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("kind", sorted(records.RECORD_KINDS))
def test_record_kinds_round_trip_as_deterministic_markdown(kind: str) -> None:
    record = record_from_mapping(mapping(kind), kind)
    text = render_record(record)
    loaded = parse_record(text, kind)
    assert loaded == record
    assert render_record(loaded) == text
    assert len(record.sha256) == 64


def test_evidence_optional_fields_and_grant_hash() -> None:
    item = records.evidence_from_mapping(
        evidence(session_id=None, actor=None, grant_sha256="b" * 64), "evidence"
    )
    assert item.as_dict()["grant_sha256"] == "b" * 64
    assert "session_id" not in item.as_dict()


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"unexpected": True}, "record"),
        ({"schema": "brain.record/v3"}, "schema"),
        ({"id": "bad"}, "id"),
        ({"kind": "note"}, "kind"),
        ({"state": "done"}, "state"),
        ({"created_at": "not-time"}, "created_at"),
        ({"created_at": "2026-08-29T01:02:03"}, "created_at"),
        ({"updated_at": (NOW - timedelta(seconds=1)).isoformat()}, "updated_at"),
        ({"confidence": True}, "confidence"),
        ({"confidence": 1.1}, "confidence"),
        ({"assertion": "guess"}, "assertion"),
        ({"sensitivity": "secret"}, "sensitivity"),
        ({"vaults": "vault_meta"}, "vaults"),
        ({"vaults": ["vault_meta", "vault_meta"]}, "vaults"),
        ({"evidence": []}, "evidence"),
        ({"evidence": "wrong"}, "evidence"),
        ({"supersedes": ["bad"]}, "supersedes"),
        ({"supersedes": [new_record_id(NOW, "a1b2")]}, "supersedes"),
        ({"dedupe_key": 1}, "dedupe_key"),
        ({"data": []}, "data"),
        ({"body": 1}, "body"),
    ],
)
def test_record_common_contract_failures(change: dict[str, object], field: str) -> None:
    value = mapping()
    value.update(change)
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value, "bad-record")
    assert raised.value.code is ErrorCode.CONTRACT_INVALID
    assert raised.value.details.get("field") == field


def test_record_id_timestamp_must_match_created_at() -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(mapping(id="brn_20260828_010203_a1b2"))
    assert raised.value.details["field"] == "id"


@pytest.mark.parametrize(
    "value",
    [
        evidence(extra=True),
        evidence(content_sha256="bad"),
        evidence(grant_sha256="bad"),
        evidence(session_id=1),
        evidence(excerpt=1),
    ],
)
def test_evidence_contract_failures(value: dict[str, object]) -> None:
    item = mapping(evidence=[value])
    with pytest.raises(BrainError) as raised:
        record_from_mapping(item)
    assert raised.value.code is ErrorCode.CONTRACT_INVALID


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("capture", {"channel": "cli", "original_source_hash": "bad"}),
        ("commitment", {"owner": "nic", "action": "x", "time": [], "blocked_by": [], "decision_ids": []}),
        ("decision", {"because": "", "forecloses": "x", "governs": "x", "encoded_in": "x", "completeness_check": "x"}),
        ("continuity", {"session_id": "s", "intent": "x", "next_step": "x"}),
        ("entity", {"entity_type": "place", "why_it_matters": "x", "privacy": "x", "linked_events": ["x"]}),
        ("entity", {"entity_type": "person", "why_it_matters": "x", "privacy": "x", "linked_events": []}),
        ("preference", {"scope": "x", "stability": "x", "evidence": "x", "correction_history": []}),
    ],
)
def test_kind_specific_contract_failures(kind: str, data: dict[str, object]) -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(mapping(kind, data=data))
    assert raised.value.code is ErrorCode.CONTRACT_INVALID


def test_kind_data_refuses_unknown_fields_and_empty_entity_links() -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(mapping("capture", data={**kind_data("capture"), "bulk": []}))
    assert raised.value.details["field"] == "data"
    with pytest.raises(BrainError) as raised:
        record_from_mapping(
            mapping(
                "entity",
                data={"entity_type": "person", "why_it_matters": "owner", "privacy": "private", "linked_events": []},
            )
        )
    assert raised.value.details["field"] == "data.linked_events"


def test_noncanonical_data_is_refused() -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(mapping(data={**kind_data("decision"), "bad": {1, 2}}))
    assert "canonical JSON" in raised.value.message


@pytest.mark.parametrize("now,entropy", [(datetime(2026, 1, 1), "abcd"), (NOW, "BAD")])
def test_new_record_id_refuses_naive_time_and_bad_entropy(now: datetime, entropy: str) -> None:
    with pytest.raises(BrainError):
        new_record_id(now, entropy)


def test_parse_record_refuses_missing_frontmatter() -> None:
    with pytest.raises(BrainError) as raised:
        parse_record("plain text", "plain")
    assert raised.value.code is ErrorCode.RECORD_CORRUPT


def test_store_create_is_atomic_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping())
    first = store.create(record)
    second = store.create(record)
    assert first.created is True
    assert second.created is False
    assert first.content_sha256 == second.content_sha256
    changed = record_from_mapping(mapping(title="different"))
    with pytest.raises(BrainError) as raised:
        store.create(changed)
    assert raised.value.code is ErrorCode.RECORD_EXISTS


def test_store_create_semantic_recovers_same_origin_and_noops_cross_origin(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    semantic_key = "a" * 64
    first_data = {**kind_data("decision"), "origin_id": "origin-a"}
    first = record_from_mapping(mapping(dedupe_key=semantic_key, data=first_data))

    accepted, created = store.create_semantic(
        first,
        semantic_key=semantic_key,
        origin_id="origin-a",
        observed_preimage_sha256=hashlib.sha256(b"").hexdigest(),
    )
    recovered, retry = store.create_semantic(
        first,
        semantic_key=semantic_key,
        origin_id="origin-a",
        observed_preimage_sha256="f" * 64,
    )
    second_data = {**kind_data("decision"), "origin_id": "origin-b"}
    second = record_from_mapping(
        mapping(
            id=new_record_id(NOW + timedelta(seconds=1), "b2c3"),
            created_at=(NOW + timedelta(seconds=1)).isoformat(),
            updated_at=(NOW + timedelta(seconds=1)).isoformat(),
            dedupe_key=semantic_key,
            data=second_data,
        )
    )
    duplicate, no_op = store.create_semantic(
        second,
        semantic_key=semantic_key,
        origin_id="origin-b",
        observed_preimage_sha256="f" * 64,
    )

    assert accepted == recovered == duplicate == first
    assert created.created is True
    assert retry.created is True
    assert no_op.created is False
    assert len(store.all()) == 1


def test_store_create_semantic_validates_claim_and_preimage(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    semantic_key = "a" * 64
    decision = record_from_mapping(
        mapping(dedupe_key=semantic_key, data={**kind_data("decision"), "origin_id": "origin-a"})
    )
    with pytest.raises(BrainError) as raised:
        store.create_semantic(
            decision,
            semantic_key=semantic_key,
            origin_id="origin-a",
            observed_preimage_sha256="b" * 64,
        )
    assert raised.value.details["refusal_code"] == "canonical_preimage_changed"
    assert store.all() == ()

    with pytest.raises(BrainError):
        store.create_semantic(
            decision,
            semantic_key="bad",
            origin_id="origin-a",
            observed_preimage_sha256=hashlib.sha256(b"").hexdigest(),
        )
    with pytest.raises(BrainError):
        store.create_semantic(
            decision,
            semantic_key=semantic_key,
            origin_id="BAD",
            observed_preimage_sha256=hashlib.sha256(b"").hexdigest(),
        )


def test_store_temp_failure_does_not_publish_and_postpublish_is_resumable(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping())

    def fail_temp(stage: str, _path: Path) -> None:
        if stage == "after_temp":
            raise RuntimeError("killed")

    with pytest.raises(RuntimeError, match="killed"):
        store.create(record, fault=fail_temp)
    assert store.all() == ()
    assert not tuple(tmp_path.rglob("*.tmp"))

    def fail_publish(stage: str, _path: Path) -> None:
        if stage == "after_publish":
            raise RuntimeError("crashed")

    with pytest.raises(RuntimeError, match="crashed"):
        store.create(record, fault=fail_publish)
    assert store.create(record).created is False
    assert store.get(record.id) == record


def test_store_requires_root_and_reports_missing_record(tmp_path: Path) -> None:
    with pytest.raises(BrainError) as raised:
        RecordStore(tmp_path / "missing")
    assert raised.value.code is ErrorCode.ROOT_MISSING
    with pytest.raises(BrainError) as raised:
        RecordStore(tmp_path).get("missing")
    assert raised.value.code is ErrorCode.NOT_FOUND


def test_store_revise_and_transition_preserve_history(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    original = record_from_mapping(mapping("commitment"))
    store.create(original)
    revised, receipt = store.revise(
        original.id,
        now=NOW + timedelta(seconds=1),
        data={"action": "send final deck"},
        title="Send final deck",
        body="updated",
        evidence=(records.evidence_from_mapping(evidence(locator="session:test:2"), "event"),),
        entropy="b2c3",
    )
    assert receipt.created
    assert revised.state == "proposed"
    assert revised.data["action"] == "send final deck"
    assert original.id in revised.supersedes
    assert store.current(kind="commitment") == (revised,)
    confirmed, _ = store.revise(
        revised.id,
        now=NOW + timedelta(seconds=2),
        state="confirmed",
        entropy="c3d4",
    )
    assert confirmed.state == "confirmed"
    with pytest.raises(BrainError) as raised:
        store.revise(confirmed.id, now=NOW + timedelta(seconds=3), state="proposed", entropy="d4e5")
    assert raised.value.code is ErrorCode.RECORD_TRANSITION


def test_all_lifecycle_transition_tables_are_well_formed() -> None:
    for kind, state_map in records.TRANSITIONS.items():
        assert set(state_map) == set(records.STATES[kind])
        assert all(targets <= records.STATES[kind] for targets in state_map.values())


def test_commitment_conditions_can_be_empty_and_terminal_due_is_not_overdue() -> None:
    clean = record_from_mapping(mapping("commitment"))
    assert records.commitment_conditions(clean, now=NOW) == ()
    terminal_data = {**kind_data("commitment"), "time": {"due": (NOW - timedelta(days=1)).isoformat()}}
    terminal = record_from_mapping(mapping("commitment", state="done", data=terminal_data))
    assert "overdue" not in records.commitment_conditions(terminal, now=NOW)


def test_exact_capture_duplicate_uses_current_record_only(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    assert store.exact_capture_duplicate(HASH) is None
    capture = record_from_mapping(mapping("capture"))
    store.create(capture)
    assert store.exact_capture_duplicate(HASH) == capture
    archived, _ = store.revise(capture.id, now=NOW + timedelta(seconds=1), state="archived", entropy="b2c3")
    assert store.exact_capture_duplicate(HASH) == archived
    with pytest.raises(BrainError):
        store.exact_capture_duplicate("bad")

    other = record_from_mapping(mapping("capture", data={**kind_data("capture"), "original_source_hash": "b" * 64}))
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_store = RecordStore(other_root)
    other_store.create(other)
    assert other_store.exact_capture_duplicate("c" * 64) is None


def test_store_defensive_path_and_write_failures(monkeypatch, tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping())

    def bad_commonpath(_paths: object) -> str:
        raise ValueError("different drives")

    monkeypatch.setattr(records.os.path, "commonpath", bad_commonpath)
    with pytest.raises(BrainError) as raised:
        store._destination(record)
    assert raised.value.code is ErrorCode.PATH_ESCAPE
    monkeypatch.setattr(records.os.path, "commonpath", lambda _paths: str(tmp_path.parent))
    with pytest.raises(BrainError) as raised:
        store._destination(record)
    assert raised.value.code is ErrorCode.PATH_ESCAPE


def test_store_publish_cleanup_and_scan_failures(monkeypatch, tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping())
    real_link = records.os.link

    def fail_link(_source: object, _destination: object) -> None:
        raise PermissionError("blocked")

    monkeypatch.setattr(records.os, "link", fail_link)
    with pytest.raises(BrainError) as raised:
        store.create(record)
    assert raised.value.code is ErrorCode.RECORD_WRITE_FAILED
    monkeypatch.setattr(records.os, "link", real_link)
    store.create(record)

    real_commonpath = records.os.path.commonpath
    monkeypatch.setattr(records.os.path, "commonpath", lambda _paths: (_ for _ in ()).throw(ValueError("drives")))
    with pytest.raises(BrainError) as raised:
        store.all()
    assert raised.value.code is ErrorCode.PATH_ESCAPE
    monkeypatch.setattr(records.os.path, "commonpath", lambda _paths: str(tmp_path.parent))
    with pytest.raises(BrainError) as raised:
        store.all()
    assert raised.value.code is ErrorCode.PATH_ESCAPE
    monkeypatch.setattr(records.os.path, "commonpath", real_commonpath)

    def fail_read(_path: Path, *args: object, **kwargs: object) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(BrainError) as raised:
        store.all()
    assert raised.value.code is ErrorCode.RECORD_CORRUPT


def test_store_cleanup_failure_does_not_hide_published_record(monkeypatch, tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping())

    def fail_unlink(_path: Path, *args: object, **kwargs: object) -> None:
        raise OSError("cleanup blocked")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert store.create(record).created is True


def test_store_duplicate_id_in_two_envelopes_is_rejected(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping())
    receipt = store.create(record)
    original = tmp_path / receipt.relative_path
    duplicate = original.parent.parent / "07" / original.name
    duplicate.parent.mkdir()
    duplicate.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        store.all()
    assert raised.value.code is ErrorCode.DUPLICATE_ID


def test_store_detects_duplicate_ids_and_path_envelope_mismatch(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping())
    receipt = store.create(record)
    original = tmp_path / receipt.relative_path
    duplicate = tmp_path / "records" / "capture" / "2026" / "08" / f"{record.id}.md"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(render_record(record), encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        store.all()
    assert raised.value.code is ErrorCode.RECORD_CORRUPT
    duplicate.unlink()
    wrong_name = original.with_name("wrong.md")
    os.replace(original, wrong_name)
    with pytest.raises(BrainError) as raised:
        store.all()
    assert raised.value.code is ErrorCode.RECORD_CORRUPT


def candidate(message: str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "actor": "nic",
        "source_message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "evidence_excerpt": "send the deck",
        "assertion": "explicit",
        "confidence": 0.95,
        "owner": "nic",
        "action": "Send the deck",
        "time": {"due": "2026-09-01"},
    }
    value.update(changes)
    return value


def test_commitment_candidate_authentication_confirmation_and_threshold() -> None:
    message = "I will send the deck tomorrow."
    item = commitment_candidate_from_mapping(candidate(message))
    confirmed = record_from_commitment_candidate(
        item,
        authenticated_actor="nic",
        source_message=message,
        session_id="s1",
        now=NOW,
        allow_explicit_confirmation=True,
        entropy="a1b2",
    )
    assert confirmed is not None and confirmed.state == "confirmed"
    inferred = commitment_candidate_from_mapping(candidate(message, assertion="inferred"))
    proposed = record_from_commitment_candidate(
        inferred,
        authenticated_actor="nic",
        source_message=message,
        session_id="s1",
        now=NOW,
        allow_explicit_confirmation=True,
        entropy="b2c3",
    )
    assert proposed is not None and proposed.state == "proposed"
    low = commitment_candidate_from_mapping(candidate(message, confidence=0.2))
    assert record_from_commitment_candidate(
        low,
        authenticated_actor="nic",
        source_message=message,
        session_id="s1",
        now=NOW,
        allow_explicit_confirmation=False,
    ) is None


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"source_message_sha256": "bad"},
        {"confidence": True},
        {"assertion": "possible"},
        {"time": []},
    ],
)
def test_commitment_candidate_contract_failures(change: dict[str, object]) -> None:
    value = candidate("I will send the deck.")
    value.update(change)
    with pytest.raises(BrainError):
        commitment_candidate_from_mapping(value)


def test_commitment_candidate_refuses_false_source_and_excerpt_and_bad_threshold() -> None:
    message = "I will send the deck."
    wrong_actor = commitment_candidate_from_mapping(candidate(message, actor="other"))
    with pytest.raises(BrainError, match="authentication"):
        record_from_commitment_candidate(
            wrong_actor,
            authenticated_actor="nic",
            source_message=message,
            session_id="s",
            now=NOW,
            allow_explicit_confirmation=False,
        )
    bad_excerpt = commitment_candidate_from_mapping(candidate(message, evidence_excerpt="not present"))
    with pytest.raises(BrainError, match="exact source span"):
        record_from_commitment_candidate(
            bad_excerpt,
            authenticated_actor="nic",
            source_message=message,
            session_id="s",
            now=NOW,
            allow_explicit_confirmation=False,
        )
    valid = commitment_candidate_from_mapping(candidate(message))
    with pytest.raises(BrainError) as raised:
        record_from_commitment_candidate(
            valid,
            authenticated_actor="nic",
            source_message=message,
            session_id="s",
            now=NOW,
            allow_explicit_confirmation=False,
            minimum_confidence=2,
        )
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# v2 (brain.record/v2): item, fact, source and links.
# ---------------------------------------------------------------------------


def v2_source(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "said_by": "owner",
        "came_by": "direct",
        "words": "owner words",
        "recorded_by": "chatgpt-web",
    }
    value.update(changes)
    return value


def v2_kind_data(kind: str) -> dict[str, object]:
    return {
        "item": {"level": "possible", "size": "task"},
        "fact": {"observed_at": NOW.isoformat()},
    }[kind]


def v2_mapping(kind: str = "item", **changes: object) -> dict[str, object]:
    states = {"item": "open", "fact": "current"}
    value: dict[str, object] = {
        "schema": records.RECORD_SCHEMA_V2,
        "id": new_record_id(NOW, "a1b2"),
        "kind": kind,
        "title": f"{kind} title",
        "state": states[kind],
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "sensitivity": "private",
        "vaults": ["vault_meta"],
        "owners": ["nic"],
        "entities": [],
        "evidence": [evidence(excerpt="owner words")],
        "supersedes": [],
        "dedupe_key": None,
        "data": v2_kind_data(kind),
        "body": "Body text",
        "source": v2_source(),
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("kind", sorted(records.RECORD_KINDS_V2 - {"continuity", "entity", "preference"}))
def test_v2_kinds_round_trip_as_deterministic_markdown(kind: str) -> None:
    record = record_from_mapping(v2_mapping(kind), kind)
    text = render_record(record)
    loaded = parse_record(text, kind)
    assert loaded == record
    assert render_record(loaded) == text
    assert record.assertion is None
    assert record.confidence is None
    assert "assertion" not in text
    assert "confidence" not in text


def test_v2_item_with_links_round_trips_and_renders_links() -> None:
    value = v2_mapping(
        "item",
        links=[{"type": "waiting_on", "target": "brn_20260101_000000_aaaa"}, {"type": "about", "target": "gt:request:1"}],
    )
    record = record_from_mapping(value, "item")
    assert record.links == ({"type": "waiting_on", "target": "brn_20260101_000000_aaaa"}, {"type": "about", "target": "gt:request:1"})
    text = render_record(record)
    assert "links" in text
    loaded = parse_record(text, "item")
    assert loaded.links == record.links


def test_v2_item_without_links_omits_links_from_output() -> None:
    record = record_from_mapping(v2_mapping("item"), "item")
    assert record.links == ()
    assert "links" not in render_record(record)


def test_v1_rejects_a_v2_only_kind() -> None:
    value = mapping("decision")
    value["kind"] = "item"
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value)
    assert raised.value.details["field"] == "kind"


def test_v2_rejects_a_v1_only_kind() -> None:
    value = v2_mapping("item")
    value["kind"] = "capture"
    value["data"] = {"channel": "cli", "original_source_hash": HASH, "route": None, "duplicate_of": None}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value)
    assert raised.value.details["field"] == "kind"


def test_v1_forbids_a_source_field() -> None:
    value = mapping("decision")
    value["source"] = v2_source()
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value)
    assert raised.value.details["field"] == "source"


@pytest.mark.parametrize("field", ["assertion", "confidence"])
def test_v2_forbids_assertion_and_confidence(field: str) -> None:
    value = v2_mapping("item")
    value[field] = "explicit" if field == "assertion" else 1.0
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value)
    assert raised.value.details["field"] == field


@pytest.mark.parametrize(
    ("data_change", "field"),
    [
        ({"level": "maybe"}, "data.level"),
        ({"size": "epic"}, "data.size"),
        ({"owed_to": 1}, "data.owed_to"),
        ({"repeat": 1}, "data.repeat"),
        ({"reason": 1}, "data.reason"),
        ({"unexpected": True}, "data"),
        ({"aliases": "clanker"}, "data.aliases"),
        ({"aliases": [1]}, "data.aliases[]"),
        ({"aliases": [""]}, "data.aliases[]"),
        ({"aliases": ["clanker", "clanker"]}, "data.aliases"),
        ({"aliases": [f"alias-{i}" for i in range(6)]}, "data.aliases"),
        ({"aliases": ["x" * 81]}, "data.aliases[]"),
    ],
)
def test_v2_item_data_contract_failures(data_change: dict[str, object], field: str) -> None:
    data = dict(v2_kind_data("item"))
    data.update(data_change)
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data))
    assert raised.value.details["field"] == field


def test_v2_item_owed_to_and_repeat_accept_strings() -> None:
    data = {"level": "accepted", "size": "task", "owed_to": "electricity company", "repeat": "weekly:sun"}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert record.data["owed_to"] == "electricity company"
    assert record.data["repeat"] == "weekly:sun"


def test_v2_item_reason_is_validated_even_when_not_required() -> None:
    data = {"level": "accepted", "size": "task", "reason": "keeping notes"}
    record = record_from_mapping(v2_mapping("item", data=data, state="open"))
    assert record.data["reason"] == "keeping notes"


def test_v2_item_dropped_requires_a_reason() -> None:
    data = {"level": "accepted", "size": "task"}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data, state="dropped"))
    assert raised.value.details["field"] == "data.reason"


@pytest.mark.parametrize(
    ("timing", "field"),
    [
        ({"due": "not-a-date"}, "data.timing.due"),
        ({"at": "19:00"}, "data.timing.at"),
        ({"due": "2026-10-02", "at": "25:00"}, "data.timing.at"),
        ({"due": "2026-10-02", "at": "19:60"}, "data.timing.at"),
        ({"due": "2026-10-02", "at": "7pm"}, "data.timing.at"),
        ({"window": {"start": "bad", "end": "2026-10-02"}}, "data.timing.window.start"),
        ({"window": {"start": "2026-10-01", "end": "2026-10-02", "extra": 1}}, "data.timing.window"),
        ({"on_event": 1}, "data.timing.on_event"),
        ({"on_return": 1}, "data.timing.on_return"),
        ({"duration_minutes": True}, "data.timing.duration_minutes"),
        ({"duration_minutes": 0}, "data.timing.duration_minutes"),
        ({"duration_minutes": 1.5}, "data.timing.duration_minutes"),
        ({"padded_minutes": -1}, "data.timing.padded_minutes"),
        ({"resolved_from": {"words": "Friday", "today": "2026-09-26", "extra": 1}}, "data.timing.resolved_from"),
        ({"resolved_from": {"today": "2026-09-26"}}, "data.timing.resolved_from.words"),
        ({"resolved_from": {"words": "Friday", "today": "not-a-date"}}, "data.timing.resolved_from.today"),
        ({"unexpected": 1}, "data.timing"),
    ],
)
def test_v2_item_timing_contract_failures(timing: dict[str, object], field: str) -> None:
    data = {"level": "accepted", "size": "task", "timing": timing}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data))
    assert raised.value.details["field"] == field


def test_v2_item_timing_accepts_at_beside_due() -> None:
    # P2 (docs/product/steps/c-record-format.md §5): a time of day next to a date.
    data = {"level": "accepted", "size": "task", "timing": {"due": "2026-10-02", "at": "19:00"}}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert record.data["timing"] == {"due": "2026-10-02", "at": "19:00"}


def test_v2_item_timing_valid_window_and_resolved_from() -> None:
    data = {
        "level": "accepted",
        "size": "task",
        "timing": {
            "window": {"start": "2026-10-01", "end": "2026-10-02"},
            "duration_minutes": 30,
            "padded_minutes": 45,
            "resolved_from": {"words": "owner words", "today": "2026-09-26"},
        },
    }
    record = record_from_mapping(v2_mapping("item", data=data))
    assert record.data["timing"]["window"] == {"start": "2026-10-01", "end": "2026-10-02"}
    assert record.data["timing"]["resolved_from"]["words"] == "owner words"


def test_v2_item_check_structure_validated_even_when_not_done() -> None:
    data = {"level": "accepted", "size": "task", "check": {"locator": "gt:request:1", "checked_by": "damien"}}
    record = record_from_mapping(v2_mapping("item", data=data, state="open"))
    assert record.data["check"] == {"locator": "gt:request:1", "checked_by": "damien"}


@pytest.mark.parametrize(
    ("check", "field"),
    [
        (None, "data.check"),
        ({"locator": "x"}, "data.check.checked_by"),
        ({"locator": "x", "checked_by": "y", "extra": 1}, "data.check"),
    ],
)
def test_v2_item_done_requires_a_valid_check(check: object, field: str) -> None:
    data = {"level": "accepted", "size": "task", "check": check}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data, state="done"))
    assert raised.value.details["field"] == field


def test_v2_item_parked_requires_parked_until() -> None:
    data = {"level": "accepted", "size": "task"}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data, state="parked"))
    assert raised.value.details["field"] == "data.parked_until"


@pytest.mark.parametrize(
    "parked_until",
    ["2026-10-10", {"event": "the next review"}],
)
def test_v2_item_parked_until_accepts_a_date_or_an_event(parked_until: object) -> None:
    data = {"level": "accepted", "size": "task", "parked_until": parked_until}
    record = record_from_mapping(v2_mapping("item", data=data, state="parked"))
    assert record.data["parked_until"] == parked_until


def test_v2_item_parked_until_is_validated_even_when_not_parked() -> None:
    data = {"level": "accepted", "size": "task", "parked_until": {"event": "the next review", "extra": 1}}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data, state="open"))
    assert raised.value.details["field"] == "data.parked_until"


def test_v2_item_accepts_aliases() -> None:
    # Proposed - Nic approves before release speed-02 goes live
    # (docs/product/2026-09-26-gtbc-speed-goal.md Wave 3).
    data = {"level": "accepted", "size": "task", "aliases": ["Clanker", "token idea"]}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert record.data["aliases"] == ["Clanker", "token idea"]


def test_v2_item_aliases_accepts_the_boundary_count_and_length() -> None:
    at_the_limit = {"level": "accepted", "size": "task", "aliases": [f"a{i}" for i in range(5)]}
    record = record_from_mapping(v2_mapping("item", data=at_the_limit))
    assert len(record.data["aliases"]) == 5
    longest = {"level": "accepted", "size": "task", "aliases": ["x" * 80]}
    long_record = record_from_mapping(v2_mapping("item", data=longest))
    assert len(long_record.data["aliases"][0]) == 80


def test_v2_item_without_aliases_omits_the_field() -> None:
    # No save tool sets data.aliases yet (Wave 3): a record that never
    # specifies it must keep exactly today's data shape, with no key added,
    # so the exact-input identity check in the hosted provider is unaffected.
    record = record_from_mapping(v2_mapping("item", data=dict(v2_kind_data("item"))))
    assert "aliases" not in record.data


@pytest.mark.parametrize(
    ("data_change", "field"),
    [
        ({"observed_at": None}, "data.observed_at"),
        ({"observed_at": "not-a-time"}, "data.observed_at"),
        ({"valid_until": "not-a-time"}, "data.valid_until"),
        ({"unexpected": 1}, "data"),
        ({"aliases": "clanker"}, "data.aliases"),
        ({"aliases": [1]}, "data.aliases[]"),
        ({"aliases": ["clanker", "clanker"]}, "data.aliases"),
        ({"aliases": [f"alias-{i}" for i in range(6)]}, "data.aliases"),
        ({"aliases": ["x" * 81]}, "data.aliases[]"),
    ],
)
def test_v2_fact_contract_failures(data_change: dict[str, object], field: str) -> None:
    data = dict(v2_kind_data("fact"))
    data.update(data_change)
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("fact", data=data))
    assert raised.value.details["field"] == field


def test_v2_fact_accepts_a_valid_until() -> None:
    later = (NOW + timedelta(days=90)).isoformat()
    data = {"observed_at": NOW.isoformat(), "valid_until": later}
    record = record_from_mapping(v2_mapping("fact", data=data))
    assert record.data["valid_until"] == later


def test_v2_fact_accepts_aliases() -> None:
    # Proposed - Nic approves before release speed-02 goes live
    # (docs/product/2026-09-26-gtbc-speed-goal.md Wave 3).
    data = {"observed_at": NOW.isoformat(), "aliases": ["Clanker"]}
    record = record_from_mapping(v2_mapping("fact", data=data))
    assert record.data["aliases"] == ["Clanker"]


def test_v2_fact_without_aliases_omits_the_field() -> None:
    record = record_from_mapping(v2_mapping("fact", data=dict(v2_kind_data("fact"))))
    assert "aliases" not in record.data


@pytest.mark.parametrize(
    ("source_change", "field"),
    [
        ({"said_by": "nobody"}, "source.said_by"),
        ({"came_by": "nowhere"}, "source.came_by"),
        ({"said_by": "owner", "came_by": "agent_guess"}, "source.came_by"),
        ({"said_by": "agent", "speaker": None}, "source.speaker"),
        ({"said_by": "other", "speaker": None}, "source.speaker"),
        ({"came_by": "direct", "words": None}, "source.words"),
        ({"recorded_by": None}, "source.recorded_by"),
        ({"unexpected": True}, "source"),
    ],
)
def test_v2_source_contract_failures(source_change: dict[str, object], field: str) -> None:
    value = v2_mapping("item")
    value["source"] = v2_source(**source_change)
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value)
    assert raised.value.details["field"] == field


def test_v2_source_speaker_optional_for_owner_but_validated() -> None:
    value = v2_mapping("item")
    value["source"] = v2_source(speaker="fleet-boss")
    record = record_from_mapping(value)
    assert record.source["speaker"] == "fleet-boss"

    value["source"] = v2_source(speaker=1)
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value)
    assert raised.value.details["field"] == "source.speaker"


def test_v2_source_words_optional_when_came_by_is_system() -> None:
    value = v2_mapping("item")
    value["source"] = {"said_by": "agent", "came_by": "system", "speaker": "cron", "recorded_by": "chatgpt-web"}
    record = record_from_mapping(value)
    assert "words" not in record.source

    value["source"] = {
        "said_by": "agent",
        "came_by": "system",
        "speaker": "cron",
        "words": "owner words",
        "recorded_by": "chatgpt-web",
    }
    record = record_from_mapping(value)
    assert record.source["words"] == "owner words"


def test_v2_source_words_must_equal_the_last_evidence_excerpt() -> None:
    value = v2_mapping("item")
    value["source"] = v2_source(words="something else entirely")
    with pytest.raises(BrainError) as raised:
        record_from_mapping(value)
    assert raised.value.details["field"] == "source.words"


def test_revise_rejects_v2_only_parameters_for_a_v1_record(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(mapping("preference"))
    store.create(record)
    with pytest.raises(BrainError) as raised:
        store.revise(record.id, now=NOW + timedelta(seconds=1), state="active", expected_sha256="a" * 64)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_revise_v2_requires_expected_sha256_actor_and_source(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    record = record_from_mapping(v2_mapping("item"))
    store.create(record)
    with pytest.raises(BrainError) as raised:
        store.revise(record.id, now=NOW + timedelta(seconds=1), state="waiting")
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_v2_links_contract_failures() -> None:
    for bad_links, field in (
        ("not-a-list", "links"),
        ([{"type": "about"}], "links[].target"),
        ([{"type": "unknown", "target": "x"}], "links[].type"),
        ([{"type": "about", "target": "x", "extra": 1}], "links[]"),
    ):
        value = v2_mapping("item")
        value["links"] = bad_links
        with pytest.raises(BrainError) as raised:
            record_from_mapping(value)
        assert raised.value.details["field"] == field


# ---------------------------------------------------------------------------
# Ciel rebuild slice 3 (docs/product/steps/c-record-format.md §13): an item's importance
# (N5) and a preference's focus projects (N4).
# ---------------------------------------------------------------------------


def importance(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "level": "high",
        "reason": "the visa lapses if it slips",
        "proposed_by": "chatgpt-web",
        "confirmed": False,
    }
    value.update(changes)
    return value


def v2_preference_mapping(data: dict[str, object]) -> dict[str, object]:
    value = v2_mapping("item")
    value.update(kind="preference", state="active", data=data)
    return value


def preference_data(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {"scope": "personal", "stability": "durable", "evidence": [], "correction_history": []}
    value.update(changes)
    return value


@pytest.mark.parametrize("confirmed", [False, True])
def test_v2_item_importance_round_trips(confirmed: bool) -> None:
    data = {"level": "accepted", "size": "task", "importance": importance(confirmed=confirmed)}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert record.data["importance"] == importance(confirmed=confirmed)
    text = render_record(record)
    loaded = parse_record(text, "item")
    assert loaded == record
    assert render_record(loaded) == text


def test_v2_item_without_importance_omits_the_field() -> None:
    # No field means normal: a record that never sets it keeps exactly today's shape.
    record = record_from_mapping(v2_mapping("item", data=dict(v2_kind_data("item"))))
    assert "importance" not in record.data
    assert "importance" not in render_record(record)


def test_v2_item_importance_none_means_normal_and_is_not_stored() -> None:
    data = {"level": "accepted", "size": "task", "importance": None}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert "importance" not in record.data


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ("high", "data.importance"),
        (importance(level="medium"), "data.importance.level"),
        (importance(level="normal"), "data.importance.level"),
        (importance(reason=""), "data.importance.reason"),
        (importance(reason=None), "data.importance.reason"),
        (importance(proposed_by=" "), "data.importance.proposed_by"),
        (importance(confirmed="yes"), "data.importance.confirmed"),
        (importance(confirmed=None), "data.importance.confirmed"),
        (importance(confirmed=1), "data.importance.confirmed"),
        (importance(extra=True), "data.importance"),
        ({k: v for k, v in importance().items() if k != "proposed_by"}, "data.importance.proposed_by"),
    ],
)
def test_v2_item_importance_contract_failures(value: object, field: str) -> None:
    data = {"level": "accepted", "size": "task", "importance": value}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data))
    assert raised.value.details["field"] == field


def test_v2_fact_refuses_importance() -> None:
    data = {"observed_at": NOW.isoformat(), "importance": importance()}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("fact", data=data))
    assert raised.value.details["field"] == "data"


@pytest.mark.parametrize(
    "projects",
    [[], ["vault_exosuit"], ["vault_exosuit", "job search"], [f"p{i}" for i in range(10)], ["x" * 128]],
)
def test_preference_focus_projects_round_trip(projects: list[str]) -> None:
    record = record_from_mapping(v2_preference_mapping(preference_data(focus_projects=projects)))
    assert record.data["focus_projects"] == projects
    text = render_record(record)
    assert parse_record(text, "preference") == record


def test_preference_without_focus_projects_omits_the_field() -> None:
    record = record_from_mapping(v2_preference_mapping(preference_data()))
    assert "focus_projects" not in record.data


@pytest.mark.parametrize(
    ("projects", "field"),
    [
        (None, "data.focus_projects"),
        ("vault_exosuit", "data.focus_projects"),
        ([""], "data.focus_projects[]"),
        ([1], "data.focus_projects[]"),
        (["vault_exosuit", "vault_exosuit"], "data.focus_projects"),
        ([f"p{i}" for i in range(11)], "data.focus_projects"),
        (["x" * 129], "data.focus_projects[]"),
    ],
)
def test_preference_focus_projects_contract_failures(projects: object, field: str) -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_preference_mapping(preference_data(focus_projects=projects)))
    assert raised.value.details["field"] == field


def test_v2_item_refuses_focus_projects() -> None:
    data = {"level": "accepted", "size": "task", "focus_projects": ["vault_exosuit"]}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data))
    assert raised.value.details["field"] == "data"


def _words_evidence(words: str) -> Evidence:
    return Evidence(
        source_type="session_message",
        locator="session:test:2",
        content_sha256=HASH,
        excerpt=words,
        session_id="test",
        actor="chatgpt-web",
    )


def test_revise_proposes_then_confirms_importance_only_in_nics_words(tmp_path: Path) -> None:
    """Slice 3's done-when, in the local store: a proposal saves unconfirmed, an agent can't
    confirm it, Nic's yes does, and an agent can't quietly undo what he confirmed."""

    store = RecordStore(tmp_path)
    created = record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task"}))
    receipt = store.create(created)
    agent = v2_source(said_by="agent", came_by="agent_guess", speaker="chatgpt-web", words="it matters")

    proposed, proposed_receipt = store.revise(
        created.id,
        now=NOW + timedelta(seconds=1),
        data={"importance": importance()},
        evidence=(_words_evidence("it matters"),),
        entropy="b001",
        expected_sha256=receipt.content_sha256,
        actor="chatgpt-web",
        source=agent,
    )
    assert proposed.data["importance"]["confirmed"] is False

    before = sorted(path.read_bytes() for path in tmp_path.rglob("*.md"))
    with pytest.raises(BrainError) as raised:
        store.revise(
            proposed.id,
            now=NOW + timedelta(seconds=2),
            data={"importance": importance(confirmed=True)},
            evidence=(_words_evidence("it matters"),),
            entropy="b002",
            expected_sha256=proposed_receipt.content_sha256,
            actor="chatgpt-web",
            source=agent,
        )
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS
    assert sorted(path.read_bytes() for path in tmp_path.rglob("*.md")) == before

    confirmed, confirmed_receipt = store.revise(
        proposed.id,
        now=NOW + timedelta(seconds=3),
        data={"importance": importance(confirmed=True)},
        evidence=(_words_evidence("yes"),),
        entropy="b003",
        expected_sha256=proposed_receipt.content_sha256,
        actor="damien",
        source=v2_source(words="yes", recorded_by="damien"),
    )
    assert confirmed.data["importance"] == importance(confirmed=True)  # proposer kept: chatgpt-web

    with pytest.raises(BrainError) as raised:
        store.revise(
            confirmed.id,
            now=NOW + timedelta(seconds=4),
            data={"importance": None},
            evidence=(_words_evidence("it matters"),),
            entropy="b004",
            expected_sha256=confirmed_receipt.content_sha256,
            actor="chatgpt-web",
            source=agent,
        )
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS

    normal, _ = store.revise(
        confirmed.id,
        now=NOW + timedelta(seconds=5),
        data={"importance": None},
        evidence=(_words_evidence("it is not that important"),),
        entropy="b005",
        expected_sha256=confirmed_receipt.content_sha256,
        actor="chatgpt-web",
        source=v2_source(words="it is not that important"),
    )
    assert "importance" not in normal.data


# Release ciel-03 slice 1 (Ciel and Brain Home PRD §3; ROA-75): the guessed stake levels, the block
# type, who to raise it with, and a rough length that names who estimated it.


@pytest.mark.parametrize("level", ["real_cost", "minor"])
def test_v2_item_takes_the_guessed_stake_levels(level: str) -> None:
    data = {"level": "accepted", "size": "task", "importance": importance(level=level)}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert record.data["importance"]["level"] == level
    assert parse_record(render_record(record), "item") == record


def test_v2_item_block_raise_with_and_rough_length_round_trip() -> None:
    data = {
        "level": "accepted",
        "size": "task",
        "block": "admin",
        "raise_with": "Sam",
        "timing": {"duration_minutes": 20, "padded_minutes": 30, "estimated_by": "chatgpt-web", "start_by": "2026-10-01"},
    }
    record = record_from_mapping(v2_mapping("item", data=data))
    assert (record.data["block"], record.data["raise_with"]) == ("admin", "Sam")
    assert record.data["timing"]["estimated_by"] == "chatgpt-web"
    assert parse_record(render_record(record), "item") == record


def test_v2_item_without_the_sorting_fields_keeps_todays_shape() -> None:
    record = record_from_mapping(v2_mapping("item", data=dict(v2_kind_data("item"))))
    text = render_record(record)
    assert "block" not in record.data and "raise_with" not in record.data
    assert "block:" not in text and "raise_with" not in text


def test_v2_item_none_block_and_raise_with_are_not_stored() -> None:
    data = {"level": "accepted", "size": "task", "block": None, "raise_with": None}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert "block" not in record.data and "raise_with" not in record.data


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"block": "deep"}, "data.block"),
        ({"raise_with": "x" * 129}, "data.raise_with"),
        ({"timing": {"estimated_by": "chatgpt-web"}}, "data.timing.estimated_by"),
        ({"timing": {"start_by": "Friday"}}, "data.timing.start_by"),
    ],
)
def test_v2_item_sorting_field_contract_failures(data: dict[str, object], field: str) -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task", **data}))
    assert raised.value.details["field"] == field


def test_v2_fact_refuses_a_block() -> None:
    data = {"observed_at": NOW.isoformat(), "block": "admin"}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("fact", data=data))
    assert raised.value.details["field"] == "data"


# Ciel and Brain Home PRD §4.1 (build plan slice 4): a next action's first step, one line.


def test_v2_item_first_step_round_trips_and_is_left_out_when_absent() -> None:
    data = {"level": "accepted", "size": "task", "block": "admin", "first_step": "Find the accountant's number"}
    record = record_from_mapping(v2_mapping("item", data=data))
    assert record.data["first_step"] == "Find the accountant's number"
    assert parse_record(render_record(record), "item") == record
    bare = record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task", "first_step": None}))
    assert "first_step" not in bare.data


@pytest.mark.parametrize("step", ["x" * 257, "two\nlines", "carriage\rreturn", "", 42])
def test_v2_item_first_step_is_one_line_of_at_most_256_characters(step: object) -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task", "first_step": step}))
    assert raised.value.details["field"] == "data.first_step"


def test_first_saved_is_the_earliest_version_the_ids_name() -> None:
    record = record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task"}))
    assert first_saved(record) == record.created_at
    older = replace(record, supersedes=("brn_20260101_000000_abcd", "not-a-record-id"))
    assert first_saved(older) == datetime(2026, 1, 1, tzinfo=UTC)


# Step I (placement engine; PRD §4.3, Accepted - Nic, 29 Sep 2026): Nic's working window.
WEEKDAYS_9_TO_6 = {day: {"start": "09:00", "end": "18:00"} for day in ("mon", "tue", "wed", "thu", "fri")}


@pytest.mark.parametrize(
    "window",
    [
        {**WEEKDAYS_9_TO_6, "sat": None, "sun": None},
        WEEKDAYS_9_TO_6,
        {"sat": {"start": "10:00", "end": "12:30"}},
        {},
    ],
)
def test_preference_working_window_round_trip(window: dict[str, object]) -> None:
    record = record_from_mapping(v2_preference_mapping(preference_data(working_window=window)))
    assert record.data["working_window"] == window
    assert parse_record(render_record(record), "preference") == record


def test_preference_without_working_window_omits_the_field() -> None:
    record = record_from_mapping(v2_preference_mapping(preference_data()))
    assert "working_window" not in record.data


@pytest.mark.parametrize(
    ("window", "field"),
    [
        (None, "data.working_window"),
        ("weekdays 9 to 6", "data.working_window"),
        ({"monday": None}, "data.working_window"),
        ({"mon": "09:00-18:00"}, "data.working_window.mon"),
        ({"mon": {"start": "09:00"}}, "data.working_window.mon.end"),
        ({"mon": {"start": "9am", "end": "18:00"}}, "data.working_window.mon.start"),
        ({"mon": {"start": "09:00", "end": "18:00", "lunch": "12:00"}}, "data.working_window.mon"),
        ({"mon": {"start": "18:00", "end": "09:00"}}, "data.working_window.mon"),
        ({"mon": {"start": "09:00", "end": "09:00"}}, "data.working_window.mon"),
    ],
)
def test_preference_working_window_contract_failures(window: object, field: str) -> None:
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_preference_mapping(preference_data(working_window=window)))
    assert raised.value.details["field"] == field


def test_v2_item_refuses_a_working_window() -> None:
    data = {"level": "accepted", "size": "task", "working_window": WEEKDAYS_9_TO_6}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data=data))
    assert raised.value.details["field"] == "data"


# Step I §6 (Nic, 29 Sep 2026: "dont pad my responses just take them").
def test_nics_own_length_round_trips_unpadded() -> None:
    timing = {"duration_minutes": 60, "padded_minutes": 60, "estimated_by": records.OWNER_ESTIMATE}
    record = record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task", "timing": timing}))
    assert record.data["timing"]["padded_minutes"] == 60
    assert parse_record(render_record(record), "item") == record


def test_nics_own_length_refuses_padding() -> None:
    timing = {"duration_minutes": 60, "padded_minutes": 90, "estimated_by": records.OWNER_ESTIMATE}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task", "timing": timing}))
    assert raised.value.details["field"] == "data.timing.padded_minutes"


def test_an_agents_guess_may_still_be_padded() -> None:
    timing = {"duration_minutes": 60, "padded_minutes": 90, "estimated_by": "chatgpt-web"}
    record = record_from_mapping(v2_mapping("item", data={"level": "accepted", "size": "task", "timing": timing}))
    assert record.data["timing"]["padded_minutes"] == 90


# Work-01 parser contract (accepted 30 Sep). All fields absent means the old bytes.
WORK_ID = "brn_20260828_010203_c3d4"
WORK_DATE = {"words": "by Friday", "read_by": "code", "read_on": "2026-08-29", "day": "2026-09-04"}
WORK_DATA = {
    "parent": WORK_ID, "order": 1, "called": "project",
    "next_action": {"text": "open the form", "done": False}, "whose": "Sam",
    "expected": WORK_DATE, "after": [WORK_ID],
    "needs": {"from": "Sam", "what": "the form", "expected": WORK_DATE},
    "dates": {"deadline": WORK_DATE, "start_by": WORK_DATE},
    "finish_line": "the form is sent", "labels": ["exhibition admin"], "started_at": NOW.isoformat(),
    "stake": {"level": "changes_life", "at_stake": "40 display slots a month", "odds": "likely",
              "cost_shape": "grows", "confirmed": True, "proposed_by": "chatgpt-web"},
    "review_marks": [{"mark": "agents_wording", "part": "title", "as": WORK_ID, "text": "is this right?"},
                     {"mark": "looks_like", "kind": "goal"}, {"mark": "ask_stake"},
                     {"mark": "spelling", "text": "resaerch"}],
    "words_from": "chat",
}


@pytest.mark.parametrize("field", sorted(WORK_DATA))
def test_each_work_field_saves_and_reads_back(field, tmp_path):
    value = deepcopy(WORK_DATA[field])
    record = record_from_mapping(v2_mapping(data={"level": "possible", field: value}))
    store = RecordStore(tmp_path)
    receipt = store.create(record)
    assert receipt.created and store.get(record.id).data[field] == value
    assert parse_record(render_record(record)) == record


def test_untouched_item_bytes_match_frozen_w0():
    # Independently measured with records.py from 680f1e1, before the W1 changes.
    assert hashlib.sha256(render_record(record_from_mapping(v2_mapping())).encode()).hexdigest() == (
        "2189ea9040a30bcd743091d88c56c15ac27c0b6343e4bbe371535786314dfb93"
    )


@pytest.mark.parametrize(("change", "field"), [
    ({"parent": "not-an-id"}, "data.parent"), ({"order": 0}, "data.order"),
    ({"order": 1000}, "data.order"), ({"order": True}, "data.order"), ({"order": 1.5}, "data.order"),
    ({"called": "task"}, "data.called"), ({"next_action": []}, "data.next_action"),
    ({"next_action": {"text": "", "done": False}}, "data.next_action.text"),
    ({"next_action": {"text": "x" * 257, "done": False}}, "data.next_action.text"),
    ({"next_action": {"text": "two\nlines", "done": False}}, "data.next_action.text"),
    ({"next_action": {"text": "one", "done": 1}}, "data.next_action.done"),
    ({"next_action": {"text": "one", "done": False, "extra": 1}}, "data.next_action"),
    ({"whose": "x" * 129}, "data.whose"), ({"whose": " "}, "data.whose"),
    ({"expected": {}}, "data.expected.words"), ({"after": []}, "data.after"),
    ({"after": [WORK_ID] * 2}, "data.after"), ({"after": ["bad"]}, "data.after[]"),
    ({"after": [f"brn_20260828_010203_{i:04x}" for i in range(21)]}, "data.after"),
    ({"needs": {}}, "data.needs.from"), ({"needs": {"from": "x" * 129}}, "data.needs.from"),
    ({"needs": {"from": "Sam", "what": "x" * 257}}, "data.needs.what"),
    ({"needs": {"from": "Sam", "extra": 1}}, "data.needs"),
    ({"dates": {"unknown": WORK_DATE}}, "data.dates"),
    ({"dates": {"start_by": WORK_DATE}}, "data.dates.start_by"),
    ({"finish_line": "x" * 257}, "data.finish_line"),
    ({"labels": []}, "data.labels"), ({"labels": list("abcdef")}, "data.labels"),
    ({"labels": ["same", "same"]}, "data.labels"), ({"labels": ["x" * 65]}, "data.labels[]"),
    ({"labels": "exhibition admin"}, "data.labels"), ({"labels": [""]}, "data.labels[]"),
    ({"started_at": "today"}, "data.started_at"), ({"started_at": "2026-09-30T09:00:00"}, "data.started_at"),
    ({"stake": {"confirmed": 1, "proposed_by": "chatgpt-web"}}, "data.stake.confirmed"),
    ({"stake": {"confirmed": False}}, "data.stake.proposed_by"),
    ({"stake": {"confirmed": False, "proposed_by": "x", "level": "high"}}, "data.stake.level"),
    ({"stake": {"confirmed": False, "proposed_by": "x", "odds": 0.5}}, "data.stake.odds"),
    ({"stake": {"confirmed": False, "proposed_by": "x", "at_stake": ""}}, "data.stake.at_stake"),
    ({"stake": {"confirmed": False, "proposed_by": "x", "cost_shape": "flat"}}, "data.stake.cost_shape"),
    ({"stake": {"confirmed": False, "proposed_by": "x", "extra": 1}}, "data.stake"),
    ({"review_marks": [{"mark": "question"}] * 11}, "data.review_marks"),
    ({"review_marks": {}}, "data.review_marks"),
    ({"review_marks": [{"mark": "unknown"}]}, "data.review_marks[].mark"),
    ({"review_marks": [{"mark": "looks_like"}]}, "data.review_marks[].kind"),
    ({"review_marks": [{"mark": "spelling"}]}, "data.review_marks[].text"),
    ({"review_marks": [{"mark": "question", "part": 1}]}, "data.review_marks[].part"),
    ({"review_marks": [{"mark": "question", "as": "bad"}]}, "data.review_marks[].as"),
    ({"words_from": "owner"}, "data.words_from"),
])
def test_bad_work_shape_names_its_field(change, field):
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(data={"level": "possible", **deepcopy(change)}))
    assert raised.value.code == ErrorCode.CONTRACT_INVALID
    assert raised.value.details["field"] == field


@pytest.mark.parametrize("field", sorted(WORK_DATA))
def test_work_fields_cannot_be_null(field):
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(data={"level": "possible", field: None}))
    assert raised.value.details["field"].startswith(f"data.{field}")


@pytest.mark.parametrize(("kind", "extra"), [
    ("deadline", {"day": "2026-10-01"}), ("appointment", {"day": "2026-10-01", "at": "12:00"}),
    ("planned", {"day": "2026-10-01"}), ("planned", {"start": "2026-10-01", "end": "2026-10-03"}),
    ("not_before", {"event": "Sam replies"}), ("not_before", {"day": "2026-10-01"}),
    ("suggested", {"kind": "appointment", "day": "2026-10-01", "speaker": "Sam"}),
    ("done", {"day": "2026-09-29"}),
])
def test_each_work_date_kind_reads(kind, extra):
    entry = {"words": "Friday", "read_by": "system:lark-bridge", "read_on": "2026-09-30", **extra}
    record = record_from_mapping(v2_mapping(data={"level": "possible", "dates": {kind: entry}}))
    assert parse_record(render_record(record)).data["dates"][kind] == entry


@pytest.mark.parametrize("kind", ["deadline", "done"])
@pytest.mark.parametrize(("entry", "field"), [
    ({**WORK_DATE, "words": "x" * 257}, "words"), ({**WORK_DATE, "read_by": "system:"}, "read_by"),
    ({**WORK_DATE, "read_on": "today"}, "read_on"), ({**WORK_DATE, "day": "tomorrow"}, "day"),
    ({**WORK_DATE, "at": "24:00"}, "at"), ({**WORK_DATE, "start": 1}, "start"),
    ({**WORK_DATE, "end": 1}, "end"), ({**WORK_DATE, "event": ""}, "event"),
    ({**WORK_DATE, "speaker": ""}, "speaker"), ({**WORK_DATE, "kind": "expected"}, "kind"),
    ({**WORK_DATE, "confirmed": 1}, "confirmed"), ({**WORK_DATE, "unknown": 1}, None),
])
def test_bad_date_keys_name_the_field(kind, entry, field):
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(data={"level": "possible", "dates": {kind: entry}}))
    assert raised.value.details["field"] == f"data.dates.{kind}" + (f".{field}" if field else "")


@pytest.mark.parametrize(("kind", "entry", "field"), [
    ("deadline", {"words": "Friday", "read_by": "code", "read_on": "2026-09-30"}, "day"),
    ("appointment", WORK_DATE, "at"),
    ("planned", {"words": "Friday", "read_by": "code", "read_on": "2026-09-30"}, "start"),
    ("not_before", {"words": "Friday", "read_by": "code", "read_on": "2026-09-30"}, None),
    ("suggested", WORK_DATE, "kind"),
    ("suggested", {**WORK_DATE, "kind": "planned"}, "speaker"),
])
def test_date_kind_requires_its_listed_keys(kind, entry, field):
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(data={"level": "possible", "dates": {kind: entry}}))
    assert raised.value.details["field"] == f"data.dates.{kind}" + (f".{field}" if field else "")


@pytest.mark.parametrize("field", ["words", "read_by", "read_on", "day"])
def test_done_date_requires_its_listed_keys(field):
    entry = {key: value for key, value in WORK_DATE.items() if key != field}
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(data={"level": "possible", "dates": {"done": entry}}))
    assert raised.value.details["field"] == f"data.dates.done.{field}"


def test_done_date_permits_all_declared_date_keys():
    entry = {**WORK_DATE, "at": "12:00", "start": "2026-09-01", "end": "2026-09-04",
             "event": "Sam replies", "kind": "planned", "speaker": "Nic", "confirmed": True}
    record = record_from_mapping(v2_mapping(data={"level": "possible", "dates": {"done": entry}}))
    assert parse_record(render_record(record)) == record


@pytest.mark.parametrize("state", ["open", "parked", "done", "dropped", "no_longer_needed"])
@pytest.mark.parametrize("level", sorted(records.ITEM_LEVELS))
def test_done_date_is_not_tied_to_item_state_or_level(state, level):
    data = {"level": level, "dates": {"done": WORK_DATE}}
    if state == "done":
        data["check"] = {"locator": "owner words", "checked_by": "code"}
    elif state == "parked":
        data["parked_until"] = "2026-10-01"
    elif state == "dropped":
        data["reason"] = "no longer wanted"
    record = record_from_mapping(v2_mapping(state=state, data=data))
    assert parse_record(render_record(record)) == record


@pytest.mark.parametrize(("old", "field"), [
    ({"size": "goal"}, "data.size"), ({"first_step": "old"}, "data.first_step"),
    ({"importance": {"level": "high", "confirmed": False, "proposed_by": "x"}}, "data.importance"),
    *[({"timing": {key: "old"}}, f"data.timing.{key}")
      for key in ("due", "at", "window", "on_event", "on_return", "resolved_from", "start_by")],
])
def test_work_and_old_only_data_never_mix(old, field):
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(data={"level": "possible", "labels": ["admin"], **old}))
    assert raised.value.details["field"] == field


@pytest.mark.parametrize("state", ["waiting", "blocked"])
def test_work_and_old_only_states_never_mix(state):
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(state=state, data={"level": "possible", "labels": ["admin"]}))
    assert raised.value.details["field"] == "state"


@pytest.mark.parametrize("link_type", ["part_of", "blocked_by", "waiting_on"])
def test_work_and_old_only_links_never_mix(link_type):
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(links=[{"type": link_type, "target": WORK_ID}], data={"level": "possible", "order": 1}))
    assert raised.value.details["field"] == "links[].type"


@pytest.mark.parametrize("field", ["parent", "after"])
@pytest.mark.parametrize("supersedes", [[], [WORK_ID]])
def test_work_cannot_depend_on_its_own_first_id(field, supersedes):
    own_first = supersedes[0] if supersedes else new_record_id(NOW, "a1b2")
    with pytest.raises(BrainError) as raised:
        record_from_mapping(v2_mapping(supersedes=supersedes, data={"level": "possible", field: own_first if field == "parent" else [own_first]}))
    assert raised.value.details["field"] == f"data.{field}"


def test_first_id_uses_the_first_not_latest_superseded_version():
    record = record_from_mapping(v2_mapping(supersedes=[WORK_ID, "brn_20260828_020203_e5f6"]))
    assert records.first_id(record) == WORK_ID
    assert records.first_id(record_from_mapping(v2_mapping())) == new_record_id(NOW, "a1b2")


def test_supervisor_r4_structure_only_boundary_and_kept_fields():
    data = deepcopy(WORK_DATA)
    data["stake"]["at_stake"] = "x" * 513
    data["stake"]["odds"] = "x" * 257
    data["review_marks"][0]["part"] = "x" * 129
    data["review_marks"][0]["text"] = "x" * 1025
    data["dates"]["deadline"].update(at="12:00", start="2026-09-01", end="2026-09-04", event="Sam replies", confirmed=True)
    data.update(level="possible", timing={"duration_minutes": 30, "padded_minutes": 45, "estimated_by": "agent"},
                owed_to="Sam", block="admin", raise_with="Sam", aliases=["form"], reason="needed")
    record = record_from_mapping(v2_mapping(data=data, links=[{"type": "about", "target": WORK_ID}]))
    assert parse_record(render_record(record)) == record


@pytest.mark.parametrize("mark", sorted(records.REVIEW_MARKS))
def test_all_review_marks_are_known(mark):
    entry = {"mark": mark}
    if mark == "looks_like":
        entry["kind"] = "project"
    if mark == "spelling":
        entry["text"] = "resaerch"
    record = record_from_mapping(v2_mapping(data={"level": "possible", "review_marks": [entry] * 10}))
    assert len(record.data["review_marks"]) == 10


@pytest.mark.parametrize("level", sorted(records.STAKE_LEVELS))
def test_each_stake_level_reads(level):
    record = record_from_mapping(v2_mapping(data={"level": "possible", "stake": {"level": level, "confirmed": False, "proposed_by": "agent"}}))
    assert record.data["stake"]["level"] == level


def test_work_text_list_and_id_limits_are_inclusive():
    data = {"level": "possible", "next_action": {"text": "x" * 256, "done": True}, "whose": "x" * 128,
            "needs": {"from": "x" * 128, "what": "x" * 256}, "finish_line": "x" * 256,
            "labels": [str(i) + "x" * 63 for i in range(5)], "order": 999,
            "after": [f"brn_20260828_010203_{i:04x}" for i in range(20)]}
    record = record_from_mapping(v2_mapping(data=data))
    assert parse_record(render_record(record)) == record
