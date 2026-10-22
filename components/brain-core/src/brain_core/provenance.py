"""Canonical source ordering, pack identity, and disposable receipts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from brain_core.contracts import ContextPack, ExclusionReceipt, SourceReceipt, canonical_json, stable_hash
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import read_local_text


def locator_for_line(text: str, line_index: int) -> str:
    lines = text.splitlines()
    bounded = min(max(line_index, 0), max(len(lines) - 1, 0))
    for index in range(bounded, -1, -1):
        stripped = lines[index].strip() if lines else ""
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return f"heading:{heading}"
    return f"line:{bounded + 1}"


def order_sources(sources: Iterable[SourceReceipt]) -> tuple[SourceReceipt, ...]:
    return tuple(sorted(sources, key=lambda item: (item.vault, item.path, item.locator, item.content_sha256)))


def order_exclusions(excluded: Iterable[ExclusionReceipt]) -> tuple[ExclusionReceipt, ...]:
    return tuple(sorted(excluded, key=lambda item: (item.vault, item.path or "", item.code, item.reason)))


def pack_identity(payload: dict[str, Any]) -> tuple[str, str]:
    identity_payload = dict(payload)
    identity_payload.pop("pack_id", None)
    identity_payload.pop("pack_sha256", None)
    digest = stable_hash(identity_payload)
    return f"pack_{digest[:16]}", digest


def make_pack(
    *,
    protocol_version: str,
    request_id: str,
    result: str,
    selected_vaults: tuple[str, ...],
    considered_and_dropped: tuple[dict[str, str], ...],
    sources: Iterable[SourceReceipt],
    excluded: Iterable[ExclusionReceipt],
    warnings: tuple[str, ...],
    tokenizer: str,
    policy_sha256: str,
) -> ContextPack:
    ordered_sources = order_sources(sources)
    ordered_excluded = order_exclusions(excluded)
    payload: dict[str, Any] = {
        "protocol_version": protocol_version,
        "request_id": request_id,
        "result": result,
        "selected_vaults": list(selected_vaults),
        "considered_and_dropped": list(considered_and_dropped),
        "sources": [asdict(source) for source in ordered_sources],
        "excluded": [asdict(item) for item in ordered_excluded],
        "warnings": list(warnings),
        "tokenizer": tokenizer,
        "policy_sha256": policy_sha256,
    }
    pack_id, digest = pack_identity(payload)
    return ContextPack(
        protocol_version=protocol_version,
        request_id=request_id,
        result=result,
        pack_id=pack_id,
        selected_vaults=selected_vaults,
        considered_and_dropped=considered_and_dropped,
        sources=ordered_sources,
        excluded=ordered_excluded,
        warnings=warnings,
        tokenizer=tokenizer,
        policy_sha256=policy_sha256,
        pack_sha256=digest,
    )


def save_pack(pack: ContextPack, receipt_dir: str | Path) -> Path:
    directory = Path(receipt_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{pack.pack_id}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{pack.pack_id}-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(pack.as_dict()))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def load_pack(receipt_dir: str | Path, pack_id: str) -> dict[str, Any]:
    if re.fullmatch(r"pack_[0-9a-f]{16}", pack_id) is None:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "Invalid pack ID",
            details={"pack_id": pack_id},
        )
    path = Path(receipt_dir) / f"{pack_id}.json"
    if not path.is_file():
        raise BrainError(
            ErrorCode.NOT_FOUND,
            f"Pack receipt not found: {pack_id}",
            details={"pack_id": pack_id, "receipt_dir": str(receipt_dir)},
        )
    try:
        payload = json.loads(read_local_text(path, max_bytes=5_000_000))
    except json.JSONDecodeError as exc:
        raise BrainError(
            ErrorCode.SOURCE_UNREADABLE,
            f"Pack receipt is invalid JSON: {pack_id}",
            details={"pack_id": pack_id, "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(payload, dict):
        raise BrainError(
            ErrorCode.SOURCE_UNREADABLE,
            f"Pack receipt is not an object: {pack_id}",
            details={"pack_id": pack_id},
        )
    if payload.get("pack_id") != pack_id:
        raise BrainError(
            ErrorCode.SOURCE_CHANGED,
            f"Pack receipt identity mismatch: {pack_id}",
            details={"requested_pack_id": pack_id, "payload_pack_id": payload.get("pack_id")},
        )
    expected_id, expected_hash = pack_identity(payload)
    if payload.get("pack_id") != expected_id or payload.get("pack_sha256") != expected_hash:
        raise BrainError(
            ErrorCode.SOURCE_CHANGED,
            f"Pack receipt hash mismatch: {pack_id}",
            details={"pack_id": pack_id, "expected_sha256": expected_hash, "actual_sha256": payload.get("pack_sha256")},
        )
    return payload
