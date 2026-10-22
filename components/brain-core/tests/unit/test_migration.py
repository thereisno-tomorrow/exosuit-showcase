from __future__ import annotations

from pathlib import Path

import pytest

import brain_core.migration as migration
from brain_core.errors import BrainError, ErrorCode
from brain_core.migration import decision_to_record, import_decisions, parse_decision_log, record_to_legacy_fields
from brain_core.records import RecordStore, record_from_mapping


def entry(decision_id: str = "20260829-test", *, because: str = "Evidence passed") -> str:
    return f"""- [2026-08-29] Decision: Test decision
  Decision-ID: {decision_id}
  Because: {because}
  Forecloses: Doing nothing
  Governs: Test behavior
  Encoded in: tests
  Completeness check: All fields round trip
  State: encoded
"""


def test_parse_convert_and_field_round_trip() -> None:
    decisions = parse_decision_log("# Decisions\n\n" + entry())
    assert len(decisions) == 1
    decision = decisions[0]
    record = decision_to_record(decision)
    assert record.kind == "decision"
    assert record.state == "accepted"
    assert record_to_legacy_fields(record) == decision.fields
    assert record.data["legacy_field_sha256"] == decision.field_sha256


def test_parser_supports_continuation_lines_and_ignores_preamble() -> None:
    text = entry(because="First line").replace("  Forecloses:", "    second line\n  Forecloses:")
    decision = parse_decision_log("intro\n" + text)[0]
    assert decision.fields["Because"] == "First line\nsecond line"
    same_level = entry(because="First line").replace("  Forecloses:", "  same-level continuation\n  Forecloses:")
    decision = parse_decision_log(same_level)[0]
    assert decision.fields["Because"] == "First line\nsame-level continuation"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (entry().replace("  State: encoded\n", ""), ErrorCode.MIGRATION_MISMATCH),
        (entry().replace("  State: encoded", "  Unknown: value"), ErrorCode.MIGRATION_MISMATCH),
        (entry().replace("  State: encoded", "unexpected"), ErrorCode.MIGRATION_MISMATCH),
        (entry().replace("  Because: Evidence passed", "  Because: one\n  Because: two"), ErrorCode.MIGRATION_MISMATCH),
        (entry() + "\n" + entry(), ErrorCode.DUPLICATE_ID),
    ],
)
def test_parser_refuses_incomplete_unexpected_duplicate_fields_and_ids(text: str, code: ErrorCode) -> None:
    with pytest.raises(BrainError) as raised:
        parse_decision_log(text)
    assert raised.value.code is code


def test_dry_run_and_live_import_are_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "decisions.md"
    source.write_text(entry() + "\n" + entry("20260829-second"), encoding="utf-8")
    dry = import_decisions(source)
    assert dry.dry_run and dry.count == 2 and dry.created == 0
    assert dry.as_dict()["decision_ids"] == ["20260829-test", "20260829-second"]
    store_root = tmp_path / "home"
    store_root.mkdir()
    store = RecordStore(store_root)
    live = import_decisions(source, store, dry_run=False)
    repeated = import_decisions(source, store, dry_run=False)
    assert live.created == 2
    assert repeated.created == 0
    assert len(store.current(kind="decision")) == 2


def test_live_import_requires_store(tmp_path: Path) -> None:
    source = tmp_path / "decisions.md"
    source.write_text(entry(), encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        import_decisions(source, dry_run=False)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_nonimported_record_cannot_render_as_legacy() -> None:
    value = {
        "schema": "brain.record/v1",
        "id": "brn_20260829_000000_abcd",
        "kind": "decision",
        "title": "Native",
        "state": "accepted",
        "created_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:00:00+00:00",
        "assertion": "explicit",
        "confidence": 1,
        "sensitivity": "private",
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [{"source_type": "message", "locator": "m", "content_sha256": "a" * 64}],
        "supersedes": [],
        "dedupe_key": None,
        "data": {
            "because": "x",
            "forecloses": "x",
            "governs": "x",
            "encoded_in": "x",
            "completeness_check": "x",
        },
        "body": "",
    }
    with pytest.raises(BrainError) as raised:
        record_to_legacy_fields(record_from_mapping(value))
    assert raised.value.code is ErrorCode.MIGRATION_MISMATCH


def test_import_detects_round_trip_mismatch(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "decisions.md"
    source.write_text(entry(), encoding="utf-8")
    monkeypatch.setattr(migration, "record_to_legacy_fields", lambda _record: {})
    with pytest.raises(BrainError) as raised:
        import_decisions(source)
    assert raised.value.code is ErrorCode.MIGRATION_MISMATCH
