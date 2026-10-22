from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import brain_core.operations as operations
from brain_core.durability import CATALOG_BYTES, CATALOG_ID, CATALOG_PATH, CATALOG_SCHEMA, CATALOG_SHA256, EMPTY_SHA256, SharedDurabilityWriter
from brain_core.errors import BrainError, ErrorCode
from brain_core.protocol import ProtocolRequest
from brain_core.records import RECORD_SCHEMA, new_record_id


FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = "2026-08-29T00:00:00Z"
MOMENT = datetime(2026, 8, 29, tzinfo=UTC)


def request(operation: str, **parameters: object) -> ProtocolRequest:
    return ProtocolRequest("1", "request-operations", operation, dict(parameters))


def evidence(locator: str = "message:1") -> dict[str, str]:
    return {
        "source_type": "session_message",
        "locator": locator,
        "content_sha256": hashlib.sha256(locator.encode()).hexdigest(),
        "excerpt": locator,
        "session_id": "session-a",
        "actor": "nic",
    }


def preference_record() -> dict[str, object]:
    return {
        "schema": RECORD_SCHEMA,
        "id": new_record_id(MOMENT, "a1b2"),
        "kind": "preference",
        "title": "Prefer concise receipts",
        "state": "proposed",
        "created_at": MOMENT.isoformat(),
        "updated_at": MOMENT.isoformat(),
        "assertion": "explicit",
        "confidence": 1.0,
        "sensitivity": "private",
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [evidence()],
        "supersedes": [],
        "dedupe_key": "preference:concise-receipts",
        "data": {"scope": "receipts", "stability": "durable", "evidence": ["message:1"], "correction_history": []},
        "body": "",
    }


def durability_parameters(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "brain.durability-request/v1",
        "verb": "decide",
        "operation": "accept",
        "origin_id": "operation-origin",
        "choice": "Use one shared durability writer",
        "title": "Use one shared durability writer",
        "because": "Both entrypoints must converge",
        "forecloses": "Separate canonical writers",
        "governing_scope": "Personal durable decisions",
        "encoded_in": "UNENCODED",
        "completeness_check": "One semantic origin remains",
        "explicit_scope": None,
        "default_scope": {"kind": "personal", "project_id": None},
        "source_context_sha256": hashlib.sha256(b"source").hexdigest(),
        "session_delta_sha256": None,
        "accepted_at": "2026-09-01T20:00:00+08:00",
        "ownership_catalog": {
            "path": CATALOG_PATH,
            "bytes": CATALOG_BYTES,
            "sha256": CATALOG_SHA256,
            "schema": CATALOG_SCHEMA,
            "catalog_id": CATALOG_ID,
        },
        "expected_owner_locator": "vault://brain-home",
        "expected_writer_generation": 1,
        "observed_preimage_sha256": EMPTY_SHA256,
    }
    value.update(changes)
    return value


def durability_request(**changes: object) -> ProtocolRequest:
    return ProtocolRequest("1", "request-durability", "durability.write", durability_parameters(**changes))


def fixture_catalog(tmp_path: Path) -> tuple[Path, list[str]]:
    source = FIXTURES / "foundation"
    roots = tmp_path / "vaults"
    shutil.copytree(source / "vaults", roots)
    node_map = tmp_path / "node-map.yaml"
    career = (roots / "career").as_posix()
    augmentus = (roots / "augmentus").as_posix()
    node_map.write_text(
        "\n".join(
            (
                "schema: brain.node-map/v1",
                "node: synthetic-local",
                "vaults:",
                "  - id: vault_career",
                f"    root: '{career}'",
                "    manifest: brain.yaml",
                "  - id: vault_augmentus",
                f"    root: '{augmentus}'",
                "    manifest: brain.yaml",
                "",
            )
        ),
        encoding="utf-8",
    )
    grants = [
        str(source / "grants" / "exact-five-paths" / "career.yaml"),
        str(source / "grants" / "exact-five-paths" / "augmentus.yaml"),
    ]
    return node_map, grants


