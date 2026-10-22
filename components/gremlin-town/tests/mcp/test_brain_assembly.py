from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from gremlin_town.mcp import brain_assembly
from gremlin_town.mcp.brain_assembly import (
    _GITHUB_OPERATIONS,
    BrainAssemblyError,
    DurableProviderState,
    load_owner_policy,
)
from gremlin_town.mcp.brain_runtime import (
    BrainOperation,
    BrainRuntimeError,
    OwnerAuthorization,
    OwnerSelector,
)


FIRST_TIME = datetime.fromisoformat("2026-09-21T12:00:00+08:00")
LATER_TIME = datetime.fromisoformat("2026-10-04T09:30:00+08:00")


@pytest.fixture(autouse=True)
def _reset_warned_incomplete_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dedupe set is process-global (by design: load_owner_policy reloads
    # on every Brain call), so isolate it per test rather than per policy.
    monkeypatch.setattr(brain_assembly, "_WARNED_INCOMPLETE_OWNERS", set())


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _empty_sources(tmp_path: Path) -> Path:
    return _write_json(
        tmp_path / "brain-sources.json",
        {
            "schema": "gt.brain-source-evidence/v1",
            "revision": "fixture-sources-v1",
            "sources": [],
        },
    )


def _authorization() -> OwnerAuthorization:
    return OwnerAuthorization(
        operation=BrainOperation.MEMORY_SAVE,
        access="write",
        selector=OwnerSelector("personal"),
        owner_id="brain-home",
        owner_locator="vault://brain-home",
        policy_revision="fixture-policy-v1",
        capability=object(),
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_assembled_writing_storage_gets_archive_switch(tmp_path, monkeypatch, enabled):
    import anyio
    from dataclasses import replace
    from test_brain_composition import _settings
    from test_github_brain_provider import LANE_B
    real = brain_assembly.GitHubBrainHomeStorage
    built = []
    def fake_storage(**kwargs):
        value = real(**kwargs)
        built.append(value)
        return value
    monkeypatch.setattr(brain_assembly, "GitHubBrainHomeStorage", fake_storage)
    runtime = brain_assembly.build_brain_runtime(replace(_settings(tmp_path), archive_superseded=enabled),
                                               github_transport=LANE_B.FakeGitHub())
    try:
        assert len(built) == 1 and built[0].archive_superseded is enabled
    finally:
        anyio.run(runtime.aclose)


def test_operation_time_is_durable_across_fresh_state_instances(tmp_path: Path) -> None:
    sources = _empty_sources(tmp_path)
    first = DurableProviderState(tmp_path / "state", sources, clock=lambda: FIRST_TIME)
    assert first.operation_time("ordinary-save-1") == FIRST_TIME
    first.close()

    fresh = DurableProviderState(tmp_path / "state", sources, clock=lambda: LATER_TIME)
    assert fresh.operation_time("ordinary-save-1") == FIRST_TIME
    fresh.close()


# --- P3 / finding 15: operation_time_scoped, a new entry point alongside the -------
# --- unscoped operation_time that continuity and decisions keep using unchanged ----


def test_operation_time_scoped_pins_independent_rows_per_caller(tmp_path: Path) -> None:
    sources = _empty_sources(tmp_path)
    state = DurableProviderState(tmp_path / "state", sources, clock=lambda: FIRST_TIME)

    nic_time = state.operation_time_scoped("nic", "buy-milk")
    damien_time = state.operation_time_scoped("damien", "buy-milk")

    assert nic_time == FIRST_TIME
    assert damien_time == FIRST_TIME  # same fixed clock; the point is they are separate rows
    state.close()

    # Prove independence: a fresh clock only affects a key pinned for the first time.
    # Both callers' rows already exist, so a third, new caller pins the new clock value
    # while nic's and damien's stay exactly as first pinned.
    fresh = DurableProviderState(tmp_path / "state", sources, clock=lambda: LATER_TIME)
    assert fresh.operation_time_scoped("nic", "buy-milk") == FIRST_TIME
    assert fresh.operation_time_scoped("damien", "buy-milk") == FIRST_TIME
    assert fresh.operation_time_scoped("fleet-boss", "buy-milk") == LATER_TIME
    fresh.close()


def test_operation_time_scoped_is_durable_across_fresh_state_instances(tmp_path: Path) -> None:
    sources = _empty_sources(tmp_path)
    first = DurableProviderState(tmp_path / "state", sources, clock=lambda: FIRST_TIME)
    assert first.operation_time_scoped("nic", "renew-passport") == FIRST_TIME
    first.close()

    fresh = DurableProviderState(tmp_path / "state", sources, clock=lambda: LATER_TIME)
    assert fresh.operation_time_scoped("nic", "renew-passport") == FIRST_TIME
    fresh.close()


def test_operation_time_scoped_does_not_collide_with_the_unscoped_key(tmp_path: Path) -> None:
    """The scoped key ("<caller>:<operation_id>") and the plain operation_id string are
    different SQLite rows, so a scoped and an unscoped pin for the same literal
    operation_id text are independent - this is what lets a legacy (pre-scoping) pin
    coexist with a caller-scoped one for the same operation_id."""

    sources = _empty_sources(tmp_path)
    state = DurableProviderState(tmp_path / "state", sources, clock=lambda: FIRST_TIME)
    assert state.operation_time("shared-id") == FIRST_TIME
    state.close()

    fresh = DurableProviderState(tmp_path / "state", sources, clock=lambda: LATER_TIME)
    assert fresh.operation_time_scoped("nic", "shared-id") == LATER_TIME  # a fresh row, not the unscoped one above
    assert fresh.operation_time("shared-id") == FIRST_TIME  # unscoped row untouched
    fresh.close()


def test_operation_time_scoped_validates_caller_and_operation_id_the_same_way(tmp_path: Path) -> None:
    state = DurableProviderState(tmp_path / "state", _empty_sources(tmp_path), clock=lambda: FIRST_TIME)

    with pytest.raises(BrainRuntimeError) as bad_caller:
        state.operation_time_scoped("Not Valid", "ok-id")
    assert bad_caller.value.code == "brain.invalid_argument"

    with pytest.raises(BrainRuntimeError) as bad_operation_id:
        state.operation_time_scoped("nic", "Not Valid")
    assert bad_operation_id.value.code == "brain.invalid_argument"

    state.close()


def test_ordinary_authenticated_request_needs_no_per_message_registry_entry(
    tmp_path: Path,
) -> None:
    state = DurableProviderState(tmp_path / "state", _empty_sources(tmp_path))
    evidence = state.source_evidence(_authorization(), "mcp-request:chat-preference-1")
    state.close()

    assert evidence.source_type == "authenticated_request"
    assert evidence.locator == "gt-mcp://authenticated-request/chat-preference-1"
    assert evidence.actor == "authenticated-mcp-principal"
    assert len(evidence.content_sha256) == 64
    assert "does not independently verify unseen conversation content" in evidence.excerpt


def test_document_reference_still_requires_owner_scoped_enrollment(tmp_path: Path) -> None:
    sources = _empty_sources(tmp_path)
    state = DurableProviderState(tmp_path / "state", sources)
    with pytest.raises(BrainRuntimeError) as denied:
        state.source_evidence(_authorization(), "retrieved-document-1")
    state.close()
    assert denied.value.code == "brain.policy.excluded"


@pytest.mark.parametrize("kind", ["system", "fleet", "project"])
def test_current_owner_adapter_refuses_unimplemented_decision_owners(
    tmp_path: Path, kind: str
) -> None:
    project_id = "gremlin-town" if kind == "project" else None
    policy = {
        "schema": "gt.brain-owner-policy/v1",
        "revision": "fixture-policy-v1",
        "owners": [
            {
                "kind": kind,
                "project_id": project_id,
                "owner_id": project_id or kind,
                "owner_locator": f"vault://{project_id or kind}",
                "readers": ["chatgpt-web"],
                "writers": ["chatgpt-web"],
                "operations": ["decision.record"],
                "session_id": "session-1" if kind == "project" else None,
            }
        ],
    }
    path = _write_json(tmp_path / f"{kind}.json", policy)

    with pytest.raises(BrainAssemblyError, match="different canonical owner adapter"):
        load_owner_policy(path)


def _brain_home_owner(kind: str, operations: list[str]) -> dict:
    return {
        "kind": kind,
        "project_id": None,
        "owner_id": "brain-home",
        "owner_locator": "vault://brain-home",
        "readers": ["chatgpt-web"],
        "writers": ["chatgpt-web"],
        "operations": operations,
        "session_id": None,
    }


def test_load_owner_policy_warns_when_a_brain_home_owner_omits_a_supported_operation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # A release can add a Brain operation (_GITHUB_OPERATIONS) without the
    # live policy's owner lists catching up; startup must warn, not fail
    # silently (found live 2026-09-26).
    policy = {
        "schema": "gt.brain-owner-policy/v1",
        "revision": "fixture-policy-v1",
        "owners": [
            _brain_home_owner("personal", ["decision.record", "memory.save", "memory.read"]),
        ],
    }
    path = _write_json(tmp_path / "policy.json", policy)

    caplog.set_level(logging.WARNING)
    load_owner_policy(path)

    assert "brain-home" in caplog.text
    assert "personal" in caplog.text
    assert "memory.change" in caplog.text
    assert "memory.search" in caplog.text
    assert "continuity.list" in caplog.text


def test_load_owner_policy_warns_only_once_per_process_for_the_same_gap(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # load_owner_policy reloads on every Brain call (the per-call recheck in
    # build_brain_runtime, and api/brain.py's HTTP handler), so the same
    # owner/missing-operations gap must not re-log on every call.
    policy = {
        "schema": "gt.brain-owner-policy/v1",
        "revision": "fixture-policy-v1",
        "owners": [
            _brain_home_owner("personal", ["decision.record", "memory.save", "memory.read"]),
        ],
    }
    path = _write_json(tmp_path / "policy.json", policy)

    caplog.set_level(logging.WARNING)
    load_owner_policy(path)
    load_owner_policy(path)

    assert caplog.text.count("is missing supported operations") == 1


def test_load_owner_policy_is_quiet_when_brain_home_owners_are_complete(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    full_operations = sorted(_GITHUB_OPERATIONS)
    policy = {
        "schema": "gt.brain-owner-policy/v1",
        "revision": "fixture-policy-v1",
        "owners": [
            _brain_home_owner("personal", full_operations),
            _brain_home_owner("cross-project", full_operations),
        ],
    }
    path = _write_json(tmp_path / "policy.json", policy)

    caplog.set_level(logging.WARNING)
    loaded = load_owner_policy(path)

    assert caplog.text == ""
    assert {owner.selector.kind for owner in loaded.owners} == {"personal", "cross-project"}


def test_enrolled_document_evidence_remains_distinct_from_request_provenance(
    tmp_path: Path,
) -> None:
    content_hash = hashlib.sha256(b"retrieved document bytes").hexdigest()
    sources = _write_json(
        tmp_path / "brain-sources.json",
        {
            "schema": "gt.brain-source-evidence/v1",
            "revision": "fixture-sources-v1",
            "sources": [
                {
                    "reference": "retrieved-document-1",
                    "owner_id": "brain-home",
                    "owner_locator": "vault://brain-home",
                    "evidence": {
                        "source_type": "retrieved_document",
                        "locator": "fixture://retrieved/document-1",
                        "content_sha256": content_hash,
                        "excerpt": "bounded retrieved excerpt",
                    },
                }
            ],
        },
    )
    state = DurableProviderState(tmp_path / "state", sources)
    evidence = state.source_evidence(_authorization(), "retrieved-document-1")
    state.close()
    assert evidence.source_type == "retrieved_document"
    assert evidence.content_sha256 == content_hash
