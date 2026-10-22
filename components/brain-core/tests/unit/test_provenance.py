from __future__ import annotations

import json
from pathlib import Path

import pytest

import brain_core.provenance as provenance
from brain_core.contracts import (
    ExclusionReceipt,
    SourceReceipt,
    canonical_json,
    stable_hash,
)
from brain_core.errors import BrainError, ErrorCode
from brain_core.provenance import (
    load_pack,
    locator_for_line,
    make_pack,
    order_exclusions,
    order_sources,
    pack_identity,
    save_pack,
)


def source(vault: str, path: str, locator: str = "line:1", digest: str = "a" * 64) -> SourceReceipt:
    return SourceReceipt(
        vault=vault,
        path=path,
        content_sha256=digest,
        locator=locator,
        access="export",
        grant_sha256=None,
        source_tokens=2,
        excerpt="alpha evidence",
    )


def exclusion(vault: str, path: str | None, code: str = ErrorCode.NOT_FOUND.value) -> ExclusionReceipt:
    return ExclusionReceipt(vault=vault, path=path, reason="synthetic exclusion", code=code)


def pack(*, sources: tuple[SourceReceipt, ...] = (), excluded: tuple[ExclusionReceipt, ...] = ()):
    return make_pack(
        protocol_version="1",
        request_id="request-1",
        result="ok",
        selected_vaults=("vault_b", "vault_a"),
        considered_and_dropped=({"vault": "vault_c", "reason": "cutoff"},),
        sources=sources,
        excluded=excluded,
        warnings=("synthetic",),
        tokenizer="brain.lexical/v1",
        policy_sha256="f" * 64,
    )


@pytest.mark.parametrize(
    ("text", "line", "expected"),
    [
        ("# Root\nbody\n## Detail\nmore", 3, "heading:Detail"),
        ("# Root\nbody", 99, "heading:Root"),
        ("plain\nbody", -5, "line:1"),
        ("", 0, "line:1"),
        ("#\nbody", 1, "line:2"),
    ],
)
def test_locator_uses_the_nearest_nonempty_heading_or_a_bounded_line(
    text: str, line: int, expected: str
) -> None:
    assert locator_for_line(text, line) == expected


def test_source_and_exclusion_ordering_is_canonical() -> None:
    sources = (
        source("vault_b", "b.md"),
        source("vault_a", "z.md"),
        source("vault_a", "a.md", "line:2", "b" * 64),
        source("vault_a", "a.md", "line:1", "c" * 64),
    )
    excluded = (
        exclusion("vault_b", None, "z"),
        exclusion("vault_a", "z.md", "a"),
        exclusion("vault_a", None, "b"),
    )
    assert [(item.vault, item.path, item.locator) for item in order_sources(sources)] == [
        ("vault_a", "a.md", "line:1"),
        ("vault_a", "a.md", "line:2"),
        ("vault_a", "z.md", "line:1"),
        ("vault_b", "b.md", "line:1"),
    ]
    assert [(item.vault, item.path, item.code) for item in order_exclusions(excluded)] == [
        ("vault_a", None, "b"),
        ("vault_a", "z.md", "a"),
        ("vault_b", None, "z"),
    ]


def test_make_pack_orders_receipts_and_hashes_the_complete_canonical_payload() -> None:
    left = pack(
        sources=(source("vault_b", "b.md"), source("vault_a", "a.md")),
        excluded=(exclusion("vault_b", None), exclusion("vault_a", "secret.md")),
    )
    right = pack(
        sources=tuple(reversed(left.sources)),
        excluded=tuple(reversed(left.excluded)),
    )
    assert left == right
    assert left.pack_id == f"pack_{left.pack_sha256[:16]}"
    identity_payload = left.as_dict()
    identity_payload.pop("pack_id")
    identity_payload.pop("pack_sha256")
    assert left.pack_sha256 == stable_hash(identity_payload)
    assert pack_identity(left.as_dict()) == (left.pack_id, left.pack_sha256)

    changed_order = make_pack(
        protocol_version="1",
        request_id="request-1",
        result="ok",
        selected_vaults=("vault_a", "vault_b"),
        considered_and_dropped=({"vault": "vault_c", "reason": "cutoff"},),
        sources=left.sources,
        excluded=left.excluded,
        warnings=("synthetic",),
        tokenizer="brain.lexical/v1",
        policy_sha256="f" * 64,
    )
    assert changed_order.pack_sha256 != left.pack_sha256


def test_save_and_load_round_trip_is_canonical_and_replaces_same_receipt(tmp_path: Path) -> None:
    receipt = pack(sources=(source("vault_a", "a.md"),))
    target = save_pack(receipt, tmp_path / "nested")
    assert target.name == f"{receipt.pack_id}.json"
    assert target.read_text(encoding="utf-8") == canonical_json(receipt.as_dict()) + "\n"
    assert load_pack(target.parent, receipt.pack_id) == json.loads(canonical_json(receipt.as_dict()))
    assert save_pack(receipt, target.parent) == target
    assert not list(target.parent.glob("*.tmp"))


def test_save_failure_removes_the_temporary_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = pack()

    def refused(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(provenance.os, "replace", refused)
    with pytest.raises(OSError, match="synthetic replace failure"):
        save_pack(receipt, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_load_refuses_missing_invalid_and_non_object_receipts(tmp_path: Path) -> None:
    with pytest.raises(BrainError) as missing:
        load_pack(tmp_path, "pack_0123456789abcdef")
    assert missing.value.code is ErrorCode.NOT_FOUND

    invalid_json = tmp_path / "pack_1111111111111111.json"
    invalid_json.write_text("{broken", encoding="utf-8")
    with pytest.raises(BrainError) as invalid:
        load_pack(tmp_path, "pack_1111111111111111")
    assert invalid.value.code is ErrorCode.SOURCE_UNREADABLE
    assert invalid.value.details["line"] == 1

    array = tmp_path / "pack_2222222222222222.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(BrainError) as non_object:
        load_pack(tmp_path, "pack_2222222222222222")
    assert non_object.value.code is ErrorCode.SOURCE_UNREADABLE


@pytest.mark.parametrize(
    "pack_id",
    ["", "receipt_0123456789abcdef", "pack_", "pack_ABCDEF0123456789", "pack_deadbeef", "pack_0" * 20],
)
def test_load_refuses_pack_ids_outside_the_generated_shape(tmp_path: Path, pack_id: str) -> None:
    with pytest.raises(BrainError) as caught:
        load_pack(tmp_path, pack_id)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_load_detects_any_receipt_tampering(tmp_path: Path) -> None:
    receipt = pack(sources=(source("vault_a", "a.md"),))
    target = save_pack(receipt, tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["sources"][0]["excerpt"] = "tampered"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BrainError) as caught:
        load_pack(tmp_path, receipt.pack_id)
    assert caught.value.code is ErrorCode.SOURCE_CHANGED
    assert caught.value.details["actual_sha256"] == receipt.pack_sha256
    assert caught.value.details["expected_sha256"] != receipt.pack_sha256


def test_load_rejects_a_valid_payload_stored_under_the_wrong_requested_id(tmp_path: Path) -> None:
    receipt = pack()
    wrong_id = "pack_0123456789abcdef"
    (tmp_path / f"{wrong_id}.json").write_text(canonical_json(receipt.as_dict()), encoding="utf-8")
    with pytest.raises(BrainError) as caught:
        load_pack(tmp_path, wrong_id)
    assert caught.value.code is ErrorCode.SOURCE_CHANGED