@pytest.mark.parametrize(
    ("helper", "parameters", "name"),
    [
        (operations._required, {}, "value"),
        (operations._required, {"value": ""}, "value"),
        (operations._strings, {"value": "wrong"}, "value"),
        (operations._strings, {"value": [""]}, "value"),
        (operations._positive, {"value": True}, "value"),
        (operations._positive, {"value": 0}, "value"),
        (operations._nonnegative_float, {"value": True}, "value"),
        (operations._nonnegative_float, {"value": -1}, "value"),
        (operations._nonnegative_float, {"value": float("inf")}, "value"),
        (operations._boolean, {"value": "false"}, "value"),
        (operations._optional_string, {"value": 1}, "value"),
        (operations._optional_string, {"value": ""}, "value"),
    ],
)
def test_parameter_helpers_refuse_malformed_values(helper, parameters, name) -> None:
    with pytest.raises(BrainError) as raised:
        if helper is operations._required:
            helper(parameters, name, str)
        elif helper is operations._positive:
            helper(parameters, name, 1)
        elif helper is operations._nonnegative_float:
            helper(parameters, name, 1.0)
        else:
            helper(parameters, name)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_parameter_helpers_accept_defaults_and_typed_values() -> None:
    assert operations._required({"value": "yes"}, "value", str) == "yes"
    assert operations._strings({}, "value") == ()
    assert operations._strings({"value": ["a", "b"]}, "value") == ("a", "b")
    assert operations._positive({}, "value", 7) == 7
    assert operations._positive({"value": 2}, "value", 7) == 2
    assert operations._nonnegative_float({}, "value", 1.5) == 1.5
    assert operations._nonnegative_float({"value": 0}, "value", 1.5) == 0.0
    assert operations._boolean({}, "value") is False
    assert operations._boolean({"value": True}, "value") is True
    assert operations._optional_string({}, "value") is None
    assert operations._optional_string({}, "value", "default") == "default"
    assert operations._optional_string({"value": "set"}, "value") == "set"


@pytest.mark.parametrize("value", [1, "not-a-time", "2026-08-29T00:00:00"])
def test_moment_refuses_untyped_invalid_and_naive_timestamps(value: object) -> None:
    with pytest.raises(BrainError) as raised:
        operations._moment({"now": value})
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_moment_accepts_default_and_aware_timestamps() -> None:
    assert operations._moment({}).tzinfo is not None
    assert operations._moment({"now": NOW}).isoformat() == "2026-08-29T00:00:00+00:00"


def test_catalog_requires_exactly_one_typed_source(tmp_path: Path) -> None:
    for parameters in ({}, {"node_map": "a", "legacy_registry": "b"}, {"node_map": 42}):
        with pytest.raises(BrainError) as raised:
            operations._catalog(parameters)
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(BrainError) as raised:
        operations._catalog({"legacy_registry": 42})
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_legacy_catalog_adapter_and_custom_node(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "compass.md").write_text("legacy evidence", encoding="utf-8")
    registry = tmp_path / "registry.md"
    registry.write_text(
        f"  - vault: {root.as_posix()}\n"
        "    vault-name: Legacy Test\n"
        "    compass: ops/compass.md\n"
        "    loads-from: [ops/compass.md]\n"
        "    domains:\n"
        "      - testing\n",
        encoding="utf-8",
    )
    source, legacy, node, manifests = operations._catalog(
        {"legacy_registry": str(registry), "node": "legacy-test-node"}
    )
    assert (source, legacy, node) == (str(registry), True, "legacy-test-node")
    assert manifests[0].topics == ("testing",)


def test_grants_are_sorted_and_duplicate_ids_fail() -> None:
    paths = [
        str(FIXTURES / "foundation" / "grants" / "exact-five-paths" / "career.yaml"),
        str(FIXTURES / "foundation" / "grants" / "exact-five-paths" / "augmentus.yaml"),
    ]
    grants = operations._grants({"grant_files": paths})
    assert [item.id for item in grants] == sorted(item.id for item in grants)
    with pytest.raises(BrainError) as raised:
        operations._grants({"grant_files": [paths[0], paths[0]]})
    assert raised.value.code is ErrorCode.DUPLICATE_ID


