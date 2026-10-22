from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from ruamel.yaml.error import YAMLError

import brain_core.contracts as contracts
from brain_core.contracts import (
    Budget,
    ContextPack,
    ContextRequest,
    ExclusionReceipt,
    ExportSpec,
    Grant,
    NodeEntry,
    SearchSpec,
    SourceReceipt,
    VaultManifest,
    canonical_json,
    grant_from_mapping,
    node_map_from_mapping,
    parse_yaml,
    stable_hash,
    vault_manifest_from_mapping,
)
from brain_core.errors import BrainError, ErrorCode, fail


FOUNDATION = Path(__file__).parents[1] / "fixtures" / "foundation"


def valid_manifest() -> dict[str, object]:
    return {
        "schema": "brain.vault/v1",
        "id": "vault_fixture",
        "name": "Fixture",
        "purpose": "Synthetic contract fixture",
        "topics": ["testing", "contracts"],
        "sensitivity": "private",
        "sync_class": "private-sync",
        "default_branch": "main",
        "intent": "intent.md",
        "record_roots": ["records"],
        "dependencies": ["vault_dependency"],
        "search": {"paths": ["intent.md", "search-only.md"], "purpose": "ranking-only"},
        "exports": [
            {"name": "orientation", "paths": ["intent.md", "intent.md"], "max_bytes": 20_000},
            {"name": "summary", "paths": ["summary.md"]},
        ],
    }


def valid_grant() -> dict[str, object]:
    return {
        "schema": "brain.grant/v1",
        "id": "grant_fixture-01",
        "issuer": "synthetic-user",
        "authenticated_source": "fixture://request/source-message.txt",
        "source_message_sha256": "a" * 64,
        "session_id": "session-fixture",
        "operation": "read",
        "vault": "vault_fixture",
        "paths": ["one.md", "two.md"],
        "issued_at": "2026-08-29T00:00:00Z",
        "expires_at": "2026-08-29T01:00:00+00:00",
    }


EXPECTED_EXIT_CODES = {
    ErrorCode.INVALID_ARGUMENT: 2,
    ErrorCode.PROTOCOL_INVALID: 3,
    ErrorCode.PROTOCOL_VERSION: 4,
    ErrorCode.YAML_INVALID: 10,
    ErrorCode.CONTRACT_INVALID: 11,
    ErrorCode.DUPLICATE_ID: 12,
    ErrorCode.ROOT_MISSING: 20,
    ErrorCode.PATH_ABSOLUTE: 21,
    ErrorCode.PATH_TRAVERSAL: 22,
    ErrorCode.PATH_ESCAPE: 23,
    ErrorCode.PATH_CASE_COLLISION: 24,
    ErrorCode.PATH_MISSING: 25,
    ErrorCode.GRANT_MISSING: 30,
    ErrorCode.GRANT_EXPIRED: 31,
    ErrorCode.GRANT_WRONG_PATH: 32,
    ErrorCode.POLICY_EXCLUDED: 33,
    ErrorCode.INDEX_BUSY: 40,
    ErrorCode.INDEX_BUILD_FAILED: 41,
    ErrorCode.INDEX_STALE: 42,
    ErrorCode.INDEX_MISSING: 43,
    ErrorCode.TOKENIZER_MISMATCH: 44,
    ErrorCode.SOURCE_CHANGED: 50,
    ErrorCode.SOURCE_BINARY: 51,
    ErrorCode.SOURCE_OVERSIZED: 52,
    ErrorCode.SOURCE_UNREADABLE: 53,
    ErrorCode.NOT_FOUND: 60,
    ErrorCode.CAPABILITY_UNAVAILABLE: 61,
    ErrorCode.RECORD_EXISTS: 70,
    ErrorCode.RECORD_TRANSITION: 71,
    ErrorCode.RECORD_CORRUPT: 72,
    ErrorCode.RECORD_WRITE_FAILED: 73,
    ErrorCode.CONTINUITY_EMPTY: 74,
    ErrorCode.MIGRATION_MISMATCH: 75,
    ErrorCode.NEEDS_OWNER_WORDS: 76,
    ErrorCode.REASON_REQUIRED: 77,
    ErrorCode.DONE_NEEDS_CHECK: 78,
    ErrorCode.TIME_NOT_FROM_WORDS: 79,
    ErrorCode.RECORD_CHANGED: 80,
    ErrorCode.ACTOR_MISMATCH: 81,
    ErrorCode.TIME_UNRESOLVED: 82,
}


