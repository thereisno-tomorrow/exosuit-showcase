from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest

from brain_core.durability import (
    CATALOG_BYTES,
    CATALOG_ID,
    CATALOG_PATH,
    CATALOG_SCHEMA,
    CATALOG_SHA256,
    EMPTY_SHA256,
    SharedDurabilityWriter,
)
from brain_core.errors import BrainError


NOW = "2026-09-01T20:00:00+08:00"
SOURCE_HASH = hashlib.sha256(b"source context").hexdigest()
DELTA_HASH = hashlib.sha256(b"session delta").hexdigest()


def roots(tmp_path: Path) -> tuple[Path, Path]:
    brain_home = tmp_path / "brain-home"
    meta = tmp_path / "meta-vault"
    brain_home.mkdir()
    (meta / "ops").mkdir(parents=True)
    (meta / "ops" / "decisions.md").write_text("# Decisions\n", encoding="utf-8", newline="\n")
    return brain_home, meta


def writer(tmp_path: Path, *, fault=None) -> tuple[SharedDurabilityWriter, Path, Path]:
    brain_home, meta = roots(tmp_path)
    return (
        SharedDurabilityWriter(
            root_bindings={"vault://brain-home": brain_home, "vault://meta-vault": meta},
            fault=fault,
        ),
        brain_home,
        meta,
    )


def request(
    *,
    verb: str = "decide",
    origin_id: str = "origin-a",
    explicit_scope: dict[str, object] | None = None,
    default_scope: dict[str, object] | None = None,
    expected_owner_locator: str = "vault://brain-home",
    observed_preimage_sha256: str = EMPTY_SHA256,
) -> dict[str, object]:
    return {
        "schema": "brain.durability-request/v1",
        "verb": verb,
        "operation": "accept",
        "origin_id": origin_id,
        "choice": "Use one shared durability writer",
        "title": "Use one shared durability writer",
        "because": "Capture and Decide must converge",
        "forecloses": "Separate canonical writers",
        "governing_scope": "Durable decisions for this owner",
        "encoded_in": "UNENCODED",
        "completeness_check": "One semantic origin remains",
        "explicit_scope": explicit_scope,
        "default_scope": default_scope or {"kind": "personal", "project_id": None},
        "source_context_sha256": SOURCE_HASH,
        "session_delta_sha256": DELTA_HASH if verb == "capture" else None,
        "accepted_at": NOW,
        "ownership_catalog": {
            "path": CATALOG_PATH,
            "bytes": CATALOG_BYTES,
            "sha256": CATALOG_SHA256,
            "schema": CATALOG_SCHEMA,
            "catalog_id": CATALOG_ID,
        },
        "expected_owner_locator": expected_owner_locator,
        "expected_writer_generation": 1,
        "observed_preimage_sha256": observed_preimage_sha256,
    }


def test_t01_concurrent_capture_decide_one_origin(tmp_path: Path) -> None:
    shared, brain_home, meta = writer(tmp_path)
    capture = request(verb="capture", origin_id="capture-origin")
    decide = request(verb="decide", origin_id="decide-origin")
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(shared.write, (capture, decide)))

    assert sorted(item.status for item in receipts) == ["created", "duplicate/no-op"]
    assert {item.writer_id for item in receipts} == {"brain-core-shared-durability-writer/v1"}
    assert len(tuple((brain_home / "records" / "decision").rglob("*.md"))) == 1
    assert (meta / "ops" / "decisions.md").read_text(encoding="utf-8") == "# Decisions\n"