def test_context_request_validates_required_fields_and_optional_expansion() -> None:
    with pytest.raises(BrainError):
        operations._context_request("r", {}, (), "node")
    built = operations._context_request(
        "r",
        {
            "session_id": "session",
            "intent": "compare evidence",
            "vaults": ["vault_career"],
            "facets": ["launch"],
            "source_tokens": 20,
            "vault_budget": 1,
            "excerpt_budget": 2,
            "expansion_facet": "evidence",
        },
        (),
        "node",
    )
    assert built.expansion_facet == "evidence"
    assert built.budget.source_tokens == 20


def test_doctor_success_and_capability_failures(monkeypatch) -> None:
    healthy = operations.doctor()
    assert healthy["fts5"] == "ok"
    assert healthy["network_calls"] == healthy["model_calls"] == 0
    assert operations.dispatch(request("doctor"))["phase"] == "2-records"

    monkeypatch.setattr(operations.sys, "version_info", (3, 11))
    with pytest.raises(BrainError) as raised:
        operations.doctor()
    assert raised.value.code is ErrorCode.CAPABILITY_UNAVAILABLE

    monkeypatch.setattr(operations.sys, "version_info", (3, 14))
    monkeypatch.setattr(
        operations.sqlite3,
        "connect",
        lambda *_: (_ for _ in ()).throw(operations.sqlite3.OperationalError("no fts5")),
    )
    with pytest.raises(BrainError) as raised:
        operations.doctor()
    assert raised.value.code is ErrorCode.CAPABILITY_UNAVAILABLE


def test_dispatch_runs_complete_fixture_pipeline(tmp_path: Path) -> None:
    node_map, grants = fixture_catalog(tmp_path)
    database = tmp_path / "brain.db"
    receipts = tmp_path / "receipts"
    base = {"node_map": str(node_map)}

    discovered = operations.dispatch(request("vault.discover", **base))
    assert discovered["adapter"] == "brain.vault/v1"
    assert len(discovered["vaults"]) == 2

    rebuilt = operations.dispatch(
        request(
            "index.rebuild",
            **base,
            database=str(database),
            grant_files=grants,
            session_id="synthetic-pilot-session",
            now=NOW,
            lock_timeout=0,
        )
    )
    assert rebuilt["files"] == 7
    assert operations.dispatch(request("index.status", database=str(database)))["state"] == "fresh"

    context = {
        **base,
        "database": str(database),
        "grant_files": grants,
        "session_id": "synthetic-pilot-session",
        "intent": "product marketing launch evidence for Augmentus interview",
        "vaults": ["vault_career", "vault_augmentus"],
        "facets": ["launch", "automation", "evidence"],
        "now": NOW,
        "allow_stale": False,
    }
    plan = operations.dispatch(request("context.plan", **context))
    assert set(plan["planned_vaults"]) == {"vault_career", "vault_augmentus"}
    pack = operations.dispatch(
        request("context.build", **context, receipt_dir=str(receipts))
    )
    assert len(pack["sources"]) == 7
    inspected = operations.dispatch(
        request("context.inspect", receipt_dir=str(receipts), pack_id=pack["pack_id"])
    )
    assert inspected["pack_id"] == pack["pack_id"]
    assert inspected["pack_sha256"] == pack["pack_sha256"]


def test_dispatch_runs_skill_operations() -> None:
    path = str(FIXTURES / "skills" / "job-match" / "SKILL.md")
    listed = operations.dispatch(request("skills.list", skill_paths=[path]))
    assert listed["skills"][0]["id"] == "job-match"
    checked = operations.dispatch(request("skills.check", skill_paths=[path]))
    assert checked == {"result": "ok", "count": 1, "model_calls": 0, "network_calls": 0}
    metadata_only = operations.dispatch(
        request(
            "skills.route",
            skill_paths=[path],
            intent="score this job",
            authority=["career-read"],
            project_owner="career",
        )
    )
    assert metadata_only["status"] == "selected"
    assert "instructions" not in metadata_only
    routed = operations.dispatch(
        request(
            "skills.route",
            skill_paths=[path],
            intent="score this job",
            authority=["career-read"],
            project_owner="career",
            load_instructions=True,
        )
    )
    assert routed["selected"]["id"] == "job-match"
    assert "JOB-MATCH-INSTRUCTION-BODY" in routed["instructions"]