def assert_contract_error(call, field: str) -> BrainError:
    with pytest.raises(BrainError) as caught:
        call()
    error = caught.value
    assert error.code is ErrorCode.CONTRACT_INVALID
    assert error.details["field"] == field
    return error


def test_error_codes_exit_codes_and_serialization_are_stable() -> None:
    assert set(EXPECTED_EXIT_CODES) == set(ErrorCode)
    for code, exit_code in EXPECTED_EXIT_CODES.items():
        error = BrainError(code, "visible failure", details={"z": 1})
        assert error.exit_code == exit_code
        assert str(error) == "visible failure"
        assert error.as_dict() == {
            "code": code.value,
            "message": "visible failure",
            "details": {"z": 1},
        }

    with_hint = BrainError(ErrorCode.NOT_FOUND, "missing", hint="Choose another source.")
    assert with_hint.details == {}
    assert with_hint.as_dict()["hint"] == "Choose another source."


def test_fail_raises_the_public_error_unchanged() -> None:
    with pytest.raises(BrainError) as caught:
        fail(ErrorCode.INVALID_ARGUMENT, "bad input", details={"field": "x"}, hint="Fix x.")
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert caught.value.message == "bad input"
    assert caught.value.details == {"field": "x"}
    assert caught.value.hint == "Fix x."


def test_canonical_json_and_hash_are_unicode_safe_and_order_independent() -> None:
    left = {"z": [2, 1], "a": "café"}
    right = {"a": "café", "z": [2, 1]}
    rendered = '{"a":"café","z":[2,1]}'
    assert canonical_json(left) == canonical_json(right) == rendered
    assert stable_hash(left) == stable_hash(right)
    assert stable_hash(left) == hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def test_parse_yaml_accepts_a_mapping_and_refuses_non_mappings() -> None:
    assert parse_yaml("name: fixture\ncount: 2\n", "fixture.yaml") == {
        "name": "fixture",
        "count": 2,
    }
    for raw in ("- one\n- two\n", "null\n"):
        with pytest.raises(BrainError) as caught:
            parse_yaml(raw, "fixture.yaml")
        assert caught.value.code is ErrorCode.CONTRACT_INVALID
        assert caught.value.details == {"locator": "fixture.yaml", "expected": "mapping"}


@pytest.mark.parametrize("raw", ["name: one\nname: two\n", "outer: [unterminated\n"])
def test_parse_yaml_reports_syntax_and_duplicate_key_locations(raw: str) -> None:
    with pytest.raises(BrainError) as caught:
        parse_yaml(raw, "broken.yaml")
    error = caught.value
    assert error.code is ErrorCode.YAML_INVALID
    assert error.details["locator"] == "broken.yaml"
    assert error.details["line"] >= 1
    assert error.details["column"] >= 1
    assert error.hint == "Repair the reported YAML syntax or duplicate key."


def test_parse_yaml_handles_a_parser_failure_without_a_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenYaml:
        allow_duplicate_keys = True

        def __init__(self, **_kwargs: object) -> None:
            pass

        def load(self, _text: str) -> object:
            raise YAMLError("synthetic parser failure")

    monkeypatch.setattr(contracts, "YAML", BrokenYaml)
    with pytest.raises(BrainError) as caught:
        parse_yaml("ignored", "unmarked.yaml")
    assert caught.value.details == {"locator": "unmarked.yaml"}


