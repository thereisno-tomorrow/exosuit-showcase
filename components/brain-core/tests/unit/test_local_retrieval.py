from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import brain_core.local_retrieval as local_retrieval
from brain_core.contracts import (
    GRANT_SCHEMA,
    VAULT_SCHEMA,
    ExportSpec,
    Grant,
    SearchSpec,
    VaultManifest,
)
from brain_core.errors import BrainError, ErrorCode
from brain_core.local_retrieval import parse_catalog, retrieve, to_ciel_evidence_ref

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def sealed(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "vault"
    root.mkdir()
    bodies = {
        "anchor.md": "opaque fact alpha",
        "linked.md": "provenance owner version content hash",
        "old.md": "old superseded fact",
        "secret.md": "PRIVATE-CANARY forbidden",
        "search-only.md": "searchable but not disclosable",
    }
    for name, text in bodies.items():
        (root / name).write_text(text, encoding="utf-8")
    manifest = VaultManifest(
        VAULT_SCHEMA,
        "vault_alpha",
        "Alpha",
        "Generic sealed evidence",
        ("evidence",),
        "private",
        "local-only",
        "main",
        None,
        (),
        (),
        SearchSpec(("anchor.md", "old.md", "search-only.md")),
        (ExportSpec("public", ("anchor.md", "old.md")),),
        str(root),
    )
    grant = Grant(
        GRANT_SCHEMA,
        "grant_alpha",
        "nic",
        "session",
        "a" * 64,
        "session-1",
        "read",
        "vault_alpha",
        ("linked.md",),
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=10),
    )
    evidence = [
        {
            "evidence_id": "E1",
            "owner_vault": "vault_alpha",
            "relative_path": "anchor.md",
            "content_sha256": digest(root / "anchor.md"),
            "state": "current",
        },
        {
            "evidence_id": "E2",
            "owner_vault": "vault_alpha",
            "relative_path": "linked.md",
            "content_sha256": digest(root / "linked.md"),
            "state": "current",
        },
        {
            "evidence_id": "E3",
            "owner_vault": "vault_alpha",
            "relative_path": "old.md",
            "content_sha256": digest(root / "old.md"),
            "state": "superseded",
        },
        {
            "evidence_id": "E4",
            "owner_vault": "vault_alpha",
            "relative_path": "secret.md",
            "content_sha256": digest(root / "secret.md"),
            "state": "current",
        },
        {
            "evidence_id": "E5",
            "owner_vault": "vault_alpha",
            "relative_path": "search-only.md",
            "content_sha256": digest(root / "search-only.md"),
            "state": "current",
        },
    ]
    document = {
        "schema": "brain.a02-retrieval-catalog/v1",
        "repository_version": "sealed-v1",
        "evidence": evidence,
        "links": [
            {"source": "E1", "target": "E2", "type": "supports"},
            {"source": "E2", "target": "E3", "type": "supersedes"},
        ],
    }
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(document), encoding="utf-8")
    return {
        "root": root,
        "manifest": manifest,
        "grant": grant,
        "document": document,
        "catalog": catalog,
    }


def run(sealed: dict[str, object], intent: str = "opaque fact", **kwargs: object):
    return retrieve(
        sealed["catalog"],
        (sealed["manifest"],),
        intent,
        (sealed["grant"],),
        now=NOW,
        session_id="session-1",
        **kwargs,
    )