def test_submitting_agent_evidence_preserves_nic_and_canonical_retry(tmp_path: Path) -> None:
    from brain_core.records import parse_record
    shared, brain_home, _ = writer(tmp_path)
    value = request()
    value["source_evidence"] = [{
        "source_type": "authenticated_host", "locator": "gt:local-codex",
        "content_sha256": SOURCE_HASH, "actor": "local-codex",
        "excerpt": "Authenticated submitter; decision accepted by Nic",
    }]
    receipt = shared.write(value)
    assert receipt.status == "created"
    canonical = next((brain_home / "records" / "decision").rglob("*.md"))
    before = canonical.read_bytes()
    record = parse_record(before.decode("utf-8"), str(canonical))
    assert [item.actor for item in record.evidence] == ["nic", "local-codex"]
    assert shared.write(value).canonical_sha256 == receipt.canonical_sha256
    assert canonical.read_bytes() == before
    value["source_evidence"][0]["actor"] = "different-agent"
    assert shared.write(value).refusal_code == "origin_evidence_conflict"
    assert canonical.read_bytes() == before
    value["origin_id"] = "independent-semantic-origin"
    assert shared.write(value).status == "duplicate/no-op"


@pytest.mark.parametrize("evidence", [None, {}, [None], [{"actor": "forged"}], [{
    "source_type": "host", "locator": "gt:local-codex", "content_sha256": SOURCE_HASH,
    "credential": "not-allowed",
}]])
def test_invalid_submitting_evidence_refused_before_write(tmp_path: Path, evidence: object) -> None:
    shared, brain_home, meta = writer(tmp_path)
    value = request()
    value["source_evidence"] = evidence
    with pytest.raises(BrainError):
        shared.write(value)
    assert not list(brain_home.rglob("*.md"))
    assert (meta / "ops" / "decisions.md").read_text() == "# Decisions\n"


def test_t02_decision_watch_precedes_capture(tmp_path: Path) -> None:
    shared, brain_home, _ = writer(tmp_path)
    watch = shared.write(request(verb="decide", origin_id="watch-origin"))
    capture = shared.write(request(verb="capture", origin_id="capture-origin"))
    continuity_ran_after_decision = capture.status == "duplicate/no-op"

    assert watch.status == "created"
    assert capture.status == "duplicate/no-op"
    assert continuity_ran_after_decision is True
    assert len(tuple((brain_home / "records" / "decision").rglob("*.md"))) == 1