def test_private_scalar_decoders_cover_their_boundary_rules() -> None:
    assert contracts._mapping({"a": 1}, "field", "x") == {"a": 1}
    assert contracts._list([1], "field", "x") == [1]
    assert contracts._string("", "field", "x", allow_empty=True) == ""
    assert contracts._optional_strings(None, "field", "x") == ()
    assert contracts._integer(0, "field", "x", minimum=0) == 0
    assert contracts._one_of("a", {"a", "b"}, "field", "x") == "a"

    for value in (None, 1, ""):
        assert_contract_error(lambda value=value: contracts._string(value, "field", "x"), "field")
    for value in (None, "list"):
        assert_contract_error(lambda value=value: contracts._list(value, "field", "x"), "field")
    assert_contract_error(lambda: contracts._mapping([], "field", "x"), "field")
    for value in (True, 0, "2"):
        assert_contract_error(lambda value=value: contracts._integer(value, "field", "x"), "field")
    with pytest.raises(BrainError) as caught:
        contracts._one_of("c", {"b", "a"}, "mode", "x")
    assert caught.value.details["allowed"] == ["a", "b"]


def test_vault_manifest_decodes_explicit_fields_and_deduplicates_paths() -> None:
    manifest = vault_manifest_from_mapping(valid_manifest(), "brain.yaml", root="C:/synthetic")
    assert manifest.schema == "brain.vault/v1"
    assert manifest.id == "vault_fixture"
    assert manifest.search == SearchSpec(paths=("intent.md", "search-only.md"), purpose="ranking-only")
    assert manifest.exports == (
        ExportSpec("orientation", ("intent.md", "intent.md"), 20_000),
        ExportSpec("summary", ("summary.md",), 20_000),
    )
    assert manifest.export_paths == ("intent.md", "summary.md")
    assert manifest.index_paths == ("intent.md", "search-only.md", "summary.md")
    assert manifest.root == "C:/synthetic"
    assert manifest.as_dict()["root"] == "C:/synthetic"
    with pytest.raises(FrozenInstanceError):
        manifest.name = "changed"  # type: ignore[misc]


def test_vault_manifest_defaults_search_to_exports_and_other_optional_fields() -> None:
    data = {
        "schema": "brain.vault/v1",
        "id": "vault_minimal",
        "name": "Minimal",
        "exports": [{"name": "intent", "paths": ["intent.md"]}],
    }
    manifest = vault_manifest_from_mapping(data, "minimal.yaml")
    assert manifest.purpose == "Unspecified"
    assert manifest.topics == ()
    assert manifest.sensitivity == "private"
    assert manifest.sync_class == "local-only"
    assert manifest.default_branch == "main"
    assert manifest.intent is None
    assert manifest.record_roots == ()
    assert manifest.dependencies == ()
    assert manifest.search == SearchSpec(paths=("intent.md",), purpose="ranking-only")


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"schema": "brain.vault/v2"}, "schema"),
        ({"id": "Vault_Bad"}, "id"),
        ({"name": " "}, "name"),
        ({"purpose": ""}, "purpose"),
        ({"topics": "topic"}, "topics"),
        ({"topics": [1]}, "topics[]"),
        ({"sensitivity": "secret"}, "sensitivity"),
        ({"sync_class": "cloud"}, "sync_class"),
        ({"default_branch": ""}, "default_branch"),
        ({"intent": ""}, "intent"),
        ({"record_roots": "records"}, "record_roots"),
        ({"dependencies": "vault_other"}, "dependencies"),
        ({"exports": "intent.md"}, "exports"),
        ({"exports": [1]}, "exports[0]"),
        ({"exports": [{"name": "", "paths": ["intent.md"]}]}, "exports[0].name"),
        ({"exports": [{"name": "intent", "paths": "intent.md"}]}, "exports[0].paths"),
        ({"exports": [{"name": "intent", "paths": [1]}]}, "exports[0].paths[]"),
        ({"exports": [{"name": "intent", "paths": [], "max_bytes": True}]}, "exports[0].max_bytes"),
        ({"exports": [{"name": "intent", "paths": [], "max_bytes": 0}]}, "exports[0].max_bytes"),
        ({"search": []}, "search"),
        ({"search": {"paths": "intent.md"}}, "search.paths"),
        ({"search": {"paths": [], "purpose": ""}}, "search.purpose"),
    ],
)
def test_vault_manifest_refuses_schema_and_field_errors(change: dict[str, object], field: str) -> None:
    data = valid_manifest()
    data.update(change)
    assert_contract_error(lambda: vault_manifest_from_mapping(data, "brain.yaml"), field)


def test_vault_manifest_requires_schema_and_id_strings() -> None:
    for missing in ("schema", "id"):
        data = valid_manifest()
        data.pop(missing)
        assert_contract_error(lambda data=data: vault_manifest_from_mapping(data, "brain.yaml"), missing)