def test_a02_linked_gain_current_filter_permission_before_ingestion(
    sealed: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    original = local_retrieval.read_snapshot

    def observed(root: str | Path, locator: str, **kwargs: object):
        reads.append(locator)
        return original(root, locator, **kwargs)

    monkeypatch.setattr(local_retrieval, "read_snapshot", observed)
    result = run(sealed)
    assert [item.evidence_id for item in result.sources] == ["E1", "E2"]
    assert [item.retrieval_method for item in result.sources] == ["fts5", "fts5+typed-links"]
    assert result.ingested_ids == ("E1", "E2", "E5")
    assert reads == ["anchor.md", "linked.md", "search-only.md"]
    assert "old.md" not in reads and "secret.md" not in reads
    assert [asdict(item) for item in result.relations] == [
        {"source_id": "E1", "target_id": "E2", "relation_type": "supports"}
    ]
    serialized = json.dumps([asdict(item) for item in result.exclusions])
    assert all(value not in serialized for value in ("E3", "E4", "PRIVATE-CANARY", "secret.md"))


def test_links_removed_and_disabled_equal_lexical_baseline(sealed: dict[str, object]) -> None:
    disabled = run(sealed, expand_links=False)
    document = dict(sealed["document"])
    document["links"] = []
    no_links = Path(sealed["catalog"]).with_name("no-links.json")
    no_links.write_text(json.dumps(document), encoding="utf-8")
    original = sealed["catalog"]
    sealed["catalog"] = no_links
    removed = run(sealed)
    sealed["catalog"] = original
    assert [item.evidence_id for item in disabled.sources] == ["E1"]
    assert disabled == removed


def test_read_gate_withholds_search_only_evidence(sealed: dict[str, object]) -> None:
    result = run(sealed, "searchable")
    assert result.sources == ()
    assert result.ingested_ids == ("E1", "E2", "E5")
    assert any(item.reason == "matching evidence was not disclosed" for item in result.exclusions)


def test_hash_change_fails_closed(sealed: dict[str, object]) -> None:
    Path(sealed["root"], "anchor.md").write_text("changed", encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        run(sealed)
    assert raised.value.code is ErrorCode.SOURCE_CHANGED


def test_determinism_limit_empty_and_no_match(sealed: dict[str, object]) -> None:
    first = run(sealed)
    assert first == run(sealed)
    assert len(run(sealed, "", result_limit=1).sources) == 1
    assert run(sealed, "not-present").sources == ()
    with pytest.raises(BrainError):
        run(sealed, result_limit=0)
    with pytest.raises(BrainError):
        run(sealed, per_source_tokens=True)


def test_ciel_evidence_mapping_is_exact_and_metadata_only(sealed: dict[str, object]) -> None:
    result = run(sealed)
    mapping = to_ciel_evidence_ref(result.sources[0], result.relations)
    assert set(mapping) == {
        "schema",
        "evidence_id",
        "owner_locator",
        "source_locator",
        "source_version",
        "content_sha256",
        "authority_basis",
        "retrieval_method",
        "supersedes",
        "metadata_only",
    }
    assert mapping["owner_locator"] == "vault://vault_alpha"
    assert mapping["metadata_only"] is True
    assert all(field not in mapping for field in ("excerpt", "body", "prompt", "secret"))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.pop("links"),
        lambda value: value.update(schema="wrong"),
        lambda value: value.update(repository_version=""),
        lambda value: value.update(evidence={}),
        lambda value: value["evidence"][0].update(extra=True),
        lambda value: value["evidence"][0].update(evidence_id="bad id"),
        lambda value: value["evidence"][0].update(content_sha256="bad"),
        lambda value: value["evidence"][0].update(state="unknown"),
        lambda value: value["evidence"].append(dict(value["evidence"][0])),
        lambda value: value["evidence"].append(
            {**value["evidence"][0], "evidence_id": "E9"}
        ),
        lambda value: value["links"][0].update(type="unknown"),
        lambda value: value["links"][0].update(target="missing"),
        lambda value: value["links"].append(dict(value["links"][0])),
    ],
)
def test_catalog_schema_drift_fails_closed(
    sealed: dict[str, object], mutation
) -> None:
    document = json.loads(json.dumps(sealed["document"]))
    mutation(document)
    with pytest.raises(BrainError) as raised:
        parse_catalog(json.dumps(document), "catalog.json")
    assert raised.value.code in {ErrorCode.CONTRACT_INVALID, ErrorCode.PATH_TRAVERSAL}


def test_catalog_duplicate_json_key_and_yaml_failure() -> None:
    with pytest.raises(BrainError):
        parse_catalog(
            '{"schema":"brain.a02-retrieval-catalog/v1","schema":"x",'
            '"repository_version":"v","evidence":[],"links":[]}',
            "catalog.json",
        )
    with pytest.raises(BrainError):
        parse_catalog("schema: [", "catalog.yaml")