def test_dispatch_unknown_operation_is_stable() -> None:
    with pytest.raises(BrainError) as raised:
        operations.dispatch(request("unknown.operation"))
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert "doctor" in raised.value.details["supported"]


def test_record_operations_create_transition_and_read_history(tmp_path: Path) -> None:
    created = operations.dispatch(request("record.create", brain_home=str(tmp_path), record=preference_record()))
    assert created["receipt"]["created"] is True
    duplicate = operations.dispatch(request("record.create", brain_home=str(tmp_path), record=preference_record()))
    assert duplicate["receipt"]["created"] is False
    record_id = created["record"]["id"]
    assert operations.dispatch(request("record.show", brain_home=str(tmp_path), record_id=record_id))["title"] == "Prefer concise receipts"

    revised = operations.dispatch(
        request(
            "record.transition",
            brain_home=str(tmp_path),
            record_id=record_id,
            state="active",
            evidence=evidence("message:2"),
            now=(MOMENT + timedelta(seconds=1)).isoformat(),
            entropy="b2c3",
        )
    )
    assert revised["record"]["state"] == "active"
    current = operations.dispatch(request("record.list", brain_home=str(tmp_path), kind="preference"))
    history = operations.dispatch(request("record.list", brain_home=str(tmp_path), current=False))
    assert current["count"] == 1
    assert history["count"] == 2


def test_durability_write_dispatch_is_strict_and_reports_counters(monkeypatch, tmp_path: Path) -> None:
    brain_home = tmp_path / "brain-home"
    meta = tmp_path / "meta-vault"
    brain_home.mkdir()
    (meta / "ops").mkdir(parents=True)
    (meta / "ops" / "decisions.md").write_text("# Decisions\n", encoding="utf-8", newline="\n")
    shared = SharedDurabilityWriter(root_bindings={"vault://brain-home": brain_home, "vault://meta-vault": meta})
    monkeypatch.setattr(operations, "SharedDurabilityWriter", lambda: shared)

    created = operations.dispatch(durability_request())
    duplicate = operations.dispatch(durability_request(origin_id="operation-origin-b"))

    assert created["status"] == "created"
    assert duplicate["status"] == "duplicate/no-op"
    assert created["model_calls"] == created["network_calls"] == 0
    assert duplicate["model_calls"] == duplicate["network_calls"] == 0
    assert created["writer_id"] == duplicate["writer_id"] == "brain-core-shared-durability-writer/v1"

    malformed = durability_parameters(unknown=True)
    with pytest.raises(BrainError) as raised:
        operations.dispatch(ProtocolRequest("1", "request-durability", "durability.write", malformed))
    assert raised.value.code is ErrorCode.CONTRACT_INVALID


def test_durability_write_dispatch_refuses_ambiguous_scope(monkeypatch, tmp_path: Path) -> None:
    brain_home = tmp_path / "brain-home"
    meta = tmp_path / "meta-vault"
    brain_home.mkdir()
    (meta / "ops").mkdir(parents=True)
    (meta / "ops" / "decisions.md").write_text("# Decisions\n", encoding="utf-8", newline="\n")
    shared = SharedDurabilityWriter(root_bindings={"vault://brain-home": brain_home, "vault://meta-vault": meta})
    monkeypatch.setattr(operations, "SharedDurabilityWriter", lambda: shared)

    result = operations.dispatch(durability_request(default_scope={"kind": "ambiguous", "project_id": None}))
    assert result["status"] == "failure"
    assert result["refusal_code"] == "scope_ambiguous"
    assert not (brain_home / "records").exists()