def test_node_map_decodes_defaults_and_preserves_order() -> None:
    node_map = node_map_from_mapping(
        {
            "schema": "brain.node-map/v1",
            "node": "synthetic-local",
            "vaults": [
                {"id": "vault_career", "root": "vaults/career"},
                {"id": "vault_augmentus", "root": "vaults/augmentus", "manifest": "custom.yaml"},
            ],
        },
        "node-map.yaml",
    )
    assert node_map.node == "synthetic-local"
    assert node_map.vaults == (
        NodeEntry("vault_career", "vaults/career", "brain.yaml"),
        NodeEntry("vault_augmentus", "vaults/augmentus", "custom.yaml"),
    )


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"schema": "brain.node-map/v2", "node": "local", "vaults": []}, "schema"),
        ({"schema": "brain.node-map/v1", "node": "", "vaults": []}, "node"),
        ({"schema": "brain.node-map/v1", "node": "local", "vaults": None}, "vaults"),
        ({"schema": "brain.node-map/v1", "node": "local", "vaults": [1]}, "vaults[0]"),
        (
            {"schema": "brain.node-map/v1", "node": "local", "vaults": [{"id": "", "root": "x"}]},
            "vaults[0].id",
        ),
        (
            {"schema": "brain.node-map/v1", "node": "local", "vaults": [{"id": "v", "root": ""}]},
            "vaults[0].root",
        ),
        (
            {
                "schema": "brain.node-map/v1",
                "node": "local",
                "vaults": [{"id": "v", "root": "x", "manifest": ""}],
            },
            "vaults[0].manifest",
        ),
    ],
)
def test_node_map_refuses_invalid_fields(data: dict[str, object], field: str) -> None:
    assert_contract_error(lambda: node_map_from_mapping(data, "node-map.yaml"), field)


def test_grant_decodes_timestamps_allows_exact_scope_and_hashes_canonically() -> None:
    grant = grant_from_mapping(valid_grant(), "grant.yaml")
    issued = datetime(2026, 8, 29, tzinfo=timezone.utc)
    assert grant.issued_at == issued
    assert grant.expires_at == issued + timedelta(hours=1)
    assert grant.allows("vault_fixture", "one.md", issued)
    assert not grant.allows("vault_other", "one.md", issued)
    assert not grant.allows("vault_fixture", "other.md", issued)
    assert not grant.allows("vault_fixture", "one.md", issued - timedelta(microseconds=1))
    assert not grant.allows("vault_fixture", "one.md", grant.expires_at)
    assert grant.sha256 == stable_hash(grant.as_dict())
    assert grant.as_dict()["issued_at"] == "2026-08-29T00:00:00+00:00"

    non_read = Grant(**{**grant.as_dict(), "operation": "write"})  # type: ignore[arg-type]
    assert not non_read.allows("vault_fixture", "one.md", issued)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"schema": "brain.grant/v2"}, "schema"),
        ({"id": "bad-grant"}, "id"),
        ({"issuer": ""}, "issuer"),
        ({"authenticated_source": ""}, "authenticated_source"),
        ({"source_message_sha256": "A" * 64}, "source_message_sha256"),
        ({"source_message_sha256": "a" * 63}, "source_message_sha256"),
        ({"session_id": ""}, "session_id"),
        ({"operation": "write"}, "operation"),
        ({"vault": ""}, "vault"),
        ({"paths": "one.md"}, "paths"),
        ({"paths": [1]}, "paths[]"),
        ({"issued_at": "not-a-time"}, "issued_at"),
        ({"issued_at": "2026-08-29T00:00:00"}, "issued_at"),
        ({"expires_at": "2026-08-29T01:00:00"}, "expires_at"),
        ({"expires_at": "2026-08-29T00:00:00Z"}, "expires_at"),
        ({"expires_at": "2026-08-28T23:59:59Z"}, "expires_at"),
    ],
)
def test_grant_refuses_invalid_scope_identity_hash_and_timestamps(
    change: dict[str, object], field: str
) -> None:
    data = valid_grant()
    data.update(change)
    assert_contract_error(lambda: grant_from_mapping(data, "grant.yaml"), field)