def test_invalid_grants_manifests_owner_and_intent_fail_closed(
    sealed: dict[str, object]
) -> None:
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            (sealed["manifest"],),
            "opaque",
            [sealed["grant"]],  # type: ignore[arg-type]
            now=NOW,
            session_id="session-1",
        )
    bad = replace(sealed["grant"], schema="wrong")
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            (sealed["manifest"],),
            "opaque",
            (bad,),
            now=NOW,
            session_id="session-1",
        )
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            (sealed["manifest"],),
            "opaque",
            (sealed["grant"], sealed["grant"]),
            now=NOW,
            session_id="session-1",
        )
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            (),
            "opaque",
            (),
            now=NOW,
            session_id="session-1",
        )
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            (sealed["manifest"], sealed["manifest"]),
            "opaque",
            (),
            now=NOW,
            session_id="session-1",
        )
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            (sealed["manifest"],),
            7,  # type: ignore[arg-type]
            (sealed["grant"],),
            now=NOW,
            session_id="session-1",
        )

    document = json.loads(json.dumps(sealed["document"]))
    document["evidence"][0]["owner_vault"] = "vault_missing"
    missing = Path(sealed["catalog"]).with_name("missing-owner.json")
    missing.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BrainError):
        retrieve(
            missing,
            (sealed["manifest"],),
            "opaque",
            (),
            now=NOW,
            session_id="session-1",
        )


def test_expired_grant_prevents_body_ingestion(
    sealed: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = replace(sealed["grant"], expires_at=NOW)
    reads: list[str] = []
    original = local_retrieval.read_snapshot

    def observed(root: str | Path, locator: str, **kwargs: object):
        reads.append(locator)
        return original(root, locator, **kwargs)

    monkeypatch.setattr(local_retrieval, "read_snapshot", observed)
    result = retrieve(
        sealed["catalog"],
        (sealed["manifest"],),
        "provenance",
        (expired,),
        now=NOW,
        session_id="session-1",
    )
    assert "linked.md" not in reads
    assert "E2" not in result.ingested_ids
    assert result.sources == ()


@pytest.mark.parametrize(
    ("text", "locator"),
    [
        ("{", "bad.json"),
        ("[]", "bad.json"),
        ("schema: [", "bad.yaml"),
        ("- item", "bad.yaml"),
    ],
)
def test_catalog_parse_failures_are_contract_errors(text: str, locator: str) -> None:
    with pytest.raises(BrainError) as raised:
        parse_catalog(text, locator)
    assert raised.value.code is ErrorCode.CONTRACT_INVALID


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["evidence"].__setitem__(0, "wrong"),
        lambda value: value["evidence"][0].update(owner_vault=""),
        lambda value: value["links"].__setitem__(0, "wrong"),
    ],
)
def test_catalog_nominal_entry_types_fail_closed(
    sealed: dict[str, object], mutation
) -> None:
    document = json.loads(json.dumps(sealed["document"]))
    mutation(document)
    with pytest.raises(BrainError):
        parse_catalog(json.dumps(document), "catalog.json")


def test_nominal_types_bounds_and_reverse_link_expansion(
    sealed: dict[str, object]
) -> None:
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            [sealed["manifest"]],  # type: ignore[arg-type]
            "opaque",
            (),
            now=NOW,
            session_id="session-1",
        )
    with pytest.raises(BrainError):
        retrieve(
            sealed["catalog"],
            (sealed["manifest"],),
            "opaque",
            (object(),),  # type: ignore[arg-type]
            now=NOW,
            session_id="session-1",
        )
    with pytest.raises(BrainError):
        run(sealed, result_limit=True)
    with pytest.raises(BrainError):
        run(sealed, per_source_tokens=1501)

    document = json.loads(json.dumps(sealed["document"]))
    document["links"] = [{"source": "E2", "target": "E1", "type": "decides"}]
    reverse = Path(sealed["catalog"]).with_name("reverse.json")
    reverse.write_text(json.dumps(document), encoding="utf-8")
    sealed["catalog"] = reverse
    assert [item.evidence_id for item in run(sealed).sources] == ["E1", "E2"]