def test_t03_crash_after_publish_before_receipt_retry(tmp_path: Path) -> None:
    def crash(stage: str, _path: Path) -> None:
        if stage == "after_canonical_publish_before_receipt":
            raise RuntimeError("crash")

    shared, brain_home, meta = writer(tmp_path, fault=crash)
    value = request(origin_id="retry-origin")
    with pytest.raises(RuntimeError, match="crash"):
        shared.write(value)
    canonical = next((brain_home / "records" / "decision").rglob("*.md"))
    pre_retry = canonical.read_bytes()

    script = (
        "import json,sys;"
        "from brain_core.durability import SharedDurabilityWriter;"
        "value=json.loads(sys.argv[1]);"
        "writer=SharedDurabilityWriter(root_bindings={'vault://brain-home':sys.argv[2],'vault://meta-vault':sys.argv[3]});"
        "print(json.dumps(writer.write(value).as_dict(),sort_keys=True))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(value), str(brain_home), str(meta)],
        cwd=Path(__file__).parents[2], capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["status"] == "created"
    assert canonical.read_bytes() == pre_retry
    assert len(tuple((brain_home / "records" / "decision").rglob("*.md"))) == 1


def test_t04_explicit_scope_precedes_default(tmp_path: Path) -> None:
    shared, brain_home, meta = writer(tmp_path)
    meta_before = (meta / "ops" / "decisions.md").read_bytes()
    receipt = shared.write(
        request(
            explicit_scope={"kind": "personal", "project_id": None},
            default_scope={"kind": "system", "project_id": None},
        )
    )

    assert receipt.status == "created"
    assert receipt.owner_locator == "vault://brain-home"
    assert len(tuple((brain_home / "records" / "decision").rglob("*.md"))) == 1
    assert (meta / "ops" / "decisions.md").read_bytes() == meta_before


def test_t05_ambiguous_default_refuses_without_global_write(tmp_path: Path) -> None:
    shared, brain_home, meta = writer(tmp_path)
    meta_before = (meta / "ops" / "decisions.md").read_bytes()
    receipt = shared.write(request(default_scope={"kind": "ambiguous", "project_id": None}))

    assert receipt.status == "failure"
    assert receipt.refusal_code == "scope_ambiguous"
    assert not (brain_home / "records").exists()
    assert (meta / "ops" / "decisions.md").read_bytes() == meta_before


def test_t07_old_entrypoint_race_one_origin(tmp_path: Path) -> None:
    shared, brain_home, _ = writer(tmp_path)

    def old_capture_adapter(value: dict[str, object]):
        return shared.write(value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_future = pool.submit(old_capture_adapter, request(verb="capture", origin_id="old-origin"))
        direct_future = pool.submit(shared.write, request(verb="decide", origin_id="direct-origin"))
    receipts = (old_future.result(), direct_future.result())

    assert sorted(item.status for item in receipts) == ["created", "duplicate/no-op"]
    assert len(tuple((brain_home / "records" / "decision").rglob("*.md"))) == 1


def test_t08_bounded_io_and_zero_second_model_call(tmp_path: Path) -> None:
    shared, brain_home, meta = writer(tmp_path)
    meta_before = (meta / "ops" / "decisions.md").read_bytes()
    receipt = shared.write(request())
    relative_files = {
        path.relative_to(brain_home).as_posix()
        for path in brain_home.rglob("*")
        if path.is_file()
    }

    assert receipt.model_calls == 0
    assert receipt.network_calls == 0
    assert len([path for path in relative_files if path.endswith(".md")]) == 1
    assert "records/.durability/writer.lock" in relative_files
    assert "records/.durability/receipts/origin-a.json" in relative_files
    assert (meta / "ops" / "decisions.md").read_bytes() == meta_before


def test_t09_frontstage_requires_observed_postimage(tmp_path: Path) -> None:
    def forge(stage: str, path: Path) -> None:
        if stage == "after_canonical_publish_before_receipt":
            path.write_text("forged\n", encoding="utf-8", newline="\n")

    shared, brain_home, _ = writer(tmp_path, fault=forge)
    receipt = shared.write(request())

    assert receipt.status == "failure"
    assert receipt.refusal_code in {"brain.record.corrupt", "canonical_postimage_changed"}
    assert not (brain_home / "records" / ".durability" / "receipts" / "origin-a.json").exists()


def test_markdown_owner_is_atomic_idempotent_and_origin_bound(tmp_path: Path) -> None:
    shared, _, meta = writer(tmp_path)
    decisions = meta / "ops" / "decisions.md"
    preimage = hashlib.sha256(decisions.read_bytes()).hexdigest()
    system = request(
        explicit_scope={"kind": "system", "project_id": None},
        default_scope={"kind": "personal", "project_id": None},
        expected_owner_locator="vault://meta-vault",
        observed_preimage_sha256=preimage,
    )
    created = shared.write(system)
    retry = shared.write({**system, "observed_preimage_sha256": "f" * 64})
    duplicate = shared.write({**system, "origin_id": "origin-b", "observed_preimage_sha256": "f" * 64})

    text = decisions.read_text(encoding="utf-8")
    assert created.status == retry.status == "created"
    assert duplicate.status == "duplicate/no-op"
    assert text.count("  Origin-ID: origin-a\n") == 1
    assert text.count(f"  Semantic-Key: {created.semantic_key}\n") == 1
    assert text.count("Decision-ID:") == 1

    conflicting = {**system, "choice": "A different choice", "title": "A different choice"}
    refused = shared.write(conflicting)
    assert refused.status == "failure"
    assert refused.refusal_code == "origin_id_conflict"
    assert decisions.read_text(encoding="utf-8") == text


def test_project_route_requires_an_exported_decision_surface(tmp_path: Path) -> None:
    brain_home, meta = roots(tmp_path)
    project = tmp_path / "project-alpha"
    (project / "ops").mkdir(parents=True)
    decisions = project / "ops" / "decisions.md"
    decisions.write_text("# Decisions\n", encoding="utf-8", newline="\n")
    (project / "ops" / "vault-manifest.md").write_text(
        "---\nexports:\n  decisions: ops/decisions.md\n---\n\n# Project manifest\n",
        encoding="utf-8",
        newline="\n",
    )
    shared = SharedDurabilityWriter(
        root_bindings={"vault://brain-home": brain_home, "vault://meta-vault": meta},
        project_roots={"project-alpha": project},
    )
    value = request(
        explicit_scope={"kind": "project", "project_id": "project-alpha"},
        expected_owner_locator="vault://project/project-alpha",
        observed_preimage_sha256=hashlib.sha256(decisions.read_bytes()).hexdigest(),
    )
    created = shared.write(value)
    assert created.status == "created"
    assert created.owner_locator == "vault://project/project-alpha"

    blocked = tmp_path / "project-blocked"
    (blocked / "ops").mkdir(parents=True)
    blocked_decisions = blocked / "ops" / "decisions.md"
    blocked_decisions.write_text("# Decisions\n", encoding="utf-8", newline="\n")
    (blocked / "ops" / "vault-manifest.md").write_text(
        "---\nexports:\n  compass: ops/compass.md\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    blocked_writer = SharedDurabilityWriter(
        root_bindings={"vault://brain-home": brain_home, "vault://meta-vault": meta},
        project_roots={"project-blocked": blocked},
    )
    refused = blocked_writer.write(
        request(
            origin_id="blocked-origin",
            explicit_scope={"kind": "project", "project_id": "project-blocked"},
            expected_owner_locator="vault://project/project-blocked",
            observed_preimage_sha256=hashlib.sha256(blocked_decisions.read_bytes()).hexdigest(),
        )
    )
    assert refused.status == "failure"
    assert refused.refusal_code == "project_decisions_unavailable"
    assert blocked_decisions.read_text(encoding="utf-8") == "# Decisions\n"
    assert not (blocked / "ops" / ".durability").exists()


def test_project_route_rejects_malformed_manifest_frontmatter(tmp_path: Path) -> None:
    brain_home, meta = roots(tmp_path)
    project = tmp_path / "project-invalid"
    (project / "ops").mkdir(parents=True)
    decisions = project / "ops" / "decisions.md"
    decisions.write_text("# Decisions\n", encoding="utf-8", newline="\n")
    (project / "ops" / "vault-manifest.md").write_text(
        "---\nexports:\n  decisions: [\n---\n\n# Project manifest\n",
        encoding="utf-8",
        newline="\n",
    )
    shared = SharedDurabilityWriter(
        root_bindings={"vault://brain-home": brain_home, "vault://meta-vault": meta},
        project_roots={"project-invalid": project},
    )

    refused = shared.write(
        request(
            explicit_scope={"kind": "project", "project_id": "project-invalid"},
            expected_owner_locator="vault://project/project-invalid",
            observed_preimage_sha256=hashlib.sha256(decisions.read_bytes()).hexdigest(),
        )
    )

    assert refused.status == "failure"
    assert refused.refusal_code == "project_manifest_invalid"
    assert decisions.read_text(encoding="utf-8") == "# Decisions\n"
    assert not (project / "ops" / ".durability").exists()


def test_brain_record_origin_cannot_claim_two_semantics(tmp_path: Path) -> None:
    shared, brain_home, _ = writer(tmp_path)
    assert shared.write(request()).status == "created"
    refused = shared.write({**request(), "choice": "A different choice", "title": "A different choice"})
    assert refused.status == "failure"
    assert refused.refusal_code == "origin_id_conflict"
    assert len(tuple((brain_home / "records" / "decision").rglob("*.md"))) == 1