def test_grant_requires_schema_and_timestamp_strings() -> None:
    for missing in ("schema", "issued_at", "expires_at"):
        data = valid_grant()
        data.pop(missing)
        assert_contract_error(lambda data=data: grant_from_mapping(data, "grant.yaml"), missing)


def test_request_receipt_and_pack_contracts_have_deterministic_defaults() -> None:
    request = ContextRequest(request_id="request-1", session_id="session-1", intent="Compare evidence")
    assert request.budget == Budget(source_tokens=12_000, vaults=4, excerpts=12)
    assert request.named_vaults == ()
    assert request.grants == ()

    source = SourceReceipt(
        vault="vault_fixture",
        path="intent.md",
        content_sha256="b" * 64,
        locator="intent.md#L1",
        access="export",
        source_tokens=3,
        excerpt="Synthetic intent.",
    )
    exclusion = ExclusionReceipt(
        vault="vault_other", path=None, reason="No grant", code=ErrorCode.GRANT_MISSING.value
    )
    pack = ContextPack(
        protocol_version="1",
        request_id="request-1",
        result="evidence-insufficient",
        selected_vaults=("vault_fixture",),
        considered_and_dropped=({"vault": "vault_other", "reason": "cutoff"},),
        sources=(source,),
        excluded=(exclusion,),
        warnings=("Synthetic fixture only",),
        tokenizer="brain.lexical/v1",
        policy_sha256="c" * 64,
        pack_sha256="d" * 64,
    )
    assert pack.as_dict()["sources"][0]["path"] == "intent.md"
    assert pack.as_dict()["excluded"][0]["path"] is None


def test_foundation_fixtures_model_current_contract_and_exact_five_path_grants() -> None:
    node_data = parse_yaml((FOUNDATION / "node-map.yaml").read_text(encoding="utf-8"), "node-map.yaml")
    node_map = node_map_from_mapping(node_data, "node-map.yaml")
    assert tuple(entry.id for entry in node_map.vaults) == ("vault_career", "vault_augmentus")

    manifests: dict[str, VaultManifest] = {}
    for entry in node_map.vaults:
        manifest_path = FOUNDATION / entry.root / entry.manifest
        manifest_data = parse_yaml(manifest_path.read_text(encoding="utf-8"), str(manifest_path))
        manifests[entry.id] = vault_manifest_from_mapping(manifest_data, str(manifest_path))

    assert manifests["vault_career"].search.paths == ("intent.md",)
    assert manifests["vault_augmentus"].search.paths == ("intent.md",)
    assert manifests["vault_career"].export_paths == ("intent.md",)
    assert manifests["vault_augmentus"].export_paths == ("intent.md",)

    grants = []
    for path in sorted((FOUNDATION / "grants" / "exact-five-paths").glob("*.yaml")):
        grants.append(grant_from_mapping(parse_yaml(path.read_text(encoding="utf-8"), str(path)), str(path)))
    granted = {(grant.vault, path) for grant in grants for path in grant.paths}
    assert granted == {
        ("vault_career", "profile/career-record.md"),
        ("vault_career", "profile/expertise.md"),
        ("vault_career", "strategy/positioning.md"),
        ("vault_career", "materials/resumes/base-pmm.md"),
        ("vault_augmentus", "reference/jd-augmentus-marketing.md"),
    }
    roots = {entry.id: FOUNDATION / entry.root for entry in node_map.vaults}
    assert all((roots[vault] / path).is_file() for vault, path in granted)

    source_message = (FOUNDATION / "grants" / "source-message.txt").read_bytes()
    source_message_sha256 = hashlib.sha256(source_message).hexdigest()
    assert source_message_sha256 == "f1b13ea5fec0e732d2ba1fe034f9f512ed8c1b01b42efca127644aca3be8db92"
    assert all(grant.source_message_sha256 == source_message_sha256 for grant in grants)

    expected_hashes = parse_yaml(
        (FOUNDATION / "expected-content-sha256.yaml").read_text(encoding="utf-8"),
        "expected-content-sha256.yaml",
    )["files"]
    for expected in expected_hashes:
        content = (roots[expected["vault"]] / expected["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected["sha256"]