def test_continuity_operations_recover_incremental_state(tmp_path: Path) -> None:
    common = {"brain_home": str(tmp_path), "session_id": "session-a"}
    started = operations.dispatch(
        request(
            "continuity.start",
            **common,
            intent="Implement Phase 2",
            sources=["plan"],
            next_step="Build records",
            evidence=evidence(),
            now=NOW,
            entropy="a1b2",
        )
    )
    assert started["changed"] is True
    ignored = operations.dispatch(
        request(
            "continuity.append",
            **common,
            event="effect",
            value="noise",
            meaningful=False,
            evidence=evidence("message:2"),
            now=(MOMENT + timedelta(seconds=1)).isoformat(),
        )
    )
    assert ignored["changed"] is False and ignored["receipt"] is None
    operations.dispatch(
        request(
            "continuity.append",
            **common,
            event="blocker",
            value="B2 credentials missing",
            evidence_file=str(_write_yaml(tmp_path / "evidence.yaml", evidence("message:3"))),
            now=(MOMENT + timedelta(seconds=2)).isoformat(),
            entropy="b2c3",
        )
    )
    brief = operations.dispatch(request("continuity.recover", brain_home=str(tmp_path), max_tokens=200))
    assert brief["blockers"] == ["B2 credentials missing"]
    closed = operations.dispatch(
        request(
            "continuity.close",
            **common,
            evidence=evidence("message:4"),
            now=(MOMENT + timedelta(seconds=3)).isoformat(),
            entropy="c3d4",
        )
    )
    assert closed["record"]["state"] == "closed"


def test_continuity_start_names_its_task_and_recover_reads_one_session(tmp_path: Path) -> None:
    """N12b through the protocol: continuity.start takes task_id; recover takes session_id."""

    task_id = "brn_20260829_000000_abcd"
    for session_id, entropy, extra in (
        ("session-a", "a1b2", {"task_id": task_id}),
        ("session-b", "b2c3", {}),
    ):
        operations.dispatch(
            request(
                "continuity.start", brain_home=str(tmp_path), session_id=session_id,
                intent=f"Intent {session_id}", sources=[], evidence=evidence(), now=NOW, entropy=entropy, **extra,
            )
        )
    linked = operations.dispatch(request("continuity.recover", brain_home=str(tmp_path), session_id="session-a"))
    assert linked["session_id"] == "session-a"
    assert linked["task_id"] == task_id
    unlinked = operations.dispatch(request("continuity.recover", brain_home=str(tmp_path), session_id="session-b"))
    assert "task_id" not in unlinked
    with pytest.raises(BrainError) as raised:
        operations.dispatch(
            request(
                "continuity.start", brain_home=str(tmp_path), session_id="session-c", intent="Intent",
                sources=[], evidence=evidence(), now=NOW, entropy="c3d4", task_id="not-a-record",
            )
        )
    assert raised.value.code is ErrorCode.CONTRACT_INVALID


def _write_yaml(path: Path, value: dict[str, str]) -> Path:
    path.write_text("\n".join(f"{key}: '{item}'" for key, item in value.items()) + "\n", encoding="utf-8")
    return path


def test_decision_import_operation_dry_run_and_apply(tmp_path: Path) -> None:
    source = tmp_path / "decisions.md"
    source.write_text(
        "- [2026-08-29] Decision: Test import\n"
        "  Decision-ID: 20260829-test-import\n"
        "  Because: Evidence exists.\n"
        "  Forecloses: No import.\n"
        "  Governs: Tests.\n"
        "  Encoded in: Fixture.\n"
        "  Completeness check: One record.\n"
        "  State: encoded\n",
        encoding="utf-8",
    )
    dry = operations.dispatch(request("decisions.import", source=str(source)))
    assert dry["dry_run"] is True and dry["count"] == 1
    applied = operations.dispatch(request("decisions.import", source=str(source), brain_home=str(tmp_path), apply=True))
    assert applied["dry_run"] is False and applied["created"] == 1


def test_memory_clearout_plan_dispatches_to_the_dry_run(tmp_path: Path) -> None:
    (tmp_path / "records").mkdir()
    result = operations.dispatch(request("memory.clearout.plan", root=str(tmp_path)))
    assert result["dry_run"] is True
    assert result["archive"] == []
    assert result["totals"] == {"archive": 0, "keep": 0}


def test_mapping_input_requires_exactly_one_valid_source(tmp_path: Path) -> None:
    with pytest.raises(BrainError) as raised:
        operations._input_mapping({}, "record", "record_file")
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(BrainError):
        operations._input_mapping({"record": {}, "record_file": "x"}, "record", "record_file")
    with pytest.raises(BrainError):
        operations._input_mapping({"record_file": 7}, "record", "record_file")
    with pytest.raises(BrainError):
        operations._input_mapping({"record": "wrong"}, "record", "record_file")
